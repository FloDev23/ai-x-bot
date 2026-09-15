#!/usr/bin/env bash
#
# deploy.sh — aggiorna ed avvia bot + dashboard in un solo comando.
#
# Cosa fa, in ordine:
#   1. Verifica che non ci siano modifiche ai file tracciati (i file non
#      tracciati come backups/ vengono ignorati)
#   2. Backup del database (SQLite backup API, sicuro a bot acceso) e di .env
#   3. git pull --ff-only del branch corrente
#   4. Aggiorna le dipendenze del bot (requirements.txt, nel venv se presente)
#   5. Aggiorna le dipendenze della dashboard (dashboard/requirements.txt)
#   6. Preflight fail-closed: config valida, APPROVAL_REQUIRED=true, DB integro.
#      Funziona sia in dry-run sia in modalità live (DRY_RUN=false)
#   7. Installa/aggiorna il servizio systemd della dashboard (se cambiato)
#   8. Verifica/aggiunge i permessi per leggere i log (gruppo systemd-journal)
#   9. Riavvia bot e dashboard, controlla che siano attivi e senza Traceback
#
# Uso:
#   cd ~/ai-x-bot
#   ./deploy.sh
#
# Va lanciato come utente normale (es. ubuntu), NON con sudo davanti:
# lo script chiede sudo internamente solo per i comandi che ne hanno bisogno
# (systemctl, copia del file .service). Serve un utente con permessi sudo
# passwordless per systemctl, altrimenti verrà chiesta la password ad ogni
# comando sudo.

set -euo pipefail

# ---- Config — modifica solo se il tuo setup ha nomi/percorsi diversi ----
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_SERVICE="flexdropin-bot"
DASHBOARD_SERVICE="flexdropin-dashboard"
VENV_DIR="$REPO_DIR/venv"
DASHBOARD_SERVICE_FILE="$REPO_DIR/dashboard/flexdropin-dashboard.service"
SYSTEMD_DIR="/etc/systemd/system"
BACKUP_DIR="$REPO_DIR/backups"
BACKUPS_TO_KEEP=10

step() { echo -e "\n\033[1;36m▶ $1\033[0m"; }
ok()   { echo -e "\033[1;32m✅ $1\033[0m"; }
warn() { echo -e "\033[1;33m⚠️  $1\033[0m"; }
fail() { echo -e "\033[1;31m❌ $1\033[0m"; exit 1; }

cd "$REPO_DIR"

# ---- 1. Modifiche locali non committate ----
step "1/9 · Controllo modifiche ai file tracciati"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short --untracked-files=no
  fail "Ci sono modifiche locali non committate in $REPO_DIR. Fai commit o 'git stash' prima di eseguire il deploy."
fi
ok "Nessuna modifica locale in sospeso"

# ---- 2. Backup database e .env ----
step "2/9 · Backup database e .env"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  fail "Python del virtualenv non disponibile: $VENV_DIR/bin/python"
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
if [[ -f "$REPO_DIR/bot_data.db" ]]; then
  "$VENV_DIR/bin/python" - "$REPO_DIR/bot_data.db" "$BACKUP_DIR/bot_data.db.bak-$STAMP" <<'PY' \
    || fail "Backup del database fallito: nessuna modifica eseguita"
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
target.close()
source.close()
PY
  ok "Database salvato in backups/bot_data.db.bak-$STAMP"
else
  warn "bot_data.db non trovato: salto il backup del database"
fi
if [[ -f "$REPO_DIR/.env" ]]; then
  cp "$REPO_DIR/.env" "$BACKUP_DIR/env.bak-$STAMP"
  chmod 600 "$BACKUP_DIR/env.bak-$STAMP"
  ok "Configurazione salvata in backups/env.bak-$STAMP"
fi
# Conserva solo gli ultimi $BACKUPS_TO_KEEP backup di ciascun tipo.
for pattern in "bot_data.db.bak-*" "env.bak-*"; do
  find "$BACKUP_DIR" -maxdepth 1 -name "$pattern" -type f -print \
    | sort -r | tail -n +"$((BACKUPS_TO_KEEP + 1))" | xargs -r rm -f
done

# ---- 3. git pull ----
step "3/9 · git pull"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PREVIOUS_HEAD="$(git rev-parse --short HEAD)"
rollback_hint() {
  echo ""
  warn "Per tornare alla versione precedente:"
  echo "   git checkout $PREVIOUS_HEAD && cp backups/env.bak-$STAMP .env && sudo systemctl restart $BOT_SERVICE"
}
git pull --ff-only origin "$BRANCH" || fail "git pull fallito. Risolvi eventuali conflitti manualmente e rilancia lo script."
ok "Codice aggiornato (branch: $BRANCH, da $PREVIOUS_HEAD a $(git rev-parse --short HEAD))"

# ---- 4. Dipendenze bot ----
step "4/9 · Dipendenze bot"
if [[ -d "$VENV_DIR" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  pip install -r "$REPO_DIR/requirements.txt" -q
  deactivate
  ok "Dipendenze bot aggiornate (venv: $VENV_DIR)"
else
  warn "Nessun virtualenv trovato in $VENV_DIR — salto l'installazione dipendenze bot. " \
       "Se il bot usa un venv con un altro nome, aggiorna VENV_DIR in cima allo script."
fi

# ---- 5. Dipendenze dashboard ----
step "5/9 · Dipendenze dashboard"
if [[ -d "$VENV_DIR" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  pip install -r "$REPO_DIR/dashboard/requirements.txt" -q
  deactivate
else
  pip3 install -r "$REPO_DIR/dashboard/requirements.txt" --break-system-packages -q
fi
ok "Dipendenze dashboard aggiornate"

# ---- 6. Preflight fail-closed ----
step "6/9 · Preflight produzione"
PREFLIGHT_OUTPUT="$("$VENV_DIR/bin/python" "$REPO_DIR/scripts/preflight_production.py" \
  --allow-live \
  --db-path "$REPO_DIR/bot_data.db")" || {
  echo "$PREFLIGHT_OUTPUT"
  rollback_hint
  fail "Preflight fallito: nessun servizio è stato riavviato"
}
if [[ "$PREFLIGHT_OUTPUT" == *'"dry_run":false'* ]]; then
  warn "LIVE: DRY_RUN=false, il bot pubblica davvero su X (sempre con approvazione Telegram)"
else
  ok "Modalità dry-run: nessuna pubblicazione reale su X"
fi
ok "Preflight superato: approval-only, configurazione valida e database integro"

# ---- 7. Servizio systemd della dashboard ----
step "7/9 · Servizio systemd della dashboard"
if [[ ! -f "$SYSTEMD_DIR/$DASHBOARD_SERVICE.service" ]] || ! cmp -s "$DASHBOARD_SERVICE_FILE" "$SYSTEMD_DIR/$DASHBOARD_SERVICE.service"; then
  sudo cp "$DASHBOARD_SERVICE_FILE" "$SYSTEMD_DIR/$DASHBOARD_SERVICE.service"
  sudo systemctl daemon-reload
  sudo systemctl enable "$DASHBOARD_SERVICE" >/dev/null
  ok "Servizio $DASHBOARD_SERVICE installato/aggiornato e abilitato all'avvio automatico"
else
  ok "Servizio $DASHBOARD_SERVICE già installato e aggiornato, nessuna modifica necessaria"
fi

# ---- 8. Permessi lettura log ----
step "8/9 · Permessi lettura log (gruppo systemd-journal)"
NEEDS_RELOGIN=0
if ! groups "$USER" | grep -qw systemd-journal; then
  sudo usermod -aG systemd-journal "$USER"
  NEEDS_RELOGIN=1
  warn "Utente '$USER' aggiunto al gruppo systemd-journal ora."
else
  ok "Permessi journal già presenti per '$USER'"
fi

# ---- 9. Riavvio servizi ----
step "9/9 · Riavvio bot e dashboard"
# daemon-reload incondizionato: qualunque unit file sia cambiato su disco
# (dashboard copiato allo step 5, bot modificato manualmente in passato, o
# qualsiasi altra causa) la cache di systemd viene sempre riallineata prima
# del restart, evitando il warning "unit file ... changed on disk".
sudo systemctl daemon-reload
RESTARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
sudo systemctl restart "$BOT_SERVICE"
sudo systemctl restart "$DASHBOARD_SERVICE"
sleep 2

BOT_STATE="$(systemctl is-active "$BOT_SERVICE" || true)"
DASH_STATE="$(systemctl is-active "$DASHBOARD_SERVICE" || true)"

echo ""
if [[ "$BOT_STATE" == "active" ]]; then
  ok "$BOT_SERVICE: attivo"
else
  rollback_hint
  fail "$BOT_SERVICE NON è attivo (stato: $BOT_STATE). Controlla: journalctl -u $BOT_SERVICE -n 50 --no-pager"
fi

sleep 8
if sudo journalctl -u "$BOT_SERVICE" --since "$RESTARTED_AT" --no-pager | grep -q "Traceback"; then
  sudo journalctl -u "$BOT_SERVICE" --since "$RESTARTED_AT" --no-pager | grep -A 15 "Traceback" | tail -20
  rollback_hint
  fail "$BOT_SERVICE ha registrato un Traceback dopo il riavvio"
fi
ok "Nessun Traceback nei log del bot dopo il riavvio"

if [[ "$DASH_STATE" == "active" ]]; then
  ok "$DASHBOARD_SERVICE: attivo"
else
  fail "$DASHBOARD_SERVICE NON è attivo (stato: $DASH_STATE). Controlla: journalctl -u $DASHBOARD_SERVICE -n 50 --no-pager"
fi

echo ""
ok "Deploy completato."
echo "   Tunnel dashboard: ssh -L 5050:127.0.0.1:5050 ubuntu@<ip-server>  →  http://127.0.0.1:5050"

if [[ "$NEEDS_RELOGIN" -eq 1 ]]; then
  echo ""
  warn "Sei stato aggiunto ora al gruppo systemd-journal: la lettura dei log nella dashboard " \
       "funzionerà solo dopo che ti disconnetti e riconnetti via SSH (o rilanci ./deploy.sh una seconda volta dopo il re-login)."
fi
