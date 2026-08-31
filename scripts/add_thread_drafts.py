#!/usr/bin/env python3
"""Insert the 3 operator-authored threads as pending_approval post_drafts."""
import json
import secrets
import sqlite3
from datetime import datetime, timezone

DB = "/home/ubuntu/ai-x-bot/bot_data.db"
NOW = datetime.now(timezone.utc).isoformat()

THREADS = [
    {
        "title": "Drop-in o abbonamento?",
        "category": "gym_strategy",
        "tweets": [
            "Abbonamento mensile o drop-in?\n\nNon è una questione di preferenza. È una questione di vita.\n\nEcco quando conviene davvero uno dei due 🧵",
            "L'abbonamento mensile ha senso se:\n\n→ vai in palestra almeno 3 volte a settimana\n→ hai orari fissi\n→ non viaggi mai\n→ non ti stanchi mai della stessa routine\n\nSe tutte e 4 le condizioni sono vere, l'abbonamento è efficiente.",
            "Il problema? Per la maggior parte delle persone nessuna delle 4 condizioni è stabile.\n\nRiunioni che cambiano. Figli. Trasferte. Settimane caotiche.\n\nE il mese scorre comunque.",
            "Il drop-in ha senso se:\n\n→ viaggi spesso\n→ hai orari variabili\n→ vuoi provare discipline diverse\n→ non vuoi sentirti in colpa quando salti\n\nPaghi solo quello che usi. Niente sensi di colpa.",
            "La verità scomoda: molti abbonamenti sono una forma di ottimismo.\n\n\"Questo mese ci vado di più.\"\n\nIl drop-in è realismo.\n\n\"Ci vado quando posso, pago solo allora.\"",
            "FlexDropin nasce esattamente da questo: il fitness dovrebbe adattarsi alla vita reale, non il contrario.\n\nPrenoти una lezione. Paghi quella. Fine.\n\nSenza contratti, senza sensi di colpa.\n\nflexdropin.com",
        ],
    },
    {
        "title": "Posti vuoti e ricavi",
        "category": "gym_strategy",
        "tweets": [
            "Ogni giorno nelle palestre e negli studi italiani migliaia di posti rimangono vuoti.\n\nNon perché la gente non vuole allenarsi.\n\nPerché il sistema di prenotazione è rotto 🧵",
            "Un posto vuoto in una lezione non è solo uno spazio libero.\n\nÈ un posto che non tornerà.\n\nQuando la lezione inizia, quel ricavo è perso per sempre.",
            "Il processo classico per un drop-in:\n\n→ DM su Instagram\n→ aspetta risposta\n→ chiedi il prezzo\n→ vai allo sportello\n→ paghi in contanti\n\nÈ il 2026 e stiamo ancora prenotando una lezione fitness così.",
            "Il risultato? Molte palestre rinunciano ai drop-in.\n\nTroppa gestione. Troppa confusione.\n\nE quei posti rimangono vuoti.",
            "Con una prenotazione digitale:\n\n→ il cliente prenota in 30 secondi\n→ paga online\n→ riceve conferma automatica\n→ tu non tocchi niente\n\nLo stesso posto vale ricavi reali invece di zero.",
            "FlexDropin automatizza questo flusso per palestre e studi.\n\nAttivazione gratuita. Commissione solo sulle prenotazioni ricevute.\n\nSe non prenota nessuno, non paghi niente.\n\nflexdropin.com",
        ],
    },
    {
        "title": "La storia di FlexDropin",
        "category": "founder_journey",
        "tweets": [
            "Perché ho costruito FlexDropin.\n\nUna storia che inizia con una lezione di CrossFit e un DM rimasto senza risposta 🧵",
            "Ero in trasferta a Milano.\n\nAvevo un'ora libera. Volevo allenarmi.\n\nHo cercato una box CrossFit vicino a me, ho scritto su Instagram per chiedere se accettavano drop-in.\n\nNessuna risposta.",
            "Ho provato con un altro studio. Risposta dopo 3 ore: \"Sì, ma solo in contanti. Vieni direttamente.\"\n\nHo cercato un bancomat. Il bancomat era fuori servizio.\n\nNon mi sono allenato.",
            "Non era un problema di voglia. Era un problema di sistema.\n\nPrenotare un hotel: 30 secondi.\nPrenotare un volo: 2 minuti.\nPrenotare una lezione fitness: un'odissea.",
            "Ho iniziato a parlare con gestori di palestre.\n\nTutti dicevano la stessa cosa: \"I drop-in ci interessano, ma la gestione è un casino.\"\n\nDM, WhatsApp, cassa, conferme manuali. Per ogni singola prenotazione.",
            "FlexDropin è la risposta a entrambi i problemi.\n\nPer chi si allena: prenota in 30 secondi, paghi solo quello che fai.\nPer chi gestisce: nessun DM, nessun contante, prenotazioni automatiche.\n\nIl fitness che funziona per tutti.\n\nflexdropin.com",
        ],
    },
]

SCORE_JSON = json.dumps(
    {"total": 75, "authority": "operator"},
    allow_nan=False, sort_keys=True, separators=(",", ":"),
)
SOURCE_IDS_JSON = "[]"


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    added = 0
    skipped = 0
    for thread in THREADS:
        title = thread["title"]
        tweets = thread["tweets"]
        category = thread["category"]
        first_tweet = tweets[0]
        thread_tweets_json = json.dumps(tweets, ensure_ascii=False, separators=(",", ":"))
        pub_key = "thread-operator:" + secrets.token_urlsafe(12)

        existing = conn.execute(
            "SELECT id FROM post_drafts WHERE thread_tweets_json = ?",
            (thread_tweets_json,),
        ).fetchone()
        if existing:
            print(f"  skip (exists): {title}")
            skipped += 1
            continue

        cursor = conn.execute(
            """INSERT INTO post_drafts
               (publication_key, text, category, source_ids_json, score_json,
                intended_slot, status, origin, thread_tweets_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending_approval', 'manual_operator', ?, ?, ?)""",
            (
                pub_key,
                first_tweet,
                category,
                SOURCE_IDS_JSON,
                SCORE_JSON,
                NOW,
                thread_tweets_json,
                NOW,
                NOW,
            ),
        )
        draft_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO editorial_queue
               (draft_id, translation_status, translation_policy, created_at, updated_at)
               VALUES (?, 'pending', 'advisory', ?, ?)""",
            (draft_id, NOW, NOW),
        )
        print(f"  added (thread #{draft_id}): {title}")
        added += 1

    conn.commit()
    conn.close()
    print(f"\nDone: {added} added, {skipped} skipped.")


if __name__ == "__main__":
    main()
