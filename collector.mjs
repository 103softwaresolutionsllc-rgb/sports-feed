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
 *   ODDS_API_KEY=... \
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
 * Board layer (Predict remake spec §4.2): the same odds responses also
 * publish a `board` section — per covered game, per-bookmaker moneyline /
 * spread / total line sets in American format, each carrying the
 * bookmaker's name and the snapshot's fetch time. This is a publishing
 * change on responses already fetched: ZERO extra provider requests, and
 * board coverage is exactly the odds candidates above (≤3 near-term
 * games per sport per run).
 *
 * Odds API layer (oddsapi.mjs): the board's odds source since 2026-11-17
 * (Isaac's decision; design-doc amendment). API-Sports' free plan
 * refuses the current season for Basketball/Baseball/Hockey, which left
 * the published board empty, so the board's prices now come from The
 * Odds API free tier (key in ODDS_API_KEY — the same key the
 * SportsBettingGPT backend's Sage get_live_odds uses; the 500
 * credits/month are SHARED). Sweeps are gated PER SPORT: each sport
 * fetches at most every ~22 hours (4 team sports × 3 credits + MMA
 * h2h-only × 1 credit = 13 when every sport is due), gated by the
 * prior published feed's boardMeta (oddsApiSportsFetchedAt) and the
 * credit headers; between a sport's sweeps its entries carry over
 * verbatim. MMA (sport "MMA", league "UFC") is an Odds-only board
 * sport — API-Sports MMA is not wired — and publishes moneyline-only
 * line sets. API-Sports board entries remain only as a fallback for
 * games The Odds API does not carry, and a sport The Odds API covers
 * (entries this run, or a sweep that succeeded this run — even a
 * zero-game one) no longer reports its API-Sports failure in `errors`
 * (an errors entry would stale that sport's board prices in the app) —
 * the API-Sports message moves to `apiSportsErrors` instead.
 *
 * News layer (news.mjs): the same run also collects headlines per league
 * from ESPN's unofficial news JSON (no key, no quota), with the league's
 * ESPN RSS feed as fallback, into the feed's top-level `news` object.
 * News failures are per-league `error` entries — they never block the
 * API-Sports sections, and empty leagues stay honestly empty.
 *
 * Aggregates layer (aggregates.mjs): the same run also republishes the
 * Intelligence Engine aggregates the daily harvest already produced
 * (data branch, data/intelligence/aggregates/<sport>.json) into the
 * feed's top-level `aggregates` section — { summary, weighted_averages }
 * per display sport, summary carried verbatim — so the BetTracker board's
 * model overlay (boardEngineInputs) can run on live games. ZERO extra
 * provider requests: the documents are read over HTTPS from the data
 * branch's published directory (or a local INTELLIGENCE_DATA_DIR), never
 * from API-Sports. Failures land in the feed's `aggregatesErrors`, never
 * in `errors` (a per-sport `errors` entry means that sport's collector
 * run failed and would stale its board prices), and never block the feed.
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
  extractBoardGame,
} from './normalize.mjs';
import { collectNews } from './news.mjs';
import { collectAggregates } from './aggregates.mjs';
import { collectBetstackSlate, BETSTACK_SPORTS } from './betstack.mjs';
import {
  ODDS_API_SPORT_KEYS,
  ODDS_API_SPORT_MARKETS,
  LOW_CREDIT_FLOOR,
  oddsApiPayloadToBoard,
  boardEntryToSlateItem,
  sportsFetchedAtFromMeta,
  sweepPlan,
  carryOverEntries,
  mergeBoard,
  mergeSlate,
} from './oddsapi.mjs';

/**
 * The prior published feed — read at collect start so the Odds API
 * layer can apply its sweep gate and carry the last sweep's board over
 * between sweeps. The Vercel Blob pathname is stable (addRandomSuffix:
 * false in api/collect.mjs); FEED_PRIOR_URL overrides it (local runs,
 * tests of the deployment wiring). Any failure → null → the layer
 * behaves as a first run.
 */
const PRIOR_FEED_URL =
  process.env.FEED_PRIOR_URL ||
  'https://lmiqgg08ux9djfii.public.blob.vercel-storage.com/feed.json';

async function readPriorFeed() {
  try {
    const res = await fetch(PRIOR_FEED_URL, {
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data && typeof data === 'object' ? data : null;
  } catch {
    return null;
  }
}

async function oddsApiGet(sportKey, key, markets) {
  const url =
    `https://api.the-odds-api.com/v4/sports/${sportKey}/odds/` +
    `?apiKey=${encodeURIComponent(key)}&regions=us` +
    `&markets=${markets.join(',')}&oddsFormat=american`;
  const res = await fetch(url, { signal: AbortSignal.timeout(20000) });
  const remaining = Number(res.headers.get('x-requests-remaining'));
  const used = Number(res.headers.get('x-requests-used'));
  if (!res.ok) throw new Error(`The Odds API ${sportKey} HTTP ${res.status}`);
  const data = await res.json();
  return {
    games: Array.isArray(data) ? data : [],
    creditsRemaining: Number.isFinite(remaining) ? remaining : null,
    creditsUsed: Number.isFinite(used) ? used : null,
  };
}

/** { oddsSportKey: epochMs } → { oddsSportKey: ISO string }, finite only. */
function sportsFetchedAtIso(byKeyMs) {
  const iso = {};
  for (const [sportKey, ms] of Object.entries(byKeyMs ?? {})) {
    if (Number.isFinite(ms)) iso[sportKey] = new Date(ms).toISOString();
  }
  return iso;
}

/** The most recent sweep time in a per-sport map, as ISO, or null. */
function latestSweepIso(byKeyMs) {
  const times = Object.values(byKeyMs ?? {}).filter(Number.isFinite);
  return times.length ? new Date(Math.max(...times)).toISOString() : null;
}

/**
 * The Odds API board layer for one run. The sweep gate is PER SPORT
 * (oddsapi.mjs's sweepPlan): a sport is fetched when its own last
 * successful sweep is at least ~22h old — or it has never swept, so a
 * newly added sport (MMA, 2026-11-17) fetches on the first run after
 * it ships instead of waiting for the other sports' gate. Sports not
 * due carry their prior published entries over verbatim; a due sport
 * whose fetch fails carries over too and stays due next run. The
 * low-credit floor stays global: under it, nothing sweeps.
 * boardMeta records the per-sport sweep times
 * (oddsApiSportsFetchedAt), the most recent sweep overall
 * (oddsApiFetchedAt), and the credit headers. Never throws: a layer
 * failure degrades to whatever carry-over exists (possibly none) plus
 * a boardMeta status, never a blocked feed.
 */
async function buildOddsLayer(prior, nowMs) {
  const priorMeta = prior?.boardMeta ?? null;
  const carriedAll = carryOverEntries(prior?.board, priorMeta, nowMs);
  const carriedSlateAll = carriedAll
    .map((entry) => boardEntryToSlateItem(entry, nowMs))
    .filter(Boolean);
  const carriedBySport = new Map(
    Object.keys(ODDS_API_SPORT_KEYS).map((sport) => [
      sport,
      { entries: [], slateItems: [] },
    ])
  );
  for (const entry of carriedAll) {
    carriedBySport.get(entry.sport)?.entries.push(entry);
  }
  for (const item of carriedSlateAll) {
    carriedBySport.get(item.sport)?.slateItems.push(item);
  }
  const lastFetchMs = sportsFetchedAtFromMeta(priorMeta);
  const baseMeta = {
    source: 'the-odds-api',
    oddsApiFetchedAt: latestSweepIso(lastFetchMs),
    oddsApiSportsFetchedAt: sportsFetchedAtIso(lastFetchMs),
    creditsRemaining: priorMeta?.creditsRemaining ?? null,
    creditsUsed: priorMeta?.creditsUsed ?? null,
    oddsApiGameIds: carriedAll.map((e) => e.id),
  };
  const key = process.env.ODDS_API_KEY;
  if (!key) {
    return {
      entries: carriedAll,
      slateItems: carriedSlateAll,
      meta: { ...baseMeta, status: 'not-configured' },
      note: null,
      sweptSports: [],
    };
  }
  const plan = sweepPlan({
    nowMs,
    priorMeta,
    creditsRemaining: priorMeta?.creditsRemaining ?? null,
  });
  if (plan.lowCredits) {
    return {
      entries: carriedAll,
      slateItems: carriedSlateAll,
      meta: { ...baseMeta, status: 'skipped-low-credits' },
      note: `sweep skipped: ${priorMeta?.creditsRemaining} credits remaining (floor ${LOW_CREDIT_FLOOR})`,
      sweptSports: [],
    };
  }
  const fetchedAt = new Date(nowMs).toISOString();
  const entries = [];
  const slateItems = [];
  const sportErrors = {};
  const sweptSports = [];
  const fetchMsByKey = { ...plan.lastFetchMs };
  let creditsRemaining = priorMeta?.creditsRemaining ?? null;
  let creditsUsed = priorMeta?.creditsUsed ?? null;
  for (const [sport, sportKey] of Object.entries(ODDS_API_SPORT_KEYS)) {
    const carried = carriedBySport.get(sport) ?? { entries: [], slateItems: [] };
    if (!plan.due.includes(sport)) {
      entries.push(...carried.entries);
      slateItems.push(...carried.slateItems);
      continue;
    }
    try {
      const { games, creditsRemaining: rem, creditsUsed: used } =
        await oddsApiGet(sportKey, key, ODDS_API_SPORT_MARKETS[sport]);
      if (rem != null) creditsRemaining = rem;
      if (used != null) creditsUsed = used;
      const mapped = oddsApiPayloadToBoard(games, sport, fetchedAt);
      entries.push(...mapped.entries);
      slateItems.push(...mapped.slateItems);
      fetchMsByKey[sportKey] = nowMs;
      sweptSports.push(sport);
      console.log(
        `Odds API ${sport}: ${games.length} games, ${mapped.entries.length} board entries`
      );
    } catch (err) {
      sportErrors[sport] = String(err?.message || err);
      entries.push(...carried.entries);
      slateItems.push(...carried.slateItems);
      console.error(`Odds API ${sport}: FAILED — ${sportErrors[sport]}`);
    }
  }
  const failedDue = plan.due.filter((sport) => sportErrors[sport]);
  let status;
  if (plan.due.length === 0) status = 'carried';
  else if (failedDue.length === plan.due.length) status = 'sweep-failed-carried';
  else if (failedDue.length > 0) status = 'partial';
  else status = 'ok';
  return {
    entries,
    slateItems,
    meta: {
      source: 'the-odds-api',
      oddsApiFetchedAt: latestSweepIso(fetchMsByKey),
      oddsApiSportsFetchedAt: sportsFetchedAtIso(fetchMsByKey),
      creditsRemaining,
      creditsUsed,
      status,
      ...(Object.keys(sportErrors).length ? { sportErrors } : {}),
      oddsApiGameIds: entries.map((e) => e.id),
    },
    note:
      status === 'sweep-failed-carried'
        ? 'sweep failed for every due sport; prior board carried over'
        : null,
    sweptSports,
  };
}

/**
 * The sports whose API-Sports failure must NOT stay in the feed's
 * `errors`: every sport with Odds API board entries this run, PLUS
 * every sport whose Odds sweep SUCCEEDED this run — even one that
 * came back with zero games (a successful empty MLB sweep in the
 * offseason is coverage, not an error state; the sport's dead
 * API-Sports season message must not gate its board in the app).
 */
export function oddsCoveredSports(oddsLayer) {
  return new Set([
    ...(oddsLayer?.entries ?? []).map((e) => e.sport),
    ...(oddsLayer?.sweptSports ?? []),
  ]);
}

/**
 * Move the covered sports' messages out of `errors` (mutated) into a
 * fresh apiSportsErrors map — the same separation aggregatesErrors
 * already uses. Returns the apiSportsErrors map.
 */
export function extractApiSportsErrors(errors, coveredSports) {
  const apiSportsErrors = {};
  for (const sport of coveredSports ?? []) {
    if (errors?.[sport]) {
      apiSportsErrors[sport] = errors[sport];
      delete errors[sport];
    }
  }
  return apiSportsErrors;
}

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
  const board = [];
  const windowEnd = Date.now() + ODDS_WINDOW_HOURS * 60 * 60 * 1000;
  const candidates = games
    .filter((g) => g.status === 'upcoming' && g.startTs <= windowEnd)
    .slice(0, MAX_ODDS_LOOKUPS);
  // Every candidate's odds response feeds BOTH the pick card and the
  // board entry, so the loop never stops early: candidates are already
  // capped at MAX_ODDS_LOOKUPS, and the request count is unchanged.
  for (const game of candidates) {
    try {
      const odds = await apiGet(sport, key, `/odds?game=${game.id}`);
      // Board publishing (spec §4.2) reuses THIS response — the full odds
      // payload already arrived for the pick card; publishing the board
      // costs zero extra provider requests. fetchedAt is the moment this
      // snapshot arrived; every line set in it carries that as-of time.
      const fetchedAt = new Date().toISOString();
      const line = extractFavoriteLine(odds, game);
      if (line && line.impliedPct != null && picks.length < MAX_PICKS_PER_SPORT) {
        picks.push(toPickCard(game, line));
      }
      const boardGame = extractBoardGame(odds, game, fetchedAt);
      if (boardGame) board.push(boardGame);
    } catch {
      // Odds are best-effort per game; a failed lookup never blocks the slate.
    }
  }
  return { slate, picks, board };
}

/**
 * One full collection pass — the same work the CLI writes to disk and the
 * Vercel Function (api/collect.mjs) publishes to Vercel Blob. Returns the
 * feed object; never throws for per-sport or per-league failures (those
 * land in the feed's `errors` / per-league `error` entries).
 */
/**
 * The BetStack slate fill is gated OFF by default: it runs only when
 * BETSTACK_FILL_ENABLED is exactly "true" (unset in the Vercel project,
 * so the layer is dark). A live probe on 2026-11-17 showed the real
 * BetStack payload does not match betstack.mjs's expectations — the
 * league parameter does not filter (every league query returns one
 * identical mixed-league slate) and no scores appear anywhere — so as
 * written the fill would publish other leagues' fixtures mislabeled
 * under the requested sport. Do not enable until betstack.mjs is
 * reworked against the real payload shape.
 */
export function betstackFillEnabled(env = process.env) {
  return env.BETSTACK_FILL_ENABLED === 'true';
}

/**
 * The API-Sports pass is gated OFF by default: it runs only when
 * APISPORTS_COLLECTOR_ENABLED is exactly "true" (unset in the
 * sports-feed Vercel project, so the pass stays dark). Gated on
 * 2026-11-17 after API-Sports suspended the account: their support
 * confirmed the cause was requests arriving from shared cloud egress
 * IPs — Vercel's pool is exactly that — and the fix is a dedicated
 * outbound IP, which a Vercel Function cannot provide. API-Sports
 * data now enters the system only through the Intelligence Engine
 * harvest running on Isaac's own infrastructure (the Legion runner,
 * INTELLIGENCE_ALLOW_API=1). The board never depended on this pass:
 * The Odds API layer below is the board's odds source and covers the
 * slate; this pass only ever supplied fallback fill and pick cards.
 * Do not enable in any shared-IP environment — a single call from a
 * shared pool can re-trigger the suspension.
 */
export function apiSportsCollectorEnabled(env = process.env) {
  return env.APISPORTS_COLLECTOR_ENABLED === 'true';
}

export async function collectFeed() {
  const picks = [];
  let slate = [];
  let board = [];
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
    if (!apiSportsCollectorEnabled()) {
      // Policy gate (see apiSportsCollectorEnabled): no API-Sports
      // calls from Vercel's shared egress IPs — not even a failing
      // one. The sport simply contributes nothing this pass; the
      // Odds API layer covers board and slate.
      continue;
    }
    sportsConfigured.push(sport);
    try {
      const feed = await buildSportFeed(sport, key);
      picks.push(...feed.picks);
      slate.push(...feed.slate);
      board.push(...feed.board);
      console.log(
        `${sport}: ${feed.slate.length} slate, ${feed.picks.length} picks, ${feed.board.length} board`
      );
    } catch (err) {
      errors[sport] = String(err?.message || err);
      console.error(`${sport}: FAILED — ${errors[sport]}`);
    }
  }

  // Odds API layer: the board's odds source (see the header note). The
  // prior published feed gates the sweep and supplies carry-over. The
  // layer never throws; this catch is a last resort so an Odds API
  // surprise degrades the board to the API-Sports fallback alone.
  let oddsLayer;
  try {
    const prior = await readPriorFeed();
    oddsLayer = await buildOddsLayer(prior, Date.now());
  } catch (err) {
    oddsLayer = {
      entries: [],
      slateItems: [],
      meta: {
        source: 'the-odds-api',
        oddsApiFetchedAt: null,
        oddsApiSportsFetchedAt: {},
        creditsRemaining: null,
        creditsUsed: null,
        status: 'layer-failed',
        oddsApiGameIds: [],
      },
      note: `Odds API layer failed: ${String(err?.message || err)}`,
      sweptSports: [],
    };
    console.error(oddsLayer.note);
  }
  board = mergeBoard(oddsLayer.entries, board);
  slate = mergeSlate(oddsLayer.slateItems, slate);
  if (oddsLayer.note) errors.OddsApi = oddsLayer.note;
  // A sport The Odds API covers (fresh sweep, carry-over, or a sweep
  // that succeeded this run — even one that returned zero games, like
  // MLB in the offseason) did not fail its board run, even when its
  // API-Sports attempt did — leaving the API-Sports message in
  // `errors` would stale that sport's board prices in the app. The
  // message moves to `apiSportsErrors`, the same separation
  // aggregatesErrors already uses.
  const apiSportsErrors = extractApiSportsErrors(
    errors,
    oddsCoveredSports(oddsLayer)
  );

  picks.sort((a, b) => a.startTs - b.startTs);
  board.sort((a, b) => a.startTs - b.startTs);
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
  // Aggregates layer runs in the same pass. collectAggregates() is fully
  // guarded per sport and never throws; this catch is a last resort so an
  // aggregates-layer surprise can never block publishing the feed — the
  // section degrades to {} and the app's no-stats gate withholds model
  // reads, exactly as it did before this section existed.
  let aggregates = {};
  let aggregatesErrors = {};
  try {
    ({ aggregates, errors: aggregatesErrors } = await collectAggregates());
    for (const [sport, message] of Object.entries(aggregatesErrors)) {
      console.error(`aggregates ${sport}: FAILED — ${message}`);
    }
  } catch (err) {
    aggregatesErrors = { Aggregates: String(err?.message || err) };
    console.error(`aggregates layer failed: ${aggregatesErrors.Aggregates}`);
  }
  // BetStack layer (audit 2026-11-17, docs/extra-sources-audit-2026-11-17.md)
  // — GATED OFF by default via betstackFillEnabled() (see its note: the
  // live payload does not match betstack.mjs yet). When enabled, it fills
  // slate ONLY for sports whose slate is still empty after the API-Sports
  // and Odds API passes — BetStack fills empty slots, it does not compete
  // with a primary source, and its consensus line is never published as a
  // board price (slate fixtures/scores only). Key-gated inside
  // betstack.mjs (BETSTACK_API_KEY); collectBetstackSlate never throws,
  // and this catch is a last resort — failures degrade to the feed's
  // betstackErrors map and can never block publishing.
  let betstackErrors = {};
  let betstackFilled = false;
  if (betstackFillEnabled()) {
    try {
      const emptySports = Object.keys(BETSTACK_SPORTS).filter(
        (s) => !slate.some((item) => item.sport === s)
      );
      const fill = await collectBetstackSlate({ sports: emptySports });
      betstackErrors = fill.errors;
      betstackFilled = fill.slate.length > 0;
      slate.push(...fill.slate);
      slate.sort((a, b) => {
        if (a.status !== b.status) return a.status === 'live' ? -1 : 1;
        return a.startTs - b.startTs;
      });
    } catch (err) {
      betstackErrors = { BetStack: String(err?.message || err) };
      console.error(`betstack layer failed: ${betstackErrors.BetStack}`);
    }
  }
  const feed = {
    generatedAt: new Date().toISOString(),
    source:
      '103 sports-feed collector — API-Sports (games/stats), The Odds API (board odds), ESPN (news)' +
      (betstackFilled ? ', BetStack (slate fill)' : ''),
    sportsConfigured,
    picks,
    slate,
    board,
    boardMeta: oddsLayer.meta,
    aggregates,
    aggregatesErrors,
    betstackErrors,
    apiSportsErrors,
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
    `Wrote ${outPath}: ${feed.picks.length} picks, ${feed.slate.length} slate items, ${feed.board.length} board games (Odds API: ${feed.boardMeta?.status}), ${newsCount} news articles, aggregates for ${Object.keys(feed.aggregates ?? {}).length} sports, sports: ${feed.sportsConfigured.join(', ') || 'none'}`
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
