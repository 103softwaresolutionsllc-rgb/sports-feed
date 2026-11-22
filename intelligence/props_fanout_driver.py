"""props_fanout_driver.py — operational driver for the player fan-out.

Runs intelligence_espn_players.run_espn_fanout ONE TEAM AT A TIME,
saving the registry and game-log stores after each team, so a slow
or failing team can never hold the whole league hostage and progress
is never lost. Resumable: it loads the existing stores from the data
root and skips teams already covered (per the log store's coverage
block) unless --force names them again.

This exists because player_props_harvest.main() runs the pass
monolithically and saves only at the end; the Nov 20, 2026 NBA run
stalled mid-league and lost everything. The driver changes no data
semantics — every record is produced by run_espn_fanout itself.

Usage:
  python3 props_fanout_driver.py --sport basketball \
      --season 2026-2027 --data-root <path-to-data/intelligence>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intelligence_harvest import (  # noqa: E402
    ESPN_NAME_ALIASES, RawLanding, urllib_transport)
from intelligence_espn_players import run_espn_fanout  # noqa: E402
from player_game_logs import GameLogStore  # noqa: E402
from player_props_harvest import SPORT_TO_LEAGUE  # noqa: E402
from player_registry import PlayerRegistry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True,
                        choices=sorted(SPORT_TO_LEAGUE))
    parser.add_argument("--season", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--skip", default="",
                        help="comma-separated team names to skip")
    parser.add_argument("--force", action="store_true",
                        help="reprocess every team even if covered "
                             "(repairs: re-stamps records from source)")
    args = parser.parse_args(argv)

    sport, season = args.sport, args.season
    league = SPORT_TO_LEAGUE[sport]
    data_root = Path(args.data_root)
    props_dir = data_root / "props"
    props_dir.mkdir(parents=True, exist_ok=True)
    registry_path = props_dir / "player_registry.json"
    log_path = props_dir / f"game_logs_{league}.json"

    teams_doc = json.loads(
        (data_root / "normalized" / sport
         / f"teams_{season}.json").read_text())
    # Canonicalize through the same alias map the fan-out's team-id
    # lookup uses: a teams file carrying ESPN's raw spelling
    # ("LA Clippers") would otherwise miss its id lookup and silently
    # fetch zero athletes — exactly what bit the 2025-2026 run.
    team_names = [ESPN_NAME_ALIASES.get(t["team"], t["team"])
                  for t in teams_doc["teams"]]
    if not team_names:
        # Fail loud (Nov 22, 2026): an empty teams doc — clobbered by a
        # bad normalize on Nov 21 — made this driver iterate nothing,
        # report "complete" in ~2s, and re-stamp coverage as fresh,
        # masking two nights of zero collection. Never again.
        print(json.dumps({"status": "failed", "league": league,
                          "season": season,
                          "reason": "empty_teams_list",
                          "detail": "teams doc lists zero teams; "
                                    "refusing to run or stamp coverage"},
                         indent=2), flush=True)
        return 2
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    registry = (PlayerRegistry.load(registry_path)
                if registry_path.exists() else PlayerRegistry.empty())
    log_store = (GameLogStore.load(log_path)
                 if log_path.exists() else GameLogStore.empty(league))
    # "Covered" is per-season: a team counts only if the store already
    # holds games for it in THIS season (a prior season's coverage must
    # never skip a new season's run).
    covered = [] if args.force else sorted({
        g.get("team")
        for p in (log_store.document.get("players") or {}).values()
        for g in p.get("games", [])
        if g.get("season") == season and g.get("team")})
    landing = RawLanding(data_root)

    totals = {"players_registered": 0, "players_updated": 0,
              "games_added": 0, "athletes_fetched": 0}
    attempted = 0
    for team in team_names:
        if team in skip:
            print(f"SKIP {team}", flush=True)
            continue
        if team in covered:
            print(f"DONE {team} (already covered)", flush=True)
            continue
        attempted += 1
        report = run_espn_fanout(
            sport, season, [team], transport=urllib_transport,
            landing=landing, registry=registry, log_store=log_store)
        for key in totals:
            totals[key] += report.get(key, 0)
        if report.get("teams_fetched"):
            covered.append(team)
        registry.save(registry_path)
        log_store.save(log_path)
        print(f"TEAM {team}: fetched={report.get('teams_fetched')} "
              f"athletes={report.get('athletes_fetched')} "
              f"registered_total={totals['players_registered']} "
              f"games_total={totals['games_added']}", flush=True)

    if attempted > 0 and totals["athletes_fetched"] == 0:
        # Fail loud: teams were attempted and not one athlete came back.
        # Reporting "complete" here is how a dead source hides.
        print(json.dumps({"status": "failed", "league": league,
                          "season": season,
                          "reason": "zero_athletes_fetched",
                          "teams_attempted": attempted, **totals},
                         indent=2), flush=True)
        return 2

    seasons_present = sorted({
        g.get("season")
        for p in (log_store.document.get("players") or {}).values()
        for g in p.get("games", []) if g.get("season")})
    # Stamp coverage only when the run leaves at least one team covered;
    # a fresh as_of over zero coverage masked the Nov 21-22 outage from
    # the publish gate's staleness check.
    coverage_stamped = bool(covered)
    if coverage_stamped:
        log_store.set_coverage({
            "sport": sport,
            "season": season,
            "seasons": seasons_present,
            "teams_covered": [t for t in team_names if t in covered],
            "teams_total": len(team_names),
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": "espn-fallback",
        })
        log_store.save(log_path)
    registry.save(registry_path)
    print(json.dumps({"status": "complete", "league": league,
                      "season": season,
                      "teams_covered": len(covered),
                      "teams_total": len(team_names),
                      "coverage_stamped": coverage_stamped, **totals},
                     indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
