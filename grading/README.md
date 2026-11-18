# The Grading Ledger

Phase B (B1) of the BetTracker Quant Apparatus interconnection spec
(`~/workspace/your_files/bettracker-quant-apparatus-interconnection-spec-2026-11-18.md`, §5).
The apparatus's P&L attribution: every probability the system publishes
is archived, matched to the final score, and graded — permanently —
so the model's honesty is a measured fact, not an impression.

## Layout

Code (this repo, `main`):

| File | Job |
|---|---|
| `grading/store.mjs` | Append-only JSONL ledger: contract-validated appends, idempotent re-runs, supersede-by-id corrections |
| `grading/math.mjs` | Brier + favored-side calibration buckets (spec §5.1: 0.5–0.6 … 0.9–1.0) |
| `grading/records.mjs` | GradeRecord construction + the stream registry + odds math (implied, devig) |
| `grading/report.mjs` | Report generation (JSON + Markdown) from the ledger |
| `grading/results.mjs` | Final scores from the data branch: ESPN results cache + harvested payloads |
| `grading/fetch-results.mjs` | Bounded ESPN scoreboard fetcher that fills the results cache |
| `grading/run.mjs` | The nightly job: materialize archives → grade → append → report |
| `grading/backfill.mjs` | One-time builder of prediction archives from Blob history |
| `predictions.mjs` | The collector's archive step (gradable content per edition) |
| `model/` | Canonical port of the app's board model — see `model/PROVENANCE.md` |

Data (the `data` branch):

```
data/predictions/feed-<timestamp>.json   one archive per collector edition
data/grades/<YYYY-MM>.jsonl              the ledger, filed by append month
data/grades/results/espn-<SPORT>-<YYYY-MM>.json   archived finals (espn-fallback)
data/grades/reports/latest.{json,md}     regenerated reports, never hand-edited
```

## The streams (never blended — in records, in reports, anywhere)

- **`sim-backtest`** (SIMULATED): the Lab's Phase 3 backtest
  probabilities. The acceptance stream — the ledger's math was proven
  against it (Δ 0.0000 on all four sports, see
  `simulation-lab/scripts/ledger-acceptance.mjs`) before live numbers
  were trusted.
- **`app-model`** (Estimated): the board's Gaussian probabilities,
  computed by the canonical port from archived editions — the same
  inputs the app consumed (edition aggregates, edition slate for rest
  days). This is the stream that printed 91.8% / 93.5% on Nov 17.
- **`book-implied`** (Live): devigged bookmaker moneylines from the
  archived line sets. The market's opinion, kept so the model's record
  can be compared against it. **Not the model.**
- **`book-implied-pick-card`** (Live): the feed pick cards' published
  `winProb` — the favorite's RAW implied percentage (gap G4) — graded
  as published, in its own stream because it is computed differently
  from the devigged line-set numbers.

One graded pick per game per stream (home moneyline; for cards, the
card's favorite pick as published). Grading both sides of one
probability would double-count a single opinion.

## Conventions

- **Edition selection:** a game is graded from the LAST edition
  published before its start that carried it — the closing pre-game
  read, the ledger's equivalent of a closing line.
- **Brier:** win (p−1)², loss (p−0)², push (p−0.5)²; records with
  `modelProb: null` are counted in samples but excluded from Brier and
  calibration — never backfilled.
- **Calibration** folds probabilities to the favored side
  (p′ = max(p, 1−p)); folding leaves Brier unchanged.
- **`settledAt`** on live-stream grades is the game's start timestamp
  (the event settled); `fetchedAt` is when the grade was written.
  On `sim-backtest` grades `settledAt` is the grading timestamp — the
  harness's game rows carry no dates (their source payloads do;
  recovering them is future work, noted in the acceptance script).
- **Idempotency identity** is a record's substance — all fields except
  `fetchedAt`. A rebuild that changes nothing substantive appends
  nothing; a substantive change supersedes by id as a new line.

## Recorded gaps and findings (Phase B)

- **Injury extras are not archived.** The app's on-device read adds an
  injury edge from a live ESPN injury report; no archive of that
  report exists, so reconstructed app-model probabilities use the
  app's own no-entry default (impact 0). Grades therefore measure the
  model's deterministic core. See `model/PROVENANCE.md`.
- **The Nov 17 hockey aggregates window.** Editions published on
  Nov 17 carried a Hockey aggregates doc with zero teams (the doc was
  mid-rebuild the night the rule-10 ESPN fallback landed the real NHL
  data), so the board's NHL model reads were withheld that day — on
  Isaac's phone exactly as in this reconstruction. Later editions
  carry all 33 teams. The ledger records the withholding faithfully
  (NHL games from that window have book-implied grades only). A
  health board (Phase E) is what surfaces this class of event.
- **Board history depth.** The Blob history index reaches back to
  Nov 6, 2026, but board sections with aggregates exist only from
  Nov 17 — the first live report's window is therefore days, not
  weeks, and its samples are small. The archive step now runs every
  collector tick, so the window grows on its own.
- **Pick cards** exist only in editions from the API-Sports era
  (the pass is gated off since the suspension) plus, prospectively,
  the Phase 5 failover editions once deployed.
- **B2 (parlay grades)** remains a contract and an on-device report
  until Isaac rules on sync (spec Open Decision 4). The ledger never
  reaches for the phone.
