/**
 * Monte Carlo game-outcome simulation for the Predict run view.
 *
 * This is a TRUE repeated simulation — it draws thousands of final margins
 * (and final scores) from the same distribution the closed-form model uses,
 * then reports empirical rates. It is the only thing in the app that may be
 * labeled "Monte Carlo" for game outcomes. (The bet-slip EV simulator in
 * betIntelligenceEngine.js simulates bet outcomes, a different thing.)
 *
 * Inputs mirror the live engine state:
 *   final margin ~ Normal(currentDiff + muPerMin * tau, sigmaPerMin * sqrt(tau))
 * Remaining combined score is simulated with the same per-minute sigma scaled
 * by 1.35, matching the totalSideProb convention in predictFlowEngine.js.
 * Margin and total are drawn independently (v1 simplification).
 */

/** Deterministic RNG (mulberry32) so a given game state renders stable numbers. */
export function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussian(rng) {
  let u = 0;
  let v = 0;
  while (u === 0) u = rng();
  while (v === 0) v = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function percentile(sorted, p) {
  if (!sorted.length) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(p * sorted.length)));
  return sorted[idx];
}

/**
 * @param {object} args
 * @param {number} args.currentHome   current score, team 1 (perspective team)
 * @param {number} args.currentAway   current score, team 2
 * @param {number} args.muPerMin      expected margin drift per minute (team 1 perspective)
 * @param {number} args.sigmaPerMin   margin volatility per sqrt-minute
 * @param {number} args.tau           minutes remaining
 * @param {number} args.gameMin       full game minutes (for remaining-score expectation)
 * @param {number} args.expectedCombinedScore  sport's typical combined score
 * @param {number} [args.spreadLine]  signed line from the PICKED team's perspective (-3.5 = favored)
 * @param {'t1'|'t2'} [args.pickSide]
 * @param {number} [args.totalLine]   over/under line
 * @param {number} [args.sims]        simulation count (default 10000, capped 50000)
 * @param {number} [args.seed]        RNG seed (default derived from game state)
 */
export function simulateGameMonteCarlo({
  currentHome,
  currentAway,
  muPerMin,
  sigmaPerMin,
  tau,
  gameMin,
  expectedCombinedScore,
  spreadLine = null,
  pickSide = 't1',
  totalLine = null,
  sims = 10000,
  seed = null,
}) {
  const n = Math.max(100, Math.min(50000, Math.floor(sims) || 10000));
  const home0 = Number(currentHome) || 0;
  const away0 = Number(currentAway) || 0;
  const diff = home0 - away0;
  const t = Math.max(0, tau || 0);

  const meanMargin = diff + (muPerMin || 0) * t;
  const sdMargin = (sigmaPerMin || 0) * Math.sqrt(t);
  const expectedRemaining = (expectedCombinedScore || 0) * (gameMin > 0 ? t / gameMin : 0);
  const sdRemainingTotal = 1.35 * (sigmaPerMin || 0) * Math.sqrt(t);

  const effectiveSeed =
    seed != null
      ? seed
      : Math.round((meanMargin + 500) * 1000) * 31 +
        Math.round(sdMargin * 1000) * 7 +
        Math.round(t * 60) +
        home0 * 131 +
        away0 * 17 +
        n;
  const rng = mulberry32(effectiveSeed);

  const line = spreadLine != null && spreadLine !== '' ? parseFloat(spreadLine) : null;
  const oU = totalLine != null && totalLine !== '' ? parseFloat(totalLine) : null;

  const margins = new Array(n);
  const homeFinals = new Array(n);
  const awayFinals = new Array(n);
  let wins = 0;
  let covers = 0;
  let overs = 0;

  for (let i = 0; i < n; i++) {
    const margin = meanMargin + sdMargin * gaussian(rng);
    const remainingTotal = Math.max(0, expectedRemaining + sdRemainingTotal * gaussian(rng));
    const totalFinal = home0 + away0 + remainingTotal;
    let hf = (totalFinal + margin) / 2;
    let af = (totalFinal - margin) / 2;
    if (hf < home0) hf = home0;
    if (af < away0) af = away0;
    const homeI = Math.round(hf);
    const awayI = Math.round(af);
    const finalMargin = homeI - awayI;

    margins[i] = finalMargin;
    homeFinals[i] = homeI;
    awayFinals[i] = awayI;

    if (homeI > awayI) wins += 1;
    else if (homeI === awayI) wins += 0.5;

    if (line != null && Number.isFinite(line)) {
      // Line is signed from the picked team's perspective (see spreadCoverProb).
      const coverMargin = pickSide === 't2' ? line - finalMargin : finalMargin + line;
      if (coverMargin > 0) covers += 1;
      else if (coverMargin === 0) covers += 0.5;
    }
    if (oU != null && Number.isFinite(oU)) {
      const tot = homeI + awayI;
      if (tot > oU) overs += 1;
      else if (tot === oU) overs += 0.5;
    }
  }

  const sortedMargins = [...margins].sort((a, b) => a - b);
  const sortedHome = [...homeFinals].sort((a, b) => a - b);
  const sortedAway = [...awayFinals].sort((a, b) => a - b);

  // Final-margin distribution buckets for the chart (13 buckets across the range).
  const lo = percentile(sortedMargins, 0.005);
  const hi = percentile(sortedMargins, 0.995);
  const span = Math.max(hi - lo, 1);
  const bucketCount = 13;
  const bucketWidth = span / bucketCount;
  const buckets = new Array(bucketCount).fill(0);
  for (const m of margins) {
    const idx = Math.min(bucketCount - 1, Math.max(0, Math.floor((m - lo) / bucketWidth)));
    buckets[idx] += 1;
  }
  const distribution = buckets.map((count, i) => ({
    center: Math.round(lo + bucketWidth * (i + 0.5)),
    pct: (count / n) * 100,
  }));

  return {
    sims: n,
    winPct: (wins / n) * 100,
    coverPct: line != null && Number.isFinite(line) ? (covers / n) * 100 : null,
    overPct: oU != null && Number.isFinite(oU) ? (overs / n) * 100 : null,
    medianMargin: percentile(sortedMargins, 0.5),
    marginP10: percentile(sortedMargins, 0.1),
    marginP90: percentile(sortedMargins, 0.9),
    homeRange: [percentile(sortedHome, 0.1), percentile(sortedHome, 0.9)],
    awayRange: [percentile(sortedAway, 0.1), percentile(sortedAway, 0.9)],
    distribution,
    meanMargin,
    sdMargin,
  };
}
