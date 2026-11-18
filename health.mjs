/**
 * health.mjs — the collector's source-health board (Quant Apparatus
 * spec §8, Phase E).
 *
 * Every collector tick publishes `health.json` into the same Blob
 * store as feed.json, describing the sources THIS run can truthfully
 * observe: its own passes and their gates. The shape is the Phase E
 * SourceHealth contract (contracts/source-health.schema.json); the
 * record is validated against the contract before it is written —
 * an invalid board is logged and skipped, never published.
 *
 * NEVER-BREAK-THE-FEED GUARANTEE (same as history.mjs and
 * predictions.mjs, and stronger in one way: it is test-proven):
 * persistFeedHealth() is called only AFTER the feed write has
 * succeeded, catches every failure, and never throws. The failure
 * simulation lives in tests/health.test.mjs.
 *
 * THE BOARD NEVER EDITS THE CHAIN (§8 chain truth rule): this module
 * reads gate state, it never changes it. API-Sports stays gated off
 * until Isaac or the Legion runner changes the gate; BetStack is
 * reported as not-a-link for exactly as long as it cannot serve real
 * data. Surveillance is not control.
 */

import { put } from '@vercel/blob';
import { apiSportsCollectorEnabled, betstackFillEnabled } from './collector.mjs';
import { LOW_CREDIT_FLOOR } from './oddsapi.mjs';
import { validate } from './contracts/validate.mjs';

export const HEALTH_PATH = 'health.json';

const BLOB_WRITE_OPTS = {
  access: 'public',
  addRandomSuffix: false,
  allowOverwrite: true,
  cacheControlMaxAge: 60,
  contentType: 'application/json',
};

const APISPORTS_GATE_REASON =
  'Account suspended by API-Sports — their support confirmed the cause was ' +
  'requests arriving from shared cloud egress IPs, which is what Vercel ' +
  'Functions run on. The pass is gated off (APISPORTS_COLLECTOR_ENABLED is ' +
  'not "true") so no call is made from a shared IP; API-Sports data enters ' +
  'only through the harvest on Isaac\'s own infrastructure. The fix is the ' +
  'dedicated-IP Legion runner, which is not installed yet (the laptop is ' +
  'at the Lenovo depot).';

const BETSTACK_NOT_A_LINK_REASON =
  'Not a link: the slate-fill layer is dark (BETSTACK_FILL_ENABLED unset) ' +
  'and the live probe of 2026-11-17 failed — the key authenticates, but ' +
  'every league query returns one identical unfiltered mixed-league slate ' +
  'with no lines and no scores, so BetStack cannot serve real data today. ' +
  'It does not count toward any chain\'s depth.';

/** Which source produced this edition's pick cards (defensive: older
 *  editions predate picksMeta and are read from the picks themselves). */
export function picksSourceOf(feed) {
  if (feed?.picksMeta?.source) return feed.picksMeta.source;
  return Array.isArray(feed?.picks) && feed.picks.length > 0
    ? 'api-sports'
    : 'none';
}

function apiSportsEntry(feed, env, generatedAt) {
  const base = {
    name: 'api-sports',
    role: 'Pick cards (primary) + slate fallback fill for the board',
    chain: 'pick-cards',
    chainPosition: 1,
    observedBy: 'collector',
  };
  if (!apiSportsCollectorEnabled(env)) {
    return {
      ...base,
      link: false,
      status: 'gated-off',
      reason: APISPORTS_GATE_REASON,
      // No attempt was made — that is the point of the gate — and this
      // board has never seen the pass succeed, so both stay null.
      lastGoodAt: null,
      lastAttemptAt: null,
      since: '2026-11-17',
      quota: null,
      detail:
        'Pick cards fail over to The Odds API board (see the pick-cards seam).',
    };
  }
  const configured = feed?.sportsConfigured ?? [];
  const sportErrors = {
    ...(feed?.apiSportsErrors ?? {}),
    ...(feed?.errors ?? {}),
  };
  const errored = configured.filter((s) => sportErrors[s]);
  const notConfigured = Object.entries(feed?.errors ?? {}).some(([, m]) =>
    String(m).includes('not configured')
  );
  const lastAttemptAt = generatedAt;
  if (configured.length === 0 && notConfigured) {
    return {
      ...base,
      link: false,
      status: 'down',
      reason: 'Gate is on but no API-Sports key is configured.',
      lastGoodAt: null,
      lastAttemptAt: null,
      since: null,
      quota: null,
      detail: null,
    };
  }
  if (errored.length > 0) {
    return {
      ...base,
      link: errored.length < configured.length,
      status: 'degraded',
      reason: `API-Sports pass errored for: ${errored.join(', ')}.`,
      lastGoodAt: errored.length < configured.length ? generatedAt : null,
      lastAttemptAt,
      since: null,
      quota: null,
      detail: null,
    };
  }
  return {
    ...base,
    link: true,
    status: 'ok',
    reason: null,
    lastGoodAt: generatedAt,
    lastAttemptAt,
    since: null,
    quota: null,
    detail: null,
  };
}

const ODDS_SWEEP_ATTEMPTED = new Set(['ok', 'partial', 'sweep-failed-carried']);

function oddsApiEntry(feed, generatedAt, picksSource) {
  const meta = feed?.boardMeta ?? null;
  const base = {
    name: 'the-odds-api',
    role: 'Board lines (one book\'s complete line set per game) + Notable Lines failover for pick cards',
    chain: 'board-lines',
    chainPosition: 1,
    observedBy: 'collector',
    quota: {
      creditsRemaining: meta?.creditsRemaining ?? null,
      creditsUsed: meta?.creditsUsed ?? null,
      floor: LOW_CREDIT_FLOOR,
    },
    lastGoodAt: meta?.oddsApiFetchedAt ?? null,
  };
  const failoverNote =
    picksSource === 'the-odds-api'
      ? 'Also produced this edition\'s pick cards (Notable Lines failover).'
      : null;
  if (!meta) {
    return {
      ...base,
      link: false,
      status: 'not-observed',
      reason: 'This edition carries no boardMeta — the Odds layer state is not observable from it.',
      lastAttemptAt: null,
      since: null,
      detail: failoverNote,
    };
  }
  const attempted = ODDS_SWEEP_ATTEMPTED.has(meta.status);
  const lastAttemptAt = attempted ? generatedAt : null;
  switch (meta.status) {
    case 'ok':
      return { ...base, link: true, status: 'ok', reason: null, lastAttemptAt, since: null, detail: failoverNote };
    case 'carried':
      return {
        ...base,
        link: true,
        status: 'ok',
        reason: null,
        lastAttemptAt,
        since: null,
        detail:
          'Between sweeps (per-sport ~22h gate): the last sweep\'s board carried over verbatim.' +
          (failoverNote ? ` ${failoverNote}` : ''),
      };
    case 'partial':
      return {
        ...base,
        link: true,
        status: 'degraded',
        reason: `Sweep failed for some due sports (${Object.keys(meta.sportErrors ?? {}).join(', ') || 'see boardMeta'}); their prior entries carried over.`,
        lastAttemptAt,
        since: null,
        detail: failoverNote,
      };
    case 'sweep-failed-carried':
      return {
        ...base,
        link: true,
        status: 'degraded',
        reason: 'The sweep failed for every due sport; the prior board carried over.',
        lastAttemptAt,
        since: null,
        detail: failoverNote,
      };
    case 'skipped-low-credits':
      return {
        ...base,
        link: true,
        status: 'degraded',
        reason: `Sweep skipped: ${meta.creditsRemaining} credits remaining, below the ${LOW_CREDIT_FLOOR}-credit floor. The prior board carried over.`,
        lastAttemptAt: null,
        since: null,
        detail: failoverNote,
      };
    case 'not-configured':
      return {
        ...base,
        link: false,
        status: 'down',
        reason: 'ODDS_API_KEY is not set in the collector environment; only carried-over entries (if any) are published.',
        lastAttemptAt: null,
        since: null,
        detail: failoverNote,
      };
    case 'layer-failed':
      return {
        ...base,
        link: false,
        status: 'down',
        reason: feed?.errors?.OddsApi ?? 'The Odds API layer failed this run.',
        lastAttemptAt,
        since: null,
        detail: failoverNote,
      };
    default:
      return {
        ...base,
        link: false,
        status: 'not-observed',
        reason: `Unrecognized boardMeta status '${meta.status}'.`,
        lastAttemptAt,
        since: null,
        detail: failoverNote,
      };
  }
}

function espnEntry(feed, generatedAt) {
  const news = feed?.news ?? null;
  const base = {
    name: 'espn',
    role: 'News + context for the feed (keyless); also the harvest\'s failover source',
    chain: null,
    chainPosition: null,
    observedBy: 'collector',
    quota: null,
    since: null,
  };
  if (!news || typeof news !== 'object') {
    return { ...base, link: false, status: 'not-observed', reason: 'This edition carries no news section.', lastGoodAt: null, lastAttemptAt: null, detail: null };
  }
  if (news.error) {
    return { ...base, link: false, status: 'down', reason: String(news.error), lastGoodAt: null, lastAttemptAt: generatedAt, detail: null };
  }
  const leagueErrors = Object.entries(news)
    .filter(([k, v]) => k !== 'fetchedAt' && v && typeof v === 'object' && v.error)
    .map(([k]) => k);
  if (leagueErrors.length > 0) {
    return {
      ...base,
      link: true,
      status: 'degraded',
      reason: `News fetch failed for: ${leagueErrors.join(', ')} (JSON and RSS both).`,
      lastGoodAt: news.fetchedAt ?? null,
      lastAttemptAt: generatedAt,
      detail: null,
    };
  }
  return { ...base, link: true, status: 'ok', reason: null, lastGoodAt: news.fetchedAt ?? null, lastAttemptAt: generatedAt, detail: null };
}

function betstackEntry(feed, env) {
  const base = {
    name: 'betstack',
    role: 'Slate fill (collector) / first link of Sage\'s odds chain',
    chain: 'odds-chain',
    chainPosition: 1,
    observedBy: 'collector',
    quota: null,
  };
  if (!betstackFillEnabled(env)) {
    return {
      ...base,
      link: false,
      status: 'not-a-link',
      reason: BETSTACK_NOT_A_LINK_REASON,
      lastGoodAt: null,
      lastAttemptAt: null,
      since: '2026-11-17',
      detail: null,
    };
  }
  const errors = feed?.betstackErrors ?? {};
  const errored = Object.keys(errors).length > 0;
  return {
    ...base,
    link: true,
    status: errored ? 'degraded' : 'ok',
    reason: errored ? `Fill errors: ${Object.values(errors).join('; ')}` : null,
    lastGoodAt: null,
    lastAttemptAt: feed?.generatedAt ?? null,
    since: null,
    detail: 'Fill gate is ON. Per-run fill counts are not carried in the feed payload, so this board reports the gate + errors only.',
  };
}

/**
 * Build the collector's health record for one edition. Pure.
 * `archive` is persistFeedPredictions()'s result ({ ok, games,
 * modelPriced, pickCards, error? }) or null when not supplied.
 */
export function buildCollectorHealth({ feed, archive = null, env = process.env, now = new Date() } = {}) {
  const generatedAt = feed?.generatedAt ?? now.toISOString();
  const picksSource = picksSourceOf(feed);
  const picks = Array.isArray(feed?.picks) ? feed.picks : [];
  const aggregatesErrors = feed?.aggregatesErrors ?? {};

  const seams = {
    'pick-cards': {
      status: picksSource === 'none' ? 'degraded' : 'ok',
      detail:
        picksSource === 'api-sports'
          ? 'Produced by the API-Sports pass.'
          : picksSource === 'the-odds-api'
            ? 'The API-Sports pass yielded no cards; rebuilt from The Odds API board (Notable Lines failover, Phase 5).'
            : feed?.picksMeta
              ? 'No source produced pick cards this edition.'
              : 'No pick cards this edition, and the edition predates picksMeta labeling (published by an older collector build, before the Phase 5 failover shipped).',
      counts: { source: picksSource, cards: picks.length },
    },
    'board-edition': {
      status: 'ok',
      detail: null,
      counts: {
        boardGames: Array.isArray(feed?.board) ? feed.board.length : 0,
        slateItems: Array.isArray(feed?.slate) ? feed.slate.length : 0,
      },
    },
    'prediction-archive': archive
      ? {
          status: archive.ok ? 'ok' : 'degraded',
          detail: archive.ok
            ? 'This edition\'s gradable content was archived for the nightly grading ledger (Phase B).'
            : `Archive step failed (feed unaffected): ${archive.error ?? 'unknown error'}`,
          counts: {
            games: archive.games ?? null,
            modelPriced: archive.modelPriced ?? null,
            pickCards: archive.pickCards ?? null,
          },
        }
      : {
          status: 'not-observed',
          detail: 'The archive step\'s result was not supplied to the health publisher.',
          counts: null,
        },
    'intelligence-aggregates': {
      status: Object.keys(aggregatesErrors).length === 0 ? 'ok' : 'degraded',
      detail:
        Object.keys(aggregatesErrors).length === 0
          ? null
          : `Aggregates failed for: ${Object.keys(aggregatesErrors).join(', ')}.`,
      counts: { sports: Object.keys(feed?.aggregates ?? {}).length },
    },
  };

  return {
    contract: 'source-health',
    v: 1,
    class: 'Live',
    source: 'sports-feed collector (api/collect.mjs)',
    fetchedAt: now.toISOString(),
    id: `source-health:collector:${generatedAt}`,
    publisher: 'collector',
    board: 'collector',
    generatedAt,
    sources: [
      apiSportsEntry(feed, env, generatedAt),
      oddsApiEntry(feed, generatedAt, picksSource),
      espnEntry(feed, generatedAt),
      betstackEntry(feed, env),
    ],
    seams,
    flags: [],
  };
}

/**
 * Publish one edition's health board beside feed.json. Fully guarded:
 * NEVER throws, runs only after the feed write has succeeded, and a
 * health failure (build, validation, or Blob write) cannot fail or
 * delay the edition. Returns { published, url?, error? }.
 * `putImpl` is injectable for tests.
 */
export async function persistFeedHealth(feed, { archive = null, env = process.env, now = new Date(), putImpl = null } = {}) {
  try {
    const record = buildCollectorHealth({ feed, archive, env, now });
    const check = validate('source-health', record);
    if (!check.ok) {
      console.error(
        `health: record failed contract validation (not published, feed unaffected): ${check.errors.join('; ')}`
      );
      return { published: false, error: `validation: ${check.errors.join('; ')}` };
    }
    const putFn = putImpl ?? put;
    const blob = await putFn(HEALTH_PATH, JSON.stringify(record, null, 2), BLOB_WRITE_OPTS);
    console.log(`health: board published for edition ${record.generatedAt} -> ${blob?.url ?? HEALTH_PATH}`);
    return { published: true, url: blob?.url ?? null, record };
  } catch (err) {
    console.error(
      `health: publish failed (feed unaffected): ${String(err?.message || err)}`
    );
    return { published: false, error: String(err?.message || err) };
  }
}
