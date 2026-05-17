"""
fetch_mlb.py
Fetches today's MLB games from the official Stats API,
joins FiveThirtyEight Elo + pitcher-adjusted probabilities,
and writes data/todays_games.json.
"""

import json
import csv
import io
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
MLB_API_BASE  = "https://statsapi.mlb.com/api/v1"

# ⚠️  FiveThirtyEight shut down in 2023.  The primary URL below returns 403.
#     The GitHub archive is the last known copy (last updated ~Oct 2023).
#     For 2024+ seasons you need a replacement source — see ALTERNATIVES below.
#
# ALTERNATIVES if FTE data is stale / missing today's games:
#   A) Build your own Elo from scratch using pybaseball historical game logs
#      `pip install pybaseball` → `from pybaseball import schedule_and_record`
#   B) Baseball Reference win expectancy (scrape /leagues/MLB/standings.shtml)
#   C) Retrosheet Elo CSV maintained by the community on GitHub:
#      https://github.com/BillPetti/baseball-research/  (check for forks)
#   D) FanGraphs depth charts win probability (requires scraping)
#
# For now the code uses the GitHub archive and warns if today has no rows.
FTE_CSV_URL   = (
    "https://raw.githubusercontent.com/fivethirtyeight/data"
    "/master/mlb-elo/mlb_elo_latest.csv"
)
DATA_DIR      = Path("data")
OUT_FILE      = DATA_DIR / "todays_games.json"
TODAY_STR     = date.today().isoformat()          # e.g. "2026-05-17"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Team name normalisation ───────────────────────────────────────────────────
# FiveThirtyEight uses short franchise names; MLB API uses full names.
# This map converts MLB API full names → FTE abbreviation used in the CSV.
MLB_TO_FTE: dict[str, str] = {
    # AL East
    "Baltimore Orioles":       "BAL",
    "Boston Red Sox":          "BOS",
    "New York Yankees":        "NYY",
    "Tampa Bay Rays":          "TBR",
    "Toronto Blue Jays":       "TOR",
    # AL Central
    "Chicago White Sox":       "CHW",
    "Cleveland Guardians":     "CLE",
    "Detroit Tigers":          "DET",
    "Kansas City Royals":      "KCR",
    "Minnesota Twins":         "MIN",
    # AL West
    "Houston Astros":          "HOU",
    "Los Angeles Angels":      "LAA",
    "Oakland Athletics":       "OAK",
    "Seattle Mariners":        "SEA",
    "Texas Rangers":           "TEX",
    # NL East
    "Atlanta Braves":          "ATL",
    "Miami Marlins":           "MIA",
    "New York Mets":           "NYM",
    "Philadelphia Phillies":   "PHI",
    "Washington Nationals":    "WSN",
    # NL Central
    "Chicago Cubs":            "CHC",
    "Cincinnati Reds":         "CIN",
    "Milwaukee Brewers":       "MIL",
    "Pittsburgh Pirates":      "PIT",
    "St. Louis Cardinals":     "STL",
    # NL West
    "Arizona Diamondbacks":    "ARI",
    "Colorado Rockies":        "COL",
    "Los Angeles Dodgers":     "LAD",
    "San Diego Padres":        "SDP",
    "San Francisco Giants":    "SFG",
    # Relocated / renamed variants sometimes seen in older data
    "Athletics":               "OAK",
}

# Park factors: run-environment flags for known hitter-friendly parks
HITTER_FRIENDLY_PARKS: set[str] = {
    "Coors Field",           # extreme
    "Great American Ball Park",
    "Globe Life Field",
    "Guaranteed Rate Field",
    "American Family Field",
}

DIVISION_MAP: dict[str, str] = {
    "BAL": "AL East",  "BOS": "AL East",  "NYY": "AL East",
    "TBR": "AL East",  "TOR": "AL East",
    "CHW": "AL Central", "CLE": "AL Central", "DET": "AL Central",
    "KCR": "AL Central", "MIN": "AL Central",
    "HOU": "AL West",  "LAA": "AL West",  "OAK": "AL West",
    "SEA": "AL West",  "TEX": "AL West",
    "ATL": "NL East",  "MIA": "NL East",  "NYM": "NL East",
    "PHI": "NL East",  "WSN": "NL East",
    "CHC": "NL Central", "CIN": "NL Central", "MIL": "NL Central",
    "PIT": "NL Central", "STL": "NL Central",
    "ARI": "NL West",  "COL": "NL West",  "LAD": "NL West",
    "SDP": "NL West",  "SFG": "NL West",
}


# ── 1. FiveThirtyEight CSV ────────────────────────────────────────────────────

def fetch_fte_csv() -> dict[str, dict]:
    """
    Download FTE MLB Elo CSV and return a dict keyed by (date, team1, team2)
    for fast lookup.  Also build a secondary index by (team1, team2) for
    today's rows.
    Returns: {(team1_abbr, team2_abbr): row_dict}  — only today's games.
    """
    log.info("Downloading FiveThirtyEight MLB Elo CSV …")
    resp = requests.get(FTE_CSV_URL, timeout=30)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    today_rows: dict[tuple, dict] = {}

    for row in reader:
        if row.get("date") != TODAY_STR:
            continue
        team1 = row["team1"].strip()
        team2 = row["team2"].strip()
        today_rows[(team1, team2)] = row

    if not today_rows:
        log.warning(
            "⚠️  FTE CSV has NO rows for %s.  "
            "FiveThirtyEight shut down in 2023 — the archive may be stale.  "
            "model_prob_home/away will be None; run fetch_elo_fallback() instead.",
            TODAY_STR,
        )
    log.info("FTE rows for today (%s): %d", TODAY_STR, len(today_rows))
    return today_rows


def get_fte_row(fte_index: dict, home_abbr: str, away_abbr: str) -> dict | None:
    """
    FTE CSV has team1=away, team2=home convention (road team listed first).
    Try both orderings.
    """
    return (
        fte_index.get((away_abbr, home_abbr))
        or fte_index.get((home_abbr, away_abbr))
    )


# ── 2. MLB Stats API ──────────────────────────────────────────────────────────

def fetch_mlb_schedule(game_date: str = TODAY_STR) -> list[dict]:
    """
    Fetch today's schedule with probable pitchers, team info, and venue.
    Returns raw list of game dicts from the API.
    """
    url = f"{MLB_API_BASE}/schedule"
    params = {
        "sportId": 1,
        "date": game_date,
        "hydrate": "probablePitcher(note,stats),team,venue,linescore",
        "fields": (
            "dates,games,gamePk,gameDate,status,teams,"
            "home,away,team,name,abbreviation,"
            "probablePitcher,id,fullName,"
            "venue,name,"
            "seriesDescription,gameNumber,gamesInSeries"
        ),
    }
    log.info("Fetching MLB schedule for %s …", game_date)
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()

    data = resp.json()
    dates = data.get("dates", [])
    if not dates:
        log.warning("No games found for %s", game_date)
        return []

    games_raw = dates[0].get("games", [])
    log.info("Games found: %d", len(games_raw))
    return games_raw


# ── 3. Pitcher detail helper ──────────────────────────────────────────────────

def parse_pitcher(pitcher_node: dict | None) -> dict:
    if not pitcher_node:
        return {"id": None, "name": "TBD", "note": "", "record": ""}
    # Build W-L record string if stats are available
    stats = pitcher_node.get("stats", {}) or {}
    pitching = stats.get("pitching", {}) or {}
    wins   = pitching.get("wins")
    losses = pitching.get("losses")
    record = f"{wins}-{losses}" if wins is not None and losses is not None else ""
    return {
        "id":     pitcher_node.get("id"),
        "name":   pitcher_node.get("fullName", "TBD"),
        "note":   pitcher_node.get("note", ""),
        "record": record,
    }


# ── 4. Build enriched game record ─────────────────────────────────────────────

def build_game_record(raw: dict, fte_index: dict) -> dict:
    """
    Merge MLB API game data with FTE Elo row.
    Returns a clean, flat dict ready for JSON output.
    """
    teams  = raw.get("teams", {})
    home_t = teams.get("home", {})
    away_t = teams.get("away", {})

    home_name = home_t.get("team", {}).get("name", "")
    away_name = away_t.get("team", {}).get("name", "")
    home_abbr = MLB_TO_FTE.get(home_name, home_t.get("team", {}).get("abbreviation", "???"))
    away_abbr = MLB_TO_FTE.get(away_name, away_t.get("team", {}).get("abbreviation", "???"))

    venue_name = raw.get("venue", {}).get("name", "")
    game_dt    = raw.get("gameDate", "")          # ISO-8601 UTC string

    home_pitcher = parse_pitcher(home_t.get("probablePitcher"))
    away_pitcher = parse_pitcher(away_t.get("probablePitcher"))

    # ── FTE join ──────────────────────────────────────────────────────────────
    fte = get_fte_row(fte_index, home_abbr, away_abbr)

    fte_home_prob: float | None = None
    fte_away_prob: float | None = None
    elo_home: float | None = None
    elo_away: float | None = None
    pitcher_home_rgs: float | None = None
    pitcher_away_rgs: float | None = None
    fte_matched = False

    if fte:
        fte_matched = True
        # FTE convention: team1=away, team2=home (road team listed first)
        # rating_prob1 is win prob for team1 (away), rating_prob2 for team2 (home)
        # We detect orientation by matching abbreviations
        if fte["team2"] == home_abbr:
            # standard orientation: team1=away, team2=home
            fte_home_prob      = _safe_float(fte.get("rating_prob2"))
            fte_away_prob      = _safe_float(fte.get("rating_prob1"))
            elo_home           = _safe_float(fte.get("elo2_pre"))
            elo_away           = _safe_float(fte.get("elo1_pre"))
            pitcher_home_rgs   = _safe_float(fte.get("pitcher2_rgs"))
            pitcher_away_rgs   = _safe_float(fte.get("pitcher1_rgs"))
        else:
            # reversed orientation
            fte_home_prob      = _safe_float(fte.get("rating_prob1"))
            fte_away_prob      = _safe_float(fte.get("rating_prob2"))
            elo_home           = _safe_float(fte.get("elo1_pre"))
            elo_away           = _safe_float(fte.get("elo2_pre"))
            pitcher_home_rgs   = _safe_float(fte.get("pitcher1_rgs"))
            pitcher_away_rgs   = _safe_float(fte.get("pitcher2_rgs"))

    status = raw.get("status", {}).get("abstractGameState", "Preview")

    return {
        # ── identifiers ───────────────────────────────────────────────────────
        "game_pk":          raw.get("gamePk"),
        "game_date":        game_dt,
        "status":           status,
        "series":           raw.get("seriesDescription", "Regular Season"),
        "game_number":      raw.get("gameNumber", 1),        # doubleheader
        "games_in_series":  raw.get("gamesInSeries", 1),

        # ── teams ─────────────────────────────────────────────────────────────
        "home_name":        home_name,
        "away_name":        away_name,
        "home_abbr":        home_abbr,
        "away_abbr":        away_abbr,
        "home_division":    DIVISION_MAP.get(home_abbr, "Unknown"),
        "away_division":    DIVISION_MAP.get(away_abbr, "Unknown"),

        # ── venue ─────────────────────────────────────────────────────────────
        "venue":            venue_name,
        "park_flag":        venue_name in HITTER_FRIENDLY_PARKS,

        # ── pitchers ──────────────────────────────────────────────────────────
        "home_pitcher":     home_pitcher,
        "away_pitcher":     away_pitcher,

        # ── FTE Elo model ─────────────────────────────────────────────────────
        "fte_matched":      fte_matched,
        "elo_home":         elo_home,
        "elo_away":         elo_away,
        "pitcher_home_rgs": pitcher_home_rgs,   # pitcher rating adjustment
        "pitcher_away_rgs": pitcher_away_rgs,
        "model_prob_home":  fte_home_prob,       # Elo + pitcher adjusted
        "model_prob_away":  fte_away_prob,

        # ── placeholders filled by later pipeline stages ───────────────────
        # fetch_odds.py  → adds pinnacle_home_odds / away_odds / no_vig_probs
        # fetch_pitchers.py → adds home_fip / away_fip / xfip / siera
        # value_calc.py  → adds edge, signal, kelly_stake
        # fetch_mlb.py   → bullpen exhaustion added in next pass (separate fn)
        "pinnacle_home_odds":      None,
        "pinnacle_away_odds":      None,
        "no_vig_prob_home":        None,
        "no_vig_prob_away":        None,
        "home_fip":                None,
        "away_fip":                None,
        "edge_home":               None,
        "edge_away":               None,
        "signal":                  None,
        "kelly_stake":             None,
        "bullpen_flag_home":       False,
        "bullpen_flag_away":       False,
    }


# ── 5. Bullpen exhaustion (MLB Stats API pitch log) ───────────────────────────

def fetch_bullpen_loads(team_id: int, days: int = 3) -> int:
    """
    Returns total bullpen pitches thrown by a team over the last `days` days.
    Uses the MLB Stats API /schedule + game boxscore pitching logs.
    """
    from datetime import timedelta
    end_dt   = date.today()
    start_dt = end_dt - timedelta(days=days)

    url = f"{MLB_API_BASE}/schedule"
    params = {
        "sportId": 1,
        "teamId":  team_id,
        "startDate": start_dt.isoformat(),
        "endDate":   end_dt.isoformat(),
        "hydrate":   "linescore",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        dates = resp.json().get("dates", [])
    except Exception as exc:
        log.warning("Bullpen fetch failed for team %s: %s", team_id, exc)
        return 0

    game_pks = [
        g["gamePk"]
        for d in dates
        for g in d.get("games", [])
        if g.get("status", {}).get("abstractGameState") == "Final"
    ]

    total_bullpen_pitches = 0
    for pk in game_pks:
        total_bullpen_pitches += _get_bullpen_pitches(pk, team_id)

    return total_bullpen_pitches


def _get_bullpen_pitches(game_pk: int, team_id: int) -> int:
    """
    Parse a single game's boxscore to count relief-pitcher pitches for team_id.
    """
    url = f"{MLB_API_BASE}/game/{game_pk}/boxscore"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        box = resp.json()
    except Exception as exc:
        log.debug("Boxscore fetch failed %s: %s", game_pk, exc)
        return 0

    # Determine 'home' or 'away' side for this team
    teams_box = box.get("teams", {})
    side = None
    for s in ("home", "away"):
        if teams_box.get(s, {}).get("team", {}).get("id") == team_id:
            side = s
            break
    if not side:
        return 0

    pitchers = teams_box[side].get("pitchers", [])  # list of player IDs
    players  = teams_box[side].get("players", {})

    bullpen_pitches = 0
    # First pitcher is the starter — skip them, count only relievers
    for pid in pitchers[1:]:
        key   = f"ID{pid}"
        stats = (players.get(key, {})
                        .get("stats", {})
                        .get("pitching", {})
                        .get("numberOfPitches", 0))
        bullpen_pitches += int(stats or 0)

    return bullpen_pitches


BULLPEN_EXHAUSTION_THRESHOLD = 200   # pitches over 3 days


def annotate_bullpen_flags(games: list[dict]) -> list[dict]:
    """
    For each game, fetch 3-day bullpen pitch loads for home and away teams,
    and set bullpen_flag_home / bullpen_flag_away on the record.
    """
    # Build team_id lookup (need a fresh API call)
    team_ids = _fetch_team_ids()

    for game in games:
        for side in ("home", "away"):
            abbr    = game[f"{side}_abbr"]
            team_id = team_ids.get(abbr)
            if not team_id:
                continue
            load = fetch_bullpen_loads(team_id)
            log.info("Bullpen load %s (%s): %d pitches / 3 days", abbr, side, load)
            game[f"bullpen_flag_{side}"] = load >= BULLPEN_EXHAUSTION_THRESHOLD
            game[f"bullpen_pitches_{side}_3d"] = load

    return games


def _fetch_team_ids() -> dict[str, int]:
    """Returns {FTE_abbr: mlb_team_id} for all MLB teams."""
    url = f"{MLB_API_BASE}/teams"
    params = {"sportId": 1, "activeStatus": "Active"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        teams = resp.json().get("teams", [])
    except Exception as exc:
        log.warning("Team ID fetch failed: %s", exc)
        return {}

    result: dict[str, int] = {}
    for t in teams:
        name  = t.get("name", "")
        abbr  = MLB_TO_FTE.get(name)
        if abbr:
            result[abbr] = t["id"]
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main(fetch_bullpen: bool = True) -> list[dict]:
    DATA_DIR.mkdir(exist_ok=True)

    # 1. FiveThirtyEight Elo CSV
    fte_index = fetch_fte_csv()

    # 2. MLB Stats API schedule
    raw_games = fetch_mlb_schedule()
    if not raw_games:
        log.warning("No games today — writing empty output.")
        OUT_FILE.write_text(json.dumps([], indent=2))
        return []

    # 3. Build enriched records
    games: list[dict] = []
    for raw in raw_games:
        record = build_game_record(raw, fte_index)

        if not record["fte_matched"]:
            log.warning(
                "FTE match missing: %s @ %s",
                record["away_abbr"], record["home_abbr"]
            )
        games.append(record)

    log.info("Enriched %d games (FTE matched: %d)",
             len(games),
             sum(1 for g in games if g["fte_matched"]))

    # 4. Bullpen exhaustion (optional, costs extra API calls)
    if fetch_bullpen and games:
        log.info("Fetching bullpen pitch loads (last 3 days) …")
        games = annotate_bullpen_flags(games)

    # 5. Sort: AL first, then NL; within each, alphabetical by home team
    def sort_key(g):
        div = g.get("home_division", "ZZ")
        league = 0 if div.startswith("AL") else 1
        return (league, div, g["home_name"])

    games.sort(key=sort_key)

    # 6. Write output
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date":         TODAY_STR,
        "game_count":   len(games),
        "games":        games,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log.info("Written → %s  (%d games)", OUT_FILE, len(games))
    return games


if __name__ == "__main__":
    # Pass --no-bullpen flag to skip the expensive bullpen API calls during dev
    fetch_bp = "--no-bullpen" not in sys.argv
    main(fetch_bullpen=fetch_bp)
