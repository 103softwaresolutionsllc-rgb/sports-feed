# The source-health board (Quant Apparatus Phase E, spec §8)

Rule 10 says every data dependency is served by an ordered chain of
sources, and a link that cannot serve real data does not count as a
link. This board is that doctrine made visible: one published record
per publisher describes every seam's state, so no source failure is
ever again discovered by screenshot (the API-Sports suspension of
Nov 17, 2026 surfaced ~a day late, in a dashboard screenshot).

## Who publishes what

| Board | Publisher | Where | When | Shape |
|---|---|---|---|---|
| Collector board | `api/collect.mjs` via `health.mjs` (`persistFeedHealth`) | `health.json` in the Vercel Blob store, beside `feed.json` | Every collector tick (~90 min) | SourceHealth contract, `publisher: "collector"` |
| Nightly rollup | `grading/run.mjs` via `grading/health-rollup.mjs` (`buildHealthRollup` / `writeHealthRollup`) | `data/health/latest.json` on the sports-feed `data` branch | Nightly, inside the grading run | SourceHealth contract, `publisher: "grading-rollup"` |
| Flags | The rollup's `flags`, rendered by `grading/report.mjs` | A **Source health** section in the nightly ledger report, `data/grades/reports/latest.md` | Nightly | One plain-language line per flag |

The contract is `contracts/source-health.schema.json` (registered in
`contracts/VERSIONS.md`, validated by `contracts/validate.mjs` as
`source-health`). Both publishers validate their record against it
before writing; an invalid board is never published.

### What each publisher may claim

A board entry is only as honest as its vantage point. Every entry
carries `observedBy`:

- **Collector board** (`observedBy: "collector"`): the sources the
  collector itself runs — the API-Sports picks pass (including its
  gate state and the suspension reason), The Odds API layer (status
  plus credit quota from the response headers the run actually saw),
  the ESPN news pass, and BetStack's fill gate — plus the seams it
  just executed: which source produced the edition's pick cards, and
  the prediction archive's counters for the edition (games archived,
  model-priced games, pick cards, from `persistFeedPredictions`'s
  return value).
- **Nightly rollup** (`observedBy: "data-branch"`): what the data
  branch's files show — ledger totals and per-sport grade freshness,
  fit versions and ages from `data/fits/manifest.json`, the harvest's
  serving source read from the stored payloads themselves (fallback
  payloads carry `parameters.source: "espn-fallback"`), the last
  harvest commit, and the fan-out's absence (`data/intelligence/props/`
  does not exist on the branch). The sim service is the one
  `standing-record` entry: its deployment state cannot be observed
  from the data branch, so the board records the build record —
  Phase D built, not deployed — and labels it as such.
- Anything a publisher cannot see is `not-observed`, in words. The
  rollup says plainly that it does not observe the collector's Blob
  board; the collector board says when an edition predates a label it
  would otherwise report.

## The rules the board lives by

1. **Never break the feed.** `persistFeedHealth` runs only after the
   feed write has succeeded, is fully guarded, and never throws —
   the same guarantee as `history.mjs` and `predictions.mjs`, except
   this one is test-proven with a simulated Blob failure
   (`tests/health.test.mjs`). The rollup is likewise a guarded
   passenger on the grading run: a rollup failure cannot break
   grading.
2. **The not-a-link rule.** An entry with `link: false` does not
   count toward its chain's depth. BetStack is the standing example:
   gate dark **and** probe failing (the key authenticates; every
   league query returns one identical unfiltered slate with no lines
   or scores), so it is published as `status: "not-a-link"` — never
   as a healthy standby. The same treatment applies to any gated-off
   or undeployed entry (the API-Sports passes while gated, the sim
   service while undeployed).
3. **The board never edits the chain.** No publisher changes a gate,
   a chain, a fit, or a deployment in response to what it records.
   There is no auto-ungating path anywhere in this code: gate changes
   are Isaac's acts, or the Legion runner's by design (it is the only
   environment that sets `INTELLIGENCE_ALLOW_API=1`). Surveillance is
   not control.
4. **Nulls over inventions.** `lastAttemptAt` is null when no attempt
   was made (a gated-off pass makes no call — that is the point of
   the gate). `lastGoodAt` is null when the publisher has never seen
   a success. `since` is an observed timestamp or a standing-record
   date, never a back-computed guess.
5. **Flags are plain language and evidence-bound.** The nightly
   flags fire on: a chain reduced to its last verified link; grades
   stale >48h for a sport; a fit older than the weekly cadence + 1
   day; a harvest silent >48h; a standing not-ok condition older than
   a day. Routine all-ok states produce no flags.

## Current truth the board reports (Nov 18, 2026)

- **API-Sports** — suspended (shared-IP cause, confirmed by their
  support). Collector pass `gated-off`; harvest gate default-deny.
  Pick cards fail over to The Odds API board; harvest games fail over
  to ESPN. Fix = the dedicated-IP Legion runner, not yet installed
  (laptop at the Lenovo depot).
- **The Odds API** — the board's price source; sweep gate skips
  below 50 credits remaining (the live board showed 490 on
  Nov 18, 2026).
- **BetStack** — `not-a-link` (see rule 2).
- **Player fan-out** — `not-in-production`: proof runs only
  (Nov 17, ESPN fallback, NBA 30/30 + NFL 32/32); stores live outside
  the data branch until the Legion runner lands.
- **Sim service** — `not-deployed`: Phase D built and proven locally
  (sportsbettinggpt `sim-service/`, commit `3c0167e`); Railway deploy
  is Isaac's separate word.
- **Prediction archive + grading ledger** — live on the data branch;
  the collector's archive step activates with the pending sports-feed
  deploy (Isaac's word), which also carries this board's collector
  half.
