"""gapfill_stats_nhl.py — Sage Gap-Filler, Phase 2 collector (S2).

Collects CURRENT-SEASON NHL player game logs from the league's own
public web API (api-web.nhle.com, design input S2, grade B) and
publishes them as a COLLECTED layer beside the Intelligence Engine's
provider data — never inside it. Spec:
~/workspace/your_files/sage-gap-filler-design-input-2026-11-16.md
(Phase 2; gap G2/G3 for hockey; decisions D4/D5 settled Nov 16,
2026: collected data serves answers only, and collected seasons do
not count in the aggregates' season labels).

Binding rules inherited from that spec:
  * Provenance is the product. Every published game record carries
    the section 4.2 provenance block: origin "collected", source
    name, the exact URL fetched, fetched-at, collector name,
    classification "Historical" (a collected record NEVER surfaces
    as Live), and an expiry (stats class, section 4.5: next refresh,
    hard cap 36 hours).
  * Empty slots only. This collector publishes to its own layer
    (collected/game_logs_NHL.json); it writes nothing into provider
    aggregates or provider game-log stores. The one shared file it
    maintains is the permanent player REGISTRY — identity
    infrastructure, not provider data — because section 4.3 keys
    records on permanent registry IDs and NHL players held none.
  * Identity joins ONLY on provider ID (the registry canon in
    player_props_harvest.upsert_harvested_player): NHL players are
    registered under provider "nhl" from the roster payloads
    themselves. A roster entry that cannot be keyed (no provider
    ID, no name) is STAGED, never guessed into a record.
  * Raw payloads land append-only under raw/collected/
    nhl_game_logs/<date>/ BEFORE parsing, so any record can be
    re-derived and audited against what was actually fetched.
  * Failures are visible with reason classes. A failed run (the
    API unreachable, no roster fetched, nothing keyed) publishes
    nothing new and leaves the previous layer AND the registry
    file untouched. Per-team / per-player failures inside an
    otherwise successful run are counted, named in the staging
    file, and mark the published document partial: true.
  * Bounded: a fixed target list (the 32 teams below), one roster
    fetch per team plus one game-log fetch per rostered player,
    one retry on a network error, no crawling. The collector
    cannot be pointed at an arbitrary URL.

Season: the current season is resolved from the API itself —
/roster/{team}/current redirects to /roster/{team}/{seasonId},
and the season is read from the final URL (verified live
2026-11-21: current = 20262027). --season pins an explicit season
instead. Game type 2 is the regular season.

Response shapes were verified against live responses on
2026-11-21 and frozen as fixtures under test_fixtures/
(nhl_roster_edm_20262027.json, nhl_gamelog_skater_8478402_
20262027.json, nhl_gamelog_goalie_8479973_20262027.json).

CLI:
  python3 gapfill_stats_nhl.py --data-root <path> [--season S]
Writes <data-root>/collected/game_logs_NHL.json (published layer),
<data-root>/collected/game_logs_NHL_staging.json (unkeyed and
fetch-failed players), the raw payloads under
<data-root>/raw/collected/nhl_game_logs/<date>/, updates
<data-root>/props/player_registry.json in place, and prints a
run report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from player_props_harvest import upsert_harvested_player
from player_registry import PlayerRegistry

API_BASE = "https://api-web.nhle.com/v1"
SOURCE_NAME = "NHL official web API"
COLLECTOR = "gapfill_stats_nhl"
PROVIDER = "nhl"
LEAGUE = "NHL"
GAME_TYPE = 2  # regular season
EXPIRY = timedelta(hours=36)  # spec 4.5 stats class, hard cap
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")

# Fixed target list: the 32 teams, tricode -> canonical full name.
# Tricodes and names verified against the league's own standings
# payload on 2026-11-21.
TEAMS = {
    "ANA": "Anaheim Ducks", "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres", "CAR": "Carolina Hurricanes",
    "CBJ": "Columbus Blue Jackets", "CGY": "Calgary Flames",
    "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche",
    "DAL": "Dallas Stars", "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild",
    "MTL": "Montréal Canadiens", "NJD": "New Jersey Devils",
    "NSH": "Nashville Predators", "NYI": "New York Islanders",
    "NYR": "New York Rangers", "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins",
    "SEA": "Seattle Kraken", "SJS": "San Jose Sharks",
    "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs", "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets", "WSH": "Washington Capitals",
}

_ROSTER_GROUPS = ("forwards", "defensemen", "goalies")

# API field -> stored statistic key. A statistic a game does not
# carry stays ABSENT from that game's stats — never zero-filled.
SKATER_STATS = {
    "goals": "goals", "assists": "assists", "points": "points",
    "plusMinus": "plus_minus", "pim": "pim", "shots": "shots",
    "powerPlayGoals": "power_play_goals",
    "powerPlayPoints": "power_play_points",
    "shorthandedGoals": "shorthanded_goals",
    "shorthandedPoints": "shorthanded_points",
    "gameWinningGoals": "game_winning_goals",
    "otGoals": "ot_goals", "shifts": "shifts", "toi": "toi",
}
GOALIE_STATS = {
    "gamesStarted": "games_started", "decision": "decision",
    "shotsAgainst": "shots_against", "goalsAgainst": "goals_against",
    "savePctg": "save_pct", "shutouts": "shutouts",
    "goals": "goals", "assists": "assists", "pim": "pim",
    "toi": "toi",
}


# ---------------------------------------------------------------- fetch

def _fetch(url: str) -> tuple[bytes, str]:
    """(body, final URL after redirects)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.geturl()


def _fetch_with_retry(url: str) -> tuple[bytes, str]:
    try:
        return _fetch(url)
    except Exception:
        return _fetch(url)  # one retry, then the failure propagates


def _reason_class(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "HTTPError":
        return "http"
    if "Timeout" in name or "timeout" in str(exc).lower():
        return "timeout"
    return "network"


def season_from_roster_url(url: str) -> str | None:
    m = re.search(r"/roster/[A-Z]{3}/(\d{8})(?:[/?]|$)", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------- parse

def parse_roster(payload: dict) -> tuple[list[dict], list[dict]]:
    """Roster payload -> (keyable entries, unparseable raw entries).

    An entry: {nhl_id, full_name, position, player_type}. Entries
    missing an id or a name cannot be keyed to the registry; they
    are returned separately for staging, never repaired.
    """
    entries, unparseable = [], []
    for group in _ROSTER_GROUPS:
        for raw in payload.get(group) or []:
            first = (raw.get("firstName") or {}).get("default")
            last = (raw.get("lastName") or {}).get("default")
            if raw.get("id") is None or not first or not last:
                unparseable.append(raw)
                continue
            entries.append({
                "nhl_id": raw["id"],
                "full_name": f"{first} {last}",
                "position": raw.get("positionCode"),
                "player_type": "goalie" if group == "goalies"
                               else "skater",
            })
    return entries, unparseable


def parse_game_log(payload: dict, player_type: str) -> tuple[list[dict], int]:
    """Game-log payload -> (game entries, dropped count).

    A game entry: {game_id, date, team_abbrev, opponent_abbrev,
    home_road, stats}. Entries missing a game id or date are
    dropped and counted, never repaired into plausible data.
    """
    mapping = GOALIE_STATS if player_type == "goalie" else SKATER_STATS
    games, dropped = [], 0
    for raw in payload.get("gameLog") or []:
        if raw.get("gameId") is None or not raw.get("gameDate"):
            dropped += 1
            continue
        stats = {}
        for api_key, key in mapping.items():
            if api_key in raw and raw[api_key] is not None:
                stats[key] = raw[api_key]
        games.append({
            "game_id": raw["gameId"],
            "date": raw["gameDate"],
            "team_abbrev": raw.get("teamAbbrev"),
            "opponent_abbrev": raw.get("opponentAbbrev"),
            "home_road": raw.get("homeRoadFlag"),
            "stats": stats,
        })
    games.sort(key=lambda g: (g["date"], str(g["game_id"])))
    return games, dropped


# ---------------------------------------------------------------- records

def build_game_record(game: dict, player: dict, season: str,
                      source_url: str, fetched_at: datetime) -> dict:
    """One game entry -> a published record with the 4.2 block."""
    expires = fetched_at + EXPIRY
    fetched_iso = fetched_at.astimezone(timezone.utc).isoformat()
    expires_iso = expires.astimezone(timezone.utc).isoformat()
    return {
        "player_id": player["player_id"],
        "player_name": player["full_name"],
        "team": player["team"],
        "team_abbrev": game["team_abbrev"],
        "position": player["position"],
        "player_type": player["player_type"],
        "game_id": game["game_id"],
        "date": game["date"],
        "season": season,
        "opponent": game["opponent_abbrev"],
        "home_road": game["home_road"],
        "stats": game["stats"],
        "collected": True,
        "expires_at": expires_iso,
        "provenance": {
            "origin": "collected",
            "source_name": SOURCE_NAME,
            "source_url": source_url,
            "fetched_at": fetched_iso,
            "collector": COLLECTOR,
            "classification": "Historical",
            "expires_at": expires_iso,
            "supersedes": None,
        },
    }


# ---------------------------------------------------------------- run

def run(data_root: Path, season: str | None = None,
        teams: list[str] | None = None,
        fetch=_fetch_with_retry,
        now: datetime | None = None) -> dict:
    fetched_at = now or datetime.now(timezone.utc)
    fetched_iso = fetched_at.astimezone(timezone.utc).isoformat()
    effective = fetched_at.strftime("%Y-%m-%d")
    target_teams = teams or sorted(TEAMS)

    landing_dir = data_root / "raw" / "collected" / "nhl_game_logs" \
        / effective
    logs_dir = landing_dir / "game_logs"

    def land(name: str, body: bytes, subdir: Path) -> Path:
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / name
        if not path.exists():
            path.write_bytes(body)
        return path

    # -- rosters first: they carry the rostered identities, and the
    # first /current response resolves the current season.
    rosters: dict[str, list[dict]] = {}
    staged: list[dict] = []
    failures: list[dict] = []
    teams_fetched = 0
    resolved_season = season
    for tricode in target_teams:
        if season:
            url = f"{API_BASE}/roster/{tricode}/{season}"
        else:
            url = f"{API_BASE}/roster/{tricode}/current"
        try:
            body, final_url = fetch(url)
        except Exception as exc:  # noqa: BLE001 — reported, not hidden
            failures.append({"target": f"roster:{tricode}",
                             "reason_class": _reason_class(exc),
                             "detail": type(exc).__name__})
            continue
        land(f"{tricode}_roster.json", body, landing_dir)
        if resolved_season is None:
            resolved_season = season_from_roster_url(final_url)
        try:
            payload = json.loads(body)
            entries, unparseable = parse_roster(payload)
        except Exception as exc:  # noqa: BLE001
            failures.append({"target": f"roster:{tricode}",
                             "reason_class": "parse",
                             "detail": type(exc).__name__})
            continue
        teams_fetched += 1
        rosters[tricode] = entries
        for raw in unparseable:
            staged.append({"staging_reason": "unparseable_roster_entry",
                           "team_abbrev": tricode, "raw": raw})

    if teams_fetched == 0:
        return {"status": "failed", "reason_class": "network",
                "detail": "no roster fetched", "failures": failures}
    if resolved_season is None:
        return {"status": "failed", "reason_class": "parse",
                "detail": "current season not resolvable from "
                          "roster redirect", "failures": failures}

    # -- identity: register every keyable rostered player under
    # provider "nhl" (provider-ID joins only, the registry canon).
    registry_path = data_root / "props" / "player_registry.json"
    registry = PlayerRegistry.load(registry_path) \
        if registry_path.exists() else PlayerRegistry.empty()
    keyed: dict[str, dict] = {}  # tricode -> {nhl_id: player entry}
    registered = updated = 0
    for tricode, entries in rosters.items():
        keyed[tricode] = {}
        for entry in entries:
            result = upsert_harvested_player(registry, {
                "provider_id": entry["nhl_id"],
                "full_name": entry["full_name"],
                "team_name": TEAMS[tricode],
                "league": LEAGUE,
                "position": entry["position"],
            }, effective, provider=PROVIDER)
            if result["skipped"]:
                staged.append({
                    "staging_reason": result["skipped"],
                    "team_abbrev": tricode,
                    "player_name": entry["full_name"],
                    "nhl_id": entry["nhl_id"]})
                continue
            if "registered" in result["changes"]:
                registered += 1
            elif result["changes"]:
                updated += 1
            keyed[tricode][entry["nhl_id"]] = {
                **entry,
                "player_id": result["player"]["player_id"],
                "team": TEAMS[tricode],
            }

    # -- game logs, one fetch per keyed player.
    players_doc: dict[str, dict] = {}
    games_published = dropped_entries = 0
    for tricode in target_teams:
        for nhl_id, player in (keyed.get(tricode) or {}).items():
            url = (f"{API_BASE}/player/{nhl_id}/game-log/"
                   f"{resolved_season}/{GAME_TYPE}")
            try:
                body, _final = fetch(url)
            except Exception as exc:  # noqa: BLE001
                cls = _reason_class(exc)
                failures.append({
                    "target": f"game-log:{nhl_id}",
                    "reason_class": cls,
                    "detail": type(exc).__name__})
                staged.append({
                    "staging_reason": f"fetch_failed_{cls}",
                    "team_abbrev": tricode,
                    "player_id": player["player_id"],
                    "player_name": player["full_name"],
                    "nhl_id": nhl_id})
                continue
            land(f"{nhl_id}.json", body, logs_dir)
            try:
                payload = json.loads(body)
                games, dropped = parse_game_log(
                    payload, player["player_type"])
            except Exception as exc:  # noqa: BLE001
                failures.append({
                    "target": f"game-log:{nhl_id}",
                    "reason_class": "parse",
                    "detail": type(exc).__name__})
                staged.append({
                    "staging_reason": "parse_failed",
                    "team_abbrev": tricode,
                    "player_id": player["player_id"],
                    "player_name": player["full_name"],
                    "nhl_id": nhl_id})
                continue
            dropped_entries += dropped
            records = [build_game_record(g, player, resolved_season,
                                         url, fetched_at)
                       for g in games]
            games_published += len(records)
            players_doc[player["player_id"]] = {
                "player_id": player["player_id"],
                "full_name": player["full_name"],
                "team": player["team"],
                "team_abbrev": tricode,
                "position": player["position"],
                "player_type": player["player_type"],
                "nhl_id": nhl_id,
                "games": records,
            }

    if not players_doc:
        return {"status": "failed", "reason_class": "parse",
                "detail": "no players published",
                "failures": failures}

    partial = bool(failures or staged)
    collected_dir = data_root / "collected"
    collected_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "kind": "collected-game-logs",
        "league": LEAGUE,
        "season": resolved_season,
        "game_type": GAME_TYPE,
        "classification": "Historical",
        "collected": True,
        "generated_at": fetched_iso,
        "partial": partial,
        "source": {
            "source_name": SOURCE_NAME,
            "api_base": API_BASE,
            "fetched_at": fetched_iso,
            "raw_landing": str(landing_dir),
        },
        "players": players_doc,
        "counts": {
            "teams_fetched": teams_fetched,
            "teams_failed": len(target_teams) - teams_fetched,
            "players_registered": registered,
            "players_updated": updated,
            "players_published": len(players_doc),
            "games_published": games_published,
            "staged": len(staged),
            "fetch_failures": len(failures),
            "dropped_entries": dropped_entries,
        },
    }
    (collected_dir / "game_logs_NHL.json").write_text(
        json.dumps(doc, indent=1))
    (collected_dir / "game_logs_NHL_staging.json").write_text(
        json.dumps({
            "kind": "collected-game-logs-staging",
            "league": LEAGUE,
            "season": resolved_season,
            "generated_at": fetched_iso,
            "staged": staged,
            "failures": failures,
        }, indent=1))
    if registered or updated:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry.save(registry_path)
    return {"status": "ok", "season": resolved_season,
            "partial": partial,
            "out": str(collected_dir / "game_logs_NHL.json"),
            **doc["counts"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/intelligence")
    parser.add_argument("--season", default=None,
                        help="explicit season, e.g. 20262027 "
                             "(default: the API's current season)")
    parser.add_argument("--teams", default=None,
                        help="comma-separated tricodes (default: "
                             "all 32)")
    args = parser.parse_args(argv)
    teams = [t.strip() for t in args.teams.split(",")] \
        if args.teams else None
    result = run(Path(args.data_root), season=args.season,
                 teams=teams)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
