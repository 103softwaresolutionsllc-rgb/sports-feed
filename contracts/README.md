# The Apparatus Contract Layer

Phase A of the BetTracker Quant Apparatus interconnection spec
(`~/workspace/your_files/bettracker-quant-apparatus-interconnection-spec-2026-11-18.md`, §4).
One versioned schema for each object that crosses a seam between the
apparatus's parts — collector, harvest, BetTracker app, Simulation Lab,
Sage/SportsBettingGPT — so a producer and a consumer can never quietly
disagree about what a field means.

Phase A is **foundation only**: schemas, a validator, and consumer test
wiring. No producer changes its wire format in this phase, and nothing
about production behavior changes.

## The envelope, and the two validation levels

Every contract's full form carries the spec's common envelope —
`contract`, `v`, `class` (Live / Historical / Estimated / SIMULATED),
`source`, `fetchedAt`, `id` — alongside its payload fields. Today's
native payloads do not all carry that envelope yet, so validation runs
at two honest levels:

1. **Envelope level** (`validate(name, obj)` — the default). The object
   must satisfy the schema's full `required`: envelope + payload. This
   is the target form for anything newly written to cross a seam.
2. **Payload level** (`validate(name, obj, { level: 'payload' })`).
   The object must satisfy the schema's `payloadRequired` fields with
   the contract's types and constraints, after the producer's native
   fields are renamed per the mapping tables below. Envelope fields are
   not required at this level (if present, they are still checked).

The mapping tables are the contract's other half: they state, field by
field, how each real producer's native shape becomes the contract
shape. A field that exists in reality but not in the contract — or the
reverse — is a recorded gap (end of this file), never a silent drop.

`validate.mjs` is dependency-free and supports the draft-07 subset the
schemas use (types incl. type arrays, required, properties, items,
enum, const, additionalProperties false-or-schema, minimum/maximum,
minItems, minLength). `format` is annotation-only. It never coerces:
failures return `{ ok: false, errors: [...] }` with named, path-qualified
errors.

## Field-mapping tables

### Game — `game`

Collector slate/board (`normalize.mjs`: normalizeGame / toSlateItem / extractBoardGame):

| Contract field | Native field | Notes |
|---|---|---|
| gameId | derived: `apisp:<sport>:<id>` | Native ids are per-surface (`apisp-slate-…`, `apisp-board-…`); the contract id is the game, not the surface |
| league | `sportLabel` | |
| homeTeam / awayTeam | `home` / `away` | Canonical names are the feed's spellings |
| homeCode / awayCode | `homeCode` / `awayCode` | |
| startAt | `startTs` | Epoch ms → ISO 8601 |
| status | `status` | Collector vocabulary is `upcoming` / `live`; `upcoming` maps to the contract's `scheduled`. Finished games are excluded from the slate by design |
| homeScore / awayScore | `homeScore` / `awayScore` | Present on live/final boards |

Intelligence harvest (API-Sports games payload, raw landing shape):

| Contract field | Native field | Notes |
|---|---|---|
| gameId | derived: `api-sports:<sport>:<id>` | ESPN-fallback payloads namespace as `espn:<sport>:<id>` |
| league | `league.name` | e.g. `NBA` |
| homeTeam / awayTeam | `teams.home.name` / `teams.away.name` | |
| startAt | `date` | Already ISO 8601 |
| status | `status.short` mapped | `NS` → scheduled; `FT`, `AOT`, `FT_PEN` → final; in-play codes → live |
| homeScore / awayScore | `scores.home.total` / `scores.away.total` | Null before tip-off |

Class for harvested games: **Historical**. Class for collector games: **Live**.

### LineSet — `lineset`

Collector (`normalize.mjs`: extractBoardLineSet), from one bookmaker's bets:

| Contract field | Native field | Notes |
|---|---|---|
| bookmaker | `bookmaker` | Exactly one book — a blend cannot be expressed |
| fetchedAt | `fetchedAt` | The odds snapshot's fetch time |
| moneyline | `moneyline` | `{ home, away }` American prices; null when the book carries none. Native prices are **strings** (`'-200'`, `'+150'`); the mapping converts them to the contract's integers — see gap G6 |
| spread | `spread` | `{ line, home, away }`; line signed from the home side |
| total | `total` | `{ line, over, under }`; null when not carried |
| gameId | — (wrapping board game) | Not embedded in the native set; the board game carries it |
| boardSnapshotRef | — (derived) | `<feed generatedAt>#<gameId>`; derivable today, embedded at envelope adoption |

### Pick — `pick`

App team leg (`mixedLegs.buildTeamLegSnapshot`):

| Contract field | Native field | Notes |
|---|---|---|
| id | `selection_id` | |
| gameId | `game_id` | The board game's id; provider namespacing happens at envelope adoption |
| league | `league` | |
| market | `market_type` mapped | `ml` → moneyline, `spread` → spread, `total` → total |
| subject | `picked_team`, `pick_side` | `{ kind: 'team', name: picked_team, team: picked_team }` |
| side | `pick_side` / `total_side` | `t1` → home, `t2` → away; totals use `total_side` (over/under) |
| line | `line` | Null for moneyline |
| price | `odds` | American odds from the tapped book |
| bookmaker | `bookmaker` | The one book whose line set served the pick (Rule 9) |
| model.probability | `probability` | Locked at selection (Lock-In law) |
| model.projection | `projected_margin` | Team legs project margin; prop legs project the stat |
| model.evPerUnit / evPercent | `expected_value.ev_per_unit` / `.ev_percent` | Null when the leg carries no usable odds |
| model.modelVersion | `model_version` | **Null in today's legs — see gap G1** |
| model.modelSource | `model_source` | `current` for team legs |
| model.lockedAt | `locked_at` | |

App prop leg (`propsTicketMath.buildLockedSnapshot`, via `mixedPropLegs.buildPropLegForMixed`):

| Contract field | Native field | Notes |
|---|---|---|
| id | `selection_id` | |
| gameId | `game_id` | |
| league | `league` | |
| market | constant `player_prop` | Native carries `market_id` / `market_name` instead |
| subject | `player_name`, `team_at_selection`, `player_id` | `{ kind: 'player', name, team, playerId }` |
| side | `direction` | over / under |
| stat | `statistic_key` | The taxonomy stat (e.g. `points`) |
| line | `line` | |
| price | `odds` | Usually absent — prop picks are picks-only for money; the field is then omitted, never invented |
| model.probability / projection | `probability` / `projection` | Locked snapshot values |
| model.evPerUnit / evPercent | `expected_value.*` | Withheld (null) without odds |
| model.modelSource | `model_source` | `player_props` |
| model.lockedAt | `locked_at` | |

Feed pick card (`normalize.mjs`: toPickCard) — **partial mapping**:

| Contract field | Native field | Notes |
|---|---|---|
| id | `id` | |
| league | `sportLabel` | |
| market | derived from `pick` text / `footer` | `ML` → moneyline; a spread figure → spread |
| subject | derived from `pick` text | `{ kind: 'team', name: <favorite team> }` |
| — | `winProb` | **Does not map.** It is the bookmaker-implied percentage, not a model probability — see gap G4 |

### ParlaySnapshot — `parlay-snapshot`

App parlay store (`mixedTicketService`; settlement by `mixedSettlement.settleMixedTicketMath`):

| Contract field | Native field | Notes |
|---|---|---|
| snapshotId / id | `ticket_id` | |
| status | `status` | open / settled |
| createdAt | `created_at` | |
| stake | `stake` | The one ticket-level stake |
| legs[] | `legs[]` | Each leg mapped per the Pick tables above |
| settlement.settledAt | `settlement.settled_at` | |
| settlement.result | `settlement.result` | win / loss / push |
| settlement.totalStaked | `settlement.total_staked` | |
| settlement.totalPayout | `settlement.total_payout` | Null when any pick is unpriced |
| settlement.profit | `settlement.profit` | Null when any pick is unpriced |
| settlement.moneyTracked | `settlement.money_tracked` | False for picks-only parlays |
| settlement.legs[].selectionId | `selection_id` | |
| settlement.legs[].outcome | `outcome` | win / loss / push / void / ungraded (early-loss close) |
| settlement.legs[].actual | `actual` | Prop legs: the player's actual stat; team legs: null |
| settlement.legs[].finalScore | `final_score` | Team legs: `{ home, away }`; prop legs: null |

### FitArtifact — `fit-artifact`

Simulation Lab fit files (`simulation-lab/data/<sport>-fit.json`, all four sports share the shape):

| Contract field | Native field | Notes |
|---|---|---|
| sport | `sport` | NBA / NFL / MLB / NHL |
| fitVersion | — assigned `0` | Native files are unversioned — see gap G2 |
| parameters.leagueAvg | `leagueAvgPoints` | Named "Points" for every sport, runs and goals included — see gap G3 |
| parameters.homeEdge | `homeEdgePoints` | Same note |
| parameters.shrink / totalSD / marginSD | `shrink` / `totalSD` / `marginSD` | |
| trainedOn.seasons | `seasons` | |
| trainedOn.gamesUsed / gamesExcluded | `gamesUsed` / `gamesExcluded` | |
| teams | `teams` | Values `{ homePF, homePA, awayPF, awayPA }` verbatim |
| playerStatSDs | `playerStatSDs` | |
| playerStatSDSource | `playerStatSDSource` | States fitted vs tagged-parameter provenance |
| producedBy | `provenance` | A prose string natively — see gap G2 |
| producedAt | `fittedAt` | |

### GradeRecord — `grade-record`

App unified record (`unifiedRecord.adaptSettledMixedTicket`) — leg entries:

| Contract field | Native field | Notes |
|---|---|---|
| id | `id` | `<ticket_id>:<selection_id>` |
| subjectKind | constant `pick` | |
| snapshotId | `ticketId` | |
| selectionId | `selectionId` | |
| result | `result` | win / loss / push (void/ungraded legs produce no entry) |
| modelProb | `modelProb` | The leg's recorded locked probability; null when none was recorded — never backfilled |
| league / sport | `league` / `sport` | |
| prediction | `prediction` | Plain-language pick label |
| settledAt | `settledAt` | |
| brierContribution | — | Computed by the ledger in Phase B; absent natively |

Ticket entries map with `subjectKind: 'parlay'`, `snapshotId ← id`,
`modelProb ← modelProb` (always null for a parlay — no invented ticket
probability), `stake ← stake`, `profit ← profit`.

### SourceHealth — `source-health` (Phase E)

There is no native payload to map: both publishers (the collector in
`health.mjs`, the nightly rollup in `grading/health-rollup.mjs`) write
the contract shape directly, at envelope level. Field notes:

| Contract field | Semantics |
|---|---|
| `status` | Spec §8's five (`ok` / `degraded` / `suspended` / `down` / `gated-off`) plus three Phase E extensions: `not-a-link` (cannot serve real data — BetStack), `not-deployed` (built, not running — the sim service), `not-observed` (this publisher cannot see it from where it stands) |
| `link` | The chain truth rule made a field: only `link: true` entries count toward a chain's real depth (Rule 10) |
| `observedBy` | `collector` (its own run), `data-branch` (files on the data branch), or `standing-record` (the apparatus build record — used only where no live observation exists, and labeled as such) |
| `since` | ISO timestamp when the publisher observed the condition begin; a bare `YYYY-MM-DD` date when it is a standing-record day. Never back-computed from guesses |
| `lastGoodAt` / `lastAttemptAt` | Null when the publisher made no attempt or has never seen a success — a gated-off pass reports `lastAttemptAt: null` because it truly made no call |

The board is read-only: no publisher ever changes a gate, a chain, or
a fit in response to what it records (docs/health-board.md).

## Known gaps (recorded, not hidden)

- **G1 — `model_version` is null on every app leg today.** Both leg
  builders record `model_version: null`. The contract keeps the field
  (nullable) so the gap is visible at the seam; stamping a real model
  version is producer work for a later phase.
- **G2 — Fit files carry no version and no script/commit provenance.**
  `fitVersion` maps to the assigned `0`, and `producedBy` maps from a
  prose `provenance` string. The Phase C fit pipeline mints real
  versions and records script + commit.
- **G3 — Fit parameter names say "Points" for runs and goals.**
  `leagueAvgPoints` / `homeEdgePoints` are the native names in all four
  sports' files. The contract uses neutral names; the native files keep
  theirs until the fit pipeline regenerates them.
- **G4 — Feed pick cards do not fully map to Pick.** A pick card's
  `winProb` is the bookmaker-implied percentage from the line, not a
  model probability, and mapping it into `model.probability` would
  silently re-meaning a field — exactly what the contract layer exists
  to prevent. Pick cards therefore validate only partially in v1; a
  future minor version may add an `impliedProb` field for them.
- **G6 — The collector publishes board prices as strings.** Its
  `decimalToAmerican` returns `'+150'` / `'-200'` strings, so every
  price in a published line set is a string today, while the
  contract's canonical price is an integer. The documented mapping
  converts at the seam; the validator never coerces. Making the
  collector emit integers natively is producer work for a later phase.
- **G5 — `boardSnapshotRef` and game-id namespacing are derived, not
  embedded.** Native line sets and app legs reference their board
  snapshot by fetch time, not by an edition ref, and app `game_id`s are
  board ids rather than provider-namespaced ids. Both become embedded
  when producers adopt the envelope (post-Phase A); until then the
  mapping derives them and says so.

## Versioning rule

From spec §4: additive changes are minor; removing or re-meaning a
field is major and requires every consumer's test suite to pass against
the new major before any producer ships it. Producers write one
version; consumers declare the versions they accept. A payload that
fails validation fails loudly at the seam — in tests now, on the health
board in Phase E — never as a zero on a user's screen.
