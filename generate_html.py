"""
generate_html.py — MLB Value Dashboard
Games sorted by start time. No AL/NL split.
"""
import json, logging
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR   = Path("data")
GAMES_FILE = DATA_DIR / "todays_games.json"
OUT_HTML   = Path("index.html")
log = logging.getLogger(__name__)

def to_hu_time(iso_utc):
    if not iso_utc: return "—"
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        off = timedelta(hours=3) if 3 <= dt.month <= 10 else timedelta(hours=2)
        return (dt + off).strftime("%H:%M")
    except: return "—"

def american_to_decimal(v):
    """Convert American odds to European decimal format."""
    if v is None: return None
    if v > 0: return round(v/100 + 1, 2)
    return round(100/abs(v) + 1, 2)

def fmt_odds(v):
    d = american_to_decimal(v)
    return f"{d:.2f}" if d is not None else "—"
def fmt_pct(v):  return f"{v*100:.1f}%" if v is not None else "—"
def fmt_edge(v): return f"{'+' if (v or 0) >= 0 else ''}{(v or 0)*100:.1f}%" if v is not None else "—"
def edge_cls(v): return "ep" if (v or 0) > 0 else ("en" if (v or 0) < 0 else "eu")
def bar(v):      return round(min(max((v or 0.5), 0), 1) * 100)

def fip_cls(v):
    if v is None: return "fa"
    if v < 3.3:   return "fe"
    if v < 3.9:   return "fg"
    if v < 4.5:   return "fa"
    return "fp"

def fip_str(v): return f"{v:.2f}" if v is not None else "—"

def signal_html(g):
    sig  = g.get("signal","none")
    side = g.get("bet_side","")
    abbr = g["home_abbr"] if side=="home" else g["away_abbr"]
    if sig=="bet": return f'<span class="bv">🎯 fogadj</span><br><span class="bsub">{abbr} ML</span>'
    if sig in ("no_odds","no_model"): return f'<span class="bn">{sig.replace("_"," ")}</span>'
    return '<span class="bn">—</span>'

def kelly_html(g):
    if g.get("signal")=="value" and g.get("kelly_stake"):
        return f'<span class="kv">{g["kelly_stake"]*100:.1f}%</span><span class="ks">bankroll</span>'
    return '<span class="kn">—</span>'

def extra_tags(g):
    t = []
    if g.get("bullpen_flag_home"): t.append(f'<span class="tb">⚡ {g["home_abbr"]}</span>')
    if g.get("bullpen_flag_away"): t.append(f'<span class="tb">⚡ {g["away_abbr"]}</span>')
    if g.get("park_flag"):         t.append(f'<span class="tp">🏟 {g.get("venue","")}</span>')
    return '<div class="tags">'+"".join(t)+"</div>" if t else ""

def pbars(a_lbl, h_lbl, a_p, h_p, cls):
    return (
        f'<div class="pbs">'
        f'<div class="pr"><span class="pl">{a_lbl}</span>'
        f'<div class="bw"><div class="b {cls}" style="width:{bar(a_p)}%"></div></div>'
        f'<span class="pn">{fmt_pct(a_p)}</span></div>'
        f'<div class="pr"><span class="pl">{h_lbl}</span>'
        f'<div class="bw"><div class="b {cls}" style="width:{bar(h_p)}%"></div></div>'
        f'<span class="pn">{fmt_pct(h_p)}</span></div>'
        f'</div>'
    )

def odds_cell(g):
    aa,ha = g["away_abbr"],g["home_abbr"]
    ao,ho = g.get("pinnacle_away_odds"),g.get("pinnacle_home_odds")
    anv,hnv = g.get("no_vig_prob_away"),g.get("no_vig_prob_home")
    # Decimal: > 2.00 means underdog (was positive American), highlight green
    oc = lambda v: "op" if (american_to_decimal(v) or 0) > 2.0 else "on"
    return (
        f'<div class="opr">'
        f'<div class="or"><span class="ol">{aa}</span><span class="ov {oc(ao)}">{fmt_odds(ao)}</span>'
        f'<span class="onv">{fmt_pct(anv)}</span></div>'
        f'<div class="or"><span class="ol">{ha}</span><span class="ov {oc(ho)}">{fmt_odds(ho)}</span>'
        f'<span class="onv">{fmt_pct(hnv)}</span></div>'
        f'</div>'
    )

def pitcher_line(name, fip, record):
    n = name or "TBD"
    mu = ' style="color:var(--mu)"' if n=="TBD" else ""
    rec = f'<span class="prec">{record}</span>' if record else ""
    return (
        f'<div class="pl2">'
        f'<span class="pnm"{mu}>{n}</span>'
        f'{rec}'
        f'<span class="fipb {fip_cls(fip)}">{fip_str(fip)}</span>'
        f'</div>'
    )

def game_row(g):
    sig    = g.get("signal","none")
    ha,aa  = g["home_abbr"],g["away_abbr"]
    hn,an  = g["home_name"],g["away_name"]
    hp     = g.get("home_pitcher",{})
    ap     = g.get("away_pitcher",{})
    eh,ea  = g.get("edge_home"),g.get("edge_away")
    rc     = "rv" if sig=="bet" else "rn"
    t      = to_hu_time(g.get("game_date",""))
    return f"""    <tr class="{rc}">
      <td class="cm">
        <div class="gt">{t}</div>
        <div class="tr2"><span class="ta">{aa}</span><span class="tn">{an}</span></div>
        <div class="at">@</div>
        <div class="tr2"><span class="ta">{ha}</span><span class="tn">{hn}</span></div>
        {extra_tags(g)}
      </td>
      <td class="cp">
        {pitcher_line(ap.get("name",""),g.get("away_fip"),ap.get("record",""))}
        {pitcher_line(hp.get("name",""),g.get("home_fip"),hp.get("record",""))}
      </td>
      <td>{pbars(aa,ha,g.get("model_prob_away"),g.get("model_prob_home"),"bm")}</td>
      <td>{pbars(aa,ha,g.get("no_vig_prob_away"),g.get("no_vig_prob_home"),"bk")}</td>
      <td class="co">{odds_cell(g)}</td>
      <td class="ce">
        <span class="en {edge_cls(ea)}">{fmt_edge(ea)}</span><span class="es">{aa}</span>
        <span class="en {edge_cls(eh)}">{fmt_edge(eh)}</span><span class="es">{ha}</span>
      </td>
      <td class="cs">{signal_html(g)}</td>
      <td class="ck">{kelly_html(g)}</td>
    </tr>"""

CSS = """<style>
:root{--bg:#0b1525;--sur:#111e33;--brd:#1e2f4a;--brd2:#243651;
  --tx:#c8d8f0;--mu:#5a7499;--acc:#3b82f6;--acc2:#60a5fa;
  --gn:#22c55e;--rd:#ef4444;--am:#f59e0b;--amd:#431407;--rdd:#450a0a;
  --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:14px;min-height:100vh;}
.topbar{position:sticky;top:0;z-index:100;}
header{background:#0a1220ee;backdrop-filter:blur(12px);
  border-bottom:1px solid var(--brd);padding:10px 16px;display:flex;
  align-items:center;justify-content:space-between;gap:12px;}
.hl{display:flex;align-items:center;gap:10px;}
.hico{width:26px;height:26px;background:var(--acc);border-radius:5px;
  display:flex;align-items:center;justify-content:center;font-size:14px;}
h1{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.05em;color:#fff;}
.hm{font-family:var(--mono);font-size:11px;color:var(--mu);white-space:nowrap;}
.ld{display:inline-block;width:6px;height:6px;background:var(--gn);border-radius:50%;
  margin-right:5px;animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.legend{display:flex;gap:12px;padding:7px 16px;flex-wrap:wrap;
  border-bottom:1px solid var(--brd);background:var(--sur);}
.li{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--mu);font-family:var(--mono);}
.summary{display:flex;gap:16px;padding:7px 16px;
  border-bottom:1px solid var(--brd);background:var(--sur);flex-wrap:wrap;}
.si{display:flex;flex-direction:column;}
.sv{font-family:var(--mono);font-size:17px;font-weight:600;line-height:1.2;}
.sl{font-family:var(--mono);font-size:10px;color:var(--mu);text-transform:uppercase;letter-spacing:.05em;}
.sg{color:var(--gn)}.sr{color:var(--rd)}.sb{color:var(--acc2)}.sa{color:var(--am)}
.bv{display:inline-flex;align-items:center;padding:1px 6px;border-radius:3px;
  font-size:10px;font-family:var(--mono);font-weight:600;
  background:#1e3a1e;color:var(--gn);border:1px solid #2d5a2d;}
.bn{display:inline-flex;align-items:center;padding:1px 6px;border-radius:3px;
  font-size:10px;font-family:var(--mono);font-weight:600;
  background:#111;color:var(--mu);border:1px solid var(--brd);}
.bsub{font-family:var(--mono);font-size:9px;color:var(--mu);margin-top:2px;}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;min-width:700px;}
thead th{padding:7px 9px;text-align:left;font-family:var(--mono);font-size:10px;
  font-weight:500;letter-spacing:.08em;color:var(--mu);text-transform:uppercase;
  border-bottom:1px solid var(--brd2);background:var(--sur);white-space:nowrap;
  z-index:80;}
thead th.rc{text-align:right;}thead th.cc{text-align:center;}
tbody tr{border-bottom:1px solid var(--brd);transition:background .1s;}
tbody tr:hover{background:#132035;}
tr.rv{border-left:3px solid var(--gn);}
tr.rf{border-left:3px solid var(--rd);}
tr.rn{border-left:3px solid transparent;}
td{padding:8px 9px;vertical-align:middle;white-space:nowrap;}
.cm{min-width:155px;}
.gt{font-family:var(--mono);font-size:11px;color:var(--mu);margin-bottom:3px;}
.tr2{display:flex;align-items:center;gap:5px;line-height:1.75;}
.ta{font-family:var(--mono);font-size:12px;font-weight:600;color:#e2eeff;width:30px;}
.tn{font-size:11px;color:var(--mu);}
.at{font-family:var(--mono);font-size:9px;color:var(--brd2);}
.tags{display:flex;gap:3px;flex-wrap:wrap;margin-top:3px;}
.tag{font-size:9px;font-family:var(--mono);padding:0 4px;border-radius:2px;border:1px solid;}
.tb{color:var(--am);border-color:#78350f;background:var(--amd);}
.tp{color:#86efac;border-color:#166534;background:#052e16;}
.cp{min-width:175px;max-width:200px;}
.pl2{display:flex;align-items:center;gap:4px;
  margin-bottom:4px;line-height:1;}
.pl2:last-child{margin-bottom:0;}
.pnm{font-size:12px;color:var(--tx);flex:1;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.prec{font-family:var(--mono);font-size:10px;color:var(--mu);flex-shrink:0;white-space:nowrap;}
.fipb{font-family:var(--mono);font-size:11px;font-weight:600;
  padding:1px 4px;border-radius:3px;flex-shrink:0;}
.fe{color:#4ade80;background:#052e16;}
.fg{color:#86efac;background:#052e16;}
.fa{color:var(--mu);background:#1a1a2e;}
.fp{color:#fca5a5;background:#450a0a;}
.pbs{display:flex;flex-direction:column;gap:2px;min-width:100px;}
.pr{display:flex;align-items:center;gap:5px;}
.pl{font-family:var(--mono);font-size:9px;color:var(--mu);width:26px;}
.bw{flex:1;height:4px;background:var(--brd2);border-radius:2px;overflow:hidden;}
.b{height:100%;border-radius:2px;}
.bm{background:var(--acc)}.bk{background:var(--mu);}
.pn{font-family:var(--mono);font-size:11px;font-weight:500;
  color:#e2eeff;width:38px;text-align:right;}
.co{min-width:95px;}
.opr{display:flex;flex-direction:column;gap:2px;}
.or{display:flex;align-items:center;gap:4px;line-height:1.85;}
.ol{font-family:var(--mono);font-size:9px;color:var(--mu);width:26px;}
.ov{font-family:var(--mono);font-size:12px;font-weight:600;}
.op{color:var(--gn)}.on{color:#93c5fd;}
.onv{font-family:var(--mono);font-size:10px;color:var(--mu);}
.ce{min-width:75px;text-align:right;}
.en{font-family:var(--mono);font-size:13px;font-weight:600;display:block;}
.ep{color:var(--gn)}.en2{color:var(--rd)}.eu{color:var(--mu);}
.es{font-family:var(--mono);font-size:9px;color:var(--mu);display:block;}
.cs{text-align:center;min-width:85px;}
.ck{text-align:right;min-width:65px;}
.kv{font-family:var(--mono);font-size:12px;font-weight:600;color:var(--gn);display:block;}
.ks{font-family:var(--mono);font-size:9px;color:var(--mu);}
.kn{font-family:var(--mono);font-size:11px;color:var(--brd2);}
footer{padding:10px 16px;font-family:var(--mono);font-size:10px;
  color:var(--mu);border-top:1px solid var(--brd);line-height:1.8;}
@media(max-width:600px){h1{font-size:12px;}.hm{display:none;}td{padding:6px 7px;}}
</style>"""

TH = """<thead><tr>
  <th>Mérkőzés</th><th>Dobó · W-L · FIP</th>
  <th>Modell prob</th><th>Pinnacle prob</th><th>Pinnacle odds (dec.)</th>
  <th class="rc">Edge</th><th class="cc">Szignál</th><th class="rc">Kelly tét</th>
</tr></thead>"""


def render(doc):
    games         = sorted(doc["games"], key=lambda g: g.get("game_date",""))
    elo_last_date = doc.get("elo_last_date", "—")
    elo_games     = doc.get("elo_games_processed", 0)
    ts       = doc.get("generated_at","")[:16].replace("T"," ")
    date_str = doc.get("date","")
    n        = len(games)

    cnt = {"bet":0,"bull":0}
    for g in games:
        s = g.get("signal","")
        if s=="bet": cnt["bet"]+=1
        if g.get("bullpen_flag_home") or g.get("bullpen_flag_away"): cnt["bull"]+=1

    ve = [abs(g.get("edge_home",0) or 0) if g.get("bet_side")=="home"
          else abs(g.get("edge_away",0) or 0)
          for g in games if g.get("signal")=="bet"]
    avg = f"{sum(ve)/len(ve)*100:.1f}%" if ve else "—"

    rows = "\n".join(game_row(g) for g in games)

    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MLB Value Dashboard — {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
{CSS}
</head><body>

<div class="topbar">
<header>
  <div class="hl"><div class="hico">⚾</div><h1>MLB VALUE DASHBOARD</h1></div>
  <div class="hm"><span class="ld"></span>{date_str} · frissítve {ts} UTC · {n} mérkőzés</div>
</header>
<div class="legend">
  <div class="li"><span class="bv">🎯 fogadj</span> jobb dobó (W-L + FIP) + pozitív edge</div>
  <div class="li">⚡ bullpen &gt;200 pitch/3 nap &nbsp;·&nbsp; 🏟 hitter-barát pálya</div>
</div>
<div class="summary">
  <div class="si"><span class="sv sb">{n}</span><span class="sl">mérkőzés</span></div>
  <div class="si"><span class="sv sg">{cnt['bet']}</span><span class="sl">fogadás</span></div>
  <div class="si"><span class="sv sa">{cnt['bull']}</span><span class="sl">bullpen flag</span></div>
  <div class="si"><span class="sv sg">{avg}</span><span class="sl">avg edge (value)</span></div>
</div>
</div>

<div class="tw"><table>{TH}<tbody>
{rows}
</tbody></table></div>

<footer>
  <div>Modell: saját MLB Elo (K=4, HFA=+24, ⅓ regresszió) + FIP pitcher adj · Odds: Pinnacle no-vig · Edge: 4–10% = value / &gt;10% = kizárás</div>
  <div style="margin-top:4px;color:#3b5a7a">
    Frissítve: {ts} UTC · Napi cron: 11:00 + 14:00 UTC · Nem fogadási tanácsadás.
  </div>
  <div style="margin-top:4px;display:flex;align-items:center;gap:8px;">
    <span style="color:#1e3a1e;background:#0d2010;border:1px solid #2d5a2d;
      border-radius:3px;padding:1px 7px;font-size:10px;letter-spacing:.04em;">
      ✓ Elo — {elo_last_date} meccsek feldolgozva · {elo_games:,} mérkőzés összesen
    </span>
  </div>
</footer>

</body></html>"""


def main():
    doc  = json.loads(GAMES_FILE.read_text())
    html = render(doc)
    OUT_HTML.write_text(html, encoding="utf-8")
    log.info("HTML → %s  (%d bytes)", OUT_HTML, len(html))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    main()
