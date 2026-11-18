/**
 * Phase E (spec §8) — the nightly source-health rollup.
 *
 * Pins: the rollup validates against the SourceHealth contract; the
 * harvest's serving source is read from the stored payloads (not
 * assumed); the fan-out's absence and the sim service's undeployed
 * state are reported as what they are; fit versions/ages come from
 * the Phase C manifest; and the flags fire on evidence (>48h stale
 * grades, chain on its last link, standing not-ok conditions).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  buildHealthRollup,
  healthFlagsFor,
  renderSourceHealthSection,
  writeHealthRollup,
} from '../grading/health-rollup.mjs';
import { validate } from '../contracts/validate.mjs';

const NOW = new Date('2026-11-19T08:00:00.000Z');

function writeJson(path, doc) {
  mkdirSync(join(path, '..'), { recursive: true });
  writeFileSync(path, JSON.stringify(doc, null, 2));
}

function fixtureRoot() {
  const root = mkdtempSync(join(tmpdir(), 'health-rollup-'));
  // Fits manifest (Phase C shape).
  writeJson(join(root, 'data', 'fits', 'manifest.json'), {
    contract: 'fit-manifest',
    v: 1,
    sports: {
      NBA: { fitVersion: '1.4144.20261118', publishedAt: '2026-11-18T09:12:05.508Z', trainedOn: { gamesUsed: 4144 } },
      NFL: { fitVersion: '1.805.20261118', publishedAt: '2026-11-18T09:12:05.508Z', trainedOn: { gamesUsed: 805 } },
    },
  });
  // Ledger: 3 records across two sports, all graded "tonight".
  const gradesDir = join(root, 'data', 'grades');
  mkdirSync(gradesDir, { recursive: true });
  const rec = (id, sport) =>
    JSON.stringify({ id, sport, fetchedAt: '2026-11-19T07:47:00.000Z', contract: 'grade-record', v: 1 });
  writeFileSync(join(gradesDir, '2026-11.jsonl'), [rec('g1', 'NBA'), rec('g2', 'NBA'), rec('g3', 'NFL')].join('\n') + '\n');
  // Harvest: basketball native payloads CARRY the games on 11-16; from
  // 11-17 the native payloads are stored empty (the gate) and the
  // fallback payloads carry the games — presence is not service, and
  // the sampler must read it that way. Football likewise on 11-18.
  const payload = (source, games) => ({
    parameters: source
      ? { league: '12', season: '2022-2023', source }
      : { league: '12', season: '2022-2023' },
    results: games,
    response: Array.from({ length: games }, (_, i) => ({ id: i })),
  });
  writeJson(join(root, 'data', 'intelligence', 'raw', 'basketball', 'games', '2026-11-16', '060000-001.json'), payload(null, 12));
  writeJson(join(root, 'data', 'intelligence', 'raw', 'basketball', 'games', '2026-11-17', '060000-001.json'), payload(null, 0));
  writeJson(join(root, 'data', 'intelligence', 'raw', 'basketball', 'games', '2026-11-17', '060001-001.json'), payload('espn-fallback', 12));
  writeJson(join(root, 'data', 'intelligence', 'raw', 'american-football', 'games', '2026-11-18', '060000-001.json'), payload(null, 0));
  writeJson(join(root, 'data', 'intelligence', 'raw', 'american-football', 'games', '2026-11-18', '060001-001.json'), payload('espn-fallback', 9));
  // Soccer: native payloads carried games on 11-16, but its newest day
  // (11-18) has only empty payloads — no source served it. Stale
  // service must not be read as current service.
  writeJson(join(root, 'data', 'intelligence', 'raw', 'soccer', 'games', '2026-11-16', '060000-001.json'), payload(null, 10));
  writeJson(join(root, 'data', 'intelligence', 'raw', 'soccer', 'games', '2026-11-18', '060000-001.json'), payload(null, 0));
  writeJson(join(root, 'data', 'intelligence', 'harvest_state.json'), { '2026-11-18': { basketball: 20 } });
  // Prediction archives on the branch; NO props dir (fan-out absent).
  writeJson(join(root, 'data', 'predictions', 'feed-2026-11-18T09-00-15-871Z.json'), { contract: 'prediction-archive' });
  return root;
}

function build(root, extra = {}) {
  return buildHealthRollup({ root, now: NOW, lastCommitAt: '2026-11-18T06:23:00.000Z', ...extra });
}

function entry(record, name) {
  return record.sources.find((s) => s.name === name);
}

test('rollup: the record validates against the source-health contract (envelope)', () => {
  const rollup = build(fixtureRoot(), { grading: { appended: 3 } });
  const check = validate('source-health', rollup);
  assert.deepEqual(check.errors, []);
  assert.equal(rollup.publisher, 'grading-rollup');
  assert.equal(rollup.seams['grading-ledger'].counts.appendedTonight, 3);
  assert.equal(rollup.seams['grading-ledger'].counts.archivesOnBranch, 1);
});

test('rollup: harvest serving source is READ from stored payloads — API-Sports gated-off, ESPN serving', () => {
  const rollup = build(fixtureRoot());
  const api = entry(rollup, 'api-sports');
  assert.equal(api.status, 'gated-off');
  assert.equal(api.link, false);
  assert.equal(api.observedBy, 'data-branch');
  // The basketball scan found the last native day before the fallback run.
  assert.equal(api.lastGoodAt, '2026-11-16');
  assert.match(api.detail, /2026-11-17/);
  const espn = entry(rollup, 'espn-fallback');
  assert.equal(espn.status, 'ok');
  assert.equal(espn.link, true);
  assert.match(espn.detail, /Currently serving/);
});

test('rollup: a sport no source served on the newest day is a named hole, not stale service', () => {
  const rollup = build(fixtureRoot());
  // Soccer's stale native service does not drag the board to 'mixed':
  // every sport actually served on its newest day was served by ESPN.
  const api = entry(rollup, 'api-sports');
  assert.equal(api.status, 'gated-off');
  assert.equal(rollup.seams.harvest.counts.sportsUnserved, 1);
  assert.match(rollup.seams.harvest.detail, /soccer/);
  assert.ok(
    rollup.flags.some((f) => /soccer's newest harvest day \(2026-11-18\) carried no games from either/.test(f)),
    'unserved-sport flag'
  );
});

test('rollup: fan-out absence and sim-service undeployed state are reported as what they are', () => {
  const rollup = build(fixtureRoot());
  const fanout = entry(rollup, 'player-fan-out');
  assert.equal(fanout.status, 'not-in-production');
  assert.equal(fanout.link, false);
  assert.match(fanout.reason, /proof runs/);
  const sim = entry(rollup, 'sim-service');
  assert.equal(sim.status, 'not-deployed');
  assert.equal(sim.link, false);
  assert.equal(sim.observedBy, 'standing-record');
  assert.match(sim.reason, /not deployed to Railway/);
});

test('rollup: fit versions and ages come from the Phase C manifest', () => {
  const rollup = build(fixtureRoot());
  const fits = entry(rollup, 'fit-pipeline');
  assert.equal(fits.status, 'ok');
  assert.match(fits.detail, /NBA 1\.4144\.20261118 \(0d old, 4144 games\)/);
  assert.equal(rollup.seams.fits.counts.sports, 2);
  assert.equal(rollup.seams.fits.counts.newestFitAgeDays, 0);
});

test('rollup: ledger totals + freshness are reported per sport', () => {
  const rollup = build(fixtureRoot());
  assert.equal(rollup.seams['grading-ledger'].counts.totalRecords, 3);
  const ledger = entry(rollup, 'grading-ledger');
  assert.equal(ledger.status, 'ok');
  assert.match(ledger.detail, /NBA: 2 grades/);
  assert.match(ledger.detail, /NFL: 1 grades/);
});

test('rollup: flags fire on the evidence — last-link chain, fan-out, undeployed sim (>24h)', () => {
  const rollup = build(fixtureRoot());
  assert.ok(rollup.flags.some((f) => /down to its last verified link/.test(f)), 'chain-depth flag');
  assert.ok(rollup.flags.some((f) => /player fan-out has never had a production run/.test(f)), 'fan-out flag');
  // Sim built Nov 18; at Nov 19 08:00 it has been undeployed >24h.
  assert.ok(rollup.flags.some((f) => /sim service \(Phase D\) is built but not deployed/.test(f)), 'sim flag');
  // Fresh grades + fresh fits + harvest yesterday: no staleness flags.
  assert.ok(!rollup.flags.some((f) => /newest ledger grade/.test(f)));
  assert.ok(!rollup.flags.some((f) => /fit \(/.test(f)));
  assert.ok(!rollup.flags.some((f) => /stored no new games/.test(f)));
});

test('rollup: healthFlagsFor — stale grades and stale fits flag in plain language', () => {
  const flags = healthFlagsFor(
    {
      harvestServing: 'api-sports',
      lastHarvestDay: '2026-11-18',
      ledgerBySport: { NBA: { records: 9, lastGradeAt: '2026-11-16T08:00:00.000Z' } },
      fits: { NBA: { fitVersion: '1.9.20261101', ageDays: 12 } },
      fanout: { status: 'ok', stores: 2 },
      fanoutSince: '2026-11-17',
      simServiceSince: '2026-11-18',
    },
    NOW
  );
  assert.ok(flags.some((f) => /NBA's newest ledger grade is 3 days old/.test(f)));
  assert.ok(flags.some((f) => /The NBA fit \(1\.9\.20261101\) is 12 days old/.test(f)));
  // API-Sports serving again: no chain-depth flag.
  assert.ok(!flags.some((f) => /last verified link/.test(f)));
});

test('rollup: writeHealthRollup writes a valid latest.json', () => {
  const root = fixtureRoot();
  const rollup = build(root);
  const { path } = writeHealthRollup(root, rollup);
  assert.ok(existsSync(path));
  const written = JSON.parse(readFileSync(path, 'utf8'));
  assert.equal(validate('source-health', written).ok, true);
});

test('rollup: the report section lists statuses and flags in plain language', () => {
  const md = renderSourceHealthSection(build(fixtureRoot()));
  assert.match(md, /^## Source health/m);
  assert.match(md, /api-sports — gated-off \(not a link\)/);
  assert.match(md, /sim-service — not-deployed \(not a link\)/);
  assert.match(md, /FLAG: API-Sports is not serving the harvest/);
});

test('rollup: an empty data root degrades to not-observed, never throws, never invents', () => {
  const root = mkdtempSync(join(tmpdir(), 'health-rollup-empty-'));
  const rollup = buildHealthRollup({ root, now: NOW, lastCommitAt: null });
  assert.equal(validate('source-health', rollup).ok, true);
  assert.equal(entry(rollup, 'api-sports').status, 'not-observed');
  assert.equal(rollup.seams.fits.status, 'not-observed');
  assert.equal(rollup.seams['grading-ledger'].counts.totalRecords, 0);
});
