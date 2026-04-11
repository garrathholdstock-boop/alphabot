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
NEWS_API_KEY   = os.environ.get("NEWS_API_KEY", "")       # from newsapi.org — free tier
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")     # from console.anthropic.com

# ── Binance (crypto only) ─────────────────────────────────────
# Live Binance keys (real money when IS_LIVE=true)
BINANCE_KEY    = os.environ.get("BINANCE_KEY",    "")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET", "")

# Testnet keys (virtual money — safe to test)
BINANCE_TESTNET_KEY    = os.environ.get("BINANCE_TESTNET_KEY",    "")
BINANCE_TESTNET_SECRET = os.environ.get("BINANCE_TESTNET_SECRET", "")
BINANCE_USE_TESTNET    = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"

# Auto-select correct endpoint and keys
if BINANCE_USE_TESTNET and BINANCE_TESTNET_KEY:
    BINANCE_BASE   = "https://testnet.binance.vision"
    _BIN_KEY       = BINANCE_TESTNET_KEY
    _BIN_SECRET    = BINANCE_TESTNET_SECRET
    USE_BINANCE    = True
elif BINANCE_KEY:
    BINANCE_BASE   = "https://api.binance.com"
    _BIN_KEY       = BINANCE_KEY
    _BIN_SECRET    = BINANCE_SECRET
    USE_BINANCE    = True
else:
    BINANCE_BASE   = "https://api.binance.com"
    _BIN_KEY       = ""
    _BIN_SECRET    = ""
    USE_BINANCE    = False

BINANCE_DELAY  = 0.5    # seconds between Binance API calls (conservative — avoid rate limits)
_last_binance_call  = 0.0
_binance_ban_until  = 0.0   # epoch time when ban expires — stop ALL calls until then

# ── Safety settings ───────────────────────────────────────────
MAX_DAILY_LOSS      = 50.0    # $ shut off if day loss hits this
STOP_LOSS_PCT       = 5.0     # % swing stop (wide — give stocks room to breathe)
TRAILING_STOP_PCT   = 2.0     # % trail — only activates after TRAIL_TRIGGER_PCT profit
TRAIL_TRIGGER_PCT   = 3.0     # % profit required before trailing stop activates
TAKE_PROFIT_PCT     = 10.0    # % take profit — wider to match wider stop
MAX_HOLD_DAYS       = 5       # days max hold (extended to match wider stop logic)
GAP_DOWN_PCT        = 3.0     # % gap down at open triggers immediate sell
MAX_POSITIONS       = 3       # per-bot position limit
MAX_TOTAL_POSITIONS = 3       # GLOBAL cap — hard limit, no exceptions (quality > quantity)
MAX_DAILY_SPEND     = 5000.0
MAX_PORTFOLIO_EXPOSURE = 3000.0
DAILY_PROFIT_TARGET = 200.0   # $ — lowered: survival > profit in early phase
MAX_TRADES_PER_DAY  = 5       # hard cap on total trades per day across all bots
CYCLE_SECONDS          = 60
INTRADAY_CYCLE_SECONDS = 10

# ── Risk-based position sizing ────────────────────────────────
RISK_PER_TRADE_PCT  = 1.0     # % of portfolio to risk per trade
MAX_TRADE_VALUE     = 500.0   # $ hard cap on any single trade

# ── Signal quality threshold ──────────────────────────────────
MIN_SIGNAL_SCORE    = 5       # 0-11 score — trade if score >= this
                              # Multiple factors must align but no single one required

# ── Performance & risk analytics ─────────────────────────────
LOSS_STREAK_LIMIT   = 3       # consecutive losses → pause 2 hours
LOSS_STREAK_PAUSE   = 7200    # seconds to pause after loss streak (2 hours)

# ── Volatility sizing ─────────────────────────────────────────
VIX_LOW_THRESHOLD   = 15.0
VIX_HIGH_THRESHOLD  = 25.0
VIX_EXTREME         = 35.0

# ── News boost ────────────────────────────────────────────────
NEWS_POSITIVE_BOOST = 1.5     # multiply trade size for positive-news stocks

# ── Crypto stops (wider — crypto is more volatile) ────────────
CRYPTO_STOP_PCT     = 4.0     # % crypto swing stop
CRYPTO_TRAIL_PCT    = 3.0     # % crypto trailing stop

# ── Market regime settings ────────────────────────────────────
VIX_FEAR_THRESHOLD    = 25.0   # VIX above this = fear, pause bull buys
SPY_MA_PERIOD         = 20     # SPY moving average period for trend filter
BEAR_TICKERS          = ["SQQQ","UVXY","GLD","SLV","SPXS"]  # buy in stock bear mode
SPY_FAST_DROP_PCT     = 3.0    # % SPY single-day drop triggers instant bear mode
SPY_CIRCUIT_BREAKER   = 5.0    # % SPY intraday drop pauses ALL new buys immediately
MACRO_KEYWORDS        = [      # macro news terms that trigger full pause
    "federal reserve","fed rate","interest rate","recession","inflation",
    "iran","war","sanctions","oil embargo","nuclear","geopolit",
    "bank collapse","credit crisis","market crash","circuit breaker",
    "emergency","black swan","systemic"
]

# ── Crypto regime settings ────────────────────────────────────
BTC_MA_PERIOD         = 20     # BTC moving average period
BTC_CRASH_PCT         = 5.0    # % BTC single-day drop = volatility spike
CRYPTO_MAX_EXPOSURE   = 2000.0 # $ max total in open crypto positions

# ── Intraday scanner settings ────────────────────────────────
INTRADAY_TIMEFRAME      = "1Hour"   # bar size for stock intraday scanner
INTRADAY_BARS           = 48        # 48 x 1h = 2 days of hourly data
INTRADAY_EMA_FAST       = 5         # faster EMA for intraday
INTRADAY_EMA_SLOW       = 13        # slower EMA for intraday
INTRADAY_RSI_LIMIT      = 75        # same RSI cap
INTRADAY_VOL_RATIO      = 1.5       # volume confirmation
INTRADAY_TAKE_PROFIT    = 2.5       # % — smaller target for intraday
INTRADAY_STOP_LOSS      = 1.0       # % — tighter stop for intraday
INTRADAY_MAX_POSITIONS  = 2         # separate limit from swing positions
INTRADAY_MAX_TRADE      = 300.0     # $ — smaller size per intraday trade
INTRADAY_START_HOUR_ET  = 10        # don't trade first 30 mins (volatile open)
INTRADAY_END_HOUR_ET    = 15        # stop at 3pm ET — avoid volatile close

# Crypto intraday — 15 min bars, runs 24/7
CRYPTO_INTRADAY_TIMEFRAME = "15Min"
CRYPTO_INTRADAY_BARS      = 96      # 96 x 15min = 24h of data
CRYPTO_INTRADAY_EMA_FAST  = 5
CRYPTO_INTRADAY_EMA_SLOW  = 13
CRYPTO_INTRADAY_TP        = 2.0     # % take profit — crypto moves fast
CRYPTO_INTRADAY_SL        = 1.0     # % stop loss
CRYPTO_INTRADAY_MAX_POS   = 2       # separate from swing crypto positions
CRYPTO_INTRADAY_MAX_TRADE = 200.0   # $ per intraday crypto trade
CRYPTO_INTRADAY_VOL_RATIO = 1.5

# ── Small cap settings ───────────────────────────────────────
SMALLCAP_MIN_PRICE    = 2.0    # $ minimum price
SMALLCAP_MAX_PRICE    = 20.0   # $ maximum price
SMALLCAP_POOL_SIZE    = 50     # number of small caps to maintain
SMALLCAP_STOP_LOSS    = 1.5    # % tighter stop-loss for small caps
SMALLCAP_MAX_TRADE    = 250.0  # $ smaller position size for small caps
SMALLCAP_VOL_RATIO    = 2.0    # higher volume confirmation required
SMALLCAP_REFRESH_DAYS = 7      # refresh pool every 7 days

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

# ── Crypto watchlist ─────────────────────────────────────────
# Alpaca format (used as fallback if Binance not configured): COIN/USD
# Binance format (used when USE_BINANCE=True): COINUSDT
# The bot auto-selects format based on USE_BINANCE flag

CRYPTO_WATCHLIST_ALPACA = [
    "BTC/USD","ETH/USD","SOL/USD","AVAX/USD","DOGE/USD","SHIB/USD",
    "LTC/USD","BCH/USD","LINK/USD","DOT/USD","UNI/USD","AAVE/USD",
    "XTZ/USD","BAT/USD","CRV/USD","GRT/USD","MKR/USD","MATIC/USD",
    "ALGO/USD","XLM/USD","SUSHI/USD","YFI/USD","ETH/BTC",
]

# Top 50 coins on Binance by volume — auto-refreshes weekly
# Kept at 50 to stay well within Binance rate limits
CRYPTO_WATCHLIST_BINANCE = [
    # Large caps — always trade these
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "AVAXUSDT","DOGEUSDT","DOTUSDT","MATICUSDT","LINKUSDT","LTCUSDT",
    "BCHUSDT","XLMUSDT","ATOMUSDT","ETCUSDT","NEARUSDT","ALGOUSDT",
    # High volatility meme/momentum
    "SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT","WIFUSDT",
    # DeFi
    "AAVEUSDT","UNIUSDT","MKRUSDT","CRVUSDT","GRTUSDT","SUSHIUSDT",
    # AI tokens
    "FETUSDT","AGIXUSDT","OCEANUSDT","WLDUSDT","ARKMUSDT",
    # Layer 2
    "ARBUSDT","OPUSDT","STRKUSDT","INJUSDT","APTUSDT","SUIUSDT",
    # Gaming/NFT
    "AXSUSDT","SANDUSDT","MANAUSDT","GALAUSDT","IMXUSDT",
    # Other high volume
    "FILUSDT","ICPUSDT","RUNEUSDT","TIAUSDT","KASUSDT",
]

# Active list — switches based on USE_BINANCE flag
CRYPTO_WATCHLIST = CRYPTO_WATCHLIST_BINANCE if USE_BINANCE else CRYPTO_WATCHLIST_ALPACA

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
        self.trades_today    = 0       # count of completed trades today

    def check_reset(self):
        today = datetime.now().date()
        if today != self.last_reset_day:
            log.info(f"[{self.label}] Daily reset")
            self.reset()

state            = BotState("STOCKS")
crypto_state     = BotState("CRYPTO")
smallcap_state   = BotState("SMALLCAP")
intraday_state   = BotState("INTRADAY")
crypto_intraday_state = BotState("CRYPTO_ID")
account_info = {}

# ── Global risk state ─────────────────────────────────────────
global_risk = {
    "loss_streak":      0,       # consecutive losses across all bots
    "paused_until":     None,    # datetime when loss streak pause ends
    "total_positions":  0,       # live count across all bots
    "vix_level":        None,    # latest VIX reading for vol sizing
}

# ── Performance analytics ─────────────────────────────────────
perf = {
    "all_trades":      [],       # every completed trade
    "peak_portfolio":  0.0,      # highest portfolio value seen
    "max_drawdown":    0.0,      # worst peak-to-trough %
    "sharpe_daily":    [],       # daily returns for Sharpe calculation
}

# ── Small cap pool (refreshes weekly) ────────────────────────
smallcap_pool = {
    "symbols":       [],
    "last_refresh":  None,
    "last_refresh_day": None,
}

# ── Market regime (updated each cycle) ───────────────────────
market_regime = {
    "mode":        "BULL",   # BULL or BEAR
    "vix":         None,
    "spy_price":   None,
    "spy_ma20":    None,
    "spy_trend":   "unknown",
    "last_check":  None,
}

# ── Circuit breaker state ─────────────────────────────────────
circuit_breaker = {
    "active":       False,   # True = all new buys paused
    "reason":       None,    # why it triggered
    "triggered_at": None,
    "spy_open":     None,    # SPY price at open today for intraday % calc
    "macro_paused": False,   # paused due to macro news
}

# ── Crypto regime (updated each cycle) ───────────────────────
crypto_regime = {
    "mode":        "BULL",   # BULL or BEAR
    "btc_price":   None,
    "btc_ma20":    None,
    "btc_change":  None,     # latest daily % change
    "last_check":  None,
}

# ── News sentiment state (updated each morning) ───────────────
news_state = {
    "skip_list":     {},    # { symbol: { reason, headline, sentiment_score } }
    "watch_list":    {},    # { symbol: { headline, sentiment } } — positive news
    "last_scan_day": None,
    "last_scan_time": None,
    "briefing":      [],    # list of summary lines for email
    "scan_complete": False,
}

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

def place_stop_order_alpaca(symbol, qty, stop_price):
    """Place a real stop-loss order on Alpaca exchange — NOT software stop."""
    result = alpaca_post("/v2/orders", {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          "sell",
        "type":          "stop",
        "stop_price":    str(round(stop_price, 2)),
        "time_in_force": "gtc",
    })
    if result:
        log.info(f"[STOP ORDER] Placed exchange stop for {symbol} @ ${stop_price:.2f} id:{result.get('id','')[:8]}")
    return result

def cancel_stop_order_alpaca(order_id):
    """Cancel an exchange stop order."""
    try:
        r = requests.delete(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
        return r.ok
    except: return False

def update_exchange_stop(symbol, qty, new_stop_price):
    """Cancel old exchange stop and place new one at updated trailing price."""
    old_id = exchange_stops.get(symbol)
    if old_id:
        cancel_stop_order_alpaca(old_id)
    new_order = place_stop_order_alpaca(symbol, qty, round(new_stop_price, 2))
    if new_order and new_order.get("id"):
        exchange_stops[symbol] = new_order["id"]
        log.info(f"[TRAIL] Updated exchange stop {symbol} → ${new_stop_price:.2f}")

# Track exchange stop order IDs per position
exchange_stops = {}  # { symbol: order_id }

# ── Binance API helpers ───────────────────────────────────────
import hashlib, hmac, urllib.parse

def _binance_sign(params):
    """Sign Binance request with HMAC-SHA256."""
    query = urllib.parse.urlencode(params)
    sig   = hmac.new(_BIN_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig

def _binance_ts():
    return int(time.time() * 1000)

BINANCE_HEADERS = {"X-MBX-APIKEY": _BIN_KEY}

def binance_get(path, params=None, signed=False):
    global _last_binance_call, _binance_ban_until

    # ── Ban check — skip ALL calls until ban expires ──
    now_ts = time.time()
    if now_ts < _binance_ban_until:
        remaining = int(_binance_ban_until - now_ts)
        # Only log once per minute to keep logs clean
        if remaining % 60 < 2:
            log.warning(f"[BINANCE] Ban active — {remaining}s remaining ({datetime.fromtimestamp(_binance_ban_until).strftime('%H:%M:%S')})")
        return None  # Hard stop — no call made

    # ── Rate limit — space out calls ──
    elapsed = time.time() - _last_binance_call
    if elapsed < BINANCE_DELAY:
        time.sleep(BINANCE_DELAY - elapsed)
    _last_binance_call = time.time()

    try:
        p = params or {}
        if signed:
            p["timestamp"] = _binance_ts()
            url = f"{BINANCE_BASE}{path}?{_binance_sign(p)}"
        else:
            url = f"{BINANCE_BASE}{path}" + (f"?{urllib.parse.urlencode(p)}" if p else "")
        r = requests.get(url, headers=BINANCE_HEADERS, timeout=10)
        if not r.ok:
            if r.status_code == 418 or r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 120))
                _binance_ban_until = time.time() + retry_after
                log.warning(f"[BINANCE] Rate limited — banned for {retry_after}s. Will retry at {datetime.fromtimestamp(_binance_ban_until).strftime('%H:%M:%S')}")
                return None
            log.debug(f"[BINANCE] {path}: {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        log.debug(f"[BINANCE] {path}: {e}")
        return None

def binance_post(path, params):
    try:
        params["timestamp"] = _binance_ts()
        url  = f"{BINANCE_BASE}{path}"
        body = _binance_sign(params)
        r = requests.post(url, data=body, headers=BINANCE_HEADERS, timeout=10)
        if not r.ok:
            log.warning(f"Binance POST {path}: {r.status_code} {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.warning(f"Binance POST {path}: {e}")
        return None

def binance_fetch_bars(symbol, interval="1d", limit=35):
    """Fetch OHLCV bars from Binance. interval: 1d, 1h, 15m etc."""
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        log.warning(f"[BINANCE] No data returned for {symbol} — check API key or IP restrictions")
        return None
    if len(data) < 10:
        log.warning(f"[BINANCE] Not enough bars for {symbol}: got {len(data)}, need 10+")
        return None
    # Binance kline format: [open_time, open, high, low, close, volume, ...]
    bars = [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5])} for k in data]
    log.debug(f"[BINANCE] {symbol} — {len(bars)} bars, latest close: {bars[-1]['c']}")
    return bars

def binance_fetch_price(symbol):
    """Get latest price from Binance."""
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"]) if data and "price" in data else None

# Cache exchange info to avoid repeated API calls
_binance_lot_cache = {}

def binance_get_lot_size(symbol):
    """Get min qty and step size for a symbol from Binance exchange info."""
    if symbol in _binance_lot_cache:
        return _binance_lot_cache[symbol]
    try:
        data = binance_get("/api/v3/exchangeInfo", {"symbol": symbol})
        if not data: return 0.0001, 0.0001
        for filt in data.get("symbols", [{}])[0].get("filters", []):
            if filt.get("filterType") == "LOT_SIZE":
                min_qty  = float(filt["minQty"])
                step_qty = float(filt["stepSize"])
                _binance_lot_cache[symbol] = (min_qty, step_qty)
                return min_qty, step_qty
    except: pass
    return 0.0001, 0.0001

def round_step(qty, step):
    """Round qty down to nearest step size."""
    import math
    if step <= 0: return qty
    precision = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty / step) * step, precision)

def binance_place_order(symbol, side, usdt_amount):
    """Place a market order on Binance with correct lot size."""
    price = binance_fetch_price(symbol)
    if not price:
        log.error(f"[BINANCE] Cannot get price for {symbol}")
        return None
    min_qty, step_qty = binance_get_lot_size(symbol)
    raw_qty = usdt_amount / price
    qty     = round_step(raw_qty, step_qty)
    if qty < min_qty:
        log.warning(f"[BINANCE] {symbol} qty {qty} below min {min_qty} — skipping")
        return None
    result = binance_post("/api/v3/order", {
        "symbol":   symbol,
        "side":     side.upper(),
        "type":     "MARKET",
        "quantity": str(qty),
    })
    if result:
        log.info(f"[BINANCE] ORDER {side.upper()} {qty} {symbol} @ ~${price:.4f}")
    return result

def binance_get_balance(asset="USDT"):
    """Get available balance for an asset."""
    data = binance_get("/api/v3/account", signed=True)
    if not data:
        return 0.0
    for b in data.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0

def binance_get_top_coins(limit=100):
    """Get top coins by 24h volume from Binance. Returns USDT pairs only."""
    tickers = binance_get("/api/v3/ticker/24hr")
    if not tickers:
        return CRYPTO_WATCHLIST_BINANCE  # fallback to static list
    usdt = [t for t in tickers
            if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume", 0)) > 1_000_000  # min $1M daily volume
            and not any(bad in t["symbol"] for bad in ["UP","DOWN","BEAR","BULL","LEVERAGE"])]
    usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    top = [t["symbol"] for t in usdt[:limit]]
    log.info(f"[BINANCE] Top {len(top)} coins by volume fetched")
    return top if top else CRYPTO_WATCHLIST_BINANCE

# ── Market data ───────────────────────────────────────────────
def fetch_bars(symbol, crypto=False):
    """Fetch daily OHLCV bars. Routes crypto to Binance if configured."""
    if crypto and USE_BINANCE:
        bars = binance_fetch_bars(symbol, interval="1d", limit=35)
        return bars if bars and len(bars) >= 15 else None
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
    """Fetch latest price. Routes crypto to Binance if configured."""
    if crypto and USE_BINANCE:
        return binance_fetch_price(symbol)
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

# ── News sentiment analysis ──────────────────────────────────
def fetch_news_for_symbol(symbol):
    """Fetch latest news headlines for a stock symbol via NewsAPI."""
    if not NEWS_API_KEY:
        return []
    try:
        # Clean symbol for search (remove /USD etc for crypto)
        query = symbol.replace("/USD", "").replace("/BTC", "")
        url = (
            f"https://newsapi.org/v2/everything"
            f"?q={query}+stock+OR+{query}+shares+OR+{query}+earnings"
            f"&sortBy=publishedAt"
            f"&pageSize=5"
            f"&language=en"
            f"&apiKey={NEWS_API_KEY}"
        )
        r = requests.get(url, timeout=8)
        if not r.ok:
            return []
        articles = r.json().get("articles", [])
        return [
            {
                "title":  a.get("title", ""),
                "source": a.get("source", {}).get("name", ""),
                "published": a.get("publishedAt", "")[:10],
            }
            for a in articles if a.get("title")
        ]
    except Exception as e:
        log.debug(f"News fetch {symbol}: {e}")
        return []

def analyse_sentiment_with_claude(symbol, headlines):
    """Use Claude API to score news sentiment for a stock."""
    if not CLAUDE_API_KEY or not headlines:
        return None, "no_data"
    try:
        headline_text = "\n".join(
            f"- {h['title']} ({h['source']}, {h['published']})"
            for h in headlines[:5]
        )
        prompt = (
            f"You are a financial analyst. Analyse these news headlines for {symbol} "
            f"and return ONLY a JSON object with no markdown:\n\n"
            f"{headline_text}\n\n"
            f'Return exactly: {{"sentiment": "POSITIVE" or "NEGATIVE" or "NEUTRAL", '
            f'"score": number from -1.0 to 1.0, '
            f'"skip": true or false (true if strongly negative news that should prevent buying today), '
            f'"reason": "one short sentence explaining the key risk or opportunity", '
            f'"key_headline": "the most important headline"}}'
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if not r.ok:
            return None, "api_error"
        text = r.json()["content"][0]["text"].strip()
        # Strip any markdown fences just in case
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result, "ok"
    except Exception as e:
        log.debug(f"Sentiment {symbol}: {e}")
        return None, "error"

def run_morning_news_scan():
    """Scan all 100 stocks for news sentiment. Runs at 9:00am ET on weekdays."""
    global news_state
    today = datetime.now().date()

    log.info("=" * 50)
    log.info("MORNING NEWS SCAN starting...")
    log.info(f"Scanning {len(US_WATCHLIST)} stocks for sentiment")
    log.info("=" * 50)

    skip_list  = {}
    watch_list = {}
    briefing   = []
    skipped    = 0
    positive   = 0
    neutral    = 0
    errors     = 0

    if not NEWS_API_KEY:
        log.warning("NEWS_API_KEY not set — skipping news scan. Add it in Railway Variables.")
        news_state["scan_complete"] = True
        news_state["last_scan_day"] = today
        return

    for symbol in US_WATCHLIST:
        try:
            headlines = fetch_news_for_symbol(symbol)
            if not headlines:
                neutral += 1
                continue

            result, status = analyse_sentiment_with_claude(symbol, headlines)
            if not result or status != "ok":
                errors += 1
                continue
            # Safety: validate Claude returned expected fields
            if not isinstance(result.get("score"), (int, float)):
                errors += 1
                continue

            sentiment  = result.get("sentiment", "NEUTRAL")
            score      = result.get("score", 0)
            should_skip = result.get("skip", False)
            reason     = result.get("reason", "")
            key_headline = result.get("key_headline", headlines[0]["title"] if headlines else "")

            if should_skip or sentiment == "NEGATIVE":
                skip_list[symbol] = {
                    "sentiment": sentiment,
                    "score":     score,
                    "reason":    reason,
                    "headline":  key_headline,
                }
                briefing.append(f"  🔴 SKIP  {symbol:8} | {reason}")
                log.info(f"[NEWS] SKIP {symbol}: {reason}")
                skipped += 1

            elif sentiment == "POSITIVE" and score > 0.3:
                watch_list[symbol] = {
                    "sentiment": sentiment,
                    "score":     score,
                    "reason":    reason,
                    "headline":  key_headline,
                }
                briefing.append(f"  🟢 BOOST {symbol:8} | {reason}")
                log.info(f"[NEWS] POSITIVE {symbol}: {reason}")
                positive += 1
            else:
                neutral += 1

            # Small delay to respect API rate limits
            time.sleep(0.5)

        except Exception as e:
            log.warning(f"[NEWS] Error scanning {symbol}: {e}")
            errors += 1

    news_state.update({
        "skip_list":      skip_list,
        "watch_list":     watch_list,
        "briefing":       briefing,
        "last_scan_day":  today,
        "last_scan_time": datetime.now().strftime("%H:%M:%S"),
        "scan_complete":  True,
    })

    log.info(f"[NEWS] Scan complete: {skipped} skip | {positive} positive | {neutral} neutral | {errors} errors")

    # Send morning briefing email
    send_morning_briefing(skipped, positive, neutral)

def build_near_miss_section(label, candidates, threshold, top_n=10):
    """Build a near-miss scorecard showing stocks/coins that almost triggered a trade."""
    if not candidates:
        return f"{label} NEAR MISSES\n{'─'*50}\n  No scan data yet\n"

    # Get candidates below threshold, sorted by score descending
    near_misses = [
        c for c in candidates
        if c.get("score", 0) < threshold and c.get("score", 0) > 0
    ]
    near_misses.sort(key=lambda x: x.get("score", 0), reverse=True)
    near_misses = near_misses[:top_n]

    if not near_misses:
        return f"{label} NEAR MISSES\n{'─'*50}\n  No candidates close to threshold\n"

    lines = []
    for c in near_misses:
        score     = c.get("score", 0)
        gap       = threshold - score
        sym       = c.get("symbol", "?")
        price     = c.get("price", 0)
        rsi       = c.get("rsi")
        signal    = c.get("signal", "HOLD")
        vol_ratio = c.get("vol_ratio", 0)
        sma_cross = "YES" if signal in ("BUY","SELL") else "no"
        rsi_str   = f"{rsi:.1f}" if rsi else "—"
        gap_bar   = "█" * int(score) + "░" * int(threshold - score)  # visual bar
        lines.append(
            f"  {sym:<10} score:{score:.1f}/{threshold:.0f}  [{gap_bar}]  "
            f"gap:{gap:.1f}  RSI:{rsi_str}  vol:{vol_ratio:.1f}x  SMA cross:{sma_cross}  "
            f"${price:.4f}  ← {gap:.1f} away from trade"
        )

    header = f"{label} NEAR MISSES (top {len(near_misses)}, threshold={threshold})\n{'─'*50}\n"
    body   = "\n".join(lines)
    footer = "\n\nHow to read: score/threshold | gap = points needed | SMA cross = crossover signal fired\n"
    return header + body + footer

def send_morning_briefing(skipped, positive, neutral):
    """Email the morning news briefing before market open."""
    def fmt_item(sym, data, tag):
        return tag + " " + sym + " | " + data["reason"] + " | " + data["headline"]
    skip_lines  = "\n".join(fmt_item(s,d,"SKIP ") for s,d in news_state["skip_list"].items()) or "  None all clear!"
    boost_lines = "\n".join(fmt_item(s,d,"BOOST") for s,d in news_state["watch_list"].items()) or "  None today"

    # Build near-miss scorecards
    stocks_near_miss = build_near_miss_section("US STOCKS", state.candidates, MIN_SIGNAL_SCORE)
    crypto_near_miss = build_near_miss_section("CRYPTO", crypto_state.candidates, MIN_SIGNAL_SCORE)

    body = f"""
AlphaBot Morning Briefing
{'='*50}
Date:     {datetime.now().strftime('%A, %d %B %Y')}
Time:     {datetime.now().strftime('%H:%M ET')} (market opens at 9:30 ET)
Stocks scanned: {len(US_WATCHLIST)}
Signal threshold: {MIN_SIGNAL_SCORE}/10

SKIPPING TODAY ({skipped} stocks — negative news):
{skip_lines}

POSITIVE SIGNALS ({positive} stocks — good news):
{boost_lines}

SUMMARY
{'─'*50}
  {skipped} stocks skipped due to negative news
  {positive} stocks flagged as positive
  {neutral} stocks with neutral/no news — trading normally

The bot will automatically avoid skipped stocks today.
All restrictions clear at midnight and reset tomorrow.

{'='*50}
SIGNAL SCORECARD — Near Misses
{'='*50}
These almost triggered a trade. Use this to tune the threshold.

{stocks_near_miss}
{crypto_near_miss}
{'='*50}
Sent by AlphaBot · Market opens in ~30 minutes
""".strip()

    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = f"AlphaBot Morning Briefing — {datetime.now().strftime('%d %b %Y')} ({skipped} stocks skipped)"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Morning briefing emailed to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Morning briefing email failed: {e}")

# ── Small cap pool management ────────────────────────────────
def refresh_smallcap_pool():
    """Fetch and rank the most active small cap stocks from Alpaca.
    Filters by price $2-$20, active, tradable on NYSE/NASDAQ.
    Refreshes weekly so the pool stays current."""
    global smallcap_pool
    log.info("[SMALLCAP] Refreshing small cap pool...")

    try:
        # Fetch all active US equity assets from Alpaca
        assets = alpaca_get("/v2/assets?status=active&asset_class=us_equity")
        if not assets:
            log.warning("[SMALLCAP] Could not fetch assets from Alpaca")
            return

        # Filter to small cap criteria
        candidates = [
            a for a in assets
            if (a.get("tradable")
                and a.get("exchange") in ("NYSE", "NASDAQ", "ARCA")
                and a.get("status") == "active"
                and not a.get("symbol","").endswith(("W","R","P","Q"))  # exclude warrants/rights
                and len(a.get("symbol","")) <= 5  # exclude very long symbols
            )
        ]

        log.info(f"[SMALLCAP] {len(candidates)} tradable small cap candidates found")

        # Score by fetching bars and checking price range + volume
        scored = []
        checked = 0
        for asset in candidates:
            sym = asset.get("symbol","")
            if not sym or sym in US_WATCHLIST:
                continue  # skip if already in main watchlist
            bars = fetch_bars(sym)
            if not bars or len(bars) < 10:
                continue
            price = bars[-1]["c"]
            if not (SMALLCAP_MIN_PRICE <= price <= SMALLCAP_MAX_PRICE):
                continue
            volumes = [b["v"] for b in bars]
            avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
            if avg_vol < 50000:  # skip very thinly traded stocks
                continue
            # Score by volume * recent momentum
            closes = [b["c"] for b in bars]
            momentum = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
            score = avg_vol * (1 + abs(momentum) / 100)
            scored.append({"symbol": sym, "price": price, "avg_vol": avg_vol, "momentum": momentum, "score": score})
            checked += 1
            if checked >= 300:  # cap API calls
                break
            time.sleep(0.1)  # rate limit courtesy

        # Sort by score and take top SMALLCAP_POOL_SIZE
        scored.sort(key=lambda x: x["score"], reverse=True)
        pool = [s["symbol"] for s in scored[:SMALLCAP_POOL_SIZE]]

        smallcap_pool["symbols"]          = pool
        smallcap_pool["last_refresh"]     = datetime.now().strftime("%Y-%m-%d %H:%M")
        smallcap_pool["last_refresh_day"] = datetime.now().date()

        log.info(f"[SMALLCAP] Pool refreshed: {len(pool)} stocks | Top 5: {pool[:5]}")

    except Exception as e:
        log.error(f"[SMALLCAP] Pool refresh error: {e}")

def should_refresh_smallcap():
    """Check if small cap pool needs refreshing."""
    if not smallcap_pool["symbols"]:
        return True
    if not smallcap_pool["last_refresh_day"]:
        return True
    days_since = (datetime.now().date() - smallcap_pool["last_refresh_day"]).days
    return days_since >= SMALLCAP_REFRESH_DAYS

# ── Circuit breaker ──────────────────────────────────────────
def check_circuit_breaker():
    """Check for fast SPY drop and intraday crash. Updates circuit_breaker state."""
    global circuit_breaker

    # Reset circuit breaker at market open each day
    et = datetime.now(ZoneInfo("America/New_York"))
    mins = et.hour * 60 + et.minute
    if mins == 570:  # exactly 9:30am — reset
        circuit_breaker["active"]      = False
        circuit_breaker["reason"]      = None
        circuit_breaker["triggered_at"]= None
        circuit_breaker["macro_paused"]= False
        log.info("[CIRCUIT] Reset for new trading day")

    # Fetch SPY snapshot for intraday price
    spy_snap = None
    try:
        r = requests.get(f"{DATA_BASE}/v2/stocks/SPY/snapshot?feed=sip", headers=HEADERS, timeout=8)
        if r.ok:
            spy_snap = r.json()
    except: pass

    if not spy_snap:
        return

    spy_now  = spy_snap.get("latestTrade", {}).get("p")
    spy_open = spy_snap.get("dailyBar",    {}).get("o")
    spy_prev = spy_snap.get("prevDailyBar",{}).get("c")

    if not spy_now or not spy_prev:
        return

    # Store spy open price
    if spy_open:
        circuit_breaker["spy_open"] = spy_open

    # Check 1: fast single-day drop vs previous close
    daily_chg = ((spy_now - spy_prev) / spy_prev) * 100
    if daily_chg <= -SPY_FAST_DROP_PCT and not circuit_breaker["active"]:
        log.warning(f"[CIRCUIT] SPY fast drop {daily_chg:.1f}% vs prev close — flipping to BEAR")
        market_regime["mode"] = "BEAR"

    # Check 2: intraday circuit breaker vs today's open
    if spy_open:
        intraday_chg = ((spy_now - spy_open) / spy_open) * 100
        if intraday_chg <= -SPY_CIRCUIT_BREAKER and not circuit_breaker["active"]:
            reason = f"SPY intraday -{abs(intraday_chg):.1f}% (circuit breaker)"
            log.warning(f"[CIRCUIT] TRIGGERED: {reason}")
            circuit_breaker.update({
                "active":       True,
                "reason":       reason,
                "triggered_at": datetime.now().strftime("%H:%M:%S"),
            })

def check_macro_news():
    """Scan for macro news that should pause all trading. Runs during morning scan."""
    global circuit_breaker
    if not NEWS_API_KEY:
        return
    try:
        # Check top financial headlines for macro keywords
        r = requests.get(
            f"https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=20&apiKey={NEWS_API_KEY}",
            timeout=8
        )
        if not r.ok: return
        articles = r.json().get("articles", [])
        headlines = " ".join(a.get("title","").lower() for a in articles)
        triggered_keywords = [kw for kw in MACRO_KEYWORDS if kw in headlines]
        if triggered_keywords:
            # Use Claude to score macro risk if available
            if CLAUDE_API_KEY:
                sample = "\n".join(a.get("title","") for a in articles[:10])
                prompt = ("Rate the systemic market risk of these headlines 1-10 (10=extreme). "
                          "Return ONLY JSON: {\"score\": 7, \"pause_trading\": true, \"reason\": \"brief reason\"}\n\n" + sample)
                r2 = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 100,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=12
                )
                if r2.ok:
                    result = json.loads(r2.json()["content"][0]["text"].replace("```json","").replace("```","").strip())
                    score = result.get("score", 0)
                    # Hard constraint: only pause when Claude is very confident (score >= 7)
                    if result.get("pause_trading") and isinstance(score, (int,float)) and score >= 7:
                        reason = result.get("reason", f"Macro risk keywords: {', '.join(triggered_keywords[:3])}")
                        log.warning(f"[CIRCUIT] MACRO PAUSE triggered: {reason} (score:{score})")
                        circuit_breaker["macro_paused"] = True
                        circuit_breaker["active"]       = True
                        circuit_breaker["reason"]       = f"Macro news: {reason}"
                        circuit_breaker["triggered_at"] = datetime.now().strftime("%H:%M:%S")
                        return
            # No Claude — pause on keyword match alone if 2+ keywords hit
            if len(triggered_keywords) >= 2:
                reason = f"Macro keywords detected: {', '.join(triggered_keywords[:3])}"
                log.warning(f"[CIRCUIT] MACRO PAUSE: {reason}")
                circuit_breaker["macro_paused"] = True
                circuit_breaker["active"]       = True
                circuit_breaker["reason"]       = reason
                circuit_breaker["triggered_at"] = datetime.now().strftime("%H:%M:%S")
    except Exception as e:
        log.debug(f"[CIRCUIT] Macro check error: {e}")

# ── Intraday bar fetchers ─────────────────────────────────────
# Binance interval map: convert Alpaca-style timeframes to Binance format
BINANCE_INTERVAL_MAP = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m", "30Min": "30m",
    "1Hour": "1h", "2Hour": "2h", "4Hour": "4h", "1Day": "1d",
}

def fetch_intraday_bars(symbol, timeframe="1Hour", limit=48, crypto=False):
    """Fetch sub-daily bars. Routes crypto to Binance if configured."""
    if crypto and USE_BINANCE:
        binance_tf = BINANCE_INTERVAL_MAP.get(timeframe, "15m")
        bars = binance_fetch_bars(symbol, interval=binance_tf, limit=limit)
        return bars if bars and len(bars) >= 10 else None
    try:
        if crypto:
            enc = requests.utils.quote(symbol, safe="")
            url = (f"{DATA_BASE}/v1beta3/crypto/us/bars"
                   f"?symbols={enc}&timeframe={timeframe}&limit={limit}")
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", {}).get(symbol, [])
        else:
            url = (f"{DATA_BASE}/v2/stocks/{symbol}/bars"
                   f"?timeframe={timeframe}&limit={limit}&feed=sip&adjustment=raw")
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", [])
        return bars if bars and len(bars) >= 10 else None
    except Exception as e:
        log.debug(f"intraday bars {symbol}: {e}")
        return None

def get_intraday_signal(closes, volumes, ema_fast, ema_slow, rsi_limit, vol_ratio_min):
    """Intraday signal — faster EMA periods, same logic as swing."""
    ef  = ema(closes, ema_fast)
    es  = ema(closes, ema_slow)
    pef = ema(closes[:-1], ema_fast)
    pes = ema(closes[:-1], ema_slow)
    rsi_val = calc_rsi(closes)
    if None in (ef, es, pef, pes, rsi_val):
        return "HOLD", ef, es, rsi_val
    cross_up   = pef <= pes and ef > es
    cross_down = pef >= pes and ef < es
    # Volume check
    vol_ok = True
    if volumes and len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        vol_ok  = volumes[-1] >= avg_vol * vol_ratio_min
    if cross_up and rsi_val < rsi_limit and vol_ok:
        return "BUY", ef, es, rsi_val
    if cross_down or rsi_val > rsi_limit:
        return "SELL", ef, es, rsi_val
    return "HOLD", ef, es, rsi_val

def is_intraday_window():
    """Stock intraday only trades between 10am-3pm ET."""
    et   = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5: return False
    return INTRADAY_START_HOUR_ET <= et.hour < INTRADAY_END_HOUR_ET

# ── Intraday position manager ─────────────────────────────────
def check_intraday_positions(st, crypto=False):
    """Faster position check for intraday trades — tighter stops."""
    sl_pct = CRYPTO_INTRADAY_SL if crypto else INTRADAY_STOP_LOSS
    tp_pct = CRYPTO_INTRADAY_TP if crypto else INTRADAY_TAKE_PROFIT
    now    = datetime.now()
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym, crypto=crypto)
        if not live: continue
        entry = pos["entry_price"]
        high  = pos.get("highest_price", entry)
        pct   = ((live - entry) / entry) * 100
        # Trail the stop
        if live > high:
            pos["highest_price"] = live
            new_stop = live * (1 - sl_pct / 100)
            if new_stop > pos["stop_price"]:
                pos["stop_price"] = new_stop
        reason = None
        if live >= pos.get("take_profit_price", entry * 1.025): reason = f"Take-Profit (+{pct:.1f}%)"
        elif live <= pos["stop_price"]:                          reason = f"Stop-Loss ({pct:.1f}%)"
        # Force close at end of trading window for stocks
        if not crypto and not is_intraday_window() and is_market_open():
            reason = "End-of-Window"
        if reason:
            pnl = (live - entry) * pos["qty"]
            entry_ts   = pos.get("entry_ts")
            hold_hours = round((now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 2) if entry_ts else None
            log.info(f"[{st.label}] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")
            place_order(sym, "sell", pos["qty"], crypto=crypto)
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": f"[ID]{reason}",
                "time": now.strftime("%H:%M:%S"), "hold_hours": hold_hours})
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                st.shutoff = True

# ── Intraday stock scanner cycle ──────────────────────────────
def run_intraday_cycle(watchlist, st):
    """1-hour bar scanner for sharp single-day stock moves. 10am–3pm ET only."""
    st.check_reset()
    if st.shutoff: return
    if not is_intraday_window(): return
    if market_regime["mode"] == "BEAR": return  # no intraday in bear mode
    if circuit_breaker["active"]:
        log.info("[INTRADAY] Circuit breaker active — skipping")
        return

    st.running    = True
    st.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[INTRADAY] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")

    check_intraday_positions(st, crypto=False)
    if st.shutoff: st.running = False; return

    results = []
    for sym in watchlist:
        if sym in news_state.get("skip_list", {}): continue
        bars = fetch_intraday_bars(sym, timeframe=INTRADAY_TIMEFRAME, limit=INTRADAY_BARS)
        if not bars or len(bars) < 14: continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        prev    = closes[-2]
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, ef, es, rsi_val = get_intraday_signal(
            closes, volumes,
            INTRADAY_EMA_FAST, INTRADAY_EMA_SLOW,
            INTRADAY_RSI_LIMIT, INTRADAY_VOL_RATIO
        )
        # VWAP confirmation — only BUY when price is above VWAP
        vwap_pos = vwap_signal(bars)
        if signal == "BUY" and vwap_pos == "BELOW":
            signal = "HOLD"  # price below VWAP — skip intraday buy
            log.debug(f"[INTRADAY] {sym} BUY suppressed — price below VWAP")
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": ef, "sma21": es, "rsi": rsi_val,
            "vol_ratio": vol_ratio, "vwap": vwap_pos, "intraday": True})

    results.sort(key=lambda x: {"BUY":0,"HOLD":1,"SELL":2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[INTRADAY] {buys} BUY / {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= INTRADAY_MAX_POSITIONS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= MAX_PORTFOLIO_EXPOSURE: break
        qty = max(1, int(INTRADAY_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - INTRADAY_STOP_LOSS / 100)
        tp_price   = s["price"] * (1 + INTRADAY_TAKE_PROFIT / 100)
        log.info(f"[INTRADAY] BUY {s['symbol']} @ ${s['price']:.2f} x{qty} "
                 f"stop:${stop_price:.2f} target:${tp_price:.2f} RSI:{s['rsi']:.1f}")
        order = place_order(s["symbol"], "buy", qty)
        if order:
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": s["price"],
                "stop_price": stop_price, "highest_price": s["price"],
                "take_profit_price": tp_price,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": s["price"], "pnl": None, "reason": "[ID]Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_ts})
            pos_count += 1

    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        pnl = (s["price"] - pos["entry_price"]) * pos["qty"]
        entry_ts   = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 2) if entry_ts else None
        log.info(f"[INTRADAY] SELL {s['symbol']} @ ${s['price']:.2f} P&L:${pnl:+.2f}")
        place_order(s["symbol"], "sell", pos["qty"])
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "[ID]Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        if st.daily_pnl >= DAILY_PROFIT_TARGET: st.shutoff = True; break
        if st.daily_pnl <= -MAX_DAILY_LOSS:     st.shutoff = True; break

    st.running = False

# ── Intraday crypto scanner cycle ─────────────────────────────
def run_crypto_intraday_cycle(watchlist, st):
    """15-minute bar scanner for crypto spikes. Runs 24/7."""
    st.check_reset()
    if st.shutoff: return
    if crypto_regime["mode"] == "BEAR":
        log.info("[CRYPTO_ID] Bear mode — skipping intraday buys")
        return

    st.running    = True
    st.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[CRYPTO_ID] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")

    check_intraday_positions(st, crypto=True)
    if st.shutoff: st.running = False; return

    results = []
    for sym in watchlist:
        bars = fetch_intraday_bars(sym, timeframe=CRYPTO_INTRADAY_TIMEFRAME,
                                   limit=CRYPTO_INTRADAY_BARS, crypto=True)
        if not bars or len(bars) < 14: continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        prev    = closes[-2]
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, ef, es, rsi_val = get_intraday_signal(
            closes, volumes,
            CRYPTO_INTRADAY_EMA_FAST, CRYPTO_INTRADAY_EMA_SLOW,
            INTRADAY_RSI_LIMIT, CRYPTO_INTRADAY_VOL_RATIO
        )
        # VWAP filter for crypto intraday
        vwap_pos = vwap_signal(bars)
        if signal == "BUY" and vwap_pos == "BELOW":
            signal = "HOLD"
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": ef, "sma21": es, "rsi": rsi_val,
            "vol_ratio": vol_ratio, "vwap": vwap_pos, "intraday": True})

    results.sort(key=lambda x: {"BUY":0,"HOLD":1,"SELL":2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[CRYPTO_ID] {buys} BUY / {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= CRYPTO_INTRADAY_MAX_POS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= CRYPTO_MAX_EXPOSURE: break
        qty = max(0.0001, round(CRYPTO_INTRADAY_MAX_TRADE / s["price"], 6))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - CRYPTO_INTRADAY_SL / 100)
        tp_price   = s["price"] * (1 + CRYPTO_INTRADAY_TP / 100)
        log.info(f"[CRYPTO_ID] BUY {s['symbol']} @ ${s['price']:.4f} "
                 f"stop:${stop_price:.4f} target:${tp_price:.4f} RSI:{s['rsi']:.1f}")
        order = place_order(s["symbol"], "buy", qty, crypto=True)
        if order:
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": s["price"],
                "stop_price": stop_price, "highest_price": s["price"],
                "take_profit_price": tp_price,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": s["price"], "pnl": None, "reason": "[ID]Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_ts})
            pos_count += 1

    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        pnl = (s["price"] - pos["entry_price"]) * pos["qty"]
        entry_ts   = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 2) if entry_ts else None
        log.info(f"[CRYPTO_ID] SELL {s['symbol']} @ ${s['price']:.4f} P&L:${pnl:+.2f}")
        place_order(s["symbol"], "sell", pos["qty"], crypto=True)
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "[ID]Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        if st.daily_pnl >= DAILY_PROFIT_TARGET: st.shutoff = True; break
        if st.daily_pnl <= -MAX_DAILY_LOSS:     st.shutoff = True; break

    st.running = False

# ── Market regime detection ──────────────────────────────────
def update_market_regime():
    """Check VIX and SPY trend to determine BULL or BEAR mode."""
    global market_regime

    # Fetch SPY bars for MA calculation
    spy_bars = fetch_bars("SPY")
    vix_bars = fetch_bars("VIXY")  # VIXY is the VIX ETF tradeable via Alpaca

    spy_price = None
    spy_ma20  = None
    vix_val   = None

    if spy_bars and len(spy_bars) >= SPY_MA_PERIOD:
        closes    = [b["c"] for b in spy_bars]
        spy_price = closes[-1]
        spy_ma20  = sum(closes[-SPY_MA_PERIOD:]) / SPY_MA_PERIOD

    # Try VIXY as VIX proxy, fallback to hardcoded neutral
    if vix_bars:
        vix_val = vix_bars[-1]["c"]

    # Determine regime
    bear_signals = 0
    if spy_price and spy_ma20 and spy_price < spy_ma20:
        bear_signals += 1
        log.info(f"[REGIME] SPY ${spy_price:.2f} below MA20 ${spy_ma20:.2f} — bearish signal")
    if vix_val and vix_val > VIX_FEAR_THRESHOLD:
        bear_signals += 1
        log.info(f"[REGIME] VIX proxy ${vix_val:.2f} above threshold {VIX_FEAR_THRESHOLD} — fear signal")

    old_mode       = market_regime["mode"]
    bear_count     = market_regime.get("bear_count", 0)
    # Require 2 consecutive bear signals before switching (reduces whipsaw)
    if bear_signals >= 1:
        bear_count += 1
    else:
        bear_count = max(0, bear_count - 1)
    new_mode = "BEAR" if bear_count >= 2 else "BULL"

    market_regime.update({
        "mode":       new_mode,
        "bear_count": bear_count,
        "vix":        vix_val,
        "spy_price":  spy_price,
        "spy_ma20":   spy_ma20,
        "spy_trend":  "below MA20" if (spy_price and spy_ma20 and spy_price < spy_ma20) else "above MA20",
        "last_check": datetime.now().strftime("%H:%M:%S"),
    })

    if old_mode != new_mode:
        log.warning(f"[REGIME] Mode changed: {old_mode} -> {new_mode} (bear_count={bear_count})")
        if new_mode == "BEAR":
            log.warning("[REGIME] BEAR MODE: pausing bull buys, rotating to defensive tickers")
        else:
            log.info("[REGIME] BULL MODE: resuming normal trading")

    spy_p_str  = f"${spy_price:.2f}"  if spy_price else "N/A"
    spy_m_str  = f"${spy_ma20:.2f}"  if spy_ma20  else "N/A"
    vix_str_   = f"{vix_val:.2f}"    if vix_val   else "N/A"
    log.info(f"[REGIME] {new_mode} | SPY: {spy_p_str} MA20: {spy_m_str} | VIX proxy: {vix_str_}")
    return new_mode

# ── Crypto regime detection ──────────────────────────────────
def update_crypto_regime():
    """Check BTC trend and volatility to determine crypto BULL or BEAR mode."""
    global crypto_regime

    btc_bars = fetch_bars("BTC/USD", crypto=True)
    if not btc_bars or len(btc_bars) < BTC_MA_PERIOD:
        log.info("[CRYPTO REGIME] Not enough BTC data — staying in current mode")
        return crypto_regime["mode"]

    closes    = [b["c"] for b in btc_bars]
    btc_price = closes[-1]
    btc_prev  = closes[-2]
    btc_ma20  = sum(closes[-BTC_MA_PERIOD:]) / BTC_MA_PERIOD
    btc_change = ((btc_price - btc_prev) / btc_prev) * 100

    bear_signals = 0

    # Signal 1: BTC below its 20-day MA
    if btc_price < btc_ma20:
        bear_signals += 1
        log.info(f"[CRYPTO REGIME] BTC ${btc_price:.0f} below MA20 ${btc_ma20:.0f} — bearish")

    # Signal 2: BTC crashed more than BTC_CRASH_PCT today
    if btc_change <= -BTC_CRASH_PCT:
        bear_signals += 1
        log.info(f"[CRYPTO REGIME] BTC daily drop {btc_change:.1f}% — volatility spike")

    old_mode   = crypto_regime["mode"]
    bear_count = crypto_regime.get("bear_count", 0)
    if bear_signals >= 1:
        bear_count += 1
    else:
        bear_count = max(0, bear_count - 1)
    new_mode = "BEAR" if bear_count >= 2 else "BULL"
    crypto_regime["bear_count"] = bear_count

    crypto_regime.update({
        "mode":       new_mode,
        "btc_price":  btc_price,
        "btc_ma20":   btc_ma20,
        "btc_change": btc_change,
        "last_check": datetime.now().strftime("%H:%M:%S"),
    })

    if old_mode != new_mode:
        log.warning(f"[CRYPTO REGIME] Mode changed: {old_mode} -> {new_mode}")
        if new_mode == "BEAR":
            log.warning("[CRYPTO REGIME] BEAR MODE: pausing all new crypto buys, protecting capital")
        else:
            log.info("[CRYPTO REGIME] BULL MODE: resuming crypto trading")

    log.info(f"[CRYPTO REGIME] {new_mode} | BTC: ${btc_price:.0f} MA20: ${btc_ma20:.0f} | Daily: {btc_change:+.1f}%")
    return new_mode

# ── Portfolio exposure check ──────────────────────────────────
def total_exposure(st):
    return sum(pos["entry_price"] * pos["qty"] for pos in st.positions.values())

def all_positions_count():
    """Total open positions across ALL bots."""
    return (len(state.positions) + len(crypto_state.positions) +
            len(smallcap_state.positions) + len(intraday_state.positions) +
            len(crypto_intraday_state.positions))

# ── Volatility-adjusted trade sizing ─────────────────────────
def vol_adjusted_size(base_size):
    """Scale position size down when VIX is elevated."""
    vix = global_risk.get("vix_level")
    if not vix:
        return base_size
    if vix >= VIX_EXTREME:
        return base_size * 0.25      # -75% in extreme fear
    if vix >= VIX_HIGH_THRESHOLD:
        return base_size * 0.50      # -50% in high fear
    if vix <= VIX_LOW_THRESHOLD:
        return base_size * 1.25      # +25% in calm market
    return base_size

# ── News boost ────────────────────────────────────────────────
def news_size_multiplier(symbol):
    """Boost size for positive-news stocks, normal for neutral."""
    if symbol in news_state.get("watch_list", {}):
        return NEWS_POSITIVE_BOOST
    return 1.0

# ── Loss streak check ─────────────────────────────────────────
def is_loss_streak_paused():
    """Returns True if bot should pause due to consecutive losses."""
    if global_risk["paused_until"] and datetime.now() < global_risk["paused_until"]:
        remaining = (global_risk["paused_until"] - datetime.now()).seconds // 60
        log.info(f"[RISK] Loss streak pause active — {remaining} mins remaining")
        return True
    return False

def record_trade_result(pnl, symbol):
    """Track loss streaks and trigger pause if needed."""
    perf["all_trades"].append({"pnl": pnl, "symbol": symbol, "time": datetime.now().isoformat()})
    if pnl < 0:
        global_risk["loss_streak"] += 1
        if global_risk["loss_streak"] >= LOSS_STREAK_LIMIT:
            pause_until = datetime.now() + timedelta(seconds=LOSS_STREAK_PAUSE)
            global_risk["paused_until"] = pause_until
            log.warning(f"[RISK] {LOSS_STREAK_LIMIT} consecutive losses — pausing all trading for 1 hour until {pause_until.strftime('%H:%M')}")
    else:
        global_risk["loss_streak"] = 0  # reset on win

# ── Breakout signal ───────────────────────────────────────────
# ── Relative Strength vs SPY ─────────────────────────────────
_spy_closes_cache = {"closes": [], "last_fetch": None}

def get_spy_closes():
    """Fetch SPY daily closes, cached so we only call once per cycle."""
    now = datetime.now()
    last = _spy_closes_cache["last_fetch"]
    if last and (now - last).seconds < 300:  # cache 5 mins
        return _spy_closes_cache["closes"]
    bars = fetch_bars("SPY", crypto=False)
    if bars:
        closes = [b["c"] for b in bars]
        _spy_closes_cache["closes"] = closes
        _spy_closes_cache["last_fetch"] = now
        return closes
    return _spy_closes_cache["closes"]

def relative_strength_vs_spy(stock_closes):
    """Compare stock's recent performance to SPY.
    Returns positive number if stock is outperforming SPY, negative if underperforming."""
    spy_closes = get_spy_closes()
    if not spy_closes or len(spy_closes) < 5 or len(stock_closes) < 5:
        return 0.0
    periods = min(len(spy_closes), len(stock_closes), 10)
    stock_ret = (stock_closes[-1] - stock_closes[-periods]) / stock_closes[-periods] * 100
    spy_ret   = (spy_closes[-1]   - spy_closes[-periods])   / spy_closes[-periods]   * 100
    return round(stock_ret - spy_ret, 2)  # positive = outperforming SPY

# ── VWAP calculation for intraday ─────────────────────────────
def calc_vwap(bars):
    """Volume-weighted average price from intraday bars.
    Price above VWAP = bullish, below = bearish."""
    if not bars or len(bars) < 3:
        return None
    total_vol = sum(b["v"] for b in bars)
    if total_vol == 0:
        return None
    vwap = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars) / total_vol
    return vwap

def vwap_signal(bars):
    """Returns 'ABOVE', 'BELOW', or None vs VWAP."""
    vwap = calc_vwap(bars)
    if not vwap or not bars:
        return None
    price = bars[-1]["c"]
    pct_from_vwap = ((price - vwap) / vwap) * 100
    if pct_from_vwap > 0.3:   return "ABOVE"   # meaningfully above VWAP — bullish
    if pct_from_vwap < -0.3:  return "BELOW"   # meaningfully below VWAP — bearish
    return "AT"

def is_breakout(closes, lookback=20):
    """Price breaking above highest close in last N bars."""
    if len(closes) < lookback + 1: return False
    return closes[-1] > max(closes[-(lookback+1):-1])

def is_choppy_market():
    """SPY within 0.5% of MA20 = ranging, no trend — skip trading."""
    spy_price = market_regime.get("spy_price")
    spy_ma20  = market_regime.get("spy_ma20")
    if not spy_price or not spy_ma20: return False
    return abs((spy_price - spy_ma20) / spy_ma20) * 100 < 0.5

# ── Unified signal scorer (0-11, threshold 5) ────────────────
def score_signal(sym, price, change, rsi, vol_ratio, closes):
    """
    Score a BUY candidate 0-11. Trade if score >= MIN_SIGNAL_SCORE (5).
    Multiple factors must align — no single factor guarantees a trade.

      Breakout 20-bar high  : +2.0  (strong momentum signal)
      Strong momentum 5d>3% : +1.0  (meaningful price move)
      Relative strength SPY : +1.5  (only trade market leaders)
      Volume 2x+            : +2.0  (strong conviction)
      Volume 1.5x+          : +1.0  (decent conviction)
      Volume 1.2x+          : +0.5  (mild confirmation)
      RSI 50-65 sweet spot  : +1.0  (ideal momentum zone)
      RSI 40-50 building    : +0.5  (building momentum)
      MACD bullish          : +1.0  (acceleration confirmed)
      Positive news         : +1.5  (catalyst — your edge)
      Negative news         : -5.0  (hard skip)
      RSI overbought >75    : -1.0  (too extended)
      Choppy SPY            : -1.0  (low quality environment)
    """
    score = 0.0

    # Breakout or strong momentum
    if is_breakout(closes, lookback=20):
        score += 2.0
    elif len(closes) >= 6 and closes[-6] > 0:
        m5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if m5d >= 3.0:   score += 1.0
        elif m5d >= 1.5: score += 0.5

    # Relative strength vs SPY
    if relative_strength_vs_spy(closes):
        score += 1.5

    # Volume conviction
    if vol_ratio >= 2.0:   score += 2.0
    elif vol_ratio >= 1.5: score += 1.0
    elif vol_ratio >= 1.2: score += 0.5

    # RSI quality
    if rsi:
        if 50 <= rsi <= 65:  score += 1.0
        elif 40 <= rsi < 50: score += 0.5
        elif rsi > 75:       score -= 1.0

    # MACD confirmation
    if len(closes) >= 35:
        mv, ms = calc_macd(closes)
        if mv is not None and ms is not None and mv > ms:
            score += 1.0

    # News catalyst
    if sym in news_state.get("watch_list", {}): score += 1.5
    if sym in news_state.get("skip_list",   {}): score -= 5.0

    # Environment
    if is_choppy_market(): score -= 1.0

    return round(min(11.0, max(0.0, score)), 1)

# ── Max drawdown tracking ─────────────────────────────────────
def update_drawdown(portfolio_value):
    """Track peak portfolio value and calculate max drawdown."""
    if portfolio_value > perf["peak_portfolio"]:
        perf["peak_portfolio"] = portfolio_value
    if perf["peak_portfolio"] > 0:
        dd = ((perf["peak_portfolio"] - portfolio_value) / perf["peak_portfolio"]) * 100
        if dd > perf["max_drawdown"]:
            perf["max_drawdown"] = dd

def calc_profit_factor():
    """Gross profit / gross loss."""
    wins   = sum(t["pnl"] for t in perf["all_trades"] if t["pnl"] > 0)
    losses = sum(abs(t["pnl"]) for t in perf["all_trades"] if t["pnl"] < 0)
    return round(wins / losses, 2) if losses > 0 else float("inf")

def calc_sharpe():
    """Simple daily Sharpe ratio estimate."""
    daily = perf["sharpe_daily"]
    if len(daily) < 5: return None
    import statistics
    avg  = statistics.mean(daily)
    std  = statistics.stdev(daily)
    return round((avg / std) * (252 ** 0.5), 2) if std > 0 else None

# ── Indicators ────────────────────────────────────────────────
def ema(prices, period):
    """Exponential Moving Average — weights recent prices more heavily than SMA."""
    if len(prices) < period: return None
    k = 2 / (period + 1)
    result = sum(prices[:period]) / period  # seed with SMA
    for price in prices[period:]:
        result = price * k + result * (1 - k)
    return result

def sma(prices, period):
    """Simple Moving Average — kept for regime checks."""
    if len(prices) < period: return None
    return sum(prices[-period:]) / period

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    ch = [prices[i] - prices[i-1] for i in range(1, len(prices))][-period:]
    ag = sum(c for c in ch if c > 0) / period
    al = sum(-c for c in ch if c < 0) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def calc_macd(prices):
    """MACD = EMA12 - EMA26. Signal line = EMA9 of MACD.
    Returns (macd_line, signal_line) or (None, None) if not enough data."""
    if len(prices) < 35: return None, None
    macd_line = []
    for i in range(26, len(prices) + 1):
        e12 = ema(prices[:i], 12)
        e26 = ema(prices[:i], 26)
        if e12 and e26:
            macd_line.append(e12 - e26)
    if len(macd_line) < 9: return None, None
    signal_line = ema(macd_line, 9)
    return macd_line[-1], signal_line

VOLUME_MIN_RATIO = 1.2  # volume must be at least 1.2x 10-day average to confirm signal

def get_signal(closes, volumes=None):
    """
    Signal uses EMA 9/21 crossover (upgraded from SMA) + RSI filter.
    Optional volume confirmation: only BUY if volume >= 1.5x average.
    MACD used as additional confirmation for BUY signals.
    """
    # EMA crossover (upgraded from SMA)
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    pe9  = ema(closes[:-1], 9)
    pe21 = ema(closes[:-1], 21)
    rsi  = calc_rsi(closes)

    if None in (e9, e21, pe9, pe21, rsi):
        return "HOLD", e9, e21, rsi

    cross_up   = pe9 <= pe21 and e9 > e21   # EMA 9 crossed above EMA 21
    cross_down = pe9 >= pe21 and e9 < e21   # EMA 9 crossed below EMA 21

    # MACD confirmation
    macd, macd_sig = calc_macd(closes)
    macd_bullish = macd is not None and macd_sig is not None and macd > macd_sig
    macd_bearish = macd is not None and macd_sig is not None and macd < macd_sig

    # Volume confirmation
    vol_confirmed = True
    if volumes and len(volumes) >= 11:
        avg_vol = sum(volumes[-11:-1]) / 10
        vol_confirmed = volumes[-1] >= avg_vol * VOLUME_MIN_RATIO

    # BUY: EMA crossover up + RSI not overbought + volume confirmed + MACD bullish
    if cross_up and rsi < 75 and vol_confirmed and (macd_bullish or macd is None):
        return "BUY", e9, e21, rsi

    # SELL: EMA crossover down OR overbought RSI OR MACD turning bearish
    if cross_down or rsi > 75 or (cross_down and macd_bearish):
        return "SELL", e9, e21, rsi

    return "HOLD", e9, e21, rsi

def get_signal_smallcap(closes, volumes=None):
    """Tighter signal for small caps — higher volume requirement, same EMA/RSI/MACD logic."""
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    pe9  = ema(closes[:-1], 9)
    pe21 = ema(closes[:-1], 21)
    rsi  = calc_rsi(closes)
    if None in (e9, e21, pe9, pe21, rsi):
        return "HOLD", e9, e21, rsi
    cross_up   = pe9 <= pe21 and e9 > e21
    cross_down = pe9 >= pe21 and e9 < e21
    macd, macd_sig = calc_macd(closes)
    macd_bullish = macd is not None and macd_sig is not None and macd > macd_sig
    macd_bearish = macd is not None and macd_sig is not None and macd < macd_sig
    # Small caps need stronger volume confirmation
    vol_confirmed = True
    if volumes and len(volumes) >= 11:
        avg_vol = sum(volumes[-11:-1]) / 10
        vol_confirmed = volumes[-1] >= avg_vol * SMALLCAP_VOL_RATIO
    if cross_up and rsi < 75 and vol_confirmed and (macd_bullish or macd is None):
        return "BUY", e9, e21, rsi
    if cross_down or rsi > 75 or (cross_down and macd_bearish):
        return "SELL", e9, e21, rsi
    return "HOLD", e9, e21, rsi

def is_market_open():
    et   = datetime.now(ZoneInfo("America/New_York"))
    mins = et.hour * 60 + et.minute
    return et.weekday() < 5 and 570 <= mins < 960

# ── Orders ────────────────────────────────────────────────────
# ── Slippage model ────────────────────────────────────────────
# Conservative estimate: 0.1% for large caps, 0.3% for crypto/small caps
SLIPPAGE_STOCK  = 0.003   # 0.3% — realistic for market orders
SLIPPAGE_CRYPTO = 0.005   # 0.5% — crypto spreads wider

def apply_slippage(price, side, crypto=False):
    """Apply conservative slippage to get realistic fill price."""
    slippage = SLIPPAGE_CRYPTO if crypto else SLIPPAGE_STOCK
    if side == "buy":
        return price * (1 + slippage)   # pay more when buying
    else:
        return price * (1 - slippage)   # receive less when selling

def query_order_status(order_id, crypto=False):
    """Check actual fill status of an order."""
    try:
        if crypto and USE_BINANCE:
            return None  # Binance order status checked via account endpoint
        result = alpaca_get(f"/v2/orders/{order_id}")
        return result
    except: return None

def get_actual_fill_price(order_result, side, estimated_price, crypto=False):
    """Extract actual fill price from order result, fall back to slippage estimate."""
    if not order_result:
        return apply_slippage(estimated_price, side, crypto)
    # Alpaca returns filled_avg_price
    filled = order_result.get("filled_avg_price")
    if filled:
        try:
            return float(filled)
        except: pass
    # Binance returns fills array
    fills = order_result.get("fills", [])
    if fills:
        total_qty   = sum(float(f["qty"]) for f in fills)
        total_value = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        if total_qty > 0:
            return total_value / total_qty
    # Fall back to slippage model
    return apply_slippage(estimated_price, side, crypto)

def is_order_filled(order_result):
    """Check if order was actually filled (not just submitted)."""
    if not order_result: return False
    status = order_result.get("status","")
    # Alpaca statuses
    if status in ("filled", "partially_filled"): return True
    # Binance statuses
    if status in ("FILLED", "PARTIALLY_FILLED"): return True
    # If no status field but order ID exists, assume submitted
    if order_result.get("id") or order_result.get("orderId"): return True
    return False

def place_order(symbol, side, qty, crypto=False, estimated_price=None):
    """Place order. Routes crypto to Binance if configured, otherwise Alpaca.
    Returns (order_result, actual_fill_price) tuple."""
    if crypto and USE_BINANCE:
        price = estimated_price or binance_fetch_price(symbol)
        usdt  = float(qty) * price if price else float(qty)
        result = binance_place_order(symbol, side, usdt)
        fill_price = get_actual_fill_price(result, side, price or 0, crypto=True)
        return result, fill_price
    result = alpaca_post("/v2/orders", {
        "symbol": symbol, "qty": str(qty), "side": side,
        "type": "market", "time_in_force": "gtc" if crypto else "day",
    })
    fill_price = get_actual_fill_price(result, side, estimated_price or 0, crypto=False)
    if result: log.info(f"ORDER {side.upper()} {qty} {symbol} fill~${fill_price:.4f}")
    return result, fill_price

# ── Bot cycle ─────────────────────────────────────────────────
def calc_unrealized_pnl(st):
    """Calculate total unrealized P&L across all open positions."""
    total = 0.0
    for sym, pos in st.positions.items():
        price = pos.get("highest_price", pos["entry_price"])
        total += (price - pos["entry_price"]) * pos["qty"]
    return total

def check_stop_losses(st, crypto=False):
    """Check all open positions for stop-loss, trailing stop, take-profit, max hold days.
    Also checks unrealized P&L against daily loss limit."""
    now = datetime.now()

    # FIX: Check TOTAL loss including unrealized open positions
    unrealized = calc_unrealized_pnl(st)
    total_loss = st.daily_pnl + unrealized
    if total_loss <= -MAX_DAILY_LOSS and not st.shutoff:
        log.warning(f"[{st.label}] Total loss (realized ${st.daily_pnl:.2f} + unrealized ${unrealized:.2f}) = ${total_loss:.2f} — shutting off")
        st.shutoff = True
        return

    market_just_opened = False
    if not crypto:
        et = datetime.now(ZoneInfo("America/New_York"))
        mins = et.hour * 60 + et.minute
        market_just_opened = (mins >= 570 and mins <= 575)

    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym, crypto=crypto)
        if not live:
            continue

        reason = None
        entry  = pos["entry_price"]
        high   = pos.get("highest_price", entry)
        pct_from_entry = ((live - entry) / entry) * 100

        # Trail stop — but ONLY after TRAIL_TRIGGER_PCT profit (avoid early stop-outs)
        pct_profit = ((live - entry) / entry) * 100
        trail_pct  = CRYPTO_TRAIL_PCT if crypto else TRAILING_STOP_PCT
        if live > high:
            pos["highest_price"] = live
            # Only start trailing after position is up TRAIL_TRIGGER_PCT
            if pct_profit >= TRAIL_TRIGGER_PCT:
                new_stop = live * (1 - trail_pct / 100)
                if new_stop > pos["stop_price"]:
                    old_stop = pos["stop_price"]
                    pos["stop_price"] = new_stop
                    log.info(f"[{st.label}] TRAIL {sym} stop raised ${old_stop:.4f} -> ${new_stop:.4f} (profit:{pct_profit:.1f}%)")
                    # Update the real exchange stop order too
                    if not crypto and sym in exchange_stops:
                        update_exchange_stop(sym, pos["qty"], new_stop)

        # Update days held
        if pos.get("entry_date"):
            entry_date = datetime.fromisoformat(pos["entry_date"]).date()
            pos["days_held"] = (now.date() - entry_date).days

        # 1. Gap-down protection at market open (stocks only)
        if market_just_opened and not crypto:
            gap_pct = ((live - entry) / entry) * 100
            if gap_pct <= -GAP_DOWN_PCT:
                reason = f"Gap-Down ({gap_pct:.1f}%)"

        # 2. Take-profit
        if reason is None and live >= pos.get("take_profit_price", entry * 1.05):
            reason = f"Take-Profit (+{pct_from_entry:.1f}%)"

        # 3. Trailing / hard stop-loss (crypto uses wider stop)
        if reason is None and live <= pos["stop_price"]:
            reason = f"Stop-Loss ({pct_from_entry:.1f}%)"

        # 4. Max hold days
        if reason is None and pos.get("days_held", 0) >= MAX_HOLD_DAYS:
            reason = f"Max Hold ({pos['days_held']} days)"

        if reason:
            pnl = (live - entry) * pos["qty"]
            emoji = "+" if pnl >= 0 else "-"
            log.info(f"[{st.label}] SELL {sym} @ ${live:.4f} | {reason} | P&L:{emoji}${abs(pnl):.2f}")
            place_order(sym, "sell", pos["qty"], crypto=crypto)
            del st.positions[sym]
            st.daily_pnl += pnl
            entry_ts = pos.get("entry_ts")
            hold_hours = None
            if entry_ts:
                hold_hours = round((now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1)
            st.trades.insert(0, {
                "symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": reason,
                "time": now.strftime("%H:%M:%S"),
                "hold_hours": hold_hours,
            })
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
    regime = market_regime["mode"]
    log.info(f"[{st.label}] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Regime: {regime}")

    check_stop_losses(st, crypto=crypto)
    if st.shutoff: return

    # Regime logic
    if crypto:
        c_regime = crypto_regime["mode"]
        if c_regime == "BEAR":
            log.info(f"[{st.label}] CRYPTO BEAR MODE — pausing new buys, protecting capital")
            # Don't buy anything new in crypto bear mode — just manage existing positions
            st.running = False
            return
    else:
        if regime == "BEAR":
            log.info(f"[{st.label}] BEAR MODE — scanning defensive/inverse tickers")
            watchlist = BEAR_TICKERS

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
        signal, e9, e21, rsi = get_signal(closes, volumes)
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi,
            "vol_ratio": vol_ratio, "closes": closes})

    results.sort(key=lambda x: {"BUY": 0, "HOLD": 1, "SELL": 2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[{st.label}] {buys} BUY / {len(results)} scanned")

    # Open BUY positions
    # ── RANK candidates by score before deciding ─────────────
    buy_candidates = [s for s in results if s["signal"] == "BUY"]
    for s in buy_candidates:
        s["score"] = score_signal(s["symbol"], s["price"], s["change"],
                                   s.get("rsi"), s.get("vol_ratio"),
                                   s.get("closes", [s["price"]]*21))
    buy_candidates.sort(key=lambda x: x["score"], reverse=True)

    pos_count = len(st.positions)
    for s in buy_candidates:
        if pos_count >= MAX_POSITIONS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if st.daily_spend >= MAX_DAILY_SPEND: break
        # Global position cap
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[{st.label}] Global position cap ({MAX_TOTAL_POSITIONS}) reached — no new buys")
            break
        # Loss streak pause
        if is_loss_streak_paused(): break
        # News sentiment skip check
        if not crypto and s["symbol"] in news_state.get("skip_list", {}):
            log.info(f"[{st.label}] SKIP {s['symbol']} — negative news")
            continue
        # Circuit breaker
        if not crypto and circuit_breaker["active"]:
            log.info(f"[{st.label}] CIRCUIT BREAKER active — no new buys")
            break
        # Portfolio exposure cap
        cap = CRYPTO_MAX_EXPOSURE if crypto else MAX_PORTFOLIO_EXPOSURE
        if total_exposure(st) >= cap: break
        # Risk-based sizing: risk 1% of portfolio, stop distance determines size
        pv         = float(account_info.get("portfolio_value", 10000))
        stop_pct   = CRYPTO_STOP_PCT if crypto else STOP_LOSS_PCT
        base_size  = risk_based_size(pv, stop_pct)
        # Apply vol adjustment and news boost on top
        adj_size   = vol_adjusted_size(base_size) * news_size_multiplier(s["symbol"])
        adj_size   = min(adj_size, MAX_TRADE_VALUE * 1.5)
        qty = max(0.0001, round(adj_size / s["price"], 6)) if crypto else max(1, int(adj_size / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_pct_use = CRYPTO_STOP_PCT if crypto else STOP_LOSS_PCT
        stop_price = s["price"] * (1 - stop_pct_use / 100)
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)
        # Score threshold — only trade high-quality setups
        sig_score = s.get("score", 0)
        if sig_score < MIN_SIGNAL_SCORE:
            log.info(f"[{st.label}] SKIP {s['symbol']} score:{sig_score}/10 below threshold {MIN_SIGNAL_SCORE}")
            continue

        # Choppy market filter — don't trade if SPY is ranging
        if not crypto and is_choppy_market():
            log.info(f"[{st.label}] SKIP — choppy market detected, waiting for trend")
            break

        # Max trades per day hard cap
        total_trades_today = sum(s2.trades_today for s2 in [state, crypto_state])
        if total_trades_today >= MAX_TRADES_PER_DAY:
            log.info(f"[{st.label}] Max trades per day ({MAX_TRADES_PER_DAY}) reached — no more buys today")
            break

        log.info(f"[{st.label}] ✅ BUY #{pos_count+1} score:{sig_score}/10 {s['symbol']} size:${adj_size:.0f}")
        log.info(f"[{st.label}] BUY {s['symbol']} @ ${s['price']:.4f} x{qty} stop:${stop_price:.4f} target:${take_profit_price:.4f} RSI:{s['rsi']:.1f}")
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=crypto, estimated_price=s["price"])
        if order:
            # Use actual fill price (with slippage) not just signal price
            actual_stop  = fill_price * (1 - stop_pct_use / 100)
            actual_tp    = fill_price * (1 + TAKE_PROFIT_PCT / 100)
            st.positions[s["symbol"]] = {
                "qty": qty,
                "entry_price": fill_price,
                "stop_price": actual_stop,
                "highest_price": fill_price,
                "take_profit_price": actual_tp,
                "entry_date": datetime.now().date().isoformat(),
                "days_held": 0,
            }
            # Place real exchange stop order on Alpaca (not just software stop)
            if not crypto:
                stop_order = place_stop_order_alpaca(s["symbol"], qty, round(actual_stop, 2))
                if stop_order and stop_order.get("id"):
                    exchange_stops[s["symbol"]] = stop_order["id"]
                    log.info(f"[{st.label}] Exchange stop placed for {s['symbol']} @ ${actual_stop:.2f}")
            st.daily_spend += trade_val
            st.trades_today += 1
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"),
                "entry_ts": datetime.now().isoformat()})
            st.positions[s["symbol"]]["entry_ts"] = datetime.now().isoformat()
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
            st.trades_today += 1
            entry_ts = pos.get("entry_ts")
            hold_hours = None
            if entry_ts:
                hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1)
            st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
                "price": s["price"], "pnl": pnl, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"),
                "hold_hours": hold_hours})
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

    # News summary for email
    news_summary = ""
    if news_state["scan_complete"]:
        skips = "\n".join(f"  🔴 {s}: {d['reason']}" for s,d in news_state["skip_list"].items()) or "  None"
        boosts = "\n".join(f"  🟢 {s}: {d['reason']}" for s,d in news_state["watch_list"].items()) or "  None"
        news_summary = f"\nMORNING NEWS SCAN\n{'─'*40}\nSkipped:{'\n'}{skips}\nPositive:{'\n'}{boosts}\n"

    # Near miss scorecards for daily summary
    stocks_near_miss = build_near_miss_section("US STOCKS", state.candidates, MIN_SIGNAL_SCORE)
    crypto_near_miss = build_near_miss_section("CRYPTO", crypto_state.candidates, MIN_SIGNAL_SCORE)
    near_miss_summary = (
        f"\nSIGNAL SCORECARD — Near Misses\n{'='*40}\n"
        f"Stocks and crypto that almost traded today.\n"
        f"Use this to tune MIN_SIGNAL_SCORE (currently {MIN_SIGNAL_SCORE}/10)\n\n"
        f"{stocks_near_miss}\n{crypto_near_miss}"
    )

    body = (f"AlphaBot Daily Summary\n{'='*40}\n"
            f"Date: {datetime.now().strftime('%A, %d %B %Y')}\n"
            f"Mode: {'LIVE' if IS_LIVE else 'Paper'} Trading\n"
            f"Portfolio: ${float(account_info.get('portfolio_value',0)):,.2f}\n\n"
            f"{section(state)}\n{section(smallcap_state)}\n{section(intraday_state)}\n{section(crypto_state)}\n{section(crypto_intraday_state)}\n"
            f"{news_summary}"
            f"{near_miss_summary}\n"
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
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #090b0e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; font-size: 14px; }}
  .header {{ background: #0d1117; border-bottom: 1px solid #1e2a1e; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }}
  .logo {{ display: flex; align-items: center; gap: 10px; }}
  .logo-icon {{ width: 32px; height: 32px; background: linear-gradient(135deg,#00ff88,#00aaff); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; }}
  .logo-text {{ font-weight: 700; font-size: 16px; }}
  .logo-sub {{ font-size: 10px; color: #444; letter-spacing: 1.5px; text-transform: uppercase; }}
  .badge {{ padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 700; }}
  .badge-paper {{ background: rgba(255,204,0,0.1); color: #ffcc00; border: 1px solid rgba(255,204,0,0.3); }}
  .badge-live  {{ background: rgba(255,68,102,0.1); color: #ff4466; border: 1px solid rgba(255,68,102,0.3); }}
  .refresh {{ font-size: 11px; color: #444; }}
  @media(max-width:480px) {{ .refresh {{ display:none; }} }}
  .container {{ padding: 24px; max-width: 1100px; margin: 0 auto; }}
  .grid4 {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; margin-bottom: 20px; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }}
  .card {{ background: rgba(255,255,255,0.025); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 18px 20px; }}
  .card-green {{ border-color: rgba(0,255,136,0.15); }}
  .card-blue  {{ border-color: rgba(0,170,255,0.15); }}
  .lbl {{ font-size: 10px; letter-spacing: 2px; color: #555; text-transform: uppercase; margin-bottom: 4px; }}
  .big {{ font-size: 22px; font-weight: 700; font-family: monospace; }}
  .green {{ color: #00ff88; }}
  .blue  {{ color: #00aaff; }}
  .red   {{ color: #ff4466; }}
  .gold  {{ color: #ffcc00; }}
  .grey  {{ color: #555; }}
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
  .tab {{ padding: 10px 16px; cursor: pointer; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #444; border-bottom: 2px solid transparent; text-decoration: none; }}
  .tab-stocks.active {{ color: #00aaff; border-bottom-color: #00aaff; }}
  .tab-crypto.active {{ color: #00ff88; border-bottom-color: #00ff88; }}
  .tab:hover {{ color: #e0e0e0; }}
  .empty {{ text-align: center; padding: 50px; color: #333; font-size: 15px; }}
  @media(max-width:768px) {{
    /* Container */
    .container {{ padding: 10px; }}

    /* Header — stack logo and info vertically */
    .header {{ padding: 10px 14px; flex-direction: column; align-items: flex-start; gap: 6px; }}
    .header-right {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; width: 100%; }}
    .header-pnl {{ display: flex; gap: 14px; }}
    .refresh {{ display: none; }}

    /* Regime cards — ALWAYS stack vertically on mobile */
    .regime-stats {{ flex-wrap: wrap; gap: 6px !important; }}
    .regime-desc {{ display: none; }}
    .regime-title {{ font-size: 20px !important; }}

    /* Stats grid — 2 cols */
    .grid4 {{ grid-template-columns: 1fr 1fr; gap: 10px; }}
    .grid2 {{ grid-template-columns: 1fr; gap: 10px; }}

    /* Cards */
    .card {{ padding: 12px 14px; }}
    .big {{ font-size: 18px; }}
    .section-title {{ font-size: 13px; margin-bottom: 10px; }}
    .lbl {{ font-size: 9px; letter-spacing: 1.5px; }}

    /* Bot status grid */
    .bot-status-grid {{ grid-template-columns: 1fr 1fr !important; gap: 6px !important; font-size: 11px !important; }}

    /* Tables — horizontal scroll */
    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0 -4px; }}
    table {{ min-width: 480px; font-size: 11px; }}
    th, td {{ padding: 6px 8px; white-space: nowrap; }}

    /* Tabs */
    .tab-bar {{ overflow-x: auto; -webkit-overflow-scrolling: touch; white-space: nowrap; display: flex; }}
    .tab {{ padding: 10px 12px; font-size: 10px; flex-shrink: 0; }}

    /* Scan panel tables */
    #scan-stocks, #scan-crypto {{ overflow-x: auto; }}
  }}

  @media(max-width:480px) {{
    .grid4 {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
    .big {{ font-size: 16px; }}
    .card {{ padding: 10px 12px; }}
    .container {{ padding: 8px; }}
    table {{ min-width: 420px; font-size: 10px; }}
    th, td {{ padding: 5px 7px; }}
  }}

  @media(max-width:380px) {{
    .grid4 {{ grid-template-columns: 1fr 1fr; }}
    .big {{ font-size: 14px; }}
    .card {{ padding: 8px 10px; }}
    table {{ min-width: 380px; font-size: 10px; }}
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
      <div class="logo-sub">Automated Day Trader · Railway</div>
    </div>
  </div>
  <div class="header-right">
    <div class="header-pnl">
      <div style="text-align:right">
        <div style="font-size:10px;color:#00aaff">US P&amp;L</div>
        <div style="font-family:monospace;font-size:13px;font-weight:700;color:{stocks_pnl_color}">{stocks_pnl}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:10px;color:#00ff88">Crypto P&amp;L</div>
        <div style="font-family:monospace;font-size:13px;font-weight:700;color:{crypto_pnl_color}">{crypto_pnl}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:10px;color:#444">Portfolio</div>
        <div style="font-family:monospace;font-size:13px;font-weight:700;color:#00aaff">{portfolio}</div>
      </div>
    </div>
    <div class="refresh">↻ {now}</div>
  </div>
</div>

<div class="container">

  <!-- Market Regime Banners -->
  <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:16px;width:100%">
    <!-- Stocks Regime -->
    <div style="padding:12px 14px;border-radius:12px;background:{regime_bg};border:1px solid {regime_border}">
      <div style="margin-bottom:8px">
        <div style="font-size:9px;letter-spacing:2px;color:#888;text-transform:uppercase;margin-bottom:2px">US Stocks</div>
        <div class="regime-title" style="font-size:16px;font-weight:700;color:{regime_color};line-height:1.2">{regime_icon} {regime}</div>
        <div class="regime-desc" style="font-size:10px;color:#555;margin-top:3px">{regime_desc}</div>
      </div>
      <div class="regime-stats" style="display:flex;gap:10px;font-size:11px;flex-wrap:wrap">
        <div><span style="color:#555">SPY </span><span style="font-family:monospace;font-weight:700">{spy_str}</span></div>
        <div><span style="color:#555">MA20 </span><span style="font-family:monospace;color:#777">{spy_ma_str}</span></div>
        <div><span style="color:#555">VIX </span><span style="font-family:monospace;color:{vix_regime_color}">{vix_str}</span></div>
        <div><span style="color:#555">Exp </span><span style="font-family:monospace">${exposure:.0f}</span></div>
      </div>
    </div>
    <!-- Crypto Regime -->
    <div style="padding:12px 14px;border-radius:12px;background:{c_regime_bg};border:1px solid {c_regime_border}">
      <div style="margin-bottom:8px">
        <div style="font-size:9px;letter-spacing:2px;color:#888;text-transform:uppercase;margin-bottom:2px">Crypto</div>
        <div class="regime-title" style="font-size:16px;font-weight:700;color:{c_regime_color};line-height:1.2">{c_regime_icon} {c_regime}</div>
        <div class="regime-desc" style="font-size:10px;color:#555;margin-top:3px">{c_regime_desc}</div>
      </div>
      <div class="regime-stats" style="display:flex;gap:10px;font-size:11px;flex-wrap:wrap">
        <div><span style="color:#555">BTC </span><span style="font-family:monospace;font-weight:700">{btc_str}</span></div>
        <div><span style="color:#555">MA20 </span><span style="font-family:monospace;color:#777">{btc_ma_str}</span></div>
        <div><span style="color:#555">Chg </span><span style="font-family:monospace;color:{btc_chg_color}">{btc_chg_str}</span></div>
        <div><span style="color:#555">Exp </span><span style="font-family:monospace">${crypto_exposure:.0f}</span></div>
      </div>
    </div>
  </div>

  <!-- Circuit Breaker Banner (only shown when active) -->
  {circuit_banner}

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
      <div class="lbl">Market</div>
      <div style="margin-top:6px;display:flex;align-items:center">
        <span class="dot {market_dot}"></span>
        <span style="font-weight:700;font-size:13px">{market_status}</span>
      </div>
    </div>
  </div>

  <!-- Bot status row -->
  <div class="grid2">
    <div class="card card-blue">
      <div class="section-title blue">📈 US Stocks Bot</div>
      <div class="bot-status-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">
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
      <div class="bot-status-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">
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

  <!-- Performance Analytics -->
  <div class="card" style="margin-bottom:20px;border-color:rgba(255,200,0,0.2)">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="section-title" style="color:#ffcc00;margin-bottom:0">📊 Performance Analytics</div>
      <div style="font-size:11px;color:#555">Updates every cycle</div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl" style="margin-bottom:4px">Win Rate</div>
        <div style="font-size:22px;font-weight:700;color:{trades_wr_color}">{win_rate}%</div>
        <div style="font-size:10px;color:#555">{wins}W / {losses}L</div>
      </div>
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl" style="margin-bottom:4px">Max Drawdown</div>
        <div style="font-size:22px;font-weight:700;color:{dd_color}">{max_dd}%</div>
        <div style="font-size:10px;color:#555">Peak: ${peak_pv}</div>
      </div>
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl" style="margin-bottom:4px">Profit Factor</div>
        <div style="font-size:22px;font-weight:700;color:{pf_color}">{profit_factor}</div>
        <div style="font-size:10px;color:#555">&gt;1.5 = good</div>
      </div>
      <div style="text-align:center;padding:10px;background:rgba(255,255,255,0.03);border-radius:8px">
        <div class="lbl" style="margin-bottom:4px">Sharpe Ratio</div>
        <div style="font-size:22px;font-weight:700;color:{sharpe_color}">{sharpe}</div>
        <div style="font-size:10px;color:#555">&gt;1.0 = good</div>
      </div>
    </div>
    <div style="margin-top:12px;display:flex;gap:20px;font-size:12px">
      <span>Loss streak: <b style="color:{streak_color}">{loss_streak}</b> / {streak_limit}</span>
      <span>Pause: <b style="color:#888">{pause_status}</b></span>
      <span>VIX level: <b style="color:{vix_color}">{vix_level}</b></span>
      <span>Size multiplier: <b style="color:#ffcc00">{size_mult}x</b></span>
      <span>Global positions: <b>{global_pos}</b> / {max_global}</span>
    </div>
  </div>

  <!-- Performance Analytics -->
  <div class="card" style="margin-bottom:20px;border-color:rgba(255,204,0,0.2)">
    <div class="section-title" style="color:#ffcc00">📊 Performance Analytics</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;font-size:13px">
      <div><div class="lbl">Win Rate</div><b style="font-size:18px;color:{trades_wr_color}">{win_rate}%</b></div>
      <div><div class="lbl">Profit Factor</div><b style="font-size:18px;color:{old_pf_color}">{profit_factor}</b></div>
      <div><div class="lbl">Sharpe Ratio</div><b style="font-size:18px;color:{sharpe_color}">{sharpe}</b></div>
      <div><div class="lbl">Max Drawdown</div><b style="font-size:18px;color:#ff4466">{max_dd}%</b></div>
    </div>
    <div style="margin-top:12px;font-size:11px;color:#555">
      Loss streak: {loss_streak}/{streak_limit} · {pause_status} · VIX: {vix_level} · Signal min: {signal_threshold}/10 · Global positions: {global_pos}/{max_global}
    </div>
  </div>

  <!-- Morning News Briefing -->
  <div class="card" style="margin-bottom:20px;border-color:rgba(170,136,255,0.2)">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="section-title" style="color:#aa88ff;margin-bottom:0">📰 Morning News Scan</div>
      <div style="font-size:11px;color:#555">{news_scan_time}</div>
    </div>
    {news_html}
  </div>

  <!-- Last Scan Section -->
  <div style="margin-bottom:20px">
    <div class="tab-bar" style="margin-bottom:0;border-bottom:none">
      <div class="tab tab-stocks active" onclick="showScan('stocks',this)" style="border-bottom:2px solid #00aaff;color:#00aaff">📈 US Stocks Last Scan</div>
      <div class="tab tab-crypto" onclick="showScan('crypto',this)">🪙 Crypto Last Scan</div>
    </div>
    <div class="card" style="border-radius:0 12px 12px 12px;margin-top:0">
      <div id="scan-stocks" class="scan-panel active">{stocks_scan_html}</div>
      <div id="scan-crypto" class="scan-panel">{crypto_scan_html}</div>
    </div>
  </div>

  <script>
  function showScan(tab, el) {{
    document.querySelectorAll('.scan-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-bar .tab').forEach(t => {{
      t.classList.remove('active');
      t.style.borderBottomColor = 'transparent';
      t.style.color = '#444';
    }});
    document.getElementById('scan-' + tab).classList.add('active');
    el.classList.add('active');
    el.style.borderBottomColor = tab === 'stocks' ? '#00aaff' : '#00ff88';
    el.style.color = tab === 'stocks' ? '#00aaff' : '#00ff88';
  }}
  </script>

  <div style="margin-top:24px;padding:14px;background:rgba(255,204,0,0.04);border:1px solid rgba(255,204,0,0.12);border-radius:8px;font-size:11px;color:#666;line-height:1.8">
    ⚠ <strong style="color:#ffcc00">Safety:</strong>
    Initial stop: {stop_loss}% &nbsp;|&nbsp;
    Trailing stop: {trailing_stop}% &nbsp;|&nbsp;
    Take-profit: {take_profit}% &nbsp;|&nbsp;
    Max hold: {max_hold_days} days &nbsp;|&nbsp;
    Gap-down sell: {gap_down}% &nbsp;|&nbsp;
    Daily loss limit: ${max_loss} &nbsp;|&nbsp;
    Max per trade: ${max_trade} &nbsp;|&nbsp;
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
    # Small cap state
    sc_dot     = ("dot-green" if smallcap_state.running else "dot-gold") if not smallcap_state.shutoff else "dot-red"
    sc_status  = "Shut Off" if smallcap_state.shutoff else ("Running" if smallcap_state.running else "Idle")

    # Performance analytics for dashboard
    all_completed = perf["all_trades"]
    perf_trades = len(all_completed)
    wins   = [t for t in all_completed if t["pnl"] > 0]
    losses = [t for t in all_completed if t["pnl"] <= 0]
    perf_wr    = round(len(wins) / perf_trades * 100) if perf_trades else 0
    wr_color   = "#00ff88" if perf_wr >= 50 else "#ff4466"
    pf         = calc_profit_factor()
    perf_pf    = f"{pf:.2f}" if pf != float("inf") else "∞"
    pf_color   = "#00ff88" if pf >= 1.5 else ("#ffcc00" if pf >= 1.0 else "#ff4466")
    sharpe     = calc_sharpe()
    perf_sharpe = f"{sharpe:.2f}" if sharpe else "—"
    sh_color   = "#00ff88" if (sharpe and sharpe >= 1.0) else "#ffcc00"
    perf_dd    = round(perf["max_drawdown"], 1)
    perf_avg_win  = round(sum(t["pnl"] for t in wins)  / len(wins),  2) if wins   else 0
    perf_avg_loss = round(abs(sum(t["pnl"] for t in losses) / len(losses)), 2) if losses else 0


    # Binance status
    if not USE_BINANCE:
        binance_status = "⚠ Alpaca (25 coins only)"
    elif BINANCE_USE_TESTNET:
        binance_status = f"🧪 Binance TESTNET ({len(CRYPTO_WATCHLIST)} coins)"
    else:
        binance_status = f"✅ Binance LIVE ({len(CRYPTO_WATCHLIST)} coins)"

    # Intraday state
    id_dot     = ("dot-green" if intraday_state.running else "dot-gold") if not intraday_state.shutoff else "dot-red"
    id_status  = "Shut Off" if intraday_state.shutoff else ("Running" if intraday_state.running else ("Window Closed" if not is_intraday_window() else "Idle"))
    cid_dot    = ("dot-green" if crypto_intraday_state.running else "dot-gold") if not crypto_intraday_state.shutoff else "dot-red"
    cid_status = "Shut Off" if crypto_intraday_state.shutoff else ("Running" if crypto_intraday_state.running else "Idle")

    # Performance analytics values
    all_t = perf["all_trades"]
    wins_count   = sum(1 for t in all_t if t["pnl"] > 0)
    losses_count = sum(1 for t in all_t if t["pnl"] <= 0)
    total_trades = len(all_t)
    win_rate     = int(wins_count / total_trades * 100) if total_trades else 0
    trades_wr_color = "green" if win_rate >= 55 else ("orange" if win_rate >= 45 else "red")

    max_dd    = round(perf["max_drawdown"], 1)
    dd_color  = "green" if max_dd < 5 else ("orange" if max_dd < 10 else "red")
    peak_pv   = f"{perf['peak_portfolio']:,.0f}"

    pf        = calc_profit_factor()
    profit_factor = f"{pf:.2f}" if pf != float("inf") else "∞"
    pf_color  = "green" if pf >= 1.5 else ("orange" if pf >= 1.0 else "red")

    sharpe_val = calc_sharpe()
    sharpe     = f"{sharpe_val:.2f}" if sharpe_val else "—"
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
            f'background:rgba(255,68,102,0.15);border:2px solid #ff4466;'
            f'display:flex;align-items:center;gap:16px">'
            f'<div style="font-size:28px">🚨</div>'
            f'<div>'
            f'<div style="font-size:16px;font-weight:700;color:#ff4466">CIRCUIT BREAKER ACTIVE — ALL NEW BUYS PAUSED</div>'
            f'<div style="font-size:12px;color:#888;margin-top:4px">Reason: {circuit_breaker["reason"]} · Triggered: {circuit_breaker["triggered_at"]}</div>'
            f'<div style="font-size:11px;color:#555;margin-top:2px">Existing positions still managed normally. Resets at next market open.</div>'
            f'</div></div>'
        )
    else:
        circuit_banner = ""

    regime       = market_regime["mode"]
    regime_color = "red" if regime == "BEAR" else "green"
    vix_str      = f"{market_regime['vix']:.1f}" if market_regime["vix"] else "N/A"
    spy_str      = f"${market_regime['spy_price']:.2f}" if market_regime["spy_price"] else "N/A"
    spy_ma_str   = f"${market_regime['spy_ma20']:.2f}" if market_regime["spy_ma20"] else "N/A"
    exposure     = total_exposure(state)
    c_regime       = crypto_regime["mode"]
    c_regime_color = "red" if c_regime == "BEAR" else "green"
    btc_str        = f"${crypto_regime['btc_price']:.0f}" if crypto_regime["btc_price"] else "N/A"
    btc_ma_str     = f"${crypto_regime['btc_ma20']:.0f}" if crypto_regime["btc_ma20"] else "N/A"
    btc_chg_str    = f"{crypto_regime['btc_change']:+.1f}%" if crypto_regime["btc_change"] is not None else "N/A"
    btc_chg_color  = "red" if crypto_regime["btc_change"] and crypto_regime["btc_change"] < -BTC_CRASH_PCT else "e0e0e0"
    crypto_exposure = total_exposure(crypto_state)

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
          <div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Type</th><th>Qty</th><th>Entry</th><th>Stop</th><th>P&amp;L</th></tr></thead>
          <tbody>{rows}</tbody></table></div></div>"""
    else:
        positions_html = ""

    # Recent trades
    # Completed trades only (SELL side with P&L) — last 10
    completed = (
        [dict(t, market="Stock")    for t in state.trades                 if t["side"] == "SELL" and t.get("pnl") is not None] +
        [dict(t, market="SmallCap") for t in smallcap_state.trades        if t["side"] == "SELL" and t.get("pnl") is not None] +
        [dict(t, market="Intraday") for t in intraday_state.trades        if t["side"] == "SELL" and t.get("pnl") is not None] +
        [dict(t, market="Crypto")   for t in crypto_state.trades          if t["side"] == "SELL" and t.get("pnl") is not None] +
        [dict(t, market="CryptoID") for t in crypto_intraday_state.trades if t["side"] == "SELL" and t.get("pnl") is not None]
    )
    completed.sort(key=lambda t: t["time"], reverse=True)

    # Open (BUY) trades for reference
    open_trades = (
        [dict(t, market="Stock")  for t in state.trades       if t["side"] == "BUY"] +
        [dict(t, market="Crypto") for t in crypto_state.trades if t["side"] == "BUY"]
    )

    if completed:
        wins   = sum(1 for t in completed if t["pnl"] > 0)
        losses = sum(1 for t in completed if t["pnl"] <= 0)
        total_pnl = sum(t["pnl"] for t in completed)
        win_rate  = int(wins / len(completed) * 100) if completed else 0
        avg_hold  = None
        hold_vals = [t["hold_hours"] for t in completed if t.get("hold_hours") is not None]
        if hold_vals:
            avg_hold = sum(hold_vals) / len(hold_vals)

        def hold_str(h):
            if h is None: return "—"
            if h < 1: return f"{int(h*60)}m"
            if h < 24: return f"{h:.1f}h"
            return f"{h/24:.1f}d"

        rows = ""
        for t in completed[:10]:
            pnl_color = "green" if t["pnl"] >= 0 else "red"
            pnl_sign  = "+" if t["pnl"] >= 0 else ""
            pnl_pct   = (t["pnl"] / (t["price"] * t["qty"])) * 100 if t["price"] and t["qty"] else 0
            result_icon = "✅" if t["pnl"] > 0 else "❌"
            market_c  = "blue" if t["market"] == "Stock" else "green"
            rows += f"""<tr>
              <td>{result_icon}</td>
              <td class="{market_c}" style="font-weight:700">{t["symbol"]}</td>
              <td style="color:#555;font-size:11px">{t["market"]}</td>
              <td style="color:#555">{t["time"]}</td>
              <td class="{pnl_color}" style="font-weight:700">{pnl_sign}${t["pnl"]:.2f}</td>
              <td class="{pnl_color}">{pnl_sign}{pnl_pct:.1f}%</td>
              <td style="color:#888">{hold_str(t.get("hold_hours"))}</td>
              <td style="color:#555;font-size:11px">{t.get("reason","—")}</td>
            </tr>"""

        avg_hold_str = hold_str(avg_hold) if avg_hold else "—"
        summary = (f'<div style="display:flex;gap:24px;margin-bottom:14px;font-size:13px">'
            f'<span class="green" style="font-weight:700">✅ {wins} wins</span>'
            f'<span class="red" style="font-weight:700">❌ {losses} losses</span>'
            f'<span style="color:#ffcc00">Win rate: {win_rate}%</span>'
            f'<span style="color:#e0e0e0">Total P&amp;L: <b class="{"green" if total_pnl>=0 else "red"}">{("+" if total_pnl>=0 else "")}${total_pnl:.2f}</b></span>'
            f'<span style="color:#555">Avg hold: {avg_hold_str}</span>'
            f'</div>')

        trades_html = (f'<div class="card" style="margin-bottom:20px">'
            f'<div class="section-title">Last {min(10,len(completed))} Completed Trades</div>'
            f'{summary}'
            f'<table><thead><tr><th></th><th>Symbol</th><th>Type</th><th>Closed</th><th>P&amp;L $</th><th>P&amp;L %</th><th>Hold Time</th><th>Exit Reason</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')
    else:
        trades_html = '<div class="card" style="margin-bottom:20px"><div class="empty">No completed trades yet — bot will show results here after first sell</div></div>'

    # Screener — BUY signals summary
    all_cands = (
        [dict(c, market="Stock") for c in state.candidates if c["signal"]=="BUY"] +
        [dict(c, market="Crypto") for c in crypto_state.candidates if c["signal"]=="BUY"]
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
          <div class="section-title">Current BUY Signals ({len(all_cands)})</div>
          <div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Type</th><th>Price</th><th>Chg%</th><th>Signal</th><th>RSI</th><th>Vol Ratio</th></tr></thead>
          <tbody>{rows}</tbody></table></div></div>"""
    else:
        screener_html = '<div class="card" style="margin-bottom:20px"><div class="empty">No BUY signals yet — bot will scan on next cycle</div></div>'

    # Full scan tables — US Stocks
    def build_scan_table(candidates, color, label):
        if not candidates:
            return f'<div class="empty">No scan data yet — waiting for first cycle</div>'
        rows = ""
        order = {"BUY": 0, "HOLD": 1, "SELL": 2}
        for c in sorted(candidates, key=lambda x: order.get(x["signal"], 1)):
            sig_class = {"BUY":"sig-buy","SELL":"sig-sell","HOLD":"sig-hold"}.get(c["signal"], "sig-hold")
            chg_c = "green" if c["change"] >= 0 else "red"
            rsi_val = f"{c['rsi']:.1f}" if c.get("rsi") else "—"
            rsi_c = "red" if c.get("rsi") and c["rsi"] > 70 else ("green" if c.get("rsi") and c["rsi"] < 35 else "")
            vol = f"{c['vol_ratio']:.2f}x" if c.get("vol_ratio") else "—"
            s9  = f"${c['sma9']:.4f}" if c.get("sma9") else "—"
            s21 = f"${c['sma21']:.4f}" if c.get("sma21") else "—"
            rows += f"""<tr>
              <td style="font-weight:700" class="{color}">{c['symbol']}</td>
              <td>${c['price']:.4f}</td>
              <td class="{chg_c}">{'+' if c['change']>=0 else ''}{c['change']:.2f}%</td>
              <td><span class="{sig_class}">{c['signal']}</span></td>
              <td class="{rsi_c}">{rsi_val}</td>
              <td style="color:#555">{s9}</td>
              <td style="color:#555">{s21}</td>
              <td style="color:#777">{vol}</td>
            </tr>"""
        count = len(candidates)
        buys  = sum(1 for c in candidates if c["signal"] == "BUY")
        holds = sum(1 for c in candidates if c["signal"] == "HOLD")
        sells = sum(1 for c in candidates if c["signal"] == "SELL")
        return f"""
          <div style="display:flex;gap:16px;margin-bottom:14px;font-size:12px">
            <span class="green" style="font-weight:700">{buys} BUY</span>
            <span style="color:#555">{holds} HOLD</span>
            <span class="red">{sells} SELL</span>
            <span style="color:#444;margin-left:auto">{count} total scanned</span>
          </div>
          <div style="overflow-x:auto">
          <table><thead><tr><th>Symbol</th><th>Price</th><th>Chg%</th><th>Signal</th><th>RSI</th><th>SMA 9</th><th>SMA 21</th><th>Vol Ratio</th></tr></thead>
          <tbody>{rows}</tbody></table></div>"""

    stocks_scan_html = build_scan_table(state.candidates, "blue", "US Stocks")
    crypto_scan_html = build_scan_table(crypto_state.candidates, "green", "Crypto")

    # News section
    if not news_state["scan_complete"]:
        if NEWS_API_KEY:
            news_html = '<div class="empty" style="padding:20px">Waiting for 9:00 AM ET morning scan...</div>'
        else:
            news_html = '<div style="padding:12px;background:rgba(255,204,0,0.05);border:1px solid rgba(255,204,0,0.2);border-radius:8px;font-size:12px;color:#888">⚠ Add <b style="color:#ffcc00">NEWS_API_KEY</b> and <b style="color:#ffcc00">CLAUDE_API_KEY</b> in Railway Variables to enable news scanning. Get free keys at <b>newsapi.org</b> and <b>console.anthropic.com</b></div>'
    else:
        skip_rows = "".join(
            f'<tr><td style="font-weight:700;color:#ff4466">{sym}</td><td><span class="sig-sell">SKIP</span></td><td style="color:#888;font-size:12px">{d["reason"]}</td></tr>'
            for sym, d in news_state["skip_list"].items()
        )
        boost_rows = "".join(
            f'<tr><td style="font-weight:700;color:#00ff88">{sym}</td><td><span class="sig-buy">POSITIVE</span></td><td style="color:#888;font-size:12px">{d["reason"]}</td></tr>'
            for sym, d in news_state["watch_list"].items()
        )
        all_rows = skip_rows + boost_rows
        if all_rows:
            news_html = f'''<table><thead><tr><th>Symbol</th><th>Sentiment</th><th>Reason</th></tr></thead><tbody>{all_rows}</tbody></table>
              <div style="margin-top:10px;font-size:11px;color:#555">{len(news_state["skip_list"])} skipped today · {len(news_state["watch_list"])} positive · resets at midnight</div>'''
        else:
            news_html = '<div style="color:#555;font-size:13px;padding:8px 0">All clear — no negative news found today. Trading normally on all stocks.</div>'

    news_scan_time = f"Last scan: {news_state['last_scan_time']} ET" if news_state["last_scan_time"] else "Scans at 9:00 AM ET daily"

    return DASHBOARD_HTML.format(
        now            = datetime.now().strftime("%H:%M:%S"),
        circuit_banner = circuit_banner,

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
        stocks_scan_html = stocks_scan_html,
        crypto_scan_html = crypto_scan_html,
        sc_dot       = sc_dot,
        sc_status    = sc_status,
        sc_cycle     = smallcap_state.cycle_count,
        sc_positions = len(smallcap_state.positions),
        sc_trades    = len(smallcap_state.trades),
        sc_last      = smallcap_state.last_cycle or "—",
        sc_pool_size = len(smallcap_pool["symbols"]),
        sc_refresh   = smallcap_pool.get("last_refresh", "Not yet"),
        binance_label= "Binance" if USE_BINANCE else "Alpaca",
        win_rate     = win_rate,
        trades_wr_color = trades_wr_color,
        wins         = wins_count,
        losses       = losses_count,
        max_dd       = max_dd,
        dd_color     = dd_color,
        peak_pv      = peak_pv,
        profit_factor= profit_factor,
        old_pf_color  = old_pf_color,
        sharpe       = sharpe,
        sharpe_color = sharpe_color,
        loss_streak  = loss_streak,
        streak_color = streak_color,
        streak_limit = LOSS_STREAK_LIMIT,
        pause_status = pause_status,
        vix_level    = vix_level,
        vix_color    = vix_color,
        size_mult    = size_mult,
        global_pos   = global_pos,
        max_global   = MAX_TOTAL_POSITIONS,
        signal_threshold = MIN_SIGNAL_SCORE,
        id_dot       = id_dot,
        id_status    = id_status,
        id_cycle     = intraday_state.cycle_count,
        id_positions = len(intraday_state.positions),
        id_trades    = len(intraday_state.trades),
        id_last      = intraday_state.last_cycle or "—",
        cid_dot      = cid_dot,
        cid_status   = cid_status,
        cid_cycle    = crypto_intraday_state.cycle_count,
        cid_positions= len(crypto_intraday_state.positions),
        cid_trades   = len(crypto_intraday_state.trades),
        cid_last     = crypto_intraday_state.last_cycle or "—",
        stop_loss      = STOP_LOSS_PCT,
        trailing_stop  = TRAILING_STOP_PCT,
        take_profit    = TAKE_PROFIT_PCT,
        max_hold_days  = MAX_HOLD_DAYS,
        gap_down       = GAP_DOWN_PCT,
        max_loss       = MAX_DAILY_LOSS,
        max_trade      = MAX_TRADE_VALUE,
        max_spend      = MAX_DAILY_SPEND,
        profit_target  = DAILY_PROFIT_TARGET,
        max_exposure   = MAX_PORTFOLIO_EXPOSURE,
        regime         = regime,
        regime_color   = regime_color,
        regime_bg      = "rgba(255,68,102,0.08)" if regime == "BEAR" else "rgba(0,255,136,0.05)",
        regime_border  = "rgba(255,68,102,0.25)" if regime == "BEAR" else "rgba(0,255,136,0.15)",
        regime_icon    = "🐻" if regime == "BEAR" else "🐂",
        regime_desc    = ("Buying SQQQ/UVXY/GLD · Pausing bull trades" if regime == "BEAR" else "Normal trading · Momentum stocks"),
        spy_str        = spy_str,
        spy_ma_str     = spy_ma_str,
        vix_str        = vix_str,
        vix_regime_color = "red" if market_regime["vix"] and market_regime["vix"] > VIX_FEAR_THRESHOLD else "#e0e0e0",
        exposure       = exposure,
        c_regime       = c_regime,
        c_regime_color = c_regime_color,
        c_regime_bg    = "rgba(255,68,102,0.08)" if c_regime == "BEAR" else "rgba(0,255,136,0.05)",
        c_regime_border= "rgba(255,68,102,0.25)" if c_regime == "BEAR" else "rgba(0,255,136,0.15)",
        c_regime_icon  = "🐻" if c_regime == "BEAR" else "🐂",
        c_regime_desc  = ("Pausing buys · Protecting capital" if c_regime == "BEAR" else f"Normal trading · {binance_status}"),
        btc_str        = btc_str,
        btc_ma_str     = btc_ma_str,
        btc_chg_str    = btc_chg_str,
        btc_chg_color  = btc_chg_color,
        crypto_exposure     = crypto_exposure,
        crypto_max_exposure = CRYPTO_MAX_EXPOSURE,
        news_html      = news_html,
        news_scan_time = news_scan_time,
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
def run_cycle_smallcap(watchlist, st):
    """Small cap specific trading cycle with tighter risk controls."""
    st.check_reset()
    if st.shutoff: return
    if not is_market_open():
        return
    if market_regime["mode"] == "BEAR":
        log.info("[SMALLCAP] BEAR MODE — pausing all small cap buys")
        return

    st.running    = True
    st.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[SMALLCAP] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Pool: {len(watchlist)} stocks")

    # Check stop losses with tighter stop
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym)
        if not live: continue
        now = datetime.now()
        # Update trailing stop
        if live > pos.get("highest_price", pos["entry_price"]):
            pos["highest_price"] = live
            new_stop = live * (1 - SMALLCAP_STOP_LOSS / 100)
            if new_stop > pos["stop_price"]:
                pos["stop_price"] = new_stop
        # Check exit conditions
        pct = ((live - pos["entry_price"]) / pos["entry_price"]) * 100
        reason = None
        if live <= pos["stop_price"]:   reason = f"Stop-Loss ({pct:.1f}%)"
        elif live >= pos.get("take_profit_price", pos["entry_price"] * 1.05): reason = f"Take-Profit (+{pct:.1f}%)"
        elif pos.get("days_held", 0) >= MAX_HOLD_DAYS: reason = f"Max Hold"
        if reason:
            pnl = (live - pos["entry_price"]) * pos["qty"]
            entry_ts = pos.get("entry_ts")
            hold_hours = round((now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None
            log.info(f"[SMALLCAP] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")
            place_order(sym, "sell", pos["qty"])
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": reason,
                "time": now.strftime("%H:%M:%S"), "hold_hours": hold_hours})
            if st.daily_pnl <= -MAX_DAILY_LOSS: st.shutoff = True; break
    if st.shutoff: st.running = False; return

    # Scan small caps
    results = []
    for sym in watchlist:
        if sym in news_state["skip_list"]: continue  # respect news skip list
        bars = fetch_bars(sym)
        if not bars: continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        if not (SMALLCAP_MIN_PRICE <= price <= SMALLCAP_MAX_PRICE): continue
        prev    = closes[-2] if len(closes) > 1 else price
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, e9, e21, rsi = get_signal_smallcap(closes, volumes)
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi,
            "vol_ratio": vol_ratio, "smallcap": True})

    results.sort(key=lambda x: {"BUY": 0, "HOLD": 1, "SELL": 2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[SMALLCAP] {buys} BUY signals from {len(results)} scanned")

    # Open BUY positions
    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= MAX_POSITIONS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= MAX_PORTFOLIO_EXPOSURE: break
        qty = max(1, int(SMALLCAP_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - SMALLCAP_STOP_LOSS / 100)
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)
        log.info(f"[SMALLCAP] BUY {s['symbol']} @ ${s['price']:.4f} x{qty} = ${trade_val:.0f} stop:${stop_price:.4f} RSI:{s['rsi']:.1f}")
        order = place_order(s["symbol"], "buy", qty)
        if order:
            now_str = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": s["price"],
                "stop_price": stop_price, "highest_price": s["price"],
                "take_profit_price": take_profit_price,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_str, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": s["price"], "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_str})
            pos_count += 1

    # Close SELL positions
    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        pnl = (s["price"] - pos["entry_price"]) * pos["qty"]
        entry_ts = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None
        log.info(f"[SMALLCAP] SELL {s['symbol']} @ ${s['price']:.4f} P&L:${pnl:+.2f}")
        place_order(s["symbol"], "sell", pos["qty"])
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        if st.daily_pnl >= DAILY_PROFIT_TARGET: st.shutoff = True; break
        if st.daily_pnl <= -MAX_DAILY_LOSS: st.shutoff = True; break

    st.running = False

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

    # Verify Binance connectivity if enabled
    if USE_BINANCE:
        log.info("[BINANCE] Testing connection...")
        test_bars = binance_fetch_bars("BTCUSDT", limit=5)
        if test_bars:
            log.info(f"[BINANCE] Connected — BTC latest: ${test_bars[-1]['c']:,.2f}")
        else:
            log.error("[BINANCE] Cannot fetch data — check BINANCE_KEY/BINANCE_SECRET and Railway IP whitelist")
            log.error("[BINANCE] Note: Binance may require IP whitelisting in API settings")

    # Verify Binance connection (if configured)
    if USE_BINANCE:
        mode = "TESTNET (virtual money)" if BINANCE_USE_TESTNET else ("LIVE (real money)" if IS_LIVE else "PAPER")
        log.info(f"[BINANCE] Mode: {mode}")
        log.info(f"[BINANCE] Endpoint: {BINANCE_BASE}")
        bal = binance_get_balance("USDT")
        if bal is not None:
            log.info(f"[BINANCE] Connected — USDT balance: ${bal:,.2f}")
            log.info(f"[BINANCE] Scanning {len(CRYPTO_WATCHLIST)} coins")
        else:
            log.warning("[BINANCE] Could not connect — check keys in Railway Variables")
    else:
        log.info("[BINANCE] Not configured — using Alpaca for crypto (25 coins)")
        log.info("[BINANCE] Add BINANCE_KEY + BINANCE_SECRET to Railway to enable")

    last_email_day = None
    cycle = 0

    while True:
        try:
            cycle += 1
            log.info(f"\n{'─'*50}")
            log.info(f"Main cycle {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # Refresh account info each cycle
            account_info = alpaca_get("/v2/account") or account_info

            # Update performance analytics
            if account_info:
                pv = float(account_info.get("portfolio_value", 0))
                update_drawdown(pv)
                # Track daily return for Sharpe
                last_pv = float(account_info.get("last_equity", pv))
                if last_pv > 0:
                    daily_ret = (pv - last_pv) / last_pv * 100
                    if daily_ret not in perf["sharpe_daily"]:
                        perf["sharpe_daily"].append(daily_ret)
                        perf["sharpe_daily"] = perf["sharpe_daily"][-30:]  # keep 30 days

            # ── PANIC KILL SWITCH ────────────────────────────
            # If portfolio drops 5% from starting value in one day → close everything
            if account_info:
                pv = float(account_info.get("portfolio_value", 0))
                last_pv = float(account_info.get("last_equity", pv))
                if last_pv > 0:
                    drawdown_pct = ((pv - last_pv) / last_pv) * 100
                    if drawdown_pct <= -5.0:
                        log.warning(f"PANIC KILL SWITCH: Portfolio down {drawdown_pct:.1f}% today (${pv:,.2f} vs ${last_pv:,.2f})")
                        log.warning("Closing ALL positions and stopping all bots!")
                        for sym, pos in list(state.positions.items()):
                            place_order(sym, "sell", pos["qty"], crypto=False, estimated_price=pos["entry_price"])
                            if sym in exchange_stops:
                                cancel_stop_order_alpaca(exchange_stops.pop(sym))
                        for sym, pos in list(crypto_state.positions.items()):
                            place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
                        state.positions.clear()
                        crypto_state.positions.clear()
                        state.shutoff = True
                        crypto_state.shutoff = True
                        smallcap_state.shutoff = True
                        intraday_state.shutoff = True
                        crypto_intraday_state.shutoff = True
                        circuit_breaker["active"] = True
                        circuit_breaker["reason"] = f"PANIC: Portfolio -{abs(drawdown_pct):.1f}% today"

            # Update market regimes
            if not IS_LIVE or is_market_open():
                update_market_regime()
                check_circuit_breaker()  # intraday crash detection
            update_crypto_regime()  # always runs — crypto is 24/7

            # Refresh Binance top-100 coins weekly (Monday morning)
            et_now = datetime.now(ZoneInfo("America/New_York"))
            if (USE_BINANCE and et_now.weekday() == 0
                    and et_now.hour == 9 and et_now.minute < 2):
                log.info("[BINANCE] Refreshing top coins list...")
                fresh = binance_get_top_coins(100)
                if fresh:
                    CRYPTO_WATCHLIST[:] = fresh
                    log.info(f"[BINANCE] Watchlist updated: {len(CRYPTO_WATCHLIST)} coins")

            # Refresh small cap pool if needed (runs in background thread)
            if should_refresh_smallcap() and is_market_open():
                log.info("[SMALLCAP] Starting pool refresh in background...")
                threading.Thread(target=refresh_smallcap_pool, daemon=True).start()

            # ── Swing trade bots (daily bars) ──
            run_cycle(US_WATCHLIST, state, crypto=False)
            run_cycle(CRYPTO_WATCHLIST, crypto_state, crypto=True)
            if smallcap_pool["symbols"]:
                run_cycle_smallcap(smallcap_pool["symbols"], smallcap_state)

            # ── Intraday bots (faster bars) ──
            run_intraday_cycle(US_WATCHLIST, intraday_state)
            run_crypto_intraday_cycle(CRYPTO_WATCHLIST, crypto_intraday_state)

            # Morning news scan at 9:00am ET (30 mins before open)
            et = datetime.now(ZoneInfo("America/New_York"))
            if (et.weekday() < 5
                    and et.hour == 9 and et.minute < 2
                    and news_state["last_scan_day"] != et.date()):
                log.info("Running morning news scan...")
                def morning_tasks():
                    check_macro_news()       # macro circuit breaker first
                    run_morning_news_scan()  # then individual stock sentiment
                threading.Thread(target=morning_tasks, daemon=True).start()

            # Daily email at 5pm ET
            if et.hour == 17 and et.minute < 2 and last_email_day != et.date():  # every day — crypto runs weekends too
                send_daily_summary()
                last_email_day = et.date()

            # Intraday bots run on their own faster sub-cycle
            # Run 6 intraday cycles per 1 swing cycle
            intraday_cycles = CYCLE_SECONDS // INTRADAY_CYCLE_SECONDS
            for _ in range(intraday_cycles):
                run_intraday_cycle(US_WATCHLIST, intraday_state)
                run_crypto_intraday_cycle(CRYPTO_WATCHLIST, crypto_intraday_state)
                time.sleep(INTRADAY_CYCLE_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped")
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
