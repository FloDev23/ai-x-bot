# CONTEXT — FlexDropin X Growth Agent

Documento di contesto per ripartire senza rianalizzare tutto il repo.
Generato il 2026-10-16 leggendo il codice a HEAD `e7e0293` (branch `main`).
**Stato di produzione NON verificato in questa analisi** (l'accesso SSH al VPS è stato negato dal classifier): vedi §13 per i comandi di verifica.

> Dove README/SETUP e codice divergono, **vince il codice**. README e SETUP sono in parte obsoleti (vedi §12).

---

## 1. Cos'è

Servizio Python **approval-only** (singolo processo) che:

1. prepara e pubblica su X (`@FlexDropin`) post in inglese per **FlexDropin**, app di prenotazione di singole lezioni fitness (drop-in) senza abbonamento (backend Supabase + Stripe Connect; app su Play Store/App Store; sito `flexdropin.com`);
2. è controllato interamente da **Telegram** (una sola chat autorizzata);
3. produce un **digest growth read-only** giornaliero (account da seguire, post dove mettere like, account da non seguire più, bozze di risposta). **Mai** azioni di engagement automatiche: like/follow/unfollow/reply/DM li fa Floriano a mano su X.

Operatore: Floriano (FloDev23, flexdropin@gmail.com), solo founder. Lingua operativa con il bot: italiano (Telegram, doc); contenuto X: inglese (US).

**Unica scrittura X automatica**: `Publisher` → `TwitterClient.create_tweet` (+ `media_upload`) per una bozza approvata e assegnata a un piano dovuto. Il test `tests/test_x_write_safety.py` (AST scan) fa fallire la suite se compaiono chiamate vietate (`like_tweet`, `follow_user`, `reply_to_tweet`, `retweet`, `send_dm`, …) o se `create_tweet`/`post_tweet` appaiono fuori dai file ammessi.

### Progetti collegati
Tutti sotto `/Users/floriano/flo_mobile_app/`:
- `../flexDropin` — app mobile (Expo/React Native + Supabase + Stripe Connect). Fatti di prodotto, fee, categorie: `../flexDropin/CLAUDE.md`.
- `../flexDropin-website` — sito Next.js `flexdropin.com` (EN default, `/it` in euro), hub deeplink. Contesto: `../flexDropin-website/CLAUDE.md`.
- `../flexdropin-growth` — materiali marketing (Product Hunt, outreach, stampa, LinkedIn, directory).
- `../flexdropin-instagram` — contenuti Instagram `@flexdropin` (italiano, target gestori palestre e clienti drop-in a Verona; post programmati in Meta Business Suite, storie pubblicate a mano). Contesto: `../flexdropin-instagram/CLAUDE.md`. Canale separato da X: stesso prodotto, lingua e target diversi.

## 2. Stato e decisioni recenti (cronologia utile)

| Data | Cosa |
|---|---|
| 2026-07/08 | Setup repo, VPS Oracle (`ubuntu@80.225.89.115`), design growth + Telegram (docs/superpowers) |
| 2026-08-24/26 | Coda approvata adattiva, refresh fonti, volume flessibile, `/newpost` manuale, media browser |
| 2026-09-03/04 | Phase 0 stabilization, Reply Copilot (poi **eliminato**), incidente coda pubblicazioni (timestamp naive) riparato |
| 2026-09-15 | Tracking like/follow/following (`x_following`), digest con "like" e "unfollow" |
| 2026-09-23/25 | Campagne inserite via script: product journey (draft 65–74, media 39–48), US gym owner, calendario "human-first" 25 set–31 ott (94→95 post) |
| 2026-09-29 | Product Hunt launch spostato 30 set → **7 ott 2026** (script `update_product_hunt_launch_oct7.py`, `announce_product_hunt_date_change.py`) |
| ~2026-10 | **Outage crediti X API**: post persi → `reschedule_credit_outage_posts.py` (vedi anche memoria `production-vps-access`) |
| 2026-10-03 | **Pivot growth**: da gestori di palestre a **startup founder / indie hacker / build-in-public** (account 10 follower, ~9 impression/post; ricerca gym owner = 0 candidati; i founder ricambiano il follow e supportano PH). Reply Copilot rimosso, codice gym-classifier rimosso |
| 2026-10-03 | `feat(growth)`: bozze di risposta manuali a founder con traduzione IT (`growth_replies`) |
| 2026-10-03/05 | Fix: bozze reply con em dash scartate (ultimo commit `e7e0293`) |
| 2026-10-19 | **Growth fitness-first**: target principale creator/coach/venue fitness, founder secondari (max 2 su 5 account); vedi §8 |

Preferenze utente (da memoria): **cancellare il codice morto** invece di tenerlo; **non reintrodurre logica gym-classifier** nel growth; i gym owner si raggiungono **anche fuori da X** (Instagram, LinkedIn, gruppi FB, outreach diretto); su X si passa dai creator/coach fitness, i cui follower sono il pubblico di FlexDropin.

## 3. Architettura

```
main.py  FlexDropinGrowthAgent  (DI container + APScheduler BackgroundScheduler, tz Europe/Rome)
 ├─ thread daemon "flexdropin-telegram-polling" → TelegramController.run_forever(stop_event)
 └─ job schedulati (vedi §5)

Fonti ──► ContentPlanner ──► DraftPipeline ──► (QueueReplenisher) ──► Telegram card (EN + traduzione IT)
 (blog feed, NewsAPI,        (categoria,         (generazione,          │  Approva (solo chat autorizzata)
  /ideas, founder_note,       fonti, link)        fact-guard,            ▼
  product_fact)                                   scoring, dup)    editorial_queue (approved)
                                                                         │
                          PublicationPlanner (piani ET, 2–3/giorno) ◄────┘
                                  │ publish_due (ogni 5 min)
                                  ▼
                              Publisher ──► TwitterClient (tweepy) ──► X
```

Principi progettuali ricorrenti (non romperli):
- **Fail-closed ovunque**: dati malformati/ambigui → rifiuto, mai fallback creativi. Molti `type(x) is int` espliciti, validazioni canoniche di timestamp/ID.
- **Idempotenza + claim/lease in SQLite** (claim token per pubblicazione, replenishment, growth digest build, query budget, telegram update).
- **DI esplicita** in `FlexDropinGrowthAgent.__init__` (`_DEPENDENCY_KEYS`); i test iniettano fake.
- **Esito ambiguo ≠ retry**: scrittura X con esito incerto → `unknown`/`publication_unknown`, mai ritentata; riconciliazione manuale.
- **Errori sanitizzati**: log con `error_type=` soltanto, mai token/URL bot/segreti. `TelegramApi` ha filtri sui log di `urllib3.connectionpool`.
- `APPROVAL_REQUIRED` deve essere `true` (altrimenti `validate_config()` fallisce). `DRY_RUN` default `true`.
- Il codice ha commenti/nomi misti IT/EN; test molto estesi (~1300+ test, vedi §11).

## 4. Moduli (`modules/`)

| Modulo | Ruolo |
|---|---|
| `database.py` (~10k righe) | Tutto SQLite: schema + migrazioni additive in `_init_schema`, claim atomici, tutte le query. Classe `Database(db_path='bot_data.db')`. Dataclass `PostDraftPublicationClaim`, `PublicationPlanClaim`, `ReplyGenerationClaim` |
| `twitter_client.py` | Wrapper tweepy: `post_tweet`, `post_thread`, letture (search, followers, following, metrics, user info). Eccezioni `XPublicationError/Unknown/Rejected/Paused`. Ogni chiamata passa da `XApiUsageMeter` (prenotazione costo) |
| `x_api_usage.py` | `XApiUsageMeter`: reserve/complete/fail/unknown, budget mensile opzionale (`X_API_MONTHLY_BUDGET_USD`, 0 = solo telemetria). Costi unitari in micro-USD configurabili |
| `publisher.py` | **Unico confine di scrittura X.** `publish(draft_id)` (legacy per slot) e `publish_plan(plan_id)`. Controlla stato/revisione/pausa/dry_run, claim, media verificato, scrive, persiste esito |
| `publication_queue.py` | `QueueReplenisher` (riserva 14 approvati, max 5 in revisione, cap generazione giornaliero, retry traduzioni) e `PublicationPlanner` (`ensure_day`, `reconcile`, `publish_due`, `simulate_due`) |
| `adaptive_timing.py` | `AdaptiveTimingPolicy`: scelta orari in finestre ET (08:30–10:30, 13:00–15:30, 18:00–20:30), gap ≥4h, apprendimento dopo 30 post maturi (weekday dopo 90), scelta deterministica via digest |
| `publication_cadence.py` | `PublicationCadencePolicy`: 2 post/giorno + 3° post in 3 giorni/settimana (cold start mar/gio/sab; poi dinamico dopo 30 post) |
| `draft_pipeline.py` | Generazione grounded → fact guard → scoring → duplicate gate → draft `pending_approval`. Anche `regenerate`, `edit`, `approve`, `postpone`, `discard`, creazione manuale da sessione Telegram |
| `content_planner.py` | Portfolio categorie: `gym_strategy .35`, `fitness_business_insight .25`, `shareable_fitness .20`, `product_proof .10`, `founder_journey .10`; pesi appresi 0.3–3.0 normalizzati; scelta di 1 fonte (rotazione, esclude fonti in bozze live); link al blog solo entro `MAX_LINKS_PER_WEEK` (1) e cooldown 30gg per articolo |
| `ai_generator.py` | Groq (`openai/gpt-oss-120b`, `reasoning_effort="low"`): `generate_grounded_tweet`, `analyze_claims`, `rewrite_to_limit`, `translate_review_copy`, `draft_founder_reply`, `analyze_image`, `select_best_media`, + legacy (`generate_flexdropin_comment`, `generate_value_reply`, `generate_lead_dm` usati solo da lead_finder) |
| `character.py` + `character.json` | Persona/agenti/mapping categoria→agenti. **character.json è ancora scritto per gym owner** (vedi §12) |
| `fact_guard.py` | Valida claim estratti dal LLM vs fonti: tipi `first_person, number, product_claim, incident, medical, testimonial, named_entity, named_current_event` → tipi di fonte richiesti; numeri devono comparire nelle fonti; scadenza fonti |
| `scoring.py` | `TweetScorer` (LLM, soglia `DRAFT_SCORE_THRESHOLD=70`) + `semantic_similarity` deterministica (soglia duplicati 0.72) |
| `review_translation.py` | Traduzione IT fedele per revisione (URL/hashtag preservati). Mai pubblicata su X |
| `editorial_feed.py` | Client del feed fisso `https://flexdropin.com/api/editorial-feed` (validazione schema, host fisso) |
| `source_refresh.py` / `source_ingestion.py` / `source_validation.py` / `news_fetcher.py` | Refresh separato blog / NewsAPI (allowlist `NEWS_TRUSTED_DOMAINS`, vuota = disabilitata) |
| `growth_discovery.py` | Scoperta account founder (2 query topic + network di seed account), budget `GROWTH_QUERY_BUDGET`≤3, score soglia 75, cache profili 7gg |
| `growth_candidate_schema.py` | Filtri duri `startup_founder_peer_v1`: profilo pubblico, post originale <30gg, bio con identità founder (regex), no spam/crypto/follow-farming, no suppress 30gg |
| `growth_digest.py` | Digest giornaliero Rome: ≤5 account, ≤10 like (quote 4 followed / 3 suggested / 3 founder_conversation), ≤5 unfollow/settimana (non-ricambia, ≥30gg, non pinned) |
| `growth_replies.py` | ≤5 bozze di risposta/giorno (`reply_en` ≤240 char, `post_it`, `reply_it`), vietati URL, @, #, "flexdropin", em/en dash; max 3 generazioni per bozza |
| `analytics.py` | Snapshot follower, sync following, metriche tweet propri (max 20), ricalcolo pesi categoria, report settimanale |
| `telegram_api.py` | Transport Requests limitato per Bot API (allowlist errori, validazione reply_markup, metadata media) |
| `telegram_controller.py` (~3.5k righe) | Auth chat, claim update idempotente, comandi, callback, flussi `/newpost` `/newthread` `/ideas`, media intake, card growth/reply |
| `telegram_media_browser.py` / `telegram_post_browser.py` | Rendering compatto path-free di media e indice post |
| `manual_post_service.py` | Crea bozze approvate dal testo esatto dell'operatore (nessun gate editoriale) |
| `media_processor.py` / `media_store.py` / `media_matcher.py` | Upload sicuro (no-follow, dir privata, flock, hash/identità), analisi vision Groq, ffmpeg per video, matching media↔bozza (soglia 80), quarantena/eliminazione verificata |
| `notifier.py` | Notifiche Telegram (errori, lead) |
| `lead_finder.py` | Lead discovery opzionale (`ENABLE_LEAD_DISCOVERY=false`): da non usare |
| `content_scheduler.py` | Solo helper stagionali (compat) |

## 5. Scheduler (`main.py: register_jobs`)

Timezone scheduler `Europe/Rome` (`BOT_TIMEZONE`); gli slot di pubblicazione sono invece calcolati in `America/New_York`.

| Job id | Quando | Funzione |
|---|---|---|
| `source_refresh` | cron 10:30 Rome | blog FlexDropin + NewsAPI (canali indipendenti) |
| `queue_replenishment` | ogni 30 min | genera bozze fino a riserva 14 approvati / 5 in revisione / cap 5 al giorno Rome |
| `translation_retry` | ogni 30 min | riprova traduzioni IT (max 3) e annuncia card pronte |
| `publication_planning` | ogni 15 min | `PublicationPlanner.reconcile` (crea/riconcilia piani ET) |
| `adaptive_publish` | ogni 5 min | `publish_due` → `Publisher.publish_plan` |
| `growth_digest` | cron 09:00 Rome (`GROWTH_DIGEST_TIME`) | digest + bozze reply (opzionali) → push su Telegram |
| `follower_snapshot` | cron 23:15 | snapshot follower + sync lista following reale |
| `performance_metrics` | cron 23:30 | metriche tweet propri + pesi categoria |
| `weekly_growth_report` | lun 09:00 | report settimanale su Telegram |
| `lead_discovery_*` | solo se `ENABLE_LEAD_DISCOVERY=true` | 10:00, 16:00 (default off) |

`_publish` legacy lancia `RuntimeError("legacy_direct_publication_disabled")` (fail-closed). `create_draft_cycle` / `publish_cycle` (per slot) restano nel file ma **non sono registrati** come job.

## 6. Pipeline contenuto → X

1. **Fonti** (`content_sources`): tipi `evergreen_idea, verified_news, founder_note, owned_blog_article, product_fact`. Feed blog → `owned_blog_article` (NON product fact: sostiene solo testo generico e numeri/entità presenti nel titolo/sommario).
2. **Generazione** (`DraftPipeline.create_for_queue_with_outcome`): planner sceglie categoria+fonte+link; fino a 3 candidati con angoli diversi; rewrite al limite 280; ogni candidato passa **fact guard → scoring (≥70) → duplicate (<0.72)**. Esiti loggati in `draft_evaluations` (`rejected_fact/score/duplicate`, `pending_approval`).
3. **Revisione**: card Telegram con testo EN completo + traduzione IT. Traduzione mancante/stale blocca l'approvazione (policy `required`); per i post manuali è `advisory`.
4. **Approva** (solo callback dalla chat autorizzata) → `approve_queued_draft_atomic` → coda approvata.
5. **Pianificazione**: `publication_plans` (posizioni 1–3 per `local_date` ET, `scheduled_for` UNIQUE). Assegnazione draft↔piano atomica; un draft ha un solo piano attivo (indice unico parziale).
6. **Pubblicazione**: piano dovuto, `status=planned`, non in pausa, entro grace `PUBLICATION_PLAN_GRACE_MINUTES=90` (oltre → `skipped`/`expired`, draft torna `approved`). `DRY_RUN=true` → piano `simulated`, nessuna chiamata X. Live: claim draft+piano, upload media verificato (se presente), `create_tweet`, persistenza; thread: ogni parte salvata in `thread_publication_parts`, parte ambigua → `publication_unknown`.
7. **Pausa globale**: `bot_state.paused` (`/pause`, `/resume`). Attenzione: `_publication_is_paused` = `get_state("paused") != "false"` → **se la chiave manca, è in pausa** (fail-closed).

Stati draft (`post_drafts.status`): `pending_approval, approved, published, expired, discarded, publication_unknown, …`. Stati piano: `open, planned, publishing, published, simulated, skipped, unknown`. Origini draft: `generated`, `manual_operator`, media-suggested, ecc.

Campagne editoriali (manuali, non generate): inserite da script come draft `telegram-manual:<token>` via `ManualPostService`; calendario in `campaigns/*.md` (human-first 09:15 ET + midday + 18:30 ET). Vedi §9.

## 7. Telegram

Comandi (menu registrato in `_register_telegram_commands`): `/posts` (indice ≤8 righe, dettaglio EN+IT, rimuovi/ripristina), `/newpost` (testo esatto → categoria → 0–3 fonti → media opzionale → approvato subito), `/newthread` (2–10 tweet), `/media` (browser verificato: archivia/ripristina/elimina definitivo con doppia conferma solo se mai usato), `/status` (coda, job, stima costi X mese UTC), `/growth`, `/errors`, `/stats`, `/ideas` (aggiunge fonte), `/pause`, `/resume`, `/help`.

Callback growth: `gr:<id>:<rev>` (scheda reply), `gra:p|s|g:<ref>` (Pubblicata / Salta / Rigenera), più azioni digest (`Segnala come seguito`, `Like messo`, `Salta`, `Non pertinente`, `Unfollow fatto`, `Tieni`, pin). Tutte registrano **solo decisioni locali** in SQLite. Callback e update sono idempotenti (`telegram_updates`). Foto/video caricati → registrano media, **non creano bozze**.

Testo inviato sempre `parse_mode=None` (nessun Markdown/HTML da dati utente), limite messaggio, callback ≤64 byte.

## 8. Growth (fitness-first dal 2026-10-19)

- **Target**: segmento principale `fitness` (creator, coach, trainer, atleti, istruttori yoga/pilates, palestre e studi: regex bio `_FITNESS_IDENTITY_PATTERN`); segmento secondario `peer` (founder/indie hacker, `_FOUNDER_IDENTITY_PATTERN`). `audience_segment(profile)` restituisce `"fitness"`, `"peer"` o `None` (fitness vince se la bio ha entrambi). `GROWTH_RELEVANCE_POLICY="fitness_first_v1"`: il cambio di valore invalida le cache candidati vecchie (rivalutate al prossimo run). Filtro duro `no_audience_context` se la bio non è né fitness né founder. Rumore esteso: affiliate, codici sconto, sarms/steroidi/peptidi/trt oltre a crypto, follow-back ecc.
- **Score account**: role_bio fitness 40 / founder 35, topic fit (fitness+startup terms per fitness, solo startup per founder) 25/15, activity 15/8, mercato en 15, follow-back range 10, affinità 5. Soglia 75.
- **Digest account**: ≤5 al giorno, di cui **al massimo 2 founder** (`MAX_FOUNDER_ACCOUNTS`).
- **Seed accounts** (`config.GROWTH_SEED_ACCOUNTS`, hardcoded, non da env): ClassPass, CrossFit, Peloton, Mindbody, Hevy, ProductHunt. Gli handle sono ipotesi non verificate: un handle sbagliato non produce risultati ma non rompe nulla; da rivedere a mano.
- **Query**: 2 query topic in `growth_discovery.DEFAULT_TOPIC_QUERIES` (cosa postano creator/coach: workout, leg day, my clients, class today; e venue: gym owner, studio owner, boutique fitness, drop-in) + 1 query post `fitness_conversation` in `growth_digest.POST_QUERY_PORTFOLIO` (budget 1/giorno).
- **Score post** (`_POST_SIGNALS`): training_content 12, class_or_gym_talk 12, access_pain 12 (drop-in, day pass, membership, travel), poi i vecchi segnali founder (build_in_public 15, launch 12, traction 12, feedback_request 10, fitness_tech 10, founder_struggle 8, question 6).
- **Like**: quote 4 followed_account / 3 suggested_account / 3 `fitness_conversation`; autore cooldown 3gg; followed ≤72h, suggested ≤7gg. `get_following_state()` espone `target` (bio fitness o founder) per filtrare la timeline.
- **Unfollow**: ≥30 giorni, non ricambia, non `pinned`; 5/settimana.
- **Reply drafts**: `AIGenerator.draft_manual_reply` (ex `draft_founder_reply`): Floriano risponde da pari a creator/coach/venue/founder, mai pitch, né link/hashtag/@/emoji/em dash, niente consigli medici. Fonti reply: `fitness_conversation` poi `suggested_account`. I code storici `founder_conversation` restano ammessi nelle allowlist del DB per leggere righe già persistite.
- Tabelle: `growth_candidates`, `growth_profile_claims`, `growth_suggestions` (kind `account|post|reevaluate`, cooldown 30gg), `growth_digest_runs`, `growth_read_claims`, `x_following`, `follower_snapshots(+_runs)`, `growth_reply_drafts`, `growth_follows`.
- Costi X (default): post read 0.005, user read 0.010, owned read 0.001, create 0.015, create **con URL 0.200**, media 0.

## 9. Campagne e contenuti

- `campaigns/flexdropin-editorial-plan-october-2026.md`: piano approvato 25 set–31 ott, ET, 09:15 apertura "human thought" ogni giorno, midday 12:15–14:10 su alcuni giorni, sera 18:30 con prodotto/ricerca/blog/founder; 61.7% human-first; max 3 post linkati in 7 giorni rolling; PH launch 7 ott (6 post PH in totale).
- `campaigns/flexdropin-x-editorial-runbook-…md` (723 righe): testi esatti e verifiche; `campaigns/flexdropin-product-journey-2026-10.md` (draft 65–74, media 39–48); `campaigns/flexdropin-us-gym-owner-2026-10.md`.
- Script una-tantum in `scripts/`: `insert_*_campaign.py`, `update_product_hunt_launch_oct7.py`, `announce_product_hunt_date_change.py`, `reschedule_credit_outage_posts.py`, `reschedule_human_first_campaign.py` (**untracked**: contiene testi/schedule del calendario human-first con `ManualPostService`, verifica idempotente pre-update; non è chiaro se sia stato eseguito in produzione e non è committato), `translate_*_to_english.py`, `add_thread_drafts.py`, `promote_sources_to_drafts.py`, `publish_once.py` (pubblicazione singola controllata), `preflight_production.py`.
- Pattern degli script: backup DB → verifiche (draft approvati/non pubblicati, media riservati, integrity_check) → update idempotente su `publication_plans`. Riutilizzare questi quando serve riprogrammare (es. dopo outage).
- Asset: `media_library/product-journey-2026-09/` e `us-gym-owner-campaign-2026-10/` (PNG) — **gitignored**, esistono solo in locale e sul VPS.

## 10. Config (`config.py`) — vincoli che causano errori all'avvio

- Obbligatorie: `TWITTER_API_KEY/SECRET/ACCESS_TOKEN/ACCESS_TOKEN_SECRET/BEARER_TOKEN`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` (formato `id:token`), `TELEGRAM_CHAT_ID`; `NEWSAPI_KEY` solo se `NEWS_TRUSTED_DOMAINS` non vuota.
- Valori **fissi** per questa release (altro → `ValueError`): `POSTS_PER_DAY=2`, `THIRD_POST_DAYS_PER_WEEK=3`, `GROWTH_SUGGESTION_COOLDOWN_DAYS=30`, `GROWTH_POST_QUERY_BUDGET=1`, `APPROVAL_REQUIRED=true`; `APPROVED_QUEUE_TARGET≥14`, `GROWTH_UNFOLLOW_REVIEW_DAYS≥30`, `GROWTH_QUERY_BUDGET` limitato a 3, `GROWTH_ACCOUNT_SUGGESTION_LIMIT≤5`, `GROWTH_POST_SUGGESTION_LIMIT≤10`. Booleani solo `true`/`false` minuscoli.
- `validate_config()` verifica anche che la config non sia cambiata dopo l'import.
- Modelli Groq: `GROQ_MODEL=openai/gpt-oss-120b`, `GROQ_VISION_MODEL=qwen/qwen3.6-27b` (la lineup Groq cambia spesso: se la generazione o la vision falliscono, controllare prima il nome del modello).
- Variabili legacy ancora in `config.py` ma inutilizzate dal runtime attivo: `SEARCH_TOPICS` (usata da `refresh_sources_cycle` → NewsAPI), `POST_INTERVAL`, `ENGAGEMENT_*`, `HUMAN_MODE_PROBABILITY`, `MAX_FLEXDROPIN_MENTIONS_PER_DAY`, `USER_COMMENT_COOLDOWN_HOURS`, `PERFORMANCE_CYCLE_TIME`, `TWEET_SCORE_THRESHOLD`, `FLEXDROPIN_PROMO`, `CONTENT_SLOTS`, `DRAFT_LEAD_MINUTES`, `PUBLISH_GRACE_SECONDS` (quest'ultime tre servono ai percorsi per slot legacy).

## 11. Test, deploy, operazioni

- **Test**: `venv/bin/python -m pytest -q` (nessuna rete; fake in `tests/fakes.py`, `tests/conftest.py`). Baseline storica nel ledger SDD: 1320 passed (4 set); la suite è cresciuta da allora. Risultato del run di oggi: vedi §14. Controlli extra: `python -m compileall -q main.py config.py modules dashboard`, `git diff --check`.
- **Test chiave**: `test_end_to_end_dry_run.py` (fonte→approvazione→14 riserva→piani→simulazione→riavvio), `test_x_write_safety.py`, `test_publisher.py`, `test_adaptive_publication.py`, `test_telegram_workflows.py`, `test_production_preflight.py`.
- **Deploy** (`deploy.sh`, da eseguire sul VPS come `ubuntu`, senza sudo): rifiuta se ci sono modifiche tracciate locali → backup DB (sqlite backup API) + `.env` in `backups/` (ultimi 10) → `git pull --ff-only` → pip bot+dashboard → `scripts/preflight_production.py --allow-live` → (re)installa unit dashboard → restart `flexdropin-bot` e `flexdropin-dashboard` → controlla assenza di `Traceback` nei log. Stampa il comando di rollback.
- **Passaggio live** (`DRY_RUN=false`) è una fase separata che richiede autorizzazione esplicita (SETUP §9). Non sappiamo da questo repo se sia già stato attivato: l'outage crediti X e i reschedule implicano che **in produzione si pubblica davvero** (verificare, §13).
- **Dashboard** (`dashboard/app.py`, Flask, 127.0.0.1:5050, solo via tunnel SSH `ssh -L 5050:127.0.0.1:5050 ubuntu@80.225.89.115`): pagine overview, leads, engagement, posts, logs (journalctl), media (upload/archivia/elimina/riutilizzabile). Unit systemd `flexdropin-dashboard.service`. Parte è legacy (leads/engagement).
- **Produzione** (memoria `production-vps-access`): host `ubuntu@80.225.89.115`, chiave `ssh-key-2026-07-15.key` nella root del repo (gitignored, **non committare**), repo `/home/ubuntu/ai-x-bot`, unit `flexdropin-bot`, log `~/ai-x-bot/bot.log` (UTC), DB `~/ai-x-bot/bot_data.db`, backup in `~/ai-x-bot/backups/`. **Niente `sqlite3` CLI** sul VPS: usare `venv/bin/python3` + modulo `sqlite3` (`file:bot_data.db?mode=ro`). Leggere `.env` è bloccato dal classifier: chiedere i valori a Floriano.
- Il DB locale `bot_data.db` è praticamente vuoto (solo 8 `target_accounts`): **non rappresenta la produzione**.

## 12. Incoerenze e debito tecnico noti

1. **README.md e SETUP.md obsoleti**: descrivono ancora il digest "palestre da seguire / non-palestra da unfolloware", `GROWTH_POST_QUERY_BUDGET`, Reply Copilot come dismesso, etc. Il codice ora è founder-oriented e non classifica più le palestre. `VPS_ROLLOUT.md` riporta ancora `GROWTH_POST_QUERY_BUDGET=2` e `GROWTH_UNFOLLOW_REVIEW_DAYS=14`, che oggi **non validano** (budget deve essere 1, review ≥30); e fa riferimento a HEAD `e51ac9c`.
2. **`character.json` gym-centrico** (bio "for gym and studio operators", lore "100 gyms challenge", topic palestre; `categoryAgents` include categorie legacy IT). Coerente con le categorie post (che parlano di gym), ma NON con il target growth founder. Decisione da prendere: va riallineato (contenuti X verso founder/build-in-public?) oppure no (contenuti restano per gym owner/utenti, solo il growth cerca founder).
3. **Audience contenuti vs growth**: ora allineate (fitness). `character.json` resta gym-centrico, coerente con il nuovo target; i founder sono solo secondari.
4. **`.env.example`** contiene `GROWTH_SEED_ACCOUNTS=`, `GROWTH_DIGEST_LIMIT`, `ENABLE_REPLY_COPILOT`, `REPLY_COPILOT_*`, che non hanno più effetto (seed hardcoded in `config.py`; reply copilot rimosso). Da pulire (preferenza utente: cancellare codice/config morti).
5. **Codice morto/legacy probabile**: `lead_finder.py` + job lead (default off), `content_scheduler.py`, `generate_flexdropin_comment/value_reply/lead_dm` in `ai_generator`, tabelle `leads`, `seen_tweets`, `target_accounts`, `spam_guard`, `ideas`, `posted_tweets`, `reply_suggestions` (residua nel DB locale), pagine leads/engagement della dashboard, `create_draft_cycle`/`publish_cycle` per slot, variabili legacy di §10, e la tabella `growth_follows`. Verificare uso reale prima di rimuovere.
6. `main.py: growth_digest_cycle` chiama `growth_replies.build` e poi `push_growth_digest`, che richiama `build` ancora (idempotente) — duplicazione innocua.
7. Test: `test_growth_digest.py::test_hard_crash_before_commit_recovers_only_after_lease_expiry` e due test in `test_telegram_workflows.py` (`test_posts_renders_complete_safe_draft_card_and_latest_published`, `test_published_preview_requires_exact_tweet_media_binding`) compaiono deselezionati nei permessi locali (`.claude/settings.local.json`), residuo di vecchie sessioni: oggi passano tutti (1411 passed). Possibile flakiness solo sotto carico.
8. `bot.log` locale è del 24 ago e il DB `.bak-20260827` è obsoleto: file locali di scarto (gitignored).
9. Dipendenze: `tweepy==4.14.0`, `groq>=0.11.0`, `python-dotenv==1.0.0`, `requests==2.31.0`, `apscheduler==3.10.4`, `Flask`. Warning Tweepy di deprecazione noto. `ffmpeg` richiesto per i video.

## 13. Come ripartire in una nuova sessione

Checklist rapida:
1. `git status && git log --oneline -5` — verificare che HEAD sia ancora `e7e0293` e che `scripts/reschedule_human_first_campaign.py` sia ancora untracked.
2. Chiedere/permettere accesso di **sola lettura** al VPS. Comandi suggeriti (da eseguire a mano o da autorizzare esplicitamente):
   ```bash
   ssh -i ssh-key-2026-07-15.key ubuntu@80.225.89.115 'cd ~/ai-x-bot && git log --oneline -3 && systemctl is-active flexdropin-bot flexdropin-dashboard && tail -40 bot.log'
   ```
   e per il DB (read-only, `venv/bin/python3` con `sqlite3`): conteggi `post_drafts` per `status`, `publication_plans` per `status` e prossimi `planned`, ultimi `error_events`, `x_api_usage_events` del mese, ultimi `follower_snapshots`, `growth_suggestions`/`growth_reply_drafts` di oggi, `bot_state` (chiave `paused`).
3. Chiedere a Floriano il valore di `DRY_RUN` in produzione (il `.env` non è leggibile).
4. Prima di toccare `config.py`, `Publisher`, `TwitterClient` o `database.py`: leggere i test omologhi e rieseguire la suite. Non aggiungere mai chiamate X di engagement; non allentare i controlli fail-closed.
5. Per riprogrammare post dopo outage/cambi data: copiare il pattern di `scripts/reschedule_credit_outage_posts.py` e `announce_product_hunt_date_change.py` (backup → verifica → update idempotente).
6. Per cambi alla strategia growth: restare fitness-first (§8), non reintrodurre il vecchio classificatore palestre.

## 14. Esito test odierno

`venv/bin/python -m pytest -q` su HEAD `e7e0293`: **1411 passed, 1 warning** (`imghdr` deprecato) in ~22 s. Nessun test fallito, inclusi quelli citati al punto 7 di §12.
