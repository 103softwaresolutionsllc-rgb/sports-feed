"""103 Sports BetTracker — Player Props player registry (Phase 1).

The registry is the DYNAMIC layer's identity core: one permanent record
per player, referenced by the permanent taxonomy. Design rules enforced
here:

  * Every player has a permanent internal player_id that never changes
    and is never reused. External provider IDs are stored alongside
    (populated by the harvest in a later phase) — they are lookup keys,
    never identity.
  * Team and status history are EFFECTIVE-DATED: a trade, signing,
    injury, suspension, or retirement appends a new entry and closes
    the previous one. History is never rewritten, so a player's team
    association at any past date stays queryable forever.
  * A player is NEVER deleted. Retirement closes a career; it does not
    erase the record or its history. (There is deliberately no delete
    operation in this module.)

Storage is a single JSON document, consistent with the Intelligence
Engine's file/aggregate patterns (JSON in, JSON out, missing data stays
explicit). The bundled player_registry_seed.json is a labeled test
fixture, not a live roster feed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REGISTRY_VERSION = 1
CLASSIFICATION = "Historical"
STATUSES = {"active", "injured", "suspended", "inactive", "retired"}

_SEED_PATH = Path(__file__).with_name("player_registry_seed.json")


def _entry_on(entries: list[dict], on: str | None) -> dict | None:
    """The history entry effective on a date (ISO 'YYYY-MM-DD').

    With on=None, returns the open-ended entry (to is None); falls back
    to the latest entry by start date. With a date, returns the entry
    whose [from, to) window contains it.
    """
    if not entries:
        return None
    if on is None:
        for e in entries:
            if e.get("to") is None:
                return e
        return max(entries, key=lambda e: e["from"])
    covering = [e for e in entries
                if e["from"] <= on and (e.get("to") is None or on < e["to"])]
    if covering:
        return max(covering, key=lambda e: e["from"])
    return None


class PlayerRegistry:
    """In-memory view over a registry JSON document."""

    def __init__(self, document: dict):
        self.document = document
        self.players: list[dict] = document.setdefault("players", [])

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "PlayerRegistry":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    @classmethod
    def load_seed(cls) -> "PlayerRegistry":
        """The bundled seed/test-fixture registry."""
        return cls.load(_SEED_PATH)

    @classmethod
    def empty(cls) -> "PlayerRegistry":
        return cls({"registry_version": REGISTRY_VERSION,
                    "classification": CLASSIFICATION, "players": []})

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.document, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
            fh.write("\n")

    # -- lookups ----------------------------------------------------------
    def all_players(self) -> list[dict]:
        return list(self.players)

    def get_by_id(self, player_id: str) -> dict | None:
        for p in self.players:
            if p["player_id"] == player_id:
                return p
        return None

    def get_by_provider_id(self, provider: str, provider_id) -> dict | None:
        for p in self.players:
            ids = p.get("provider_ids") or {}
            if provider in ids and str(ids[provider]) == str(provider_id):
                return p
        return None

    def find_by_exact_name(self, full_name: str) -> dict | None:
        for p in self.players:
            if p["full_name"] == full_name:
                return p
        return None

    # -- effective-dated facts --------------------------------------------
    def current_team(self, player: dict, on: str | None = None) -> str | None:
        entry = _entry_on(player.get("team_history", []), on)
        return entry["team"] if entry else None

    def team_on(self, player: dict, on: str) -> str | None:
        return self.current_team(player, on=on)

    def current_status(self, player: dict,
                       on: str | None = None) -> str | None:
        entry = _entry_on(player.get("status_history", []), on)
        return entry["status"] if entry else None

    def status_on(self, player: dict, on: str) -> str | None:
        return self.current_status(player, on=on)

    # -- mutation (append-only history; never delete) ----------------------
    def _next_player_id(self) -> str:
        nums = []
        for p in self.players:
            m = re.search(r"(\d+)$", p["player_id"])
            if m:
                nums.append(int(m.group(1)))
        return f"plyr-{(max(nums) + 1) if nums else 1:04d}"

    def add_player(self, record: dict) -> dict:
        """Register a new player. Assigns the next permanent internal ID
        when the record does not carry one. Rejects duplicate IDs."""
        record = dict(record)
        pid = record.get("player_id") or self._next_player_id()
        if self.get_by_id(pid) is not None:
            raise ValueError(f"player_id already registered: {pid}")
        record["player_id"] = pid
        record.setdefault("provider_ids", {})
        record.setdefault("team_history", [])
        record.setdefault("status_history", [])
        self.players.append(record)
        return record

    @staticmethod
    def _close_open(entries: list[dict], effective: str) -> None:
        for e in entries:
            if e.get("to") is None:
                e["to"] = effective

    def record_team_change(self, player: dict, team: str,
                           effective: str) -> dict:
        """Close the current team stint at `effective` and open a new
        one. Prior stints are preserved verbatim."""
        history = player.setdefault("team_history", [])
        self._close_open(history, effective)
        entry = {"team": team, "from": effective, "to": None}
        history.append(entry)
        return entry

    def record_status_change(self, player: dict, status: str,
                             effective: str, note: str = "") -> dict:
        """Close the current status at `effective` and open a new one
        (injury, suspension, retirement, return to active, ...)."""
        if status not in STATUSES:
            raise ValueError(f"unknown player status: {status}")
        history = player.setdefault("status_history", [])
        self._close_open(history, effective)
        entry = {"status": status, "from": effective, "to": None,
                 "note": note}
        history.append(entry)
        return entry
