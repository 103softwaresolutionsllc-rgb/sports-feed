"""distribution_validation_v12.py — line-model v1.2 distribution test.

Follow-up to distribution_validation.py (the v1 pass). The v1 pass
scored candidates by exact-outcome log score on 2025-2026 and found
the smoothed empirical L10 distribution ahead of every parametric
family — but exact-outcome log score is the metric that flatters a
step-function distribution most. This pass scores the candidates
where a line-making model actually earns its keep, on the untouched
holdout (2026-2027 games only):

  negbin_l10     the shipped v1.1 fit, reused as-is from
                 player_props_projection (negative binomial when the
                 L10 window is overdispersed, Poisson otherwise)
  empirical_l10  smoothed empirical distribution over the same L10
                 window: add-0.5 smoothing as in the v1 pass,
                 p(k) = (count(k) + 0.5) / (n + 0.5 * K), with the
                 support K = the distinct values IN THE WINDOW. (The
                 v1 log-score pass also added the outcome itself to
                 the support; for calibration/Brier scoring that would
                 leak the outcome into the prediction, so it is not
                 done here.)
  blend          50/50 mixture of the two: p_over is the average of
                 the two candidates' p_over (equivalently, the CDFs
                 are averaged).

Metrics (integer statistics; every candidate):
  (a) fair-line calibration — each model's OWN fair (median)
      half-point line, selected with the production procedure from
      player_prop_lines.fair_line (candidates median±3, ties toward
      the mean's bucket); report the actual over-hit rate and
      |over_rate - 0.5|.
  (b) Brier score for P(over) at COMMON lines — the v1.1 model's
      fair line and offsets -2, -1, +1, +2 (half-point lines kept;
      for sparse stats an offset can go below 0.5, where every model
      says 1.0 and the outcome is always over — kept as specified).
      Brier = mean((p_over - outcome)^2) over all evaluated lines.

Evaluation set: outcomes are 2026-2027 games only, each with at
least 10 prior games in the player's full series. The fitting
window is the prior 10 games and MAY reach back into 2025-2026 for
early-season games — windows always precede outcomes, so this is
legitimate and matches how the live engine fits in October.

Number statistics: minutes gets the v1.1 (Gaussian) fit only —
calibration and Brier at its own fair line +/- offsets — because
the empirical/blend candidates are count distributions.
field_goal_percentage is not a lineable market (no lines are posted
for it); its series size is reported and it is excluded from line
metrics.

Usage:
  python3 distribution_validation_v12.py \
      --data-root <.../intelligence>
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from player_game_logs import GameLogStore  # noqa: E402
from player_props_projection import (  # noqa: E402
    distribution_median, distribution_p_over, fit_distribution)
from player_props_taxonomy import markets_for_league  # noqa: E402

WINDOW = 10
MIN_PRIOR = 10
EVAL_SEASON = "2026-2027"
MODELS = ["negbin_l10", "empirical_l10", "blend"]
OFFSETS = [-2.0, -1.0, 0.0, 1.0, 2.0]


# ---------------------------------------------------------------------------
# Empirical candidate (add-0.5 smoothed window frequencies)
# ---------------------------------------------------------------------------

def _emp_fit(window):
    counts = defaultdict(int)
    for v in window:
        counts[int(round(v))] += 1
    denom = len(window) + 0.5 * len(counts)
    return counts, denom


def _emp_p_over(efit, line: float) -> float:
    counts, denom = efit
    return sum((c + 0.5) / denom for k, c in counts.items() if k > line)


def _emp_cdf(efit, k: int) -> float:
    counts, denom = efit
    return sum((c + 0.5) / denom for v, c in counts.items() if v <= k)


def _emp_median(efit) -> float:
    counts, _ = efit
    for k in sorted(counts):
        if _emp_cdf(efit, k) >= 0.5:
            return float(k)
    return float(max(counts))


# ---------------------------------------------------------------------------
# Fair-line selection — the production procedure from
# player_prop_lines.fair_line, parameterized by the model's p_over.
# ---------------------------------------------------------------------------

def _fair_line(p_over_fn, median: float, mu: float) -> float:
    line, best_gap = None, 2.0
    lo = max(0, int(median) - 3)
    ks = sorted(range(lo, int(median) + 4),
                key=lambda k: (abs(k - math.floor(mu)), k))
    for k in ks:
        cand = k + 0.5
        gap = abs(p_over_fn(cand) - 0.5)
        if gap < best_gap:
            line, best_gap = cand, gap
    return line


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--league", default="NBA")
    args = ap.parse_args(argv)

    data_root = Path(args.data_root)
    store = GameLogStore.load(data_root / "props"
                              / f"game_logs_{args.league}.json")
    markets = [m for m in markets_for_league(args.league)
               if m.get("active") and m["statistic_type"] in
               ("integer", "number")]
    stat_keys = [m["statistic_key"] for m in markets]
    type_of = {m["statistic_key"]: m["statistic_type"] for m in markets}

    # cal[stat][model] = [over_hits, n]
    # brier[stat][model] = [sq_err_sum, line_count]
    cal = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    brier = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    series_n = defaultdict(int)

    players = store.document.get("players") or {}
    for player_id, rec in players.items():
        games = sorted(rec.get("games", []),
                       key=lambda g: g.get("date") or "")
        for key in stat_keys:
            series = []
            for g in games:
                stats = g.get("stats") or {}
                if key in stats and stats[key] is not None:
                    series.append((g, float(stats[key])))
            if len(series) <= MIN_PRIOR:
                continue
            integer = type_of[key] == "integer"
            for t in range(MIN_PRIOR, len(series)):
                game, actual = series[t]
                if game.get("season") != EVAL_SEASON:
                    continue
                window = [v for _, v in series[t - WINDOW:t]]
                series_n[key] += 1
                mu = statistics.fmean(window)

                if not integer:
                    # v1.1 fit only (Gaussian). Fair line = the fit's
                    # median (its mean); minutes also gets Brier at
                    # its own fair line +/- offsets.
                    fit = fit_distribution(window, "number")
                    med = distribution_median(fit, "number")
                    cell = cal[key]["negbin_l10"]
                    cell[1] += 1
                    if actual > med:
                        cell[0] += 1
                    if key == "minutes":
                        base = round(mu * 2) / 2
                        if base == int(base):
                            base += 0.5
                        bc = brier[key]["negbin_l10"]
                        for off in OFFSETS:
                            line = base + off
                            p = distribution_p_over(fit, line, "number")
                            bc[0] += (p - (1.0 if actual > line
                                           else 0.0)) ** 2
                            bc[1] += 1
                    continue

                a_int = int(round(actual))
                fit = fit_distribution(window, "integer")
                efit = _emp_fit(window)

                def nb_p(L, fit=fit):
                    return distribution_p_over(fit, L, "integer")

                def em_p(L, efit=efit):
                    return _emp_p_over(efit, L)

                def bl_p(L, fit=fit, efit=efit):
                    return 0.5 * (distribution_p_over(fit, L, "integer")
                                  + _emp_p_over(efit, L))

                p_fns = {"negbin_l10": nb_p, "empirical_l10": em_p,
                         "blend": bl_p}

                # Blend median: smallest k whose blended CDF >= 0.5.
                nb_med = distribution_median(fit, "integer")
                em_med = _emp_median(efit)
                hi = int(max(nb_med, em_med)) + 3
                bl_med = float(hi)
                for k in range(0, hi + 1):
                    cdf = 0.5 * ((1.0 - nb_p(float(k)))
                                 + _emp_cdf(efit, k))
                    if cdf >= 0.5:
                        bl_med = float(k)
                        break
                medians = {"negbin_l10": nb_med,
                           "empirical_l10": em_med, "blend": bl_med}

                fair = {m: _fair_line(p_fns[m], medians[m], mu)
                        for m in MODELS}
                for m in MODELS:
                    cell = cal[key][m]
                    cell[1] += 1
                    if a_int > fair[m]:
                        cell[0] += 1

                common = [fair["negbin_l10"] + off for off in OFFSETS]
                for m in MODELS:
                    bc = brier[key][m]
                    for line in common:
                        p = p_fns[m](line)
                        bc[0] += (p - (1.0 if a_int > line
                                       else 0.0)) ** 2
                        bc[1] += 1

    print(f"Line-model v1.2 test — {args.league} holdout {EVAL_SEASON} "
          f"(rolling origin, L{WINDOW}; outcomes {EVAL_SEASON} only, "
          f"windows may reach into 2025-2026)\n")
    print(f"{'stat':<26}{'model':<14}{'n':>7}{'over%':>8}"
          f"{'|cal err|':>10}{'brier':>9}")
    summary = {}
    pooled_cal = defaultdict(lambda: [0, 0])
    pooled_brier = defaultdict(lambda: [0.0, 0])
    for key in stat_keys:
        if series_n.get(key, 0) == 0:
            continue
        rows = {}
        for model in MODELS:
            hits, n = cal[key][model]
            if n == 0:
                continue
            over_rate = hits / n
            bs, bc = brier[key][model]
            rows[model] = {
                "n": n,
                "over_rate": round(over_rate, 4),
                "cal_err": round(abs(over_rate - 0.5), 4),
                "brier": round(bs / bc, 4) if bc else None,
                "brier_lines": bc,
            }
            if type_of[key] == "integer":
                pooled_cal[model][0] += hits
                pooled_cal[model][1] += n
                pooled_brier[model][0] += bs
                pooled_brier[model][1] += bc
            print(f"{key:<26}{model:<14}{n:>7}"
                  f"{100.0 * over_rate:>7.1f}%"
                  f"{abs(over_rate - 0.5):>10.4f}"
                  f"{(bs / bc) if bc else float('nan'):>9.4f}")
        summary[key] = rows
        print()

    overall = {}
    print("OVERALL (integer stats only — the stats where all three "
          "candidates compete)")
    for model in MODELS:
        per_stat = [summary[k][model] for k in summary
                    if type_of[k] == "integer" and model in summary[k]
                    and summary[k][model]["brier"] is not None]
        if not per_stat:
            continue
        mean_cal_err = sum(r["cal_err"] for r in per_stat) / len(per_stat)
        hits, n = pooled_cal[model]
        bs, bc = pooled_brier[model]
        overall[model] = {
            "stats": len(per_stat),
            "mean_cal_err": round(mean_cal_err, 4),
            "pooled_over_rate": round(hits / n, 4) if n else None,
            "pooled_cal_err": round(abs(hits / n - 0.5), 4) if n
            else None,
            "pooled_brier": round(bs / bc, 4) if bc else None,
            "predictions": n,
            "brier_lines": bc,
        }
        print(f"{model:<14}mean|cal err| {mean_cal_err:.4f}   "
              f"pooled over {100.0 * hits / n:.1f}%   "
              f"pooled Brier {bs / bc:.4f}   "
              f"({n} predictions, {bc} lines)")
    print()
    print(json.dumps({"per_stat": summary, "overall": overall},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
