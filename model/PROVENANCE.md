# Canonical port of the BetTracker app model

Phase B of the BetTracker Quant Apparatus interconnection spec
(`~/workspace/your_files/bettracker-quant-apparatus-interconnection-spec-2026-11-18.md`, §5.1).

## What this is

The BetTracker app's board model — the closed-form Gaussian that prices
board games on-device (`computePreGameModel`: μ from per-game scoring
edges + form/rest/injury edges + home edge, probability =
`normCDF(μ/σ)`) — ported to a canonical shared module so the grading
ledger can compute the **app-model stream** server-side from archived
feed editions, with the same numbers the app produced.

This directory is the canonical home from Phase B onward. The app keeps
its own copies (it is not modified by Phase B); the equivalence test
(`tests/model-equivalence.test.mjs`) proves the port and the app
originals return identical outputs on fixture inputs, so drift between
the two is a test failure, not a silent divergence.

## Source

Copied from the BetTracker app repo (`bettracker-app`, HEAD `6550be3`)
on 2026-11-18:

| Ported file | App original |
|---|---|
| `utils/leagueConfig.js` | `src/utils/leagueConfig.js` (byte-identical) |
| `utils/predictFlowEngine.js` | `src/utils/predictFlowEngine.js` |
| `utils/monteCarloSim.js` | `src/utils/monteCarloSim.js` (byte-identical) |
| `services/board/boardEngineInputs.js` | `src/services/board/boardEngineInputs.js` |
| `services/board/boardModel.js` | `src/services/board/boardModel.js` |

The ONLY changes from the originals are mechanical: relative import
specifiers gain the `.js` extension Node ESM requires (the app's Metro
bundler resolves extensionless specifiers; Node does not). The layout
mirrors the app's (`utils/`, `board/`) so the specifiers' shapes are
unchanged. No logic was edited.

## What the model consumes (and what reconstruction can and cannot see)

`buildGameModel(game, aggregatesDoc, extras)` is deterministic given:

- the board game (teams, sport, start time) — archived in every feed
  edition's `board` section;
- the Intelligence Engine aggregates doc for the sport — republished by
  the collector into every edition's `aggregates` section, so the
  archive carries the exact doc the app consumed;
- `restDays` extras — the app derives them from the board's own game
  list (`restDaysFromSchedule`), also archived;
- `injuryImpact` extras — the app derives these from a live ESPN injury
  report fetched on-device (`injuryImpactFor` in
  SportsbookPredictScreen). **That report is not archived anywhere.**
  Reconstruction passes no injury extra, which is exactly the app's own
  behavior when its injury payload has no entry for a team
  (`injuryToLevel('None') = 0`). App-model grades therefore grade the
  model's deterministic core; on-device reads taken while a team
  carried a Medium/High injury impact will differ from the
  reconstructed probability by the injury edge ((inj₂ − inj₁) × 3.5
  points of μ). This gap is recorded here and in
  `grading/README.md`, not papered over.

Calibration note: the app's `calibrationEngine` adjustment is **not**
in the board path — it is applied only by the legacy
`betIntelligenceEngine.computeBetIntelligence` from the device's own
settled-ticket history. Board and ticket-leg probabilities (the Nov 17
91.8% / 93.5% favorites included) are the raw `computePreGameModel`
probabilities via `buildPriceCells`. The app-model stream therefore
grades the raw Gaussian, which is what the board actually published.
