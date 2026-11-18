"""103 Sports Intelligence Engine — API-Sports harvester (Phase 1).

Harvest-first sourcing (spec: 103-sports-intelligence-engine-spec-2026-11-05):
pipe the APIs we already have, save everything raw, update incrementally.

Guarantees:
  * The API key is read from the APISPORTS_KEY environment variable and sent
    only as the ``x-apisports-key`` header. It is NEVER printed, logged, or
    written to disk by this module.
  * A JSON budget ledger hard-caps backfill requests per sport per UTC day
    (default 20). The harvester refuses to spend past the cap.
  * Raw landing is append-only: every payload is stored exactly as returned
    in a brand-new file (exclusive-create), plus one manifest line recording
    endpoint, params, retrieval time, and classification "Historical".
  * Endpoint paths / league ids live in SPORT_CONFIG so shapes can be
    corrected after the first real run without touching logic.

Sports: basketball + american-football (team datasets plus per-team
team-statistics and player-statistics fan-out plans), and baseball /
hockey / soccer on the same team-level path. League ids, season formats,
and endpoint paths for the three newer sports — and the player-statistics
paths for basketball / american-football — are marked VERIFY in
SPORT_CONFIG: they could not be confirmed offline and are first-live-run
checklist items (docs/intelligence-engine-phase1.md).

Dry-run mode makes NO network calls.

CLI:
    APISPORTS_KEY=... python3 services/intelligence_harvest.py
    python3 services/intelligence_harvest.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CLASSIFICATION = "Historical"
DEFAULT_DATA_ROOT = Path("data/intelligence")
DEFAULT_DAILY_CAP = 20

# --------------------------------------------------------------------------
# Per-sport configuration. Endpoint paths and league ids are parameterized
# on purpose: they must be verified against the first real payloads (see
# docs/intelligence-engine-phase1.md). "datasets" are the fixed per-season
# team-level pulls; team_statistics / player_statistics are per-team
# fan-outs planned after team ids are known.
# --------------------------------------------------------------------------
SPORT_CONFIG = {
    "basketball": {
        "base_url": "https://v1.basketball.api-sports.io",
        # NBA league id on API-Basketball — VERIFY on first live run.
        "league": 12,
        "seasons": ["2022-2023", "2023-2024", "2024-2025"],
        "datasets": {
            "teams": {"path": "/teams", "params": ["league", "season"]},
            "standings": {"path": "/standings", "params": ["league", "season"]},
            "games": {"path": "/games", "params": ["league", "season"]},
        },
        # Team statistics fan out per team once team ids are known.
        "team_statistics": {
            "path": "/statistics",
            "params": ["league", "season", "team"],
        },
        # Player statistics fan out per team once team ids are known.
        # Path VERIFY on first live run (API-Basketball player-statistics
        # endpoint name could not be confirmed offline).
        "player_statistics": {
            "path": "/players/statistics",
            "params": ["league", "season", "team"],
        },
    },
    "american-football": {
        "base_url": "https://v1.american-football.api-sports.io",
        # NFL league id on API-American-Football — VERIFY on first live run.
        "league": 1,
        "seasons": ["2022", "2023", "2024"],
        "datasets": {
            "teams": {"path": "/teams", "params": ["league", "season"]},
            "standings": {"path": "/standings", "params": ["league", "season"]},
            "games": {"path": "/games", "params": ["league", "season"]},
        },
        "team_statistics": {
            "path": "/teams/statistics",
            "params": ["league", "season", "team"],
        },
        # Player statistics fan out per team once team ids are known.
        # Path VERIFY on first live run (could not be confirmed offline).
        "player_statistics": {
            "path": "/players/statistics",
            "params": ["league", "season", "team"],
        },
    },
    "baseball": {
        "base_url": "https://v1.baseball.api-sports.io",
        # MLB league id on API-Baseball — VERIFY on first live run.
        "league": 1,
        # API-Baseball seasons are believed to be single years (like
        # american-football) — VERIFY format on first live run. The free
        # plan walled baseball to 2022-2024 in the Oct 2026 collector logs.
        "seasons": ["2022", "2023", "2024"],
        "datasets": {
            "teams": {"path": "/teams", "params": ["league", "season"]},
            "standings": {"path": "/standings", "params": ["league", "season"]},
            "games": {"path": "/games", "params": ["league", "season"]},
        },
        # Team-statistics path VERIFY on first live run.
        "team_statistics": {
            "path": "/teams/statistics",
            "params": ["league", "season", "team"],
        },
    },
    "hockey": {
        "base_url": "https://v1.hockey.api-sports.io",
        # NHL league id on API-Hockey — VERIFY on first live run (57 is
        # believed correct but could not be confirmed offline).
        "league": 57,
        # VERIFIED Nov 17, 2026 — the hard way. API-Hockey rejects the
        # "2022-2023" label: every hockey payload in the raw landing is
        # the API error {"season": "The Season field must contain an
        # integer."}, which is why hockey normalized to zero teams for
        # weeks (the normalizer was blamed; the bug was here). The API
        # wants the season's starting year as an integer — season_param
        # _for() applies "start_year" below; the label stays on the
        # task and in downstream file naming. The free plan walled
        # hockey to the 2022-2024 era seasons in the Oct 2026 logs.
        "season_format": "start_year",
        "seasons": ["2022-2023", "2023-2024", "2024-2025"],
        "datasets": {
            "teams": {"path": "/teams", "params": ["league", "season"]},
            "standings": {"path": "/standings", "params": ["league", "season"]},
            "games": {"path": "/games", "params": ["league", "season"]},
        },
        # Team-statistics path VERIFY on first live run.
        "team_statistics": {
            "path": "/teams/statistics",
            "params": ["league", "season", "team"],
        },
    },
    "soccer": {
        "base_url": "https://v3.football.api-sports.io",
        # Premier League id on API-Football — VERIFY on first live run
        # (39 is the long-standing id for the English Premier League).
        "league": 39,
        # API-Football seasons are single years — VERIFY which seasons the
        # free plan serves on first live run.
        "seasons": ["2022", "2023", "2024"],
        "datasets": {
            "teams": {"path": "/teams", "params": ["league", "season"]},
            "standings": {"path": "/standings", "params": ["league", "season"]},
            # API-Football calls games "fixtures"; the payload is stored
            # under the shared "games" dataset name so the normalize /
            # aggregate pipeline stays uniform across sports.
            "games": {"path": "/fixtures", "params": ["league", "season"]},
        },
        # Team-statistics path VERIFY on first live run (API-Football's
        # team-statistics response shape also differs — see normalizer).
        "team_statistics": {
            "path": "/teams/statistics",
            "params": ["league", "season", "team"],
        },
    },
}


class BudgetExceeded(Exception):
    """Raised when a spend would exceed the daily per-sport cap."""


class HarvestError(Exception):
    """A fetch failed. Messages never contain the API key."""


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Budget ledger
# --------------------------------------------------------------------------
class BudgetLedger:
    """JSON state file: { "<UTC day>": { "<sport>": <requests used> } }."""

    def __init__(self, state_path: Path, daily_cap: int = DEFAULT_DAILY_CAP):
        self.state_path = Path(state_path)
        self.daily_cap = int(daily_cap)
        self._state: dict = {}
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._state = {}

    def used(self, sport: str, day: str | None = None) -> int:
        day = day or utc_day()
        return int(self._state.get(day, {}).get(sport, 0))

    def remaining(self, sport: str, day: str | None = None) -> int:
        return max(0, self.daily_cap - self.used(sport, day))

    def can_spend(self, sport: str, n: int = 1, day: str | None = None) -> bool:
        return self.used(sport, day) + n <= self.daily_cap

    def spend(self, sport: str, n: int = 1, day: str | None = None) -> int:
        day = day or utc_day()
        if not self.can_spend(sport, n, day):
            raise BudgetExceeded(
                f"{sport}: daily backfill cap {self.daily_cap} reached "
                f"({self.used(sport, day)} used on {day})"
            )
        self._state.setdefault(day, {})[sport] = self.used(sport, day) + n
        self._save()
        return self.used(sport, day)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)


# --------------------------------------------------------------------------
# Raw landing (append-only)
# --------------------------------------------------------------------------
class RawLanding:
    """Stores payloads under raw/<sport>/<dataset>/<YYYY-MM-DD>/.

    Files are created with exclusive-create ("x") so an existing payload can
    never be overwritten; the sequence number in the filename increments per
    directory. A manifest.jsonl in the same directory gets one line per file.
    """

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)

    def store(
        self,
        sport: str,
        dataset: str,
        endpoint: str,
        params: dict,
        payload,
        retrieved_at: datetime | None = None,
    ) -> Path:
        retrieved_at = retrieved_at or datetime.now(timezone.utc)
        day = retrieved_at.strftime("%Y-%m-%d")
        directory = self.data_root / "raw" / sport / dataset / day
        directory.mkdir(parents=True, exist_ok=True)
        stamp = retrieved_at.strftime("%H%M%S")
        n = 1
        while True:
            path = directory / f"{stamp}-{n:03d}.json"
            try:
                with open(path, "x") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                break
            except FileExistsError:
                n += 1
        manifest_entry = {
            "file": path.name,
            "endpoint": endpoint,
            "params": params,
            "retrieved_at": retrieved_at.isoformat(),
            "classification": CLASSIFICATION,
        }
        with open(directory / "manifest.jsonl", "a") as fh:
            fh.write(json.dumps(manifest_entry, sort_keys=True) + "\n")
        return path


# --------------------------------------------------------------------------
# Fetch tasks + harvester
# --------------------------------------------------------------------------
@dataclass
class FetchTask:
    sport: str
    dataset: str
    season: str
    path: str
    params: dict

    @property
    def endpoint(self) -> str:
        return self.path


def season_param_for(cfg: dict, season):
    """The season value to SEND to a sport's API.

    Most API-Sports products take the configured label as-is
    ("2022-2023" / "2022"). Sports whose config sets
    "season_format": "start_year" (hockey) reject the label — the API
    demands the season's starting year as an integer. Only the wire
    value changes; FetchTask.season keeps the label.
    """
    if cfg.get("season_format") == "start_year":
        return int(str(season).split("-")[0])
    return season


def build_plan(sport: str) -> list[FetchTask]:
    """Fixed per-season dataset tasks (teams / standings / games)."""
    cfg = SPORT_CONFIG[sport]
    tasks: list[FetchTask] = []
    for season in cfg["seasons"]:
        for dataset, spec in cfg["datasets"].items():
            params = {}
            for key in spec["params"]:
                if key == "league":
                    params["league"] = cfg["league"]
                elif key == "season":
                    params["season"] = season_param_for(cfg, season)
            tasks.append(
                FetchTask(
                    sport=sport,
                    dataset=dataset,
                    season=season,
                    path=spec["path"],
                    params=params,
                )
            )
    return tasks


def _fanout_plan(sport: str, season: str, team_ids: list,
                 config_key: str, dataset: str) -> list[FetchTask]:
    """Per-team fan-out tasks for a config spec (team/player statistics)."""
    cfg = SPORT_CONFIG[sport]
    spec = cfg[config_key]
    tasks = []
    for team_id in team_ids:
        params = {}
        for key in spec["params"]:
            if key == "league":
                params["league"] = cfg["league"]
            elif key == "season":
                params["season"] = season_param_for(cfg, season)
            elif key == "team":
                params["team"] = team_id
        tasks.append(
            FetchTask(
                sport=sport,
                dataset=dataset,
                season=season,
                path=spec["path"],
                params=params,
            )
        )
    return tasks


def build_team_statistics_plan(
    sport: str, season: str, team_ids: list
) -> list[FetchTask]:
    """Per-team statistics tasks; run after teams are known, budget permitting."""
    return _fanout_plan(sport, season, team_ids,
                        "team_statistics", "team_statistics")


def build_player_statistics_plan(
    sport: str, season: str, team_ids: list
) -> list[FetchTask]:
    """Per-team player-statistics tasks (basketball / american-football).

    Run after teams are known, budget permitting. Sports without a
    player_statistics spec raise KeyError — player level is Phase 2 scope
    for basketball + american-football only.
    """
    return _fanout_plan(sport, season, team_ids,
                        "player_statistics", "player_statistics")


def urllib_transport(url: str, params: dict, headers: dict):
    """Default stdlib transport. Returns the parsed JSON body."""
    full_url = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — re-raised without any header data
        raise HarvestError(f"GET {url} failed: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------
# ESPN games failover — governing rule 10 (Isaac, Nov 17, 2026): "when one
# account is suspended due to whatever reason the next API takes its place
# and we keep going like nothing happened."
#
# The harvest's games datasets are the results record everything else
# builds on (normalized teams, aggregates, the Simulation Lab's fits).
# When the API-Sports payload for a games task comes back as an error
# (suspension, lockout, quota death) or empty, the SAME task is served
# from ESPN's public scoreboards and translated into the API-Sports
# shape, so the normalizer and everything downstream see one format.
# The fallback payload is stored as its own raw file with
# params.source = "espn-fallback" — provenance is never hidden, and
# the two sources are never blended inside one payload: a season's
# games come from one source or the other. ESPN is keyless, so
# fallback fetches never touch the API-Sports budget ledger.
# --------------------------------------------------------------------------

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_SPORTS = {
    "basketball": {"path": "basketball/nba", "league_name": "NBA"},
    "american-football": {"path": "football/nfl", "league_name": "NFL"},
    "baseball": {"path": "baseball/mlb", "league_name": "MLB"},
    "hockey": {"path": "hockey/nhl", "league_name": "NHL"},
}

# ESPN display names that differ from the API-Sports spellings the
# rest of the stack keys on. Applied at translation time so a
# fallback season joins cleanly with primary-source seasons.
ESPN_NAME_ALIASES = {
    "LA Clippers": "Los Angeles Clippers",
    "St. Louis Cardinals": "St.Louis Cardinals",
    "Athletics": "Oakland Athletics",
}

# Club allowlists (API-Sports spellings, post-alias). ESPN types some
# non-club events as regular season — the Feb 2025 NHL scoreboard
# carries the 4 Nations Face-Off national teams (Canada / Finland /
# Sweden / USA) as season.type 2 — so a fallback game counts only
# when BOTH sides are league clubs. All-Star sides (Team LeBron,
# American League / National League) are excluded by the same check.
# These mirror the Simulation Lab fit lists; clubs change rarely,
# and a missing club shows up as a short fallback_games count rather
# than silent pollution.
ESPN_FALLBACK_CLUBS = {
    "basketball": {
        "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
        "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
        "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
        "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat",
        "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans",
        "New York Knicks", "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers",
        "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
        "Toronto Raptors", "Utah Jazz", "Washington Wizards",
    },
    "american-football": {
        "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
        "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
        "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
        "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
        "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers",
        "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings", "New England Patriots",
        "New Orleans Saints", "New York Giants", "New York Jets", "Philadelphia Eagles",
        "Pittsburgh Steelers", "San Francisco 49ers", "Seattle Seahawks",
        "Tampa Bay Buccaneers", "Tennessee Titans", "Washington Commanders",
    },
    "baseball": {
        "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles", "Boston Red Sox",
        "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
        "Colorado Rockies", "Detroit Tigers", "Houston Astros", "Kansas City Royals",
        "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins", "Milwaukee Brewers",
        "Minnesota Twins", "New York Mets", "New York Yankees", "Oakland Athletics",
        "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres",
        "San Francisco Giants", "Seattle Mariners", "St.Louis Cardinals", "Tampa Bay Rays",
        "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
    },
    "hockey": {
        "Anaheim Ducks", "Arizona Coyotes", "Boston Bruins", "Buffalo Sabres",
        "Calgary Flames", "Carolina Hurricanes", "Chicago Blackhawks", "Colorado Avalanche",
        "Columbus Blue Jackets", "Dallas Stars", "Detroit Red Wings", "Edmonton Oilers",
        "Florida Panthers", "Los Angeles Kings", "Minnesota Wild", "Montreal Canadiens",
        "Nashville Predators", "New Jersey Devils", "New York Islanders", "New York Rangers",
        "Ottawa Senators", "Philadelphia Flyers", "Pittsburgh Penguins", "San Jose Sharks",
        "Seattle Kraken", "St. Louis Blues", "Tampa Bay Lightning", "Toronto Maple Leafs",
        "Utah Hockey Club", "Vancouver Canucks", "Vegas Golden Knights",
        "Washington Capitals", "Winnipeg Jets",
    },
}


def _espn_months(sport: str, season_label: str) -> list[str]:
    """YYYYMM scoreboard months covering one harvest season."""
    label = str(season_label)
    if sport in ("basketball", "hockey"):
        start = int(label.split("-")[0])
        months = [(start, m) for m in (10, 11, 12)] + \
                 [(start + 1, m) for m in (1, 2, 3, 4, 5, 6)]
    elif sport == "american-football":
        start = int(label)
        months = [(start, m) for m in (9, 10, 11, 12)] + \
                 [(start + 1, m) for m in (1, 2)]
    elif sport == "baseball":
        start = int(label)
        months = [(start, m) for m in (3, 4, 5, 6, 7, 8, 9, 10, 11)]
    else:
        return []
    return [f"{y}{m:02d}" for y, m in months]


def _espn_score(raw):
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def espn_games_payload(sport: str, season_label: str, transport) -> dict:
    """Fetch one season of game RESULTS from ESPN, API-Sports-shaped.

    Only regular-season and postseason games (ESPN season.type 2/3),
    only completed ones. Raises on total failure so the caller can
    record the fallback error and move on.
    """
    cfg = ESPN_SPORTS[sport]
    url = f"{ESPN_BASE}/{cfg['path']}/scoreboard"
    events: dict = {}
    for month in _espn_months(sport, season_label):
        payload = transport(url, {"dates": month, "limit": 1000},
                            {"User-Agent": "Mozilla/5.0"})
        for event in (payload or {}).get("events", []):
            events[event.get("id")] = event
    games = []
    for event in events.values():
        if not isinstance(event, dict):
            continue
        season_info = event.get("season") or {}
        if season_info.get("type") not in (2, 3):
            continue  # preseason / spring training / exhibitions
        status = (event.get("status") or {}).get("type") or {}
        if not status.get("completed"):
            continue
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        sides = {}
        for comp in competitors:
            team = comp.get("team") or {}
            name = ESPN_NAME_ALIASES.get(team.get("displayName"),
                                         team.get("displayName"))
            score = _espn_score(comp.get("score"))
            if name is None or score is None:
                sides = {}
                break
            try:
                team_id = int(team.get("id"))
            except (TypeError, ValueError):
                team_id = abs(hash(name)) % 1_000_000_000
            sides[comp.get("homeAway")] = {
                "id": team_id, "name": name, "score": score,
            }
        if set(sides) != {"home", "away"}:
            continue
        clubs = ESPN_FALLBACK_CLUBS.get(sport)
        if clubs and (sides["home"]["name"] not in clubs
                      or sides["away"]["name"] not in clubs):
            continue  # national teams, All-Star sides, exhibitions
        try:
            game_id = int(event.get("id"))
        except (TypeError, ValueError):
            continue
        games.append({
            "id": game_id,
            "date": event.get("date"),
            "league": {"id": SPORT_CONFIG[sport]["league"],
                       "name": cfg["league_name"],
                       "season": str(season_label)},
            "teams": {
                "home": {"id": sides["home"]["id"], "name": sides["home"]["name"]},
                "away": {"id": sides["away"]["id"], "name": sides["away"]["name"]},
            },
            "scores": {
                "home": {"total": sides["home"]["score"]},
                "away": {"total": sides["away"]["score"]},
            },
            "status": {"short": "FT", "long": "Finished"},
        })
    if not games:
        raise HarvestError(
            f"ESPN fallback returned no games for {sport} {season_label}")
    games.sort(key=lambda g: (g["date"] or "", g["id"]))
    return {
        "get": "games",
        "parameters": {"league": str(SPORT_CONFIG[sport]["league"]),
                       "season": str(season_label),
                       "source": "espn-fallback"},
        "errors": [],
        "results": len(games),
        "response": games,
    }


def _payload_failed(payload) -> bool:
    """An API-Sports payload that carries no usable data."""
    if not isinstance(payload, dict):
        return True
    if payload.get("errors"):
        return True
    return payload.get("results") == 0


@dataclass
class Harvester:
    api_key: str | None
    landing: RawLanding
    ledger: BudgetLedger
    transport: object = field(default=urllib_transport)
    # Policy gate (Nov 17, 2026): API-Sports suspended the account
    # because requests arrived from shared cloud IPs (GitHub Actions,
    # Vercel). The API may only be called from a dedicated-IP
    # environment that explicitly opts in with INTELLIGENCE_ALLOW_API=1
    # (the Legion runner). Everywhere else the harvest runs
    # fallback-only: no API request, no budget spend, no stored error
    # payload — rule 10 serves games from ESPN instead.
    allow_api: bool = False

    def _fetch(self, sport: str, task: FetchTask):
        cfg = SPORT_CONFIG[sport]
        url = cfg["base_url"].rstrip("/") + task.path
        headers = {"x-apisports-key": self.api_key or ""}
        return self.transport(url, task.params, headers)

    def run(self, sports: list[str], dry_run: bool = False) -> dict:
        report: dict = {}
        for sport in sports:
            tasks = build_plan(sport)
            entry = {
                "planned": len(tasks),
                "fetched": 0,
                "api_allowed": self.allow_api,
                "skipped_api_disabled": 0,
                "skipped_budget": 0,
                "fetch_errors": 0,
                "fallback_files": [],
                "fallback_games": 0,
                "fallback_errors": 0,
                "files": [],
                "budget_used_today": self.ledger.used(sport),
                "budget_cap": self.ledger.daily_cap,
                "dry_run": dry_run,
                "tasks": [
                    {"dataset": t.dataset, "season": t.season,
                     "endpoint": t.path, "params": t.params}
                    for t in tasks
                ],
            }
            if not dry_run:
                for task in tasks:
                    payload = None
                    if not self.allow_api:
                        # Fallback-only mode: never touch the API (see
                        # the allow_api policy note on the dataclass).
                        # payload stays None, so the rule-10 block
                        # below serves games from ESPN; teams and
                        # standings simply store nothing this run.
                        entry["skipped_api_disabled"] += 1
                    elif not self.ledger.can_spend(sport):
                        entry["skipped_budget"] += 1
                        continue
                    else:
                        self.ledger.spend(sport)
                        try:
                            payload = self._fetch(sport, task)
                        except Exception:  # noqa: BLE001 — recorded, not hidden
                            entry["fetch_errors"] += 1
                        if payload is not None:
                            path = self.landing.store(
                                sport, task.dataset, task.path, task.params, payload
                            )
                            entry["fetched"] += 1
                            entry["files"].append(str(path))
                    # Governing rule 10: a failed games payload (suspended
                    # account, lockout, quota death — any reason) promotes
                    # the next API in the chain automatically.
                    if task.dataset == "games" and sport in ESPN_SPORTS \
                            and _payload_failed(payload):
                        try:
                            fallback = espn_games_payload(
                                sport, task.season, self.transport)
                            fb_path = self.landing.store(
                                sport, task.dataset, task.path,
                                {**task.params, "source": "espn-fallback"},
                                fallback,
                            )
                            entry["fallback_files"].append(str(fb_path))
                            entry["fallback_games"] += fallback["results"]
                        except Exception:  # noqa: BLE001 — recorded, run continues
                            entry["fallback_errors"] += 1
                entry["budget_used_today"] = self.ledger.used(sport)
            report[sport] = entry
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only; make NO network calls")
    parser.add_argument("--sports", nargs="+", default=list(SPORT_CONFIG),
                        choices=list(SPORT_CONFIG))
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--cap", type=int, default=DEFAULT_DAILY_CAP,
                        help="daily backfill request cap per sport")
    args = parser.parse_args(argv)

    allow_api = os.environ.get("INTELLIGENCE_ALLOW_API") == "1"
    api_key = os.environ.get("APISPORTS_KEY")
    if not args.dry_run and allow_api and not api_key:
        print("INTELLIGENCE_ALLOW_API=1 but APISPORTS_KEY is not set; "
              "refusing to harvest (use --dry-run to plan without a "
              "key).", file=sys.stderr)
        return 2

    data_root = Path(args.data_root)
    harvester = Harvester(
        api_key=api_key,
        landing=RawLanding(data_root),
        ledger=BudgetLedger(data_root / "harvest_state.json", daily_cap=args.cap),
        allow_api=allow_api,
    )
    report = harvester.run(args.sports, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
