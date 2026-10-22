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
from datetime import datetime, timezone
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


def _load_games_doc():
    """Read todays_games.json. An empty list (older runs) becomes an empty document."""
    import fetch_mlb
    raw = json.loads(fetch_mlb.OUT_FILE.read_text())
    if isinstance(raw, dict) and isinstance(raw.get("games"), list):
        return raw
    log.warning("todays_games.json was not a game document — using an empty slate.")
    return fetch_mlb.empty_document()


def _stamp_elo(doc: dict, elo_state: dict) -> None:
    doc["elo_last_date"] = elo_state.get("last_game_date") or "—"
    doc["elo_games_processed"] = elo_state.get("games_processed", 0)


def _write_doc(doc: dict) -> None:
    import fetch_mlb
    fetch_mlb.OUT_FILE.write_text(json.dumps(doc, indent=2))


def _result_message(doc: dict, counts: dict) -> str:
    """Notification text. Signal value is 'bet' — the same literal value_calc writes."""
    games = doc.get("games", [])
    bets_n = counts.get("bet", 0)
    bull = sum(
        1 for g in games
        if g.get("bullpen_flag_home") or g.get("bullpen_flag_away")
    )
    if bets_n > 0:
        parts = []
        for g in games:
            if g.get("signal") != "bet":
                continue
            side = g.get("bet_side")
            edge = g.get("edge_home") if side == "home" else g.get("edge_away")
            kelly = g.get("kelly_stake") or 0
            parts.append(
                f"{g['away_abbr']}@{g['home_abbr']} {side or '?'} "
                f"{(edge or 0) * 100:+.1f}% Kelly={kelly * 100:.1f}%"
            )
        return (
            f"⚾ MLB {doc.get('date')}: {bets_n} fogadás, {bull} bullpen | "
            + " | ".join(parts)
        )
    return (
        f"⚾ MLB {doc.get('date')}: {len(games)} meccs, nincs fogadás, {bull} bullpen"
    )


def _notify_results(doc: dict, counts: dict) -> None:
    notify(_result_message(doc, counts))
    games = doc.get("games", [])
    log.info(
        "Pipeline kész. bet=%d bullpen=%d total=%d",
        counts.get("bet", 0),
        sum(1 for g in games if g.get("bullpen_flag_home") or g.get("bullpen_flag_away")),
        len(games),
    )


def _finish_without_games() -> None:
    """Off day: still fold yesterday into Elo, and replace the dashboard."""
    import elo_engine as ee
    import generate_html as gh

    elo_state = ee.update_yesterday()
    doc = _load_games_doc()
    _stamp_elo(doc, elo_state)
    doc["generated_at"] = datetime.now(timezone.utc).isoformat()
    _write_doc(doc)
    gh.main()
    notify(f"⚾ MLB {doc.get('date')}: nincs mai mérkőzés.")
    log.info("No games today. Elo through %s. Empty dashboard written.",
             elo_state.get("last_game_date"))


def main():
    DATA_DIR.mkdir(exist_ok=True)

    # ── 1. MLB schedule ───────────────────────────────────────────────────────
    log.info("═══ Step 1: MLB schedule ═══")
    import fetch_mlb
    games = fetch_mlb.main(fetch_bullpen=True)
    if not games:
        log.warning("No games today — skipping odds, still updating Elo and HTML.")
        _finish_without_games()
        return

    # ── 2. Elo probabilities + FIP ────────────────────────────────────────────
    log.info("═══ Step 2: Elo + FIP ═══")
    import elo_engine as ee
    import fetch_pitchers as fp

    # Catch up Elo through yesterday (idempotent if already current)
    ee.update_yesterday()
    elo_state = ee.load_ratings()

    # Load FIP data + W-L records
    fip_data    = fp.load_fip()
    record_data = fp.load_records()

    doc = _load_games_doc()
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
    _stamp_elo(doc, elo_state)

    _write_doc(doc)
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
    doc2 = _load_games_doc()
    _notify_results(doc2, counts)


if __name__ == "__main__":
    main()
