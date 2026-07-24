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
                    file_deleted INTEGER DEFAULT 0
                )
            """)
            # Migrazione per database creati prima dell'introduzione di
            # file_deleted (il file viene rimosso dal disco dopo l'uso per
            # risparmiare spazio, ma il record resta per lo storico)
            try:
                c.execute("ALTER TABLE media_library ADD COLUMN file_deleted INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # colonna già presente

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
                    SELECT * FROM media_library WHERE used = 0 AND category = ?
                    ORDER BY uploaded_at ASC LIMIT ?
                """, (category, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM media_library WHERE used = 0
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
                SELECT * FROM media_library WHERE used = 0 ORDER BY uploaded_at ASC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_all_media(self, limit: int = 300) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM media_library ORDER BY uploaded_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def mark_media_used(self, media_id: int, tweet_id: str = ''):
        with self._conn() as conn:
            conn.execute("""
                UPDATE media_library SET used = 1, used_at = ?, used_in_tweet_id = ? WHERE id = ?
            """, (datetime.now().isoformat(), tweet_id, media_id))

    def mark_media_file_deleted(self, media_id: int):
        """Segna che il file fisico è stato rimosso dal disco per risparmiare
        spazio (il record resta nel DB come storico/audit)."""
        with self._conn() as conn:
            conn.execute("UPDATE media_library SET file_deleted = 1 WHERE id = ?", (media_id,))

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