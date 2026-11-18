# data/fits — pipeline-published Simulation Lab fits

Quant Apparatus Phase C (spec §6). These artifacts are produced by
the fit pipeline, **not** by hand. The hand-built fits they supersede
live in the simulation-lab working tree (`data/<sport>-fit.json`,
fitVersion `0`) and remain the diff baseline.

## Layout

- `<sport>-fit.json` — one FitArtifact per sport (Phase A contract
  `fit-artifact`, envelope level): parameters, per-team split tables,
  player stat SDs with provenance, `trainedOn`, `producedBy`,
  `producedAt`.
- `manifest.json` — the consumer index. Per sport: `fitVersion`,
  artifact filename, `sha256` of the artifact file as written,
  `trainedOn`, `publishedAt`, `producedBy`, and `supersedes` (the
  fitVersion this publication replaced). Consumers load fits
  **through the manifest** and verify the hash before trusting an
  artifact.

## Versioning scheme

`fitVersion = 1.<gamesUsed>.<yyyymmdd>` (UTC date of the run).

- `1` is the pipeline era. The hand-built artifacts are version `0`.
- `<gamesUsed>` is the number of games the fit trained on — the
  version changes exactly when the training set does, and the gate
  guarantees it never decreases for a published fit.
- `<yyyymmdd>` disambiguates same-size republications (e.g. a code
  fix in the fit math). The manifest's `sha256` is the ultimate
  content identity; two artifacts with the same version and
  different hashes differ, and the manifest says which one is
  current.

## The gate (what "published" means)

Every artifact here passed all four checks in
`simulation-lab/scripts/fit-gate.mjs` at publication time:

1. **Count** — `gamesUsed` ≥ the incumbent fit's.
2. **Bands** — headline parameters inside documented per-sport
   plausible bands.
3. **Coverage** — leave-one-season-out backtest with the shipping
   simulation machinery beats the home-win base-rate baseline on
   out-of-sample Brier (skill > 0) with pooled Brier ≤ 0.26.
4. **Contract** — envelope-level validation against the Phase A
   FitArtifact schema.

A candidate that fails any check is not published; the manifest
keeps pointing at the last good artifact. The pipeline exits
non-zero and names the failed check.

## Player stat SD provenance (fitted vs tagged)

`playerStatSDSource` travels inside each artifact and says, in
words, what the numbers are:

- **NBA / NFL — FITTED.** Pooled within-player residual SDs over
  regulars from the player fan-out's per-game logs
  (`scripts/fit-player-sds.mjs`; NBA season 2024-2025, minGames 15;
  NFL season 2024, minGames 10 — the same settings that produced the
  hand-built artifacts' SDs, which the pipeline reproduces exactly).
- **MLB / NHL — TAGGED PARAMETERS, not fitted.** The fan-out does
  not cover baseball or hockey, so these SDs are league-typical
  parameter values. They are labeled as parameters in the artifact;
  no consumer may treat them as fitted values.

## Training-set note: the overtime correction

The hand-built extraction filtered `status.short == 'FT'` only and
silently dropped every overtime game the API marks `AOT` — 222 NBA
games (86 / 66 / 70 across the three seasons) and Super Bowl LVIII
in the NFL set. The pipeline extractor accepts FT and AOT (finished
games), so pipeline NBA/NFL fits train on the complete set. The
parameter deltas vs the hand-built fits are the expected signature
of adding overtime games (NBA: leagueAvg +0.57, totalSD +0.73,
marginSD −0.28); MLB and NHL — whose feeds mark extra-inning /
overtime games FT — reproduce the hand-built parameters exactly.
Full diff: `simulation-lab/fit-pipeline-diff-2026-11-18.md`.

## Running the pipeline by hand

```sh
cd ~/workspace/simulation-lab
node scripts/fit-pipeline.mjs            # all four sports, publish
node scripts/fit-pipeline.mjs --dry-run  # fit + gate, publish nothing
node scripts/fit-pipeline.mjs --sport NBA
```

Defaults read the harvest raw store from this data-branch checkout
(`data/intelligence`) and publish into this directory. The fit is
local compute over files — no provider calls. Player SD refits read
the fan-out logs from `~/workspace/.tmp-fanout` until the fan-out
lands on this branch (the Legion runner is its production home);
when the Legion harvest runs, this pipeline runs on the same
machine against the same checkouts with no changes.

Cadence: weekly (and whenever a sport's held seasons grow). The
GitHub form of the schedule is `sports-feed/.github/workflows/
fit-pipeline.yml` — created locally, not yet pushed (the repo's gh
token lacks the `workflow` scope; see the file header). Until the
pipeline's canonical home (simulation-lab) is mirrored into git,
the operative cadence is the manual command above or a Legion task.

## What these fits may feed

Lab simulations (SIMULATED class) and Sage's Estimated answers
under the Engine's classification law (spec §6.2). They do **not**
touch the Current model: recalibration of the model of record is
governed by §6.3 and Isaac's go, unchanged by this pipeline.
