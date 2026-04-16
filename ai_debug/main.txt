"""
AlphaBot Debug Portal — powered by Claude API
Safari-compatible version (form POST, no fetch API)
Run: uvicorn main:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
import os, subprocess, sqlite3, json, anthropic
from datetime import datetime

app = FastAPI()

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
DB_PATH        = "/home/alphabot/app/alphabot.db"
SCREEN_NAME    = "alphabot"

ALPHABOT_CONTEXT = """
You are an expert debugging assistant for AlphaBot, an automated multi-market day trading bot.

ARCHITECTURE:
- VPS: 178.104.170.58 (Hetzner), user: root
- Git root: /home/alphabot/app/ (branch: master)
- Start: bash /home/alphabot/start.sh → runs python3 app/main.py inside screen session "alphabot"
- Dashboard: port 8080, this debug portal: port 8000
- DB: /home/alphabot/app/alphabot.db

FILES:
- app/main.py       — main scan/execution loop, capital efficiency logic ~line 253/273
- app/dashboard.py  — web dashboard
- core/config.py    — all config constants
- core/execution.py — place_order, IBKR stop placement
- core/risk.py      — score_signal, check_stop_losses
- data/analytics.py / data/database.py

CURRENT CONFIG:
- MIN_SIGNAL_SCORE=5 (must raise to 7 before going live)
- MAX_POSITIONS=3 per market, MAX_TOTAL_POSITIONS=15
- CYCLE_SECONDS=60, STOP_LOSS_PCT=5%, IS_LIVE=false (paper trading)
- Brokers: Alpaca (US stocks paper), IBKR (stops + ASX/FTSE), Binance (crypto — 401 error, API permissions not set)

KNOWN COSMETIC ERRORS (non-blocking, ignore):
- Error 10089 HOOD/SOFI/FCEL — market data subscription, cosmetic only
- Binance POST 401 — API key permissions not set, crypto orders fail but scanning works
- BrokenPipeError / ConnectionResetError in dashboard — client disconnected, harmless

CAPITAL EFFICIENCY:
- Logic 1 ~line 253: Score rotation — rotates weakest held pos if new signal 1.5+ higher AND profit >0.1%. Logged as ROTATE
- Logic 2 ~line 273: Stale capital — exits position flat +-0.5% for 30+ min. Logged as STALE EXIT

P1 REMAINING: Raise MIN_SIGNAL_SCORE 5->7, fix Binance 401 permissions.

When diagnosing: ignore known cosmetic errors, identify real issues, give file+line where possible, provide exact fix command. Be concise — owner reads on mobile.
"""

QUICK_QUESTIONS = [
    ("🤖 Bot status", "Bot status — is it running and healthy?"),
    ("📊 Dashboard issue", "Dashboard not updating — what is wrong?"),
    ("🔍 Real errors only", "Any real errors in the logs? Ignore known cosmetic issues."),
    ("💰 Positions & P&L", "Check positions and P&L from the DB. Any issues?"),
    ("🎯 Near misses", "Near-miss analysis — what signals are we almost hitting? Should we adjust MIN_SIGNAL_SCORE?"),
    ("🚀 Next steps", "What should I do next to move AlphaBot closer to live trading?"),
]

def get_screen_log():
    try:
        subprocess.run(
            ["/usr/bin/screen", "-S", SCREEN_NAME, "-X", "hardcopy", "/tmp/alphabot_screen.txt"],
            timeout=5, capture_output=True
        )
        if os.path.exists("/tmp/alphabot_screen.txt"):
            with open("/tmp/alphabot_screen.txt", "r", errors="replace") as f:
                lines = f.readlines()
                return "".join(lines[-150:])
        return "No screen log available"
    except Exception as e:
        return f"Could not capture screen: {e}"

def get_screen_status():
    try:
        r = subprocess.run(["/usr/bin/screen", "-ls"], capture_output=True, text=True, timeout=5)
        return r.stdout + r.stderr
    except Exception as e:
        return f"Error: {e}"

def get_db_summary():
    if not os.path.exists(DB_PATH):
        return {"error": f"DB not found at {DB_PATH}"}
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

def call_claude(question, log, screen_status, db):
    if not CLAUDE_API_KEY:
        return "ERROR: CLAUDE_API_KEY not set."
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        context = f"SCREEN STATUS:\n{screen_status}\n\nRECENT LOG:\n{log[-8000:]}\n\nDB SUMMARY:\n{json.dumps(db, default=str)[:3000]}"
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=ALPHABOT_CONTEXT,
            messages=[{"role": "user", "content": f"DEBUG CONTEXT:\n{context}\n\nQUESTION: {question}"}]
        )
        return message.content[0].text
    except Exception as e:
        return f"Claude API error: {e}"

def render_page(answer="", question="", error=""):
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
        trades_html += f"""
        <tr>
          <td>{t.get('symbol','')}</td>
          <td>{t.get('side','')}</td>
          <td>{t.get('qty','')}</td>
          <td>${float(t.get('price',0)):.2f}</td>
          <td style="color:{pnl_color}">${float(pnl):.2f}</td>
          <td style="color:#64748b;font-size:10px">{t.get('reason','')}</td>
        </tr>"""

    nm_html = ""
    for n in near_misses:
        nm_html += f"""
        <tr>
          <td>{n.get('symbol','')}</td>
          <td>{n.get('score','')}</td>
          <td>{n.get('reason','')}</td>
          <td style="color:#64748b;font-size:10px">{str(n.get('created_at',''))[:16]}</td>
        </tr>"""

    answer_html = ""
    if answer:
        answer_html = f"""
        <div style="background:#0a1a0f;border:1px solid #00ff88;border-radius:10px;padding:16px;margin-bottom:16px;">
          <div style="font-size:10px;font-weight:700;letter-spacing:1px;color:#00ff88;text-transform:uppercase;margin-bottom:10px;">⚡ Claude Analysis</div>
          <div style="font-size:13px;line-height:1.7;white-space:pre-wrap;color:#e2e8f0;">{answer}</div>
        </div>"""

    error_html = f'<div style="background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;padding:12px;margin-bottom:16px;color:#ef4444;font-size:13px;">{error}</div>' if error else ""

    quick_btns = ""
    for label, q in QUICK_QUESTIONS:
        quick_btns += f'<button type="submit" name="question" value="{q}" style="background:#111118;border:1px solid #1e1e2e;color:#64748b;font-family:\'JetBrains Mono\',monospace;font-size:11px;padding:7px 11px;border-radius:6px;cursor:pointer;margin:3px;">{label}</button>'

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
  textarea {{ width:100%; background:#111118; border:1px solid #1e1e2e; border-radius:8px; color:#e2e8f0; font-family:'JetBrains Mono',monospace; font-size:13px; padding:10px 12px; resize:none; height:70px; }}
  textarea:focus {{ outline:none; border-color:#00ff88; }}
  .ask-btn {{ display:block; width:100%; background:#00ff88; border:none; border-radius:8px; color:#000; font-family:'Syne',sans-serif; font-weight:800; font-size:15px; padding:12px; cursor:pointer; margin-top:8px; letter-spacing:0.5px; }}
  table {{ width:100%; border-collapse:collapse; font-size:11px; }}
  th {{ color:#64748b; text-align:left; padding:4px 6px; border-bottom:1px solid #1e1e2e; font-weight:700; }}
  td {{ padding:5px 6px; border-bottom:1px solid #0f0f18; }}
  .stat {{ display:inline-block; background:#0a0a0f; border:1px solid #1e1e2e; border-radius:6px; padding:8px 12px; margin:4px; text-align:center; }}
  .stat-val {{ font-size:20px; font-weight:700; color:#00ff88; }}
  .stat-lbl {{ font-size:10px; color:#64748b; margin-top:2px; }}
  details summary {{ cursor:pointer; color:#64748b; font-size:11px; letter-spacing:1px; text-transform:uppercase; font-weight:700; padding:4px 0; }}
  details[open] summary {{ color:#e2e8f0; margin-bottom:8px; }}
  pre {{ font-size:10px; color:#475569; white-space:pre-wrap; word-break:break-all; line-height:1.5; max-height:200px; overflow-y:auto; }}
</style>
</head>
<body>

<header>
  <div class="logo">Alpha<span>Bot</span> DEBUG</div>
  <div class="badge">● {status_text}</div>
</header>

{error_html}
{answer_html}

<form method="POST" action="/ask">
  <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px;">
    {quick_btns}
  </div>
  <div class="card">
    <div class="card-title">Ask anything</div>
    <textarea name="question" placeholder="e.g. Why is ASX not trading?">{question}</textarea>
    <button type="submit" class="ask-btn">ASK CLAUDE</button>
  </div>
</form>

<div class="card">
  <div class="card-title">📊 Stats</div>
  <div>
    <div class="stat"><div class="stat-val">{trades_today}</div><div class="stat-lbl">Trades Today</div></div>
    <div class="stat"><div class="stat-val">{len(near_misses)}</div><div class="stat-lbl">Near Misses</div></div>
    <div class="stat"><div class="stat-val" style="color:{'#00ff88' if bot_running else '#ef4444'}">{status_text}</div><div class="stat-lbl">Bot Status</div></div>
  </div>
</div>

<div class="card">
  <div class="card-title">💼 Recent Trades</div>
  {'<table><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>P&L</th><th>Reason</th></tr>' + trades_html + '</table>' if trades_html else '<div style="color:#64748b;font-size:12px;">No trades found</div>'}
</div>

<div class="card">
  <div class="card-title">🎯 Near Misses</div>
  {'<table><tr><th>Symbol</th><th>Score</th><th>Reason</th><th>Time</th></tr>' + nm_html + '</table>' if nm_html else '<div style="color:#64748b;font-size:12px;">No near misses found</div>'}
</div>

<div class="card">
  <details>
    <summary>📟 Screen Status</summary>
    <pre>{screen_status}</pre>
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
    answer = call_claude(question, log, screen_status, db)
    return render_page(answer=answer, question=question)
