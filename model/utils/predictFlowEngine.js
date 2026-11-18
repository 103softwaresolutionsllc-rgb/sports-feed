/**
 * Sport-aware predict-flow-v2 probability engine.
 * Closed-form win probability + live Brownian margin update.
 */

import { getLeagueConfig } from './leagueConfig.js';

export function erf(x) {
  const sign = x < 0 ? -1 : 1;
  x = Math.abs(x);
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;
  const t = 1 / (1 + p * x);
  const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
  return sign * y;
}

export const normCDF = (z) => 0.5 * (1 + erf(z / Math.SQRT2));

export function winProb(diff, muPerMin, sigmaPerMin, tau) {
  if (tau <= 1e-6) return diff > 0 ? 1 : diff < 0 ? 0 : 0.5;
  const z = (diff + muPerMin * tau) / (sigmaPerMin * Math.sqrt(tau));
  return normCDF(z);
}

export function gaussianRandom() {
  let u = 0;
  let v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** Clock / scoring config per sport (not hard-coded basketball only). */
export const SPORT_CLOCK = {
  Basketball: {
    gameMin: 48,
    periodMin: 12,
    periods: 4,
    periodLabel: 'Q',
    // Typical combined score ~210–230
    scoreEvents: [
      { pts: 2, weight: 0.72 },
      { pts: 3, weight: 0.28 },
    ],
    expectedCombinedScore: 220,
    homeEdge: 2.8,
    sigma: 11.5,
    jumpDefaults: { period: 1, minLeft: 11, secLeft: 30 },
  },
  Football: {
    gameMin: 60,
    periodMin: 15,
    periods: 4,
    periodLabel: 'Q',
    // Typical combined score ~40–50 (FG / TD)
    scoreEvents: [
      { pts: 3, weight: 0.45 },
      { pts: 6, weight: 0.2 },
      { pts: 7, weight: 0.35 },
    ],
    expectedCombinedScore: 45,
    homeEdge: 2.5,
    sigma: 13.5,
    jumpDefaults: { period: 1, minLeft: 14, secLeft: 30 },
  },
  Hockey: {
    gameMin: 60,
    periodMin: 20,
    periods: 3,
    periodLabel: 'P',
    // Typical combined goals ~5–7
    scoreEvents: [{ pts: 1, weight: 1 }],
    expectedCombinedScore: 6,
    homeEdge: 0.25,
    sigma: 2.8,
    jumpDefaults: { period: 1, minLeft: 19, secLeft: 30 },
  },
  Baseball: {
    gameMin: 54,
    periodMin: 6,
    periods: 9,
    periodLabel: 'Inn',
    // Typical combined runs ~8–10
    scoreEvents: [
      { pts: 1, weight: 0.7 },
      { pts: 2, weight: 0.18 },
      { pts: 3, weight: 0.08 },
      { pts: 4, weight: 0.04 },
    ],
    expectedCombinedScore: 9,
    homeEdge: 0.15,
    sigma: 3.2,
    jumpDefaults: { period: 1, minLeft: 5, secLeft: 0 },
  },
  MMA: {
    gameMin: 15,
    periodMin: 5,
    periods: 3,
    periodLabel: 'R',
    // Sparse fight-scoring ticks (not football-style points)
    scoreEvents: [{ pts: 1, weight: 1 }],
    expectedCombinedScore: 8,
    homeEdge: 0,
    sigma: 4.5,
    jumpDefaults: { period: 1, minLeft: 4, secLeft: 30 },
  },
};

export function getSportClock(sport) {
  return SPORT_CLOCK[sport] || SPORT_CLOCK.Basketball;
}

const INJURY_LEVEL = { None: 0, Low: 1, Med: 2, High: 3 };

export function injuryToLevel(injuryImpact) {
  if (typeof injuryImpact === 'number' && Number.isFinite(injuryImpact)) {
    return Math.min(3, Math.max(0, Math.round(injuryImpact)));
  }
  return INJURY_LEVEL[injuryImpact] ?? 0;
}

export function formFromStreak(streak) {
  if (!Array.isArray(streak)) return 50;
  const filled = streak.filter((s) => s === 'W' || s === 'L' || s === 'w' || s === 'l');
  if (!filled.length) return 50;
  const wins = filled.filter((s) => s === 'W' || s === 'w').length;
  return Math.round((wins / filled.length) * 100);
}

/**
 * Map TeamSelectorCard / seeded team value into engine team inputs.
 */
export function teamCardToEngineInputs(card, sport) {
  const cfg = getLeagueConfig(sport);
  const offenseAvg = cfg.teamStats?.offense?.avg ?? 112;
  const defenseAvg = cfg.teamStats?.defense?.avg ?? 112;

  let ppg = parseFloat(card?.points);
  let oppPpg = parseFloat(card?.opp_ppg);

  if (sport === 'Baseball') {
    if (!Number.isFinite(ppg)) ppg = parseFloat(card?.points);
    if (!Number.isFinite(oppPpg)) oppPpg = parseFloat(card?.era);
  }
  if (sport === 'Hockey' && !Number.isFinite(oppPpg)) {
    oppPpg = parseFloat(card?.opp_ppg);
  }
  if (sport === 'MMA') {
    if (!Number.isFinite(ppg)) ppg = parseFloat(card?.points);
    if (!Number.isFinite(oppPpg)) oppPpg = parseFloat(card?.opp_ppg) || defenseAvg;
  }

  if (!Number.isFinite(ppg)) ppg = offenseAvg;
  if (!Number.isFinite(oppPpg)) oppPpg = defenseAvg;

  const restRaw = card?.restDays != null ? Number(card.restDays) : 2;
  const rest = Number.isFinite(restRaw) ? Math.min(4, Math.max(0, restRaw)) : 2;

  return {
    name: card?.name || '',
    ppg,
    oppPpg,
    form: formFromStreak(card?.streak),
    rest,
    injury: injuryToLevel(card?.injuryImpact || 'None'),
    logo: card?.logo,
    logoFallback: card?.logoFallback,
    color: card?.color,
    abbr: card?.abbr,
  };
}

export function computePreGameModel(t1, t2, homeAway, sport = 'Basketball') {
  const clock = getSportClock(sport);
  const ppgEdge = t1.ppg - t1.oppPpg - (t2.ppg - t2.oppPpg);
  const formEdge = (t1.form - t2.form) * 0.12;
  const restEdge = (t1.rest - t2.rest) * 0.4;
  const injEdge = (t2.injury - t1.injury) * 3.5;
  const homeEdgePts = clock.homeEdge ?? 2.8;
  const homeEdge = homeAway === 'home' ? homeEdgePts : homeAway === 'away' ? -homeEdgePts : 0;
  const mu = ppgEdge + formEdge + restEdge + injEdge + homeEdge;
  const sigma = clock.sigma;
  const z = mu / sigma;
  return { mu, sigma, z, prob: normCDF(z), gameMin: clock.gameMin };
}

export function impliedProbFromAmerican(odds) {
  const o = parseFloat(odds);
  if (Number.isNaN(o)) return null;
  return o > 0 ? 100 / (o + 100) : Math.abs(o) / (Math.abs(o) + 100);
}

/** Profit per $1 staked (American odds). */
export function profitPerDollarAmerican(odds) {
  const o = parseFloat(odds);
  if (Number.isNaN(o) || o === 0) return null;
  return o > 0 ? o / 100 : 100 / Math.abs(o);
}

/**
 * Core payout EV — expected profit, not gross payout.
 * @returns {{ profit, payout, loss, ev, evPct, implied, edge, valid, hasWinProb, signal, hint }}
 */
export function computeBetEV({ stake, odds, winProbability }) {
  const s = parseFloat(stake);
  const o = parseFloat(odds);
  const b = profitPerDollarAmerican(o);

  if (Number.isNaN(s) || s <= 0 || b == null) {
    return {
      profit: null,
      payout: null,
      loss: null,
      ev: null,
      evPct: null,
      implied: null,
      edge: null,
      valid: false,
      hasWinProb: false,
      signal: 'neutral',
      hint: 'Enter valid odds and stake to calculate payout EV.',
    };
  }

  const profit = s * b;
  const payout = s + profit;
  const loss = s;
  const implied = impliedProbFromAmerican(o);
  const p =
    winProbability != null && Number.isFinite(winProbability)
      ? Math.min(1, Math.max(0, winProbability))
      : null;
  const hasWinProb = p != null;
  const ev = hasWinProb ? p * profit - (1 - p) * s : null;
  const evPct = hasWinProb && s > 0 ? (ev / s) * 100 : null;
  const edge = hasWinProb && implied != null ? p - implied : null;

  let signal = 'neutral';
  let hint = 'Enter model probability to calculate payout EV.';
  if (hasWinProb) {
    if (ev >= 0) {
      signal = 'positive';
      hint =
        p < implied
          ? 'Positive EV — long-shot odds outweigh the model’s low win rate; loss (−stake) is still weighted on most outcomes.'
          : 'Positive EV — model signal vs the book (informational).';
    } else {
      signal = 'negative';
      hint =
        p < (implied ?? 0.5)
          ? 'Negative EV — model expects this side to lose more often than the payout compensates; includes −stake on those losses.'
          : 'Negative EV — book-favored on this line (informational).';
    }
  }

  return {
    profit,
    payout,
    loss,
    ev,
    evPct,
    implied,
    edge,
    valid: true,
    hasWinProb,
    signal,
    hint,
  };
}

export function expectedValue(stake, odds, winProbability) {
  const result = computeBetEV({ stake, odds, winProbability });
  return result.hasWinProb ? result.ev : null;
}

/**
 * Spread cover probability. Line is signed from the picked team's perspective (-3.5 = favored by 3.5).
 * Margin mu is Team 1 expected margin.
 */
export function spreadCoverProb({ mu, sigma, spreadLine, pickSide = 't1' }) {
  const line = parseFloat(spreadLine);
  const sig = parseFloat(sigma);
  if (Number.isNaN(line) || Number.isNaN(sig) || sig <= 0 || Number.isNaN(mu)) return null;

  if (pickSide === 't2') {
    return normCDF((line - mu) / sig);
  }
  return normCDF((mu + line) / sig);
}

/** Live spread cover probability (Team 1 margin perspective). */
export function spreadCoverProbLive({
  currentMargin,
  spreadLine,
  pickSide,
  muPerMin,
  sigmaPerMin,
  tau,
}) {
  const line = parseFloat(spreadLine);
  if (Number.isNaN(line) || tau == null) return null;
  const m = Number(currentMargin) || 0;

  if (pickSide === 't2') {
    return 1 - winProb(m - line, muPerMin, sigmaPerMin, tau);
  }
  return winProb(m + line, muPerMin, sigmaPerMin, tau);
}

/** Over/under side probability from projected team scoring inputs. */
export function totalSideProb({ t1ppg, t2ppg, sigma, totalLine, side = 'over' }) {
  const n = parseFloat(totalLine);
  const sig = parseFloat(sigma);
  const p1 = parseFloat(t1ppg);
  const p2 = parseFloat(t2ppg);
  if (Number.isNaN(n) || Number.isNaN(sig) || sig <= 0 || Number.isNaN(p1) || Number.isNaN(p2)) {
    return null;
  }

  const projectedTotal = p1 + p2;
  const sigmaTotal = sig * 1.35;
  const pOver = normCDF((projectedTotal - n) / sigmaTotal);
  return side === 'under' ? 1 - pOver : pOver;
}

export function formatMarketContext({
  betType,
  pickName,
  spreadLine,
  totalLine,
  totalSide,
}) {
  if (betType === 'spread') {
    const line = parseFloat(spreadLine);
    if (Number.isNaN(line) || !pickName) return null;
    const favored =
      line < 0 ? `favored by ${Math.abs(line)}` : line > 0 ? `getting ${line} pts` : 'pick em';
    const lineStr = line > 0 ? `+${line}` : String(line);
    return `${pickName} ${lineStr} (${favored})`;
  }
  if (betType === 'total') {
    const n = parseFloat(totalLine);
    if (Number.isNaN(n)) return null;
    const sideLabel = totalSide === 'under' ? 'Under' : 'Over';
    return `${sideLabel} ${n}`;
  }
  if (betType === 'ml' && pickName) {
    return `${pickName} moneyline`;
  }
  return null;
}

const PROB_LABELS = {
  ml: 'Win probability',
  spread: 'Cover probability',
};

function totalProbLabel(totalSide) {
  return totalSide === 'under' ? 'Under probability' : 'Over probability';
}

/**
 * Resolve model win probability for the active market type.
 */
export function resolveMarketWinProbability({
  marketType = 'ml',
  pickSide = 't1',
  mlProb,
  preGame,
  live,
  spreadLine,
  totalLine,
  totalSide,
  t1ppg,
  t2ppg,
}) {
  if (marketType === 'spread') {
    if (live) {
      return spreadCoverProbLive({
        currentMargin: live.currentMargin ?? live.home - live.away,
        spreadLine,
        pickSide,
        muPerMin: live.muPerMin,
        sigmaPerMin: live.sigmaPerMin,
        tau: Math.max(live.gameMin - live.t, 0),
      });
    }
    if (preGame) {
      return spreadCoverProb({
        mu: preGame.mu,
        sigma: preGame.sigma,
        spreadLine,
        pickSide,
      });
    }
    return null;
  }

  if (marketType === 'total') {
    const sigma = preGame?.sigma ?? live?.sigmaPerMin * Math.sqrt(live?.gameMin || 1);
    return totalSideProb({
      t1ppg,
      t2ppg,
      sigma,
      totalLine,
      side: totalSide || 'over',
    });
  }

  return mlProb;
}

/**
 * Top-level bet slip metrics for Predict Flow UI.
 */
export function computeBetSlipMetrics({
  stake,
  odds,
  marketType = 'ml',
  spreadLine,
  totalLine,
  totalSide = 'over',
  pickSide = 't1',
  pickName,
  mlProb,
  preGame,
  live,
  t1ppg,
  t2ppg,
}) {
  const modelProb = resolveMarketWinProbability({
    marketType: marketType || 'ml',
    pickSide,
    mlProb,
    preGame,
    live,
    spreadLine,
    totalLine,
    totalSide,
    t1ppg,
    t2ppg,
  });

  const payout = computeBetEV({ stake, odds, winProbability: modelProb });
  const marketLabel = formatMarketContext({
    betType: marketType || 'ml',
    pickName,
    spreadLine,
    totalLine,
    totalSide,
  });
  const probLabel =
    (marketType || 'ml') === 'total'
      ? totalProbLabel(totalSide || 'over')
      : PROB_LABELS[marketType || 'ml'] || PROB_LABELS.ml;

  return {
    marketType: marketType || 'ml',
    marketLabel,
    probLabel,
    modelProb,
    modelProbPct: modelProb != null ? modelProb * 100 : null,
    ...payout,
  };
}

export function elapsedFromJump(period, minLeft, secLeft, sport) {
  const clock = getSportClock(sport);
  const periodElapsed =
    (period - 1) * clock.periodMin +
    (clock.periodMin - (minLeft + secLeft / 60));
  return Math.min(Math.max(periodElapsed, 0), clock.gameMin);
}

export function formatGameClock(tElapsed, sport) {
  const clock = getSportClock(sport);
  const rem = Math.max(clock.gameMin - tElapsed, 0);
  if (tElapsed >= clock.gameMin) return '00:00';
  const m = Math.floor(rem % clock.periodMin);
  const s = Math.floor((rem % 1) * 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function currentPeriod(tElapsed, sport) {
  const clock = getSportClock(sport);
  if (tElapsed >= clock.gameMin) return clock.periods;
  return Math.min(clock.periods, Math.floor(tElapsed / clock.periodMin) + 1);
}

export function periodDisplay(tElapsed, sport) {
  const clock = getSportClock(sport);
  if (tElapsed >= clock.gameMin) return 'Final';
  return `${clock.periodLabel}${currentPeriod(tElapsed, sport)}`;
}

function normalizeScoreEvents(clock) {
  const raw = clock.scoreEvents || [];
  if (!raw.length) return [];
  // Support legacy [2, 3] and weighted [{ pts, weight }]
  if (typeof raw[0] === 'number') {
    return raw.map((pts) => ({ pts, weight: 1 / raw.length }));
  }
  const totalWeight = raw.reduce((sum, e) => sum + (e.weight || 0), 0) || 1;
  return raw.map((e) => ({ pts: e.pts, weight: (e.weight || 0) / totalWeight }));
}

function averageScoreEventValue(clock) {
  const events = normalizeScoreEvents(clock);
  if (!events.length) return 1;
  return events.reduce((sum, e) => sum + e.pts * e.weight, 0);
}

/**
 * Pick points for this sim tick using sport-realistic scoring frequency.
 * Probability is derived from expectedCombinedScore so hockey stays ~6 goals,
 * football ~45 points, basketball ~220, etc.
 */
export function pickSimScoreEvent(clock, dt = 0.3) {
  const events = normalizeScoreEvents(clock);
  if (!events.length) return 0;

  const avgPts = averageScoreEventValue(clock) || 1;
  const expected = clock.expectedCombinedScore ?? 200;
  const ticks = Math.max(clock.gameMin / dt, 1);
  const scoreRate = Math.min(0.95, Math.max(0, expected / (ticks * avgPts)));

  if (Math.random() >= scoreRate) return 0;

  let roll = Math.random();
  for (const event of events) {
    roll -= event.weight;
    if (roll <= 0) return event.pts;
  }
  return events[events.length - 1].pts;
}

/** Manual “score a play” button values for the live UI. */
export function getManualScoreButtons(sport) {
  const clock = getSportClock(sport);
  const events = normalizeScoreEvents(clock);
  const unique = [...new Set(events.map((e) => e.pts))].sort((a, b) => a - b);
  return unique.length ? unique : [1];
}

export const MODE_LABEL = {
  tipoff: 'Live Tracking',
  simulate: 'Simulation',
  jumpin: 'Live — joined in progress',
};

export const FLOW_ACCENTS = {
  blue: '#2563EB',
  purple: '#7C3AED',
  orange: '#F97316',
  red: '#DC2626',
  green: '#16A34A',
  navy: '#0B1120',
};

export const MODE_COLOR = {
  tipoff: FLOW_ACCENTS.blue,
  simulate: FLOW_ACCENTS.orange,
  jumpin: FLOW_ACCENTS.green,
};
