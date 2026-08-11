"""
Gestisce la libreria media: rilevamento tipo file, estrazione di un
fotogramma dai video (via ffmpeg) da usare come proxy per l'analisi AI, e
registrazione nel database.

Richiede ffmpeg installato sul server per i video:
    sudo apt install ffmpeg
Se ffmpeg non è disponibile, i video vengono comunque salvati e registrati
nella libreria, ma senza descrizione/categoria AI (da compilare a mano
nella dashboard).
"""
import logging
import ntpath
import os
import re
import shutil
import stat
import subprocess
import unicodedata
import uuid
from pathlib import Path
from typing import BinaryIO, Dict, Optional, Tuple

from config import TELEGRAM_MAX_IMAGE_BYTES, TELEGRAM_MAX_VIDEO_BYTES

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
}
BMFF_BRANDS_BY_MIME = {
    "video/mp4": {
        b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6",
        b"mp41", b"mp42", b"avc1", b"dash",
    },
    "video/quicktime": {b"qt  "},
    "video/x-m4v": {b"M4V ", b"M4VH", b"M4VP"},
}
IMAGE_BMFF_BRANDS = {
    b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1",
}
MAX_FTYP_BOX_BYTES = 4096
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def sanitize_media_filename(filename: str) -> str:
    """Return a basename safe to join to the configured media directory."""
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        return ""
    if os.path.basename(filename) != filename or ntpath.basename(filename) != filename:
        return ""
    ascii_name = (
        unicodedata.normalize("NFKD", filename)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^A-Za-z0-9_.-]", "_", ascii_name).strip("._")


def validate_media_upload(
    filename: str,
    mime_type: str,
    file_size: int,
) -> Tuple[bool, str]:
    """Validate untrusted Telegram/dashboard metadata before persistence."""
    safe_name = sanitize_media_filename(filename)
    if not safe_name:
        return False, "invalid_filename"
    extension = os.path.splitext(safe_name)[1].lower()
    expected_mime = MIME_BY_EXTENSION.get(extension)
    if expected_mime is None:
        return False, "unsupported_extension"
    normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime != expected_mime:
        return False, "mime_extension_mismatch"
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size <= 0:
        return False, "invalid_file_size"
    limit = (
        TELEGRAM_MAX_IMAGE_BYTES
        if extension in IMAGE_EXTENSIONS
        else TELEGRAM_MAX_VIDEO_BYTES
    )
    if file_size > limit:
        return False, "file_too_large"
    return True, "ok"


def _sniff_media_kind(header: bytes) -> Optional[str]:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _bmff_matches_mime(header: bytes, mime_type: str) -> bool:
    if len(header) < 16:
        return False
    box_size = int.from_bytes(header[:4], "big")
    if (
        header[4:8] != b"ftyp"
        or box_size < 16
        or box_size > MAX_FTYP_BOX_BYTES
        or box_size > len(header)
        or (box_size - 16) % 4 != 0
    ):
        return False
    major_brand = header[8:12]
    compatible_brands = {
        header[offset:offset + 4]
        for offset in range(16, box_size, 4)
    }
    all_brands = compatible_brands | {major_brand}
    if all_brands & IMAGE_BMFF_BRANDS:
        return False
    return major_brand in BMFF_BRANDS_BY_MIME.get(mime_type, set())


def media_content_matches(stream: BinaryIO, mime_type: str) -> bool:
    """Check a small magic-byte signature without consuming the upload stream."""
    position = stream.tell()
    try:
        header = stream.read(MAX_FTYP_BOX_BYTES)
    finally:
        stream.seek(position)
    detected = _sniff_media_kind(header)
    expected = (mime_type or "").split(";", 1)[0].strip().lower()
    if expected.startswith("video/"):
        return _bmff_matches_mime(header, expected)
    return detected == expected


def stage_media_upload(stream: BinaryIO, directory: str, filename: str) -> str:
    """Copy an upload to an exclusive server-generated file in ``directory``."""
    safe_name = sanitize_media_filename(filename)
    if not safe_name:
        raise ValueError("invalid_filename")
    extension = os.path.splitext(safe_name)[1].lower()
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid_media_directory")
    root_fd = os.open(str(root), os.O_RDONLY | _DIRECTORY)
    try:
        for _attempt in range(20):
            storage_name = f".upload-{uuid.uuid4().hex}{extension}"
            try:
                fd = os.open(
                    storage_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as staged:
                    shutil.copyfileobj(stream, staged)
                    staged.flush()
                    os.fsync(staged.fileno())
            except Exception:
                try:
                    os.unlink(storage_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                raise
            return str(root / storage_name)
    finally:
        os.close(root_fd)
    raise FileExistsError("unable_to_allocate_media_staging")


def _copy_file_descriptor(source_fd: int, destination_fd: int) -> None:
    """Copy from a pinned source inode to an exclusively opened destination."""
    os.lseek(source_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise OSError("media_destination_write_failed")
            view = view[written:]
    os.fsync(destination_fd)
    os.lseek(destination_fd, 0, os.SEEK_SET)


def _claim_final_media_path(
    staged_path: str,
    filename: str,
    source_fd: int,
) -> Tuple[str, str, int]:
    """Copy a pinned regular inode to an exclusively claimed final file.

    The returned descriptor still refers to the final inode, so callers can
    validate exactly the bytes that will be retained before persisting a row.
    """
    safe_name = sanitize_media_filename(filename)
    if not safe_name:
        raise ValueError("invalid_filename")
    staged = Path(staged_path)
    root = staged.parent.resolve(strict=True)
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("invalid_staged_file")

    base, extension = os.path.splitext(safe_name)
    root_fd = os.open(str(root), os.O_RDONLY | _DIRECTORY)
    try:
        for counter in range(1000):
            final_name = safe_name if counter == 0 else f"{base}_{counter}{extension}"
            try:
                final_fd = os.open(
                    final_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                continue
            try:
                if not stat.S_ISREG(os.fstat(final_fd).st_mode):
                    raise ValueError("invalid_media_destination")
                _copy_file_descriptor(source_fd, final_fd)
            except Exception:
                os.close(final_fd)
                try:
                    os.unlink(final_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                raise
            return str(root / final_name), final_name, final_fd
    finally:
        os.close(root_fd)
    raise FileExistsError("unable_to_allocate_media_destination")


def _unlink_regular_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        candidate = Path(path)
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.exists() and stat.S_ISREG(os.lstat(candidate).st_mode):
            candidate.unlink()
    except OSError:
        logger.warning("media_cleanup_failed error_type=OSError")


def detect_media_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "image"  # default prudente: se l'estensione non è riconosciuta,
    # meglio trattarlo come immagine (l'analisi fallirà in modo esplicito
    # invece di bloccare l'upload)


def extract_video_frame(video_path: str, output_dir: str, timestamp: str = "00:00:01") -> Optional[str]:
    """Estrae un fotogramma dal video con ffmpeg, da usare come proxy per
    l'analisi AI (Groq vision analizza immagini, non video)."""
    os.makedirs(output_dir, exist_ok=True)
    frame_path = os.path.join(output_dir, f"_frame_{uuid.uuid4().hex[:8]}.jpg")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", timestamp, "-vframes", "1", frame_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not os.path.exists(frame_path):
            logger.error(f"❌ ffmpeg non è riuscito a estrarre un frame da {video_path}: "
                         f"{result.stderr[-300:] if result.stderr else 'nessun dettaglio'}")
            return None
        return frame_path
    except FileNotFoundError:
        logger.error("❌ ffmpeg non è installato sul server. Installa con: sudo apt install ffmpeg")
        return None
    except Exception as e:
        logger.error(f"❌ Errore estrazione frame video: {e}")
        return None


class MediaProcessor:
    """Coordina analisi AI + registrazione nel database per un file appena caricato."""

    def __init__(self, db, ai_generator=None):
        self.db = db
        self.ai = ai_generator

    def process_new_file(
        self,
        filepath: str,
        filename: str,
        mime_type: str,
        file_size: int,
        user_context: str,
    ) -> Dict:
        """
        Analizza un file appena caricato (immagine o video) e lo registra
        nel database come 'non usato'. Il file viene SEMPRE registrato,
        anche se l'analisi AI fallisce: in quel caso resta con categoria
        'other' e senza descrizione, modificabile a mano dalla dashboard.
        """
        staged_path = filepath
        final_path = None
        persisted = False
        frame_to_cleanup = None
        try:
            valid, reason = validate_media_upload(filename, mime_type, file_size)
            if not valid:
                raise ValueError(reason)
            flags = os.O_RDONLY | _NOFOLLOW
            fd = os.open(staged_path, flags)
            with os.fdopen(fd, "rb") as media_file:
                source_stat = os.fstat(media_file.fileno())
                if not stat.S_ISREG(source_stat.st_mode):
                    raise ValueError("invalid_staged_file")
                actual_size = source_stat.st_size
                if actual_size != file_size:
                    raise ValueError("file_size_mismatch")
                if not media_content_matches(media_file, mime_type):
                    raise ValueError("mime_content_mismatch")
                final_path, final_name, final_fd = _claim_final_media_path(
                    staged_path, filename, media_file.fileno(),
                )
                with os.fdopen(final_fd, "rb") as final_file:
                    final_stat = os.fstat(final_file.fileno())
                    if (
                        not stat.S_ISREG(final_stat.st_mode)
                        or final_stat.st_size != file_size
                    ):
                        raise ValueError("file_size_mismatch")
                    if not media_content_matches(final_file, mime_type):
                        raise ValueError("mime_content_mismatch")

            media_type = detect_media_type(final_name)
            analysis_path = final_path
            if media_type == "video":
                frame = extract_video_frame(final_path, os.path.dirname(final_path))
                if frame:
                    analysis_path = frame
                    frame_to_cleanup = frame

            result = self.ai.analyze_image(analysis_path) if self.ai else None
            if result is not None and not isinstance(result, dict):
                result = None
            tags = result.get("tags", []) if result else []
            clean_context = " ".join(str(user_context or "").split())[:2000]
            record = self.db.add_media_with_context(
                filename=final_name,
                filepath=final_path,
                media_type=media_type,
                category=(result or {}).get("category", "other"),
                ai_description=(result or {}).get("description", ""),
                ai_tags=",".join(tags) if isinstance(tags, list) else str(tags),
                user_context=clean_context,
                mime_type=mime_type,
                file_size=file_size,
            )
            persisted = True
            if not result:
                logger.warning(
                    "media_analysis_unavailable stored_with_category=other"
                )
            return record
        finally:
            _unlink_regular_file(frame_to_cleanup)
            _unlink_regular_file(staged_path)
            if not persisted:
                _unlink_regular_file(final_path)
