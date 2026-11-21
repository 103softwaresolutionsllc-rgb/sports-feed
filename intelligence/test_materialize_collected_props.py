"""Tests for materialize_collected_props — Route 1 to the MLB props
store (Nov 21, 2026).

The materializer is exercised against the REAL collected layer
(collected/game_logs_MLB.json, the Gap-Filler Phase 2 output) copied
into a temp data root — count conservation, schema shape, provenance
honesty, and derivation are all checked against actual records,
never invented data. Skipped when the collected layer is absent
from this checkout.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import materialize_collected_props as mat

COLLECTED_MLB = (Path.home() / "workspace" / "sports-feed-data-wt" /
                 "data" / "intelligence" / "collected" /
                 "game_logs_MLB.json")


@unittest.skipUnless(COLLECTED_MLB.exists(),
                     "collected MLB layer not present in this checkout")
class MaterializeRealLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layer = json.loads(COLLECTED_MLB.read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "collected").mkdir(parents=True)
        shutil.copy(COLLECTED_MLB, root / "collected" /
                    "game_logs_MLB.json")
        cls.report = mat.materialize(root, league="MLB")
        cls.store = json.loads(
            (root / "props" / "game_logs_MLB.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counts_conserve(self):
        layer_games = sum(len(r.get("games") or [])
                          for r in self.layer["players"].values())
        self.assertEqual(self.report["status"], "ok")
        self.assertEqual(self.report["players_in"],
                         len(self.layer["players"]))
        self.assertEqual(self.report["players_out"],
                         len(self.layer["players"]))
        self.assertEqual(self.report["games_in"], layer_games)
        self.assertEqual(self.report["games_out"], layer_games)
        self.assertTrue(self.report["counts_conserved"])
        self.assertEqual(self.report["unmapped_records"], 0)

    def test_store_schema_matches_props_store_shape(self):
        self.assertEqual(self.store["version"], 1)
        self.assertEqual(self.store["classification"], "Historical")
        self.assertEqual(self.store["league"], "MLB")
        cov = self.store["coverage"]
        self.assertEqual(cov["season"], "2026")
        self.assertEqual(cov["seasons"], ["2026"])
        self.assertEqual(cov["sport"], "baseball")
        self.assertEqual(cov["source"],
                         "mlb-stats-api-collected-promoted")
        for rec in self.store["players"].values():
            self.assertEqual(set(rec.keys()), {"games"})
            for game in rec["games"]:
                self.assertEqual(
                    set(game.keys()),
                    {"game_id", "date", "season", "team", "stats"})

    def test_provenance_block_is_honest(self):
        prov = self.store["provenance"]
        self.assertEqual(prov["origin"],
                         "materialized_from_collected_layer")
        self.assertEqual(prov["source_name"], "MLB Stats API")
        self.assertEqual(prov["collector"], "gapfill_stats_mlb")
        self.assertEqual(prov["materialized_by"],
                         "materialize_collected_props")
        self.assertEqual(prov["collected_generated_at"],
                         self.layer["generated_at"])
        # The layer's fetch timestamps carry over, not the
        # materialization time.
        self.assertEqual(prov["collected_fetched_at_min"],
                         self.layer["source"]["fetched_at"])
        self.assertEqual(prov["collected_fetched_at_max"],
                         self.layer["source"]["fetched_at"])
        self.assertEqual(prov["promotion"]["ruled_by"], "Isaac Jones")
        self.assertEqual(prov["promotion"]["date"], "2026-11-21")
        self.assertIn("D4", prov["promotion"]["basis"])
        self.assertEqual(prov["unmapped_records"], 0)

    def test_derivation_applied_and_never_zero_filled(self):
        derived = 0
        for rec in self.store["players"].values():
            for game in rec["games"]:
                stats = game["stats"]
                if all(k in stats for k in ("hits", "runs", "rbi")):
                    self.assertEqual(
                        stats["hits_runs_rbi"],
                        float(stats["hits"] + stats["runs"]
                              + stats["rbi"]))
                    derived += 1
                else:
                    self.assertNotIn("hits_runs_rbi", stats)
        self.assertGreater(derived, 10000)

    def test_zero_game_players_carried_with_empty_logs(self):
        layer_empty = {pid for pid, r in self.layer["players"].items()
                       if not r.get("games")}
        self.assertGreater(len(layer_empty), 0)
        for pid in layer_empty:
            self.assertEqual(self.store["players"][pid]["games"], [])


class MaterializeGuardTests(unittest.TestCase):
    def test_missing_layer_fails_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = mat.materialize(Path(tmp), league="MLB")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason_class"], "missing_layer")


if __name__ == "__main__":
    unittest.main()
