# FlexDropin X Growth Agent

Un servizio Python approval-only per preparare contenuti X, gestirli da Telegram e osservare la crescita follower. SQLite conserva fonti, bozze, approvazioni, media, candidati e metriche.

Il processo non mette like, non segue/smette di seguire, non risponde e non invia DM. L'unica scrittura X è la pubblicazione di una specifica bozza che:

- deriva da fonti persistite e ammissibili;
- supera fact-check, scoring e controllo duplicati;
- è stata approvata esplicitamente dalla chat Telegram autorizzata;
- è assegnata a un piano giornaliero persistente, è dovuta e non è in pausa;
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

Lo scheduler gira in `Europe/Rome`; le due o tre ore di pubblicazione vengono
invece calcolate come orari reali `America/New_York`, quindi seguono
automaticamente l'ora legale statunitense. Il processo registra soltanto:

- refresh indipendente del blog FlexDropin e delle news allowlisted alle 10:30;
- rifornimento della coda approvabile ogni 30 minuti;
- retry delle traduzioni italiane ogni 30 minuti;
- creazione/riconciliazione dei piani USA ogni 15 minuti;
- controllo dei piani dovuti ogni 5 minuti;
- discovery growth read-only alle 11:00;
- snapshot follower alle 23:15;
- metriche dei post propri alle 23:30;
- report growth Telegram il lunedì alle 09:00.

La discovery lead è secondaria e viene aggiunta solo con `ENABLE_LEAD_DISCOVERY=true`. Non esistono job di engagement automatico, human-mode o build-in-public.

Telegram long polling gira in un thread daemon nominato. Scheduler e polling condividono un `threading.Event` per lo shutdown ordinato.

## Flusso quotidiano

1. Il refresh automatico aggiorna il blog FlexDropin e le eventuali news fidate.
2. Ogni 30 minuti il bot prova a mantenere una riserva di 14 post: massimo 5
   bozze in revisione e 5 nuove bozze per giorno `Europe/Rome`.
3. Fact guard, scorer e duplicate gate rifiutano contenuti non sicuri.
4. Telegram mostra sempre il tweet inglese completo e sotto la traduzione
   italiana completa, marcata come testo di sola revisione.
5. Solo il callback `Approva` della chat autorizzata inserisce la bozza nella
   riserva pubblicabile. Una traduzione mancante o stale blocca l'approvazione.
6. Ogni giorno vengono creati almeno due piani. Nel cold start martedì, giovedì
   e sabato ne vengono creati tre; dopo 30 post maturi il bot può scegliere
   dinamicamente tre giorni settimanali. Gli slot cadono nelle finestre
   08:30–10:30, 13:00–15:30 e 18:00–20:30 ET, separati da almeno 4 ore.
7. Al momento dovuto `Publisher` ricontrolla piano, revisione, fonti, pausa,
   media e idempotenza, quindi invia a X esclusivamente il testo inglese.
8. Con `DRY_RUN=true` il piano diventa `simulated`, la bozza resta approvata e
   non viene eseguita alcuna chiamata di scrittura X.

I comandi Telegram includono `/status`, `/posts`, `/growth`, `/stats`, `/ideas`,
`/newpost`, `/pause`, `/resume`, `/errors` e `/help`. `/newpost` conserva il
testo inglese esatto, fa scegliere categoria, 1–3 fonti e un media facoltativo,
poi prepara o acquisisce la traduzione italiana prima di creare una sola bozza
in revisione. Il refresh riuscito o senza novità è silenzioso; `/errors` mostra
solo codici di errore sistemici sanitizzati.

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

Il test end-to-end è in `tests/test_end_to_end_dry_run.py` e prova fonte →
coda bilingue → approvazione Telegram → riserva da 14 → due/tre piani ET →
simulazioni, incluso un riavvio senza duplicati, `/newpost` e zero scritture
X/engagement.

Prima di ogni riavvio sul VPS, `deploy.sh` esegue un preflight in sola lettura
che richiede `APPROVAL_REQUIRED=true`, `DRY_RUN=true`, configurazione valida e
`PRAGMA integrity_check=ok`. Il passaggio alla pubblicazione automatica reale è
una fase separata: richiede due giornate USA simulate, una riserva di 14 post,
`/errors` pulito e una nuova autorizzazione esplicita prima di cambiare
`DRY_RUN`. La procedura completa è in [SETUP.md](SETUP.md).

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
