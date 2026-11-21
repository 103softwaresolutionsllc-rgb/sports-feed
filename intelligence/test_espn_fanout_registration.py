"""ESPN fan-out registration: the complete record reaches the registry.

Regression coverage for the Nov 21, 2026 unification: the fan-out's
roster pass always KNEW each athlete's position (it filters NFL
fetches by it), but registration records were derived from gamelog
stat rows, which carry no position — so every fan-out-registered
player landed in the registry with position missing. The fan-out
now threads roster positions into upsert_harvested_player, the one
registration path every league registers through.
"""

import unittest

from intelligence_espn_players import run_espn_fanout
from player_game_logs import GameLogStore
from player_registry import PlayerRegistry

TEAMS_PAYLOAD = {
    "sports": [{"leagues": [{"teams": [
        {"team": {"id": 12, "displayName": "Kansas City Chiefs"}},
    ]}]}],
}

ROSTER_PAYLOAD = {
    "athletes": [
        {"id": 3139477, "displayName": "Patrick Mahomes",
         "position": {"abbreviation": "QB"}},
        {"id": 555000, "displayName": "Fixture Interior Lineman",
         "position": {"abbreviation": "G"}},
    ],
}

GAMELOG_PAYLOAD = {
    "names": ["passingYards", "passingTouchdowns", "rushingYards"],
    "events": {
        "401": {"gameDate": "2026-11-15T18:00:00Z",
                "team": {"id": 12}},
    },
    "seasonTypes": [{
        "displayName": "Regular Season",
        "categories": [{"events": [
            {"eventId": 401, "stats": ["300", "2", "15"]},
        ]}],
    }],
}


def _transport(url, params, headers):
    if url.endswith("/football/nfl/teams"):
        return TEAMS_PAYLOAD
    if url.endswith("/football/nfl/teams/12/roster"):
        return ROSTER_PAYLOAD
    if "/athletes/3139477/gamelog" in url:
        return GAMELOG_PAYLOAD
    raise AssertionError(f"unexpected url: {url}")


class _StubLanding:
    def __init__(self):
        self.stored = []

    def store(self, sport, kind, endpoint, params, payload):
        self.stored.append((sport, kind))
        return f"/stub/{sport}/{kind}.json"


class EspnFanoutRegistrationTests(unittest.TestCase):
    def _run(self, registry, log_store):
        return run_espn_fanout(
            "american-football", "2026", ["Kansas City Chiefs"],
            transport=_transport, landing=_StubLanding(),
            registry=registry, log_store=log_store,
            effective_date="2026-11-21")

    def test_position_threaded_into_registry_record(self):
        registry = PlayerRegistry.empty()
        log_store = GameLogStore.empty("NFL")
        report = self._run(registry, log_store)
        self.assertEqual(report["players_registered"], 1)
        players = registry.all_players()
        # The guard is outside the priced positions: never fetched,
        # never registered.
        self.assertEqual(len(players), 1)
        player = players[0]
        self.assertEqual(player["full_name"], "Patrick Mahomes")
        self.assertEqual(player["position"], "QB")
        self.assertEqual(player["league"], "NFL")
        self.assertEqual(player["provider_ids"], {"espn": 3139477})
        self.assertEqual(registry.current_team(player),
                         "Kansas City Chiefs")
        self.assertEqual(log_store.games_count(player["player_id"]), 1)

    def test_rerun_is_idempotent_no_duplicate_registration(self):
        registry = PlayerRegistry.empty()
        log_store = GameLogStore.empty("NFL")
        self._run(registry, log_store)
        report = self._run(registry, log_store)
        self.assertEqual(report["players_registered"], 0)
        self.assertEqual(report["games_added"], 0)
        players = registry.all_players()
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["position"], "QB")
        self.assertEqual(log_store.games_count(players[0]["player_id"]), 1)


if __name__ == "__main__":
    unittest.main()
