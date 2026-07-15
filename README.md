# 🤖 AI X Bot - Growth Agent v3 per FlexDropin

Un bot automatico per X (Twitter) che genera e pubblica contenuti usando AI (Groq), fetcha notizie (NewsAPI) e gestisce l'engagement.

## 🆕 Novità v3 (Growth Agent)

Il bot è passato da "pubblica e basta" a vero agente di crescita:

- **Memoria a lungo termine** (`modules/database.py`, SQLite) - non ripete argomenti recenti
- **Palinsesto settimanale + calendario eventi/stagionale** (`modules/content_scheduler.py`)
- **Scoring pre-pubblicazione 0-40** con rigenerazione automatica (`modules/scoring.py`)
- **Multi-agente + Editor** (Business/Fitness/Founder/Copywriter/Community) (`modules/ai_generator.py`)
- **Persona founder** ("build in public") invece di "social media manager"
- **Human mode**, thread generator, varianti A/B
- **Opportunity Detector / CRM lead** a costo controllato (`modules/lead_finder.py`)
- **Engagement mirato** su una lista curata di account con scoring influencer (`modules/engagement.py`)
- **Performance analytics / auto-learning** sulle categorie che funzionano meglio (`modules/analytics.py`)
- **Regole anti-spam** esplicite e **gestione dei costi X API 2026** (pay-per-use: letture ~$0.005,
  post ~$0.015, post con link ~$0.20) tramite cicli a orario fisso invece di intervalli continui

Configura i nuovi orari/soglie in `.env` (vedi `.env.example`) e la lista `TARGET_ACCOUNTS`
per abilitare l'engagement mirato sugli account che ti interessano davvero.

⚠️ **Nota modello Groq**: `mixtral-8x7b-32768` è stato rimosso da Groq. Il default è
ora `openai/gpt-oss-120b` (configurabile via `GROQ_MODEL` in `.env`).

## 🌍 Mercato: X in inglese, per il mercato internazionale

Su decisione di Floriano, questo bot X è dedicato al **mercato internazionale**
(gestori di palestre/boutique studio fuori dall'Italia): tutti i contenuti
generati (tweet, thread, commenti, lead scoring) sono ora in **inglese**.
Il mercato italiano resta presidiato via Instagram e visite di persona,
fuori da questo bot. Questo è coerente con l'architettura reale di FlexDropin,
che gestisce IVA condizionale e UI IT/EN in base al paese della palestra.

Se in futuro vorrai riportare anche l'italiano su X (es. per gestori IT che
usano anche X), la persona/i prompt sono centralizzati in
`modules/ai_generator.py` (`FOUNDER_PERSONA`, `AGENTS`) e si possono
duplicare per lingua senza toccare il resto dell'architettura.

## ✨ Caratteristiche

- 📰 **Fetching Notizie** - Recupera articoli da NewsAPI
- 🧠 **Generazione AI** - Crea tweet e commenti con Groq (LLM avanzato)
- 🐦 **Posting Automatico** - Pubblica tweet su X automaticamente
- 💬 **Engagement Automatico** - Commenta, mette like e segue utenti
- ⏰ **Scheduler** - Esecuzione automatica a intervalli configurabili
- 📊 **Logging** - Monitora tutte le azioni in `bot.log`

## 🚀 Quick Start

### 1. Prerequisiti
- Python 3.8+
- Account X (Twitter) con accesso API
- Account Groq (gratuito)
- Account NewsAPI (gratuito)

### 2. Ottieni le API Keys
Segui la guida in **SETUP.md** per ottenere tutte le chiavi.

### 3. Installazione
```bash
git clone https://github.com/FloDev23/ai-x-bot.git
cd ai-x-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configurazione
```bash
cp .env.example .env
# Edita .env con le tue API keys
```

### 5. Avvia il Bot
```bash
python main.py
```

## 📖 Documentazione

Vedi **SETUP.md** per:
- Guida dettagliata setup API keys
- Installazione e configurazione
- Troubleshooting
- Opzioni di deploy 24/7

## 🛠️ Configurazione

Modifica `.env` per personalizzare:

```env
# Topic da seguire
SEARCH_TOPICS=AI,Machine Learning,Crypto,Tech

# Intervallo tra post (secondi)
POST_INTERVAL=3600

# Numero massimo di articoli da processare
MAX_SEARCH_RESULTS=5

# Soglia minima di engagement per likeare
LIKE_ENGAGEMENT_THRESHOLD=50
```

## 📁 Struttura Progetto

```
ai-x-bot/
├── main.py                    # Entry point del bot
├── config.py                  # Configurazione centralizzata
├── requirements.txt           # Dipendenze Python
├── .env.example              # Template .env
├── SETUP.md                  # Guida completa setup
├── README.md                 # Questo file
└── modules/
    ├── __init__.py
    ├── news_fetcher.py       # Fetching notizie
    ├── ai_generator.py       # Generazione AI
    ├── twitter_client.py     # Client X/Twitter
    └── engagement.py         # Engagement manager
```

## 🔄 Come Funziona

### Ciclo di Posting
1. Fetcha notizie da NewsAPI su topic configurati
2. Genera tweet con AI (Groq)
3. Pubblica su X

### Ciclo di Engagement
1. Cerca tweet sui topic
2. Commenta tweet con engagement alto
3. Mette like e segue utenti

## 💰 Costi

**100% GRATUITO!**

| Servizio | Prezzo | Limite |
|----------|--------|--------|
| Groq API | FREE | 14,400 token/min |
| NewsAPI | FREE | 100 req/giorno |
| X API | FREE | Basic tier |
| **TOTALE** | **$0** | ✅ |

## 🌐 Deploy 24/7

### Railway (Gratuito)
1. Vai a https://railway.app
2. Connetti il repo GitHub
3. Aggiungi variabili d'ambiente
4. Deploy!

### Render (Gratuito)
1. Vai a https://render.com
2. Crea "Background Worker"
3. Connetti repo GitHub
4. Deploy!

## 📊 Monitoraggio

Visualizza i log in tempo reale:
```bash
tail -f bot.log
```

## 🐛 Troubleshooting

### "Variabili d'ambiente mancanti"
✅ Controlla il file `.env` e il file `.env.example`

### "Errore di autenticazione X"
✅ Verifica che i token siano corretti (senza spazi)

### "Groq API error"
✅ Controlla la connessione internet e la validità della chiave

## 🤝 Contributi

Contributi sono benvenuti! Feel free to:
- Segnalare bug
- Proporre nuove feature
- Fare pull request

## 📝 Licenza

MIT License - vedi LICENSE file

## 🚀 Prossimi Step

- [ ] Aggiungere database per tracking tweet
- [ ] Aggiungi supporto per immagini
- [ ] Migliora AI prompts
- [ ] Aggiungi analytics dashboard
- [ ] Supporto per video

## 📞 Supporto

Hai domande? Apri un issue su GitHub!

---

**Fatto con ❤️ da FloDev23**
