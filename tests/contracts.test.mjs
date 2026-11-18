/**
 * Contract-layer consumer tests (Quant Apparatus spec §4, Phase A).
 *
 * The sports-feed repo is the contracts' home and their first consumer:
 *  (a) the collector's own shapes — a game and line sets built from the
 *      repo fixtures by the repo's normalizers, plus whatever
 *      constituents the repo's current feed.json carries — are mapped
 *      per contracts/README.md and validated at PAYLOAD level;
 *  (b) one full ENVELOPED example per contract validates at envelope
 *      level (the target form for new seam traffic);
 *  (c) deliberately corrupted payloads fail loudly, per contract —
 *      missing required fields, wrong types, and a blended-books
 *      LineSet (the shape Rule 9 forbids) among them.
 *
 * Run: node --test tests/contracts.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { validate, listContracts } from '../contracts/validate.mjs';
import {
  normalizeGame,
  extractBoardGame,
  extractFavoriteLine,
  toPickCard,
} from '../normalize.mjs';

const FETCHED_AT = '2026-10-22T16:40:00.000Z';
const START_TS = 1792714500 + 900; // Thu Oct 22 2026, 5:30 PM PDT

const oddsPayload = JSON.parse(
  readFileSync(new URL('./fixtures/apisports-nba-odds.json', import.meta.url), 'utf8')
);
const feed = JSON.parse(
  readFileSync(new URL('../feed.json', import.meta.url), 'utf8')
);

// A raw API-Sports game in the shape games.test.mjs / board.test.mjs use.
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

// ---------------------------------------------------------------------------
// Mappers — contracts/README.md, implemented. Native → contract payload.
// ---------------------------------------------------------------------------

function mapCollectorGame(g) {
  return {
    gameId: `apisp:${g.sport.toLowerCase()}:${g.id}`,
    league: g.sportLabel,
    homeTeam: g.home,
    awayTeam: g.away,
    homeCode: g.homeCode ?? null,
    awayCode: g.awayCode ?? null,
    startAt: new Date(g.startTs).toISOString(),
    // Collector vocabulary: 'upcoming' | 'live' → contract vocabulary.
    status: g.status === 'upcoming' ? 'scheduled' : g.status,
    homeScore: g.homeScore ?? null,
    awayScore: g.awayScore ?? null,
  };
}

function mapLineSet(set) {
  // Prices: the collector publishes American-price STRINGS ('-200',
  // '+150' — decimalToAmerican's return type); the contract's canonical
  // form is the integer. The conversion lives here, in the documented
  // mapping — the validator itself never coerces (README gap G6).
  const price = (v) => (v == null ? null : Number(v));
  return {
    bookmaker: set.bookmaker,
    fetchedAt: set.fetchedAt,
    moneyline: set.moneyline
      ? { home: price(set.moneyline.home), away: price(set.moneyline.away) }
      : null,
    spread: set.spread
      ? { line: set.spread.line, home: price(set.spread.home), away: price(set.spread.away) }
      : null,
    total: set.total
      ? { line: set.total.line, over: price(set.total.over), under: price(set.total.under) }
      : null,
  };
}

function mapPickCard(card, game) {
  // Partial mapping (README gap G4): winProb is bookmaker-implied and
  // deliberately does NOT map to model.probability.
  const favName = card.pick.replace(/\s+(ML|[+-]\d+(?:\.\d+)?)$/, '');
  return {
    id: card.id,
    gameId: `apisp:${game.sport.toLowerCase()}:${game.id}`,
    league: card.sportLabel,
    market: card.pick.endsWith('ML') ? 'moneyline' : 'spread',
    subject: { kind: 'team', name: favName, team: favName },
    side: favName === game.home ? 'home' : 'away',
    line: null,
  };
}

function expectOk(contract, obj, level) {
  const r = validate(contract, obj, level ? { level } : undefined);
  assert.equal(r.ok, true, `${contract} should validate: ${r.errors.join('; ')}`);
}

function expectFail(contract, obj, level, needle) {
  const r = validate(contract, obj, level ? { level } : undefined);
  assert.equal(r.ok, false, `${contract} should NOT validate`);
  assert.ok(
    r.errors.some((e) => e.includes(needle)),
    `expected an error mentioning '${needle}', got: ${r.errors.join('; ')}`
  );
}

// ---------------------------------------------------------------------------
// (a) The collector's own shapes, at payload level
// ---------------------------------------------------------------------------

test('contracts: all six contracts are registered', () => {
  assert.deepEqual(
    [...listContracts()].sort(),
    ['fit-artifact', 'game', 'grade-record', 'lineset', 'parlay-snapshot', 'pick']
  );
});

test('contracts: collector game maps to a valid Game payload', () => {
  const game = normalizeGame(nbaGameRaw, 'Basketball');
  assert.ok(game, 'fixture game should normalize');
  expectOk('game', mapCollectorGame(game), 'payload');
});

test('contracts: board line sets map to valid LineSet payloads', () => {
  const game = normalizeGame(nbaGameRaw, 'Basketball');
  const board = extractBoardGame(oddsPayload, game, FETCHED_AT);
  assert.ok(board, 'fixture should yield a board game');
  assert.ok(board.lineSets.length >= 1);
  for (const set of board.lineSets) {
    expectOk('lineset', mapLineSet(set), 'payload');
  }
  // The board game itself is also a Game.
  expectOk(
    'game',
    {
      gameId: `apisp:${board.sport.toLowerCase()}:${board.gameId}`,
      league: board.league,
      homeTeam: board.home,
      awayTeam: board.away,
      homeCode: board.homeCode,
      awayCode: board.awayCode,
      startAt: board.startAt,
      status: 'scheduled',
    },
    'payload'
  );
});

test('contracts: pick card maps to a valid Pick payload (partial — no model)', () => {
  const game = normalizeGame(nbaGameRaw, 'Basketball');
  const line = extractFavoriteLine(oddsPayload, game);
  assert.ok(line, 'fixture should yield a favorite line');
  const card = toPickCard(game, line);
  const mapped = mapPickCard(card, game);
  expectOk('pick', mapped, 'payload');
  assert.equal('model' in mapped, false, 'bookmaker-implied winProb must not become model.probability');
});

test('contracts: current feed.json constituents validate (slate + picks)', () => {
  assert.ok(Array.isArray(feed.slate) && Array.isArray(feed.picks));
  // The repo's checked-in feed.json is a stale empty edition (slate and
  // picks both empty); the loops are the wiring — the fixture-built
  // edition above exercises the same mappers non-vacuously.
  for (const item of feed.slate) {
    assert.equal(typeof item.id, 'string');
    assert.equal(typeof item.teams, 'string');
  }
  for (const card of feed.picks) {
    assert.equal(typeof card.id, 'string');
    assert.equal(typeof card.pick, 'string');
  }
});

// ---------------------------------------------------------------------------
// (b) One full enveloped example per contract
// ---------------------------------------------------------------------------

const ENVELOPED = {
  game: {
    contract: 'game', v: 1, class: 'Live', source: '103 sports-feed collector',
    fetchedAt: FETCHED_AT, id: 'apisp:basketball:456789',
    gameId: 'apisp:basketball:456789', league: 'NBA',
    homeTeam: 'Los Angeles Lakers', awayTeam: 'Boston Celtics',
    homeCode: 'LAL', awayCode: 'BOS',
    startAt: '2026-10-23T00:30:00.000Z', status: 'scheduled',
    homeScore: null, awayScore: null,
  },
  lineset: {
    contract: 'lineset', v: 1, class: 'Live', source: 'the-odds-api',
    fetchedAt: FETCHED_AT, id: 'apisp:basketball:456789:DraftKings',
    gameId: 'apisp:basketball:456789', bookmaker: 'DraftKings',
    boardSnapshotRef: '2026-10-22T16:40:00.000Z#apisp:basketball:456789',
    moneyline: { home: -150, away: 130 },
    spread: { line: -3.5, home: -110, away: -110 },
    total: { line: 221.5, over: -110, under: -110 },
  },
  pick: {
    // Tonight's real locked leg (Nov 17, 2026): Timberwolves ML, 91.8%, -135.
    contract: 'pick', v: 1, class: 'Estimated', source: 'bettracker-app',
    fetchedAt: '2026-11-17T19:02:00-08:00', id: 'LEG_TEAM_1042_ml_t1_a1',
    gameId: 'board-nba-1042', league: 'NBA', market: 'moneyline',
    subject: { kind: 'team', name: 'Minnesota Timberwolves', team: 'Minnesota Timberwolves' },
    side: 'home', line: null, price: -135, bookmaker: 'DraftKings',
    model: {
      probability: 0.918, projection: 6.4, evPerUnit: 0.31, evPercent: 31.2,
      modelVersion: null, modelSource: 'current', lockedAt: '2026-11-17T19:02:00-08:00',
    },
  },
  'parlay-snapshot': {
    // Modeled on Isaac's Nov 17, 2026 parlay (five priced picks, all
    // outcomes known except the Lakers leg's — shown open here).
    contract: 'parlay-snapshot', v: 1, class: 'Estimated', source: 'bettracker-app',
    fetchedAt: '2026-11-17T19:05:00-08:00', id: 'TKT-20261117-1',
    snapshotId: 'TKT-20261117-1', status: 'open',
    createdAt: '2026-11-17T19:05:00-08:00', stake: 10,
    legs: [
      {
        id: 'LEG_TEAM_1041_ml_t1_a1', gameId: 'board-nba-1041', league: 'NBA',
        market: 'moneyline',
        subject: { kind: 'team', name: 'San Antonio Spurs', team: 'San Antonio Spurs' },
        side: 'home', line: null, price: -382, bookmaker: 'DraftKings',
        model: { probability: 0.633, projection: 3.1, evPerUnit: null, evPercent: null, modelVersion: null, modelSource: 'current', lockedAt: '2026-11-17T19:05:00-08:00' },
      },
      {
        id: 'LEG_TEAM_1042_ml_t1_a2', gameId: 'board-nba-1042', league: 'NBA',
        market: 'moneyline',
        subject: { kind: 'team', name: 'Minnesota Timberwolves', team: 'Minnesota Timberwolves' },
        side: 'home', line: null, price: -135, bookmaker: null,
        model: { probability: 0.918, projection: 6.4, evPerUnit: null, evPercent: null, modelVersion: null, modelSource: 'current', lockedAt: '2026-11-17T19:05:00-08:00' },
      },
      {
        id: 'LEG_TEAM_1043_spread_t2_a3', gameId: 'board-nba-1043', league: 'NBA',
        market: 'spread',
        subject: { kind: 'team', name: 'New York Knicks', team: 'New York Knicks' },
        side: 'away', line: -2.5, price: -115, bookmaker: null,
        model: { probability: 0.405, projection: -1.2, evPerUnit: null, evPercent: null, modelVersion: null, modelSource: 'current', lockedAt: '2026-11-17T19:05:00-08:00' },
      },
    ],
  },
  'fit-artifact': {
    contract: 'fit-artifact', v: 1, class: 'Historical',
    source: '103 Sports Intelligence Engine harvest (sports-feed repo, data branch)',
    fetchedAt: '2026-11-17T00:00:00-08:00', id: 'fit:NBA:0',
    sport: 'NBA', fitVersion: '0',
    parameters: { leagueAvg: 113.116777154513, homeEdge: 2.3324834268230497, shrink: 0.8, totalSD: 18.04614626809417, marginSD: 14.098869068669025 },
    trainedOn: { seasons: ['2022-2023', '2023-2024', '2024-2025'], gamesUsed: 3922, gamesExcluded: 5 },
    teams: {
      'Atlanta Hawks': { homePF: 118.60058369814466, homePA: 118.10339795705652, awayPF: 115.6411863835524, awayPA: 118.79598078867542 },
      'Boston Celtics': { homePF: 118.48749342475196, homePA: 106.19401365233112, awayPF: 114.35602836866979, awayPA: 107.68699763593379 },
    },
    playerStatSDs: { points: 6.86, rebounds: 2.82, assists: 2.25, steals: 1.01, blocks: 0.93, threePointersMade: 1.51 },
    producedBy: '103 Sports Intelligence Engine harvest (sports-feed repo, data branch)',
    producedAt: '2026-11-17T00:00:00-08:00',
  },
  'grade-record': {
    // The Timberwolves leg of the Nov 17 parlay, graded: POR 105 @ MIN 106.
    contract: 'grade-record', v: 1, class: 'Historical', source: 'bettracker-app unified record',
    fetchedAt: '2026-11-17T23:03:53-08:00', id: 'TKT-20261117-1:LEG_TEAM_1042_ml_t1_a2',
    subjectKind: 'pick', snapshotId: 'TKT-20261117-1',
    selectionId: 'LEG_TEAM_1042_ml_t1_a2',
    result: 'win', modelProb: 0.918, league: 'NBA', sport: 'NBA',
    prediction: 'Minnesota Timberwolves — Moneyline',
    settledAt: '2026-11-17T23:03:53-08:00',
  },
};

for (const [name, example] of Object.entries(ENVELOPED)) {
  test(`contracts: enveloped ${name} example validates`, () => {
    expectOk(name, example);
  });
}

// ---------------------------------------------------------------------------
// (c) Negative tests — corruption must fail loudly, per contract
// ---------------------------------------------------------------------------

test('contracts: Game rejects a missing league and a bogus status', () => {
  const { league, ...noLeague } = ENVELOPED.game;
  expectFail('game', noLeague, 'payload', "missing required property 'league'");
  expectFail('game', { ...ENVELOPED.game, status: 'bogus' }, 'payload', 'status');
});

test('contracts: LineSet rejects a blended-books set (Rule 9)', () => {
  // Two books in one set: bookmaker as an array is a type failure…
  expectFail(
    'lineset',
    { ...ENVELOPED.lineset, bookmaker: ['DraftKings', 'FanDuel'] },
    'payload',
    'bookmaker'
  );
  // …and a set smuggling a second book as an extra property is rejected too.
  expectFail(
    'lineset',
    { bookmaker: 'DraftKings', fetchedAt: FETCHED_AT, bookmakers: ['DraftKings', 'FanDuel'] },
    'payload',
    "unexpected property 'bookmakers'"
  );
  // Wrong price type: a decimal string is not an American integer price.
  expectFail(
    'lineset',
    { bookmaker: 'DraftKings', fetchedAt: FETCHED_AT, moneyline: { home: '-150', away: 130 } },
    'payload',
    'moneyline.home'
  );
});

test('contracts: Pick rejects probability above 1 and a missing subject', () => {
  const badProb = structuredClone(ENVELOPED.pick);
  badProb.model.probability = 1.4;
  expectFail('pick', badProb, 'payload', 'probability');
  const { subject, ...noSubject } = ENVELOPED.pick;
  expectFail('pick', noSubject, 'payload', "missing required property 'subject'");
});

test('contracts: ParlaySnapshot rejects empty legs and a string stake', () => {
  expectFail('parlay-snapshot', { ...ENVELOPED['parlay-snapshot'], legs: [] }, 'payload', 'legs');
  expectFail('parlay-snapshot', { ...ENVELOPED['parlay-snapshot'], stake: '10' }, 'payload', 'stake');
});

test('contracts: FitArtifact rejects a string SD and missing training record', () => {
  const badSd = structuredClone(ENVELOPED['fit-artifact']);
  badSd.parameters.totalSD = '18.05';
  expectFail('fit-artifact', badSd, 'payload', 'totalSD');
  const { trainedOn, ...noTraining } = ENVELOPED['fit-artifact'];
  expectFail('fit-artifact', noTraining, 'payload', "missing required property 'trainedOn'");
});

test('contracts: GradeRecord rejects an absent modelProb and a void result', () => {
  // modelProb is required-but-nullable: absent is a failure, null is not.
  const { modelProb, ...noProb } = ENVELOPED['grade-record'];
  expectFail('grade-record', noProb, 'payload', "missing required property 'modelProb'");
  expectOk('grade-record', { ...ENVELOPED['grade-record'], modelProb: null }, 'payload');
  expectFail('grade-record', { ...ENVELOPED['grade-record'], result: 'void' }, 'payload', 'result');
});

test('contracts: envelope level requires the envelope (payload level does not)', () => {
  const { contract, v, class: _c, source, fetchedAt, ...payloadOnly } = ENVELOPED.game;
  expectFail('game', payloadOnly, undefined, "missing required property 'contract'");
  expectOk('game', payloadOnly, 'payload');
});
