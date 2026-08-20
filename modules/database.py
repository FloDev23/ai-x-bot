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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Dict, Mapping, Optional, Set, Tuple
from contextlib import ExitStack, contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from modules.media_store import (
    PinnedMediaFile,
    media_store_lock,
    record_has_media_identity,
    verify_pinned_media,
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
                    manual_followed_at TEXT,
                    followed_back_at TEXT,
                    suppressed_until TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS follower_snapshots (
                    observed_on TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    relevant INTEGER NOT NULL,
                    source TEXT,
                    profile_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (observed_on, user_id)
                )
            """)

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

    def count_links_last_days(self, days: int = 7) -> int:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as c FROM posted_tweets WHERE has_link = 1 AND created_at >= ?
            """, (since,)).fetchone()
            return row['c'] if row else 0

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
                SELECT tweet_id FROM posted_tweets WHERE tweet_id != '' ORDER BY created_at DESC LIMIT ?
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
            eligible.append(
                self._decode_json_fields(row, {"metadata_json": "metadata"})
            )
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
            eligible.append(
                self._decode_json_fields(row, {"metadata_json": "metadata"})
            )
        return eligible

    def content_source_exists(self, url: str) -> bool:
        if not url:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM content_sources WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
            return row is not None

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
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tweet_metrics (tweet_id, impressions, likes, retweets, replies, bookmarks, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET
                    impressions=excluded.impressions, likes=excluded.likes,
                    retweets=excluded.retweets, replies=excluded.replies,
                    bookmarks=excluded.bookmarks, checked_at=excluded.checked_at
            """, (tweet_id, impressions, likes, retweets, replies, bookmarks,
                  datetime.now().isoformat()))

    def get_category_performance(self, days: int = 30) -> Dict[str, Dict]:
        """Aggrega metriche per categoria: usato dal modulo analytics per l'auto-learning"""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT p.category, m.impressions, m.likes, m.retweets, m.replies, m.bookmarks
                FROM posted_tweets p
                JOIN tweet_metrics m ON p.tweet_id = m.tweet_id
                WHERE p.created_at >= ? AND p.tweet_id != ''
            """, (since,)).fetchall()

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

    def _decode_growth_candidate(self, row: sqlite3.Row) -> Dict:
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
            return self._decode_growth_candidate(row)
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def get_cached_growth_candidate(
        self,
        user_id: str,
        now: datetime,
    ) -> Optional[Dict]:
        candidate = self.get_growth_candidate(user_id)
        if not candidate:
            return None
        try:
            expires_at = self._parse_datetime(candidate["profile_expires_at"])
            evaluated_at = self._parse_datetime(candidate["last_evaluated_at"])
        except (KeyError, TypeError, ValueError):
            return None
        current_time = self._as_utc(now)
        score_data = candidate.get("score_data")
        if (
            not isinstance(score_data, dict)
            or type(score_data.get("hard_filter_passed")) is not bool
            or evaluated_at > current_time
            or expires_at <= current_time
        ):
            return None
        return candidate

    def is_growth_candidate_suppressed(self, user_id: str, now: datetime) -> bool:
        candidate = self.get_growth_candidate(user_id)
        if not candidate or not candidate.get("suppressed_until"):
            return False
        try:
            return self._parse_datetime(candidate["suppressed_until"]) > self._as_utc(now)
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
                    type(row["user_id"]) is not str
                    or not row["user_id"]
                    or type(row["score"]) is not int
                    or not 0 <= row["score"] <= 100
                ):
                    continue
                evaluated_at = self._parse_datetime(row["last_evaluated_at"])
                if (
                    evaluated_at > current_time
                    or self._parse_datetime(row["profile_expires_at"]) <= current_time
                ):
                    continue
                if (
                    row["suppressed_until"]
                    and self._parse_datetime(row["suppressed_until"]) > current_time
                ):
                    continue
                decoded = self._decode_growth_candidate(row)
                if decoded["score_data"].get("hard_filter_passed") is not True:
                    continue
                activity_value = decoded.get("activity_at")
                if activity_value is not None:
                    activity_at = self._parse_datetime(activity_value)
                    if activity_at > current_time:
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
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        manual_followed_at = now_iso if decision == "followed_manually" else None
        suppressed_until = None
        if decision in {"rejected", "discarded"}:
            suppressed_until = (now + timedelta(days=30)).isoformat()
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE growth_candidates
                SET decision = ?, rejection_reason = ?,
                    manual_followed_at = COALESCE(?, manual_followed_at),
                    suppressed_until = ?
                WHERE id = ? AND decision = 'new'
            """, (
                decision,
                reason,
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
        user_id = profile.get("user_id", profile.get("id"))
        if type(user_id) is not str or not user_id:
            raise ValueError("Follower profile requires an exact string user_id or id")
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
                int(relevant),
                source,
                json.dumps(profile),
                self._now_iso(),
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
                WHERE user_id = ? AND followed_back_at IS NULL
            """, (observed_at, user_id))
            return cursor.rowcount == 1

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

    def claim_growth_counter(self, key: str, limit: int) -> bool:
        """Atomically reserve one paid growth read before contacting X."""
        if type(key) is not str or not key or type(limit) is not int or limit <= 0:
            return False
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
            raw_value = row["value"] if row else "0"
            if (
                type(raw_value) is not str
                or not raw_value.isascii()
                or not raw_value.isdigit()
            ):
                return False
            used = int(raw_value)
            if used >= limit:
                return False
            conn.execute("""
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (key, str(used + 1), self._now_iso()))
            return True

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
