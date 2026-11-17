"""CLI contract: deterministic JSON, legacy and event-level fixtures."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from grid_flex_settlement import cli
from grid_flex_settlement.canon import canonical_json

ROOT = Path(__file__).parents[1]


def _run_cli(path: Path) -> dict:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        argv = ["grid-flex-settlement", str(path)]
        original = cli.argparse.ArgumentParser.parse_args

        def fake_parse(self, args=None, namespace=None):
            return original(self, argv[1:], namespace)

        cli.argparse.ArgumentParser.parse_args = fake_parse
        try:
            cli.main()
        finally:
            cli.argparse.ArgumentParser.parse_args = original
    return json.loads(buffer.getvalue())


class TestCli(unittest.TestCase):
    def test_legacy_fixture_keeps_original_amount(self):
        result = _run_cli(ROOT / "fixtures" / "input.json")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["settlement_amount"], 195.0)
        self.assertEqual(result["project"], "grid-flex-settlement")

    def test_event_level_fixture_is_auditable(self):
        result = _run_cli(ROOT / "fixtures" / "event_level.json")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["reconciliation"]["balanced"])
        unit = result["units"][0]
        self.assertEqual(unit["metrics"]["window_hours"], "25")
        self.assertEqual(unit["bill"]["revision"], 2)
        self.assertFalse(unit["bill"]["supersedes"]["rewritten"])
        self.assertEqual(
            {component["name"] for component in unit["components"]},
            {"capacity", "performance"},
        )

    def test_cli_output_is_stable(self):
        first = _run_cli(ROOT / "fixtures" / "event_level.json")
        second = _run_cli(ROOT / "fixtures" / "event_level.json")
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_cli_accepts_a_path_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.json"
            target.write_text((ROOT / "fixtures" / "event_level.json").read_text())
            result = _run_cli(target)
            self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
