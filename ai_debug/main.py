"""
AlphaBot Debug Agent v7
- Feedback box: user steers Claude mid-session
- Comprehensive dashboard audit: metrics, prices, P&L, market hours, active trading check
- POST-redirect-GET pattern
- Persistent CONTEXT.md
- Context compression every 10 steps
- Stuck detector
"""

from fastapi import FastAPI, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
import os, subprocess, sqlite3, json, anthropic, base64, html, re, uuid
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.error import URLError

app = FastAPI()
SESSIONS = {}

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
DB_PATH = "/home/alphabot/app/alphabot.db"
APP_PATH = "/home/alphabot/app"
CONTEXT_PATH = "/home/alphabot/app/ai_debug/CONTEXT.md"
SCREEN_NAME = "alphabot"
MAX_STEPS_BEFORE_COMPRESS = 10

SYSTEM_PROMPT = """You are an expert autonomous debugging agent for AlphaBot trading bot.

Read the CONTEXT section carefully - it has architecture, known errors, previous fixes.

ARCHITECTURE:
- Bot screen: "alphabot" | Dashboard: port 8080 | Debug agent: port 8000
- Files: app/main.py, app/dashboard.py, core/config.py, core/execution.py, core/risk.py
- DB: /home/alphabot/app/alphabot.db
- Brokers: Alpaca (US), IBKR (FTSE/ASX/stops), Binance (crypto - 401 error known)

COMMAND SYNTAX (no smart quotes ever):
- grep -n searchterm /home/alphabot/app/app/main.py
- sed -n 200,250p /home/alphabot/app/app/main.py
- cat /home/alphabot/app/core/config.py

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
- If user provides feedback or correction, prioritise it IMMEDIATELY in next response
- NO smart quotes in commands - use plain ASCII only
- If COMPLETE, fill CONTEXT_UPDATE so fix is remembered
- If stuck (same command failing), change approach completely
"""

COMPRESS_PROMPT = """Summarise this debugging session into under 300 words.
Include: problem being solved, what was tried, what was found, current theory.
Be specific with file names and line numbers."""

AUDIT_SYSTEM = """You are auditing the AlphaBot trading dashboard and bot health.

You will receive:
1. Dashboard HTML (from port 8080)
2. Database snapshot (trades, positions, P&L)
3. Bot screen log (last 60 lines)
4. Current UTC time and market hours status

AUDIT CHECKLIST - check every item:

DASHBOARD METRICS:
- Total balance: does it match Alpaca paper account?
- This Month / Last Month P&L: calculated correctly from DB trades?
- This Week / Last Week P&L: correct date ranges?
- Last 7 Days chart: populating with real data?
- Risk Status: showing correct value?
- Near misses table: populated?
- Recent trades table: showing latest trades with correct P&L?
- All $ and % values: formatted correctly, not showing NaN/None/0 when data exists?

MARKET HOURS & TRADING:
- US Market (NYSE/NASDAQ): open Mon-Fri 14:30-21:00 UTC
- FTSE (LSE): open Mon-Fri 08:00-16:30 UTC
- ASX: open Mon-Fri 23:00-05:00 UTC (previous day)
- Crypto: always open

For each market currently OPEN:
- Is the bot scanning? (check screen log for scan messages)
- Are candidates being found? (check log for "qualified BUY" messages)
- Are positions being taken? (check DB for today's trades)
- If 0 signals for open market - is that expected or a bug?

SCORING:
Give each section: PASS / WARN / FAIL
Final verdict: PASS (all good) / WARN (minor issues) / FAIL (broken)

Be specific: "Dashboard shows $X but DB total is $Y" not vague statements."""


def clean(s):
    return s.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'").strip()


def is_safe(cmd):
    cmd = clean(cmd)
    blocked = ['rm ', 'chmod +x', '> /', 'dd ', 'mkfs', 'reboot', 'shutdown', 'passwd', 'sudo']
    if any(b in cmd for b in blocked):
        return False
    allowed = ['grep', 'cat ', 'head', 'tail', 'sed', 'wc ', 'ls ', 'find ', 'python3',
               'ps ', 'df ', 'free ', 'screen', 'git ', 'sqlite3', 'echo ', 'env',
               'netstat', 'ss ', 'curl', 'cp ', 'pip ', 'wget ']
    return any(cmd.startswith(a) for a in allowed)


def run_cmd(cmd, timeout=10):
    cmd = clean(cmd)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return out[:4000] if len(out) > 4000 else out
    except Exception as e:
        return f"Error: {e}"


def load_context():
    try:
        with open(CONTEXT_PATH, 'r') as f:
            return f.read()
    except:
        return "No context file found."


def update_context(fix_summary):
    try:
        with open(CONTEXT_PATH, 'r') as f:
            content = f.read()
        now = datetime.now().strftime("%d-%b-%Y %H:%M")
        new_line = f"\n- [{now}] {fix_summary}"
        content = content.replace(
            "## Fixes Made By Agent (most recent first)",
            f"## Fixes Made By Agent (most recent first){new_line}"
        )
        content = re.sub(r'## Last Updated\n.*', f'## Last Updated\n{now}', content)
        with open(CONTEXT_PATH, 'w') as f:
            f.write(content)
    except:
        pass


def get_bot_context():
    try:
        subprocess.run(["/usr/bin/screen", "-S", SCREEN_NAME, "-X", "hardcopy", "/tmp/ab.txt"],
                       timeout=2, capture_output=True)
        with open("/tmp/ab.txt", "r", errors="replace") as f:
            log = "".join(f.readlines()[-80:])
    except:
        log = "No screen log"
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

        # All trades
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 50")
        snap["trades"] = [dict(r) for r in cur.fetchall()]

        # Today's trades
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT * FROM trades WHERE created_at LIKE ?", (f"{today}%",))
        snap["today_trades"] = [dict(r) for r in cur.fetchall()]

        # P&L totals
        cur.execute("SELECT SUM(pnl) as total FROM trades")
        row = cur.fetchone()
        snap["total_pnl"] = float(row["total"] or 0)

        # This week
        cur.execute("SELECT SUM(pnl) as wpnl FROM trades WHERE created_at >= date('now', '-7 days')")
        row = cur.fetchone()
        snap["week_pnl"] = float(row["wpnl"] or 0)

        # Near misses
        try:
            cur.execute("SELECT * FROM near_misses ORDER BY created_at DESC LIMIT 10")
            snap["near_misses"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["near_misses"] = []

        # Positions
        try:
            cur.execute("SELECT * FROM positions")
            snap["positions"] = [dict(r) for r in cur.fetchall()]
        except:
            snap["positions"] = []

        conn.close()
    except Exception as e:
        snap["error"] = str(e)
    return snap


def fetch_dashboard():
    try:
        with urlopen("http://localhost:8080", timeout=8) as r:
            raw = r.read().decode('utf-8', errors='replace')
            # Strip scripts and styles for cleaner audit
            raw = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL)
            raw = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL)
            return raw[:30000]
    except Exception as e:
        return f"DASHBOARD FETCH FAILED: {e}"


def get_market_hours():
    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    m = now_utc.minute
    dow = now_utc.weekday()  # 0=Mon, 6=Sun
    time_dec = h + m / 60.0
    is_weekday = dow < 5

    markets = {}
    markets["UTC_time"] = now_utc.strftime("%H:%M UTC %a")
    markets["US"] = "OPEN" if is_weekday and 14.5 <= time_dec <= 21.0 else "CLOSED"
    markets["FTSE"] = "OPEN" if is_weekday and 8.0 <= time_dec <= 16.5 else "CLOSED"
    markets["ASX"] = "OPEN" if is_weekday and (time_dec >= 23.0 or time_dec <= 5.0) else "CLOSED"
    markets["CRYPTO"] = "OPEN"
    return markets


def call_claude(messages, system=None):
    if not CLAUDE_API_KEY:
        return "ERROR: CLAUDE_API_KEY not set."
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        r = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system or SYSTEM_PROMPT,
            messages=messages
        )
        return r.content[0].text
    except Exception as e:
        return f"Claude API error: {e}"


def compress_history(steps, question):
    text = "\n\n".join([
        f"{'CLAUDE' if s['role'] == 'assistant' else 'USER/VPS'}: {s['content'] if isinstance(s['content'], str) else str(s['content'])}"
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
    status = re.search(r'STATUS:\s*(\w+)', text)
    status = status.group(1) if status else "INVESTIGATING"
    analysis = re.search(r'ANALYSIS:\s*(.*?)(?=NEXT_COMMAND:|REASON:|CONTEXT_UPDATE:|$)', text, re.DOTALL)
    analysis = analysis.group(1).strip() if analysis else ""
    command = re.search(r'```(?:bash)?\s*(.*?)```', text, re.DOTALL)
    command = clean(command.group(1)) if command else ""
    reason = re.search(r'REASON:\s*(.*?)(?=CONTEXT_UPDATE:|$)', text, re.DOTALL)
    reason = reason.group(1).strip() if reason else ""
    ctx = re.search(r'CONTEXT_UPDATE:\s*(.*?)$', text, re.DOTALL)
    ctx = ctx.group(1).strip() if ctx else ""
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
    return RedirectResponse(f"/r/{sid}", status_code=303)


def render(analysis="", command="", reason="", status="", cmd_output="", cmd_run="",
           error="", question="", history="", complete=False, compressed=False,
           step_count=0, ctx_updated=False, audit_result="", **kwargs):

    screen_status = run_cmd("/usr/bin/screen -ls")
    bot_ok = "alphabot" in screen_status
    sc = "#00ff88" if bot_ok else "#ef4444"
    st = "RUNNING" if bot_ok else "DOWN"
    trades, today = get_db_display()
    steps = dec(history)
    markets = get_market_hours()

    # Market status badges
    mkt_html = ""
    for mkt, mst in markets.items():
        if mkt == "UTC_time":
            mkt_html += f'<span style="font-size:10px;color:#64748b;margin-right:8px;">{mst}</span>'
        else:
            c = "#00ff88" if mst == "OPEN" else "#475569"
            mkt_html += f'<span style="font-size:10px;font-weight:700;color:{c};margin-right:8px;">{mkt}:{mst}</span>'

    trades_html = ""
    for t in trades:
        pnl = float(t.get("pnl", 0) or 0)
        c = "#00ff88" if pnl >= 0 else "#ef4444"
        trades_html += f"<tr><td>{t.get('symbol','')}</td><td>{t.get('side','')}</td><td>${float(t.get('price',0)):.2f}</td><td style='color:{c}'>${pnl:.2f}</td><td style='color:#475569;font-size:10px'>{t.get('reason','')}</td></tr>"

    sc2 = {"INVESTIGATING": "#f59e0b", "FOUND_ISSUE": "#ef4444", "FIX_PROPOSED": "#7c3aed",
            "VERIFIED": "#00ff88", "COMPLETE": "#00ff88"}.get(status, "#64748b")
    stuck = detect_stuck(steps, command) if command else False

    agent_html = ""
    if audit_result:
        # Colour code audit result
        verdict_color = "#00ff88" if "PASS" in audit_result[:200] else "#f59e0b" if "WARN" in audit_result[:200] else "#ef4444"
        agent_html = f"""<div style="background:#0a0a14;border:2px solid {verdict_color};border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:{verdict_color};text-transform:uppercase;margin-bottom:10px;">Dashboard Audit Report</div>
          <pre style="font-size:12px;color:#e2e8f0;white-space:pre-wrap;line-height:1.6;">{html.escape(audit_result)}</pre>
        </div>"""
    elif complete:
        ctx_badge = '<div style="background:#7c3aed;color:#fff;font-size:9px;font-weight:700;padding:3px 8px;border-radius:6px;margin-top:8px;display:inline-block;">CONTEXT UPDATED</div>' if ctx_updated else ''
        agent_html = f"""<div style="background:#0a1a0f;border:2px solid #00ff88;border-radius:10px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:24px;color:#00ff88;font-weight:700;margin-bottom:8px;">COMPLETE</div>
          <div style="font-size:13px;color:#94a3b8;">{html.escape(analysis)}</div>{ctx_badge}</div>"""
    elif analysis:
        badges = f'<span style="font-size:9px;color:#64748b;margin-left:8px;">Step {step_count}</span>'
        if compressed:
            badges += '<span style="font-size:9px;background:#1e1e2e;color:#64748b;padding:2px 6px;border-radius:4px;margin-left:6px;">COMPRESSED</span>'
        agent_html = f"""<div style="background:#0a1a0f;border:1px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:6px;">
            <div style="display:flex;align-items:center;">
              <span style="font-size:10px;font-weight:700;letter-spacing:1px;color:#00ff88;text-transform:uppercase;">Claude Analysis</span>{badges}
            </div>
            <div style="background:{sc2};color:#000;font-size:9px;font-weight:700;padding:3px 8px;border-radius:10px;">{status}</div>
          </div>
          <div style="font-size:13px;line-height:1.7;color:#e2e8f0;margin-bottom:12px;">{html.escape(analysis)}</div>
          {f'<div style="font-size:11px;color:#64748b;font-style:italic;margin-bottom:10px;">{html.escape(reason)}</div>' if reason else ''}
        </div>"""

        # FEEDBACK BOX
        agent_html += f"""<div style="background:#0d0d1a;border:1px solid #f59e0b;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#f59e0b;text-transform:uppercase;margin-bottom:8px;">Your Feedback (optional)</div>
          <form method="POST" action="/feedback">
            <input type="hidden" name="question" value="{html.escape(question)}">
            <input type="hidden" name="history" value="{html.escape(history)}">
            <input type="hidden" name="step_count" value="{step_count}">
            <textarea name="feedback" placeholder="e.g. Focus on the FTSE issue, ignore Binance for now..." style="width:100%;background:#0a0a0f;border:1px solid #f59e0b;border-radius:6px;color:#e2e8f0;font-family:'JetBrains Mono',monospace;font-size:12px;padding:8px;resize:none;height:55px;"></textarea>
            <button type="submit" style="display:block;width:100%;background:#f59e0b;border:none;border-radius:6px;color:#000;font-family:'Syne',sans-serif;font-weight:800;font-size:13px;padding:8px;cursor:pointer;margin-top:6px;">SEND FEEDBACK + CONTINUE</button>
          </form>
        </div>"""

        if command:
            if stuck:
                btn_color, btn_text = "#f59e0b", "APPROVE &amp; RUN (STUCK - will force new approach)"
            elif is_safe(command):
                btn_color, btn_text = "#00ff88", "APPROVE &amp; RUN"
            else:
                btn_color, btn_text = "#ef4444", "NOT ALLOWED (unsafe)"
            disabled = "" if is_safe(command) else "disabled"

            agent_html += f"""<div style="background:#0d0d1a;border:2px solid #7c3aed;border-radius:10px;padding:16px;margin-bottom:12px;">
              <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#7c3aed;text-transform:uppercase;margin-bottom:12px;">Next Action</div>
              <div style="background:#0a0a0f;border:1px solid #1e1e2e;border-radius:8px;padding:12px;margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
                  <span style="font-size:10px;color:#64748b;font-weight:700;">COMMAND</span>
                  <button onclick="copyText('cmdbox')" style="background:#1e1e2e;border:none;color:#94a3b8;font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;">COPY</button>
                </div>
                <pre id="cmdbox" style="color:#00ff88;margin:0;font-size:12px;white-space:pre-wrap;">{html.escape(command)}</pre>
              </div>
              <form method="POST" action="/approve">
                <input type="hidden" name="command" value="{html.escape(command)}">
                <input type="hidden" name="question" value="{html.escape(question)}">
                <input type="hidden" name="history" value="{html.escape(history)}">
                <input type="hidden" name="step_count" value="{step_count}">
                {'<input type="hidden" name="stuck" value="1">' if stuck else ''}
                <button type="submit" {disabled} style="display:block;width:100%;background:{btn_color};border:none;border-radius:8px;color:#000;font-family:'Syne',sans-serif;font-weight:800;font-size:15px;padding:12px;cursor:pointer;">{btn_text}</button>
              </form>
            </div>"""

    cmd_html = ""
    if cmd_output:
        cmd_html = f"""<div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Output: {html.escape(cmd_run)}</div>
          <pre style="color:#94a3b8;font-size:11px;max-height:250px;overflow-y:auto;white-space:pre-wrap;">{html.escape(cmd_output)}</pre>
        </div>"""

    err_html = f'<div style="background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;padding:12px;margin-bottom:12px;color:#ef4444;font-size:13px;">{html.escape(error)}</div>' if error else ""

    hist_html = ""
    if steps:
        items = ""
        for i, s in enumerate(steps):
            rc = "#00ff88" if s["role"] == "assistant" else "#7c3aed"
            rl = "Claude" if s["role"] == "assistant" else "Input"
            txt = s["content"] if isinstance(s["content"], str) else str(s["content"])
            items += f"""<div style="border-left:2px solid {rc};padding:8px 12px;margin-bottom:6px;background:#0a0a0f;border-radius:0 6px 6px 0;">
              <div style="font-size:9px;color:{rc};font-weight:700;text-transform:uppercase;margin-bottom:3px;">Step {i+1} — {rl}</div>
              <pre style="font-size:10px;color:#475569;white-space:pre-wrap;">{html.escape(txt[:400])}{'...' if len(txt)>400 else ''}</pre>
            </div>"""
        hist_html = f"""<div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <details><summary style="cursor:pointer;font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;">Session History ({len(steps)} steps)</summary>
          <div style="margin-top:10px;">{items}</div></details></div>"""

    quick = ""
    for label, q in [
        ("Bot status", "Is the bot running and healthy? Check screen log and DB."),
        ("Real errors", "Any real errors in the logs? Ignore known cosmetic errors in context."),
        ("Positions", "Check current positions and P&L from the DB."),
        ("Near misses", "Analyse near misses - should we adjust MIN_SIGNAL_SCORE?"),
        ("Next steps", "What should I do next to move AlphaBot closer to live trading?"),
        ("Fix Binance", "The Binance 401 error is blocking crypto trades. Check .env for BINANCE keys and diagnose."),
        ("Fix FTSE", "FTSE scanning shows 0 qualified BUY every cycle. Diagnose and fix why no FTSE stocks qualify."),
    ]:
        quick += f"""<form method="POST" action="/ask" style="display:inline-block;margin:3px;">
          <input type="hidden" name="question" value="{html.escape(q)}">
          <button type="submit" style="background:#111118;border:1px solid #1e1e2e;color:#94a3b8;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;">{label}</button>
        </form>"""

    # Audit dashboard button
    quick += """<form method="POST" action="/audit" style="display:inline-block;margin:3px;">
      <button type="submit" style="background:#0a1a0f;border:2px solid #00ff88;color:#00ff88;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;font-weight:700;">Audit Dashboard</button>
    </form>"""

    safe_files = ["app/main.py", "app/dashboard.py", "core/config.py", "core/execution.py",
                  "core/risk.py", "data/analytics.py", "data/database.py", "start.sh", ".env"]
    file_btns = "".join([f"""<form method="POST" action="/file" style="display:inline-block;margin:3px;">
      <input type="hidden" name="filename" value="{f}">
      <input type="hidden" name="question" value="{html.escape(question)}">
      <input type="hidden" name="history" value="{html.escape(history)}">
      <button type="submit" style="background:#0a0a14;border:1px solid #1e1e2e;color:#64748b;font-family:'JetBrains Mono',monospace;font-size:10px;padding:6px 10px;border-radius:5px;cursor:pointer;">{f}</button>
    </form>""" for f in safe_files])

    ctx_content = load_context()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaBot Agent v7</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0a0a0f; color:#e2e8f0; font-family:'JetBrains Mono',monospace; padding:16px; }}
header {{ display:flex; align-items:center; gap:12px; margin-bottom:16px; padding-bottom:14px; border-bottom:1px solid #1e1e2e; flex-wrap:wrap; }}
.logo {{ font-family:'Syne',sans-serif; font-size:20px; font-weight:800; color:#00ff88; }}
.logo span {{ color:#64748b; }}
.v {{ font-size:10px; color:#7c3aed; font-weight:700; background:#1e1e2e; padding:2px 6px; border-radius:4px; margin-left:4px; }}
.badge {{ margin-left:auto; background:#111118; border:1px solid {sc}; color:{sc}; font-size:10px; font-weight:700; padding:4px 10px; border-radius:20px; }}
.card {{ background:#111118; border:1px solid #1e1e2e; border-radius:10px; padding:14px; margin-bottom:12px; }}
textarea {{ width:100%; background:#0a0a0f; border:1px solid #1e1e2e; border-radius:8px; color:#e2e8f0; font-family:'JetBrains Mono',monospace; font-size:13px; padding:10px 12px; resize:none; height:70px; }}
textarea:focus {{ outline:none; border-color:#00ff88; }}
.ask-btn {{ display:block; width:100%; background:#00ff88; border:none; border-radius:8px; color:#000; font-family:'Syne',sans-serif; font-weight:800; font-size:15px; padding:12px; cursor:pointer; margin-top:8px; }}
table {{ width:100%; border-collapse:collapse; font-size:11px; }}
th {{ color:#64748b; text-align:left; padding:4px 6px; border-bottom:1px solid #1e1e2e; }}
td {{ padding:5px 6px; border-bottom:1px solid #0f0f18; }}
details summary {{ cursor:pointer; color:#64748b; font-size:11px; text-transform:uppercase; font-weight:700; padding:4px 0; }}
.toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#00ff88; color:#000; font-weight:700; font-size:13px; padding:10px 20px; border-radius:8px; display:none; z-index:999; }}
</style>
</head>
<body>
<div class="toast" id="toast">Copied!</div>
<script>
function copyText(id) {{
  var t = document.getElementById(id);
  if (t) navigator.clipboard.writeText(t.innerText).then(function() {{
    var toast = document.getElementById('toast');
    toast.style.display = 'block';
    setTimeout(function() {{ toast.style.display = 'none'; }}, 1500);
  }});
}}
</script>
<header>
  <div class="logo">Alpha<span>Bot</span> AGENT<span class="v">v7</span></div>
  <div class="badge">&#9679; {st}</div>
  <div style="width:100%;margin-top:4px;">{mkt_html}</div>
</header>
{err_html}{agent_html}{cmd_html}{hist_html}
<div style="margin-bottom:12px;">{quick}</div>
<form method="POST" action="/ask">
  <div class="card">
    <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:10px;">Describe the problem</div>
    <textarea name="question" placeholder="e.g. Fix the Binance 401 error"></textarea>
    <button type="submit" class="ask-btn">START AGENT</button>
  </div>
</form>
<div class="card">
  <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:10px;">File Viewer</div>
  {file_btns}
</div>
<div class="card">
  <details>
    <summary style="color:#7c3aed;">Agent Context File</summary>
    <pre style="margin-top:8px;font-size:10px;color:#475569;white-space:pre-wrap;max-height:250px;overflow-y:auto;">{html.escape(ctx_content)}</pre>
  </details>
</div>
<div class="card">
  <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:10px;">Stats</div>
  <div style="display:inline-block;background:#0a0a0f;border:1px solid #1e1e2e;border-radius:6px;padding:8px 12px;margin:4px;text-align:center;">
    <div style="font-size:20px;font-weight:700;color:#00ff88;">{today}</div>
    <div style="font-size:10px;color:#64748b;margin-top:2px;">Trades Today</div>
  </div>
  <div style="display:inline-block;background:#0a0a0f;border:1px solid #1e1e2e;border-radius:6px;padding:8px 12px;margin:4px;text-align:center;">
    <div style="font-size:20px;font-weight:700;color:{sc};">{st}</div>
    <div style="font-size:10px;color:#64748b;margin-top:2px;">Bot</div>
  </div>
</div>
<div class="card">
  <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:10px;">Recent Trades</div>
  {'<table><tr><th>Symbol</th><th>Side</th><th>Price</th><th>P&L</th><th>Reason</th></tr>' + trades_html + '</table>' if trades_html else '<div style="color:#64748b;font-size:12px;">No trades</div>'}
</div>
<div class="card">
  <details>
    <summary>Screen Status</summary>
    <pre style="margin-top:8px;font-size:10px;color:#475569;">{html.escape(screen_status)}</pre>
  </details>
</div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home(response: Response):
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
        return RedirectResponse("/", status_code=303)
    log, screen, db = get_bot_context()
    context = load_context()
    messages = [{"role": "user", "content": f"CONTEXT:\n{context}\n\nLIVE LOGS:\n{log}\n\nSCREEN:\n{screen}\n\nDB:\n{json.dumps(db, default=str)[:800]}\n\nPROBLEM: {question}"}]
    response = call_claude(messages)
    status, analysis, command, reason, ctx_update = parse_response(response)
    steps = messages + [{"role": "assistant", "content": response}]
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
        return RedirectResponse("/", status_code=303)
    steps = dec(history)
    # Inject user feedback as a high-priority message
    steps.append({"role": "user", "content": f"USER FEEDBACK (prioritise this): {feedback}\n\nPlease acknowledge this direction and adjust your approach accordingly."})
    step_count += 1

    compressed = False
    if step_count % MAX_STEPS_BEFORE_COMPRESS == 0:
        steps = compress_history(steps, question)
        compressed = True

    response = call_claude(steps)
    status, analysis, command, reason, ctx_update = parse_response(response)
    steps.append({"role": "assistant", "content": response})
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
    note = "\n\n[NOTE: Same command tried multiple times. You MUST try a completely different approach now.]" if stuck else ""
    steps.append({"role": "user", "content": f"COMMAND: {command}\nOUTPUT:\n{output}{note}"})
    compressed = False
    if step_count % MAX_STEPS_BEFORE_COMPRESS == 0:
        steps = compress_history(steps, question)
        compressed = True
    response = call_claude(steps)
    status, analysis, next_cmd, reason, ctx_update = parse_response(response)
    steps.append({"role": "assistant", "content": response})
    complete = status == "COMPLETE"
    if complete and ctx_update:
        update_context(ctx_update)
    return store(dict(analysis=analysis, command=next_cmd, reason=reason, status=status,
                      cmd_output=output, cmd_run=command, question=question,
                      history=enc(steps), complete=complete, step_count=step_count,
                      compressed=compressed, ctx_updated=bool(ctx_update and complete)))


@app.post("/audit")
async def audit_dashboard():
    # Fetch all data in parallel
    dash_html = fetch_dashboard()
    db_snap = get_db_snapshot()
    log, screen, _ = get_bot_context()
    markets = get_market_hours()

    # Build audit prompt
    audit_input = f"""CURRENT TIME: {markets['UTC_time']}

MARKET STATUS:
- US (NYSE/NASDAQ): {markets['US']}
- FTSE (LSE): {markets['FTSE']}
- ASX: {markets['ASX']}
- Crypto (Binance/Coinbase): {markets['CRYPTO']} (note: Binance 401 error known - orders fail but scanning should work)

DATABASE SNAPSHOT:
{json.dumps(db_snap, default=str, indent=2)[:3000]}

BOT SCREEN LOG (last 80 lines):
{log}

DASHBOARD HTML (stripped):
{dash_html[:15000]}

Please perform the comprehensive audit as instructed."""

    result = call_claude(
        [{"role": "user", "content": audit_input}],
        system=AUDIT_SYSTEM
    )
    return store(dict(audit_result=result, question="Dashboard Audit"))


@app.post("/file")
async def view_file(filename: str = Form(""), question: str = Form(""), history: str = Form("")):
    safe_files = ["app/main.py", "app/dashboard.py", "core/config.py", "core/execution.py",
                  "core/risk.py", "data/analytics.py", "data/database.py", "start.sh", ".env"]
    if filename not in safe_files:
        return store(dict(error=f"File not allowed: {filename}", question=question, history=history))
    path = os.path.join(APP_PATH, filename)
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"Error: {e}"
    return store(dict(file_content=content, file_name=filename, question=question, history=history))
