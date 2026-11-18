/**
 * Tests for the Notable Lines failover (Phase 5, 2026-11-17):
 * oddsapi.mjs's notablePicksFromBoard / boardEntryToPickCard and their
 * wiring in collector.mjs's collectFeed.
 *
 * The contract under test:
 *  - When the API-Sports picks pipeline yields zero picks (its pass
 *    gated off, erroring, or empty), the feed's `picks` are rebuilt
 *    from The Odds API board entries the collector already fetched —
 *    same toPickCard shape the app renders, PLUS an honest
 *    `source: 'the-odds-api'` on every card and
 *    `picksMeta.source: 'the-odds-api'` on the edition.
 *  - When API-Sports produces picks, the picks output is byte-for-byte
 *    what it was before the failover existed (no source field added,
 *    picksMeta.source 'api-sports') and the failover does not run.
 *  - Selection is deterministic: closest moneylines first per sport
 *    (smallest gap between the sides' implied probabilities), capped
 *    at 3 per sport, upcoming games within 24h only, API-Sports
 *    pipeline sports only (MMA stays board-only).
 *
 * The collectFeed tests stub global fetch (prior feed, The Odds API,
 * API-Sports, ESPN, aggregates) — no live calls, dummy keys only.
 *
 * Run: node --test tests/notable-lines-failover.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  americanToImpliedPct,
  boardEntryToPickCard,
  notablePicksFromBoard,
  oddsApiPayloadToBoard,
  NOTABLE_PICKS_SOURCE,
} from '../oddsapi.mjs';
import { collectFeed } from '../collector.mjs';

const FETCHED_AT = '2026-11-17T12:00:00.000Z';
const NOW_MS = Date.parse(FETCHED_AT);
const HOUR = 60 * 60 * 1000;

const nbaPayload = JSON.parse(
  readFileSync(new URL('./fixtures/oddsapi-nba-odds.json', import.meta.url), 'utf8')
);
const apiSportsOddsPayload = JSON.parse(
  readFileSync(new URL('./fixtures/apisports-nba-odds.json', import.meta.url), 'utf8')
);

// --- implied probability -----------------------------------------------------

test('americanToImpliedPct converts American prices to implied percentages', () => {
  assert.equal(americanToImpliedPct('-150'), 60);
  assert.equal(americanToImpliedPct('+130'), 43);
  assert.equal(americanToImpliedPct(-110), 52);
  assert.equal(americanToImpliedPct('+100'), 50);
  assert.equal(americanToImpliedPct(200), 33);
});

test('americanToImpliedPct rejects prices that cannot be American odds', () => {
  for (const bad of [0, '0', -50, 99, 'abc', '', null, undefined, NaN]) {
    assert.equal(americanToImpliedPct(bad), null, `price ${JSON.stringify(bad)}`);
  }
});

// --- single-card conversion ----------------------------------------------------

test('boardEntryToPickCard emits the toPickCard shape PLUS the Odds source label', () => {
  const { entries } = oddsApiPayloadToBoard(nbaPayload, 'Basketball', FETCHED_AT);
  const lakers = entries.find((e) => e.home === 'Los Angeles Lakers');
  const card = boardEntryToPickCard(lakers);
  assert.equal(card.source, 'the-odds-api');
  assert.equal(card.source, NOTABLE_PICKS_SOURCE);
  // Everything else is exactly the shape toPickCard has always published.
  assert.deepEqual(
    Object.keys(card).sort(),
    [
      'badge', 'badgeType', 'barColor', 'footer', 'hot', 'id', 'pick',
      'source', 'sport', 'sportLabel', 'startTs', 'teamA', 'teamB', 'winProb',
    ].sort()
  );
  assert.equal(card.id, `apisp-pick-Basketball-${lakers.gameId}`);
  assert.equal(card.sport, 'Basketball');
  assert.equal(card.sportLabel, 'NBA');
  assert.equal(card.teamA, 'Boston Celtics');
  assert.equal(card.teamB, 'Los Angeles Lakers');
  // DraftKings moneyline: Lakers -150 → 60% implied; spread -4.5 (home-signed).
  assert.equal(card.winProb, 60);
  assert.equal(card.pick, 'Los Angeles Lakers -4.5');
  assert.equal(card.badge, 'Bookmaker favorite');
  assert.ok(card.footer.startsWith('DraftKings · Spread + moneyline ·'));
});

test('boardEntryToPickCard: an exact pick’em goes to the home side (primary-path tie rule)', () => {
  const entry = syntheticEntry({
    id: 'tie1', home: 'Home Team', away: 'Away Team',
    startTs: NOW_MS + 2 * HOUR, mlHome: '-110', mlAway: '-110', spreadLine: -1.5,
  });
  const card = boardEntryToPickCard(entry);
  assert.equal(card.winProb, 52);
  assert.equal(card.pick, 'Home Team -1.5');
  assert.equal(card.badge, 'Close line');
});

test('boardEntryToPickCard: an away favorite gets its own (negated) spread line', () => {
  const entry = syntheticEntry({
    id: 'away1', home: 'Home Team', away: 'Away Team',
    startTs: NOW_MS + 2 * HOUR, mlHome: '+130', mlAway: '-150', spreadLine: 4.5,
  });
  const card = boardEntryToPickCard(entry);
  assert.equal(card.pick, 'Away Team -4.5');
  assert.equal(card.winProb, 60);
});

test('boardEntryToPickCard is null for cards the board cannot honestly support', () => {
  const base = syntheticEntry({
    id: 'x1', home: 'Home Team', away: 'Away Team',
    startTs: NOW_MS + 2 * HOUR, mlHome: '-110', mlAway: '-110', spreadLine: -1.5,
  });
  // MMA stays board-only — never a picks sport, on either path.
  assert.equal(boardEntryToPickCard({ ...base, sport: 'MMA', league: 'UFC' }), null);
  // No complete moneyline → no implied favorite → no card.
  assert.equal(
    boardEntryToPickCard({ ...base, lineSets: [{ ...base.lineSets[0], moneyline: null }] }),
    null
  );
  assert.equal(
    boardEntryToPickCard({
      ...base,
      lineSets: [{ ...base.lineSets[0], moneyline: { home: '-110', away: null } }],
    }),
    null
  );
  // No named bookmaker → the card's footer could not name its source book.
  assert.equal(
    boardEntryToPickCard({ ...base, lineSets: [{ ...base.lineSets[0], bookmaker: '  ' }] }),
    null
  );
  assert.equal(boardEntryToPickCard(null), null);
  assert.equal(boardEntryToPickCard({ ...base, startTs: 0 }), null);
});

// --- selection -----------------------------------------------------------------

function syntheticEntry({ id, sport = 'Basketball', league = 'NBA', home, away, startTs, mlHome, mlAway, spreadLine = null }) {
  return {
    id: `apisp-board-${sport}-${id}`,
    gameId: id,
    sport,
    league,
    home,
    away,
    homeCode: null,
    awayCode: null,
    startTs,
    startAt: new Date(startTs).toISOString(),
    lineSets: [
      {
        bookmaker: 'DraftKings',
        fetchedAt: FETCHED_AT,
        moneyline: mlHome == null && mlAway == null ? null : { home: mlHome, away: mlAway },
        spread: spreadLine == null ? null : { line: spreadLine, home: '-110', away: '-110' },
        total: null,
      },
    ],
  };
}

test('notablePicksFromBoard selects the closest lines per sport, capped at 3, in start order', () => {
  const entries = [
    syntheticEntry({ id: 'blowout', home: 'H1', away: 'A1', startTs: NOW_MS + 4 * HOUR, mlHome: '-300', mlAway: '+240' }),
    syntheticEntry({ id: 'mid', home: 'H2', away: 'A2', startTs: NOW_MS + 2 * HOUR, mlHome: '-150', mlAway: '+130' }),
    syntheticEntry({ id: 'close', home: 'H3', away: 'A3', startTs: NOW_MS + 3 * HOUR, mlHome: '-120', mlAway: '+100' }),
    syntheticEntry({ id: 'pickem', home: 'H4', away: 'A4', startTs: NOW_MS + 5 * HOUR, mlHome: '-110', mlAway: '-110' }),
  ];
  const cards = notablePicksFromBoard(entries, { nowMs: NOW_MS });
  // The -300 blowout (implied gap 46) loses the cap to the three closer
  // lines (gaps 0, 5, 17); the published order is by start time.
  assert.deepEqual(
    cards.map((c) => c.id),
    ['apisp-pick-Basketball-mid', 'apisp-pick-Basketball-close', 'apisp-pick-Basketball-pickem']
  );
  assert.ok(cards.every((c) => c.source === 'the-odds-api'));
});

test('notablePicksFromBoard excludes MMA, past games, games beyond 24h, and moneyline-less entries', () => {
  const entries = [
    syntheticEntry({ id: 'mma1', sport: 'MMA', league: 'UFC', home: 'F1', away: 'F2', startTs: NOW_MS + 2 * HOUR, mlHome: '-110', mlAway: '-110' }),
    syntheticEntry({ id: 'past', home: 'H', away: 'A', startTs: NOW_MS - HOUR, mlHome: '-110', mlAway: '-110' }),
    syntheticEntry({ id: 'far', home: 'H', away: 'A', startTs: NOW_MS + 25 * HOUR, mlHome: '-110', mlAway: '-110' }),
    syntheticEntry({ id: 'noml', home: 'H', away: 'A', startTs: NOW_MS + 2 * HOUR, mlHome: null, mlAway: null }),
    syntheticEntry({ id: 'halfml', home: 'H', away: 'A', startTs: NOW_MS + 2 * HOUR, mlHome: '-110', mlAway: null }),
    syntheticEntry({ id: 'good', home: 'H', away: 'A', startTs: NOW_MS + 2 * HOUR, mlHome: '-110', mlAway: '-110' }),
  ];
  const cards = notablePicksFromBoard(entries, { nowMs: NOW_MS });
  assert.deepEqual(cards.map((c) => c.id), ['apisp-pick-Basketball-good']);
  assert.deepEqual(notablePicksFromBoard([], { nowMs: NOW_MS }), []);
  assert.deepEqual(notablePicksFromBoard(null, { nowMs: NOW_MS }), []);
});

test('notablePicksFromBoard is deterministic: same board, same cards, regardless of input order', () => {
  const entries = [
    syntheticEntry({ id: 'b', home: 'H2', away: 'A2', startTs: NOW_MS + 3 * HOUR, mlHome: '-120', mlAway: '+100' }),
    syntheticEntry({ id: 'a', home: 'H1', away: 'A1', startTs: NOW_MS + 2 * HOUR, mlHome: '-120', mlAway: '+100' }),
  ];
  const forward = notablePicksFromBoard(entries, { nowMs: NOW_MS });
  const reversed = notablePicksFromBoard([...entries].reverse(), { nowMs: NOW_MS });
  assert.deepEqual(reversed.map((c) => c.id), forward.map((c) => c.id));
});

// --- collectFeed wiring (stubbed fetch, dummy keys) ----------------------------

function stubResponse({ ok = true, status = 200, jsonData = null, headers = {} } = {}) {
  const lower = Object.fromEntries(
    Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v])
  );
  return {
    ok,
    status,
    headers: { get: (name) => lower[String(name).toLowerCase()] ?? null },
    json: async () => jsonData,
    text: async () => '',
  };
}

function oddsGame({ id, home, away, startMs, mlHome, mlAway, spreadLine, totalLine }) {
  return {
    id,
    sport_key: 'basketball_nba',
    sport_title: 'NBA',
    commence_time: new Date(startMs).toISOString(),
    home_team: home,
    away_team: away,
    bookmakers: [
      {
        key: 'draftkings',
        title: 'DraftKings',
        markets: [
          { key: 'h2h', outcomes: [
            { name: home, price: mlHome }, { name: away, price: mlAway } ] },
          { key: 'spreads', outcomes: [
            { name: home, point: spreadLine, price: -110 },
            { name: away, point: -spreadLine, price: -110 } ] },
          { key: 'totals', outcomes: [
            { name: 'Over', point: totalLine, price: -110 },
            { name: 'Under', point: totalLine, price: -110 } ] },
        ],
      },
    ],
  };
}

function localDateStr(offsetDays) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const ENV_KEYS = [
  'APISPORTS_COLLECTOR_ENABLED', 'APISPORTS_KEY', 'APISPORTS_BASKETBALL_KEY',
  'APISPORTS_FOOTBALL_KEY', 'APISPORTS_BASEBALL_KEY', 'APISPORTS_HOCKEY_KEY',
  'ODDS_API_KEY', 'FEED_PRIOR_URL', 'BETSTACK_FILL_ENABLED', 'BETSTACK_API_KEY',
];

/**
 * Run collectFeed() against a fully stubbed fetch. `oddsPayload` is
 * served for the NBA Odds API sweep (other sports sweep empty);
 * `apiSportsGames` (raw games, Basketball) + the recorded API-Sports
 * odds fixture are served when the API-Sports pass runs.
 */
async function runStubbedCollect({ env, oddsPayload, apiSportsGames = [] }) {
  const savedEnv = {};
  for (const k of ENV_KEYS) savedEnv[k] = process.env[k];
  const savedFetch = globalThis.fetch;
  const calls = { apiSports: 0, oddsApi: 0 };
  for (const k of ENV_KEYS) delete process.env[k];
  Object.assign(process.env, env);
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.startsWith('https://prior-feed.invalid/')) {
      return stubResponse({ ok: false, status: 404 });
    }
    if (u.startsWith('https://api.the-odds-api.com/')) {
      calls.oddsApi += 1;
      const games = u.includes('basketball_nba') ? oddsPayload : [];
      return stubResponse({
        jsonData: games,
        headers: { 'x-requests-remaining': '480', 'x-requests-used': '20' },
      });
    }
    if (u.includes('api-sports.io')) {
      calls.apiSports += 1;
      if (u.includes('v1.basketball.api-sports.io')) {
        if (u.includes('/odds?')) return stubResponse({ jsonData: apiSportsOddsPayload });
        if (u.includes('/games?')) {
          const date = new URL(u).searchParams.get('date');
          const games = date === localDateStr(0) ? apiSportsGames : [];
          return stubResponse({ jsonData: { errors: [], response: games } });
        }
      }
      return stubResponse({ jsonData: { errors: [], response: [] } });
    }
    // ESPN news + aggregates documents: fail — both layers are guarded
    // and degrade to empty sections, never blocking the feed.
    return stubResponse({ ok: false, status: 503 });
  };
  try {
    const feed = await collectFeed();
    return { feed, calls };
  } finally {
    globalThis.fetch = savedFetch;
    for (const k of ENV_KEYS) {
      if (savedEnv[k] === undefined) delete process.env[k];
      else process.env[k] = savedEnv[k];
    }
  }
}

test('collectFeed failover: API-Sports gated off → picks rebuilt from the Odds board, labeled the-odds-api', async () => {
  const now = Date.now();
  const oddsPayload = [
    oddsGame({ id: 'odds-gsw', home: 'Golden State Warriors', away: 'Denver Nuggets', startMs: now + 2 * HOUR, mlHome: -110, mlAway: -110, spreadLine: -1.5, totalLine: 220.5 }),
    oddsGame({ id: 'odds-lal', home: 'Los Angeles Lakers', away: 'Boston Celtics', startMs: now + 3 * HOUR, mlHome: -150, mlAway: 130, spreadLine: -4.5, totalLine: 225.5 }),
  ];
  const { feed, calls } = await runStubbedCollect({
    env: {
      // Dummy keys only; the API-Sports gate stays OFF (flag absent),
      // so the pass must not fire even though a key is present.
      APISPORTS_KEY: 'dummy-apisports-key',
      ODDS_API_KEY: 'dummy-odds-key',
      FEED_PRIOR_URL: 'https://prior-feed.invalid/feed.json',
    },
    oddsPayload,
  });
  assert.equal(calls.apiSports, 0, 'gated API-Sports pass made no calls');
  assert.ok(calls.oddsApi > 0, 'Odds API sweep ran');
  assert.deepEqual(feed.sportsConfigured, []);
  assert.equal(feed.board.length, 2, 'board is full from the Odds sweep');
  // The featured matchup is picks[0]: the earliest-starting selected card.
  assert.equal(feed.picks.length, 2);
  assert.deepEqual(feed.picksMeta, { source: 'the-odds-api', count: 2 });
  assert.ok(feed.source.includes('The Odds API (board odds + notable lines)'));
  const [first, second] = feed.picks;
  assert.equal(first.teamB, 'Golden State Warriors');
  assert.equal(first.winProb, 52);
  assert.equal(first.badge, 'Close line');
  assert.equal(first.pick, 'Golden State Warriors -1.5');
  assert.equal(second.teamB, 'Los Angeles Lakers');
  assert.equal(second.winProb, 60);
  assert.equal(second.pick, 'Los Angeles Lakers -4.5');
  for (const card of feed.picks) {
    assert.equal(card.source, 'the-odds-api', 'every fallback card names its actual source');
  }
});

test('collectFeed primary: API-Sports picks present → output unchanged, failover does not run', async () => {
  const now = Date.now();
  const startSec = Math.floor((now + 2 * HOUR) / 1000);
  const nbaGame = {
    id: 456789,
    date: new Date(startSec * 1000).toISOString(),
    time: '00:30',
    timestamp: startSec,
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
  const { feed, calls } = await runStubbedCollect({
    env: {
      APISPORTS_COLLECTOR_ENABLED: 'true',
      APISPORTS_KEY: 'dummy-apisports-key',
      ODDS_API_KEY: 'dummy-odds-key',
      FEED_PRIOR_URL: 'https://prior-feed.invalid/feed.json',
    },
    // The Odds board is ALSO full this run — the failover must still
    // not touch picks: one edition, one picks source.
    oddsPayload: [
      oddsGame({ id: 'odds-gsw', home: 'Golden State Warriors', away: 'Denver Nuggets', startMs: now + 2 * HOUR, mlHome: -110, mlAway: -110, spreadLine: -1.5, totalLine: 220.5 }),
    ],
    apiSportsGames: [nbaGame],
  });
  assert.ok(calls.apiSports > 0, 'API-Sports pass ran');
  assert.equal(feed.picks.length, 1);
  assert.deepEqual(feed.picksMeta, { source: 'api-sports', count: 1 });
  assert.ok(!feed.source.includes('notable lines'), 'feed source string unchanged on the primary path');
  const [card] = feed.picks;
  // Byte-for-byte the pre-failover toPickCard shape: NO source field.
  assert.deepEqual(
    Object.keys(card).sort(),
    [
      'badge', 'badgeType', 'barColor', 'footer', 'hot', 'id', 'pick',
      'sport', 'sportLabel', 'startTs', 'teamA', 'teamB', 'winProb',
    ].sort()
  );
  // Bet365 fixture: Lakers 1.50 decimal → 67% implied, spread -5.5.
  assert.equal(card.id, 'apisp-pick-Basketball-456789');
  assert.equal(card.winProb, 67);
  assert.equal(card.badge, 'Strong favorite');
  assert.equal(card.pick, 'Los Angeles Lakers -5.5');
});
