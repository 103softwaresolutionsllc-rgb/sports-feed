/**
 * fetch-results.mjs — archive final scores for the grading ledger.
 *
 *   node grading/fetch-results.mjs --root <data-branch checkout>
 *        [--months 2026-11,2026-10] [--sports NBA,NFL,NHL]
 *
 * Source: ESPN's public scoreboards (site.api.espn.com) — the same
 * keyless failover source the Intelligence Engine harvest already
 * uses for games and player data (rule 10). One bounded request per
 * sport per month; nothing else is called. Results are written to
 * <root>/data/grades/results/espn-<SPORT>-<YYYY-MM>.json with their
 * provenance, so the grading job (run.mjs) reproduces every grade
 * from archived files and never calls a provider itself.
 *
 * Only COMPLETED events are archived. Team names are stored as ESPN
 * publishes them; matching canonicalizes names at read time
 * (results.mjs), with the known aliases recorded there.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { gradesDir } from './store.mjs';

const ESPN_PATHS = {
  NBA: 'basketball/nba',
  NFL: 'football/nfl',
  NHL: 'hockey/nhl',
  MLB: 'baseball/mlb',
};

function parseArgs(argv) {
  const args = { root: null, months: null, sports: ['NBA', 'NFL', 'NHL'] };
  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--root') args.root = argv[++i];
    else if (arg === '--months') args.months = argv[++i].split(',').map((s) => s.trim());
    else if (arg === '--sports') args.sports = argv[++i].split(',').map((s) => s.trim());
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (!args.root) throw new Error('--root <data-branch checkout> is required');
  if (!args.months) {
    const now = new Date();
    const prev = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
    const fmt = (d) => `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
    args.months = [fmt(now), fmt(prev)];
  }
  return args;
}

export function finalsFromEspnScoreboard(payload, sport) {
  const games = [];
  for (const event of payload?.events || []) {
    if (!event?.status?.type?.completed) continue;
    const competition = event.competitions?.[0];
    const competitors = competition?.competitors || [];
    const home = competitors.find((c) => c.homeAway === 'home');
    const away = competitors.find((c) => c.homeAway === 'away');
    if (!home || !away) continue;
    const homeScore = Number(home.score);
    const awayScore = Number(away.score);
    if (!Number.isFinite(homeScore) || !Number.isFinite(awayScore)) continue;
    games.push({
      sport,
      date: event.date,
      home: home.team?.displayName,
      away: away.team?.displayName,
      homeScore,
      awayScore,
    });
  }
  return games;
}

async function main() {
  const args = parseArgs(process.argv);
  const resultsDir = join(gradesDir(args.root), 'results');
  mkdirSync(resultsDir, { recursive: true });
  const summary = [];
  for (const sport of args.sports) {
    const path = ESPN_PATHS[sport];
    if (!path) throw new Error(`no ESPN path for sport ${sport}`);
    for (const month of args.months) {
      const yyyymm = month.replace('-', '');
      const url = `https://site.api.espn.com/apis/site/v2/sports/${path}/scoreboard?dates=${yyyymm}&limit=1000`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`ESPN ${sport} ${month} HTTP ${res.status}`);
      const payload = await res.json();
      const games = finalsFromEspnScoreboard(payload, sport);
      const file = join(resultsDir, `espn-${sport}-${month}.json`);
      writeFileSync(
        file,
        JSON.stringify(
          { source: 'espn-fallback', sport, month, fetchedAt: new Date().toISOString(), url, games },
          null,
          2
        ) + '\n',
        'utf8'
      );
      summary.push({ sport, month, finals: games.length, file });
    }
  }
  console.log(JSON.stringify(summary, null, 2));
}

const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  main().catch((err) => {
    console.error(`fetch-results failed: ${String(err?.message || err)}`);
    process.exit(1);
  });
}
