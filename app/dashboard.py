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
import json as _json
_CONFIG_PATH = "/home/alphabot/app/trading_config.json"

def _load_tcfg():
    try:
        with open(_CONFIG_PATH) as f: return _json.load(f)
    except: return {}

def _save_tcfg(updates: dict):
    try:
        c = _load_tcfg()
        c.update(updates)
        import datetime as _dt
        c["_last_modified"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c["_modified_by"] = "dashboard"
        with open(_CONFIG_PATH, "w") as f: _json.dump(c, f, indent=2)
        return True
    except Exception as e:
        log.error(f"[SETTINGS] Save failed: {e}")
        return False
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
@media(max-width:1024px){
  .grid5{grid-template-columns:1fr 1fr}
  .grid3{grid-template-columns:1fr}
  .grid2{grid-template-columns:1fr}
  .container{padding:12px}
  .header{padding:12px 16px}
  table{min-width:500px;font-size:13px}
  th,td{padding:8px 10px}
}
@media(max-width:768px){
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
  .pos-table-wrap{display:none}
  .pos-cards{display:block}
  .trades-table-wrap{display:none}
  .trades-cards{display:block}
  .scan-table th:nth-child(6),.scan-table td:nth-child(6),
  .scan-table th:nth-child(7),.scan-table td:nth-child(7),
  .scan-table th:nth-child(8),.scan-table td:nth-child(8){display:none}
  .scan-table td:nth-child(4),.scan-table td:nth-child(5){white-space:nowrap}
}
.pos-card{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:12px;padding:14px;margin-bottom:10px;cursor:pointer;-webkit-tap-highlight-color:rgba(0,255,136,0.1);user-select:none;-webkit-user-select:none}
.pos-card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.pos-card-sym{font-size:17px;font-weight:700;font-family:'Syne',sans-serif}
.pos-card-pnl{font-size:15px;font-weight:700;text-align:right;line-height:1.3}
.pos-card-row{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;margin-bottom:0}
.pos-card-item{display:flex;flex-direction:column;gap:2px}
.pos-card-label{font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:1.5px}
.pos-card-value{font-size:13px;font-weight:600;font-family:'JetBrains Mono',monospace}
.pos-card-detail{margin-top:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)}
.pos-card-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.trade-card{background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:12px;margin-bottom:8px}
.trade-card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.trade-card-sym{font-size:15px;font-weight:700;font-family:'Syne',sans-serif;color:#00aaff}
.trade-card-pnl{font-size:14px;font-weight:700}
.trade-card-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 8px}
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

    # ── Daily cards: Today, Yesterday, Day before ──
    def _day_stats(days_ago):
        d = date.today() - timedelta(days=days_ago)
        d_next = d + timedelta(days=1)
        t, pnl, w = _db_pnl_for_period(d.isoformat(), d_next.isoformat())
        avg = pnl / t if t else 0.0
        pct = _pct(pnl, port_val)
        wr = int(w/t*100) if t else 0
        name = d.strftime("%A")  # Monday, Tuesday etc
        return {"name": name, "short": d.strftime("%a %d %b"), "t": t, "pnl": pnl, "w": w, "avg": avg, "pct": pct, "wr": wr}

    d0 = _day_stats(0)  # Today
    d1 = _day_stats(1)  # Yesterday
    d2 = _day_stats(2)  # Day before yesterday

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
            live = pos.get("_live") or pos.get("highest_price", pos["entry_price"])
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
                f'<tr id="det-{idx}" style="display:none;background:rgba(255,255,255,0.04)">'
                f'<td colspan="9" style="padding:16px 20px;border-bottom:1px solid rgba(255,255,255,0.05)">'
                f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px 20px;font-size:13px;margin-bottom:12px">'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Score</span><br><b style="color:#ffcc00;font-size:16px">{score}/10</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Qty</span><br><b style="font-size:15px">{qty:,}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Entry</span><br><b style="font-size:15px">${entry:.4f}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Live</span><br><b style="color:#00aaff;font-size:15px">${live:.4f}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Stop</span><br><b style="color:#ff4466;font-size:15px">${pos["stop_price"]:.4f}</b> <span style="color:#ff4466;font-size:12px">({stop_pct:+.1f}%)</span></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Target</span><br><b style="color:#00ff88;font-size:15px">${tp_price:.4f}</b> <span style="color:#00ff88;font-size:12px">(+{target_pct:.1f}%)</span></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">P&amp;L</span><br><b style="color:{pnl_c};font-size:15px">{sign}${pnl:.2f}</b> <span style="color:{pnl_c};font-size:12px">({sign}{pnl_pct:.1f}%)</span></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Held</span><br><b style="font-size:15px">{entry_dt}</b></div>'
                f'</div>'
            )
            if bd_html:
                pos_rows += (
                    f'<tr id="det-{idx}-bd" style="display:none;background:rgba(255,255,255,0.04)">'
                    f'<td colspan="9" style="padding:0 20px 16px">'
                    f'<div style="border-top:1px solid rgba(255,255,255,0.06);padding-top:12px">'
                    f'<span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Why Bought</span><br>'
                    f'<div style="font-size:12px;color:#b0bec5;margin-top:6px;white-space:pre-wrap">{bd_html}</div>'
                    f'</div></td></tr>'
                )
            pos_rows += (f'</td></tr>'
            )
        # Build iPhone card HTML for positions
        iphone_pos_cards = ""
        for idx,(sym,pos,cat_col,cat) in enumerate(all_pos):
            live2 = pos.get("_live") or pos.get("highest_price", pos["entry_price"])
            entry2 = pos["entry_price"]; qty2 = pos["qty"]
            pnl2 = (live2-entry2)*qty2; pnl_pct2 = ((live2-entry2)/entry2)*100
            pos_val2 = live2*qty2; pnl_c2 = "#00ff88" if pnl2>=0 else "#ff4466"
            sign2 = "+" if pnl2>=0 else ""
            stop_pct2 = round(((pos["stop_price"]-entry2)/entry2)*100,1)
            tp2 = pos.get("take_profit_price", entry2*1.10)
            score2 = pos.get("signal_score","—")
            try:
                dt2 = datetime.fromisoformat(pos.get("entry_ts",""))
                if dt2.tzinfo is None: dt2 = dt2.replace(tzinfo=ZoneInfo("UTC"))
                dt_p2 = dt2.astimezone(PARIS)
                purchased2 = dt_p2.strftime("%d %b %H:%M")
                held2 = datetime.now(PARIS) - dt_p2
                hh2 = int(held2.total_seconds()//3600); hm2 = int((held2.total_seconds()%3600)//60)
                held_str2 = f"{hh2//24}d {hh2%24}h" if hh2>=24 else f"{hh2}h {hm2}m"
            except:
                purchased2 = pos.get("entry_date","—"); held_str2 = "—"
            bd2 = pos.get("entry_breakdown","")
            bd_html2 = f'<div style="font-size:11px;color:#888;margin-top:8px;white-space:pre-wrap">{bd2}</div>' if bd2 else ""
            iphone_pos_cards += (
                f'<div class="pos-card" onclick="toggleCard({idx})" style="border-color:{cat_col}22;cursor:pointer;-webkit-tap-highlight-color:transparent">'
                f'<div class="pos-card-header">'
                f'<div><span class="pos-card-sym" style="color:{cat_col}">{sym}</span>'
                f'<span style="font-size:11px;color:{cat_col};margin-left:8px;font-weight:700;opacity:0.7">{cat}</span></div>'
                f'<div style="text-align:right">'
                f'<div class="pos-card-pnl" style="color:{pnl_c2}">{sign2}${pnl2:.2f} <span style="font-size:11px;opacity:0.8">({sign2}{pnl_pct2:.1f}%)</span></div>'
                f'<div class="tap-hint" style="font-size:10px;color:#475569;margin-top:3px">tap for more ▾</div>'
                f'</div>'
                f'</div>'
                f'<div class="pos-card-row">'
                f'<div class="pos-card-item"><span class="pos-card-label">Entry</span><span class="pos-card-value">${entry2:.4f}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Live</span><span class="pos-card-value" style="color:#00aaff">${live2:.4f}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Stop</span><span class="pos-card-value" style="color:#ff4466">${pos["stop_price"]:.4f} ({stop_pct2:+.1f}%)</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Position</span><span class="pos-card-value">${pos_val2:,.0f}</span></div>'
                f'</div>'
                f'<div class="pos-card-detail" id="card-det-{idx}" style="display:none">'
                f'<div class="pos-card-detail-grid">'
                f'<div class="pos-card-item"><span class="pos-card-label">Held</span><span class="pos-card-value">{held_str2}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Purchased</span><span class="pos-card-value">{purchased2}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Score</span><span class="pos-card-value" style="color:#ffcc00">{score2}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Target</span><span class="pos-card-value" style="color:#00ff88">${tp2:.4f}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Qty</span><span class="pos-card-value">{qty2:,}</span></div>'
                f'</div>{bd_html2}</div>'
                f'</div>'
            )
        positions_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<div class="section-title">CURRENTLY HOLDING ({len(all_pos)}) <span style="font-size:13px;color:#475569;font-weight:400;font-family:\'JetBrains Mono\'"></span></div>'
            f'<div class="pos-table-wrap table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Type</th><th>Held</th><th>Purchased</th>'
            f'<th>Entry $</th><th>Live $</th><th>Stop</th><th>Position $</th><th>P&L</th>'
            f'</tr></thead><tbody>{pos_rows}</tbody></table></div>'
            f'<div class="pos-cards">{iphone_pos_cards}</div>'
            f'</div>'
            f'<script>'
            f'function toggleDetail(i){{var r=document.getElementById("det-"+i);r.style.display=r.style.display==="none"?"table-row":"none";}}'
            f'function toggleCard(i){{var d=document.getElementById("card-det-"+i);var open=d.style.display!=="none";d.style.display=open?"none":"block";var card=d.closest(".pos-card");var hint=card?card.querySelector(".tap-hint"):null;if(hint)hint.textContent=open?"tap for more ▾":"tap to close ▴";}}'
            f'</script>'
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
            discipline = row[9] if len(row) > 9 else "swing"
            _disc_map = {"crypto_intraday":("⚡","#aa88ff","Crypto ID"),"stock_intraday":("⚡","#00aaff","Stock ID"),"crypto_swing":("🔄","#00ff88","Crypto Swing"),"stock_swing":("📈","#00aaff","Stock Swing"),"swing":("📈","#00aaff","Swing")}
            disc_icon, disc_col, disc_label = _disc_map.get(discipline, ("•","#475569", discipline))
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
            t_idx = len([x for x in trade_rows.split('trade-det-') if x]) - 1
            if pnl < 0:
                sell_reason = "🛑 Stop loss triggered"
                sell_col = "#ff4466"
            elif hold_h and hold_h > 96:
                sell_reason = "⏱ Max hold reached — stale exit"
                sell_col = "#ffcc00"
            elif hold_h and hold_h < 0.5:
                sell_reason = "⚡ Quick scalp"
                sell_col = "#00ff88"
            elif pnl > 0:
                sell_reason = "🎯 Take profit hit"
                sell_col = "#00ff88"
            else:
                sell_reason = "— Position closed"
                sell_col = "#475569"
            trade_rows += (
                f'<tr onclick="toggleTrade({t_idx})" style="cursor:pointer">'
                f'<td>{"✅" if pnl>0 else "❌"}</td>'
                f'<td style="font-weight:700;color:#00aaff">{sym} <span title="{disc_label}" style="font-size:10px;background:rgba(255,255,255,0.06);color:{disc_col};border:1px solid {disc_col}44;border-radius:4px;padding:1px 5px;margin-left:3px;font-weight:700">{disc_icon}</span></td>'
                f'<td style="color:{mkt_col};font-size:11px;font-weight:700">{market}</td>'
                f'<td style="color:#475569">{date_s}</td>'
                f'<td style="color:#475569">{time_s}</td>'
                f'<td style="color:#777">{price_s}</td>'
                f'<td style="color:#aaa">{qty_s}</td>'
                f'<td style="color:#aaa">{total_s}</td>'
                f'<td style="color:#475569">{hold_s}</td>'
                f'<td style="color:{pc};font-weight:700">{sign}${pnl:.2f}</td>'
                f'<td style="color:{pc};font-weight:700">{(f"{sign}{abs(pnl/(price*qty)*100):.1f}%" if price and qty else "—")}</td>'
                f'<td style="color:#475569">{score or "—"}</td>'
                f'</tr>'
                f'<tr id="trade-det-{t_idx}" style="display:none;background:rgba(255,255,255,0.04)">'
                f'<td colspan="12" style="padding:14px 20px;border-bottom:1px solid rgba(255,255,255,0.05)">'
                f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px 20px;font-size:13px;margin-bottom:12px">'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Exit Price</span><br><b style="font-size:15px">{price_s}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Qty</span><br><b style="font-size:15px">{qty_s}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Total Value</span><br><b style="font-size:15px">{total_s}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Held</span><br><b style="font-size:15px">{hold_s}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Score</span><br><b style="color:#ffcc00;font-size:15px">{score or "—"}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">P&amp;L</span><br><b style="color:{pc};font-size:15px">{sign}${pnl:.2f}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Market</span><br><b style="color:{mkt_col};font-size:15px">{market}</b></div>'
                f'<div><span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Closed</span><br><b style="font-size:15px">{date_s} {time_s}</b></div>'
                f'</div>'
                f'<div style="border-top:1px solid rgba(255,255,255,0.06);padding-top:10px">'
                f'<span style="color:#8899aa;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Exit Reason</span><br>'
                f'<b style="color:{sell_col};font-size:14px">{sell_reason}</b>'
                f'</div>'
                f'</td></tr>'
            )
        # Build iPhone trade cards
        iphone_trade_cards = ""
        for row in db_trades:
            sym_t,pnl_t,side_t,ts_t,score_t = row[0],row[1],row[2],row[3],row[4]
            qty_t = row[5] if len(row)>5 else None
            price_t = row[6] if len(row)>6 else None
            hold_t = row[7] if len(row)>7 else None
            market_t = row[8] if len(row)>8 else "—"
            pc_t = "#00ff88" if pnl_t>=0 else "#ff4466"
            sign_t = "+" if pnl_t>=0 else ""
            try:
                dt_t = datetime.fromisoformat(ts_t)
                if dt_t.tzinfo is None: dt_t = dt_t.replace(tzinfo=ZoneInfo("UTC"))
                dt_tp = dt_t.astimezone(PARIS)
                date_t = dt_tp.strftime("%d %b")
                time_t = dt_tp.strftime("%H:%M")
            except:
                date_t = ts_t[:10] if ts_t else "—"; time_t = ""
            mkt_col_t = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00","ASX":"#ffaa00","FTSE":"#cc88ff"}.get(market_t,"#475569")
            disc_t = row[9] if len(row)>9 else "swing"
            _dm = {"crypto_intraday":("⚡","#aa88ff"),"stock_intraday":("⚡","#00aaff"),"crypto_swing":("🔄","#00ff88"),"stock_swing":("📈","#00aaff"),"swing":("📈","#00aaff")}
            disc_icon_t, disc_col_t = _dm.get(disc_t, ("•","#475569"))
            qty_s_t = f"{int(qty_t):,}" if qty_t else "—"
            price_s_t = f"${price_t:.4f}" if price_t else "—"
            total_s_t = f"${price_t*qty_t:,.0f}" if price_t and qty_t else "—"
            hold_s_t = f"{hold_t:.1f}h" if hold_t else "—"
            iphone_trade_cards += (
                f'<div class="trade-card">'
                f'<div class="trade-card-header">'
                f'<div style="display:flex;align-items:center;gap:8px">'  
                f'<span style="font-size:16px">{"✅" if pnl_t>0 else "❌"}</span>'
                f'<div><span class="trade-card-sym">{sym_t}</span> <span style="font-size:10px;color:{disc_col_t};background:rgba(255,255,255,0.06);border:1px solid {disc_col_t}44;border-radius:4px;padding:1px 4px;font-weight:700">{disc_icon_t}</span>'
                f'<span style="font-size:10px;color:{mkt_col_t};margin-left:6px;font-weight:700">{market_t}</span></div>'
                f'</div>'
                f'<span class="trade-card-pnl" style="color:{pc_t}">{sign_t}${pnl_t:.2f}</span>'
                f'</div>'
                f'<div class="trade-card-row">'
                f'<div class="pos-card-item"><span class="pos-card-label">Date</span><span class="pos-card-value">{date_t} {time_t}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Price</span><span class="pos-card-value">{price_s_t}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Qty</span><span class="pos-card-value">{qty_s_t}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Total</span><span class="pos-card-value">{total_s_t}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Held</span><span class="pos-card-value">{hold_s_t}</span></div>'
                f'<div class="pos-card-item"><span class="pos-card-label">Score</span><span class="pos-card-value" style="color:#ffcc00">{score_t or "—"}</span></div>'
                f'</div></div>'
            )
        trades_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<div class="section-title" style="text-transform:uppercase;letter-spacing:1px">RECENT TRADES <span style="font-size:12px;color:#475569;font-weight:400;text-transform:none">DB-backed · survives restarts</span></div>'
            f'<div class="trades-table-wrap table-wrap"><table><thead><tr>'
            f'<th></th><th>Symbol</th><th>Mkt</th><th>Date</th><th>Time</th>'
            f'<th>Entry $</th><th>Qty</th><th>Total $</th><th>Held</th><th>P&L</th><th>%</th><th>Score</th>'
            f'</tr></thead>'
            f'<tbody>{trade_rows}</tbody></table></div>'
            f'<div class="trades-cards">{iphone_trade_cards}</div>'
            f'<div style="margin-top:10px;font-size:13px;color:#475569">Total: {total_t} trades · '
            f'<span style="color:{_col(total_pnl_db)}">{_fmt(total_pnl_db)}</span> all-time · '
            f'{win_rate}% win rate</div>'
            f'<script>function toggleTrade(i){{var r=document.getElementById("trade-det-"+i);if(r)r.style.display=r.style.display==="none"?"table-row":"none";}}</script>'
            f'</div>'
        )
    else:
        trades_html = f'<div class="card" style="margin-bottom:16px"><div class="empty">No completed trades yet — tracking starts when first position closes</div></div>'

    # ── Scan table builder ──
    bear_syms = set(BEAR_TICKERS)
    # ── Gate helper functions (shared by scanner + RTT) ──────────
    from core.config import MAX_SECTOR_POSITIONS, SECTOR_MAP as _SECTOR_MAP

    def _dot(ok, close=False, size=14):
        if ok:    return f'<span style="color:#00ff88;font-size:{size}px;line-height:1;filter:drop-shadow(0 0 3px #00ff88)">●</span>'
        if close: return f'<span style="color:#ffcc00;font-size:{size}px;line-height:1;filter:drop-shadow(0 0 3px #ffcc00)">●</span>'
        return     f'<span style="color:#ff4466;font-size:{size}px;line-height:1">●</span>'

    def _gates(sc, ema_gap, c, held_syms):
        """Returns (dot, value_str, pass) for each gate."""
        rsi = c.get("rsi"); vr = c.get("vol_ratio",0); sym = c["symbol"]
        vwap = c.get("vwap"); sec = _SECTOR_MAP.get(sym)
        held_sec = sum(1 for s in held_syms if _SECTOR_MAP.get(s)==sec) if sec else 0

        g_scr  = _dot(sc>=MIN_SIGNAL_SCORE, sc>=MIN_SIGNAL_SCORE-1)
        g_ema  = _dot(ema_gap is not None and ema_gap>0, ema_gap is not None and ema_gap>-0.5)
        g_rsi  = _dot(not rsi or rsi<75, rsi and rsi<80) if rsi else _dot(True)
        g_vol  = _dot(vr>=1.5, vr>=1.2)
        g_vap  = _dot(vwap!="BELOW") if vwap else _dot(True)
        # SEC gate removed from display but still checked for all_pass

        v_scr  = f"{sc:.1f}/{MIN_SIGNAL_SCORE}"
        v_ema  = (f"+{ema_gap:.2f}%" if ema_gap and ema_gap>0 else f"{ema_gap:.2f}%" if ema_gap else "—")
        v_rsi  = f"{rsi:.1f}" if rsi else "—"
        v_vol  = f"{vr:.2f}x" if vr else "—"
        v_vap  = vwap or "n/a"
        v_sec  = f"{held_sec}/{MAX_SECTOR_POSITIONS}" if sec else "—"  # kept for dropdown only

        all_pass = (sc>=MIN_SIGNAL_SCORE and ema_gap is not None and ema_gap>0
                    and (not rsi or rsi<75) and vr>=1.5
                    and vwap!="BELOW" and held_sec<MAX_SECTOR_POSITIONS)
        g_sec = ""  # removed from display
        return (g_scr,g_ema,g_rsi,g_vol,g_vap,g_sec,
                v_scr,v_ema,v_rsi,v_vol,v_vap,v_sec, all_pass)

    _held_syms_gates = set(sym for sym,_,_c,_t in all_pos) if all_pos else set()

    def build_scan_table(candidates, color):
        if not candidates:
            return '<div class="empty">No scan data yet</div>'
        scored = []
        for c in candidates:
            sc = score_signal(c["symbol"],c["price"],c["change"],c.get("rsi"),c.get("vol_ratio"),c.get("closes",[c["price"]]*22))
            sma9=c.get("sma9"); sma21=c.get("sma21")
            ema_gap = round(((sma9-sma21)/sma21)*100,2) if sma9 and sma21 and sma21>0 else None
            scored.append((sc,ema_gap,c))
        normal = sorted([(sc,eg,c) for sc,eg,c in scored if c["symbol"] not in bear_syms],key=lambda x:-x[0])
        bears  = sorted([(sc,eg,c) for sc,eg,c in scored if c["symbol"] in bear_syms],key=lambda x:-x[0])
        scored = normal + bears
        buys  = sum(1 for sc,eg,c in scored if sc>=MIN_SIGNAL_SCORE and eg is not None and eg>0)
        watch = sum(1 for sc,eg,c in scored if sc>=MIN_SIGNAL_SCORE-1 and sc<MIN_SIGNAL_SCORE)
        rows = ""
        for sc,ema_gap,c in scored:
            sym = c["symbol"]
            g_scr,g_ema,g_rsi,g_vol,g_vap,g_sec,v_scr,v_ema,v_rsi,v_vol,v_vap,v_sec,all_pass = _gates(sc,ema_gap,c,_held_syms_gates)
            bear_badge = ('<span style="font-size:9px;background:rgba(255,136,0,0.2);color:#ff8800;border:1px solid rgba(255,136,0,0.4);border-radius:3px;padding:1px 5px;margin-left:4px;font-weight:700">BEAR</span>' if sym in bear_syms else "")
            cc = "#00ff88" if c["change"]>=0 else "#ff4466"
            chg_s = "+" if c["change"]>=0 else ""
            price_s = f"${c['price']:.4f}" if c['price'] < 10 else f"${c['price']:.2f}"
            row_glow = "border-left:2px solid #00ff88;background:rgba(0,255,136,0.04);" if all_pass else (
                       "border-left:2px solid #333;" if sc < MIN_SIGNAL_SCORE-1 else "border-left:2px solid #ffcc00;background:rgba(255,204,0,0.02);")
            rid = f"sc_{sym}"

            # ── Desktop row (hidden on mobile) ──
            rows += f"""
<tr class="scan-row-desktop" onclick="toggleScan('{rid}')" style="cursor:pointer;{row_glow}">
  <td style="font-weight:700;color:{color};white-space:nowrap">{sym}{bear_badge}</td>
  <td style="color:#e0e0e0">{price_s}</td>
  <td style="color:{cc};font-weight:700">{chg_s}{c["change"]:.2f}%</td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_scr}
      <span style="font-size:11px;font-weight:700;color:{"#00ff88" if sc>=MIN_SIGNAL_SCORE else "#ffcc00" if sc>=MIN_SIGNAL_SCORE-1 else "#475569"}">{v_scr}</span>
    </span>
  </td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_ema}
      <span style="font-size:11px;color:{"#00ff88" if ema_gap and ema_gap>0 else "#ffcc00" if ema_gap and ema_gap>-0.5 else "#475569"}">{v_ema}</span>
    </span>
  </td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_rsi}
      <span style="font-size:11px;color:{"#00ff88" if c.get("rsi") and 50<=c["rsi"]<=65 else "#ffcc00" if c.get("rsi") and c["rsi"]<=75 else "#ff4466" if c.get("rsi") and c["rsi"]>75 else "#475569"}">{v_rsi}</span>
    </span>
  </td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_vol}
      <span style="font-size:11px;color:{"#00ff88" if c.get("vol_ratio",0)>=1.5 else "#ffcc00" if c.get("vol_ratio",0)>=1.2 else "#475569"}">{v_vol}</span>
    </span>
  </td>
  <td style="text-align:center">{g_vap}</td>
  <td style="text-align:center">{g_sec}<span style="font-size:10px;color:#475569;margin-left:3px">{v_sec}</span></td>
</tr>
<tr id="{rid}_detail" class="scan-row-desktop" style="display:none;background:rgba(255,255,255,0.02)">
  <td colspan="9" style="padding:10px 16px;font-size:12px;color:#475569;border-bottom:1px solid rgba(255,255,255,0.04)">
    Score {v_scr} · EMA {v_ema} · RSI {v_rsi} · Vol {v_vol} · VWAP {v_vap} · Sector {v_sec}
    {"&nbsp;&nbsp;<span style=\"color:#00ff88;font-weight:700\">✅ ALL GATES PASS — eligible to trade</span>" if all_pass else ""}
  </td>
</tr>"""

            # ── Mobile row (hidden on desktop) ──
            rows += f"""
<tr class="scan-row-mobile" onclick="toggleScan('{rid}_mob')" style="cursor:pointer;{row_glow}">
  <td style="font-weight:700;color:{color};font-size:13px;padding-right:4px">{sym}</td>
  <td style="font-size:12px;color:#e0e0e0">{price_s}</td>
  <td style="font-size:12px;color:{cc}">{chg_s}{c["change"]:.1f}%</td>
  <td style="text-align:center;padding:0 2px">{g_scr}</td>
  <td style="text-align:center;padding:0 2px">{g_ema}</td>
  <td style="text-align:center;padding:0 2px">{g_rsi}</td>
  <td style="text-align:center;padding:0 2px">{g_vol}</td>
  <td style="text-align:center;padding:0 2px">{g_vap}</td>
  <td style="text-align:center;padding:0 2px">{g_sec}</td>
</tr>
<tr id="{rid}_mob_detail" class="scan-row-mobile" style="display:none;background:rgba(255,255,255,0.02)">
  <td colspan="9" style="padding:8px 12px;font-size:11px;color:#475569">
    SCR:{v_scr} EMA:{v_ema} RSI:{v_rsi} Vol:{v_vol} VAP:{v_vap} Sec:{v_sec}
  </td>
</tr>"""

        scanner_css = """<style>
@media(min-width:600px){.scan-row-mobile{display:none!important}}
@media(max-width:599px){.scan-row-desktop{display:none!important}}
</style>"""
        return (
            f'{scanner_css}'
            f'<div style="display:flex;gap:18px;margin-bottom:14px;font-size:14px;flex-wrap:wrap">'
            f'<span style="color:#00ff88;font-weight:700">🟢 {buys} READY</span>'
            f'<span style="color:#ffcc00;font-weight:700">⚡ {watch} CLOSE</span>'
            f'<span style="color:#475569;margin-left:auto">{len(scored)} scanned</span></div>'
            f'<div style="overflow-x:auto">'
            f'<table><thead>'
            f'<tr class="scan-row-desktop"><th style="font-size:13px">Symbol</th><th style="font-size:13px">Price</th><th style="font-size:13px">Chg%</th>'
            f'<th style="font-size:13px;text-align:center">SCR</th><th style="font-size:13px;text-align:center">EMA</th><th style="font-size:13px;text-align:center">RSI</th><th style="font-size:13px;text-align:center">VOL</th><th style="font-size:13px;text-align:center">VAP</th></tr>'
            f'<tr class="scan-row-mobile" style="font-size:11px;color:#8899aa">'
            f'<th>SYM</th><th>PRICE</th><th>CHG</th>'
            f'<th style="text-align:center">S</th><th style="text-align:center">E</th>'
            f'<th style="text-align:center">R</th><th style="text-align:center">V</th>'
            f'<th style="text-align:center">P</th><th style="text-align:center">X</th></tr>'
            f'</thead><tbody>{rows}</tbody></table></div>'
            f'<script>function toggleScan(id){{var d=document.getElementById(id+"_detail");if(d)d.style.display=d.style.display==="none"?"table-row":"none";}}</script>'
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

    # ── READY TO TRADE — traffic light gate rows ──
    def ready_to_trade_rows(scored, color, label, held_syms=set()):
        rows = ""
        for sc, ema_gap, c in scored:
            if sc < MIN_SIGNAL_SCORE: continue
            if ema_gap is None or ema_gap <= 0: continue
            sym = c["symbol"]
            g_scr,g_ema,g_rsi,g_vol,g_vap,g_sec,v_scr,v_ema,v_rsi,v_vol,v_vap,v_sec,all_pass = _gates(sc,ema_gap,c,held_syms)
            cc    = "#00ff88" if c["change"]>=0 else "#ff4466"
            chg_s = "+" if c["change"]>=0 else ""
            price_s = f"${c['price']:.4f}" if c['price'] < 10 else f"${c['price']:.2f}"
            row_border = "border-left:3px solid #00ff88;" if all_pass else "border-left:3px solid #ffcc00;"
            row_bg     = "background:rgba(0,255,136,0.05);" if all_pass else "background:rgba(255,204,0,0.02);"
            rid = f"rtt_{sym}_{label}"

            scr_c = "#00ff88" if sc>=MIN_SIGNAL_SCORE else "#ffcc00" if sc>=MIN_SIGNAL_SCORE-1 else "#475569"
            ema_c2 = "#00ff88" if ema_gap and ema_gap>0 else "#ffcc00" if ema_gap and ema_gap>-0.5 else "#475569"
            rsi_c2 = "#00ff88" if c.get("rsi") and 50<=c["rsi"]<=65 else "#ffcc00" if c.get("rsi") and c["rsi"]<=75 else "#ff4466" if c.get("rsi") and c["rsi"]>75 else "#475569"
            vol_c2 = "#00ff88" if c.get("vol_ratio",0)>=1.5 else "#ffcc00" if c.get("vol_ratio",0)>=1.2 else "#475569"
            rows += f"""<div style="margin-bottom:4px">
<!-- Desktop RTT: dot + value stacked, larger -->
<div class="rtt-desktop" onclick="toggleRTT('{rid}')" style="display:grid;grid-template-columns:100px 80px 58px repeat(5,1fr);
  align-items:center;gap:6px;padding:13px 16px;border-radius:10px;{row_border}{row_bg}cursor:pointer">
  <div style="font-weight:700;color:{color};font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{sym}</div>
  <div style="font-size:14px;color:#d0d8e0">{price_s}</div>
  <div style="font-size:13px;font-weight:700;color:{cc}">{chg_s}{c["change"]:.1f}%</div>
  <div style="display:flex;flex-direction:column;align-items:center;gap:2px">{g_scr}<span style="font-size:11px;font-weight:700;color:{scr_c}">{v_scr}</span></div>
  <div style="display:flex;flex-direction:column;align-items:center;gap:2px">{g_ema}<span style="font-size:11px;color:{ema_c2}">{v_ema}</span></div>
  <div style="display:flex;flex-direction:column;align-items:center;gap:2px">{g_rsi}<span style="font-size:11px;color:{rsi_c2}">{v_rsi}</span></div>
  <div style="display:flex;flex-direction:column;align-items:center;gap:2px">{g_vol}<span style="font-size:11px;color:{vol_c2}">{v_vol}</span></div>
  <div style="display:flex;flex-direction:column;align-items:center;gap:2px">{g_vap}<span style="font-size:11px;color:#8899aa">{v_vap}</span></div>
</div>
<!-- Mobile RTT: tight dots only, fits in box -->
<div class="rtt-mobile" onclick="toggleRTT('{rid}_mob')" style="display:grid;grid-template-columns:60px 56px 40px repeat(6,24px);
  align-items:center;gap:2px;padding:8px 8px;border-radius:8px;{row_border}{row_bg}cursor:pointer">
  <div style="font-weight:700;color:{color};font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{sym}</div>
  <div style="font-size:11px;color:#e0e0e0">{price_s}</div>
  <div style="font-size:10px;color:{cc}">{chg_s}{c["change"]:.1f}%</div>
  <div style="text-align:center">{g_scr}</div>
  <div style="text-align:center">{g_ema}</div>
  <div style="text-align:center">{g_rsi}</div>
  <div style="text-align:center">{g_vol}</div>
  <div style="text-align:center">{g_vap}</div>
  <div style="text-align:center">{g_sec}</div>
</div>
<div id="{rid}_detail" style="display:none;padding:14px 18px 16px;font-size:13px;color:#b0bec5;
  background:rgba(255,255,255,0.05);border-radius:0 0 10px 10px;
  border:1px solid rgba(255,255,255,0.08);border-top:none">
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">
    <div><b style="color:#e8edf2;font-size:12px;text-transform:uppercase;letter-spacing:0.5px">Score</b><br><span style="font-size:15px;font-weight:700;color:#e0e0e0">{v_scr}</span></div>
    <div><b style="color:#e8edf2;font-size:12px;text-transform:uppercase;letter-spacing:0.5px">EMA</b><br><span style="font-size:15px;font-weight:700;color:#e0e0e0">{v_ema}</span></div>
    <div><b style="color:#e8edf2;font-size:12px;text-transform:uppercase;letter-spacing:0.5px">RSI</b><br><span style="font-size:15px;font-weight:700;color:#e0e0e0">{v_rsi}</span></div>
    <div><b style="color:#e8edf2;font-size:12px;text-transform:uppercase;letter-spacing:0.5px">Volume</b><br><span style="font-size:15px;font-weight:700;color:#e0e0e0">{v_vol}</span></div>
    <div><b style="color:#e8edf2;font-size:12px;text-transform:uppercase;letter-spacing:0.5px">VWAP</b><br><span style="font-size:15px;font-weight:700;color:#e0e0e0">{v_vap}</span></div>
    <div><b style="color:#e8edf2;font-size:12px;text-transform:uppercase;letter-spacing:0.5px">Sector</b><br><span style="font-size:15px;font-weight:700;color:#e0e0e0">{v_sec}</span></div>
  </div>
  {"<div style=\"margin-top:10px;color:#00ff88;font-weight:700;font-size:13px\">✅ ALL GATES PASS — bot will execute</div>" if all_pass else "<div style=\"margin-top:10px;color:#ffcc00;font-size:13px\">⚠ Some gates failing — held back</div>"}
</div>
<div id="{rid}_mob_detail" style="display:none;padding:10px 12px;font-size:12px;color:#b0bec5;
  background:rgba(255,255,255,0.05);border-radius:0 0 8px 8px;border:1px solid rgba(255,255,255,0.08);border-top:none">
  SCR:<b style="color:#e0e0e0">{v_scr}</b> · EMA:<b style="color:#e0e0e0">{v_ema}</b> · RSI:<b style="color:#e0e0e0">{v_rsi}</b> · Vol:<b style="color:#e0e0e0">{v_vol}</b> · VAP:<b style="color:#e0e0e0">{v_vap}</b>{"<span style=\"color:#00ff88;font-weight:700\"> ✅</span>" if all_pass else ""}
</div>
</div>"""
        return rows

    _held_syms = set(sym for sym,_,_c,_t in all_pos) if all_pos else set()
    rtt_rows = (
        ready_to_trade_rows(crypto_scored, "#00ff88", "Crypto", _held_syms) +
        ready_to_trade_rows(us_scored,     "#00aaff", "US",     _held_syms) +
        ready_to_trade_rows(ftse_scored,   "#cc88ff", "FTSE",   _held_syms) +
        ready_to_trade_rows(asx_scored,    "#ffaa00", "ASX",    _held_syms) +
        ready_to_trade_rows(sc_scored,     "#ffcc00", "SmCap",  _held_syms)
    )
    if rtt_rows:
        ready_to_trade_html = (
            f'<div class="card" style="margin-bottom:16px;border-color:rgba(0,255,136,0.3);background:rgba(0,255,136,0.03)">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
            f'<div class="section-title" style="color:#00ff88;margin-bottom:0">🟢 READY TO TRADE</div>'
            f'<div style="font-size:12px;color:#475569">Score ≥ {MIN_SIGNAL_SCORE} + EMA crossed</div>'
            f'</div>'
            f'<style>@media(min-width:600px){{.rtt-mobile{{display:none!important}}}}@media(max-width:599px){{.rtt-desktop{{display:none!important}}}}</style>'
            f'<div class="rtt-desktop" style="display:grid;grid-template-columns:100px 80px 58px repeat(5,1fr);'
            f'gap:6px;padding:4px 16px 10px;font-size:11px;letter-spacing:1px;color:#8899aa;font-weight:700;text-transform:uppercase">'
            f'<div>Symbol</div><div>Price</div><div>Chg%</div>'
            f'<div style="text-align:center">SCR</div><div style="text-align:center">EMA</div>'
            f'<div style="text-align:center">RSI</div><div style="text-align:center">VOL</div>'
            f'<div style="text-align:center">VAP</div>'
            f'</div>'
            f'<div class="rtt-mobile" style="display:grid;grid-template-columns:60px 56px 40px repeat(6,24px);'
            f'gap:2px;padding:3px 8px 8px;font-size:9px;color:#475569;font-weight:700;text-transform:uppercase">'
            f'<div>Sym</div><div>Price</div><div>Chg</div>'
            f'<div style="text-align:center">S</div><div style="text-align:center">E</div>'
            f'<div style="text-align:center">R</div><div style="text-align:center">V</div>'
            f'<div style="text-align:center">P</div><div style="text-align:center">X</div>'
            f'</div>'
            f'{rtt_rows}'
            f'<script>function toggleRTT(id){{var d=document.getElementById(id+"_detail");if(d)d.style.display=d.style.display==="none"?"block":"none";}}</script>'
            f'</div>'
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
                    sym = c["symbol"]
                    g_scr,g_ema,g_rsi,g_vol,g_vap,g_sec,v_scr,v_ema,v_rsi,v_vol,v_vap,v_sec,all_pass = _gates(sc,ema_gap,c,_held_syms_gates)
                    bear_badge = ('<span style="font-size:9px;background:rgba(255,136,0,0.2);color:#ff8800;border:1px solid rgba(255,136,0,0.4);border-radius:3px;padding:1px 5px;margin-left:4px;font-weight:700">BEAR</span>' if sym in bear_syms else "")
                    cc = "#00ff88" if c["change"]>=0 else "#ff4466"
                    chg_s = "+" if c["change"]>=0 else ""
                    price_s = f"${c['price']:.4f}" if c['price'] < 10 else f"${c['price']:.2f}"
                    row_glow = "border-left:3px solid #00ff88;background:rgba(0,255,136,0.05);" if all_pass else (
                               "border-left:3px solid #1a2a1a;" if sc < MIN_SIGNAL_SCORE-1 else "border-left:3px solid #ffcc00;background:rgba(255,204,0,0.03);")
                    rid = f"sc_{sym}"
                    scr_c = "#00ff88" if sc>=MIN_SIGNAL_SCORE else "#ffcc00" if sc>=MIN_SIGNAL_SCORE-1 else "#8899aa"
                    ema_c = "#00ff88" if ema_gap and ema_gap>0 else "#ffcc00" if ema_gap and ema_gap>-0.5 else "#8899aa"
                    rsi_c = "#00ff88" if c.get("rsi") and 50<=c["rsi"]<=65 else "#ffcc00" if c.get("rsi") and c["rsi"]<=75 else "#ff4466" if c.get("rsi") and c["rsi"]>75 else "#8899aa"
                    vol_c = "#00ff88" if c.get("vol_ratio",0)>=1.5 else "#ffcc00" if c.get("vol_ratio",0)>=1.2 else "#8899aa"
                    rows += f"""
<tr class="scan-row-desktop" onclick="toggleScan('{rid}')" style="cursor:pointer;{row_glow}">
  <td style="font-weight:700;font-size:14px;color:{color};white-space:nowrap;padding:10px 12px">{sym}{bear_badge}</td>
  <td style="color:#d0d8e0;font-size:13px;padding:10px 8px">{price_s}</td>
  <td style="color:{cc};font-weight:700;font-size:13px;padding:10px 8px">{chg_s}{c["change"]:.2f}%</td>
  <td style="text-align:center;padding:10px 6px"><span style="display:inline-flex;align-items:center;gap:5px">{g_scr}<span style="font-size:12px;font-weight:700;color:{scr_c}">{v_scr}</span></span></td>
  <td style="text-align:center;padding:10px 6px"><span style="display:inline-flex;align-items:center;gap:5px">{g_ema}<span style="font-size:12px;color:{ema_c}">{v_ema}</span></span></td>
  <td style="text-align:center;padding:10px 6px"><span style="display:inline-flex;align-items:center;gap:5px">{g_rsi}<span style="font-size:12px;color:{rsi_c}">{v_rsi}</span></span></td>
  <td style="text-align:center;padding:10px 6px"><span style="display:inline-flex;align-items:center;gap:5px">{g_vol}<span style="font-size:12px;color:{vol_c}">{v_vol}</span></span></td>
  <td style="text-align:center;padding:10px 6px">{g_vap}</td>
</tr>
<tr id="{rid}_detail" class="scan-row-desktop" style="display:none;background:rgba(255,255,255,0.04)">
  <td colspan="9" style="padding:12px 18px;font-size:13px;color:#b0bec5;border-bottom:1px solid rgba(255,255,255,0.06)">
    Score <b style="color:#e0e0e0">{v_scr}</b> · EMA <b style="color:#e0e0e0">{v_ema}</b> · RSI <b style="color:#e0e0e0">{v_rsi}</b> · Vol <b style="color:#e0e0e0">{v_vol}</b> · VWAP <b style="color:#e0e0e0">{v_vap}</b>{"&nbsp;&nbsp;<span style=\"color:#00ff88;font-weight:700\">✅ ALL GATES PASS — bot will execute</span>" if all_pass else ""}
  </td>
</tr>
<tr class="scan-row-mobile" onclick="toggleScan('{rid}_mob')" style="cursor:pointer;{row_glow}">
  <td style="font-weight:700;color:{color};font-size:13px;padding:9px 6px">{sym}</td>
  <td style="font-size:12px;color:#d0d8e0;padding:9px 4px">{price_s}</td>
  <td style="font-size:12px;color:{cc};padding:9px 4px">{chg_s}{c["change"]:.1f}%</td>
  <td style="text-align:center;padding:9px 2px">{g_scr}</td>
  <td style="text-align:center;padding:9px 2px">{g_ema}</td>
  <td style="text-align:center;padding:9px 2px">{g_rsi}</td>
  <td style="text-align:center;padding:9px 2px">{g_vol}</td>
  <td style="text-align:center;padding:9px 2px">{g_vap}</td>
</tr>
<tr id="{rid}_mob_detail" class="scan-row-mobile" style="display:none;background:rgba(255,255,255,0.04)">
  <td colspan="9" style="padding:10px 12px;font-size:12px;color:#b0bec5">
    SCR:<b style="color:#e0e0e0">{v_scr}</b> · EMA:<b style="color:#e0e0e0">{v_ema}</b> · RSI:<b style="color:#e0e0e0">{v_rsi}</b> · Vol:<b style="color:#e0e0e0">{v_vol}</b> · VAP:<b style="color:#e0e0e0">{v_vap}</b>{"<span style=\"color:#00ff88;font-weight:700\"> ✅</span>" if all_pass else ""}
  </td>
</tr>"""
                return rows

            scanner_css_mkt = """<style>
@media(min-width:600px){.scan-row-mobile{display:none!important}}
@media(max-width:599px){.scan-row-desktop{display:none!important}}
</style>"""
            thead = (scanner_css_mkt +
                     '<div style="overflow-x:auto"><table class="scan-table"><thead>'
                     '<tr class="scan-row-desktop"><th style="font-size:13px">Symbol</th><th style="font-size:13px">Price</th><th style="font-size:13px">Chg%</th>'
                     '<th style="font-size:13px;text-align:center">SCR</th><th style="font-size:13px;text-align:center">EMA</th><th style="font-size:13px;text-align:center">RSI</th><th style="font-size:13px;text-align:center">VOL</th><th style="font-size:13px;text-align:center">VAP</th></tr>'
                     '<tr class="scan-row-mobile" style="font-size:11px;color:#8899aa">'
                     '<th>SYM</th><th>PRICE</th><th>CHG</th>'
                     '<th style="text-align:center">S</th><th style="text-align:center">E</th>'
                     '<th style="text-align:center">R</th><th style="text-align:center">V</th>'
                     '<th style="text-align:center">P</th></tr>'
                     '</thead><tbody>')
            tfoot = '</tbody></table></div><script>function toggleScan(id){var d=document.getElementById(id+"_detail");if(d)d.style.display=d.style.display==="none"?"table-row":"none";}</script>'
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
        f'<div style="font-size:13px;color:#475569">Open markets expanded · top 10 shown/collapse</div>'
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
<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:14px">
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
    <div class="lbl" style="color:#00aaff">TODAY</div>
    <div style="font-size:22px;font-weight:700;color:{_col(d0["pnl"])};margin:6px 0">{_fmt(d0["pnl"])}</div>
    <div style="font-size:12px;color:#475569;margin-top:3px">{d0["t"]} trades · {d0["wr"]}% win</div>
    <div style="font-size:12px;color:#475569">avg {_fmt(d0["avg"])} · <span style="color:{_col(d0["pnl"])}">{d0["pct"]:+.2f}%</span></div>
  </div>
  <div class="card">
    <div class="lbl">{d1["name"]}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(d1["pnl"])};margin:6px 0">{_fmt(d1["pnl"])}</div>
    <div style="font-size:12px;color:#475569;margin-top:3px">{d1["t"]} trades · {d1["wr"]}% win</div>
    <div style="font-size:12px;color:#475569">avg {_fmt(d1["avg"])} · <span style="color:{_col(d1["pnl"])}">{d1["pct"]:+.2f}%</span></div>
  </div>
  <div class="card">
    <div class="lbl">{d2["name"]}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(d2["pnl"])};margin:6px 0">{_fmt(d2["pnl"])}</div>
    <div style="font-size:12px;color:#475569;margin-top:3px">{d2["t"]} trades · {d2["wr"]}% win</div>
    <div style="font-size:12px;color:#475569">avg {_fmt(d2["avg"])} · <span style="color:{_col(d2["pnl"])}">{d2["pct"]:+.2f}%</span></div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#00aaff">LAST 7 DAYS</div>
    <div style="font-size:22px;font-weight:700;color:{_col(week_pnl)};margin:6px 0">{_fmt(week_pnl)}</div>
    <div style="font-size:12px;color:#475569;margin-top:3px">{week_t} trades · {week_wr}% win</div>
    <div style="font-size:12px;color:#475569">best <span style="color:#00ff88">{week_best}</span> · worst <span style="color:#ff4466">{week_worst}</span></div>
  </div>
  <div class="card">
    <div class="lbl">ALL TIME</div>
    <div style="font-size:22px;font-weight:700;color:{_col(total_pnl_db)};margin:6px 0">{_fmt(total_pnl_db)}</div>
    <div style="font-size:12px;color:#475569;margin-top:3px">{total_t} trades · {win_rate}% win</div>
    <div style="font-size:12px;color:#475569">avg score {avg_sc_db:.1f}</div>
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
      <div><span style="color:#475569">Status </span><span class="dot" style="background:{'#00ff88' if st_states.get('us',{}).get('running') else ('#ff4466' if st_states.get('us',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('us',{}))}</div>
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
      <div><span style="color:#475569">Status </span><span class="dot" style="background:{'#00ff88' if st_states.get('crypto',{}).get('running') else ('#ff4466' if st_states.get('crypto',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('crypto',{}))}</div>
      <div><span style="color:#475569">BTC </span><b>{btc_str}</b></div>
      <div><span style="color:#475569">MA14 </span><span style="color:#888">${st_crypto_regime.get('btc_ma20',0):.0f}</span></div>
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
      <div><span style="color:#475569">Status </span><span class="dot" style="background:{'#00ff88' if st_states.get('smallcap',{}).get('running') else ('#ff4466' if st_states.get('smallcap',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('smallcap',{}))}</div>
      <div><span style="color:#475569">Pool </span>{len(smallcap_pool.get('symbols',[]))}</div>
      <div><span style="color:#475569">Positions </span><b>{st_states.get('smallcap',{}).get('positions',0)}</b></div>
      <div><span style="color:#475569">Cycle </span>#{st_states.get('smallcap',{}).get('cycle',0)}</div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(170,136,255,0.2)">
    <div style="font-size:16px;font-weight:700;color:#aa88ff;margin-bottom:10px">⚡ Intraday</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#475569">Stocks </span><span class="dot" style="background:{'#00ff88' if st_states.get('intraday',{}).get('running') else ('#ff4466' if st_states.get('intraday',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('intraday',{}))}</div>
      <div><span style="color:#475569">ID Cycle </span>#{st_states.get('intraday',{}).get('cycle',0)}</div>
      <div><span style="color:#475569">ID Pos </span>{st_states.get('intraday',{}).get('positions',0)}</div>
      <div><span style="color:#475569">Crypto </span><span class="dot {_dot(st_states.get('crypto_id',{}))}"></span>{_status(st_states.get('crypto_id',{}))}</div>
    </div>
  </div>
</div>

{trades_html}
{positions_html}
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
            f'<td style="font-weight:700;color:#00aaff">{sym} <span title="{disc_label}" style="font-size:10px;background:rgba(255,255,255,0.06);color:{disc_col};border:1px solid {disc_col}44;border-radius:4px;padding:1px 5px;margin-left:3px;font-weight:700">{disc_icon}</span></td>'
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
# SETTINGS PANEL
# ═══════════════════════════════════════════════════════════════
def _build_settings_page(msg=None, msg_type="ok"):
    c = _load_tcfg()
    def v(k, default=""): return c.get(k, default)
    msg_html = ""
    if msg:
        col = "#00ff88" if msg_type == "ok" else "#ff4466"
        msg_html = f'''<div style="background:rgba(0,255,136,0.08);border:1px solid {col};border-radius:10px;padding:14px 20px;margin-bottom:20px;color:{col};font-weight:700">
            {"✅" if msg_type == "ok" else "❌"} {msg}</div>'''

    def row(label, key, default, typ="number", step="1", note=""):
        val = v(key, default)
        return f'''<div style="display:grid;grid-template-columns:220px 140px 1fr;align-items:center;gap:16px;padding:12px 0;border-bottom:1px solid rgba(255,255,255,0.04)">
            <div>
              <div style="font-size:13px;font-weight:700;color:#e0e0e0">{label}</div>
              {"<div style=\'font-size:11px;color:#475569;margin-top:3px\'>" + note + "</div>" if note else ""}
            </div>
            <input name="{key}" type="{typ}" step="{step}" value="{val}"
              style="background:#0d1117;border:1px solid rgba(255,255,255,0.12);border-radius:8px;color:#00ff88;
                     font-family:\'JetBrains Mono\',monospace;font-size:15px;font-weight:700;padding:9px 14px;width:100%;text-align:right">
            <div style="font-size:11px;color:#475569">{note if not note else ""}</div>
        </div>'''

    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot Settings</title>
{BASE_CSS}
<style>
.settings-section{{background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.07);border-radius:14px;padding:22px 26px;margin-bottom:20px}}
.settings-section-title{{font-family:\'Syne\',sans-serif;font-size:16px;font-weight:700;color:#ffcc00;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,0.06)}}
input[type=number]{{-moz-appearance:textfield}}
input[type=number]::-webkit-outer-spin-button,input[type=number]::-webkit-inner-spin-button{{-webkit-appearance:none;margin:0}}
#pin-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:999;align-items:center;justify-content:center}}
#pin-overlay.visible{{display:flex}}
.pin-box{{background:#0d1117;border:1px solid rgba(255,204,0,0.3);border-radius:16px;padding:36px 40px;text-align:center;max-width:360px;width:90%}}
</style>
</head><body>
<div class="header">
  <div class="logo">Alpha<span>Bot</span> <span style="color:#ffcc00;font-size:15px">⚙ Settings</span></div>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="badge {"badge-live" if IS_LIVE else "badge-paper"}">{"● LIVE" if IS_LIVE else "◎ PAPER"}</span>
  </div>
</div>
<div class="controls-bar">
  <a href="/" class="tab" style="text-decoration:none">← Dashboard</a>
  <a href="/analytics" class="tab" style="text-decoration:none">📊 Intelligence</a>
  <a href="/settings" class="tab" style="text-decoration:none;color:#ffcc00;border-bottom:2px solid #ffcc00">⚙️ Settings</a>
</div>

<div class="container" style="max-width:860px">
  {msg_html}

  <div style="background:rgba(255,204,0,0.06);border:1px solid rgba(255,204,0,0.2);border-radius:10px;padding:14px 18px;margin-bottom:22px;font-size:13px;color:#ffcc00">
    ⚡ Changes apply within <strong>60 seconds</strong> — no restart needed. PIN required to save.
  </div>

  <form id="settings-form">

    <div class="settings-section">
      <div class="settings-section-title">🎯 Signal & Position Limits</div>
      {row("Min Signal Score", "MIN_SIGNAL_SCORE", 5, step="1", note="5 = paper, 7+ = live")}
      {row("Max Positions Per Strategy", "MAX_POSITIONS", 3, step="1")}
      {row("Max Total Positions", "MAX_TOTAL_POSITIONS", 15, step="1")}
      {row("Max Trades Per Day", "MAX_TRADES_PER_DAY", 50, step="1")}
      {row("Max Sector Positions", "MAX_SECTOR_POSITIONS", 1, step="1")}
    </div>

    <div class="settings-section">
      <div class="settings-section-title">🛑 Stop Loss & Profit Targets</div>
      {row("Stop Loss %", "STOP_LOSS_PCT", 5.0, step="0.1", note="Swing trades")}
      {row("Trailing Stop %", "TRAILING_STOP_PCT", 2.0, step="0.1")}
      {row("Take Profit %", "TAKE_PROFIT_PCT", 10.0, step="0.1", note="Swing trades")}
      {row("Crypto Stop %", "CRYPTO_STOP_PCT", 4.0, step="0.1")}
      {row("Max Hold Days", "MAX_HOLD_DAYS", 5, step="1")}
    </div>

    <div class="settings-section">
      <div class="settings-section-title">⚡ Intraday Settings</div>
      {row("Intraday Stop Loss %", "INTRADAY_STOP_LOSS", 1.0, step="0.1")}
      {row("Intraday Take Profit %", "INTRADAY_TAKE_PROFIT", 2.5, step="0.1")}
      {row("Max Intraday Positions", "INTRADAY_MAX_POSITIONS", 2, step="1")}
      {row("Crypto Intraday Stop %", "CRYPTO_INTRADAY_SL", 1.0, step="0.1")}
      {row("Crypto Intraday TP %", "CRYPTO_INTRADAY_TP", 2.0, step="0.1")}
      {row("Max Crypto Intraday Positions", "CRYPTO_INTRADAY_MAX_POS", 2, step="1")}
    </div>

    <div class="settings-section">
      <div class="settings-section-title">💰 Risk & Exposure Limits</div>
      {row("Max Daily Loss %", "MAX_DAILY_LOSS_PCT", 0.5, step="0.1", note="% of portfolio")}
      {row("Daily Profit Target %", "DAILY_PROFIT_TARGET_PCT", 2.0, step="0.1")}
      {row("Max Daily Spend %", "MAX_DAILY_SPEND_PCT", 50.0, step="1.0")}
      {row("Max Portfolio Exposure %", "MAX_EXPOSURE_PCT", 30.0, step="1.0")}
      {row("Max Trade Size %", "MAX_TRADE_PCT", 5.0, step="0.5")}
      {row("Crypto Exposure %", "CRYPTO_EXPOSURE_PCT", 20.0, step="1.0")}
    </div>

    <div class="settings-section">
      <div class="settings-section-title">🔧 Bot Cycle</div>
      {row("Cycle Seconds", "CYCLE_SECONDS", 60, step="5", note="Main loop interval (seconds)")}
      {row("Loss Streak Pause Limit", "LOSS_STREAK_LIMIT", 3, step="1")}
      {row("VIX High Threshold", "VIX_HIGH_THRESHOLD", 25.0, step="1.0")}
      {row("VIX Extreme Threshold", "VIX_EXTREME", 35.0, step="1.0")}
    </div>

    <div style="text-align:center;padding:10px 0 30px">
      <button type="button" onclick="showPin()"
        style="background:rgba(255,204,0,0.15);border:1px solid rgba(255,204,0,0.4);border-radius:10px;
               color:#ffcc00;font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700;
               padding:16px 48px;cursor:pointer;letter-spacing:1px">
        🔒 SAVE SETTINGS
      </button>
    </div>
  </form>
</div>

<!-- PIN overlay -->
<div id="pin-overlay" onclick="if(event.target===this)hidePin()">
  <div class="pin-box">
    <div style="font-size:20px;font-weight:700;color:#ffcc00;margin-bottom:8px">🔒 Enter PIN</div>
    <div style="font-size:13px;color:#475569;margin-bottom:20px">Required to save settings</div>
    <input id="pin-input" type="password" maxlength="10" placeholder="••••"
      style="background:#111;border:1px solid rgba(255,204,0,0.3);border-radius:8px;color:#ffcc00;
             font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:700;padding:12px;
             width:100%;text-align:center;letter-spacing:4px;margin-bottom:16px"
      onkeydown="if(event.key==='Enter')submitSettings()">
    <div style="display:flex;gap:10px">
      <button onclick="hidePin()"
        style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#475569;
               padding:12px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:13px">
        Cancel
      </button>
      <button onclick="submitSettings()"
        style="flex:2;background:rgba(255,204,0,0.15);border:1px solid rgba(255,204,0,0.4);border-radius:8px;
               color:#ffcc00;padding:12px;cursor:pointer;font-family:'JetBrains Mono',monospace;
               font-size:13px;font-weight:700">
        Save Settings
      </button>
    </div>
    <div id="pin-error" style="color:#ff4466;font-size:12px;margin-top:10px;display:none">Wrong PIN</div>
  </div>
</div>

<script>
function showPin(){{document.getElementById('pin-overlay').classList.add('visible');document.getElementById('pin-input').focus();}}
function hidePin(){{document.getElementById('pin-overlay').classList.remove('visible');document.getElementById('pin-error').style.display='none';}}
function submitSettings(){{
  var pin=document.getElementById('pin-input').value;
  var form=document.getElementById('settings-form');
  var inputs=form.querySelectorAll('input[name]');
  var data={{}};
  inputs.forEach(function(i){{data[i.name]=i.type==='number'?parseFloat(i.value):i.value;}});
  fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{pin:pin,settings:data}})  }})
  .then(r=>r.json()).then(d=>{{
    if(d.status==='ok'){{hidePin();window.location.href='/settings?msg=saved'}}
    else if(d.status==='wrong_pin'){{document.getElementById('pin-error').style.display='block'}}
    else{{alert('Error: '+JSON.stringify(d))}}
  }});
}}
</script>
</body></html>'''
