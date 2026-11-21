"""Unit tests for gapfill_stats_mlb — the Sage Gap-Filler's Phase 2
MLB collector (source S3, MLB Stats API).

The parser and run tests run against REAL recorded responses
(Phase 0 fixture discipline from the Nov 16 spec): the league's
actual 2026 game logs for Aaron Judge, Paul Skenes, and Shohei
Ohtani (both groups), an empty game log for a rostered player who
never appeared (Luke Adams), the 2026 teams payload, and an excerpt
of the real 2026 player-discovery payload holding exactly those
four players' unmodified entries, all frozen under test_fixtures/.
Fetch-failure and missing-identity cases use small hand-built
stubs — failure-path checks only, never a validation basis.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gapfill_stats_mlb as gf

FIXTURES = Path(__file__).parent / "test_fixtures"
NOW = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)

JUDGE, SKENES, OHTANI, ADAMS = 592450, 694973, 660271, 702726


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str):
    return json.loads(_fixture_bytes(name))


def fixture_fetch(fail_ids=(), fail_discovery=False):
    """A fetch(url) -> bytes serving the frozen real responses."""
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
        if fail_discovery and url == gf.players_url(2026):
            raise ConnectionError("stubbed discovery failure")
        for pid in fail_ids:
            if f"/people/{pid}/" in url:
                raise ConnectionError("stubbed fetch failure")
        if url in routes:
            return routes[url]
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


def _regular_split_ids(pid: int, group: str) -> set[str]:
    payload = _fixture_json(f"mlb_gamelog_{pid}_{group}_2026.json")
    ids = set()
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            if split.get("gameType") in (None, "R"):
                ids.add(str(split["game"]["gamePk"]))
    return ids


class ParseTests(unittest.TestCase):
    def test_hitting_mapping_and_counts(self):
        payload = _fixture_json("mlb_gamelog_592450_hitting_2026.json")
        games, stats = gf.parse_gamelog(payload, "hitting")
        self.assertEqual(len(games), 66)
        self.assertEqual(stats["splits_seen"], 66)
        raw_first = payload["stats"][0]["splits"][0]
        first = games[0]
        self.assertEqual(first["game_id"],
                         str(raw_first["game"]["gamePk"]))
        self.assertEqual(first["date"], "2026-03-25")
        self.assertEqual(first["team"], "New York Yankees")
        self.assertEqual(first["opponent"], "San Francisco Giants")
        for api_key, canonical in (("hits", "hits"),
                                   ("totalBases", "total_bases"),
                                   ("homeRuns", "home_runs"),
                                   ("rbi", "rbi"),
                                   ("baseOnBalls", "walks"),
                                   ("strikeOuts", "strikeouts"),
                                   ("stolenBases", "stolen_bases")):
            self.assertEqual(first["stats"][canonical],
                             float(raw_first["stat"][api_key]))

    def test_rate_strings_never_carried(self):
        payload = _fixture_json("mlb_gamelog_592450_hitting_2026.json")
        games, _ = gf.parse_gamelog(payload, "hitting")
        for game in games:
            for value in game["stats"].values():
                self.assertIsInstance(value, float)

    def test_pitching_mapping_uses_outs_not_innings_string(self):
        payload = _fixture_json("mlb_gamelog_694973_pitching_2026.json")
        games, _ = gf.parse_gamelog(payload, "pitching")
        self.assertEqual(len(games), 32)
        raw_first = payload["stats"][0]["splits"][0]
        first = games[0]
        # inningsPitched is a baseball-notation string ("6.1" =
        # 6 1/3); the true out count is the `outs` field.
        self.assertEqual(first["stats"]["outs_recorded"],
                         float(raw_first["stat"]["outs"]))
        self.assertEqual(first["stats"]["hits_allowed"],
                         float(raw_first["stat"]["hits"]))
        self.assertEqual(first["stats"]["walks_allowed"],
                         float(raw_first["stat"]["baseOnBalls"]))
        self.assertEqual(first["stats"]["pitching_strikeouts"],
                         float(raw_first["stat"]["strikeOuts"]))
        self.assertEqual(first["stats"]["earned_runs"],
                         float(raw_first["stat"]["earnedRuns"]))
        self.assertIn(first["stats"]["wins"], (0.0, 1.0))

    def test_empty_log_parses_to_zero_games(self):
        payload = _fixture_json("mlb_gamelog_702726_hitting_2026.json")
        games, stats = gf.parse_gamelog(payload, "hitting")
        self.assertEqual(games, [])
        self.assertEqual(stats["splits_seen"], 0)

    def test_two_way_groups(self):
        excerpt = _fixture_json("mlb_players_2026_excerpt.json")
        by_id = {p["id"]: p for p in excerpt["people"]}
        self.assertEqual(gf.groups_for_person(by_id[OHTANI]),
                         ["hitting", "pitching"])
        self.assertEqual(gf.groups_for_person(by_id[SKENES]),
                         ["pitching"])
        self.assertEqual(gf.groups_for_person(by_id[JUDGE]),
                         ["hitting"])

    def test_two_way_merge_one_entry_both_sides(self):
        hit, _ = gf.parse_gamelog(
            _fixture_json("mlb_gamelog_660271_hitting_2026.json"),
            "hitting")
        pit, _ = gf.parse_gamelog(
            _fixture_json("mlb_gamelog_660271_pitching_2026.json"),
            "pitching")
        merged, conflicts = gf.merge_games(
            {"hitting": hit, "pitching": pit})
        self.assertEqual(conflicts, 0)
        expected = _regular_split_ids(OHTANI, "hitting") \
            | _regular_split_ids(OHTANI, "pitching")
        self.assertEqual(len(merged), len(expected))
        both = [g for g in merged if "hits" in g["stats"]
                and "pitching_strikeouts" in g["stats"]]
        self.assertTrue(both, "expected games where Ohtani hit "
                              "and pitched in the same game")


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "intelligence"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **fetch_kwargs):
        return gf.run(self.root, season=2026, now=NOW,
                      fetch=fixture_fetch(**fetch_kwargs))

    def _published_doc(self):
        return json.loads(
            (self.root / "collected" / "game_logs_MLB.json")
            .read_text())

    def test_full_run_publishes_keyed_provenanced_games(self):
        result = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["partial"])
        self.assertEqual(result["players_published"], 4)
        self.assertEqual(result["players_with_games"], 3)
        expected_games = (
            len(_regular_split_ids(JUDGE, "hitting"))
            + len(_regular_split_ids(SKENES, "pitching"))
            + len(_regular_split_ids(OHTANI, "hitting")
                  | _regular_split_ids(OHTANI, "pitching")))
        self.assertEqual(result["games_published"], expected_games)

        doc = self._published_doc()
        self.assertTrue(doc["collected"])
        self.assertEqual(doc["classification"], "Historical")
        expected_expiry = (NOW + timedelta(hours=36)).isoformat()
        for slot in doc["players"].values():
            for entry in slot["games"]:
                self.assertTrue(entry["collected"])
                prov = entry["provenance"]
                for field in ("origin", "source_name", "source_url",
                              "fetched_at", "collector",
                              "classification", "expires_at",
                              "supersedes"):
                    self.assertIn(field, prov)
                self.assertEqual(prov["origin"], "collected")
                self.assertEqual(prov["classification"],
                                 "Historical")
                self.assertEqual(prov["collector"],
                                 "gapfill_stats_mlb")
                self.assertEqual(prov["expires_at"], expected_expiry)

    def test_players_registered_by_provider_id(self):
        self._run()
        registry = json.loads(
            (self.root / "props" / "player_registry.json")
            .read_text())
        by_mlb = {}
        for p in registry["players"]:
            mlb_id = (p.get("provider_ids") or {}).get("mlb")
            if mlb_id is not None:
                by_mlb[mlb_id] = p
        self.assertEqual(set(by_mlb), {JUDGE, SKENES, OHTANI, ADAMS})
        self.assertEqual(by_mlb[JUDGE]["full_name"], "Aaron Judge")
        self.assertEqual(by_mlb[JUDGE]["league"], "MLB")
        self.assertEqual(by_mlb[JUDGE]["team_history"][-1]["team"],
                         "New York Yankees")
        # The rostered player who never appeared is registered
        # with zero published games — keyed, not staged.
        doc = self._published_doc()
        adams_id = by_mlb[ADAMS]["player_id"]
        self.assertEqual(doc["players"][adams_id]["games"], [])

    def test_rerun_is_idempotent(self):
        self._run()
        self._run()
        registry = json.loads(
            (self.root / "props" / "player_registry.json")
            .read_text())
        mlb_players = [p for p in registry["players"]
                       if "mlb" in (p.get("provider_ids") or {})]
        self.assertEqual(len(mlb_players), 4)
        for p in mlb_players:
            self.assertEqual(len(p["team_history"]), 1)

    def test_raw_payloads_land_before_publication(self):
        self._run()
        landing = self.root / "raw" / "collected" / "mlb_game_logs" \
            / "2026-11-21"
        self.assertTrue((landing / "players_season2026.json")
                        .exists())
        self.assertTrue((landing / "people" / "660271_pitching.json")
                        .exists())
        self.assertTrue((landing / "people" / "592450_hitting.json")
                        .exists())

    def test_player_fetch_failure_stages_and_goes_partial(self):
        result = self._run(fail_ids=(SKENES,))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["partial"])
        staging = json.loads(
            (self.root / "collected" / "game_logs_MLB_staging.json")
            .read_text())
        reasons = {(s["mlb_id"], s["staging_reason"])
                   for s in staging["staged"]}
        self.assertIn((SKENES, "fetch_failed"), reasons)
        doc = self._published_doc()
        names = {slot["full_name"] for slot in doc["players"]
                 .values()}
        self.assertNotIn("Paul Skenes", names)
        self.assertIn("Aaron Judge", names)

    def test_discovery_failure_publishes_nothing(self):
        result = self._run(fail_discovery=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_class"], "network")
        self.assertFalse(
            (self.root / "collected" / "game_logs_MLB.json").exists())
        self.assertFalse(
            (self.root / "props" / "player_registry.json").exists())


if __name__ == "__main__":
    unittest.main()
