/**
 * run.mjs — the nightly grading job (Quant Apparatus spec §5.1).
 *
 *   node grading/run.mjs --root <data-branch checkout> [--blob-base <url>]
 *                        [--predictions-root <dir>] [--now <ISO>]
 *
 * Steps:
 *   1. Materialize prediction archives: with --blob-base, fetch the
 *      predictions index and download any archive files not already in
 *      the predictions root (default <root>/data/predictions). Without
 *      it, grade whatever archives are already there.
 *   2. For each game, select the LAST edition published before its
 *      start that carried it — the closing pre-game read, the same
 *      convention as a closing line. Pick cards are selected the same
 *      way, by card id.
 *   3. Match finals from the data branch (results.mjs — harvested
 *      payloads + the archived ESPN results cache). THIS SCRIPT MAKES
 *      NO PROVIDER CALLS; the only network it may touch is the
 *      apparatus's own Blob archive in step 1.
 *   4. Append GradeRecords through the ledger store (validated,
 *      idempotent — a re-run appends nothing twice) and regenerate
 *      the reports from the full ledger.
 *
 * Bounded: the predictions index is capped at 500 entries; each
 * archive is read once; results are indexed once in memory.
 */

import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, join } from 'node:path';
import { appendRecords, loadLedger } from './store.mjs';
import { writeReports } from './report.mjs';
import { loadResults, findResult } from './results.mjs';
import {
  STREAMS,
  devigMoneyline,
  gameKey,
  makePickGrade,
  resultFromScore,
} from './records.mjs';

function parseArgs(argv) {
  const args = { root: null, predictionsRoot: null, blobBase: null, now: new Date() };
  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--root') args.root = argv[++i];
    else if (arg === '--predictions-root') args.predictionsRoot = argv[++i];
    else if (arg === '--blob-base') args.blobBase = argv[++i];
    else if (arg === '--now') args.now = new Date(argv[++i]);
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (!args.root) throw new Error('--root <data-branch checkout> is required');
  if (!args.predictionsRoot) {
    args.predictionsRoot = join(args.root, 'data', 'predictions');
  }
  return args;
}

async function materializeFromBlob(blobBase, predictionsRoot) {
  const indexRes = await fetch(`${blobBase}/predictions/index.json`, { cache: 'no-store' });
  if (!indexRes.ok) throw new Error(`predictions index HTTP ${indexRes.status}`);
  const index = await indexRes.json();
  mkdirSync(predictionsRoot, { recursive: true });
  let fetched = 0;
  for (const entry of index.snapshots || []) {
    const local = join(predictionsRoot, basename(entry.path));
    if (existsSync(local)) continue;
    const res = await fetch(entry.url, { cache: 'no-store' });
    if (!res.ok) continue;
    writeFileSync(local, await res.text(), 'utf8');
    fetched += 1;
  }
  return fetched;
}

export function loadArchives(predictionsRoot) {
  const archives = [];
  if (!existsSync(predictionsRoot)) return archives;
  for (const file of readdirSync(predictionsRoot)) {
    if (!file.endsWith('.json')) continue;
    try {
      const doc = JSON.parse(readFileSync(join(predictionsRoot, file), 'utf8'));
      if (doc && doc.contract === 'prediction-archive') archives.push(doc);
    } catch {
      // A malformed archive is skipped; the ledger never guesses.
    }
  }
  archives.sort((a, b) => String(a.generatedAt).localeCompare(String(b.generatedAt)));
  return archives;
}

/**
 * Select, per game, the last pre-game edition's record of it; and per
 * pick card, the last pre-game edition that published it.
 */
export function selectGradables(archives) {
  const games = new Map();
  const cards = new Map();
  for (const archive of archives) {
    const generatedMs = Date.parse(archive.generatedAt);
    for (const game of archive.games || []) {
      if (!(generatedMs <= game.startTs)) continue; // pre-game editions only
      const key = gameKey({ league: game.league, startTs: game.startTs, away: game.away, home: game.home });
      games.set(key, { archive, game, key });
    }
    for (const card of archive.picks || []) {
      if (!(generatedMs <= card.startTs)) continue;
      cards.set(card.id, { archive, card });
    }
  }
  return { games: [...games.values()], cards: [...cards.values()] };
}

function parseCardPick(card) {
  // pick text: "<favorite team> <spread>" or "<favorite team> ML".
  const text = String(card.pick || '').trim();
  let favorite = null;
  let spread = null;
  if (text.endsWith(' ML')) {
    favorite = text.slice(0, -3).trim();
  } else {
    const match = text.match(/^(.*?)\s+([+-]?\d+(?:\.\d+)?)$/);
    if (match) {
      favorite = match[1].trim();
      spread = Number(match[2]);
    }
  }
  if (!favorite) return null;
  let side = null;
  if (favorite === card.teamB) side = 'home';
  else if (favorite === card.teamA) side = 'away';
  if (!side) return null;
  return { favorite, spread, side };
}

export function gradeSelections({ games, cards }, results, nowIso) {
  const records = [];
  const stats = { gamesSelected: games.length, gamesGraded: 0, gamesPending: 0, cardsSelected: cards.length, cardsGraded: 0, cardsSkipped: 0 };

  for (const { archive, game, key } of games) {
    const result = findResult(results, {
      sport: game.league,
      startAt: game.startAt,
      home: game.home,
      away: game.away,
    });
    if (!result) {
      stats.gamesPending += 1;
      continue;
    }
    stats.gamesGraded += 1;
    const settledAt = result.startAt || game.startAt;
    const outcome = resultFromScore(result.homeScore, result.awayScore);
    const snapshotId = `${archive.generatedAt}#${key}`;

    if (game.model && game.model.homeWinProb != null) {
      records.push(
        makePickGrade({
          stream: STREAMS.APP_MODEL,
          recordClass: 'Estimated',
          sport: game.league,
          league: game.league,
          gameKey: key,
          snapshotId,
          selectionPart: 'ml-home',
          prediction: `${game.home} — Moneyline`,
          modelProb: game.model.homeWinProb,
          result: outcome,
          settledAt,
          now: nowIso,
        })
      );
    }

    const primary = (game.lineSets || [])[0];
    if (primary?.moneyline) {
      const devigged = devigMoneyline(primary.moneyline.home, primary.moneyline.away);
      if (devigged) {
        records.push(
          makePickGrade({
            stream: STREAMS.BOOK_IMPLIED,
            recordClass: 'Live',
            sport: game.league,
            league: game.league,
            gameKey: key,
            snapshotId,
            selectionPart: 'ml-home',
            prediction: `${game.home} — Moneyline (book-implied, ${primary.bookmaker})`,
            modelProb: devigged.home,
            result: outcome,
            settledAt,
            now: nowIso,
          })
        );
      }
    }
  }

  for (const { archive, card } of cards) {
    const parsed = parseCardPick(card);
    if (!parsed || card.impliedProb == null) {
      stats.cardsSkipped += 1;
      continue;
    }
    const startAt = new Date(card.startTs).toISOString();
    const result = findResult(results, {
      sport: card.sportLabel,
      startAt,
      home: card.teamB,
      away: card.teamA,
    });
    if (!result) {
      stats.cardsSkipped += 1;
      continue;
    }
    const favScore = parsed.side === 'home' ? result.homeScore : result.awayScore;
    const oppScore = parsed.side === 'home' ? result.awayScore : result.homeScore;
    let outcome;
    if (parsed.spread == null) {
      outcome = favScore > oppScore ? 'win' : favScore < oppScore ? 'loss' : 'push';
    } else {
      const covered = favScore - oppScore + parsed.spread;
      outcome = covered > 0 ? 'win' : covered < 0 ? 'loss' : 'push';
    }
    const key = gameKey({ league: card.sportLabel, startTs: card.startTs, away: card.teamA, home: card.teamB });
    records.push(
      makePickGrade({
        stream: STREAMS.BOOK_IMPLIED_PICK_CARD,
        recordClass: 'Live',
        sport: card.sportLabel,
        league: card.sportLabel,
        gameKey: key,
        snapshotId: `${archive.generatedAt}#${card.id}`,
        selectionPart: 'pick-card',
        prediction: card.pick,
        modelProb: card.impliedProb,
        result: outcome,
        settledAt: result.startAt || startAt,
        now: nowIso,
      })
    );
    stats.cardsGraded += 1;
  }

  return { records, stats };
}

export async function runGrading({ root, predictionsRoot, blobBase, now }) {
  let materialized = 0;
  if (blobBase) materialized = await materializeFromBlob(blobBase, predictionsRoot);
  const archives = loadArchives(predictionsRoot);
  const selections = selectGradables(archives);
  const results = loadResults(root);
  const nowIso = now.toISOString();
  const { records, stats } = gradeSelections(selections, results, nowIso);
  const append = appendRecords(root, records, { now });
  const { records: all } = loadLedger(root);
  const { report } = writeReports(root, [...all.values()], { generatedAt: nowIso });
  return {
    materialized,
    archives: archives.length,
    ...stats,
    recordsBuilt: records.length,
    appended: append.appended,
    skippedDuplicates: append.skipped,
    superseded: append.superseded,
    ledgerRecords: report.recordCount,
  };
}

const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  const args = parseArgs(process.argv);
  runGrading(args)
    .then((summary) => {
      console.log(JSON.stringify(summary, null, 2));
    })
    .catch((err) => {
      console.error(`grading run failed: ${String(err?.message || err)}`);
      process.exit(1);
    });
}
