"""Import only complete, domain-verified news into the source pool."""
from typing import Iterable, Optional

from config import NEWS_TRUSTED_DOMAINS
from modules.source_validation import is_safe_https_url


class SourceIngestor:
    def __init__(
        self,
        database,
        news_fetcher,
        trusted_domains: Optional[Iterable[str]] = None,
    ):
        self.database = database
        self.news_fetcher = news_fetcher
        domains = NEWS_TRUSTED_DOMAINS if trusted_domains is None else trusted_domains
        self.trusted_domains = {
            domain.strip().lower().rstrip(".")
            for domain in domains
            if isinstance(domain, str) and domain.strip()
        }

    def refresh_verified_news(self, topics, per_topic: int = 1) -> int:
        """Store at most ``per_topic`` complete articles from trusted domains."""
        if (
            not self.trusted_domains
            or type(per_topic) is not int
            or per_topic <= 0
        ):
            return 0

        records = []
        seen_urls = set()
        for topic in topics:
            articles = self.news_fetcher.get_trending_news(topic, limit=per_topic)
            stored_for_topic = 0
            for article in articles:
                if stored_for_topic >= per_topic:
                    break
                details = self._valid_article_details(article)
                if not details or details["url"] in seen_urls:
                    continue
                seen_urls.add(details["url"])
                records.append({
                    "source_type": "verified_news",
                    "text": details["summary"],
                    "url": details["url"],
                    "metadata": {
                        "title": details["title"],
                        "summary": details["summary"],
                        "published_at": details["published_at"],
                        "source_name": details["source_name"],
                    },
                    "trust_state": "verified",
                    "verified_by": "trusted_news_ingestion",
                })
                stored_for_topic += 1
        return self.database.insert_verified_news_batch(records)

    def _valid_article_details(self, article):
        if not isinstance(article, dict):
            return None
        title = self._nonempty(article.get("title"))
        summary = self._nonempty(article.get("description") or article.get("summary"))
        url = self._nonempty(article.get("url"))
        published_at = self._nonempty(article.get("published_at") or article.get("publishedAt"))
        raw_source = article.get("source")
        source_name = self._nonempty(
            raw_source.get("name") if isinstance(raw_source, dict) else raw_source
        )
        if not all((title, summary, url, published_at, source_name)):
            return None
        if not self._is_trusted_https_url(url):
            return None
        return {
            "title": title,
            "summary": summary,
            "url": url,
            "published_at": published_at,
            "source_name": source_name,
        }

    @staticmethod
    def _nonempty(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _is_trusted_https_url(self, url: str) -> bool:
        return is_safe_https_url(url, self.trusted_domains)
