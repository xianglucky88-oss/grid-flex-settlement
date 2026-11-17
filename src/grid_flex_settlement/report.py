"""Stable output envelope and serialization.

The report is designed to be diffable and explainable:

* deterministic ordering everywhere (units by key, components by fixed order,
  lines by interval);
* money as both integer cents (exact) and a fixed two-decimal string;
* every line carries the interval, rule version, rate version, baseline
  version, instruction version, and its allocation residual;
* component and batch sums reconcile, so a single cent can be traced from a
  meter interval to the final number.
"""

from __future__ import annotations

from typing import Any, Optional

from .decimalutil import format_decimal, format_money
from .models import (
    BatchResult,
    ComponentResult,
    LineItem,
    PipelineResult,
    UnitResult,
)
from .timeutil import Interval, format_instant

PROJECT = "grid-flex-settlement"


def build_result(project: str, data: dict) -> dict:
    envelope = PipelineResult(project=project, data=data)
    return {"project": envelope.project, "status": "ok", **envelope.data}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _interval_dict(interval: Optional[Interval], timezone_name: Optional[str] = None) -> Optional[dict[str, Any]]:
    if interval is None:
        return None
    return interval.as_dict(timezone_name=timezone_name)


def _line_dict(line: LineItem, timezone_name: Optional[str]) -> dict[str, Any]:
    return {
        "line_id": line.line_id,
        "component": line.component,
        "interval": _interval_dict(line.interval, timezone_name),
        "utc_interval": [format_instant(line.interval.start), format_instant(line.interval.end)],
        "quantity_kwh": format_decimal(line.quantity_kwh, places=9),
        "unit_rate": format_decimal(line.unit_rate, places=9),
        "amount_exact": format_decimal(line.amount_exact, places=10),
        "amount_cents": line.amount_cents,
        "amount": format_money(line.amount_cents),
        "allocation_residual": line.allocation_residual,
        "direction": line.direction,
        "capacity_kw": format_decimal(line.capacity_kw),
        "baseline_kw": format_decimal(line.baseline_kw),
        "actual_kw": format_decimal(line.actual_kw),
        "delivered_kw": format_decimal(line.delivered_kw),
        "executed_kw": format_decimal(line.executed_kw) if line.executed_kw is not None else None,
        "rate_version": line.rate_version,
        "baseline_version": line.baseline_version,
        "instruction_version": line.instruction_version,
        "rule_version": line.rule_version,
        "source_reading_ids": list(line.source_reading_ids),
        "flags": list(line.flags),
    }


def _component_dict(component: ComponentResult, timezone_name: Optional[str]) -> dict[str, Any]:
    return {
        "name": component.name,
        "amount_exact": format_decimal(component.amount_exact, places=10),
        "amount_cents": component.amount_cents,
        "amount": format_money(component.amount_cents),
        "line_count": len(component.lines),
        "allocation_reconciles": sum(line.amount_cents for line in component.lines) == component.amount_cents,
        "explanation": component.explanation,
        "lines": [_line_dict(line, timezone_name) for line in component.lines],
    }


def unit_to_dict(unit: UnitResult) -> dict[str, Any]:
    timezone_name = _unit_timezone(unit)
    return {
        "key": unit.key,
        "event_id": unit.event_id,
        "resource_id": unit.resource_id,
        "status": unit.status,
        "currency": unit.currency,
        "windows": {
            "eligibility": _interval_dict(unit.eligibility_window, timezone_name),
            "event": _interval_dict(unit.event_window, timezone_name),
            "settlement": _interval_dict(unit.settlement_window, timezone_name),
        },
        "baseline": unit.baseline,
        "rate": unit.rate,
        "instruction": unit.instruction,
        "metrics": unit.metrics,
        "components": [_component_dict(component, timezone_name) for component in unit.components],
        "final_cents": unit.final_cents,
        "final_amount": unit.final_amount,
        "errors": list(unit.errors),
        "warnings": list(unit.warnings),
        "bill": unit.bill,
        "revision": unit.revision,
        "audit": _audit_dict(unit.audit),
    }


def _audit_dict(audit: dict[str, Any]) -> dict[str, Any]:
    """Render datetimes as ISO strings without touching the rest."""

    def render(value: Any) -> Any:
        from datetime import datetime

        if isinstance(value, datetime):
            return format_instant(value)
        if isinstance(value, dict):
            return {key: render(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [render(item) for item in value]
        return value

    return render(audit)


def _unit_timezone(unit: UnitResult) -> Optional[str]:
    """Local timezone used for display, taken from the event window provenance."""

    windows = unit.audit.get("windows")
    if isinstance(windows, dict):
        event = windows.get("event")
        if isinstance(event, dict) and event.get("timezone"):
            return str(event["timezone"])
    timezone_name = unit.audit.get("timezone")
    return str(timezone_name) if isinstance(timezone_name, str) else None


def batch_to_dict(batch: BatchResult) -> dict[str, Any]:
    totals = dict(batch.totals)
    units = [unit_to_dict(unit) for unit in sorted(batch.units, key=lambda item: item.key)]
    return {
        "project": PROJECT,
        "status": batch.status,
        "totals": totals,
        "batch_errors": list(batch.errors),
        "audit": _audit_dict(batch.audit),
        "units": units,
    }


def unit_from_dict(data: dict[str, Any]) -> UnitResult:
    """Rehydrate a unit from :func:`unit_to_dict` for checkpoint resume."""

    from .decimalutil import dec
    from .models import ComponentResult, LineItem
    from .timeutil import parse_timestamp

    def interval(value: Any) -> Optional[Interval]:
        if not value:
            return None
        start = parse_timestamp(value["start"])
        end = parse_timestamp(value["end"])
        return Interval(start, end)

    components: list[ComponentResult] = []
    for raw in data.get("components", []) or []:
        lines: list[LineItem] = []
        for item in raw.get("lines", []) or []:
            lines.append(
                LineItem(
                    line_id=item["line_id"],
                    component=item["component"],
                    interval=interval(item["interval"]),
                    quantity_kwh=dec(item["quantity_kwh"]),
                    unit_rate=dec(item["unit_rate"]),
                    amount_exact=dec(item["amount_exact"]),
                    amount_cents=int(item["amount_cents"]),
                    allocation_residual=item.get("allocation_residual", "0"),
                    direction=item.get("direction", "reduce"),
                    capacity_kw=dec(item.get("capacity_kw", "0")),
                    baseline_kw=dec(item.get("baseline_kw", "0")),
                    actual_kw=dec(item.get("actual_kw", "0")),
                    delivered_kw=dec(item.get("delivered_kw", "0")),
                    executed_kw=dec(item["executed_kw"]) if item.get("executed_kw") is not None else None,
                    rate_version=item.get("rate_version", ""),
                    baseline_version=item.get("baseline_version"),
                    instruction_version=item.get("instruction_version"),
                    rule_version=item.get("rule_version", ""),
                    source_reading_ids=tuple(item.get("source_reading_ids", [])),
                    flags=tuple(item.get("flags", [])),
                )
            )
        components.append(
            ComponentResult(
                name=raw["name"],
                amount_exact=dec(raw["amount_exact"]),
                amount_cents=int(raw["amount_cents"]),
                lines=tuple(lines),
                explanation=raw.get("explanation", ""),
            )
        )

    windows = data.get("windows", {}) or {}
    return UnitResult(
        key=data["key"],
        event_id=data.get("event_id", ""),
        resource_id=data.get("resource_id", ""),
        status=data.get("status", "settled"),
        currency=data.get("currency", "USD"),
        settlement_window=interval(windows.get("settlement")),
        event_window=interval(windows.get("event")),
        eligibility_window=interval(windows.get("eligibility")),
        baseline=data.get("baseline", {}) or {},
        rate=data.get("rate", {}) or {},
        instruction=data.get("instruction", {}) or {},
        metrics=data.get("metrics", {}) or {},
        components=tuple(components),
        final_cents=int(data.get("final_cents", 0)),
        errors=tuple(data.get("errors", []) or []),
        warnings=tuple(data.get("warnings", []) or []),
        audit=data.get("audit", {}) or {},
        bill=data.get("bill"),
        revision=data.get("revision"),
    )


# ---------------------------------------------------------------------------
# Reconciliation report
# ---------------------------------------------------------------------------


def reconcile(batch: BatchResult) -> dict[str, Any]:
    """Prove that every cent in every total is attributable to a line."""

    issues: list[dict[str, Any]] = []
    for unit in batch.units:
        if unit.status == "error":
            if unit.final_cents != 0:
                issues.append({"unit": unit.key, "issue": "error unit has non-zero amount"})
            continue
        component_sum = sum(component.amount_cents for component in unit.components)
        if component_sum != unit.final_cents:
            issues.append(
                {
                    "unit": unit.key,
                    "issue": "component cents do not sum to unit total",
                    "component_cents": component_sum,
                    "unit_cents": unit.final_cents,
                }
            )
        for component in unit.components:
            line_sum = sum(line.amount_cents for line in component.lines)
            if line_sum != component.amount_cents:
                issues.append(
                    {
                        "unit": unit.key,
                        "component": component.name,
                        "issue": "line cents do not sum to component total",
                        "line_cents": line_sum,
                        "component_cents": component.amount_cents,
                    }
                )
    return {
        "balanced": not issues,
        "issues": issues,
        "checked_units": len(batch.units),
        "checked_components": sum(len(unit.components) for unit in batch.units),
        "checked_lines": sum(len(component.lines) for unit in batch.units for component in unit.components),
    }
