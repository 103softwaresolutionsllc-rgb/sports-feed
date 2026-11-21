"""Unit tests for props_feed_mlb — Route 2 to the MLB props store
(Nov 21, 2026): the direct MLB Stats API harvest.

Runs against the same frozen REAL responses as the collector tests
(the league's actual 2026 game logs for Aaron Judge, Paul Skenes,
Shohei Ohtani in both groups, an empty log for Luke Adams, the 2026
teams payload, and an excerpt of the real discovery payload) served
through a stub fetch. The feed's distinct obligations are what is
pinned here: it writes the PROPS store (not a collected layer), it
registers players WITH position, derived stats are applied, the raw
landing is append-only under raw/props_feed/mlb/, and a re-run over
identical data is a pure verification pass that does not rewrite
the store.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import gapfill_stats_mlb as gf
import props_feed_mlb as feed
from player_registry import PlayerRegistry

FIXTURES = Path(__file__).parent / "test_fixtures"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

JUDGE, SKENES, OHTANI, ADAMS = 592450, 694973, 660271, 702726
EXPECTED_POSITIONS = {JUDGE: "RF", SKENES: "P", OHTANI: "TWP",
                      ADAMS: "3B"}


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str):
    return json.loads(_fixture_bytes(name))


def fixture_fetch():
    routes = {
        gf.players_url(2026): _fixture_bytes(
            "mlb_players_2026_excerpt.json"),
        gf.teams_url(2026): _fixture_bytes("mlb_teams_2026.json"),
    }
    for pid, group in ((JUDGE, "hitting"), (SKENES, "pitching"),
                       (OHTANI, "hitting"), (OHTANI, "pitching"),
                       (ADAMS, "hitting")):
        routes[gf.gamelog_url(pid, 2026, group)] = _fixture_bytes(
            f"mlb_gamelog_{pid}_{group}_2026.json")

    def fetch(url: str) -> bytes:
        if url in routes:
            return routes[url]
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


def _regular_ids(pid: int, group: str) -> set[str]:
    payload = _fixture_json(f"mlb_gamelog_{pid}_{group}_2026.json")
    ids = set()
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            if split.get("gameType") in (None, "R"):
                ids.add(str(split["game"]["gamePk"]))
    return ids


class FeedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.report = feed.run(self.root, season=2026, now=NOW,
                               fetch=fixture_fetch())

    def tearDown(self):
        self.tmp.cleanup()

    def _registry(self) -> PlayerRegistry:
        return PlayerRegistry.load(
            self.root / "props" / "player_registry.json")

    def _store(self) -> dict:
        return json.loads(
            (self.root / "props" / "game_logs_MLB.json").read_text())

    def test_run_ok_and_store_created(self):
        self.assertEqual(self.report["status"], "ok")
        self.assertFalse(self.report["partial"])
        self.assertEqual(self.report["staged_unkeyed"], 0)
        store = self._store()
        self.assertEqual(store["league"], "MLB")
        self.assertEqual(store["provenance"]["origin"],
                         "direct_harvest_props_feed")
        self.assertEqual(store["coverage"]["source"],
                         "mlb-stats-api-props-feed")

    def test_game_counts_match_real_fixtures(self):
        registry = self._registry()
        store = self._store()
        expected = {
            JUDGE: len(_regular_ids(JUDGE, "hitting")),
            SKENES: len(_regular_ids(SKENES, "pitching")),
            OHTANI: len(_regular_ids(OHTANI, "hitting")
                        | _regular_ids(OHTANI, "pitching")),
            ADAMS: 0,
        }
        for mlb_id, count in expected.items():
            player = registry.get_by_provider_id("mlb", mlb_id)
            self.assertIsNotNone(player, mlb_id)
            games = store["players"][player["player_id"]]["games"]
            self.assertEqual(len(games), count, mlb_id)
        self.assertEqual(self.report["games_added"],
                         sum(expected.values()))
        self.assertEqual(self.report["games_corrected"], 0)

    def test_positions_registered_from_api_typing(self):
        registry = self._registry()
        for mlb_id, abbr in EXPECTED_POSITIONS.items():
            player = registry.get_by_provider_id("mlb", mlb_id)
            self.assertEqual(player["position"], abbr, mlb_id)
        self.assertEqual(self.report["position_changes"], 0)
        self.assertEqual(self.report["registered_new"], 4)

    def test_derived_stat_applied(self):
        registry = self._registry()
        store = self._store()
        judge = registry.get_by_provider_id("mlb", JUDGE)
        games = store["players"][judge["player_id"]]["games"]
        self.assertTrue(games)
        for game in games:
            stats = game["stats"]
            self.assertEqual(
                stats["hits_runs_rbi"],
                float(stats["hits"] + stats["runs"] + stats["rbi"]))

    def test_raw_landing_append_only_under_props_feed(self):
        landing = self.root / "raw" / "props_feed" / "mlb" / "2026-11-21"
        self.assertTrue(
            (landing / "players_season2026.json").exists())
        self.assertTrue(
            (landing / "people" / f"{OHTANI}_pitching.json").exists())

    def test_rerun_is_pure_verification(self):
        before = (self.root / "props" /
                  "game_logs_MLB.json").read_bytes()
        again = feed.run(self.root, season=2026, now=NOW,
                         fetch=fixture_fetch())
        self.assertEqual(again["status"], "ok")
        self.assertEqual(again["games_added"], 0)
        self.assertEqual(again["games_corrected"], 0)
        self.assertEqual(again["games_unchanged"],
                         self.report["games_added"])
        self.assertFalse(again["store_written"])
        self.assertEqual(
            (self.root / "props" / "game_logs_MLB.json").read_bytes(),
            before)


if __name__ == "__main__":
    unittest.main()
