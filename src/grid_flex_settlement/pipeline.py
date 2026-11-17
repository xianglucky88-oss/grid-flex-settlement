"""Orchestration boundary.

``run`` is the single entry point used by the CLI and tests:

1. parse and validate input inside a timezone-ambiguity collector, so DST
   decisions appear in the audit rather than being silent;
2. resolve versions and settle each event/resource unit in isolation;
3. optionally compare the three baseline designs;
4. reconcile component and line cents, then emit a deterministic report.

Checkpoint support: ``run(payload, checkpoint=...)`` accepts the serialized
output of a previous run.  A unit whose input fingerprint is unchanged is
reused verbatim.  A unit whose inputs changed is recalculated.  A unit that
previously errored is retried.  This makes an interrupted batch resumable
without turning one resource's failure into a batch-wide result.

Rounding-order comparison: ``run(payload, compare_rounding=True)`` reports the
difference between
``round(sum(exact lines))`` (used here) and ``sum(round(exact line))``
(round-per-line), so a one-cent ordering artefact is visible instead of hidden.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Optional

from .canon import fingerprint
from .decimalutil import dec, from_minor
from .engine import compare_designs, settle
from .models import BatchResult, SettlementRequest
from .parser import is_event_level, parse_payload
from .report import batch_to_dict, reconcile, unit_to_dict
from .timeutil import collect_timezone_notes, format_instant


def prepare(payload: dict) -> tuple[SettlementRequest, list[dict[str, Any]]]:
    with collect_timezone_notes() as notes:
        request = parse_payload(payload)
    return request, list(notes)


def settle_payload(payload: dict, *, resume: Optional[dict[str, Any]] = None) -> BatchResult:
    request, tz_notes = prepare(payload)
    resume_units = _checkpoint_units(resume)
    batch = settle(request, resume_units=resume_units)
    if tz_notes:
        audit = dict(batch.audit)
        audit["timezone"] = tz_notes
        batch = replace(batch, audit=audit)
    return batch


def run(
    payload: dict,
    *,
    include_design_comparison: bool = False,
    compare_rounding: bool = False,
    checkpoint: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    request, tz_notes = prepare(payload)
    batch = settle(request, resume_units=_checkpoint_units(checkpoint))
    if tz_notes:
        audit = dict(batch.audit)
        audit["timezone"] = tz_notes
        batch = replace(batch, audit=audit)

    result = batch_to_dict(batch)
    result["reconciliation"] = reconcile(batch)
    result["input"] = {
        "fingerprint": request.payload_fingerprint,
        "as_of": format_instant(request.as_of),
        "schema": "event-level" if is_event_level(payload) else "legacy",
        "rule_version": request.rules.rule_version,
    }
    if not is_event_level(payload):
        # Backwards-compatible single-unit summary.  The authoritative,
        # auditable values live under ``units``; these keys exist so the
        # original fixture/API contract keeps working.
        result.update(_legacy_summary(batch))
    if include_design_comparison:
        result["design"] = _design_section(request)
    if compare_rounding:
        result["rounding_comparison"] = _rounding_comparison(batch)
    result["checkpoint"] = _checkpoint_payload(batch, request)
    return result


def _design_section(request: SettlementRequest) -> dict[str, Any]:
    comparison = compare_designs(request)
    return comparison


def _legacy_summary(batch: BatchResult) -> dict[str, Any]:
    if len(batch.units) != 1:
        return {
            "settlement_amount": float(from_minor(sum(unit.final_cents for unit in batch.units))),
            "explanation": {"unit_count": len(batch.units)},
        }
    unit = batch.units[0]
    window_seconds = unit.settlement_window.duration_seconds if unit.settlement_window else 3600
    hours = Decimal(window_seconds) / Decimal(3600) if window_seconds else Decimal(1)
    baseline_kw = dec(unit.metrics.get("baseline_kwh", "0")) / hours
    actual_kw = dec(unit.metrics.get("actual_kwh", "0")) / hours
    delivered_kw = dec(unit.metrics.get("delivered_kwh", "0")) / hours
    return {
        "settlement_amount": float(from_minor(unit.final_cents)),
        "baseline_kw": round(float(baseline_kw), 3),
        "actual_kw": float(actual_kw),
        "delivered_kw": round(float(delivered_kw), 3),
        "explanation": {
            "baseline_rows": unit.baseline.get("samples", 0),
            "rate": unit.audit.get("rates", [{}])[0].get("label") if unit.audit.get("rates") else None,
            "baseline_version": unit.baseline.get("version"),
            "rule_version": unit.audit.get("rule_version"),
            "component_cents": {component.name: component.amount_cents for component in unit.components},
        },
    }


def _rounding_comparison(batch: BatchResult) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    escalated = 0
    de_escalated = 0
    for unit in batch.units:
        component_level = 0
        line_level = 0
        for component in unit.components:
            line_level += sum(_round_half_even(line.amount_exact) for line in component.lines)
            component_level += component.amount_cents
        delta = component_level - line_level
        if delta:
            escalated += 1 if delta > 0 else 0
            de_escalated += 1 if delta < 0 else 0
        rows.append(
            {
                "unit": unit.key,
                "component_rounded_cents": component_level,
                "line_rounded_cents": line_level,
                "delta_cents": delta,
                "used": "component",
            }
        )
    return {
        "policy_used": "round each component once, allocate cents to lines",
        "alternative": "round each line independently",
        "units_with_delta": escalated + de_escalated,
        "rows": sorted(rows, key=lambda row: row["unit"]),
    }


def _round_half_even(value: Decimal) -> int:
    from .decimalutil import to_minor

    return to_minor(value)


def _checkpoint_units(checkpoint: Optional[dict[str, Any]]) -> Optional[dict[str, dict]]:
    if not checkpoint:
        return None
    units = checkpoint.get("units") or []
    restored: dict[str, dict] = {}
    for unit in units:
        if not isinstance(unit, dict) or "key" not in unit:
            continue
        audit = unit.get("audit") or {}
        restored[unit["key"]] = {
            "input_fingerprint": audit.get("input_fingerprint"),
            "unit": unit,
        }
    return restored


def _checkpoint_payload(batch: BatchResult, request: SettlementRequest) -> dict[str, Any]:
    return {
        "payload_fingerprint": request.payload_fingerprint,
        "as_of": format_instant(request.as_of),
        "rule_version": request.rules.rule_version,
        "units": [unit_to_dict(unit) for unit in sorted(batch.units, key=lambda item: item.key)],
    }
