"""
app/dashboard.py — AlphaBot Web Dashboard
HTTP server, all HTML templates, dashboard builder, analytics page, kill switch endpoints.
Access at http://YOUR_HETZNER_IP:8080
"""

import json
import sqlite3
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    global_risk, perf, kill_switch, circuit_breaker,
    market_regime, crypto_regime, news_state, smallcap_pool,
    exchange_stops, account_info, near_miss_tracker,
    CRYPTO_WATCHLIST, DB_PATH,
    _state_lock,
)
import core.config as cfg
from core.risk import (
    total_exposure, all_positions_count, calc_profit_factor, calc_sharpe,
    vol_adjusted_size, is_market_open, is_intraday_window,
)
from core.execution import place_order, cancel_stop_order_alpaca
from data.analytics import score_signal
from data.database import (
    db_get_leaderboard, db_search_symbol, db_get_skip_reason_breakdown,
    db_get_reports, db_get_report_by_id,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


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
  .tab-bar {{ display: flex; border-bottom: 1px solid rgba(255,255,255,0.06); margin-bottom: 20px; }}
  .tab {{ padding: 10px 16px; cursor: pointer; font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
          text-transform: uppercase; color: #444; border-bottom: 2px solid transparent; text-decoration: none; }}
  .tab-stocks.active {{ color: #00aaff; border-bottom-color: #00aaff; }}
  .tab-crypto.active {{ color: #00ff88; border-bottom-color: #00ff88; }}
  .tab:hover {{ color: #e0e0e0; }}
  .empty {{ text-align: center; padding: 50px; color: #333; font-size: 15px; }}
  .scan-panel {{ display: none; }} .scan-panel.active {{ display: block; }}
  .bot-status-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 13px; }}
  .regime-stats {{ display: flex; gap: 10px; font-size: 11px; flex-wrap: wrap; }}
  @media(max-width:768px) {{
    .container {{ padding: 10px; }}
    .header {{ padding: 10px 14px; flex-direction: column; align-items: flex-start; gap: 6px; }}
    .header-right {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; width: 100%; }}
    .refresh {{ display: none; }}
    .grid4 {{ grid-template-columns: 1fr 1fr; gap: 10px; }}
    .grid2 {{ grid-template-columns: 1fr; gap: 10px; }}
    .card {{ padding: 12px 14px; }}
    .big {{ font-size: 18px; }}
    .bot-status-grid {{ grid-template-columns: 1fr 1fr !important; gap: 6px !important; font-size: 11px !important; }}
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
      <div class="logo-sub">Automated Day Trader · Hetzner</div>
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

  <!-- Market Regime Banners -->
  <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:16px">
    <div style="padding:12px 14px;border-radius:12px;background:{regime_bg};border:1px solid {regime_border}">
      <div style="font-size:9px;letter-spacing:2px;color:#888;text-transform:uppercase;margin-bottom:2px">US Stocks</div>
      <div style="font-size:16px;font-weight:700;color:{regime_color}">{regime_icon} {regime}</div>
      <div class="regime-stats" style="margin-top:6px">
        <span><span style="color:#555">SPY </span><span style="font-family:monospace;font-weight:700">{spy_str}</span></span>
        <span><span style="color:#555">MA20 </span><span style="font-family:monospace;color:#777">{spy_ma_str}</span></span>
        <span><span style="color:#555">VIX </span><span style="font-family:monospace;color:{vix_regime_color}">{vix_str}</span></span>
        <span><span style="color:#555">Exp </span><span style="font-family:monospace">${exposure_str}</span></span>
      </div>
    </div>
    <div style="padding:12px 14px;border-radius:12px;background:{c_regime_bg};border:1px solid {c_regime_border}">
      <div style="font-size:9px;letter-spacing:2px;color:#888;text-transform:uppercase;margin-bottom:2px">Crypto</div>
      <div style="font-size:16px;font-weight:700;color:{c_regime_color}">{c_regime_icon} {c_regime}</div>
      <div class="regime-stats" style="margin-top:6px">
        <span><span style="color:#555">BTC </span><span style="font-family:monospace;font-weight:700">{btc_str}</span></span>
        <span><span style="color:#555">MA20 </span><span style="font-family:monospace;color:#777">{btc_ma_str}</span></span>
        <span><span style="color:#555">Chg </span><span style="font-family:monospace;color:{btc_chg_color}">{btc_chg_str}</span></span>
        <span><span style="color:#555">Exp </span><span style="font-family:monospace">${crypto_exposure_str}</span></span>
      </div>
    </div>
  </div>

  {kill_banner}
  {circuit_banner}

  <!-- Top stats -->
  <div class="grid4">
    <div class="card"><div class="lbl">Portfolio</div><div class="big blue">{portfolio}</div></div>
    <div class="card"><div class="lbl">US P&L Today</div><div class="big {stocks_pnl_color}">{stocks_pnl}</div></div>
    <div class="card card-green"><div class="lbl">Crypto P&L Today</div><div class="big {crypto_pnl_color}">{crypto_pnl}</div></div>
    <div class="card"><div class="lbl">Market</div><div style="margin-top:6px;display:flex;align-items:center"><span class="dot {market_dot}"></span><span style="font-weight:700;font-size:13px">{market_status}</span></div></div>
  </div>

  <!-- Bot status grid -->
  <div class="grid2">
    <div class="card card-blue">
      <div class="section-title blue">📈 US Stocks Bot</div>
      <div class="bot-status-grid">
        <div><div class="lbl">Status</div><span class="dot {stocks_dot}"></span>{stocks_status}</div>
        <div><div class="lbl">Cycle</div>#{stocks_cycle}</div>
        <div><div class="lbl">Open Positions</div><span style="font-weight:700">{stocks_positions}</span></div>
        <div><div class="lbl">Daily Spend</div>${stocks_spend} / ${max_spend}</div>
        <div><div class="lbl">Last Run</div><span style="color:#555">{stocks_last}</span></div>
        <div><div class="lbl">Trades Today</div>{stocks_trades}</div>
      </div>
    </div>
    <div class="card card-green">
      <div class="section-title green">🪙 Crypto Bot</div>
      <div class="bot-status-grid">
        <div><div class="lbl">Status</div><span class="dot {crypto_dot}"></span>{crypto_status}</div>
        <div><div class="lbl">Cycle</div>#{crypto_cycle}</div>
        <div><div class="lbl">Open Positions</div><span style="font-weight:700">{crypto_positions}</span></div>
        <div><div class="lbl">24/7 Mode</div><span class="green">Always On</span></div>
        <div><div class="lbl">Last Run</div><span style="color:#555">{crypto_last}</span></div>
        <div><div class="lbl">Trades Today</div>{crypto_trades}</div>
      </div>
    </div>
    <div class="card">
      <div class="section-title" style="color:#ffcc00">📊 Small Cap Bot</div>
      <div class="bot-status-grid">
        <div><div class="lbl">Status</div><span class="dot {sc_dot}"></span>{sc_status}</div>
        <div><div class="lbl">Cycle</div>#{sc_cycle}</div>
        <div><div class="lbl">Positions</div><span style="font-weight:700">{sc_positions}</span></div>
        <div><div class="lbl">Pool Size</div>{sc_pool_size}</div>
        <div><div class="lbl">Last Run</div><span style="color:#555">{sc_last}</span></div>
        <div><div class="lbl">Trades</div>{sc_trades}</div>
      </div>
    </div>
    <div class="card">
      <div class="section-title" style="color:#aa88ff">⚡ Intraday Bots</div>
      <div class="bot-status-grid">
        <div><div class="lbl">Stocks ID</div><span class="dot {id_dot}"></span>{id_status}</div>
        <div><div class="lbl">ID Cycle</div>#{id_cycle}</div>
        <div><div class="lbl">ID Positions</div>{id_positions}</div>
        <div><div class="lbl">ID Trades</div>{id_trades}</div>
        <div><div class="lbl">Crypto ID</div><span class="dot {cid_dot}"></span>{cid_status}</div>
        <div><div class="lbl">CID Cycle</div>#{cid_cycle}</div>
      </div>
    </div>
  </div>

  {positions_html}
  {trades_html}
  {screener_html}

  <!-- Performance Analytics -->
  <div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.2)">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="section-title" style="color:#ffcc00;margin-bottom:0">📊 Performance Analytics</div>
    </div>
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

  <!-- Last Scan -->
  <div style="margin-bottom:20px">
    <div class="tab-bar" style="margin-bottom:0;border-bottom:none">
      <div class="tab tab-stocks active" onclick="showScan('stocks',this)" style="border-bottom:2px solid #00aaff;color:#00aaff">📈 US Stocks Last Scan</div>
      <div class="tab tab-crypto" onclick="showScan('crypto',this)">🪙 Crypto Last Scan</div>
      <div class="tab" onclick="showScan('smallcap',this)" style="color:#ffcc00">📊 Small Cap Last Scan</div>
    </div>
    <div class="card" style="border-radius:0 12px 12px 12px;margin-top:0">
      <div id="scan-stocks" class="scan-panel active">{stocks_scan_html}</div>
      <div id="scan-crypto" class="scan-panel">{crypto_scan_html}</div>
      <div id="scan-smallcap" class="scan-panel">{smallcap_scan_html}</div>
    </div>
  </div>
  <script>
  function showScan(tab, el) {{
    document.querySelectorAll('.scan-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-bar .tab').forEach(t => {{ t.style.borderBottomColor='transparent'; t.style.color='#444'; }});
    document.getElementById('scan-' + tab).classList.add('active');
    el.style.borderBottomColor = tab==='stocks' ? '#00aaff' : '#00ff88';
    el.style.color = tab==='stocks' ? '#00aaff' : '#00ff88';
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

    portfolio  = f"${float(acc.get('portfolio_value', 0)):,.2f}" if acc else "—"
    sc_dot     = ("dot-green" if smallcap_state.running else "dot-gold") if not smallcap_state.shutoff else "dot-red"
    sc_status  = "Shut Off" if smallcap_state.shutoff else ("Running" if smallcap_state.running else "Idle")
    id_dot     = ("dot-green" if intraday_state.running else "dot-gold") if not intraday_state.shutoff else "dot-red"
    id_status  = "Shut Off" if intraday_state.shutoff else ("Running" if intraday_state.running else ("Window Closed" if not is_intraday_window() else "Idle"))
    cid_dot    = ("dot-green" if crypto_intraday_state.running else "dot-gold") if not crypto_intraday_state.shutoff else "dot-red"
    cid_status = "Shut Off" if crypto_intraday_state.shutoff else ("Running" if crypto_intraday_state.running else "Idle")

    if not USE_BINANCE:           binance_status = "⚠ Alpaca (25 coins only)"
    elif BINANCE_USE_TESTNET:     binance_status = f"🧪 Binance TESTNET ({len(CRYPTO_WATCHLIST)} coins)"
    else:                          binance_status = f"✅ Binance LIVE ({len(CRYPTO_WATCHLIST)} coins)"

    # Performance
    all_t        = perf["all_trades"]
    wins_count   = sum(1 for t in all_t if t.get("pnl", 0) > 0)
    losses_count = sum(1 for t in all_t if t.get("pnl", 0) <= 0)
    total_trades = len(all_t)
    win_rate     = int(wins_count / total_trades * 100) if total_trades else 0
    trades_wr_color = "green" if win_rate >= 55 else ("orange" if win_rate >= 45 else "red")
    max_dd    = round(perf["max_drawdown"], 1)
    dd_color  = "green" if max_dd < 5 else ("orange" if max_dd < 10 else "red")
    peak_pv   = f"{perf['peak_portfolio']:,.0f}"
    pf        = calc_profit_factor()
    profit_factor = f"{pf:.2f}" if pf != float("inf") else "∞"
    pf_color  = "green" if pf >= 1.5 else ("orange" if pf >= 1.0 else "red")
    sharpe_val   = calc_sharpe()
    sharpe       = f"{sharpe_val:.2f}" if sharpe_val else "—"
    sharpe_color = "green" if (sharpe_val and sharpe_val >= 1.0) else ("orange" if (sharpe_val and sharpe_val >= 0.5) else "#888")
    loss_streak  = global_risk["loss_streak"]
    streak_color = "red" if loss_streak >= LOSS_STREAK_LIMIT else ("orange" if loss_streak >= 2 else "green")
    pause_until  = global_risk.get("paused_until")
    pause_status = pause_until.strftime("%H:%M") if pause_until and datetime.now() < pause_until else "None"
    vix_val_now  = global_risk.get("vix_level")
    vix_level    = f"{vix_val_now:.1f}" if vix_val_now else "—"
    vix_color    = "red" if (vix_val_now and vix_val_now >= VIX_EXTREME) else ("orange" if (vix_val_now and vix_val_now >= VIX_HIGH_THRESHOLD) else "green")
    size_mult    = round(vol_adjusted_size(1.0), 2)
    global_pos   = all_positions_count()

    # Circuit breaker banner
    if circuit_breaker["active"]:
        circuit_banner = (
            f'<div style="margin-bottom:16px;padding:16px 20px;border-radius:12px;'
            f'background:rgba(255,68,102,0.15);border:2px solid #ff4466;display:flex;align-items:center;gap:16px">'
            f'<div style="font-size:28px">🚨</div>'
            f'<div><div style="font-size:16px;font-weight:700;color:#ff4466">CIRCUIT BREAKER ACTIVE — ALL NEW BUYS PAUSED</div>'
            f'<div style="font-size:12px;color:#888;margin-top:4px">Reason: {circuit_breaker["reason"]} · Triggered: {circuit_breaker["triggered_at"]}</div>'
            f'<div style="font-size:11px;color:#555;margin-top:2px">Existing positions still managed. Resets at next market open.</div>'
            f'</div></div>'
        )
    else:
        circuit_banner = ""

    # Regime data
    regime     = market_regime["mode"]
    c_regime   = crypto_regime["mode"]
    vix_str    = f"{market_regime['vix']:.1f}" if market_regime["vix"] else "N/A"
    spy_str    = f"${market_regime['spy_price']:.2f}" if market_regime["spy_price"] else "N/A"
    spy_ma_str = f"${market_regime['spy_ma20']:.2f}" if market_regime["spy_ma20"] else "N/A"
    btc_str    = f"${crypto_regime['btc_price']:.0f}" if crypto_regime["btc_price"] else "N/A"
    btc_ma_str = f"${crypto_regime['btc_ma20']:.0f}" if crypto_regime["btc_ma20"] else "N/A"
    btc_chg_str    = f"{crypto_regime['btc_change']:+.1f}%" if crypto_regime["btc_change"] is not None else "N/A"
    btc_chg_color  = "red" if crypto_regime["btc_change"] and crypto_regime["btc_change"] < -BTC_CRASH_PCT else "#e0e0e0"
    exposure       = total_exposure(state)
    crypto_exposure = total_exposure(crypto_state)
    exposure_str        = f"{exposure:.0f}"
    crypto_exposure_str = f"{crypto_exposure:.0f}"
    def pnl_str(v):   return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
    def pnl_color(v): return "green" if v >= 0 else "red"
    def dot_for(st):  return "dot-red" if st.shutoff else ("dot-green" if st.running else "dot-gold")
    def status_for(st): return "Shut Off" if st.shutoff else ("Running" if st.running else "Idle")

    # Kill switch banner
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

    # Positions table — clickable rows expand to show signal breakdown
    all_pos = (
        [(sym, pos, "blue",  "Stock")   for sym, pos in state.positions.items()] +
        [(sym, pos, "green", "Crypto")  for sym, pos in crypto_state.positions.items()] +
        [(sym, pos, "gold",  "SmCap")   for sym, pos in smallcap_state.positions.items()] +
        [(sym, pos, "blue",  "ID")      for sym, pos in intraday_state.positions.items()] +
        [(sym, pos, "green", "CrypID")  for sym, pos in crypto_intraday_state.positions.items()]
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
            entry    = pos["entry_price"]
            pnl      = (live - entry) * pos["qty"]
            pnl_pct  = ((live - entry) / entry) * 100
            pnl_c    = "green" if pnl >= 0 else "red"
            sign     = "+" if pnl >= 0 else ""
            entry_ts = pos.get("entry_ts", "")
            try:
                dt = datetime.fromisoformat(entry_ts)
                entry_dt = dt.strftime("%d %b %H:%M")
            except:
                entry_dt = pos.get("entry_date", "—")
            cat_colors = {"Stock":"#00aaff","Crypto":"#00ff88","SmCap":"#ffcc00","ID":"#aa88ff","CrypID":"#00ff88"}
            cat_color  = cat_colors.get(category, "#555")
            row_id     = f"pos-detail-{idx}"

            # ── Build signal breakdown panel ──
            bd = pos.get("entry_breakdown", "")
            score = pos.get("signal_score", "—")
            stop_pct  = round(((pos["stop_price"] - entry) / entry) * 100, 1)
            tp_price  = pos.get("take_profit_price", entry * 1.10)
            tp_pct    = round(((tp_price - entry) / entry) * 100, 1)
            days_held = pos.get("days_held", 0)

            if bd:
                # Parse the text breakdown into nice HTML
                lines = [l.strip() for l in bd.split("\n") if l.strip() and "─" not in l]
                bd_rows = ""
                for line in lines:
                    if ":" in line:
                        parts = line.split(":", 1)
                        label = parts[0].strip()
                        value = parts[1].strip() if len(parts) > 1 else ""
                        color_val = "#00ff88" if "✅" in value else ("#ff4466" if "🔴" in value or "❌" in value else ("#ffcc00" if "⚠" in value else "#aaa"))
                        bd_rows += f'<tr><td style="color:#555;font-size:11px;padding:3px 8px;white-space:nowrap">{label}</td><td style="color:{color_val};font-size:11px;padding:3px 8px">{value}</td></tr>'
                breakdown_html = f'<table style="width:100%;border-collapse:collapse">{bd_rows}</table>'
            else:
                breakdown_html = f'<div style="color:#555;font-size:12px">Score: {score}/10 · No detailed breakdown available</div>'

            detail_panel = f'''
            <tr id="{row_id}" style="display:none">
              <td colspan="7" style="padding:0">
                <div style="background:#0a0f0a;border:1px solid rgba(0,255,136,0.15);border-radius:8px;margin:4px 0 8px 0;padding:14px 16px">
                  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
                    <div style="font-size:13px;font-weight:700;color:#00ff88">📊 Why we bought {sym}</div>
                    <div style="font-size:11px;color:#555">{entry_dt} · Score: <b style="color:#ffcc00">{score}/10</b></div>
                  </div>
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                    <div>
                      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Signal Metrics</div>
                      {breakdown_html}
                    </div>
                    <div>
                      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Position Details</div>
                      <table style="width:100%;border-collapse:collapse">
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">Entry</td><td style="color:#e0e0e0;font-size:11px;padding:3px 8px;font-family:monospace">${entry:.4f}</td></tr>
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">Live</td><td style="color:#00aaff;font-size:11px;padding:3px 8px;font-family:monospace">${live:.4f}</td></tr>
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">Stop</td><td style="color:#ff4466;font-size:11px;padding:3px 8px;font-family:monospace">${pos["stop_price"]:.4f} ({stop_pct:+.1f}%)</td></tr>
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">Target</td><td style="color:#00ff88;font-size:11px;padding:3px 8px;font-family:monospace">${tp_price:.4f} ({tp_pct:+.1f}%)</td></tr>
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">Qty</td><td style="color:#e0e0e0;font-size:11px;padding:3px 8px;font-family:monospace">{pos["qty"]}</td></tr>
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">Days held</td><td style="color:#e0e0e0;font-size:11px;padding:3px 8px">{days_held}</td></tr>
                        <tr><td style="color:#555;font-size:11px;padding:3px 8px">P&L</td><td style="color:{'#00ff88' if pnl>=0 else '#ff4466'};font-size:11px;padding:3px 8px;font-weight:700">{sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)</td></tr>
                      </table>
                    </div>
                  </div>
                </div>
              </td>
            </tr>'''

            rows += (
                f'<tr onclick="togglePos(\'{row_id}\')" style="cursor:pointer" '
                f'onmouseover="this.style.background=\'rgba(255,255,255,0.03)\'" '
                f'onmouseout="this.style.background=\'transparent\'">'
                f'<td style="font-weight:700" class="{color}">▶ {sym}</td>'
                f'<td><span style="font-size:10px;color:{cat_color};font-weight:700">{category}</span></td>'
                f'<td style="color:#555;font-size:11px">{entry_dt}</td>'
                f'<td style="font-family:monospace">${entry:.4f}</td>'
                f'<td style="font-family:monospace;color:#00aaff">${live:.4f}</td>'
                f'<td class="red" style="font-family:monospace">${pos["stop_price"]:.4f}</td>'
                f'<td class="{pnl_c}" style="font-weight:700;font-family:monospace">{sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)</td>'
                f'</tr>'
                f'{detail_panel}'
            )

        positions_html = (
            f'<div class="card" style="margin-bottom:20px">'
            f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">'
            f'<div class="section-title" style="margin:0">Open Positions ({len(all_pos)})</div>'
            f'<div style="font-size:11px;color:#555">Tap any row to see why we bought it</div>'
            f'</div>'
            f'<div class="table-wrap"><table><thead><tr>'
            f'<th>Symbol</th><th>Type</th><th>Entry Time</th><th>Entry $</th><th>Live $</th><th>Stop</th><th>P&L</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></div>'
            f'<script>'
            f'function togglePos(id) {{'
            f'  var el = document.getElementById(id);'
            f'  var row = el.previousElementSibling;'
            f'  if (el.style.display === "none") {{'
            f'    el.style.display = "table-row";'
            f'    row.querySelector("td:first-child").textContent = row.querySelector("td:first-child").textContent.replace("▶","▼");'
            f'  }} else {{'
            f'    el.style.display = "none";'
            f'    row.querySelector("td:first-child").textContent = row.querySelector("td:first-child").textContent.replace("▼","▶");'
            f'  }}'
            f'}}'
            f'</script>'
        )
    else:
        positions_html = ""

    # Recent trades
    completed = (
        [dict(t, market="Stock")    for t in state.trades                 if t["side"]=="SELL" and t.get("pnl") is not None] +
        [dict(t, market="SmallCap") for t in smallcap_state.trades        if t["side"]=="SELL" and t.get("pnl") is not None] +
        [dict(t, market="Intraday") for t in intraday_state.trades        if t["side"]=="SELL" and t.get("pnl") is not None] +
        [dict(t, market="Crypto")   for t in crypto_state.trades          if t["side"]=="SELL" and t.get("pnl") is not None] +
        [dict(t, market="CryptoID") for t in crypto_intraday_state.trades if t["side"]=="SELL" and t.get("pnl") is not None]
    )
    completed.sort(key=lambda t: t["time"], reverse=True)
    if completed:
        wins      = sum(1 for t in completed if t["pnl"] > 0)
        losses    = sum(1 for t in completed if t["pnl"] <= 0)
        total_pnl = sum(t["pnl"] for t in completed)
        tr_wr     = int(wins / len(completed) * 100) if completed else 0
        def hold_str(h):
            if h is None: return "—"
            if h < 1: return f"{int(h*60)}m"
            if h < 24: return f"{h:.1f}h"
            return f"{h/24:.1f}d"
        rows = ""
        for t in completed[:10]:
            pc    = "green" if t["pnl"] >= 0 else "red"
            sign  = "+" if t["pnl"] >= 0 else ""
            mc    = "blue" if t["market"]=="Stock" else "green"
            rows += (f'<tr><td>{"✅" if t["pnl"]>0 else "❌"}</td>'
                     f'<td class="{mc}" style="font-weight:700">{t["symbol"]}</td>'
                     f'<td style="color:#555;font-size:11px">{t["market"]}</td>'
                     f'<td style="color:#555">{t["time"]}</td>'
                     f'<td class="{pc}" style="font-weight:700">{sign}${t["pnl"]:.2f}</td>'
                     f'<td style="color:#555">{hold_str(t.get("hold_hours"))}</td>'
                     f'<td style="color:#555;font-size:11px">{t.get("reason","—")}</td></tr>')
        summary = (f'<div style="display:flex;gap:20px;margin-bottom:14px;font-size:12px;flex-wrap:wrap">'
                   f'<span class="green">✅ {wins} wins</span><span class="red">❌ {losses} losses</span>'
                   f'<span style="color:#ffcc00">Win rate: {tr_wr}%</span>'
                   f'<span>Total P&L: <b class="{"green" if total_pnl>=0 else "red"}">{chr(43) if total_pnl>=0 else ""}${total_pnl:.2f}</b></span>'
                   f'</div>')
        trades_html = (f'<div class="card" style="margin-bottom:20px"><div class="section-title">Last {min(10,len(completed))} Trades</div>'
                       f'{summary}<div class="table-wrap"><table><thead><tr><th></th><th>Symbol</th><th>Type</th><th>Time</th>'
                       f'<th>P&L</th><th>Hold</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table></div></div>')
    else:
        trades_html = '<div class="card" style="margin-bottom:20px"><div class="empty">No completed trades yet</div></div>'

    # Current BUY signals screener — only show actual trades (score >= threshold AND ema crossed)
    all_cands = (
        [dict(c, market="Stock")  for c in state.candidates
         if c["signal"]=="BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="Crypto") for c in crypto_state.candidates
         if c["signal"]=="BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE]
    )
    if all_cands:
        rows = ""
        for c in all_cands:
            mc  = "blue" if c["market"]=="Stock" else "green"
            cc  = "green" if c["change"] >= 0 else "red"
            rsi = c.get("rsi")
            if rsi:
                if 50 <= rsi <= 65:  rsi_color = "#00ff88"; rsi_label = f"{rsi:.1f} ✅"
                elif rsi > 75:       rsi_color = "#ff4466"; rsi_label = f"{rsi:.1f} 🔴"
                elif rsi > 65:       rsi_color = "#ffcc00"; rsi_label = f"{rsi:.1f} ⚠"
                else:                rsi_color = "#555";    rsi_label = f"{rsi:.1f}"
            else:
                rsi_color = "#555"; rsi_label = "—"
            vr = c.get("vol_ratio", 0)
            if vr >= 2.0:   vol_color = "#00ff88"; vol_label = f"{vr:.2f}x 🔥"
            elif vr >= 1.5: vol_color = "#00aaff"; vol_label = f"{vr:.2f}x ✅"
            else:            vol_color = "#555";    vol_label = f"{vr:.2f}x" if vr else "—"
            sc = c.get("score", 0)
            chg_sign = "+" if c["change"] >= 0 else ""
            rows += (f'<tr><td class="{mc}" style="font-weight:700">{c["symbol"]}</td>'
                     f'<td>{c["market"]}</td><td>${c["price"]:.4f}</td>'
                     f'<td class="{cc}">{chg_sign}{c["change"]:.2f}%</td>'
                     f'<td><span class="sig-buy">🟢 BUY {sc:.1f}</span></td>'
                     f'<td style="color:{rsi_color};font-weight:700">{rsi_label}</td>'
                     f'<td style="color:{vol_color};font-weight:700">{vol_label}</td></tr>')
        screener_html = (f'<div class="card" style="margin-bottom:20px"><div class="section-title">Current BUY Signals ({len(all_cands)})</div>'
                         f'<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Type</th><th>Price</th>'
                         f'<th>Chg%</th><th>Signal</th><th>RSI</th><th>Vol</th></tr></thead>'
                         f'<tbody>{rows}</tbody></table></div></div>')
    else:
        screener_html = '<div class="card" style="margin-bottom:20px"><div class="empty">No BUY signals yet — waiting for first cycle</div></div>'

    # Full scan tables
    def build_scan_table(candidates, color):
        if not candidates: return '<div class="empty">No scan data yet</div>'
        scored = []
        for c in candidates:
            sc = score_signal(c["symbol"], c["price"], c["change"],
                              c.get("rsi"), c.get("vol_ratio"),
                              c.get("closes", [c["price"]]*22))
            scored.append((sc, c))

        # Sort purely by score descending — best opportunities always at top
        bear_syms    = set(BEAR_TICKERS)
        bear_items   = [(sc,c) for sc,c in scored if c["symbol"] in bear_syms]
        normal_items = [(sc,c) for sc,c in scored if c["symbol"] not in bear_syms]
        bear_items.sort(key=lambda x: -x[0])
        normal_items.sort(key=lambda x: -x[0])  # score descending only
        scored = bear_items + normal_items

        rows = ""
        for sc, c in scored:
            # ── Smart signal badge ──────────────────────────────
            ema_gap     = c.get("ema_gap")
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

            # ── Change colour ───────────────────────────────────
            cc = "green" if c["change"] >= 0 else "red"

            # ── RSI colour coding ───────────────────────────────
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

            # ── Volume colour coding ────────────────────────────
            vr = c.get("vol_ratio", 0)
            if vr >= 2.0:    vol_color = "#00ff88"; vol_label = f"{vr:.2f}x 🔥"
            elif vr >= 1.5:  vol_color = "#00aaff"; vol_label = f"{vr:.2f}x ✅"
            elif vr >= 1.2:  vol_color = "#ffcc00"; vol_label = f"{vr:.2f}x ⚠"
            elif vr > 0:     vol_color = "#555";    vol_label = f"{vr:.2f}x"
            else:             vol_color = "#555";    vol_label = "—"

            # ── Score bar ───────────────────────────────────────
            threshold = MIN_SIGNAL_SCORE
            pct = min(100, int((sc / 11) * 100))
            if sc >= threshold:         bar_color = "#00ff88"; prox = f"✅ TRADE {sc:.1f}"
            elif sc >= threshold - 1:   bar_color = "#ffcc00"; prox = f"🔥 {sc:.1f}/{threshold}"
            elif sc >= threshold - 2:   bar_color = "#ff8800"; prox = f"⚡ {sc:.1f}/{threshold}"
            else:                        bar_color = "#333";    prox = f"{sc:.1f}/{threshold}"
            score_bar = (f'<div style="display:flex;align-items:center;gap:6px">'
                         f'<div style="width:50px;height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden">'
                         f'<div style="width:{pct}%;height:100%;background:{bar_color};border-radius:3px"></div></div>'
                         f'<span style="font-size:11px;color:{bar_color};font-weight:700">{prox}</span></div>')

            # ── EMA cross ───────────────────────────────────────
            if ema_gap is not None:
                if ema_gap > 0:        ema_col = "#00ff88"; ema_str = f"+{ema_gap:.2f}% ✅"
                elif ema_gap > -0.5:   ema_col = "#ffcc00"; ema_str = f"{ema_gap:.2f}% 🔥"
                elif ema_gap > -1.5:   ema_col = "#ff8800"; ema_str = f"{ema_gap:.2f}% ⚡"
                else:                   ema_col = "#555";    ema_str = f"{ema_gap:.2f}%"
            else:
                ema_col = "#555"; ema_str = "—"

            bear_badge = ('<span style="font-size:9px;background:rgba(255,136,0,0.2);color:#ff8800;border:1px solid rgba(255,136,0,0.4);'
                          'border-radius:4px;padding:1px 5px;margin-left:4px;font-weight:700">BEAR</span>'
                          if c["symbol"] in bear_syms else "")
            row_bg = "background:rgba(255,136,0,0.04);" if c["symbol"] in bear_syms else ""
            chg_sign = "+" if c["change"] >= 0 else ""
            rows += (f'<tr style="{row_bg}"><td style="font-weight:700" class="{color}">{c["symbol"]}{bear_badge}</td>'
                     f'<td>${c["price"]:.4f}</td>'
                     f'<td class="{cc}">{chg_sign}{c["change"]:.2f}%</td>'
                     f'<td>{sig_html}</td>'
                     f'<td>{score_bar}</td>'
                     f'<td style="color:{ema_col};font-size:11px;font-weight:700">{ema_str}</td>'
                     f'<td style="color:{rsi_color};font-size:11px;font-weight:700">{rsi_label}</td>'
                     f'<td style="color:{vol_color};font-size:11px;font-weight:700">{vol_label}</td></tr>')

        buys  = sum(1 for sc,c in scored if sc >= MIN_SIGNAL_SCORE and c.get("ema_gap",  -99) > 0)
        watch = sum(1 for sc,c in scored if sc >= MIN_SIGNAL_SCORE and c.get("ema_gap", -99) <= 0)
        return (f'<div style="display:flex;gap:16px;margin-bottom:14px;font-size:12px;flex-wrap:wrap">'
                f'<span class="green" style="font-weight:700">🟢 {buys} BUY</span>'
                f'<span style="color:#00aaff;font-weight:700">👀 {watch} WATCH</span>'
                f'<span style="color:#444;margin-left:auto">{len(scored)} scanned</span></div>'
                f'<div style="overflow-x:auto"><table><thead><tr>'
                f'<th>Symbol</th><th>Price</th><th>Chg%</th><th>Signal</th>'
                f'<th>Score</th><th>EMA Cross</th><th>RSI</th><th>Vol</th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div>')

    stocks_scan_html  = build_scan_table(state.candidates, "blue")
    crypto_scan_html  = build_scan_table(crypto_state.candidates, "green")
    if smallcap_state.candidates:
        smallcap_scan_html = build_scan_table(smallcap_state.candidates, "gold")
    elif smallcap_pool.get("symbols"):
        pool_size = len(smallcap_pool["symbols"])
        last_refresh = smallcap_pool.get("last_refresh", "—")
        smallcap_scan_html = (
            f'<div style="padding:20px;text-align:center;color:#555">'
            f'<div style="font-size:14px;color:#ffcc00;margin-bottom:8px">📊 Pool ready — {pool_size} stocks loaded</div>'
            f'<div style="font-size:12px">Refreshed: {last_refresh}</div>'
            f'<div style="font-size:12px;margin-top:4px">Scan results will appear after next cycle</div>'
            f'<div style="font-size:11px;color:#444;margin-top:8px">Top stocks: {", ".join(smallcap_pool["symbols"][:8])}</div>'
            f'</div>'
        )
    else:
        smallcap_scan_html = '<div class="empty">Small cap pool refreshing — check back after first cycle (takes ~5 mins)</div>'

    # News section
    if not news_state["scan_complete"]:
        if cfg.NEWS_API_KEY:
            news_html = '<div class="empty" style="padding:20px">Waiting for 9:00 AM ET morning scan...</div>'
        else:
            news_html = '<div style="padding:12px;background:rgba(255,204,0,0.05);border:1px solid rgba(255,204,0,0.2);border-radius:8px;font-size:12px;color:#888">⚠ Add <b style="color:#ffcc00">NEWS_API_KEY</b> and <b style="color:#ffcc00">CLAUDE_API_KEY</b> to .env to enable news scanning</div>'
    else:
        skip_rows  = "".join(f'<tr><td style="font-weight:700;color:#ff4466">{sym}</td><td><span class="sig-sell">SKIP</span></td><td style="color:#888;font-size:12px">{d["reason"]}</td></tr>' for sym, d in news_state["skip_list"].items())
        boost_rows = "".join(f'<tr><td style="font-weight:700;color:#00ff88">{sym}</td><td><span class="sig-buy">POSITIVE</span></td><td style="color:#888;font-size:12px">{d["reason"]}</td></tr>' for sym, d in news_state["watch_list"].items())
        all_rows   = skip_rows + boost_rows
        news_html  = (f'<table><thead><tr><th>Symbol</th><th>Sentiment</th><th>Reason</th></tr></thead><tbody>{all_rows}</tbody></table>'
                      f'<div style="margin-top:10px;font-size:11px;color:#555">{len(news_state["skip_list"])} skipped · {len(news_state["watch_list"])} positive</div>'
                      if all_rows else '<div style="color:#555;font-size:13px;padding:8px 0">All clear — no negative news today.</div>')
    news_scan_time = f"Last scan: {news_state.get('last_scan_time', '')} ET" if news_state.get("last_scan_time") else "Scans at 9:00 AM ET daily"

    return DASHBOARD_HTML.format(
        now=datetime.now().strftime("%H:%M:%S"),
        circuit_banner=circuit_banner, kill_banner=kill_banner,
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
        exposure_str=exposure_str, crypto_exposure_str=crypto_exposure_str,
        win_rate=win_rate, trades_wr_color=trades_wr_color, wins=wins_count, losses=losses_count,
        max_dd=max_dd, dd_color=dd_color, peak_pv=peak_pv,
        profit_factor=profit_factor, pf_color=pf_color,
        sharpe=sharpe, sharpe_color=sharpe_color,
        loss_streak=loss_streak, streak_color=streak_color, streak_limit=LOSS_STREAK_LIMIT,
        pause_status=pause_status, vix_level=vix_level, vix_color=vix_color,
        size_mult=size_mult, global_pos=global_pos, max_global=MAX_TOTAL_POSITIONS,
        signal_threshold=MIN_SIGNAL_SCORE,
        regime=regime, regime_color="red" if regime=="BEAR" else "green",
        regime_bg="rgba(255,68,102,0.08)" if regime=="BEAR" else "rgba(0,255,136,0.05)",
        regime_border="rgba(255,68,102,0.25)" if regime=="BEAR" else "rgba(0,255,136,0.15)",
        regime_icon="🐻" if regime=="BEAR" else "🐂",
        spy_str=spy_str, spy_ma_str=spy_ma_str, vix_str=vix_str,
        vix_regime_color="red" if market_regime["vix"] and market_regime["vix"] > VIX_FEAR_THRESHOLD else "#e0e0e0",
        c_regime=c_regime, c_regime_color="red" if c_regime=="BEAR" else "green",
        c_regime_bg="rgba(255,68,102,0.08)" if c_regime=="BEAR" else "rgba(0,255,136,0.05)",
        c_regime_border="rgba(255,68,102,0.25)" if c_regime=="BEAR" else "rgba(0,255,136,0.15)",
        c_regime_icon="🐻" if c_regime=="BEAR" else "🐂",
        btc_str=btc_str, btc_ma_str=btc_ma_str, btc_chg_str=btc_chg_str, btc_chg_color=btc_chg_color,
        news_html=news_html, news_scan_time=news_scan_time,
        dash_token=DASH_TOKEN,
        stop_loss=STOP_LOSS_PCT, trailing_stop=TRAILING_STOP_PCT, take_profit=TAKE_PROFIT_PCT,
        max_hold_days=MAX_HOLD_DAYS, gap_down=GAP_DOWN_PCT,
        max_loss=MAX_DAILY_LOSS, max_trade=MAX_TRADE_VALUE,
        max_spend=MAX_DAILY_SPEND,
    )


# ── Analytics page ────────────────────────────────────────────
def build_analytics_page(search_sym=None, report_id=None, period="all"):
    period_days  = {"90": 90, "30": 30, "all": None}.get(period, None)
    period_label = {"90": "Last 90 Days", "30": "Last 30 Days", "all": "All Time"}.get(period, "All Time")
    leaders      = db_get_leaderboard(limit=20, period_days=period_days)
    medal        = ["🥇","🥈","🥉"]

    lb_rows = ""
    for i, row in enumerate(leaders):
        sym, trades, wins, losses, total_pnl, best, worst, avg_sc = row[:8]
        win_rate = int(wins/trades*100) if trades > 0 else 0
        pc = "#00cc66" if total_pnl >= 0 else "#cc2244"
        rank = medal[i] if i < 3 else f"#{i+1}"
        lb_rows += (f'<tr onclick="searchSym(\'{sym}\')" style="cursor:pointer">'
                    f'<td style="color:#888;font-weight:700">{rank}</td>'
                    f'<td style="color:#00aaff;font-weight:700">{sym}</td>'
                    f'<td>{trades}</td><td style="color:#00cc66">{wins}</td>'
                    f'<td style="color:#cc2244">{losses}</td>'
                    f'<td style="color:#888">{win_rate}%</td>'
                    f'<td style="color:{pc};font-weight:700">${total_pnl:+.2f}</td>'
                    f'<td style="color:#00cc66">${best:+.2f}</td>'
                    f'<td style="color:#cc2244">${worst:+.2f}</td>'
                    f'<td style="color:#ffcc00">{avg_sc:.1f}</td></tr>')
    if not lb_rows:
        lb_rows = '<tr><td colspan="10" style="text-align:center;color:#555;padding:20px">No trades yet — check back after first week</td></tr>'

    search_html = ""
    if search_sym:
        results = db_search_symbol(search_sym)
        stats   = results["stats"]
        trades  = results["trades"]
        if stats:
            sym2, total_t, wins2, losses2, total_pnl2, best2, worst2, avg_sc2, nm_count, last_t, first_t, _ = stats
            wr2  = int(wins2/total_t*100) if total_t > 0 else 0
            pc2  = "#00cc66" if total_pnl2 >= 0 else "#cc2244"
            search_html += (f'<div style="background:#0d1117;border:1px solid #1a3a5c;border-radius:12px;padding:20px;margin-bottom:20px">'
                            f'<div style="font-size:20px;font-weight:700;color:#00aaff;margin-bottom:12px">{sym2}</div>'
                            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
                            f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700;color:{pc2}">${total_pnl2:+.2f}</div><div style="font-size:10px;color:#555;text-transform:uppercase">Total P&L</div></div>'
                            f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700">{total_t}</div><div style="font-size:10px;color:#555;text-transform:uppercase">Trades</div></div>'
                            f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700;color:#00cc66">{wr2}%</div><div style="font-size:10px;color:#555;text-transform:uppercase">Win Rate</div></div>'
                            f'<div style="background:#111820;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:700;color:#ffcc00">{nm_count}</div><div style="font-size:10px;color:#555;text-transform:uppercase">Near Misses</div></div>'
                            f'</div></div>')
        else:
            search_html = f'<div style="color:#555;padding:20px;text-align:center">No data found for <b style="color:#00aaff">{search_sym}</b> yet</div>'

    reports     = db_get_reports(limit=30)
    report_rows = ""
    for r in reports:
        rid, rtype, rdate, subject = r
        icon     = "📊" if rtype=="daily" else "📈" if rtype=="weekly" else "☀️"
        type_col = "#00aaff" if rtype=="daily" else "#00cc66" if rtype=="weekly" else "#ffcc00"
        report_rows += (f'<tr onclick="loadReport({rid})" style="cursor:pointer">'
                        f'<td style="padding:8px;color:{type_col}">{icon} {rtype.title()}</td>'
                        f'<td style="padding:8px;color:#888">{rdate}</td>'
                        f'<td style="padding:8px;color:#e0e0e0">{subject or "—"}</td></tr>')
    if not report_rows:
        report_rows = '<tr><td colspan="3" style="padding:20px;text-align:center;color:#555">No reports archived yet</td></tr>'

    report_viewer = ""
    if report_id:
        report = db_get_report_by_id(int(report_id))
        if report:
            _, rtype, rdate, subject, body_html, body_text, _ = report
            report_viewer = (f'<div style="background:#0d1117;border:1px solid #1a3a5c;border-radius:12px;padding:20px;margin-bottom:20px">'
                             f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
                             f'<div style="font-weight:700;color:#e0e0e0">{subject}</div>'
                             f'<div style="color:#555;font-size:12px">{rdate}</div></div>'
                             f'<div style="border-top:1px solid #1a1a1a;padding-top:16px;font-size:13px;line-height:1.6;color:#ccc;white-space:pre-wrap">{body_text}</div>'
                             f'</div>')

    # Skip reason breakdown
    skip_reasons = db_get_skip_reason_breakdown()
    skip_reason_html = ""
    if skip_reasons:
        rows = "".join(f'<tr><td style="color:#ffcc00">{r[0]}</td><td>{r[1]}</td><td style="color:#00aaff">{r[2]:.1f}</td></tr>' for r in skip_reasons)
        skip_reason_html = (f'<div class="card" style="margin-bottom:20px">'
                            f'<div class="section-title">📋 Skip Reason Breakdown</div>'
                            f'<table><thead><tr><th>Reason</th><th>Count</th><th>Avg Score</th></tr></thead>'
                            f'<tbody>{rows}</tbody></table></div>')

    # Overall DB stats
    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(pnl), AVG(score) FROM trades WHERE side='SELL'")
        row = c.fetchone()
        total_trades_db = row[0] or 0
        total_pnl_db    = row[1] or 0
        avg_score_db    = row[2] or 0
        c.execute("SELECT COUNT(DISTINCT symbol) FROM trades")
        unique_syms = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM near_misses")
        total_misses = c.fetchone()[0] or 0
        conn.close()
    except:
        total_trades_db = total_pnl_db = avg_score_db = unique_syms = total_misses = 0

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
<div style="background:#0d1117;border-bottom:1px solid #1a1a1a;padding:16px 24px;display:flex;align-items:center;justify-content:space-between">
  <div style="display:flex;align-items:center;gap:12px">
    <a href="/" style="color:#555;text-decoration:none;font-size:13px">← Dashboard</a>
    <span style="color:#333">|</span>
    <span style="font-size:16px;font-weight:700;color:#00aaff">🧠 Trading Intelligence</span>
  </div>
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
      <div class="section-title" style="margin:0">🏆 Stock Leaderboard — {period_label}</div>
      <div>
        <button class="period-tab {'active' if period=='30' else ''}" onclick="setPeriod('30')">30 Days</button>
        <button class="period-tab {'active' if period=='90' else ''}" onclick="setPeriod('90')">90 Days</button>
        <button class="period-tab {'active' if period=='all' else ''}" onclick="setPeriod('all')">All Time</button>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table><thead><tr>
      <th>Rank</th><th>Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th>
      <th>Win Rate</th><th>Total P&L</th><th>Best</th><th>Worst</th><th>Avg Score</th>
    </tr></thead><tbody>{lb_rows}</tbody></table>
    </div>
  </div>

  {skip_reason_html}

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
        return True  # Auth disabled in paper mode — re-enable before going live

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK"); return

        if self.path == "/api":
            with _state_lock:
                data = json.dumps({
                    "stocks":    {"pnl": state.daily_pnl, "positions": len(state.positions), "trades": len(state.trades), "cycle": state.cycle_count},
                    "crypto":    {"pnl": crypto_state.daily_pnl, "positions": len(crypto_state.positions), "trades": len(crypto_state.trades), "cycle": crypto_state.cycle_count},
                    "portfolio": float(cfg.account_info.get("portfolio_value", 0)) if cfg.account_info else 0,
                    "kill_switch": kill_switch["active"],
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
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)
        submitted_pin = query_params.get("pin", [None])[0]
        base_path = parsed_path.path

        # PIN-protected kill switch paths
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
            kill_switch.update({"active": True, "reason": "Manual kill from dashboard", "activated_at": datetime.now().strftime("%H:%M:%S")})
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                st.shutoff = True
            log.warning("[KILL SWITCH] Manual kill activated from dashboard")
            self._json(json.dumps({"status": "killed"}))

        elif base_path == "/resume":
            kill_switch.update({"active": False, "reason": "", "activated_at": None})
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                st.shutoff = False
            log.info("[KILL SWITCH] Resumed from dashboard")
            self._json(json.dumps({"status": "resumed"}))

        elif base_path == "/close-all":
            log.warning("[KILL SWITCH] Close all positions requested from dashboard")
            for sym, pos in list(state.positions.items()):
                place_order(sym, "sell", pos["qty"], estimated_price=pos["entry_price"])
                if sym in exchange_stops:
                    cancel_stop_order_alpaca(exchange_stops.pop(sym))
            for sym, pos in list(crypto_state.positions.items()):
                place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
            state.positions.clear(); crypto_state.positions.clear()
            kill_switch.update({"active": True, "reason": "Close all — liquidated from dashboard", "activated_at": datetime.now().strftime("%H:%M:%S")})
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
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
        pass  # suppress default access logs


def start_dashboard():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    log.info(f"Dashboard running on port {PORT}")
    server.serve_forever()
