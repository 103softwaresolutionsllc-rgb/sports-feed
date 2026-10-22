# 103 Sports Feed Collector

Server-side collector that turns API-Sports data into the single public JSON
feed the 103 Sports BetTracker app reads on its Home screen ("Today's Notable
Lines" + "Live & upcoming" slate).

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
  `normalize.mjs`), and writes `feed.json`.
- `sports-feed.workflow.yml` — GitHub Actions template that runs the pass
  every 90 minutes and commits the refreshed `feed.json`. Setup steps are in
  the file header. A cron on any always-on machine works too:
  `node collector.mjs --out /path/to/webroot/feed.json`.

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
  "source": "API-Sports via 103 sports-feed collector",
  "sportsConfigured": ["Basketball", "Football"],
  "picks":  [ /* Notable Lines cards: favorite, implied %, bookmaker */ ],
  "slate":  [ /* live + upcoming games */ ],
  "errors": { "Hockey": "not configured (no key)" }
}
```

Honesty rules baked in: picks show the bookmaker's favorite and the
probability implied by the odds (never a "model probability"); sports
without keys or with failed fetches appear in `errors` and contribute no
items — the app shows empty states rather than placeholder data.

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
