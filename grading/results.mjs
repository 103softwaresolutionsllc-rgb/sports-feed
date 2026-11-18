/**
 * results.mjs — final scores for grading, from the data branch.
 *
 * Two sources, both already the apparatus's own:
 *   1. The results cache (<root>/data/grades/results/*.json) written
 *      by fetch-results.mjs from ESPN's public scoreboards — the
 *      rule-10 failover source the harvest itself uses — archived so
 *      grading is reproducible without re-calling anything.
 *   2. Harvested game payloads (<root>/data/intelligence/raw/<sport>/
 *      games/**.json): API-Sports-shaped responses (including the
 *      harvest's ESPN-fallback translations, which arrive in the same
 *      shape). These carry the held seasons' finals.
 *
 * The nightly grading script itself makes NO provider calls; it reads
 * only these files.
 *
 * Matching: predictions are matched on (sport, UTC date, home, away)
 * with team names canonicalized (case/punctuation-insensitive plus an
 * alias table for the known cross-source spellings). If the exact
 * date misses, a ±1 day window on the same team pair is tried (a
 * scoreboard date and a start timestamp can straddle UTC midnight);
 * the nearest date wins and the match is flagged `dateShifted`.
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SPORT_BY_DIR = {
  basketball: 'NBA',
  'american-football': 'NFL',
  baseball: 'MLB',
  hockey: 'NHL',
  soccer: 'Soccer',
};

const ALIASES = new Map([
  ['la clippers', 'los angeles clippers'],
  ['los angeles clippers', 'los angeles clippers'],
  ['utah hockey club', 'utah mammoth'],
  ['athletics', 'oakland athletics'],
]);

export function normalizeTeamName(name) {
  // Periods fold to spaces, so "St. Louis", "St Louis", and the feed's
  // own "St.Louis" spelling all canonicalize identically.
  const base = String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return ALIASES.get(base) || base;
}

function dateOf(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10);
}

function keyFor(sport, date, home, away) {
  return `${sport}|${date}|${normalizeTeamName(home)}|${normalizeTeamName(away)}`;
}

function walkJsonFiles(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walkJsonFiles(path, out);
    else if (entry.endsWith('.json')) out.push(path);
  }
  return out;
}

function addResult(index, byPair, result) {
  if (
    !result ||
    result.homeScore == null ||
    result.awayScore == null ||
    !result.date ||
    !result.home ||
    !result.away
  ) {
    return;
  }
  const key = keyFor(result.sport, result.date, result.home, result.away);
  if (!index.has(key)) index.set(key, result);
  const pairKey = `${result.sport}|${normalizeTeamName(result.home)}|${normalizeTeamName(result.away)}`;
  if (!byPair.has(pairKey)) byPair.set(pairKey, []);
  byPair.get(pairKey).push(result);
}

/** Extract final games from one API-Sports-shaped payload object. */
export function finalsFromApiSportsPayload(payload, sport) {
  const response = Array.isArray(payload?.response) ? payload.response : [];
  const finals = [];
  for (const game of response) {
    const short = game?.status?.short;
    if (!['FT', 'AOT', 'FT_PEN', 'AP'].includes(short)) continue;
    const homeScore = game?.scores?.home?.total;
    const awayScore = game?.scores?.away?.total;
    if (homeScore == null || awayScore == null) continue;
    finals.push({
      sport,
      date: dateOf(game.date),
      home: game?.teams?.home?.name,
      away: game?.teams?.away?.name,
      homeScore,
      awayScore,
      source: 'harvest',
    });
  }
  return finals;
}

/**
 * Load every known final under `root`.
 * @returns {{ byKey: Map, byPair: Map, counts: {cache: number, harvest: number} }}
 */
export function loadResults(root) {
  const byKey = new Map();
  const byPair = new Map();
  const counts = { cache: 0, harvest: 0 };

  const cacheDir = join(root, 'data', 'grades', 'results');
  for (const file of walkJsonFiles(cacheDir)) {
    try {
      const doc = JSON.parse(readFileSync(file, 'utf8'));
      for (const game of doc.games || []) {
        addResult(byKey, byPair, {
          ...game,
          date: dateOf(game.date) || game.date,
          startAt: game.startAt || game.date,
          source: game.source || doc.source || 'cache',
        });
        counts.cache += 1;
      }
    } catch {
      // A malformed cache file is skipped, never fatal to grading.
    }
  }

  const rawRoot = join(root, 'data', 'intelligence', 'raw');
  if (existsSync(rawRoot)) {
    for (const dirName of readdirSync(rawRoot)) {
      const sport = SPORT_BY_DIR[dirName];
      if (!sport) continue;
      const gamesDir = join(rawRoot, dirName, 'games');
      for (const file of walkJsonFiles(gamesDir)) {
        try {
          const payload = JSON.parse(readFileSync(file, 'utf8'));
          for (const result of finalsFromApiSportsPayload(payload, sport)) {
            addResult(byKey, byPair, result);
            counts.harvest += 1;
          }
        } catch {
          // Skip unreadable payloads (e.g. partial writes), as above.
        }
      }
    }
  }

  return { byKey, byPair, counts };
}

/**
 * Find the final for one predicted game.
 * @param results — the loadResults() return value.
 * @param game — { sport (league code), startAt (ISO), home, away }.
 */
export function findResult(results, game) {
  const date = dateOf(game.startAt);
  if (!date) return null;
  const exact = results.byKey.get(keyFor(game.sport, date, game.home, game.away));
  if (exact) return { ...exact, dateShifted: false };
  const pairKey = `${game.sport}|${normalizeTeamName(game.home)}|${normalizeTeamName(game.away)}`;
  const candidates = (results.byPair.get(pairKey) || []).filter((r) => {
    const delta = Math.abs(new Date(r.date) - new Date(date)) / 86400000;
    return delta <= 1;
  });
  if (!candidates.length) return null;
  candidates.sort(
    (a, b) =>
      Math.abs(new Date(a.date) - new Date(date)) -
      Math.abs(new Date(b.date) - new Date(date))
  );
  return { ...candidates[0], dateShifted: true };
}
