"""
AlphaBot — Automated Day Trading Bot
Trades US stocks + crypto via Alpaca API
Runs 24/7 on Railway.app
"""

import os
import time
import logging
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("AlphaBot")

# ── Config (set these as environment variables in Railway) ────
ALPACA_KEY    = os.environ.get("ALPACA_KEY",    "YOUR_API_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "YOUR_SECRET_KEY")
IS_LIVE       = os.environ.get("IS_LIVE",       "false").lower() == "true"
GMAIL_USER    = os.environ.get("GMAIL_USER",    "garrathholdstock@gmail.com")
GMAIL_PASS    = os.environ.get("GMAIL_PASS",    "YOUR_GMAIL_APP_PASSWORD")
EMAIL_TO      = "garrathholdstock@gmail.com"

ALPACA_BASE   = "https://api.alpaca.markets"   if IS_LIVE else "https://paper-api.alpaca.markets"
DATA_BASE     = "https://data.alpaca.markets"

# ── Safety settings ───────────────────────────────────────────
MAX_DAILY_LOSS     = 50.0    # $ — shut off if losses hit this
STOP_LOSS_PCT      = 2.0     # % — auto sell if position drops this much
MAX_POSITIONS      = 3       # max open positions at once
MAX_TRADE_VALUE    = 500.0   # $ — max spend per single trade
MAX_DAILY_SPEND    = 5000.0  # $ — max total buying per day
DAILY_PROFIT_TARGET= 2000.0  # $ — stop trading once up this much
CYCLE_SECONDS      = 60      # how often the bot runs

# ── Watchlists ────────────────────────────────────────────────
US_WATCHLIST = [
    # Mega cap tech
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","NFLX","ORCL","ADBE",
    # Semiconductors
    "AMD","INTC","QCOM","AVGO","MU","AMAT","LRCX","KLAC","TXN","MRVL",
    # Fintech & crypto-adjacent
    "COIN","HOOD","SQ","PYPL","SOFI","AFRM","UPST","NU","MARA","RIOT",
    # EV & clean energy
    "RIVN","LCID","NIO","XPEV","LI","BLNK","CHPT","PLUG","FCEL","BE",
    # AI & cloud
    "PLTR","AI","PATH","SNOW","DDOG","NET","CRWD","ZS","OKTA","MDB",
    # Healthcare & biotech
    "MRNA","BNTX","NVAX","HIMS","TDOC","ACCD","SDGR","RXRX","BEAM","SGEN",
    # Consumer & retail
    "SHOP","ETSY","ABNB","UBER","LYFT","DASH","RBLX","SNAP","PINS","YELP",
    # Energy
    "XOM","CVX","OXY","SLB","HAL","MPC","VLO","PSX","DVN","FANG",
    # ETFs
    "SPY","QQQ","IWM","ARKK","SOXL","TQQQ","SQQQ","GLD","SLV","UVXY",
    # High volatility
    "GME","AMC","SPCE","WKHS","NKLA","OPEN","DKNG","CLOV","WISH","LCID",
]

CRYPTO_WATCHLIST = [
    "BTC/USD","ETH/USD","SOL/USD","AVAX/USD","DOGE/USD","SHIB/USD",
    "LTC/USD","BCH/USD","LINK/USD","DOT/USD","UNI/USD","AAVE/USD",
    "XTZ/USD","BAT/USD","CRV/USD","GRT/USD","MKR/USD","MATIC/USD",
    "ALGO/USD","XLM/USD","SUSHI/USD","YFI/USD","ETH/BTC",
]

# ── State (resets daily) ──────────────────────────────────────
class BotState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.daily_pnl       = 0.0
        self.daily_spend     = 0.0
        self.positions       = {}   # { symbol: { qty, entry_price, stop_price } }
        self.trades          = []   # list of completed trades
        self.shutoff         = False
        self.last_reset_day  = datetime.now().date()
        log.info("=== Daily state reset ===")

    def check_reset(self):
        today = datetime.now().date()
        if today != self.last_reset_day:
            self.reset()

state    = BotState()
crypto_state = BotState()

# ── Alpaca API helpers ────────────────────────────────────────
HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

def alpaca_get(path, base=None):
    url = (base or ALPACA_BASE) + path
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {path} failed: {e}")
        return None

def alpaca_post(path, body):
    url = ALPACA_BASE + path
    try:
        r = requests.post(url, json=body, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"POST {path} failed: {e}")
        return None

def get_account():
    return alpaca_get("/v2/account")

# ── Market data ───────────────────────────────────────────────
def fetch_bars(symbol, limit=30, crypto=False):
    end   = datetime.utcnow()
    start = end - timedelta(days=60)
    start_str = start.strftime("%Y-%m-%d")
    end_str   = end.strftime("%Y-%m-%d")

    try:
        if crypto:
            encoded = requests.utils.quote(symbol, safe="")
            url = f"{DATA_BASE}/v1beta3/crypto/us/bars?symbols={encoded}&timeframe=1Day&start={start_str}&end={end_str}&limit={limit}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                return None
            data = r.json()
            bars = data.get("bars", {}).get(symbol, [])
        else:
            url = f"{DATA_BASE}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start_str}&end={end_str}&limit={limit}&feed=sip&adjustment=raw"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                log.debug(f"{symbol}: {r.json().get('message','error')}")
                return None
            bars = r.json().get("bars", [])

        return bars if bars and len(bars) >= 15 else None
    except Exception as e:
        log.debug(f"fetch_bars {symbol}: {e}")
        return None

def fetch_latest_price(symbol, crypto=False):
    try:
        if crypto:
            encoded = requests.utils.quote(symbol, safe="")
            url = f"{DATA_BASE}/v1beta3/crypto/us/latest/bars?symbols={encoded}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                return None
            return r.json().get("bars", {}).get(symbol, {}).get("c")
        else:
            url = f"{DATA_BASE}/v2/stocks/{symbol}/snapshot?feed=sip"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                return None
            data = r.json()
            return data.get("latestTrade", {}).get("p") or data.get("latestQuote", {}).get("ap")
    except Exception as e:
        log.debug(f"fetch_price {symbol}: {e}")
        return None

# ── Technical indicators ──────────────────────────────────────
def sma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    changes = changes[-period:]
    gains  = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_gain = sum(gains)  / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_signal(closes):
    s9   = sma(closes, 9)
    s21  = sma(closes, 21)
    p9   = sma(closes[:-1], 9)
    p21  = sma(closes[:-1], 21)
    rsi  = calc_rsi(closes)

    if None in (s9, s21, p9, p21, rsi):
        return "HOLD", s9, s21, rsi

    cross_up   = p9 <= p21 and s9 > s21
    cross_down = p9 >= p21 and s9 < s21

    if cross_up and rsi < 70:
        return "BUY", s9, s21, rsi
    if cross_down or rsi > 70:
        return "SELL", s9, s21, rsi
    return "HOLD", s9, s21, rsi

# ── Market hours ──────────────────────────────────────────────
def is_market_open():
    et  = datetime.now(ZoneInfo("America/New_York"))
    day = et.weekday()          # 0=Mon, 6=Sun
    if day >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 570 <= mins < 960    # 9:30–16:00 ET

# ── Order placement ───────────────────────────────────────────
def place_order(symbol, side, qty, crypto=False):
    tif = "gtc" if crypto else "day"
    body = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": tif,
    }
    result = alpaca_post("/v2/orders", body)
    if result:
        log.info(f"ORDER {side.upper()} {qty} {symbol} [{'CRYPTO' if crypto else 'STOCK'}]")
    return result

# ── Stop-loss checker ─────────────────────────────────────────
def check_stop_losses(st, crypto=False):
    for symbol, pos in list(st.positions.items()):
        live = fetch_latest_price(symbol, crypto=crypto)
        if not live:
            continue
        if live <= pos["stop_price"]:
            pnl = (live - pos["entry_price"]) * pos["qty"]
            log.warning(f"STOP-LOSS {symbol} @ ${live:.4f} | P&L: ${pnl:+.2f}")
            place_order(symbol, "sell", pos["qty"], crypto=crypto)
            del st.positions[symbol]
            st.daily_pnl += pnl
            st.trades.append({
                "symbol": symbol, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": "Stop-Loss",
                "time": datetime.now().strftime("%H:%M:%S")
            })
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"DAILY LOSS LIMIT HIT (${st.daily_pnl:.2f}). Shutting off.")
                st.shutoff = True

# ── Screener ──────────────────────────────────────────────────
def run_screener(watchlist, crypto=False):
    label = "CRYPTO" if crypto else "STOCKS"
    log.info(f"[{label}] Scanning {len(watchlist)} symbols...")
    results = []
    for symbol in watchlist:
        bars = fetch_bars(symbol, crypto=crypto)
        if not bars:
            continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        prev    = closes[-2] if len(closes) > 1 else price
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, s9, s21, rsi = get_signal(closes)
        results.append({
            "symbol": symbol, "price": price, "change": change,
            "signal": signal, "sma9": s9, "sma21": s21,
            "rsi": rsi, "vol_ratio": vol_ratio
        })

    results.sort(key=lambda x: {"BUY": 0, "HOLD": 1, "SELL": 2}[x["signal"]])
    buys = [r for r in results if r["signal"] == "BUY"]
    log.info(f"[{label}] Scan complete — {len(buys)} BUY / {len(results)} total")
    return results

# ── Main bot cycle ────────────────────────────────────────────
def run_cycle(watchlist, st, crypto=False):
    label = "CRYPTO" if crypto else "STOCKS"

    # Daily reset check
    st.check_reset()

    if st.shutoff:
        log.info(f"[{label}] Shutoff active — skipping cycle")
        return

    # Market hours check for stocks only
    if not crypto and not is_market_open():
        et = datetime.now(ZoneInfo("America/New_York"))
        log.info(f"[{label}] Market closed ({et.strftime('%H:%M ET')}) — skipping")
        return

    log.info(f"[{label}] Running cycle... P&L today: ${st.daily_pnl:+.2f}")

    # 1. Check stop-losses
    check_stop_losses(st, crypto=crypto)

    if st.shutoff:
        return

    # 2. Scan market
    candidates = run_screener(watchlist, crypto=crypto)

    # 3. Open BUY positions
    pos_count = len(st.positions)
    for stock in candidates:
        if stock["signal"] != "BUY":
            continue
        if pos_count >= MAX_POSITIONS:
            log.info(f"[{label}] Max positions ({MAX_POSITIONS}) reached")
            break
        if stock["symbol"] in st.positions:
            continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET:
            log.info(f"[{label}] Daily profit target hit (${st.daily_pnl:.2f}) — no new buys")
            break
        if st.daily_spend >= MAX_DAILY_SPEND:
            log.info(f"[{label}] Daily spend limit hit (${st.daily_spend:.2f}) — no new buys")
            break

        price = stock["price"]
        if crypto:
            qty = max(0.0001, round(MAX_TRADE_VALUE / price, 6))
        else:
            qty = max(1, int(MAX_TRADE_VALUE / price))

        trade_value = qty * price
        if st.daily_spend + trade_value > MAX_DAILY_SPEND:
            log.info(f"[{label}] Skipping {stock['symbol']} — would exceed daily spend")
            continue

        stop_price = price * (1 - STOP_LOSS_PCT / 100)
        log.info(
            f"[{label}] BUY {stock['symbol']} @ ${price:.4f} "
            f"x{qty} = ${trade_value:.0f} | "
            f"Stop: ${stop_price:.4f} | RSI: {stock['rsi']:.1f}"
        )
        order = place_order(stock["symbol"], "buy", qty, crypto=crypto)
        if order:
            st.positions[stock["symbol"]] = {
                "qty": qty,
                "entry_price": price,
                "stop_price": stop_price
            }
            st.daily_spend += trade_value
            st.trades.append({
                "symbol": stock["symbol"], "side": "BUY", "qty": qty,
                "price": price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S")
            })
            pos_count += 1

    # 4. Close SELL positions
    for stock in candidates:
        if stock["signal"] != "SELL":
            continue
        if stock["symbol"] not in st.positions:
            continue

        pos = st.positions[stock["symbol"]]
        pnl = (stock["price"] - pos["entry_price"]) * pos["qty"]
        log.info(
            f"[{label}] SELL {stock['symbol']} @ ${stock['price']:.4f} "
            f"| P&L: ${pnl:+.2f}"
        )
        order = place_order(stock["symbol"], "sell", pos["qty"], crypto=crypto)
        if order:
            del st.positions[stock["symbol"]]
            st.daily_pnl += pnl
            st.trades.append({
                "symbol": stock["symbol"], "side": "SELL", "qty": pos["qty"],
                "price": stock["price"], "pnl": pnl, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S")
            })

            if st.daily_pnl >= DAILY_PROFIT_TARGET:
                log.info(f"[{label}] Daily profit target hit! ${st.daily_pnl:.2f} — stopping for today")
                st.shutoff = True
                break
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"[{label}] Daily loss limit hit! ${st.daily_pnl:.2f} — stopping for today")
                st.shutoff = True
                break

# ── Daily email summary ───────────────────────────────────────
def send_daily_summary():
    account = get_account()
    portfolio = float(account.get("portfolio_value", 0)) if account else 0

    def trade_summary(st, label):
        sells  = [t for t in st.trades if t["side"] == "SELL" and t["pnl"] is not None]
        wins   = [t for t in sells if t["pnl"] > 0]
        losses = [t for t in sells if t["pnl"] <= 0]
        lines  = "\n".join(
            f"  {t['time']}  {t['side']:4}  {t['symbol']:10}  ${t['price']:.4f}"
            f"{'  P&L: ' + ('+' if t['pnl'] >= 0 else '') + f'${t[\"pnl\"]:.2f}' if t['pnl'] is not None else ''}"
            f"  [{t['reason']}]"
            for t in st.trades[-20:]
        ) or "  No trades today"
        return (
            f"{label}\n"
            f"{'─'*40}\n"
            f"Daily P&L:      ${st.daily_pnl:+.2f}\n"
            f"Trades:         {len(sells)}\n"
            f"Wins/Losses:    {len(wins)}/{len(losses)}\n"
            f"Win rate:       {int(len(wins)/len(sells)*100) if sells else 0}%\n"
            f"Open positions: {len(st.positions)}\n\n"
            f"Trade log:\n{lines}\n"
        )

    body = f"""
AlphaBot Daily Summary
{'='*40}
Date:           {datetime.now().strftime('%A, %d %B %Y')}
Mode:           {'LIVE' if IS_LIVE else 'Paper'} Trading
Portfolio:      ${portfolio:,.2f}

{trade_summary(state, 'US STOCKS')}

{trade_summary(crypto_state, 'CRYPTO')}

{'='*40}
Settings:
  Stop-loss:        {STOP_LOSS_PCT}%
  Daily loss limit: ${MAX_DAILY_LOSS}
  Max per trade:    ${MAX_TRADE_VALUE}
  Daily spend cap:  ${MAX_DAILY_SPEND}
  Profit target:    ${DAILY_PROFIT_TARGET}
{'='*40}
Sent by AlphaBot running on Railway
""".strip()

    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = f"AlphaBot Daily Summary — {datetime.now().strftime('%d %b %Y')}"
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Daily summary emailed to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email failed: {e}")

# ── Main loop ─────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("AlphaBot starting up")
    log.info(f"Mode:    {'LIVE' if IS_LIVE else 'PAPER'} trading")
    log.info(f"Stocks:  {len(US_WATCHLIST)} symbols")
    log.info(f"Crypto:  {len(CRYPTO_WATCHLIST)} pairs")
    log.info(f"Cycle:   every {CYCLE_SECONDS}s")
    log.info(f"Safety:  stop-loss={STOP_LOSS_PCT}% | daily-loss=${MAX_DAILY_LOSS} | profit-target=${DAILY_PROFIT_TARGET}")
    log.info("=" * 50)

    # Verify connection
    account = get_account()
    if not account:
        log.error("Cannot connect to Alpaca — check your API keys in Railway environment variables")
        return
    log.info(f"Connected — Portfolio: ${float(account.get('portfolio_value',0)):,.2f}")

    last_email_day = None
    cycle = 0

    while True:
        try:
            cycle += 1
            now = datetime.now()
            log.info(f"─── Cycle {cycle} | {now.strftime('%Y-%m-%d %H:%M:%S')} ───")

            # Run stock bot
            run_cycle(US_WATCHLIST, state, crypto=False)

            # Run crypto bot (always runs, 24/7)
            run_cycle(CRYPTO_WATCHLIST, crypto_state, crypto=True)

            # Send daily summary email at 5pm ET on weekdays
            et = datetime.now(ZoneInfo("America/New_York"))
            if (et.weekday() < 5
                    and et.hour == 17
                    and et.minute < 2
                    and last_email_day != et.date()):
                send_daily_summary()
                last_email_day = et.date()

            log.info(f"Cycle done. Sleeping {CYCLE_SECONDS}s...\n")
            time.sleep(CYCLE_SECONDS)

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            break
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")
            time.sleep(30)  # wait 30s then retry

if __name__ == "__main__":
    main()
