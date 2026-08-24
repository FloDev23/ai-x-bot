"""Deterministic two-or-three post cadence for the US audience."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from modules.adaptive_timing import TimingSample


@dataclass(frozen=True)
class CadenceDecision:
    post_count: int
    third_post_weekdays: tuple[int, int, int]
    reason: str


class PublicationCadencePolicy:
    """Choose exactly three third-post weekdays without inspecting post copy."""

    _COLD_THIRD_DAYS = (1, 3, 5)  # Tuesday, Thursday, Saturday.

    def __init__(
        self,
        *,
        audience_timezone: str,
        third_days_per_week: int,
        learning_min_posts: int,
    ):
        if type(audience_timezone) is not str or not audience_timezone:
            raise ValueError("audience_timezone must be a non-empty IANA name")
        try:
            self._zone = ZoneInfo(audience_timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("audience_timezone must be a valid IANA name") from error
        if type(third_days_per_week) is not int or third_days_per_week != 3:
            raise ValueError("third_days_per_week must be exactly 3")
        if type(learning_min_posts) is not int or learning_min_posts <= 0:
            raise ValueError("learning_min_posts must be a positive integer")
        self.learning_min_posts = learning_min_posts

    def choose(
        self,
        local_date: date,
        samples: Iterable[TimingSample],
    ) -> CadenceDecision:
        if type(local_date) is not date:
            raise ValueError("local_date must be an exact date")
        try:
            supplied = tuple(samples)
        except (TypeError, ValueError) as error:
            raise ValueError("samples must be iterable") from error

        mature = self._mature_samples(local_date, supplied)
        if len(mature) < self.learning_min_posts:
            selected = self._COLD_THIRD_DAYS
            reason = "cold_start"
        else:
            selected = self._rank_three_weekdays(mature)
            reason = "performance_weighted"
        return CadenceDecision(
            post_count=3 if local_date.weekday() in selected else 2,
            third_post_weekdays=selected,
            reason=reason,
        )

    def _mature_samples(self, local_date, samples):
        plan_start = datetime.combine(local_date, time.min, tzinfo=self._zone)
        plan_start_utc = plan_start.astimezone(timezone.utc)
        valid = []
        for sample in samples:
            if type(sample) is not TimingSample:
                continue
            scheduled = sample.scheduled_for
            measured = sample.measured_at
            if (
                type(scheduled) is not datetime
                or scheduled.tzinfo is None
                or scheduled.utcoffset() is None
                or type(measured) is not datetime
                or measured.tzinfo is None
                or measured.utcoffset() is None
                or type(sample.impressions) is not int
                or sample.impressions < 0
                or type(sample.engagements) is not int
                or sample.engagements < 0
                or sample.engagements > sample.impressions
            ):
                continue
            scheduled_utc = scheduled.astimezone(timezone.utc)
            measured_utc = measured.astimezone(timezone.utc)
            if measured_utc < scheduled_utc + timedelta(hours=24):
                continue
            if scheduled_utc >= plan_start_utc or measured_utc > plan_start_utc:
                continue
            valid.append(sample)
        return tuple(valid)

    def _rank_three_weekdays(
        self,
        samples: tuple[TimingSample, ...],
    ) -> tuple[int, int, int]:
        ranked = []
        for weekday in range(7):
            matching = [
                sample
                for sample in samples
                if sample.scheduled_for.astimezone(self._zone).weekday() == weekday
            ]
            impressions = sum(sample.impressions for sample in matching)
            engagements = sum(sample.engagements for sample in matching)
            prior_engagements = 5 if weekday in self._COLD_THIRD_DAYS else 4
            score = (engagements + prior_engagements) / (impressions + 100)
            ranked.append((score, weekday))
        selected = sorted(
            weekday
            for _score, weekday in sorted(
                ranked,
                key=lambda item: (-item[0], item[1]),
            )[:3]
        )
        return selected[0], selected[1], selected[2]
