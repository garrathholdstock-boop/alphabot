"""
core/execution.py — AlphaBot Order Execution & API Wrappers
Stocks: IB Gateway via ib_insync (Docker socat → port 4004 paper / 4001 live)
Crypto: Binance API (testnet or live)
Broker: IBKR only — no Alpaca.

═══════════════════════════════════════════════════════════════════════════
ARCHITECTURE: Single shared IBKR connection on a dedicated event-loop thread
═══════════════════════════════════════════════════════════════════════════
All worker threads submit async coroutines to the manager's event loop via
asyncio.run_coroutine_threadsafe(). This guarantees:
  - No cross-thread event loop clashes (root cause of connection errors)
  - One clientId, never in use (eliminates clientId collision errors)
  - Natural serialisation — only one IB request in flight at any moment
  - Clean reconnection — one place owns connection state
  - Safe for any number of worker threads (10, 20, 100 — doesn't matter)

Public API is unchanged. fetch_bars(), place_order(), etc. work exactly
as before from the caller's perspective.
═══════════════════════════════════════════════════════════════════════════
"""

import time
import logging
import hashlib
import hmac
import urllib.parse
import requests
import asyncio
import threading
from datetime import datetime, timedelta
from ib_insync import IB, Stock, MarketOrder, StopOrder, LimitOrder, util

from core.config import (
    log, IS_LIVE,
    IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID, IBKR_MARKET_DATA_TYPE,
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


# ═══════════════════════════════════════════════════════════════════════════
# IBKR CONNECTION MANAGER — single instance on a dedicated event loop
# ═══════════════════════════════════════════════════════════════════════════

_IB_INSTANCE       = None              # the single IB() object
_IB_LOOP           = None              # dedicated asyncio event loop
_IB_LOOP_THREAD    = None              # thread running the event loop
_IB_CONNECTED      = False             # current connection state
_IB_START_LOCK     = threading.Lock()  # guards manager startup
_IB_CONNECT_LOCK   = threading.Lock()  # guards reconnection attempts
_IB_LAST_CONNECT   = 0.0               # timestamp of last connect attempt
_IB_CONNECT_BACKOFF = 1.0              # current backoff (seconds)
_IB_METRICS = {
    "requests_total": 0,
    "requests_failed": 0,
    "last_latency_ms": 0,
    "last_request_at": None,
    "reconnects": 0,
}

_SHARED_CLIENT_ID = IBKR_CLIENT_ID if IBKR_CLIENT_ID else 1


def _is_weekend_maintenance():
    """IBKR does maintenance Sat/Sun. ASX opens Sun 23:00 UTC so reconnect then."""
    now_utc = datetime.utcnow()
    wd, hr = now_utc.weekday(), now_utc.hour
    return (wd == 5 and hr < 23) or (wd == 6 and hr < 23)


def _ib_loop_runner():
    """Runs forever in the dedicated IB thread — owns the event loop."""
    global _IB_LOOP
    _IB_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_IB_LOOP)
    try:
        _IB_LOOP.run_forever()
    except Exception as e:
        log.error(f"[IBKR-MGR] Event loop crashed: {e}")
    finally:
        try: _IB_LOOP.close()
        except: pass


def _start_manager():
    """Start the manager thread + event loop. Idempotent."""
    global _IB_LOOP_THREAD
    with _IB_START_LOCK:
        if _IB_LOOP_THREAD is not None and _IB_LOOP_THREAD.is_alive():
            return
        _IB_LOOP_THREAD = threading.Thread(
            target=_ib_loop_runner, name="IBKR-Manager", daemon=True
        )
        _IB_LOOP_THREAD.start()
        for _ in range(50):
            if _IB_LOOP is not None and _IB_LOOP.is_running():
                break
            time.sleep(0.05)
        log.info("[IBKR-MGR] Manager thread started")


def start_ibkr_manager():
    """Public entrypoint — call this from main.py at startup."""
    _start_manager()


async def _async_connect():
    """Establish the shared IB connection. Runs on manager's loop.

    Handles Error 326 ('clientId already in use') with exponential backoff.
    When Gateway has a lingering session from a recent restart, it can take
    30-60s to release the slot. Rather than failing 5 times in quick succession
    and tripping the kill switch, we back off and let Gateway catch up.
    """
    global _IB_INSTANCE, _IB_CONNECTED
    if _IB_INSTANCE is None:
        _IB_INSTANCE = IB()
    if _IB_INSTANCE.isConnected():
        _IB_CONNECTED = True
        return True

    # Exponential backoff retry schedule for Error 326 specifically.
    # Total max wait ~115s (5+15+30+60) across 4 retries.
    # Good case (clean Gateway): attempt 1 succeeds in ~1s.
    backoff_schedule = [0, 5, 15, 30, 60]
    last_exc = None
    for attempt, wait_before in enumerate(backoff_schedule, start=1):
        if wait_before > 0:
            log.info(f"[IBKR-MGR] Waiting {wait_before}s before retry (attempt {attempt}/{len(backoff_schedule)}) — likely stale Gateway session")
            await asyncio.sleep(wait_before)
        try:
            await _IB_INSTANCE.connectAsync(
                IBKR_HOST, IBKR_PORT,
                clientId=_SHARED_CLIENT_ID, timeout=15,
            )
            _IB_CONNECTED = True
            _IB_METRICS["reconnects"] += 1
            log.info(f"[IBKR-MGR] Connected (clientId={_SHARED_CLIENT_ID}) on attempt {attempt}")
            # Set market data type — 3 = delayed (free on paper), 1 = live (paid subscription).
            # Controlled by IBKR_MARKET_DATA_TYPE env var. Silences Error 10089 on paper accounts.
            # Applied on every successful (re)connect so Gateway restarts don't revert it.
            try:
                _IB_INSTANCE.reqMarketDataType(IBKR_MARKET_DATA_TYPE)
                _mdt_label = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}.get(IBKR_MARKET_DATA_TYPE, str(IBKR_MARKET_DATA_TYPE))
                log.info(f"[IBKR-MGR] Market data type set to {IBKR_MARKET_DATA_TYPE} ({_mdt_label})")
            except Exception as e:
                log.warning(f"[IBKR-MGR] Failed to set market data type: {e!r} — quotes may be rejected with Error 10089")
            return True
        except Exception as e:
            last_exc = e
            _IB_CONNECTED = False
            # Make sure we clean up any half-open state before retrying,
            # otherwise next connectAsync may hit "already connected" internally.
            try:
                if _IB_INSTANCE.isConnected():
                    _IB_INSTANCE.disconnect()
            except Exception:
                pass
            # Is this a 326 (clientId in use) or a transient error worth retrying?
            msg = str(e).lower()
            is_326 = ("326" in msg or "client id" in msg or "already in use" in msg
                      or isinstance(e, TimeoutError))   # 326 often manifests as timeout after peer-close
            if not is_326:
                # Non-326 errors: log and fail fast. Don't burn the whole backoff on e.g. a config error.
                log.error(f"[IBKR-MGR] Connect failed (non-retryable): {e!r}")
                return False
            if attempt < len(backoff_schedule):
                log.warning(f"[IBKR-MGR] Attempt {attempt} hit Error 326 / session-in-use. Will retry.")
            else:
                log.error(f"[IBKR-MGR] All {len(backoff_schedule)} attempts exhausted. Last error: {e!r}")
    return False


def _ensure_connected():
    """Check & reconnect if needed. Returns True on success.
    Exponential backoff to avoid hammering IB during outages."""
    global _IB_CONNECTED, _IB_LAST_CONNECT, _IB_CONNECT_BACKOFF

    if _is_weekend_maintenance():
        return False

    _start_manager()

    if _IB_CONNECTED and _IB_INSTANCE is not None:
        try:
            if _IB_INSTANCE.isConnected():
                return True
        except: pass
        _IB_CONNECTED = False

    with _IB_CONNECT_LOCK:
        if _IB_CONNECTED and _IB_INSTANCE is not None:
            try:
                if _IB_INSTANCE.isConnected():
                    return True
            except: pass
            _IB_CONNECTED = False

        now = time.time()
        if now - _IB_LAST_CONNECT < _IB_CONNECT_BACKOFF:
            return False
        _IB_LAST_CONNECT = now

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_connect(), _IB_LOOP)
            ok = fut.result(timeout=20)
            if ok:
                _IB_CONNECT_BACKOFF = 1.0
            else:
                _IB_CONNECT_BACKOFF = min(_IB_CONNECT_BACKOFF * 2, 30.0)
            return ok
        except Exception as e:
            _IB_CONNECT_BACKOFF = min(_IB_CONNECT_BACKOFF * 2, 30.0)
            log.error(f"[IBKR-MGR] Reconnect failed: {e}")
            return False


def ibkr_graceful_disconnect():
    """Cleanly disconnect from IBKR Gateway. Called from main.py's SIGTERM handler.

    Why: when systemd restarts the bot with SIGTERM, we want Gateway to see
    a clean close (FIN) on the socket, not a half-open hang that Gateway
    must time out. A clean close releases clientId=1 immediately, letting
    the next bot process connect without hitting Error 326.

    Safe to call multiple times / when not connected / during shutdown."""
    global _IB_INSTANCE, _IB_CONNECTED
    try:
        if _IB_INSTANCE is not None and _IB_INSTANCE.isConnected():
            log.info("[IBKR-MGR] Graceful disconnect requested (SIGTERM/shutdown)")
            # disconnect() runs sync from any thread; ib_insync handles the loop internally.
            _IB_INSTANCE.disconnect()
            log.info("[IBKR-MGR] Disconnected cleanly")
    except Exception as e:
        log.warning(f"[IBKR-MGR] Graceful disconnect error (non-fatal): {e!r}")
    finally:
        _IB_CONNECTED = False


def _ibkr_submit(coro_factory, *args, timeout=15, **kwargs):
    """Bridge from worker threads to the manager's event loop.

    coro_factory: async function taking (ib, *args, **kwargs).
    Returns result or None on failure. Tracks metrics + api_health."""

    _IB_METRICS["requests_total"] += 1
    _IB_METRICS["last_request_at"] = datetime.now().isoformat()
    t0 = time.time()

    if not _ensure_connected():
        _IB_METRICS["requests_failed"] += 1
        record_api_failure("ibkr")
        return None

    try:
        coro = coro_factory(_IB_INSTANCE, *args, **kwargs)
        fut = asyncio.run_coroutine_threadsafe(coro, _IB_LOOP)
        result = fut.result(timeout=timeout)
        _IB_METRICS["last_latency_ms"] = int((time.time() - t0) * 1000)
        record_api_success()
        return result
    except asyncio.TimeoutError:
        _IB_METRICS["requests_failed"] += 1
        log.warning(f"[IBKR-MGR] Request timed out after {timeout}s")
        record_api_failure("ibkr")
        return None
    except Exception as e:
        _IB_METRICS["requests_failed"] += 1
        log.warning(f"[IBKR-MGR] Request failed: {e}")
        global _IB_CONNECTED
        if "connect" in str(e).lower() or "closed" in str(e).lower():
            _IB_CONNECTED = False
        record_api_failure("ibkr")
        return None


def get_ib():
    """Legacy compatibility — returns shared IB instance if connected."""
    if _ensure_connected():
        return _IB_INSTANCE
    return None


def get_ibkr_metrics():
    """Expose manager metrics for dashboard."""
    return {
        **_IB_METRICS,
        "connected": _IB_CONNECTED,
        "manager_running": _IB_LOOP_THREAD is not None and _IB_LOOP_THREAD.is_alive(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# API HEALTH TRACKING
# ═══════════════════════════════════════════════════════════════════════════

# Tier 2 auto-reset: if kill switch has been active AND API has been healthy
# for AUTO_RESET_HEALTHY_SECS continuously, clear the kill switch. Capped at
# AUTO_RESET_MAX_PER_DAY per 24h so we never flap.
AUTO_RESET_HEALTHY_SECS  = 300   # 5 minutes of continuous success
AUTO_RESET_MAX_PER_DAY   = 3     # after 3 auto-resets in 24h, requires manual

# Track when kill switch went active + when the current healthy streak began.
# These are module-local (not in api_health) because they're purely internal.
_kill_activated_epoch   = None   # float epoch when kill switch most recently activated
_healthy_streak_began   = None   # float epoch when current success streak started
_auto_reset_history     = []     # list of epochs when auto-resets happened


def _purge_old_resets(now_epoch):
    """Drop reset events older than 24h so we only count recent ones."""
    cutoff = now_epoch - 86400
    global _auto_reset_history
    _auto_reset_history = [t for t in _auto_reset_history if t > cutoff]


def _try_auto_reset_kill_switch():
    """If conditions met, clear kill_switch and st.shutoff across all disciplines.
    Called from record_api_success. Loud log on every decision point.
    Safe to call repeatedly — bails early if conditions not met.
    """
    global _healthy_streak_began, _kill_activated_epoch
    if not kill_switch.get("active"):
        return                              # nothing to reset
    if _healthy_streak_began is None:
        return                              # no streak tracked yet
    now = time.time()
    streak_len = now - _healthy_streak_began
    if streak_len < AUTO_RESET_HEALTHY_SECS:
        return                              # not healthy long enough yet
    _purge_old_resets(now)
    if len(_auto_reset_history) >= AUTO_RESET_MAX_PER_DAY:
        # Limit hit — log ONCE per kill activation to avoid flood
        if _kill_activated_epoch and (now - _kill_activated_epoch) < 60:
            log.error(f"[AUTO-RESET] BLOCKED — already used {len(_auto_reset_history)}/{AUTO_RESET_MAX_PER_DAY} auto-resets in last 24h. Manual reset required.")
        return

    # All gates passed — reset.
    log.warning("=" * 60)
    log.warning(f"[AUTO-RESET] Kill switch auto-reset firing after {streak_len:.0f}s of API health")
    log.warning(f"[AUTO-RESET] Prior kill reason: {kill_switch.get('reason')!r}")
    log.warning(f"[AUTO-RESET] Resets used today: {len(_auto_reset_history) + 1}/{AUTO_RESET_MAX_PER_DAY}")
    kill_switch["active"]       = False
    kill_switch["reason"]       = ""
    kill_switch["activated_at"] = None
    for st in [state, crypto_state, smallcap_state, intraday_state,
               crypto_intraday_state, asx_state, ftse_state]:
        st.shutoff = False
    api_health["ibkr_fails"] = 0
    api_health["data_fails"] = 0
    _auto_reset_history.append(now)
    _kill_activated_epoch = None
    log.warning("[AUTO-RESET] Kill switch cleared. All disciplines resumed.")
    log.warning("=" * 60)


def record_api_success():
    global _healthy_streak_began
    # If we were previously failing, this is the start of a new streak.
    had_failures = api_health.get("ibkr_fails", 0) > 0 or api_health.get("data_fails", 0) > 0
    api_health["ibkr_fails"] = 0
    api_health["data_fails"] = 0
    api_health["last_success"] = datetime.now().isoformat()
    now = time.time()
    if had_failures or _healthy_streak_began is None:
        _healthy_streak_began = now
    # If kill switch is active and we've been healthy long enough, reset it.
    _try_auto_reset_kill_switch()


def record_api_failure(source="ibkr"):
    global _healthy_streak_began, _kill_activated_epoch
    if source == "ibkr" and _is_weekend_maintenance():
        return
    key = f"{source}_fails"
    api_health[key] = api_health.get(key, 0) + 1
    # Any failure breaks the healthy streak.
    _healthy_streak_began = None
    total = api_health.get("ibkr_fails", 0) + api_health.get("data_fails", 0)
    if total >= api_health["max_fails"] and not kill_switch["active"]:
        kill_switch["active"] = True
        kill_switch["reason"] = f"API kill: {total} consecutive failures ({source})"
        kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
        _kill_activated_epoch = time.time()
        for st in [state, crypto_state, smallcap_state, intraday_state,
                   crypto_intraday_state, asx_state, ftse_state]:
            st.shutoff = True
        log.error(f"[API KILL] {total} consecutive API failures — all bots stopped")


# ═══════════════════════════════════════════════════════════════════════════
# CONTRACT BUILDERS
# ═══════════════════════════════════════════════════════════════════════════

def _make_contract(symbol, exchange="SMART", currency="USD"):
    return Stock(symbol, exchange, currency)


_INTL_MARKET = {}
try:
    from core.config import (
        ASX_WATCHLIST, FTSE_WATCHLIST,
        ASX_SMALLCAP_WATCHLIST, FTSE_SMALLCAP_WATCHLIST,
    )
    for s in ASX_WATCHLIST:           _INTL_MARKET[s] = ("ASX", "AUD")
    for s in FTSE_WATCHLIST:          _INTL_MARKET[s] = ("LSE", "GBP")
    for s in ASX_SMALLCAP_WATCHLIST:  _INTL_MARKET[s] = ("ASX", "AUD")
    for s in FTSE_SMALLCAP_WATCHLIST: _INTL_MARKET[s] = ("LSE", "GBP")
except Exception:
    pass


def _contract_for(symbol):
    exch, curr = _INTL_MARKET.get(symbol, ("SMART", "USD"))
    return _make_contract(symbol, exch, curr)


def _timeframe_to_ibkr(timeframe):
    mapping = {
        "1Min": "1 min", "5Min": "5 mins", "15Min": "15 mins",
        "30Min": "30 mins", "1Hour": "1 hour", "2Hour": "2 hours",
        "4Hour": "4 hours", "1Day": "1 day",
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


# ═══════════════════════════════════════════════════════════════════════════
# IBKR MARKET DATA — async coroutines run on manager's loop
# ═══════════════════════════════════════════════════════════════════════════

async def _async_fetch_bars(ib, symbol):
    contract = _contract_for(symbol)
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="", durationStr="60 D",
        barSizeSetting="1 day", whatToShow="TRADES",
        useRTH=True, formatDate=1, keepUpToDate=False,
    )
    if not bars or len(bars) < 15:
        return None
    return [{"t": b.date, "o": b.open, "h": b.high, "l": b.low,
             "c": b.close, "v": b.volume} for b in bars]


def fetch_bars(symbol, crypto=False):
    """Fetch daily bars. Crypto uses Binance, stocks use IBKR."""
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 300):
            return None
        bars = binance_fetch_bars(symbol, interval="1d", limit=35)
        return bars if bars and len(bars) >= 15 else None
    try:
        return _ibkr_submit(_async_fetch_bars, symbol, timeout=15)
    except Exception as e:
        log.warning(f"[IBKR] fetch_bars {symbol}: {e}")
        return None


def fetch_bars_batch(symbols, limit=30):
    if not symbols:
        return {}
    results = {}
    for symbol in symbols:
        bars = fetch_bars(symbol)
        if bars:
            results[symbol] = bars
        time.sleep(0.05)
    log.info(f"[IBKR BATCH] Fetched bars for {len(results)}/{len(symbols)} symbols")
    return results


async def _async_fetch_latest_price(ib, symbol):
    contract = _contract_for(symbol)
    ticker = ib.reqMktData(contract, "", True, False)
    await asyncio.sleep(0.5)
    price = ticker.last or ticker.close or ticker.bid or ticker.ask
    ib.cancelMktData(contract)
    if price and price > 0:
        return float(price)
    return None


def fetch_latest_price(symbol, crypto=False):
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 120):
            return None
        return binance_fetch_price(symbol)
    try:
        return _ibkr_submit(_async_fetch_latest_price, symbol, timeout=10)
    except Exception as e:
        log.warning(f"[IBKR] fetch_latest_price {symbol}: {e}")
        return None


async def _async_fetch_intraday(ib, symbol, timeframe, limit):
    contract = _contract_for(symbol)
    bar_size = _timeframe_to_ibkr(timeframe)
    duration = _limit_to_duration(limit, timeframe)
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="", durationStr=duration,
        barSizeSetting=bar_size, whatToShow="TRADES",
        useRTH=True, formatDate=1, keepUpToDate=False,
    )
    if not bars or len(bars) < 10:
        return None
    return [{"t": b.date, "o": b.open, "h": b.high, "l": b.low,
             "c": b.close, "v": b.volume} for b in bars]


def fetch_intraday_bars(symbol, timeframe="1Hour", limit=48, crypto=False):
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 300):
            return None
        binance_tf = BINANCE_INTERVAL_MAP.get(timeframe, "15m")
        bars = binance_fetch_bars(symbol, interval=binance_tf, limit=limit)
        return bars if bars and len(bars) >= 10 else None
    try:
        return _ibkr_submit(_async_fetch_intraday, symbol, timeframe, limit, timeout=15)
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
        time.sleep(0.05)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# IBKR ACCOUNT INFO
# ═══════════════════════════════════════════════════════════════════════════

async def _async_get_account(ib):
    # Use async variants — calling ib.accountSummary() synchronously from within
    # a coroutine running on the manager's loop triggers
    # "This event loop is already running" because ib_insync internally does
    # run_until_complete on the current loop.
    summary = await ib.accountSummaryAsync()
    result = {}
    for item in summary:
        if item.tag == "NetLiquidation":
            result["portfolio_value"] = float(item.value)
        elif item.tag == "TotalCashValue":
            result["cash"] = float(item.value)
        elif item.tag == "GrossPositionValue":
            result["long_market_value"] = float(item.value)
    result["last_equity"] = result.get("portfolio_value", 0)
    try:
        # ib.portfolio() returns cached state — safe to call synchronously
        for item in ib.portfolio():
            sym = item.contract.symbol
            price = item.marketPrice
            if price and price > 0:
                cfg.live_prices[sym] = float(price)
    except Exception:
        pass
    return result


def ibkr_get_account():
    try:
        result = _ibkr_submit(_async_get_account, timeout=10)
        return result if result else {}
    except Exception as e:
        log.warning(f"[IBKR] get_account: {e}")
        return {}


async def _async_get_positions(ib):
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


def ibkr_get_positions():
    try:
        result = _ibkr_submit(_async_get_positions, timeout=10)
        return result if result else []
    except Exception as e:
        log.warning(f"[IBKR] get_positions: {e}")
        return []


async def _async_get_open_orders(ib):
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


def ibkr_get_open_orders():
    try:
        result = _ibkr_submit(_async_get_open_orders, timeout=10)
        return result if result else []
    except Exception as e:
        log.warning(f"[IBKR] get_open_orders: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# IBKR STOP ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

async def _async_place_stop(ib, symbol, qty, stop_price):
    contract = _contract_for(symbol)
    order = StopOrder("SELL", qty, stop_price)
    trade = ib.placeOrder(contract, order)
    await asyncio.sleep(0.5)
    return {"id": trade.order.orderId, "symbol": symbol, "status": "accepted"}


def place_stop_order_ibkr(symbol, qty, stop_price):
    try:
        result = _ibkr_submit(_async_place_stop, symbol, qty, stop_price, timeout=10)
        if result:
            log.info(f"[IBKR STOP] Placed stop for {symbol} @ ${stop_price:.2f} id:{result['id']}")
        return result
    except Exception as e:
        log.warning(f"[IBKR] place_stop_order {symbol}: {e}")
        return None


async def _async_cancel_order(ib, order_id):
    open_trades = ib.openTrades()
    for trade in open_trades:
        if trade.order.orderId == order_id:
            ib.cancelOrder(trade.order)
            await asyncio.sleep(0.3)
            return True
    return False


def cancel_stop_order_ibkr(order_id):
    try:
        result = _ibkr_submit(_async_cancel_order, order_id, timeout=10)
        if result:
            log.info(f"[IBKR] Cancelled order {order_id}")
        return bool(result)
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


# ═══════════════════════════════════════════════════════════════════════════
# BINANCE API (unchanged — separate broker)
# ═══════════════════════════════════════════════════════════════════════════

def _binance_sign(params):
    query = urllib.parse.urlencode(params)
    sig = hmac.new(_BIN_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
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
                    tg(f"⚠️ Binance rate limit — banned for {retry_after}s")
                except: pass
            log.warning(f"[BINANCE] {path} {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.warning(f"[BINANCE] {path} error: {e}")
        return None


def binance_post(path, params):
    elapsed = time.time() - cfg._last_binance_call
    if elapsed < BINANCE_DELAY:
        time.sleep(BINANCE_DELAY - elapsed)
    cfg._last_binance_call = time.time()
    try:
        params["timestamp"] = _binance_ts()
        url = f"{BINANCE_BASE}{path}"
        body = _binance_sign(params)
        r = requests.post(url, headers=BINANCE_HEADERS, data=body, timeout=10)
        if not r.ok:
            log.warning(f"[BINANCE POST] {path} {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.warning(f"[BINANCE POST] {path} error: {e}")
        return None


def binance_fetch_bars(symbol, interval="1d", limit=35):
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return None
    bars = []
    for k in data:
        bars.append({
            "t": datetime.fromtimestamp(k[0] / 1000),
            "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "v": float(k[5]),
        })
    return bars


def binance_fetch_price(symbol):
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"]) if data and "price" in data else None


def binance_get_lot_size(symbol):
    if symbol in _binance_lot_cache:
        return _binance_lot_cache[symbol]
    try:
        data = binance_get("/api/v3/exchangeInfo", {"symbol": symbol})
        if data and data.get("symbols"):
            for f in data["symbols"][0]["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                    _binance_lot_cache[symbol] = (step, min_qty)
                    return step, min_qty
    except Exception as e:
        log.warning(f"[BINANCE] lot_size {symbol}: {e}")
    return 0.00001, 0.00001


def round_step(qty, step):
    if step == 0:
        return qty
    decimals = max(0, -int(f"{step:e}".split("e")[1]))
    return round(qty - (qty % step), decimals)


def binance_place_order(symbol, side, usdt_amount):
    price = binance_fetch_price(symbol)
    if not price:
        return None
    step, min_qty = binance_get_lot_size(symbol)
    qty = round_step(usdt_amount / price, step)
    if qty < min_qty:
        log.warning(f"[BINANCE] {symbol} qty {qty} below min {min_qty}")
        return None
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": qty,
    }
    result = binance_post("/api/v3/order", params)
    if result:
        fills = result.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            total_usd = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            avg_price = total_usd / total_qty if total_qty else price
            result["_real_fill_price"] = avg_price
        else:
            result["_real_fill_price"] = price
    return result


def binance_get_balance(asset="USDT"):
    data = binance_get("/api/v3/account", signed=True)
    if not data:
        return 0.0
    for b in data.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"]) + float(b["locked"])
    return 0.0


def binance_get_top_coins(limit=100):
    data = binance_get("/api/v3/ticker/24hr")
    if not data:
        return []
    usdt_pairs = [d for d in data if d["symbol"].endswith("USDT")]
    sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    return [p["symbol"] for p in sorted_pairs[:limit]]


# ═══════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def apply_slippage(price, side, crypto=False):
    slip = SLIPPAGE_CRYPTO if crypto else SLIPPAGE_STOCK
    return price * (1 + slip) if side.lower() == "buy" else price * (1 - slip)


def get_actual_fill_price(order_result, side, estimated_price, crypto=False):
    if not order_result:
        return apply_slippage(estimated_price, side, crypto)
    if "_real_fill_price" in order_result:
        return order_result["_real_fill_price"]
    if "_ibkr_fill_price" in order_result:
        return order_result["_ibkr_fill_price"]
    return apply_slippage(estimated_price, side, crypto)


def is_order_filled(order_result):
    if not order_result:
        return False
    status = str(order_result.get("status", "")).lower()
    return status in ("filled", "closed")


async def _async_place_order(ib, symbol, side, qty, estimated_price, order_type, stop_price):
    """Stock order placement coroutine."""
    contract = _contract_for(symbol)
    ib_side = "BUY" if side.lower() == "buy" else "SELL"

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
        await asyncio.sleep(0.5)
        result = {"id": trade.order.orderId, "symbol": symbol, "status": "accepted"}
        return result, stop_price

    if IS_LIVE and estimated_price:
        vix_now = cfg.global_risk.get("vix_level") or 20
        signal_score = getattr(place_order, "_last_score", 5)
        if signal_score >= 9:   base_tol = 0.010
        elif signal_score >= 7: base_tol = 0.006
        else:                   base_tol = 0.003
        if vix_now >= 30:   vix_adj = 0.004
        elif vix_now >= 20: vix_adj = 0.002
        else:               vix_adj = 0.0
        tolerance = base_tol + vix_adj
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
        await asyncio.sleep(0.5)
        if trade.orderStatus.status in ("Filled", "Submitted", "PreSubmitted"):
            avg = trade.orderStatus.avgFillPrice
            if avg and avg > 0:
                fill_price = avg
                break
        if trade.orderStatus.status == "Filled":
            break

    if fill_price is None:
        fill_price = apply_slippage(estimated_price or 0, side, crypto=False)

    slip_pct = (
        (fill_price - (estimated_price or fill_price)) / (estimated_price or fill_price) * 100
        if estimated_price else 0
    )
    log.info(f"[IBKR FILL] {ib_side} {symbol} fill:${fill_price:.4f} signal:${(estimated_price or 0):.4f} slip:{slip_pct:+.3f}%")

    result = {
        "id":               trade.order.orderId,
        "symbol":           symbol,
        "status":           trade.orderStatus.status,
        "_ibkr_fill_price": fill_price,
    }
    return result, fill_price


def place_order(symbol, side, qty, crypto=False, estimated_price=None, order_type=None, stop_price=None):
    """Place order via Binance (crypto) or IBKR (stocks).
    Returns (order_result, actual_fill_price)."""
    if crypto and USE_BINANCE:
        price = estimated_price or binance_fetch_price(symbol)
        usdt = float(qty) * price if price else float(qty)
        result = binance_place_order(symbol, side, usdt)
        if result and "_real_fill_price" in result:
            return result, result["_real_fill_price"]
        fill_price = get_actual_fill_price(result, side, price or 0, crypto=True)
        return result, fill_price

    try:
        result = _ibkr_submit(
            _async_place_order,
            symbol, side, qty, estimated_price, order_type, stop_price,
            timeout=30,
        )
        if result is None:
            return None, estimated_price or 0
        return result
    except Exception as e:
        log.error(f"[IBKR] place_order error: {e}")
        return None, estimated_price or 0


async def _async_update_live_prices(ib):
    for item in ib.portfolio():
        sym = item.contract.symbol
        price = item.marketPrice
        if price and price > 0:
            cfg.live_prices[sym] = float(price)


def update_live_prices():
    try:
        _ibkr_submit(_async_update_live_prices, timeout=5)
    except Exception as e:
        log.debug(f"[LIVE PRICES] update failed: {e}")
