"""materialize_collected_props.py — Route 1 to a league props store:
materialize a Gap-Filler COLLECTED layer into the props game-log
store the line-maker reads (props/game_logs_<LEAGUE>.json).

Why this exists, stated plainly: the collected layer is answers-only
by design (Gap-Filler spec, decision D4 — collected data serves Sage's
answers, never the model, until the source earns trust and Isaac
approves promotion per source). On Nov 21, 2026 Isaac ruled the MLB
Stats API source PROMOTED under D4 and approved this materialization
explicitly. The store this module writes therefore carries its
provenance on its face:

  * origin: materialized from the collected layer — NOT a direct
    provider harvest, and never dressed as one;
  * source: MLB Stats API, via the gapfill_stats_mlb collector;
  * the collected layer's own fetch timestamps, carried over;
  * the promotion ruling itself (who, when, under what rule).

Anyone reading the store can see exactly what the data is and why it
is allowed to feed line-making. Route 2 (props_feed_mlb.py) is the
direct-harvest counterpart: it writes the same store from the same
API without the collected layer in between, and the two routes'
outputs are diffed against each other before either is trusted.

Mapping rules (the props-store schema is defined by
player_game_logs.GameLogStore and the NBA/NFL stores):
  * players keyed by the registry's permanent player_id, exactly as
    the collected layer keys them (identity was settled by provider
    ID when the layer was collected — never re-keyed here);
  * a game entry is {game_id, date, season, team, stats} — the
    collected layer's per-game provenance and opponent fields are
    layer metadata and stay behind in the layer; the store's own
    provenance block (below) is where this store's origin lives;
  * derived statistics are applied with the harvest's own
    _add_derived (baseball branch: hits_runs_rbi), exactly as a
    direct harvest would compute them;
  * counts conserve: players and games out == players and games in.
    A record that cannot map cleanly (missing game_id/date/season,
    a non-numeric stat value) is REPORTED in the run report and in
    the store's provenance block — never silently dropped, never
    zero-filled, never repaired into plausible-looking data.

CLI:
  python3 materialize_collected_props.py --data-root <path>
      [--league MLB]
Reads <data-root>/collected/game_logs_<LEAGUE>.json, writes
<data-root>/props/game_logs_<LEAGUE>.json, prints a run report.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from player_game_logs import GameLogStore
    from player_props_harvest import _add_derived
except ImportError:  # package-style import (sports-feed repo layout)
    from intelligence.player_game_logs import GameLogStore  # type: ignore
    from intelligence.player_props_harvest import _add_derived  # type: ignore

# League -> (sport key for _add_derived, coverage source string).
# The coverage source is what the line-maker stamps onto published
# lines as the store's source: it names the real origin, not a
# generic provider label.
LEAGUES = {
    "MLB": {
        "sport": "baseball",
        "coverage_source": "mlb-stats-api-collected-promoted",
        "source_name": "MLB Stats API",
        "collector": "gapfill_stats_mlb",
    },
}

PROMOTION = {
    "ruled_by": "Isaac Jones",
    "date": "2026-11-21",
    "basis": ("Gap-Filler D4 promotion ruling (Nov 21, 2026): the MLB "
              "Stats API source is promoted from answers-only "
              "collected data to a line-making input. This store was "
              "materialized from the collected layer under that "
              "ruling; it is not direct provider-harvest data and "
              "must not be represented as such."),
}


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) \
        and not isinstance(value, bool)


def materialize(data_root: Path, league: str = "MLB",
                now: datetime | None = None) -> dict:
    """Materialize one league's collected layer into its props
    store. Returns the run report (see module docstring)."""
    cfg = LEAGUES[league]
    collected_path = data_root / "collected" / f"game_logs_{league}.json"
    if not collected_path.exists():
        return {"status": "failed", "reason_class": "missing_layer",
                "detail": f"no collected layer at {collected_path}"}
    doc = json.loads(collected_path.read_text())
    if doc.get("league") != league or not doc.get("collected"):
        return {"status": "failed", "reason_class": "not_collected",
                "detail": "input document is not this league's "
                          "collected layer"}

    run_at = now or datetime.now(timezone.utc)
    players_in = doc.get("players") or {}
    problems: list[dict] = []
    fetched_ats: list[str] = []
    teams: set[str] = set()
    games_in = 0
    games_out = 0

    store = GameLogStore.empty(league)
    for player_id, rec in players_in.items():
        # Every collected player is carried, including zero-game
        # players (rostered, never appeared): their slot exists with
        # an empty log, so player counts conserve exactly.
        slot = store.document["players"].setdefault(
            player_id, {"games": []})
        for game in rec.get("games") or []:
            games_in += 1
            prov = game.get("provenance") or {}
            if prov.get("fetched_at"):
                fetched_ats.append(prov["fetched_at"])
            stats = game.get("stats")
            reason = None
            if not game.get("game_id"):
                reason = "missing_game_id"
            elif not game.get("date"):
                reason = "missing_date"
            elif not game.get("season"):
                reason = "missing_season"
            elif not isinstance(stats, dict):
                reason = "stats_not_a_mapping"
            elif any(not _numeric(v) for v in stats.values()):
                reason = "non_numeric_stat_value"
            if reason is not None:
                problems.append({
                    "player_id": player_id,
                    "game_id": game.get("game_id"),
                    "reason": reason,
                })
                continue
            mapped = dict(stats)
            _add_derived(cfg["sport"], mapped)
            result = store.append_game(player_id, {
                "game_id": game["game_id"],
                "date": game["date"],
                "season": game["season"],
                "team": game.get("team"),
                "stats": mapped,
            })
            if result["added"]:
                games_out += 1
                if game.get("team"):
                    teams.add(game["team"])
            else:
                # append_game dedupes by game_id within the player;
                # the collected layer is already deduped, so a
                # non-add here means the layer carried a duplicate
                # the materializer must not hide.
                problems.append({
                    "player_id": player_id,
                    "game_id": game.get("game_id"),
                    "reason": "duplicate_game_id_in_layer",
                })
        slot = store.document["players"][player_id]

    players_out = len(store.document["players"])
    store.set_coverage({
        "as_of": run_at.strftime("%Y-%m-%d"),
        "season": doc.get("season"),
        "seasons": [doc.get("season")],
        "source": cfg["coverage_source"],
        "sport": cfg["sport"],
        "teams_covered": sorted(teams),
    })
    store.document["provenance"] = {
        "origin": "materialized_from_collected_layer",
        "source_name": cfg["source_name"],
        "collected_layer": f"collected/game_logs_{league}.json",
        "collected_generated_at": doc.get("generated_at"),
        "collected_fetched_at_min": min(fetched_ats) if fetched_ats
        else None,
        "collected_fetched_at_max": max(fetched_ats) if fetched_ats
        else None,
        "collector": cfg["collector"],
        "materialized_by": "materialize_collected_props",
        "materialized_at": run_at.astimezone(timezone.utc).isoformat(),
        "promotion": dict(PROMOTION),
        "unmapped_records": len(problems),
    }

    out_path = data_root / "props" / f"game_logs_{league}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    store.save(out_path)
    return {
        "status": "ok",
        "league": league,
        "players_in": len(players_in),
        "players_out": players_out,
        "games_in": games_in,
        "games_out": games_out,
        "counts_conserved": len(players_in) == players_out
        and games_in == games_out,
        "unmapped_records": len(problems),
        "problems": problems[:50],
        "out": str(out_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/intelligence")
    parser.add_argument("--league", default="MLB",
                        choices=sorted(LEAGUES))
    args = parser.parse_args(argv)
    result = materialize(Path(args.data_root), league=args.league)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
