"""103 Sports BetTracker — Player Props player identification (Phase 1).

Resolves a submitted player reference against the permanent registry
BEFORE any statistical data is touched. Matching sequence, in order:

  1. Provider player ID (when the caller supplies one).
  2. Exact full-name match.
  3. Normalized name match (case, accents, punctuation folded away —
     "Luka Doncic" resolves "Luka Dončić").
  4. Fuzzy name match (Levenshtein distance over normalized names).

Fuzzy results are SUGGESTIONS ONLY. A fuzzy hit never resolves identity:
the result carries suggestion_requires_confirmation=True and player=None,
so no caller can silently substitute a similarly named player. The user
(or an operator) must confirm the suggested identity explicitly, after
which the confirmed player_id is submitted instead of a name.
"""

from __future__ import annotations

import re
import unicodedata

from player_registry import PlayerRegistry

# A normalized candidate within this edit distance of the submission is
# offered as a suggestion. Deliberately tight: better to miss a
# suggestion than to suggest the wrong human.
FUZZY_MAX_DISTANCE = 2


def normalize_name(name: str) -> str:
    """Fold a name to its comparison form: accents stripped, case
    folded, punctuation removed, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(ch for ch in decomposed
                       if not unicodedata.combining(ch))
    lowered = stripped.lower()
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return " ".join(cleaned.split())


def levenshtein(a: str, b: str) -> int:
    """Classic Levenshtein edit distance (stdlib implementation)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1,            # deletion
                           cur[j - 1] + 1,         # insertion
                           prev[j - 1] + (ca != cb)))  # substitution
        prev = cur
    return prev[-1]


def _suggestion(player: dict, matched_via: str, distance: int) -> dict:
    return {
        "player_id": player["player_id"],
        "full_name": player["full_name"],
        "league": player["league"],
        "matched_via": matched_via,
        "distance": distance,
    }


def resolve_player(registry: PlayerRegistry, submitted_name: str | None,
                   provider: str | None = None,
                   provider_id=None) -> dict:
    """Resolve a submitted player reference.

    Returns a dict:
      status                         "provider_id" | "exact" |
                                     "normalized" | "suggestion" |
                                     "not_found"
      player                         the registry record when identity is
                                     RESOLVED (steps 1-3); None otherwise
      suggestions                    ranked fuzzy candidates (step 4)
      suggestion_requires_confirmation  True whenever suggestions exist
    """
    result = {"status": "not_found", "player": None, "suggestions": [],
              "suggestion_requires_confirmation": False}

    # 1. Provider ID — an explicit external key beats any name matching.
    if provider and provider_id is not None:
        hit = registry.get_by_provider_id(provider, provider_id)
        if hit is not None:
            result.update(status="provider_id", player=hit)
            return result

    if not submitted_name or not str(submitted_name).strip():
        return result
    submitted = str(submitted_name).strip()

    # 2. Exact name.
    hit = registry.find_by_exact_name(submitted)
    if hit is not None:
        result.update(status="exact", player=hit)
        return result

    # 3. Normalized name (accents/case/punctuation folded).
    target = normalize_name(submitted)
    for p in registry.all_players():
        if normalize_name(p["full_name"]) == target:
            result.update(status="normalized", player=p)
            return result

    # 4. Fuzzy — suggestions only, identity stays unresolved.
    candidates = []
    for p in registry.all_players():
        d = levenshtein(target, normalize_name(p["full_name"]))
        if d <= FUZZY_MAX_DISTANCE:
            candidates.append(_suggestion(p, "fuzzy", d))
    candidates.sort(key=lambda s: (s["distance"], s["full_name"]))
    if candidates:
        result.update(status="suggestion", suggestions=candidates,
                      suggestion_requires_confirmation=True)
    return result
