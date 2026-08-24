"""Deterministic, bounded publication timing for the US audience."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import re
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_WINDOW_PATTERN = re.compile(
    r"^(?P<start_hour>[01][0-9]|2[0-3]):(?P<start_minute>[0-5][0-9])-"
    r"(?P<end_hour>[01][0-9]|2[0-3]):(?P<end_minute>[0-5][0-9])$"
)
_BUCKET_MINUTES = 90


@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time

    @classmethod
    def parse(cls, value: str) -> "TimeWindow":
        match = _WINDOW_PATTERN.fullmatch(value) if type(value) is str else None
        if match is None:
            raise ValueError("time window must use HH:MM-HH:MM")
        start = time(
            int(match["start_hour"]),
            int(match["start_minute"]),
        )
        end = time(
            int(match["end_hour"]),
            int(match["end_minute"]),
        )
        if _minute_of_day(start) >= _minute_of_day(end):
            raise ValueError("time window end must be after start")
        return cls(start=start, end=end)


@dataclass(frozen=True)
class TimingSample:
    scheduled_for: datetime
    measured_at: datetime
    impressions: int
    engagements: int


@dataclass(frozen=True)
class DailyTimingDecision:
    times: tuple[datetime, ...]
    bucket_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class _Bucket:
    identifier: str
    start_minute: int
    end_minute: int


def _minute_of_day(value: time) -> int:
    return value.hour * 60 + value.minute


def _aware(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _digest_integer(*parts: object) -> int:
    payload = ":".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(sha256(payload).digest(), "big")


class AdaptiveTimingPolicy:
    """Choose two restart-stable ET publication times inside safe windows."""

    def __init__(
        self,
        *,
        audience_timezone: str,
        morning_window: str,
        evening_window: str,
        midday_window: str = "13:00-15:30",
        minimum_gap_hours: int,
        timing_min_posts: int,
        weekday_min_posts: int,
    ):
        if type(audience_timezone) is not str or not audience_timezone:
            raise ValueError("audience_timezone must be a non-empty IANA name")
        try:
            self._zone = ZoneInfo(audience_timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("audience_timezone must be a valid IANA name") from error
        if type(minimum_gap_hours) is not int or minimum_gap_hours <= 0:
            raise ValueError("minimum_gap_hours must be a positive integer")
        if type(timing_min_posts) is not int or timing_min_posts <= 0:
            raise ValueError("timing_min_posts must be a positive integer")
        if type(weekday_min_posts) is not int or weekday_min_posts < timing_min_posts:
            raise ValueError("weekday_min_posts must cover timing_min_posts")

        self._morning = TimeWindow.parse(morning_window)
        self._midday = TimeWindow.parse(midday_window)
        self._evening = TimeWindow.parse(evening_window)
        morning_start = _minute_of_day(self._morning.start)
        morning_end = _minute_of_day(self._morning.end)
        midday_start = _minute_of_day(self._midday.start)
        midday_end = _minute_of_day(self._midday.end)
        evening_start = _minute_of_day(self._evening.start)
        evening_end = _minute_of_day(self._evening.end)
        if morning_end > midday_start or midday_end > evening_start:
            raise ValueError("publication windows must not overlap")
        gap_minutes = minimum_gap_hours * 60
        feasible_midday_start = max(midday_start, morning_start + gap_minutes)
        feasible_midday_end = min(midday_end, evening_end - gap_minutes)
        if feasible_midday_start > feasible_midday_end:
            raise ValueError("minimum gap cannot fit inside publication windows")

        self._minimum_gap = timedelta(hours=minimum_gap_hours)
        self._minimum_gap_minutes = minimum_gap_hours * 60
        self._timing_min_posts = timing_min_posts
        self._weekday_min_posts = weekday_min_posts
        self._windows = (self._morning, self._midday, self._evening)
        self._buckets = (
            self._window_buckets("morning", self._morning),
            self._window_buckets("midday", self._midday),
            self._window_buckets("evening", self._evening),
        )

    def choose(
        self,
        local_date: date,
        installation_id: str,
        samples: Iterable[TimingSample],
        *,
        post_count: int = 2,
    ) -> DailyTimingDecision:
        if type(local_date) is not date:
            raise ValueError("local_date must be an exact date")
        if type(installation_id) is not str or not installation_id.strip():
            raise ValueError("installation_id must be a non-empty string")
        if type(post_count) is not int or post_count not in {2, 3}:
            raise ValueError("post_count must be exactly 2 or 3")
        try:
            supplied_samples = tuple(samples)
        except (TypeError, ValueError) as error:
            raise ValueError("samples must be iterable") from error

        mature_samples = self._mature_samples(local_date, supplied_samples)
        selected_indices = (0, 2) if post_count == 2 else (0, 1, 2)
        selected_windows = tuple(self._windows[index] for index in selected_indices)
        selected_bucket_groups = tuple(
            self._buckets[index] for index in selected_indices
        )
        learned_buckets = None
        if len(mature_samples) >= self._timing_min_posts:
            learned_buckets = self._learned_buckets(
                local_date,
                installation_id,
                mature_samples,
                selected_bucket_groups,
            )

        if learned_buckets is None:
            minutes = [
                self._cold_minute(local_date, installation_id, index, window)
                for index, window in enumerate(selected_windows)
            ]
            reason = "cold_start"
        else:
            minutes = [
                self._minute_in_bucket(
                    local_date,
                    installation_id,
                    index,
                    bucket,
                )
                for index, bucket in enumerate(learned_buckets)
            ]
            reason = "performance_weighted"

        minutes = self._enforce_gap(minutes, selected_windows)
        times = tuple(self._local_datetime(local_date, minute) for minute in minutes)
        if any(
            following - prior < self._minimum_gap
            for prior, following in zip(times, times[1:])
        ):
            raise ValueError("adaptive timing decision violates minimum gap")
        bucket_ids = tuple(
            self._bucket_for_minute(buckets, minute).identifier
            for buckets, minute in zip(selected_bucket_groups, minutes)
        )
        return DailyTimingDecision(
            times=times,
            bucket_ids=bucket_ids,
            reason=reason,
        )

    @staticmethod
    def _window_buckets(label: str, window: TimeWindow) -> tuple[_Bucket, ...]:
        start = _minute_of_day(window.start)
        final = _minute_of_day(window.end)
        buckets = []
        cursor = start
        index = 0
        while cursor <= final:
            end = min(final, cursor + _BUCKET_MINUTES - 1)
            buckets.append(_Bucket(f"{label}:{index}", cursor, end))
            cursor = end + 1
            index += 1
        return tuple(buckets)

    def _mature_samples(
        self,
        local_date: date,
        samples: Sequence[object],
    ) -> tuple[TimingSample, ...]:
        plan_start = datetime.combine(local_date, time.min, tzinfo=self._zone)
        plan_start_utc = plan_start.astimezone(timezone.utc)
        valid = []
        for sample in samples:
            if type(sample) is not TimingSample:
                continue
            if not _aware(sample.scheduled_for) or not _aware(sample.measured_at):
                continue
            if type(sample.impressions) is not int or sample.impressions < 0:
                continue
            if type(sample.engagements) is not int or sample.engagements < 0:
                continue
            if sample.engagements > sample.impressions:
                continue
            scheduled_utc = sample.scheduled_for.astimezone(timezone.utc)
            measured_utc = sample.measured_at.astimezone(timezone.utc)
            if measured_utc < scheduled_utc + timedelta(hours=24):
                continue
            if scheduled_utc >= plan_start_utc or measured_utc > plan_start_utc:
                continue
            local_minute = _minute_of_day(sample.scheduled_for.astimezone(self._zone).timetz())
            if not any(
                bucket.start_minute <= local_minute <= bucket.end_minute
                for window_buckets in self._buckets
                for bucket in window_buckets
            ):
                continue
            valid.append(sample)
        return tuple(valid)

    def _learned_buckets(
        self,
        local_date: date,
        installation_id: str,
        samples: Sequence[TimingSample],
        selected_bucket_groups: Sequence[Sequence[_Bucket]],
    ) -> tuple[_Bucket, ...] | None:
        choices = []
        for position, buckets in enumerate(selected_bucket_groups):
            statistics = []
            for bucket in buckets:
                matching = []
                for sample in samples:
                    local_sample = sample.scheduled_for.astimezone(self._zone)
                    minute = _minute_of_day(local_sample.timetz())
                    if bucket.start_minute <= minute <= bucket.end_minute:
                        matching.append(sample)
                if len(matching) < 3:
                    continue
                if len(samples) >= self._weekday_min_posts:
                    weekday_matching = [
                        sample
                        for sample in matching
                        if sample.scheduled_for.astimezone(self._zone).weekday()
                        == local_date.weekday()
                    ]
                    if len(weekday_matching) >= 3:
                        matching = weekday_matching
                impressions = sum(sample.impressions for sample in matching)
                engagements = sum(sample.engagements for sample in matching)
                statistics.append((bucket, (engagements + 1) / (impressions + 100)))
            if not statistics:
                return None
            choices.append(
                self._weighted_bucket(
                    local_date,
                    installation_id,
                    position,
                    statistics,
                )
            )
        return tuple(choices)

    @staticmethod
    def _weighted_bucket(
        local_date: date,
        installation_id: str,
        position: int,
        statistics: Sequence[tuple[_Bucket, float]],
    ) -> _Bucket:
        exploration = _digest_integer(
            installation_id,
            local_date.isoformat(),
            position,
            "explore",
        ) % 100 < 20
        if exploration:
            index = _digest_integer(
                installation_id,
                local_date.isoformat(),
                position,
                "exploration-choice",
            ) % len(statistics)
            return statistics[index][0]

        total = sum(score for _, score in statistics)
        if total <= 0:
            return statistics[0][0]
        fraction = (
            _digest_integer(
                installation_id,
                local_date.isoformat(),
                position,
                "weighted-choice",
            )
            % 1_000_000
        ) / 1_000_000
        target = fraction * total
        cumulative = 0.0
        for bucket, score in statistics:
            cumulative += score
            if target < cumulative:
                return bucket
        return statistics[-1][0]

    @staticmethod
    def _cold_minute(
        local_date: date,
        installation_id: str,
        position: int,
        window: TimeWindow,
    ) -> int:
        start = _minute_of_day(window.start)
        end = _minute_of_day(window.end)
        return start + (
            _digest_integer(installation_id, local_date.isoformat(), position)
            % (end - start + 1)
        )

    @staticmethod
    def _minute_in_bucket(
        local_date: date,
        installation_id: str,
        position: int,
        bucket: _Bucket,
    ) -> int:
        return bucket.start_minute + (
            _digest_integer(
                installation_id,
                local_date.isoformat(),
                position,
                bucket.identifier,
                "minute",
            )
            % (bucket.end_minute - bucket.start_minute + 1)
        )

    def _enforce_gap(
        self,
        minutes: list[int],
        windows: Sequence[TimeWindow],
    ) -> list[int]:
        if len(minutes) != len(windows) or len(minutes) not in {2, 3}:
            raise ValueError("invalid publication timing shape")
        selected = [
            min(
                _minute_of_day(window.end),
                max(_minute_of_day(window.start), minute),
            )
            for minute, window in zip(minutes, windows)
        ]
        if all(
            following - prior >= self._minimum_gap_minutes
            for prior, following in zip(selected, selected[1:])
        ):
            return selected

        earliest = [_minute_of_day(windows[0].start)]
        for window in windows[1:]:
            earliest.append(max(
                _minute_of_day(window.start),
                earliest[-1] + self._minimum_gap_minutes,
            ))
        if any(
            minute > _minute_of_day(window.end)
            for minute, window in zip(earliest, windows)
        ):
            raise ValueError("minimum gap cannot fit inside publication windows")
        return earliest

    @staticmethod
    def _bucket_for_minute(buckets: Sequence[_Bucket], minute: int) -> _Bucket:
        for bucket in buckets:
            if bucket.start_minute <= minute <= bucket.end_minute:
                return bucket
        raise ValueError("selected minute is outside its approved window")

    def _local_datetime(self, local_date: date, minute: int) -> datetime:
        return datetime.combine(
            local_date,
            time(minute // 60, minute % 60),
            tzinfo=self._zone,
        )
