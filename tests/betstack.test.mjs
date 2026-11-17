/**
 * Unit tests for the BetStack slate layer (betstack.mjs).
 *
 * Fixture honesty: tests/fixtures/betstack-nba-events.json is CONSTRUCTED
 * from the provider's published example payload (api.betstack.dev:
 * id / home_team / away_team / commence_time / result / lines) plus the
 * field spellings the SportsBettingGPT backend transform has carried
 * since it was wired there (dict-shaped teams, homeTeam/awayTeam,
 * event_id, line price spellings). It is NOT a live recording — the
 * owner's BetStack key lives in the backend's Railway env and was not
 * accessible at audit time (2026-11-17), so no live probe backs this
 * shape yet. The normalizer is deliberately defensive for that reason,
 * and the first live run after the key lands in the collector's env is
 * the real shape check. No test here makes a network call: fetch is
 * stubbed throughout.
 *
 * Run: node --test tests/betstack.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  BETSTACK_SPORTS,
  normalizeBetstackEvent,
  toBetstackSlateItem,
  collectBetstackSlate,
} from '../betstack.mjs';

const events = JSON.parse(
  readFileSync(new URL('./fixtures/betstack-nba-events.json', import.meta.url), 'utf8')
);
const byId = (id) => events.find((e) => (e.id ?? e.event_id) === id);
const HOUR = 60 * 60 * 1000;
const iso = (ts) => new Date(ts).toISOString();

test('upcoming event normalizes to the internal game shape', () => {
  const raw = { ...byId(91001), commence_time: iso(Date.now() + 3 * HOUR) };
  const g = normalizeBetstackEvent(raw, 'Basketball');
  assert.ok(g);
  assert.equal(g.home, 'New York Knicks');
  assert.equal(g.away, 'Boston Celtics');
  assert.equal(g.sportLabel, 'NBA');
  assert.equal(g.status, 'upcoming');
  assert.equal(g.homeScore, null);
  assert.equal(g.startTs, Date.parse(raw.commence_time));
});

test('live event carries scores and live status', () => {
  const raw = { ...byId(91002), commence_time: iso(Date.now() - 1 * HOUR) };
  const g = normalizeBetstackEvent(raw, 'Basketball');
  assert.ok(g);
  assert.equal(g.status, 'live');
  assert.equal(g.homeScore, 58);
  assert.equal(g.awayScore, 54);
});

test('finished games are excluded (results, not slate)', () => {
  assert.equal(normalizeBetstackEvent(byId(91003), 'Basketball'), null);
});

test('dict-shaped teams and camelCase spellings parse', () => {
  const dict = { ...byId(91004), commence_time: iso(Date.now() + 4 * HOUR) };
  const g1 = normalizeBetstackEvent(dict, 'Basketball');
  assert.equal(g1.home, 'Miami Heat');
  assert.equal(g1.away, 'Chicago Bulls');

  const camel = { ...byId(91006), commence_time: iso(Date.now() + 5 * HOUR) };
  const g2 = normalizeBetstackEvent(camel, 'Basketball');
  assert.equal(g2.home, 'Toronto Raptors');
  assert.equal(g2.away, 'Atlanta Hawks');
  assert.equal(g2.id, 91006);
});

test('an event with a missing team or unreadable start is dropped, never repaired', () => {
  assert.equal(normalizeBetstackEvent(byId(91005), 'Basketball'), null);
  const badTime = { ...byId(91001), commence_time: 'not-a-date' };
  assert.equal(normalizeBetstackEvent(badTime, 'Basketball'), null);
  assert.equal(normalizeBetstackEvent(byId(91001), 'Football'), null);
});

test('slate items carry BetStack provenance in their id', () => {
  const raw = { ...byId(91001), commence_time: iso(Date.now() + 3 * HOUR) };
  const item = toBetstackSlateItem(normalizeBetstackEvent(raw, 'Basketball'));
  assert.ok(item.id.startsWith('betstack-slate-Basketball-'), item.id);
  assert.equal(item.sportLabel, 'NBA');
  assert.equal(item.teams, 'Boston Celtics vs New York Knicks');
  assert.equal(item.status, 'upcoming');
});

function stubFetch(payload, calls) {
  return async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 200, json: async () => payload };
  };
}

test('collect fills a sport with one request, key in the header only', async () => {
  const calls = [];
  const payload = [
    { ...byId(91001), commence_time: iso(Date.now() + 3 * HOUR) },
    { ...byId(91003) },
  ];
  const { slate, errors } = await collectBetstackSlate({
    sports: ['Basketball'],
    apiKey: 'test-key',
    fetchImpl: stubFetch(payload, calls),
  });
  assert.deepEqual(errors, {});
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /league_key=basketball_nba/);
  assert.equal(calls[0].opts.headers['X-API-Key'], 'test-key');
  assert.equal(slate.length, 1);
  assert.ok(slate[0].id.startsWith('betstack-slate-'));
});

test('no key configured: honest per-sport errors, no request made', async () => {
  let called = 0;
  const { slate, errors } = await collectBetstackSlate({
    sports: ['Basketball', 'Hockey'],
    apiKey: '',
    fetchImpl: async () => {
      called += 1;
      throw new Error('must not be called');
    },
  });
  assert.equal(called, 0);
  assert.deepEqual(slate, []);
  assert.equal(errors.Basketball, 'not configured (no key)');
  assert.equal(errors.Hockey, 'not configured (no key)');
});

test('a failing sport degrades to an error entry and never throws', async () => {
  const { slate, errors } = await collectBetstackSlate({
    sports: ['Baseball'],
    apiKey: 'test-key',
    fetchImpl: async () => ({ ok: false, status: 500, json: async () => ({}) }),
  });
  assert.deepEqual(slate, []);
  assert.match(errors.Baseball, /HTTP 500/);
});

test('sports outside the fill set (Football) are ignored entirely', async () => {
  const { slate, errors } = await collectBetstackSlate({
    sports: ['Football'],
    apiKey: 'test-key',
    fetchImpl: stubFetch([], []),
  });
  assert.deepEqual(slate, []);
  assert.deepEqual(errors, {});
  assert.equal(BETSTACK_SPORTS.Football, undefined);
});
