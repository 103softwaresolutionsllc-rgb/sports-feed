/**
 * boardEngineInputs.js — Intelligence Engine aggregates → prediction-model
 * inputs (Sportsbook Predict remake, Phase 2). PURE functions only.
 *
 * The mapping is §2.1 of the sportsbook remake spec. The prediction model
 * itself (predictFlowEngine) does NOT change; what changes is where its
 * inputs come from. Today `teamCardToEngineInputs` reads a card the user
 * typed, and silently substitutes league averages (offense/defense `avg`)
 * for anything missing. This mapping NEVER does that:
 *
 *   THE NO-STATS GATE (binding): a team with no aggregate stats gets NO
 *   model read. `aggregateTeamToEngineInputs` returns null and
 *   `buildMatchupModelInputs` returns null for the whole matchup — the
 *   board still shows the book's prices; the model line and edges simply
 *   don't render. League-average numbers are never dressed up as a
 *   team-specific read.
 *
 * Input provenance (§2.1 table):
 *   - Scoring / opponent averages ← aggregates' recency-weighted averages
 *     (`weighted_averages` in the Engine's per-sport aggregates document):
 *     `points_for_per_game` / `points_against_per_game`, preferring the
 *     home/away split pair (`home_*` for the home team, `away_*` for the
 *     away team) when BOTH split values are held, else the overall pair.
 *     Classification: Historical, with the seasons held carried through
 *     from the aggregates summary so surfaces can state them.
 *   - Form ← the team's recent-results record, mapped through the SAME
 *     `formFromStreak` the card adapter uses. SPEC GAP (named, not
 *     papered over): the Engine's aggregates document as published today
 *     carries no recent-results record, so until it does, callers pass
 *     none and form is `formFromStreak([])` — the engine's own neutral
 *     50 — never a record invented from a W-L string.
 *   - Rest days ← schedule-derived, supplied by the caller (days since
 *     the team's previous game, from feed/harvest schedule data); clamped
 *     exactly as `teamCardToEngineInputs` clamps (0–4, default 2).
 *   - Injury level ← the ESPN injury report's severity, supplied by the
 *     caller as an impact label and mapped by the existing
 *     `injuryToLevel` (the report itself is fetched by teamDataService;
 *     this module stays pure). Default 'None' ⇒ 0, as today.
 *   - Home / away ← the slate itself: the board game knows its home team.
 *
 * The returned object is exactly the shape `computePreGameModel` consumes
 * ({ name, ppg, oppPpg, form, rest, injury }) plus provenance fields the
 * engine ignores.
 */

import { formFromStreak, injuryToLevel } from '../../utils/predictFlowEngine.js';

const PF = 'points_for_per_game';
const PA = 'points_against_per_game';
const HOME_PF = 'home_points_for_per_game';
const HOME_PA = 'home_points_against_per_game';
const AWAY_PF = 'away_points_for_per_game';
const AWAY_PA = 'away_points_against_per_game';

function finitePositive(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function seasonsHeldOf(summary) {
  if (!summary || typeof summary !== 'object') return [];
  return Array.isArray(summary.seasons_held) ? summary.seasons_held.filter((s) => typeof s === 'string') : [];
}

/**
 * Look up one team's weighted-average record in a per-sport aggregates
 * document ({ summary, weighted_averages: { <team>: { <metric>: value } } }).
 * Exact name match only — no fuzzy substitution (canon: a suggestion is
 * never silently used as the team). Returns the record or null.
 */
export function teamAggregateFromDoc(aggregatesDoc, teamName) {
  if (!aggregatesDoc || typeof aggregatesDoc !== 'object') return null;
  const weighted = aggregatesDoc.weighted_averages;
  if (!weighted || typeof weighted !== 'object') return null;
  const record = weighted[teamName];
  return record && typeof record === 'object' ? record : null;
}

/**
 * Map ONE team's aggregates to engine inputs, or null when the gate
 * withholds the model read for this team.
 *
 * @param {object} args
 * @param {string} args.name — team name (display + engine `name`).
 * @param {object|null} args.aggregate — the team's weighted-average
 *   record (see teamAggregateFromDoc), or null when the Engine holds none.
 * @param {'home'|'away'|null} [args.homeAway] — split preference.
 * @param {string[]} [args.recentResults] — recent-results record
 *   ('W'/'L' entries) when a source publishes one; see header spec gap.
 * @param {number} [args.restDays] — schedule-derived rest, caller-supplied.
 * @param {string|number} [args.injuryImpact] — ESPN severity label/level.
 * @param {object} [args.summary] — the aggregates summary (seasons held).
 */
export function aggregateTeamToEngineInputs({
  name,
  aggregate,
  homeAway = null,
  recentResults = null,
  restDays = null,
  injuryImpact = null,
  summary = null,
} = {}) {
  if (!aggregate || typeof aggregate !== 'object') return null; // THE GATE

  let ppg = null;
  let oppPpg = null;
  let statsSource = 'overall';
  if (homeAway === 'home') {
    ppg = finitePositive(aggregate[HOME_PF]);
    oppPpg = finitePositive(aggregate[HOME_PA]);
    if (ppg != null && oppPpg != null) statsSource = 'home_split';
    else { ppg = null; oppPpg = null; }
  } else if (homeAway === 'away') {
    ppg = finitePositive(aggregate[AWAY_PF]);
    oppPpg = finitePositive(aggregate[AWAY_PA]);
    if (ppg != null && oppPpg != null) statsSource = 'away_split';
    else { ppg = null; oppPpg = null; }
  }
  if (ppg == null || oppPpg == null) {
    ppg = finitePositive(aggregate[PF]);
    oppPpg = finitePositive(aggregate[PA]);
    statsSource = 'overall';
  }
  // THE GATE: no held scoring averages ⇒ no model inputs for this team.
  // Never the league average, never the other split half-repaired.
  if (ppg == null || oppPpg == null) return null;

  const restRaw = restDays != null ? Number(restDays) : 2;
  const rest = Number.isFinite(restRaw) ? Math.min(4, Math.max(0, restRaw)) : 2;

  return {
    name: typeof name === 'string' ? name : '',
    ppg,
    oppPpg,
    form: formFromStreak(Array.isArray(recentResults) ? recentResults : []),
    rest,
    injury: injuryToLevel(injuryImpact ?? 'None'),
    classification: 'Historical',
    statsSource,
    seasonsHeld: seasonsHeldOf(summary),
  };
}

/**
 * Build the model inputs for one board matchup. Returns null when EITHER
 * team is gated out — a matchup model read needs both teams' aggregates
 * (§2.1: "Where a team has NO aggregate stats, the model read for that
 * game is withheld").
 *
 * On success returns { team1, team2, homeAway: 'home', classification }
 * shaped for the existing engine call:
 *   computePreGameModel(m.team1, m.team2, m.homeAway, sport)
 * with team1 = the home team (the slate knows its home team, §2.1).
 * Each side accepts the same per-team fields as
 * aggregateTeamToEngineInputs minus `name`/`homeAway`, which the matchup
 * supplies: { aggregate, recentResults, restDays, injuryImpact }.
 */
export function buildMatchupModelInputs({ homeTeam, awayTeam, home, away, summary = null } = {}) {
  const homeInputs = aggregateTeamToEngineInputs({
    ...(home || {}),
    name: homeTeam,
    homeAway: 'home',
    summary,
  });
  if (!homeInputs) return null;
  const awayInputs = aggregateTeamToEngineInputs({
    ...(away || {}),
    name: awayTeam,
    homeAway: 'away',
    summary,
  });
  if (!awayInputs) return null;
  return {
    team1: homeInputs,
    team2: awayInputs,
    homeAway: 'home',
    classification: 'Historical',
    seasonsHeld: homeInputs.seasonsHeld,
  };
}

export default { aggregateTeamToEngineInputs, buildMatchupModelInputs, teamAggregateFromDoc };
