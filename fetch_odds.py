"""
fetch_odds.py
Pulls today's MLB moneyline odds from The Odds API (Pinnacle).
Writes data/odds_raw.json and annotates data/todays_games.json in place.
"""

import json, logging, os
from datetime import datetime, timezone
from pathlib import Path
import requests

DATA_DIR   = Path("data")
GAMES_FILE = DATA_DIR / "todays_games.json"
ODDS_FILE  = DATA_DIR / "odds_raw.json"

# ── Provider config ───────────────────────────────────────────────────────────
# OddsPapi (https://oddspapi.io) — tartalmazza a Pinnacle-t, 250 req/hó ingyenes
# Regisztráció: https://oddspapi.io/register
# Alternatíva: the-odds-api.com (500 kredit/hó, DE Pinnacle nélkül)
#
# Ha the-odds-api.com-ot használsz, állítsd PROVIDER = "theodds"-re
# és add meg az ottani kulcsot ODDS_API_KEY-ként.

PROVIDER = os.environ.get("ODDS_PROVIDER", "oddspapi")  # "oddspapi" | "theodds"
API_KEY  = os.environ.get("ODDS_API_KEY", "")

# OddsPapi endpoint
ODDSPAPI_URL = "https://api.oddspapi.io/v1/odds"
ODDSPAPI_PARAMS = {
    "token":    API_KEY,
    "sport":    "baseball",
    "league":   "MLB",
    "bookmakers": "pinnacle",
    "oddsFormat": "american",
}

# The Odds API endpoint (fallback, Pinnacle nem garantált)
THEODDS_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
THEODDS_PARAMS = {
    "apiKey":     API_KEY,
    "regions":    "eu",
    "markets":    "h2h",
    "oddsFormat": "american",
    "bookmakers": "pinnacle",
}

BASE_URL = ODDSPAPI_URL if PROVIDER == "oddspapi" else THEODDS_URL
PARAMS   = ODDSPAPI_PARAMS if PROVIDER == "oddspapi" else THEODDS_PARAMS

log = logging.getLogger(__name__)


def american_to_implied(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def remove_vig(p_home: float, p_away: float) -> tuple[float, float]:
    total = p_home + p_away
    return round(p_home / total, 4), round(p_away / total, 4)


def fetch_odds() -> dict:
    log.info("Fetching Pinnacle MLB odds (provider=%s) …", PROVIDER)
    resp = requests.get(BASE_URL, params=PARAMS, timeout=20)
    resp.raise_for_status()
    raw = resp.json()

    # OddsPapi wraps data in {"data": [...]}; The Odds API returns [...] directly
    events = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(events, list):
        log.warning("Unexpected odds response format: %s", type(events))
        events = []

    ODDS_FILE.write_text(json.dumps(events, indent=2))
    log.info("  %d events returned", len(events))
    return {e["id"]: e for e in events}


def _normalize(name: str) -> str:
    return name.lower().replace(" ", "").replace(".", "")


def annotate_games(games_doc: dict, odds_map: dict) -> dict:
    now_utc = datetime.now(timezone.utc)
    for game in games_doc["games"]:

        # Skip games that have already started — those would be live odds
        game_dt_str = game.get("game_date", "")
        if game_dt_str:
            try:
                game_dt = datetime.fromisoformat(game_dt_str.replace("Z", "+00:00"))
                if game_dt < now_utc:
                    log.info("Skipping live/finished game: %s @ %s (started %s UTC)",
                             game["away_abbr"], game["home_abbr"],
                             game_dt.strftime("%H:%M"))
                    continue
            except Exception:
                pass

        home_n = _normalize(game["home_name"])
        away_n = _normalize(game["away_name"])
        match  = None

        for ev in odds_map.values():
            h = _normalize(ev.get("home_team", ""))
            a = _normalize(ev.get("away_team", ""))
            if h == home_n and a == away_n:
                match = ev
                break

        if not match:
            log.warning("No odds: %s @ %s", game["away_name"], game["home_name"])
            continue

        for bm in match.get("bookmakers", []):
            if bm["key"] != "pinnacle":
                continue
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                outcomes = {o["name"]: o["price"] for o in mkt["outcomes"]}
                h_odds = outcomes.get(match["home_team"])
                a_odds = outcomes.get(match["away_team"])
                if h_odds is None or a_odds is None:
                    continue
                p_h = american_to_implied(h_odds)
                p_a = american_to_implied(a_odds)
                nv_h, nv_a = remove_vig(p_h, p_a)
                game["pinnacle_home_odds"] = h_odds
                game["pinnacle_away_odds"] = a_odds
                game["no_vig_prob_home"]   = nv_h
                game["no_vig_prob_away"]   = nv_a
    return games_doc


def main():
    DATA_DIR.mkdir(exist_ok=True)
    if not API_KEY:
        raise RuntimeError("ODDS_API_KEY env var not set")
    odds_map   = fetch_odds()
    games_doc  = json.loads(GAMES_FILE.read_text())

    # Save pre-game odds snapshot for later evaluation
    snapshot_file = DATA_DIR / f"odds_pregame_{games_doc.get('date','today')}.json"
    if not snapshot_file.exists():   # only save once per day (first run)
        snapshot_file.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "date":       games_doc.get("date"),
            "odds_raw":   list(odds_map.values()),
        }, indent=2))
        log.info("Pre-game odds snapshot → %s", snapshot_file)

    games_doc  = annotate_games(games_doc, odds_map)
    GAMES_FILE.write_text(json.dumps(games_doc, indent=2))
    log.info("Odds annotated → %s", GAMES_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    main()
