/**
 * API-Sports payload normalizers for the 103 sports-feed collector.
 *
 * Pure functions, no I/O. Parsing is defensive: team/score/status shapes
 * vary slightly across the per-sport APIs, so normalizers accept the
 * documented variants and return null for anything they cannot read
 * honestly. Odds bets are matched by the SHAPE of their values (a Home/Away
 * pair = moneyline; "Home -4.5" style values = spread) rather than by bet
 * names, which differ per sport.
 *
 * "How far off" honesty rule for picks: a pick card shows the bookmaker's
 * favorite and the probability IMPLIED BY THE ODDS — never a model
 * probability, never "simulations".
 */

export const SPORT_APIS = {
  Basketball: { host: 'v1.basketball.api-sports.io', leagueId: 12, sportLabel: 'NBA' },
  Football: { host: 'v1.american-football.api-sports.io', leagueId: 1, sportLabel: 'NFL' },
  Baseball: { host: 'v1.baseball.api-sports.io', leagueId: 1, sportLabel: 'MLB' },
  Hockey: { host: 'v1.hockey.api-sports.io', leagueId: 57, sportLabel: 'NHL' },
  // MMA intentionally not wired: the MMA API returns fights in a different
  // shape, to be verified against a live key before shipping a normalizer.
};

const FINISHED_SHORTS = new Set(['FT', 'AOT', 'AP']);
const EXCLUDED_SHORTS = new Set(['POSTP', 'CANC', 'SUSP', 'ABD']);

/** Implied probability (0–100, rounded) of a decimal odd. */
export function decimalToImpliedPct(decimalOdd) {
  const d = Number(decimalOdd);
  if (!Number.isFinite(d) || d <= 1) return null;
  return Math.round(100 / d);
}

/** Decimal odd → American odd string ('-150' / '+130'). */
export function decimalToAmerican(decimalOdd) {
  const d = Number(decimalOdd);
  if (!Number.isFinite(d) || d <= 1) return null;
  if (d >= 2) return `+${Math.round((d - 1) * 100)}`;
  return `${Math.round(-100 / (d - 1))}`;
}

function sideName(side) {
  if (typeof side === 'string') return side;
  return side?.name || null;
}

function sideCode(side, fallbackName) {
  if (side && typeof side === 'object' && side.code) return side.code;
  if (!fallbackName) return '—';
  return fallbackName.replace(/[^A-Za-z]/g, '').slice(0, 3).toUpperCase() || '—';
}

/** Total score for one side: explicit total, else sum of period values. */
export function totalScore(sideScores) {
  if (!sideScores || typeof sideScores !== 'object') return null;
  if (Number.isFinite(sideScores.total)) return sideScores.total;
  let sum = 0;
  let found = false;
  for (const [k, v] of Object.entries(sideScores)) {
    if (/^(quarter|period|inning)_?\d+$/.test(k) && Number.isFinite(v)) {
      sum += v;
      found = true;
    }
  }
  return found ? sum : null;
}

/** 'live' | 'upcoming' | 'finished' | 'excluded' for a raw game. */
export function gameStatus(raw) {
  const short = raw?.status?.short;
  if (EXCLUDED_SHORTS.has(short)) return 'excluded';
  if (FINISHED_SHORTS.has(short)) return 'finished';
  if (short && short !== 'NS' && short !== 'TBD') return 'live';
  return 'upcoming';
}

function startTimestamp(raw) {
  if (Number.isFinite(raw?.timestamp)) return raw.timestamp * 1000;
  const t = Date.parse(raw?.date || '');
  return Number.isFinite(t) ? t : 0;
}

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'America/Los_Angeles',
  });
}

function dayLabel(ts) {
  const d = new Date(ts);
  const now = new Date();
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (sameDay(d, now)) return 'Today';
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (sameDay(d, tomorrow)) return 'Tomorrow';
  return d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'America/Los_Angeles',
  });
}

/**
 * Normalize a raw API-Sports game into the internal shape, or null when the
 * game cannot be read honestly (missing teams, excluded/finished status —
 * finished games belong to results, not the upcoming slate).
 */
export function normalizeGame(raw, sport) {
  const cfg = SPORT_APIS[sport];
  if (!cfg || !raw) return null;
  const status = gameStatus(raw);
  if (status === 'excluded' || status === 'finished') return null;
  const home = sideName(raw.teams?.home);
  const away = sideName(raw.teams?.away);
  if (!home || !away) return null;
  const startTs = startTimestamp(raw);
  const homeScore = totalScore(raw.scores?.home);
  const awayScore = totalScore(raw.scores?.away);
  return {
    id: raw.id,
    sport,
    sportLabel: cfg.sportLabel,
    home,
    away,
    homeCode: sideCode(raw.teams?.home, home),
    awayCode: sideCode(raw.teams?.away, away),
    homeScore,
    awayScore,
    status,
    statusLong: raw.status?.long || null,
    startTs,
  };
}

/** Slate item in the exact shape the BetTracker Home slate cards render. */
export function toSlateItem(game) {
  let timeLabel;
  let detailLabel;
  if (game.status === 'live') {
    timeLabel =
      game.homeScore != null && game.awayScore != null
        ? `Live · ${game.awayCode} ${game.awayScore}–${game.homeScore} ${game.homeCode}`
        : 'Live';
    detailLabel = game.statusLong || 'In progress';
  } else {
    timeLabel = formatTime(game.startTs);
    detailLabel = dayLabel(game.startTs);
  }
  return {
    id: `apisp-slate-${game.sport}-${game.id}`,
    sport: game.sport,
    sportLabel: game.sportLabel,
    teams: `${game.away} vs ${game.home}`,
    time: timeLabel,
    detail: detailLabel,
    status: game.status,
    startTs: game.startTs,
  };
}

/**
 * Extract the favorite's line from a raw /odds response for one game.
 * Returns null when no moneyline can be read. Matching is by value shape:
 * a bet whose values are a Home/Away pair is the moneyline; a bet with
 * values like "Home -4.5" is the spread.
 */
export function extractFavoriteLine(oddsPayload, game) {
  const entries = Array.isArray(oddsPayload) ? oddsPayload : oddsPayload?.response;
  if (!Array.isArray(entries) || entries.length === 0) return null;
  for (const entry of entries) {
    for (const bookmaker of entry.bookmakers || []) {
      let ml = null;
      let spread = null;
      for (const bet of bookmaker.bets || []) {
        const values = bet.values || [];
        const homeVal = values.find((v) => v.value === 'Home');
        const awayVal = values.find((v) => v.value === 'Away');
        if (!ml && homeVal && awayVal) {
          ml = { home: Number(homeVal.odd), away: Number(awayVal.odd) };
          continue;
        }
        if (!spread) {
          for (const v of values) {
            const m = /^(Home|Away)\s*([+-]\d+(?:\.\d+)?)$/.exec(String(v.value || ''));
            if (m) {
              spread = { side: m[1] === 'Home' ? 'home' : 'away', line: m[2] };
              break;
            }
          }
        }
      }
      if (ml && Number.isFinite(ml.home) && Number.isFinite(ml.away)) {
        const favSide = ml.home <= ml.away ? 'home' : 'away';
        const favDec = favSide === 'home' ? ml.home : ml.away;
        return {
          bookmaker: bookmaker.name || 'Bookmaker',
          favSide,
          favName: favSide === 'home' ? game.home : game.away,
          favDecimal: favDec,
          impliedPct: decimalToImpliedPct(favDec),
          spread: spread && spread.side === favSide ? spread.line : null,
        };
      }
    }
  }
  return null;
}

const BAR_BY_BADGE = { high: '#4a9eff', signal: '#22c55e', caution: '#eab308' };

/** Pick card in the exact shape the BetTracker Home "Notable Lines" cards render. */
export function toPickCard(game, line) {
  const pct = line.impliedPct;
  const badgeType = pct >= 65 ? 'high' : pct >= 55 ? 'signal' : 'caution';
  const badge =
    pct >= 65 ? 'Strong favorite' : pct >= 55 ? 'Bookmaker favorite' : 'Close line';
  return {
    id: `apisp-pick-${game.sport}-${game.id}`,
    sport: game.sport,
    sportLabel: game.sportLabel,
    hot: pct >= 70,
    badge,
    badgeType,
    teamA: game.away,
    teamB: game.home,
    pick: `${line.favName} ${line.spread ?? 'ML'}`,
    winProb: pct,
    barColor: BAR_BY_BADGE[badgeType],
    footer: `${line.bookmaker} · ${line.spread ? 'Spread + moneyline' : 'Moneyline'} · ${dayLabel(game.startTs)} ${formatTime(game.startTs)}`,
    startTs: game.startTs,
  };
}
