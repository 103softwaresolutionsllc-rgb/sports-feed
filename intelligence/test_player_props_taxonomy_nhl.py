"""NHL taxonomy tests (adopted Nov 21, 2026).

The NHL market set was adopted in full from the Nov 21, 2026 taxonomy
proposal, grounded in the NHL official web API game logs collected
for the 20262027 season. These tests pin:

  * the taxonomy structure — 8 markets (5 skater, 3 goalie),
    defined Nov 21, 2026 and ACTIVATED the same day once the
    collected layer — promoted under Isaac's D4 promotion ruling
    for the NHL source — was materialized into an NHL props store
    with a prior-season (20252026) harvest behind the publish gate;
  * every carried market key against the REAL collected layer
    (collected/game_logs_NHL.json) — keys must exist in actual
    records, sampled across skaters and goalies, never against
    invented data;
  * the two derivations, computed by the harvest's own _add_derived
    on real records: saves (shots_against - goals_against, both
    components required) and wins (from the decision string, which
    the derivation consumes; absent decision -> absent wins);
  * validation behavior — the league check passes for NHL, defined
    markets pass the market check now that they are active, and
    undefined markets (shutouts, blocked_shots) still reject at the
    market check with MARKET_UNSUPPORTED.

Run from the repo root:
    python3 -m unittest discover -s intelligence -p 'test_*.py'
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import player_props_taxonomy as taxonomy
import player_props_validation as val
from player_props_harvest import _add_derived
from player_registry import PlayerRegistry

COLLECTED_NHL = (Path.home() / "workspace" / "sports-feed-data-wt" /
                 "data" / "intelligence" / "collected" /
                 "game_logs_NHL.json")

SKATER_KEYS = ("goals", "points", "assists", "shots",
               "power_play_points")
GOALIE_CARRIED_KEYS = ("goals_against",)  # + derivation components
DERIVED_KEYS = ("saves", "wins")


def check_status(result, name):
    for c in result["checks"]:
        if c["check"] == name:
            return c["status"]
    raise AssertionError(f"check {name} missing")


class NhlTaxonomyStructureTests(unittest.TestCase):
    def test_league_defined(self):
        self.assertTrue(taxonomy.league_supported("NHL"))
        self.assertEqual(taxonomy.get_league("NHL")["sport"], "hockey")
        self.assertEqual(taxonomy.get_league("NHL")["status"],
                         "phase1")

    def test_eight_markets_in_groups(self):
        markets = taxonomy.markets_for_league("NHL")
        self.assertEqual(len(markets), 8)
        by_cat = {}
        for m in markets:
            by_cat.setdefault(m["category"], []).append(
                m["statistic_key"])
        self.assertEqual(sorted(by_cat["Scoring"]),
                         ["goals", "points"])
        self.assertEqual(by_cat["Playmaking"], ["assists"])
        self.assertEqual(by_cat["Shooting"], ["shots"])
        self.assertEqual(by_cat["Special Teams"],
                         ["power_play_points"])
        self.assertEqual(sorted(by_cat["Goaltending"]),
                         ["goals_against", "saves", "wins"])

    def test_market_record_shape(self):
        for m in taxonomy.markets_for_league("NHL"):
            self.assertEqual(m["market_id"],
                             f"nhl_player_{m['statistic_key']}")
            self.assertEqual(m["league"], "NHL")
            self.assertEqual(m["statistic_type"], "integer")
            self.assertEqual(m["period"], "full_game")
            self.assertEqual(m["directions"], ["over", "under"])
            self.assertTrue(m["supports_alternate_lines"])
            self.assertTrue(m["supports_historical_analysis"])
            self.assertTrue(m["supports_player_projection"])

    def test_all_markets_defined_and_active(self):
        # Activated Nov 21, 2026 (see the taxonomy module): the
        # taxonomy resolves every market and every market is live,
        # so an NHL selection validates at the market check.
        for key in SKATER_KEYS + GOALIE_CARRIED_KEYS + DERIVED_KEYS:
            m = taxonomy.get_market("NHL", key)
            self.assertIsNotNone(m, key)
            self.assertTrue(m["active"], key)
            self.assertTrue(taxonomy.market_supported("NHL", key),
                            key)

    def test_resolution_by_id_and_name(self):
        by_id = taxonomy.get_market("NHL", "nhl_player_shots")
        by_name = taxonomy.get_market("NHL", "Shots on Goal")
        self.assertIs(by_id, by_name)

    def test_undefined_markets_stay_undefined(self):
        # Per the adopted proposal these are NOT defined: shutouts
        # (D2), blocked shots / hits (D4 — not in the source at
        # all), signed/rate/participation stats, subset stats, and
        # toi_seconds (carried as a conditioning stat, not a market).
        for key in ("shutouts", "blocked_shots", "hits",
                    "plus_minus", "pim", "save_pct", "games_started",
                    "power_play_goals", "shorthanded_points",
                    "game_winning_goals", "ot_goals", "shifts",
                    "toi_seconds", "shots_against"):
            self.assertIsNone(taxonomy.get_market("NHL", key), key)
            self.assertFalse(taxonomy.market_supported("NHL", key),
                             key)

    def test_nhl_keys_do_not_leak_into_other_leagues(self):
        self.assertIsNone(taxonomy.get_market("NBA", "shots"))
        self.assertIsNone(taxonomy.get_market("NBA", "saves"))
        self.assertIsNone(taxonomy.get_market("MLB", "power_play_points"))
        self.assertIsNone(taxonomy.get_market("NHL", "hits"))
        self.assertIsNone(taxonomy.get_market("NHL", "points_rebounds"))


@unittest.skipUnless(COLLECTED_NHL.exists(),
                     "collected NHL layer not present in this checkout")
class NhlCollectedLayerKeyTests(unittest.TestCase):
    """Every carried taxonomy key must exist in real collected
    records, and the derivations must compute from real records."""

    @classmethod
    def setUpClass(cls):
        doc = json.loads(COLLECTED_NHL.read_text())
        cls.skater_record = None
        cls.goalie_decided = None    # a goalie game WITH a decision
        cls.goalie_relief = None     # a goalie game WITHOUT one
        for rec in doc["players"].values():
            for game in rec.get("games") or []:
                stats = game.get("stats") or {}
                if (cls.skater_record is None
                        and "power_play_points" in stats):
                    cls.skater_record = stats
                if "shots_against" in stats:
                    if ("decision" in stats
                            and cls.goalie_decided is None):
                        cls.goalie_decided = stats
                    if ("decision" not in stats
                            and cls.goalie_relief is None):
                        cls.goalie_relief = stats
            if (cls.skater_record is not None
                    and cls.goalie_decided is not None
                    and cls.goalie_relief is not None):
                break

    def test_skater_keys_exist_in_real_records(self):
        self.assertIsNotNone(self.skater_record)
        for key in SKATER_KEYS:
            self.assertIn(key, self.skater_record, key)

    def test_goalie_carried_keys_exist_in_real_records(self):
        self.assertIsNotNone(self.goalie_decided)
        for key in ("goals_against", "shots_against"):
            self.assertIn(key, self.goalie_decided, key)

    def test_saves_derives_from_real_record(self):
        stats = dict(self.goalie_decided)
        self.assertNotIn("saves", stats)  # never carried, only derived
        _add_derived("hockey", stats)
        self.assertEqual(
            stats["saves"],
            float(stats["shots_against"] - stats["goals_against"]))
        self.assertGreaterEqual(stats["saves"], 0)

    def test_saves_absent_without_a_component(self):
        stats = dict(self.goalie_decided)
        del stats["goals_against"]
        _add_derived("hockey", stats)
        self.assertNotIn("saves", stats)

    def test_wins_derives_from_decision_and_consumes_it(self):
        stats = dict(self.goalie_decided)
        expected = 1.0 if stats["decision"] == "W" else 0.0
        _add_derived("hockey", stats)
        self.assertEqual(stats["wins"], expected)
        # The decision string is consumed by the derivation — never
        # carried as a stored stat.
        self.assertNotIn("decision", stats)

    def test_wins_absent_without_decision(self):
        # Relief appearances carry no decision of record: wins stays
        # absent, never zero-filled.
        self.assertIsNotNone(self.goalie_relief)
        stats = dict(self.goalie_relief)
        _add_derived("hockey", stats)
        self.assertNotIn("wins", stats)
        # Saves still derives on the same record — the derivations
        # are independent.
        self.assertIn("saves", stats)


class NhlGateTests(unittest.TestCase):
    """The NHL publish-gate calibration (Nov 21, 2026): the NBA's
    established/thin bars on an 82-game season, with a 7-day
    staleness bar that clears the league's scheduled pauses."""

    def test_threshold_values_pinned(self):
        from player_prop_lines import gate_thresholds
        self.assertEqual(gate_thresholds("NHL"),
                         {"prior_min": 40, "current_max": 3,
                          "stale_days": 7})

    def test_suspect_rule_boundaries(self):
        from player_prop_lines import evaluate_publish_gate

        def games(season, n):
            return [{"season": season, "date": "2026-01-01",
                     "stats": {}} for _ in range(n)]

        cases = {
            # (prior, current, withheld under NHL?)
            "at_bar_thin": (40, 3, True),
            "below_bar_thin": (39, 0, False),
            "at_bar_active": (40, 4, False),
            "full_prior_out": (82, 0, True),
        }
        for name, (prior, current, expected) in cases.items():
            players = {"p": {"games": games("20252026", prior)
                             + games("20262027", current)},
                       "context": {"games": games("20262027", 25)}}
            withheld = evaluate_publish_gate(players, "NHL")
            self.assertEqual("p" in withheld, expected, name)
            self.assertNotIn("context", withheld)

    def test_staleness_clears_league_pauses(self):
        from player_prop_lines import store_stale_reason
        # Newest game Dec 23 (the Christmas shutdown's last game
        # day); as-of Dec 29 is 6 days — inside the 7-day bar, so
        # the pause does not withhold the league. Eight days is a
        # dead feed, not a pause.
        players = {"p1": {"games": [
            {"season": "20262027", "date": "2026-12-23",
             "stats": {"points": 1.0}}]}}
        self.assertIsNone(
            store_stale_reason(players, "2026-12-29", "NHL"))
        self.assertEqual(
            store_stale_reason(players, "2026-12-31", "NHL"),
            "stale_store")
        # The same 6-day gap is far outside the NBA's 2-day bar.
        self.assertEqual(
            store_stale_reason(players, "2026-12-29", "NBA"),
            "stale_store")


class NhlValidationTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.empty()
        self.player = self.registry.add_player({
            "full_name": "Test Skater",
            "league": "NHL",
            "provider_ids": {"nhl": 8470000},
            "team_history": [{"team": "Edmonton Oilers",
                              "from": "2026-07-01", "to": None}],
            "status_history": [{"status": "active",
                                "from": "2026-07-01", "to": None,
                                "note": ""}],
        })

    def v(self, **selection):
        selection.setdefault("player_id", self.player["player_id"])
        return val.validate_selection(selection, self.registry)

    def test_undefined_nhl_market_rejected_market_unsupported(self):
        r = self.v(league="NHL", market="shutouts", line=0.5,
                   direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(check_status(r, "league"), "pass")
        self.assertEqual(check_status(r, "market"), "fail")
        self.assertIn("MARKET_UNSUPPORTED",
                      [e["code"] for e in r["errors"]])
        self.assertFalse(r["ticket_eligible"])

    def test_defined_nhl_market_passes_market_check(self):
        r = self.v(league="NHL", market="shots", line=2.5,
                   direction="over")
        self.assertEqual(check_status(r, "league"), "pass")
        self.assertEqual(check_status(r, "market"), "pass")
        self.assertNotIn("MARKET_UNSUPPORTED",
                         [e["code"] for e in r["errors"]])


if __name__ == "__main__":
    unittest.main()
