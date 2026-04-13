"""
core/execution.py — AlphaBot Order Execution & API Wrappers
All Alpaca and Binance API calls, order placement, slippage model, and fill price logic.
"""

import time
import logging
import hashlib
import hmac
import urllib.parse
import requests
from datetime import datetime

from core.config import (
    log, ALPACA_BASE, DATA_BASE, HEADERS, IS_LIVE,
    BINANCE_BASE, BINANCE_HEADERS, _BIN_KEY, _BIN_SECRET,
    BINANCE_DELAY, BINANCE_INTERVAL_MAP, USE_BINANCE,
    SLIPPAGE_STOCK, SLIPPAGE_CRYPTO, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    _binance_lot_cache, _binance_balance_cache,
    api_health, kill_switch, exchange_stops,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    _save_ban_to_disk,
)
import core.config as cfg


# ── API health ────────────────────────────────────────────────
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
        log.error(f"[API KILL] {total_fails} consecutive API failures — all bots stopped")


# ── Alpaca API ────────────────────────────────────────────────
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
    """Place a real stop-loss order on Alpaca exchange."""
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
    try:
        r = requests.delete(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
        return r.ok
    except: return False

def update_exchange_stop(symbol, qty, new_stop_price):
    old_id = exchange_stops.get(symbol)
    if old_id:
        cancel_stop_order_alpaca(old_id)
    new_order = place_stop_order_alpaca(symbol, qty, round(new_stop_price, 2))
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
        elif remaining <= 0:
            log.info(f"[BINANCE] Ban expired — waiting 120s safety buffer")
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
                from app.notifications import tg
                tg(f"⚠️ <b>Binance Ban</b>\nDuration: {retry_after}s", category="binance_ban")
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
    except: pass
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


# ── Market data fetchers ──────────────────────────────────────
def fetch_bars_batch(symbols, limit=30):
    if not symbols: return {}
    end   = datetime.utcnow()
    from datetime import timedelta
    start = end - timedelta(days=60)
    s_str = start.strftime("%Y-%m-%d")
    e_str = end.strftime("%Y-%m-%d")
    results = {}
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        syms_param = ",".join(chunk)
        try:
            url = (f"{DATA_BASE}/v2/stocks/bars"
                   f"?symbols={requests.utils.quote(syms_param, safe=',')}"
                   f"&timeframe=1Day&start={s_str}&end={e_str}"
                   f"&limit={limit}&feed=iex&adjustment=raw")
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
    log.info(f"[BATCH] Fetched bars for {len(results)}/{len(symbols)} symbols")
    return results

def fetch_bars(symbol, crypto=False):
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 300):
            return None
        bars = binance_fetch_bars(symbol, interval="1d", limit=35)
        return bars if bars and len(bars) >= 15 else None
    from datetime import timedelta
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
            r = requests.get(f"{DATA_BASE}/v2/stocks/{symbol}/bars?timeframe=1Day&start={s}&end={e}&limit=30&feed=iex&adjustment=raw", headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", [])
        return bars if bars and len(bars) >= 15 else None
    except: return None

def fetch_latest_price(symbol, crypto=False):
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 120):
            return None
        return binance_fetch_price(symbol)
    try:
        if crypto:
            enc = requests.utils.quote(symbol, safe="")
            r = requests.get(f"{DATA_BASE}/v1beta3/crypto/us/latest/bars?symbols={enc}", headers=HEADERS, timeout=10)
            if not r.ok: return None
            return r.json().get("bars", {}).get(symbol, {}).get("c")
        else:
            r = requests.get(f"{DATA_BASE}/v2/stocks/{symbol}/snapshot?feed=iex", headers=HEADERS, timeout=10)
            if not r.ok: return None
            d = r.json()
            return d.get("latestTrade", {}).get("p") or d.get("latestQuote", {}).get("ap")
    except: return None

def fetch_intraday_bars_batch(symbols, timeframe="1Hour", limit=48):
    if not symbols: return {}
    if USE_BINANCE and time.time() < (cfg._binance_ban_until + 300):
        return {}
    results = {}
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        syms_param = ",".join(chunk)
        try:
            url = (f"{DATA_BASE}/v2/stocks/bars"
                   f"?symbols={requests.utils.quote(syms_param, safe=',')}"
                   f"&timeframe={timeframe}&limit={limit}&feed=iex&adjustment=raw")
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
    if crypto and USE_BINANCE:
        if time.time() < (cfg._binance_ban_until + 300):
            return None
        binance_tf = BINANCE_INTERVAL_MAP.get(timeframe, "15m")
        bars = binance_fetch_bars(symbol, interval=binance_tf, limit=limit)
        return bars if bars and len(bars) >= 10 else None
    try:
        if crypto:
            enc = requests.utils.quote(symbol, safe="")
            url = f"{DATA_BASE}/v1beta3/crypto/us/bars?symbols={enc}&timeframe={timeframe}&limit={limit}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", {}).get(symbol, [])
        else:
            url = f"{DATA_BASE}/v2/stocks/{symbol}/bars?timeframe={timeframe}&limit={limit}&feed=iex&adjustment=raw"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok: return None
            bars = r.json().get("bars", [])
        return bars if bars and len(bars) >= 10 else None
    except Exception as e:
        log.debug(f"intraday bars {symbol}: {e}")
        return None

def check_data_freshness(bars, max_age_hours=2):
    if not bars: return False, "no data"
    try:
        last_bar = bars[-1]
        bar_time_str = last_bar.get("t", "")
        if not bar_time_str: return True, "unknown"
        bar_time = datetime.fromisoformat(bar_time_str.replace("Z", "+00:00"))
        age = datetime.now(bar_time.tzinfo) - bar_time
        age_hours = age.total_seconds() / 3600
        age_str = f"{age_hours:.1f}h old"
        if age_hours > max_age_hours: return False, age_str
        return True, age_str
    except:
        return True, "unknown"


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
    filled = order_result.get("filled_avg_price")
    if filled:
        try:
            fp = float(filled)
            if fp > 0:
                log.info(f"[FILL] Real fill: ${fp:.4f} vs signal: ${estimated_price:.4f}")
                return fp
        except: pass
    fills = order_result.get("fills", [])
    if fills:
        total_qty   = sum(float(f["qty"]) for f in fills)
        total_value = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        if total_qty > 0:
            fp = total_value / total_qty
            log.info(f"[FILL] Binance fill: ${fp:.4f} vs signal: ${estimated_price:.4f}")
            return fp
    return apply_slippage(estimated_price, side, crypto)

def is_order_filled(order_result):
    if not order_result: return False
    status = order_result.get("status", "")
    if status in ("filled", "partially_filled", "FILLED", "PARTIALLY_FILLED"): return True
    if order_result.get("id") or order_result.get("orderId"): return True
    return False

def query_order_status(order_id, crypto=False):
    try:
        if crypto and USE_BINANCE: return None
        return alpaca_get(f"/v2/orders/{order_id}")
    except: return None


# ── Main order placement ──────────────────────────────────────
def place_order(symbol, side, qty, crypto=False, estimated_price=None):
    """Place order via Binance (crypto) or Alpaca (stocks/paper).
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

    # ── Stocks via Alpaca ──
    if IS_LIVE and estimated_price and not crypto:
        spread_pct = 0.0
        try:
            snap = alpaca_get(f"/v2/stocks/{symbol}/snapshot?feed=iex", base=DATA_BASE)
            if snap:
                bid = float(snap.get("latestQuote", {}).get("bp", 0) or 0)
                ask = float(snap.get("latestQuote", {}).get("ap", 0) or 0)
                if bid > 0 and ask > 0:
                    spread_pct = ((ask - bid) / bid) * 100
                    if spread_pct > 1.0:
                        log.warning(f"[SPREAD] {symbol} spread too wide ({spread_pct:.2f}%) — skipping")
                        return None, estimated_price
                    log.info(f"[SPREAD] {symbol} bid:${bid:.2f} ask:${ask:.2f} spread:{spread_pct:.3f}% — OK")
        except Exception as e:
            log.debug(f"[SPREAD] Could not check spread for {symbol}: {e}")

        vix_now      = cfg.global_risk.get("vix_level") or 20
        signal_score = getattr(place_order, "_last_score", 5)

        if signal_score >= 9:   base_tol = 0.010
        elif signal_score >= 7: base_tol = 0.006
        else:                   base_tol = 0.003

        if vix_now >= 30:   vix_adj = 0.004
        elif vix_now >= 20: vix_adj = 0.002
        else:               vix_adj = 0.0

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
            log.info(f"[LIMIT ORDER] {side.upper()} {symbol} limit:${limit_price:.2f} signal:${estimated_price:.2f} tolerance:{tolerance*100:.2f}%")
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
        order_id  = result["id"]
        real_fill = None
        for attempt in range(5):
            time.sleep(1)
            filled_order = alpaca_get(f"/v2/orders/{order_id}")
            if filled_order:
                status    = filled_order.get("status", "")
                avg_price = filled_order.get("filled_avg_price")
                if avg_price and float(avg_price) > 0:
                    real_fill = float(avg_price)
                    slip_pct  = ((real_fill - (estimated_price or real_fill)) / (estimated_price or real_fill) * 100)
                    log.info(f"[FILL] Alpaca {side.upper()} {symbol}: signal=${estimated_price:.4f} fill=${real_fill:.4f} slippage={slip_pct:+.3f}% status={status}")
                    break
                if status in ("filled", "partially_filled"):
                    break
        if real_fill:
            return result, real_fill

    fill_price = get_actual_fill_price(result, side, estimated_price or 0, crypto=False)
    if result: log.info(f"ORDER {side.upper()} {qty} {symbol} fill~${fill_price:.4f}")
    return result, fill_price
