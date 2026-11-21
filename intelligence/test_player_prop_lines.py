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
