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
  // seasonStyle: how the API's REQUIRED `season` parameter is written.
  // 'span' = "2026-2027" (season starts in October, spans two calendar
  // years); 'year' = "2026" (single calendar year). Football's /games
  // takes no season parameter (verified live 2026-10-22) — left unset.
  Basketball: { host: 'v1.basketball.api-sports.io', leagueId: 12, sportLabel: 'NBA', seasonStyle: 'span' },
  Football: { host: 'v1.american-football.api-sports.io', leagueId: 1, sportLabel: 'NFL' },
  Baseball: { host: 'v1.baseball.api-sports.io', leagueId: 1, sportLabel: 'MLB', seasonStyle: 'year' },
  Hockey: { host: 'v1.hockey.api-sports.io', leagueId: 57, sportLabel: 'NHL', seasonStyle: 'span' },
  // MMA intentionally not wired: the MMA API returns fights in a different
  // shape, to be verified against a live key before shipping a normalizer.
};

/**
 * The API-Sports `season` string for a sport on a given date, or null when
 * the sport's /games endpoint takes no season parameter. Pure date math —
 * the collector resolves it per queried day, so no /seasons discovery
 * call ever enters the request budget.
 *
 * 'span' seasons start in October: Oct–Dec opens `${y}-${y + 1}`, and
 * Jan–Sep still belongs to the season that opened the previous October
 * (an April playoff game is "2026-2027", not "2027-2028"). 'year' seasons
 * are the calendar year itself — an October World Series game is still
 * that year's season ("2026").
 */
export function seasonForDate(sport, date) {
  const cfg = SPORT_APIS[sport];
  if (!cfg || !cfg.seasonStyle) return null;
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null;
  const y = date.getFullYear();
  if (cfg.seasonStyle === 'year') return String(y);
  return date.getMonth() >= 9 ? `${y}-${y + 1}` : `${y - 1}-${y}`;
}

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

/**
 * Core game fields (id / status / date / timestamp) resolved across the
 * two payload envelopes the sport APIs use:
 *  - flat (Basketball, Baseball, Hockey): { id, date, timestamp, status,
 *    teams, scores } at the top level;
 *  - nested (American Football): id, date and status live under a `game`
 *    envelope — { game: { id, date: { timezone, date, time, timestamp },
 *    status }, teams, scores } — with NO top-level id/date/timestamp.
 * Reading only the flat fields for Football silently yields id=undefined
 * and timestamp=undefined (live symptom 2026-10-22: startTs fell back to
 * 0 and the slate rendered the epoch as "Wed, Dec 31" / "4:00 PM").
 */
function coreFields(raw) {
  const nested =
    raw?.game && typeof raw.game === 'object' ? raw.game : null;
  return {
    id: raw?.id ?? nested?.id ?? null,
    status: raw?.status ?? nested?.status ?? null,
    timestamp: raw?.timestamp ?? nested?.date?.timestamp ?? nested?.timestamp,
    date: raw?.date ?? nested?.date ?? null,
  };
}

/** 'live' | 'upcoming' | 'finished' | 'excluded' for a raw game. */
export function gameStatus(raw) {
  const short = coreFields(raw).status?.short;
  if (EXCLUDED_SHORTS.has(short)) return 'excluded';
  if (FINISHED_SHORTS.has(short)) return 'finished';
  if (short && short !== 'NS' && short !== 'TBD') return 'live';
  return 'upcoming';
}

function startTimestamp(raw) {
  const core = coreFields(raw);
  if (Number.isFinite(core.timestamp)) return core.timestamp * 1000;
  if (typeof core.date === 'string') {
    const t = Date.parse(core.date);
    if (Number.isFinite(t)) return t;
  } else if (core.date && typeof core.date === 'object') {
    // Football envelope date parts: { date: 'YYYY-MM-DD', time: 'HH:MM' }
    // in the payload's timezone (UTC for NFL games).
    const t = Date.parse(
      `${core.date.date}T${core.date.time || '00:00'}:00Z`
    );
    if (Number.isFinite(t)) return t;
  }
  return 0;
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
  const core = coreFields(raw);
  const startTs = startTimestamp(raw);
  // A game whose start time cannot be read honestly must not reach the
  // slate: startTs 0 renders as the 1969 epoch ("Wed, Dec 31").
  if (!Number.isFinite(startTs) || startTs <= 0) return null;
  const homeScore = totalScore(raw.scores?.home);
  const awayScore = totalScore(raw.scores?.away);
  return {
    id: core.id,
    sport,
    sportLabel: cfg.sportLabel,
    home,
    away,
    homeCode: sideCode(raw.teams?.home, home),
    awayCode: sideCode(raw.teams?.away, away),
    homeScore,
    awayScore,
    status,
    statusLong: core.status?.long || null,
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

// ---------------------------------------------------------------------------
// News normalizers (ESPN JSON primary, ESPN RSS fallback).
// Spec: sports-news-module-spec-2026-10-22.md — normalized story shape
// { id, source, league, headline, description, publishedAt, url, imageUrl?,
// teamTags[] }, id = hash of url (dedupe key across sources), description
// truncated to ~200 chars, newest first, cap 10–15 per league.
// Pure functions, no I/O — fetching lives in news.mjs.
// ---------------------------------------------------------------------------

export const NEWS_LEAGUES = {
  Basketball: {
    espnSport: 'basketball',
    espnLeague: 'nba',
    leagueLabel: 'NBA',
    rssUrl: 'https://www.espn.com/espn/rss/nba/news',
  },
  Football: {
    espnSport: 'football',
    espnLeague: 'nfl',
    leagueLabel: 'NFL',
    rssUrl: 'https://www.espn.com/espn/rss/nfl/news',
  },
  Baseball: {
    espnSport: 'baseball',
    espnLeague: 'mlb',
    leagueLabel: 'MLB',
    rssUrl: 'https://www.espn.com/espn/rss/mlb/news',
  },
  Hockey: {
    espnSport: 'hockey',
    espnLeague: 'nhl',
    leagueLabel: 'NHL',
    rssUrl: 'https://www.espn.com/espn/rss/nhl/news',
  },
};

export const MAX_NEWS_PER_LEAGUE = 10;
const MAX_TEAM_TAGS = 6;

/** FNV-1a hash of a string, as 8 hex chars. Used for story dedupe ids. */
export function hashId(str) {
  let h = 0x811c9dc5;
  const s = String(str || '');
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

/** Collapse whitespace; non-strings become ''. */
export function cleanText(value) {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

/** Truncate to ~max chars on a word boundary, with an ellipsis. */
export function truncateText(value, max = 200) {
  const t = cleanText(value);
  if (t.length <= max) return t;
  const cut = t.slice(0, max - 1);
  const lastSpace = cut.lastIndexOf(' ');
  const body = lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut;
  return `${body.trimEnd()}…`;
}

function toIso(value) {
  const t = Date.parse(typeof value === 'string' ? value : '');
  return Number.isFinite(t) ? new Date(t).toISOString() : null;
}

/** Dedupe by id, sort newest first (undated last), cap per-league count. */
function finishNewsList(stories) {
  const seen = new Set();
  const out = [];
  for (const s of stories) {
    if (!s || seen.has(s.id)) continue;
    seen.add(s.id);
    out.push(s);
  }
  out.sort((a, b) => {
    const ta = a.publishedAt ? Date.parse(a.publishedAt) : 0;
    const tb = b.publishedAt ? Date.parse(b.publishedAt) : 0;
    return tb - ta;
  });
  return out.slice(0, MAX_NEWS_PER_LEAGUE);
}

function makeStory({ sport, leagueLabel, headline, description, publishedIso, url, imageUrl, teamTags }) {
  const story = {
    id: `news-${hashId(url)}`,
    source: 'ESPN',
    sport,
    league: leagueLabel,
    headline,
    description: truncateText(description, 200),
    published: publishedIso,
    publishedAt: publishedIso,
    url,
    link: url,
    teamTags: teamTags || [],
  };
  if (imageUrl) story.imageUrl = imageUrl;
  return story;
}

/**
 * Normalize one raw ESPN JSON article, or null when it cannot be read
 * honestly (a story needs at least a headline and a web URL).
 * Team tags come from categories[] entries of type "team" — no guessing.
 */
export function normalizeEspnArticle(raw, sport) {
  const cfg = NEWS_LEAGUES[sport];
  if (!cfg || !raw || typeof raw !== 'object') return null;
  const headline = cleanText(raw.headline);
  const url = raw.links?.web?.href || raw.links?.mobile?.href || null;
  if (!headline || !url) return null;
  const teamTags = [];
  for (const cat of Array.isArray(raw.categories) ? raw.categories : []) {
    const name = cat?.type === 'team' ? cleanText(cat.description) : '';
    if (name && !teamTags.includes(name)) teamTags.push(name);
    if (teamTags.length >= MAX_TEAM_TAGS) break;
  }
  const image = Array.isArray(raw.images)
    ? raw.images.find((i) => i && (i.url || i.href))
    : null;
  return makeStory({
    sport,
    leagueLabel: cfg.leagueLabel,
    headline,
    description: raw.description,
    publishedIso: toIso(raw.published),
    url,
    imageUrl: image?.url || image?.href || null,
    teamTags,
  });
}

/**
 * Normalize a whole ESPN JSON news payload. Anything that is not an object
 * with an articles array yields [] — never a throw, never invented stories.
 */
export function normalizeEspnNews(payload, sport) {
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.articles)) {
    return [];
  }
  return finishNewsList(
    payload.articles.map((a) => normalizeEspnArticle(a, sport)).filter(Boolean)
  );
}

function decodeEntities(s) {
  return String(s)
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function xmlTagText(block, tag) {
  const m = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`).exec(block);
  if (!m) return null;
  let v = m[1].trim();
  const cdata = /^<!\[CDATA\[([\s\S]*?)\]\]>$/.exec(v);
  if (cdata) v = cdata[1];
  return decodeEntities(v).trim();
}

/**
 * Minimal RSS 2.0 parser for an ESPN league feed (no XML dependency).
 * Extracts <item> blocks and the same story fields the JSON path yields
 * (RSS carries no team tags or images — teamTags stays [], honestly).
 * Malformed input yields [].
 */
export function parseEspnRss(xml, sport) {
  const cfg = NEWS_LEAGUES[sport];
  if (!cfg || typeof xml !== 'string' || !xml.includes('<item')) return [];
  const stories = [];
  const itemRe = /<item[\s>][\s\S]*?<\/item>/g;
  let m;
  while ((m = itemRe.exec(xml)) !== null) {
    const block = m[0];
    const headline = cleanText(xmlTagText(block, 'title'));
    const url = xmlTagText(block, 'link');
    if (!headline || !url) continue;
    const rawDesc = xmlTagText(block, 'description') || '';
    stories.push(
      makeStory({
        sport,
        leagueLabel: cfg.leagueLabel,
        headline,
        description: rawDesc.replace(/<[^>]*>/g, ' '),
        publishedIso: toIso(xmlTagText(block, 'pubDate')),
        url,
        imageUrl: null,
        teamTags: [],
      })
    );
  }
  return finishNewsList(stories);
}

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
