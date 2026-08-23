from modules.source_ingestion import SourceIngestor


def test_only_complete_allowlisted_article_becomes_verified_source(fake_db, fake_news):
    fake_news.articles = [
        {
            "title": "Operators rethink class capacity",
            "description": "A concrete reported change.",
            "url": "https://industry.example/report",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Industry Example",
        },
        {
            "title": "Untrusted claim",
            "description": "Should not enter the source pool.",
            "url": "https://spam.example/post",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Spam Example",
        },
    ]
    ingestor = SourceIngestor(fake_db, fake_news, {"industry.example"})

    assert ingestor.refresh_verified_news(["gym operations"]) == 1
    assert fake_db.sources[0]["source_type"] == "verified_news"
    assert fake_db.sources[0]["url"] == "https://industry.example/report"
    assert fake_db.sources[0]["metadata"] == {
        "title": "Operators rethink class capacity",
        "summary": "A concrete reported change.",
        "published_at": "2026-08-10T08:00:00Z",
        "source_name": "Industry Example",
    }


def test_ingestor_rejects_incomplete_or_lookalike_or_duplicate_urls(fake_db, fake_news):
    fake_db.sources = [{"id": 1, "url": "https://industry.example/duplicate"}]
    fake_news.articles = [
        {
            "title": "Missing publication date",
            "description": "Cannot be used.",
            "url": "https://industry.example/missing-date",
            "published_at": "",
            "source": "Industry Example",
        },
        {
            "title": "Lookalike domain",
            "description": "Cannot be used.",
            "url": "https://notindustry.example/report",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Industry Example",
        },
        {
            "title": "Duplicate",
            "description": "Cannot be added twice.",
            "url": "https://industry.example/duplicate",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Industry Example",
        },
        *[
            {
                "title": "Malformed authority",
                "description": "Cannot be verified.",
                "url": url,
                "published_at": "2026-08-10T08:00:00Z",
                "source": "Industry Example",
            }
            for url in (
                "https://industry.example:bad/report",
                "https://industry.example:99999/report",
                "https://user:pass@industry.example/report",
                "https://industry .example/report",
                "https://industry.example../report",
            )
        ],
    ]
    ingestor = SourceIngestor(fake_db, fake_news, {"industry.example"})

    assert ingestor._is_trusted_https_url(
        "https://industry.example.:443/report"
    ) is True
    assert ingestor.refresh_verified_news(["gym operations"], per_topic=10) == 0
    assert len(fake_db.sources) == 1


def test_empty_allowlist_disables_automatic_ingestion(fake_db, fake_news):
    fake_news.articles = [{"title": "Would otherwise be valid"}]
    ingestor = SourceIngestor(fake_db, fake_news, set())

    assert ingestor.refresh_verified_news(["gym operations"]) == 0
    assert fake_news.queries == []
