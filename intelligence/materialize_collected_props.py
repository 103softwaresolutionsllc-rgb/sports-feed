"""materialize_collected_props.py — Route 1 to a league props store:
materialize a Gap-Filler COLLECTED layer into the props game-log
store the line-maker reads (props/game_logs_<LEAGUE>.json).

Why this exists, stated plainly: the collected layer is answers-only
by design (Gap-Filler spec, decision D4 — collected data serves Sage's
answers, never the model, until the source earns trust and Isaac
approves promotion per source). On Nov 21, 2026 Isaac ruled the MLB
Stats API source PROMOTED under D4 and approved this materialization
explicitly; the same day he adopted the NHL taxonomy proposal in
full, which is his D4 promotion ruling for the NHL source (the NHL
official web API). Each league's config below carries its own
ruling. The store this module writes therefore carries its
provenance on its face:

  * origin: materialized from the collected layer — NOT a direct
    provider harvest, and never dressed as one;
  * source: the league's real source (MLB Stats API via
    gapfill_stats_mlb; NHL official web API via gapfill_stats_nhl);
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
    _add_derived (baseball branch: hits_runs_rbi; hockey branch:
    saves, wins), exactly as a direct harvest would compute them;
  * APPEND semantics: an existing props store is loaded and the
    layer's games are appended into it (the store's append_game
    dedupes by game_id and reports corrections explicitly). The
    collected layer is a current-season snapshot — the collector
    republishes it wholesale — so the props store is the durable
    multi-season record: prior-season games already in the store
    survive every later materialization untouched. A first
    materialization into an empty store is the special case where
    counts conserve exactly (players and games out == in);
  * counts are accounted, never assumed: every game in the layer is
    reported as added, already present, corrected, or unmapped. A
    record that cannot map cleanly (missing game_id/date/season, a
    non-numeric stat value) is REPORTED in the run report and in
    the store's provenance block — never silently dropped, never
    zero-filled, never repaired into plausible-looking data;
  * league-specific string stats are handled explicitly, per
    league config, before the numeric check: NHL's toi ("MM:SS")
    is converted to toi_seconds (integer seconds — a conditioning
    stat, not a market, per decision D5 of the NHL proposal) and
    its decision string is consumed by the wins derivation in
    _add_derived and never carried as a stat. Any OTHER non-numeric
    stat value is an unmapped record, reported — never dropped
    silently. (A full census of the NHL layer, Nov 21, 2026, found
    toi and decision are the only non-numeric stats it carries.)

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

# League -> config. Fields:
#   sport            — sport key for _add_derived.
#   coverage_source  — what the line-maker stamps onto published
#                      lines as the store's source: it names the real
#                      origin, not a generic provider label.
#   source_name      — the source's name, for the provenance block.
#   collector        — the Gap-Filler collector that built the layer.
#   promotion        — THIS league's D4 promotion ruling, recorded
#                      verbatim in the store's provenance. A ruling
#                      for one source never extends to another.
#   prepare          — optional stats preparation applied to each
#                      game's stats before the numeric check (league
#                      string-stat handling; see module docstring).
#   string_keys      — stat keys allowed to still be strings at the
#                      numeric check because a derivation consumes
#                      them (NHL: decision, consumed by wins).
LEAGUES = {
    "MLB": {
        "sport": "baseball",
        "coverage_source": "mlb-stats-api-collected-promoted",
        "source_name": "MLB Stats API",
        "collector": "gapfill_stats_mlb",
        "promotion": {
            "ruled_by": "Isaac Jones",
            "date": "2026-11-21",
            "basis": ("Gap-Filler D4 promotion ruling (Nov 21, "
                      "2026): the MLB Stats API source is promoted "
                      "from answers-only collected data to a "
                      "line-making input. This store was "
                      "materialized from the collected layer under "
                      "that ruling; it is not direct provider-"
                      "harvest data and must not be represented "
                      "as such."),
        },
    },
    "NHL": {
        "sport": "hockey",
        "coverage_source": "nhl-web-api-collected-promoted",
        "source_name": "NHL official web API",
        "collector": "gapfill_stats_nhl",
        "promotion": {
            "ruled_by": "Isaac Jones",
            "date": "2026-11-21",
            "basis": ("Gap-Filler D4 promotion ruling (Nov 21, "
                      "2026): Isaac adopted the NHL player-props "
                      "taxonomy proposal in full; that adoption is "
                      "his promotion ruling for the NHL source (the "
                      "NHL official web API, api-web.nhle.com), "
                      "promoting it from answers-only collected "
                      "data to a line-making input. This store was "
                      "materialized from the collected layer under "
                      "that ruling; it is not direct provider-"
                      "harvest data and must not be represented "
                      "as such."),
        },
        "prepare": "_prepare_nhl_stats",
        "string_keys": ("decision",),
    },
}


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) \
        and not isinstance(value, bool)


def _toi_to_seconds(value) -> int | None:
    """An NHL time-on-ice string ("MM:SS", minutes may exceed 59
    in overtime) -> integer seconds, or None when the value is not
    a well-formed MM:SS string."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2 or not parts[0].isdigit() \
            or len(parts[1]) != 2 or not parts[1].isdigit():
        return None
    minutes, seconds = int(parts[0]), int(parts[1])
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


def _prepare_nhl_stats(stats: dict) -> None:
    """NHL string-stat handling (proposal decision D5), in place.

    toi ("MM:SS") converts to toi_seconds (integer seconds). A toi
    value that does not parse is put back untouched: the numeric
    check then reports the record unmapped — visible, never
    repaired into a plausible number. The decision string is NOT
    touched here: the wins derivation in _add_derived consumes it.
    """
    if "toi" in stats:
        raw = stats.pop("toi")
        seconds = _toi_to_seconds(raw)
        if seconds is not None:
            stats["toi_seconds"] = seconds
        else:
            stats["toi"] = raw


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
    games_in = 0
    games_added = 0
    games_corrected = 0
    games_already_present = 0

    prepare = globals()[cfg["prepare"]] if cfg.get("prepare") \
        else None
    string_keys = set(cfg.get("string_keys") or ())

    # Append semantics: an existing store is the durable multi-season
    # record and is loaded, never rebuilt from the layer snapshot.
    out_path = data_root / "props" / f"game_logs_{league}.json"
    if out_path.exists():
        store = GameLogStore.load(out_path)
    else:
        store = GameLogStore.empty(league)

    for player_id, rec in players_in.items():
        # Every collected player is carried, including zero-game
        # players (rostered, never appeared): their slot exists with
        # an empty log, so player counts conserve exactly.
        store.document["players"].setdefault(player_id, {"games": []})
        seen_this_run: set[str] = set()
        for game in rec.get("games") or []:
            games_in += 1
            prov = game.get("provenance") or {}
            if prov.get("fetched_at"):
                fetched_ats.append(prov["fetched_at"])
            stats = game.get("stats")
            reason = None
            detail = None
            mapped = None
            if not game.get("game_id"):
                reason = "missing_game_id"
            elif not game.get("date"):
                reason = "missing_date"
            elif not game.get("season"):
                reason = "missing_season"
            elif not isinstance(stats, dict):
                reason = "stats_not_a_mapping"
            elif str(game["game_id"]) in seen_this_run:
                # The layer itself carried this game twice for the
                # player — distinct from the game already being in
                # the store from an earlier materialization, which
                # is the normal append case below.
                reason = "duplicate_game_id_in_layer"
            else:
                mapped = dict(stats)
                if prepare is not None:
                    prepare(mapped)
                bad = sorted(k for k, v in mapped.items()
                             if not _numeric(v)
                             and k not in string_keys)
                if bad:
                    reason = "non_numeric_stat_value"
                    detail = {"stat_keys": bad}
                else:
                    _add_derived(cfg["sport"], mapped)
                    bad = sorted(k for k, v in mapped.items()
                                 if not _numeric(v))
                    if bad:
                        reason = "non_numeric_stat_value"
                        detail = {"stat_keys": bad,
                                  "after_derivation": True}
            if reason is not None:
                problem = {
                    "player_id": player_id,
                    "game_id": game.get("game_id"),
                    "reason": reason,
                }
                if detail:
                    problem["detail"] = detail
                problems.append(problem)
                continue
            seen_this_run.add(str(game["game_id"]))
            result = store.append_game(player_id, {
                "game_id": game["game_id"],
                "date": game["date"],
                "season": game["season"],
                "team": game.get("team"),
                "stats": mapped,
            })
            if result["added"]:
                games_added += 1
            elif result["corrected"]:
                # The store held this game with different stats:
                # append_game replaced the entry and says so. A
                # correction is explicit, counted here — never a
                # silent blend.
                games_corrected += 1
            else:
                games_already_present += 1

    games_out = games_added + games_corrected + games_already_present
    players_out = len(store.document["players"])
    layer_players_present = all(
        pid in store.document["players"] for pid in players_in)
    # Coverage describes the STORE after this run (all seasons it
    # holds), not just the layer snapshot that fed it.
    store_seasons = sorted({
        g.get("season")
        for rec in store.document["players"].values()
        for g in rec.get("games") or [] if g.get("season")})
    store_teams = sorted({
        g.get("team")
        for rec in store.document["players"].values()
        for g in rec.get("games") or [] if g.get("team")})
    store.set_coverage({
        "as_of": run_at.strftime("%Y-%m-%d"),
        "season": store_seasons[-1] if store_seasons
        else doc.get("season"),
        "seasons": store_seasons or [doc.get("season")],
        "source": cfg["coverage_source"],
        "sport": cfg["sport"],
        "teams_covered": store_teams,
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
        "promotion": dict(cfg["promotion"]),
        "unmapped_records": len(problems),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    store.save(out_path)
    return {
        "status": "ok",
        "league": league,
        "players_in": len(players_in),
        "players_out": players_out,
        "games_in": games_in,
        "games_out": games_out,
        "games_added": games_added,
        "games_corrected": games_corrected,
        "games_already_present": games_already_present,
        "counts_conserved": layer_players_present
        and games_in == games_out + len(problems),
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
