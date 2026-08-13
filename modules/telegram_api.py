"""Small, bounded Requests transport for the Telegram Bot API."""

import hashlib
import logging
import os
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, Optional, Sequence
from urllib.parse import quote

import requests

from config import (
    MEDIA_LIBRARY_DIR,
    TELEGRAM_MAX_IMAGE_BYTES,
    TELEGRAM_MAX_VIDEO_BYTES,
)


REQUEST_TIMEOUT = 10
TELEGRAM_POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25"))
TELEGRAM_MESSAGE_MAX_CHARS = 4096
TELEGRAM_CAPTION_MAX_CHARS = 1024
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64
TELEGRAM_CALLBACK_ANSWER_MAX_CHARS = 200
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_MAX_CONTENT_LENGTH_DIGITS = 19
_MAX_CONTENT_LENGTH_VALUE = (1 << 63) - 1
_MAX_TELEGRAM_ID_CHARS = 4096
_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
}
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/quicktime", "video/x-m4v"})
_ALLOWED_OPERATIONS = frozenset({
    "api",
    "cycle",
    "daily_content_cycle",
    "download",
    "error_persistence",
    "http",
    "json",
    "media_metadata",
    "notification_delivery",
    "opportunity_cycle",
    "performance_cycle",
    "telegram_error",
    "telegram_update",
    "telegram_update_state",
    "transport",
})
_ALLOWED_METHODS = frozenset({
    "answerCallbackQuery",
    "downloadFile",
    "getFile",
    "getUpdates",
    "sendDocument",
    "sendMessage",
    "sendPhoto",
    "sendVideo",
})
_ALLOWED_CODES = frozenset({
    "file_too_large",
    "invalid_content_length",
    "invalid_error_code",
    "invalid_media_metadata",
    "invalid_object",
    "invalid_result",
    "invalid_status",
    "unsupported_media_type",
})
_ALLOWED_EXCEPTION_CLASSES = frozenset({
    "BaseException",
    "ConnectionError",
    "Exception",
    "FileNotFoundError",
    "JSONDecodeError",
    "OSError",
    "OverflowError",
    "PermissionError",
    "RuntimeError",
    "TelegramApiError",
    "TimeoutError",
    "TypeError",
    "ValueError",
})


def _allowlisted(value: Any, allowed: frozenset[str]) -> Optional[str]:
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _allowed_integer(value: Any) -> Optional[int]:
    if type(value) is int and -(1 << 63) <= value <= (1 << 63) - 1:
        return value
    return None


def _allowed_exception_label(value: Any) -> Optional[str]:
    return _allowlisted(value, _ALLOWED_EXCEPTION_CLASSES)


def _exception_class_name(error: BaseException) -> str:
    for exception_type in type(error).__mro__:
        label = _allowed_exception_label(exception_type.__name__)
        if label is not None:
            return label
    return "Exception"


def safe_exception_class(error: BaseException) -> str:
    """Return a credential-safe exception class from the error's MRO."""
    return _exception_class_name(error)


def _format_safe_error(
    *,
    operation: Any = None,
    method: Any = None,
    status: Any = None,
    exception_type: Any = None,
    code: Any = None,
) -> str:
    fields = []
    safe_operation = _allowlisted(operation, _ALLOWED_OPERATIONS)
    safe_method = _allowlisted(method, _ALLOWED_METHODS)
    safe_status = _allowed_integer(status)
    safe_exception = _allowed_exception_label(exception_type)
    safe_code = _allowed_integer(code)
    if safe_operation is not None:
        fields.append(f"operation={safe_operation}")
    if safe_method is not None:
        fields.append(f"method={safe_method}")
    if safe_status is not None:
        fields.append(f"status={safe_status}")
    if safe_exception is not None:
        fields.append(f"exception={safe_exception}")
    if safe_code is not None:
        fields.append(f"code={safe_code}")
    else:
        safe_code_label = _allowlisted(code, _ALLOWED_CODES)
        if safe_code_label is not None:
            fields.append(f"code={safe_code_label}")
    return " ".join(fields)


class TelegramApiError(RuntimeError):
    """A Telegram transport/protocol error safe to persist or report."""

    def __init__(
        self,
        _unsafe_legacy_message: Any = None,
        *,
        operation: Any = None,
        method: Any = None,
        status: Any = None,
        exception_type: Any = None,
        code: Any = None,
    ):
        del _unsafe_legacy_message
        self.operation = _allowlisted(operation, _ALLOWED_OPERATIONS)
        self.method = _allowlisted(method, _ALLOWED_METHODS)
        self.status = _allowed_integer(status)
        self.exception_type = _allowed_exception_label(exception_type)
        self.code = _allowed_integer(code)
        if self.code is None:
            self.code = _allowlisted(code, _ALLOWED_CODES)
        if not any(
            value is not None
            for value in (
                self.operation,
                self.method,
                self.status,
                self.exception_type,
                self.code,
            )
        ):
            self.exception_type = type(self).__name__
        super().__init__(
            _format_safe_error(
                operation=self.operation,
                method=self.method,
                status=self.status,
                exception_type=self.exception_type,
                code=self.code,
            )
        )


def sanitize_error(
    error: Any,
    secrets: Sequence[str] = (),
    *,
    operation: Any = None,
    method: Any = None,
    status: Any = None,
    code: Any = None,
) -> str:
    """Describe an error using only non-secret, allowlisted metadata."""
    del secrets
    if isinstance(error, TelegramApiError):
        operation = operation if operation is not None else error.operation
        method = method if method is not None else error.method
        status = status if status is not None else error.status
        code = code if code is not None else error.code
        exception_type = error.exception_type or type(error).__name__
    elif error is None:
        exception_type = None
    else:
        exception_type = _exception_class_name(error)
    return _format_safe_error(
        operation=operation,
        method=method,
        status=status,
        exception_type=exception_type,
        code=code,
    )


def _media_metadata_error(code: str) -> TelegramApiError:
    return TelegramApiError(operation="media_metadata", code=code)


def _positive_integer(value: Any) -> bool:
    return type(value) is int and 0 < value <= _MAX_CONTENT_LENGTH_VALUE


def _valid_telegram_file_id(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_TELEGRAM_ID_CHARS
        or any(ord(character) < 32 for character in value)
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _telegram_file_identity(payload: Dict[str, Any]) -> tuple[str, str]:
    file_id = payload.get("file_id")
    if not _valid_telegram_file_id(file_id):
        raise _media_metadata_error("invalid_media_metadata")
    file_unique_id = payload.get("file_unique_id")
    if file_unique_id is None:
        identity = file_id
    elif _valid_telegram_file_id(file_unique_id):
        identity = file_unique_id
    else:
        raise _media_metadata_error("invalid_media_metadata")
    return file_id, identity


def _canonical_media_filename(subtype: str, identity: str, suffix: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"telegram-{subtype}-{digest}{suffix}"


def _normalized_media_mime(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _metadata_size_limit(mime_type: str) -> int:
    return (
        TELEGRAM_MAX_IMAGE_BYTES
        if mime_type in _IMAGE_MIME_TYPES
        else TELEGRAM_MAX_VIDEO_BYTES
    )


def _photo_metadata(photo_sizes: Any) -> Dict[str, Any]:
    if not isinstance(photo_sizes, list) or not photo_sizes:
        raise _media_metadata_error("invalid_media_metadata")
    candidates = []
    for photo in photo_sizes:
        if not isinstance(photo, dict):
            raise _media_metadata_error("invalid_media_metadata")
        file_id, identity = _telegram_file_identity(photo)
        width = photo.get("width")
        height = photo.get("height")
        file_size = photo.get("file_size")
        if not all(
            _positive_integer(value) for value in (width, height, file_size)
        ):
            raise _media_metadata_error("invalid_media_metadata")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        file_id_digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
        candidates.append((
            (width * height, width, height, file_size, digest, file_id_digest),
            file_id,
            identity,
            file_size,
        ))
    _rank, file_id, identity, file_size = max(candidates, key=lambda item: item[0])
    if file_size > TELEGRAM_MAX_IMAGE_BYTES:
        raise _media_metadata_error("file_too_large")
    return {
        "file_id": file_id,
        "message_filename": _canonical_media_filename("photo", identity, ".jpg"),
        "mime_type": "image/jpeg",
        "expected_size": file_size,
    }


def _named_media_metadata(subtype: str, payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise _media_metadata_error("invalid_media_metadata")
    file_id, identity = _telegram_file_identity(payload)
    file_name = payload.get("file_name")
    if (
        not isinstance(file_name, str)
        or not file_name
        or len(file_name) > 255
        or "\x00" in file_name
        or "/" in file_name
        or "\\" in file_name
        or file_name in {".", ".."}
    ):
        raise _media_metadata_error("invalid_media_metadata")
    suffix = Path(file_name).suffix.lower()
    expected_mime = _MIME_BY_SUFFIX.get(suffix)
    if expected_mime is None or (
        subtype == "video" and expected_mime not in _VIDEO_MIME_TYPES
    ):
        raise _media_metadata_error("unsupported_media_type")
    mime_type = _normalized_media_mime(payload.get("mime_type"))
    file_size = payload.get("file_size")
    if mime_type != expected_mime or not _positive_integer(file_size):
        raise _media_metadata_error("invalid_media_metadata")
    if file_size > _metadata_size_limit(mime_type):
        raise _media_metadata_error("file_too_large")
    return {
        "file_id": file_id,
        "message_filename": _canonical_media_filename(subtype, identity, suffix),
        "mime_type": mime_type,
        "expected_size": file_size,
    }


def telegram_media_metadata(message: Any) -> Dict[str, Any]:
    """Validate Telegram message media into the trusted Task 9 download contract.

    Exactly one of ``photo``, ``video`` or ``document`` is accepted. No
    transport or filesystem operation occurs here. Telegram ``getFile`` does
    not supply MIME or original message metadata, so callers must canonicalize
    the message with this helper before requesting or downloading the file.
    """
    if not isinstance(message, dict):
        raise _media_metadata_error("invalid_media_metadata")
    subtypes = [
        subtype for subtype in ("photo", "video", "document")
        if subtype in message
    ]
    if not subtypes:
        raise _media_metadata_error("unsupported_media_type")
    if len(subtypes) != 1:
        raise _media_metadata_error("invalid_media_metadata")
    subtype = subtypes[0]
    if subtype == "photo":
        return _photo_metadata(message[subtype])
    return _named_media_metadata(subtype, message[subtype])


def _validate_reply_markup(reply_markup: Optional[Dict[str, Any]]) -> None:
    if reply_markup is None:
        return
    if not isinstance(reply_markup, dict):
        raise ValueError("Invalid Telegram reply markup")
    keyboard = reply_markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        raise ValueError("Invalid Telegram reply markup")
    for row in keyboard:
        if not isinstance(row, list) or not row:
            raise ValueError("Invalid Telegram reply markup")
        for button in row:
            if not isinstance(button, dict) or not isinstance(button.get("text"), str):
                raise ValueError("Invalid Telegram inline button")
            callback_data = button.get("callback_data")
            if callback_data is None:
                continue
            if not isinstance(callback_data, str):
                raise ValueError("Invalid Telegram callback data")
            try:
                size = len(callback_data.encode("utf-8"))
            except UnicodeError:
                raise ValueError("Invalid Telegram callback data") from None
            if not 1 <= size <= TELEGRAM_CALLBACK_DATA_MAX_BYTES:
                raise ValueError("Invalid Telegram callback data")


class _ConnectionPoolLogFilter(logging.Filter):
    _telegram_connectionpool_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        with _CONNECTIONPOOL_LOG_LOCK:
            secrets = tuple(_TELEGRAM_LOG_SECRETS)
            targets = tuple(_TELEGRAM_LOG_TARGETS)
        fragments = [
            record.msg,
            record.args,
            record.exc_text,
            record.stack_info,
            record.pathname,
            record.filename,
            record.module,
            record.funcName,
        ]
        if record.exc_info is not None:
            fragments.append(record.exc_info[1])
            try:
                fragments.append(
                    logging.Formatter().formatException(record.exc_info)
                )
            except Exception:
                pass
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = ""
        fragments.append(rendered)
        serialized = []
        for fragment in fragments:
            try:
                serialized.append(str(fragment))
            except Exception:
                serialized.append("")
        combined = "\n".join(serialized)
        is_telegram_record = (
            "api.telegram.org/bot" in combined
            or "api.telegram.org/file/bot" in combined
            or any(secret and secret in combined for secret in secrets)
            or any(target and target in combined for target in targets)
        )
        if not is_telegram_record:
            return True
        record.msg = "urllib3 connectionpool event"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.pathname = "<redacted>"
        record.filename = "<redacted>"
        record.module = "<redacted>"
        record.funcName = "<redacted>"
        record.__dict__.pop("message", None)
        return True


_CONNECTIONPOOL_LOG_LOCK = threading.RLock()
_TELEGRAM_LOG_SECRETS = set()
_TELEGRAM_LOG_TARGETS = set()


def _register_connectionpool_secrets(token: str, *targets: str) -> None:
    logger = logging.getLogger("urllib3.connectionpool")
    with _CONNECTIONPOOL_LOG_LOCK:
        if token:
            _TELEGRAM_LOG_SECRETS.add(token)
        _TELEGRAM_LOG_TARGETS.update(target for target in targets if target)
        if not any(
            getattr(item, "_telegram_connectionpool_filter", False)
            for item in logger.filters
        ):
            logger.addFilter(_ConnectionPoolLogFilter())


class TelegramApi:
    """Dependency-injectable Telegram Bot API client with bounded I/O."""

    def __init__(
        self,
        bot_token: str,
        media_library_dir: os.PathLike | str = MEDIA_LIBRARY_DIR,
        requests_client=requests,
    ):
        self.bot_token = str(bot_token)
        self.media_library_dir = Path(os.path.abspath(os.fspath(media_library_dir)))
        self.requests = requests_client
        self._api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._file_base = f"https://api.telegram.org/file/bot{self.bot_token}"
        _register_connectionpool_secrets(
            self.bot_token,
            self._api_base,
            self._file_base,
        )

    def _safe_transport_error(self, method: str, exc: Exception) -> TelegramApiError:
        return TelegramApiError(
            operation="transport",
            method=method,
            exception_type=_exception_class_name(exc),
        )

    def _post(
        self,
        method: str,
        payload: Dict[str, Any],
        *,
        timeout: int,
        files: Optional[Dict[str, Any]] = None,
    ):
        try:
            if files is None:
                response = self.requests.post(
                    f"{self._api_base}/{method}",
                    json=payload,
                    timeout=timeout,
                )
            else:
                response = self.requests.post(
                    f"{self._api_base}/{method}",
                    data=payload,
                    files=files,
                    timeout=timeout,
                )
        except Exception as exc:
            raise self._safe_transport_error(method, exc) from None

        try:
            status = response.status_code
        except Exception as exc:
            raise self._safe_transport_error(method, exc) from None
        if type(status) is not int:
            raise TelegramApiError(
                operation="http",
                method=method,
                code="invalid_status",
            )
        if status != 200:
            raise TelegramApiError(
                operation="http",
                method=method,
                status=status,
            )
        try:
            body = response.json()
        except Exception as exc:
            raise TelegramApiError(
                operation="json",
                method=method,
                exception_type=_exception_class_name(exc),
            ) from None
        if not isinstance(body, dict):
            raise TelegramApiError(
                operation="json",
                method=method,
                code="invalid_object",
            )
        if body.get("ok") is not True:
            error_code = body.get("error_code", "unknown")
            if _allowed_integer(error_code) is None:
                error_code = "invalid_error_code"
            raise TelegramApiError(
                operation="api",
                method=method,
                code=error_code,
            )
        return body.get("result")

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = TELEGRAM_POLL_TIMEOUT,
    ) -> list:
        payload = {
            "offset": offset,
            "timeout": int(timeout),
            "allowed_updates": ["message", "callback_query"],
        }
        result = self._post(
            "getUpdates",
            payload,
            timeout=int(timeout),
        )
        if not isinstance(result, list):
            raise TelegramApiError(
                operation="json",
                method="getUpdates",
                code="invalid_result",
            )
        return result

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: Optional[str] = "HTML",
        disable_web_page_preview: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ):
        rendered_text = str(text)
        if not 1 <= len(rendered_text) <= TELEGRAM_MESSAGE_MAX_CHARS:
            raise ValueError("Telegram message exceeds the supported limit")
        _validate_reply_markup(reply_markup)
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": rendered_text,
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload, timeout=REQUEST_TIMEOUT)

    def send_media(
        self,
        chat_id: str,
        media: BinaryIO | os.PathLike | str,
        media_type: str,
        *,
        caption: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ):
        method_and_field = {
            "photo": ("sendPhoto", "photo"),
            "video": ("sendVideo", "video"),
            "document": ("sendDocument", "document"),
        }
        if media_type not in method_and_field:
            raise ValueError("media_type must be photo, video or document")
        method, field = method_and_field[media_type]
        payload: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption is not None:
            rendered_caption = str(caption)
            if len(rendered_caption) > TELEGRAM_CAPTION_MAX_CHARS:
                raise ValueError("Telegram caption exceeds the supported limit")
            payload["caption"] = rendered_caption
        _validate_reply_markup(reply_markup)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        def send(media_file: BinaryIO):
            return self._post(
                method,
                payload,
                timeout=REQUEST_TIMEOUT,
                files={field: media_file},
            )
        if isinstance(media, (str, os.PathLike)):
            with Path(media).open("rb") as media_file:
                return send(media_file)
        if not callable(getattr(media, "read", None)):
            raise TypeError("media must be a path or binary stream")
        return send(media)

    def get_file(self, file_id: str) -> Dict[str, Any]:
        result = self._post(
            "getFile",
            {"file_id": str(file_id)},
            timeout=REQUEST_TIMEOUT,
        )
        if not isinstance(result, dict):
            raise TelegramApiError(
                operation="json",
                method="getFile",
                code="invalid_result",
            )
        return result

    def answer_callback(
        self,
        callback_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ):
        payload: Dict[str, Any] = {"callback_query_id": str(callback_id)}
        if text is not None:
            rendered_text = str(text)
            if len(rendered_text) > TELEGRAM_CALLBACK_ANSWER_MAX_CHARS:
                raise ValueError("Telegram callback answer exceeds the supported limit")
            payload["text"] = rendered_text
        if show_alert:
            payload["show_alert"] = True
        return self._post(
            "answerCallbackQuery",
            payload,
            timeout=REQUEST_TIMEOUT,
        )

    @staticmethod
    def _validate_remote_file_path(file_path: str) -> str:
        if not isinstance(file_path, str) or not file_path:
            raise ValueError("Telegram file path is required")
        parsed = PurePosixPath(file_path)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or re.fullmatch(r"[A-Za-z0-9_./-]+", file_path) is None
        ):
            raise ValueError("Invalid Telegram file path")
        return quote(file_path, safe="/")

    def _reserve_destination(self, destination: os.PathLike | str):
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if not isinstance(nofollow, int) or nofollow == 0:
            raise RuntimeError("secure_nofollow_unavailable")
        if os.open not in getattr(os, "supports_dir_fd", set()):
            raise RuntimeError("secure_dir_fd_unavailable")
        media_root = self.media_library_dir
        destination_path = Path(destination)
        if not destination_path.is_absolute():
            raise ValueError("Download destination must be an absolute path")
        try:
            relative = destination_path.relative_to(media_root)
        except (TypeError, ValueError):
            raise ValueError("Download destination must be inside MEDIA_LIBRARY_DIR") from None
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Invalid download destination")
        normalized = media_root.joinpath(*relative.parts)

        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        directory_flags |= nofollow
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC

        try:
            parent_fd = os.open(media_root, directory_flags)
        except OSError as exc:
            raise ValueError("MEDIA_LIBRARY_DIR must be an existing directory") from exc
        try:
            for part in relative.parts[:-1]:
                try:
                    next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise ValueError(
                        "Download destination parent must not contain symlinks"
                    ) from exc
                os.close(parent_fd)
                parent_fd = next_fd

            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            file_flags |= nofollow
            if hasattr(os, "O_CLOEXEC"):
                file_flags |= os.O_CLOEXEC
            file_fd = os.open(
                relative.parts[-1],
                file_flags,
                0o600,
                dir_fd=parent_fd,
            )
        except Exception:
            os.close(parent_fd)
            raise
        return normalized, parent_fd, relative.parts[-1], file_fd

    @staticmethod
    def _download_byte_limit(
        destination: Path,
        message_filename: str,
        mime_type: str,
        expected_size: int,
    ) -> int:
        if (
            not isinstance(message_filename, str)
            or not message_filename
            or "\x00" in message_filename
            or "/" in message_filename
            or "\\" in message_filename
        ):
            message_suffix = ""
        else:
            message_suffix = Path(message_filename).suffix.lower()
        destination_suffix = destination.suffix.lower()
        expected_mime = _MIME_BY_SUFFIX.get(message_suffix)
        normalized_mime = (
            mime_type.split(";", 1)[0].strip().lower()
            if isinstance(mime_type, str)
            else ""
        )
        if (
            expected_mime is None
            or destination_suffix != message_suffix
            or normalized_mime != expected_mime
            or type(expected_size) is not int
            or expected_size <= 0
        ):
            raise TelegramApiError(
                operation="download",
                method="downloadFile",
                code="invalid_media_metadata",
            )
        limit = (
            TELEGRAM_MAX_IMAGE_BYTES
            if normalized_mime in _IMAGE_MIME_TYPES
            else TELEGRAM_MAX_VIDEO_BYTES
        )
        if expected_size > limit:
            raise TelegramApiError(
                operation="download",
                method="downloadFile",
                code="file_too_large",
            )
        return limit

    @staticmethod
    def _content_length(response) -> Optional[int]:
        try:
            headers = response.headers
            raw_length = headers.get("Content-Length")
        except Exception as exc:
            raise TelegramApiError(
                operation="download",
                method="downloadFile",
                exception_type=_exception_class_name(exc),
            ) from None
        if raw_length is None:
            return None
        if type(raw_length) is int:
            length = raw_length
        elif (
            isinstance(raw_length, str)
            and 1 <= len(raw_length) <= _MAX_CONTENT_LENGTH_DIGITS
            and raw_length.isascii()
            and raw_length.isdigit()
        ):
            try:
                length = int(raw_length)
            except (ValueError, OverflowError):
                raise TelegramApiError(
                    operation="download",
                    method="downloadFile",
                    code="invalid_content_length",
                ) from None
        else:
            raise TelegramApiError(
                operation="download",
                method="downloadFile",
                code="invalid_content_length",
            )
        if length < 0 or length > _MAX_CONTENT_LENGTH_VALUE:
            raise TelegramApiError(
                operation="download",
                method="downloadFile",
                code="invalid_content_length",
            )
        return length

    def download_file(
        self,
        file_path: str,
        destination: os.PathLike | str,
        *,
        message_filename: str,
        mime_type: str,
        expected_size: int,
    ) -> Path:
        """Download media using MIME and size metadata from the Telegram message.

        ``getFile`` supplies only the remote path. The caller (Task 9) must
        obtain ``message_filename``, ``mime_type`` and ``expected_size`` from
        :func:`telegram_media_metadata`, not invent or copy arbitrary fields.
        The destination basename may differ but must preserve the canonical
        extension.
        """
        safe_remote_path = self._validate_remote_file_path(file_path)
        requested_destination = Path(destination)
        byte_limit = self._download_byte_limit(
            requested_destination,
            message_filename,
            mime_type,
            expected_size,
        )
        destination_path, parent_fd, leaf_name, file_fd = self._reserve_destination(
            destination
        )
        response = None
        failure = None
        try:
            try:
                response = self.requests.get(
                    f"{self._file_base}/{safe_remote_path}",
                    stream=True,
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as exc:
                raise self._safe_transport_error("downloadFile", exc) from None
            try:
                status = response.status_code
            except Exception as exc:
                raise self._safe_transport_error("downloadFile", exc) from None
            if type(status) is not int:
                raise TelegramApiError(
                    operation="http",
                    method="downloadFile",
                    code="invalid_status",
                )
            if status != 200:
                raise TelegramApiError(
                    operation="http",
                    method="downloadFile",
                    status=status,
                )
            declared_length = self._content_length(response)
            if declared_length is not None and declared_length > byte_limit:
                raise TelegramApiError(
                    operation="download",
                    method="downloadFile",
                    code="file_too_large",
                )
            if declared_length is not None and declared_length != expected_size:
                raise TelegramApiError(
                    operation="download",
                    method="downloadFile",
                    code="invalid_media_metadata",
                )
            try:
                with os.fdopen(file_fd, "wb") as destination_file:
                    file_fd = -1
                    bytes_written = 0
                    for chunk in response.iter_content(
                        chunk_size=_DOWNLOAD_CHUNK_SIZE
                    ):
                        if not chunk:
                            continue
                        bytes_written += len(chunk)
                        if bytes_written > byte_limit:
                            raise TelegramApiError(
                                operation="download",
                                method="downloadFile",
                                code="file_too_large",
                            )
                        if bytes_written > expected_size:
                            raise TelegramApiError(
                                operation="download",
                                method="downloadFile",
                                code="invalid_media_metadata",
                            )
                        destination_file.write(chunk)
                    if bytes_written != expected_size:
                        raise TelegramApiError(
                            operation="download",
                            method="downloadFile",
                            code="invalid_media_metadata",
                        )
            except TelegramApiError:
                raise
            except Exception as exc:
                raise self._safe_transport_error("downloadFile", exc) from None
        except BaseException as exc:
            failure = exc
        finally:
            try:
                if file_fd >= 0:
                    try:
                        os.close(file_fd)
                    except BaseException as exc:
                        if failure is None:
                            failure = (
                                self._safe_transport_error("downloadFile", exc)
                                if isinstance(exc, Exception)
                                else exc
                            )
            finally:
                try:
                    if response is not None:
                        try:
                            close = getattr(response, "close", None)
                            if callable(close):
                                close()
                        except BaseException as exc:
                            if failure is None:
                                failure = (
                                    self._safe_transport_error("downloadFile", exc)
                                    if isinstance(exc, Exception)
                                    else exc
                                )
                finally:
                    try:
                        if failure is not None:
                            try:
                                os.unlink(leaf_name, dir_fd=parent_fd)
                            except FileNotFoundError:
                                pass
                            except OSError:
                                pass
                    finally:
                        try:
                            os.close(parent_fd)
                        except BaseException as exc:
                            if failure is None:
                                failure = (
                                    self._safe_transport_error("downloadFile", exc)
                                    if isinstance(exc, Exception)
                                    else exc
                                )
        if failure is not None:
            raise failure
        return destination_path
