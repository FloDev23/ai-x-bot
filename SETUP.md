# 🚀 Setup Completo AI X Bot

## Step 1: Prerequisiti

- Python 3.8 o superiore
- Account X (Twitter)
- Account Groq (gratuito)
- Account NewsAPI (gratuito)

## Step 2: Ottenere le API Keys

### 🐦 X (Twitter) API Keys

1. Vai su https://developer.twitter.com
2. Clicca "Create Project"
3. Rispondi alle domande iniziali
4. Una volta nel dashboard, clicca "Keys and tokens"
5. Genera/copia i seguenti valori:
   - **API Key** → `TWITTER_API_KEY`
   - **API Secret Key** → `TWITTER_API_SECRET`
   - **Bearer Token** → `TWITTER_BEARER_TOKEN`
   - **Access Token** (clicca "Generate") → `TWITTER_ACCESS_TOKEN`
   - **Access Token Secret** (generato insieme) → `TWITTER_ACCESS_TOKEN_SECRET`

### 🧠 Groq API Key (GRATIS)

1. Vai su https://console.groq.com
2. Clicca "Sign Up" (supporta Google/GitHub)
3. Verifica email
4. Clicca "Create API Key"
5. Copia la chiave → `GROQ_API_KEY`

**Limite gratuito:** 14,400 token/minuto (illimitato per testing)

### 📰 NewsAPI Key (GRATIS)

1. Vai su https://newsapi.org
2. Clicca "Get API Key"
3. Registrati e conferma email
4. Copia la chiave API → `NEWSAPI_KEY`

**Limite gratuito:** 100 richieste/giorno

## Step 3: Installazione Locale

```bash
# Clone il repository
git clone https://github.com/FloDev23/ai-x-bot.git
cd ai-x-bot

# Crea environment virtuale
python -m venv venv

# Attiva environment
# Su Windows:
venv\Scripts\activate
# Su macOS/Linux:
source venv/bin/activate

# Installa dipendenze
pip install -r requirements.txt
```

## Step 4: Configurazione

```bash
# Copia il file di esempio
cp .env.example .env

# Modifica .env con i tuoi valori
# Usa il tuo editor preferito (nano, vim, VS Code, etc.)
nano .env
```

**Esempio .env completato:**
```
TWITTER_API_KEY=tua_api_key
TWITTER_API_SECRET=tua_api_secret
TWITTER_ACCESS_TOKEN=tuo_access_token
TWITTER_ACCESS_TOKEN_SECRET=tuo_token_secret
TWITTER_BEARER_TOKEN=tuo_bearer_token

GROQ_API_KEY=tua_groq_key

NEWSAPI_KEY=tua_newsapi_key

SEARCH_TOPICS=AI,Machine Learning,Python,Crypto,Tech
POST_INTERVAL=3600
```

## Step 5: Test Configurazione

```bash
# Valida che tutte le chiavi siano presenti
python -c "from config import validate_config; validate_config()"

# Dovresti vedere: "✅ Configurazione validata con successo"
```

## Step 6: Avvio Bot

```bash
# Avvia il bot
python main.py

# Dovresti vedere:
# 🤖 Inizializzazione AI X Bot...
# ✅ Bot inizializzato con successo
# 🚀 Avvio AI X Bot...
# ▶️ Esecuzione iniziale...
# ✅ Bot avviato e in esecuzione. Premi Ctrl+C per fermare.
```

## 📊 Monitoraggio

Il bot crea un file `bot.log` che contiene:
- Notizie trovate
- Tweet generati
- Errori e problemi
- Engagement effettuato

```bash
# Visualizza i log in tempo reale
tail -f bot.log

# Su Windows:
Get-Content bot.log -Wait
```

## 🔧 Troubleshooting

### ❌ "Mancano variabili d'ambiente"
**Soluzione:** Controlla che il file `.env` sia nella directory corretta con tutti i campi compilati

### ❌ "Errore di autenticazione X"
**Soluzione:**
- Verifica che i token siano corretti (non abbiano spazi all'inizio/fine)
- Accedi a https://developer.twitter.com e controlla che l'app sia "ACTIVE"
- Rigenera i token se necessario

### ❌ "Groq API error"
**Soluzione:**
- Verifica che la chiave Groq sia corretta
- Controlla la connessione internet
- Accedi a https://console.groq.com per verificare l'API key

### ❌ "NewsAPI key invalid"
**Soluzione:**
- Genera una nuova chiave su https://newsapi.org
- Verifica che sia la chiave API (non l'URL)

## 🎛️ Customizzazione

### Cambia i topic da seguire
Modifica `.env`:
```
SEARCH_TOPICS=Calcio,Tennis,Politica,Economia
```

### Cambia frequenza di posting
Modifica `.env`:
```
POST_INTERVAL=1800  # Ogni 30 minuti invece di 1 ora
```

### Cambia modello AI
Modifica `config.py` (riga 13):
```python
GROQ_MODEL = 'llama-2-70b-chat'  # Più potente ma più lento
```

## 🌐 Deploy su Server (24/7)

### Opzione 1: Railway (GRATUITO)

1. Vai su https://railway.app
2. Clicca "Create New Project"
3. Seleziona "GitHub Repo"
4. Collega il tuo repo GitHub (FloDev23/ai-x-bot)
5. Clicca "Deploy"
6. Aggiungi le variabili d'ambiente:
   - Vai a "Variables"
   - Aggiungi tutte le chiavi dal tuo `.env`
7. Il bot partirà automaticamente 24/7

### Opzione 2: Render (GRATIS)

1. Vai su https://render.com
2. Clicca "New +"
3. Seleziona "Background Worker"
4. Collega il tuo repository GitHub
5. Configura:
   - Name: `ai-x-bot`
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
6. Aggiungi le variabili d'ambiente
7. Deploy!

### Opzione 3: Server Personale VPS

```bash
# Installa screen per sessioni persistenti
sudo apt-get install screen

# Avvia il bot in background
screen -S ai-x-bot
python main.py

# Detach: Ctrl+A D
# Reattach: screen -r ai-x-bot
```

## 💰 Monitoraggio Costi

| Servizio | Costo |
|----------|-------|
| Groq API | FREE (14,400 token/min) |
| NewsAPI | FREE (100 req/giorno) |
| X API | FREE (basic tier) |
| Hosting | FREE (Railway/Render) |
| **TOTALE** | **$0 al mese** ✅ |

## ✅ Checklist Finale

- [ ] Ottenute tutte e 7 le chiavi API
- [ ] Installate le dipendenze (`pip install -r requirements.txt`)
- [ ] Configurato il file `.env`
- [ ] Validata la configurazione
- [ ] Testato il bot localmente
- [ ] Deployato (opzionale)
- [ ] Bot in esecuzione 24/7

## 🆘 Supporto

Se hai problemi:

1. Controlla i log: `tail -f bot.log`
2. Verifica le API keys nel file `.env`
3. Leggi la documentazione ufficiale:
   - https://developer.twitter.com/docs
   - https://console.groq.com/docs
   - https://newsapi.org/docs

---

**Buon bot! 🚀**
