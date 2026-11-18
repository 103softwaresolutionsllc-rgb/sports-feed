export const SPORT_TO_LEAGUE = {
  Basketball: 'nba',
  Football: 'nfl',
  Baseball: 'mlb',
  Hockey: 'nhl',
  MMA: 'mma',
};

export const LEAGUE_CONFIG = {
  Basketball: {
    teamStats: {
      offense: { key: 'Points', label: 'PPG', avg: 112, higherIsBetter: true, weight: 1.0 },
      defense: { key: 'OppPoints', label: 'Opp PPG', avg: 112, higherIsBetter: false, weight: 0.9 },
      rebounds: { key: 'Rebounds', label: 'Rebounds', avg: 44, higherIsBetter: true, weight: 0.3 },
      assists: { key: 'Assists', label: 'Assists', avg: 26, higherIsBetter: true, weight: 0.3 },
      efficiency: { key: 'Field Goal %', label: 'FG%', avg: 47, higherIsBetter: true, weight: 0.2 },
    },
    fieldMap: {
      points: 'Points',
      opp_ppg: 'OppPoints',
      rebounds: 'Rebounds',
      assists: 'Assists',
      fg_pct: 'Field Goal %',
      'Field Goal %': 'Field Goal %',
      Points: 'Points',
      OppPoints: 'OppPoints',
      Rebounds: 'Rebounds',
      Assists: 'Assists',
    },
    playerProps: {
      Points: { statKey: 'Points', varianceFactor: 0.18, defenseKey: 'OppPointsAllowed' },
      Rebounds: { statKey: 'Rebounds', varianceFactor: 0.2, defenseKey: 'OppReboundsAllowed' },
      Assists: { statKey: 'Assists', varianceFactor: 0.22, defenseKey: 'OppAssistsAllowed' },
      'Field Goal %': { statKey: 'Field Goal %', varianceFactor: 0.12, defenseKey: 'OppFieldGoalPctAllowed' },
    },
  },
  Football: {
    teamStats: {
      offense: { key: 'Points', label: 'PPG', avg: 23.5, higherIsBetter: true, weight: 1.0 },
      defense: { key: 'OppPoints', label: 'Opp PPG', avg: 23.5, higherIsBetter: false, weight: 1.0 },
      yards: { key: 'Yards', label: 'Yds/G', avg: 350, higherIsBetter: true, weight: 0.45 },
      turnovers: { key: 'Turnovers', label: 'Turnovers', avg: 1.5, higherIsBetter: false, weight: 0.4 },
    },
    fieldMap: {
      points: 'Points',
      opp_ppg: 'OppPoints',
      yards: 'Yards',
      turnovers: 'Turnovers',
      Points: 'Points',
      OppPoints: 'OppPoints',
      Yards: 'Yards',
      Turnovers: 'Turnovers',
    },
    playerProps: {
      'Passing Yards': { statKey: 'Passing Yards', varianceFactor: 0.16, defenseKey: 'OppPassingYardsAllowed' },
      'Rushing Yards': { statKey: 'Rushing Yards', varianceFactor: 0.22, defenseKey: 'OppRushingYardsAllowed' },
      Receptions: { statKey: 'Receptions', varianceFactor: 0.24, defenseKey: 'OppReceptionsAllowed' },
      Touchdowns: { statKey: 'Touchdowns', varianceFactor: 0.35, defenseKey: 'OppTouchdownsAllowed' },
    },
  },
  Baseball: {
    teamStats: {
      offense: { key: 'Runs', label: 'R/G', avg: 4.5, higherIsBetter: true, weight: 1.0 },
      defense: { key: 'ERA', label: 'ERA', avg: 4.2, higherIsBetter: false, weight: 1.0 },
      batting: { key: 'Batting Average', label: 'AVG', avg: 0.248, higherIsBetter: true, weight: 0.35 },
      hits: { key: 'Hits', label: 'Hits', avg: 8, higherIsBetter: true, weight: 0.2 },
      errors: { key: 'Errors', label: 'Errors', avg: 0.8, higherIsBetter: false, weight: 0.18 },
    },
    fieldMap: {
      points: 'Runs',
      era: 'ERA',
      avg: 'Batting Average',
      hits: 'Hits',
      errors: 'Errors',
      Runs: 'Runs',
      ERA: 'ERA',
      'Batting Average': 'Batting Average',
      Hits: 'Hits',
      Errors: 'Errors',
    },
    playerProps: {
      'Batting Average': { statKey: 'Batting Average', varianceFactor: 0.1, defenseKey: 'OppBattingAverageAllowed' },
      'Home Runs': { statKey: 'Home Runs', varianceFactor: 0.4, defenseKey: 'OppHomeRunsAllowed' },
      RBIs: { statKey: 'RBIs', varianceFactor: 0.28, defenseKey: 'OppRBIsAllowed' },
      'Pitcher ERA': { statKey: 'Pitcher ERA', varianceFactor: 0.18, defenseKey: 'OppERA' },
    },
  },
  Hockey: {
    teamStats: {
      offense: { key: 'Goals', label: 'GF/G', avg: 3.1, higherIsBetter: true, weight: 1.0 },
      defense: { key: 'OppGoals', label: 'GA/G', avg: 3.1, higherIsBetter: false, weight: 1.0 },
      goalie: { key: 'Save %', label: 'SV%', avg: 0.905, higherIsBetter: true, weight: 0.45 },
      shots: { key: 'Shots on Goal', label: 'SOG', avg: 30, higherIsBetter: true, weight: 0.25 },
    },
    fieldMap: {
      points: 'Goals',
      opp_ppg: 'OppGoals',
      save_pct: 'Save %',
      shots: 'Shots on Goal',
      Goals: 'Goals',
      OppGoals: 'OppGoals',
      'Save %': 'Save %',
      'Shots on Goal': 'Shots on Goal',
    },
    playerProps: {
      Goals: { statKey: 'Goals', varianceFactor: 0.32, defenseKey: 'OppGoalsAllowed' },
      Assists: { statKey: 'Assists', varianceFactor: 0.26, defenseKey: 'OppAssistsAllowed' },
      'Shots on Goal': { statKey: 'Shots on Goal', varianceFactor: 0.2, defenseKey: 'OppShotsAllowed' },
      'Save %': { statKey: 'Save %', varianceFactor: 0.08, defenseKey: 'OppShotQuality' },
    },
  },
  MMA: {
    teamStats: {
      strikes: { key: 'Strikes', label: 'SLpM', avg: 4.5, higherIsBetter: true, weight: 0.5 },
      takedowns: { key: 'Takedowns', label: 'TD Avg', avg: 1.2, higherIsBetter: true, weight: 0.3 },
      submissions: { key: 'Submissions', label: 'Sub Avg', avg: 0.3, higherIsBetter: true, weight: 0.2 },
    },
    fieldMap: {
      points: 'Strikes',
      opp_ppg: 'Submissions',
      takedowns: 'Takedowns',
      Strikes: 'Strikes',
      Takedowns: 'Takedowns',
      Submissions: 'Submissions',
    },
    playerProps: {
      'Method of Victory': { statKey: 'WinMethodProfile', varianceFactor: 0.25, defenseKey: 'FinishVulnerability' },
      'Fight Goes the Distance': { statKey: 'DistanceProfile', varianceFactor: 0.2, defenseKey: 'DistanceRate' },
      'Inside the Distance': { statKey: 'InsideDistanceProfile', varianceFactor: 0.2, defenseKey: 'FinishVulnerability' },
      'Round Betting': { statKey: 'Avg Rounds', varianceFactor: 0.35, defenseKey: 'Avg Rounds Allowed' },
      'Total Rounds Over/Under': { statKey: 'Avg Rounds', varianceFactor: 0.35, defenseKey: 'Avg Rounds Allowed' },
    },
  },
};

export function getLeagueConfig(sport) {
  return LEAGUE_CONFIG[sport] || LEAGUE_CONFIG.Basketball;
}

export function getCanonicalFieldMap(sport) {
  return getLeagueConfig(sport).fieldMap || {};
}

export function getCanonicalStatMeta(sport, key) {
  const cfg = getLeagueConfig(sport).teamStats || {};
  return Object.values(cfg).find((item) => item.key === key) || null;
}

export function getCanonicalTeamStatKeys(sport) {
  return Object.values(getLeagueConfig(sport).teamStats || {}).map((item) => item.key);
}

export function getPrimaryDisplayStat(sport) {
  return getLeagueConfig(sport).teamStats?.offense?.key || getCanonicalTeamStatKeys(sport)[0] || 'Points';
}
