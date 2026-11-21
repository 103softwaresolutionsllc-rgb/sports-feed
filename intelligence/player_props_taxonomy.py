"""103 Sports BetTracker — Player Props permanent taxonomy (Phase 1).

The taxonomy is the PERMANENT layer of the Player Props module: statistical
categories and markets that stay stable regardless of trades, free agency,
roster changes, or retirement. Everything else (player registry, lines,
projections) references these definitions; nothing redefines a statistic
locally.

Phase 1 scope: NBA + NFL markets are fully defined. MLB and MMA are
registered as phase-later league stubs with NO markets — their taxonomies
exist in the design input but are deliberately not defined here until
their phase opens. The structure is data-driven: registering a new league
or market later is a data change, not a code change.

Market record fields (mirrors the design spec):
  market_id                      e.g. "nba_player_blocks"
  league                         "NBA" | "NFL"
  category                       grouping from the design taxonomy
  market_name                    display name, e.g. "Blocks"
  statistic_key                  canonical stat key, e.g. "blocks"
  statistic_type                 "integer" | "number" | "boolean"
  period                         "full_game" (quarter/half variants are
                                 SEPARATE market definitions, registered
                                 the same way when their phase opens)
  directions                     ["over", "under"]
  supports_alternate_lines       bool
  supports_historical_analysis   bool
  supports_player_projection     bool
  active                         bool

Rules:
  * Statistics come from official or verified game logs (ingestion is a
    later phase; this module only defines what a statistic IS).
  * Counting statistics are whole numbers ("integer"); lines against them
    may be half-points — line handling lives in the validation engine.
"""

from __future__ import annotations

# League registry. "phase1" leagues carry full market definitions;
# "phase_later" leagues are stubs on purpose (see module docstring).
LEAGUES = {
    "NBA": {"league": "NBA", "sport": "basketball", "status": "phase1"},
    "NFL": {"league": "NFL", "sport": "american-football", "status": "phase1"},
    "MLB": {"league": "MLB", "sport": "baseball", "status": "phase_later"},
    "MMA": {"league": "MMA", "sport": "mma", "status": "phase_later"},
}


def _market(league: str, category: str, name: str, key: str,
            statistic_type: str = "integer") -> dict:
    return {
        "market_id": f"{league.lower()}_player_{key}",
        "league": league,
        "category": category,
        "market_name": name,
        "statistic_key": key,
        "statistic_type": statistic_type,
        "period": "full_game",
        "directions": ["over", "under"],
        "supports_alternate_lines": True,
        "supports_historical_analysis": True,
        "supports_player_projection": True,
        "active": True,
    }


# ---------------------------------------------------------------------------
# NBA — permanent statistical categories (design taxonomy)
# ---------------------------------------------------------------------------
_NBA = [
    # Scoring
    _market("NBA", "Scoring", "Points", "points"),
    _market("NBA", "Scoring", "Field Goals Made", "field_goals_made"),
    _market("NBA", "Scoring", "Three-Pointers Made", "three_pointers_made"),
    _market("NBA", "Scoring", "Free Throws Made", "free_throws_made"),
    # Rebounding
    _market("NBA", "Rebounding", "Rebounds", "rebounds"),
    _market("NBA", "Rebounding", "Offensive Rebounds", "offensive_rebounds"),
    _market("NBA", "Rebounding", "Defensive Rebounds", "defensive_rebounds"),
    # Playmaking
    _market("NBA", "Playmaking", "Assists", "assists"),
    _market("NBA", "Playmaking", "Turnovers", "turnovers"),
    # Defense
    _market("NBA", "Defense", "Steals", "steals"),
    _market("NBA", "Defense", "Blocks", "blocks"),
    # Combined
    _market("NBA", "Combined", "Points + Rebounds", "points_rebounds"),
    _market("NBA", "Combined", "Points + Assists", "points_assists"),
    _market("NBA", "Combined", "Rebounds + Assists", "rebounds_assists"),
    _market("NBA", "Combined", "Points + Rebounds + Assists",
            "points_rebounds_assists"),
    # Shooting volume
    _market("NBA", "Shooting", "Field Goal Attempts", "field_goal_attempts"),
    _market("NBA", "Shooting", "Three-Point Attempts",
            "three_point_attempts"),
    _market("NBA", "Shooting", "Free Throw Attempts", "free_throw_attempts"),
    _market("NBA", "Shooting", "Field Goal Percentage",
            "field_goal_percentage", "number"),
    # Playing time
    _market("NBA", "Playing Time", "Minutes Played", "minutes", "number"),
    # Special
    _market("NBA", "Special", "Double-Double", "double_double", "boolean"),
    _market("NBA", "Special", "Triple-Double", "triple_double", "boolean"),
]

# ESPN game logs — the only live NBA source while API-Sports is
# suspended — carry total rebounds only, never the offensive/defensive
# split. These two markets therefore stay INACTIVE (unsupported,
# visibly) rather than present-but-empty: no approximation, no silent
# gap. Reactivation needs a source that actually records the split
# (API-Sports reinstatement is Isaac's call).
for _m in _NBA:
    if _m["statistic_key"] in ("offensive_rebounds", "defensive_rebounds"):
        _m["active"] = False

# ---------------------------------------------------------------------------
# NFL — permanent statistical categories (design taxonomy)
# ---------------------------------------------------------------------------
_NFL = [
    # Passing
    _market("NFL", "Passing", "Passing Yards", "passing_yards"),
    _market("NFL", "Passing", "Completions", "completions"),
    _market("NFL", "Passing", "Passing Attempts", "passing_attempts"),
    _market("NFL", "Passing", "Passing Touchdowns", "passing_touchdowns"),
    _market("NFL", "Passing", "Interceptions Thrown",
            "passing_interceptions"),
    # Rushing
    _market("NFL", "Rushing", "Rushing Yards", "rushing_yards"),
    _market("NFL", "Rushing", "Rushing Attempts", "rushing_attempts"),
    _market("NFL", "Rushing", "Longest Rush", "longest_rush"),
    _market("NFL", "Rushing", "Rushing Touchdowns", "rushing_touchdowns"),
    # Receiving
    _market("NFL", "Receiving", "Receptions", "receptions"),
    _market("NFL", "Receiving", "Receiving Yards", "receiving_yards"),
    _market("NFL", "Receiving", "Targets", "targets"),
    _market("NFL", "Receiving", "Longest Reception", "longest_reception"),
    _market("NFL", "Receiving", "Receiving Touchdowns",
            "receiving_touchdowns"),
    # Combined
    _market("NFL", "Combined", "Passing + Rushing Yards",
            "passing_rushing_yards"),
    _market("NFL", "Combined", "Rushing + Receiving Yards",
            "rushing_receiving_yards"),
    # Scoring (line is 0.5 for anytime/first; 1.5 for multiple)
    _market("NFL", "Scoring", "Anytime Touchdown", "anytime_touchdown",
            "boolean"),
    _market("NFL", "Scoring", "First Touchdown", "first_touchdown",
            "boolean"),
    _market("NFL", "Scoring", "Multiple Touchdowns", "multiple_touchdowns",
            "boolean"),
    # Defense
    _market("NFL", "Defense", "Tackles", "tackles"),
    _market("NFL", "Defense", "Solo Tackles", "solo_tackles"),
    _market("NFL", "Defense", "Sacks", "sacks", "number"),
    _market("NFL", "Defense", "Interceptions", "interceptions"),
    _market("NFL", "Defense", "Passes Defended", "passes_defended"),
    _market("NFL", "Defense", "Forced Fumbles", "forced_fumbles"),
    # Kicking
    _market("NFL", "Kicking", "Field Goals Made", "field_goals_made"),
    _market("NFL", "Kicking", "Extra Points Made", "extra_points_made"),
    _market("NFL", "Kicking", "Kicking Points", "kicking_points"),
    # Special teams
    _market("NFL", "Special Teams", "Punt Return Yards",
            "punt_return_yards"),
    _market("NFL", "Special Teams", "Kickoff Return Yards",
            "kickoff_return_yards"),
    _market("NFL", "Special Teams", "Return Touchdowns",
            "return_touchdowns"),
]

MARKETS = _NBA + _NFL

_BY_ID = {m["market_id"]: m for m in MARKETS}
_BY_LEAGUE: dict[str, list[dict]] = {}
for _m in MARKETS:
    _BY_LEAGUE.setdefault(_m["league"], []).append(_m)


def _norm(text: str) -> str:
    return " ".join(str(text).strip().lower().replace("_", " ")
                    .replace("-", " ").split())


def get_league(league: str | None) -> dict | None:
    """League record for a league code (case-insensitive), or None."""
    if not league:
        return None
    return LEAGUES.get(str(league).strip().upper())


def league_supported(league: str | None) -> bool:
    """True only for leagues with a live Phase-1 taxonomy (NBA, NFL).

    Phase-later stubs (MLB, MMA) are registered but NOT supported yet —
    validation must fail them explicitly rather than guess at markets.
    """
    rec = get_league(league)
    return bool(rec and rec["status"] == "phase1")


def markets_for_league(league: str) -> list[dict]:
    rec = get_league(league)
    if not rec:
        return []
    return list(_BY_LEAGUE.get(rec["league"], []))


def get_market(league: str, market_ref: str | None) -> dict | None:
    """Resolve a market within a league by market_id, statistic_key, or
    market name (case/punctuation-insensitive). None if not defined."""
    rec = get_league(league)
    if not rec or market_ref is None:
        return None
    ref = str(market_ref).strip()
    for m in _BY_LEAGUE.get(rec["league"], []):
        if ref == m["market_id"] or ref == m["statistic_key"]:
            return m
    normed = _norm(ref)
    for m in _BY_LEAGUE.get(rec["league"], []):
        if normed in (_norm(m["market_name"]), _norm(m["statistic_key"]),
                      _norm(m["market_id"])):
            return m
    return None


def market_supported(league: str, market_ref: str | None) -> bool:
    m = get_market(league, market_ref)
    return bool(m and m["active"])
