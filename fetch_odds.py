"""
fetch_odds.py
Pinnacle MLB moneyline odds — The Odds API (the-odds-api.com)
Ingyenes tier: 500 kredit/hó, Pinnacle EU régióban elérhető.
Regisztráció: https://the-odds-api.com → API key → GitHub Secret: ODDS_API_KEY

Csak el nem kezdett meccsekre alkalmaz odds-ot (élő odds szűrés).
Napi első futásnál pre-game snapshot-ot ment a data/ könyvtárba.
"""

import json, logging, os
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR   = Path("data")
GAMES_FILE = DATA_DIR / "todays_games.json"
ODDS_FILE  = DATA_DIR / "odds_raw.json"

API_KEY  = os.environ.get("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
PARAMS   = {
    "apiKey":     API_KEY,
    "regions":    "eu",          # EU régió tartalmazza a Pinnacle-t
    "markets":    "h2h",
    "oddsFormat": "american",
    "bookmakers": "pinnacle",
}

log = logging.getLogger(__name__)


def american_to_implied(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def remove_vig(p_home: float, p_away: float) -> tuple[float, float]:
    total = p_home + p_away
    return round(p_home / total, 4), round(p_away / total, 4)


def fetch_odds() -> dict:
    log.info("Fetching Pinnacle MLB odds (the-odds-api.com) …")
    resp = requests.get(BASE_URL, params=PARAMS, timeout=20)

    # Log remaining credits if header present
    remaining = resp.headers.get("x-requests-remaining")
    used      = resp.headers.get("x-requests-used")
    if remaining:
        log.info("  API credits — used: %s, remaining: %s", used, remaining)

    resp.raise_for_status()
    raw = resp.json()

    if not isinstance(raw, list):
        log.warning("Unexpected API response: %s", type(raw))
        raw = []

    ODDS_FILE.write_text(json.dumps(raw, indent=2))
    log.info("  %d events returned", len(raw))
    return {e["id"]: e for e in raw}


def _normalize(name: str) -> str:
    return name.lower().replace(" ", "").replace(".", "")


def annotate_games(games_doc: dict, odds_map: dict) -> dict:
    now_utc = datetime.now(timezone.utc)
    skipped_live = 0

    for game in games_doc["games"]:

        # Élő meccsek kiszűrése — csak pre-game odds-ot fogadunk el
        game_dt_str = game.get("game_date", "")
        if game_dt_str:
            try:
                game_dt = datetime.fromisoformat(game_dt_str.replace("Z", "+00:00"))
                if game_dt < now_utc:
                    log.info("  Skip live game: %s @ %s (started %s UTC)",
                             game["away_abbr"], game["home_abbr"],
                             game_dt.strftime("%H:%M"))
                    skipped_live += 1
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
            log.warning("  No odds match: %s @ %s", game["away_name"], game["home_name"])
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

    if skipped_live:
        log.info("  %d live game(s) skipped (pre-game only policy)", skipped_live)

    return games_doc


def main():
    DATA_DIR.mkdir(exist_ok=True)
    if not API_KEY:
        raise RuntimeError("ODDS_API_KEY env var not set")

    odds_map  = fetch_odds()
    games_doc = json.loads(GAMES_FILE.read_text())

    # Pre-game snapshot — napi első futásnál menti (utólagos kiértékeléshez)
    snapshot_file = DATA_DIR / f"odds_pregame_{games_doc.get('date', 'today')}.json"
    if not snapshot_file.exists():
        snapshot_file.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "date":       games_doc.get("date"),
            "odds_raw":   list(odds_map.values()),
        }, indent=2))
        log.info("  Pre-game snapshot → %s", snapshot_file)

    games_doc = annotate_games(games_doc, odds_map)
    GAMES_FILE.write_text(json.dumps(games_doc, indent=2))

    n_with_odds = sum(1 for g in games_doc["games"] if g.get("pinnacle_home_odds"))
    log.info("Odds annotated: %d/%d games have Pinnacle odds",
             n_with_odds, len(games_doc["games"]))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")
    main()
