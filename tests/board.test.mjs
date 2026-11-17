/**
 * Unit tests for the board extraction in normalize.mjs (Predict remake
 * spec §4.2 / §10 Phase 1): the collector publishes a `board` section in
 * feed.json from the /odds responses it already fetches.
 *
 * The fixture (tests/fixtures/apisports-nba-odds.json) is built to the
 * API-Sports documented /odds envelope — the same value shapes
 * extractFavoriteLine already parses in production (Home/Away pair =
 * moneyline, "Home -5.5" style values = spread, "Over 221.5" style =
 * total) — for the Lakers/Celtics game shape used in games.test.mjs.
 * It carries: one full bookmaker (all three markets, plus an alternate
 * spread line that must NOT win over the first complete pair), one
 * bookmaker missing the spread market, one bookmaker with no name, and
 * one bookmaker whose only prices are unreadable.
 *
 * Run: node --test tests/board.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  normalizeGame,
  extractBoardGame,
  extractBoardLineSets,
  extractBoardLineSet,
} from '../normalize.mjs';

const FETCHED_AT = '2026-10-22T16:40:00.000Z';
// Thu Oct 22 2026, 5:30 PM PDT = 2026-10-23T00:30:00Z (games.test.mjs base + 900s).
const START_TS = 1792714500 + 900;

const oddsPayload = JSON.parse(
  readFileSync(new URL('./fixtures/apisports-nba-odds.json', import.meta.url), 'utf8')
);

const nbaGameRaw = {
  id: 456789,
  date: '2026-10-23T00:30:00+00:00',
  time: '00:30',
  timestamp: START_TS,
  timezone: 'UTC',
  status: { short: 'NS', long: 'Not Started' },
  league: { id: 12, name: 'NBA', season: '2026-2027' },
  teams: {
    home: { id: 1, name: 'Los Angeles Lakers', code: 'LAL' },
    away: { id: 2, name: 'Boston Celtics', code: 'BOS' },
  },
  scores: {
    home: { quarter_1: null, quarter_2: null, quarter_3: null, quarter_4: null, total: null },
    away: { quarter_1: null, quarter_2: null, quarter_3: null, quarter_4: null, total: null },
  },
};
const game = normalizeGame(nbaGameRaw, 'Basketball');

test('fixture game normalizes (sanity: board tests run on a real game shape)', () => {
  assert.ok(game);
  assert.equal(game.id, 456789);
  assert.equal(game.startTs, START_TS * 1000);
});

test('board entry carries game id, league, teams and start time (spec §4.2)', () => {
  const board = extractBoardGame(oddsPayload, game, FETCHED_AT);
  assert.ok(board);
  assert.equal(board.id, 'apisp-board-Basketball-456789');
  assert.equal(board.gameId, 456789);
  assert.equal(board.sport, 'Basketball');
  assert.equal(board.league, 'NBA');
  assert.equal(board.home, 'Los Angeles Lakers');
  assert.equal(board.away, 'Boston Celtics');
  assert.equal(board.homeCode, 'LAL');
  assert.equal(board.awayCode, 'BOS');
  assert.equal(board.startTs, START_TS * 1000);
  assert.equal(board.startAt, new Date(START_TS * 1000).toISOString());
});

test('full bookmaker publishes all three markets, American prices, name + fetchedAt', () => {
  const board = extractBoardGame(oddsPayload, game, FETCHED_AT);
  const bet365 = board.lineSets.find((s) => s.bookmaker === 'Bet365');
  assert.ok(bet365, 'Bet365 line set present');
  assert.equal(bet365.fetchedAt, FETCHED_AT);
  // Decimals converted at publish: 1.50 -> -200, 2.50 -> +150.
  assert.deepEqual(bet365.moneyline, { home: '-200', away: '+150' });
  // First complete spread pair wins (the -6.5 alternate line is not used);
  // line signed from the home perspective; 2.00 -> +100, 1.80 -> -125.
  assert.deepEqual(bet365.spread, { line: -5.5, home: '+100', away: '-125' });
  assert.deepEqual(bet365.total, { line: 221.5, over: '+100', under: '-125' });
});

test('missing market publishes as null — the rest of the line set still ships', () => {
  const board = extractBoardGame(oddsPayload, game, FETCHED_AT);
  const betway = board.lineSets.find((s) => s.bookmaker === 'Betway');
  assert.ok(betway, 'Betway line set present');
  assert.equal(betway.spread, null);
  assert.deepEqual(betway.moneyline, { home: '+100', away: '-125' });
  assert.deepEqual(betway.total, { line: 222.5, over: '+100', under: '+100' });
});

test('malformed bookmakers in the recorded payload drop cleanly, valid ones kept', () => {
  const board = extractBoardGame(oddsPayload, game, FETCHED_AT);
  // Fixture carries 4 bookmakers: nameless (dropped) and "Broken Odds Co"
  // (only unreadable prices — dropped). Exactly the two valid ones remain.
  assert.deepEqual(
    board.lineSets.map((s) => s.bookmaker),
    ['Bet365', 'Betway']
  );
});

test('every published price is an American-format string', () => {
  const board = extractBoardGame(oddsPayload, game, FETCHED_AT);
  for (const set of board.lineSets) {
    const prices = [
      set.moneyline?.home, set.moneyline?.away,
      set.spread?.home, set.spread?.away,
      set.total?.over, set.total?.under,
    ].filter((p) => p != null);
    assert.ok(prices.length > 0);
    for (const p of prices) assert.match(p, /^[+-]\d+$/, `price shape: ${p}`);
  }
});

test('garbage payloads yield no board entry, never a throw', () => {
  for (const bad of [null, undefined, 42, 'x', {}, [], { response: [] }, { response: 'nope' }, { response: [null, 42] }]) {
    assert.equal(extractBoardGame(bad, game, FETCHED_AT), null);
    assert.deepEqual(extractBoardLineSets(bad, FETCHED_AT), []);
  }
});

test('no fetch time, no board — a price never publishes without its as-of (spec §8)', () => {
  for (const badTime of [null, undefined, '', 'not-a-date', 1792714500]) {
    assert.equal(extractBoardGame(oddsPayload, game, badTime), null);
  }
});

test('an unidentifiable game yields no board entry', () => {
  assert.equal(extractBoardGame(oddsPayload, null, FETCHED_AT), null);
  assert.equal(extractBoardGame(oddsPayload, { ...game, id: null }, FETCHED_AT), null);
  assert.equal(extractBoardGame(oddsPayload, { ...game, startTs: 0 }, FETCHED_AT), null);
  assert.equal(extractBoardGame(oddsPayload, { ...game, home: '' }, FETCHED_AT), null);
});

test('a market with one unreadable price drops alone — never repaired from elsewhere', () => {
  const payload = {
    response: [
      {
        bookmakers: [
          {
            id: 7,
            name: 'HalfBook',
            bets: [
              {
                id: 1,
                name: 'Match Winner',
                values: [
                  { value: 'Home', odd: '1.50' },
                  { value: 'Away', odd: 'garbage' },
                ],
              },
              {
                id: 3,
                name: 'Over/Under',
                values: [
                  { value: 'Over 200.5', odd: '2.00' },
                  { value: 'Under 200.5', odd: '2.00' },
                ],
              },
            ],
          },
        ],
      },
    ],
  };
  const [set] = extractBoardLineSets(payload, FETCHED_AT);
  assert.equal(set.bookmaker, 'HalfBook');
  assert.equal(set.moneyline, null, 'moneyline dropped, not patched');
  assert.deepEqual(set.total, { line: 200.5, over: '+100', under: '+100' });
});

test('spread sides at mismatched lines are not a line set', () => {
  const set = extractBoardLineSet(
    {
      id: 8,
      name: 'MismatchBook',
      bets: [
        {
          id: 2,
          name: 'Spread',
          values: [
            { value: 'Home -3.5', odd: '1.90' },
            { value: 'Away +4.5', odd: '1.90' },
          ],
        },
        {
          id: 1,
          name: 'Match Winner',
          values: [
            { value: 'Home', odd: '2.00' },
            { value: 'Away', odd: '2.00' },
          ],
        },
      ],
    },
    FETCHED_AT
  );
  assert.equal(set.spread, null);
  assert.deepEqual(set.moneyline, { home: '+100', away: '+100' });
});

test('a spread-only bookmaker still publishes (moneyline/total honestly null)', () => {
  const set = extractBoardLineSet(
    {
      id: 9,
      name: 'SpreadOnly',
      bets: [
        {
          id: 2,
          name: 'Spread',
          values: [
            { value: 'Home +2.5', odd: '1.80' },
            { value: 'Away -2.5', odd: '2.00' },
          ],
        },
      ],
    },
    FETCHED_AT
  );
  assert.equal(set.moneyline, null);
  assert.equal(set.total, null);
  // Home is the underdog here: the signed line follows the home side.
  assert.deepEqual(set.spread, { line: 2.5, home: '-125', away: '+100' });
});

test('malformed bookmaker shapes drop: no name, blank name, no bets, bets not a list', () => {
  const goodBet = {
    id: 1,
    name: 'Match Winner',
    values: [
      { value: 'Home', odd: '2.00' },
      { value: 'Away', odd: '2.00' },
    ],
  };
  assert.equal(extractBoardLineSet(null, FETCHED_AT), null);
  assert.equal(extractBoardLineSet('Bet365', FETCHED_AT), null);
  assert.equal(extractBoardLineSet({ id: 1, bets: [goodBet] }, FETCHED_AT), null);
  assert.equal(extractBoardLineSet({ id: 1, name: '   ', bets: [goodBet] }, FETCHED_AT), null);
  assert.equal(extractBoardLineSet({ id: 1, name: 'NoBets' }, FETCHED_AT), null);
  assert.equal(extractBoardLineSet({ id: 1, name: 'BadBets', bets: 'nope' }, FETCHED_AT), null);
  assert.equal(extractBoardLineSet({ id: 1, name: 'EmptyBets', bets: [] }, FETCHED_AT), null);
  assert.equal(
    extractBoardLineSet({ id: 1, name: 'JunkBets', bets: [null, 42, { values: 'nope' }] }, FETCHED_AT),
    null
  );
});

test('duplicate bookmaker names keep the first valid line set', () => {
  const mk = (odd) => ({
    id: 5,
    name: 'DupeBook',
    bets: [
      {
        id: 1,
        name: 'Match Winner',
        values: [
          { value: 'Home', odd },
          { value: 'Away', odd },
        ],
      },
    ],
  });
  const sets = extractBoardLineSets(
    { response: [{ bookmakers: [mk('2.00'), mk('1.50')] }] },
    FETCHED_AT
  );
  assert.equal(sets.length, 1);
  assert.deepEqual(sets[0].moneyline, { home: '+100', away: '+100' });
});

test('the unwrapped array form (what apiGet returns) extracts identically', () => {
  const fromObject = extractBoardGame(oddsPayload, game, FETCHED_AT);
  const fromArray = extractBoardGame(oddsPayload.response, game, FETCHED_AT);
  assert.deepEqual(fromArray, fromObject);
});
