import { test } from 'node:test';
import assert from 'node:assert/strict';
import { finalsFromEspnScoreboard } from '../grading/fetch-results.mjs';
import { finalsFromApiSportsPayload, findResult, normalizeTeamName } from '../grading/results.mjs';

test('results: team-name canonicalization folds the known spelling drift', () => {
  assert.equal(normalizeTeamName('St. Louis Blues'), normalizeTeamName('St Louis Blues'));
  assert.equal(normalizeTeamName('St.Louis Cardinals'), normalizeTeamName('St. Louis Cardinals'));
  assert.equal(normalizeTeamName('LA Clippers'), normalizeTeamName('Los Angeles Clippers'));
  assert.equal(normalizeTeamName('Utah Hockey Club'), normalizeTeamName('Utah Mammoth'));
  assert.notEqual(normalizeTeamName('Boston Bruins'), normalizeTeamName('Boston Celtics'));
});

test('results: ESPN scoreboard extraction keeps completed games only', () => {
  const payload = {
    events: [
      {
        date: '2026-11-18T00:10:00Z',
        status: { type: { completed: true } },
        competitions: [
          {
            competitors: [
              { homeAway: 'home', score: '105', team: { displayName: 'St. Louis Blues' } },
              { homeAway: 'away', score: '3', team: { displayName: 'Anaheim Ducks' } },
            ],
          },
        ],
      },
      {
        date: '2026-11-19T00:10:00Z',
        status: { type: { completed: false } },
        competitions: [
          {
            competitors: [
              { homeAway: 'home', score: '0', team: { displayName: 'A' } },
              { homeAway: 'away', score: '0', team: { displayName: 'B' } },
            ],
          },
        ],
      },
    ],
  };
  const finals = finalsFromEspnScoreboard(payload, 'NHL');
  assert.equal(finals.length, 1);
  assert.equal(finals[0].homeScore, 105);
  assert.equal(finals[0].awayScore, 3);
});

test('results: harvested API-Sports payloads yield finals, skipping unfinished games', () => {
  const payload = {
    response: [
      {
        date: '2024-11-01T00:00:00Z',
        status: { short: 'FT' },
        teams: { home: { name: 'Boston Celtics' }, away: { name: 'Miami Heat' } },
        scores: { home: { total: 110 }, away: { total: 100 } },
      },
      {
        date: '2024-11-02T00:00:00Z',
        status: { short: 'NS' },
        teams: { home: { name: 'Boston Celtics' }, away: { name: 'Miami Heat' } },
        scores: { home: { total: null }, away: { total: null } },
      },
    ],
  };
  const finals = finalsFromApiSportsPayload(payload, 'NBA');
  assert.equal(finals.length, 1);
  assert.equal(finals[0].date, '2024-11-01');
});

test('results: matching is exact-date first, then a ±1 day window on the same teams', () => {
  const byKey = new Map();
  const byPair = new Map();
  const result = {
    sport: 'NHL',
    date: '2026-11-17',
    home: 'St. Louis Blues',
    away: 'Anaheim Ducks',
    homeScore: 4,
    awayScore: 3,
    source: 'espn-fallback',
  };
  // Rebuild the indexes the way loadResults does.
  const { normalizeTeamName: norm } = { normalizeTeamName };
  byKey.set(`NHL|2026-11-17|${norm(result.home)}|${norm(result.away)}`, result);
  byPair.set(`NHL|${norm(result.home)}|${norm(result.away)}`, [result]);
  const index = { byKey, byPair };
  const exact = findResult(index, { sport: 'NHL', startAt: '2026-11-17T20:00:00.000Z', home: 'St Louis Blues', away: 'Anaheim Ducks' });
  assert.equal(exact.homeScore, 4);
  assert.equal(exact.dateShifted, false);
  const shifted = findResult(index, { sport: 'NHL', startAt: '2026-11-18T01:10:00.000Z', home: 'St Louis Blues', away: 'Anaheim Ducks' });
  assert.equal(shifted.homeScore, 4);
  assert.equal(shifted.dateShifted, true);
  const none = findResult(index, { sport: 'NHL', startAt: '2026-11-25T01:10:00.000Z', home: 'St Louis Blues', away: 'Anaheim Ducks' });
  assert.equal(none, null);
});
