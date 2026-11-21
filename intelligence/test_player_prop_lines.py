"""Unit tests for player_prop_lines.fair_line — formula checks only.

These verify the MATH against hand-computed values (a known series has
a known mean, and the fair line is the half-point at that mean). They
are not product validation: the line maker's behavior on real data is
validated against the harvested game-log store itself, per the
standing rule that no stub or fake data serves as a spec.
"""

import math
import statistics
import unittest

from player_prop_lines import fair_line


class FairLineMath(unittest.TestCase):
    def test_integer_line_is_half_point_at_mean(self):
        values = [22, 28, 25, 31, 24, 27, 29, 23, 26, 25]  # mean 26.0
        out = fair_line(values, "integer")
        self.assertIsNotNone(out)
        self.assertEqual(out["line"], 26.5)  # floor(26.0) + 0.5
        self.assertEqual(out["games_used"], 10)
        # p_over at the fair line sits near 0.5 by construction.
        self.assertAlmostEqual(out["probability_over"], 0.5, delta=0.08)
        self.assertAlmostEqual(
            out["probability_over"] + out["probability_under"], 1.0)

    def test_probability_matches_projection_formula(self):
        values = [10, 14, 12, 18, 11, 13, 15, 12, 14, 11]
        mu = statistics.fmean(values)
        sigma = statistics.stdev(values)
        out = fair_line(values, "integer")
        line = math.floor(mu) + 0.5
        from player_props_projection import normal_cdf
        expected = 1.0 - normal_cdf((line - mu) / sigma)
        self.assertEqual(out["line"], line)
        self.assertAlmostEqual(out["probability_over"], round(expected, 4))

    def test_insufficient_games_no_line(self):
        self.assertIsNone(fair_line([30, 28], "integer"))

    def test_constant_series_is_a_step(self):
        out = fair_line([20, 20, 20, 20], "integer")
        self.assertEqual(out["line"], 20.5)
        self.assertEqual(out["probability_over"], 0.0)


if __name__ == "__main__":
    unittest.main()
