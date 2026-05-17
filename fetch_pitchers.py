"""
fetch_pitchers.py
FIP számítása az MLB Stats API-ból (nem FanGraphs/pybaseball).

Miért nem pybaseball:
  FanGraphs 403-at ad vissza automatizált kérésekre (GitHub Actions).

Megoldás:
  MLB Stats API → K, BB, HR, HBP, IP → FIP képlet
  FIP = ((13×HR + 3×(BB+HBP) - 2×K) / IP) + C_FIP
  ahol C_FIP ≈ 3.15 (liga-szintű állandó)

Funkciók:
  - Fuzzy name matching (SequenceMatcher) a néveltérések kezelésére
  - Korai szezon blending: kevés IP esetén előző szezon FIP-jével súlyozunk
"""

import argparse
import json
import logging
import time
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

DATA_DIR    = Path("data")
FIP_FILE    = DATA_DIR / "pitchers_fip.json"
CACHE_DAYS  = 7

MLB_API     = "https://statsapi.mlb.com/api/v1"
C_FIP       = 3.15   # FIP konstans (liga ERA - ligaszintű FIP-numerátor/IP)

IP_MIN         = 30.0   # innings alatt "korai szezon"
REGRESSION_IP  = 40.0   # regressziós súly az előző szezon FIP felé
LEAGUE_AVG_FIP = 4.15   # fallback ha se mostani, se előző szezon nincs

FUZZY_THRESHOLD = 0.82

log = logging.getLogger(__name__)


# ── FIP számítás ──────────────────────────────────────────────────────────────

def calc_fip(k: float, bb: float, hbp: float, hr: float, ip: float) -> float | None:
    if ip < 1.0:
        return None
    return round((13*hr + 3*(bb+hbp) - 2*k) / ip + C_FIP, 2)


def parse_ip(ip_str) -> float:
    """'5.2' → 5.667 (MLB IP notation: .1 = 1/3 inning)"""
    try:
        s = str(ip_str or "0")
        if "." in s:
            whole, frac = s.split(".", 1)
            return int(whole) + int(frac[0]) / 3
        return float(s)
    except (ValueError, TypeError):
        return 0.0


# ── MLB Stats API ─────────────────────────────────────────────────────────────

def fetch_pitching_stats(season: int) -> dict[str, dict]:
    """
    Fetch season pitching stats from MLB Stats API and compute FIP.
    Returns {full_name: {"fip", "ip", "k", "bb", "hr", "hbp", "gs"}}
    """
    url = f"{MLB_API}/stats"
    params = {
        "stats":      "season",
        "group":      "pitching",
        "gameType":   "R",
        "season":     season,
        "playerPool": "All",
        "limit":      2000,
        "fields": (
            "stats,splits,player,fullName,"
            "stat,strikeOuts,baseOnBalls,homeRuns,"
            "hitBatsmen,inningsPitched,gamesStarted,era"
        ),
    }

    log.info("Fetching MLB pitching stats for %d …", season)
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()

    splits = resp.json().get("stats", [{}])[0].get("splits", [])
    log.info("  %d pitcher-season rows returned", len(splits))

    result: dict[str, dict] = {}
    for row in splits:
        name = row.get("player", {}).get("fullName", "")
        stat = row.get("stat", {})
        if not name:
            continue

        ip  = parse_ip(stat.get("inningsPitched", "0"))
        k   = float(stat.get("strikeOuts",   0) or 0)
        bb  = float(stat.get("baseOnBalls",  0) or 0)
        hr  = float(stat.get("homeRuns",     0) or 0)
        hbp = float(stat.get("hitBatsmen",   0) or 0)
        gs  = int(stat.get("gamesStarted",   0) or 0)

        fip = calc_fip(k, bb, hbp, hr, ip)

        wins   = int(stat.get("wins",   0) or 0)
        losses = int(stat.get("losses", 0) or 0)
        result[name] = {
            "fip":    fip,
            "ip":     round(ip, 1),
            "k":      int(k),
            "bb":     int(bb),
            "hr":     int(hr),
            "hbp":    int(hbp),
            "gs":     gs,
            "wins":   wins,
            "losses": losses,
            "record": f"{wins}-{losses}" if (wins + losses) > 0 else "",
        }

    return result


# ── Fuzzy name matching ───────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    return (name.lower()
               .replace(".", "")
               .replace("-", " ")
               .replace("'", "")
               .strip())


def fuzzy_get(name: str, lookup: dict, threshold: float = FUZZY_THRESHOLD):
    """Fuzzy dict lookup by name similarity."""
    if not name or not lookup:
        return None
    norm = _normalize(name)
    best_ratio, best_val = 0.0, None
    for key, val in lookup.items():
        r = SequenceMatcher(None, norm, _normalize(key)).ratio()
        if r > best_ratio:
            best_ratio, best_val = r, val
    if best_ratio >= threshold:
        return best_val
    log.debug("No fuzzy match for '%s' (best=%.2f)", name, best_ratio)
    return None


# ── Early-season FIP blending ─────────────────────────────────────────────────

def blended_fip(
    fip_curr: float | None,
    ip_curr:  float | None,
    fip_prev: float | None,
) -> float | None:
    """
    Korai szezon (<IP_MIN innings): az előző szezon FIP-jével súlyozunk.
    w = ip_curr / (ip_curr + REGRESSION_IP)
    blended = w × fip_curr + (1-w) × fip_ref
    """
    if fip_curr is None:
        return None
    ip = ip_curr or 0.0
    if ip >= IP_MIN:
        return fip_curr
    fip_ref = fip_prev if fip_prev is not None else LEAGUE_AVG_FIP
    w = ip / (ip + REGRESSION_IP)
    return round(w * fip_curr + (1 - w) * fip_ref, 2)


# ── Save / load ───────────────────────────────────────────────────────────────

def save_fip(curr: dict, prev: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    prev_vals = {k: v.get("fip") for k, v in prev.items()}
    merged: dict[str, dict] = {}

    for name, c in curr.items():
        fip_c = c.get("fip")
        ip_c  = c.get("ip")
        fip_p = fuzzy_get(name, prev_vals)
        b_fip = blended_fip(fip_c, ip_c, fip_p)

        w = c.get("wins", 0)
        l = c.get("losses", 0)
        merged[name] = {
            "fip":         fip_c,
            "ip":          ip_c,
            "fip_prev":    fip_p,
            "blended_fip": b_fip,
            "gs":          c.get("gs", 0),
            "wins":        w,
            "losses":      l,
            "record":      f"{w}-{l}" if (w + l) > 0 else "",
            "regressed":   (ip_c or 0) < IP_MIN and fip_c is not None,
        }

    out = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "season":        date.today().year,
        "source":        "mlb_stats_api",
        "pitcher_count": len(merged),
        "ip_min":        IP_MIN,
        "regression_ip": REGRESSION_IP,
        "pitchers":      merged,
    }
    FIP_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log.info("FIP data written → %s  (%d pitchers)", FIP_FILE, len(merged))


def load_fip() -> dict[str, float | None]:
    """Returns {name: blended_fip} for use in pipeline."""
    if not FIP_FILE.exists():
        log.warning("FIP file missing — pitcher adjustment disabled")
        return {}
    doc  = json.loads(FIP_FILE.read_text())
    data = doc.get("pitchers", {})
    return {name: v.get("blended_fip") or v.get("fip")
            for name, v in data.items()}


def lookup_fip(pitcher_name: str, fip_map: dict) -> float | None:
    """Fuzzy FIP lookup — use instead of fip_map.get(name)."""
    return fuzzy_get(pitcher_name, fip_map)


def load_records() -> dict[str, str]:
    """Returns {pitcher_name: 'W-L'} for all pitchers in cache."""
    if not FIP_FILE.exists():
        return {}
    doc  = json.loads(FIP_FILE.read_text())
    data = doc.get("pitchers", {})
    return {name: v.get("record", "") for name, v in data.items()}


def lookup_record(pitcher_name: str, record_map: dict) -> str:
    """Fuzzy W-L record lookup."""
    return fuzzy_get(pitcher_name, record_map) or ""


def is_stale() -> bool:
    if not FIP_FILE.exists():
        return True
    doc = json.loads(FIP_FILE.read_text())
    ts  = doc.get("generated_at", "")
    if not ts:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days
    return age >= CACHE_DAYS


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Check cache staleness only")
    parser.add_argument("--force", action="store_true",
                        help="Force refresh even if fresh")
    args = parser.parse_args()

    if args.check:
        stale = is_stale()
        print("STALE" if stale else "FRESH")
        raise SystemExit(1 if stale else 0)

    if not args.force and not is_stale():
        log.info("FIP cache fresh (<%d days) — skipping", CACHE_DAYS)
        return

    season = date.today().year
    curr   = fetch_pitching_stats(season)
    prev   = fetch_pitching_stats(season - 1)
    save_fip(curr, prev)
    log.info("Done. %d pitchers cached.", len(curr))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
