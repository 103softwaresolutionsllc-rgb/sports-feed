"""player_prop_lines.py — the Intelligence Engine's fair-line maker.

Makes the engine's OWN prop lines from harvested player game logs, the
way a sportsbook's model desk does: fit the player's recent-form
distribution for a statistic, then post the line at the distribution's
median so the over and under are as close to even as the data allows.

The math deliberately mirrors player_props_projection.project_selection
EXACTLY (same window, same Gaussian, same continuity correction, same
MIN_GAMES floor), so a model line and the app's projection of a
selection at that line always agree: the published probability_over is
what project_selection returns for an over at the published line.

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
    from player_props_projection import MIN_GAMES, WINDOWS, normal_cdf
    from player_props_taxonomy import MARKETS
except ImportError:  # package-style import (sports-feed repo layout)
    from intelligence.player_game_logs import GameLogStore  # type: ignore
    from intelligence.player_props_projection import (  # type: ignore
        MIN_GAMES, WINDOWS, normal_cdf)
    from intelligence.player_props_taxonomy import MARKETS  # type: ignore

LINE_MODEL_NAME = "intelligence-engine-fair-line"
LINE_MODEL_VERSION = "1.0"
DEFAULT_WINDOW = "L10"

# statistic types a posted line can attach to. "number" is admitted
# only for minutes (a volume stat measured continuously); rate stats
# are excluded by key below.
_LINEABLE_NUMBER_KEYS = {"minutes"}


def _window_values(series: list[dict], window: str) -> list[float]:
    """Mirror player_props_projection._window_slice semantics: the
    series is chronological; the window takes the most recent games."""
    size = WINDOWS[window]
    sliced = series if size is None else series[-size:]
    return [float(g["value"]) for g in sliced
            if isinstance(g.get("value"), (int, float))]


def fair_line(values: list[float], statistic_type: str) -> dict | None:
    """The fair line for one player's windowed values, or None when
    the sample cannot support one. Book convention for counting
    stats: the half-point nearest the mean (no pushes, threshold ==
    line under the projection engine's continuity correction)."""
    if len(values) < MIN_GAMES:
        return None
    mu = statistics.fmean(values)
    sigma = statistics.stdev(values) if len(values) > 1 else 0.0
    if statistic_type == "integer":
        line = math.floor(mu) + 0.5
    else:  # minutes: nearest half point
        line = round(mu * 2) / 2
        if line == int(line):
            line += 0.5
    if sigma > 0:
        p_over = 1.0 - normal_cdf((line - mu) / sigma)
    else:
        p_over = 1.0 if mu > line else 0.0 if mu < line else 0.5
    return {
        "line": line,
        "probability_over": round(p_over, 4),
        "probability_under": round(1.0 - p_over, 4),
        "mean": round(mu, 3),
        "stdev": round(sigma, 3),
        "games_used": len(values),
    }


def build_lines(league: str, log_store, window: str = DEFAULT_WINDOW,
                data_as_of: str | None = None,
                source: str | None = None) -> dict:
    markets = [m for m in MARKETS
               if m.get("league") == league and m.get("active")]
    lines: list[dict] = []
    skipped: dict[str, int] = {}
    players_seen: set[str] = set()

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
        for player_id in list((log_store.document.get("players") or {})):
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
        "skipped": doc["skipped"],
        "data_as_of": data_as_of,
        "source": source,
        "out": str(out_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
