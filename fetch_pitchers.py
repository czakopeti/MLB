"""
fetch_pitchers.py
FanGraphs FIP + early-season blending + fuzzy name matching.

Stratégia:
  - Aktuális szezon FIP letöltése pybaseball-lal
  - Ha egy dobónak < IP_MIN innings van a folyó szezonban:
      blended_FIP = w_curr * fip_curr + (1 - w_curr) * fip_prev
      ahol w_curr = curr_IP / (curr_IP + REGRESSION_IP)
  - Az előző szezon FIP-je is letöltődik referenciaként
  - Névegyeztetés: SequenceMatcher fuzzy matching, 0.82 küszöb
"""

import argparse
import json
import logging
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

DATA_DIR   = Path("data")
FIP_FILE   = DATA_DIR / "pitchers_fip.json"
CACHE_DAYS = 7

# Szezon elején alacsony IP esetén visszaregredálunk az előző szezon FIP-jéhez
IP_MIN        = 30.0   # innings alatt számít "korai szezonnak"
REGRESSION_IP = 40.0   # ennyi "virtuális" innings az előző szezon FIP-je felé húz
LEAGUE_AVG_FIP = 4.15  # ha előző szezon sincs, erre regredálunk

FUZZY_THRESHOLD = 0.82  # SequenceMatcher ratio küszöb

log = logging.getLogger(__name__)


# ── Fuzzy name matching ───────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, no periods, no accents for comparison."""
    return (name.lower()
               .replace(".", "")
               .replace("-", " ")
               .replace("'", ""))


def fuzzy_get(name: str, lookup: dict, threshold: float = FUZZY_THRESHOLD):
    """
    Look up `name` in `lookup` dict with fuzzy matching.
    Returns value or None if no match above threshold.
    """
    if not name or not lookup:
        return None

    norm_target = _normalize(name)
    best_ratio  = 0.0
    best_val    = None

    for key, val in lookup.items():
        ratio = SequenceMatcher(None, norm_target, _normalize(key)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_val   = val

    if best_ratio >= threshold:
        return best_val

    log.debug("No fuzzy match for '%s' (best ratio=%.2f)", name, best_ratio)
    return None


# ── pybaseball data fetch ─────────────────────────────────────────────────────

def fetch_season_fip(season: int, min_ip: float = 5.0) -> dict[str, dict]:
    """
    Download FanGraphs pitching stats for a season via pybaseball.
    Returns {pitcher_name: {"fip", "xfip", "siera", "ip", "gs"}}
    """
    from pybaseball import pitching_stats

    log.info("Downloading FanGraphs pitching stats for %d (min IP=%.0f) …", season, min_ip)
    df = pitching_stats(season, qual=int(min_ip))

    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        result[name] = {
            "fip":   _sf(row.get("FIP")),
            "xfip":  _sf(row.get("xFIP")),
            "siera": _sf(row.get("SIERA")),
            "ip":    _sf(row.get("IP")),
            "gs":    int(row.get("GS", 0) or 0),
        }

    log.info("  %d pitchers loaded for %d", len(result), season)
    return result


def _sf(val) -> float | None:
    try:
        v = float(val)
        return round(v, 2) if v == v else None
    except (TypeError, ValueError):
        return None


# ── Early-season FIP blending ─────────────────────────────────────────────────

def blended_fip(
    fip_curr: float | None,
    ip_curr: float | None,
    fip_prev: float | None,
) -> float | None:
    """
    If the pitcher has fewer than IP_MIN innings this season,
    blend their current FIP with the previous year's FIP.
    More innings this season → less regression.

    Formula:
      w = ip_curr / (ip_curr + REGRESSION_IP)
      blended = w * fip_curr + (1-w) * fip_ref
    where fip_ref = fip_prev if available, else LEAGUE_AVG_FIP.
    """
    if fip_curr is None:
        return None

    ip = ip_curr or 0.0
    if ip >= IP_MIN:
        return fip_curr          # enough data, trust current season

    fip_ref = fip_prev if fip_prev is not None else LEAGUE_AVG_FIP
    w = ip / (ip + REGRESSION_IP)
    blended = w * fip_curr + (1 - w) * fip_ref
    return round(blended, 2)


# ── Public API ────────────────────────────────────────────────────────────────

def load_fip() -> dict[str, float | None]:
    """
    Returns {pitcher_name: blended_fip} for use by main.py.
    Uses fuzzy matching keys — call fuzzy_get() on the returned dict.
    """
    if not FIP_FILE.exists():
        log.warning("FIP file missing — pitcher adjustment disabled")
        return {}
    doc  = json.loads(FIP_FILE.read_text())
    data = doc.get("pitchers", {})
    return {name: v.get("blended_fip") or v.get("fip")
            for name, v in data.items()}


def lookup_fip(pitcher_name: str, fip_map: dict) -> float | None:
    """Fuzzy lookup — use this instead of fip_map.get(pitcher_name)."""
    return fuzzy_get(pitcher_name, fip_map)


def is_stale() -> bool:
    if not FIP_FILE.exists():
        return True
    doc = json.loads(FIP_FILE.read_text())
    ts  = doc.get("generated_at", "")
    if not ts:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days
    return age >= CACHE_DAYS


# ── Save ──────────────────────────────────────────────────────────────────────

def save_fip(curr: dict, prev: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    merged: dict[str, dict] = {}

    # Build full name set (union of both seasons)
    all_names = set(curr) | set(prev)
    prev_fuzzy = prev   # use fuzzy_get for cross-season lookup

    for name in all_names:
        c = curr.get(name, {})
        p_match = fuzzy_get(name, {k: v for k, v in prev_fuzzy.items()}) or {}

        fip_c  = c.get("fip")
        ip_c   = c.get("ip")
        fip_p  = p_match.get("fip") if isinstance(p_match, dict) else None
        b_fip  = blended_fip(fip_c, ip_c, fip_p)

        merged[name] = {
            "fip":         fip_c,
            "ip":          ip_c,
            "fip_prev":    fip_p,
            "blended_fip": b_fip,
            "xfip":        c.get("xfip"),
            "siera":       c.get("siera"),
            "gs":          c.get("gs", 0),
            "regressed":   (ip_c or 0) < IP_MIN and fip_c is not None,
        }

    out = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "season":        date.today().year,
        "pitcher_count": len(merged),
        "ip_min":        IP_MIN,
        "regression_ip": REGRESSION_IP,
        "pitchers":      merged,
    }
    FIP_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log.info("FIP data written → %s  (%d pitchers, prev_season blending active)",
             FIP_FILE, len(merged))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.check:
        stale = is_stale()
        print("STALE" if stale else "FRESH")
        raise SystemExit(1 if stale else 0)

    if not args.force and not is_stale():
        log.info("FIP cache fresh — skipping")
        return

    season = date.today().year
    curr   = fetch_season_fip(season, min_ip=5.0)
    prev   = fetch_season_fip(season - 1, min_ip=20.0)
    save_fip(curr, prev)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    main()
