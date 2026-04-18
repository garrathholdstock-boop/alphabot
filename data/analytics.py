"""
data/analytics.py — AlphaBot Signal Scoring & Analytics
─────────────────────────────────────────────────────────
THIS IS YOUR TUNING FILE.
All performance analysis, signal scoring, near-miss tracking,
and edge analysis lives here. Edit this to improve the bot's edge.

Key tuning parameters:
  MIN_SIGNAL_SCORE  — in core/config.py (start at 5, raise to 7+ for live)
  score_signal()    — the scoring function below
  simulate_near_miss_exit() — simulates what near-misses would have returned
"""

import requests
import logging
import time
from datetime import datetime, timedelta

from core.config import (
    log, USE_BINANCE, MIN_SIGNAL_SCORE, CLAUDE_API_KEY,
    STOP_LOSS_PCT, TRAILING_STOP_PCT, TRAIL_TRIGGER_PCT, TAKE_PROFIT_PCT,
    near_miss_tracker, news_state, market_regime, crypto_regime,
    perf,
)
import core.config as cfg


# ── Technical indicators ──────────────────────────────────────
def ema(prices, period):
    """Exponential Moving Average — weights recent prices more than SMA."""
    if len(prices) < period: return None
    k = 2 / (period + 1)
    result = sum(prices[:period]) / period
    for price in prices[period:]:
        result = price * k + result * (1 - k)
    return result

def sma(prices, period):
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
    """MACD = EMA12 - EMA26. Signal = EMA9 of MACD."""
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

def calc_adx(bars, period=14):
    """ADX > 25 = strong trend. ADX < 20 = choppy — avoid EMA crossovers."""
    if not bars or len(bars) < period + 2: return None
    try:
        highs  = [b["h"] for b in bars]
        lows   = [b["l"] for b in bars]
        closes = [b["c"] for b in bars]
        tr_list, plus_dm, minus_dm = [], [], []
        for i in range(1, len(bars)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr  = max(h - l, abs(h - pc), abs(l - pc))
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
        if len(dx_list) < period: return None
        return round(sum(dx_list[-period:]) / period, 1)
    except:
        return None

def calc_vwap(bars):
    if not bars or len(bars) < 3: return None
    total_vol = sum(b["v"] for b in bars)
    if total_vol == 0: return None
    return sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars) / total_vol

def vwap_signal(bars):
    vwap = calc_vwap(bars)
    if not vwap or not bars: return None
    price = bars[-1]["c"]
    pct   = ((price - vwap) / vwap) * 100
    if pct > 0.3:  return "ABOVE"
    if pct < -0.3: return "BELOW"
    return "AT"

def is_breakout(closes, lookback=20):
    if len(closes) < lookback + 1: return False
    return closes[-1] > max(closes[-(lookback+1):-1])

# ── SPY relative strength ─────────────────────────────────────
_spy_closes_cache = {"closes": [], "last_fetch": None}

def get_spy_closes():
    now  = datetime.now()
    last = _spy_closes_cache["last_fetch"]
    if last and (now - last).seconds < 300:
        return _spy_closes_cache["closes"]
    from core.execution import fetch_bars
    bars = fetch_bars("SPY", crypto=False)
    if bars:
        closes = [b["c"] for b in bars]
        _spy_closes_cache["closes"] = closes
        _spy_closes_cache["last_fetch"] = now
        return closes
    return _spy_closes_cache["closes"]

def relative_strength_vs_spy(stock_closes):
    spy_closes = get_spy_closes()
    if not spy_closes or len(spy_closes) < 5 or len(stock_closes) < 5:
        return 0.0
    periods   = min(len(spy_closes), len(stock_closes), 10)
    stock_ret = (stock_closes[-1] - stock_closes[-periods]) / stock_closes[-periods] * 100
    spy_ret   = (spy_closes[-1]   - spy_closes[-periods])   / spy_closes[-periods]   * 100
    return round(stock_ret - spy_ret, 2)


# ── Core signal generator ─────────────────────────────────────
def get_signal(closes, volumes=None):
    """EMA 9/21 crossover + RSI + Volume + MACD."""
    from core.config import VOLUME_MIN_RATIO
    e9  = ema(closes, 9);  e21 = ema(closes, 21)
    pe9 = ema(closes[:-1], 9); pe21 = ema(closes[:-1], 21)
    rsi = calc_rsi(closes)
    if None in (e9, e21, pe9, pe21, rsi): return "HOLD", e9, e21, rsi

    cross_up   = pe9 <= pe21 and e9 > e21
    cross_down = pe9 >= pe21 and e9 < e21
    macd, macd_sig = calc_macd(closes)
    macd_bullish = macd is not None and macd_sig is not None and macd > macd_sig
    macd_bearish = macd is not None and macd_sig is not None and macd < macd_sig
    vol_confirmed = True
    if volumes and len(volumes) >= 11:
        avg_vol = sum(volumes[-11:-1]) / 10
        vol_confirmed = volumes[-1] >= avg_vol * VOLUME_MIN_RATIO

    if cross_up and rsi < 75 and vol_confirmed and (macd_bullish or macd is None):
        return "BUY", e9, e21, rsi
    if cross_down or rsi > 75 or (cross_down and macd_bearish):
        return "SELL", e9, e21, rsi
    return "HOLD", e9, e21, rsi

def get_signal_smallcap(closes, volumes=None):
    from core.config import SMALLCAP_VOL_RATIO
    e9  = ema(closes, 9);  e21 = ema(closes, 21)
    pe9 = ema(closes[:-1], 9); pe21 = ema(closes[:-1], 21)
    rsi = calc_rsi(closes)
    if None in (e9, e21, pe9, pe21, rsi): return "HOLD", e9, e21, rsi
    cross_up   = pe9 <= pe21 and e9 > e21
    cross_down = pe9 >= pe21 and e9 < e21
    macd, macd_sig = calc_macd(closes)
    macd_bullish = macd is not None and macd_sig is not None and macd > macd_sig
    macd_bearish = macd is not None and macd_sig is not None and macd < macd_sig
    vol_confirmed = True
    if volumes and len(volumes) >= 11:
        avg_vol = sum(volumes[-11:-1]) / 10
        vol_confirmed = volumes[-1] >= avg_vol * SMALLCAP_VOL_RATIO
    if cross_up and rsi < 75 and vol_confirmed and (macd_bullish or macd is None):
        return "BUY", e9, e21, rsi
    if cross_down or rsi > 75 or (cross_down and macd_bearish):
        return "SELL", e9, e21, rsi
    return "HOLD", e9, e21, rsi

def get_intraday_signal(closes, volumes, ema_fast, ema_slow, rsi_limit, vol_ratio_min):
    ef  = ema(closes, ema_fast);  es  = ema(closes, ema_slow)
    pef = ema(closes[:-1], ema_fast); pes = ema(closes[:-1], ema_slow)
    rsi_val = calc_rsi(closes)
    if None in (ef, es, pef, pes, rsi_val): return "HOLD", ef, es, rsi_val
    cross_up   = pef <= pes and ef > es
    cross_down = pef >= pes and ef < es
    vol_ok = True
    if volumes and len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        vol_ok  = volumes[-1] >= avg_vol * vol_ratio_min
    if cross_up and rsi_val < rsi_limit and vol_ok:
        return "BUY", ef, es, rsi_val
    if cross_down or rsi_val > rsi_limit:
        return "SELL", ef, es, rsi_val
    return "HOLD", ef, es, rsi_val


# ── ─────────────────────────────────────────────────────────────
# UNIFIED SIGNAL SCORER — edit this to tune bot performance
# ─────────────────────────────────────────────────────────────
def score_signal(sym, price, change, rsi, vol_ratio, closes, bars=None):
    """
    Score a BUY candidate 0–11. Trade if score >= MIN_SIGNAL_SCORE.

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
    from core.risk import is_choppy_market
    score = 0.0

    # Breakout or strong momentum
    if is_breakout(closes, lookback=20):
        score += 2.0
    elif len(closes) >= 6 and closes[-6] > 0:
        m5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if m5d >= 3.0:   score += 1.0
        elif m5d >= 1.5: score += 0.5

    # Relative strength vs SPY — only trade market leaders
    if relative_strength_vs_spy(closes) > 0:
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

    # ADX trend strength — kills false signals in choppy markets
    if bars and len(bars) >= 16:
        adx = calc_adx(bars, period=14)
        if adx is not None:
            if adx >= 25:   score += 1.5
            elif adx >= 20: score += 0.5
            else:           score -= 1.5

    # News catalyst
    if sym in news_state.get("watch_list", {}): score += 1.5
    if sym in news_state.get("skip_list",   {}): score -= 5.0

    # Environment
    if is_choppy_market(): score -= 1.0

    return round(min(11.0, max(0.0, score)), 1)


# ── Signal breakdown (human-readable) ─────────────────────────
def signal_breakdown(sym, price, change, rsi, vol_ratio, closes, score, crypto=False):
    from core.risk import is_choppy_market
    lines = []
    label = "CRYPTO" if crypto else "STOCK"
    lines.append(f"{'─'*52}")
    lines.append(f"  {label}: {sym}  |  Score: {score}/10  |  Price: ${price:.4f}")
    lines.append(f"{'─'*52}")
    if len(closes) >= 22:
        s9  = sum(closes[-9:]) / 9
        s21 = sum(closes[-21:]) / 21
        p9  = sum(closes[-10:-1]) / 9
        p21 = sum(closes[-22:-1]) / 21
        crossed = p9 <= p21 and s9 > s21
        lines.append(f"  SMA Cross:    {'✅ YES — 9-day crossed above 21-day' if crossed else '❌ No crossover yet'}")
        lines.append(f"  SMA 9:        ${s9:.4f}  |  SMA 21: ${s21:.4f}")
    if rsi:
        if 50 <= rsi <= 65:   rsi_note = "✅ Sweet spot (50-65) +1.0pt"
        elif 40 <= rsi < 50:  rsi_note = "⚠ Building momentum (40-50) +0.5pt"
        elif rsi > 75:        rsi_note = "🔴 Overbought (>75) -1.0pt"
        elif rsi > 70:        rsi_note = "⚠ Getting hot (70-75)"
        else:                 rsi_note = "— Neutral zone"
        lines.append(f"  RSI:          {rsi:.1f}  {rsi_note}")
    if vol_ratio:
        if vol_ratio >= 2.0:   vol_note = "✅ Strong conviction (2x+) +2.0pt"
        elif vol_ratio >= 1.5: vol_note = "✅ Good conviction (1.5x+) +1.0pt"
        elif vol_ratio >= 1.2: vol_note = "⚠ Mild confirmation (1.2x+) +0.5pt"
        else:                  vol_note = "❌ Below average — weak signal"
        lines.append(f"  Volume:       {vol_ratio:.2f}x avg  {vol_note}")
    if len(closes) >= 20:
        breakout = is_breakout(closes, lookback=20)
        lines.append(f"  Breakout:     {'✅ YES — 20-bar high +2.0pt' if breakout else '❌ No breakout'}")
    if len(closes) >= 6 and closes[-6] > 0:
        m5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if m5d >= 3.0:        mom_note = f"✅ Strong +{m5d:.1f}% +1.0pt"
        elif m5d >= 1.5:      mom_note = f"⚠ Moderate +{m5d:.1f}% +0.5pt"
        else:                 mom_note = f"— Weak {m5d:+.1f}%"
        lines.append(f"  5d Momentum:  {mom_note}")
    if not crypto and len(closes) >= 6:
        rs = relative_strength_vs_spy(closes)
        lines.append(f"  vs SPY:       {'✅ Outperforming +1.5pt' if rs > 0 else '❌ Underperforming SPY'}")
    if len(closes) >= 35:
        mv, ms = calc_macd(closes)
        if mv is not None and ms is not None:
            lines.append(f"  MACD:         {'✅ Bullish (MACD > Signal) +1.0pt' if mv > ms else '❌ Bearish (MACD < Signal)'}")
    if sym in news_state.get("watch_list", {}):
        lines.append(f"  News:         ✅ Positive catalyst +1.5pt")
    elif sym in news_state.get("skip_list", {}):
        lines.append(f"  News:         🔴 NEGATIVE — skip flag -5.0pt")
    else:
        lines.append(f"  News:         — No news flag")
    choppy = is_choppy_market()
    regime = crypto_regime["mode"] if crypto else market_regime["mode"]
    lines.append(f"  Market:       {'⚠ Choppy -1.0pt' if choppy else '✅ Trending'}")
    lines.append(f"  Regime:       {'🔴 BEAR' if regime == 'BEAR' else '✅ BULL'}")
    lines.append(f"{'─'*52}")
    return "\n".join(lines)

def sell_breakdown(sym, pos, exit_price, pnl, reason, hold_hours, crypto=False):
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


# ── Edge analysis ─────────────────────────────────────────────
def analyse_edge():
    """Score vs win rate — find your real edge."""
    trades = [t for t in perf["all_trades"] if t.get("score") is not None and t.get("pnl") is not None]
    if len(trades) < 5:
        return "Not enough trades yet (need 5+)"
    buckets = {}
    for t in trades:
        bucket = f"{int(t['score'])}-{int(t['score'])+1}"
        if bucket not in buckets:
            buckets[bucket] = {"wins": 0, "losses": 0, "total_pnl": 0}
        if t["pnl"] > 0: buckets[bucket]["wins"] += 1
        else:             buckets[bucket]["losses"] += 1
        buckets[bucket]["total_pnl"] += t["pnl"]
    lines = ["SIGNAL SCORE vs OUTCOME ANALYSIS", "=" * 40]
    for bucket in sorted(buckets.keys()):
        b = buckets[bucket]
        total    = b["wins"] + b["losses"]
        win_rate = int(b["wins"] / total * 100) if total > 0 else 0
        lines.append(
            f"  Score {bucket}: {total} trades | "
            f"Win rate: {win_rate}% | "
            f"P&L: ${b['total_pnl']:+.2f} | "
            f"{'✅ EDGE' if win_rate >= 55 else '❌ NO EDGE'}"
        )
    best  = max(buckets.items(), key=lambda x: x[1]["total_pnl"])
    worst = min(buckets.items(), key=lambda x: x[1]["total_pnl"])
    lines.append(f"  Best score bucket:  {best[0]} (${best[1]['total_pnl']:+.2f})")
    lines.append(f"  Worst score bucket: {worst[0]} (${worst[1]['total_pnl']:+.2f})")
    rec = "Raise MIN_SIGNAL_SCORE to " + best[0].split("-")[0] if best[0] != sorted(buckets.keys())[0] else "Keep current threshold"
    lines.append(f"  Recommendation: {rec}")
    lines.append("=" * 40)
    return "\n".join(lines)


# ── Near-miss tracking ────────────────────────────────────────
def load_near_miss_tracker_from_db():
    """
    Called once on bot startup to rehydrate the in-memory tracker from DB.
    Means near-miss follow-up data survives bot restarts / pkill.
    """
    try:
        from data.database import db_load_near_miss_tracker
        restored = db_load_near_miss_tracker(days_back=7)
        if restored:
            near_miss_tracker.update(restored)
            log.info(f"[NEAR MISS] Rehydrated {len(restored)} near-misses from DB")
    except Exception as e:
        log.warning(f"[NEAR MISS] Rehydration failed (non-critical): {e}")

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
    """
    Update prices_since for all tracked near-misses.
    Now also persists to DB so data survives restarts.
    Computes pct_move, MFE (max favourable excursion), MAE (max adverse excursion).
    """
    from core.execution import fetch_latest_price
    try:
        from data.database import db_update_near_miss_prices
        _db_writer = db_update_near_miss_prices
    except Exception:
        _db_writer = None

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
                # Compute running stats
                pam = nm["price_at_miss"]
                all_prices = [pam] + nm["prices_since"]
                pct_move = round((all_prices[-1] - pam) / pam * 100, 2) if pam else None
                mfe_pct  = round((max(all_prices) - pam) / pam * 100, 2) if pam else None
                mae_pct  = round((min(all_prices) - pam) / pam * 100, 2) if pam else None
                nm["pct_move"] = pct_move
                nm["mfe_pct"]  = mfe_pct
                nm["mae_pct"]  = mae_pct
                # Persist to DB
                if _db_writer:
                    try:
                        _db_writer(nm["symbol"], nm["date"], nm["prices_since"],
                                   pct_move=pct_move, mfe_pct=mfe_pct, mae_pct=mae_pct)
                    except Exception as e:
                        log.debug(f"[NEAR MISS] DB price persist failed: {e}")
        except Exception:
            pass

def mark_near_miss_triggered(symbol):
    """Mark a near-miss as triggered. Now also persists to DB."""
    from core.execution import fetch_latest_price
    try:
        from data.database import db_mark_near_miss_triggered
        _db_trigger = db_mark_near_miss_triggered
    except Exception:
        _db_trigger = None

    for key, nm in near_miss_tracker.items():
        if nm["symbol"] == symbol and not nm["triggered"]:
            trigger_price = fetch_latest_price(symbol, crypto=nm["crypto"])
            nm["triggered"]     = True
            nm["trigger_date"]  = datetime.now().date().isoformat()
            nm["trigger_price"] = trigger_price
            log.info(f"[NEAR MISS] {symbol} finally triggered!")
            if _db_trigger:
                try:
                    _db_trigger(symbol, nm["date"], trigger_price)
                except Exception as e:
                    log.debug(f"[NEAR MISS] DB trigger persist failed: {e}")

def build_sparkline_html(price_at_miss, prices_since):
    if not prices_since:
        return '<span style="color:#444;font-size:11px">Tracking...</span>'
    all_prices = [price_at_miss] + prices_since
    min_p = min(all_prices); max_p = max(all_prices)
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


# ── Near-miss exit simulation ─────────────────────────────────
def simulate_near_miss_exit(entry_price, daily_bars):
    """
    Simulate what would have happened if we took a near-miss trade.
    Applies real stop/trail/take-profit rules day-by-day.
    This is how we know if the threshold is too tight.
    """
    if not daily_bars or not entry_price: return None

    stop_pct        = STOP_LOSS_PCT / 100
    trail_pct       = TRAILING_STOP_PCT / 100
    trail_trigger   = TRAIL_TRIGGER_PCT / 100
    take_profit_pct = TAKE_PROFIT_PCT / 100

    stop_price    = entry_price * (1 - stop_pct)
    take_profit   = entry_price * (1 + take_profit_pct)
    trail_high    = entry_price
    trail_active  = False
    trail_stop    = None
    exit_price = exit_day = exit_reason = None

    for day_idx, bar in enumerate(daily_bars[:5]):
        day_low   = bar.get("l", bar.get("c"))
        day_high  = bar.get("h", bar.get("c"))
        day_close = bar.get("c")
        if day_high > trail_high: trail_high = day_high
        profit_pct = (trail_high - entry_price) / entry_price
        if profit_pct >= trail_trigger:
            trail_active = True
            trail_stop   = trail_high * (1 - trail_pct)
        if day_low <= stop_price:
            exit_price  = stop_price
            exit_day    = day_idx + 1
            exit_reason = f"Stop loss hit day {day_idx+1}"
            break
        if trail_active and trail_stop and day_low <= trail_stop:
            exit_price  = trail_stop
            exit_day    = day_idx + 1
            exit_reason = f"Trailing stop hit day {day_idx+1} (locked in after +{profit_pct*100:.1f}%)"
            break
        if day_high >= take_profit:
            exit_price  = take_profit
            exit_day    = day_idx + 1
            exit_reason = f"Take profit hit day {day_idx+1} 🎯"
            break
        if trail_active:
            trail_stop = max(trail_stop, day_close * (1 - trail_pct))

    if exit_price is None and daily_bars:
        last_bar    = daily_bars[min(4, len(daily_bars)-1)]
        exit_price  = last_bar.get("c", entry_price)
        exit_day    = min(5, len(daily_bars))
        exit_reason = f"Max hold reached — exited at day {exit_day} close"

    if exit_price is None: return None

    pnl_pct   = ((exit_price - entry_price) / entry_price) * 100
    trade_val = 400
    pnl_usd   = (pnl_pct / 100) * trade_val
    mfe_pct   = round(((trail_high - entry_price) / entry_price) * 100, 2)
    # MAE: worst intraday low seen across all bars before exit
    mae_price = min((b.get("l", b.get("c")) for b in daily_bars[:exit_day or 5]), default=entry_price)
    mae_pct   = round(((mae_price - entry_price) / entry_price) * 100, 2)

    return {
        "entry_price":    entry_price,
        "exit_price":     round(exit_price, 6),
        "exit_day":       exit_day,
        "exit_reason":    exit_reason,
        "pnl_pct":        round(pnl_pct, 2),
        "pnl_usd":        round(pnl_usd, 2),
        "profitable":     pnl_pct > 0,
        "trail_active":   trail_active,
        "max_profit_pct": mfe_pct,
        "mfe_pct":        mfe_pct,
        "mae_pct":        mae_pct,
    }

def fetch_near_miss_ohlc(symbol, from_date, days=5, crypto=False):
    from core.execution import fetch_bars, binance_get
    try:
        from_dt = datetime.fromisoformat(from_date)
        end_dt  = from_dt + timedelta(days=days + 3)
        if crypto and USE_BINANCE:
            if time.time() < (cfg._binance_ban_until + 300):
                return []
            start_ts = int(from_dt.timestamp() * 1000)
            end_ts   = int(end_dt.timestamp() * 1000)
            data = binance_get("/api/v3/klines", {
                "symbol": symbol, "interval": "1d",
                "startTime": start_ts, "endTime": end_ts, "limit": days + 3
            })
            if not data: return []
            return [{"o": float(k[1]), "h": float(k[2]),
                     "l": float(k[3]), "c": float(k[4])} for k in data]
        else:
            # Use IBKR fetch_bars for US stock near-miss OHLC simulation
            from core.execution import fetch_bars
            bars = fetch_bars(symbol, crypto=False)
            if bars and len(bars) >= 2:
                return [{"o": b.get("o", b["c"]), "h": b.get("h", b["c"]),
                         "l": b.get("l", b["c"]), "c": b["c"]} for b in bars[-days:]]
        return []
    except Exception as e:
        log.debug(f"[NEAR MISS SIM] Failed to fetch OHLC for {symbol}: {e}")
        return []

def run_near_miss_simulations():
    """
    Run exit simulations on near-misses that have enough price data.
    Now persists results to DB so they survive restarts.
    """
    try:
        from data.database import db_update_near_miss_simulation
        _db_sim = db_update_near_miss_simulation
    except Exception:
        _db_sim = None

    updated = 0
    for key, nm in near_miss_tracker.items():
        if len(nm.get("prices_since", [])) < 3: continue
        if nm.get("simulation"): continue
        try:
            bars = fetch_near_miss_ohlc(nm["symbol"], nm["date"], days=5, crypto=nm.get("crypto", False))
            if bars and len(bars) >= 2:
                sim = simulate_near_miss_exit(nm["price_at_miss"], bars)
                if sim:
                    nm["simulation"] = sim
                    updated += 1
                    # Persist to DB
                    if _db_sim:
                        try:
                            _db_sim(nm["symbol"], nm["date"], sim)
                        except Exception as e:
                            log.debug(f"[NEAR MISS SIM] DB persist failed for {nm['symbol']}: {e}")
        except Exception as e:
            log.debug(f"[NEAR MISS SIM] {nm['symbol']}: {e}")
    if updated:
        log.info(f"[NEAR MISS SIM] Ran simulations on {updated} near-misses, results persisted to DB")


# ── Weekly near-miss report with Claude AI ────────────────────
def generate_weekly_near_miss_report():
    import json
    misses = [m for m in near_miss_tracker.values() if len(m["prices_since"]) >= 3]
    if len(misses) < 3:
        return "Not enough data yet — needs at least 3 near-misses with 3+ days of follow-up."
    winners = []; losers = []
    for m in misses:
        pct = ((m["prices_since"][-1] - m["price_at_miss"]) / m["price_at_miss"]) * 100
        m["pct_move"] = round(pct, 2)
        if pct > 2:    winners.append(m)
        elif pct < -2: losers.append(m)
    triggered = [m for m in misses if m["triggered"]]
    lines = []
    for m in misses[:20]:
        prices_str = " → ".join([f"${p:.4f}" for p in [m["price_at_miss"]] + m["prices_since"]])
        pct        = m.get("pct_move", 0)
        outcome    = "UP" if pct > 2 else ("DOWN" if pct < -2 else "FLAT")
        trig       = "triggered" if m["triggered"] else "never triggered"
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
            json={"model": "claude-sonnet-4-6", "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if r.ok:
            return r.json()["content"][0]["text"]
        return f"Claude unavailable: {r.status_code}"
    except Exception as e:
        return f"Claude error: {e}"


# ── Near-miss section for emails ──────────────────────────────
def build_near_miss_section(label, candidates, threshold, top_n=10):
    if not candidates:
        return f"{label} NEAR MISSES\n{'─'*50}\n  No scan data yet\n"
    near_misses = [c for c in candidates if c.get("score", 0) < threshold and c.get("score", 0) > 0]
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
        gap_bar   = "█" * int(score) + "░" * int(threshold - score)
        lines.append(
            f"  {sym:<10} score:{score:.1f}/{threshold:.0f}  [{gap_bar}]  "
            f"gap:{gap:.1f}  RSI:{rsi_str}  vol:{vol_ratio:.1f}x  SMA cross:{sma_cross}  "
            f"${price:.4f}  ← {gap:.1f} away from trade"
        )
    header = f"{label} NEAR MISSES (top {len(near_misses)}, threshold={threshold})\n{'─'*50}\n"
    body   = "\n".join(lines)
    footer = "\n\nHow to read: score/threshold | gap = points needed | SMA cross = crossover signal fired\n"
    return header + body + footer
