import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


_TIME_WINDOW_FORMAT = re.compile(
    r"^(?P<start_hour>[01][0-9]|2[0-3]):(?P<start_minute>[0-5][0-9])-"
    r"(?P<end_hour>[01][0-9]|2[0-3]):(?P<end_minute>[0-5][0-9])$"
)


def _strict_positive_int_env(name, default):
    raw_value = os.getenv(name, str(default))
    if (
        not isinstance(raw_value, str)
        or not raw_value.isascii()
        or not raw_value.isdecimal()
        or len(raw_value) > 9
    ):
        raise ValueError(f"{name} must be a positive integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _strict_time_window_env(name, default):
    raw_value = os.getenv(name, default)
    match = (
        _TIME_WINDOW_FORMAT.fullmatch(raw_value)
        if isinstance(raw_value, str)
        else None
    )
    if match is None:
        raise ValueError(f"{name} must use HH:MM-HH:MM")
    start = int(match["start_hour"]) * 60 + int(match["start_minute"])
    end = int(match["end_hour"]) * 60 + int(match["end_minute"])
    if start >= end:
        raise ValueError(f"{name} must have an end after its start")
    return raw_value, start, end


def _adaptive_configuration():
    posts_per_day = _strict_positive_int_env("POSTS_PER_DAY", 2)
    if posts_per_day != 2:
        raise ValueError("POSTS_PER_DAY must be exactly 2 for this release")

    third_post_days = _strict_positive_int_env("THIRD_POST_DAYS_PER_WEEK", 3)
    if third_post_days != 3:
        raise ValueError(
            "THIRD_POST_DAYS_PER_WEEK must be exactly 3 for this release"
        )

    approved_queue_target = _strict_positive_int_env(
        "APPROVED_QUEUE_TARGET", 14,
    )
    if approved_queue_target < 14:
        raise ValueError("APPROVED_QUEUE_TARGET must be at least 14")

    pending_review_limit = _strict_positive_int_env("PENDING_REVIEW_LIMIT", 5)
    if pending_review_limit > approved_queue_target:
        raise ValueError("PENDING_REVIEW_LIMIT cannot exceed APPROVED_QUEUE_TARGET")

    generation_cap = _strict_positive_int_env("DRAFT_GENERATION_DAILY_CAP", 5)
    if generation_cap < pending_review_limit:
        raise ValueError(
            "DRAFT_GENERATION_DAILY_CAP must cover PENDING_REVIEW_LIMIT"
        )
    audience_timezone = os.getenv("AUDIENCE_TIMEZONE", "America/New_York")
    try:
        ZoneInfo(audience_timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("AUDIENCE_TIMEZONE must be a valid IANA timezone") from error

    morning_window, morning_start, morning_end = _strict_time_window_env(
        "MORNING_WINDOW", "08:30-10:30",
    )
    midday_window, midday_start, midday_end = _strict_time_window_env(
        "MIDDAY_WINDOW", "13:00-15:30",
    )
    evening_window, evening_start, evening_end = _strict_time_window_env(
        "EVENING_WINDOW", "18:00-20:30",
    )
    if morning_end > midday_start:
        raise ValueError("MORNING_WINDOW must end before MIDDAY_WINDOW")
    if midday_end > evening_start:
        raise ValueError("EVENING_WINDOW must start after MIDDAY_WINDOW")

    min_post_gap_hours = _strict_positive_int_env("MIN_POST_GAP_HOURS", 4)
    gap_minutes = min_post_gap_hours * 60
    feasible_midday_start = max(midday_start, morning_start + gap_minutes)
    feasible_midday_end = min(midday_end, evening_end - gap_minutes)
    if feasible_midday_start > feasible_midday_end:
        raise ValueError(
            "MIN_POST_GAP_HOURS cannot fit across all publication windows"
        )

    timing_min_posts = _strict_positive_int_env("ADAPTIVE_TIMING_MIN_POSTS", 30)
    weekday_min_posts = _strict_positive_int_env(
        "ADAPTIVE_WEEKDAY_MIN_POSTS", 90,
    )
    if weekday_min_posts < timing_min_posts:
        raise ValueError(
            "ADAPTIVE_WEEKDAY_MIN_POSTS cannot be below ADAPTIVE_TIMING_MIN_POSTS"
        )

    third_timing_min_posts = _strict_positive_int_env(
        "THIRD_POST_TIMING_MIN_POSTS", 30,
    )
    if third_timing_min_posts < timing_min_posts:
        raise ValueError(
            "THIRD_POST_TIMING_MIN_POSTS cannot be below ADAPTIVE_TIMING_MIN_POSTS"
        )

    grace_minutes = _strict_positive_int_env(
        "PUBLICATION_PLAN_GRACE_MINUTES", 90,
    )
    return {
        "POSTS_PER_DAY": posts_per_day,
        "THIRD_POST_DAYS_PER_WEEK": third_post_days,
        "APPROVED_QUEUE_TARGET": approved_queue_target,
        "PENDING_REVIEW_LIMIT": pending_review_limit,
        "DRAFT_GENERATION_DAILY_CAP": generation_cap,
        "AUDIENCE_TIMEZONE": audience_timezone,
        "MORNING_WINDOW": morning_window,
        "MIDDAY_WINDOW": midday_window,
        "EVENING_WINDOW": evening_window,
        "MIN_POST_GAP_HOURS": min_post_gap_hours,
        "ADAPTIVE_TIMING_MIN_POSTS": timing_min_posts,
        "ADAPTIVE_WEEKDAY_MIN_POSTS": weekday_min_posts,
        "THIRD_POST_TIMING_MIN_POSTS": third_timing_min_posts,
        "PUBLICATION_PLAN_GRACE_MINUTES": grace_minutes,
    }


_ADAPTIVE_CONFIGURATION = _adaptive_configuration()
POSTS_PER_DAY = _ADAPTIVE_CONFIGURATION["POSTS_PER_DAY"]
THIRD_POST_DAYS_PER_WEEK = _ADAPTIVE_CONFIGURATION["THIRD_POST_DAYS_PER_WEEK"]
APPROVED_QUEUE_TARGET = _ADAPTIVE_CONFIGURATION["APPROVED_QUEUE_TARGET"]
PENDING_REVIEW_LIMIT = _ADAPTIVE_CONFIGURATION["PENDING_REVIEW_LIMIT"]
DRAFT_GENERATION_DAILY_CAP = _ADAPTIVE_CONFIGURATION["DRAFT_GENERATION_DAILY_CAP"]
AUDIENCE_TIMEZONE = _ADAPTIVE_CONFIGURATION["AUDIENCE_TIMEZONE"]
MORNING_WINDOW = _ADAPTIVE_CONFIGURATION["MORNING_WINDOW"]
MIDDAY_WINDOW = _ADAPTIVE_CONFIGURATION["MIDDAY_WINDOW"]
EVENING_WINDOW = _ADAPTIVE_CONFIGURATION["EVENING_WINDOW"]
MIN_POST_GAP_HOURS = _ADAPTIVE_CONFIGURATION["MIN_POST_GAP_HOURS"]
ADAPTIVE_TIMING_MIN_POSTS = _ADAPTIVE_CONFIGURATION["ADAPTIVE_TIMING_MIN_POSTS"]
ADAPTIVE_WEEKDAY_MIN_POSTS = _ADAPTIVE_CONFIGURATION["ADAPTIVE_WEEKDAY_MIN_POSTS"]
THIRD_POST_TIMING_MIN_POSTS = _ADAPTIVE_CONFIGURATION[
    "THIRD_POST_TIMING_MIN_POSTS"
]
PUBLICATION_PLAN_GRACE_MINUTES = _ADAPTIVE_CONFIGURATION[
    "PUBLICATION_PLAN_GRACE_MINUTES"
]


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
    current_adaptive_configuration = _adaptive_configuration()
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

    if current_adaptive_configuration != _ADAPTIVE_CONFIGURATION:
        raise ValueError("Adaptive publishing configuration changed after import")

    print("✅ Configurazione validata con successo!")
