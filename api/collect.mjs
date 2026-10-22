/**
 * Vercel Function: scheduled collection for the 103 sports feed.
 *
 * Invoked by Vercel Cron (see vercel.json — two interleaved schedules add
 * up to every 90 minutes), or manually with the CRON_SECRET bearer token.
 * Runs the same collection pass as the CLI (collector.mjs → collectFeed())
 * and publishes the result to Vercel Blob at the stable pathname
 * `feed.json` (addRandomSuffix: false), so the public blob URL never
 * changes between runs. The GitHub Pages feed stays live as a fallback.
 *
 * Auth: when CRON_SECRET is set in the project environment, requests must
 * carry `Authorization: Bearer <CRON_SECRET>` — Vercel Cron sends that
 * header automatically. When CRON_SECRET is unset the call is allowed
 * (first-run convenience only). Responses never contain key material.
 */

import { put } from '@vercel/blob';
import { collectFeed } from '../collector.mjs';

// A collection pass can fan out to ESPN (JSON + RSS fallback per league)
// with 15s fetch timeouts; give it generous room over the platform default.
export const maxDuration = 120;

function newsArticleCount(news) {
  return Object.values(news ?? {}).reduce(
    (n, entry) => n + (Array.isArray(entry?.articles) ? entry.articles.length : 0),
    0
  );
}

export default async function handler(req, res) {
  const secret = process.env.CRON_SECRET;
  if (secret) {
    const auth = req.headers?.authorization ?? '';
    if (auth !== `Bearer ${secret}`) {
      return res.status(401).json({ ok: false, error: 'unauthorized' });
    }
  }
  try {
    const feed = await collectFeed();
    const blob = await put('feed.json', JSON.stringify(feed, null, 2), {
      access: 'public',
      addRandomSuffix: false,
      allowOverwrite: true,
      contentType: 'application/json',
    });
    const articles = newsArticleCount(feed.news);
    console.log(
      `collect ok: ${articles} news articles, ` +
        `sports configured: ${feed.sportsConfigured.join(', ') || 'none'} ` +
        `-> ${blob.url}`
    );
    return res.status(200).json({
      ok: true,
      generatedAt: feed.generatedAt,
      articles,
      sportsConfigured: feed.sportsConfigured,
      feedUrl: blob.url,
    });
  } catch (err) {
    console.error('collect failed:', err);
    return res
      .status(500)
      .json({ ok: false, error: String(err?.message || err) });
  }
}
