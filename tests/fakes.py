from datetime import date


class FakeDatabase:
    """In-memory boundary double for editorial planning tests."""

    def __init__(self):
        self.content_counts = {}
        self.sources = []
        self.drafts_today = 0
        self.links_last_days = 0
        self.added_sources = []

    def get_eligible_sources(self):
        return list(self.sources)

    def get_content_mix_counts(self, days=30):
        assert days == 30
        return dict(self.content_counts)

    def count_links_last_days(self, days=7):
        assert days == 7
        return self.links_last_days

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


class FakeNewsFetcher:
    def __init__(self):
        self.articles = []
        self.queries = []

    def get_trending_news(self, query, limit=1):
        self.queries.append((query, limit))
        return list(self.articles)
