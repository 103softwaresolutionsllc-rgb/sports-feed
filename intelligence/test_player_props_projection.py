"""Phase 2 tests: Gaussian projection engine.

Offline unittests in the Phase 1 style. The game-log store is faked
in-memory against its contract — series(player_id, statistic_key) ->
[{"game_id", "date", "season", "team", "value"}] chronological
oldest -> newest — and player_game_logs is deliberately NOT imported:
the engine is duck-typed against the contract, and so are these tests.

Run from the repo root:
    python3 -m unittest discover -s intelligence \
        -p 'test_player_props_projection.py'
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import player_props_projection as proj
import player_props_validation as val
from player_registry import PlayerRegistry


class FakeLogStore:
    """In-memory game-log store implementing the series() contract."""

    def __init__(self, data=None):
        # {(player_id, statistic_key): [entry, ...]}
        self._data = dict(data or {})

    def series(self, player_id, statistic_key):
        return list(self._data.get((player_id, statistic_key), []))


def make_series(values, season="2025-26", team="Boston Celtics"):
    """Chronological contract entries for a list of stat values."""
    return [{"game_id": f"g{i + 1:03d}",
             "date": f"2025-11-{i + 1:02d}",
             "season": season,
             "team": team,
             "value": v} for i, v in enumerate(values)]


def seasonal_series(season_values):
    """Entries spanning seasons: [(season, [values...]), ...]."""
    entries = []
    n = 0
    for season, values in season_values:
        for v in values:
            n += 1
            entries.append({"game_id": f"g{n:03d}",
                            "date": f"2025-01-{n:02d}",
                            "season": season,
                            "team": "Boston Celtics",
                            "value": v})
    return entries


TATUM = "plyr-0001"      # Jayson Tatum, active NBA (seed registry)
MAHOMES = "plyr-0007"    # Patrick Mahomes, active NFL (seed registry)


class KnownDistributionTests(unittest.TestCase):
    """Tatum points [20,22,24,26,28]: mu 24, sd sqrt(10) ~= 3.1623."""

    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.store = FakeLogStore(
            {(TATUM, "points"): make_series([20, 22, 24, 26, 28])})
        self.selection = {"league": "NBA", "player_name": "Jayson Tatum",
                          "market": "points", "line": 23.5,
                          "direction": "over"}

    def test_probability_over_matches_gaussian(self):
        r = proj.project_selection(self.selection, self.registry,
                                   self.store)
        self.assertEqual(r["model_status"], "PROJECTED")
        detail = r["projection_detail"]
        self.assertEqual(detail["threshold"], 23.5)
        self.assertEqual(detail["mean"], 24.0)
        self.assertAlmostEqual(detail["stdev"], 3.1623, delta=0.001)
        # z = (23.5 - 24) / 3.1623 = -0.15811 -> P(over) ~= 0.5628.
        self.assertAlmostEqual(detail["probability_over"], 0.5628,
                               delta=0.002)
        self.assertAlmostEqual(r["probability"], 0.5628, delta=0.002)
        self.assertEqual(r["projection"], 24.0)
        self.assertEqual(detail["games_used"], 5)
        self.assertEqual(detail["games_available"], 5)
        self.assertEqual(r["classification"], "Estimated")
        self.assertEqual(r["inputs_classification"], "Historical")

    def test_probability_under_is_complement(self):
        sel = dict(self.selection, direction="under")
        r = proj.project_selection(sel, self.registry, self.store)
        self.assertAlmostEqual(r["probability"], 1 - 0.5628, delta=0.002)
        self.assertAlmostEqual(
            r["projection_detail"]["probability_over"]
            + r["projection_detail"]["probability_under"], 1.0, places=4)


class ZeroVarianceTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.store = FakeLogStore(
            {(TATUM, "points"): make_series([30, 30, 30, 30])})

    def test_step_function_above_line(self):
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 25.5, "direction": "over"}
        r = proj.project_selection(sel, self.registry, self.store)
        self.assertEqual(r["probability"], 1.0)
        self.assertEqual(r["projection_detail"]["stdev"], 0.0)

    def test_step_function_below_line(self):
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 35.5, "direction": "over"}
        r = proj.project_selection(sel, self.registry, self.store)
        self.assertEqual(r["probability"], 0.0)


class ExpectedValueTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.store = FakeLogStore(
            {(TATUM, "points"): make_series([30, 30, 30, 30])})

    def test_ev_math_at_minus_110(self):
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 25.5, "direction": "over",
               "odds": -110}
        r = proj.project_selection(sel, self.registry, self.store)
        self.assertEqual(r["probability"], 1.0)
        self.assertFalse(r["ev_withheld"])
        ev = r["expected_value"]
        self.assertIsNotNone(ev)
        # dec = 1 + 100/110 ~= 1.9091; EV = 1.0 * dec - 1 ~= 0.9091.
        self.assertAlmostEqual(ev["decimal_odds"], 1.9091, delta=0.001)
        self.assertAlmostEqual(ev["implied_probability"], 0.5238,
                               delta=0.001)
        self.assertAlmostEqual(ev["ev_per_unit"], 0.9091, delta=0.001)
        self.assertAlmostEqual(ev["ev_percent"], 90.91, delta=0.1)
        self.assertAlmostEqual(ev["edge"], 1.0 - 0.5238, delta=0.001)

    def test_odds_helpers(self):
        self.assertAlmostEqual(proj.american_to_decimal(150), 2.5)
        self.assertAlmostEqual(proj.american_to_decimal(-200), 1.5)
        self.assertAlmostEqual(proj.american_to_implied(100), 0.5)
        self.assertAlmostEqual(proj.american_to_implied(-100), 0.5)
        self.assertAlmostEqual(proj.normal_cdf(0.0), 0.5)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.store = FakeLogStore(
            {(TATUM, "points"): make_series([20, 22, 24, 26, 28])})
        self.selection = {"league": "NBA", "player_name": "Jayson Tatum",
                          "market": "points", "line": 23.5,
                          "direction": "over"}

    def test_league_entry_applied(self):
        raw = proj.project_selection(self.selection, self.registry,
                                     self.store)
        r = proj.project_selection(
            self.selection, self.registry, self.store,
            calibrations={"NBA": {"shift": 0.02, "shrink": 1.0}})
        cal = r["projection_detail"]["calibration"]
        self.assertEqual(cal["source"], "league")
        self.assertTrue(cal["applied"])
        expected = max(0.001, min(
            0.999, raw["projection_detail"]["probability_over"] + 0.02))
        self.assertAlmostEqual(r["probability"], round(expected, 4),
                               delta=0.0002)

    def test_global_fallback_applied(self):
        raw = proj.project_selection(self.selection, self.registry,
                                     self.store)
        r = proj.project_selection(
            self.selection, self.registry, self.store,
            calibrations={"global": {"shift": -0.01, "shrink": 1.0}})
        cal = r["projection_detail"]["calibration"]
        self.assertEqual(cal["source"], "global_fallback")
        self.assertTrue(cal["applied"])
        self.assertAlmostEqual(
            r["probability"],
            raw["projection_detail"]["probability_over"] - 0.01,
            delta=0.0002)

    def test_no_calibration_is_labeled_identity(self):
        r = proj.project_selection(self.selection, self.registry,
                                   self.store, calibrations=None)
        cal = r["projection_detail"]["calibration"]
        self.assertEqual(cal["source"], "none_identity")
        self.assertFalse(cal["applied"])
        self.assertAlmostEqual(r["probability"], 0.5628, delta=0.002)


class InsufficientDataTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.selection = {"league": "NBA", "player_name": "Jayson Tatum",
                          "market": "points", "line": 23.5,
                          "direction": "over"}

    def test_fewer_than_min_games(self):
        store = FakeLogStore(
            {(TATUM, "points"): make_series([20, 22])})
        r = proj.project_selection(self.selection, self.registry, store)
        self.assertEqual(r["model_status"], "INSUFFICIENT_DATA")
        self.assertIsNone(r["projection"])
        self.assertIsNone(r["probability"])
        self.assertIsNone(r["expected_value"])
        self.assertTrue(r["analysis_allowed"])
        info = r["insufficient_data"]
        self.assertEqual(info["reason"], "fewer_than_min_games")
        self.assertEqual(info["games_available"], 2)
        self.assertEqual(info["games_in_window"], 2)
        self.assertEqual(info["games_required"], proj.MIN_GAMES)
        self.assertEqual(info["statistic_key"], "points")

    def test_no_game_logs(self):
        store = FakeLogStore()
        r = proj.project_selection(self.selection, self.registry, store)
        self.assertEqual(r["model_status"], "INSUFFICIENT_DATA")
        self.assertIsNone(r["projection"])
        self.assertEqual(r["insufficient_data"]["reason"], "no_game_logs")
        self.assertEqual(r["insufficient_data"]["games_available"], 0)


class GateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()

    def test_canonical_conseco_returns_gate_untouched(self):
        selection = {"league": "NBA", "player_name": "Jose Conseco",
                     "market": "blocks", "line": 25.5,
                     "direction": "under"}
        validation = val.validate_selection(selection, self.registry)
        gate = val.projection_gate(selection, validation)
        store = FakeLogStore(
            {(TATUM, "blocks"): make_series([1, 2, 3, 4])})
        r = proj.project_selection(selection, self.registry, store)
        self.assertEqual(r, gate)
        self.assertEqual(r["model_status"], "AWAITING_VALID_PLAYER")
        self.assertIsNone(r["projection"])
        self.assertIsNone(r["probability"])
        self.assertIsNone(r["expected_value"])
        self.assertFalse(r["analysis_allowed"])

    def test_missing_odds_projects_without_ev(self):
        store = FakeLogStore(
            {(TATUM, "points"): make_series([20, 22, 24, 26, 28])})
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 23.5, "direction": "over"}
        r = proj.project_selection(sel, self.registry, store)
        self.assertEqual(r["model_status"], "PROJECTED")
        self.assertIsNotNone(r["projection"])
        self.assertIsNotNone(r["probability"])
        self.assertIsNone(r["expected_value"])
        self.assertTrue(r["ev_withheld"])
        self.assertEqual(r["ev_withheld_reason"], "missing_odds")


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.selection = {"league": "NBA", "player_name": "Jayson Tatum",
                          "market": "points", "line": 23.5,
                          "direction": "over"}

    def test_window_changes_the_distribution(self):
        # First 20 games quiet (10), last 5 hot (30).
        values = [10] * 20 + [30] * 5
        store = FakeLogStore({(TATUM, "points"): make_series(values)})
        r5 = proj.project_selection(self.selection, self.registry, store,
                                    window="L5")
        r20 = proj.project_selection(self.selection, self.registry, store,
                                     window="L20")
        self.assertEqual(r5["projection_detail"]["games_used"], 5)
        self.assertEqual(r20["projection_detail"]["games_used"], 20)
        self.assertEqual(r5["projection_detail"]["mean"], 30.0)
        self.assertNotEqual(r5["projection_detail"]["mean"],
                            r20["projection_detail"]["mean"])

    def test_season_window_uses_latest_season_only(self):
        entries = seasonal_series([("2024-25", [10, 10, 10]),
                                   ("2025-26", [20, 20, 20, 20])])
        store = FakeLogStore({(TATUM, "points"): entries})
        r = proj.project_selection(self.selection, self.registry, store,
                                   window="season")
        detail = r["projection_detail"]
        self.assertEqual(detail["games_used"], 4)
        self.assertEqual(detail["games_available"], 7)
        self.assertEqual(detail["mean"], 20.0)

    def test_unknown_window_raises(self):
        store = FakeLogStore(
            {(TATUM, "points"): make_series([20, 22, 24, 26, 28])})
        with self.assertRaises(ValueError):
            proj.project_selection(self.selection, self.registry, store,
                                   window="L7")


class NflPathTests(unittest.TestCase):
    def test_mahomes_passing_yards_projects(self):
        registry = PlayerRegistry.load_seed()
        store = FakeLogStore({(MAHOMES, "passing_yards"): make_series(
            [250, 270, 290, 310], team="Kansas City Chiefs")})
        sel = {"league": "NFL", "player_name": "Patrick Mahomes",
               "market": "passing_yards", "line": 275.5,
               "direction": "over"}
        r = proj.project_selection(sel, registry, store)
        self.assertEqual(r["model_status"], "PROJECTED")
        detail = r["projection_detail"]
        self.assertEqual(detail["market_id"], "nfl_player_passing_yards")
        self.assertEqual(detail["statistic_key"], "passing_yards")
        self.assertEqual(detail["player_id"], MAHOMES)
        self.assertEqual(detail["mean"], 280.0)


class ContinuityCorrectionTests(unittest.TestCase):
    def test_whole_number_line_threshold_is_half_point_up(self):
        registry = PlayerRegistry.load_seed()
        store = FakeLogStore(
            {(TATUM, "points"): make_series([20, 22, 24, 26, 28])})
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 24, "direction": "over"}
        r = proj.project_selection(sel, registry, store)
        self.assertEqual(r["projection_detail"]["threshold"], 24.5)


class CorrelationTests(unittest.TestCase):
    def test_same_player_different_markets(self):
        sels = [
            {"player_id": TATUM, "market": "points", "game_id": "g1"},
            {"player_id": TATUM, "market": "rebounds", "game_id": "g2"},
        ]
        flags = proj.detect_correlations(sels)
        same_player = [f for f in flags if f["type"] == "same_player"]
        self.assertEqual(len(same_player), 1)
        self.assertEqual(same_player[0]["player"], TATUM)
        self.assertEqual(same_player[0]["selection_indexes"], [0, 1])

    def test_same_player_by_name(self):
        sels = [
            {"player_name": "Jayson Tatum", "market": "points"},
            {"player_name": "jayson tatum", "market": "rebounds"},
        ]
        flags = proj.detect_correlations(sels)
        self.assertEqual([f["type"] for f in flags], ["same_player"])
        self.assertEqual(flags[0]["selection_indexes"], [0, 1])

    def test_same_game_different_players(self):
        sels = [
            {"player_id": TATUM, "market": "points", "game_id": "g1"},
            {"player_id": "plyr-0002", "market": "points",
             "game_id": "g1"},
        ]
        flags = proj.detect_correlations(sels)
        self.assertEqual([f["type"] for f in flags], ["same_game"])
        self.assertEqual(flags[0]["game_id"], "g1")
        self.assertEqual(flags[0]["selection_indexes"], [0, 1])

    def test_same_player_same_game_adds_combined_flag(self):
        sels = [
            {"player_id": TATUM, "market": "points", "game_id": "g1"},
            {"player_id": TATUM, "market": "rebounds", "game_id": "g1"},
        ]
        types = [f["type"] for f in proj.detect_correlations(sels)]
        self.assertIn("same_player", types)
        self.assertIn("same_game", types)
        self.assertIn("same_player_same_game", types)
        combined = [f for f in proj.detect_correlations(sels)
                    if f["type"] == "same_player_same_game"][0]
        self.assertEqual(combined["selection_indexes"], [0, 1])
        self.assertEqual(combined["player"], TATUM)
        self.assertEqual(combined["game_id"], "g1")

    def test_unrelated_selections_no_flags(self):
        sels = [
            {"player_id": TATUM, "market": "points", "game_id": "g1"},
            {"player_id": MAHOMES, "market": "passing_yards",
             "game_id": "g2"},
        ]
        self.assertEqual(proj.detect_correlations(sels), [])


if __name__ == "__main__":
    unittest.main()
