/**
 * report.mjs — calibration reports from the ledger (spec §5.1).
 *
 * Reports are generated artifacts: regenerated from the ledger on
 * every grading run, never edited by hand. Streams are reported in
 * separate sections with their honesty labels (records.mjs
 * STREAM_LABELS); there is no blended total anywhere in the output.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { summarize } from './math.mjs';
import { STREAM_LABELS } from './records.mjs';
import { gradesDir } from './store.mjs';

export function buildReport(records, { generatedAt = new Date().toISOString() } = {}) {
  const grouped = summarize(records);
  const streams = {};
  for (const [stream, sports] of Object.entries(grouped)) {
    streams[stream] = {
      label: STREAM_LABELS[stream] || stream,
      sports,
    };
  }
  return {
    contract: 'grading-report',
    v: 1,
    generatedAt,
    recordCount: records.length,
    streams,
  };
}

function fmt(x, digits = 4) {
  return x == null ? '—' : Number(x).toFixed(digits);
}

export function renderMarkdown(report, { sourceHealthMd = null } = {}) {
  const lines = [];
  lines.push('# Grading ledger — calibration report');
  lines.push('');
  lines.push(`Generated ${report.generatedAt} from ${report.recordCount} ledger records.`);
  lines.push('');
  lines.push(
    'Streams are reported separately and are never blended. ' +
      'The book-implied streams grade the market\u2019s prices, not the model.'
  );
  const streamNames = Object.keys(report.streams);
  if (!streamNames.length) {
    lines.push('');
    lines.push('No grades yet.');
    if (sourceHealthMd) lines.push('', sourceHealthMd.trimEnd());
    return lines.join('\n') + '\n';
  }
  for (const stream of streamNames) {
    const { label, sports } = report.streams[stream];
    lines.push('');
    lines.push(`## ${stream} — ${label}`);
    for (const [sport, s] of Object.entries(sports)) {
      lines.push('');
      lines.push(`### ${sport}`);
      lines.push('');
      lines.push(
        `Sample: ${s.n} graded picks (${s.wins}W / ${s.losses}L / ${s.pushes}P` +
          (s.probMissing ? `, ${s.probMissing} without a recorded probability` : '') +
          `). Brier: **${fmt(s.brier)}** over ${s.brierN} probability-carrying grades.`
      );
      if (s.buckets.length) {
        lines.push('');
        lines.push('| Bucket (favored side) | n | Mean predicted | Actual win rate | Deviation |');
        lines.push('|---|---|---|---|---|');
        for (const b of s.buckets) {
          lines.push(
            `| ${b.range} | ${b.n} | ${fmt(b.meanPredicted, 3)} | ${fmt(b.actualRate, 3)} | ${fmt(b.deviation, 3)} |`
          );
        }
      }
    }
  }
  // Source health (Phase E): the nightly rollup's flags, rendered by
  // run.mjs from grading/health-rollup.mjs. This report is the
  // apparatus's daily summary surface, so the flags live here.
  if (sourceHealthMd) lines.push('', sourceHealthMd.trimEnd());
  return lines.join('\n') + '\n';
}

/** Regenerate <root>/data/grades/reports/latest.{json,md}.
 *  opts.sourceHealthMd (Phase E) appends the Source health section. */
export function writeReports(root, records, opts = {}) {
  const report = buildReport(records, opts);
  const dir = join(gradesDir(root), 'reports');
  mkdirSync(dir, { recursive: true });
  const jsonPath = join(dir, 'latest.json');
  const mdPath = join(dir, 'latest.md');
  writeFileSync(jsonPath, JSON.stringify(report, null, 2) + '\n', 'utf8');
  writeFileSync(mdPath, renderMarkdown(report, opts), 'utf8');
  return { report, jsonPath, mdPath };
}
