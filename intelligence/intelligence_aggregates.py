"""103 Sports Intelligence Engine — aggregates (Phase 1).

Reads normalized team-season records and precomputes:
  * multi-season (simple) averages per team per metric,
  * recency-weighted averages (base weights 40/25/15/10/10, most recent
    heaviest, truncated to the seasons actually held and renormalized),
  * top-10 team leaderboards per category (by weighted average),
  * top-10 PLAYER leaderboards per core category (basketball +
    american-football, from normalized player-season records, same
    recency weighting; a player's team is the one from their most recent
    held season),
  * a summary stating EXACTLY which seasons are held per sport — the honest
    label source. Three held seasons are labeled "3-season average", never
    dressed up as five-year data.

Nulls are excluded metric-by-metric; a team or player with no value for a
metric gets no entry for that metric. Output classification: "Historical"
(these are descriptive statistics of held seasons; projections built on
them belong to the Estimated tier and are labeled separately by the
router).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CLASSIFICATION = "Historical"
BASE_WEIGHTS = [0.40, 0.25, 0.15, 0.10, 0.10]
BASE_METRICS = [
    "points_for_per_game",
    "points_against_per_game",
    "home_points_for_per_game",
    "home_points_against_per_game",
    "away_points_for_per_game",
    "away_points_against_per_game",
]
LEADERBOARD_SIZE = 10


def weights_for(n: int) -> list[float]:
    """Base recency weights truncated to n seasons, renormalized to sum 1."""
    if n <= 0:
        return []
    taken = BASE_WEIGHTS[:n]
    total = sum(taken)
    return [w / total for w in taken]


def load_sport(normalized_root: Path, sport: str) -> dict:
    """{season: [team records]} from normalized/<sport>/teams_<season>.json."""
    directory = Path(normalized_root) / "normalized" / sport
    seasons: dict = {}
    if not directory.exists():
        return seasons
    for path in sorted(directory.glob("teams_*.json")):
        try:
            doc = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        seasons[str(doc.get("season"))] = doc.get("teams", [])
    return seasons


def _metric_value(record: dict, metric: str):
    if metric in record:
        return record[metric]
    if metric in (record.get("categories") or {}):
        return record["categories"][metric]
    return None


def _all_metrics(seasons: dict) -> list[str]:
    metrics = list(BASE_METRICS)
    for records in seasons.values():
        for record in records:
            for key in (record.get("categories") or {}):
                if key not in metrics:
                    metrics.append(key)
    return metrics


def _values_by_team(seasons: dict, metric: str) -> dict:
    """team name -> {season: value} for seasons where the value is present."""
    out: dict = {}
    for season, records in seasons.items():
        for record in records:
            value = _metric_value(record, metric)
            if value is None:
                continue
            out.setdefault(record["team"], {})[season] = float(value)
    return out


def multi_season_averages(seasons: dict) -> dict:
    result: dict = {}
    for metric in _all_metrics(seasons):
        for team, by_season in _values_by_team(seasons, metric).items():
            values = list(by_season.values())
            result.setdefault(team, {})[metric] = round(
                sum(values) / len(values), 3
            )
    return result


def weighted_averages(seasons: dict) -> dict:
    held_desc = sorted(seasons, reverse=True)  # newest first
    rank_weight = {
        season: w for season, w in zip(held_desc, weights_for(len(held_desc)))
    }
    result: dict = {}
    for metric in _all_metrics(seasons):
        for team, by_season in _values_by_team(seasons, metric).items():
            total_w = sum(rank_weight[s] for s in by_season)
            if total_w == 0:
                continue
            value = sum(rank_weight[s] * v for s, v in by_season.items()) / total_w
            result.setdefault(team, {})[metric] = round(value, 3)
    return result


def leaderboards(seasons: dict, weighted: dict | None = None) -> dict:
    weighted = weighted if weighted is not None else weighted_averages(seasons)
    boards: dict = {}
    counts: dict = {}
    for metric in _all_metrics(seasons):
        for team, by_season in _values_by_team(seasons, metric).items():
            counts.setdefault(team, {})[metric] = len(by_season)
    for metric in _all_metrics(seasons):
        entries = [
            {
                "team": team,
                "value": metrics[metric],
                "seasons_counted": counts.get(team, {}).get(metric, 0),
            }
            for team, metrics in weighted.items()
            if metric in metrics
        ]
        lower_is_better = "against" in metric
        entries.sort(
            key=lambda e: (e["value"], str(e["team"])),
            reverse=not lower_is_better,
        )
        boards[metric] = {
            "order": "ascending" if lower_is_better else "descending",
            "entries": entries[:LEADERBOARD_SIZE],
        }
    return boards


def load_sport_players(normalized_root: Path, sport: str) -> dict:
    """{season: [player records]} from normalized/<sport>/players_<season>.json."""
    directory = Path(normalized_root) / "normalized" / sport
    seasons: dict = {}
    if not directory.exists():
        return seasons
    for path in sorted(directory.glob("players_*.json")):
        try:
            doc = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        seasons[str(doc.get("season"))] = doc.get("players", [])
    return seasons


def _player_values(player_seasons: dict, metric: str) -> dict:
    """player name -> {season: value} for seasons where the value exists."""
    out: dict = {}
    for season, records in player_seasons.items():
        for record in records:
            value = (record.get("categories") or {}).get(metric)
            if value is None:
                continue
            out.setdefault(record["player"], {})[season] = float(value)
    return out


def _player_metrics(player_seasons: dict) -> list[str]:
    metrics: list[str] = []
    for records in player_seasons.values():
        for record in records:
            for key in (record.get("categories") or {}):
                if key not in metrics:
                    metrics.append(key)
    return metrics


def player_weighted_averages(player_seasons: dict) -> dict:
    """player name -> {"team": <most recent season's team>, metric: value}."""
    held_desc = sorted(player_seasons, reverse=True)  # newest first
    rank_weight = {
        season: w for season, w in zip(held_desc, weights_for(len(held_desc)))
    }
    latest_team: dict = {}
    for season in held_desc:
        for record in player_seasons[season]:
            latest_team.setdefault(record["player"], record.get("team"))
    result: dict = {}
    for metric in _player_metrics(player_seasons):
        for player, by_season in _player_values(player_seasons, metric).items():
            total_w = sum(rank_weight[s] for s in by_season)
            if total_w == 0:
                continue
            value = sum(rank_weight[s] * v for s, v in by_season.items()) / total_w
            slot = result.setdefault(
                player, {"team": latest_team.get(player)}
            )
            slot[metric] = round(value, 3)
    return result


def player_leaderboards(player_seasons: dict, weighted: dict | None = None) -> dict:
    """Top-10 players per category by recency-weighted per-game average.

    Every player category is higher-is-better, so all boards descend.
    Sports with no player records held produce an empty dict.
    """
    weighted = (
        weighted if weighted is not None
        else player_weighted_averages(player_seasons)
    )
    counts: dict = {}
    for metric in _player_metrics(player_seasons):
        for player, by_season in _player_values(player_seasons, metric).items():
            counts.setdefault(player, {})[metric] = len(by_season)
    boards: dict = {}
    for metric in _player_metrics(player_seasons):
        entries = [
            {
                "player": player,
                "team": data.get("team"),
                "value": data[metric],
                "seasons_counted": counts.get(player, {}).get(metric, 0),
            }
            for player, data in weighted.items()
            if metric in data
        ]
        entries.sort(key=lambda e: (e["value"], str(e["player"])), reverse=True)
        boards[metric] = {
            "order": "descending",
            "entries": entries[:LEADERBOARD_SIZE],
        }
    return boards


def season_summary(sport: str, seasons: dict) -> dict:
    held = sorted(seasons)
    n = len(held)
    return {
        "sport": sport,
        "classification": CLASSIFICATION,
        "seasons_held": held,
        "season_count": n,
        "label": f"{n}-season average" if n else "no seasons held",
        "label_detail": (
            f"Averages cover the {n} held season(s): {', '.join(held)}."
            if n
            else "No normalized seasons are held for this sport yet."
        ),
        "teams": len({r["team"] for recs in seasons.values() for r in recs}),
    }


def build_sport_aggregates(normalized_root: Path, sport: str) -> dict:
    seasons = load_sport(normalized_root, sport)
    weighted = weighted_averages(seasons)
    return {
        "summary": season_summary(sport, seasons),
        "averages": multi_season_averages(seasons),
        "weighted_averages": weighted,
        "leaderboards": leaderboards(seasons, weighted),
        "player_leaderboards": player_leaderboards(
            load_sport_players(normalized_root, sport)
        ),
    }


def write_aggregates(normalized_root: Path, out_root: Path, sport: str) -> Path:
    result = build_sport_aggregates(normalized_root, sport)
    directory = Path(out_root) / "aggregates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sport}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True,
                        choices=["basketball", "american-football", "baseball",
                                 "hockey", "soccer"])
    parser.add_argument("--data-root", default="data/intelligence")
    args = parser.parse_args(argv)
    root = Path(args.data_root)
    path = write_aggregates(root, root, args.sport)
    summary = json.loads(path.read_text())["summary"]
    print(f"{path}: {summary['label']} over {summary['seasons_held']} "
          f"({summary['teams']} teams, {CLASSIFICATION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
