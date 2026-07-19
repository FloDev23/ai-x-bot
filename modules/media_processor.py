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
import os
import subprocess
import uuid
from typing import Optional, Dict

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}


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

    def process_new_file(self, filepath: str, filename: str) -> Dict:
        """
        Analizza un file appena caricato (immagine o video) e lo registra
        nel database come 'non usato'. Il file viene SEMPRE registrato,
        anche se l'analisi AI fallisce: in quel caso resta con categoria
        'other' e senza descrizione, modificabile a mano dalla dashboard.
        """
        media_type = detect_media_type(filename)
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
        media_id = self.db.add_media(
            filename=filename,
            filepath=filepath,
            media_type=media_type,
            category=(result or {}).get("category", "other"),
            ai_description=(result or {}).get("description", ""),
            ai_tags=",".join(tags) if isinstance(tags, list) else str(tags),
        )
        record = self.db.get_media_by_id(media_id)
        if not result:
            logger.warning(f"⚠️ Analisi AI non disponibile per {filename}: registrato con categoria 'other'")
        return record
