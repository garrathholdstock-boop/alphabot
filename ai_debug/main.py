"""
AlphaBot Debug Agent v8
- Priority matrix: P1 Safety, P2 Efficiency, P3 Bugs
- Background monitor thread (every 5 mins)
- Telegram: P1 immediate, P2 immediate during market hours + digest, P3 morning only
- Auto-fix: restart bot, clear screens, dismiss cosmetics
- Approval queue with Claude briefing cards
- Persistent log file + agent_events DB table
- Morning briefing 7am Paris
- CONTEXT.md auto-sync nightly
"""

from fastapi import FastAPI, Form, Response, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import os, subprocess, sqlite3, json, anthropic, base64, html, re, uuid
import threading, time, requests, logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import urlopen
from urllib.error import URLError

app = FastAPI(root_path=os.environ.get("ROOT_PATH", ""))
SESSIONS = {}

# ── Config ────────────────────────────────────────────────────
CLAUDE_API_KEY    = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "8749498685:AAHIlJrx6Hf8SxyF5R0oXPJGYoFN5JnEg5c")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")  # swap in when you have it
DB_PATH           = "/home/alphabot/app/alphabot.db"
APP_PATH          = "/home/alphabot/app"
CONTEXT_PATH      = "/home/alphabot/app/ai_debug/CONTEXT.md"
LOG_PATH          = "/home/alphabot/app/alphabot.log"
SCREEN_NAME       = "alphabot"
PARIS             = ZoneInfo("Europe/Paris")
MAX_STEPS_BEFORE_COMPRESS = 10

# ── Approval queue (in-memory, persisted to DB) ───────────────
approval_queue = []   # list of dicts
auto_fixed_log = []   # list of dicts
_queue_lock = threading.Lock()

# ── Known cosmetic errors to always ignore ────────────────────
COSMETIC_PATTERNS = [
    "Error 10089", "Error 300", "BrokenPipeError",
    "DeprecationWarning", "reqHistoricalData: Timeout",
    "Can't find EId", "No historical data query",
    "Connection reset by peer", "Market data farm connection is OK",
    "HMDS data farm connection is OK", "Sec-def data farm",
]

# ── P1 Safety triggers ────────────────────────────────────────
P1_PATTERNS = [
    ("bot_down",          r"No Sockets found|screen.*removed|Broken pipe",
                          "🔴 P1: Bot process DOWN"),
    ("stop_not_firing",   r"unrealizedPNL=-[6-9]\d{3}|unrealizedPNL=-[1-9]\d{4}",
                          "🔴 P1: Position loss >$6k — stop may not be firing"),
    ("ibkr_disconnect",   r"API connection failed|Cannot connect to IBKR|connection refused",
                          "🔴 P1: IBKR disconnected with open positions"),
    ("kill_switch",       r"KILL SWITCH ACTIVE|PANIC KILL",
                          "🔴 P1: Kill switch triggered"),
    ("daily_loss",        r"Loss limit hit",
                          "🔴 P1: Daily loss limit hit — bot paused"),
]

# ── P2 Efficiency triggers ────────────────────────────────────
P2_PATTERNS = [
    ("no_trades_market_open", None,   # handled by logic, not regex
                          "🟡 P2: Market open but no trades in 90+ mins"),
    ("binance_failing",   r"Binance POST.*400|code.*-2010|code.*-1013",
                          "🟡 P2: Binance orders failing — crypto blocked"),
    ("binance_ban",       r"\[BINANCE\] Ban active|binance_ban_until",
                          "🟡 P2: Binance rate-limit ban active"),
    ("slow_cycle",        None,       # handled by timing logic
                          "🟡 P2: Bot cycle running >3× normal speed"),
    ("execution_block",   r"ORDER FAILED|place_order.*failed",
                          "🟡 P2: Order execution failing"),
    ("sector_cap_flood",  None,       # handled by log pattern analysis
                          "🟡 P2: Sector cap blocking repeatedly — consider raising MAX_SECTOR_POSITIONS"),
    ("rotation_bad_rate", None,       # handled by DB check
                          "🟡 P2: >50% of rotation decisions are BAD — rotation threshold may be too low"),
    ("intelligence_fail", r"\[INTELLIGENCE\].*failed|intelligence run failed",
                          "🟡 P2: Weekly intelligence run failed"),
]


# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════
def send_telegram(msg, priority="P3"):
    """Send Telegram message. Always sends P1. P2/P3 respect schedule."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID":
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        _log_agent(f"Telegram error: {e}")
        return False


def should_send_telegram(priority):
    """P1 always. P2 always (24/7 markets). P3 only in morning briefing."""
    if priority == "P1":
        return True
    if priority == "P2":
        return True  # 24/7 because crypto trades around the clock
    return False  # P3 goes in morning briefing only


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
def _log_agent(msg):
    """Write to agent log file."""
    try:
        ts = datetime.now(PARIS).strftime("%Y-%m-%d %H:%M:%S")
        with open("/home/alphabot/app/ai_debug/agent.log", "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except:
        pass


def ensure_log_file():
    """Ensure log directories exist and rotate logs older than 30 days."""
    os.makedirs("/home/alphabot/app/ai_debug", exist_ok=True)
    os.makedirs("/home/alphabot/app/logs", exist_ok=True)
    # Rotate agent log if >50MB
    agent_log = "/home/alphabot/app/ai_debug/agent.log"
    if os.path.exists(agent_log) and os.path.getsize(agent_log) > 50 * 1024 * 1024:
        ts = datetime.now(PARIS).strftime("%Y%m%d")
        os.rename(agent_log, f"/home/alphabot/app/ai_debug/agent_{ts}.log")
    # Delete agent logs older than 30 days
    try:
        import glob
        cutoff = datetime.now(PARIS).timestamp() - (30 * 86400)
        for f in glob.glob("/home/alphabot/app/ai_debug/agent_*.log"):
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                _log_agent(f"Rotated old log: {f}")
    except:
        pass


# ═══════════════════════════════════════════════════════════════
# DB — agent_events table
# ═══════════════════════════════════════════════════════════════
def init_agent_db():
    """Create agent_events table if not exists."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                priority TEXT,
                event_type TEXT,
                message TEXT,
                action_taken TEXT,
                resolved INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        _log_agent(f"init_agent_db error: {e}")


def db_log_event(priority, event_type, message, action_taken=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO agent_events (priority, event_type, message, action_taken) VALUES (?,?,?,?)",
            (priority, event_type, message, action_taken)
        )
        conn.commit()
        conn.close()
    except:
        pass


def db_get_recent_events(limit=20):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM agent_events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except:
        return []


# ═══════════════════════════════════════════════════════════════
# BOT STATUS HELPERS
# ═══════════════════════════════════════════════════════════════
def get_screen_log(lines=100):
    try:
        subprocess.run(["/usr/bin/screen", "-S", SCREEN_NAME, "-X", "hardcopy", "/tmp/ab.txt"],
                       timeout=3, capture_output=True)
        with open("/tmp/ab.txt", "r", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except:
        return ""


def get_log_file(lines=500, hours=None):
    """Read from the actual alphabot.log file — 30-day persistent, richer than screen buffer."""
    try:
        if not os.path.exists(LOG_PATH):
            return ""
        with open(LOG_PATH, "r", errors="replace") as f:
            all_lines = f.readlines()
        if hours:
            cutoff = datetime.now(PARIS) - timedelta(hours=hours)
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
            filtered = [l for l in all_lines if l[:16] >= cutoff_str]
            return "".join(filtered[-lines:])
        return "".join(all_lines[-lines:])
    except Exception as e:
        _log_agent(f"get_log_file error: {e}")
        return ""


def analyse_log_patterns(hours=6):
    """
    Deep pattern analysis on alphabot.log — looks for structural issues
    that only become visible across many cycles. Returns list of findings.
    """
    log_text = get_log_file(lines=2000, hours=hours)
    if not log_text:
        return []

    findings = []
    lines = log_text.split("\n")

    # Count skip reasons
    skip_counts = {}
    for line in lines:
        for reason in ["SECTOR_CAP", "MAX_TOTAL_POSITIONS", "MAX_DAILY_SPEND",
                       "CHOPPY_MARKET", "MAX_TRADES_DAY", "ORDER_FAILED", "MAX_EXPOSURE"]:
            if reason in line:
                skip_counts[reason] = skip_counts.get(reason, 0) + 1

    # Flag if any capacity skip > 20 occurrences in 6h
    for reason, cnt in skip_counts.items():
        if cnt >= 20:
            findings.append(("P2", "sector_cap_flood" if "SECTOR" in reason else "capacity_block",
                f"🟡 {reason} fired {cnt}× in last {hours}h — structural limit may need raising"))

    # Count ORDER FAILEDs
    order_fails = sum(1 for l in lines if "ORDER FAILED" in l or "place_order.*failed" in l.lower())
    if order_fails >= 5:
        findings.append(("P2", "execution_block",
            f"🟡 {order_fails} ORDER FAILED events in last {hours}h — execution reliability issue"))

    # Check for intelligence run failures
    intel_fails = sum(1 for l in lines if "[INTELLIGENCE]" in l and "failed" in l.lower())
    if intel_fails > 0:
        findings.append(("P2", "intelligence_fail",
            f"🟡 Intelligence run failed {intel_fails}× in log — check CLAUDE_API_KEY"))

    # Check rotation bad rate from DB
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("""SELECT
            SUM(CASE WHEN rotation_verdict='BAD' THEN 1 ELSE 0 END) as bad,
            COUNT(*) as total
            FROM rotations WHERE rotation_verdict IS NOT NULL
            AND created_at >= datetime('now','-7 days')""").fetchone()
        conn.close()
        if row and row[1] and row[1] >= 5:
            bad_rate = row[0] / row[1]
            if bad_rate > 0.5:
                findings.append(("P2", "rotation_bad_rate",
                    f"🟡 {int(bad_rate*100)}% of rotation decisions are BAD last 7d ({row[0]}/{row[1]}) — score gap threshold too low"))
    except:
        pass

    return findings


def is_bot_running():
    r = subprocess.run(["/usr/bin/screen", "-ls"], capture_output=True, text=True)
    return SCREEN_NAME in r.stdout


def get_positions_from_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Get open positions from recent trades (BUY without matching SELL)
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except:
        return []


def get_todays_pnl():
    try:
        conn = sqlite3.connect(DB_PATH)
        today = datetime.now(PARIS).strftime("%Y-%m-%d")
        r = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) as total FROM trades WHERE side='SELL' AND created_at >= ?",
            (today,)
        ).fetchone()
        conn.close()
        return float(r[0] or 0)
    except:
        return 0.0


def get_all_time_pnl():
    try:
        conn = sqlite3.connect(DB_PATH)
        r = conn.execute("SELECT COALESCE(SUM(pnl),0) as total FROM trades WHERE side='SELL'").fetchone()
        t = conn.execute("SELECT COUNT(*) as cnt FROM trades WHERE side='SELL'").fetchone()
        w = conn.execute("SELECT COUNT(*) as cnt FROM trades WHERE side='SELL' AND pnl>0").fetchone()
        conn.close()
        total = float(r[0] or 0)
        trades = int(t[0] or 0)
        wins = int(w[0] or 0)
        wr = int(wins/trades*100) if trades else 0
        return total, trades, wr
    except:
        return 0.0, 0, 0


def get_market_hours():
    now = datetime.now(timezone.utc)
    h = now.hour + now.minute / 60.0
    wd = now.weekday()
    wk = wd < 5
    return {
        "UTC_time": now.strftime("%H:%M UTC %a"),
        "Paris_time": datetime.now(PARIS).strftime("%H:%M Paris"),
        "US":     "OPEN" if wk and 14.5 <= h <= 21.0 else "CLOSED",
        "FTSE":   "OPEN" if wk and 8.0  <= h <= 16.5 else "CLOSED",
        "ASX":    "OPEN" if wk and (h >= 23.0 or h <= 5.0) else "CLOSED",
        "CRYPTO": "OPEN",
    }


def is_cosmetic(line):
    return any(p in line for p in COSMETIC_PATTERNS)


# ═══════════════════════════════════════════════════════════════
# AUTO-FIX ACTIONS
# ═══════════════════════════════════════════════════════════════
def auto_restart_bot():
    """Restart bot if down. Returns True if restarted."""
    try:
        subprocess.run("pkill -9 -f 'python3 -m app.main'", shell=True, timeout=5)
        subprocess.run("/usr/bin/screen -wipe", shell=True, timeout=5)
        time.sleep(2)
        subprocess.run(f"bash /home/alphabot/start.sh", shell=True, timeout=10,
                      cwd=APP_PATH)
        time.sleep(5)
        if is_bot_running():
            msg = (f"✅ <b>AlphaBot Auto-Restarted</b>\n"
                   f"Time: {datetime.now(PARIS).strftime('%H:%M Paris')}\n"
                   f"Status: Screen session confirmed running")
            send_telegram(msg, "P1")
            db_log_event("P1", "bot_down", "Bot was down", "Auto-restarted successfully")
            auto_fixed_log.insert(0, {
                "time": datetime.now(PARIS).strftime("%H:%M"),
                "action": "Bot restarted",
                "result": "✅ Running"
            })
            _log_agent("Auto-restarted bot successfully")
            return True
        else:
            send_telegram("🔴 <b>P1 CRITICAL: Bot restart FAILED</b>\nManual intervention required.", "P1")
            return False
    except Exception as e:
        _log_agent(f"auto_restart_bot error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# RULES ENGINE
# ═══════════════════════════════════════════════════════════════
_last_cycle_time = None
_last_no_trade_alert = None
_last_binance_alert = None
_monitor_running = False


def classify_log(log_text):
    """Parse log, skip cosmetics, return list of (priority, event_type, message)."""
    events = []
    lines = log_text.split("\n")

    for line in lines:
        if not line.strip():
            continue
        if is_cosmetic(line):
            continue

        # Check P1 patterns
        for ev_type, pattern, msg in P1_PATTERNS:
            if pattern and re.search(pattern, line, re.IGNORECASE):
                events.append(("P1", ev_type, msg, line.strip()))
                break

        # Check P2 patterns
        for ev_type, pattern, msg in P2_PATTERNS:
            if pattern and re.search(pattern, line, re.IGNORECASE):
                events.append(("P2", ev_type, msg, line.strip()))
                break

    return events


def _check_signal_execution_gap(log, now_paris, alerted):
    """
    P2 trigger: a BUY signal met threshold AND slots are available
    but no order was placed in the same cycle window.
    Detects: "✅ BUY SIGNAL BREAKDOWN" followed by no "Executing: BUY" within 2 log lines,
    OR "qualified BUY" count > 0 but no BUY order in last 10 mins during market hours.
    """
    try:
        lines = log.split("\n")
        # Look for qualified BUY signals in the log
        qualified_buys = [l for l in lines if "qualified BUY" in l and "/82 scanned" not in l
                         and "0 qualified" not in l]
        executing_buys = [l for l in lines if "Executing: BUY" in l or "IBKR MARKET] BUY" in l
                         or "IBKR LIMIT] BUY" in l]

        if qualified_buys and not executing_buys:
            # Signals qualified but no execution attempted at all
            if "signal_no_execution" not in alerted:
                alerted.add("signal_no_execution")
                sample = qualified_buys[-1][:80] if qualified_buys else ""
                with _queue_lock:
                    approval_queue.insert(0, {
                        "id": str(uuid.uuid4())[:8],
                        "priority": "P2",
                        "event_type": "signal_no_execution",
                        "message": "BUY signal qualified but no order executed — slot may be blocked",
                        "detail": sample,
                        "time": now_paris.strftime("%H:%M"),
                        "status": "pending",
                        "claude_briefing": _generate_briefing("no_trades_90min", log),
                    })
                db_log_event("P2", "signal_no_execution",
                            "Qualified BUY signal with no execution", sample)
                send_telegram(
                    f"🟡 <b>P2: BUY signal blocked</b>\n"
                    f"Signal qualified but no order placed.\n"
                    f"Check: position caps, daily spend limit, regime.\n"
                    f"→ http://178.104.170.58:8000", "P2")
        elif qualified_buys and executing_buys:
            # Good — signals are executing, clear the alert
            alerted.discard("signal_no_execution")
        else:
            # No signals — market quiet, totally normal
            alerted.discard("signal_no_execution")
    except Exception as e:
        _log_agent(f"_check_signal_execution_gap error: {e}")


def run_monitor():
    """Background thread — runs every 5 mins."""
    global _last_cycle_time, _last_no_trade_alert, _last_binance_alert, _monitor_running

    _monitor_running = True
    init_agent_db()
    ensure_log_file()
    _log_agent("Monitor thread started")

    # Track what we've already alerted on to avoid spam
    alerted = set()
    last_binance_alert_time = None
    last_p2_digest_time = None
    p2_digest_queue = []

    while True:
        try:
            now_paris = datetime.now(PARIS)

            # ── Check bot running (P1) ──────────────────────
            if not is_bot_running():
                if "bot_down" not in alerted:
                    _log_agent("Bot down — attempting auto-restart")
                    send_telegram("🔴 <b>P1: Bot DOWN detected</b> — attempting auto-restart...", "P1")
                    restarted = auto_restart_bot()
                    if restarted:
                        alerted.discard("bot_down")
                    else:
                        alerted.add("bot_down")
                        db_log_event("P1", "bot_down", "Bot down, restart failed", "Restart attempted")
            else:
                alerted.discard("bot_down")

            # ── Read and classify logs ──────────────────────
            log = get_screen_log(150)
            events = classify_log(log)

            p1_events = [(t, m, l) for p, t, m, l in events if p == "P1"]
            p2_events = [(t, m, l) for p, t, m, l in events if p == "P2"]

            # ── Handle P1 events (immediate Telegram) ───────
            for ev_type, msg, raw_line in p1_events:
                key = f"p1_{ev_type}_{raw_line[:50]}"
                if key not in alerted:
                    alerted.add(key)
                    alert = (f"🔴 <b>{msg}</b>\n"
                             f"Time: {now_paris.strftime('%H:%M Paris')}\n"
                             f"Log: <code>{html.escape(raw_line[:200])}</code>")
                    send_telegram(alert, "P1")
                    db_log_event("P1", ev_type, msg, raw_line[:300])
                    _log_agent(f"P1 alert sent: {ev_type}")

            # ── Handle P2 events ─────────────────────────────
            for ev_type, msg, raw_line in p2_events:
                key = f"p2_{ev_type}"
                # Binance: dedupe — only alert once per hour
                if ev_type == "binance_failing":
                    if last_binance_alert_time and (now_paris - last_binance_alert_time).seconds < 3600:
                        continue
                    last_binance_alert_time = now_paris

                if key not in alerted:
                    alerted.add(key)
                    p2_digest_queue.append((ev_type, msg, raw_line, now_paris))
                    # Add to approval queue
                    with _queue_lock:
                        approval_queue.insert(0, {
                            "id": str(uuid.uuid4())[:8],
                            "priority": "P2",
                            "event_type": ev_type,
                            "message": msg,
                            "detail": raw_line[:300],
                            "time": now_paris.strftime("%H:%M"),
                            "status": "pending",
                            "claude_briefing": None,
                        })
                    db_log_event("P2", ev_type, msg, raw_line[:300])

            # ── P2 digest every 5 mins if there are new events ──
            if p2_digest_queue:
                if not last_p2_digest_time or (now_paris - last_p2_digest_time).seconds >= 300:
                    last_p2_digest_time = now_paris
                    digest = _build_p2_digest(p2_digest_queue, now_paris)
                    send_telegram(digest, "P2")
                    p2_digest_queue.clear()
                    _log_agent("P2 digest sent")

            # ── No-trade check (P2) ──────────────────────────
            markets = get_market_hours()
            any_market_open = any(markets[m] == "OPEN" for m in ["US", "FTSE", "ASX", "CRYPTO"])
            if any_market_open:
                # Real P2: signal qualified but no execution — slots may be blocked
                _check_signal_execution_gap(log, now_paris, alerted)
            else:
                # Markets closed — reset execution gap alert
                alerted.discard("signal_no_execution")

            # ── Deep log pattern analysis (every 30 mins) ────
            if not hasattr(run_monitor, '_last_pattern_check'):
                run_monitor._last_pattern_check = None
            if (run_monitor._last_pattern_check is None or
                    (now_paris - run_monitor._last_pattern_check).seconds >= 1800):
                run_monitor._last_pattern_check = now_paris
                try:
                    pattern_findings = analyse_log_patterns(hours=6)
                    for pri, ev_type, msg in pattern_findings:
                        key = f"pattern_{ev_type}"
                        if key not in alerted:
                            alerted.add(key)
                            with _queue_lock:
                                approval_queue.insert(0, {
                                    "id": str(uuid.uuid4())[:8],
                                    "priority": pri,
                                    "event_type": ev_type,
                                    "message": msg,
                                    "detail": "Detected via 6h log pattern analysis",
                                    "time": now_paris.strftime("%H:%M"),
                                    "status": "pending",
                                    "claude_briefing": None,
                                })
                            db_log_event(pri, ev_type, msg, "log_pattern_analysis")
                            send_telegram(f"{'🔴' if pri=='P1' else '🟡'} <b>{msg}</b>\n→ http://178.104.170.58:8000", pri)
                            _log_agent(f"Pattern finding: {ev_type}")
                except Exception as e:
                    _log_agent(f"Pattern analysis error: {e}")


            # ── Morning briefing at 07:00 Paris ─────────────
            if now_paris.hour == 7 and now_paris.minute < 5:
                if "morning_brief_sent" not in alerted:
                    alerted.add("morning_brief_sent")
                    _send_morning_briefing(now_paris)
                    _update_context_md()
            elif now_paris.hour != 7:
                alerted.discard("morning_brief_sent")

            # ── Daily backup at 02:00 Paris ──────────────────
            # Runs every night at 2am — markets closed everywhere
            # Keeps 30 days, auto-deletes older folders
            if now_paris.hour == 2 and now_paris.minute < 5:
                if "daily_backup_done" not in alerted:
                    alerted.add("daily_backup_done")
                    try:
                        folder, results = _do_backup()
                        ok_count = sum(1 for r in results if r["ok"])
                        _log_agent(f"Daily backup complete: {folder} ({ok_count}/{len(results)} files)")
                        # Auto-clean backups older than 30 days
                        import glob as _gb
                        cutoff = datetime.now(PARIS).timestamp() - (30 * 86400)
                        for d in _gb.glob(os.path.join(BACKUP_ROOT, "????????")):
                            if os.path.getmtime(d) < cutoff:
                                import shutil as _sh
                                _sh.rmtree(d)
                                _log_agent(f"Auto-removed old backup: {d}")
                    except Exception as e:
                        _log_agent(f"Daily backup failed: {e}")
            elif now_paris.hour != 2:
                alerted.discard("daily_backup_done")

            # ── Clear old P1 alerts after 1 hour ────────────
            # (so if the issue recurs next day we alert again)
            to_remove = [k for k in alerted if k.startswith("p1_") or k.startswith("p2_")]
            # Keep for 1 cycle (5 mins) then clear non-critical ones
            # We'll rely on the log age to naturally clear

        except Exception as e:
            _log_agent(f"Monitor error: {e}")

        time.sleep(300)  # 5 minutes


def _build_p2_digest(events, now_paris):
    lines = [f"🟡 <b>AlphaBot P2 Digest</b> — {now_paris.strftime('%H:%M Paris')}\n"]
    for ev_type, msg, raw_line, t in events:
        lines.append(f"• {msg}")
    lines.append(f"\n→ Review at http://178.104.170.58:8000")
    return "\n".join(lines)


def _generate_briefing(event_type, log_snippet):
    """Generate a Claude briefing card for complex issues."""
    if not CLAUDE_API_KEY:
        return None
    briefings = {
        "no_trades_90min": (
            "📋 <b>CLAUDE BRIEFING — No trades firing</b>\n\n"
            "Paste this into Claude:\n\n"
            "AlphaBot has been running with markets open for 90+ mins but no trades executed. "
            "Please check: (1) MIN_SIGNAL_SCORE threshold, (2) market regime (BEAR mode?), "
            "(3) VWAP filter blocking all entries, (4) position caps already full. "
            f"Recent log snippet available in approval queue."
        ),
        "binance_failing": (
            "📋 <b>CLAUDE BRIEFING — Binance orders failing</b>\n\n"
            "Paste this into Claude:\n\n"
            "Binance crypto orders failing with -2010 (insufficient balance). "
            "BINANCE_SECRET may be corrupted in .env. "
            "Check: grep BINANCE_SECRET /home/alphabot/app/.env"
        ),
    }
    return briefings.get(event_type)


def _send_morning_briefing(now_paris):
    """Send 7am Paris morning briefing via Telegram."""
    try:
        log = get_log_file(lines=200, hours=12)  # Use real log file, last 12h
        total_pnl, total_trades, win_rate = get_all_time_pnl()
        today_pnl = get_todays_pnl()
        markets = get_market_hours()
        bot_up = is_bot_running()
        events = db_get_recent_events(10)
        queue_count = len([q for q in approval_queue if q["status"] == "pending"])

        # Overnight events summary
        overnight = [e for e in events if e.get("priority") in ["P1", "P2"]]
        auto_fixed = [e for e in events if e.get("action_taken") and "auto" in e.get("action_taken","").lower()]

        # Near-miss overnight summary
        nm_score = nm_cap = nm_sim = 0
        top_caps = []
        try:
            conn = sqlite3.connect(DB_PATH)
            yesterday = (datetime.now(PARIS) - timedelta(hours=24)).strftime("%Y-%m-%d")
            nm_score = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason='SCORE' AND date >= ?", (yesterday,)).fetchone()[0] or 0
            nm_cap   = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason!='SCORE' AND date >= ?", (yesterday,)).fetchone()[0] or 0
            nm_sim_row = conn.execute("SELECT ROUND(SUM(simulated_pnl_usd),2) FROM near_misses WHERE simulated_pnl_pct IS NOT NULL AND date >= ?", (yesterday,)).fetchone()
            nm_sim   = float(nm_sim_row[0] or 0)
            top_caps = conn.execute("SELECT skip_reason, COUNT(*) FROM near_misses WHERE skip_reason!='SCORE' AND date >= ? GROUP BY skip_reason ORDER BY COUNT(*) DESC LIMIT 3", (yesterday,)).fetchall()
            conn.close()
        except:
            pass

        # Rotation quality overnight
        rot_good = rot_bad = rot_neutral = 0
        try:
            conn = sqlite3.connect(DB_PATH)
            yesterday_ts = (datetime.now(PARIS) - timedelta(hours=24)).isoformat()
            for verdict in ["GOOD", "BAD", "NEUTRAL"]:
                cnt = conn.execute("SELECT COUNT(*) FROM rotations WHERE rotation_verdict=? AND created_at >= ?", (verdict, yesterday_ts)).fetchone()[0] or 0
                if verdict == "GOOD": rot_good = cnt
                elif verdict == "BAD": rot_bad = cnt
                else: rot_neutral = cnt
            conn.close()
        except:
            pass

        # Pending intelligence recommendations
        pending_recs = 0
        try:
            conn = sqlite3.connect(DB_PATH)
            pending_recs = conn.execute("SELECT COUNT(*) FROM tuning_recommendations WHERE status='PENDING'").fetchone()[0] or 0
            conn.close()
        except:
            pass

        status_icon = "✅" if bot_up else "🔴"
        msg_lines = [
            f"☀️ <b>AlphaBot Morning Briefing</b>",
            f"{now_paris.strftime('%A %d %B — %H:%M Paris')}",
            f"",
            f"<b>Bot Status</b>",
            f"{status_icon} Bot: {'RUNNING' if bot_up else 'DOWN'}",
            f"💼 All-time P&L: <b>${total_pnl:+,.2f}</b> ({total_trades} trades, {win_rate}% win rate)",
            f"📅 Today P&L: <b>${today_pnl:+,.2f}</b>",
            f"",
            f"<b>Markets</b>",
            f"🇺🇸 US: {markets['US']}",
            f"🇬🇧 FTSE: {markets['FTSE']}",
            f"🇦🇺 ASX: {markets['ASX']}",
            f"🪙 Crypto: {markets['CRYPTO']}",
            f"",
        ]

        # Near-miss overnight
        if nm_score + nm_cap > 0:
            msg_lines.append(f"<b>Near Misses (last 24h)</b>")
            msg_lines.append(f"📊 Score-based: {nm_score} | Capacity-blocked: {nm_cap}")
            if nm_sim != 0:
                msg_lines.append(f"💸 Simulated missed: ${nm_sim:+.2f}")
            if top_caps:
                msg_lines.append(f"Top blocks: {', '.join(f'{r[0]}×{r[1]}' for r in top_caps)}")
            msg_lines.append("")

        # Rotation quality
        total_rots = rot_good + rot_bad + rot_neutral
        if total_rots > 0:
            msg_lines.append(f"<b>Rotation Quality (last 24h)</b>")
            msg_lines.append(f"✅ Good: {rot_good} | ❌ Bad: {rot_bad} | — Neutral: {rot_neutral}")
            if rot_bad > rot_good:
                msg_lines.append(f"⚠️ More bad rotations than good — review /intelligence")
            msg_lines.append("")

        if overnight:
            msg_lines.append(f"<b>Overnight Events ({len(overnight)})</b>")
            for e in overnight[:5]:
                msg_lines.append(f"• {e.get('priority','?')} {e.get('message','')[:60]}")
            msg_lines.append("")

        if auto_fixed:
            msg_lines.append(f"✅ Auto-fixed: {len(auto_fixed)} issues")
            msg_lines.append("")

        if pending_recs > 0:
            msg_lines.append(f"🧠 <b>{pending_recs} intelligence recommendation(s) pending</b>")
            msg_lines.append(f"→ http://178.104.170.58:8080/intelligence")
            msg_lines.append("")

        if queue_count > 0:
            msg_lines.append(f"⚠️ <b>{queue_count} debug items need attention</b>")
            msg_lines.append(f"→ http://178.104.170.58:8000")
        else:
            msg_lines.append(f"✅ No debug issues in queue")

        msg_lines.append(f"\n<b>Dashboard:</b> http://178.104.170.58:8080")

        send_telegram("\n".join(msg_lines), "P1")
        db_log_event("P3", "morning_briefing", "Morning briefing sent", "")
        _log_agent("Morning briefing sent")
    except Exception as e:
        _log_agent(f"Morning briefing error: {e}")


def _update_context_md():
    """Rewrite CONTEXT.md with current bot state — fully dynamic."""
    try:
        total_pnl, total_trades, win_rate = get_all_time_pnl()
        today = datetime.now(PARIS).strftime("%d-%b-%Y %H:%M")
        bot_up = is_bot_running()

        # Read live positions from positions.json (more accurate than DB)
        pos_summary = "None open"
        try:
            with open("/home/alphabot/app/positions.json") as f:
                pos_data = json.load(f)
            if pos_data:
                pos_lines = []
                for sym, p in pos_data.items():
                    qty = p.get("qty", "?")
                    entry = p.get("entry_price", 0)
                    typ = p.get("_type", "")
                    pos_lines.append(f"{sym} x{qty} @ ${entry:.2f} [{typ}]")
                pos_summary = ", ".join(pos_lines)
        except:
            pass

        # Read near-miss summary from DB
        nm_summary = ""
        try:
            conn = sqlite3.connect(DB_PATH)
            nm_score = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason='SCORE'").fetchone()[0] or 0
            nm_cap   = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason!='SCORE'").fetchone()[0] or 0
            nm_cap_top = conn.execute("""SELECT skip_reason, COUNT(*) as cnt FROM near_misses
                WHERE skip_reason!='SCORE' GROUP BY skip_reason ORDER BY cnt DESC LIMIT 3""").fetchall()
            nm_summary = f"{nm_score} score-based, {nm_cap} capacity-blocked"
            if nm_cap_top:
                nm_summary += " (top: " + ", ".join(f"{r[0]}×{r[1]}" for r in nm_cap_top) + ")"
            conn.close()
        except:
            nm_summary = "unavailable"

        # Pending intelligence recommendations
        intel_summary = ""
        try:
            conn = sqlite3.connect(DB_PATH)
            recs = conn.execute("""SELECT category, parameter, recommended_value, confidence
                FROM tuning_recommendations WHERE status='PENDING' LIMIT 3""").fetchall()
            conn.close()
            if recs:
                intel_summary = "\n".join(f"  - [{r[2]} confidence] {r[0]}: {r[1]} → {r[2]}" for r in recs)
            else:
                intel_summary = "  - None pending"
        except:
            intel_summary = "  - unavailable"

        # Load current trading config
        try:
            with open("/home/alphabot/app/trading_config.json") as f:
                tcfg = json.load(f)
            min_score = tcfg.get("MIN_SIGNAL_SCORE", 5)
            max_pos = tcfg.get("MAX_POSITIONS", 3)
            max_total = tcfg.get("MAX_TOTAL_POSITIONS", 15)
            stop_pct = tcfg.get("STOP_LOSS_PCT", 5.0)
            cycle_s = tcfg.get("CYCLE_SECONDS", 60)
        except:
            min_score, max_pos, max_total, stop_pct, cycle_s = 5, 3, 15, 5.0, 60

        content = f"""# AlphaBot Debug Agent - Persistent Context
## Last Updated
{today} Paris (auto-updated by agent)

## Architecture
- VPS: 178.104.170.58 (Hetzner), user: root, Paris = UTC+2
- Git root: /home/alphabot/app/ (branch: main)
- Bot start: bash /home/alphabot/start.sh → screen session "alphabot"
- start.sh runs: python3 -m app.main (NOT python3 app/main.py)
- Dashboard: port 8080 | Debug agent: port 8000 | Intelligence: /intelligence
- DB: /home/alphabot/app/alphabot.db
- GitHub: https://github.com/garrathholdstock-boop/alphabot

## File Structure
- app/main.py — main trading loop (6 disciplines)
- app/dashboard.py — web dashboard port 8080 + /intelligence + /analytics + /settings
- core/config.py — all config + watchlists (US_WATCHLIST includes CBRE, BIPC, VRT, ANET, EQIX)
- core/execution.py — order execution (IBKR + Binance)
- core/risk.py — risk management
- data/analytics.py — signal scoring + near-miss tracking + DB persistence
- data/database.py — DB operations (v2: trades, near_misses, rotations, tuning_recommendations, intelligence_runs)
- data/intelligence.py — weekly Claude intelligence analysis (Sunday 7pm ET + manual trigger)
- ai_debug/main.py — this agent (port 8000)
- start.sh — starts 3 screens: alphabot, dashboard, agent

## Database Tables (v2 schema)
- trades: symbol, pnl, score, adx_at_entry, macd_bullish, breakout, rs_vs_spy, news_state, regime_at_entry, vix_at_entry, exit_category, discipline
- near_misses: symbol, score, skip_reason, prices_since (JSON), pct_move, simulated_pnl_pct, mfe_pct, mae_pct, triggered, discipline
- rotations: sold_symbol, bought_symbol, rotation_type (SCORE_ROTATE|STALE_EXIT), rotation_verdict (GOOD|BAD|NEUTRAL), 24h follow-up prices
- tuning_recommendations: Claude-generated tuning actions with PENDING|APPLIED|DISMISSED|SNOOZED status
- intelligence_runs: archive of weekly intelligence analysis runs + narratives
- stock_stats: per-symbol aggregated stats
- agent_events: this agent's event log

## Config (trading_config.json — hot-reloaded every 60s)
- MIN_SIGNAL_SCORE={min_score} {'⚠️ RAISE TO 7 BEFORE GOING LIVE' if min_score < 7 else '✅'}
- IS_LIVE=false (paper trading — DUQ191770)
- MAX_POSITIONS={max_pos} per discipline, MAX_TOTAL_POSITIONS={max_total}
- CYCLE_SECONDS={cycle_s} {'⚠️ RAISE TO 300 BEFORE LIVE' if cycle_s < 300 else '✅'}
- STOP_LOSS_PCT={stop_pct}%
- Brokers: IBKR (US stocks + ASX + FTSE), Binance TESTNET (crypto)

## Bot Architecture — 6 Disciplines
1. US Stocks (state) — swing trades, US market hours
2. US Intraday (intraday_state) — 9:30am-4pm ET
3. Small Cap (smallcap_state) — US hours, filtered watchlist
4. ASX (asx_state) — 1am-7am Paris
5. FTSE (ftse_state) — 9am-5:30pm Paris
6. Crypto Intraday (crypto_intraday_state) — 24/7 Binance testnet

## Skip Reason Taxonomy (near_misses.skip_reason)
- SCORE: signal below MIN_SIGNAL_SCORE (expected)
- SECTOR_CAP: sector position limit hit (structural — consider raising MAX_SECTOR_POSITIONS)
- MAX_TOTAL_POSITIONS: global position cap (structural)
- MAX_DAILY_SPEND: daily spend limit (structural)
- CHOPPY_MARKET: choppy market regime gate
- MAX_TRADES_DAY: daily trade count cap
- MAX_EXPOSURE: portfolio exposure limit
- ORDER_FAILED: IBKR/Binance order failure (execution issue)

## Rotation Logic
- Logic 1 (SCORE_ROTATE): sells weakest held position if new signal scores 1.5+ higher AND held is in profit >0.1%
- Logic 2 (STALE_EXIT): sells flat (±0.5%) position held 30+ min to free slot
- Both logged to rotations table with 24h follow-up verdict

## Intelligence System
- Weekly run: Sunday 7pm ET (daemon thread in main.py)
- Manual trigger: /intelligence page → ⚡ Run Now (PIN-gated)
- Mandate: protect capital first, open to more trades/exposure if data supports it, never touch IS_LIVE
- Recommendations reviewed at http://178.104.170.58:8080/intelligence

## Current Status
- Bot running: {'YES ✅' if bot_up else 'NO 🔴'}
- All-time P&L: ${total_pnl:+,.2f} ({total_trades} trades, {win_rate}% win rate)
- Open positions: {pos_summary}
- Near misses total: {nm_summary}
- Pending intelligence recommendations:
{intel_summary}

## KNOWN COSMETIC ERRORS — ALWAYS IGNORE
- Error 10089, Error 300 — market data subscription, harmless
- BrokenPipeError in dashboard — client disconnected, harmless
- DeprecationWarning utcnow() — Python 3.12, cosmetic
- reqHistoricalData Timeout for SPY — harmless, retries next cycle
- Can't find EId with tickerId — harmless IBKR cosmetic
- "future belongs to a different loop" — harmless if from dashboard thread, P1 if from alphabot screen

## Priority Matrix
- P1 SAFETY: bot down, stop not firing, IBKR disconnect, kill switch, daily loss limit
- P2 EFFICIENCY: signal blocked, Binance failing, execution block, sector_cap_flood, rotation_bad_rate
- P3 BUGS: dashboard mismatches, near-miss anomalies, new unknown errors

## Pre-Live Checklist
- [ ] MIN_SIGNAL_SCORE raised to 7
- [ ] CYCLE_SECONDS raised to 300
- [ ] 2-week paper period complete
- [ ] Intelligence recommendations reviewed
- [ ] IS_LIVE=true (Garrath decision only — never automated)

## Deploy Workflow
cd /home/alphabot/app && git pull origin main
pkill -9 -f python3 && pkill -9 -f uvicorn && screen -wipe && bash /home/alphabot/start.sh

## Emergency
pkill -9 -f python3 && screen -wipe && bash /home/alphabot/start.sh
"""
        os.makedirs(os.path.dirname(CONTEXT_PATH), exist_ok=True)
        with open(CONTEXT_PATH, "w") as f:
            f.write(content)
        _log_agent("CONTEXT.md updated")
    except Exception as e:
        _log_agent(f"CONTEXT.md update error: {e}")


# ═══════════════════════════════════════════════════════════════
# EXISTING V7 HELPERS (kept intact)
# ═══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are an expert autonomous debugging agent for AlphaBot trading bot.

Read the CONTEXT section carefully - it has architecture, known errors, previous fixes.

ARCHITECTURE:
- Bot screen: "alphabot" | Dashboard: port 8080 | Debug agent: port 8000
- Files: app/main.py, app/dashboard.py, core/config.py, core/execution.py, core/risk.py
- New files: data/analytics.py, data/database.py (v2), data/intelligence.py
- DB: /home/alphabot/app/alphabot.db
- Start: bash /home/alphabot/start.sh (3 screens: alphabot, dashboard, agent)
- Brokers: IBKR (US/ASX/FTSE), Binance TESTNET (crypto)

DB TABLES (v2 schema — key new fields):
- trades: exit_category (STOP/TP/TRAIL/SIGNAL/MAXHOLD/EOD/ROTATE/STALE), regime_at_entry, adx_at_entry, discipline
- near_misses: skip_reason (SCORE|SECTOR_CAP|MAX_TOTAL_POSITIONS|MAX_DAILY_SPEND|CHOPPY_MARKET|MAX_TRADES_DAY|ORDER_FAILED), simulated_pnl_pct, mfe_pct, mae_pct
- rotations: rotation_type (SCORE_ROTATE|STALE_EXIT), rotation_verdict (GOOD|BAD|NEUTRAL)
- tuning_recommendations: status (PENDING|APPLIED|DISMISSED|SNOOZED), category, parameter, confidence
- intelligence_runs: weekly Claude analysis archive

PRIORITY MATRIX:
- P1 SAFETY (act immediately): bot down, stop not firing, IBKR disconnect, kill switch
- P2 EFFICIENCY (act promptly): no trades during market hours, Binance failing, execution blocks,
  sector_cap_flood (SECTOR_CAP skip reason firing >20× per 6h), rotation_bad_rate (>50% BAD verdicts)
- P3 BUGS (queue for review): cosmetic errors, dashboard mismatches

COMMAND SYNTAX (no smart quotes):
- grep -n searchterm /home/alphabot/app/app/main.py
- sed -n 200,250p /home/alphabot/app/app/main.py
- cat /home/alphabot/app/core/config.py
- sqlite3 /home/alphabot/app/alphabot.db "SELECT skip_reason, COUNT(*) FROM near_misses GROUP BY skip_reason"
- tail -100 /home/alphabot/app/alphabot.log

RESPONSE FORMAT (always exact):

STATUS: [INVESTIGATING | FOUND_ISSUE | FIX_PROPOSED | VERIFIED | COMPLETE]

ANALYSIS:
[2-4 sentences. Cite line numbers and actual values.]

NEXT_COMMAND:
```
command here
```

REASON:
[1 sentence]

CONTEXT_UPDATE:
[Only when COMPLETE: 1-2 sentences on what was fixed. Otherwise leave blank.]

RULES:
- One command per response
- P1 issues get diagnosed before anything else
- Never suggest git reset --hard
- Never touch .env — flag to user instead
- NO smart quotes in commands
- If COMPLETE, fill CONTEXT_UPDATE
- Prefer reading alphabot.log over screen buffer for pattern analysis
"""

COMPRESS_PROMPT = """Summarise this debugging session into under 300 words.
Include: problem being solved, what was tried, what was found, current theory.
Be specific with file names and line numbers."""

AUDIT_SYSTEM = """You are auditing the AlphaBot trading dashboard and bot health.

PRIORITY MATRIX:
P1 SAFETY — flag immediately: stop not firing, bot down, IBKR disconnected with positions open
P2 EFFICIENCY — flag prominently: no trades during market hours, Binance failing, zero signals,
  high capacity skip rate (SECTOR_CAP/MAX_TOTAL_POSITIONS firing repeatedly), rotation BAD rate >50%
P3 BUGS — note for review: dashboard mismatches, formatting issues

IGNORE COMPLETELY (known cosmetic): Error 10089, Error 300, BrokenPipeError, DeprecationWarning

MARKET HOURS (Paris time):
- US: 3:30pm-10pm Paris (Mon-Fri)
- FTSE: 9am-5:30pm Paris (Mon-Fri)
- ASX: 1am-7am Paris (Mon-Fri)
- Crypto: 24/7

DB HEALTH CHECKS (v2 schema):
- near_misses.skip_reason breakdown: SCORE = normal, high SECTOR_CAP = structural issue
- trades.exit_category: high STOP rate = stop too tight or bad entries, high TP = good
- rotations.rotation_verdict: >50% BAD = score gap threshold (1.5) too low
- tuning_recommendations: any PENDING recs = review /intelligence page
- intelligence_runs: last run < 8 days ago = healthy, older = Sunday job may have failed

AUDIT CHECKLIST:
DASHBOARD: Balance correct? P&L from DB? Live prices showing? Positions accurate?
TRADING: Any market open with 0 signals >90 mins? Positions at stops? Binance working?
DATA QUALITY: New columns populating (exit_category, regime_at_entry)? Near-miss MFE/MAE updating?
INTELLIGENCE: Any pending recommendations? Last run date?
SCORING: Give each section PASS/WARN/FAIL. Final verdict: PASS/WARN/FAIL

Be specific with numbers from the DB snapshot provided."""


def clean(s):
    return s.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'").strip()


def is_safe(cmd):
    cmd = clean(cmd)
    blocked = ['rm ', 'chmod +x', '> /', 'dd ', 'mkfs', 'reboot', 'shutdown', 'passwd', 'sudo']
    if any(b in cmd for b in blocked):
        return False
    allowed = ['grep', 'cat ', 'head', 'tail', 'sed', 'wc ', 'ls ', 'find ', 'python3',
               'ps ', 'df ', 'free ', 'screen', 'git ', 'sqlite3', 'echo ', 'env',
               'netstat', 'ss ', 'curl', 'cp ', 'pip ', 'wget ', 'bash ']
    return any(cmd.startswith(a) for a in allowed)


def run_cmd(cmd, timeout=15):
    cmd = clean(cmd)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                          cwd=APP_PATH)
        out = (r.stdout + r.stderr).strip()
        return out[:4000] if len(out) > 4000 else out
    except Exception as e:
        return f"Error: {e}"


def load_context():
    try:
        with open(CONTEXT_PATH, 'r') as f:
            return f.read()
    except:
        return "No context file found — run _update_context_md() to initialise."


def update_context(fix_summary):
    try:
        with open(CONTEXT_PATH, 'r') as f:
            content = f.read()
        now = datetime.now(PARIS).strftime("%d-%b-%Y %H:%M")
        new_line = f"\n- [{now}] {fix_summary}"
        if "## Fixes Made By Agent" in content:
            content = content.replace(
                "## Fixes Made By Agent (most recent first)",
                f"## Fixes Made By Agent (most recent first){new_line}"
            )
        with open(CONTEXT_PATH, 'w') as f:
            f.write(content)
    except:
        pass


def get_bot_context():
    log = get_screen_log(80)
    screen = run_cmd("/usr/bin/screen -ls")
    db = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 5")
        db["recent_trades"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?",
                    (f"{datetime.now().strftime('%Y-%m-%d')}%",))
        db["trades_today"] = cur.fetchone()["cnt"]
        conn.close()
    except Exception as e:
        db = {"error": str(e)}
    return log, screen, db


def get_db_snapshot():
    snap = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Core trades
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 50")
        snap["trades"] = [dict(r) for r in cur.fetchall()]
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT * FROM trades WHERE created_at LIKE ?", (f"{today}%",))
        snap["today_trades"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COALESCE(SUM(pnl),0) as total FROM trades WHERE side='SELL'")
        snap["total_pnl"] = float(cur.fetchone()["total"] or 0)
        cur.execute("SELECT COALESCE(SUM(pnl),0) as wpnl FROM trades WHERE created_at >= date('now','-30 days') AND side='SELL'")
        snap["week_pnl"] = float(cur.fetchone()["wpnl"] or 0)

        # Near misses — enhanced with new columns
        try:
            cur.execute("""SELECT symbol, score, skip_reason, created_at, pct_move,
                simulated_pnl_pct, mfe_pct, mae_pct, triggered
                FROM near_misses ORDER BY created_at DESC LIMIT 20""")
            snap["near_misses"] = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT skip_reason, COUNT(*) as cnt FROM near_misses GROUP BY skip_reason ORDER BY cnt DESC")
            snap["skip_reason_breakdown"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["near_misses"] = []
            snap["skip_reason_breakdown"] = []

        # Exit categories
        try:
            cur.execute("""SELECT exit_category, COUNT(*) as cnt, ROUND(AVG(pnl),2) as avg_pnl
                FROM trades WHERE side='SELL' AND exit_category IS NOT NULL
                GROUP BY exit_category ORDER BY cnt DESC""")
            snap["exit_categories"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["exit_categories"] = []

        # Rotation audit
        try:
            cur.execute("""SELECT rotation_type, rotation_verdict, COUNT(*) as cnt
                FROM rotations WHERE rotation_verdict IS NOT NULL
                GROUP BY rotation_type, rotation_verdict""")
            snap["rotation_summary"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["rotation_summary"] = []

        # Pending intelligence recommendations
        try:
            cur.execute("""SELECT category, action, parameter, recommended_value,
                confidence, evidence FROM tuning_recommendations
                WHERE status='PENDING' ORDER BY created_at DESC LIMIT 5""")
            snap["pending_recommendations"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["pending_recommendations"] = []

        # Latest intelligence run
        try:
            cur.execute("""SELECT run_id, rec_count, created_at, triggered_by
                FROM intelligence_runs ORDER BY created_at DESC LIMIT 1""")
            row = cur.fetchone()
            snap["latest_intelligence_run"] = dict(row) if row else None
        except:
            snap["latest_intelligence_run"] = None

        # Agent events
        try:
            cur.execute("SELECT * FROM agent_events ORDER BY created_at DESC LIMIT 10")
            snap["agent_events"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["agent_events"] = []

        conn.close()
    except Exception as e:
        snap["error"] = str(e)
    return snap


def fetch_dashboard():
    try:
        with urlopen("http://localhost:8080", timeout=8) as r:
            raw = r.read().decode('utf-8', errors='replace')
            raw = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL)
            raw = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL)
            return raw[:30000]
    except Exception as e:
        return f"DASHBOARD FETCH FAILED: {e}"


def call_claude(messages, system=None, max_tokens=1500):
    if not CLAUDE_API_KEY:
        return "ERROR: CLAUDE_API_KEY not set."
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, timeout=60.0)
        r = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=system or SYSTEM_PROMPT,
            messages=messages
        )
        return r.content[0].text
    except Exception as e:
        _log_agent(f"Claude API error: {e}")
        return f"STATUS: INVESTIGATING\n\nANALYSIS:\nClaude API error: {e}. Check CLAUDE_API_KEY in .env and retry.\n\nNEXT_COMMAND:\n```\necho $CLAUDE_API_KEY | head -c 20\n```\n\nREASON:\nVerify API key is set correctly."


def compress_history(steps, question):
    text = "\n\n".join([
        f"{'CLAUDE' if s['role']=='assistant' else 'USER/VPS'}: {s['content'] if isinstance(s['content'],str) else str(s['content'])}"
        for s in steps
    ])
    summary = call_claude(
        [{"role": "user", "content": f"PROBLEM: {question}\n\nSESSION:\n{text}"}],
        system=COMPRESS_PROMPT
    )
    return [{"role": "user", "content": f"[COMPRESSED - {len(steps)} steps so far]\n{summary}\n\nContinue: {question}"}]


def detect_stuck(steps, cmd, threshold=3):
    if not cmd:
        return False
    recent = []
    for s in reversed(steps[-10:]):
        if s["role"] == "user" and "COMMAND:" in s.get("content", ""):
            m = re.search(r'COMMAND: (.+?)\n', s["content"])
            if m:
                recent.append(clean(m.group(1)))
    return recent.count(clean(cmd)) >= threshold


def parse_response(text):
    status  = re.search(r'STATUS:\s*(\w+)', text)
    status  = status.group(1) if status else "INVESTIGATING"
    analysis = re.search(r'ANALYSIS:\s*(.*?)(?=NEXT_COMMAND:|REASON:|CONTEXT_UPDATE:|$)', text, re.DOTALL)
    analysis = analysis.group(1).strip() if analysis else ""
    command  = re.search(r'```(?:bash)?\s*(.*?)```', text, re.DOTALL)
    command  = clean(command.group(1)) if command else ""
    reason   = re.search(r'REASON:\s*(.*?)(?=CONTEXT_UPDATE:|$)', text, re.DOTALL)
    reason   = reason.group(1).strip() if reason else ""
    ctx      = re.search(r'CONTEXT_UPDATE:\s*(.*?)$', text, re.DOTALL)
    ctx      = ctx.group(1).strip() if ctx else ""
    return status, analysis, command, reason, ctx


def enc(steps):
    return base64.b64encode(json.dumps(steps).encode()).decode()


def dec(h):
    if not h:
        return []
    try:
        return json.loads(base64.b64decode(h.encode()).decode())
    except:
        return []


def get_db_display():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 5")
        trades = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?",
                    (f"{datetime.now().strftime('%Y-%m-%d')}%",))
        today = cur.fetchone()["cnt"]
        conn.close()
        return trades, today
    except:
        return [], "?"


def store(data):
    sid = str(uuid.uuid4())[:8]
    SESSIONS[sid] = data
    _base = os.environ.get("ROOT_PATH", "")
    return RedirectResponse(f"{_base}/r/{sid}", status_code=303)


# ═══════════════════════════════════════════════════════════════
# UI RENDERER
# ═══════════════════════════════════════════════════════════════
def render(analysis="", command="", reason="", status="", cmd_output="", cmd_run="",
           error="", question="", history="", complete=False, compressed=False,
           step_count=0, ctx_updated=False, audit_result="", **kwargs):

    BASE = os.environ.get("ROOT_PATH", "")  # e.g. "" direct, or "/agent" via Nginx
    screen_status = run_cmd("/usr/bin/screen -ls")
    bot_ok = SCREEN_NAME in screen_status
    sc = "#00ff88" if bot_ok else "#ef4444"
    st = "RUNNING" if bot_ok else "DOWN"
    trades, today_count = get_db_display()
    steps = dec(history)
    markets = get_market_hours()
    total_pnl, total_trades, win_rate = get_all_time_pnl()
    today_pnl = get_todays_pnl()
    recent_events = db_get_recent_events(8)

    # ── Approval queue HTML ───────────────────────────────────
    with _queue_lock:
        q_items = list(approval_queue)
    pending = [q for q in q_items if q["status"] == "pending"]
    resolved = [q for q in q_items if q["status"] != "pending"][:3]

    queue_html = ""
    if pending:
        items_html = ""
        for item in pending[:5]:
            p_color = {"P1": "#ef4444", "P2": "#f59e0b", "P3": "#64748b"}.get(item["priority"], "#64748b")
            briefing_html = ""
            if item.get("claude_briefing"):
                briefing_html = f"""
                <div style="background:#0a0a14;border:1px solid #7c3aed;border-radius:6px;padding:8px;margin:8px 0;font-size:11px;color:#a78bfa;white-space:pre-wrap;">{html.escape(item['claude_briefing'])}</div>"""

            items_html += f"""
            <div style="border:1px solid {p_color};border-radius:8px;padding:12px;margin-bottom:8px;background:#0a0a0f;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <span style="font-size:9px;font-weight:700;color:{p_color};background:rgba(0,0,0,0.4);padding:2px 8px;border-radius:10px;">{item['priority']}</span>
                <span style="font-size:10px;color:#475569;">{item['time']}</span>
              </div>
              <div style="font-size:15px;color:#e2e8f0;margin-bottom:4px;">{html.escape(item['message'])}</div>
              <div style="font-size:13px;color:#475569;margin-bottom:8px;">{html.escape(item['detail'][:100])}</div>
              {briefing_html}
              <div style="display:flex;gap:8px;">
                <form method="POST" action="{BASE}/queue/investigate" style="flex:1">
                  <input type="hidden" name="item_id" value="{item['id']}">
                  <input type="hidden" name="event_type" value="{item['event_type']}">
                  <button type="submit" style="width:100%;background:#7c3aed;border:none;border-radius:6px;color:#fff;font-size:11px;font-weight:700;padding:7px;cursor:pointer;">🔍 INVESTIGATE</button>
                </form>
                <form method="POST" action="{BASE}/queue/dismiss" style="flex:1">
                  <input type="hidden" name="item_id" value="{item['id']}">
                  <button type="submit" style="width:100%;background:#1e1e2e;border:1px solid #475569;border-radius:6px;color:#64748b;font-size:11px;font-weight:700;padding:7px;cursor:pointer;">✕ DISMISS</button>
                </form>
              </div>
            </div>"""

        queue_html = f"""
        <div style="background:#111118;border:1px solid #f59e0b;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#f59e0b;text-transform:uppercase;margin-bottom:10px;">⚠️ Approval Queue ({len(pending)} pending)</div>
          {items_html}
        </div>"""
    elif resolved:
        queue_html = f"""
        <div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:12px;margin-bottom:12px;">
          <div style="font-size:13px;font-weight:700;color:#00ff88;text-transform:uppercase;margin-bottom:6px;">✅ Queue Clear</div>
          <div style="font-size:14px;color:#475569;">No pending items.</div>
        </div>"""
    else:
        queue_html = f"""
        <div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:12px;margin-bottom:12px;">
          <div style="font-size:13px;font-weight:700;color:#00ff88;text-transform:uppercase;margin-bottom:6px;">✅ Queue Clear</div>
        </div>"""

    # ── Auto-fix log HTML ─────────────────────────────────────
    autofix_html = ""
    if auto_fixed_log:
        rows = "".join([
            f"<tr><td style='color:#475569'>{a['time']}</td><td style='color:#e2e8f0'>{a['action']}</td><td style='color:#00ff88'>{a['result']}</td></tr>"
            for a in auto_fixed_log[:5]
        ])
        autofix_html = f"""
        <div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#00ff88;text-transform:uppercase;margin-bottom:8px;">✅ Auto-Fixed Overnight</div>
          <table><tr><th>Time</th><th>Action</th><th>Result</th></tr>{rows}</table>
        </div>"""

    # ── Market status ─────────────────────────────────────────
    mkt_html = ""
    for mkt, mst in markets.items():
        if mkt in ("UTC_time", "Paris_time"):
            mkt_html += f'<span style="font-size:12px;color:#64748b;margin-right:10px;">{mst}</span>'
        else:
            c = "#00ff88" if mst == "OPEN" else "#475569"
            mkt_html += f'<span style="font-size:12px;font-weight:700;color:{c};margin-right:10px;">{mkt} {mst}</span>'

    # ── Recent events log ─────────────────────────────────────
    # ── Live event feed — always visible, colour coded ──────
    events_html = ""
    # Always show the feed, even if empty
    feed_rows = ""
    if recent_events:
        for e in recent_events:
            p = e.get("priority","P3")
            pc = {"P1":"#ef4444","P2":"#f59e0b","P3":"#475569"}.get(p,"#475569")
            bg = {"P1":"rgba(239,68,68,0.06)","P2":"rgba(245,158,11,0.06)","P3":"rgba(255,255,255,0.02)"}.get(p,"")
            icon = {"P1":"🔴","P2":"🟡","P3":"🔵"}.get(p,"⚪")
            action = e.get("action_taken","")
            action_html = f'<div style="color:#00ff88;font-size:11px;margin-top:2px;">→ {html.escape(action[:60])}</div>' if action else ""
            ts = e.get("created_at","")[:16].replace("T"," ")
            ev_type = e.get("event_type","unknown")
            safe_ev = ev_type.replace("'","").replace('"','')
            safe_msg = html.escape(e.get("message","")).replace("'","&#39;")
            safe_detail = html.escape(action).replace("'","&#39;")
            feed_rows += f'''
            <div onclick="investigateEvent('{safe_ev}','{safe_msg}','{safe_detail}')"
                 style="display:flex;align-items:flex-start;gap:10px;padding:12px 14px;background:{bg};border-left:3px solid {pc};margin-bottom:3px;border-radius:0 6px 6px 0;cursor:pointer;"
                 onmouseover="this.style.opacity='0.7'" onmouseout="this.style.opacity='1'">
              <span style="font-size:16px;flex-shrink:0;">{icon}</span>
              <div style="flex:1;min-width:0;">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
                  <span style="font-size:14px;color:#e2e8f0;font-weight:600;">{html.escape(e.get("message","")[:65])}</span>
                  <div style="display:flex;align-items:center;gap:10px;flex-shrink:0;">
                    <span style="font-size:11px;color:#475569;">{ts}</span>
                    <span style="font-size:12px;color:{pc};font-weight:700;">→ Investigate</span>
                  </div>
                </div>
                {action_html}
              </div>
            </div>'''
    else:
        feed_rows = '<div style="text-align:center;padding:24px;color:#475569;font-size:13px;">No events yet — monitor checks every 5 mins</div>'

    events_html = f"""
    <div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
        <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;">📡 Live Event Feed <span style="font-size:11px;font-weight:400;color:#475569;text-transform:none;letter-spacing:0;">· tap any row to investigate</span></div>
        <div style="display:flex;gap:12px;font-size:11px;">
          <span style="color:#ef4444;font-weight:700;">🔴 P1 Safety</span>
          <span style="color:#f59e0b;font-weight:700;">🟡 P2 Efficiency</span>
          <span style="color:#475569;font-weight:700;">🔵 P3 Bug</span>
        </div>
      </div>
      <div style="max-height:320px;overflow-y:auto;">{feed_rows}</div>
    </div>
    <form id="event-inv-form" method="POST" action="{BASE}/event/investigate" style="display:none;">
      <input type="hidden" id="eif-type" name="event_type">
      <input type="hidden" id="eif-msg" name="message">
      <input type="hidden" id="eif-detail" name="detail">
    </form>
    <script>
    function investigateEvent(ev_type, message, detail) {{
      document.getElementById('eif-type').value = ev_type;
      document.getElementById('eif-msg').value = message;
      document.getElementById('eif-detail').value = detail;
      document.getElementById('event-inv-form').submit();
    }}
    </script>"""

    # ── Trades table ──────────────────────────────────────────
    trades_html = ""
    for t in trades:
        pnl = float(t.get("pnl", 0) or 0)
        c = "#00ff88" if pnl >= 0 else "#ef4444"
        trades_html += f"<tr><td>{t.get('symbol','')}</td><td>{t.get('side','')}</td><td>${float(t.get('price',0)):.2f}</td><td style='color:{c}'>${pnl:.2f}</td><td style='color:#475569;font-size:10px'>{t.get('reason','')[:20]}</td></tr>"

    # ── Agent analysis panel ──────────────────────────────────
    sc2 = {"INVESTIGATING":"#f59e0b","FOUND_ISSUE":"#ef4444","FIX_PROPOSED":"#7c3aed",
           "VERIFIED":"#00ff88","COMPLETE":"#00ff88"}.get(status,"#64748b")
    stuck = detect_stuck(steps, command) if command else False

    agent_html = ""
    if audit_result:
        verdict_color = "#00ff88" if "PASS" in audit_result[:200] else "#f59e0b" if "WARN" in audit_result[:200] else "#ef4444"
        agent_html = f"""<div style="background:#0a0a14;border:2px solid {verdict_color};border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:{verdict_color};text-transform:uppercase;margin-bottom:10px;">Dashboard Audit</div>
          <pre style="font-size:12px;color:#e2e8f0;white-space:pre-wrap;line-height:1.6;">{html.escape(audit_result)}</pre>
        </div>"""
    elif complete:
        ctx_badge = '<div style="background:#7c3aed;color:#fff;font-size:9px;font-weight:700;padding:3px 8px;border-radius:6px;margin-top:8px;display:inline-block;">CONTEXT UPDATED</div>' if ctx_updated else ''
        agent_html = f"""<div style="background:#0a1a0f;border:2px solid #00ff88;border-radius:10px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:24px;color:#00ff88;font-weight:700;margin-bottom:8px;">✅ COMPLETE</div>
          <div style="font-size:13px;color:#94a3b8;">{html.escape(analysis)}</div>{ctx_badge}</div>"""
    elif analysis:
        badges = f'<span style="font-size:9px;color:#64748b;margin-left:8px;">Step {step_count}</span>'
        if compressed:
            badges += '<span style="font-size:9px;background:#1e1e2e;color:#64748b;padding:2px 6px;border-radius:4px;margin-left:6px;">COMPRESSED</span>'
        agent_html = f"""<div style="background:#0a1a0f;border:1px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:6px;">
            <div style="display:flex;align-items:center;"><span style="font-size:10px;font-weight:700;color:#00ff88;text-transform:uppercase;letter-spacing:1px;">Claude Analysis</span>{badges}</div>
            <div style="background:{sc2};color:#000;font-size:9px;font-weight:700;padding:3px 8px;border-radius:10px;">{status}</div>
          </div>
          <div style="font-size:13px;line-height:1.7;color:#e2e8f0;margin-bottom:12px;">{html.escape(analysis)}</div>
          {f'<div style="font-size:11px;color:#64748b;font-style:italic;margin-bottom:10px;">{html.escape(reason)}</div>' if reason else ''}
        </div>"""

        # Feedback box
        agent_html += f"""<div style="background:#0d0d1a;border:1px solid #f59e0b;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#f59e0b;text-transform:uppercase;margin-bottom:8px;">Your Feedback</div>
          <form method="POST" action="{BASE}/feedback">
            <input type="hidden" name="question" value="{html.escape(question)}">
            <input type="hidden" name="history" value="{html.escape(history)}">
            <input type="hidden" name="step_count" value="{step_count}">
            <textarea name="feedback" placeholder="e.g. Focus on FTSE, ignore Binance..." style="height:55px;"></textarea>
            <button type="submit" style="display:block;width:100%;background:#f59e0b;border:none;border-radius:6px;color:#000;font-weight:800;font-size:13px;padding:8px;cursor:pointer;margin-top:6px;">SEND FEEDBACK + CONTINUE</button>
          </form>
        </div>"""

        if command:
            if stuck:
                btn_color, btn_text = "#f59e0b", "APPROVE &amp; RUN (STUCK)"
            elif is_safe(command):
                btn_color, btn_text = "#00ff88", "APPROVE &amp; RUN"
            else:
                btn_color, btn_text = "#ef4444", "BLOCKED (unsafe)"
            disabled = "" if is_safe(command) else "disabled"

            agent_html += f"""<div style="background:#0d0d1a;border:2px solid #7c3aed;border-radius:10px;padding:16px;margin-bottom:12px;">
              <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#7c3aed;text-transform:uppercase;margin-bottom:12px;">Next Action</div>
              <div style="background:#0a0a0f;border:1px solid #1e1e2e;border-radius:8px;padding:12px;margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
                  <span style="font-size:10px;color:#64748b;font-weight:700;">COMMAND</span>
                  <button onclick="navigator.clipboard.writeText(document.getElementById('cmdbox').textContent)" style="background:#1e1e2e;border:none;color:#94a3b8;font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;">COPY</button>
                </div>
                <pre id="cmdbox" style="color:#00ff88;margin:0;font-size:12px;white-space:pre-wrap;">{html.escape(command)}</pre>
              </div>
              <form method="POST" action="{BASE}/approve">
                <input type="hidden" name="command" value="{html.escape(command)}">
                <input type="hidden" name="question" value="{html.escape(question)}">
                <input type="hidden" name="history" value="{html.escape(history)}">
                <input type="hidden" name="step_count" value="{step_count}">
                {'<input type="hidden" name="stuck" value="1">' if stuck else ''}
                <button type="submit" {disabled} style="display:block;width:100%;background:{btn_color};border:none;border-radius:8px;color:#000;font-weight:800;font-size:15px;padding:12px;cursor:pointer;">{btn_text}</button>
              </form>
            </div>"""

    # ── History ───────────────────────────────────────────────
    hist_html = ""
    if steps:
        items = ""
        for i, s in enumerate(steps):
            rc = "#00ff88" if s["role"]=="assistant" else "#7c3aed"
            rl = "Claude" if s["role"]=="assistant" else "Input"
            txt = s["content"] if isinstance(s["content"],str) else str(s["content"])
            items += f"""<div style="border-left:2px solid {rc};padding:8px 12px;margin-bottom:6px;background:#0a0a0f;border-radius:0 6px 6px 0;">
              <div style="font-size:9px;color:{rc};font-weight:700;text-transform:uppercase;margin-bottom:3px;">Step {i+1} — {rl}</div>
              <pre style="font-size:10px;color:#475569;white-space:pre-wrap;">{html.escape(txt[:400])}{'...' if len(txt)>400 else ''}</pre>
            </div>"""
        hist_html = f"""<div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <details><summary style="cursor:pointer;font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;">Session History ({len(steps)} steps)</summary>
          <div style="margin-top:10px;">{items}</div></details></div>"""

    # ── Quick actions ─────────────────────────────────────────
    quick_btns = [
        ("🏥 Bot Health",      "Is the bot running and healthy? Check all 3 screens (alphabot, dashboard, agent), IBKR connection, and current positions."),
        ("📊 Positions",       "Check current open positions from positions.json, their P&L, hold time, and stop distances."),
        ("🚨 Real Errors",     "Find any real errors in alphabot.log from the last 6 hours — ignore known cosmetics listed in CONTEXT.md."),
        ("🎯 Near Misses",     "Analyse near_misses table — what's the skip_reason breakdown? Are we getting blocked by SECTOR_CAP or MAX_TOTAL_POSITIONS more than SCORE?"),
        ("💰 Trading Check",   "Is the bot trading efficiently? Check exit_category distribution — too many STOPs vs TPs? Any execution blocks?"),
        ("🔄 Rotation Audit",  "Check rotations table — what's the GOOD/BAD/NEUTRAL breakdown? Is the score gap threshold of 1.5 appropriate?"),
        ("🧠 Intelligence",    "Check tuning_recommendations table for PENDING items. Has the weekly intelligence run fired? Check intelligence_runs table."),
        ("📈 Log Patterns",    "Analyse alphabot.log for repeated patterns — are any skip reasons flooding? Any ORDER FAILED spikes? Cycle timing issues?"),
        ("🔑 Binance Fix",     "Check BINANCE_SECRET in .env — it may be corrupted. Diagnose any -2010 error."),
        ("⚡ Pre-Live Check",  "Review the pre-live checklist: MIN_SIGNAL_SCORE at 7? CYCLE_SECONDS at 300? 2-week paper period done? Intelligence reviewed?"),
    ]
    quick = ""
    for label, q in quick_btns:
        quick += f"""<form method="POST" action="{BASE}/ask" style="display:inline-block;margin:3px;">
          <input type="hidden" name="question" value="{html.escape(q)}">
          <button type="submit" style="background:#111118;border:1px solid #1e1e2e;color:#94a3b8;font-family:'JetBrains Mono',monospace;font-size:13px;padding:10px 14px;border-radius:6px;cursor:pointer;">{label}</button>
        </form>"""

    quick += """<form method="POST" action="{BASE}/audit" style="display:inline-block;margin:3px;">
      <button type="submit" style="background:#0a1a0f;border:2px solid #00ff88;color:#00ff88;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;font-weight:700;">🔍 Full Audit</button>
    </form>"""

    quick += """<form method="POST" action="{BASE}/morning" style="display:inline-block;margin:3px;">
      <button type="submit" style="background:#0a0a1a;border:2px solid #7c3aed;color:#a78bfa;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;font-weight:700;">☀️ Morning Brief</button>
    </form>"""

    quick += """<a href="{_base}/maintenance" style="display:inline-block;margin:3px;text-decoration:none;">
      <button style="background:#0a1020;border:2px solid #f59e0b;color:#f59e0b;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;font-weight:700;">🔧 Maintenance</button>
    </a>"""

    quick += """<a href="{_base}/log" style="display:inline-block;margin:3px;text-decoration:none;">
      <button style="background:#0a1a0f;border:2px solid #00ff88;color:#00ff88;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;font-weight:700;">📋 Live Log</button>
    </a>"""

    safe_files = ["app/main.py","app/dashboard.py","core/config.py","core/execution.py",
                  "core/risk.py","data/analytics.py","data/database.py","data/intelligence.py",
                  "ai_debug/main.py","start.sh",".env"]
    cmd_html = ""
    if cmd_output:
        cmd_html = f"""<div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Output: {html.escape(cmd_run)}</div>
          <pre style="color:#94a3b8;font-size:11px;max-height:250px;overflow-y:auto;white-space:pre-wrap;">{html.escape(cmd_output)}</pre>
        </div>"""

    err_html = f'<div style="background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;padding:12px;margin-bottom:12px;color:#ef4444;font-size:13px;">{html.escape(error)}</div>' if error else ""

    file_btns = "".join([f"""<form method="POST" action="{BASE}/file" style="display:inline-block;margin:2px;">
      <input type="hidden" name="filename" value="{f}">
      <input type="hidden" name="question" value="{html.escape(question)}">
      <input type="hidden" name="history" value="{html.escape(history)}">
      <button type="submit" style="background:#0a0a14;border:1px solid #1e1e2e;color:#64748b;font-family:'JetBrains Mono',monospace;font-size:12px;padding:8px 12px;border-radius:5px;cursor:pointer;">{f}</button>
    </form>""" for f in safe_files])

    telegram_status = "✅ Connected" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID != "YOUR_CHAT_ID") else "⚠️ Token set — add TELEGRAM_CHAT_ID to .env"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- auto-refresh via JS to avoid Safari form interference -->
<title>AlphaBot Agent v8</title>
<script>var BASE="{BASE}";</script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0a0a0f; color:#e2e8f0; font-family:'JetBrains Mono',monospace; padding:16px; max-width:1000px; font-size:15px; margin:0 auto; }}
header {{ display:flex; align-items:center; gap:12px; margin-bottom:16px; padding-bottom:14px; border-bottom:1px solid #1e1e2e; flex-wrap:wrap; position:sticky; top:0; background:#0a0a0f; z-index:100; padding-top:8px; }}
.logo {{ font-family:'Syne',sans-serif; font-size:26px; font-weight:800; color:#00ff88; }}
.badge {{ margin-left:auto; background:#111118; border:1px solid {sc}; color:{sc}; font-size:13px; font-weight:700; padding:6px 14px; border-radius:20px; }}
.card {{ background:#111118; border:1px solid #1e1e2e; border-radius:10px; padding:14px; margin-bottom:12px; }}
textarea {{ width:100%; background:#0a0a0f; border:1px solid #1e1e2e; border-radius:8px; color:#e2e8f0; font-family:'JetBrains Mono',monospace; font-size:15px; padding:12px 14px; resize:none; height:80px; }}
textarea:focus {{ outline:none; border-color:#00ff88; }}
.ask-btn {{ display:block; width:100%; background:#00ff88; border:none; border-radius:8px; color:#000; font-family:'Syne',sans-serif; font-weight:800; font-size:17px; padding:14px; cursor:pointer; margin-top:8px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ color:#64748b; text-align:left; padding:4px 6px; border-bottom:1px solid #1e1e2e; font-size:11px; text-transform:uppercase; letter-spacing:1px; }}
td {{ padding:5px 6px; border-bottom:1px solid #0f0f18; }}
details summary {{ cursor:pointer; }}
</style>
</head>
<body>

<header>
  <div>
    <div class="logo">AlphaBot <span style="color:#64748b">Agent</span> <span style="font-size:11px;color:#7c3aed;background:#1e1e2e;padding:2px 6px;border-radius:4px;">v8</span></div>
    <div style="font-size:10px;color:#475569;margin-top:2px;">{mkt_html}</div>
  </div>
  <div class="badge">BOT {st}</div>
</header>

<!-- Stats strip -->
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;">
  <div class="card" style="padding:10px 12px;">
    <div style="font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:1px;">All-time P&L</div>
    <div style="font-size:24px;font-weight:700;color:{'#00ff88' if total_pnl>=0 else '#ef4444'};font-family:'Syne',sans-serif;">${total_pnl:+,.0f}</div>
    <div style="font-size:13px;color:#475569;">{total_trades} trades · {win_rate}% win</div>
  </div>
  <div class="card" style="padding:10px 12px;">
    <div style="font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:1px;">Today P&L</div>
    <div style="font-size:24px;font-weight:700;color:{'#00ff88' if today_pnl>=0 else '#ef4444'};font-family:'Syne',sans-serif;">${today_pnl:+,.0f}</div>
    <div style="font-size:13px;color:#475569;">Trades today: {today_count}</div>
  </div>
  <div class="card" style="padding:10px 12px;">
    <div style="font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:1px;">Queue</div>
    <div style="font-size:24px;font-weight:700;color:{'#f59e0b' if pending else '#00ff88'};font-family:'Syne',sans-serif;">{len(pending)}</div>
    <div style="font-size:13px;color:#475569;">items pending</div>
  </div>
  <div class="card" style="padding:10px 12px;">
    <div style="font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:1px;">Telegram</div>
    <div style="font-size:14px;font-weight:700;color:#{'00ff88' if TELEGRAM_CHAT_ID != 'YOUR_CHAT_ID' else 'f59e0b'};margin-top:4px;">{telegram_status}</div>
  </div>
</div>

{queue_html}
{autofix_html}
{err_html}
{agent_html}
{cmd_html}
{hist_html}

<!-- Ask Claude -->
<div class="card">
  <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Ask Claude</div>
  <form method="POST" action="{BASE}/ask" id="ask-form" onsubmit="showThinking()">
    <textarea name="question" id="ask-q" placeholder="What's going on with the bot? Why aren't there any trades?...">{html.escape(question) if not complete and not audit_result else ''}</textarea>
    <button type="submit" id="ask-btn" class="ask-btn">ASK CLAUDE →</button>
  </form>
</div>
<script>
function showThinking() {{
  var btn = document.getElementById('ask-btn');
  var q = document.getElementById('ask-q').value.trim();
  if (!q) {{ return false; }}
  btn.textContent = '⏳ Thinking... (10-20 seconds)';
  btn.style.background = '#1e1e2e';
  btn.style.color = '#00ff88';
  btn.disabled = false;
  return true;
}}
</script>

<!-- Quick actions -->
<div class="card">
  <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Quick Actions</div>
  {quick}
</div>

<!-- File viewer -->
<div class="card">
  <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">View File</div>
  {file_btns}
</div>

<!-- Live event feed — below the fold, reference only -->
{events_html}

<!-- Auto-fix log -->


<!-- Recent trades -->
<div class="card">
  <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Recent Trades</div>
  <table><tr><th>Symbol</th><th>Side</th><th>Price</th><th>P&L</th><th>Reason</th></tr>{trades_html}</table>
</div>

<div style="text-align:center;margin-top:20px;font-size:12px;color:#1e1e2e;">
  Auto-refreshes every 60s · <a href="http://178.104.170.58:8080" style="color:#1e1e2e;">Dashboard →</a>
</div>
<script>
// Only auto-refresh on home page, not on result pages
var _isResult = window.location.pathname.startsWith('/r/');
if (!_isResult) {{
  var _t=60;
  setInterval(function(){{
    _t--;
    if(_t<=0){{ window.location.reload(); }}
  }},1000);
}}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def startup():
    init_agent_db()
    ensure_log_file()
    _update_context_md()
    t = threading.Thread(target=run_monitor, daemon=True)
    t.start()
    _log_agent("Agent v8 started — monitor thread running")


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def index(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return render()


@app.get("/r/{sid}", response_class=HTMLResponse)
async def result(sid: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    data = SESSIONS.get(sid, {})
    return render(**data)


@app.post("/ask")
async def ask(question: str = Form("")):
    if not question.strip():
        return RedirectResponse(os.environ.get("ROOT_PATH","")+"/", status_code=303)
    log, screen, db = get_bot_context()
    context = load_context()
    messages = [{"role": "user", "content": f"CONTEXT:\n{context}\n\nLIVE LOGS:\n{log}\n\nSCREEN:\n{screen}\n\nDB:\n{json.dumps(db, default=str)[:800]}\n\nPROBLEM: {question}"}]
    response_text = call_claude(messages)
    status, analysis, command, reason, ctx_update = parse_response(response_text)
    steps = messages + [{"role": "assistant", "content": response_text}]
    complete = status == "COMPLETE"
    if complete and ctx_update:
        update_context(ctx_update)
    return store(dict(analysis=analysis, command=command, reason=reason, status=status,
                      question=question, history=enc(steps), complete=complete,
                      step_count=1, ctx_updated=bool(ctx_update and complete)))


@app.post("/feedback")
async def feedback(feedback: str = Form(""), question: str = Form(""),
                   history: str = Form(""), step_count: int = Form(0)):
    if not feedback.strip():
        return RedirectResponse(os.environ.get("ROOT_PATH","")+"/", status_code=303)
    steps = dec(history)
    steps.append({"role": "user", "content": f"USER FEEDBACK (prioritise this): {feedback}"})
    step_count += 1
    compressed = False
    if step_count % MAX_STEPS_BEFORE_COMPRESS == 0:
        steps = compress_history(steps, question)
        compressed = True
    response_text = call_claude(steps)
    status, analysis, command, reason, ctx_update = parse_response(response_text)
    steps.append({"role": "assistant", "content": response_text})
    complete = status == "COMPLETE"
    if complete and ctx_update:
        update_context(ctx_update)
    return store(dict(analysis=analysis, command=command, reason=reason, status=status,
                      question=question, history=enc(steps), complete=complete,
                      step_count=step_count, compressed=compressed,
                      ctx_updated=bool(ctx_update and complete)))


@app.post("/approve")
async def approve(command: str = Form(""), question: str = Form(""),
                  history: str = Form(""), step_count: int = Form(0),
                  stuck: str = Form("")):
    command = clean(command)
    steps = dec(history)
    if not is_safe(command):
        return store(dict(error=f"Command blocked: {command}", question=question,
                          history=history, step_count=step_count))
    output = run_cmd(command)
    step_count += 1
    note = "\n\n[NOTE: Same command tried multiple times — try a completely different approach.]" if stuck else ""
    steps.append({"role": "user", "content": f"COMMAND: {command}\nOUTPUT:\n{output}{note}"})
    compressed = False
    if step_count % MAX_STEPS_BEFORE_COMPRESS == 0:
        steps = compress_history(steps, question)
        compressed = True
    response_text = call_claude(steps)
    status, analysis, next_cmd, reason, ctx_update = parse_response(response_text)
    steps.append({"role": "assistant", "content": response_text})
    complete = status == "COMPLETE"
    if complete and ctx_update:
        update_context(ctx_update)
    return store(dict(analysis=analysis, command=next_cmd, reason=reason, status=status,
                      cmd_output=output, cmd_run=command, question=question,
                      history=enc(steps), complete=complete, step_count=step_count,
                      compressed=compressed, ctx_updated=bool(ctx_update and complete)))


@app.post("/audit")
async def audit_dashboard():
    dash_html = fetch_dashboard()
    db_snap = get_db_snapshot()
    log_screen = get_screen_log(80)
    log_file   = get_log_file(lines=200, hours=6)  # real log — last 6h
    markets = get_market_hours()
    audit_input = f"""CURRENT TIME: {markets['UTC_time']} / {markets['Paris_time']}

MARKET STATUS:
- US: {markets['US']} (3:30pm-10pm Paris)
- FTSE: {markets['FTSE']} (9am-5:30pm Paris)
- ASX: {markets['ASX']} (1am-7am Paris)
- Crypto: {markets['CRYPTO']}

DATABASE SNAPSHOT (v2 schema):
{json.dumps(db_snap, default=str, indent=2)[:4000]}

BOT SCREEN LOG (last 80 lines):
{log_screen}

ALPHABOT.LOG (last 6h, last 200 lines):
{log_file[:3000]}

DASHBOARD HTML:
{dash_html[:12000]}

Perform comprehensive audit using the v2 schema knowledge. Flag P1 first, P2 second, P3 last.
Pay special attention to: skip_reason breakdown, exit_category distribution, rotation verdicts, pending intelligence recommendations."""

    result = call_claude([{"role": "user", "content": audit_input}], system=AUDIT_SYSTEM)
    return store(dict(audit_result=result, question="Dashboard Audit"))


@app.post("/morning")
async def morning_brief():
    """Manually trigger morning briefing."""
    now_paris = datetime.now(PARIS)
    _send_morning_briefing(now_paris)
    return store(dict(analysis="Morning briefing sent to Telegram.", status="COMPLETE",
                      question="Morning Brief", complete=True))


@app.post("/queue/investigate")
async def queue_investigate(item_id: str = Form(""), event_type: str = Form("")):
    """Turn a queue item into a debug session."""
    with _queue_lock:
        for item in approval_queue:
            if item["id"] == item_id:
                item["status"] = "investigating"
                break
    q_map = {
        "bot_down":          "Bot is down or was down. Diagnose and fix.",
        "binance_failing":   "Binance orders failing with -2010. Check BINANCE_SECRET in .env and diagnose.",
        "no_trades_90min":   "No trades in 90+ mins despite markets being open. Find the execution blocker.",
        "zero_scans":        "A market is open but showing 0 qualified signals. Diagnose the scan issue.",
        "execution_block":   "Order execution is failing. Diagnose the place_order error.",
        "stop_not_firing":   "Position P&L suggests stop-loss may not be firing. Check stop logic.",
    }
    question = q_map.get(event_type, f"Investigate: {event_type}")
    log, screen, db = get_bot_context()
    context = load_context()
    messages = [{"role": "user", "content": f"CONTEXT:\n{context}\n\nLIVE LOGS:\n{log}\n\nSCREEN:\n{screen}\n\nDB:\n{json.dumps(db, default=str)[:800]}\n\nPROBLEM: {question}"}]
    response_text = call_claude(messages)
    status, analysis, command, reason, ctx_update = parse_response(response_text)
    steps = messages + [{"role": "assistant", "content": response_text}]
    return store(dict(analysis=analysis, command=command, reason=reason, status=status,
                      question=question, history=enc(steps), complete=False, step_count=1))


@app.post("/queue/dismiss")
async def queue_dismiss(item_id: str = Form("")):
    with _queue_lock:
        for item in approval_queue:
            if item["id"] == item_id:
                item["status"] = "dismissed"
                break
    return RedirectResponse(os.environ.get("ROOT_PATH","")+"/", status_code=303)


@app.post("/event/investigate")
async def event_investigate(event_type: str = Form(""), message: str = Form(""), detail: str = Form("")):
    """Launch a Claude debug session directly from a live event feed tap."""
    q_map = {
        "bot_down":           "Bot is down or was down recently. Diagnose and fix — check screen session and restart if needed.",
        "binance_failing":    "Binance orders failing with -2010 (insufficient balance) or -1013 (lot size). Check BINANCE_SECRET in .env and diagnose the exact failure.",
        "no_trades_90min":    "No trades in 90+ mins despite markets being open. Find the execution blocker — check regime, MIN_SIGNAL_SCORE, VWAP filter, position caps.",
        "zero_scans":         "A market is open but showing 0 qualified signals every cycle. Diagnose why no stocks are qualifying — check signal scoring and market hours logic.",
        "execution_block":    "Order execution is failing. Diagnose the place_order error — check IBKR connection and order parameters.",
        "stop_not_firing":    "Position P&L suggests stop-loss may not be firing correctly. Check stop logic in main.py.",
        "no_trades_90min":    "Markets open but no trades for 90+ minutes. Check: regime mode, MIN_SIGNAL_SCORE threshold, VWAP filter, position caps all full?",
    }
    question = q_map.get(event_type, f"Investigate this event: {message}. Detail: {detail}")
    log, screen, db = get_bot_context()
    context = load_context()
    messages = [{"role": "user", "content": f"CONTEXT:\n{context}\n\nLIVE LOGS:\n{log}\n\nSCREEN:\n{screen}\n\nDB:\n{json.dumps(db, default=str)[:800]}\n\nEVENT TRIGGERED: {message}\n\nPROBLEM: {question}"}]
    response_text = call_claude(messages)
    status, analysis, command, reason, ctx_update = parse_response(response_text)
    steps = messages + [{"role": "assistant", "content": response_text}]
    complete = status == "COMPLETE"
    if complete and ctx_update:
        update_context(ctx_update)
    return store(dict(analysis=analysis, command=command, reason=reason, status=status,
                      question=question, history=enc(steps), complete=complete, step_count=1,
                      ctx_updated=bool(ctx_update and complete)))


@app.post("/file")
async def view_file(filename: str = Form(""), question: str = Form(""), history: str = Form("")):
    safe_files = ["app/main.py","app/dashboard.py","core/config.py","core/execution.py",
                  "core/risk.py","data/analytics.py","data/database.py","start.sh",".env"]
    if filename not in safe_files:
        return store(dict(error=f"File not allowed: {filename}", question=question, history=history))
    path = os.path.join(APP_PATH, filename)
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"Error: {e}"
    log, screen, db = get_bot_context()
    context = load_context()
    question_with_file = f"File contents of {filename}:\n\n{content[:8000]}\n\n{question or 'Analyse this file for issues.'}"
    messages = [{"role": "user", "content": f"CONTEXT:\n{context}\n\nPROBLEM: {question_with_file}"}]
    response_text = call_claude(messages)
    status, analysis, command, reason, ctx_update = parse_response(response_text)
    steps = messages + [{"role": "assistant", "content": response_text}]
    return store(dict(analysis=analysis, command=command, reason=reason, status=status,
                      question=question or filename, history=enc(steps), step_count=1))


# ═══════════════════════════════════════════════════════════════
# MAINTENANCE SYSTEM
# ═══════════════════════════════════════════════════════════════
BACKUP_ROOT  = "/home/alphabot/backups"
MAINT_PIN    = os.environ.get("KILL_PIN", os.environ.get("MAINT_PIN", "1234"))

FILES_TO_BACKUP = [
    ("app/main.py",           "main.py"),
    ("app/dashboard.py",      "dashboard.py"),
    ("data/analytics.py",     "analytics.py"),
    ("data/database.py",      "database.py"),
    ("data/intelligence.py",  "intelligence.py"),
    ("core/config.py",        "config.py"),
    ("ai_debug/main.py",      "agent_main.py"),
    ("trading_config.json",   "trading_config.json"),
    ("alphabot.db",           "alphabot.db"),
]


def _do_backup():
    """Create a dated backup folder and copy all key files. Returns (folder, results)."""
    import shutil
    date_str   = datetime.now(PARIS).strftime("%Y%m%d")
    backup_dir = os.path.join(BACKUP_ROOT, date_str)
    os.makedirs(backup_dir, exist_ok=True)
    results = []
    for src_rel, dst_name in FILES_TO_BACKUP:
        src = os.path.join(APP_PATH, src_rel)
        dst = os.path.join(backup_dir, dst_name)
        try:
            shutil.copy2(src, dst)
            size = os.path.getsize(dst)
            results.append({"file": dst_name, "ok": True,
                            "size": f"{size/1024:.1f} KB", "path": dst})
        except Exception as e:
            results.append({"file": dst_name, "ok": False, "error": str(e), "path": dst})
    return backup_dir, results


def _list_backups():
    """Return list of backup folders with metadata."""
    import glob
    backups = []
    try:
        dirs = sorted(glob.glob(os.path.join(BACKUP_ROOT, "????????")), reverse=True)
        for d in dirs:
            date_str = os.path.basename(d)
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                label = dt.strftime("%A %d %B %Y")
            except:
                label = date_str
            files = os.listdir(d) if os.path.isdir(d) else []
            total_size = sum(
                os.path.getsize(os.path.join(d, f))
                for f in files if os.path.isfile(os.path.join(d, f))
            )
            backups.append({
                "date_str": date_str,
                "label":    label,
                "path":     d,
                "files":    len(files),
                "size_mb":  round(total_size / 1024 / 1024, 2),
            })
    except Exception as e:
        _log_agent(f"_list_backups error: {e}")
    return backups


def _do_monday_check():
    """Run the pre-Monday readiness check. Returns list of (status, message) tuples."""
    checks = []

    # 1. All 3 screens alive?
    screens = run_cmd("/usr/bin/screen -ls")
    for name in ["alphabot", "dashboard", "agent"]:
        if name in screens:
            checks.append(("✅", f"Screen '{name}' is running"))
        else:
            checks.append(("🔴", f"Screen '{name}' is DOWN — restart needed"))

    # 2. Last log entry recent?
    try:
        log_text = get_log_file(lines=5)
        if log_text.strip():
            checks.append(("✅", "Bot log has recent activity"))
        else:
            checks.append(("⚠️", "Bot log appears empty — check alphabot screen"))
    except:
        checks.append(("⚠️", "Could not read bot log"))

    # 3. No P1 events in last 24h?
    try:
        conn = sqlite3.connect(DB_PATH)
        p1_count = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE priority='P1' "
            "AND created_at >= datetime('now','-24 hours')"
        ).fetchone()[0] or 0
        conn.close()
        if p1_count == 0:
            checks.append(("✅", "No P1 safety events in last 24 hours"))
        else:
            checks.append(("🔴", f"{p1_count} P1 safety event(s) in last 24h — review before Monday"))
    except:
        checks.append(("⚠️", "Could not check P1 event history"))

    # 4. MIN_SIGNAL_SCORE check
    try:
        with open(os.path.join(APP_PATH, "trading_config.json")) as f:
            cfg_j = json.load(f)
        score = cfg_j.get("MIN_SIGNAL_SCORE", 5)
        is_live = cfg_j.get("IS_LIVE", False)
        if is_live and score < 7:
            checks.append(("🔴", f"IS_LIVE=true but MIN_SIGNAL_SCORE={score} — raise to 7 before live!"))
        elif score < 7:
            checks.append(("⚠️", f"MIN_SIGNAL_SCORE={score} — remember to raise to 7 before going live"))
        else:
            checks.append(("✅", f"MIN_SIGNAL_SCORE={score} ✓"))
        checks.append(("ℹ️", f"IS_LIVE={'true ⚠️' if is_live else 'false ✓ (paper trading)'}"))
    except:
        checks.append(("⚠️", "Could not read trading_config.json"))

    # 5. Pending intelligence recommendations?
    try:
        conn = sqlite3.connect(DB_PATH)
        pending = conn.execute(
            "SELECT COUNT(*) FROM tuning_recommendations WHERE status='PENDING'"
        ).fetchone()[0] or 0
        conn.close()
        if pending > 0:
            checks.append(("⚠️", f"{pending} intelligence recommendation(s) pending — review at :8080/intelligence"))
        else:
            checks.append(("✅", "No pending intelligence recommendations"))
    except:
        checks.append(("⚠️", "Could not check intelligence recommendations"))

    # 6. Open positions — just list them
    try:
        with open(os.path.join(APP_PATH, "positions.json")) as f:
            pos = json.load(f)
        if pos:
            syms = ", ".join(pos.keys())
            checks.append(("ℹ️", f"Open positions going into Monday: {syms}"))
        else:
            checks.append(("ℹ️", "No open positions — starting Monday flat"))
    except:
        checks.append(("ℹ️", "Could not read positions.json"))

    # 7. Friday backup exists?
    backups = _list_backups()
    if backups:
        latest = backups[0]
        checks.append(("✅", f"Latest backup: {latest['label']} ({latest['size_mb']} MB, {latest['files']} files)"))
    else:
        checks.append(("⚠️", "No backups found — run Friday Backup before making changes"))

    return checks


def _build_maintenance_page(msg=None, msg_type="ok", backup_result=None,
                             monday_result=None, backups=None):
    """Build the full maintenance page HTML."""
    _base = os.environ.get("ROOT_PATH","")
    now = datetime.now(PARIS).strftime("%A %d %B %Y · %H:%M Paris")

    msg_html = ""
    if msg:
        col = "#00ff88" if msg_type == "ok" else "#ef4444"
        ico = "✅" if msg_type == "ok" else "❌"
        msg_html = f'<div style="background:{col}18;border:1px solid {col}44;border-radius:10px;padding:14px 18px;margin-bottom:20px;color:{col};font-weight:700">{ico} {html.escape(msg)}</div>'

    # ── Friday Backup result ──────────────────────────────────
    backup_html = ""
    if backup_result:
        folder, results = backup_result
        rows = ""
        for r in results:
            ok_col = "#00ff88" if r["ok"] else "#ef4444"
            ok_ico = "✅" if r["ok"] else "❌"
            detail = r.get("size", r.get("error", ""))
            rows += f'<tr><td style="color:{ok_col}">{ok_ico} {r["file"]}</td><td style="color:#94a3b8;font-size:11px">{detail}</td></tr>'
        backup_html = f"""
        <div style="background:#0a1a0f;border:1px solid rgba(0,255,136,0.3);border-radius:12px;padding:20px;margin-bottom:20px">
          <div style="font-size:15px;font-weight:700;color:#00ff88;margin-bottom:4px">✅ Backup Complete</div>
          <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;font-family:'JetBrains Mono',monospace">{html.escape(folder)}</div>
          <table><thead><tr><th>File</th><th>Size</th></tr></thead><tbody>{rows}</tbody></table>
          <div style="font-size:12px;color:#94a3b8;margin-top:12px">
            Verify in Termius: <code style="color:#00ff88">ls -lh {html.escape(folder)}/</code>
          </div>
        </div>"""

    # ── Monday Check result ───────────────────────────────────
    monday_html = ""
    if monday_result:
        rows = ""
        all_ok = all(s in ("✅", "ℹ️") for s, _ in monday_result)
        for status, msg_item in monday_result:
            col = "#00ff88" if status == "✅" else "#ef4444" if status == "🔴" else "#f59e0b" if status == "⚠️" else "#94a3b8"
            rows += f'<tr><td style="font-size:16px;width:28px">{status}</td><td style="color:{col}">{html.escape(msg_item)}</td></tr>'
        verdict_col = "#00ff88" if all_ok else "#ef4444"
        verdict_txt = "READY FOR MONDAY ✅" if all_ok else "ACTION REQUIRED BEFORE MONDAY 🔴"
        monday_html = f"""
        <div style="background:#0a0a1a;border:1px solid {verdict_col}44;border-radius:12px;padding:20px;margin-bottom:20px">
          <div style="font-size:15px;font-weight:700;color:{verdict_col};margin-bottom:14px">{verdict_txt}</div>
          <table><tbody>{rows}</tbody></table>
        </div>"""

    # ── Backup list ───────────────────────────────────────────
    if backups is None:
        backups = _list_backups()
    backup_list_html = ""
    if backups:
        rows = ""
        for b in backups:
            rows += (
                f'<tr>'
                f'<td style="color:#e0e0e0;font-weight:700">{html.escape(b["label"])}</td>'
                f'<td style="color:#94a3b8;font-family:\'JetBrains Mono\',monospace;font-size:11px">{b["path"]}</td>'
                f'<td style="color:#00aaff">{b["files"]} files</td>'
                f'<td style="color:#ffcc00">{b["size_mb"]} MB</td>'
                f'<td>'
                f'<button onclick="restoreBackup(\'{html.escape(b["date_str"])}\')" '
                f'style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);'
                f'border-radius:6px;color:#ef4444;font-size:11px;padding:4px 10px;cursor:pointer">'
                f'↩ Restore</button>'
                f'</td>'
                f'</tr>'
            )
        backup_list_html = f"""
        <div class="card" style="margin-bottom:20px">
          <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#f59e0b;text-transform:uppercase;margin-bottom:12px">🗂 Backup Archive</div>
          <div style="font-size:12px;color:#94a3b8;margin-bottom:12px">
            These folders live at <code style="color:#00ff88">/home/alphabot/backups/</code> on the VPS.
            Verify any time in Termius with <code style="color:#00ff88">ls -lh /home/alphabot/backups/</code>
          </div>
          <div style="overflow-x:auto">
            <table><thead><tr><th>Date</th><th>Path</th><th>Files</th><th>Size</th><th>Action</th></tr></thead>
            <tbody>{rows}</tbody></table>
          </div>
        </div>"""
    else:
        backup_list_html = """
        <div class="card" style="margin-bottom:20px">
          <div style="font-size:13px;font-weight:700;color:#f59e0b;margin-bottom:8px">🗂 Backup Archive</div>
          <div style="color:#94a3b8;font-size:13px">No backups yet — run Friday Backup to create the first one.</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaBot Maintenance</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0a0a0f; color:#e2e8f0; font-family:'JetBrains Mono',monospace; padding:16px; max-width:1000px; font-size:15px; margin:0 auto; }}
.card {{ background:#111118; border:1px solid #1e1e2e; border-radius:10px; padding:18px; margin-bottom:16px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ color:#94a3b8; text-align:left; padding:6px 8px; border-bottom:1px solid #1e1e2e; font-size:11px; text-transform:uppercase; letter-spacing:1px; }}
td {{ padding:8px 8px; border-bottom:1px solid #0f0f18; }}
.btn {{ display:inline-block;padding:12px 22px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;cursor:pointer;border:none;text-align:center; }}
#pin-overlay {{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:999;align-items:center;justify-content:center}}
#pin-overlay.visible {{display:flex}}
.pin-box {{background:#111118;border:1px solid rgba(245,158,11,0.4);border-radius:16px;padding:32px 36px;text-align:center;max-width:360px;width:90%}}
</style>
</head>
<body>

<div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #1e1e2e;flex-wrap:wrap">
  <div>
    <div style="font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:#f59e0b">🔧 AlphaBot <span style="color:#94a3b8">Maintenance</span></div>
    <div style="font-size:11px;color:#94a3b8;margin-top:2px">{now}</div>
  </div>
  <a href="/" style="margin-left:auto;color:#94a3b8;text-decoration:none;font-size:13px">← Back to Agent</a>
</div>

{msg_html}
{backup_html}
{monday_html}

<!-- Action buttons -->
<div class="card" style="margin-bottom:20px;border-color:rgba(245,158,11,0.2)">
  <div style="font-size:13px;font-weight:700;letter-spacing:1px;color:#f59e0b;text-transform:uppercase;margin-bottom:16px">🛠 Maintenance Actions</div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">

    <!-- Friday Backup -->
    <div style="background:#0a1020;border:1px solid rgba(0,255,136,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#00ff88;margin-bottom:6px">📦 Friday Backup</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Backs up all Python files, the database, and config to a dated folder on the VPS.
        Run every Friday night before weekend work. Takes ~5 seconds.
      </div>
      <button onclick="runAction('backup')" class="btn" style="width:100%;background:rgba(0,255,136,0.12);border:1px solid rgba(0,255,136,0.35);color:#00ff88">
        📦 Run Backup Now
      </button>
    </div>

    <!-- Pre-Monday Check -->
    <div style="background:#0a1020;border:1px solid rgba(0,170,255,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#00aaff;margin-bottom:6px">✅ Pre-Monday Check</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Checks all 3 screens running, no P1 alerts, config correct, intelligence reviewed,
        open positions known. Run Sunday night before bed.
      </div>
      <button onclick="runAction('monday')" class="btn" style="width:100%;background:rgba(0,170,255,0.12);border:1px solid rgba(0,170,255,0.35);color:#00aaff">
        ✅ Run Check Now
      </button>
    </div>

    <!-- Clean Old Backups -->
    <div style="background:#0a1020;border:1px solid rgba(239,68,68,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#ef4444;margin-bottom:6px">🧹 Clean Old Backups</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Removes backup folders older than 28 days (keeps last 4 weeks).
        Run monthly to keep disk usage in check.
      </div>
      <button onclick="pinAction('clean', 'Clean backups older than 28 days?')" class="btn" style="width:100%;background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.3);color:#ef4444">
        🧹 Clean Old Backups
      </button>
    </div>

    <!-- View Disk Usage -->
    <div style="background:#0a1020;border:1px solid rgba(170,136,255,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#aa88ff;margin-bottom:6px">💾 Disk Usage</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Shows how much disk space the VPS is using — backups, logs, database.
        Good to check monthly so you don't run out of space.
      </div>
      <button onclick="runAction('disk')" class="btn" style="width:100%;background:rgba(170,136,255,0.1);border:1px solid rgba(170,136,255,0.3);color:#aa88ff">
        💾 Check Disk Usage
      </button>
    </div>

    <!-- Export DB -->
    <div style="background:#0a1020;border:1px solid rgba(0,170,255,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#00aaff;margin-bottom:6px">📤 Export Database</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Downloads <code>alphabot.db</code> directly to your device — all trade history,
        near-misses, intelligence runs. Off-site backup. Run weekly.
      </div>
      <a href="{_base}/maintenance/export-db" style="text-decoration:none">
        <button class="btn" style="width:100%;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.3);color:#00aaff">
          📤 Download Database
        </button>
      </a>
    </div>

    <!-- Revert a File -->
    <div style="background:#0a1020;border:1px solid rgba(255,204,0,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#ffcc00;margin-bottom:6px">↩ Revert a File</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Put up a dodgy file? Pick the file, see all dated backups for it,
        choose the version you want. PIN required. Never touches the database.
      </div>
      <a href="{_base}/maintenance/revert" style="text-decoration:none">
        <button class="btn" style="width:100%;background:rgba(255,204,0,0.08);border:1px solid rgba(255,204,0,0.3);color:#ffcc00">
          ↩ Revert a File
        </button>
      </a>
    </div>

    <!-- Pull from GitHub -->
    <div style="background:#0a1020;border:1px solid rgba(0,255,136,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#00ff88;margin-bottom:6px">⬇️ Pull from GitHub</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Force-pulls latest code from GitHub. Resets all tracked files to match the repo.
        Safe — never touches <code style="color:#ffcc00">.env</code> or <code style="color:#ffcc00">alphabot.db</code>.
        PIN required. Restart bot after.
      </div>
      <button onclick="pinAction('github-pull', 'Force pull from GitHub? All tracked files will be reset to the repo version.')"
        class="btn" style="width:100%;background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.3);color:#00ff88">
        ⬇️ Pull from GitHub
      </button>
    </div>

    <!-- Download from VPS -->
    <div style="background:#0a1020;border:1px solid rgba(170,136,255,0.2);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#aa88ff;margin-bottom:6px">📥 Download from VPS</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Download all app files as a zip — or pick individual files.
        Use this to get a local copy of the current VPS state, especially before rebuilds.
      </div>
      <a href="{_base}/maintenance/download" style="text-decoration:none">
        <button class="btn" style="width:100%;background:rgba(170,136,255,0.1);border:1px solid rgba(170,136,255,0.3);color:#aa88ff">
          📥 Download Files
        </button>
      </a>
    </div>

  </div>
</div>

{backup_list_html}

<!-- Disk usage result -->
<div id="disk-result" style="display:none" class="card">
  <div style="font-size:13px;font-weight:700;color:#aa88ff;margin-bottom:10px">💾 Disk Usage</div>
  <pre id="disk-output" style="font-size:12px;color:#94a3b8;white-space:pre-wrap"></pre>
</div>

<!-- PIN overlay for destructive actions -->
<div id="pin-overlay" onclick="if(event.target===this)closePin()">
  <div class="pin-box">
    <div style="font-size:18px;font-weight:700;color:#f59e0b;margin-bottom:6px">🔒 Confirm Action</div>
    <div id="pin-label" style="font-size:13px;color:#e0e0e0;margin-bottom:18px"></div>
    <input id="pin-input" type="password" maxlength="10" placeholder="••••"
      style="background:#0a0a0f;border:1px solid rgba(245,158,11,0.4);border-radius:8px;color:#f59e0b;
             font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;padding:10px;
             width:100%;text-align:center;letter-spacing:4px;margin-bottom:14px"
      onkeydown="if(event.key==='Enter')submitPin()">
    <div style="display:flex;gap:10px">
      <button onclick="closePin()" style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#94a3b8;padding:10px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:13px">Cancel</button>
      <button onclick="submitPin()" style="flex:2;background:rgba(245,158,11,0.15);border:1px solid rgba(245,158,11,0.4);border-radius:8px;color:#f59e0b;padding:10px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700">Confirm</button>
    </div>
    <div id="pin-error" style="color:#ef4444;font-size:12px;margin-top:10px;display:none">Wrong PIN</div>
  </div>
</div>

<script>
var _pendingAction = null;

function runAction(action) {{
  if (action === 'disk') {{
    fetch(BASE+'/maintenance/disk')
      .then(r => r.json())
      .then(d => {{
        document.getElementById('disk-result').style.display = 'block';
        document.getElementById('disk-output').textContent = d.output || 'Error';
      }});
    return;
  }}
  window.location.href = BASE+'/maintenance/run?action=' + action;
}}

function pinAction(action, label) {{
  _pendingAction = action;
  document.getElementById('pin-label').textContent = label;
  document.getElementById('pin-error').style.display = 'none';
  document.getElementById('pin-input').value = '';
  document.getElementById('pin-overlay').classList.add('visible');
  document.getElementById('pin-input').focus();
}}

function closePin() {{
  document.getElementById('pin-overlay').classList.remove('visible');
  _pendingAction = null;
}}

function submitPin() {{
  var pin = document.getElementById('pin-input').value;
  fetch(BASE+'/maintenance/action', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{pin: pin, action: _pendingAction}})
  }}).then(r => r.json()).then(d => {{
    if (d.status === 'wrong_pin') {{
      document.getElementById('pin-error').style.display = 'block';
    }} else {{
      closePin();
      window.location.href = BASE+'/maintenance?msg=' + encodeURIComponent(d.message || 'Done');
    }}
  }});
}}

function restoreBackup(dateStr) {{
  _pendingAction = 'restore:' + dateStr;
  document.getElementById('pin-label').textContent = 'Restore all files from ' + dateStr + '? This will overwrite current code.';
  document.getElementById('pin-error').style.display = 'none';
  document.getElementById('pin-input').value = '';
  document.getElementById('pin-overlay').classList.add('visible');
  document.getElementById('pin-input').focus();
}}
</script>
</body></html>"""


@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(msg: str = None, msg_type: str = "ok"):
    return HTMLResponse(_build_maintenance_page(msg=msg, msg_type=msg_type))


@app.get("/maintenance/run")
async def maintenance_run(action: str = "backup"):
    """Run non-destructive maintenance actions — no PIN needed."""
    if action == "backup":
        folder, results = _do_backup()
        all_ok = all(r["ok"] for r in results)
        _log_agent(f"Backup {'complete' if all_ok else 'partial'}: {folder}")
        return HTMLResponse(_build_maintenance_page(
            msg=f"Backup saved to {folder}" if all_ok else "Backup completed with some errors — check results",
            msg_type="ok" if all_ok else "error",
            backup_result=(folder, results),
        ))
    elif action == "monday":
        checks = _do_monday_check()
        all_ok = all(s in ("✅", "ℹ️") for s, _ in checks)
        _log_agent(f"Pre-Monday check: {'PASS' if all_ok else 'ACTION REQUIRED'}")
        return HTMLResponse(_build_maintenance_page(
            monday_result=checks,
        ))
    return HTMLResponse(_build_maintenance_page(msg="Unknown action", msg_type="error"))


@app.get("/maintenance/disk")
async def maintenance_disk():
    """Return disk usage summary as JSON."""
    output = run_cmd("df -h /home && echo '---' && du -sh /home/alphabot/backups/ 2>/dev/null && du -sh /home/alphabot/app/alphabot.db && du -sh /home/alphabot/app/alphabot.log")
    return JSONResponse({"output": output})


@app.post("/maintenance/action")
async def maintenance_action(request: Request):
    """PIN-gated destructive maintenance actions."""
    from fastapi.responses import JSONResponse as JR
    try:
        body = await request.json()
        if body.get("pin") != MAINT_PIN:
            return JR({"status": "wrong_pin"})
        action = body.get("action", "")

        if action == "clean":
            import glob as _glob
            cutoff = datetime.now(PARIS).timestamp() - (28 * 86400)
            removed = []
            for d in _glob.glob(os.path.join(BACKUP_ROOT, "????????")):
                if os.path.getmtime(d) < cutoff:
                    import shutil
                    shutil.rmtree(d)
                    removed.append(os.path.basename(d))
            msg = f"Removed {len(removed)} old backup(s): {', '.join(removed)}" if removed else "No backups older than 28 days found"
            _log_agent(f"Maintenance clean: {msg}")
            return JR({"status": "ok", "message": msg})

        elif action == "github-pull":
            # Safe force pull — resets tracked files to remote, never touches .env or DB
            steps = [
                f"cd {APP_PATH} && git fetch origin main",
                f"cd {APP_PATH} && git stash",
                f"cd {APP_PATH} && git reset --hard origin/main",
                f"cd {APP_PATH} && git stash drop 2>/dev/null || true",
            ]
            results = []
            for cmd in steps:
                out = run_cmd(cmd, timeout=30)
                results.append(f"$ {cmd.split('&& ')[1]}\n{out}")
            summary = "\n\n".join(results)
            _log_agent("GitHub force pull executed")
            return JR({"status": "ok", "ok": True,
                       "message": f"Pull complete — .env and database untouched. Restart the bot to apply new code.\n\n{summary[:500]}"})

        elif action.startswith("restore:"):
            import shutil
            date_str   = action.split(":", 1)[1]
            backup_dir = os.path.join(BACKUP_ROOT, date_str)
            if not os.path.isdir(backup_dir):
                return JR({"status": "error", "message": f"Backup folder not found: {backup_dir}"})
            restored = []
            errors   = []
            restore_map = {v: os.path.join(APP_PATH, k) for k, v in FILES_TO_BACKUP
                           if v != "alphabot.db"}  # never auto-restore DB
            for src_name, dst_path in restore_map.items():
                src = os.path.join(backup_dir, src_name)
                if os.path.exists(src):
                    try:
                        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                        shutil.copy2(src, dst_path)
                        restored.append(src_name)
                    except Exception as e:
                        errors.append(f"{src_name}: {e}")
            _log_agent(f"Restore from {date_str}: {len(restored)} files restored")
            if errors:
                return JR({"status": "ok", "message": f"Restored {len(restored)} files. Errors: {'; '.join(errors)}. Restart the bot to apply."})
            return JR({"status": "ok", "message": f"Restored {len(restored)} files from {date_str}. Restart the bot to apply: pkill -9 -f python3 && bash /home/alphabot/start.sh"})

            _log_agent(f"Restore from {date_str}: {len(restored)} files restored")
            if errors:
                return JR({"status": "ok", "message": f"Restored {len(restored)} files. Errors: {'; '.join(errors)}. Restart the bot to apply."})
            return JR({"status": "ok", "message": f"Restored {len(restored)} files from {date_str}. Restart the bot to apply: pkill -9 -f python3 && bash /home/alphabot/start.sh"})

        elif action.startswith("revert-file:"):
            # Format: revert-file:YYYYMMDD:filename.py
            import shutil
            parts     = action.split(":", 2)
            date_str  = parts[1] if len(parts) > 1 else ""
            file_name = parts[2] if len(parts) > 2 else ""
            revertable = [v for _, v in FILES_TO_BACKUP if v != "alphabot.db"]
            if file_name not in revertable:
                return JR({"status": "error", "message": f"File not allowed: {file_name}"})
            src = os.path.join(BACKUP_ROOT, date_str, file_name)
            if not os.path.exists(src):
                return JR({"status": "error", "message": f"Backup not found: {src}"})
            # Find destination path
            dst = None
            for rel, name in FILES_TO_BACKUP:
                if name == file_name:
                    dst = os.path.join(APP_PATH, rel)
                    break
            if not dst:
                return JR({"status": "error", "message": "Could not resolve destination path"})
            shutil.copy2(src, dst)
            _log_agent(f"File revert: {file_name} restored from backup {date_str}")
            return JR({"status": "ok", "message": f"{file_name} restored from {date_str}. Restart the bot to apply changes."})

        return JR({"status": "error", "message": "Unknown action"})
    except Exception as e:
        return JR({"status": "error", "message": str(e)})


@app.get("/maintenance/export-db")
async def export_db():
    """Download alphabot.db directly to browser."""
    if not os.path.exists(DB_PATH):
        return HTMLResponse("Database not found", status_code=404)
    filename = f"alphabot_{datetime.now(PARIS).strftime('%Y%m%d_%H%M')}.db"
    return FileResponse(DB_PATH, media_type="application/octet-stream", filename=filename)


@app.get("/maintenance/revert", response_class=HTMLResponse)
async def revert_page(file: str = None, msg: str = None):
    _base = os.environ.get("ROOT_PATH","")
    """Pick a file, see its dated backups, choose one to restore."""
    import html as _html
    now_str    = datetime.now(PARIS).strftime("%A %d %B %Y · %H:%M Paris")
    revertable = [v for _, v in FILES_TO_BACKUP if v != "alphabot.db"]

    msg_html = ""
    if msg:
        msg_html = f'<div style="background:#00ff8818;border:1px solid #00ff8844;border-radius:10px;padding:14px 18px;margin-bottom:20px;color:#00ff88;font-weight:700">✅ {_html.escape(msg)}</div>'

    # File-specific backup list
    file_backups_html = ""
    if file and file in revertable:
        backups = _list_backups()
        rows = ""
        for b in backups:
            fpath = os.path.join(b["path"], file)
            if os.path.exists(fpath):
                size     = os.path.getsize(fpath)
                modified = datetime.fromtimestamp(
                    os.path.getmtime(fpath), tz=PARIS).strftime("%Y-%m-%d %H:%M")
                rows += (
                    f'<tr>'
                    f'<td style="color:#e0e0e0;font-weight:700">{_html.escape(b["label"])}</td>'
                    f'<td style="color:#94a3b8;font-size:12px">{modified}</td>'
                    f'<td style="color:#00aaff">{size/1024:.1f} KB</td>'
                    f'<td><button onclick="doRevert(\'{_html.escape(b["date_str"])}\',\'{_html.escape(file)}\')" '
                    f'style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);'
                    f'border-radius:6px;color:#ef4444;font-size:12px;padding:5px 12px;cursor:pointer">'
                    f'↩ Use This</button></td>'
                    f'</tr>'
                )
        if rows:
            file_backups_html = f"""
            <div style="background:#111118;border:1px solid rgba(239,68,68,0.25);border-radius:10px;padding:18px;margin-bottom:16px">
              <div style="font-size:13px;font-weight:700;color:#ef4444;margin-bottom:4px">
                2. Pick a backup date for <span style="color:#ffcc00;font-family:'JetBrains Mono',monospace">{_html.escape(file)}</span>
              </div>
              <div style="font-size:12px;color:#94a3b8;margin-bottom:14px">PIN required. Bot restart needed after.</div>
              <table><thead><tr><th>Date</th><th>Saved</th><th>Size</th><th></th></tr></thead>
              <tbody>{rows}</tbody></table>
            </div>"""
        else:
            file_backups_html = f'<div style="background:#111118;border-radius:10px;padding:18px;color:#94a3b8">No backups found for {_html.escape(file)} — run Friday Backup first.</div>'

    # File picker grid
    file_btns = ""
    for f_name in revertable:
        sel = "rgba(255,204,0,0.6)" if f_name == file else "#1e1e2e"
        col = "#ffcc00" if f_name == file else "#94a3b8"
        file_btns += (
            f'<a href="{_base}/maintenance/revert?file={_html.escape(f_name)}" style="text-decoration:none">'
            f'<div style="background:#111118;border:1px solid {sel};border-radius:8px;'
            f'padding:10px 14px;font-size:12px;color:{col};cursor:pointer;'
            f'font-family:\'JetBrains Mono\',monospace">{_html.escape(f_name)}</div></a>'
        )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Revert File — AlphaBot</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0f;color:#e2e8f0;font-family:'JetBrains Mono',monospace;padding:16px;max-width:900px;font-size:14px;margin:0 auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{color:#94a3b8;text-align:left;padding:6px 8px;border-bottom:1px solid #1e1e2e;font-size:11px;text-transform:uppercase;letter-spacing:1px}}
td{{padding:8px;border-bottom:1px solid #0f0f18}}
#po{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:999;align-items:center;justify-content:center}}
#po.v{{display:flex}}
.pb{{background:#111118;border:1px solid rgba(239,68,68,0.4);border-radius:16px;padding:32px;text-align:center;max-width:360px;width:90%}}
</style></head><body>
<div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #1e1e2e;flex-wrap:wrap">
  <div>
    <div style="font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:#ef4444">↩ Revert a File</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:2px">{now_str}</div>
  </div>
  <a href="{_base}/maintenance" style="margin-left:auto;color:#94a3b8;text-decoration:none;font-size:13px">← Maintenance</a>
</div>
{msg_html}
<div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:18px;margin-bottom:16px">
  <div style="font-size:13px;font-weight:700;color:#ffcc00;margin-bottom:12px">1. Pick the file to revert</div>
  <div style="display:flex;flex-wrap:wrap;gap:8px">{file_btns}</div>
</div>
{file_backups_html}
<div id="po" onclick="if(event.target===this)closePin()">
  <div class="pb">
    <div style="font-size:16px;font-weight:700;color:#ef4444;margin-bottom:6px">↩ Confirm Revert</div>
    <div id="pl" style="font-size:12px;color:#e0e0e0;margin-bottom:16px"></div>
    <input id="pi" type="password" maxlength="10" placeholder="••••"
      style="background:#0a0a0f;border:1px solid rgba(239,68,68,0.4);border-radius:8px;color:#ef4444;
             font-family:'JetBrains Mono',monospace;font-size:20px;padding:10px;width:100%;
             text-align:center;letter-spacing:4px;margin-bottom:14px"
      onkeydown="if(event.key==='Enter')submitPin()">
    <div style="display:flex;gap:10px">
      <button onclick="closePin()" style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#94a3b8;padding:10px;cursor:pointer;font-size:13px">Cancel</button>
      <button onclick="submitPin()" style="flex:2;background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);border-radius:8px;color:#ef4444;padding:10px;cursor:pointer;font-size:13px;font-weight:700">Revert File</button>
    </div>
    <div id="pe" style="color:#ef4444;font-size:12px;margin-top:10px;display:none">Wrong PIN</div>
  </div>
</div>
<script>
var _d=null,_f=null;
function doRevert(d,f){{_d=d;_f=f;document.getElementById('pl').textContent='Revert '+f+' to backup from '+d+'?';document.getElementById('pe').style.display='none';document.getElementById('pi').value='';document.getElementById('po').classList.add('v');document.getElementById('pi').focus();}}
function closePin(){{document.getElementById('po').classList.remove('v');}}
function submitPin(){{
  fetch(BASE+'/maintenance/action',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{pin:document.getElementById('pi').value,action:'revert-file:'+_d+':'+_f}})}})
  .then(r=>r.json()).then(d=>{{
    if(d.status==='wrong_pin'){{document.getElementById('pe').style.display='block';}}
    else{{closePin();window.location.href=BASE+'/maintenance/revert?msg='+encodeURIComponent(d.message||'Done');}}
  }});
}}
</script>
</body></html>""")


# ═══════════════════════════════════════════════════════════════
# DOWNLOAD FROM VPS — zip or individual file
# ═══════════════════════════════════════════════════════════════
@app.get("/maintenance/download", response_class=HTMLResponse)
async def download_page():
    _base = os.environ.get("ROOT_PATH","")
    """Pick individual files to download, or grab the whole app as a zip."""
    import html as _html
    now_str = datetime.now(PARIS).strftime("%A %d %B %Y · %H:%M Paris")

    # Build file list with sizes
    file_rows = ""
    for src_rel, dst_name in FILES_TO_BACKUP:
        fpath = os.path.join(APP_PATH, src_rel)
        if os.path.exists(fpath):
            size = os.path.getsize(fpath)
            size_str = f"{size/1024:.1f} KB" if size < 1024*1024 else f"{size/1024/1024:.1f} MB"
            modified = datetime.fromtimestamp(os.path.getmtime(fpath),
                                              tz=PARIS).strftime("%Y-%m-%d %H:%M")
            file_rows += (
                f'<tr>'
                f'<td style="color:#e0e0e0;font-family:\'JetBrains Mono\',monospace">{_html.escape(dst_name)}</td>'
                f'<td style="color:#94a3b8;font-size:12px">{_html.escape(src_rel)}</td>'
                f'<td style="color:#00aaff">{size_str}</td>'
                f'<td style="color:#94a3b8;font-size:12px">{modified}</td>'
                f'<td><a href="{_base}/maintenance/download/file?name={_html.escape(dst_name)}" '
                f'style="text-decoration:none">'
                f'<button style="background:rgba(170,136,255,0.1);border:1px solid rgba(170,136,255,0.3);'
                f'border-radius:6px;color:#aa88ff;font-size:12px;padding:5px 12px;cursor:pointer">'
                f'⬇ Download</button></a></td>'
                f'</tr>'
            )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Download from VPS — AlphaBot</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0f;color:#e2e8f0;font-family:'JetBrains Mono',monospace;padding:16px;max-width:960px;font-size:14px;margin:0 auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{color:#94a3b8;text-align:left;padding:8px;border-bottom:1px solid #1e1e2e;font-size:11px;text-transform:uppercase;letter-spacing:1px}}
td{{padding:8px;border-bottom:1px solid #0f0f18;vertical-align:middle}}
</style></head><body>
<div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #1e1e2e;flex-wrap:wrap">
  <div>
    <div style="font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:#aa88ff">📥 Download from VPS</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:2px">{now_str}</div>
  </div>
  <a href="{_base}/maintenance" style="margin-left:auto;color:#94a3b8;text-decoration:none;font-size:13px">← Maintenance</a>
</div>

<div style="background:#0a1020;border:1px solid rgba(170,136,255,0.25);border-radius:12px;padding:20px;margin-bottom:20px">
  <div style="font-size:15px;font-weight:700;color:#aa88ff;margin-bottom:8px">📦 Download Everything as ZIP</div>
  <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
    Creates a zip of all current app files from the VPS — the exact code that is running right now.
    Does not include <code style="color:#ffcc00">.env</code>.
    Great to grab before a major rebuild session.
  </div>
  <a href="{_base}/maintenance/download/zip" style="text-decoration:none">
    <button style="background:rgba(170,136,255,0.15);border:1px solid rgba(170,136,255,0.4);border-radius:8px;
                   color:#aa88ff;font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700;
                   padding:12px 28px;cursor:pointer">
      📦 Download All as ZIP
    </button>
  </a>
</div>

<div style="background:#111118;border:1px solid #1e1e2e;border-radius:12px;padding:20px">
  <div style="font-size:13px;font-weight:700;color:#e0e0e0;margin-bottom:14px">📄 Individual Files</div>
  <div style="overflow-x:auto">
    <table><thead><tr><th>File</th><th>VPS Path</th><th>Size</th><th>Modified</th><th></th></tr></thead>
    <tbody>{file_rows}</tbody></table>
  </div>
</div>
</body></html>""")


@app.get("/maintenance/download/zip")
async def download_zip():
    """Create a zip of all app files and serve it for download."""
    import zipfile, tempfile
    zip_name = f"alphabot_{datetime.now(PARIS).strftime('%Y%m%d_%H%M')}.zip"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for src_rel, dst_name in FILES_TO_BACKUP:
            fpath = os.path.join(APP_PATH, src_rel)
            if os.path.exists(fpath) and dst_name != ".env":
                zf.write(fpath, dst_name)
    _log_agent(f"VPS zip download: {zip_name}")
    return FileResponse(tmp.name, media_type="application/zip", filename=zip_name)


@app.get("/maintenance/download/file")
async def download_file(name: str = ""):
    """Download a single named file from the VPS."""
    allowed = {dst: src for src, dst in FILES_TO_BACKUP}
    if name not in allowed:
        return HTMLResponse("File not allowed", status_code=403)
    fpath = os.path.join(APP_PATH, allowed[name])
    if not os.path.exists(fpath):
        return HTMLResponse("File not found", status_code=404)
    _log_agent(f"VPS file download: {name}")
    return FileResponse(fpath, media_type="application/octet-stream", filename=name)


# ═══════════════════════════════════════════════════════════════
# LIVE BOT LOG — scrollable, auto-refresh, works on iPad
# ═══════════════════════════════════════════════════════════════
@app.get("/log", response_class=HTMLResponse)
async def live_log_page(lines: int = 200, screen: str = "alphabot"):
    _base = os.environ.get("ROOT_PATH","")
    """Scrollable live bot log — works on iPad, auto-refreshes every 10s."""
    now_str = datetime.now(PARIS).strftime("%H:%M:%S Paris")

    # Read from the persistent log file
    log_content = run_cmd(f"tail -{lines} {LOG_PATH}", timeout=10)

    # Colour-code log lines
    coloured = ""
    for line in log_content.split("\n"):
        if any(x in line for x in ["ERROR", "FAILED", "CRASH", "P1", "kill switch"]):
            col = "#ef4444"
        elif any(x in line for x in ["WARNING", "WARN", "⚠"]):
            col = "#f59e0b"
        elif any(x in line for x in ["✅", "BUY", "SELL", "FILLED", "profit"]):
            col = "#00ff88"
        elif any(x in line for x in ["SKIP", "HOLD", "near_miss"]):
            col = "#94a3b8"
        elif any(x in line for x in ["INTELLIGENCE", "ATR", "ROTATE", "STALE"]):
            col = "#aa88ff"
        else:
            col = "#cbd5e1"
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        coloured += f'<div style="color:{col};padding:1px 0;line-height:1.5">{safe}</div>'

    # Screen tab buttons
    screens = ["alphabot", "dashboard", "agent"]
    tabs = ""
    for s in screens:
        active = "border-color:rgba(0,255,136,0.6);color:#00ff88" if s == screen else "border-color:#1e1e2e;color:#94a3b8"
        tabs += (f'<a href="{_base}/log?screen={s}&lines={lines}" style="text-decoration:none">'
                 f'<button style="background:#111118;border:1px solid;{active};border-radius:6px;'
                 f'padding:6px 14px;font-size:12px;cursor:pointer;font-family:\'JetBrains Mono\',monospace">'
                 f'{s}</button></a> ')

    # Lines selector
    line_opts = ""
    for n in [50, 100, 200, 500]:
        sel = "color:#00ff88;font-weight:700" if n == lines else "color:#94a3b8"
        line_opts += (f'<a href="{_base}/log?screen={screen}&lines={n}" '
                      f'style="text-decoration:none;{sel};font-size:12px;margin-right:10px">{n}</a>')

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>AlphaBot Live Log</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0f;color:#e2e8f0;font-family:'JetBrains Mono',monospace;
      font-size:12px;padding:0;margin:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}}
#header{{padding:10px 14px;border-bottom:1px solid #1e1e2e;background:#0a0a0f;
         display:flex;align-items:center;gap:10px;flex-wrap:wrap;flex-shrink:0}}
#log-wrap{{flex:1;overflow-y:auto;padding:12px 14px;-webkit-overflow-scrolling:touch}}
#log-content{{font-size:11px;line-height:1.5;word-break:break-all}}
#footer{{padding:8px 14px;border-top:1px solid #1e1e2e;background:#0a0a0f;
         display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap}}
#countdown{{color:#94a3b8;font-size:11px}}
.refresh-dot{{width:8px;height:8px;border-radius:50%;background:#00ff88;display:inline-block;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.3}}}}
</style>
</head><body>

<div id="header">
  <div style="font-family:'Syne',sans-serif;font-size:16px;font-weight:800;color:#00ff88">
    AlphaBot <span style="color:#64748b">Live Log</span>
  </div>
  <div style="display:flex;gap:6px">{tabs}</div>
  <a href="/" style="margin-left:auto;color:#94a3b8;text-decoration:none;font-size:12px">← Agent</a>
</div>

<div id="log-wrap">
  <div id="log-content">{coloured if coloured else '<div style="color:#475569">No log output yet.</div>'}</div>
</div>

<div id="footer">
  <span class="refresh-dot"></span>
  <span style="color:#94a3b8;font-size:11px">Auto-refresh every 10s · {now_str} · Last {lines} lines</span>
  <span style="margin-left:auto;color:#64748b;font-size:11px">Lines: {line_opts}</span>
  <span id="countdown" style="font-size:11px;color:#475569"></span>
</div>

<script>
// Auto-scroll to bottom on load
window.addEventListener('load', function() {{
  var w = document.getElementById('log-wrap');
  w.scrollTop = w.scrollHeight;
}});

// Countdown + auto-refresh every 10s
var secs = 10;
var cd = document.getElementById('countdown');
setInterval(function() {{
  secs--;
  if (cd) cd.textContent = 'Refreshing in ' + secs + 's';
  if (secs <= 0) location.reload();
}}, 1000);
</script>
</body></html>""")
