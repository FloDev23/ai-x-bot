"""Small, bounded Requests transport for the Telegram Bot API."""

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Sequence
from urllib.parse import quote

import requests

from config import MEDIA_LIBRARY_DIR


REQUEST_TIMEOUT = 10
TELEGRAM_POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25"))
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_MAX_SAFE_ERROR_CHARS = 1000


class TelegramApiError(RuntimeError):
    """A Telegram transport/protocol error safe to persist or report."""


def sanitize_error(error: Any, secrets: Sequence[str] = ()) -> str:
    """Return bounded error context without updates, credentials or queries."""
    text = str(error)
    if re.search(r"(?i)[\"']?update_id[\"']?\s*[:=]", text):
        return "[redacted raw Telegram payload]"

    text = re.sub(
        r"(?i)([\"']?authorization[\"']?\s*[:=]\s*[\"']?Bearer\s+)"
        r"[^,\s}\]\"']+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?authorization[\"']?\s*[:=]\s*[\"']?Basic\s+)"
        r"[^,\s}\]\"']+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?authorization[\"']?\s*[:=]\s*[\"']?)"
        r"(?!(?:Bearer|Basic)\b)"
        r"[^,\s}\]\"']+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+(?!\[redacted\])[^,\s}\]\"']+",
        "Bearer [redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(https?://[^\s?'\"}]+)\?[^\s'\"}]+",
        r"\1?[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?(?:token|api[_-]?key|password|secret|credential)"
        r"[\"']?\s*[:=]\s*)[\"']?[^,\s}\]\"']+[\"']?",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?is)(\b(?:request[\s_-]*)?body\s*[:=]\s*).*$",
        r"\1[redacted]",
        text,
    )
    configured_secrets = list(secrets)
    configured_secrets.extend(
        value
        for key, value in os.environ.items()
        if value
        and re.search(
            r"(?i)(?:token|api[_-]?key|password|secret|credential|authorization)",
            key,
        )
    )
    for secret in configured_secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = re.sub(
        r"\b\d{6,}:[A-Za-z0-9_-]{8,}\b",
        "[redacted]",
        text,
    )
    return text[:_MAX_SAFE_ERROR_CHARS]


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

    def _safe_transport_error(self, method: str, exc: Exception) -> TelegramApiError:
        return TelegramApiError(
            f"Telegram {method} transport error: "
            f"{sanitize_error(exc, secrets=[self.bot_token])}"
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

        if response.status_code != 200:
            raise TelegramApiError(
                f"Telegram {method} HTTP error: status {response.status_code}"
            )
        try:
            body = response.json()
        except Exception as exc:
            safe = sanitize_error(exc, secrets=[self.bot_token])
            raise TelegramApiError(
                f"Telegram {method} JSON error: {safe}"
            ) from None
        if not isinstance(body, dict):
            raise TelegramApiError(f"Telegram {method} JSON error: invalid object")
        if body.get("ok") is not True:
            error_code = body.get("error_code", "unknown")
            description = sanitize_error(
                body.get("description", "request rejected"),
                secrets=[self.bot_token],
            )
            raise TelegramApiError(
                f"Telegram {method} API error {error_code}: {description}"
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
            raise TelegramApiError("Telegram getUpdates JSON error: result is not a list")
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
            raise TelegramApiError("Telegram getFile JSON error: result is not an object")
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
        try:
            media_root = self.media_library_dir.resolve(strict=True)
        except OSError as exc:
            raise ValueError("MEDIA_LIBRARY_DIR must be an existing directory") from exc
        if not media_root.is_dir():
            raise ValueError("MEDIA_LIBRARY_DIR must be an existing directory")
        destination_path = Path(destination)
        if not destination_path.is_absolute():
            raise ValueError("Download destination must be an absolute path")
        try:
            normalized = destination_path.parent.resolve(strict=True) / destination_path.name
        except OSError as exc:
            raise ValueError("Download destination parent must exist") from exc
        try:
            relative = normalized.relative_to(media_root)
        except ValueError:
            raise ValueError("Download destination must be inside MEDIA_LIBRARY_DIR") from None
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Invalid download destination")

        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        directory_flags |= nofollow
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC

        parent_fd = os.open(media_root, directory_flags)
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
        try:
            try:
                response = self.requests.get(
                    f"{self._file_base}/{safe_remote_path}",
                    stream=True,
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as exc:
                raise self._safe_transport_error("downloadFile", exc) from None
            if response.status_code != 200:
                raise TelegramApiError(
                    "Telegram downloadFile HTTP error: "
                    f"status {response.status_code}"
                )
            with os.fdopen(file_fd, "wb") as destination_file:
                file_fd = -1
                try:
                    for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                        if chunk:
                            destination_file.write(chunk)
                except TelegramApiError:
                    raise
                except Exception as exc:
                    raise self._safe_transport_error("downloadFile", exc) from None
            return destination_path
        except Exception:
            if file_fd >= 0:
                os.close(file_fd)
            try:
                os.unlink(leaf_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            os.close(parent_fd)
