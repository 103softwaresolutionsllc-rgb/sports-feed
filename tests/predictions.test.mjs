import { test } from 'node:test';
import assert from 'node:assert/strict';
import { americanToInt, convertLineSet, extractPredictions, mergePredictionsIndexEntries, predictionsPathFor } from '../predictions.mjs';

const AGGREGATES = {
  summary: { seasons_held: ['2022-2023', '2023-2024', '2024-2025'] },
  weighted_averages: {
    'Home Team': { points_for_per_game: 114, points_against_per_game: 106 },
    'Away Team': { points_for_per_game: 104, points_against_per_game: 112 },
  },
};

function feed() {
  return {
    generatedAt: '2026-11-17T21:00:17.363Z',
    source: 'test collector',
    aggregates: { Basketball: AGGREGATES },
    board: [
      {
        id: 'apisp-board-Basketball-abc',
        gameId: 'abc',
        sport: 'Basketball',
        league: 'NBA',
        home: 'Home Team',
        away: 'Away Team',
        startTs: 1794960600000,
        startAt: '2026-11-18T00:10:00.000Z',
        lineSets: [
          {
            bookmaker: 'DraftKings',
            fetchedAt: '2026-11-17T12:00:16.644Z',
            moneyline: { home: '-200', away: '+170' },
            spread: { line: -5.5, home: '-110', away: '-110' },
            total: { line: 224.5, over: '-110', under: '-110' },
          },
        ],
      },
      {
        id: 'apisp-board-Basketball-def',
        gameId: 'def',
        sport: 'Basketball',
        league: 'NBA',
        home: 'Unknown Team',
        away: 'Away Team',
        startTs: 1794964200000,
        startAt: '2026-11-18T01:10:00.000Z',
        lineSets: [],
      },
    ],
    picks: [
      {
        id: 'apisp-pick-Basketball-123',
        sport: 'Basketball',
        sportLabel: 'NBA',
        teamA: 'Away Team',
        teamB: 'Home Team',
        pick: 'Home Team ML',
        winProb: 66,
        footer: 'DraftKings · Moneyline · Tomorrow 7:10 PM',
        startTs: 1794960600000,
      },
    ],
  };
}

test('predictions: american prices become contract integers at the seam (G6)', () => {
  assert.equal(americanToInt('-200'), -200);
  assert.equal(americanToInt('+170'), 170);
  assert.equal(americanToInt(-110), -110);
  assert.equal(americanToInt('ev'), null);
  const set = convertLineSet(feed().board[0].lineSets[0]);
  assert.equal(set.moneyline.home, -200);
  assert.equal(set.spread.line, -5.5);
});

test('predictions: model probabilities are archived from the edition aggregates', () => {
  const archive = extractPredictions(feed());
  assert.equal(archive.contract, 'prediction-archive');
  assert.equal(archive.boardSnapshotRef, '2026-11-17T21:00:17.363Z');
  const priced = archive.games[0];
  assert.ok(priced.model, 'model read present when aggregates cover both teams');
  assert.equal(priced.model.origin, 'model');
  assert.equal(priced.model.stream, 'app-model');
  assert.ok(priced.model.homeWinProb > 0.5 && priced.model.homeWinProb < 1);
  // The no-stats gate withholds the read for an uncovered team — archived
  // as null, never replaced with a league-average guess.
  assert.equal(archive.games[1].model, null);
  assert.equal(archive.games[1].aggregatesPresent, true);
});

test('predictions: pick cards are labeled book-implied and carry NO model probability (G4)', () => {
  const archive = extractPredictions(feed());
  assert.equal(archive.picks.length, 1);
  const card = archive.picks[0];
  assert.equal(card.origin, 'book-implied');
  assert.equal(card.stream, 'book-implied-pick-card');
  assert.equal(card.impliedProb, 0.66);
  assert.ok(!('modelProb' in card));
  assert.ok(!('probability' in card));
  assert.ok(!('model' in card));
});

test('predictions: path + index merge mirror the history conventions', () => {
  assert.equal(
    predictionsPathFor('2026-11-17T21:00:17.363Z'),
    'predictions/feed-2026-11-17T21-00-17-363Z.json'
  );
  const merged = mergePredictionsIndexEntries(
    [{ timestamp: '2026-11-17T21:00:17.363Z', path: 'predictions/a.json', url: 'u1' }],
    { timestamp: '2026-11-18T00:00:00.000Z', path: 'predictions/b.json', url: 'u2' }
  );
  assert.equal(merged.length, 2);
  assert.equal(merged[0].path, 'predictions/b.json'); // newest first
});
