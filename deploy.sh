#!/usr/bin/env bash
#
# deploy.sh — aggiorna ed avvia bot + dashboard in un solo comando.
#
# Cosa fa, in ordine:
#   1. Verifica che non ci siano modifiche locali non committate
#   2. git pull del branch corrente
#   3. Aggiorna le dipendenze del bot (requirements.txt, nel venv se presente)
#   4. Aggiorna le dipendenze della dashboard (dashboard/requirements.txt)
#   5. Installa/aggiorna il servizio systemd della dashboard (se cambiato)
#   6. Verifica/aggiunge i permessi per leggere i log (gruppo systemd-journal)
#   7. Riavvia bot e dashboard, e controlla che siano davvero attivi
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

step() { echo -e "\n\033[1;36m▶ $1\033[0m"; }
ok()   { echo -e "\033[1;32m✅ $1\033[0m"; }
warn() { echo -e "\033[1;33m⚠️  $1\033[0m"; }
fail() { echo -e "\033[1;31m❌ $1\033[0m"; exit 1; }

cd "$REPO_DIR"

# ---- 1. Modifiche locali non committate ----
step "1/7 · Controllo modifiche locali non committate"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Ci sono modifiche locali non committate in $REPO_DIR. Fai commit o 'git stash' prima di eseguire il deploy."
fi
ok "Nessuna modifica locale in sospeso"

# ---- 2. git pull ----
step "2/7 · git pull"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull origin "$BRANCH" || fail "git pull fallito. Risolvi eventuali conflitti manualmente e rilancia lo script."
ok "Codice aggiornato (branch: $BRANCH)"

# ---- 3. Dipendenze bot ----
step "3/7 · Dipendenze bot"
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

# ---- 4. Dipendenze dashboard ----
step "4/7 · Dipendenze dashboard"
if [[ -d "$VENV_DIR" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  pip install -r "$REPO_DIR/dashboard/requirements.txt" -q
  deactivate
else
  pip3 install -r "$REPO_DIR/dashboard/requirements.txt" --break-system-packages -q
fi
ok "Dipendenze dashboard aggiornate"

# ---- 5. Servizio systemd della dashboard ----
step "5/7 · Servizio systemd della dashboard"
if [[ ! -f "$SYSTEMD_DIR/$DASHBOARD_SERVICE.service" ]] || ! cmp -s "$DASHBOARD_SERVICE_FILE" "$SYSTEMD_DIR/$DASHBOARD_SERVICE.service"; then
  sudo cp "$DASHBOARD_SERVICE_FILE" "$SYSTEMD_DIR/$DASHBOARD_SERVICE.service"
  sudo systemctl daemon-reload
  sudo systemctl enable "$DASHBOARD_SERVICE" >/dev/null
  ok "Servizio $DASHBOARD_SERVICE installato/aggiornato e abilitato all'avvio automatico"
else
  ok "Servizio $DASHBOARD_SERVICE già installato e aggiornato, nessuna modifica necessaria"
fi

# ---- 6. Permessi lettura log ----
step "6/7 · Permessi lettura log (gruppo systemd-journal)"
NEEDS_RELOGIN=0
if ! groups "$USER" | grep -qw systemd-journal; then
  sudo usermod -aG systemd-journal "$USER"
  NEEDS_RELOGIN=1
  warn "Utente '$USER' aggiunto al gruppo systemd-journal ora."
else
  ok "Permessi journal già presenti per '$USER'"
fi

# ---- 7. Riavvio servizi ----
step "7/7 · Riavvio bot e dashboard"
sudo systemctl restart "$BOT_SERVICE"
sudo systemctl restart "$DASHBOARD_SERVICE"
sleep 2

BOT_STATE="$(systemctl is-active "$BOT_SERVICE" || true)"
DASH_STATE="$(systemctl is-active "$DASHBOARD_SERVICE" || true)"

echo ""
if [[ "$BOT_STATE" == "active" ]]; then
  ok "$BOT_SERVICE: attivo"
else
  fail "$BOT_SERVICE NON è attivo (stato: $BOT_STATE). Controlla: journalctl -u $BOT_SERVICE -n 50 --no-pager"
fi

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
