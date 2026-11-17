"""DST transitions, local windows, out-of-order input, and recalculation."""

import copy
import unittest
from datetime import datetime, timezone

from grid_flex_settlement.canon import canonical_json
from grid_flex_settlement.pipeline import run
from grid_flex_settlement.timeutil import (
    Interval,
    TimeError,
    localize_naive,
    parse_timestamp,
    split_in_local_midnight,
)

try:  # pragma: no cover - import shim for both discovery styles
    from . import support
except ImportError:  # running with ``discover -s tests`` and tests/ on sys.path
    import support  # type: ignore[no-redef]


def _unit(result, key="e1/r1"):
    for unit in result["units"]:
        if unit["key"] == key:
            return unit
    raise AssertionError(f"missing unit {key}")


def _component(unit, name):
    for component in unit["components"]:
        if component["name"] == name:
            return component
    return None


class TestDstParsing(unittest.TestCase):
    def test_ambiguous_fall_back_hour_is_deterministic(self):
        naive = datetime(2026, 11, 1, 1, 30)
        instant, note = localize_naive(naive, "America/New_York")
        self.assertEqual(instant, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertTrue(note["dst_ambiguous"])
        self.assertEqual(note["resolution"], "fold=0 (first occurrence)")
        # Repeating the call must not change the answer.
        again, _ = localize_naive(naive, "America/New_York")
        self.assertEqual(instant, again)

    def test_spring_forward_gap_is_rejected(self):
        with self.assertRaises(TimeError):
            localize_naive(datetime(2026, 3, 8, 2, 30), "America/New_York")

    def test_offset_aware_timestamp_ignores_default_zone(self):
        instant = parse_timestamp("2026-11-01T01:30:00-04:00", default_tz="America/New_York")
        self.assertEqual(instant, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))

    def test_split_at_local_midnight_preserves_duration(self):
        window = Interval(
            parse_timestamp("2026-11-01T04:00:00Z"), parse_timestamp("2026-11-02T05:00:00Z")
        )
        pieces = split_in_local_midnight(window, "America/New_York")
        self.assertEqual(sum(piece.duration_seconds for piece in pieces), window.duration_seconds)
        # Midnight local in New York on 2 Nov is 05:00Z (EST).
        self.assertEqual(pieces[-1].end, parse_timestamp("2026-11-02T05:00:00Z"))


class TestDstSettlement(unittest.TestCase):
    def _dst_payload(self, local_start, local_end, tz="America/New_York"):
        return support.payload(
            events=[
                support.event(
                    start=local_start,
                    end=local_end,
                    timezone=tz,
                    baseline_window={
                        "start": "2026-10-31T00:00:00",
                        "end": "2026-11-01T00:00:00",
                    }
                    if tz == "America/New_York" and local_start.startswith("2026-11")
                    else {"start": "2026-03-07T00:00:00", "end": "2026-03-08T00:00:00"},
                )
            ],
            eligibility=[
                support.eligibility(
                    start=local_start,
                    end=(
                        "2026-11-03T00:00:00"
                        if local_start.startswith("2026-11")
                        else "2026-03-10T00:00:00"
                    ),
                    timezone=tz,
                )
            ],
            baselines=[
                support.baseline(
                    level_kw=100,
                    timezone=tz,
                    window=(
                        {"start": "2026-10-31T00:00:00", "end": "2026-11-01T00:00:00"}
                        if local_start.startswith("2026-11")
                        else {"start": "2026-03-07T00:00:00", "end": "2026-03-08T00:00:00"}
                    ),
                )
            ],
            instructions=[
                support.instruction(
                    start=local_start,
                    end=local_end,
                    capacity_kw=10,
                    timezone=tz,
                )
            ],
            readings=[
                support.reading(
                    start=local_start, end=local_end, kw=50, timezone=tz
                )
            ],
            rates=[
                support.rate(
                    capacity_rate_per_kw_hour="1",
                    energy_rate_per_kwh="0",
                    deviation_rate_per_kwh="0",
                )
            ],
            rules={"split_at_local_midnight": True, "shortfall_penalty": False},
        )

    def test_fall_back_day_is_25_hours(self):
        payload = self._dst_payload("2026-11-01T00:00:00", "2026-11-02T00:00:00")
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(unit["metrics"]["window_hours"], "25")
        # 10 kW * 25 h * 1.00 = 250.00
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 25000)
        # The event window in UTC must start at 04:00Z and end at 05:00Z next day.
        self.assertEqual(unit["windows"]["event"]["start"], "2026-11-01T04:00:00Z")
        self.assertEqual(unit["windows"]["event"]["end"], "2026-11-02T05:00:00Z")

    def test_spring_forward_day_is_23_hours(self):
        payload = self._dst_payload("2026-03-08T00:00:00", "2026-03-09T00:00:00")
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "settled")
        self.assertEqual(unit["metrics"]["window_hours"], "23")
        self.assertEqual(_component(unit, "capacity")["amount_cents"], 23000)

    def test_dst_run_is_deterministic(self):
        payload = self._dst_payload("2026-11-01T00:00:00", "2026-11-02T00:00:00")
        first = run(payload)
        second = run(payload)
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_ambiguous_wall_time_is_recorded_in_the_audit(self):
        # 01:30 on the fall-back day occurs twice.  The run must pick one fold,
        # say which, and still be reproducible.
        payload = support.payload(
            events=[
                support.event(
                    start="2026-11-01T01:30:00",
                    end="2026-11-01T03:30:00",
                    timezone="America/New_York",
                )
            ],
            eligibility=[
                support.eligibility(
                    start="2026-11-01T00:00:00",
                    end="2026-11-02T00:00:00",
                    timezone="America/New_York",
                )
            ],
            instructions=[
                support.instruction(
                    start="2026-11-01T01:30:00",
                    end="2026-11-01T03:30:00",
                    timezone="America/New_York",
                )
            ],
            readings=[
                support.reading(
                    start="2026-11-01T01:30:00",
                    end="2026-11-01T03:30:00",
                    kw=60,
                    timezone="America/New_York",
                )
            ],
            rates=[
                support.rate(
                    capacity_rate_per_kw_hour="1",
                    energy_rate_per_kwh="0",
                    deviation_rate_per_kwh="0",
                )
            ],
        )
        first = run(payload)
        notes = first["audit"].get("timezone") or []
        self.assertTrue(notes)
        self.assertTrue(all(note["code"] == "dst_ambiguous" for note in notes))
        self.assertTrue(all(note["resolution"].startswith("fold=0") for note in notes))
        # The chosen instant is the first occurrence: 01:30 EDT = 05:30Z.
        self.assertEqual(_unit(first)["windows"]["event"]["start"], "2026-11-01T05:30:00Z")
        self.assertEqual(canonical_json(first), canonical_json(run(payload)))

    def test_local_midnight_split_creates_two_rate_days(self):
        payload = support.payload(
            events=[
                support.event(
                    start="2026-11-01T00:00:00",
                    end="2026-11-02T00:00:00",
                    timezone="America/New_York",
                )
            ],
            eligibility=[
                support.eligibility(
                    start="2026-11-01T00:00:00", end="2026-11-03T00:00:00", timezone="America/New_York"
                )
            ],
            instructions=[
                support.instruction(
                    start="2026-11-01T00:00:00", end="2026-11-02T00:00:00", timezone="America/New_York"
                )
            ],
            readings=[
                support.reading(
                    start="2026-11-01T00:00:00", end="2026-11-02T00:00:00", kw=60, timezone="America/New_York"
                )
            ],
            baselines=[support.baseline(level_kw=100, timezone="America/New_York")],
            rates=[
                support.rate(
                    rate_version_id="day1",
                    effective_from="2026-11-01T04:00:00Z",
                    effective_to="2026-11-02T05:00:00Z",
                    capacity_rate_per_kw_hour="1",
                )
            ],
            rules={"split_at_local_midnight": True},
        )
        result = run(payload)
        unit = _unit(result)
        capacity = _component(unit, "capacity")
        self.assertEqual(len(capacity["lines"]), 1)
        self.assertEqual(capacity["lines"][0]["interval"]["timezone"], "America/New_York")


class TestOutOfOrderInputs(unittest.TestCase):
    def test_shuffled_records_produce_identical_amounts(self):
        base = support.payload(
            readings=[
                support.reading(reading_id="m1", start="2026-06-01T10:00:00Z", end="2026-06-01T10:30:00Z", kw=60),
                support.reading(reading_id="m2", start="2026-06-01T10:30:00Z", end="2026-06-01T11:00:00Z", kw=70),
            ],
            instructions=[
                support.instruction(instruction_id="i1", issued_at="2026-06-01T08:00:00Z"),
                support.instruction(
                    instruction_id="i1",
                    version=2,
                    issued_at="2026-06-01T09:00:00Z",
                    capacity_kw=100,
                ),
            ],
        )
        forward = run(base)
        shuffled = copy.deepcopy(base)
        shuffled["readings"].reverse()
        shuffled["instructions"].reverse()
        backward = run(shuffled)
        self.assertEqual(forward["totals"], backward["totals"])
        # Version 2 instruction must win regardless of input order.
        self.assertEqual(_component(_unit(forward), "capacity")["amount_cents"], 5000)
        self.assertEqual(_component(_unit(backward), "capacity")["amount_cents"], 5000)

    def test_reading_revision_later_in_feed_wins(self):
        payload = support.payload(
            readings=[
                support.reading(reading_id="m1", kw=50, version=1),
                support.reading(reading_id="m1", kw=60, version=2),
                support.reading(reading_id="m1", kw=45, version=3),
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["metrics"]["actual_kwh"], "45")


class TestPartialExecution(unittest.TestCase):
    def test_partial_execution_credits_only_delivered_energy(self):
        payload = support.payload(readings=[support.reading(kw=80)])
        result = run(payload)
        unit = _unit(result)
        # baseline 100, actual 80 => 20 kW for 1 h at 0.25
        self.assertEqual(_component(unit, "performance")["amount_cents"], 500)
        # shortfall = 50 - 20 = 30 kWh at 0.10
        self.assertEqual(_component(unit, "deviation")["amount_cents"], -300)

    def test_execution_telemetry_becomes_actual_when_meter_is_absent(self):
        payload = support.payload(
            readings=[],
            executions=[
                support.execution(start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z", kw=70)
            ],
            rules={"deviation_basis": "instruction"},
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["metrics"]["actual_source"], "execution_telemetry")
        self.assertEqual(unit["metrics"]["actual_kwh"], "70")
        self.assertEqual(_component(unit, "performance")["amount_cents"], 750)

    def test_deviation_basis_execution_uses_telemetry_reference(self):
        payload = support.payload(
            readings=[support.reading(kw=80)],
            executions=[
                support.execution(start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z", kw=30)
            ],
            rules={"deviation_basis": "execution"},
        )
        result = run(payload)
        unit = _unit(result)
        deviation = _component(unit, "deviation")
        # executed 30, delivered 20 => shortfall 10 kWh * 0.10 = 1.00
        self.assertEqual(deviation["amount_cents"], -100)
        effective = deviation["lines"][0]["flags"]
        self.assertIn("deviation_basis:execution_telemetry", effective)
        # With the instruction basis the shortfall is 50-20 = 30 kWh.
        instruction_basis = run(
            support.payload(
                readings=[support.reading(kw=80)],
                executions=[
                    support.execution(start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z", kw=30)
                ],
                rules={"deviation_basis": "instruction"},
            )
        )
        self.assertEqual(
            _component(_unit(instruction_basis), "deviation")["amount_cents"], -300
        )


class TestRevocation(unittest.TestCase):
    def test_revoked_instruction_blocks_settlement(self):
        payload = support.payload(
            instructions=[
                support.instruction(version=1),
                support.instruction(
                    instruction_id="i1",
                    version=2,
                    status="revoked",
                    issued_at="2026-06-01T09:30:00Z",
                ),
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "error")
        self.assertIn("instruction_missing", {item["code"] for item in unit["errors"]})

    def test_revoked_event_is_reported_not_paid(self):
        payload = support.payload(
            corrections=[
                support.correction(status="withdrawn", issued_at="2026-06-02T00:00:00Z")
            ]
        )
        result = run(payload)
        unit = _unit(result)
        self.assertEqual(unit["status"], "revoked")
        self.assertEqual(unit["final_cents"], 0)
        self.assertIn("reason", unit["audit"])


class TestRecalculation(unittest.TestCase):
    def test_second_recalculation_creates_revision_two(self):
        first = run(support.payload())
        first_bill = _unit(first)["bill"]
        self.assertEqual(first_bill["revision"], 1)

        # Feed the issued bill back as a prior bill and correct the meter data.
        prior = {
            "bill_id": first_bill["bill_id"],
            "event_id": "e1",
            "resource_id": "r1",
            "revision": first_bill["revision"],
            "status": "issued",
            "amount_cents": first_bill["amount_cents"],
            "currency": first_bill["currency"],
        }
        second = run(
            support.payload(
                prior_bills=[prior],
                readings=[support.reading(kw=70)],
                as_of="2026-06-05T00:00:00Z",
            )
        )
        bill = _unit(second)["bill"]
        self.assertEqual(bill["revision"], 2)
        self.assertEqual(bill["supersedes"]["amount_cents"], first_bill["amount_cents"])
        self.assertFalse(bill["supersedes"]["rewritten"])
        # baseline 100, actual 70 => 30 kWh * 0.25 = 7.50
        self.assertEqual(bill["amount_cents"], 750 + 2500 - 200)

    def test_recalculation_is_idempotent(self):
        payload = support.payload(as_of="2026-06-05T00:00:00Z")
        first = run(payload)
        prior = {
            "bill_id": _unit(first)["bill"]["bill_id"],
            "event_id": "e1",
            "resource_id": "r1",
            "revision": 1,
            "status": "draft",
            "amount_cents": _unit(first)["bill"]["amount_cents"],
        }
        once = run(support.payload(prior_bills=[prior], as_of="2026-06-05T00:00:00Z"))
        twice = run(support.payload(prior_bills=[prior], as_of="2026-06-05T00:00:00Z"))
        self.assertEqual(once["totals"], twice["totals"])
        self.assertEqual(
            canonical_json(once["units"]),
            canonical_json(twice["units"]),
        )
