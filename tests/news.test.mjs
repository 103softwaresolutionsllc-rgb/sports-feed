/**
 * Unit tests for the news normalizers in normalize.mjs.
 * Fixtures are REAL responses recorded from ESPN on 2026-10-22
 * (tests/fixtures/espn-nba-news.json, espn-nba-rss.xml).
 *
 * Run: node --test tests/news.test.mjs
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  NEWS_LEAGUES,
  MAX_NEWS_PER_LEAGUE,
  normalizeEspnNews,
  normalizeEspnArticle,
  parseEspnRss,
  truncateText,
  hashId,
} from '../normalize.mjs';

const nbaPayload = JSON.parse(
  readFileSync(new URL('./fixtures/espn-nba-news.json', import.meta.url), 'utf8')
);
const nbaRss = readFileSync(
  new URL('./fixtures/espn-nba-rss.xml', import.meta.url),
  'utf8'
);

function assertStoryShape(s) {
  assert.equal(typeof s.id, 'string');
  assert.ok(s.id.startsWith('news-'), `id shape: ${s.id}`);
  assert.equal(s.source, 'ESPN');
  assert.equal(typeof s.headline, 'string');
  assert.ok(s.headline.length > 0, 'headline non-empty');
  assert.equal(typeof s.description, 'string');
  assert.ok(
    s.description.length <= 201,
    `description truncated to ~200 chars (got ${s.description.length})`
  );
  assert.ok(s.url.startsWith('http'), `url present: ${s.url}`);
  assert.equal(s.link, s.url);
  assert.ok(s.publishedAt, 'publishedAt present');
  assert.ok(Number.isFinite(Date.parse(s.publishedAt)), 'publishedAt is ISO');
  assert.equal(s.published, s.publishedAt);
  assert.ok(Array.isArray(s.teamTags), 'teamTags is an array');
}

test('recorded ESPN NBA payload normalizes to spec-shaped stories', () => {
  const stories = normalizeEspnNews(nbaPayload, 'Basketball');
  assert.ok(stories.length >= 1, 'at least one story');
  assert.ok(stories.length <= MAX_NEWS_PER_LEAGUE, 'capped per league');
  for (const s of stories) {
    assertStoryShape(s);
    assert.equal(s.sport, 'Basketball');
    assert.equal(s.league, 'NBA');
  }
  // Newest first.
  const times = stories.map((s) => Date.parse(s.publishedAt));
  assert.deepEqual(times, [...times].sort((a, b) => b - a));
});

test('team tags are extracted from ESPN categories (type=team)', () => {
  const stories = normalizeEspnNews(nbaPayload, 'Basketball');
  const allTags = stories.flatMap((s) => s.teamTags);
  assert.ok(allTags.length > 0, 'fixture yields at least one team tag');
  // The recorded payload's lead story is about the Dallas Mavericks.
  assert.ok(
    allTags.includes('Dallas Mavericks'),
    `expected Dallas Mavericks in tags, got: ${allTags.join(', ')}`
  );
});

test('malformed articles are dropped, valid ones kept', () => {
  const payload = {
    articles: [
      {},
      null,
      'garbage',
      { headline: 'No URL here' },
      { links: { web: { href: 'https://example.com/no-headline' } } },
      {
        headline: 'Valid story',
        description: 'Something happened.',
        published: '2026-10-22T10:00:00Z',
        links: { web: { href: 'https://example.com/valid' } },
      },
    ],
  };
  const stories = normalizeEspnNews(payload, 'Football');
  assert.equal(stories.length, 1);
  assert.equal(stories[0].headline, 'Valid story');
  assert.equal(stories[0].league, 'NFL');
});

test('garbage payloads yield an empty list, never a throw', () => {
  for (const bad of [null, undefined, 42, 'x', {}, { articles: 'nope' }, { articles: null }]) {
    assert.deepEqual(normalizeEspnNews(bad, 'Basketball'), []);
  }
  assert.equal(normalizeEspnArticle(null, 'Basketball'), null);
  assert.equal(normalizeEspnArticle({ headline: 'x' }, 'NoSuchSport'), null);
});

test('duplicate URLs dedupe to a single story', () => {
  const art = {
    headline: 'Same story',
    published: '2026-10-22T10:00:00Z',
    links: { web: { href: 'https://example.com/dup' } },
  };
  const stories = normalizeEspnNews({ articles: [art, art, art] }, 'Hockey');
  assert.equal(stories.length, 1);
});

test('truncateText caps at ~200 chars on a word boundary', () => {
  const long = 'word '.repeat(100).trim(); // 499 chars
  const out = truncateText(long, 200);
  assert.ok(out.length <= 201);
  assert.ok(out.endsWith('…'));
  assert.equal(truncateText('short text'), 'short text');
  assert.equal(truncateText(null), '');
  assert.equal(truncateText('  spaced   out  '), 'spaced out');
});

test('long ESPN descriptions are truncated in normalized stories', () => {
  const payload = {
    articles: [
      {
        headline: 'Verbose',
        description: 'detail '.repeat(80),
        published: '2026-10-22T10:00:00Z',
        links: { web: { href: 'https://example.com/verbose' } },
      },
    ],
  };
  const [s] = normalizeEspnNews(payload, 'Baseball');
  assert.ok(s.description.length <= 201);
  assert.equal(s.league, 'MLB');
});

test('recorded ESPN NBA RSS parses to the same story shape', () => {
  const stories = parseEspnRss(nbaRss, 'Basketball');
  assert.ok(stories.length >= 1, 'at least one RSS story');
  for (const s of stories) assertStoryShape(s);
  // RSS carries no team tags — they stay honestly empty.
  assert.ok(stories.every((s) => s.teamTags.length === 0));
});

test('malformed RSS yields an empty list', () => {
  assert.deepEqual(parseEspnRss('', 'Basketball'), []);
  assert.deepEqual(parseEspnRss('not xml at all', 'Basketball'), []);
  assert.deepEqual(parseEspnRss(null, 'Basketball'), []);
  assert.deepEqual(parseEspnRss(nbaRss, 'NoSuchSport'), []);
});

test('NEWS_LEAGUES covers the four wired sports with ESPN + RSS endpoints', () => {
  assert.deepEqual(Object.keys(NEWS_LEAGUES), [
    'Basketball',
    'Football',
    'Baseball',
    'Hockey',
  ]);
  for (const cfg of Object.values(NEWS_LEAGUES)) {
    assert.ok(cfg.rssUrl.startsWith('https://www.espn.com/espn/rss/'));
    assert.ok(cfg.espnSport && cfg.espnLeague && cfg.leagueLabel);
  }
});

test('hashId is stable and url-sensitive', () => {
  assert.equal(hashId('https://example.com/a'), hashId('https://example.com/a'));
  assert.notEqual(hashId('https://example.com/a'), hashId('https://example.com/b'));
});
