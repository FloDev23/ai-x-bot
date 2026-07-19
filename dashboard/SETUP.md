# Dashboard flexdropin-bot — setup

Dashboard di sola lettura (+ un'unica azione: chiudere un lead) che legge
direttamente dal database SQLite del bot (`bot_data.db`) e dai log di
systemd. **Non è mai esposta pubblicamente**: gira su `127.0.0.1` e la
raggiungi solo tramite tunnel SSH.

## Setup automatico (consigliato) — `deploy.sh`

Nella root del progetto trovi `deploy.sh`, che fa tutto da solo: pull del
codice, dipendenze bot + dashboard, installazione/aggiornamento del
servizio systemd della dashboard, permessi per leggere i log, riavvio di
entrambi i servizi e verifica finale che siano attivi.

**Primo utilizzo:**

```bash
cd ~/ai-x-bot
git pull origin main          # per scaricare deploy.sh e i file della dashboard la prima volta
chmod +x deploy.sh            # se non è già eseguibile
./deploy.sh
```

Se lo script ti segnala che sei stato appena aggiunto al gruppo
`systemd-journal` (necessario per leggere i log dalla dashboard),
disconnettiti e riconnettiti via SSH, poi rilancia `./deploy.sh` una
seconda volta: da quel momento la lettura dei log funzionerà.

**Ogni aggiornamento successivo** (nuove modifiche al bot o alla
dashboard), da qui in poi basta:

```bash
cd ~/ai-x-bot
./deploy.sh
```

Un solo comando: pull, dipendenze, restart di bot e dashboard, e conferma
che entrambi i servizi siano tornati attivi. Se qualcosa va storto (es.
conflitti git, servizio che non riparte), lo script si ferma e ti dice
esattamente cosa controllare.

## Setup manuale (se preferisci fare i passaggi a mano, o per capire cosa fa `deploy.sh`)

### 1. Installa le dipendenze (sul server, via Termius)

```bash
cd ~/ai-x-bot/dashboard
pip3 install -r requirements.txt
```

### 2. Permessi per leggere i log di systemd

L'utente che esegue la dashboard deve poter leggere `journalctl`. Se non
gira già come root:

```bash
sudo usermod -aG systemd-journal ubuntu
```

(sostituisci `ubuntu` con il tuo utente se diverso — poi disconnetti e
riconnetti la sessione SSH perché il gruppo venga applicato)

### 3. Avvia la dashboard come servizio systemd separato dal bot

```bash
sudo cp ~/ai-x-bot/dashboard/flexdropin-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flexdropin-dashboard
sudo systemctl status flexdropin-dashboard
```

Se il percorso del progetto sul server non è `/home/ubuntu/ai-x-bot`,
modifica `WorkingDirectory` ed `ExecStart` nel file `.service` prima di
copiarlo.

### 4. Apri il tunnel SSH dalla tua macchina (non dal server)

```bash
ssh -L 5050:127.0.0.1:5050 ubuntu@<ip-del-server>
```

Lascia questa sessione SSH aperta (puoi anche tenerla come tab separata in
Termius), poi apri nel browser del tuo computer:

```
http://127.0.0.1:5050
```

Vedrai la dashboard esattamente come se girasse in locale — nessuna porta è
aperta verso l'esterno sul server, quindi nessun rischio di esposizione
pubblica né bisogno di login/password.

## Pagine disponibili

- **Panoramica** — contatori rapidi + ultimi lead, ultimi account target, ultimi errori
- **Lead** — tutti i lead con filtro per stato (nuovo/gestito/ignorato), link diretto al tweet e al profilo, pulsanti per segnare un lead come gestito/ignorato
- **Engagement** — tutti gli account target curati, punteggio, follower, ultima interazione
- **Post** — storico dei post pubblicati con le metriche raccolte (like, reply, retweet, impressions)
- **Log** — ultime righe di `journalctl -u flexdropin-bot`, con filtro "solo errori"

## Note

- La dashboard **non genera né pubblica nulla** su X: è puramente di
  consultazione, tranne il pulsante "Segna gestito/Ignora" che aggiorna solo
  lo stato del lead nel database locale.
- `deploy.sh` si ferma subito se trova modifiche locali non committate sul
  server (per non rischiare di perderle con un `git pull`): in quel caso
  fai prima `git stash` o un commit, poi rilancia lo script.
- Se in futuro vuoi renderla raggiungibile senza tunnel SSH (es. da
  telefono fuori casa), serve aggiungere autenticazione e un reverse proxy
  con HTTPS — non è la configurazione attuale, che assume accesso solo via
  tunnel.

