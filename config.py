import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ========== Twitter/X API Keys ==========
TWITTER_API_KEY = os.getenv('TWITTER_API_KEY', '')
TWITTER_API_SECRET = os.getenv('TWITTER_API_SECRET', '')
TWITTER_ACCESS_TOKEN = os.getenv('TWITTER_ACCESS_TOKEN', '')
TWITTER_ACCESS_TOKEN_SECRET = os.getenv('TWITTER_ACCESS_TOKEN_SECRET', '')
TWITTER_BEARER_TOKEN = os.getenv('TWITTER_BEARER_TOKEN', '')

# ========== Groq API ==========
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
# NOTA: mixtral-8x7b-32768 è deprecato/rimosso da Groq. Modello aggiornato
# (configurabile via env GROQ_MODEL se Groq cambia ancora la lineup):
GROQ_MODEL = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')

# ========== NewsAPI ==========
NEWSAPI_KEY = os.getenv('NEWSAPI_KEY', '')
NEWSAPI_BASE_URL = 'https://newsapi.org/v2'

# ========== FlexDropin Configuration ==========
FLEXDROPIN_PROMO = True  # Abilita promozione FlexDropin
FLEXDROPIN_PLAY_STORE = 'https://play.google.com/store/apps/details?id=com.mpetaccia.flexdropin'
FLEXDROPIN_APP_STORE = 'https://apps.apple.com/it/app/flexdropin/id6758290879'
FLEXDROPIN_WEBSITE = 'https://flexdropin.com'

# ========== Bot Configuration (v1, mantenute per retrocompatibilità) ==========
SEARCH_TOPICS = os.getenv('SEARCH_TOPICS', 'CrossFit Games,fitness drop-in,lezioni fitness,palestra,yoga,running,allenamento,workout,training,CrossFit,functional training,bootcamp,pilates,CrossFit 2024').split(',')
POST_INTERVAL = int(os.getenv('POST_INTERVAL', '3600'))
MAX_SEARCH_RESULTS = int(os.getenv('MAX_SEARCH_RESULTS', '5'))
ENGAGEMENT_CHECK_INTERVAL = int(os.getenv('ENGAGEMENT_CHECK_INTERVAL', '1800'))
LIKE_ENGAGEMENT_THRESHOLD = int(os.getenv('LIKE_ENGAGEMENT_THRESHOLD', '50'))
MAX_COMMENTS_PER_SESSION = int(os.getenv('MAX_COMMENTS_PER_SESSION', '3'))

# ========== Growth Agent v3: scheduling e frequenze cicli ==========
# NOTA COSTI (X API 2026, pay-per-use): letture ~$0.005, post ~$0.015,
# post con link ~$0.20. Gli intervalli qui sotto sono pensati per contenere
# il costo mensile, non per massimizzare la frequenza di pubblicazione.
DAILY_POST_TIMES = os.getenv('DAILY_POST_TIMES', '09:00,14:00,19:00').split(',')  # 3 post/giorno
OPPORTUNITY_CYCLE_TIMES = os.getenv('OPPORTUNITY_CYCLE_TIMES', '10:00,16:00').split(',')  # 2 ricerche lead/giorno
TARGETED_ENGAGEMENT_TIMES = os.getenv('TARGETED_ENGAGEMENT_TIMES', '11:00,18:00').split(',')  # 2 cicli engagement/giorno
PERFORMANCE_CYCLE_TIME = os.getenv('PERFORMANCE_CYCLE_TIME', '23:00')  # 1 volta/giorno, owned reads economici
BUILD_IN_PUBLIC_DAY = os.getenv('BUILD_IN_PUBLIC_DAY', 'friday')  # Venerdì, punto 6

# ========== Scoring tweet (punto 3) ==========
TWEET_SCORE_THRESHOLD = int(os.getenv('TWEET_SCORE_THRESHOLD', '24'))  # su 40 (60%)
MAX_REGENERATION_ATTEMPTS = int(os.getenv('MAX_REGENERATION_ATTEMPTS', '2'))

# ========== Regole anti-spam (punto 9) ==========
MAX_FLEXDROPIN_MENTIONS_PER_DAY = int(os.getenv('MAX_FLEXDROPIN_MENTIONS_PER_DAY', '2'))
MAX_LINKS_PER_WEEK = int(os.getenv('MAX_LINKS_PER_WEEK', '3'))
USER_COMMENT_COOLDOWN_HOURS = int(os.getenv('USER_COMMENT_COOLDOWN_HOURS', '24'))

# ========== Account target curati (punto 7) ==========
# Popolare con username reali (senza @) di proprietari palestre, coach,
# founder fitness, influencer di settore che si vogliono seguire/coinvolgere.
TARGET_ACCOUNTS = [a.strip() for a in os.getenv('TARGET_ACCOUNTS', '').split(',') if a.strip()]

# ========== Human mode (punto 5) ==========
# Probabilità (0-1) che, invece di un post da palinsesto, venga pubblicato
# un post "umano" informale, per rendere l'account meno robotico.
HUMAN_MODE_PROBABILITY = float(os.getenv('HUMAN_MODE_PROBABILITY', '0.15'))

# ========== Debug Mode ==========
DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

def validate_config():
    """Valida la configurazione al startup"""
    required_keys = [
        'TWITTER_API_KEY',
        'TWITTER_API_SECRET',
        'TWITTER_ACCESS_TOKEN',
        'TWITTER_ACCESS_TOKEN_SECRET',
        'TWITTER_BEARER_TOKEN',
        'GROQ_API_KEY',
        'NEWSAPI_KEY'
    ]
    
    missing_keys = [key for key in required_keys if not os.getenv(key)]
    
    if missing_keys:
        raise ValueError(f"❌ Variabili d'ambiente mancanti: {', '.join(missing_keys)}")
    
    print("✅ Configurazione validata con successo!")
