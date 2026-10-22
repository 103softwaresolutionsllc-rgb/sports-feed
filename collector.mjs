#!/usr/bin/env node
/**
 * 103 sports-feed collector.
 *
 * Polls API-Sports with OWNER-held keys (environment variables — never in
 * any app client) and writes a single public JSON feed that the BetTracker
 * app reads. App users never touch the provider API or its quota.
 *
 * Usage:
 *   APISPORTS_BASKETBALL_KEY=... APISPORTS_FOOTBALL_KEY=... \
 *   APISPORTS_BASEBALL_KEY=... APISPORTS_HOCKEY_KEY=... \
 *   node collector.mjs --out ./feed.json
 *
 * Keys are per-sport (one subscription each on api-sports.io). Sports
 * without a key are skipped and noted in the feed's `errors` as
 * "not configured" — never silently invented.
 *
 * Quota budget (free plan = 100 requests/day per sport):
 *   Per run per sport: 2 games calls (today + tomorrow) + up to 3 odds
 *   calls (only for games starting within ODDS_WINDOW_HOURS).
 *   At the scheduled cadence of one run every 90 minutes (16 runs/day)
 *   that is at most 80 requests/day per sport — inside the cap with
 *   headroom for manual runs. (Hourly would be 120/day and blow the cap.)
 *
 * News layer (news.mjs): the same run also collects headlines per league
 * from ESPN's unofficial news JSON (no key, no quota), with the league's
 * ESPN RSS feed as fallback, into the feed's top-level `news` object.
 * News failures are per-league `error` entries — they never block the
 * API-Sports sections, and empty leagues stay honestly empty.
 */

import { writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import {
  SPORT_APIS,
  normalizeGame,
  seasonForDate,
  toSlateItem,
  toPickCard,
  extractFavoriteLine,
} from './normalize.mjs';
import { collectNews } from './news.mjs';

const MAX_PICKS_PER_SPORT = 3;
const MAX_SLATE_PER_SPORT = 6;
const MAX_ODDS_LOOKUPS = 3;
const ODDS_WINDOW_HOURS = 24;

const KEY_ENV = {
  Basketball: 'APISPORTS_BASKETBALL_KEY',
  Football: 'APISPORTS_FOOTBALL_KEY',
  Baseball: 'APISPORTS_BASEBALL_KEY',
  Hockey: 'APISPORTS_HOCKEY_KEY',
};

async function apiGet(sport, key, path) {
  const cfg = SPORT_APIS[sport];
  const res = await fetch(`https://${cfg.host}${path}`, {
    headers: { 'x-apisports-key': key },
  });
  if (!res.ok) throw new Error(`API-Sports ${sport} HTTP ${res.status}`);
  const data = await res.json();
  const errors = data?.errors;
  if (
    errors &&
    (Array.isArray(errors) ? errors.length > 0 : Object.keys(errors).length > 0)
  ) {
    throw new Error(
      `API-Sports ${sport} error: ${JSON.stringify(errors).slice(0, 120)}`
    );
  }
  return Array.isArray(data?.response) ? data.response : [];
}

function localDate(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d;
}

function localDateStr(offsetDays = 0) {
  const d = localDate(offsetDays);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

async function buildSportFeed(sport, key) {
  const cfg = SPORT_APIS[sport];
  // Basketball/Baseball/Hockey reject /games without a `season` parameter
  // ({"season":"The Season field is required."} — live 2026-10-22). The
  // season is resolved per queried day by pure date math (seasonForDate),
  // so the request budget is unchanged: still exactly 2 /games calls.
  const days = await Promise.all(
    [0, 1].map((offset) => {
      const season = seasonForDate(sport, localDate(offset));
      const seasonParam = season ? `&season=${season}` : '';
      return apiGet(
        sport,
        key,
        `/games?league=${cfg.leagueId}&date=${localDateStr(offset)}${seasonParam}`
      );
    })
  );
  const games = days
    .flat()
    .map((raw) => normalizeGame(raw, sport))
    .filter(Boolean)
    .sort((a, b) => {
      if (a.status !== b.status) return a.status === 'live' ? -1 : 1;
      return a.startTs - b.startTs;
    });

  const slate = games.slice(0, MAX_SLATE_PER_SPORT).map(toSlateItem);

  const picks = [];
  const windowEnd = Date.now() + ODDS_WINDOW_HOURS * 60 * 60 * 1000;
  const candidates = games
    .filter((g) => g.status === 'upcoming' && g.startTs <= windowEnd)
    .slice(0, MAX_ODDS_LOOKUPS);
  for (const game of candidates) {
    if (picks.length >= MAX_PICKS_PER_SPORT) break;
    try {
      const odds = await apiGet(sport, key, `/odds?game=${game.id}`);
      const line = extractFavoriteLine(odds, game);
      if (line && line.impliedPct != null) picks.push(toPickCard(game, line));
    } catch {
      // Odds are best-effort per game; a failed lookup never blocks the slate.
    }
  }
  return { slate, picks };
}

/**
 * One full collection pass — the same work the CLI writes to disk and the
 * Vercel Function (api/collect.mjs) publishes to Vercel Blob. Returns the
 * feed object; never throws for per-sport or per-league failures (those
 * land in the feed's `errors` / per-league `error` entries).
 */
export async function collectFeed() {
  const picks = [];
  const slate = [];
  const errors = {};
  const sportsConfigured = [];

  for (const sport of Object.keys(SPORT_APIS)) {
    // API-Sports issues ONE account key used across all its sport APIs;
    // a per-sport env var overrides the shared APISPORTS_KEY when set.
    const key = process.env[KEY_ENV[sport]] || process.env.APISPORTS_KEY;
    if (!key) {
      errors[sport] = 'not configured (no key)';
      continue;
    }
    sportsConfigured.push(sport);
    try {
      const feed = await buildSportFeed(sport, key);
      picks.push(...feed.picks);
      slate.push(...feed.slate);
      console.log(
        `${sport}: ${feed.slate.length} slate, ${feed.picks.length} picks`
      );
    } catch (err) {
      errors[sport] = String(err?.message || err);
      console.error(`${sport}: FAILED — ${errors[sport]}`);
    }
  }

  picks.sort((a, b) => a.startTs - b.startTs);
  slate.sort((a, b) => {
    if (a.status !== b.status) return a.status === 'live' ? -1 : 1;
    return a.startTs - b.startTs;
  });

  // News layer runs in the same pass. collectNews() is fully guarded per
  // league; this catch is a last resort so a news-layer surprise can never
  // block publishing the API-Sports sections.
  let news;
  try {
    news = await collectNews();
  } catch (err) {
    news = {
      fetchedAt: new Date().toISOString(),
      error: `news layer failed: ${String(err?.message || err)}`,
    };
    console.error(news.error);
  }
  const feed = {
    generatedAt: new Date().toISOString(),
    source: '103 sports-feed collector — API-Sports (games/odds), ESPN (news)',
    sportsConfigured,
    picks,
    slate,
    news,
    errors,
  };
  return feed;
}

async function main() {
  const outIdx = process.argv.indexOf('--out');
  const outPath = outIdx >= 0 ? process.argv[outIdx + 1] : './feed.json';
  const feed = await collectFeed();
  const newsCount = Object.values(feed.news ?? {}).reduce(
    (n, entry) => n + (Array.isArray(entry?.articles) ? entry.articles.length : 0),
    0
  );
  await writeFile(outPath, JSON.stringify(feed, null, 2));
  console.log(
    `Wrote ${outPath}: ${feed.picks.length} picks, ${feed.slate.length} slate items, ${newsCount} news articles, sports: ${feed.sportsConfigured.join(', ') || 'none'}`
  );
}

// Run the CLI only when executed directly (`node collector.mjs`), not when
// imported by the Vercel Function.
const isMainModule =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMainModule) {
  main().catch((err) => {
    console.error('Collector failed:', err);
    process.exit(1);
  });
}
