"""103 Sports Intelligence Engine — per-sport normalizers (Phase 1).

Converts raw API-Sports landing payloads into team-season records:

    data/intelligence/normalized/<sport>/teams_<season>.json

and, for basketball + american-football, player-season records:

    data/intelligence/normalized/<sport>/players_<season>.json

Rules (from the spec):
  * Per-sport independent handling — payloads have different shapes per
    sport and are normalized through small tolerant extractors.
  * Team records are computed from harvested GAMES (points for/against,
    games played, home/away splits). Core per-game categories merge in
    from the team-statistics payload when present.
  * Player records aggregate harvested player-statistics rows (normally
    one row per player per game) into per-game rates per category. A
    category present in NO row for a player stays null.
  * Missing fields stay null. Numbers are NEVER invented: if the raw data
    does not carry a value, the record carries null.
  * Every output file is labeled classification "Historical".

Raw payload season attribution: an entry's own season field wins; otherwise
the manifest entry for its file (the harvest params) supplies the season.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CLASSIFICATION = "Historical"

# "PST" is API-Football's postponed code; the rest are shared across the
# API-Sports family. Finished codes not listed here (FT, AOT, AP, AET, PEN)
# count as played when they carry scores.
NOT_FINISHED = {"NS", "TBD", "POSTP", "PST", "SUSP", "INT", "CANC", "ABD",
                "AWD", "WO"}

# Core team per-game categories per sport: output name -> candidate keys in
# a team-statistics payload entry (verify against first real payloads).
# Candidate keys for baseball / hockey / soccer are UNVERIFIED — the
# team-statistics response shapes for those APIs could not be confirmed
# offline; extend the lists after the first real payloads land.
CATEGORIES = {
    "basketball": {
        "rebounds_per_game": ["rebounds"],
        "assists_per_game": ["assists"],
        "steals_per_game": ["steals"],
        "blocks_per_game": ["blocks"],
    },
    "american-football": {
        "total_yards_per_game": ["total_yards", "totalYards", "yards"],
        "passing_yards_per_game": ["passing_yards", "passingYards"],
        "rushing_yards_per_game": ["rushing_yards", "rushingYards"],
    },
    "baseball": {
        "hits_per_game": ["hits"],
        "home_runs_per_game": ["home_runs", "homeruns", "homeRuns"],
        "runs_batted_in_per_game": ["runs_batted_in", "rbi", "rbis"],
    },
    "hockey": {
        "shots_on_goal_per_game": ["shots_on_goal", "shots", "sog"],
        "saves_per_game": ["saves"],
    },
    "soccer": {
        "shots_per_game": ["shots"],
        "shots_on_target_per_game": ["shots_on_target", "shotsOnTarget"],
    },
}

# Core player per-game categories (basketball + american-football only):
# output name -> {"keys": candidate field names in a player-statistics row,
#                 "group": stat-group name for grouped payloads (NFL)}.
# All candidate keys are UNVERIFIED until the first real player-statistics
# payloads land — the extractor tolerates flat fields, {"average"/"total"}
# dicts, group dicts (statistics.passing.yards), and {name, value} lists.
PLAYER_CATEGORIES = {
    "basketball": {
        "points_per_game": {"keys": ["points", "pts"]},
        "rebounds_per_game": {"keys": ["rebounds", "total_rebounds", "reb"]},
        "assists_per_game": {"keys": ["assists", "ast"]},
        "steals_per_game": {"keys": ["steals", "stl"]},
        "blocks_per_game": {"keys": ["blocks", "blk"]},
    },
    "american-football": {
        "passing_yards_per_game": {
            "keys": ["passing_yards", "passingYards", "pass_yards"],
            "group": "passing",
        },
        "rushing_yards_per_game": {
            "keys": ["rushing_yards", "rushingYards", "rush_yards"],
            "group": "rushing",
        },
        "receiving_yards_per_game": {
            "keys": ["receiving_yards", "receivingYards", "rec_yards"],
            "group": "receiving",
        },
    },
}


def _num(value):
    """Coerce an int/float/numeric-string to float; anything else -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _response(payload) -> list:
    if isinstance(payload, dict):
        resp = payload.get("response")
        if isinstance(resp, list):
            return resp
    if isinstance(payload, list):
        return payload
    return []


def _load_dataset(raw_root: Path, sport: str, dataset: str):
    """Yield (payload, manifest_params) for every raw file of a dataset."""
    base = Path(raw_root) / "raw" / sport / dataset
    if not base.exists():
        return
    for day_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifests = {}
        manifest_path = day_dir / "manifest.jsonl"
        if manifest_path.exists():
            for line in manifest_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                manifests[entry.get("file")] = entry.get("params", {})
        for path in sorted(day_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            yield payload, manifests.get(path.name, {})


def _entry_season(entry: dict, manifest_params: dict):
    season = entry.get("season")
    if season is None and isinstance(entry.get("league"), dict):
        season = entry["league"].get("season")
    if season is None:
        season = manifest_params.get("season")
    return str(season) if season is not None else None


def _game_parts(entry: dict):
    """Extract (game_id, status_short, home, away, home_score, away_score).

    Tolerates the API shapes:
      basketball: entry.home / entry.away / entry.scores / entry.status
      football:   entry.teams.home / entry.game.status / entry.scores
      baseball / hockey: entry.teams / entry.status / entry.scores
        (a side may be a plain number or a dict carrying "total"; if a
        hockey/baseball payload carries only per-period scores with no
        total, the game is skipped — VERIFY on first live run)
      soccer:     entry.teams / entry.fixture.status / entry.goals
        (API-Football fixtures: plain goal counts, no "scores" object)
    home/away are {"id":..., "name":...} dicts.
    """
    game = entry.get("game") if isinstance(entry.get("game"), dict) else entry
    if not isinstance(entry.get("game"), dict) and isinstance(
        entry.get("fixture"), dict
    ):
        game = entry["fixture"]
    teams = entry.get("teams") if isinstance(entry.get("teams"), dict) else entry
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    status = game.get("status") or entry.get("status") or {}
    status_short = status.get("short") if isinstance(status, dict) else None
    scores = entry.get("scores") or entry.get("goals") or {}
    home_score = _score_total(scores.get("home"))
    away_score = _score_total(scores.get("away"))
    game_id = game.get("id", entry.get("id"))
    return game_id, status_short, home, away, home_score, away_score


def _score_total(side) -> float | None:
    if isinstance(side, dict):
        return _num(side.get("total"))
    return _num(side)


def _category_value(entry: dict, candidate_keys: list):
    """Find a per-game category value in a team-statistics entry (or None)."""
    containers = [entry]
    if isinstance(entry.get("statistics"), dict):
        containers.append(entry["statistics"])
    for container in containers:
        for key in candidate_keys:
            if key not in container:
                continue
            raw = container[key]
            if isinstance(raw, dict):
                for sub in ("average", "per_game", "avg"):
                    if sub in raw and _num(raw[sub]) is not None:
                        return _num(raw[sub])
            elif _num(raw) is not None:
                return _num(raw)
    return None


def _collect_team_stats(raw_root: Path, sport: str, season: str) -> dict:
    """team_id -> {output_category: value} from team_statistics payloads."""
    stats: dict = {}
    for payload, manifest_params in _load_dataset(raw_root, sport, "team_statistics"):
        for entry in _response(payload):
            if not isinstance(entry, dict):
                continue
            if _entry_season(entry, manifest_params) != str(season):
                continue
            team = entry.get("team") or {}
            team_id = team.get("id")
            if team_id is None:
                continue
            cats = {}
            for out_name, keys in CATEGORIES.get(sport, {}).items():
                cats[out_name] = _category_value(entry, keys)
            stats[team_id] = cats  # later files win (freshest harvest)
    return stats


def _stat_value(raw):
    """Numeric value of a raw player-stat field, or None.

    Tolerates plain numbers / numeric strings and dict wrappers carrying
    one of the usual sub-keys ({"total": ...}, {"average": ...}, ...).
    """
    value = _num(raw)
    if value is not None:
        return value
    if isinstance(raw, dict):
        for sub in ("average", "total", "value", "yards", "per_game", "avg"):
            value = _num(raw.get(sub))
            if value is not None:
                return value
    return None


def _player_category_value(entry: dict, spec: dict):
    """Find one category value in a player-statistics row (or None).

    Layered, tolerant lookup — the real payload shapes are unverified:
      1. flat candidate keys on the row or on a dict "statistics" object;
      2. a stat-group dict (statistics.passing.yards style, NFL);
      3. a "statistics" list of {"name": ..., "value": ...} items whose
         name matches a candidate key or the group name.
    """
    keys = spec.get("keys", [])
    containers = [entry]
    stats = entry.get("statistics")
    if isinstance(stats, dict):
        containers.append(stats)
    for container in containers:
        for key in keys:
            if key in container:
                value = _stat_value(container[key])
                if value is not None:
                    return value
    group = spec.get("group")
    if group and isinstance(stats, dict) and isinstance(stats.get(group), dict):
        group_stats = stats[group]
        value = _stat_value(group_stats.get("yards"))
        if value is not None:
            return value
        for key in keys:
            if key in group_stats:
                value = _stat_value(group_stats[key])
                if value is not None:
                    return value
    if isinstance(stats, list):
        tokens = [k.lower() for k in keys] + ([group] if group else [])
        for item in stats:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("type") or "").lower()
            if not name:
                continue
            if any(tok and (tok in name or name in tok) for tok in tokens):
                value = _stat_value(item.get("value"))
                if value is not None:
                    return value
    return None


def normalize_players_season(sport: str, raw_root: Path, season: str) -> dict:
    """Build the player-season record set for one sport + season.

    Aggregates player_statistics rows (normally one row per player per
    game) into per-game rates: for each category, the summed values over
    the rows carrying that category, divided by the number of such rows.
    A payload carrying one season-average row per player therefore yields
    that average unchanged. A category carried by NO row stays null.
    Players are grouped per team — a mid-season trade yields one record
    per team stint.
    """
    season = str(season)
    categories = PLAYER_CATEGORIES.get(sport, {})
    grouped: dict = {}
    for payload, manifest_params in _load_dataset(
        raw_root, sport, "player_statistics"
    ):
        for entry in _response(payload):
            if not isinstance(entry, dict):
                continue
            if _entry_season(entry, manifest_params) != season:
                continue
            player = entry.get("player") or {}
            team = entry.get("team") or {}
            player_id = player.get("id")
            player_name = player.get("name")
            if player_id is None and player_name is None:
                continue
            key = (player_id, player_name, team.get("id"))
            slot = grouped.setdefault(key, {
                "player_id": player_id,
                "player": player_name,
                "team_id": team.get("id"),
                "team": team.get("name"),
                "rows": 0,
                "values": {name: [] for name in categories},
            })
            slot["rows"] += 1
            for out_name, spec in categories.items():
                value = _player_category_value(entry, spec)
                if value is not None:
                    slot["values"][out_name].append(value)

    records = []
    for slot in grouped.values():
        cats = {}
        for out_name, values in slot["values"].items():
            cats[out_name] = (
                round(sum(values) / len(values), 3) if values else None
            )
        records.append({
            "player_id": slot["player_id"],
            "player": slot["player"],
            "team_id": slot["team_id"],
            "team": slot["team"],
            "season": season,
            "games": slot["rows"],
            "categories": cats,
        })
    records.sort(key=lambda r: str(r["player"] or r["player_id"]))
    return {
        "sport": sport,
        "season": season,
        "classification": CLASSIFICATION,
        "players": records,
    }


def write_normalized_players(
    sport: str, raw_root: Path, out_root: Path, season: str
) -> Path:
    result = normalize_players_season(sport, raw_root, season)
    directory = Path(out_root) / "normalized" / sport
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"players_{season}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True))
    return path


def normalize_season(sport: str, raw_root: Path, season: str) -> dict:
    """Build the team-season record set for one sport + season."""
    season = str(season)
    teams: dict = {}

    def blank(team_id, name):
        return {
            "team_id": team_id,
            "team": name,
            "season": season,
            "games_played": 0,
            "_pf": 0.0, "_pa": 0.0,
            "_home_games": 0, "_home_pf": 0.0, "_home_pa": 0.0,
            "_away_games": 0, "_away_pf": 0.0, "_away_pa": 0.0,
        }

    games: dict = {}
    for payload, manifest_params in _load_dataset(raw_root, sport, "games"):
        for entry in _response(payload):
            if not isinstance(entry, dict):
                continue
            if _entry_season(entry, manifest_params) != season:
                continue
            game_id, status, home, away, hs, as_ = _game_parts(entry)
            if game_id is not None:
                games[game_id] = (status, home, away, hs, as_)  # last wins

    for _gid, (status, home, away, hs, as_) in games.items():
        if status is not None and str(status) in NOT_FINISHED:
            continue
        if hs is None or as_ is None:
            continue
        for side, is_home, scored, allowed in (
            (home, True, hs, as_),
            (away, False, as_, hs),
        ):
            team_id = side.get("id")
            if team_id is None:
                continue
            rec = teams.setdefault(team_id, blank(team_id, side.get("name")))
            rec["games_played"] += 1
            rec["_pf"] += scored
            rec["_pa"] += allowed
            if is_home:
                rec["_home_games"] += 1
                rec["_home_pf"] += scored
                rec["_home_pa"] += allowed
            else:
                rec["_away_games"] += 1
                rec["_away_pf"] += scored
                rec["_away_pa"] += allowed

    team_stats = _collect_team_stats(raw_root, sport, season)

    def per(total, n):
        return round(total / n, 3) if n else None

    records = []
    for team_id in sorted(teams, key=lambda t: str(teams[t]["team"] or t)):
        rec = teams[team_id]
        gp = rec["games_played"]
        record = {
            "team_id": rec["team_id"],
            "team": rec["team"],
            "season": season,
            "games_played": gp,
            "points_for_per_game": per(rec["_pf"], gp),
            "points_against_per_game": per(rec["_pa"], gp),
            "home_games": rec["_home_games"],
            "away_games": rec["_away_games"],
            "home_points_for_per_game": per(rec["_home_pf"], rec["_home_games"]),
            "home_points_against_per_game": per(rec["_home_pa"], rec["_home_games"]),
            "away_points_for_per_game": per(rec["_away_pf"], rec["_away_games"]),
            "away_points_against_per_game": per(rec["_away_pa"], rec["_away_games"]),
            "categories": {
                name: team_stats.get(team_id, {}).get(name)
                for name in CATEGORIES.get(sport, {})
            },
        }
        records.append(record)

    return {
        "sport": sport,
        "season": season,
        "classification": CLASSIFICATION,
        "teams": records,
    }


def write_normalized(sport: str, raw_root: Path, out_root: Path, season: str) -> Path:
    result = normalize_season(sport, raw_root, season)
    directory = Path(out_root) / "normalized" / sport
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"teams_{season}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True))
    return path


ALL_SPORTS = ["basketball", "american-football", "baseball", "hockey",
              "soccer"]


def _discover_seasons(raw_root: Path, sport: str) -> list[str]:
    """Seasons present in the raw landing, from harvest manifest params.

    The daily workflow normalizes everything harvested without naming
    seasons; an explicit --season still normalizes exactly one.
    """
    seasons = set()
    raw_dir = Path(raw_root) / "raw" / sport
    if raw_dir.exists():
        for manifest in raw_dir.rglob("manifest.jsonl"):
            for line in manifest.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                season = entry.get("params", {}).get("season")
                if season is not None:
                    seasons.add(str(season))
    return sorted(seasons)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True, choices=ALL_SPORTS)
    parser.add_argument("--season", default=None,
                        help="season to normalize (default: every season "
                             "present in the raw landing manifests)")
    parser.add_argument("--data-root", default="data/intelligence")
    args = parser.parse_args(argv)
    root = Path(args.data_root)
    seasons = ([args.season] if args.season
               else _discover_seasons(root, args.sport))
    if not seasons:
        print(f"no seasons found in raw landing for {args.sport}")
        return 0
    for season in seasons:
        path = write_normalized(args.sport, root, root, season)
        result = json.loads(path.read_text())
        print(f"{path}: {len(result['teams'])} teams "
              f"({args.sport} {season}, {CLASSIFICATION})")
        if args.sport in PLAYER_CATEGORIES:
            ppath = write_normalized_players(args.sport, root, root, season)
            presult = json.loads(ppath.read_text())
            print(f"{ppath}: {len(presult['players'])} players "
                  f"({args.sport} {season}, {CLASSIFICATION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
