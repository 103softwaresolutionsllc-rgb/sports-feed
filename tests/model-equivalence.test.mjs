import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

/**
 * Equivalence proof for the canonical model port (model/PROVENANCE.md):
 * the ported modules and the BetTracker app's originals must return
 * identical outputs on identical inputs. The originals are copied to a
 * temp dir with their extensionless relative imports rewritten for
 * Node ESM (the only difference the port itself carries), then both
 * implementations are exercised over the app's own recorded board
 * fixture and a grid of engine inputs.
 *
 * The Monte Carlo panel is excluded from numeric equality: it draws
 * from Math.random by design in BOTH implementations. Equivalence
 * covers the deterministic path the ledger grades — buildGameModel,
 * buildPriceCells, computePreGameModel.
 */

const APP_SRC = new URL('../../bettracker-app/src/', import.meta.url).pathname;
const PORT = new URL('../model/', import.meta.url).pathname;

const FILES = [
  'utils/leagueConfig.js',
  'utils/predictFlowEngine.js',
  'utils/monteCarloSim.js',
  'services/board/boardEngineInputs.js',
  'services/board/boardModel.js',
];

function rewriteSpecifiers(code) {
  return code.replace(/(from\s+')(\.[^']*?)(')/g, (m, pre, spec, post) =>
    spec.endsWith('.js') || spec.endsWith('.json') ? m : `${pre}${spec}.js${post}`
  );
}

function loadOriginals() {
  const dir = mkdtempSync(join(tmpdir(), 'app-model-originals-'));
  for (const rel of FILES) {
    const dest = join(dir, rel);
    const src = readFileSync(join(APP_SRC, rel), 'utf8');
    const out = rewriteSpecifiers(src);
    cpSync(join(APP_SRC, rel), dest, { force: true });
    writeFileSync(dest, out, 'utf8');
  }
  return dir;
}

function numericLineSet(set) {
  const num = (v) => (typeof v === 'string' ? parseInt(v, 10) : v);
  return {
    bookmaker: set.bookmaker,
    fetchedAt: set.fetchedAt,
    moneyline: set.moneyline ? { home: num(set.moneyline.home), away: num(set.moneyline.away) } : null,
    spread: set.spread ? { line: set.spread.line, home: num(set.spread.home), away: num(set.spread.away) } : null,
    total: set.total ? { line: set.total.line, over: num(set.total.over), under: num(set.total.under) } : null,
  };
}

test('model port: identical outputs to the app originals', async () => {
  const dir = loadOriginals();
  const origBoard = await import(pathToFileURL(join(dir, 'services/board/boardModel.js')).href);
  const origEngine = await import(pathToFileURL(join(dir, 'utils/predictFlowEngine.js')).href);
  const portBoard = await import(pathToFileURL(join(PORT, 'services/board/boardModel.js')).href);
  const portEngine = await import(pathToFileURL(join(PORT, 'utils/predictFlowEngine.js')).href);

  // computePreGameModel over a grid of plausible team inputs.
  const t1 = { name: 'A', ppg: 114, oppPpg: 106, form: 55, rest: 2, injury: 0 };
  const t2 = { name: 'B', ppg: 104, oppPpg: 112, form: 45, rest: 1, injury: 1 };
  for (const sport of ['Basketball', 'Football', 'Baseball', 'Hockey']) {
    for (const homeAway of ['home', 'away', 'neutral']) {
      assert.deepEqual(
        portEngine.computePreGameModel(t1, t2, homeAway, sport),
        origEngine.computePreGameModel(t1, t2, homeAway, sport)
      );
    }
  }

  // buildGameModel + buildPriceCells over the app's recorded fixture.
  const fixture = JSON.parse(readFileSync(join(APP_SRC, 'services/board/data/boardFixture.json'), 'utf8'));
  const entry = fixture.board[0];
  const doc = fixture.aggregates[entry.sport];
  const game = {
    id: entry.id,
    gameId: entry.gameId,
    sport: entry.sport,
    league: entry.league,
    homeTeam: entry.home,
    awayTeam: entry.away,
    startTime: entry.startAt,
    startTs: entry.startTs,
  };
  const extras = { home: { restDays: 2 }, away: { restDays: 3 } };
  const origModel = origBoard.buildGameModel(game, doc, extras);
  const portModel = portBoard.buildGameModel(game, doc, extras);
  assert.ok(origModel, 'fixture game must produce a model read');
  assert.deepEqual(portModel, origModel);

  const lineSet = numericLineSet(entry.lineSets[0]);
  assert.deepEqual(
    portBoard.buildPriceCells(game, lineSet, portModel),
    origBoard.buildPriceCells(game, lineSet, origModel)
  );

  // The gate agrees too: an uncovered matchup is null in both.
  const gated = { ...game, homeTeam: 'No Such Team' };
  assert.equal(portBoard.buildGameModel(gated, doc, extras), null);
  assert.equal(origBoard.buildGameModel(gated, doc, extras), null);

  // Helpers.
  assert.equal(portBoard.fairAmericanPrice(0.918), origBoard.fairAmericanPrice(0.918));
  const schedule = [game, { ...game, id: 'x', startTime: '2026-11-15T00:30:00.000Z' }];
  assert.equal(
    portBoard.restDaysFromSchedule(schedule, game.homeTeam, game.startTime),
    origBoard.restDaysFromSchedule(schedule, game.homeTeam, game.startTime)
  );
});
