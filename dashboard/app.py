"""
Dashboard di sola lettura (+ un'unica azione: chiudere un lead) per
flexdropin-bot. Pensata per essere raggiunta SOLO via SSH tunnel:
- binding su 127.0.0.1, mai su 0.0.0.0
- nessun login: la sicurezza è demandata interamente al tunnel SSH

Avvio locale (test):
    cd dashboard && python3 app.py

Uso reale, dalla tua macchina:
    ssh -L 5050:127.0.0.1:5050 ubuntu@<ip-server>
poi apri http://127.0.0.1:5050 nel browser del tuo computer.

Vedi SETUP.md per l'avvio come servizio systemd separato dal bot.
"""
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, send_from_directory

# Permette di importare modules.database anche eseguendo questo file
# direttamente dalla cartella dashboard/
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from modules.database import Database  # noqa: E402
from modules.media_processor import (  # noqa: E402
    MediaProcessor,
    media_content_matches,
    sanitize_media_filename,
    stage_media_upload,
    validate_media_upload,
)
from config import MEDIA_LIBRARY_DIR  # noqa: E402

app = Flask(__name__)
# IMPORTANTE: Database() usa di default un percorso relativo ('bot_data.db').
# La dashboard gira con WorkingDirectory=.../dashboard, quindi un percorso
# relativo creerebbe/leggerebbe un DB vuoto dentro dashboard/ invece del
# database reale del bot in REPO_ROOT. Lo forziamo esplicitamente qui.
BOT_DB_PATH = str(REPO_ROOT / "bot_data.db")
db = Database(db_path=BOT_DB_PATH)

MEDIA_DIR = MEDIA_LIBRARY_DIR
os.makedirs(MEDIA_DIR, mode=0o700, exist_ok=True)

# L'analisi AI richiede GROQ_API_KEY: se non configurata o Groq non
# raggiungibile dalla dashboard, i file vengono comunque salvati e
# registrati con categoria 'other' (vedi MediaProcessor.process_new_file).
try:
    from modules.ai_generator import AIGenerator
    _ai_generator = AIGenerator()
except Exception as e:  # pragma: no cover
    _ai_generator = None
    print(f"⚠️ AIGenerator non disponibile per l'analisi media: {e}")

media_processor = MediaProcessor(db, _ai_generator)

SERVICE_NAME = os.getenv("BOT_SERVICE_NAME", "flexdropin-bot")


def get_recent_logs(lines: int = 200):
    """Legge le ultime N righe di journalctl per il servizio del bot.
    Richiede che l'utente che esegue la dashboard possa leggere il journal
    (gruppo systemd-journal) - vedi SETUP.md."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return [f"⚠️ Impossibile leggere i log di {SERVICE_NAME}: {result.stderr.strip()}"]
        return [l for l in result.stdout.strip().split("\n") if l]
    except FileNotFoundError:
        return ["⚠️ journalctl non disponibile in questo ambiente."]
    except Exception as e:
        return [f"⚠️ Errore lettura log: {e}"]


def extract_error_lines(logs):
    markers = ("❌", "🚨", " ERROR ", "Errore")
    return [l for l in logs if any(m in l for m in markers)]


@app.route("/")
def overview():
    leads = db.get_all_leads(limit=8)
    targets = db.get_top_targets(limit=6)
    posts = db.get_recent_posts(limit=5)
    logs = get_recent_logs(300)
    errors = extract_error_lines(logs)[-8:]
    stats = {
        "leads_nuovi": sum(1 for l in db.get_all_leads(limit=1000) if l["status"] == "nuovo"),
        "targets_attivi": len(db.get_top_targets(limit=1000)),
        "post_totali": len(db.get_recent_posts(limit=1000)),
        "errori_recenti": len(errors),
    }
    return render_template(
        "overview.html", leads=leads, targets=targets, posts=posts,
        errors=errors, stats=stats, service=SERVICE_NAME,
    )


@app.route("/leads")
def leads_view():
    status_filter = request.args.get("status", "tutti")
    all_leads = db.get_all_leads(limit=300)
    if status_filter != "tutti":
        all_leads = [l for l in all_leads if l["status"] == status_filter]
    return render_template("leads.html", leads=all_leads, status_filter=status_filter, service=SERVICE_NAME)


@app.route("/leads/<int:lead_id>/status", methods=["POST"])
def update_lead_status(lead_id):
    new_status = request.form.get("status", "gestito")
    db.update_lead_status(lead_id, new_status)
    return redirect(request.referrer or url_for("leads_view"))


@app.route("/engagement")
def engagement_view():
    targets = db.get_top_targets(limit=100)
    return render_template("engagement.html", targets=targets, service=SERVICE_NAME)


@app.route("/posts")
def posts_view():
    posts = db.get_recent_posts(limit=60)
    return render_template("posts.html", posts=posts, service=SERVICE_NAME)


@app.route("/logs")
def logs_view():
    logs = get_recent_logs(500)
    only_errors = request.args.get("errors") == "1"
    if only_errors:
        logs = extract_error_lines(logs)
    return render_template("logs.html", logs=logs, only_errors=only_errors, service=SERVICE_NAME)


@app.route("/media")
def media_view():
    status_filter = request.args.get("status", "tutti")
    items = db.get_all_media(limit=300)
    if status_filter != "tutti":
        items = [m for m in items if m["lifecycle_state"] == status_filter]
    return render_template("media.html", items=items, status_filter=status_filter, service=SERVICE_NAME)


@app.route("/media/upload", methods=["POST"])
def media_upload():
    file = request.files.get("file")
    if not file or file.filename == "":
        return redirect(url_for("media_view"))

    file.stream.seek(0, os.SEEK_END)
    file_size = file.stream.tell()
    file.stream.seek(0)
    mime_type = file.mimetype
    valid, reason = validate_media_upload(file.filename, mime_type, file_size)
    if not valid or not media_content_matches(file.stream, mime_type):
        if valid:
            reason = "mime_content_mismatch"
        return f"Invalid media upload: {reason}", 400

    safe_name = sanitize_media_filename(file.filename)
    staged_path = None
    try:
        staged_path = stage_media_upload(file.stream, MEDIA_DIR, safe_name)
        media_processor.process_new_file(
            staged_path,
            safe_name,
            mime_type,
            file_size,
            request.form.get("user_context", ""),
        )
    except Exception as error:
        if staged_path:
            try:
                Path(staged_path).unlink(missing_ok=True)
            except OSError:
                pass
        app.logger.error(
            "media_upload_failed error_type=%s", type(error).__name__,
        )
        return "Unable to store media upload", 500

    return redirect(url_for("media_view"))


@app.route("/media/<int:media_id>/update", methods=["POST"])
def media_update(media_id):
    category = request.form.get("category")
    description = request.form.get("ai_description")
    db.update_media(media_id, category=category, ai_description=description)
    return redirect(url_for("media_view"))


@app.route("/media/<int:media_id>/delete", methods=["POST"])
def media_delete(media_id):
    item = db.get_media_by_id(media_id)
    if not item:
        return redirect(url_for("media_view"))
    media_root = Path(MEDIA_DIR).resolve()
    item_path = Path(item["filepath"]).resolve()
    if media_root not in item_path.parents:
        return "Refusing to delete a path outside the media library", 400
    def delete_file():
        if item_path.exists():
            item_path.unlink()

    try:
        deleted = db.mark_media_file_deleted(media_id, delete_file=delete_file)
    except OSError:
        return "Unable to delete media file", 500
    if not deleted:
        return "Media lifecycle state conflicts with permanent deletion", 409
    return redirect(url_for("media_view"))


@app.route("/media/<int:media_id>/archive", methods=["POST"])
def media_archive(media_id):
    db.archive_media(media_id)
    return redirect(url_for("media_view"))


@app.route("/media/<int:media_id>/reusable", methods=["POST"])
def media_reusable(media_id):
    db.set_media_reusable(media_id, request.form.get("reusable") == "1")
    return redirect(url_for("media_view"))


@app.route("/media/file/<path:filename>")
def media_file(filename):
    return send_from_directory(MEDIA_DIR, filename)


if __name__ == "__main__":
    # Bind SOLO su localhost: raggiungibile esclusivamente via SSH tunnel.
    app.run(host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "5050")), debug=False)
