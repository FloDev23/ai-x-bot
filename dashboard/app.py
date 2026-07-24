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
from werkzeug.utils import secure_filename

# Permette di importare modules.database anche eseguendo questo file
# direttamente dalla cartella dashboard/
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from modules.database import Database  # noqa: E402
from modules.media_processor import MediaProcessor  # noqa: E402
from config import MEDIA_LIBRARY_DIR  # noqa: E402

app = Flask(__name__)
# IMPORTANTE: Database() usa di default un percorso relativo ('bot_data.db').
# La dashboard gira con WorkingDirectory=.../dashboard, quindi un percorso
# relativo creerebbe/leggerebbe un DB vuoto dentro dashboard/ invece del
# database reale del bot in REPO_ROOT. Lo forziamo esplicitamente qui.
BOT_DB_PATH = str(REPO_ROOT / "bot_data.db")
db = Database(db_path=BOT_DB_PATH)

MEDIA_DIR = MEDIA_LIBRARY_DIR
os.makedirs(MEDIA_DIR, exist_ok=True)
ALLOWED_MEDIA_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "mp4", "mov", "m4v"}

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
    leads = [l for l in db.get_all_leads(limit=50) if l["action_suggested"] != "Ignora"][:8]
    targets = db.get_top_targets(limit=6)
    posts = db.get_recent_posts(limit=5)
    logs = get_recent_logs(300)
    errors = extract_error_lines(logs)[-8:]
    stats = {
        "leads_nuovi": sum(
            1 for l in db.get_all_leads(limit=1000)
            if l["status"] == "nuovo" and l["action_suggested"] != "Ignora"
        ),
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
    # Di default nascondiamo i lead con azione suggerita "Ignora": non richiedono
    # nessuna azione e affollerebbero la vista rischiando di far perdere quelli
    # realmente interessanti. Con ?action=tutti si possono comunque rivedere.
    action_filter = request.args.get("action", "azionabili")
    all_leads = db.get_all_leads(limit=300)
    if status_filter != "tutti":
        all_leads = [l for l in all_leads if l["status"] == status_filter]
    if action_filter == "azionabili":
        all_leads = [l for l in all_leads if l["action_suggested"] != "Ignora"]
    return render_template(
        "leads.html", leads=all_leads, status_filter=status_filter,
        action_filter=action_filter, service=SERVICE_NAME,
    )


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
    if status_filter == "non_usati":
        items = [m for m in items if not m["used"]]
    elif status_filter == "usati":
        items = [m for m in items if m["used"]]
    return render_template("media.html", items=items, status_filter=status_filter, service=SERVICE_NAME)


@app.route("/media/upload", methods=["POST"])
def media_upload():
    file = request.files.get("file")
    if not file or file.filename == "":
        return redirect(url_for("media_view"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        return redirect(url_for("media_view"))

    safe_name = secure_filename(file.filename)
    # Evita di sovrascrivere un file già esistente con lo stesso nome
    base, extension = os.path.splitext(safe_name)
    final_name = safe_name
    counter = 1
    while os.path.exists(os.path.join(MEDIA_DIR, final_name)):
        final_name = f"{base}_{counter}{extension}"
        counter += 1

    filepath = os.path.join(MEDIA_DIR, final_name)
    file.save(filepath)

    # Analisi AI + registrazione nel database (non blocca l'upload se fallisce)
    media_processor.process_new_file(filepath, final_name)

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
    if item and os.path.exists(item["filepath"]):
        try:
            os.remove(item["filepath"])
        except OSError:
            pass
    db.delete_media(media_id)
    return redirect(url_for("media_view"))


@app.route("/media/file/<path:filename>")
def media_file(filename):
    return send_from_directory(MEDIA_DIR, filename)


if __name__ == "__main__":
    # Bind SOLO su localhost: raggiungibile esclusivamente via SSH tunnel.
    app.run(host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "5050")), debug=False)
