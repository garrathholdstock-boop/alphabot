"""
AlphaBot Debug Agent v5
- Context compression every 10 steps (runs forever)
- Stuck detector (same command 3x = force new approach)
- Clean command execution
"""

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
import os, subprocess, sqlite3, json, anthropic, base64, html, re
from datetime import datetime

app = FastAPI()

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
DB_PATH = "/home/alphabot/app/alphabot.db"
APP_PATH = "/home/alphabot/app"
SCREEN_NAME = "alphabot"
MAX_STEPS_BEFORE_COMPRESS = 10

SYSTEM_PROMPT = """You are an expert autonomous debugging agent for AlphaBot trading bot.

ARCHITECTURE:
- VPS: 178.104.170.58, git root: /home/alphabot/app/
- Files: app/main.py (1154 lines), core/config.py, core/execution.py, core/risk.py
- DB: /home/alphabot/app/alphabot.db
- Screen session: alphabot

CONFIG: MIN_SIGNAL_SCORE=5, IS_LIVE=false, paper trading via Alpaca+IBKR+Binance

KNOWN COSMETIC ERRORS (ignore): Error 10089, Binance 401, BrokenPipeError

CRITICAL COMMAND SYNTAX - FOLLOW EXACTLY:
- Search in file: grep -n candidates /home/alphabot/app/app/main.py
  (NO quotes around search term)
- Read lines: sed -n 200,250p /home/alphabot/app/app/main.py
  (NO quotes around line range)
- Read full file: cat /home/alphabot/app/core/config.py
- Check env: grep BINANCE /home/alphabot/app/.env

RESPONSE FORMAT (always exactly this):

STATUS: [INVESTIGATING | FOUND_ISSUE | FIX_PROPOSED | VERIFIED | COMPLETE]

ANALYSIS:
[2-4 sentences. Cite specific line numbers and values from output.]

NEXT_COMMAND:
```
command here
```

REASON:
[1 sentence explaining what you expect to find]

RULES:
- One command per response
- NO quotes around grep search terms or sed line ranges
- If stuck (same command failing), try a completely different approach
- If COMPLETE, leave NEXT_COMMAND empty
- Max efficiency: grep to find line numbers, sed to read that section
"""

COMPRESS_PROMPT = """Summarise this debugging session so far into a compact paragraph.
Include: what problem we're solving, what we've tried, what we found, what the current theory is.
Be specific with file names and line numbers. Keep under 300 words."""

def clean(s):
    return s.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'").strip()

def is_safe(cmd):
    cmd = clean(cmd)
    blocked = ['rm ', 'wget ', 'curl ', 'chmod +x', '> /', 'dd ', 'mkfs', 'reboot', 'shutdown', 'passwd', 'sudo']
    if any(b in cmd for b in blocked):
        return False
    allowed = ['grep', 'cat ', 'head', 'tail', 'sed', 'wc ', 'ls ', 'find ', 'python3', 'ps ', 'df ', 'free ', 'screen', 'git ', 'sqlite3', 'echo ', 'env']
    for a in allowed:
        if cmd.startswith(a):
            return True
    return False

def run_cmd(cmd, timeout=15):
    cmd = clean(cmd)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return out[:4000] if len(out) > 4000 else out
    except Exception as e:
        return f"Error: {e}"

def get_context():
    try:
        subprocess.run(["/usr/bin/screen", "-S", SCREEN_NAME, "-X", "hardcopy", "/tmp/ab.txt"], timeout=5, capture_output=True)
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
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 3")
        db["trades"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?", (f"{datetime.now().strftime('%Y-%m-%d')}%",))
        db["today"] = cur.fetchone()["cnt"]
        conn.close()
    except Exception as e:
        db = {"error": str(e)}
    return log, screen, db

def call_claude(messages, system=None):
    if not CLAUDE_API_KEY:
        return "ERROR: CLAUDE_API_KEY not set."
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        r = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system or SYSTEM_PROMPT,
            messages=messages
        )
        return r.content[0].text
    except Exception as e:
        return f"Claude API error: {e}"

def compress_history(steps, question):
    """Summarise history into a single message to keep context small"""
    history_text = "\n\n".join([
        f"{'CLAUDE' if s['role']=='assistant' else 'VPS'}: {s['content'] if isinstance(s['content'], str) else str(s['content'])}"
        for s in steps
    ])
    summary = call_claude(
        [{"role": "user", "content": f"PROBLEM: {question}\n\nSESSION SO FAR:\n{history_text}"}],
        system=COMPRESS_PROMPT
    )
    return [{"role": "user", "content": f"[COMPRESSED HISTORY - {len(steps)} steps so far]\n{summary}\n\nContinue debugging. Problem: {question}"}]

def detect_stuck(steps, current_cmd, threshold=3):
    """Check if same command has been tried too many times"""
    if not current_cmd:
        return False
    recent_cmds = []
    for s in reversed(steps[-10:]):
        if s["role"] == "user" and "COMMAND:" in s.get("content", ""):
            match = re.search(r'COMMAND: (.+?)\n', s["content"])
            if match:
                recent_cmds.append(clean(match.group(1)))
    return recent_cmds.count(clean(current_cmd)) >= threshold

def parse_response(text):
    status = re.search(r'STATUS:\s*(\w+)', text)
    status = status.group(1) if status else "INVESTIGATING"
    analysis = re.search(r'ANALYSIS:\s*(.*?)(?=NEXT_COMMAND:|REASON:|$)', text, re.DOTALL)
    analysis = analysis.group(1).strip() if analysis else ""
    command = re.search(r'```(?:bash)?\s*(.*?)```', text, re.DOTALL)
    command = clean(command.group(1)) if command else ""
    reason = re.search(r'REASON:\s*(.*?)$', text, re.DOTALL)
    reason = reason.group(1).strip() if reason else ""
    return status, analysis, command, reason

def encode_steps(steps):
    return base64.b64encode(json.dumps(steps).encode()).decode()

def decode_steps(h):
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
        cur.execute("SELECT * FROM near_misses ORDER BY created_at DESC LIMIT 5")
        nms = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?", (f"{datetime.now().strftime('%Y-%m-%d')}%",))
        today = cur.fetchone()["cnt"]
        conn.close()
        return trades, nms, today
    except:
        return [], [], "?"

def render(analysis="", command="", reason="", status="", cmd_output="", cmd_run="",
           error="", question="", history="", complete=False, file_content="", file_name="",
           compressed=False, step_count=0):

    screen_status = run_cmd("/usr/bin/screen -ls")
    bot_ok = "alphabot" in screen_status
    sc = "#00ff88" if bot_ok else "#ef4444"
    st = "RUNNING" if bot_ok else "DOWN"
    trades, nms, today = get_db_display()
    steps = decode_steps(history)

    trades_html = ""
    for t in trades:
        pnl = float(t.get("pnl", 0) or 0)
        c = "#00ff88" if pnl >= 0 else "#ef4444"
        trades_html += f"<tr><td>{t.get('symbol','')}</td><td>{t.get('side','')}</td><td>${float(t.get('price',0)):.2f}</td><td style='color:{c}'>${pnl:.2f}</td><td style='color:#475569;font-size:10px'>{t.get('reason','')}</td></tr>"

    status_colors = {"INVESTIGATING": "#f59e0b", "FOUND_ISSUE": "#ef4444", "FIX_PROPOSED": "#7c3aed", "VERIFIED": "#00ff88", "COMPLETE": "#00ff88"}
    sc2 = status_colors.get(status, "#64748b")

    stuck = detect_stuck(steps, command) if command else False

    agent_html = ""
    if complete:
        agent_html = f"""<div style="background:#0a1a0f;border:2px solid #00ff88;border-radius:10px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:24px;color:#00ff88;font-weight:700;margin-bottom:8px;">COMPLETE</div>
          <div style="font-size:13px;color:#94a3b8;">{html.escape(analysis)}</div>
        </div>"""
    elif analysis:
        step_badge = f'<div style="font-size:9px;color:#64748b;margin-left:8px;">Step {step_count}</div>'
        compressed_badge = '<div style="font-size:9px;background:#1e1e2e;color:#64748b;padding:2px 6px;border-radius:4px;margin-left:6px;">COMPRESSED</div>' if compressed else ''
        agent_html = f"""<div style="background:#0a1a0f;border:1px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:6px;">
            <div style="display:flex;align-items:center;">
              <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#00ff88;text-transform:uppercase;">Claude Analysis</div>
              {step_badge}{compressed_badge}
            </div>
            <div style="background:{sc2};color:#000;font-size:9px;font-weight:700;padding:3px 8px;border-radius:10px;">{status}</div>
          </div>
          <div style="font-size:13px;line-height:1.7;color:#e2e8f0;margin-bottom:10px;">{html.escape(analysis)}</div>
          {f'<div style="font-size:11px;color:#64748b;font-style:italic;">{html.escape(reason)}</div>' if reason else ''}
        </div>"""

        if command:
            safe = is_safe(command)
            if stuck:
                btn_color = "#f59e0b"
                btn_text = "APPROVE &amp; RUN (WARNING: tried before - Claude will be forced to change approach)"
            elif safe:
                btn_color = "#00ff88"
                btn_text = "APPROVE &amp; RUN"
            else:
                btn_color = "#ef4444"
                btn_text = "NOT ALLOWED (unsafe)"
            disabled = "" if safe else "disabled"
            stuck_input = '<input type="hidden" name="stuck" value="1">' if stuck else ''

            agent_html += f"""<div style="background:#0d0d1a;border:2px solid #7c3aed;border-radius:10px;padding:16px;margin-bottom:12px;">
              <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#7c3aed;text-transform:uppercase;margin-bottom:12px;">Next Action</div>
              <div style="background:#0a0a0f;border:1px solid #1e1e2e;border-radius:8px;padding:12px;margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                  <span style="font-size:10px;color:#64748b;font-weight:700;">COMMAND</span>
                  <button onclick="copyText('cmdbox')" style="background:#1e1e2e;border:none;color:#94a3b8;font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;font-family:'JetBrains Mono',monospace;">COPY</button>
                </div>
                <pre id="cmdbox" style="color:#00ff88;margin:0;font-size:12px;white-space:pre-wrap;">{html.escape(command)}</pre>
              </div>
              <form method="POST" action="/approve">
                <input type="hidden" name="command" value="{html.escape(command)}">
                <input type="hidden" name="question" value="{html.escape(question)}">
                <input type="hidden" name="history" value="{html.escape(history)}">
                <input type="hidden" name="step_count" value="{step_count}">
                {stuck_input}
                <button type="submit" {disabled} style="display:block;width:100%;background:{btn_color};border:none;border-radius:8px;color:#000;font-family:'Syne',sans-serif;font-weight:800;font-size:15px;padding:12px;cursor:pointer;">{btn_text}</button>
              </form>
            </div>"""

    cmd_html = ""
    if cmd_output:
        cmd_html = f"""<div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Output: {html.escape(cmd_run)}</div>
          <pre style="color:#94a3b8;font-size:11px;max-height:250px;overflow-y:auto;white-space:pre-wrap;">{html.escape(cmd_output)}</pre>
        </div>"""

    file_html = ""
    if file_content:
        file_html = f"""<div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">File: {html.escape(file_name)}</div>
          <pre style="color:#94a3b8;font-size:10px;max-height:300px;overflow-y:auto;">{html.escape(file_content[:6000])}</pre>
        </div>"""

    err_html = f'<div style="background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;padding:12px;margin-bottom:12px;color:#ef4444;font-size:13px;">{html.escape(error)}</div>' if error else ""

    hist_html = ""
    if steps:
        items = ""
        for i, s in enumerate(steps):
            rc = "#00ff88" if s["role"] == "assistant" else "#7c3aed"
            rl = "Claude" if s["role"] == "assistant" else "VPS"
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
        ("Bot status", "Bot status - is it running and healthy?"),
        ("Real errors", "Any real errors in the logs? Ignore cosmetic issues."),
        ("Positions", "Check positions and P&L from the DB."),
        ("Near misses", "Near-miss analysis - should we adjust MIN_SIGNAL_SCORE?"),
        ("Next steps", "What should I do next to move AlphaBot closer to live?"),
        ("Fix Binance", "The Binance 401 error is blocking crypto trades. Check the .env file and diagnose why orders fail."),
    ]:
        quick += f"""<form method="POST" action="/ask" style="display:inline-block;margin:3px;">
          <input type="hidden" name="question" value="{q}">
          <button type="submit" style="background:#111118;border:1px solid #1e1e2e;color:#94a3b8;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;">{label}</button>
        </form>"""

    safe_files = ["app/main.py", "app/dashboard.py", "core/config.py", "core/execution.py", "core/risk.py", "data/analytics.py", "data/database.py", "start.sh", ".env"]
    file_btns = ""
    for f in safe_files:
        file_btns += f"""<form method="POST" action="/file" style="display:inline-block;margin:3px;">
          <input type="hidden" name="filename" value="{f}">
          <input type="hidden" name="question" value="{html.escape(question)}">
          <input type="hidden" name="history" value="{html.escape(history)}">
          <button type="submit" style="background:#0a0a14;border:1px solid #1e1e2e;color:#64748b;font-family:'JetBrains Mono',monospace;font-size:10px;padding:6px 10px;border-radius:5px;cursor:pointer;">{f}</button>
        </form>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaBot Agent v5</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0a0a0f; color:#e2e8f0; font-family:'JetBrains Mono',monospace; min-height:100vh; padding:16px; }}
header {{ display:flex; align-items:center; gap:12px; margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid #1e1e2e; }}
.logo {{ font-family:'Syne',sans-serif; font-size:22px; font-weight:800; color:#00ff88; }}
.logo span {{ color:#64748b; }}
.v {{ font-size:10px; color:#7c3aed; font-weight:700; background:#1e1e2e; padding:2px 6px; border-radius:4px; margin-left:4px; }}
.badge {{ margin-left:auto; background:#111118; border:1px solid {sc}; color:{sc}; font-size:10px; font-weight:700; padding:4px 10px; border-radius:20px; }}
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
.toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#00ff88; color:#000; font-weight:700; font-size:13px; padding:10px 20px; border-radius:8px; display:none; z-index:999; }}
</style>
</head>
<body>
<div class="toast" id="toast">Copied!</div>
<script>
function copyText(id) {{
  const t = document.getElementById(id);
  if (t) navigator.clipboard.writeText(t.innerText).then(() => {{
    const toast = document.getElementById('toast');
    toast.style.display = 'block';
    setTimeout(() => toast.style.display = 'none', 1500);
  }});
}}
</script>
<header>
  <div class="logo">Alpha<span>Bot</span> AGENT<span class="v">v5</span></div>
  <div class="badge">&#9679; {st}</div>
</header>
{err_html}{agent_html}{cmd_html}{file_html}{hist_html}
<div style="margin-bottom:12px;">{quick}</div>
<form method="POST" action="/ask">
  <div class="card">
    <div class="card-title">Describe the problem</div>
    <textarea name="question" placeholder="e.g. Fix the Binance 401 error">{html.escape(question)}</textarea>
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
    <div class="stat"><div class="stat-val">{today}</div><div class="stat-lbl">Trades Today</div></div>
    <div class="stat"><div class="stat-val" style="color:{sc};">{st}</div><div class="stat-lbl">Bot</div></div>
  </div>
</div>
<div class="card">
  <div class="card-title">Recent Trades</div>
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
async def home():
    return render()

@app.post("/ask", response_class=HTMLResponse)
async def ask(question: str = Form("")):
    if not question.strip():
        return render(error="Please enter a question.")
    log, screen, db = get_context()
    messages = [{"role": "user", "content": f"LOGS:\n{log}\n\nSCREEN:\n{screen}\n\nDB:\n{json.dumps(db, default=str)[:1000]}\n\nPROBLEM: {question}"}]
    response = call_claude(messages)
    status, analysis, command, reason = parse_response(response)
    steps = messages + [{"role": "assistant", "content": response}]
    h = encode_steps(steps)
    return render(analysis=analysis, command=command, reason=reason, status=status,
                  question=question, history=h, complete=(status == "COMPLETE"), step_count=1)

@app.post("/approve", response_class=HTMLResponse)
async def approve(command: str = Form(""), question: str = Form(""),
                  history: str = Form(""), step_count: int = Form(0),
                  stuck: str = Form("")):
    command = clean(command)
    steps = decode_steps(history)

    if not is_safe(command):
        return render(error=f"Command blocked: {command}", question=question,
                      history=history, step_count=step_count)

    output = run_cmd(command)
    step_count += 1

    # If stuck, add a forced direction message
    if stuck:
        steps.append({"role": "user", "content": f"COMMAND: {command}\nOUTPUT:\n{output}\n\n[AGENT NOTE: This command has been tried multiple times. You MUST try a completely different approach now.]"})
    else:
        steps.append({"role": "user", "content": f"COMMAND: {command}\nOUTPUT:\n{output}"})

    # Compress context every N steps to allow infinite loops
    compressed = False
    if step_count % MAX_STEPS_BEFORE_COMPRESS == 0:
        steps = compress_history(steps, question)
        compressed = True

    response = call_claude(steps)
    status, analysis, next_cmd, reason = parse_response(response)
    steps.append({"role": "assistant", "content": response})
    h = encode_steps(steps)

    return render(analysis=analysis, command=next_cmd, reason=reason, status=status,
                  cmd_output=output, cmd_run=command, question=question, history=h,
                  complete=(status == "COMPLETE"), step_count=step_count, compressed=compressed)

@app.post("/file", response_class=HTMLResponse)
async def view_file(filename: str = Form(""), question: str = Form(""), history: str = Form("")):
    safe_files = ["app/main.py", "app/dashboard.py", "core/config.py", "core/execution.py",
                  "core/risk.py", "data/analytics.py", "data/database.py", "start.sh", ".env"]
    if filename not in safe_files:
        return render(error=f"File not allowed: {filename}", question=question, history=history)
    path = os.path.join(APP_PATH, filename)
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"Error: {e}"
    return render(file_content=content, file_name=filename, question=question, history=history)
