/**
 * Gate tests for the BetStack slate fill (collector.mjs).
 *
 * The fill is gated OFF by default: collectFeed() invokes
 * collectBetstackSlate only when betstackFillEnabled() returns true,
 * which requires BETSTACK_FILL_ENABLED to be exactly "true". The flag
 * is unset in the sports-feed Vercel project, so the layer stays dark.
 * Gated on 2026-11-17 after a live probe showed the real BetStack
 * payload does not match betstack.mjs's expectations (the league
 * parameter does not filter; no scores are returned), which would have
 * published other leagues' fixtures mislabeled under the requested
 * sport. These tests pin the gate predicate so the fill cannot be
 * invoked — by this call site, its only one — without the exact flag.
 *
 * Run: node --test tests/betstack-gate.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { betstackFillEnabled } from '../collector.mjs';

test('fill is NOT enabled when BETSTACK_FILL_ENABLED is absent', () => {
  assert.equal(betstackFillEnabled({}), false);
  assert.equal(betstackFillEnabled({ BETSTACK_API_KEY: 'set' }), false);
});

test('fill is NOT enabled for truthy-but-inexact flag values', () => {
  for (const value of ['false', '1', 'yes', 'TRUE', 'True', ' true', 'true ', 'on']) {
    assert.equal(
      betstackFillEnabled({ BETSTACK_FILL_ENABLED: value }),
      false,
      `value ${JSON.stringify(value)} must not enable the fill`
    );
  }
});

test('fill is enabled only by the exact string "true"', () => {
  assert.equal(betstackFillEnabled({ BETSTACK_FILL_ENABLED: 'true' }), true);
});

test('default env source is process.env, which has no flag set in tests', () => {
  const saved = process.env.BETSTACK_FILL_ENABLED;
  delete process.env.BETSTACK_FILL_ENABLED;
  try {
    assert.equal(betstackFillEnabled(), false);
  } finally {
    if (saved !== undefined) process.env.BETSTACK_FILL_ENABLED = saved;
  }
});
