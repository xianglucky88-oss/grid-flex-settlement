"""Shared builders for settlement tests.

Every builder returns a fresh payload dict so individual tests can mutate one
input without leaking state into another test.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Optional

BASE = {
    "schema_version": 2,
    "timezone": "UTC",
    "as_of": "2026-06-03T00:00:00Z",
}


def resource(resource_id: str = "r1", **extra: Any) -> dict:
    return {"resource_id": resource_id, **extra}


def _event_builder(
    event_id: str = "e1",
    *,
    start: str = "2026-06-01T10:00:00Z",
    end: str = "2026-06-01T11:00:00Z",
    timezone: str = "UTC",
    direction: str = "reduce",
    capacity_kw: float | None = None,
    baseline_window: Optional[dict] = None,
    settlement_window: Optional[dict] = None,
    **extra: Any,
) -> dict:
    payload = {
        "event_id": event_id,
        "version": 1,
        "start": start,
        "end": end,
        "timezone": timezone,
        "direction": direction,
        "status": "active",
    }
    if capacity_kw is not None:
        payload["capacity_kw"] = capacity_kw
    if baseline_window is not None:
        payload["baseline_window"] = baseline_window
    if settlement_window is not None:
        payload["settlement_window"] = settlement_window
    payload.update(extra)
    return payload


def _eligibility_builder(
    resource_id: str = "r1",
    *,
    event_id: Optional[str] = "e1",
    start: str = "2026-06-01T00:00:00Z",
    end: str = "2026-06-02T00:00:00Z",
    eligibility_id: str = "el1",
    **extra: Any,
) -> dict:
    payload = {
        "eligibility_id": eligibility_id,
        "resource_id": resource_id,
        "start": start,
        "end": end,
        "status": "active",
    }
    if event_id is not None:
        payload["event_id"] = event_id
    payload.update(extra)
    return payload


def _baseline_builder(
    resource_id: str = "r1",
    *,
    baseline_id: str = "b1",
    event_id: Optional[str] = "e1",
    version: int = 1,
    level_kw: float | str = 100,
    method: str = "mean",
    representation: str = "scalar",
    window: Optional[dict] = None,
    samples: Optional[list] = None,
    issued_at: str = "2026-06-01T08:00:00Z",
    status: str = "active",
    **extra: Any,
) -> dict:
    payload = {
        "baseline_id": baseline_id,
        "resource_id": resource_id,
        "version": version,
        "method": method,
        "representation": representation,
        "status": status,
        "issued_at": issued_at,
    }
    if event_id is not None:
        payload["event_id"] = event_id
    if representation == "scalar":
        payload["level_kw"] = level_kw
        payload["window"] = window or {"start": "2026-06-01T09:00:00Z", "end": "2026-06-01T10:00:00Z"}
    if window is not None:
        payload["window"] = window
    if samples is not None:
        payload["samples"] = samples
    payload.update(extra)
    return payload


def _instruction_builder(
    resource_id: str = "r1",
    *,
    instruction_id: str = "i1",
    event_id: str = "e1",
    capacity_kw: float | str = 50,
    start: str = "2026-06-01T10:00:00Z",
    end: str = "2026-06-01T11:00:00Z",
    issued_at: str = "2026-06-01T08:00:00Z",
    version: int = 1,
    direction: str = "reduce",
    status: str = "active",
    **extra: Any,
) -> dict:
    payload = {
        "instruction_id": instruction_id,
        "event_id": event_id,
        "resource_id": resource_id,
        "version": version,
        "issued_at": issued_at,
        "start": start,
        "end": end,
        "capacity_kw": capacity_kw,
        "direction": direction,
        "status": status,
    }
    payload.update(extra)
    return payload


def _reading_builder(
    resource_id: str = "r1",
    *,
    reading_id: str = "m1",
    start: str = "2026-06-01T10:00:00Z",
    end: str = "2026-06-01T11:00:00Z",
    kw: float | str = 60,
    received_at: Optional[str] = None,
    version: int = 1,
    **extra: Any,
) -> dict:
    payload = {
        "reading_id": reading_id,
        "resource_id": resource_id,
        "start": start,
        "end": end,
        "kw": kw,
        "version": version,
    }
    if received_at is not None:
        payload["received_at"] = received_at
    payload.update(extra)
    return payload


def _rate_builder(
    *,
    rate_version_id: str = "rt1",
    version: int = 1,
    effective_from: str = "2026-01-01T00:00:00Z",
    effective_to: Optional[str] = None,
    capacity_rate_per_kw_hour: float | str = "0.5",
    energy_rate_per_kwh: float | str = "0.25",
    deviation_rate_per_kwh: float | str = "0.1",
    currency: str = "USD",
    direction: str = "both",
    issued_at: str = "2025-12-01T00:00:00Z",
    status: str = "active",
    priority: int = 0,
    **extra: Any,
) -> dict:
    payload = {
        "rate_version_id": rate_version_id,
        "version": version,
        "effective_from": effective_from,
        "capacity_rate_per_kw_hour": capacity_rate_per_kw_hour,
        "energy_rate_per_kwh": energy_rate_per_kwh,
        "deviation_rate_per_kwh": deviation_rate_per_kwh,
        "currency": currency,
        "direction": direction,
        "issued_at": issued_at,
        "status": status,
        "priority": priority,
    }
    if effective_to is not None:
        payload["effective_to"] = effective_to
    payload.update(extra)
    return payload


def _correction_builder(
    *,
    correction_id: str = "c1",
    event_id: str = "e1",
    version: int = 1,
    issued_at: str = "2026-06-02T00:00:00Z",
    status: str = "active",
    changes: Optional[dict] = None,
    supersedes: Optional[dict] = None,
) -> dict:
    payload = {
        "correction_id": correction_id,
        "event_id": event_id,
        "version": version,
        "issued_at": issued_at,
        "status": status,
        "changes": changes or {},
    }
    if supersedes is not None:
        payload["supersedes"] = supersedes
    return payload


def _prior_bill_builder(
    *,
    bill_id: str = "bill-1",
    event_id: str = "e1",
    resource_id: str = "r1",
    revision: int = 1,
    status: str = "locked",
    amount_cents: int = 1000,
) -> dict:
    return {
        "bill_id": bill_id,
        "event_id": event_id,
        "resource_id": resource_id,
        "revision": revision,
        "status": status,
        "amount_cents": amount_cents,
        "currency": "USD",
    }


def payload(
    *,
    events: Optional[list] = None,
    eligibility: Optional[list] = None,  # noqa: A002 - public builder keyword
    baselines: Optional[list] = None,
    instructions: Optional[list] = None,
    readings: Optional[list] = None,
    rates: Optional[list] = None,
    corrections: Optional[list] = None,
    prior_bills: Optional[list] = None,
    rules: Optional[dict] = None,
    executions: Optional[list] = None,
    as_of: Optional[str] = None,
    **extra: Any,
) -> dict:
    body: dict[str, Any] = copy.deepcopy(BASE)
    body["events"] = [_event_builder()] if events is None else events
    body["eligibility"] = [_eligibility_builder()] if eligibility is None else eligibility
    body["baselines"] = [_baseline_builder()] if baselines is None else baselines
    body["instructions"] = [_instruction_builder()] if instructions is None else instructions
    body["readings"] = [_reading_builder()] if readings is None else readings
    body["rates"] = [_rate_builder()] if rates is None else rates
    body["corrections"] = corrections or []
    body["prior_bills"] = prior_bills or []
    if executions is not None:
        body["executions"] = executions
    if rules is not None:
        body["rules"] = rules
    if as_of is not None:
        body["as_of"] = as_of
    body.update(extra)
    return body


def _execution_builder(
    resource_id: str = "r1",
    *,
    sample_id: str = "x1",
    start: str = "2026-06-01T10:00:00Z",
    end: str = "2026-06-01T11:00:00Z",
    kw: float | str = 60,
    instruction_id: Optional[str] = "i1",
    received_at: Optional[str] = None,
    version: int = 1,
) -> dict:
    payload = {
        "sample_id": sample_id,
        "resource_id": resource_id,
        "start": start,
        "end": end,
        "kw": kw,
        "version": version,
    }
    if instruction_id is not None:
        payload["instruction_id"] = instruction_id
    if received_at is not None:
        payload["received_at"] = received_at
    return payload


# Public aliases: ``payload()`` keyword arguments intentionally share these
# names, so the underlying builders are private and re-exported here.
event = _event_builder
eligibility = _eligibility_builder
baseline = _baseline_builder
instruction = _instruction_builder
reading = _reading_builder
rate = _rate_builder
execution = _execution_builder
correction = _correction_builder
prior_bill = _prior_bill_builder
