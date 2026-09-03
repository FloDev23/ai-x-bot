#!/usr/bin/env python3
"""Approval-only orchestration for the FlexDropin X growth agent."""

import logging
import secrets
import sys
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import (
    ADAPTIVE_TIMING_MIN_POSTS,
    ADAPTIVE_WEEKDAY_MIN_POSTS,
    APPROVED_QUEUE_TARGET,
    AUDIENCE_TIMEZONE,
    BOT_TIMEZONE,
    CONTENT_SLOTS,
    DRAFT_LEAD_MINUTES,
    DRAFT_GENERATION_DAILY_CAP,
    DRAFT_SCORE_THRESHOLD,
    DRY_RUN,
    ENABLE_LEAD_DISCOVERY,
    GROWTH_ACCOUNT_SUGGESTION_LIMIT,
    GROWTH_DIGEST_TIME,
    GROWTH_POST_QUERY_BUDGET,
    GROWTH_POST_SUGGESTION_LIMIT,
    GROWTH_SUGGESTION_COOLDOWN_DAYS,
    LEAD_NOTIFY_MIN_SCORE,
    MEDIA_LIBRARY_DIR,
    MEDIA_MATCH_THRESHOLD,
    MAX_LINKS_PER_WEEK,
    MIN_POST_GAP_HOURS,
    MIDDAY_WINDOW,
    MORNING_WINDOW,
    NEWS_TRUSTED_DOMAINS,
    OPPORTUNITY_CYCLE_TIMES,
    PUBLISH_GRACE_SECONDS,
    PUBLICATION_PLAN_GRACE_MINUTES,
    PENDING_REVIEW_LIMIT,
    THIRD_POST_DAYS_PER_WEEK,
    THIRD_POST_TIMING_MIN_POSTS,
    SEARCH_TOPICS,
    SEMANTIC_DUPLICATE_THRESHOLD,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    X_API_MONTHLY_BUDGET_MICROUSD,
    X_API_UNIT_COSTS_MICROUSD,
    EVENING_WINDOW,
    validate_config,
)
from modules.adaptive_timing import AdaptiveTimingPolicy
from modules.ai_generator import AIGenerator
from modules.analytics import PerformanceAnalyzer
from modules.content_planner import ContentPlanner
from modules.database import Database
from modules.draft_pipeline import DraftPipeline
from modules.editorial_feed import FlexDropinEditorialFeedClient
from modules.fact_guard import FactGuard
from modules.growth_discovery import GrowthDiscovery
from modules.growth_digest import GrowthDigestService
from modules.lead_finder import LeadFinder
from modules.media_matcher import MediaMatcher
from modules.media_processor import MediaProcessor
from modules.news_fetcher import NewsFetcher
from modules.notifier import TelegramNotifier
from modules.publisher import PublishResult, Publisher
from modules.publication_queue import (
    PublicationPlanner,
    QueueReplenisher,
    QueueReplenishResult,
)
from modules.publication_cadence import PublicationCadencePolicy
from modules.review_translation import ReviewTranslator
from modules.scoring import TweetScorer
from modules.source_ingestion import SourceIngestor
from modules.source_refresh import (
    SourceRefreshChannel,
    SourceRefreshCoordinator,
    SourceRefreshResult,
)
from modules.telegram_api import TELEGRAM_POLL_TIMEOUT, TelegramApi
from modules.telegram_controller import TelegramController
from modules.twitter_client import TwitterClient
from modules.x_api_usage import XApiUsageMeter


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class FlexDropinGrowthAgent:
    """Wire safe application components and the explicit production schedule."""

    _DEPENDENCY_KEYS = frozenset({
        "adaptive_timing",
        "analytics",
        "approval_required",
        "authorized_chat_id",
        "clock",
        "content_slots",
        "db",
        "draft_lead_minutes",
        "draft_pipeline",
        "dry_run",
        "editorial_feed_client",
        "fact_guard",
        "generator",
        "growth_discovery",
        "growth_digest",
        "lead_discovery_enabled",
        "lead_cycle_times",
        "lead_finder",
        "media_library_dir",
        "media_matcher",
        "media_processor",
        "news_fetcher",
        "news_trusted_domains",
        "notifier",
        "planner",
        "publisher",
        "publication_planner",
        "publication_cadence",
        "queue_replenisher",
        "review_translator",
        "scheduler",
        "scorer",
        "source_ingestor",
        "source_refresh",
        "telegram_api",
        "telegram_bot_token",
        "telegram_controller",
        "telegram_poll_timeout",
        "timezone_name",
        "x_client",
        "x_api_usage_meter",
    })
    _REQUIRED_INJECTED_BOUNDARIES = frozenset({
        "db",
        "editorial_feed_client",
        "generator",
        "lead_finder",
        "news_fetcher",
        "scheduler",
        "scorer",
        "telegram_api",
        "x_client",
    })

    def __init__(self, dependencies=None):
        if dependencies is None:
            validate_config()
            supplied = {}
            injected = False
        elif isinstance(dependencies, Mapping):
            supplied = dict(dependencies)
            unknown = sorted(set(supplied) - self._DEPENDENCY_KEYS)
            if unknown:
                raise ValueError("unknown dependencies: " + ", ".join(unknown))
            missing = sorted(
                name
                for name in self._REQUIRED_INJECTED_BOUNDARIES
                if supplied.get(name) is None
            )
            if missing:
                raise ValueError(
                    "missing injected dependencies: " + ", ".join(missing)
                )
            injected = True
        else:
            raise TypeError("dependencies must be a mapping or None")

        def resolve(name, factory):
            value = supplied.get(name)
            return factory() if value is None else value

        if supplied.get("approval_required", True) is not True:
            raise ValueError("approval_required must be true")

        self.timezone_name = supplied.get("timezone_name", BOT_TIMEZONE)
        self.timezone = ZoneInfo(self.timezone_name)
        self.clock = supplied.get(
            "clock", lambda: datetime.now(self.timezone)
        )
        self.content_slots = tuple(supplied.get("content_slots", CONTENT_SLOTS))
        self.draft_lead_minutes = supplied.get(
            "draft_lead_minutes", DRAFT_LEAD_MINUTES
        )
        self.dry_run = supplied.get("dry_run", DRY_RUN)
        self.lead_discovery_enabled = supplied.get(
            "lead_discovery_enabled", ENABLE_LEAD_DISCOVERY
        )
        self.lead_cycle_times = tuple(supplied.get(
            "lead_cycle_times", OPPORTUNITY_CYCLE_TIMES
        ))
        self.authorized_chat_id = str(
            supplied.get("authorized_chat_id", TELEGRAM_CHAT_ID)
        )
        self.media_library_dir = supplied.get(
            "media_library_dir", MEDIA_LIBRARY_DIR
        )
        trusted_domains = supplied.get(
            "news_trusted_domains", NEWS_TRUSTED_DOMAINS
        )

        self.db = resolve("db", Database)
        self.x_api_usage_meter = resolve(
            "x_api_usage_meter",
            lambda: XApiUsageMeter(
                self.db,
                monthly_budget_microusd=X_API_MONTHLY_BUDGET_MICROUSD,
                unit_costs_microusd=X_API_UNIT_COSTS_MICROUSD,
                clock=self.clock,
            ),
        )
        telegram_token = supplied.get("telegram_bot_token", TELEGRAM_BOT_TOKEN)
        self.telegram_api = resolve(
            "telegram_api",
            lambda: TelegramApi(
                telegram_token,
                media_library_dir=self.media_library_dir,
            ),
        )
        notifier_token = telegram_token or ("injected" if injected else "")
        self.notifier = resolve(
            "notifier",
            lambda: TelegramNotifier(
                notifier_token,
                self.authorized_chat_id,
                database=self.db,
                telegram_api=self.telegram_api,
            ),
        )

        self.news_fetcher = resolve("news_fetcher", NewsFetcher)
        self.editorial_feed_client = resolve(
            "editorial_feed_client",
            self._build_editorial_feed_client,
        )
        self.ai_generator = resolve("generator", AIGenerator)
        self.review_translator = resolve(
            "review_translator",
            lambda: ReviewTranslator(self.ai_generator),
        )
        self.adaptive_timing = resolve(
            "adaptive_timing",
            lambda: AdaptiveTimingPolicy(
                audience_timezone=AUDIENCE_TIMEZONE,
                morning_window=MORNING_WINDOW,
                midday_window=MIDDAY_WINDOW,
                evening_window=EVENING_WINDOW,
                minimum_gap_hours=MIN_POST_GAP_HOURS,
                timing_min_posts=ADAPTIVE_TIMING_MIN_POSTS,
                weekday_min_posts=ADAPTIVE_WEEKDAY_MIN_POSTS,
            ),
        )
        self.twitter_client = resolve(
            "x_client",
            lambda: TwitterClient(usage_meter=self.x_api_usage_meter),
        )
        self.scorer = resolve(
            "scorer",
            lambda: TweetScorer(
                self.ai_generator.client,
                self.ai_generator.model,
            ),
        )

        self.content_planner = resolve(
            "planner",
            lambda: ContentPlanner(
                self.db,
                timezone_name=self.timezone_name,
                max_links_per_week=MAX_LINKS_PER_WEEK,
            ),
        )
        self.source_ingestor = resolve(
            "source_ingestor",
            lambda: SourceIngestor(
                self.db,
                self.news_fetcher,
                trusted_domains=trusted_domains,
            ),
        )
        self.source_refresh = resolve(
            "source_refresh",
            lambda: SourceRefreshCoordinator(
                self.db,
                self.editorial_feed_client,
                self.source_ingestor,
            ),
        )
        self.fact_guard = resolve(
            "fact_guard",
            lambda: FactGuard(self.ai_generator),
        )
        self.draft_pipeline = resolve(
            "draft_pipeline",
            lambda: DraftPipeline(
                self.db,
                self.content_planner,
                self.ai_generator,
                self.fact_guard,
                self.scorer,
                score_threshold=DRAFT_SCORE_THRESHOLD,
                duplicate_threshold=SEMANTIC_DUPLICATE_THRESHOLD,
                now_fn=self.clock,
                review_translator=self.review_translator,
            ),
        )
        self.media_processor = resolve(
            "media_processor",
            lambda: MediaProcessor(self.db, self.ai_generator),
        )
        self.media_matcher = resolve(
            "media_matcher",
            lambda: MediaMatcher(
                self.db,
                self.ai_generator,
                threshold=MEDIA_MATCH_THRESHOLD,
            ),
        )
        self.queue_replenisher = resolve(
            "queue_replenisher",
            lambda: QueueReplenisher(
                db=self.db,
                pipeline=self.draft_pipeline,
                translator=self.review_translator,
                media_matcher=self.media_matcher,
                operator_timezone="Europe/Rome",
                approved_queue_target=APPROVED_QUEUE_TARGET,
                pending_review_limit=PENDING_REVIEW_LIMIT,
                daily_generation_cap=DRAFT_GENERATION_DAILY_CAP,
            ),
        )
        self.publisher = resolve(
            "publisher",
            lambda: Publisher(
                self.db,
                self.twitter_client,
                dry_run=self.dry_run,
                clock=self.clock,
                grace_seconds=PUBLISH_GRACE_SECONDS,
                timezone_name=self.timezone_name,
                plan_grace_minutes=PUBLICATION_PLAN_GRACE_MINUTES,
            ),
        )
        self.analytics = resolve(
            "analytics",
            lambda: PerformanceAnalyzer(self.twitter_client, self.db),
        )
        self.analyzer = self.analytics
        self.publication_cadence = resolve(
            "publication_cadence",
            lambda: PublicationCadencePolicy(
                audience_timezone=AUDIENCE_TIMEZONE,
                third_days_per_week=THIRD_POST_DAYS_PER_WEEK,
                learning_min_posts=THIRD_POST_TIMING_MIN_POSTS,
            ),
        )
        self.publication_planner = resolve(
            "publication_planner",
            lambda: PublicationPlanner(
                db=self.db,
                timing_policy=self.adaptive_timing,
                cadence_policy=self.publication_cadence,
                timing_sample_provider=self.analytics.timing_samples,
                now_fn=self.clock,
                audience_timezone=AUDIENCE_TIMEZONE,
                installation_id_provider=lambda: secrets.token_hex(16),
                source_expiry_safety_margin=timedelta(hours=2),
                max_links_per_week=MAX_LINKS_PER_WEEK,
                dry_run=self.dry_run,
                plan_grace_minutes=PUBLICATION_PLAN_GRACE_MINUTES,
            ),
        )
        self.growth_discovery = resolve(
            "growth_discovery",
            lambda: GrowthDiscovery(self.twitter_client, self.db),
        )
        self.growth_digest = resolve(
            "growth_digest",
            lambda: GrowthDigestService(
                self.twitter_client,
                self.db,
                discovery=self.growth_discovery,
                account_limit=GROWTH_ACCOUNT_SUGGESTION_LIMIT,
                post_limit=GROWTH_POST_SUGGESTION_LIMIT,
                post_query_budget=GROWTH_POST_QUERY_BUDGET,
                cooldown_days=GROWTH_SUGGESTION_COOLDOWN_DAYS,
            ),
        )
        self.lead_finder = resolve(
            "lead_finder",
            lambda: LeadFinder(
                self.twitter_client,
                self.ai_generator.client,
                self.ai_generator.model,
                self.db,
            ),
        )

        self.scheduler = resolve(
            "scheduler",
            lambda: BackgroundScheduler(timezone=self.timezone),
        )
        self.telegram_controller = resolve(
            "telegram_controller",
            lambda: TelegramController(
                self.telegram_api,
                self.db,
                self.notifier,
                self.authorized_chat_id,
                draft_pipeline=self.draft_pipeline,
                media_processor=self.media_processor,
                media_matcher=self.media_matcher,
                analytics=self.analytics,
                growth_digest=self.growth_digest,
                scheduler_status=self.scheduler_status,
                queue_service=self.queue_replenisher,
                dry_run=self.dry_run,
                now_fn=self.clock,
                poll_timeout=supplied.get(
                    "telegram_poll_timeout", TELEGRAM_POLL_TIMEOUT
                ),
                news_trusted_domains=trusted_domains,
            ),
        )

        self.stop_event = threading.Event()
        self.telegram_thread = None
        self._scheduler_started = False

    @staticmethod
    def _build_editorial_feed_client():
        import requests

        return FlexDropinEditorialFeedClient(requests)

    @staticmethod
    def _slot_parts(slot_time):
        if not isinstance(slot_time, str):
            raise ValueError("slot time must use HH:MM")
        parts = slot_time.split(":")
        if len(parts) != 2 or any(
            len(part) != 2 or not part.isascii() or not part.isdigit()
            for part in parts
        ):
            raise ValueError("slot time must use HH:MM")
        hour, minute = (int(part) for part in parts)
        if hour > 23 or minute > 59:
            raise ValueError("slot time must use HH:MM")
        return hour, minute

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime):
            raise ValueError("clock must return datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def _slot(self, slot_time, now=None):
        hour, minute = self._slot_parts(slot_time)
        moment = self._now() if now is None else now
        if not isinstance(moment, datetime):
            raise ValueError("now must be datetime")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.timezone)
        local = moment.astimezone(self.timezone)
        return local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _notify_error(self, operation, error):
        logger.error("%s failed: %s", operation, type(error).__name__)
        self.notifier.notify_error(operation, error)

    def refresh_sources_cycle(self):
        try:
            result = self.source_refresh.refresh(
                SEARCH_TOPICS,
                per_topic=1,
            )
            if (
                type(result) is not SourceRefreshResult
                or type(result.blog) is not SourceRefreshChannel
                or type(result.news) is not SourceRefreshChannel
                or result.blog.error_code not in {"", "blog_refresh_failed"}
                or result.news.error_code
                not in {"", "external_news_refresh_failed"}
                or any(
                    type(value) is not int or value < 0 or value > 1_000_000
                    for channel in (result.blog, result.news)
                    for value in (
                        channel.inserted,
                        channel.updated,
                        channel.unchanged,
                    )
                )
            ):
                raise ValueError("invalid source refresh result")
        except Exception:
            safe_error = RuntimeError("source_refresh_failed")
            self._notify_error("source_refresh_cycle", safe_error)
            return SourceRefreshResult(
                blog=SourceRefreshChannel(error_code="blog_refresh_failed"),
                news=SourceRefreshChannel(
                    error_code="external_news_refresh_failed",
                ),
            )

        for channel, operation in (
            (result.blog, "blog_source_refresh"),
            (result.news, "external_news_source_refresh"),
        ):
            if channel.error_code:
                self._notify_error(
                    operation,
                    RuntimeError(channel.error_code),
                )
        return result

    def create_draft_cycle(self, intended_slot_time, now=None):
        try:
            intended_slot = self._slot(intended_slot_time, now)
            draft, persistence_outcome = (
                self.draft_pipeline.create_for_slot_with_outcome(intended_slot)
            )
            if not draft:
                return None
            if persistence_outcome != "created":
                return draft
            self.media_matcher.attach_best(draft["id"])
            current = self.db.get_post_draft(draft["id"]) or draft
            self.telegram_controller._send_draft_card(
                self.authorized_chat_id,
                current,
            )
            return current
        except Exception as error:
            self._notify_error("cycle", error)
            return None

    def publish_cycle(self, intended_slot_time, now=None):
        try:
            effective_now = self._now() if now is None else now
            intended_slot = self._slot(intended_slot_time, effective_now)
            draft = self.db.get_active_draft_for_slot(intended_slot.isoformat())
            if not draft:
                return PublishResult("not_found")
            return self.publisher.publish(draft["id"], now=effective_now)
        except Exception as error:
            self._notify_error("cycle", error)
            return PublishResult("publication_failed")

    def queue_replenishment_cycle(self, now=None):
        if self.stop_event.is_set():
            return QueueReplenishResult("failed", None, False)
        try:
            current = self._now() if now is None else now
            result = self.queue_replenisher.run(current)
            if not isinstance(result, QueueReplenishResult):
                raise ValueError("invalid queue replenishment result")
            if (
                not self.stop_event.is_set()
                and result.announce
                and result.draft_id is not None
            ):
                draft = self.db.get_queue_draft(result.draft_id)
                if draft is not None:
                    self.telegram_controller._send_draft_card(
                        self.authorized_chat_id,
                        draft,
                    )
            return result
        except Exception as error:
            self._notify_error("queue_replenishment_cycle", error)
            return QueueReplenishResult("failed", None, False)

    def translation_retry_cycle(self, now=None):
        if self.stop_event.is_set():
            return []
        try:
            current = self._now() if now is None else now
            ready_ids = self.queue_replenisher.retry_pending_translations(
                current,
                limit=3,
            )
            if type(ready_ids) is not list:
                raise ValueError("invalid translation retry result")
            announced = []
            for draft_id in ready_ids:
                if self.stop_event.is_set():
                    break
                if type(draft_id) is not int or draft_id <= 0:
                    continue
                draft = self.db.get_queue_draft(draft_id)
                if draft is None or draft.get("translation_status") != "ready":
                    continue
                self.telegram_controller._send_draft_card(
                    self.authorized_chat_id,
                    draft,
                )
                announced.append(draft_id)
            return announced
        except Exception as error:
            self._notify_error("translation_retry_cycle", error)
            return []

    def publication_planning_cycle(self, now=None):
        if self.stop_event.is_set():
            return []
        try:
            current = self._now() if now is None else now
            plans = self.publication_planner.reconcile(current)
            return plans if type(plans) is list else []
        except Exception as error:
            self._notify_error("publication_planning_cycle", error)
            return []

    def adaptive_publish_cycle(self, now=None):
        if self.stop_event.is_set():
            return []
        try:
            current = self._now() if now is None else now
            results = self.publication_planner.publish_due(
                current,
                publisher=self.publisher,
            )
            return results if type(results) is list else []
        except Exception as error:
            self._notify_error("adaptive_publish_cycle", error)
            return []

    def growth_digest_cycle(self, now=None):
        try:
            current = self._now() if now is None else now
            digest = self.growth_digest.build(current)
            return self.telegram_controller.push_growth_digest(
                digest, explicit=False,
            )
        except Exception as error:
            self._notify_error("growth_digest_cycle", error)
            return "growth_digest_failed"

    def follower_snapshot_cycle(self, now=None):
        try:
            return self.analytics.capture_follower_snapshot(
                self._now() if now is None else now
            )
        except Exception as error:
            self._notify_error("cycle", error)
            return {}

    def performance_metrics_cycle(self):
        try:
            return self.analytics.refresh_own_tweet_metrics()
        except Exception as error:
            self._notify_error("cycle", error)
            return None

    def weekly_growth_report_cycle(self, now=None):
        try:
            return self.telegram_controller.push_weekly_report(
                self._now() if now is None else now
            )
        except Exception as error:
            self._notify_error("cycle", error)
            return "weekly_report_failed"

    def opportunity_cycle(self):
        """Read X for optional leads; never perform an engagement write."""
        try:
            return self.lead_finder.find_opportunities(
                ai_generator=self.ai_generator,
                notifier=self.notifier,
                notify_min_score=LEAD_NOTIFY_MIN_SCORE,
            )
        except Exception as error:
            self._notify_error("opportunity_cycle", error)
            return []

    def _add_cron_job(
        self,
        function,
        *,
        job_id,
        name,
        hour,
        minute,
        args=None,
        day_of_week=None,
        timezone=None,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    ):
        trigger_options = {
            "hour": hour,
            "minute": minute,
            "timezone": self.timezone if timezone is None else timezone,
        }
        if day_of_week is not None:
            trigger_options["day_of_week"] = day_of_week
        return self.scheduler.add_job(
            function,
            CronTrigger(**trigger_options),
            id=job_id,
            name=name,
            args=args,
            replace_existing=True,
            coalesce=coalesce,
            max_instances=max_instances,
            misfire_grace_time=misfire_grace_time,
        )

    def _add_interval_job(self, function, *, job_id, name, minutes):
        if type(minutes) is not int or minutes <= 0:
            raise ValueError("interval minutes must be a positive integer")
        return self.scheduler.add_job(
            function,
            IntervalTrigger(minutes=minutes, timezone=self.timezone),
            id=job_id,
            name=name,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=max(60, minutes * 60),
        )

    def register_jobs(self):
        """Register the complete allowlisted schedule, and nothing else."""
        self._add_cron_job(
            self.refresh_sources_cycle,
            job_id="source_refresh",
            name="Editorial source refresh",
            hour=10,
            minute=30,
        )
        self._add_interval_job(
            self.queue_replenishment_cycle,
            job_id="queue_replenishment",
            name="Approved queue replenishment",
            minutes=30,
        )
        self._add_interval_job(
            self.translation_retry_cycle,
            job_id="translation_retry",
            name="Italian review translation retry",
            minutes=30,
        )
        self._add_interval_job(
            self.publication_planning_cycle,
            job_id="publication_planning",
            name="Adaptive US publication planning",
            minutes=15,
        )
        self._add_interval_job(
            self.adaptive_publish_cycle,
            job_id="adaptive_publish",
            name="Publish due approved plan",
            minutes=5,
        )
        growth_hour, growth_minute = self._slot_parts(GROWTH_DIGEST_TIME)
        self._add_cron_job(
            self.growth_digest_cycle,
            job_id="growth_digest",
            name="Daily manual growth digest",
            hour=growth_hour,
            minute=growth_minute,
            timezone=ZoneInfo("Europe/Rome"),
        )
        self._add_cron_job(
            self.follower_snapshot_cycle,
            job_id="follower_snapshot",
            name="Follower snapshot",
            hour=23,
            minute=15,
        )
        self._add_cron_job(
            self.performance_metrics_cycle,
            job_id="performance_metrics",
            name="Owned post performance metrics",
            hour=23,
            minute=30,
        )
        self._add_cron_job(
            self.weekly_growth_report_cycle,
            job_id="weekly_growth_report",
            name="Weekly growth report",
            day_of_week="mon",
            hour=9,
            minute=0,
        )
        if self.lead_discovery_enabled:
            for cycle_time in self.lead_cycle_times:
                hour, minute = self._slot_parts(cycle_time.strip())
                self._add_cron_job(
                    self.opportunity_cycle,
                    job_id=f"lead_discovery_{cycle_time}",
                    name=f"Optional lead discovery {cycle_time}",
                    hour=hour,
                    minute=minute,
                )
        return self.scheduler.get_jobs()

    def scheduler_status(self):
        result = []
        for job in self.scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            result.append({
                "id": job.id,
                "name": job.name,
                "next_run": (
                    next_run.isoformat()
                    if hasattr(next_run, "isoformat")
                    else None
                ),
            })
        return result

    def _register_telegram_commands(self):
        register = getattr(self.telegram_api, "set_my_commands", None)
        if not callable(register):
            return
        commands = [
            {"command": "posts",   "description": "Bozze in coda e pianificate"},
            {"command": "newpost",   "description": "Crea nuovo post manuale"},
            {"command": "newthread", "description": "Crea thread manuale (2–10 tweet)"},
            {"command": "media",   "description": "Libreria media"},
            {"command": "status",  "description": "Stato bot e conteggi coda"},
            {"command": "growth",  "description": "Digest crescita giornaliero"},
            {"command": "errors",  "description": "Errori recenti"},
            {"command": "stats",   "description": "Report settimanale analytics"},
            {"command": "ideas",   "description": "Aggiungi fonte di contenuto"},
            {"command": "pause",   "description": "Pausa scheduler"},
            {"command": "resume",  "description": "Riprendi scheduler"},
            {"command": "help",    "description": "Aiuto comandi"},
        ]
        ok = register(commands)
        if not ok:
            logger.warning("set_my_commands failed — menu buttons not registered")

    def start(self, block=True):
        """Start the scheduler and one stoppable Telegram polling thread."""
        if self._scheduler_started:
            raise RuntimeError("agent already started")
        self.stop_event.clear()
        self._register_telegram_commands()
        self.register_jobs()
        self.scheduler.start()
        self._scheduler_started = True
        self.telegram_thread = threading.Thread(
            target=self.telegram_controller.run_forever,
            args=(self.stop_event,),
            name="flexdropin-telegram-polling",
            daemon=True,
        )
        self.telegram_thread.start()
        logger.info("Growth agent started with approval-only schedule")
        if not block:
            return
        try:
            while not self.stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            logger.info("Stopping growth agent")
        finally:
            self.shutdown()

    def shutdown(self):
        """Signal polling first, then stop jobs and join the daemon thread."""
        self.stop_event.set()
        if self._scheduler_started:
            self.scheduler.shutdown(wait=True)
            self._scheduler_started = False
        if self.telegram_thread and self.telegram_thread.is_alive():
            poll_timeout = getattr(
                self.telegram_controller,
                "poll_timeout",
                1,
            )
            self.telegram_thread.join(timeout=min(max(poll_timeout + 1, 1), 30))

    def _publish(self, *_args, **_kwargs):
        """Keep the retired entry point fail-closed for old callers."""
        raise RuntimeError("legacy_direct_publication_disabled")


def main():
    try:
        FlexDropinGrowthAgent().start()
    except KeyboardInterrupt:
        logger.info("Growth agent stopped")
    except Exception as error:
        logger.error("Fatal startup error: %s", type(error).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
