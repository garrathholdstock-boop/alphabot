"""
app/main.py — AlphaBot Main Loop
All trading cycle functions (swing, intraday, smallcap, crypto)
and the main orchestration loop.
"""

import time
import threading
import logging
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from core.config import (
    log, IS_LIVE, USE_BINANCE, CYCLE_SECONDS, INTRADAY_CYCLE_SECONDS,
    MIN_SIGNAL_SCORE, MAX_POSITIONS, MAX_TOTAL_POSITIONS, MAX_TRADES_PER_DAY,
    MAX_DAILY_LOSS, MAX_DAILY_SPEND, MAX_PORTFOLIO_EXPOSURE, DAILY_PROFIT_TARGET,
    MAX_TRADE_VALUE, CRYPTO_MAX_EXPOSURE, INTRADAY_MAX_TRADE,
    CRYPTO_INTRADAY_MAX_TRADE, SMALLCAP_MAX_TRADE, SMALLCAP_MAX_TRADE,
    STOP_LOSS_PCT, TRAILING_STOP_PCT, TAKE_PROFIT_PCT,
    CRYPTO_STOP_PCT, SMALLCAP_STOP_LOSS,
    INTRADAY_STOP_LOSS, INTRADAY_TAKE_PROFIT, INTRADAY_MAX_POSITIONS,
    CRYPTO_INTRADAY_SL, CRYPTO_INTRADAY_TP, CRYPTO_INTRADAY_MAX_POS,
    CRYPTO_INTRADAY_EMA_FAST, CRYPTO_INTRADAY_EMA_SLOW, CRYPTO_INTRADAY_VOL_RATIO,
    CRYPTO_INTRADAY_TIMEFRAME, CRYPTO_INTRADAY_BARS,
    INTRADAY_TIMEFRAME, INTRADAY_BARS, INTRADAY_EMA_FAST,
    INTRADAY_EMA_SLOW, INTRADAY_RSI_LIMIT, INTRADAY_VOL_RATIO,
    SMALLCAP_MIN_PRICE, SMALLCAP_MAX_PRICE,
    NEWS_API_KEY,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    global_risk, perf, kill_switch, circuit_breaker,
    market_regime, crypto_regime, news_state, near_miss_tracker,
    exchange_stops, account_info, smallcap_pool,
    CRYPTO_WATCHLIST, US_WATCHLIST,
    _state_lock,
    MAX_DAILY_LOSS_PCT, MAX_DAILY_SPEND_PCT, MAX_EXPOSURE_PCT,
    DAILY_PROFIT_TARGET_PCT, MAX_TRADE_PCT, CRYPTO_EXPOSURE_PCT,
    INTRADAY_TRADE_PCT, CRYPTO_INTRADAY_PCT,
    SECTOR_MAP, MAX_SECTOR_POSITIONS,
)
import core.config as cfg

from core.execution import (
    alpaca_get, alpaca_post, fetch_bars, fetch_bars_batch,
    fetch_latest_price, fetch_intraday_bars, fetch_intraday_bars_batch,
    place_order, place_stop_order_alpaca, cancel_stop_order_alpaca,
    update_exchange_stop, binance_get_top_coins,
)
from core.risk import (
    total_exposure, all_positions_count, all_symbols_held, sectors_held,
    calc_unrealized_pnl, check_stop_losses, is_loss_streak_paused,
    record_trade_result, record_trade_with_score,
    update_drawdown, calc_profit_factor, calc_sharpe,
    equity_curve_size_factor, vol_adjusted_size, news_size_multiplier,
    is_market_open, update_market_regime, update_crypto_regime,
    check_circuit_breaker, check_macro_news, is_choppy_market,
    is_intraday_window,
)
from data.analytics import (
    get_signal, get_signal_smallcap, get_intraday_signal,
    score_signal, signal_breakdown, sell_breakdown,
    vwap_signal, is_breakout, calc_rsi, calc_macd, calc_adx,
    record_near_miss, update_near_miss_prices, mark_near_miss_triggered,
    run_near_miss_simulations, analyse_edge,
)
from data.database import db_record_trade, db_record_near_miss, db_record_report
from app.notifications import (
    tg, tg_trade_buy, tg_trade_sell, tg_hot_miss, tg_critical,
    run_morning_news_scan, send_daily_summary, send_weekly_near_miss_email,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


# ── Small cap pool management ─────────────────────────────────
def refresh_smallcap_pool():
    global smallcap_pool
    log.info("[SMALLCAP] Refreshing small cap pool...")
    try:
        assets = alpaca_get("/v2/assets?status=active&asset_class=us_equity")
        if not assets:
            log.warning("[SMALLCAP] Could not fetch assets from Alpaca")
            return
        candidates = [
            a for a in assets
            if (a.get("tradable")
                and a.get("exchange") in ("NYSE","NASDAQ","ARCA")
                and a.get("status") == "active"
                and not a.get("symbol","").endswith(("W","R","P","Q"))
                and len(a.get("symbol","")) <= 5)
        ]
        log.info(f"[SMALLCAP] {len(candidates)} tradable candidates found")
        scored = []
        checked = 0
        for asset in candidates:
            sym = asset.get("symbol","")
            if not sym or sym in US_WATCHLIST: continue
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
            scored.append({"symbol": sym, "price": price, "avg_vol": avg_vol, "momentum": momentum, "score": sc})
            checked += 1
            if checked >= 300: break
            time.sleep(0.1)
        scored.sort(key=lambda x: x["score"], reverse=True)
        from core.config import SMALLCAP_POOL_SIZE
        pool = [s["symbol"] for s in scored[:SMALLCAP_POOL_SIZE]]
        smallcap_pool["symbols"]          = pool
        smallcap_pool["last_refresh"]     = datetime.now().strftime("%Y-%m-%d %H:%M")
        smallcap_pool["last_refresh_day"] = datetime.now().date()
        log.info(f"[SMALLCAP] Pool refreshed: {len(pool)} stocks | Top 5: {pool[:5]}")
    except Exception as e:
        log.error(f"[SMALLCAP] Pool refresh error: {e}")

def should_refresh_smallcap():
    from core.config import SMALLCAP_REFRESH_DAYS
    if not smallcap_pool["symbols"]: return True
    if not smallcap_pool["last_refresh_day"]: return True
    days_since = (datetime.now().date() - smallcap_pool["last_refresh_day"]).days
    return days_since >= SMALLCAP_REFRESH_DAYS


# ── Intraday position manager ─────────────────────────────────
def check_intraday_positions(st, crypto=False):
    sl_pct = CRYPTO_INTRADAY_SL if crypto else INTRADAY_STOP_LOSS
    tp_pct = CRYPTO_INTRADAY_TP if crypto else INTRADAY_TAKE_PROFIT
    now    = datetime.now()
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym, crypto=crypto)
        if not live: continue
        entry = pos["entry_price"]
        high  = pos.get("highest_price", entry)
        pct   = ((live - entry) / entry) * 100
        if live > high:
            pos["highest_price"] = live
            new_stop = live * (1 - sl_pct / 100)
            if new_stop > pos["stop_price"]:
                pos["stop_price"] = new_stop
        reason = None
        if live >= pos.get("take_profit_price", entry * 1.025): reason = f"Take-Profit (+{pct:.1f}%)"
        elif live <= pos["stop_price"]:                          reason = f"Stop-Loss ({pct:.1f}%)"
        if not crypto and not is_intraday_window() and is_market_open():
            reason = "End-of-Window"
        if reason:
            pnl      = (live - entry) * pos["qty"]
            entry_ts = pos.get("entry_ts")
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


# ── Main swing trading cycle ──────────────────────────────────
def run_cycle(watchlist, st, crypto=False):
    st.check_reset()
    if st.shutoff: return
    if kill_switch["active"]: return
    if is_loss_streak_paused(): return
    if not crypto and not is_market_open(): return
    if market_regime["mode"] == "BEAR" and not crypto:
        log.info(f"[{st.label}] BEAR MODE — rotating to defensive tickers")

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[{st.label}] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Positions: {len(st.positions)}")

    check_stop_losses(st, crypto=crypto)
    if st.shutoff: st.running = False; return

    # Fetch bars — batched for stocks, individual for crypto
    if not crypto:
        bars_cache = fetch_bars_batch(watchlist)
    else:
        bars_cache = {}

    results = []
    for sym in watchlist:
        if sym in news_state.get("skip_list", {}): continue
        if USE_BINANCE and crypto and time.time() < cfg._binance_ban_until: break

        if not crypto and sym in bars_cache:
            bars = bars_cache[sym]
        else:
            bars = fetch_bars(sym, crypto=crypto)
        if not bars or len(bars) < 22: continue

        closes  = [b["c"] for b in bars]
        volumes = [b.get("v", 0) for b in bars]
        price   = closes[-1]
        prev    = closes[-2] if len(closes) > 1 else price
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, e9, e21, rsi = get_signal(closes, volumes)
        sig_score = score_signal(sym, price, change, rsi, vol_ratio, closes, bars=bars) if signal == "BUY" else 0
        results.append({
            "symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21,
            "rsi": rsi, "vol_ratio": vol_ratio, "score": sig_score,
            "closes": closes,
        })

    results.sort(key=lambda x: (-x.get("score", 0), {"BUY":0,"HOLD":1,"SELL":2}.get(x["signal"], 1)))
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY" and r.get("score", 0) >= MIN_SIGNAL_SCORE)
    log.info(f"[{st.label}] {buys} qualified BUY / {len(results)} scanned")

    # ── Open new positions ──
    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= MAX_POSITIONS: break
        if s["symbol"] in st.positions: continue
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[{st.label}] Global position cap ({MAX_TOTAL_POSITIONS}) reached")
            break
        if s["symbol"] in all_symbols_held(): continue
        sym_sector = SECTOR_MAP.get(s["symbol"])
        if sym_sector and sectors_held().get(sym_sector, 0) >= MAX_SECTOR_POSITIONS:
            log.info(f"[{st.label}] SKIP {s['symbol']} — sector {sym_sector} full")
            continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= MAX_PORTFOLIO_EXPOSURE: break

        stop_pct_use  = CRYPTO_STOP_PCT if crypto else STOP_LOSS_PCT
        base_qty      = max(1 if not crypto else 0.0001,
                            int(MAX_TRADE_VALUE / s["price"]) if not crypto
                            else round(CRYPTO_INTRADAY_MAX_TRADE / s["price"], 6))
        eq_factor     = equity_curve_size_factor()
        vol_factor    = vol_adjusted_size(1.0)
        news_factor   = news_size_multiplier(s["symbol"])
        qty           = max(1 if not crypto else 0.0001,
                            int(base_qty * eq_factor * vol_factor * news_factor) if not crypto
                            else round(base_qty * eq_factor * vol_factor * news_factor, 6))
        trade_val     = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue

        sig_score = s.get("score", 0)
        if sig_score < MIN_SIGNAL_SCORE:
            if sig_score >= MIN_SIGNAL_SCORE - 1.5:
                record_near_miss(s["symbol"], sig_score, s["price"], crypto=crypto)
                db_record_near_miss(s["symbol"], sig_score, MIN_SIGNAL_SCORE,
                                    MIN_SIGNAL_SCORE - sig_score, s["price"], crypto, "SCORE")
                if sig_score >= MIN_SIGNAL_SCORE - 0.5:
                    tg_hot_miss(s["symbol"], sig_score, "SCORE_THRESHOLD", s["price"])
            log.info(f"[{st.label}] SKIP {s['symbol']} score:{sig_score}/10 below threshold {MIN_SIGNAL_SCORE}")
            continue

        if not crypto and is_choppy_market():
            log.info(f"[{st.label}] SKIP — choppy market")
            break

        total_trades_today = sum(s2.trades_today for s2 in [state, crypto_state])
        if total_trades_today >= MAX_TRADES_PER_DAY:
            log.info(f"[{st.label}] Max trades per day ({MAX_TRADES_PER_DAY}) reached")
            break

        stop_price        = s["price"] * (1 - stop_pct_use / 100)
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)

        breakdown = signal_breakdown(
            s["symbol"], s["price"], s.get("change", 0), s.get("rsi"),
            s.get("vol_ratio", 1), s.get("closes", [s["price"]]*22),
            sig_score, crypto=crypto
        )
        log.info(f"[{st.label}] ✅ BUY SIGNAL BREAKDOWN:\n{breakdown}")
        log.info(f"[{st.label}] Executing: BUY {s['symbol']} x{qty} @ ~${s['price']:.4f} | stop:${stop_price:.4f} | target:${take_profit_price:.4f}")

        mark_near_miss_triggered(s["symbol"])
        place_order._last_score = sig_score
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=crypto, estimated_price=s["price"])

        if order and fill_price:
            tg_trade_buy(s["symbol"], fill_price, sig_score, market="crypto" if crypto else "stock")

        if not order:
            log.warning(f"[{st.label}] ORDER FAILED for {s['symbol']}")
            record_near_miss(s["symbol"], sig_score, s["price"], crypto=crypto)
            continue

        actual_stop = fill_price * (1 - stop_pct_use / 100)
        actual_tp   = fill_price * (1 + TAKE_PROFIT_PCT / 100)
        now_ts = datetime.now().isoformat()
        st.positions[s["symbol"]] = {
            "qty": qty, "entry_price": fill_price,
            "stop_price": actual_stop, "highest_price": fill_price,
            "take_profit_price": actual_tp,
            "entry_date": datetime.now().date().isoformat(),
            "entry_ts": now_ts, "days_held": 0,
            "signal_score": sig_score, "entry_breakdown": breakdown,
        }

        if not crypto:
            stop_order = place_stop_order_alpaca(s["symbol"], qty, round(actual_stop, 2))
            if stop_order and stop_order.get("id"):
                exchange_stops[s["symbol"]] = stop_order["id"]
                log.info(f"[{st.label}] Exchange stop placed for {s['symbol']} @ ${actual_stop:.2f}")
            else:
                log.error(f"[EMERGENCY] Stop order FAILED for {s['symbol']} — emergency exit")
                place_order(s["symbol"], "sell", qty, crypto=False, estimated_price=fill_price)
                if s["symbol"] in st.positions: del st.positions[s["symbol"]]
                pos_count -= 1
                continue

        st.daily_spend += trade_val
        st.trades_today += 1
        st.trades.insert(0, {
            "symbol": s["symbol"], "side": "BUY", "qty": qty,
            "price": fill_price, "pnl": None, "reason": "Signal",
            "time": datetime.now().strftime("%H:%M:%S"),
            "entry_ts": now_ts, "score": sig_score,
            "rsi": s.get("rsi"), "vol_ratio": s.get("vol_ratio"),
            "breakdown": breakdown,
        })
        pos_count += 1

    # ── Close SELL positions ──
    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        entry_ts   = pos.get("entry_ts")
        hold_hours = round((datetime.now() - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 1) if entry_ts else None

        if not crypto and s["symbol"] in exchange_stops:
            cancel_stop_order_alpaca(exchange_stops.pop(s["symbol"]))

        order_sell, sell_price = place_order(s["symbol"], "sell", pos["qty"], crypto=crypto, estimated_price=s["price"])
        pnl = (sell_price - pos["entry_price"]) * pos["qty"]
        bd  = sell_breakdown(s["symbol"], pos, sell_price, pnl, "Signal", hold_hours, crypto=crypto)
        log.info(f"[{st.label}] SELL BREAKDOWN:\n{bd}")

        if order_sell:
            del st.positions[s["symbol"]]
            st.daily_pnl += pnl
            st.trades_today += 1
            st.trades.insert(0, {
                "symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
                "price": sell_price, "pnl": pnl, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"),
                "hold_hours": hold_hours, "breakdown": bd,
            })
            st.trades = st.trades[:200]
            record_trade_with_score(pnl, s["symbol"], score=pos.get("signal_score"), hold_hours=hold_hours)
            tg_trade_sell(s["symbol"], sell_price, pnl, hold_hours or 0, "Signal", market="crypto" if crypto else "stock")
            db_record_trade(s["symbol"], "SELL", pos["qty"], sell_price, pnl,
                            pos.get("signal_score"), None, None, hold_hours, "Signal", bd,
                            "crypto" if crypto else "stock")
            if st.daily_pnl >= DAILY_PROFIT_TARGET:
                log.info(f"[{st.label}] Profit target hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"[{st.label}] Loss limit hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break

    st.running = False


# ── Small cap cycle ───────────────────────────────────────────
def run_cycle_smallcap(watchlist, st):
    st.check_reset()
    if st.shutoff: return
    if not is_market_open(): return
    if market_regime["mode"] == "BEAR":
        log.info("[SMALLCAP] BEAR MODE — pausing all small cap buys")
        return

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[SMALLCAP] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Pool: {len(watchlist)} stocks")

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
        if live <= pos["stop_price"]:   reason = f"Stop-Loss ({pct:.1f}%)"
        elif live >= pos.get("take_profit_price", pos["entry_price"] * 1.05): reason = f"Take-Profit (+{pct:.1f}%)"
        elif pos.get("days_held", 0) >= cfg.MAX_HOLD_DAYS: reason = "Max Hold"
        if reason:
            pnl      = (live - pos["entry_price"]) * pos["qty"]
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
        signal, e9, e21, rsi = get_signal_smallcap(closes, volumes)
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi,
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
        qty       = max(1, int(SMALLCAP_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price        = s["price"] * (1 - SMALLCAP_STOP_LOSS / 100)
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)
        log.info(f"[SMALLCAP] BUY {s['symbol']} @ ${s['price']:.4f} x{qty} = ${trade_val:.0f}")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            now_str = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": stop_price, "highest_price": fill_price,
                "take_profit_price": take_profit_price,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_str, "days_held": 0}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_str})
            pos_count += 1

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
        if st.daily_pnl <= -MAX_DAILY_LOSS:     st.shutoff = True; break
    st.running = False


# ── Intraday stock cycle ──────────────────────────────────────
def run_intraday_cycle(watchlist, st):
    st.check_reset()
    if st.shutoff: return
    if not is_intraday_window(): return
    if market_regime["mode"] == "BEAR": return
    if circuit_breaker["active"]:
        log.info("[INTRADAY] Circuit breaker active — skipping")
        return

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[INTRADAY] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")

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
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[INTRADAY] Global position cap ({MAX_TOTAL_POSITIONS}) reached")
            break
        if s["symbol"] in all_symbols_held(): continue
        sym_sector = SECTOR_MAP.get(s["symbol"])
        if sym_sector and sectors_held().get(sym_sector, 0) >= MAX_SECTOR_POSITIONS: continue
        qty       = max(1, int(INTRADAY_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - INTRADAY_STOP_LOSS / 100)
        tp_price   = s["price"] * (1 + INTRADAY_TAKE_PROFIT / 100)
        log.info(f"[INTRADAY] BUY {s['symbol']} @ ${s['price']:.2f} x{qty}")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            actual_stop = fill_price * (1 - INTRADAY_STOP_LOSS / 100)
            actual_tp   = fill_price * (1 + INTRADAY_TAKE_PROFIT / 100)
            stop_order  = place_stop_order_alpaca(s["symbol"], qty, round(actual_stop, 2))
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


# ── Intraday crypto cycle ─────────────────────────────────────
def run_crypto_intraday_cycle(watchlist, st):
    st.check_reset()
    if st.shutoff: return
    if USE_BINANCE and time.time() < cfg._binance_ban_until: return
    if crypto_regime["mode"] == "BEAR":
        log.info("[CRYPTO_ID] Bear mode — skipping intraday buys")
        return

    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[CRYPTO_ID] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f}")

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
    log.info(f"[CRYPTO_ID] {buys} BUY / {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if pos_count >= CRYPTO_INTRADAY_MAX_POS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= CRYPTO_MAX_EXPOSURE: break
        qty       = max(0.0001, round(CRYPTO_INTRADAY_MAX_TRADE / s["price"], 6))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - CRYPTO_INTRADAY_SL / 100)
        tp_price   = s["price"] * (1 + CRYPTO_INTRADAY_TP / 100)
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
        pnl = (sell_price - pos["entry_price"]) * pos["qty"]
        log.info(f"[CRYPTO_ID] SELL {s['symbol']} @ ${sell_price:.4f} P&L:${pnl:+.2f}")
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "[ID]Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        if st.daily_pnl >= DAILY_PROFIT_TARGET: st.shutoff = True; break
        if st.daily_pnl <= -MAX_DAILY_LOSS:     st.shutoff = True; break
    st.running = False


# ── Main orchestration loop ───────────────────────────────────
def main():
    global account_info
    cfg.account_info = {}

    log.info("=" * 50)
    log.info("AlphaBot starting up")
    log.info(f"Mode:   {'LIVE' if IS_LIVE else 'PAPER'} trading")
    log.info(f"Port:   {cfg.PORT}")
    log.info("=" * 50)

    # Start dashboard first — Railway health check needs it immediately
    from app.dashboard import start_dashboard
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()
    time.sleep(2)
    log.info(f"Dashboard ready on port {cfg.PORT}")

    # Verify Alpaca connection
    cfg.account_info = alpaca_get("/v2/account") or {}
    if not cfg.account_info:
        log.error("Cannot connect to Alpaca — check ALPACA_KEY and ALPACA_SECRET")
    else:
        log.info(f"Connected — Portfolio: ${float(cfg.account_info.get('portfolio_value',0)):,.2f}")

    # Binance startup — NO API calls to avoid triggering bans on restart
    if USE_BINANCE:
        mode = "TESTNET" if cfg.BINANCE_USE_TESTNET else ("LIVE" if IS_LIVE else "PAPER")
        log.info(f"[BINANCE] Mode: {mode} | Endpoint: {cfg.BINANCE_BASE}")
        log.info(f"[BINANCE] Scanning {len(CRYPTO_WATCHLIST)} coins — will connect on first cycle")

    # ── Startup position recovery ──
    log.info("=== Startup recovery check ===")
    try:
        open_positions = alpaca_get("/v2/positions") or []
        recovered = 0
        for pos in open_positions:
            sym      = pos.get("symbol")
            qty      = float(pos.get("qty", 0))
            entry    = float(pos.get("avg_entry_price", 0))
            stop_pct = cfg.CRYPTO_STOP_PCT if "/" in str(sym) else STOP_LOSS_PCT
            stop     = entry * (1 - stop_pct / 100)
            tp       = entry * (1 + TAKE_PROFIT_PCT / 100)
            is_crypto    = pos.get("asset_class") == "crypto" or "/" in str(sym) or str(sym).endswith("USD") and sym not in ["BUSD"]
            # Additional check — if symbol is in crypto watchlist it's crypto

            if sym in CRYPTO_WATCHLIST or sym.replace("/","") + "USD" in [c.replace("/","") for c in CRYPTO_WATCHLIST]:
                is_crypto = True
            target_state = crypto_state if is_crypto else state
            if sym not in target_state.positions:
                target_state.positions[sym] = {
                    "qty": qty, "entry_price": entry, "stop_price": stop,
                    "highest_price": entry, "take_profit_price": tp,
                    "entry_date": datetime.now().date().isoformat(),
                    "days_held": 0, "entry_ts": datetime.now().isoformat(),
                }
                current_price = fetch_latest_price(sym, crypto=is_crypto)
                if current_price and current_price <= stop:
                    pnl = (current_price - entry) * qty
                    log.warning(f"[RECOVERY] {sym} already below stop — closing immediately P&L:${pnl:+.2f}")
                    place_order(sym, "sell", qty, crypto=is_crypto, estimated_price=current_price)
                    continue
                if not is_crypto:
                    stop_order = place_stop_order_alpaca(sym, qty, round(stop, 2))
                    if stop_order and stop_order.get("id"):
                        exchange_stops[sym] = stop_order["id"]
                        log.info(f"[RECOVERY] Restored {sym} — exchange stop re-placed")
                recovered += 1
        log.info(f"=== Recovered {recovered} open position(s) ===")
    except Exception as e:
        log.error(f"Startup recovery failed: {e}")

    # ── Verify exchange stops ──
    log.info("=== Verifying exchange stops on all positions ===")
    try:
        open_orders    = alpaca_get("/v2/orders?status=open") or []
        stop_order_syms = {o["symbol"] for o in open_orders if o.get("type") == "stop"}
        for sym, pos in state.positions.items():
            if sym not in stop_order_syms and sym not in exchange_stops:
                log.warning(f"[STOPS] {sym} has no exchange stop — placing now")
                stop_order = place_stop_order_alpaca(sym, pos["qty"], round(pos["stop_price"], 2))
                if stop_order and stop_order.get("id"):
                    exchange_stops[sym] = stop_order["id"]
    except Exception as e:
        log.error(f"Stop verification failed: {e}")

    last_email_day = None
    cycle = 0

    while True:
        try:
            cycle += 1
            log.info(f"\n{'─'*50}")
            log.info(f"Main cycle {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            log.info(f"[WATCHDOG] Cycle {cycle} alive | Stocks P&L: ${state.daily_pnl:+.2f} | Crypto P&L: ${crypto_state.daily_pnl:+.2f} | Positions: {len(state.positions)}S/{len(crypto_state.positions)}C")

            # Every 10 cycles: verify stops + reconcile positions
            if cycle % 10 == 0:
                try:
                    open_orders = alpaca_get("/v2/orders?status=open") or []
                    stop_syms   = {o["symbol"] for o in open_orders if o.get("type") == "stop"}
                    for sym, pos in state.positions.items():
                        if sym not in stop_syms:
                            log.warning(f"[WATCHDOG] Exchange stop missing for {sym} — replacing")
                            stop_order = place_stop_order_alpaca(sym, pos["qty"], round(pos["stop_price"], 2))
                            if stop_order and stop_order.get("id"):
                                exchange_stops[sym] = stop_order["id"]
                    broker_positions = alpaca_get("/v2/positions") or []
                    broker_syms = {p["symbol"] for p in broker_positions}
                    local_syms  = set(state.positions.keys())
                    phantom = local_syms - broker_syms
                    for sym in phantom:
                        log.warning(f"[RECONCILE] {sym} in local state but NOT on broker — removing phantom")
                        del state.positions[sym]
                    for p in broker_positions:
                        sym = p["symbol"]
                        if sym in broker_syms - local_syms:
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
                            log.warning(f"[RECONCILE] {sym} found on broker but missing locally — re-added")
                except Exception as e:
                    log.warning(f"[WATCHDOG] Reconciliation failed: {e}")

            # Refresh account info
            cfg.account_info = alpaca_get("/v2/account") or cfg.account_info

            # ── Dynamic limit scaling from live balances ──
            if cfg.account_info:
                alpaca_pv  = float(cfg.account_info.get("portfolio_value", 1000))
                binance_pv = cfg._binance_balance_cache.get("value", 0.0)
                cache_age  = time.time() - cfg._binance_balance_cache.get("ts", 0)
                ban_clear  = time.time() >= (cfg._binance_ban_until + 300)
                if USE_BINANCE and ban_clear and cache_age > 600:
                    try:
                        from core.execution import binance_get_balance
                        fresh = binance_get_balance("USDT")
                        if fresh is not None:
                            binance_pv = fresh
                            cfg._binance_balance_cache["value"] = fresh
                            cfg._binance_balance_cache["ts"]    = time.time()
                    except: pass

                total_pv    = alpaca_pv + binance_pv
                crypto_base = binance_pv if binance_pv > 0 else alpaca_pv * 0.20

                cfg.MAX_DAILY_LOSS         = total_pv * cfg.MAX_DAILY_LOSS_PCT / 100
                cfg.DAILY_PROFIT_TARGET    = total_pv * cfg.DAILY_PROFIT_TARGET_PCT / 100
                cfg.MAX_DAILY_SPEND        = alpaca_pv * cfg.MAX_DAILY_SPEND_PCT / 100
                cfg.MAX_PORTFOLIO_EXPOSURE = alpaca_pv * cfg.MAX_EXPOSURE_PCT / 100
                cfg.MAX_TRADE_VALUE        = alpaca_pv * cfg.MAX_TRADE_PCT / 100
                cfg.INTRADAY_MAX_TRADE     = alpaca_pv * 0.03
                cfg.SMALLCAP_MAX_TRADE     = alpaca_pv * 0.025
                cfg.CRYPTO_MAX_EXPOSURE    = crypto_base * cfg.MAX_EXPOSURE_PCT / 100
                cfg.CRYPTO_INTRADAY_MAX_TRADE = crypto_base * 0.02

                log.info(
                    f"[SIZING] Alpaca:${alpaca_pv:,.2f} + Binance:${binance_pv:,.2f} = Total:${total_pv:,.2f} | "
                    f"StockTrade:${cfg.MAX_TRADE_VALUE:.0f} CryptoTrade:${cfg.CRYPTO_INTRADAY_MAX_TRADE:.0f} DailyLoss:${cfg.MAX_DAILY_LOSS:.0f}"
                )

            # Performance analytics
            if cfg.account_info:
                pv = float(cfg.account_info.get("portfolio_value", 0))
                update_drawdown(pv)
                last_pv = float(cfg.account_info.get("last_equity", pv))
                if last_pv > 0:
                    daily_ret = (pv - last_pv) / last_pv * 100
                    if daily_ret not in perf["sharpe_daily"]:
                        perf["sharpe_daily"].append(daily_ret)
                        perf["sharpe_daily"] = perf["sharpe_daily"][-30:]

            # ── PANIC KILL SWITCH ──
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
                                cancel_stop_order_alpaca(exchange_stops.pop(sym))
                        for sym, pos in list(crypto_state.positions.items()):
                            place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
                        state.positions.clear()
                        crypto_state.positions.clear()
                        for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state]:
                            st.shutoff = True
                        circuit_breaker["active"] = True
                        circuit_breaker["reason"] = f"PANIC: Portfolio -{abs(drawdown_pct):.1f}% today"
                        tg_critical(f"🚨 PANIC KILL SWITCH: Portfolio down {drawdown_pct:.1f}%! All positions closed.")

            # Near-miss + regime updates
            update_near_miss_prices()
            if not IS_LIVE or is_market_open():
                update_market_regime()
                check_circuit_breaker()
            update_crypto_regime()

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

            # ── Run all bot cycles ──
            run_cycle(US_WATCHLIST, state, crypto=False)
            run_cycle(CRYPTO_WATCHLIST, crypto_state, crypto=True)
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
                log.info("[WEEKLY] Generating near-miss analysis report...")
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
            log.info("Stopped")
            break
        except Exception as e:
            log.error(f"[CRASH] Error in main loop: {e}")
            log.error(f"[CRASH] Bot recovering — sleeping 30s then resuming")
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
