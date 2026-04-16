"""
app/dashboard.py — AlphaBot Web Dashboard
HTTP server, all HTML templates, dashboard builder, analytics page, kill switch endpoints.
Access at http://YOUR_HETZNER_IP:8080
Broker: IBKR only. P&L sourced from SQLite DB (survives restarts).
"""

import json
import sqlite3
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    pass

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


# ── DB helpers — all P&L reads from SQLite (survives restarts) ───────────────
def _db_pnl_for_period(since_iso, until_iso=None):
    """Return (trade_count, total_pnl, win_count) for a date range from DB."""
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
    """Return today's total realised P&L from DB."""
    from datetime import date
    today = date.today().isoformat()
    t, pnl, w = _db_pnl_for_period(today)
    return pnl

def _db_all_time_stats():
    """Return (total_trades, total_pnl, wins, losses, avg_score) from DB."""
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
    """Return recent completed trades from DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT symbol, pnl, side, created_at, score FROM trades "
            "WHERE side='SELL' ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return rows
    except:
        return []


# ── Login page ────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot Login</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #090b0e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .box { background: #0d1117; border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;
         padding: 40px; width: 100%; max-width: 380px; }
  .logo { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; }
  .logo-icon { width: 40px; height: 40px; background: linear-gradient(135deg,#00ff88,#00aaff);
               border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; }
  .logo-text { font-size: 20px; font-weight: 700; }
  .logo-sub { font-size: 11px; color: #444; letter-spacing: 1.5px; text-transform: uppercase; }
  label { display: block; font-size: 11px; color: #555; letter-spacing: 1.5px;
          text-transform: uppercase; margin-bottom: 6px; margin-top: 16px; }
  input { width: 100%; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
          border-radius: 8px; padding: 12px 14px; color: #e0e0e0; font-size: 14px; outline: none; }
  input:focus { border-color: #00aaff; }
  button { width: 100%; margin-top: 24px; padding: 13px; background: linear-gradient(135deg,#00ff88,#00aaff);
           border: none; border-radius: 8px; color: #090b0e; font-size: 14px; font-weight: 700;
           cursor: pointer; letter-spacing: 1px; }
  p { font-size: 11px; color: #444; margin-top: 16px; text-align: center; }
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <div><div class="logo-text">AlphaBot</div><div class="logo-sub">Automated Day Trader</div></div>
  </div>
  <form method="POST" action="/login">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" autofocus>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password">
    <button type="submit">Sign In →</button>
  </form>
  <p>After signing in, bookmark the URL for instant mobile access</p>
</div>
</body>
</html>"""

# ── Main dashboard HTML template ──────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #090b0e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; font-size: 14px; }}
  .header {{ background: #0d1117; border-bottom: 1px solid #1e2a1e; padding: 16px 24px;
             display: flex; align-items: center; justify-content: space-between; }}
  .logo {{ display: flex; align-items: center; gap: 10px; }}
  .logo-icon {{ width: 32px; height: 32px; background: linear-gradient(135deg,#00ff88,#00aaff);
                border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; }}
  .logo-text {{ font-weight: 700; font-size: 16px; }}
  .logo-sub {{ font-size: 10px; color: #444; letter-spacing: 1.5px; text-transform: uppercase; }}
  .badge {{ padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 700; }}
  .badge-paper {{ background: rgba(255,204,0,0.1); color: #ffcc00; border: 1px solid rgba(255,204,0,0.3); }}
  .badge-live  {{ background: rgba(255,68,102,0.1); color: #ff4466; border: 1px solid rgba(255,68,102,0.3); }}
  .refresh {{ font-size: 11px; color: #444; }}
  .container {{ padding: 24px; max-width: 1100px; margin: 0 auto; }}
  .grid4 {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; margin-bottom: 20px; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }}
  .card {{ background: rgba(255,255,255,0.025); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 18px 20px; }}
  .card-green {{ border-color: rgba(0,255,136,0.15); }}
  .card-blue  {{ border-color: rgba(0,170,255,0.15); }}
  .lbl {{ font-size: 10px; letter-spacing: 2px; color: #555; text-transform: uppercase; margin-bottom: 4px; }}
  .big {{ font-size: 22px; font-weight: 700; font-family: monospace; }}
  .green {{ color: #00ff88; }} .blue {{ color: #00aaff; }}
  .red {{ color: #ff4466; }}   .gold {{ color: #ffcc00; }} .grey {{ color: #555; }}
  .section-title {{ font-size: 15px; font-weight: 700; margin-bottom: 14px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ font-size: 10px; color: #444; letter-spacing: 1.5px; text-transform: uppercase; padding: 10px 12px; text-align: left; font-weight: 600; }}
  td {{ padding: 9px 12px; border-top: 1px solid rgba(255,255,255,0.04); font-family: monospace; }}
  tr:hover td {{ background: rgba(255,255,255,0.02); }}
  .sig-buy  {{ background: rgba(0,255,136,0.1); color: #00ff88; border: 1px solid #00ff88; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }}
  .sig-sell {{ background: rgba(255,68,102,0.1); color: #ff4466; border: 1px solid #ff4466; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }}
  .sig-hold {{ background: rgba(255,255,255,0.05); color: #555; border: 1px solid #333; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }}
  .dot-green {{ background: #00ff88; box-shadow: 0 0 6px #00ff88; }}
  .dot-red   {{ background: #ff4466; box-shadow: 0 0 6px #ff4466; }}
  .dot-gold  {{ background: #ffcc00; box-shadow: 0 0 6px #ffcc00; }}
  .dot-amber {{ background: #ffaa00; box-shadow: 0 0 6px #ffaa00; }}
  .dot-purple {{ background: #cc88ff; box-shadow: 0 0 6px #cc88ff; }}
  .tab-bar {{ display: flex; border-bottom: 1px solid rgba(255,255,255,0.06); margin-bottom: 20px; }}
  .tab {{ padding: 10px 16px; cursor: pointer; font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
          text-transform: uppercase; color: #444; border-bottom: 2px solid transparent; text-decoration: none; }}
  .tab-stocks.active {{ color: #00aaff; border-bottom-color: #00aaff; }}
  .tab-crypto.active {{ color: #00ff88; border-bottom-color: #00ff88; }}
  .tab:hover {{ color: #e0e0e0; }}
  .empty {{ text-align: center; padding: 50px; color: #333; font-size: 15px; }}
  .scan-panel {{ display: none; }} .scan-panel.active {{ display: block; }}
  .bot-status-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 13px; }}
  @media(max-width:768px) {{
    .container {{ padding: 10px; }}
    .header {{ padding: 10px 14px; flex-direction: column; align-items: flex-start; gap: 6px; }}
    .header-right {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; width: 100%; }}
    .refresh {{ display: none; }}
    .grid4 {{ grid-template-columns: 1fr 1fr; gap: 10px; }}
    .grid2 {{ grid-template-columns: 1fr; gap: 10px; }}
    .card {{ padding: 12px 14px; }}
    .big {{ font-size: 18px; }}
    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    table {{ min-width: 480px; font-size: 11px; }}
    th, td {{ padding: 6px 8px; white-space: nowrap; }}
  }}
  @media(max-width:480px) {{
    .grid4 {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
    .big {{ font-size: 16px; }}
    table {{ min-width: 420px; font-size: 10px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <div>
      <div style="display:flex;align-items:center;gap:8px">
        <span class="logo-text">AlphaBot</span>
        <span class="badge {mode_badge}">{mode_label}</span>
      </div>
      <div class="logo-sub">Automated Day Trader · IBKR</div>
    </div>
  </div>
  <div class="header-right" style="display:flex;align-items:center;gap:16px">
    <div style="display:flex;gap:14px">
      <div style="text-align:right">
        <div style="font-size:10px;color:#00aaff">US P&L</div>
        <div style="font-family:monospace;font-size:13px;font-weight:700;color:{stocks_pnl_color}">{stocks_pnl}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:10px;color:#00ff88">Crypto P&L</div>
        <div style="font-family:monospace;font-size:13px;font-weight:700;color:{crypto_pnl_color}">{crypto_pnl}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:10px;color:#444">Portfolio</div>
        <div style="font-family:monospace;font-size:13px;font-weight:700;color:#00aaff">{portfolio}</div>
      </div>
    </div>
    <a href="/analytics" style="padding:6px 14px;border-radius:6px;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff;text-decoration:none;font-size:11px;font-weight:700;letter-spacing:1px">🧠 ANALYTICS</a>
    <div class="refresh" id="refresh-timer">↻ {now}</div>
  </div>
</div>

<!-- Kill switch controls -->
<div style="background:#0d1117;border-bottom:1px solid rgba(255,255,255,0.06);padding:8px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
  <span style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px">Controls:</span>
  <button onclick="pinCmd('/kill','🛑 Kill all bots?')" style="padding:6px 16px;border-radius:6px;border:1px solid #ff4466;background:rgba(255,68,102,0.1);color:#ff4466;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:1px">🛑 KILL ALL BOTS</button>
  <button onclick="pinCmd('/close-all','💰 Close all positions?')" style="padding:6px 16px;border-radius:6px;border:1px solid #ff8800;background:rgba(255,136,0,0.1);color:#ff8800;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:1px">💰 CLOSE ALL POSITIONS</button>
  <button onclick="pinCmd('/resume','▶ Resume all bots?')" style="padding:6px 16px;border-radius:6px;border:1px solid #00ff88;background:rgba(0,255,136,0.1);color:#00ff88;font-size:11px;font-weight:700;cursor:pointer;letter-spacing:1px">▶ RESUME</button>
  <span id="dash-token" style="display:none">{dash_token}</span>
  <span id="cmd-status" style="font-size:11px;color:#555;margin-left:8px"></span>
</div>
<script>
function pinCmd(path, label) {{
  var pin = prompt('Enter PIN to confirm: ' + label);
  if (pin === null) return;
  var token = document.getElementById('dash-token').textContent;
  var status = document.getElementById('cmd-status');
  status.textContent = 'Verifying...';
  fetch(path + '?token=' + token + '&pin=' + encodeURIComponent(pin), {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.status === 'wrong_pin') {{ status.textContent = '❌ Wrong PIN'; return; }}
      status.textContent = '✅ ' + d.status + ' — refreshing...';
      setTimeout(() => location.reload(), 2000);
    }})
    .catch(e => {{ status.textContent = '❌ Error: ' + e; }});
}}
</script>

<div class="container">

  {kill_banner}
  {circuit_banner}

  <!-- Portfolio strip -->
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:10px;margin-bottom:12px">
    <div class="card" style="padding:12px 16px">
      <div style="display:flex;align-items:baseline;justify-content:space-between">
        <div><div class="lbl">Total Balance · IBKR + Binance</div><div class="big blue" style="font-size:28px">{portfolio}</div></div>
        <div style="text-align:right"><div style="font-size:11px;color:#555">{now_date}</div><div style="font-size:11px;color:#444">{market_status} <span class="dot {market_dot}" style="display:inline-block;vertical-align:middle"></span></div></div>
      </div>
      <div style="margin-top:8px;display:flex;gap:24px;font-size:12px;align-items:center">
        <span><span style="color:#555">Today </span><span style="font-weight:700;color:{combined_pnl_color}">{combined_pnl_pct_fmt}%</span></span>
        <span><span style="color:#555">Trades </span><span style="font-weight:700">{trades_today_count}</span></span>
        <span><span style="color:#555">P&L </span><span style="font-weight:700;color:{combined_pnl_color}">{combined_pnl}</span></span>
      </div>
    </div>
    <div class="card" style="padding:12px 16px">
      <div class="lbl" style="color:#00aaff">{this_month_name}</div>
      <div style="font-size:20px;font-weight:700;color:{tm_color};margin:4px 0">{tm_pnl_fmt}</div>
      <div style="font-size:11px;color:#555;margin-bottom:3px">{tm_t} trades · {tm_wr}</div>
      <div>{tm_vs_lm}</div>
    </div>
    <div class="card" style="padding:12px 16px">
      <div class="lbl">{lm_name}</div>
      <div style="font-size:20px;font-weight:700;color:{lm_color};margin:4px 0">{lm_pnl_fmt}</div>
      <div style="font-size:11px;color:#555">{lm_t} trades · {lm_wr}</div>
    </div>
    <div class="card" style="padding:12px 16px">
      <div class="lbl" style="color:#00aaff">{this_week_label}</div>
      <div style="font-size:20px;font-weight:700;color:{tw_color};margin:4px 0">{tw_pnl_fmt}</div>
      <div style="font-size:11px;color:#555;margin-bottom:3px">{tw_t} trades · {tw_wr}</div>
      <div>{tw_vs_lw}</div>
    </div>
    <div class="card" style="padding:12px 16px">
      <div class="lbl">Last Week</div>
      <div style="font-size:20px;font-weight:700;color:{lw_color};margin:4px 0">{lw_pnl_fmt}</div>
      <div style="font-size:11px;color:#555">{lw_t} trades · {lw_wr}</div>
    </div>
  </div>

  <!-- Risk card row -->
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:12px">
    <div class="card" style="padding:12px 16px">
      <div class="lbl">Last 7 Days</div>
      <div style="font-size:22px;font-weight:700;color:{week_pnl_color};margin:4px 0">{week_pnl}</div>
      <div style="font-size:11px;color:#555">{week_trades} trades · {week_win_rate}% win rate</div>
      <div style="font-size:11px;color:#555;margin-top:2px">Best: <span style="color:#00ff88">{week_best}</span> · Worst: <span style="color:#ff4466">{week_worst}</span></div>
    </div>
    <div style="display:none"></div>
    <div class="card" style="padding:12px 16px">
      <div class="lbl">Risk Status</div>
      <div style="margin-top:6px;font-size:12px;display:flex;flex-direction:column;gap:4px">
        <div><span style="color:#555">VIX </span><span style="color:{vix_color};font-weight:700">{vix_level}</span></div>
        <div><span style="color:#555">Signal min </span><span style="color:#ffcc00;font-weight:700">{signal_threshold}/10</span></div>
        <div><span style="color:#555">Global pos </span><span style="font-weight:700">{global_pos}/{max_global}</span></div>
        <div><span style="color:#555">Loss streak </span><span style="color:{streak_color};font-weight:700">{loss_streak}/{streak_limit}</span></div>
      </div>
    </div>
  </div>

  <!-- Market regime + bot cards -->
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:10px">
    <div class="card card-blue" style="padding:12px 14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:14px;font-weight:700;color:#00aaff">📈 US Stocks</div>
        <div style="font-size:18px;font-weight:700;color:{regime_color}">{regime_icon} {regime}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
        <div><span style="color:#555">Status </span><span class="dot {stocks_dot}"></span>{stocks_status}</div>
        <div><span style="color:#555">SPY </span><b>{spy_str}</b></div>
        <div><span style="color:#555">Cycle </span>#{stocks_cycle}</div>
        <div><span style="color:#555">MA20 </span><span style="color:#777">{spy_ma_str}</span></div>
        <div><span style="color:#555">Positions </span><b>{stocks_positions}</b></div>
        <div><span style="color:#555">VIX </span><span style="color:{vix_regime_color}">{vix_str}</span></div>
        <div><span style="color:#555">Trades </span>{stocks_trades}</div>
        <div><span style="color:#555">Exp </span>${exposure_str}</div>
      </div>
    </div>
    <div class="card card-green" style="padding:12px 14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:14px;font-weight:700;color:#00ff88">🪙 Crypto</div>
        <div style="font-size:18px;font-weight:700;color:{c_regime_color}">{c_regime_icon} {c_regime}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
        <div><span style="color:#555">Status </span><span class="dot {crypto_dot}"></span>{crypto_status}</div>
        <div><span style="color:#555">BTC </span><b>{btc_str}</b></div>
        <div><span style="color:#555">Cycle </span>#{crypto_cycle}</div>
        <div><span style="color:#555">MA20 </span><span style="color:#777">{btc_ma_str}</span></div>
        <div><span style="color:#555">Positions </span><b>{crypto_positions}</b></div>
        <div><span style="color:#555">Chg </span><span style="color:{btc_chg_color}">{btc_chg_str}</span></div>
        <div><span style="color:#555">Trades </span>{crypto_trades}</div>
        <div><span style="color:#555">Exp </span>${crypto_exposure_str}</div>
      </div>
    </div>
    <div class="card" style="padding:12px 14px;border-color:rgba(255,170,0,0.3)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:14px;font-weight:700;color:#ffaa00">🦘 ASX <span style="font-size:10px;color:#555;font-weight:400">Sydney 00:00–06:00 UTC</span></div>
        <div style="font-size:18px;font-weight:700;color:{asx_regime_color}">{asx_regime_icon} {asx_regime_mode}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
        <div><span style="color:#555">Status </span><span class="dot {asx_dot}"></span>{asx_status}</div>
        <div><span style="color:#555">CBA </span><b>{asx_cba_str}</b></div>
        <div><span style="color:#555">Positions </span><b>{asx_pos_count}</b></div>
        <div><span style="color:#555">MA20 </span><span style="color:#777">{asx_ma_str}</span></div>
        <div><span style="color:#555">Market </span><span style="color:{asx_hours_color}">{asx_hours}</span></div>
        <div><span style="color:#555">Regime </span><span style="color:{asx_regime_color}">{asx_regime_mode}</span></div>
      </div>
    </div>
    <div class="card" style="padding:12px 14px;border-color:rgba(204,136,255,0.3)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:14px;font-weight:700;color:#cc88ff">🎩 FTSE <span style="font-size:10px;color:#555;font-weight:400">London 08:00–16:30 UTC</span></div>
        <div style="font-size:18px;font-weight:700;color:{ftse_regime_color}">{ftse_regime_icon} {ftse_regime_mode}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
        <div><span style="color:#555">Status </span><span class="dot {ftse_dot}"></span>{ftse_status}</div>
        <div><span style="color:#555">HSBA </span><b>{ftse_hsba_str}</b></div>
        <div><span style="color:#555">Positions </span><b>{ftse_pos_count}</b></div>
        <div><span style="color:#555">MA20 </span><span style="color:#777">{ftse_ma_str}</span></div>
        <div><span style="color:#555">Market </span><span style="color:{ftse_hours_color}">{ftse_hours}</span></div>
        <div><span style="color:#555">Regime </span><span style="color:{ftse_regime_color}">{ftse_regime_mode}</span></div>
      </div>
    </div>
  </div>

  <!-- Small Cap + Intraday -->
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:12px">
    <div class="card" style="padding:12px 14px;border-color:rgba(255,204,0,0.2)">
      <div style="font-size:14px;font-weight:700;color:#ffcc00;margin-bottom:8px">📊 Small Cap</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
        <div><span style="color:#555">Status </span><span class="dot {sc_dot}"></span>{sc_status}</div>
        <div><span style="color:#555">Cycle </span>#{sc_cycle}</div>
        <div><span style="color:#555">Positions </span><b>{sc_positions}</b></div>
        <div><span style="color:#555">Trades </span>{sc_trades}</div>
        <div><span style="color:#555">Pool </span>{sc_pool_size}</div>
        <div><span style="color:#555">Last Run </span><span style="color:#555;font-size:11px">{sc_last}</span></div>
      </div>
    </div>
    <div class="card" style="padding:12px 14px;border-color:rgba(170,136,255,0.2)">
      <div style="font-size:14px;font-weight:700;color:#aa88ff;margin-bottom:8px">⚡ Intraday</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px">
        <div><span style="color:#555">Stocks </span><span class="dot {id_dot}"></span>{id_status}</div>
        <div><span style="color:#555">ID Cycle </span>#{id_cycle}</div>
        <div><span style="color:#555">ID Pos </span>{id_positions}</div>
        <div><span style="color:#555">ID Trades </span>{id_trades}</div>
        <div><span style="color:#555">Crypto </span><span class="dot {cid_dot}"></span>{cid_status}</div>
        <div><span style="color:#555">CID Cycle </span>#{cid_cycle}</div>
      </div>
    </div>
  </div>

  {positions_html}
  {trades_html}
  {screener_html}

  <!-- Performance Analytics -->
  <div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.2)">
    <div class="section-title" style="color:#ffcc00">📊 Performance Analytics</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl">Win Rate</div><div style="font-size:22px;font-weight:700;color:{trades_wr_color}">{win_rate}%</div>
        <div style="font-size:10px;color:#555">{wins}W / {losses}L</div>
      </div>
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl">Max Drawdown</div><div style="font-size:22px;font-weight:700;color:{dd_color}">{max_dd}%</div>
        <div style="font-size:10px;color:#555">Peak: ${peak_pv}</div>
      </div>
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl">Profit Factor</div><div style="font-size:22px;font-weight:700;color:{pf_color}">{profit_factor}</div>
        <div style="font-size:10px;color:#555">&gt;1.5 = good</div>
      </div>
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl">Sharpe Ratio</div><div style="font-size:22px;font-weight:700;color:{sharpe_color}">{sharpe}</div>
        <div style="font-size:10px;color:#555">&gt;1.0 = good</div>
      </div>
    </div>
    <div style="margin-top:12px;display:flex;gap:20px;font-size:12px;flex-wrap:wrap">
      <span>Loss streak: <b style="color:{streak_color}">{loss_streak}</b>/{streak_limit}</span>
      <span>Pause: <b style="color:#888">{pause_status}</b></span>
      <span>VIX: <b style="color:{vix_color}">{vix_level}</b></span>
      <span>Size: <b style="color:#ffcc00">{size_mult}x</b></span>
      <span>Global pos: <b>{global_pos}</b>/{max_global}</span>
      <span>Signal min: <b style="color:#ffcc00">{signal_threshold}/10</b></span>
    </div>
  </div>

  <!-- Morning News -->
  <div class="card" style="margin-bottom:20px;border-color:rgba(170,136,255,0.2)">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="section-title" style="color:#aa88ff;margin-bottom:0">📰 Morning News Scan</div>
      <div style="font-size:11px;color:#555">{news_scan_time}</div>
    </div>
    {news_html}
  </div>

  <!-- Last Scan tabs -->
  <div style="margin-bottom:20px">
    <div class="tab-bar" style="margin-bottom:0;border-bottom:none">
      <div class="tab tab-stocks active" onclick="showScan('stocks',this)" style="border-bottom:2px solid #00aaff;color:#00aaff">📈 US Stocks</div>
      <div class="tab tab-crypto" onclick="showScan('crypto',this)">🪙 Crypto</div>
      <div class="tab" onclick="showScan('smallcap',this)" style="color:#ffcc00">📊 Small Cap</div>
      <div class="tab" onclick="showScan('asx',this)" style="color:#ffaa00">🦘 ASX</div>
      <div class="tab" onclick="showScan('ftse',this)" style="color:#cc88ff">🎩 FTSE</div>
    </div>
    <div class="card" style="border-radius:0 12px 12px 12px;margin-top:0">
      <div id="scan-stocks" class="scan-panel active">{stocks_scan_html}</div>
      <div id="scan-crypto" class="scan-panel">{crypto_scan_html}</div>
      <div id="scan-smallcap" class="scan-panel">{smallcap_scan_html}</div>
      <div id="scan-asx" class="scan-panel">{asx_scan_html}</div>
      <div id="scan-ftse" class="scan-panel">{ftse_scan_html}</div>
    </div>
  </div>
  <script>
  function showScan(tab, el) {{
    document.querySelectorAll('.scan-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-bar .tab').forEach(t => {{ t.style.borderBottomColor='transparent'; t.style.color='#444'; }});
    document.getElementById('scan-' + tab).classList.add('active');
    var colors = {{stocks:'#00aaff',crypto:'#00ff88',smallcap:'#ffcc00',asx:'#ffaa00',ftse:'#cc88ff'}};
    el.style.borderBottomColor = colors[tab] || '#e0e0e0';
    el.style.color = colors[tab] || '#e0e0e0';
  }}
  </script>

  <div style="margin-top:24px;padding:14px;background:rgba(255,204,0,0.04);border:1px solid rgba(255,204,0,0.12);border-radius:8px;font-size:11px;color:#666;line-height:1.8">
    ⚠ <strong style="color:#ffcc00">Safety:</strong>
    Stop: {stop_loss}% &nbsp;|&nbsp; Trail: {trailing_stop}% &nbsp;|&nbsp; TP: {take_profit}% &nbsp;|&nbsp;
    Max hold: {max_hold_days}d &nbsp;|&nbsp; Gap-down: {gap_down}% &nbsp;|&nbsp;
    Daily loss: ${max_loss:.0f} &nbsp;|&nbsp; Per trade: ${max_trade:.0f} &nbsp;|&nbsp; Daily spend: ${max_spend:.0f}
  </div>
</div>
<script>
var _t = 60; var _el = document.getElementById("refresh-timer");
setInterval(function() {{ _t--; if (_el) _el.textContent = "↻ refreshing in " + _t + "s"; if (_t<=0) window.location.reload(); }}, 1000);
</script>
</body>
</html>"""


# ── Build dashboard data ──────────────────────────────────────
def build_dashboard():
    acc    = cfg.account_info
    market = is_market_open()

    portfolio = f"${float(acc.get('portfolio_value', 0)):,.2f}" if acc else "—"
    port_val  = float(acc.get('portfolio_value', 1000000)) if acc else 1000000

    try:
        from zoneinfo import ZoneInfo as _ZI
    except ImportError:
        from backports.zoneinfo import ZoneInfo as _ZI
    _paris = _ZI("Europe/Paris")
    now_date = datetime.now(_paris).strftime("%A %d %B %Y")

    # ── Combined P&L — DB-backed (survives restarts) ──────────
    today_db_pnl = _db_today_pnl()
    combined_pnl_val = today_db_pnl  # authoritative source: DB
    # Also add any in-memory positions not yet closed (unrealised component visible)
    # Keep in-memory daily_pnl as indicator for session but show DB total for cards
    combined_pnl_pct = round((combined_pnl_val / port_val) * 100, 2) if port_val else 0
    combined_pnl = f"+${combined_pnl_val:.2f}" if combined_pnl_val >= 0 else f"-${abs(combined_pnl_val):.2f}"
    combined_pnl_color = "green" if combined_pnl_val >= 0 else "red"
    combined_pnl_pct_fmt = f"+{combined_pnl_pct:.2f}" if combined_pnl_pct >= 0 else f"{combined_pnl_pct:.2f}"

    # Today's trade count from DB
    from datetime import date as _date, timedelta as _td
    _today_str = _date.today().isoformat()
    trades_today_count, _, _ = _db_pnl_for_period(_today_str)

    # ── Period stats from DB ──────────────────────────────────
    try:
        _dow = _date.today().weekday()
        _this_week_start  = (_date.today() - _td(days=_dow)).isoformat()
        _last_week_start  = (_date.today() - _td(days=_dow+7)).isoformat()
        _last_week_end    = (_date.today() - _td(days=_dow)).isoformat()
        _this_month_start = _date.today().replace(day=1).isoformat()
        _lm_date          = _date.today().replace(day=1) - _td(days=1)
        _last_month_start = _lm_date.replace(day=1).isoformat()
        _last_month_end   = _date.today().replace(day=1).isoformat()

        tw_t, tw_pnl, tw_w = _db_pnl_for_period(_this_week_start)
        lw_t, lw_pnl, lw_w = _db_pnl_for_period(_last_week_start, _last_week_end)
        tm_t, tm_pnl, tm_w = _db_pnl_for_period(_this_month_start)
        lm_t, lm_pnl, lm_w = _db_pnl_for_period(_last_month_start, _last_month_end)

        conn = sqlite3.connect(DB_PATH)
        _7d = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), MAX(pnl), MIN(pnl) "
            "FROM trades WHERE side='SELL' AND created_at >= datetime('now','-7 days')"
        ).fetchone()
        conn.close()

        week_trades  = _7d[0] or 0
        week_pnl_val = _7d[1] or 0.0
        week_wins    = _7d[2] or 0
        week_best    = f"+${_7d[3]:.2f}" if _7d[3] else "—"
        week_worst   = f"-${abs(_7d[4]):.2f}" if _7d[4] else "—"
        week_win_rate = int(week_wins / week_trades * 100) if week_trades else 0
    except Exception as _e:
        log.debug(f"[DASH] Period stats error: {_e}")
        tw_t=tw_pnl=tw_w=lw_t=lw_pnl=lw_w=tm_t=tm_pnl=tm_w=lm_t=lm_pnl=lm_w=0
        week_trades=week_pnl_val=week_wins=week_win_rate=0
        week_best=week_worst="—"

    week_pnl       = f"+${week_pnl_val:.2f}" if week_pnl_val >= 0 else f"-${abs(week_pnl_val):.2f}"
    week_pnl_color = "green" if week_pnl_val >= 0 else "red"

    def _fmt_pnl(v):   return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
    def _pnl_color(v): return "#00cc66" if v >= 0 else "#ff4466"
    def _wr(t, w):     return f"{int(w/t*100)}%" if t else "—"
    def _pct_ret(pnl, port): return round(pnl / port * 100, 2) if port else 0.0
    def _fmt_pct(v):   return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"
    def _vs(cur, prev):
        if prev == 0: return "—"
        diff = cur - prev
        return (f'<span style="color:#00cc66;font-size:10px">▲ {abs(diff):.1f}% vs prior</span>'
                if diff >= 0 else f'<span style="color:#ff4466;font-size:10px">▼ {abs(diff):.1f}% vs prior</span>')

    _now_paris      = datetime.now(_paris)
    _this_month_name = _now_paris.strftime("%B")
    _lm_name        = (_now_paris.replace(day=1) - _td(days=1)).strftime("%B")
    _this_week_label = f"Wk {_now_paris.strftime('%d %b')} →"

    tm_pct   = _pct_ret(tm_pnl, port_val)
    lm_pct   = _pct_ret(lm_pnl, port_val)
    tw_pct   = _pct_ret(tw_pnl, port_val)
    lw_pct   = _pct_ret(lw_pnl, port_val)
    tm_vs_lm = _vs(tm_pct, lm_pct)
    tw_vs_lw = _vs(tw_pct, lw_pct)

    # ── Bot status dots ───────────────────────────────────────
    sc_dot     = ("dot-green" if smallcap_state.running else "dot-gold") if not smallcap_state.shutoff else "dot-red"
    sc_status  = "Shut Off" if smallcap_state.shutoff else ("Running" if smallcap_state.running else "Idle")
    id_dot     = ("dot-green" if intraday_state.running else "dot-gold") if not intraday_state.shutoff else "dot-red"
    id_status  = "Shut Off" if intraday_state.shutoff else ("Running" if intraday_state.running else ("Window Closed" if not is_intraday_window() else "Idle"))
    cid_dot    = ("dot-green" if crypto_intraday_state.running else "dot-gold") if not crypto_intraday_state.shutoff else "dot-red"
    cid_status = "Shut Off" if crypto_intraday_state.shutoff else ("Running" if crypto_intraday_state.running else "Idle")

    # ── Performance stats from DB ─────────────────────────────
    total_trades_db, total_pnl_db, wins_count, losses_count, avg_score_db = _db_all_time_stats()
    win_rate        = int(wins_count / total_trades_db * 100) if total_trades_db else 0
    trades_wr_color = "green" if win_rate >= 55 else ("orange" if win_rate >= 45 else "red")
    max_dd          = round(perf["max_drawdown"], 1)
    dd_color        = "green" if max_dd < 5 else ("orange" if max_dd < 10 else "red")
    peak_pv         = f"{perf['peak_portfolio']:,.0f}"
    pf              = calc_profit_factor()
    profit_factor   = f"{pf:.2f}" if pf != float("inf") else "∞"
    pf_color        = "green" if pf >= 1.5 else ("orange" if pf >= 1.0 else "red")
    sharpe_val      = calc_sharpe()
    sharpe          = f"{sharpe_val:.2f}" if sharpe_val else "—"
    sharpe_color    = "green" if (sharpe_val and sharpe_val >= 1.0) else ("orange" if (sharpe_val and sharpe_val >= 0.5) else "#888")
    loss_streak     = global_risk["loss_streak"]
    streak_color    = "red" if loss_streak >= LOSS_STREAK_LIMIT else ("orange" if loss_streak >= 2 else "green")
    pause_until     = global_risk.get("paused_until")
    pause_status    = pause_until.strftime("%H:%M") if pause_until and datetime.now() < pause_until else "None"
    vix_val_now     = global_risk.get("vix_level")
    vix_level       = f"{vix_val_now:.1f}" if vix_val_now else "—"
    vix_color       = "red" if (vix_val_now and vix_val_now >= VIX_EXTREME) else ("orange" if (vix_val_now and vix_val_now >= VIX_HIGH_THRESHOLD) else "green")
    size_mult       = round(vol_adjusted_size(1.0), 2)
    global_pos      = all_positions_count()

    # ── Circuit breaker banner ────────────────────────────────
    if circuit_breaker["active"]:
        circuit_banner = (
            f'<div style="margin-bottom:16px;padding:16px 20px;border-radius:12px;'
            f'background:rgba(255,68,102,0.15);border:2px solid #ff4466;display:flex;align-items:center;gap:16px">'
            f'<div style="font-size:28px">🚨</div>'
            f'<div><div style="font-size:16px;font-weight:700;color:#ff4466">CIRCUIT BREAKER ACTIVE — ALL NEW BUYS PAUSED</div>'
            f'<div style="font-size:12px;color:#888;margin-top:4px">Reason: {circuit_breaker["reason"]} · Triggered: {circuit_breaker["triggered_at"]}</div>'
            f'</div></div>'
        )
    else:
        circuit_banner = ""

    # ── Regime data ───────────────────────────────────────────
    regime     = market_regime["mode"]
    c_regime   = crypto_regime["mode"]
    vix_str    = f"{market_regime['vix']:.1f}" if market_regime["vix"] else "N/A"
    spy_str    = f"${market_regime['spy_price']:.2f}" if market_regime["spy_price"] else "N/A"
    spy_ma_str = f"${market_regime['spy_ma20']:.2f}" if market_regime["spy_ma20"] else "N/A"
    btc_str    = f"${crypto_regime['btc_price']:.0f}" if crypto_regime["btc_price"] else "N/A"
    btc_ma_str = f"${crypto_regime['btc_ma20']:.0f}" if crypto_regime["btc_ma20"] else "N/A"
    btc_chg_str   = f"{crypto_regime['btc_change']:+.1f}%" if crypto_regime["btc_change"] is not None else "N/A"
    btc_chg_color = "red" if crypto_regime["btc_change"] and crypto_regime["btc_change"] < -BTC_CRASH_PCT else "#e0e0e0"
    exposure_str        = f"{total_exposure(state):.0f}"
    crypto_exposure_str = f"{total_exposure(crypto_state):.0f}"

    # ── ASX regime data ───────────────────────────────────────
    asx_mode         = asx_regime.get("mode", "BULL")
    asx_regime_color  = "#ffaa00" if asx_mode == "BULL" else "#ff4466"
    asx_regime_bg     = "rgba(255,170,0,0.05)" if asx_mode == "BULL" else "rgba(255,68,102,0.05)"
    asx_regime_border = "rgba(255,170,0,0.2)"  if asx_mode == "BULL" else "rgba(255,68,102,0.2)"
    asx_regime_icon   = "🐂" if asx_mode == "BULL" else "🐻"
    asx_cba_str       = f"${asx_regime['spy']:.2f}" if asx_regime.get("spy") else "N/A"
    asx_ma_str        = f"${asx_regime['ma20']:.2f}" if asx_regime.get("ma20") else "N/A"
    from app.main import is_asx_open, is_ftse_open
    _asx_open   = is_asx_open()
    asx_hours   = "OPEN" if _asx_open else "CLOSED"
    asx_hours_color = "#ffaa00" if _asx_open else "#555"
    asx_pos_count   = len(asx_state.positions)
    asx_dot         = "dot-amber" if _asx_open else "dot-gold"
    asx_status      = "Scanning" if _asx_open else "Market Closed"

    # ── FTSE regime data ──────────────────────────────────────
    ftse_mode         = ftse_regime.get("mode", "BULL")
    ftse_regime_color  = "#cc88ff" if ftse_mode == "BULL" else "#ff4466"
    ftse_regime_bg     = "rgba(204,136,255,0.05)" if ftse_mode == "BULL" else "rgba(255,68,102,0.05)"
    ftse_regime_border = "rgba(204,136,255,0.2)"  if ftse_mode == "BULL" else "rgba(255,68,102,0.2)"
    ftse_regime_icon   = "🐂" if ftse_mode == "BULL" else "🐻"
    ftse_hsba_str      = f"${ftse_regime['spy']:.2f}" if ftse_regime.get("spy") else "N/A"
    ftse_ma_str        = f"${ftse_regime['ma20']:.2f}" if ftse_regime.get("ma20") else "N/A"
    _ftse_open  = is_ftse_open()
    ftse_hours  = "OPEN" if _ftse_open else "CLOSED"
    ftse_hours_color = "#cc88ff" if _ftse_open else "#555"
    ftse_pos_count   = len(ftse_state.positions)
    ftse_dot         = "dot-purple" if _ftse_open else "dot-gold"
    ftse_status      = "Scanning" if _ftse_open else "Market Closed"

    def pnl_str(v):    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
    def pnl_color(v):  return "green" if v >= 0 else "red"
    def dot_for(st):   return "dot-red" if st.shutoff else ("dot-green" if st.running else "dot-gold")
    def status_for(st): return "Shut Off" if st.shutoff else ("Running" if st.running else "Idle")

    # ── Kill switch banner ────────────────────────────────────
    if kill_switch["active"]:
        kill_banner = (
            f'<div style="background:rgba(255,68,102,0.15);border:1px solid #ff4466;border-radius:8px;'
            f'padding:14px 20px;margin-bottom:16px;display:flex;align-items:center;gap:12px">'
            f'<span style="font-size:20px">🛑</span>'
            f'<div><div style="font-weight:700;color:#ff4466;font-size:14px">KILL SWITCH ACTIVE — All bots stopped</div>'
            f'<div style="font-size:12px;color:#888;margin-top:2px">{kill_switch["reason"]} · {kill_switch["activated_at"]}</div>'
            f'</div></div>'
        )
    else:
        kill_banner = ""

    # ── Positions table ───────────────────────────────────────
    all_pos = (
        [(sym, pos, "blue",  "Stock")   for sym, pos in state.positions.items()] +
        [(sym, pos, "green", "Crypto")  for sym, pos in crypto_state.positions.items()] +
        [(sym, pos, "gold",  "SmCap")   for sym, pos in smallcap_state.positions.items()] +
        [(sym, pos, "blue",  "ID")      for sym, pos in intraday_state.positions.items()] +
        [(sym, pos, "green", "CrypID")  for sym, pos in crypto_intraday_state.positions.items()] +
        [(sym, pos, "amber", "ASX")     for sym, pos in asx_state.positions.items()] +
        [(sym, pos, "purple","FTSE")    for sym, pos in ftse_state.positions.items()]
    )
    if all_pos:
        from core.execution import fetch_latest_price
        rows = ""
        for idx, (sym, pos, color, category) in enumerate(all_pos):
            is_crypto = category in ("Crypto", "CrypID")
            try:
                live = fetch_latest_price(sym, crypto=is_crypto) or pos.get("highest_price", pos["entry_price"])
            except:
                live = pos.get("highest_price", pos["entry_price"])
            entry   = pos["entry_price"]
            pnl     = (live - entry) * pos["qty"]
            pnl_pct = ((live - entry) / entry) * 100
            pnl_c   = "green" if pnl >= 0 else "red"
            sign    = "+" if pnl >= 0 else ""
            entry_ts = pos.get("entry_ts", "")
            paris = ZoneInfo("Europe/Paris")
            now_paris = datetime.now(paris)
            try:
                dt = datetime.fromisoformat(entry_ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                dt_paris = dt.astimezone(paris)
                held = now_paris - dt_paris
                held_hrs  = int(held.total_seconds() // 3600)
                held_mins = int((held.total_seconds() % 3600) // 60)
                if held_hrs >= 24:
                    held_days = held_hrs // 24
                    held_rem  = held_hrs % 24
                    entry_dt  = f"Held {held_days}d {held_rem}h"
                else:
                    entry_dt = f"Held {held_hrs}h {held_mins}m"
            except:
                entry_dt = pos.get("entry_date", "—")
            cat_colors = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00","ID":"#aa88ff","CrypID":"#00ff88","ASX":"#ffaa00","FTSE":"#cc88ff"}
            cat_color  = cat_colors.get(category, "#555")
            stop_pct   = round(((pos["stop_price"] - entry) / entry) * 100, 1)
            tp_price   = pos.get("take_profit_price", entry * 1.10)
            tp_pct     = round(((tp_price - entry) / entry) * 100, 1)
            days_held  = pos.get("days_held", 0)
            score      = pos.get("signal_score", "—")

            rows += (
                f'<tr>'
                f'<td style="font-weight:700" class="{color}">{sym}</td>'
                f'<td><span style="font-size:10px;color:{cat_color};font-weight:700">{category}</span></td>'
                f'<td style="color:#555;font-size:11px">{entry_dt}</td>'
                f'<td style="font-family:monospace">${entry:.4f}</td>'
                f'<td style="font-family:monospace;color:#00aaff">${live:.4f}</td>'
                f'<td class="red" style="font-family:monospace">${pos["stop_price"]:.4f} ({stop_pct:+.1f}%)</td>'
                f'<td class="{pnl_c}" style="font-weight:700;font-family:monospace">{sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)</td>'
                f'</tr>'
            )
        positions_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">Open Positions ({len(all_pos)})</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Type</th><th>Held</th><th>Entry $</th><th>Live $</th><th>Stop</th><th>P&L</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></div>'
        )
    else:
        positions_html = ""

    # ── Recent trades — from DB (survives restarts) ───────────
    db_trades = _db_recent_trades(limit=10)
    if db_trades:
        rows = ""
        for sym, pnl, side, created_at, score in db_trades:
            pc   = "green" if pnl >= 0 else "red"
            sign = "+" if pnl >= 0 else ""
            ts   = created_at[:16] if created_at else "—"
            rows += (
                f'<tr><td>{"✅" if pnl>0 else "❌"}</td>'
                f'<td style="font-weight:700;color:#00aaff">{sym}</td>'
                f'<td style="color:#555">{ts}</td>'
                f'<td class="{pc}" style="font-weight:700">{sign}${pnl:.2f}</td>'
                f'<td style="color:#555">{score or "—"}</td></tr>'
            )
        trades_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">Recent Trades (DB)</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th></th><th>Symbol</th><th>Time</th><th>P&L</th><th>Score</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
            f'<div style="margin-top:8px;font-size:11px;color:#555">Total: {total_trades_db} trades · '
            f'<span style="color:{"#00cc66" if total_pnl_db>=0 else "#ff4466"}">'
            f'{"+" if total_pnl_db>=0 else ""}${total_pnl_db:.2f}</span> all-time P&L · '
            f'{win_rate}% win rate</div>'
            f'</div>'
        )
    else:
        trades_html = '<div class="card" style="margin-bottom:20px"><div class="empty">No completed trades yet</div></div>'

    # ── Current BUY signals screener ──────────────────────────
    all_cands = (
        [dict(c, market="Stock")    for c in state.candidates
         if c["signal"] == "BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="Intraday") for c in intraday_state.candidates
         if c["signal"] == "BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="SmallCap") for c in smallcap_state.candidates
         if c["signal"] == "BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="ASX")      for c in asx_state.candidates
         if c["signal"] == "BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="FTSE")     for c in ftse_state.candidates
         if c["signal"] == "BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="Crypto")   for c in crypto_intraday_state.candidates
         if c["signal"] == "BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE]
    )
    if all_cands:
        rows = ""
        for c in all_cands:
            mc   = "blue" if c["market"] == "Stock" else "green"
            cc   = "green" if c["change"] >= 0 else "red"
            rsi  = c.get("rsi")
            if rsi:
                if 50 <= rsi <= 65:  rsi_color = "#00ff88"; rsi_label = f"{rsi:.1f} ✅"
                elif rsi > 75:       rsi_color = "#ff4466"; rsi_label = f"{rsi:.1f} 🔴"
                elif rsi > 65:       rsi_color = "#ffcc00"; rsi_label = f"{rsi:.1f} ⚠"
                else:                rsi_color = "#555";    rsi_label = f"{rsi:.1f}"
            else:
                rsi_color = "#555"; rsi_label = "—"
            vr = c.get("vol_ratio", 0)
            if vr >= 2.0:    vol_color = "#00ff88"; vol_label = f"{vr:.2f}x 🔥"
            elif vr >= 1.5:  vol_color = "#00aaff"; vol_label = f"{vr:.2f}x ✅"
            else:             vol_color = "#555";    vol_label = f"{vr:.2f}x" if vr else "—"
            sc = c.get("score", 0)
            chg_sign = "+" if c["change"] >= 0 else ""
            rows += (
                f'<tr><td class="{mc}" style="font-weight:700">{c["symbol"]}</td>'
                f'<td>{c["market"]}</td><td>${c["price"]:.4f}</td>'
                f'<td class="{cc}">{chg_sign}{c["change"]:.2f}%</td>'
                f'<td><span class="sig-buy">🟢 BUY {sc:.1f}</span></td>'
                f'<td style="color:{rsi_color};font-weight:700">{rsi_label}</td>'
                f'<td style="color:{vol_color};font-weight:700">{vol_label}</td></tr>'
            )
        screener_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">Current BUY Signals ({len(all_cands)})</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Type</th><th>Price</th><th>Chg%</th><th>Signal</th><th>RSI</th><th>Vol</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></div>'
        )
    else:
        screener_html = '<div class="card" style="margin-bottom:20px"><div class="empty">No BUY signals yet — waiting for first cycle</div></div>'

    # ── Full scan tables ──────────────────────────────────────
    def build_scan_table(candidates, color):
        if not candidates:
            return '<div class="empty">No scan data yet</div>'
        scored = []
        for c in candidates:
            sc = c.get("score") or 0 if c.get("intraday") else score_signal(c["symbol"], c["price"], c["change"], c.get("rsi"), c.get("vol_ratio"), c.get("closes", [c["price"]] * 22))
            scored.append((sc, c))
        bear_syms    = set(BEAR_TICKERS)
        bear_items   = sorted([(sc, c) for sc, c in scored if c["symbol"] in bear_syms], key=lambda x: -x[0])
        normal_items = sorted([(sc, c) for sc, c in scored if c["symbol"] not in bear_syms], key=lambda x: -x[0])
        scored = normal_items + bear_items

        rows = ""
        for sc, c in scored:
            sma9 = c.get("sma9"); sma21 = c.get("sma21")
            ema_gap = round(((sma9 - sma21) / sma21) * 100, 2) if sma9 and sma21 and sma21 > 0 else None
            ema_crossed = ema_gap is not None and ema_gap > 0
            score_ok    = sc >= MIN_SIGNAL_SCORE
            if score_ok and ema_crossed:
                sig_html = f'<span class="sig-buy">🟢 BUY {sc:.1f}</span>'
            elif score_ok and not ema_crossed:
                sig_html = f'<span style="background:rgba(0,170,255,0.15);color:#00aaff;border:1px solid #00aaff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">👀 WATCH {sc:.1f}</span>'
            elif not score_ok and ema_crossed:
                sig_html = f'<span style="background:rgba(255,204,0,0.1);color:#ffcc00;border:1px solid #ffcc00;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">⚡ SIGNAL {sc:.1f}</span>'
            elif c["signal"] == "SELL":
                sig_html = f'<span class="sig-sell">SELL</span>'
            else:
                sig_html = f'<span class="sig-hold">{sc:.1f}/{MIN_SIGNAL_SCORE}</span>'
            cc = "green" if c["change"] >= 0 else "red"
            rsi = c.get("rsi")
            if rsi:
                if 50 <= rsi <= 65:   rsi_color = "#00ff88"; rsi_label = f"{rsi:.1f} ✅"
                elif 40 <= rsi < 50:  rsi_color = "#00aaff"; rsi_label = f"{rsi:.1f} 📈"
                elif 65 < rsi <= 75:  rsi_color = "#ffcc00"; rsi_label = f"{rsi:.1f} ⚠"
                elif rsi > 75:        rsi_color = "#ff4466"; rsi_label = f"{rsi:.1f} 🔴"
                elif rsi < 30:        rsi_color = "#ff8800"; rsi_label = f"{rsi:.1f} 📉"
                else:                 rsi_color = "#555";    rsi_label = f"{rsi:.1f}"
            else:
                rsi_color = "#555"; rsi_label = "—"
            vr = c.get("vol_ratio", 0)
            if vr >= 2.0:    vol_color = "#00ff88"; vol_label = f"{vr:.2f}x 🔥"
            elif vr >= 1.5:  vol_color = "#00aaff"; vol_label = f"{vr:.2f}x ✅"
            elif vr >= 1.2:  vol_color = "#ffcc00"; vol_label = f"{vr:.2f}x ⚠"
            elif vr > 0:     vol_color = "#555";    vol_label = f"{vr:.2f}x"
            else:             vol_color = "#555";    vol_label = "—"
            threshold = MIN_SIGNAL_SCORE
            pct = min(100, int((sc / 11) * 100))
            if sc >= threshold:       bar_color = "#00ff88"; prox = f"✅ TRADE {sc:.1f}"
            elif sc >= threshold - 1: bar_color = "#ffcc00"; prox = f"🔥 {sc:.1f}/{threshold}"
            elif sc >= threshold - 2: bar_color = "#ff8800"; prox = f"⚡ {sc:.1f}/{threshold}"
            else:                      bar_color = "#333";    prox = f"{sc:.1f}/{threshold}"
            score_bar = (
                f'<div style="display:flex;align-items:center;gap:6px">'
                f'<div style="width:50px;height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden">'
                f'<div style="width:{pct}%;height:100%;background:{bar_color};border-radius:3px"></div></div>'
                f'<span style="font-size:11px;color:{bar_color};font-weight:700">{prox}</span></div>'
            )
            if ema_gap is not None:
                if ema_gap > 0:       ema_col = "#00ff88"; ema_str = f"+{ema_gap:.2f}% ✅"
                elif ema_gap > -0.5:  ema_col = "#ffcc00"; ema_str = f"{ema_gap:.2f}% 🔥"
                elif ema_gap > -1.5:  ema_col = "#ff8800"; ema_str = f"{ema_gap:.2f}% ⚡"
                else:                  ema_col = "#555";    ema_str = f"{ema_gap:.2f}%"
            else:
                ema_col = "#555"; ema_str = "—"
            bear_badge = (
                '<span style="font-size:9px;background:rgba(255,136,0,0.2);color:#ff8800;border:1px solid rgba(255,136,0,0.4);'
                'border-radius:4px;padding:1px 5px;margin-left:4px;font-weight:700">BEAR</span>'
                if c["symbol"] in bear_syms else ""
            )
            row_bg   = "background:rgba(255,136,0,0.04);" if c["symbol"] in bear_syms else ""
            chg_sign = "+" if c["change"] >= 0 else ""
            rows += (
                f'<tr style="{row_bg}">'
                f'<td style="font-weight:700" class="{color}">{c["symbol"]}{bear_badge}</td>'
                f'<td>${c["price"]:.4f}</td>'
                f'<td class="{cc}">{chg_sign}{c["change"]:.2f}%</td>'
                f'<td>{sig_html}</td>'
                f'<td>{score_bar}</td>'
                f'<td style="color:{ema_col};font-size:11px;font-weight:700">{ema_str}</td>'
                f'<td style="color:{rsi_color};font-size:11px;font-weight:700">{rsi_label}</td>'
                f'<td style="color:{vol_color};font-size:11px;font-weight:700">{vol_label}</td></tr>'
            )
        buys  = sum(1 for sc, c in scored if sc >= MIN_SIGNAL_SCORE and c.get("ema_gap", -99) > 0)
        watch = sum(1 for sc, c in scored if sc >= MIN_SIGNAL_SCORE and c.get("ema_gap", -99) <= 0)
        return (
            f'<div style="display:flex;gap:16px;margin-bottom:14px;font-size:12px;flex-wrap:wrap">'
            f'<span class="green" style="font-weight:700">🟢 {buys} BUY</span>'
            f'<span style="color:#00aaff;font-weight:700">👀 {watch} WATCH</span>'
            f'<span style="color:#444;margin-left:auto">{len(scored)} scanned</span></div>'
            f'<div style="overflow-x:auto"><table><thead><tr>'
            f'<th>Symbol</th><th>Price</th><th>Chg%</th><th>Signal</th>'
            f'<th>Score</th><th>EMA Cross</th><th>RSI</th><th>Vol</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
        )

    stocks_scan_html = build_scan_table(state.candidates, "blue")
    crypto_scan_html = build_scan_table(crypto_intraday_state.candidates, "green")
    asx_scan_html    = build_scan_table(asx_state.candidates, "amber")
    ftse_scan_html   = build_scan_table(ftse_state.candidates, "purple")

    if smallcap_state.candidates:
        smallcap_scan_html = build_scan_table(smallcap_state.candidates, "gold")
    elif smallcap_pool.get("symbols"):
        pool_size    = len(smallcap_pool["symbols"])
        last_refresh = smallcap_pool.get("last_refresh", "—")
        smallcap_scan_html = (
            f'<div style="padding:20px;text-align:center;color:#555">'
            f'<div style="font-size:14px;color:#ffcc00;margin-bottom:8px">📊 Pool ready — {pool_size} stocks loaded</div>'
            f'<div style="font-size:12px">Refreshed: {last_refresh}</div>'
            f'<div style="font-size:12px;margin-top:4px">Scan results will appear after next cycle</div>'
            f'</div>'
        )
    else:
        smallcap_scan_html = '<div class="empty">Small cap pool refreshing — check back after first cycle</div>'

    # ── News section ──────────────────────────────────────────
    if not news_state["scan_complete"]:
        if cfg.NEWS_API_KEY:
            news_html = '<div class="empty" style="padding:20px">Waiting for 9:00 AM ET morning scan...</div>'
        else:
            news_html = '<div style="padding:12px;background:rgba(255,204,0,0.05);border:1px solid rgba(255,204,0,0.2);border-radius:8px;font-size:12px;color:#888">⚠ Add <b style="color:#ffcc00">NEWS_API_KEY</b> to .env to enable news scanning</div>'
    else:
        skip_rows  = "".join(f'<tr><td style="font-weight:700;color:#ff4466">{sym}</td><td><span class="sig-sell">SKIP</span></td><td style="color:#888;font-size:12px">{d["reason"]}</td></tr>' for sym, d in news_state["skip_list"].items())
        boost_rows = "".join(f'<tr><td style="font-weight:700;color:#00ff88">{sym}</td><td><span class="sig-buy">POSITIVE</span></td><td style="color:#888;font-size:12px">{d["reason"]}</td></tr>' for sym, d in news_state["watch_list"].items())
        all_rows   = skip_rows + boost_rows
        news_html  = (
            f'<table><thead><tr><th>Symbol</th><th>Sentiment</th><th>Reason</th></tr></thead><tbody>{all_rows}</tbody></table>'
            f'<div style="margin-top:10px;font-size:11px;color:#555">{len(news_state["skip_list"])} skipped · {len(news_state["watch_list"])} positive</div>'
            if all_rows else '<div style="color:#555;font-size:13px;padding:8px 0">All clear — no negative news today.</div>'
        )
    news_scan_time = f"Last scan: {news_state.get('last_scan_time', '')} ET" if news_state.get("last_scan_time") else "Scans at 9:00 AM ET daily"

    return DASHBOARD_HTML.format(
        now=datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S"),
        now_date=now_date,
        combined_pnl=combined_pnl, combined_pnl_color=combined_pnl_color,
        combined_pnl_pct=combined_pnl_pct, combined_pnl_pct_fmt=combined_pnl_pct_fmt,
        trades_today_count=trades_today_count,
        mode_badge="badge-live" if IS_LIVE else "badge-paper",
        mode_label="LIVE" if IS_LIVE else "PAPER",
        portfolio=portfolio,
        stocks_pnl=pnl_str(state.daily_pnl), stocks_pnl_color=pnl_color(state.daily_pnl),
        crypto_pnl=pnl_str(crypto_state.daily_pnl), crypto_pnl_color=pnl_color(crypto_state.daily_pnl),
        market_status="Open" if market else "Closed",
        market_dot="dot-green" if market else "dot-red",
        stocks_dot=dot_for(state), stocks_status=status_for(state),
        stocks_cycle=state.cycle_count, stocks_positions=len(state.positions),
        stocks_spend=f"{state.daily_spend:.0f}", stocks_last=state.last_cycle or "—",
        stocks_trades=len(state.trades),
        crypto_dot=dot_for(crypto_state), crypto_status=status_for(crypto_state),
        crypto_cycle=crypto_state.cycle_count, crypto_positions=len(crypto_state.positions),
        crypto_last=crypto_state.last_cycle or "—", crypto_trades=len(crypto_state.trades),
        sc_dot=sc_dot, sc_status=sc_status, sc_cycle=smallcap_state.cycle_count,
        sc_positions=len(smallcap_state.positions), sc_trades=len(smallcap_state.trades),
        sc_last=smallcap_state.last_cycle or "—", sc_pool_size=len(smallcap_pool["symbols"]),
        id_dot=id_dot, id_status=id_status, id_cycle=intraday_state.cycle_count,
        id_positions=len(intraday_state.positions), id_trades=len(intraday_state.trades),
        id_last=intraday_state.last_cycle or "—",
        cid_dot=cid_dot, cid_status=cid_status, cid_cycle=crypto_intraday_state.cycle_count,
        positions_html=positions_html, trades_html=trades_html, screener_html=screener_html,
        stocks_scan_html=stocks_scan_html, crypto_scan_html=crypto_scan_html,
        smallcap_scan_html=smallcap_scan_html,
        asx_scan_html=asx_scan_html, ftse_scan_html=ftse_scan_html,
        exposure_str=exposure_str, crypto_exposure_str=crypto_exposure_str,
        win_rate=win_rate, trades_wr_color=trades_wr_color, wins=wins_count, losses=losses_count,
        max_dd=max_dd, dd_color=dd_color, peak_pv=peak_pv,
        profit_factor=profit_factor, pf_color=pf_color,
        sharpe=sharpe, sharpe_color=sharpe_color,
        loss_streak=loss_streak, streak_color=streak_color, streak_limit=LOSS_STREAK_LIMIT,
        pause_status=pause_status, vix_level=vix_level, vix_color=vix_color,
        size_mult=size_mult, global_pos=global_pos, max_global=MAX_TOTAL_POSITIONS,
        signal_threshold=MIN_SIGNAL_SCORE,
        tm_pnl_fmt=_fmt_pnl(tm_pnl), tm_color=_pnl_color(tm_pnl), tm_t=tm_t, tm_wr=_wr(tm_t, tm_w),
        lm_pnl_fmt=_fmt_pnl(lm_pnl), lm_color=_pnl_color(lm_pnl), lm_t=lm_t, lm_wr=_wr(lm_t, lm_w),
        tw_pnl_fmt=_fmt_pnl(tw_pnl), tw_color=_pnl_color(tw_pnl), tw_t=tw_t, tw_wr=_wr(tw_t, tw_w),
        lw_pnl_fmt=_fmt_pnl(lw_pnl), lw_color=_pnl_color(lw_pnl), lw_t=lw_t, lw_wr=_wr(lw_t, lw_w),
        tm_vs_lm=tm_vs_lm, tw_vs_lw=tw_vs_lw,
        this_month_name=_this_month_name, lm_name=_lm_name,
        this_week_label=_this_week_label,
        week_pnl=week_pnl, week_pnl_color=week_pnl_color, week_trades=week_trades,
        week_win_rate=week_win_rate, week_best=week_best, week_worst=week_worst,
        regime=regime, regime_color="red" if regime == "BEAR" else "green",
        regime_bg="rgba(255,68,102,0.08)" if regime == "BEAR" else "rgba(0,255,136,0.05)",
        regime_border="rgba(255,68,102,0.25)" if regime == "BEAR" else "rgba(0,255,136,0.15)",
        regime_icon="🐻" if regime == "BEAR" else "🐂",
        spy_str=spy_str, spy_ma_str=spy_ma_str, vix_str=vix_str,
        vix_regime_color="red" if market_regime["vix"] and market_regime["vix"] > VIX_FEAR_THRESHOLD else "#e0e0e0",
        c_regime=c_regime, c_regime_color="red" if c_regime == "BEAR" else "green",
        c_regime_bg="rgba(255,68,102,0.08)" if c_regime == "BEAR" else "rgba(0,255,136,0.05)",
        c_regime_border="rgba(255,68,102,0.25)" if c_regime == "BEAR" else "rgba(0,255,136,0.15)",
        c_regime_icon="🐻" if c_regime == "BEAR" else "🐂",
        btc_str=btc_str, btc_ma_str=btc_ma_str, btc_chg_str=btc_chg_str, btc_chg_color=btc_chg_color,
        news_html=news_html, news_scan_time=news_scan_time,
        dash_token=DASH_TOKEN,
        stop_loss=STOP_LOSS_PCT, trailing_stop=TRAILING_STOP_PCT, take_profit=TAKE_PROFIT_PCT,
        max_hold_days=MAX_HOLD_DAYS, gap_down=GAP_DOWN_PCT,
        max_loss=MAX_DAILY_LOSS, max_trade=MAX_TRADE_VALUE, max_spend=MAX_DAILY_SPEND,
        asx_regime_mode=asx_mode, asx_regime_color=asx_regime_color,
        asx_regime_bg=asx_regime_bg, asx_regime_border=asx_regime_border,
        asx_regime_icon=asx_regime_icon, asx_cba_str=asx_cba_str,
        asx_ma_str=asx_ma_str, asx_hours=asx_hours, asx_hours_color=asx_hours_color,
        asx_pos_count=asx_pos_count, asx_dot=asx_dot, asx_status=asx_status,
        ftse_regime_mode=ftse_mode, ftse_regime_color=ftse_regime_color,
        ftse_regime_bg=ftse_regime_bg, ftse_regime_border=ftse_regime_border,
        ftse_regime_icon=ftse_regime_icon, ftse_hsba_str=ftse_hsba_str,
        ftse_ma_str=ftse_ma_str, ftse_hours=ftse_hours, ftse_hours_color=ftse_hours_color,
        ftse_pos_count=ftse_pos_count, ftse_dot=ftse_dot, ftse_status=ftse_status,
        circuit_banner=circuit_banner, kill_banner=kill_banner,
    )


# ── Analytics page ────────────────────────────────────────────
def build_analytics_page(search_sym=None, report_id=None, period="all"):
    period_days  = {"90": 90, "30": 30, "all": None}.get(period, None)
    period_label = {"90": "Last 90 Days", "30": "Last 30 Days", "all": "All Time"}.get(period, "All Time")
    leaders      = db_get_leaderboard(limit=20, period_days=period_days)
    medal        = ["🥇", "🥈", "🥉"]

    lb_rows = ""
    for i, row in enumerate(leaders):
        sym, trades, wins, losses, total_pnl, best, worst, avg_sc = row[:8]
        win_rate = int(wins / trades * 100) if trades > 0 else 0
        pc   = "#00cc66" if total_pnl >= 0 else "#cc2244"
        rank = medal[i] if i < 3 else f"#{i+1}"
        lb_rows += (
            f'<tr onclick="searchSym(\'{sym}\')" style="cursor:pointer">'
            f'<td style="color:#888;font-weight:700">{rank}</td>'
            f'<td style="color:#00aaff;font-weight:700">{sym}</td>'
            f'<td>{trades}</td><td style="color:#00cc66">{wins}</td>'
            f'<td style="color:#cc2244">{losses}</td>'
            f'<td style="color:#888">{win_rate}%</td>'
            f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
            f'<td style="color:#00cc66">${best:+.2f}</td>'
            f'<td style="color:#cc2244">${worst:+.2f}</td>'
            f'<td style="color:#ffcc00">{avg_sc:.1f}</td></tr>'
        )
    if not lb_rows:
        lb_rows = '<tr><td colspan="10" style="text-align:center;color:#555;padding:20px">No trades yet</td></tr>'

    search_html = ""
    if search_sym:
        results = db_search_symbol(search_sym)
        stats   = results["stats"]
        if stats:
            sym2, total_t, wins2, losses2, total_pnl2, best2, worst2, avg_sc2, nm_count, last_t, first_t, _ = stats
            wr2 = int(wins2 / total_t * 100) if total_t > 0 else 0
            pc2 = "#00cc66" if total_pnl2 >= 0 else "#cc2244"
            search_html = (
                f'<div style="background:#0d1117;border:1px solid #1a3a5c;border-radius:12px;padding:20px;margin-bottom:20px">'
                f'<div style="font-size:20px;font-weight:700;color:#00aaff;margin-bottom:12px">{sym2}</div>'
                f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">'
                f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700;color:{pc2}">${total_pnl2:+.2f}</div><div style="font-size:10px;color:#555;text-transform:uppercase">Total P&L</div></div>'
                f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700">{total_t}</div><div style="font-size:10px;color:#555;text-transform:uppercase">Trades</div></div>'
                f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700;color:#00cc66">{wr2}%</div><div style="font-size:10px;color:#555;text-transform:uppercase">Win Rate</div></div>'
                f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700;color:#ffcc00">{nm_count}</div><div style="font-size:10px;color:#555;text-transform:uppercase">Near Misses</div></div>'
                f'</div></div>'
            )
        else:
            search_html = f'<div style="color:#555;padding:20px;text-align:center">No data for <b style="color:#00aaff">{search_sym}</b> yet</div>'

    reports     = db_get_reports(limit=30)
    report_rows = ""
    for r in reports:
        rid, rtype, rdate, subject = r
        icon     = "📊" if rtype == "daily" else "📈" if rtype == "weekly" else "☀️"
        type_col = "#00aaff" if rtype == "daily" else "#00cc66" if rtype == "weekly" else "#ffcc00"
        report_rows += (
            f'<tr onclick="loadReport({rid})" style="cursor:pointer">'
            f'<td style="padding:8px;color:{type_col}">{icon} {rtype.title()}</td>'
            f'<td style="padding:8px;color:#888">{rdate}</td>'
            f'<td style="padding:8px;color:#e0e0e0">{subject or "—"}</td></tr>'
        )
    if not report_rows:
        report_rows = '<tr><td colspan="3" style="padding:20px;text-align:center;color:#555">No reports yet</td></tr>'

    report_viewer = ""
    if report_id:
        report = db_get_report_by_id(int(report_id))
        if report:
            _, rtype, rdate, subject, body_html, body_text, _ = report
            report_viewer = (
                f'<div style="background:#0d1117;border:1px solid #1a3a5c;border-radius:12px;padding:20px;margin-bottom:20px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
                f'<div style="font-weight:700;color:#e0e0e0">{subject}</div>'
                f'<div style="color:#555;font-size:12px">{rdate}</div></div>'
                f'<div style="border-top:1px solid #1a1a1a;padding-top:16px;font-size:13px;line-height:1.6;color:#ccc;white-space:pre-wrap">{body_text}</div>'
                f'</div>'
            )

    skip_reasons    = db_get_skip_reason_breakdown()
    skip_reason_html = ""
    if skip_reasons:
        rows = "".join(f'<tr><td style="color:#ffcc00">{r[0]}</td><td>{r[1]}</td><td style="color:#00aaff">{r[2]:.1f}</td></tr>' for r in skip_reasons)
        skip_reason_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">📋 Skip Reason Breakdown</div>'
            f'<table><thead><tr><th>Reason</th><th>Count</th><th>Avg Score</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )

    total_trades_db, total_pnl_db, wins_db, losses_db, avg_score_db = _db_all_time_stats()
    try:
        conn = sqlite3.connect(DB_PATH)
        unique_syms  = conn.execute("SELECT COUNT(DISTINCT symbol) FROM trades").fetchone()[0] or 0
        total_misses = conn.execute("SELECT COUNT(*) FROM near_misses").fetchone()[0] or 0
        nm_rows      = conn.execute(
            "SELECT symbol, score, skip_reason, created_at, pct_move, NULL, triggered "
            "FROM near_misses ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
    except:
        unique_syms = total_misses = 0
        nm_rows = []

    if nm_rows:
        nr = ""
        for row in nm_rows:
            sym2, sc2, reason2, ts2, pct2, days2, checked2 = row
            ts_short = ts2[:10] if ts2 else "—"
            pct_str  = f"+{pct2:.1f}%" if pct2 and pct2 >= 0 else (f"{pct2:.1f}%" if pct2 else "Pending")
            pct_c    = "#00ff88" if pct2 and pct2 > 0 else ("#ff4466" if pct2 and pct2 < 0 else "#555")
            nr += (
                f'<tr><td style="font-weight:700;color:#ffcc00">{sym2}</td>'
                f'<td style="color:#ffcc00">{sc2}/10</td>'
                f'<td style="color:#888">{reason2 or "SCORE"}</td>'
                f'<td style="color:#555">{ts_short}</td>'
                f'<td style="color:{pct_c};font-weight:700">{pct_str}</td>'
                f'<td style="color:#555">{days2 or "—"}d</td>'
                f'<td>{"✅" if checked2 else "⏳"}</td></tr>'
            )
        near_miss_html = (
            f'<div class="card" style="border-color:rgba(255,136,0,0.2)">'
            f'<div class="section-title" style="color:#ff8800">🎯 Near-Miss Intelligence ({len(nm_rows)} tracked)</div>'
            f'<div style="overflow-x:auto"><table><thead><tr>'
            f'<th>Symbol</th><th>Score</th><th>Skip Reason</th><th>Date</th><th>Outcome</th><th>Days</th><th>Checked</th>'
            f'</tr></thead><tbody>{nr}</tbody></table></div>'
            f'<div style="font-size:11px;color:#555;margin-top:10px">Stocks just below threshold — tracked 5 days.</div>'
            f'</div>'
        )
        thresholds = [3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]
        bars_js = ""
        for thr in thresholds:
            qualifying = [r for r in nm_rows if r[1] >= thr and r[4] is not None]
            if qualifying:
                avg_pct   = sum(r[4] for r in qualifying) / len(qualifying)
                bar_color = "#00ff88" if avg_pct > 0 else "#ff4466"
                bar_h     = min(80, max(4, abs(avg_pct) * 8))
                bars_js  += (
                    f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1">'
                    f'<div style="font-size:10px;color:{bar_color};font-weight:700">{avg_pct:+.1f}%</div>'
                    f'<div style="width:100%;height:{bar_h}px;background:{bar_color};border-radius:3px 3px 0 0;opacity:0.8"></div>'
                    f'<div style="font-size:9px;color:#555;text-align:center">{thr}<br>{len(qualifying)}n</div></div>'
                )
            else:
                bars_js += (
                    f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1">'
                    f'<div style="font-size:10px;color:#333">—</div>'
                    f'<div style="width:100%;height:4px;background:#222;border-radius:3px 3px 0 0"></div>'
                    f'<div style="font-size:9px;color:#333;text-align:center">{thr}<br>0n</div></div>'
                )
        threshold_html = (
            f'<div class="card" style="border-color:rgba(255,204,0,0.15)">'
            f'<div class="section-title" style="color:#ffcc00">📈 Threshold Sensitivity — Avg Outcome by Min Score</div>'
            f'<div style="display:flex;align-items:flex-end;gap:8px;height:120px;padding:0 8px;border-bottom:1px solid #222;margin-bottom:8px">{bars_js}</div>'
            f'<div style="font-size:11px;color:#555">Use this to calibrate your minimum signal score before going live.</div>'
            f'</div>'
        )
    else:
        near_miss_html = (
            '<div class="card" style="border-color:rgba(255,136,0,0.2)">'
            '<div class="section-title" style="color:#ff8800">🎯 Near-Miss Intelligence</div>'
            '<div style="color:#444;font-size:13px;padding:12px 0">No near-misses tracked yet.</div>'
            '</div>'
        )
        threshold_html = (
            '<div class="card" style="border-color:rgba(255,204,0,0.15)">'
            '<div class="section-title" style="color:#ffcc00">📈 Threshold Sensitivity Chart</div>'
            '<div style="color:#444;font-size:13px;padding:12px 0">Chart populates once near-miss data is available.</div>'
            '</div>'
        )

    pnl_col_db = "#00cc66" if total_pnl_db >= 0 else "#cc2244"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot Analytics</title>
<style>
  * {{ box-sizing:border-box;margin:0;padding:0; }}
  body {{ background:#090b0e;color:#e0e0e0;font-family:'Segoe UI',sans-serif;font-size:14px; }}
  .container {{ padding:24px;max-width:1100px;margin:0 auto; }}
  .card {{ background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.07);border-radius:12px;padding:18px 20px;margin-bottom:20px; }}
  .section-title {{ font-size:15px;font-weight:700;margin-bottom:14px; }}
  table {{ width:100%;border-collapse:collapse;font-size:13px; }}
  th {{ font-size:10px;color:#444;letter-spacing:1.5px;text-transform:uppercase;padding:10px 12px;text-align:left; }}
  td {{ padding:9px 12px;border-top:1px solid rgba(255,255,255,0.04);font-family:monospace; }}
  tr:hover td {{ background:rgba(255,255,255,0.02); }}
  input {{ background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:10px 14px;color:#e0e0e0;font-size:14px;outline:none;width:70%; }}
  button {{ padding:10px 20px;background:rgba(0,170,255,0.15);border:1px solid rgba(0,170,255,0.3);border-radius:8px;color:#00aaff;font-size:13px;font-weight:700;cursor:pointer;margin-left:8px; }}
  .period-tab {{ padding:6px 14px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#555;font-size:11px;font-weight:700;cursor:pointer;margin-left:4px; }}
  .period-tab.active {{ background:rgba(0,170,255,0.15);border-color:rgba(0,170,255,0.3);color:#00aaff; }}
</style>
</head>
<body>
<div style="background:#0d1117;border-bottom:1px solid #1a1a1a;padding:16px 24px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:100">
  <a href="/" style="color:#555;text-decoration:none;font-size:13px">← Dashboard</a>
  <span style="color:#333">|</span>
  <span style="font-size:16px;font-weight:700;color:#00aaff">🧠 Trading Intelligence</span>
</div>
<div class="container">
  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px">
    <div class="card" style="text-align:center"><div style="font-size:24px;font-weight:700;color:{pnl_col_db}">${total_pnl_db:+.2f}</div><div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Total P&L</div></div>
    <div class="card" style="text-align:center"><div style="font-size:24px;font-weight:700;color:#00aaff">{total_trades_db}</div><div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Total Trades</div></div>
    <div class="card" style="text-align:center"><div style="font-size:24px;font-weight:700;color:#00ff88">{unique_syms}</div><div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Symbols Traded</div></div>
    <div class="card" style="text-align:center"><div style="font-size:24px;font-weight:700;color:#ffcc00">{avg_score_db:.1f}</div><div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Avg Score</div></div>
    <div class="card" style="text-align:center"><div style="font-size:24px;font-weight:700;color:#ff8800">{total_misses}</div><div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Near Misses</div></div>
  </div>
  <div class="card">
    <div class="section-title">🔍 Stock / Crypto Search</div>
    <div style="display:flex;align-items:center;margin-bottom:12px">
      <input type="text" id="search-input" placeholder="Search any ticker — e.g. NVDA, BTCUSDT..." value="{search_sym or ''}" onkeydown="if(event.key==='Enter') doSearch()">
      <button onclick="doSearch()">Search</button>
    </div>
    {search_html}
  </div>
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <div class="section-title" style="margin:0">🏆 Leaderboard — {period_label}</div>
      <div>
        <button class="period-tab {'active' if period=='30' else ''}" onclick="setPeriod('30')">30 Days</button>
        <button class="period-tab {'active' if period=='90' else ''}" onclick="setPeriod('90')">90 Days</button>
        <button class="period-tab {'active' if period=='all' else ''}" onclick="setPeriod('all')">All Time</button>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table><thead><tr><th>Rank</th><th>Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th>
    <th>Win Rate</th><th>Total P&L</th><th>Best</th><th>Worst</th><th>Avg Score</th>
    </tr></thead><tbody>{lb_rows}</tbody></table></div>
  </div>
  {skip_reason_html}
  {near_miss_html}
  {threshold_html}
  <div class="card">
    <div class="section-title">📁 Report Archive</div>
    {report_viewer}
    <table><thead><tr><th>Type</th><th>Date</th><th>Subject</th></tr></thead><tbody>{report_rows}</tbody></table>
  </div>
</div>
<script>
function doSearch() {{ var s=document.getElementById('search-input').value.trim().toUpperCase(); if(s) window.location.href='/analytics?search='+encodeURIComponent(s); }}
function searchSym(s) {{ window.location.href='/analytics?search='+encodeURIComponent(s); }}
function setPeriod(p) {{ window.location.href='/analytics?period='+p; }}
function loadReport(id) {{ window.location.href='/analytics?report_id='+id; }}
</script>
</body>
</html>"""


# ── HTTP Handler ──────────────────────────────────────────────
class DashboardHandler(BaseHTTPRequestHandler):

    def _check_auth(self):
        return True  # re-enable before going live

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK"); return

        if self.path == "/api":
            with _state_lock:
                data = json.dumps({
                    "stocks":      {"pnl": state.daily_pnl, "positions": len(state.positions), "trades": len(state.trades), "cycle": state.cycle_count},
                    "crypto":      {"pnl": crypto_state.daily_pnl, "positions": len(crypto_state.positions), "trades": len(crypto_state.trades), "cycle": crypto_state.cycle_count},
                    "portfolio":   float(cfg.account_info.get("portfolio_value", 0)) if cfg.account_info else 0,
                    "kill_switch": kill_switch["active"],
                    "today_pnl":   _db_today_pnl(),
                })
            self._json(data); return

        if self.path.startswith("/analytics"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                html = build_analytics_page(
                    search_sym=params.get("search", [None])[0],
                    report_id=params.get("report_id", [None])[0],
                    period=params.get("period", ["all"])[0]
                )
            except Exception as e:
                log.error(f"[ANALYTICS] Failed: {e}")
                html = f"<html><body style='background:#111;color:#fff;padding:40px'><h2>Analytics Error</h2><pre>{e}</pre></body></html>"
            self._html(html); return

        try:
            with _state_lock:
                html = build_dashboard()
        except Exception as e:
            import traceback
            log.error(f"[DASHBOARD] build_dashboard() failed: {e}")
            log.error(traceback.format_exc())
            html = f"<html><body style='background:#111;color:#fff;padding:40px;font-family:monospace'><h2>Dashboard Error</h2><pre>{e}</pre></body></html>"
        self._html(html)

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs, parse_qsl
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        parsed_path  = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)
        submitted_pin = query_params.get("pin", [None])[0]
        base_path    = parsed_path.path

        if base_path in ("/kill", "/close-all", "/resume") and submitted_pin is not None:
            if submitted_pin != KILL_PIN:
                self._json(json.dumps({"status": "wrong_pin"}))
                log.warning(f"[SECURITY] Wrong PIN attempt on {base_path}")
                return
            self.path = base_path

        if base_path == "/login":
            params = dict(parse_qsl(body))
            if params.get("username") == DASH_USER and params.get("password") == DASH_PASS:
                self.send_response(302)
                self.send_header("Location", f"/?token={DASH_TOKEN}")
                self.send_header("Set-Cookie", f"auth={DASH_TOKEN}; Path=/; SameSite=Lax")
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header("Location", "/?error=1")
                self.end_headers()
            return

        if base_path == "/kill":
            kill_switch.update({"active": True, "reason": "Manual kill from dashboard", "activated_at": datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")})
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, asx_state, ftse_state]:
                st.shutoff = True
            log.warning("[KILL SWITCH] Manual kill activated from dashboard")
            self._json(json.dumps({"status": "killed"}))

        elif base_path == "/resume":
            kill_switch.update({"active": False, "reason": "", "activated_at": None})
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, asx_state, ftse_state]:
                st.shutoff = False
            log.info("[KILL SWITCH] Resumed from dashboard")
            self._json(json.dumps({"status": "resumed"}))

        elif base_path == "/close-all":
            log.warning("[KILL SWITCH] Close all positions requested from dashboard")
            for sym, pos in list(state.positions.items()):
                place_order(sym, "sell", pos["qty"], estimated_price=pos["entry_price"])
                if sym in exchange_stops:
                    cancel_stop_order_ibkr(exchange_stops.pop(sym))
            for sym, pos in list(crypto_state.positions.items()):
                place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
            state.positions.clear()
            crypto_state.positions.clear()
            kill_switch.update({"active": True, "reason": "Close all — liquidated from dashboard", "activated_at": datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")})
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, asx_state, ftse_state]:
                st.shutoff = True
            self._json(json.dumps({"status": "closed"}))

        else:
            self.send_response(404); self.end_headers()

    def _html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data.encode() if isinstance(data, str) else data)

    def log_message(self, format, *args):
        pass  # suppress access logs


def start_dashboard():
    server = ThreadedHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    log.info(f"Dashboard running on port {PORT}")
    server.serve_forever()
