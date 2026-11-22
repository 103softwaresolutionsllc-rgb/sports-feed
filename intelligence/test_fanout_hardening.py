"""Tests for the Nov 22, 2026 fail-loud hardening.

Incident: a normalize over an empty harvest overwrote the 30-team NBA
teams doc with an empty one (commit 61cc59f, Nov 21). The fan-out driver
then iterated zero teams for two nights, reported "complete" in ~2s, and
re-stamped the store's coverage as fresh — masking the outage from the
publish gate's staleness check. These tests pin the three guards:

1. write_normalized refuses to overwrite a non-empty teams doc with an
   empty normalize.
2. The driver fails loud (exit 2, status "failed") on an empty teams
   list, and stamps no coverage.
3. The driver fails loud when teams were attempted but zero athletes
   were fetched.
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intelligence_normalize as norm
import props_fanout_driver as driver


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


class WriteNormalizedGuardTests(unittest.TestCase):
    def test_refuses_to_clobber_nonempty_teams_doc_with_empty(self):
        with TemporaryDirectory() as tmp:
            raw_root = Path(tmp) / "rawroot"  # empty: normalize finds nothing
            out_root = Path(tmp) / "out"
            target = out_root / "normalized" / "basketball" / "teams_2026-2027.json"
            good = {"sport": "basketball", "season": "2026-2027",
                    "teams": [{"team_id": 1, "team": "Atlanta Hawks",
                               "season": "2026-2027"}]}
            _write(target, good)
            with self.assertRaises(ValueError):
                norm.write_normalized("basketball", raw_root, out_root,
                                      "2026-2027")
            self.assertEqual(json.loads(target.read_text()), good)

    def test_writes_when_no_existing_doc(self):
        with TemporaryDirectory() as tmp:
            raw_root = Path(tmp) / "rawroot"
            out_root = Path(tmp) / "out"
            path = norm.write_normalized("basketball", raw_root, out_root,
                                         "2026-2027")
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())["teams"], [])

    def test_players_doc_guard_refuses_empty_clobber(self):
        with TemporaryDirectory() as tmp:
            raw_root = Path(tmp) / "rawroot"  # empty: normalize finds nothing
            out_root = Path(tmp) / "out"
            target = (out_root / "normalized" / "basketball"
                      / "players_2026-2027.json")
            good = {"sport": "basketball", "season": "2026-2027",
                    "players": [{"player_id": 1, "player": "Test Player",
                                 "season": "2026-2027"}]}
            _write(target, good)
            with self.assertRaises(ValueError):
                norm.write_normalized_players("basketball", raw_root,
                                              out_root, "2026-2027")
            self.assertEqual(json.loads(target.read_text()), good)


class DriverGuardTests(unittest.TestCase):
    def _data_root(self, tmp: str, teams: list) -> Path:
        root = Path(tmp) / "intelligence"
        _write(root / "normalized" / "basketball" / "teams_2026-2027.json",
               {"sport": "basketball", "season": "2026-2027", "teams": teams})
        return root

    def _argv(self, root: Path) -> list[str]:
        return ["--sport", "basketball", "--season", "2026-2027",
                "--data-root", str(root), "--force"]

    def test_empty_teams_list_fails_loud_and_stamps_nothing(self):
        with TemporaryDirectory() as tmp:
            root = self._data_root(tmp, [])
            rc = driver.main(self._argv(root))
            self.assertEqual(rc, 2)
            # No coverage stamp was written over the store.
            self.assertFalse((root / "props" / "game_logs_NBA.json").exists())

    def test_zero_athletes_fetched_fails_loud(self):
        with TemporaryDirectory() as tmp:
            root = self._data_root(
                tmp, [{"team_id": 1, "team": "Atlanta Hawks",
                       "season": "2026-2027"}])
            original = driver.run_espn_fanout
            driver.run_espn_fanout = lambda *a, **k: {
                "teams_fetched": 0, "athletes_fetched": 0,
                "players_registered": 0, "players_updated": 0,
                "games_added": 0}
            try:
                rc = driver.main(self._argv(root))
            finally:
                driver.run_espn_fanout = original
            self.assertEqual(rc, 2)

    def test_successful_run_still_completes_and_stamps(self):
        with TemporaryDirectory() as tmp:
            root = self._data_root(
                tmp, [{"team_id": 1, "team": "Atlanta Hawks",
                       "season": "2026-2027"}])
            original = driver.run_espn_fanout
            driver.run_espn_fanout = lambda *a, **k: {
                "teams_fetched": 1, "athletes_fetched": 12,
                "players_registered": 12, "players_updated": 12,
                "games_added": 3}
            try:
                rc = driver.main(self._argv(root))
            finally:
                driver.run_espn_fanout = original
            self.assertEqual(rc, 0)
            store = json.loads(
                (root / "props" / "game_logs_NBA.json").read_text())
            self.assertEqual(store["coverage"]["teams_total"], 1)
            self.assertEqual(store["coverage"]["teams_covered"],
                             ["Atlanta Hawks"])


if __name__ == "__main__":
    unittest.main()
