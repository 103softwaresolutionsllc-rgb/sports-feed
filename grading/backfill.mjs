/**
 * backfill.mjs — one-time backlog builder for the prediction archive.
 *
 *   node grading/backfill.mjs --root <data-branch checkout>
 *        [--blob-base <url>] [--out <dir>] [--limit <n>]
 *
 * The collector's archive step (predictions.mjs) runs from the next
 * deployed run onward. Editions published BEFORE it existed survive
 * whole in the Blob history (history.mjs). This tool walks that
 * history index, fetches each edition, and runs the SAME extraction
 * the collector runs (extractPredictions) to produce the archive
 * files the nightly job consumes, under <root>/data/predictions.
 *
 * Idempotent: editions whose archive file already exists are skipped.
 * Bounded by the history index (≤500 entries).
 */

import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { extractPredictions, predictionsPathFor } from '../predictions.mjs';

const DEFAULT_BLOB_BASE = 'https://lmiqgg08ux9djfii.public.blob.vercel-storage.com';

function parseArgs(argv) {
  const args = { root: null, blobBase: DEFAULT_BLOB_BASE, out: null, limit: null };
  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--root') args.root = argv[++i];
    else if (arg === '--blob-base') args.blobBase = argv[++i];
    else if (arg === '--out') args.out = argv[++i];
    else if (arg === '--limit') args.limit = Number(argv[++i]);
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (!args.root) throw new Error('--root <data-branch checkout> is required');
  if (!args.out) args.out = join(args.root, 'data', 'predictions');
  return args;
}

async function main() {
  const args = parseArgs(process.argv);
  mkdirSync(args.out, { recursive: true });
  const indexRes = await fetch(`${args.blobBase}/history/index.json`, { cache: 'no-store' });
  if (!indexRes.ok) throw new Error(`history index HTTP ${indexRes.status}`);
  const index = await indexRes.json();
  const snapshots = [...(index.snapshots || [])].reverse(); // oldest first
  const todo = args.limit ? snapshots.slice(0, args.limit) : snapshots;
  const summary = { editions: todo.length, written: 0, skipped: 0, games: 0, modelPriced: 0, pickCards: 0 };
  for (const entry of todo) {
    const path = predictionsPathFor(entry.timestamp);
    const local = join(args.out, path.split('/').pop());
    if (existsSync(local)) {
      summary.skipped += 1;
      continue;
    }
    const res = await fetch(entry.url, { cache: 'no-store' });
    if (!res.ok) continue;
    const feed = await res.json();
    const archive = extractPredictions(feed);
    writeFileSync(local, JSON.stringify(archive, null, 2) + '\n', 'utf8');
    summary.written += 1;
    summary.games += archive.games.length;
    summary.modelPriced += archive.games.filter((g) => g.model).length;
    summary.pickCards += archive.picks.length;
  }
  console.log(JSON.stringify(summary, null, 2));
}

const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  main().catch((err) => {
    console.error(`backfill failed: ${String(err?.message || err)}`);
    process.exit(1);
  });
}
