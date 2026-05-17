"""
elo_engine.py
Self-contained MLB Elo rating system.

Methodology (closely follows FiveThirtyEight's documented approach):
  - Initial rating:     1500 for all teams
  - K-factor:           4  (MLB has ~162 games/season → small K)
  - Home field edge:   +24 Elo points added to home team before E() calc
  - Pitcher adj:        convert starter FIP → Elo offset vs league average
  - Season carry-over:  regress 1/3 toward 1500 at start of each new season

Data source:
  MLB Stats API  https://statsapi.mlb.com/api/v1/schedule
  (free, no API key, returns final scores with winning/losing team)

Typical usage:
  python elo_engine.py --bootstrap 2022 2023 2024 2025
  python elo_engine.py --update          # add yesterday's results
  python elo_engine.py --ratings         # print current ratings table
"""

import argparse
import json
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR       = Path("data")
RATINGS_FILE   = DATA_DIR / "elo_ratings.json"
ELO_LOG_FILE   = DATA_DIR / "elo_game_log.jsonl"   # one processed game per line

MLB_API_BASE   = "https://statsapi.mlb.com/api/v1"

# Elo tuning constants
INITIAL_ELO    = 1500.0
K_FACTOR       = 4.0          # per-game update step
HOME_ADVANTAGE = 24.0         # Elo points added to home team
REGRESS_FRAC   = 1 / 3        # fraction regressed toward mean each new season

# Pitcher FIP adjustment
# FTE used pitcher_rgs (recent game score).  We substitute FIP:
# Each 1.0 FIP above/below league average ≈ 25 Elo points of edge.
# Average MLB FIP 2020-2025: ~4.15 (drifts slightly year to year)
LEAGUE_AVG_FIP = 4.15
FIP_SCALE      = 25.0         # Elo points per 1.0 FIP difference

# Request throttling (be polite to the MLB API)
API_SLEEP      = 0.3          # seconds between requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Team abbreviation map (MLB Stats API → our FTE-style 3-letter code) ───────
# Reused from fetch_mlb.py — keeping it local here so this module is standalone.
MLB_NAME_TO_ABBR: dict[str, str] = {
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "New York Yankees": "NYY",  "Tampa Bay Rays": "TBR",
    "Toronto Blue Jays": "TOR", "Chicago White Sox": "CHW",
    "Cleveland Guardians": "CLE", "Detroit Tigers": "DET",
    "Kansas City Royals": "KCR", "Minnesota Twins": "MIN",
    "Houston Astros": "HOU",    "Los Angeles Angels": "LAA",
    "Oakland Athletics": "OAK", "Seattle Mariners": "SEA",
    "Texas Rangers": "TEX",     "Atlanta Braves": "ATL",
    "Miami Marlins": "MIA",     "New York Mets": "NYM",
    "Philadelphia Phillies": "PHI", "Washington Nationals": "WSN",
    "Chicago Cubs": "CHC",      "Cincinnati Reds": "CIN",
    "Milwaukee Brewers": "MIL", "Pittsburgh Pirates": "PIT",
    "St. Louis Cardinals": "STL", "Arizona Diamondbacks": "ARI",
    "Colorado Rockies": "COL",  "Los Angeles Dodgers": "LAD",
    "San Diego Padres": "SDP",  "San Francisco Giants": "SFG",
    "Athletics": "OAK",
}

ALL_TEAMS = sorted(set(MLB_NAME_TO_ABBR.values()))


# ══════════════════════════════════════════════════════════════════════════════
# Core Elo math
# ══════════════════════════════════════════════════════════════════════════════

def expected_win_prob(elo_a: float, elo_b: float) -> float:
    """
    Standard Elo win probability for team A vs team B.
    Call with adjusted ratings (home advantage already added).
    """
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def update_elo(
    elo_winner: float,
    elo_loser: float,
    k: float = K_FACTOR,
) -> tuple[float, float]:
    """
    Return (new_elo_winner, new_elo_loser) after one game.
    Pass in the *pre-game* ratings (before home-advantage adjustment).
    The expected prob is calculated internally with raw ratings for symmetry.
    """
    e_win = expected_win_prob(elo_winner, elo_loser)
    delta  = k * (1.0 - e_win)
    return elo_winner + delta, elo_loser - delta


def fip_to_elo_adj(fip: Optional[float]) -> float:
    """
    Convert a starter's FIP to an Elo-point adjustment relative to league avg.
    Better pitcher (lower FIP) → positive adj (boosts their team's rating).
    Returns 0.0 if FIP is unknown.
    """
    if fip is None:
        return 0.0
    return (LEAGUE_AVG_FIP - fip) * FIP_SCALE


def pregame_prob(
    elo_home: float,
    elo_away: float,
    home_fip: Optional[float] = None,
    away_fip: Optional[float] = None,
) -> tuple[float, float]:
    """
    Compute home and away win probabilities including:
      - Home field advantage (+24 Elo to home)
      - Starting pitcher FIP adjustment (if provided)

    Returns (prob_home, prob_away)  — sum to 1.0.
    """
    home_adj = elo_home + HOME_ADVANTAGE
    away_adj = elo_away

    # Net pitcher advantage: home_adj = (avg - home_fip)*scale - (avg - away_fip)*scale
    #                                  = (away_fip - home_fip) * scale
    pitcher_net = fip_to_elo_adj(home_fip) - fip_to_elo_adj(away_fip)
    home_adj += pitcher_net   # positive = home pitcher is better

    prob_home = expected_win_prob(home_adj, away_adj)
    return round(prob_home, 4), round(1.0 - prob_home, 4)


def regress_ratings(ratings: dict[str, float]) -> dict[str, float]:
    """
    Season carry-over: pull each team 1/3 of the way toward 1500.
    Applied once at the start of every new season.
    """
    return {
        team: INITIAL_ELO + (1.0 - REGRESS_FRAC) * (elo - INITIAL_ELO)
        for team, elo in ratings.items()
    }


# ══════════════════════════════════════════════════════════════════════════════
# State: load / save ratings
# ══════════════════════════════════════════════════════════════════════════════

def _empty_state() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_game_date": None,
        "seasons_processed": [],
        "ratings": {t: INITIAL_ELO for t in ALL_TEAMS},
        "games_processed": 0,
    }


def load_ratings() -> dict:
    if RATINGS_FILE.exists():
        return json.loads(RATINGS_FILE.read_text())
    log.info("No ratings file found — starting fresh.")
    return _empty_state()


def save_ratings(state: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    state["generated_at"] = datetime.now(timezone.utc).isoformat()
    RATINGS_FILE.write_text(json.dumps(state, indent=2))
    log.info("Ratings saved → %s  (%d teams, %d games processed)",
             RATINGS_FILE, len(state["ratings"]), state["games_processed"])


# ══════════════════════════════════════════════════════════════════════════════
# MLB Stats API: fetch completed game results
# ══════════════════════════════════════════════════════════════════════════════

def _api_get(path: str, params: dict) -> dict:
    url  = f"{MLB_API_BASE}{path}"
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_season_results(year: int) -> list[dict]:
    """
    Fetch all completed regular-season games for a given year.
    Returns list of dicts: {date, home, away, home_score, away_score, winner}
    """
    # MLB regular season boundaries (approximate; API only returns actual games)
    start = f"{year}-03-20"
    end   = f"{year}-11-05"

    log.info("Fetching %d season results from MLB API …", year)

    params = {
        "sportId":   1,
        "startDate": start,
        "endDate":   end,
        "hydrate":   "linescore,team",
        "gameType":  "R",           # Regular season only
    }

    data  = _api_get("/schedule", params)
    dates = data.get("dates", [])
    results: list[dict] = []

    for date_block in dates:
        for game in date_block.get("games", []):
            rec = _parse_game_result(game)
            if rec:
                results.append(rec)

    log.info("  %d completed games found for %d", len(results), year)
    return results


def fetch_date_results(game_date: str) -> list[dict]:
    """
    Fetch completed games for a single date (YYYY-MM-DD).
    Used for daily incremental updates.
    """
    params = {
        "sportId":  1,
        "date":     game_date,
        "hydrate":  "linescore,team",
        "gameType": "R",
    }
    data  = _api_get("/schedule", params)
    dates = data.get("dates", [])
    results: list[dict] = []
    for date_block in dates:
        for game in date_block.get("games", []):
            rec = _parse_game_result(game)
            if rec:
                results.append(rec)
    return results


def _parse_game_result(game: dict) -> Optional[dict]:
    """
    Extract a clean result record from a raw API game node.
    Returns None if the game isn't finished.
    """
    state = game.get("status", {}).get("abstractGameState", "")
    if state != "Final":
        return None

    teams  = game.get("teams", {})
    home_t = teams.get("home", {})
    away_t = teams.get("away", {})

    home_name = home_t.get("team", {}).get("name", "")
    away_name = away_t.get("team", {}).get("name", "")
    home_abbr = MLB_NAME_TO_ABBR.get(home_name)
    away_abbr = MLB_NAME_TO_ABBR.get(away_name)

    if not home_abbr or not away_abbr:
        log.debug("Unknown team: '%s' or '%s'", home_name, away_name)
        return None

    # Scores come from the linescore hydration
    home_score = home_t.get("score")
    away_score = away_t.get("score")

    if home_score is None or away_score is None:
        return None

    # Determine winner (handle ties by skipping — extremely rare in MLB)
    if home_score == away_score:
        return None
    home_won = home_score > away_score

    return {
        "game_pk":   game.get("gamePk"),
        "date":      game.get("gameDate", "")[:10],
        "home":      home_abbr,
        "away":      away_abbr,
        "home_score": int(home_score),
        "away_score": int(away_score),
        "home_won":  home_won,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Elo processing: apply a list of game results to the ratings state
# ══════════════════════════════════════════════════════════════════════════════

def process_games(
    games:   list[dict],
    state:   dict,
    log_games: bool = False,
) -> dict:
    """
    Apply a sorted list of game results to the ratings state in-place.
    Handles season carry-over automatically when the year changes.
    Returns updated state.
    """
    ratings    = state["ratings"]
    last_year  = None
    log_handle = None

    if log_games:
        DATA_DIR.mkdir(exist_ok=True)
        log_handle = open(ELO_LOG_FILE, "a")

    # Games must be in chronological order
    games_sorted = sorted(games, key=lambda g: g["date"])

    for game in games_sorted:
        game_year = int(game["date"][:4])

        # Season transition: regress ratings toward mean
        if last_year is not None and game_year != last_year:
            log.info("Season boundary %d→%d: regressing ratings …",
                     last_year, game_year)
            ratings = regress_ratings(ratings)
            if game_year not in state["seasons_processed"]:
                state["seasons_processed"].append(game_year)

        last_year = game_year

        home = game["home"]
        away = game["away"]

        # Ensure teams exist (expansion teams, relocations)
        ratings.setdefault(home, INITIAL_ELO)
        ratings.setdefault(away, INITIAL_ELO)

        elo_home_pre = ratings[home]
        elo_away_pre = ratings[away]

        # Win probability with home field advantage (no pitcher adj at historical step)
        prob_home = expected_win_prob(elo_home_pre + HOME_ADVANTAGE, elo_away_pre)

        if game["home_won"]:
            new_home, new_away = update_elo(elo_home_pre, elo_away_pre)
        else:
            new_away, new_home = update_elo(elo_away_pre, elo_home_pre)

        if log_handle:
            log_handle.write(json.dumps({
                "date":          game["date"],
                "home":          home,
                "away":          away,
                "home_elo_pre":  round(elo_home_pre, 1),
                "away_elo_pre":  round(elo_away_pre, 1),
                "prob_home_pre": round(prob_home, 4),
                "home_won":      game["home_won"],
                "home_elo_post": round(new_home, 1),
                "away_elo_post": round(new_away, 1),
            }) + "\n")

        ratings[home] = new_home
        ratings[away] = new_away
        state["games_processed"] += 1

        if game["date"] > (state["last_game_date"] or ""):
            state["last_game_date"] = game["date"]

    state["ratings"] = ratings

    if log_handle:
        log_handle.close()

    return state


# ══════════════════════════════════════════════════════════════════════════════
# High-level commands
# ══════════════════════════════════════════════════════════════════════════════

def bootstrap(years: list[int]) -> dict:
    """
    Full rebuild from scratch for the given seasons.
    Overwrites RATINGS_FILE.
    """
    state = _empty_state()

    for year in sorted(years):
        if year not in state["seasons_processed"]:
            state["seasons_processed"].append(year)

        games = fetch_season_results(year)
        if not games:
            log.warning("No games returned for %d — skipping", year)
            continue

        state = process_games(games, state, log_games=True)
        save_ratings(state)          # checkpoint after each season
        time.sleep(API_SLEEP)

    log.info("Bootstrap complete.  Seasons: %s", state["seasons_processed"])
    return state


def update_yesterday(state: Optional[dict] = None) -> dict:
    """
    Fetch yesterday's results and update ratings.
    Safe to call daily from GitHub Actions.
    """
    if state is None:
        state = load_ratings()

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Avoid reprocessing
    if state.get("last_game_date") and state["last_game_date"] >= yesterday:
        log.info("Ratings already current through %s — nothing to do.",
                 state["last_game_date"])
        return state

    log.info("Fetching results for %s …", yesterday)
    games = fetch_date_results(yesterday)
    log.info("  %d completed games on %s", len(games), yesterday)

    if games:
        state = process_games(games, state, log_games=True)
        save_ratings(state)

    return state


def print_ratings_table(state: dict) -> None:
    """Pretty-print current ratings sorted by Elo descending."""
    ratings = state["ratings"]
    ranked  = sorted(ratings.items(), key=lambda x: x[1], reverse=True)

    print(f"\n{'Rank':>4}  {'Team':>4}  {'Elo':>7}  {'vs .500':>8}")
    print("─" * 30)
    for i, (team, elo) in enumerate(ranked, 1):
        diff = elo - INITIAL_ELO
        sign = "+" if diff >= 0 else ""
        print(f"{i:>4}  {team:>4}  {elo:>7.1f}  {sign}{diff:>7.1f}")

    last = state.get("last_game_date", "—")
    processed = state.get("games_processed", 0)
    print(f"\nLast game date: {last}  |  Games processed: {processed:,}")


# ══════════════════════════════════════════════════════════════════════════════
# Public API used by value_calc.py
# ══════════════════════════════════════════════════════════════════════════════

def get_pregame_probs(
    home_abbr: str,
    away_abbr: str,
    home_fip:  Optional[float] = None,
    away_fip:  Optional[float] = None,
    state:     Optional[dict]  = None,
) -> dict:
    """
    Main interface for the betting pipeline.
    Returns a dict with all probability and Elo fields needed by value_calc.py.

    Example:
        probs = get_pregame_probs("BOS", "NYY", home_fip=3.45, away_fip=3.90)
        # → {"model_prob_home": 0.5121, "model_prob_away": 0.4879,
        #     "elo_home": 1532.4, "elo_away": 1548.1,
        #     "pitcher_adj_home": 17.5, "pitcher_adj_away": -17.5,
        #     "source": "custom_elo"}
    """
    if state is None:
        state = load_ratings()

    ratings = state["ratings"]
    elo_home = ratings.get(home_abbr, INITIAL_ELO)
    elo_away = ratings.get(away_abbr, INITIAL_ELO)

    pitcher_adj_home = fip_to_elo_adj(home_fip)
    pitcher_adj_away = fip_to_elo_adj(away_fip)

    prob_home, prob_away = pregame_prob(elo_home, elo_away, home_fip, away_fip)

    return {
        "model_prob_home":   prob_home,
        "model_prob_away":   prob_away,
        "elo_home":          round(elo_home, 1),
        "elo_away":          round(elo_away, 1),
        "pitcher_adj_home":  round(pitcher_adj_home, 1),
        "pitcher_adj_away":  round(pitcher_adj_away, 1),
        "home_field_adj":    HOME_ADVANTAGE,
        "source":            "custom_elo",
        "ratings_as_of":     state.get("last_game_date"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="MLB Elo rating engine")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--bootstrap", nargs="+", type=int, metavar="YEAR",
        help="Rebuild ratings from scratch for given seasons, e.g. --bootstrap 2022 2023 2024 2025"
    )
    group.add_argument(
        "--update", action="store_true",
        help="Add yesterday's results to existing ratings"
    )
    group.add_argument(
        "--ratings", action="store_true",
        help="Print current ratings table"
    )
    group.add_argument(
        "--prob", nargs=2, metavar=("HOME", "AWAY"),
        help="Print pregame win probability, e.g. --prob BOS NYY"
    )

    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    if args.bootstrap:
        state = bootstrap(args.bootstrap)
        print_ratings_table(state)

    elif args.update:
        state = update_yesterday()
        print_ratings_table(state)

    elif args.ratings:
        state = load_ratings()
        print_ratings_table(state)

    elif args.prob:
        home, away = args.prob
        state  = load_ratings()
        result = get_pregame_probs(home, away, state=state)
        print(f"\n{away} @ {home}")
        print(f"  Elo:        home={result['elo_home']:.1f}  away={result['elo_away']:.1f}")
        print(f"  Prob home:  {result['model_prob_home']:.1%}")
        print(f"  Prob away:  {result['model_prob_away']:.1%}")
        print(f"  Ratings as of: {result['ratings_as_of']}")


if __name__ == "__main__":
    main()
