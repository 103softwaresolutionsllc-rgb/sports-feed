/**
 * Prediction archive for the grading ledger (Quant Apparatus spec
 * §5.1, Phase B).
 *
 * history.mjs already snapshots every published feed edition whole,
 * under `history/` in the Blob store. This module builds on that —
 * it does NOT duplicate the snapshot. What the ledger needs is the
 * edition's GRADABLE content, extracted once per run:
 *
 *   - every board game with its line sets (prices converted from the
 *     collector's American strings to the contract's integers at this
 *     seam — the documented G6 mapping conversion);
 *   - the app-model probabilities for each game, computed by the
 *     canonical port of the app's board model (model/) from the
 *     edition's own aggregates section — the same inputs the app
 *     consumed, so the archived probability is the board's number,
 *     labeled origin 'model';
 *   - every pick card, labeled origin 'book-implied': a card's winProb
 *     is the bookmaker-implied percentage, NOT a model probability
 *     (gap G4), and the archive never lets the two share a field.
 *
 * Layout (same Blob store as feed.json):
 *   predictions/feed-<ISO timestamp, sanitized>.json — one per run.
 *   predictions/index.json — { updatedAt, snapshots: [...] }, newest
 *       first, capped like the history index.
 *
 * NEVER-BREAK-THE-FEED GUARANTEE: persistFeedPredictions() is called
 * only AFTER the feed write and the history snapshot have succeeded,
 * catches every failure, logs it, and never throws.
 */

import { put, head } from '@vercel/blob';
import {
  buildGameModel,
  restDaysFromSchedule,
} from './model/services/board/boardModel.js';

export const PREDICTIONS_INDEX_PATH = 'predictions/index.json';
export const MAX_PREDICTIONS_INDEX_ENTRIES = 500;

const BLOB_WRITE_OPTS = {
  access: 'public',
  addRandomSuffix: false,
  allowOverwrite: true,
  contentType: 'application/json',
};

/** American price (string or number) -> contract integer, or null. */
export function americanToInt(value) {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? Math.trunc(value) : null;
  }
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!/^[+-]?\d+$/.test(trimmed)) return null;
  return parseInt(trimmed, 10);
}

function convertMarket(market, keys) {
  if (!market || typeof market !== 'object') return null;
  const out = {};
  for (const key of keys) {
    out[key] = key === 'line' ? (typeof market.line === 'number' ? market.line : null) : americanToInt(market[key]);
  }
  return out;
}

/** One native line set -> archived line set (integer prices). */
export function convertLineSet(lineSet) {
  if (!lineSet || typeof lineSet !== 'object') return null;
  return {
    bookmaker: lineSet.bookmaker,
    fetchedAt: lineSet.fetchedAt ?? null,
    moneyline: convertMarket(lineSet.moneyline, ['home', 'away']),
    spread: convertMarket(lineSet.spread, ['line', 'home', 'away']),
    total: convertMarket(lineSet.total, ['line', 'over', 'under']),
  };
}

/** Native board entry -> the canonical game shape the model consumes. */
export function canonicalGame(entry) {
  return {
    id: entry.id,
    gameId: entry.gameId,
    sport: entry.sport,
    league: entry.league,
    homeTeam: entry.home,
    awayTeam: entry.away,
    startTime: entry.startAt,
    startTs: entry.startTs,
  };
}

/**
 * Extract one edition's gradable content. Pure; exported for tests.
 * `feed` is the parsed feed payload (board + aggregates + picks).
 */
export function extractPredictions(feed) {
  const board = Array.isArray(feed?.board) ? feed.board : [];
  const canonicalGames = board.map(canonicalGame);
  const games = board.map((entry, i) => {
    const game = canonicalGames[i];
    const doc = feed?.aggregates?.[entry.sport] ?? null;
    let model = null;
    if (doc) {
      try {
        const read = buildGameModel(game, doc, {
          home: { restDays: restDaysFromSchedule(canonicalGames, game.homeTeam, game.startTime) },
          away: { restDays: restDaysFromSchedule(canonicalGames, game.awayTeam, game.startTime) },
        });
        if (read) {
          model = {
            origin: 'model',
            stream: 'app-model',
            homeWinProb: read.homeWinProb,
            awayWinProb: read.awayWinProb,
            projectedMargin: read.projectedMargin,
            sigma: read.sigma,
            classification: read.classification,
          };
        }
      } catch {
        model = null; // a model failure gates the read, never the archive
      }
    }
    return {
      id: entry.id,
      gameId: entry.gameId,
      sport: entry.sport,
      league: entry.league,
      home: entry.home,
      away: entry.away,
      startAt: entry.startAt,
      startTs: entry.startTs,
      lineSets: (entry.lineSets || []).map(convertLineSet).filter(Boolean),
      aggregatesPresent: Boolean(doc),
      model,
    };
  });

  const picks = (Array.isArray(feed?.picks) ? feed.picks : []).map((card) => ({
    id: card.id,
    sport: card.sport,
    sportLabel: card.sportLabel,
    teamA: card.teamA,
    teamB: card.teamB,
    pick: card.pick,
    footer: card.footer,
    startTs: card.startTs,
    winProb: card.winProb,
    impliedProb: typeof card.winProb === 'number' ? card.winProb / 100 : null,
    origin: 'book-implied',
    stream: 'book-implied-pick-card',
  }));

  return {
    contract: 'prediction-archive',
    v: 1,
    boardSnapshotRef: feed?.generatedAt ?? null,
    generatedAt: feed?.generatedAt ?? null,
    source: feed?.source ?? null,
    games,
    picks,
  };
}

export function predictionsPathFor(generatedAt) {
  const ts = String(generatedAt || new Date().toISOString());
  return `predictions/feed-${ts.replace(/[:.]/g, '-')}.json`;
}

/** Merge one entry into the predictions index. Pure; exported for tests. */
export function mergePredictionsIndexEntries(existing, entry, cap = MAX_PREDICTIONS_INDEX_ENTRIES) {
  const prior = Array.isArray(existing) ? existing : [];
  const merged = [
    entry,
    ...prior.filter(
      (e) => e && typeof e === 'object' && e.path !== entry.path && e.timestamp
    ),
  ];
  merged.sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)));
  return merged.slice(0, Math.max(1, cap));
}

function isNotFound(err) {
  const msg = String(err?.message || err);
  return err?.name === 'BlobNotFoundError' || /does not exist|not found/i.test(msg);
}

async function readIndexEntries() {
  let meta;
  try {
    meta = await head(PREDICTIONS_INDEX_PATH);
  } catch (err) {
    if (isNotFound(err)) return [];
    throw err;
  }
  const res = await fetch(meta.url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`predictions index fetch HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data?.snapshots) ? data.snapshots : [];
}

/**
 * Persist one run's prediction archive + index update. Fully guarded:
 * NEVER throws, and runs only after the feed itself is published.
 * `feed` is the parsed feed object the run produced.
 *
 * Returns the run's archive result — { ok, games, modelPriced,
 * pickCards } on success, { ok: false, error } on failure — so the
 * source-health board (health.mjs, Phase E) can report this edition's
 * archive counters truthfully instead of re-deriving them.
 */
export async function persistFeedPredictions(feed) {
  try {
    const archive = extractPredictions(feed);
    const result = {
      ok: true,
      games: archive.games.length,
      modelPriced: archive.games.filter((g) => g.model).length,
      pickCards: archive.picks.length,
    };
    const timestamp = String(feed?.generatedAt || new Date().toISOString());
    const path = predictionsPathFor(timestamp);
    const snapshot = await put(path, JSON.stringify(archive, null, 2), BLOB_WRITE_OPTS);
    console.log(
      `predictions: archive persisted (${result.games} games, ` +
        `${result.modelPriced} model-priced, ` +
        `${result.pickCards} pick cards) -> ${snapshot.url}`
    );
    try {
      const existing = await readIndexEntries();
      const snapshots = mergePredictionsIndexEntries(existing, {
        timestamp,
        path,
        url: snapshot.url,
      });
      await put(
        PREDICTIONS_INDEX_PATH,
        JSON.stringify({ updatedAt: new Date().toISOString(), snapshots }, null, 2),
        BLOB_WRITE_OPTS
      );
    } catch (idxErr) {
      result.indexError = String(idxErr?.message || idxErr);
      console.error(
        `predictions: index update failed (archive kept, feed unaffected): ${result.indexError}`
      );
    }
    return result;
  } catch (err) {
    const message = String(err?.message || err);
    console.error(
      `predictions: archive persist failed (feed unaffected): ${message}`
    );
    return { ok: false, error: message };
  }
}
