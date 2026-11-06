/**
 * Unit tests for the pure helpers in history.mjs (snapshot path shaping and
 * index merging). The Blob-writing path (persistFeedHistory) is exercised
 * only against the live store — it is fully guarded by design, and these
 * tests pin the deterministic logic it relies on.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  snapshotPathFor,
  mergeIndexEntries,
  MAX_INDEX_ENTRIES,
} from '../history.mjs';

test('snapshotPathFor: history/ prefix, timestamp sanitized for URLs/filesystems', () => {
  assert.equal(
    snapshotPathFor('2026-11-06T07:15:30.123Z'),
    'history/feed-2026-11-06T07-15-30-123Z.json'
  );
  assert.ok(!snapshotPathFor('2026-11-06T07:15:30.123Z').includes(':'));
});

test('mergeIndexEntries: newest first, entry lands at the head', () => {
  const existing = [
    { timestamp: '2026-11-06T05:45:00.000Z', path: 'history/feed-b.json', url: 'u-b' },
    { timestamp: '2026-11-06T04:15:00.000Z', path: 'history/feed-a.json', url: 'u-a' },
  ];
  const merged = mergeIndexEntries(existing, {
    timestamp: '2026-11-06T07:15:00.000Z',
    path: 'history/feed-c.json',
    url: 'u-c',
  });
  assert.deepEqual(
    merged.map((e) => e.path),
    ['history/feed-c.json', 'history/feed-b.json', 'history/feed-a.json']
  );
});

test('mergeIndexEntries: dedupes by path (same-millisecond re-run)', () => {
  const existing = [
    { timestamp: '2026-11-06T07:15:00.000Z', path: 'history/feed-c.json', url: 'old' },
  ];
  const merged = mergeIndexEntries(existing, {
    timestamp: '2026-11-06T07:15:00.000Z',
    path: 'history/feed-c.json',
    url: 'new',
  });
  assert.equal(merged.length, 1);
  assert.equal(merged[0].url, 'new');
});

test('mergeIndexEntries: caps at MAX_INDEX_ENTRIES, keeping the newest', () => {
  const existing = [];
  for (let i = 0; i < MAX_INDEX_ENTRIES + 10; i++) {
    const ts = new Date(Date.UTC(2026, 0, 1) + i * 60000).toISOString();
    existing.push({ timestamp: ts, path: `history/feed-${i}.json`, url: `u-${i}` });
  }
  const merged = mergeIndexEntries(existing, {
    timestamp: '2027-01-01T00:00:00.000Z',
    path: 'history/feed-newest.json',
    url: 'u-newest',
  });
  assert.equal(merged.length, MAX_INDEX_ENTRIES);
  assert.equal(merged[0].path, 'history/feed-newest.json');
});

test('mergeIndexEntries: tolerates a missing/garbage existing listing', () => {
  const entry = { timestamp: '2026-11-06T07:15:00.000Z', path: 'p', url: 'u' };
  assert.deepEqual(mergeIndexEntries(undefined, entry), [entry]);
  assert.deepEqual(mergeIndexEntries([null, { nope: true }, entry], entry), [
    entry,
  ]);
});
