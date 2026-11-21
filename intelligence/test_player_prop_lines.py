"""Unit tests for player_prop_lines.fair_line — formula checks only.

These verify the MATH against hand-computed values under the v1.1
distribution layer (negative binomial / Poisson for integer stats,
chosen by the 2025-2026 out-of-sample backtest in
distribution_validation.py). They are not product validation: the
line maker's behavior on real data is validated against the harvested
game-log store itself, per the standing rule that no stub or fake
data serves as a spec.
"""

import math
import unittest

from player_prop_lines import (
    build_lines, evaluate_publish_gate, fair_line, store_stale_reason)
from player_props_projection import (
    distribution_p_over, fit_distribution)


class FairLineMath(unittest.TestCase):
    def test_integer_line_is_median_half_point(self):
        values = [22, 28, 25, 31, 24, 27, 29, 23, 26, 25]  # mean 26.0
        out = fair_line(values, "integer")
        self.assertIsNotNone(out)
        # Sample var 7.78 < mean -> Poisson(26). Median-side lines:
        # over 25.5 = 0.5261 (gap .026) beats over 26.5 = 0.4481.
        self.assertEqual(out["line"], 25.5)
        self.assertAlmostEqual(out["probability_over"], 0.5261,
                               delta=0.001)
        self.assertEqual(out["games_used"], 10)
        self.assertAlmostEqual(
            out["probability_over"] + out["probability_under"], 1.0)
        proj = out["player_projection"]
        self.assertEqual(proj["distribution"], "poisson")
        self.assertEqual(proj["mean"], 26.0)
        self.assertEqual(proj["median"], 26.0)

    def test_probability_matches_projection_engine(self):
        values = [10, 14, 12, 18, 11, 13, 15, 12, 14, 11]  # mean 13.0
        out = fair_line(values, "integer")
        # Poisson(13): over 12.5 = 0.5369 is the closest-to-even line.
        self.assertEqual(out["line"], 12.5)
        fit = fit_distribution([float(v) for v in values], "integer")
        expected = distribution_p_over(
            fit, math.floor(out["line"]) + 0.5, "integer")
        self.assertAlmostEqual(out["probability_over"],
                               round(expected, 4), delta=0.0001)

    def test_market_block_stays_unsourced(self):
        out = fair_line([22, 28, 25, 31, 24, 27, 29, 23, 26, 25],
                        "integer")
        market = out["market"]
        self.assertIsNone(market["sportsbook_line"])
        self.assertIsNone(market["sportsbook_odds"])
        self.assertIsNone(market["market_consensus"])
        self.assertEqual(market["status"], "not_sourced")
        self.assertTrue(out["alternates"])

    def test_insufficient_games_no_line(self):
        self.assertIsNone(fair_line([30, 28], "integer"))

    def test_constant_series_fits_poisson(self):
        out = fair_line([20, 20, 20, 20], "integer")
        # Poisson(20): over 19.5 = 0.5297 beats over 20.5 = 0.4409.
        self.assertEqual(out["line"], 19.5)
        self.assertAlmostEqual(out["probability_over"], 0.5297,
                               delta=0.001)


def _games(season: str, n: int, points: float = 20.0) -> list[dict]:
    return [{"season": season, "date": "2026-11-19",
             "stats": {"points": points}} for _ in range(n)]


class PublishGateTests(unittest.TestCase):
    def test_established_player_thin_current_season_withheld(self):
        players = {"p1": {"games": _games("2025-2026", 69)
                          + _games("2026-2027", 2)}}
        withheld = evaluate_publish_gate(players)
        self.assertIn("p1", withheld)
        self.assertEqual(withheld["p1"]["reasons"],
                         ["suspect_current_season"])
        self.assertEqual(withheld["p1"]["current_season_games"], 2)
        self.assertEqual(withheld["p1"]["prior_season_games"], 69)

    def test_healthy_and_rookie_players_clear(self):
        players = {
            "star": {"games": _games("2025-2026", 70)
                     + _games("2026-2027", 15)},
            "rookie": {"games": _games("2026-2027", 2)},
            "sophomore": {"games": _games("2025-2026", 12)
                          + _games("2026-2027", 1)},
        }
        self.assertEqual(evaluate_publish_gate(players), {})

    def test_stale_store_detected(self):
        players = {"p1": {"games": [
            {"season": "2026-2027", "date": "2026-11-01",
             "stats": {"points": 20.0}}]}}
        self.assertEqual(store_stale_reason(players, "2026-11-21"),
                         "stale_store")
        self.assertIsNone(store_stale_reason(players, "2026-11-02"))
        self.assertIsNone(store_stale_reason(players, None))


class LeagueGateTests(unittest.TestCase):
    """The gate's thresholds are calibrated per league (Nov 21, 2026):
    the NBA bar (40 prior games, 2 stale days) both fails to fire and
    misfires on the NFL's 17-game, weekly-rhythm season."""

    def test_threshold_values_pinned(self):
        from player_prop_lines import gate_thresholds
        self.assertEqual(gate_thresholds("NBA"),
                         {"prior_min": 40, "current_max": 3,
                          "stale_days": 2})
        self.assertEqual(gate_thresholds("NFL"),
                         {"prior_min": 14, "current_max": 3,
                          "stale_days": 5})
        # MLB (Nov 21, 2026): calibrated for a completed-season
        # store — see the GATE_THRESHOLDS comment for the revisit
        # obligation when the 2027 season opens.
        self.assertEqual(gate_thresholds("MLB"),
                         {"prior_min": 100, "current_max": 5,
                          "stale_days": 220})
        # An unlisted league falls back to the strictest (NBA) bar.
        self.assertEqual(gate_thresholds("MMA"), gate_thresholds("NBA"))
        self.assertEqual(gate_thresholds(None), gate_thresholds("NBA"))

    def test_mlb_completed_season_store_is_not_stale(self):
        # The real 2026 store shape: newest game Sep 27, 2026 (the
        # regular season's final day), harvest as-of Nov 21, 2026 —
        # 55 days. Fresh under MLB's off-season-tolerant bar, stale
        # under the NBA's near-nightly bar.
        players = {"p1": {"games": [
            {"season": "2026", "date": "2026-09-27",
             "stats": {"hits": 1.0}}]}}
        self.assertIsNone(
            store_stale_reason(players, "2026-11-21", "MLB"))
        self.assertEqual(
            store_stale_reason(players, "2026-11-21", "NBA"),
            "stale_store")
        # 221 days crosses even the MLB bar.
        self.assertEqual(
            store_stale_reason(players, "2027-05-06", "MLB"),
            "stale_store")

    def test_mlb_suspect_rule_needs_a_prior_season(self):
        # Single-season store: every prior count is 0, so the
        # suspect rule cannot fire no matter how thin the record.
        players = {"p": {"games": _games("2026", 2)},
                   "context": {"games": _games("2026", 150)}}
        self.assertEqual(evaluate_publish_gate(players, "MLB"), {})

    def test_mlb_suspect_rule_boundaries(self):
        cases = {
            # (prior, current, withheld under MLB?)
            "at_bar_thin": (100, 5, True),
            "below_bar_thin": (99, 0, False),
            "at_bar_active": (100, 6, False),
            "full_prior_out": (162, 0, True),
        }
        for name, (prior, current, expected) in cases.items():
            players = {"p": {"games": _games("2025", prior)
                             + _games("2026", current)},
                       "context": {"games": _games("2026", 150)}}
            withheld = evaluate_publish_gate(players, "MLB")
            self.assertEqual("p" in withheld, expected, name)
            self.assertNotIn("context", withheld)

    def test_nfl_established_bar_is_one_nfl_season(self):
        # 15 prior games is a near-full NFL season but nowhere near
        # the NBA's 40: withheld under NFL rules, clear under NBA.
        players = {"qb": {"games": _games("2025", 15)
                          + _games("2026", 2)}}
        self.assertIn("qb", evaluate_publish_gate(players, "NFL"))
        self.assertEqual(evaluate_publish_gate(players, "NBA"), {})
        self.assertEqual(evaluate_publish_gate(players), {})

    def test_nfl_boundaries(self):
        cases = {
            # (prior, current, withheld under NFL?)
            "at_bar_thin": (14, 3, True),
            "below_bar_thin": (13, 0, False),
            "at_bar_active": (14, 4, False),
            "full_prior_out": (17, 0, True),
        }
        for name, (prior, current, expected) in cases.items():
            # The gate reads "current season" as the latest season in
            # the STORE, so a context player carries 2026 games (a
            # one-player store with no 2026 games would make 2025 the
            # "current" season and the case meaningless).
            players = {"p": {"games": _games("2025", prior)
                             + _games("2026", current)},
                       "context": {"games": _games("2026", 10)}}
            withheld = evaluate_publish_gate(players, "NFL")
            self.assertEqual("p" in withheld, expected, name)
            self.assertNotIn("context", withheld)
            if expected:
                self.assertEqual(withheld["p"]["prior_season_games"],
                                 prior)
                self.assertEqual(withheld["p"]["current_season_games"],
                                 current)

    def test_stale_window_is_per_league(self):
        # Newest game 4 days behind the as-of date: stale for the
        # near-nightly NBA, fresh for the weekly NFL.
        players = {"p1": {"games": [
            {"season": "2026", "date": "2026-11-16",
             "stats": {"points": 20.0}}]}}
        self.assertEqual(
            store_stale_reason(players, "2026-11-20", "NBA"),
            "stale_store")
        self.assertIsNone(
            store_stale_reason(players, "2026-11-20", "NFL"))
        # 6 days behind is stale even on the NFL's rhythm.
        self.assertEqual(
            store_stale_reason(players, "2026-11-22", "NFL"),
            "stale_store")


class _FakeStore:
    """Minimal GameLogStore stand-in: document + series()."""

    def __init__(self, values_by_player: dict, players_doc: dict):
        self.document = {"players": players_doc}
        self._values = values_by_player

    def series(self, player_id: str, key: str) -> list[dict]:
        if key != "points":
            return []
        return [{"value": v} for v in self._values.get(player_id, [])]


class BuildLinesGateTests(unittest.TestCase):
    def test_withheld_player_publishes_no_lines(self):
        doc_players = {
            "p-clear": {"games": _games("2025-2026", 50)
                        + _games("2026-2027", 14)},
            "p-held": {"games": _games("2025-2026", 69)
                       + _games("2026-2027", 2)},
        }
        store = _FakeStore(
            {"p-clear": [20, 22, 24, 26, 28, 21, 23, 25, 27, 29],
             "p-held": [30, 31, 29, 32, 30, 31, 29, 30, 32, 31]},
            doc_players)
        doc = build_lines("NBA", store, data_as_of="2026-11-21")
        line_players = {l["player_id"] for l in doc["lines"]}
        self.assertIn("p-clear", line_players)
        self.assertNotIn("p-held", line_players)
        held = {w["player_id"] for w in doc["withheld"]}
        self.assertEqual(held, {"p-held"})
        self.assertEqual(doc["gate"]["withheld_players"], 1)
        self.assertEqual(doc["gate"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
