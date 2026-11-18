# Grading ledger — calibration report

Generated 2026-11-18T09:44:21.478Z from 40 ledger records.

Streams are reported separately and are never blended. The book-implied streams grade the market’s prices, not the model.

## book-implied — Book-implied stream (devigged bookmaker prices — NOT the model)

### NHL

Sample: 11 graded picks (6W / 5L / 0P). Brier: **0.2458** over 11 probability-carrying grades.

| Bucket (favored side) | n | Mean predicted | Actual win rate | Deviation |
|---|---|---|---|---|
| 0.5-0.6 | 10 | 0.557 | 0.400 | 0.157 |
| 0.6-0.7 | 1 | 0.645 | 1.000 | 0.355 |

### NBA

Sample: 5 graded picks (2W / 3L / 0P). Brier: **0.2974** over 5 probability-carrying grades.

| Bucket (favored side) | n | Mean predicted | Actual win rate | Deviation |
|---|---|---|---|---|
| 0.5-0.6 | 2 | 0.565 | 1.000 | 0.435 |
| 0.6-0.7 | 1 | 0.677 | 0.000 | 0.677 |
| 0.7-0.8 | 2 | 0.748 | 0.500 | 0.248 |

## app-model — App model stream (the board's Gaussian probabilities, from archived editions)

### NBA

Sample: 5 graded picks (2W / 3L / 0P). Brier: **0.3092** over 5 probability-carrying grades.

| Bucket (favored side) | n | Mean predicted | Actual win rate | Deviation |
|---|---|---|---|---|
| 0.5-0.6 | 1 | 0.509 | 0.000 | 0.509 |
| 0.6-0.7 | 1 | 0.633 | 0.000 | 0.633 |
| 0.9-1.0 | 3 | 0.926 | 0.667 | 0.259 |

## book-implied-pick-card — Book-implied pick-card stream (published winProb, raw implied — NOT the model)

### NFL

Sample: 19 graded picks (14W / 5L / 0P). Brier: **0.1742** over 19 probability-carrying grades.

| Bucket (favored side) | n | Mean predicted | Actual win rate | Deviation |
|---|---|---|---|---|
| 0.5-0.6 | 1 | 0.580 | 0.000 | 0.580 |
| 0.6-0.7 | 11 | 0.656 | 0.636 | 0.020 |
| 0.7-0.8 | 4 | 0.735 | 1.000 | 0.265 |
| 0.8-0.9 | 3 | 0.837 | 1.000 | 0.163 |

## Source health

Nightly rollup: `data/health/latest.json` · Collector board: `health.json` (Blob store, beside feed.json). The board reports; it never changes a gate.

- api-sports — gated-off (not a link)
- espn-fallback — ok
- player-fan-out — not-in-production (not a link)
- fit-pipeline — ok
- grading-ledger — ok
- sim-service — not-deployed (not a link)

Needs attention:
- FLAG: API-Sports is not serving the harvest — games are coming from the ESPN fallback, so the games chain is down to its last verified link. API-Sports stays gated off until the Legion runner is installed.
- FLAG: soccer's newest harvest day (2026-11-18) carried no games from either source — neither API-Sports nor the ESPN fallback served it.
- FLAG: The player fan-out has never had a production run (proof runs only, Nov 17) — its production home is the Legion runner, which is not installed yet.
