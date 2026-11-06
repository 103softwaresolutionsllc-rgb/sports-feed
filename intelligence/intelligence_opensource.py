"""103 Sports Intelligence Engine — open-dataset importer (nflverse).

Imports nflverse team-season statistics (public CSVs published with the
nflverse-data GitHub releases) into the Intelligence Engine:

  * the CSV rows land in the SAME append-only raw landing as API harvests
    (dataset "opensource_team_season" under american-football), and
  * the normalized output matches the normalizer's team-season document
    shape ({sport, season, classification, teams: [...]}) so downstream
    consumers see one record shape.

Everything imported here is tagged source "open-dataset" — in the payload,
in the raw-landing manifest params, in the normalized document, and on
every record — and classification "Historical". The normalized file is
named opensource_teams_<season>.json on purpose: the aggregates loader
globs teams_*.json, and open-dataset records must never silently
overwrite or merge with API-derived records for the same season. Merging
sources is a deliberate later step, not an accident of a filename.

Data honesty:
  * nflverse team-season stats are OFFENSE-only. The normalizer shape's
    points for/against and home/away fields are not carried by the source,
    so they stay null in imported records — never invented.
  * team_id stays null (nflverse keys teams by abbreviation, not by
    API-Sports ids); the abbreviation is the team name the source carries.
  * Per-game category rates are totals divided by the games column; when
    games (or a needed total) is absent, the rate stays null.
  * total yards = passing yards + rushing yards, the standard nflverse
    convention; it is computed only when BOTH totals are present.

The CSV download happens only in the CLI path (urllib). All parsing /
conversion functions are pure and offline-testable. The release asset URL
below is a TEMPLATE: the exact asset name on the nflverse-data releases
page must be verified on the first live run (see
docs/intelligence-engine-phase1.md).

CLI:
    python3 services/intelligence_opensource.py --season 2023
    python3 services/intelligence_opensource.py --season 2023 \
        --csv-file stats_team_season_2023.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import urllib.request
from pathlib import Path

try:  # run as a script: services/ is on sys.path
    from intelligence_harvest import RawLanding
except ImportError:  # imported as services.intelligence_opensource
    from services.intelligence_harvest import RawLanding

CLASSIFICATION = "Historical"
SOURCE = "open-dataset"
SOURCE_NAME = "nflverse"
SPORT = "american-football"
DATASET = "opensource_team_season"
ENDPOINT_TAG = "nflverse:stats_team_season"

# VERIFY on first live run: release asset names on nflverse-data have
# changed over time. Confirm the real asset name for the team-season
# stats file before relying on this template.
NFLVERSE_URL_TEMPLATE = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats/stats_team_season_{season}.csv"
)

# Column candidates in the nflverse team-season CSV (first present wins).
COLUMNS = {
    "team": ["team"],
    "games": ["games", "games_played"],
    "passing_yards": ["passing_yards"],
    "rushing_yards": ["rushing_yards"],
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


def _column(row: dict, candidates: list):
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return _num(row[key])
    return None


def parse_csv(text: str) -> list[dict]:
    """CSV text -> list of row dicts (values as returned, strings)."""
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def rows_to_payload(rows: list[dict], season: str) -> dict:
    """The raw-landing payload: rows exactly as parsed, plus source tags."""
    return {
        "source": SOURCE,
        "source_name": SOURCE_NAME,
        "season": str(season),
        "response": rows,
    }


def store_raw(landing: RawLanding, rows: list[dict], season: str,
              endpoint: str = ENDPOINT_TAG) -> Path:
    """Append the parsed CSV to the raw landing (Historical, per manifest)."""
    return landing.store(
        SPORT,
        DATASET,
        endpoint,
        {"season": str(season), "source": SOURCE},
        rows_to_payload(rows, season),
    )


def rows_to_records(rows: list[dict], season: str) -> list[dict]:
    """nflverse rows -> team-season records in the normalizer's shape."""
    season = str(season)
    records = []
    for row in rows:
        team = row.get(COLUMNS["team"][0])
        if not team:
            continue
        games = _column(row, COLUMNS["games"])
        passing = _column(row, COLUMNS["passing_yards"])
        rushing = _column(row, COLUMNS["rushing_yards"])

        def per_game(total):
            if total is None or not games:
                return None
            return round(total / games, 3)

        total_yards = (
            passing + rushing
            if passing is not None and rushing is not None
            else None
        )
        records.append({
            "team_id": None,
            "team": team,
            "season": season,
            "games_played": int(games) if games is not None else None,
            # Offense-only source: points and splits are NOT carried.
            "points_for_per_game": None,
            "points_against_per_game": None,
            "home_games": None,
            "away_games": None,
            "home_points_for_per_game": None,
            "home_points_against_per_game": None,
            "away_points_for_per_game": None,
            "away_points_against_per_game": None,
            "categories": {
                "total_yards_per_game": per_game(total_yards),
                "passing_yards_per_game": per_game(passing),
                "rushing_yards_per_game": per_game(rushing),
            },
            "source": SOURCE,
        })
    records.sort(key=lambda r: str(r["team"]))
    return records


def rows_to_document(rows: list[dict], season: str) -> dict:
    return {
        "sport": SPORT,
        "season": str(season),
        "classification": CLASSIFICATION,
        "source": SOURCE,
        "teams": rows_to_records(rows, season),
    }


def write_normalized(data_root: Path, rows: list[dict], season: str) -> Path:
    """Write normalized/<sport>/opensource_teams_<season>.json."""
    doc = rows_to_document(rows, season)
    directory = Path(data_root) / "normalized" / SPORT
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"opensource_teams_{season}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return path


def import_csv_text(text: str, season: str, data_root: Path,
                    endpoint: str = ENDPOINT_TAG) -> tuple[Path, Path, dict]:
    """Full offline import: raw landing + normalized doc. Returns paths/doc."""
    rows = parse_csv(text)
    raw_path = store_raw(RawLanding(Path(data_root)), rows, season, endpoint)
    doc = rows_to_document(rows, season)
    normalized_path = write_normalized(data_root, rows, season)
    return raw_path, normalized_path, doc


def download_csv(url: str) -> str:
    """Fetch a CSV over the network. CLI path only — never used by tests."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", required=True,
                        help="nflverse season year, e.g. 2023")
    parser.add_argument("--data-root", default="data/intelligence")
    parser.add_argument("--csv-file", default=None,
                        help="import a local CSV instead of downloading")
    parser.add_argument("--url", default=None,
                        help="override the download URL (default: the "
                             "nflverse-data release template)")
    args = parser.parse_args(argv)

    if args.csv_file:
        text = Path(args.csv_file).read_text()
        endpoint = ENDPOINT_TAG
    else:
        url = args.url or NFLVERSE_URL_TEMPLATE.format(season=args.season)
        text = download_csv(url)
        endpoint = url

    raw_path, normalized_path, doc = import_csv_text(
        text, args.season, Path(args.data_root), endpoint=endpoint
    )
    print(f"raw: {raw_path}")
    print(f"{normalized_path}: {len(doc['teams'])} teams "
          f"({SPORT} {args.season}, {CLASSIFICATION}, source={SOURCE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
