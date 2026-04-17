"""
core/config.py — AlphaBot Configuration
All constants, environment variables, watchlists, and shared global state.
"""

import os
import time
import logging
import hashlib as _hashlib
from datetime import datetime
from dotenv import load_dotenv 
load_dotenv()

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("AlphaBot")

# ── API Keys & Auth ───────────────────────────────────────────
IS_LIVE        = os.environ.get("IS_LIVE",       "false").lower() == "true"
GMAIL_USER     = os.environ.get("GMAIL_USER",    "garrathholdstock@gmail.com")
GMAIL_PASS     = os.environ.get("GMAIL_PASS",    "YOUR_GMAIL_APP_PASSWORD")
EMAIL_TO       = "garrathholdstock@gmail.com"
PORT           = int(os.environ.get("PORT", 8080))
DASH_USER      = os.environ.get("DASH_USER", "alpha")
DASH_PASS      = os.environ.get("DASH_PASS", "bot123")
KILL_PIN       = os.environ.get("KILL_PIN", "1234")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "")
DASH_TOKEN     = _hashlib.md5(f"{DASH_USER}:{DASH_PASS}:alphabot".encode()).hexdigest()

NEWS_API_KEY   = os.environ.get("NEWS_API_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

# ── IBKR Connection ───────────────────────────────────────────
# IB Gateway Docker runs socat on port 4004 → paper port 4002 internally
# Port 4001 = live trading
IBKR_HOST      = "127.0.0.1"
IBKR_PORT      = 4001 if IS_LIVE else 4004
IBKR_CLIENT_ID = 1

# ── Binance ───────────────────────────────────────────────────
BINANCE_KEY            = os.environ.get("BINANCE_KEY",    "") or os.environ.get("BINANCE_TESTNET_KEY", "")
BINANCE_SECRET         = os.environ.get("BINANCE_SECRET", "") or os.environ.get("BINANCE_TESTNET_SECRET", "")
BINANCE_USE_TESTNET    = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"

if BINANCE_USE_TESTNET and BINANCE_KEY:
    BINANCE_BASE = "https://testnet.binance.vision"
    _BIN_KEY     = BINANCE_KEY
    _BIN_SECRET  = BINANCE_SECRET
    USE_BINANCE  = True
    log.info("[BINANCE] Using TESTNET")
elif BINANCE_KEY:
    BINANCE_BASE = "https://api.binance.com"
    _BIN_KEY     = BINANCE_KEY
    _BIN_SECRET  = BINANCE_SECRET
    USE_BINANCE  = True
else:
    BINANCE_BASE = "https://api.binance.com"
    _BIN_KEY     = ""
    _BIN_SECRET  = ""
    USE_BINANCE  = False

BINANCE_DELAY      = 0.5
_last_binance_call = 0.0
_binance_ban_until = 0.0

# ── Persist Binance ban state across restarts ─────────────────
_BAN_FILE = "/tmp/binance_ban.txt"

def _load_ban_from_disk():
    global _binance_ban_until
    try:
        with open(_BAN_FILE, "r") as f:
            saved = float(f.read().strip())
            if saved > time.time():
                _binance_ban_until = saved
                mins = int((saved - time.time()) / 60)
                print(f"[BINANCE] Loaded ban from disk — {mins} minutes remaining")
    except:
        pass

def _save_ban_to_disk(expiry):
    try:
        with open(_BAN_FILE, "w") as f:
            f.write(str(expiry))
    except:
        pass

_load_ban_from_disk()

# ── Account Size & Risk Limits ────────────────────────────────
STARTING_BALANCE        = float(os.getenv("STARTING_BALANCE", "1000.0"))

MAX_DAILY_LOSS_PCT      = 0.5
MAX_DAILY_SPEND_PCT     = 50.0
MAX_EXPOSURE_PCT        = 30.0
DAILY_PROFIT_TARGET_PCT = 2.0
MAX_TRADE_PCT           = 5.0
CRYPTO_EXPOSURE_PCT     = 20.0
INTRADAY_TRADE_PCT      = 3.0
CRYPTO_INTRADAY_PCT     = 2.0
SMALLCAP_TRADE_PCT      = 2.5

MAX_DAILY_LOSS         = STARTING_BALANCE * MAX_DAILY_LOSS_PCT / 100
MAX_DAILY_SPEND        = STARTING_BALANCE * MAX_DAILY_SPEND_PCT / 100
MAX_PORTFOLIO_EXPOSURE = STARTING_BALANCE * MAX_EXPOSURE_PCT / 100
DAILY_PROFIT_TARGET    = STARTING_BALANCE * DAILY_PROFIT_TARGET_PCT / 100
MAX_TRADE_VALUE        = STARTING_BALANCE * MAX_TRADE_PCT / 100
CRYPTO_MAX_EXPOSURE    = STARTING_BALANCE * CRYPTO_EXPOSURE_PCT / 100
INTRADAY_MAX_TRADE     = STARTING_BALANCE * INTRADAY_TRADE_PCT / 100
CRYPTO_INTRADAY_MAX_TRADE = STARTING_BALANCE * CRYPTO_INTRADAY_PCT / 100
SMALLCAP_MAX_TRADE     = STARTING_BALANCE * SMALLCAP_TRADE_PCT / 100

# ── Stop / Trail / Take Profit ────────────────────────────────
STOP_LOSS_PCT       = 5.0
TRAILING_STOP_PCT   = 2.0
TRAIL_TRIGGER_PCT   = 3.0
TAKE_PROFIT_PCT     = 10.0
MAX_HOLD_DAYS       = 5
GAP_DOWN_PCT        = 3.0
CRYPTO_STOP_PCT     = 4.0
CRYPTO_TRAIL_PCT    = 3.0

# ── Position & Trade Limits ───────────────────────────────────
MAX_POSITIONS             = int(os.getenv("MAX_POSITIONS", "3"))
MAX_TOTAL_POSITIONS       = int(os.getenv("MAX_TOTAL_POSITIONS", "15"))
MAX_TRADES_PER_DAY        = int(os.getenv("MAX_TRADES_PER_DAY", "50"))
CYCLE_SECONDS             = 60
INTRADAY_CYCLE_SECONDS    = 10

# ── Risk-based sizing ─────────────────────────────────────────
RISK_PER_TRADE_PCT  = 1.0

# ── Signal threshold ──────────────────────────────────────────
# 5 = paper trading; raise to 7+ before going live
MIN_SIGNAL_SCORE    = int(os.getenv("MIN_SIGNAL_SCORE", "5"))

# ── News boost ────────────────────────────────────────────────
NEWS_POSITIVE_BOOST = 1.5

# ── Loss streak settings ──────────────────────────────────────
LOSS_STREAK_LIMIT   = 3
LOSS_STREAK_PAUSE   = 7200

# ── VIX thresholds ────────────────────────────────────────────
VIX_LOW_THRESHOLD   = 15.0
VIX_HIGH_THRESHOLD  = 25.0
VIX_EXTREME         = 35.0
VIX_FEAR_THRESHOLD  = 25.0

# ── Market regime ─────────────────────────────────────────────
SPY_MA_PERIOD       = 20
BEAR_TICKERS        = []
SPY_FAST_DROP_PCT   = 3.0
SPY_CIRCUIT_BREAKER = 5.0
MACRO_KEYWORDS      = [
    "federal reserve","fed rate","interest rate","recession","inflation",
    "iran","war","sanctions","oil embargo","nuclear","geopolit",
    "bank collapse","credit crisis","market crash","circuit breaker",
    "emergency","black swan","systemic"
]

# ── Crypto regime ─────────────────────────────────────────────
BTC_MA_PERIOD       = 20
BTC_CRASH_PCT       = 5.0

# ── Intraday scanner settings ─────────────────────────────────
INTRADAY_TIMEFRAME      = "1Hour"
INTRADAY_BARS           = 48
INTRADAY_EMA_FAST       = 5
INTRADAY_EMA_SLOW       = 13
INTRADAY_RSI_LIMIT      = 75
INTRADAY_VOL_RATIO      = 1.5
INTRADAY_TAKE_PROFIT    = 2.5
INTRADAY_STOP_LOSS      = 1.0
INTRADAY_MAX_POSITIONS  = 2
INTRADAY_START_HOUR_ET  = 10
INTRADAY_END_HOUR_ET    = 15

# ── Crypto intraday ───────────────────────────────────────────
CRYPTO_INTRADAY_TIMEFRAME = "15Min"
CRYPTO_INTRADAY_BARS      = 96
CRYPTO_INTRADAY_EMA_FAST  = 5
CRYPTO_INTRADAY_EMA_SLOW  = 13
CRYPTO_INTRADAY_TP        = 2.0
CRYPTO_INTRADAY_SL        = 1.0
CRYPTO_INTRADAY_MAX_POS   = 2
CRYPTO_INTRADAY_VOL_RATIO = 1.5

# ── Small cap settings ────────────────────────────────────────
SMALLCAP_MIN_PRICE    = 2.0
SMALLCAP_MAX_PRICE    = 20.0
SMALLCAP_POOL_SIZE    = 50
SMALLCAP_STOP_LOSS    = 1.5
SMALLCAP_VOL_RATIO    = 2.0
SMALLCAP_REFRESH_DAYS = 7

# ── Slippage model ────────────────────────────────────────────
SLIPPAGE_STOCK  = 0.003
SLIPPAGE_CRYPTO = 0.005

# ── Rapid loss kill switch ────────────────────────────────────
RAPID_LOSS_COUNT   = 3
RAPID_LOSS_MINUTES = 15
RAPID_LOSS_AMOUNT  = 30.0

# ── Volume confirmation ───────────────────────────────────────
VOLUME_MIN_RATIO = 1.2

# ── Watchlists ────────────────────────────────────────────────
US_WATCHLIST = [
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","NFLX","ORCL","ADBE",
    "AMD","INTC","QCOM","AVGO","MU","AMAT","LRCX","KLAC","TXN","MRVL",
    "COIN","HOOD","PYPL","SOFI","AFRM","UPST","NU","MARA","RIOT",
    "RIVN","LCID","NIO","XPEV","LI","BLNK","CHPT","PLUG","BE",
    "PLTR","AI","PATH","SNOW","DDOG","NET","CRWD","ZS","OKTA","MDB",
    "MRNA","BNTX","NVAX","HIMS","TDOC","SDGR","RXRX","BEAM",
    "SHOP","ETSY","ABNB","UBER","LYFT","DASH","RBLX","SNAP","PINS","YELP",
    "XOM","CVX","OXY","SLB","HAL","MPC","VLO","PSX","DVN","FANG",
    "GME","AMC","SPCE","WKHS","OPEN","DKNG","CLOV",
]

CRYPTO_WATCHLIST_BINANCE = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT","LTCUSDT",
    "BCHUSDT","XLMUSDT","ATOMUSDT","ETCUSDT","NEARUSDT","ALGOUSDT",
    "WIFUSDT",
    "AAVEUSDT","UNIUSDT","MKRUSDT","CRVUSDT","GRTUSDT","SUSHIUSDT",
    "FETUSDT","AGIXUSDT","OCEANUSDT","WLDUSDT","ARKMUSDT",
    "ARBUSDT","OPUSDT","STRKUSDT","INJUSDT","APTUSDT","SUIUSDT",
    "AXSUSDT","SANDUSDT","MANAUSDT","GALAUSDT","IMXUSDT",
    "FILUSDT","ICPUSDT","RUNEUSDT","TIAUSDT","KASUSDT",
]

CRYPTO_WATCHLIST = CRYPTO_WATCHLIST_BINANCE

# ── Sector correlation map ────────────────────────────────────
SECTOR_MAP = {
    "NVDA":"SEMI","AMD":"SEMI","INTC":"SEMI","QCOM":"SEMI","AVGO":"SEMI",
    "MU":"SEMI","AMAT":"SEMI","LRCX":"SEMI","KLAC":"SEMI","TXN":"SEMI","MRVL":"SEMI",
    "AAPL":"BIGTECH","MSFT":"BIGTECH","GOOGL":"BIGTECH","AMZN":"BIGTECH",
    "META":"BIGTECH","NFLX":"BIGTECH","ORCL":"BIGTECH","ADBE":"BIGTECH",
    "TSLA":"EV","RIVN":"EV","LCID":"EV","NIO":"EV","XPEV":"EV","LI":"EV",
    "BLNK":"EV","CHPT":"EV","WKHS":"EV",
    "COIN":"CRYPTO_STOCK","MARA":"CRYPTO_STOCK","RIOT":"CRYPTO_STOCK","HOOD":"CRYPTO_STOCK",
    "PYPL":"FINTECH","SOFI":"FINTECH","AFRM":"FINTECH","UPST":"FINTECH","NU":"FINTECH",
    "PLTR":"AI","AI":"AI","PATH":"AI","SNOW":"AI","DDOG":"AI",
    "NET":"AI","CRWD":"AI","ZS":"AI","OKTA":"AI","MDB":"AI",
    "XOM":"ENERGY","CVX":"ENERGY","OXY":"ENERGY","SLB":"ENERGY","HAL":"ENERGY",
    "MPC":"ENERGY","VLO":"ENERGY","PSX":"ENERGY","DVN":"ENERGY","FANG":"ENERGY",
    "MRNA":"BIOTECH","BNTX":"BIOTECH","NVAX":"BIOTECH","HIMS":"BIOTECH",
    "TDOC":"BIOTECH","SDGR":"BIOTECH","RXRX":"BIOTECH","BEAM":"BIOTECH",
    "BTC/USD":"BTC","ETH/USD":"ETH","BTCUSDT":"BTC","ETHUSDT":"ETH",
}
MAX_SECTOR_POSITIONS = 1

# ── Shared bot state ──────────────────────────────────────────
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
        self.trades_today    = 0
        self.loss_cooldown   = {}

    def check_reset(self):
        today = datetime.now().date()
        if today != self.last_reset_day:
            log.info(f"[{self.label}] Daily reset")
            self.reset()

# Bot instances — imported by all other modules
state                 = BotState("STOCKS")
crypto_state          = BotState("CRYPTO")
smallcap_state        = BotState("SMALLCAP")
intraday_state        = BotState("INTRADAY")
crypto_intraday_state = BotState("CRYPTO_ID")
asx_state             = BotState("ASX")
ftse_state            = BotState("FTSE")
account_info          = {}

# ── Global risk state ─────────────────────────────────────────
global_risk = {
    "loss_streak":     0,
    "paused_until":    None,
    "total_positions": 0,
    "vix_level":       None,
}

# ── Performance analytics store ───────────────────────────────
perf = {
    "all_trades":     [],
    "peak_portfolio": 0.0,
    "max_drawdown":   0.0,
    "sharpe_daily":   [],
}

# ── Near-miss tracker ─────────────────────────────────────────
near_miss_tracker = {}

# ── Market regime ─────────────────────────────────────────────
market_regime = {
    "mode":       "BULL",
    "vix":        None,
    "spy_price":  None,
    "spy_ma20":   None,
    "spy_trend":  "unknown",
    "last_check": None,
}

# ── Circuit breaker ───────────────────────────────────────────
circuit_breaker = {
    "active":       False,
    "reason":       None,
    "triggered_at": None,
    "spy_open":     None,
    "macro_paused": False,
}

# ── Crypto regime ─────────────────────────────────────────────
crypto_regime = {
    "mode":       "BULL",
    "btc_price":  None,
    "btc_ma20":   None,
    "btc_change": None,
    "last_check": None,
}

# ── News sentiment state ──────────────────────────────────────
news_state = {
    "skip_list":      {},
    "watch_list":     {},
    "last_scan_day":  None,
    "last_scan_time": None,
    "briefing":       [],
    "scan_complete":  False,
}

# ── Kill switch ───────────────────────────────────────────────
kill_switch = {
    "active":       False,
    "reason":       "",
    "activated_at": None,
}

# ── Small cap pool ────────────────────────────────────────────
smallcap_pool = {
    "symbols":          [],
    "last_refresh":     None,
    "last_refresh_day": None,
}

# ── Exchange stop order tracking ──────────────────────────────
exchange_stops = {}

# ── Binance balance cache ─────────────────────────────────────
_binance_balance_cache = {"value": 0.0, "ts": 0}

# ── Live price cache (populated each cycle from IBKR portfolio) ───
live_prices = {}  # symbol -> float, updated every cycle from ib.portfolio()

# ── API health tracking ───────────────────────────────────────
api_health = {
    "ibkr_fails": 0,
    "data_fails":   0,
    "last_success": None,
    "max_fails":    5,
}

# ── Binance headers ───────────────────────────────────────────
BINANCE_HEADERS = {"X-MBX-APIKEY": _BIN_KEY}

# ── Database path ─────────────────────────────────────────────
DB_PATH = "/home/alphabot/app/alphabot.db"
CONFIG_JSON_PATH = "/home/alphabot/app/trading_config.json"

# ── Hot-reload trading params from config.json ────────────────
# Called at the start of each main cycle — changes apply within 60s
import json as _json

def load_trading_config():
    """Read trading_config.json and apply values to module globals.
    Called every main cycle so dashboard changes take effect without restart."""
    global MIN_SIGNAL_SCORE, MAX_POSITIONS, MAX_TOTAL_POSITIONS, MAX_TRADES_PER_DAY
    global CYCLE_SECONDS, STOP_LOSS_PCT, TRAILING_STOP_PCT, TAKE_PROFIT_PCT
    global MAX_HOLD_DAYS, CRYPTO_STOP_PCT, INTRADAY_STOP_LOSS, INTRADAY_TAKE_PROFIT
    global INTRADAY_MAX_POSITIONS, CRYPTO_INTRADAY_MAX_POS, CRYPTO_INTRADAY_SL, CRYPTO_INTRADAY_TP
    global MAX_DAILY_LOSS_PCT, MAX_DAILY_SPEND_PCT, MAX_EXPOSURE_PCT, DAILY_PROFIT_TARGET_PCT
    global MAX_TRADE_PCT, CRYPTO_EXPOSURE_PCT, MAX_SECTOR_POSITIONS
    global LOSS_STREAK_LIMIT, VIX_HIGH_THRESHOLD, VIX_EXTREME
    try:
        with open(CONFIG_JSON_PATH) as f:
            c = _json.load(f)
        MIN_SIGNAL_SCORE          = int(c.get("MIN_SIGNAL_SCORE", MIN_SIGNAL_SCORE))
        MAX_POSITIONS             = int(c.get("MAX_POSITIONS", MAX_POSITIONS))
        MAX_TOTAL_POSITIONS       = int(c.get("MAX_TOTAL_POSITIONS", MAX_TOTAL_POSITIONS))
        MAX_TRADES_PER_DAY        = int(c.get("MAX_TRADES_PER_DAY", MAX_TRADES_PER_DAY))
        CYCLE_SECONDS             = int(c.get("CYCLE_SECONDS", CYCLE_SECONDS))
        STOP_LOSS_PCT             = float(c.get("STOP_LOSS_PCT", STOP_LOSS_PCT))
        TRAILING_STOP_PCT         = float(c.get("TRAILING_STOP_PCT", TRAILING_STOP_PCT))
        TAKE_PROFIT_PCT           = float(c.get("TAKE_PROFIT_PCT", TAKE_PROFIT_PCT))
        MAX_HOLD_DAYS             = int(c.get("MAX_HOLD_DAYS", MAX_HOLD_DAYS))
        CRYPTO_STOP_PCT           = float(c.get("CRYPTO_STOP_PCT", CRYPTO_STOP_PCT))
        INTRADAY_STOP_LOSS        = float(c.get("INTRADAY_STOP_LOSS", INTRADAY_STOP_LOSS))
        INTRADAY_TAKE_PROFIT      = float(c.get("INTRADAY_TAKE_PROFIT", INTRADAY_TAKE_PROFIT))
        INTRADAY_MAX_POSITIONS    = int(c.get("INTRADAY_MAX_POSITIONS", INTRADAY_MAX_POSITIONS))
        CRYPTO_INTRADAY_MAX_POS   = int(c.get("CRYPTO_INTRADAY_MAX_POS", CRYPTO_INTRADAY_MAX_POS))
        CRYPTO_INTRADAY_SL        = float(c.get("CRYPTO_INTRADAY_SL", CRYPTO_INTRADAY_SL))
        CRYPTO_INTRADAY_TP        = float(c.get("CRYPTO_INTRADAY_TP", CRYPTO_INTRADAY_TP))
        MAX_DAILY_LOSS_PCT        = float(c.get("MAX_DAILY_LOSS_PCT", MAX_DAILY_LOSS_PCT))
        MAX_DAILY_SPEND_PCT       = float(c.get("MAX_DAILY_SPEND_PCT", MAX_DAILY_SPEND_PCT))
        MAX_EXPOSURE_PCT          = float(c.get("MAX_EXPOSURE_PCT", MAX_EXPOSURE_PCT))
        DAILY_PROFIT_TARGET_PCT   = float(c.get("DAILY_PROFIT_TARGET_PCT", DAILY_PROFIT_TARGET_PCT))
        MAX_TRADE_PCT             = float(c.get("MAX_TRADE_PCT", MAX_TRADE_PCT))
        CRYPTO_EXPOSURE_PCT       = float(c.get("CRYPTO_EXPOSURE_PCT", CRYPTO_EXPOSURE_PCT))
        MAX_SECTOR_POSITIONS      = int(c.get("MAX_SECTOR_POSITIONS", MAX_SECTOR_POSITIONS))
        LOSS_STREAK_LIMIT         = int(c.get("LOSS_STREAK_LIMIT", LOSS_STREAK_LIMIT))
        VIX_HIGH_THRESHOLD        = float(c.get("VIX_HIGH_THRESHOLD", VIX_HIGH_THRESHOLD))
        VIX_EXTREME               = float(c.get("VIX_EXTREME", VIX_EXTREME))
    except FileNotFoundError:
        pass  # config.json not yet created — use defaults
    except Exception as e:
        log.warning(f"[CONFIG] Failed to load trading_config.json: {e}")

# ── Binance interval map ──────────────────────────────────────
BINANCE_INTERVAL_MAP = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m", "30Min": "30m",
    "1Hour": "1h", "2Hour": "2h", "4Hour": "4h", "1Day": "1d",
}

# ── SPY closes cache ──────────────────────────────────────────
_spy_closes_cache = {"closes": [], "last_fetch": None}

# ── Binance lot size cache ────────────────────────────────────
_binance_lot_cache = {}

# ── Thread lock ───────────────────────────────────────────────
import threading as _threading
_state_lock = _threading.Lock()

# ── ASX Watchlist (ASX exchange, AUD) ─────────────────────────
ASX_WATCHLIST = [
    "CBA","NAB","WBC","ANZ","MQG",
    "BHP","RIO","FMG","MIN","S32",
    "CSL","RMD","COH","SHL","PME",
    "WTC","XRO","TLX","ALU","MP1",
    "WOW","COL","JBH","ARB","REH",
    "WDS","STO","BPT","KAR","WHC",
    "GMG","SCG","GPT","MGR","CHC",
    "TCL","QAN","AZJ","ORI","AMC",
]

# ── FTSE Watchlist (LSE exchange, GBP) ────────────────────────
FTSE_WATCHLIST = [
    "HSBA","LLOY","BARC","AV.","LGEN","PRU","STJ","HLMA",
    "SBRE","ABDN","OSB","NWG","MNG","JUP","ITRK",
    "SHEL","BP.","SSE","SGE","EXPN",
    "RIO","BHP","AAL","GLEN","FRES","MNDI",
    "ULVR","DGE","BATS","IMB","OCDO","ABF",
    "AZN","GSK","HLN","HIK","NXT",
    "BA.","RR.","IMI","WEIR","DCC","PSN","MTO",
    "AUTO","MKS","TSCO","SBRY","WPP","WTB",
    "JD.","BRBY","KGF","HMSO",
    "VOD","BT.A","SMIN","DPLM",
    "LAND","SGRO","BLND","BBOX","PHP","SUPR",
    "UU.","SVT","NG.","CNA",
    "REL","PSON","ITV",
    "CCH","TATE",
    "IAG","EZJ",
    "CRH","SMT","FCIT",
    "PETS","BKG","TW.","BWY",
]

# ── Market configs per exchange ───────────────────────────────
MARKET_CONFIG = {
    "US":   {"exchange": "SMART", "currency": "USD", "fx_pair": None},
    "ASX":  {"exchange": "ASX",   "currency": "AUD", "fx_pair": "AUD.USD"},
    "LSE":  {"exchange": "LSE",   "currency": "GBP", "fx_pair": "GBP.USD"},
}

# ── ASX/FTSE regime state dicts ───────────────────────────────
asx_regime = {
    "mode": "BULL", "spy": None, "ma20": None,
    "vix": None, "updated": None,
}
ftse_regime = {
    "mode": "BULL", "spy": None, "ma20": None,
    "vix": None, "updated": None,
}
