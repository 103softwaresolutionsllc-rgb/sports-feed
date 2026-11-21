"""Phase 1 tests: taxonomy, player registry, identification service.

Run from the repo root:
    python3 -m unittest discover -s intelligence -p 'test_*.py'
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import player_identification as ident
import player_props_taxonomy as taxonomy
from player_registry import PlayerRegistry


class TaxonomyTests(unittest.TestCase):
    def test_nba_blocks_market_matches_spec_example(self):
        m = taxonomy.get_market("NBA", "blocks")
        self.assertIsNotNone(m)
        self.assertEqual(m["market_id"], "nba_player_blocks")
        self.assertEqual(m["market_name"], "Blocks")
        self.assertEqual(m["statistic_key"], "blocks")
        self.assertEqual(m["statistic_type"], "integer")
        self.assertEqual(m["period"], "full_game")
        self.assertEqual(m["directions"], ["over", "under"])
        self.assertTrue(m["supports_alternate_lines"])
        self.assertTrue(m["supports_historical_analysis"])
        self.assertTrue(m["supports_player_projection"])
        self.assertTrue(m["active"])

    def test_nba_and_nfl_markets_defined(self):
        self.assertGreaterEqual(len(taxonomy.markets_for_league("NBA")), 20)
        self.assertGreaterEqual(len(taxonomy.markets_for_league("NFL")), 25)
        for key in ("passing_yards", "rushing_yards", "receptions",
                    "anytime_touchdown", "sacks", "field_goals_made"):
            self.assertIsNotNone(taxonomy.get_market("NFL", key), key)
        for key in ("points", "rebounds", "assists", "steals", "blocks",
                    "points_rebounds_assists", "minutes"):
            self.assertIsNotNone(taxonomy.get_market("NBA", key), key)

    def test_markets_are_league_scoped(self):
        # An NFL market is not an NBA market and vice versa.
        self.assertIsNone(taxonomy.get_market("NBA", "passing_yards"))
        self.assertIsNone(taxonomy.get_market("NFL", "blocks"))
        self.assertFalse(taxonomy.market_supported("NBA", "passing_yards"))

    def test_mma_is_phase_later_stub(self):
        # MLB's taxonomy was defined Nov 21, 2026 (see
        # test_player_props_taxonomy_mlb.py); MMA remains the stub.
        self.assertIsNotNone(taxonomy.get_league("MMA"))
        self.assertFalse(taxonomy.league_supported("MMA"))
        self.assertEqual(taxonomy.markets_for_league("MMA"), [])

    def test_market_resolution_by_id_and_name(self):
        by_id = taxonomy.get_market("NBA", "nba_player_blocks")
        by_name = taxonomy.get_market("NBA", "Blocks")
        self.assertIs(by_id, by_name)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()

    def test_seed_contents(self):
        leagues = {p["league"] for p in self.registry.all_players()}
        self.assertIn("NBA", leagues)
        self.assertIn("NFL", leagues)
        self.assertIn("MLB", leagues)  # Canseco, for the canonical test
        statuses = {self.registry.current_status(p)
                    for p in self.registry.all_players()}
        self.assertIn("retired", statuses)
        self.assertIn("injured", statuses)
        self.assertIn("active", statuses)

    def test_permanent_ids_unique(self):
        ids = [p["player_id"] for p in self.registry.all_players()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_traded_player_history_preserved(self):
        # Acceptance criterion: player traded -> historical team
        # association preserved, current team is the new team.
        doncic = self.registry.find_by_exact_name("Luka Dončić")
        self.assertEqual(self.registry.team_on(doncic, "2024-06-01"),
                         "Dallas Mavericks")
        self.assertEqual(self.registry.current_team(doncic),
                         "Los Angeles Lakers")
        self.assertEqual(len(doncic["team_history"]), 2)

    def test_record_team_change_appends_never_rewrites(self):
        reg = PlayerRegistry.load_seed()
        mccaffrey = reg.find_by_exact_name("Christian McCaffrey")
        before = [dict(e) for e in mccaffrey["team_history"]]
        reg.record_team_change(mccaffrey, "Test City Testers", "2027-01-15")
        self.assertEqual(reg.current_team(mccaffrey), "Test City Testers")
        self.assertEqual(reg.team_on(mccaffrey, "2020-06-01"),
                         "Carolina Panthers")
        self.assertEqual(reg.team_on(mccaffrey, "2024-06-01"),
                         "San Francisco 49ers")
        # The pre-existing stints are untouched apart from the closing
        # date the change itself sets on the previously-open stint.
        self.assertEqual(mccaffrey["team_history"][0], before[0])
        self.assertEqual(len(mccaffrey["team_history"]), 3)

    def test_retired_player_record_remains_queryable(self):
        # Acceptance criterion: retired -> historical record preserved.
        rose = self.registry.find_by_exact_name("Derrick Rose")
        self.assertIsNotNone(rose)
        self.assertEqual(self.registry.current_status(rose), "retired")
        self.assertEqual(self.registry.team_on(rose, "2011-05-01"),
                         "Chicago Bulls")
        self.assertIs(self.registry.get_by_id(rose["player_id"]), rose)

    def test_status_change_appends(self):
        # Acceptance criterion: injured -> availability updated (status
        # history is append-only and effective-dated).
        reg = PlayerRegistry.load_seed()
        tatum = reg.find_by_exact_name("Jayson Tatum")
        reg.record_status_change(tatum, "injured", "2026-11-20",
                                 "Test fixture injury.")
        self.assertEqual(reg.current_status(tatum), "injured")
        self.assertEqual(reg.status_on(tatum, "2026-11-01"), "active")
        reg.record_status_change(tatum, "active", "2026-12-25")
        self.assertEqual(reg.current_status(tatum), "active")
        self.assertEqual(len(tatum["status_history"]), 3)

    def test_add_player_assigns_permanent_id(self):
        reg = PlayerRegistry.empty()
        rec = reg.add_player({"full_name": "Test Player", "league": "NBA",
                              "position": "SG", "jersey_number": 9})
        self.assertTrue(rec["player_id"].startswith("plyr-"))
        with self.assertRaises(ValueError):
            reg.add_player({"player_id": rec["player_id"],
                            "full_name": "Duplicate", "league": "NBA"})


class IdentificationTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlayerRegistry.load_seed()

    def test_exact_match(self):
        r = ident.resolve_player(self.registry, "Jayson Tatum")
        self.assertEqual(r["status"], "exact")
        self.assertEqual(r["player"]["full_name"], "Jayson Tatum")
        self.assertFalse(r["suggestion_requires_confirmation"])

    def test_normalized_match_folds_accents_and_case(self):
        r = ident.resolve_player(self.registry, "luka doncic")
        self.assertEqual(r["status"], "normalized")
        self.assertEqual(r["player"]["full_name"], "Luka Dončić")
        r2 = ident.resolve_player(self.registry, "NIKOLA JOKIC")
        self.assertEqual(r2["status"], "normalized")
        self.assertEqual(r2["player"]["full_name"], "Nikola Jokić")

    def test_provider_id_match(self):
        reg = PlayerRegistry.empty()
        reg.add_player({"full_name": "Fixture Player", "league": "NBA",
                        "position": "C",
                        "provider_ids": {"api-sports": 4242}})
        r = ident.resolve_player(reg, None, provider="api-sports",
                                 provider_id=4242)
        self.assertEqual(r["status"], "provider_id")
        self.assertEqual(r["player"]["full_name"], "Fixture Player")

    def test_fuzzy_is_suggestion_only_never_substitution(self):
        # Acceptance criterion: misspelled name -> identity suggestions,
        # with NO automatic substitution of the suggested player.
        r = ident.resolve_player(self.registry, "Jayson Tatom")
        self.assertEqual(r["status"], "suggestion")
        self.assertIsNone(r["player"])
        self.assertTrue(r["suggestion_requires_confirmation"])
        self.assertEqual(r["suggestions"][0]["full_name"], "Jayson Tatum")

    def test_not_found(self):
        r = ident.resolve_player(self.registry, "Zebedee Nonexistent")
        self.assertEqual(r["status"], "not_found")
        self.assertIsNone(r["player"])
        self.assertEqual(r["suggestions"], [])

    def test_levenshtein(self):
        self.assertEqual(ident.levenshtein("conseco", "canseco"), 1)
        self.assertEqual(ident.levenshtein("tatum", "tatum"), 0)
        self.assertEqual(ident.levenshtein("", "abc"), 3)


def _base_registry_doc() -> dict:
    return {
        "registry_version": 1,
        "classification": "Historical",
        "players": [{
            "player_id": "plyr-0001",
            "full_name": "Base Player",
            "league": "NBA",
            "provider_ids": {"espn": 42},
            "team_history": [
                {"team": "Team A", "from": "2026-01-01", "to": None}],
            "status_history": [
                {"status": "active", "from": "2026-01-01", "to": None,
                 "note": ""}],
        }],
    }


class MergeAwareSaveTests(unittest.TestCase):
    """Registry saves merge with the on-disk document (Nov 21, 2026).

    The registry has many concurrent writers; a blind whole-document
    save from a stale in-memory copy destroyed a peer's 802 NHL
    registrations and then a peer's 1,625 MLB registrations in one
    night. These tests pin the merge-aware behavior.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "player_registry.json"
        PlayerRegistry(_base_registry_doc()).save(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _load_file(self) -> PlayerRegistry:
        return PlayerRegistry.load(self.path)

    def test_stale_save_preserves_peer_registrations(self):
        stale = self._load_file()
        peer = self._load_file()
        peer.add_player({"full_name": "Peer Player", "league": "NHL",
                         "provider_ids": {"nhl": 777}})
        peer.save(self.path)
        # The stale writer registers its own player (explicit id, no
        # collision) and saves over the peer's newer state.
        stale.add_player({"player_id": "plyr-0100",
                          "full_name": "Stale Writer Player",
                          "league": "NFL",
                          "provider_ids": {"espn": 999}})
        remaps = stale.save(self.path)
        self.assertEqual(remaps, [])
        saved = self._load_file()
        names = {p["full_name"] for p in saved.all_players()}
        self.assertEqual(names, {"Base Player", "Peer Player",
                                 "Stale Writer Player"})
        # The stale writer adopted the merged state in memory too.
        self.assertEqual(len(stale.all_players()), 3)
        self.assertIsNotNone(stale.get_by_provider_id("nhl", 777))

    def test_id_collision_remaps_the_later_writer(self):
        stale = self._load_file()
        peer = self._load_file()
        peer_player = peer.add_player(
            {"full_name": "Peer Player", "league": "NHL",
             "provider_ids": {"nhl": 777}})
        self.assertEqual(peer_player["player_id"], "plyr-0002")
        peer.save(self.path)
        # The stale writer allocated the same next id for a DIFFERENT
        # player (disjoint provider identity).
        stale_player = stale.add_player(
            {"full_name": "Stale Player", "league": "NFL",
             "provider_ids": {"espn": 999}})
        self.assertEqual(stale_player["player_id"], "plyr-0002")
        remaps = stale.save(self.path)
        self.assertEqual(len(remaps), 1)
        self.assertEqual(remaps[0]["from"], "plyr-0002")
        self.assertEqual(remaps[0]["to"], "plyr-0003")
        # First writer keeps the contested id; the later writer's
        # record survives under the fresh id, remapped in place.
        self.assertEqual(stale_player["player_id"], "plyr-0003")
        saved = self._load_file()
        self.assertEqual(len(saved.all_players()), 3)
        self.assertEqual(saved.get_by_id("plyr-0002")["full_name"],
                         "Peer Player")
        self.assertEqual(saved.get_by_id("plyr-0003")["full_name"],
                         "Stale Player")

    def test_same_player_records_field_merge(self):
        stale = self._load_file()
        peer = self._load_file()
        peer_player = peer.get_by_id("plyr-0001")
        peer.record_team_change(peer_player, "Team B", "2026-06-01")
        peer.save(self.path)
        # The stale writer's copy predates the team change; it fills
        # in the player's position and saves.
        stale_player = stale.get_by_id("plyr-0001")
        stale_player["position"] = "PG"
        stale.save(self.path)
        saved = self._load_file()
        merged = saved.get_by_id("plyr-0001")
        # The peer's appended history survived the stale save...
        self.assertEqual(saved.current_team(merged), "Team B")
        self.assertEqual(len(merged["team_history"]), 2)
        # ...and the stale writer's fact landed too.
        self.assertEqual(merged["position"], "PG")
        self.assertEqual(merged["provider_ids"], {"espn": 42})

    def test_unparseable_disk_document_is_never_overwritten(self):
        self.path.write_text("{ not json", encoding="utf-8")
        reg = PlayerRegistry(_base_registry_doc())
        with self.assertRaises(ValueError):
            reg.save(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "{ not json")


if __name__ == "__main__":
    unittest.main()
