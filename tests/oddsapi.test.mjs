/**
 * Unit tests for The Odds API board layer (oddsapi.mjs) — the board's
 * odds source since 2026-11-17 (design-doc amendment; API-Sports' free
 * plan refuses the current season for Basketball/Baseball/Hockey).
 *
 * The fixture (tests/fixtures/oddsapi-nba-odds.json) is built to The
 * Odds API's documented bulk-odds envelope (array of games →
 * bookmakers → markets → outcomes, American prices as numbers, spread /
 * total points on the outcomes). It carries: a game where FanDuel is
 * first in payload order but DraftKings must win by preference; a game
 * where DraftKings lacks totals and a non-preference book is complete,
 * yet BetMGM must win by preference; and a game no book carries
 * complete, which must produce no entry. No live calls — the fetch and
 * the credit headers live in collector.mjs and are not exercised here.
 *
 * Run: node --test tests/oddsapi.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  americanPriceString,
  chooseBookmaker,
  completeMarkets,
  oddsApiGameToBoard,
  oddsApiPayloadToBoard,
  boardEntryToSlateItem,
  shouldSweep,
  sportsFetchedAtFromMeta,
  sweepPlan,
  carryOverEntries,
  sameGame,
  mergeBoard,
  mergeSlate,
  ODDS_API_SPORT_KEYS,
  ODDS_API_SPORT_MARKETS,
  SWEEP_MIN_INTERVAL_MS,
  LOW_CREDIT_FLOOR,
} from '../oddsapi.mjs';
import { oddsCoveredSports, extractApiSportsErrors } from '../collector.mjs';

const FETCHED_AT = '2026-11-17T12:00:00.000Z';
const FETCHED_MS = Date.parse(FETCHED_AT);
const payload = JSON.parse(
  readFileSync(new URL('./fixtures/oddsapi-nba-odds.json', import.meta.url), 'utf8')
);
const [lakersGame, warriorsGame, knicksGame] = payload;
const mmaPayload = JSON.parse(
  readFileSync(new URL('./fixtures/oddsapi-mma-odds.json', import.meta.url), 'utf8')
);
const [topuriaFight, merabFight, islamFight] = mmaPayload;

// --- price formatting ------------------------------------------------------

test('americanPriceString renders the published string form', () => {
  assert.equal(americanPriceString(-150), '-150');
  assert.equal(americanPriceString(130), '+130');
  assert.equal(americanPriceString(100), '+100');
  assert.equal(americanPriceString(-100), '-100');
});

test('americanPriceString rejects prices that cannot be American odds', () => {
  assert.equal(americanPriceString(0), null);
  assert.equal(americanPriceString(-50), null);
  assert.equal(americanPriceString(99), null);
  assert.equal(americanPriceString('n/a'), null);
  assert.equal(americanPriceString(undefined), null);
});

// --- market mapping --------------------------------------------------------

test('completeMarkets maps h2h / spreads / totals into the board shapes', () => {
  const dk = lakersGame.bookmakers.find((b) => b.key === 'draftkings');
  const markets = completeMarkets(dk, lakersGame.home_team, lakersGame.away_team);
  assert.deepEqual(markets.moneyline, { home: '-150', away: '+130' });
  // Spread line is the HOME outcome's point, signed from home's side.
  assert.deepEqual(markets.spread, { line: -4.5, home: '-110', away: '-110' });
  assert.deepEqual(markets.total, { line: 225.5, over: '-105', under: '-115' });
});

test('completeMarkets is null when any one market is missing or partial', () => {
  const dk = warriorsGame.bookmakers.find((b) => b.key === 'draftkings');
  assert.equal(completeMarkets(dk, warriorsGame.home_team, warriorsGame.away_team), null);
  const knicksDk = knicksGame.bookmakers[0];
  assert.equal(completeMarkets(knicksDk, knicksGame.home_team, knicksGame.away_team), null);
});

test('completeMarkets rejects a spread whose sides are not one line', () => {
  const book = {
    title: 'Broken Book',
    markets: [
      { key: 'h2h', outcomes: [
        { name: 'Home Team', price: -110 }, { name: 'Away Team', price: -110 } ] },
      { key: 'spreads', outcomes: [
        { name: 'Home Team', point: -4.5, price: -110 },
        { name: 'Away Team', point: 3.5, price: -110 } ] },
      { key: 'totals', outcomes: [
        { name: 'Over', point: 200.5, price: -110 },
        { name: 'Under', point: 200.5, price: -110 } ] },
    ],
  };
  assert.equal(completeMarkets(book, 'Home Team', 'Away Team'), null);
});

// --- bookmaker preference --------------------------------------------------

test('chooseBookmaker prefers DraftKings over payload order', () => {
  const chosen = chooseBookmaker(lakersGame);
  assert.equal(chosen.bookmaker.key, 'draftkings');
  assert.equal(chosen.markets.moneyline.home, '-150');
});

test('chooseBookmaker skips a preferred book missing a market; preference still beats payload order', () => {
  // DraftKings lacks totals here; BetRivers is complete AND first in
  // payload order; BetMGM is complete and outranks Caesars (also
  // complete) in the preference list. BetMGM must win.
  const chosen = chooseBookmaker(warriorsGame);
  assert.equal(chosen.bookmaker.key, 'betmgm');
  assert.deepEqual(chosen.markets.total, { line: 233.5, over: '-110', under: '-110' });
});

test('chooseBookmaker falls back to the first complete non-preference book', () => {
  const game = {
    home_team: 'Home Team',
    away_team: 'Away Team',
    bookmakers: [
      { key: 'fanduel', title: 'FanDuel', markets: [
        { key: 'h2h', outcomes: [
          { name: 'Home Team', price: -110 }, { name: 'Away Team', price: -110 } ] } ] },
      { key: 'somebook', title: 'Some Book', markets: [
        { key: 'h2h', outcomes: [
          { name: 'Home Team', price: -115 }, { name: 'Away Team', price: -105 } ] },
        { key: 'spreads', outcomes: [
          { name: 'Home Team', point: -1.5, price: -110 },
          { name: 'Away Team', point: 1.5, price: -110 } ] },
        { key: 'totals', outcomes: [
          { name: 'Over', point: 210.5, price: -110 },
          { name: 'Under', point: 210.5, price: -110 } ] } ] },
    ],
  };
  const chosen = chooseBookmaker(game);
  assert.equal(chosen.bookmaker.key, 'somebook');
});

test('chooseBookmaker is null when no book carries all three markets', () => {
  assert.equal(chooseBookmaker(knicksGame), null);
});

// --- board entry + slate emission ------------------------------------------

test('oddsApiGameToBoard emits the existing board shape with one line set', () => {
  const { entry, slateItem } = oddsApiGameToBoard(lakersGame, 'Basketball', FETCHED_AT);
  assert.deepEqual(
    Object.keys(entry).sort(),
    ['away', 'awayCode', 'gameId', 'home', 'homeCode', 'id', 'league', 'lineSets', 'sport', 'startAt', 'startTs'].sort()
  );
  assert.equal(entry.id, `apisp-board-Basketball-${lakersGame.id}`);
  assert.equal(entry.gameId, lakersGame.id);
  assert.equal(entry.sport, 'Basketball');
  assert.equal(entry.league, 'NBA');
  assert.equal(entry.home, 'Los Angeles Lakers');
  assert.equal(entry.away, 'Boston Celtics');
  assert.equal(entry.startTs, Date.parse('2026-11-18T03:30:00Z'));
  assert.equal(entry.startAt, '2026-11-18T03:30:00.000Z');
  assert.equal(entry.lineSets.length, 1);
  assert.equal(entry.lineSets[0].bookmaker, 'DraftKings');
  // The sweep's fetch time is the set's as-of time.
  assert.equal(entry.lineSets[0].fetchedAt, FETCHED_AT);
  assert.deepEqual(entry.lineSets[0].spread, { line: -4.5, home: '-110', away: '-110' });
  // Slate twin: same id scheme, toSlateItem shape.
  assert.equal(slateItem.id, `apisp-slate-Basketball-${lakersGame.id}`);
  assert.equal(slateItem.teams, 'Boston Celtics vs Los Angeles Lakers');
  assert.equal(slateItem.status, 'upcoming');
  assert.equal(slateItem.sportLabel, 'NBA');
});

test('oddsApiGameToBoard marks a commenced game live on the slate', () => {
  const live = { ...lakersGame, commence_time: '2026-11-17T10:00:00Z' };
  const { slateItem } = oddsApiGameToBoard(live, 'Basketball', FETCHED_AT);
  assert.equal(slateItem.status, 'live');
});

test('oddsApiGameToBoard is null for an unidentifiable or uncovered game', () => {
  assert.equal(oddsApiGameToBoard(knicksGame, 'Basketball', FETCHED_AT), null);
  assert.equal(oddsApiGameToBoard({ ...lakersGame, id: '' }, 'Basketball', FETCHED_AT), null);
  assert.equal(oddsApiGameToBoard(lakersGame, 'Soccer', FETCHED_AT), null);
  assert.equal(oddsApiGameToBoard(lakersGame, 'Basketball', 'not-a-time'), null);
});

test('oddsApiPayloadToBoard maps a whole response, dropping uncovered games', () => {
  const { entries, slateItems } = oddsApiPayloadToBoard(payload, 'Basketball', FETCHED_AT);
  assert.equal(entries.length, 2);
  assert.equal(slateItems.length, 2);
  assert.deepEqual(
    entries.map((e) => e.lineSets[0].bookmaker),
    ['DraftKings', 'BetMGM']
  );
});

test('boardEntryToSlateItem rebuilds the slate twin from a carried entry', () => {
  const { entry } = oddsApiGameToBoard(lakersGame, 'Basketball', FETCHED_AT);
  const item = boardEntryToSlateItem(entry, FETCHED_MS);
  assert.equal(item.id, `apisp-slate-Basketball-${lakersGame.id}`);
  assert.equal(item.teams, 'Boston Celtics vs Los Angeles Lakers');
  assert.equal(item.status, 'upcoming');
  const later = boardEntryToSlateItem(entry, Date.parse('2026-11-18T04:00:00Z'));
  assert.equal(later.status, 'live');
});

// --- sweep gate --------------------------------------------------------------

test('shouldSweep: first run (no prior state) sweeps', () => {
  assert.deepEqual(
    shouldSweep({ nowMs: FETCHED_MS, lastSweepMs: null, creditsRemaining: null }),
    { sweep: true, reason: 'due' }
  );
});

test('shouldSweep: a sweep younger than the gate carries over', () => {
  assert.deepEqual(
    shouldSweep({
      nowMs: FETCHED_MS + 90 * 60 * 1000,
      lastSweepMs: FETCHED_MS,
      creditsRemaining: 480,
    }),
    { sweep: false, reason: 'carried' }
  );
});

test('shouldSweep: a sweep older than the gate is due again', () => {
  assert.deepEqual(
    shouldSweep({
      nowMs: FETCHED_MS + SWEEP_MIN_INTERVAL_MS + 1,
      lastSweepMs: FETCHED_MS,
      creditsRemaining: 480,
    }),
    { sweep: true, reason: 'due' }
  );
});

test('shouldSweep: low credits skip even when a sweep is due', () => {
  assert.deepEqual(
    shouldSweep({
      nowMs: FETCHED_MS + SWEEP_MIN_INTERVAL_MS * 3,
      lastSweepMs: FETCHED_MS,
      creditsRemaining: LOW_CREDIT_FLOOR - 1,
    }),
    { sweep: false, reason: 'skipped-low-credits' }
  );
});

// --- carry-over ---------------------------------------------------------------

test('carryOverEntries keeps only recorded Odds API entries, verbatim', () => {
  const { entry } = oddsApiGameToBoard(lakersGame, 'Basketball', FETCHED_AT);
  const apiSportsEntry = {
    id: 'apisp-board-Basketball-456789',
    gameId: 456789,
    sport: 'Basketball',
    startTs: entry.startTs,
  };
  const meta = { oddsApiGameIds: [entry.id] };
  const carried = carryOverEntries([entry, apiSportsEntry], meta, FETCHED_MS + 60_000);
  assert.deepEqual(carried, [entry]);
});

test('carryOverEntries drops games that started over 4 hours ago', () => {
  const { entry } = oddsApiGameToBoard(lakersGame, 'Basketball', FETCHED_AT);
  const meta = { oddsApiGameIds: [entry.id] };
  const afterStart = entry.startTs + 4 * 60 * 60 * 1000 + 1;
  assert.deepEqual(carryOverEntries([entry], meta, afterStart), []);
  assert.deepEqual(carryOverEntries([entry], null, FETCHED_MS), []);
});

// --- fallback merge -------------------------------------------------------------

test('sameGame matches across providers by teams + start window', () => {
  const a = { sport: 'Football', home: 'Kansas City Chiefs', away: 'Buffalo Bills', startTs: 1_000_000 };
  const b = { sport: 'Football', home: 'Buffalo Bills', away: 'Kansas City Chiefs', startTs: 1_000_000 + 30 * 60 * 1000 };
  assert.equal(sameGame(a, b), true);
  assert.equal(sameGame(a, { ...b, startTs: 1_000_000 + 3 * 60 * 60 * 1000 }), false);
  assert.equal(sameGame(a, { ...b, sport: 'Basketball' }), false);
});

test('mergeBoard: the Odds API entry wins a shared game; API-Sports-only games survive', () => {
  const oddsEntry = {
    id: 'apisp-board-Football-oddsid1',
    sport: 'Football',
    home: 'Kansas City Chiefs',
    away: 'Buffalo Bills',
    startTs: 1_000_000,
    lineSets: [{ bookmaker: 'DraftKings' }],
  };
  const dupApiSports = {
    id: 'apisp-board-Football-123',
    sport: 'Football',
    home: 'Kansas City Chiefs',
    away: 'Buffalo Bills',
    startTs: 1_000_000,
    lineSets: [{ bookmaker: 'SomeBook' }],
  };
  const otherApiSports = {
    id: 'apisp-board-Football-124',
    sport: 'Football',
    home: 'Dallas Cowboys',
    away: 'Philadelphia Eagles',
    startTs: 2_000_000,
    lineSets: [{ bookmaker: 'SomeBook' }],
  };
  const merged = mergeBoard([oddsEntry], [dupApiSports, otherApiSports]);
  assert.deepEqual(merged, [oddsEntry, otherApiSports]);
});

// --- MMA / UFC (h2h-only board sport, added 2026-11-17) ----------------------

test('MMA wiring: sport key mma_mixed_martial_arts, h2h-only markets', () => {
  assert.equal(ODDS_API_SPORT_KEYS.MMA, 'mma_mixed_martial_arts');
  assert.deepEqual(ODDS_API_SPORT_MARKETS.MMA, ['h2h']);
  // Team sports keep the full three-market set.
  assert.deepEqual(ODDS_API_SPORT_MARKETS.Basketball, ['h2h', 'spreads', 'totals']);
  assert.deepEqual(ODDS_API_SPORT_MARKETS.Hockey, ['h2h', 'spreads', 'totals']);
});

test('completeMarkets with h2h only: a moneyline-only book IS complete for MMA', () => {
  const dk = topuriaFight.bookmakers.find((b) => b.key === 'draftkings');
  const markets = completeMarkets(dk, topuriaFight.home_team, topuriaFight.away_team, ['h2h']);
  assert.deepEqual(markets.moneyline, { home: '-425', away: '+330' });
  // The published shape still carries all three keys; uncarried
  // markets are null (normalize.mjs's line sets do the same).
  assert.equal(markets.spread, null);
  assert.equal(markets.total, null);
});

test('completeMarkets with h2h only: a partial moneyline (one fighter) does NOT publish', () => {
  const dk = merabFight.bookmakers[0];
  assert.equal(
    completeMarkets(dk, merabFight.home_team, merabFight.away_team, ['h2h']),
    null
  );
});

test('chooseBookmaker for MMA: preference order holds among moneyline-only books', () => {
  // FanDuel is first in payload order and complete; DraftKings must
  // still win by preference.
  const chosen = chooseBookmaker(topuriaFight, ['h2h']);
  assert.equal(chosen.bookmaker.key, 'draftkings');
  assert.deepEqual(chosen.markets.moneyline, { home: '-425', away: '+330' });
});

test('chooseBookmaker for MMA: a partial preferred book is skipped for a complete one', () => {
  // DraftKings prices only one fighter here; BetMGM prices both.
  const chosen = chooseBookmaker(islamFight, ['h2h']);
  assert.equal(chosen.bookmaker.key, 'betmgm');
  assert.deepEqual(chosen.markets.moneyline, { home: '-275', away: '+235' });
  // And when the ONLY book is partial, no book qualifies at all.
  assert.equal(chooseBookmaker(merabFight, ['h2h']), null);
});

test('oddsApiGameToBoard maps an MMA fight: sport MMA, league UFC, moneyline-only set', () => {
  const { entry, slateItem } = oddsApiGameToBoard(topuriaFight, 'MMA', FETCHED_AT);
  assert.equal(entry.id, `apisp-board-MMA-${topuriaFight.id}`);
  assert.equal(entry.sport, 'MMA');
  assert.equal(entry.league, 'UFC');
  assert.equal(entry.home, 'Ilia Topuria');
  assert.equal(entry.away, 'Charles Oliveira');
  assert.equal(entry.lineSets.length, 1);
  assert.equal(entry.lineSets[0].bookmaker, 'DraftKings');
  assert.deepEqual(entry.lineSets[0].moneyline, { home: '-425', away: '+330' });
  assert.equal(entry.lineSets[0].spread, null);
  assert.equal(entry.lineSets[0].total, null);
  // Slate twin through the existing toSlateItem path.
  assert.equal(slateItem.id, `apisp-slate-MMA-${topuriaFight.id}`);
  assert.equal(slateItem.sport, 'MMA');
  assert.equal(slateItem.sportLabel, 'UFC');
  assert.equal(slateItem.teams, 'Charles Oliveira vs Ilia Topuria');
});

test('oddsApiPayloadToBoard for MMA drops the fight no book carries complete', () => {
  const { entries, slateItems } = oddsApiPayloadToBoard(mmaPayload, 'MMA', FETCHED_AT);
  assert.equal(entries.length, 2);
  assert.equal(slateItems.length, 2);
  assert.deepEqual(
    entries.map((e) => e.lineSets[0].bookmaker),
    ['DraftKings', 'BetMGM']
  );
});

test('boardEntryToSlateItem rebuilds the slate twin for a carried MMA entry', () => {
  const { entry } = oddsApiGameToBoard(topuriaFight, 'MMA', FETCHED_AT);
  const item = boardEntryToSlateItem(entry, FETCHED_MS);
  assert.equal(item.id, `apisp-slate-MMA-${topuriaFight.id}`);
  assert.equal(item.sportLabel, 'UFC');
  assert.equal(item.teams, 'Charles Oliveira vs Ilia Topuria');
});

// --- per-sport sweep gate -------------------------------------------------------

test('sportsFetchedAtFromMeta migrates the pre-map global time to the four original sports only', () => {
  const byKey = sportsFetchedAtFromMeta({ oddsApiFetchedAt: FETCHED_AT });
  assert.equal(byKey.basketball_nba, FETCHED_MS);
  assert.equal(byKey.americanfootball_nfl, FETCHED_MS);
  assert.equal(byKey.baseball_mlb, FETCHED_MS);
  assert.equal(byKey.icehockey_nhl, FETCHED_MS);
  // MMA was not wired for the global sweep — no inherited time, so
  // it is due immediately.
  assert.equal(byKey.mma_mixed_martial_arts, undefined);
});

test('sportsFetchedAtFromMeta prefers the per-sport map where it has a key', () => {
  const later = '2026-11-17T18:00:00.000Z';
  const byKey = sportsFetchedAtFromMeta({
    oddsApiFetchedAt: FETCHED_AT,
    oddsApiSportsFetchedAt: { basketball_nba: later, mma_mixed_martial_arts: later },
  });
  assert.equal(byKey.basketball_nba, Date.parse(later));
  assert.equal(byKey.mma_mixed_martial_arts, Date.parse(later));
  assert.equal(byKey.icehockey_nhl, FETCHED_MS); // migrated from the global time
});

test('sweepPlan: a newly added sport is due immediately while the others carry over', () => {
  const plan = sweepPlan({
    nowMs: FETCHED_MS + 60 * 60 * 1000, // 1h after the last full sweep
    priorMeta: { oddsApiFetchedAt: FETCHED_AT },
    creditsRemaining: 491,
  });
  assert.equal(plan.lowCredits, false);
  assert.deepEqual(plan.due, ['MMA']);
  assert.deepEqual(
    [...plan.carriedSports].sort(),
    ['Baseball', 'Basketball', 'Football', 'Hockey']
  );
});

test('sweepPlan: the 22h boundary — due at exactly the interval, carried 1ms before', () => {
  const meta = { oddsApiFetchedAt: FETCHED_AT };
  const justBefore = sweepPlan({
    nowMs: FETCHED_MS + SWEEP_MIN_INTERVAL_MS - 1,
    priorMeta: meta,
    creditsRemaining: 491,
  });
  assert.deepEqual(justBefore.due, ['MMA']); // only the never-swept sport
  const atBoundary = sweepPlan({
    nowMs: FETCHED_MS + SWEEP_MIN_INTERVAL_MS,
    priorMeta: meta,
    creditsRemaining: 491,
  });
  assert.deepEqual(
    [...atBoundary.due].sort(),
    ['Baseball', 'Basketball', 'Football', 'Hockey', 'MMA']
  );
  assert.deepEqual(atBoundary.carriedSports, []);
});

test('sweepPlan: low credits close every sport gate (global floor)', () => {
  const plan = sweepPlan({
    nowMs: FETCHED_MS + SWEEP_MIN_INTERVAL_MS * 3,
    priorMeta: { oddsApiFetchedAt: FETCHED_AT },
    creditsRemaining: LOW_CREDIT_FLOOR - 1,
  });
  assert.equal(plan.lowCredits, true);
  assert.deepEqual(plan.due, []);
});

// --- API-Sports error relocation -------------------------------------------------

test('error relocation: a successful EMPTY sweep still covers its sport (MLB offseason)', () => {
  // Baseball's sweep came back HTTP OK with zero games: no board
  // entries, but the sport was swept — its dead API-Sports season
  // message must move out of `errors` all the same.
  const layer = { entries: [], sweptSports: ['Baseball'] };
  const covered = oddsCoveredSports(layer);
  assert.equal(covered.has('Baseball'), true);
  const errors = {
    Baseball: 'API-Sports Baseball error: {"plan":"Free plans do not have access to this season"}',
    Hockey: 'API-Sports Hockey error: {"plan":"Free plans do not have access to this season"}',
  };
  const apiSportsErrors = extractApiSportsErrors(errors, covered);
  assert.deepEqual(Object.keys(apiSportsErrors), ['Baseball']);
  assert.deepEqual(Object.keys(errors), ['Hockey']);
});

test('error relocation: sports with board entries stay covered via entries (carry-over runs)', () => {
  const layer = {
    entries: [{ sport: 'Hockey' }, { sport: 'Basketball' }],
    sweptSports: [],
  };
  const covered = oddsCoveredSports(layer);
  assert.equal(covered.has('Hockey'), true);
  assert.equal(covered.has('Basketball'), true);
  assert.equal(covered.has('Baseball'), false);
});

test('mergeSlate: a shared game lists once, as the Odds API item (the id that joins the board)', () => {
  const oddsItem = {
    id: 'apisp-slate-Football-oddsid1',
    sport: 'Football',
    teams: 'Buffalo Bills vs Kansas City Chiefs',
    startTs: 1_000_000,
    status: 'upcoming',
  };
  const dupApiSports = {
    id: 'apisp-slate-Football-123',
    sport: 'Football',
    teams: 'Buffalo Bills vs Kansas City Chiefs',
    startTs: 1_000_000,
    status: 'upcoming',
  };
  const otherApiSports = {
    id: 'apisp-slate-Football-124',
    sport: 'Football',
    teams: 'Philadelphia Eagles vs Dallas Cowboys',
    startTs: 2_000_000,
    status: 'upcoming',
  };
  const merged = mergeSlate([oddsItem], [dupApiSports, otherApiSports]);
  assert.deepEqual(merged, [oddsItem, otherApiSports]);
});
