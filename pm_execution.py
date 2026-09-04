"""pm_execution.py — PM execution-intent schemas + extraction (our code).

Extends the framework's ``PortfolioDecision`` with an ``execution`` block so
the Portfolio Manager can express today's open-window orders (sizes, price
limits, stops, partial sells) plus long-term intent that is NOT executed
today (``future_notes`` — carried forward by the dated decision cards).

The subclass swap (daily_run installer) repoints the framework's schema
global to ``ExecutionPortfolioDecision`` before the graph build; the bound
structured-output schema then carries ``execution``. Extraction here
validates the raw tool-call payload against ``ExecutionIntent``:
present-valid / present-invalid (reason) / absent.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from tradingagents.agents.schemas import PortfolioDecision

EXECUTION_VALID = "present_valid"
EXECUTION_INVALID = "present_invalid"
EXECUTION_ABSENT = "absent"


class PmOrderKind(str, Enum):
    BUY = "BUY"    # non-held buy, or explicit add to a held position
    SELL = "SELL"  # held only; shares <= held; never shorts


class PmOrder(BaseModel):
    """One today-open-window order. Exactly one sizing field must be set."""

    kind: PmOrderKind
    # Exactly one of the three sizing fields (validated below).
    value_usd: float | None = None      # "$200" — buy intent value
    shares: int | None = None           # "2 of 8" — sell/buy share count
    fraction_held: float | None = None  # "trim 25%" — sell fraction of held
    limit_px: float | None = None       # SELL: minimum acceptable price (floor).
                                        # BUY: max payable; fills only if the
                                        # auction print is inside the limit.
    stop_px: float | None = None        # protective GTC stop override (replaces
                                        # the -8% default); after a partial sell
                                        # it re-anchors the remainder
    cap_value_usd: float | None = None  # day-clamp on this order ("cap at 5%")
    notes: str | None = None

    @model_validator(mode="after")
    def _check_sizing(self) -> PmOrder:
        present = [f for f in ("value_usd", "shares", "fraction_held")
                   if getattr(self, f) is not None]
        if len(present) != 1:
            raise ValueError(
                f"order must size with exactly one of value_usd/shares/"
                f"fraction_held (got {present or 'none'})")
        if self.value_usd is not None and self.value_usd <= 0:
            raise ValueError("value_usd must be positive")
        if self.shares is not None and self.shares <= 0:
            raise ValueError("shares must be positive")
        if self.fraction_held is not None and not 0 < self.fraction_held <= 1:
            raise ValueError("fraction_held must be in (0, 1]")
        return self


class ExecutionIntent(BaseModel):
    """Today's open-window execution intent. Orders are day-expiry only."""

    orders: list[PmOrder] = Field(default_factory=list)
    # Advisory only — NEVER executed: close/band semantics differ from the
    # broker's GTC touch stops. Recorded on the decision card.
    invalidation_px: float | None = None
    # Long-term intent (tranche 2/3, triggers, pauses, watch levels). NOT
    # executable today — carried forward by the dated decision card, which a
    # future PM reads before re-deciding.
    future_notes: str | None = None

    @model_validator(mode="after")
    def _no_buy_sell_conflict(self) -> ExecutionIntent:
        kinds = {o.kind for o in self.orders}
        if PmOrderKind.BUY in kinds and PmOrderKind.SELL in kinds:
            raise ValueError(
                "block has both BUY and SELL orders for one ticker (conflict)")
        return self


class ExecutionPortfolioDecision(PortfolioDecision):
    """Framework ``PortfolioDecision`` extended with the execution block."""

    execution: ExecutionIntent | None = None


def extract_execution(pm_payload: dict[str, Any]
                      ) -> tuple[str, ExecutionIntent | None, str | None]:
    """Validate a raw PM payload's ``execution`` block.

    Returns ``(EXECUTION_VALID|EXECUTION_INVALID|EXECUTION_ABSENT, intent,
    reason)``. Invalid and absent both yield ``intent=None`` (legacy engine
    path); invalid carries the validation reason for the compliance log.
    """
    block = pm_payload.get("execution") if isinstance(pm_payload, dict) else None
    if block is None:
        return EXECUTION_ABSENT, None, None
    try:
        intent = ExecutionIntent.model_validate(block)
    except ValidationError as exc:
        reason = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                           for e in exc.errors())
        return EXECUTION_INVALID, None, reason
    return EXECUTION_VALID, intent, None
