/**
 * Unit tests for the API-Sports game normalizers + season resolver in
 * normalize.mjs.
 *
 * The Football fixture uses the American Football API's documented
 * /games envelope: id/date/status nested under `game` (with date as a
 * { timezone, date, time, timestamp } object), teams/scores top-level.
 * It reproduces the live 2026-10-22 bug: Patriots at Bears, Thursday
 * Night Football, Oct 22 2026 5:15 PM PDT — the old flat-only parsing
 * produced startTs 0, slate id "apisp-slate-Football-undefined", and the
 * epoch rendered as detail "Wed, Dec 31" / time "4:00 PM".
 *
 * Run: node --test tests/games.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  seasonForDate,
  gameStatus,
  normalizeGame,
  toSlateItem,
} from '../normalize.mjs';

// Thu Oct 22 2026, 5:15 PM PDT = 2026-10-23T00:15:00Z.
const TNF_TIMESTAMP = 1792714500;

const nflGame = {
  game: {
    id: 987654,
    stage: 'Regular Season',
    week: 'Week 7',
    date: {
      timezone: 'UTC',
      date: '2026-10-23',
      time: '00:15',
      timestamp: TNF_TIMESTAMP,
    },
    venue: { name: 'Soldier Field', city: 'Chicago' },
    status: { short: 'NS', long: 'Not Started', timer: null },
  },
  league: { id: 1, name: 'NFL', season: '2026' },
  country: { name: 'USA' },
  teams: {
    home: { id: 10, name: 'Chicago Bears', logo: 'https://example.com/chi.png' },
    away: { id: 20, name: 'New England Patriots', logo: 'https://example.com/ne.png' },
  },
  scores: {
    home: { quarter_1: null, quarter_2: null, quarter_3: null, quarter_4: null, overtime: null, total: null },
    away: { quarter_1: null, quarter_2: null, quarter_3: null, quarter_4: null, overtime: null, total: null },
  },
};

test('football envelope: id, timestamp and status read from the `game` object', () => {
  const g = normalizeGame(nflGame, 'Football');
  assert.ok(g, 'game normalizes');
  assert.equal(g.id, 987654);
  assert.equal(g.startTs, TNF_TIMESTAMP * 1000);
  assert.equal(g.status, 'upcoming');
  assert.equal(g.statusLong, 'Not Started');
  assert.equal(g.home, 'Chicago Bears');
  assert.equal(g.away, 'New England Patriots');
});

test('football envelope: slate item shows the real date/time, never the epoch', () => {
  const item = toSlateItem(normalizeGame(nflGame, 'Football'));
  assert.equal(item.id, 'apisp-slate-Football-987654');
  assert.equal(item.teams, 'New England Patriots vs Chicago Bears');
  assert.equal(item.time, '5:15 PM');
  assert.notEqual(item.detail, 'Wed, Dec 31');
  assert.equal(item.startTs, TNF_TIMESTAMP * 1000);
});

test('football envelope: nested FT status excludes the game (finished)', () => {
  const finished = {
    ...nflGame,
    game: { ...nflGame.game, status: { short: 'FT', long: 'Finished', timer: null } },
  };
  assert.equal(gameStatus(finished), 'finished');
  assert.equal(normalizeGame(finished, 'Football'), null);
});

test('football envelope: nested live status and scores are read', () => {
  const live = {
    ...nflGame,
    game: { ...nflGame.game, status: { short: 'Q3', long: '3rd Quarter', timer: null } },
    scores: {
      home: { quarter_1: 7, quarter_2: 3, quarter_3: 7, quarter_4: null, overtime: null, total: 17 },
      away: { quarter_1: 10, quarter_2: 7, quarter_3: 7, quarter_4: null, overtime: null, total: 24 },
    },
  };
  const g = normalizeGame(live, 'Football');
  assert.equal(g.status, 'live');
  assert.equal(g.homeScore, 17);
  assert.equal(g.awayScore, 24);
  assert.ok(toSlateItem(g).time.startsWith('Live'));
});

test('flat envelope (basketball) still normalizes from top-level fields', () => {
  const nbaGame = {
    id: 456789,
    date: '2026-10-23T00:30:00+00:00',
    time: '00:30',
    timestamp: TNF_TIMESTAMP + 900,
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
  const g = normalizeGame(nbaGame, 'Basketball');
  assert.ok(g, 'game normalizes');
  assert.equal(g.id, 456789);
  assert.equal(g.startTs, (TNF_TIMESTAMP + 900) * 1000);
  assert.equal(g.homeCode, 'LAL');
  assert.equal(g.awayCode, 'BOS');
});

test('a game with no readable start time is dropped, never epoch-dated', () => {
  const dateless = {
    id: 111,
    status: { short: 'NS', long: 'Not Started' },
    teams: { home: { name: 'A Team' }, away: { name: 'B Team' } },
    scores: { home: {}, away: {} },
  };
  assert.equal(normalizeGame(dateless, 'Basketball'), null);
});

test('seasonForDate: span sports (NBA/NHL) start in October', () => {
  for (const sport of ['Basketball', 'Hockey']) {
    assert.equal(seasonForDate(sport, new Date(2026, 9, 22)), '2026-2027', `${sport} Oct 2026`);
    assert.equal(seasonForDate(sport, new Date(2026, 9, 1)), '2026-2027', `${sport} Oct 1 boundary`);
    assert.equal(seasonForDate(sport, new Date(2026, 8, 30)), '2025-2026', `${sport} Sep 30 boundary`);
    assert.equal(seasonForDate(sport, new Date(2027, 3, 15)), '2026-2027', `${sport} Apr 2027 playoffs`);
    assert.equal(seasonForDate(sport, new Date(2027, 5, 10)), '2026-2027', `${sport} Jun 2027 finals`);
    assert.equal(seasonForDate(sport, new Date(2027, 0, 1)), '2026-2027', `${sport} Jan 2027`);
  }
});

test('seasonForDate: baseball is the calendar year, October included', () => {
  assert.equal(seasonForDate('Baseball', new Date(2026, 9, 22)), '2026', 'World Series month');
  assert.equal(seasonForDate('Baseball', new Date(2026, 3, 1)), '2026', 'Opening Day');
  assert.equal(seasonForDate('Baseball', new Date(2026, 11, 31)), '2026');
  assert.equal(seasonForDate('Baseball', new Date(2027, 0, 15)), '2027');
});

test('seasonForDate: football takes no season; bad input yields null', () => {
  assert.equal(seasonForDate('Football', new Date(2026, 9, 22)), null);
  assert.equal(seasonForDate('NoSuchSport', new Date(2026, 9, 22)), null);
  assert.equal(seasonForDate('Basketball', new Date('garbage')), null);
  assert.equal(seasonForDate('Basketball', null), null);
  assert.equal(seasonForDate('Basketball', '2026-10-22'), null);
});
