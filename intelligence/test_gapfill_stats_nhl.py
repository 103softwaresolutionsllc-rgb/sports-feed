"""Unit tests for gapfill_stats_nhl — the Sage Gap-Filler's
Phase 2 NHL collector (league official web API, source S2).

The parser tests run against REAL recorded responses (Phase 0
fixture discipline from the Nov 16 spec): the league's actual
2026-11-21 EDM roster and game logs for Connor McDavid (skater)
and Stuart Skinner (goalie), frozen under test_fixtures/.
Everything else (registration, staging, failure behavior) uses a
fake transport serving those fixtures plus small hand-built
inputs — formula checks only, never a validation basis.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gapfill_stats_nhl as gf

FIXTURES = Path(__file__).parent / "test_fixtures"
ROSTER = FIXTURES / "nhl_roster_edm_20262027.json"
SKATER_LOG = FIXTURES / "nhl_gamelog_skater_8478402_20262027.json"
GOALIE_LOG = FIXTURES / "nhl_gamelog_goalie_8479973_20262027.json"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _fake_fetch(roster_by_team=None, log_by_id=None, fail=()):
    """fetch(url) -> (bytes, final_url) serving the fixtures."""
    roster_by_team = roster_by_team or {}
    log_by_id = log_by_id or {}

    def fetch(url):
        if url in fail:
            raise ConnectionError("boom")
        if "/roster/" in url:
            # .../roster/{TRICODE}/{season-or-current}
            tricode = url.rstrip("/").split("/")[-2]
            body = roster_by_team.get(tricode)
            if body is None:
                raise ConnectionError(f"no roster for {tricode}")
            final = url.replace("/current", "/20262027")
            return body, final
        if "/player/" in url:
            nhl_id = int(url.split("/player/")[1].split("/")[0])
            body = log_by_id.get(nhl_id)
            if body is None:
                body = json.dumps({"seasonId": 20262027,
                                   "gameTypeId": 2,
                                   "gameLog": []}).encode()
            return body, url
        raise AssertionError(f"unexpected url {url}")

    return fetch


def _edm_fetch(**kw):
    return _fake_fetch(
        roster_by_team={"EDM": ROSTER.read_bytes()},
        log_by_id={8478402: SKATER_LOG.read_bytes()}, **kw)


class TargetListTests(unittest.TestCase):
    def test_32_teams(self):
        # The 32 tricodes verified against the league's standings
        # payload on 2026-11-21.
        self.assertEqual(len(gf.TEAMS), 32)
        self.assertEqual(set(gf.TEAMS), {
            "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL",
            "DAL", "DET", "EDM", "FLA", "LAK", "MIN", "MTL", "NJD",
            "NSH", "NYI", "NYR", "OTT", "PHI", "PIT", "SEA", "SJS",
            "STL", "TBL", "TOR", "UTA", "VAN", "VGK", "WPG", "WSH"})
        self.assertEqual(gf.TEAMS["EDM"], "Edmonton Oilers")
        self.assertEqual(gf.TEAMS["UTA"], "Utah Mammoth")


class RosterParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries, cls.unparseable = gf.parse_roster(
            json.loads(ROSTER.read_text()))

    def test_full_roster_parses(self):
        self.assertEqual(len(self.entries), 27)  # 16 F + 8 D + 3 G
        self.assertEqual(self.unparseable, [])
        types = [e["player_type"] for e in self.entries]
        self.assertEqual(types.count("goalie"), 3)
        self.assertEqual(types.count("skater"), 24)

    def test_known_player(self):
        mcdavid = [e for e in self.entries
                   if e["nhl_id"] == 8478402]
        self.assertEqual(len(mcdavid), 1)
        self.assertEqual(mcdavid[0]["full_name"], "Connor McDavid")
        self.assertEqual(mcdavid[0]["position"], "C")
        self.assertEqual(mcdavid[0]["player_type"], "skater")

    def test_unparseable_entries_are_separated_not_repaired(self):
        entries, unparseable = gf.parse_roster({"forwards": [
            {"firstName": {"default": "No"},
             "lastName": {"default": "Id"}}]})
        self.assertEqual(entries, [])
        self.assertEqual(len(unparseable), 1)


class GameLogParseTests(unittest.TestCase):
    def test_skater_log(self):
        games, dropped = gf.parse_game_log(
            json.loads(SKATER_LOG.read_text()), "skater")
        self.assertEqual(len(games), 24)
        self.assertEqual(dropped, 0)
        # Chronological after the sort (API serves newest first);
        # the frozen log's earliest game is 2026-09-29 vs VAN.
        self.assertEqual(games[0]["date"], "2026-09-29")
        self.assertEqual(games[0]["opponent_abbrev"], "VAN")
        latest = games[-1]
        self.assertEqual(latest["date"], "2026-11-19")
        self.assertEqual(latest["opponent_abbrev"], "TBL")
        self.assertEqual(latest["home_road"], "R")
        self.assertEqual(latest["stats"]["shots"], 2)
        self.assertEqual(latest["stats"]["toi"], "22:57")
        self.assertEqual(latest["stats"]["plus_minus"], -1)

    def test_goalie_log(self):
        games, dropped = gf.parse_game_log(
            json.loads(GOALIE_LOG.read_text()), "goalie")
        self.assertEqual(len(games), 16)
        self.assertEqual(dropped, 0)
        latest = games[-1]
        self.assertEqual(latest["date"], "2026-11-17")
        self.assertEqual(latest["stats"]["games_started"], 1)
        self.assertEqual(latest["stats"]["decision"], "L")
        self.assertEqual(latest["stats"]["shots_against"], 27)
        self.assertEqual(latest["stats"]["goals_against"], 2)
        self.assertAlmostEqual(latest["stats"]["save_pct"],
                               0.925926, places=5)
        # A statistic the API does not return for goalies stays
        # absent — never zero-filled.
        self.assertNotIn("points", latest["stats"])
        self.assertNotIn("shots", latest["stats"])

    def test_malformed_entries_dropped_and_counted(self):
        games, dropped = gf.parse_game_log({"gameLog": [
            {"gameDate": "2026-11-01"},          # no gameId
            {"gameId": 123},                     # no date
            {"gameId": 124, "gameDate": "2026-11-02", "goals": 1},
        ]}, "skater")
        self.assertEqual(len(games), 1)
        self.assertEqual(dropped, 2)


class SeasonResolutionTests(unittest.TestCase):
    def test_season_from_redirect_url(self):
        self.assertEqual(gf.season_from_roster_url(
            "https://api-web.nhle.com/v1/roster/EDM/20262027"),
            "20262027")
        self.assertIsNone(gf.season_from_roster_url(
            "https://api-web.nhle.com/v1/roster/EDM/current"))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_to_end_edm(self):
        result = gf.run(self.root, teams=["EDM"],
                        fetch=_edm_fetch(), now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["season"], "20262027")
        self.assertEqual(result["teams_fetched"], 1)
        self.assertEqual(result["players_registered"], 27)
        self.assertEqual(result["players_published"], 27)
        self.assertEqual(result["games_published"], 24)
        self.assertEqual(result["staged"], 0)
        self.assertFalse(result["partial"])

        doc = json.loads(
            (self.root / "collected" / "game_logs_NHL.json")
            .read_text())
        self.assertEqual(doc["kind"], "collected-game-logs")
        self.assertTrue(doc["collected"])
        self.assertEqual(doc["classification"], "Historical")
        # McDavid's record, keyed to the permanent registry.
        registry = json.loads(
            (self.root / "props" / "player_registry.json")
            .read_text())
        mcdavid = [p for p in registry["players"]
                   if p["full_name"] == "Connor McDavid"]
        self.assertEqual(len(mcdavid), 1)
        self.assertEqual(mcdavid[0]["provider_ids"],
                         {"nhl": 8478402})
        self.assertEqual(mcdavid[0]["league"], "NHL")
        self.assertEqual(mcdavid[0]["team_history"][-1]["team"],
                         "Edmonton Oilers")
        entry = doc["players"][mcdavid[0]["player_id"]]
        self.assertEqual(len(entry["games"]), 24)
        # Raw landing exists for the roster and the game log.
        landing = (self.root / "raw" / "collected"
                   / "nhl_game_logs" / "2026-11-21")
        self.assertTrue((landing / "EDM_roster.json").exists())
        self.assertTrue(
            (landing / "game_logs" / "8478402.json").exists())

    def test_provenance_complete_on_every_record(self):
        result = gf.run(self.root, teams=["EDM"],
                        fetch=_edm_fetch(), now=NOW)
        self.assertEqual(result["status"], "ok")
        doc = json.loads(
            (self.root / "collected" / "game_logs_NHL.json")
            .read_text())
        required = {"origin", "source_name", "source_url",
                    "fetched_at", "collector", "classification",
                    "expires_at", "supersedes"}
        seen = 0
        for player in doc["players"].values():
            for game in player["games"]:
                seen += 1
                prov = game["provenance"]
                self.assertEqual(set(prov), required)
                self.assertEqual(prov["origin"], "collected")
                self.assertEqual(prov["classification"],
                                 "Historical")
                self.assertEqual(prov["collector"],
                                 "gapfill_stats_nhl")
                self.assertIn("/player/8478402/game-log/20262027/2",
                              prov["source_url"])
                self.assertTrue(game["collected"])
                # Stats expiry: fetched_at + 36h, hard cap (4.5).
                self.assertEqual(
                    datetime.fromisoformat(prov["expires_at"])
                    - datetime.fromisoformat(prov["fetched_at"]),
                    timedelta(hours=36))
        self.assertEqual(seen, 24)

    def test_identity_joins_on_provider_id_only(self):
        # Two humans sharing a name must never merge: distinct
        # provider IDs produce distinct registry records.
        roster = {"forwards": [
            {"id": 111, "firstName": {"default": "Sam"},
             "lastName": {"default": "Same"},
             "positionCode": "C"},
            {"id": 222, "firstName": {"default": "Sam"},
             "lastName": {"default": "Same"},
             "positionCode": "L"}]}
        fetch = _fake_fetch(
            roster_by_team={"EDM": json.dumps(roster).encode()})
        result = gf.run(self.root, teams=["EDM"], fetch=fetch,
                        now=NOW)
        self.assertEqual(result["status"], "ok")
        registry = json.loads(
            (self.root / "props" / "player_registry.json")
            .read_text())
        sams = [p for p in registry["players"]
                if p["full_name"] == "Sam Same"]
        self.assertEqual(len(sams), 2)
        self.assertEqual(
            {p["provider_ids"]["nhl"] for p in sams}, {111, 222})

    def test_game_log_fetch_failure_stages_player(self):
        # McDavid's log fetch fails; the other 26 rostered players
        # return empty logs. The run publishes partial, McDavid is
        # staged with the failure class, and he does NOT appear in
        # the published layer with an invented empty record.
        base = _edm_fetch()

        def fetch(url):
            if "/player/8478402/" in url:
                raise ConnectionError("down")
            return base(url)

        result = gf.run(self.root, teams=["EDM"], fetch=fetch,
                        now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["partial"])
        self.assertEqual(result["players_published"], 26)
        self.assertEqual(result["games_published"], 0)
        self.assertEqual(result["fetch_failures"], 1)
        staging = json.loads(
            (self.root / "collected" / "game_logs_NHL_staging.json")
            .read_text())
        self.assertEqual(len(staging["staged"]), 1)
        self.assertEqual(staging["staged"][0]["staging_reason"],
                         "fetch_failed_network")
        self.assertEqual(staging["staged"][0]["player_name"],
                         "Connor McDavid")
        doc = json.loads(
            (self.root / "collected" / "game_logs_NHL.json")
            .read_text())
        names = {p["full_name"] for p in doc["players"].values()}
        self.assertNotIn("Connor McDavid", names)

    def test_total_failure_leaves_previous_layer_untouched(self):
        layer = self.root / "collected" / "game_logs_NHL.json"
        layer.parent.mkdir(parents=True)
        layer.write_text('{"sentinel": true}')

        def fetch(url):
            raise ConnectionError("api down")

        result = gf.run(self.root, teams=["EDM"], fetch=fetch,
                        now=NOW)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_class"], "network")
        self.assertEqual(layer.read_text(), '{"sentinel": true}')
        # The registry file is not created or touched either.
        self.assertFalse(
            (self.root / "props" / "player_registry.json").exists())


if __name__ == "__main__":
    unittest.main()
