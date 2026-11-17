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
  carryOverEntries,
  sameGame,
  mergeBoard,
  mergeSlate,
  SWEEP_MIN_INTERVAL_MS,
  LOW_CREDIT_FLOOR,
} from '../oddsapi.mjs';

const FETCHED_AT = '2026-11-17T12:00:00.000Z';
const FETCHED_MS = Date.parse(FETCHED_AT);
const payload = JSON.parse(
  readFileSync(new URL('./fixtures/oddsapi-nba-odds.json', import.meta.url), 'utf8')
);
const [lakersGame, warriorsGame, knicksGame] = payload;

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
