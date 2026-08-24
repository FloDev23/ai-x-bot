# FlexDropin X Growth Agent

Un servizio Python approval-only per preparare contenuti X, gestirli da Telegram e osservare la crescita follower. SQLite conserva fonti, bozze, approvazioni, media, candidati e metriche.

Il processo non mette like, non segue/smette di seguire, non risponde e non invia DM. L'unica scrittura X è la pubblicazione di una specifica bozza che:

- deriva da fonti persistite e ammissibili;
- supera fact-check, scoring e controllo duplicati;
- è stata approvata esplicitamente dalla chat Telegram autorizzata;
- è dovuta per lo slot esatto e non è in pausa;
- passa attraverso `Publisher`, in modo idempotente.

`DRY_RUN=true` mantiene aperto tutto il flusso fino al confine X, ma non crea tweet. Caricare una foto o un video registra soltanto un media disponibile: non crea una bozza.

## Avvio locale

Richiede Python 3.11 o compatibile, credenziali X, una chiave Groq e un bot Telegram con chat ID autorizzato.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "from config import validate_config; validate_config()"
python main.py
```

Lasciare `APPROVAL_REQUIRED=true`, `DRY_RUN=true` ed `ENABLE_LEAD_DISCOVERY=false` durante il rollout iniziale. `APPROVAL_REQUIRED=false` viene rifiutato all'avvio. Il feed ufficiale FlexDropin non richiede chiavi o variabili; NewsAPI è facoltativa finché `NEWS_TRUSTED_DOMAINS` resta vuota.

La configurazione completa e la procedura VPS sono in [SETUP.md](SETUP.md).

## Scheduler sicuro

Tutti i trigger usano `Europe/Rome` (o `BOT_TIMEZONE`) e il processo registra solo:

- refresh indipendente del blog FlexDropin e delle news allowlisted alle 10:30;
- creazione bozze alle 12:00 e 18:00 per gli slot 14:00 e 20:00;
- tentativi di pubblicazione approvata alle 14:00 e 20:00;
- discovery growth read-only alle 11:00;
- snapshot follower alle 23:15;
- metriche dei post propri alle 23:30;
- report growth Telegram il lunedì alle 09:00.

La discovery lead è secondaria e viene aggiunta solo con `ENABLE_LEAD_DISCOVERY=true`. Non esistono job di engagement automatico, human-mode o build-in-public.

Telegram long polling gira in un thread daemon nominato. Scheduler e polling condividono un `threading.Event` per lo shutdown ordinato.

## Flusso quotidiano

1. Inserire da Telegram una fonte testuale e classificarla.
2. Il planner prepara al massimo due bozze al giorno dagli slot configurati.
3. Fact guard, scorer e duplicate gate rifiutano contenuti non sicuri.
4. Il bot invia la card Telegram con anteprima e controlli.
5. Solo il callback `Approva` della chat autorizzata porta la bozza in stato `approved`.
6. Allo slot esatto `Publisher` verifica stato, pausa, scadenza e idempotenza.
7. In dry-run restituisce `dry_run` senza invocare X.

I comandi Telegram includono `/status`, `/posts`, `/growth`, `/stats`, `/ideas`, `/pause`, `/resume`, `/errors` e `/help`. Il refresh riuscito o senza novità è silenzioso; `/errors` mostra solo codici di errore sistemici sanitizzati.

## Fonti automatiche

Ogni giorno il bot legge il feed fisso `https://flexdropin.com/api/editorial-feed` e, se configurata, NewsAPI. Gli articoli del blog vengono salvati come `owned_blog_article`: non sono product fact e possono sostenere soltanto testo generale, numeri esatti e named entity realmente presenti nel titolo o nel sommario.

Il planner ruota fonti mai usate o meno recenti, esclude quelle già legate a bozze live e seleziona sempre un solo record. Un link al blog consuma la quota globale `MAX_LINKS_PER_WEEK` e lo stesso articolo non viene linkato di nuovo prima di 30 giorni.

## Verifica

La suite non richiede rete e usa boundary fake per X, Telegram, Groq e news:

```bash
venv/bin/python -m pytest -v
venv/bin/python -m compileall -q main.py config.py modules dashboard
git diff --check
```

Il test end-to-end è in `tests/test_end_to_end_dry_run.py` e prova fonte → bozza → approvazione Telegram → publish dry-run con zero scritture X/engagement, oltre all'upload media senza creazione bozza.

Prima di ogni riavvio sul VPS, `deploy.sh` esegue un preflight in sola lettura che richiede `APPROVAL_REQUIRED=true`, `DRY_RUN=true`, configurazione valida e `PRAGMA integrity_check=ok`. Una singola pubblicazione reale non richiede di modificare `.env`: usa `scripts/publish_once.py`, una fingerprint immutabile della bozza approvata e un override `DRY_RUN=false` limitato a quel solo processo. La procedura completa è in [SETUP.md](SETUP.md#9-prima-pubblicazione-reale-controllata).

## Componenti principali

- `main.py`: dependency injection, job allowlist, cicli e shutdown.
- `modules/draft_pipeline.py`: generazione source-backed e gate editoriali.
- `modules/telegram_controller.py`: autorizzazione, callback idempotenti e media intake.
- `modules/publisher.py`: unico confine di scrittura X.
- `modules/growth_discovery.py`: discovery follower read-only.
- `modules/analytics.py`: snapshot, metriche proprie e report settimanale.
- `modules/database.py`: persistenza SQLite concorrente e restart-safe.
- `modules/editorial_feed.py`: client fixed-host e validazione del feed ufficiale.
- `modules/source_refresh.py`: isolamento del refresh blog/NewsAPI.
