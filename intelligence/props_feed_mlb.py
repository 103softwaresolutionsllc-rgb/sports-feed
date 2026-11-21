"""props_feed_mlb.py — Route 2 to the MLB props store: a DIRECT
harvest of the MLB Stats API into props/game_logs_MLB.json, the
store the line-maker reads.

Route 1 (materialize_collected_props.py) built the store from the
Gap-Filler collected layer under Isaac's Nov 21, 2026 D4 promotion
ruling. This feed is the counterpart: the same league API, the same
endpoints, the same canonical key mapping (imported from
gapfill_stats_mlb so the two routes cannot drift apart), written
straight into the props store through the store's own append
semantics — no collected layer in between. Where the routes agree,
the store is doubly verified; where they disagree, append_game
reports a correction and the disagreement is investigated, never
blended silently.

What this feed does that the collector does not:
  * registers players through the unified registry upsert WITH
    position (the API's primaryPosition abbreviation) — the
    collector's registration left MLB positions empty, and a feed
    run backfills them;
  * writes the props store (the collector writes only its own
    collected layer and never touches provider stores).

Discipline, inherited from the harvest/collector canon:
  * identity joins ONLY on provider ID (provider_ids.mlb) via
    upsert_harvested_player — never by name;
  * raw payloads land append-only under raw/props_feed/mlb/<date>/
    BEFORE parsing; a same-day re-run reuses the landing;
  * a statistic a game does not carry stays absent — never
    zero-filled; derived stats come from the harvest's _add_derived;
  * failures are visible: discovery failure publishes nothing; a
    single player's failed fetch is staged and the run completes
    partial, flagged in the report;
  * the store is saved only when a game was actually added or
    corrected — a pure verification pass leaves the published file
    byte-identical.

CADENCE — intended, NOT scheduled: in season, this feed is meant to
run nightly after the night's final games, alongside the Engine's
other nightly collectors, refreshing the store while the publish
gate guards what ships. NO cron exists for it and this module does
not create one: scheduling is Isaac's call. The 2026 season is
complete (final regular-season games Sep 27, 2026), so until the
2027 season opens in spring 2027, runs are verification and repair
only.

CLI:
  python3 props_feed_mlb.py --data-root <path> [--season 2026]
Writes/updates <data-root>/props/game_logs_MLB.json and
<data-root>/props/player_registry.json, lands raw payloads under
<data-root>/raw/props_feed/mlb/<date>/, prints a run report.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from gapfill_stats_mlb import (
        SOURCE_NAME, gamelog_url, groups_for_person, merge_games,
        parse_gamelog, players_url, teams_url)
    from player_game_logs import GameLogStore
    from player_props_harvest import _add_derived, upsert_harvested_player
    from player_registry import PlayerRegistry
except ImportError:  # package-style import (sports-feed repo layout)
    from intelligence.gapfill_stats_mlb import (  # type: ignore
        SOURCE_NAME, gamelog_url, groups_for_person, merge_games,
        parse_gamelog, players_url, teams_url)
    from intelligence.player_game_logs import GameLogStore  # type: ignore
    from intelligence.player_props_harvest import (  # type: ignore
        _add_derived, upsert_harvested_player)
    from intelligence.player_registry import PlayerRegistry  # type: ignore

FEED = "props_feed_mlb"
LEAGUE = "MLB"
SPORT = "baseball"
PROVIDER = "mlb"
DEFAULT_SEASON = 2026
COVERAGE_SOURCE = "mlb-stats-api-props-feed"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
FETCH_WORKERS = 6


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
    """Raw landing first: a payload already landed today is reused,
    never refetched; a fresh fetch is written BEFORE it is parsed."""
    if landing_path.exists():
        return landing_path.read_bytes()
    payload = _fetch_with_retry(fetch, url)
    landing_path.parent.mkdir(parents=True, exist_ok=True)
    landing_path.write_bytes(payload)
    return payload


def position_of(person: dict) -> str | None:
    """The registry position for a discovered player: the API's own
    primaryPosition abbreviation (P, C, 1B, SS, TWP, ...) — the same
    short-form convention the registry carries for every other
    league (QB, C, G). Absent typing stays absent."""
    pos = person.get("primaryPosition") or {}
    abbr = pos.get("abbreviation")
    return abbr if isinstance(abbr, str) and abbr else None


def run(data_root: Path, season: int = DEFAULT_SEASON,
        now: datetime | None = None, fetch=None) -> dict:
    fetch = fetch or _fetch_bytes
    run_at = now or datetime.now(timezone.utc)
    effective = run_at.strftime("%Y-%m-%d")
    landing_dir = data_root / "raw" / "props_feed" / "mlb" / effective

    # Discovery + teams: a failure here fails the run — nothing is
    # written anywhere.
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

    registry_path = data_root / "props" / "player_registry.json"
    registry = PlayerRegistry.load(registry_path) \
        if registry_path.exists() else PlayerRegistry.empty()
    store_path = data_root / "props" / f"game_logs_{LEAGUE}.json"
    store = GameLogStore.load(store_path) \
        if store_path.exists() else GameLogStore.empty(LEAGUE)
    store_created = not store_path.exists()

    totals = {"registered_new": 0, "position_changes": 0,
              "team_changes": 0, "games_added": 0,
              "games_corrected": 0, "games_unchanged": 0}
    registry_dirty = False
    total_games_seen = 0

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
                games, _pstats = parse_gamelog(
                    json.loads(payloads[(mlb_id, group)]), group)
            except Exception:  # noqa: BLE001
                parse_failed = True
                continue
            per_group[group] = games
        if failed_groups or parse_failed:
            staged.append({
                "mlb_id": mlb_id, "full_name": person["fullName"],
                "staging_reason": "fetch_failed"
                if failed_groups else "parse_failed",
                "groups_failed": failed_groups,
            })
            if not per_group:
                continue
        games, _conflicts = merge_games(per_group)

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
            "league": LEAGUE, "position": position_of(person),
        }, effective, provider=PROVIDER)
        player = result["player"]
        if player is None:
            staged.append({
                "mlb_id": mlb_id, "full_name": person["fullName"],
                "staging_reason": result["skipped"] or "unkeyed",
            })
            continue
        if result["changes"]:
            registry_dirty = True
        if "registered" in result["changes"]:
            totals["registered_new"] += 1
        if "position_change" in result["changes"]:
            totals["position_changes"] += 1
        if "team_change" in result["changes"]:
            totals["team_changes"] += 1

        player_id = player["player_id"]
        # The player's slot exists even with zero games, matching
        # the store shape Route 1 publishes.
        store.document["players"].setdefault(player_id, {"games": []})
        for game in games:
            stats = dict(game["stats"])
            _add_derived(SPORT, stats)
            outcome = store.append_game(player_id, {
                "game_id": game["game_id"],
                "date": game["date"],
                "season": game["season"],
                "team": game.get("team"),
                "stats": stats,
            })
            total_games_seen += 1
            if outcome["added"]:
                totals["games_added"] += 1
            elif outcome["corrected"]:
                totals["games_corrected"] += 1
            else:
                totals["games_unchanged"] += 1

    if total_games_seen == 0 and not store_created:
        return {"status": "failed", "reason_class": "parse",
                "detail": "zero games parsed across the league",
                "staged_unkeyed": len(staged)}

    store_changed = totals["games_added"] > 0 \
        or totals["games_corrected"] > 0
    if store_created:
        teams_covered = sorted({
            g.get("team") for prec in
            store.document["players"].values()
            for g in prec.get("games", []) if g.get("team")})
        store.set_coverage({
            "as_of": effective, "season": str(season),
            "seasons": [str(season)], "source": COVERAGE_SOURCE,
            "sport": SPORT, "teams_covered": teams_covered,
        })
        store.document["provenance"] = {
            "origin": "direct_harvest_props_feed",
            "source_name": SOURCE_NAME,
            "feed": FEED,
            "harvested_at": run_at.astimezone(timezone.utc).isoformat(),
        }
        store_changed = True
    elif store_changed:
        coverage = dict(store.document.get("coverage") or {})
        coverage["as_of"] = effective
        store.set_coverage(coverage)

    if registry_dirty:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry.save(registry_path)
    if store_changed:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store.save(store_path)

    return {
        "status": "ok",
        "season": season,
        "partial": bool(staged),
        "players_discovered": len(people),
        "staged_unkeyed": len(staged),
        "staged": staged[:50],
        "games_seen": total_games_seen,
        **totals,
        "store_written": store_changed,
        "registry_written": registry_dirty,
        "out": str(store_path),
    }


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
