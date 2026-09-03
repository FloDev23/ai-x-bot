"""Configurable X API usage reservations and cost estimates."""

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional


_OPERATION = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class XApiUsageClaim:
    request_token: str
    operation: str
    max_units: int


class XApiBudgetExceeded(RuntimeError):
    """Raised before a network call that would exceed the configured cap."""


class XApiUsageMeter:
    """Reserve estimated spend before X calls and settle it afterward."""

    def __init__(
        self,
        database,
        *,
        monthly_budget_microusd: int = 0,
        unit_costs_microusd: Optional[Mapping[str, int]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        costs = dict(unit_costs_microusd or {})
        if (
            type(monthly_budget_microusd) is not int
            or monthly_budget_microusd < 0
            or monthly_budget_microusd > 1_000_000_000_000_000
            or any(
                type(operation) is not str
                or _OPERATION.fullmatch(operation) is None
                or type(cost) is not int
                or not 0 <= cost <= 1_000_000_000
                for operation, cost in costs.items()
            )
        ):
            raise ValueError("invalid X API usage configuration")
        self.database = database
        self.monthly_budget_microusd = monthly_budget_microusd
        self.unit_costs_microusd = costs
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def reserve(
        self,
        operation: str,
        max_units: int,
        now: Optional[datetime] = None,
    ) -> Optional[XApiUsageClaim]:
        if (
            type(operation) is not str
            or _OPERATION.fullmatch(operation) is None
            or type(max_units) is not int
            or not 1 <= max_units <= 1_000_000_000
        ):
            return None
        current = self.clock() if now is None else now
        if (
            type(current) is not datetime
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            return None
        request_token = secrets.token_urlsafe(24)
        unit_cost = self.unit_costs_microusd.get(operation, 0)
        reserved = self.database.reserve_x_api_usage(
            request_token=request_token,
            operation=operation,
            max_units=max_units,
            unit_cost_microusd=unit_cost,
            monthly_budget_microusd=self.monthly_budget_microusd,
            occurred_at=current,
        )
        if reserved is not True:
            return None
        return XApiUsageClaim(request_token, operation, max_units)

    def complete(self, claim: XApiUsageClaim, actual_units: int) -> bool:
        if not isinstance(claim, XApiUsageClaim):
            return False
        return self.database.settle_x_api_usage(
            claim.request_token, "completed", actual_units=actual_units,
        )

    def fail(self, claim: XApiUsageClaim) -> bool:
        if not isinstance(claim, XApiUsageClaim):
            return False
        return self.database.settle_x_api_usage(claim.request_token, "failed")

    def unknown(self, claim: XApiUsageClaim) -> bool:
        if not isinstance(claim, XApiUsageClaim):
            return False
        return self.database.settle_x_api_usage(claim.request_token, "unknown")
