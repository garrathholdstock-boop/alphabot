"""
AlphaBot — Automated Day Trading Bot
Trades US stocks + crypto via Alpaca API
Built-in web dashboard served on Railway
"""

import os
import time
import logging
import smtplib
import threading
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("AlphaBot")

# ── Config ────────────────────────────────────────────────────
ALPACA_KEY     = os.environ.get("ALPACA_KEY",    "YOUR_API_KEY")
ALPACA_SECRET  = os.environ.get("ALPACA_SECRET", "YOUR_SECRET_KEY")
IS_LIVE        = os.environ.get("IS_LIVE",       "false").lower() == "true"
GMAIL_USER     = os.environ.get("GMAIL_USER",    "garrathholdstock@gmail.com")
GMAIL_PASS     = os.environ.get("GMAIL_PASS",    "YOUR_GMAIL_APP_PASSWORD")
EMAIL_TO       = "garrathholdstock@gmail.com"
PORT           = int(os.environ.get("PORT", 8080))

ALPACA_BASE    = "https://api.alpaca.markets" if IS_LIVE else "https://paper-api.alpaca.markets"
DATA_BASE      = "https://data.alpaca.markets"

# ── Safety settings ───────────────────────────────────────────
MAX_DAILY_LOSS      = 50.0
STOP_LOSS_PCT       = 2.0
MAX_POSITIONS       = 3
MAX_TRADE_VALUE     = 500.0
MAX_DAILY_SPEND     = 5000.0
DAILY_PROFIT_TARGET = 2000.0
CYCLE_SECONDS       = 60

# ── Watchlists ────────────────────────────────────────────────
US_WATCHLIST = [
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","NFLX","ORCL","ADBE",
    "AMD","INTC","QCOM","AVGO","MU","AMAT","LRCX","KLAC","TXN","MRVL",
    "COIN","HOOD","SQ","PYPL","SOFI","AFRM","UPST","NU","MARA","RIOT",
    "RIVN","LCID","NIO","XPEV","LI","BLNK","CHPT","PLUG","FCEL","BE",
    "PLTR","AI","PATH","SNOW","DDOG","NET","CRWD","ZS","OKTA","MDB",
    "MRNA","BNTX","NVAX","HIMS","TDOC","ACCD","SDGR","RXRX","BEAM","SGEN",
    "SHOP","ETSY","ABNB","UBER","LYFT","DASH","RBLX","SNAP","PINS","YELP",
    "XOM","CVX","OXY","SLB","HAL","MPC","VLO","PSX","DVN","FANG",
    "SPY","QQQ","IWM","ARKK","SOXL","TQQQ","SQQQ","GLD","SLV","UVXY",
    "GME","AMC","SPCE","WKHS","NKLA","OPEN","DKNG","CLOV","WISH","LCID",
]

CRYPTO_WATCHLIST = [
    "BTC/USD","ETH/USD","SOL/USD","AVAX/USD","DOGE/USD","SHIB/USD",
    "LTC/USD","BCH/USD","LINK/USD","DOT/USD","UNI/USD","AAVE/USD",
    "XTZ/USD","BAT/USD","CRV/USD","GRT/USD","MKR/USD","MATIC/USD",
    "ALGO/USD","XLM/USD","SUSHI/USD","YFI/USD","ETH/BTC",
]

# ── Shared state (read by dashboard, written by bot) ─────────
class BotState:
    def __init__(self, label):
        self.label = label
        self.reset()

    def reset(self):
        self.daily_pnl       = 0.0
        self.daily_spend     = 0.0
        self.positions       = {}
        self.trades          = []
        self.shutoff         = False
        self.last_reset_day  = datetime.now().date()
        self.last_cycle      = None
        self.cycle_count     = 0
        self.running         = False
        self.candidates      = []

    def check_reset(self):
        today = datetime.now().date()
        if today != self.last_reset_day:
            log.info(f"[{self.label}] Daily reset")
            self.reset()

state        = BotState("STOCKS")
crypto_state = BotState("CRYPTO")
account_info = {}

# ── Alpaca API ────────────────────────────────────────────────
HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

def alpaca_get(path, base=None):
    try:
        r = requests.get((base or ALPACA_BASE) + path, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}")
        return None

def alpaca_post(path, body):
    try:
        r = requests.post(ALPACA_BASE + path, json=body, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"POST {path}: {e}")
        return None

# ── Market data ───────────────────────────────────────────────
def fetch_bars(symbol, crypto=False):
    end   = datetime.utcnow()
    start = end - timedelta(days=60)
    s, e  = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    try:
        if crypto:
            enc = requests.utils.quote(symbol, safe="")
            r = requests.get(f"{DATA_BASE}/v1beta3/crypto/us/bars?symbols={enc}&timeframe=1Day&start={s}&end={e}&limit=30", headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", {}).get(symbol, [])
        else:
            r = requests.get(f"{DATA_BASE}/v2/stocks/{symbol}/bars?timeframe=1Day&start={s}&end={e}&limit=30&feed=sip&adjustment=raw", headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", [])
        return bars if bars and len(bars) >= 15 else None
    except: return None

def fetch_latest_price(symbol, crypto=False):
    try:
        if crypto:
            enc = requests.utils.quote(symbol, safe="")
            r = requests.get(f"{DATA_BASE}/v1beta3/crypto/us/latest/bars?symbols={enc}", headers=HEADERS, timeout=10)
            if not r.ok: return None
            return r.json().get("bars", {}).get(symbol, {}).get("c")
        else:
            r = requests.get(f"{DATA_BASE}/v2/stocks/{symbol}/snapshot?feed=sip", headers=HEADERS, timeout=10)
            if not r.ok: return None
            d = r.json()
            return d.get("latestTrade", {}).get("p") or d.get("latestQuote", {}).get("ap")
    except: return None

# ── Indicators ────────────────────────────────────────────────
def sma(prices, period):
    if len(prices) < period: return None
    return sum(prices[-period:]) / period

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    ch = [prices[i] - prices[i-1] for i in range(1, len(prices))][-period:]
    ag = sum(c for c in ch if c > 0) / period
    al = sum(-c for c in ch if c < 0) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def get_signal(closes):
    s9, s21 = sma(closes, 9), sma(closes, 21)
    p9, p21 = sma(closes[:-1], 9), sma(closes[:-1], 21)
    rsi = calc_rsi(closes)
    if None in (s9, s21, p9, p21, rsi): return "HOLD", s9, s21, rsi
    if p9 <= p21 and s9 > s21 and rsi < 70: return "BUY", s9, s21, rsi
    if (p9 >= p21 and s9 < s21) or rsi > 70: return "SELL", s9, s21, rsi
    return "HOLD", s9, s21, rsi

def is_market_open():
    et   = datetime.now(ZoneInfo("America/New_York"))
    mins = et.hour * 60 + et.minute
    return et.weekday() < 5 and 570 <= mins < 960

# ── Orders ────────────────────────────────────────────────────
def place_order(symbol, side, qty, crypto=False):
    result = alpaca_post("/v2/orders", {
        "symbol": symbol, "qty": str(qty), "side": side,
        "type": "market", "time_in_force": "gtc" if crypto else "day",
    })
    if result: log.info(f"ORDER {side.upper()} {qty} {symbol}")
    return result

# ── Bot cycle ─────────────────────────────────────────────────
def check_stop_losses(st, crypto=False):
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym, crypto=crypto)
        if not live: continue
        if live <= pos["stop_price"]:
            pnl = (live - pos["entry_price"]) * pos["qty"]
            log.warning(f"[{st.label}] STOP-LOSS {sym} @ ${live:.4f} P&L:${pnl:+.2f}")
            place_order(sym, "sell", pos["qty"], crypto=crypto)
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": "Stop-Loss",
                "time": datetime.now().strftime("%H:%M:%S")})
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"[{st.label}] Daily loss limit hit!")
                st.shutoff = True

def run_cycle(watchlist, st, crypto=False):
    st.check_reset()
    if st.shutoff: return
    if not crypto and not is_market_open():
        et = datetime.now(ZoneInfo("America/New_York"))
        log.info(f"[{st.label}] Market closed ({et.strftime('%H:%M ET')})")
        return

    st.running     = True
    st.last_cycle  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[{st.label}] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")

    check_stop_losses(st, crypto=crypto)
    if st.shutoff: return

    # Scan
    results = []
    for sym in watchlist:
        bars = fetch_bars(sym, crypto=crypto)
        if not bars: continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        prev    = closes[-2] if len(closes) > 1 else price
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, s9, s21, rsi = get_signal(closes)
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": s9, "sma21": s21, "rsi": rsi, "vol_ratio": vol_ratio})

    results.sort(key=lambda x: {"BUY": 0, "HOLD": 1, "SELL": 2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[{st.label}] {buys} BUY / {len(results)} scanned")

    # Open BUY positions
    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= MAX_POSITIONS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if st.daily_spend >= MAX_DAILY_SPEND: break
        qty = max(0.0001, round(MAX_TRADE_VALUE / s["price"], 6)) if crypto else max(1, int(MAX_TRADE_VALUE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - STOP_LOSS_PCT / 100)
        log.info(f"[{st.label}] BUY {s['symbol']} @ ${s['price']:.4f} x{qty} stop:${stop_price:.4f} RSI:{s['rsi']:.1f}")
        order = place_order(s["symbol"], "buy", qty, crypto=crypto)
        if order:
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": s["price"], "stop_price": stop_price}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": s["price"], "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S")})
            pos_count += 1

    # Close SELL positions
    for s in results:
        if s["signal"] != "SELL": continue
        if s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        pnl = (s["price"] - pos["entry_price"]) * pos["qty"]
        log.info(f"[{st.label}] SELL {s['symbol']} @ ${s['price']:.4f} P&L:${pnl:+.2f}")
        order = place_order(s["symbol"], "sell", pos["qty"], crypto=crypto)
        if order:
            del st.positions[s["symbol"]]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
                "price": s["price"], "pnl": pnl, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S")})
            st.trades = st.trades[:200]
            if st.daily_pnl >= DAILY_PROFIT_TARGET:
                log.info(f"[{st.label}] Profit target hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"[{st.label}] Loss limit hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break

    st.running = False

# ── Email ─────────────────────────────────────────────────────
def send_daily_summary():
    def section(st):
        sells = [t for t in st.trades if t["side"] == "SELL" and t.get("pnl") is not None]
        wins  = [t for t in sells if t["pnl"] > 0]
        def fmt_trade(t):
            pnl_str = ""
            if t.get("pnl") is not None:
                sign = "+" if t["pnl"] >= 0 else ""
                pnl_str = f"  P&L: {sign}${t['pnl']:.2f}"
            return f"  {t['time']}  {t['side']:4}  {t['symbol']:10}  ${t['price']:.4f}{pnl_str}"
        lines = "\n".join(fmt_trade(t) for t in st.trades[:20]) or "  No trades today"
        return (f"{st.label}\n{'─'*40}\n"
                f"Daily P&L:   ${st.daily_pnl:+.2f}\n"
                f"Trades:      {len(sells)}\n"
                f"Win rate:    {int(len(wins)/len(sells)*100) if sells else 0}%\n"
                f"Positions:   {len(st.positions)}\n\n"
                f"Trade log:\n{lines}\n")

    body = (f"AlphaBot Daily Summary\n{'='*40}\n"
            f"Date: {datetime.now().strftime('%A, %d %B %Y')}\n"
            f"Mode: {'LIVE' if IS_LIVE else 'Paper'} Trading\n"
            f"Portfolio: ${float(account_info.get('portfolio_value',0)):,.2f}\n\n"
            f"{section(state)}\n{section(crypto_state)}\n"
            f"{'='*40}\nSent by AlphaBot on Railway")
    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = f"AlphaBot Daily Summary — {datetime.now().strftime('%d %b %Y')}"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Summary emailed to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email failed: {e}")

# ── Web dashboard ─────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>AlphaBot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #090b0e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; font-size: 14px; }
  .header { background: #0d1117; border-bottom: 1px solid #1e2a1e; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  .logo { display: flex; align-items: center; gap: 10px; }
  .logo-icon { width: 32px; height: 32px; background: linear-gradient(135deg,#00ff88,#00aaff); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; }
  .logo-text { font-weight: 700; font-size: 16px; }
  .logo-sub { font-size: 10px; color: #444; letter-spacing: 1.5px; text-transform: uppercase; }
  .badge { padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 700; }
  .badge-paper { background: rgba(255,204,0,0.1); color: #ffcc00; border: 1px solid rgba(255,204,0,0.3); }
  .badge-live  { background: rgba(255,68,102,0.1); color: #ff4466; border: 1px solid rgba(255,68,102,0.3); }
  .refresh { font-size: 11px; color: #444; }
  .container { padding: 24px; max-width: 1100px; margin: 0 auto; }
  .grid4 { display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; margin-bottom: 20px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }
  .card { background: rgba(255,255,255,0.025); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 18px 20px; }
  .card-green { border-color: rgba(0,255,136,0.15); }
  .card-blue  { border-color: rgba(0,170,255,0.15); }
  .lbl { font-size: 10px; letter-spacing: 2px; color: #555; text-transform: uppercase; margin-bottom: 4px; }
  .big { font-size: 22px; font-weight: 700; font-family: monospace; }
  .green { color: #00ff88; }
  .blue  { color: #00aaff; }
  .red   { color: #ff4466; }
  .gold  { color: #ffcc00; }
  .grey  { color: #555; }
  .section-title { font-size: 15px; font-weight: 700; margin-bottom: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { font-size: 10px; color: #444; letter-spacing: 1.5px; text-transform: uppercase; padding: 10px 12px; text-align: left; font-weight: 600; }
  td { padding: 9px 12px; border-top: 1px solid rgba(255,255,255,0.04); font-family: monospace; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .sig-buy  { background: rgba(0,255,136,0.1); color: #00ff88; border: 1px solid #00ff88; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
  .sig-sell { background: rgba(255,68,102,0.1); color: #ff4466; border: 1px solid #ff4466; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
  .sig-hold { background: rgba(255,255,255,0.05); color: #555; border: 1px solid #333; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-green { background: #00ff88; box-shadow: 0 0 6px #00ff88; }
  .dot-red   { background: #ff4466; box-shadow: 0 0 6px #ff4466; }
  .dot-gold  { background: #ffcc00; box-shadow: 0 0 6px #ffcc00; }
  .tab-bar { display: flex; border-bottom: 1px solid rgba(255,255,255,0.06); margin-bottom: 20px; }
  .tab { padding: 10px 16px; cursor: pointer; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #444; border-bottom: 2px solid transparent; text-decoration: none; }
  .tab-stocks.active { color: #00aaff; border-bottom-color: #00aaff; }
  .tab-crypto.active { color: #00ff88; border-bottom-color: #00ff88; }
  .tab:hover { color: #e0e0e0; }
  .empty { text-align: center; padding: 50px; color: #333; font-size: 15px; }
  @media(max-width:600px) { .grid4 { grid-template-columns: 1fr 1fr; } .grid2 { grid-template-columns: 1fr; } }
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
      <div class="logo-sub">Automated Day Trader · Railway</div>
    </div>
  </div>
  <div class="refresh">Auto-refreshes every 60s · Last update: {now}</div>
</div>

<div class="container">

  <!-- Top stats -->
  <div class="grid4">
    <div class="card">
      <div class="lbl">Portfolio Value</div>
      <div class="big blue">{portfolio}</div>
    </div>
    <div class="card">
      <div class="lbl">US Stocks P&amp;L Today</div>
      <div class="big {stocks_pnl_color}">{stocks_pnl}</div>
    </div>
    <div class="card card-green">
      <div class="lbl">Crypto P&amp;L Today</div>
      <div class="big {crypto_pnl_color}">{crypto_pnl}</div>
    </div>
    <div class="card">
      <div class="lbl">Market Status</div>
      <div style="margin-top:6px">
        <span class="dot {market_dot}"></span>
        <span style="font-weight:700;font-size:13px">{market_status}</span>
      </div>
    </div>
  </div>

  <!-- Bot status row -->
  <div class="grid2">
    <div class="card card-blue">
      <div class="section-title blue">📈 US Stocks Bot</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">
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
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">
        <div><div class="lbl">Status</div><span class="dot {crypto_dot}"></span>{crypto_status}</div>
        <div><div class="lbl">Cycle</div>#{crypto_cycle}</div>
        <div><div class="lbl">Open Positions</div><span style="font-weight:700">{crypto_positions}</span></div>
        <div><div class="lbl">24/7 Mode</div><span class="green">Always On</span></div>
        <div><div class="lbl">Last Run</div><span style="color:#555">{crypto_last}</span></div>
        <div><div class="lbl">Trades Today</div>{crypto_trades}</div>
      </div>
    </div>
  </div>

  <!-- Open positions -->
  {positions_html}

  <!-- Recent trades -->
  {trades_html}

  <!-- Screener -->
  {screener_html}

  <div style="margin-top:24px;padding:14px;background:rgba(255,204,0,0.04);border:1px solid rgba(255,204,0,0.12);border-radius:8px;font-size:11px;color:#666;line-height:1.8">
    ⚠ <strong style="color:#ffcc00">Safety:</strong>
    Stop-loss: {stop_loss}% per trade &nbsp;|&nbsp;
    Daily loss limit: ${max_loss} &nbsp;|&nbsp;
    Max per trade: ${max_trade} &nbsp;|&nbsp;
    Daily spend cap: ${max_spend} &nbsp;|&nbsp;
    Profit target: ${profit_target}
  </div>
</div>
</body>
</html>"""

def build_dashboard():
    global account_info
    acc    = account_info
    et     = datetime.now(ZoneInfo("America/New_York"))
    market = is_market_open()

    portfolio = f"${float(acc.get('portfolio_value', 0)):,.2f}" if acc else "—"

    def pnl_str(v): return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
    def pnl_color(v): return "green" if v >= 0 else "red"

    def dot_for(st):
        if st.shutoff: return "dot-red"
        if st.running: return "dot-green"
        return "dot-gold"

    def status_for(st):
        if st.shutoff: return "Shut Off"
        if st.running: return "Running"
        return "Idle"

    # Positions table
    all_pos = (
        [(sym, pos, "blue", False) for sym, pos in state.positions.items()] +
        [(sym, pos, "green", True) for sym, pos in crypto_state.positions.items()]
    )
    if all_pos:
        rows = ""
        for sym, pos, color, crypto in all_pos:
            live = pos["entry_price"]
            pnl  = (live - pos["entry_price"]) * pos["qty"]
            pnl_c = "green" if pnl >= 0 else "red"
            rows += f"""<tr>
              <td style="color:#{'' if color=='blue' else ''}; font-weight:700" class="{color}">{sym}</td>
              <td>{'Crypto' if crypto else 'Stock'}</td>
              <td>{pos['qty']}</td>
              <td>${pos['entry_price']:.4f}</td>
              <td class="red">${pos['stop_price']:.4f}</td>
              <td class="{pnl_c}" style="font-weight:700">{'+' if pnl>=0 else ''}${pnl:.2f}</td>
            </tr>"""
        positions_html = f"""<div class="card" style="margin-bottom:20px">
          <div class="section-title">Open Positions ({len(all_pos)})</div>
          <table><thead><tr><th>Symbol</th><th>Type</th><th>Qty</th><th>Entry</th><th>Stop</th><th>P&amp;L</th></tr></thead>
          <tbody>{rows}</tbody></table></div>"""
    else:
        positions_html = ""

    # Recent trades
    all_trades = (
        [dict(t, market="Stock") for t in state.trades[:5]] +
        [dict(t, market="Crypto") for t in crypto_state.trades[:5]]
    )
    all_trades.sort(key=lambda t: t["time"], reverse=True)
    if all_trades:
        rows = ""
        for t in all_trades[:12]:
            if t.get("pnl") is not None:
                pnl_color = "green" if t["pnl"] >= 0 else "red"
                pnl_sign  = "+" if t["pnl"] >= 0 else ""
                pnl_td = f'<td class="{pnl_color}">{pnl_sign}${t["pnl"]:.2f}</td>'
            else:
                pnl_td = '<td>—</td>'
            side_c = "green" if t["side"] == "BUY" else "red"
            market_c = "blue" if t["market"] == "Stock" else "green"
            rows += f"""<tr>
              <td style="color:#555">{t['time']}</td>
              <td class="{market_c}">{t['symbol']}</td>
              <td>{t['market']}</td>
              <td class="{side_c}" style="font-weight:700">{t['side']}</td>
              <td>{t['qty']}</td>
              <td>${t['price']:.4f}</td>
              {pnl_td}
              <td style="color:#555;font-size:11px">{t['reason']}</td>
            </tr>"""
        trades_html = f"""<div class="card" style="margin-bottom:20px">
          <div class="section-title">Recent Trades</div>
          <table><thead><tr><th>Time</th><th>Symbol</th><th>Type</th><th>Side</th><th>Qty</th><th>Price</th><th>P&amp;L</th><th>Reason</th></tr></thead>
          <tbody>{rows}</tbody></table></div>"""
    else:
        trades_html = ""

    # Screener top 10
    all_cands = (
        [dict(c, market="Stock") for c in state.candidates[:5] if c["signal"]=="BUY"] +
        [dict(c, market="Crypto") for c in crypto_state.candidates[:5] if c["signal"]=="BUY"]
    )
    if all_cands:
        rows = ""
        for c in all_cands:
            sig_class = {"BUY":"sig-buy","SELL":"sig-sell","HOLD":"sig-hold"}[c["signal"]]
            market_c  = "blue" if c["market"] == "Stock" else "green"
            chg_c     = "green" if c["change"] >= 0 else "red"
            rsi_c     = "red" if c["rsi"] and c["rsi"] > 70 else ("green" if c["rsi"] and c["rsi"] < 35 else "")
            rows += f"""<tr>
              <td class="{market_c}" style="font-weight:700">{c['symbol']}</td>
              <td>{c['market']}</td>
              <td>${c['price']:.4f}</td>
              <td class="{chg_c}">{'+' if c['change']>=0 else ''}{c['change']:.2f}%</td>
              <td><span class="{sig_class}">{c['signal']}</span></td>
              <td class="{rsi_c}">{f"{c['rsi']:.1f}" if c['rsi'] else '—'}</td>
              <td style="color:#555">{f"{c['vol_ratio']:.2f}x" if c['vol_ratio'] else '—'}</td>
            </tr>"""
        screener_html = f"""<div class="card" style="margin-bottom:20px">
          <div class="section-title">Current BUY Signals</div>
          <table><thead><tr><th>Symbol</th><th>Type</th><th>Price</th><th>Chg%</th><th>Signal</th><th>RSI</th><th>Vol Ratio</th></tr></thead>
          <tbody>{rows}</tbody></table></div>"""
    else:
        screener_html = f'<div class="card" style="margin-bottom:20px"><div class="empty">No BUY signals yet — bot will scan on next cycle</div></div>'

    return DASHBOARD_HTML.format(
        now            = datetime.now().strftime("%H:%M:%S"),
        mode_badge     = "badge-live" if IS_LIVE else "badge-paper",
        mode_label     = "LIVE" if IS_LIVE else "PAPER",
        portfolio      = portfolio,
        stocks_pnl     = pnl_str(state.daily_pnl),
        stocks_pnl_color = pnl_color(state.daily_pnl),
        crypto_pnl     = pnl_str(crypto_state.daily_pnl),
        crypto_pnl_color = pnl_color(crypto_state.daily_pnl),
        market_status  = "Open" if market else "Closed",
        market_dot     = "dot-green" if market else "dot-red",
        stocks_dot     = dot_for(state),
        stocks_status  = status_for(state),
        stocks_cycle   = state.cycle_count,
        stocks_positions = len(state.positions),
        stocks_spend   = f"{state.daily_spend:.0f}",
        stocks_last    = state.last_cycle or "—",
        stocks_trades  = len(state.trades),
        crypto_dot     = dot_for(crypto_state),
        crypto_status  = status_for(crypto_state),
        crypto_cycle   = crypto_state.cycle_count,
        crypto_positions = len(crypto_state.positions),
        crypto_last    = crypto_state.last_cycle or "—",
        crypto_trades  = len(crypto_state.trades),
        positions_html = positions_html,
        trades_html    = trades_html,
        screener_html  = screener_html,
        stop_loss      = STOP_LOSS_PCT,
        max_loss       = MAX_DAILY_LOSS,
        max_trade      = MAX_TRADE_VALUE,
        max_spend      = MAX_DAILY_SPEND,
        profit_target  = DAILY_PROFIT_TARGET,
    )

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if self.path == "/api":
            data = json.dumps({
                "stocks": {"pnl": state.daily_pnl, "positions": len(state.positions), "trades": len(state.trades), "cycle": state.cycle_count},
                "crypto": {"pnl": crypto_state.daily_pnl, "positions": len(crypto_state.positions), "trades": len(crypto_state.trades), "cycle": crypto_state.cycle_count},
                "portfolio": float(account_info.get("portfolio_value", 0)) if account_info else 0,
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())
            return
        html = build_dashboard()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        pass  # suppress default access logs

def start_dashboard():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    log.info(f"Dashboard running on port {PORT}")
    server.serve_forever()

# ── Main ──────────────────────────────────────────────────────
def main():
    global account_info

    log.info("=" * 50)
    log.info("AlphaBot starting up")
    log.info(f"Mode:   {'LIVE' if IS_LIVE else 'PAPER'} trading")
    log.info(f"Port:   {PORT}")
    log.info("=" * 50)

    # Start web dashboard FIRST so Railway health check passes immediately
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()
    time.sleep(2)  # Give server a moment to bind to port
    log.info(f"Dashboard ready on port {PORT}")

    # Verify Alpaca connection
    account_info = alpaca_get("/v2/account") or {}
    if not account_info:
        log.error("Cannot connect to Alpaca — check ALPACA_KEY and ALPACA_SECRET")
    else:
        log.info(f"Connected — Portfolio: ${float(account_info.get('portfolio_value',0)):,.2f}")

    last_email_day = None
    cycle = 0

    while True:
        try:
            cycle += 1
            log.info(f"\n{'─'*50}")
            log.info(f"Main cycle {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # Refresh account info each cycle
            account_info = alpaca_get("/v2/account") or account_info

            # Run both bots
            run_cycle(US_WATCHLIST, state, crypto=False)
            run_cycle(CRYPTO_WATCHLIST, crypto_state, crypto=True)

            # Daily email at 5pm ET
            et = datetime.now(ZoneInfo("America/New_York"))
            if et.weekday() < 5 and et.hour == 17 and et.minute < 2 and last_email_day != et.date():
                send_daily_summary()
                last_email_day = et.date()

            log.info(f"Sleeping {CYCLE_SECONDS}s...")
            time.sleep(CYCLE_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped")
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
