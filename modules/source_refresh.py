from dataclasses import dataclass


@dataclass(frozen=True)
class SourceRefreshChannel:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    error_code: str = ""


@dataclass(frozen=True)
class SourceRefreshResult:
    blog: SourceRefreshChannel
    news: SourceRefreshChannel


def _valid_count(value):
    if type(value) is not int or value < 0 or value > 1_000_000:
        raise ValueError("invalid refresh result")
    return value


class SourceRefreshCoordinator:
    def __init__(self, database, editorial_feed_client, news_ingestor):
        self.database = database
        self.editorial_feed_client = editorial_feed_client
        self.news_ingestor = news_ingestor

    def refresh(self, topics, per_topic=1):
        try:
            records = self.editorial_feed_client.fetch()
            counts = self.database.import_owned_blog_articles(records)
            if type(counts) is not dict or frozenset(counts) != frozenset({
                "inserted", "updated", "unchanged",
            }):
                raise ValueError("invalid blog refresh result")
            blog = SourceRefreshChannel(
                inserted=_valid_count(counts["inserted"]),
                updated=_valid_count(counts["updated"]),
                unchanged=_valid_count(counts["unchanged"]),
            )
        except Exception:
            blog = SourceRefreshChannel(error_code="blog_refresh_failed")

        try:
            inserted = self.news_ingestor.refresh_verified_news(
                topics,
                per_topic=per_topic,
            )
            news = SourceRefreshChannel(inserted=_valid_count(inserted))
        except Exception:
            news = SourceRefreshChannel(
                error_code="external_news_refresh_failed",
            )

        return SourceRefreshResult(blog=blog, news=news)
