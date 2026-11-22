"""gapfill_injuries_nba.py — Sage Gap-Filler, first collector (G1).

Collects the NBA's OFFICIAL injury report (design input S4, grade B)
and publishes it as a COLLECTED layer beside the Intelligence
Engine's provider data — never inside it. Spec:
~/workspace/your_files/sage-gap-filler-design-input-2026-11-16.md
(Phase 1; decisions D1/D2/D4 settled Nov 16, 2026).

Binding rules inherited from that spec:
  * Provenance is the product. Every published record carries the
    section 4.2 provenance block: origin "collected", source name,
    the exact URL fetched, fetched-at, collector name,
    classification "Historical" (a collected record NEVER surfaces
    as Live), and an expiry.
  * Empty slots only. This collector publishes to its own layer
    (collected/injuries_NBA.json); it writes nothing into provider
    aggregates, game logs, or the registry.
  * Identity: records key to the permanent player registry by
    normalized name (team as corroboration). A row that cannot be
    keyed is STAGED, not published — visible in the staging file
    and the run report, never silently dropped or guessed into a
    plausible-looking record.
  * Expiry (spec 4.5): an availability record expires at game
    start + 2 hours. Expired records leave the published layer on
    the next run; the raw PDF stays in the landing for audit.
  * Failures are visible: a failed fetch or parse reports its
    reason class and publishes nothing new (the previous layer is
    left untouched).
  * Bounded: one page fetch + one PDF fetch per run, one retry on
    a network error, no crawling. The collector knows its target;
    it cannot be pointed at an arbitrary URL (an explicit --url is
    accepted only for the same ak-static injury-report host/path,
    for replay and tests).

Parsing: the report is a PDF whose reason cells wrap and are
vertically centered, so pdftotext -layout interleaves fragments.
The parser works from pdftotext -bbox word coordinates instead:
columns are calibrated from the header row, rows cluster by y,
status-bearing lines anchor records, and reason-only orphan lines
attach to the adjacent anchor whose cell they complete (top
fragment precedes its anchor, bottom fragment follows it).

CLI:
  python3 gapfill_injuries_nba.py --data-root <path>
Writes <data-root>/collected/injuries_NBA.json (published layer),
<data-root>/collected/staging/injuries_NBA.json (unkeyed rows),
the raw PDF under <data-root>/raw/collected/nba_injury_report/,
and prints a run report.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PAGE_URL = "https://official.nba.com/nba-injury-report/"
PDF_HOST_PATH = ("ak-static.cms.nba.com", "/referee/injury/")
SOURCE_NAME = "NBA official injury report"
COLLECTOR = "gapfill_injuries_nba"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
STATUSES = {"Out", "Doubtful", "Questionable", "Probable", "Available"}
EASTERN = ZoneInfo("America/New_York")

# Fallback column left-edges (points) from the verified 2026-11-21
# report, used only when the header row cannot be located.
_FALLBACK_COLUMNS = {"date": 20, "time": 115, "matchup": 195,
                     "team": 260, "player": 420, "status": 580,
                     "reason": 660}
_COLUMN_ORDER = ["date", "time", "matchup", "team", "player",
                 "status", "reason"]


# ---------------------------------------------------------------- fetch

def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _fetch_with_retry(url: str) -> bytes:
    try:
        return _fetch(url)
    except Exception:
        return _fetch(url)  # one retry, then the failure propagates


def find_latest_report_url(page_html: str) -> str | None:
    """The newest Injury-Report PDF linked from the report page."""
    links = sorted(set(re.findall(
        r'href="(https://ak-static\.cms\.nba\.com/referee/injury/'
        r'Injury-Report_[^"]+\.pdf)"', page_html)))
    def stamp(url: str):
        m = re.search(r"Injury-Report_(\d{4})-(\d{2})-(\d{2})_"
                      r"(\d{2})_(\d{2})(AM|PM)", url)
        if not m:
            return (0,)
        y, mo, d, h, mi, ap = m.groups()
        hour = int(h) % 12 + (12 if ap == "PM" else 0)
        return (int(y), int(mo), int(d), hour, int(mi))
    return max(links, key=stamp) if links else None


# ---------------------------------------------------------------- parse

def words_from_bbox(xml_bytes: bytes) -> list[dict]:
    """pdftotext -bbox output -> flat word list {p, y, x, t}."""
    root = ET.fromstring(xml_bytes)
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    words = []
    for pi, page in enumerate(root.findall(".//x:page", ns)):
        for w in page.findall(".//x:word", ns):
            words.append({"p": pi, "y": round(float(w.get("yMin")), 1),
                          "x": round(float(w.get("xMin")), 1),
                          "t": w.text or ""})
    return words


def words_from_pdf(pdf_path: Path) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".xml") as tmp:
        subprocess.run(["pdftotext", "-bbox", str(pdf_path), tmp.name],
                       check=True, capture_output=True, timeout=60)
        return words_from_bbox(Path(tmp.name).read_bytes())


def _cluster_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for page in sorted({w["p"] for w in words}):
        pw = sorted((w for w in words if w["p"] == page),
                    key=lambda w: (w["y"], w["x"]))
        cur: list[dict] = []
        cur_y: float | None = None
        for w in pw:
            if cur_y is None or abs(w["y"] - cur_y) > 2.5:
                if cur:
                    lines.append(cur)
                cur, cur_y = [], w["y"]
            cur.append(w)
        if cur:
            lines.append(cur)
    return [sorted(line, key=lambda w: w["x"]) for line in lines]


def _calibrate_columns(lines: list[list[dict]]) -> dict[str, float]:
    """Column left-edges from the header row. The header reads
    'Game Date | Game Time | Matchup | Team | Player Name |
    Current Status | Reason' — 'Game' appears twice, so positions
    are taken positionally, never keyed by word text."""
    for line in lines:
        texts = [w["t"] for w in line]
        if texts[:2] == ["Game", "Date"] and "Reason" in texts:
            by_text = {}
            for w in line:
                by_text.setdefault(w["t"], w["x"])
            games = [w["x"] for w in line if w["t"] == "Game"]
            return {"date": games[0],
                    "time": games[1] if len(games) > 1
                            else games[0] + 97,
                    "matchup": by_text.get("Matchup", games[0] + 177),
                    "team": by_text.get("Team", games[0] + 241),
                    "player": by_text.get("Player", games[0] + 402),
                    "status": by_text.get("Current", games[0] + 563),
                    "reason": by_text.get("Reason", games[0] + 643)}
    return dict(_FALLBACK_COLUMNS)


def _column_of(x: float, cols: dict[str, float]) -> str:
    name = "date"
    for col in _COLUMN_ORDER:
        if x >= cols[col] - 6:
            name = col
    return name


def _line_cells(line: list[dict], cols: dict[str, float]) -> dict[str, str]:
    cells: dict[str, list[str]] = {c: [] for c in _COLUMN_ORDER}
    for w in line:
        cells[_column_of(w["x"], cols)].append(w["t"])
    return {c: " ".join(v) for c, v in cells.items()}


def parse_report(words: list[dict]) -> tuple[list[dict], dict]:
    """Words -> availability rows with carry-forward game context.

    Returns (rows, stats). A row: {game_date, game_time, matchup,
    team, player_name ("Last, First" as printed), status, reason}.
    Rows whose status cell is not in the closed vocabulary are
    counted as dropped, never repaired into plausible data.
    """
    lines = _cluster_lines(words)
    cols = _calibrate_columns(lines)
    rows: list[dict] = []
    stats = {"lines_seen": 0, "anchors": 0, "dropped_no_status": 0,
             "orphan_reason_lines": 0}
    ctx = {"date": None, "time": None, "matchup": None, "team": None}
    # pending orphan reason fragments waiting for their anchor
    pending_top: list[str] = []

    def is_reason_only(cells: dict[str, str]) -> bool:
        return bool(cells["reason"]) and not any(
            cells[c] for c in ("date", "time", "matchup", "team",
                               "player", "status"))

    i = 0
    while i < len(lines):
        cells = _line_cells(lines[i], cols)
        i += 1
        stats["lines_seen"] += 1
        joined = " ".join(cells.values())
        if not joined.strip() \
                or cells["team"] in ("Page", "Injury Report:") \
                or cells["player"] == "Player Name" \
                or cells["date"].startswith("Game"):
            continue
        if cells["date"]:
            ctx["date"] = cells["date"]
        if cells["time"]:
            ctx["time"] = cells["time"]
        if cells["matchup"]:
            ctx["matchup"] = cells["matchup"]
        if cells["team"]:
            ctx["team"] = cells["team"]
        if is_reason_only(cells):
            pending_top.append(cells["reason"])
            stats["orphan_reason_lines"] += 1
            continue
        if not cells["player"]:
            continue
        status = cells["status"].strip()
        if status not in STATUSES:
            stats["dropped_no_status"] += 1
            pending_top = []
            continue
        stats["anchors"] += 1
        reason_parts: list[str] = []
        if cells["reason"]:
            reason_parts.append(cells["reason"])
        else:
            # Wrapped cell: top fragment(s) precede the anchor,
            # bottom fragment(s) follow it as reason-only lines.
            reason_parts.extend(pending_top)
            while i < len(lines):
                nxt = _line_cells(lines[i], cols)
                if is_reason_only(nxt):
                    reason_parts.append(nxt["reason"])
                    stats["orphan_reason_lines"] += 1
                    i += 1
                else:
                    break
        pending_top = []
        rows.append({
            "game_date": ctx["date"], "game_time": ctx["time"],
            "matchup": ctx["matchup"], "team": ctx["team"],
            "player_name": cells["player"], "status": status,
            "reason": " ".join(reason_parts).strip(),
        })
    return rows, stats


# ---------------------------------------------------------------- records

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def normalize_name(name: str) -> str:
    """'Dieng, Ousmane' / 'Ousmane Dieng' -> comparable key."""
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    tokens = [t.strip(".,").lower() for t in name.split()]
    tokens = [t for t in tokens if t and t not in _SUFFIXES]
    return " ".join(tokens)


def report_name_to_display(name: str) -> str:
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}"
    return name.strip()


def load_registry_index(registry_path: Path) -> dict[str, list[dict]]:
    """normalized name -> [{player_id, full_name, current_team}]."""
    raw = json.loads(registry_path.read_text())
    players = raw.get("players") or {}
    if isinstance(players, list):
        players = {p["player_id"]: p for p in players}
    index: dict[str, list[dict]] = {}
    for pid, p in players.items():
        history = p.get("team_history") or []
        team = history[-1]["team"] if history else None
        index.setdefault(normalize_name(p.get("full_name", "")), []).append(
            {"player_id": pid, "full_name": p.get("full_name"),
             "current_team": team})
    return index


def key_row(row: dict, index: dict[str, list[dict]]):
    """(player record or None, staging reason or None)."""
    candidates = index.get(normalize_name(row["player_name"])) or []
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        team_matches = [c for c in candidates
                        if c["current_team"] == row["team"]]
        if len(team_matches) == 1:
            return team_matches[0], None
        return None, "ambiguous_identity"
    return None, "no_registry_match"


def game_start_utc(game_date: str | None, game_time: str | None):
    """'11/21/2026' + '07:30 (ET)' -> aware UTC datetime or None.

    The report prints game times on a 12-hour clock with NO meridiem
    marker. NBA tip times are afternoon/evening, so the bare hour is
    read as PM (12 = noon): '07:30' is 19:30 ET, never 07:30 ET.
    Reading it as AM — the original bug — stamped every expiry ~12
    hours early, so game-day records expired before they published.
    """
    if not game_date:
        return None
    try:
        day = datetime.strptime(game_date.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None
    hour, minute = 23, 59
    if game_time:
        m = re.search(r"(\d{1,2}):(\d{2})", game_time)
        if m:
            hour = int(m.group(1)) % 12 + 12  # bare 12-hour time = PM
            minute = int(m.group(2))
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=EASTERN).astimezone(timezone.utc)


def build_records(rows: list[dict], index: dict[str, list[dict]],
                  source_url: str, fetched_at: datetime,
                  report_ts: datetime | None):
    """Rows -> (published records, staged rows). Provenance per 4.2."""
    fetched_iso = fetched_at.astimezone(timezone.utc).isoformat()
    published, staged = [], []
    for row in rows:
        player, staging_reason = key_row(row, index)
        start = game_start_utc(row["game_date"], row["game_time"])
        expires = (start + timedelta(hours=2)) if start \
            else fetched_at + timedelta(hours=12)
        record = {
            "player_name": report_name_to_display(row["player_name"]),
            "team": row["team"],
            "game_date": row["game_date"],
            "game_time": row["game_time"],
            "matchup": row["matchup"],
            "status": row["status"],
            "reason": row["reason"],
            "reason_category": (row["reason"].split(" - ")[0]
                                if " - " in row["reason"] else None),
            "collected": True,
            "expires_at": expires.astimezone(timezone.utc).isoformat(),
            "provenance": {
                "origin": "collected",
                "source_name": SOURCE_NAME,
                "source_url": source_url,
                "fetched_at": fetched_iso,
                "collector": COLLECTOR,
                "classification": "Historical",
                "expires_at": expires.astimezone(
                    timezone.utc).isoformat(),
                "supersedes": None,
            },
        }
        if report_ts is not None:
            record["provenance"]["report_timestamp"] = report_ts \
                .astimezone(timezone.utc).isoformat()
        if player is None:
            staged.append({**record, "player_id": None,
                           "staging_reason": staging_reason})
        else:
            record["player_id"] = player["player_id"]
            record["registry_team"] = player["current_team"]
            record["team_agrees_with_registry"] = (
                player["current_team"] == row["team"])
            published.append(record)
    return published, staged


def report_timestamp_from(words: list[dict], url: str):
    """The document's own stamp ('Injury Report: 11/21/26 12:45 AM',
    ET), falling back to the filename."""
    texts = [w["t"] for w in words]
    for i, t in enumerate(texts):
        if t == "Report:" and i + 3 < len(texts):
            stamp = " ".join(texts[i + 1:i + 4])
            try:
                return datetime.strptime(stamp, "%m/%d/%y %I:%M %p") \
                    .replace(tzinfo=EASTERN)
            except ValueError:
                pass
    m = re.search(r"Injury-Report_(\d{4})-(\d{2})-(\d{2})_"
                  r"(\d{2})_(\d{2})(AM|PM)", url)
    if m:
        y, mo, d, h, mi, ap = m.groups()
        hour = int(h) % 12 + (12 if ap == "PM" else 0)
        return datetime(int(y), int(mo), int(d), hour, int(mi),
                        tzinfo=EASTERN)
    return None


# ---------------------------------------------------------------- run

def run(data_root: Path, url_override: str | None = None,
        now: datetime | None = None) -> dict:
    fetched_at = now or datetime.now(timezone.utc)
    if url_override:
        host_path = re.sub(r"^https?://", "", url_override)
        if not host_path.startswith(PDF_HOST_PATH[0] + PDF_HOST_PATH[1]):
            return {"status": "failed", "reason_class": "policy",
                    "detail": "--url must be an ak-static injury "
                              "report PDF"}
        pdf_url = url_override
        page_html = None
    else:
        try:
            page_html = _fetch_with_retry(PAGE_URL).decode(
                "utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 — reported, not hidden
            return {"status": "failed", "reason_class": "network",
                    "detail": f"report page: {type(exc).__name__}"}
        pdf_url = find_latest_report_url(page_html)
        if not pdf_url:
            return {"status": "failed", "reason_class": "parse",
                    "detail": "no injury report PDF linked on page"}
    try:
        pdf_bytes = _fetch_with_retry(pdf_url)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason_class": "network",
                "detail": f"report pdf: {type(exc).__name__}"}

    # Raw landing: append-only, before parsing (spec 4.2).
    filename = pdf_url.rsplit("/", 1)[-1]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    landing_dir = data_root / "raw" / "collected" / "nba_injury_report" \
        / (m.group(1) if m else "undated")
    landing_dir.mkdir(parents=True, exist_ok=True)
    landing_path = landing_dir / filename
    if not landing_path.exists():
        landing_path.write_bytes(pdf_bytes)

    try:
        words = words_from_pdf(landing_path)
        rows, stats = parse_report(words)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "reason_class": "parse",
                "detail": f"{type(exc).__name__}: {exc}"}
    if not rows:
        return {"status": "failed", "reason_class": "parse",
                "detail": "zero rows parsed", "parse_stats": stats}

    report_ts = report_timestamp_from(words, pdf_url)
    registry_path = data_root / "props" / "player_registry.json"
    index = load_registry_index(registry_path) \
        if registry_path.exists() else {}
    published, staged = build_records(rows, index, pdf_url,
                                      fetched_at, report_ts)
    # Expired records leave the published layer (spec 4.5).
    live, expired = [], 0
    for rec in published:
        if datetime.fromisoformat(rec["expires_at"]) > fetched_at:
            live.append(rec)
        else:
            expired += 1

    collected_dir = data_root / "collected"
    collected_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "kind": "collected-injuries",
        "league": "NBA",
        "classification": "Historical",
        "collected": True,
        "generated_at": fetched_at.isoformat(),
        "report": {
            "source_name": SOURCE_NAME,
            "source_url": pdf_url,
            "report_timestamp": report_ts.astimezone(
                timezone.utc).isoformat() if report_ts else None,
            "fetched_at": fetched_at.isoformat(),
            "raw_landing": str(landing_path),
        },
        "records": live,
        "counts": {"rows_parsed": len(rows), "published": len(live),
                   "staged_unkeyed": len(staged),
                   "expired_at_publish": expired,
                   "dropped_no_status": stats["dropped_no_status"]},
    }
    (collected_dir / "injuries_NBA.json").write_text(
        json.dumps(doc, indent=1))
    (collected_dir / "injuries_NBA_staging.json").write_text(json.dumps({
        "kind": "collected-injuries-staging",
        "league": "NBA",
        "generated_at": fetched_at.isoformat(),
        "report_url": pdf_url,
        "staged": staged,
    }, indent=1))
    return {"status": "ok", "report_url": pdf_url,
            "report_timestamp": doc["report"]["report_timestamp"],
            "rows_parsed": len(rows), "published": len(live),
            "staged_unkeyed": len(staged), "expired_at_publish": expired,
            "parse_stats": stats,
            "out": str(collected_dir / "injuries_NBA.json")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/intelligence")
    parser.add_argument("--url", default=None,
                        help="explicit injury-report PDF URL "
                             "(ak-static host only; replay/tests)")
    args = parser.parse_args(argv)
    result = run(Path(args.data_root), url_override=args.url)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
