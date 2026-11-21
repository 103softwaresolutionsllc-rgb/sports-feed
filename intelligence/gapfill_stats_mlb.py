"""gapfill_stats_mlb.py — Sage Gap-Filler, Phase 2 collector (G2/G3).

Collects MLB current-season player game logs from the MLB Stats API
(design input S3, grade B — the league's official stats API) and
publishes them as a COLLECTED layer beside the Intelligence Engine's
provider data — never inside it. Spec:
~/workspace/your_files/sage-gap-filler-design-input-2026-11-16.md
(Phase 2; decisions D4/D5 settled Nov 16, 2026: collected data serves
answers and displays only, and collected seasons never count in the
aggregates' provider season labels).

Binding rules inherited from that spec:
  * Provenance is the product. Every published game record carries
    the section 4.2 provenance block: origin "collected", source
    name, the exact URL fetched, fetched-at, collector name,
    classification "Historical" (a collected record NEVER surfaces
    as Live), and an expiry.
  * Expiry (spec 4.5): current-season statistics expire at the next
    scheduled refresh for the source, hard cap 36 hours. The
    collected layer is built for that refresh cadence; consumers
    treat an expired record as absent, never as fresh.
  * Empty slots only. This collector publishes to its own layer
    (collected/game_logs_MLB.json); it writes nothing into provider
    aggregates or provider game-log stores.
  * Identity: players key to the permanent player registry by
    PROVIDER ID ONLY (provider_ids.mlb), via the registry machinery
    (player_registry.PlayerRegistry + the harvest's
    upsert_harvested_player) — never by name join. A record that
    cannot be keyed is STAGED, not published — visible in the
    staging file and the run report, never silently dropped or
    guessed into a plausible-looking record.
  * The raw payload lands append-only BEFORE parsing, exactly like
    provider harvests — a parser fix never loses the source data.
    A re-run on the same day reuses the landed payloads instead of
    refetching (bounded, resumable).
  * Failures are visible: a failed discovery fetch, or a run that
    parses zero games, reports its reason class and publishes
    nothing new (the previous layer is left untouched). A single
    player's failed game-log fetch is staged and the run publishes
    partial results flagged partial: true (spec section 6) — it
    never takes the rest of the league down with it.
  * Bounded: the target list is the league's own season player
    list — one discovery fetch, one teams fetch, one game-log
    fetch per player per applicable group, one retry on a network
    error, no crawling. The collector knows its targets; it cannot
    be pointed at an arbitrary URL.

Statistic keys: game entries carry canonical statistic keys (the
same keys the MLB Player Props taxonomy proposal uses). Hitting and
pitching keys are disjoint by construction — a pitcher's hits are
hits_allowed, his strikeouts pitching_strikeouts — so a two-way
player's hitting and pitching splits for one game merge into a
single entry without collision. Fields the API reports as rate
strings (avg, era, ...) or baseball-notation strings
(inningsPitched, where "6.1" means 6 1/3 innings) are NOT carried:
outs_recorded is the true out count and is what lines grade
against. A statistic a game does not carry stays absent from that
game's entry, never zero-filled.

CLI:
  python3 gapfill_stats_mlb.py --data-root <path> [--season 2026]
Writes <data-root>/collected/game_logs_MLB.json (published layer),
<data-root>/collected/game_logs_MLB_staging.json (unkeyed/failed
records), raw payloads under <data-root>/raw/collected/
mlb_game_logs/<date>/, registers discovered players in
<data-root>/props/player_registry.json, and prints a run report.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from player_props_harvest import upsert_harvested_player
from player_registry import PlayerRegistry

API_BASE = "https://statsapi.mlb.com/api/v1"
SOURCE_NAME = "MLB Stats API"
COLLECTOR = "gapfill_stats_mlb"
LEAGUE = "MLB"
PROVIDER = "mlb"
DEFAULT_SEASON = 2026
STATS_EXPIRY_HOURS = 36  # spec 4.5 hard cap for collected statistics
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
FETCH_WORKERS = 6

# API field -> canonical statistic key. Counting statistics only;
# rate strings and summary strings are deliberately not carried.
HITTING_STAT_MAP = {
    "gamesPlayed": "games_played",
    "plateAppearances": "plate_appearances",
    "atBats": "at_bats",
    "runs": "runs",
    "hits": "hits",
    "doubles": "doubles",
    "triples": "triples",
    "homeRuns": "home_runs",
    "totalBases": "total_bases",
    "rbi": "rbi",
    "baseOnBalls": "walks",
    "strikeOuts": "strikeouts",
    "stolenBases": "stolen_bases",
    "caughtStealing": "caught_stealing",
    "hitByPitch": "hit_by_pitch",
    "sacBunts": "sac_bunts",
    "sacFlies": "sac_flies",
    "groundIntoDoublePlay": "ground_into_double_play",
}
PITCHING_STAT_MAP = {
    "gamesPitched": "games_pitched",
    "gamesStarted": "games_started",
    "outs": "outs_recorded",
    "battersFaced": "batters_faced",
    "numberOfPitches": "pitches_thrown",
    "hits": "hits_allowed",
    "runs": "runs_allowed",
    "earnedRuns": "earned_runs",
    "baseOnBalls": "walks_allowed",
    "strikeOuts": "pitching_strikeouts",
    "homeRuns": "home_runs_allowed",
    "hitBatsmen": "hit_batsmen",
    "wins": "wins",
    "losses": "losses",
    "saves": "saves",
    "holds": "holds",
    "blownSaves": "blown_saves",
    "completeGames": "complete_games",
    "shutouts": "shutouts",
    "gamesFinished": "games_finished",
    "wildPitches": "wild_pitches",
    "balks": "balks",
}
GROUP_STAT_MAPS = {"hitting": HITTING_STAT_MAP,
                   "pitching": PITCHING_STAT_MAP}


# ---------------------------------------------------------------- urls

def players_url(season: int) -> str:
    return f"{API_BASE}/sports/1/players?season={season}"


def teams_url(season: int) -> str:
    return f"{API_BASE}/teams?sportId=1&season={season}"


def gamelog_url(player_id: int, season: int, group: str) -> str:
    return (f"{API_BASE}/people/{player_id}/stats?stats=gameLog"
            f"&season={season}&group={group}")


# ---------------------------------------------------------------- fetch

def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _fetch_with_retry(fetch, url: str) -> bytes:
    try:
        return fetch(url)
    except Exception:
        return fetch(url)  # one retry, then the failure propagates


def _land_or_fetch(fetch, url: str, landing_path: Path) -> bytes:
    """Raw landing first (spec 4.2): a payload already landed today
    is reused, never refetched; a fresh fetch is written to the
    landing BEFORE it is parsed."""
    if landing_path.exists():
        return landing_path.read_bytes()
    payload = _fetch_with_retry(fetch, url)
    landing_path.parent.mkdir(parents=True, exist_ok=True)
    landing_path.write_bytes(payload)
    return payload


# ---------------------------------------------------------------- parse

def groups_for_person(person: dict) -> list[str]:
    """Which game-log groups apply, from the league's own position
    typing: pitchers pitch, everyone else hits, and a player the
    league types as Two-Way Player does both."""
    pos_type = ((person.get("primaryPosition") or {}).get("type")
                or "")
    if pos_type == "Pitcher":
        return ["pitching"]
    if pos_type == "Two-Way Player":
        return ["hitting", "pitching"]
    return ["hitting"]


def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def map_stats(raw_stat: dict, group: str) -> tuple[dict, int]:
    """API stat block -> canonical keys. Returns (stats, dropped):
    mapped fields whose value is not numeric are dropped and
    counted, never repaired into plausible-looking data."""
    stat_map = GROUP_STAT_MAPS[group]
    stats, dropped = {}, 0
    for api_key, canonical in stat_map.items():
        if api_key not in raw_stat:
            continue
        num = _numeric(raw_stat[api_key])
        if num is None:
            dropped += 1
            continue
        stats[canonical] = num
    return stats, dropped


def parse_gamelog(payload: dict, group: str) -> tuple[list[dict], dict]:
    """One gameLog payload -> game dicts (pre-merge, pre-provenance).

    A game: {game_id, date, season, game_type, team, opponent,
    stats}. Splits with no game id are dropped and counted, never
    repaired. Non-regular-season splits are counted separately and
    excluded — the layer is regular-season, like every other
    game-log store in the Engine.
    """
    stats = {"splits_seen": 0, "dropped_no_game_id": 0,
             "dropped_non_numeric": 0, "excluded_non_regular": 0,
             "duplicate_splits": 0}
    games: list[dict] = []
    seen: set[str] = set()
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            stats["splits_seen"] += 1
            game_pk = (split.get("game") or {}).get("gamePk")
            if game_pk is None:
                stats["dropped_no_game_id"] += 1
                continue
            game_id = str(game_pk)
            if split.get("gameType") not in (None, "R"):
                stats["excluded_non_regular"] += 1
                continue
            if game_id in seen:
                stats["duplicate_splits"] += 1
                continue
            seen.add(game_id)
            mapped, dropped = map_stats(split.get("stat") or {}, group)
            stats["dropped_non_numeric"] += dropped
            games.append({
                "game_id": game_id,
                "date": split.get("date"),
                "season": str(split.get("season")) if split.get(
                    "season") is not None else None,
                "game_type": split.get("gameType"),
                "team": (split.get("team") or {}).get("name"),
                "opponent": (split.get("opponent") or {}).get("name"),
                "stats": mapped,
            })
    return games, stats


def merge_games(per_group: dict[str, list[dict]]) -> tuple[list[dict],
                                                          int]:
    """Merge one player's per-group games by game_id. Hitting and
    pitching keys are disjoint, so a two-way player's two splits
    for one game become one entry. Returns (games, conflicts):
    a team/date disagreement between groups for the same game is
    counted, and the first group's stamp is kept."""
    merged: dict[str, dict] = {}
    conflicts = 0
    for group in ("hitting", "pitching"):
        for game in per_group.get(group) or []:
            existing = merged.get(game["game_id"])
            if existing is None:
                merged[game["game_id"]] = dict(game)
                continue
            if existing["team"] != game["team"] \
                    or existing["date"] != game["date"]:
                conflicts += 1
            existing["stats"] = {**existing["stats"], **game["stats"]}
    games = sorted(merged.values(),
                   key=lambda g: (g["date"] is None, g["date"] or "",
                                  g["game_id"]))
    return games, conflicts


# ---------------------------------------------------------------- run

def run(data_root: Path, season: int = DEFAULT_SEASON,
        now: datetime | None = None, fetch=None) -> dict:
    fetch = fetch or _fetch_bytes
    fetched_at = now or datetime.now(timezone.utc)
    fetched_iso = fetched_at.astimezone(timezone.utc).isoformat()
    expires_iso = (fetched_at + timedelta(hours=STATS_EXPIRY_HOURS)) \
        .astimezone(timezone.utc).isoformat()
    landing_dir = data_root / "raw" / "collected" / "mlb_game_logs" \
        / fetched_at.strftime("%Y-%m-%d")

    # Discovery + teams: a failure here fails the run — nothing new
    # is published and the previous layer is left untouched.
    try:
        players_payload = json.loads(_land_or_fetch(
            fetch, players_url(season),
            landing_dir / f"players_season{season}.json"))
        teams_payload = json.loads(_land_or_fetch(
            fetch, teams_url(season),
            landing_dir / f"teams_season{season}.json"))
    except Exception as exc:  # noqa: BLE001 — reported, not hidden
        return {"status": "failed", "reason_class": "network",
                "detail": f"discovery: {type(exc).__name__}"}
    people = players_payload.get("people") or []
    team_names = {t.get("id"): t.get("name")
                  for t in teams_payload.get("teams") or []}
    if not people:
        return {"status": "failed", "reason_class": "parse",
                "detail": "discovery returned zero players"}

    # Fetch plan: one game-log fetch per player per applicable
    # group. Fetches land raw payloads in parallel; parsing and
    # publishing are sequential and deterministic.
    plan: list[tuple[dict, str]] = []
    staged: list[dict] = []
    for person in people:
        if person.get("isPlayer") is False:
            continue
        if person.get("id") is None or not person.get("fullName"):
            staged.append({
                "mlb_id": person.get("id"),
                "full_name": person.get("fullName"),
                "staging_reason": "missing_provider_id"
                if person.get("id") is None else "missing_name",
            })
            continue
        for group in groups_for_person(person):
            plan.append((person, group))

    def fetch_one(item: tuple[dict, str]):
        person, group = item
        url = gamelog_url(person["id"], season, group)
        path = landing_dir / "people" / f"{person['id']}_{group}.json"
        try:
            return item, _land_or_fetch(fetch, url, path), None
        except Exception as exc:  # noqa: BLE001
            return item, None, f"{type(exc).__name__}"

    payloads: dict[tuple[int, str], bytes] = {}
    fetch_errors: dict[tuple[int, str], str] = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for (person, group), payload, error in pool.map(fetch_one,
                                                        plan):
            key = (person["id"], group)
            if error is not None:
                fetch_errors[key] = error
            else:
                payloads[key] = payload

    # Registry: loaded (or started empty) in memory; saved only
    # when the run publishes, so a failed run changes nothing.
    registry_path = data_root / "props" / "player_registry.json"
    registry = PlayerRegistry.load(registry_path) \
        if registry_path.exists() else PlayerRegistry.empty()
    effective = fetched_at.strftime("%Y-%m-%d")

    totals = {"splits_seen": 0, "dropped_no_game_id": 0,
              "dropped_non_numeric": 0, "excluded_non_regular": 0,
              "duplicate_splits": 0, "merge_conflicts": 0}
    published_players: dict[str, dict] = {}
    total_games = 0
    players_with_games = 0

    for person in people:
        if person.get("isPlayer") is False \
                or person.get("id") is None \
                or not person.get("fullName"):
            continue
        mlb_id = person["id"]
        groups = groups_for_person(person)
        failed_groups = [g for g in groups
                         if (mlb_id, g) in fetch_errors]
        per_group: dict[str, list[dict]] = {}
        parse_failed = False
        for group in groups:
            if (mlb_id, group) in fetch_errors:
                continue
            try:
                games, pstats = parse_gamelog(
                    json.loads(payloads[(mlb_id, group)]), group)
            except Exception:  # noqa: BLE001
                parse_failed = True
                continue
            per_group[group] = games
            for k in totals:
                if k in pstats:
                    totals[k] += pstats[k]
        if failed_groups or parse_failed:
            staged.append({
                "mlb_id": mlb_id, "full_name": person["fullName"],
                "staging_reason": "fetch_failed"
                if failed_groups else "parse_failed",
                "groups_failed": failed_groups,
                "detail": fetch_errors.get((mlb_id, failed_groups[0]))
                if failed_groups else None,
            })
            if not per_group:
                continue  # nothing landed for this player at all
        games, conflicts = merge_games(per_group)
        totals["merge_conflicts"] += conflicts

        latest_team = None
        for game in reversed(games):
            if game.get("team"):
                latest_team = game["team"]
                break
        if latest_team is None:
            current = (person.get("currentTeam") or {}).get("id")
            latest_team = team_names.get(current)
        result = upsert_harvested_player(registry, {
            "provider_id": mlb_id, "full_name": person["fullName"],
            "team_name": latest_team, "status": None,
            "league": LEAGUE,
        }, effective, provider=PROVIDER)
        player = result["player"]
        if player is None:
            staged.append({
                "mlb_id": mlb_id, "full_name": person["fullName"],
                "staging_reason": result["skipped"] or "unkeyed",
            })
            continue

        entries = []
        for game in games:
            source_url = gamelog_url(mlb_id, season, groups[0])
            provenance = {
                "origin": "collected",
                "source_name": SOURCE_NAME,
                "source_url": source_url,
                "fetched_at": fetched_iso,
                "collector": COLLECTOR,
                "classification": "Historical",
                "expires_at": expires_iso,
                "supersedes": None,
            }
            if len(groups) > 1:
                provenance["source_urls"] = [
                    gamelog_url(mlb_id, season, g) for g in groups]
            entries.append({
                "game_id": game["game_id"], "date": game["date"],
                "season": game["season"], "team": game["team"],
                "opponent": game["opponent"], "stats": game["stats"],
                "collected": True, "provenance": provenance,
            })
        published_players[player["player_id"]] = {
            "mlb_id": mlb_id, "full_name": person["fullName"],
            "games": entries,
        }
        total_games += len(entries)
        if entries:
            players_with_games += 1

    if total_games == 0:
        return {"status": "failed", "reason_class": "parse",
                "detail": "zero games parsed across the league",
                "staged_unkeyed": len(staged)}

    partial = bool(staged) or totals["dropped_no_game_id"] > 0
    collected_dir = data_root / "collected"
    collected_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "kind": "collected-game-logs",
        "league": LEAGUE,
        "season": str(season),
        "classification": "Historical",
        "collected": True,
        "generated_at": fetched_iso,
        "partial": partial,
        "source": {
            "source_name": SOURCE_NAME,
            "discovery_url": players_url(season),
            "fetched_at": fetched_iso,
            "raw_landing": str(landing_dir),
        },
        "players": published_players,
        "counts": {
            "players_discovered": len(people),
            "players_published": len(published_players),
            "players_with_games": players_with_games,
            "players_without_games":
                len(published_players) - players_with_games,
            "games_published": total_games,
            "staged_unkeyed": len(staged),
            **totals,
        },
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry.save(registry_path)
    (collected_dir / "game_logs_MLB.json").write_text(
        json.dumps(doc, indent=1))
    (collected_dir / "game_logs_MLB_staging.json").write_text(
        json.dumps({
            "kind": "collected-game-logs-staging",
            "league": LEAGUE,
            "season": str(season),
            "generated_at": fetched_iso,
            "staged": staged,
        }, indent=1))
    return {"status": "ok", "season": season, "partial": partial,
            "players_discovered": len(people),
            "players_published": len(published_players),
            "players_with_games": players_with_games,
            "games_published": total_games,
            "staged_unkeyed": len(staged),
            "parse_totals": totals,
            "out": str(collected_dir / "game_logs_MLB.json")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/intelligence")
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON)
    args = parser.parse_args(argv)
    result = run(Path(args.data_root), season=args.season)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
