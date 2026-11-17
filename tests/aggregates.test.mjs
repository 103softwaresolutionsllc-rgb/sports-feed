/**
 * Unit tests for aggregates.mjs — publishing the Intelligence Engine
 * aggregates into feed.json's `aggregates` section (BetTracker sportsbook
 * remake follow-up: the app's boardService passes this section through to
 * boardEngineInputs, which reads `weighted_averages` per team and
 * `summary.seasons_held` for provenance).
 *
 * The fixture (tests/fixtures/intelligence-aggregates-basketball.json) is
 * an EXCERPT of the real document the daily harvest publishes on this
 * repo's `data` branch at data/intelligence/aggregates/basketball.json —
 * the summary verbatim and two teams' weighted_averages verbatim (Boston
 * Celtics, Miami Heat), read from the published document on 2026-11-16.
 * The summary's `teams: 38` is the Engine's own count for the full
 * document and is carried verbatim, as the publisher must.
 *
 * All transport is injected (fake fetch / temp local directory): no live
 * calls, no provider requests.
 *
 * Run: node --test tests/aggregates.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  AGGREGATES_SPORT_MAP,
  DEFAULT_AGGREGATES_BASE_URL,
  sanitizeAggregatesDoc,
  collectAggregates,
} from '../aggregates.mjs';

const fixtureDoc = JSON.parse(
  readFileSync(
    new URL('./fixtures/intelligence-aggregates-basketball.json', import.meta.url),
    'utf8'
  )
);

const EMPTY_BUT_HELD_DOC = {
  summary: {
    sport: 'hockey',
    classification: 'Historical',
    seasons_held: ['2022-2023', '2023-2024', '2024-2025'],
    season_count: 3,
    label: '3-season average',
    label_detail: 'Averages cover the 3 held season(s): 2022-2023, 2023-2024, 2024-2025.',
    teams: 0,
  },
  weighted_averages: {},
};

function okJsonResponse(doc, { lastModified = null } = {}) {
  return {
    ok: true,
    status: 200,
    headers: { get: (name) => (name === 'last-modified' ? lastModified : null) },
    json: async () => doc,
  };
}

// ---------------------------------------------------------------- sanitize

test('sanitize: fixture doc keeps the exact shape boardEngineInputs consumes', () => {
  const doc = sanitizeAggregatesDoc(fixtureDoc);
  assert.ok(doc);
  // weighted_averages: { team: { metric: value } } with the scoring pair
  // (and home/away splits) the engine inputs read.
  const celtics = doc.weighted_averages['Boston Celtics'];
  assert.equal(celtics.points_for_per_game, 116.612);
  assert.equal(celtics.points_against_per_game, 107.519);
  assert.equal(celtics.home_points_for_per_game, 118.319);
  assert.equal(celtics.away_points_against_per_game, 108.101);
  assert.equal(doc.weighted_averages['Miami Heat'].points_for_per_game, 109.714);
  // Only the consumed sections are republished — no leaderboards/averages.
  assert.deepEqual(Object.keys(doc).sort(), ['summary', 'weighted_averages']);
});

test('sanitize: provenance summary is carried verbatim (seasons held, label, classification)', () => {
  const doc = sanitizeAggregatesDoc(fixtureDoc);
  assert.deepEqual(doc.summary, fixtureDoc.summary);
  assert.deepEqual(doc.summary.seasons_held, ['2022-2023', '2023-2024', '2024-2025']);
  assert.equal(doc.summary.classification, 'Historical');
  assert.equal(doc.summary.label, '3-season average');
  assert.equal(doc.summary.season_count, 3);
});

test('sanitize: valid doc with empty weighted_averages publishes as-is (gate honesty)', () => {
  const doc = sanitizeAggregatesDoc(EMPTY_BUT_HELD_DOC);
  assert.ok(doc);
  assert.deepEqual(doc.weighted_averages, {});
  assert.deepEqual(doc.summary.seasons_held, ['2022-2023', '2023-2024', '2024-2025']);
});

test('sanitize: malformed documents are rejected whole, never repaired', () => {
  assert.equal(sanitizeAggregatesDoc(null), null);
  assert.equal(sanitizeAggregatesDoc([]), null);
  assert.equal(sanitizeAggregatesDoc('doc'), null);
  assert.equal(sanitizeAggregatesDoc({ weighted_averages: {} }), null); // no summary
  assert.equal(sanitizeAggregatesDoc({ summary: {} }), null); // no weighted_averages
  assert.equal(sanitizeAggregatesDoc({ summary: {}, weighted_averages: [] }), null);
});

test('sanitize: non-numeric metrics and non-object team records are dropped, values never invented', () => {
  const doc = sanitizeAggregatesDoc({
    summary: { sport: 'basketball', seasons_held: ['2024-2025', 42, null] },
    weighted_averages: {
      'Real Team': { points_for_per_game: 110.5, bogus: 'lots', missing: null },
      'Ghost Team': 'not-a-record',
    },
  });
  assert.deepEqual(doc.weighted_averages, {
    'Real Team': { points_for_per_game: 110.5 },
  });
  assert.deepEqual(doc.summary.seasons_held, ['2024-2025']);
});

test('sanitize: data_as_of comes from the source timestamp or the doc generated_at, else is omitted', () => {
  const fromSource = sanitizeAggregatesDoc(fixtureDoc, {
    dataAsOf: 'Mon, 16 Nov 2026 07:00:00 GMT',
  });
  assert.equal(fromSource.data_as_of, '2026-11-16T07:00:00.000Z');

  const fromDoc = sanitizeAggregatesDoc({ ...fixtureDoc, generated_at: '2026-11-15T06:23:00Z' });
  assert.equal(fromDoc.data_as_of, '2026-11-15T06:23:00.000Z');

  const neither = sanitizeAggregatesDoc(fixtureDoc, { dataAsOf: 'not a date' });
  assert.ok(!('data_as_of' in neither));
});

// ----------------------------------------------------------------- collect

test('collect: remote source maps display sports to Engine file sports and keys output by display name', async () => {
  const requested = [];
  const fetchFn = async (url) => {
    requested.push(url);
    if (url.endsWith('/basketball.json')) {
      return okJsonResponse(fixtureDoc, { lastModified: 'Mon, 16 Nov 2026 07:00:00 GMT' });
    }
    if (url.endsWith('/hockey.json')) return okJsonResponse(EMPTY_BUT_HELD_DOC);
    return okJsonResponse({ ...fixtureDoc, summary: { ...fixtureDoc.summary, sport: 'x' } });
  };
  const { aggregates, errors } = await collectAggregates({ env: {}, fetchFn });
  assert.deepEqual(errors, {});
  assert.deepEqual(Object.keys(aggregates).sort(), ['Baseball', 'Basketball', 'Football', 'Hockey']);
  // Football reads american-football.json (the Engine's file sport), etc.
  assert.ok(requested.includes(`${DEFAULT_AGGREGATES_BASE_URL}/american-football.json`));
  assert.ok(requested.includes(`${DEFAULT_AGGREGATES_BASE_URL}/basketball.json`));
  assert.ok(requested.includes(`${DEFAULT_AGGREGATES_BASE_URL}/baseball.json`));
  assert.ok(requested.includes(`${DEFAULT_AGGREGATES_BASE_URL}/hockey.json`));
  // Provenance survives the trip: seasons held + Last-Modified as-of.
  assert.deepEqual(aggregates.Basketball.summary.seasons_held, ['2022-2023', '2023-2024', '2024-2025']);
  assert.equal(aggregates.Basketball.data_as_of, '2026-11-16T07:00:00.000Z');
  assert.deepEqual(aggregates.Hockey.weighted_averages, {});
});

test('collect: INTELLIGENCE_AGGREGATES_BASE_URL overrides the default base (router env parity)', async () => {
  const requested = [];
  const fetchFn = async (url) => {
    requested.push(url);
    return okJsonResponse(fixtureDoc);
  };
  await collectAggregates({
    env: { INTELLIGENCE_AGGREGATES_BASE_URL: 'https://example.test/agg/' },
    fetchFn,
  });
  assert.ok(requested.length > 0);
  assert.ok(requested.every((u) => u.startsWith('https://example.test/agg/')));
});

test('collect: a failed sport is omitted and named in errors; other sports still publish; never throws', async () => {
  const fetchFn = async (url) => {
    if (url.endsWith('/baseball.json')) return { ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) };
    if (url.endsWith('/hockey.json')) throw new Error('network down');
    return okJsonResponse(fixtureDoc);
  };
  const { aggregates, errors } = await collectAggregates({ env: {}, fetchFn });
  assert.deepEqual(Object.keys(aggregates).sort(), ['Basketball', 'Football']);
  assert.match(errors.Baseball, /404/);
  assert.match(errors.Hockey, /network down/);
});

test('collect: a malformed remote document is omitted with an error, not published', async () => {
  const fetchFn = async (url) =>
    url.endsWith('/basketball.json')
      ? okJsonResponse({ nope: true })
      : okJsonResponse(fixtureDoc);
  const { aggregates, errors } = await collectAggregates({ env: {}, fetchFn });
  assert.ok(!('Basketball' in aggregates));
  assert.match(errors.Basketball, /malformed/);
});

test('collect: local INTELLIGENCE_DATA_DIR wins per sport; remote fills the rest (router precedence)', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'agg-data-'));
  try {
    mkdirSync(join(dir, 'aggregates'), { recursive: true });
    writeFileSync(join(dir, 'aggregates', 'basketball.json'), JSON.stringify(fixtureDoc));
    const fetched = [];
    const fetchFn = async (url) => {
      fetched.push(url);
      return okJsonResponse(EMPTY_BUT_HELD_DOC);
    };
    const { aggregates, errors } = await collectAggregates({
      env: { INTELLIGENCE_DATA_DIR: dir },
      fetchFn,
    });
    assert.deepEqual(errors, {});
    // Basketball came from disk (real fixture values), not the remote stub.
    assert.equal(aggregates.Basketball.weighted_averages['Boston Celtics'].points_for_per_game, 116.612);
    assert.ok(!fetched.some((u) => u.endsWith('/basketball.json')));
    assert.ok(fetched.some((u) => u.endsWith('/hockey.json')));
    // Local as-of is the file write time — present and parseable.
    assert.ok(Number.isFinite(Date.parse(aggregates.Basketball.data_as_of)));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('sport map covers exactly the collector board sports', () => {
  assert.deepEqual(AGGREGATES_SPORT_MAP, {
    Basketball: 'basketball',
    Football: 'american-football',
    Baseball: 'baseball',
    Hockey: 'hockey',
  });
});
