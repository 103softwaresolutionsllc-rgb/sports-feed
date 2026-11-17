/**
 * Intelligence Engine aggregates publishing for the 103 sports feed.
 *
 * Where the aggregates live: the daily harvest job
 * (.github/workflows/intelligence-harvest.yml) runs
 * intelligence/intelligence_aggregates.py per sport and commits the
 * output to this repo's `data` branch at
 * `data/intelligence/aggregates/<sport>.json` — one document per sport in
 * the Engine's own shape ({ summary, averages, weighted_averages,
 * leaderboards, player_leaderboards }). SportsBettingGPT's router reads
 * them from that branch over HTTPS (INTELLIGENCE_AGGREGATES_BASE_URL in
 * intelligence/SYNC.md); the repo is public, so the same documents are
 * served at the raw.githubusercontent.com URL below.
 *
 * This module republishes the two fields the BetTracker board's model
 * overlay consumes (src/services/board/boardEngineInputs.js in the app:
 * `weighted_averages` + `summary.seasons_held`) as the feed's top-level
 * `aggregates` section, keyed by the collector's display sport names —
 * the exact keying the app's boardService.aggregatesOf passes through.
 * The Engine's `summary` is carried VERBATIM (classification, seasons
 * held, label, label detail, team count): it is the provenance the app
 * labels model inputs with ("Historical", seasons held), so it is never
 * stripped, summarized, or recomputed here. The document's as-of time
 * follows the router's own rule — the document's `generated_at` when it
 * carries one, else the source's Last-Modified (HTTPS) or file write
 * time (local directory) — and is published as `data_as_of` only when a
 * source actually provided one; it is never invented.
 *
 * Sources (the router's two modes, same precedence — local wins per
 * sport, remote fills the rest; either alone is sufficient and the two
 * are never blended into one document):
 *   1. INTELLIGENCE_DATA_DIR — a local directory laid out as the
 *      pipeline writes it: <dir>/aggregates/<sport>.json (a bare
 *      <dir>/<sport>.json is also accepted, as in the router).
 *   2. INTELLIGENCE_AGGREGATES_BASE_URL — base URL of a directory of
 *      <sport>.json documents over HTTPS; defaults to the data branch's
 *      published aggregates directory (DEFAULT_AGGREGATES_BASE_URL).
 *
 * Cost: ZERO provider requests. These fetches read documents the harvest
 * already produced from GitHub's raw file host (or a local directory) —
 * they never touch API-Sports and spend none of its quota, and no new
 * harvest runs.
 *
 * Honesty rules:
 *   - A sport whose document cannot be loaded or fails validation is
 *     OMITTED from `aggregates` and named in the returned `errors` map
 *     (published as the feed's separate `aggregatesErrors` — never in
 *     the feed's `errors`, whose per-sport entries mean "this sport's
 *     collector run failed" and would stale that sport's board prices).
 *     The app's no-stats gate then withholds that sport's model reads;
 *     prices still render. Nothing is repaired or filled in.
 *   - A valid document with empty `weighted_averages` (a sport whose
 *     normalized seasons are held but produced no team averages yet)
 *     publishes AS-IS: summary + empty averages. The gate withholds the
 *     model read; the provenance still reaches the app.
 *   - collectAggregates never throws: a total aggregates outage degrades
 *     to an empty section plus per-sport errors, never a failed feed.
 */

import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';

/** Display sport name (collector / feed keying) -> aggregates file sport. */
export const AGGREGATES_SPORT_MAP = {
  Basketball: 'basketball',
  Football: 'american-football',
  Baseball: 'baseball',
  Hockey: 'hockey',
  // Soccer aggregates exist on the data branch, but the collector has no
  // Soccer board sport (SPORT_APIS in normalize.mjs), so there is no
  // display name for the app to look one up by; it is not published.
};

export const AGGREGATES_BASE_URL_ENV = 'INTELLIGENCE_AGGREGATES_BASE_URL';
export const AGGREGATES_DATA_DIR_ENV = 'INTELLIGENCE_DATA_DIR';

/**
 * The data branch's published aggregates directory — the same source the
 * SportsBettingGPT router reads via INTELLIGENCE_AGGREGATES_BASE_URL.
 * Layout: <base>/<sport>.json, e.g. .../aggregates/basketball.json.
 */
export const DEFAULT_AGGREGATES_BASE_URL =
  'https://raw.githubusercontent.com/103softwaresolutionsllc-rgb/sports-feed/data/data/intelligence/aggregates';

function isPlainObject(v) {
  return Boolean(v) && typeof v === 'object' && !Array.isArray(v);
}

function isIsoTime(v) {
  return typeof v === 'string' && v.length > 0 && Number.isFinite(Date.parse(v));
}

/** ISO string for a Date / date string, or null when unparseable. */
function toIso(v) {
  if (v == null) return null;
  const ms = v instanceof Date ? v.getTime() : Date.parse(String(v));
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

/**
 * Validate + project one Engine aggregates document into the shape the
 * app's boardEngineInputs consumes:
 *
 *   { summary: { sport, classification, seasons_held, season_count,
 *                label, label_detail, teams },
 *     weighted_averages: { <team>: { <metric>: <finite number> } },
 *     data_as_of?: <ISO string> }
 *
 * `summary` fields are copied verbatim when they carry the Engine's
 * types (seasons_held filtered to strings); nothing is recomputed or
 * defaulted. `weighted_averages` keeps only finite numeric metric values
 * and object team records — malformed entries are dropped, never
 * repaired. `averages` / `leaderboards` / `player_leaderboards` are NOT
 * republished: the board overlay does not consume them and they would
 * roughly triple the section's size.
 *
 * `dataAsOf` (the loader's source timestamp) wins; otherwise the
 * document's own `generated_at` is used when it carries one. The field
 * is omitted entirely when neither exists.
 *
 * Returns null when the document cannot be read honestly at all (not an
 * object, or `summary` / `weighted_averages` missing or not objects).
 */
export function sanitizeAggregatesDoc(raw, { dataAsOf = null } = {}) {
  if (!isPlainObject(raw)) return null;
  if (!isPlainObject(raw.summary)) return null;
  if (!isPlainObject(raw.weighted_averages)) return null;

  const s = raw.summary;
  const summary = {};
  if (typeof s.sport === 'string') summary.sport = s.sport;
  if (typeof s.classification === 'string') summary.classification = s.classification;
  summary.seasons_held = Array.isArray(s.seasons_held)
    ? s.seasons_held.filter((x) => typeof x === 'string')
    : [];
  if (Number.isFinite(s.season_count)) summary.season_count = s.season_count;
  if (typeof s.label === 'string') summary.label = s.label;
  if (typeof s.label_detail === 'string') summary.label_detail = s.label_detail;
  if (Number.isFinite(s.teams)) summary.teams = s.teams;

  const weighted = {};
  for (const [team, record] of Object.entries(raw.weighted_averages)) {
    if (!isPlainObject(record)) continue;
    const metrics = {};
    for (const [metric, value] of Object.entries(record)) {
      if (Number.isFinite(value)) metrics[metric] = value;
    }
    weighted[team] = metrics;
  }

  const doc = { summary, weighted_averages: weighted };
  const asOf = toIso(dataAsOf) ?? (isIsoTime(raw.generated_at) ? toIso(raw.generated_at) : null);
  if (asOf) doc.data_as_of = asOf;
  return doc;
}

/** One sport's document from a local INTELLIGENCE_DATA_DIR, or null. */
async function loadLocalDoc(dataDir, fileSport, io) {
  for (const candidate of [
    join(dataDir, 'aggregates', `${fileSport}.json`),
    join(dataDir, `${fileSport}.json`),
  ]) {
    let text;
    try {
      text = await io.readFile(candidate, 'utf8');
    } catch {
      continue; // not at this candidate path — try the next layout
    }
    let dataAsOf = null;
    try {
      const st = await io.stat(candidate);
      dataAsOf = st?.mtime ?? null;
    } catch {
      dataAsOf = null; // content read fine; as-of stays unknown, never invented
    }
    return { raw: JSON.parse(text), dataAsOf };
  }
  return null;
}

/** One sport's document from the aggregates base URL, or null. */
async function loadRemoteDoc(baseUrl, fileSport, fetchFn) {
  const url = `${String(baseUrl).replace(/\/+$/, '')}/${fileSport}.json`;
  const res = await fetchFn(url);
  if (!res || !res.ok) {
    throw new Error(`aggregates HTTP ${res?.status ?? 'no response'} for ${fileSport}`);
  }
  const raw = await res.json();
  let dataAsOf = null;
  try {
    dataAsOf = res.headers?.get?.('last-modified') ?? null;
  } catch {
    dataAsOf = null;
  }
  return { raw, dataAsOf };
}

/**
 * Load every board sport's aggregates document. Returns
 * { aggregates, errors } — `aggregates` keyed by display sport name,
 * `errors` keyed the same way with a plain message per sport that could
 * not be loaded. Never throws.
 *
 * Options (all injectable for tests; production passes nothing):
 *   env      — environment map (default process.env)
 *   fetchFn  — fetch implementation (default global fetch)
 *   io       — { readFile, stat } for the local-directory source
 */
export async function collectAggregates({
  env = process.env,
  fetchFn = fetch,
  io = { readFile, stat },
} = {}) {
  const aggregates = {};
  const errors = {};
  const dataDir = env?.[AGGREGATES_DATA_DIR_ENV] || null;
  const baseUrl = env?.[AGGREGATES_BASE_URL_ENV] || DEFAULT_AGGREGATES_BASE_URL;

  for (const [displaySport, fileSport] of Object.entries(AGGREGATES_SPORT_MAP)) {
    try {
      let loaded = null;
      if (dataDir) {
        try {
          loaded = await loadLocalDoc(dataDir, fileSport, io);
        } catch {
          loaded = null; // unreadable local doc — fall through to remote
        }
      }
      if (!loaded) loaded = await loadRemoteDoc(baseUrl, fileSport, fetchFn);
      const doc = sanitizeAggregatesDoc(loaded?.raw, { dataAsOf: loaded?.dataAsOf });
      if (!doc) {
        errors[displaySport] = `aggregates document for ${fileSport} is malformed`;
        continue;
      }
      aggregates[displaySport] = doc;
    } catch (err) {
      errors[displaySport] = String(err?.message || err);
    }
  }
  return { aggregates, errors };
}

export default { collectAggregates, sanitizeAggregatesDoc };
