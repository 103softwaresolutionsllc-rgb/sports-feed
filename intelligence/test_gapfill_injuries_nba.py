"""Unit tests for gapfill_injuries_nba — the Sage Gap-Filler's first
collector (NBA official injury report).

The parser tests run against a REAL recorded report (Phase 0
fixture discipline from the Nov 16 spec): the word coordinates of
the league's actual 2026-11-21 12:45 AM report, frozen under
test_fixtures/. Everything else (keying, expiry, provenance) uses
small hand-built inputs — formula checks only, never a validation
basis.
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import gapfill_injuries_nba as gf

FIXTURE = (Path(__file__).parent / "test_fixtures"
           / "nba_injury_report_2026-11-21_0045_words.json")


def _fixture_rows():
    words = json.loads(FIXTURE.read_text())["words"]
    rows, stats = gf.parse_report(words)
    return rows, stats


class FixtureParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.stats = _fixture_rows()
        cls.by_name = {}
        for r in cls.rows:
            cls.by_name.setdefault(
                gf.report_name_to_display(r["player_name"]), r)

    def test_report_is_substantial(self):
        # The frozen report holds exactly 51 player rows (the
        # fixture's own status-word count); every one must parse.
        self.assertEqual(len(self.rows), 51)
        self.assertEqual(self.stats["anchors"], 51)
        self.assertEqual(self.stats["dropped_no_status"], 0)

    def test_simple_row(self):
        r = self.by_name["Ousmane Dieng"]
        self.assertEqual(r["status"], "Out")
        self.assertEqual(r["team"], "Milwaukee Bucks")
        self.assertEqual(r["matchup"], "MIL@PHI")
        self.assertEqual(r["game_date"], "11/21/2026")
        self.assertEqual(r["reason"],
                         "Injury/Illness - Left Finger Fracture")

    def test_wrapped_reason_reassembled(self):
        # The reason cell wraps and is vertically centered: its
        # first line prints ABOVE the player row, its last BELOW.
        r = self.by_name["Dominick Barlow"]
        self.assertEqual(r["status"], "Probable")
        self.assertEqual(r["reason"],
                         "Injury/Illness - Low Back Stress Reaction")
        r2 = self.by_name["Ariel Hukporti"]
        self.assertEqual(
            r2["reason"],
            "Injury/Illness - Right Achilles Tendon Rupture / Tear")

    def test_carry_forward_context(self):
        r = self.by_name["Deni Avdija"]
        self.assertEqual(r["team"], "Portland Trail Blazers")
        self.assertEqual(r["matchup"], "POR@NYK")
        self.assertEqual(r["game_date"], "11/21/2026")
        self.assertEqual(r["game_time"], "07:30 (ET)")

    def test_all_statuses_in_vocabulary(self):
        for r in self.rows:
            self.assertIn(r["status"], gf.STATUSES)


class KeyingTests(unittest.TestCase):
    def setUp(self):
        self.index = {
            "nikola jokic": [{"player_id": "plyr-0001",
                              "full_name": "Nikola Jokic",
                              "current_team": "Denver Nuggets"}],
            "sam smith": [
                {"player_id": "plyr-0002", "full_name": "Sam Smith",
                 "current_team": "Miami Heat"},
                {"player_id": "plyr-0003", "full_name": "Sam Smith",
                 "current_team": "Denver Nuggets"}],
        }

    def test_unique_name_keys(self):
        rec, reason = gf.key_row(
            {"player_name": "Jokic, Nikola", "team": "Denver Nuggets"},
            self.index)
        self.assertEqual(rec["player_id"], "plyr-0001")
        self.assertIsNone(reason)

    def test_team_disambiguates(self):
        rec, _ = gf.key_row(
            {"player_name": "Smith, Sam", "team": "Miami Heat"},
            self.index)
        self.assertEqual(rec["player_id"], "plyr-0002")

    def test_unknown_staged(self):
        rec, reason = gf.key_row(
            {"player_name": "Ghost, Gary", "team": "Nowhere"},
            self.index)
        self.assertIsNone(rec)
        self.assertEqual(reason, "no_registry_match")


class RecordTests(unittest.TestCase):
    def test_expiry_is_game_start_plus_two_hours(self):
        start = gf.game_start_utc("11/21/2026", "07:30 (ET)")
        self.assertEqual(start, datetime(2026, 11, 21, 12, 30,
                                         tzinfo=timezone.utc))

    def test_provenance_complete(self):
        rows = [{"game_date": "11/21/2026", "game_time": "07:30 (ET)",
                 "matchup": "DEN@MIA", "team": "Denver Nuggets",
                 "player_name": "Jokic, Nikola", "status": "Out",
                 "reason": "Injury/Illness - Rest"}]
        index = {"nikola jokic": [{"player_id": "plyr-0001",
                                   "full_name": "Nikola Jokic",
                                   "current_team": "Denver Nuggets"}]}
        published, staged = gf.build_records(
            rows, index, "https://ak-static.cms.nba.com/x.pdf",
            datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc), None)
        self.assertEqual(len(published), 1)
        self.assertEqual(staged, [])
        prov = published[0]["provenance"]
        for field in ("origin", "source_name", "source_url",
                      "fetched_at", "collector", "classification",
                      "expires_at", "supersedes"):
            self.assertIn(field, prov)
        self.assertEqual(prov["origin"], "collected")
        self.assertEqual(prov["classification"], "Historical")
        self.assertTrue(published[0]["collected"])
        self.assertEqual(published[0]["reason_category"],
                         "Injury/Illness")

    def test_latest_url_wins(self):
        html = ('<a href="https://ak-static.cms.nba.com/referee/injury/'
                'Injury-Report_2026-11-21_12_15AM.pdf">a</a>'
                '<a href="https://ak-static.cms.nba.com/referee/injury/'
                'Injury-Report_2026-11-21_12_45AM.pdf">b</a>'
                '<a href="https://ak-static.cms.nba.com/referee/injury/'
                'Injury-Report_2026-11-20_11_30PM.pdf">c</a>')
        self.assertTrue(gf.find_latest_report_url(html)
                        .endswith("12_45AM.pdf"))


if __name__ == "__main__":
    unittest.main()
