/**
 * Gate tests for the API-Sports collector pass (collector.mjs).
 *
 * The pass is gated OFF by default: collectFeed() invokes the
 * API-Sports pass only when apiSportsCollectorEnabled() returns true,
 * which requires APISPORTS_COLLECTOR_ENABLED to be exactly "true".
 * The flag is unset in the sports-feed Vercel project, so the pass
 * stays dark. Gated on 2026-11-17 after API-Sports suspended the
 * account over shared cloud egress IPs (support-confirmed): Vercel
 * Functions egress from a shared pool, and a single API call from a
 * shared address can re-trigger the suspension. API-Sports data
 * enters the system only via the dedicated-IP Legion harvest runner.
 * These tests pin the gate predicate so the pass cannot be invoked —
 * by this call site, its only one — without the exact flag.
 *
 * Run: node --test tests/apisports-gate.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiSportsCollectorEnabled } from '../collector.mjs';

test('pass is NOT enabled when APISPORTS_COLLECTOR_ENABLED is absent', () => {
  assert.equal(apiSportsCollectorEnabled({}), false);
  assert.equal(apiSportsCollectorEnabled({ APISPORTS_KEY: 'set' }), false);
});

test('pass is NOT enabled for truthy-but-inexact flag values', () => {
  for (const value of ['false', '1', 'yes', 'TRUE', 'True', ' true', 'true ', 'on']) {
    assert.equal(
      apiSportsCollectorEnabled({ APISPORTS_COLLECTOR_ENABLED: value }),
      false,
      `value ${JSON.stringify(value)} must not enable the pass`
    );
  }
});

test('pass is enabled only by the exact string "true"', () => {
  assert.equal(apiSportsCollectorEnabled({ APISPORTS_COLLECTOR_ENABLED: 'true' }), true);
});

test('default env source is process.env, which has no flag set in tests', () => {
  const saved = process.env.APISPORTS_COLLECTOR_ENABLED;
  delete process.env.APISPORTS_COLLECTOR_ENABLED;
  try {
    assert.equal(apiSportsCollectorEnabled(), false);
  } finally {
    if (saved !== undefined) process.env.APISPORTS_COLLECTOR_ENABLED = saved;
  }
});
