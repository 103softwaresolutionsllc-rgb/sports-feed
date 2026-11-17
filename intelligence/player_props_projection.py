"""103 Sports BetTracker — Player Props projection engine (Phase 2).

The house model is a closed-form Gaussian over the player's harvested
game-log distribution for the market's statistic: the projection is the
mean of the windowed distribution, and the over/under probability is
the Gaussian tail past the (continuity-corrected) line. No learning, no
fitted parameters beyond the distribution itself — the same
deterministic stance as the rest of BetTracker's model.

Integration contract (Phase 1): validation runs FIRST and this engine
never overrides it. project_selection() calls
player_props_validation.validate_selection() and
player_props_validation.projection_gate(); a failed validation returns
the gate dict UNTOUCHED — failed validation yields AWAITING_VALID_PLAYER
with null projection/probability/EV, and nothing downstream may paper
over that with a number.

Windows: the default window is L10 (the last 10 games). Recent form
matters more than a full season for a prop line, but a 5-game window is
noisy and a 20-game window goes stale across role changes, injuries, and
trades — L10 balances recent form without starving the sample. "season"
means the latest season present in the player's series. Fewer than
MIN_GAMES games in the window is INSUFFICIENT_DATA, stated explicitly —
never a fabricated projection from one or two games.

Calibration: apply_calibration() mirrors the BetTracker
calibrationEngine concept (per-sport calibration with global fallback)
in pure Python. Honest status: NO fitted calibration parameters exist
yet — fitting needs settled outcomes, which is a later phase. The
production default is therefore the identity (calibrations=None or no
matching entry), explicitly labeled source "none_identity" in every
result, never applied silently.

Classification (Intelligence Engine spec): figures computed from
historical data are "Estimated"; the underlying game-log distribution
is "Historical". The two are labeled separately on every result, never
blended.

Game-log store contract (duck-typed; the store itself is a separate
module): log_store.series(player_id, statistic_key) returns a list of
{"game_id", "date", "season", "team", "value"} dicts, chronological
oldest -> newest, only games where the statistic is present.
"""

from __future__ import annotations

import math
import statistics

import player_props_taxonomy as taxonomy
import player_props_validation
from player_identification import normalize_name

# Window name -> number of most-recent games. "season" (None) means all
# entries from the latest season present in the series.
WINDOWS = {"L5": 5, "L10": 10, "L20": 20, "season": None}
DEFAULT_WINDOW = "L10"
MIN_GAMES = 3


def normal_cdf(z: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def american_to_decimal(odds) -> float:
    """American odds -> decimal odds (total return per unit staked)."""
    odds = float(odds)
    if odds > 0:
        return 1.0 + odds / 100.0
    if odds < 0:
        return 1.0 + 100.0 / abs(odds)
    raise ValueError("American odds of 0 are not valid.")


def american_to_implied(odds) -> float:
    """American odds -> implied probability (before any vig removal)."""
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    raise ValueError("American odds of 0 are not valid.")


def _clamp_probability(p: float) -> float:
    return max(0.001, min(0.999, p))


def apply_calibration(p: float, league: str | None,
                      calibrations: dict | None) -> tuple[float, dict]:
    """Apply per-league calibration to a raw probability, with global
    fallback, mirroring the calibrationEngine concept.

    An entry is {"shift": float = 0.0, "shrink": float = 1.0} and maps
        p' = clamp(0.5 + (p - 0.5) * shrink + shift, 0.001, 0.999).

    Lookup order: calibrations[league] (source "league"), then
    calibrations["global"] (source "global_fallback"), then the
    identity (source "none_identity", applied False).

    No fitted calibration parameters exist yet — fitting needs settled
    outcomes (a later phase) — so callers pass calibrations=None in
    production and every result carries the identity label explicitly.
    """
    calibrations = calibrations or {}
    entry = None
    source = "none_identity"
    if league is not None and calibrations.get(league) is not None:
        entry = calibrations[league]
        source = "league"
    elif calibrations.get("global") is not None:
        entry = calibrations["global"]
        source = "global_fallback"

    if entry is None:
        info = {"applied": False, "source": "none_identity",
                "shift": 0.0, "shrink": 1.0}
        return p, info

    shift = float(entry.get("shift", 0.0))
    shrink = float(entry.get("shrink", 1.0))
    calibrated = _clamp_probability(0.5 + (p - 0.5) * shrink + shift)
    info = {"applied": True, "source": source,
            "shift": shift, "shrink": shrink}
    return calibrated, info


def _window_slice(full: list[dict], window: str) -> list[dict]:
    """The entries in `window`, preserving chronological order.

    Raises ValueError for an unknown window string.
    """
    if window not in WINDOWS:
        raise ValueError(
            f"Unknown window {window!r}; expected one of: "
            f"{', '.join(WINDOWS)}.")
    size = WINDOWS[window]
    if size is not None:
        return list(full[-size:])
    # "season": only the latest season present in the series.
    if not full:
        return []
    latest = max(str(e.get("season")) for e in full)
    return [e for e in full if str(e.get("season")) == latest]


def project_selection(selection: dict, registry, log_store,
                      window: str = DEFAULT_WINDOW,
                      calibrations: dict | None = None) -> dict:
    """Project one player-prop selection with the house Gaussian model.

    selection / registry follow the Phase 1 validation contract;
    log_store follows the game-log store contract in the module
    docstring. Returns the projection-gate dict, extended:

      * validation failed -> the gate dict UNTOUCHED
        (model_status AWAITING_VALID_PLAYER, all numbers null).
      * validation passed, too little data -> model_status
        INSUFFICIENT_DATA with an "insufficient_data" block stating
        exactly what is missing; numbers stay null.
      * validation passed, enough data -> model_status PROJECTED with
        projection / probability / expected_value (EV only when the
        gate did not withhold it) and a "projection_detail" block.
    """
    validation = player_props_validation.validate_selection(
        selection, registry)
    gate = player_props_validation.projection_gate(selection, validation)

    # 1. Failed validation: the gate is final. Nothing here may
    # override AWAITING_VALID_PLAYER or its nulls.
    if not validation["valid"]:
        return gate

    # 2. Market + identity from the validated selection.
    league = validation["league_requested"]
    market = taxonomy.get_market(league, selection.get("market"))
    statistic_key = market["statistic_key"]
    player_id = validation["player_id"]

    # 3. Window the player's full series for this statistic.
    full = log_store.series(player_id, statistic_key)
    windowed = _window_slice(full, window)
    values = [entry["value"] for entry in windowed]

    # 4. Too little data is a stated outcome, not a small-sample guess.
    # Analysis stays allowed (validation passed); it is the DATA that
    # is missing, and that is never papered over with a fabricated
    # number.
    if len(values) < MIN_GAMES:
        result = dict(gate)
        result.update({
            "model_status": "INSUFFICIENT_DATA",
            "projection": None,
            "probability": None,
            "expected_value": None,
            "insufficient_data": {
                "reason": ("no_game_logs" if not full
                           else "fewer_than_min_games"),
                "games_available": len(full),
                "games_in_window": len(values),
                "games_required": MIN_GAMES,
                "window": window,
                "statistic_key": statistic_key,
            },
            "classification": "Estimated",
            "inputs_classification": "Historical",
        })
        return result

    # 5. Gaussian over the windowed distribution. Integer statistics
    # get a continuity correction: the threshold is the middle of the
    # line's whole-number bucket (floor(line) + 0.5), which equals the
    # line itself for half-point lines and sits half a point above
    # whole-number lines, where a push is possible.
    mu = statistics.fmean(values)
    sigma = statistics.stdev(values) if len(values) > 1 else 0.0
    line = validation["line"]
    if market["statistic_type"] == "integer":
        threshold = math.floor(line) + 0.5
    else:
        threshold = line
    if sigma > 0:
        p_over = 1.0 - normal_cdf((threshold - mu) / sigma)
    else:
        # A constant series has no spread: the outcome is a step.
        p_over = 1.0 if mu > threshold else 0.0 if mu < threshold else 0.5

    # 6. Calibrate the over probability ONCE; under is its complement.
    p_over_cal, cal_info = apply_calibration(p_over, league, calibrations)
    probability_over = round(p_over_cal, 4)
    probability_under = round(1.0 - p_over_cal, 4)
    direction = validation["direction"]
    probability = (probability_over if direction == "over"
                   else probability_under)

    result = dict(gate)
    result.update({
        "model_status": "PROJECTED",
        "projection": round(mu, 3),
        "probability": probability,
        "projection_detail": {
            "player_id": player_id,
            "player_name": validation["matched_player"],
            "league": league,
            "market_id": market["market_id"],
            "statistic_key": statistic_key,
            "window": window,
            "games_used": len(values),
            "games_available": len(full),
            "mean": round(mu, 3),
            "stdev": round(sigma, 3),
            "threshold": threshold,
            "probability_over": probability_over,
            "probability_under": probability_under,
            "calibration": cal_info,
            "classification": "Estimated",
            "inputs_classification": "Historical",
        },
        "classification": "Estimated",
        "inputs_classification": "Historical",
    })

    # 7. EV only when the gate allows it. Missing or unrecognized odds
    # withhold EV (the gate's ev_withheld fields carry through) but
    # never block the projection itself.
    if not gate["ev_withheld"]:
        odds = selection["odds"]
        implied = american_to_implied(odds)
        decimal = american_to_decimal(odds)
        ev_per_unit = probability * decimal - 1.0
        result["expected_value"] = {
            "ev_per_unit": round(ev_per_unit, 4),
            "ev_percent": round(ev_per_unit * 100.0, 2),
            "implied_probability": round(implied, 4),
            "decimal_odds": round(decimal, 4),
            "edge": round(probability - implied, 4),
        }

    return result


def detect_correlations(selections: list[dict]) -> list[dict]:
    """Flag correlated groups within a set of selections.

    Pure function; a later phase warns on these flags before a ticket
    is accepted. Two correlation shapes are detected:

      * same player — selections sharing a player identity key:
        ("id", player_id) when a player_id is present, else
        ("name", normalize_name(player_name)) when a name is present.
        A selection with neither is skipped for player grouping.
      * same game — selections sharing a game_id. A game is only ever
        taken from the selection itself, never guessed.

    A group sharing BOTH keys additionally earns a
    same_player_same_game flag. Indexes are positions in the input
    list. Detail strings are plain language (user-facing copy in this
    product is always plain language; codes live in fields, not prose).
    Returns [] when nothing correlates.
    """
    player_groups: dict[tuple, list[int]] = {}
    player_labels: dict[tuple, str] = {}
    game_groups: dict = {}
    combined_groups: dict[tuple, list[int]] = {}
    combined_labels: dict[tuple, tuple] = {}

    for idx, sel in enumerate(selections):
        player_key = None
        if sel.get("player_id"):
            player_key = ("id", sel["player_id"])
            player_labels.setdefault(player_key, str(sel["player_id"]))
        elif sel.get("player_name"):
            player_key = ("name", normalize_name(sel["player_name"]))
            player_labels.setdefault(player_key,
                                     str(sel["player_name"]))
        if player_key is not None:
            player_groups.setdefault(player_key, []).append(idx)

        game_id = sel.get("game_id")
        if game_id:
            game_groups.setdefault(game_id, []).append(idx)

        if player_key is not None and game_id:
            combined_key = (player_key, game_id)
            combined_groups.setdefault(combined_key, []).append(idx)
            combined_labels.setdefault(
                combined_key, (player_labels[player_key], game_id))

    flags: list[dict] = []
    for key, indexes in player_groups.items():
        if len(indexes) >= 2:
            player = player_labels[key]
            flags.append({
                "type": "same_player",
                "player": player,
                "selection_indexes": list(indexes),
                "detail": (
                    f"Selections {indexes} are all for the same player "
                    f"({player}). Multiple props on one player tend to "
                    f"move together, so they are not independent."),
            })
    for game_id, indexes in game_groups.items():
        if len(indexes) >= 2:
            flags.append({
                "type": "same_game",
                "game_id": game_id,
                "selection_indexes": list(indexes),
                "detail": (
                    f"Selections {indexes} are all in the same game "
                    f"({game_id}). Props from one game tend to move "
                    f"together, so they are not independent."),
            })
    for key, indexes in combined_groups.items():
        if len(indexes) >= 2:
            player, game_id = combined_labels[key]
            flags.append({
                "type": "same_player_same_game",
                "player": player,
                "game_id": game_id,
                "selection_indexes": list(indexes),
                "detail": (
                    f"Selections {indexes} are for the same player "
                    f"({player}) in the same game ({game_id}). These "
                    f"are the most strongly correlated picks in the "
                    f"set."),
            })
    return flags
