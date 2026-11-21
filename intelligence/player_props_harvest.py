"""103 Sports BetTracker — Player Props player fan-out harvest (Phase 2).

Extends the Intelligence Engine harvest (intelligence_harvest) from
team-level datasets to per-team PLAYER statistics for basketball (NBA)
and american-football (NFL), feeding the two dynamic-layer stores:

  * the permanent player registry (player_registry) — harvested players
    are registered / updated by PROVIDER ID ONLY (see
    upsert_harvested_player; names are never join keys), and
  * the per-game log store (player_game_logs) — one entry per player
    per game, keyed by the permanent taxonomy's statistic_keys.

Guarantees (mirroring intelligence_harvest):
  * The budget cap is SHARED with the base harvest: both read/write the
    same harvest_state.json ledger, so base + fan-out requests together
    never exceed the daily per-sport cap.
  * Raw payloads land append-only through the base RawLanding before
    anything is parsed.
  * Statistics are harvested or arithmetically derived from harvested
    components (see _add_derived); a statistic whose components are not
    all present stays absent — numbers are NEVER invented.
  * Endpoint payload shapes are UNVERIFIED (see STAT_FIELD_CANDIDATES):
    they could not be confirmed offline, so extraction is layered and
    tolerant in the same spirit as intelligence_normalize.

Dry-run mode makes NO network calls and NO writes.

CLI:
    APISPORTS_KEY=... python3 intelligence/player_props_harvest.py \
        --sport basketball
    python3 intelligence/player_props_harvest.py --sport basketball \
        --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from intelligence_harvest import (
        DEFAULT_DAILY_CAP,
        SPORT_CONFIG,
        BudgetExceeded,
        BudgetLedger,
        RawLanding,
        build_player_statistics_plan,
        urllib_transport,
        utc_day,
    )
    from intelligence_normalize import (
        _num,
        _player_category_value,
        _response,
        _stat_value,
    )
    from player_game_logs import GameLogStore
    from player_registry import STATUSES, PlayerRegistry
except ImportError:  # package-style import (intelligence.<module>)
    from intelligence.intelligence_harvest import (  # type: ignore
        DEFAULT_DAILY_CAP,
        SPORT_CONFIG,
        BudgetExceeded,
        BudgetLedger,
        RawLanding,
        build_player_statistics_plan,
        urllib_transport,
        utc_day,
    )
    from intelligence.intelligence_normalize import (  # type: ignore
        _num,
        _player_category_value,
        _response,
        _stat_value,
    )
    from intelligence.player_game_logs import GameLogStore  # type: ignore
    from intelligence.player_registry import (  # type: ignore
        STATUSES,
        PlayerRegistry,
    )

PROVIDER = "api-sports"
SPORT_TO_LEAGUE = {"basketball": "NBA", "american-football": "NFL"}
DEFAULT_DATA_ROOT = Path("data/intelligence")

# ---------------------------------------------------------------------------
# Per-statistic field candidates: taxonomy statistic_key ->
# {"keys": [...], "group": optional} in the shape _player_category_value
# accepts (intelligence_normalize).
#
# UNVERIFIED: every candidate list below is modeled on the plausible
# API-Sports payload shapes (flat fields, {"total"/"average"} wrappers,
# NFL statistics.<group> dicts, {name, value} lists). The real
# player-statistics payload shapes could not be confirmed offline —
# extend the lists after the first real payloads land, exactly as
# intelligence_normalize documents for its own candidate tables.
# Derived/combined taxonomy keys (points_rebounds, anytime_touchdown,
# ...) are NOT in this table: they are computed by _add_derived from
# harvested components only.
# ---------------------------------------------------------------------------
STAT_FIELD_CANDIDATES = {
    "basketball": {
        "points": {"keys": ["points", "pts"]},
        # API-Basketball style payloads nest rebounds as a dict
        # ({"off": ..., "def": ..., "tot": ...}); _stat_value does not
        # check the "tot" sub-key, so _extract_stat adds a local pass
        # for it (and for the off/def components below).
        "rebounds": {"keys": ["rebounds", "total_rebounds", "reb"]},
        "offensive_rebounds": {
            "keys": ["offensive_rebounds", "offensiveRebounds", "oreb",
                     "off_rebounds"],
        },
        "defensive_rebounds": {
            "keys": ["defensive_rebounds", "defensiveRebounds", "dreb",
                     "def_rebounds"],
        },
        "assists": {"keys": ["assists", "ast"]},
        "turnovers": {"keys": ["turnovers", "tov", "to"]},
        "steals": {"keys": ["steals", "stl"]},
        "blocks": {"keys": ["blocks", "blk"]},
        "field_goals_made": {
            "keys": ["field_goals_made", "fieldGoalsMade", "fgm",
                     "fg_made"],
        },
        "three_pointers_made": {
            "keys": ["three_pointers_made", "threePointersMade", "tpm",
                     "3pm", "fg3m"],
        },
        "free_throws_made": {
            "keys": ["free_throws_made", "freeThrowsMade", "ftm",
                     "ft_made"],
        },
        "field_goal_attempts": {
            "keys": ["field_goal_attempts", "fieldGoalsAttempted",
                     "fga", "fg_attempted"],
        },
        "three_point_attempts": {
            "keys": ["three_point_attempts", "threePointAttempts",
                     "tpa", "3pa", "fg3a"],
        },
        "free_throw_attempts": {
            "keys": ["free_throw_attempts", "freeThrowAttempts", "fta",
                     "ft_attempted"],
        },
        "field_goal_percentage": {
            "keys": ["field_goal_percentage", "fieldGoalPercentage",
                     "fg_pct", "fg_percentage"],
        },
        "minutes": {"keys": ["minutes", "min", "mins"]},
    },
    "american-football": {
        # Grouped payloads: statistics.passing / .rushing / .receiving /
        # .defense / .kicking dicts. NOTE: _player_category_value's
        # group branch returns the group's "yards" for ANY grouped spec,
        # so _extract_stat resolves grouped keys directly and only falls
        # back to _player_category_value when no group dict is present.
        "passing_yards": {
            "keys": ["passing_yards", "passingYards", "pass_yards",
                     "yards"],
            "group": "passing",
        },
        "completions": {
            "keys": ["completions", "passing_completions", "completed",
                     "comp"],
            "group": "passing",
        },
        "passing_attempts": {
            "keys": ["passing_attempts", "passingAttempts",
                     "pass_attempts", "attempts"],
            "group": "passing",
        },
        "passing_touchdowns": {
            "keys": ["passing_touchdowns", "passingTouchdowns",
                     "pass_touchdowns", "touchdowns"],
            "group": "passing",
        },
        "passing_interceptions": {
            "keys": ["passing_interceptions", "passingInterceptions",
                     "interceptions_thrown", "interceptions"],
            "group": "passing",
        },
        "rushing_yards": {
            "keys": ["rushing_yards", "rushingYards", "rush_yards",
                     "yards"],
            "group": "rushing",
        },
        "rushing_attempts": {
            "keys": ["rushing_attempts", "rushingAttempts",
                     "rush_attempts", "attempts", "carries"],
            "group": "rushing",
        },
        "longest_rush": {
            "keys": ["longest_rush", "longestRush", "longest", "long"],
            "group": "rushing",
        },
        "rushing_touchdowns": {
            "keys": ["rushing_touchdowns", "rushingTouchdowns",
                     "rush_touchdowns", "touchdowns"],
            "group": "rushing",
        },
        "receptions": {
            "keys": ["receptions", "receiving_receptions", "catches",
                     "rec"],
            "group": "receiving",
        },
        "receiving_yards": {
            "keys": ["receiving_yards", "receivingYards", "rec_yards",
                     "yards"],
            "group": "receiving",
        },
        "targets": {"keys": ["targets", "target"], "group": "receiving"},
        "longest_reception": {
            "keys": ["longest_reception", "longestReception", "longest",
                     "long"],
            "group": "receiving",
        },
        "receiving_touchdowns": {
            "keys": ["receiving_touchdowns", "receivingTouchdowns",
                     "rec_touchdowns", "touchdowns"],
            "group": "receiving",
        },
        "tackles": {
            "keys": ["tackles", "total_tackles", "tackles_total"],
            "group": "defense",
        },
        "solo_tackles": {
            "keys": ["solo_tackles", "soloTackles", "solo",
                     "tackles_solo"],
            "group": "defense",
        },
        "sacks": {"keys": ["sacks", "sack"], "group": "defense"},
        "interceptions": {
            "keys": ["interceptions", "defensive_interceptions", "int"],
            "group": "defense",
        },
        "passes_defended": {
            "keys": ["passes_defended", "passesDefended",
                     "pass_defended", "defended"],
            "group": "defense",
        },
        "forced_fumbles": {
            "keys": ["forced_fumbles", "forcedFumbles",
                     "fumbles_forced"],
            "group": "defense",
        },
        "field_goals_made": {
            "keys": ["field_goals_made", "fieldGoalsMade",
                     "field_goals", "fg_made"],
            "group": "kicking",
        },
        "extra_points_made": {
            "keys": ["extra_points_made", "extraPointsMade",
                     "extra_points", "xp_made"],
            "group": "kicking",
        },
        "kicking_points": {
            "keys": ["kicking_points", "kickingPoints", "points"],
            "group": "kicking",
        },
    },
}

# Alternate spellings of the NFL stat-group names (payloads have been
# seen both ways in the API-Sports family — UNVERIFIED, see above).
_GROUP_ALIASES = {
    "defense": ("defense", "defensive"),
}

# NBA nested-dict parents: statistic_key -> (parent field, sub-keys).
# Shooting lines may arrive as one dict per shot family
# ({"total"/"made": ..., "attempted": ..., "percentage": ...}) and
# rebounds as {"off": ..., "def": ..., "tot": ...}; these passes read
# the components out of the parent dict directly.
_NBA_PARENT_FIELDS = {
    "offensive_rebounds": ("rebounds",
                           ("off", "offensive", "off_rebounds")),
    "defensive_rebounds": ("rebounds",
                           ("def", "defensive", "def_rebounds")),
    "field_goals_made": ("field_goals", ("total", "made")),
    "three_pointers_made": ("three_pointers", ("total", "made")),
    "free_throws_made": ("free_throws", ("total", "made")),
    "field_goal_attempts": ("field_goals", ("attempted", "attempts")),
    "three_point_attempts": ("three_pointers",
                             ("attempted", "attempts")),
    "free_throw_attempts": ("free_throws", ("attempted", "attempts")),
    "field_goal_percentage": ("field_goals", ("percentage",)),
}


def _stat_value_ext(raw):
    """_stat_value plus the {"tot": ...} / {"made": ...} wrappers.

    _stat_value (intelligence_normalize) checks the sub-keys "average",
    "total", "value", "yards", "per_game", "avg" — but not "tot"
    (rebounds dicts) or "made" (shooting / kicking dicts). This local
    pass covers those two without changing the shared normalizer.
    """
    value = _stat_value(raw)
    if value is not None:
        return value
    if isinstance(raw, dict):
        for sub in ("tot", "made"):
            value = _num(raw.get(sub))
            if value is not None:
                return value
    return None


def _containers(entry: dict) -> list:
    containers = [entry]
    stats = entry.get("statistics")
    if isinstance(stats, dict):
        containers.append(stats)
    return containers


def _extract_stat(entry: dict, statistic_key: str, spec: dict):
    """Find one statistic value in a player-statistics row (or None).

    Layered lookup (payload shapes are UNVERIFIED — see the table):
      1. flat candidate keys on the row / statistics dict, via
         _stat_value_ext (covers the "tot"/"made" dict wrappers);
      2. the NFL stat-group dict, resolved by direct key lookup —
         deliberately NOT via _player_category_value's group branch,
         which returns the group's "yards" for any grouped spec and
         would misread e.g. completions as passing yards;
      3. NBA nested parent dicts (rebounds off/def, shooting families);
      4. _player_category_value itself (flat keys, {"average"/"total"}
         wrappers, {name, value} lists) — skipped when a group dict is
         present, for the reason in step 2.
    """
    keys = spec.get("keys", [])
    for container in _containers(entry):
        for key in keys:
            if key in container:
                value = _stat_value_ext(container[key])
                if value is not None:
                    return value

    stats = entry.get("statistics")
    group = spec.get("group")
    group_present = False
    if group and isinstance(stats, dict):
        for alias in _GROUP_ALIASES.get(group, (group,)):
            group_stats = stats.get(alias)
            if not isinstance(group_stats, dict):
                continue
            group_present = True
            for key in keys:
                if key in group_stats:
                    value = _stat_value_ext(group_stats[key])
                    if value is not None:
                        return value

    parent = _NBA_PARENT_FIELDS.get(statistic_key)
    if parent is not None:
        field, subs = parent
        for container in _containers(entry):
            raw = container.get(field)
            if isinstance(raw, dict):
                for sub in subs:
                    value = _num(raw.get(sub))
                    if value is not None:
                        return value

    if not group_present:
        return _player_category_value(entry, spec)
    return None


def _add_derived(sport: str, stats: dict) -> None:
    """Add derived statistics in place.

    Every derivation is arithmetic over harvested components and is
    computed ONLY when every component is present — a missing component
    leaves the derived key absent (never invented, never zero-filled).
    """
    if sport == "basketball":
        def _sum(*keys):
            if all(k in stats for k in keys):
                return float(sum(stats[k] for k in keys))
            return None

        for key, parts in (
            ("points_rebounds", ("points", "rebounds")),
            ("points_assists", ("points", "assists")),
            ("rebounds_assists", ("rebounds", "assists")),
            ("points_rebounds_assists",
             ("points", "rebounds", "assists")),
        ):
            value = _sum(*parts)
            if value is not None:
                stats[key] = value
        # Double/triple-double: the three core categories must all be
        # present; steals/blocks join the >= 10 count when present.
        if all(k in stats for k in ("points", "rebounds", "assists")):
            cats = [stats["points"], stats["rebounds"], stats["assists"]]
            for extra in ("steals", "blocks"):
                if extra in stats:
                    cats.append(stats[extra])
            tens = sum(1 for v in cats if v >= 10)
            stats["double_double"] = 1.0 if tens >= 2 else 0.0
            stats["triple_double"] = 1.0 if tens >= 3 else 0.0
    elif sport == "american-football":
        if "passing_yards" in stats and "rushing_yards" in stats:
            stats["passing_rushing_yards"] = float(
                stats["passing_yards"] + stats["rushing_yards"])
        if "rushing_yards" in stats and "receiving_yards" in stats:
            stats["rushing_receiving_yards"] = float(
                stats["rushing_yards"] + stats["receiving_yards"])
        td_keys = ("passing_touchdowns", "rushing_touchdowns",
                   "receiving_touchdowns")
        present_tds = [stats[k] for k in td_keys if k in stats]
        if present_tds:
            stats["anytime_touchdown"] = (
                1.0 if any(v > 0 for v in present_tds) else 0.0)
        # Kicking-points fallback: 3 per field goal + 1 per extra
        # point, only when the provider did not carry the total itself
        # and both components were harvested.
        if ("kicking_points" not in stats
                and "field_goals_made" in stats
                and "extra_points_made" in stats):
            stats["kicking_points"] = float(
                stats["field_goals_made"] * 3
                + stats["extra_points_made"])


def _row_status(entry: dict, player: dict):
    """A registry status string carried by the row, or None.

    Payload shapes do not define a player-status field (UNVERIFIED), so
    this normally returns None; a value is extracted only when a field
    literally carries one of the registry STATUSES.
    """
    for raw in (entry.get("status"), player.get("status"),
                entry.get("player_status")):
        if isinstance(raw, str) and raw.strip().lower() in STATUSES:
            return raw.strip().lower()
    return None


def parse_player_stat_rows(sport: str, payload, season=None) -> list[dict]:
    """Parse one player-statistics payload into row dicts.

    Row: {provider_player_id, player_name, provider_team_id, team_name,
          game_id, date, season, stats, status}. Rows where both the
    player id and the player name are missing are skipped (mirroring
    intelligence_normalize). `season` is the caller's season argument —
    no manifest is available at this layer; an entry's own season field
    is used only when the argument is None.
    """
    table = STAT_FIELD_CANDIDATES.get(sport, {})
    rows = []
    for entry in _response(payload):
        if not isinstance(entry, dict):
            continue
        player = entry.get("player") or {}
        team = entry.get("team") or {}
        if not isinstance(player, dict):
            player = {}
        if not isinstance(team, dict):
            team = {}
        player_id = player.get("id")
        player_name = player.get("name")
        if player_id is None and player_name is None:
            continue
        game = entry.get("game") if isinstance(entry.get("game"),
                                               dict) else entry
        game_id = game.get("id")
        raw_date = game.get("date")
        if isinstance(raw_date, dict):
            raw_date = raw_date.get("date")
        date = raw_date[:10] if isinstance(raw_date, str) else None
        if season is not None:
            row_season = str(season)
        elif entry.get("season") is not None:
            row_season = str(entry["season"])
        else:
            row_season = None
        stats: dict = {}
        for statistic_key, spec in table.items():
            value = _extract_stat(entry, statistic_key, spec)
            if value is not None:
                stats[statistic_key] = float(value)
        _add_derived(sport, stats)
        rows.append({
            "provider_player_id": player_id,
            "player_name": player_name,
            "provider_team_id": team.get("id"),
            "team_name": team.get("name"),
            "game_id": game_id,
            "date": date,
            "season": row_season,
            "stats": stats,
            "status": _row_status(entry, player),
        })
    return rows


def roster_records_from_rows(rows: list[dict]) -> dict:
    """Collapse parsed rows to one roster record per provider player id.

    Returns {provider_player_id: {"provider_id", "full_name",
    "team_name", "status"}}. The latest row wins: rows are ranked by
    date, and a row with no date loses to any dated row (ties go to the
    later row in the list). Rows without a provider player id cannot
    key a record and are skipped — identity joins on provider ID only.
    """
    best: dict = {}
    for row in rows:
        pid = row.get("provider_player_id")
        if pid is None:
            continue
        rank = (row.get("date") is not None, row.get("date") or "")
        if pid in best and rank < best[pid][0]:
            continue
        record = {
            "provider_id": pid,
            "full_name": row.get("player_name"),
            "team_name": row.get("team_name"),
            "status": row.get("status"),
        }
        if row.get("league") is not None:
            record["league"] = row["league"]
        best[pid] = (rank, record)
    return {pid: record for pid, (_rank, record) in best.items()}


def upsert_harvested_player(registry: PlayerRegistry, record: dict,
                            effective_date: str | None,
                            provider: str = PROVIDER) -> dict:
    """Register or update one harvested player in the registry.

    Owner canon — non-negotiable:
      * Identity joins ONLY on provider ID (registry.get_by_provider_id
        under the provider). NEVER on name: two humans who share a name
        must never merge into one record. Each provider is its own ID
        namespace: the ESPN fallback (governing rule 10) registers
        under provider "espn" and can never collide with or merge
        into an "api-sports" record.
      * A missing provider ID skips the record outright.
      * History is append-only via the registry's effective-dated
        record_team_change / record_status_change; an idempotent
        re-harvest (same team, same status) appends nothing.

    This function is THE registration path for every league and
    source (API-Sports fan-out, ESPN fan-out, and the collected-layer
    collectors all register through it), and it persists the complete
    record shape the registry carries: full_name, league, position,
    current team (team_history), provider_ids. Position is a current
    fact, not history — a harvest that reports one fills it in when
    missing and corrects it when it changed; a harvest that reports
    none leaves the stored value untouched.

    Returns {"player": record_or_None, "changes": [...],
             "skipped": reason_or_None}.
    """
    provider_id = record.get("provider_id")
    if provider_id is None or provider_id == "":
        return {"player": None, "changes": [],
                "skipped": "missing_provider_id"}
    effective = effective_date or datetime.now(timezone.utc).strftime(
        "%Y-%m-%d")

    existing = registry.get_by_provider_id(provider, provider_id)
    if existing is None:
        new_record: dict = {
            "full_name": record.get("full_name"),
            "provider_ids": {provider: provider_id},
            "team_history": [],
            "status_history": [{
                "status": "active", "from": effective, "to": None,
                "note": "Registered from harvested game data.",
            }],
        }
        if record.get("league") is not None:
            new_record["league"] = record["league"]
        if record.get("position"):
            new_record["position"] = record["position"]
        if record.get("team_name"):
            new_record["team_history"] = [{
                "team": record["team_name"], "from": effective,
                "to": None,
            }]
        player = registry.add_player(new_record)
        return {"player": player, "changes": ["registered"],
                "skipped": None}

    changes = []
    team = record.get("team_name")
    if team and team != registry.current_team(existing):
        registry.record_team_change(existing, team, effective)
        changes.append("team_change")
    position = record.get("position")
    if position and position != existing.get("position"):
        existing["position"] = position
        changes.append("position_change")
    status = record.get("status")
    if (status and status in STATUSES
            and status != registry.current_status(existing)):
        registry.record_status_change(existing, status, effective)
        changes.append("status_change")
    return {"player": existing, "changes": changes, "skipped": None}


# ---------------------------------------------------------------------------
# Fan-out state (which teams are already covered, per sport + season)
# ---------------------------------------------------------------------------
def _order_teams(team_ids, covered_ids, priority_team_ids=()) -> list:
    """Priority teams first, then the rest in the given order.

    Only ids present in team_ids are returned, deduplicated, with
    already-covered ids (compared as strings) excluded.
    """
    covered = {str(t) for t in covered_ids}
    by_key = {}
    for tid in team_ids:
        by_key.setdefault(str(tid), tid)
    result = []
    used = set()
    # Priority block first, in priority_team_ids order (ids not present
    # in team_ids are not fetchable and are ignored).
    for tid in priority_team_ids:
        key = str(tid)
        if key in by_key and key not in covered and key not in used:
            result.append(by_key[key])
            used.add(key)
    for tid in team_ids:
        key = str(tid)
        if key not in covered and key not in used:
            result.append(tid)
            used.add(key)
    return result


def _sorted_ids(ids) -> list:
    try:
        return sorted(ids)
    except TypeError:
        return sorted(ids, key=str)


class FanoutState:
    """JSON state file: {sport: {"season": str,
    "covered_team_ids": [...], "cycles_completed": int}}.

    Tracks player fan-out coverage so daily runs rotate through the
    league instead of re-fetching the same teams. Persistence mirrors
    BudgetLedger: write to a .tmp sibling, then replace.
    """

    def __init__(self, state_path):
        self.state_path = Path(state_path)
        self._state: dict = {}
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._state = {}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)

    def _entry(self, sport: str, season: str) -> dict:
        entry = self._state.setdefault(
            sport, {"season": None, "covered_team_ids": [],
                    "cycles_completed": 0})
        if entry.get("season") != str(season):
            entry["season"] = str(season)
            entry["covered_team_ids"] = []
            entry["cycles_completed"] = 0
        return entry

    def covered_team_ids(self, sport: str, season=None) -> list:
        """Covered ids for a sport (peek — never mutates).

        A stored season different from `season` means nothing is
        covered for that season yet.
        """
        entry = self._state.get(sport)
        if not entry:
            return []
        if season is not None and entry.get("season") != str(season):
            return []
        return list(entry.get("covered_team_ids", []))

    def next_batch(self, sport: str, season: str, team_ids, n: int,
                   priority_team_ids=()) -> list:
        """The next `n` uncovered team ids; marks them covered + saves.

        Ordering: priority teams first, then the rest in the given
        order. A stored season different from `season` resets coverage.
        When every team is covered, a new cycle starts (coverage
        clears, cycles_completed increments) and ordering restarts
        from the top.
        """
        entry = self._entry(sport, season)
        deduped = list(dict.fromkeys(team_ids))
        ordered = _order_teams(deduped, entry["covered_team_ids"],
                               priority_team_ids)
        if deduped and not ordered:
            entry["covered_team_ids"] = []
            entry["cycles_completed"] = int(
                entry.get("cycles_completed", 0)) + 1
            ordered = _order_teams(deduped, [], priority_team_ids)
        batch = ordered[:max(0, int(n))]
        covered = entry["covered_team_ids"]
        covered_keys = {str(t) for t in covered}
        for tid in batch:
            if str(tid) not in covered_keys:
                covered.append(tid)
                covered_keys.add(str(tid))
        self.save()
        return batch


def plan_fanout_day(sport: str, season: str, team_ids, ledger,
                    state: FanoutState | None = None,
                    priority_team_ids=(), day: str | None = None) -> dict:
    """Plan one day of player fan-out. PURE: spends nothing and does
    not mutate `state` (it only peeks at the covered list).

    Ordering mirrors FanoutState.next_batch, including the new-cycle
    restart when every team is already covered.
    """
    day = day or utc_day()
    season = str(season)
    deduped = list(dict.fromkeys(team_ids))
    covered = state.covered_team_ids(sport, season) if state else []
    covered_keys = {str(t) for t in covered}
    covered_count = sum(1 for t in deduped if str(t) in covered_keys)
    available = ledger.remaining(sport, day)
    all_covered = bool(deduped) and covered_count == len(deduped)
    if all_covered:
        pool = _order_teams(deduped, [], priority_team_ids)
    else:
        pool = _order_teams(deduped, covered, priority_team_ids)
    planned = pool[:available]
    if all_covered:
        deferred = len(deduped) - len(planned)
    else:
        deferred = len(pool) - len(planned)
    if deferred:
        note = (f"{len(planned)} of {len(deduped)} teams fit in "
                f"today's remaining budget ({available} requests); "
                f"{deferred} deferred.")
    elif all_covered:
        note = ("all teams already covered; the plan restarts coverage "
                "from the top (new cycle) and fits in today's budget.")
    else:
        note = "all uncovered teams fit in today's remaining budget."
    return {
        "sport": sport,
        "league": SPORT_TO_LEAGUE[sport],
        "season": season,
        "date": day,
        "daily_cap": ledger.daily_cap,
        "requests_used": ledger.used(sport, day),
        "requests_available": available,
        "teams_total": len(deduped),
        "teams_planned": planned,
        "requests_planned": len(planned),
        "teams_deferred": deferred,
        "fits_in_cap": deferred == 0,
        "note": note,
    }


def run_fanout(sport: str, season: str, team_ids, *, transport, ledger,
               landing, registry, log_store, state=None,
               priority_team_ids=(), api_key=None, day=None,
               effective_date=None) -> dict:
    """Run one player fan-out pass over a batch of teams.

    Batch selection follows FanoutState.next_batch when `state` is
    given (which marks the batch covered), otherwise priority + given
    order truncated to the ledger's remaining budget. Teams cut by the
    budget — at selection or at the per-task spend check — are counted
    in skipped_budget; BudgetExceeded never escapes this function.
    """
    league = SPORT_TO_LEAGUE[sport]
    season = str(season)
    day = day or utc_day()
    effective = effective_date or day
    deduped = list(dict.fromkeys(team_ids))

    if state is not None:
        covered = state.covered_team_ids(sport, season)
        covered_keys = {str(t) for t in covered}
        covered_count = sum(1 for t in deduped
                            if str(t) in covered_keys)
        if deduped and covered_count == len(deduped):
            pool = _order_teams(deduped, [], priority_team_ids)
        else:
            pool = _order_teams(deduped, covered, priority_team_ids)
        batch = state.next_batch(sport, season, deduped,
                                 ledger.remaining(sport, day),
                                 priority_team_ids)
        skipped_budget = len(pool) - len(batch)
    else:
        ordered = _order_teams(deduped, [], priority_team_ids)
        batch = ordered[:ledger.remaining(sport, day)]
        skipped_budget = len(ordered) - len(batch)

    tasks = build_player_statistics_plan(sport, season, batch)
    base_url = SPORT_CONFIG[sport]["base_url"].rstrip("/")
    headers = {"x-apisports-key": api_key or ""}

    fetched = 0
    players_registered = 0
    players_updated = 0
    games_added = 0
    games_corrected = 0
    files: list[str] = []
    fetched_team_ids: list = []

    for task in tasks:
        if not ledger.can_spend(sport, day=day):
            skipped_budget += 1
            continue
        try:
            ledger.spend(sport, day=day)
        except BudgetExceeded:
            skipped_budget += 1
            continue
        payload = transport(base_url + task.path, task.params, headers)
        path = landing.store(sport, "player_statistics", task.path,
                             task.params, payload)
        files.append(str(path))
        fetched += 1
        fetched_team_ids.append(task.params.get("team"))

        rows = parse_player_stat_rows(sport, payload, season=season)
        for record in roster_records_from_rows(rows).values():
            record["league"] = league
            result = upsert_harvested_player(registry, record, effective)
            if result["skipped"]:
                continue
            if "registered" in result["changes"]:
                players_registered += 1
            elif result["changes"]:
                players_updated += 1
        for row in rows:
            if row.get("game_id") is None:
                continue
            player = registry.get_by_provider_id(
                PROVIDER, row.get("provider_player_id"))
            if player is None:
                continue
            outcome = log_store.append_game(player["player_id"], {
                "game_id": row["game_id"],
                "date": row.get("date"),
                "season": row.get("season"),
                "team": row.get("team_name"),
                "stats": row.get("stats") or {},
            })
            if outcome["added"]:
                games_added += 1
            if outcome["corrected"]:
                games_corrected += 1

    if state is not None:
        covered_after = state.covered_team_ids(sport, season)
    else:
        covered_after = fetched_team_ids
    log_store.set_coverage({
        "sport": sport,
        "season": season,
        "teams_covered": _sorted_ids(covered_after),
        "teams_total": len(deduped),
        "as_of": effective,
    })

    return {
        "sport": sport,
        "league": league,
        "season": season,
        "planned": len(tasks),
        "fetched": fetched,
        "skipped_budget": skipped_budget,
        "players_registered": players_registered,
        "players_updated": players_updated,
        "games_added": games_added,
        "games_corrected": games_corrected,
        "files": files,
        "budget_used_today": ledger.used(sport, day),
        "budget_cap": ledger.daily_cap,
    }


def _load_team_ids(teams_path: Path):
    """(team_ids, {team_id: team_name}) from a normalized teams file."""
    data = json.loads(teams_path.read_text())
    records = data.get("teams", []) if isinstance(data, dict) else data
    team_ids = []
    names = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        tid = rec.get("team_id")
        if tid is None:
            continue
        team_ids.append(tid)
        names[tid] = rec.get("team")
    return team_ids, names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True,
                        choices=sorted(SPORT_TO_LEAGUE))
    parser.add_argument("--season", default=None,
                        help="season to harvest (default: last entry of "
                             "SPORT_CONFIG[sport]['seasons'])")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--registry", default=None,
                        help="player registry path (default: "
                             "<data-root>/props/player_registry.json)")
    parser.add_argument("--cap", type=int, default=DEFAULT_DAILY_CAP,
                        help="daily backfill request cap per sport "
                             "(shared ledger with the base harvest)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only; make NO network calls/writes")
    args = parser.parse_args(argv)

    # Policy gate (Nov 17, 2026): same rule as the base harvest —
    # API-Sports is called only from a dedicated-IP environment that
    # opts in with INTELLIGENCE_ALLOW_API=1. Everywhere else the API
    # pass is skipped and the ESPN fallback below fills the stores.
    allow_api = os.environ.get("INTELLIGENCE_ALLOW_API") == "1"
    api_key = os.environ.get("APISPORTS_KEY")
    if not args.dry_run and allow_api and not api_key:
        print("INTELLIGENCE_ALLOW_API=1 but APISPORTS_KEY is not set; "
              "refusing to harvest (use --dry-run to plan without a "
              "key).", file=sys.stderr)
        return 2

    sport = args.sport
    league = SPORT_TO_LEAGUE[sport]
    season = str(args.season or SPORT_CONFIG[sport]["seasons"][-1])
    data_root = Path(args.data_root)
    registry_path = (Path(args.registry) if args.registry
                     else data_root / "props" / "player_registry.json")
    log_path = data_root / "props" / f"game_logs_{league}.json"

    teams_path = (data_root / "normalized" / sport
                  / f"teams_{season}.json")
    if not teams_path.exists():
        print(json.dumps({
            "status": "no_team_data",
            "sport": sport,
            "league": league,
            "season": season,
            "teams_file": str(teams_path),
            "note": "no normalized teams file for this sport/season; "
                    "run the base harvest + normalize first (no teams "
                    "are invented).",
        }, indent=2))
        return 0
    team_ids, team_names = _load_team_ids(teams_path)

    registry = (PlayerRegistry.load(registry_path)
                if registry_path.exists() else PlayerRegistry.empty())
    log_store = (GameLogStore.load(log_path) if log_path.exists()
                 else GameLogStore.empty(league))
    state = FanoutState(data_root / "props" / "fanout_state.json")
    ledger = BudgetLedger(data_root / "harvest_state.json",
                          daily_cap=args.cap)

    # Priority: teams whose name matches a current team of any registry
    # player in this league (registry and normalized records both carry
    # team names, so the match is on name).
    current_names = set()
    for player in registry.all_players():
        if player.get("league") != league:
            continue
        team = registry.current_team(player)
        if team:
            current_names.add(team)
    priority_team_ids = [tid for tid in team_ids
                         if team_names.get(tid) in current_names]

    if args.dry_run:
        plan = plan_fanout_day(sport, season, team_ids, ledger,
                               state=state,
                               priority_team_ids=priority_team_ids)
        print(json.dumps(plan, indent=2))
        return 0

    if allow_api:
        report = run_fanout(
            sport, season, team_ids,
            transport=urllib_transport, ledger=ledger,
            landing=RawLanding(data_root), registry=registry,
            log_store=log_store, state=state,
            priority_team_ids=priority_team_ids, api_key=api_key,
        )
    else:
        report = {
            "games_added": 0,
            "players_registered": 0,
            "api_skipped": "INTELLIGENCE_ALLOW_API not set — shared-IP "
                           "environment; API pass disabled by policy, "
                           "ESPN fallback serves the fan-out",
        }

    # Governing rule 10 (Isaac, Nov 17, 2026): when the primary
    # account delivers NOTHING — suspended, locked, quota-dead, any
    # reason — the next API takes its place automatically. The ESPN
    # fallback pass fills the same registry and log stores under
    # provider "espn" (see intelligence_espn_players).
    if report["games_added"] == 0 and report["players_registered"] == 0:
        try:
            try:
                from intelligence_espn_players import run_espn_fanout
            except ImportError:
                from intelligence.intelligence_espn_players import (  # type: ignore
                    run_espn_fanout,
                )
            report["espn_fallback"] = run_espn_fanout(
                sport, season, list(team_names.values()),
                transport=urllib_transport,
                landing=RawLanding(data_root), registry=registry,
                log_store=log_store,
            )
        except Exception as exc:  # noqa: BLE001 — reported, never hidden
            report["espn_fallback"] = {
                "source": "espn-fallback",
                "error": f"{type(exc).__name__}: {exc}",
            }

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry.save(registry_path)
    log_store.save(log_path)
    state.save()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
