"""distribution_validation.py — out-of-sample distribution-fit test.

Rolling-origin backtest on REAL harvested game logs (never fixtures):
for every player-game with at least WINDOW prior games in the target
season, each candidate model is fit on the prior window only and
scored on the actual outcome with the log score (a proper scoring
rule — higher is better). Candidates:

  gauss_l10            the incumbent: Gaussian, L10 mean / sample sd,
                       continuity-corrected buckets for integer stats
  gauss_l10_recency    same, exponentially recency-weighted (HL=5)
  gauss_season         Gaussian over the expanding season window
  empirical_l10        window frequencies, add-0.5 smoothed
  poisson_l10          Poisson, lambda = L10 mean        (counts)
  negbin_l10           negative binomial, method of moments (counts)
  minutes_conditioned  Gaussian with the MEAN conditioned on
                       opportunity: per-minute rate x projected
                       minutes (spread stays the incumbent's)

Also recorded per model: its own fair line (the half-point threshold
whose over-probability is closest to 0.5) and whether the actual went
over — a calibrated median should hit ~50%.

Usage:
  python3 distribution_validation.py --data-root <.../intelligence> \
      --season 2025-2026
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
from player_props_taxonomy import markets_for_league  # noqa: E402

WINDOW = 10
MIN_PRIOR = 10

MODELS = ["gauss_l10", "gauss_l10_recency", "gauss_season",
          "empirical_l10", "poisson_l10", "negbin_l10",
          "minutes_conditioned"]


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gauss_bucket_logp(mu, sigma, actual, integer: bool) -> float:
    if sigma <= 0:
        hit = (mu == actual) if integer else abs(mu - actual) < 1e-9
        return math.log(0.99) if hit else math.log(0.01)
    if integer:
        lo = _norm_cdf((actual - 0.5 - mu) / sigma)
        hi = _norm_cdf((actual + 0.5 - mu) / sigma)
        p = max(hi - lo, 1e-12)
    else:
        z = (actual - mu) / sigma
        p = math.exp(-0.5 * z * z) / (sigma * math.sqrt(2 * math.pi))
        p = max(p, 1e-12)
    return math.log(p)


def _gauss_p_over(mu, sigma, line: float) -> float:
    if sigma <= 0:
        return 1.0 if mu > line else 0.0 if mu < line else 0.5
    return 1.0 - _norm_cdf((line - mu) / sigma)


def _poisson_logp(lam, k) -> float:
    lam = max(lam, 1e-9)
    return -lam + k * math.log(lam) - math.lgamma(k + 1)


def _poisson_cdf(lam, k) -> float:
    return sum(math.exp(_poisson_logp(lam, i)) for i in range(0, k + 1))


def _negbin_params(mu, var):
    if var <= mu or mu <= 0:
        return None
    r = mu * mu / (var - mu)
    p = r / (r + mu)
    return r, p


def _negbin_logp(r, p, k) -> float:
    return (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
            + r * math.log(p) + k * math.log(max(1 - p, 1e-12)))


def _negbin_cdf(r, p, k) -> float:
    return sum(math.exp(_negbin_logp(r, p, i)) for i in range(0, k + 1))


def _weighted_mean_sd(values, weights):
    tw = sum(weights)
    mu = sum(v * w for v, w in zip(values, weights)) / tw
    var = sum(w * (v - mu) ** 2 for v, w in zip(values, weights)) / tw
    return mu, math.sqrt(var)


def _fair_line_from_p_over(p_over_fn, center: float) -> float:
    """Half-point line whose over-probability is closest to 0.5."""
    best, best_gap = None, 2.0
    base = math.floor(center)
    for cand in [base - 1.5, base - 0.5, base + 0.5, base + 1.5]:
        gap = abs(p_over_fn(cand) - 0.5)
        if gap < best_gap:
            best, best_gap = cand, gap
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--season", default="2025-2026")
    ap.add_argument("--league", default="NBA")
    args = ap.parse_args(argv)

    data_root = Path(args.data_root)
    store = GameLogStore.load(data_root / "props"
                              / f"game_logs_{args.league}.json")
    markets = [m for m in markets_for_league(args.league)
               if m.get("active") and m["statistic_type"] in
               ("integer", "number")]
    stat_keys = [m["statistic_key"] for m in markets]
    integer_keys = {m["statistic_key"] for m in markets
                    if m["statistic_type"] == "integer"}

    # scores[stat][model] = [logp_sum, n, over_hits, line_n]
    scores = defaultdict(lambda: defaultdict(
        lambda: [0.0, 0, 0, 0]))

    players = store.document.get("players") or {}
    for player_id, rec in players.items():
        games = sorted(
            (g for g in rec.get("games", [])
             if g.get("season") == args.season),
            key=lambda g: g.get("date") or "")
        if len(games) <= MIN_PRIOR:
            continue
        for key in stat_keys:
            series = []
            for g in games:
                stats = g.get("stats") or {}
                if key in stats and stats[key] is not None:
                    series.append((g, float(stats[key])))
            if len(series) <= MIN_PRIOR:
                continue
            integer = key in integer_keys
            for t in range(MIN_PRIOR, len(series)):
                window = [v for _, v in series[t - WINDOW:t]]
                season_vals = [v for _, v in series[:t]]
                actual = series[t][1]
                a_int = int(round(actual)) if integer else actual

                fits = {}
                mu, sd = statistics.fmean(window), (
                    statistics.stdev(window) if len(window) > 1 else 0.0)
                fits["gauss_l10"] = ("gauss", mu, sd)
                if integer:
                    counts = defaultdict(int)
                    for v in window:
                        counts[int(round(v))] += 1
                    support = sorted(set(counts) | {a_int})
                    fits["empirical_l10"] = ("empirical", counts, support)
                weights = [0.5 ** ((WINDOW - 1 - i) / 5.0)
                           for i in range(len(window))]
                wmu, wsd = _weighted_mean_sd(window, weights)
                fits["gauss_l10_recency"] = ("gauss", wmu, wsd)
                smu, ssd = statistics.fmean(season_vals), (
                    statistics.stdev(season_vals)
                    if len(season_vals) > 1 else 0.0)
                fits["gauss_season"] = ("gauss", smu, ssd)
                if integer:
                    fits["poisson_l10"] = ("poisson", mu, None)
                    var = statistics.variance(window) \
                        if len(window) > 1 else 0.0
                    nb = _negbin_params(mu, var)
                    fits["negbin_l10"] = ("negbin", nb, None) \
                        if nb else ("poisson", mu, None)
                    # minutes-conditioned mean
                    mins_w, mins_ok = [], True
                    for g, v in series[t - WINDOW:t]:
                        m = (g.get("stats") or {}).get("minutes")
                        if m is None or float(m) <= 0:
                            mins_ok = False
                            break
                        mins_w.append((float(m), v))
                    if mins_ok and key != "minutes":
                        rates = [v / m for m, v in mins_w]
                        proj_min = statistics.fmean(
                            [m for m, _ in mins_w])
                        fits["minutes_conditioned"] = (
                            "gauss", statistics.fmean(rates) * proj_min,
                            sd)

                for model, (kind, a, b) in fits.items():
                    cell = scores[key][model]
                    if kind == "empirical":
                        n_w = sum(a.values())
                        k = len(b)
                        p = (a.get(a_int, 0) + 0.5) / (n_w + 0.5 * k)
                        cell[0] += math.log(max(p, 1e-12))
                        cell[1] += 1
                        continue
                    if kind == "gauss":
                        logp = _gauss_bucket_logp(a, b, actual, integer)
                        p_over_fn = lambda L: _gauss_p_over(a, b, L)  # noqa: E731
                    elif kind == "poisson":
                        logp = _poisson_logp(a, a_int)
                        p_over_fn = lambda L: 1.0 - _poisson_cdf(a, int(math.floor(L)))  # noqa: E731
                    else:
                        r, p = a
                        logp = _negbin_logp(r, p, a_int)
                        p_over_fn = lambda L: 1.0 - _negbin_cdf(r, p, int(math.floor(L)))  # noqa: E731
                    cell[0] += logp
                    cell[1] += 1
                    if integer:
                        line = _fair_line_from_p_over(
                            p_over_fn, a if kind != "negbin" else
                            (r * (1 - p) / p))
                        cell[3] += 1
                        if actual > line:
                            cell[2] += 1

    print(f"Distribution validation — {args.league} {args.season} "
          f"(rolling origin, L{WINDOW}, log score; higher = better)\n")
    print(f"{'stat':<26}{'model':<20}{'n':>7}{'logscore':>10}"
          f"{'Δ vs gauss':>11}{'over%':>7}")
    verdicts = {}
    for key in stat_keys:
        if key not in scores:
            continue
        base = scores[key]["gauss_l10"]
        base_ls = base[0] / base[1] if base[1] else float("nan")
        best_model, best_ls = None, None
        for model in MODELS:
            cell = scores[key].get(model)
            if not cell or cell[1] == 0:
                continue
            ls = cell[0] / cell[1]
            over = (f"{100.0 * cell[2] / cell[3]:.0f}%"
                    if cell[3] else "—")
            print(f"{key:<26}{model:<20}{cell[1]:>7}{ls:>10.4f}"
                  f"{ls - base_ls:>+11.4f}{over:>7}")
            if best_ls is None or ls > best_ls:
                best_model, best_ls = model, ls
        verdicts[key] = {
            "best": best_model,
            "best_logscore": round(best_ls, 4),
            "gauss_l10_logscore": round(base_ls, 4),
            "margin": round(best_ls - base_ls, 4),
            "n": base[1],
        }
        print()
    print(json.dumps({"verdicts": verdicts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
