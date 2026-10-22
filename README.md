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

News honesty rules: a league whose ESPN JSON *and* RSS fetches both fail
(or return nothing usable) gets `"articles": []` and a plain-language
`error` saying so — stories are never invented. Story `id` is a hash of
the article URL (dedupe key), descriptions are truncated to ~200 chars,
lists are newest-first and capped at 10 per league. `via` records whether
the JSON endpoint or the RSS fallback served the league. Team tags come
only from ESPN's own article categories; RSS stories carry none.

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
