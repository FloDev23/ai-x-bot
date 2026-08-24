"""Deterministic, source-backed editorial planning."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config import BOT_TIMEZONE, MAX_LINKS_PER_WEEK


PORTFOLIO = {
    "gym_strategy": 0.35,
    "fitness_business_insight": 0.25,
    "shareable_fitness": 0.20,
    "product_proof": 0.10,
    "founder_journey": 0.10,
}

SOURCE_TYPES = {
    "gym_strategy": {
        "evergreen_idea",
        "verified_news",
        "founder_note",
        "owned_blog_article",
    },
    "fitness_business_insight": {"verified_news", "owned_blog_article"},
    "shareable_fitness": {
        "evergreen_idea",
        "verified_news",
        "owned_blog_article",
    },
    "product_proof": {"product_fact"},
    "founder_journey": {"founder_note"},
}

@dataclass(frozen=True)
class ContentPlan:
    category: str
    source_ids: List[int]
    intended_slot: datetime
    include_link: bool


def _portfolio_deficit(category: str, counts: Dict[str, int]) -> float:
    next_total = sum(counts.values()) + 1
    return PORTFOLIO[category] * next_total - counts.get(category, 0)


def choose_portfolio_category(counts: Dict[str, int]) -> str:
    """Return the category with the largest 30-day portfolio deficit."""
    return max(PORTFOLIO, key=lambda category: _portfolio_deficit(category, counts))


class ContentPlanner:
    """Select one source-backed candidate without creating a draft."""

    def __init__(
        self,
        database,
        timezone_name: str = BOT_TIMEZONE,
        max_links_per_week: int = MAX_LINKS_PER_WEEK,
    ):
        if type(max_links_per_week) is not int or max_links_per_week < 0:
            raise ValueError("max_links_per_week must be a non-negative integer")
        self.database = database
        self.timezone_name = timezone_name
        self.max_links_per_week = max_links_per_week

    def plan(self, intended_slot: datetime) -> Optional[ContentPlan]:
        """Plan a candidate if the day cap and the source gate both permit it."""
        local_timezone = ZoneInfo(self.timezone_name)
        local_slot = (
            intended_slot.replace(tzinfo=local_timezone)
            if intended_slot.tzinfo is None
            else intended_slot.astimezone(local_timezone)
        )
        if self.database.count_drafts_for_local_date(
            local_slot.date(),
            self.timezone_name,
        ) >= 2:
            return None

        counts = self.database.get_content_mix_counts(days=30)
        sources = self.database.get_eligible_sources()
        if (
            not isinstance(sources, list)
            or any(
                not isinstance(source, dict)
                or type(source.get("id")) is not int
                or source["id"] <= 0
                or (
                    "metadata" in source
                    and not isinstance(source.get("metadata"), dict)
                )
                for source in sources
            )
            or len({source["id"] for source in sources}) != len(sources)
        ):
            return None
        source_ids = [source["id"] for source in sources]
        usage = self.database.get_content_source_usage(
            source_ids,
            now=local_slot,
        )
        if not self._valid_usage(usage, source_ids, local_slot):
            return None
        sources_by_category = self._eligible_sources_by_category(
            sources,
            usage=usage,
            now=local_slot,
        )
        eligible_categories = [
            category for category in PORTFOLIO if sources_by_category[category]
        ]
        if not eligible_categories:
            return None

        category = max(
            eligible_categories,
            key=lambda candidate: _portfolio_deficit(candidate, counts),
        )
        selected_source = sources_by_category[category][0]
        include_link = False
        if selected_source["source_type"] in {
            "product_fact",
            "owned_blog_article",
        }:
            include_link = self.database.count_links_last_days(
                7,
                now=local_slot,
            ) < self.max_links_per_week
        if include_link and selected_source["source_type"] == "owned_blog_article":
            last_linked = usage[selected_source["id"]]["last_linked_at"]
            if last_linked is not None:
                linked_at = self._parse_timestamp(last_linked)
                include_link = linked_at <= (
                    local_slot.astimezone(timezone.utc) - timedelta(days=30)
                )
        return ContentPlan(
            category=category,
            source_ids=[selected_source["id"]],
            intended_slot=intended_slot,
            include_link=include_link,
        )

    def _eligible_sources_by_category(
        self,
        sources: Optional[List[Dict]] = None,
        *,
        usage=None,
        now=None,
    ) -> Dict[str, List[Dict]]:
        sources = self.database.get_eligible_sources() if sources is None else sources
        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        effective_now = effective_now.astimezone(timezone.utc)
        if usage is None:
            usage = {
                source["id"]: {
                    "bound_to_live_draft": False,
                    "last_published_at": None,
                    "last_linked_at": None,
                }
                for source in sources
            }
        return {
            category: self._primary_source_for_category(
                category,
                sources,
                usage,
                effective_now,
            )
            for category in SOURCE_TYPES
        }

    @classmethod
    def _primary_source_for_category(
        cls,
        category: str,
        sources: List[Dict],
        usage,
        now,
    ) -> List[Dict]:
        candidates = [
            source
            for source in sources
            if source.get("source_type") in SOURCE_TYPES[category]
            and not usage[source["id"]]["bound_to_live_draft"]
        ]
        candidates.sort(
            key=lambda source: cls._rotation_key(
                source,
                usage[source["id"]],
                now,
            ),
        )
        return candidates[:1]

    @staticmethod
    def _parse_timestamp(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _valid_usage(cls, usage, source_ids, now):
        if not isinstance(usage, dict) or set(usage) != set(source_ids):
            return False
        current = now.astimezone(timezone.utc)
        for source_id in source_ids:
            item = usage.get(source_id)
            if not isinstance(item, dict):
                return False
            if type(item.get("bound_to_live_draft")) is not bool:
                return False
            for field in ("last_published_at", "last_linked_at"):
                value = item.get(field)
                if value is None:
                    continue
                if not isinstance(value, str) or not value.strip():
                    return False
                try:
                    parsed = cls._parse_timestamp(value)
                except (TypeError, ValueError):
                    return False
                if parsed > current:
                    return False
        return True

    @classmethod
    def _rotation_key(cls, source, usage, now):
        last_published = usage["last_published_at"]
        if last_published is None:
            bucket = 0
            last_used = float("-inf")
        else:
            parsed = cls._parse_timestamp(last_published)
            age = now - parsed
            bucket = 1 if age >= timedelta(days=30) else 2
            last_used = parsed.timestamp()

        published_timestamp = float("-inf")
        raw_published = source.get("metadata", {}).get("published_at")
        if isinstance(raw_published, str):
            try:
                if len(raw_published) == 10:
                    parsed_date = date.fromisoformat(raw_published)
                    parsed_published = datetime.combine(
                        parsed_date,
                        time.min,
                        tzinfo=timezone.utc,
                    )
                else:
                    parsed_published = cls._parse_timestamp(raw_published)
                published_timestamp = parsed_published.timestamp()
            except (TypeError, ValueError):
                pass
        return (
            bucket,
            last_used,
            -published_timestamp,
            -source["id"],
        )
