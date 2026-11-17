"""Immutable domain and result models.

The public result is a plain dict (see :mod:`grid_flex_settlement.report`), but
the calculation is expressed with frozen dataclasses so that no stage can
mutate an input version after it has been fingerprinted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from .timeutil import Interval


@dataclass(frozen=True)
class PipelineResult:
    project: str
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    event_id: str
    version: int
    window: Interval
    timezone: str
    direction: str  # "reduce" | "increase"
    status: str = "active"
    capacity_kw: Optional[Decimal] = None
    issued_at: Optional[datetime] = None
    baseline_window: Optional[Interval] = None
    settlement_window: Optional[Interval] = None
    baseline_lookback_minutes: Optional[int] = None
    baseline_method: str = "mean"  # mean | latest
    rate_version_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class EligibilityWindow:
    eligibility_id: str
    resource_id: str
    window: Interval
    version: int = 1
    status: str = "active"
    event_id: Optional[str] = None
    issued_at: Optional[datetime] = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Sample:
    """A constant quantity over a half-open interval."""

    interval: Interval
    kw: Decimal
    sample_id: Optional[str] = None
    received_at: Optional[datetime] = None
    version: int = 1

    @property
    def start(self) -> datetime:
        return self.interval.start

    @property
    def end(self) -> datetime:
        return self.interval.end


@dataclass(frozen=True)
class Reading:
    reading_id: str
    resource_id: str
    sample: Sample


@dataclass(frozen=True)
class BaselineVersion:
    baseline_id: str
    resource_id: str
    version: int
    method: str
    status: str
    issued_at: datetime
    effective_at: Optional[datetime]
    samples: tuple[Sample, ...]
    event_id: Optional[str] = None
    supersedes: Optional[tuple[str, int]] = None  # (baseline_id, version)
    window: Optional[Interval] = None
    representation: str = "profile"  # profile | scalar
    level_kw: Optional[Decimal] = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class DispatchInstruction:
    instruction_id: str
    event_id: str
    resource_id: str
    version: int
    issued_at: datetime
    window: Interval
    direction: str
    capacity_kw: Decimal
    status: str = "active"  # active | cancelled | revoked
    supersedes: Optional[tuple[str, int]] = None


@dataclass(frozen=True)
class ExecutionSample:
    sample_id: str
    resource_id: str
    interval: Interval
    kw: Decimal
    instruction_id: Optional[str] = None
    received_at: Optional[datetime] = None
    version: int = 1


@dataclass(frozen=True)
class PerformanceBand:
    min_ratio: Decimal
    factor: Decimal
    max_ratio: Optional[Decimal] = None
    label: Optional[str] = None


@dataclass(frozen=True)
class RateVersion:
    rate_version_id: str
    tariff_id: str
    version: int
    window: Interval
    issued_at: datetime
    status: str
    capacity_rate_per_kw_hour: Decimal
    energy_rate_per_kwh: Decimal
    deviation_rate_per_kwh: Decimal
    energy_rate_basis: str = "kwh"  # kwh | kw (demand-style, no time integration)
    currency: str = "USD"
    direction: str = "both"  # reduce | increase | both
    capacity_rate_basis: str = "kw_hour"  # kw_hour | kw_event
    capacity_rate_per_kw_event: Optional[Decimal] = None
    supersedes: Optional[tuple[str, int]] = None
    priority: int = 0
    performance_bands: tuple[PerformanceBand, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Correction:
    correction_id: str
    event_id: str
    version: int
    issued_at: datetime
    status: str = "active"  # active | withdrawn
    supersedes: Optional[tuple[str, int]] = None
    changes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriorBill:
    bill_id: str
    event_id: str
    resource_id: str
    revision: int
    status: str  # locked | issued | draft | superseded
    amount_cents: int
    currency: str = "USD"
    fingerprint: Optional[str] = None


@dataclass(frozen=True)
class SettlementRules:
    rule_version: str = "grid-flex-settlement.rules/2.0"
    baseline_policy: str = "hybrid"  # materialized | dynamic | hybrid
    honor_baseline_revisions: bool = True
    min_baseline_coverage: Decimal = Decimal("0.95")
    coverage_policy: str = "prorate"  # prorate | strict
    baseline_policy_on_gap: str = "strict"  # strict | prorate
    deviation_basis: str = "execution"  # execution | instruction
    rounding: str = "half_even"
    split_at_local_midnight: bool = True
    allow_stacking_instructions: bool = False
    performance_bands: tuple[PerformanceBand, ...] = ()
    shortfall_penalty: bool = True
    credit_overdelivery: bool = False
    late_reading_policy: str = "exclude"  # exclude | include
    rate_conflict_policy: str = "error"  # error | priority
    max_settlement_rounds: int = 2
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettlementRequest:
    events: tuple[Event, ...]
    eligibility: tuple[EligibilityWindow, ...]
    baselines: tuple[BaselineVersion, ...]
    instructions: tuple[DispatchInstruction, ...]
    executions: tuple[ExecutionSample, ...]
    readings: tuple[Reading, ...]
    rates: tuple[RateVersion, ...]
    corrections: tuple[Correction, ...] = ()
    prior_bills: tuple[PriorBill, ...] = ()
    resources: tuple[dict[str, Any], ...] = ()
    rules: SettlementRules = SettlementRules()
    as_of: Optional[datetime] = None
    payload_fingerprint: str = ""
    warnings: tuple[dict[str, Any], ...] = ()
    parse_issues: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineItem:
    line_id: str
    component: str
    interval: Interval
    quantity_kwh: Decimal
    unit_rate: Decimal
    amount_exact: Decimal
    amount_cents: int
    allocation_residual: str
    direction: str
    capacity_kw: Decimal
    baseline_kw: Decimal
    actual_kw: Decimal
    delivered_kw: Decimal
    executed_kw: Optional[Decimal]
    rate_version: str
    baseline_version: Optional[str]
    instruction_version: Optional[str]
    rule_version: str
    source_reading_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.component}:{self.line_id}"

    @property
    def interval_key(self) -> str:
        return f"{self.interval.start.isoformat()}/{self.interval.end.isoformat()}"


@dataclass(frozen=True)
class ComponentResult:
    name: str
    amount_exact: Decimal
    amount_cents: int
    lines: tuple[LineItem, ...]
    explanation: str = ""


@dataclass(frozen=True)
class UnitResult:
    key: str
    event_id: str
    resource_id: str
    status: str  # settled | revoked | zero | error
    currency: str
    settlement_window: Optional[Interval]
    event_window: Optional[Interval]
    eligibility_window: Optional[Interval]
    baseline: dict[str, Any]
    rate: dict[str, Any]
    instruction: dict[str, Any]
    metrics: dict[str, Any]
    components: tuple[ComponentResult, ...]
    final_cents: int = 0
    errors: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    audit: dict[str, Any] = field(default_factory=dict)
    bill: Optional[dict[str, Any]] = None
    revision: Optional[dict[str, Any]] = None

    @property
    def final_amount(self) -> str:
        from .decimalutil import format_money

        return format_money(self.final_cents)


@dataclass(frozen=True)
class BatchResult:
    status: str
    units: tuple[UnitResult, ...]
    errors: tuple[dict[str, Any], ...]
    totals: dict[str, Any]
    audit: dict[str, Any]
    design_comparison: Optional[dict[str, Any]] = None
