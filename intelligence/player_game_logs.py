"""103 Sports BetTracker — Player Props per-game log store (Phase 2).

The game-log store is the historical layer the projections read: one
entry per player per game, holding the harvested statistic line for
that game keyed by the permanent taxonomy's statistic_keys
(player_props_taxonomy). It is populated by the player fan-out harvest
(player_props_harvest) from harvested game data — never invented, and
a statistic a game does not carry stays absent from that game's entry
(never zero-filled).

Document format (single JSON document, consistent with the registry):

    {"version": 1, "classification": "Historical", "league": "NBA",
     "players": {player_id: {"games": [entry, ...]}}, "coverage": {}}

    entry = {"game_id": ..., "date": "YYYY-MM-DD" or None,
             "season": str or None, "team": str or None,
             "stats": {statistic_key: float}}

Rules:
  * Games are deduplicated by game_id (compared as strings, so the
    same game harvested under an int and a str id is one game).
  * Re-harvesting a game with identical stats is a no-op. Re-harvesting
    it with DIFFERENT stats is a stat correction: the stored entry is
    replaced and the caller is told ({"corrected": True}) — corrections
    are explicit, never silent blends of old and new lines.
  * Each player's games are kept sorted chronologically (undated games
    last, ties broken by game_id) so series() reads oldest -> newest.
  * Coverage metadata (which teams / season the store currently covers)
    is written by the harvest via set_coverage(); this module only
    stores it.
"""

from __future__ import annotations

import json
from pathlib import Path

DOCUMENT_VERSION = 1
CLASSIFICATION = "Historical"


def _sort_key(entry: dict):
    return (entry.get("date") is None, entry.get("date") or "",
            str(entry.get("game_id")))


def _numeric(value):
    """Float value of a stored stat, or None (bools are not stats)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _stats_equal(a: dict, b: dict) -> bool:
    """Stats-dict equality with numeric coercion (24 == 24.0)."""
    if set(a) != set(b):
        return False
    for key in a:
        va, vb = a[key], b[key]
        na, nb = _numeric(va), _numeric(vb)
        if na is not None and nb is not None:
            if na != nb:
                return False
        elif va != vb:
            return False
    return True


class GameLogStore:
    """In-memory view over a game-log JSON document."""

    def __init__(self, document: dict):
        self.document = document
        document.setdefault("version", DOCUMENT_VERSION)
        document.setdefault("classification", CLASSIFICATION)
        document.setdefault("players", {})
        document.setdefault("coverage", {})

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "GameLogStore":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    @classmethod
    def empty(cls, league: str) -> "GameLogStore":
        return cls({"version": DOCUMENT_VERSION,
                    "classification": CLASSIFICATION, "league": league,
                    "players": {}, "coverage": {}})

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.document, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
            fh.write("\n")

    # -- mutation ---------------------------------------------------------
    def append_game(self, player_id: str, entry: dict) -> dict:
        """Add one game entry for a player (see module docstring).

        Returns {"added": bool, "corrected": bool}: a brand-new game_id
        adds; an existing game_id with identical stats AND identical
        team/date/season is a no-op; an existing game_id that differs
        in stats or in its team stamp replaces the stored entry as a
        correction. (The team clause is what lets a re-harvest repair
        mis-stamped teams — see intelligence_espn_players, Nov 20,
        2026: ESPN's roster endpoint scrambled team assignments, and
        entries are now stamped from each game's own gamelog meta.)
        """
        slot = self.document["players"].setdefault(player_id,
                                                   {"games": []})
        games = slot.setdefault("games", [])
        stats = {}
        for key, value in (entry.get("stats") or {}).items():
            num = _numeric(value)
            stats[key] = num if num is not None else value
        new_entry = {
            "game_id": entry.get("game_id"),
            "date": entry.get("date"),
            "season": entry.get("season"),
            "team": entry.get("team"),
            "stats": stats,
        }
        gid = str(new_entry["game_id"])
        for idx, existing in enumerate(games):
            if str(existing.get("game_id")) != gid:
                continue
            if _stats_equal(existing.get("stats") or {}, stats) \
                    and existing.get("team") == new_entry["team"] \
                    and existing.get("date") == new_entry["date"] \
                    and existing.get("season") == new_entry["season"]:
                return {"added": False, "corrected": False}
            games[idx] = new_entry
            games.sort(key=_sort_key)
            return {"added": False, "corrected": True}
        games.append(new_entry)
        games.sort(key=_sort_key)
        return {"added": True, "corrected": False}

    # -- reads ------------------------------------------------------------
    def series(self, player_id: str, statistic_key: str) -> list[dict]:
        """Chronological values of one statistic for one player.

        Only games where the statistic is present and numeric appear —
        a game that does not carry the stat is absent from the series,
        never zero-filled.
        """
        slot = self.document["players"].get(player_id) or {}
        out = []
        for game in sorted(slot.get("games", []), key=_sort_key):
            stats = game.get("stats") or {}
            if statistic_key not in stats:
                continue
            value = _numeric(stats[statistic_key])
            if value is None:
                continue
            out.append({
                "game_id": game.get("game_id"),
                "date": game.get("date"),
                "season": game.get("season"),
                "team": game.get("team"),
                "value": value,
            })
        return out

    def games_count(self, player_id: str) -> int:
        """Distinct games held for a player (0 when unknown)."""
        slot = self.document["players"].get(player_id) or {}
        return len(slot.get("games", []))

    # -- coverage ---------------------------------------------------------
    def set_coverage(self, coverage: dict) -> None:
        self.document["coverage"] = dict(coverage)
