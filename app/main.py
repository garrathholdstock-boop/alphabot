"""
app/main.py — AlphaBot Main Loop
All trading cycle functions (swing, intraday, smallcap, crypto)
and the main orchestration loop.
"""

# Self-load .env so this process works correctly after VPS reboot / systemd start
import os as _os, pathlib as _pathlib
_env_path = _pathlib.Path("/home/alphabot/app/.env")
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _os.environ.setdefault(_k.strip(), _v.strip())

import time
import threading
import logging
import json
import os
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
    state, crypto_state, smallcap_state, smallcap_asx_state, smallcap_ftse_state,
    intraday_state, crypto_intraday_state, asx_state, ftse_state,
    asx_state, ftse_state, bear_state,
    global_risk, perf, kill_switch, circuit_breaker,
    market_regime, crypto_regime, asx_regime, ftse_regime, news_state, near_miss_tracker,
    exchange_stops, account_info, smallcap_pool,
    CRYPTO_WATCHLIST, US_WATCHLIST, ASX_WATCHLIST, FTSE_WATCHLIST, BEAR_WATCHLIST,
    US_SMALLCAP_WATCHLIST, FTSE_SMALLCAP_WATCHLIST, ASX_SMALLCAP_WATCHLIST,
    _state_lock,
    MAX_DAILY_LOSS_PCT, MAX_DAILY_SPEND_PCT, MAX_EXPOSURE_PCT,
    DAILY_PROFIT_TARGET_PCT, MAX_TRADE_PCT, CRYPTO_EXPOSURE_PCT,
    INTRADAY_TRADE_PCT, CRYPTO_INTRADAY_PCT,
    SECTOR_MAP, MAX_SECTOR_POSITIONS,
)
import core.config as cfg
from core.config import load_trading_config

from core.execution import (
    ibkr_get_account, ibkr_get_positions, ibkr_get_open_orders, fetch_bars, fetch_bars_batch,
    fetch_latest_price, fetch_intraday_bars, fetch_intraday_bars_batch,
    place_order,
    update_exchange_stop, binance_get_top_coins, update_live_prices,
    start_ibkr_manager,
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
    vwap_signal, is_breakout, calc_rsi, calc_macd, calc_adx, calc_atr,
    record_near_miss, update_near_miss_prices, mark_near_miss_triggered,
    run_near_miss_simulations, analyse_edge,
    load_near_miss_tracker_from_db,
)
from data.database import (
    db_record_trade, db_record_near_miss, db_record_report,
    db_record_rotation, db_get_pending_rotations, db_update_rotation_followup,
    db_write_status, db_write_smallcap_watchlists, db_read_smallcap_watchlists,
    db_read_watchlist, db_write_watchlist, db_read_all_watchlists,
    db_write_positions, db_read_positions,
    db_write_portfolio, db_read_portfolio,
)
from app.notifications import (
    tg, tg_trade_buy, tg_trade_sell, tg_hot_miss, tg_critical,
    run_morning_news_scan, send_daily_summary, send_weekly_near_miss_email,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


# ── Small cap pool management ─────────────────────────────────
def load_all_watchlists_from_db():
    """Load all watchlists from DB on startup. Any market without a DB entry
    keeps its config.py default. Also updates _INTL_MARKET for exchange routing."""
    import core.config as _cfg
    from core.execution import _INTL_MARKET
    try:
        all_wl = db_read_all_watchlists()
    except Exception as e:
        log.warning(f"[WATCHLIST] DB load failed: {e} — using config defaults")
        return

    if not all_wl:
        log.info("[WATCHLIST] No DB watchlists — using config defaults")
        return

    # Main markets — if DB has them, replace config lists in place
    loaded = []
    if "us" in all_wl:
        _cfg.US_WATCHLIST[:] = all_wl["us"]["tickers"]
        loaded.append(f"US:{len(all_wl['us']['tickers'])}")
    if "ftse" in all_wl:
        _cfg.FTSE_WATCHLIST[:] = all_wl["ftse"]["tickers"]
        for s in all_wl["ftse"]["tickers"]:
            _INTL_MARKET[s] = ("LSE", "GBP")
        loaded.append(f"FTSE:{len(all_wl['ftse']['tickers'])}")
    if "asx" in all_wl:
        _cfg.ASX_WATCHLIST[:] = all_wl["asx"]["tickers"]
        for s in all_wl["asx"]["tickers"]:
            _INTL_MARKET[s] = ("ASX", "AUD")
        loaded.append(f"ASX:{len(all_wl['asx']['tickers'])}")
    if "crypto" in all_wl:
        _cfg.CRYPTO_WATCHLIST[:] = all_wl["crypto"]["tickers"]
        loaded.append(f"CRYPTO:{len(all_wl['crypto']['tickers'])}")
    if "bear" in all_wl:
        _cfg.BEAR_WATCHLIST[:] = all_wl["bear"]["tickers"]
        loaded.append(f"BEAR:{len(all_wl['bear']['tickers'])}")

    # Smallcaps — also write to smallcap_pool so run_cycle_smallcap sees them
    if "us_smallcap" in all_wl:
        smallcap_pool["us"] = all_wl["us_smallcap"]["tickers"]
        _cfg.US_SMALLCAP_WATCHLIST[:] = all_wl["us_smallcap"]["tickers"]
        loaded.append(f"SmUS:{len(all_wl['us_smallcap']['tickers'])}")
    if "ftse_smallcap" in all_wl:
        smallcap_pool["ftse"] = all_wl["ftse_smallcap"]["tickers"]
        _cfg.FTSE_SMALLCAP_WATCHLIST[:] = all_wl["ftse_smallcap"]["tickers"]
        for s in all_wl["ftse_smallcap"]["tickers"]:
            _INTL_MARKET[s] = ("LSE", "GBP")
        loaded.append(f"SmFTSE:{len(all_wl['ftse_smallcap']['tickers'])}")
    if "asx_smallcap" in all_wl:
        smallcap_pool["asx"] = all_wl["asx_smallcap"]["tickers"]
        _cfg.ASX_SMALLCAP_WATCHLIST[:] = all_wl["asx_smallcap"]["tickers"]
        for s in all_wl["asx_smallcap"]["tickers"]:
            _INTL_MARKET[s] = ("ASX", "AUD")
        loaded.append(f"SmASX:{len(all_wl['asx_smallcap']['tickers'])}")

    if loaded:
        log.info(f"[WATCHLIST] Loaded from DB: {' | '.join(loaded)}")
    else:
        log.info("[WATCHLIST] DB empty for all markets — using config defaults")


def update_smallcap_watchlists(us=None, ftse=None, asx=None):
    """Update smallcap watchlists — called on startup and by agent Refresh Small Caps."""
    import core.config as _cfg
    # On startup with no args, try loading from DB first
    if us is None and ftse is None and asx is None:
        try:
            saved = db_read_smallcap_watchlists()
            if saved and saved.get("us"):
                us   = saved["us"]
                ftse = saved["ftse"]
                asx  = saved["asx"]
                log.info(f"[SMALLCAP] Loaded from DB (updated {saved.get('updated_at','?')}) | US:{len(us)} FTSE:{len(ftse)} ASX:{len(asx)}")
            else:
                log.info("[SMALLCAP] No DB watchlists found — using config defaults")
                return
        except Exception as e:
            log.warning(f"[SMALLCAP] DB load failed: {e} — using config defaults")
            return
    if us:
        smallcap_pool["us"]   = us
        _cfg.US_SMALLCAP_WATCHLIST[:] = us
    if ftse:
        smallcap_pool["ftse"] = ftse
        _cfg.FTSE_SMALLCAP_WATCHLIST[:] = ftse
    if asx:
        smallcap_pool["asx"]  = asx
        _cfg.ASX_SMALLCAP_WATCHLIST[:] = asx
    smallcap_pool["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info(f"[SMALLCAP] Watchlists updated | US:{len(smallcap_pool['us'])} FTSE:{len(smallcap_pool['ftse'])} ASX:{len(smallcap_pool['asx'])}")
    # Update IBKR exchange routing for new symbols
    try:
        from core.execution import _INTL_MARKET
        for s in smallcap_pool["ftse"]: _INTL_MARKET[s] = ("LSE", "GBP")
        for s in smallcap_pool["asx"]:  _INTL_MARKET[s] = ("ASX", "AUD")
    except: pass


# ── Intraday position manager ─────────────────────────────────
def check_intraday_positions(st, crypto=False):
    sl_pct = CRYPTO_INTRADAY_SL if crypto else INTRADAY_STOP_LOSS
    tp_pct = CRYPTO_INTRADAY_TP if crypto else INTRADAY_TAKE_PROFIT
    now    = datetime.now(ZoneInfo("UTC"))
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
            hold_hours = round((now - datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")) if datetime.fromisoformat(entry_ts).tzinfo is None else now - datetime.fromisoformat(entry_ts)).total_seconds() / 3600, 2) if entry_ts else None
            log.info(f"[{st.label}] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")
            place_order(sym, "sell", pos["qty"], crypto=crypto)
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": f"[ID]{reason}",
                "time": now.strftime("%H:%M:%S"), "hold_hours": hold_hours})
            db_record_trade(sym, "SELL", pos["qty"], live, pnl,
                pos.get("signal_score"), None, None, hold_hours,
                f"[ID]{reason}", "", "crypto" if crypto else "stock",
                discipline="crypto_intraday" if crypto else "stock_intraday",
                exit_category=("STOP" if "Stop" in str(reason) else "TP" if "Take-Profit" in str(reason) else "EOD"))
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
        if not bars or len(bars) < (15 if crypto else 22): continue

        closes  = [b["c"] for b in bars]
        volumes = [b.get("v", 0) for b in bars]
        price   = closes[-1]
        prev    = closes[-2] if len(closes) > 1 else price
        change  = ((price - prev) / prev) * 100
        avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, e9, e21, rsi = get_signal(closes, volumes)
        sig_score = score_signal(sym, price, change, rsi, vol_ratio, closes, bars=bars)
        results.append({
            "symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21, "ema_cross": ("✅" if e9 and e21 and e9 > e21 else "–"),
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
        if s["score"] < MIN_SIGNAL_SCORE: continue
        if pos_count >= MAX_POSITIONS:
            # ── Opportunity cost rotation ─────────────────────
            worst_sym, worst_curr_score, worst_pct = None, 999, 0
            for held_sym, held_pos in st.positions.items():
                held_cand = next((r for r in results if r["symbol"] == held_sym), None)
                if not held_cand: continue
                curr_score = held_cand.get("score", 0)
                entry_price = held_pos.get("entry_price", 0)
                curr_price  = held_cand["price"]
                pct_profit  = ((curr_price - entry_price) / entry_price * 100) if entry_price else 0
                if curr_score < worst_curr_score:
                    worst_curr_score = curr_score
                    worst_sym        = held_sym
                    worst_pct        = pct_profit
                    worst_pos        = held_pos
                    worst_price      = curr_price

            score_gap = sig_score - worst_curr_score

            # ── Logic 2: Stale capital exit ───────────────────
            stale_sym = None
            for held_sym, held_pos in st.positions.items():
                held_cand = next((r for r in results if r["symbol"] == held_sym), None)
                if not held_cand: continue
                entry_ts = held_pos.get("entry_ts")
                if not entry_ts: continue
                hold_mins = (datetime.now(ZoneInfo("UTC")) - (datetime.fromisoformat(entry_ts) if datetime.fromisoformat(entry_ts).tzinfo else datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 60
                ep = held_pos.get("entry_price", 0)
                cp = held_cand["price"]
                flat_pct = abs((cp - ep) / ep * 100) if ep else 99
                if hold_mins >= 30 and flat_pct <= 0.5:
                    stale_sym = held_sym
                    stale_pos = held_pos
                    stale_price = cp
                    break

            if stale_sym and not (worst_sym and score_gap >= 1.5):
                ep = stale_pos.get("entry_price", 0)
                cp = stale_price
                flat_pct = abs((cp - ep) / ep * 100) if ep else 0
                hold_mins = (datetime.now(ZoneInfo("UTC")) - (_ts2 if (_ts2 := datetime.fromisoformat(stale_pos.get("entry_ts","2000-01-01T00:00:00+00:00"))).tzinfo else _ts2.replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 60 if stale_pos.get("entry_ts") else 0
                log.info(f"[{st.label}] ⏱ STALE EXIT: {stale_sym} flat {flat_pct:.2f}% after {hold_mins:.0f}min → freeing slot for {s['symbol']} (score {sig_score:.1f})")
                ord_st, st_price = place_order(stale_sym, "sell", stale_pos["qty"], crypto=crypto, estimated_price=stale_price)
                if ord_st:
                    pnl_st = (st_price - stale_pos["entry_price"]) * stale_pos["qty"]
                    del st.positions[stale_sym]
                    st.daily_pnl += pnl_st
                    st.trades.insert(0, {"symbol": stale_sym, "side": "SELL", "qty": stale_pos["qty"],
                        "price": st_price, "pnl": pnl_st, "reason": "StaleCapital",
                        "time": datetime.now().strftime("%H:%M:%S")})
                    db_record_trade(stale_sym, "SELL", stale_pos["qty"], st_price, pnl_st,
                        stale_pos.get("signal_score"), None, None, None, "StaleCapital", "",
                        "crypto" if crypto else "stock",
                        discipline="crypto_swing" if crypto else "stock_swing",
                        adx_at_entry=stale_pos.get("_adx_entry"),
                        macd_bullish=stale_pos.get("_macd_bull"),
                        breakout=stale_pos.get("_breakout"),
                        rs_vs_spy=stale_pos.get("_rs_spy"),
                        news_state=stale_pos.get("_news_state"),
                        regime_at_entry=stale_pos.get("_regime"),
                        vix_at_entry=stale_pos.get("_vix"),
                        exit_category="STALE")
                    try:
                        db_record_rotation("STALE_EXIT",
                            sold_symbol=stale_sym, sold_price=st_price,
                            sold_score=stale_pos.get("signal_score"), sold_pnl=pnl_st,
                            bought_symbol=s["symbol"], bought_price=s["price"],
                            bought_score=s.get("score"), market="crypto" if crypto else "stock")
                    except Exception: pass
                    pos_count -= 1
                else:
                    break

            # Minimum hold time before rotation — don't sell winners too early
            worst_hold_mins = 0
            if worst_pos and worst_pos.get("entry_ts"):
                try:
                    worst_hold_mins = (datetime.now(ZoneInfo("UTC")) - (_ts3 if (_ts3 := datetime.fromisoformat(worst_pos["entry_ts"])).tzinfo else _ts3.replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 60
                except:
                    worst_hold_mins = 999

            if worst_sym and score_gap >= 1.5 and worst_hold_mins >= 15:
                log.info(f"[{st.label}] 🔄 ROTATE: sell {worst_sym} (score {worst_curr_score:.1f}, held {worst_hold_mins:.0f}m) → buy {s['symbol']} (score {sig_score:.1f}, gap +{score_gap:.1f})")
                ord_rot, rot_price = place_order(worst_sym, "sell", worst_pos["qty"], crypto=crypto, estimated_price=worst_price)
                if ord_rot:
                    pnl_rot = (rot_price - worst_pos["entry_price"]) * worst_pos["qty"]
                    del st.positions[worst_sym]
                    st.daily_pnl += pnl_rot
                    st.trades.insert(0, {"symbol": worst_sym, "side": "SELL", "qty": worst_pos["qty"],
                        "price": rot_price, "pnl": pnl_rot, "reason": "Rotation",
                        "time": datetime.now().strftime("%H:%M:%S")})
                    db_record_trade(worst_sym, "SELL", worst_pos["qty"], rot_price, pnl_rot,
                        worst_pos.get("signal_score"), None, None, None, "Rotation", "",
                        "crypto" if crypto else "stock",
                        discipline="crypto_swing" if crypto else "stock_swing",
                        adx_at_entry=worst_pos.get("_adx_entry"),
                        macd_bullish=worst_pos.get("_macd_bull"),
                        breakout=worst_pos.get("_breakout"),
                        rs_vs_spy=worst_pos.get("_rs_spy"),
                        news_state=worst_pos.get("_news_state"),
                        regime_at_entry=worst_pos.get("_regime"),
                        vix_at_entry=worst_pos.get("_vix"),
                        exit_category="ROTATE")
                    try:
                        db_record_rotation("SCORE_ROTATE",
                            sold_symbol=worst_sym, sold_price=rot_price,
                            sold_score=worst_curr_score, sold_pnl=pnl_rot,
                            bought_symbol=s["symbol"], bought_price=s["price"],
                            bought_score=sig_score, market="crypto" if crypto else "stock")
                    except Exception: pass
                    pos_count -= 1
                else:
                    break
            else:
                break
        if s["symbol"] in st.positions: continue
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[{st.label}] Global position cap ({MAX_TOTAL_POSITIONS}) reached")
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "MAX_TOTAL_POSITIONS")
            except Exception: pass
            break
        if s["symbol"] in all_symbols_held(): continue
        sym_sector = SECTOR_MAP.get(s["symbol"])
        if sym_sector and sectors_held().get(sym_sector, 0) >= MAX_SECTOR_POSITIONS:
            log.info(f"[{st.label}] SKIP {s['symbol']} — sector {sym_sector} full")
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "SECTOR_CAP")
            except Exception: pass
            continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET:
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "DAILY_TARGET_HIT")
            except Exception: pass
            break
        if total_exposure(st) >= MAX_PORTFOLIO_EXPOSURE:
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "MAX_EXPOSURE")
            except Exception: pass
            break

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
        if st.daily_spend + trade_val > MAX_DAILY_SPEND:
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "MAX_DAILY_SPEND")
            except Exception: pass
            continue

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
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "CHOPPY_MARKET")
            except Exception: pass
            break

        total_trades_today = sum(s2.trades_today for s2 in [state, crypto_state])
        if total_trades_today >= MAX_TRADES_PER_DAY:
            log.info(f"[{st.label}] Max trades per day ({MAX_TRADES_PER_DAY}) reached")
            try:
                db_record_near_miss(s["symbol"], s.get("score",0), MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - s.get("score",0), s["price"], crypto, "MAX_TRADES_DAY")
            except Exception: pass
            break

        stop_pct_use      = CRYPTO_STOP_PCT if crypto else STOP_LOSS_PCT
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)

        # ATR-based dynamic stop loss — adapts to each stock's volatility
        # Falls back to fixed % if ATR unavailable or USE_ATR_STOPS disabled
        _use_atr   = getattr(cfg, "USE_ATR_STOPS", True)
        _atr_mult  = float(getattr(cfg, "ATR_STOP_MULTIPLIER", 2.0))
        _atr_bars  = bars_cache.get(s["symbol"]) if not crypto else None
        _atr_val   = None
        if _use_atr and not crypto and _atr_bars and len(_atr_bars) >= 16:
            _atr_val = calc_atr(_atr_bars, period=14)

        if _atr_val and _atr_val > 0:
            stop_price = s["price"] - (_atr_val * _atr_mult)
            # Safety cap: ATR stop can't be worse than 2× the fixed % stop
            min_stop   = s["price"] * (1 - (stop_pct_use * 2) / 100)
            stop_price = max(stop_price, min_stop)
            log.info(f"[{st.label}] ATR stop for {s['symbol']}: ATR={_atr_val:.4f} × {_atr_mult} = ${s['price'] - stop_price:.4f} below entry (${stop_price:.4f})")
        else:
            stop_price = s["price"] * (1 - stop_pct_use / 100)

        breakdown = signal_breakdown(
            s["symbol"], s["price"], s.get("change", 0), s.get("rsi"),
            s.get("vol_ratio", 1), s.get("closes", [s["price"]]*22),
            sig_score, crypto=crypto
        )
        log.info(f"[{st.label}] ✅ BUY SIGNAL BREAKDOWN:\n{breakdown}")
        log.info(f"[{st.label}] Executing: BUY {s['symbol']} x{qty} @ ~${s['price']:.4f} | stop:${stop_price:.4f} | target:${take_profit_price:.4f}")

        # Capture structured entry context for analytics
        try:
            _closes = s.get("closes", [])
            _bars   = bars_cache.get(s["symbol"]) if not crypto else None
            _adx_entry    = calc_adx(_bars, period=14) if _bars and len(_bars) >= 16 else None
            _macd_v, _macd_s = calc_macd(_closes) if len(_closes) >= 35 else (None, None)
            _macd_bull    = bool(_macd_v and _macd_s and _macd_v > _macd_s)
            _breakout_val = is_breakout(_closes, lookback=20) if len(_closes) >= 21 else None
            from data.analytics import relative_strength_vs_spy
            _rs_spy       = relative_strength_vs_spy(_closes) if not crypto and len(_closes) >= 5 else None
            _news_st      = ("WATCH" if s["symbol"] in news_state.get("watch_list", {})
                             else "SKIP" if s["symbol"] in news_state.get("skip_list", {})
                             else "NONE")
            _regime_entry = market_regime.get("mode", "BULL") if not crypto else crypto_regime.get("mode", "BULL")
            _vix_entry    = market_regime.get("vix")
        except Exception:
            _adx_entry = _macd_bull = _breakout_val = _rs_spy = None
            _news_st = "NONE"; _regime_entry = None; _vix_entry = None

        mark_near_miss_triggered(s["symbol"])
        place_order._last_score = sig_score
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=crypto, estimated_price=s["price"])

        if order and fill_price:
            tg_trade_buy(s["symbol"], fill_price, sig_score, market="crypto" if crypto else "stock")

        if not order:
            log.warning(f"[{st.label}] ORDER FAILED for {s['symbol']}")
            record_near_miss(s["symbol"], sig_score, s["price"], crypto=crypto)
            try:
                db_record_near_miss(s["symbol"], sig_score, MIN_SIGNAL_SCORE,
                    MIN_SIGNAL_SCORE - sig_score, s["price"], crypto, "ORDER_FAILED")
            except Exception: pass
            continue

        if _atr_val and _atr_val > 0:
            actual_stop = fill_price - (_atr_val * _atr_mult)
            actual_stop = max(actual_stop, fill_price * (1 - (stop_pct_use * 2) / 100))
        else:
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
            # Entry context stored for sell-side structured recording
            "_adx_entry": _adx_entry, "_macd_bull": _macd_bull,
            "_breakout": _breakout_val, "_rs_spy": _rs_spy,
            "_news_state": _news_st, "_regime": _regime_entry, "_vix": _vix_entry,
            "_atr_entry": _atr_val,  # ATR at entry — for stop quality tracking
        }

        # Software stop-loss active — exchange stop orders not supported on this account
        if not crypto:
            log.info(f"[{st.label}] Software stop-loss active for {s['symbol']} @ ${actual_stop:.2f}")

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
        hold_hours = round((datetime.now(ZoneInfo("UTC")) - (datetime.fromisoformat(entry_ts) if datetime.fromisoformat(entry_ts).tzinfo else datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 3600, 1) if entry_ts else None

        if not crypto and s["symbol"] in exchange_stops:
            exchange_stops.pop(s["symbol"], None)

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
                            "crypto" if crypto else "stock",
                            discipline="crypto_swing" if crypto else "stock_swing",
                            adx_at_entry=pos.get("_adx_entry"),
                            macd_bullish=pos.get("_macd_bull"),
                            breakout=pos.get("_breakout"),
                            rs_vs_spy=pos.get("_rs_spy"),
                            news_state=pos.get("_news_state"),
                            regime_at_entry=pos.get("_regime"),
                            vix_at_entry=pos.get("_vix"),
                            exit_category="SIGNAL")
            if st.daily_pnl >= DAILY_PROFIT_TARGET:
                log.info(f"[{st.label}] Profit target hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break
            if st.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"[{st.label}] Loss limit hit! ${st.daily_pnl:.2f}")
                st.shutoff = True; break

    st.running = False


# ── Small cap cycle ───────────────────────────────────────────
def run_cycle_smallcap(watchlist, st, market="us"):
    """Smallcap cycle — works for US, FTSE, and ASX markets.
    market: 'us' | 'ftse' | 'asx'
    """
    st.check_reset()
    if st.shutoff: return
    if kill_switch["active"]: return
    if is_loss_streak_paused(): return

    # Market-appropriate open check
    if market == "us"   and not is_market_open(): return
    if market == "ftse" and not is_ftse_open(): return
    if market == "asx"  and not is_asx_open(): return
    if market_regime["mode"] == "BEAR":
        log.info(f"[SMALLCAP_{market.upper()}] BEAR MODE — pausing buys")
        return

    label = f"SMALLCAP_{market.upper()}"
    st.running    = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[{label}] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Pool: {len(watchlist)}")

    # ── Manage existing positions ──
    for sym, pos in list(st.positions.items()):
        live = fetch_latest_price(sym)
        if not live: continue
        now = datetime.now(ZoneInfo("UTC"))
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
            hold_hours = round((now - (datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")) if datetime.fromisoformat(entry_ts).tzinfo is None else datetime.fromisoformat(entry_ts))).total_seconds() / 3600, 1) if entry_ts else None
            log.info(f"[{label}] SELL {sym} @ ${live:.4f} | {reason} | P&L:${pnl:+.2f}")
            place_order(sym, "sell", pos["qty"])
            del st.positions[sym]
            st.daily_pnl += pnl
            st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                "price": live, "pnl": pnl, "reason": reason,
                "time": now.strftime("%H:%M:%S"), "hold_hours": hold_hours})
            db_record_trade(sym, "SELL", pos["qty"], live, pnl,
                pos.get("signal_score"), None, None, hold_hours, reason, "", "stock",
                discipline=f"smallcap_{market}",
                exit_category=("STOP" if "Stop" in reason else "TP" if "Take-Profit" in reason else "MAXHOLD"))
            if st.daily_pnl <= -MAX_DAILY_LOSS: st.shutoff = True; break
    if st.shutoff: st.running = False; return

    # ── Scan for new signals ──
    results = []
    for sym in watchlist:
        if sym in news_state.get("skip_list", {}): continue
        bars = fetch_bars(sym)
        if not bars or len(bars) < 15: continue
        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        price   = closes[-1]
        if not (SMALLCAP_MIN_PRICE <= price <= SMALLCAP_MAX_PRICE): continue
        prev      = closes[-2] if len(closes) > 1 else price
        change    = ((price - prev) / prev) * 100
        avg_vol   = sum(volumes[-10:]) / min(10, len(volumes))
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        signal, e9, e21, rsi = get_signal_smallcap(closes, volumes)
        sig_score = score_signal(sym, price, change, rsi, vol_ratio, closes, bars=bars)
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": e9, "sma21": e21, "rsi": rsi,
            "vol_ratio": vol_ratio, "score": sig_score, "smallcap": True})

    results.sort(key=lambda x: (-x.get("score", 0), {"BUY":0,"HOLD":1,"SELL":2}.get(x["signal"], 1)))
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY" and r.get("score", 0) >= MIN_SIGNAL_SCORE)
    log.info(f"[{label}] {buys} qualified BUY / {len(results)} scanned")

    # ── Open new positions (max 2 per market to leave room for other disciplines) ──
    SMALLCAP_MAX_POS = 2
    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if s.get("score", 0) < MIN_SIGNAL_SCORE: continue
        if pos_count >= SMALLCAP_MAX_POS: break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= MAX_PORTFOLIO_EXPOSURE: break
        qty       = max(1, int(SMALLCAP_MAX_TRADE / s["price"]))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price        = s["price"] * (1 - SMALLCAP_STOP_LOSS / 100)
        take_profit_price = s["price"] * (1 + TAKE_PROFIT_PCT / 100)
        log.info(f"[{label}] BUY {s['symbol']} @ ${s['price']:.4f} x{qty} = ${trade_val:.0f} score:{s['score']:.1f}")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            now_str = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": stop_price, "highest_price": fill_price,
                "take_profit_price": take_profit_price,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_str, "days_held": 0,
                "signal_score": s["score"]}
            st.daily_spend += trade_val
            st.trades.insert(0, {"symbol": s["symbol"], "side": "BUY", "qty": qty,
                "price": fill_price, "pnl": None, "reason": "Signal",
                "time": datetime.now().strftime("%H:%M:%S"), "entry_ts": now_str})
            db_record_trade(s["symbol"], "BUY", qty, fill_price, None,
                s["score"], None, None, None, "Signal", "", "stock",
                discipline=f"smallcap_{market}")
            pos_count += 1

    # ── Signal-based sells ──
    for s in results:
        if s["signal"] != "SELL" or s["symbol"] not in st.positions: continue
        pos = st.positions[s["symbol"]]
        pnl = (s["price"] - pos["entry_price"]) * pos["qty"]
        entry_ts = pos.get("entry_ts")
        hold_hours = round((datetime.now(ZoneInfo("UTC")) - (datetime.fromisoformat(entry_ts) if datetime.fromisoformat(entry_ts).tzinfo else datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 3600, 1) if entry_ts else None
        log.info(f"[{label}] SELL {s['symbol']} @ ${s['price']:.4f} P&L:${pnl:+.2f}")
        place_order(s["symbol"], "sell", pos["qty"])
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        db_record_trade(s["symbol"], "SELL", pos["qty"], s["price"], pnl,
            pos.get("signal_score"), None, None, hold_hours, "Signal", "", "stock",
            discipline=f"smallcap_{market}")
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
        sig_score = score_signal(sym, price, change, rsi_val, vol_ratio, closes, bars=bars) if signal == "BUY" else 0
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": ef, "sma21": es, "rsi": rsi_val,
            "vol_ratio": vol_ratio, "vwap": vwap_pos, "intraday": True,
            "score": sig_score})

    results.sort(key=lambda x: {("BUY"):0,"HOLD":1,"SELL":2}[x["signal"]])
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY" and r.get("score", 0) >= MIN_SIGNAL_SCORE)
    log.info(f"[INTRADAY] {buys} BUY / {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if s.get("score", 0) < MIN_SIGNAL_SCORE:
            log.info(f"[INTRADAY] SKIP {s['symbol']} score:{s.get('score',0):.1f} below threshold {MIN_SIGNAL_SCORE}")
            continue
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
        log.info(f"[INTRADAY] BUY {s['symbol']} @ ${s['price']:.2f} x{qty} score:{s.get('score','?')}/10")
        order, fill_price = place_order(s["symbol"], "buy", qty, estimated_price=s["price"])
        if order:
            actual_stop = fill_price * (1 - INTRADAY_STOP_LOSS / 100)
            actual_tp   = fill_price * (1 + INTRADAY_TAKE_PROFIT / 100)
            # Software stop-loss active — exchange stop orders not supported on this account
            log.info(f"[INTRADAY] Software stop-loss active for {s['symbol']} @ ${actual_stop:.2f}")
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": actual_stop, "highest_price": fill_price,
                "take_profit_price": actual_tp,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0,
                "signal_score": s.get("score", sig_score)}
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
        hold_hours = round((datetime.now(ZoneInfo("UTC")) - (datetime.fromisoformat(entry_ts) if datetime.fromisoformat(entry_ts).tzinfo else datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 3600, 2) if entry_ts else None
        log.info(f"[INTRADAY] SELL {s['symbol']} @ ${s['price']:.2f} P&L:${pnl:+.2f}")
        place_order(s["symbol"], "sell", pos["qty"])
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "[ID]Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        db_record_trade(s["symbol"], "SELL", pos["qty"], s["price"], pnl,
            pos.get("signal_score"), None, None, hold_hours, "[ID]Signal", "", "stock",
            discipline="stock_intraday", exit_category="SIGNAL")
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

    scan_list = watchlist
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
        sig_score = score_signal(sym, price, change, rsi_val, vol_ratio, closes, bars=bars) if signal == "BUY" else 0
        results.append({"symbol": sym, "price": price, "change": change,
            "signal": signal, "sma9": ef, "sma21": es, "rsi": rsi_val,
            "vol_ratio": vol_ratio, "vwap": vwap_pos, "intraday": True,
            "score": sig_score})

    results.sort(key=lambda x: (-x.get("score", 0), {"BUY":0,"HOLD":1,"SELL":2}[x["signal"]]))
    st.candidates = results
    buys = sum(1 for r in results if r["signal"] == "BUY" and r.get("score", 0) >= MIN_SIGNAL_SCORE)
    log.info(f"[CRYPTO_ID] {buys} BUY / {len(results)} scanned")

    pos_count = len(st.positions)
    for s in results:
        if s["signal"] != "BUY": continue
        if s.get("score", 0) < MIN_SIGNAL_SCORE:
            log.info(f"[CRYPTO_ID] SKIP {s['symbol']} score:{s.get('score',0):.1f} below threshold {MIN_SIGNAL_SCORE}")
            continue
        if pos_count >= CRYPTO_INTRADAY_MAX_POS: break
        if all_positions_count() >= MAX_TOTAL_POSITIONS:
            log.info(f"[CRYPTO_ID] Global position cap ({MAX_TOTAL_POSITIONS}) reached — skipping")
            break
        if s["symbol"] in st.positions: continue
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        if total_exposure(st) >= CRYPTO_MAX_EXPOSURE: break
        qty       = max(0.0001, round(CRYPTO_INTRADAY_MAX_TRADE / s["price"], 6))
        trade_val = qty * s["price"]
        if st.daily_spend + trade_val > MAX_DAILY_SPEND: continue
        stop_price = s["price"] * (1 - CRYPTO_INTRADAY_SL / 100)
        tp_price   = s["price"] * (1 + CRYPTO_INTRADAY_TP / 100)
        log.info(f"[CRYPTO_ID] BUY {s['symbol']} @ ${s['price']:.4f} score:{s.get('score','?')}/10")
        order, fill_price = place_order(s["symbol"], "buy", qty, crypto=True, estimated_price=s["price"])
        if order:
            actual_stop = fill_price * (1 - CRYPTO_INTRADAY_SL / 100)
            actual_tp   = fill_price * (1 + CRYPTO_INTRADAY_TP / 100)
            now_ts = datetime.now().isoformat()
            st.positions[s["symbol"]] = {"qty": qty, "entry_price": fill_price,
                "stop_price": actual_stop, "highest_price": fill_price,
                "take_profit_price": actual_tp,
                "entry_date": datetime.now().date().isoformat(),
                "entry_ts": now_ts, "days_held": 0,
                "signal_score": s.get("score", sig_score)}
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
        hold_hours = round((datetime.now(ZoneInfo("UTC")) - (datetime.fromisoformat(entry_ts) if datetime.fromisoformat(entry_ts).tzinfo else datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC")))).total_seconds() / 3600, 2) if entry_ts else None
        order_sell, sell_price = place_order(s["symbol"], "sell", pos["qty"], crypto=True, estimated_price=s["price"])
        pnl = (sell_price - pos["entry_price"]) * pos["qty"]
        log.info(f"[CRYPTO_ID] SELL {s['symbol']} @ ${sell_price:.4f} P&L:${pnl:+.2f}")
        del st.positions[s["symbol"]]
        st.daily_pnl += pnl
        st.trades.insert(0, {"symbol": s["symbol"], "side": "SELL", "qty": pos["qty"],
            "price": s["price"], "pnl": pnl, "reason": "[ID]Signal",
            "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
        db_record_trade(s["symbol"], "SELL", pos["qty"], sell_price, pnl,
            pos.get("signal_score"), None, None, hold_hours, "[ID]Signal", "", "crypto",
            discipline="crypto_intraday", exit_category="SIGNAL")
        if st.daily_pnl >= DAILY_PROFIT_TARGET: st.shutoff = True; break
        if st.daily_pnl <= -MAX_DAILY_LOSS:     st.shutoff = True; break
    st.running = False


# ── Main orchestration loop ───────────────────────────────────

# ── International market hours (UTC) ─────────────────────────
def is_asx_open():
    """ASX: Mon-Fri 00:00-06:00 UTC (10am-4pm AEST, adjusts for DST)."""
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return 0 <= now.hour < 6

def is_ftse_open():
    """LSE: Mon-Fri 08:00-16:30 UTC."""
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return (now.hour == 8 and now.minute >= 0) or (9 <= now.hour < 16) or (now.hour == 16 and now.minute < 30)

def update_asx_regime():
    """Use CBA as ASX market proxy (largest ASX stock by cap)."""
    try:
        bars = fetch_bars("CBA", crypto=False)
        if not bars or len(bars) < 20: return
        prices = [b["c"] for b in bars[-20:]]
        ma20 = sum(prices) / 20
        price = prices[-1]
        prev = asx_regime.get("mode", "BULL")
        if price > ma20:
            asx_regime["mode"] = "BULL"
        else:
            asx_regime["mode"] = "BEAR"
        asx_regime.update({"spy": price, "ma20": ma20, "updated": datetime.utcnow()})
        if asx_regime["mode"] != prev:
            log.info(f"[ASX REGIME] Changed → {asx_regime['mode']}")
        log.info(f"[ASX REGIME] {asx_regime['mode']} | CBA: ${price:.2f} MA20: ${ma20:.2f}")
    except Exception as e:
        log.warning(f"[ASX REGIME] update failed: {e}")

def update_ftse_regime():
    """Use HSBA as FTSE market proxy (largest LSE stock by cap)."""
    try:
        bars = fetch_bars("HSBA", crypto=False)
        if not bars or len(bars) < 20: return
        prices = [b["c"] for b in bars[-20:]]
        ma20 = sum(prices) / 20
        price = prices[-1]
        prev = ftse_regime.get("mode", "BULL")
        if price > ma20:
            ftse_regime["mode"] = "BULL"
        else:
            ftse_regime["mode"] = "BEAR"
        ftse_regime.update({"spy": price, "ma20": ma20, "updated": datetime.utcnow()})
        if ftse_regime["mode"] != prev:
            log.info(f"[FTSE REGIME] Changed → {ftse_regime['mode']}")
        log.info(f"[FTSE REGIME] {ftse_regime['mode']} | HSBA: ${price:.2f} MA20: ${ma20:.2f}")
    except Exception as e:
        log.warning(f"[FTSE REGIME] update failed: {e}")

def run_bear_cycle(st):
    """
    Bear discipline — inverse ETF intraday plays on confirmed BEAR days.
    - Only fires when market_regime = BEAR
    - Buys SQQQ, SPXU, SDOW, FAZ on signal — all 4 allowed during testing
    - Score threshold: 4.0 (lean in aggressively on bear days)
    - Force-sells ALL positions at 3:45pm ET (15 min before close) every day
    - Single-day only — never holds overnight
    """
    from core.config import BEAR_MIN_SCORE
    st.check_reset()
    if st.shutoff: return
    if kill_switch["active"]: return
    if not is_market_open(): return

    # Only run in BEAR regime
    if market_regime.get("mode") != "BEAR":
        return

    st.running = True
    st.last_cycle = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
    st.cycle_count += 1
    log.info(f"[BEAR] Cycle {st.cycle_count} | P&L: ${st.daily_pnl:+.2f} | Positions: {len(st.positions)}")

    # ── EOD force-sell: exit all positions at 3:45pm ET ──────────
    et_now = datetime.now(ZoneInfo("America/New_York"))
    if et_now.hour == 15 and et_now.minute >= 45:
        if st.positions:
            log.info("[BEAR] EOD force-sell — closing all bear positions before market close")
        for sym in list(st.positions.keys()):
            pos  = st.positions[sym]
            live = fetch_latest_price(sym, crypto=False)
            if not live:
                live = pos["entry_price"]
            qty  = pos["qty"]
            pnl  = (live - pos["entry_price"]) * qty
            order, fill = place_order(sym, "sell", qty, crypto=False, estimated_price=live)
            fill = fill or live
            pnl_actual = (fill - pos["entry_price"]) * qty
            log.info(f"[BEAR] EOD SELL {sym} x{qty} @ ${fill:.2f} | P&L: ${pnl_actual:+.2f}")
            st.daily_pnl += pnl_actual
            st.trades.insert(0, {
                "symbol": sym, "side": "SELL", "qty": qty,
                "price": fill, "pnl": pnl_actual, "reason": "Bear EOD",
                "time": datetime.now().strftime("%H:%M:%S"),
                "entry_ts": pos.get("entry_ts"), "score": pos.get("signal_score"),
                "rsi": None, "vol_ratio": None, "breakdown": "Bear EOD forced exit",
            })
            try:
                db_record_trade(sym, "SELL", qty, fill, pnl_actual,
                                pos.get("signal_score", 0), None, None,
                                (datetime.now() - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600
                                if pos.get("entry_ts") else 0,
                                "Bear EOD", "Bear EOD forced exit",
                                market="stock", discipline="bear_swing",
                                exit_category="BEAR_EOD")
            except Exception as e:
                log.debug(f"[BEAR] DB record failed: {e}")
            del st.positions[sym]
        return

    # ── Stop-loss check on existing positions ────────────────────
    check_stop_losses(st, crypto=False)
    if st.shutoff: return

    # ── Scan for new entries ─────────────────────────────────────
    # All 4 bear symbols allowed — max positions = 4 during testing
    bear_min_score = getattr(cfg, "BEAR_MIN_SCORE", 4.0)
    bars_cache = fetch_bars_batch(BEAR_WATCHLIST)

    for sym in BEAR_WATCHLIST:
        if sym in st.positions:
            continue  # already holding
        if len(st.positions) >= 4:
            break  # all 4 slots used

        bars = bars_cache.get(sym)
        if not bars or len(bars) < 22:
            continue

        closes  = [b["c"] for b in bars]
        volumes = [b.get("v", 0) for b in bars]
        price   = closes[-1]
        if price <= 0:
            continue

        signal, e_fast, e_slow, rsi = get_signal(closes, volumes)
        if signal != "BUY":
            continue

        vol_ratio = 1.0
        if len(volumes) >= 11:
            avg_vol = sum(volumes[-11:-1]) / 10
            vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        # Score using standard scorer — bear ETFs benefit from
        # breakout, volume, momentum signals same as normal stocks
        score = score_signal(sym, price, 0, rsi, vol_ratio, closes, bars=bars)

        log.info(f"[BEAR] {sym} score:{score} | price:${price:.2f} | rsi:{rsi:.1f if rsi else 'N/A'} | vol:{vol_ratio:.1f}x")

        if score < bear_min_score:
            log.info(f"[BEAR] SKIP {sym} score:{score} below bear threshold {bear_min_score}")
            continue

        # Size the trade
        qty = max(1, int(MAX_TRADE_VALUE / price))
        trade_val = qty * price

        if st.daily_spend + trade_val > MAX_DAILY_SPEND:
            log.info(f"[BEAR] SKIP {sym} — daily spend limit")
            continue

        stop_pct  = STOP_LOSS_PCT
        stop_price = price * (1 - stop_pct / 100)

        # ATR stop if available
        _use_atr  = getattr(cfg, "USE_ATR_STOPS", True)
        _atr_mult = float(getattr(cfg, "ATR_STOP_MULTIPLIER", 2.0))
        if _use_atr and bars and len(bars) >= 16:
            _atr_val = calc_atr(bars, period=14)
            if _atr_val and _atr_val > 0:
                atr_stop = price - (_atr_val * _atr_mult)
                min_stop = price * (1 - (stop_pct * 2) / 100)
                stop_price = max(atr_stop, min_stop)
                log.info(f"[BEAR] ATR stop for {sym}: ${stop_price:.2f}")

        take_profit = price * 1.05  # 5% TP on bear plays

        log.info(f"[BEAR] ✅ BUY {sym} x{qty} @ ~${price:.2f} | score:{score} | stop:${stop_price:.2f}")
        order, fill_price = place_order(sym, "buy", qty, crypto=False, estimated_price=price)

        if not order or not fill_price:
            log.warning(f"[BEAR] ORDER FAILED for {sym}")
            continue

        now_ts = datetime.now().isoformat()
        actual_stop = fill_price * (1 - stop_pct / 100)
        if _use_atr and bars and len(bars) >= 16:
            _atr_val2 = calc_atr(bars, period=14)
            if _atr_val2 and _atr_val2 > 0:
                actual_stop = max(fill_price - (_atr_val2 * _atr_mult),
                                  fill_price * (1 - (stop_pct * 2) / 100))

        st.positions[sym] = {
            "qty": qty, "entry_price": fill_price,
            "stop_price": actual_stop, "highest_price": fill_price,
            "take_profit_price": take_profit,
            "entry_date": datetime.now().date().isoformat(),
            "entry_ts": now_ts, "days_held": 0,
            "signal_score": score, "entry_breakdown": f"Bear play score:{score}",
            "_atr_entry": None,
        }
        st.daily_spend += trade_val
        st.trades_today += 1
        st.trades.insert(0, {
            "symbol": sym, "side": "BUY", "qty": qty,
            "price": fill_price, "pnl": None, "reason": "Bear Signal",
            "time": datetime.now().strftime("%H:%M:%S"),
            "entry_ts": now_ts, "score": score,
        })
        log.info(f"[BEAR] Position opened: {sym} x{qty} @ ${fill_price:.2f} | stop:${actual_stop:.2f}")

    st.running = False


def run_intl_cycle(watchlist, st, regime, market_open_fn, label):
    """Run a scan cycle for an international market (ASX or FTSE)."""
    if not market_open_fn(): return
    if kill_switch["active"]: return
    if st.shutoff: return
    if is_loss_streak_paused(): return
    if regime["mode"] == "BEAR": return
    discipline = "asx_swing" if label == "ASX" else "ftse_swing"
    results = []
    for sym in watchlist:
        try:
            bars = fetch_bars(sym, crypto=False)
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
            ema_cross = "✅" if e9 and e21 and e9 > e21 else "–"
            results.append({"symbol": sym, "price": price, "change": change, "signal": signal,
                            "score": sig_score, "rsi": rsi, "ema_cross": ema_cross,
                            "vol_ratio": vol_ratio, "sma9": e9, "sma21": e21, "closes": closes})
        except Exception as e:
            log.debug(f"[{label}] {sym} scan error: {e}")
    results.sort(key=lambda x: -x.get("score", 0))
    st.candidates = results
    buys = [r for r in results if r["signal"] == "BUY" and r["score"] >= MIN_SIGNAL_SCORE]
    log.info(f"[{label}] {len(buys)} qualified BUY / {len(results)} scanned")
    for s in buys[:10]:
        sym = s["symbol"]
        if sym in st.positions: continue
        if len(st.positions) >= MAX_POSITIONS: break
        if all_positions_count() >= MAX_TOTAL_POSITIONS: break
        if st.daily_pnl >= DAILY_PROFIT_TARGET: break
        qty = max(1, int(MAX_TRADE_VALUE / s["price"]))
        try:
            place_order._last_score = s["score"]
            order, fill = place_order(sym, "buy", qty, crypto=False, estimated_price=s["price"])
            if order:
                now_ts = datetime.now().isoformat()
                st.positions[sym] = {
                    "qty": qty, "entry_price": fill,
                    "stop_price": fill * (1 - STOP_LOSS_PCT / 100),
                    "highest_price": fill,
                    "take_profit_price": fill * (1 + TAKE_PROFIT_PCT / 100),
                    "entry_date": datetime.now().date().isoformat(),
                    "entry_ts": now_ts, "days_held": 0,
                    "signal_score": s["score"],
                    "entry_breakdown": "",
                }
                log.info(f"[{label}] BUY {sym} qty={qty} @ ${fill:.2f} score={s['score']}")
        except Exception as e:
            log.warning(f"[{label}] place_order {sym}: {e}")
    for sym in list(st.positions.keys()):
        pos = st.positions[sym]
        try:
            price = fetch_latest_price(sym, crypto=False)
            if not price: continue
            entry     = pos["entry_price"]
            entry_ts  = pos.get("entry_ts")
            now       = datetime.now(ZoneInfo("UTC"))
            hold_hours = round(
                (now - (datetime.fromisoformat(entry_ts) if datetime.fromisoformat(entry_ts).tzinfo
                        else datetime.fromisoformat(entry_ts).replace(tzinfo=ZoneInfo("UTC"))
                        )).total_seconds() / 3600, 2
            ) if entry_ts else None
            reason = None
            if price <= pos["stop_price"]:
                reason = f"Stop-Loss"; exit_cat = "STOP"
            elif price >= pos.get("take_profit_price", entry * 1.05):
                reason = f"Take-Profit"; exit_cat = "TP"
            if reason:
                order_sell, sell_price = place_order(sym, "sell", pos["qty"],
                                                     crypto=False, estimated_price=price)
                if order_sell:
                    pnl = (sell_price - entry) * pos["qty"]
                    del st.positions[sym]
                    st.daily_pnl += pnl
                    st.trades.insert(0, {"symbol": sym, "side": "SELL", "qty": pos["qty"],
                        "price": sell_price, "pnl": pnl, "reason": reason,
                        "time": datetime.now().strftime("%H:%M:%S"), "hold_hours": hold_hours})
                    try:
                        db_record_trade(sym, "SELL", pos["qty"], sell_price, pnl,
                            pos.get("signal_score"), None, None, hold_hours,
                            reason, "", label.lower(), discipline=discipline,
                            exit_category=exit_cat,
                            regime_at_entry=regime.get("mode"))
                    except Exception as db_e:
                        log.debug(f"[{label}] db_record_trade failed: {db_e}")
                    log.info(f"[{label}] SELL {sym} @ ${sell_price:.2f} P&L: ${pnl:+.2f} ({reason})")
        except Exception as e:
            log.warning(f"[{label}] exit check {sym}: {e}")

def main():
    global account_info
    cfg.account_info = {}

    log.info("=" * 50)
    log.info("AlphaBot starting up")
    log.info(f"Mode:   {'LIVE' if IS_LIVE else 'PAPER'} trading")
    log.info(f"Port:   {cfg.PORT}")
    log.info("=" * 50)

    log.info("AlphaBot bot process starting — dashboard runs separately on port 8080")

    # Start the IBKR connection manager (single shared connection on dedicated event loop)
    start_ibkr_manager()
    log.info("[STARTUP] IBKR connection manager initialised")

    # Load all watchlists from DB (US, FTSE, ASX, smallcaps, crypto, bear)
    # Any market without a DB entry keeps its config.py default
    load_all_watchlists_from_db()

    # Load smallcap watchlists from DB (legacy path — kept for backwards compat)
    update_smallcap_watchlists()

    # Verify IBKR connection
    cfg.account_info = ibkr_get_account() or {}
    if not cfg.account_info:
        log.error("Cannot connect to IBKR — check TWS/Gateway connection")
    else:
        log.info(f"Connected — Portfolio: ${float(cfg.account_info.get('portfolio_value',0)):,.2f}")

    # Binance startup
    if USE_BINANCE:
        mode = "TESTNET" if cfg.BINANCE_USE_TESTNET else ("LIVE" if IS_LIVE else "PAPER")
        log.info(f"[BINANCE] Using {mode}")

    last_email_day = None

    # Run IBKR position recovery before first scan cycle
    try:
        run_ibkr_startup_recovery()
    except NameError:
        pass

    # Rehydrate near-miss tracker from DB — survives restarts
    try:
        load_near_miss_tracker_from_db()
    except Exception as e:
        log.warning(f"[STARTUP] Near-miss rehydration failed (non-critical): {e}")

    # Rotation audit job — checks 24h follow-up on pending rotations
    def _rotation_audit_job():
        """Run every cycle: check pending rotations older than 24h and record verdict."""
        try:
            pending = db_get_pending_rotations(hours=24)
            if not pending:
                return
            for rot in pending:
                rot_id, rtype, sold_sym, bought_sym, sold_price, bought_price, market = rot
                try:
                    sold_now   = fetch_latest_price(sold_sym, crypto=(market=="crypto")) if sold_sym else None
                    bought_now = fetch_latest_price(bought_sym, crypto=(market=="crypto")) if bought_sym else None
                    db_update_rotation_followup(rot_id, sold_now, bought_now)
                    log.info(f"[ROTATION AUDIT] {rtype} | sold {sold_sym} now ${sold_now} | bought {bought_sym} now ${bought_now}")
                except Exception as e:
                    log.debug(f"[ROTATION AUDIT] Follow-up failed for rotation {rot_id}: {e}")
        except Exception as e:
            log.debug(f"[ROTATION AUDIT] Job failed (non-critical): {e}")

    # ── Thread-safe cycle trackers ──
    _thread_lock = threading.Lock()
    _cycle_counter = {"main": 0}

    def _run_thread(name, fn, interval_seconds):
        """Generic thread runner — calls fn() every interval_seconds."""
        log.info(f"[THREAD] {name} thread starting (interval={interval_seconds}s)")
        while True:
            try:
                fn()
            except Exception as e:
                log.error(f"[THREAD:{name}] Error: {e}")
            time.sleep(interval_seconds)

    # ── Thread 1: US Stocks swing cycle (60s) ──
    def _us_stocks_thread():
        run_cycle(US_WATCHLIST, state, crypto=False)

    # ── Thread 2: Crypto swing cycle (60s) ──
    def _crypto_swing_thread():
        if not (USE_BINANCE and time.time() < (cfg._binance_ban_until + 300)):
            run_cycle(CRYPTO_WATCHLIST, crypto_state, crypto=True)

    # ── Thread 3: FTSE cycle (60s) ──
    def _ftse_thread():
        update_ftse_regime()
        run_intl_cycle(FTSE_WATCHLIST, ftse_state, ftse_regime, is_ftse_open, "FTSE")

    # ── Thread 4: ASX cycle (60s) ──
    def _asx_thread():
        update_asx_regime()
        run_intl_cycle(ASX_WATCHLIST, asx_state, asx_regime, is_asx_open, "ASX")

    # ── Thread 5: Intraday US + Crypto (30s) ──
    def _intraday_thread():
        run_intraday_cycle(US_WATCHLIST, intraday_state)
        if not (USE_BINANCE and time.time() < (cfg._binance_ban_until + 300)):
            run_crypto_intraday_cycle(CRYPTO_WATCHLIST, crypto_intraday_state)

    # ── Thread 6: Smallcap US (120s) ──
    def _smallcap_us_thread():
        run_cycle_smallcap(smallcap_pool["us"], smallcap_state, market="us")

    # ── Thread 7: Smallcap FTSE (120s) ──
    def _smallcap_ftse_thread():
        run_cycle_smallcap(smallcap_pool["ftse"], smallcap_ftse_state, market="ftse")

    # ── Thread 8: Smallcap ASX (120s) ──
    def _smallcap_asx_thread():
        run_cycle_smallcap(smallcap_pool["asx"], smallcap_asx_state, market="asx")

    # ── Thread 9: Bear discipline (60s — inverse ETFs on BEAR days) ──
    def _bear_thread():
        run_bear_cycle(bear_state)

    # Start all trading threads as daemons
    trading_threads = [
        # Stock swing disciplines — 300s (5min) cycles
        # Uses daily bars, multi-day holds — no need for sub-minute scanning
        ("US-Swing",       _us_stocks_thread,     300),
        ("FTSE",           _ftse_thread,          300),
        ("ASX",            _asx_thread,           300),
        ("Smallcap-US",    _smallcap_us_thread,   300),
        ("Smallcap-FTSE",  _smallcap_ftse_thread, 300),
        ("Smallcap-ASX",   _smallcap_asx_thread,  300),
        ("Bear",           _bear_thread,          300),
        # Crypto swing — 60s (daily bars but 24/7 market, faster response to regime changes)
        ("Crypto-Swing",   _crypto_swing_thread,   60),
        # Intraday (US stocks + crypto intraday) — 30s for fast momentum captures
        ("Intraday",       _intraday_thread,       30),
    ]
    for name, fn, interval in trading_threads:
        t = threading.Thread(target=_run_thread, args=(name, fn, interval), daemon=True, name=name)
        t.start()
        log.info(f"[THREAD] Started: {name} ({interval}s interval)")
        time.sleep(2)  # stagger thread starts to avoid API hammering

    # ── Main orchestration loop (10s) ──
    # Handles: account refresh, DB status write, watchdog, panic kill, regime updates
    cycle = 0
    et = datetime.now(ZoneInfo("America/New_York"))

    while True:
        try:
            cycle += 1

            # Hot-reload trading params from config.json every cycle
            load_trading_config()

            log.info(f"\n{'─'*50}")
            log.info(f"Main cycle {cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            log.info(f"[WATCHDOG] Cycle {cycle} alive | Stocks P&L: ${state.daily_pnl:+.2f} | Crypto P&L: ${crypto_state.daily_pnl:+.2f} | Positions: {len(state.positions)}S/{len(crypto_state.positions)}C")

            # Every 10 cycles: reconcile positions with IBKR + run rotation audit
            if cycle % 10 == 0:
                try:
                    # Run rotation audit in daemon thread to avoid IBKR loop conflicts
                    threading.Thread(target=_rotation_audit_job, daemon=True).start()
                except Exception as e:
                    log.debug(f"[ROTATION AUDIT] Thread start failed: {e}")
                try:
                    ibkr_orders = ibkr_get_open_orders() or []
                    stop_syms = {o.get("symbol") for o in ibkr_orders if o.get("order_type") == "STP"}
                    broker_positions = ibkr_get_positions() or []
                    broker_syms = {p.get("symbol") for p in broker_positions}
                    local_syms  = set(state.positions.keys())
                    phantom = local_syms - broker_syms
                    for sym in phantom:
                        log.warning(f"[RECONCILE] {sym} in local state but NOT on IBKR — removing phantom")
                        del state.positions[sym]
                except Exception as e:
                    log.warning(f"[WATCHDOG] Reconciliation failed: {e}")

            # Refresh account info + live prices
            cfg.account_info = ibkr_get_account() or cfg.account_info
            update_live_prices()

            # Write positions snapshot for dashboard
            try:
                snap = {}
                for label, st in [("Stock", state), ("Crypto", crypto_state),
                                   ("SmCap", smallcap_state), ("ID", intraday_state),
                                   ("CrypID", crypto_intraday_state), ("ASX", asx_state),
                                   ("FTSE", ftse_state), ("Bear", bear_state)]:
                    for sym, pos in st.positions.items():
                        snap[sym] = {**pos, "_type": label, "_live": cfg.live_prices.get(sym)}
                db_write_positions(snap)
            except Exception as _e:
                log.warning(f"[SNAPSHOT] {_e}")

            # Write status snapshot for dashboard
            try:
                status_snap = {
                    "cycle": cycle,
                    "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
                    "account": cfg.account_info or {},
                    "market_regime": {
                        "mode": market_regime.get("mode", "BULL"),
                        "spy_price": market_regime.get("spy_price"),
                        "spy_ma20": market_regime.get("spy_ma20"),
                        "vix": market_regime.get("vix"),
                    },
                    "crypto_regime": {
                        "mode": crypto_regime.get("mode", "BULL"),
                        "btc_price": crypto_regime.get("btc_price"),
                        "btc_change": crypto_regime.get("btc_change"),
                        "btc_ma20": crypto_regime.get("btc_ma20"),
                    },
                    "asx_regime": {
                        "mode": asx_regime.get("mode", "BULL"),
                        "spy": asx_regime.get("spy"),
                    },
                    "ftse_regime": {
                        "mode": ftse_regime.get("mode", "BULL"),
                        "spy": ftse_regime.get("spy"),
                    },
                    "states": {
                        "us": {"cycle": state.cycle_count, "pnl": state.daily_pnl, "positions": len(state.positions), "running": state.running, "shutoff": state.shutoff},
                        "crypto": {"cycle": crypto_state.cycle_count, "pnl": crypto_state.daily_pnl, "positions": len(crypto_state.positions), "running": crypto_state.running, "shutoff": crypto_state.shutoff},
                        "asx": {"cycle": asx_state.cycle_count, "pnl": asx_state.daily_pnl, "positions": len(asx_state.positions), "running": asx_state.running, "shutoff": asx_state.shutoff},
                        "ftse": {"cycle": ftse_state.cycle_count, "pnl": ftse_state.daily_pnl, "positions": len(ftse_state.positions), "running": ftse_state.running, "shutoff": ftse_state.shutoff},
                        "smallcap": {"cycle": smallcap_state.cycle_count, "pnl": smallcap_state.daily_pnl, "positions": len(smallcap_state.positions), "running": smallcap_state.running, "shutoff": smallcap_state.shutoff},
                        "smallcap_ftse": {"cycle": smallcap_ftse_state.cycle_count, "pnl": smallcap_ftse_state.daily_pnl, "positions": len(smallcap_ftse_state.positions), "running": smallcap_ftse_state.running, "shutoff": smallcap_ftse_state.shutoff},
                        "smallcap_asx": {"cycle": smallcap_asx_state.cycle_count, "pnl": smallcap_asx_state.daily_pnl, "positions": len(smallcap_asx_state.positions), "running": smallcap_asx_state.running, "shutoff": smallcap_asx_state.shutoff},
                        "intraday": {"cycle": intraday_state.cycle_count, "pnl": intraday_state.daily_pnl, "positions": len(intraday_state.positions), "running": intraday_state.running, "shutoff": intraday_state.shutoff},
                        "crypto_id": {"cycle": crypto_intraday_state.cycle_count, "pnl": crypto_intraday_state.daily_pnl, "positions": len(crypto_intraday_state.positions), "running": crypto_intraday_state.running, "shutoff": crypto_intraday_state.shutoff},
                    },
                    "candidates": {
                        "us": state.candidates[:50],
                        "crypto_id": crypto_intraday_state.candidates[:50],
                        "crypto_swing": crypto_state.candidates[:50],
                        "asx": asx_state.candidates[:50],
                        "ftse": ftse_state.candidates[:50],
                        "smallcap": smallcap_state.candidates[:50],
                        "smallcap_ftse": smallcap_ftse_state.candidates[:50],
                        "smallcap_asx": smallcap_asx_state.candidates[:50],
                    },
                    "kill_switch": kill_switch,
                    "circuit_breaker": circuit_breaker,
                    "global_risk": {k: str(v) if hasattr(v, "strftime") else v for k, v in global_risk.items()},
                    "perf": {k: v for k, v in perf.items() if k != "sharpe_daily"},
                    "sizing": {
                        "total_pv": float(cfg.account_info.get("portfolio_value", 0)) + cfg._binance_balance_cache.get("value", 0.0) if cfg.account_info else cfg._binance_balance_cache.get("value", 0.0),
                        "ibkr_pv":  float(cfg.account_info.get("portfolio_value", 0)) if cfg.account_info else 0.0,
                        "binance_pv": cfg._binance_balance_cache.get("value", 0.0),
                    },
                }
                # DB is source of truth — replaces status.json entirely
                db_write_status(status_snap)
            except Exception as _se:
                log.warning(f"[STATUS] {_se}")

            # ── Dynamic limit scaling from live balances ──
            if cfg.account_info:
                ibkr_pv  = float(cfg.account_info.get("portfolio_value", 1000))
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

                total_pv    = ibkr_pv + binance_pv
                # Persist last known good portfolio value for dashboard fallback
                try:
                    db_write_portfolio({"total_pv": total_pv, "ibkr_pv": ibkr_pv, "binance_pv": binance_pv, "ts": time.time()})
                except: pass
                crypto_base = binance_pv if binance_pv > 0 else ibkr_pv * 0.20

                cfg.MAX_DAILY_LOSS         = total_pv * cfg.MAX_DAILY_LOSS_PCT / 100
                cfg.DAILY_PROFIT_TARGET    = total_pv * cfg.DAILY_PROFIT_TARGET_PCT / 100
                cfg.MAX_DAILY_SPEND        = ibkr_pv * cfg.MAX_DAILY_SPEND_PCT / 100
                cfg.MAX_PORTFOLIO_EXPOSURE = ibkr_pv * cfg.MAX_EXPOSURE_PCT / 100
                cfg.MAX_TRADE_VALUE        = ibkr_pv * cfg.MAX_TRADE_PCT / 100
                cfg.INTRADAY_MAX_TRADE     = ibkr_pv * 0.03
                cfg.SMALLCAP_MAX_TRADE     = ibkr_pv * 0.025
                cfg.CRYPTO_MAX_EXPOSURE    = crypto_base * cfg.MAX_EXPOSURE_PCT / 100
                cfg.CRYPTO_INTRADAY_MAX_TRADE = crypto_base * 0.02

                log.info(
                    f"[SIZING] IBKR:${ibkr_pv:,.2f} + Binance:${binance_pv:,.2f} = Total:${total_pv:,.2f} | "
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
                        for sym, pos in list(crypto_state.positions.items()):
                            place_order(sym, "sell", pos["qty"], crypto=True, estimated_price=pos["entry_price"])
                        state.positions.clear()
                        crypto_state.positions.clear()
                        for st in [state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state, bear_state]:
                            st.shutoff = True
                        circuit_breaker["active"] = True
                        circuit_breaker["reason"] = f"PANIC: Portfolio -{abs(drawdown_pct):.1f}% today"
                        tg_critical(f"🚨 PANIC KILL SWITCH: Portfolio down {drawdown_pct:.1f}%! All positions closed.")

            # Near-miss + regime updates
            update_near_miss_prices()
            _is_weekend = datetime.utcnow().weekday() >= 5
            if not _is_weekend and (not IS_LIVE or is_market_open()):
                update_market_regime()
                check_circuit_breaker()
            update_crypto_regime()  # crypto runs 24/7 including weekends

            # Weekly Binance watchlist refresh (Monday 9am ET)
            et_now = datetime.now(ZoneInfo("America/New_York"))
            if (USE_BINANCE and et_now.weekday() == 0 and et_now.hour == 9 and et_now.minute < 2):
                log.info("[BINANCE] Refreshing top coins list...")
                fresh = binance_get_top_coins(100)
                if fresh:
                    CRYPTO_WATCHLIST[:] = fresh
                    log.info(f"[BINANCE] Watchlist updated: {len(CRYPTO_WATCHLIST)} coins")

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

            # Weekly near-miss report — Saturday 6am Paris / midnight ET
            if et.weekday() == 5 and et.hour == 0 and et.minute < 2:
                log.info("[WEEKLY] Generating near-miss analysis report...")
                threading.Thread(target=send_weekly_near_miss_email, daemon=True).start()

            # Weekly intelligence analysis — Saturday 7am Paris / 1am ET
            # Runs Saturday morning so Garrath has the full weekend to review + tune before Monday open
            if et.weekday() == 5 and et.hour == 1 and et.minute < 2:
                log.info("[INTELLIGENCE] Starting weekly intelligence run (Saturday 7am Paris)...")
                def _run_intel():
                    try:
                        from data.intelligence import run_intelligence_analysis
                        run_id, cnt, narrative = run_intelligence_analysis(triggered_by="scheduled")
                        log.info(f"[INTELLIGENCE] Weekly run complete — {cnt} recs stored (run_id={run_id})")
                    except Exception as e:
                        log.error(f"[INTELLIGENCE] Weekly run failed: {e}")
                threading.Thread(target=_run_intel, daemon=True).start()

            # Daily near-miss simulations — noon ET
            if et.hour == 12 and et.minute < 2:
                threading.Thread(target=run_near_miss_simulations, daemon=True).start()

            time.sleep(CYCLE_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped")
            break
        except Exception as e:
            log.error(f"[CRASH] Error in main loop: {e}")
            log.error(f"[CRASH] Bot recovering — sleeping 30s then resuming")
            try:
                open_orders = ibkr_get_open_orders() or []
                stop_syms = {o["symbol"] for o in open_orders if o.get("type") == "stop"}
                for sym, pos in state.positions.items():
                    if sym not in stop_syms:
                        log.warning(f"[CRASH RECOVERY] Missing stop for {sym} — software stop-loss active")
            except: pass
            time.sleep(30)
def run_ibkr_startup_recovery():
    """Recover open positions from IBKR on startup."""
    try:
        ibkr_positions = ibkr_get_positions() or []
        open_orders = ibkr_get_open_orders() or []
        stop_map = {o["symbol"]: o for o in open_orders if o.get("order_type") == "STP"}
        recovered = 0
        for p in ibkr_positions:
            sym = p.get("symbol")
            qty = float(p.get("qty", 0) or 0)
            avg = float(p.get("avg_entry_price", 0) or p.get("avg_cost", 0) or 0)
            if not sym or qty <= 0:
                continue
            stop = stop_map.get(sym, {}).get("stop_price", avg * (1 - STOP_LOSS_PCT / 100))
            state.positions[sym] = {
                "qty": qty,
                "entry_price": avg,
                "stop_price": stop,
                "take_profit_price": avg * (1 + TAKE_PROFIT_PCT / 100),
                "entry_ts": datetime.now(ZoneInfo("UTC")).isoformat(),
                "entry_date": datetime.now(ZoneInfo("Europe/Paris")).strftime("%d %b %H:%M"),
                "signal_score": "—",
                "entry_breakdown": "",
            }
            log.info(f"[RECOVERY] Restored position: {sym} x{qty} @ ${avg:.2f}")
            if sym not in stop_map:
                log.warning(f"[RECOVERY] {sym} has no stop on IBKR — software stop-loss active @ ${stop:.2f}")
            recovered += 1
        log.info(f"=== Recovered {recovered} open position(s) ===\n")
    except Exception as e:
        log.error(f"[RECOVERY] Failed: {e}")


if __name__ == "__main__":
    main()
