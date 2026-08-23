"""Deterministic, source-backed editorial planning."""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config import BOT_TIMEZONE


PORTFOLIO = {
    "gym_strategy": 0.35,
    "fitness_business_insight": 0.25,
    "shareable_fitness": 0.20,
    "product_proof": 0.10,
    "founder_journey": 0.10,
}

SOURCE_TYPES = {
    "gym_strategy": {"evergreen_idea", "verified_news", "founder_note"},
    "fitness_business_insight": {"verified_news"},
    "shareable_fitness": {"evergreen_idea", "verified_news"},
    "product_proof": {"product_fact"},
    "founder_journey": {"founder_note"},
}

SOURCE_TYPE_PRIORITY = {
    "gym_strategy": ("verified_news", "evergreen_idea", "founder_note"),
    "fitness_business_insight": ("verified_news",),
    "shareable_fitness": ("verified_news", "evergreen_idea"),
    "product_proof": ("product_fact",),
    "founder_journey": ("founder_note",),
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

    def __init__(self, database, timezone_name: str = BOT_TIMEZONE):
        self.database = database
        self.timezone_name = timezone_name

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
        sources_by_category = self._eligible_sources_by_category(sources)
        eligible_categories = [
            category for category in PORTFOLIO if sources_by_category[category]
        ]
        if not eligible_categories:
            return None

        category = max(
            eligible_categories,
            key=lambda candidate: _portfolio_deficit(candidate, counts),
        )
        include_link = (
            category == "product_proof"
            and self.database.count_links_last_days(7) < 1
        )
        return ContentPlan(
            category=category,
            source_ids=[source["id"] for source in sources_by_category[category]],
            intended_slot=intended_slot,
            include_link=include_link,
        )

    def _eligible_sources_by_category(
        self,
        sources: Optional[List[Dict]] = None,
    ) -> Dict[str, List[Dict]]:
        sources = self.database.get_eligible_sources() if sources is None else sources
        return {
            category: self._primary_source_for_category(category, sources)
            for category in SOURCE_TYPES
        }

    @staticmethod
    def _primary_source_for_category(
        category: str,
        sources: List[Dict],
    ) -> List[Dict]:
        for source_type in SOURCE_TYPE_PRIORITY[category]:
            for source in sources:
                if source.get("source_type") == source_type:
                    return [source]
        return []
