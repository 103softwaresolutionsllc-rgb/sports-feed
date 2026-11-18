/**
 * Phase E (spec §8) — the collector's source-health board.
 *
 * Pins: the emitted record's shape (contract-valid), gate-state
 * truthfulness (a gated source reports gated + its reason), the
 * BetStack not-a-link representation, the archive counters seam, and
 * the never-break-the-feed guarantee — including a simulated Blob
 * failure proving the edition still publishes.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  HEALTH_PATH,
  buildCollectorHealth,
  persistFeedHealth,
  picksSourceOf,
} from '../health.mjs';
import { validate } from '../contracts/validate.mjs';

/** A production-shaped edition: gates off, Odds layer carried,
 *  pick cards from the failover — the Nov 18, 2026 live shape. */
function productionFeed() {
  return {
    generatedAt: '2026-11-18T09:00:15.871Z',
    source: '103 sports-feed collector — test',
    sportsConfigured: [],
    picks: [{ id: 'odds-pick-1' }, { id: 'odds-pick-2' }],
    picksMeta: { source: 'the-odds-api', count: 2 },
    slate: [{ id: 's1' }, { id: 's2' }, { id: 's3' }],
    board: [{ id: 'b1' }, { id: 'b2' }],
    boardMeta: {
      source: 'the-odds-api',
      status: 'carried',
      creditsRemaining: 490,
      creditsUsed: 10,
      oddsApiFetchedAt: '2026-11-17T13:30:01.802Z',
      oddsApiSportsFetchedAt: {},
      oddsApiGameIds: ['b1', 'b2'],
    },
    aggregates: { Basketball: {}, Football: {}, Baseball: {}, Hockey: {} },
    aggregatesErrors: {},
    betstackErrors: {},
    apiSportsErrors: {},
    news: {
      fetchedAt: '2026-11-18T09:00:15.429Z',
      Basketball: { articles: [{ title: 'x' }] },
      Football: { articles: [] },
    },
    errors: {},
  };
}

const GATES_OFF = {}; // neither APISPORTS_COLLECTOR_ENABLED nor BETSTACK_FILL_ENABLED set
const NOW = new Date('2026-11-18T09:00:20.000Z');
const ARCHIVE_OK = { ok: true, games: 62, modelPriced: 41, pickCards: 2 };

function entry(record, name) {
  return record.sources.find((s) => s.name === name);
}

test('health: emitted record validates against the source-health contract (envelope)', () => {
  const record = buildCollectorHealth({ feed: productionFeed(), archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const check = validate('source-health', record);
  assert.deepEqual(check.errors, []);
  assert.equal(check.ok, true);
  assert.equal(record.publisher, 'collector');
  assert.equal(record.generatedAt, '2026-11-18T09:00:15.871Z');
});

test('health: gated-off API-Sports reports gated-off + the suspension reason, and made no attempt', () => {
  const record = buildCollectorHealth({ feed: productionFeed(), archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const api = entry(record, 'api-sports');
  assert.equal(api.status, 'gated-off');
  assert.equal(api.link, false);
  assert.match(api.reason, /suspended/i);
  assert.match(api.reason, /Legion runner/);
  // The gate means no call was made — the board must not invent one.
  assert.equal(api.lastAttemptAt, null);
  assert.equal(api.lastGoodAt, null);
});

test('health: with the gate ON and a clean run, API-Sports reports ok (gate state is read, not assumed)', () => {
  const feed = productionFeed();
  feed.sportsConfigured = ['Basketball'];
  feed.picksMeta = { source: 'api-sports', count: 2 };
  const record = buildCollectorHealth({
    feed,
    archive: ARCHIVE_OK,
    env: { APISPORTS_COLLECTOR_ENABLED: 'true' },
    now: NOW,
  });
  const api = entry(record, 'api-sports');
  assert.equal(api.status, 'ok');
  assert.equal(api.link, true);
  assert.equal(api.lastGoodAt, feed.generatedAt);
  assert.equal(api.lastAttemptAt, feed.generatedAt);
  assert.equal(record.seams['pick-cards'].counts.source, 'api-sports');
});

test('health: BetStack is represented as not-a-link, never as a healthy standby', () => {
  const record = buildCollectorHealth({ feed: productionFeed(), archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const bs = entry(record, 'betstack');
  assert.equal(bs.status, 'not-a-link');
  assert.equal(bs.link, false);
  assert.match(bs.reason, /Not a link/);
  assert.match(bs.reason, /identical unfiltered/);
});

test('health: Odds layer reports its quota and carried state truthfully', () => {
  const record = buildCollectorHealth({ feed: productionFeed(), archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const odds = entry(record, 'the-odds-api');
  assert.equal(odds.status, 'ok');
  assert.equal(odds.link, true);
  assert.equal(odds.quota.creditsRemaining, 490);
  assert.equal(odds.quota.creditsUsed, 10);
  assert.equal(odds.quota.floor, 50);
  assert.equal(odds.lastGoodAt, '2026-11-17T13:30:01.802Z');
  // 'carried' means no sweep was attempted this tick.
  assert.equal(odds.lastAttemptAt, null);
  assert.match(odds.detail, /Notable Lines failover/);
});

test('health: low-credit skip is degraded with the floor named; missing key is down', () => {
  const low = productionFeed();
  low.boardMeta = { ...low.boardMeta, status: 'skipped-low-credits', creditsRemaining: 12 };
  const lowRecord = buildCollectorHealth({ feed: low, archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const lowOdds = entry(lowRecord, 'the-odds-api');
  assert.equal(lowOdds.status, 'degraded');
  assert.match(lowOdds.reason, /12 credits remaining/);
  assert.match(lowOdds.reason, /50-credit floor/);

  const noKey = productionFeed();
  noKey.boardMeta = { ...noKey.boardMeta, status: 'not-configured', creditsRemaining: null };
  const noKeyRecord = buildCollectorHealth({ feed: noKey, archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const noKeyOdds = entry(noKeyRecord, 'the-odds-api');
  assert.equal(noKeyOdds.status, 'down');
  assert.equal(noKeyOdds.link, false);
});

test('health: pick-cards seam names the source that actually produced the cards', () => {
  const record = buildCollectorHealth({ feed: productionFeed(), archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const seam = record.seams['pick-cards'];
  assert.equal(seam.status, 'ok');
  assert.deepEqual(seam.counts, { source: 'the-odds-api', cards: 2 });
  assert.match(seam.detail, /Notable Lines failover/);
});

test('health: an edition predating picksMeta is read honestly (no source, degraded seam)', () => {
  const feed = productionFeed();
  delete feed.picksMeta;
  feed.picks = [];
  assert.equal(picksSourceOf(feed), 'none');
  const record = buildCollectorHealth({ feed, archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  const seam = record.seams['pick-cards'];
  assert.equal(seam.status, 'degraded');
  assert.equal(seam.counts.source, 'none');
  assert.match(seam.detail, /predates picksMeta/);
});

test('health: archive seam carries the Phase B counters; an archive failure degrades it', () => {
  const ok = buildCollectorHealth({ feed: productionFeed(), archive: ARCHIVE_OK, env: GATES_OFF, now: NOW });
  assert.equal(ok.seams['prediction-archive'].status, 'ok');
  assert.deepEqual(ok.seams['prediction-archive'].counts, { games: 62, modelPriced: 41, pickCards: 2 });

  const bad = buildCollectorHealth({
    feed: productionFeed(),
    archive: { ok: false, error: 'blob store unavailable' },
    env: GATES_OFF,
    now: NOW,
  });
  assert.equal(bad.seams['prediction-archive'].status, 'degraded');
  assert.match(bad.seams['prediction-archive'].detail, /blob store unavailable/);

  const absent = buildCollectorHealth({ feed: productionFeed(), env: GATES_OFF, now: NOW });
  assert.equal(absent.seams['prediction-archive'].status, 'not-observed');
});

test('health: persistFeedHealth writes health.json beside feed.json when the store works', async () => {
  const writes = [];
  const result = await persistFeedHealth(productionFeed(), {
    archive: ARCHIVE_OK,
    env: GATES_OFF,
    now: NOW,
    putImpl: async (path, body) => {
      writes.push({ path, body });
      return { url: 'https://blob.example/health.json' };
    },
  });
  assert.equal(result.published, true);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, HEALTH_PATH);
  const written = JSON.parse(writes[0].body);
  assert.equal(validate('source-health', written).ok, true);
});

test('health: NEVER-BREAK-THE-FEED — a Blob failure during health publish cannot fail the edition', async () => {
  // Simulate api/collect.mjs's publish sequence: the feed write
  // succeeds first; then the health publish's store blows up. The
  // sequence must complete normally — the edition is published.
  const order = [];
  const fakeFeedPublish = async () => {
    order.push('feed.json');
    return { url: 'https://blob.example/feed.json' };
  };
  await fakeFeedPublish();
  const result = await persistFeedHealth(productionFeed(), {
    archive: ARCHIVE_OK,
    env: GATES_OFF,
    now: NOW,
    putImpl: async () => {
      throw new Error('Blob store exploded');
    },
  });
  order.push('response-200');
  assert.equal(result.published, false);
  assert.match(result.error, /Blob store exploded/);
  assert.deepEqual(order, ['feed.json', 'response-200']);
});

test('health: persistFeedHealth never throws, even on a malformed feed', async () => {
  const result = await persistFeedHealth(null, {
    env: GATES_OFF,
    now: NOW,
    putImpl: async () => ({ url: 'x' }),
  });
  assert.equal(typeof result.published, 'boolean');
});

test('health: the collector handler publishes health AFTER the feed write, guarded like its siblings', () => {
  const src = readFileSync(new URL('../api/collect.mjs', import.meta.url), 'utf8');
  const feedWrite = src.indexOf("put('feed.json'");
  const healthCall = src.indexOf('await persistFeedHealth');
  assert.ok(feedWrite >= 0 && healthCall > feedWrite, 'health publish must follow the feed write');
  assert.match(src, /await persistFeedPredictions\(feed\)/);
});
