"""Trusted filesystem boundary for identity-bound media files.

Pathnames are locators only.  A media object is trusted through a pinned file
descriptor plus an immutable identity, while application writers coordinate on
an advisory lock held on the private media-root directory inode.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, Mapping, Tuple


_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: Dict[Tuple[int, int], threading.RLock] = {}
_THREAD_STATE = threading.local()
_QUARANTINE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class MediaDirectoryFsyncError(OSError):
    """A directory entry may exist, but its durability is not established."""


def _require_nofollow_support() -> int:
    if not isinstance(_NOFOLLOW, int) or _NOFOLLOW == 0:
        raise RuntimeError("secure_nofollow_unavailable")
    return _NOFOLLOW


def validate_private_media_directory(directory_stat: os.stat_result) -> None:
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ValueError("invalid_media_directory")
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        raise RuntimeError("secure_owner_check_unavailable")
    if directory_stat.st_uid != geteuid():
        raise PermissionError("insecure_media_directory_owner")
    permissions = stat.S_IMODE(directory_stat.st_mode)
    if permissions & 0o077 or permissions & 0o700 != 0o700:
        raise PermissionError("insecure_media_directory_permissions")


def open_private_media_directory(directory: Path) -> Tuple[Path, int]:
    root = directory.resolve(strict=True)
    root_fd = os.open(
        str(root), os.O_RDONLY | _DIRECTORY | _require_nofollow_support(),
    )
    try:
        validate_private_media_directory(os.fstat(root_fd))
    except Exception:
        os.close(root_fd)
        raise
    return root, root_fd


def _flock_retry(fd: int, operation: int) -> None:
    while True:
        try:
            fcntl.flock(fd, operation)
            return
        except InterruptedError:
            continue


def fsync_media_directory(root_fd: int) -> None:
    """Durably persist a directory entry mutation, retrying EINTR."""
    while True:
        try:
            os.fsync(root_fd)
            return
        except InterruptedError:
            continue


def _durably_fsync_directory(root_fd: int) -> None:
    try:
        fsync_media_directory(root_fd)
    except OSError as exc:
        raise MediaDirectoryFsyncError("media_directory_fsync_failed") from exc


def _validated_record_locator(record: Mapping) -> Tuple[Path, str]:
    filepath = record.get("filepath")
    filename = record.get("filename")
    if type(filepath) is not str or type(filename) is not str:
        raise ValueError("invalid_media_locator")
    locator = Path(filepath)
    if locator.name != filename or filename in {"", ".", ".."}:
        raise ValueError("invalid_media_locator")
    return locator.parent, filename


def validate_quarantine_name(
    quarantine_name: str, *, expected_name: str | None = None,
) -> str:
    """Accept only one deterministic private-root quarantine basename."""
    if (
        type(quarantine_name) is not str
        or "\x00" in quarantine_name
        or os.path.basename(quarantine_name) != quarantine_name
        or quarantine_name in {"", ".", ".."}
        or not quarantine_name.startswith(".delete-")
        or _QUARANTINE_TOKEN.fullmatch(quarantine_name[len(".delete-"):]) is None
        or (expected_name is not None and quarantine_name != expected_name)
    ):
        raise ValueError("invalid_quarantine_name")
    return quarantine_name


def quarantine_verified_media(
    record: Mapping, quarantine_name: str, *, expected_name: str | None = None,
) -> str:
    """Move one verified inode into a private same-directory quarantine.

    The caller receives no usable pathname.  The basename is intentionally
    random and is only transient recovery metadata in the database.
    """
    quarantine_name = validate_quarantine_name(
        quarantine_name, expected_name=expected_name,
    )
    root, name = _validated_record_locator(record)
    with media_store_lock(root) as (_locked_root, root_fd):
        try:
            file_fd = os.open(
                quarantine_name, os.O_RDONLY | _require_nofollow_support(),
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            file_fd = os.open(
                name, os.O_RDONLY | _require_nofollow_support(), dir_fd=root_fd,
            )
            source_name = name
        else:
            # This is the hard-crash boundary after rename/fsync and before the
            # intent transaction.  A deterministic intent basename makes it
            # safe to resume without scanning or trusting arbitrary entries.
            source_name = quarantine_name
        try:
            pinned = PinnedMediaFile(
                root_fd, source_name, file_fd, media_identity_from_record(record),
            )
            verify_pinned_media(pinned)
            if source_name == name:
                while True:
                    try:
                        os.rename(name, quarantine_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                        break
                    except InterruptedError:
                        continue
                _durably_fsync_directory(root_fd)
            else:
                # A restart found the deterministic post-rename entry.  It is
                # not a committed quarantine until this retry succeeds.
                _durably_fsync_directory(root_fd)
        finally:
            os.close(file_fd)
    return quarantine_name


def unlink_quarantined_media(
    record: Mapping, quarantine_name: str, *, expected_name: str | None = None,
) -> None:
    """Unlink a random quarantine entry through a trusted directory FD."""
    root, _name = _validated_record_locator(record)
    quarantine_name = validate_quarantine_name(
        quarantine_name, expected_name=expected_name,
    )
    with media_store_lock(root) as (_locked_root, root_fd):
        try:
            file_fd = os.open(
                quarantine_name, os.O_RDONLY | _require_nofollow_support(),
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            # A prior process may have died after unlink and before directory
            # fsync.  Persist that absence before the DB tombstone can happen.
            _durably_fsync_directory(root_fd)
            return
        while True:
            try:
                pinned = PinnedMediaFile(
                    root_fd, quarantine_name, file_fd,
                    media_identity_from_record(record),
                )
                verify_pinned_media(pinned)
                while True:
                    try:
                        os.unlink(quarantine_name, dir_fd=root_fd)
                        break
                    except InterruptedError:
                        continue
                break
            finally:
                os.close(file_fd)
        _durably_fsync_directory(root_fd)


def deletion_entries_are_absent(
    record: Mapping, quarantine_name: str, *, expected_name: str | None = None,
) -> bool:
    """Authorize a crash-recovery tombstone only when both names are absent."""
    quarantine_name = validate_quarantine_name(
        quarantine_name, expected_name=expected_name,
    )
    root, name = _validated_record_locator(record)
    with media_store_lock(root) as (_locked_root, root_fd):
        for entry_name in (name, quarantine_name):
            try:
                os.stat(entry_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            return False
    return True


def verified_delete_entry_state(
    record: Mapping, quarantine_name: str, *, expected_name: str | None = None,
) -> str:
    """Return the one verified durable locator left by a delete attempt.

    This is deliberately a closed set so callers can compensate only when the
    original identity is still present; an uncertain/absent directory state
    must retain the intent for startup reconciliation.
    """
    quarantine_name = validate_quarantine_name(
        quarantine_name, expected_name=expected_name,
    )
    root, name = _validated_record_locator(record)
    with media_store_lock(root) as (_locked_root, root_fd):
        for entry_name, state in ((quarantine_name, "quarantine"), (name, "original")):
            try:
                file_fd = os.open(
                    entry_name, os.O_RDONLY | _require_nofollow_support(),
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                continue
            try:
                verify_pinned_media(PinnedMediaFile(
                    root_fd, entry_name, file_fd, media_identity_from_record(record),
                ))
                return state
            finally:
                os.close(file_fd)
    return "absent"


class MediaStoreLease:
    """Reentrant process/thread lease plus cross-process advisory lock."""

    def __init__(self, root_fd: int):
        directory_stat = os.fstat(root_fd)
        validate_private_media_directory(directory_stat)
        self.root_fd = root_fd
        self.key = (directory_stat.st_dev, directory_stat.st_ino)
        self._thread_lock = None
        self._acquired = False

    def acquire(self) -> "MediaStoreLease":
        if self._acquired:
            raise RuntimeError("media_store_lease_already_acquired")
        with _ROOT_LOCKS_GUARD:
            thread_lock = _ROOT_LOCKS.setdefault(self.key, threading.RLock())
        thread_lock.acquire()
        depths = getattr(_THREAD_STATE, "depths", None)
        if depths is None:
            depths = {}
            _THREAD_STATE.depths = depths
        depth = depths.get(self.key, 0)
        try:
            if depth == 0:
                _flock_retry(self.root_fd, fcntl.LOCK_EX)
            depths[self.key] = depth + 1
        except Exception:
            thread_lock.release()
            raise
        self._thread_lock = thread_lock
        self._acquired = True
        return self

    def release(self) -> None:
        if not self._acquired or self._thread_lock is None:
            return
        depths = getattr(_THREAD_STATE, "depths", {})
        depth = depths.get(self.key, 0)
        try:
            if depth <= 1:
                depths.pop(self.key, None)
                _flock_retry(self.root_fd, fcntl.LOCK_UN)
            else:
                depths[self.key] = depth - 1
        finally:
            self._acquired = False
            self._thread_lock.release()

    def __enter__(self) -> "MediaStoreLease":
        return self.acquire()

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.release()


def media_store_lock_is_held(root_fd: int) -> bool:
    directory_stat = os.fstat(root_fd)
    key = (directory_stat.st_dev, directory_stat.st_ino)
    return getattr(_THREAD_STATE, "depths", {}).get(key, 0) > 0


@contextmanager
def media_store_lock(directory: Path) -> Iterator[Tuple[Path, int]]:
    root, root_fd = open_private_media_directory(Path(directory))
    try:
        with MediaStoreLease(root_fd):
            yield root, root_fd
    finally:
        os.close(root_fd)


def _sha256_file_descriptor(file_fd: int, expected_size: int) -> str:
    pread = getattr(os, "pread", None)
    if pread is None:
        raise RuntimeError("secure_pread_unavailable")
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        try:
            chunk = pread(file_fd, min(1024 * 1024, expected_size - offset), offset)
        except InterruptedError:
            continue
        if not chunk:
            raise ValueError("media_file_identity_changed")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class MediaFileIdentity:
    device: int
    inode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class PinnedMediaFile:
    root_fd: int
    name: str
    file_fd: int
    identity: MediaFileIdentity


def capture_media_identity(file_fd: int) -> MediaFileIdentity:
    before = os.fstat(file_fd)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("invalid_media_file")
    digest = _sha256_file_descriptor(file_fd, before.st_size)
    after = os.fstat(file_fd)
    if (
        not stat.S_ISREG(after.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
    ):
        raise ValueError("media_file_identity_changed")
    return MediaFileIdentity(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        sha256=digest,
    )


def verify_pinned_media(pinned: PinnedMediaFile) -> None:
    """Verify locator, pinned inode, exact size and digest while locked."""
    try:
        if not media_store_lock_is_held(pinned.root_fd):
            raise RuntimeError("media_store_lock_not_held")
        validate_private_media_directory(os.fstat(pinned.root_fd))
        descriptor_stat = os.fstat(pinned.file_fd)
        path_stat = os.stat(
            pinned.name, dir_fd=pinned.root_fd, follow_symlinks=False,
        )
        identity = pinned.identity
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or descriptor_stat.st_dev != identity.device
            or descriptor_stat.st_ino != identity.inode
            or descriptor_stat.st_size != identity.size
            or path_stat.st_dev != identity.device
            or path_stat.st_ino != identity.inode
            or path_stat.st_size != identity.size
            or _sha256_file_descriptor(pinned.file_fd, identity.size)
            != identity.sha256
        ):
            raise ValueError("media_file_identity_changed")
    except RuntimeError:
        raise
    except (FileNotFoundError, OSError, PermissionError, ValueError):
        raise ValueError("media_file_identity_changed") from None


def media_identity_from_record(record: Mapping) -> MediaFileIdentity:
    device = record.get("file_device")
    inode = record.get("file_inode")
    size = record.get("file_size")
    digest = record.get("file_sha256")
    if (
        type(device) is not int
        or device < 0
        or type(inode) is not int
        or inode <= 0
        or type(size) is not int
        or size <= 0
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("media_identity_unavailable")
    return MediaFileIdentity(device, inode, size, digest)


def record_has_media_identity(record: Mapping) -> bool:
    try:
        media_identity_from_record(record)
    except ValueError:
        return False
    return True


@contextmanager
def open_verified_media(record: Mapping) -> Iterator[BinaryIO]:
    """Future publisher contract: verify a stored identity before reading."""
    if record.get("file_deleted"):
        raise ValueError("media_file_deleted")
    filepath = record.get("filepath")
    filename = record.get("filename")
    if type(filepath) is not str or type(filename) is not str:
        raise ValueError("invalid_media_locator")
    locator = Path(filepath)
    if locator.name != filename or filename in {"", ".", ".."}:
        raise ValueError("invalid_media_locator")
    identity = media_identity_from_record(record)
    with media_store_lock(locator.parent) as (_root, root_fd):
        file_fd = os.open(
            locator.name,
            os.O_RDONLY | _require_nofollow_support(),
            dir_fd=root_fd,
        )
        try:
            pinned = PinnedMediaFile(root_fd, locator.name, file_fd, identity)
            verify_pinned_media(pinned)
            with os.fdopen(file_fd, "rb") as media_file:
                file_fd = -1
                yield media_file
        finally:
            if file_fd >= 0:
                os.close(file_fd)
