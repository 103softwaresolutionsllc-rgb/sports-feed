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


def _provider_identity(player: dict) -> frozenset:
    """A player's identity as (provider, provider_id) pairs. Two
    records are the same player when their identities intersect —
    identity joins on provider ID only, per registry canon."""
    ids = player.get("provider_ids") or {}
    return frozenset((k, str(v)) for k, v in ids.items())


def _merge_history(memory_entries, disk_entries) -> list:
    """Union two append-only histories by stint, chronological.

    A stint is keyed by (team|status, from): the same stint recorded
    by two writers — one of whom closed it, the other still holding
    it open — is ONE stint, and the closed version carries more
    truth (among closed versions, the earliest closing is the first
    recorded transition and wins). If the union still leaves more
    than one open stint (two writers each opened one), the latest by
    start date stays open and the others are closed at that date —
    current_team/current_status read the open entry, and it must be
    the newest fact, not an accident of list order.
    """
    def stint_key(entry: dict):
        return (entry.get("team") or entry.get("status") or "",
                entry.get("from") or "")

    by_stint: dict = {}
    for entry in list(disk_entries or []) + list(memory_entries or []):
        key = stint_key(entry)
        current = by_stint.get(key)
        if current is None:
            by_stint[key] = entry
        elif current.get("to") is None and entry.get("to") is not None:
            by_stint[key] = entry
        elif current.get("to") is not None \
                and entry.get("to") is not None \
                and entry["to"] < current["to"]:
            by_stint[key] = entry
    merged = sorted(by_stint.values(), key=lambda e: e.get("from") or "")
    opens = [e for e in merged if e.get("to") is None]
    if len(opens) > 1:
        for entry in opens[:-1]:
            entry["to"] = opens[-1].get("from")
    return merged


def _merge_player(memory_player: dict, disk_player: dict) -> dict:
    """Field-merge two records of the SAME player. The in-memory
    record object survives (callers hold references to it); facts it
    lacks are folded in from the disk record."""
    merged_ids = dict(disk_player.get("provider_ids") or {})
    merged_ids.update(memory_player.get("provider_ids") or {})
    memory_player["provider_ids"] = merged_ids
    for field in ("full_name", "league", "position"):
        if not memory_player.get(field) and disk_player.get(field):
            memory_player[field] = disk_player[field]
    memory_player["team_history"] = _merge_history(
        memory_player.get("team_history"),
        disk_player.get("team_history"))
    memory_player["status_history"] = _merge_history(
        memory_player.get("status_history"),
        disk_player.get("status_history"))
    return memory_player


def _merge_registry_documents(memory: dict, disk: dict):
    """Merge the on-disk registry document into the in-memory one.

    Returns (merged_document, id_remaps). Rules:
      * A player only on disk is preserved verbatim.
      * The same player on both sides (same player_id, intersecting
        provider identity) is field-merged via _merge_player.
      * A player_id claimed on disk by a DIFFERENT player (disjoint
        provider identity — two writers allocated the same next id
        from the same stale base) stays with the disk record: first
        writer wins, ids are permanent. The in-memory record is
        remapped in place to a fresh id and the remap is reported.
    """
    merged = dict(memory)
    disk_players = list(disk.get("players") or [])
    disk_by_id = {p["player_id"]: p for p in disk_players}
    used_ids = set(disk_by_id) | {
        p["player_id"] for p in memory.get("players") or []}

    def fresh_id() -> str:
        nums = []
        for pid in used_ids:
            m = re.search(r"(\d+)$", pid)
            if m:
                nums.append(int(m.group(1)))
        nid = f"plyr-{(max(nums) + 1) if nums else 1:04d}"
        used_ids.add(nid)
        return nid

    out: list[dict] = []
    remaps: list[dict] = []
    consumed: set[str] = set()
    for player in memory.get("players") or []:
        pid = player["player_id"]
        rival = disk_by_id.get(pid)
        if rival is None or pid in consumed:
            out.append(player)
            continue
        consumed.add(pid)
        if _provider_identity(rival) & _provider_identity(player):
            out.append(_merge_player(player, rival))
        else:
            out.append(rival)  # first writer keeps the contested id
            new_id = fresh_id()
            remaps.append({
                "from": pid, "to": new_id,
                "provider_ids": dict(player.get("provider_ids") or {}),
            })
            player["player_id"] = new_id
            out.append(player)
    for player in disk_players:
        if player["player_id"] not in consumed:
            out.append(player)
    merged["players"] = out
    return merged, remaps


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

    def save(self, path: str | Path) -> list:
        """Persist the registry — MERGE-AWARE (Nov 21, 2026).

        This document has many writers (fan-out driver, collected-
        layer collectors, the nightly harvest cron), each holding an
        in-memory copy loaded at its start. A blind whole-document
        write from a stale copy silently destroys registrations a
        peer saved meanwhile — it happened twice on Nov 20-21, 2026
        (802 NHL registrations overwritten by a peer fan-out's save,
        then 1,625 MLB registrations the same way, with player_id
        collisions in between). Saving therefore reloads the on-disk
        document and merges it (see _merge_registry_documents)
        before writing, adopts the merged document as this
        registry's state — so later id allocation in this process
        builds on the true maximum — and returns the list of id
        remaps the merge applied (usually empty). An on-disk
        document that cannot be parsed is never overwritten blind:
        save raises instead.
        """
        path = Path(path)
        remaps: list = []
        if path.exists():
            try:
                with open(path, encoding="utf-8") as fh:
                    disk = json.load(fh)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"refusing to overwrite unparseable registry "
                    f"at {path}: {exc}") from exc
            merged, remaps = _merge_registry_documents(
                self.document, disk)
            self.document = merged
            self.players = merged.setdefault("players", [])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.document, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
            fh.write("\n")
        return remaps

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
