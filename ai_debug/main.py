"""
AlphaBot Debug Portal v2 — powered by Claude API
- Auto file reading
- Command execution with approval
- 1-click copy for commands
Run: uvicorn main:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
import os, subprocess, sqlite3, json, anthropic
from datetime import datetime

app = FastAPI()

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
DB_PATH        = "/home/alphabot/app/alphabot.db"
APP_PATH       = "/home/alphabot/app"
SCREEN_NAME    = "alphabot"

ALLOWED_COMMANDS = [
    "screen -ls",
    "grep",
    "cat ",
    "head ",
    "tail ",
    "wc ",
    "ls ",
    "sqlite3",
    "git log",
    "git status",
    "git diff",
    "ps aux",
    "df -h",
    "free -m",
]

SAFE_FILES = [
    "app/main.py",
    "app/dashboard.py",
    "core/config.py",
    "core/execution.py",
    "core/risk.py",
    "data/analytics.py",
    "data/database.py",
    "start.sh",
    ".env",
]

ALPHABOT_CONTEXT = """
You are an expert debugging assistant for AlphaBot, an automated multi-market day trading bot.
You have direct access to VPS logs, database, and source files via this debug portal.

ARCHITECTURE:
- VPS: 178.104.170.58 (Hetzner), user: root
- Git root: /home/alphabot/app/ (branch: master)
- Start: bash /home/alphabot/start.sh -> screen session "alphabot"
- Dashboard: port 8080, debug portal: port 8000
- DB: /home/alphabot/app/alphabot.db

KEY FILES:
- app/main.py       -- scan/execution loop, capital efficiency ~line 253/273
- core/config.py    -- all config constants and API keys
- core/execution.py -- place_order, IBKR stop placement
- core/risk.py      -- score_signal, check_stop_losses

CURRENT CONFIG:
- MIN_SIGNAL_SCORE=5 (raise to 7 before live)
- MAX_POSITIONS=3 per market, MAX_TOTAL_POSITIONS=15
- IS_LIVE=false (paper trading)
- Brokers: Alpaca (US paper), IBKR (stops+ASX+FTSE), Binance (401 error - permissions not set)

KNOWN COSMETIC ERRORS (ignore these):
- Error 10089 HOOD/SOFI/FCEL -- market data subscription
- Binance POST 401 -- API permissions not set, cosmetic
- BrokenPipeError in dashboard -- client disconnected, harmless

P1 BEFORE LIVE: Raise MIN_SIGNAL_SCORE to 7, fix Binance API permissions.

RESPONSE FORMAT:
Always end your response with a COMMANDS section if any action is needed.
Format commands exactly like this so they can be extracted:
COMMANDS:
```
command here
```
If multiple commands, put each in its own block. Keep diagnosis concise - owner reads on mobile.
"""

QUICK_QUESTIONS = [
    ("Bot status", "Bot status -- is it running and healthy?"),
    ("Real errors", "Any real errors in the logs? Ignore known cosmetic issues."),
    ("Positions", "Check positions and P&L from the DB. Any issues?"),
    ("Near misses", "Near-miss analysis -- should we adjust MIN_SIGNAL_SCORE?"),
    ("Next steps", "What should I do next to move AlphaBot closer to live trading?"),
    ("Review config", "Review current config. Any settings that need changing before live?"),
]

def run_cmd(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"Error: {e}"

def get_screen_log():
    try:
        subprocess.run(
            ["/usr/bin/screen", "-S", SCREEN_NAME, "-X", "hardcopy", "/tmp/alphabot_screen.txt"],
            timeout=5, capture_output=True
        )
        if os.path.exists("/tmp/alphabot_screen.txt"):
            with open("/tmp/alphabot_screen.txt", "r", errors="replace") as f:
                return "".join(f.readlines()[-150:])
        return "No screen log"
    except Exception as e:
        return f"Screen error: {e}"

def get_screen_status():
    return run_cmd("/usr/bin/screen -ls")

def get_file(filename):
    path = os.path.join(APP_PATH, filename)
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def get_db_summary():
    if not os.path.exists(DB_PATH):
        return {"error": f"DB not found"}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        result = {}
        try:
            cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 5")
            result["recent_trades"] = [dict(r) for r in cur.fetchall()]
        except:
            result["recent_trades"] = []
        try:
            cur.execute("SELECT * FROM near_misses ORDER BY created_at DESC LIMIT 5")
            result["near_misses"] = [dict(r) for r in cur.fetchall()]
        except:
            result["near_misses"] = []
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE created_at LIKE ?", (f"{today}%",))
            result["trades_today"] = cur.fetchone()["cnt"]
        except:
            result["trades_today"] = "unknown"
        conn.close()
        return result
    except Exception as e:
        return {"error": str(e)}

def extract_commands(text):
    """Extract commands from Claude response code blocks"""
    import re
    blocks = re.findall(r'```(?:bash)?\n?(.*?)```', text, re.DOTALL)
    return [b.strip() for b in blocks if b.strip()]

def call_claude(question, log, screen_status, db, extra_files=""):
    if not CLAUDE_API_KEY:
        return "ERROR: CLAUDE_API_KEY not set."
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        context = f"SCREEN STATUS:\n{screen_status}\n\nRECENT LOG:\n{log[-6000:]}\n\nDB:\n{json.dumps(db, default=str)[:2000]}"
        if extra_files:
            context += f"\n\nFILES:\n{extra_files[:4000]}"
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            system=ALPHABOT_CONTEXT,
            messages=[{"role": "user", "content": f"CONTEXT:\n{context}\n\nQUESTION: {question}"}]
        )
        return message.content[0].text
    except Exception as e:
        return f"Claude API error: {e}"

def is_safe_command(cmd):
    cmd = cmd.strip()
    for allowed in ALLOWED_COMMANDS:
        if cmd.startswith(allowed):
            return True
    return False

def render_page(answer="", question="", error="", cmd_output="", cmd_run="", file_content="", file_name=""):
    screen_status = get_screen_status()
    bot_running = "alphabot" in screen_status
    status_color = "#00ff88" if bot_running else "#ef4444"
    status_text = "RUNNING" if bot_running else "DOWN"

    db = get_db_summary()
    trades_today = db.get("trades_today", "?")
    recent_trades = db.get("recent_trades", [])
    near_misses = db.get("near_misses", [])

    trades_html = ""
    for t in recent_trades:
        pnl = t.get("pnl", 0) or 0
        pnl_color = "#00ff88" if float(pnl) >= 0 else "#ef4444"
        trades_html += f"""<tr>
          <td>{t.get('symbol','')}</td><td>{t.get('side','')}</td>
          <td>{t.get('qty','')}</td><td>${float(t.get('price',0)):.2f}</td>
          <td style="color:{pnl_color}">${float(pnl):.2f}</td>
          <td style="color:#64748b;font-size:10px">{t.get('reason','')}</td>
        </tr>"""

    nm_html = ""
    for n in near_misses:
        nm_html += f"""<tr>
          <td>{n.get('symbol','')}</td><td>{n.get('score','')}</td>
          <td>{n.get('reason','')}</td>
          <td style="color:#64748b;font-size:10px">{str(n.get('created_at',''))[:16]}</td>
        </tr>"""

    # Extract commands from answer for the action panel
    commands = extract_commands(answer) if answer else []
    
    cmd_blocks = ""
    for i, cmd in enumerate(commands):
        cmd_id = f"cmd_{i}"
        cmd_blocks += f"""
        <div style="background:#0a0a0f;border:1px solid #1e1e2e;border-radius:8px;padding:12px;margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-size:10px;color:#64748b;font-weight:700;text-transform:uppercase;">Command {i+1}</span>
            <button onclick="copyCmd('{cmd_id}')" style="background:#1e1e2e;border:none;color:#94a3b8;font-size:10px;padding:4px 10px;border-radius:4px;cursor:pointer;font-family:'JetBrains Mono',monospace;">COPY</button>
          </div>
          <pre id="{cmd_id}" style="color:#00ff88;margin:0;font-size:12px;white-space:pre-wrap;">{cmd}</pre>
          <form method="POST" action="/run" style="margin-top:8px;">
            <input type="hidden" name="cmd" value="{cmd}">
            <input type="hidden" name="question" value="{question}">
            <button type="submit" style="background:#7c3aed;border:none;color:#fff;font-size:11px;padding:5px 14px;border-radius:5px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-weight:700;">RUN ON VPS</button>
          </form>
        </div>"""

    answer_section = ""
    if answer:
        answer_section = f"""
        <div style="background:#0a1a0f;border:1px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#00ff88;text-transform:uppercase;margin-bottom:10px;">Claude Analysis</div>
          <div style="font-size:13px;line-height:1.8;white-space:pre-wrap;color:#e2e8f0;">{answer}</div>
        </div>"""
        
        if cmd_blocks:
            answer_section += f"""
        <div style="background:#0d0d1a;border:1px solid #7c3aed;border-radius:10px;padding:16px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#7c3aed;text-transform:uppercase;margin-bottom:12px;">Termius Actions</div>
          {cmd_blocks}
        </div>"""

    cmd_output_section = ""
    if cmd_output:
        cmd_output_section = f"""
        <div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Command Output: <span style="color:#94a3b8">{cmd_run}</span></div>
          <pre style="color:#94a3b8;font-size:11px;max-height:200px;overflow-y:auto;">{cmd_output}</pre>
        </div>"""

    file_section = ""
    if file_content:
        file_section = f"""
        <div style="background:#0a0a14;border:1px solid #1e1e2e;border-radius:10px;padding:14px;margin-bottom:12px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;margin-bottom:8px;">File: {file_name}</div>
          <pre style="color:#94a3b8;font-size:10px;max-height:300px;overflow-y:auto;">{file_content[:5000]}</pre>
        </div>"""

    error_html = f'<div style="background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;padding:12px;margin-bottom:12px;color:#ef4444;font-size:13px;">{error}</div>' if error else ""

    quick_btns = ""
    for label, q in QUICK_QUESTIONS:
        quick_btns += f"""<form method="POST" action="/ask" style="display:inline-block;margin:3px;">
          <input type="hidden" name="question" value="{q}">
          <button type="submit" style="background:#111118;border:1px solid #1e1e2e;color:#94a3b8;font-family:'JetBrains Mono',monospace;font-size:11px;padding:8px 12px;border-radius:6px;cursor:pointer;">{label}</button>
        </form>"""

    file_btns = ""
    for f in SAFE_FILES:
        file_btns += f"""<form method="POST" action="/file" style="display:inline-block;margin:3px;">
          <input type="hidden" name="filename" value="{f}">
          <input type="hidden" name="question" value="{question}">
          <button type="submit" style="background:#0a0a14;border:1px solid #1e1e2e;color:#64748b;font-family:'JetBrains Mono',monospace;font-size:10px;padding:6px 10px;border-radius:5px;cursor:pointer;">{f}</button>
        </form>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaBot Debug</title>
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
  details[open] summary {{ color:#e2e8f0; margin-bottom:8px; }}
  pre {{ font-size:10px; color:#475569; white-space:pre-wrap; word-break:break-all; line-height:1.5; }}
  .copy-toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#00ff88; color:#000; font-weight:700; font-size:13px; padding:10px 20px; border-radius:8px; display:none; z-index:999; font-family:'Syne',sans-serif; }}
</style>
</head>
<body>

<div class="copy-toast" id="toast">Copied!</div>

<script>
function copyCmd(id) {{
  const text = document.getElementById(id).innerText;
  navigator.clipboard.writeText(text).then(() => {{
    const t = document.getElementById('toast');
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 1500);
  }});
}}
</script>

<header>
  <div class="logo">Alpha<span>Bot</span> DEBUG</div>
  <div class="badge">&#9679; {status_text}</div>
</header>

{error_html}
{answer_section}
{cmd_output_section}
{file_section}

<div style="margin-bottom:12px;">
  {quick_btns}
</div>

<form method="POST" action="/ask">
  <div class="card">
    <div class="card-title">Ask anything</div>
    <textarea name="question" placeholder="e.g. Why is ASX not trading? Check main.py for the issue.">{question}</textarea>
    <button type="submit" class="ask-btn">ASK CLAUDE</button>
  </div>
</form>

<div class="card">
  <div class="card-title">File Viewer</div>
  <div>{file_btns}</div>
</div>

<div class="card">
  <div class="card-title">Stats</div>
  <div>
    <div class="stat"><div class="stat-val">{trades_today}</div><div class="stat-lbl">Trades Today</div></div>
    <div class="stat"><div class="stat-val">{len(near_misses)}</div><div class="stat-lbl">Near Misses</div></div>
    <div class="stat"><div class="stat-val" style="color:{status_color};">{status_text}</div><div class="stat-lbl">Bot Status</div></div>
  </div>
</div>

<div class="card">
  <div class="card-title">Recent Trades</div>
  {'<table><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>P&L</th><th>Reason</th></tr>' + trades_html + '</table>' if trades_html else '<div style="color:#64748b;font-size:12px;">No trades found</div>'}
</div>

<div class="card">
  <div class="card-title">Near Misses</div>
  {'<table><tr><th>Symbol</th><th>Score</th><th>Reason</th><th>Time</th></tr>' + nm_html + '</table>' if nm_html else '<div style="color:#64748b;font-size:12px;">No near misses</div>'}
</div>

<div class="card">
  <details>
    <summary>Screen Status</summary>
    <pre style="margin-top:8px;">{screen_status}</pre>
  </details>
</div>

</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return render_page()

@app.post("/ask", response_class=HTMLResponse)
async def ask(question: str = Form("")):
    if not question.strip():
        return render_page(error="Please enter a question.")
    log = get_screen_log()
    screen_status = get_screen_status()
    db = get_db_summary()
    # Auto-load config and recent main.py snippet for context
    config = get_file("core/config.py")
    extra = f"core/config.py:\n{config[:2000]}"
    answer = call_claude(question, log, screen_status, db, extra)
    return render_page(answer=answer, question=question)

@app.post("/file", response_class=HTMLResponse)
async def view_file(filename: str = Form(""), question: str = Form("")):
    if filename not in SAFE_FILES:
        return render_page(error=f"File not allowed: {filename}", question=question)
    content = get_file(filename)
    return render_page(file_content=content, file_name=filename, question=question)

@app.post("/run", response_class=HTMLResponse)
async def run_command(cmd: str = Form(""), question: str = Form("")):
    if not is_safe_command(cmd):
        return render_page(error=f"Command not allowed for safety: {cmd}", question=question)
    output = run_cmd(cmd)
    return render_page(cmd_output=output, cmd_run=cmd, question=question)
