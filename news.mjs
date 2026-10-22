/**
 * ESPN news fetcher for the 103 sports-feed collector.
 *
 * Primary source: ESPN's unofficial per-league news JSON
 * (https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news) —
 * no key, no quota; it powers ESPN's own apps. Fallback: the league's ESPN
 * RSS feed, parsed to the identical story shape (see normalize.mjs).
 *
 * Honesty rules (spec): failures are reported per league in the feed's
 * news entries as `error` with an empty article list — never a throw past
 * collectNews(), and never invented placeholder stories.
 */

import {
  NEWS_LEAGUES,
  normalizeEspnNews,
  parseEspnRss,
} from './normalize.mjs';

const FETCH_TIMEOUT_MS = 15000;
const HEADERS = {
  'user-agent':
    '103-sports-feed-collector/1.0 (+https://github.com/103softwaresolutionsllc-rgb/sports-feed)',
};

async function fetchJson(url) {
  const res = await fetch(url, {
    headers: HEADERS,
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchText(url) {
  const res = await fetch(url, {
    headers: HEADERS,
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

/**
 * News for one sport: ESPN JSON first, RSS if the JSON fails or yields no
 * usable articles. Returns { articles, error, via } — error is null on
 * success; via records which source served ('espn-json' | 'espn-rss').
 */
export async function fetchLeagueNews(sport) {
  const cfg = NEWS_LEAGUES[sport];
  if (!cfg) {
    return { articles: [], error: `unknown sport: ${sport}`, via: null };
  }
  const jsonUrl = `https://site.api.espn.com/apis/site/v2/sports/${cfg.espnSport}/${cfg.espnLeague}/news`;
  let jsonProblem;
  try {
    const payload = await fetchJson(jsonUrl);
    const articles = normalizeEspnNews(payload, sport);
    if (articles.length > 0) return { articles, error: null, via: 'espn-json' };
    jsonProblem = 'ESPN JSON returned no usable articles';
  } catch (err) {
    jsonProblem = `ESPN JSON failed: ${err?.message || err}`;
  }
  try {
    const xml = await fetchText(cfg.rssUrl);
    const articles = parseEspnRss(xml, sport);
    if (articles.length > 0) return { articles, error: null, via: 'espn-rss' };
    return {
      articles: [],
      error: `${jsonProblem}; RSS fallback returned no usable articles`,
      via: null,
    };
  } catch (err) {
    return {
      articles: [],
      error: `${jsonProblem}; RSS fallback failed: ${err?.message || err}`,
      via: null,
    };
  }
}

/**
 * The feed's top-level `news` object: a fetchedAt timestamp plus one entry
 * per sport ({ league, via, articles, error }). Fully guarded — a news
 * failure can never take down the rest of the feed run.
 */
export async function collectNews() {
  const news = { fetchedAt: new Date().toISOString() };
  for (const sport of Object.keys(NEWS_LEAGUES)) {
    const cfg = NEWS_LEAGUES[sport];
    try {
      const { articles, error, via } = await fetchLeagueNews(sport);
      news[sport] = { league: cfg.leagueLabel, via, articles, error };
      console.log(
        `News ${sport} (${cfg.leagueLabel}): ${articles.length} articles` +
          (via ? ` via ${via}` : '') +
          (error ? ` — ${error}` : '')
      );
    } catch (err) {
      news[sport] = {
        league: cfg.leagueLabel,
        via: null,
        articles: [],
        error: String(err?.message || err),
      };
      console.error(`News ${sport}: FAILED — ${news[sport].error}`);
    }
  }
  return news;
}
