"""
app/main.py — AlphaBot Main Bot Logic
Scan cycles for US stocks, crypto, ASX, FTSE, small cap, intraday.
Broker: IBKR only — no Alpaca.
asx_state and ftse_state are BotState objects (not plain dicts).
"""

import time
import threading
import logging
from datetime import datetime, timedelta

from core.config import (
    log, IS_LIVE, USE_BINANCE, BINANCE_USE_TESTNET,
    CRYPTO_WATCHLIST, US_WATCHLIST, ASX_WATCHLIST, FTSE_WATCHLIST,
    MIN_SIGNAL_SCORE, MAX_POSITIONS, MAX_TOTAL_POSITIONS, MAX_TRADES_PER_DAY,
    CYCLE_SECONDS, INTRADAY_CYCLE_SECONDS,
    STOP_LOSS_PCT, TRAILING_STOP_PCT, TRAIL_TRIGGER_PCT, TAKE_PROFIT_PCT,
    MAX_HOLD_DAYS, GAP_DOWN_PCT, CRYPTO_STOP_PCT,
    MAX_DAILY_LOSS, MAX_DAILY_SPEND, MAX_PORTFOLIO_EXPOSURE, DAILY_PROFIT_TARGET,
    MAX_TRADE_VALUE, SMALLCAP_MAX_TRADE, SECTOR_MAP, MAX_SECTOR_POSITIONS,
    SMALLCAP_MIN_PRICE, SMALLCAP_MAX_PRICE, SMALLCAP_STOP_LOSS,
    INTRADAY_TIMEFRAME, INTRADAY_BARS, INTRADAY_EMA_FAST, INTRADAY_EMA_SLOW,
    INTRADAY_RSI_LIMIT, INTRADAY_VOL_RATIO, INTRADAY_TAKE_PROFIT, INTRADAY_STOP_LOSS,
    INTRADAY_MAX_POSITIONS,
    CRYPTO_INTRADAY_TIMEFRAME, CRYPTO_INTRADAY_BARS, CRYPTO_INTRADAY_EMA_FAST,
    CRYPTO_INTRADAY_EMA_SLOW, CRYPTO_INTRADAY_TP, CRYPTO_INTRADAY_SL,
    CRYPTO_INTRADAY_MAX_POS, CRYPTO_INTRADAY_VOL_RATIO,
    SMALLCAP_REFRESH_DAYS, SMALLCAP_POOL_SIZE,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    asx_state, ftse_state,
    global_risk, perf, kill_switch, circuit_breaker,
    market_regime, crypto_regime, asx_regime, ftse_regime,
    news_state, smallcap_pool, exchange_stops, near_miss_tracker,
    DB_PATH,
)
import core.config as cfg
from core.execution import (
    ibkr_get_account, ibkr_get_positions, ibkr_get_open_orders,
    fetch_bars, fetch_bars_batch, fetch_latest_price,
    fetch_intraday_bars, fetch_intraday_bars_batch,
    place_order, place_stop_order_ibkr, cancel_stop_order_ibkr,
    update_exchange_stop, binance_get_balance, binance_get_top_coins,
)
from core.risk import (
    total_exposure, all_positions_count, all_symbols_held, sectors_held,
    check_stop_losses, update_market_regime, update_crypto_regime,
    check_circuit_breaker, check_macro_news,
    is_market_open, is_intraday_window, is_loss_streak_paused,
    record_trade_result, record_trade_with_score, update_drawdown,
    calc_profit_factor, calc_sharpe, equity_curve_size_factor,
    vol_adjusted_size, news_size_multiplier,
)
from data.analytics import score_signal, score_signal_intraday
from data.database import (
    db_save_trade, db_save_near_miss, db_update_near_misses,
    db_save_daily_report, db_save_weekly_report,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


# ── Signal functions ──────────────────────────────────────────
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)

def get_signal(closes, volumes):
    if len(closes) < 22:
        return "HOLD", None, None, None
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    rsi_val = rsi(closes)
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 1
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    if e9 and e21 and e9 > e21 and rsi_val and 40 <= rsi_val <= 70 and vol_ratio >= 1.2:
        return "BUY", e9, e21, rsi_val
    elif e9 and e21 and e9 < e21:
        return "SELL", e9, e21, rsi_val
    return "HOLD", e9, e21, rsi_val

def get_signal_smallcap(closes, volumes):
    return get_signal(closes, volumes)

def get_intraday_signal(closes, volumes, ema_fast, ema_slow, rsi_limit, vol_ratio_min):
    if len(closes) < ema_slow + 2:
        return "HOLD", None, None, None
    ef = ema(closes, ema_fast)
    es = ema(closes, ema_slow)
    rsi_val = rsi(closes)
    avg_vol = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    if ef and es and ef > es and rsi_val and rsi_val < rsi_limit and vol_ratio >= vol_ratio_min:
        return "BUY", ef, es, rsi_val
    elif ef and es and ef < es:
        return "SELL", ef, es, rsi_val
    return "HOLD", ef, es, rsi_val

def vwap_signal(bars):
    if not bars or len(bars) < 3:
        return "UNKNOWN"
    try:
        total_vol = sum(b["v"] for b in bars if b["v"] > 0)
        if total_vol == 0:
            return "UNKNOWN"
        vwap_val = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars) / total_vol
        last_close = bars[-1]["c"]
        return "ABOVE" if last_close >= vwap_val else "BELOW"
    except:
        return "UNKNOWN"

def check_intraday_positions(st, crypto=False):
    """Exit intraday positions that hit stop or TP."""
    from core.config import (INTRADAY_TAKE_PROFIT, INTRADAY_STOP_LOSS,
                              CRYPTO_INTRADAY_TP, CRYPTO_INTRADAY_SL)
    now = datetime.now()
    tp_pct = CRYPTO_INTRADAY_TP if crypto else INTRADAY_TAKE_PROFIT
    sl_pct = CRYPTO_INTRADAY_SL if crypto else INTRADAY_STOP_LOSS
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym, crypto=crypto)
        if not live: continue
        entry  = pos["entry_price"]
        pct    = ((live - entry) / entry) * 100
        reason = None
        if pct <= -sl_pct:  reason = f"[ID] Stop-Loss ({pct:.1f}%)"
        elif pct >= tp_pct: reason = f"[ID] Take-Profit (+{pct:.1f}%)"
        if reason:
            pnl      = (live - entry) * pos["qty"]
            entry_ts = pos.get("entry_ts")
            hold_h   = round((now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 2) if entry_ts else None
            log.info(f"[{st.label}] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")
            if sym in exchange_stops:
                cancel_stop_order_ibkr(exchange_stops.pop(sym))
            place_order(sym, "sell", pos["qty"], crypto=crypto, estimated_price=live)
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": reason,
                "time": now.strftime("%H:%M:%S"), "hold_hours": hold_h})
            record_trade_result(pnl, sym)
            db_save_trade(sym, "SELL", pos["qty"], live, pnl, reason, None, hold_h)
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                st.shutoff = True


# ── Near-miss tracker ─────────────────────────────────────────
def track_near_miss(symbol, score, skip_reason):
    """Track stocks that scored just below threshold."""
    if symbol not in near_miss_tracker:
        near_miss_tracker[symbol] = {
            "score":       score,
            "skip_reason": skip_reason,
            "entry_price": None,
            "tracked_at":  datetime.now().isoformat(),
        }
        db_save_near_miss(symbol, score, skip_reason)
        log.debug(f"[NEAR-MISS] Tracking {symbol} score={score:.1f} reason={skip_reason}")

def update_near_miss_prices():
    """Update near-miss price movements."""
    db_update_near_misses()


# ── Small cap pool management ─────────────────────────────────
def should_refresh_smallcap():
    if not smallcap_pool["last_refresh_day"]:
        return True
    days_since = (datetime.now().date() - smallcap_pool["last_refresh_day"]).days
    return days_since >= SMALLCAP_REFRESH_DAYS

def refresh_smallcap_pool():
    """Build small cap pool from IBKR bars scan."""
    log.info("[SMALLCAP] Refreshing small cap pool via IBKR...")
    scored = []
    for sym in US_WATCHLIST:
        try:
            bars = fetch_bars(sym)
            if not bars or len(bars) < 10: continue
            price = bars[-1]["c"]
            if not (SMALLCAP_MIN_PRICE <= price <= SMALLCAP_MAX_PRICE): continue
            volumes = [b["v"] for b in bars]
            avg_vol = sum(volumes[-10:]) / min(10, len(volumes))
            if avg_vol < 50000: continue
            closes   = [b["c"] for b in bars]
            momentum = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
            sc       = avg_vol * (1 + abs(momentum) / 100)
            scored.append({"symbol": sym, "score": sc})
        except:
            continue
    scored.sort(key=lambda x: -x["score"])
    pool = [s["symbol"] for s in scored[:SMALLCAP_POOL_SIZE]]
    smallcap_pool["symbols"]          = pool
    smallcap_pool["last_refresh"]     = datetime.now().strftime("%Y-%m-%d %H:%M")
    smallcap_pool["last_refresh_day"] = datetime.now().date()
    log.info(f"[SMALLCAP] Pool refreshed: {len(pool)} stocks")


# ── Capital efficiency logic ──────────────────────────────────
def check_capital_efficiency(st, new_signal_score, new_symbol):
    """
    Logic 1: Rotate weakest position if new signal scores 1.5+ higher AND held pos profitable.
    Logic 2: Exit stale flat position to free slot.
    Returns symbol to exit, or None.
    """
    if not st.positions:
        return None

    # Logic 1: Score rotation
    weakest_sym   = None
    weakest_score = new_signal_score
    for sym, pos in st.positions.items():
        pos_score = pos.get("signal_score", 0) or 0
        if pos_score < weakest_score:
            live = fetch_latest_price(sym)
            if live and (live - pos["entry_price"]) / pos["entry_price"] * 100 > 0.1:
                if new_signal_score - pos_score >= 1.5:
                    weakest_sym   = sym
                    weakest_score = pos_score
    if weakest_sym:
        log.info(f"[ROTATE] 🔄 Rotating out {weakest_sym} (score {weakest_score:.1f}) for {new_symbol} (score {new_signal_score:.1f})")
        return weakest_sym

    # Logic 2: Stale capital exit
    now = datetime.now()
    for sym, pos in st.positions.items():
        if not pos.get("entry_ts"): continue
        entry_dt = datetime.fromisoformat(pos["entry_ts"])
        held_mins = (now - entry_dt).total_seconds() / 60
        if held_mins >= 30:
            live = fetch_latest_price(sym)
            if live:
                pct = abs((live - pos["entry_price"]) / pos["entry_price"] * 100)
                if pct <= 0.5:
                    log.info(f"[STALE EXIT] ⏱ {sym} flat {pct:.2f}% after {held_mins:.0f}m — freeing slot")
                    return sym
    return None


# ── Main scan cycle ───────────────────────────────────────────
def run_cycle(watchlist, st, crypto=False):
    """Main scan cycle for US stocks or crypto."""
    st.check_reset()
    if st.shutoff: return
    if kill_switch["active"]: return
    if is_loss_streak_paused(): return

    if not crypto:
        if circuit_breaker["active"]:
            log.info(f"[{st.label}] Circuit breaker active — skipping buys")
        if market_regime["mode"] == "BEAR":
            log.info(f"[{st.label}] BEAR mode — reduced activity")
    else:
        if USE_BINANCE and time.time() < cfg._binance_ban_until:
            return
        if crypto_regime["mode"] == "BEAR":
            log.info(f"[{st.label}] BEAR mode — skipping crypto buys")

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1

    # Exit management first
    check_stop_losses(st, crypto=crypto)
    if st.shutoff: st.running = False; return

    # Scan
    if crypto and USE_BINANCE:
        bars_data = {}
        for sym in watchlist[:20]:
            b = fetch_bars(sym, crypto=True)
            if b: bars_data[sym] = b
    else:
        bars_data = fetch_bars_batch(watchlist)

    results = []
    for sym in watchlist:
        if sym not in bars_data: continue
        bars    = bars_data[sym]
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        prev    = closes[-2] if len(closes) > 1 else price
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, e9, e21, rsi_val = get_signal(closes, volumes)
        sig_score = score_signal(sym, price, change, rsi_val, vol_ratio, closes, bars=bars)

        # Near miss tracking
        if (sig_score >= MIN_SIGNAL_SCORE - 2 and sig_score < MIN_SIGNAL_SCORE
                and signal == "BUY" and sym not in st.positions):
            skip_reason = (
                "SCORE" if sig_score < MIN_SIGNAL_SCORE else
                "RSI_HIGH" if rsi_val and rsi_val > 70 else
                "VOL_LOW" if vol_ratio < 1.2 else "EMA"
            )
            track_near_miss(sym, sig_score, skip_reason)

        results.append({
            "symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21,
            "ema_cross": ("✅" if e9 and e21 and e9 > e21 else "–"),
            "rsi": rsi_val, "vol_ratio": vol_ratio, "score": sig_score,
            "closes": closes,
        })

    results.sort(key=lambda x: (-x.get("score", 0), {"BUY":0,"HOLD":1,"SELL":2}.get(x["signal"], 1)))
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY" and r.get("score", 0) >= MIN_SIGNAL_SCORE)
    log.info(f"[{st.label}] {buys} qualified BUY / {len(results)} scanned")

    # Entry logic
    pos_count = len(st.positions)
    for s in results:
        if s["score"] < MIN_SIGNAL_SCORE: continue
        if s["signal"] != "BUY": continue
        if pos_count >= MAX_POSITIONS: break
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[{st.label}] Global position cap reached")
            break
        sym = s["symbol"]
        if sym in st.positions: continue
        if sym in all_symbols_held(): continue
        sym_sector = SECTOR_MAP.get(sym)
        if sym_sector and sectors_held().get(sym_sector, 0) >= MAX_SECTOR_POSITIONS: continue
        if sym in news_state.get("skip_list", {}): continue
        if circuit_breaker["active"] and not crypto: continue
        if (not crypto and market_regime["mode"] == "BEAR"
                and sym not in ["SQQQ","UVXY","GLD","SLV","SPXS","SH","PSQ","SDOW","TLT","VXX"]):
            continue
        if crypto and crypto_regime["mode"] == "BEAR": continue

        # Capital efficiency
        if pos_count >= MAX_POSITIONS:
            exit_sym = check_capital_efficiency(st, s["score"], sym)
            if exit_sym:
                pos = st.positions[exit_sym]
                live_exit = fetch_latest_price(exit_sym, crypto=crypto)
                if live_exit:
                    pnl = (live_exit - pos["entry_price"]) * pos["qty"]
                    place_order(exit_sym, "sell", pos["qty"], crypto=crypto, estimated_price=live_exit)
                    if exit_sym in exchange_stops:
                        cancel_stop_order_ibkr(exchange_stops.pop(exit_sym))
                    del st.positions[exit_sym]
                    st.daily_pnl += pnl
                    st.trades.insert(0, {"symbol": exit_sym, "side": "SELL", "qty": pos["qty"],
                        "price": live_exit, "pnl": pnl, "reason": "🔄 ROTATE",
                        "time": datetime.now().strftime("%H:%M:%S")})
                    db_save_trade(exit_sym, "SELL", pos["qty"], live_exit, pnl, "ROTATE", None, None)
                    pos_count -= 1
            else:
                break

        # Size the trade
        base_size = cfg.MAX_TRADE_VALUE
        size_mult = equity_curve_size_factor() * vol_adjusted_size(1.0) * news_size_multiplier(sym)
        trade_val = min(base_size * size_mult, base_size)
        qty = max(1, int(trade_val / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        if total_exposure(st) + trade_val > MAX_PORTFOLIO_EXPOSURE: continue

        stop_price = s["price"] * (1 - STOP_LOSS_PCT / 100)
        tp_price   = s["price"] * (1 + TAKE_PROFIT_PCT / 100)
        log.info(f"[{st.label}] Executing: BUY {sym} x{qty} @ ~${s['price']:.4f} | score:{s['score']:.1f} | stop:${stop_price:.4f} | target:${tp_price:.4f}")

        place_order._last_score = s["score"]
        order, fill_price = place_order(sym, "buy", qty, crypto=crypto, estimated_price=s["price"])
        if order:
            actual_stop = fill_price * (1 - STOP_LOSS_PCT / 100)
            actual_tp   = fill_price * (1 + TAKE_PROFIT_PCT / 100)
            if not crypto:
                stop_order = place_stop_order_ibkr(sym, qty, round(actual_stop, 2))
                if stop_order and stop_order.get("id"):
                    exchange_stops[sym] = stop_order["id"]
                else:
                    log.error(f"[EMERGENCY] Stop order FAILED for {sym} — emergency exit")
                    place_order(sym, "sell", qty, estimated_price=fill_price)
                    continue
            now_ts = datetime.now().isoformat()
            st.positions[sym] = {
                "qty": qty, "entry_price": fill_price,
                "stop_price": actual_stop, "highest_price": fill_price,
                "take_profit_price": actual_tp,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0,
                "signal_score": s["score"],
            }
            st.daily_spend  += trade_val
            st.trades_today += 1
            st.trades.insert(0, {"symbol": sym, "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_ts})
            db_save_trade(sym, "BUY", qty, fill_price, None, "Signal", s["score"], None)
            pos_count += 1

    # Sell signals
    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos  = st.positions[s["symbol"]]
        pnl  = (s["price"] - pos["entry_price"]) * pos["qty"]
        entry_ts   = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None
        log.info(f"[{st.label}] SELL {s['symbol']} @ ${s['price']:.4f} P&L:${pnl:+.2f}")
        if not crypto and s["symbol"] in exchange_stops:
            cancel_stop_order_ibkr(exchange_stops.pop(s["symbol"]))
        place_order(s["symbol"], "sell", pos["qty"], crypto=crypto, estimated_price=s["price"])
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        record_trade_result(pnl, s["symbol"])
        db_save_trade(s["symbol"], "SELL", pos["qty"], s["price"], pnl, "Signal",
                      pos.get("signal_score"), hold_hours)
        if st.daily_pnl >= DAILY_PROFIT_TARGET: st.shutoff = True; break
        if st.daily_pnl <= -MAX_DAILY_LOSS:     st.shutoff = True; break

    st.running = False


# ── Small cap cycle ───────────────────────────────────────────
def run_cycle_smallcap(watchlist, st):
    st.check_reset()
    if st.shutoff: return
    if kill_switch["active"]: return
    if market_regime["mode"] == "BEAR":
        log.info("[SMALLCAP] BEAR MODE — pausing")
        return

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[SMALLCAP] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Pool: {len(watchlist)} stocks")

    # Exit management
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym)
        if not live: continue
        now = datetime.now()
        if live > pos.get("highest_price", pos["entry_price"]):
            pos["highest_price"] = live
            new_stop = live * (1 - SMALLCAP_STOP_LOSS / 100)
            if new_stop > pos["stop_price"]:
                pos["stop_price"] = new_stop
        pct    = ((live - pos["entry_price"]) / pos["entry_price"]) * 100
        reason = None
        if live <= pos["stop_price"]:
            reason = f"Stop-Loss ({pct:.1f}%)"
        elif live >= pos.get("take_profit_price", pos["entry_price"] * 1.05):
            reason = f"Take-Profit (+{pct:.1f}%)"
        elif pos.get("days_held", 0) >= cfg.MAX_HOLD_DAYS:
            reason = "Max Hold"
        if reason:
            pnl      = (live - pos["entry_price"]) * pos["qty"]
            entry_ts = pos.get("entry_ts")
            hold_h   = round((now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None
            log.info(f"[SMALLCAP] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")
            place_order(sym, "sell", pos["qty"])
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": reason,
                "time": now.strftime("%H:%M:%S"), "hold_hours": hold_h})
            db_save_trade(sym, "SELL", pos["qty"], live, pnl, reason, None, hold_h)
            if st.daily_pnl <= -MAX_DAILY_LOSS: st.shutoff = True; break
    if st.shutoff: st.running = False; return

    results = []
    for sym in watchlist:
        if sym in news_state["skip_list"]: continue
        bars = fetch_bars(sym)
        if not bars: continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        if not (SMALLCAP_MIN_PRICE <= price <= SMALLCAP_MAX_PRICE): continue
        prev      = closes[-2] if len(closes) > 1 else price
        change    = ((price - prev) / prev) * 100
        avg_vol   = sum(volumes[-10:]) / min(10, len(volumes))
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, e9, e21, rsi_val = get_signal_smallcap(closes, volumes)
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi_val,
            "vol_ratio": vol_ratio, "smallcap": True})

    results.sort(key=lambda x: {"BUY":0,"HOLD":1,"SELL":2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[SMALLCAP] {buys} BUY signals from {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= MAX_POSITIONS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= MAX_PORTFOLIO_EXPOSURE: break
        qty       = max(1, int(cfg.SMALLCAP_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price        = s["price"] * (1 - SMALLCAP_STOP_LOSS / 100)
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)
        log.info(f"[SMALLCAP] BUY {s['symbol']} @ ${s['price']:.4f} x{qty} = ${trade_val:.0f}")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": stop_price, "highest_price": fill_price,
                "take_profit_price": take_profit_price,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_ts})
            db_save_trade(s["symbol"], "BUY", qty, fill_price, None, "Signal", None, None)
            pos_count += 1
    st.running = False


# ── Intraday stock cycle ──────────────────────────────────────
def run_intraday_cycle(watchlist, st):
    st.check_reset()
    if st.shutoff: return
    if not is_intraday_window(): return
    if market_regime["mode"] == "BEAR": return
    if circuit_breaker["active"]: return

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1

    check_intraday_positions(st, crypto=False)
    if st.shutoff: st.running = False; return

    bars_batch = fetch_intraday_bars_batch(watchlist, timeframe=INTRADAY_TIMEFRAME, limit=INTRADAY_BARS)
    results = []
    for sym in watchlist:
        if sym in news_state.get("skip_list", {}): continue
        bars = bars_batch.get(sym)
        if not bars or len(bars) < 14: continue
        closes    = [b["c"] for b in bars]
        volumes   = [b["v"] for b in bars]
        price     = closes[-1]
        prev      = closes[-2]
        change    = ((price - prev) / prev) * 100
        avg_vol   = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, ef, es, rsi_val = get_intraday_signal(
            closes, volumes, INTRADAY_EMA_FAST, INTRADAY_EMA_SLOW,
            INTRADAY_RSI_LIMIT, INTRADAY_VOL_RATIO
        )
        vwap_pos = vwap_signal(bars)
        if signal == "BUY" and vwap_pos == "BELOW":
            signal = "HOLD"
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
        if all_positions_count() >= MAX_TOTAL_POSITIONS: break
        if s["symbol"] in all_symbols_held(): continue
        qty       = max(1, int(cfg.INTRADAY_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - INTRADAY_STOP_LOSS / 100)
        tp_price   = s["price"] * (1 + INTRADAY_TAKE_PROFIT / 100)
        log.info(f"[INTRADAY] BUY {s['symbol']} @ ${s['price']:.2f} x{qty}")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            actual_stop = fill_price * (1 - INTRADAY_STOP_LOSS / 100)
            actual_tp   = fill_price * (1 + INTRADAY_TAKE_PROFIT / 100)
            stop_order  = place_stop_order_ibkr(s["symbol"], qty, round(actual_stop, 2))
            if stop_order and stop_order.get("id"):
                exchange_stops[s["symbol"]] = stop_order["id"]
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
            db_save_trade(s["symbol"], "BUY", qty, fill_price, None, "[ID]Signal", None, None)
            pos_count += 1
    st.running = False


# ── Intraday crypto cycle ─────────────────────────────────────
def run_crypto_intraday_cycle(watchlist, st):
    st.check_reset()
    if st.shutoff: return
    if USE_BINANCE and time.time() < cfg._binance_ban_until: return
    if crypto_regime["mode"] == "BEAR": return

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1

    check_intraday_positions(st, crypto=True)
    if st.shutoff: st.running = False; return

    scan_list = watchlist[:10] if USE_BINANCE else watchlist
    results = []
    for sym in scan_list:
        bars = fetch_intraday_bars(sym, timeframe=CRYPTO_INTRADAY_TIMEFRAME,
                                   limit=CRYPTO_INTRADAY_BARS, crypto=True)
        if not bars or len(bars) < 14: continue
        closes    = [b["c"] for b in bars]
        volumes   = [b["v"] for b in bars]
        price     = closes[-1]
        prev      = closes[-2]
        change    = ((price - prev) / prev) * 100
        avg_vol   = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, ef, es, rsi_val = get_intraday_signal(
            closes, volumes,
            CRYPTO_INTRADAY_EMA_FAST, CRYPTO_INTRADAY_EMA_SLOW,
            INTRADAY_RSI_LIMIT, CRYPTO_INTRADAY_VOL_RATIO
        )
        vwap_pos = vwap_signal(bars)
        if signal == "BUY" and vwap_pos == "BELOW":
            signal = "HOLD"
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": ef, "sma21": es, "rsi": rsi_val,
            "vol_ratio": vol_ratio, "vwap": vwap_pos, "intraday": True})

    results.sort(key=lambda x: {"BUY":0,"HOLD":1,"SELL":2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY")
    log.info(f"[CRYPTO_ID] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")
    log.info(f"[CRYPTO_ID] {buys} BUY / {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= CRYPTO_INTRADAY_MAX_POS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        qty       = max(0.0001, round(cfg.CRYPTO_INTRADAY_MAX_TRADE / s["price"], 6))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        log.info(f"[CRYPTO_ID] BUY {s['symbol']} @ ${s['price']:.4f}")
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=True, estimated_price=s["price"])
        if order:
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
                "price": fill_price, "pnl": None, "reason": "[CID]Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_ts})
            pos_count += 1
    st.running = False


# ── International market hours ────────────────────────────────
def is_asx_open():
    """ASX: Mon-Fri 00:00-06:00 UTC."""
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return 0 <= now.hour < 6

def is_ftse_open():
    """LSE: Mon-Fri 08:00-16:30 UTC."""
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return (now.hour == 8 and now.minute >= 0) or (9 <= now.hour < 16) or (now.hour == 16 and now.minute < 30)

def update_asx_regime():
    """Use CBA as ASX market proxy."""
    try:
        bars = fetch_bars("CBA", crypto=False)
        if not bars or len(bars) < 20: return
        prices = [b["c"] for b in bars[-20:]]
        ma20   = sum(prices) / 20
        price  = prices[-1]
        prev   = asx_regime.get("mode", "BULL")
        asx_regime["mode"] = "BULL" if price > ma20 else "BEAR"
        asx_regime.update({"spy": price, "ma20": ma20, "updated": datetime.utcnow()})
        if asx_regime["mode"] != prev:
            log.info(f"[ASX REGIME] Changed → {asx_regime['mode']}")
        log.info(f"[ASX REGIME] {asx_regime['mode']} | CBA: ${price:.2f} MA20: ${ma20:.2f}")
    except Exception as e:
        log.warning(f"[ASX REGIME] update failed: {e}")

def update_ftse_regime():
    """Use HSBA as FTSE market proxy."""
    try:
        bars = fetch_bars("HSBA", crypto=False)
        if not bars or len(bars) < 20: return
        prices = [b["c"] for b in bars[-20:]]
        ma20   = sum(prices) / 20
        price  = prices[-1]
        prev   = ftse_regime.get("mode", "BULL")
        ftse_regime["mode"] = "BULL" if price > ma20 else "BEAR"
        ftse_regime.update({"spy": price, "ma20": ma20, "updated": datetime.utcnow()})
        if ftse_regime["mode"] != prev:
            log.info(f"[FTSE REGIME] Changed → {ftse_regime['mode']}")
        log.info(f"[FTSE REGIME] {ftse_regime['mode']} | HSBA: ${price:.2f} MA20: ${ma20:.2f}")
    except Exception as e:
        log.warning(f"[FTSE REGIME] update failed: {e}")

def run_intl_cycle(watchlist, st, regime, market_open_fn, label):
    """Run a scan cycle for ASX or FTSE.
    st is a BotState object (asx_state or ftse_state).
    """
    if not market_open_fn(): return
    if regime["mode"] == "BEAR": return

    results = []
    for sym in watchlist:
        try:
            bars = fetch_bars(sym, crypto=False)
            if not bars or len(bars) < 22: continue
            closes    = [b["c"] for b in bars]
            volumes   = [b.get("v", 0) for b in bars]
            price     = closes[-1]
            prev      = closes[-2] if len(closes) > 1 else price
            change    = ((price - prev) / prev) * 100
            avg_vol   = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 1
            vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
            signal, e9, e21, rsi_val = get_signal(closes, volumes)
            sig_score = score_signal(sym, price, change, rsi_val, vol_ratio, closes, bars=bars) if signal == "BUY" else 0
            ema_cross = "✅" if e9 and e21 and e9 > e21 else "–"
            results.append({
                "symbol": sym, "price": price, "change": change,
                "signal": signal, "score": sig_score, "rsi": rsi_val,
                "ema_cross": ema_cross, "vol_ratio": vol_ratio,
                "sma9": e9, "sma21": e21, "closes": closes,
            })
        except Exception as e:
            log.debug(f"[{label}] {sym} scan error: {e}")

    results.sort(key=lambda x: -x.get("score", 0))
    st.candidates = results  # BotState.candidates — works correctly now
    buys = [r for r in results if r["signal"] == "BUY" and r["score"] >= MIN_SIGNAL_SCORE]
    log.info(f"[{label}] {len(buys)} qualified BUY / {len(results)} scanned")

    # Entry
    for s in buys[:5]:
        sym = s["symbol"]
        if sym in st.positions: continue
        if all_positions_count() >= MAX_TOTAL_POSITIONS: break
        qty = max(1, int(MAX_TRADE_VALUE / s["price"]))
        try:
            place_order._last_score = s["score"]
            order, fill = place_order(sym, "buy", qty, crypto=False, estimated_price=s["price"])
            if order:
                stop = fill * (1 - STOP_LOSS_PCT / 100)
                tp   = fill * (1 + TAKE_PROFIT_PCT / 100)
                st.positions[sym] = {
                    "qty": qty, "entry_price": fill, "stop_price": stop,
                    "highest_price": fill, "take_profit_price": tp,
                    "entry_date": datetime.now().date().isoformat(),
                    "entry_ts": datetime.now().isoformat(), "days_held": 0,
                    "signal_score": s["score"],
                }
                st.trades.insert(0, {"symbol": sym, "side": "BUY", "qty": qty,
                    "price": fill, "pnl": None, "reason": "Signal",
                    "time": datetime.now().strftime("%H:%M:%S")})
                log.info(f"[{label}] BUY {sym} qty={qty} @ ${fill:.2f} score={s['score']}")
        except Exception as e:
            log.warning(f"[{label}] place_order {sym}: {e}")

    # Exit
    for sym in list(st.positions.keys()):
        pos = st.positions[sym]
        try:
            price = fetch_latest_price(sym, crypto=False)
            if not price: continue
            entry = pos["entry_price"]
            pnl   = (price - entry) * pos["qty"]
            if price <= entry * (1 - STOP_LOSS_PCT / 100) or price >= entry * (1 + TAKE_PROFIT_PCT / 100):
                place_order(sym, "sell", pos["qty"], crypto=False, estimated_price=price)
                del st.positions[sym]
                st.daily_pnl += pnl
                st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                    "price": price, "pnl": pnl, "reason": "Stop/TP",
                    "time": datetime.now().strftime("%H:%M:%S")})
                db_save_trade(sym, "SELL", pos["qty"], price, pnl, "Stop/TP", None, None)
                log.info(f"[{label}] SELL {sym} @ ${price:.2f} P&L: ${pnl:+.2f}")
        except Exception as e:
            log.warning(f"[{label}] exit check {sym}: {e}")


# ── Email / notifications ─────────────────────────────────────
def send_daily_summary():
    try:
        from app.notifications import send_email, tg
        from core.risk import calc_profit_factor, calc_sharpe
        all_t   = perf["all_trades"]
        wins    = sum(1 for t in all_t if t.get("pnl", 0) > 0)
        total   = len(all_t)
        wr      = int(wins / total * 100) if total else 0
        pnl_today = state.daily_pnl + crypto_state.daily_pnl
        subject = f"AlphaBot Daily — {'+' if pnl_today >= 0 else ''}${pnl_today:.2f}"
        body = (f"Daily P&L: ${pnl_today:+.2f}\n"
                f"Positions: {len(state.positions)}S / {len(crypto_state.positions)}C\n"
                f"Win rate: {wr}% ({wins}/{total})\n"
                f"Profit factor: {calc_profit_factor():.2f}\n"
                f"Max drawdown: {perf['max_drawdown']:.1f}%")
        send_email(subject, body)
        tg(f"📊 <b>Daily Summary</b>\nP&L: <b>${pnl_today:+.2f}</b>\n{wr}% win rate", category="daily")
        db_save_daily_report(subject, body)
    except Exception as e:
        log.warning(f"[EMAIL] Daily summary failed: {e}")

def run_morning_news_scan():
    try:
        from app.notifications import tg
        news_state["last_scan_day"]  = datetime.now().date()
        news_state["last_scan_time"] = datetime.now().strftime("%H:%M")
        news_state["scan_complete"]  = True
        log.info("[NEWS] Morning scan complete")
    except Exception as e:
        log.warning(f"[NEWS] Scan failed: {e}")

def send_weekly_near_miss_email():
    try:
        from app.notifications import send_email
        if not near_miss_tracker: return
        lines = [f"{sym}: score={d['score']:.1f} reason={d['skip_reason']}" for sym, d in list(near_miss_tracker.items())[:20]]
        body  = "Near-miss report (stocks that scored just below threshold):\n\n" + "\n".join(lines)
        send_email("AlphaBot Weekly Near-Miss Report", body)
        log.info(f"[WEEKLY] Near-miss report sent ({len(near_miss_tracker)} entries)")
    except Exception as e:
        log.warning(f"[WEEKLY] Near-miss email failed: {e}")

def run_near_miss_simulations():
    db_update_near_misses()


# ── Main orchestration ────────────────────────────────────────
def main():
    cfg.account_info = {}

    log.info("=" * 50)
    log.info("AlphaBot starting up")
    log.info(f"Mode:   {'LIVE' if IS_LIVE else 'PAPER'} trading")
    log.info(f"Broker: IBKR (IB Gateway)")
    log.info(f"Port:   {cfg.PORT}")
    log.info("=" * 50)

    # Start dashboard
    from app.dashboard import start_dashboard
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()
    time.sleep(2)
    log.info(f"Dashboard ready on port {cfg.PORT}")

    # Verify IBKR connection
    cfg.account_info = ibkr_get_account() or {}
    if not cfg.account_info:
        log.error("Cannot connect to IBKR — check TWS/Gateway connection")
    else:
        log.info(f"Connected — Portfolio: ${float(cfg.account_info.get('portfolio_value', 0)):,.2f}")

    # Binance startup
    if USE_BINANCE:
        mode = "TESTNET" if cfg.BINANCE_USE_TESTNET else ("LIVE" if IS_LIVE else "PAPER")
        log.info(f"[BINANCE] Mode: {mode} | Endpoint: {cfg.BINANCE_BASE}")
        log.info(f"[BINANCE] Scanning {len(CRYPTO_WATCHLIST)} coins — will connect on first cycle")

    # Startup position recovery from IBKR
    def run_ibkr_startup_recovery():
        recovered = 0
        try:
            from core.execution import get_ib
            ib_conn = get_ib()
            if not ib_conn or not ib_conn.isConnected():
                return
            ibkr_positions = ib_conn.positions()
            open_orders    = ib_conn.openOrders()
            stop_syms = {
                o.contract.symbol for o in open_orders
                if hasattr(o, 'contract') and hasattr(o, 'order')
                and getattr(o.order, 'orderType', '') == 'STP'
            }
            for pos in ibkr_positions:
                sym   = pos.contract.symbol
                qty   = float(pos.position)
                entry = float(pos.avgCost)
                if qty <= 0: continue
                stop = entry * (1 - STOP_LOSS_PCT / 100)
                tp   = entry * (1 + TAKE_PROFIT_PCT / 100)
                if sym not in state.positions:
                    state.positions[sym] = {
                        "qty": qty, "entry_price": entry, "stop_price": stop,
                        "highest_price": entry, "take_profit_price": tp,
                        "entry_date": datetime.now().date().isoformat(),
                        "days_held": 0, "entry_ts": datetime.now().isoformat(),
                    }
                    log.info(f"[RECOVERY] Restored position: {sym} x{qty} @ ${entry:.2f}")
                    if sym not in stop_syms:
                        log.warning(f"[RECOVERY] {sym} has no stop on IBKR — placing now @ ${stop:.2f}")
                        stop_order = place_stop_order_ibkr(sym, int(qty), round(stop, 2))
                        if stop_order and stop_order.get("id"):
                            exchange_stops[sym] = stop_order["id"]
                            log.info(f"[RECOVERY] Stop placed for {sym} @ ${stop:.2f}")
                    else:
                        log.info(f"[RECOVERY] {sym} already has stop on IBKR ✅")
                    recovered += 1
            log.info(f"=== Recovered {recovered} open position(s) ===")
        except Exception as e:
            log.error(f"Startup recovery failed: {e}")

    run_ibkr_startup_recovery()

    last_email_day = None
    cycle          = 0

    while True:
        try:
            cycle += 1
            log.info(f"\n{'─'*50}")
            log.info(f"Main cycle {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            log.info(
                f"[WATCHDOG] Cycle {cycle} alive | "
                f"Stocks P&L: ${state.daily_pnl:+.2f} | "
                f"Crypto P&L: ${crypto_state.daily_pnl:+.2f} | "
                f"Positions: {len(state.positions)}S/{len(crypto_state.positions)}C"
            )

            # Every 10 cycles: reconcile positions with IBKR
            if cycle % 10 == 0:
                try:
                    ibkr_pos  = ibkr_get_positions() or []
                    ibkr_syms = {p.get("symbol") for p in ibkr_pos}
                    local_syms = set(state.positions.keys())
                    phantom = local_syms - ibkr_syms
                    for sym in phantom:
                        log.warning(f"[RECONCILE] {sym} in local state but NOT on IBKR — removing phantom")
                        del state.positions[sym]
                except Exception as e:
                    log.warning(f"[WATCHDOG] Reconciliation failed: {e}")

            # Refresh account info
            cfg.account_info = ibkr_get_account() or cfg.account_info

            # Dynamic limit scaling from live balances
            if cfg.account_info:
                ibkr_pv    = float(cfg.account_info.get("portfolio_value", 1000))
                binance_pv = cfg._binance_balance_cache.get("value", 0.0)
                cache_age  = time.time() - cfg._binance_balance_cache.get("ts", 0)
                ban_clear  = time.time() >= (cfg._binance_ban_until + 300)
                if USE_BINANCE and ban_clear and cache_age > 600:
                    try:
                        fresh = binance_get_balance("USDT")
                        if fresh is not None:
                            binance_pv = fresh
                            cfg._binance_balance_cache["value"] = fresh
                            cfg._binance_balance_cache["ts"]    = time.time()
                    except:
                        pass

                total_pv    = ibkr_pv + binance_pv
                crypto_base = binance_pv if binance_pv > 0 else ibkr_pv * 0.20

                cfg.MAX_DAILY_LOSS            = total_pv * cfg.MAX_DAILY_LOSS_PCT / 100
                cfg.DAILY_PROFIT_TARGET       = total_pv * cfg.DAILY_PROFIT_TARGET_PCT / 100
                cfg.MAX_DAILY_SPEND           = ibkr_pv  * cfg.MAX_DAILY_SPEND_PCT / 100
                cfg.MAX_PORTFOLIO_EXPOSURE    = ibkr_pv  * cfg.MAX_EXPOSURE_PCT / 100
                cfg.MAX_TRADE_VALUE           = ibkr_pv  * cfg.MAX_TRADE_PCT / 100
                cfg.INTRADAY_MAX_TRADE        = ibkr_pv  * 0.03
                cfg.SMALLCAP_MAX_TRADE        = ibkr_pv  * 0.025
                cfg.CRYPTO_MAX_EXPOSURE       = crypto_base * cfg.MAX_EXPOSURE_PCT / 100
                cfg.CRYPTO_INTRADAY_MAX_TRADE = crypto_base * 0.02

                log.info(
                    f"[SIZING] IBKR:${ibkr_pv:,.2f} + Binance:${binance_pv:,.2f} = Total:${total_pv:,.2f} | "
                    f"StockTrade:${cfg.MAX_TRADE_VALUE:.0f} CryptoTrade:${cfg.CRYPTO_INTRADAY_MAX_TRADE:.0f} "
                    f"DailyLoss:${cfg.MAX_DAILY_LOSS:.0f}"
                )

            # Performance analytics
            if cfg.account_info:
                pv      = float(cfg.account_info.get("portfolio_value", 0))
                update_drawdown(pv)
                last_pv = float(cfg.account_info.get("last_equity", pv))
                if last_pv > 0:
                    daily_ret = (pv - last_pv) / last_pv * 100
                    if daily_ret not in perf["sharpe_daily"]:
                        perf["sharpe_daily"].append(daily_ret)
                        perf["sharpe_daily"] = perf["sharpe_daily"][-30:]

            # Panic kill switch — portfolio down 5%
            if cfg.account_info:
                pv      = float(cfg.account_info.get("portfolio_value", 0))
                last_pv = float(cfg.account_info.get("last_equity", pv))
                if last_pv > 0:
                    drawdown_pct = ((pv - last_pv) / last_pv) * 100
                    if drawdown_pct <= -5.0:
                        log.warning(f"PANIC KILL SWITCH: Portfolio down {drawdown_pct:.1f}%!")
                        for sym, pos in list(state.positions.items()):
                            place_order(sym, "sell", pos["qty"], crypto=False, estimated_price=pos["entry_price"])
                            if sym in exchange_stops:
                                cancel_stop_order_ibkr(exchange_stops.pop(sym))
                        for sym, pos in list(crypto_state.positions.items()):
                            place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
                        state.positions.clear()
                        crypto_state.positions.clear()
                        for st_obj in [state, crypto_state, smallcap_state, intraday_state,
                                       crypto_intraday_state, asx_state, ftse_state]:
                            st_obj.shutoff = True
                        circuit_breaker["active"] = True
                        circuit_breaker["reason"] = f"PANIC: Portfolio -{abs(drawdown_pct):.1f}% today"

            # Near-miss + regime updates
            update_near_miss_prices()
            if not IS_LIVE or is_market_open():
                update_market_regime()
                check_circuit_breaker()
            update_crypto_regime()
            update_asx_regime()
            update_ftse_regime()

            # Weekly Binance watchlist refresh (Monday 9am ET)
            et_now = datetime.now(ZoneInfo("America/New_York"))
            if (USE_BINANCE and et_now.weekday() == 0 and et_now.hour == 9 and et_now.minute < 2):
                log.info("[BINANCE] Refreshing top coins list...")
                fresh = binance_get_top_coins(100)
                if fresh:
                    CRYPTO_WATCHLIST[:] = fresh
                    log.info(f"[BINANCE] Watchlist updated: {len(CRYPTO_WATCHLIST)} coins")

            # Small cap pool refresh
            if should_refresh_smallcap() and is_market_open():
                log.info("[SMALLCAP] Starting pool refresh in background...")
                threading.Thread(target=refresh_smallcap_pool, daemon=True).start()

            # ── Run all scan cycles ──
            run_cycle(US_WATCHLIST, state, crypto=False)
            run_cycle(CRYPTO_WATCHLIST, crypto_state, crypto=True)
            run_intl_cycle(ASX_WATCHLIST, asx_state, asx_regime, is_asx_open, "ASX")
            run_intl_cycle(FTSE_WATCHLIST, ftse_state, ftse_regime, is_ftse_open, "FTSE")
            if smallcap_pool["symbols"]:
                run_cycle_smallcap(smallcap_pool["symbols"], smallcap_state)
            run_intraday_cycle(US_WATCHLIST, intraday_state)
            run_crypto_intraday_cycle(CRYPTO_WATCHLIST, crypto_intraday_state)

            # Morning news scan at 9:00am ET
            et = datetime.now(ZoneInfo("America/New_York"))
            if (et.weekday() < 5 and et.hour == 9 and et.minute < 2
                    and news_state["last_scan_day"] != et.date()):
                log.info("Running morning news scan...")
                def morning_tasks():
                    check_macro_news()
                    run_morning_news_scan()
                threading.Thread(target=morning_tasks, daemon=True).start()

            # Daily email at 5pm ET
            if et.hour == 17 and et.minute < 2 and last_email_day != et.date():
                send_daily_summary()
                last_email_day = et.date()

            # Weekly near-miss report — Sunday 6pm ET
            if et.weekday() == 6 and et.hour == 18 and et.minute < 2:
                threading.Thread(target=send_weekly_near_miss_email, daemon=True).start()

            # Daily near-miss simulations — noon ET
            if et.hour == 12 and et.minute < 2:
                threading.Thread(target=run_near_miss_simulations, daemon=True).start()

            # Intraday sub-cycles
            intraday_cycles = CYCLE_SECONDS // INTRADAY_CYCLE_SECONDS
            for _ in range(intraday_cycles):
                run_intraday_cycle(US_WATCHLIST, intraday_state)
                if not (USE_BINANCE and time.time() < (cfg._binance_ban_until + 300)):
                    run_crypto_intraday_cycle(CRYPTO_WATCHLIST, crypto_intraday_state)
                time.sleep(INTRADAY_CYCLE_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            log.error(f"[CRASH] Error in main loop: {e}")
            log.error(f"[CRASH] Bot recovering — sleeping 30s then resuming")
            try:
                ibkr_pos  = ibkr_get_positions() or []
                ibkr_syms = {p.get("symbol") for p in ibkr_pos}
                for sym in list(state.positions.keys()):
                    if sym not in ibkr_syms:
                        log.warning(f"[CRASH RECOVERY] {sym} not on IBKR — removing phantom")
                        del state.positions[sym]
            except:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
