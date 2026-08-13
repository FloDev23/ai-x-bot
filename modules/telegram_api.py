"""Small, bounded Requests transport for the Telegram Bot API."""

import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Sequence
from urllib.parse import quote

import requests

from config import (
    MEDIA_LIBRARY_DIR,
    TELEGRAM_MAX_IMAGE_BYTES,
    TELEGRAM_MAX_VIDEO_BYTES,
)


REQUEST_TIMEOUT = 10
TELEGRAM_POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25"))
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v"})
_ALLOWED_OPERATIONS = frozenset({
    "api",
    "cycle",
    "daily_content_cycle",
    "download",
    "error_persistence",
    "http",
    "json",
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
    "invalid_object",
    "invalid_result",
    "invalid_status",
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


class _ConnectionPoolLogFilter(logging.Filter):
    _telegram_connectionpool_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "urllib3 connectionpool event"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _install_connectionpool_log_filter() -> None:
    logger = logging.getLogger("urllib3.connectionpool")
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
        _install_connectionpool_log_filter()
        self.bot_token = str(bot_token)
        self.media_library_dir = Path(os.path.abspath(os.fspath(media_library_dir)))
        self.requests = requests_client
        self._api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._file_base = f"https://api.telegram.org/file/bot{self.bot_token}"

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
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": str(text),
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
        media_path: os.PathLike | str,
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
            payload["caption"] = str(caption)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        with Path(media_path).open("rb") as media_file:
            return self._post(
                method,
                payload,
                timeout=REQUEST_TIMEOUT,
                files={field: media_file},
            )

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
            payload["text"] = str(text)
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
    def _download_byte_limit(destination: Path) -> int:
        suffix = destination.suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            return TELEGRAM_MAX_IMAGE_BYTES
        if suffix in _VIDEO_SUFFIXES:
            return TELEGRAM_MAX_VIDEO_BYTES
        return max(TELEGRAM_MAX_IMAGE_BYTES, TELEGRAM_MAX_VIDEO_BYTES)

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
        elif isinstance(raw_length, str) and raw_length.isascii() and raw_length.isdigit():
            length = int(raw_length)
        else:
            raise TelegramApiError(
                operation="download",
                method="downloadFile",
                code="invalid_content_length",
            )
        if length < 0:
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
    ) -> Path:
        safe_remote_path = self._validate_remote_file_path(file_path)
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
            byte_limit = self._download_byte_limit(destination_path)
            declared_length = self._content_length(response)
            if declared_length is not None and declared_length > byte_limit:
                raise TelegramApiError(
                    operation="download",
                    method="downloadFile",
                    code="file_too_large",
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
                        destination_file.write(chunk)
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
