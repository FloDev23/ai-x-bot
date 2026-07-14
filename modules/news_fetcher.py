import logging
import requests
from config import NEWSAPI_KEY, NEWSAPI_BASE_URL, MAX_SEARCH_RESULTS
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

class NewsFetcher:
    """Fetcha notizie da NewsAPI"""
    
    def __init__(self):
        self.api_key = NEWSAPI_KEY
        self.base_url = NEWSAPI_BASE_URL
    
    def get_trending_news(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> List[Dict]:
        """
        Fetcha notizie trending su un argomento specifico
        
        Args:
            query: Topic da cercare
            limit: Numero massimo di risultati
        
        Returns:
            Lista di articoli
        """
        try:
            params = {
                'q': query,
                'sortBy': 'publishedAt',
                'language': 'en',
                'apiKey': self.api_key
            }
            
            response = requests.get(
                f'{self.base_url}/everything',
                params=params,
                timeout=10
            )
            response.raise_for_status()
            
            articles = response.json().get('articles', [])
            logger.info(f"✅ Trovate {len(articles)} notizie per: {query}")
            
            return articles[:limit]
        
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Errore nel fetching delle notizie: {e}")
            return []
    
    def get_latest_news_by_source(self, source: str, limit: int = MAX_SEARCH_RESULTS) -> List[Dict]:
        """
        Fetcha notizie da una fonte specifica
        
        Args:
            source: Fonte di notizie
            limit: Numero massimo di risultati
        
        Returns:
            Lista di articoli
        """
        try:
            params = {
                'sources': source,
                'sortBy': 'publishedAt',
                'apiKey': self.api_key
            }
            
            response = requests.get(
                f'{self.base_url}/top-headlines',
                params=params,
                timeout=10
            )
            response.raise_for_status()
            
            articles = response.json().get('articles', [])
            logger.info(f"✅ Trovate {len(articles)} notizie da: {source}")
            
            return articles[:limit]
        
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Errore nel fetching delle notizie: {e}")
            return []
