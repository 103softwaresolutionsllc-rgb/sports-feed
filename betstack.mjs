/**
 * BetStack slate layer for the 103 sports-feed collector.
 *
 * Why this layer exists (key audit, 2026-11-17): the collector's primary
 * source, API-Sports, refuses the CURRENT season on its free plan for
 * Basketball, Baseball and Hockey ("Free plans do not have access to this
 * season, try from 2022 to 2024" — observed live in the feed's own
 * `errors`), so those sports publish an empty slate. BetStack
 * (api.betstack.dev) is a free-forever API whose /events endpoint carries
 * current-season schedules, live/final scores and a consensus line for
 * exactly those leagues. This layer fills the slate for a sport ONLY
 * when the API-Sports pass produced no slate items for it — the same
 * fill-empty-slots-only rule the Intelligence Engine tiers use. It never
 * overwrites, merges into, or "repairs" an API-Sports record.
 *
 * What this layer deliberately does NOT publish:
 *   - Board entries. BetStack's line is a cross-book CONSENSUS, not a
 *     named bookmaker's price. The board contract (Predict remake spec
 *     §4.2) requires every line set to carry its bookmaker's name, and
 *     prices from different sources are never blended into one entry —
 *     so BetStack prices never reach the board. The board's odds source
 *     is The Odds API (oddsapi.mjs).
 *   - Pick cards. A pick card quotes a bookmaker's favorite; a consensus
 *     line has no bookmaker to quote.
 *
 * Provenance: every slate item this layer emits carries the id prefix
 * `betstack-slate-` (API-Sports items carry `apisp-slate-`), so a
 * record's origin is visible in the published feed itself.
 *
 * Key handling: BETSTACK_API_KEY from the environment (owner-held, set
 * in the Vercel project — never in any app client, never in this repo).
 * With no key set, every requested sport degrades to a
 * "not configured (no key)" entry in the returned errors map — the same
 * honest empty state the API-Sports pass produces for unconfigured
 * sports. The layer activates the moment the key lands in the env; no
 * code change is needed.
 *
 * Quota discipline: BetStack's documented limit is one request per 60
 * seconds per key. This layer makes ONE request per filled sport per
 * collector run (at most 3: Basketball, Baseball, Hockey), and the
 * collector runs every 90 minutes — orders of magnitude inside the
 * limit, with no per-game follow-up calls of any kind.
 *
 * Request-shape note: BetStack's public example calls the league filter
 * `league_key`, while the SportsBettingGPT backend (Sage) has called it
 * `league` since it was wired there. Both spellings are sent (same
 * value); servers ignore unknown query params, so the documented one is
 * honored either way. If a future live check shows one spelling is
 * rejected, drop the other — do not guess a third.
 *
 * Failure behavior: collectBetstackSlate never throws. A failed sport
 * lands in the returned errors map (published by the collector as the
 * feed's `betstackErrors`, mirroring `aggregatesErrors`) and contributes
 * no slate items; the rest of the feed is unaffected.
 */

import { toSlateItem } from './normalize.mjs';

/** Sports this layer may fill, in collector display-sport vocabulary. */
export const BETSTACK_SPORTS = {
  Basketball: { leagueKey: 'basketball_nba', sportLabel: 'NBA' },
  Baseball: { leagueKey: 'baseball_mlb', sportLabel: 'MLB' },
  Hockey: { leagueKey: 'icehockey_nhl', sportLabel: 'NHL' },
  // Football intentionally absent: API-Sports covers the NFL (its free
  // plan does not season-block Football), so a BetStack fill would be
  // pure duplication.
};

export const BETSTACK_BASE_URL = 'https://api.betstack.dev/api/v1';
const MAX_SLATE_PER_SPORT = 6; // parity with the API-Sports slate cap

function teamName(side) {
  if (typeof side === 'string') return side;
  if (side && typeof side === 'object') return side.name || null;
  return null;
}

function teamCode(side, fallbackName) {
  if (side && typeof side === 'object' && side.code) return side.code;
  if (!fallbackName) return '—';
  return (
    fallbackName.replace(/[^A-Za-z]/g, '').slice(0, 3).toUpperCase() || '—'
  );
}

function scoreValue(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * Normalize one raw BetStack event into the collector's internal game
 * shape (the same shape normalizeGame produces for API-Sports), or null
 * when the event cannot be read honestly: missing teams, an unparseable
 * start time, or a finished game (finished games belong to results, not
 * the upcoming slate — parity with normalizeGame).
 *
 * Field spellings follow the provider's published example
 * (home_team / away_team / commence_time / result{home_score,
 * away_score, final}) with the backend-observed fallbacks the
 * SportsBettingGPT transform carries (homeTeam / home, dict-shaped
 * teams).
 */
export function normalizeBetstackEvent(raw, sport) {
  const cfg = BETSTACK_SPORTS[sport];
  if (!cfg || !raw || typeof raw !== 'object') return null;

  const homeRaw = raw.home_team ?? raw.homeTeam ?? raw.home;
  const awayRaw = raw.away_team ?? raw.awayTeam ?? raw.away;
  const home = teamName(homeRaw);
  const away = teamName(awayRaw);
  if (!home || !away) return null;

  const startTs = Date.parse(raw.commence_time || raw.start_time || '');
  if (!Number.isFinite(startTs) || startTs <= 0) return null;

  const result = raw.result && typeof raw.result === 'object' ? raw.result : {};
  const homeScore = scoreValue(result.home_score);
  const awayScore = scoreValue(result.away_score);

  let status;
  if (result.final === true) {
    status = 'finished';
  } else if (startTs <= Date.now()) {
    status = homeScore != null && awayScore != null ? 'live' : 'upcoming';
  } else {
    status = 'upcoming';
  }
  if (status === 'finished') return null;

  return {
    id: raw.id ?? raw.event_id ?? null,
    sport,
    sportLabel: cfg.sportLabel,
    home,
    away,
    homeCode: teamCode(homeRaw, home),
    awayCode: teamCode(awayRaw, away),
    homeScore: status === 'live' ? homeScore : null,
    awayScore: status === 'live' ? awayScore : null,
    status,
    statusLong: status === 'live' ? 'In progress' : null,
    startTs,
  };
}

/** Slate item with this layer's provenance prefix swapped in. */
export function toBetstackSlateItem(game) {
  const item = toSlateItem(game);
  item.id = item.id.replace(/^apisp-slate-/, 'betstack-slate-');
  return item;
}

async function fetchSportEvents(sport, apiKey, fetchImpl) {
  const cfg = BETSTACK_SPORTS[sport];
  const params = new URLSearchParams({
    league: cfg.leagueKey,
    league_key: cfg.leagueKey,
    completed: 'false',
    include_lines: 'true',
  });
  const res = await fetchImpl(`${BETSTACK_BASE_URL}/events?${params}`, {
    headers: { 'X-API-Key': apiKey, Accept: 'application/json' },
  });
  if (!res.ok) throw new Error(`BetStack ${sport} HTTP ${res.status}`);
  const data = await res.json();
  const events = Array.isArray(data) ? data : data?.events;
  if (!Array.isArray(events)) {
    throw new Error(`BetStack ${sport} unexpected response shape`);
  }
  return events;
}

/**
 * Fill slate items for the given sports (collector display names) from
 * BetStack. Callers pass ONLY sports whose primary-source slate came
 * back empty — this layer fills empty slots, it does not compete with
 * the primary source. Returns { slate, errors }; never throws.
 *
 * fetchImpl is injectable for tests; production passes the global fetch.
 */
export async function collectBetstackSlate({
  sports,
  apiKey = process.env.BETSTACK_API_KEY,
  fetchImpl = fetch,
} = {}) {
  const slate = [];
  const errors = {};
  const requested = (sports || []).filter((s) => BETSTACK_SPORTS[s]);
  if (requested.length === 0) return { slate, errors };

  if (!apiKey) {
    for (const sport of requested) {
      errors[sport] = 'not configured (no key)';
    }
    return { slate, errors };
  }

  for (const sport of requested) {
    try {
      const events = await fetchSportEvents(sport, apiKey, fetchImpl);
      const items = events
        .map((raw) => normalizeBetstackEvent(raw, sport))
        .filter(Boolean)
        .sort((a, b) => {
          if (a.status !== b.status) return a.status === 'live' ? -1 : 1;
          return a.startTs - b.startTs;
        })
        .slice(0, MAX_SLATE_PER_SPORT)
        .map(toBetstackSlateItem);
      slate.push(...items);
      console.log(`BetStack ${sport}: ${items.length} slate items (fill)`);
    } catch (err) {
      errors[sport] = String(err?.message || err);
      console.error(`BetStack ${sport}: FAILED — ${errors[sport]}`);
    }
  }
  return { slate, errors };
}
