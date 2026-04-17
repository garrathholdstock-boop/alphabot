"""
app/dashboard.py — AlphaBot Web Dashboard (FastAPI)
Runs as a standalone uvicorn service on port 8080.
Independent of the main bot process — never deadlocks.
"""

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import json, sqlite3, html as html_module
from datetime import datetime, date, timedelta
from urllib.parse import parse_qs

from core.config import (
    log, IS_LIVE, USE_BINANCE, BINANCE_USE_TESTNET,
    PORT, DASH_TOKEN, KILL_PIN, DASH_USER, DASH_PASS,
    MIN_SIGNAL_SCORE, LOSS_STREAK_LIMIT,
    STOP_LOSS_PCT, TRAILING_STOP_PCT, TAKE_PROFIT_PCT, MAX_HOLD_DAYS, GAP_DOWN_PCT,
    MAX_DAILY_LOSS, MAX_DAILY_SPEND, MAX_PORTFOLIO_EXPOSURE, DAILY_PROFIT_TARGET,
    MAX_TRADE_VALUE, MAX_TOTAL_POSITIONS,
    VIX_HIGH_THRESHOLD, VIX_EXTREME, VIX_FEAR_THRESHOLD, BTC_CRASH_PCT,
    BEAR_TICKERS,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    asx_state, ftse_state,
    global_risk, perf, kill_switch, circuit_breaker,
    market_regime, crypto_regime, asx_regime, ftse_regime, news_state, smallcap_pool,
    exchange_stops, account_info, near_miss_tracker,
    CRYPTO_WATCHLIST, DB_PATH,
    _state_lock,
)
import core.config as cfg
from core.risk import (
    total_exposure, all_positions_count, calc_profit_factor, calc_sharpe,
    vol_adjusted_size, is_market_open, is_intraday_window,
)
from core.execution import place_order, cancel_stop_order_ibkr
from data.analytics import score_signal
from data.database import (
    db_get_leaderboard, db_search_symbol, db_get_skip_reason_breakdown,
    db_get_reports, db_get_report_by_id,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

app = FastAPI()
PARIS = ZoneInfo("Europe/Paris")


# ═══════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════
def _db_pnl_for_period(since_iso, until_iso=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        if until_iso:
            r = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) "
                "FROM trades WHERE side='SELL' AND created_at >= ? AND created_at < ?",
                (since_iso, until_iso)
            ).fetchone()
        else:
            r = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) "
                "FROM trades WHERE side='SELL' AND created_at >= ?",
                (since_iso,)
            ).fetchone()
        conn.close()
        return (r[0] or 0, r[1] or 0.0, r[2] or 0)
    except:
        return (0, 0.0, 0)

def _db_today_pnl():
    t, pnl, w = _db_pnl_for_period(date.today().isoformat())
    return pnl

def _db_all_time_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END), "
            "COALESCE(AVG(score),0) FROM trades WHERE side='SELL'"
        ).fetchone()
        conn.close()
        return (r[0] or 0, r[1] or 0.0, r[2] or 0, r[3] or 0, r[4] or 0.0)
    except:
        return (0, 0.0, 0, 0, 0.0)

def _db_recent_trades(limit=10):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT symbol, pnl, side, created_at, score, qty, price, hold_hours, market FROM trades "
            "WHERE side='SELL' ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return rows
    except:
        return []



# ═══════════════════════════════════════════════════════════════
# STATUS SNAPSHOT LOADER (reads from bot process via file)
# ═══════════════════════════════════════════════════════════════
_status_cache = {}

def _load_status():
    """Load status.json written by bot process every cycle."""
    global _status_cache
    try:
        with open("/home/alphabot/app/status.json") as f:
            _status_cache = json.load(f)
    except:
        pass
    return _status_cache

def _st(market):
    """Get state dict for a market from status snapshot."""
    return _load_status().get("states", {}).get(market, {})


# ═══════════════════════════════════════════════════════════════
# SHARED CSS + FONTS
# ═══════════════════════════════════════════════════════════════
BASE_CSS = """
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px}
body{background:#090b0e;color:#e0e0e0;font-family:'JetBrains Mono',monospace;font-size:15px;line-height:1.5}
.header{background:#0d1117;border-bottom:1px solid #1a2a1a;padding:18px 28px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;flex-wrap:wrap;gap:12px}
.logo{font-family:'Syne',sans-serif;font-size:24px;font-weight:800;color:#00ff88}
.logo span{color:#475569}
.badge{padding:4px 12px;border-radius:5px;font-size:12px;font-weight:700;letter-spacing:1px}
.badge-paper{background:rgba(255,204,0,0.1);color:#ffcc00;border:1px solid rgba(255,204,0,0.3)}
.badge-live{background:rgba(255,68,102,0.1);color:#ff4466;border:1px solid rgba(255,68,102,0.3)}
.container{padding:24px;max-width:1200px;margin:0 auto}
.grid5{display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.card{background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.07);border-radius:14px;padding:20px 22px;margin-bottom:0}
.card-green{border-color:rgba(0,255,136,0.2)}
.card-blue{border-color:rgba(0,170,255,0.2)}
.lbl{font-size:11px;letter-spacing:2px;color:#475569;text-transform:uppercase;margin-bottom:6px}
.big{font-size:26px;font-weight:700;font-family:'Syne',sans-serif}
.green{color:#00ff88}.blue{color:#00aaff}.red{color:#ff4466}.gold{color:#ffcc00}.grey{color:#475569}
.section-title{font-size:17px;font-weight:700;margin-bottom:16px;font-family:'Syne',sans-serif}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-size:11px;color:#475569;letter-spacing:1.5px;text-transform:uppercase;padding:12px 14px;text-align:left;font-weight:600}
td{padding:11px 14px;border-top:1px solid rgba(255,255,255,0.04);font-family:'JetBrains Mono',monospace}
tr:hover td{background:rgba(255,255,255,0.025)}
.sig-buy{background:rgba(0,255,136,0.1);color:#00ff88;border:1px solid #00ff88;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700}
.sig-sell{background:rgba(255,68,102,0.1);color:#ff4466;border:1px solid #ff4466;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700}
.sig-hold{background:rgba(255,255,255,0.05);color:#475569;border:1px solid #333;padding:3px 10px;border-radius:5px;font-size:12px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
.dot-green{background:#00ff88;box-shadow:0 0 6px #00ff88}
.dot-red{background:#ff4466;box-shadow:0 0 6px #ff4466}
.dot-gold{background:#ffcc00;box-shadow:0 0 6px #ffcc00}
.dot-amber{background:#ffaa00;box-shadow:0 0 6px #ffaa00}
.dot-purple{background:#cc88ff;box-shadow:0 0 6px #cc88ff}
.tab-bar{display:flex;border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:20px;flex-wrap:wrap}
.tab{padding:12px 18px;cursor:pointer;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#475569;border-bottom:2px solid transparent;text-decoration:none}
.tab:hover{color:#e0e0e0}
.empty{text-align:center;padding:50px;color:#333;font-size:16px}
.scan-panel{display:none}.scan-panel.active{display:block}
.controls-bar{background:#0d1117;border-bottom:1px solid rgba(255,255,255,0.06);padding:10px 28px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:73px;z-index:99}
.ctrl-btn{padding:8px 18px;border-radius:7px;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:1px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
@media(max-width:900px){
  .grid5{grid-template-columns:1fr 1fr}
  .grid3{grid-template-columns:1fr}
  .grid2{grid-template-columns:1fr}
  .container{padding:12px}
  .header{padding:12px 16px}
  table{min-width:500px;font-size:13px}
  th,td{padding:8px 10px}
}
@media(max-width:500px){
  .big{font-size:20px}
  .grid5{grid-template-columns:1fr 1fr}
  .grid2{grid-template-columns:1fr}
  .container{padding:8px}
  .header{padding:10px 12px;gap:8px}
  .logo{font-size:20px}
  .controls-bar{padding:8px 12px;gap:8px;top:60px}
  .ctrl-btn{padding:6px 12px;font-size:11px}
  table{min-width:420px;font-size:12px}
  th,td{padding:6px 8px}
  .section-title{font-size:15px}
  .card{padding:14px 14px}
  .tab{padding:10px 12px;font-size:11px}
  .lbl{font-size:10px}
}
</style>"""


# ═══════════════════════════════════════════════════════════════
# BUILD DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════
def build_dashboard():
    # Load live status from bot process (written every cycle via status.json)
    st_data = _load_status()
    st_states = st_data.get("states", {})
    st_regime = st_data.get("market_regime", {})
    st_crypto_regime = st_data.get("crypto_regime", {})
    st_asx_regime = st_data.get("asx_regime", {})
    st_ftse_regime = st_data.get("ftse_regime", {})
    st_account = st_data.get("account", cfg.account_info or {})
    st_kill = st_data.get("kill_switch", kill_switch)
    st_circuit = st_data.get("circuit_breaker", circuit_breaker)
    st_candidates = st_data.get("candidates", {})
    st_global_risk = st_data.get("global_risk", global_risk)
    st_perf = st_data.get("perf", perf)

    acc = st_account
    port_val = float(acc.get("portfolio_value", 1000000)) if acc else 1000000
    portfolio = f"${port_val:,.2f}"
    now_paris = datetime.now(PARIS)
    now_date = now_paris.strftime("%A %d %B %Y")

    # ── P&L helpers ──
    def _fmt(v): return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
    def _col(v): return "#00ff88" if v >= 0 else "#ff4466"
    def _wr(t, w): return f"{int(w/t*100)}%" if t else "—"
    def _pct(pnl, port): return round(pnl/port*100, 2) if port else 0.0
    def _fmtpct(v): return f"+{v:.1f}%" if v>=0 else f"{v:.1f}%"
    def _vs(cur, prev):
        if prev == 0: return "—"
        diff = cur - prev
        return (f'<span style="color:#00ff88;font-size:11px">▲ {abs(diff):.1f}% vs prior</span>'
                if diff >= 0 else f'<span style="color:#ff4466;font-size:11px">▼ {abs(diff):.1f}% vs prior</span>')
    def _dot(st): return "dot-red" if st.get("shutoff") else ("dot-green" if st.get("running") else "dot-gold")
    def _status(st): return "Shut Off" if st.get("shutoff") else ("Running" if st.get("running") else "Idle")
    def _pnl(st): return _fmt(st.get("pnl", 0.0))
    def _pnlc(st): return _col(st.get("pnl", 0.0))

    # ── Period P&L ──
    today_str = date.today().isoformat()
    _dow = date.today().weekday()
    _wk_start = (date.today() - timedelta(days=_dow)).isoformat()
    _lwk_start = (date.today() - timedelta(days=_dow+7)).isoformat()
    _lwk_end = _wk_start
    _mo_start = date.today().replace(day=1).isoformat()
    _lm_d = date.today().replace(day=1) - timedelta(days=1)
    _lm_start = _lm_d.replace(day=1).isoformat()
    _lm_end = _mo_start

    today_pnl = _db_today_pnl()
    tw_t, tw_pnl, tw_w = _db_pnl_for_period(_wk_start)
    lw_t, lw_pnl, lw_w = _db_pnl_for_period(_lwk_start, _lwk_end)
    tm_t, tm_pnl, tm_w = _db_pnl_for_period(_mo_start)
    lm_t, lm_pnl, lm_w = _db_pnl_for_period(_lm_start, _lm_end)
    today_count, _, _ = _db_pnl_for_period(today_str)

    try:
        conn = sqlite3.connect(DB_PATH)
        _7d = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), MAX(pnl), MIN(pnl) "
            "FROM trades WHERE side='SELL' AND created_at >= datetime('now','-7 days')"
        ).fetchone()
        conn.close()
        week_t, week_pnl, week_w = _7d[0] or 0, _7d[1] or 0.0, _7d[2] or 0
        week_best = f"+${_7d[3]:.2f}" if _7d[3] else "—"
        week_worst = f"-${abs(_7d[4]):.2f}" if _7d[4] else "—"
        week_wr = int(week_w/week_t*100) if week_t else 0
    except:
        week_t=week_pnl=week_w=week_wr=0; week_best=week_worst="—"

    tm_pct = _pct(tm_pnl, port_val); lm_pct = _pct(lm_pnl, port_val)
    tw_pct = _pct(tw_pnl, port_val); lw_pct = _pct(lw_pnl, port_val)
    this_month = now_paris.strftime("%B")
    last_month = (now_paris.replace(day=1) - timedelta(days=1)).strftime("%B")
    this_week_lbl = f"Wk {now_paris.strftime('%d %b')} →"

    # ── Stats from DB ──
    total_t, total_pnl_db, wins_db, losses_db, avg_sc_db = _db_all_time_stats()
    win_rate = int(wins_db/total_t*100) if total_t else 0
    wr_color = "#00ff88" if win_rate >= 55 else ("#ffcc00" if win_rate >= 45 else "#ff4466")
    max_dd = round(st_perf.get("max_drawdown", 0.0), 1)
    dd_color = "#00ff88" if max_dd < 5 else ("#ffcc00" if max_dd < 10 else "#ff4466")
    pf = calc_profit_factor()
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
    pf_color = "#00ff88" if pf >= 1.5 else ("#ffcc00" if pf >= 1.0 else "#ff4466")
    sharpe_v = calc_sharpe()
    sharpe_str = f"{sharpe_v:.2f}" if sharpe_v else "—"
    sharpe_color = "#00ff88" if (sharpe_v and sharpe_v >= 1.0) else ("#ffcc00" if (sharpe_v and sharpe_v >= 0.5) else "#888")
    loss_streak = global_risk["loss_streak"]
    streak_color = "#ff4466" if loss_streak >= LOSS_STREAK_LIMIT else ("#ffcc00" if loss_streak >= 2 else "#00ff88")
    vix_v = st_global_risk.get("vix_level")
    vix_str_val = f"{vix_v:.1f}" if vix_v else "—"
    vix_color = "#ff4466" if (vix_v and vix_v >= VIX_EXTREME) else ("#ffcc00" if (vix_v and vix_v >= VIX_HIGH_THRESHOLD) else "#00ff88")
    size_mult = round(vol_adjusted_size(1.0), 2)
    global_pos = all_positions_count()
    pause_until = global_risk.get("paused_until")
    pause_status = pause_until.strftime("%H:%M") if pause_until and datetime.now() < pause_until else "None"

    # ── Market regime ──
    regime = st_regime.get("mode", "BULL")
    c_regime = st_crypto_regime.get("mode", "BULL")
    regime_color = "#00ff88" if regime == "BULL" else "#ff4466"
    c_regime_color = "#00ff88" if c_regime == "BULL" else "#ff4466"
    spy_str = f"${st_regime['spy_price']:.2f}" if st_regime.get("spy_price") else "N/A"
    spy_ma = f"${st_regime['spy_ma20']:.2f}" if st_regime.get("spy_ma20") else "N/A"
    vix_regime = f"{st_regime['vix']:.1f}" if st_regime.get("vix") else "N/A"
    btc_str = f"${st_crypto_regime['btc_price']:.0f}" if st_crypto_regime.get("btc_price") else "N/A"
    btc_chg = st_crypto_regime.get("btc_change")
    btc_chg_str = f"{btc_chg:+.1f}%" if btc_chg is not None else "N/A"
    btc_chg_col = "#ff4466" if btc_chg and btc_chg < -BTC_CRASH_PCT else "#e0e0e0"

    from app.main import is_asx_open, is_ftse_open
    asx_open = is_asx_open(); ftse_open = is_ftse_open()
    market_open = is_market_open()
    asx_mode = st_asx_regime.get("mode","BULL"); ftse_mode = st_ftse_regime.get("mode","BULL")
    asx_col = "#ffaa00" if asx_mode=="BULL" else "#ff4466"
    ftse_col = "#cc88ff" if ftse_mode=="BULL" else "#ff4466"
    asx_cba = f"${st_asx_regime['spy']:.2f}" if st_asx_regime.get("spy") else "N/A"
    ftse_hsba = f"${st_ftse_regime['spy']:.2f}" if st_ftse_regime.get("spy") else "N/A"

    # ── Kill/circuit banners ──
    kill_banner = ""
    if st_kill.get("active"):
        kill_banner = (
            f'<div style="background:rgba(255,68,102,0.15);border:2px solid #ff4466;border-radius:12px;'
            f'padding:16px 22px;margin-bottom:16px;display:flex;align-items:center;gap:14px">'
            f'<span style="font-size:28px">🛑</span>'
            f'<div><div style="font-size:17px;font-weight:700;color:#ff4466">KILL SWITCH ACTIVE — All bots stopped</div>'
            f'<div style="font-size:13px;color:#888;margin-top:3px">{st_kill.get("reason","")} · {st_kill.get("activated_at","")}</div>'
            f'</div></div>'
        )
    circuit_banner = ""
    if st_circuit.get("active"):
        circuit_banner = (
            f'<div style="background:rgba(255,68,102,0.12);border:2px solid #ff4466;border-radius:12px;'
            f'padding:16px 22px;margin-bottom:16px;display:flex;align-items:center;gap:14px">'
            f'<span style="font-size:28px">🚨</span>'
            f'<div><div style="font-size:17px;font-weight:700;color:#ff4466">CIRCUIT BREAKER — All buys paused</div>'
            f'<div style="font-size:13px;color:#888;margin-top:3px">{st_circuit.get("reason","")}</div>'
            f'</div></div>'
        )

    # ── Positions table — read from snapshot file (bot is separate process) ──
    _type_colors = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00",
                    "ID":"#aa88ff","CrypID":"#00ff88","ASX":"#ffaa00","FTSE":"#cc88ff"}
    try:
        import json as _json
        with open("/home/alphabot/app/positions.json") as _pf:
            _snap = _json.load(_pf)
        all_pos = [(sym, pos, _type_colors.get(pos.get("_type","Stock"),"#00aaff"), pos.get("_type","Stock"))
                   for sym, pos in _snap.items()]
    except Exception:
        all_pos = []
    if all_pos:
        pos_rows = ""
        for idx,(sym,pos,cat_col,cat) in enumerate(all_pos):
            live = cfg.live_prices.get(sym) or pos.get("highest_price", pos["entry_price"])
            entry = pos["entry_price"]; qty = pos["qty"]
            pnl = (live-entry)*qty; pnl_pct = ((live-entry)/entry)*100
            pos_val = live*qty; pnl_c = "#00ff88" if pnl>=0 else "#ff4466"
            sign = "+" if pnl>=0 else ""
            stop_pct = round(((pos["stop_price"]-entry)/entry)*100,1)
            tp_price = pos.get("take_profit_price", entry*1.10)
            target_pct = round(((tp_price-entry)/entry)*100,1)
            score = pos.get("signal_score","—")
            breakdown = pos.get("entry_breakdown","")
            bd_html = (f'<div style="font-size:12px;color:#888;margin-top:10px;white-space:pre-wrap;'
                       f'border-top:1px solid rgba(255,255,255,0.06);padding-top:10px">{breakdown}</div>'
                       if breakdown else "")
            try:
                dt = datetime.fromisoformat(pos.get("entry_ts",""))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                dt_p = dt.astimezone(PARIS)
                purchased = dt_p.strftime("%d %b %H:%M")
                held = datetime.now(PARIS) - dt_p
                hh = int(held.total_seconds()//3600); hm = int((held.total_seconds()%3600)//60)
                entry_dt = f"{hh//24}d {hh%24}h" if hh>=24 else f"{hh}h {hm}m"
            except:
                purchased = pos.get("entry_date","—"); entry_dt = "—"
            pos_rows += (
                f'<tr onclick="toggleDetail({idx})" style="cursor:pointer">'
                f'<td style="font-weight:700;color:{cat_col}">{sym}</td>'
                f'<td><span style="font-size:11px;color:{cat_col};font-weight:700">{cat}</span></td>'
                f'<td style="color:#475569">{entry_dt}</td>'
                f'<td style="color:#777">{purchased}</td>'
                f'<td>${entry:.4f}</td>'
                f'<td style="color:#00aaff">${live:.4f}</td>'
                f'<td style="color:#ff4466">${pos["stop_price"]:.4f} ({stop_pct:+.1f}%)</td>'
                f'<td>${pos_val:,.0f}</td>'
                f'<td style="color:{pnl_c};font-weight:700">{sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)</td>'
                f'</tr>'
                f'<tr id="det-{idx}" style="display:none;background:rgba(255,255,255,0.02)">'
                f'<td colspan="9" style="padding:14px 18px">'
                f'<div style="display:flex;flex-wrap:wrap;gap:18px;font-size:13px;color:#aaa">'
                f'<span><span style="color:#475569">Score </span><b style="color:#ffcc00">{score}/10</b></span>'
                f'<span><span style="color:#475569">Qty </span><b>{qty:,}</b></span>'
                f'<span><span style="color:#475569">Entry </span><b>${entry:.4f}</b></span>'
                f'<span><span style="color:#475569">Live </span><b style="color:#00aaff">${live:.4f}</b></span>'
                f'<span><span style="color:#475569">Stop </span><b style="color:#ff4466">${pos["stop_price"]:.4f} ({stop_pct:+.1f}%)</b></span>'
                f'<span><span style="color:#475569">Target </span><b style="color:#00ff88">${tp_price:.4f} (+{target_pct:.1f}%)</b></span>'
                f'<span><span style="color:#475569">P&L </span><b style="color:{pnl_c}">{sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)</b></span>'
                f'</div>{bd_html}</td></tr>'
            )
        positions_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<div class="section-title">Open Positions ({len(all_pos)}) <span style="font-size:13px;color:#475569;font-weight:400;font-family:\'JetBrains Mono\'">· tap to expand</span></div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Type</th><th>Held</th><th>Purchased</th>'
            f'<th>Entry $</th><th>Live $</th><th>Stop</th><th>Position $</th><th>P&L</th>'
            f'</tr></thead><tbody>{pos_rows}</tbody></table></div></div>'
            f'<script>function toggleDetail(i){{var r=document.getElementById("det-"+i);r.style.display=r.style.display==="none"?"table-row":"none";}}</script>'
        )
    else:
        positions_html = f'<div class="card" style="margin-bottom:16px"><div class="empty">No open positions</div></div>'

    # ── Recent trades from DB ──
    db_trades = _db_recent_trades(10)
    if db_trades:
        trade_rows = ""
        for row in db_trades:
            sym,pnl,side,ts,score = row[0],row[1],row[2],row[3],row[4]
            qty    = row[5] if len(row) > 5 else None
            price  = row[6] if len(row) > 6 else None
            hold_h = row[7] if len(row) > 7 else None
            market = row[8] if len(row) > 8 else "—"
            pc = "#00ff88" if pnl>=0 else "#ff4466"; sign = "+" if pnl>=0 else ""
            # Date + time
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None: dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                dt_p = dt.astimezone(PARIS)
                date_s = dt_p.strftime("%d %b")
                time_s = dt_p.strftime("%H:%M")
            except:
                date_s = ts[:10] if ts else "—"; time_s = ts[11:16] if ts and len(ts)>15 else "—"
            qty_s   = f"{int(qty):,}" if qty else "—"
            price_s = f"${price:.4f}" if price else "—"
            total_s = f"${price*qty:,.0f}" if price and qty else "—"
            hold_s  = f"{hold_h:.1f}h" if hold_h else "—"
            mkt_col = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00","ASX":"#ffaa00","FTSE":"#cc88ff"}.get(market,"#475569")
            trade_rows += (
                f'<tr>'
                f'<td>{"✅" if pnl>0 else "❌"}</td>'
                f'<td style="font-weight:700;color:#00aaff">{sym}</td>'
                f'<td style="color:{mkt_col};font-size:11px;font-weight:700">{market}</td>'
                f'<td style="color:#475569">{date_s}</td>'
                f'<td style="color:#475569">{time_s}</td>'
                f'<td style="color:#777">{price_s}</td>'
                f'<td style="color:#aaa">{qty_s}</td>'
                f'<td style="color:#aaa">{total_s}</td>'
                f'<td style="color:#475569">{hold_s}</td>'
                f'<td style="color:{pc};font-weight:700">{sign}${pnl:.2f}</td>'
                f'<td style="color:#475569">{score or "—"}</td>'
                f'</tr>'
            )
        trades_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<div class="section-title">Recent Trades <span style="font-size:12px;color:#475569;font-weight:400">DB-backed · survives restarts</span></div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th></th><th>Symbol</th><th>Mkt</th><th>Date</th><th>Time</th>'
            f'<th>Entry $</th><th>Qty</th><th>Total $</th><th>Held</th><th>P&L</th><th>Score</th>'
            f'</tr></thead>'
            f'<tbody>{trade_rows}</tbody></table>'
            f'<div style="margin-top:10px;font-size:13px;color:#475569">Total: {total_t} trades · '
            f'<span style="color:{_col(total_pnl_db)}">{_fmt(total_pnl_db)}</span> all-time · '
            f'{win_rate}% win rate</div></div>'
        )
    else:
        trades_html = f'<div class="card" style="margin-bottom:16px"><div class="empty">No completed trades yet — tracking starts when first position closes</div></div>'

    # ── Scan table builder ──
    bear_syms = set(BEAR_TICKERS)
    def build_scan_table(candidates, color):
        if not candidates:
            return '<div class="empty">No scan data yet</div>'
        scored = []
        for c in candidates:
            sc = score_signal(c["symbol"],c["price"],c["change"],c.get("rsi"),c.get("vol_ratio"),c.get("closes",[c["price"]]*22))
            scored.append((sc,c))
        normal = sorted([(sc,c) for sc,c in scored if c["symbol"] not in bear_syms],key=lambda x:-x[0])
        bears  = sorted([(sc,c) for sc,c in scored if c["symbol"] in bear_syms],key=lambda x:-x[0])
        scored = normal + bears
        buys   = sum(1 for sc,c in scored if sc>=MIN_SIGNAL_SCORE)
        watch  = sum(1 for sc,c in scored if sc>=MIN_SIGNAL_SCORE-1 and sc<MIN_SIGNAL_SCORE)
        rows = ""
        for sc,c in scored:
            sma9=c.get("sma9"); sma21=c.get("sma21")
            ema_gap = round(((sma9-sma21)/sma21)*100,2) if sma9 and sma21 and sma21>0 else None
            ema_crossed = ema_gap is not None and ema_gap > 0
            score_ok = sc >= MIN_SIGNAL_SCORE
            if score_ok and ema_crossed:   sig = f'<span class="sig-buy">🟢 BUY {sc:.1f}</span>'
            elif score_ok:                 sig = f'<span style="background:rgba(0,170,255,0.15);color:#00aaff;border:1px solid #00aaff;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700">👀 WATCH {sc:.1f}</span>'
            elif ema_crossed:              sig = f'<span style="background:rgba(255,204,0,0.1);color:#ffcc00;border:1px solid #ffcc00;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700">⚡ SIGNAL {sc:.1f}</span>'
            elif c["signal"]=="SELL":     sig = f'<span class="sig-sell">SELL</span>'
            else:                          sig = f'<span class="sig-hold">{sc:.1f}/{MIN_SIGNAL_SCORE}</span>'
            rsi=c.get("rsi")
            if rsi:
                if 50<=rsi<=65:    rsi_col="#00ff88"; rsi_lbl=f"{rsi:.1f} ✅"
                elif 40<=rsi<50:   rsi_col="#00aaff"; rsi_lbl=f"{rsi:.1f} 📈"
                elif 65<rsi<=75:   rsi_col="#ffcc00"; rsi_lbl=f"{rsi:.1f} ⚠"
                elif rsi>75:       rsi_col="#ff4466"; rsi_lbl=f"{rsi:.1f} 🔴"
                elif rsi<30:       rsi_col="#ff8800"; rsi_lbl=f"{rsi:.1f} 📉"
                else:               rsi_col="#475569"; rsi_lbl=f"{rsi:.1f}"
            else: rsi_col="#475569"; rsi_lbl="—"
            vr=c.get("vol_ratio",0)
            if vr>=2.0:    vc="#00ff88"; vl=f"{vr:.2f}x 🔥"
            elif vr>=1.5:  vc="#00aaff"; vl=f"{vr:.2f}x ✅"
            elif vr>=1.2:  vc="#ffcc00"; vl=f"{vr:.2f}x ⚠"
            elif vr>0:     vc="#475569"; vl=f"{vr:.2f}x"
            else:           vc="#475569"; vl="—"
            pct=min(100,int((sc/11)*100))
            if sc>=MIN_SIGNAL_SCORE:     bc="#00ff88"; prox=f"✅ {sc:.1f}"
            elif sc>=MIN_SIGNAL_SCORE-1: bc="#ffcc00"; prox=f"🔥 {sc:.1f}/{MIN_SIGNAL_SCORE}"
            elif sc>=MIN_SIGNAL_SCORE-2: bc="#ff8800"; prox=f"⚡ {sc:.1f}/{MIN_SIGNAL_SCORE}"
            else:                         bc="#333";    prox=f"{sc:.1f}/{MIN_SIGNAL_SCORE}"
            score_bar = (
                f'<div style="display:flex;align-items:center;gap:8px">'
                f'<div style="width:55px;height:7px;background:#1a1a1a;border-radius:4px;overflow:hidden">'
                f'<div style="width:{pct}%;height:100%;background:{bc};border-radius:4px"></div></div>'
                f'<span style="font-size:12px;color:{bc};font-weight:700">{prox}</span></div>'
            )
            if ema_gap is not None:
                if ema_gap>0:      ec="#00ff88"; es=f"+{ema_gap:.2f}% ✅"
                elif ema_gap>-0.5: ec="#ffcc00"; es=f"{ema_gap:.2f}% 🔥"
                elif ema_gap>-1.5: ec="#ff8800"; es=f"{ema_gap:.2f}% ⚡"
                else:               ec="#475569"; es=f"{ema_gap:.2f}%"
            else: ec="#475569"; es="—"
            bear_badge = ('<span style="font-size:10px;background:rgba(255,136,0,0.2);color:#ff8800;border:1px solid rgba(255,136,0,0.4);border-radius:4px;padding:1px 6px;margin-left:5px;font-weight:700">BEAR</span>' if c["symbol"] in bear_syms else "")
            cc = "#00ff88" if c["change"]>=0 else "#ff4466"
            chg_s = "+" if c["change"]>=0 else ""
            row_bg = "background:rgba(255,136,0,0.03);" if c["symbol"] in bear_syms else ""
            rows += (
                f'<tr style="{row_bg}">'
                f'<td style="font-weight:700;color:{color}">{c["symbol"]}{bear_badge}</td>'
                f'<td>${c["price"]:.4f}</td>'
                f'<td style="color:{cc}">{chg_s}{c["change"]:.2f}%</td>'
                f'<td>{sig}</td><td>{score_bar}</td>'
                f'<td style="color:{ec};font-weight:700">{es}</td>'
                f'<td style="color:{rsi_col};font-weight:700">{rsi_lbl}</td>'
                f'<td style="color:{vc};font-weight:700">{vl}</td></tr>'
            )
        return (
            f'<div style="display:flex;gap:18px;margin-bottom:16px;font-size:14px;flex-wrap:wrap">'
            f'<span style="color:#00ff88;font-weight:700">🟢 {buys} BUY</span>'
            f'<span style="color:#00aaff;font-weight:700">👀 {watch} NEAR</span>'
            f'<span style="color:#475569;margin-left:auto">{len(scored)} scanned</span></div>'
            f'<div style="overflow-x:auto"><table><thead><tr>'
            f'<th>Symbol</th><th>Price</th><th>Chg%</th><th>Signal</th><th>Score</th>'
            f'<th>EMA Cross</th><th>RSI</th><th>Vol</th></tr></thead><tbody>{rows}</tbody></table></div>'
        )

    # ── Build scored candidates per market ──
    def score_candidates(candidates):
        out = []
        for c in candidates:
            sc = score_signal(c["symbol"],c["price"],c["change"],c.get("rsi"),c.get("vol_ratio"),c.get("closes",[c["price"]]*22))
            sma9=c.get("sma9"); sma21=c.get("sma21")
            ema_gap = round(((sma9-sma21)/sma21)*100,2) if sma9 and sma21 and sma21>0 else None
            out.append((sc, ema_gap, c))
        out.sort(key=lambda x: -x[0])
        return out

    us_scored     = score_candidates(st_candidates.get("us", []))
    crypto_scored = score_candidates(st_candidates.get("crypto", []))
    asx_scored    = score_candidates(st_candidates.get("asx", []))
    ftse_scored   = score_candidates(st_candidates.get("ftse", []))
    sc_scored     = score_candidates(st_candidates.get("smallcap", []))

    # ── READY TO TRADE screener — signals that qualify RIGHT NOW ──
    def ready_to_trade_rows(scored, color, label):
        rows = ""
        for sc, ema_gap, c in scored:
            ema_crossed = ema_gap is not None and ema_gap > 0
            score_ok = sc >= MIN_SIGNAL_SCORE
            if not (score_ok and ema_crossed): continue
            cc = "#00ff88" if c["change"]>=0 else "#ff4466"
            chg_s = "+" if c["change"]>=0 else ""
            rsi = c.get("rsi")
            rsi_s = f"{rsi:.1f}" if rsi else "—"
            rsi_c = "#00ff88" if rsi and 50<=rsi<=65 else ("#ffcc00" if rsi and rsi<=75 else "#ff4466" if rsi and rsi>75 else "#475569")
            vr = c.get("vol_ratio",0)
            vr_s = f"{vr:.2f}x" if vr else "—"
            vr_c = "#00ff88" if vr>=1.5 else ("#ffcc00" if vr>=1.2 else "#475569")
            rows += (
                f'<tr>'
                f'<td style="font-weight:700;color:{color}">{c["symbol"]}</td>'
                f'<td style="font-size:11px;color:{color};font-weight:700">{label}</td>'
                f'<td>${c["price"]:.4f}</td>'
                f'<td style="color:{cc}">{chg_s}{c["change"]:.2f}%</td>'
                f'<td><span class="sig-buy">🟢 BUY {sc:.1f}</span></td>'
                f'<td style="color:#00ff88;font-weight:700">+{ema_gap:.2f}% ✅</td>'
                f'<td style="color:{rsi_c};font-weight:700">{rsi_s}</td>'
                f'<td style="color:{vr_c};font-weight:700">{vr_s}</td>'
                f'</tr>'
            )
        return rows

    rtt_rows = (
        ready_to_trade_rows(crypto_scored, "#00ff88", "Crypto") +
        ready_to_trade_rows(us_scored,     "#00aaff", "US") +
        ready_to_trade_rows(ftse_scored,   "#cc88ff", "FTSE") +
        ready_to_trade_rows(asx_scored,    "#ffaa00", "ASX") +
        ready_to_trade_rows(sc_scored,     "#ffcc00", "SmCap")
    )
    if rtt_rows:
        ready_to_trade_html = (
            f'<div class="card" style="margin-bottom:16px;border-color:rgba(0,255,136,0.3);background:rgba(0,255,136,0.03)">' 
            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">' 
            f'<div class="section-title" style="color:#00ff88;margin-bottom:0">🟢 READY TO TRADE</div>' 
            f'<div style="font-size:13px;color:#475569">Score ≥ {MIN_SIGNAL_SCORE} + EMA crossed — eligible for immediate execution</div>' 
            f'</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Market</th><th>Price</th><th>Chg%</th><th>Signal</th><th>EMA Cross</th><th>RSI</th><th>Vol</th>'
            f'</tr></thead><tbody>{rtt_rows}</tbody></table></div></div>'
        )
    else:
        ready_to_trade_html = (
            f'<div class="card" style="margin-bottom:16px;border-color:rgba(255,255,255,0.07)">' 
            f'<div style="display:flex;align-items:center;gap:12px">' 
            f'<div class="section-title" style="color:#475569;margin-bottom:0">🟢 READY TO TRADE</div>' 
            f'<div style="font-size:13px;color:#475569">No signals qualify right now — watching {sum(len(x) for x in [us_scored,crypto_scored,ftse_scored,asx_scored,sc_scored])} stocks across all markets</div>'
            f'</div></div>'
        )

    # ── Build per-market accordion panels ──
    def build_market_accordion(mid, label, icon, color, open_now, scored, is_open):
        if not scored:
            preview_html = f'<div style="padding:16px;color:#475569;font-size:14px">No scan data yet — waiting for first cycle</div>'
            full_html = preview_html
            buys = watch = 0
        else:
            buys  = sum(1 for sc,eg,c in scored if sc>=MIN_SIGNAL_SCORE and eg and eg>0)
            watch = sum(1 for sc,eg,c in scored if sc>=MIN_SIGNAL_SCORE and (not eg or eg<=0))
            near  = sum(1 for sc,eg,c in scored if MIN_SIGNAL_SCORE-1.5<=sc<MIN_SIGNAL_SCORE)
            total = len(scored)

            def mk_rows(items):
                rows = ""
                for sc,ema_gap,c in items:
                    ema_crossed = ema_gap is not None and ema_gap > 0
                    score_ok = sc >= MIN_SIGNAL_SCORE
                    if score_ok and ema_crossed:   sig = f'<span class="sig-buy">🟢 BUY {sc:.1f}</span>'
                    elif score_ok:                 sig = f'<span style="background:rgba(0,170,255,0.15);color:#00aaff;border:1px solid #00aaff;padding:3px 9px;border-radius:5px;font-size:12px;font-weight:700">👀 WATCH {sc:.1f}</span>'
                    elif ema_crossed:              sig = f'<span style="background:rgba(255,204,0,0.1);color:#ffcc00;border:1px solid #ffcc00;padding:3px 9px;border-radius:5px;font-size:12px;font-weight:700">⚡ SIGNAL {sc:.1f}</span>'
                    else:                          sig = f'<span class="sig-hold">{sc:.1f}/{MIN_SIGNAL_SCORE}</span>'
                    rsi=c.get("rsi")
                    if rsi:
                        if 50<=rsi<=65:    rc="#00ff88"; rl=f"{rsi:.1f} ✅"
                        elif rsi>75:       rc="#ff4466"; rl=f"{rsi:.1f} 🔴"
                        elif rsi>65:       rc="#ffcc00"; rl=f"{rsi:.1f} ⚠"
                        else:               rc="#475569"; rl=f"{rsi:.1f}"
                    else: rc="#475569"; rl="—"
                    vr=c.get("vol_ratio",0)
                    if vr>=2.0:    vc="#00ff88"; vl=f"{vr:.2f}x 🔥"
                    elif vr>=1.5:  vc="#00aaff"; vl=f"{vr:.2f}x ✅"
                    elif vr>=1.2:  vc="#ffcc00"; vl=f"{vr:.2f}x ⚠"
                    elif vr>0:     vc="#475569"; vl=f"{vr:.2f}x"
                    else:           vc="#475569"; vl="—"
                    pct=min(100,int((sc/11)*100))
                    if sc>=MIN_SIGNAL_SCORE:     bc="#00ff88"; prox=f"✅ {sc:.1f}"
                    elif sc>=MIN_SIGNAL_SCORE-1: bc="#ffcc00"; prox=f"🔥 {sc:.1f}"
                    elif sc>=MIN_SIGNAL_SCORE-2: bc="#ff8800"; prox=f"⚡ {sc:.1f}"
                    else:                         bc="#333";    prox=f"{sc:.1f}"
                    sbar = (
                        f'<div style="display:flex;align-items:center;gap:7px">'
                        f'<div style="width:50px;height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden">'
                        f'<div style="width:{pct}%;height:100%;background:{bc};border-radius:3px"></div></div>'
                        f'<span style="font-size:12px;color:{bc};font-weight:700">{prox}</span></div>'
                    )
                    eg_str = f"+{ema_gap:.2f}% ✅" if ema_gap and ema_gap>0 else (f"{ema_gap:.2f}% 🔥" if ema_gap and ema_gap>-0.5 else (f"{ema_gap:.2f}% ⚡" if ema_gap and ema_gap>-1.5 else (f"{ema_gap:.2f}%" if ema_gap else "—")))
                    eg_col = "#00ff88" if ema_gap and ema_gap>0 else ("#ffcc00" if ema_gap and ema_gap>-0.5 else ("#ff8800" if ema_gap and ema_gap>-1.5 else "#475569"))
                    cc="#00ff88" if c["change"]>=0 else "#ff4466"
                    chg_s="+" if c["change"]>=0 else ""
                    rows += (
                        f'<tr><td style="font-weight:700;color:{color}">{c["symbol"]}</td>'
                        f'<td>${c["price"]:.4f}</td>'
                        f'<td style="color:{cc}">{chg_s}{c["change"]:.2f}%</td>'
                        f'<td>{sig}</td><td>{sbar}</td>'
                        f'<td style="color:{eg_col};font-weight:700">{eg_str}</td>'
                        f'<td style="color:{rc};font-weight:700">{rl}</td>'
                        f'<td style="color:{vc};font-weight:700">{vl}</td></tr>'
                    )
                return rows

            thead = ('<div style="overflow-x:auto"><table><thead><tr>'
                     '<th>Symbol</th><th>Price</th><th>Chg%</th><th>Signal</th><th>Score</th>'
                     '<th>EMA Cross</th><th>RSI</th><th>Vol</th></tr></thead><tbody>')
            tfoot = '</tbody></table></div>'
            summary = (
                f'<div style="display:flex;gap:16px;margin-bottom:12px;font-size:14px;flex-wrap:wrap">'
                f'<span style="color:#00ff88;font-weight:700">🟢 {buys} BUY</span>'
                f'<span style="color:#00aaff;font-weight:700">👀 {watch} WATCH</span>'
                f'<span style="color:#ff8800;font-weight:700">⚡ {near} NEAR</span>'
                f'<span style="color:#475569;margin-left:auto">{total} scanned</span></div>'
            )
            preview_rows = mk_rows(scored[:10])
            full_rows    = mk_rows(scored)
            preview_html = summary + thead + preview_rows + tfoot
            show_all_btn = (
                f'<div id="mkt-btn-{mid}" style="margin-top:10px;text-align:center">'
                f'<button onclick="showFullMarket(\'{mid}\')" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:7px;color:#475569;padding:8px 20px;font-size:13px;cursor:pointer;font-family:monospace">'
                f'▼ Show all {total} stocks</button></div>' if total > 10 else ''
            )
            full_html = summary + thead + full_rows + tfoot

        # Market status badge
        status_badge = (
            f'<span style="font-size:12px;font-weight:700;color:{color};background:rgba(255,255,255,0.06);'
            f'padding:3px 10px;border-radius:5px;margin-left:8px">OPEN</span>'
            if open_now else
            f'<span style="font-size:12px;color:#475569;background:rgba(255,255,255,0.03);'
            f'padding:3px 10px;border-radius:5px;margin-left:8px">CLOSED</span>'
        )
        buys_badge = (
            f'<span style="font-size:12px;color:#00ff88;font-weight:700;margin-left:auto">🟢 {buys} BUY</span>'
            if buys > 0 else
            f'<span style="font-size:12px;color:#475569;margin-left:auto">{len(scored)} scanned</span>'
        ) if scored else ''

        border_col = color if open_now else "rgba(255,255,255,0.07)"
        bg_col = f"rgba({'0,255,136' if color=='#00ff88' else '0,170,255' if color=='#00aaff' else '204,136,255' if color=='#cc88ff' else '255,170,0' if color=='#ffaa00' else '255,204,0'},0.03)" if open_now else "transparent"

        return (
            f'<div style="border:1px solid {border_col};border-radius:12px;margin-bottom:10px;background:{bg_col};overflow:hidden">'
            f'<div onclick="toggleMarket(\'{mid}\')" style="display:flex;align-items:center;gap:10px;padding:14px 18px;cursor:pointer;user-select:none">'
            f'<span style="font-size:18px">{icon}</span>'
            f'<span style="font-size:16px;font-weight:700;color:{color if open_now else "#475569"}">{label}</span>'
            f'{status_badge}'
            f'{buys_badge}'
            f'<span id="mkt-arrow-{mid}" style="font-size:13px;color:#475569;margin-left:12px">{"▼" if open_now else "▶"}</span>'
            f'</div>'
            f'<div id="mkt-body-{mid}" style="display:{"block" if open_now else "none"};padding:0 18px 16px 18px">'
            f'<div id="mkt-preview-{mid}">{preview_html}</div>'
            f'<div id="mkt-full-{mid}" style="display:none">{full_html}</div>'
            f'{show_all_btn if scored and len(scored)>10 else ""}'
            f'</div>'
            f'</div>'
        )

    # Order: crypto always first (24/7), then open markets, then closed
    market_scanner_html = (
        f'<div style="margin-bottom:20px">'
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">'
        f'<div class="section-title" style="margin-bottom:0">📡 Market Scanner</div>'
        f'<div style="font-size:13px;color:#475569">Open markets expanded · top 10 shown · tap to expand/collapse</div>'
        f'</div>'
        + build_market_accordion("crypto","Crypto","🪙","#00ff88", True, crypto_scored, True)
        + build_market_accordion("us","US Stocks","📈","#00aaff", market_open, us_scored, market_open)
        + build_market_accordion("ftse","FTSE","🎩","#cc88ff", ftse_open, ftse_scored, ftse_open)
        + build_market_accordion("asx","ASX","🦘","#ffaa00", asx_open, asx_scored, asx_open)
        + build_market_accordion("smallcap","Small Cap","📊","#ffcc00", market_open, sc_scored, market_open)
        + f'</div>'
    )

    # ── News ──
    if not news_state["scan_complete"]:
        news_html = '<div class="empty" style="padding:24px">Waiting for 9:00 AM ET morning scan...</div>'
    else:
        skip_r = "".join(f'<tr><td style="color:#ff4466;font-weight:700">{s}</td><td><span class="sig-sell">SKIP</span></td><td style="color:#888;font-size:13px">{d["reason"]}</td></tr>' for s,d in news_state["skip_list"].items())
        boost_r = "".join(f'<tr><td style="color:#00ff88;font-weight:700">{s}</td><td><span class="sig-buy">POSITIVE</span></td><td style="color:#888;font-size:13px">{d["reason"]}</td></tr>' for s,d in news_state["watch_list"].items())
        all_r = skip_r + boost_r
        news_html = (
            f'<table><thead><tr><th>Symbol</th><th>Sentiment</th><th>Reason</th></tr></thead><tbody>{all_r}</tbody></table>'
            if all_r else '<div style="color:#475569;font-size:14px;padding:10px 0">All clear — no negative news today.</div>'
        )
    news_time = f"Last scan: {news_state.get('last_scan_time','')} ET" if news_state.get("last_scan_time") else "Scans at 9:00 AM ET daily"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot</title>
{BASE_CSS}
</head>
<body>

<div class="header">
  <div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="logo">AlphaBot <span>Dashboard</span></div>
      <span class="badge {'badge-live' if IS_LIVE else 'badge-paper'}">{'LIVE' if IS_LIVE else 'PAPER'}</span>
    </div>
    <div style="font-size:12px;color:#475569;margin-top:3px">IBKR · {now_date}</div>
  </div>
  <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
    <div style="text-align:right">
      <div style="font-size:11px;color:#00aaff;letter-spacing:1px">US P&L</div>
      <div style="font-size:15px;font-weight:700;color:{_col(st_states.get('us',{}).get('pnl',0))}">{_fmt(st_states.get('us',{}).get('pnl',0))}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:11px;color:#00ff88;letter-spacing:1px">Crypto P&L</div>
      <div style="font-size:15px;font-weight:700;color:{_col(st_states.get('crypto',{}).get('pnl',0))}">{_fmt(st_states.get('crypto',{}).get('pnl',0))}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:11px;color:#475569;letter-spacing:1px">Portfolio</div>
      <div style="font-size:15px;font-weight:700;color:#00aaff">{portfolio}</div>
    </div>
    <a href="/analytics" style="padding:8px 16px;border-radius:8px;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">🧠 ANALYTICS</a>
    <div style="font-size:12px;color:#475569">Cycle #{st_data.get('cycle', 0)}</div>
    <div style="font-size:12px;color:#475569" id="refresh-timer">↻ 60s</div>
  </div>
</div>

<div class="controls-bar">
  <span style="font-size:12px;color:#475569;text-transform:uppercase;letter-spacing:1px">Controls:</span>
  <button class="ctrl-btn" onclick="pinCmd('/kill','🛑 Kill all bots?')" style="border:1px solid #ff4466;background:rgba(255,68,102,0.1);color:#ff4466">🛑 KILL ALL BOTS</button>
  <button class="ctrl-btn" onclick="pinCmd('/close-all','💰 Close all positions?')" style="border:1px solid #ff8800;background:rgba(255,136,0,0.1);color:#ff8800">💰 CLOSE ALL POSITIONS</button>
  <button class="ctrl-btn" onclick="pinCmd('/resume','▶ Resume?')" style="border:1px solid #00ff88;background:rgba(0,255,136,0.1);color:#00ff88">▶ RESUME</button>
  <span id="dash-token" style="display:none">{DASH_TOKEN}</span>
  <span id="cmd-status" style="font-size:13px;color:#475569;margin-left:8px"></span>
</div>
<script>
function pinCmd(path,label){{
  var pin=prompt('PIN to confirm: '+label);
  if(pin===null)return;
  var token=document.getElementById('dash-token').textContent;
  var status=document.getElementById('cmd-status');
  status.textContent='Verifying...';
  fetch(path+'?token='+token+'&pin='+encodeURIComponent(pin),{{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
      if(d.status==='wrong_pin'){{status.textContent='❌ Wrong PIN';return;}}
      status.textContent='✅ '+d.status+' — refreshing...';
      setTimeout(()=>location.reload(),2000);
    }}).catch(e=>{{status.textContent='❌ Error: '+e;}});
}}
</script>

<div class="container">

{kill_banner}{circuit_banner}

<!-- Portfolio strip -->
<div class="grid5" style="margin-bottom:14px">
  <div class="card">
    <div style="display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <div>
        <div class="lbl">Total Balance · IBKR + Binance</div>
        <div class="big blue" style="font-size:32px">{portfolio}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:12px;color:#475569">{now_date}</div>
        <div style="font-size:13px;color:#475569;margin-top:3px">
          <span class="dot {'dot-green' if market_open else 'dot-red'}"></span>{"Open" if market_open else "Closed"}
        </div>
      </div>
    </div>
    <div style="margin-top:10px;display:flex;gap:24px;font-size:14px;flex-wrap:wrap">
      <span><span style="color:#475569">Today </span><span style="font-weight:700;color:{_col(today_pnl)}">{_fmt(today_pnl)}</span></span>
      <span><span style="color:#475569">Trades </span><span style="font-weight:700">{today_count}</span></span>
    </div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#00aaff">{this_month}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(tm_pnl)};margin:6px 0">{_fmt(tm_pnl)}</div>
    <div style="font-size:12px;color:#475569">{tm_t} trades · {_wr(tm_t,tm_w)}</div>
    <div style="margin-top:4px">{_vs(tm_pct,lm_pct)}</div>
  </div>
  <div class="card">
    <div class="lbl">{last_month}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(lm_pnl)};margin:6px 0">{_fmt(lm_pnl)}</div>
    <div style="font-size:12px;color:#475569">{lm_t} trades · {_wr(lm_t,lm_w)}</div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#00aaff">{this_week_lbl}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(tw_pnl)};margin:6px 0">{_fmt(tw_pnl)}</div>
    <div style="font-size:12px;color:#475569">{tw_t} trades · {_wr(tw_t,tw_w)}</div>
    <div style="margin-top:4px">{_vs(tw_pct,lw_pct)}</div>
  </div>
  <div class="card">
    <div class="lbl">Last Week</div>
    <div style="font-size:22px;font-weight:700;color:{_col(lw_pnl)};margin:6px 0">{_fmt(lw_pnl)}</div>
    <div style="font-size:12px;color:#475569">{lw_t} trades · {_wr(lw_t,lw_w)}</div>
  </div>
</div>

<!-- Last 7 days + Risk -->
<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px">
  <div class="card">
    <div class="lbl">Last 7 Days</div>
    <div style="font-size:24px;font-weight:700;color:{_col(week_pnl)};margin:6px 0">{_fmt(week_pnl)}</div>
    <div style="font-size:13px;color:#475569">{week_t} trades · {week_wr}% win rate</div>
    <div style="font-size:13px;color:#475569;margin-top:4px">Best: <span style="color:#00ff88">{week_best}</span> · Worst: <span style="color:#ff4466">{week_worst}</span></div>
  </div>
  <div class="card">
    <div class="lbl">Performance</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
      <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px">
        <div style="font-size:20px;font-weight:700;color:{wr_color}">{win_rate}%</div>
        <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Win Rate</div>
      </div>
      <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px">
        <div style="font-size:20px;font-weight:700;color:{pf_color}">{pf_str}</div>
        <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Prof Factor</div>
      </div>
      <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px">
        <div style="font-size:20px;font-weight:700;color:{dd_color}">{max_dd}%</div>
        <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Max DD</div>
      </div>
      <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px">
        <div style="font-size:20px;font-weight:700;color:{sharpe_color}">{sharpe_str}</div>
        <div style="font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Sharpe</div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="lbl">Risk Status</div>
    <div style="margin-top:10px;font-size:14px;display:flex;flex-direction:column;gap:6px">
      <div><span style="color:#475569">VIX </span><span style="color:{vix_color};font-weight:700">{vix_str_val}</span></div>
      <div><span style="color:#475569">Signal min </span><span style="color:#ffcc00;font-weight:700">{MIN_SIGNAL_SCORE}/10</span></div>
      <div><span style="color:#475569">Global pos </span><span style="font-weight:700">{global_pos}/{MAX_TOTAL_POSITIONS}</span></div>
      <div><span style="color:#475569">Loss streak </span><span style="color:{streak_color};font-weight:700">{loss_streak}/{LOSS_STREAK_LIMIT}</span></div>
      <div><span style="color:#475569">Size mult </span><span style="color:#ffcc00;font-weight:700">{size_mult}x</span></div>
    </div>
  </div>
</div>

<!-- Market cards -->
<div class="grid2" style="margin-bottom:14px">
  <div class="card card-blue">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#00aaff">📈 US Stocks</div>
      <div style="font-size:18px;font-weight:700;color:{regime_color}">{'🐂' if regime=='BULL' else '🐻'} {regime}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Status </span><span class="dot {_dot(st_states.get('us',{}))}"></span>{_status(st_states.get('us',{}))}</div>
      <div><span style="color:#475569">SPY </span><b>{spy_str}</b></div>
      <div><span style="color:#475569">Cycle </span>#{st_states.get('us',{}).get('cycle',0)}</div>
      <div><span style="color:#475569">MA20 </span><span style="color:#777">{spy_ma}</span></div>
      <div><span style="color:#475569">Positions </span><b>{st_states.get('us',{}).get('positions',0)}</b></div>
      <div><span style="color:#475569">VIX </span><span style="color:{vix_color}">{vix_regime}</span></div>
    </div>
  </div>
  <div class="card card-green">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#00ff88">🪙 Crypto</div>
      <div style="font-size:18px;font-weight:700;color:{c_regime_color}">{'🐂' if c_regime=='BULL' else '🐻'} {c_regime}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Status </span><span class="dot {_dot(st_states.get('crypto',{}))}"></span>{_status(st_states.get('crypto',{}))}</div>
      <div><span style="color:#475569">BTC </span><b>{btc_str}</b></div>
      <div><span style="color:#475569">Cycle </span>#{st_states.get('crypto',{}).get('cycle',0)}</div>
      <div><span style="color:#475569">Chg </span><span style="color:{btc_chg_col}">{btc_chg_str}</span></div>
      <div><span style="color:#475569">Positions </span><b>{st_states.get('crypto',{}).get('positions',0)}</b></div>
      <div><span style="color:#475569">Testnet </span><span style="color:#ffcc00">{'YES' if BINANCE_USE_TESTNET else 'LIVE'}</span></div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(255,170,0,0.25)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#ffaa00">🦘 ASX <span style="font-size:11px;color:#475569;font-weight:400">00:00–06:00 UTC</span></div>
      <div style="font-size:16px;font-weight:700;color:{asx_col}">{'🐂' if asx_mode=='BULL' else '🐻'} {asx_mode}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Market </span><span style="color:{'#ffaa00' if asx_open else '#475569'};font-weight:700">{'OPEN' if asx_open else 'CLOSED'}</span></div>
      <div><span style="color:#475569">CBA </span><b>{asx_cba}</b></div>
      <div><span style="color:#475569">Positions </span><b>{st_states.get('asx',{}).get('positions',0)}</b></div>
      <div><span style="color:#475569">Cycle </span>#{st_states.get('asx',{}).get('cycle',0)}</div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(204,136,255,0.25)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#cc88ff">🎩 FTSE <span style="font-size:11px;color:#475569;font-weight:400">08:00–16:30 UTC</span></div>
      <div style="font-size:16px;font-weight:700;color:{ftse_col}">{'🐂' if ftse_mode=='BULL' else '🐻'} {ftse_mode}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Market </span><span style="color:{'#cc88ff' if ftse_open else '#475569'};font-weight:700">{'OPEN' if ftse_open else 'CLOSED'}</span></div>
      <div><span style="color:#475569">HSBA </span><b>{ftse_hsba}</b></div>
      <div><span style="color:#475569">Positions </span><b>{st_states.get('ftse',{}).get('positions',0)}</b></div>
      <div><span style="color:#475569">Cycle </span>#{st_states.get('ftse',{}).get('cycle',0)}</div>
    </div>
  </div>
</div>

<!-- Small cap + Intraday -->
<div class="grid2" style="margin-bottom:14px">
  <div class="card" style="border-color:rgba(255,204,0,0.2)">
    <div style="font-size:16px;font-weight:700;color:#ffcc00;margin-bottom:10px">📊 Small Cap</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Status </span><span class="dot {_dot(st_states.get('smallcap',{}))}"></span>{_status(st_states.get('smallcap',{}))}</div>
      <div><span style="color:#475569">Pool </span>{len(smallcap_pool.get('symbols',[]))}</div>
      <div><span style="color:#475569">Positions </span><b>{st_states.get('smallcap',{}).get('positions',0)}</b></div>
      <div><span style="color:#475569">Cycle </span>#{st_states.get('smallcap',{}).get('cycle',0)}</div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(170,136,255,0.2)">
    <div style="font-size:16px;font-weight:700;color:#aa88ff;margin-bottom:10px">⚡ Intraday</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Stocks </span><span class="dot {_dot(st_states.get('intraday',{}))}"></span>{_status(st_states.get('intraday',{}))}</div>
      <div><span style="color:#475569">ID Cycle </span>#{st_states.get('intraday',{}).get('cycle',0)}</div>
      <div><span style="color:#475569">ID Pos </span>{st_states.get('intraday',{}).get('positions',0)}</div>
      <div><span style="color:#475569">Crypto </span><span class="dot {_dot(st_states.get('crypto_id',{}))}"></span>{_status(st_states.get('crypto_id',{}))}</div>
    </div>
  </div>
</div>

{positions_html}
{trades_html}
{ready_to_trade_html}

<!-- Market Scanner — vertical accordion, open markets first -->
{market_scanner_html}
<script>
function toggleMarket(id){{
  var body=document.getElementById('mkt-body-'+id);
  var arr=document.getElementById('mkt-arrow-'+id);
  var expanded=body.style.display!=='none';
  body.style.display=expanded?'none':'block';
  arr.textContent=expanded?'▶':'▼';
}}
function showFullMarket(id){{
  var preview=document.getElementById('mkt-preview-'+id);
  var full=document.getElementById('mkt-full-'+id);
  var btn=document.getElementById('mkt-btn-'+id);
  preview.style.display='none';
  full.style.display='block';
  btn.style.display='none';
}}
</script>

<!-- News -->
<div class="card" style="margin-bottom:20px;border-color:rgba(170,136,255,0.2)">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px">
    <div class="section-title" style="color:#aa88ff;margin-bottom:0">📰 Morning News Scan</div>
    <div style="font-size:13px;color:#475569">{news_time}</div>
  </div>
  {news_html}
</div>

<!-- Safety strip -->
<div style="padding:16px 20px;background:rgba(255,204,0,0.04);border:1px solid rgba(255,204,0,0.12);border-radius:10px;font-size:13px;color:#555;line-height:2">
  ⚠ <b style="color:#ffcc00">Safety:</b>
  Stop: {STOP_LOSS_PCT}% &nbsp;|&nbsp; Trail: {TRAILING_STOP_PCT}% &nbsp;|&nbsp; TP: {TAKE_PROFIT_PCT}%
  &nbsp;|&nbsp; Max hold: {MAX_HOLD_DAYS}d &nbsp;|&nbsp; Gap-down: {GAP_DOWN_PCT}%
  &nbsp;|&nbsp; Daily loss: ${MAX_DAILY_LOSS:.0f} &nbsp;|&nbsp; Per trade: ${MAX_TRADE_VALUE:.0f}
  &nbsp;|&nbsp; Daily spend: ${MAX_DAILY_SPEND:.0f}
</div>

</div>
<script>
var _t=60;var _el=document.getElementById("refresh-timer");
setInterval(function(){{_t--;if(_el)_el.textContent="↻ "+_t+"s";if(_t<=0)window.location.reload();}},1000);
</script>
</body></html>"""


# ═══════════════════════════════════════════════════════════════
# BUILD ANALYTICS PAGE
# ═══════════════════════════════════════════════════════════════
def build_analytics_page(search_sym=None, report_id=None, period="all"):
    period_days  = {"90": 90, "30": 30, "all": None}.get(period, None)
    period_label = {"90": "Last 90 Days", "30": "Last 30 Days", "all": "All Time"}.get(period, "All Time")
    leaders      = db_get_leaderboard(limit=20, period_days=period_days)
    medals       = ["🥇","🥈","🥉"]

    lb_rows = ""
    for i,row in enumerate(leaders):
        sym,trades,wins,losses,total_pnl,best,worst,avg_sc = row[:8]
        wr = int(wins/trades*100) if trades else 0
        pc = "#00ff88" if total_pnl>=0 else "#ff4466"
        medal = medals[i] if i < 3 else f"#{i+1}"
        lb_rows += (
            f'<tr>'
            f'<td style="color:#475569">{medal}</td>'
            f'<td style="font-weight:700;color:#00aaff">{sym}</td>'
            f'<td>{trades}</td><td style="color:#00ff88">{wins}</td><td style="color:#ff4466">{losses}</td>'
            f'<td style="font-weight:700;color:{"#00ff88" if wr>=55 else "#ffcc00" if wr>=45 else "#ff4466"}">{wr}%</td>'
            f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
            f'<td style="color:#00ff88">${best:.2f}</td><td style="color:#ff4466">${worst:.2f}</td>'
            f'<td style="color:#ffcc00">{avg_sc:.1f}</td></tr>'
        )
    if not lb_rows:
        lb_rows = '<tr><td colspan="10" style="text-align:center;color:#475569;padding:24px">No trades yet — data populates as trades close</td></tr>'

    search_html = ""
    if search_sym:
        res = db_search_symbol(search_sym)
        stats = res["stats"]
        if stats:
            sym2,total_t,wins2,losses2,total_pnl2,best2,worst2,avg_sc2,nm_count,last_t,first_t,_ = stats
            wr2 = int(wins2/total_t*100) if total_t>0 else 0
            pc2 = "#00ff88" if total_pnl2>=0 else "#ff4466"
            search_html = (
                f'<div style="background:#0d1117;border:1px solid rgba(0,170,255,0.2);border-radius:12px;padding:22px;margin-bottom:20px">'
                f'<div style="font-size:22px;font-weight:700;color:#00aaff;margin-bottom:14px;font-family:\'Syne\',sans-serif">{sym2}</div>'
                f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px">'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700;color:{pc2}">${total_pnl2:+.2f}</div><div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Total P&L</div></div>'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700">{total_t}</div><div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Trades</div></div>'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700;color:#00ff88">{wr2}%</div><div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Win Rate</div></div>'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700;color:#ff8800">{nm_count}</div><div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Near Misses</div></div>'
                f'</div></div>'
            )
        else:
            search_html = f'<div style="color:#475569;padding:20px;text-align:center;font-size:15px">No data for <b style="color:#00aaff">{search_sym}</b> yet</div>'

    # Skip reasons
    skip_reasons = db_get_skip_reason_breakdown()
    skip_html = ""
    if skip_reasons:
        rows = "".join(f'<tr><td style="color:#ffcc00">{r[0]}</td><td>{r[1]}</td><td style="color:#00aaff">{r[2]:.1f}</td></tr>' for r in skip_reasons)
        skip_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">📋 Skip Reason Breakdown</div>'
            f'<table><thead><tr><th>Reason</th><th>Count</th><th>Avg Score</th></tr></thead><tbody>{rows}</tbody></table></div>'
        )

    # DB stats
    total_t_db, total_pnl_db, wins_db, losses_db, avg_sc_db = _db_all_time_stats()
    try:
        conn = sqlite3.connect(DB_PATH)
        unique_syms  = conn.execute("SELECT COUNT(DISTINCT symbol) FROM trades").fetchone()[0] or 0
        total_misses = conn.execute("SELECT COUNT(*) FROM near_misses").fetchone()[0] or 0
        nm_rows = conn.execute(
            "SELECT symbol, score, skip_reason, created_at, pct_move, NULL, triggered "
            "FROM near_misses ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
    except:
        unique_syms=total_misses=0; nm_rows=[]

    pnl_col_db = "#00ff88" if total_pnl_db>=0 else "#ff4466"

    # Near misses
    if nm_rows:
        nr = ""
        for row in nm_rows:
            sym2,sc2,reason2,ts2,pct2,days2,checked2 = row
            ts_s = ts2[:10] if ts2 else "—"
            pct_s = f"+{pct2:.1f}%" if pct2 and pct2>=0 else (f"{pct2:.1f}%" if pct2 else "Pending")
            pct_c = "#00ff88" if pct2 and pct2>0 else ("#ff4466" if pct2 and pct2<0 else "#475569")
            nr += (
                f'<tr><td style="font-weight:700;color:#ffcc00">{sym2}</td>'
                f'<td style="color:#ffcc00">{sc2}/10</td>'
                f'<td style="color:#888">{reason2 or "SCORE"}</td>'
                f'<td style="color:#475569">{ts_s}</td>'
                f'<td style="color:{pct_c};font-weight:700">{pct_s}</td>'
                f'<td>{"✅" if checked2 else "⏳"}</td></tr>'
            )
        near_miss_html = (
            f'<div class="card" style="margin-bottom:20px;border-color:rgba(255,136,0,0.2)">'
            f'<div class="section-title" style="color:#ff8800">🎯 Near-Miss Intelligence ({len(nm_rows)} tracked)</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Score</th><th>Skip Reason</th><th>Date</th><th>Outcome</th><th>Checked</th>'
            f'</tr></thead><tbody>{nr}</tbody></table></div>'
            f'<div style="font-size:13px;color:#475569;margin-top:12px">Stocks just below threshold — tracked 5 days to see what they did.</div></div>'
        )
        # Threshold chart
        thresholds = [3.5,4.0,4.5,5.0,5.5,6.0,6.5,7.0]
        bars_html = ""
        for thr in thresholds:
            q = [r for r in nm_rows if r[1]>=thr and r[4] is not None]
            if q:
                avg_pct = sum(r[4] for r in q)/len(q)
                bc = "#00ff88" if avg_pct>0 else "#ff4466"
                bh = min(80,max(4,abs(avg_pct)*8))
                bars_html += (
                    f'<div style="display:flex;flex-direction:column;align-items:center;gap:5px;flex:1">'
                    f'<div style="font-size:11px;color:{bc};font-weight:700">{avg_pct:+.1f}%</div>'
                    f'<div style="width:100%;height:{bh}px;background:{bc};border-radius:4px 4px 0 0;opacity:0.8"></div>'
                    f'<div style="font-size:10px;color:#475569;text-align:center">{thr}<br>{len(q)}n</div></div>'
                )
            else:
                bars_html += (
                    f'<div style="display:flex;flex-direction:column;align-items:center;gap:5px;flex:1">'
                    f'<div style="font-size:11px;color:#333">—</div>'
                    f'<div style="width:100%;height:4px;background:#222;border-radius:4px 4px 0 0"></div>'
                    f'<div style="font-size:10px;color:#333;text-align:center">{thr}<br>0n</div></div>'
                )
        threshold_html = (
            f'<div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.15)">'
            f'<div class="section-title" style="color:#ffcc00">📈 Threshold Sensitivity — Avg Outcome by Min Score</div>'
            f'<div style="display:flex;align-items:flex-end;gap:8px;height:120px;padding:0 8px;border-bottom:1px solid #222;margin-bottom:10px">{bars_html}</div>'
            f'<div style="font-size:13px;color:#475569">Use this to calibrate your minimum signal score before going live.</div></div>'
        )
    else:
        near_miss_html = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(255,136,0,0.2)">'
            '<div class="section-title" style="color:#ff8800">🎯 Near-Miss Intelligence</div>'
            '<div style="color:#475569;font-size:14px;padding:12px 0">No near-misses tracked yet — populates as signals approach threshold.</div></div>'
        )
        threshold_html = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.15)">'
            '<div class="section-title" style="color:#ffcc00">📈 Threshold Sensitivity Chart</div>'
            '<div style="color:#475569;font-size:14px;padding:12px 0">Chart populates once near-miss data is available.</div></div>'
        )

    # Reports
    reports = db_get_reports(limit=30)
    report_rows = ""
    for r in reports:
        rid,rtype,rdate,subject = r
        icon = "📊" if rtype=="daily" else "📈" if rtype=="weekly" else "☀️"
        tc = "#00aaff" if rtype=="daily" else "#00ff88" if rtype=="weekly" else "#ffcc00"
        report_rows += (
            f'<tr onclick="loadReport({rid})" style="cursor:pointer">'
            f'<td style="color:{tc}">{icon} {rtype.title()}</td>'
            f'<td style="color:#888">{rdate}</td>'
            f'<td style="color:#e0e0e0">{subject or "—"}</td></tr>'
        )
    if not report_rows:
        report_rows = '<tr><td colspan="3" style="padding:24px;text-align:center;color:#475569">No reports yet</td></tr>'

    report_viewer = ""
    if report_id:
        rep = db_get_report_by_id(int(report_id))
        if rep:
            _,rtype,rdate,subject,body_html,body_text,_ = rep
            report_viewer = (
                f'<div style="background:#0d1117;border:1px solid rgba(0,170,255,0.2);border-radius:12px;padding:22px;margin-bottom:20px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
                f'<div style="font-weight:700;color:#e0e0e0;font-size:16px">{subject}</div>'
                f'<div style="color:#475569;font-size:13px">{rdate}</div></div>'
                f'<div style="border-top:1px solid #1a1a1a;padding-top:16px;font-size:14px;line-height:1.7;color:#ccc;white-space:pre-wrap">{body_text}</div>'
                f'</div>'
            )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot Analytics</title>
{BASE_CSS}
<style>
input{{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:12px 16px;color:#e0e0e0;font-size:15px;outline:none;width:70%;font-family:'JetBrains Mono',monospace}}
input:focus{{border-color:#00aaff}}
.period-btn{{padding:8px 16px;border-radius:7px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#475569;font-size:12px;font-weight:700;cursor:pointer;margin-left:6px;font-family:'JetBrains Mono',monospace}}
.period-btn.active{{background:rgba(0,170,255,0.15);border-color:rgba(0,170,255,0.3);color:#00aaff}}
</style>
</head>
<body>
<div style="background:#0d1117;border-bottom:1px solid #1a1a1a;padding:18px 28px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:100">
  <a href="/" style="color:#475569;text-decoration:none;font-size:14px;font-family:'JetBrains Mono',monospace">← Dashboard</a>
  <span style="color:#333">|</span>
  <span style="font-size:20px;font-weight:700;color:#00aaff;font-family:'Syne',sans-serif">🧠 Trading Intelligence</span>
</div>
<div class="container">

  <!-- Stats strip -->
  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:22px">
    <div class="card" style="text-align:center">
      <div style="font-size:26px;font-weight:700;color:{pnl_col_db}">${total_pnl_db:+.2f}</div>
      <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Total P&L</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:26px;font-weight:700;color:#00aaff">{total_t_db}</div>
      <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Total Trades</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:26px;font-weight:700;color:#00ff88">{unique_syms}</div>
      <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Symbols Traded</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:26px;font-weight:700;color:#ffcc00">{avg_sc_db:.1f}</div>
      <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Avg Score</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:26px;font-weight:700;color:#ff8800">{total_misses}</div>
      <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Near Misses</div>
    </div>
  </div>

  <!-- Search -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">🔍 Symbol Search</div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap">
      <input type="text" id="search-input" placeholder="Search any ticker — NVDA, BTCUSDT..." value="{search_sym or ''}" onkeydown="if(event.key==='Enter') doSearch()">
      <button onclick="doSearch()" style="padding:12px 22px;background:rgba(0,170,255,0.15);border:1px solid rgba(0,170,255,0.3);border-radius:8px;color:#00aaff;font-size:14px;font-weight:700;cursor:pointer;font-family:'JetBrains Mono',monospace">Search</button>
    </div>
    {search_html}
  </div>

  <!-- Leaderboard -->
  <div class="card" style="margin-bottom:20px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px">
      <div class="section-title" style="margin:0">🏆 Leaderboard — {period_label}</div>
      <div>
        <button class="period-btn {'active' if period=='30' else ''}" onclick="setPeriod('30')">30 Days</button>
        <button class="period-btn {'active' if period=='90' else ''}" onclick="setPeriod('90')">90 Days</button>
        <button class="period-btn {'active' if period=='all' else ''}" onclick="setPeriod('all')">All Time</button>
      </div>
    </div>
    <div class="table-wrap">
      <table><thead><tr>
        <th>Rank</th><th>Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th>
        <th>Win Rate</th><th>Total P&L</th><th>Best</th><th>Worst</th><th>Avg Score</th>
      </tr></thead><tbody>{lb_rows}</tbody></table>
    </div>
  </div>

  {skip_html}
  {near_miss_html}
  {threshold_html}

  <!-- Reports -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">📁 Report Archive</div>
    {report_viewer}
    <table><thead><tr><th>Type</th><th>Date</th><th>Subject</th></tr></thead><tbody>{report_rows}</tbody></table>
  </div>

</div>
<script>
function doSearch(){{var s=document.getElementById('search-input').value.trim().toUpperCase();if(s)window.location.href='/analytics?search='+encodeURIComponent(s);}}
function setPeriod(p){{window.location.href='/analytics?period='+p;}}
function loadReport(id){{window.location.href='/analytics?report_id='+id;}}
</script>
</body></html>"""


# ═══════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ═══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api")
async def api_json():
    with _state_lock:
        data = {
            "stocks":      {"pnl": state.daily_pnl, "positions": len(state.positions), "cycle": state.cycle_count},
            "crypto":      {"pnl": crypto_state.daily_pnl, "positions": len(crypto_state.positions), "cycle": crypto_state.cycle_count},
            "portfolio":   float(cfg.account_info.get("portfolio_value", 0)) if cfg.account_info else 0,
            "kill_switch": kill_switch["active"],
            "today_pnl":   _db_today_pnl(),
        }
    return JSONResponse(data)

@app.get("/", response_class=HTMLResponse)
async def index(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        with _state_lock:
            html = build_dashboard()
        return html
    except Exception as e:
        import traceback
        log.error(f"[DASHBOARD] Error: {e}\n{traceback.format_exc()}")
        return HTMLResponse(f"<html><body style='background:#111;color:#fff;padding:40px;font-family:monospace'><h2>Dashboard Error</h2><pre>{e}</pre></body></html>")

@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request, search: str = None, report_id: str = None, period: str = "all"):
    response = Response()
    response.headers["Cache-Control"] = "no-store"
    try:
        html = build_analytics_page(search_sym=search, report_id=report_id, period=period)
        return HTMLResponse(html)
    except Exception as e:
        log.error(f"[ANALYTICS] Error: {e}")
        return HTMLResponse(f"<html><body style='background:#111;color:#fff;padding:40px'><h2>Analytics Error</h2><pre>{e}</pre></body></html>")

@app.post("/kill")
async def kill(request: Request):
    params = dict(request.query_params)
    if params.get("pin") != KILL_PIN:
        return JSONResponse({"status": "wrong_pin"})
    kill_switch.update({"active": True, "reason": "Manual kill from dashboard",
                        "activated_at": datetime.now(PARIS).strftime("%H:%M:%S")})
    for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, asx_state, ftse_state]:
        st.shutoff = True
    log.warning("[KILL SWITCH] Manual kill activated from dashboard")
    return JSONResponse({"status": "killed"})

@app.post("/resume")
async def resume(request: Request):
    params = dict(request.query_params)
    if params.get("pin") != KILL_PIN:
        return JSONResponse({"status": "wrong_pin"})
    kill_switch.update({"active": False, "reason": "", "activated_at": None})
    for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, asx_state, ftse_state]:
        st.shutoff = False
    log.info("[KILL SWITCH] Resumed from dashboard")
    return JSONResponse({"status": "resumed"})

@app.post("/close-all")
async def close_all(request: Request):
    params = dict(request.query_params)
    if params.get("pin") != KILL_PIN:
        return JSONResponse({"status": "wrong_pin"})
    log.warning("[KILL SWITCH] Close all positions from dashboard")
    for sym, pos in list(state.positions.items()):
        place_order(sym, "sell", pos["qty"], estimated_price=pos["entry_price"])
    for sym, pos in list(crypto_state.positions.items()):
        place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
    state.positions.clear(); crypto_state.positions.clear()
    kill_switch.update({"active": True, "reason": "Close all — liquidated from dashboard",
                        "activated_at": datetime.now(PARIS).strftime("%H:%M:%S")})
    for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, asx_state, ftse_state]:
        st.shutoff = True
    return JSONResponse({"status": "closed"})


# ═══════════════════════════════════════════════════════════════
# KEEP start_dashboard() for backwards compat with main.py
# ═══════════════════════════════════════════════════════════════
def start_dashboard():
    """Legacy stub — dashboard now runs as standalone uvicorn service."""
    log.info("[DASHBOARD] FastAPI dashboard — run via uvicorn in separate screen session")
