/**
 * math.mjs — grading math for the ledger (Quant Apparatus spec §5.1).
 *
 * Pure functions over GradeRecords. Streams are NEVER blended: every
 * aggregation is per (stream, sport), where `stream` is the record's
 * contract `source` field.
 *
 * Brier: mean of brierContribution over records that carry a non-null
 * modelProb. Win contributes (p−1)², loss (p−0)², push (p−0.5)² — the
 * standard half-outcome scoring for a drawn binary event. Records with
 * modelProb: null (parlay grades, unrecorded legs) are counted in the
 * sample but excluded from Brier and calibration, never backfilled.
 *
 * Calibration (spec §5.1 buckets 0.5–0.6 … 0.9–1.0): probabilities are
 * folded to the favored side — p′ = max(p, 1−p), outcome′ = whether the
 * favored side won — so every graded pick lands in a 0.5–1.0 bucket.
 * Folding leaves Brier contributions unchanged ((p−y)² is symmetric),
 * so the calibration view and the Brier view describe the same grades.
 * Pushes are excluded from calibration (no side won) and counted
 * separately.
 */

export const CALIBRATION_BUCKETS = [
  { lo: 0.5, hi: 0.6, range: '0.5-0.6' },
  { lo: 0.6, hi: 0.7, range: '0.6-0.7' },
  { lo: 0.7, hi: 0.8, range: '0.7-0.8' },
  { lo: 0.8, hi: 0.9, range: '0.8-0.9' },
  { lo: 0.9, hi: 1.0, range: '0.9-1.0' },
];

export function brierContribution(modelProb, result) {
  if (modelProb == null) return null;
  if (result === 'win') return (modelProb - 1) ** 2;
  if (result === 'loss') return (modelProb - 0) ** 2;
  if (result === 'push') return (modelProb - 0.5) ** 2;
  return null;
}

export function calibrationBuckets(records) {
  const buckets = CALIBRATION_BUCKETS.map((b) => ({
    ...b,
    n: 0,
    predSum: 0,
    winSum: 0,
  }));
  for (const record of records) {
    const p = record.modelProb;
    if (p == null) continue;
    if (record.result !== 'win' && record.result !== 'loss') continue;
    const y = record.result === 'win' ? 1 : 0;
    const pFav = Math.max(p, 1 - p);
    const yFav = p >= 0.5 ? y : 1 - y;
    const idx = Math.min(
      buckets.length - 1,
      Math.max(0, Math.floor((pFav - 0.5) * 10))
    );
    buckets[idx].n += 1;
    buckets[idx].predSum += pFav;
    buckets[idx].winSum += yFav;
  }
  return buckets
    .filter((b) => b.n > 0)
    .map((b) => ({
      range: b.range,
      n: b.n,
      meanPredicted: b.predSum / b.n,
      actualRate: b.winSum / b.n,
      deviation: Math.abs(b.predSum / b.n - b.winSum / b.n),
    }));
}

/** Summarize one (stream, sport) group of GradeRecords. */
export function summarizeGroup(records) {
  let wins = 0;
  let losses = 0;
  let pushes = 0;
  let probMissing = 0;
  let brierSum = 0;
  let brierN = 0;
  for (const record of records) {
    if (record.result === 'win') wins += 1;
    else if (record.result === 'loss') losses += 1;
    else if (record.result === 'push') pushes += 1;
    if (record.modelProb == null) {
      probMissing += 1;
    } else if (record.brierContribution != null) {
      brierSum += record.brierContribution;
      brierN += 1;
    }
  }
  return {
    n: records.length,
    wins,
    losses,
    pushes,
    probMissing,
    brierN,
    brier: brierN ? brierSum / brierN : null,
    buckets: calibrationBuckets(records),
  };
}

/**
 * Group records by stream (`source`) then sport and summarize each
 * group. The returned shape keeps streams as separate top-level keys —
 * there is deliberately no cross-stream total.
 */
export function summarize(records) {
  const byStream = new Map();
  for (const record of records) {
    const stream = record.source;
    if (!byStream.has(stream)) byStream.set(stream, new Map());
    const bySport = byStream.get(stream);
    const sport = record.sport || 'unknown';
    if (!bySport.has(sport)) bySport.set(sport, []);
    bySport.get(sport).push(record);
  }
  const out = {};
  for (const [stream, bySport] of byStream) {
    out[stream] = {};
    for (const [sport, group] of bySport) {
      out[stream][sport] = summarizeGroup(group);
    }
  }
  return out;
}
