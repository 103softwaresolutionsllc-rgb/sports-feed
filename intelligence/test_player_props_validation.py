"""Phase 1 tests: validation engine + projection gate.

Covers the spec's acceptance criteria adapted to NBA/NFL scope,
including the canonical test case:

    {league: NBA, player_name: "Jose Conseco", market: blocks,
     line: 25.5, direction: under}
        -> REJECTED, PLAYER_LEAGUE_MISMATCH, no projection, no ticket.

Run from the repo root:
    python3 -m unittest discover -s intelligence -p 'test_*.py'
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import player_identification as ident
import player_props_validation as val
from player_registry import PlayerRegistry


def check_status(result, name):
    for c in result["checks"]:
        if c["check"] == name:
            return c["status"]
    raise AssertionError(f"check {name} missing")


class CanonicalCaseTests(unittest.TestCase):
    """The canonical test case from the spec, verbatim."""

    def setUp(self):
        self.registry = PlayerRegistry.load_seed()
        self.selection = {"league": "NBA", "player_name": "Jose Conseco",
                          "market": "blocks", "line": 25.5,
                          "direction": "under"}
        self.result = val.validate_selection(self.selection, self.registry)

    def test_rejected_with_league_mismatch(self):
        r = self.result
        self.assertFalse(r["valid"])
        self.assertEqual(r["validation_status"], "REJECTED")
        self.assertEqual(r["error_code"], "PLAYER_LEAGUE_MISMATCH")
        # The registry's canonical spelling carries the accent; folded,
        # it is the spec's expected "Jose Canseco".
        self.assertEqual(r["matched_player"], "José Canseco")
        self.assertEqual(ident.normalize_name(r["matched_player"]),
                         "jose canseco")
        self.assertEqual(r["league_requested"], "NBA")
        self.assertEqual(r["league_found"], "MLB")
        self.assertEqual(r["career_status"], "RETIRED")
        self.assertTrue(r["suggestion_requires_confirmation"])

    def test_checks_run_in_order_with_spec_outcomes(self):
        r = self.result
        self.assertEqual([c["check"] for c in r["checks"]],
                         ["league", "identity", "league_membership",
                          "player_status", "market", "line", "direction",
                          "availability"])
        self.assertEqual(check_status(r, "league"), "pass")
        self.assertEqual(check_status(r, "identity"), "fail")
        self.assertEqual(check_status(r, "league_membership"), "fail")
        self.assertEqual(check_status(r, "player_status"), "not_reached")
        # A valid market and a valid line do NOT rescue an invalid
        # player — both pass and the selection is still rejected.
        self.assertEqual(check_status(r, "market"), "pass")
        self.assertEqual(check_status(r, "line"), "pass")
        self.assertEqual(check_status(r, "direction"), "pass")
        self.assertEqual(check_status(r, "availability"), "not_reached")

    def test_no_projection_no_ticket(self):
        r = self.result
        self.assertFalse(r["projection_allowed"])
        self.assertFalse(r["ticket_integration_allowed"])
        self.assertFalse(r["ticket_eligible"])
        gate = val.projection_gate(self.selection, r)
        self.assertIsNone(gate["projection"])
        self.assertIsNone(gate["probability"])
        self.assertIsNone(gate["expected_value"])
        self.assertEqual(gate["model_status"], "AWAITING_VALID_PLAYER")
        self.assertFalse(gate["analysis_allowed"])


class AcceptanceCriteriaTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()

    def v(self, **kw):
        return val.validate_selection(kw, self.registry)

    def test_valid_nba_player_blocks_accepts(self):
        r = self.v(league="NBA", player_name="Jayson Tatum",
                   market="blocks", line=0.5, direction="over")
        self.assertTrue(r["valid"])
        self.assertEqual(r["validation_status"], "ACCEPTED")
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["projection_allowed"])
        self.assertTrue(r["ticket_integration_allowed"])
        self.assertEqual(r["market_id"], "nba_player_blocks")

    def test_valid_nfl_player_accepts(self):
        r = self.v(league="NFL", player_name="Patrick Mahomes",
                   market="passing_yards", line=275.5, direction="over")
        self.assertTrue(r["valid"])
        self.assertEqual(r["market_id"], "nfl_player_passing_yards")

    def test_retired_nba_player_rejected_for_current_market(self):
        r = self.v(league="NBA", player_name="Derrick Rose",
                   market="points", line=10.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "PLAYER_RETIRED")

    def test_injured_player_unavailable(self):
        r = self.v(league="NBA", player_name="Fred VanVleet",
                   market="assists", line=6.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "PLAYER_UNAVAILABLE")

    def test_misspelled_name_suggestions_without_substitution(self):
        r = self.v(league="NBA", player_name="Jayson Tatom",
                   market="points", line=25.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "PLAYER_NOT_FOUND")
        self.assertTrue(r["suggestion_requires_confirmation"])
        self.assertEqual(r["suggestions"][0]["full_name"], "Jayson Tatum")
        self.assertFalse(r["ticket_integration_allowed"])
        # ... and the confirmed-suggestion path DOES work: resubmitting
        # with the confirmed player_id validates cleanly.
        pid = r["suggestions"][0]["player_id"]
        r2 = self.v(league="NBA", player_id=pid, market="points",
                    line=25.5, direction="over")
        self.assertTrue(r2["valid"])

    def test_player_not_found(self):
        r = self.v(league="NBA", player_name="Zebedee Nonexistent",
                   market="points", line=10.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "PLAYER_NOT_FOUND")
        self.assertIsNone(r["matched_player"])

    def test_unsupported_market_rejected(self):
        r = self.v(league="NBA", player_name="Jayson Tatum",
                   market="passing_yards", line=100.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "MARKET_UNSUPPORTED")

    def test_phase_later_league_rejected(self):
        r = self.v(league="MLB", player_name="José Canseco",
                   market="hits", line=1.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "LEAGUE_UNSUPPORTED")

    def test_missing_line_rejected(self):
        r = self.v(league="NBA", player_name="Jayson Tatum",
                   market="points", direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "LINE_INVALID")

    def test_negative_and_zero_lines_rejected(self):
        for bad in (-3.5, 0):
            r = self.v(league="NBA", player_name="Jayson Tatum",
                       market="points", line=bad, direction="over")
            self.assertFalse(r["valid"], bad)
            self.assertEqual(r["error_code"], "LINE_INVALID", bad)

    def test_half_point_line_accepts(self):
        r = self.v(league="NBA", player_name="Nikola Jokić",
                   market="rebounds", line=12.5, direction="under")
        self.assertTrue(r["valid"])
        self.assertEqual(r["line"], 12.5)

    def test_invalid_direction_rejected(self):
        r = self.v(league="NBA", player_name="Jayson Tatum",
                   market="points", line=25.5, direction="sideways")
        self.assertFalse(r["valid"])
        self.assertEqual(r["error_code"], "DIRECTION_INVALID")

    def test_missing_odds_analysis_allowed_ev_withheld(self):
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 25.5, "direction": "over"}
        r = val.validate_selection(sel, self.registry)
        self.assertTrue(r["valid"])  # missing odds is NOT an error
        gate = val.projection_gate(sel, r)
        self.assertTrue(gate["analysis_allowed"])
        self.assertIsNone(gate["expected_value"])
        self.assertTrue(gate["ev_withheld"])
        self.assertEqual(gate["ev_withheld_reason"], "missing_odds")
        self.assertEqual(gate["model_status"],
                         "AWAITING_PROJECTION_MODEL")

    def test_valid_odds_do_not_withhold_ev(self):
        sel = {"league": "NBA", "player_name": "Jayson Tatum",
               "market": "points", "line": 25.5, "direction": "over",
               "odds": -110}
        r = val.validate_selection(sel, self.registry)
        self.assertTrue(r["valid"])
        gate = val.projection_gate(sel, r)
        self.assertFalse(gate["ev_withheld"])
        self.assertIsNone(gate["ev_withheld_reason"])

    def test_provider_id_selection_validates(self):
        reg = PlayerRegistry.empty()
        rec = reg.add_player({
            "full_name": "Fixture Guard", "league": "NBA",
            "position": "SG", "provider_ids": {"api-sports": 777},
            "team_history": [{"team": "Fixture City", "from": "2024-10-01",
                              "to": None}],
            "status_history": [{"status": "active", "from": "2024-10-01",
                                "to": None, "note": ""}]})
        r = val.validate_selection(
            {"league": "NBA", "provider": "api-sports", "provider_id": 777,
             "market": "steals", "line": 1.5, "direction": "over"}, reg)
        self.assertTrue(r["valid"])
        self.assertEqual(r["player_id"], rec["player_id"])


if __name__ == "__main__":
    unittest.main()
