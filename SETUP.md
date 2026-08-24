# Setup approval-only

Questa release usa Telegram come control plane obbligatorio. `DRY_RUN=false`
permette soltanto a `Publisher` di inviare una bozza inglese già approvata in
Telegram e assegnata a uno dei due piani giornalieri USA. La traduzione italiana
è esclusivamente un aiuto privato alla revisione e non raggiunge mai X.

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
POSTS_PER_DAY=2
APPROVED_QUEUE_TARGET=7
PENDING_REVIEW_LIMIT=3
DRAFT_GENERATION_DAILY_CAP=4
AUDIENCE_TIMEZONE=America/New_York
MORNING_WINDOW=08:30-11:30
EVENING_WINDOW=16:30-20:30
MIN_POST_GAP_HOURS=6
ADAPTIVE_TIMING_MIN_POSTS=30
ADAPTIVE_WEEKDAY_MIN_POSTS=90
PUBLICATION_PLAN_GRACE_MINUTES=90
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

Avviare il servizio con `DRY_RUN=true`. Il bot rifornisce la coda ogni 30
minuti, pianifica ogni 15 minuti e simula i piani dovuti ogni 5 minuti:

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
- [ ] ogni card mostra l'inglese completo e la traduzione italiana completa;
- [ ] prova di tutti i pulsanti bozza: `Approva`, `Rigenera`, `Modifica`, `Scegli media`, `Solo testo`, `Posticipa` e `Scarta`;
- [ ] `/pause` impedisce la pubblicazione e `/resume` la riabilita;
- [ ] reinvio dello stesso callback senza doppia mutazione;
- [ ] digest growth con link/username e sole azioni manuali;
- [ ] snapshot follower e report `/stats` coerenti;
- [ ] riserva `approved/planned` pari a 7 e non più di 3 card in revisione;
- [ ] due giornate `America/New_York` con 2 piani `simulated` al giorno, uno
      nella finestra mattutina e uno nella finestra serale, separati di 6 ore;
- [ ] `/errors` privo di errori sistemici irrisolti;
- [ ] conteggio scritture X pari a zero, inclusi post ed engagement.

Controllare inoltre `/status`, `/posts`, `/errors` e `bot.log`. L'allowlist scheduler deve contenere soltanto i job documentati nel README; la discovery lead deve essere assente.

Non cambiare `DRY_RUN` durante questa checklist. Il passaggio live descritto
sotto richiede una nuova autorizzazione esplicita; `APPROVAL_REQUIRED=true`
resta obbligatorio anche dopo l'attivazione.

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

## 9. Attivazione automatica reale, separata dal dry-run

Prima dell'autorizzazione finale la configurazione persistente deve rimanere:

```dotenv
APPROVAL_REQUIRED=true
DRY_RUN=true
```

1. Completare due intere giornate USA in dry-run con 2 piani simulati per
   giornata, riserva da 7, card bilingui complete, `/errors` pulito e zero write X.
2. Controllare su Telegram le bozze approvate, gli orari ET/Roma e gli eventuali
   media. Usare `/pause` se esiste qualunque dubbio.
3. Eseguire backup SQLite e preflight; annotare HEAD e conteggi dei piani.
4. Ottenere una nuova autorizzazione esplicita a modificare il solo valore
   `DRY_RUN=false`. Non cambiare `APPROVAL_REQUIRED=true`.
5. Fermare il servizio, modificare `.env`, ripetere `validate_config()` e
   `PRAGMA integrity_check`, quindi riavviare il servizio.
6. Sorvegliare il primo piano dovuto. `published` deve produrre un solo tweet;
   `unknown`/`publication_unknown` richiede riconciliazione manuale su X e non
   va mai ritentato. In caso di errore usare subito `/pause`.
7. Dopo il primo giorno live verificare esattamente due pubblicazioni, testo X
   solo inglese, media corretto, nessun engagement automatico e `/errors` pulito.

## Troubleshooting

- `Variabili d'ambiente mancanti`: verificare tutte le credenziali obbligatorie; se sono configurati domini news, aggiungere `NEWSAPI_KEY`.
- `APPROVAL_REQUIRED must be true`: ripristinare `APPROVAL_REQUIRED=true`.
- Nessun draft: aggiungere una fonte ammissibile e controllare outcome/errori in SQLite e `/errors`.
- Fonti automatiche assenti: verificare `/api/editorial-feed`, il job `source_refresh` delle 10:30 e `/errors`; `NEWSAPI_KEY` serve soltanto se l'allowlist esterna non è vuota.
- Draft non pubblicato: verificare traduzione pronta, approvazione, `/pause`,
  assegnazione al piano ET, grace window di 90 minuti e `DRY_RUN`.
- Media rifiutato: verificare MIME reale, dimensione, permessi della directory e disponibilità di `ffmpeg` per i video.
- Polling Telegram fermo: verificare token, chat ID, timeout e log sanitizzati; non stampare mai token o URL bot completi.
