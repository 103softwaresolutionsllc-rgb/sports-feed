/**
 * health-rollup.mjs — the nightly source-health rollup (Quant
 * Apparatus spec §8, Phase E).
 *
 * The grading run's sibling: where the collector's board (health.mjs)
 * reports what the collector sees, this rollup reports what the DATA
 * BRANCH can truthfully show — ledger totals and freshness, fit
 * versions and ages from the Phase C manifest, and the harvest /
 * fan-out states as they are observable from stored files. Anything
 * not observable from here is recorded as not-observed or as a
 * standing-record entry, never invented.
 *
 * Output: <root>/data/health/latest.json (SourceHealth contract,
 * publisher 'grading-rollup'), validated before it is written. The
 * flags it computes are rendered into the nightly ledger report's
 * Source health section by run.mjs via report.mjs.
 *
 * THE BOARD NEVER EDITS THE CHAIN: this module reads state only. It
 * cannot ungate API-Sports, enable BetStack, deploy the sim service,
 * or publish a fit. Those are Isaac's or the Legion runner's acts;
 * the board's whole job is to make sure none of them is ever again
 * discovered by screenshot.
 *
 * Manual run (the grading workflow is not yet pushed):
 *   node grading/health-rollup.mjs --root <data-branch checkout>
 */

import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { loadLedger } from './store.mjs';
import { validate } from '../contracts/validate.mjs';

export function healthDir(root) {
  return join(root, 'data', 'health');
}

const DAY_MS = 24 * 60 * 60 * 1000;

function dayAge(thenMs, nowMs) {
  return Math.floor((nowMs - thenMs) / DAY_MS);
}

/** Day directories under raw/<sport>/games, ascending. */
function harvestDays(root, sport) {
  const dir = join(root, 'data', 'intelligence', 'raw', sport, 'games');
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isDirectory() && /^\d{4}-\d{2}-\d{2}$/.test(e.name))
    .map((e) => e.name)
    .sort();
}

/**
 * Which source SERVED one sport-day: the source whose stored payloads
 * actually carry games. A gated API-Sports attempt still stores its
 * (empty) payload beside the fallback's — presence is not service.
 * Fallback payloads are their own files carrying
 * parameters.source = 'espn-fallback'; a native payload has no marker.
 * Returns 'espn-fallback' | 'api-sports' | 'mixed' | null (no payload
 * that day carried games). Reads one day directory (bounded).
 */
export function dayServing(root, sport, day) {
  const dir = join(root, 'data', 'intelligence', 'raw', sport, 'games', day);
  if (!existsSync(dir)) return null;
  const carrying = new Set();
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.json')).slice(0, 500)) {
    try {
      const doc = JSON.parse(readFileSync(join(dir, file), 'utf8'));
      const games =
        typeof doc?.results === 'number'
          ? doc.results
          : Array.isArray(doc?.response)
            ? doc.response.length
            : 0;
      if (games > 0) carrying.add(doc?.parameters?.source ?? 'api-sports');
    } catch {
      // An unreadable payload proves nothing either way; skip it.
    }
  }
  if (carrying.size === 0) return null;
  if (carrying.size > 1) return 'mixed';
  return [...carrying][0];
}

/**
 * What the data branch shows about the harvest, per sport: the newest
 * stored day and which source served THAT day. Serving is judged on
 * the newest day only — the grading run is scheduled after the
 * harvest window, so the newest day is complete; judging by the last
 * day any source ever served would present stale service (a sport
 * whose fallback never fired) as current. A sport whose newest day
 * carried no games from either source reports serving: null and is
 * named in the rollup's unserved list — a hole, reported as a hole.
 * Plus, for basketball (the richest series), the current fallback
 * run: the earliest consecutive espn-fallback day ending at the
 * newest day, and the last day native payloads carried games.
 */
export function observeHarvest(root) {
  const rawRoot = join(root, 'data', 'intelligence', 'raw');
  const sports = {};
  if (existsSync(rawRoot)) {
    for (const entry of readdirSync(rawRoot, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const days = harvestDays(root, entry.name);
      if (!days.length) continue;
      const lastDay = days[days.length - 1];
      sports[entry.name] = { lastDay, serving: dayServing(root, entry.name, lastDay) };
    }
  }
  let fallbackSinceDay = null;
  let lastNativeDay = null;
  const bbDays = harvestDays(root, 'basketball');
  for (let i = bbDays.length - 1; i >= 0 && i >= bbDays.length - 31; i--) {
    const serving = dayServing(root, 'basketball', bbDays[i]);
    if (serving === 'espn-fallback') {
      fallbackSinceDay = bbDays[i];
    } else if (serving === 'api-sports' || serving === 'mixed') {
      lastNativeDay = bbDays[i];
      break;
    }
    // null (an empty day) proves nothing; keep walking.
  }
  let lastStateDay = null;
  try {
    const state = JSON.parse(
      readFileSync(join(root, 'data', 'intelligence', 'harvest_state.json'), 'utf8')
    );
    const days = Object.keys(state).filter((k) => /^\d{4}-\d{2}-\d{2}$/.test(k)).sort();
    lastStateDay = days.length ? days[days.length - 1] : null;
  } catch {
    lastStateDay = null;
  }
  return { sports, fallbackSinceDay, lastNativeDay, lastStateDay };
}

/** Newest commit touching data/intelligence, via git; null when the
 *  checkout is not a git repo or git is unavailable (not observed). */
export function lastHarvestCommitAt(root) {
  try {
    const out = execFileSync(
      'git',
      ['-C', root, 'log', '-1', '--format=%cI', '--', 'data/intelligence'],
      { encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'] }
    ).trim();
    return out || null;
  } catch {
    return null;
  }
}

/**
 * The flag rules (spec §8 alerting). Each rule is plain language and
 * fires only on evidence in `state` — the structured observations the
 * rollup itself made. Exported for tests.
 */
export function healthFlagsFor(state, now) {
  const flags = [];
  const nowMs = now.getTime();

  // Chain depth: the games chain is on its last verified link.
  if (state.harvestServing === 'espn-fallback') {
    flags.push(
      'API-Sports is not serving the harvest — games are coming from the ESPN fallback, ' +
        'so the games chain is down to its last verified link. API-Sports stays gated off ' +
        'until the Legion runner is installed.'
    );
  }
  // A sport no source served on the newest stored day is a hole.
  for (const unserved of state.unservedSports ?? []) {
    flags.push(
      `${unserved.sport}'s newest harvest day (${unserved.day}) carried no games from either ` +
        'source — neither API-Sports nor the ESPN fallback served it.'
    );
  }
  // Harvest staleness (>48h without a stored day).
  if (state.lastHarvestDay) {
    const age = dayAge(Date.parse(`${state.lastHarvestDay}T00:00:00.000Z`), nowMs);
    if (age >= 2) {
      flags.push(
        `The harvest has stored no new games since ${state.lastHarvestDay} (${age} days ago).`
      );
    }
  }
  // Ledger freshness per sport (>48h since the newest grade).
  for (const [sport, info] of Object.entries(state.ledgerBySport ?? {})) {
    if (!info.lastGradeAt) continue;
    const ageH = Math.floor((nowMs - Date.parse(info.lastGradeAt)) / (60 * 60 * 1000));
    if (ageH > 48) {
      flags.push(
        `${sport}'s newest ledger grade is ${Math.floor(ageH / 24)} days old — grades should land nightly.`
      );
    }
  }
  // Fit staleness (weekly cadence + a day's grace).
  for (const [sport, fit] of Object.entries(state.fits ?? {})) {
    if (fit.ageDays != null && fit.ageDays > 8) {
      flags.push(
        `The ${sport} fit (${fit.fitVersion}) is ${fit.ageDays} days old — the fit pipeline publishes weekly; check it.`
      );
    }
  }
  // Standing not-ok conditions older than a day.
  if (
    state.fanout &&
    state.fanout.status === 'not-in-production' &&
    state.fanoutSince &&
    dayAge(Date.parse(`${state.fanoutSince}T00:00:00.000Z`), nowMs) >= 1
  ) {
    flags.push(
      'The player fan-out has never had a production run (proof runs only, Nov 17) — ' +
        'its production home is the Legion runner, which is not installed yet.'
    );
  }
  if (state.simServiceSince && dayAge(Date.parse(`${state.simServiceSince}T00:00:00.000Z`), nowMs) >= 1) {
    flags.push(
      'The sim service (Phase D) is built but not deployed — Sage cannot serve simulations ' +
        'until it is deployed to Railway (the deploy is Isaac\'s word).'
    );
  }
  return flags;
}

/**
 * Build the nightly rollup record from a data-branch checkout.
 * Fully defensive: a missing store degrades that entry to
 * not-observed; this builder does not throw on absent data.
 * `grading` is runGrading's own counters ({ appended }) when invoked
 * from the grading run, else null.
 */
export function buildHealthRollup({ root, now = new Date(), grading = null, lastCommitAt } = {}) {
  const nowMs = now.getTime();

  // --- Ledger -------------------------------------------------------
  const { records } = loadLedger(root);
  const ledgerBySport = {};
  for (const record of records.values()) {
    const sport = record.sport ?? 'unknown';
    if (!ledgerBySport[sport]) ledgerBySport[sport] = { records: 0, lastGradeAt: null };
    ledgerBySport[sport].records += 1;
    if (record.fetchedAt && (!ledgerBySport[sport].lastGradeAt || record.fetchedAt > ledgerBySport[sport].lastGradeAt)) {
      ledgerBySport[sport].lastGradeAt = record.fetchedAt;
    }
  }
  const ledgerTotal = records.size;
  const staleSports = Object.entries(ledgerBySport)
    .filter(([, i]) => i.lastGradeAt && nowMs - Date.parse(i.lastGradeAt) > 48 * 60 * 60 * 1000)
    .map(([s]) => s);

  // --- Fits ----------------------------------------------------------
  const fits = {};
  let fitsManifestPresent = false;
  try {
    const manifest = JSON.parse(readFileSync(join(root, 'data', 'fits', 'manifest.json'), 'utf8'));
    fitsManifestPresent = true;
    for (const [sport, entry] of Object.entries(manifest.sports ?? {})) {
      const publishedMs = entry.publishedAt ? Date.parse(entry.publishedAt) : null;
      fits[sport] = {
        fitVersion: entry.fitVersion ?? null,
        publishedAt: entry.publishedAt ?? null,
        ageDays: publishedMs != null && Number.isFinite(publishedMs) ? dayAge(publishedMs, nowMs) : null,
        gamesUsed: entry.trainedOn?.gamesUsed ?? null,
      };
    }
  } catch {
    fitsManifestPresent = false;
  }
  const fitAges = Object.values(fits).map((f) => f.ageDays).filter((a) => a != null);
  const fitsStale = Object.entries(fits).filter(([, f]) => f.ageDays != null && f.ageDays > 8).map(([s]) => s);

  // --- Harvest -------------------------------------------------------
  const harvest = observeHarvest(root);
  const sampledServings = Object.values(harvest.sports).map((s) => s.serving).filter(Boolean);
  const harvestServing =
    sampledServings.length === 0
      ? null
      : sampledServings.every((s) => s === 'espn-fallback')
        ? 'espn-fallback'
        : sampledServings.every((s) => s === 'api-sports')
          ? 'api-sports'
          : 'mixed';
  const harvestDaysSeen = Object.values(harvest.sports).map((s) => s.lastDay).sort();
  const lastHarvestDay = harvestDaysSeen.length ? harvestDaysSeen[harvestDaysSeen.length - 1] : harvest.lastStateDay;
  const commitAt = lastCommitAt !== undefined ? lastCommitAt : lastHarvestCommitAt(root);

  // --- Fan-out -------------------------------------------------------
  const propsDir = join(root, 'data', 'intelligence', 'props');
  const fanoutStores = existsSync(propsDir)
    ? readdirSync(propsDir).filter((f) => /^game_logs_.+\.json$/.test(f))
    : [];
  const fanout =
    fanoutStores.length > 0
      ? { status: 'ok', stores: fanoutStores.length }
      : { status: 'not-in-production', stores: 0 };

  // --- Prediction archives ------------------------------------------
  const predictionsDir = join(root, 'data', 'predictions');
  const archiveEditions = existsSync(predictionsDir)
    ? readdirSync(predictionsDir).filter((f) => /^feed-.*\.json$/.test(f)).length
    : 0;

  const unservedSports = Object.entries(harvest.sports)
    .filter(([, i]) => i.serving === null)
    .map(([sport, i]) => ({ sport, day: i.lastDay }));

  const state = {
    harvestServing,
    lastHarvestDay,
    unservedSports,
    ledgerBySport,
    fits,
    fanout,
    fanoutSince: '2026-11-17',
    simServiceSince: '2026-11-18',
  };
  const flags = healthFlagsFor(state, now);

  const sources = [
    harvestServing === null
      ? {
          name: 'api-sports',
          role: 'Games/fixtures/stats for the Intelligence Engine harvest (primary)',
          chain: 'games-harvest',
          chainPosition: 1,
          link: false,
          status: 'not-observed',
          reason: 'No stored harvest payloads on the data branch to observe.',
          observedBy: 'data-branch',
          lastGoodAt: null,
          lastAttemptAt: null,
          since: null,
          quota: null,
          detail: null,
        }
      : harvestServing === 'espn-fallback'
        ? {
            name: 'api-sports',
            role: 'Games/fixtures/stats for the Intelligence Engine harvest (primary)',
            chain: 'games-harvest',
            chainPosition: 1,
            link: false,
            status: 'gated-off',
            reason:
              'Account suspended (shared-IP cause confirmed by API-Sports support, 2026-11-17) and the ' +
              'harvest gate is default-deny: INTELLIGENCE_ALLOW_API is set only on the Legion runner, ' +
              'which is not installed yet (the laptop is at the Lenovo depot). This board observes the ' +
              'effect: in every sampled sport, the games in the newest stored payloads were carried by ' +
              'the ESPN fallback; the native payloads beside them are empty.',
            observedBy: 'data-branch',
            lastGoodAt: harvest.lastNativeDay,
            lastAttemptAt: null,
            since: '2026-11-17',
            quota: null,
            detail: harvest.fallbackSinceDay
              ? `ESPN-fallback payloads run from ${harvest.fallbackSinceDay} through the newest stored day (${lastHarvestDay}).`
              : null,
          }
        : harvestServing === 'mixed'
          ? {
              name: 'api-sports',
              role: 'Games/fixtures/stats for the Intelligence Engine harvest (primary)',
              chain: 'games-harvest',
              chainPosition: 1,
              link: true,
              status: 'degraded',
              reason:
                'Serving is split across sports in the newest stored payloads: native API-Sports ' +
                'payloads carry games for some sports, the ESPN fallback for others. Per-sport detail ' +
                'is in the harvest seam\'s source data on the data branch.',
              observedBy: 'data-branch',
              lastGoodAt: lastHarvestDay,
              lastAttemptAt: lastHarvestDay,
              since: null,
              quota: null,
              detail: Object.entries(harvest.sports)
                .map(([s, i]) => `${s}: ${i.serving ?? 'nothing carried games'} (${i.lastDay})`)
                .join(' · '),
            }
          : {
            name: 'api-sports',
            role: 'Games/fixtures/stats for the Intelligence Engine harvest (primary)',
            chain: 'games-harvest',
            chainPosition: 1,
            link: true,
            status: 'ok',
            reason: null,
            observedBy: 'data-branch',
            lastGoodAt: lastHarvestDay,
            lastAttemptAt: lastHarvestDay,
            since: null,
            quota: null,
            detail: 'Newest stored payloads are native API-Sports payloads again.',
          },
    {
      name: 'espn-fallback',
      role: 'Failover source for the games harvest (keyless public scoreboards)',
      chain: 'games-harvest',
      chainPosition: 2,
      link: true,
      status: 'ok',
      reason: null,
      observedBy: 'data-branch',
      lastGoodAt: lastHarvestDay,
      lastAttemptAt: lastHarvestDay,
      since: null,
      quota: null,
      detail:
        harvestServing === 'espn-fallback'
          ? 'Currently serving the games harvest — stored as its own payloads with source "espn-fallback", never blended into an API-Sports payload.'
          : harvestServing === 'mixed'
            ? 'Serving the sports the native payloads do not carry (see the api-sports entry); verified link for the rest.'
            : 'Verified standby link; not the serving source in the newest stored payloads.',
    },
    {
      name: 'player-fan-out',
      role: 'Player game logs for props (Intelligence Engine fan-out)',
      chain: null,
      chainPosition: null,
      link: false,
      status: fanout.status,
      reason:
        fanout.status === 'ok'
          ? null
          : 'No production run: the fan-out is not in the Actions workflow and its production home is the ' +
            'pending Legion runner. The only stores are the Nov 17, 2026 ESPN-fallback proof runs ' +
            '(NBA 30/30 teams, NFL 32/32) in ~/workspace/.tmp-fanout — data/intelligence/props/ is absent ' +
            'from the data branch.',
      observedBy: 'data-branch',
      lastGoodAt: null,
      lastAttemptAt: null,
      since: fanout.status === 'ok' ? null : '2026-11-17',
      quota: null,
      detail: fanout.status === 'ok' ? `${fanout.stores} game-log stores present on the data branch.` : null,
    },
    {
      name: 'fit-pipeline',
      role: 'Publishes versioned FitArtifacts for the Simulation Lab (Phase C)',
      chain: 'fit-publication',
      chainPosition: 1,
      link: fitsManifestPresent,
      status: !fitsManifestPresent ? 'not-observed' : fitsStale.length ? 'degraded' : 'ok',
      reason: !fitsManifestPresent
        ? 'No fit manifest on the data branch (data/fits/manifest.json missing).'
        : fitsStale.length
          ? `Fits older than the weekly cadence for: ${fitsStale.join(', ')}.`
          : null,
      observedBy: 'data-branch',
      lastGoodAt: fitAges.length ? Object.values(fits).map((f) => f.publishedAt).sort().slice(-1)[0] : null,
      lastAttemptAt: null,
      since: null,
      quota: null,
      detail: fitsManifestPresent
        ? Object.entries(fits)
            .map(([s, f]) => `${s} ${f.fitVersion} (${f.ageDays}d old, ${f.gamesUsed} games)`)
            .join(' · ')
        : null,
    },
    {
      name: 'grading-ledger',
      role: 'Grades the model\'s live probability stream nightly (Phase B)',
      chain: null,
      chainPosition: null,
      link: true,
      status: staleSports.length ? 'degraded' : 'ok',
      reason: staleSports.length ? `Newest grade is over 48h old for: ${staleSports.join(', ')}.` : null,
      observedBy: 'data-branch',
      lastGoodAt: Object.values(ledgerBySport).map((i) => i.lastGradeAt).filter(Boolean).sort().slice(-1)[0] ?? null,
      lastAttemptAt: now.toISOString(),
      since: null,
      quota: null,
      detail:
        ledgerTotal === 0
          ? 'Ledger is empty.'
          : Object.entries(ledgerBySport)
              .map(([s, i]) => `${s}: ${i.records} grades, newest ${i.lastGradeAt ?? 'never'}`)
              .join(' · '),
    },
    {
      name: 'sim-service',
      role: 'Serves the canonical simulation engine to Sage (Phase D)',
      chain: null,
      chainPosition: null,
      link: false,
      status: 'not-deployed',
      reason:
        'Built Nov 18, 2026 (sportsbettinggpt sim-service/, commit 3c0167e) and proven locally, but not ' +
        'deployed to Railway — the deploy is Isaac\'s separate word. Sage\'s sim client waits behind ' +
        'SAGE_SIM_ENABLED (default off). Deployment state is not observable from the data branch; this ' +
        'entry is the standing build record and changes when the deploy lands.',
      observedBy: 'standing-record',
      lastGoodAt: null,
      lastAttemptAt: null,
      since: '2026-11-18',
      quota: null,
      detail: null,
    },
  ];

  const seams = {
    'grading-ledger': {
      status: staleSports.length ? 'degraded' : 'ok',
      detail: null,
      counts: {
        totalRecords: ledgerTotal,
        appendedTonight: grading?.appended ?? null,
        archivesOnBranch: archiveEditions,
      },
    },
    fits: {
      status: !fitsManifestPresent ? 'not-observed' : fitsStale.length ? 'degraded' : 'ok',
      detail: fitsManifestPresent
        ? Object.entries(fits).map(([s, f]) => `${s} ${f.fitVersion}`).join(' · ')
        : 'data/fits/manifest.json is absent from this checkout.',
      counts: {
        sports: Object.keys(fits).length,
        oldestFitAgeDays: fitAges.length ? Math.max(...fitAges) : null,
        newestFitAgeDays: fitAges.length ? Math.min(...fitAges) : null,
      },
    },
    harvest: {
      status:
        harvestServing === 'espn-fallback' || harvestServing === 'mixed' || unservedSports.length
          ? 'degraded'
          : harvestServing
            ? 'ok'
            : 'not-observed',
      detail:
        `Serving source (newest stored day): ${harvestServing ?? 'not observed'}. ` +
        (unservedSports.length
          ? `No source carried games on the newest day for: ${unservedSports.map((u) => u.sport).join(', ')}. `
          : '') +
        `Last harvest commit touching data/intelligence: ${commitAt ?? 'not observed'}.`,
      counts: {
        lastHarvestDay: lastHarvestDay ?? null,
        sportsSampled: sampledServings.length,
        sportsUnserved: unservedSports.length,
      },
    },
    'collector-board': {
      status: 'not-observed',
      detail:
        'The collector publishes health.json to the Blob store beside feed.json every tick; it is not ' +
        'mirrored on the data branch, so the nightly rollup does not observe it.',
      counts: null,
    },
  };

  return {
    contract: 'source-health',
    v: 1,
    class: 'Historical',
    source: 'sports-feed grading rollup (grading/health-rollup.mjs)',
    fetchedAt: now.toISOString(),
    id: `source-health:nightly:${now.toISOString()}`,
    publisher: 'grading-rollup',
    board: 'nightly-rollup',
    generatedAt: now.toISOString(),
    sources,
    seams,
    flags,
  };
}

/** Validate + write <root>/data/health/latest.json. Throws on an
 *  invalid record — the nightly job should fail loudly on its own
 *  board; run.mjs guards the grading path against that failure. */
export function writeHealthRollup(root, rollup) {
  const check = validate('source-health', rollup);
  if (!check.ok) {
    throw new Error(`health rollup fails the contract: ${check.errors.join('; ')}`);
  }
  const dir = healthDir(root);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'latest.json');
  writeFileSync(path, JSON.stringify(rollup, null, 2) + '\n', 'utf8');
  return { path };
}

/** The ledger report's Source health section (markdown). */
export function renderSourceHealthSection(rollup) {
  const lines = [];
  lines.push('## Source health');
  lines.push('');
  lines.push(
    'Nightly rollup: `data/health/latest.json` · Collector board: `health.json` ' +
      '(Blob store, beside feed.json). The board reports; it never changes a gate.'
  );
  lines.push('');
  for (const source of rollup.sources ?? []) {
    lines.push(`- ${source.name} — ${source.status}${source.link ? '' : ' (not a link)'}`);
  }
  lines.push('');
  if (rollup.flags?.length) {
    lines.push('Needs attention:');
    for (const flag of rollup.flags) lines.push(`- FLAG: ${flag}`);
  } else {
    lines.push('No source flags tonight.');
  }
  return lines.join('\n') + '\n';
}

const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  const argv = process.argv.slice(2);
  let root = null;
  let now = new Date();
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--root') root = argv[++i];
    else if (argv[i] === '--now') now = new Date(argv[++i]);
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  if (!root) throw new Error('--root <data-branch checkout> is required');
  try {
    const rollup = buildHealthRollup({ root, now });
    const { path } = writeHealthRollup(root, rollup);
    console.log(`health rollup written: ${path}`);
    for (const flag of rollup.flags) console.log(`FLAG: ${flag}`);
    if (!rollup.flags.length) console.log('no flags');
  } catch (err) {
    console.error(`health rollup failed: ${String(err?.message || err)}`);
    process.exit(1);
  }
}
