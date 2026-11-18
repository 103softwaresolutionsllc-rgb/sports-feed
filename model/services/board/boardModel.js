/**
 * boardModel.js — the model's read on a board game + tappable price cells
 * (Sportsbook Predict remake, Phase 3). PURE functions only.
 *
 * The model itself does not change (§2.1): inputs come from
 * boardEngineInputs (Intelligence Engine aggregates; the no-stats gate
 * withholds the whole read), probabilities and EV come from the existing
 * predictFlowEngine definitions (computePreGameModel / computeBetSlipMetrics
 * / computeBetEV), and the Monte Carlo panel runs the existing
 * simulateGameMonteCarlo from the pre-game (t = 0) state.
 *
 * HONESTY RULES enforced here:
 *   - No aggregates for either team ⇒ NO model read at all (null). Prices
 *     still render; the UI shows the plain "Model doesn't have this
 *     matchup yet" note. No league-average fallback is ever dressed up.
 *   - A model-derived FAIR price is computed from the model probability
 *     and labeled as the model's — it is never placed inside a book price
 *     cell as if a book offered it (§7.5).
 *   - Edge is model % − implied %, per computeBetEV, everywhere.
 */

import {
  computePreGameModel,
  computeBetSlipMetrics,
  getSportClock,
} from '../../utils/predictFlowEngine.js';
import { simulateGameMonteCarlo } from '../../utils/monteCarloSim.js';
import { buildMatchupModelInputs, teamAggregateFromDoc } from './boardEngineInputs.js';

/**
 * Rest days for a team from a schedule of games (board/slate entries in
 * canonical shape): whole days between the team's most recent EARLIER
 * game in the schedule and this game's start. Null when the schedule
 * holds no earlier game for the team — the caller then leaves the
 * boardEngineInputs default (2) in place rather than inventing rest.
 */
export function restDaysFromSchedule(games, teamName, gameStartIso) {
  const startMs = Date.parse(gameStartIso);
  if (!Number.isFinite(startMs) || !Array.isArray(games)) return null;
  let prevMs = null;
  for (const g of games) {
    if (!g || (g.homeTeam !== teamName && g.awayTeam !== teamName)) continue;
    const ms = Date.parse(g.startTime);
    if (Number.isFinite(ms) && ms < startMs && (prevMs == null || ms > prevMs)) prevMs = ms;
  }
  if (prevMs == null) return null;
  return Math.floor((startMs - prevMs) / (24 * 60 * 60 * 1000));
}

/**
 * Build the model read for one board game, or null when the no-stats
 * gate withholds it.
 *
 * @param {object} game — canonical board game (boardContract shape).
 * @param {object|null} aggregatesDoc — the sport's Intelligence Engine
 *   aggregates document ({ summary, weighted_averages }), or null.
 * @param {object} [extras] — caller-supplied per-team inputs per
 *   boardEngineInputs: { home: {recentResults, restDays, injuryImpact},
 *   away: {...} }.
 * @returns {null|{ preGame, modelSpread, modelTotal, homeWinProb,
 *   awayWinProb, projectedMargin, sigma, seasonsHeld, classification,
 *   inputs }} — modelSpread is signed from the HOME perspective
 *   (−μ: negative = home favored, like a book line); modelTotal is the
 *   projected combined score the total probability is solved from.
 */
export function buildGameModel(game, aggregatesDoc, extras = {}) {
  if (!game || typeof game !== 'object') return null;
  const inputs = buildMatchupModelInputs({
    homeTeam: game.homeTeam,
    awayTeam: game.awayTeam,
    home: { aggregate: teamAggregateFromDoc(aggregatesDoc, game.homeTeam), ...(extras.home || {}) },
    away: { aggregate: teamAggregateFromDoc(aggregatesDoc, game.awayTeam), ...(extras.away || {}) },
    summary: aggregatesDoc?.summary ?? null,
  });
  if (!inputs) return null; // THE GATE — no stats, no model read.
  const preGame = computePreGameModel(inputs.team1, inputs.team2, inputs.homeAway, game.sport);
  return {
    preGame,
    projectedMargin: preGame.mu,
    sigma: preGame.sigma,
    modelSpread: -preGame.mu,
    modelTotal: inputs.team1.ppg + inputs.team2.ppg,
    homeWinProb: preGame.prob,
    awayWinProb: 1 - preGame.prob,
    seasonsHeld: inputs.seasonsHeld,
    classification: 'Estimated',
    inputsClassification: inputs.classification,
    inputs,
  };
}

/**
 * The Model read section's empty-state copy, state-aware (1.3.1): when
 * the game has NO line, the note must not claim prices are shown — a
 * no-line game's section says only that the model has no read. When the
 * game DOES carry prices, the fuller wording stays: prices are on
 * screen, without a model line beside them.
 */
export function modelReadEmptyNote(game) {
  const hasLine = Array.isArray(game?.lineSets) && game.lineSets.length > 0;
  return hasLine
    ? "The model doesn't have this matchup yet — prices are shown without a model line."
    : "The model doesn't have this matchup yet.";
}

/** Fair American price for a model probability (the price at EV = 0). */
export function fairAmericanPrice(probability) {
  const p = Number(probability);
  if (!Number.isFinite(p) || p <= 0 || p >= 1) return null;
  if (p >= 0.5) return -Math.round((p / (1 - p)) * 100);
  return Math.round(((1 - p) / p) * 100);
}

/**
 * One tappable price cell. `line` is the line the LEG locks: for spread,
 * signed from the PICKED team's perspective (the spreadCoverProb /
 * buildTeamLegSnapshot convention); for total, the total line; for
 * moneyline, null. `bookLine` keeps the board's home-perspective spread
 * line for display where needed.
 */
function makeCell(game, lineSet, model, { market, side, odds, line, bookLine }) {
  let metrics = null;
  if (model) {
    const pickSide = side === 'home' ? 't1' : side === 'away' ? 't2' : null;
    metrics = computeBetSlipMetrics({
      stake: 1,
      odds,
      marketType: market === 'ml' ? 'ml' : market,
      spreadLine: market === 'spread' ? line : undefined,
      totalLine: market === 'total' ? line : undefined,
      totalSide: market === 'total' ? side : 'over',
      pickSide: pickSide || 't1',
      pickName: side === 'home' ? game.homeTeam : side === 'away' ? game.awayTeam : null,
      mlProb: pickSide === 't2' ? model.awayWinProb : model.homeWinProb,
      preGame: model.preGame,
      t1ppg: model.inputs.team1.ppg,
      t2ppg: model.inputs.team2.ppg,
    });
  }
  const modelProb = metrics ? metrics.modelProb : null;
  return {
    key: `${lineSet.bookmaker}|${market}|${side}|${line ?? ''}`,
    market,
    side,
    odds,
    line,
    bookLine: bookLine ?? null,
    bookmaker: lineSet.bookmaker,
    fetchedAt: lineSet.fetchedAt,
    // Model overlay — null when the gate withholds the read. edge is
    // model − implied (a 0–1 fraction), per computeBetEV.
    modelProb,
    edge: metrics ? metrics.edge : null,
    evPct: metrics ? metrics.evPct : null,
    fairPrice: modelProb != null ? fairAmericanPrice(modelProb) : null,
    marketLabel: metrics ? metrics.marketLabel : null,
  };
}

/**
 * All price cells for one line set of a game, in book order:
 * spread (home, away), total (over, under), moneyline (home, away).
 * Markets the line set does not carry produce NO cells — never a guess.
 */
export function buildPriceCells(game, lineSet, model) {
  if (!game || !lineSet) return [];
  const cells = [];
  if (lineSet.spread) {
    const L = lineSet.spread.line; // signed from home perspective
    cells.push(makeCell(game, lineSet, model, {
      market: 'spread', side: 'home', odds: lineSet.spread.home, line: L, bookLine: L,
    }));
    cells.push(makeCell(game, lineSet, model, {
      market: 'spread', side: 'away', odds: lineSet.spread.away, line: -L, bookLine: L,
    }));
  }
  if (lineSet.total) {
    cells.push(makeCell(game, lineSet, model, {
      market: 'total', side: 'over', odds: lineSet.total.over, line: lineSet.total.line, bookLine: lineSet.total.line,
    }));
    cells.push(makeCell(game, lineSet, model, {
      market: 'total', side: 'under', odds: lineSet.total.under, line: lineSet.total.line, bookLine: lineSet.total.line,
    }));
  }
  if (lineSet.moneyline) {
    cells.push(makeCell(game, lineSet, model, {
      market: 'ml', side: 'home', odds: lineSet.moneyline.home, line: null, bookLine: null,
    }));
    cells.push(makeCell(game, lineSet, model, {
      market: 'ml', side: 'away', odds: lineSet.moneyline.away, line: null, bookLine: null,
    }));
  }
  return cells;
}

/**
 * Monte Carlo panel data for a game (§11.3: a panel in the game view),
 * run pre-game from the t = 0 state with the book's spread/total lines
 * from the given line set (either may be absent). Returns null when
 * there is no model read — the simulation runs from the model's state,
 * and a gated matchup has none.
 */
export function runGameMonteCarlo(game, model, lineSet) {
  if (!game || !model) return null;
  const clock = getSportClock(game.sport);
  const pre = model.preGame;
  return simulateGameMonteCarlo({
    currentHome: 0,
    currentAway: 0,
    muPerMin: pre.mu / pre.gameMin,
    sigmaPerMin: pre.sigma / Math.sqrt(pre.gameMin),
    tau: pre.gameMin,
    gameMin: pre.gameMin,
    expectedCombinedScore: model.modelTotal,
    spreadLine: lineSet?.spread ? lineSet.spread.line : null,
    pickSide: 't1',
    totalLine: lineSet?.total ? lineSet.total.line : null,
    sims: 10000,
  });
}

export default { buildGameModel, buildPriceCells, runGameMonteCarlo, fairAmericanPrice, restDaysFromSchedule, modelReadEmptyNote };
