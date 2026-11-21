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

from player_prop_lines import fair_line
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


if __name__ == "__main__":
    unittest.main()
