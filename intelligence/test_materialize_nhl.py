"""Tests for materialize_collected_props — the NHL props store
(Nov 21, 2026).

Exercised against the REAL collected layer (collected/
game_logs_NHL.json, the Gap-Filler Phase 2 output) copied into a
temp data root — count conservation, schema shape, provenance
honesty, the D5 string handling (toi -> toi_seconds; decision
consumed by the wins derivation), the hockey derivations, and the
append semantics the nightly publish phase depends on are all
checked against actual records, never invented data. Expectations
are computed from the layer itself, so the tests hold whether the
layer carries one season or two. Skipped when the collected layer
is absent from this checkout.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import materialize_collected_props as mat

COLLECTED_NHL = (Path.home() / "workspace" / "sports-feed-data-wt" /
                 "data" / "intelligence" / "collected" /
                 "game_logs_NHL.json")


def _expected_toi_seconds(toi: str) -> int:
    minutes, seconds = toi.split(":")
    return int(minutes) * 60 + int(seconds)


@unittest.skipUnless(COLLECTED_NHL.exists(),
                     "collected NHL layer not present in this checkout")
class MaterializeNhlRealLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layer = json.loads(COLLECTED_NHL.read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "collected").mkdir(parents=True)
        shutil.copy(COLLECTED_NHL, root / "collected" /
                    "game_logs_NHL.json")
        cls.root = root
        cls.report = mat.materialize(root, league="NHL")
        cls.store = json.loads(
            (root / "props" / "game_logs_NHL.json").read_text())
        # Layer games indexed by (player_id, game_id) for the
        # per-game comparisons below.
        cls.layer_games = {}
        for pid, rec in cls.layer["players"].items():
            for game in rec.get("games") or []:
                cls.layer_games[(pid, str(game["game_id"]))] = game

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counts_conserve_on_fresh_store(self):
        layer_games = sum(len(r.get("games") or [])
                          for r in self.layer["players"].values())
        self.assertEqual(self.report["status"], "ok")
        self.assertEqual(self.report["players_in"],
                         len(self.layer["players"]))
        self.assertEqual(self.report["players_out"],
                         len(self.layer["players"]))
        self.assertEqual(self.report["games_in"], layer_games)
        self.assertEqual(self.report["games_out"], layer_games)
        self.assertEqual(self.report["games_added"], layer_games)
        self.assertTrue(self.report["counts_conserved"])
        self.assertEqual(self.report["unmapped_records"], 0)

    def test_store_schema_and_coverage(self):
        self.assertEqual(self.store["version"], 1)
        self.assertEqual(self.store["classification"], "Historical")
        self.assertEqual(self.store["league"], "NHL")
        cov = self.store["coverage"]
        layer_seasons = sorted({
            g["season"] for g in self.layer_games.values()})
        self.assertEqual(cov["seasons"], layer_seasons)
        self.assertEqual(cov["season"], layer_seasons[-1])
        self.assertEqual(cov["sport"], "hockey")
        self.assertEqual(cov["source"],
                         "nhl-web-api-collected-promoted")
        for rec in self.store["players"].values():
            self.assertEqual(set(rec.keys()), {"games"})
            for game in rec["games"]:
                self.assertEqual(
                    set(game.keys()),
                    {"game_id", "date", "season", "team", "stats"})

    def test_provenance_block_records_nhl_ruling(self):
        prov = self.store["provenance"]
        self.assertEqual(prov["origin"],
                         "materialized_from_collected_layer")
        self.assertEqual(prov["source_name"],
                         "NHL official web API")
        self.assertEqual(prov["collector"], "gapfill_stats_nhl")
        self.assertEqual(prov["materialized_by"],
                         "materialize_collected_props")
        self.assertEqual(prov["promotion"]["ruled_by"],
                         "Isaac Jones")
        self.assertEqual(prov["promotion"]["date"], "2026-11-21")
        self.assertIn("D4", prov["promotion"]["basis"])
        self.assertIn("NHL", prov["promotion"]["basis"])
        self.assertEqual(prov["unmapped_records"], 0)

    def test_string_stats_handled_and_nothing_else_non_numeric(self):
        # D5: every stored game carries toi_seconds equal to the
        # layer's toi converted; neither string stat survives; and
        # every stored stat value is numeric.
        checked = 0
        for pid, rec in self.store["players"].items():
            for game in rec["games"]:
                stats = game["stats"]
                self.assertNotIn("toi", stats)
                self.assertNotIn("decision", stats)
                for value in stats.values():
                    self.assertIsInstance(value, (int, float))
                    self.assertNotIsInstance(value, bool)
                layer_game = self.layer_games[(pid,
                                               str(game["game_id"]))]
                self.assertEqual(
                    stats["toi_seconds"],
                    float(_expected_toi_seconds(
                        layer_game["stats"]["toi"])))
                checked += 1
        self.assertEqual(checked, len(self.layer_games))

    def test_hockey_derivations_against_layer(self):
        saves_checked = wins_checked = relief_checked = 0
        for pid, rec in self.store["players"].items():
            for game in rec["games"]:
                stats = game["stats"]
                layer_stats = self.layer_games[
                    (pid, str(game["game_id"]))]["stats"]
                if "shots_against" in layer_stats:
                    self.assertEqual(
                        stats["saves"],
                        float(layer_stats["shots_against"]
                              - layer_stats["goals_against"]))
                    saves_checked += 1
                if "decision" in layer_stats:
                    self.assertEqual(
                        stats["wins"],
                        1.0 if layer_stats["decision"] == "W"
                        else 0.0)
                    wins_checked += 1
                elif "shots_against" in layer_stats:
                    # Relief appearance: no decision of record, so
                    # wins stays absent — never zero-filled.
                    self.assertNotIn("wins", stats)
                    relief_checked += 1
        self.assertGreater(saves_checked, 0)
        self.assertGreater(wins_checked, 0)
        self.assertGreater(relief_checked, 0)

    def test_second_run_is_pure_append_noop(self):
        # The nightly publish phase re-materializes the current
        # layer into the existing store: nothing is added, nothing
        # is corrected, and every layer game is accounted as
        # already present.
        before = sum(len(r["games"])
                     for r in self.store["players"].values())
        report2 = mat.materialize(self.root, league="NHL")
        self.assertEqual(report2["status"], "ok")
        self.assertEqual(report2["games_added"], 0)
        self.assertEqual(report2["games_corrected"], 0)
        self.assertEqual(report2["games_already_present"],
                         report2["games_in"])
        self.assertEqual(report2["games_out"], report2["games_in"])
        self.assertTrue(report2["counts_conserved"])
        store2 = json.loads(
            (self.root / "props" / "game_logs_NHL.json").read_text())
        after = sum(len(r["games"])
                    for r in store2["players"].values())
        self.assertEqual(before, after)


class ToiConversionTests(unittest.TestCase):
    def test_well_formed(self):
        self.assertEqual(mat._toi_to_seconds("17:43"), 17 * 60 + 43)
        self.assertEqual(mat._toi_to_seconds("60:00"), 3600)
        self.assertEqual(mat._toi_to_seconds("0:51"), 51)
        self.assertEqual(mat._toi_to_seconds("65:00"), 3900)

    def test_malformed_returns_none(self):
        for bad in ("17:5", "17:60", "abc", "17", "", None, 1063):
            self.assertIsNone(mat._toi_to_seconds(bad), bad)


class MaterializeNhlGuardTests(unittest.TestCase):
    def test_missing_layer_fails_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = mat.materialize(Path(tmp), league="NHL")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason_class"], "missing_layer")

    def test_unknown_string_stat_is_reported_not_dropped(self):
        # A stat string that is neither toi nor decision must
        # surface as an unmapped record naming the key — the
        # materializer handles ONLY the two known string stats.
        doc = {
            "kind": "collected-game-logs", "league": "NHL",
            "collected": True, "season": "20262027",
            "generated_at": "2026-11-21T00:00:00+00:00",
            "players": {"plyr-0001": {"games": [{
                "game_id": 1, "date": "2026-11-01",
                "season": "20262027", "team": "Edmonton Oilers",
                "stats": {"goals": 1, "toi": "20:00",
                          "mystery": "not-a-number"},
                "provenance": {"fetched_at":
                               "2026-11-21T00:00:00+00:00"},
            }]}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "collected").mkdir(parents=True)
            (root / "collected" / "game_logs_NHL.json").write_text(
                json.dumps(doc))
            report = mat.materialize(root, league="NHL")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["unmapped_records"], 1)
        self.assertEqual(report["problems"][0]["reason"],
                         "non_numeric_stat_value")
        self.assertEqual(report["problems"][0]["detail"]["stat_keys"],
                         ["mystery"])
        # The one layer game is fully accounted: 0 in the store,
        # 1 reported unmapped.
        self.assertEqual(report["games_out"], 0)
        self.assertTrue(report["counts_conserved"])


if __name__ == "__main__":
    unittest.main()
