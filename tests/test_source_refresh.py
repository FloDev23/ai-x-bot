from modules.source_refresh import SourceRefreshCoordinator


class BlogClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class Database:
    def __init__(self, blog_counts=None, blog_error=None):
        self.blog_counts = blog_counts or {
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
        }
        self.blog_error = blog_error
        self.blog_batches = []

    def import_owned_blog_articles(self, records):
        self.blog_batches.append(records)
        if self.blog_error:
            raise self.blog_error
        return dict(self.blog_counts)


class NewsIngestor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def refresh_verified_news(self, topics, per_topic=1):
        self.calls.append((list(topics), per_topic))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_refresh_keeps_blog_success_when_external_news_fails():
    database = Database({"inserted": 2, "updated": 0, "unchanged": 1})
    coordinator = SourceRefreshCoordinator(
        database,
        BlogClient([{"slug": "one"}, {"slug": "two"}]),
        NewsIngestor(RuntimeError("SECRET_NEWS")),
    )

    result = coordinator.refresh(["gym operations"], per_topic=1)

    assert result.blog.inserted == 2
    assert result.blog.updated == 0
    assert result.blog.unchanged == 1
    assert result.blog.error_code == ""
    assert result.news.inserted == 0
    assert result.news.error_code == "external_news_refresh_failed"
    assert "SECRET" not in repr(result)


def test_refresh_keeps_external_news_success_when_blog_fails():
    news = NewsIngestor(3)
    coordinator = SourceRefreshCoordinator(
        Database(),
        BlogClient(RuntimeError("SECRET_BLOG")),
        news,
    )

    result = coordinator.refresh(["fitness business"], per_topic=2)

    assert result.blog.error_code == "blog_refresh_failed"
    assert result.blog.inserted == 0
    assert result.news.inserted == 3
    assert result.news.error_code == ""
    assert news.calls == [(["fitness business"], 2)]
    assert "SECRET" not in repr(result)


def test_refresh_reports_both_success_and_both_failure_with_closed_shape():
    success = SourceRefreshCoordinator(
        Database({"inserted": 1, "updated": 2, "unchanged": 3}),
        BlogClient([{"slug": "one"}]),
        NewsIngestor(4),
    ).refresh(["one"])
    failure = SourceRefreshCoordinator(
        Database(),
        BlogClient(RuntimeError("blog payload")),
        NewsIngestor(RuntimeError("news payload")),
    ).refresh(["one"])

    assert success.blog.__dict__ == {
        "inserted": 1,
        "updated": 2,
        "unchanged": 3,
        "error_code": "",
    }
    assert success.news.__dict__ == {
        "inserted": 4,
        "updated": 0,
        "unchanged": 0,
        "error_code": "",
    }
    assert failure.blog.error_code == "blog_refresh_failed"
    assert failure.news.error_code == "external_news_refresh_failed"
    assert "payload" not in repr(failure)


def test_refresh_treats_disabled_external_allowlist_as_clean_no_change():
    result = SourceRefreshCoordinator(
        Database(),
        BlogClient([]),
        NewsIngestor(0),
    ).refresh([], per_topic=1)

    assert result.blog.unchanged == 0
    assert result.news.inserted == 0
    assert result.news.error_code == ""
