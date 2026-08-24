import logging
import requests
from config import NEWSAPI_KEY, NEWSAPI_BASE_URL, MAX_SEARCH_RESULTS
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class NewsFetchUnavailable(RuntimeError):
    pass

class NewsFetcher:
    """Fetcha notizie da NewsAPI"""
    
    def __init__(
        self,
        api_key: str = NEWSAPI_KEY,
        base_url: str = NEWSAPI_BASE_URL,
        requests_client=requests,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.requests = requests_client

    def _fetch(self, path, params, limit):
        try:
            response = self.requests.get(
                f"{self.base_url}/{path}",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if type(payload) is not dict or type(payload.get("articles")) is not list:
                raise ValueError("invalid response shape")
            return payload["articles"][:limit]
        except Exception:
            logger.error("News fetch unavailable")
            raise NewsFetchUnavailable("news_fetch_unavailable") from None
    
    def get_trending_news(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> List[Dict]:
        """
        Fetcha notizie trending su un argomento specifico
        
        Args:
            query: Topic da cercare
            limit: Numero massimo di risultati
        
        Returns:
            Lista di articoli
        """
        if not self.api_key:
            logger.info("News fetching disabled because NEWSAPI_KEY is not configured")
            return []
        params = {
            'q': query,
            'sortBy': 'publishedAt',
            'language': 'en',
            'apiKey': self.api_key
        }
        return self._fetch("everything", params, limit)
    
    def get_latest_news_by_source(self, source: str, limit: int = MAX_SEARCH_RESULTS) -> List[Dict]:
        """
        Fetcha notizie da una fonte specifica
        
        Args:
            source: Fonte di notizie
            limit: Numero massimo di risultati
        
        Returns:
            Lista di articoli
        """
        if not self.api_key:
            logger.info("News fetching disabled because NEWSAPI_KEY is not configured")
            return []
        params = {
            'sources': source,
            'sortBy': 'publishedAt',
            'apiKey': self.api_key
        }
        return self._fetch("top-headlines", params, limit)
