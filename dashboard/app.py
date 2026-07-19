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

from flask import Flask, render_template, request, redirect, url_for

# Permette di importare modules.database anche eseguendo questo file
# direttamente dalla cartella dashboard/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules.database import Database  # noqa: E402

app = Flask(__name__)
db = Database()

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


if __name__ == "__main__":
    # Bind SOLO su localhost: raggiungibile esclusivamente via SSH tunnel.
    app.run(host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "5050")), debug=False)
