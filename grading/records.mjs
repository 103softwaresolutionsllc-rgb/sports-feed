/**
 * records.mjs — GradeRecord construction for the ledger's streams
 * (Quant Apparatus spec §5.1), plus the odds math the streams need.
 *
 * THE STREAMS (kept distinct in `source`, in reports, everywhere):
 *   - 'sim-backtest'        — the Simulation Lab's Phase 3 backtest
 *                             probabilities (class SIMULATED). The
 *                             acceptance stream: ledger math is proven
 *                             against it before live numbers are
 *                             trusted.
 *   - 'app-model'           — the BetTracker app's Gaussian board
 *                             probabilities (class Estimated), computed
 *                             by the canonical port in ../model from
 *                             archived feed editions.
 *   - 'book-implied'        — bookmaker-implied probabilities devigged
 *                             from archived line sets (class Live).
 *                             NOT the model. Exists so the model's
 *                             record can be compared against the
 *                             market's, never merged with it.
 *   - 'book-implied-pick-card' — the feed pick cards' published
 *                             winProb: the favorite's RAW implied
 *                             percentage (class Live), graded as
 *                             published. A separate stream from the
 *                             devigged line-set numbers because the
 *                             two are computed differently (gap G4).
 *
 * One graded pick per game per stream: the home moneyline (for the
 * pick-card stream, the card's favorite pick as published). Grading
 * both sides of the same probability would double-count one opinion.
 */

import { brierContribution } from './math.mjs';

export const STREAMS = {
  SIM_BACKTEST: 'sim-backtest',
  APP_MODEL: 'app-model',
  BOOK_IMPLIED: 'book-implied',
  BOOK_IMPLIED_PICK_CARD: 'book-implied-pick-card',
};

export const STREAM_LABELS = {
  'sim-backtest':
    'Simulation backtest stream (SIMULATED — Lab Phase 3 probabilities)',
  'app-model':
    "App model stream (the board's Gaussian probabilities, from archived editions)",
  'book-implied':
    'Book-implied stream (devigged bookmaker prices — NOT the model)',
  'book-implied-pick-card':
    'Book-implied pick-card stream (published winProb, raw implied — NOT the model)',
};

export function slug(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/** Stable per-game key for live-stream ids: league + start + matchup. */
export function gameKey({ league, startTs, away, home }) {
  return `${league}:${startTs}:${slug(away)}@${slug(home)}`;
}

/** Raw implied probability of an American price (vig included). */
export function impliedFromAmerican(odds) {
  const o = Number(odds);
  if (!Number.isFinite(o) || o === 0) return null;
  return o < 0 ? -o / (-o + 100) : 100 / (o + 100);
}

/** Devigged moneyline probabilities from one book's two prices. */
export function devigMoneyline(homeOdds, awayOdds) {
  const homeRaw = impliedFromAmerican(homeOdds);
  const awayRaw = impliedFromAmerican(awayOdds);
  if (homeRaw == null || awayRaw == null) return null;
  const total = homeRaw + awayRaw;
  if (!(total > 0)) return null;
  return { home: homeRaw / total, away: awayRaw / total };
}

/** Result of a home-side pick from a final score. */
export function resultFromScore(homeScore, awayScore) {
  if (homeScore > awayScore) return 'win';
  if (homeScore < awayScore) return 'loss';
  return 'push';
}

/**
 * Build one envelope-level GradeRecord for a graded pick.
 * `modelProb` is the graded probability whose origin `stream` names —
 * for the book-implied streams it is an implied probability, and the
 * stream label says so wherever the record is reported.
 */
export function makePickGrade({
  stream,
  recordClass,
  sport,
  league = null,
  gameKey: key,
  snapshotId,
  selectionPart,
  prediction,
  modelProb,
  result,
  settledAt,
  now,
}) {
  const id = `${stream}:${key}:${selectionPart}`;
  return {
    contract: 'grade-record',
    v: 1,
    class: recordClass,
    source: stream,
    fetchedAt: now,
    id,
    subjectKind: 'pick',
    snapshotId,
    selectionId: `${key}:${selectionPart}`,
    result,
    modelProb: modelProb ?? null,
    settledAt,
    sport,
    league,
    prediction,
    brierContribution: brierContribution(modelProb ?? null, result),
  };
}
