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
    db_missed_profit_total, db_missed_profit_summary,
    db_capacity_skips, db_threshold_sensitivity,
    db_edge_by_discipline_and_score, db_performance_by_regime,
    db_entry_gate_attribution, db_rotation_summary, db_exit_category_breakdown,
    db_get_pending_recommendations, db_get_recommendation_history,
    db_apply_recommendation, db_dismiss_recommendation, db_snooze_recommendation,
    db_get_latest_intelligence_run, db_get_intelligence_runs,
    db_ev_by_discipline, db_discipline_detail,
    db_log_config_change, db_get_config_history,
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
.logo span{color:#94a3b8}
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
.lbl{font-size:11px;letter-spacing:2px;color:#94a3b8;text-transform:uppercase;margin-bottom:6px}
.big{font-size:26px;font-weight:700;font-family:'Syne',sans-serif}
.green{color:#00ff88}.blue{color:#00aaff}.red{color:#ff4466}.gold{color:#ffcc00}.grey{color:#94a3b8}
.section-title{font-size:17px;font-weight:700;margin-bottom:16px;font-family:'Syne',sans-serif}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-size:11px;color:#94a3b8;letter-spacing:1.5px;text-transform:uppercase;padding:12px 14px;text-align:left;font-weight:600}
td{padding:11px 14px;border-top:1px solid rgba(255,255,255,0.04);font-family:'JetBrains Mono',monospace}
tr:hover td{background:rgba(255,255,255,0.025)}
.sig-buy{background:rgba(0,255,136,0.1);color:#00ff88;border:1px solid #00ff88;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700}
.sig-sell{background:rgba(255,68,102,0.1);color:#ff4466;border:1px solid #ff4466;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700}
.sig-hold{background:rgba(255,255,255,0.05);color:#94a3b8;border:1px solid #333;padding:3px 10px;border-radius:5px;font-size:12px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
.dot-green{background:#00ff88;box-shadow:0 0 6px #00ff88}
.dot-red{background:#ff4466;box-shadow:0 0 6px #ff4466}
.dot-gold{background:#ffcc00;box-shadow:0 0 6px #ffcc00}
.dot-amber{background:#ffaa00;box-shadow:0 0 6px #ffaa00}
.dot-purple{background:#cc88ff;box-shadow:0 0 6px #cc88ff}
.tab-bar{display:flex;border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:20px;flex-wrap:wrap}
.tab{padding:12px 18px;cursor:pointer;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#94a3b8;border-bottom:2px solid transparent;text-decoration:none}
.tab:hover{color:#e0e0e0}
.empty{text-align:center;padding:50px;color:#94a3b8;font-size:16px}
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
@media(max-width:820px){
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
  .pos-table-wrap{display:none !important}
  .pos-cards{display:block !important}
  .trades-table-wrap{display:none !important}
  .trades-cards{display:block !important}
  .pstrip{grid-template-columns:1fr 1fr 1fr !important;gap:6px !important}
  .pstrip .card{padding:8px 10px !important}
  .pstrip .big{font-size:18px !important}
  .scan-table th:nth-child(6),.scan-table td:nth-child(6),
  .scan-table th:nth-child(7),.scan-table td:nth-child(7),
  .scan-table th:nth-child(8),.scan-table td:nth-child(8){display:none}
  .scan-table td:nth-child(4),.scan-table td:nth-child(5){white-space:nowrap}
}
.pos-cards{display:none}
.trades-cards{display:none}
.pos-card{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:12px;padding:14px;margin-bottom:10px;cursor:pointer;-webkit-tap-highlight-color:rgba(0,255,136,0.1);user-select:none;-webkit-user-select:none}
.pos-card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.pos-card-sym{font-size:17px;font-weight:700;font-family:'Syne',sans-serif}
.pos-card-pnl{font-size:15px;font-weight:700;text-align:right;line-height:1.3}
.pos-card-row{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;margin-bottom:0}
.pos-card-item{display:flex;flex-direction:column;gap:2px}
.pos-card-label{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.5px}
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
                f'<td style="color:#94a3b8">{entry_dt}</td>'
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
                f'<div class="tap-hint" style="font-size:10px;color:#94a3b8;margin-top:3px">tap for more ▾</div>'
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
            f'<div class="section-title">CURRENTLY HOLDING ({len(all_pos)}) <span style="font-size:13px;color:#94a3b8;font-weight:400;font-family:\'JetBrains Mono\'"></span></div>'
            f'<div class="pos-table-wrap table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Type</th><th>Held</th><th>Purchased</th>'
            f'<th>Entry $</th><th>Live $</th><th>Stop</th><th>Position $</th><th>P&L</th>'
            f'</tr></thead><tbody>{pos_rows}</tbody></table></div>'
            f'<div class="pos-cards">{iphone_pos_cards}</div>'
            f'</div>'
            f'<script>'
            f'function toggleDetail(i){{var r=document.getElementById("det-"+i);r.style.display=r.style.display==="none"?"table-row":"none";}}'
            f'function toggleCard(i){{var d=document.getElementById("card-det-"+i);if(!d)return;d.style.display=d.style.display==="block"?"none":"block";}}'
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
            disc_icon, disc_col, disc_label = _disc_map.get(discipline, ("•","#94a3b8", discipline))
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
            mkt_col = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00","ASX":"#ffaa00","FTSE":"#cc88ff"}.get(market,"#94a3b8")
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
                sell_col = "#94a3b8"
            trade_rows += (
                f'<tr onclick="toggleTrade({t_idx})" style="cursor:pointer">'
                f'<td>{"✅" if pnl>0 else "❌"}</td>'
                f'<td style="font-weight:700;color:#00aaff">{sym} <span title="{disc_label}" style="font-size:10px;background:rgba(255,255,255,0.06);color:{disc_col};border:1px solid {disc_col}44;border-radius:4px;padding:1px 5px;margin-left:3px;font-weight:700">{disc_icon}</span></td>'
                f'<td style="color:{mkt_col};font-size:11px;font-weight:700">{market}</td>'
                f'<td style="color:#94a3b8">{date_s}</td>'
                f'<td style="color:#94a3b8">{time_s}</td>'
                f'<td style="color:#777">{price_s}</td>'
                f'<td style="color:#aaa">{qty_s}</td>'
                f'<td style="color:#aaa">{total_s}</td>'
                f'<td style="color:#94a3b8">{hold_s}</td>'
                f'<td style="color:{pc};font-weight:700">{sign}${pnl:.2f}</td>'
                f'<td style="color:{pc};font-weight:700">{(f"{sign}{abs(pnl/(price*qty)*100):.1f}%" if price and qty else "—")}</td>'
                f'<td style="color:#94a3b8">{score or "—"}</td>'
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
        # Visible line: Symbol · Market · Intraday badge (only if intraday) · % P/L
        # Tap expand: header row (Date+Score left, Entry Reason right) · metrics grid · exit reason
        iphone_trade_cards = ""
        _card_counter = 0
        for row in db_trades:
            sym_t,pnl_t,side_t,ts_t,score_t = row[0],row[1],row[2],row[3],row[4]
            qty_t = row[5] if len(row)>5 else None
            price_t = row[6] if len(row)>6 else None
            hold_t = row[7] if len(row)>7 else None
            market_t = row[8] if len(row)>8 else "—"
            pc_t = "#00ff88" if pnl_t>=0 else "#ff4466"
            sign_t = "+" if pnl_t>=0 else ""
            mkt_col_t = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00","ASX":"#ffaa00","FTSE":"#cc88ff"}.get(market_t,"#94a3b8")
            disc_t = row[9] if len(row)>9 else "swing"
            # Intraday badge only for intraday disciplines — swing trades get no badge
            _is_intraday = "intraday" in str(disc_t).lower()
            qty_s_t = f"{int(qty_t):,}" if qty_t else "—"
            total_s_t = f"${price_t*qty_t:,.0f}" if price_t and qty_t else "—"
            hold_s_t = f"{hold_t:.1f}h" if hold_t else "—"
            # % P/L with proper sign (negative for losses)
            if price_t and qty_t:
                _pct_val = pnl_t / (price_t * qty_t) * 100
                pct_s_t = f"{'+' if _pct_val >= 0 else ''}{_pct_val:.2f}%"
            else:
                pct_s_t = f"{sign_t}${pnl_t:.2f}"
            # Date parse
            try:
                _dt = datetime.fromisoformat(ts_t)
                if _dt.tzinfo is None: _dt = _dt.replace(tzinfo=ZoneInfo("UTC"))
                _dtp = _dt.astimezone(PARIS)
                date_full_t = _dtp.strftime("%a %d %b · %H:%M")
            except:
                date_full_t = ts_t[:16] if ts_t else "—"
            # Score — integer when whole, one decimal otherwise
            if isinstance(score_t, (int, float)) and score_t:
                score_disp_t = f"{int(score_t)}" if float(score_t).is_integer() else f"{score_t:.1f}"
            else:
                score_disp_t = "—"
            # Entry (buy) reason — inferred from score band (no separate column in DB)
            if isinstance(score_t, (int, float)):
                if score_t >= 8:
                    entry_reason_t = "🎯 Strong multi-signal confluence"
                elif score_t >= 6:
                    entry_reason_t = "📈 Solid setup — multiple gates aligned"
                elif score_t >= 5:
                    entry_reason_t = "✔ Threshold signal — baseline entry"
                else:
                    entry_reason_t = "• Low-score entry"
            else:
                entry_reason_t = "• Signal triggered"
            # Exit reason (mirrors desktop logic)
            if pnl_t < 0:
                sell_reason_t = "🛑 Stop loss triggered"
                sell_col_t = "#ff4466"
            elif hold_t and hold_t > 96:
                sell_reason_t = "⏱ Max hold reached — stale exit"
                sell_col_t = "#ffcc00"
            elif hold_t and hold_t < 0.5:
                sell_reason_t = "⚡ Quick scalp"
                sell_col_t = "#00ff88"
            elif pnl_t > 0:
                sell_reason_t = "🎯 Take profit hit"
                sell_col_t = "#00ff88"
            else:
                sell_reason_t = "— Position closed"
                sell_col_t = "#94a3b8"
            t_card_idx = _card_counter
            _card_counter += 1
            intraday_badge = (
                f'<span style="font-size:10px;background:rgba(170,136,255,0.12);color:#aa88ff;'
                f'border:1px solid rgba(170,136,255,0.35);border-radius:4px;padding:1px 6px;'
                f'font-weight:700;letter-spacing:0.5px">ID</span>'
            ) if _is_intraday else ''
            iphone_trade_cards += (
                f'<div onclick="toggleTradeCard({t_card_idx})" style="padding:12px 2px;border-bottom:1px solid rgba(255,255,255,0.05);cursor:pointer">'
                # Visible row
                f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'
                f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
                f'<span style="font-size:15px;font-weight:700;font-family:Syne,sans-serif;color:#e0e0e0">{sym_t}</span>'
                f'<span style="font-size:12px;font-weight:700;color:{mkt_col_t}">{market_t}</span>'
                f'{intraday_badge}'
                f'</div>'
                f'<div style="font-size:16px;font-weight:700;color:{pc_t};white-space:nowrap">{pct_s_t}</div>'
                f'</div>'
                # Expanded panel
                f'<div id="tcard-{t_card_idx}" style="display:none;margin-top:10px;padding:12px 14px;background:rgba(255,255,255,0.03);border-radius:8px">'
                # Header row — Date + Score (left)  |  Entry Reason (right, inferred from score)
                f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,0.06)">'
                f'<div>'
                f'<div style="font-size:13px;font-weight:600;color:#e0e0e0">{date_full_t}</div>'
                f'<div style="font-size:11px;color:#94a3b8;margin-top:3px">Score <b style="color:#ffcc00;font-size:13px;margin-left:2px">{score_disp_t}</b></div>'
                f'</div>'
                f'<div style="text-align:right;max-width:55%">'
                f'<div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px">Entry Reason</div>'
                f'<div style="font-size:12px;color:#00aaff;font-weight:600;margin-top:3px;line-height:1.3">{entry_reason_t}</div>'
                f'</div>'
                f'</div>'
                # Metrics grid
                f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;font-size:13px;margin-bottom:10px">'
                f'<div><span style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px">Total</span><br><b>{total_s_t}</b></div>'
                f'<div><span style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px">Qty</span><br><b>{qty_s_t}</b></div>'
                f'<div><span style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px">Held</span><br><b>{hold_s_t}</b></div>'
                f'<div><span style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px">P&amp;L</span><br><b style="color:{pc_t}">{sign_t}${pnl_t:.2f}</b></div>'
                f'</div>'
                # Exit reason at bottom
                f'<div style="border-top:1px solid rgba(255,255,255,0.06);padding-top:8px">'
                f'<span style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px">Exit Reason</span><br>'
                f'<b style="color:{sell_col_t};font-size:13px">{sell_reason_t}</b>'
                f'</div></div>'
                f'</div>'
            )
        trades_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<div class="section-title" style="text-transform:uppercase;letter-spacing:1px">RECENT TRADES <span style="font-size:12px;color:#94a3b8;font-weight:400;text-transform:none">DB-backed · survives restarts</span></div>'
            f'<div class="trades-table-wrap table-wrap"><table><thead><tr>'
            f'<th></th><th>Symbol</th><th>Mkt</th><th>Date</th><th>Time</th>'
            f'<th>Entry $</th><th>Qty</th><th>Total $</th><th>Held</th><th>P&L</th><th>%</th><th>Score</th>'
            f'</tr></thead>'
            f'<tbody>{trade_rows}</tbody></table></div>'
            f'<div class="trades-cards">{iphone_trade_cards}</div>'
            f'<div style="margin-top:10px;font-size:13px;color:#94a3b8">Total: {total_t} trades · '
            f'<span style="color:{_col(total_pnl_db)}">{_fmt(total_pnl_db)}</span> all-time · '
            f'{win_rate}% win rate</div>'
            f'<script>function toggleTrade(i){{var r=document.getElementById("trade-det-"+i);if(r)r.style.display=r.style.display==="none"?"table-row":"none";}}function toggleTradeCard(i){{var d=document.getElementById("tcard-"+i);if(!d)return;d.style.display=d.style.display==="block"?"none":"block";}}</script>'
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
      <span style="font-size:11px;font-weight:700;color:{"#00ff88" if sc>=MIN_SIGNAL_SCORE else "#ffcc00" if sc>=MIN_SIGNAL_SCORE-1 else "#94a3b8"}">{v_scr}</span>
    </span>
  </td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_ema}
      <span style="font-size:11px;color:{"#00ff88" if ema_gap and ema_gap>0 else "#ffcc00" if ema_gap and ema_gap>-0.5 else "#94a3b8"}">{v_ema}</span>
    </span>
  </td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_rsi}
      <span style="font-size:11px;color:{"#00ff88" if c.get("rsi") and 50<=c["rsi"]<=65 else "#ffcc00" if c.get("rsi") and c["rsi"]<=75 else "#ff4466" if c.get("rsi") and c["rsi"]>75 else "#94a3b8"}">{v_rsi}</span>
    </span>
  </td>
  <td style="text-align:center">
    <span style="display:inline-flex;align-items:center;gap:5px">
      {g_vol}
      <span style="font-size:11px;color:{"#00ff88" if c.get("vol_ratio",0)>=1.5 else "#ffcc00" if c.get("vol_ratio",0)>=1.2 else "#94a3b8"}">{v_vol}</span>
    </span>
  </td>
  <td style="text-align:center">{g_vap}</td>
  <td style="text-align:center">{g_sec}<span style="font-size:10px;color:#94a3b8;margin-left:3px">{v_sec}</span></td>
</tr>
<tr id="{rid}_detail" class="scan-row-desktop" style="display:none;background:rgba(255,255,255,0.02)">
  <td colspan="9" style="padding:10px 16px;font-size:12px;color:#94a3b8;border-bottom:1px solid rgba(255,255,255,0.04)">
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
  <td colspan="9" style="padding:8px 12px;font-size:11px;color:#94a3b8">
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
            f'<span style="color:#94a3b8;margin-left:auto">{len(scored)} scanned</span></div>'
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

            scr_c = "#00ff88" if sc>=MIN_SIGNAL_SCORE else "#ffcc00" if sc>=MIN_SIGNAL_SCORE-1 else "#94a3b8"
            ema_c2 = "#00ff88" if ema_gap and ema_gap>0 else "#ffcc00" if ema_gap and ema_gap>-0.5 else "#94a3b8"
            rsi_c2 = "#00ff88" if c.get("rsi") and 50<=c["rsi"]<=65 else "#ffcc00" if c.get("rsi") and c["rsi"]<=75 else "#ff4466" if c.get("rsi") and c["rsi"]>75 else "#94a3b8"
            vol_c2 = "#00ff88" if c.get("vol_ratio",0)>=1.5 else "#ffcc00" if c.get("vol_ratio",0)>=1.2 else "#94a3b8"
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
            f'<div style="font-size:12px;color:#94a3b8">Score ≥ {MIN_SIGNAL_SCORE} + EMA crossed</div>'
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
            f'gap:2px;padding:3px 8px 8px;font-size:9px;color:#94a3b8;font-weight:700;text-transform:uppercase">'
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
            f'<div class="section-title" style="color:#94a3b8;margin-bottom:0">🟢 READY TO TRADE</div>' 
            f'<div style="font-size:13px;color:#94a3b8">No signals qualify right now — watching {sum(len(x) for x in [us_scored,crypto_scored,ftse_scored,asx_scored,sc_scored])} stocks across all markets</div>'
            f'</div></div>'
        )

    # ── Build per-market accordion panels ──
    def build_market_accordion(mid, label, icon, color, open_now, scored, is_open):
        if not scored:
            preview_html = f'<div style="padding:16px;color:#94a3b8;font-size:14px">No scan data yet — waiting for first cycle</div>'
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
                f'<span style="color:#94a3b8;margin-left:auto">{total} scanned</span></div>'
            )
            preview_rows = mk_rows(scored[:10])
            full_rows    = mk_rows(scored)
            preview_html = summary + thead + preview_rows + tfoot
            show_all_btn = (
                f'<div id="mkt-btn-{mid}" style="margin-top:10px;text-align:center">'
                f'<button onclick="showFullMarket(\'{mid}\')" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:7px;color:#94a3b8;padding:8px 20px;font-size:13px;cursor:pointer;font-family:monospace">'
                f'▼ Show all {total} stocks</button></div>' if total > 10 else ''
            )
            full_html = summary + thead + full_rows + tfoot

        # Market status badge
        status_badge = (
            f'<span style="font-size:12px;font-weight:700;color:{color};background:rgba(255,255,255,0.06);'
            f'padding:3px 10px;border-radius:5px;margin-left:8px">OPEN</span>'
            if open_now else
            f'<span style="font-size:12px;color:#94a3b8;background:rgba(255,255,255,0.03);'
            f'padding:3px 10px;border-radius:5px;margin-left:8px">CLOSED</span>'
        )
        buys_badge = (
            f'<span style="font-size:12px;color:#00ff88;font-weight:700;margin-left:auto">🟢 {buys} BUY</span>'
            if buys > 0 else
            f'<span style="font-size:12px;color:#94a3b8;margin-left:auto">{len(scored)} scanned</span>'
        ) if scored else ''

        border_col = color if open_now else "rgba(255,255,255,0.07)"
        bg_col = f"rgba({'0,255,136' if color=='#00ff88' else '0,170,255' if color=='#00aaff' else '204,136,255' if color=='#cc88ff' else '255,170,0' if color=='#ffaa00' else '255,204,0'},0.03)" if open_now else "transparent"

        return (
            f'<div style="border:1px solid {border_col};border-radius:12px;margin-bottom:10px;background:{bg_col};overflow:hidden">'
            f'<div onclick="toggleMarket(\'{mid}\')" style="display:flex;align-items:center;gap:10px;padding:14px 18px;cursor:pointer;user-select:none">'
            f'<span style="font-size:18px">{icon}</span>'
            f'<span style="font-size:16px;font-weight:700;color:{color if open_now else "#94a3b8"}">{label}</span>'
            f'{status_badge}'
            f'{buys_badge}'
            f'<span id="mkt-arrow-{mid}" style="font-size:13px;color:#94a3b8;margin-left:12px">{"▼" if open_now else "▶"}</span>'
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
        f'<div style="font-size:13px;color:#94a3b8">Open markets expanded · top 10 shown/collapse</div>'
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
            if all_r else '<div style="color:#94a3b8;font-size:14px;padding:10px 0">All clear — no negative news today.</div>'
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
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">IBKR · {now_date}</div>
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
      <div style="font-size:11px;color:#94a3b8;letter-spacing:1px">Portfolio</div>
      <div style="font-size:15px;font-weight:700;color:#00aaff">{portfolio}</div>
    </div>
    <a href="/analytics" style="padding:8px 16px;border-radius:8px;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">📊 ANALYTICS</a>
    <a href="/intelligence" style="padding:8px 16px;border-radius:8px;background:rgba(170,136,255,0.1);border:1px solid rgba(170,136,255,0.3);color:#aa88ff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">🧠 INTELLIGENCE</a>
    <div style="font-size:12px;color:#94a3b8">Cycle #{st_data.get('cycle', 0)}</div>
    <div style="font-size:12px;color:#94a3b8" id="refresh-timer">↻ 60s</div>
  </div>
</div>

<div class="controls-bar">
  <a href="/analytics" class="tab" style="text-decoration:none">📊 Analytics</a>
  <a href="/intelligence" class="tab" style="text-decoration:none">🧠 Intelligence</a>
  <a href="/settings" class="tab" style="text-decoration:none">⚙️ Settings</a>
  <span style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px">Controls:</span>
  <button class="ctrl-btn" onclick="pinCmd('/kill','🛑 Kill all bots?')" style="border:1px solid #ff4466;background:rgba(255,68,102,0.1);color:#ff4466">🛑 KILL ALL BOTS</button>
  <button class="ctrl-btn" onclick="pinCmd('/close-all','💰 Close all positions?')" style="border:1px solid #ff8800;background:rgba(255,136,0,0.1);color:#ff8800">💰 CLOSE ALL POSITIONS</button>
  <button class="ctrl-btn" onclick="pinCmd('/resume','▶ Resume?')" style="border:1px solid #00ff88;background:rgba(0,255,136,0.1);color:#00ff88">▶ RESUME</button>
  <span id="dash-token" style="display:none">{DASH_TOKEN}</span>
  <span id="cmd-status" style="font-size:13px;color:#94a3b8;margin-left:8px"></span>
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
<div class="pstrip" style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:14px">
  <div class="card">
    <div style="display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <div>
        <div class="lbl">Total Balance · IBKR + Binance</div>
        <div class="big blue" style="font-size:32px">{portfolio}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:12px;color:#94a3b8">{now_date}</div>
        <div style="font-size:13px;color:#94a3b8;margin-top:3px">
          <span class="dot {'dot-green' if market_open else 'dot-red'}"></span>{"Open" if market_open else "Closed"}
        </div>
      </div>
    </div>
    <div style="margin-top:10px;display:flex;gap:24px;font-size:14px;flex-wrap:wrap">
      <span><span style="color:#94a3b8">Today </span><span style="font-weight:700;color:{_col(today_pnl)}">{_fmt(today_pnl)}</span></span>
      <span><span style="color:#94a3b8">Trades </span><span style="font-weight:700">{today_count}</span></span>
    </div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#00aaff">TODAY</div>
    <div style="font-size:22px;font-weight:700;color:{_col(d0["pnl"])};margin:6px 0">{_fmt(d0["pnl"])}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">{d0["t"]} trades · {d0["wr"]}% win</div>
    <div style="font-size:12px;color:#94a3b8">avg {_fmt(d0["avg"])} · <span style="color:{_col(d0["pnl"])}">{d0["pct"]:+.2f}%</span></div>
  </div>
  <div class="card">
    <div class="lbl">{d1["name"]}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(d1["pnl"])};margin:6px 0">{_fmt(d1["pnl"])}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">{d1["t"]} trades · {d1["wr"]}% win</div>
    <div style="font-size:12px;color:#94a3b8">avg {_fmt(d1["avg"])} · <span style="color:{_col(d1["pnl"])}">{d1["pct"]:+.2f}%</span></div>
  </div>
  <div class="card">
    <div class="lbl">{d2["name"]}</div>
    <div style="font-size:22px;font-weight:700;color:{_col(d2["pnl"])};margin:6px 0">{_fmt(d2["pnl"])}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">{d2["t"]} trades · {d2["wr"]}% win</div>
    <div style="font-size:12px;color:#94a3b8">avg {_fmt(d2["avg"])} · <span style="color:{_col(d2["pnl"])}">{d2["pct"]:+.2f}%</span></div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#00aaff">LAST 7 DAYS</div>
    <div style="font-size:22px;font-weight:700;color:{_col(week_pnl)};margin:6px 0">{_fmt(week_pnl)}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">{week_t} trades · {week_wr}% win</div>
    <div style="font-size:12px;color:#94a3b8">best <span style="color:#00ff88">{week_best}</span> · worst <span style="color:#ff4466">{week_worst}</span></div>
  </div>
  <div class="card">
    <div class="lbl">ALL TIME</div>
    <div style="font-size:22px;font-weight:700;color:{_col(total_pnl_db)};margin:6px 0">{_fmt(total_pnl_db)}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">{total_t} trades · {win_rate}% win</div>
    <div style="font-size:12px;color:#94a3b8">avg score {avg_sc_db:.1f}</div>
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
      <div><span style="color:#94a3b8">Status </span><span class="dot" style="background:{'#00ff88' if st_states.get('us',{}).get('running') else ('#ff4466' if st_states.get('us',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('us',{}))}</div>
      <div><span style="color:#94a3b8">SPY </span><b>{spy_str}</b></div>
      <div><span style="color:#94a3b8">Cycle </span>#{st_states.get('us',{}).get('cycle',0)}</div>
      <div><span style="color:#94a3b8">MA20 </span><span style="color:#777">{spy_ma}</span></div>
      <div><span style="color:#94a3b8">Positions </span><b>{st_states.get('us',{}).get('positions',0)}</b></div>
      <div><span style="color:#94a3b8">VIX </span><span style="color:{vix_color}">{vix_regime}</span></div>
    </div>
  </div>
  <div class="card card-green">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#00ff88">🪙 Crypto</div>
      <div style="font-size:18px;font-weight:700;color:{c_regime_color}">{'🐂' if c_regime=='BULL' else '🐻'} {c_regime}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#94a3b8">Status </span><span class="dot" style="background:{'#00ff88' if st_states.get('crypto',{}).get('running') else ('#ff4466' if st_states.get('crypto',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('crypto',{}))}</div>
      <div><span style="color:#94a3b8">BTC </span><b>{btc_str}</b></div>
      <div><span style="color:#94a3b8">MA14 </span><span style="color:#888">${(st_crypto_regime.get('btc_ma20') or 0):.0f}</span></div>
      <div><span style="color:#94a3b8">Cycle </span>#{st_states.get('crypto',{}).get('cycle',0)}</div>
      <div><span style="color:#94a3b8">Chg </span><span style="color:{btc_chg_col}">{btc_chg_str}</span></div>
      <div><span style="color:#94a3b8">Positions </span><b>{st_states.get('crypto',{}).get('positions',0)}</b></div>
      <div><span style="color:#94a3b8">Testnet </span><span style="color:#ffcc00">{'YES' if BINANCE_USE_TESTNET else 'LIVE'}</span></div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(255,170,0,0.25)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#ffaa00">🦘 ASX <span style="font-size:11px;color:#94a3b8;font-weight:400">00:00–06:00 UTC</span></div>
      <div style="font-size:16px;font-weight:700;color:{asx_col}">{'🐂' if asx_mode=='BULL' else '🐻'} {asx_mode}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#94a3b8">Market </span><span style="color:{'#ffaa00' if asx_open else '#94a3b8'};font-weight:700">{'OPEN' if asx_open else 'CLOSED'}</span></div>
      <div><span style="color:#94a3b8">CBA </span><b>{asx_cba}</b></div>
      <div><span style="color:#94a3b8">Positions </span><b>{st_states.get('asx',{}).get('positions',0)}</b></div>
      <div><span style="color:#94a3b8">Cycle </span>#{st_states.get('asx',{}).get('cycle',0)}</div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(204,136,255,0.25)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700;color:#cc88ff">🎩 FTSE <span style="font-size:11px;color:#94a3b8;font-weight:400">08:00–16:30 UTC</span></div>
      <div style="font-size:16px;font-weight:700;color:{ftse_col}">{'🐂' if ftse_mode=='BULL' else '🐻'} {ftse_mode}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#94a3b8">Market </span><span style="color:{'#cc88ff' if ftse_open else '#94a3b8'};font-weight:700">{'OPEN' if ftse_open else 'CLOSED'}</span></div>
      <div><span style="color:#94a3b8">HSBA </span><b>{ftse_hsba}</b></div>
      <div><span style="color:#94a3b8">Positions </span><b>{st_states.get('ftse',{}).get('positions',0)}</b></div>
      <div><span style="color:#94a3b8">Cycle </span>#{st_states.get('ftse',{}).get('cycle',0)}</div>
    </div>
  </div>
</div>

<!-- Small cap + Intraday -->
<div class="grid2" style="margin-bottom:14px">
  <div class="card" style="border-color:rgba(255,204,0,0.2)">
    <div style="font-size:16px;font-weight:700;color:#ffcc00;margin-bottom:10px">📊 Small Cap</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#94a3b8">Status </span><span class="dot" style="background:{'#00ff88' if st_states.get('smallcap',{}).get('running') else ('#ff4466' if st_states.get('smallcap',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('smallcap',{}))}</div>
      <div><span style="color:#94a3b8">Pool </span>{len(smallcap_pool.get('symbols',[]))}</div>
      <div><span style="color:#94a3b8">Positions </span><b>{st_states.get('smallcap',{}).get('positions',0)}</b></div>
      <div><span style="color:#94a3b8">Cycle </span>#{st_states.get('smallcap',{}).get('cycle',0)}</div>
    </div>
  </div>
  <div class="card" style="border-color:rgba(170,136,255,0.2)">
    <div style="font-size:16px;font-weight:700;color:#aa88ff;margin-bottom:10px">⚡ Intraday</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:14px">
      <div><span style="color:#94a3b8">Stocks </span><span class="dot" style="background:{'#00ff88' if st_states.get('intraday',{}).get('running') else ('#ff4466' if st_states.get('intraday',{}).get('shutoff') else '#ffcc00')}"></span>{_status(st_states.get('intraday',{}))}</div>
      <div><span style="color:#94a3b8">ID Cycle </span>#{st_states.get('intraday',{}).get('cycle',0)}</div>
      <div><span style="color:#94a3b8">ID Pos </span>{st_states.get('intraday',{}).get('positions',0)}</div>
      <div><span style="color:#94a3b8">Crypto </span><span class="dot {_dot(st_states.get('crypto_id',{}))}"></span>{_status(st_states.get('crypto_id',{}))}</div>
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
    <div style="font-size:13px;color:#94a3b8">{news_time}</div>
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

    # ── Leaderboard ───────────────────────────────────────────
    lb_rows = ""
    for i, row in enumerate(leaders):
        sym, trades, wins, losses, total_pnl, best, worst, avg_sc = row[:8]
        wr    = int(wins / trades * 100) if trades else 0
        pc    = "#00ff88" if total_pnl >= 0 else "#ff4466"
        medal = medals[i] if i < 3 else f"#{i+1}"
        wr_col = "#00ff88" if wr >= 55 else "#ffcc00" if wr >= 45 else "#ff4466"
        lb_rows += (
            f'<tr>'
            f'<td style="color:#94a3b8">{medal}</td>'
            f'<td style="font-weight:700;color:#00aaff">{sym}</td>'
            f'<td>{trades}</td><td style="color:#00ff88">{wins}</td><td style="color:#ff4466">{losses}</td>'
            f'<td style="font-weight:700;color:{wr_col}">{wr}%</td>'
            f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
            f'<td style="color:#00ff88">${best:.2f}</td><td style="color:#ff4466">${worst:.2f}</td>'
            f'<td style="color:#ffcc00">{avg_sc:.1f}</td></tr>'
        )
    if not lb_rows:
        lb_rows = '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:24px">No trades yet</td></tr>'

    # ── Symbol search ─────────────────────────────────────────
    search_html = ""
    if search_sym:
        res   = db_search_symbol(search_sym)
        stats = res["stats"]
        if stats:
            sym2, total_t, wins2, losses2, total_pnl2, best2, worst2, avg_sc2, nm_count, last_t, first_t, _ = stats
            wr2 = int(wins2 / total_t * 100) if total_t > 0 else 0
            pc2 = "#00ff88" if total_pnl2 >= 0 else "#ff4466"
            search_html = (
                f'<div style="background:#0d1117;border:1px solid rgba(0,170,255,0.2);border-radius:12px;padding:22px;margin-bottom:20px">'
                f'<div style="font-size:22px;font-weight:700;color:#00aaff;margin-bottom:14px;font-family:\'Syne\',sans-serif">{sym2}</div>'
                f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px">'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700;color:{pc2}">${total_pnl2:+.2f}</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Total P&L</div></div>'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700">{total_t}</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Trades</div></div>'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700;color:#00ff88">{wr2}%</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Win Rate</div></div>'
                f'<div style="background:#111820;border-radius:10px;padding:14px;text-align:center"><div style="font-size:24px;font-weight:700;color:#ff8800">{nm_count}</div><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:5px">Near Misses</div></div>'
                f'</div></div>'
            )
        else:
            search_html = f'<div style="color:#94a3b8;padding:20px;text-align:center;font-size:15px">No data for <b style="color:#00aaff">{search_sym}</b> yet</div>'

    # ── DB overview stats ─────────────────────────────────────
    total_t_db, total_pnl_db, wins_db, losses_db, avg_sc_db = _db_all_time_stats()
    pnl_col_db = "#00ff88" if total_pnl_db >= 0 else "#ff4466"
    try:
        conn = sqlite3.connect(DB_PATH)
        unique_syms  = conn.execute("SELECT COUNT(DISTINCT symbol) FROM trades").fetchone()[0] or 0
        total_misses = conn.execute("SELECT COUNT(*) FROM near_misses").fetchone()[0] or 0
        score_misses = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason='SCORE'").fetchone()[0] or 0
        cap_misses   = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason!='SCORE'").fetchone()[0] or 0
        nm_rows = conn.execute(
            "SELECT symbol, score, skip_reason, created_at, pct_move, NULL, triggered, "
            "simulated_pnl_pct, mfe_pct, mae_pct "
            "FROM near_misses ORDER BY created_at DESC LIMIT 40"
        ).fetchall()
        conn.close()
    except Exception:
        unique_syms = total_misses = score_misses = cap_misses = 0
        nm_rows = []

    # ── Phase 2 new data ──────────────────────────────────────
    missed_usd, missed_count, missed_winners = db_missed_profit_total(days=period_days)
    missed_disc  = db_missed_profit_summary(days=period_days)
    cap_skips    = db_capacity_skips(days=30)
    thresh_data  = db_threshold_sensitivity()
    edge_data    = db_edge_by_discipline_and_score()
    regime_data  = db_performance_by_regime()
    gate_data    = db_entry_gate_attribution()
    rot_data     = db_rotation_summary(days=30)
    exit_data    = db_exit_category_breakdown(days=30)
    skip_reasons = db_get_skip_reason_breakdown()
    ev_data      = db_ev_by_discipline(days=period_days)

    # ── HEADLINE STATS STRIP (6 cards now) ───────────────────
    win_rate_db = int(wins_db / total_t_db * 100) if total_t_db else 0
    missed_col  = "#ff8800" if missed_usd < 0 else "#00ff88"

    stats_strip = f"""
    <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:22px">
      <div class="card" style="text-align:center">
        <div style="font-size:22px;font-weight:700;color:{pnl_col_db}">${total_pnl_db:+.2f}</div>
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Realised P&L</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:22px;font-weight:700;color:#00aaff">{total_t_db}</div>
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Trades</div>
        <div style="font-size:11px;color:#888;margin-top:2px">{win_rate_db}% win</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:22px;font-weight:700;color:#ffcc00">{avg_sc_db:.1f}</div>
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Avg Score</div>
      </div>
      <div class="card" style="text-align:center;border-color:rgba(255,136,0,0.25)">
        <div style="font-size:22px;font-weight:700;color:#ff8800">{total_misses}</div>
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Near Misses</div>
        <div style="font-size:11px;color:#888;margin-top:2px">{cap_misses} capacity</div>
      </div>
      <div class="card" style="text-align:center;border-color:rgba(255,136,0,0.25)">
        <div style="font-size:22px;font-weight:700;color:{missed_col}">${missed_usd:+.2f}</div>
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Missed Profit</div>
        <div style="font-size:11px;color:#888;margin-top:2px">{missed_count} sims run</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:22px;font-weight:700;color:#00ff88">{unique_syms}</div>
        <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:6px">Symbols</div>
      </div>
    </div>"""

    # ── MISSED PROFIT by discipline ───────────────────────────
    disc_map = {
        "stock_swing":    ("📈 Stock Swing",    "#00aaff"),
        "crypto_swing":   ("🔄 Crypto Swing",   "#00ff88"),
        "stock_intraday": ("⚡ Stock ID",        "#ffcc00"),
        "crypto_intraday":("⚡ Crypto ID",       "#aa88ff"),
        "swing":          ("📈 Swing",           "#00aaff"),
    }
    if missed_disc:
        disc_rows = ""
        for disc, cnt, total_usd, avg_pct, winners in missed_disc:
            label, col = disc_map.get(disc, (disc, "#94a3b8"))
            usd_col = "#00ff88" if (total_usd or 0) >= 0 else "#ff4466"
            disc_rows += (
                f'<tr>'
                f'<td style="color:{col};font-weight:700">{label}</td>'
                f'<td>{cnt}</td>'
                f'<td style="color:{usd_col};font-weight:700">${total_usd:+.2f}</td>'
                f'<td style="color:#888">{avg_pct:+.2f}%</td>'
                f'<td style="color:#00ff88">{int(winners)}</td>'
                f'</tr>'
            )
        missed_profit_html = (
            f'<div class="card" style="margin-bottom:20px;border-color:rgba(255,136,0,0.2)">'
            f'<div class="section-title" style="color:#ff8800">💸 Missed Profit Analysis</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">Simulated P&L if near-miss trades had been taken, using real stop/trail/TP rules.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Discipline</th><th>Near Misses</th><th>Simulated P&L</th><th>Avg %</th><th>Would Win</th>'
            f'</tr></thead><tbody>{disc_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        missed_profit_html = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(255,136,0,0.2)">'
            '<div class="section-title" style="color:#ff8800">💸 Missed Profit Analysis</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates once near-miss simulations have run (daily at noon ET).</div></div>'
        )

    # ── CAPACITY SKIP BREAKDOWN ───────────────────────────────
    skip_reason_labels = {
        "SCORE":               ("📊 Score below threshold", "#94a3b8"),
        "SECTOR_CAP":          ("🏗 Sector full",           "#ffcc00"),
        "MAX_TOTAL_POSITIONS": ("📦 Global pos cap",        "#ffcc00"),
        "MAX_DAILY_SPEND":     ("💰 Daily spend limit",     "#ff8800"),
        "CHOPPY_MARKET":       ("〰 Choppy market",         "#888"),
        "MAX_TRADES_DAY":      ("🔄 Max trades/day",        "#ff8800"),
        "DAILY_TARGET_HIT":    ("🎯 Profit target hit",     "#00ff88"),
        "MAX_EXPOSURE":        ("⚠ Exposure limit",         "#ff4466"),
        "ORDER_FAILED":        ("❌ Order failed",           "#ff4466"),
    }
    if cap_skips:
        cap_rows = ""
        for reason, cnt, avg_sc in cap_skips:
            label, col = skip_reason_labels.get(reason, (reason, "#888"))
            cap_rows += (
                f'<tr>'
                f'<td style="color:{col};font-weight:700">{label}</td>'
                f'<td style="font-size:15px;font-weight:700">{cnt}</td>'
                f'<td style="color:#ffcc00">{avg_sc:.1f}</td>'
                f'</tr>'
            )
        capacity_html = (
            f'<div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.15)">'
            f'<div class="section-title" style="color:#ffcc00">🚧 Capacity Skips — Last 30 Days</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">Strong signals blocked by capacity or regime limits — not score. High counts here mean raise position limits or regime thresholds.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Reason</th><th>Count</th><th>Avg Score</th>'
            f'</tr></thead><tbody>{cap_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        capacity_html = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.15)">'
            '<div class="section-title" style="color:#ffcc00">🚧 Capacity Skips</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">No capacity skips yet — populates as signals get blocked by position/spend/regime limits.</div></div>'
        )

    # ── NEAR MISS TABLE (enhanced) ────────────────────────────
    if nm_rows:
        nr = ""
        for row in nm_rows:
            sym2, sc2, reason2, ts2, pct2, _, checked2, sim_pct, mfe, mae = row
            ts_s    = ts2[:10] if ts2 else "—"
            pct_s   = f"{pct2:+.1f}%" if pct2 is not None else "Tracking…"
            pct_c   = "#00ff88" if pct2 and pct2 > 0 else ("#ff4466" if pct2 and pct2 < 0 else "#94a3b8")
            sim_s   = f"{sim_pct:+.1f}%" if sim_pct is not None else "—"
            sim_c   = "#00ff88" if sim_pct and sim_pct > 0 else ("#ff4466" if sim_pct and sim_pct < 0 else "#94a3b8")
            mfe_s   = f"+{mfe:.1f}%" if mfe is not None else "—"
            reason_label, reason_col = skip_reason_labels.get(reason2 or "SCORE", (reason2 or "SCORE", "#888"))
            nr += (
                f'<tr>'
                f'<td style="font-weight:700;color:#ffcc00">{sym2}</td>'
                f'<td style="color:#ffcc00">{sc2}/10</td>'
                f'<td style="color:{reason_col};font-size:11px">{reason_label}</td>'
                f'<td style="color:#94a3b8">{ts_s}</td>'
                f'<td style="color:{pct_c};font-weight:700">{pct_s}</td>'
                f'<td style="color:{sim_c};font-weight:700">{sim_s}</td>'
                f'<td style="color:#888">{mfe_s}</td>'
                f'<td>{"✅" if checked2 else "⏳"}</td>'
                f'</tr>'
            )
        near_miss_html = (
            f'<div class="card" style="margin-bottom:20px;border-color:rgba(255,136,0,0.2)">'
            f'<div class="section-title" style="color:#ff8800">🎯 Near-Miss Intelligence ({len(nm_rows)} tracked)</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Score</th><th>Skip Reason</th><th>Date</th>'
            f'<th>Actual %</th><th>Sim P&L</th><th>MFE</th><th>Fired</th>'
            f'</tr></thead><tbody>{nr}</tbody></table></div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-top:12px">'
            f'Actual % = price move since miss. Sim P&L = what we\'d have made with real stops. MFE = best it got.</div></div>'
        )
    else:
        near_miss_html = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(255,136,0,0.2)">'
            '<div class="section-title" style="color:#ff8800">🎯 Near-Miss Intelligence</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">No near-misses tracked yet.</div></div>'
        )

    # ── THRESHOLD SENSITIVITY (now DB-backed) ────────────────
    if thresh_data:
        bars_html = ""
        for bucket, cnt, avg_pct, winners in thresh_data:
            if avg_pct is None: continue
            bc = "#00ff88" if avg_pct > 0 else "#ff4466"
            bh = min(80, max(4, abs(avg_pct) * 8))
            bars_html += (
                f'<div style="display:flex;flex-direction:column;align-items:center;gap:5px;flex:1">'
                f'<div style="font-size:11px;color:{bc};font-weight:700">{avg_pct:+.1f}%</div>'
                f'<div style="width:100%;height:{bh}px;background:{bc};border-radius:4px 4px 0 0;opacity:0.8"></div>'
                f'<div style="font-size:10px;color:#94a3b8;text-align:center">{bucket}<br>{cnt}n</div></div>'
            )
        threshold_html = (
            f'<div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.15)">'
            f'<div class="section-title" style="color:#ffcc00">📈 Threshold Sensitivity — Avg Outcome by Score</div>'
            f'<div style="display:flex;align-items:flex-end;gap:8px;height:120px;padding:0 8px;border-bottom:1px solid #222;margin-bottom:10px">{bars_html}</div>'
            f'<div style="font-size:13px;color:#94a3b8">Use this to calibrate MIN_SIGNAL_SCORE before going live. Data is DB-backed and survives restarts.</div></div>'
        )
    else:
        threshold_html = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.15)">'
            '<div class="section-title" style="color:#ffcc00">📈 Threshold Sensitivity</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates once near-miss price follow-up data is available.</div></div>'
        )

    # ── EDGE BY DISCIPLINE + SCORE ────────────────────────────
    if edge_data:
        edge_rows = ""
        for disc, sb, cnt, wins, losses, total_pnl in edge_data:
            if not cnt: continue
            wr   = int(wins / cnt * 100)
            pc   = "#00ff88" if total_pnl >= 0 else "#ff4466"
            wr_c = "#00ff88" if wr >= 55 else "#ffcc00" if wr >= 45 else "#ff4466"
            label, col = disc_map.get(disc, (disc, "#888"))
            edge_rows += (
                f'<tr>'
                f'<td style="color:{col}">{label}</td>'
                f'<td style="color:#ffcc00;font-weight:700">{sb}+</td>'
                f'<td>{cnt}</td>'
                f'<td style="color:{wr_c};font-weight:700">{wr}%</td>'
                f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
                f'</tr>'
            )
        edge_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">🔬 Edge by Discipline + Score</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">Win rate and P&L per score band per discipline — tells you which score thresholds to raise or lower per market.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Discipline</th><th>Score Band</th><th>Trades</th><th>Win Rate</th><th>Total P&L</th>'
            f'</tr></thead><tbody>{edge_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        edge_html = (
            '<div class="card" style="margin-bottom:20px">'
            '<div class="section-title">🔬 Edge by Discipline + Score</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates as closed trades accumulate.</div></div>'
        )

    # ── REGIME PERFORMANCE ────────────────────────────────────
    if regime_data:
        reg_rows = ""
        for regime, cnt, wins, losses, total_pnl, avg_pnl in regime_data:
            if not cnt: continue
            wr   = int(wins / cnt * 100)
            pc   = "#00ff88" if total_pnl >= 0 else "#ff4466"
            wr_c = "#00ff88" if wr >= 55 else "#ffcc00" if wr >= 45 else "#ff4466"
            reg_col = {"BULL": "#00ff88", "BEAR": "#ff4466", "CHOPPY": "#ffcc00"}.get(regime, "#888")
            reg_rows += (
                f'<tr>'
                f'<td style="font-weight:700;color:{reg_col}">{regime}</td>'
                f'<td>{cnt}</td>'
                f'<td style="color:{wr_c};font-weight:700">{wr}%</td>'
                f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
                f'<td style="color:#888">${avg_pnl:+.2f}</td>'
                f'</tr>'
            )
        regime_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">🌍 Performance by Market Regime</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">Are we only profitable in bull markets? This answers that.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Regime</th><th>Trades</th><th>Win Rate</th><th>Total P&L</th><th>Avg P&L</th>'
            f'</tr></thead><tbody>{reg_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        regime_html = (
            '<div class="card" style="margin-bottom:20px">'
            '<div class="section-title">🌍 Performance by Regime</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates as trades close with regime context captured.</div></div>'
        )

    # ── EXIT CATEGORY BREAKDOWN ───────────────────────────────
    exit_cat_labels = {
        "STOP":    ("🛑 Stop Loss",        "#ff4466"),
        "TP":      ("🎯 Take Profit",      "#00ff88"),
        "TRAIL":   ("📐 Trailing Stop",    "#00aaff"),
        "SIGNAL":  ("📊 Signal Exit",      "#888"),
        "MAXHOLD": ("⏱ Max Hold",          "#ffcc00"),
        "EOD":     ("🌙 End of Day",        "#888"),
        "ROTATE":  ("🔄 Rotated Out",      "#aa88ff"),
        "STALE":   ("💤 Stale Capital",    "#94a3b8"),
        "UNKNOWN": ("— Unknown",            "#333"),
    }
    if exit_data:
        exit_rows = ""
        for cat, cnt, wins, total_pnl, avg_pnl in exit_data:
            label, col = exit_cat_labels.get(cat, (cat, "#888"))
            wr   = int(wins / cnt * 100) if cnt else 0
            pc   = "#00ff88" if total_pnl >= 0 else "#ff4466"
            exit_rows += (
                f'<tr>'
                f'<td style="font-weight:700;color:{col}">{label}</td>'
                f'<td>{cnt}</td>'
                f'<td style="color:#888">{wr}%</td>'
                f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
                f'<td style="color:#888">${avg_pnl:+.2f}</td>'
                f'</tr>'
            )
        exit_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">🚪 Exit Category Breakdown — Last 30 Days</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">How positions are closing. Too many stops = tighten entry. Too many signals = trust the bot more.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Exit Type</th><th>Count</th><th>Win Rate</th><th>Total P&L</th><th>Avg P&L</th>'
            f'</tr></thead><tbody>{exit_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        exit_html = (
            '<div class="card" style="margin-bottom:20px">'
            '<div class="section-title">🚪 Exit Category Breakdown</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates as trades close.</div></div>'
        )

    # ── ROTATION AUDIT ────────────────────────────────────────
    if rot_data:
        rot_rows = ""
        for rtype, verdict, cnt, avg_sold_pct, avg_bought_pct in rot_data:
            v_col  = {"GOOD": "#00ff88", "BAD": "#ff4466", "NEUTRAL": "#ffcc00"}.get(verdict, "#888")
            r_label = "🔄 Score Rotate" if rtype == "SCORE_ROTATE" else "💤 Stale Exit"
            rot_rows += (
                f'<tr>'
                f'<td style="color:#888">{r_label}</td>'
                f'<td style="font-weight:700;color:{v_col}">{verdict}</td>'
                f'<td>{cnt}</td>'
                f'<td style="color:#ff4466">{avg_sold_pct:+.1f}%</td>'
                f'<td style="color:#00ff88">{avg_bought_pct:+.1f}%</td>'
                f'</tr>'
            )
        rotation_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">🔄 Rotation Audit — Last 30 Days</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">GOOD = new position outperformed old by >1% over 24h. BAD = we sold a winner too early.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Type</th><th>Verdict</th><th>Count</th><th>Sold % After</th><th>Bought % After</th>'
            f'</tr></thead><tbody>{rot_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        rotation_html = (
            '<div class="card" style="margin-bottom:20px">'
            '<div class="section-title">🔄 Rotation Audit</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates 24h after first rotation or stale exit fires.</div></div>'
        )

    # ── ENTRY GATE ATTRIBUTION ────────────────────────────────
    gate_rows = ""
    for gate_name, rows in gate_data.items():
        for val, cnt, wins, total_pnl in rows:
            if not cnt: continue
            wr   = int(wins / cnt * 100)
            pc   = "#00ff88" if total_pnl >= 0 else "#ff4466"
            wr_c = "#00ff88" if wr >= 55 else "#ffcc00" if wr >= 45 else "#ff4466"
            gate_label = {
                "breakout":    ("🚀 Breakout",    "1" if val else "0"),
                "macd_bullish":("📊 MACD Bull",   "Yes" if val else "No"),
                "adx":         ("📐 ADX",          str(val)),
            }.get(gate_name, (gate_name, str(val)))
            gate_rows += (
                f'<tr>'
                f'<td style="color:#888">{gate_label[0]}</td>'
                f'<td style="color:#ffcc00">{gate_label[1]}</td>'
                f'<td>{cnt}</td>'
                f'<td style="color:{wr_c};font-weight:700">{wr}%</td>'
                f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
                f'</tr>'
            )
    if gate_rows:
        gate_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">🔭 Entry Gate Attribution</div>'
            f'<div style="font-size:13px;color:#94a3b8;margin-bottom:14px">Win rate when each entry gate was active vs not. Shows which gates actually predict winners.</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Gate</th><th>Value</th><th>Trades</th><th>Win Rate</th><th>Total P&L</th>'
            f'</tr></thead><tbody>{gate_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        gate_html = (
            '<div class="card" style="margin-bottom:20px">'
            '<div class="section-title">🔭 Entry Gate Attribution</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:12px 0">Populates as trades close with structured entry context.</div></div>'
        )

    # ── SKIP REASON SUMMARY (all) ─────────────────────────────
    if skip_reasons:
        skip_rows = "".join(
            f'<tr><td style="color:#ffcc00">{r[0]}</td><td>{r[1]}</td><td style="color:#00aaff">{r[2]:.1f}</td></tr>'
            for r in skip_reasons
        )
        skip_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">📋 All Skip Reasons</div>'
            f'<div class="table-wrap"><table><thead><tr><th>Reason</th><th>Count</th><th>Avg Score</th></tr></thead>'
            f'<tbody>{skip_rows}</tbody></table></div>'
            f'</div>'
        )
    else:
        skip_html = ""

    # ── EV BY DISCIPLINE ──────────────────────────────────────
    # Expected Value = (win_rate * avg_win) - (loss_rate * avg_loss)
    # The single number that tells you if a discipline is worth running.
    DISC_META = {
        "stock_swing":     ("📈 Stock Swing",     "#00aaff"),
        "crypto_swing":    ("🔄 Crypto Swing",    "#00ff88"),
        "stock_intraday":  ("⚡ Stock Intraday",  "#ffcc00"),
        "crypto_intraday": ("⚡ Crypto Intraday", "#aa88ff"),
        "swing":           ("📈 Swing",            "#00aaff"),
        "asx_swing":       ("🇦🇺 ASX Swing",      "#ffaa00"),
        "ftse_swing":      ("🇬🇧 FTSE Swing",     "#cc88ff"),
    }

    def _ev_verdict(ev, trades):
        if trades < 10:
            return ("⏳ Need more data", "#94a3b8", "Insufficient sample (n<10)")
        if ev > 5:
            return ("✅ Positive Edge", "#00ff88", "This discipline is making money per trade")
        if ev > 0:
            return ("🟡 Marginal Edge", "#ffcc00", "Positive but thin — watch closely")
        if ev > -5:
            return ("⚠️ Marginal Loss", "#ff8800", "Losing slightly per trade — review threshold")
        return ("❌ Negative Edge", "#ff4466", "Losing consistently — consider pausing or raising score")

    if ev_data:
        ev_cards = ""
        for row in ev_data:
            disc, trades, wins, losses, win_rate, avg_win, avg_loss, ev, total_pnl, avg_score, avg_hold = row
            avg_score = avg_score or 0.0
            avg_hold  = avg_hold  or 0.0
            avg_score = avg_score or 0.0
            avg_hold  = avg_hold  or 0.0
            label, col = DISC_META.get(disc, (disc, "#888"))
            verdict, verdict_col, verdict_note = _ev_verdict(ev, trades)
            pnl_col = "#00ff88" if total_pnl >= 0 else "#ff4466"
            ev_col  = "#00ff88" if ev > 0 else "#ff4466"

            # Fetch per-discipline detail for exit category mini-breakdown
            detail = db_discipline_detail(disc, days=period_days)
            exit_mini = ""
            if detail.get("exit_cats"):
                _exit_cols = {"STOP":"#ff4466","TP":"#00ff88","SIGNAL":"#888",
                              "ROTATE":"#aa88ff","STALE":"#94a3b8","EOD":"#888","MAXHOLD":"#ffcc00"}
                cats = detail["exit_cats"][:3]
                exit_mini = " · ".join(
                    f'<span style="color:{_exit_cols.get(c[0], "#888")}">{c[0]} {c[1]}</span>'
                    for c in cats
                )

            # Score bucket mini bar
            score_mini = ""
            if detail.get("score_buckets"):
                for sb, cnt, sb_wins, sb_pnl in detail["score_buckets"]:
                    sb_wr = int(sb_wins / cnt * 100) if cnt else 0
                    sb_col = "#00ff88" if sb_wr >= 55 else "#ffcc00" if sb_wr >= 40 else "#ff4466"
                    score_mini += (
                        f'<span style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);'
                        f'border-radius:4px;padding:2px 7px;font-size:11px;margin-right:4px;color:{sb_col}">'
                        f'Score {sb}: {sb_wr}% ({cnt}n)</span>'
                    )

            ev_cards += f"""
            <div style="background:rgba(255,255,255,0.025);border:1px solid {col}33;
                        border-left:3px solid {col};border-radius:12px;padding:18px 20px;margin-bottom:12px">
              <!-- Header -->
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
                <div style="font-size:16px;font-weight:700;color:{col};font-family:'Syne',sans-serif">{label}</div>
                <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
                  <span style="font-size:11px;color:#94a3b8">{trades} trades · avg score {avg_score:.1f} · avg hold {avg_hold:.1f}h</span>
                  <span style="font-size:12px;font-weight:700;color:{verdict_col};background:rgba(255,255,255,0.04);
                               border:1px solid {verdict_col}44;border-radius:6px;padding:3px 10px">{verdict}</span>
                </div>
              </div>
              <!-- EV + key metrics -->
              <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:12px">
                <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px 6px;border:1px solid {ev_col}44">
                  <div style="font-size:20px;font-weight:700;color:{ev_col}">${ev:+.2f}</div>
                  <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:3px">EV / Trade</div>
                </div>
                <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px 6px">
                  <div style="font-size:20px;font-weight:700;color:{'#00ff88' if win_rate>=55 else '#ffcc00' if win_rate>=40 else '#ff4466'}">{win_rate:.0f}%</div>
                  <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Win Rate</div>
                </div>
                <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px 6px">
                  <div style="font-size:20px;font-weight:700;color:#00ff88">${avg_win:.2f}</div>
                  <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Avg Win</div>
                </div>
                <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px 6px">
                  <div style="font-size:20px;font-weight:700;color:#ff4466">${avg_loss:.2f}</div>
                  <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Avg Loss</div>
                </div>
                <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px 6px">
                  <div style="font-size:20px;font-weight:700;color:{'#00ff88' if avg_win>avg_loss else '#ff4466'}">{f"{avg_win/avg_loss:.1f}×" if avg_loss else "∞"}</div>
                  <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Reward/Risk</div>
                </div>
                <div style="text-align:center;background:rgba(255,255,255,0.03);border-radius:8px;padding:10px 6px">
                  <div style="font-size:20px;font-weight:700;color:{pnl_col}">${total_pnl:+.2f}</div>
                  <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:3px">Total P&L</div>
                </div>
              </div>
              <!-- Verdict note -->
              <div style="font-size:12px;color:{verdict_col};margin-bottom:10px;padding:6px 10px;
                          background:{verdict_col}11;border-radius:6px">{verdict_note}</div>
              <!-- Score buckets -->
              {f'<div style="margin-bottom:8px;flex-wrap:wrap;display:flex;gap:4px">{score_mini}</div>' if score_mini else ''}
              <!-- Exit mini breakdown -->
              {f'<div style="font-size:11px;color:#94a3b8">Exits: {exit_mini}</div>' if exit_mini else ''}
            </div>"""

        ev_section = f"""
        <div class="card" style="margin-bottom:20px;border-color:rgba(0,170,255,0.15)">
          <div class="section-title" style="color:#00aaff">⚡ Expected Value by Discipline</div>
          <div style="font-size:13px;color:#94a3b8;margin-bottom:16px">
            EV = (Win Rate × Avg Win) − (Loss Rate × Avg Loss). The single number that tells you
            if a discipline is worth running. Positive = edge exists. Negative = losing per trade
            regardless of win rate. Reward/Risk should be >1.0 — means winners are bigger than losers.
          </div>
          {ev_cards}
        </div>"""
    else:
        ev_section = (
            '<div class="card" style="margin-bottom:20px;border-color:rgba(0,170,255,0.15)">'
            '<div class="section-title" style="color:#00aaff">⚡ Expected Value by Discipline</div>'
            '<div style="color:#94a3b8;font-size:14px;padding:20px 0">Populates once each discipline has 2+ closed trades.</div>'
            '</div>'
        )

    # ── REPORTS ───────────────────────────────────────────────
    reports = db_get_reports(limit=30)
    report_rows = ""
    for r in reports:
        rid, rtype, rdate, subject = r
        icon = "📊" if rtype == "daily" else "📈" if rtype == "weekly" else "☀️"
        tc = "#00aaff" if rtype == "daily" else "#00ff88" if rtype == "weekly" else "#ffcc00"
        report_rows += (
            f'<tr onclick="loadReport({rid})" style="cursor:pointer">'
            f'<td style="color:{tc}">{icon} {rtype.title()}</td>'
            f'<td style="color:#888">{rdate}</td>'
            f'<td style="color:#e0e0e0">{subject or "—"}</td></tr>'
        )
    if not report_rows:
        report_rows = '<tr><td colspan="3" style="padding:24px;text-align:center;color:#94a3b8">No reports yet</td></tr>'

    report_viewer = ""
    if report_id:
        rep = db_get_report_by_id(int(report_id))
        if rep:
            _, rtype, rdate, subject, body_html_r, body_text, _ = rep
            report_viewer = (
                f'<div style="background:#0d1117;border:1px solid rgba(0,170,255,0.2);border-radius:12px;padding:22px;margin-bottom:20px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
                f'<div style="font-weight:700;color:#e0e0e0;font-size:16px">{subject}</div>'
                f'<div style="color:#94a3b8;font-size:13px">{rdate}</div></div>'
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
.period-btn{{padding:8px 16px;border-radius:7px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#94a3b8;font-size:12px;font-weight:700;cursor:pointer;margin-left:6px;font-family:'JetBrains Mono',monospace}}
.period-btn.active{{background:rgba(0,170,255,0.15);border-color:rgba(0,170,255,0.3);color:#00aaff}}
@media(max-width:820px){{
  .analytics-strip{{grid-template-columns:1fr 1fr 1fr !important}}
}}
</style>
</head>
<body>
<div class="header">
  <div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="logo">AlphaBot <span>Analytics</span></div>
      <span class="badge {'badge-live' if IS_LIVE else 'badge-paper'}">{'LIVE' if IS_LIVE else 'PAPER'}</span>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <a href="/analytics" style="padding:8px 16px;border-radius:8px;background:rgba(0,170,255,0.15);border:1px solid rgba(0,170,255,0.5);color:#00aaff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">📊 ANALYTICS</a>
    <a href="/intelligence" style="padding:8px 16px;border-radius:8px;background:rgba(170,136,255,0.1);border:1px solid rgba(170,136,255,0.3);color:#aa88ff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">🧠 INTELLIGENCE</a>
    <a href="/settings" style="padding:8px 16px;border-radius:8px;background:rgba(255,204,0,0.08);border:1px solid rgba(255,204,0,0.25);color:#ffcc00;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">⚙️ SETTINGS</a>
  </div>
</div>
<div class="controls-bar">
  <a href="/" class="tab" style="text-decoration:none">← Dashboard</a>
  <a href="/analytics" class="tab" style="text-decoration:none;color:#00aaff;border-bottom:2px solid #00aaff">📊 Analytics</a>
  <a href="/intelligence" class="tab" style="text-decoration:none">🧠 Intelligence</a>
  <a href="/settings" class="tab" style="text-decoration:none">⚙️ Settings</a>
  <span style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-left:8px">Controls:</span>
  <button class="ctrl-btn" onclick="pinAction('/kill','🛑 Kill all bots?')" style="border:1px solid #ff4466;background:rgba(255,68,102,0.1);color:#ff4466">🛑 KILL</button>
  <button class="ctrl-btn" onclick="pinAction('/close-all','💰 Close all positions?')" style="border:1px solid #ff8800;background:rgba(255,136,0,0.1);color:#ff8800">💰 CLOSE ALL</button>
  <button class="ctrl-btn" onclick="pinAction('/resume','▶ Resume?')" style="border:1px solid #00ff88;background:rgba(0,255,136,0.1);color:#00ff88">▶ RESUME</button>
  <span id="act-status" style="font-size:13px;color:#94a3b8;margin-left:8px"></span>
</div>
<script>
function pinAction(path,label){{
  var pin=prompt('PIN to confirm: '+label);
  if(pin===null)return;
  var status=document.getElementById('act-status');
  if(status)status.textContent='Sending...';
  fetch(path+'?pin='+encodeURIComponent(pin),{{method:'POST'}})
  .then(r=>r.json()).then(d=>{{
    if(status)status.textContent=d.status==='wrong_pin'?'❌ Wrong PIN':'✅ Done';
  }}).catch(()=>{{if(status)status.textContent='❌ Error';}});
}}
</script>
<div class="container">

  {stats_strip}

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

  {ev_section}
  {missed_profit_html}
  {capacity_html}
  {near_miss_html}
  {threshold_html}
  {edge_html}
  {regime_html}
  {exit_html}
  {rotation_html}
  {gate_html}
  {skip_html}

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

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str = None, msg_type: str = "ok"):
    return HTMLResponse(_build_settings_page(msg, msg_type))

@app.post("/settings")
async def settings_save(request: Request):
    body = await request.json()
    if body.get("pin") != KILL_PIN:
        return JSONResponse({"status": "wrong_pin"})
    new_settings = body.get("settings", {})
    if new_settings:
        # Read current values before overwriting so we can log the diff
        old_cfg = _load_tcfg()
        if _save_tcfg(new_settings):
            # Log each changed parameter
            for param, new_val in new_settings.items():
                old_val = old_cfg.get(param)
                if str(old_val) != str(new_val):
                    db_log_config_change(param, old_val, new_val, changed_by="manual")
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "error"})
    return JSONResponse({"status": "error"})


# ═══════════════════════════════════════════════════════════════
# INTELLIGENCE ROUTES + PAGE
# ═══════════════════════════════════════════════════════════════
@app.get("/intelligence", response_class=HTMLResponse)
async def intelligence_page(request: Request, triggered: str = None, error: str = None, since: str = None):
    try:
        return HTMLResponse(_build_intelligence_page(
            run_triggered=(triggered == "1"),
            run_error=error,
            since_run_id=since,
        ))
    except Exception as e:
        log.error(f"[INTELLIGENCE PAGE] {e}")
        return HTMLResponse(f"<pre style='color:#fff;background:#111;padding:40px'>Error: {e}</pre>", status_code=500)


@app.post("/intelligence/run")
async def intelligence_run(request: Request):
    try:
        body = await request.json()
        if body.get("pin") != KILL_PIN:
            return JSONResponse({"status": "wrong_pin"})
        import threading
        def _bg():
            try:
                from data.intelligence import run_intelligence_analysis
                run_id, cnt, _ = run_intelligence_analysis(triggered_by="manual")
                log.info(f"[INTELLIGENCE] Manual run complete — {cnt} recs, run_id={run_id}")
            except Exception as e:
                log.error(f"[INTELLIGENCE] Manual run failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})


@app.post("/intelligence/apply")
async def intelligence_apply(request: Request):
    try:
        body = await request.json()
        if body.get("pin") != KILL_PIN:
            return JSONResponse({"status": "wrong_pin"})
        rec_id    = int(body.get("rec_id", 0))
        parameter = body.get("parameter", "")
        value     = body.get("value")
        if parameter and value is not None:
            try:
                numeric = float(value)
                cfg_val = int(numeric) if numeric == int(numeric) else numeric
            except (ValueError, TypeError):
                cfg_val = value
            old_cfg = _load_tcfg()
            old_val = old_cfg.get(parameter)
            if not _save_tcfg({parameter: cfg_val}):
                return JSONResponse({"status": "error", "detail": "config write failed"})
            db_log_config_change(parameter, old_val, cfg_val, changed_by="intelligence")
        db_apply_recommendation(rec_id)
        log.info(f"[INTELLIGENCE] Applied rec {rec_id}: {parameter}={value}")
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})


@app.post("/intelligence/dismiss")
async def intelligence_dismiss(request: Request):
    try:
        body = await request.json()
        db_dismiss_recommendation(int(body.get("rec_id", 0)))
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})


@app.post("/intelligence/snooze")
async def intelligence_snooze(request: Request):
    try:
        body = await request.json()
        db_snooze_recommendation(int(body.get("rec_id", 0)), days=7)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})


@app.get("/intelligence/status")
async def intelligence_status(since: str = None):
    """Polled by progress bar. Pass since=run_id to detect genuinely new runs."""
    try:
        run = db_get_latest_intelligence_run()
        if not run:
            return JSONResponse({"run_id": None, "new_run": False})
        current_id = run.get("run_id")
        # If caller provided a previous run_id, new_run = True only when it changed
        new_run = (since is None) or (current_id != since)
        return JSONResponse({
            "run_id": current_id,
            "new_run": new_run,
            "rec_count": run.get("rec_count", 0),
            "rec_count_raw": run.get("rec_count_raw", 0),
            "created_at": run.get("created_at"),
        })
    except Exception as e:
        return JSONResponse({"run_id": None, "new_run": False})


def _build_intelligence_page(run_triggered=False, run_error=None, since_run_id=None):
    pending    = db_get_pending_recommendations()
    history    = db_get_recommendation_history(limit=20)
    latest_run = db_get_latest_intelligence_run()
    past_runs  = db_get_intelligence_runs(limit=8)

    CONF_COL = {"HIGH": "#00ff88", "MEDIUM": "#ffcc00", "LOW": "#ff8800"}
    CAT_COL  = {
        "THRESHOLD":       "#00aaff",
        "POSITION_LIMITS": "#aa88ff",
        "STOP_LOSS":       "#ff4466",
        "REGIME_GATE":     "#ffcc00",
        "WATCHLIST":       "#00ff88",
        "OBSERVATION":     "#94a3b8",
    }
    CAT_ICON = {
        "THRESHOLD": "🎯", "POSITION_LIMITS": "📦",
        "STOP_LOSS": "🛑", "REGIME_GATE": "🌍",
        "WATCHLIST": "📋", "OBSERVATION": "👁",
    }
    ACTION_COL = {
        "RAISE": "#00ff88", "LOWER": "#ff4466", "ADD": "#00ff88",
        "REMOVE": "#ff4466", "MONITOR": "#ffcc00", "NONE": "#94a3b8",
    }

    # ── Last run info ─────────────────────────────────────────
    if latest_run:
        ts   = (latest_run.get("created_at") or "")[:16].replace("T", " ")
        cnt  = latest_run.get("rec_count", 0)
        trig = latest_run.get("triggered_by", "?")
        last_run_html = (
            f'<div style="font-size:13px;color:#94a3b8">'
            f'Last run: <span style="color:#e0e0e0">{ts}</span>'
            f' · {cnt} recs · <span style="color:#888">{trig}</span></div>'
        )
    else:
        last_run_html = '<div style="font-size:13px;color:#94a3b8">No run yet — first run Saturday 7am Paris or trigger manually.</div>'

    pending_count = len(pending)
    badge = (
        f'<span style="background:#ff4466;color:#fff;border-radius:10px;'
        f'padding:2px 8px;font-size:11px;font-weight:700;margin-left:6px">'
        f'{pending_count}</span>'
    ) if pending_count else ""

    run_status_html = ""
    if run_triggered:
        run_status_html = """
        <div id="thinking-card" style="background:rgba(170,136,255,0.06);border:1px solid rgba(170,136,255,0.3);
             border-radius:14px;padding:28px 24px;margin-bottom:20px;text-align:center">
          <div style="font-size:22px;font-weight:700;color:#aa88ff;font-family:'Syne',sans-serif;margin-bottom:8px">
            🧠 Claude is analysing your trading data...
          </div>
          <div style="font-size:13px;color:#94a3b8;margin-bottom:20px">
            Reading EV by discipline, near-miss data, exit categories, rotation audit...
          </div>
          <div style="background:rgba(255,255,255,0.06);border-radius:20px;height:8px;margin:0 auto 16px;max-width:500px;overflow:hidden">
            <div id="intel-progress" style="height:100%;width:0%;background:linear-gradient(90deg,#7c3aed,#aa88ff);
                 border-radius:20px;transition:width 0.5s ease"></div>
          </div>
          <div id="intel-status-msg" style="font-size:12px;color:#94a3b8;font-family:'JetBrains Mono',monospace">
            Starting...
          </div>
        </div>
        <script>
        (function() {{
          var start = Date.now();
          var maxMs = 90000;
          var messages = [
            [0,  "Assembling trading data..."],
            [10, "Calculating EV by discipline..."],
            [20, "Reviewing near-miss patterns..."],
            [30, "Analysing exit categories..."],
            [40, "Checking rotation quality..."],
            [50, "Sending data to Claude..."],
            [60, "Claude is reading your performance..."],
            [70, "Generating recommendations..."],
            [80, "Validating and storing results..."],
            [88, "Almost done..."],
          ];

          function update() {{
            var elapsed = Date.now() - start;
            var pct = Math.min(92, Math.round(elapsed / maxMs * 100));
            var bar = document.getElementById('intel-progress');
            var msg = document.getElementById('intel-status-msg');
            if (bar) bar.style.width = pct + '%';
            var label = messages[0][1];
            for (var i = 0; i < messages.length; i++) {{
              if (pct >= messages[i][0]) label = messages[i][1];
            }}
            if (msg) msg.textContent = label;
          }}

          function checkDone() {{
            var sinceParam = '{since_run_id or ""}';
            fetch('/intelligence/status?since=' + encodeURIComponent(sinceParam))
              .then(function(r) {{ return r.json(); }})
              .then(function(d) {{
                if (d.new_run) {{
                  var bar = document.getElementById('intel-progress');
                  var msg = document.getElementById('intel-status-msg');
                  if (bar) {{ bar.style.width = '100%'; bar.style.background = 'linear-gradient(90deg,#00ff88,#00aaff)'; }}
                  if (msg) msg.textContent = '✅ Complete — loading recommendations...';
                  setTimeout(function() {{ location.href = '/intelligence'; }}, 800);
                }} else if (Date.now() - start > maxMs) {{
                  location.href = '/intelligence';
                }} else {{
                  update();
                  setTimeout(checkDone, 4000);
                }}
              }})
              .catch(function() {{
                update();
                setTimeout(checkDone, 4000);
              }});
          }}

          update();
          setTimeout(checkDone, 5000);
        }})();
        </script>"""
    elif run_error:
        run_status_html = (
            f'<div style="background:rgba(255,68,102,0.08);border:1px solid rgba(255,68,102,0.3);'
            f'border-radius:10px;padding:14px 18px;margin-bottom:20px;color:#ff4466;font-weight:700">'
            f'❌ Run failed: {run_error}</div>'
        )

    # ── Pending cards ─────────────────────────────────────────
    if pending:
        rec_cards = ""
        for r in pending:
            rec_id    = r.get("id")
            category  = r.get("category", "")
            action    = r.get("action", "")
            parameter = r.get("parameter", "")
            discipline= r.get("discipline", "all")
            cur_val   = r.get("current_value")
            rec_val   = r.get("recommended_value")
            evidence  = r.get("evidence", "")
            confidence= r.get("confidence", "LOW")
            sample_sz = r.get("sample_size")
            created   = (r.get("created_at") or "")[:10]
            is_obs    = category == "OBSERVATION"
            cat_c  = CAT_COL.get(category, "#888")
            cat_ic = CAT_ICON.get(category, "•")
            conf_c = CONF_COL.get(confidence, "#888")
            act_c  = ACTION_COL.get(action, "#888")

            if not is_obs and parameter and rec_val is not None:
                action_line = (
                    f'<div style="font-size:16px;font-weight:700;color:#e0e0e0;margin:12px 0 8px">'
                    f'<span style="color:{act_c}">{action}</span> '
                    f'<span style="color:{cat_c}">{parameter}</span>'
                    f'{f" ({discipline})" if discipline and discipline != "all" else ""}'
                    f': <span style="color:#94a3b8;font-size:14px;text-decoration:line-through">{cur_val}</span>'
                    f' → <span style="color:#00ff88;font-size:16px">{rec_val}</span></div>'
                )
            elif not is_obs and parameter:
                action_line = (
                    f'<div style="font-size:16px;font-weight:700;color:#e0e0e0;margin:12px 0 8px">'
                    f'<span style="color:{act_c}">{action}</span> '
                    f'<span style="color:{cat_c}">{parameter}</span></div>'
                )
            else:
                action_line = ""

            sample_html = f'<span style="font-size:11px;color:#94a3b8;margin-left:8px">n={sample_sz}</span>' if sample_sz else ""

            btn_apply = ""
            if not is_obs and parameter and rec_val is not None:
                btn_apply = (
                    f'<button onclick="applyRec({rec_id},\'{parameter}\',\'{rec_val}\')" '
                    f'style="padding:10px 20px;background:rgba(0,255,136,0.12);border:1px solid rgba(0,255,136,0.35);'
                    f'border-radius:8px;color:#00ff88;font-family:\'JetBrains Mono\',monospace;font-size:13px;'
                    f'font-weight:700;cursor:pointer;margin-right:8px">✅ Apply</button>'
                )
            btn_snooze = "" if is_obs else (
                f'<button onclick="snoozeRec({rec_id})" '
                f'style="padding:10px 16px;background:rgba(255,204,0,0.08);border:1px solid rgba(255,204,0,0.25);'
                f'border-radius:8px;color:#ffcc00;font-family:\'JetBrains Mono\',monospace;font-size:13px;'
                f'cursor:pointer;margin-right:8px">⏸ Snooze 7d</button>'
            )
            btn_dismiss = (
                f'<button onclick="dismissRec({rec_id})" '
                f'style="padding:10px 16px;background:rgba(255,68,102,0.08);border:1px solid rgba(255,68,102,0.25);'
                f'border-radius:8px;color:#ff4466;font-family:\'JetBrains Mono\',monospace;font-size:13px;cursor:pointer">'
                f'✕ Dismiss</button>'
            )

            rec_cards += f"""
            <div style="background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.07);
                        border-left:3px solid {cat_c};border-radius:12px;padding:20px 24px;margin-bottom:14px">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                  <span style="font-size:18px">{cat_ic}</span>
                  <span style="font-size:12px;font-weight:700;color:{cat_c};text-transform:uppercase;
                               letter-spacing:1px;background:rgba(255,255,255,0.05);
                               border:1px solid {cat_c}44;border-radius:4px;padding:2px 8px">{category}</span>
                  <span style="font-size:11px;font-weight:700;color:{conf_c};text-transform:uppercase;letter-spacing:1px">● {confidence}</span>
                  {sample_html}
                </div>
                <div style="font-size:11px;color:#94a3b8">{created}</div>
              </div>
              {action_line}
              <div style="font-size:13px;color:#ccc;line-height:1.6;margin-bottom:16px;
                          border-left:2px solid rgba(255,255,255,0.06);padding-left:12px;margin-top:8px">{evidence}</div>
              <div style="display:flex;flex-wrap:wrap;gap:8px">{btn_apply}{btn_snooze}{btn_dismiss}</div>
            </div>"""

        pending_section = f"""
        <div class="card" style="margin-bottom:20px;border-color:rgba(0,170,255,0.2)">
          <div class="section-title" style="color:#00aaff">📬 Pending Recommendations{badge}</div>
          <div style="font-size:13px;color:#94a3b8;margin-bottom:16px">
            Apply changes in one tap — takes effect within 60 seconds, no restart needed.
          </div>
          {rec_cards}
        </div>"""
    else:
        if latest_run:
            raw_cnt  = latest_run.get("rec_count_raw", 0) or 0
            pass_cnt = latest_run.get("rec_count", 0) or 0
            ts       = (latest_run.get("created_at") or "")[:16].replace("T", " ")
            total_t_db = latest_run  # just for reference

            if raw_cnt == 0:
                reason_msg = "Claude reviewed the data but found nothing actionable yet — sample size is too small for confident recommendations. Keep trading and check back next week."
                reason_icon = "⏳"
            elif pass_cnt == 0 and raw_cnt > 0:
                reason_msg = f"Claude generated {raw_cnt} recommendation(s) but they didn't pass validation (unexpected category or action values). This is a data quality issue — check the narrative below for Claude's actual findings."
                reason_icon = "⚠️"
            else:
                reason_msg = "All recommendations have been actioned — apply, snooze or dismiss moves them to history below."
                reason_icon = "✅"

            pending_section = f"""
            <div class="card" style="margin-bottom:20px;border-color:rgba(0,170,255,0.2)">
              <div class="section-title" style="color:#00aaff">📬 Pending Recommendations</div>
              <div style="background:rgba(255,255,255,0.03);border-radius:10px;padding:20px;text-align:center">
                <div style="font-size:28px;margin-bottom:10px">{reason_icon}</div>
                <div style="font-size:15px;color:#e0e0e0;font-weight:700;margin-bottom:8px">No pending recommendations</div>
                <div style="font-size:13px;color:#94a3b8;max-width:600px;margin:0 auto;line-height:1.7">{reason_msg}</div>
                <div style="font-size:11px;color:#475569;margin-top:12px">
                  Last run: {ts} · Claude generated {raw_cnt} rec(s) · {pass_cnt} stored
                </div>
              </div>
            </div>"""
        else:
            pending_section = """
            <div class="card" style="margin-bottom:20px;border-color:rgba(0,170,255,0.2)">
              <div class="section-title" style="color:#00aaff">📬 Pending Recommendations</div>
              <div style="color:#94a3b8;font-size:14px;padding:20px 0;text-align:center">
                No analysis run yet — runs Saturday 7am Paris or tap ⚡ Run Now above.
              </div>
            </div>"""

    # ── Latest narrative ──────────────────────────────────────
    narrative_html = ""
    if latest_run and latest_run.get("narrative"):
        narrative_html = f"""
        <div class="card" style="margin-bottom:20px;border-color:rgba(0,255,136,0.1)">
          <div class="section-title" style="color:#00ff88">📝 Latest Analysis Narrative</div>
          <div style="font-size:14px;color:#ccc;line-height:1.8;white-space:pre-wrap">{latest_run['narrative']}</div>
        </div>"""

    # ── History table ─────────────────────────────────────────
    history_section = ""
    if history:
        hist_rows = ""
        for r in history:
            cat    = r.get("category", "")
            param  = r.get("parameter", "")
            cur    = r.get("current_value")
            rec    = r.get("recommended_value")
            disc   = r.get("discipline", "all")
            status = r.get("status", "")
            ts     = (r.get("actioned_at") or r.get("created_at") or "")[:10]
            cat_c  = CAT_COL.get(cat, "#888")
            cat_ic = CAT_ICON.get(cat, "•")
            st_col = {"APPLIED": "#00ff88", "DISMISSED": "#ff4466", "SNOOZED": "#ffcc00"}.get(status, "#888")
            change = f"{param}: {cur} → {rec}" if param and rec is not None else (param or cat)
            hist_rows += (
                f'<tr><td style="color:{cat_c}">{cat_ic} {cat}</td>'
                f'<td style="color:#e0e0e0;font-size:12px">{change}</td>'
                f'<td style="color:#94a3b8;font-size:11px">{disc}</td>'
                f'<td style="font-weight:700;color:{st_col}">{status}</td>'
                f'<td style="color:#94a3b8">{ts}</td></tr>'
            )
        history_section = f"""
        <div class="card" style="margin-bottom:20px">
          <div class="section-title">📜 Decision History</div>
          <div class="table-wrap">
            <table><thead><tr><th>Category</th><th>Change</th><th>Discipline</th><th>Status</th><th>Date</th></tr></thead>
            <tbody>{hist_rows}</tbody></table>
          </div>
        </div>"""

    # ── Run archive ───────────────────────────────────────────
    runs_section = ""
    if past_runs:
        run_rows = ""
        for r in past_runs:
            ts      = (r.get("created_at") or "")[:16].replace("T", " ")
            trig    = r.get("triggered_by", "?")
            cnt     = r.get("rec_count", 0)
            narr    = r.get("narrative") or "—"
            preview = (narr[:120] + "…") if len(narr) > 120 else narr
            tc      = "#00aaff" if trig == "scheduled" else "#ffcc00"
            run_rows += (
                f'<tr><td style="color:#94a3b8;font-size:11px">{ts}</td>'
                f'<td style="color:{tc};font-size:11px">{trig}</td>'
                f'<td style="color:#ffcc00">{cnt}</td>'
                f'<td style="color:#94a3b8;font-size:12px">{preview}</td></tr>'
            )
        runs_section = f"""
        <div class="card" style="margin-bottom:20px">
          <div class="section-title">🗂 Run Archive</div>
          <div class="table-wrap">
            <table><thead><tr><th>Time</th><th>Trigger</th><th>Recs</th><th>Summary</th></tr></thead>
            <tbody>{run_rows}</tbody></table>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot Intelligence</title>
{BASE_CSS}
<style>
#pin-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:999;align-items:center;justify-content:center}}
#pin-overlay.visible{{display:flex}}
.pin-box{{background:#0d1117;border:1px solid rgba(0,255,136,0.3);border-radius:16px;padding:36px 40px;text-align:center;max-width:380px;width:90%}}
</style>
</head><body>
<div class="header">
  <div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="logo">AlphaBot <span>Intelligence</span></div>
      {badge}
    </div>
    <div style="font-size:13px;color:#94a3b8;margin-top:3px">{last_run_html}</div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <button onclick="triggerRun()"
      style="padding:9px 18px;background:rgba(170,136,255,0.12);border:1px solid rgba(170,136,255,0.35);
             border-radius:8px;color:#aa88ff;font-family:'JetBrains Mono',monospace;
             font-size:12px;font-weight:700;cursor:pointer;letter-spacing:0.5px">⚡ Run Now</button>
    <a href="/analytics" style="padding:8px 16px;border-radius:8px;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">📊 ANALYTICS</a>
    <a href="/settings" style="padding:8px 16px;border-radius:8px;background:rgba(255,204,0,0.08);border:1px solid rgba(255,204,0,0.25);color:#ffcc00;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">⚙️ SETTINGS</a>
  </div>
</div>
<div class="controls-bar">
  <a href="/" class="tab" style="text-decoration:none">← Dashboard</a>
  <a href="/analytics" class="tab" style="text-decoration:none">📊 Analytics</a>
  <a href="/intelligence" class="tab" style="text-decoration:none;color:#aa88ff;border-bottom:2px solid #aa88ff">🧠 Intelligence</a>
  <a href="/settings" class="tab" style="text-decoration:none">⚙️ Settings</a>
  <span style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-left:8px">Controls:</span>
  <button class="ctrl-btn" onclick="pinAction('/kill','🛑 Kill all bots?')" style="border:1px solid #ff4466;background:rgba(255,68,102,0.1);color:#ff4466">🛑 KILL</button>
  <button class="ctrl-btn" onclick="pinAction('/close-all','💰 Close all positions?')" style="border:1px solid #ff8800;background:rgba(255,136,0,0.1);color:#ff8800">💰 CLOSE ALL</button>
  <button class="ctrl-btn" onclick="pinAction('/resume','▶ Resume?')" style="border:1px solid #00ff88;background:rgba(0,255,136,0.1);color:#00ff88">▶ RESUME</button>
  <span id="act-status" style="font-size:13px;color:#94a3b8;margin-left:8px"></span>
</div>
<script>
function pinAction(path,label){{
  var pin=prompt('PIN to confirm: '+label);
  if(pin===null)return;
  var status=document.getElementById('act-status');
  if(status)status.textContent='Sending...';
  fetch(path+'?pin='+encodeURIComponent(pin),{{method:'POST'}})
  .then(r=>r.json()).then(d=>{{
    if(status)status.textContent=d.status==='wrong_pin'?'❌ Wrong PIN':'✅ Done';
  }}).catch(()=>{{if(status)status.textContent='❌ Error';}});
}}
</script>

<div class="container">
  {run_status_html}
  {pending_section}
  {narrative_html}
  {history_section}
  {runs_section}
</div>

<!-- PIN overlay -->
<div id="pin-overlay" onclick="if(event.target===this)closePin()">
  <div class="pin-box">
    <div style="font-size:20px;font-weight:700;color:#00ff88;margin-bottom:8px">🔒 Confirm Apply</div>
    <div id="pin-action-label" style="font-size:13px;color:#e0e0e0;margin-bottom:6px"></div>
    <div style="font-size:12px;color:#94a3b8;margin-bottom:20px">Takes effect within 60s — no restart needed.</div>
    <input id="pin-input" type="password" maxlength="10" placeholder="••••"
      style="background:#111;border:1px solid rgba(0,255,136,0.3);border-radius:8px;color:#00ff88;
             font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:700;padding:12px;
             width:100%;text-align:center;letter-spacing:4px;margin-bottom:16px"
      onkeydown="if(event.key==='Enter')submitApply()">
    <div style="display:flex;gap:10px">
      <button onclick="closePin()" style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#94a3b8;padding:12px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:13px">Cancel</button>
      <button onclick="submitApply()" style="flex:2;background:rgba(0,255,136,0.15);border:1px solid rgba(0,255,136,0.4);border-radius:8px;color:#00ff88;padding:12px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700">Apply Change</button>
    </div>
    <div id="pin-error" style="color:#ff4466;font-size:12px;margin-top:10px;display:none">Wrong PIN</div>
  </div>
</div>

<script>
var _pendingRec=null;
function applyRec(id,param,val){{
  _pendingRec={{id:id,param:param,val:val}};
  document.getElementById('pin-action-label').textContent='Apply: '+param+' → '+val;
  document.getElementById('pin-error').style.display='none';
  document.getElementById('pin-input').value='';
  document.getElementById('pin-overlay').classList.add('visible');
  document.getElementById('pin-input').focus();
}}
function closePin(){{document.getElementById('pin-overlay').classList.remove('visible');_pendingRec=null;}}
function submitApply(){{
  if(!_pendingRec)return;
  var pin=document.getElementById('pin-input').value;
  fetch('/intelligence/apply',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{pin:pin,rec_id:_pendingRec.id,parameter:_pendingRec.param,value:_pendingRec.val}})}})
  .then(r=>r.json()).then(d=>{{
    if(d.status==='ok'){{closePin();location.reload();}}
    else if(d.status==='wrong_pin'){{document.getElementById('pin-error').style.display='block';}}
    else{{alert('Error: '+JSON.stringify(d));}}
  }});
}}
function dismissRec(id){{
  if(!confirm('Dismiss permanently?'))return;
  fetch('/intelligence/dismiss',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{rec_id:id}})}}).then(()=>location.reload());
}}
function snoozeRec(id){{
  fetch('/intelligence/snooze',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{rec_id:id}})}}).then(()=>location.reload());
}}
function triggerRun(){{
  var pin=prompt('PIN to trigger intelligence run:');
  if(pin===null)return;
  // Capture current run_id BEFORE firing so poller detects genuinely NEW run
  fetch('/intelligence/status')
  .then(function(r){{return r.json();}})
  .then(function(current){{
    var prevRunId=current.run_id||'';
    fetch('/intelligence/run',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{pin:pin}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.status==='ok'){{
        location.href='/intelligence?triggered=1&since='+encodeURIComponent(prevRunId);
      }} else if(d.status==='wrong_pin') alert('Wrong PIN');
      else alert('Error: '+JSON.stringify(d));
    }});
  }}).catch(function(){{
    fetch('/intelligence/run',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{pin:pin}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.status==='ok') location.href='/intelligence?triggered=1';
      else if(d.status==='wrong_pin') alert('Wrong PIN');
    }});
  }});
}}
</script>
</body></html>"""


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
              {"<div style=\'font-size:11px;color:#94a3b8;margin-top:3px\'>" + note + "</div>" if note else ""}
            </div>
            <input name="{key}" type="{typ}" step="{step}" value="{val}"
              style="background:#0d1117;border:1px solid rgba(255,255,255,0.12);border-radius:8px;color:#00ff88;
                     font-family:\'JetBrains Mono\',monospace;font-size:15px;font-weight:700;padding:9px 14px;width:100%;text-align:right">
            <div style="font-size:11px;color:#94a3b8">{note if not note else ""}</div>
        </div>'''

    # ── Config change history ─────────────────────────────────
    history = db_get_config_history(limit=30)
    if history:
        hist_rows = ""
        for h in history:
            ts       = (h.get("created_at") or "")[:16].replace("T", " ")
            param    = h.get("parameter", "")
            old_v    = h.get("old_value", "—")
            new_v    = h.get("new_value", "")
            by       = h.get("changed_by", "manual")
            by_col   = "#aa88ff" if by == "intelligence" else "#ffcc00"
            by_label = "🧠 Intelligence" if by == "intelligence" else "👤 Manual"
            hist_rows += (
                f'<tr>'
                f'<td style="color:#94a3b8;font-size:11px">{ts}</td>'
                f'<td style="color:#00aaff;font-family:\'JetBrains Mono\',monospace">{param}</td>'
                f'<td style="color:#94a3b8;text-decoration:line-through">{old_v}</td>'
                f'<td style="color:#00ff88;font-weight:700">{new_v}</td>'
                f'<td style="color:{by_col};font-size:11px">{by_label}</td>'
                f'</tr>'
            )
        config_history_html = f'''
        <div class="container" style="max-width:860px;padding-top:0">
          <div class="settings-section">
            <div class="settings-section-title">📋 Config Change History</div>
            <div style="font-size:12px;color:#94a3b8;margin-bottom:14px">
              Every parameter change is logged here — manual saves and intelligence-applied recommendations.
              Claude sees this data during analysis to understand what changed and when.
            </div>
            <div class="table-wrap">
              <table><thead><tr>
                <th>Time</th><th>Parameter</th><th>Old Value</th><th>New Value</th><th>Changed By</th>
              </tr></thead><tbody>{hist_rows}</tbody></table>
            </div>
          </div>
        </div>'''
    else:
        config_history_html = f'''
        <div class="container" style="max-width:860px;padding-top:0">
          <div class="settings-section">
            <div class="settings-section-title">📋 Config Change History</div>
            <div style="color:#94a3b8;font-size:13px;padding:16px 0">
              No changes recorded yet — every save will appear here going forward.
            </div>
          </div>
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
  <div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="logo">AlphaBot <span>Settings</span></div>
      <span class="badge {"badge-live" if IS_LIVE else "badge-paper"}">{"● LIVE" if IS_LIVE else "◎ PAPER"}</span>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <a href="/analytics" style="padding:8px 16px;border-radius:8px;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">📊 ANALYTICS</a>
    <a href="/intelligence" style="padding:8px 16px;border-radius:8px;background:rgba(170,136,255,0.1);border:1px solid rgba(170,136,255,0.3);color:#aa88ff;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">🧠 INTELLIGENCE</a>
    <a href="/settings" style="padding:8px 16px;border-radius:8px;background:rgba(255,204,0,0.12);border:1px solid rgba(255,204,0,0.4);color:#ffcc00;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:1px;font-family:'JetBrains Mono',monospace">⚙️ SETTINGS</a>
  </div>
</div>
<div class="controls-bar">
  <a href="/" class="tab" style="text-decoration:none">← Dashboard</a>
  <a href="/analytics" class="tab" style="text-decoration:none">📊 Analytics</a>
  <a href="/intelligence" class="tab" style="text-decoration:none">🧠 Intelligence</a>
  <a href="/settings" class="tab" style="text-decoration:none;color:#ffcc00;border-bottom:2px solid #ffcc00">⚙️ Settings</a>
  <span style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-left:8px">Controls:</span>
  <button class="ctrl-btn" onclick="pinAction('/kill','🛑 Kill all bots?')" style="border:1px solid #ff4466;background:rgba(255,68,102,0.1);color:#ff4466">🛑 KILL</button>
  <button class="ctrl-btn" onclick="pinAction('/close-all','💰 Close all positions?')" style="border:1px solid #ff8800;background:rgba(255,136,0,0.1);color:#ff8800">💰 CLOSE ALL</button>
  <button class="ctrl-btn" onclick="pinAction('/resume','▶ Resume?')" style="border:1px solid #00ff88;background:rgba(0,255,136,0.1);color:#00ff88">▶ RESUME</button>
  <span id="act-status" style="font-size:13px;color:#94a3b8;margin-left:8px"></span>
</div>
<script>
function pinAction(path,label){{
  var pin=prompt('PIN to confirm: '+label);
  if(pin===null)return;
  var status=document.getElementById('act-status');
  if(status)status.textContent='Sending...';
  fetch(path+'?pin='+encodeURIComponent(pin),{{method:'POST'}})
  .then(r=>r.json()).then(d=>{{
    if(status)status.textContent=d.status==='wrong_pin'?'❌ Wrong PIN':'✅ Done';
  }}).catch(()=>{{if(status)status.textContent='❌ Error';}});
}}
</script>

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
    <div style="font-size:13px;color:#94a3b8;margin-bottom:20px">Required to save settings</div>
    <input id="pin-input" type="password" maxlength="10" placeholder="••••"
      style="background:#111;border:1px solid rgba(255,204,0,0.3);border-radius:8px;color:#ffcc00;
             font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:700;padding:12px;
             width:100%;text-align:center;letter-spacing:4px;margin-bottom:16px"
      onkeydown="if(event.key==='Enter')submitSettings()">
    <div style="display:flex;gap:10px">
      <button onclick="hidePin()"
        style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#94a3b8;
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
{config_history_html}
</body></html>'''
