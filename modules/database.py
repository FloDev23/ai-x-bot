"""
Database module - Memoria a lungo termine del bot
Usa SQLite locale (file bot_data.db) per:
- storico tweet pubblicati (evita ripetizioni)
- database idee (categoria, priorità, stato, performance)
- lead commerciali rilevati (opportunity detector)
- performance/metriche per categoria (auto-learning)
- lista account target curata (influencer/prospect scoring)
- regole anti-spam (ultimo contatto per utente, ultimo link postato, ecc.)

Nessuna chiamata esterna: questo modulo è a costo zero.
"""
import sqlite3
import logging
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Dict, Mapping, Optional, Set, Tuple
from contextlib import ExitStack, contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from modules.media_store import (
    PinnedMediaFile,
    media_store_lock,
    record_has_media_identity,
    verify_pinned_media,
)
from modules.growth_candidate_schema import (
    evaluate_growth_candidate_filters,
    is_canonical_growth_latest_post,
    is_canonical_growth_profile,
    is_json_safe_mapping,
    parse_growth_datetime,
)
from modules.source_validation import (
    is_complete_owned_blog_article,
    is_complete_verified_news,
)

logger = logging.getLogger(__name__)

DB_PATH = 'bot_data.db'
LIVE_DRAFT_STATUSES = (
    "pending_approval",
    "approved",
    "publishing",
    "published",
    "publication_unknown",
)
_LIVE_DRAFT_STATUS_SQL = (
    "'pending_approval', 'approved', 'publishing', 'published', "
    "'publication_unknown'"
)
MEDIA_FILE_DELETE_SAFE_STATES = ("available", "used", "archived")
_MEDIA_STORE_MUTATION_MAX_ATTEMPTS = 5
_MEDIA_LIFECYCLE_MIGRATION_KEY = "migration:media_lifecycle_state"
_EDITORIAL_QUEUE_MIGRATION_KEY = "migration:approved_editorial_queue_v1"
_MEDIA_MUTATION_SNAPSHOT_COLUMNS = (
    "id",
    "filename",
    "filepath",
    "file_device",
    "file_inode",
    "file_size",
    "file_sha256",
    "lifecycle_state",
    "reserved_by_draft_id",
    "file_deleted",
    "used",
    "reusable",
)
_PUBLICATION_MEDIA_IDENTITY_COLUMNS = (
    "id",
    "filename",
    "filepath",
    "media_type",
    "file_device",
    "file_inode",
    "file_size",
    "file_sha256",
)
_DRAFT_BINDING_SNAPSHOT_COLUMNS = (
    "id",
    "revision",
    "status",
    "media_id",
    "published_tweet_id",
)
_PREVIEW_MEDIA_SNAPSHOT_COLUMNS = (
    "id",
    "filename",
    "filepath",
    "media_type",
    "mime_type",
    "file_device",
    "file_inode",
    "file_size",
    "file_sha256",
    "lifecycle_state",
    "reserved_by_draft_id",
    "file_deleted",
    "used",
    "used_in_tweet_id",
)


@dataclass(frozen=True)
class PostDraftPublicationClaim:
    """Immutable identity of the exact draft snapshot authorized for X."""

    draft_id: int
    revision: int
    publication_key: str
    text: str
    category: str
    source_ids_json: str
    score_json: str
    intended_slot: str
    media_id: Optional[int]
    approved_at: Optional[str]
    approved_by: Optional[str]


@dataclass(frozen=True)
class PublicationPlanClaim:
    """Immutable identity of one exact planned publication attempt."""

    plan_id: int
    plan_revision: int
    draft_id: int
    draft_revision: int
    scheduled_for: str
    claim_token: str


class Database:
    """Wrapper SQLite per tutta la memoria persistente del bot"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_schema()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_datetime(cls, value: str) -> datetime:
        return cls._as_utc(datetime.fromisoformat(value))

    @classmethod
    def _normalize_datetime_iso(cls, value: str) -> str:
        return cls._parse_datetime(value).isoformat()

    @staticmethod
    def _sanitize_persisted_text(value: Any) -> str:
        text = str(value)
        if text.lstrip().startswith("{") and re.search(
            r"(?i)[\"']?update_id[\"']?\s*:", text
        ):
            return "[redacted raw Telegram payload]"
        text = re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
            "Bearer [redacted]",
            text,
        )
        text = re.sub(
            r"\b\d{6,}:[A-Za-z0-9_-]{8,}\b",
            "[redacted]",
            text,
        )
        text = re.sub(
            r"(?i)(https?://[^\s?'\"}]+)\?[^\s'\"}]+",
            r"\1?[redacted]",
            text,
        )
        text = re.sub(
            r"(?i)([\"']?(?:token|api[_-]?key|authorization|password|secret|"
            r"credential)[\"']?\s*[:=]\s*)[\"']?[^,\s}\]\"']+[\"']?",
            r"\1[redacted]",
            text,
        )
        return text

    @classmethod
    def _safe_telegram_result(cls, result: Dict) -> Dict:
        if not isinstance(result, dict):
            return {}
        safe = {}
        for key in ("result", "error"):
            value = result.get(key)
            if value is None or isinstance(value, (bool, int, float)):
                if key in result:
                    safe[key] = value
            elif isinstance(value, str):
                safe[key] = cls._sanitize_persisted_text(value)
            elif key in result:
                safe[key] = "[redacted non-scalar value]"
        return safe

    @staticmethod
    def _decode_json_fields(row: sqlite3.Row, fields: Dict[str, str]) -> Dict:
        result = dict(row)
        for stored_name, public_name in fields.items():
            raw = result.pop(stored_name, None)
            result[public_name] = json.loads(raw) if raw else None
        return result

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            c = conn.cursor()
            c.execute("BEGIN IMMEDIATE")

            # Storico tweet pubblicati (memoria a lungo termine, punto 1)
            c.execute("""
                CREATE TABLE IF NOT EXISTS posted_tweets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT,
                    text TEXT NOT NULL,
                    category TEXT,
                    topic TEXT,
                    has_link INTEGER DEFAULT 0,
                    score_total INTEGER,
                    agent_used TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # Database idee (punto 4)
            c.execute("""
                CREATE TABLE IF NOT EXISTS ideas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idea TEXT NOT NULL,
                    categoria TEXT,
                    priorita INTEGER DEFAULT 5,
                    data_ultima_pubblicazione TEXT,
                    stato TEXT DEFAULT 'nuova',
                    performance REAL
                )
            """)

            # Lead commerciali (punto 19 - Opportunity Detector)
            c.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT,
                    author_username TEXT,
                    author_id TEXT,
                    text TEXT,
                    score INTEGER,
                    matched_keyword TEXT,
                    action_suggested TEXT,
                    status TEXT DEFAULT 'nuovo',
                    created_at TEXT NOT NULL
                )
            """)

            # Tweet già valutati dall'opportunity detector ma NON salvati come
            # lead (azione suggerita "Ignora"): non ci interessano in dashboard,
            # ma dobbiamo comunque ricordare di averli già scored per non
            # richiamare l'AI sugli stessi tweet ad ogni ciclo.
            c.execute("""
                CREATE TABLE IF NOT EXISTS seen_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL
                )
            """)

            # Performance per categoria (punto 2 - auto learning)
            c.execute("""
                CREATE TABLE IF NOT EXISTS category_weights (
                    category TEXT PRIMARY KEY,
                    weight REAL DEFAULT 1.0,
                    total_posts INTEGER DEFAULT 0,
                    total_engagement INTEGER DEFAULT 0,
                    avg_ctr REAL DEFAULT 0.0,
                    updated_at TEXT
                )
            """)

            # Metriche raccolte sui tweet postati (owned reads, economico)
            c.execute("""
                CREATE TABLE IF NOT EXISTS tweet_metrics (
                    tweet_id TEXT PRIMARY KEY,
                    impressions INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    retweets INTEGER DEFAULT 0,
                    replies INTEGER DEFAULT 0,
                    bookmarks INTEGER DEFAULT 0,
                    checked_at TEXT
                )
            """)

            # Account target curati (punto 7 - riconoscere influencer)
            c.execute("""
                CREATE TABLE IF NOT EXISTS target_accounts (
                    username TEXT PRIMARY KEY,
                    user_id TEXT,
                    category TEXT,
                    follower_count INTEGER DEFAULT 0,
                    engagement_score REAL DEFAULT 0.0,
                    verified INTEGER DEFAULT 0,
                    score INTEGER DEFAULT 0,
                    last_interacted TEXT
                )
            """)

            # Regole anti-spam: ultimo contatto per utente/hashtag/link (punto 9)
            c.execute("""
                CREATE TABLE IF NOT EXISTS spam_guard (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)

            # Libreria media (foto/video caricati da Floriano, analizzati
            # dall'AI e usati una sola volta ciascuno nei post)
            c.execute("""
                CREATE TABLE IF NOT EXISTS media_library (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    category TEXT DEFAULT 'other',
                    ai_description TEXT,
                    ai_tags TEXT,
                    uploaded_at TEXT DEFAULT (datetime('now')),
                    used INTEGER DEFAULT 0,
                    used_at TEXT,
                    used_in_tweet_id TEXT,
                    file_deleted INTEGER DEFAULT 0,
                    lifecycle_state TEXT DEFAULT 'available',
                    reusable INTEGER DEFAULT 0,
                    user_context TEXT DEFAULT '',
                    reserved_by_draft_id INTEGER,
                    mime_type TEXT,
                    file_size INTEGER DEFAULT 0,
                    file_device INTEGER,
                    file_inode INTEGER,
                    file_sha256 TEXT
                )
            """)

            media_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(media_library)")
            }
            media_migrations = {
                "file_deleted": "INTEGER DEFAULT 0",
                "lifecycle_state": "TEXT DEFAULT 'available'",
                "reusable": "INTEGER DEFAULT 0",
                "user_context": "TEXT DEFAULT ''",
                "reserved_by_draft_id": "INTEGER",
                "mime_type": "TEXT",
                "file_size": "INTEGER DEFAULT 0",
                "file_device": "INTEGER",
                "file_inode": "INTEGER",
                "file_sha256": "TEXT",
            }
            lifecycle_added = "lifecycle_state" not in media_columns
            for column, definition in media_migrations.items():
                if column not in media_columns:
                    c.execute(
                        f"ALTER TABLE media_library ADD COLUMN {column} {definition}"
                    )

            # Legacy data only: retained for non-destructive schema compatibility.
            # Read-only discovery and manual candidate decisions use the tables
            # below; no production method reads or writes growth_follows.
            c.execute("""
                CREATE TABLE IF NOT EXISTS growth_follows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    followed_at TEXT DEFAULT (datetime('now')),
                    followed_back INTEGER DEFAULT 0,
                    checked_at TEXT,
                    unfollowed INTEGER DEFAULT 0,
                    unfollowed_at TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS content_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    url TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    trust_state TEXT NOT NULL DEFAULT 'verified',
                    verified_by TEXT,
                    verified_at TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS post_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publication_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    score_json TEXT NOT NULL,
                    intended_slot TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_approval',
                    media_id INTEGER,
                    approved_at TEXT,
                    approved_by TEXT,
                    published_tweet_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                )
            """)
            draft_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(post_drafts)")
            }
            if "revision" not in draft_columns:
                c.execute(
                    "ALTER TABLE post_drafts "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            for draft in c.execute(
                "SELECT id, intended_slot FROM post_drafts"
            ).fetchall():
                try:
                    normalized_slot = self._normalize_datetime_iso(
                        draft["intended_slot"]
                    )
                except (TypeError, ValueError):
                    continue
                if normalized_slot != draft["intended_slot"]:
                    c.execute(
                        "UPDATE post_drafts SET intended_slot = ? WHERE id = ?",
                        (normalized_slot, draft["id"]),
                    )

            c.execute("""
                CREATE TABLE IF NOT EXISTS growth_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    latest_post_json TEXT,
                    score INTEGER NOT NULL,
                    score_json TEXT NOT NULL,
                    discovery_source TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'new',
                    rejection_reason TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_evaluated_at TEXT NOT NULL,
                    profile_expires_at TEXT NOT NULL,
                    digest_sent_at TEXT,
                    decision_at TEXT,
                    manual_followed_at TEXT,
                    followed_back_at TEXT,
                    suppressed_until TEXT
                )
            """)
            growth_candidate_columns = {
                row["name"] for row in c.execute(
                    "PRAGMA table_info(growth_candidates)"
                )
            }
            if "decision_at" not in growth_candidate_columns:
                c.execute(
                    "ALTER TABLE growth_candidates ADD COLUMN decision_at TEXT"
                )

            c.execute("""
                CREATE TABLE IF NOT EXISTS growth_profile_claims (
                    observed_on TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (observed_on, user_id)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS follower_snapshots (
                    observed_on TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    relevant INTEGER NOT NULL,
                    source TEXT,
                    attribution_source TEXT,
                    profile_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    is_new INTEGER NOT NULL DEFAULT 0,
                    captured_at TEXT,
                    PRIMARY KEY (observed_on, user_id)
                )
            """)
            follower_snapshot_columns = {
                row["name"] for row in c.execute(
                    "PRAGMA table_info(follower_snapshots)"
                )
            }
            follower_snapshot_migrations = {
                "attribution_source": "TEXT",
                "is_new": "INTEGER NOT NULL DEFAULT 0",
                "captured_at": "TEXT",
            }
            for column, definition in follower_snapshot_migrations.items():
                if column not in follower_snapshot_columns:
                    c.execute(
                        f"ALTER TABLE follower_snapshots "
                        f"ADD COLUMN {column} {definition}"
                    )

            c.execute("""
                CREATE TABLE IF NOT EXISTS follower_snapshot_runs (
                    observed_on TEXT PRIMARY KEY,
                    followers_total INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            follower_run_columns = {
                row["name"] for row in c.execute(
                    "PRAGMA table_info(follower_snapshot_runs)"
                )
            }
            follower_run_migrations = {
                "completed": "INTEGER NOT NULL DEFAULT 0",
                "summary_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, definition in follower_run_migrations.items():
                if column not in follower_run_columns:
                    c.execute(
                        f"ALTER TABLE follower_snapshot_runs "
                        f"ADD COLUMN {column} {definition}"
                    )

            c.execute("""
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'processing',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    received_at TEXT NOT NULL,
                    processed_at TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS editorial_queue (
                    draft_id INTEGER PRIMARY KEY REFERENCES post_drafts(id),
                    translation_it TEXT,
                    translation_status TEXT NOT NULL CHECK (
                        translation_status IN (
                            'pending', 'ready', 'failed', 'invalidated'
                        )
                    ),
                    review_ready_at TEXT,
                    approved_queue_at TEXT,
                    not_before TEXT,
                    blocked_reason TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS publication_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_date TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK (position IN (1, 2, 3)),
                    scheduled_for TEXT NOT NULL UNIQUE,
                    draft_id INTEGER REFERENCES post_drafts(id),
                    draft_revision INTEGER,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'open', 'planned', 'publishing', 'published',
                            'simulated', 'skipped', 'unknown'
                        )
                    ),
                    selection_reason_json TEXT NOT NULL DEFAULT '{}',
                    claim_token TEXT,
                    published_tweet_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(local_date, position)
                )
            """)
            publication_plans_schema = c.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'publication_plans'"
            ).fetchone()
            legacy_position_check = re.search(
                r"position\s+INTEGER\s+NOT\s+NULL\s+CHECK\s*\(\s*"
                r"position\s+IN\s*\(\s*1\s*,\s*2\s*\)\s*\)",
                publication_plans_schema["sql"] if publication_plans_schema else "",
                flags=re.IGNORECASE,
            )
            if legacy_position_check is not None:
                c.execute("DROP INDEX IF EXISTS uq_publication_plans_active_draft")
                c.execute("""
                    CREATE TABLE publication_plans_v3 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        local_date TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK (position IN (1, 2, 3)),
                        scheduled_for TEXT NOT NULL UNIQUE,
                        draft_id INTEGER REFERENCES post_drafts(id),
                        draft_revision INTEGER,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'open', 'planned', 'publishing', 'published',
                                'simulated', 'skipped', 'unknown'
                            )
                        ),
                        selection_reason_json TEXT NOT NULL DEFAULT '{}',
                        claim_token TEXT,
                        published_tweet_id TEXT,
                        revision INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(local_date, position)
                    )
                """)
                c.execute("""
                    INSERT INTO publication_plans_v3 (
                        id, local_date, position, scheduled_for, draft_id,
                        draft_revision, status, selection_reason_json,
                        claim_token, published_tweet_id, revision,
                        created_at, updated_at
                    )
                    SELECT
                        id, local_date, position, scheduled_for, draft_id,
                        draft_revision, status, selection_reason_json,
                        claim_token, published_tweet_id, revision,
                        created_at, updated_at
                    FROM publication_plans
                """)
                c.execute("DROP TABLE publication_plans")
                c.execute(
                    "ALTER TABLE publication_plans_v3 RENAME TO publication_plans"
                )
            c.execute("""
                CREATE TABLE IF NOT EXISTS draft_replenishment_claims (
                    token TEXT PRIMARY KEY,
                    operator_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('claimed', 'completed', 'released')
                    ),
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    draft_id INTEGER REFERENCES post_drafts(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_publication_plans_active_draft
                ON publication_plans(draft_id)
                WHERE draft_id IS NOT NULL
                  AND status IN ('planned', 'publishing', 'unknown')
            """)
            queue_migration = c.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (_EDITORIAL_QUEUE_MIGRATION_KEY,),
            ).fetchone()
            if queue_migration is None or queue_migration["value"] != "complete":
                queue_now = self._now_iso()
                c.execute("""
                    INSERT OR IGNORE INTO editorial_queue (
                        draft_id, translation_status, created_at, updated_at
                    )
                    SELECT id, 'pending', ?, ?
                    FROM post_drafts
                    WHERE status IN ('pending_approval', 'approved')
                """, (queue_now, queue_now))
                c.execute("""
                    INSERT INTO bot_state (key, value, updated_at)
                    VALUES (?, 'complete', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = 'complete', updated_at = excluded.updated_at
                """, (_EDITORIAL_QUEUE_MIGRATION_KEY, queue_now))

            lifecycle_migration = c.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (_MEDIA_LIFECYCLE_MIGRATION_KEY,),
            ).fetchone()
            if lifecycle_added or lifecycle_migration is None:
                c.execute("""
                    INSERT INTO bot_state (key, value, updated_at)
                    VALUES (?, 'pending', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = 'pending', updated_at = excluded.updated_at
                """, (_MEDIA_LIFECYCLE_MIGRATION_KEY, self._now_iso()))
                lifecycle_migration_pending = True
            else:
                lifecycle_migration_pending = (
                    lifecycle_migration["value"] == "pending"
                )

            c.execute("""
                CREATE TABLE IF NOT EXISTS error_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    context TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    safe_message TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS draft_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intended_slot TEXT NOT NULL,
                    category TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
            """)

            duplicate_slot_exists = c.execute(
                "SELECT 1 FROM post_drafts "
                "WHERE status IN (" + _LIVE_DRAFT_STATUS_SQL + ") "
                "GROUP BY intended_slot HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone() is not None
            if not duplicate_slot_exists:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_post_drafts_live_intended_slot "
                    "ON post_drafts(intended_slot) WHERE status IN ("
                    + _LIVE_DRAFT_STATUS_SQL
                    + ")"
                )
            lifecycle_repair_needed = c.execute("""
                SELECT 1 FROM media_library
                WHERE lifecycle_state IS NULL OR lifecycle_state = ''
                LIMIT 1
            """).fetchone() is not None
            media_schema_reconciliation_needed = (
                lifecycle_migration_pending
                or lifecycle_repair_needed
                or duplicate_slot_exists
            )

            migration_key = "migration:legacy_ideas_to_sources"
            migration_done = c.execute(
                "SELECT 1 FROM bot_state WHERE key = ?", (migration_key,)
            ).fetchone()
            if not migration_done:
                now = self._now_iso()
                legacy_ideas = c.execute("""
                    SELECT id, idea, categoria, priorita
                    FROM ideas
                    WHERE COALESCE(stato, 'nuova') != 'usata'
                """).fetchall()
                for idea in legacy_ideas:
                    metadata = {
                        "legacy_idea_id": idea["id"],
                        "category": idea["categoria"],
                        "priority": idea["priorita"],
                    }
                    c.execute("""
                        INSERT INTO content_sources (
                            source_type, text, metadata_json, trust_state,
                            created_at, updated_at
                        ) VALUES ('evergreen_idea', ?, ?, 'verified', ?, ?)
                    """, (idea["idea"], json.dumps(metadata), now, now))
                c.execute("""
                    INSERT INTO bot_state (key, value, updated_at)
                    VALUES (?, 'complete', ?)
                """, (migration_key, now))

            conn.commit()
        if media_schema_reconciliation_needed:
            self._reconcile_media_schema(lifecycle_migration_pending)
        logger.info("✅ Database inizializzato (bot_data.db)")

    # ---------- Posted tweets / memoria a lungo termine ----------

    def log_posted_tweet(self, text: str, category: str, topic: str = '',
                          tweet_id: str = '', has_link: bool = False,
                          score_total: int = None, agent_used: str = ''):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO posted_tweets (tweet_id, text, category, topic, has_link, score_total, agent_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (tweet_id, text, category, topic, int(has_link), score_total, agent_used,
                  datetime.now().isoformat()))

    def get_recent_topics(self, days: int = 3, limit: int = 15) -> List[str]:
        """Ritorna gli argomenti/categorie pubblicati di recente, per evitare ripetizioni"""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT topic, category, text FROM posted_tweets
                WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?
            """, (since, limit)).fetchall()
            return [f"{r['category']}: {r['topic'] or r['text'][:60]}" for r in rows]

    def count_links_last_days(
        self,
        days: int = 7,
        now: Optional[datetime] = None,
    ) -> int:
        if type(days) is not int or days <= 0:
            return 0
        current = self._as_utc(now or datetime.now(timezone.utc))
        since = current - timedelta(days=days)
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT created_at FROM posted_tweets WHERE has_link = 1
            """).fetchall()
        count = 0
        for row in rows:
            try:
                created_at = self._parse_datetime(row["created_at"])
            except (TypeError, ValueError):
                count += 1
                continue
            if created_at >= since:
                count += 1
        return count

    def last_post_had_link(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT has_link FROM posted_tweets ORDER BY created_at DESC LIMIT 1
            """).fetchone()
            return bool(row['has_link']) if row else False

    def count_flexdropin_mentions_today(self) -> int:
        today = datetime.now().date().isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as c FROM posted_tweets
                WHERE created_at >= ? AND (text LIKE '%FlexDropin%' OR category = 'promo')
            """, (today,)).fetchone()
            return row['c'] if row else 0

    def category_posted_recently(self, category: str, hours: int = 20) -> bool:
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as c FROM posted_tweets WHERE category = ? AND created_at >= ?
            """, (category, since)).fetchone()
            return (row['c'] if row else 0) > 0

    def get_recent_tweet_ids(self, limit: int = 20) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT tweet_id FROM posted_tweets
                WHERE typeof(tweet_id) = 'text' AND length(trim(tweet_id)) > 0
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [r['tweet_id'] for r in rows]

    def get_recent_posts(self, limit: int = 30) -> List[Dict]:
        """
        Post pubblicati con le relative metriche (se già raccolte dal ciclo
        di performance). Per la dashboard: LEFT JOIN così un post compare
        anche prima che le metriche vengano lette la prima volta.
        """
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT p.id, p.tweet_id, p.text, p.category, p.topic, p.has_link,
                       p.score_total, p.agent_used, p.created_at,
                       m.impressions, m.likes, m.retweets, m.replies, m.bookmarks
                FROM posted_tweets p
                LEFT JOIN tweet_metrics m ON m.tweet_id = p.tweet_id
                ORDER BY p.created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- Ideas database ----------

    def add_idea(self, idea: str, categoria: str, priorita: int = 5):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO ideas (idea, categoria, priorita, stato) VALUES (?, ?, ?, 'nuova')
            """, (idea, categoria, priorita))

    def get_next_idea(self, categoria: Optional[str] = None) -> Optional[Dict]:
        with self._conn() as conn:
            if categoria:
                row = conn.execute("""
                    SELECT * FROM ideas WHERE stato = 'nuova' AND categoria = ?
                    ORDER BY priorita DESC LIMIT 1
                """, (categoria,)).fetchone()
            else:
                row = conn.execute("""
                    SELECT * FROM ideas WHERE stato = 'nuova' ORDER BY priorita DESC LIMIT 1
                """).fetchone()
            return dict(row) if row else None

    def mark_idea_used(self, idea_id: int):
        with self._conn() as conn:
            conn.execute("""
                UPDATE ideas SET stato = 'usata', data_ultima_pubblicazione = ? WHERE id = ?
            """, (datetime.now().isoformat(), idea_id))

    # ---------- Content sources ----------

    def add_content_source(
        self,
        source_type: str,
        text: str,
        url: Optional[str] = None,
        metadata: Optional[Dict] = None,
        trust_state: str = "verified",
        verified_by: Optional[str] = None,
        verified_at: Optional[str] = None,
    ) -> int:
        with self._conn() as conn:
            return self._insert_content_source_in_conn(
                conn,
                source_type=source_type,
                text=text,
                url=url,
                metadata=metadata,
                trust_state=trust_state,
                verified_by=verified_by,
                verified_at=verified_at,
            )

    def _insert_content_source_in_conn(
        self,
        conn,
        *,
        source_type: str,
        text: str,
        url: Optional[str],
        metadata: Optional[Dict],
        trust_state: str,
        verified_by: Optional[str],
        verified_at: Optional[str],
    ) -> int:
        now = self._now_iso()
        effective_trust = trust_state
        effective_verified_at = verified_at
        expires_at = None
        if source_type == "product_fact":
            if not verified_by:
                effective_trust = "pending"
                effective_verified_at = None
            elif effective_trust == "verified":
                effective_verified_at = effective_verified_at or now
                expires_at = (
                    datetime.fromisoformat(effective_verified_at)
                    + timedelta(days=90)
                ).isoformat()
        cursor = conn.execute("""
            INSERT INTO content_sources (
                source_type, text, url, metadata_json, trust_state,
                verified_by, verified_at, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source_type,
            text,
            url,
            json.dumps(metadata or {}),
            effective_trust,
            verified_by,
            effective_verified_at,
            expires_at,
            now,
            now,
        ))
        return cursor.lastrowid

    def add_content_source_consuming_state_atomic(
        self,
        *,
        state_key: str,
        expected_state_value: str,
        source_type: str,
        text: str,
        url: Optional[str] = None,
        metadata: Optional[Dict] = None,
        trust_state: str = "verified",
        verified_by: Optional[str] = None,
        verified_at: Optional[str] = None,
    ) -> Tuple[Optional[int], str]:
        """Consume one exact Telegram session with its source insertion."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            consumed = conn.execute(
                "DELETE FROM bot_state WHERE key = ? AND value = ?",
                (state_key, expected_state_value),
            )
            if consumed.rowcount != 1:
                return None, "session_conflict"
            if url and conn.execute(
                "SELECT 1 FROM content_sources WHERE url = ? LIMIT 1", (url,)
            ).fetchone():
                return None, "duplicate"
            source_id = self._insert_content_source_in_conn(
                conn,
                source_type=source_type,
                text=text,
                url=url,
                metadata=metadata,
                trust_state=trust_state,
                verified_by=verified_by,
                verified_at=verified_at,
            )
            return source_id, "created"

    def get_content_source(self, source_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM content_sources WHERE id = ?", (source_id,)
            ).fetchone()
        if not row:
            return None
        return self._decode_json_fields(row, {"metadata_json": "metadata"})

    def import_owned_blog_articles(self, records: List[Dict]) -> Dict[str, int]:
        """Atomically import one validated snapshot of the official blog feed."""
        if type(records) is not list or len(records) > 100:
            raise ValueError("invalid_owned_blog_import")

        now = self._now_iso()
        prepared = []
        seen_urls = set()
        for record in records:
            if type(record) is not dict or frozenset(record) != frozenset({
                "slug",
                "url",
                "title",
                "summary",
                "published_at",
                "content_hash",
            }):
                raise ValueError("invalid_owned_blog_import")
            if any(type(record[field]) is not str for field in record):
                raise ValueError("invalid_owned_blog_import")
            metadata = {
                "title": record.get("title"),
                "summary": record.get("summary"),
                "published_at": record.get("published_at"),
                "source_name": "FlexDropin Blog",
                "slug": record.get("slug"),
                "feed_version": 1,
                "content_hash": record.get("content_hash"),
            }
            source = {
                "source_type": "owned_blog_article",
                "text": f"{record.get('title')}\n{record.get('summary')}",
                "url": record.get("url"),
                "metadata": metadata,
                "trust_state": "verified",
                "verified_by": "flexdropin_editorial_feed",
            }
            if (
                not is_complete_owned_blog_article(source)
                or source["url"] in seen_urls
            ):
                raise ValueError("invalid_owned_blog_import")
            seen_urls.add(source["url"])
            prepared.append(source)

        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_by_url = {}
                if prepared:
                    placeholders = ", ".join("?" for _ in prepared)
                    rows = conn.execute(
                        "SELECT * FROM content_sources WHERE url IN ("
                        + placeholders
                        + ")",
                        [source["url"] for source in prepared],
                    ).fetchall()
                    existing_by_url = {}
                    for row in rows:
                        if row["url"] in existing_by_url:
                            raise ValueError("owned_blog_source_conflict")
                        existing_by_url[row["url"]] = row

                for row in existing_by_url.values():
                    if (
                        row["source_type"] != "owned_blog_article"
                        or row["verified_by"] != "flexdropin_editorial_feed"
                    ):
                        raise ValueError("owned_blog_source_conflict")

                inserted = 0
                updated = 0
                unchanged = 0
                for source in prepared:
                    row = existing_by_url.get(source["url"])
                    metadata_json = json.dumps(
                        source["metadata"],
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if row is None:
                        conn.execute("""
                            INSERT INTO content_sources (
                                source_type, text, url, metadata_json,
                                trust_state, verified_by, verified_at,
                                expires_at, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                        """, (
                            "owned_blog_article",
                            source["text"],
                            source["url"],
                            metadata_json,
                            "verified",
                            "flexdropin_editorial_feed",
                            now,
                            now,
                            now,
                        ))
                        inserted += 1
                        continue

                    if row["trust_state"] != "verified":
                        unchanged += 1
                        continue
                    try:
                        current_metadata = json.loads(row["metadata_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        current_metadata = None
                    if (
                        isinstance(current_metadata, dict)
                        and current_metadata.get("content_hash")
                        == source["metadata"]["content_hash"]
                    ):
                        unchanged += 1
                        continue
                    conn.execute("""
                        UPDATE content_sources
                        SET text = ?, metadata_json = ?, verified_at = ?,
                            expires_at = NULL, updated_at = ?
                        WHERE id = ?
                    """, (
                        source["text"],
                        metadata_json,
                        now,
                        now,
                        row["id"],
                    ))
                    updated += 1
                return {
                    "inserted": inserted,
                    "updated": updated,
                    "unchanged": unchanged,
                }
            except Exception:
                conn.rollback()
                raise

    def insert_verified_news_batch(self, records: List[Dict]) -> int:
        """Insert complete verified-news rows in one SQLite transaction."""
        if type(records) is not list:
            raise ValueError("invalid_verified_news_batch")
        prepared = []
        seen_urls = set()
        for record in records:
            if (
                type(record) is not dict
                or frozenset(record) != frozenset({
                    "source_type",
                    "text",
                    "url",
                    "metadata",
                    "trust_state",
                    "verified_by",
                })
                or record.get("source_type") != "verified_news"
                or record.get("trust_state") != "verified"
                or record.get("verified_by") != "trusted_news_ingestion"
                or type(record.get("metadata")) is not dict
                or frozenset(record["metadata"]) != frozenset({
                    "title",
                    "summary",
                    "published_at",
                    "source_name",
                })
                or record.get("text") != record["metadata"].get("summary")
                or not is_complete_verified_news(record)
            ):
                raise ValueError("invalid_verified_news_batch")
            url = record.get("url")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            prepared.append(record)

        now = self._now_iso()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_urls = set()
                if prepared:
                    placeholders = ", ".join("?" for _ in prepared)
                    rows = conn.execute(
                        "SELECT url FROM content_sources WHERE url IN ("
                        + placeholders
                        + ")",
                        [record["url"] for record in prepared],
                    ).fetchall()
                    existing_urls = {row["url"] for row in rows}

                inserted = 0
                for record in prepared:
                    if record["url"] in existing_urls:
                        continue
                    metadata_json = json.dumps(
                        record["metadata"],
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    conn.execute("""
                        INSERT INTO content_sources (
                            source_type, text, url, metadata_json,
                            trust_state, verified_by, verified_at,
                            expires_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """, (
                        "verified_news",
                        record["text"],
                        record["url"],
                        metadata_json,
                        "verified",
                        "trusted_news_ingestion",
                        now,
                        now,
                        now,
                    ))
                    inserted += 1
                return inserted
            except Exception:
                conn.rollback()
                raise

    def _eligible_content_sources_in_conn(
        self,
        conn,
        source_ids: List[int],
        now: Optional[datetime] = None,
    ) -> List[Dict]:
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(
                isinstance(source_id, bool)
                or not isinstance(source_id, int)
                or source_id <= 0
                for source_id in source_ids
            )
        ):
            return []
        placeholders = ", ".join("?" for _ in set(source_ids))
        rows = conn.execute(
            "SELECT * FROM content_sources WHERE id IN (" + placeholders + ")",
            list(dict.fromkeys(source_ids)),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        current = self._as_utc(now or datetime.now(timezone.utc))
        eligible = []
        for source_id in source_ids:
            row = by_id.get(source_id)
            if row is None or row["trust_state"] != "verified":
                return []
            if row["expires_at"]:
                try:
                    expires_at = self._parse_datetime(row["expires_at"])
                except (TypeError, ValueError):
                    return []
                if expires_at <= current:
                    return []
            source = self._decode_json_fields(
                row, {"metadata_json": "metadata"},
            )
            if (
                source.get("source_type") == "verified_news"
                and not is_complete_verified_news(source)
            ):
                return []
            if (
                source.get("source_type") == "owned_blog_article"
                and not is_complete_owned_blog_article(source)
            ):
                return []
            eligible.append(source)
        return eligible

    def get_eligible_content_sources(
        self,
        source_ids: List[int],
        now: Optional[datetime] = None,
    ) -> List[Dict]:
        """Return every requested source in order, or none if any is ineligible."""
        with self._conn() as conn:
            return self._eligible_content_sources_in_conn(conn, source_ids, now)

    def get_eligible_sources(
        self,
        source_type: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[Dict]:
        with self._conn() as conn:
            if source_type:
                rows = conn.execute("""
                    SELECT * FROM content_sources
                    WHERE trust_state = 'verified' AND source_type = ?
                      AND source_type != 'media_context'
                    ORDER BY created_at DESC
                """, (source_type,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM content_sources
                    WHERE trust_state = 'verified'
                      AND source_type != 'media_context'
                    ORDER BY created_at DESC
                """).fetchall()

        current = now or datetime.now(timezone.utc)
        if isinstance(current, str):
            current = datetime.fromisoformat(current)
        current = self._as_utc(current)
        eligible = []
        for row in rows:
            if row["expires_at"] and self._parse_datetime(row["expires_at"]) <= current:
                continue
            source = self._decode_json_fields(
                row, {"metadata_json": "metadata"},
            )
            if (
                source.get("source_type") == "verified_news"
                and not is_complete_verified_news(source)
            ):
                continue
            if (
                source.get("source_type") == "owned_blog_article"
                and not is_complete_owned_blog_article(source)
            ):
                continue
            eligible.append(source)
        return eligible

    def content_source_exists(self, url: str) -> bool:
        if not url:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM content_sources WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
            return row is not None

    def get_content_source_usage(
        self,
        source_ids: List[int],
        now: Optional[datetime] = None,
    ) -> Optional[Dict[int, Dict]]:
        """Return fail-closed live/publication usage for requested sources."""
        if (
            type(source_ids) is not list
            or any(
                type(source_id) is not int or source_id <= 0
                for source_id in source_ids
            )
        ):
            return None
        unique_source_ids = list(dict.fromkeys(source_ids))
        current = self._as_utc(now or datetime.now(timezone.utc))
        usage = {
            source_id: {
                "bound_to_live_draft": False,
                "last_published_at": None,
                "last_linked_at": None,
            }
            for source_id in unique_source_ids
        }
        if not usage:
            return usage

        with self._conn() as conn:
            drafts = conn.execute("""
                SELECT source_ids_json, status, published_tweet_id
                FROM post_drafts
            """).fetchall()
            tweets = conn.execute("""
                SELECT tweet_id, has_link, created_at FROM posted_tweets
            """).fetchall()

        tweets_by_id = {}
        for tweet in tweets:
            tweet_id = tweet["tweet_id"]
            if not isinstance(tweet_id, str) or not tweet_id.strip():
                continue
            if tweet_id in tweets_by_id:
                return None
            tweets_by_id[tweet_id] = tweet

        live_statuses = frozenset({
            "pending_approval",
            "approved",
            "publishing",
            "publication_unknown",
        })
        requested = set(unique_source_ids)
        for draft in drafts:
            try:
                decoded_ids = json.loads(draft["source_ids_json"])
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                return None
            if (
                type(decoded_ids) is not list
                or not decoded_ids
                or any(
                    type(source_id) is not int or source_id <= 0
                    for source_id in decoded_ids
                )
                or len(set(decoded_ids)) != len(decoded_ids)
            ):
                return None
            relevant_ids = requested.intersection(decoded_ids)
            if not relevant_ids:
                continue
            status = draft["status"]
            if status in live_statuses:
                for source_id in relevant_ids:
                    usage[source_id]["bound_to_live_draft"] = True
                continue
            if status != "published":
                continue

            published_tweet_id = draft["published_tweet_id"]
            if (
                not isinstance(published_tweet_id, str)
                or not published_tweet_id.strip()
            ):
                return None
            tweet = tweets_by_id.get(published_tweet_id)
            if tweet is None:
                return None
            try:
                published_at = self._parse_datetime(tweet["created_at"])
            except (TypeError, ValueError):
                return None
            if published_at > current:
                return None
            published_iso = published_at.isoformat()
            for source_id in relevant_ids:
                prior_published = usage[source_id]["last_published_at"]
                if (
                    prior_published is None
                    or published_at > self._parse_datetime(prior_published)
                ):
                    usage[source_id]["last_published_at"] = published_iso
                if tweet["has_link"] == 1:
                    prior_linked = usage[source_id]["last_linked_at"]
                    if (
                        prior_linked is None
                        or published_at > self._parse_datetime(prior_linked)
                    ):
                        usage[source_id]["last_linked_at"] = published_iso
        return usage

    # ---------- Post drafts ----------

    def create_post_draft(
        self,
        text: str,
        category: str,
        source_ids: List[int],
        score_data: Dict,
        intended_slot: str,
        publication_key: str,
    ) -> int:
        now = self._now_iso()
        intended_slot = self._normalize_datetime_iso(intended_slot)
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO post_drafts (
                    publication_key, text, category, source_ids_json, score_json,
                    intended_slot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                publication_key,
                text,
                category,
                json.dumps(source_ids),
                json.dumps(score_data),
                intended_slot,
                now,
                now,
            ))
            return cursor.lastrowid

    def _insert_draft_evaluation_in_conn(
        self,
        conn,
        intended_slot: str,
        category: str,
        outcome: str,
        details: Dict,
        created_at: Optional[str] = None,
    ) -> int:
        cursor = conn.execute("""
            INSERT INTO draft_evaluations (
                intended_slot, category, outcome, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (
            self._normalize_datetime_iso(intended_slot),
            category,
            outcome,
            json.dumps(details or {}),
            created_at or self._now_iso(),
        ))
        return cursor.lastrowid

    def create_or_get_post_draft(
        self,
        text: str,
        category: str,
        source_ids: List[int],
        score_data: Dict,
        intended_slot: str,
        publication_key: str,
    ) -> Tuple[Optional[Dict], str]:
        """Atomically claim a live slot, returning ``(draft, outcome)``."""
        intended_slot = self._normalize_datetime_iso(intended_slot)
        now = self._now_iso()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM post_drafts WHERE intended_slot = ? "
                "AND status IN (" + _LIVE_DRAFT_STATUS_SQL + ") "
                "ORDER BY id ASC LIMIT 1",
                (intended_slot,),
            ).fetchone()
            if row:
                return self._decode_post_draft(row), "existing"
            if not self._eligible_content_sources_in_conn(conn, source_ids):
                return None, "no_eligible_source"
            try:
                cursor = conn.execute("""
                    INSERT INTO post_drafts (
                        publication_key, text, category, source_ids_json,
                        score_json, intended_slot, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    publication_key,
                    text,
                    category,
                    json.dumps(source_ids),
                    json.dumps(score_data),
                    intended_slot,
                    now,
                    now,
                ))
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM post_drafts WHERE intended_slot = ? "
                    "AND status IN (" + _LIVE_DRAFT_STATUS_SQL + ") "
                    "ORDER BY id ASC LIMIT 1",
                    (intended_slot,),
                ).fetchone()
                if row:
                    return self._decode_post_draft(row), "existing"
                raise
            draft_id = cursor.lastrowid
            self._insert_draft_evaluation_in_conn(
                conn,
                intended_slot,
                category,
                "pending_approval",
                {
                    "draft_id": draft_id,
                    "source_ids": source_ids,
                    "scores": score_data,
                },
                now,
            )
            row = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            return self._decode_post_draft(row), "created"

    def _decode_post_draft(self, row: sqlite3.Row) -> Dict:
        return self._decode_json_fields(row, {
            "source_ids_json": "source_ids",
            "score_json": "score_data",
        })

    @staticmethod
    def _publication_claim_from_row(
        row: sqlite3.Row,
    ) -> PostDraftPublicationClaim:
        return PostDraftPublicationClaim(
            draft_id=row["id"],
            revision=row["revision"],
            publication_key=row["publication_key"],
            text=row["text"],
            category=row["category"],
            source_ids_json=row["source_ids_json"],
            score_json=row["score_json"],
            intended_slot=row["intended_slot"],
            media_id=row["media_id"],
            approved_at=row["approved_at"],
            approved_by=row["approved_by"],
        )

    @staticmethod
    def _post_draft_matches_publication_claim(
        row: Optional[sqlite3.Row],
        claim: PostDraftPublicationClaim,
    ) -> bool:
        if row is None or not isinstance(claim, PostDraftPublicationClaim):
            return False
        return (
            row["id"] == claim.draft_id
            and row["status"] == "publishing"
            and row["revision"] == claim.revision
            and row["publication_key"] == claim.publication_key
            and row["text"] == claim.text
            and row["category"] == claim.category
            and row["source_ids_json"] == claim.source_ids_json
            and row["score_json"] == claim.score_json
            and row["intended_slot"] == claim.intended_slot
            and row["media_id"] == claim.media_id
            and row["approved_at"] == claim.approved_at
            and row["approved_by"] == claim.approved_by
            and row["published_tweet_id"] is None
        )

    @staticmethod
    def _publication_media_matches_in_conn(
        conn,
        claim: PostDraftPublicationClaim,
        expected_media: Optional[Mapping],
    ) -> bool:
        reserved = conn.execute("""
            SELECT * FROM media_library
            WHERE reserved_by_draft_id = ?
              AND lifecycle_state = 'reserved'
            ORDER BY id
        """, (claim.draft_id,)).fetchall()
        if claim.media_id is None:
            return expected_media is None and not reserved
        if (
            not isinstance(expected_media, Mapping)
            or expected_media.get("id") != claim.media_id
            or expected_media.get("lifecycle_state") != "reserved"
            or expected_media.get("reserved_by_draft_id") != claim.draft_id
            or expected_media.get("file_deleted")
            or not record_has_media_identity(expected_media)
            or len(reserved) != 1
        ):
            return False
        row = reserved[0]
        return (
            row["id"] == claim.media_id
            and not row["file_deleted"]
            and all(
                row[column] == expected_media.get(column)
                for column in _PUBLICATION_MEDIA_IDENTITY_COLUMNS
            )
        )

    def get_post_draft(self, draft_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        return self._decode_post_draft(row) if row else None

    def list_post_drafts(
        self,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict]:
        with self._conn() as conn:
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                rows = conn.execute(
                    "SELECT * FROM post_drafts WHERE status IN ("
                    + placeholders
                    + ") ORDER BY intended_slot DESC LIMIT ?",
                    list(statuses) + [limit],
                ).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM post_drafts
                    ORDER BY intended_slot DESC LIMIT ?
                """, (limit,)).fetchall()
        return [self._decode_post_draft(row) for row in rows]

    def get_active_draft_for_slot(self, intended_slot: str) -> Optional[Dict]:
        intended_slot = self._normalize_datetime_iso(intended_slot)
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM post_drafts
                WHERE intended_slot = ?
                  AND status IN (
                      'pending_approval', 'approved', 'publishing', 'published',
                      'publication_unknown'
                  )
                ORDER BY created_at DESC LIMIT 1
            """, (intended_slot,)).fetchone()
        return self._decode_post_draft(row) if row else None

    def get_content_mix_counts(self, days: int = 30) -> Dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT category, COUNT(*) AS count
                FROM post_drafts
                WHERE created_at >= ?
                  AND status IN (
                      'pending_approval', 'approved', 'publishing', 'published',
                      'publication_unknown'
                  )
                GROUP BY category
            """, (since,)).fetchall()
        return {row["category"]: row["count"] for row in rows}

    def count_drafts_for_local_date(
        self,
        local_date: date,
        timezone_name: str,
    ) -> int:
        if isinstance(local_date, str):
            local_date = date.fromisoformat(local_date)
        local_timezone = ZoneInfo(timezone_name)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT intended_slot FROM post_drafts"
            ).fetchall()
        count = 0
        for row in rows:
            intended = datetime.fromisoformat(row["intended_slot"])
            if intended.tzinfo is None:
                intended = intended.replace(tzinfo=local_timezone)
            if intended.astimezone(local_timezone).date() == local_date:
                count += 1
        return count

    # ---------- Approved editorial queue / adaptive publication plans ----------

    @staticmethod
    def _exact_positive_identifier(value: Any) -> bool:
        return type(value) is int and value > 0

    @staticmethod
    def _exact_nonnegative_revision(value: Any) -> bool:
        return type(value) is int and value >= 0

    @staticmethod
    def _strict_aware_datetime(value: Any) -> Optional[datetime]:
        if type(value) is datetime:
            parsed = value
        elif type(value) is str and value and len(value) <= 64:
            try:
                parsed = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _decode_source_ids(raw_value: Any) -> Optional[List[int]]:
        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return None
        if (
            type(decoded) is not list
            or not decoded
            or any(type(source_id) is not int or source_id <= 0 for source_id in decoded)
            or len(decoded) != len(set(decoded))
        ):
            return None
        return decoded

    @classmethod
    def _decode_queue_draft_row(cls, row: Optional[sqlite3.Row]) -> Optional[Dict]:
        if row is None:
            return None
        record = dict(row)
        source_ids = cls._decode_source_ids(record.pop("source_ids_json", None))
        try:
            score_data = json.loads(record.pop("score_json", ""))
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return None
        queue_statuses = {"pending", "ready", "failed", "invalidated"}
        draft_statuses = {
            "pending_approval",
            "approved",
            "publishing",
            "published",
            "publication_unknown",
            "publication_failed",
            "expired",
            "superseded",
            "discarded",
        }
        if (
            source_ids is None
            or type(score_data) is not dict
            or record.get("translation_status") not in queue_statuses
            or record.get("status") not in draft_statuses
            or not cls._exact_positive_identifier(record.get("id"))
            or not cls._exact_positive_identifier(record.get("draft_id"))
            or record["id"] != record["draft_id"]
            or not cls._exact_nonnegative_revision(record.get("revision"))
            or not cls._exact_nonnegative_revision(record.get("queue_revision"))
        ):
            return None
        translation = record.get("translation_it")
        if translation is not None and (
            type(translation) is not str
            or not translation.strip()
            or len(translation) > 4096
            or len(translation.encode("utf-8")) > 8192
        ):
            return None
        if record["translation_status"] == "ready" and translation is None:
            return None
        blocked_reason = record.get("blocked_reason")
        if blocked_reason is not None and (
            type(blocked_reason) is not str
            or not blocked_reason
            or len(blocked_reason) > 128
        ):
            return None
        for field in ("created_at", "updated_at", "queue_created_at", "queue_updated_at"):
            if cls._strict_aware_datetime(record.get(field)) is None:
                return None
        for field in (
            "review_ready_at",
            "approved_queue_at",
            "not_before",
            "approved_at",
        ):
            if record.get(field) is not None and cls._strict_aware_datetime(
                record[field]
            ) is None:
                return None
        current = datetime.now(timezone.utc)
        for field in ("approved_queue_at", "approved_at"):
            parsed = cls._strict_aware_datetime(record.get(field))
            if parsed is not None and parsed > current:
                return None
        record["source_ids"] = source_ids
        record["score_data"] = score_data
        return record

    @staticmethod
    def _queue_draft_row_in_conn(conn, draft_id: int) -> Optional[sqlite3.Row]:
        return conn.execute("""
            SELECT
                d.id, d.publication_key, d.text, d.category,
                d.source_ids_json, d.score_json, d.intended_slot, d.status,
                d.media_id, d.approved_at, d.approved_by,
                d.published_tweet_id, d.error, d.created_at, d.updated_at,
                d.revision,
                q.draft_id, q.translation_it, q.translation_status,
                q.review_ready_at, q.approved_queue_at, q.not_before,
                q.blocked_reason, q.revision AS queue_revision,
                q.created_at AS queue_created_at,
                q.updated_at AS queue_updated_at
            FROM post_drafts d
            JOIN editorial_queue q ON q.draft_id = d.id
            WHERE d.id = ?
        """, (draft_id,)).fetchone()

    def ensure_editorial_queue(self, draft_id: int) -> Optional[Dict]:
        if not self._exact_positive_identifier(draft_id):
            return None
        with self._post_draft_mutation_lock(draft_id) as conn:
            draft = conn.execute(
                "SELECT status FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if draft is None or draft["status"] not in ("pending_approval", "approved"):
                return None
            now = self._now_iso()
            conn.execute("""
                INSERT OR IGNORE INTO editorial_queue (
                    draft_id, translation_status, created_at, updated_at
                ) VALUES (?, 'pending', ?, ?)
            """, (draft_id, now, now))
            return self._decode_queue_draft_row(
                self._queue_draft_row_in_conn(conn, draft_id)
            )

    def get_queue_draft(self, draft_id: int) -> Optional[Dict]:
        if not self._exact_positive_identifier(draft_id):
            return None
        with self._conn() as conn:
            row = self._queue_draft_row_in_conn(conn, draft_id)
        return self._decode_queue_draft_row(row)

    def save_review_translation(
        self,
        draft_id: int,
        expected_draft_revision: int,
        text_it: str,
    ) -> bool:
        if (
            not self._exact_positive_identifier(draft_id)
            or not self._exact_nonnegative_revision(expected_draft_revision)
            or type(text_it) is not str
            or not text_it.strip()
            or len(text_it) > 4096
            or len(text_it.encode("utf-8")) > 8192
        ):
            return False
        with self._post_draft_mutation_lock(draft_id) as conn:
            current = self._queue_draft_row_in_conn(conn, draft_id)
            if (
                current is None
                or current["revision"] != expected_draft_revision
                or current["status"] not in ("pending_approval", "approved")
            ):
                return False
            if (
                current["translation_status"] == "ready"
                and current["translation_it"] == text_it
            ):
                return True
            now = self._now_iso()
            cursor = conn.execute("""
                UPDATE editorial_queue
                SET translation_it = ?, translation_status = 'ready',
                    review_ready_at = ?, approved_queue_at = NULL,
                    blocked_reason = NULL, updated_at = ?,
                    revision = revision + 1
                WHERE draft_id = ? AND revision = ?
            """, (
                text_it,
                now,
                now,
                draft_id,
                current["queue_revision"],
            ))
            return cursor.rowcount == 1

    def approve_queued_draft_atomic(
        self,
        draft_id: int,
        expected_draft_revision: int,
        expected_queue_revision: int,
        approved_by: str,
        approved_at: str,
    ) -> bool:
        try:
            return self._approve_queued_draft_atomic(
                draft_id,
                expected_draft_revision,
                expected_queue_revision,
                approved_by,
                approved_at,
            )
        except (OSError, ValueError, sqlite3.DatabaseError):
            return False

    def _approve_queued_draft_atomic(
        self,
        draft_id: int,
        expected_draft_revision: int,
        expected_queue_revision: int,
        approved_by: str,
        approved_at: str,
    ) -> bool:
        approval_time = self._strict_aware_datetime(approved_at)
        validation_time = datetime.now(timezone.utc)
        if (
            not self._exact_positive_identifier(draft_id)
            or not self._exact_nonnegative_revision(expected_draft_revision)
            or not self._exact_nonnegative_revision(expected_queue_revision)
            or type(approved_by) is not str
            or not approved_by.strip()
            or len(approved_by) > 64
            or approval_time is None
            or approval_time > validation_time
        ):
            return False
        with self._post_draft_mutation_lock(draft_id) as conn:
            current = self._queue_draft_row_in_conn(conn, draft_id)
            if (
                current is None
                or current["revision"] != expected_draft_revision
                or current["queue_revision"] != expected_queue_revision
                or current["status"] not in ("pending_approval", "approved")
                or current["translation_status"] != "ready"
                or type(current["translation_it"]) is not str
                or not current["translation_it"].strip()
            ):
                return False
            source_ids = self._decode_source_ids(current["source_ids_json"])
            if source_ids is None or not self._eligible_content_sources_in_conn(
                conn, source_ids, validation_time
            ):
                return False
            if not self._queue_media_binding_valid_in_conn(conn, current):
                return False
            now = self._now_iso()
            draft_cursor = conn.execute("""
                UPDATE post_drafts
                SET status = 'approved', approved_at = ?, approved_by = ?,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND revision = ?
                  AND status IN ('pending_approval', 'approved')
            """, (
                approval_time.isoformat(),
                approved_by.strip(),
                now,
                draft_id,
                expected_draft_revision,
            ))
            if draft_cursor.rowcount != 1:
                return False
            queue_cursor = conn.execute("""
                UPDATE editorial_queue
                SET approved_queue_at = ?, blocked_reason = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE draft_id = ? AND revision = ?
                  AND translation_status = 'ready'
            """, (
                approval_time.isoformat(),
                now,
                draft_id,
                expected_queue_revision,
            ))
            if queue_cursor.rowcount != 1:
                conn.rollback()
                return False
            return True

    @staticmethod
    def _queue_media_binding_valid_in_conn(conn, draft_row: Mapping) -> bool:
        reserved = conn.execute("""
            SELECT * FROM media_library
            WHERE reserved_by_draft_id = ? AND lifecycle_state = 'reserved'
            ORDER BY id
        """, (draft_row["id"],)).fetchall()
        media_id = draft_row["media_id"]
        if media_id is None:
            return not reserved
        if type(media_id) is not int or media_id <= 0 or len(reserved) != 1:
            return False
        media = reserved[0]
        return (
            media["id"] == media_id
            and media["file_deleted"] == 0
            and media["lifecycle_state"] == "reserved"
            and media["reserved_by_draft_id"] == draft_row["id"]
            and record_has_media_identity(dict(media))
        )

    def invalidate_review_translation(
        self,
        draft_id: int,
        expected_draft_revision: int,
    ) -> bool:
        if (
            not self._exact_positive_identifier(draft_id)
            or not self._exact_nonnegative_revision(expected_draft_revision)
        ):
            return False
        with self._post_draft_mutation_lock(draft_id) as conn:
            current = self._queue_draft_row_in_conn(conn, draft_id)
            if (
                current is None
                or current["revision"] != expected_draft_revision
                or current["status"] not in ("pending_approval", "approved")
            ):
                return False
            now = self._now_iso()
            draft_cursor = conn.execute("""
                UPDATE post_drafts
                SET status = 'pending_approval', approved_at = NULL,
                    approved_by = NULL, updated_at = ?, revision = revision + 1
                WHERE id = ? AND revision = ?
                  AND status IN ('pending_approval', 'approved')
            """, (now, draft_id, expected_draft_revision))
            if draft_cursor.rowcount != 1:
                return False
            queue_cursor = conn.execute("""
                UPDATE editorial_queue
                SET translation_it = NULL, translation_status = 'invalidated',
                    review_ready_at = NULL, approved_queue_at = NULL,
                    blocked_reason = NULL, updated_at = ?,
                    revision = revision + 1
                WHERE draft_id = ? AND revision = ?
            """, (now, draft_id, current["queue_revision"]))
            if queue_cursor.rowcount != 1:
                conn.rollback()
                return False
            return True

    def get_queue_counts(self, operator_date: date, timezone_name: str) -> Dict[str, int]:
        if type(operator_date) is not date or type(timezone_name) is not str:
            raise ValueError("invalid queue count boundary")
        try:
            operator_zone = ZoneInfo(timezone_name)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("invalid queue count timezone") from error
        counts = {
            "awaiting_translation": 0,
            "awaiting_review": 0,
            "approved_available": 0,
            "approved_or_planned": 0,
            "planned_today": 0,
            "blocked": 0,
        }
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT d.id, d.status, q.translation_status,
                       q.translation_it, q.approved_queue_at,
                       q.not_before, q.blocked_reason
                FROM editorial_queue q
                JOIN post_drafts d ON d.id = q.draft_id
            """).fetchall()
            plans = conn.execute("""
                SELECT scheduled_for, status FROM publication_plans
                WHERE status IN (
                    'planned', 'publishing', 'published',
                    'simulated', 'unknown'
                )
            """).fetchall()
            active_drafts = {
                row["draft_id"]
                for row in conn.execute("""
                    SELECT draft_id FROM publication_plans
                    WHERE draft_id IS NOT NULL
                      AND status IN ('planned', 'publishing', 'unknown')
                """).fetchall()
            }
        approved_reserve = set()
        for row in rows:
            if row["blocked_reason"]:
                counts["blocked"] += 1
                continue
            if row["status"] not in ("pending_approval", "approved"):
                continue
            if row["translation_status"] != "ready" or not row["translation_it"]:
                counts["awaiting_translation"] += 1
            elif row["approved_queue_at"] is None:
                counts["awaiting_review"] += 1
            elif row["status"] == "approved":
                approved_reserve.add(row["id"])
                if row["id"] not in active_drafts:
                    not_before = self._strict_aware_datetime(row["not_before"])
                    if row["not_before"] is None or (
                        not_before is not None and not_before <= now
                    ):
                        counts["approved_available"] += 1
        counts["approved_or_planned"] = len(approved_reserve.union(active_drafts))
        for plan in plans:
            scheduled = self._strict_aware_datetime(plan["scheduled_for"])
            if scheduled and scheduled.astimezone(operator_zone).date() == operator_date:
                counts["planned_today"] += 1
        return counts

    @staticmethod
    def _decode_replenishment_claim(row: Optional[sqlite3.Row]) -> Optional[Dict]:
        if row is None:
            return None
        record = dict(row)
        if (
            type(record.get("token")) is not str
            or not record["token"]
            or record.get("status") not in {"claimed", "completed", "released"}
        ):
            return None
        return record

    def claim_replenishment(
        self,
        operator_date: date,
        max_daily: int,
        now: datetime,
        ttl_seconds: int = 1800,
        cycle_key: Optional[str] = None,
    ) -> Optional[Dict]:
        current = self._strict_aware_datetime(now)
        if (
            type(operator_date) is not date
            or type(max_daily) is not int
            or max_daily <= 0
            or current is None
            or type(ttl_seconds) is not int
            or ttl_seconds <= 0
            or ttl_seconds > 86400
            or (
                cycle_key is not None
                and (
                    type(cycle_key) is not str
                    or re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", cycle_key) is None
                )
            )
        ):
            return None
        now_iso = current.isoformat()
        expires_at = (current + timedelta(seconds=ttl_seconds)).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired_claims = conn.execute("""
                    SELECT token, claimed_at, draft_id
                    FROM draft_replenishment_claims
                    WHERE operator_date = ? AND status = 'claimed'
                      AND julianday(expires_at) <= julianday(?)
                    ORDER BY julianday(claimed_at), token
                """, (operator_date.isoformat(), now_iso)).fetchall()
            expired_tokens = [row["token"] for row in expired_claims]
            reusable_claim = None
            if cycle_key is not None:
                reusable_claim = next(
                    (row for row in expired_claims if row["draft_id"] is None),
                    None,
                )
            conn.execute("""
                UPDATE draft_replenishment_claims
                SET status = 'released', updated_at = ?
                WHERE operator_date = ? AND status = 'claimed'
                  AND julianday(expires_at) <= julianday(?)
            """, (now_iso, operator_date.isoformat(), now_iso))
            if cycle_key is not None and conn.execute("""
                SELECT 1 FROM draft_replenishment_claims
                WHERE operator_date = ? AND status = 'claimed'
                  AND julianday(expires_at) > julianday(?)
                LIMIT 1
            """, (operator_date.isoformat(), now_iso)).fetchone() is not None:
                return None
            count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM draft_replenishment_claims
                WHERE operator_date = ?
                  AND (
                    status = 'completed'
                    OR (
                        status = 'claimed'
                        AND julianday(expires_at) > julianday(?)
                    )
                  )
            """, (operator_date.isoformat(), now_iso)).fetchone()["count"]
            if count >= max_daily:
                return None
            token = (
                reusable_claim["token"]
                if reusable_claim is not None
                else secrets.token_urlsafe(24)
            )
            if cycle_key is not None:
                for expired_token in expired_tokens:
                    conn.execute("""
                        DELETE FROM bot_state
                        WHERE key LIKE 'replenishment_cycle:%' AND value = ?
                    """, (expired_token,))
                cycle_state_key = (
                    "replenishment_cycle:"
                    + operator_date.isoformat()
                    + ":"
                    + cycle_key
                )
                claimed_cycle = conn.execute("""
                    INSERT OR IGNORE INTO bot_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, (cycle_state_key, token, now_iso))
                if claimed_cycle.rowcount != 1:
                    return None
            if reusable_claim is not None:
                cursor = conn.execute("""
                    UPDATE draft_replenishment_claims
                    SET status = 'claimed', expires_at = ?, updated_at = ?
                    WHERE token = ? AND status = 'released' AND draft_id IS NULL
                """, (expires_at, now_iso, token))
                if cursor.rowcount != 1:
                    return None
                ordinal = conn.execute("""
                    SELECT COUNT(*) + 1 AS ordinal
                    FROM draft_replenishment_claims
                    WHERE operator_date = ? AND status = 'completed'
                      AND julianday(claimed_at) < julianday(?)
                """, (
                    operator_date.isoformat(),
                    reusable_claim["claimed_at"],
                )).fetchone()["ordinal"]
                record = self._decode_replenishment_claim(conn.execute(
                    "SELECT * FROM draft_replenishment_claims WHERE token = ?",
                    (token,),
                ).fetchone())
                if record is not None:
                    record["ordinal"] = ordinal
                return record
            conn.execute("""
                INSERT INTO draft_replenishment_claims (
                    token, operator_date, status, claimed_at, expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, 'claimed', ?, ?, ?, ?)
            """, (
                token,
                operator_date.isoformat(),
                now_iso,
                expires_at,
                now_iso,
                now_iso,
            ))
            record = self._decode_replenishment_claim(conn.execute(
                "SELECT * FROM draft_replenishment_claims WHERE token = ?",
                (token,),
            ).fetchone())
            if record is not None:
                record["ordinal"] = count + 1
            return record

    def complete_replenishment_claim(self, token: str, draft_id: int) -> bool:
        if (
            type(token) is not str
            or not token
            or len(token) > 128
            or not self._exact_positive_identifier(draft_id)
        ):
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone() is None:
                return False
            cursor = conn.execute("""
                UPDATE draft_replenishment_claims
                SET status = 'completed', draft_id = ?, updated_at = ?
                WHERE token = ? AND status = 'claimed'
                  AND NOT EXISTS (
                    SELECT 1 FROM draft_replenishment_claims other
                    WHERE other.draft_id = ? AND other.status = 'completed'
                  )
            """, (draft_id, self._now_iso(), token, draft_id))
            return cursor.rowcount == 1

    def release_replenishment_claim(self, token: str) -> bool:
        if type(token) is not str or not token or len(token) > 128:
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""
                UPDATE draft_replenishment_claims
                SET status = 'released', updated_at = ?
                WHERE token = ? AND status = 'claimed'
            """, (self._now_iso(), token))
            if cursor.rowcount == 1:
                conn.execute("""
                    DELETE FROM bot_state
                    WHERE key LIKE 'replenishment_cycle:%' AND value = ?
                """, (token,))
            return cursor.rowcount == 1

    @classmethod
    def _decode_publication_plan(cls, row: Optional[sqlite3.Row]) -> Optional[Dict]:
        if row is None:
            return None
        record = dict(row)
        try:
            reason = json.loads(record.pop("selection_reason_json"))
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return None
        allowed_reason_keys = {
            "source_urgency", "score", "category_diversity",
            "format_diversity", "approval_age", "timing_reason",
            "timing_bucket",
        }
        if (
            type(reason) is not dict
            or not reason
            or not set(reason).issubset(allowed_reason_keys)
            or reason.get("timing_reason") not in {
                "cold_start", "performance_weighted",
            }
            or type(reason.get("timing_bucket")) is not str
            or re.fullmatch(
                r"(?:morning|midday|evening):[0-9]{1,2}",
                reason["timing_bucket"],
            ) is None
            or record.get("status") not in {
                "open", "planned", "publishing", "published",
                "simulated", "skipped", "unknown",
            }
            or not cls._exact_positive_identifier(record.get("id"))
            or not cls._exact_nonnegative_revision(record.get("revision"))
            or cls._strict_aware_datetime(record.get("scheduled_for")) is None
        ):
            return None
        numeric_contract = {
            "source_urgency": (0, 1_000_000),
            "score": (0, 100),
            "category_diversity": (0, 1),
            "format_diversity": (0, 1),
            "approval_age": (0, 31_536_000),
        }
        for key, (minimum, maximum) in numeric_contract.items():
            if key in reason and (
                type(reason[key]) is not int
                or not minimum <= reason[key] <= maximum
            ):
                return None
        record["selection_reason"] = reason
        return record

    def create_or_get_publication_positions(
        self,
        local_date: date,
        decision,
        now: datetime,
    ) -> List[Dict]:
        from modules.adaptive_timing import DailyTimingDecision

        current = self._strict_aware_datetime(now)
        if (
            type(local_date) is not date
            or type(decision) is not DailyTimingDecision
            or current is None
            or type(decision.times) is not tuple
            or len(decision.times) not in {2, 3}
            or type(decision.bucket_ids) is not tuple
            or len(decision.bucket_ids) != len(decision.times)
            or decision.reason not in {"cold_start", "performance_weighted"}
        ):
            return []
        expected_bucket_groups = (
            ("morning", "evening")
            if len(decision.times) == 2
            else ("morning", "midday", "evening")
        )
        prepared = []
        for position, (scheduled, bucket_id, expected_group) in enumerate(
            zip(decision.times, decision.bucket_ids, expected_bucket_groups), start=1
        ):
            normalized = self._strict_aware_datetime(scheduled)
            if (
                normalized is None
                or scheduled.date() != local_date
                or type(bucket_id) is not str
                or re.fullmatch(
                    rf"{expected_group}:[0-9]{{1,2}}", bucket_id
                ) is None
            ):
                return []
            reason = json.dumps(
                {
                    "timing_bucket": bucket_id,
                    "timing_reason": decision.reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            prepared.append((position, scheduled.isoformat(), reason))
        if any(
            later <= earlier
            for earlier, later in zip(decision.times, decision.times[1:])
        ):
            return []
        now_iso = current.isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM publication_plans WHERE local_date = ? ORDER BY position",
                (local_date.isoformat(),),
            ).fetchall()
            if existing:
                decoded = [self._decode_publication_plan(row) for row in existing]
                return decoded if len(decoded) in {2, 3} and all(decoded) else []
            for position, scheduled_for, reason in prepared:
                conn.execute("""
                    INSERT INTO publication_plans (
                        local_date, position, scheduled_for, status,
                        selection_reason_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'open', ?, ?, ?)
                """, (
                    local_date.isoformat(),
                    position,
                    scheduled_for,
                    reason,
                    now_iso,
                    now_iso,
                ))
            rows = conn.execute(
                "SELECT * FROM publication_plans WHERE local_date = ? ORDER BY position",
                (local_date.isoformat(),),
            ).fetchall()
            decoded = [self._decode_publication_plan(row) for row in rows]
            return decoded if len(decoded) == len(prepared) and all(decoded) else []

    def list_publication_positions(
        self,
        local_date: Optional[date] = None,
        statuses: Optional[List[str]] = None,
    ) -> List[Dict]:
        allowed = {
            "open", "planned", "publishing", "published",
            "simulated", "skipped", "unknown",
        }
        if local_date is not None and type(local_date) is not date:
            return []
        if statuses is not None and (
            type(statuses) is not list
            or not statuses
            or any(type(status) is not str or status not in allowed for status in statuses)
        ):
            return []
        clauses = []
        parameters = []
        if local_date is not None:
            clauses.append("local_date = ?")
            parameters.append(local_date.isoformat())
        if statuses is not None:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        sql = "SELECT * FROM publication_plans"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY local_date, position"
        with self._conn() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [
            decoded
            for row in rows
            if (decoded := self._decode_publication_plan(row)) is not None
        ]

    def list_approved_queue(self, now: datetime) -> List[Dict]:
        current = self._strict_aware_datetime(now)
        if current is None:
            return []
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT
                    d.id, d.publication_key, d.text, d.category,
                    d.source_ids_json, d.score_json, d.intended_slot, d.status,
                    d.media_id, d.approved_at, d.approved_by,
                    d.published_tweet_id, d.error, d.created_at, d.updated_at,
                    d.revision,
                    q.draft_id, q.translation_it, q.translation_status,
                    q.review_ready_at, q.approved_queue_at, q.not_before,
                    q.blocked_reason, q.revision AS queue_revision,
                    q.created_at AS queue_created_at,
                    q.updated_at AS queue_updated_at
                FROM editorial_queue q
                JOIN post_drafts d ON d.id = q.draft_id
                WHERE d.status = 'approved'
                  AND q.translation_status = 'ready'
                  AND q.approved_queue_at IS NOT NULL
                  AND q.blocked_reason IS NULL
                  AND (q.not_before IS NULL OR julianday(q.not_before) <= julianday(?))
                  AND NOT EXISTS (
                    SELECT 1 FROM publication_plans p
                    WHERE p.draft_id = d.id
                      AND p.status IN ('planned', 'publishing', 'unknown')
                  )
                ORDER BY julianday(q.approved_queue_at), d.id
            """, (current.isoformat(),)).fetchall()
        return [
            decoded
            for row in rows
            if (decoded := self._decode_queue_draft_row(row)) is not None
        ]

    @staticmethod
    def _safe_plan_reason(reason: Any) -> Optional[str]:
        if type(reason) is not dict or not reason or len(reason) > 5:
            return None
        allowed = {
            "source_urgency", "score", "category_diversity",
            "format_diversity", "approval_age",
        }
        if not set(reason).issubset(allowed):
            return None
        numeric_contract = {
            "source_urgency": (0, 1_000_000),
            "score": (0, 100),
            "category_diversity": (0, 1),
            "format_diversity": (0, 1),
            "approval_age": (0, 31_536_000),
        }
        projected = {}
        for key, value in reason.items():
            minimum, maximum = numeric_contract[key]
            if type(value) is not int or not minimum <= value <= maximum:
                return None
            projected[key] = value
        try:
            encoded = json.dumps(
                projected,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return None
        return encoded if len(encoded.encode("utf-8")) <= 1024 else None

    def assign_publication_plan_atomic(
        self,
        plan_id: int,
        draft_id: int,
        expected_draft_revision: int,
        reason: Dict,
        source_valid_at: Optional[datetime] = None,
        max_links_per_week: Optional[int] = None,
    ) -> bool:
        reason_json = self._safe_plan_reason(reason)
        requested_source_validation = (
            self._strict_aware_datetime(source_valid_at)
            if source_valid_at is not None
            else None
        )
        if (
            not self._exact_positive_identifier(plan_id)
            or not self._exact_positive_identifier(draft_id)
            or not self._exact_nonnegative_revision(expected_draft_revision)
            or reason_json is None
            or (source_valid_at is not None and requested_source_validation is None)
            or (
                max_links_per_week is not None
                and (type(max_links_per_week) is not int or max_links_per_week < 0)
            )
        ):
            return False
        try:
            with self._post_draft_mutation_lock(draft_id) as conn:
                plan = conn.execute(
                    "SELECT * FROM publication_plans WHERE id = ?", (plan_id,)
                ).fetchone()
                current = self._queue_draft_row_in_conn(conn, draft_id)
                if (
                    plan is None
                    or plan["status"] != "open"
                    or plan["draft_id"] is not None
                    or current is None
                    or current["status"] != "approved"
                    or current["revision"] != expected_draft_revision
                    or current["translation_status"] != "ready"
                    or not current["translation_it"]
                    or current["approved_queue_at"] is None
                    or current["blocked_reason"] is not None
                ):
                    return False
                scheduled = self._strict_aware_datetime(plan["scheduled_for"])
                if scheduled is None:
                    return False
                source_validation_time = requested_source_validation or scheduled
                if source_validation_time < scheduled:
                    return False
                not_before = self._strict_aware_datetime(current["not_before"])
                if current["not_before"] is not None and (
                    not_before is None or not_before > scheduled
                ):
                    return False
                source_ids = self._decode_source_ids(current["source_ids_json"])
                if source_ids is None or not self._eligible_content_sources_in_conn(
                    conn, source_ids, source_validation_time
                ):
                    return False
                if not self._queue_media_binding_valid_in_conn(conn, current):
                    return False
                if (
                    max_links_per_week is not None
                    and re.search(r"https?://", current["text"])
                ):
                    since = scheduled - timedelta(days=7)
                    link_count = 0
                    posted_links = conn.execute("""
                        SELECT created_at FROM posted_tweets WHERE has_link = 1
                    """).fetchall()
                    for row in posted_links:
                        try:
                            created = self._parse_datetime(row["created_at"])
                        except (TypeError, ValueError):
                            link_count += 1
                            continue
                        if since <= created <= scheduled:
                            link_count += 1
                    planned_links = conn.execute("""
                        SELECT p.scheduled_for, d.text
                        FROM publication_plans p
                        JOIN post_drafts d ON d.id = p.draft_id
                        WHERE p.id != ?
                          AND p.status IN ('planned', 'publishing', 'unknown')
                    """, (plan_id,)).fetchall()
                    for row in planned_links:
                        planned_at = self._strict_aware_datetime(row["scheduled_for"])
                        if (
                            planned_at is None
                            or not isinstance(row["text"], str)
                        ):
                            link_count += 1
                        elif (
                            since <= planned_at <= scheduled + timedelta(days=7)
                            and re.search(r"https?://", row["text"])
                        ):
                            link_count += 1
                    if link_count >= max_links_per_week:
                        return False
                try:
                    timing_reason = json.loads(plan["selection_reason_json"])
                    ranking_reason = json.loads(reason_json)
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    RecursionError,
                ):
                    return False
                if type(timing_reason) is not dict or type(ranking_reason) is not dict:
                    return False
                merged_reason = {
                    key: timing_reason[key]
                    for key in ("timing_bucket", "timing_reason")
                    if key in timing_reason
                }
                merged_reason.update(ranking_reason)
                try:
                    merged_reason_json = json.dumps(
                        merged_reason,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    return False
                if len(merged_reason_json.encode("utf-8")) > 1024:
                    return False
                cursor = conn.execute("""
                    UPDATE publication_plans
                    SET draft_id = ?, draft_revision = ?, status = 'planned',
                        selection_reason_json = ?, updated_at = ?,
                        revision = revision + 1
                    WHERE id = ? AND revision = ? AND status = 'open'
                      AND draft_id IS NULL
                """, (
                    draft_id,
                    expected_draft_revision,
                    merged_reason_json,
                    self._now_iso(),
                    plan_id,
                    plan["revision"],
                ))
                return cursor.rowcount == 1
        except (sqlite3.IntegrityError, OSError, ValueError):
            return False

    def mark_publication_plan_simulated(
        self,
        plan_id: int,
        expected_revision: int,
    ) -> bool:
        if (
            not self._exact_positive_identifier(plan_id)
            or not self._exact_nonnegative_revision(expected_revision)
        ):
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""
                UPDATE publication_plans
                SET status = 'simulated', claim_token = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND revision = ? AND status = 'planned'
            """, (self._now_iso(), plan_id, expected_revision))
            return cursor.rowcount == 1

    def get_simulated_draft_ids(self) -> Set[int]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT draft_id FROM publication_plans
                WHERE status = 'simulated' AND draft_id IS NOT NULL
            """).fetchall()
        return {
            row["draft_id"]
            for row in rows
            if self._exact_positive_identifier(row["draft_id"])
        }

    def get_publication_plan(self, plan_id: int) -> Optional[Dict]:
        if not self._exact_positive_identifier(plan_id):
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return self._decode_publication_plan(row)

    @staticmethod
    def _publication_plan_matches_claim(
        row: Optional[sqlite3.Row],
        claim: PublicationPlanClaim,
    ) -> bool:
        return (
            row is not None
            and isinstance(claim, PublicationPlanClaim)
            and row["id"] == claim.plan_id
            and row["status"] == "publishing"
            and row["revision"] == claim.plan_revision
            and row["draft_id"] == claim.draft_id
            and row["draft_revision"] == claim.draft_revision
            and row["scheduled_for"] == claim.scheduled_for
            and row["claim_token"] == claim.claim_token
            and row["published_tweet_id"] is None
        )

    def _due_publication_plan_eligible_in_conn(
        self,
        conn,
        plan: sqlite3.Row,
        draft: sqlite3.Row,
        current: datetime,
        grace_minutes: int,
    ) -> bool:
        scheduled = self._strict_aware_datetime(
            plan["scheduled_for"] if plan is not None else None
        )
        pause = conn.execute(
            "SELECT value FROM bot_state WHERE key = 'paused'"
        ).fetchone()
        if (
            plan is None
            or plan["status"] != "planned"
            or scheduled is None
            or current < scheduled
            or current > scheduled + timedelta(minutes=grace_minutes)
            or pause is None
            or pause["value"] != "false"
            or draft is None
            or draft["status"] != "approved"
            or draft["revision"] != plan["draft_revision"]
            or draft["translation_status"] != "ready"
            or type(draft["translation_it"]) is not str
            or not draft["translation_it"].strip()
            or draft["blocked_reason"] is not None
        ):
            return False
        source_ids = self._decode_source_ids(draft["source_ids_json"])
        return bool(
            source_ids is not None
            and self._eligible_content_sources_in_conn(conn, source_ids, current)
            and self._queue_media_binding_valid_in_conn(conn, draft)
        )

    def simulate_due_publication_plan(
        self,
        plan_id: int,
        expected_revision: int,
        now: datetime,
        grace_minutes: int,
    ) -> bool:
        current = self._strict_aware_datetime(now)
        if (
            not self._exact_positive_identifier(plan_id)
            or not self._exact_nonnegative_revision(expected_revision)
            or current is None
            or type(grace_minutes) is not int
            or not 1 <= grace_minutes <= 1440
        ):
            return False
        initial = self.get_publication_plan(plan_id)
        if initial is None or not self._exact_positive_identifier(
            initial.get("draft_id")
        ):
            return False
        draft_id = initial["draft_id"]
        with self._post_draft_mutation_lock(draft_id) as conn:
            plan = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            draft = self._queue_draft_row_in_conn(conn, draft_id)
            if (
                plan is None
                or plan["revision"] != expected_revision
                or plan["draft_id"] != draft_id
                or not self._due_publication_plan_eligible_in_conn(
                    conn, plan, draft, current, grace_minutes,
                )
            ):
                return False
            cursor = conn.execute("""
                UPDATE publication_plans
                SET status = 'simulated', claim_token = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND revision = ? AND status = 'planned'
            """, (self._now_iso(), plan_id, expected_revision))
            return cursor.rowcount == 1

    def claim_due_publication_plan(
        self,
        plan_id: int,
        expected_plan_revision: int,
        now: datetime,
        grace_minutes: int,
    ) -> Optional[Tuple[Dict, PostDraftPublicationClaim, PublicationPlanClaim]]:
        current = self._strict_aware_datetime(now)
        if (
            not self._exact_positive_identifier(plan_id)
            or not self._exact_nonnegative_revision(expected_plan_revision)
            or current is None
            or type(grace_minutes) is not int
            or not 1 <= grace_minutes <= 1440
        ):
            return None
        initial = self.get_publication_plan(plan_id)
        if (
            initial is None
            or initial.get("status") != "planned"
            or not self._exact_positive_identifier(initial.get("draft_id"))
        ):
            return None
        draft_id = initial["draft_id"]
        with self._post_draft_mutation_lock(draft_id) as conn:
            plan = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            draft = self._queue_draft_row_in_conn(conn, draft_id)
            if (
                plan is None
                or plan["revision"] != expected_plan_revision
                or plan["draft_id"] != draft_id
                or not self._due_publication_plan_eligible_in_conn(
                    conn, plan, draft, current, grace_minutes,
                )
            ):
                return None
            token = secrets.token_urlsafe(24)
            updated_at = self._now_iso()
            draft_update = conn.execute("""
                UPDATE post_drafts
                SET status = 'publishing', updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'approved' AND revision = ?
                  AND published_tweet_id IS NULL
            """, (updated_at, draft_id, draft["revision"]))
            if draft_update.rowcount != 1:
                return None
            claimed_draft_row = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            draft_claim = self._publication_claim_from_row(claimed_draft_row)
            plan_update = conn.execute("""
                UPDATE publication_plans
                SET status = 'publishing', claim_token = ?,
                    draft_revision = ?, updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'planned' AND revision = ?
                  AND draft_id = ?
            """, (
                token,
                draft_claim.revision,
                updated_at,
                plan_id,
                expected_plan_revision,
                draft_id,
            ))
            if plan_update.rowcount != 1:
                raise sqlite3.IntegrityError("publication_plan_claim_conflict")
            claimed_plan_row = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            plan_claim = PublicationPlanClaim(
                plan_id=claimed_plan_row["id"],
                plan_revision=claimed_plan_row["revision"],
                draft_id=draft_id,
                draft_revision=draft_claim.revision,
                scheduled_for=claimed_plan_row["scheduled_for"],
                claim_token=token,
            )
            return self._decode_post_draft(claimed_draft_row), draft_claim, plan_claim

    def validate_publication_plan_media(
        self,
        draft_claim: PostDraftPublicationClaim,
        plan_claim: PublicationPlanClaim,
        expected_media: Mapping,
    ) -> bool:
        if (
            not isinstance(draft_claim, PostDraftPublicationClaim)
            or not isinstance(plan_claim, PublicationPlanClaim)
        ):
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_claim.draft_id,)
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_claim.plan_id,)
            ).fetchone()
            return (
                self._post_draft_matches_publication_claim(draft, draft_claim)
                and self._publication_plan_matches_claim(plan, plan_claim)
                and self._publication_media_matches_in_conn(
                    conn, draft_claim, expected_media,
                )
            )

    def finalize_publication_plan(
        self,
        draft_claim: PostDraftPublicationClaim,
        plan_claim: PublicationPlanClaim,
        tweet_id: str,
        expected_media: Optional[Mapping] = None,
    ) -> bool:
        if (
            not isinstance(draft_claim, PostDraftPublicationClaim)
            or not isinstance(plan_claim, PublicationPlanClaim)
            or not self._canonical_x_tweet_id(tweet_id)
        ):
            return False
        with self._media_store_mutation_lock(
            "draft_reservations", draft_claim.draft_id,
        ) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_claim.draft_id,)
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_claim.plan_id,)
            ).fetchone()
            if (
                not self._post_draft_matches_publication_claim(draft, draft_claim)
                or not self._publication_plan_matches_claim(plan, plan_claim)
                or not self._publication_media_matches_in_conn(
                    conn, draft_claim, expected_media,
                )
            ):
                return False
            try:
                score_data = json.loads(draft_claim.score_json)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                score_data = {}
            score_total = score_data.get("total") if type(score_data) is dict else None
            if type(score_total) is not int or not 0 <= score_total <= 100:
                score_total = None
            completed_at = self._now_iso()
            conn.execute("""
                INSERT INTO posted_tweets (
                    tweet_id, text, category, topic, has_link, score_total,
                    agent_used, created_at
                ) VALUES (?, ?, ?, '', ?, ?, 'adaptive_approved_publisher', ?)
            """, (
                tweet_id,
                draft_claim.text,
                draft_claim.category,
                int(bool(re.search(r"https?://", draft_claim.text))),
                score_total,
                completed_at,
            ))
            if draft_claim.media_id is not None:
                media_update = conn.execute("""
                    UPDATE media_library
                    SET used = 1, used_at = ?, used_in_tweet_id = ?,
                        lifecycle_state = 'used', reserved_by_draft_id = NULL
                    WHERE id = ? AND lifecycle_state = 'reserved'
                      AND reserved_by_draft_id = ? AND file_deleted = 0
                """, (
                    completed_at,
                    tweet_id,
                    draft_claim.media_id,
                    draft_claim.draft_id,
                ))
                if media_update.rowcount != 1:
                    raise sqlite3.IntegrityError("planned_media_changed")
            draft_update = conn.execute("""
                UPDATE post_drafts
                SET status = 'published', published_tweet_id = ?, error = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'publishing' AND revision = ?
            """, (
                tweet_id,
                completed_at,
                draft_claim.draft_id,
                draft_claim.revision,
            ))
            plan_update = conn.execute("""
                UPDATE publication_plans
                SET status = 'published', published_tweet_id = ?,
                    claim_token = NULL, updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'publishing' AND revision = ?
                  AND claim_token = ?
            """, (
                tweet_id,
                completed_at,
                plan_claim.plan_id,
                plan_claim.plan_revision,
                plan_claim.claim_token,
            ))
            if draft_update.rowcount != 1 or plan_update.rowcount != 1:
                raise sqlite3.IntegrityError("planned_publication_finalization_conflict")
            self._insert_draft_evaluation_in_conn(
                conn,
                plan_claim.scheduled_for,
                draft_claim.category,
                "published",
                {
                    "draft_id": draft_claim.draft_id,
                    "plan_id": plan_claim.plan_id,
                },
                completed_at,
            )
            return True

    def _transition_publication_plan_claim(
        self,
        draft_claim: PostDraftPublicationClaim,
        plan_claim: PublicationPlanClaim,
        *,
        plan_status: str,
        draft_status: str,
        safe_error: Optional[str],
    ) -> bool:
        if (
            not isinstance(draft_claim, PostDraftPublicationClaim)
            or not isinstance(plan_claim, PublicationPlanClaim)
            or plan_status not in {"planned", "skipped", "unknown"}
            or draft_status not in {"approved", "publication_unknown"}
        ):
            return False
        sanitized = (
            self._sanitize_persisted_text(safe_error)
            if safe_error is not None
            else None
        )
        with self._post_draft_mutation_lock(draft_claim.draft_id) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_claim.draft_id,)
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_claim.plan_id,)
            ).fetchone()
            if (
                not self._post_draft_matches_publication_claim(draft, draft_claim)
                or not self._publication_plan_matches_claim(plan, plan_claim)
            ):
                return False
            changed_at = self._now_iso()
            draft_update = conn.execute("""
                UPDATE post_drafts
                SET status = ?, error = ?, updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'publishing' AND revision = ?
                  AND published_tweet_id IS NULL
            """, (
                draft_status,
                sanitized,
                changed_at,
                draft_claim.draft_id,
                draft_claim.revision,
            ))
            new_draft_revision = draft_claim.revision + 1
            plan_update = conn.execute("""
                UPDATE publication_plans
                SET status = ?, claim_token = NULL, draft_revision = ?,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'publishing' AND revision = ?
                  AND claim_token = ?
            """, (
                plan_status,
                new_draft_revision,
                changed_at,
                plan_claim.plan_id,
                plan_claim.plan_revision,
                plan_claim.claim_token,
            ))
            if draft_update.rowcount != 1 or plan_update.rowcount != 1:
                raise sqlite3.IntegrityError("planned_publication_transition_conflict")
            return True

    def restore_publication_plan_claim(
        self,
        draft_claim: PostDraftPublicationClaim,
        plan_claim: PublicationPlanClaim,
        now: datetime,
        grace_minutes: int,
        safe_error: Optional[str] = None,
    ) -> bool:
        current = self._strict_aware_datetime(now)
        scheduled = self._strict_aware_datetime(plan_claim.scheduled_for)
        if (
            current is None
            or scheduled is None
            or type(grace_minutes) is not int
            or not 1 <= grace_minutes <= 1440
        ):
            return False
        within_grace = current <= scheduled + timedelta(minutes=grace_minutes)
        return self._transition_publication_plan_claim(
            draft_claim,
            plan_claim,
            plan_status="planned" if within_grace else "skipped",
            draft_status="approved",
            safe_error=safe_error,
        )

    def mark_publication_plan_unknown(
        self,
        draft_claim: PostDraftPublicationClaim,
        plan_claim: PublicationPlanClaim,
        safe_error: str,
    ) -> bool:
        return self._transition_publication_plan_claim(
            draft_claim,
            plan_claim,
            plan_status="unknown",
            draft_status="publication_unknown",
            safe_error=safe_error,
        )

    def skip_expired_publication_plan(
        self,
        plan_id: int,
        expected_revision: int,
        now: datetime,
        grace_minutes: int,
    ) -> bool:
        current = self._strict_aware_datetime(now)
        if (
            not self._exact_positive_identifier(plan_id)
            or not self._exact_nonnegative_revision(expected_revision)
            or current is None
            or type(grace_minutes) is not int
            or not 1 <= grace_minutes <= 1440
        ):
            return False
        plan = self.get_publication_plan(plan_id)
        if plan is None or not self._exact_positive_identifier(plan.get("draft_id")):
            return False
        scheduled = self._strict_aware_datetime(plan.get("scheduled_for"))
        if scheduled is None or current <= scheduled + timedelta(minutes=grace_minutes):
            return False
        with self._post_draft_mutation_lock(plan["draft_id"]) as conn:
            current_plan = conn.execute(
                "SELECT * FROM publication_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            current_draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (plan["draft_id"],)
            ).fetchone()
            if (
                current_plan is None
                or current_plan["status"] != "planned"
                or current_plan["revision"] != expected_revision
                or current_draft is None
                or current_draft["status"] != "approved"
                or current_draft["revision"] != current_plan["draft_revision"]
            ):
                return False
            cursor = conn.execute("""
                UPDATE publication_plans
                SET status = 'skipped', updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'planned' AND revision = ?
            """, (self._now_iso(), plan_id, expected_revision))
            return cursor.rowcount == 1

    def get_recent_content_texts(
        self,
        days: int = 30,
        exclude_draft_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> List[str]:
        current = self._as_utc(now or datetime.now(timezone.utc))
        since = (current - timedelta(days=days)).isoformat()
        until = current.isoformat()
        with self._conn() as conn:
            if exclude_draft_id is None:
                rows = conn.execute("""
                    SELECT text, created_at FROM post_drafts
                    WHERE created_at >= ? AND created_at <= ?
                    UNION ALL
                    SELECT text, created_at FROM posted_tweets
                    WHERE created_at >= ? AND created_at <= ?
                    ORDER BY created_at DESC
                """, (since, until, since, until)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT text, created_at FROM post_drafts
                    WHERE created_at >= ? AND created_at <= ? AND id != ?
                    UNION ALL
                    SELECT text, created_at FROM posted_tweets
                    WHERE created_at >= ? AND created_at <= ?
                    ORDER BY created_at DESC
                """, (since, until, exclude_draft_id, since, until)).fetchall()
        return [row["text"] for row in rows]

    def postpone_post_draft_atomic(
        self,
        draft_id: int,
        expected_revision: int,
        expected_statuses: List[str],
        new_slot: str,
    ) -> bool:
        """Move a draft with revision CAS while atomically claiming the slot."""
        if not expected_statuses:
            return False
        new_slot = self._normalize_datetime_iso(new_slot)
        placeholders = ", ".join("?" for _ in expected_statuses)
        try:
            with self._post_draft_mutation_lock(draft_id) as conn:
                current = conn.execute(
                    "SELECT status, revision FROM post_drafts WHERE id = ?",
                    (draft_id,),
                ).fetchone()
                if (
                    current is None
                    or current["revision"] != expected_revision
                    or current["status"] not in expected_statuses
                ):
                    return False
                occupied = conn.execute(
                    "SELECT id FROM post_drafts WHERE intended_slot = ? "
                    "AND id != ? AND status IN ("
                    + _LIVE_DRAFT_STATUS_SQL
                    + ") LIMIT 1",
                    (new_slot, draft_id),
                ).fetchone()
                if occupied:
                    return False
                cursor = conn.execute(
                    "UPDATE post_drafts SET status = 'pending_approval', "
                    "intended_slot = ?, approved_at = NULL, "
                    "approved_by = NULL, updated_at = ?, "
                    "revision = revision + 1 WHERE id = ? AND revision = ? "
                    "AND status IN (" + placeholders + ")",
                    [
                        new_slot,
                        self._now_iso(),
                        draft_id,
                        expected_revision,
                        *expected_statuses,
                    ],
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def postpone_post_draft_consuming_state_atomic(
        self,
        *,
        state_key: str,
        expected_state_value: str,
        draft_id: int,
        expected_revision: int,
        expected_statuses: List[str],
        new_slot: str,
    ) -> str:
        """Consume one exact Telegram session with one draft postponement."""
        if not expected_statuses:
            return "draft_conflict"
        new_slot = self._normalize_datetime_iso(new_slot)
        placeholders = ", ".join("?" for _ in expected_statuses)
        with self._post_draft_mutation_lock(draft_id) as conn:
            consumed = conn.execute(
                "DELETE FROM bot_state WHERE key = ? AND value = ?",
                (state_key, expected_state_value),
            )
            if consumed.rowcount != 1:
                return "session_conflict"
            current = conn.execute(
                "SELECT status, revision FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if (
                current is None
                or current["revision"] != expected_revision
                or current["status"] not in expected_statuses
            ):
                conn.rollback()
                return "draft_conflict"
            occupied = conn.execute(
                "SELECT id FROM post_drafts WHERE intended_slot = ? "
                "AND id != ? AND status IN ("
                + _LIVE_DRAFT_STATUS_SQL
                + ") LIMIT 1",
                (new_slot, draft_id),
            ).fetchone()
            if occupied:
                conn.rollback()
                return "slot_conflict"
            cursor = conn.execute(
                "UPDATE post_drafts SET status = 'pending_approval', "
                "intended_slot = ?, approved_at = NULL, "
                "approved_by = NULL, updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND revision = ? "
                "AND status IN (" + placeholders + ")",
                [
                    new_slot,
                    self._now_iso(),
                    draft_id,
                    expected_revision,
                    *expected_statuses,
                ],
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return "draft_conflict"
            return "postponed"

    def approve_post_draft_atomic(
        self,
        draft_id: int,
        expected_revision: int,
        expected_slot: str,
        approved_by: str,
        now_fn=None,
    ) -> bool:
        """Approve one exact pending revision before its unchanged slot."""
        expected_slot = self._normalize_datetime_iso(expected_slot)
        with self._post_draft_mutation_lock(draft_id) as conn:
            raw_now = (
                now_fn() if callable(now_fn) else datetime.now(timezone.utc)
            )
            if isinstance(raw_now, str):
                try:
                    current_time = self._parse_datetime(raw_now)
                except (TypeError, ValueError):
                    return False
            elif isinstance(raw_now, datetime):
                current_time = self._as_utc(raw_now)
            else:
                return False
            current = conn.execute(
                "SELECT status, revision, intended_slot FROM post_drafts "
                "WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if (
                current is None
                or current["status"] != "pending_approval"
                or current["revision"] != expected_revision
                or current["intended_slot"] != expected_slot
            ):
                return False
            if current_time >= self._parse_datetime(current["intended_slot"]):
                expired = conn.execute("""
                    UPDATE post_drafts
                    SET status = 'expired', updated_at = ?,
                        revision = revision + 1
                    WHERE id = ? AND status = 'pending_approval'
                      AND revision = ? AND intended_slot = ?
                """, (
                    self._now_iso(),
                    draft_id,
                    expected_revision,
                    expected_slot,
                ))
                if expired.rowcount != 1:
                    return False
                return False
            cursor = conn.execute("""
                UPDATE post_drafts
                SET status = 'approved', approved_at = ?, approved_by = ?,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'pending_approval'
                  AND revision = ? AND intended_slot = ?
            """, (
                current_time.isoformat(),
                approved_by,
                self._now_iso(),
                draft_id,
                expected_revision,
                expected_slot,
            ))
            return cursor.rowcount == 1

    def replace_post_draft_atomic(
        self,
        *,
        prior_draft_id: int,
        expected_revision: int,
        expected_slot: str,
        expected_category: str,
        expected_source_ids: List[int],
        text: str,
        score_data: Dict,
        publication_key: str,
    ) -> Tuple[Optional[Dict], str]:
        """Supersede and replace one exact draft in a single transaction."""
        expected_slot = self._normalize_datetime_iso(expected_slot)
        now = self._now_iso()
        with self._media_store_mutation_lock(
            "draft_reservations", prior_draft_id,
        ) as conn:
            prior = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (prior_draft_id,)
            ).fetchone()
            if prior is None:
                return None, "conflict"
            prior_source_ids = json.loads(prior["source_ids_json"])
            if (
                prior["revision"] != expected_revision
                or prior["status"]
                not in ("pending_approval", "approved", "expired")
                or prior["intended_slot"] != expected_slot
                or prior["category"] != expected_category
                or prior_source_ids != expected_source_ids
            ):
                return None, "conflict"
            if not self._eligible_content_sources_in_conn(
                conn, expected_source_ids
            ):
                return None, "no_eligible_source"

            cursor = conn.execute(
                "UPDATE post_drafts SET status = 'superseded', updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND revision = ? "
                "AND intended_slot = ? AND status IN "
                "('pending_approval', 'approved', 'expired')",
                (now, prior_draft_id, expected_revision, expected_slot),
            )
            if cursor.rowcount != 1:
                return None, "conflict"
            replacement = conn.execute("""
                INSERT INTO post_drafts (
                    publication_key, text, category, source_ids_json,
                    score_json, intended_slot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                publication_key,
                text,
                expected_category,
                json.dumps(expected_source_ids),
                json.dumps(score_data),
                expected_slot,
                now,
                now,
            ))
            replacement_id = replacement.lastrowid
            conn.execute("""
                INSERT INTO editorial_queue (
                    draft_id, translation_status, created_at, updated_at
                ) VALUES (?, 'pending', ?, ?)
            """, (replacement_id, now, now))
            self._insert_draft_evaluation_in_conn(
                conn,
                expected_slot,
                expected_category,
                "pending_approval",
                {
                    "draft_id": replacement_id,
                    "source_ids": expected_source_ids,
                    "scores": score_data,
                    "supersedes_draft_id": prior_draft_id,
                },
                now,
            )
            conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'available', reserved_by_draft_id = NULL
                WHERE reserved_by_draft_id = ? AND lifecycle_state = 'reserved'
            """, (prior_draft_id,))
            row = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (replacement_id,)
            ).fetchone()
            return self._decode_post_draft(row), "created"

    def replace_post_draft_consuming_state_atomic(
        self,
        *,
        state_key: str,
        expected_state_value: str,
        prior_draft_id: int,
        expected_revision: int,
        expected_slot: str,
        expected_category: str,
        expected_source_ids: List[int],
        text: str,
        score_data: Dict,
        publication_key: str,
    ):
        """Consume one exact Telegram session with one draft replacement."""
        expected_slot = self._normalize_datetime_iso(expected_slot)
        now = self._now_iso()
        with self._media_store_mutation_lock(
            "draft_reservations", prior_draft_id,
        ) as conn:
            consumed = conn.execute(
                "DELETE FROM bot_state WHERE key = ? AND value = ?",
                (state_key, expected_state_value),
            )
            if consumed.rowcount != 1:
                existing = conn.execute(
                    "SELECT * FROM post_drafts WHERE publication_key = ?",
                    (publication_key,),
                ).fetchone()
                if existing and self._telegram_edit_retry_matches_in_conn(
                    conn,
                    existing=existing,
                    prior_draft_id=prior_draft_id,
                    text=text,
                    score_data=score_data,
                    expected_slot=expected_slot,
                    expected_category=expected_category,
                    expected_source_ids=expected_source_ids,
                ):
                    return self._decode_post_draft(existing), "already_applied"
                return None, "session_conflict"
            prior = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (prior_draft_id,)
            ).fetchone()
            prior_source_ids = (
                json.loads(prior["source_ids_json"]) if prior else None
            )
            if (
                prior is None
                or prior["revision"] != expected_revision
                or prior["status"] != "pending_approval"
                or prior["intended_slot"] != expected_slot
                or prior["category"] != expected_category
                or prior_source_ids != expected_source_ids
            ):
                conn.rollback()
                return None, "conflict"
            if not self._eligible_content_sources_in_conn(
                conn, expected_source_ids
            ):
                conn.rollback()
                return None, "no_eligible_source"
            cursor = conn.execute(
                "UPDATE post_drafts SET status = 'superseded', updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND revision = ? "
                "AND intended_slot = ? AND status = 'pending_approval'",
                (now, prior_draft_id, expected_revision, expected_slot),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None, "conflict"
            replacement = conn.execute("""
                INSERT INTO post_drafts (
                    publication_key, text, category, source_ids_json,
                    score_json, intended_slot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                publication_key,
                text,
                expected_category,
                json.dumps(expected_source_ids),
                json.dumps(score_data),
                expected_slot,
                now,
                now,
            ))
            replacement_id = replacement.lastrowid
            conn.execute("""
                INSERT INTO editorial_queue (
                    draft_id, translation_status, created_at, updated_at
                ) VALUES (?, 'pending', ?, ?)
            """, (replacement_id, now, now))
            self._insert_draft_evaluation_in_conn(
                conn,
                expected_slot,
                expected_category,
                "pending_approval",
                {
                    "draft_id": replacement_id,
                    "source_ids": expected_source_ids,
                    "scores": score_data,
                    "supersedes_draft_id": prior_draft_id,
                },
                now,
            )
            conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'available', reserved_by_draft_id = NULL
                WHERE reserved_by_draft_id = ? AND lifecycle_state = 'reserved'
            """, (prior_draft_id,))
            row = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (replacement_id,)
            ).fetchone()
            return self._decode_post_draft(row), "created"

    @staticmethod
    def _telegram_edit_retry_matches_in_conn(
        conn,
        *,
        existing,
        prior_draft_id: int,
        text: str,
        score_data: Dict,
        expected_slot: str,
        expected_category: str,
        expected_source_ids: List[int],
    ) -> bool:
        try:
            sources_match = json.loads(existing["source_ids_json"]) == expected_source_ids
            score_matches = json.loads(existing["score_json"]) == score_data
        except (TypeError, ValueError):
            return False
        if not (
            existing["text"] == text
            and existing["category"] == expected_category
            and existing["intended_slot"] == expected_slot
            and sources_match
            and score_matches
        ):
            return False
        audits = conn.execute(
            "SELECT details_json FROM draft_evaluations "
            "WHERE intended_slot = ? AND category = ? "
            "AND outcome = 'pending_approval' ORDER BY id DESC",
            (expected_slot, expected_category),
        ).fetchall()
        for audit in audits:
            try:
                details = json.loads(audit["details_json"])
            except (TypeError, ValueError):
                continue
            if (
                isinstance(details, dict)
                and details.get("draft_id") == existing["id"]
                and details.get("supersedes_draft_id") == prior_draft_id
                and details.get("source_ids") == expected_source_ids
                and details.get("scores") == score_data
            ):
                return True
        return False

    def claim_post_draft_for_publication(
        self,
        draft_id: int,
        expected_revision: int,
    ) -> Optional[Tuple[Dict, PostDraftPublicationClaim]]:
        """Atomically claim exactly the approved revision the caller read.

        The returned draft is selected after the transition in the same
        transaction.  Callers must publish only that snapshot and carry the
        returned token through every terminal transition.
        """
        if (
            isinstance(draft_id, bool)
            or not isinstance(draft_id, int)
            or draft_id <= 0
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return None
        with self._post_draft_mutation_lock(draft_id) as conn:
            candidate = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if (
                candidate is None
                or candidate["status"] != "approved"
                or candidate["revision"] != expected_revision
                or candidate["published_tweet_id"] is not None
            ):
                return None
            claimed_at = self._now_iso()
            updated = conn.execute("""
                UPDATE post_drafts
                SET status = 'publishing', updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND status = 'approved' AND revision = ?
                  AND published_tweet_id IS NULL
            """, (claimed_at, draft_id, expected_revision))
            if updated.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            claim = self._publication_claim_from_row(row)
            return self._decode_post_draft(row), claim

    def validate_post_draft_publication_media(
        self,
        claim: PostDraftPublicationClaim,
        expected_media: Mapping,
    ) -> bool:
        """Re-read claim and reservation under the caller's media root lease.

        Publisher calls this only after ``open_verified_media`` has acquired
        the root lease.  Taking SQLite's write lock second preserves the
        application-wide root-then-database lock ordering.
        """
        if not isinstance(claim, PostDraftPublicationClaim):
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?",
                (claim.draft_id,),
            ).fetchone()
            return (
                self._post_draft_matches_publication_claim(draft, claim)
                and self._publication_media_matches_in_conn(
                    conn, claim, expected_media,
                )
            )

    def validate_post_draft_preview_media(
        self,
        expected_draft: Mapping,
        expected_media: Mapping,
    ) -> bool:
        """Revalidate one preview binding under the caller's media lease."""
        if not isinstance(expected_draft, Mapping) or not isinstance(
            expected_media, Mapping,
        ):
            return False
        draft_id = expected_draft.get("id")
        media_id = expected_media.get("id")
        if type(draft_id) is not int or type(media_id) is not int:
            return False
        try:
            expected_draft_values = tuple(
                expected_draft[column]
                for column in _DRAFT_BINDING_SNAPSHOT_COLUMNS
            )
            expected_media_values = tuple(
                expected_media[column]
                for column in _PREVIEW_MEDIA_SNAPSHOT_COLUMNS
            )
        except (KeyError, TypeError):
            return False
        status = expected_draft.get("status")
        if status == "published":
            tweet_id = expected_draft.get("published_tweet_id")
            binding_valid = (
                isinstance(tweet_id, str)
                and bool(tweet_id)
                and expected_media.get("lifecycle_state") == "used"
                and expected_media.get("used_in_tweet_id") == tweet_id
            )
        else:
            binding_valid = (
                status in {"pending_approval", "approved", "publishing"}
                and expected_media.get("lifecycle_state") == "reserved"
                and expected_media.get("reserved_by_draft_id") == draft_id
            )
        if not binding_valid or expected_draft.get("media_id") != media_id:
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_id,),
            ).fetchone()
            current_media = conn.execute(
                "SELECT * FROM media_library WHERE id = ?", (media_id,),
            ).fetchone()
            return (
                current_draft is not None
                and current_media is not None
                and tuple(
                    current_draft[column]
                    for column in _DRAFT_BINDING_SNAPSHOT_COLUMNS
                ) == expected_draft_values
                and tuple(
                    current_media[column]
                    for column in _PREVIEW_MEDIA_SNAPSHOT_COLUMNS
                ) == expected_media_values
            )

    def transition_post_draft(
        self,
        draft_id: int,
        expected_statuses: List[str],
        new_status: str,
        **changes: Any,
    ) -> bool:
        allowed = {
            "text", "score_json", "intended_slot", "media_id", "approved_at",
            "approved_by", "published_tweet_id", "error", "updated_at",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(
                "Unsupported draft fields: " + ", ".join(sorted(invalid))
            )
        changed_media_id = changes.get("media_id")
        if (
            "media_id" in changes
            and changed_media_id is not None
            and (
                type(changed_media_id) is not int
                or changed_media_id <= 0
            )
        ):
            raise ValueError("invalid_media_id")
        if not expected_statuses:
            return False
        assignments = [
            "status = ?",
            "updated_at = ?",
            "revision = revision + 1",
        ]
        values = [new_status, self._now_iso()]
        for name, value in changes.items():
            if name == "score_json" and not isinstance(value, str):
                value = json.dumps(value)
            elif name == "intended_slot":
                value = self._normalize_datetime_iso(value)
            assignments.append(name + " = ?")
            values.append(value)
        placeholders = ", ".join("?" for _ in expected_statuses)
        values.extend([draft_id] + list(expected_statuses))
        extra_media_ids = ()
        if (
            type(changed_media_id) is int
            and changed_media_id > 0
        ):
            extra_media_ids = (changed_media_id,)
        try:
            with self._post_draft_mutation_lock(
                draft_id,
                extra_media_ids=extra_media_ids,
            ) as conn:
                cursor = conn.execute(
                    "UPDATE post_drafts SET " + ", ".join(assignments)
                    + " WHERE id = ? AND status IN (" + placeholders + ")",
                    values,
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def finalize_post_draft_publication(
        self,
        claim: PostDraftPublicationClaim,
        tweet_id: str,
        expected_media: Optional[Mapping] = None,
    ) -> bool:
        """Atomically persist a confirmed tweet, media use and draft state."""
        if type(tweet_id) is not str or not tweet_id:
            raise ValueError("invalid_tweet_id")
        if not isinstance(claim, PostDraftPublicationClaim):
            return False
        with self._media_store_mutation_lock(
            "draft_reservations", claim.draft_id,
        ) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (claim.draft_id,)
            ).fetchone()
            if not self._post_draft_matches_publication_claim(draft, claim):
                return False
            if not self._publication_media_matches_in_conn(
                conn, claim, expected_media,
            ):
                return False

            try:
                score_data = json.loads(claim.score_json or "{}")
            except (TypeError, ValueError):
                score_data = {}
            score_total = (
                score_data.get("total")
                if isinstance(score_data, dict)
                else None
            )
            created_at = self._now_iso()
            conn.execute("""
                INSERT INTO posted_tweets (
                    tweet_id, text, category, topic, has_link, score_total,
                    agent_used, created_at
                ) VALUES (?, ?, ?, '', ?, ?, 'approved_publisher', ?)
            """, (
                tweet_id,
                claim.text,
                claim.category,
                int(bool(re.search(r"https?://", claim.text))),
                score_total,
                created_at,
            ))

            if claim.media_id is not None:
                media_update = conn.execute("""
                    UPDATE media_library
                    SET used = 1, used_at = ?, used_in_tweet_id = ?,
                        lifecycle_state = 'used', reserved_by_draft_id = NULL
                    WHERE id = ? AND lifecycle_state = 'reserved'
                      AND reserved_by_draft_id = ? AND file_deleted = 0
                """, (
                    created_at,
                    tweet_id,
                    claim.media_id,
                    claim.draft_id,
                ))
                if media_update.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        "media changed during publication finalization"
                    )

            draft_update = conn.execute("""
                UPDATE post_drafts
                SET status = 'published', published_tweet_id = ?, error = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'publishing'
                  AND revision = ? AND published_tweet_id IS NULL
            """, (
                tweet_id,
                created_at,
                claim.draft_id,
                claim.revision,
            ))
            if draft_update.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "draft changed during publication finalization"
                )
            return True

    def fail_post_draft_publication(
        self,
        claim: PostDraftPublicationClaim,
        safe_error: str,
    ) -> bool:
        """Atomically record a definite failure and release its reservation."""
        if not isinstance(claim, PostDraftPublicationClaim):
            return False
        safe_error = self._sanitize_persisted_text(safe_error)
        with self._media_store_mutation_lock(
            "draft_reservations", claim.draft_id,
        ) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?",
                (claim.draft_id,),
            ).fetchone()
            if not self._post_draft_matches_publication_claim(draft, claim):
                return False
            media_ids = [row["id"] for row in conn.execute("""
                SELECT id FROM media_library
                WHERE reserved_by_draft_id = ?
                  AND lifecycle_state = 'reserved'
                ORDER BY id
            """, (claim.draft_id,)).fetchall()]
            now = self._now_iso()
            updated = conn.execute("""
                UPDATE post_drafts
                SET status = 'publication_failed', error = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND status = 'publishing' AND revision = ?
            """, (
                safe_error,
                now,
                claim.draft_id,
                claim.revision,
            ))
            if updated.rowcount != 1:
                return False
            conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'available', reserved_by_draft_id = NULL
                WHERE reserved_by_draft_id = ?
                  AND lifecycle_state = 'reserved'
            """, (claim.draft_id,))
            for media_id in media_ids:
                self._insert_draft_evaluation_in_conn(
                    conn,
                    claim.intended_slot,
                    claim.category,
                    "media_released",
                    {"draft_id": claim.draft_id, "media_id": media_id},
                    now,
                )
            return True

    def _transition_post_draft_publication_claim(
        self,
        claim: PostDraftPublicationClaim,
        new_status: str,
        safe_error: Optional[str],
    ) -> bool:
        if (
            not isinstance(claim, PostDraftPublicationClaim)
            or new_status not in {"approved", "publication_unknown"}
        ):
            return False
        if safe_error is not None:
            safe_error = self._sanitize_persisted_text(safe_error)
        with self._post_draft_mutation_lock(claim.draft_id) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?",
                (claim.draft_id,),
            ).fetchone()
            if not self._post_draft_matches_publication_claim(draft, claim):
                return False
            updated = conn.execute("""
                UPDATE post_drafts
                SET status = ?, error = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND status = 'publishing' AND revision = ?
                  AND published_tweet_id IS NULL
            """, (
                new_status,
                safe_error,
                self._now_iso(),
                claim.draft_id,
                claim.revision,
            ))
            return updated.rowcount == 1

    def restore_post_draft_publication_claim(
        self,
        claim: PostDraftPublicationClaim,
    ) -> bool:
        """Return the exact unspent claim to approved after a late pause."""
        return self._transition_post_draft_publication_claim(
            claim, "approved", None,
        )

    def mark_post_draft_publication_unknown(
        self,
        claim: PostDraftPublicationClaim,
        safe_error: str,
    ) -> bool:
        """Persist ambiguity only for the exact claim that attempted X."""
        return self._transition_post_draft_publication_claim(
            claim, "publication_unknown", safe_error,
        )

    def record_draft_evaluation(
        self,
        intended_slot: str,
        category: str,
        outcome: str,
        details: Dict,
    ) -> int:
        intended_slot = self._normalize_datetime_iso(intended_slot)
        with self._conn() as conn:
            return self._insert_draft_evaluation_in_conn(
                conn,
                intended_slot,
                category,
                outcome,
                details,
            )

    def count_draft_evaluations(self, outcome: str, days: int = 7) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS count FROM draft_evaluations
                WHERE outcome = ? AND created_at >= ?
            """, (outcome, since)).fetchone()
        return row["count"] if row else 0

    # ---------- Leads / opportunity detector ----------

    def add_lead(self, tweet_id: str, author_username: str, author_id: str,
                 text: str, score: int, matched_keyword: str, action_suggested: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO leads (tweet_id, author_username, author_id, text, score,
                                    matched_keyword, action_suggested, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'nuovo', ?)
            """, (tweet_id, author_username, author_id, text, score, matched_keyword,
                  action_suggested, datetime.now().isoformat()))

    def lead_already_seen(self, tweet_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE tweet_id = ? "
                "UNION SELECT 1 FROM seen_tweets WHERE tweet_id = ?",
                (tweet_id, tweet_id),
            ).fetchone()
            return row is not None

    def mark_tweet_seen(self, tweet_id: str):
        """Ricorda un tweet già valutato (azione 'Ignora') senza salvarlo come lead."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_tweets (tweet_id, seen_at) VALUES (?, ?)",
                (tweet_id, datetime.now().isoformat()),
            )

    def get_open_leads(self, min_score: int = 0, limit: int = 50) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM leads WHERE status = 'nuovo' AND score >= ?
                ORDER BY score DESC LIMIT ?
            """, (min_score, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_all_leads(self, limit: int = 200) -> List[Dict]:
        """Tutti i lead (qualsiasi stato), più recenti prima. Per la dashboard."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM leads ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def update_lead_status(self, lead_id: int, status: str):
        with self._conn() as conn:
            conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))

    # ---------- Category weights / performance ----------

    def get_category_weight(self, category: str) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT weight FROM category_weights WHERE category = ?",
                                (category,)).fetchone()
            return row['weight'] if row else 1.0

    def get_all_category_weights(self) -> Dict[str, float]:
        with self._conn() as conn:
            rows = conn.execute("SELECT category, weight FROM category_weights").fetchall()
            return {r['category']: r['weight'] for r in rows}

    def update_category_weight(self, category: str, weight: float, avg_ctr: float = 0.0):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO category_weights (category, weight, avg_ctr, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET weight = ?, avg_ctr = ?, updated_at = ?
            """, (category, weight, avg_ctr, datetime.now().isoformat(),
                  weight, avg_ctr, datetime.now().isoformat()))

    def save_tweet_metrics(self, tweet_id: str, impressions: int, likes: int,
                           retweets: int, replies: int, bookmarks: int = 0):
        metrics = (impressions, likes, retweets, replies, bookmarks)
        if (
            not self._canonical_x_tweet_id(tweet_id)
            or any(type(value) is not int or value < 0 for value in metrics)
        ):
            return False
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tweet_metrics (tweet_id, impressions, likes, retweets, replies, bookmarks, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET
                    impressions=excluded.impressions, likes=excluded.likes,
                    retweets=excluded.retweets, replies=excluded.replies,
                    bookmarks=excluded.bookmarks, checked_at=excluded.checked_at
            """, (
                tweet_id,
                impressions,
                likes,
                retweets,
                replies,
                bookmarks,
                self._now_iso(),
            ))
        return True

    @staticmethod
    def _canonical_x_tweet_id(value: Any) -> bool:
        return (
            type(value) is str
            and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
            and int(value) <= (1 << 64) - 1
        )

    def get_publication_timing_samples(
        self,
        now: datetime,
        min_age_hours: int = 24,
    ) -> List:
        """Project only mature, canonical owned-post performance samples."""
        from modules.adaptive_timing import TimingSample

        current = self._strict_aware_datetime(now)
        if (
            current is None
            or type(min_age_hours) is not int
            or min_age_hours <= 0
            or min_age_hours > 24 * 365
        ):
            return []
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT p.tweet_id, p.created_at, m.checked_at,
                       m.impressions, m.likes, m.retweets, m.replies,
                       m.bookmarks,
                       (
                           SELECT COUNT(*) FROM posted_tweets duplicate
                           WHERE duplicate.tweet_id = p.tweet_id
                       ) AS posted_count
                FROM posted_tweets p
                JOIN tweet_metrics m ON m.tweet_id = p.tweet_id
            """).fetchall()
        samples = []
        minimum_age = timedelta(hours=min_age_hours)
        for row in rows:
            if (
                not self._canonical_x_tweet_id(row["tweet_id"])
                or row["posted_count"] != 1
            ):
                continue
            scheduled = self._strict_aware_datetime(row["created_at"])
            measured = self._strict_aware_datetime(row["checked_at"])
            metrics = [
                row["impressions"],
                row["likes"],
                row["retweets"],
                row["replies"],
                row["bookmarks"],
            ]
            if (
                scheduled is None
                or measured is None
                or scheduled > current
                or measured > current
                or measured < scheduled + minimum_age
                or any(type(value) is not int or value < 0 for value in metrics)
            ):
                continue
            engagements = sum(metrics[1:])
            if engagements > metrics[0]:
                continue
            samples.append(TimingSample(
                scheduled_for=scheduled,
                measured_at=measured,
                impressions=metrics[0],
                engagements=engagements,
            ))
        samples.sort(key=lambda sample: (
            sample.scheduled_for,
            sample.measured_at,
        ))
        return samples

    def get_first_posted_at(self) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT created_at FROM posted_tweets
                WHERE julianday(created_at) IS NOT NULL
                  AND typeof(tweet_id) = 'text' AND length(trim(tweet_id)) > 0
                ORDER BY julianday(created_at), id
                LIMIT 1
            """).fetchone()
        return row["created_at"] if row else None

    def get_category_performance(
        self,
        days: int = 30,
        *,
        end_at: Optional[datetime] = None,
    ) -> Dict[str, Dict]:
        """Aggrega metriche per categoria: usato dal modulo analytics per l'auto-learning"""
        if type(days) is not int or days <= 0:
            return {}
        current_time = end_at or datetime.now(timezone.utc)
        if (
            type(current_time) is not datetime
            or current_time.tzinfo is None
            or current_time.utcoffset() is None
        ):
            raise ValueError("end_at must be timezone-aware")
        current_time = current_time.astimezone(timezone.utc)
        since = (current_time - timedelta(days=days)).isoformat()
        until = current_time.isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT p.category, m.impressions, m.likes, m.retweets, m.replies, m.bookmarks
                FROM posted_tweets p
                JOIN tweet_metrics m ON p.tweet_id = m.tweet_id
                WHERE julianday(p.created_at) >= julianday(?)
                  AND julianday(p.created_at) <= julianday(?)
                  AND typeof(p.tweet_id) = 'text'
                  AND length(trim(p.tweet_id)) > 0
            """, (since, until)).fetchall()

        agg: Dict[str, Dict] = {}
        for r in rows:
            cat = r['category'] or 'generico'
            a = agg.setdefault(cat, {'impressions': 0, 'engagement': 0, 'posts': 0})
            a['impressions'] += r['impressions'] or 0
            a['engagement'] += (r['likes'] or 0) + (r['retweets'] or 0) + (r['replies'] or 0) + (r['bookmarks'] or 0)
            a['posts'] += 1
        return agg

    # ---------- Target accounts ----------

    def upsert_target_account(self, username: str, category: str = '', score: int = 0,
                               follower_count: int = 0, engagement_score: float = 0.0,
                               verified: bool = False, user_id: str = ''):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO target_accounts (username, user_id, category, follower_count,
                                              engagement_score, verified, score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    user_id=excluded.user_id, category=excluded.category,
                    follower_count=excluded.follower_count, engagement_score=excluded.engagement_score,
                    verified=excluded.verified, score=excluded.score
            """, (username, user_id, category, follower_count, engagement_score, int(verified), score))

    def get_top_targets(self, limit: int = 20) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM target_accounts ORDER BY score DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def mark_target_interacted(self, username: str):
        with self._conn() as conn:
            conn.execute("""
                UPDATE target_accounts SET last_interacted = ? WHERE username = ?
            """, (datetime.now().isoformat(), username))

    # ---------- Anti-spam guard ----------

    def commented_on_user_recently(self, username: str, hours: int = 24) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM spam_guard WHERE key = ?",
                                (f"commented:{username}",)).fetchone()
            if not row:
                return False
            last = datetime.fromisoformat(row['value'])
            return (datetime.now() - last) < timedelta(hours=hours)

    def mark_commented_on_user(self, username: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO spam_guard (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """, (f"commented:{username}", datetime.now().isoformat(), datetime.now().isoformat()))

    def get_last_hashtags(self, limit: int = 5) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT text FROM posted_tweets ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        hashtags = []
        for r in rows:
            hashtags += [w for w in r['text'].split() if w.startswith('#')]
        return hashtags

    # ---------- Libreria media (foto/video reali per i post) ----------

    @staticmethod
    def _media_mutation_snapshot(
        conn: sqlite3.Connection,
        target_kind: str,
        target_id: int,
    ) -> Tuple[Tuple[Any, ...], ...]:
        columns = ", ".join(_MEDIA_MUTATION_SNAPSHOT_COLUMNS)
        if target_kind == "media":
            rows = conn.execute(
                f"SELECT {columns} FROM media_library WHERE id = ? ORDER BY id",
                (target_id,),
            ).fetchall()
        elif target_kind == "draft_reservations":
            rows = conn.execute(
                f"""
                SELECT {columns} FROM media_library
                WHERE reserved_by_draft_id = ?
                  AND lifecycle_state = 'reserved'
                ORDER BY id
                """,
                (target_id,),
            ).fetchall()
        elif target_kind == "all_media":
            rows = conn.execute(
                f"SELECT {columns} FROM media_library ORDER BY id"
            ).fetchall()
        else:
            raise ValueError("invalid_media_mutation_target")
        return tuple(
            tuple(row[column] for column in _MEDIA_MUTATION_SNAPSHOT_COLUMNS)
            for row in rows
        )

    @staticmethod
    def _media_mutation_roots(
        snapshot: Tuple[Tuple[Any, ...], ...],
    ) -> Tuple[str, ...]:
        roots = set()
        for values in snapshot:
            record = dict(zip(_MEDIA_MUTATION_SNAPSHOT_COLUMNS, values))
            identity_values = (
                record["file_device"],
                record["file_inode"],
                record["file_sha256"],
            )
            if all(value is None for value in identity_values):
                continue
            if not record_has_media_identity(record):
                raise ValueError("media_identity_unavailable")
            filepath = record["filepath"]
            filename = record["filename"]
            if type(filepath) is not str or type(filename) is not str:
                raise ValueError("invalid_media_locator")
            locator = Path(filepath)
            if locator.name != filename or filename in {"", ".", ".."}:
                raise ValueError("invalid_media_locator")
            roots.add(str(locator.parent.resolve(strict=True)))
        return tuple(sorted(roots))

    @staticmethod
    def _post_draft_binding_snapshot(
        conn: sqlite3.Connection,
        draft_id: int,
        extra_media_ids: Tuple[int, ...] = (),
    ) -> Tuple[Optional[Tuple[Any, ...]], Tuple[Tuple[Any, ...], ...]]:
        draft_columns = ", ".join(_DRAFT_BINDING_SNAPSHOT_COLUMNS)
        draft = conn.execute(
            f"SELECT {draft_columns} FROM post_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
        draft_snapshot = (
            tuple(
                draft[column]
                for column in _DRAFT_BINDING_SNAPSHOT_COLUMNS
            )
            if draft is not None
            else None
        )
        media_columns = ", ".join(_MEDIA_MUTATION_SNAPSHOT_COLUMNS)
        conditions = [
            "id = (SELECT media_id FROM post_drafts WHERE id = ?)",
            "reserved_by_draft_id = ?",
        ]
        parameters: List[Any] = [draft_id, draft_id]
        if extra_media_ids:
            conditions.append(
                "id IN (" + ", ".join("?" for _ in extra_media_ids) + ")"
            )
            parameters.extend(extra_media_ids)
        media_rows = conn.execute(
            f"SELECT {media_columns} FROM media_library WHERE "
            + " OR ".join(conditions)
            + " ORDER BY id",
            parameters,
        ).fetchall()
        media_snapshot = tuple(
            tuple(row[column] for column in _MEDIA_MUTATION_SNAPSHOT_COLUMNS)
            for row in media_rows
        )
        return draft_snapshot, media_snapshot

    @contextmanager
    def _post_draft_mutation_lock(
        self,
        draft_id: int,
        *,
        extra_media_ids: Tuple[int, ...] = (),
    ):
        """Fence one draft binding from preview through its mutation commit.

        Discovery never owns SQLite's write lock.  Bound media roots are
        acquired in stable order first; only then is the exact draft/media
        snapshot revalidated under ``BEGIN IMMEDIATE``.  A retarget retries
        from discovery so no stale or unrelated root is held during mutation.
        """
        safe_extra_media_ids = tuple(sorted({
            media_id
            for media_id in extra_media_ids
            if type(media_id) is int and media_id > 0
        }))
        for _attempt in range(_MEDIA_STORE_MUTATION_MAX_ATTEMPTS):
            with self._conn() as snapshot_conn:
                candidate = self._post_draft_binding_snapshot(
                    snapshot_conn,
                    draft_id,
                    safe_extra_media_ids,
                )
            roots = self._media_mutation_roots(candidate[1])
            with ExitStack() as stack:
                for root in roots:
                    stack.enter_context(media_store_lock(Path(root)))
                with self._conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    validated = self._post_draft_binding_snapshot(
                        conn,
                        draft_id,
                        safe_extra_media_ids,
                    )
                    if validated != candidate:
                        conn.rollback()
                        continue
                    yield conn
                    return
        raise RuntimeError("post_draft_binding_snapshot_unstable")

    @contextmanager
    def _media_store_mutation_lock(
        self,
        target_kind: str,
        target_id: int,
    ):
        """Lock and revalidate a target snapshot before mutating it.

        Root locks are never awaited while a SQLite transaction is open.  The
        connection yielded to the caller owns the BEGIN IMMEDIATE transaction
        that validated the target, so the mutation cannot outlive that
        validated identity/lifecycle snapshot.
        """
        for _attempt in range(_MEDIA_STORE_MUTATION_MAX_ATTEMPTS):
            with self._conn() as snapshot_conn:
                candidate = self._media_mutation_snapshot(
                    snapshot_conn, target_kind, target_id,
                )
            roots = self._media_mutation_roots(candidate)

            with ExitStack() as stack:
                for root in roots:
                    stack.enter_context(media_store_lock(Path(root)))
                with self._conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    validated = self._media_mutation_snapshot(
                        conn, target_kind, target_id,
                    )
                    if validated != candidate:
                        conn.rollback()
                        continue
                    yield conn
                    return
        raise RuntimeError("media_store_snapshot_unstable")

    def _reconcile_media_schema(self, lifecycle_migration_pending: bool) -> None:
        """Run lifecycle-writing migrations under every affected root lock."""
        with self._media_store_mutation_lock("all_media", 0) as conn:
            migration = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (_MEDIA_LIFECYCLE_MIGRATION_KEY,),
            ).fetchone()
            migration_still_pending = (
                lifecycle_migration_pending
                and migration is not None
                and migration["value"] == "pending"
            )
            if migration_still_pending:
                conn.execute("""
                    UPDATE media_library
                    SET lifecycle_state = CASE
                        WHEN file_deleted = 1 THEN 'deleted'
                        WHEN used = 1 THEN 'used'
                        ELSE 'available'
                    END
                """)
                conn.execute("""
                    UPDATE bot_state
                    SET value = 'complete', updated_at = ?
                    WHERE key = ? AND value = 'pending'
                """, (self._now_iso(), _MEDIA_LIFECYCLE_MIGRATION_KEY))
            else:
                conn.execute("""
                    UPDATE media_library SET lifecycle_state = CASE
                        WHEN file_deleted = 1 THEN 'deleted'
                        WHEN used = 1 THEN 'used'
                        ELSE 'available'
                    END
                    WHERE lifecycle_state IS NULL OR lifecycle_state = ''
                """)

            duplicate_slots = conn.execute(
                "SELECT intended_slot FROM post_drafts "
                "WHERE status IN (" + _LIVE_DRAFT_STATUS_SQL + ") "
                "GROUP BY intended_slot HAVING COUNT(*) > 1"
            ).fetchall()
            for duplicate in duplicate_slots:
                rows = conn.execute(
                    "SELECT id, category FROM post_drafts "
                    "WHERE intended_slot = ? AND status IN ("
                    + _LIVE_DRAFT_STATUS_SQL
                    + ") ORDER BY CASE status "
                    "WHEN 'published' THEN 5 "
                    "WHEN 'publication_unknown' THEN 4 "
                    "WHEN 'publishing' THEN 3 "
                    "WHEN 'approved' THEN 2 ELSE 1 END DESC, id ASC",
                    (duplicate["intended_slot"],),
                ).fetchall()
                keeper_id = rows[0]["id"]
                for stale in rows[1:]:
                    now = self._now_iso()
                    conn.execute(
                        "UPDATE post_drafts SET status = 'superseded', "
                        "error = 'migration_duplicate_slot', updated_at = ?, "
                        "revision = revision + 1 WHERE id = ?",
                        (now, stale["id"]),
                    )
                    conn.execute("""
                        UPDATE media_library
                        SET lifecycle_state = 'available',
                            reserved_by_draft_id = NULL
                        WHERE reserved_by_draft_id = ?
                          AND lifecycle_state = 'reserved'
                    """, (stale["id"],))
                    conn.execute("""
                        INSERT INTO draft_evaluations (
                            intended_slot, category, outcome, details_json,
                            created_at
                        ) VALUES (?, ?, 'migration_duplicate_slot', ?, ?)
                    """, (
                        duplicate["intended_slot"],
                        stale["category"],
                        json.dumps({
                            "draft_id": stale["id"],
                            "kept_draft_id": keeper_id,
                        }),
                        now,
                    ))
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_post_drafts_live_intended_slot "
                "ON post_drafts(intended_slot) WHERE status IN ("
                + _LIVE_DRAFT_STATUS_SQL
                + ")"
            )

    def add_media(self, filename: str, filepath: str, media_type: str,
                  category: str = 'other', ai_description: str = '',
                  ai_tags: str = '') -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO media_library (filename, filepath, media_type, category, ai_description, ai_tags)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (filename, filepath, media_type, category, ai_description, ai_tags))
            return cur.lastrowid

    def add_media_with_context(
        self,
        filename: str,
        filepath: str,
        media_type: str,
        category: str = "other",
        ai_description: str = "",
        ai_tags: str = "",
        user_context: str = "",
        mime_type: Optional[str] = None,
        file_size: int = 0,
        pinned_media: Optional[PinnedMediaFile] = None,
    ) -> Dict:
        """Persist a row/source only if the locked pinned identity still holds."""
        if not isinstance(pinned_media, PinnedMediaFile):
            raise ValueError("media_identity_required")
        locator = Path(filepath)
        if locator.name != filename or pinned_media.name != filename:
            raise ValueError("invalid_media_locator")
        identity = pinned_media.identity
        if file_size != identity.size:
            raise ValueError("file_size_mismatch")
        now = self._now_iso()
        with media_store_lock(locator.parent) as (_root, locked_root_fd):
            locked_root = os.fstat(locked_root_fd)
            pinned_root = os.fstat(pinned_media.root_fd)
            if (
                locked_root.st_dev != pinned_root.st_dev
                or locked_root.st_ino != pinned_root.st_ino
            ):
                raise ValueError("invalid_media_locator")
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute("""
                    INSERT INTO media_library (
                        filename, filepath, media_type, category, ai_description,
                        ai_tags, lifecycle_state, user_context, mime_type,
                        file_size, file_device, file_inode, file_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, 'available', ?, ?, ?, ?, ?, ?)
                """, (
                    filename, filepath, media_type, category, ai_description,
                    ai_tags, user_context, mime_type, identity.size,
                    identity.device, identity.inode, identity.sha256,
                ))
                media_id = cursor.lastrowid
                source_text = user_context or ai_description or filename
                conn.execute("""
                    INSERT INTO content_sources (
                        source_type, text, metadata_json, trust_state,
                        created_at, updated_at
                    ) VALUES ('media_context', ?, ?, 'verified', ?, ?)
                """, (
                    source_text,
                    json.dumps({"media_id": media_id}),
                    now,
                    now,
                ))
                row = conn.execute(
                    "SELECT * FROM media_library WHERE id = ?", (media_id,)
                ).fetchone()
                verify_pinned_media(pinned_media)
                conn.commit()
                return dict(row)

    def get_media_context_source(self, media_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM content_sources
                WHERE source_type = 'media_context'
                ORDER BY id ASC
            """).fetchall()
        for row in rows:
            decoded = self._decode_json_fields(row, {"metadata_json": "metadata"})
            if decoded["metadata"].get("media_id") == media_id:
                return decoded
        return None

    def get_media_by_id(self, media_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM media_library WHERE id = ?", (media_id,)).fetchone()
            return dict(row) if row else None

    def get_unused_media(self, category: Optional[str] = None, limit: int = 20) -> List[Dict]:
        """
        Media non ancora usati. Se category è specificata, filtra su
        quella. Usato principalmente come base per get_unused_media_pool;
        la scelta finale di QUALE media usare non è più FIFO ma affidata
        all'AI (vedi AIGenerator.select_best_media in main.py).
        """
        with self._conn() as conn:
            if category:
                rows = conn.execute("""
                    SELECT * FROM media_library
                    WHERE lifecycle_state = 'available' AND file_deleted = 0
                      AND category = ?
                    ORDER BY uploaded_at ASC LIMIT ?
                """, (category, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM media_library
                    WHERE lifecycle_state = 'available' AND file_deleted = 0
                    ORDER BY uploaded_at ASC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_unused_media_pool(self, limit: int = 15) -> List[Dict]:
        """
        Pool di candidati non ancora usati da sottoporre all'AI per la
        scelta del media più adatto al post di oggi. Il limite serve solo a
        contenere la dimensione del prompt, non è un criterio di scelta:
        la selezione vera e propria è per contenuto, non per data.
        """
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM media_library
                WHERE lifecycle_state = 'available' AND file_deleted = 0
                ORDER BY uploaded_at ASC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_all_media(self, limit: int = 300) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM media_library ORDER BY uploaded_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_available_media(self, limit: int = 15) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM media_library
                WHERE lifecycle_state = 'available' AND file_deleted = 0
                ORDER BY uploaded_at ASC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def reserve_media(self, media_id: int, draft_id: int) -> bool:
        if (
            type(media_id) is not int
            or media_id <= 0
            or type(draft_id) is not int
            or draft_id <= 0
        ):
            return False
        with self._post_draft_mutation_lock(
            draft_id,
            extra_media_ids=(media_id,),
        ) as conn:
            cursor = conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'reserved', reserved_by_draft_id = ?
                WHERE id = ? AND lifecycle_state = 'available'
                  AND file_deleted = 0
            """, (draft_id, media_id))
            return cursor.rowcount == 1

    def attach_media_to_draft(self, media_id: int, draft_id: int) -> bool:
        """Atomically reserve media and append its trace source to a draft."""
        with self._media_store_mutation_lock("media", media_id) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            media = conn.execute(
                "SELECT * FROM media_library WHERE id = ?", (media_id,)
            ).fetchone()
            if (
                not draft
                or draft["status"] != "pending_approval"
                or draft["media_id"] is not None
                or not media
                or media["lifecycle_state"] != "available"
                or media["file_deleted"]
            ):
                return False
            context_source_id = None
            for source in conn.execute("""
                SELECT id, metadata_json FROM content_sources
                WHERE source_type = 'media_context'
                ORDER BY id ASC
            """).fetchall():
                try:
                    metadata = json.loads(source["metadata_json"])
                except (TypeError, ValueError):
                    continue
                if metadata.get("media_id") == media_id:
                    context_source_id = source["id"]
                    break
            if context_source_id is None:
                return False
            reserved = conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'reserved', reserved_by_draft_id = ?
                WHERE id = ? AND lifecycle_state = 'available'
                  AND file_deleted = 0
            """, (draft_id, media_id))
            if reserved.rowcount != 1:
                return False
            source_ids = json.loads(draft["source_ids_json"])
            if context_source_id not in source_ids:
                source_ids.append(context_source_id)
            now = self._now_iso()
            updated = conn.execute("""
                UPDATE post_drafts
                SET media_id = ?, source_ids_json = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND status = 'pending_approval'
                  AND media_id IS NULL AND revision = ?
            """, (
                media_id, json.dumps(source_ids), now, draft_id,
                draft["revision"],
            ))
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("draft changed during media attach")
            self._insert_draft_evaluation_in_conn(
                conn,
                draft["intended_slot"],
                draft["category"],
                "media_reserved",
                {"draft_id": draft_id, "media_id": media_id},
                now,
            )
            return True

    def detach_media_from_draft(self, draft_id: int) -> bool:
        """Atomically return a pending draft to text-only and release its media."""
        with self._media_store_mutation_lock(
            "draft_reservations", draft_id,
        ) as conn:
            draft = conn.execute(
                "SELECT * FROM post_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not draft or draft["status"] != "pending_approval":
                return False
            media_id = draft["media_id"]
            if media_id is None:
                return True
            media = conn.execute(
                "SELECT * FROM media_library WHERE id = ?", (media_id,)
            ).fetchone()
            if (
                not media
                or media["lifecycle_state"] != "reserved"
                or media["reserved_by_draft_id"] != draft_id
            ):
                return False
            media_source_ids = set()
            for source in conn.execute("""
                SELECT id, metadata_json FROM content_sources
                WHERE source_type = 'media_context'
            """).fetchall():
                try:
                    metadata = json.loads(source["metadata_json"])
                except (TypeError, ValueError):
                    continue
                if metadata.get("media_id") == media_id:
                    media_source_ids.add(source["id"])
            source_ids = [
                source_id
                for source_id in json.loads(draft["source_ids_json"])
                if source_id not in media_source_ids
            ]
            released = conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'available', reserved_by_draft_id = NULL
                WHERE id = ? AND lifecycle_state = 'reserved'
                  AND reserved_by_draft_id = ?
            """, (media_id, draft_id))
            if released.rowcount != 1:
                return False
            now = self._now_iso()
            updated = conn.execute("""
                UPDATE post_drafts
                SET media_id = NULL, source_ids_json = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ? AND status = 'pending_approval'
                  AND revision = ? AND media_id = ?
            """, (
                json.dumps(source_ids), now, draft_id, draft["revision"], media_id,
            ))
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("draft changed during media detach")
            self._insert_draft_evaluation_in_conn(
                conn,
                draft["intended_slot"],
                draft["category"],
                "media_released",
                {"draft_id": draft_id, "media_id": media_id},
                now,
            )
            return True

    def release_media_for_draft(self, draft_id: int) -> None:
        with self._media_store_mutation_lock(
            "draft_reservations", draft_id,
        ) as conn:
            draft = conn.execute(
                "SELECT intended_slot, category FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            media_ids = [row["id"] for row in conn.execute("""
                SELECT id FROM media_library
                WHERE reserved_by_draft_id = ?
                  AND lifecycle_state = 'reserved'
            """, (draft_id,)).fetchall()]
            conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'available', reserved_by_draft_id = NULL
                WHERE reserved_by_draft_id = ?
                  AND lifecycle_state = 'reserved'
            """, (draft_id,))
            if draft:
                now = self._now_iso()
                for media_id in media_ids:
                    self._insert_draft_evaluation_in_conn(
                        conn,
                        draft["intended_slot"],
                        draft["category"],
                        "media_released",
                        {"draft_id": draft_id, "media_id": media_id},
                        now,
                    )

    def mark_media_used(self, media_id: int, tweet_id: str = ''):
        with self._media_store_mutation_lock("media", media_id) as conn:
            conn.execute("""
                UPDATE media_library
                SET used = 1, used_at = ?, used_in_tweet_id = ?,
                    lifecycle_state = 'used', reserved_by_draft_id = NULL
                WHERE id = ? AND lifecycle_state IN ('available', 'reserved')
                  AND file_deleted = 0
            """, (self._now_iso(), tweet_id, media_id))

    def archive_media(self, media_id: int) -> bool:
        with self._media_store_mutation_lock("media", media_id) as conn:
            cursor = conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'archived', reserved_by_draft_id = NULL
                WHERE id = ? AND lifecycle_state = 'available'
            """, (media_id,))
            return cursor.rowcount == 1

    def set_media_reusable(self, media_id: int, reusable: bool) -> bool:
        with self._media_store_mutation_lock("media", media_id) as conn:
            cursor = conn.execute("""
                UPDATE media_library
                SET reusable = ?,
                    lifecycle_state = CASE
                        WHEN ? = 1 AND lifecycle_state = 'used' THEN 'available'
                        WHEN ? = 0 AND used = 1
                             AND lifecycle_state = 'available' THEN 'used'
                        ELSE lifecycle_state
                    END
                WHERE id = ?
            """, (int(reusable), int(reusable), int(reusable), media_id))
            return cursor.rowcount == 1

    def mark_media_file_deleted(self, media_id: int, delete_file=None) -> bool:
        """Segna che il file fisico è stato rimosso dal disco per risparmiare
        spazio (il record resta nel DB come storico/audit)."""
        with self._media_store_mutation_lock("media", media_id) as conn:
            row = conn.execute(
                "SELECT lifecycle_state, file_deleted "
                "FROM media_library WHERE id = ?",
                (media_id,),
            ).fetchone()
            if (
                not row
                or row["file_deleted"]
                or row["lifecycle_state"] not in MEDIA_FILE_DELETE_SAFE_STATES
            ):
                return False
            cursor = conn.execute("""
                UPDATE media_library
                SET file_deleted = 1, lifecycle_state = 'deleted',
                    reserved_by_draft_id = NULL
                WHERE id = ? AND file_deleted = 0
                  AND lifecycle_state = ?
            """, (media_id, row["lifecycle_state"]))
            if cursor.rowcount != 1:
                return False
            if delete_file is not None:
                delete_file()
            return True

    def update_media(self, media_id: int, category: Optional[str] = None,
                      ai_description: Optional[str] = None):
        with self._media_store_mutation_lock("media", media_id) as conn:
            if category is not None:
                conn.execute(
                    "UPDATE media_library SET category = ? WHERE id = ?",
                    (category, media_id),
                )
            if ai_description is not None:
                conn.execute(
                    "UPDATE media_library SET ai_description = ? WHERE id = ?",
                    (ai_description, media_id),
                )

    def delete_media(self, media_id: int):
        with self._media_store_mutation_lock("media", media_id) as conn:
            conn.execute("DELETE FROM media_library WHERE id = ?", (media_id,))

    # ---------- Growth candidates and follower snapshots ----------

    def upsert_growth_candidate(self, candidate: Dict) -> int:
        user_id = candidate.get("user_id")
        if type(user_id) is not str or not user_id:
            raise ValueError("Growth candidate user_id must be a non-empty string")
        username = candidate.get("username")
        if type(username) is not str or not username:
            raise ValueError("Growth candidate username must be a non-empty string")
        now = self._now_iso()
        profile_expires_at = candidate.get("profile_expires_at") or (
            datetime.now(timezone.utc) + timedelta(days=7)
        ).isoformat()
        profile = candidate.get("profile") or {}
        latest_post = candidate.get("latest_post")
        score_data = candidate.get("score_data", candidate.get("score_json", {}))
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO growth_candidates (
                    user_id, username, profile_json, latest_post_json, score,
                    score_json, discovery_source, decision, rejection_reason,
                    first_seen_at, last_evaluated_at, profile_expires_at,
                    digest_sent_at, manual_followed_at, followed_back_at,
                    suppressed_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    profile_json = excluded.profile_json,
                    latest_post_json = excluded.latest_post_json,
                    score = excluded.score,
                    score_json = excluded.score_json,
                    discovery_source = excluded.discovery_source,
                    last_evaluated_at = excluded.last_evaluated_at,
                    profile_expires_at = excluded.profile_expires_at
            """, (
                user_id,
                username,
                json.dumps(profile),
                json.dumps(latest_post) if latest_post is not None else None,
                int(candidate["score"]),
                json.dumps(score_data),
                candidate["discovery_source"],
                candidate.get("decision", "new"),
                candidate.get("rejection_reason"),
                candidate.get("first_seen_at", now),
                candidate.get("last_evaluated_at", now),
                profile_expires_at,
                candidate.get("digest_sent_at"),
                candidate.get("manual_followed_at"),
                candidate.get("followed_back_at"),
                candidate.get("suppressed_until"),
            ))
            row = conn.execute(
                "SELECT id FROM growth_candidates WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row["id"]

    def _decode_growth_candidate_for_audit(self, row: sqlite3.Row) -> Dict:
        """Decode an audit row without treating it as discovery-eligible."""
        result = self._decode_json_fields(row, {
            "profile_json": "profile",
            "latest_post_json": "latest_post",
            "score_json": "score_data",
        })
        profile = result.get("profile")
        score_data = result.get("score_data")
        latest_post = result.get("latest_post")
        if not isinstance(profile, dict) or not isinstance(score_data, dict):
            raise ValueError("Malformed growth candidate JSON")
        if latest_post is not None and not isinstance(latest_post, dict):
            raise ValueError("Malformed growth candidate latest post")
        latest_post = latest_post or {}
        result["audience_segment"] = score_data.get("audience_segment")
        reasons = score_data.get("reasons") or []
        result["reasons"] = reasons if isinstance(reasons, list) else []
        result["activity_at"] = (
            score_data.get("activity_at") or latest_post.get("created_at")
        )
        username = result.get("username")
        tweet_id = latest_post.get("id", latest_post.get("tweet_id"))
        direct_url = None
        if (
            type(username) is str
            and re.fullmatch(r"[A-Za-z0-9_]{1,15}", username)
        ):
            direct_url = f"https://x.com/{username}"
            if (
                type(tweet_id) is str
                and tweet_id.isascii()
                and tweet_id.isdigit()
            ):
                direct_url += f"/status/{tweet_id}"
        result["direct_url"] = direct_url
        return result

    def _validated_growth_candidate(
        self,
        row: sqlite3.Row,
        now: datetime,
    ) -> Optional[Dict]:
        """Decode one canonical eligible row for both cache and digest reads."""
        try:
            result = self._decode_json_fields(row, {
                "profile_json": "profile",
                "latest_post_json": "latest_post",
                "score_json": "score_data",
            })
            current_time = self._as_utc(now)
            profile = result.get("profile")
            latest_post = result.get("latest_post")
            score_data = result.get("score_data")
            user_id = result.get("user_id")
            username = result.get("username")
            score = result.get("score")

            if (
                type(result.get("id")) is not int
                or result["id"] <= 0
                or type(user_id) is not str
                or not user_id
                or type(username) is not str
                or re.fullmatch(r"[A-Za-z0-9_]{1,15}", username) is None
                or type(score) is not int
                or not 0 <= score <= 100
                or type(result.get("discovery_source")) is not str
                or not result["discovery_source"]
                or not is_json_safe_mapping(profile)
                or not is_json_safe_mapping(latest_post)
                or not is_json_safe_mapping(score_data)
            ):
                return None

            if not is_canonical_growth_profile(
                profile,
                user_id=user_id,
                username=username,
            ):
                return None

            latest_id = latest_post.get("id")
            if not is_canonical_growth_latest_post(latest_post):
                return None
            if evaluate_growth_candidate_filters(
                profile,
                latest_post,
                current_time,
            ) != (True, "accepted"):
                return None

            evaluated_at = parse_growth_datetime(
                result.get("last_evaluated_at")
            )
            expires_at = parse_growth_datetime(
                result.get("profile_expires_at")
            )
            latest_activity = parse_growth_datetime(
                latest_post.get("created_at")
            )
            activity_at = parse_growth_datetime(
                score_data.get("activity_at")
            )
            if (
                evaluated_at is None
                or expires_at is None
                or latest_activity is None
                or activity_at is None
                or evaluated_at > current_time
                or expires_at <= current_time
                or expires_at <= evaluated_at
                or latest_activity != activity_at
                or activity_at > evaluated_at
            ):
                return None

            reasons = score_data.get("reasons")
            if (
                type(score_data.get("total")) is not int
                or score_data["total"] != score
                or score_data.get("audience_segment") not in {
                    "primary", "amplifier", "end_user",
                }
                or type(reasons) is not list
                or any(type(reason) is not str for reason in reasons)
                or score_data.get("hard_filter_passed") is not True
                or score_data.get("filter_reason") != "accepted"
            ):
                return None

            result["audience_segment"] = score_data["audience_segment"]
            result["reasons"] = reasons
            result["activity_at"] = score_data["activity_at"]
            result["direct_url"] = (
                f"https://x.com/{username}/status/{latest_id}"
            )
            return result
        except (
            AttributeError,
            json.JSONDecodeError,
            KeyError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None

    def get_growth_candidate(self, user_id: str) -> Optional[Dict]:
        if type(user_id) is not str or not user_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM growth_candidates WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return self._decode_growth_candidate_for_audit(row)
        except (
            AttributeError,
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None

    def get_cached_growth_candidate(
        self,
        user_id: str,
        now: datetime,
    ) -> Optional[Dict]:
        if type(user_id) is not str or not user_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM growth_candidates WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        candidate = self._validated_growth_candidate(row, now)
        if candidate is None or candidate["user_id"] != user_id:
            return None
        return candidate

    def is_growth_candidate_suppressed(self, user_id: str, now: datetime) -> bool:
        if type(user_id) is not str or not user_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT suppressed_until FROM growth_candidates WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row or row["suppressed_until"] is None:
            return False
        try:
            return self._parse_datetime(row["suppressed_until"]) > self._as_utc(now)
        except (TypeError, ValueError):
            return True

    def get_digest_candidates(
        self,
        limit: int = 5,
        *,
        now: Optional[datetime] = None,
        threshold: int = 75,
    ) -> List[Dict]:
        if type(limit) is not int or limit <= 0:
            return []
        current_time = self._as_utc(now or datetime.now(timezone.utc))
        with self._conn() as conn:
            conn.execute("""
                UPDATE growth_candidates
                SET decision = 'new', rejection_reason = NULL,
                    suppressed_until = NULL
                WHERE decision IN ('rejected', 'discarded')
                  AND (
                      suppressed_until IS NULL
                      OR julianday(suppressed_until) <= julianday(?)
                  )
            """, (current_time.isoformat(),))
            rows = conn.execute("""
                SELECT * FROM growth_candidates
                WHERE decision = 'new' AND score >= ?
            """, (threshold,)).fetchall()
        eligible = []
        for row in rows:
            try:
                if (
                    row["suppressed_until"]
                    and self._parse_datetime(row["suppressed_until"]) > current_time
                ):
                    continue
                decoded = self._validated_growth_candidate(row, current_time)
                if decoded is None:
                    continue
            except (
                AttributeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                continue
            eligible.append(decoded)

        def activity_timestamp(candidate):
            value = candidate.get("activity_at")
            try:
                return self._parse_datetime(value).timestamp()
            except (TypeError, ValueError):
                return float("-inf")

        eligible.sort(key=lambda candidate: candidate["user_id"])
        eligible.sort(
            key=lambda candidate: (
                candidate["score"], activity_timestamp(candidate)
            ),
            reverse=True,
        )
        return eligible[:limit]

    def mark_candidate_decision(
        self,
        candidate_id: int,
        decision: str,
        reason: Optional[str] = None,
        *,
        decided_at: Optional[datetime] = None,
    ) -> bool:
        if (
            type(candidate_id) is not int
            or candidate_id <= 0
            or decision not in {
                "saved", "followed_manually", "discarded", "rejected",
            }
            or (reason is not None and type(reason) is not str)
        ):
            return False
        now = datetime.now(timezone.utc) if decided_at is None else decided_at
        if (
            type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("decided_at must be timezone-aware")
        now = now.astimezone(timezone.utc)
        now_iso = now.isoformat()
        manual_followed_at = now_iso if decision == "followed_manually" else None
        suppressed_until = None
        if decision in {"rejected", "discarded"}:
            suppressed_until = (now + timedelta(days=30)).isoformat()
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE growth_candidates
                SET decision = ?, rejection_reason = ?,
                    decision_at = ?,
                    manual_followed_at = COALESCE(?, manual_followed_at),
                    suppressed_until = ?
                WHERE id = ? AND decision = 'new'
            """, (
                decision,
                reason,
                now_iso,
                manual_followed_at,
                suppressed_until,
                candidate_id,
            ))
            return cursor.rowcount == 1

    def save_follower_snapshot(
        self,
        observed_on: str,
        profile: Dict,
        relevant: bool,
        source: Optional[str],
    ) -> bool:
        try:
            normalized_date = date.fromisoformat(observed_on).isoformat()
        except (TypeError, ValueError):
            raise ValueError("observed_on must be an exact ISO date") from None
        if normalized_date != observed_on:
            raise ValueError("observed_on must be an exact ISO date")
        if not is_canonical_growth_profile(profile):
            raise ValueError("Follower profile must match the canonical schema")
        if type(relevant) is not bool:
            raise ValueError("relevant must be an exact boolean")
        if source is not None and type(source) is not str:
            raise ValueError("source must be a string or None")
        user_id = profile.get("user_id", profile.get("id"))
        username = profile.get("username", "")
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO follower_snapshots (
                    observed_on, user_id, username, relevant, source,
                    profile_json, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observed_on, user_id) DO UPDATE SET
                    username = excluded.username,
                    relevant = excluded.relevant,
                    source = excluded.source,
                    profile_json = excluded.profile_json
            """, (
                observed_on,
                user_id,
                username,
                1 if relevant else 0,
                source,
                json.dumps(profile, allow_nan=False),
                self._now_iso(),
            ))
            return cursor.rowcount == 1

    def capture_follower_observation(
        self,
        observed_on: str,
        observed_at: datetime,
        profile: Dict,
        relevant: bool,
    ) -> Optional[Dict]:
        """Atomically persist one capture and attribute a manual conversion."""
        try:
            normalized_date = date.fromisoformat(observed_on).isoformat()
        except (TypeError, ValueError):
            return None
        if (
            normalized_date != observed_on
            or type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or type(relevant) is not bool
            or not is_canonical_growth_profile(profile)
        ):
            return None
        observed_iso = observed_at.astimezone(timezone.utc).isoformat()
        user_id = profile.get("user_id", profile.get("id"))
        username = profile["username"]
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            other_snapshot = conn.execute("""
                SELECT 1 FROM follower_snapshots
                WHERE user_id = ? AND observed_on != ?
                LIMIT 1
            """, (user_id, observed_on)).fetchone()
            current = conn.execute("""
                SELECT captured_at FROM follower_snapshots
                WHERE observed_on = ? AND user_id = ?
            """, (observed_on, user_id)).fetchone()
            is_new = other_snapshot is None and (
                current is None or current["captured_at"] is None
            )
            candidate = conn.execute("""
                SELECT id, decision, discovery_source
                FROM growth_candidates WHERE user_id = ?
            """, (user_id,)).fetchone()
            attribution_source = "unattributed"
            snapshot_source = "x_followers"
            if candidate is not None:
                candidate_id = candidate["id"]
                discovery_source = candidate["discovery_source"]
                if type(candidate_id) is int and candidate_id > 0:
                    snapshot_source = f"candidate:{candidate_id}"
                if type(discovery_source) is str and discovery_source:
                    attribution_source = discovery_source

            conn.execute("""
                INSERT INTO follower_snapshots (
                    observed_on, user_id, username, relevant, source,
                    attribution_source, profile_json, first_seen_at,
                    is_new, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observed_on, user_id) DO UPDATE SET
                    username = excluded.username,
                    relevant = excluded.relevant,
                    source = excluded.source,
                    attribution_source = excluded.attribution_source,
                    profile_json = excluded.profile_json,
                    is_new = CASE
                        WHEN follower_snapshots.captured_at IS NULL
                        THEN excluded.is_new
                        ELSE follower_snapshots.is_new
                    END,
                    captured_at = COALESCE(
                        follower_snapshots.captured_at, excluded.captured_at
                    )
            """, (
                observed_on,
                user_id,
                username,
                1 if relevant else 0,
                snapshot_source,
                attribution_source,
                json.dumps(profile, allow_nan=False),
                observed_iso,
                1 if is_new else 0,
                observed_iso,
            ))

            followed_back = False
            if is_new and candidate is not None:
                cursor = conn.execute("""
                    UPDATE growth_candidates
                    SET followed_back_at = ?
                    WHERE user_id = ?
                      AND decision = 'followed_manually'
                      AND followed_back_at IS NULL
                      AND julianday(manual_followed_at) <= julianday(?)
                """, (observed_iso, user_id, observed_iso))
                followed_back = cursor.rowcount == 1
            return {
                "is_new": is_new,
                "relevant": relevant,
                "attribution_source": attribution_source,
                "followed_back": followed_back,
            }

    def capture_follower_snapshot_batch(
        self,
        observed_on: str,
        observed_at: datetime,
        observations: List[tuple],
        followers_total: int,
    ) -> Optional[Dict]:
        """Commit a complete follower capture, its conversions and marker once."""
        try:
            normalized_date = date.fromisoformat(observed_on).isoformat()
        except (TypeError, ValueError):
            return None
        if (
            normalized_date != observed_on
            or type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or type(followers_total) is not int
            or followers_total < 0
            or not isinstance(observations, list)
        ):
            return None
        observed_iso = observed_at.astimezone(timezone.utc).isoformat()
        for item in observations:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not is_canonical_growth_profile(item[0])
                or type(item[1]) is not bool
            ):
                return None
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior_run = conn.execute("""
                SELECT followers_total, captured_at, completed, summary_json
                FROM follower_snapshot_runs
                WHERE observed_on = ?
            """, (observed_on,)).fetchone()
            if (
                prior_run is not None
                and prior_run["completed"] == 1
                and prior_run["captured_at"] >= observed_iso
            ):
                try:
                    summary = json.loads(prior_run["summary_json"])
                except (TypeError, ValueError):
                    summary = None
                if isinstance(summary, dict):
                    return summary
                return {
                    "followers_total": prior_run["followers_total"],
                    "new_total": 0,
                    "new_relevant": 0,
                    "source_counts": {},
                    "follow_backs_by_source": {},
                }
            repaired_first_seen = {}
            if prior_run is None or prior_run["completed"] != 1:
                repaired_first_seen = {
                    row["user_id"]: row["first_seen_at"]
                    for row in conn.execute("""
                        SELECT user_id, first_seen_at FROM follower_snapshots
                        WHERE observed_on = ? AND captured_at IS NOT NULL
                    """, (observed_on,)).fetchall()
                    if type(row["user_id"]) is str and row["user_id"]
                }
                conn.execute(
                    "DELETE FROM follower_snapshots "
                    "WHERE observed_on = ? AND captured_at IS NOT NULL",
                    (observed_on,),
                )

            summary = {
                "followers_total": followers_total,
                "new_total": 0,
                "new_relevant": 0,
                "source_counts": {},
                "follow_backs_by_source": {},
            }
            for profile, relevant in observations:
                user_id = profile.get("user_id", profile.get("id"))
                current = conn.execute("""
                    SELECT first_seen_at, captured_at FROM follower_snapshots
                    WHERE observed_on = ? AND user_id = ?
                """, (observed_on, user_id)).fetchone()
                other_snapshot = conn.execute("""
                    SELECT 1 FROM follower_snapshots
                    WHERE user_id = ? AND observed_on != ? LIMIT 1
                """, (user_id, observed_on)).fetchone()
                is_new = other_snapshot is None and (
                    current is None or current["captured_at"] is None
                )
                candidate = conn.execute("""
                    SELECT id, discovery_source FROM growth_candidates
                    WHERE user_id = ?
                """, (user_id,)).fetchone()
                attribution_source = "unattributed"
                snapshot_source = "x_followers"
                if candidate is not None:
                    if type(candidate["id"]) is int and candidate["id"] > 0:
                        snapshot_source = f"candidate:{candidate['id']}"
                    if type(candidate["discovery_source"]) is str and candidate[
                        "discovery_source"
                    ]:
                        attribution_source = candidate["discovery_source"]
                conn.execute("""
                    INSERT INTO follower_snapshots (
                        observed_on, user_id, username, relevant, source,
                        attribution_source, profile_json, first_seen_at,
                        is_new, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(observed_on, user_id) DO UPDATE SET
                        username = excluded.username,
                        relevant = excluded.relevant,
                        source = excluded.source,
                        attribution_source = excluded.attribution_source,
                        profile_json = excluded.profile_json,
                        is_new = CASE WHEN follower_snapshots.captured_at IS NULL
                            THEN excluded.is_new ELSE follower_snapshots.is_new END,
                        captured_at = COALESCE(follower_snapshots.captured_at,
                            excluded.captured_at)
                """, (
                    observed_on, user_id, profile["username"], 1 if relevant else 0,
                    snapshot_source, attribution_source,
                    json.dumps(profile, allow_nan=False),
                    repaired_first_seen.get(user_id, observed_iso),
                    1 if is_new else 0, observed_iso,
                ))
                followed_back = False
                if is_new:
                    conversion = conn.execute("""
                        SELECT candidates.id, candidates.decision,
                               candidates.decision_at,
                               candidates.followed_back_at,
                               snapshots.first_seen_at
                        FROM growth_candidates AS candidates
                        JOIN follower_snapshots AS snapshots
                          ON snapshots.user_id = candidates.user_id
                         AND snapshots.observed_on = ?
                        WHERE candidates.user_id = ?
                    """, (observed_on, user_id)).fetchone()
                    if conversion is not None:
                        first_seen_at = parse_growth_datetime(
                            conversion["first_seen_at"]
                        )
                        decision_at = parse_growth_datetime(
                            conversion["decision_at"]
                        )
                        if (
                            conversion["decision"] == "followed_manually"
                            and conversion["followed_back_at"] is None
                            and first_seen_at is not None
                            and decision_at is not None
                            and first_seen_at > decision_at
                        ):
                            cursor = conn.execute("""
                                UPDATE growth_candidates
                                SET followed_back_at = ?
                                WHERE id = ? AND user_id = ?
                                  AND decision = 'followed_manually'
                                  AND decision_at = ?
                                  AND followed_back_at IS NULL
                            """, (
                                observed_iso, conversion["id"], user_id,
                                conversion["decision_at"],
                            ))
                            followed_back = cursor.rowcount == 1
                if is_new:
                    summary["new_total"] += 1
                    if relevant:
                        summary["new_relevant"] += 1
                    summary["source_counts"][attribution_source] = (
                        summary["source_counts"].get(attribution_source, 0) + 1
                    )
                    if followed_back:
                        summary["follow_backs_by_source"][attribution_source] = (
                            summary["follow_backs_by_source"].get(
                                attribution_source, 0
                            ) + 1
                        )
            summary["source_counts"] = dict(sorted(summary["source_counts"].items()))
            summary["follow_backs_by_source"] = dict(
                sorted(summary["follow_backs_by_source"].items())
            )
            conn.execute("""
                INSERT INTO follower_snapshot_runs (
                    observed_on, followers_total, captured_at, completed,
                    summary_json
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(observed_on) DO UPDATE SET
                    followers_total = excluded.followers_total,
                    captured_at = excluded.captured_at, completed = 1,
                    summary_json = excluded.summary_json
            """, (
                observed_on, followers_total, observed_iso,
                json.dumps(summary, allow_nan=False, sort_keys=True),
            ))
            return summary

    def save_follower_snapshot_run(
        self,
        observed_on: str,
        observed_at: datetime,
        followers_total: int,
    ) -> bool:
        """Persist the daily total, including a successful empty snapshot."""
        try:
            normalized_date = date.fromisoformat(observed_on).isoformat()
        except (TypeError, ValueError):
            return False
        if (
            normalized_date != observed_on
            or type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or type(followers_total) is not int
            or followers_total < 0
        ):
            return False
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO follower_snapshot_runs (
                    observed_on, followers_total, captured_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(observed_on) DO UPDATE SET
                    followers_total = excluded.followers_total,
                    captured_at = excluded.captured_at
            """, (
                observed_on,
                followers_total,
                observed_at.astimezone(timezone.utc).isoformat(),
            ))
            return cursor.rowcount == 1

    def get_known_follower_ids(
        self,
        before_date: Optional[str] = None,
    ) -> Set[str]:
        with self._conn() as conn:
            if before_date is None:
                rows = conn.execute(
                    "SELECT DISTINCT user_id FROM follower_snapshots"
                ).fetchall()
            else:
                rows = conn.execute("""
                    SELECT DISTINCT user_id FROM follower_snapshots
                    WHERE observed_on < ?
                """, (before_date,)).fetchall()
        return {row["user_id"] for row in rows}

    def mark_candidate_followed_back(self, user_id: str, observed_at: str) -> bool:
        if type(user_id) is not str or not user_id:
            return False
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE growth_candidates SET followed_back_at = ?
                WHERE user_id = ? AND decision = 'followed_manually'
                  AND followed_back_at IS NULL
                  AND julianday(manual_followed_at) <= julianday(?)
            """, (observed_at, user_id, observed_at))
            return cursor.rowcount == 1

    def get_weekly_growth_analytics(
        self,
        start_on: str,
        end_on: str,
        start_at: datetime,
        end_at: datetime,
    ) -> Dict:
        """Return factual rows for one inclusive operating-date window."""
        try:
            start_date = date.fromisoformat(start_on)
            end_date = date.fromisoformat(end_on)
        except (TypeError, ValueError):
            raise ValueError("growth report dates must be exact ISO dates") from None
        if (
            start_date.isoformat() != start_on
            or end_date.isoformat() != end_on
            or start_date > end_date
            or type(start_at) is not datetime
            or type(end_at) is not datetime
            or start_at.tzinfo is None
            or start_at.utcoffset() is None
            or end_at.tzinfo is None
            or end_at.utcoffset() is None
            or start_at >= end_at
        ):
            raise ValueError("invalid growth report window")
        start_iso = start_at.astimezone(timezone.utc).isoformat()
        end_iso = end_at.astimezone(timezone.utc).isoformat()
        decision_counts = {
            "saved": 0,
            "followed_manually": 0,
            "discarded": 0,
            "rejected": 0,
        }
        with self._conn() as conn:
            latest_run = conn.execute("""
                SELECT followers_total FROM follower_snapshot_runs
                WHERE observed_on <= ? AND completed = 1
                ORDER BY observed_on DESC
                LIMIT 1
            """, (end_on,)).fetchone()
            followers_total = (
                latest_run["followers_total"] if latest_run else 0
            )

            new_rows = conn.execute("""
                SELECT snapshots.relevant, snapshots.attribution_source
                FROM follower_snapshots AS snapshots
                JOIN follower_snapshot_runs AS runs
                  ON runs.observed_on = snapshots.observed_on
                 AND runs.completed = 1
                WHERE snapshots.observed_on >= ?
                  AND snapshots.observed_on <= ?
                  AND snapshots.is_new = 1
            """, (start_on, end_on)).fetchall()
            new_followers = len(new_rows)
            new_relevant_followers = sum(
                row["relevant"] == 1 for row in new_rows
            )
            follower_sources: Dict[str, int] = {}
            for row in new_rows:
                source = row["attribution_source"]
                if type(source) is not str or not source:
                    source = "unattributed"
                follower_sources[source] = follower_sources.get(source, 0) + 1

            candidate_count_row = conn.execute("""
                SELECT COUNT(*) AS count FROM growth_candidates
                WHERE julianday(first_seen_at) >= julianday(?)
                  AND julianday(first_seen_at) < julianday(?)
            """, (start_iso, end_iso)).fetchone()
            candidate_count = (
                candidate_count_row["count"] if candidate_count_row else 0
            )
            candidate_source_rows = conn.execute("""
                SELECT DISTINCT discovery_source FROM growth_candidates
                WHERE julianday(first_seen_at) >= julianday(?)
                  AND julianday(first_seen_at) < julianday(?)
            """, (start_iso, end_iso)).fetchall()
            candidate_sources = {
                row["discovery_source"]
                for row in candidate_source_rows
                if type(row["discovery_source"]) is str
                and row["discovery_source"]
            }

            for row in conn.execute("""
                SELECT decision, COUNT(*) AS count FROM growth_candidates
                WHERE julianday(decision_at) >= julianday(?)
                  AND julianday(decision_at) < julianday(?)
                GROUP BY decision
            """, (start_iso, end_iso)).fetchall():
                if row["decision"] in decision_counts:
                    decision_counts[row["decision"]] = row["count"]

            manual_by_source: Dict[str, int] = {}
            followed_back_by_source: Dict[str, int] = {}
            for row in conn.execute("""
                SELECT discovery_source, manual_followed_at, followed_back_at
                FROM growth_candidates
                WHERE decision = 'followed_manually'
                  AND julianday(manual_followed_at) >= julianday(?)
                  AND julianday(manual_followed_at) < julianday(?)
            """, (start_iso, end_iso)).fetchall():
                source = row["discovery_source"]
                if type(source) is not str or not source:
                    source = "unattributed"
                manual_by_source[source] = manual_by_source.get(source, 0) + 1
                followed_at = row["followed_back_at"]
                if (
                    type(followed_at) is str
                    and conn.execute(
                        """
                        SELECT CASE WHEN julianday(?) >= julianday(?)
                                          AND julianday(?) < julianday(?)
                                    THEN 1 ELSE 0 END AS valid
                        """,
                        (
                            followed_at,
                            row["manual_followed_at"],
                            followed_at,
                            end_iso,
                        ),
                    ).fetchone()["valid"] == 1
                ):
                    followed_back_by_source[source] = (
                        followed_back_by_source.get(source, 0) + 1
                    )

            post_rows = conn.execute("""
                SELECT p.category, m.impressions
                FROM posted_tweets p
                LEFT JOIN tweet_metrics m ON m.tweet_id = p.tweet_id
                WHERE julianday(p.created_at) >= julianday(?)
                  AND julianday(p.created_at) < julianday(?)
                  AND typeof(p.tweet_id) = 'text'
                  AND length(trim(p.tweet_id)) > 0
                ORDER BY p.created_at, p.id
            """, (start_iso, end_iso)).fetchall()

            budget_rows = conn.execute("""
                SELECT key, value FROM bot_state
                WHERE key LIKE 'growth_queries:%'
                   OR key LIKE 'growth_profile_evaluations:%'
            """).fetchall()

        query_budget_used = 0
        profiles_evaluated = 0
        valid_days = {
            (start_date + timedelta(days=offset)).isoformat()
            for offset in range((end_date - start_date).days + 1)
        }
        for row in budget_rows:
            key = row["key"]
            value = row["value"]
            if (
                type(key) is not str
                or type(value) is not str
                or not value.isascii()
                or not value.isdigit()
            ):
                continue
            if len(value) > 9:
                continue
            count = int(value)
            if count > 1_000_000:
                continue
            if key.startswith("growth_queries:"):
                day_key = key.removeprefix("growth_queries:")
                if day_key in valid_days:
                    query_budget_used += count
            elif key.startswith("growth_profile_evaluations:"):
                day_key = key.removeprefix("growth_profile_evaluations:")
                if day_key in valid_days:
                    profiles_evaluated += count

        return {
            "followers_total": followers_total,
            "new_followers": new_followers,
            "new_relevant_followers": new_relevant_followers,
            "new_follower_sources": dict(sorted(follower_sources.items())),
            "candidate_count": candidate_count,
            "candidate_sources": sorted(candidate_sources),
            "decision_counts": decision_counts,
            "manual_follows_by_source": dict(sorted(manual_by_source.items())),
            "follow_backs_by_source": dict(
                sorted(followed_back_by_source.items())
            ),
            "posts": [dict(row) for row in post_rows],
            "query_budget_used": query_budget_used,
            "profiles_evaluated": profiles_evaluated,
        }

    # ---------- Telegram updates, state and safe errors ----------

    def claim_telegram_update(self, update_id: int, chat_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO telegram_updates (
                    update_id, chat_id, state, result_json, received_at
                ) VALUES (?, ?, 'processing', '{}', ?)
            """, (update_id, str(chat_id), self._now_iso()))
            return cursor.rowcount == 1

    def complete_telegram_update(
        self,
        update_id: int,
        state: str,
        result: Dict,
    ) -> None:
        safe_state = self._sanitize_persisted_text(state)
        safe_result = self._safe_telegram_result(result)
        with self._conn() as conn:
            conn.execute("""
                UPDATE telegram_updates
                SET state = ?, result_json = ?, processed_at = ?
                WHERE update_id = ? AND state = 'processing'
            """, (
                safe_state,
                json.dumps(safe_result),
                self._now_iso(),
                update_id,
            ))

    def set_state(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (key, value, self._now_iso()))

    def claim_growth_profile_evaluation(
        self,
        observed_on: str,
        user_id: str,
        limit: int,
    ) -> str:
        """Atomically reserve one daily paid profile read for one exact user."""
        if (
            type(observed_on) is not str
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_on) is None
            or type(user_id) is not str
            or not user_id
            or type(limit) is not int
            or limit <= 0
        ):
            return "budget_exhausted"
        try:
            if date.fromisoformat(observed_on).isoformat() != observed_on:
                return "budget_exhausted"
        except ValueError:
            return "budget_exhausted"
        count_key = f"growth_profile_evaluations:{observed_on}"
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("""
                SELECT 1 FROM growth_profile_claims
                WHERE observed_on = ? AND user_id = ?
            """, (observed_on, user_id)).fetchone()
            if existing:
                return "already_claimed"
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (count_key,)
            ).fetchone()
            raw_value = row["value"] if row else "0"
            if (
                type(raw_value) is not str
                or not raw_value.isascii()
                or not raw_value.isdigit()
            ):
                return "budget_exhausted"
            used = int(raw_value)
            if used >= limit:
                return "budget_exhausted"
            claimed_at = self._now_iso()
            conn.execute("""
                INSERT INTO growth_profile_claims (
                    observed_on, user_id, claimed_at
                ) VALUES (?, ?, ?)
            """, (observed_on, user_id, claimed_at))
            conn.execute("""
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (count_key, str(used + 1), claimed_at))
            return "claimed"

    def claim_growth_query(
        self,
        day_key: str,
        limit: int,
        source_count: int = 3,
    ) -> Optional[int]:
        """Atomically reserve a daily query and its rotated source index."""
        if (
            type(day_key) is not str
            or not day_key
            or type(limit) is not int
            or limit <= 0
            or type(source_count) is not int
            or source_count <= 0
        ):
            return None
        count_key = f"growth_queries:{day_key}"
        start_key = f"growth_query_start:{day_key}"
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count_row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (count_key,)
            ).fetchone()
            raw_count = count_row["value"] if count_row else "0"
            if (
                type(raw_count) is not str
                or not raw_count.isascii()
                or not raw_count.isdigit()
            ):
                return None
            used = int(raw_count)
            if used >= limit:
                return None

            start_row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (start_key,)
            ).fetchone()
            if start_row:
                raw_start = start_row["value"]
                if (
                    type(raw_start) is not str
                    or not raw_start.isascii()
                    or not raw_start.isdigit()
                ):
                    return None
                start = int(raw_start) % source_count
            else:
                offset_row = conn.execute(
                    "SELECT value FROM bot_state WHERE key = 'growth_source_offset'"
                ).fetchone()
                raw_offset = offset_row["value"] if offset_row else "0"
                if (
                    type(raw_offset) is not str
                    or not raw_offset.isascii()
                    or not raw_offset.isdigit()
                ):
                    return None
                start = int(raw_offset) % source_count
                now = self._now_iso()
                conn.execute(
                    "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
                    (start_key, str(start), now),
                )
                conn.execute("""
                    INSERT INTO bot_state (key, value, updated_at)
                    VALUES ('growth_source_offset', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (str((start - 1) % source_count), now))

            conn.execute("""
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (count_key, str(used + 1), self._now_iso()))
            return (start + used) % source_count

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def get_or_create_state(self, key: str, value: str) -> Optional[str]:
        if (
            type(key) is not str
            or not key
            or len(key) > 128
            or type(value) is not str
            or not value
            or len(value) > 128
        ):
            return None
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT OR IGNORE INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, value, self._now_iso()))
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def compare_and_set_state(
        self,
        key: str,
        expected_value: str,
        new_value: str,
    ) -> bool:
        """Replace one exact state value so concurrent workflows have one winner."""
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE bot_state
                SET value = ?, updated_at = ?
                WHERE key = ? AND value = ?
            """, (new_value, self._now_iso(), key, expected_value))
            return cursor.rowcount == 1

    def compare_and_clear_state(self, key: str, expected_value: str) -> bool:
        """Consume one exact state value at most once across processes."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM bot_state WHERE key = ? AND value = ?",
                (key, expected_value),
            )
            return cursor.rowcount == 1

    def log_error(self, context: str, error_type: str, safe_message: str) -> int:
        context = self._sanitize_persisted_text(context)
        error_type = self._sanitize_persisted_text(error_type)
        safe_message = self._sanitize_persisted_text(safe_message)
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO error_events (
                    context, error_type, safe_message, created_at
                ) VALUES (?, ?, ?, ?)
            """, (context, error_type, safe_message, self._now_iso()))
            return cursor.lastrowid

    def get_recent_errors(
        self,
        limit: int = 10,
        unresolved_only: bool = True,
    ) -> List[Dict]:
        with self._conn() as conn:
            if unresolved_only:
                rows = conn.execute("""
                    SELECT * FROM error_events WHERE resolved = 0
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM error_events
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,)).fetchall()
        return [dict(row) for row in rows]
