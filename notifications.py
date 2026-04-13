"""
app/notifications.py — AlphaBot Notifications
Telegram alerts, daily email summary, morning briefing, weekly near-miss report.
"""

import smtplib
import requests
import logging
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from core.config import (
    log, GMAIL_USER, GMAIL_PASS, EMAIL_TO,
    TELEGRAM_TOKEN, TELEGRAM_CHAT,
    NEWS_API_KEY, CLAUDE_API_KEY,
    MIN_SIGNAL_SCORE, IS_LIVE,
    state, crypto_state, smallcap_state, intraday_state, crypto_intraday_state,
    news_state, near_miss_tracker, account_info,
    US_WATCHLIST,
)

# ── Telegram ──────────────────────────────────────────────────
_last_tg_msg = {}

def tg(message, category="info", force=False):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    try:
        now = time.time()
        if not force and category in _last_tg_msg:
            if now - _last_tg_msg[category] < 300: return
        _last_tg_msg[category] = now
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT, "text": message, "parse_mode": "HTML"
        }, timeout=10)
        if not resp.ok:
            log.debug(f"[TG] Failed: {resp.status_code}")
    except Exception as e:
        log.debug(f"[TG] Error: {e}")

def tg_critical(message):
    tg(message, category=f"critical_{message[:20]}", force=True)

def tg_trade_buy(symbol, price, score, market="stock"):
    emoji = "🟢" if market == "stock" else "💎"
    now   = datetime.now().strftime("%H:%M:%S")
    msg   = (f"{emoji} <b>BUY — {symbol}</b>"
             f"\nPrice: <code>${price:.4f}</code>"
             f"\nScore: <code>{score:.1f}/11</code>"
             f"\nMarket: {market.upper()}"
             f"\nTime: {now} Paris")
    tg(msg, category=f"buy_{symbol}", force=True)

def tg_trade_sell(symbol, price, pnl, hold_hours, reason, market="stock"):
    emoji = "✅" if pnl >= 0 else "❌"
    sign  = "+" if pnl >= 0 else ""
    now   = datetime.now().strftime("%H:%M:%S")
    msg   = (f"{emoji} <b>SELL — {symbol}</b>"
             f"\nPrice: <code>${price:.4f}</code>"
             f"\nP&L: <code>{sign}${pnl:.2f}</code>"
             f"\nHold: {hold_hours:.1f}h"
             f"\nReason: {reason}"
             f"\nTime: {now} Paris")
    tg(msg, category=f"sell_{symbol}", force=True)

def tg_hot_miss(symbol, score, skip_reason, price):
    msg = (f"🔥 <b>HOT MISS — {symbol}</b>"
           f"\nScore: <code>{score:.1f}/11</code> — high quality!"
           f"\nBlocked by: <b>{skip_reason}</b>"
           f"\nPrice: <code>${price:.4f}</code>"
           f"\nConsider raising limits to capture these")
    tg(msg, category=f"hotmiss_{symbol}", force=True)


# ── News sentiment scan ───────────────────────────────────────
def fetch_news_for_symbol(symbol):
    if not NEWS_API_KEY: return []
    try:
        query = symbol.replace("/USD", "").replace("/BTC", "")
        url = (
            f"https://newsapi.org/v2/everything"
            f"?q={query}+stock+OR+{query}+shares+OR+{query}+earnings"
            f"&sortBy=publishedAt&pageSize=5&language=en&apiKey={NEWS_API_KEY}"
        )
        r = requests.get(url, timeout=8)
        if not r.ok: return []
        articles = r.json().get("articles", [])
        return [
            {"title": a.get("title",""), "source": a.get("source",{}).get("name",""), "published": a.get("publishedAt","")[:10]}
            for a in articles if a.get("title")
        ]
    except Exception as e:
        log.debug(f"News fetch {symbol}: {e}")
        return []

def analyse_sentiment_with_claude(symbol, headlines):
    if not CLAUDE_API_KEY or not headlines: return None, "no_data"
    import json
    try:
        headline_text = "\n".join(f"- {h['title']} ({h['source']}, {h['published']})" for h in headlines[:5])
        prompt = (
            f"You are a financial analyst. Analyse these news headlines for {symbol} "
            f"and return ONLY a JSON object with no markdown:\n\n{headline_text}\n\n"
            f'Return exactly: {{"sentiment": "POSITIVE" or "NEGATIVE" or "NEUTRAL", '
            f'"score": number from -1.0 to 1.0, '
            f'"skip": true or false, '
            f'"reason": "one short sentence", '
            f'"key_headline": "the most important headline"}}'
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=15,
        )
        if not r.ok: return None, "api_error"
        text = r.json()["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result, "ok"
    except Exception as e:
        log.debug(f"Sentiment {symbol}: {e}")
        return None, "error"

def run_morning_news_scan():
    global news_state
    today = datetime.now().date()
    log.info("=" * 50)
    log.info("MORNING NEWS SCAN starting...")
    log.info(f"Scanning {len(US_WATCHLIST)} stocks for sentiment")
    log.info("=" * 50)

    skip_list = {}; watch_list = {}; briefing = []
    skipped = positive = neutral = errors = 0

    if not NEWS_API_KEY:
        log.warning("NEWS_API_KEY not set — skipping news scan.")
        news_state["scan_complete"] = True
        news_state["last_scan_day"] = today
        return

    for symbol in US_WATCHLIST:
        try:
            headlines = fetch_news_for_symbol(symbol)
            if not headlines: neutral += 1; continue
            result, status = analyse_sentiment_with_claude(symbol, headlines)
            if not result or status != "ok": errors += 1; continue
            if not isinstance(result.get("score"), (int, float)): errors += 1; continue
            sentiment    = result.get("sentiment", "NEUTRAL")
            score        = result.get("score", 0)
            should_skip  = result.get("skip", False)
            reason       = result.get("reason", "")
            key_headline = result.get("key_headline", headlines[0]["title"] if headlines else "")
            if should_skip or sentiment == "NEGATIVE":
                skip_list[symbol] = {"sentiment": sentiment, "score": score, "reason": reason, "headline": key_headline}
                briefing.append(f"  🔴 SKIP  {symbol:8} | {reason}")
                log.info(f"[NEWS] SKIP {symbol}: {reason}")
                skipped += 1
            elif sentiment == "POSITIVE" and score > 0.3:
                watch_list[symbol] = {"sentiment": sentiment, "score": score, "reason": reason, "headline": key_headline}
                briefing.append(f"  🟢 BOOST {symbol:8} | {reason}")
                log.info(f"[NEWS] POSITIVE {symbol}: {reason}")
                positive += 1
            else:
                neutral += 1
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"[NEWS] Error scanning {symbol}: {e}")
            errors += 1

    news_state.update({
        "skip_list": skip_list, "watch_list": watch_list,
        "briefing": briefing, "last_scan_day": today,
        "last_scan_time": datetime.now().strftime("%H:%M:%S"),
        "scan_complete": True,
    })
    log.info(f"[NEWS] Scan complete: {skipped} skip | {positive} positive | {neutral} neutral | {errors} errors")
    send_morning_briefing(skipped, positive, neutral)


# ── Morning briefing email ────────────────────────────────────
def send_morning_briefing(skipped, positive, neutral):
    from data.analytics import build_near_miss_section
    def fmt_item(sym, data, tag):
        return tag + " " + sym + " | " + data["reason"] + " | " + data["headline"]
    skip_lines  = "\n".join(fmt_item(s,d,"SKIP ") for s,d in news_state["skip_list"].items()) or "  None — all clear!"
    boost_lines = "\n".join(fmt_item(s,d,"BOOST") for s,d in news_state["watch_list"].items()) or "  None today"
    stocks_near_miss = build_near_miss_section("US STOCKS", state.candidates, MIN_SIGNAL_SCORE)
    crypto_near_miss = build_near_miss_section("CRYPTO", crypto_state.candidates, MIN_SIGNAL_SCORE)
    body = f"""
AlphaBot Morning Briefing
{'='*50}
Date:     {datetime.now().strftime('%A, %d %B %Y')}
Time:     {datetime.now().strftime('%H:%M ET')} (market opens at 9:30 ET)
Stocks scanned: {len(US_WATCHLIST)}
Signal threshold: {MIN_SIGNAL_SCORE}/10

SKIPPING TODAY ({skipped} stocks — negative news):
{skip_lines}

POSITIVE SIGNALS ({positive} stocks — good news):
{boost_lines}

SUMMARY
{'─'*50}
  {skipped} stocks skipped due to negative news
  {positive} stocks flagged as positive
  {neutral} stocks with neutral/no news — trading normally

{'='*50}
SIGNAL SCORECARD — Near Misses
{'='*50}
{stocks_near_miss}
{crypto_near_miss}
{'='*50}
Sent by AlphaBot · Market opens in ~30 minutes
""".strip()
    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = f"AlphaBot Morning Briefing — {datetime.now().strftime('%d %b %Y')} ({skipped} stocks skipped)"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Morning briefing emailed to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Morning briefing email failed: {e}")


# ── Daily summary email ───────────────────────────────────────
def send_daily_summary():
    from data.analytics import build_near_miss_section, analyse_edge
    from core.risk import calc_sharpe, calc_profit_factor

    def section(st):
        sells = [t for t in st.trades if t["side"] == "SELL" and t.get("pnl") is not None]
        wins  = [t for t in sells if t["pnl"] > 0]
        def fmt_trade(t):
            lines = []
            if t["side"] == "BUY":
                lines.append(f"  {t['time']}  BUY   {t['symbol']:10}  ${t['price']:.4f}")
                if t.get("score"):
                    lines.append(f"    Score: {t['score']}/10  RSI: {t.get('rsi','?')}  Vol: {t.get('vol_ratio','?')}x")
                if t.get("breakdown"):
                    for line in t["breakdown"].split("\n")[2:-1]:
                        lines.append(f"  {line}")
            else:
                sign    = "+" if t.get("pnl", 0) >= 0 else ""
                pnl_str = f"  P&L: {sign}${t['pnl']:.2f}" if t.get("pnl") is not None else ""
                hold_str= f"  Held: {t['hold_hours']}h" if t.get("hold_hours") else ""
                lines.append(f"  {t['time']}  SELL  {t['symbol']:10}  ${t['price']:.4f}{pnl_str}{hold_str}")
                if t.get("breakdown"):
                    for line in t["breakdown"].split("\n")[2:-1]:
                        lines.append(f"  {line}")
            return "\n".join(lines)
        trade_lines = "\n\n".join(fmt_trade(t) for t in st.trades[:10]) or "  No trades today"
        return (f"{st.label}\n{'─'*40}\n"
                f"Daily P&L:   ${st.daily_pnl:+.2f}\n"
                f"Trades:      {len(sells)}\n"
                f"Win rate:    {int(len(wins)/len(sells)*100) if sells else 0}%\n"
                f"Positions:   {len(st.positions)}\n\n"
                f"Trade log:\n{trade_lines}\n")

    news_summary = ""
    if news_state["scan_complete"]:
        skips  = "\n".join(f"  🔴 {s}: {d['reason']}" for s,d in news_state["skip_list"].items()) or "  None"
        boosts = "\n".join(f"  🟢 {s}: {d['reason']}" for s,d in news_state["watch_list"].items()) or "  None"
        news_summary = f"\nMORNING NEWS SCAN\n{'─'*40}\nSkipped:\n{skips}\nPositive:\n{boosts}\n"

    stocks_near_miss = build_near_miss_section("US STOCKS", state.candidates, MIN_SIGNAL_SCORE)
    crypto_near_miss = build_near_miss_section("CRYPTO", crypto_state.candidates, MIN_SIGNAL_SCORE)
    near_miss_summary = (
        f"\nSIGNAL SCORECARD — Near Misses\n{'='*40}\n"
        f"Stocks and crypto that almost traded today.\n"
        f"Use this to tune MIN_SIGNAL_SCORE (currently {MIN_SIGNAL_SCORE}/10)\n\n"
        f"{stocks_near_miss}\n{crypto_near_miss}"
    )
    edge_analysis = analyse_edge()

    body = (f"AlphaBot Daily Summary\n{'='*40}\n"
            f"Date: {datetime.now().strftime('%A, %d %B %Y')}\n"
            f"Mode: {'LIVE' if IS_LIVE else 'Paper'} Trading\n"
            f"Portfolio: ${float(account_info.get('portfolio_value',0)):,.2f}\n\n"
            f"{section(state)}\n{section(smallcap_state)}\n{section(intraday_state)}\n"
            f"{section(crypto_state)}\n{section(crypto_intraday_state)}\n"
            f"{news_summary}"
            f"{near_miss_summary}\n"
            f"\nEDGE ANALYSIS\n{edge_analysis}\n"
            f"{'='*40}\nSent by AlphaBot")
    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg["Subject"] = f"AlphaBot Daily Summary — {datetime.now().strftime('%d %b %Y')}"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Summary emailed to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ── Weekly near-miss report ───────────────────────────────────
def send_weekly_near_miss_email():
    from data.analytics import run_near_miss_simulations, generate_weekly_near_miss_report, build_sparkline_html
    from data.database import db_record_report
    try:
        run_near_miss_simulations()
        misses = [m for m in near_miss_tracker.values() if len(m["prices_since"]) >= 1]
        if not misses:
            log.info("[WEEKLY] No near-miss data to report yet")
            return

        claude_analysis = generate_weekly_near_miss_report()

        total_sim_pnl  = sum(m["simulation"]["pnl_usd"] for m in misses if m.get("simulation"))
        missed_profit  = sum(m["simulation"]["pnl_usd"] for m in misses if m.get("simulation") and m["simulation"]["profitable"])
        avoided_loss   = sum(abs(m["simulation"]["pnl_usd"]) for m in misses if m.get("simulation") and not m["simulation"]["profitable"])

        rows = ""
        for m in sorted(misses, key=lambda x: x.get("pct_move", 0), reverse=True)[:20]:
            pct      = m.get("pct_move", 0)
            color    = "#00ff88" if pct >= 0 else "#ff4466"
            spark    = build_sparkline_html(m["price_at_miss"], m["prices_since"])
            trig     = "✅ Triggered!" if m["triggered"] else "❌ Never triggered"
            trig_col = "#00ff88" if m["triggered"] else "#555"
            sim      = m.get("simulation", {})
            sim_pnl  = f"+${sim['pnl_usd']:.2f} ({sim['exit_reason']})" if sim else "Pending"
            sim_peak = f"+{sim['max_profit_pct']:.1f}%" if sim else "—"
            rows += (
                f'<tr>'
                f'<td style="font-weight:700;color:#00aaff">{m["symbol"]}</td>'
                f'<td>{m["date"]}</td>'
                f'<td style="color:#ffcc00">{m["score"]}/{m["threshold"]}</td>'
                f'<td style="color:#ff8800">{m["gap"]}</td>'
                f'<td>${m["price_at_miss"]:.4f}</td>'
                f'<td>{spark}</td>'
                f'<td style="color:{color};font-weight:700">{pct:+.1f}%</td>'
                f'<td style="color:#888">{sim_pnl}</td>'
                f'<td style="color:#00ff88">{sim_peak}</td>'
                f'<td style="color:{trig_col}">{trig}</td>'
                f'</tr>'
            )

        winners   = len([m for m in misses if m.get("pct_move", 0) > 2])
        losers    = len([m for m in misses if m.get("pct_move", 0) < -2])
        triggered = len([m for m in misses if m["triggered"]])

        html = f"""<!DOCTYPE html>
<html>
<head><style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #090b0e; color: #e0e0e0; padding: 24px; }}
  h1 {{ color: #00ff88; }} h2 {{ color: #00aaff; border-bottom: 1px solid #1a2a1a; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  th {{ background: #0d1117; color: #555; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; padding: 10px; text-align: left; }}
  td {{ padding: 10px; border-top: 1px solid #1a1a1a; font-size: 13px; }}
  .stat {{ display: inline-block; background: #0d1117; border-radius: 8px; padding: 12px 20px; margin: 8px; text-align: center; }}
  .stat-val {{ font-size: 24px; font-weight: 700; }}
  .insight {{ background: #0d1117; border-left: 3px solid #00aaff; padding: 16px; margin: 16px 0; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; }}
</style></head>
<body>
<h1>⚡ AlphaBot Weekly Near-Miss Report</h1>
<p style="color:#555">Week ending {datetime.now().strftime('%B %d, %Y')} · Threshold: {MIN_SIGNAL_SCORE}/11</p>
<div>
  <div class="stat"><div class="stat-val" style="color:#ffcc00">{len(misses)}</div><div>Near-Misses</div></div>
  <div class="stat"><div class="stat-val" style="color:#00ff88">{winners}</div><div>Went Up 2%+</div></div>
  <div class="stat"><div class="stat-val" style="color:#ff4466">{losers}</div><div>Went Down 2%+</div></div>
  <div class="stat"><div class="stat-val" style="color:#00aaff">{triggered}</div><div>Eventually Triggered</div></div>
  <div class="stat"><div class="stat-val" style="color:{'#00ff88' if total_sim_pnl >= 0 else '#ff4466'}">${total_sim_pnl:+.2f}</div><div>Simulated P&L</div></div>
  <div class="stat"><div class="stat-val" style="color:#00ff88">${missed_profit:+.2f}</div><div>Profit Missed</div></div>
  <div class="stat"><div class="stat-val" style="color:#ff8800">${avoided_loss:.2f}</div><div>Loss Avoided</div></div>
</div>
<h2>🤖 Claude AI Analysis</h2>
<div class="insight">{claude_analysis}</div>
<h2>📊 Near-Miss Detail</h2>
<table>
  <thead><tr>
    <th>Symbol</th><th>Date</th><th>Score</th><th>Gap</th>
    <th>Entry</th><th>Chart</th><th>5-Day</th>
    <th>Simulated Exit</th><th>Peak</th><th>Status</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="color:#333;font-size:11px;margin-top:32px">AlphaBot Weekly Report · {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC</p>
</body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"AlphaBot Weekly Near-Miss Report — {datetime.now().strftime('%b %d')}"
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())

        log.info(f"[WEEKLY] Near-miss report sent — {len(misses)} misses analysed")
        db_record_report("weekly", f"AlphaBot Weekly Near-Miss Report — {datetime.now().strftime('%b %d')}", html, claude_analysis)

    except Exception as e:
        log.error(f"[WEEKLY] Failed to send report: {e}")
