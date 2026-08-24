import os
import re
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

# ========== Telegram Notifier ==========
# Notifiche in tempo reale: lead trovati (con bozza di commento/DM pronta),
# riepilogo azioni dell'engagement mirato (like/follow/commento) ed errori.
# Se lasciate vuote, il notifier resta disattivato senza rompere il bot.
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
# Punteggio minimo (0-100) di un lead per ricevere la notifica Telegram con
# bozza di commento/DM. Sotto questa soglia il lead viene comunque salvato
# nel CRM locale (tabella leads) ma non genera notifica, per non spammarti
# di lead poco rilevanti e non sprecare chiamate Groq per la bozza.
LEAD_NOTIFY_MIN_SCORE = int(os.getenv('LEAD_NOTIFY_MIN_SCORE', '40'))

# ========== Libreria Media (foto/video reali per i post) ==========
# Cartella dove vengono salvati i file caricati dalla dashboard.
# Percorso assoluto calcolato dalla root del progetto, non relativo, per
# evitare lo stesso problema di percorso avuto con bot_data.db.
MEDIA_LIBRARY_DIR = os.getenv(
    'MEDIA_LIBRARY_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media_library')
)
# Modello Groq con supporto vision per analizzare foto/frame video.
# ATTENZIONE: la lineup multimodale di Groq cambia spesso (i modelli vision
# precedenti sono già stati deprecati più volte nel 2026). Se questo modello
# smette di funzionare, controlla i modelli vision disponibili su
# https://console.groq.com/docs/vision e aggiorna GROQ_VISION_MODEL nel .env.
GROQ_VISION_MODEL = os.getenv('GROQ_VISION_MODEL', 'qwen/qwen3.6-27b')
TELEGRAM_MAX_IMAGE_BYTES = int(os.getenv(
    'TELEGRAM_MAX_IMAGE_BYTES', str(10 * 1024 * 1024)
))
TELEGRAM_MAX_VIDEO_BYTES = int(os.getenv(
    'TELEGRAM_MAX_VIDEO_BYTES', str(50 * 1024 * 1024)
))
MEDIA_MATCH_THRESHOLD = int(os.getenv('MEDIA_MATCH_THRESHOLD', '80'))
MEDIA_MATCH_REASON_MAX_CHARS = 500

# ========== Approval-only publishing rollout ==========
def _strict_boolean_env(name, default):
    """Parse a canonical lower-case boolean without weakening safety."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default, True
    if raw_value == "true":
        return True, True
    if raw_value == "false":
        return False, True
    return default, False


BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Rome")
CONTENT_SLOTS = [value.strip() for value in os.getenv(
    "CONTENT_SLOTS", "14:00,20:00"
).split(",") if value.strip()]
DRAFT_LEAD_MINUTES = int(os.getenv("DRAFT_LEAD_MINUTES", "120"))
PUBLISH_GRACE_SECONDS = int(os.getenv("PUBLISH_GRACE_SECONDS", "300"))
APPROVAL_REQUIRED, _APPROVAL_REQUIRED_VALID = _strict_boolean_env(
    "APPROVAL_REQUIRED", True,
)
DRY_RUN, _DRY_RUN_VALID = _strict_boolean_env("DRY_RUN", True)
DRAFT_SCORE_THRESHOLD = int(os.getenv("DRAFT_SCORE_THRESHOLD", "70"))
SEMANTIC_DUPLICATE_THRESHOLD = float(os.getenv("SEMANTIC_DUPLICATE_THRESHOLD", "0.72"))
MAX_LINKS_PER_WEEK = int(os.getenv("MAX_LINKS_PER_WEEK", "1"))
ENABLE_LEAD_DISCOVERY = os.getenv("ENABLE_LEAD_DISCOVERY", "false").lower() == "true"


def _positive_int_env(name, default, maximum=None):
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, maximum) if maximum is not None else value


# ========== Read-only X growth discovery ==========
GROWTH_SCORE_THRESHOLD = _positive_int_env("GROWTH_SCORE_THRESHOLD", 75)
GROWTH_QUERY_BUDGET = _positive_int_env("GROWTH_QUERY_BUDGET", 3, maximum=3)
GROWTH_NEW_PROFILE_BUDGET = _positive_int_env("GROWTH_NEW_PROFILE_BUDGET", 25)
GROWTH_PROFILE_CACHE_DAYS = _positive_int_env("GROWTH_PROFILE_CACHE_DAYS", 7)
GROWTH_DIGEST_LIMIT = _positive_int_env("GROWTH_DIGEST_LIMIT", 5)
GROWTH_SEED_ACCOUNTS = tuple(
    value.strip().lstrip("@")
    for value in os.getenv("GROWTH_SEED_ACCOUNTS", "").split(",")
    if value.strip().lstrip("@")
)

# ========== NewsAPI ==========
NEWSAPI_KEY = os.getenv('NEWSAPI_KEY', '')
NEWSAPI_BASE_URL = 'https://newsapi.org/v2'
NEWS_TRUSTED_DOMAINS = {
    domain.strip().lower().rstrip('.')
    for domain in os.getenv('NEWS_TRUSTED_DOMAINS', '').split(',')
    if domain.strip()
}

# ========== FlexDropin Configuration ==========
FLEXDROPIN_PROMO = True  # Abilita promozione FlexDropin
FLEXDROPIN_PLAY_STORE = 'https://play.google.com/store/apps/details?id=com.mpetaccia.flexdropin'
FLEXDROPIN_APP_STORE = 'https://apps.apple.com/it/app/flexdropin/id6758290879'
FLEXDROPIN_WEBSITE = 'https://flexdropin.com'

# ========== Bot Configuration (v1, mantenute per retrocompatibilità) ==========
# NOTA MERCATO: topic in inglese, il bot su X è dedicato al mercato
# internazionale (gestori palestre/boutique studio fuori dall'Italia).
# Il mercato italiano resta presidiato via Instagram / di persona.
SEARCH_TOPICS = os.getenv('SEARCH_TOPICS', 'CrossFit,functional training,yoga,pilates,HIIT,calisthenics,boutique fitness,gym management software,drop-in fitness,class booking app,boxing gym,indoor cycling,gym owner,fitness franchise,wellness trend,gym membership retention,fitness studio marketing').split(',')
POST_INTERVAL = int(os.getenv('POST_INTERVAL', '3600'))
MAX_SEARCH_RESULTS = int(os.getenv('MAX_SEARCH_RESULTS', '5'))
ENGAGEMENT_CHECK_INTERVAL = int(os.getenv('ENGAGEMENT_CHECK_INTERVAL', '1800'))
LIKE_ENGAGEMENT_THRESHOLD = int(os.getenv('LIKE_ENGAGEMENT_THRESHOLD', '50'))
MAX_COMMENTS_PER_SESSION = int(os.getenv('MAX_COMMENTS_PER_SESSION', '3'))

# ========== Growth Agent v3: scheduling e frequenze cicli ==========
# NOTA COSTI (X API 2026, pay-per-use): letture ~$0.005, post ~$0.015,
# post con link ~$0.20. Gli intervalli qui sotto sono pensati per contenere
# il costo mensile, non per massimizzare la frequenza di pubblicazione.
OPPORTUNITY_CYCLE_TIMES = os.getenv('OPPORTUNITY_CYCLE_TIMES', '10:00,16:00').split(',')  # 2 ricerche lead/giorno
PERFORMANCE_CYCLE_TIME = os.getenv('PERFORMANCE_CYCLE_TIME', '23:00')  # 1 volta/giorno, owned reads economici

# ========== Scoring tweet (punto 3) ==========
TWEET_SCORE_THRESHOLD = int(os.getenv('TWEET_SCORE_THRESHOLD', '24'))  # su 40 (60%)
MAX_REGENERATION_ATTEMPTS = int(os.getenv('MAX_REGENERATION_ATTEMPTS', '2'))

# ========== Regole anti-spam (punto 9) ==========
MAX_FLEXDROPIN_MENTIONS_PER_DAY = int(os.getenv('MAX_FLEXDROPIN_MENTIONS_PER_DAY', '2'))
USER_COMMENT_COOLDOWN_HOURS = int(os.getenv('USER_COMMENT_COOLDOWN_HOURS', '24'))

# ========== Human mode (punto 5) ==========
# Probabilità (0-1) che, invece di un post da palinsesto, venga pubblicato
# un post "umano" informale, per rendere l'account meno robotico.
HUMAN_MODE_PROBABILITY = float(os.getenv('HUMAN_MODE_PROBABILITY', '0.15'))

# ========== Debug Mode ==========
DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

_TELEGRAM_BOT_TOKEN_FORMAT = re.compile(
    r'^[1-9][0-9]{0,19}:[A-Za-z0-9_-]{16,128}$'
)
_TELEGRAM_CHAT_ID_FORMAT = re.compile(r'^-?[1-9][0-9]{0,19}$')


def _valid_telegram_bot_token(value):
    return (
        isinstance(value, str)
        and _TELEGRAM_BOT_TOKEN_FORMAT.fullmatch(value) is not None
    )


def _valid_telegram_chat_id(value):
    if (
        not isinstance(value, str)
        or _TELEGRAM_CHAT_ID_FORMAT.fullmatch(value) is None
    ):
        return False
    parsed = int(value)
    return -(1 << 63) <= parsed <= (1 << 63) - 1


def validate_config():
    """Validate the approval-only production boundary at startup."""
    current_approval_required, current_approval_required_valid = (
        _strict_boolean_env("APPROVAL_REQUIRED", True)
    )
    current_dry_run, current_dry_run_valid = _strict_boolean_env(
        "DRY_RUN", True,
    )
    required_keys = [
        'TWITTER_API_KEY',
        'TWITTER_API_SECRET',
        'TWITTER_ACCESS_TOKEN',
        'TWITTER_ACCESS_TOKEN_SECRET',
        'TWITTER_BEARER_TOKEN',
        'GROQ_API_KEY',
        'TELEGRAM_BOT_TOKEN',
        'TELEGRAM_CHAT_ID',
    ]
    if NEWS_TRUSTED_DOMAINS:
        required_keys.append('NEWSAPI_KEY')

    missing_keys = [
        key
        for key in required_keys
        if not isinstance(os.getenv(key), str) or not os.getenv(key).strip()
    ]

    if missing_keys:
        raise ValueError(f"❌ Variabili d'ambiente mancanti: {', '.join(missing_keys)}")

    invalid_telegram_keys = []
    if not _valid_telegram_bot_token(os.getenv('TELEGRAM_BOT_TOKEN')):
        invalid_telegram_keys.append('TELEGRAM_BOT_TOKEN')
    if not _valid_telegram_chat_id(os.getenv('TELEGRAM_CHAT_ID')):
        invalid_telegram_keys.append('TELEGRAM_CHAT_ID')
    if invalid_telegram_keys:
        raise ValueError(
            "❌ Formato Telegram non valido: "
            + ", ".join(invalid_telegram_keys)
        )

    if (
        not _DRY_RUN_VALID
        or not current_dry_run_valid
        or current_dry_run is not DRY_RUN
    ):
        raise ValueError("DRY_RUN must be exactly true or false")

    if (
        not _APPROVAL_REQUIRED_VALID
        or not current_approval_required_valid
        or current_approval_required is not APPROVAL_REQUIRED
        or APPROVAL_REQUIRED is not True
    ):
        raise ValueError("APPROVAL_REQUIRED must be true for this release")

    print("✅ Configurazione validata con successo!")
