"""
core/execution.py — AlphaBot Order Execution & API Wrappers
Stocks: IB Gateway via ib_insync (Docker socat → port 4004 paper / 4001 live)
Crypto: Binance API (testnet or live)
Broker: IBKR only — no Alpaca.
"""

import time
import logging
import hashlib
import hmac
import urllib.parse
import requests
import asyncio
import nest_asyncio
from datetime import datetime, timedelta
from ib_insync import IB, Stock, MarketOrder, StopOrder, LimitOrder, util

nest_asyncio.apply()

from core.config import (
    log, IS_LIVE,
    IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID,
    BINANCE_BASE, BINANCE_HEADERS, _BIN_KEY, _BIN_SECRET,
    BINANCE_DELAY, BINANCE_INTERVAL_MAP, USE_BINANCE,
    SLIPPAGE_STOCK, SLIPPAGE_CRYPTO, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    _binance_lot_cache, _binance_balance_cache,
    api_health, kill_switch, exchange_stops,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    asx_state, ftse_state,
    _save_ban_to_disk,
)
import core.config as cfg

# ── Per-thread IB connection pool ────────────────────────────
# Each thread gets its own IB connection with a unique client ID.
# This avoids deadlocks when multiple threads call IBKR simultaneously.
# Client ID assignment:
#   1 = Main / account / watchdog
#   2 = US-Swing stocks
#   3 = Intraday US
#   4 = FTSE
#   5 = ASX
#   6 = Smallcap
#   7 = Crypto-Swing (fallback, rarely needs IBKR)
#   8+ = reserved

import threading as _threading

_THREAD_CLIENT_IDS = {
    "MainThread":    1,
    "US-Swing":      2,
    "Intraday":      3,
    "FTSE":          4,
    "ASX":           5,
    "Smallcap":      6,
    "Crypto-Swing":  7,
}
_DEFAULT_CLIENT_ID = 1

# Per-thread IB instance storage
_ib_pool = {}
_ib_pool_lock = _threading.Lock()

def _get_client_id():
    """Return the client ID for the current thread."""
    thread_name = _threading.current_thread().name
    return _THREAD_CLIENT_IDS.get(thread_name, _DEFAULT_CLIENT_ID)

def get_ib():
    """Get or create IB connection for the current thread.
    Each thread maintains its own dedicated connection."""
    import datetime as _dt
    # IBKR does weekend maintenance — don't attempt connections Sat/Sun
    # ASX opens Sunday 1am Paris (Sat 11pm UTC) so allow connections from 11pm UTC Saturday
    now_utc = _dt.datetime.utcnow()
    weekday = now_utc.weekday()  # 5=Sat, 6=Sun
    hour_utc = now_utc.hour
    is_weekend_maintenance = (
        (weekday == 5 and hour_utc < 23) or  # Saturday before 11pm UTC
        (weekday == 6 and hour_utc < 23)     # Sunday before 11pm UTC (Mon ASX opens)
    )
    if is_weekend_maintenance:
        return None

    thread_name = _threading.current_thread().name
    client_id = _get_client_id()

    with _ib_pool_lock:
        ib = _ib_pool.get(thread_name)

    # Check if existing connection is healthy
    if ib is not None:
        try:
            if ib.isConnected():
                return ib
        except:
            pass
        # Connection dead — clean it up
        try:
            ib.disconnect()
        except:
            pass

    # Create new connection for this thread
    try:
        ib = IB()
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=client_id, timeout=10)
        log.info(f"[IBKR] Thread '{thread_name}' connected (clientId={client_id})")
        with _ib_pool_lock:
            _ib_pool[thread_name] = ib
        return ib
    except Exception as e:
        log.error(f"[IBKR] Thread '{thread_name}' connection failed (clientId={client_id}): {e}")
        with _ib_pool_lock:
            _ib_pool.pop(thread_name, None)
        return None

def run_ib(coro):
    """Run an ib_insync coroutine synchronously."""
    ib = get_ib()
    if not ib:
        return None
    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(coro)
    except Exception as e:
        log.error(f"[IBKR] run_ib error: {e}")
        return None


# ── API health ────────────────────────────────────────────────
def record_api_success():
    api_health["ibkr_fails"] = 0
    api_health["data_fails"]  = 0
    api_health["last_success"] = datetime.now().isoformat()

def record_api_failure(source="ibkr"):
    key = f"{source}_fails"
    api_health[key] = api_health.get(key, 0) + 1
    total_fails = api_health.get("ibkr_fails", 0) + api_health.get("data_fails", 0)
    if total_fails >= api_health["max_fails"] and not kill_switch["active"]:
        kill_switch["active"]       = True
        kill_switch["reason"]       = f"API kill: {total_fails} consecutive failures ({source})"
        kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
        for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
                   asx_state, ftse_state]:
            st.shutoff = True
        log.error(f"[API KILL] {total_fails} consecutive API failures — all bots stopped")


# ── IBKR market data ──────────────────────────────────────────
def _make_contract(symbol, exchange="SMART", currency="USD"):
    return Stock(symbol, exchange, currency)

_INTL_MARKET = {}
try:
    from core.config import ASX_WATCHLIST, FTSE_WATCHLIST
    for s in ASX_WATCHLIST: _INTL_MARKET[s] = ("ASX", "AUD")
    for s in FTSE_WATCHLIST: _INTL_MARKET[s] = ("LSE", "GBP")
except Exception:
    pass

def _contract_for(symbol):
    exch, curr = _INTL_MARKET.get(symbol, ("SMART", "USD"))
    return _make_contract(symbol, exch, curr)

def fetch_bars(symbol, crypto=False):
    """Fetch daily bars. Crypto uses Binance, stocks use IBKR."""
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 300):
            return None
        bars = binance_fetch_bars(symbol, interval="1d", limit=35)
        return bars if bars and len(bars) >= 15 else None

    ib = get_ib()
    if not ib:
        record_api_failure("ibkr")
        return None
    try:
        contract = _contract_for(symbol)
        bars_ib = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="60 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=False,
        )
        if not bars_ib or len(bars_ib) < 15:
            return None
        record_api_success()
        return [{"t": b.date, "o": b.open, "h": b.high, "l": b.low,
                 "c": b.close, "v": b.volume} for b in bars_ib]
    except Exception as e:
        log.warning(f"[IBKR] fetch_bars {symbol}: {e}")
        record_api_failure("ibkr")
        return None

def fetch_bars_batch(symbols, limit=30):
    if not symbols:
        return {}
    results = {}
    for symbol in symbols:
        bars = fetch_bars(symbol)
        if bars:
            results[symbol] = bars
        time.sleep(0.1)
    log.info(f"[IBKR BATCH] Fetched bars for {len(results)}/{len(symbols)} symbols")
    return results

def fetch_latest_price(symbol, crypto=False):
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 120):
            return None
        return binance_fetch_price(symbol)

    ib = get_ib()
    if not ib:
        return None
    try:
        contract = _contract_for(symbol)
        ticker = ib.reqMktData(contract, "", True, False)
        ib.sleep(0.5)
        price = ticker.last or ticker.close or ticker.bid or ticker.ask
        ib.cancelMktData(contract)
        if price and price > 0:
            record_api_success()
            return float(price)
        return None
    except Exception as e:
        log.warning(f"[IBKR] fetch_latest_price {symbol}: {e}")
        return None

def fetch_intraday_bars(symbol, timeframe="1Hour", limit=48, crypto=False):
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 300):
            return None
        binance_tf = BINANCE_INTERVAL_MAP.get(timeframe, "15m")
        bars = binance_fetch_bars(symbol, interval=binance_tf, limit=limit)
        return bars if bars and len(bars) >= 10 else None

    ib = get_ib()
    if not ib:
        return None
    try:
        contract = _contract_for(symbol)
        bar_size = _timeframe_to_ibkr(timeframe)
        duration = _limit_to_duration(limit, timeframe)
        bars_ib = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=False,
        )
        if not bars_ib or len(bars_ib) < 10:
            return None
        record_api_success()
        return [{"t": b.date, "o": b.open, "h": b.high, "l": b.low,
                 "c": b.close, "v": b.volume} for b in bars_ib]
    except Exception as e:
        log.warning(f"[IBKR] fetch_intraday_bars {symbol}: {e}")
        return None

def fetch_intraday_bars_batch(symbols, timeframe="1Hour", limit=48):
    if not symbols:
        return {}
    results = {}
    for symbol in symbols:
        bars = fetch_intraday_bars(symbol, timeframe=timeframe, limit=limit)
        if bars:
            results[symbol] = bars
        time.sleep(0.1)
    return results

def _timeframe_to_ibkr(timeframe):
    mapping = {
        "1Min":  "1 min",  "5Min":  "5 mins", "15Min": "15 mins",
        "30Min": "30 mins", "1Hour": "1 hour", "2Hour": "2 hours",
        "4Hour": "4 hours", "1Day":  "1 day",
    }
    return mapping.get(timeframe, "1 hour")

def _limit_to_duration(limit, timeframe):
    mins_map = {
        "1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30,
        "1Hour": 60, "2Hour": 120, "4Hour": 240, "1Day": 1440,
    }
    mins = mins_map.get(timeframe, 60)
    total_mins = mins * limit
    if total_mins <= 480:    return f"{max(1, total_mins // 60 + 1)} D"
    elif total_mins <= 2400: return f"{max(1, total_mins // 480 + 1)} D"
    else:                    return "5 D"


# ── IBKR account info ─────────────────────────────────────────
def ibkr_get_account():
    ib = get_ib()
    if not ib:
        return {}
    try:
        summary = ib.accountSummary()
        result = {}
        for item in summary:
            if item.tag == "NetLiquidation":
                result["portfolio_value"] = float(item.value)
            elif item.tag == "TotalCashValue":
                result["cash"] = float(item.value)
            elif item.tag == "GrossPositionValue":
                result["long_market_value"] = float(item.value)
        result["last_equity"] = result.get("portfolio_value", 0)
        # Update live prices from portfolio push (no market data subscription needed)
        try:
            for item in ib.portfolio():
                sym = item.contract.symbol
                price = item.marketPrice
                if price and price > 0:
                    cfg.live_prices[sym] = float(price)
        except Exception:
            pass
        record_api_success()
        return result
    except Exception as e:
        log.warning(f"[IBKR] get_account: {e}")
        record_api_failure("ibkr")
        return {}

def ibkr_get_positions():
    ib = get_ib()
    if not ib:
        return []
    try:
        positions = ib.positions()
        result = []
        for pos in positions:
            if pos.contract.secType == "STK":
                result.append({
                    "symbol":          pos.contract.symbol,
                    "qty":             str(pos.position),
                    "avg_entry_price": str(pos.avgCost),
                    "asset_class":     "us_equity",
                })
        return result
    except Exception as e:
        log.warning(f"[IBKR] get_positions: {e}")
        return []

def ibkr_get_open_orders():
    ib = get_ib()
    if not ib:
        return []
    try:
        trades = ib.openTrades()
        result = []
        for trade in trades:
            result.append({
                "symbol":     trade.contract.symbol,
                "type":       trade.order.orderType.lower(),
                "order_type": trade.order.orderType.upper(),
                "id":         trade.order.orderId,
                "order_id":   trade.order.orderId,
            })
        return result
    except Exception as e:
        log.warning(f"[IBKR] get_open_orders: {e}")
        return []


# ── IBKR stop order management ────────────────────────────────
def place_stop_order_ibkr(symbol, qty, stop_price):
    ib = get_ib()
    if not ib:
        return None
    try:
        contract = _contract_for(symbol)
        order    = StopOrder("SELL", qty, stop_price)
        trade    = ib.placeOrder(contract, order)
        ib.sleep(0.5)
        order_id = trade.order.orderId
        log.info(f"[IBKR STOP] Placed stop for {symbol} @ ${stop_price:.2f} id:{order_id}")
        return {"id": order_id, "symbol": symbol, "status": "accepted"}
    except Exception as e:
        log.warning(f"[IBKR] place_stop_order {symbol}: {e}")
        return None

def cancel_stop_order_ibkr(order_id):
    ib = get_ib()
    if not ib:
        return False
    try:
        open_trades = ib.openTrades()
        for trade in open_trades:
            if trade.order.orderId == order_id:
                ib.cancelOrder(trade.order)
                ib.sleep(0.3)
                log.info(f"[IBKR] Cancelled order {order_id}")
                return True
        return False
    except Exception as e:
        log.warning(f"[IBKR] cancel_order {order_id}: {e}")
        return False

def update_exchange_stop(symbol, qty, new_stop_price):
    old_id = exchange_stops.get(symbol)
    if old_id:
        cancel_stop_order_ibkr(old_id)
    new_order = place_stop_order_ibkr(symbol, qty, round(new_stop_price, 2))
    if new_order and new_order.get("id"):
        exchange_stops[symbol] = new_order["id"]
        log.info(f"[TRAIL] Updated exchange stop {symbol} → ${new_stop_price:.2f}")


# ── Binance API ───────────────────────────────────────────────
def _binance_sign(params):
    query = urllib.parse.urlencode(params)
    sig   = hmac.new(_BIN_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig

def _binance_ts():
    return int(time.time() * 1000)

def binance_get(path, params=None, signed=False):
    now_ts = time.time()
    ban_clear_at = cfg._binance_ban_until + 120
    if now_ts < ban_clear_at:
        remaining = int(cfg._binance_ban_until - now_ts)
        if remaining > 0 and remaining % 60 < 2:
            log.warning(f"[BINANCE] Ban active — {remaining}s remaining")
        return None

    elapsed = time.time() - cfg._last_binance_call
    if elapsed < BINANCE_DELAY:
        time.sleep(BINANCE_DELAY - elapsed)
    cfg._last_binance_call = time.time()

    try:
        p = params or {}
        if signed:
            p["timestamp"] = _binance_ts()
            url = f"{BINANCE_BASE}{path}?{_binance_sign(p)}"
        else:
            url = f"{BINANCE_BASE}{path}" + (f"?{urllib.parse.urlencode(p)}" if p else "")
        r = requests.get(url, headers=BINANCE_HEADERS, timeout=10)
        if not r.ok:
            if r.status_code in (418, 429):
                retry_after = int(r.headers.get("Retry-After", 120))
                cfg._binance_ban_until = time.time() + retry_after
                _save_ban_to_disk(cfg._binance_ban_until)
                log.warning(f"[BINANCE] Rate limited — banned for {retry_after}s")
                try:
                    from app.notifications import tg
                    tg(f"⚠️ <b>Binance Ban</b>\nDuration: {retry_after}s", category="binance_ban")
                except:
                    pass
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
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data or len(data) < 10:
        return None
    return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5])} for k in data]

def binance_fetch_price(symbol):
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"]) if data and "price" in data else None

def binance_get_lot_size(symbol):
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
    except:
        pass
    return 0.0001, 0.0001

def round_step(qty, step):
    import math
    if step <= 0: return qty
    precision = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty / step) * step, precision)

def binance_place_order(symbol, side, usdt_amount):
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
        "symbol":           symbol,
        "side":             side.upper(),
        "type":             "MARKET",
        "quantity":         str(qty),
        "newOrderRespType": "FULL",
    })
    if result:
        fills = result.get("fills", [])
        if fills:
            total_qty   = sum(float(f["qty"]) for f in fills)
            total_value = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            real_fill   = total_value / total_qty if total_qty > 0 else price
            slip_pct    = ((real_fill - price) / price * 100)
            log.info(f"[BINANCE] ORDER {side.upper()} {qty} {symbol} | signal=${price:.4f} fill=${real_fill:.4f} slippage={slip_pct:+.3f}%")
            result["_real_fill_price"] = real_fill
    return result

def binance_get_balance(asset="USDT"):
    data = binance_get("/api/v3/account", signed=True)
    if not data: return 0.0
    for b in data.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0

def binance_get_top_coins(limit=100):
    if time.time() < (cfg._binance_ban_until + 300):
        from core.config import CRYPTO_WATCHLIST_BINANCE
        return CRYPTO_WATCHLIST_BINANCE
    tickers = binance_get("/api/v3/ticker/24hr")
    if not tickers:
        from core.config import CRYPTO_WATCHLIST_BINANCE
        return CRYPTO_WATCHLIST_BINANCE
    usdt = [t for t in tickers
            if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume", 0)) > 1_000_000
            and not any(bad in t["symbol"] for bad in ["UP","DOWN","BEAR","BULL","LEVERAGE"])]
    usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    top = [t["symbol"] for t in usdt[:limit]]
    log.info(f"[BINANCE] Top {len(top)} coins by volume fetched")
    from core.config import CRYPTO_WATCHLIST_BINANCE
    return top if top else CRYPTO_WATCHLIST_BINANCE


# ── Slippage & fill price ─────────────────────────────────────
def apply_slippage(price, side, crypto=False):
    slippage = SLIPPAGE_CRYPTO if crypto else SLIPPAGE_STOCK
    if side == "buy":
        return price * (1 + slippage)
    else:
        return price * (1 - slippage)

def get_actual_fill_price(order_result, side, estimated_price, crypto=False):
    if not order_result or not estimated_price:
        return apply_slippage(estimated_price or 0, side, crypto)
    if not IS_LIVE:
        return apply_slippage(estimated_price, side, crypto)
    fills = order_result.get("fills", [])
    if fills:
        total_qty   = sum(float(f["qty"]) for f in fills)
        total_value = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        if total_qty > 0:
            fp = total_value / total_qty
            log.info(f"[FILL] Binance fill: ${fp:.4f} vs signal: ${estimated_price:.4f}")
            return fp
    ibkr_fill = order_result.get("_ibkr_fill_price")
    if ibkr_fill and float(ibkr_fill) > 0:
        return float(ibkr_fill)
    return apply_slippage(estimated_price, side, crypto)

def is_order_filled(order_result):
    if not order_result: return False
    status = order_result.get("status", "")
    if status in ("filled", "partially_filled", "FILLED", "PARTIALLY_FILLED", "accepted"): return True
    if order_result.get("id") or order_result.get("orderId"): return True
    return False


# ── Main order placement ──────────────────────────────────────
def place_order(symbol, side, qty, crypto=False, estimated_price=None, order_type=None, stop_price=None):
    """Place order via Binance (crypto) or IBKR (stocks).
    Returns (order_result, actual_fill_price)."""

    # ── Crypto via Binance ──
    if crypto and USE_BINANCE:
        price  = estimated_price or binance_fetch_price(symbol)
        usdt   = float(qty) * price if price else float(qty)
        result = binance_place_order(symbol, side, usdt)
        if result and "_real_fill_price" in result:
            return result, result["_real_fill_price"]
        fill_price = get_actual_fill_price(result, side, price or 0, crypto=True)
        return result, fill_price

    # ── Stocks via IBKR ──
    ib = get_ib()
    if not ib:
        log.error(f"[IBKR] Cannot place order — not connected")
        return None, estimated_price or 0

    try:
        contract = _contract_for(symbol)
        ib_side  = "BUY" if side.lower() == "buy" else "SELL"

        # ── Short-sell prevention guard ──
        if ib_side == "SELL":
            held = 0
            for pos in ib.positions():
                if pos.contract.symbol == symbol:
                    held = pos.position
                    break
            if held <= 0:
                log.warning(f"[IBKR] SHORT-SELL BLOCKED: {symbol} — no long position (held={held})")
                return None, estimated_price or 0
            qty = min(qty, held)

        if order_type and "STP" in order_type.upper() and stop_price:
            order = StopOrder(ib_side, qty, stop_price)
            trade = ib.placeOrder(contract, order)
            ib.sleep(0.5)
            result = {"id": trade.order.orderId, "symbol": symbol, "status": "accepted"}
            return result, stop_price

        if IS_LIVE and estimated_price:
            vix_now      = cfg.global_risk.get("vix_level") or 20
            signal_score = getattr(place_order, "_last_score", 5)
            if signal_score >= 9:   base_tol = 0.010
            elif signal_score >= 7: base_tol = 0.006
            else:                   base_tol = 0.003
            if vix_now >= 30:   vix_adj = 0.004
            elif vix_now >= 20: vix_adj = 0.002
            else:               vix_adj = 0.0
            tolerance   = base_tol + vix_adj
            if side.lower() == "buy":
                limit_price = round(estimated_price * (1 + tolerance), 2)
            else:
                limit_price = round(estimated_price * (1 - tolerance), 2)
            order = LimitOrder(ib_side, qty, limit_price)
            log.info(f"[IBKR LIMIT] {ib_side} {symbol} x{qty} limit:${limit_price:.2f} signal:${estimated_price:.2f}")
        else:
            order = MarketOrder(ib_side, qty)
            log.info(f"[IBKR MARKET] {ib_side} {symbol} x{qty} @ ~${(estimated_price or 0):.2f}")

        trade = ib.placeOrder(contract, order)

        fill_price = None
        for _ in range(20):
            ib.sleep(0.5)
            if trade.orderStatus.status in ("Filled", "Submitted", "PreSubmitted"):
                avg = trade.orderStatus.avgFillPrice
                if avg and avg > 0:
                    fill_price = avg
                    break
            if trade.orderStatus.status == "Filled":
                break

        if fill_price is None:
            fill_price = apply_slippage(estimated_price or 0, side, crypto=False)

        slip_pct = ((fill_price - (estimated_price or fill_price)) / (estimated_price or fill_price) * 100) if estimated_price else 0
        log.info(f"[IBKR FILL] {ib_side} {symbol} fill:${fill_price:.4f} signal:${(estimated_price or 0):.4f} slip:{slip_pct:+.3f}%")

        result = {
            "id":               trade.order.orderId,
            "symbol":           symbol,
            "status":           trade.orderStatus.status,
            "_ibkr_fill_price": fill_price,
        }
        record_api_success()
        return result, fill_price

    except Exception as e:
        log.error(f"[IBKR] place_order error: {e}")
        record_api_failure("ibkr")
        return None, estimated_price or 0
  

def update_live_prices():
    """Read current portfolio from IBKR and populate cfg.live_prices."""
    ib = get_ib()
    if not ib:
        return
    try:
        for item in ib.portfolio():
            sym = item.contract.symbol
            price = item.marketPrice
            if price and price > 0:
                cfg.live_prices[sym] = float(price)
    except Exception as e:
        log.debug(f"[LIVE PRICES] update failed: {e}")
