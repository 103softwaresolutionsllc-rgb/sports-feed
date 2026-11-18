# Grading ledger — calibration report

Generated 2026-11-18T08:48:25.779Z from 40 ledger records.

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
