/**
 * Forward accumulation for the 103 sports feed (Intelligence Engine input).
 *
 * The Vercel Function (api/collect.mjs) has always OVERWRITTEN the single
 * public `feed.json` on every run, destroying the previous state. The
 * Intelligence Engine needs that history, so after each successful feed
 * publish this module ALSO persists a timestamped snapshot of the same
 * payload under `history/` in the same Blob store, plus a rolling index.
 *
 * Layout (same Blob store as feed.json):
 *   history/feed-<ISO timestamp, filename-sanitized>.json  — one per run,
 *       byte-identical to the feed.json payload of that run.
 *   history/index.json — { updatedAt, snapshots: [{ timestamp, path, url }] },
 *       newest first, capped at the most recent MAX_INDEX_ENTRIES entries.
 *       The cap trims the INDEX listing only; snapshot blobs beyond the cap
 *       are not deleted by this module.
 *
 * NEVER-BREAK-THE-FEED GUARANTEE: persistFeedHistory() is called only AFTER
 * the feed.json write has succeeded, catches every failure (network, Blob
 * quota, serialization, index read/write), logs it, and swallows it. It
 * never throws, and it cannot alter the feed payload or its write — the
 * feed is already published before this runs. A history outage degrades to
 * log lines; the public feed is unaffected.
 */

import { put, head } from '@vercel/blob';

export const HISTORY_INDEX_PATH = 'history/index.json';
export const MAX_INDEX_ENTRIES = 500;

const BLOB_WRITE_OPTS = {
  access: 'public',
  addRandomSuffix: false,
  allowOverwrite: true,
  contentType: 'application/json',
};

/** Blob pathname for one run's snapshot. Pure; exported for tests. */
export function snapshotPathFor(generatedAt) {
  const ts = String(generatedAt || new Date().toISOString());
  // ':' and '.' are legal in blob pathnames but unfriendly in URLs and on
  // local filesystems when snapshots are mirrored down — sanitize them.
  return `history/feed-${ts.replace(/[:.]/g, '-')}.json`;
}

/**
 * Merge one new entry into an index listing: dedupe by path (a re-run in
 * the same millisecond rewrites the same snapshot path), newest first,
 * capped at `cap` entries. Pure; exported for tests.
 */
export function mergeIndexEntries(existing, entry, cap = MAX_INDEX_ENTRIES) {
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

/**
 * Current index entries. A missing index is a normal first-run state and
 * yields []; any other read failure throws so the caller skips the index
 * update instead of clobbering a listing it could not read.
 */
async function readIndexEntries() {
  let meta;
  try {
    meta = await head(HISTORY_INDEX_PATH);
  } catch (err) {
    if (isNotFound(err)) return [];
    throw err;
  }
  const res = await fetch(meta.url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`history index fetch HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data?.snapshots) ? data.snapshots : [];
}

/**
 * Persist one run's snapshot + index update. Fully guarded: NEVER throws.
 * `feedJson` must be the exact serialized payload that was written to
 * feed.json, so snapshots stay byte-identical to what the app consumed.
 */
export async function persistFeedHistory(feedJson, generatedAt) {
  try {
    const timestamp = String(generatedAt || new Date().toISOString());
    const path = snapshotPathFor(timestamp);
    const snapshot = await put(path, feedJson, BLOB_WRITE_OPTS);
    console.log(`history: snapshot persisted -> ${snapshot.url}`);

    // The index update is best-effort on top of best-effort: if the index
    // cannot be read or written, the snapshot above still stands and a
    // later run (or a rebuild) can re-list it.
    try {
      const existing = await readIndexEntries();
      const snapshots = mergeIndexEntries(existing, {
        timestamp,
        path,
        url: snapshot.url,
      });
      const index = await put(
        HISTORY_INDEX_PATH,
        JSON.stringify(
          { updatedAt: new Date().toISOString(), snapshots },
          null,
          2
        ),
        BLOB_WRITE_OPTS
      );
      console.log(
        `history: index updated (${snapshots.length} entries) -> ${index.url}`
      );
    } catch (idxErr) {
      console.error(
        `history: index update failed (snapshot kept, feed unaffected): ${String(
          idxErr?.message || idxErr
        )}`
      );
    }
  } catch (err) {
    console.error(
      `history: snapshot persist failed (feed unaffected): ${String(
        err?.message || err
      )}`
    );
  }
}
