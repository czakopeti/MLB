"""
main.py — MLB Value Dashboard pipeline orchestrator.

Sorrend:
  1. fetch_mlb.py   → MLB Stats API + schedule, todays_games.json
  2. elo_engine.py  → Elo probs + FIP injection
  3. fetch_odds.py  → Pinnacle odds
  4. value_calc.py  → edge, signal, Kelly
  5. generate_html.py → index.html

Env vars (GitHub Secrets):
  ODDS_API_KEY   — The Odds API kulcs (kötelező)
  NTFY_TOPIC     — ntfy értesítési topic (opcionális)
"""

import json
import logging
import os
import sys
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")


def notify(msg: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(), timeout=10)
        log.info("ntfy sent: %s", msg)
    except Exception as e:
        log.warning("ntfy failed: %s", e)


def main():
    DATA_DIR.mkdir(exist_ok=True)

    # ── 1. MLB schedule ───────────────────────────────────────────────────────
    log.info("═══ Step 1: MLB schedule ═══")
    import fetch_mlb
    games = fetch_mlb.main(fetch_bullpen=True)
    if not games:
        log.warning("No games today — stopping.")
        notify("MLB Dashboard: nincs mai mérkőzés.")
        return

    # ── 2. Elo probabilities + FIP ────────────────────────────────────────────
    log.info("═══ Step 2: Elo + FIP ═══")
    import elo_engine as ee
    import fetch_pitchers as fp

    # Update Elo with yesterday's results (idempotent)
    ee.update_yesterday()
    elo_state = ee.load_ratings()

    # Load FIP data + W-L records
    fip_data    = fp.load_fip()
    record_data = fp.load_records()

    doc = json.loads(fetch_mlb.OUT_FILE.read_text())
    for game in doc["games"]:
        h_name = game["home_pitcher"].get("name", "TBD")
        a_name = game["away_pitcher"].get("name", "TBD")
        h_fip  = fip_data.get(h_name)
        a_fip  = fip_data.get(a_name)
        game["home_fip"] = h_fip
        game["away_fip"] = a_fip

        probs = ee.get_pregame_probs(
            game["home_abbr"], game["away_abbr"],
            home_fip=h_fip, away_fip=a_fip,
            state=elo_state,
        )
        # W-L rekord fuzzy lookup a pitcher stats cache-ből
        h_rec = fp.lookup_record(h_name, record_data)
        a_rec = fp.lookup_record(a_name, record_data)
        if h_rec:
            game["home_pitcher"]["record"] = h_rec
        if a_rec:
            game["away_pitcher"]["record"] = a_rec

        game.update({
            "model_prob_home":  probs["model_prob_home"],
            "model_prob_away":  probs["model_prob_away"],
            "elo_home":         probs["elo_home"],
            "elo_away":         probs["elo_away"],
            "pitcher_adj_home": probs["pitcher_adj_home"],
            "pitcher_adj_away": probs["pitcher_adj_away"],
            "fte_matched":      True,
        })

    # Elo státusz mentése a doc-ba (dashboard footer mutatja)
    doc["elo_last_date"]       = elo_state.get("last_game_date", "—")
    doc["elo_games_processed"] = elo_state.get("games_processed", 0)

    fetch_mlb.OUT_FILE.write_text(json.dumps(doc, indent=2))
    log.info("Elo + FIP injected for %d games", len(doc["games"]))

    # ── 3. Pinnacle odds ──────────────────────────────────────────────────────
    log.info("═══ Step 3: Pinnacle odds ═══")
    import fetch_odds as fo
    if not os.environ.get("ODDS_API_KEY"):
        log.error("ODDS_API_KEY not set — cannot fetch odds. Aborting.")
        sys.exit(1)
    fo.main()

    # ── 4. Value signals ──────────────────────────────────────────────────────
    log.info("═══ Step 4: Edge + Kelly ═══")
    import value_calc as vc
    counts = vc.main()
    log.info("Signals: %s", counts)

    # ── 5. HTML render ────────────────────────────────────────────────────────
    log.info("═══ Step 5: Generate HTML ═══")
    import generate_html as gh
    gh.main()

    # ── ntfy notification ─────────────────────────────────────────────────────
    v = counts.get("value", 0)
    f = counts.get("flag", 0)
    total = len(doc["games"])

    if v > 0:
        doc2   = json.loads(fetch_mlb.OUT_FILE.read_text())
        bets   = [
            f"{g['away_abbr']}@{g['home_abbr']} {g.get('bet_side','?')} "
            f"{(g.get('edge_home') or g.get('edge_away') or 0)*100:+.1f}% "
            f"Kelly={((g.get('kelly_stake') or 0)*100):.1f}%"
            for g in doc2["games"] if g.get("signal") == "value"
        ]
        notify(f"⚾ MLB {fetch_mlb.TODAY_STR}: {v} value bet, {f} piros | " + " | ".join(bets))
    else:
        notify(f"⚾ MLB {fetch_mlb.TODAY_STR}: {total} meccs, nincs value bet, {f} piros zászló")

    log.info("Pipeline kész. value=%d flag=%d total=%d", v, f, total)


if __name__ == "__main__":
    main()
