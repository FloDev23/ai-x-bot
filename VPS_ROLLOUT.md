# VPS Rollout — Task 7 Step 6 (dry-run)

`DRY_RUN=true` e `APPROVAL_REQUIRED=true` obbligatori per tutta la procedura.

## 1. Backup + preflight

```bash
cp bot_data.db bot_data.db.bak-$(date +%Y%m%d-%H%M%S)

venv/bin/python scripts/preflight_production.py \
  --require-dry-run \
  --db-path ./bot_data.db
```

Deve restituire solo configurazione, booleani e `integrity_check=ok`.
Se fallisce: **non continuare**.

---

## 2. Pull

```bash
git pull origin main
git log --oneline -3
```

Verifica che HEAD sia `e51ac9c test: acceptance story + operator docs for Task 7`.

---

## 3. Migrazione schema

```bash
venv/bin/python -c "
from modules.database import Database
db = Database('./bot_data.db')
print('schema ok')
"
```

Nessun errore = tabelle aggiornate.

---

## 4. Validazione config

```bash
venv/bin/python -c "from config import validate_config; validate_config(); print('config ok')"
```

---

## 5. Aggiungi variabili mancanti in `.env` (se non presenti)

Apri `.env` e verifica che esistano:

```dotenv
GROWTH_DIGEST_TIME=09:00
GROWTH_ACCOUNT_SUGGESTION_LIMIT=5
GROWTH_POST_SUGGESTION_LIMIT=10
GROWTH_POST_QUERY_BUDGET=2
GROWTH_SUGGESTION_COOLDOWN_DAYS=30
GROWTH_UNFOLLOW_REVIEW_DAYS=14
```

Rimuovi `GROWTH_DIGEST_LIMIT` se presente (deprecato).

Ripeti validate_config dopo ogni modifica a `.env`.

---

## 6. Riavvio servizio

```bash
sudo systemctl restart flexdropin-bot
sudo systemctl status flexdropin-bot
```

Il processo deve essere `active (running)`. Aspetta 5 secondi, poi:

```bash
tail -30 bot.log
```

Nessun traceback = ok.

---

## 7. Smoke test via Telegram

Esegui questi comandi nella chat autorizzata nell'ordine indicato.
Spunta ogni riga dopo la verifica.

- [ ] `/status` — mostra conteggi coda, nessun errore
- [ ] `/errors` — vuoto
- [ ] `/posts` — indice compatto (≤ 8 righe) o "nessun post"
- [ ] `/media` — browser apre (o "nessun media disponibile")
- [ ] `/newpost` → scrivi testo inglese → scegli `fitness_business_insight` → `Nessuna fonte` → `Nessun media` — un post approvato compare in `/posts`
- [ ] `/growth` — "Nessun nuovo suggerimento." (il digest arriva alle 09:00 Rome)
- [ ] `/pause` → `/resume` — entrambi rispondono correttamente
- [ ] Carica una foto → nessuna bozza creata, media registrato in `/media`
- [ ] `/errors` — ancora vuoto

---

## 8. Verifica log dopo smoke test

```bash
grep -i "error\|traceback\|exception\|create_tweet\|post_tweet" bot.log | tail -20
```

Zero righe con `create_tweet` o `post_tweet` = nessuna scrittura X.

---

## 9. Attesa digest growth (09:00 Rome)

Il giorno dopo alle 09:00 `Europe/Rome`, `/growth` deve mostrare:

```
Growth giornaliero — azioni solo manuali
Account: N
Post: N
Da rivalutare: N
[Account] [Post] [Da rivalutare]
```

- Clicca `Account` → scheda con `@username`, pulsante `Segnala come seguito`
- Clicca `Segnala come seguito` → "Seguito registrato solo localmente; nessuna azione è stata inviata a X."
- `/errors` ancora vuoto

---

## 10. Due giornate USA simulate

Osserva i log per almeno due giornate:

```bash
grep "simulated\|planned\|published" bot.log | tail -20
```

- Almeno 2 piani `simulated` per giornata
- Zero `published` (DRY_RUN=true)
- Zero `create_tweet` nel log

---

## Quando la checklist è completa

Torna qui e scrivi "checklist completata" — marco Task 7 completo e il bot è pronto per la fase live (autorizzazione separata, vedere SETUP.md §9).
