"""103 Sports Intelligence Engine — ESPN player fan-out fallback.

Governing rule 10 (Isaac, Nov 17, 2026): when one account is suspended
due to whatever reason the next API takes its place and we keep going
like nothing happened.

The player fan-out (player_props_harvest) was single-sourced on
API-Sports, so its registry and per-game log stores stayed EMPTY —
first because the provider's player-statistics payload shapes were
never verified, then because the account was suspended outright.
This module is the next link in the chain: it serves the same
fan-out from ESPN's public endpoints (team rosters + per-athlete
game logs), translated into the API-Sports entry shape the fan-out's
own parser consumes, and feeds the results through the fan-out's own
machinery — parse_player_stat_rows, roster_records_from_rows,
upsert_harvested_player, GameLogStore — so the registry and the log
stores fill exactly as designed, under provider "espn".

Provider namespaces stay separate (registry canon: identity joins on
provider ID only, per provider). An ESPN athlete ID can never collide
with or merge into an api-sports record.

Honesty rules, mirroring the rest of the stack:
  * Raw per-team payloads land in the RawLanding BEFORE parsing,
    stamped params.source = "espn-fallback" — provenance is never
    hidden, and sources are never blended inside one payload.
  * Only statistics ESPN actually reports are emitted, and only
    keys the taxonomy knows (STAT_FIELD_CANDIDATES). A stat ESPN
    does not break out (e.g. offensive/defensive rebounds) stays
    absent — never derived, never invented.
  * Regular-season game logs only in v1 (the gamelog's own
    "Regular Season" split); postseason logs are a later add.
  * ESPN is keyless: this pass never touches the API-Sports budget
    ledger. It is still bounded — one season per run, rostered
    athletes only, and for football only the positions the prop
    taxonomy prices (QB/RB/FB/WR/TE).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from intelligence_harvest import ESPN_NAME_ALIASES, RawLanding
    from player_props_harvest import (
        SPORT_TO_LEAGUE,
        STAT_FIELD_CANDIDATES,
        parse_player_stat_rows,
        roster_records_from_rows,
        upsert_harvested_player,
    )
except ImportError:  # package-style import (intelligence.<module>)
    from intelligence.intelligence_harvest import (  # type: ignore
        ESPN_NAME_ALIASES,
        RawLanding,
    )
    from intelligence.player_props_harvest import (  # type: ignore
        SPORT_TO_LEAGUE,
        STAT_FIELD_CANDIDATES,
        parse_player_stat_rows,
        roster_records_from_rows,
        upsert_harvested_player,
    )

ESPN_PROVIDER = "espn"
SITE = "https://site.api.espn.com/apis/site/v2/sports"
V3 = "https://site.api.espn.com/apis/common/v3/sports"

ESPN_PLAYER_SPORTS = {
    "basketball": {"path": "basketball/nba"},
    "american-football": {"path": "football/nfl"},
}

# Positions the NFL prop taxonomy prices; everyone else's gamelog is
# never fetched (bounded by design).
NFL_PROP_POSITIONS = {"QB", "RB", "FB", "WR", "TE"}

# ESPN gamelog `names` -> taxonomy statistic_key. Names of the form
# "xMade-xAttempted" arrive as one "made-attempted" string and are
# split via PAIR_STAT_NAMES below.
SINGLE_STAT_NAMES = {
    "basketball": {
        "minutes": "minutes",
        "points": "points",
        "totalRebounds": "rebounds",
        "assists": "assists",
        "blocks": "blocks",
        "steals": "steals",
        "turnovers": "turnovers",
        "fieldGoalPct": "field_goal_percentage",
    },
    "american-football": {
        "completions": "completions",
        "passingAttempts": "passing_attempts",
        "passingYards": "passing_yards",
        "passingTouchdowns": "passing_touchdowns",
        "interceptions": "passing_interceptions",
        "rushingAttempts": "rushing_attempts",
        "rushingYards": "rushing_yards",
        "rushingTouchdowns": "rushing_touchdowns",
        "receptions": "receptions",
        "receivingYards": "receiving_yards",
        "receivingTouchdowns": "receiving_touchdowns",
        "targets": "targets",
        "longRushing": "longest_rush",
        "longReceiving": "longest_reception",
    },
}

PAIR_STAT_NAMES = {
    "fieldGoalsMade-fieldGoalsAttempted": (
        "field_goals_made", "field_goal_attempts"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": (
        "three_pointers_made", "three_point_attempts"),
    "freeThrowsMade-freeThrowsAttempted": (
        "free_throws_made", "free_throw_attempts"),
}


def _get(transport, url, params=None):
    return transport(url, params or {}, {"User-Agent": "Mozilla/5.0"})


def _gamelog_season(season_label: str) -> int:
    """ESPN gamelogs key winter seasons by END year (2024-2025 -> 2025)."""
    label = str(season_label)
    if "-" in label:
        tail = label.split("-")[1]
        return int(tail) if len(tail) == 4 else 2000 + int(tail)
    return int(label)


def espn_team_ids(sport: str, transport) -> dict:
    """{canonical team name: ESPN team id} for the league."""
    path = ESPN_PLAYER_SPORTS[sport]["path"]
    payload = _get(transport, f"{SITE}/{path}/teams")
    out = {}
    leagues = (payload.get("sports") or [{}])[0].get("leagues") or []
    teams = (leagues[0].get("teams") if leagues else []) or []
    for item in teams:
        team = item.get("team") or {}
        name = team.get("displayName")
        if name is None:
            continue
        canonical = ESPN_NAME_ALIASES.get(name, name)
        try:
            out[canonical] = int(team.get("id"))
        except (TypeError, ValueError):
            continue
    return out


def _roster_athletes(payload) -> list:
    """Flatten both ESPN roster shapes (flat list / position groups)."""
    athletes = []
    for item in (payload or {}).get("athletes") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("items"), list):
            for sub in item["items"]:
                if isinstance(sub, dict):
                    athletes.append(sub)
        else:
            athletes.append(item)
    return athletes


def _athlete_position(athlete: dict):
    pos = athlete.get("position")
    if isinstance(pos, dict):
        return pos.get("abbreviation")
    return None


def _translate_stats(sport: str, names: list, values: list) -> dict:
    """One gamelog stat line -> flat taxonomy-keyed stats.

    Only keys the taxonomy knows are emitted (the fan-out's
    STAT_FIELD_CANDIDATES is the contract); anything else ESPN
    reports is dropped, never renamed into existence.
    """
    known = STAT_FIELD_CANDIDATES.get(sport, {})
    singles = SINGLE_STAT_NAMES.get(sport, {})
    stats: dict = {}
    for name, raw in zip(names, values):
        if raw is None or raw == "":
            continue
        if name in PAIR_STAT_NAMES:
            made_key, att_key = PAIR_STAT_NAMES[name]
            parts = str(raw).split("-")
            if len(parts) == 2:
                for key, part in ((made_key, parts[0]), (att_key, parts[1])):
                    if key in known:
                        try:
                            stats[key] = float(part)
                        except ValueError:
                            pass
            continue
        key = singles.get(name)
        if key is not None and key in known:
            try:
                stats[key] = float(raw)
            except (TypeError, ValueError):
                pass
    return stats


def espn_player_entries(sport: str, season_label: str, athlete: dict,
                        team_id: int, team_name: str, transport) -> list:
    """One athlete's regular-season gamelog -> API-shaped entries."""
    path = ESPN_PLAYER_SPORTS[sport]["path"]
    athlete_id = athlete.get("id")
    payload = _get(
        transport,
        f"{V3}/{path}/athletes/{athlete_id}/gamelog",
        {"season": _gamelog_season(season_label)},
    )
    names = payload.get("names") or []
    events_meta = payload.get("events") or {}
    entries = []
    seen = set()
    for season_type in payload.get("seasonTypes") or []:
        if "Regular Season" not in (season_type.get("displayName") or ""):
            continue
        for category in season_type.get("categories") or []:
            for event in category.get("events") or []:
                event_id = event.get("eventId")
                if event_id is None or event_id in seen:
                    continue
                seen.add(event_id)
                stats = _translate_stats(sport, names, event.get("stats") or [])
                if not stats:
                    continue
                meta = events_meta.get(str(event_id)) or {}
                try:
                    game_id = int(event_id)
                    player_id = int(athlete_id)
                except (TypeError, ValueError):
                    continue
                entry = {
                    "player": {"id": player_id,
                               "name": athlete.get("displayName")},
                    "team": {"id": team_id, "name": team_name},
                    "game": {"id": game_id, "date": meta.get("gameDate")},
                }
                entry.update(stats)
                entries.append(entry)
    return entries


def run_espn_fanout(sport: str, season: str, team_names, *, transport,
                    landing: RawLanding, registry, log_store,
                    effective_date: str | None = None) -> dict:
    """One ESPN fallback pass over the given teams (canonical names).

    Mirrors player_props_harvest.run_fanout's store -> parse ->
    register -> append flow, under provider "espn", with no
    API-Sports budget involvement. Per-team and per-athlete failures
    are counted in the report, never fatal and never hidden.
    """
    league = SPORT_TO_LEAGUE[sport]
    season = str(season)
    effective = effective_date or datetime.now(
        timezone.utc).strftime("%Y-%m-%d")
    team_ids = espn_team_ids(sport, transport)

    report = {
        "source": "espn-fallback",
        "sport": sport,
        "league": league,
        "season": season,
        "teams_planned": len(team_names),
        "teams_fetched": 0,
        "teams_missing": 0,
        "athletes_fetched": 0,
        "athlete_errors": 0,
        "players_registered": 0,
        "players_updated": 0,
        "games_added": 0,
        "games_corrected": 0,
        "files": [],
    }

    covered = []
    for team_name in team_names:
        espn_id = team_ids.get(team_name)
        if espn_id is None:
            report["teams_missing"] += 1
            continue
        path = ESPN_PLAYER_SPORTS[sport]["path"]
        try:
            roster = _get(transport, f"{SITE}/{path}/teams/{espn_id}/roster")
        except Exception:  # noqa: BLE001 — counted, pass continues
            report["teams_missing"] += 1
            continue
        entries = []
        for athlete in _roster_athletes(roster):
            if sport == "american-football" and \
                    _athlete_position(athlete) not in NFL_PROP_POSITIONS:
                continue
            try:
                entries.extend(espn_player_entries(
                    sport, season, athlete, espn_id, team_name, transport))
                report["athletes_fetched"] += 1
            except Exception:  # noqa: BLE001 — counted, pass continues
                report["athlete_errors"] += 1
        payload = {
            "get": "players/statistics",
            "parameters": {"team": team_name, "season": season,
                           "source": "espn-fallback"},
            "errors": [],
            "results": len(entries),
            "response": entries,
        }
        stored = landing.store(
            sport, "player_statistics", "/players/statistics",
            {"team": team_name, "season": season,
             "source": "espn-fallback"}, payload)
        report["files"].append(str(stored))
        report["teams_fetched"] += 1
        covered.append(team_name)

        rows = parse_player_stat_rows(sport, payload, season=season)
        for record in roster_records_from_rows(rows).values():
            record["league"] = league
            result = upsert_harvested_player(
                registry, record, effective, provider=ESPN_PROVIDER)
            if result["skipped"]:
                continue
            if "registered" in result["changes"]:
                report["players_registered"] += 1
            elif result["changes"]:
                report["players_updated"] += 1
        for row in rows:
            if row.get("game_id") is None:
                continue
            player = registry.get_by_provider_id(
                ESPN_PROVIDER, row.get("provider_player_id"))
            if player is None:
                continue
            outcome = log_store.append_game(player["player_id"], {
                "game_id": row["game_id"],
                "date": row.get("date"),
                "season": row.get("season"),
                "team": row.get("team_name"),
                "stats": row.get("stats") or {},
            })
            if outcome["added"]:
                report["games_added"] += 1
            if outcome["corrected"]:
                report["games_corrected"] += 1

    log_store.set_coverage({
        "sport": sport,
        "season": season,
        "teams_covered": covered,
        "teams_total": len(team_names),
        "as_of": effective,
        "source": "espn-fallback",
    })
    return report
