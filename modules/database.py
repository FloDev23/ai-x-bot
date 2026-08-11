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
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Dict, Optional, Set
from contextlib import contextmanager
from zoneinfo import ZoneInfo

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
                    file_size INTEGER DEFAULT 0
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
            }
            lifecycle_added = "lifecycle_state" not in media_columns
            for column, definition in media_migrations.items():
                if column not in media_columns:
                    c.execute(
                        f"ALTER TABLE media_library ADD COLUMN {column} {definition}"
                    )
            if lifecycle_added:
                c.execute("""
                    UPDATE media_library
                    SET lifecycle_state = CASE
                        WHEN file_deleted = 1 THEN 'deleted'
                        WHEN used = 1 THEN 'used'
                        ELSE 'available'
                    END
                """)
            else:
                c.execute("""
                    UPDATE media_library SET lifecycle_state = CASE
                        WHEN file_deleted = 1 THEN 'deleted'
                        WHEN used = 1 THEN 'used'
                        ELSE 'available'
                    END
                    WHERE lifecycle_state IS NULL OR lifecycle_state = ''
                """)

            # Crescita rete: account seguiti dal ciclo di growth, per capire
            # chi ha ricambiato e decidere l'unfollow automatico se non lo
            # fa entro GROWTH_UNFOLLOW_AFTER_DAYS
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

            duplicate_slots = c.execute(
                "SELECT intended_slot FROM post_drafts "
                "WHERE status IN (" + _LIVE_DRAFT_STATUS_SQL + ") "
                "GROUP BY intended_slot HAVING COUNT(*) > 1"
            ).fetchall()
            for duplicate in duplicate_slots:
                rows = c.execute(
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
                    c.execute(
                        "UPDATE post_drafts SET status = 'superseded', "
                        "error = 'migration_duplicate_slot', updated_at = ?, "
                        "revision = revision + 1 WHERE id = ?",
                        (now, stale["id"]),
                    )
                    c.execute("""
                        UPDATE media_library
                        SET lifecycle_state = 'available',
                            reserved_by_draft_id = NULL
                        WHERE reserved_by_draft_id = ?
                          AND lifecycle_state = 'reserved'
                    """, (stale["id"],))
                    c.execute("""
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
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_post_drafts_live_intended_slot "
                "ON post_drafts(intended_slot) WHERE status IN ("
                + _LIVE_DRAFT_STATUS_SQL
                + ")"
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
                    datetime.fromisoformat(effective_verified_at) + timedelta(days=90)
                ).isoformat()

        with self._conn() as conn:
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
    ):
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

    def get_recent_content_texts(self, days: int = 30) -> List[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT text, created_at FROM post_drafts WHERE created_at >= ?
                UNION ALL
                SELECT text, created_at FROM posted_tweets WHERE created_at >= ?
                ORDER BY created_at DESC
            """, (since, since)).fetchall()
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
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
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
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
                or current_time >= self._parse_datetime(current["intended_slot"])
            ):
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
    ):
        """Supersede and replace one exact draft in a single transaction."""
        expected_slot = self._normalize_datetime_iso(expected_slot)
        now = self._now_iso()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
        try:
            with self._conn() as conn:
                cursor = conn.execute(
                    "UPDATE post_drafts SET " + ", ".join(assignments)
                    + " WHERE id = ? AND status IN (" + placeholders + ")",
                    values,
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            return False

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
    ) -> int:
        """Atomically persist an available media row and its audit source."""
        now = self._now_iso()
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO media_library (
                    filename, filepath, media_type, category, ai_description,
                    ai_tags, lifecycle_state, user_context, mime_type, file_size
                ) VALUES (?, ?, ?, ?, ?, ?, 'available', ?, ?, ?)
            """, (
                filename, filepath, media_type, category, ai_description,
                ai_tags, user_context, mime_type, file_size,
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
            return media_id

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
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'reserved', reserved_by_draft_id = ?
                WHERE id = ? AND lifecycle_state = 'available'
                  AND file_deleted = 0
            """, (draft_id, media_id))
            return cursor.rowcount == 1

    def attach_media_to_draft(self, media_id: int, draft_id: int) -> bool:
        """Atomically reserve media and append its trace source to a draft."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
                media_id, json.dumps(source_ids), now, draft_id, draft["revision"],
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

    def release_media_for_draft(self, draft_id: int) -> None:
        with self._conn() as conn:
            draft = conn.execute(
                "SELECT intended_slot, category FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            media_ids = [row["id"] for row in conn.execute("""
                SELECT id FROM media_library
                WHERE reserved_by_draft_id = ? AND lifecycle_state = 'reserved'
            """, (draft_id,)).fetchall()]
            conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'available', reserved_by_draft_id = NULL
                WHERE reserved_by_draft_id = ? AND lifecycle_state = 'reserved'
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
        with self._conn() as conn:
            conn.execute("""
                UPDATE media_library
                SET used = 1, used_at = ?, used_in_tweet_id = ?,
                    lifecycle_state = 'used', reserved_by_draft_id = NULL
                WHERE id = ? AND lifecycle_state IN ('available', 'reserved')
                  AND file_deleted = 0
            """, (self._now_iso(), tweet_id, media_id))

    def archive_media(self, media_id: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE media_library
                SET lifecycle_state = 'archived', reserved_by_draft_id = NULL
                WHERE id = ? AND lifecycle_state = 'available'
            """, (media_id,))
            return cursor.rowcount == 1

    def set_media_reusable(self, media_id: int, reusable: bool) -> bool:
        with self._conn() as conn:
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

    def mark_media_file_deleted(self, media_id: int):
        """Segna che il file fisico è stato rimosso dal disco per risparmiare
        spazio (il record resta nel DB come storico/audit)."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE media_library
                SET file_deleted = 1, lifecycle_state = 'deleted',
                    reserved_by_draft_id = NULL
                WHERE id = ?
            """, (media_id,))

    def update_media(self, media_id: int, category: Optional[str] = None,
                      ai_description: Optional[str] = None):
        with self._conn() as conn:
            if category is not None:
                conn.execute("UPDATE media_library SET category = ? WHERE id = ?", (category, media_id))
            if ai_description is not None:
                conn.execute("UPDATE media_library SET ai_description = ? WHERE id = ?", (ai_description, media_id))

    def delete_media(self, media_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM media_library WHERE id = ?", (media_id,))

    # ---------- Crescita rete (follow/unfollow per costruire seguito reale) ----------

    def add_growth_follow(self, username: str, user_id: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO growth_follows (username, user_id) VALUES (?, ?)
            """, (username, user_id))

    def count_growth_follows_today(self) -> int:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as c FROM growth_follows WHERE date(followed_at) = date('now')
            """).fetchone()
            return row['c'] if row else 0

    def already_growth_followed(self, user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT 1 FROM growth_follows WHERE user_id = ? LIMIT 1
            """, (user_id,)).fetchone()
            return row is not None

    def get_growth_follows_pending_check(self, days_old: int = 21) -> List[Dict]:
        """
        Account seguiti da almeno `days_old` giorni, non ancora segnati come
        'ha ricambiato' e non ancora rimossi: candidati per il controllo di
        unfollow automatico.
        """
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM growth_follows
                WHERE unfollowed = 0 AND followed_back = 0
                AND julianday('now') - julianday(followed_at) >= ?
            """, (days_old,)).fetchall()
            return [dict(r) for r in rows]

    def mark_growth_followed_back(self, follow_id: int):
        with self._conn() as conn:
            conn.execute("""
                UPDATE growth_follows SET followed_back = 1, checked_at = ? WHERE id = ?
            """, (datetime.now().isoformat(), follow_id))

    def mark_growth_unfollowed(self, follow_id: int):
        with self._conn() as conn:
            conn.execute("""
                UPDATE growth_follows SET unfollowed = 1, unfollowed_at = ? WHERE id = ?
            """, (datetime.now().isoformat(), follow_id))

    # ---------- Growth candidates and follower snapshots ----------

    def upsert_growth_candidate(self, candidate: Dict) -> int:
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
                str(candidate["user_id"]),
                candidate["username"],
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
                (str(candidate["user_id"]),),
            ).fetchone()
            return row["id"]

    def _decode_growth_candidate(self, row: sqlite3.Row) -> Dict:
        return self._decode_json_fields(row, {
            "profile_json": "profile",
            "latest_post_json": "latest_post",
            "score_json": "score_data",
        })

    def get_digest_candidates(self, limit: int = 5) -> List[Dict]:
        now = datetime.now(timezone.utc)
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
            """, (now.isoformat(),))
            rows = conn.execute("""
                SELECT * FROM growth_candidates
                WHERE decision = 'new' AND score >= 75
                ORDER BY score DESC, first_seen_at ASC
            """).fetchall()
        eligible = []
        for row in rows:
            try:
                if self._parse_datetime(row["profile_expires_at"]) <= now:
                    continue
                if (
                    row["suppressed_until"]
                    and self._parse_datetime(row["suppressed_until"]) > now
                ):
                    continue
            except (TypeError, ValueError):
                continue
            eligible.append(self._decode_growth_candidate(row))
            if len(eligible) == limit:
                break
        return eligible

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
        if user_id is None:
            raise ValueError("Follower profile requires user_id or id")
        username = profile.get("username", "")
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO follower_snapshots (
                    observed_on, user_id, username, relevant, source,
                    profile_json, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                observed_on,
                str(user_id),
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
        with self._conn() as conn:
            cursor = conn.execute("""
                UPDATE growth_candidates SET followed_back_at = ?
                WHERE user_id = ? AND followed_back_at IS NULL
            """, (observed_at, str(user_id)))
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

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

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
