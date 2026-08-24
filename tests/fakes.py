from datetime import date
from pathlib import Path
from types import SimpleNamespace


class FakeDatabase:
    """In-memory boundary double for editorial planning tests."""

    def __init__(self):
        self.content_counts = {}
        self.sources = []
        self.drafts_today = 0
        self.links_last_days = 0
        self.source_usage = {}
        self.added_sources = []
        self.telegram_updates = {}
        self.operational_mutations = []
        self.logged_errors = []

    def get_eligible_sources(self):
        return list(self.sources)

    def get_content_mix_counts(self, days=30):
        assert days == 30
        return dict(self.content_counts)

    def count_links_last_days(self, days=7, now=None):
        assert days == 7
        del now
        return self.links_last_days

    def get_content_source_usage(self, source_ids, now=None):
        del now
        if self.source_usage is None:
            return None
        return {
            source_id: {
                "bound_to_live_draft": False,
                "last_published_at": None,
                "last_linked_at": None,
                **self.source_usage.get(source_id, {}),
            }
            for source_id in source_ids
        }

    def count_drafts_for_local_date(self, local_date, timezone_name):
        assert isinstance(local_date, date)
        assert timezone_name == "Europe/Rome"
        return self.drafts_today

    def content_source_exists(self, url):
        return any(source.get("url") == url for source in self.sources)

    def add_content_source(self, **source):
        source_id = len(self.sources) + 1
        stored = {"id": source_id, **source}
        self.sources.append(stored)
        self.added_sources.append(stored)
        return source_id

    def insert_verified_news_batch(self, records):
        inserted = 0
        for record in records:
            if self.content_source_exists(record["url"]):
                continue
            self.add_content_source(**record)
            inserted += 1
        return inserted

    def claim_telegram_update(self, update_id, chat_id):
        if update_id in self.telegram_updates:
            return False
        self.telegram_updates[update_id] = {
            "chat_id": str(chat_id),
            "state": "processing",
            "result": {},
        }
        return True

    def complete_telegram_update(self, update_id, state, result):
        update = self.telegram_updates[update_id]
        if update["state"] == "processing":
            update.update(state=state, result=dict(result))

    def log_error(self, context, error_type, safe_message):
        self.logged_errors.append((context, error_type, safe_message))
        return len(self.logged_errors)


class FakeNewsFetcher:
    def __init__(self):
        self.articles = []
        self.queries = []

    def get_trending_news(self, query, limit=1):
        self.queries.append((query, limit))
        return list(self.articles)


class FakeEditorialFeedClient:
    def __init__(self):
        self.records = []
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return list(self.records)


class FakeXClient:
    """No-network X boundary for orchestration tests."""

    def __init__(self):
        self.posts = []
        self.engagement_writes = []
        self.followers = []

    def post_tweet(self, text, **kwargs):
        self.posts.append((text, kwargs))
        return SimpleNamespace(data={"id": "9001"})

    def get_followers_profiles(self):
        return list(self.followers)

    def read_followers_profiles(self):
        return SimpleNamespace(profiles=list(self.followers), complete=True)

    def search_recent_authors(self, _query):
        return []

    def get_network_candidates(self, _seed_accounts):
        return []

    def get_latest_original_post(self, _user_id):
        return None

    def get_tweet_metrics(self, _tweet_ids):
        return {}

    def search_tweets(self, _query, limit=10):
        del limit
        return []


class FakeGroundedGenerator:
    """Deterministic content boundary with source-aware claim output."""

    def __init__(self):
        self.client = SimpleNamespace()
        self.model = "fake-model"
        self.candidate_indices = []
        self.text = (
            "I decided to reduce posting frequency so every post earns attention."
        )

    def generate_grounded_tweet(
        self, _category, _sources, _include_link, candidate_index=None
    ):
        self.candidate_indices.append(candidate_index)
        return {"text": self.text}

    def rewrite_to_limit(self, _text, _sources, limit=280, category=None):
        return self.text if len(self.text) <= limit else None

    def analyze_claims(self, text, sources):
        source_ids = [source["id"] for source in sources]
        claims = []
        if text.startswith("I "):
            claims.append({
                "type": "first_person",
                "text": text,
                "supported_by": source_ids,
            })
        return {"claims": claims}

    def select_best_media(self, _category, _text, _candidates):
        return None

    def analyze_image(self, _stream, _filename):
        return {
            "category": "studio",
            "description": "Future studio content",
            "tags": ["studio"],
        }

    def translate_review_copy(self, english_text):
        return f"Traduzione italiana fedele: {english_text}"


class FakeEditorialScorer:
    def score_draft(self, _text, sources=None, recent_texts=None):
        del sources, recent_texts
        return {
            "hook": 9,
            "usefulness": 9,
            "specificity": 9,
            "originality": 9,
            "audience_relevance": 9,
            "follow_worthiness": 9,
            "semantic_novelty": 9,
            "total": 90,
        }


class FakeTelegramApi:
    """Telegram boundary supporting cards, callbacks and one local photo."""

    def __init__(self, media_library_dir):
        self.media_library_dir = Path(media_library_dir)
        self.messages = []
        self.media_messages = []
        self.callback_answers = []
        self.downloads = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}

    def send_media(self, chat_id, media, media_type, **kwargs):
        content = media.read()
        self.media_messages.append((str(chat_id), content, media_type, kwargs))
        return {"message_id": len(self.media_messages)}

    def answer_callback(self, callback_id, **kwargs):
        self.callback_answers.append((callback_id, kwargs))
        return True

    def get_updates(self, offset=None, timeout=25):
        del offset, timeout
        return []

    def get_file(self, file_id):
        return {
            "file_id": file_id,
            "file_unique_id": "photo-unique",
            "file_size": 6,
            "file_path": "photos/fake.jpg",
        }

    def download_file(
        self,
        file_path,
        destination,
        *,
        message_filename,
        mime_type,
        expected_size,
    ):
        destination = Path(destination)
        self.downloads.append({
            "file_path": file_path,
            "destination": destination,
            "message_filename": message_filename,
            "mime_type": mime_type,
            "expected_size": expected_size,
        })
        destination.write_bytes(b"\xff\xd8\xff\xe0\x00\x10")
        return destination


class FakeScheduler:
    def __init__(self):
        self.jobs = {}
        self.started = False
        self.shutdown_calls = []

    def add_job(
        self,
        func,
        trigger,
        *,
        id,
        name,
        args=None,
        kwargs=None,
        replace_existing=False,
        coalesce=False,
        max_instances=1,
        misfire_grace_time=None,
    ):
        if id in self.jobs and not replace_existing:
            raise ValueError("duplicate job")
        job = SimpleNamespace(
            func=func,
            trigger=trigger,
            id=id,
            name=name,
            args=tuple(args or ()),
            kwargs=dict(kwargs or {}),
            coalesce=coalesce,
            max_instances=max_instances,
            misfire_grace_time=misfire_grace_time,
            next_run_time=None,
        )
        self.jobs[id] = job
        return job

    def get_jobs(self):
        return list(self.jobs.values())

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.shutdown_calls.append(bool(wait))
        self.started = False


def callback_update(update_id, data, chat_id=42):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def photo_update(update_id, caption="Future studio content", chat_id=42):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "caption": caption,
            "photo": [{
                "file_id": "photo-file",
                "file_unique_id": "photo-unique",
                "width": 1200,
                "height": 800,
                "file_size": 6,
            }],
        },
    }
