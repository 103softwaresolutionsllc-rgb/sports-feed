/**
 * The Odds API board layer for the 103 sports-feed collector.
 *
 * Why this exists (Isaac's decision, 2026-11-17 — design doc amendment):
 * API-Sports' free plan refuses the CURRENT season for Basketball,
 * Baseball and Hockey ("Free plans do not have access to this season,
 * try from 2022 to 2024"), so the board the collector published from
 * API-Sports odds was empty for those sports. The board's odds source is
 * now The Odds API free tier, wired into the same collector run. The
 * PUBLISHED SHAPE DOES NOT CHANGE: entries are byte-for-byte the board
 * shape normalize.mjs's extractBoardGame emits (id / gameId / sport /
 * league / home / away / codes / startTs / startAt / lineSets), and
 * slate items are the toSlateItem shape — only the provenance of the
 * prices changes. API-Sports remains the source for games/stats,
 * aggregates and the props pipeline, and its board entries survive only
 * as a fallback for games The Odds API does not carry.
 *
 * Pure functions only — no I/O. The collector (collector.mjs) owns the
 * fetch, the credit headers, and reading the prior published feed.
 *
 * Credit discipline (free tier = 500 credits/month, SHARED with the
 * SportsBettingGPT backend's Sage get_live_odds usage on the same key):
 * one bulk odds call costs markets × regions = 3 markets × 1 region =
 * 3 credits, so a four-sport sweep costs 12 credits. Sweeps are gated
 * to at most one per SWEEP_MIN_INTERVAL_MS; at 22h that projects
 * ~33 sweeps/month ≈ 393 credits — inside the ≤400/month sweep budget
 * with the shared pool protected by the LOW_CREDIT_FLOOR skip below.
 * Between sweeps the board CARRIES OVER the prior published Odds API
 * entries verbatim (their lineSets keep the sweep's fetchedAt, so the
 * app ages them honestly) instead of republishing an empty board.
 */

import { SPORT_APIS, toSlateItem } from './normalize.mjs';

/** Display sport (SPORT_APIS key) → The Odds API sport key. */
export const ODDS_API_SPORT_KEYS = {
  Basketball: 'basketball_nba',
  Football: 'americanfootball_nfl',
  Baseball: 'baseball_mlb',
  Hockey: 'icehockey_nhl',
};

/**
 * Bookmaker preference, by The Odds API bookmaker `key`. The published
 * line set is ONE book's — the first book in this order that carries
 * all three markets complete; if none of the four does, the first
 * remaining bookmaker (payload order) that carries all three complete.
 * Two books' prices are never blended into one set.
 */
export const BOOKMAKER_PREFERENCE = ['draftkings', 'fanduel', 'betmgm', 'caesars'];

/** Minimum spacing between sweeps (see the credit math in the header). */
export const SWEEP_MIN_INTERVAL_MS = 22 * 60 * 60 * 1000;

/** Never sweep when the shared pool's remaining credits are below this. */
export const LOW_CREDIT_FLOOR = 50;

/** Carried-over games that started longer ago than this are dropped. */
export const CARRY_MAX_START_AGE_MS = 4 * 60 * 60 * 1000;

/** Cross-source "same game" start-time tolerance for the fallback merge. */
export const SAME_GAME_START_TOLERANCE_MS = 2 * 60 * 60 * 1000;

/**
 * Numeric American price (The Odds API with oddsFormat=american returns
 * numbers like -150 / 130) → the published string form ('-150' /
 * '+130'), or null when the price cannot be a real American price:
 * non-finite, zero, or |price| < 100 (the app's usable-price rule).
 */
export function americanPriceString(price) {
  const n = Number(price);
  if (!Number.isFinite(n) || n === 0 || Math.abs(n) < 100) return null;
  return n > 0 ? `+${Math.round(n)}` : `${Math.round(n)}`;
}

function marketOf(bookmaker, key) {
  const markets = Array.isArray(bookmaker?.markets) ? bookmaker.markets : [];
  return markets.find((m) => m?.key === key) || null;
}

function outcomeOf(market, name) {
  const outcomes = Array.isArray(market?.outcomes) ? market.outcomes : [];
  return outcomes.find((o) => o?.name === name) || null;
}

/**
 * One bookmaker's three markets from a raw Odds API game, in the board
 * line-set market shapes — or null unless ALL THREE markets are
 * complete. The board's Odds API sets are always complete: a partial
 * book never publishes here (the fallback merge, not a partial set, is
 * how a thin game gets covered).
 *
 *   moneyline { home, away }          — h2h outcomes named by team
 *   spread    { line, home, away }    — line signed from the HOME
 *                                       team's perspective (the home
 *                                       outcome's point, verbatim)
 *   total     { line, over, under }   — Over/Under outcomes' shared point
 */
export function completeMarkets(bookmaker, homeTeam, awayTeam) {
  if (!bookmaker || typeof bookmaker !== 'object') return null;

  const h2h = marketOf(bookmaker, 'h2h');
  const homeMl = outcomeOf(h2h, homeTeam);
  const awayMl = outcomeOf(h2h, awayTeam);
  const mlHome = americanPriceString(homeMl?.price);
  const mlAway = americanPriceString(awayMl?.price);
  if (mlHome == null || mlAway == null) return null;

  const spreads = marketOf(bookmaker, 'spreads');
  const homeSp = outcomeOf(spreads, homeTeam);
  const awaySp = outcomeOf(spreads, awayTeam);
  const spHome = americanPriceString(homeSp?.price);
  const spAway = americanPriceString(awaySp?.price);
  const homePoint = Number(homeSp?.point);
  const awayPoint = Number(awaySp?.point);
  if (spHome == null || spAway == null) return null;
  if (!Number.isFinite(homePoint) || !Number.isFinite(awayPoint)) return null;
  // The two sides of a spread are the same line with opposite signs
  // (or a 0 / pick'em pair). Anything else is not one honest line.
  if (awayPoint !== -homePoint) return null;

  const totals = marketOf(bookmaker, 'totals');
  const over = outcomeOf(totals, 'Over');
  const under = outcomeOf(totals, 'Under');
  const tOver = americanPriceString(over?.price);
  const tUnder = americanPriceString(under?.price);
  const overPoint = Number(over?.point);
  const underPoint = Number(under?.point);
  if (tOver == null || tUnder == null) return null;
  if (!Number.isFinite(overPoint) || !(overPoint > 0) || overPoint !== underPoint) {
    return null;
  }

  return {
    moneyline: { home: mlHome, away: mlAway },
    spread: { line: homePoint, home: spHome, away: spAway },
    total: { line: overPoint, over: tOver, under: tUnder },
  };
}

/**
 * Choose the ONE bookmaker whose line set publishes for a game:
 * BOOKMAKER_PREFERENCE order first, then payload order — in both cases
 * only a bookmaker carrying all three markets complete qualifies.
 * Returns { bookmaker, markets } or null when no book qualifies.
 */
export function chooseBookmaker(game) {
  const books = Array.isArray(game?.bookmakers) ? game.bookmakers : [];
  const usable = books.filter(
    (b) => b && typeof b === 'object' && typeof b.title === 'string' && b.title.trim()
  );
  const ordered = [
    ...BOOKMAKER_PREFERENCE.map((key) => usable.find((b) => b.key === key)).filter(Boolean),
    ...usable.filter((b) => !BOOKMAKER_PREFERENCE.includes(b.key)),
  ];
  for (const bookmaker of ordered) {
    const markets = completeMarkets(bookmaker, game.home_team, game.away_team);
    if (markets) return { bookmaker, markets };
  }
  return null;
}

/**
 * One raw Odds API game → { entry, slateItem } in the published board /
 * slate shapes, or null when the game cannot be identified honestly or
 * no bookmaker carries a complete line set. `fetchedAt` is the SWEEP's
 * fetch time — the board's as-of time for every price in the set.
 *
 * Ids keep the collector's apisp-board- / apisp-slate- scheme with The
 * Odds API game id as the game component: the app joins slate items to
 * board entries by that scheme, and the prices' provenance is carried
 * honestly by the line set's bookmaker name and the feed's boardMeta.
 */
export function oddsApiGameToBoard(game, sport, fetchedAt) {
  const cfg = SPORT_APIS[sport];
  if (!cfg || !game || typeof game !== 'object') return null;
  if (typeof game.id !== 'string' || !game.id) return null;
  const home = typeof game.home_team === 'string' ? game.home_team.trim() : '';
  const away = typeof game.away_team === 'string' ? game.away_team.trim() : '';
  if (!home || !away) return null;
  const startTs = Date.parse(typeof game.commence_time === 'string' ? game.commence_time : '');
  if (!Number.isFinite(startTs) || startTs <= 0) return null;
  const fetchedMs = Date.parse(typeof fetchedAt === 'string' ? fetchedAt : '');
  if (!Number.isFinite(fetchedMs)) return null;
  const fetchedIso = new Date(fetchedMs).toISOString();

  const chosen = chooseBookmaker(game);
  if (!chosen) return null;
  const lineSet = {
    bookmaker: chosen.bookmaker.title.trim(),
    fetchedAt: fetchedIso,
    moneyline: chosen.markets.moneyline,
    spread: chosen.markets.spread,
    total: chosen.markets.total,
  };
  const entry = {
    id: `apisp-board-${sport}-${game.id}`,
    gameId: game.id,
    sport,
    league: cfg.sportLabel,
    home,
    away,
    homeCode: null,
    awayCode: null,
    startTs,
    startAt: new Date(startTs).toISOString(),
    lineSets: [lineSet],
  };
  // Slate twin via the same normalizer the API-Sports path uses, so the
  // shape cannot drift. The Odds API lists in-play and upcoming games;
  // a commence time in the past means the game has started.
  const slateItem = toSlateItem({
    id: game.id,
    sport,
    sportLabel: cfg.sportLabel,
    home,
    away,
    homeCode: null,
    awayCode: null,
    homeScore: null,
    awayScore: null,
    status: startTs <= fetchedMs ? 'live' : 'upcoming',
    statusLong: null,
    startTs,
  });
  return { entry, slateItem };
}

/**
 * A published board entry → its slate twin. Used on carry-over runs,
 * where the entry survives from the prior feed but the slate is rebuilt
 * every run. Status is read against the CURRENT time (a carried game
 * may have tipped off since the sweep).
 */
export function boardEntryToSlateItem(entry, nowMs) {
  if (!entry || typeof entry !== 'object') return null;
  if (!SPORT_APIS[entry.sport] || !entry.home || !entry.away) return null;
  if (!Number.isFinite(entry.startTs) || entry.startTs <= 0) return null;
  return toSlateItem({
    id: entry.gameId,
    sport: entry.sport,
    sportLabel: entry.league,
    home: entry.home,
    away: entry.away,
    homeCode: entry.homeCode ?? null,
    awayCode: entry.awayCode ?? null,
    homeScore: null,
    awayScore: null,
    status: entry.startTs <= nowMs ? 'live' : 'upcoming',
    statusLong: null,
    startTs: entry.startTs,
  });
}

/** A whole Odds API bulk response (array of games) → board + slate lists. */
export function oddsApiPayloadToBoard(games, sport, fetchedAt) {
  const entries = [];
  const slateItems = [];
  for (const game of Array.isArray(games) ? games : []) {
    const mapped = oddsApiGameToBoard(game, sport, fetchedAt);
    if (!mapped) continue;
    entries.push(mapped.entry);
    slateItems.push(mapped.slateItem);
  }
  return { entries, slateItems };
}

/**
 * The sweep decision. Inputs are the prior published feed's boardMeta
 * (oddsApiFetchedAt + creditsRemaining, as persisted by the last run)
 * and the current time. Rules, in order:
 *   - remaining credits known and below LOW_CREDIT_FLOOR → skip
 *     ('skipped-low-credits'); the shared pool is never drained by the
 *     board.
 *   - last sweep younger than SWEEP_MIN_INTERVAL_MS → skip ('carried').
 *   - otherwise → sweep ('due'; also the no-prior-state first run).
 */
export function shouldSweep({ nowMs, lastSweepMs, creditsRemaining }) {
  if (
    creditsRemaining != null &&
    Number.isFinite(Number(creditsRemaining)) &&
    Number(creditsRemaining) < LOW_CREDIT_FLOOR
  ) {
    return { sweep: false, reason: 'skipped-low-credits' };
  }
  if (
    lastSweepMs != null &&
    Number.isFinite(lastSweepMs) &&
    nowMs - lastSweepMs < SWEEP_MIN_INTERVAL_MS
  ) {
    return { sweep: false, reason: 'carried' };
  }
  return { sweep: true, reason: 'due' };
}

/**
 * The prior published board's Odds API entries, carried into this run
 * verbatim when no sweep happens. Identified by the ids the last sweep
 * recorded in boardMeta.oddsApiGameIds (API-Sports entries are never
 * carried — that layer republishes its own every run). Games that
 * started more than CARRY_MAX_START_AGE_MS ago are dropped: their odds
 * are dead and the game is over or nearly so.
 */
export function carryOverEntries(priorBoard, priorMeta, nowMs) {
  const ids = new Set(
    Array.isArray(priorMeta?.oddsApiGameIds) ? priorMeta.oddsApiGameIds : []
  );
  if (ids.size === 0 || !Array.isArray(priorBoard)) return [];
  return priorBoard.filter(
    (entry) =>
      entry &&
      ids.has(entry.id) &&
      Number.isFinite(entry.startTs) &&
      entry.startTs > nowMs - CARRY_MAX_START_AGE_MS
  );
}

function teamKey(name) {
  return String(name || '').trim().toLowerCase();
}

/**
 * Cross-source identity: same sport, same team pair (either
 * orientation), start times within tolerance. Used ONLY to keep an
 * API-Sports fallback entry from duplicating a game The Odds API
 * already covers — never to blend the two sources' prices.
 */
export function sameGame(a, b) {
  if (!a || !b || a.sport !== b.sport) return false;
  const aHome = teamKey(a.home);
  const aAway = teamKey(a.away);
  if (!aHome || !aAway) return false;
  const samePair =
    (aHome === teamKey(b.home) && aAway === teamKey(b.away)) ||
    (aHome === teamKey(b.away) && aAway === teamKey(b.home));
  if (!samePair) return false;
  if (!Number.isFinite(a.startTs) || !Number.isFinite(b.startTs)) return false;
  return Math.abs(a.startTs - b.startTs) <= SAME_GAME_START_TOLERANCE_MS;
}

/**
 * Board merge: Odds API entries first (they win every shared game),
 * then API-Sports entries for games The Odds API does not carry.
 */
export function mergeBoard(oddsEntries, apiSportsEntries) {
  const merged = [...(Array.isArray(oddsEntries) ? oddsEntries : [])];
  for (const entry of Array.isArray(apiSportsEntries) ? apiSportsEntries : []) {
    if (!merged.some((o) => sameGame(o, entry))) merged.push(entry);
  }
  return merged;
}

function slateTeams(item) {
  const parts = String(item?.teams || '').split(' vs ');
  if (parts.length !== 2) return null;
  return { away: parts[0], home: parts[1] };
}

/**
 * Slate merge with the same rule, in slate terms: an Odds API slate
 * item and an API-Sports slate item describing the same game collapse
 * to the ODDS API item — its id is the one that joins the Odds API
 * board entry in the app, so the game lists once, with its prices.
 */
export function mergeSlate(oddsItems, apiSportsItems) {
  const merged = [...(Array.isArray(oddsItems) ? oddsItems : [])];
  const asGames = merged
    .map((item) => {
      const t = slateTeams(item);
      return t ? { sport: item.sport, startTs: item.startTs, ...t } : null;
    })
    .filter(Boolean);
  for (const item of Array.isArray(apiSportsItems) ? apiSportsItems : []) {
    const t = slateTeams(item);
    if (!t) {
      merged.push(item);
      continue;
    }
    const game = { sport: item.sport, startTs: item.startTs, ...t };
    if (!asGames.some((o) => sameGame(o, game))) {
      merged.push(item);
      asGames.push(game);
    }
  }
  return merged;
}
