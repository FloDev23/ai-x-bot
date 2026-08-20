import logging
import os
import re
import tweepy
from collections.abc import Mapping
from dataclasses import dataclass
from requests import exceptions as requests_exceptions
from config import (
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN,
    TWITTER_ACCESS_TOKEN_SECRET,
    TWITTER_BEARER_TOKEN
)
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FollowerProfilesRead:
    """One paginated follower traversal and whether it reached the end."""

    profiles: Tuple[Dict, ...]
    complete: bool


class XPublicationError(RuntimeError):
    """Base class for failures at the X publication boundary."""


class XPublicationUnknown(XPublicationError):
    """X may have accepted the write, so an automatic retry is unsafe."""


class XPublicationRejected(XPublicationError):
    """X definitively rejected the write before a tweet was created."""


class XPublicationPaused(XPublicationError):
    """The final publication gate closed before tweet creation."""


def _publication_outcome_is_unknown(error: BaseException) -> bool:
    """Classify transport/server failures conservatively, including wrappers."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(
            current,
            (
                TimeoutError,
                ConnectionError,
                requests_exceptions.Timeout,
                requests_exceptions.ConnectionError,
            ),
        ):
            return True
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if type(status_code) is int and status_code >= 500:
            return True
        for related in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(related, BaseException):
                pending.append(related)
    return False


def _publication_rejection_is_definite(error: BaseException) -> bool:
    """Only a concrete 4xx response proves that X rejected the write."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if (
            type(status_code) is int
            and 400 <= status_code < 500
        ):
            return True
        for related in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(related, BaseException):
                pending.append(related)
    return False


def _raise_publication_error(error: BaseException) -> None:
    if isinstance(error, XPublicationError):
        raise error
    if _publication_outcome_is_unknown(error):
        raise XPublicationUnknown("x_publication_outcome_unknown") from error
    if _publication_rejection_is_definite(error):
        raise XPublicationRejected("x_rejected_publication") from error
    raise XPublicationUnknown("x_publication_outcome_unknown") from error

class TwitterClient:
    """Gestisce l'interazione con X (Twitter) API"""
    
    def __init__(self):
        # Autenticazione con OAuth 2.0
        self._client = tweepy.Client(
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
        self._api = tweepy.API(auth, wait_on_rate_limit=True)
    
    def _upload_media(
        self,
        media_file: BinaryIO,
        media_type: str = "image",
        filename: Optional[str] = None,
    ) -> str:
        """Upload an already-open verified stream without reopening a path."""
        if not callable(getattr(media_file, "read", None)):
            raise XPublicationRejected("verified_media_stream_required")
        safe_filename = filename or (
            "approved-video.mp4" if media_type == "video" else "approved-image.jpg"
        )
        if (
            type(safe_filename) is not str
            or safe_filename in {"", ".", ".."}
            or os.path.basename(safe_filename) != safe_filename
        ):
            raise XPublicationRejected("invalid_media_filename")
        try:
            if media_type == "video":
                media = self._api.media_upload(
                    safe_filename,
                    file=media_file,
                    chunked=True,
                    media_category="tweet_video",
                )
            else:
                media = self._api.media_upload(safe_filename, file=media_file)
        except Exception as error:
            logger.error(
                "x_media_upload_failed error_type=%s", type(error).__name__
            )
            _raise_publication_error(error)
        media_id = getattr(media, "media_id_string", None)
        if not media_id:
            raise XPublicationRejected("x_media_upload_rejected")
        return str(media_id)

    def post_tweet(
        self,
        text: str,
        media_path: Optional[BinaryIO] = None,
        media_type: str = "image",
        *,
        before_write: Optional[Callable[[], bool]] = None,
        media_filename: Optional[str] = None,
    ):
        """
        Create one tweet, optionally from an already verified media stream.

        ``before_write`` runs after any media upload and immediately before
        ``create_tweet``.  A false or unavailable gate prevents the write.
        """
        params = {"text": text}
        if media_path is not None:
            if not callable(getattr(media_path, "read", None)):
                raise XPublicationRejected("verified_media_stream_required")
            media_id = self._upload_media(
                media_path, media_type, filename=media_filename
            )
            params["media_ids"] = [media_id]

        if before_write is not None:
            try:
                allowed = before_write()
            except Exception as error:
                raise XPublicationPaused("publication_gate_unavailable") from error
            if allowed is not True:
                raise XPublicationPaused("publication_paused")

        try:
            response = self._client.create_tweet(**params)
        except Exception as error:
            logger.error("x_tweet_write_failed error_type=%s", type(error).__name__)
            _raise_publication_error(error)

        data = getattr(response, "data", response)
        tweet_id = data.get("id") if isinstance(data, dict) else None
        if not tweet_id:
            raise XPublicationUnknown("x_publication_response_missing_id")
        logger.info("x_tweet_published tweet_id=%s", tweet_id)
        return response
    
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
            tweets = self._client.search_recent_tweets(
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

    def get_authenticated_user_id_cached(self) -> Optional[str]:
        """Wrapper con cache in memoria per evitare letture ripetute inutili"""
        if not hasattr(self, '_cached_self_id'):
            self._cached_self_id = self.get_authenticated_user_id()
        return self._cached_self_id

    @staticmethod
    def _iso_datetime(value) -> Optional[str]:
        if value is None:
            return None
        if type(value) is str:
            return value
        if hasattr(value, "isoformat"):
            normalized = value.isoformat()
            return normalized if type(normalized) is str else None
        return None

    @classmethod
    def _profile_dict(cls, user) -> Optional[Dict]:
        user_id = getattr(user, "id", None)
        username = getattr(user, "username", None)
        if type(user_id) is int and user_id > 0:
            normalized_id = str(user_id)
        elif (
            type(user_id) is str
            and user_id.isascii()
            and user_id.isdigit()
        ):
            normalized_id = user_id
        else:
            return None
        if (
            type(username) is not str
            or re.fullmatch(r"[A-Za-z0-9_]{1,15}", username) is None
        ):
            return None
        description = getattr(user, "description", None)
        if description is None:
            description = ""
        protected = getattr(user, "protected", None)
        location = getattr(user, "location", None)
        created_at = cls._iso_datetime(getattr(user, "created_at", None))
        metrics = getattr(user, "public_metrics", None)
        if (
            type(description) is not str
            or type(protected) is not bool
            or (location is not None and type(location) is not str)
            or not isinstance(metrics, Mapping)
        ):
            return None
        normalized_metrics = {}
        for name in ("followers_count", "following_count"):
            value = metrics.get(name)
            if type(value) is not int or value < 0:
                return None
            normalized_metrics[name] = value
        for name in ("tweet_count", "listed_count"):
            value = metrics.get(name, 0)
            if type(value) is not int or value < 0:
                return None
            normalized_metrics[name] = value
        return {
            "id": normalized_id,
            "user_id": normalized_id,
            "username": username,
            "description": description,
            "protected": protected,
            "location": location,
            "created_at": created_at,
            **normalized_metrics,
            "spam_signals": [],
        }

    @staticmethod
    def _growth_user_fields() -> List[str]:
        return [
            "username",
            "description",
            "protected",
            "location",
            "created_at",
            "public_metrics",
        ]

    def read_followers_profiles(self) -> FollowerProfilesRead:
        """Read one full follower traversal with an explicit completion bit."""
        self_id = self.get_authenticated_user_id_cached()
        if not self_id:
            return FollowerProfilesRead((), False)
        profiles = []
        seen_ids = set()
        seen_tokens = set()
        pagination_token = None
        for _page_number in range(100):
            if pagination_token is not None:
                if pagination_token in seen_tokens:
                    return FollowerProfilesRead(tuple(profiles), False)
                seen_tokens.add(pagination_token)
            params = {
                "id": self_id,
                "max_results": 1000,
                "user_fields": self._growth_user_fields(),
            }
            if pagination_token is not None:
                params["pagination_token"] = pagination_token
            try:
                response = self._client.get_users_followers(**params)
            except Exception as error:
                logger.warning(
                    "x_growth_followers_read_failed error_type=%s",
                    type(error).__name__,
                )
                return FollowerProfilesRead(tuple(profiles), False)
            try:
                response_users = getattr(response, "data", None)
                if not isinstance(response_users, (list, tuple)):
                    raise ValueError("malformed follower page data")
            except Exception as error:
                logger.warning(
                    "x_growth_followers_page_skipped error_type=%s",
                    type(error).__name__,
                )
                return FollowerProfilesRead(tuple(profiles), False)
            for user in response_users:
                try:
                    profile = self._profile_dict(user)
                except Exception as error:
                    logger.warning(
                        "x_growth_follower_record_skipped error_type=%s",
                        type(error).__name__,
                    )
                    continue
                if profile is None or profile["id"] in seen_ids:
                    continue
                seen_ids.add(profile["id"])
                profiles.append(profile)
            try:
                meta = getattr(response, "meta", None)
                if not isinstance(meta, Mapping):
                    raise ValueError("malformed follower page metadata")
                next_token = meta.get("next_token")
            except Exception as error:
                logger.warning(
                    "x_growth_followers_page_skipped error_type=%s",
                    type(error).__name__,
                )
                return FollowerProfilesRead(tuple(profiles), False)
            if next_token is None:
                return FollowerProfilesRead(tuple(profiles), True)
            if type(next_token) is not str or not next_token:
                return FollowerProfilesRead(tuple(profiles), False)
            pagination_token = next_token
        return FollowerProfilesRead(tuple(profiles), False)

    def get_followers_profiles(self) -> List[Dict]:
        """Legacy Task 10 boundary: retain valid rows from a partial traversal."""
        return list(self.read_followers_profiles().profiles)

    def _search_recent_profiles(self, query: str, limit: int) -> List[Dict]:
        try:
            response = self._client.search_recent_tweets(
                query=query,
                max_results=max(10, min(limit, 100)),
                tweet_fields=["author_id", "created_at", "lang"],
                expansions=["author_id"],
                user_fields=self._growth_user_fields(),
            )
        except Exception as error:
            logger.warning(
                "x_growth_search_read_failed error_type=%s",
                type(error).__name__,
            )
            return []
        includes = getattr(response, "includes", None) or {}
        users = includes.get("users", []) if isinstance(includes, dict) else []
        if not isinstance(users, (list, tuple)):
            users = []
        result = []
        seen = set()
        for user in users:
            try:
                profile = self._profile_dict(user)
            except Exception as error:
                logger.warning(
                    "x_growth_search_record_skipped error_type=%s",
                    type(error).__name__,
                )
                continue
            if profile is None or profile["id"] in seen:
                continue
            seen.add(profile["id"])
            result.append(profile)
        return result

    def search_recent_authors(self, query: str, limit: int = 25) -> List[Dict]:
        """Read public profiles for authors returned by one recent search."""
        if type(query) is not str or not query.strip():
            return []
        return self._search_recent_profiles(query.strip(), limit)

    def get_network_candidates(
        self,
        seed_accounts,
        limit: int = 25,
    ) -> List[Dict]:
        """Discover authors around configured seeds with one read-only search."""
        seeds = [
            seed.strip()
            for seed in seed_accounts
            if type(seed) is str
            and re.fullmatch(r"[A-Za-z0-9_]{1,15}", seed.strip()) is not None
        ]
        if not seeds:
            return []
        mentions = " OR ".join(f"@{seed}" for seed in seeds)
        return self._search_recent_profiles(f"({mentions}) -is:retweet", limit)

    def get_latest_original_post(self, user_id: str) -> Optional[Dict]:
        """Read the latest original post, excluding replies and reposts."""
        if type(user_id) is not str or not user_id:
            return None
        try:
            response = self._client.get_users_tweets(
                id=user_id,
                max_results=5,
                exclude=["retweets", "replies"],
                tweet_fields=["created_at", "lang", "public_metrics"],
            )
        except Exception as error:
            logger.warning(
                "x_growth_latest_post_read_failed error_type=%s",
                type(error).__name__,
            )
            return None
        tweets = getattr(response, "data", None) or []
        if not isinstance(tweets, (list, tuple)) or not tweets:
            return None
        latest = tweets[0]
        latest_id = getattr(latest, "id", None)
        if type(latest_id) is int and latest_id > 0:
            normalized_id = str(latest_id)
        elif (
            type(latest_id) is str
            and latest_id.isascii()
            and latest_id.isdigit()
        ):
            normalized_id = latest_id
        else:
            return None
        text = getattr(latest, "text", None)
        created_at = self._iso_datetime(getattr(latest, "created_at", None))
        lang = getattr(latest, "lang", None)
        if lang is None:
            lang = ""
        if (
            type(text) is not str
            or type(created_at) is not str
            or type(lang) is not str
        ):
            return None
        return {
            "id": normalized_id,
            "text": text,
            "created_at": created_at,
            "lang": lang,
            "is_original": True,
        }

    def get_user_info(self, username: str) -> Optional[Dict]:
        """
        Recupera dati pubblici di un utente (follower, verifica) per lo
        scoring degli influencer (punto 7). Da chiamare con parsimonia:
        anche questa è una lettura a pagamento.
        """
        try:
            user = self._client.get_user(
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
            tweets = self._client.get_users_tweets(
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
            tweets = self._client.get_tweets(
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
            user = self._client.get_me()
            return user.data.id
        except Exception as e:
            logger.error(f"❌ Errore nell'ottenere l'ID utente: {e}")
            return None
