import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { appendRecords, loadLedger, monthFileName } from '../grading/store.mjs';
import { makePickGrade } from '../grading/records.mjs';

function root() {
  return mkdtempSync(join(tmpdir(), 'ledger-'));
}

function grade(overrides = {}) {
  return makePickGrade({
    stream: 'app-model',
    recordClass: 'Estimated',
    sport: 'NBA',
    league: 'NBA',
    gameKey: 'NBA:1794960600000:away-team@home-team',
    snapshotId: '2026-11-17T21:00:17.363Z#NBA:1794960600000:away-team@home-team',
    selectionPart: 'ml-home',
    prediction: 'Home Team — Moneyline',
    modelProb: 0.8,
    result: 'win',
    settledAt: '2026-11-18T00:10:00.000Z',
    now: '2026-11-18T08:00:00.000Z',
    ...overrides,
  });
}

test('store: appends validated records to the append-month file', () => {
  const r = root();
  const out = appendRecords(r, [grade()], { now: new Date('2026-11-18T08:00:00Z') });
  assert.equal(out.appended, 1);
  const files = readdirSync(join(r, 'data', 'grades'));
  assert.deepEqual(files, ['2026-11.jsonl']);
  const lines = readFileSync(join(r, 'data', 'grades', '2026-11.jsonl'), 'utf8').trim().split('\n');
  assert.equal(lines.length, 1);
  const { records, lines: total } = loadLedger(r);
  assert.equal(total, 1);
  assert.equal(records.get(grade().id).modelProb, 0.8);
});

test('store: re-appending identical records is a no-op (idempotent nights)', () => {
  const r = root();
  appendRecords(r, [grade()], { now: new Date('2026-11-18T08:00:00Z') });
  const out = appendRecords(r, [grade()], { now: new Date('2026-11-19T08:00:00Z') });
  assert.equal(out.appended, 0);
  assert.equal(out.skipped, 1);
  assert.equal(loadLedger(r).lines, 1);
});

test('store: a rebuild with only a new fetchedAt is a duplicate, not a correction', () => {
  const r = root();
  appendRecords(r, [grade()], { now: new Date('2026-11-18T08:00:00Z') });
  const rebuilt = grade({ now: '2026-11-19T08:00:00.000Z' });
  assert.notEqual(rebuilt.fetchedAt, grade().fetchedAt);
  const out = appendRecords(r, [rebuilt], { now: new Date('2026-11-19T08:00:00Z') });
  assert.equal(out.appended, 0);
  assert.equal(out.skipped, 1);
  assert.equal(loadLedger(r).lines, 1);
});

test('store: a correction supersedes by id as a NEW line, never an edit', () => {
  const r = root();
  appendRecords(r, [grade()], { now: new Date('2026-11-18T08:00:00Z') });
  const corrected = grade({ modelProb: 0.75 });
  const out = appendRecords(r, [corrected], { now: new Date('2026-11-18T09:00:00Z') });
  assert.equal(out.appended, 1);
  assert.equal(out.superseded, 1);
  const { records, lines } = loadLedger(r);
  assert.equal(lines, 2);
  assert.equal(records.size, 1);
  assert.equal(records.get(grade().id).modelProb, 0.75);
});

test('store: a record missing modelProb entirely is rejected loudly, nothing written', () => {
  const r = root();
  const bad = grade();
  delete bad.modelProb;
  assert.throws(() => appendRecords(r, [bad]), /fails the contract/);
  assert.equal(loadLedger(r).lines, 0);
});

test('store: modelProb null is valid (the parlay honesty rule) — present but null', () => {
  const r = root();
  const parlayGrade = {
    contract: 'grade-record',
    v: 1,
    class: 'Historical',
    source: 'app-unified-record',
    fetchedAt: '2026-11-18T08:00:00.000Z',
    id: 'ticket-1',
    subjectKind: 'parlay',
    snapshotId: 'ticket-1',
    result: 'loss',
    modelProb: null,
    settledAt: '2026-11-18T04:03:53.000Z',
    sport: 'NBA',
    stake: 10,
    profit: -10,
  };
  const out = appendRecords(r, [parlayGrade]);
  assert.equal(out.appended, 1);
});

test('store: month file name is the UTC append month', () => {
  assert.equal(monthFileName(new Date('2026-01-05T23:00:00Z')), '2026-01.jsonl');
  assert.equal(monthFileName(new Date('2026-12-31T23:59:59Z')), '2026-12.jsonl');
});
