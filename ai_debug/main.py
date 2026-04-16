"""
AlphaBot Debug Agent v3
Full agentic loop: diagnose -> propose -> approve -> run -> re-analyze -> repeat
"""

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
import os, subprocess, sqlite3, json, anthropic
from datetime import datetime

app = FastAPI()

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
DB_PATH        = "/home/alphabot/app/alphabot.db"
APP_PATH       = "/home/alphabot/app"
SCREEN_NAME    = "alphabot"

ALLOWED_PREFIXES = [
    "grep ", "cat ", "head ", "tail ", "wc ", "ls ", "ps ",
    "df ", "free ", "screen -ls", "git log", "git status",
    "git diff", "sqlite3 ", "echo ", "env", "python3 -c",
]

SAFE_FILES = [
    "app/main.py", "app/dashboard.py", "core/config.py",
    "core/execution.py", "core/risk.py", "data/analytics.py",
    "data/database.py", "start.sh", ".env",
]

SYSTEM_PROMPT = """You are an expert autonomous debugging agent for AlphaBot, an automated trading bot.

ARCHITECTURE:
- VPS: 178.104.170.58, git root: /home/alphabot/app/
- Start: bash /home/alphabot/start.sh -> screen "alphabot"
- Dashboard: port 8080, debug portal: port 8000
- DB: /home/alphabot/app/alphabot.db
- Files: app/main.py, core/config.py, core/execution.py, core/risk.py

CONFIG:
- MIN_SIGNAL_SCORE=5 (raise to 7 before live)
- IS_LIVE=false (paper trading)
- Brokers: Alpaca (US paper), IBKR (stops+ASX+FTSE), Binance (401 error)

KNOWN COSMETIC ERRORS (ignore):
- Error 10089 HOOD/SOFI/FCEL -- market data subscription
- Binance POST 401 -- API permissions not set
- BrokenPipeError in dashboard -- client disconnected

YOUR JOB:
You are in an agentic loop. Each turn you either:
1. DIAGNOSE - identify the issue from logs/output
2. PROPOSE - suggest the next command to run
3. VERIFY - confirm the fix worked from command output
4. COMPLETE - declare the issue resolved

RESPONSE FORMAT (always use exactly this structure):

STATUS: [INVESTIGATING | FOUND_ISSUE | FIX_PROPOSED | VERIFIED | COMPLETE]

ANALYSIS:
[Your diagnosis in 2-4 sentences. Be specific. Reference actual values from logs.]

NEXT_COMMAND:
```
[single shell command to run next, or empty if COMPLETE]
```

REASON:
[Why this command, what you expect to see, 1-2 sentences]

Rules:
- One command at a time only
- Commands must be read-only diagnostics OR safe config changes
- Never suggest restarting the bot mid-diagnosis
- If issue is resolved, set STATUS: COMPLETE and leave NEXT_COMMAND empty
- If you need a file, use: cat /home/alphabot/app/[filepath]
- If you need env vars, use: grep BINANCE /home/alphabot/app/.env
"""

def run_cmd(cmd, timeout=15):
    try:
        # Clean smart quotes
        cmd = cmd.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return out[:3000] if len(out) > 3000 else out
    except Exception as e:
        return f"Error running command: {e}"

def is_safe(cmd):
    cmd = cmd.strip().replace('\u201c', '"').replace('\u201d', '"')
    for prefix in ALLOWED_PREFIXES:
        if cmd.startswith(prefix) or cmd == prefix.strip():
            return True
    return False

def get_context():
    try:
        r = subprocess.run(["/usr/bin/screen", "-S", SCREEN_NAME, "-X", "hardcopy", "/tmp/ab_screen.txt"],
                           timeout=5, capture_output=True)
        if os.path.exists("/tmp/ab_screen.txt"):
            with open("/tmp/ab_screen.txt", "r", errors="replace") as f:
                log = "".join(f.readlines()[-80:])
        else:
            log = "No screen log"
    except:
        log = "Screen error"

    screen_status = run_cmd("/usr/bin/screen -ls")

    db = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 3")
        db["trades"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM near_misses ORDER BY created_at DESC LIMIT 3")
        db["near_misses"] = [dict(r) for r in cur.fetchall()]
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?", (f"{today}%",))
        db["trades_today"] = cur.fetchone()["cnt"]
        conn.close()
    except Exception as e:
        db = {"error": str(e)}

    return log, screen_status, db

def call_claude(messages):
    if not CLAUDE_API_KEY:
        return "ERROR: CLAUDE_API_KEY not set."
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=messages
        )
        return response.content[0].text
    except Exception as e:
        return f"Claude API error: {e}"

def parse_response(text):
    import re
    status = ""
    analysis = ""
    command = ""
    reason = ""

    m = re.search(r'STATUS:\s*(\w+)', text)
    if m:
        status = m.group(1)

    m = re.search(r'ANALYSIS:\s*(.*?)(?=NEXT_COMMAND:|REASON:|$)', text, re.DOTALL)
    if m:
        analysis = m.group(1).strip()

    m = re.search(r'```(?:bash)?\s*(.*?)```', text, re.DOTALL)
    if m:
        command = m.group(1).strip()

    m = re.search(r'REASON:\s*(.*?)$', text, re.DOTALL)
    if m:
        reason = m.group(1).strip()

    return status, analysis, command, reason

def get_db_display():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 5")
        trades = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM near_misses ORDER BY created_at DESC LIMIT 5")
        nms = [dict(r) for r in cur.fetchall()]
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?", (f"{today}%",))
        today_count = cur.fetchone()["cnt"]
        conn.close()
        return trades, nms, today_count
    except:
        return [], [], "?"

def render(analysis="", command="", reason="", status="", cmd_output="",
           cmd_run="", error="", question="", history=None, file_content="", file_name="", complete=False):

    screen_status = run_cmd("/usr/bin/screen -ls")
    bot_running = "alphabot" in screen_status
    status_color = "#00ff88" if bot_running else "#ef4444"
    status_text = "RUNNING" if bot_running else "DOWN"

    trades, nms, today_count = get_db_display()

    trades_html = ""
    for t in trades:
        pnl = float(t.get("pnl", 0) or 0)
        c = "#00ff88" if pnl >= 0 else "#ef4444"
        trades_html += f"<tr><td>{t.get('symbol','')}</td><td>{t.get('side','')}</td><td>${float(t.get('price',0)):.2f}</td><td style='color:{c}'>${pnl:.2f}</td><td style='color:#475569;font-size:10px'>{t.get('reason','')}</td></tr>"

    nm_html = ""
    for n in nms:
        nm_html += f"<tr><td>{n.get('symbol','')}</td><td>{n.get('score','')}</td><td>{n.get('reason','')}</td></tr>"

    # History panel
    history_html = ""
    if history:
        steps = json.loads(history)
        for i, step in enumerate(steps):
            role_color = "#00ff88" if step["role"] == "assistant" else "#7c3aed"
            role_label = "Claude" if step["role"] == "assistant" else "VPS Output"
            text = step["content"]
            if isinstance(text, list):
                text = " ".join([c.get("text","") for c in text if isinstance(c, dict)])
            history_html += f"""
            <div style="border-left:2px solid {role_color};padding:8px 12px;margin-bottom:8px;background:#0a0a0f;border-radius:0 6px 6px 0;">
              <div style="font-size:9px;color:{role_color};font-weight:700;text-transform:uppercase;margin-bottom:4px;">Step {i+1} — {role_label}</div>
              <pre style="font-size:11px;color:#94a3b8;white-space:pre-wrap;word-break:break-all;">{text[:600]}{'...' if len(text)>600 else ''}</pre>
            </div>"""

    # Agent action panel
    agent_html = ""
    if analysis and not complete:
        status_badge_color = {"INVESTIGATING": "#f59e0b", "FOUND_ISSUE": "#ef4444",
                               "FIX_PROPOSED": "#7c3aed", "VERIFIED": "#00ff88"}.get(status, "#64748b")
        agent_html = f"""
        <div style="background:#0a1a0f;border:1px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
            <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#00ff88;text-transform:uppercase;">Claude Analysis</div>
            <div style="background:{status_badge_color};color:#000;font-size:9px;font-weight:700;padding:3px 8px;border-radius:10px;">{status}</div>
          </div>
          <div style="font-size:13px;line-height:1.7;color:#e2e8f0;margin-bottom:12px;">{analysis}</div>
          {f'<div style="font-size:11px;color:#64748b;font-style:italic;margin-bottom:10px;">{reason}</div>' if reason else ''}
        </div>"""

        if command:
            safe = is_safe(command)
            btn_color = "#00ff88" if safe else "#ef4444"
            btn_text = "APPROVE & RUN" if safe else "NOT ALLOWED (unsafe)"
            btn_disabled = "" if safe else "disabled"
            agent_html += f"""
        <div style="background:#0d0d1a;border:2px solid #7c3aed;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#7c3aed;text-transform:uppercase;margin-bottom:12px;">Next Action</div>
          <div style="background:#0a0a0f;border:1px solid #1e1e2e;border-radius:8px;padding:12px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
              <span style="font-size:10px;color:#64748b;font-weight:700;">COMMAND</span>
              <button onclick="copyText('cmdbox')" style="background:#1e1e2e;border:none;color:#94a3b8;font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;font-family:'JetBrains Mono',monospace;">COPY</button>
            </div>
            <pre id="cmdbox" style="color:#00ff88;margin:0;font-size:12px;white-space:pre-wrap;">{command}</pre>
          </div>
          <form method="POST" action="/approve">
            <input type="hidden" name="command" value="{command}">
            <input type="hidden" name="question" value="{question}">
            <input type="hidden" name="history" value='{history or "[]"}'>
            <button type="submit" {btn_disabled} style="display:block;width:100%;background:{btn_color};border:none;border-radius:8px;color:#000;font-family:'Syne',sans-serif;font-weight:800;font-size:15px;padding:12px;cursor:pointer;">
              {btn_text}
            </button>
          </form>
        </div>"""

    if complete:
        agent_html = f"""
        <div style="background:#0a1a0f;border:2px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:12px;text-align:center;">
          <div style="font-size:24px;margin-bottom:8px;">COMPLETE</div>
          <div style="font-size:13px;color:#00ff88;font-weight:700;">Issue resolved</div>
          <div style="font-size:13px;color:#94a3b8;margin-top:8px;">{analysis}</div>
        </div>"""

    cmd_output_html = ""
    if cmd_output:
        cmd_output_html = f"""
        <div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Output: <span style="color:#94a3b8;">{cmd_run}</span></div>
          <pre style="color:#94a3b8;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap;">{cmd_output}</pre>
        </div>"""

    file_html = ""
    if file_content:
        file_html = f"""
        <div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">File: {file_name}</div>
          <pre style="color:#94a3b8;font-size:10px;max-height:300px;overflow-y:auto;">{file_content[:5000]}</pre>
        </div>"""

    error_html = f'<div style="background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;padding:12px;margin-bottom:12px;color:#ef4444;font-size:13px;">{error}</div>' if error else ""

    history_section = ""
    if history_html:
        history_section = f"""
        <div style="background:#111118;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <details>
            <summary style="cursor:pointer;font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;">Session History ({len(json.loads(history)) if history else 0} steps)</summary>
            <div style="margin-top:10px;">{history_html}</div>
          </details>
        </div>"""

    quick_btns = ""
    for label, q in [
        ("Bot status", "Bot status -- is it running and healthy?"),
        ("Real errors", "Any real errors in the logs? Ignore cosmetic issues."),
        ("Positions", "Check positions and P&L from the DB."),
        ("Near misses", "Near-miss analysis -- should we adjust MIN_SIGNAL_SCORE?"),
        ("Next steps", "What should I do next to move AlphaBot closer to live?"),
        ("Fix Binance", "The Binance 401 error is blocking crypto trades. Diagnose and fix it step by step."),
    ]:
        quick_btns += f"""<form method="POST" action="/ask" style="display:inline-block;margin:3px;">
          <input type="hidden" name="question" value="{q}">
          <button type="submit" style="background:#111118;border:1px solid #1e1e2e;color:#94a3b8;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;">{label}</button>
        </form>"""

    file_btns = ""
    for f in SAFE_FILES:
        file_btns += f"""<form method="POST" action="/file" style="display:inline-block;margin:3px;">
          <input type="hidden" name="filename" value="{f}">
          <input type="hidden" name="question" value="{question}">
          <input type="hidden" name="history" value='{history or "[]"}'>
          <button type="submit" style="background:#0a0a14;border:1px solid #1e1e2e;color:#64748b;font-family:'JetBrains Mono',monospace;font-size:10px;padding:6px 10px;border-radius:5px;cursor:pointer;">{f}</button>
        </form>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaBot Debug Agent</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#0a0a0f; color:#e2e8f0; font-family:'JetBrains Mono',monospace; min-height:100vh; padding:16px; }}
  header {{ display:flex; align-items:center; gap:12px; margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid #1e1e2e; }}
  .logo {{ font-family:'Syne',sans-serif; font-size:22px; font-weight:800; color:#00ff88; letter-spacing:-0.5px; }}
  .logo span {{ color:#64748b; }}
  .badge {{ margin-left:auto; background:#111118; border:1px solid {status_color}; color:{status_color}; font-size:10px; font-weight:700; padding:4px 10px; border-radius:20px; letter-spacing:1px; }}
  .card {{ background:#111118; border:1px solid #1e1e2e; border-radius:10px; padding:14px; margin-bottom:12px; }}
  .card-title {{ font-size:10px; font-weight:700; letter-spacing:1px; color:#64748b; text-transform:uppercase; margin-bottom:10px; }}
  textarea {{ width:100%; background:#0a0a0f; border:1px solid #1e1e2e; border-radius:8px; color:#e2e8f0; font-family:'JetBrains Mono',monospace; font-size:13px; padding:10px 12px; resize:none; height:70px; }}
  textarea:focus {{ outline:none; border-color:#00ff88; }}
  .ask-btn {{ display:block; width:100%; background:#00ff88; border:none; border-radius:8px; color:#000; font-family:'Syne',sans-serif; font-weight:800; font-size:15px; padding:12px; cursor:pointer; margin-top:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:11px; }}
  th {{ color:#64748b; text-align:left; padding:4px 6px; border-bottom:1px solid #1e1e2e; font-weight:700; }}
  td {{ padding:5px 6px; border-bottom:1px solid #0f0f18; }}
  .stat {{ display:inline-block; background:#0a0a0f; border:1px solid #1e1e2e; border-radius:6px; padding:8px 12px; margin:4px; text-align:center; min-width:80px; }}
  .stat-val {{ font-size:20px; font-weight:700; color:#00ff88; }}
  .stat-lbl {{ font-size:10px; color:#64748b; margin-top:2px; }}
  details summary {{ cursor:pointer; color:#64748b; font-size:11px; letter-spacing:1px; text-transform:uppercase; font-weight:700; padding:4px 0; }}
  .toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#00ff88; color:#000; font-weight:700; font-size:13px; padding:10px 20px; border-radius:8px; display:none; z-index:999; font-family:'Syne',sans-serif; }}
</style>
</head>
<body>
<div class="toast" id="toast">Copied!</div>
<script>
function copyText(id) {{
  const t = document.getElementById(id);
  if (t) {{
    navigator.clipboard.writeText(t.innerText).then(() => {{
      const toast = document.getElementById('toast');
      toast.style.display = 'block';
      setTimeout(() => toast.style.display = 'none', 1500);
    }});
  }}
}}
</script>

<header>
  <div class="logo">Alpha<span>Bot</span> AGENT</div>
  <div class="badge">&#9679; {status_text}</div>
</header>

{error_html}
{agent_html}
{cmd_output_html}
{file_html}
{history_section}

<div style="margin-bottom:12px;">{quick_btns}</div>

<form method="POST" action="/ask">
  <div class="card">
    <div class="card-title">Describe the problem</div>
    <textarea name="question" placeholder="e.g. Fix the Binance 401 error">{question}</textarea>
    <button type="submit" class="ask-btn">START AGENT</button>
  </div>
</form>

<div class="card">
  <div class="card-title">File Viewer</div>
  <div>{file_btns}</div>
</div>

<div class="card">
  <div class="card-title">Stats</div>
  <div>
    <div class="stat"><div class="stat-val">{today_count}</div><div class="stat-lbl">Trades Today</div></div>
    <div class="stat"><div class="stat-val">{len(nms)}</div><div class="stat-lbl">Near Misses</div></div>
    <div class="stat"><div class="stat-val" style="color:{status_color};">{status_text}</div><div class="stat-lbl">Bot</div></div>
  </div>
</div>

<div class="card">
  <div class="card-title">Recent Trades</div>
  {'<table><tr><th>Symbol</th><th>Side</th><th>Price</th><th>P&L</th><th>Reason</th></tr>' + trades_html + '</table>' if trades_html else '<div style="color:#64748b;font-size:12px;">No trades</div>'}
</div>

<div class="card">
  <div class="card-title">Near Misses</div>
  {'<table><tr><th>Symbol</th><th>Score</th><th>Reason</th></tr>' + nm_html + '</table>' if nm_html else '<div style="color:#64748b;font-size:12px;">None</div>'}
</div>

<div class="card">
  <details>
    <summary>Screen Status</summary>
    <pre style="margin-top:8px;font-size:10px;color:#475569;">{screen_status}</pre>
  </details>
</div>

</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return render()

@app.post("/ask", response_class=HTMLResponse)
async def ask(question: str = Form("")):
    if not question.strip():
        return render(error="Please enter a question.")
    log, screen_status, db = get_context()
    messages = [{
        "role": "user",
        "content": f"LOGS:\n{log}\n\nSCREEN:\n{screen_status}\n\nDB:\n{json.dumps(db, default=str)[:1500]}\n\nPROBLEM: {question}"
    }]
    response = call_claude(messages)
    status, analysis, command, reason = parse_response(response)
    history = json.dumps(messages + [{"role": "assistant", "content": response}])
    complete = status == "COMPLETE"
    return render(analysis=analysis, command=command, reason=reason,
                  status=status, question=question, history=history, complete=complete)

@app.post("/approve", response_class=HTMLResponse)
async def approve(command: str = Form(""), question: str = Form(""),
                  history: str = Form("[]")):
    command = command.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
    if not is_safe(command):
        return render(error=f"Command blocked: {command}", question=question, history=history)
    output = run_cmd(command)
    history_list = json.loads(history)
    history_list.append({"role": "user", "content": f"COMMAND RUN: {command}\nOUTPUT:\n{output}"})
    response = call_claude(history_list)
    status, analysis, next_command, reason = parse_response(response)
    history_list.append({"role": "assistant", "content": response})
    complete = status == "COMPLETE"
    return render(analysis=analysis, command=next_command, reason=reason,
                  status=status, cmd_output=output, cmd_run=command,
                  question=question, history=json.dumps(history_list), complete=complete)

@app.post("/file", response_class=HTMLResponse)
async def view_file(filename: str = Form(""), question: str = Form(""),
                    history: str = Form("[]")):
    if filename not in SAFE_FILES:
        return render(error=f"File not allowed: {filename}", question=question, history=history)
    path = os.path.join(APP_PATH, filename)
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"Error: {e}"
    return render(file_content=content, file_name=filename, question=question, history=history)
