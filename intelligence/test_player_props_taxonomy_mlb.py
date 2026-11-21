"""MLB taxonomy tests (adopted Nov 21, 2026).

The MLB market set was adopted from the Nov 21, 2026 taxonomy proposal,
grounded in the MLB Stats API game logs. These tests pin:

  * the taxonomy structure — 15 markets (8 batting, 6 pitching, 1
    derived combined), all defined but INACTIVE until a line-making
    feed exists (the NBA rebound-split representation);
  * every non-derived market key against the REAL collected layer
    (collected/game_logs_MLB.json) — keys must exist in actual records,
    sampled across batting and pitching, never against invented data;
  * the hits_runs_rbi derivation, computed by the harvest's own
    _add_derived on a real record's components;
  * validation behavior — the league check now passes for MLB, defined
    markets are structurally accepted, and both dormant defined
    markets and undefined markets (quality_starts) reject at the
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

COLLECTED_MLB = (Path.home() / "workspace" / "sports-feed-data-wt" /
                 "data" / "intelligence" / "collected" /
                 "game_logs_MLB.json")

BATTING_KEYS = ("hits", "total_bases", "runs", "rbi", "home_runs",
                "stolen_bases", "walks", "strikeouts")
PITCHING_KEYS = ("pitching_strikeouts", "outs_recorded", "earned_runs",
                 "hits_allowed", "walks_allowed", "wins")
DERIVED_KEY = "hits_runs_rbi"


def check_status(result, name):
    for c in result["checks"]:
        if c["check"] == name:
            return c["status"]
    raise AssertionError(f"check {name} missing")


class MlbTaxonomyStructureTests(unittest.TestCase):
    def test_league_defined(self):
        self.assertTrue(taxonomy.league_supported("MLB"))
        self.assertEqual(taxonomy.get_league("MLB")["sport"], "baseball")

    def test_fifteen_markets_in_groups(self):
        markets = taxonomy.markets_for_league("MLB")
        self.assertEqual(len(markets), 15)
        by_cat = {}
        for m in markets:
            by_cat.setdefault(m["category"], []).append(m["statistic_key"])
        self.assertEqual(sorted(by_cat["Batting"]), sorted(BATTING_KEYS))
        self.assertEqual(sorted(by_cat["Pitching"]),
                         sorted(PITCHING_KEYS))
        self.assertEqual(by_cat["Combined"], [DERIVED_KEY])

    def test_market_record_shape(self):
        for m in taxonomy.markets_for_league("MLB"):
            self.assertEqual(m["market_id"],
                             f"mlb_player_{m['statistic_key']}")
            self.assertEqual(m["league"], "MLB")
            self.assertEqual(m["statistic_type"], "integer")
            self.assertEqual(m["period"], "full_game")
            self.assertEqual(m["directions"], ["over", "under"])
            self.assertTrue(m["supports_alternate_lines"])
            self.assertTrue(m["supports_historical_analysis"])
            self.assertTrue(m["supports_player_projection"])

    def test_all_markets_defined_but_dormant(self):
        # Defined-but-dormant, the rebound-split representation: the
        # taxonomy resolves every market, yet none is active, so no
        # MLB selection can validate until activation.
        for key in BATTING_KEYS + PITCHING_KEYS + (DERIVED_KEY,):
            m = taxonomy.get_market("MLB", key)
            self.assertIsNotNone(m, key)
            self.assertFalse(m["active"], key)
            self.assertFalse(taxonomy.market_supported("MLB", key), key)

    def test_resolution_by_id_and_name(self):
        by_id = taxonomy.get_market("MLB", "mlb_player_total_bases")
        by_name = taxonomy.get_market("MLB", "Total Bases")
        self.assertIs(by_id, by_name)

    def test_undefined_markets_stay_undefined(self):
        # Per the adopted proposal these are NOT defined: quality
        # starts, second-wave doubles / pitch count, rate stats.
        for key in ("quality_starts", "doubles", "pitches_thrown",
                    "batting_average", "innings_pitched"):
            self.assertIsNone(taxonomy.get_market("MLB", key), key)
            self.assertFalse(taxonomy.market_supported("MLB", key), key)

    def test_mlb_keys_do_not_leak_into_other_leagues(self):
        self.assertIsNone(taxonomy.get_market("NBA", "total_bases"))
        self.assertIsNone(taxonomy.get_market("NFL", "hits"))


@unittest.skipUnless(COLLECTED_MLB.exists(),
                     "collected MLB layer not present in this checkout")
class MlbCollectedLayerKeyTests(unittest.TestCase):
    """Every taxonomy key must exist in real collected records."""

    @classmethod
    def setUpClass(cls):
        doc = json.loads(COLLECTED_MLB.read_text())
        cls.seen_keys = set()
        cls.batting_record = None
        cls.pitching_record = None
        wanted = set(BATTING_KEYS + PITCHING_KEYS)
        for rec in doc["players"].values():
            for game in rec.get("games") or []:
                stats = game.get("stats") or {}
                cls.seen_keys |= set(stats)
                if (cls.batting_record is None
                        and all(k in stats for k in ("hits", "runs", "rbi"))):
                    cls.batting_record = stats
                if (cls.pitching_record is None
                        and "pitching_strikeouts" in stats):
                    cls.pitching_record = stats
            if (wanted <= cls.seen_keys and cls.batting_record is not None
                    and cls.pitching_record is not None):
                break

    def test_batting_keys_exist_in_real_records(self):
        for key in BATTING_KEYS:
            self.assertIn(key, self.seen_keys, key)
        self.assertIsNotNone(self.batting_record)

    def test_pitching_keys_exist_in_real_records(self):
        for key in PITCHING_KEYS:
            self.assertIn(key, self.seen_keys, key)
        self.assertIsNotNone(self.pitching_record)

    def test_derived_market_computes_from_real_record(self):
        stats = dict(self.batting_record)
        stats.pop(DERIVED_KEY, None)  # collected records never carry it
        _add_derived("baseball", stats)
        self.assertEqual(stats[DERIVED_KEY],
                         float(stats["hits"] + stats["runs"]
                               + stats["rbi"]))

    def test_derived_market_absent_without_all_components(self):
        stats = dict(self.batting_record)
        stats.pop(DERIVED_KEY, None)
        del stats["rbi"]
        _add_derived("baseball", stats)
        self.assertNotIn(DERIVED_KEY, stats)


class MlbValidationTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()

    def v(self, **selection):
        return val.validate_selection(selection, self.registry)

    def test_undefined_mlb_market_rejected_market_unsupported(self):
        r = self.v(league="MLB", player_name="José Canseco",
                   market="quality_starts", line=0.5, direction="over")
        self.assertFalse(r["valid"])
        # The league check passes now that the taxonomy is defined;
        # the rejection lands at the market check.
        self.assertEqual(check_status(r, "league"), "pass")
        self.assertEqual(check_status(r, "market"), "fail")
        self.assertIn("MARKET_UNSUPPORTED",
                      [e["code"] for e in r["errors"]])
        self.assertFalse(r["ticket_eligible"])

    def test_defined_but_dormant_mlb_market_rejected(self):
        # A defined MLB market is structurally accepted yet inactive —
        # exactly how the NBA rebound splits behave.
        r = self.v(league="MLB", player_name="José Canseco",
                   market="hits", line=1.5, direction="over")
        self.assertFalse(r["valid"])
        self.assertEqual(check_status(r, "league"), "pass")
        self.assertEqual(check_status(r, "market"), "fail")
        self.assertIn("MARKET_UNSUPPORTED",
                      [e["code"] for e in r["errors"]])


if __name__ == "__main__":
    unittest.main()
