# 103 Sports Feed Collector

Server-side collector that turns API-Sports data into the single public JSON
feed the 103 Sports BetTracker app reads on its Home screen ("Today's Notable
Lines" + "Live & upcoming" slate), plus a news layer (ESPN headlines) the
apps surface on their AI Hub / insights screens.

## Why it exists

Isaac's rule (2026-10-22): app users must never be able to use or make
requests against his API accounts. So the API-Sports keys live ONLY here, on
the server side. This collector polls the provider on a schedule inside the
free quota and publishes `feed.json`; the app only GETs that file. One
collector run serves every app user — provider quota is spent once, around
the clock, regardless of user count.

## How it runs

- `collector.mjs` — one collection pass. Reads keys from the environment,
  fetches today's + tomorrow's games per configured sport, pulls odds for up
  to 3 near-term games per sport, normalizes everything (see
  `normalize.mjs`), collects the news layer (see `news.mjs`), and writes
  `feed.json`.
- `news.mjs` — ESPN news collection, one fetcher per league. Primary:
  ESPN's unofficial news JSON
  (`site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news` — no key,
  no quota). Fallback: the league's ESPN RSS feed
  (`www.espn.com/espn/rss/{league}/news`), parsed to the same story shape
  by the dependency-free parser in `normalize.mjs`. Per spec, only
  headline + ~200-char description + outbound link are published, with
  source attribution — never full article text.
- `tests/news.test.mjs` — unit tests for the news normalizers, run against
  real ESPN responses recorded as fixtures (`node --test tests/news.test.mjs`).
- `tests/board.test.mjs` — unit tests for the board extraction, run against
  a documented-envelope API-Sports /odds fixture
  (`tests/fixtures/apisports-nba-odds.json`), including missing-market and
  malformed-bookmaker cases.
- `aggregates.mjs` — republishes the Intelligence Engine aggregates the
  daily harvest already produced into the feed's `aggregates` section
  (see below). Zero provider requests.
- `tests/aggregates.test.mjs` — unit tests for the aggregates publishing,
  run against an excerpt of the real published basketball aggregates
  document (`tests/fixtures/intelligence-aggregates-basketball.json`),
  with all transport injected (no live calls).
- `sports-feed.workflow.yml` — GitHub Actions template that runs the pass
  every 90 minutes and commits the refreshed `feed.json`. Setup steps are in
  the file header. A cron on any always-on machine works too:
  `node collector.mjs --out /path/to/webroot/feed.json`.

## Vercel deployment

The same collector also runs on Vercel (project `sports-feed`, team
"Isaac E Jones III's projects"), deployed from this repo:

- `api/collect.mjs` — Vercel Function that runs `collectFeed()` (the exact
  pass the CLI runs) and publishes the result to Vercel Blob at the stable
  pathname `feed.json` (`addRandomSuffix: false`, public, JSON).
- `vercel.json` — two cron entries (`0 0,3,6,9,12,15,18,21 * * *` and
  `30 1,4,7,10,13,16,19,22 * * *`) interleave to the same 90-minute cadence
  as the GitHub schedule. Vercel Cron calls `/api/collect` with the
  `CRON_SECRET` bearer header automatically; the function requires it
  whenever `CRON_SECRET` is set.
- Keys live only as project environment variables on Vercel
  (`APISPORTS_KEY` / per-sport overrides, `CRON_SECRET`,
  `BLOB_READ_WRITE_TOKEN` from the connected Blob store) — never in the
  repo, never in the app.
- Feed URL (Vercel Blob, stable): _pending first verified publish — see
  git history / project dashboard._
- The GitHub Pages feed
  (https://103softwaresolutionsllc-rgb.github.io/sports-feed/feed.json)
  remains live as a fallback.

## Feed history (forward accumulation)

`feed.json` is still overwritten every run — that stable URL is the app's
contract. But the Intelligence Engine needs the history that overwriting
used to destroy, so the Vercel Function (`api/collect.mjs`, via
`history.mjs`) now ALSO persists every run's payload, additively, in the
same Blob store:

- `history/feed-<ISO timestamp>.json` — one snapshot per run,
  byte-identical to that run's `feed.json` payload (the exact serialized
  string is reused for both writes).
- `history/index.json` — rolling index
  `{ updatedAt, snapshots: [{ timestamp, path, url }] }`, newest first,
  capped at the most recent 500 entries (~31 days at the 90-minute
  cadence). The cap trims the index listing only; older snapshot blobs
  are not deleted.

**Never-break-the-feed guarantee:** history persistence runs only AFTER
the `feed.json` write has succeeded, and it is fully guarded — any failure
(network, Blob quota, serialization, index read/write) is logged and
swallowed. It never throws, and it cannot alter the feed's content, path,
or write order. If history ever breaks, the public feed keeps publishing
exactly as before; the failure shows up only in the function logs
(`history: …` lines). The CLI path (`collector.mjs --out`) writes to a
local file as before and does not write history.

## Quota budget (API-Sports free plan: 100 requests/day per sport)

Per run per sport: 2 games calls + ≤3 odds calls = ≤5. At the 90-minute
cadence (16 runs/day) that's ≤80/day per sport — inside the cap with
headroom. Odds are only fetched for games starting within 24 hours.
(Hourly on-the-hour was considered and rejected: 24 runs × 5 = 120/day
would blow the cap.)

## Feed shape

```json
{
  "generatedAt": "ISO timestamp",
  "source": "103 sports-feed collector — API-Sports (games/odds), ESPN (news)",
  "sportsConfigured": ["Basketball", "Football"],
  "picks":  [ /* Notable Lines cards: favorite, implied %, bookmaker */ ],
  "slate":  [ /* live + upcoming games */ ],
  "board":  [ /* sportsbook board (Predict remake spec §4.2): per covered
                 game { id, gameId, sport, league, home, away, homeCode,
                 awayCode, startTs, startAt, lineSets: [ { bookmaker,
                 fetchedAt, moneyline: { home, away } | null,
                 spread: { line, home, away } | null,
                 total: { line, over, under } | null } ] } */ ],
  "aggregates": { /* Intelligence Engine aggregates per display sport,
                 e.g. "Basketball": { summary: { sport, classification,
                 seasons_held, season_count, label, label_detail, teams },
                 weighted_averages: { team: { metric: value } },
                 data_as_of?: ISO } — see Aggregates section below.
                 Football reads the Engine's american-football doc. */ },
  "aggregatesErrors": { /* display sport -> message, only for sports
                 whose aggregates document could not be loaded */ },
  "news": {
    "fetchedAt": "ISO timestamp",
    "Basketball": {
      "league": "NBA",
      "via": "espn-json",
      "articles": [ /* { id, source, sport, league, headline, description,
                       published/publishedAt, url/link, imageUrl?, teamTags } */ ],
      "error": null
    }
    /* Football (NFL), Baseball (MLB), Hockey (NHL) — same shape */
  },
  "errors": { "Hockey": "not configured (no key)" }
}
```

Honesty rules baked in: picks show the bookmaker's favorite and the
probability implied by the odds (never a "model probability"); sports
without keys or with failed fetches appear in `errors` and contribute no
items — the app shows empty states rather than placeholder data.

Board rules (spec §§4.2/4.4/8): the board is published from the odds
responses the collector already fetches for the pick cards — zero extra
provider requests, so coverage is the same ≤3 near-term games per sport
per run. Prices are the provider's decimals converted to American format
at publish time. The spread `line` is signed from the home team's
perspective (negative = home favored). A market publishes only when both
sides are present at the same line (first complete pair wins when a
bookmaker lists alternate lines); a market the provider does not carry
is `null`, a malformed bookmaker is dropped whole, and nothing is ever
repaired into an invented price. Every line set carries its bookmaker's
name and the snapshot's `fetchedAt` — no as-of time, no publication.

News honesty rules: a league whose ESPN JSON *and* RSS fetches both fail
(or return nothing usable) gets `"articles": []` and a plain-language
`error` saying so — stories are never invented. Story `id` is a hash of
the article URL (dedupe key), descriptions are truncated to ~200 chars,
lists are newest-first and capped at 10 per league. `via` records whether
the JSON endpoint or the RSS fallback served the league. Team tags come
only from ESPN's own article categories; RSS stories carry none.

## Aggregates (Intelligence Engine → board model overlay)

Where the aggregates live: the daily harvest job
(`.github/workflows/intelligence-harvest.yml`) runs
`intelligence/intelligence_aggregates.py` per sport and commits the
output to this repo's `data` branch at
`data/intelligence/aggregates/<sport>.json` (Engine file sports:
`basketball`, `american-football`, `baseball`, `hockey`, `soccer`).
SportsBettingGPT's router reads them from that branch over HTTPS via
`INTELLIGENCE_AGGREGATES_BASE_URL` (see `intelligence/SYNC.md`).

How the feed build reaches them: `aggregates.mjs` (`collectAggregates`,
run inside `collectFeed`) reads the SAME already-produced documents —
zero extra provider requests, no new harvest, no API-Sports quota spent.
Source resolution mirrors the router's: a local `INTELLIGENCE_DATA_DIR`
(directory laid out `<dir>/aggregates/<sport>.json`) wins per sport;
otherwise the documents are fetched from `INTELLIGENCE_AGGREGATES_BASE_URL`
when set, defaulting to the data branch's published aggregates directory
on raw.githubusercontent.com (the repo is public). Soccer is not
published: the collector has no Soccer board sport for the app to key by.

Section shape: `aggregates` is keyed by display sport name
(`Basketball` / `Football` / `Baseball` / `Hockey`) — the keying the
app's `boardService.aggregatesOf` passes through — and each value is the
per-sport document in the shape `boardEngineInputs` consumes:
`{ summary, weighted_averages }`, plus `data_as_of` when a source
timestamp exists. Only those two sections are republished; the Engine's
`averages` / `leaderboards` / `player_leaderboards` are not (the overlay
does not consume them).

Provenance: `summary` is carried VERBATIM from the Engine document —
`classification` ("Historical"), `seasons_held`, `season_count`, `label`,
`label_detail`, `teams` — because the app labels model inputs Historical
with the seasons held. `data_as_of` follows the router's as-of rule (the
document's `generated_at`, else the source's Last-Modified / file write
time) and is omitted when no source provides one — never invented (the
raw file host sends no Last-Modified, so remote documents normally carry
no `data_as_of`; their provenance is the summary plus the feed's own
`generatedAt`).

Aggregates honesty rules: a sport whose document cannot be loaded or
fails validation is omitted from `aggregates` and named in
`aggregatesErrors` — deliberately NOT in the feed's `errors`, whose
per-sport entries mean "this sport's collector run failed" and would
stale that sport's board prices in the app. The app's no-stats gate then
withholds that sport's model reads while prices still render. A valid
document with empty `weighted_averages` (seasons held, no team averages
yet — the current hockey state) publishes as-is. The aggregates layer
never throws and never blocks the feed.

## App side

BetTracker reads the feed via `EXPO_PUBLIC_HOME_FEED_URL`
(`src/services/apiSportsService.js` is a validating reader with a 30-minute
local cache). No provider keys exist in the app.

## Not yet wired

- MMA: the MMA API returns fights in a different shape; verify against a
  live key before adding a normalizer.
- First live run will confirm the per-sport payload shapes (the normalizers
  are unit-tested against documented-shape fixtures, and written
  defensively: unknown shapes yield no items, never garbage).
