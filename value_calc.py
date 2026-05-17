"""
value_calc.py
Fogadási szignál logika:
  BET ha MINDHÁROM feltétel teljesül:
    1. Az egyik oldal dobójának jobb a W-L rekordja
    2. Ugyanannak az oldalnak alacsonyabb a FIP-je (jobb)
    3. A mi modellünk magasabb valószínűséget ad annak az oldalnak
       mint a Pinnacle (pozitív edge)
  Minden más esetben: nincs szignál.
"""

import json, logging
from pathlib import Path

DATA_DIR   = Path("data")
GAMES_FILE = DATA_DIR / "todays_games.json"

KELLY_FRACTION = 0.25
MAX_BET        = 0.03

log = logging.getLogger(__name__)


def parse_record(rec: str) -> float | None:
    """'6-2' → 0.75  |  '0-0' vagy None → None"""
    if not rec or "-" not in rec:
        return None
    try:
        w, l = rec.split("-")
        w, l = int(w.strip()), int(l.strip())
        total = w + l
        return w / total if total > 0 else None
    except (ValueError, TypeError):
        return None


def kelly(prob: float, odds_american: int) -> float:
    """Quarter-Kelly stake as fraction of bankroll."""
    if odds_american > 0:
        b = odds_american / 100
    else:
        b = 100 / abs(odds_american)
    q   = 1 - prob
    raw = (b * prob - q) / b
    if raw <= 0:
        return 0.0
    return round(min(raw * KELLY_FRACTION, MAX_BET), 4)


def pitcher_advantage(game: dict) -> str | None:
    """
    Melyik oldalnak van pitching előnye?
    Feltétel: jobb W-L rekord ÉS alacsonyabb FIP ugyanazon az oldalon.
    Visszatér: 'home' | 'away' | None (ha nem dönthető el)
    """
    hp = game.get("home_pitcher", {})
    ap = game.get("away_pitcher", {})
    h_fip = game.get("home_fip")
    a_fip = game.get("away_fip")
    h_rec = parse_record(hp.get("record", ""))
    a_rec = parse_record(ap.get("record", ""))

    # Ha valamelyik adat hiányzik, nem tudunk dönteni
    if h_fip is None or a_fip is None:
        return None
    if h_rec is None or a_rec is None:
        return None

    home_better_fip = h_fip < a_fip        # alacsonyabb FIP = jobb
    home_better_rec = h_rec > a_rec        # magasabb win% = jobb

    if home_better_fip and home_better_rec:
        return "home"
    if (not home_better_fip) and (not home_better_rec):
        return "away"
    return None   # az egyik metrikában az egyik, a másikban a másik nyeri → nincs egyértelmű előny


def compute_signals(game: dict) -> dict:
    mp_h = game.get("model_prob_home")
    mp_a = game.get("model_prob_away")
    nv_h = game.get("no_vig_prob_home")
    nv_a = game.get("no_vig_prob_away")

    # Mindig kiszámítjuk az edge-et megjelenítés céljából
    if mp_h is not None and nv_h is not None:
        game["edge_home"] = round(mp_h - nv_h, 4)
        game["edge_away"] = round(mp_a - nv_a, 4)
    else:
        game["edge_home"] = None
        game["edge_away"] = None

    # Alapértelmezett: nincs szignál
    game["signal"]      = "none"
    game["bet_side"]    = None
    game["kelly_stake"] = None

    if mp_h is None or mp_a is None:
        game["signal"] = "no_model"
        return game
    if nv_h is None or nv_a is None:
        game["signal"] = "no_odds"
        return game

    # Melyik oldalnak van pitcher előnye?
    adv = pitcher_advantage(game)
    if adv is None:
        return game   # nincs egyértelmű pitcher előny → nincs fogadás

    # Modell edge az előnyös oldalon
    if adv == "home":
        model_prob = mp_h
        mkt_prob   = nv_h
        odds       = game.get("pinnacle_home_odds")
    else:
        model_prob = mp_a
        mkt_prob   = nv_a
        odds       = game.get("pinnacle_away_odds")

    edge = model_prob - mkt_prob

    if edge > 0:
        # Minden feltétel teljesül → BET
        game["signal"]      = "bet"
        game["bet_side"]    = adv
        game["kelly_stake"] = kelly(model_prob, odds) if odds else None

    return game


def main():
    DATA_DIR.mkdir(exist_ok=True)
    doc = json.loads(GAMES_FILE.read_text())
    counts = {"bet": 0, "none": 0, "no_odds": 0, "no_model": 0}
    for game in doc["games"]:
        compute_signals(game)
        s = game.get("signal", "none")
        counts[s] = counts.get(s, 0) + 1
    GAMES_FILE.write_text(json.dumps(doc, indent=2))
    log.info("Signals: %s", counts)
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")
    main()
