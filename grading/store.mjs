/**
 * store.mjs — the grading ledger's append-only JSONL store
 * (Quant Apparatus spec §5.3).
 *
 * Layout under a data-branch checkout root:
 *   <root>/data/grades/<YYYY-MM>.jsonl   one GradeRecord per line,
 *                                        filed by APPEND month
 *   <root>/data/grades/reports/          generated reports (§5.1)
 *   <root>/data/grades/results/          archived final-score caches
 *   <root>/data/predictions/             archived prediction editions
 *
 * Rules (binding):
 *   - Every record validates against the Phase A grade-record contract
 *     (envelope level) BEFORE it is appended. An invalid record fails
 *     the whole append loudly — nothing is written.
 *   - Append-only: existing lines are never edited or deleted. A
 *     correction is a NEW line with the same id; readers fold lines in
 *     file order and the later line supersedes the earlier.
 *   - Idempotent: appending a record whose id already exists with
 *     identical content is a no-op, so re-running a night never
 *     double-counts.
 */

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { validate } from '../contracts/validate.mjs';

export function gradesDir(root) {
  return join(root, 'data', 'grades');
}

export function monthFileName(date = new Date()) {
  const y = date.getUTCFullYear();
  const m = String(date.getUTCMonth() + 1).padStart(2, '0');
  return `${y}-${m}.jsonl`;
}

function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * Identity of a grade is its substance — every field except
 * `fetchedAt`, which records when THIS write happened, not what the
 * grade says. A nightly rebuild produces the same grades with a new
 * fetchedAt; those are duplicates, not corrections. A change to any
 * substantive field (probability, result, Brier, …) still supersedes.
 */
function sameSubstance(a, b) {
  const { fetchedAt: _a, ...restA } = a;
  const { fetchedAt: _b, ...restB } = b;
  return deepEqual(restA, restB);
}

/**
 * Read the whole ledger under `root`, folded by id (later lines win).
 * Returns { records: Map<id, record>, lines: number, files: string[] }.
 * A missing grades directory is an empty ledger, not an error.
 */
export function loadLedger(root) {
  const dir = gradesDir(root);
  const records = new Map();
  let lines = 0;
  let files = [];
  if (existsSync(dir)) {
    files = readdirSync(dir)
      .filter((f) => /^\d{4}-\d{2}\.jsonl$/.test(f))
      .sort();
    for (const file of files) {
      const text = readFileSync(join(dir, file), 'utf8');
      for (const line of text.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const record = JSON.parse(trimmed);
        lines += 1;
        records.set(record.id, record);
      }
    }
  }
  return { records, lines, files };
}

/** Validate one record at envelope level; throws with named errors. */
export function assertValidGradeRecord(record) {
  const result = validate('grade-record', record);
  if (!result.ok) {
    throw new Error(
      `GradeRecord ${record?.id ?? '<no id>'} fails the contract: ${result.errors.join('; ')}`
    );
  }
}

/**
 * Append records to the ledger. All records are validated first; any
 * failure aborts the append before anything is written.
 *
 * @returns {{ appended: number, skipped: number, superseded: number,
 *             file: string|null }}
 */
export function appendRecords(root, records, { now = new Date() } = {}) {
  for (const record of records) assertValidGradeRecord(record);

  const { records: existing } = loadLedger(root);
  const toAppend = [];
  let skipped = 0;
  let superseded = 0;
  for (const record of records) {
    const prior = existing.get(record.id);
    if (prior && sameSubstance(prior, record)) {
      skipped += 1;
      continue;
    }
    if (prior) superseded += 1;
    toAppend.push(record);
    existing.set(record.id, record);
  }

  if (!toAppend.length) {
    return { appended: 0, skipped, superseded, file: null };
  }
  const dir = gradesDir(root);
  mkdirSync(dir, { recursive: true });
  const file = join(dir, monthFileName(now));
  appendFileSync(
    file,
    toAppend.map((r) => JSON.stringify(r)).join('\n') + '\n',
    'utf8'
  );
  return { appended: toAppend.length, skipped, superseded, file };
}
