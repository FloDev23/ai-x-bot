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
import subprocess
import unicodedata
import uuid
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
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video"
    return None


def media_content_matches(stream: BinaryIO, mime_type: str) -> bool:
    """Check a small magic-byte signature without consuming the upload stream."""
    position = stream.tell()
    try:
        header = stream.read(32)
    finally:
        stream.seek(position)
    detected = _sniff_media_kind(header)
    expected = (mime_type or "").split(";", 1)[0].strip().lower()
    if expected.startswith("video/"):
        return detected == "video"
    return detected == expected


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
        valid, reason = validate_media_upload(filename, mime_type, file_size)
        if not valid:
            raise ValueError(reason)
        if os.path.getsize(filepath) != file_size:
            raise ValueError("file_size_mismatch")
        with open(filepath, "rb") as media_file:
            if not media_content_matches(media_file, mime_type):
                raise ValueError("mime_content_mismatch")

        safe_name = sanitize_media_filename(filename)
        media_type = detect_media_type(safe_name)
        analysis_path = filepath
        frame_to_cleanup = None

        if media_type == "video":
            frame = extract_video_frame(filepath, os.path.dirname(filepath))
            if frame:
                analysis_path = frame
                frame_to_cleanup = frame

        result = None
        if self.ai:
            result = self.ai.analyze_image(analysis_path)

        if frame_to_cleanup and os.path.exists(frame_to_cleanup):
            os.remove(frame_to_cleanup)

        tags = result.get("tags", []) if result else []
        clean_context = " ".join(str(user_context or "").split())[:2000]
        media_id = self.db.add_media_with_context(
            filename=safe_name,
            filepath=filepath,
            media_type=media_type,
            category=(result or {}).get("category", "other"),
            ai_description=(result or {}).get("description", ""),
            ai_tags=",".join(tags) if isinstance(tags, list) else str(tags),
            user_context=clean_context,
            mime_type=mime_type,
            file_size=file_size,
        )
        record = self.db.get_media_by_id(media_id)
        if not result:
            logger.warning(f"⚠️ Analisi AI non disponibile per {safe_name}: registrato con categoria 'other'")
        return record
