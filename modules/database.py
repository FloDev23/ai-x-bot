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
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = 'bot_data.db'


class Database:
    """Wrapper SQLite per tutta la memoria persistente del bot"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_schema()

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
            row = conn.execute("SELECT 1 FROM leads WHERE tweet_id = ?", (tweet_id,)).fetchone()
            return row is not None

    def get_open_leads(self, min_score: int = 0, limit: int = 50) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM leads WHERE status = 'nuovo' AND score >= ?
                ORDER BY score DESC LIMIT ?
            """, (min_score, limit)).fetchall()
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
