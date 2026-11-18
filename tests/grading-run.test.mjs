import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runGrading } from '../grading/run.mjs';
import { loadLedger } from '../grading/store.mjs';

const START = 1794960600000; // 2026-11-18T00:10:00.000Z

function fixtureRoot() {
  const root = mkdtempSync(join(tmpdir(), 'grading-run-'));
  const predictionsRoot = join(root, 'data', 'predictions');
  const resultsDir = join(root, 'data', 'grades', 'results');
  mkdirSync(predictionsRoot, { recursive: true });
  mkdirSync(resultsDir, { recursive: true });

  const archive = {
    contract: 'prediction-archive',
    v: 1,
    boardSnapshotRef: '2026-11-17T21:00:17.363Z',
    generatedAt: '2026-11-17T21:00:17.363Z',
    source: 'test',
    games: [
      {
        id: 'g1',
        gameId: 'g1',
        sport: 'Basketball',
        league: 'NBA',
        home: 'Home Team',
        away: 'Away Team',
        startAt: '2026-11-18T00:10:00.000Z',
        startTs: START,
        lineSets: [
          { bookmaker: 'DraftKings', fetchedAt: '2026-11-17T12:00:00.000Z', moneyline: { home: -200, away: 170 }, spread: null, total: null },
        ],
        aggregatesPresent: true,
        model: { origin: 'model', stream: 'app-model', homeWinProb: 0.8, awayWinProb: 0.2 },
      },
    ],
    picks: [
      {
        id: 'apisp-pick-Basketball-9',
        sport: 'Basketball',
        sportLabel: 'NBA',
        teamA: 'Other Away',
        teamB: 'Other Home',
        pick: 'Other Home ML',
        footer: 'DraftKings · Moneyline · Tomorrow 7:10 PM',
        startTs: START,
        winProb: 66,
        impliedProb: 0.66,
        origin: 'book-implied',
        stream: 'book-implied-pick-card',
      },
    ],
  };
  writeFileSync(join(predictionsRoot, 'feed-test.json'), JSON.stringify(archive));

  const results = {
    source: 'espn-fallback',
    sport: 'NBA',
    month: '2026-11',
    fetchedAt: '2026-11-19T08:00:00.000Z',
    games: [
      { sport: 'NBA', date: '2026-11-18T00:10:00.000Z', home: 'Home Team', away: 'Away Team', homeScore: 110, awayScore: 100 },
      { sport: 'NBA', date: '2026-11-18T00:10:00.000Z', home: 'Other Home', away: 'Other Away', homeScore: 90, awayScore: 95 },
    ],
  };
  writeFileSync(join(resultsDir, 'espn-NBA-2026-11.json'), JSON.stringify(results));
  return { root, predictionsRoot };
}

test('run: grades model, devigged-implied, and pick-card streams separately; re-run is a no-op', async () => {
  const { root, predictionsRoot } = fixtureRoot();
  const now = new Date('2026-11-19T08:00:00.000Z');
  const first = await runGrading({ root, predictionsRoot, now });
  assert.equal(first.appended, 3);
  assert.equal(first.gamesGraded, 1);
  assert.equal(first.cardsGraded, 1);

  const { records } = loadLedger(root);
  const byStream = {};
  for (const record of records.values()) byStream[record.source] = record;
  assert.deepEqual(Object.keys(byStream).sort(), ['app-model', 'book-implied', 'book-implied-pick-card']);
  assert.equal(byStream['app-model'].modelProb, 0.8);
  assert.equal(byStream['app-model'].result, 'win');
  // Devigged from -200/+170, NOT the raw 66.7% and NOT the model number.
  const rawHome = 200 / 300;
  const rawAway = 100 / 270;
  assert.ok(Math.abs(byStream['book-implied'].modelProb - rawHome / (rawHome + rawAway)) < 1e-12);
  // The card keeps its published raw implied 0.66 even though the favorite lost.
  assert.equal(byStream['book-implied-pick-card'].modelProb, 0.66);
  assert.equal(byStream['book-implied-pick-card'].result, 'loss');

  const report = JSON.parse(readFileSync(join(root, 'data', 'grades', 'reports', 'latest.json'), 'utf8'));
  assert.equal(report.recordCount, 3);
  assert.ok(report.streams['app-model'].sports.NBA.brier != null);
  assert.ok(existsSync(join(root, 'data', 'grades', 'reports', 'latest.md')));

  const second = await runGrading({ root, predictionsRoot, now });
  assert.equal(second.appended, 0);
  assert.equal(second.skippedDuplicates, 3);
  assert.equal(loadLedger(root).records.size, 3);
});

test('run: a game with no final yet stays pending and ungraded', async () => {
  const { root, predictionsRoot } = fixtureRoot();
  // Grade "before" the finals exist in the cache: move results aside by
  // using an earlier now and an empty results dir root variant.
  const emptyRoot = mkdtempSync(join(tmpdir(), 'grading-run-empty-'));
  const summary = await runGrading({ root: emptyRoot, predictionsRoot, now: new Date('2026-11-19T08:00:00.000Z') });
  assert.equal(summary.gamesPending, 1);
  assert.equal(summary.appended, 0);
});
