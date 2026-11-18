import { test } from 'node:test';
import assert from 'node:assert/strict';
import { brierContribution, calibrationBuckets, summarize } from '../grading/math.mjs';
import { devigMoneyline, impliedFromAmerican, makePickGrade } from '../grading/records.mjs';

test('math: brier contributions for win / loss / push / missing', () => {
  assert.ok(Math.abs(brierContribution(0.8, 'win') - 0.04) < 1e-12);
  assert.ok(Math.abs(brierContribution(0.8, 'loss') - 0.64) < 1e-12);
  assert.ok(Math.abs(brierContribution(0.8, 'push') - 0.09) < 1e-12);
  assert.equal(brierContribution(null, 'win'), null);
});

test('math: implied probability and devigging', () => {
  assert.ok(Math.abs(impliedFromAmerican(-200) - 2 / 3) < 1e-12);
  assert.ok(Math.abs(impliedFromAmerican(150) - 0.4) < 1e-12);
  const devigged = devigMoneyline(-200, 170);
  const rawHome = 2 / 3;
  const rawAway = 100 / 270;
  assert.ok(Math.abs(devigged.home - rawHome / (rawHome + rawAway)) < 1e-12);
  assert.ok(Math.abs(devigged.home + devigged.away - 1) < 1e-12);
  assert.equal(devigMoneyline(null, 170), null);
});

function record(stream, prob, result, sport = 'NBA') {
  return makePickGrade({
    stream,
    recordClass: 'Live',
    sport,
    league: sport,
    gameKey: `${sport}:1:a@b`,
    snapshotId: 'snap#1',
    selectionPart: 'ml-home',
    prediction: 'B — Moneyline',
    modelProb: prob,
    result,
    settledAt: '2026-11-18T00:00:00.000Z',
    now: '2026-11-18T08:00:00.000Z',
  });
}

test('math: calibration folds to the favored side (spec buckets 0.5-1.0)', () => {
  // An underdog pick at 0.2 that lost: the favored side (0.8) won.
  const buckets = calibrationBuckets([record('app-model', 0.2, 'loss')]);
  assert.equal(buckets.length, 1);
  assert.equal(buckets[0].range, '0.8-0.9');
  assert.equal(buckets[0].actualRate, 1);
  assert.ok(Math.abs(buckets[0].meanPredicted - 0.8) < 1e-12);
  // A 0.95 favorite that lost is a calibration miss in the top bucket.
  const top = calibrationBuckets([record('app-model', 0.95, 'loss')]);
  assert.equal(top[0].range, '0.9-1.0');
  assert.equal(top[0].actualRate, 0);
});

test('math: pushes and prob-less records never enter Brier or calibration', () => {
  const summary = summarize([
    record('app-model', 0.8, 'push'),
    record('app-model', null, 'win'),
    record('app-model', 0.8, 'win'),
  ]);
  const nba = summary['app-model'].NBA;
  assert.equal(nba.n, 3);
  assert.equal(nba.pushes, 1);
  assert.equal(nba.probMissing, 1);
  assert.equal(nba.brierN, 2); // push (prob-carrying) + win; prob-less excluded
  assert.equal(nba.buckets.length, 1); // only the win folds into calibration
});

test('math: streams are summarized separately, never blended', () => {
  const summary = summarize([
    record('app-model', 0.9, 'win'),
    record('book-implied', 0.6, 'loss'),
  ]);
  assert.deepEqual(Object.keys(summary).sort(), ['app-model', 'book-implied']);
  assert.ok(Math.abs(summary['app-model'].NBA.brier - 0.01) < 1e-12);
  assert.ok(Math.abs(summary['book-implied'].NBA.brier - 0.36) < 1e-12);
});
