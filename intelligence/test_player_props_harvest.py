"""Phase 2 tests: game-log store, player fan-out harvest, registry upsert.

Offline only — fixture payloads are inline and modeled on the shapes
intelligence_normalize tolerates (basketball flat fields; NFL
statistics group dicts). No network calls.

Run from the repo root:
    python3 -m unittest intelligence.test_player_props_harvest -v
"""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import player_props_harvest as harvest
from intelligence_harvest import BudgetLedger, RawLanding
from player_game_logs import GameLogStore
from player_registry import PlayerRegistry

DAY = "2026-11-16"

# ---------------------------------------------------------------------------
# Fixture payloads (labeled fixtures, NOT live API responses).
# Basketball: entries {player:{id,name}, team:{id,name},
# game:{id,date}, points/rebounds/assists/... as flat fields}.
# ---------------------------------------------------------------------------
BASKETBALL_TEAM1 = {
    "response": [
        {"player": {"id": 101, "name": "Fixture Guard"},
         "team": {"id": 1, "name": "Fixture City"},
         "game": {"id": 9001, "date": "2024-11-01"},
         "points": 24, "rebounds": 6, "assists": 8, "steals": 2,
         "blocks": 1, "turnovers": 3},
        {"player": {"id": 102, "name": "Fixture Wing"},
         "team": {"id": 1, "name": "Fixture City"},
         "game": {"id": 9001, "date": "2024-11-01"},
         "points": 12, "rebounds": 11, "assists": 4},
        # Same players, a second game (newer date).
        {"player": {"id": 101, "name": "Fixture Guard"},
         "team": {"id": 1, "name": "Fixture City"},
         "game": {"id": 9002, "date": "2024-11-03"},
         "points": 30, "rebounds": 10, "assists": 11, "steals": 1,
         "blocks": 0, "turnovers": 2},
        {"player": {"id": 102, "name": "Fixture Wing"},
         "team": {"id": 1, "name": "Fixture City"},
         "game": {"id": 9002, "date": "2024-11-03"},
         "points": 8, "rebounds": 5, "assists": 2},
    ]
}

BASKETBALL_TEAM2 = {
    "response": [
        {"player": {"id": 201, "name": "Fixture Center"},
         "team": {"id": 2, "name": "Fixture Town"},
         "game": {"id": 9001, "date": "2024-11-01"},
         "points": 18, "rebounds": 14, "assists": 3, "blocks": 3},
    ]
}

# A row missing assists entirely: derived stats that need assists must
# stay absent (never invented).
BASKETBALL_NO_ASSISTS = {
    "response": [
        {"player": {"id": 301, "name": "Fixture Scorer"},
         "team": {"id": 3, "name": "Fixture Ville"},
         "game": {"id": 9010, "date": "2024-11-05"},
         "points": 20, "rebounds": 7},
    ]
}

# NFL: entries with statistics group dicts
# (statistics.passing / .rushing / .receiving / .defense / .kicking).
NFL_QB = {
    "response": [
        {"player": {"id": 501, "name": "Fixture Passer"},
         "team": {"id": 11, "name": "Fixture Eleven"},
         "game": {"id": 7001, "date": "2024-09-08"},
         "statistics": {
             "passing": {"yards": 280, "completions": 22,
                         "attempts": 33, "touchdowns": 2,
                         "interceptions": 1},
             "rushing": {"yards": 40, "attempts": 5, "longest": 12,
                         "touchdowns": 0},
         }},
        {"player": {"id": 502, "name": "Fixture Kicker"},
         "team": {"id": 11, "name": "Fixture Eleven"},
         "game": {"id": 7001, "date": "2024-09-08"},
         "statistics": {
             "kicking": {"field_goals": 2, "extra_points": 3},
         }},
    ]
}


def _record(provider_id=101, name="Fixture Guard", team="Fixture City",
            status=None, league="NBA"):
    return {"provider_id": provider_id, "full_name": name,
            "team_name": team, "status": status, "league": league}


class UpsertTests(unittest.TestCase):
    def test_new_player_registered_with_permanent_id(self):
        reg = PlayerRegistry.empty()
        result = harvest.upsert_harvested_player(reg, _record(),
                                                 "2024-11-01")
        self.assertIsNone(result["skipped"])
        self.assertEqual(result["changes"], ["registered"])
        player = result["player"]
        self.assertTrue(player["player_id"].startswith("plyr-"))
        self.assertEqual(player["provider_ids"],
                         {"api-sports": 101})
        self.assertEqual(player["league"], "NBA")
        self.assertEqual(reg.current_team(player), "Fixture City")
        self.assertEqual(player["team_history"],
                         [{"team": "Fixture City", "from": "2024-11-01",
                           "to": None}])
        self.assertEqual(reg.current_status(player), "active")
        self.assertEqual(player["status_history"][0]["note"],
                         "Registered from harvested game data.")
        self.assertIs(reg.get_by_provider_id("api-sports", 101), player)

    def test_team_change_preserves_history(self):
        reg = PlayerRegistry.empty()
        player = harvest.upsert_harvested_player(
            reg, _record(), "2024-01-01")["player"]
        before = len(player["team_history"])
        result = harvest.upsert_harvested_player(
            reg, _record(team="New Fixture Team"), "2025-06-01")
        self.assertEqual(result["changes"], ["team_change"])
        self.assertEqual(reg.team_on(player, "2024-06-01"),
                         "Fixture City")
        self.assertEqual(reg.current_team(player), "New Fixture Team")
        self.assertEqual(len(player["team_history"]), before + 1)

    def test_status_change_appends(self):
        reg = PlayerRegistry.empty()
        player = harvest.upsert_harvested_player(
            reg, _record(), "2024-01-01")["player"]
        result = harvest.upsert_harvested_player(
            reg, _record(status="injured"), "2025-06-01")
        self.assertEqual(result["changes"], ["status_change"])
        self.assertEqual(reg.status_on(player, "2025-01-01"), "active")
        self.assertEqual(reg.current_status(player), "injured")
        self.assertEqual(len(player["status_history"]), 2)

    def test_idempotent_reharvest_appends_nothing(self):
        reg = PlayerRegistry.empty()
        player = harvest.upsert_harvested_player(
            reg, _record(), "2024-01-01")["player"]
        team_len = len(player["team_history"])
        status_len = len(player["status_history"])
        result = harvest.upsert_harvested_player(
            reg, _record(), "2024-01-01")
        self.assertEqual(result["changes"], [])
        self.assertEqual(len(player["team_history"]), team_len)
        self.assertEqual(len(player["status_history"]), status_len)

    def test_missing_provider_id_skipped(self):
        reg = PlayerRegistry.empty()
        result = harvest.upsert_harvested_player(
            reg, _record(provider_id=None), "2024-01-01")
        self.assertEqual(result["skipped"], "missing_provider_id")
        self.assertIsNone(result["player"])
        self.assertEqual(reg.all_players(), [])

    def test_same_name_different_provider_id_is_separate_player(self):
        reg = PlayerRegistry.empty()
        first = harvest.upsert_harvested_player(
            reg, _record(provider_id=101), "2024-01-01")["player"]
        second = harvest.upsert_harvested_player(
            reg, _record(provider_id=999), "2024-01-01")["player"]
        self.assertIsNot(first, second)
        self.assertNotEqual(first["player_id"], second["player_id"])
        self.assertEqual(len(reg.all_players()), 2)


class GameLogTests(unittest.TestCase):
    def _entry(self, game_id, date, points, rebounds=6):
        return {"game_id": game_id, "date": date, "season": "2024-2025",
                "team": "Fixture City",
                "stats": {"points": float(points),
                          "rebounds": float(rebounds)}}

    def test_append_dedupe_and_correction(self):
        store = GameLogStore.empty("NBA")
        first = store.append_game("plyr-0001",
                                  self._entry(9001, "2024-11-01", 24))
        self.assertEqual(first, {"added": True, "corrected": False})
        dupe = store.append_game("plyr-0001",
                                 self._entry(9001, "2024-11-01", 24))
        self.assertEqual(dupe, {"added": False, "corrected": False})
        self.assertEqual(store.games_count("plyr-0001"), 1)
        fixed = store.append_game("plyr-0001",
                                  self._entry(9001, "2024-11-01", 26))
        self.assertEqual(fixed, {"added": False, "corrected": True})
        self.assertEqual(store.games_count("plyr-0001"), 1)
        series = store.series("plyr-0001", "points")
        self.assertEqual([s["value"] for s in series], [26.0])

    def test_series_chronological_and_absent_stat_not_zero_filled(self):
        store = GameLogStore.empty("NBA")
        store.append_game("plyr-0001",
                          self._entry(9002, "2024-11-03", 30))
        store.append_game("plyr-0001",
                          self._entry(9001, "2024-11-01", 24))
        store.append_game("plyr-0001", {
            "game_id": 9003, "date": "2024-11-05", "season": "2024-2025",
            "team": "Fixture City", "stats": {"rebounds": 9.0},
        })
        points = store.series("plyr-0001", "points")
        self.assertEqual([s["game_id"] for s in points], [9001, 9002])
        self.assertEqual([s["value"] for s in points], [24.0, 30.0])
        rebounds = store.series("plyr-0001", "rebounds")
        self.assertEqual([s["game_id"] for s in rebounds],
                         [9001, 9002, 9003])
        self.assertEqual(store.games_count("plyr-0001"), 3)
        self.assertEqual(store.games_count("plyr-9999"), 0)

    def test_save_load_round_trip(self):
        store = GameLogStore.empty("NBA")
        store.append_game("plyr-0001",
                          self._entry(9001, "2024-11-01", 24))
        store.set_coverage({"sport": "basketball", "teams_total": 30})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "game_logs_NBA.json"
            store.save(path)
            loaded = GameLogStore.load(path)
        self.assertEqual(loaded.document["league"], "NBA")
        self.assertEqual(loaded.document["classification"],
                         "Historical")
        self.assertEqual(loaded.games_count("plyr-0001"), 1)
        self.assertEqual(loaded.document["coverage"]["teams_total"], 30)


class ParseTests(unittest.TestCase):
    def test_basketball_rows_extract_base_and_derived(self):
        rows = harvest.parse_player_stat_rows(
            "basketball", BASKETBALL_TEAM1, season="2024-2025")
        self.assertEqual(len(rows), 4)
        guard = rows[0]
        self.assertEqual(guard["provider_player_id"], 101)
        self.assertEqual(guard["player_name"], "Fixture Guard")
        self.assertEqual(guard["provider_team_id"], 1)
        self.assertEqual(guard["team_name"], "Fixture City")
        self.assertEqual(guard["game_id"], 9001)
        self.assertEqual(guard["date"], "2024-11-01")
        self.assertEqual(guard["season"], "2024-2025")
        stats = guard["stats"]
        self.assertEqual(stats["points"], 24.0)
        self.assertEqual(stats["rebounds"], 6.0)
        self.assertEqual(stats["assists"], 8.0)
        # Derived: 24 + 6 + 8, computed only from harvested components.
        self.assertEqual(stats["points_rebounds_assists"], 38.0)
        self.assertEqual(stats["points_rebounds"], 30.0)
        self.assertEqual(stats["points_assists"], 32.0)
        self.assertEqual(stats["rebounds_assists"], 14.0)
        # Only points is >= 10 -> no double-double.
        self.assertEqual(stats["double_double"], 0.0)
        self.assertEqual(stats["triple_double"], 0.0)
        # Game 2 for the guard is a triple-double (30 / 10 / 11).
        guard_g2 = rows[2]
        self.assertEqual(guard_g2["stats"]["double_double"], 1.0)
        self.assertEqual(guard_g2["stats"]["triple_double"], 1.0)

    def test_missing_assists_means_no_derived_pra(self):
        rows = harvest.parse_player_stat_rows(
            "basketball", BASKETBALL_NO_ASSISTS, season="2024-2025")
        self.assertEqual(len(rows), 1)
        stats = rows[0]["stats"]
        self.assertEqual(stats["points"], 20.0)
        self.assertEqual(stats["points_rebounds"], 27.0)
        self.assertNotIn("assists", stats)
        self.assertNotIn("points_rebounds_assists", stats)
        self.assertNotIn("points_assists", stats)
        self.assertNotIn("double_double", stats)

    def test_nfl_grouped_rows(self):
        rows = harvest.parse_player_stat_rows(
            "american-football", NFL_QB, season="2024")
        qb = rows[0]
        stats = qb["stats"]
        self.assertEqual(stats["passing_yards"], 280.0)
        # The group "yards" shortcut must not leak into completions.
        self.assertEqual(stats["completions"], 22.0)
        self.assertEqual(stats["passing_attempts"], 33.0)
        self.assertEqual(stats["passing_touchdowns"], 2.0)
        self.assertEqual(stats["passing_interceptions"], 1.0)
        self.assertEqual(stats["rushing_yards"], 40.0)
        self.assertEqual(stats["longest_rush"], 12.0)
        self.assertEqual(stats["passing_rushing_yards"], 320.0)
        self.assertEqual(stats["anytime_touchdown"], 1.0)
        # Defense interceptions must not pick up the QB's thrown pick.
        self.assertNotIn("interceptions", stats)
        kicker = rows[1]
        self.assertEqual(kicker["stats"]["field_goals_made"], 2.0)
        self.assertEqual(kicker["stats"]["extra_points_made"], 3.0)
        # Fallback: 2 * 3 + 3 = 9 kicking points.
        self.assertEqual(kicker["stats"]["kicking_points"], 9.0)

    def test_row_without_player_identity_skipped(self):
        payload = {"response": [
            {"team": {"id": 1, "name": "Fixture City"},
             "game": {"id": 1, "date": "2024-11-01"}, "points": 5},
        ]}
        self.assertEqual(
            harvest.parse_player_stat_rows("basketball", payload), [])

    def test_roster_records_latest_row_wins(self):
        rows = [
            {"provider_player_id": 101, "player_name": "Fixture Guard",
             "team_name": "Old Team", "date": "2024-11-01",
             "status": None},
            {"provider_player_id": 101, "player_name": "Fixture Guard",
             "team_name": "New Team", "date": "2025-02-01",
             "status": None},
            {"provider_player_id": 101, "player_name": "Fixture Guard",
             "team_name": "Undated Team", "date": None, "status": None},
        ]
        records = harvest.roster_records_from_rows(rows)
        self.assertEqual(records[101]["team_name"], "New Team")
        self.assertEqual(records[101]["provider_id"], 101)


class BudgetPlanTests(unittest.TestCase):
    def _ledger(self, tmp, used=0, cap=20):
        ledger = BudgetLedger(Path(tmp) / "harvest_state.json",
                              daily_cap=cap)
        if used:
            ledger.spend("basketball", used, day=DAY)
        return ledger

    def test_plan_respects_shared_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp, used=9)  # base harvest spent 9
            plan = harvest.plan_fanout_day(
                "basketball", "2024-2025", list(range(1, 31)), ledger,
                day=DAY)
        self.assertEqual(plan["requests_used"], 9)
        self.assertEqual(plan["requests_available"], 11)
        self.assertEqual(len(plan["teams_planned"]), 11)
        self.assertEqual(plan["requests_planned"], 11)
        self.assertEqual(plan["teams_deferred"], 19)
        self.assertFalse(plan["fits_in_cap"])

    def test_plan_fits_when_budget_covers_all_teams(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            plan = harvest.plan_fanout_day(
                "basketball", "2024-2025", [1, 2, 3, 4, 5], ledger,
                day=DAY)
        self.assertEqual(len(plan["teams_planned"]), 5)
        self.assertEqual(plan["teams_deferred"], 0)
        self.assertTrue(plan["fits_in_cap"])

    def test_plan_priority_teams_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            plan = harvest.plan_fanout_day(
                "basketball", "2024-2025", [1, 2, 3, 4, 5], ledger,
                priority_team_ids=(4, 2), day=DAY)
        self.assertEqual(plan["teams_planned"][:2], [4, 2])
        self.assertEqual(plan["teams_planned"][2:], [1, 3, 5])

    def test_plan_is_pure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp, used=9)
            state = harvest.FanoutState(Path(tmp) / "fanout_state.json")
            harvest.plan_fanout_day(
                "basketball", "2024-2025", [1, 2, 3], ledger,
                state=state, day=DAY)
            self.assertEqual(ledger.used("basketball", DAY), 9)
            self.assertEqual(
                state.covered_team_ids("basketball", "2024-2025"), [])
            self.assertFalse(
                (Path(tmp) / "fanout_state.json").exists())


class FanoutStateTests(unittest.TestCase):
    def test_next_batch_rotates_and_starts_new_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = harvest.FanoutState(Path(tmp) / "fanout_state.json")
            first = state.next_batch("basketball", "2024-2025",
                                     [1, 2, 3], 2)
            self.assertEqual(first, [1, 2])
            second = state.next_batch("basketball", "2024-2025",
                                      [1, 2, 3], 2)
            self.assertEqual(second, [3])
            third = state.next_batch("basketball", "2024-2025",
                                     [1, 2, 3], 2)
            self.assertEqual(third, [1, 2])  # new cycle, from the top
            self.assertEqual(
                state.covered_team_ids("basketball", "2024-2025"),
                [1, 2])

    def test_next_batch_season_change_resets_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = harvest.FanoutState(Path(tmp) / "fanout_state.json")
            state.next_batch("basketball", "2023-2024", [1, 2, 3], 3)
            batch = state.next_batch("basketball", "2024-2025",
                                     [1, 2, 3], 2)
            self.assertEqual(batch, [1, 2])
            self.assertEqual(
                state.covered_team_ids("basketball", "2023-2024"), [])


class RunFanoutTests(unittest.TestCase):
    def _transport(self, calls):
        payloads = {1: BASKETBALL_TEAM1, 2: BASKETBALL_TEAM2}

        def fake_transport(url, params, headers):
            calls.append((url, params, headers))
            return copy.deepcopy(payloads[params["team"]])

        return fake_transport

    def test_run_fanout_end_to_end_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = BudgetLedger(root / "harvest_state.json",
                                  daily_cap=20)
            landing = RawLanding(root)
            registry = PlayerRegistry.empty()
            logs = GameLogStore.empty("NBA")
            calls = []
            report = harvest.run_fanout(
                "basketball", "2024-2025", [1, 2],
                transport=self._transport(calls), ledger=ledger,
                landing=landing, registry=registry, log_store=logs,
                api_key="fixture-key", day=DAY,
                effective_date="2024-11-16")
            self.assertEqual(report["fetched"], 2)
            self.assertEqual(report["planned"], 2)
            self.assertEqual(report["skipped_budget"], 0)
            self.assertEqual(ledger.used("basketball", DAY), 2)
            self.assertEqual(report["budget_used_today"], 2)
            self.assertEqual(len(report["files"]), 2)
            for name in report["files"]:
                self.assertTrue(Path(name).exists())
            self.assertEqual(calls[0][2],
                             {"x-apisports-key": "fixture-key"})
            self.assertEqual(report["players_registered"], 3)
            self.assertEqual(report["games_added"], 5)
            self.assertEqual(report["games_corrected"], 0)
            guard = registry.get_by_provider_id("api-sports", 101)
            self.assertIsNotNone(guard)
            self.assertEqual(registry.current_team(guard),
                             "Fixture City")
            series = logs.series(guard["player_id"], "points")
            self.assertEqual([s["value"] for s in series], [24.0, 30.0])
            pra = logs.series(guard["player_id"],
                              "points_rebounds_assists")
            self.assertEqual([s["value"] for s in pra], [38.0, 51.0])
            self.assertEqual(logs.document["coverage"]["teams_total"],
                             2)
            self.assertEqual(
                logs.document["coverage"]["teams_covered"], [1, 2])

            # Re-run with the same payloads: budget is spent again, but
            # no game is added or corrected and nobody re-registers.
            rerun = harvest.run_fanout(
                "basketball", "2024-2025", [1, 2],
                transport=self._transport([]), ledger=ledger,
                landing=landing, registry=registry, log_store=logs,
                api_key="fixture-key", day=DAY,
                effective_date="2024-11-16")
            self.assertEqual(rerun["games_added"], 0)
            self.assertEqual(rerun["games_corrected"], 0)
            self.assertEqual(rerun["players_registered"], 0)
            self.assertEqual(rerun["players_updated"], 0)
            self.assertEqual(ledger.used("basketball", DAY), 4)

    def test_run_fanout_stops_at_cap_without_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = BudgetLedger(root / "harvest_state.json",
                                  daily_cap=20)
            ledger.spend("basketball", 19, day=DAY)
            payloads = {t: BASKETBALL_TEAM2 for t in (1, 2, 3)}

            def fake_transport(url, params, headers):
                return copy.deepcopy(payloads[params["team"]])

            report = harvest.run_fanout(
                "basketball", "2024-2025", [1, 2, 3],
                transport=fake_transport, ledger=ledger,
                landing=RawLanding(root),
                registry=PlayerRegistry.empty(),
                log_store=GameLogStore.empty("NBA"), day=DAY)
            self.assertEqual(report["fetched"], 1)
            self.assertEqual(report["skipped_budget"], 2)
            self.assertEqual(ledger.used("basketball", DAY), 20)
            self.assertEqual(report["budget_used_today"], 20)


if __name__ == "__main__":
    unittest.main()
