import logging
import tweepy
from config import (
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN,
    TWITTER_ACCESS_TOKEN_SECRET,
    TWITTER_BEARER_TOKEN
)
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class TwitterClient:
    """Gestisce l'interazione con X (Twitter) API"""
    
    def __init__(self):
        # Autenticazione con OAuth 2.0
        self.client = tweepy.Client(
            bearer_token=TWITTER_BEARER_TOKEN,
            consumer_key=TWITTER_API_KEY,
            consumer_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
            wait_on_rate_limit=True
        )
        
        # API v1.1 per alcune operazioni
        auth = tweepy.OAuthHandler(TWITTER_API_KEY, TWITTER_API_SECRET)
        auth.set_access_token(TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET)
        self.api = tweepy.API(auth, wait_on_rate_limit=True)
    
    def upload_media(self, filepath: str, media_type: str = "image") -> Optional[str]:
        """
        Carica un'immagine o un video su X (API v1.1, richiede l'auth OAuth1
        già configurata in self.api) e ritorna il media_id da allegare al
        tweet. Per i video usa l'upload chunked con media_category
        'tweet_video' (obbligatorio per X, gestisce anche l'attesa del
        processing lato server tramite tweepy).
        """
        try:
            if media_type == "video":
                media = self.api.media_upload(filepath, chunked=True, media_category="tweet_video")
            else:
                media = self.api.media_upload(filepath)
            return media.media_id_string
        except Exception as e:
            logger.error(f"❌ Errore upload media ({filepath}): {e}")
            return None

    def post_tweet(self, text: str, reply_to: Optional[str] = None,
                    media_path: Optional[str] = None, media_type: str = "image") -> Optional[Dict]:
        """
        Posta un tweet, opzionalmente con un'immagine o un video allegato
        (dalla libreria media). Se l'upload del media fallisce, il tweet
        viene comunque pubblicato solo testo, invece di bloccare tutto.

        Args:
            text: Testo del tweet
            reply_to: ID del tweet a cui rispondere (opzionale)
            media_path: percorso locale del file da allegare (opzionale)
            media_type: 'image' o 'video', per scegliere il tipo di upload corretto
        
        Returns:
            Risposta dell'API
        """
        try:
            params = {"text": text}

            if reply_to:
                params["reply_settings"] = "public"
                params["in_reply_to_tweet_id"] = reply_to

            if media_path:
                media_id = self.upload_media(media_path, media_type)
                if media_id:
                    params["media_ids"] = [media_id]
                else:
                    logger.warning(f"⚠️ Upload media fallito per {media_path}: pubblico solo il testo")

            response = self.client.create_tweet(**params)
            logger.info(f"✅ Tweet postato: {response}")
            return response
        
        except Exception as e:
            logger.error(f"❌ Errore nel posting del tweet: {e}")
            return None
    
    def reply_to_tweet(self, tweet_id: str, text: str) -> Optional[Dict]:
        """
        Risponde a un tweet
        
        Args:
            tweet_id: ID del tweet a cui rispondere
            text: Testo della risposta
        
        Returns:
            Risposta dell'API
        """
        try:
            response = self.client.create_tweet(
                text=text,
                in_reply_to_tweet_id=tweet_id,
                reply_settings="public"
            )
            logger.info(f"✅ Risposta postata: {response}")
            return response
        
        except Exception as e:
            logger.error(f"❌ Errore nel posting della risposta: {e}")
            return None
    
    def search_tweets(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Cerca tweet su X

        NOTA COSTI (X API 2026): ogni chiamata di ricerca ha un costo (~$0.005
        a lettura). Va quindi usata con query mirate e poche volte al giorno
        (vedi lead_finder.py e engagement.py), non a ciclo continuo come nella v1.

        Args:
            query: Query di ricerca
            limit: Numero massimo di risultati

        Returns:
            Lista di tweet
        """
        try:
            tweets = self.client.search_recent_tweets(
                query=query,
                max_results=max(10, min(limit, 100)),
                tweet_fields=['public_metrics', 'author_id', 'created_at'],
                expansions=['author_id'],
                user_fields=['username']
            )

            if tweets.data:
                users_by_id = {}
                if tweets.includes and 'users' in tweets.includes:
                    users_by_id = {u.id: u.username for u in tweets.includes['users']}

                result = []
                for tweet in tweets.data:
                    result.append({
                        'id': tweet.id,
                        'text': tweet.text,
                        'author_id': tweet.author_id,
                        'author_username': users_by_id.get(tweet.author_id, ''),
                        'engagement_score': sum([
                            tweet.public_metrics.get('like_count', 0),
                            tweet.public_metrics.get('retweet_count', 0),
                            tweet.public_metrics.get('reply_count', 0)
                        ])
                    })
                logger.info(f"✅ Trovati {len(result)} tweet per: {query}")
                return result

            return []

        except Exception as e:
            logger.error(f"❌ Errore nella ricerca dei tweet: {e}")
            return []

    def like_tweet(self, tweet_id: str) -> bool:
        """Mette like a un tweet"""
        try:
            self.client.like(tweet_id)
            logger.info(f"👍 Like a tweet {tweet_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Errore nel mettere like: {e}")
            return False

    def follow_user(self, user_id_or_username: str) -> bool:
        """Segue un utente, dato lo user_id (preferito) o lo username come fallback"""
        try:
            target_id = user_id_or_username
            if not str(user_id_or_username).isdigit():
                info = self.get_user_info(user_id_or_username)
                if not info:
                    return False
                target_id = info['id']
            self.client.follow_user(target_id)
            logger.info(f"➕ Follow a {user_id_or_username}")
            return True
        except Exception as e:
            logger.error(f"❌ Errore nel follow: {e}")
            return False

    def unfollow_user(self, user_id: str) -> bool:
        """Smette di seguire un utente (punto crescita rete: unfollow chi non ricambia)"""
        try:
            self.client.unfollow_user(user_id)
            logger.info(f"➖ Unfollow di {user_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Errore nell'unfollow: {e}")
            return False

    def get_authenticated_user_id_cached(self) -> Optional[str]:
        """Wrapper con cache in memoria per evitare letture ripetute inutili"""
        if not hasattr(self, '_cached_self_id'):
            self._cached_self_id = self.get_authenticated_user_id()
        return self._cached_self_id

    def get_follower_ids(self, max_results: int = 1000) -> set:
        """
        Recupera gli ID di chi segue il bot, per verificare se un account
        che il bot ha seguito ha ricambiato (usato dal ciclo di unfollow
        automatico). Una singola chiamata paginata, non per ogni utente
        singolarmente: molto più economica.
        """
        follower_ids = set()
        try:
            self_id = self.get_authenticated_user_id_cached()
            if not self_id:
                return follower_ids
            paginator = tweepy.Paginator(
                self.client.get_users_followers, self_id, max_results=1000,
            )
            for page in paginator:
                if page.data:
                    follower_ids.update(str(u.id) for u in page.data)
                if len(follower_ids) >= max_results:
                    break
        except Exception as e:
            logger.error(f"❌ Errore nel recupero follower: {e}")
        return follower_ids

    def get_user_info(self, username: str) -> Optional[Dict]:
        """
        Recupera dati pubblici di un utente (follower, verifica) per lo
        scoring degli influencer (punto 7). Da chiamare con parsimonia:
        anche questa è una lettura a pagamento.
        """
        try:
            user = self.client.get_user(
                username=username.lstrip('@'),
                user_fields=['public_metrics', 'verified']
            )
            if not user.data:
                return None
            metrics = user.data.public_metrics or {}
            return {
                'id': user.data.id,
                'username': user.data.username,
                'followers_count': metrics.get('followers_count', 0),
                'verified': getattr(user.data, 'verified', False),
                'engagement_avg': 1.0,  # placeholder: da raffinare con storico tweet reale
            }
        except Exception as e:
            logger.error(f"❌ Errore nel recuperare info utente @{username}: {e}")
            return None

    def get_latest_tweet(self, username: str) -> Optional[Dict]:
        """Recupera l'ultimo tweet pubblico di un utente target curato"""
        try:
            info = self.get_user_info(username)
            if not info:
                return None
            tweets = self.client.get_users_tweets(
                id=info['id'], max_results=5, tweet_fields=['public_metrics']
            )
            if not tweets.data:
                return None
            t = tweets.data[0]
            return {'id': t.id, 'text': t.text}
        except Exception as e:
            logger.error(f"❌ Errore nel recuperare ultimo tweet di @{username}: {e}")
            return None

    def get_tweet_metrics(self, tweet_ids: List[str]) -> Dict[str, Dict]:
        """
        Legge le metriche pubbliche dei PROPRI tweet (owned read, più economico
        della search generica - vedi modules/analytics.py). Ritorna
        {tweet_id: {impression_count, like_count, retweet_count, reply_count, bookmark_count}}
        """
        result = {}
        try:
            tweets = self.client.get_tweets(
                ids=tweet_ids,
                tweet_fields=['public_metrics', 'non_public_metrics']
            )
            if not tweets.data:
                return result
            for t in tweets.data:
                metrics = dict(t.public_metrics or {})
                non_public = getattr(t, 'non_public_metrics', None) or {}
                metrics['impression_count'] = non_public.get(
                    'impression_count', metrics.get('impression_count', 0)
                )
                result[t.id] = metrics
            return result
        except Exception as e:
            logger.error(f"❌ Errore nel recuperare metriche: {e}")
            return result

    def get_authenticated_user_id(self) -> Optional[str]:
        """
        Ottiene l'ID dell'utente autenticato
        
        Returns:
            ID dell'utente
        """
        try:
            user = self.client.get_me()
            return user.data.id
        except Exception as e:
            logger.error(f"❌ Errore nell'ottenere l'ID utente: {e}")
            return None