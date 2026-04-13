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
import sqlite3
import re
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
DASH_USER      = os.environ.get("DASH_USER", "alpha")
DASH_PASS      = os.environ.get("DASH_PASS", "bot123")
KILL_PIN       = os.environ.get("KILL_PIN", "1234")  # PIN to confirm kill switch actions
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")  # Bot token from @BotFather
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "")   # Your chat ID
import hashlib as _hashlib
DASH_TOKEN     = _hashlib.md5(f"{DASH_USER}:{DASH_PASS}:alphabot".encode()).hexdigest()

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

# ── Persist ban state across restarts ────────────────────────
_BAN_FILE = "/tmp/binance_ban.txt"

def _load_ban_from_disk():
    """On startup, check if a ban was active before restart."""
    global _binance_ban_until
    try:
        with open(_BAN_FILE, "r") as f:
            saved = float(f.read().strip())
            if saved > time.time():
                _binance_ban_until = saved
                mins = int((saved - time.time()) / 60)
                print(f"[BINANCE] Loaded ban from disk — {mins} minutes remaining")
    except:
        pass  # no file or expired — start fresh

def _save_ban_to_disk(expiry):
    """Save ban expiry so next restart knows about it."""
    try:
        with open(_BAN_FILE, "w") as f:
            f.write(str(expiry))
    except:
        pass

_load_ban_from_disk()  # Run immediately on import

# ── Safety settings ───────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# ACCOUNT SIZE — set STARTING_BALANCE in Railway Variables, everything scales
# ─────────────────────────────────────────────────────────────
STARTING_BALANCE    = float(os.getenv("STARTING_BALANCE", "1000.0"))  # $ — set in Railway Variables

# All risk limits as % of portfolio — scale automatically forever
MAX_DAILY_LOSS_PCT      = 0.5   # 0.5%  → $5 on $1k  | $50 on $10k
MAX_DAILY_SPEND_PCT     = 50.0  # 50%   → $500 on $1k | $5,000 on $10k
MAX_EXPOSURE_PCT        = 30.0  # 30%   → $300 on $1k | $3,000 on $10k
DAILY_PROFIT_TARGET_PCT = 2.0   # 2%    → $20 on $1k  | $200 on $10k
MAX_TRADE_PCT           = 5.0   # 5%    → $50 on $1k  | $500 on $10k
CRYPTO_EXPOSURE_PCT     = 20.0  # 20%   → $200 on $1k | $2,000 on $10k
INTRADAY_TRADE_PCT      = 3.0   # 3%    → $30 on $1k  | $300 on $10k
CRYPTO_INTRADAY_PCT     = 2.0   # 2%    → $20 on $1k  | $200 on $10k
SMALLCAP_TRADE_PCT      = 2.5   # 2.5%  → $25 on $1k  | $250 on $10k

# Computed from STARTING_BALANCE — do not edit these directly
MAX_DAILY_LOSS         = STARTING_BALANCE * MAX_DAILY_LOSS_PCT / 100
MAX_DAILY_SPEND        = STARTING_BALANCE * MAX_DAILY_SPEND_PCT / 100
MAX_PORTFOLIO_EXPOSURE = STARTING_BALANCE * MAX_EXPOSURE_PCT / 100
DAILY_PROFIT_TARGET    = STARTING_BALANCE * DAILY_PROFIT_TARGET_PCT / 100
MAX_TRADE_VALUE        = STARTING_BALANCE * MAX_TRADE_PCT / 100

STOP_LOSS_PCT       = 5.0     # % swing stop (wide — give stocks room to breathe)
TRAILING_STOP_PCT   = 2.0     # % trail — only activates after TRAIL_TRIGGER_PCT profit
TRAIL_TRIGGER_PCT   = 3.0     # % profit required before trailing stop activates
TAKE_PROFIT_PCT     = 10.0    # % take profit — wider to match wider stop
MAX_HOLD_DAYS       = 5       # days max hold
GAP_DOWN_PCT        = 3.0     # % gap down at open triggers immediate sell
MAX_POSITIONS       = int(os.getenv("MAX_POSITIONS", "3"))        # per-bot position limit
MAX_TOTAL_POSITIONS = int(os.getenv("MAX_TOTAL_POSITIONS", "3"))   # GLOBAL cap — hard limit
MAX_TRADES_PER_DAY  = int(os.getenv("MAX_TRADES_PER_DAY", "10"))  # max trades per day across all bots
CYCLE_SECONDS          = 60
INTRADAY_CYCLE_SECONDS = 10

# ── Risk-based position sizing ────────────────────────────────
RISK_PER_TRADE_PCT  = 1.0     # % of portfolio to risk per trade
MAX_TRADE_VALUE     = STARTING_BALANCE * MAX_TRADE_PCT / 100  # auto-scaled

# ── Signal quality threshold ──────────────────────────────────
MIN_SIGNAL_SCORE    = int(os.getenv("MIN_SIGNAL_SCORE", "5"))  # 5 for paper trading — see action and collect data
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
BEAR_TICKERS          = ["SQQQ","UVXY","GLD","SLV","SPXS","SH","PSQ","SDOW","TLT","VXX"]  # buy in stock bear mode
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
CRYPTO_MAX_EXPOSURE   = STARTING_BALANCE * CRYPTO_EXPOSURE_PCT / 100

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
INTRADAY_MAX_TRADE      = STARTING_BALANCE * INTRADAY_TRADE_PCT / 100
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
CRYPTO_INTRADAY_MAX_TRADE = STARTING_BALANCE * CRYPTO_INTRADAY_PCT / 100
CRYPTO_INTRADAY_VOL_RATIO = 1.5

# ── Small cap settings ───────────────────────────────────────
SMALLCAP_MIN_PRICE    = 2.0    # $ minimum price
SMALLCAP_MAX_PRICE    = 20.0   # $ maximum price
SMALLCAP_POOL_SIZE    = 50     # number of small caps to maintain
SMALLCAP_STOP_LOSS    = 1.5    # % tighter stop-loss for small caps
SMALLCAP_MAX_TRADE    = STARTING_BALANCE * SMALLCAP_TRADE_PCT / 100
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
    "XTZ/USD","BAT/USD","CRV/USD","GRT/USD","MKR/USD","LINK/USD",
    "ALGO/USD","XLM/USD","SUSHI/USD","YFI/USD","ETH/BTC",
]

# Top 50 coins on Binance by volume — auto-refreshes weekly
# Kept at 50 to stay well within Binance rate limits
CRYPTO_WATCHLIST_BINANCE = [
    # Large caps — always trade these
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT","LINKUSDT","LTCUSDT",
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
        self.loss_cooldown   = {}      # { symbol: expiry_timestamp } — wash sale prevention

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

# ── Sector correlation map ───────────────────────────────────
# Prevents holding multiple stocks from the same sector simultaneously
SECTOR_MAP = {
    # Semiconductors
    "NVDA":"SEMI","AMD":"SEMI","INTC":"SEMI","QCOM":"SEMI","AVGO":"SEMI",
    "MU":"SEMI","AMAT":"SEMI","LRCX":"SEMI","KLAC":"SEMI","TXN":"SEMI","MRVL":"SEMI","SOXL":"SEMI",
    # Mega cap tech
    "AAPL":"BIGTECH","MSFT":"BIGTECH","GOOGL":"BIGTECH","AMZN":"BIGTECH",
    "META":"BIGTECH","NFLX":"BIGTECH","ORCL":"BIGTECH","ADBE":"BIGTECH",
    # EV
    "TSLA":"EV","RIVN":"EV","LCID":"EV","NIO":"EV","XPEV":"EV","LI":"EV",
    "BLNK":"EV","CHPT":"EV","WKHS":"EV","NKLA":"EV",
    # Crypto-adjacent
    "COIN":"CRYPTO_STOCK","MARA":"CRYPTO_STOCK","RIOT":"CRYPTO_STOCK","HOOD":"CRYPTO_STOCK",
    # Fintech
    "SQ":"FINTECH","PYPL":"FINTECH","SOFI":"FINTECH","AFRM":"FINTECH","UPST":"FINTECH","NU":"FINTECH",
    # AI/Cloud
    "PLTR":"AI","AI":"AI","PATH":"AI","SNOW":"AI","DDOG":"AI",
    "NET":"AI","CRWD":"AI","ZS":"AI","OKTA":"AI","MDB":"AI",
    # Energy
    "XOM":"ENERGY","CVX":"ENERGY","OXY":"ENERGY","SLB":"ENERGY","HAL":"ENERGY",
    "MPC":"ENERGY","VLO":"ENERGY","PSX":"ENERGY","DVN":"ENERGY","FANG":"ENERGY",
    # Biotech
    "MRNA":"BIOTECH","BNTX":"BIOTECH","NVAX":"BIOTECH","HIMS":"BIOTECH",
    "TDOC":"BIOTECH","ACCD":"BIOTECH","SDGR":"BIOTECH","RXRX":"BIOTECH","BEAM":"BIOTECH",
    # Crypto pairs — each is its own sector
    "BTC/USD":"BTC","ETH/USD":"ETH","BTCUSDT":"BTC","ETHUSDT":"ETH",
}
MAX_SECTOR_POSITIONS = 1  # only 1 stock per sector at a time

def sectors_held():
    """Return set of sectors currently held across all bots."""
    held = {}
    for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
        for sym in st.positions:
            sector = SECTOR_MAP.get(sym)
            if sector:
                held[sector] = held.get(sector, 0) + 1
    return held

# ── Performance analytics ─────────────────────────────────────
perf = {
    "all_trades":      [],       # every completed trade
    "peak_portfolio":  0.0,      # highest portfolio value seen
    "max_drawdown":    0.0,      # worst peak-to-trough %
    "sharpe_daily":    [],       # daily returns for Sharpe calculation
}

# ── Near-miss tracker — persistent follow-up system ─────────
near_miss_tracker = {}

def record_near_miss(symbol, score, price, crypto=False):
    today = datetime.now().date().isoformat()
    key   = f"{symbol}_{today}"
    if key not in near_miss_tracker:
        near_miss_tracker[key] = {
            "symbol": symbol, "date": today, "score": score,
            "threshold": MIN_SIGNAL_SCORE, "gap": round(MIN_SIGNAL_SCORE - score, 1),
            "price_at_miss": price, "prices_since": [],
            "triggered": False, "trigger_date": None, "trigger_price": None,
            "crypto": crypto, "recorded_at": datetime.now().isoformat(),
        }

def update_near_miss_prices():
    for key, nm in list(near_miss_tracker.items()):
        try:
            miss_date  = datetime.fromisoformat(nm["date"]).date()
            days_since = (datetime.now().date() - miss_date).days
            if days_since > 7 or len(nm["prices_since"]) >= 5:
                continue
            price = fetch_latest_price(nm["symbol"], crypto=nm["crypto"])
            if price:
                last = nm["prices_since"][-1] if nm["prices_since"] else None
                if price != last:
                    nm["prices_since"].append(round(price, 4))
        except:
            pass

def mark_near_miss_triggered(symbol):
    for key, nm in near_miss_tracker.items():
        if nm["symbol"] == symbol and not nm["triggered"]:
            nm["triggered"]     = True
            nm["trigger_date"]  = datetime.now().date().isoformat()
            nm["trigger_price"] = fetch_latest_price(symbol, crypto=nm["crypto"])
            log.info(f"[NEAR MISS] {symbol} finally triggered!")

def build_sparkline_html(price_at_miss, prices_since):
    if not prices_since:
        return '<span style="color:#444;font-size:11px">Tracking...</span>'
    all_prices = [price_at_miss] + prices_since
    min_p = min(all_prices)
    max_p = max(all_prices)
    rng   = max_p - min_p if max_p != min_p else 1
    W, H, P = 80, 28, 3
    def px(p): return P + ((p - min_p) / rng) * (W - P*2)
    def py(p): return H - P - ((p - min_p) / rng) * (H - P*2)
    points = " ".join(f"{px(p):.1f},{py(p):.1f}" for p in all_prices)
    final  = prices_since[-1]
    pct    = ((final - price_at_miss) / price_at_miss) * 100
    color  = "#00ff88" if pct >= 0 else "#ff4466"
    return (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<svg width="{W}" height="{H}" style="overflow:visible">'
        f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'<circle cx="{px(price_at_miss):.1f}" cy="{py(price_at_miss):.1f}" r="2" fill="#ffcc00"/>'
        f'<circle cx="{px(final):.1f}" cy="{py(final):.1f}" r="2" fill="{color}"/>'
        f"</svg>"
        f'<span style="color:{color};font-size:11px;font-weight:700">{pct:+.1f}%</span>'
        f"</div>"
    )

def simulate_near_miss_exit(entry_price, daily_bars):
    """
    Simulate what would have happened if we had taken a near-miss trade.
    Applies real stop loss, trailing stop and take profit rules day by day.
    Returns detailed simulation result.
    """
    if not daily_bars or not entry_price:
        return None

    stop_pct        = STOP_LOSS_PCT / 100        # 5%
    trail_pct       = TRAILING_STOP_PCT / 100    # 2%
    trail_trigger   = TRAIL_TRIGGER_PCT / 100    # 3% profit before trailing activates
    take_profit_pct = TAKE_PROFIT_PCT / 100      # 10%

    stop_price      = entry_price * (1 - stop_pct)
    take_profit     = entry_price * (1 + take_profit_pct)
    trail_high      = entry_price
    trail_active    = False
    trail_stop      = None

    exit_price  = None
    exit_day    = None
    exit_reason = None

    for day_idx, bar in enumerate(daily_bars[:5]):
        day_low  = bar.get("l", bar.get("c"))
        day_high = bar.get("h", bar.get("c"))
        day_close= bar.get("c")

        # Update trailing high
        if day_high > trail_high:
            trail_high = day_high

        # Check if trailing stop should activate
        profit_pct = (trail_high - entry_price) / entry_price
        if profit_pct >= trail_trigger:
            trail_active = True
            trail_stop   = trail_high * (1 - trail_pct)

        # Check hard stop loss first (worst case intraday)
        if day_low <= stop_price:
            exit_price  = stop_price
            exit_day    = day_idx + 1
            exit_reason = f"Stop loss hit day {day_idx+1}"
            break

        # Check trailing stop
        if trail_active and trail_stop and day_low <= trail_stop:
            exit_price  = trail_stop
            exit_day    = day_idx + 1
            exit_reason = f"Trailing stop hit day {day_idx+1} (locked in after +{profit_pct*100:.1f}%)"
            break

        # Check take profit
        if day_high >= take_profit:
            exit_price  = take_profit
            exit_day    = day_idx + 1
            exit_reason = f"Take profit hit day {day_idx+1} 🎯"
            break

        # Update trailing stop each day
        if trail_active:
            trail_stop = max(trail_stop, day_close * (1 - trail_pct))

    # If still open after 5 days — exit at day 5 close
    if exit_price is None and daily_bars:
        last_bar    = daily_bars[min(4, len(daily_bars)-1)]
        exit_price  = last_bar.get("c", entry_price)
        exit_day    = min(5, len(daily_bars))
        exit_reason = f"Max hold reached — exited at day {exit_day} close"

    if exit_price is None:
        return None

    pnl_pct  = ((exit_price - entry_price) / entry_price) * 100
    trade_val = 400  # default crypto trade size
    pnl_usd  = (pnl_pct / 100) * trade_val

    return {
        "entry_price":  entry_price,
        "exit_price":   round(exit_price, 6),
        "exit_day":     exit_day,
        "exit_reason":  exit_reason,
        "pnl_pct":      round(pnl_pct, 2),
        "pnl_usd":      round(pnl_usd, 2),
        "profitable":   pnl_pct > 0,
        "trail_active": trail_active,
        "max_profit_pct": round(((trail_high - entry_price) / entry_price) * 100, 2),
    }


def fetch_near_miss_ohlc(symbol, from_date, days=5, crypto=False):
    """Fetch daily OHLC bars for a symbol starting from a specific date."""
    try:
        from_dt = datetime.fromisoformat(from_date)
        end_dt  = from_dt + timedelta(days=days + 3)  # extra buffer for weekends

        if crypto and USE_BINANCE:
            if time.time() < (_binance_ban_until + 300):
                return []
            start_ts = int(from_dt.timestamp() * 1000)
            end_ts   = int(end_dt.timestamp() * 1000)
            data = binance_get("/api/v3/klines", {
                "symbol": symbol, "interval": "1d",
                "startTime": start_ts, "endTime": end_ts, "limit": days + 3
            })
            if not data:
                return []
            return [{"o": float(k[1]), "h": float(k[2]),
                     "l": float(k[3]), "c": float(k[4])} for k in data]
        else:
            # Alpaca stocks
            start_str = from_dt.strftime("%Y-%m-%dT00:00:00Z")
            end_str   = end_dt.strftime("%Y-%m-%dT00:00:00Z")
            resp = alpaca_get(f"/v2/stocks/{symbol}/bars?timeframe=1Day&start={start_str}&end={end_str}&limit={days+3}&feed=iex")
            if resp and resp.get("bars"):
                return [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in resp["bars"]]
        return []
    except Exception as e:
        log.debug(f"[NEAR MISS SIM] Failed to fetch OHLC for {symbol}: {e}")
        return []


def run_near_miss_simulations():
    """
    Run exit simulations on all near-misses that have 3+ days of history.
    Updates near_miss_tracker with simulation results.
    """
    updated = 0
    for key, nm in near_miss_tracker.items():
        # Only simulate once we have 3+ days of follow-up
        if len(nm.get("prices_since", [])) < 3:
            continue
        # Skip if already simulated
        if nm.get("simulation"):
            continue
        try:
            bars = fetch_near_miss_ohlc(
                nm["symbol"], nm["date"],
                days=5, crypto=nm.get("crypto", False)
            )
            if bars and len(bars) >= 2:
                sim = simulate_near_miss_exit(nm["price_at_miss"], bars)
                if sim:
                    nm["simulation"] = sim
                    updated += 1
        except Exception as e:
            log.debug(f"[NEAR MISS SIM] {nm['symbol']}: {e}")
    if updated:
        log.info(f"[NEAR MISS SIM] Ran simulations on {updated} near-misses")


def generate_weekly_near_miss_report():
    misses = [m for m in near_miss_tracker.values() if len(m["prices_since"]) >= 3]
    if len(misses) < 3:
        return "Not enough data yet — needs at least 3 near-misses with 3+ days of follow-up."
    winners  = []
    losers   = []
    for m in misses:
        pct = ((m["prices_since"][-1] - m["price_at_miss"]) / m["price_at_miss"]) * 100
        m["pct_move"] = round(pct, 2)
        if pct > 2:  winners.append(m)
        elif pct < -2: losers.append(m)
    triggered = [m for m in misses if m["triggered"]]
    win_rate  = len(winners) / len(misses) * 100 if misses else 0
    lines = []
    for m in misses[:20]:
        prices_str = " → ".join([f"${p:.4f}" for p in [m["price_at_miss"]] + m["prices_since"]])
        pct    = m.get("pct_move", 0)
        outcome = "UP" if pct > 2 else ("DOWN" if pct < -2 else "FLAT")
        trig   = "triggered" if m["triggered"] else "never triggered"
        lines.append(f"{m['symbol']}: score={m['score']}/{m['threshold']} | {prices_str} | {pct:+.1f}% {outcome} | {trig}")
    data_summary = "\n".join(lines)
    prompt = (
        "You are a quant analyst reviewing near-miss data for an automated trading bot. "
        f"Threshold: {MIN_SIGNAL_SCORE}/11. Near-misses scored just below and were NOT traded. "
        f"Data: {data_summary} "
        f"Stats: {len(misses)} tracked, {len(winners)} went UP, {len(losers)} went DOWN, {len(triggered)} triggered. "
        "Provide: 1) Verdict: threshold TOO TIGHT or ABOUT RIGHT? "
        "2) Recommendation: raise/lower/keep MIN_SIGNAL_SCORE? "
        "3) Top 3 missed opportunities 4) Top 3 good misses 5) One key insight. Be concise."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-5", "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if r.ok:
            return r.json()["content"][0]["text"]
        return f"Claude unavailable: {r.status_code}"
    except Exception as e:
        return f"Claude error: {e}"


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
        record_api_success()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path}: {e}")
        record_api_failure("alpaca")
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

# Binance balance cache — avoids calling every cycle
_binance_balance_cache = {"value": 0.0, "ts": 0}

# ── API health tracking ───────────────────────────────────────
api_health = {
    "alpaca_fails":  0,    # consecutive Alpaca failures
    "data_fails":    0,    # consecutive data fetch failures
    "last_success":  None, # timestamp of last successful API call
    "max_fails":     5,    # kill switch if this many consecutive failures
}

def record_api_success():
    api_health["alpaca_fails"] = 0
    api_health["data_fails"]   = 0
    api_health["last_success"] = datetime.now().isoformat()

def record_api_failure(source="alpaca"):
    key = f"{source}_fails"
    api_health[key] = api_health.get(key, 0) + 1
    total_fails = api_health["alpaca_fails"] + api_health["data_fails"]
    if total_fails >= api_health["max_fails"] and not kill_switch["active"]:
        kill_switch["active"]       = True
        kill_switch["reason"]       = f"API kill: {total_fails} consecutive failures ({source})"
        kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
        for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
            st.shutoff = True
        log.error(f"[API KILL] {total_fails} consecutive API failures — all bots stopped to prevent blind trading")

# ── Telegram Notifications ───────────────────────────────────
_last_tg_msg = {}  # rate limit — don't spam same message

def tg(message, category="info", force=False):
    """Send a Telegram notification. Rate limited per category."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        # Rate limit — same category max once per 5 minutes unless forced
        now = time.time()
        if not force and category in _last_tg_msg:
            if now - _last_tg_msg[category] < 300:
                return
        _last_tg_msg[category] = now

        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        if not resp.ok:
            log.debug(f"[TG] Failed: {resp.status_code}")
    except Exception as e:
        log.debug(f"[TG] Error: {e}")

def tg_critical(message):
    """Send critical alert — always delivered, no rate limit."""
    tg(message, category=f"critical_{message[:20]}", force=True)

def tg_trade_buy(symbol, price, score, market="stock"):
    """Notify on BUY order placed."""
    emoji = "🟢" if market == "stock" else "💎"
    now   = datetime.now().strftime("%H:%M:%S")
    msg   = (f"{emoji} <b>BUY — {symbol}</b>"
             f"\nPrice: <code>${price:.4f}</code>"
             f"\nScore: <code>{score:.1f}/11</code>"
             f"\nMarket: {market.upper()}"
             f"\nTime: {now} Paris")
    tg(msg, category=f"buy_{symbol}", force=True)

def tg_trade_sell(symbol, price, pnl, hold_hours, reason, market="stock"):
    """Notify on SELL order placed."""
    emoji = "✅" if pnl >= 0 else "❌"
    sign  = "+" if pnl >= 0 else ""
    now   = datetime.now().strftime("%H:%M:%S")
    msg   = (f"{emoji} <b>SELL — {symbol}</b>"
             f"\nPrice: <code>${price:.4f}</code>"
             f"\nP&L: <code>{sign}${pnl:.2f}</code>"
             f"\nHold: {hold_hours:.1f}h"
             f"\nReason: {reason}"
             f"\nTime: {now} Paris")
    tg(msg, category=f"sell_{symbol}", force=True)

def tg_hot_miss(symbol, score, skip_reason, price):
    """Notify on high-scoring near-miss blocked by limits."""
    msg = (f"🔥 <b>HOT MISS — {symbol}</b>"
           f"\nScore: <code>{score:.1f}/11</code> — high quality!"
           f"\nBlocked by: <b>{skip_reason}</b>"
           f"\nPrice: <code>${price:.4f}</code>"
           f"\nConsider raising limits to capture these")
    tg(msg, category=f"hotmiss_{symbol}", force=True)


# ── Trading Intelligence Database ────────────────────────────
DB_PATH = "/home/alphabot/app/alphabot.db"

def init_db():
    """Create database tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # All completed trades with full signal context
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        side        TEXT NOT NULL,
        qty         REAL,
        price       REAL,
        pnl         REAL,
        score       REAL,
        rsi         REAL,
        vol_ratio   REAL,
        hold_hours  REAL,
        reason      TEXT,
        signal_breakdown TEXT,
        market      TEXT,
        date        TEXT,
        time        TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    )""")

    # Daily and weekly reports stored for archive
    c.execute("""CREATE TABLE IF NOT EXISTS reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        type        TEXT NOT NULL,
        date        TEXT NOT NULL,
        subject     TEXT,
        body_html   TEXT,
        body_text   TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    )""")

    # Near-miss history with follow-up prices
    c.execute("""CREATE TABLE IF NOT EXISTS near_misses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        date            TEXT NOT NULL,
        score           REAL,
        threshold       REAL,
        gap             REAL,
        price_at_miss   REAL,
        prices_since    TEXT,
        pct_move        REAL,
        triggered       INTEGER DEFAULT 0,
        trigger_date    TEXT,
        trigger_price   REAL,
        crypto          INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now'))
    )""")

    # Stock leaderboard cache (rebuilt daily)
    c.execute("""CREATE TABLE IF NOT EXISTS stock_stats (
        symbol          TEXT PRIMARY KEY,
        total_trades    INTEGER DEFAULT 0,
        wins            INTEGER DEFAULT 0,
        losses          INTEGER DEFAULT 0,
        total_pnl       REAL DEFAULT 0,
        best_trade      REAL DEFAULT 0,
        worst_trade     REAL DEFAULT 0,
        avg_score       REAL DEFAULT 0,
        near_miss_count INTEGER DEFAULT 0,
        last_traded     TEXT,
        first_traded    TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""")

    conn.commit()
    conn.close()
    return True

def db_record_trade(symbol, side, qty, price, pnl, score, rsi, vol_ratio,
                    hold_hours, reason, breakdown, market="stock"):
    """Save a completed trade to the database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.now()
        c.execute("""INSERT INTO trades
            (symbol, side, qty, price, pnl, score, rsi, vol_ratio,
             hold_hours, reason, signal_breakdown, market, date, time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, side, qty, price, pnl, score, rsi, vol_ratio,
             hold_hours, reason, breakdown, market,
             now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")))
        conn.commit()

        # Update stock stats
        c.execute("SELECT * FROM stock_stats WHERE symbol=?", (symbol,))
        row = c.fetchone()
        today = now.strftime("%Y-%m-%d")
        if row:
            wins   = row[2] + (1 if pnl and pnl > 0 else 0)
            losses = row[3] + (1 if pnl and pnl < 0 else 0)
            total  = row[4] + (pnl or 0)
            best   = max(row[5], pnl or 0)
            worst  = min(row[6], pnl or 0)
            trades = row[1] + 1
            avg_sc = ((row[7] * row[1]) + (score or 0)) / trades if trades > 0 else 0
            c.execute("""UPDATE stock_stats SET
                total_trades=?, wins=?, losses=?, total_pnl=?,
                best_trade=?, worst_trade=?, avg_score=?,
                last_traded=?, updated_at=datetime('now')
                WHERE symbol=?""",
                (trades, wins, losses, round(total,2), round(best,2),
                 round(worst,2), round(avg_sc,2), today, symbol))
        else:
            c.execute("""INSERT INTO stock_stats
                (symbol, total_trades, wins, losses, total_pnl,
                 best_trade, worst_trade, avg_score, last_traded, first_traded)
                VALUES (?,1,?,?,?,?,?,?,?,?)""",
                (symbol,
                 1 if pnl and pnl > 0 else 0,
                 1 if pnl and pnl < 0 else 0,
                 round(pnl or 0, 2),
                 round(pnl or 0, 2),
                 round(pnl or 0, 2),
                 round(score or 0, 2),
                 today, today))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record trade: {e}")

def db_record_report(rtype, subject, body_html, body_text=""):
    """Save a daily or weekly report to the database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("""INSERT INTO reports (type, date, subject, body_html, body_text)
                     VALUES (?,?,?,?,?)""",
                  (rtype, today, subject, body_html, body_text))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record report: {e}")

def db_record_near_miss(symbol, score, threshold, gap, price, crypto=False, skip_reason="SCORE"):
    """Save a near-miss to the database with skip reason."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Add skip_reason column if not exists
        try:
            c.execute("ALTER TABLE near_misses ADD COLUMN skip_reason TEXT DEFAULT 'SCORE'")
            conn.commit()
        except:
            pass  # Column already exists

        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT id FROM near_misses WHERE symbol=? AND date=?", (symbol, today))
        if not c.fetchone():
            c.execute("""INSERT INTO near_misses
                (symbol, date, score, threshold, gap, price_at_miss, crypto, skip_reason)
                VALUES (?,?,?,?,?,?,?,?)""",
                (symbol, today, score, threshold, round(gap,2), price,
                 1 if crypto else 0, skip_reason))
            conn.commit()
            c.execute("""INSERT INTO stock_stats (symbol, near_miss_count)
                         VALUES (?, 1)
                         ON CONFLICT(symbol) DO UPDATE SET
                         near_miss_count = near_miss_count + 1""", (symbol,))
            conn.commit()
        else:
            # Update skip reason if better reason found
            c.execute("UPDATE near_misses SET skip_reason=? WHERE symbol=? AND date=?",
                      (skip_reason, symbol, today))
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record near miss: {e}")

def db_search_symbol(symbol):
    """Search all history for a symbol — trades, near-misses, stats."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        sym = symbol.upper().strip()

        c.execute("SELECT * FROM trades WHERE symbol=? ORDER BY created_at DESC", (sym,))
        trades = c.fetchall()

        c.execute("SELECT * FROM near_misses WHERE symbol=? ORDER BY date DESC LIMIT 20", (sym,))
        misses = c.fetchall()

        c.execute("SELECT * FROM stock_stats WHERE symbol=?", (sym,))
        stats = c.fetchone()

        conn.close()
        return {"trades": trades, "near_misses": misses, "stats": stats}
    except Exception as e:
        log.warning(f"[DB] Search failed: {e}")
        return {"trades": [], "near_misses": [], "stats": None}

def db_get_leaderboard(limit=20, period_days=None):
    """Get stock leaderboard — all time or rolling period."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if period_days:
            since = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%d")
            c.execute("""SELECT symbol,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl),2) as total_pnl,
                ROUND(MAX(pnl),2) as best,
                ROUND(MIN(pnl),2) as worst,
                ROUND(AVG(score),1) as avg_score
                FROM trades WHERE side='SELL' AND date >= ?
                GROUP BY symbol ORDER BY total_pnl DESC LIMIT ?""",
                (since, limit))
        else:
            c.execute("""SELECT symbol, total_trades, wins, losses, total_pnl,
                best_trade, worst_trade, avg_score
                FROM stock_stats ORDER BY total_pnl DESC LIMIT ?""", (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.warning(f"[DB] Leaderboard failed: {e}")
        return []

def db_get_skip_reason_breakdown():
    """Get breakdown of why near-misses were skipped."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT skip_reason, COUNT(*) as count,
                     ROUND(AVG(score),2) as avg_score
                     FROM near_misses
                     GROUP BY skip_reason
                     ORDER BY count DESC""")
        rows = c.fetchall()
        conn.close()
        return rows
    except:
        return []

def db_get_reports(limit=30, rtype=None):
    """Get recent reports for archive."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if rtype:
            c.execute("SELECT id, type, date, subject FROM reports WHERE type=? ORDER BY date DESC LIMIT ?",
                      (rtype, limit))
        else:
            c.execute("SELECT id, type, date, subject FROM reports ORDER BY date DESC LIMIT ?",
                      (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        return []

def db_get_report_by_id(report_id):
    """Get full report content by ID."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM reports WHERE id=?", (report_id,))
        row = c.fetchone()
        conn.close()
        return row
    except:
        return None

# Initialise database on startup
try:
    init_db()
    log.info("[DB] Trading Intelligence Database ready")
except Exception as e:
    log.warning(f"[DB] Database init failed: {e}")


# ── Global kill switch ────────────────────────────────────────
kill_switch = {
    "active": False,
    "reason": "",
    "activated_at": None,
}

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

    # ── Ban check — skip ALL calls until 120s AFTER ban expires ──
    # 120s buffer prevents triggering a fresh ban as the old one expires
    now_ts = time.time()
    ban_clear_at = _binance_ban_until + 120
    if now_ts < ban_clear_at:
        remaining = int(_binance_ban_until - now_ts)
        if remaining > 0 and remaining % 60 < 2:
            log.warning(f"[BINANCE] Ban active — {remaining}s remaining, will resume at {datetime.fromtimestamp(ban_clear_at).strftime('%H:%M:%S')}")
        elif remaining <= 0:
            log.info(f"[BINANCE] Ban expired — waiting 120s safety buffer before resuming")
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
                _save_ban_to_disk(_binance_ban_until)
                log.warning(f"[BINANCE] Rate limited — banned for {retry_after}s. Will retry at {datetime.fromtimestamp(_binance_ban_until).strftime('%H:%M:%S')} Paris+2")
                tg(f"⚠️ <b>Binance Ban Detected</b>\nDuration: {retry_after}s\nRetry at: {datetime.fromtimestamp(_binance_ban_until).strftime('%H:%M')} Paris", category="binance_ban")
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
        "symbol":          symbol,
        "side":            side.upper(),
        "type":            "MARKET",
        "quantity":        str(qty),
        "newOrderRespType": "FULL",  # Request full response including fills[]
    })
    if result:
        # Extract real fill price from fills[] array
        fills = result.get("fills", [])
        if fills:
            total_qty   = sum(float(f["qty"]) for f in fills)
            total_value = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            real_fill   = total_value / total_qty if total_qty > 0 else price
            slip_pct    = ((real_fill - price) / price * 100)
            log.info(f"[BINANCE] ORDER {side.upper()} {qty} {symbol} | signal=${price:.4f} fill=${real_fill:.4f} slippage={slip_pct:+.3f}%")
            result["_real_fill_price"] = real_fill  # store for place_order() to use
        else:
            log.info(f"[BINANCE] ORDER {side.upper()} {qty} {symbol} @ ~${price:.4f} (no fill data)")
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
    if time.time() < (_binance_ban_until + 300):
        return CRYPTO_WATCHLIST_BINANCE  # return static list during ban
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
def fetch_bars_batch(symbols, limit=30):
    """Fetch daily bars for multiple US stocks in ONE API call.
    Alpaca supports up to 100 symbols per request — far more efficient than
    looping 100 individual calls (reduces ~100 API calls to 1-2).
    Returns dict: { symbol: [bars] }
    """
    if not symbols:
        return {}
    end   = datetime.utcnow()
    start = end - timedelta(days=60)
    s_str = start.strftime("%Y-%m-%d")
    e_str = end.strftime("%Y-%m-%d")
    results = {}
    # Alpaca allows max 100 symbols per batch request
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        syms_param = ",".join(chunk)
        try:
            url = (f"{DATA_BASE}/v2/stocks/bars"
                   f"?symbols={requests.utils.quote(syms_param, safe=',')}"
                   f"&timeframe=1Day&start={s_str}&end={e_str}"
                   f"&limit={limit}&feed=sip&adjustment=raw")
            r = requests.get(url, headers=HEADERS, timeout=30)
            if not r.ok:
                log.warning(f"[BATCH] Bars fetch failed: {r.status_code}")
                record_api_failure("data")
                continue
            record_api_success()
            data = r.json().get("bars", {})
            for sym, bars in data.items():
                if bars and len(bars) >= 15:
                    results[sym] = bars
        except Exception as e:
            log.warning(f"[BATCH] Error: {e}")
    log.info(f"[BATCH] Fetched bars for {len(results)}/{len(symbols)} symbols in {len(symbols)//chunk_size + 1} API call(s)")
    return results


def check_data_freshness(bars, max_age_hours=2):
    """Check if bar data is fresh enough to trade on.
    Returns (is_fresh, age_str) tuple.
    Stale data causes phantom wins in paper trading that become real losses live."""
    if not bars:
        return False, "no data"
    try:
        last_bar = bars[-1]
        bar_time_str = last_bar.get("t", "")
        if not bar_time_str:
            return True, "unknown"  # no timestamp — assume ok
        bar_time = datetime.fromisoformat(bar_time_str.replace("Z", "+00:00"))
        age = datetime.now(bar_time.tzinfo) - bar_time
        age_hours = age.total_seconds() / 3600
        age_str = f"{age_hours:.1f}h old"
        # Daily bars can be up to 24h old and still valid
        # Intraday bars should be fresh within max_age_hours
        if age_hours > max_age_hours:
            return False, age_str
        return True, age_str
    except:
        return True, "unknown"  # parse error — don't block trading


def fetch_bars(symbol, crypto=False):
    """Fetch daily OHLCV bars. Routes crypto to Binance if configured."""
    if crypto and USE_BINANCE:
        # Hard abort if banned or within 5 min buffer
        if time.time() < (_binance_ban_until + 300):
            return None
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
        # Hard stop — never call Binance during or within 120s after ban
        if time.time() < (_binance_ban_until + 120):
            return None
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

def fetch_intraday_bars_batch(symbols, timeframe="1Hour", limit=48):
    """Batch fetch intraday bars for multiple US stocks — same as fetch_bars_batch but for sub-daily timeframes."""
    if not symbols:
        return {}
    # For crypto batch calls via Binance, check ban first
    if USE_BINANCE and time.time() < (_binance_ban_until + 300):
        return {}
    results = {}
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        syms_param = ",".join(chunk)
        try:
            url = (f"{DATA_BASE}/v2/stocks/bars"
                   f"?symbols={requests.utils.quote(syms_param, safe=',')}"
                   f"&timeframe={timeframe}&limit={limit}&feed=sip&adjustment=raw")
            r = requests.get(url, headers=HEADERS, timeout=30)
            if not r.ok:
                log.warning(f"[INTRADAY BATCH] Failed: {r.status_code}")
                continue
            data = r.json().get("bars", {})
            for sym, bars in data.items():
                if bars and len(bars) >= 10:
                    results[sym] = bars
        except Exception as e:
            log.warning(f"[INTRADAY BATCH] Error: {e}")
    return results


def fetch_intraday_bars(symbol, timeframe="1Hour", limit=48, crypto=False):
    """Fetch sub-daily bars. Routes crypto to Binance if configured."""
    if crypto and USE_BINANCE:
        # Hard abort if banned or within 5 min buffer
        if time.time() < (_binance_ban_until + 300):
            return None
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

    # Batch fetch all intraday bars in one API call
    bars_batch = fetch_intraday_bars_batch(watchlist, timeframe=INTRADAY_TIMEFRAME, limit=INTRADAY_BARS)
    results = []
    for sym in watchlist:
        if sym in news_state.get("skip_list", {}): continue
        bars = bars_batch.get(sym)
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
        # GLOBAL cap — prevents swing + intraday bots exceeding combined limit
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[INTRADAY] Global position cap ({MAX_TOTAL_POSITIONS}) reached — no new buys")
            break
        # Global symbol lock — no two bots can hold same ticker
        if s["symbol"] in all_symbols_held():
            log.info(f"[INTRADAY] SKIP {s['symbol']} — already held by another bot")
            continue
        # Sector correlation cap
        sym_sector = SECTOR_MAP.get(s["symbol"])
        if sym_sector:
            if sectors_held().get(sym_sector, 0) >= MAX_SECTOR_POSITIONS:
                log.info(f"[INTRADAY] SKIP {s['symbol']} — sector {sym_sector} full")
                continue
        qty = max(1, int(INTRADAY_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - INTRADAY_STOP_LOSS / 100)
        tp_price   = s["price"] * (1 + INTRADAY_TAKE_PROFIT / 100)
        log.info(f"[INTRADAY] BUY {s['symbol']} @ ${s['price']:.2f} x{qty} "
                 f"stop:${stop_price:.2f} target:${tp_price:.2f} RSI:{s['rsi']:.1f}")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            actual_stop = fill_price * (1 - INTRADAY_STOP_LOSS / 100)
            actual_tp   = fill_price * (1 + INTRADAY_TAKE_PROFIT / 100)
            # Mandatory exchange stop — emergency exit if it fails
            stop_order = place_stop_order_alpaca(s["symbol"], qty, round(actual_stop, 2))
            if stop_order and stop_order.get("id"):
                exchange_stops[s["symbol"]] = stop_order["id"]
                log.info(f"[INTRADAY] Exchange stop placed for {s['symbol']} @ ${actual_stop:.2f}")
            else:
                log.error(f"[EMERGENCY] Intraday stop FAILED for {s['symbol']} — emergency exit")
                place_order(s["symbol"], "sell", qty, estimated_price=fill_price)
                continue
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": actual_stop, "highest_price": fill_price,
                "take_profit_price": actual_tp,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "[ID]Signal",
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
    # Ban check FIRST — before anything else touches Binance
    if USE_BINANCE and time.time() < _binance_ban_until:
        return  # silent skip during ban — no Binance calls at all
    if crypto_regime["mode"] == "BEAR":
        log.info("[CRYPTO_ID] Bear mode — skipping intraday buys")
        return

    st.running    = True
    st.last_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[CRYPTO_ID] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")

    check_intraday_positions(st, crypto=True)
    if st.shutoff: st.running = False; return

    # Limit to top 50 coins when using Binance — balances coverage vs rate limits
    # Full list runs fine on Alpaca crypto
    scan_list = watchlist[:50] if USE_BINANCE else watchlist
    results = []
    for sym in scan_list:
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
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=True, estimated_price=s["price"])
        if order:
            # Use slippage-adjusted fill price for all calculations
            actual_stop = fill_price * (1 - CRYPTO_INTRADAY_SL / 100)
            actual_tp   = fill_price * (1 + CRYPTO_INTRADAY_TP / 100)
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": actual_stop, "highest_price": fill_price,
                "take_profit_price": actual_tp,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "[ID]Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_ts})
            pos_count += 1

    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        pnl = (s["price"] - pos["entry_price"]) * pos["qty"]
        entry_ts   = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 2) if entry_ts else None
        order_sell, sell_price = place_order(s["symbol"], "sell", pos["qty"], crypto=True, estimated_price=s["price"])
        pnl = (sell_price - pos["entry_price"]) * pos["qty"]  # recalc with real exit price
        log.info(f"[CRYPTO_ID] SELL {s['symbol']} @ ${sell_price:.4f} P&L:${pnl:+.2f}")
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

    # Fetch real VIX — try direct feed first, fall back to VIXY ETF
    vix_bars = None
    try:
        from datetime import timezone
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        url   = f"/v2/stocks/VIX/bars?timeframe=1Day&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}&feed=iex"
        resp  = alpaca_get(url)
        if resp and resp.get("bars"):
            raw = resp["bars"]
            vix_bars = [{"c": b["c"], "h": b["h"], "l": b["l"], "o": b["o"]} for b in raw]
            log.info(f"[REGIME] Real VIX: {vix_bars[-1]['c']:.2f}")
        else:
            raise ValueError("No VIX bars returned")
    except Exception:
        # Fallback to VIXY ETF as VIX proxy — always available
        vix_bars = fetch_bars("VIXY")
        if vix_bars:
            log.info(f"[REGIME] VIX via VIXY proxy: {vix_bars[-1]['c']:.2f}")

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
        log.info(f"[REGIME] VIX ${vix_val:.2f} above threshold {VIX_FEAR_THRESHOLD} — fear signal")

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
    log.info(f"[REGIME] {new_mode} | SPY: {spy_p_str} MA20: {spy_m_str} | VIX: {vix_str_}")
    return new_mode

# ── Crypto regime detection ──────────────────────────────────
def update_crypto_regime():
    """Check BTC trend and volatility to determine crypto BULL or BEAR mode."""
    global crypto_regime

    # Skip regime update if banned or within 120s of ban expiry
    if USE_BINANCE and time.time() < (_binance_ban_until + 300):
        return crypto_regime["mode"]
    btc_symbol = "BTCUSDT" if USE_BINANCE else "BTC/USD"
    btc_bars = fetch_bars(btc_symbol, crypto=True)
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

def all_symbols_held():
    """Set of ALL symbols currently held across every bot — prevents duplicate positions."""
    held = set()
    for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
        held.update(st.positions.keys())
    return held

# Thread-safe lock for state modifications
import threading as _threading
_state_lock = _threading.Lock()

# ── Volatility-adjusted trade sizing ─────────────────────────
def equity_curve_size_factor():
    """Auto-reduce position size based on current drawdown from peak portfolio.
    If bot is losing, it trades smaller — protecting remaining capital.
    Returns a multiplier between 0.25 and 1.0."""
    if perf["peak_portfolio"] <= 0 or not account_info:
        return 1.0
    current_pv  = float(account_info.get("portfolio_value", perf["peak_portfolio"]))
    drawdown_pct = ((perf["peak_portfolio"] - current_pv) / perf["peak_portfolio"]) * 100
    if drawdown_pct <= 0:
        return 1.0    # at or above peak — full size
    if drawdown_pct >= 10:
        log.warning(f"[EQUITY CURVE] Drawdown {drawdown_pct:.1f}% — trading at 25% size")
        return 0.25   # 10%+ drawdown — quarter size
    if drawdown_pct >= 5:
        log.info(f"[EQUITY CURVE] Drawdown {drawdown_pct:.1f}% — trading at 50% size")
        return 0.5    # 5-10% drawdown — half size
    if drawdown_pct >= 2:
        log.info(f"[EQUITY CURVE] Drawdown {drawdown_pct:.1f}% — trading at 75% size")
        return 0.75   # 2-5% drawdown — 3/4 size
    return 1.0


def risk_based_size(portfolio_value, stop_pct, risk_pct=1.0):
    """Risk 1% of portfolio per trade. Stop distance determines position size."""
    risk_amount = portfolio_value * (risk_pct / 100)
    size = risk_amount / (stop_pct / 100)
    return min(size, MAX_TRADE_VALUE)

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

# Dynamic kill switch config
RAPID_LOSS_COUNT   = 3     # number of losses
RAPID_LOSS_MINUTES = 15    # within this many minutes
RAPID_LOSS_AMOUNT  = 30.0  # OR total loss > this $ in window

def record_trade_result(pnl, symbol):
    """Track loss streaks, rapid losses, and trigger pauses/kill if needed."""
    now_iso = datetime.now().isoformat()
    perf["all_trades"].append({
        "pnl": pnl, "symbol": symbol, "time": now_iso,
        "score": None,  # filled by caller if available
    })
    if pnl < 0:
        global_risk["loss_streak"] += 1
        if global_risk["loss_streak"] >= LOSS_STREAK_LIMIT:
            pause_until = datetime.now() + timedelta(seconds=LOSS_STREAK_PAUSE)
            global_risk["paused_until"] = pause_until
            log.warning(f"[RISK] {LOSS_STREAK_LIMIT} consecutive losses — pausing until {pause_until.strftime('%H:%M')}")
    else:
        global_risk["loss_streak"] = 0

    # Dynamic kill switch — X losses in Y minutes
    window_start = datetime.now() - timedelta(minutes=RAPID_LOSS_MINUTES)
    recent_losses = [
        t for t in perf["all_trades"]
        if t["pnl"] < 0
        and datetime.fromisoformat(t["time"]) > window_start
    ]
    recent_loss_total = sum(abs(t["pnl"]) for t in recent_losses)
    if len(recent_losses) >= RAPID_LOSS_COUNT or recent_loss_total >= RAPID_LOSS_AMOUNT:
        if not kill_switch["active"]:
            kill_switch["active"]       = True
            kill_switch["reason"]       = f"Dynamic kill: {len(recent_losses)} losses (${recent_loss_total:.2f}) in {RAPID_LOSS_MINUTES}min"
            kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                st.shutoff = True
            log.warning(f"[DYNAMIC KILL] {kill_switch['reason']} — all bots stopped!")

def record_trade_with_score(pnl, symbol, score=None, signal=None, rsi=None, vol_ratio=None, hold_hours=None):
    """Enhanced trade recording that logs signal quality vs outcome.
    This is the data that tells you where your real edge is."""
    record_trade_result(pnl, symbol)
    # Enrich the last trade record with signal context
    if perf["all_trades"]:
        perf["all_trades"][-1].update({
            "score":      score,
            "signal":     signal,
            "rsi":        rsi,
            "vol_ratio":  vol_ratio,
            "hold_hours": hold_hours,
            "outcome":    "WIN" if pnl > 0 else "LOSS",
        })

def analyse_edge():
    """Analyse signal score vs win rate to find your real edge.
    Run this to understand which setups actually work."""
    trades = [t for t in perf["all_trades"] if t.get("score") is not None and t.get("pnl") is not None]
    if len(trades) < 5:
        return "Not enough trades yet (need 5+)"

    # Group by score bucket
    buckets = {}
    for t in trades:
        bucket = f"{int(t['score'])}-{int(t['score'])+1}"
        if bucket not in buckets:
            buckets[bucket] = {"wins": 0, "losses": 0, "total_pnl": 0}
        if t["pnl"] > 0:
            buckets[bucket]["wins"] += 1
        else:
            buckets[bucket]["losses"] += 1
        buckets[bucket]["total_pnl"] += t["pnl"]

    lines = ["SIGNAL SCORE vs OUTCOME ANALYSIS", "=" * 40]
    for bucket in sorted(buckets.keys()):
        b = buckets[bucket]
        total = b["wins"] + b["losses"]
        win_rate = int(b["wins"] / total * 100) if total > 0 else 0
        lines.append(
            f"  Score {bucket}: {total} trades | "
            f"Win rate: {win_rate}% | "
            f"P&L: ${b['total_pnl']:+.2f} | "
            f"{'✅ EDGE' if win_rate >= 55 else '❌ NO EDGE'}"
        )

    # Best and worst performing scores
    best = max(buckets.items(), key=lambda x: x[1]["total_pnl"])
    worst = min(buckets.items(), key=lambda x: x[1]["total_pnl"])
    lines.append(f"  Best score bucket:  {best[0]} (${best[1]['total_pnl']:+.2f})")
    lines.append(f"  Worst score bucket: {worst[0]} (${worst[1]['total_pnl']:+.2f})")
    rec = "Raise MIN_SIGNAL_SCORE to " + best[0].split("-")[0] if best[0] != sorted(buckets.keys())[0] else "Keep current threshold"
    lines.append(f"  Recommendation: {rec}")
    lines.append("=" * 40)
    return "\n".join(lines)


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
def score_signal(sym, price, change, rsi, vol_ratio, closes, bars=None):
    """
    Score a BUY candidate 0-11. Trade if score >= MIN_SIGNAL_SCORE.
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
      ADX > 25 strong trend : +1.5  (kills false signals in choppy markets)
      ADX 20-25 building    : +0.5  (trend developing)
      ADX < 20 choppy       : -1.5  (avoid — likely whipsaw)
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

    # ADX trend strength filter — kills false signals in choppy markets
    # Grok recommendation: ADX > 25 confirms genuine trend before EMA cross
    if bars and len(bars) >= 16:
        adx = calc_adx(bars, period=14)
        if adx is not None:
            if adx >= 25:
                score += 1.5   # strong trend — EMA cross is reliable
            elif adx >= 20:
                score += 0.5   # trend developing — moderate confidence
            else:
                score -= 1.5   # choppy/ranging — EMA cross likely a whipsaw

    # News catalyst
    if sym in news_state.get("watch_list", {}): score += 1.5
    if sym in news_state.get("skip_list",   {}): score -= 5.0

    # Environment
    if is_choppy_market(): score -= 1.0

    return round(min(11.0, max(0.0, score)), 1)

# ── Signal breakdown — explains WHY a trade fired ────────────
def signal_breakdown(sym, price, change, rsi, vol_ratio, closes, score, crypto=False):
    """Returns a human-readable breakdown of every factor that went into the score."""
    lines = []

    # Header
    label = "CRYPTO" if crypto else "STOCK"
    lines.append(f"{'─'*52}")
    lines.append(f"  {label}: {sym}  |  Score: {score}/10  |  Price: ${price:.4f}")
    lines.append(f"{'─'*52}")

    # SMA crossover
    if len(closes) >= 22:
        s9  = sum(closes[-9:]) / 9
        s21 = sum(closes[-21:]) / 21
        p9  = sum(closes[-10:-1]) / 9
        p21 = sum(closes[-22:-1]) / 21
        crossed = p9 <= p21 and s9 > s21
        lines.append(f"  SMA Cross:    {'✅ YES — 9-day crossed above 21-day' if crossed else '❌ No crossover yet'}")
        lines.append(f"  SMA 9:        ${s9:.4f}  |  SMA 21: ${s21:.4f}")

    # RSI
    if rsi:
        if 50 <= rsi <= 65:
            rsi_note = "✅ Sweet spot (50-65) +1.0pt"
        elif 40 <= rsi < 50:
            rsi_note = "⚠ Building momentum (40-50) +0.5pt"
        elif rsi > 75:
            rsi_note = "🔴 Overbought (>75) -1.0pt"
        elif rsi > 70:
            rsi_note = "⚠ Getting hot (70-75)"
        else:
            rsi_note = "— Neutral zone"
        lines.append(f"  RSI:          {rsi:.1f}  {rsi_note}")

    # Volume
    if vol_ratio:
        if vol_ratio >= 2.0:
            vol_note = "✅ Strong conviction (2x+) +2.0pt"
        elif vol_ratio >= 1.5:
            vol_note = "✅ Good conviction (1.5x+) +1.0pt"
        elif vol_ratio >= 1.2:
            vol_note = "⚠ Mild confirmation (1.2x+) +0.5pt"
        else:
            vol_note = "❌ Below average — weak signal"
        lines.append(f"  Volume:       {vol_ratio:.2f}x avg  {vol_note}")

    # Breakout
    if len(closes) >= 20:
        breakout = is_breakout(closes, lookback=20)
        lines.append(f"  Breakout:     {'✅ YES — 20-bar high +2.0pt' if breakout else '❌ No breakout'}")

    # 5-day momentum
    if len(closes) >= 6 and closes[-6] > 0:
        m5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if m5d >= 3.0:
            mom_note = f"✅ Strong +{m5d:.1f}% +1.0pt"
        elif m5d >= 1.5:
            mom_note = f"⚠ Moderate +{m5d:.1f}% +0.5pt"
        else:
            mom_note = f"— Weak {m5d:+.1f}%"
        lines.append(f"  5d Momentum:  {mom_note}")

    # Relative strength (stocks only)
    if not crypto and len(closes) >= 6:
        rs = relative_strength_vs_spy(closes)
        lines.append(f"  vs SPY:       {'✅ Outperforming +1.5pt' if rs else '❌ Underperforming SPY'}")

    # MACD
    if len(closes) >= 35:
        mv, ms = calc_macd(closes)
        if mv is not None and ms is not None:
            macd_note = f"{'✅ Bullish (MACD > Signal) +1.0pt' if mv > ms else '❌ Bearish (MACD < Signal)'}"
            lines.append(f"  MACD:         {macd_note}")

    # News
    if sym in news_state.get("watch_list", {}):
        lines.append(f"  News:         ✅ Positive catalyst +1.5pt")
    elif sym in news_state.get("skip_list", {}):
        lines.append(f"  News:         🔴 NEGATIVE — skip flag -5.0pt")
    else:
        lines.append(f"  News:         — No news flag")

    # Market environment
    choppy = is_choppy_market()
    regime = crypto_regime["mode"] if crypto else market_regime["mode"]
    lines.append(f"  Market:       {'⚠ Choppy -1.0pt' if choppy else '✅ Trending'}")
    lines.append(f"  Regime:       {'🔴 BEAR' if regime == 'BEAR' else '✅ BULL'}")

    lines.append(f"{'─'*52}")
    return "\n".join(lines)


def sell_breakdown(sym, pos, exit_price, pnl, reason, hold_hours, crypto=False):
    """Returns a human-readable breakdown of why a position was closed."""
    entry  = pos.get("entry_price", 0)
    stop   = pos.get("stop_price", 0)
    target = pos.get("take_profit_price", 0)
    qty    = pos.get("qty", 0)
    pct    = ((exit_price - entry) / entry * 100) if entry > 0 else 0
    hold_str = f"{hold_hours:.1f}h" if hold_hours else "?"

    lines = [
        f"{'─'*52}",
        f"  SELL: {sym}  |  P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} ({pct:+.2f}%)",
        f"{'─'*52}",
        f"  Reason:       {reason}",
        f"  Entry:        ${entry:.4f}",
        f"  Exit:         ${exit_price:.4f}",
        f"  Stop was:     ${stop:.4f} ({((stop-entry)/entry*100):+.1f}%)",
        f"  Target was:   ${target:.4f} ({((target-entry)/entry*100):+.1f}%)",
        f"  Qty:          {qty}",
        f"  Hold time:    {hold_str}",
        f"  Result:       {'✅ WIN' if pnl >= 0 else '❌ LOSS'}",
        f"{'─'*52}",
    ]
    return "\n".join(lines)


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

def calc_adx(bars, period=14):
    """Average Directional Index — measures trend STRENGTH not direction.
    ADX > 25 = strong trend, good for EMA crossover trades.
    ADX < 20 = choppy/ranging market, avoid EMA crossover trades."""
    if not bars or len(bars) < period + 2:
        return None
    try:
        highs  = [b["h"] for b in bars]
        lows   = [b["l"] for b in bars]
        closes = [b["c"] for b in bars]
        tr_list, plus_dm, minus_dm = [], [], []
        for i in range(1, len(bars)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            pdm = max(highs[i] - highs[i-1], 0) if (highs[i] - highs[i-1]) > (lows[i-1] - lows[i]) else 0
            mdm = max(lows[i-1] - lows[i], 0) if (lows[i-1] - lows[i]) > (highs[i] - highs[i-1]) else 0
            tr_list.append(tr); plus_dm.append(pdm); minus_dm.append(mdm)
        def wilder_smooth(data, n):
            s = sum(data[:n])
            result = [s]
            for v in data[n:]:
                s = s - s/n + v
                result.append(s)
            return result
        atr   = wilder_smooth(tr_list, period)
        pdi_s = wilder_smooth(plus_dm, period)
        mdi_s = wilder_smooth(minus_dm, period)
        dx_list = []
        for i in range(len(atr)):
            if atr[i] == 0: continue
            pdi = 100 * pdi_s[i] / atr[i]
            mdi = 100 * mdi_s[i] / atr[i]
            dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
            dx_list.append(dx)
        if len(dx_list) < period:
            return None
        adx = sum(dx_list[-period:]) / period
        return round(adx, 1)
    except:
        return None


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
    """Extract actual fill price from order result.
    
    On live trading: uses real exchange fill price.
    On paper trading: always applies slippage model since paper fills are not realistic.
    This ensures paper trading P&L reflects real-world execution costs.
    """
    if not order_result or not estimated_price:
        return apply_slippage(estimated_price or 0, side, crypto)

    # Paper trading — Alpaca paper fills are optimistic (filled at signal price)
    # We ALWAYS apply slippage in paper mode to get realistic P&L
    if not IS_LIVE:
        return apply_slippage(estimated_price, side, crypto)

    # Live trading — use actual exchange fill price
    # Alpaca returns filled_avg_price
    filled = order_result.get("filled_avg_price")
    if filled:
        try:
            fp = float(filled)
            if fp > 0:
                log.info(f"[FILL] Real fill: ${fp:.4f} vs signal: ${estimated_price:.4f} (slippage: {((fp-estimated_price)/estimated_price*100):+.3f}%)")
                return fp
        except: pass

    # Binance real fills array
    fills = order_result.get("fills", [])
    if fills:
        total_qty   = sum(float(f["qty"]) for f in fills)
        total_value = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        if total_qty > 0:
            fp = total_value / total_qty
            log.info(f"[FILL] Binance fill: ${fp:.4f} vs signal: ${estimated_price:.4f}")
            return fp

    # Final fallback — apply slippage model
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
    
    Option B execution: for live trading, waits briefly then fetches the actual
    filled_avg_price from the exchange — not just the estimated price.
    For paper trading, applies slippage model to simulate realistic fills.
    
    Returns (order_result, actual_fill_price) tuple."""

    # ── Crypto via Binance ──
    if crypto and USE_BINANCE:
        price  = estimated_price or binance_fetch_price(symbol)
        usdt   = float(qty) * price if price else float(qty)
        result = binance_place_order(symbol, side, usdt)

        # Use real fill price if Binance returned fills[] (newOrderRespType=FULL)
        if result and "_real_fill_price" in result:
            real_fill = result["_real_fill_price"]
            return result, real_fill

        # Fallback to slippage model
        fill_price = get_actual_fill_price(result, side, price or 0, crypto=True)
        return result, fill_price

    # ── Stocks via Alpaca ──
    # Live trading: use limit orders with 0.5% tolerance for better fills
    # Paper trading: use market orders (slippage model handles simulation)
    if IS_LIVE and estimated_price and not crypto:
        # Check bid/ask spread before placing limit order
        # If spread > profit target, trade is mathematically not worth taking
        spread_pct = 0.0
        try:
            snap = alpaca_get(f"/v2/stocks/{symbol}/snapshot?feed=sip", base=DATA_BASE)
            if snap:
                bid = float(snap.get("latestQuote", {}).get("bp", 0) or 0)
                ask = float(snap.get("latestQuote", {}).get("ap", 0) or 0)
                if bid > 0 and ask > 0:
                    spread_pct = ((ask - bid) / bid) * 100
                    if spread_pct > 1.0:  # spread wider than 1% — skip trade
                        log.warning(f"[SPREAD] {symbol} spread too wide ({spread_pct:.2f}%) — skipping to avoid bad fill")
                        return None, estimated_price
                    log.info(f"[SPREAD] {symbol} bid:${bid:.2f} ask:${ask:.2f} spread:{spread_pct:.3f}% — OK")
        except Exception as e:
            log.debug(f"[SPREAD] Could not check spread for {symbol}: {e}")

        # Dynamic tolerance — stronger signal = willing to chase more
        # Also widens in high VIX environments where spreads are naturally wider
        vix_now     = global_risk.get("vix_level") or 20
        signal_score = getattr(place_order, "_last_score", 5)  # injected by caller if available

        if signal_score >= 9:
            base_tol = 0.010   # 1.0% — strong signal, chase it
        elif signal_score >= 7:
            base_tol = 0.006   # 0.6% — good signal
        else:
            base_tol = 0.003   # 0.3% — borderline, be conservative

        # VIX adjustment — wider spreads in fearful markets
        if vix_now >= 30:
            vix_adj = 0.004    # +0.4% in high fear
        elif vix_now >= 20:
            vix_adj = 0.002    # +0.2% in elevated fear
        else:
            vix_adj = 0.0

        tolerance = max(base_tol + vix_adj, spread_pct / 100 + 0.002)

        if side == "buy":
            limit_price = round(estimated_price * (1 + tolerance), 2)
        else:
            limit_price = round(estimated_price * (1 - tolerance), 2)

        result = alpaca_post("/v2/orders", {
            "symbol": symbol, "qty": str(qty), "side": side,
            "type": "limit", "limit_price": str(limit_price),
            "time_in_force": "day",
        })
        if result:
            log.info(f"[LIMIT ORDER] {side.upper()} {symbol} limit:${limit_price:.2f} signal:${estimated_price:.2f} tolerance:{tolerance*100:.2f}% score:{signal_score:.1f} VIX:{vix_now:.0f}")
        else:
            log.warning(f"[LIMIT] Failed for {symbol} — falling back to market order")
            result = alpaca_post("/v2/orders", {
                "symbol": symbol, "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day",
            })
    else:
        result = alpaca_post("/v2/orders", {
            "symbol": symbol, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "gtc" if crypto else "day",
        })

    if IS_LIVE and result and result.get("id"):
        # Option B: wait for fill then fetch actual filled_avg_price from Alpaca
        order_id = result["id"]
        real_fill = None
        for attempt in range(5):  # try up to 5 times over ~5 seconds
            time.sleep(1)
            filled_order = alpaca_get(f"/v2/orders/{order_id}")
            if filled_order:
                status = filled_order.get("status", "")
                avg_price = filled_order.get("filled_avg_price")
                if avg_price and float(avg_price) > 0:
                    real_fill = float(avg_price)
                    slip_pct  = ((real_fill - (estimated_price or real_fill)) / (estimated_price or real_fill) * 100)
                    log.info(f"[FILL] Alpaca {side.upper()} {symbol}: signal=${estimated_price:.4f} fill=${real_fill:.4f} slippage={slip_pct:+.3f}% status={status}")
                    break
                if status in ("filled", "partially_filled"):
                    break  # filled but no price yet — use fallback
        if real_fill:
            return result, real_fill

    # Paper trading or fallback — apply slippage model
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
    # Hard stop — skip ALL live price fetches during Binance ban
    if crypto and USE_BINANCE and time.time() < _binance_ban_until:
        return  # positions safe — exchange stops protect them during ban
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

    # Ban check and kill switch BEFORE setting running=True or touching any data
    if crypto and USE_BINANCE and time.time() < _binance_ban_until:
        remaining = int(_binance_ban_until - time.time())
        log.info(f"[{st.label}] Binance ban active ({remaining}s remaining) — skipping cycle silently")
        return

    if kill_switch["active"]:
        log.info(f"[{st.label}] Kill switch active — no trading")
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

    # Scan — use batch API call for US stocks (1 call per 100 symbols vs 100 individual calls)
    results = []
    if not crypto:
        # Batch fetch all US stock bars in one API call
        bars_batch = fetch_bars_batch(watchlist)
        for sym in watchlist:
            bars = bars_batch.get(sym)
            if not bars: continue
            closes  = [b["c"] for b in bars]
            volumes = [b["v"] for b in bars]
            price   = closes[-1]
            prev    = closes[-2] if len(closes) > 1 else price
            change  = ((price - prev) / prev) * 100
            avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
            vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
            signal, e9, e21, rsi = get_signal(closes, volumes)
            ema_gap = round(((e9 - e21) / e21) * 100, 2) if e9 and e21 and e21 != 0 else None
            results.append({"symbol": sym, "price": price, "change": change,
                "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi,
                "vol_ratio": vol_ratio, "closes": closes, "bars": bars,
                "ema_gap": ema_gap})
    else:
        # Crypto — individual calls (Binance handles its own batching)
        for sym in watchlist:
            bars = fetch_bars(sym, crypto=True)
            if not bars: continue
            closes  = [b["c"] for b in bars]
            volumes = [b["v"] for b in bars]
            price   = closes[-1]
            prev    = closes[-2] if len(closes) > 1 else price
            change  = ((price - prev) / prev) * 100
            avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
            vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
            signal, e9, e21, rsi = get_signal(closes, volumes)
            ema_gap = round(((e9 - e21) / e21) * 100, 2) if e9 and e21 and e21 != 0 else None
            results.append({"symbol": sym, "price": price, "change": change,
                "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi,
                "vol_ratio": vol_ratio, "closes": closes, "bars": bars,
                "ema_gap": ema_gap})

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
                                   s.get("closes", [s["price"]]*21),
                                   bars=s.get("bars"))
    buy_candidates.sort(key=lambda x: x["score"], reverse=True)

    # Track near-misses with skip reasons for ALL scored candidates
    # This captures WHY each trade was skipped — not just score
    for s in results:
        sc = s.get("score") or score_signal(s["symbol"], s["price"], s["change"],
                         s.get("rsi"), s.get("vol_ratio"),
                         s.get("closes", [s["price"]]*21),
                         bars=s.get("bars"))
        gap = MIN_SIGNAL_SCORE - sc

        # Determine skip reason
        skip_reason = None
        if s["signal"] == "BUY" and sc >= MIN_SIGNAL_SCORE:
            # Would have traded — check if blocked by limits
            pos_count       = len(st.positions)
            global_pos      = all_positions_count()
            daily_trades    = st.daily_trades
            exposure_pct    = (sum(p.get("value", 0) for p in st.positions.values()) /
                               max(total_portfolio_value(), 1)) * 100

            if daily_trades >= MAX_TRADES_PER_DAY:
                skip_reason = "TRADE_LIMIT"
            elif global_pos >= MAX_TOTAL_POSITIONS:
                skip_reason = "POS_LIMIT"
            elif pos_count >= MAX_POSITIONS:
                skip_reason = "POS_LIMIT"
            elif exposure_pct >= MAX_EXPOSURE_PCT:
                skip_reason = "EXPOSURE"
            elif s["symbol"] in st.positions:
                skip_reason = "DUPLICATE"
            else:
                # Check sector cap
                sym_sector = next((sec for sec, syms in SECTOR_MAP.items()
                                   if s["symbol"] in syms), None)
                if sym_sector:
                    sector_count = sum(1 for p in st.positions
                                       if next((sec for sec, syms in SECTOR_MAP.items()
                                                if p in syms), None) == sym_sector)
                    if sector_count >= MAX_SECTOR_POSITIONS:
                        skip_reason = "SECTOR"
        elif sc < MIN_SIGNAL_SCORE and gap <= 2.0:
            skip_reason = "SCORE"

        if skip_reason:
            record_near_miss(s["symbol"], sc, s["price"], crypto=crypto)
            db_record_near_miss(s["symbol"], sc, MIN_SIGNAL_SCORE, gap, s["price"], crypto=crypto)
            # Store skip reason in tracker
            today = datetime.now().date().isoformat()
            key   = f"{s['symbol']}_{today}"
            if key in near_miss_tracker:
                near_miss_tracker[key]["skip_reason"] = skip_reason
            # Alert on hot misses — high score blocked by limits
            if sc >= 8.0 and skip_reason in ("POS_LIMIT", "TRADE_LIMIT", "EXPOSURE"):
                tg_hot_miss(s["symbol"], sc, skip_reason, s["price"])
            elif sc >= 9.0:  # exceptional signal — always alert
                tg_hot_miss(s["symbol"], sc, skip_reason, s["price"])

    # Record near-misses — scores just below threshold for 5-day follow-up
    for s in results:
        sc = score_signal(s["symbol"], s["price"], s["change"],
                         s.get("rsi"), s.get("vol_ratio"),
                         s.get("closes", [s["price"]]*21),
                         bars=s.get("bars"))
        s["score"] = sc
        gap = MIN_SIGNAL_SCORE - sc
        if 0 < gap <= 2.0:  # within 2 points of threshold
            record_near_miss(s["symbol"], sc, s["price"], crypto=crypto)
            db_record_near_miss(s["symbol"], sc, MIN_SIGNAL_SCORE, gap, s["price"], crypto=crypto)

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
        # Global symbol lock — prevent two bots holding the same ticker
        if s["symbol"] in all_symbols_held():
            log.info(f"[{st.label}] SKIP {s['symbol']} — already held by another bot")
            continue
        # Sector correlation cap — prevent holding multiple stocks in same sector
        sym_sector = SECTOR_MAP.get(s["symbol"])
        if sym_sector:
            held_sectors = sectors_held()
            if held_sectors.get(sym_sector, 0) >= MAX_SECTOR_POSITIONS:
                log.info(f"[{st.label}] SKIP {s['symbol']} — sector {sym_sector} already at max ({MAX_SECTOR_POSITIONS})")
                continue
        # Wash sale cooldown — don't re-buy a stock sold at a loss today
        cooldown_expiry = st.loss_cooldown.get(s["symbol"], 0)
        if time.time() < cooldown_expiry:
            remaining = int((cooldown_expiry - time.time()) / 60)
            log.info(f"[{st.label}] SKIP {s['symbol']} — loss cooldown ({remaining}m remaining)")
            continue
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
        adj_size   = vol_adjusted_size(base_size) * news_size_multiplier(s["symbol"]) * equity_curve_size_factor()
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

        # Detailed signal breakdown — logged so you can learn WHY each trade fired
        breakdown = signal_breakdown(
            s["symbol"], s["price"], s.get("change", 0), s.get("rsi"),
            s.get("vol_ratio", 1), s.get("closes", [s["price"]]*22),
            sig_score, crypto=crypto
        )
        log.info(f"[{st.label}] ✅ BUY SIGNAL BREAKDOWN:\n{breakdown}")
        log.info(f"[{st.label}] Executing: BUY {s['symbol']} x{qty} @ ~${s['price']:.4f} | stop:${stop_price:.4f} | target:${take_profit_price:.4f}")
        mark_near_miss_triggered(s["symbol"])  # track if this was a former near-miss
        place_order._last_score = sig_score  # pass score to place_order for dynamic tolerance
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=crypto, estimated_price=s["price"])
        if order and fill_price:
            market_type = "crypto" if crypto else "stock"
            tg_trade_buy(s["symbol"], fill_price, sig_score, market=market_type)
        if not order:
            log.warning(f"[{st.label}] ORDER FAILED for {s['symbol']} — tracking as unfilled near-miss")
            record_near_miss(s["symbol"], sig_score, s["price"], crypto=crypto)
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
            # Place real exchange stop order on Alpaca — MANDATORY for stocks
            if not crypto:
                stop_order = place_stop_order_alpaca(s["symbol"], qty, round(actual_stop, 2))
                if stop_order and stop_order.get("id"):
                    exchange_stops[s["symbol"]] = stop_order["id"]
                    log.info(f"[{st.label}] Exchange stop placed for {s['symbol']} @ ${actual_stop:.2f}")
                else:
                    # EMERGENCY: stop failed to place — exit position immediately
                    log.error(f"[EMERGENCY] Stop order FAILED for {s['symbol']} — emergency exit to protect capital")
                    place_order(s["symbol"], "sell", qty, crypto=False, estimated_price=fill_price)
                    if s["symbol"] in st.positions:
                        del st.positions[s["symbol"]]
                    log.error(f"[EMERGENCY] Position closed. No position held without exchange stop.")
                    pos_count -= 1
                    continue  # skip to next candidate
            st.daily_spend += trade_val
            st.trades_today += 1
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"),
                "entry_ts": datetime.now().isoformat(),
                "score": sig_score,
                "rsi": s.get("rsi"),
                "vol_ratio": s.get("vol_ratio"),
                "breakdown": breakdown})
            st.positions[s["symbol"]]["entry_ts"] = datetime.now().isoformat()
            st.positions[s["symbol"]]["entry_breakdown"] = breakdown
            st.positions[s["symbol"]]["signal_score"] = sig_score
            pos_count += 1

    # Close SELL positions
    for s in results:
        if s["signal"] != "SELL": continue
        if s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        entry_ts   = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None
        order_sell, sell_price = place_order(s["symbol"], "sell", pos["qty"], crypto=crypto, estimated_price=s["price"])
        pnl = (sell_price - pos["entry_price"]) * pos["qty"]
        bd  = sell_breakdown(s["symbol"], pos, sell_price, pnl, "Signal", hold_hours, crypto=crypto)
        log.info(f"[{st.label}] SELL BREAKDOWN:\n{bd}")
        if order_sell:
            del st.positions[s["symbol"]]
            st.daily_pnl += pnl
            st.trades_today += 1
            st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
                "price": sell_price, "pnl": pnl, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"),
                "hold_hours": hold_hours,
                "breakdown": bd})
            st.trades = st.trades[:200]
            if st.daily_pnl >= DAILY_PROFIT_TARGET:
                log.info(f"[{st.label}] Profit target hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"[{st.label}] Loss limit hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break

    st.running = False

# ── Email ─────────────────────────────────────────────────────
def send_weekly_near_miss_email():
    """Send weekly near-miss analysis with Claude insights, sparklines and exit simulations."""
    try:
        # Run simulations first
        run_near_miss_simulations()

        misses = [m for m in near_miss_tracker.values() if len(m["prices_since"]) >= 1]
        if not misses:
            log.info("[WEEKLY] No near-miss data to report yet")
            return

        # Claude analysis
        claude_analysis = generate_weekly_near_miss_report()

        # Build HTML email with sparklines
        rows = ""
        for m in sorted(misses, key=lambda x: x.get("pct_move", 0), reverse=True)[:20]:
            pct      = m.get("pct_move", 0)
            color    = "#00ff88" if pct >= 0 else "#ff4466"
            spark    = build_sparkline_html(m["price_at_miss"], m["prices_since"])
            trig     = "✅ Triggered!" if m["triggered"] else "❌ Never triggered"
            trig_col = "#00ff88" if m["triggered"] else "#555"
            rows += (
                f'<tr>'
                f'<td style="font-weight:700;color:#00aaff">{m["symbol"]}</td>'
                f'<td>{m["date"]}</td>'
                f'<td style="color:#ffcc00">{m["score"]}/{m["threshold"]}</td>'
                f'<td style="color:#ff8800">{m["gap"]}</td>'
                f'<td>${m["price_at_miss"]:.4f}</td>'
                f'<td>{spark}</td>'
                f'<td style="color:{color};font-weight:700">{pct:+.1f}%</td>'
                f'<td style="color:{trig_col}">{trig}</td>'
                f'</tr>'
            )

        winners = len([m for m in misses if m.get("pct_move", 0) > 2])
        losers  = len([m for m in misses if m.get("pct_move", 0) < -2])
        triggered = len([m for m in misses if m["triggered"]])

        html = f"""<!DOCTYPE html>
<html>
<head><style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #090b0e; color: #e0e0e0; padding: 24px; }}
  h1 {{ color: #00ff88; }} h2 {{ color: #00aaff; border-bottom: 1px solid #1a2a1a; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  th {{ background: #0d1117; color: #555; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; padding: 10px; text-align: left; }}
  td {{ padding: 10px; border-top: 1px solid #1a1a1a; font-size: 13px; }}
  .stat {{ display: inline-block; background: #0d1117; border-radius: 8px; padding: 12px 20px; margin: 8px; text-align: center; }}
  .stat-val {{ font-size: 24px; font-weight: 700; }}
  .insight {{ background: #0d1117; border-left: 3px solid #00aaff; padding: 16px; margin: 16px 0; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; }}
</style></head>
<body>
<h1>⚡ AlphaBot Weekly Near-Miss Report</h1>
<p style="color:#555">Week ending {datetime.now().strftime('%B %d, %Y')} · Threshold: {MIN_SIGNAL_SCORE}/11</p>

<div>
  <div class="stat"><div class="stat-val" style="color:#ffcc00">{len(misses)}</div><div>Near-Misses</div></div>
  <div class="stat"><div class="stat-val" style="color:#00ff88">{winners}</div><div>Went Up 2%+</div></div>
  <div class="stat"><div class="stat-val" style="color:#ff4466">{losers}</div><div>Went Down 2%+</div></div>
  <div class="stat"><div class="stat-val" style="color:#00aaff">{triggered}</div><div>Eventually Triggered</div></div>
  <div class="stat"><div class="stat-val" style="color:{"#00ff88" if total_sim_pnl >= 0 else "#ff4466"}">${total_sim_pnl:+.2f}</div><div>Simulated P&L</div></div>
  <div class="stat"><div class="stat-val" style="color:#00ff88">${missed_profit:+.2f}</div><div>Profit Missed</div></div>
  <div class="stat"><div class="stat-val" style="color:#ff8800">${avoided_loss:.2f}</div><div>Loss Avoided</div></div>
</div>

<h2>🤖 Claude AI Analysis</h2>
<div class="insight">{claude_analysis}</div>

<h2>📊 Near-Miss Detail (sorted by outcome)</h2>
<table>
  <thead><tr>
    <th>Symbol</th><th>Date</th><th>Score</th><th>Gap</th>
    <th>Entry</th><th>Chart</th><th>5-Day</th>
    <th>Simulated Exit</th><th>Peak</th><th>Status</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>

<p style="color:#333;font-size:11px;margin-top:32px">
  AlphaBot Weekly Report · Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC
</p>
</body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"AlphaBot Weekly Near-Miss Report — {datetime.now().strftime('%b %d')}"
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())

        log.info(f"[WEEKLY] Near-miss report sent — {len(misses)} misses analysed")
        # Archive to database
        db_record_report("weekly", f"AlphaBot Weekly Near-Miss Report — {datetime.now().strftime('%b %d')}", html, claude_analysis)

    except Exception as e:
        log.error(f"[WEEKLY] Failed to send report: {e}")


def send_daily_summary():
    def section(st):
        sells = [t for t in st.trades if t["side"] == "SELL" and t.get("pnl") is not None]
        wins  = [t for t in sells if t["pnl"] > 0]

        def fmt_trade(t):
            lines = []
            if t["side"] == "BUY":
                sign = "+"
                lines.append(f"  {t['time']}  BUY   {t['symbol']:10}  ${t['price']:.4f}")
                if t.get("score"):
                    lines.append(f"    Score: {t['score']}/10  RSI: {t.get('rsi','?')}  Vol: {t.get('vol_ratio','?')}x")
                if t.get("breakdown"):
                    # Include compact version of breakdown in email
                    for line in t["breakdown"].split("\n")[2:-1]:  # skip header/footer
                        lines.append(f"  {line}")
            else:
                sign = "+" if t.get("pnl", 0) >= 0 else ""
                pnl_str = f"  P&L: {sign}${t['pnl']:.2f}" if t.get("pnl") is not None else ""
                hold_str = f"  Held: {t['hold_hours']}h" if t.get("hold_hours") else ""
                lines.append(f"  {t['time']}  SELL  {t['symbol']:10}  ${t['price']:.4f}{pnl_str}{hold_str}")
                if t.get("breakdown"):
                    for line in t["breakdown"].split("\n")[2:-1]:
                        lines.append(f"  {line}")
            return "\n".join(lines)

        trade_lines = "\n\n".join(fmt_trade(t) for t in st.trades[:10]) or "  No trades today"
        return (f"{st.label}\n{'─'*40}\n"
                f"Daily P&L:   ${st.daily_pnl:+.2f}\n"
                f"Trades:      {len(sells)}\n"
                f"Win rate:    {int(len(wins)/len(sells)*100) if sells else 0}%\n"
                f"Positions:   {len(st.positions)}\n\n"
                f"Trade log (with signal breakdown):\n{trade_lines}\n")

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

    # Edge analysis
    edge_analysis = analyse_edge()

    body = (f"AlphaBot Daily Summary\n{'='*40}\n"
            f"Date: {datetime.now().strftime('%A, %d %B %Y')}\n"
            f"Mode: {'LIVE' if IS_LIVE else 'Paper'} Trading\n"
            f"Portfolio: ${float(account_info.get('portfolio_value',0)):,.2f}\n\n"
            f"{section(state)}\n{section(smallcap_state)}\n{section(intraday_state)}\n{section(crypto_state)}\n{section(crypto_intraday_state)}\n"
            f"{news_summary}"
            f"{near_miss_summary}\n"
            f"\nEDGE ANALYSIS\n{edge_analysis}\n"
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

# ── Analytics Dashboard ──────────────────────────────────────
def build_analytics_page(search_sym=None, report_id=None, period="all"):
    """Build the Trading Intelligence analytics page."""

    # ── Leaderboard ──
    period_days = {"90": 90, "30": 30, "all": None}.get(period, None)
    leaders = db_get_leaderboard(limit=20, period_days=period_days)
    period_label = {"90": "Last 90 Days", "30": "Last 30 Days", "all": "All Time"}.get(period, "All Time")

    medal = ["🥇","🥈","🥉"]
    lb_rows = ""
    for i, row in enumerate(leaders):
        sym, trades, wins, losses, total_pnl, best, worst, avg_sc = row[:8]
        win_rate = int(wins/trades*100) if trades > 0 else 0
        pnl_col  = "#00cc66" if total_pnl >= 0 else "#cc2244"
        best_col = "#00cc66" if best >= 0 else "#cc2244"
        worst_col= "#cc2244" if worst < 0 else "#00cc66"
        rank     = medal[i] if i < 3 else f"#{i+1}"
        lb_rows += f"""<tr onclick="searchSym('{sym}')" style="cursor:pointer">
          <td style="color:#888;font-weight:700">{rank}</td>
          <td style="color:#00aaff;font-weight:700">{sym}</td>
          <td>{trades}</td>
          <td style="color:#00cc66">{wins}</td>
          <td style="color:#cc2244">{losses}</td>
          <td style="color:#888">{win_rate}%</td>
          <td style="color:{pnl_col};font-weight:700">${total_pnl:+.2f}</td>
          <td style="color:{best_col}">${best:+.2f}</td>
          <td style="color:{worst_col}">${worst:+.2f}</td>
          <td style="color:#ffcc00">{avg_sc:.1f}</td>
        </tr>"""

    if not lb_rows:
        lb_rows = '<tr><td colspan="10" style="text-align:center;color:#555;padding:20px">No trades yet — check back after first week of trading</td></tr>'

    # ── Search results ──
    search_html = ""
    if search_sym:
        results = db_search_symbol(search_sym)
        stats   = results["stats"]
        trades  = results["trades"]
        misses  = results["near_misses"]

        if stats:
            sym, total_t, wins, losses, total_pnl, best, worst, avg_sc, nm_count, last_t, first_t, _ = stats
            win_rate = int(wins/total_t*100) if total_t > 0 else 0
            pnl_col  = "#00cc66" if total_pnl >= 0 else "#cc2244"
            search_html += f"""
            <div style="background:#0d1117;border:1px solid #1a3a5c;border-radius:12px;padding:20px;margin-bottom:20px">
              <div style="font-size:20px;font-weight:700;color:#00aaff;margin-bottom:12px">{sym}</div>
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
                <div style="background:#111820;border-radius:8px;padding:12px;text-align:center">
                  <div style="font-size:22px;font-weight:700;color:{pnl_col}">${total_pnl:+.2f}</div>
                  <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px">Total P&L</div>
                </div>
                <div style="background:#111820;border-radius:8px;padding:12px;text-align:center">
                  <div style="font-size:22px;font-weight:700;color:#e0e0e0">{total_t}</div>
                  <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px">Trades</div>
                </div>
                <div style="background:#111820;border-radius:8px;padding:12px;text-align:center">
                  <div style="font-size:22px;font-weight:700;color:#00cc66">{win_rate}%</div>
                  <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px">Win Rate</div>
                </div>
                <div style="background:#111820;border-radius:8px;padding:12px;text-align:center">
                  <div style="font-size:22px;font-weight:700;color:#ffcc00">{nm_count}</div>
                  <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px">Near Misses</div>
                </div>
              </div>"""

            # Trade history
            if trades:
                search_html += '<div style="font-size:12px;color:#555;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px">Trade History</div>'
                search_html += '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>'
                for h in ["Date","Side","Qty","Price","P&L","Score","Hold"]:
                    search_html += f'<th style="text-align:left;padding:6px 8px;color:#555;font-size:10px;text-transform:uppercase">{h}</th>'
                search_html += '</tr></thead><tbody>'
                for t in trades[:10]:
                    _, sym2, side, qty, price, pnl, score, rsi, vol, hold, reason, bd, mkt, date, time_, _ = t
                    pc = "#00cc66" if (pnl or 0) >= 0 else "#cc2244"
                    sc = "#00aaff" if side == "BUY" else "#ffcc00"
                    search_html += f"""<tr style="border-top:1px solid #1a1a1a">
                      <td style="padding:6px 8px;color:#888">{date}</td>
                      <td style="padding:6px 8px;color:{sc};font-weight:700">{side}</td>
                      <td style="padding:6px 8px">{qty}</td>
                      <td style="padding:6px 8px;font-family:monospace">${price:.4f}</td>
                      <td style="padding:6px 8px;color:{pc};font-weight:700">{f"+${pnl:.2f}" if pnl else "—"}</td>
                      <td style="padding:6px 8px;color:#ffcc00">{score or "—"}</td>
                      <td style="padding:6px 8px;color:#555">{f"{hold}h" if hold else "—"}</td>
                    </tr>"""
                search_html += '</tbody></table>'

            search_html += "</div>"
        else:
            search_html = f'<div style="color:#555;padding:20px;text-align:center">No data found for <b style="color:#00aaff">{search_sym}</b> yet</div>'

    # ── Report archive ──
    reports = db_get_reports(limit=30)
    report_rows = ""
    for r in reports:
        rid, rtype, rdate, subject = r
        icon = "📊" if rtype == "daily" else "📈" if rtype == "weekly" else "☀️"
        type_col = "#00aaff" if rtype == "daily" else "#00cc66" if rtype == "weekly" else "#ffcc00"
        report_rows += f"""<tr onclick="loadReport({rid})" style="cursor:pointer">
          <td style="padding:8px;color:{type_col}">{icon} {rtype.title()}</td>
          <td style="padding:8px;color:#888">{rdate}</td>
          <td style="padding:8px;color:#e0e0e0">{subject or "—"}</td>
        </tr>"""
    if not report_rows:
        report_rows = '<tr><td colspan="3" style="padding:20px;text-align:center;color:#555">No reports archived yet — first report arrives tonight at 11pm Paris</td></tr>'

    # ── Full report viewer ──
    report_viewer = ""
    if report_id:
        report = db_get_report_by_id(int(report_id))
        if report:
            _, rtype, rdate, subject, body_html, body_text, _ = report
            report_viewer = f"""
            <div style="background:#0d1117;border:1px solid #1a3a5c;border-radius:12px;padding:20px;margin-bottom:20px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
                <div style="font-weight:700;color:#e0e0e0">{subject}</div>
                <div style="color:#555;font-size:12px">{rdate}</div>
              </div>
              <div style="border-top:1px solid #1a1a1a;padding-top:16px;font-size:13px;line-height:1.6;color:#ccc;white-space:pre-wrap">{body_text}</div>
            </div>"""

    # ── Overall stats + expectancy + score curve ──
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Headline stats
        c.execute("SELECT COUNT(*), SUM(pnl), AVG(score) FROM trades WHERE side='SELL'")
        row = c.fetchone()
        total_trades_db = row[0] or 0
        total_pnl_db    = row[1] or 0
        avg_score_db    = row[2] or 0

        c.execute("SELECT COUNT(DISTINCT symbol) FROM trades")
        unique_syms = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM near_misses")
        total_misses = c.fetchone()[0] or 0

        # Expectancy calculation
        # Expectancy = (win_rate x avg_win) - (loss_rate x avg_loss)
        c.execute("""SELECT
            COUNT(*) as total,
            SUM(CASE WHEN pnl > 0 THEN 1.0 ELSE 0 END) as wins,
            AVG(CASE WHEN pnl > 0 THEN pnl ELSE NULL END) as avg_win,
            AVG(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE NULL END) as avg_loss
            FROM trades WHERE side='SELL' AND pnl IS NOT NULL""")
        ex = c.fetchone()
        if ex and ex[0] and ex[0] >= 5:
            total_ex  = ex[0]
            win_rate_ex  = (ex[1] or 0) / total_ex
            loss_rate_ex = 1 - win_rate_ex
            avg_win_ex   = ex[2] or 0
            avg_loss_ex  = ex[3] or 0
            expectancy   = (win_rate_ex * avg_win_ex) - (loss_rate_ex * avg_loss_ex)
            win_pct_ex   = int(win_rate_ex * 100)
            exp_color    = "#00cc66" if expectancy > 0 else "#cc2244"
            exp_label    = f"${expectancy:+.2f}"
            exp_note     = "✅ Positive Edge" if expectancy > 0 else "❌ Negative Edge"
        else:
            expectancy = None
            exp_color  = "#555"
            exp_label  = "—"
            exp_note   = f"Need {5 - (ex[0] if ex and ex[0] else 0)} more trades"
            win_pct_ex = 0

        # Score bucket analysis — which scores actually make money
        c.execute("""SELECT
            CAST(score AS INTEGER) as bucket,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(pnl), 2) as avg_pnl
            FROM trades
            WHERE side='SELL' AND pnl IS NOT NULL AND score IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket""")
        score_buckets = c.fetchall()

        conn.close()
    except Exception as e:
        total_trades_db = total_pnl_db = avg_score_db = unique_syms = total_misses = 0
        expectancy = None
        exp_color = "#555"; exp_label = "—"; exp_note = "No data yet"; win_pct_ex = 0
        score_buckets = []

    pnl_col_db = "#00cc66" if total_pnl_db >= 0 else "#cc2244"

    # ── Build skip reason breakdown ──
    skip_reasons = db_get_skip_reason_breakdown()
    reason_labels = {
        "SCORE":       ("📊", "Score Too Low",     "#ffcc00", "Signal score below threshold"),
        "TRADE_LIMIT": ("🔢", "Daily Trade Limit", "#ff8800", "Hit max 10 trades for the day"),
        "POS_LIMIT":   ("📦", "Position Limit",    "#ff4466", "Already at max 3 positions"),
        "EXPOSURE":    ("💰", "Exposure Limit",    "#ff4466", "Hit 30% portfolio exposure cap"),
        "SECTOR":      ("🏭", "Sector Cap",        "#aa44ff", "Already holding stock in same sector"),
        "DUPLICATE":   ("🔁", "Already Held",      "#555555", "Symbol already in portfolio"),
    }
    if skip_reasons:
        sr_cards = ""
        total_skips = sum(r[1] for r in skip_reasons)
        for reason, count, avg_sc in skip_reasons:
            icon, label, color, desc = reason_labels.get(reason, ("❓", reason, "#555", ""))
            pct = int(count/total_skips*100) if total_skips > 0 else 0
            sr_cards += f"""
            <div style="background:#0d1117;border-radius:10px;padding:16px;border-left:3px solid {color}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <div style="font-weight:700;color:{color}">{icon} {label}</div>
                <div style="font-size:20px;font-weight:700;color:{color}">{count}</div>
              </div>
              <div style="font-size:11px;color:#555;margin-bottom:8px">{desc}</div>
              <div style="background:#1a1a1a;border-radius:4px;height:6px;overflow:hidden">
                <div style="width:{pct}%;height:100%;background:{color};border-radius:4px"></div>
              </div>
              <div style="display:flex;justify-content:space-between;margin-top:4px">
                <div style="font-size:10px;color:#444">{pct}% of skips</div>
                <div style="font-size:10px;color:#444">Avg score: {avg_sc}</div>
              </div>
            </div>"""
        skip_reason_html = f"""
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <div class="section-title" style="margin:0">🚫 Why Trades Were Skipped</div>
            <div style="font-size:11px;color:#555">{total_skips} total skips tracked</div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">
            {sr_cards}
          </div>
          <div style="margin-top:14px;font-size:11px;color:#555;padding:10px;background:#0d1117;border-radius:8px">
            💡 <b style="color:#ffcc00">How to read this:</b>
            If <b>Position Limit</b> or <b>Trade Limit</b> is high — your limits may be too conservative and you're missing profitable trades.
            If <b>Score Too Low</b> dominates — your threshold may need tuning.
            Use the weekly simulation report to quantify the cost of each limit.
          </div>
        </div>"""
    else:
        skip_reason_html = """
        <div class="card">
          <div class="section-title">🚫 Why Trades Were Skipped</div>
          <div style="color:#555;padding:20px;text-align:center">
            No skip data yet — populates automatically as the bot runs
          </div>
        </div>"""

    # ── Build score curve HTML ──
    score_curve_html = ""
    if score_buckets:
        max_abs_pnl = max(abs(b[3]) for b in score_buckets) or 1
        bars = ""
        for bucket, trades, wins, total_pnl, avg_pnl in score_buckets:
            win_rate  = int(wins/trades*100) if trades > 0 else 0
            bar_h     = max(4, int((abs(total_pnl) / max_abs_pnl) * 80))
            bar_col   = "#00cc66" if total_pnl >= 0 else "#cc2244"
            edge_tag  = "✅" if (win_rate >= 55 and total_pnl > 0) else ("❌" if total_pnl < 0 else "⚠️")
            bars += f"""<div style="display:flex;flex-direction:column;align-items:center;gap:4px;min-width:50px">
              <div style="font-size:10px;color:{bar_col};font-weight:700">{edge_tag}</div>
              <div style="font-size:10px;color:{bar_col}">${total_pnl:+.0f}</div>
              <div style="width:36px;height:{bar_h}px;background:{bar_col};border-radius:4px 4px 0 0;
                          display:flex;align-items:flex-end;justify-content:center"></div>
              <div style="font-size:11px;color:#888;font-weight:700">Score {bucket}</div>
              <div style="font-size:10px;color:#555">{trades} trades</div>
              <div style="font-size:10px;color:#555">{win_rate}% win</div>
            </div>"""
        score_curve_html = f"""
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <div class="section-title" style="margin:0">📊 Score → Profitability Curve</div>
            <div style="font-size:11px;color:#555">Which signal scores actually make money?</div>
          </div>
          <div style="display:flex;gap:16px;align-items:flex-end;padding:16px 8px;
                      background:#0d1117;border-radius:8px;min-height:120px;overflow-x:auto">
            {bars}
          </div>
          <div style="margin-top:12px;font-size:11px;color:#555;padding:0 8px">
            ✅ = Profitable bucket (win rate 55%+) &nbsp;·&nbsp;
            ❌ = Losing bucket — consider raising MIN_SIGNAL_SCORE &nbsp;·&nbsp;
            ⚠️ = Mixed results — need more data
          </div>
          <div style="margin-top:8px;font-size:11px;color:#ffcc00;padding:0 8px">
            ⚠️ Minimum 30 trades per bucket before adjusting threshold (overfitting risk)
          </div>
        </div>"""
    else:
        score_curve_html = """
        <div class="card">
          <div class="section-title">📊 Score → Profitability Curve</div>
          <div style="color:#555;padding:20px;text-align:center">
            No completed trades yet — chart builds automatically as trades close<br>
            <span style="font-size:11px;color:#333">Will show which signal scores (5,6,7,8+) actually make money</span>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaBot Analytics</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#090b0e; color:#e0e0e0; font-family:'Segoe UI',sans-serif; font-size:14px; }}
  .header {{ background:#0d1117; border-bottom:1px solid #1e2a1e; padding:14px 24px; display:flex; align-items:center; justify-content:space-between; }}
  .nav {{ display:flex; gap:16px; align-items:center; }}
  .nav a {{ color:#555; text-decoration:none; font-size:13px; padding:6px 12px; border-radius:6px; }}
  .nav a:hover {{ color:#e0e0e0; background:rgba(255,255,255,0.05); }}
  .nav a.active {{ color:#00aaff; background:rgba(0,170,255,0.1); }}
  .container {{ padding:24px; max-width:1200px; margin:0 auto; }}
  .section-title {{ font-size:15px; font-weight:700; margin-bottom:14px; color:#e0e0e0; }}
  .card {{ background:rgba(255,255,255,0.025); border:1px solid rgba(255,255,255,0.07); border-radius:12px; padding:20px; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th {{ font-size:10px; color:#444; letter-spacing:1.5px; text-transform:uppercase; padding:10px 8px; text-align:left; }}
  td {{ padding:9px 8px; border-top:1px solid rgba(255,255,255,0.04); font-family:monospace; }}
  tr:hover td {{ background:rgba(255,255,255,0.02); }}
  .search-box {{ display:flex; gap:10px; margin-bottom:20px; }}
  .search-box input {{ flex:1; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:8px; padding:10px 14px; color:#e0e0e0; font-size:14px; outline:none; }}
  .search-box input:focus {{ border-color:#00aaff; }}
  .search-box button {{ padding:10px 20px; background:#00aaff; border:none; border-radius:8px; color:#090b0e; font-weight:700; cursor:pointer; font-size:13px; }}
  .period-tabs {{ display:flex; gap:8px; margin-bottom:16px; }}
  .period-tab {{ padding:6px 16px; border-radius:20px; font-size:12px; cursor:pointer; border:1px solid rgba(255,255,255,0.1); color:#555; background:transparent; }}
  .period-tab.active {{ background:#00aaff; color:#090b0e; border-color:#00aaff; font-weight:700; }}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:10px">
    <div style="width:28px;height:28px;background:linear-gradient(135deg,#00ff88,#00aaff);border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:14px">⚡</div>
    <span style="font-weight:700;font-size:16px">AlphaBot</span>
    <span style="font-size:11px;color:#444;margin-left:4px">Analytics</span>
  </div>
  <div class="nav">
    <a href="/">📊 Dashboard</a>
    <a href="/analytics" class="active">🧠 Analytics</a>
  </div>
</div>

<div class="container">

  <!-- Overall Stats -->
  <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:20px">
    <div class="card" style="text-align:center">
      <div style="font-size:24px;font-weight:700;color:{pnl_col_db}">${total_pnl_db:+.2f}</div>
      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Total P&L</div>
    </div>
    <div class="card" style="text-align:center;border-color:rgba({("0,200,100" if expectancy and expectancy > 0 else "200,50,50") },0.3)">
      <div style="font-size:24px;font-weight:700;color:{exp_color}">{exp_label}</div>
      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Expectancy/Trade</div>
      <div style="font-size:10px;color:{exp_color};margin-top:2px">{exp_note}</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:24px;font-weight:700;color:#e0e0e0">{total_trades_db}</div>
      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Total Trades</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:24px;font-weight:700;color:#00cc66">{win_pct_ex}%</div>
      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Win Rate</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:24px;font-weight:700;color:#ffcc00">{avg_score_db:.1f}</div>
      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Avg Score</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:24px;font-weight:700;color:#ff8800">{total_misses}</div>
      <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:4px">Near Misses</div>
    </div>
  </div>

  <!-- Score Curve -->
  {score_curve_html}

  <!-- Stock Search -->
  <div class="card">
    <div class="section-title">🔍 Stock Search</div>
    <div class="search-box">
      <input type="text" id="search-input" placeholder="Search any ticker — e.g. NVDA, BTCUSDT, AAPL..." 
             value="{search_sym or ''}" onkeydown="if(event.key==='Enter') doSearch()">
      <button onclick="doSearch()">Search</button>
    </div>
    {search_html}
  </div>

  <!-- Leaderboard -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <div class="section-title" style="margin:0">🏆 Stock Leaderboard — {period_label}</div>
      <div class="period-tabs">
        <button class="period-tab {'active' if period=='30' else ''}" onclick="setPeriod('30')">30 Days</button>
        <button class="period-tab {'active' if period=='90' else ''}" onclick="setPeriod('90')">90 Days</button>
        <button class="period-tab {'active' if period=='all' else ''}" onclick="setPeriod('all')">All Time</button>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Rank</th><th>Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th>
        <th>Win Rate</th><th>Total P&L</th><th>Best Trade</th><th>Worst Trade</th><th>Avg Score</th>
      </tr></thead>
      <tbody>{lb_rows}</tbody>
    </table>
    </div>
  </div>

  <!-- Skip Reason Breakdown -->
  {skip_reason_html}

  <!-- Report Archive -->
  <div class="card">
    <div class="section-title">📁 Report Archive</div>
    {report_viewer}
    <table>
      <thead><tr><th>Type</th><th>Date</th><th>Subject</th></tr></thead>
      <tbody>{report_rows}</tbody>
    </table>
  </div>

</div>

<script>
function doSearch() {{
  var sym = document.getElementById('search-input').value.trim().toUpperCase();
  if (sym) window.location.href = '/analytics?search=' + encodeURIComponent(sym);
}}
function searchSym(sym) {{
  window.location.href = '/analytics?search=' + encodeURIComponent(sym);
}}
function setPeriod(p) {{
  window.location.href = '/analytics?period=' + p;
}}
function loadReport(id) {{
  window.location.href = '/analytics?report_id=' + id;
}}
</script>
</body>
</html>"""


# ── Web dashboard ─────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
  .error { color: #ff4466; font-size: 13px; margin-top: 12px; text-align: center; display: none; }
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <div>
      <div class="logo-text">AlphaBot</div>
      <div class="logo-sub">Automated Day Trader</div>
    </div>
  </div>
  <form method="POST" action="/login">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" autofocus>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password">
    <button type="submit">Sign In →</button>
  </form>
  <p style="font-size:11px;color:#444;margin-top:16px;text-align:center">After signing in, bookmark the page URL for instant mobile access</p>
  <div class="error" id="err">Invalid credentials</div>
</div>
<script>
// Auto-reload every 60s — JS reload preserves cookies unlike meta refresh on mobile
var _t = 60;
var _el = document.getElementById("refresh-timer");
setInterval(function() {{
  _t--;
  if (_el) _el.textContent = "↻ refreshing in " + _t + "s";
  if (_t <= 0) {{ window.location.reload(); }}
}}, 1000);
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- Auto-refresh handled by JS below to preserve mobile session -->
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
    <div style="display:flex;align-items:center;gap:12px">
    <a href="/analytics" style="padding:6px 14px;border-radius:6px;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff;text-decoration:none;font-size:11px;font-weight:700;letter-spacing:1px">🧠 ANALYTICS</a>
    <div class="refresh" id="refresh-timer">↻ {now}</div>
  </div>
  </div>
</div>
<!-- Kill switch controls -->
<div style="background:#0d1117;border-bottom:1px solid rgba(255,255,255,0.06);padding:8px 24px;display:flex;align-items:center;gap:12px">
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
  if (pin === null) return;  // cancelled
  var token = document.getElementById('dash-token').textContent;
  var status = document.getElementById('cmd-status');
  status.textContent = 'Verifying...';
  fetch(path + '?token=' + token + '&pin=' + encodeURIComponent(pin), {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.status === 'wrong_pin') {{
        status.textContent = '❌ Wrong PIN';
        return;
      }}
      status.textContent = '✅ ' + d.status + ' — refreshing...';
      setTimeout(() => location.reload(), 2000);
    }})
    .catch(e => {{
      status.textContent = '❌ Error: ' + e;
    }});
}}
function sendCmd(path) {{
  var token = document.getElementById('dash-token').textContent;
  var status = document.getElementById('cmd-status');
  status.textContent = 'Sending...';
  fetch(path + '?token=' + token, {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      status.textContent = '✅ ' + d.status + ' — refreshing...';
      setTimeout(() => location.reload(), 2000);
    }})
    .catch(e => {{
      status.textContent = '❌ Error: ' + e;
    }});
}}
</script>

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

  <!-- Kill Switch Banner -->
  {kill_banner}

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
<script>
// Auto-reload every 60s — JS reload preserves cookies unlike meta refresh on mobile
var _t = 60;
var _el = document.getElementById("refresh-timer");
setInterval(function() {{
  _t--;
  if (_el) _el.textContent = "↻ refreshing in " + _t + "s";
  if (_t <= 0) {{ window.location.reload(); }}
}}, 1000);
</script>
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
        [dict(c, market="Stock") for c in state.candidates if c["signal"]=="BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE] +
        [dict(c, market="Crypto") for c in crypto_state.candidates if c["signal"]=="BUY" and c.get("score", 0) >= MIN_SIGNAL_SCORE]
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

        # Calculate score for each candidate and sort by score descending
        scored = []
        for c in candidates:
            sc = score_signal(c["symbol"], c["price"], c["change"],
                             c.get("rsi"), c.get("vol_ratio"),
                             c.get("closes", [c["price"]]*22))
            scored.append((sc, c))

        # Sort purely by score descending — best opportunities always at top
        # BUY > WATCH > SIGNAL all bubble up naturally by score
        bear_syms = set(BEAR_TICKERS)
        bear_items   = [(sc, c) for sc, c in scored if c["symbol"] in bear_syms]
        normal_items = [(sc, c) for sc, c in scored if c["symbol"] not in bear_syms]
        bear_items.sort(key=lambda x: -x[0])
        normal_items.sort(key=lambda x: -x[0])  # score descending only
        scored = bear_items + normal_items

        for sc, c in scored:
            # ── Smart signal badge ──────────────────────────────
            ema_crossed = c.get("ema_gap") is not None and c.get("ema_gap", -99) > 0
            score_ok    = sc >= MIN_SIGNAL_SCORE
            if score_ok and ema_crossed:
                # Both conditions met — actually trading
                sig_html = f'<span class="sig-buy">🟢 BUY {sc:.1f}</span>'
            elif score_ok and not ema_crossed:
                # Great score, waiting for EMA crossover
                sig_html = f'<span style="background:rgba(0,170,255,0.15);color:#00aaff;border:1px solid #00aaff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">👀 WATCH {sc:.1f}</span>'
            elif not score_ok and ema_crossed:
                # EMA crossed but score too low
                sig_html = f'<span style="background:rgba(255,204,0,0.1);color:#ffcc00;border:1px solid #ffcc00;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">⚡ SIGNAL {sc:.1f}</span>'
            elif c["signal"] == "SELL":
                sig_html = f'<span class="sig-sell">SELL</span>'
            else:
                sig_html = f'<span class="sig-hold">{sc:.1f}/{MIN_SIGNAL_SCORE}</span>'

            # ── Change colour ───────────────────────────────────
            chg_c = "green" if c["change"] >= 0 else "red"

            # ── RSI colour coding ───────────────────────────────
            rsi = c.get("rsi")
            rsi_val = f"{rsi:.1f}" if rsi else "—"
            if rsi:
                if 50 <= rsi <= 65:
                    rsi_color = "#00ff88"   # green — sweet spot
                    rsi_label = f"{rsi_val} ✅"
                elif 40 <= rsi < 50:
                    rsi_color = "#00aaff"   # blue — building
                    rsi_label = f"{rsi_val} 📈"
                elif 65 < rsi <= 75:
                    rsi_color = "#ffcc00"   # gold — getting hot
                    rsi_label = f"{rsi_val} ⚠"
                elif rsi > 75:
                    rsi_color = "#ff4466"   # red — overbought
                    rsi_label = f"{rsi_val} 🔴"
                elif rsi < 30:
                    rsi_color = "#ff8800"   # orange — oversold
                    rsi_label = f"{rsi_val} 📉"
                else:
                    rsi_color = "#555"      # grey — neutral
                    rsi_label = rsi_val
            else:
                rsi_color = "#555"
                rsi_label = "—"

            # ── Volume colour coding ────────────────────────────
            vr = c.get("vol_ratio", 0)
            if vr >= 2.0:
                vol_color = "#00ff88"   # green — strong conviction
                vol_label = f"{vr:.2f}x 🔥"
            elif vr >= 1.5:
                vol_color = "#00aaff"   # blue — good
                vol_label = f"{vr:.2f}x ✅"
            elif vr >= 1.2:
                vol_color = "#ffcc00"   # gold — mild
                vol_label = f"{vr:.2f}x ⚠"
            elif vr > 0:
                vol_color = "#555"      # grey — weak
                vol_label = f"{vr:.2f}x"
            else:
                vol_color = "#555"
                vol_label = "—"

            # ── Score bar ───────────────────────────────────────
            threshold = MIN_SIGNAL_SCORE
            score_pct = min(100, int((sc / 11) * 100))
            if sc >= threshold:
                bar_color = "#00ff88"
                proximity = f"✅ TRADE {sc:.1f}"
            elif sc >= threshold - 1:
                bar_color = "#ffcc00"
                proximity = f"🔥 {sc:.1f}/{threshold}"
            elif sc >= threshold - 2:
                bar_color = "#ff8800"
                proximity = f"⚡ {sc:.1f}/{threshold}"
            else:
                bar_color = "#333"
                proximity = f"{sc:.1f}/{threshold}"

            score_bar = f'''<div style="display:flex;align-items:center;gap:6px">
              <div style="width:50px;height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden">
                <div style="width:{score_pct}%;height:100%;background:{bar_color};border-radius:3px"></div>
              </div>
              <span style="font-size:11px;color:{bar_color};font-weight:700">{proximity}</span>
            </div>'''

            is_bear_ticker = c["symbol"] in bear_syms
            bear_badge = '<span style="font-size:9px;background:rgba(255,136,0,0.2);color:#ff8800;border:1px solid rgba(255,136,0,0.4);border-radius:4px;padding:1px 5px;margin-left:4px;font-weight:700">BEAR</span>' if is_bear_ticker else ''
            row_bg = "background:rgba(255,136,0,0.04);" if is_bear_ticker else ""
            # EMA gap — shows distance between EMA9 and EMA21
            ema_gap = c.get("ema_gap")
            if ema_gap is not None:
                if ema_gap > 0:
                    ema_col = "#00ff88"
                    ema_str = f"+{ema_gap:.2f}% ✅"  # crossed — bullish
                elif ema_gap > -0.5:
                    ema_col = "#ffcc00"
                    ema_str = f"{ema_gap:.2f}% 🔥"  # very close
                elif ema_gap > -1.5:
                    ema_col = "#ff8800"
                    ema_str = f"{ema_gap:.2f}% ⚡"  # close
                else:
                    ema_col = "#555"
                    ema_str = f"{ema_gap:.2f}%"     # far
            else:
                ema_col = "#555"
                ema_str = "—"

            rows += f"""<tr style="{row_bg}">
              <td style="font-weight:700" class="{color}">{c['symbol']}{bear_badge}</td>
              <td>${c['price']:.4f}</td>
              <td class="{chg_c}">{'+' if c['change']>=0 else ''}{c['change']:.2f}%</td>
              <td>{sig_html}</td>
              <td>{score_bar}</td>
              <td style="color:{ema_col};font-size:11px;font-weight:700">{ema_str}</td>
              <td style="color:{rsi_color};font-size:11px;font-weight:700">{rsi_label}</td>
              <td style="color:{vol_color};font-size:11px;font-weight:700">{vol_label}</td>
            </tr>"""

        count = len(candidates)
        buys  = sum(1 for c in candidates if c["signal"] == "BUY")
        holds = sum(1 for c in candidates if c["signal"] == "HOLD")
        sells = sum(1 for c in candidates if c["signal"] == "SELL")
        hot   = sum(1 for sc, c in scored if sc >= MIN_SIGNAL_SCORE - 1 and c["signal"] == "HOLD")
        return f"""
          <div style="display:flex;gap:16px;margin-bottom:14px;font-size:12px;flex-wrap:wrap">
            <span class="green" style="font-weight:700">{buys} BUY</span>
            <span style="color:#ffcc00">{hot} CLOSE</span>
            <span style="color:#555">{holds - hot} HOLD</span>
            <span class="red">{sells} SELL</span>
            <span style="color:#444;margin-left:auto">{count} total scanned</span>
          </div>
          <div style="overflow-x:auto">
          <table><thead><tr>
            <th>Symbol</th><th>Price</th><th>Chg%</th><th>Signal</th>
            <th>Score</th><th>EMA Cross</th><th>RSI</th><th>Vol</th>
          </tr></thead>
          <tbody>{rows}</tbody></table></div>"""

    # Kill switch banner
    if kill_switch["active"]:
        kill_banner = f'''<div style="background:rgba(255,68,102,0.15);border:1px solid #ff4466;border-radius:8px;padding:14px 20px;margin-bottom:16px;display:flex;align-items:center;gap:12px">
          <span style="font-size:20px">🛑</span>
          <div>
            <div style="font-weight:700;color:#ff4466;font-size:14px">KILL SWITCH ACTIVE — All bots stopped</div>
            <div style="font-size:12px;color:#888;margin-top:2px">{kill_switch["reason"]} · Activated at {kill_switch["activated_at"]}</div>
          </div>
        </div>'''
    else:
        kill_banner = ""

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
        kill_banner      = kill_banner,
        dash_token       = DASH_TOKEN,
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
        pf_color      = pf_color,
        old_pf_color  = pf_color,
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
    def _check_auth(self):
        """Auth disabled during paper trading — re-enable before going live."""
        return True

    def _require_auth(self):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if not self._check_auth():
            self._require_auth()
            return
        if self.path == "/api":
            with _state_lock:
                data = json.dumps({
                    "stocks": {"pnl": state.daily_pnl, "positions": len(state.positions), "trades": len(state.trades), "cycle": state.cycle_count},
                    "crypto": {"pnl": crypto_state.daily_pnl, "positions": len(crypto_state.positions), "trades": len(crypto_state.trades), "cycle": crypto_state.cycle_count},
                    "portfolio": float(account_info.get("portfolio_value", 0)) if account_info else 0,
                    "kill_switch": kill_switch["active"],
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())
            return
        # ── Analytics page ──
        if self.path.startswith("/analytics"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            search_sym = params.get("search", [None])[0]
            report_id  = params.get("report_id", [None])[0]
            period     = params.get("period", ["all"])[0]
            try:
                html = build_analytics_page(search_sym=search_sym, report_id=report_id, period=period)
            except Exception as e:
                log.error(f"[ANALYTICS] Failed: {e}")
                html = f"<html><body style='background:#111;color:#fff;padding:40px'><h2>Analytics Error</h2><pre>{e}</pre></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
            return

        # ── Main dashboard ──
        try:
            with _state_lock:
                html = build_dashboard()
        except Exception as e:
            log.error(f"[DASHBOARD] build_dashboard() failed: {e}")
            import traceback
            log.error(traceback.format_exc())
            html = f"<html><body style='background:#111;color:#fff;padding:40px;font-family:monospace'><h2>Dashboard Error</h2><pre>{e}</pre></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        """Handle login and kill switch commands."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

        # Extract PIN from query string if present
        from urllib.parse import urlparse, parse_qs
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)
        submitted_pin = query_params.get("pin", [None])[0]

        # For kill switch paths, verify PIN
        kill_paths = ["/kill", "/close-all", "/resume"]
        base_path = parsed_path.path
        if base_path in kill_paths and submitted_pin is not None:
            if submitted_pin != KILL_PIN:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "wrong_pin"}).encode())
                log.warning(f"[SECURITY] Wrong PIN attempt on {base_path}")
                return
            self.path = base_path  # strip query for handler below

        # Login form submission
        if self.path == "/login":
            import urllib.parse
            params = dict(urllib.parse.parse_qsl(body))
            if params.get("username") == DASH_USER and params.get("password") == DASH_PASS:
                # Redirect to dashboard with token in URL — works on all mobile browsers
                # User should bookmark this URL
                self.send_response(302)
                self.send_header("Location", f"/?token={DASH_TOKEN}")
                self.send_header("Set-Cookie", f"auth={DASH_TOKEN}; Path=/; SameSite=Lax")
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header("Location", "/?error=1")
                self.end_headers()
            return

        if not self._check_auth():
            self._require_auth()
            return

        if self.path == "/kill":
            kill_switch["active"]       = True
            kill_switch["reason"]       = "Manual kill switch activated from dashboard"
            kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
            # Shut off all bots
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                st.shutoff = True
            log.warning("[KILL SWITCH] Manual kill activated from dashboard — all bots stopped")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "killed"}).encode())

        elif self.path == "/resume":
            kill_switch["active"]       = False
            kill_switch["reason"]       = ""
            kill_switch["activated_at"] = None
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                st.shutoff = False
            log.info("[KILL SWITCH] Resumed from dashboard — all bots restarted")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "resumed"}).encode())

        elif self.path == "/close-all":
            log.warning("[KILL SWITCH] Close all positions requested from dashboard")
            for sym, pos in list(state.positions.items()):
                place_order(sym, "sell", pos["qty"], estimated_price=pos["entry_price"])
            for sym, pos in list(crypto_state.positions.items()):
                place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
            state.positions.clear()
            crypto_state.positions.clear()
            kill_switch["active"]       = True
            kill_switch["reason"]       = "Close all — positions liquidated from dashboard"
            kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
            for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                st.shutoff = True
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "closed"}).encode())

        else:
            self.send_response(404)
            self.end_headers()

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

    # Binance startup — NO API calls at startup to avoid triggering bans
    # Bot will connect naturally on first cycle after the 5-min buffer clears
    if USE_BINANCE:
        mode = "TESTNET (virtual money)" if BINANCE_USE_TESTNET else ("LIVE (real money)" if IS_LIVE else "PAPER")
        log.info(f"[BINANCE] Mode: {mode}")
        log.info(f"[BINANCE] Endpoint: {BINANCE_BASE}")
        log.info(f"[BINANCE] Scanning {len(CRYPTO_WATCHLIST)} coins — will connect on first cycle")
        log.info(f"[BINANCE] No startup ping — avoids triggering IP ban on restart")
    else:
        log.info("[BINANCE] Not configured — add BINANCE_KEY + BINANCE_SECRET to Railway to enable")

    # ── STARTUP RECOVERY — rebuild state from exchange ──────────
    log.info("=== Startup recovery check ===")
    try:
        # Recover open Alpaca positions
        open_positions = alpaca_get("/v2/positions") or []
        recovered = 0
        for pos in open_positions:
            sym   = pos.get("symbol")
            qty   = float(pos.get("qty", 0))
            entry = float(pos.get("avg_entry_price", 0))
            stop_pct = CRYPTO_STOP_PCT if "/" in str(sym) else STOP_LOSS_PCT
            stop  = entry * (1 - stop_pct / 100)
            tp    = entry * (1 + TAKE_PROFIT_PCT / 100)
            # Determine which state to add to
            is_crypto = pos.get("asset_class") == "crypto"
            target_state = crypto_state if is_crypto else state
            if sym not in target_state.positions:
                target_state.positions[sym] = {
                    "qty": qty, "entry_price": entry, "stop_price": stop,
                    "highest_price": entry, "take_profit_price": tp,
                    "entry_date": datetime.now().date().isoformat(),
                    "days_held": 0, "entry_ts": datetime.now().isoformat(),
                }
                # Check if current price already below stop — close immediately
                current_price = float(pos.get("current_price", 0)) or fetch_latest_price(sym, crypto=is_crypto)
                if current_price and current_price <= stop:
                    pnl = (current_price - entry) * qty
                    log.warning(f"[RECOVERY] {sym} already below stop (current:${current_price:.4f} stop:${stop:.4f}) — closing immediately P&L:${pnl:+.2f}")
                    place_order(sym, "sell", qty, crypto=is_crypto, estimated_price=current_price)
                    continue  # don't add to positions

                # Re-place exchange stop for any recovered position
                if not is_crypto:
                    stop_order = place_stop_order_alpaca(sym, qty, round(stop, 2))
                    if stop_order and stop_order.get("id"):
                        exchange_stops[sym] = stop_order["id"]
                        log.info(f"[RECOVERY] Restored {sym} qty:{qty} entry:${entry:.2f} current:${current_price:.2f} — exchange stop re-placed")
                else:
                    log.info(f"[RECOVERY] Restored crypto {sym} qty:{qty} entry:${entry:.4f} current:${current_price:.4f}")
                recovered += 1
        if recovered:
            log.info(f"=== Recovered {recovered} open position(s) from exchange ===")
        else:
            log.info("=== No open positions to recover ===")
    except Exception as e:
        log.error(f"Startup recovery failed: {e}")

    # ── Verify ALL existing positions have exchange stops ─────────
    log.info("=== Verifying exchange stops on all positions ===")
    try:
        open_orders = alpaca_get("/v2/orders?status=open") or []
        stop_order_symbols = {o["symbol"] for o in open_orders if o.get("type") == "stop"}
        for sym, pos in state.positions.items():
            if sym not in stop_order_symbols and sym not in exchange_stops:
                log.warning(f"[STOPS] {sym} has no exchange stop — placing now")
                stop_order = place_stop_order_alpaca(sym, pos["qty"], round(pos["stop_price"], 2))
                if stop_order and stop_order.get("id"):
                    exchange_stops[sym] = stop_order["id"]
                    log.info(f"[STOPS] Exchange stop placed for {sym} @ ${pos['stop_price']:.2f}")
    except Exception as e:
        log.error(f"Stop verification failed: {e}")

    last_email_day = None
    last_watchdog  = time.time()
    cycle = 0

    while True:
        try:
            cycle += 1
            log.info(f"\n{'─'*50}")
            log.info(f"Main cycle {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # Watchdog — log health every cycle so you can detect silent failures
            now_ts = time.time()
            last_watchdog = now_ts
            log.info(f"[WATCHDOG] Cycle {cycle} alive | Stocks P&L: ${state.daily_pnl:+.2f} | Crypto P&L: ${crypto_state.daily_pnl:+.2f} | Positions: {len(state.positions)}S/{len(crypto_state.positions)}C | API fails: {api_health['alpaca_fails']} | {datetime.now().strftime('%H:%M:%S')}")

            # Every 10 cycles: verify exchange stops AND reconcile positions with broker
            if cycle % 10 == 0:
                try:
                    # ── Stop order verification ──
                    open_orders = alpaca_get("/v2/orders?status=open") or []
                    stop_syms = {o["symbol"] for o in open_orders if o.get("type") == "stop"}
                    for sym, pos in state.positions.items():
                        if sym not in stop_syms:
                            log.warning(f"[WATCHDOG] Exchange stop missing for {sym} — replacing")
                            stop_order = place_stop_order_alpaca(sym, pos["qty"], round(pos["stop_price"], 2))
                            if stop_order and stop_order.get("id"):
                                exchange_stops[sym] = stop_order["id"]

                    # ── Position reconciliation ──
                    broker_positions = alpaca_get("/v2/positions") or []
                    broker_syms = {p["symbol"] for p in broker_positions}
                    local_syms  = set(state.positions.keys())

                    # Positions we think we have but broker doesn't
                    phantom = local_syms - broker_syms
                    for sym in phantom:
                        log.warning(f"[RECONCILE] {sym} in local state but NOT on broker — removing phantom position")
                        del state.positions[sym]

                    # Positions broker has that we don't know about
                    unknown = broker_syms - local_syms
                    for p in broker_positions:
                        sym = p["symbol"]
                        if sym in unknown:
                            entry = float(p.get("avg_entry_price", 0))
                            qty   = float(p.get("qty", 0))
                            stop  = entry * (1 - STOP_LOSS_PCT / 100)
                            tp    = entry * (1 + TAKE_PROFIT_PCT / 100)
                            state.positions[sym] = {
                                "qty": qty, "entry_price": entry, "stop_price": stop,
                                "highest_price": entry, "take_profit_price": tp,
                                "entry_date": datetime.now().date().isoformat(),
                                "days_held": 0, "entry_ts": datetime.now().isoformat(),
                            }
                            log.warning(f"[RECONCILE] {sym} found on broker but missing locally — re-added (entry:${entry:.2f} stop:${stop:.2f})")
                            # Only place stop if not already tracked
                            if sym not in exchange_stops:
                                stop_order = place_stop_order_alpaca(sym, qty, round(stop, 2))
                                if stop_order and stop_order.get("id"):
                                    exchange_stops[sym] = stop_order["id"]
                                    log.info(f"[RECONCILE] Exchange stop placed for {sym} @ ${stop:.2f}")
                                else:
                                    log.warning(f"[RECONCILE] Could not place stop for {sym} — will retry next reconcile cycle")

                    if phantom or unknown:
                        log.info(f"[RECONCILE] Fixed {len(phantom)} phantom + {len(unknown)} missing positions")
                    else:
                        log.info(f"[RECONCILE] Positions in sync ({len(broker_syms)} on broker)")

                except Exception as e:
                    log.warning(f"[WATCHDOG] Reconciliation failed: {e}")

            # Refresh account info each cycle
            account_info = alpaca_get("/v2/account") or account_info

            # ── Dynamic limit scaling from live balances ──────
            # Reads actual balance from BOTH Alpaca and Binance.
            # Total portfolio = Alpaca + Binance combined.
            # Stock limits scale from Alpaca balance.
            # Crypto limits scale from Binance balance.
            # Deposit more to either → limits scale up automatically.
            # Never needs manual adjustment.
            if account_info:
                alpaca_pv = float(account_info.get("portfolio_value", 1000))

                # Fetch Binance balance — cached for 10 mins to minimise API calls
                # Only fetches when: not banned, 5 min buffer after ban, and cache expired
                binance_pv = _binance_balance_cache.get("value", 0.0)
                cache_age  = time.time() - _binance_balance_cache.get("ts", 0)
                ban_clear  = time.time() >= (_binance_ban_until + 300)  # 5 min buffer
                if USE_BINANCE and ban_clear and cache_age > 600:  # refresh every 10 mins
                    try:
                        fresh = binance_get_balance("USDT")
                        if fresh is not None:
                            binance_pv = fresh
                            _binance_balance_cache["value"] = fresh
                            _binance_balance_cache["ts"]    = time.time()
                    except:
                        pass

                # Total combined portfolio
                total_pv = alpaca_pv + binance_pv

                # Stock limits scale from Alpaca balance only
                alpaca_ratio = alpaca_pv / total_pv if total_pv > 0 else 1.0

                # Crypto limits scale from Binance balance only
                binance_ratio = binance_pv / total_pv if total_pv > 0 else 0.0

                global MAX_DAILY_LOSS, MAX_DAILY_SPEND, MAX_PORTFOLIO_EXPOSURE
                global DAILY_PROFIT_TARGET, MAX_TRADE_VALUE, CRYPTO_MAX_EXPOSURE
                global INTRADAY_MAX_TRADE, CRYPTO_INTRADAY_MAX_TRADE, SMALLCAP_MAX_TRADE

                # Global limits — based on total portfolio
                MAX_DAILY_LOSS         = total_pv * MAX_DAILY_LOSS_PCT / 100
                DAILY_PROFIT_TARGET    = total_pv * DAILY_PROFIT_TARGET_PCT / 100

                # Stock limits — proportional to Alpaca balance
                MAX_DAILY_SPEND        = alpaca_pv * MAX_DAILY_SPEND_PCT / 100
                MAX_PORTFOLIO_EXPOSURE = alpaca_pv * MAX_EXPOSURE_PCT / 100
                MAX_TRADE_VALUE        = alpaca_pv * MAX_TRADE_PCT / 100
                INTRADAY_MAX_TRADE     = alpaca_pv * 0.03
                SMALLCAP_MAX_TRADE     = alpaca_pv * 0.025

                # Crypto limits — proportional to Binance balance
                # If no Binance balance yet, use a small % of Alpaca as fallback
                crypto_base            = binance_pv if binance_pv > 0 else alpaca_pv * 0.20
                CRYPTO_MAX_EXPOSURE    = crypto_base * MAX_EXPOSURE_PCT / 100
                CRYPTO_INTRADAY_MAX_TRADE = crypto_base * 0.02

                log.info(
                    f"[SIZING] Alpaca:${alpaca_pv:,.2f} ({alpaca_ratio*100:.0f}%) + "
                    f"Binance:${binance_pv:,.2f} ({binance_ratio*100:.0f}%) = "
                    f"Total:${total_pv:,.2f} | "
                    f"StockTrade:${MAX_TRADE_VALUE:.0f} "
                    f"CryptoTrade:${CRYPTO_INTRADAY_MAX_TRADE:.0f} "
                    f"DailyLoss:${MAX_DAILY_LOSS:.0f}"
                )

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

            # Update near-miss price tracking
            update_near_miss_prices()

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
            if et.hour == 17 and et.minute < 2 and last_email_day != et.date():
                send_daily_summary()
                last_email_day = et.date()

            # Weekly near-miss report — every Sunday at 6pm ET
            if et.weekday() == 6 and et.hour == 18 and et.minute < 2:
                log.info("[WEEKLY] Generating near-miss analysis report...")
                threading.Thread(target=send_weekly_near_miss_email, daemon=True).start()

            # Run near-miss simulations daily at noon ET (after enough price history)
            if et.hour == 12 and et.minute < 2:
                threading.Thread(target=run_near_miss_simulations, daemon=True).start()

            # Intraday bots run on their own faster sub-cycle
            # Run 6 intraday cycles per 1 swing cycle
            intraday_cycles = CYCLE_SECONDS // INTRADAY_CYCLE_SECONDS
            for _ in range(intraday_cycles):
                run_intraday_cycle(US_WATCHLIST, intraday_state)
                # Hard ban check before every crypto intraday cycle
                # This is what was causing re-bans — 6 rapid scans right as ban expired
                if not (USE_BINANCE and time.time() < (_binance_ban_until + 300)):
                    run_crypto_intraday_cycle(CRYPTO_WATCHLIST, crypto_intraday_state)
                time.sleep(INTRADAY_CYCLE_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped")
            break
        except Exception as e:
            log.error(f"[CRASH] Error in main loop: {e}")
            log.error(f"[CRASH] Bot recovering — sleeping 30s then resuming")
            # On crash, verify exchange stops are still in place
            try:
                open_orders = alpaca_get("/v2/orders?status=open") or []
                stop_syms = {o["symbol"] for o in open_orders if o.get("type") == "stop"}
                for sym, pos in state.positions.items():
                    if sym not in stop_syms:
                        log.warning(f"[CRASH RECOVERY] Replacing missing stop for {sym}")
                        place_stop_order_alpaca(sym, pos["qty"], round(pos["stop_price"], 2))
            except: pass
            time.sleep(30)

if __name__ == "__main__":
    main()
