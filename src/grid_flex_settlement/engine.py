"""Event-level settlement calculation.

Pipeline inside :func:`settle_unit`:

1. **resolve** event version and corrections (``resolution``);
2. **model windows** — eligibility, event, settlement and baseline are distinct
   intervals and are intersected explicitly;
3. **select versions** — baseline, dispatch instruction, rate version;
4. **segment** the settlement basis at every interval boundary that can change
   an amount (instruction, rate, eligibility, DST local midnight, meter sample);
5. **calculate components** — capacity, performance, deviation — in exact
   decimal arithmetic;
6. **round once per component to cents** and allocate those cents to lines with
   largest remainder, so the lines sum exactly to the component;
7. **attach provenance** — every line names its interval, rule version, rate
   version, baseline version, instruction version and contributing readings.

A unit (one event × one resource) is the isolation boundary.  A failure in one
unit produces an ``error`` unit result and does not change any other unit's
amount or determinism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence

from .canon import fingerprint
from .decimalutil import (
    ZERO,
    allocate_minor,
    allocation_residuals,
    dec,
    format_decimal,
    format_money,
    from_minor,
    sum_decimals,
    to_minor,
)
from .models import (
    BatchResult,
    ComponentResult,
    Event,
    ExecutionSample,
    LineItem,
    PriorBill,
    RateVersion,
    Reading,
    SettlementRequest,
    SettlementRules,
    UnitResult,
    PerformanceBand,
)
from .resolution import (
    BaselineSelection,
    InstructionSlot,
    RateSegment,
    RecordSelection,
    ResolvedEvent,
    resolve_baseline,
    resolve_eligibility,
    resolve_events,
    resolve_executions,
    resolve_instructions,
    resolve_prior_bill,
    resolve_rates,
    resolve_readings,
    resolve_units,
)
from .timeutil import (
    Interval,
    format_instant,
    merge_intervals,
    split_in_local_midnight,
    weighted_average,
)

RULE_VERSION = "grid-flex-settlement.rules/2.0"
COMPONENT_ORDER = ("capacity", "performance", "deviation")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _UnitErrors:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def error(self, code: str, message: str, **extra: Any) -> None:
        self.errors.append({"code": code, "message": message, "severity": "error", **extra})

    def warn(self, code: str, message: str, **extra: Any) -> None:
        self.warnings.append({"code": code, "message": message, "severity": "warning", **extra})

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True)
class _Segment:
    interval: Interval
    slot: InstructionSlot
    rate: RateSegment
    baseline_kw: Optional[Decimal]
    actual_kw: Optional[Decimal]
    execution_kw: Optional[Decimal]
    baseline_coverage: Decimal
    actual_coverage: Decimal
    baseline_source_ids: tuple[str, ...]
    actual_source_ids: tuple[str, ...]
    capacity_override: Optional[Any] = None
    flags: tuple[str, ...] = ()

    @property
    def hours(self) -> Decimal:
        return Decimal(self.interval.duration_seconds) / Decimal(3600)


def _dt_key(value: Optional[datetime]) -> float:
    return value.timestamp() if value is not None else float("-inf")


def _round_cents(value: Decimal) -> int:
    return to_minor(value)


def _component_lines(
    *,
    component: str,
    unit_key: str,
    segments: Sequence[_Segment],
    exact_amounts: Sequence[Decimal],
    quantities: Sequence[Decimal],
    unit_rates: Sequence[Decimal],
    baseline_version: Optional[str],
    rule_version: str,
    extra_flags: Sequence[tuple[str, ...]] | None = None,
) -> ComponentResult:
    """Round a component once, then allocate the cents back to its intervals."""

    total_exact = sum_decimals(exact_amounts)
    total_cents = _round_cents(total_exact)
    keys = [f"{segment.interval.start.isoformat()}/{segment.interval.end.isoformat()}" for segment in segments]
    allocation = allocate_minor(total_cents, list(exact_amounts), keys) if segments else []
    residuals = allocation_residuals(exact_amounts, allocation)

    lines: list[LineItem] = []
    for index, segment in enumerate(segments):
        instruction = segment.slot.instruction
        flags = tuple(sorted(set(segment.flags).union(extra_flags[index] if extra_flags else ())))
        lines.append(
            LineItem(
                line_id=f"{unit_key}:{component}:{index:04d}",
                component=component,
                interval=segment.interval,
                quantity_kwh=quantities[index],
                unit_rate=unit_rates[index],
                amount_exact=exact_amounts[index],
                amount_cents=allocation[index],
                allocation_residual=residuals[index],
                direction=instruction.direction,
                capacity_kw=segment.slot.capacity_kw,
                baseline_kw=segment.baseline_kw if segment.baseline_kw is not None else ZERO,
                actual_kw=segment.actual_kw if segment.actual_kw is not None else ZERO,
                delivered_kw=(segment.baseline_kw or ZERO) - (segment.actual_kw or ZERO),
                executed_kw=segment.execution_kw,
                rate_version=segment.rate.label,
                baseline_version=baseline_version,
                instruction_version=f"{instruction.instruction_id}@v{instruction.version}",
                rule_version=rule_version,
                source_reading_ids=segment.actual_source_ids + segment.baseline_source_ids,
                flags=flags,
            )
        )
    return ComponentResult(
        name=component,
        amount_exact=total_exact,
        amount_cents=total_cents,
        lines=tuple(lines),
        explanation=(
            f"{component}: {len(lines)} interval line(s), exact total "
            f"{format_decimal(total_exact, places=8)} -> {format_money(total_cents)}"
        ),
    )


def _pick_performance_factor(
    rules: SettlementRules, rate: RateVersion, ratio: Optional[Decimal]
) -> tuple[Decimal, Optional[str]]:
    bands: Sequence[PerformanceBand] = rate.performance_bands or rules.performance_bands
    if not bands:
        return Decimal(1), None
    if ratio is None:
        return Decimal(0), "no_delivery"
    ordered = sorted(bands, key=lambda band: (band.min_ratio, band.max_ratio or Decimal("Infinity")))
    for band in ordered:
        if ratio >= band.min_ratio and (band.max_ratio is None or ratio <= band.max_ratio):
            return band.factor, band.label or f"[{band.min_ratio},{band.max_ratio or 'inf'}]"
    return Decimal(0), "below_minimum_performance"


def _rate_value_for_capacity(rate: RateVersion) -> tuple[Decimal, str]:
    if rate.capacity_rate_basis == "kw_event":
        return rate.capacity_rate_per_kw_event or rate.capacity_rate_per_kw_hour, "kw_event"
    return rate.capacity_rate_per_kw_hour, "kw_hour"


def _rate_currency(segments: Sequence[RateSegment]) -> str:
    currencies = {segment.rate.currency for segment in segments}
    if len(currencies) > 1:
        raise ValueError(f"mixed currencies in one settlement unit: {sorted(currencies)}")
    return next(iter(currencies)) if currencies else "USD"


def _boundaries(
    basis: Sequence[Interval],
    slots: Sequence[InstructionSlot],
    rates: Sequence[RateSegment],
    baseline: BaselineSelection,
    actual: RecordSelection,
    timezone_name: str,
    split_midnight: bool,
) -> list[datetime]:
    points: set[datetime] = set()
    for interval in basis:
        points.add(interval.start)
        points.add(interval.end)
    for slot in slots:
        points.add(slot.interval.start)
        points.add(slot.interval.end)
    for segment in rates:
        points.add(segment.interval.start)
        points.add(segment.interval.end)
    for sample in baseline.samples:
        points.add(sample.interval.start)
        points.add(sample.interval.end)
    for sample in actual.samples:
        points.add(sample.interval.start)
        points.add(sample.interval.end)
    ordered = sorted(points)
    if split_midnight:
        extra: set[datetime] = set()
        for start, end in zip(ordered, ordered[1:]):
            for piece in split_in_local_midnight(Interval(start, end), timezone_name):
                extra.add(piece.start)
                extra.add(piece.end)
        ordered = sorted(set(ordered) | extra)
    return ordered


def _slice(interval: Interval, points: Sequence[datetime]) -> list[Interval]:
    inside = [point for point in points if interval.start < point < interval.end]
    edges = [interval.start, *inside, interval.end]
    return [Interval(edges[index], edges[index + 1]) for index in range(len(edges) - 1) if edges[index] < edges[index + 1]]


def _covering_slot(segment: Interval, slots: Sequence[InstructionSlot]) -> Optional[InstructionSlot]:
    for slot in slots:
        if slot.interval.contains(segment):
            return slot
    return None


def _covering_rate(segment: Interval, rates: Sequence[RateSegment]) -> Optional[RateSegment]:
    for rate in rates:
        if rate.interval.contains(segment):
            return rate
    return None


def _sample_coverage(samples: Sequence, segment: Interval) -> tuple[Decimal, tuple[str, ...]]:
    covered = 0
    ids: list[str] = []
    for sample in samples:
        clipped = sample.interval.clip_to(segment)
        if clipped is None:
            continue
        covered += clipped.duration_seconds
        if sample.sample_id:
            ids.append(str(sample.sample_id))
    total = segment.duration_seconds
    if total <= 0:
        return Decimal(1), tuple(sorted(set(ids)))
    return Decimal(min(covered, total)) / Decimal(total), tuple(sorted(set(ids)))


def _weighted(samples: Sequence, segment: Interval) -> Decimal:
    return weighted_average([(sample.interval, sample.kw) for sample in samples], segment)


def _baseline_coverage(
    baseline: BaselineSelection, segment: Interval, value: Optional[Decimal]
) -> tuple[Decimal, tuple[str, ...]]:
    """Coverage of the *baseline reference*, not of the settlement interval.

    A scalar baseline covers every interval it applies to.  A profile baseline
    only covers intervals for which it has aligned samples.
    """

    if baseline.representation == "scalar":
        return (Decimal(1) if value is not None else Decimal(0)), tuple(
            sorted({str(sample.sample_id) for sample in baseline.samples if sample.sample_id})
        )
    if value is None:
        return Decimal(0), ()
    return _sample_coverage(baseline.samples, segment)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def settle(request: SettlementRequest, *, resume_units: Optional[dict[str, dict]] = None) -> BatchResult:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = list(request.warnings)
    resolved = resolve_events(request, issues, warnings)

    units = resolve_units(request, issues, warnings)
    unit_results: list[UnitResult] = []
    for event_id, resource_id in units:
        key = f"{event_id}/{resource_id}"
        try:
            result = settle_unit(
                request,
                resolved,
                event_id,
                resource_id,
                batch_warnings=warnings,
                resume_units=resume_units,
            )
        except Exception as exc:  # never let one unit abort the batch
            result = UnitResult(
                key=key,
                event_id=event_id,
                resource_id=resource_id,
                status="error",
                currency="USD",
                settlement_window=None,
                event_window=None,
                eligibility_window=None,
                baseline={},
                rate={},
                instruction={},
                metrics={},
                components=(),
                final_cents=0,
                errors=(
                    {
                        "code": "unhandled_unit_error",
                        "message": f"{type(exc).__name__}: {exc}",
                        "severity": "error",
                    },
                ),
                warnings=(),
                audit={"isolated": True},
            )
        unit_results.append(result)

    # Parse issues that could not be attributed to a unit are batch-level
    # errors.  They do not suppress the units that *could* be settled.
    for issue in request.parse_issues:
        issues.append({**issue, "scope": "batch"})

    status = _batch_status(unit_results, issues)
    totals = _batch_totals(unit_results)
    audit = {
        "rule_version": request.rules.rule_version,
        "payload_fingerprint": request.payload_fingerprint,
        "as_of": format_instant(request.as_of),
        "unit_count": len(unit_results),
        "settled_units": sum(1 for item in unit_results if item.status == "settled"),
        "zero_units": sum(1 for item in unit_results if item.status == "zero"),
        "revoked_units": sum(1 for item in unit_results if item.status == "revoked"),
        "error_units": sum(1 for item in unit_results if item.status == "error"),
        "deterministic_fingerprint": fingerprint(
            {
                "payload": request.payload_fingerprint,
                "as_of": request.as_of,
                "rules": request.rules.rule_version,
                "units": [(item.key, item.status, item.final_cents, item.audit.get("input_fingerprint")) for item in unit_results],
            }
        ),
        "issues": sorted(issues, key=lambda item: (item.get("code", ""), str(item.get("event_id", "")), str(item.get("resource_id", "")))),
        "warnings": sorted(warnings, key=lambda item: (item.get("code", ""), str(item.get("event_id", "")), str(item.get("resource_id", "")))),
    }
    return BatchResult(
        status=status,
        units=tuple(unit_results),
        errors=tuple(issues),
        totals=totals,
        audit=audit,
    )


def settle_unit(
    request: SettlementRequest,
    resolved: dict[str, ResolvedEvent],
    event_id: str,
    resource_id: str,
    *,
    batch_warnings: Optional[list[dict[str, Any]]] = None,
    resume_units: Optional[dict[str, dict]] = None,
) -> UnitResult:
    key = f"{event_id}/{resource_id}"
    collected = _UnitErrors()

    resolved_event = resolved.get(event_id)
    if resolved_event is None:
        collected.error("event_not_visible", "no event version is visible at the settlement as_of", event_id=event_id)
        return _empty_unit(key, event_id, resource_id, collected, resolved_event=None)

    event = resolved_event.event
    if resolved_event.revoked:
        return UnitResult(
            key=key,
            event_id=event_id,
            resource_id=resource_id,
            status="revoked",
            currency="USD",
            settlement_window=event.settlement_window or event.window,
            event_window=event.window,
            eligibility_window=None,
            baseline={},
            rate={},
            instruction={},
            metrics={"revoked": True, "reason": resolved_event.revocation_reason},
            components=(),
            final_cents=0,
            warnings=(),
            audit={
                "reason": resolved_event.revocation_reason,
                "event_version": resolved_event.version_key,
                "applied_corrections": list(resolved_event.applied_corrections),
                "input_fingerprint": fingerprint({"event": resolved_event.version_key, "revoked": True}),
            },
        )

    event_window = event.window
    settlement_window = event.settlement_window or event_window
    baseline_window = _baseline_window(event)

    eligibility, eligibility_declared = resolve_eligibility(
        request, event_id, resource_id, settlement_window, collected.warnings
    )
    eligibility_intervals = merge_intervals([item.window for item in eligibility])
    eligibility_cover = _intersect(eligibility_intervals, settlement_window)

    if not eligibility:
        collected.error(
            "outside_eligibility" if eligibility_declared else "eligibility_missing",
            "settlement window does not overlap any active eligibility window"
            if eligibility_declared
            else "no eligibility record exists for this resource and event",
            event_id=event_id,
            resource_id=resource_id,
            settlement_window=settlement_window.as_dict(timezone_name=event.timezone),
        )
        return _empty_unit(key, event_id, resource_id, collected, resolved_event=resolved_event, event=event)

    if not eligibility_cover:
        collected.error(
            "outside_eligibility",
            "settlement window does not overlap any active eligibility window",
            event_id=event_id,
            resource_id=resource_id,
        )
        return _empty_unit(
            key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility
        )

    covered_seconds = sum(item.duration_seconds for item in eligibility_cover)
    if covered_seconds < settlement_window.duration_seconds:
        collected.warn(
            "settlement_partially_eligible",
            "part of the settlement window is not covered by eligibility and will not be settled",
            coverage=str(Decimal(covered_seconds) / Decimal(settlement_window.duration_seconds)),
        )

    baseline = resolve_baseline(
        request,
        resolved_event,
        resource_id,
        baseline_window,
        _resource_readings(request, resource_id),
        collected.errors,
        collected.warnings,
    )

    if baseline.version is None and baseline.source in ("none", "materialized"):
        if not any(item.get("code") == "baseline_missing" for item in collected.errors):
            collected.error(
                "baseline_missing",
                "no authoritative baseline is available; event readings are never used as a substitute",
                event_id=event_id,
                resource_id=resource_id,
                detail=baseline.reason,
            )
        return _empty_unit(
            key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility, baseline=baseline
        )

    if baseline.coverage < request.rules.min_baseline_coverage:
        if request.rules.coverage_policy == "strict" or request.rules.baseline_policy_on_gap == "strict":
            collected.error(
                "baseline_insufficient_coverage",
                "baseline coverage is below the required threshold and the policy is strict",
                coverage=str(baseline.coverage),
                required=str(request.rules.min_baseline_coverage),
            )
            return _empty_unit(
                key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility, baseline=baseline
            )
        collected.warn(
            "baseline_prorated",
            "baseline coverage is below threshold; only covered intervals are settled",
            coverage=str(baseline.coverage),
            required=str(request.rules.min_baseline_coverage),
        )

    slots, _instruction_ids, duplicate_instructions = resolve_instructions(
        request,
        event_id,
        resource_id,
        settlement_window,
        collected.errors,
        collected.warnings,
        resolved_event,
    )
    if duplicate_instructions:
        collected.warn(
            "duplicate_instructions",
            "identical dispatch instructions were collapsed",
            details=list(duplicate_instructions),
        )
    if not slots:
        collected.error(
            "instruction_missing",
            "no active dispatch instruction covers the settlement window",
            event_id=event_id,
            resource_id=resource_id,
        )
        return _empty_unit(
            key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility, baseline=baseline
        )

    rates, _rate_ids = resolve_rates(request, resolved_event, settlement_window, collected.errors, collected.warnings)
    if not rates:
        if not collected.has_errors:
            collected.error("rate_unavailable", "no active rate covers the settlement window")
        return _empty_unit(
            key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility, baseline=baseline
        )

    try:
        currency = _rate_currency(rates)
    except ValueError as exc:
        collected.error("currency_conflict", str(exc))
        return _empty_unit(
            key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility, baseline=baseline
        )

    readings_selection = resolve_readings(
        _resource_readings(request, resource_id),
        resource_id,
        settlement_window,
        request.rules,
        request.as_of,
        collected.warnings,
    )
    executions_selection = resolve_executions(
        _resource_executions(request, resource_id), settlement_window, request.rules, request.as_of, collected.warnings
    )

    actual_selection = readings_selection
    actual_source = "meter_readings"
    if not actual_selection.samples and executions_selection.samples:
        actual_selection = executions_selection
        actual_source = "execution_telemetry"
        collected.warn(
            "actual_from_execution_telemetry",
            "no billing meter readings in the window; execution telemetry used as the actual load source",
        )
    if not actual_selection.samples:
        collected.error(
            "actual_missing",
            "no meter reading or execution sample covers the settlement window",
            event_id=event_id,
            resource_id=resource_id,
        )
        return _empty_unit(
            key, event_id, resource_id, collected, resolved_event=resolved_event, event=event, eligibility=eligibility, baseline=baseline
        )

    segments, segment_warnings, uninstructed, gaps, actual_gaps = _build_segments(
        request,
        event,
        eligibility_cover,
        baseline,
        actual_selection,
        slots,
        rates,
        executions_selection,
        resolved_event,
    )
    collected.warnings.extend(segment_warnings)

    input_fingerprint = _input_fingerprint(
        request, resolved_event, baseline, slots, rates, actual_selection, eligibility_cover, settlement_window, actual_source
    )
    if resume_units and key in resume_units:
        checkpoint = resume_units[key]
        previous = checkpoint.get("unit") or {}
        # Reuse only a *successful* unit with identical inputs.  An errored unit
        # is always retried: the failure may have been a transient upstream
        # condition, and freezing it would defeat the point of resuming.
        if (
            checkpoint.get("input_fingerprint") == input_fingerprint
            and previous.get("status") in ("settled", "zero", "revoked")
        ):
            reused = unit_to_dict(_unit_from_dict(previous))
            reused["audit"] = {**reused.get("audit", {}), "resumed_from_checkpoint": True}
            return _unit_from_dict(reused)

    components: list[ComponentResult] = []
    if segments:
        components = _components(
            request,
            event,
            baseline,
            segments,
            key,
            actual_source,
        )
    else:
        collected.error(
            "no_settleable_segments",
            "no interval has both an instruction and a rate covering it",
        )

    final_cents = sum(component.amount_cents for component in components)
    total_exact = sum_decimals(component.amount_exact for component in components)

    metrics = _metrics(baseline, segments, actual_selection, executions_selection, settlement_window, eligibility_cover)
    metrics["actual_source"] = actual_source
    metrics["uninstructed_seconds"] = sum(item.duration_seconds for item in uninstructed)
    metrics["baseline_gap_seconds"] = sum(item.duration_seconds for item in gaps)
    metrics["actual_gap_seconds"] = sum(item.duration_seconds for item in actual_gaps)
    metrics["late_readings_excluded"] = len(readings_selection.excluded) + len(executions_selection.excluded)

    prior_bill, bill_history = resolve_prior_bill(request, event_id, resource_id)
    bill_payload = _bill_payload(prior_bill, final_cents, currency, input_fingerprint)

    status = "settled" if final_cents != 0 else "zero"
    if collected.has_errors:
        status = "error"
        final_cents = 0
        components = []

    winning_rate = _dominant([segment.rate for segment in segments])
    winning_instruction = _dominant([segment.slot for segment in segments])
    audit = {
        "event_version": resolved_event.version_key,
        "input_fingerprint": input_fingerprint,
        "as_of": format_instant(request.as_of),
        "rule_version": request.rules.rule_version,
        "windows": {
            "eligibility": [
                {
                    **item.window.as_dict(timezone_name=event.timezone),
                    "eligibility_id": item.eligibility_id,
                    "version": item.version,
                }
                for item in eligibility
            ],
            "eligibility_intersection": [item.as_dict(timezone_name=event.timezone) for item in eligibility_cover],
            "event": event_window.as_dict(timezone_name=event.timezone),
            "settlement": settlement_window.as_dict(timezone_name=event.timezone),
            "baseline": baseline_window.as_dict(timezone_name=event.timezone),
            "baseline_gaps": [item.as_dict(timezone_name=event.timezone) for item in baseline.gaps],
            "relation": _window_relation(event_window, settlement_window, eligibility_cover),
        },
        "baseline": {
            "source": baseline.source,
            "version": baseline.version_label,
            "fingerprint": baseline.fingerprint,
            "coverage": format_decimal(baseline.coverage, places=6),
            "covered_seconds": baseline.covered_seconds,
            "total_seconds": baseline.total_seconds,
            "substituted": baseline.substituted,
            "reason": baseline.reason,
            "sample_count": len(baseline.samples),
        },
        "rates": [
            {
                "label": rate.label,
                "fingerprint": fingerprint(_rate_identity(rate.rate)),
                "window": rate.interval.as_dict(timezone_name=event.timezone),
            }
            for rate in rates
        ],
        "instructions": [
            {
                "label": slot.label,
                "fingerprint": fingerprint(_instruction_identity(slot.instruction)),
                "window": slot.interval.as_dict(timezone_name=event.timezone),
                "capacity_kw": format_decimal(slot.instruction.capacity_kw),
                "effective_capacity_kw": format_decimal(slot.capacity_kw),
            }
            for slot in slots
        ],
        "readings": {
            "selected": len(readings_selection.samples),
            "excluded": list(readings_selection.excluded),
            "versions": [str(sample.sample_id) for sample in readings_selection.samples],
        },
        "executions": {
            "selected": len(executions_selection.samples),
            "excluded": list(executions_selection.excluded),
        },
        "rounding": {
            "mode": request.rules.rounding,
            "component_policy": "round each component once to cents; allocate with largest remainder",
            "exact_total": format_decimal(total_exact, places=10),
            "final_cents": final_cents,
            "components": [
                {
                    "name": component.name,
                    "exact": format_decimal(component.amount_exact, places=10),
                    "cents": component.amount_cents,
                    "line_cents_sum": sum(line.amount_cents for line in component.lines),
                }
                for component in components
            ],
        },
        "revisions": {
            "prior_bill": _prior_bill_dict(prior_bill),
            "history": [_prior_bill_dict(item) for item in bill_history],
            "new_revision": bill_payload,
            "locked_prior_unchanged": prior_bill is not None and prior_bill.status == "locked",
        },
        "deterministic_fingerprint": fingerprint(
            {
                "input": input_fingerprint,
                "components": [(component.name, component.amount_cents) for component in components],
                "final_cents": final_cents,
            }
        ),
    }
    audit["stages"] = _stage_fingerprints(request, resolved_event, baseline, slots, rates, segments, components)

    warnings_out = tuple(collected.warnings)
    if batch_warnings is not None:
        batch_warnings.extend({**item, "unit": key} for item in collected.warnings)

    return UnitResult(
        key=key,
        event_id=event_id,
        resource_id=resource_id,
        status=status,
        currency=currency,
        settlement_window=settlement_window,
        event_window=event_window,
        eligibility_window=eligibility_cover[0] if eligibility_cover else None,
        baseline={
            "source": baseline.source,
            "version": baseline.version_label,
            "coverage": format_decimal(baseline.coverage, places=6),
            "fingerprint": baseline.fingerprint,
            "gaps": [gap.as_dict(timezone_name=event.timezone) for gap in baseline.gaps],
            "samples": len(baseline.samples),
        },
        rate={
            "currency": currency,
            "versions": [segment.label for segment in rates],
            "dominant": winning_rate.label if winning_rate else None,
        },
        instruction={
            "versions": [slot.label for slot in slots],
            "dominant": winning_instruction.label if winning_instruction else None,
            "duplicates_collapsed": len(duplicate_instructions),
        },
        metrics=metrics,
        components=tuple(components),
        final_cents=final_cents,
        errors=tuple(collected.errors),
        warnings=warnings_out,
        audit=audit,
        bill=bill_payload,
        revision={"number": bill_payload["revision"], "adjustment_cents": bill_payload["adjustment_cents"]}
        if bill_payload
        else None,
    )


# ---------------------------------------------------------------------------
# Segment construction and component math
# ---------------------------------------------------------------------------


def _build_segments(
    request: SettlementRequest,
    event: Event,
    basis: Sequence[Interval],
    baseline: BaselineSelection,
    actual: RecordSelection,
    slots: Sequence[InstructionSlot],
    rates: Sequence[RateSegment],
    executions: RecordSelection,
    resolved_event: ResolvedEvent,
) -> tuple[
    list[_Segment],
    list[dict[str, Any]],
    list[Interval],
    list[Interval],
    list[Interval],
]:
    warnings: list[dict[str, Any]] = []
    points = _boundaries(basis, slots, rates, baseline, actual, event.timezone, request.rules.split_at_local_midnight)

    segments: list[_Segment] = []
    uninstructed: list[Interval] = []
    gaps: list[Interval] = []
    actual_gaps: list[Interval] = []

    for window in basis:
        for piece in _slice(window, points):
            slot = _covering_slot(piece, slots)
            rate = _covering_rate(piece, rates)
            if slot is None or rate is None:
                uninstructed.append(piece)
                continue

            baseline_kw = baseline.value_for(piece)
            baseline_segment_coverage, baseline_ids = _baseline_coverage(baseline, piece, baseline_kw)
            actual_segment_coverage, actual_ids = _sample_coverage(actual.samples, piece)
            execution_segment_coverage, execution_ids = _sample_coverage(executions.samples, piece)

            actual_kw: Optional[Decimal] = None
            execution_kw: Optional[Decimal] = None
            flags: list[str] = []

            if baseline_kw is None:
                gaps.append(piece)
                flags.append("baseline_gap")
                # Never substitute event readings for a missing baseline.  The
                # capacity component may still be payable; performance and
                # deviation cannot be calculated without a reference.
                warnings.append(
                    {
                        "code": "baseline_gap_interval",
                        "message": "baseline has no coverage for a settlement interval; performance is not paid there",
                        "severity": "warning",
                        "interval": piece.as_dict(timezone_name=event.timezone),
                        "event_id": event.event_id,
                    }
                )

            if actual_segment_coverage > 0:
                actual_kw = _weighted(actual.samples, piece)
            else:
                flags.append("actual_gap")
                actual_gaps.append(piece)
                warnings.append(
                    {
                        "code": "actual_gap_interval",
                        "message": "no meter/execution sample covers a settlement interval",
                        "severity": "warning",
                        "interval": piece.as_dict(timezone_name=event.timezone),
                        "event_id": event.event_id,
                    }
                )

            if execution_segment_coverage > 0:
                execution_kw = _weighted(executions.samples, piece)

            if baseline_segment_coverage < 1:
                flags.append("baseline_partial_coverage")
            if actual_segment_coverage < 1:
                flags.append("actual_partial_coverage")

            _capacity, override = resolved_event.capacity_for(slot.instruction)
            if override is not None:
                flags.append("capacity_overridden_by_correction")

            segments.append(
                _Segment(
                    interval=piece,
                    slot=slot,
                    rate=rate,
                    baseline_kw=baseline_kw,
                    actual_kw=actual_kw,
                    execution_kw=execution_kw,
                    baseline_coverage=baseline_segment_coverage,
                    actual_coverage=actual_segment_coverage,
                    baseline_source_ids=baseline_ids,
                    actual_source_ids=tuple(sorted(set(actual_ids).union(execution_ids))),
                    capacity_override=override,
                    flags=tuple(sorted(set(flags))),
                )
            )
    return segments, warnings, uninstructed, gaps, actual_gaps


def _components(
    request: SettlementRequest,
    event: Event,
    baseline: BaselineSelection,
    segments: Sequence[_Segment],
    unit_key: str,
    actual_source: str,
) -> list[ComponentResult]:
    rules = request.rules
    sign = Decimal(1) if event.direction == "reduce" else Decimal(-1)
    rule_version = rules.rule_version or RULE_VERSION

    capacity_segments: list[_Segment] = []
    capacity_exact: list[Decimal] = []
    capacity_qty: list[Decimal] = []
    capacity_rates: list[Decimal] = []
    capacity_flags: list[tuple[str, ...]] = []

    performance_segments: list[_Segment] = []
    performance_exact: list[Decimal] = []
    performance_qty: list[Decimal] = []
    performance_rates: list[Decimal] = []
    performance_flags: list[tuple[str, ...]] = []

    deviation_segments: list[_Segment] = []
    deviation_exact: list[Decimal] = []
    deviation_qty: list[Decimal] = []
    deviation_rates: list[Decimal] = []
    deviation_flags: list[tuple[str, ...]] = []

    event_basis_seconds = sum((slot.interval.duration_seconds for slot in _unique_slots(segments)), 0)
    if event_basis_seconds == 0:
        # Fall back to the settlement span so a zero-duration slot set cannot
        # make a per-event capacity payment vanish silently.
        event_basis_seconds = sum(segment.interval.duration_seconds for segment in segments)

    for segment in segments:
        instruction = segment.slot.instruction
        rate = segment.rate.rate
        hours = segment.hours
        capacity_kwh = segment.slot.capacity_kw * hours

        # capacity is availability-based: paid for the committed capacity.
        cap_rate, cap_basis = _rate_value_for_capacity(rate)
        if cap_basis == "kw_event":
            # Allocate the once-per-event payment across intervals by time share.
            share = (
                Decimal(segment.interval.duration_seconds) / Decimal(event_basis_seconds)
                if event_basis_seconds
                else Decimal(1)
            )
            cap_quantity = segment.slot.capacity_kw * share
        else:
            cap_quantity = capacity_kwh
        capacity_segments.append(segment)
        capacity_qty.append(cap_quantity)
        capacity_rates.append(cap_rate)
        capacity_exact.append(cap_quantity * cap_rate)
        capacity_flags.append(segment.flags)

        if segment.baseline_kw is None or segment.actual_kw is None:
            continue

        delivered_kw = (segment.baseline_kw - segment.actual_kw) * sign
        delivered_kwh = delivered_kw * hours
        instructed_kwh = segment.slot.capacity_kw * hours

        ratio: Optional[Decimal]
        if instructed_kwh > 0:
            ratio = delivered_kwh / instructed_kwh
        else:
            ratio = None

        factor, _band_label = _pick_performance_factor(rules, rate, ratio)
        if rate.energy_rate_basis == "kw":
            # Demand-style tariff: the measured kW itself is priced, so the
            # quantity is a time-weighted average rather than an integral.
            performance_kwh = delivered_kw
            if performance_kwh < 0:
                performance_kwh = ZERO
        else:
            performance_kwh = delivered_kwh
            if performance_kwh < 0:
                performance_kwh = ZERO
            if instructed_kwh > 0 and not rules.credit_overdelivery:
                performance_kwh = min(performance_kwh, instructed_kwh)
        if performance_kwh == 0:
            continue

        perf_flags = list(segment.flags)
        if rate.energy_rate_basis != "kw" and instructed_kwh > 0 and delivered_kwh > instructed_kwh and not rules.credit_overdelivery:
            perf_flags.append("overdelivery_capped_at_instruction")
        if ratio is not None and factor == 0 and _band_label:
            perf_flags.append(f"performance_band:{_band_label}")

        performance_segments.append(segment)
        performance_qty.append(performance_kwh)
        performance_rates.append(rate.energy_rate_per_kwh * factor)
        performance_exact.append(performance_kwh * rate.energy_rate_per_kwh * factor)
        performance_flags.append(tuple(perf_flags))

        if rules.shortfall_penalty:
            # ``deviation_basis`` selects the reference the shortfall is
            # measured against:
            #   :instruction: the dispatched capacity that the resource accepted
            #   :execution:   what the resource actually executed, when
            #                 telemetry exists (falls back to the instruction).
            # The distinction matters for partial execution: a resource that
            # executed less than it was asked to is compared with its own
            # execution under ``execution``, not with the full instruction.
            if rules.deviation_basis == "execution" and segment.execution_kw is not None:
                reference_kwh = segment.execution_kw * hours
                deviation_basis_effective = "execution_telemetry"
            else:
                reference_kwh = instructed_kwh
                deviation_basis_effective = "instruction"
            shortfall_basis = max(ZERO, reference_kwh - delivered_kwh)
            if rate.energy_rate_basis == "kw":
                reference_kw = (
                    segment.execution_kw
                    if rules.deviation_basis == "execution" and segment.execution_kw is not None
                    else segment.slot.capacity_kw
                )
                shortfall_basis = max(ZERO, reference_kw - delivered_kw)
            if shortfall_basis > 0:
                deviation_segments.append(segment)
                deviation_qty.append(shortfall_basis)
                deviation_rates.append(rate.deviation_rate_per_kwh)
                deviation_exact.append(-shortfall_basis * rate.deviation_rate_per_kwh)
                deviation_flags.append(
                    tuple(segment.flags)
                    + ("shortfall_penalty", f"deviation_basis:{deviation_basis_effective}")
                )

    components: list[ComponentResult] = []
    if capacity_segments:
        components.append(
            _component_lines(
                component="capacity",
                unit_key=unit_key,
                segments=capacity_segments,
                exact_amounts=capacity_exact,
                quantities=capacity_qty,
                unit_rates=capacity_rates,
                baseline_version=baseline.version_label,
                rule_version=rule_version,
                extra_flags=capacity_flags,
            )
        )
    if performance_segments:
        components.append(
            _component_lines(
                component="performance",
                unit_key=unit_key,
                segments=performance_segments,
                exact_amounts=performance_exact,
                quantities=performance_qty,
                unit_rates=performance_rates,
                baseline_version=baseline.version_label,
                rule_version=rule_version,
                extra_flags=performance_flags,
            )
        )
    if deviation_segments:
        components.append(
            _component_lines(
                component="deviation",
                unit_key=unit_key,
                segments=deviation_segments,
                exact_amounts=deviation_exact,
                quantities=deviation_qty,
                unit_rates=deviation_rates,
                baseline_version=baseline.version_label,
                rule_version=rule_version,
                extra_flags=deviation_flags,
            )
        )
    return components


def _unique_slots(segments: Sequence[_Segment]) -> list[InstructionSlot]:
    seen: dict[str, InstructionSlot] = {}
    for segment in segments:
        seen[segment.slot.label] = segment.slot
    return [seen[label] for label in sorted(seen)]


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _baseline_window(event: Event) -> Interval:
    if event.baseline_window is not None:
        return event.baseline_window
    if event.baseline_lookback_minutes:
        return Interval(
            event.window.start - timedelta(minutes=event.baseline_lookback_minutes),
            event.window.start,
        )
    lookback = max(event.window.duration_seconds, 60)
    return Interval(event.window.start - timedelta(seconds=lookback), event.window.start)


def _intersect(intervals: Sequence[Interval], window: Interval) -> list[Interval]:
    result: list[Interval] = []
    for item in intervals:
        clipped = item.clip_to(window)
        if clipped is not None:
            result.append(clipped)
    return merge_intervals(result)


def _resource_readings(request: SettlementRequest, resource_id: str) -> list[Reading]:
    return [reading for reading in request.readings if reading.resource_id == resource_id]


def _resource_executions(request: SettlementRequest, resource_id: str) -> list[ExecutionSample]:
    return [sample for sample in request.executions if sample.resource_id == resource_id]


def _dominant(items: Sequence[Any]) -> Any:
    if not items:
        return None
    counts: dict[str, Any] = {}
    for item in items:
        label = getattr(item, "label", str(item))
        counts[label] = item
    return counts[sorted(counts)[-1]] if counts else None


def _metrics(
    baseline: BaselineSelection,
    segments: Sequence[_Segment],
    actual: RecordSelection,
    executions: RecordSelection,
    settlement_window: Interval,
    eligibility_cover: Sequence[Interval],
) -> dict[str, Any]:
    hours = Decimal(settlement_window.duration_seconds) / Decimal(3600)
    baseline_kwh = sum_decimals(
        (segment.baseline_kw or ZERO) * segment.hours for segment in segments if segment.baseline_kw is not None
    )
    actual_kwh = sum_decimals(
        (segment.actual_kw or ZERO) * segment.hours for segment in segments if segment.actual_kw is not None
    )
    delivered_kwh = sum_decimals(
        ((segment.baseline_kw or ZERO) - (segment.actual_kw or ZERO)) * segment.hours
        for segment in segments
        if segment.baseline_kw is not None and segment.actual_kw is not None
    )
    instructed_kwh = sum_decimals(segment.slot.capacity_kw * segment.hours for segment in segments)
    ratio = delivered_kwh / instructed_kwh if instructed_kwh > 0 else None
    return {
        "window_hours": format_decimal(hours, places=6),
        "baseline_kwh": format_decimal(baseline_kwh, places=6),
        "actual_kwh": format_decimal(actual_kwh, places=6),
        "delivered_kwh": format_decimal(delivered_kwh, places=6),
        "instructed_kwh": format_decimal(instructed_kwh, places=6),
        "performance_ratio": format_decimal(ratio, places=6) if ratio is not None else None,
        "baseline_coverage": format_decimal(baseline.coverage, places=6),
        "segment_count": len(segments),
        "meter_sample_count": len(actual.samples),
        "execution_sample_count": len(executions.samples),
        "eligibility_coverage": format_decimal(
            Decimal(sum(item.duration_seconds for item in eligibility_cover)) / Decimal(settlement_window.duration_seconds),
            places=6,
        )
        if settlement_window.duration_seconds
        else "1",
    }


def _window_relation(event: Interval, settlement: Interval, eligibility: Sequence[Interval]) -> dict[str, Any]:
    return {
        "settlement_equals_event": settlement.start == event.start and settlement.end == event.end,
        "settlement_within_event": event.contains(settlement),
        "event_overlaps_settlement": event.overlaps(settlement),
        "eligibility_covers_settlement": all(
            any(item.contains(piece) for item in eligibility)
            for piece in [settlement]
        ),
    }


def _empty_unit(
    key: str,
    event_id: str,
    resource_id: str,
    collected: _UnitErrors,
    *,
    resolved_event: Optional[ResolvedEvent] = None,
    event: Optional[Event] = None,
    eligibility: Optional[Sequence[Any]] = None,
    baseline: Optional[BaselineSelection] = None,
) -> UnitResult:
    audit: dict[str, Any] = {"isolated": True}
    if resolved_event is not None:
        audit["event_version"] = resolved_event.version_key
        audit["applied_corrections"] = list(resolved_event.applied_corrections)
    if baseline is not None:
        audit["baseline"] = {
            "source": baseline.source,
            "version": baseline.version_label,
            "coverage": format_decimal(baseline.coverage, places=6),
            "reason": baseline.reason,
        }
    audit["input_fingerprint"] = fingerprint(
        {"event": resolved_event.version_key if resolved_event else event_id, "resource": resource_id, "errors": collected.errors}
    )
    return UnitResult(
        key=key,
        event_id=event_id,
        resource_id=resource_id,
        status="error",
        currency="USD",
        settlement_window=(event.settlement_window or event.window) if event else None,
        event_window=event.window if event else None,
        eligibility_window=eligibility[0].window if eligibility else None,
        baseline={"version": baseline.version_label, "coverage": format_decimal(baseline.coverage, places=6)}
        if baseline
        else {},
        rate={},
        instruction={},
        metrics={},
        components=(),
        final_cents=0,
        errors=tuple(collected.errors),
        warnings=tuple(collected.warnings),
        audit=audit,
    )


def _input_fingerprint(
    request: SettlementRequest,
    resolved_event: ResolvedEvent,
    baseline: BaselineSelection,
    slots: Sequence[InstructionSlot],
    rates: Sequence[RateSegment],
    actual: RecordSelection,
    eligibility_cover: Sequence[Interval],
    settlement_window: Interval,
    actual_source: str,
) -> str:
    return fingerprint(
        {
            "settlement": [settlement_window.start, settlement_window.end],
            "eligibility": [[item.start, item.end] for item in eligibility_cover],
            "event": resolved_event.version_key,
            "corrections": list(resolved_event.applied_corrections),
            "baseline": baseline.fingerprint,
            "instructions": [fingerprint(_instruction_identity(slot.instruction)) for slot in slots],
            "rates": [fingerprint(_rate_identity(segment.rate)) for segment in rates],
            "actual": [
                {"id": sample.sample_id, "start": sample.interval.start, "end": sample.interval.end, "kw": sample.kw, "version": sample.version}
                for sample in actual.samples
            ],
            "actual_source": actual_source,
            "rules": {
                "version": request.rules.rule_version,
                "baseline_policy": request.rules.baseline_policy,
                "coverage_policy": request.rules.coverage_policy,
                "deviation_basis": request.rules.deviation_basis,
                "shortfall_penalty": request.rules.shortfall_penalty,
                "credit_overdelivery": request.rules.credit_overdelivery,
                "split_at_local_midnight": request.rules.split_at_local_midnight,
                "performance_bands": [
                    (band.min_ratio, band.max_ratio, band.factor) for band in request.rules.performance_bands
                ],
            },
            "as_of": request.as_of,
        }
    )


def _stage_fingerprints(
    request: SettlementRequest,
    resolved_event: ResolvedEvent,
    baseline: BaselineSelection,
    slots: Sequence[InstructionSlot],
    rates: Sequence[RateSegment],
    segments: Sequence[_Segment],
    components: Sequence[ComponentResult],
) -> dict[str, Any]:
    return {
        "1_event_resolution": fingerprint(resolved_event.version_key),
        "2_window_model": fingerprint(
            {
                "event": [resolved_event.event.window.start, resolved_event.event.window.end],
                "settlement": [
                    (resolved_event.event.settlement_window or resolved_event.event.window).start,
                    (resolved_event.event.settlement_window or resolved_event.event.window).end,
                ],
                "baseline": [baseline.window.start, baseline.window.end],
            }
        ),
        "3_baseline_selection": baseline.fingerprint,
        "4_instruction_selection": fingerprint([fingerprint(_instruction_identity(slot.instruction)) for slot in slots]),
        "5_rate_selection": fingerprint([fingerprint(_rate_identity(segment.rate)) for segment in rates]),
        "6_segmentation": fingerprint(
            [
                {
                    "start": segment.interval.start,
                    "end": segment.interval.end,
                    "instruction": segment.slot.label,
                    "rate": segment.rate.label,
                    "baseline_kw": segment.baseline_kw,
                    "actual_kw": segment.actual_kw,
                }
                for segment in segments
            ]
        ),
        "7_components": fingerprint(
            [(component.name, component.amount_exact, component.amount_cents) for component in components]
        ),
        "8_inputs": request.payload_fingerprint,
    }


def _rate_identity(rate: RateVersion) -> dict[str, Any]:
    return {
        "id": rate.rate_version_id,
        "version": rate.version,
        "window": [rate.window.start, rate.window.end],
        "issued_at": rate.issued_at,
        "capacity": rate.capacity_rate_per_kw_hour,
        "capacity_event": rate.capacity_rate_per_kw_event,
        "energy": rate.energy_rate_per_kwh,
        "deviation": rate.deviation_rate_per_kwh,
        "currency": rate.currency,
        "direction": rate.direction,
        "priority": rate.priority,
    }


def _instruction_identity(instruction: Any) -> dict[str, Any]:
    return {
        "id": instruction.instruction_id,
        "version": instruction.version,
        "window": [instruction.window.start, instruction.window.end],
        "issued_at": instruction.issued_at,
        "capacity_kw": instruction.capacity_kw,
        "direction": instruction.direction,
        "status": instruction.status,
    }


def _prior_bill_dict(bill: Optional[PriorBill]) -> Optional[dict[str, Any]]:
    if bill is None:
        return None
    return {
        "bill_id": bill.bill_id,
        "event_id": bill.event_id,
        "resource_id": bill.resource_id,
        "revision": bill.revision,
        "status": bill.status,
        "amount_cents": bill.amount_cents,
        "amount": format_money(bill.amount_cents),
        "currency": bill.currency,
        "fingerprint": bill.fingerprint,
    }


def _bill_payload(
    prior: Optional[PriorBill], final_cents: int, currency: str, input_fingerprint: str
) -> dict[str, Any]:
    if prior is None:
        return {
            "bill_id": f"bill-{fingerprint(input_fingerprint)[:12]}",
            "revision": 1,
            "status": "draft",
            "amount_cents": final_cents,
            "amount": format_money(final_cents),
            "currency": currency,
            "adjustment_cents": final_cents,
            "adjustment": format_money(final_cents),
            "supersedes": None,
            "input_fingerprint": input_fingerprint,
            "locked_prior_unchanged": None,
        }
    revision = prior.revision + 1
    return {
        "bill_id": prior.bill_id,
        "revision": revision,
        "status": "draft",
        "amount_cents": final_cents,
        "amount": format_money(final_cents),
        "currency": currency,
        "adjustment_cents": final_cents - prior.amount_cents,
        "adjustment": format_money(final_cents - prior.amount_cents),
        "supersedes": {
            "bill_id": prior.bill_id,
            "revision": prior.revision,
            "status": prior.status,
            "amount_cents": prior.amount_cents,
            "amount": format_money(prior.amount_cents),
            "rewritten": False,
        },
        "input_fingerprint": input_fingerprint,
        "locked_prior_unchanged": prior.status == "locked" and prior.amount_cents == prior.amount_cents,
    }


def _batch_status(units: Sequence[UnitResult], issues: Sequence[dict[str, Any]]) -> str:
    if not units:
        return "error" if issues else "ok"
    statuses = {unit.status for unit in units}
    if statuses <= {"settled", "zero", "revoked"}:
        return "ok" if not issues else "partial"
    if statuses <= {"error"}:
        return "error"
    return "partial"


def _batch_totals(units: Sequence[UnitResult]) -> dict[str, Any]:
    by_currency: dict[str, dict[str, Any]] = {}
    for unit in units:
        bucket = by_currency.setdefault(
            unit.currency,
            {
                "units": 0,
                "settled_cents": 0,
                "capacity_cents": 0,
                "performance_cents": 0,
                "deviation_cents": 0,
                "error_units": 0,
            },
        )
        bucket["units"] += 1
        if unit.status == "error":
            bucket["error_units"] += 1
            continue
        bucket["settled_cents"] += unit.final_cents
        for component in unit.components:
            key = f"{component.name}_cents"
            if key in bucket:
                bucket[key] += component.amount_cents
    for currency, bucket in by_currency.items():
        bucket["settled_amount"] = format_money(bucket["settled_cents"])
        bucket["capacity_amount"] = format_money(bucket["capacity_cents"])
        bucket["performance_amount"] = format_money(bucket["performance_cents"])
        bucket["deviation_amount"] = format_money(bucket["deviation_cents"])
        bucket["currency"] = currency
    if len(by_currency) == 1:
        currency, bucket = next(iter(by_currency.items()))
        return {"currency": currency, **bucket}
    return {"by_currency": {currency: by_currency[currency] for currency in sorted(by_currency)}}


def _unit_from_dict(data: dict[str, Any]) -> UnitResult:
    """Rehydrate a checkpointed unit result from its serialized report form."""

    from .report import unit_from_dict

    return unit_from_dict(data)


def unit_to_dict(unit: UnitResult) -> dict[str, Any]:
    from .report import unit_to_dict as _serialize

    return _serialize(unit)


# ---------------------------------------------------------------------------
# Design comparison
# ---------------------------------------------------------------------------


DESIGN_NOTES = {
    "materialized": {
        "summary": "Persist a baseline version per event/resource before settlement and settle against a stored snapshot.",
        "strengths": [
            "Reproducible: the exact baseline is immutable and fingerprinted.",
            "Cheap to audit and re-bill: no re-derivation from the reading lake.",
            "Explicit gaps: a missing materialized baseline fails the unit instead of being invented.",
        ],
        "weaknesses": [
            "Requires an extra materialization step and storage for every event.",
            "Late or corrected pre-event readings need an explicit baseline revision.",
        ],
    },
    "dynamic": {
        "summary": "Derive the baseline at settlement time from pre-event meter readings.",
        "strengths": [
            "Always uses the freshest reading set, including late arrivals.",
            "No extra storage or pre-computation step.",
        ],
        "weaknesses": [
            "Historical recalculation requires replaying readings as of the original instant.",
            "Sensitive to reading revisions and retention; harder to prove a prior bill.",
            "It is easy to accidentally include in-event readings, which this engine forbids.",
        ],
    },
    "hybrid": {
        "summary": "Prefer the newest visible materialized baseline; fall back to an explicit dynamic derivation only when coverage is insufficient.",
        "strengths": [
            "Deterministic when a version exists, with a documented fallback when it does not.",
            "The fallback is recorded as a distinct source and never masquerades as a materialized version.",
            "Best fit for revision-heavy tariffs: re-settlement uses the same version selection rules.",
        ],
        "weaknesses": [
            "Two code paths must be tested; the fallback decision needs an audit trail.",
            "A stale materialized baseline can hide a better dynamic one unless coverage is checked.",
        ],
    },
}


def compare_designs(request: SettlementRequest) -> dict[str, Any]:
    """Run the same units under all three baseline policies and explain the deltas."""

    outcomes: dict[str, Any] = {}
    for policy in ("materialized", "dynamic", "hybrid"):
        rules = _replace_rules(request.rules, baseline_policy=policy)
        variant = _replace_request(request, rules=rules)
        result = settle(variant)
        outcomes[policy] = {
            "status": result.status,
            "units": [
                {
                    "key": unit.key,
                    "status": unit.status,
                    "final_cents": unit.final_cents,
                    "final_amount": unit.final_amount,
                    "baseline_source": unit.baseline.get("source"),
                    "baseline_version": unit.baseline.get("version"),
                }
                for unit in result.units
            ],
            "totals": result.totals,
        }
    deltas = _design_deltas(outcomes)
    return {"designs": outcomes, "deltas": deltas, "notes": DESIGN_NOTES}


def _design_deltas(outcomes: dict[str, Any]) -> list[dict[str, Any]]:
    keys = sorted({unit["key"] for outcome in outcomes.values() for unit in outcome["units"]})
    deltas: list[dict[str, Any]] = []
    for key in keys:
        amounts = {}
        for policy, outcome in outcomes.items():
            for unit in outcome["units"]:
                if unit["key"] == key:
                    amounts[policy] = unit["final_cents"]
        if len(set(amounts.values())) > 1:
            deltas.append(
                {
                    "key": key,
                    "amounts_cents": amounts,
                    "max_delta_cents": max(amounts.values()) - min(amounts.values()),
                }
            )
    return deltas


def _replace_rules(rules: SettlementRules, **changes: Any) -> SettlementRules:
    data = {
        "rule_version": rules.rule_version,
        "baseline_policy": rules.baseline_policy,
        "honor_baseline_revisions": rules.honor_baseline_revisions,
        "min_baseline_coverage": rules.min_baseline_coverage,
        "coverage_policy": rules.coverage_policy,
        "baseline_policy_on_gap": rules.baseline_policy_on_gap,
        "deviation_basis": rules.deviation_basis,
        "rounding": rules.rounding,
        "split_at_local_midnight": rules.split_at_local_midnight,
        "allow_stacking_instructions": rules.allow_stacking_instructions,
        "performance_bands": rules.performance_bands,
        "shortfall_penalty": rules.shortfall_penalty,
        "credit_overdelivery": rules.credit_overdelivery,
        "late_reading_policy": rules.late_reading_policy,
        "rate_conflict_policy": rules.rate_conflict_policy,
        "max_settlement_rounds": rules.max_settlement_rounds,
        "notes": rules.notes,
    }
    data.update(changes)
    return SettlementRules(**data)


def _replace_request(request: SettlementRequest, **changes: Any) -> SettlementRequest:
    data = {
        "events": request.events,
        "eligibility": request.eligibility,
        "baselines": request.baselines,
        "instructions": request.instructions,
        "executions": request.executions,
        "readings": request.readings,
        "rates": request.rates,
        "corrections": request.corrections,
        "prior_bills": request.prior_bills,
        "resources": request.resources,
        "rules": request.rules,
        "as_of": request.as_of,
        "payload_fingerprint": request.payload_fingerprint,
        "warnings": request.warnings,
        "parse_issues": request.parse_issues,
    }
    data.update(changes)
    return SettlementRequest(**data)


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


def calculate(rows: list[dict], payload: dict) -> dict:
    """Original single-event calculation, retained for API compatibility.

    New code should call :func:`settle` with a parsed
    :class:`~grid_flex_settlement.models.SettlementRequest`.  This function is a
    thin shim so existing callers keep working; the CLI and pipeline use the
    event-level engine for every payload.
    """

    from .parser import parse_payload

    request = parse_payload({**payload, "readings": rows} if rows else payload)
    batch = settle(request)
    if len(batch.units) != 1:
        return {"settlement_amount": 0.0, "units": len(batch.units)}
    unit = batch.units[0]
    metrics = unit.metrics
    window_seconds = unit.settlement_window.duration_seconds if unit.settlement_window else 3600
    hours = Decimal(window_seconds) / Decimal(3600) if window_seconds else Decimal(1)
    return {
        "baseline_kw": float(dec(metrics.get("baseline_kwh", "0")) / hours),
        "actual_kw": float(dec(metrics.get("actual_kwh", "0")) / hours),
        "delivered_kw": float(dec(metrics.get("delivered_kwh", "0")) / hours),
        "settlement_amount": float(from_minor(unit.final_cents)),
        "explanation": {
            "baseline_rows": unit.baseline.get("samples", 0),
            "rate": unit.audit.get("rates", [{}])[0].get("label") if unit.audit.get("rates") else None,
            "baseline_version": unit.baseline.get("version"),
            "rule_version": unit.audit.get("rule_version"),
            "component_cents": {component.name: component.amount_cents for component in unit.components},
        },
    }
