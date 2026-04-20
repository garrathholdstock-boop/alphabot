"""
core/risk.py — AlphaBot Risk Management
Kill switches, circuit breakers, stop loss checks, loss streak logic,
position reconciliation, and portfolio exposure controls.
Broker: IBKR only — no Alpaca.
"""

import time
import logging
from datetime import datetime, timedelta

from core.config import (
    log, IS_LIVE, USE_BINANCE,
    STOP_LOSS_PCT, TRAILING_STOP_PCT, TRAIL_TRIGGER_PCT, TAKE_PROFIT_PCT,
    MAX_HOLD_DAYS, GAP_DOWN_PCT, CRYPTO_STOP_PCT, CRYPTO_TRAIL_PCT,
    MAX_DAILY_LOSS, DAILY_PROFIT_TARGET, MAX_PORTFOLIO_EXPOSURE,
    LOSS_STREAK_LIMIT, LOSS_STREAK_PAUSE,
    RAPID_LOSS_COUNT, RAPID_LOSS_MINUTES, RAPID_LOSS_AMOUNT,
    VIX_LOW_THRESHOLD, VIX_HIGH_THRESHOLD, VIX_EXTREME, VIX_FEAR_THRESHOLD,
    SPY_CIRCUIT_BREAKER, SPY_FAST_DROP_PCT, SPY_MA_PERIOD,
    BTC_MA_PERIOD, BTC_CRASH_PCT, MACRO_KEYWORDS,
    MAX_SECTOR_POSITIONS, SECTOR_MAP, MAX_TOTAL_POSITIONS,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    asx_state, ftse_state,
    global_risk, perf, kill_switch, circuit_breaker,
    market_regime, crypto_regime, news_state, exchange_stops,
)
import core.config as cfg


# ── Portfolio helpers ─────────────────────────────────────────
def total_exposure(st):
    return sum(pos["entry_price"] * pos["qty"] for pos in st.positions.values())

def all_positions_count():
    return (len(state.positions) + len(crypto_state.positions) +
            len(smallcap_state.positions) + len(intraday_state.positions) +
            len(crypto_intraday_state.positions) +
            len(asx_state.positions) + len(ftse_state.positions))

def all_symbols_held():
    held = set()
    for st in [state, crypto_state, smallcap_state, intraday_state,
               crypto_intraday_state, asx_state, ftse_state]:
        held.update(st.positions.keys())
    return held

def sectors_held():
    held = {}
    for st in [state, crypto_state, smallcap_state, intraday_state,
               crypto_intraday_state, asx_state, ftse_state]:
        for sym in st.positions:
            sector = SECTOR_MAP.get(sym)
            if sector:
                held[sector] = held.get(sector, 0) + 1
    return held

def calc_unrealized_pnl(st):
    total = 0.0
    for sym, pos in st.positions.items():
        price = pos.get("highest_price", pos["entry_price"])
        total += (price - pos["entry_price"]) * pos["qty"]
    return total


# ── Loss streak & kill switches ───────────────────────────────
def is_loss_streak_paused():
    if global_risk["paused_until"] and datetime.now() < global_risk["paused_until"]:
        remaining = (global_risk["paused_until"] - datetime.now()).seconds // 60
        log.info(f"[RISK] Loss streak pause active — {remaining} mins remaining")
        return True
    return False

def record_trade_result(pnl, symbol):
    now_iso = datetime.now().isoformat()
    perf["all_trades"].append({
        "pnl": pnl, "symbol": symbol, "time": now_iso, "score": None,
    })
    if pnl < 0:
        global_risk["loss_streak"] += 1
        if global_risk["loss_streak"] >= LOSS_STREAK_LIMIT:
            pause_until = datetime.now() + timedelta(seconds=LOSS_STREAK_PAUSE)
            global_risk["paused_until"] = pause_until
            log.warning(f"[RISK] {LOSS_STREAK_LIMIT} consecutive losses — pausing until {pause_until.strftime('%H:%M')}")
    else:
        global_risk["loss_streak"] = 0

    window_start = datetime.now() - timedelta(minutes=RAPID_LOSS_MINUTES)
    recent_losses = [
        t for t in perf["all_trades"]
        if t["pnl"] < 0 and datetime.fromisoformat(t["time"]) > window_start
    ]
    recent_loss_total = sum(abs(t["pnl"]) for t in recent_losses)
    if len(recent_losses) >= RAPID_LOSS_COUNT or recent_loss_total >= RAPID_LOSS_AMOUNT:
        if not kill_switch["active"]:
            kill_switch["active"]       = True
            kill_switch["reason"]       = f"Dynamic kill: {len(recent_losses)} losses (${recent_loss_total:.2f}) in {RAPID_LOSS_MINUTES}min"
            kill_switch["activated_at"] = datetime.now().strftime("%H:%M:%S")
            for st in [state, crypto_state, smallcap_state, intraday_state,
                       crypto_intraday_state, asx_state, ftse_state]:
                st.shutoff = True
            log.warning(f"[DYNAMIC KILL] {kill_switch['reason']}")

def record_trade_with_score(pnl, symbol, score=None, signal=None, rsi=None, vol_ratio=None, hold_hours=None):
    record_trade_result(pnl, symbol)
    if perf["all_trades"]:
        perf["all_trades"][-1].update({
            "score": score, "signal": signal, "rsi": rsi,
            "vol_ratio": vol_ratio, "hold_hours": hold_hours,
            "outcome": "WIN" if pnl > 0 else "LOSS",
        })


# ── Drawdown & performance ────────────────────────────────────
def update_drawdown(portfolio_value):
    if portfolio_value > perf["peak_portfolio"]:
        perf["peak_portfolio"] = portfolio_value
    if perf["peak_portfolio"] > 0:
        dd = ((perf["peak_portfolio"] - portfolio_value) / perf["peak_portfolio"]) * 100
        if dd > perf["max_drawdown"]:
            perf["max_drawdown"] = dd

def calc_profit_factor():
    wins   = sum(t["pnl"] for t in perf["all_trades"] if t["pnl"] > 0)
    losses = sum(abs(t["pnl"]) for t in perf["all_trades"] if t["pnl"] < 0)
    return round(wins / losses, 2) if losses > 0 else float("inf")

def calc_sharpe():
    daily = perf["sharpe_daily"]
    if len(daily) < 5: return None
    import statistics
    avg = statistics.mean(daily)
    std = statistics.stdev(daily)
    return round((avg / std) * (252 ** 0.5), 2) if std > 0 else None


# ── Position sizing multipliers ───────────────────────────────
def equity_curve_size_factor():
    if perf["peak_portfolio"] <= 0 or not cfg.account_info:
        return 1.0
    current_pv   = float(cfg.account_info.get("portfolio_value", perf["peak_portfolio"]))
    drawdown_pct = ((perf["peak_portfolio"] - current_pv) / perf["peak_portfolio"]) * 100
    if drawdown_pct <= 0:  return 1.0
    if drawdown_pct >= 10: return 0.25
    if drawdown_pct >= 5:  return 0.5
    if drawdown_pct >= 2:  return 0.75
    return 1.0

def vol_adjusted_size(base_size):
    vix = global_risk.get("vix_level")
    if not vix: return base_size
    if vix >= VIX_EXTREME:        return base_size * 0.25
    if vix >= VIX_HIGH_THRESHOLD: return base_size * 0.50
    if vix <= VIX_LOW_THRESHOLD:  return base_size * 1.25
    return base_size

def news_size_multiplier(symbol):
    from core.config import NEWS_POSITIVE_BOOST
    if symbol in news_state.get("watch_list", {}):
        return NEWS_POSITIVE_BOOST
    return 1.0


# ── Stop loss / position checks ───────────────────────────────
def check_stop_losses(st, crypto=False):
    """Check open positions for stop, trailing stop, take-profit, max hold."""
    if crypto and USE_BINANCE and time.time() < cfg._binance_ban_until:
        return
    now = datetime.now()

    unrealized = calc_unrealized_pnl(st)
    total_loss  = st.daily_pnl + unrealized
    if total_loss <= -MAX_DAILY_LOSS and not st.shutoff:
        log.warning(f"[{st.label}] Total loss ${total_loss:.2f} — shutting off")
        st.shutoff = True
        return

    from core.execution import fetch_latest_price, place_order, cancel_stop_order_ibkr

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    market_just_opened = False
    if not crypto:
        et = datetime.now(ZoneInfo("America/New_York"))
        if et.hour == 9 and 30 <= et.minute <= 32:
            market_just_opened = True

    stop_pct    = CRYPTO_STOP_PCT if crypto else STOP_LOSS_PCT
    trail_pct   = CRYPTO_TRAIL_PCT if crypto else TRAILING_STOP_PCT
    trail_trig  = TRAIL_TRIGGER_PCT

    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym, crypto=crypto)
        if not live: continue

        entry  = pos["entry_price"]
        high   = pos.get("highest_price", entry)
        # Bug fix (2026-04-20): compute days_held dynamically from entry_ts.
        # The days_held field is only ever set to 0 at entry and never
        # incremented anywhere, making MAX_HOLD_DAYS checks dead-letter.
        # entry_ts is persistent across restarts (via DB merge in recovery).
        _ets_str = pos.get("entry_ts")
        if _ets_str:
            try:
                _ets = datetime.fromisoformat(_ets_str)
                if _ets.tzinfo is None:
                    _ets = _ets.replace(tzinfo=ZoneInfo("UTC"))
                days = (datetime.now(ZoneInfo("UTC")) - _ets).days
            except Exception:
                days = pos.get("days_held", 0)
        else:
            days = pos.get("days_held", 0)

        if live > high:
            pos["highest_price"] = live
            high = live

        profit_pct = (high - entry) / entry * 100
        if profit_pct >= trail_trig:
            new_trail = high * (1 - trail_pct / 100)
            if new_trail > pos["stop_price"]:
                pos["stop_price"] = new_trail
                if not crypto:
                    from core.execution import update_exchange_stop
                    update_exchange_stop(sym, pos["qty"], round(new_trail, 2))
                log.info(f"[{st.label}] Trail updated {sym} → stop ${new_trail:.4f}")

        if market_just_opened and not crypto:
            prev_close = entry
            gap_down   = ((live - prev_close) / prev_close) * 100
            if gap_down <= -GAP_DOWN_PCT:
                reason = f"Gap-Down ({gap_down:.1f}%)"
                _close_position(st, sym, pos, live, reason, now, crypto)
                continue

        reason = None
        pct    = ((live - entry) / entry) * 100
        if live <= pos["stop_price"]:
            reason = f"Stop-Loss ({pct:.1f}%)"
        elif live >= pos.get("take_profit_price", entry * (1 + TAKE_PROFIT_PCT / 100)):
            reason = f"Take-Profit (+{pct:.1f}%)"
        elif days >= MAX_HOLD_DAYS:
            reason = f"Max-Hold ({days}d)"
        else:
            hold_hrs = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600 if pos.get("entry_ts") else 0
            if hold_hrs >= 24 and abs(pct) < 0.5:
                from core.config import MIN_SIGNAL_SCORE
                waiting = [c for c in st.candidates
                           if c.get("score", 0) >= MIN_SIGNAL_SCORE + 2
                           and c["symbol"] not in st.positions
                           and c.get("ema_gap", -99) > 0]
                if len(waiting) >= 2:
                    reason = f"Opportunity-Cost ({pct:+.1f}% after {hold_hrs:.0f}h — {len(waiting)} better signals waiting)"

        if reason:
            _close_position(st, sym, pos, live, reason, now, crypto)

def _close_position(st, sym, pos, live, reason, now, crypto):
    from core.execution import place_order, cancel_stop_order_ibkr
    pnl      = (live - pos["entry_price"]) * pos["qty"]
    entry_ts = pos.get("entry_ts")
    hold_hours = round((now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None
    log.info(f"[{st.label}] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")

    if not crypto and sym in exchange_stops:
        cancel_stop_order_ibkr(exchange_stops.pop(sym))

    place_order(sym, "sell", pos["qty"], crypto=crypto, estimated_price=live)
    del st.positions[sym]
    st.daily_pnl += pnl
    st.trades.insert(0, {
        "symbol": sym, "side": "SELL", "qty": pos["qty"],
        "price": live, "pnl": pnl, "reason": reason,
        "time": now.strftime("%H:%M:%S"), "hold_hours": hold_hours
    })
    record_trade_result(pnl, sym)
    if st.daily_pnl <= -MAX_DAILY_LOSS:
        st.shutoff = True


# ── Market regime ─────────────────────────────────────────────
def is_market_open():
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    et   = datetime.now(ZoneInfo("America/New_York"))
    mins = et.hour * 60 + et.minute
    return et.weekday() < 5 and 570 <= mins < 960

def update_market_regime():
    from core.execution import fetch_bars
    spy_bars  = fetch_bars("SPY")
    vixy_bars = fetch_bars("VIXY")

    spy_price = spy_ma20 = vix_val = None
    if spy_bars and len(spy_bars) >= SPY_MA_PERIOD:
        closes    = [b["c"] for b in spy_bars]
        spy_price = closes[-1]
        spy_ma20  = sum(closes[-SPY_MA_PERIOD:]) / SPY_MA_PERIOD
    if vixy_bars:
        raw_vixy = vixy_bars[-1]["c"]
        vix_val  = raw_vixy * 0.57
        log.info(f"[REGIME] VIX via VIXY proxy: {vix_val:.2f} (VIXY=${raw_vixy:.2f})")

    bear_signals = 0
    if spy_price and spy_ma20 and spy_price < spy_ma20: bear_signals += 1
    if vix_val and vix_val > VIX_FEAR_THRESHOLD:        bear_signals += 1

    old_mode   = market_regime["mode"]
    bear_count = market_regime.get("bear_count", 0)
    if bear_signals >= 1: bear_count += 1
    else: bear_count = max(0, bear_count - 1)
    new_mode = "BEAR" if bear_count >= 2 else "BULL"

    market_regime.update({
        "mode": new_mode, "bear_count": bear_count,
        "vix": vix_val, "spy_price": spy_price, "spy_ma20": spy_ma20,
        "spy_trend": "below MA20" if (spy_price and spy_ma20 and spy_price < spy_ma20) else "above MA20",
        "last_check": datetime.now().strftime("%H:%M:%S"),
    })
    if old_mode != new_mode:
        log.warning(f"[REGIME] Mode changed: {old_mode} -> {new_mode}")
    log.info(f"[REGIME] {new_mode} | SPY: ${spy_price or 'N/A'} MA20: ${spy_ma20 or 'N/A'} | VIX: {vix_val or 'N/A'}")
    return new_mode

def update_crypto_regime():
    if USE_BINANCE and time.time() < (cfg._binance_ban_until + 300):
        return crypto_regime["mode"]
    from core.execution import fetch_bars
    btc_symbol = "BTCUSDT" if USE_BINANCE else "BTC/USD"
    btc_bars = fetch_bars(btc_symbol, crypto=True)
    if not btc_bars or len(btc_bars) < BTC_MA_PERIOD:
        return crypto_regime["mode"]

    closes    = [b["c"] for b in btc_bars]
    btc_price = closes[-1]
    btc_prev  = closes[-2]
    btc_ma20  = sum(closes[-BTC_MA_PERIOD:]) / BTC_MA_PERIOD
    btc_change = ((btc_price - btc_prev) / btc_prev) * 100

    bear_signals = 0
    if btc_price < btc_ma20:         bear_signals += 1
    if btc_change <= -BTC_CRASH_PCT: bear_signals += 1

    old_mode   = crypto_regime["mode"]
    bear_count = crypto_regime.get("bear_count", 0)
    if bear_signals >= 1: bear_count += 1
    else: bear_count = max(0, bear_count - 1)
    new_mode = "BEAR" if bear_count >= 2 else "BULL"

    crypto_regime.update({
        "mode": new_mode, "bear_count": bear_count,
        "btc_price": btc_price, "btc_ma20": btc_ma20,
        "btc_change": btc_change,
        "last_check": datetime.now().strftime("%H:%M:%S"),
    })
    if old_mode != new_mode:
        log.warning(f"[CRYPTO REGIME] Mode changed: {old_mode} -> {new_mode}")
    log.info(f"[CRYPTO REGIME] {new_mode} | BTC: ${btc_price:.0f} MA20: ${btc_ma20:.0f} | Daily: {btc_change:+.1f}%")
    return new_mode

def check_circuit_breaker():
    """Check for intraday SPY circuit breaker using IBKR data."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    et   = datetime.now(ZoneInfo("America/New_York"))
    mins = et.hour * 60 + et.minute
    if mins == 570:
        circuit_breaker.update({"active": False, "reason": None, "triggered_at": None, "macro_paused": False})
        log.info("[CIRCUIT] Reset for new trading day")

    # Use latest SPY price from IBKR
    from core.execution import fetch_latest_price
    spy_now = fetch_latest_price("SPY")
    if not spy_now:
        return

    spy_open = circuit_breaker.get("spy_open")
    if not spy_open:
        circuit_breaker["spy_open"] = spy_now
        spy_open = spy_now

    if spy_open and spy_open > 0:
        intraday_drop = ((spy_now - spy_open) / spy_open) * 100
        if intraday_drop <= -SPY_CIRCUIT_BREAKER and not circuit_breaker["active"]:
            circuit_breaker.update({
                "active": True,
                "reason": f"SPY intraday drop {intraday_drop:.1f}% — all new buys paused",
                "triggered_at": datetime.now().strftime("%H:%M:%S"),
            })
            log.warning(f"[CIRCUIT] TRIGGERED: SPY down {intraday_drop:.1f}% today")

def check_macro_news():
    if not cfg.NEWS_API_KEY: return
    try:
        import requests as req
        url = (f"https://newsapi.org/v2/top-headlines"
               f"?category=business&language=en&pageSize=10&apiKey={cfg.NEWS_API_KEY}")
        r = req.get(url, timeout=8)
        if not r.ok: return
        articles = r.json().get("articles", [])
        for art in articles:
            title = (art.get("title") or "").lower()
            for kw in MACRO_KEYWORDS:
                if kw in title:
                    circuit_breaker["macro_paused"] = True
                    circuit_breaker["active"]       = True
                    circuit_breaker["reason"]       = f"Macro news: '{kw}' — {art.get('title','')[:60]}"
                    log.warning(f"[MACRO] Circuit breaker: {circuit_breaker['reason']}")
                    return
    except Exception as e:
        log.debug(f"[CIRCUIT] Macro check error: {e}")

def is_choppy_market():
    spy_price = market_regime.get("spy_price")
    spy_ma20  = market_regime.get("spy_ma20")
    if not spy_price or not spy_ma20: return False
    return abs((spy_price - spy_ma20) / spy_ma20) * 100 < 0.5

def is_intraday_window():
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    from core.config import INTRADAY_START_HOUR_ET, INTRADAY_END_HOUR_ET
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5: return False
    return INTRADAY_START_HOUR_ET <= et.hour < INTRADAY_END_HOUR_ET
