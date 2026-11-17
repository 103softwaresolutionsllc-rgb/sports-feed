"""103 Sports BetTracker — Player Props validation engine (Phase 1).

Validation runs BEFORE any projection, ticket integration, or statistical
work. A selection is resolved against the permanent player registry and
the permanent taxonomy through these checks, IN ORDER:

  1. league supported        5. market supported by the league taxonomy
  2. player identity         6. line numeric and > 0 (half-points OK)
  3. league membership       7. direction valid for the market
  4. player status           8. market availability

A valid market and a valid line do NOT make an invalid player selection
legitimate: any failure rejects the whole selection, with
projection_allowed=False and ticket_integration_allowed=False.

Every failure carries a DISTINCT error code — never one generic error:
  LEAGUE_UNSUPPORTED   league unknown or registered for a later phase
  PLAYER_NOT_FOUND     name/ID resolves to nobody (or only to an
                       unconfirmed fuzzy suggestion in the right league)
  PLAYER_LEAGUE_MISMATCH  player found, but belongs to another league
  PLAYER_RETIRED       retired players keep their history but cannot
                       enter a current-game market
  PLAYER_NOT_ROSTERED  inactive, or no current team association
  PLAYER_UNAVAILABLE   injured or suspended
  MARKET_UNSUPPORTED   market not in the league's permanent taxonomy
  LINE_INVALID         missing, non-numeric, or <= 0
  DIRECTION_INVALID    not one of the market's directions

Fuzzy matches are suggestions only (suggestion_requires_confirmation) —
this engine never auto-substitutes a suggested identity. When a fuzzy
candidate belongs to another league, the mismatch is reported against
that candidate (the canonical Jose Conseco case); when it belongs to the
requested league, identity stays unconfirmed and the selection is
rejected as PLAYER_NOT_FOUND until the user confirms the suggestion and
resubmits with the confirmed player_id.

Check 8 (availability) in v1: lines are user-entered, so there is no
sportsbook offering to verify; the check passes with that stated, and
odds are handled by the projection gate — missing odds are NOT an error
(analysis is allowed, EV is withheld).
"""

from __future__ import annotations

import math

import player_identification
import player_props_taxonomy as taxonomy
from player_registry import PlayerRegistry

LEAGUE_UNSUPPORTED = "LEAGUE_UNSUPPORTED"
PLAYER_NOT_FOUND = "PLAYER_NOT_FOUND"
PLAYER_LEAGUE_MISMATCH = "PLAYER_LEAGUE_MISMATCH"
PLAYER_RETIRED = "PLAYER_RETIRED"
PLAYER_NOT_ROSTERED = "PLAYER_NOT_ROSTERED"
PLAYER_UNAVAILABLE = "PLAYER_UNAVAILABLE"
MARKET_UNSUPPORTED = "MARKET_UNSUPPORTED"
LINE_INVALID = "LINE_INVALID"
DIRECTION_INVALID = "DIRECTION_INVALID"

_PASS, _FAIL, _SKIP = "pass", "fail", "not_reached"


def _check(name: str, status: str, detail: str = "") -> dict:
    return {"check": name, "status": status, "detail": detail}


def _parse_line(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _odds_recognized(odds) -> bool:
    """American odds: a number >= 100 or <= -100."""
    if odds is None or isinstance(odds, bool):
        return False
    try:
        value = float(odds)
    except (TypeError, ValueError):
        return False
    return value >= 100 or value <= -100


def validate_selection(selection: dict, registry: PlayerRegistry) -> dict:
    """Validate one proposed player-prop selection.

    selection keys: league, market, line, direction, and a player
    reference — player_name, or player_id, or provider + provider_id.
    Optional: odds (American format).
    """
    league_raw = selection.get("league")
    league = str(league_raw).strip().upper() if league_raw else None
    checks: list[dict] = []
    errors: list[dict] = []
    warnings: list[str] = []

    def fail(check_name, code, message, detail=""):
        checks.append(_check(check_name, _FAIL, detail or message))
        errors.append({"code": code, "message": message})

    # -- 1. League supported ------------------------------------------------
    league_rec = taxonomy.get_league(league)
    league_ok = taxonomy.league_supported(league)
    if league_ok:
        checks.append(_check("league", _PASS, f"{league} is supported."))
    elif league_rec is not None:
        fail("league", LEAGUE_UNSUPPORTED,
             f"{league} is registered but not yet supported.",
             "League taxonomy is scheduled for a later phase.")
    else:
        fail("league", LEAGUE_UNSUPPORTED,
             f"League {league_raw!r} is not supported.")

    # -- 2. Player identity ---------------------------------------------------
    player = None          # resolved identity
    candidate = None       # unconfirmed fuzzy candidate
    suggestions: list[dict] = []
    suggestion_requires_confirmation = False

    if selection.get("player_id"):
        player = registry.get_by_id(str(selection["player_id"]))
        identity_detail = ("player_id resolved." if player else
                           "player_id not in the registry.")
        resolution_status = "player_id" if player else "not_found"
    else:
        resolution = player_identification.resolve_player(
            registry, selection.get("player_name"),
            provider=selection.get("provider"),
            provider_id=selection.get("provider_id"))
        resolution_status = resolution["status"]
        player = resolution["player"]
        suggestions = resolution["suggestions"]
        suggestion_requires_confirmation = \
            resolution["suggestion_requires_confirmation"]
        identity_detail = f"identity resolution: {resolution_status}."
        if resolution_status == "suggestion" and suggestions:
            top = registry.get_by_id(suggestions[0]["player_id"])
            candidate = top
            identity_detail = (
                f"No exact match; closest suggestion is "
                f"{suggestions[0]['full_name']} — confirmation required, "
                f"never auto-substituted.")

    effective = player or candidate  # record used for membership reporting
    if player is not None:
        checks.append(_check("identity", _PASS, identity_detail))
    elif candidate is not None and candidate["league"] == league:
        # Right league, wrong spelling: identity is NOT confirmed.
        fail("identity", PLAYER_NOT_FOUND,
             "Player not found as submitted. A similar name exists — "
             "confirm the suggestion and resubmit; the app never "
             "substitutes a player automatically.",
             identity_detail)
    elif candidate is not None:
        # Wrong-league candidate: the membership check below reports the
        # mismatch against the candidate (canonical Conseco case).
        checks.append(_check("identity", _FAIL, identity_detail))
    else:
        fail("identity", PLAYER_NOT_FOUND,
             "Player not found in the registry.", identity_detail)

    # -- 3. League membership -------------------------------------------------
    membership_ok = False
    if effective is None:
        checks.append(_check("league_membership", _SKIP,
                             "No player identity to check."))
    elif effective["league"] != league:
        fail("league_membership", PLAYER_LEAGUE_MISMATCH,
             f"Player belongs to {effective['league']}, not {league}.",
             f"Matched player {effective['full_name']} is registered "
             f"under {effective['league']}.")
    else:
        membership_ok = True
        checks.append(_check("league_membership", _PASS,
                             f"{effective['full_name']} belongs to "
                             f"{league}."))

    # -- 4. Player status -------------------------------------------------------
    if player is None or not membership_ok:
        checks.append(_check("player_status", _SKIP,
                             "Requires a confirmed identity in the "
                             "requested league."))
    else:
        status = registry.current_status(player)
        team = registry.current_team(player)
        if status == "retired":
            fail("player_status", PLAYER_RETIRED,
                 "Player is retired and cannot be selected for a "
                 "current-game market.",
                 "Historical statistics remain available.")
        elif status in ("injured", "suspended"):
            fail("player_status", PLAYER_UNAVAILABLE,
                 f"Player is currently {status} and unavailable for "
                 f"this market.")
        elif status == "inactive" or team is None:
            fail("player_status", PLAYER_NOT_ROSTERED,
                 "Player is not currently rostered with a team.")
        else:
            checks.append(_check("player_status", _PASS,
                                 f"Status: {status}; team: {team}."))

    # -- 5. Market supported ----------------------------------------------------
    market = None
    if not league_ok:
        checks.append(_check("market", _SKIP,
                             "League taxonomy unavailable."))
    else:
        market = taxonomy.get_market(league, selection.get("market"))
        if market is None or not market["active"]:
            fail("market", MARKET_UNSUPPORTED,
                 f"Market {selection.get('market')!r} is not supported "
                 f"for {league}.")
        else:
            checks.append(_check("market", _PASS,
                                 f"{market['market_name']} "
                                 f"({market['market_id']})."))

    # -- 6. Line ------------------------------------------------------------------
    line_value = _parse_line(selection.get("line"))
    if line_value is None:
        fail("line", LINE_INVALID,
             "A numeric prop line is required.")
    elif line_value <= 0:
        fail("line", LINE_INVALID,
             "The prop line must be greater than zero.")
    else:
        checks.append(_check("line", _PASS, f"Line: {line_value}."))

    # -- 7. Direction ---------------------------------------------------------------
    direction = selection.get("direction")
    direction_norm = str(direction).strip().lower() if direction else None
    allowed = market["directions"] if market else ["over", "under"]
    if direction_norm in allowed:
        checks.append(_check("direction", _PASS,
                             f"Direction: {direction_norm}."))
    else:
        fail("direction", DIRECTION_INVALID,
             f"Direction must be one of: {', '.join(allowed)}.")

    # -- 8. Availability --------------------------------------------------------------
    if effective is None or (effective is not None and not membership_ok):
        checks.append(_check("availability", _SKIP,
                             "Requires an eligible player."))
    else:
        checks.append(_check(
            "availability", _PASS,
            "User-entered line (v1): no sportsbook offering to verify."))
        odds = selection.get("odds")
        if odds is not None and not _odds_recognized(odds):
            warnings.append(
                "Odds were provided in an unrecognized format; expected "
                "value will be withheld until valid odds are entered.")

    valid = not errors
    career_status = None
    if effective is not None:
        st = registry.current_status(effective)
        career_status = st.upper() if st else None

    return {
        "valid": valid,
        "validation_status": "ACCEPTED" if valid else "REJECTED",
        "ticket_eligible": valid,
        "checks": checks,
        "errors": errors,
        "error_code": errors[0]["code"] if errors else None,
        "warnings": warnings,
        "submitted_name": selection.get("player_name"),
        "matched_player": effective["full_name"] if effective else None,
        "player_id": effective["player_id"] if effective else None,
        "league_requested": league,
        "league_found": effective["league"] if effective else None,
        "career_status": career_status,
        "suggestion_requires_confirmation":
            suggestion_requires_confirmation,
        "suggestions": suggestions,
        "market_id": market["market_id"] if market else None,
        "line": line_value,
        "direction": direction_norm,
        "projection_allowed": valid,
        "ticket_integration_allowed": valid,
    }


def projection_gate(selection: dict, validation: dict) -> dict:
    """The Phase-1 projection gate.

    The projection MODEL is a later phase; this gate exists so nothing
    downstream can ever receive numbers for an unvalidated selection:
      * validation failed  -> projection/probability/EV are null and
        model_status is AWAITING_VALID_PLAYER.
      * validation passed  -> analysis is allowed; numbers stay null
        with model_status AWAITING_PROJECTION_MODEL until the model
        phase lands. Missing odds withhold EV (explicit flag), never
        block analysis, and are not an error.
    """
    base = {
        "market": selection.get("market"),
        "line": selection.get("line"),
        "direction": selection.get("direction"),
        "projection": None,
        "probability": None,
        "expected_value": None,
    }
    if not validation.get("valid"):
        base.update({
            "analysis_allowed": False,
            "model_status": "AWAITING_VALID_PLAYER",
            "ev_withheld": True,
            "ev_withheld_reason": "selection_failed_validation",
        })
        return base

    odds = selection.get("odds")
    if odds is None:
        ev_withheld, reason = True, "missing_odds"
    elif not _odds_recognized(odds):
        ev_withheld, reason = True, "odds_format_unrecognized"
    else:
        ev_withheld, reason = False, None
    base.update({
        "analysis_allowed": True,
        "model_status": "AWAITING_PROJECTION_MODEL",
        "ev_withheld": ev_withheld,
        "ev_withheld_reason": reason,
    })
    return base
