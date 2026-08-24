# Setup approval-only

Questa release usa Telegram come control plane obbligatorio. Non abilita pubblicazione unattended: `DRY_RUN=false` permette soltanto a `Publisher` di inviare una bozza già approvata in Telegram e dovuta per lo slot esatto.

## 1. Prerequisiti

- Python 3.11 o versione compatibile;
- credenziali X API per letture e pubblicazione;
- chiave Groq;
- bot Telegram e ID della sola chat autorizzata;
- NewsAPI solo se si abilita una allowlist `NEWS_TRUSTED_DOMAINS`;
- `ffmpeg` per l'analisi dei video caricati.

Il feed ufficiale `https://flexdropin.com/api/editorial-feed` è fisso nel codice e non richiede credenziali.

## 2. Installazione

```bash
git clone https://github.com/FloDev23/ai-x-bot.git
cd ai-x-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Non committare `.env`, `bot_data.db`, i log o la libreria media.

## 3. Credenziali obbligatorie

Compilare in `.env`:

```dotenv
TWITTER_API_KEY=
TWITTER_API_SECRET=
TWITTER_ACCESS_TOKEN=
TWITTER_ACCESS_TOKEN_SECRET=
TWITTER_BEARER_TOKEN=
GROQ_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

`TELEGRAM_CHAT_ID` è confrontato su ogni update; messaggi e callback provenienti da altre chat non modificano lo stato.

Per abilitare news verificate, inserire soltanto domini fidati e una chiave:

```dotenv
NEWS_TRUSTED_DOMAINS=example.com,industry.example
NEWSAPI_KEY=
```

Con `NEWS_TRUSTED_DOMAINS=` la fetch news è disabilitata e `NEWSAPI_KEY` non è richiesta.

Il canale NewsAPI riguarda solo fonti esterne. Non modifica né disabilita il refresh quotidiano degli articoli del blog FlexDropin.

## 4. Switch iniziali esatti

Usare questi valori durante tutto il dry-run:

```dotenv
BOT_TIMEZONE=Europe/Rome
CONTENT_SLOTS=14:00,20:00
DRAFT_LEAD_MINUTES=120
PUBLISH_GRACE_SECONDS=300
APPROVAL_REQUIRED=true
DRY_RUN=true
DRAFT_SCORE_THRESHOLD=70
SEMANTIC_DUPLICATE_THRESHOLD=0.72
MAX_LINKS_PER_WEEK=1
ENABLE_LEAD_DISCOVERY=false
MEDIA_MATCH_THRESHOLD=80
TELEGRAM_POLL_TIMEOUT=25
TELEGRAM_MAX_IMAGE_BYTES=10485760
TELEGRAM_MAX_VIDEO_BYTES=52428800
GROWTH_SCORE_THRESHOLD=75
GROWTH_QUERY_BUDGET=3
GROWTH_NEW_PROFILE_BUDGET=25
GROWTH_PROFILE_CACHE_DAYS=7
GROWTH_DIGEST_LIMIT=5
GROWTH_SEED_ACCOUNTS=
NEWS_TRUSTED_DOMAINS=
```

`validate_config()` rifiuta token/chat Telegram mancanti e `APPROVAL_REQUIRED=false`. Richiede `NEWSAPI_KEY` solo quando è configurato almeno un dominio fidato. Il query budget growth è comunque limitato a tre.

## 5. Test locale senza rete

```bash
venv/bin/python -m pytest tests/test_end_to_end_dry_run.py -v
venv/bin/python -m pytest -v
venv/bin/python -m compileall -q main.py config.py modules dashboard
git diff --check
```

I test usano dependency injection esplicita e boundary fake: non contattano X, Telegram, Groq o NewsAPI.

## 6. Aggiornamento automatico delle fonti

Alle 10:30 `Europe/Rome` il job `source_refresh` aggiorna separatamente:

- gli articoli inglesi canonici del blog, come `owned_blog_article`;
- le news esterne, solo dai domini in `NEWS_TRUSTED_DOMAINS`.

Un guasto di un canale non annulla l'altro. Successo e assenza di novità non generano messaggi Telegram; gli errori sistemici vengono sanitizzati e sono consultabili con `/errors`. Un articolo blog non viene trattato come product fact. I suoi link rispettano `MAX_LINKS_PER_WEEK=1` e un cooldown di 30 giorni sullo stesso articolo.

## 7. Dry-run sul VPS

Avviare il servizio con `DRY_RUN=true`:

```bash
python -c "from config import validate_config; validate_config()"
python main.py
```

Completare e annotare tutta la checklist:

- [ ] autorizzazione Telegram: la chat corretta funziona e una chat diversa è ignorata;
- [ ] inserimento e classificazione di una fonte testuale;
- [ ] upload di una foto senza creazione automatica di una bozza;
- [ ] upload di un video senza creazione automatica di una bozza;
- [ ] ricezione della preview della bozza e del media abbinato;
- [ ] prova di tutti i pulsanti bozza: `Approva`, `Rigenera`, `Modifica`, `Scegli media`, `Solo testo`, `Posticipa` e `Scarta`;
- [ ] `/pause` impedisce la pubblicazione e `/resume` la riabilita;
- [ ] reinvio dello stesso callback senza doppia mutazione;
- [ ] digest growth con link/username e sole azioni manuali;
- [ ] snapshot follower e report `/stats` coerenti;
- [ ] conteggio scritture X pari a zero, inclusi post ed engagement.

Controllare inoltre `/status`, `/posts`, `/errors` e `bot.log`. L'allowlist scheduler deve contenere soltanto i job documentati nel README; la discovery lead deve essere assente.

Usare `DRY_RUN=false` soltanto come override del singolo processo one-shot descritto sotto, dopo che l'intera checklist ha superato il test sul VPS. Non modificare questi valori nel `.env` persistente e non cambiare `APPROVAL_REQUIRED=true`.

Prima di un deploy o riavvio, con il database già presente, eseguire il controllo fail-closed:

```bash
venv/bin/python scripts/preflight_production.py \
  --require-dry-run \
  --db-path ./bot_data.db
```

Il comando restituisce soltanto stato di configurazione, booleani, conteggio domini e integrità del database. Non stampa credenziali. `./deploy.sh` esegue lo stesso controllo automaticamente prima di toccare i servizi e si ferma se non passa.

## 8. Esecuzione persistente

Usare un service manager con working directory del repository, utente non privilegiato, restart controllato e accesso in scrittura a database/log/media. Esempio systemd:

```ini
[Unit]
Description=FlexDropin approval-only X growth agent
After=network-online.target

[Service]
Type=simple
User=flexdropin-bot
WorkingDirectory=/opt/ai-x-bot
EnvironmentFile=/opt/ai-x-bot/.env
ExecStart=/opt/ai-x-bot/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Alla ricezione di `Ctrl+C` il processo segnala lo stesso `threading.Event` al polling Telegram, ferma lo scheduler e attende il thread entro un timeout limitato.

## 9. Prima pubblicazione reale controllata

La configurazione persistente deve rimanere:

```dotenv
APPROVAL_REQUIRED=true
DRY_RUN=true
```

1. Attendere il refresh automatico delle 10:30 oppure avviare il ciclo di refresh previsto dall'applicazione. Verificare `/errors` e che il feed `https://flexdropin.com/api/editorial-feed` risponda con la versione attesa.
2. Far creare una bozza per uno slot configurato. Deve avere score almeno 70 ed essere ancora entro la grace window quando verrà pubblicata.
3. Leggere testo e media su Telegram e premere `Approva` dalla chat autorizzata.
4. Sul VPS acquisire la fingerprint senza cambiare stato:

```bash
venv/bin/python scripts/publish_once.py inspect --draft-id <DRAFT_ID>
```

5. Confrontare ID, revisione, slot, score, presenza media e fingerprint con la bozza appena approvata. Ottenere una seconda autorizzazione umana esplicita per quella fingerprint.
6. Fermare il servizio per evitare concorrenti, pubblicare una sola volta con override limitato al processo e riavviare sempre il servizio:

```bash
restart_bot() { sudo systemctl restart flexdropin-bot; }
trap restart_bot EXIT INT TERM
sudo systemctl stop flexdropin-bot
DRY_RUN=false venv/bin/python scripts/publish_once.py publish \
  --draft-id <DRAFT_ID> \
  --fingerprint <FINGERPRINT> \
  --confirm PUBLISH_ONE_APPROVED_FLEXDROPIN_DRAFT
restart_bot
trap - EXIT INT TERM
```

Il comando rilegge la stessa bozza e revision, ricontrolla approvazione, pausa, slot, grace window, media e idempotenza. `published` è successo; `rejected` o `snapshot_changed` non scrivono; `publication_unknown` richiede riconciliazione manuale su X e non deve essere ritentato. Al termine rieseguire il preflight in dry-run e verificare che `flexdropin-bot` sia attivo.

Non modificare `.env` a `DRY_RUN=false`, non lanciare il comando una seconda volta e non usare il percorso normale dello scheduler per il primo post reale.

## Troubleshooting

- `Variabili d'ambiente mancanti`: verificare tutte le credenziali obbligatorie; se sono configurati domini news, aggiungere `NEWSAPI_KEY`.
- `APPROVAL_REQUIRED must be true`: ripristinare `APPROVAL_REQUIRED=true`.
- Nessun draft: aggiungere una fonte ammissibile e controllare outcome/errori in SQLite e `/errors`.
- Fonti automatiche assenti: verificare `/api/editorial-feed`, il job `source_refresh` delle 10:30 e `/errors`; `NEWSAPI_KEY` serve soltanto se l'allowlist esterna non è vuota.
- Draft non pubblicato: verificare approvazione, `/pause`, slot esatto, grace window e `DRY_RUN`.
- Media rifiutato: verificare MIME reale, dimensione, permessi della directory e disponibilità di `ffmpeg` per i video.
- Polling Telegram fermo: verificare token, chat ID, timeout e log sanitizzati; non stampare mai token o URL bot completi.
