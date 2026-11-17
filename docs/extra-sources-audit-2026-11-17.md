# Sports-data key audit — 2026-11-17

Ordered by Isaac: audit every sports-data key he holds and wire into the
collector only what fills a real gap. Method: backend code read
(SportsBettingGPT `services/betstack.py`, `config.py`), each provider's
own documentation/pricing pages, and live probes where a key value was
accessible without exposing it. **No key value was accessible for any
provider below** — every key lives only in the SportsBettingGPT
project's Railway env (set by Isaac 2026-11-07); there is no Railway
CLI session, no local `.env`, and no vault entry for them in the
agent's environment. All live probes are therefore BLOCKED, and each
verdict names the exact step that unblocks it.

## Coverage matrix

| Provider (key env) | Sports | Game markets | Player props | Fixtures / scores | Current season? | Plan limits |
|---|---|---|---|---|---|---|
| API-Sports (`APISPORTS_KEY`, per-sport overrides) | NBA, NFL, MLB, NHL (+ more) | h2h/spreads/totals per book | via game/player endpoints (harvest) | yes / yes | **No on free plan for NBA/MLB/NHL** ("try from 2022 to 2024"); NFL yes | Free: 100 req/day/sport |
| The Odds API (`ODDS_API_KEY`) | 4 collector sports + many | h2h/spreads/totals, 100+ books | paid tiers (event endpoint) | events only w/ odds / scores endpoint | Yes | Free: 500 credits/mo (shared with Sage) |
| Betting Data API (`BETTING_DATA_KEY`) | Free: NFL/NBA/MLB; Pro+: 20+ | h2h/spreads/totals, 50+ books | **Yes — `/props/`, Business tier only** (NBA: points/rebounds/assists/PRA/threes/blocks/steals/double-double; also NFL/MLB/NHL/WNBA) | events list / no scores product | Yes | Free $0: 25/day, h2h only · Pro $29/mo: 20k/mo · Business $99/mo: 200k/mo + props/edges/history |
| BetStack (`BETSTACK_API_KEY`) | NBA, NFL, MLB, NHL, NCAA, MLS, EPL, MMA… | consensus h2h/spread/total (single synthetic line, **not a book's price**) | **None** | **yes / yes (live + final)** | Yes | Free forever; 1 req / 60 s per key |
| BallDontLie (`BALLDONTLIE_API_KEY` — found in backend code; not named in the Nov 7 Railway set, existence there unconfirmed) | NBA, NFL, MLB, NHL, WNBA, EPL + more | GOAT tier only | **GOAT tier only** ($39.99/mo per league) | yes / yes (games endpoint, all tiers) | Yes | Free: teams/players/games, 5 req/min · ALL-STAR $9.99/league/mo: + stats, **injuries** · GOAT $39.99/league/mo: + odds, props, box scores, lineups |

Backend usage today (all three named keys): Sage's odds chain in
`services/betstack.py` — BetStack first, The Odds API second, Betting
Data API last — game markets only, h2h by config. BallDontLie is used
by one NBA-games helper whose transform mislabels scores as odds;
treat that helper as broken, not as coverage.

## Verdicts

### BetStack — WIRE-READY on `feat/collector-extra-sources` (this branch), key-gated
Fills gap (b): current-season fixtures + live/final scores for
Basketball/Baseball/Hockey, the three sports API-Sports' free plan
blanks. Built as `betstack.mjs` following the aggregates-layer pattern:
fills a sport's slate **only when the API-Sports pass produced none**,
provenance in every record id (`betstack-slate-…`), failures degrade to
a `betstackErrors` map, never throws, 1 request per filled sport per
run (limit is 1/60 s). Its consensus line is **never** published as a
board entry or pick — a consensus is not a bookmaker's price.
**Unblock (Isaac, ~1 minute):** copy `BETSTACK_API_KEY` from the
SportsBettingGPT Railway project's Variables into the sports-feed
Vercel project's env under the same name. The layer activates on the
next collector run; no code change needed. First live run is also the
real shape check — the test fixture is constructed from the provider's
published example + backend-observed spellings, honestly labeled as
such in `tests/betstack.test.mjs`.

Collector integration (apply when this branch merges, after the Odds
API change on main settles — `collector.mjs` was under concurrent edit
during the audit, so the hookup ships as this snippet, not a blind
edit). In `collectFeed()`, after the aggregates layer:

```js
let betstackErrors = {};
try {
  const emptySports = Object.keys(BETSTACK_SPORTS).filter(
    (s) => !slate.some((item) => item.sport === s)
  );
  const fill = await collectBetstackSlate({ sports: emptySports });
  betstackErrors = fill.errors;
  slate.push(...fill.slate);
  slate.sort((a, b) => {
    if (a.status !== b.status) return a.status === 'live' ? -1 : 1;
    return a.startTs - b.startTs;
  });
} catch (err) {
  betstackErrors = { BetStack: String(err?.message || err) };
}
// + `betstackErrors` on the feed object, and "BetStack (slate fill)"
//   appended to the feed `source` string when any fill item published.
```

### Betting Data API — SKIP the odds path; BLOCKED on tier confirmation for the one path that matters
Its `/odds/` duplicates The Odds API's markets book-for-book — and
should: a WIPO UDRP ruling (Case No. D2026-4323) found this provider's
predecessor domain was impersonating The Odds API and **reselling its
data without authorization**; the real Odds API advises past customers
to charge back and warns service can be interrupted. Wiring a reseller
clone of a feed we already get from the source adds a dependency and
zero data.
The one real prize is `/props/` — the only audited source of raw
player-prop prices (gap (a), the highest-value gap: game-view prop
legs are picks-only for money today for lack of exactly this). It is
**Business-tier only ($99/mo)**; Pro keys get a 403. Isaac's tier is
unverified: the one observed live response (2026-11-06, Celtics @
Hornets h2h at two books) is consistent with Free or Pro, not Business.
**Unblock (Isaac):** check the plan on the provider's dashboard (or
`/me/` with the key). If Business: set `BETTING_DATA_KEY` in the
sports-feed Vercel env and the props wiring is a small, well-defined
job (below). If Pro/Free: do not wire anything — and weigh whether a
provider under a UDRP reseller ruling is where a $99/mo upgrade
belongs, when The Odds API sells prop markets directly.

App-side note for a future BDA-props (or any prop-prices) wiring —
collector side only was in scope here: publish a `propPrices` feed
section keyed by board game id, each entry carrying bookmaker name,
player name, market key in the app taxonomy's vocabulary, line, and
Over/Under American prices with the snapshot's fetch time. The app
then (1) resolves player names against the permanent registry (exact →
normalized; never fuzzy-auto-substitute, per the validation rules),
(2) attaches prices to game-view prop legs so they lock odds like team
legs and mixed tickets settle for money, and (3) withholds EV until a
leg carries a price, exactly as team legs do today. That is a spec
§5 amendment + a 1.3.x app change; it was NOT built here (a separate
1.3.1 effort is in flight).

### BallDontLie — SKIP (for now)
Free-tier games would duplicate the BetStack slate fill. Its two
distinctive prizes are tier-gated and unverifiable without the key:
injuries at ALL-STAR ($9.99/league/mo — would feed the props
UNAVAILABLE checks and Sage's context, gap (d)) and prop prices at
GOAT ($39.99/league/mo — a legitimate, non-reseller alternative for
gap (a)). **Unblock (Isaac):** confirm whether `BALLDONTLIE_API_KEY`
exists in the Railway env at all, and its tier per league. If any
league is GOAT, re-audit this key first — it jumps the queue.

### API-Sports — no change
Already wired; free-plan season block for NBA/MLB/NHL confirmed
unchanged (it is the reason gaps (a)/(b) exist). The separate
agent-side API-Sports key from an old setup belongs to a suspended
account and was not touched.

### The Odds API — in flight, not duplicated
Being wired into the collector as the board source by a concurrent
change on main (daily sweep inside the shared 500 credits/month).
This audit treats it as covered and did not touch it.

## Test counts on this branch
- JS: `node --test tests/*.test.mjs` — 85 pass / 0 fail (52 committed
  baseline + 23 from the concurrent Odds WIP present in the tree +
  10 new `tests/betstack.test.mjs`).
- Python: `python3 -m unittest discover` in `intelligence/` — 81 OK.
- No live calls in any test; BetStack fetch is stubbed throughout.
