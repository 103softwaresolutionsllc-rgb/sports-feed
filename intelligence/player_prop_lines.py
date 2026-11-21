"""player_prop_lines.py — the Intelligence Engine's fair-line maker.

Makes the engine's OWN prop lines from harvested player game logs, the
way a sportsbook's model desk does: fit the player's recent-form
distribution for a statistic, then post the line at the distribution's
median so the over and under are as close to even as the data allows.

The math deliberately mirrors player_props_projection.project_selection
EXACTLY (same window, same fitted distribution — negative binomial /
Poisson for integer statistics, Gaussian for minutes — same continuity
correction, same MIN_GAMES floor), so a model line and the app's
projection of a selection at that line always agree: the published
probability_over is what project_selection returns for an over at the
published line. Model v1.1 (Nov 20, 2026): the distribution families
were chosen by a rolling-origin backtest on the full 2025-2026
harvested season (distribution_validation.py), where the negative
binomial beat the v1.0 Gaussian on essentially every counting stat.

Each published record keeps the three layers a sportsbook keeps
separate, and never merges them: player_projection (what the model
expects), model_line (the model's own fair threshold), and the market
block (sportsbook_line / sportsbook_odds / market_consensus — null
with status "not_sourced" until a licensed book-lines source exists).

PUBLISH GATE (framework adopted Nov 20, 2026): do not publish
projections when the underlying data is incomplete or too old.
evaluate_publish_gate withholds a player's lines entirely when his
harvested record fails plausibility — an established player (40+
prior-season games) with almost no current-season games (<= 3) is
the signature of the identity corruption found in ESPN's
current-season feed that night (athlete 6450's 2026-2027 gamelog
contained another player's games). A stale store withholds
everything. Withholding is never silent: the published document
lists every withheld player with his reasons and counts.

The gate's thresholds are calibrated PER LEAGUE (GATE_THRESHOLDS,
league-aware since Nov 21, 2026): a plausibility bar is only
meaningful against a league's season shape. The NBA plays 82 games
on a near-nightly rhythm; the NFL plays 17 on a weekly rhythm with
natural 3-4 day gaps, so the NBA's numbers applied to the NFL both
fail to fire (a 40-game prior bar is ~2.4 NFL seasons) and misfire
(a 2-day staleness bar sits inside the NFL's weekly gaps).

Honesty rules (binding):
  * Lines are MODEL lines. They are stamped classification "Estimated"
    with the model name/version and the data's as-of date, and they
    must never be presented as a sportsbook's price. Ticket pricing
    belongs to the book; this module publishes no odds.
  * A line exists only where real harvested logs support it: fewer
    than MIN_GAMES games in the window -> no line, counted in the
    report's skipped tally. Nothing is fabricated from thin data.
  * Only statistics a line can meaningfully attach to get one:
    integer counting stats (half-point lines, book convention) and
    minutes. Rate stats (percentages) and boolean markets
    (double-double etc.) get no line — their projection probability
    already IS the product, and inventing a line for them would be
    decoration, not information. Skips are counted, never silent.

CLI:
  python3 player_prop_lines.py --data-root <path> --league NBA
Writes <data-root>/props/model_lines_<LEAGUE>.json and prints a report.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from player_game_logs import GameLogStore
    from player_props_projection import (
        MIN_GAMES, WINDOWS, distribution_median, distribution_p_over,
        fit_distribution, normal_cdf)
    from player_props_taxonomy import MARKETS
except ImportError:  # package-style import (sports-feed repo layout)
    from intelligence.player_game_logs import GameLogStore  # type: ignore
    from intelligence.player_props_projection import (  # type: ignore
        MIN_GAMES, WINDOWS, distribution_median, distribution_p_over,
        fit_distribution, normal_cdf)
    from intelligence.player_props_taxonomy import MARKETS  # type: ignore

LINE_MODEL_NAME = "intelligence-engine-fair-line"
LINE_MODEL_VERSION = "1.1"
DEFAULT_WINDOW = "L10"

# statistic types a posted line can attach to. "number" is admitted
# only for minutes (a volume stat measured continuously); rate stats
# are excluded by key below.
_LINEABLE_NUMBER_KEYS = {"minutes"}

# Publish-gate thresholds per league:
#   prior_min   — prior-season games at/above which a player counts
#                 as "established" for the plausibility check.
#   current_max — current-season games at/below which an established
#                 player's record is implausibly thin.
#   stale_days  — the store's newest game may lag the harvest's
#                 as-of date by at most this many days.
# NBA: the original Nov 20, 2026 calibration (82-game season,
# near-nightly games). NFL: a 17-game season — 14 prior games is a
# near-full season — on a weekly rhythm whose natural gaps run 3-4
# days, so staleness allows 5. A league with no entry falls back to
# the NBA numbers (the strictest bar) rather than skipping the gate.
# MLB: calibrated Nov 21, 2026 for a COMPLETED-season store — the
# 2026 regular season ended Sep 27, 2026 and the store is static
# until the 2027 season opens, so staleness must tolerate the whole
# off-season (220 days reaches late April 2027) or the gate would
# withhold the league for the calendar, not for bad data. prior_min
# 100 is a near-full 162-game season for an everyday player; with a
# single-season store the suspect rule cannot fire at all (every
# player's prior-season count is 0). THESE NUMBERS MUST BE REVISITED
# WHEN THE 2027 SEASON OPENS, when live data resumes and the suspect
# rule gains a prior season to bite on.
GATE_THRESHOLDS = {
    "NBA": {"prior_min": 40, "current_max": 3, "stale_days": 2},
    "NFL": {"prior_min": 14, "current_max": 3, "stale_days": 5},
    "MLB": {"prior_min": 100, "current_max": 5, "stale_days": 220},
}
_DEFAULT_GATE_LEAGUE = "NBA"


def gate_thresholds(league: str | None) -> dict:
    """The publish-gate thresholds in force for a league."""
    return GATE_THRESHOLDS.get(league or _DEFAULT_GATE_LEAGUE,
                               GATE_THRESHOLDS[_DEFAULT_GATE_LEAGUE])


def _window_values(series: list[dict], window: str) -> list[float]:
    """Mirror player_props_projection._window_slice semantics: the
    series is chronological; the window takes the most recent games."""
    size = WINDOWS[window]
    sliced = series if size is None else series[-size:]
    return [float(g["value"]) for g in sliced
            if isinstance(g.get("value"), (int, float))]


def _p_over(fit: dict, line: float, statistic_type: str) -> float:
    if statistic_type == "integer":
        return distribution_p_over(fit, math.floor(line) + 0.5,
                                   statistic_type)
    return distribution_p_over(fit, line, statistic_type)


def fair_line(values: list[float], statistic_type: str) -> dict | None:
    """The fair line for one player's windowed values, or None when
    the sample cannot support one.

    Integer statistics: the half-point line whose over-probability
    under the fitted count distribution sits closest to 0.5 — the
    distribution's median, in book convention (no pushes). Minutes:
    the nearest half point to the mean, as before.
    """
    if len(values) < MIN_GAMES:
        return None
    fit = fit_distribution(values, statistic_type)
    mu = fit["mean"]
    median = distribution_median(fit, statistic_type)
    if statistic_type == "integer":
        line, best_gap = None, 2.0
        lo = max(0, int(median) - 3)
        # Deterministic order: closest to the mean's bucket first, so
        # an exact tie resolves toward the book-convention line.
        ks = sorted(range(lo, int(median) + 4),
                    key=lambda k: (abs(k - math.floor(mu)), k))
        for k in ks:
            cand = k + 0.5
            gap = abs(_p_over(fit, cand, statistic_type) - 0.5)
            if gap < best_gap:
                line, best_gap = cand, gap
    else:  # minutes: nearest half point
        line = round(mu * 2) / 2
        if line == int(line):
            line += 0.5
    p_over = _p_over(fit, line, statistic_type)
    alternates = []
    for alt in (line - 2.0, line - 1.0, line + 1.0, line + 2.0):
        if alt >= 0.5:
            alternates.append({
                "line": alt,
                "probability_over": round(
                    _p_over(fit, alt, statistic_type), 4),
            })
    return {
        "line": line,
        "probability_over": round(p_over, 4),
        "probability_under": round(1.0 - p_over, 4),
        "mean": round(mu, 3),
        "stdev": round(fit["stdev"], 3),
        "games_used": len(values),
        "player_projection": {
            "mean": round(mu, 3),
            "median": round(median, 3),
            "stdev": round(fit["stdev"], 3),
            "distribution": fit["family"],
            "games_used": len(values),
        },
        "alternates": alternates,
        # The market layer, kept separate by construction: no book
        # line, odds, or consensus is sourced yet, and these fields
        # stay null (never filled from the model) until one is.
        "market": {
            "sportsbook_line": None,
            "sportsbook_odds": None,
            "market_consensus": None,
            "status": "not_sourced",
        },
    }


def evaluate_publish_gate(players: dict,
                          league: str = _DEFAULT_GATE_LEAGUE) -> dict:
    """Per-player publish gate over the log store's players mapping.

    Returns {player_id: {"reasons": [...], "current_season_games": n,
    "prior_season_games": m}} for WITHHELD players only. The check is
    a completeness/plausibility rule, stated plainly: it says the
    record is suspect, not that it is certainly wrong — a genuinely
    injured established player trips it too, and his lines stay
    withheld until his data verifies. That asymmetry is the point of
    the gate. The established/thin bars are the league's own
    (GATE_THRESHOLDS): "established" means a near-full prior season
    IN THAT LEAGUE.
    """
    thresholds = gate_thresholds(league)
    all_seasons: set[str] = set()
    counts_by_player: dict[str, dict[str, int]] = {}
    for pid, prec in (players or {}).items():
        counts: dict[str, int] = {}
        for g in prec.get("games", []):
            counts[g.get("season")] = counts.get(g.get("season"), 0) + 1
            all_seasons.add(g.get("season"))
        counts_by_player[pid] = counts
    if not all_seasons:
        return {}
    latest = max(all_seasons)
    withheld: dict[str, dict] = {}
    for pid, counts in counts_by_player.items():
        current = counts.get(latest, 0)
        prior = sum(v for s, v in counts.items() if s != latest)
        if current <= thresholds["current_max"] \
                and prior >= thresholds["prior_min"]:
            withheld[pid] = {
                "reasons": ["suspect_current_season"],
                "current_season_games": current,
                "prior_season_games": prior,
            }
    return withheld


def store_stale_reason(players: dict, data_as_of: str | None,
                       league: str = _DEFAULT_GATE_LEAGUE) -> str | None:
    """The 'too old' half of the publish gate: when the store's
    newest game is more than the league's stale_days behind the
    harvest's as-of date, the whole publication is stale. Returns a
    reason or None."""
    if not data_as_of:
        return None
    dates = [g.get("date") for prec in (players or {}).values()
             for g in prec.get("games", []) if g.get("date")]
    if not dates:
        return None
    try:
        as_of = datetime.fromisoformat(str(data_as_of)[:10]).date()
        newest = datetime.fromisoformat(str(max(dates))[:10]).date()
    except ValueError:
        return None
    if (as_of - newest).days > gate_thresholds(league)["stale_days"]:
        return "stale_store"
    return None


def build_lines(league: str, log_store, window: str = DEFAULT_WINDOW,
                data_as_of: str | None = None,
                source: str | None = None) -> dict:
    markets = [m for m in MARKETS
               if m.get("league") == league and m.get("active")]
    lines: list[dict] = []
    skipped: dict[str, int] = {}
    players_seen: set[str] = set()

    players_doc = log_store.document.get("players") or {}
    withheld = evaluate_publish_gate(players_doc, league)
    stale = store_stale_reason(players_doc, data_as_of, league)
    if stale:
        withheld = {
            pid: {"reasons": [stale],
                  "current_season_games": 0, "prior_season_games": 0}
            for pid in players_doc}
    for pid in withheld:
        skipped["withheld_by_publish_gate"] = \
            skipped.get("withheld_by_publish_gate", 0) + 1

    for market in markets:
        key = market["statistic_key"]
        stype = market["statistic_type"]
        if stype == "boolean":
            skipped["boolean_market_no_line"] = \
                skipped.get("boolean_market_no_line", 0) + 1
            continue
        if stype == "number" and key not in _LINEABLE_NUMBER_KEYS:
            skipped["rate_stat_no_line"] = \
                skipped.get("rate_stat_no_line", 0) + 1
            continue
        for player_id in list(players_doc):
            if player_id in withheld:
                continue
            series = log_store.series(player_id, key)
            if not series:
                continue
            players_seen.add(player_id)
            result = fair_line(_window_values(series, window), stype)
            if result is None:
                skipped["insufficient_games"] = \
                    skipped.get("insufficient_games", 0) + 1
                continue
            lines.append({
                "player_id": player_id,
                "league": league,
                "statistic_key": key,
                "market_id": market.get("market_id"),
                **result,
                "window": window,
                "model_version": LINE_MODEL_VERSION,
                "data_as_of": data_as_of,
                "classification": "Estimated",
            })

    return {
        "classification": "Estimated",
        "kind": "model-lines",
        "league": league,
        "model": {"name": LINE_MODEL_NAME, "version": LINE_MODEL_VERSION},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_as_of": data_as_of,
        "source": source,
        "window": window,
        "players_covered": len(players_seen),
        "lines": lines,
        "skipped": skipped,
        "gate": {
            "status": "withheld_all" if stale else "active",
            "rule": "withhold when an established player's current-"
                    "season record is implausibly thin, and withhold "
                    "everything when the store is stale",
            "thresholds": gate_thresholds(league),
            "withheld_players": len(withheld),
        },
        "withheld": [
            {"player_id": pid, **info}
            for pid, info in sorted(withheld.items())],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/intelligence")
    parser.add_argument("--league", default="NBA")
    parser.add_argument("--window", default=DEFAULT_WINDOW,
                        choices=sorted(WINDOWS))
    args = parser.parse_args(argv)

    data_root = Path(args.data_root)
    log_path = data_root / "props" / f"game_logs_{args.league}.json"
    if not log_path.exists():
        print(json.dumps({"status": "no_log_store", "path": str(log_path)}))
        return 0
    raw = json.loads(log_path.read_text())
    coverage = raw.get("coverage") or {}
    data_as_of = coverage.get("as_of")
    source = coverage.get("source")
    if data_as_of is None:
        dates = [g.get("date") for p in (raw.get("players") or {}).values()
                 for g in p.get("games", []) if g.get("date")]
        data_as_of = max(dates) if dates else None

    store = GameLogStore.load(log_path)
    doc = build_lines(args.league, store, window=args.window,
                      data_as_of=data_as_of, source=source)
    out_path = data_root / "props" / f"model_lines_{args.league}.json"
    out_path.write_text(json.dumps(doc, indent=1))
    print(json.dumps({
        "status": "ok",
        "league": args.league,
        "players_covered": doc["players_covered"],
        "lines_published": len(doc["lines"]),
        "withheld_players": doc["gate"]["withheld_players"],
        "skipped": doc["skipped"],
        "data_as_of": data_as_of,
        "source": source,
        "out": str(out_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
