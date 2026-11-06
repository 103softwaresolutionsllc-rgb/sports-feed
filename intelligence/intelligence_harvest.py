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
        # Season format believed to be "2022-2023" style (like basketball)
        # — VERIFY on first live run. The free plan walled hockey to the
        # 2022-2024 era seasons in the Oct 2026 collector logs.
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
                    params["season"] = season
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
                params["season"] = season
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


@dataclass
class Harvester:
    api_key: str | None
    landing: RawLanding
    ledger: BudgetLedger
    transport: object = field(default=urllib_transport)

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
                "skipped_budget": 0,
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
                    if not self.ledger.can_spend(sport):
                        entry["skipped_budget"] += 1
                        continue
                    self.ledger.spend(sport)
                    payload = self._fetch(sport, task)
                    path = self.landing.store(
                        sport, task.dataset, task.path, task.params, payload
                    )
                    entry["fetched"] += 1
                    entry["files"].append(str(path))
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

    api_key = os.environ.get("APISPORTS_KEY")
    if not args.dry_run and not api_key:
        print("APISPORTS_KEY is not set; refusing to harvest "
              "(use --dry-run to plan without a key).", file=sys.stderr)
        return 2

    data_root = Path(args.data_root)
    harvester = Harvester(
        api_key=api_key,
        landing=RawLanding(data_root),
        ledger=BudgetLedger(data_root / "harvest_state.json", daily_cap=args.cap),
    )
    report = harvester.run(args.sports, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
