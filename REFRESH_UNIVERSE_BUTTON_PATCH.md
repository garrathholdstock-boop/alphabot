"""
REFRESH_UNIVERSE_BUTTON.py — Drop-in patch for ai_debug/main.py

Adds a "🌍 Refresh Watchlist Universe" button to the maintenance page
that runs the universe scraper + watchlist rebuild in the background.

THREE INSERTIONS (apply in any order).
Apply via GitHub web editor, commit, then VPS: git pull + systemctl restart alphabot-agent

═══════════════════════════════════════════════════════════════════
INSERTION 1 — Endpoint (Python)
═══════════════════════════════════════════════════════════════════

Paste this IMMEDIATELY BEFORE `@app.post("/maintenance/refresh-smallcaps")`
(around line 2804 in current file).

─────────────────────────────────────────────────────────────────

@app.post("/maintenance/refresh-universe")
async def refresh_universe_endpoint(request: Request):
    """
    PIN-gated full universe + watchlist refresh.
    Runs in a background thread — returns immediately.
    Progress tracked via `_universe_refresh_state` module global.
    """
    from fastapi.responses import JSONResponse as JR
    import threading
    try:
        body = await request.json()
        if body.get("pin") != MAINT_PIN:
            return JR({"status": "wrong_pin"})

        # Kick off background thread
        def _run():
            global _universe_refresh_state
            _universe_refresh_state = {"status": "running", "phase": "universe", "started_at": time.time()}
            try:
                import sys
                sys.path.insert(0, APP_PATH)
                from data.universe_loader import refresh_universe
                from data.watchlist_refresh import refresh_watchlists_from_universe
                u = refresh_universe()
                _universe_refresh_state["universe_result"] = u
                if not u.get("ok"):
                    _universe_refresh_state["status"] = "error"
                    _universe_refresh_state["message"] = f"Universe fetch failed: {u.get('errors')}"
                    return
                _universe_refresh_state["phase"] = "watchlists"
                w = refresh_watchlists_from_universe()
                _universe_refresh_state["watchlist_result"] = {k: v for k, v in w.items() if k != "picked"}
                if not w.get("ok"):
                    _universe_refresh_state["status"] = "error"
                    _universe_refresh_state["message"] = f"Watchlist build failed: {w.get('errors')}"
                    return
                _universe_refresh_state["status"] = "done"
                _universe_refresh_state["message"] = (
                    f"✅ Universe: {u['counts']['universe']} symbols • "
                    f"Watchlists: {', '.join(f'{k}={v}' for k, v in w['counts'].items())}. "
                    "Restart bot to apply."
                )
            except Exception as e:
                _universe_refresh_state["status"] = "error"
                _universe_refresh_state["message"] = f"Unexpected: {type(e).__name__}: {e}"

        threading.Thread(target=_run, daemon=True).start()
        _log_agent("Universe refresh started")
        return JR({"status": "ok", "message": "🌍 Universe refresh started — this takes 2-5 minutes. Check status below."})
    except Exception as e:
        return JR({"status": "error", "message": f"{type(e).__name__}: {e}"})


@app.get("/maintenance/refresh-universe/status")
async def refresh_universe_status():
    """Poll endpoint for the background universe refresh."""
    from fastapi.responses import JSONResponse as JR
    state = _universe_refresh_state.copy() if _universe_refresh_state else {"status": "idle"}
    if state.get("started_at"):
        state["elapsed_seconds"] = round(time.time() - state["started_at"], 0)
    return JR(state)


# Module-level state for the background job
_universe_refresh_state = {"status": "idle"}


─────────────────────────────────────────────────────────────────

═══════════════════════════════════════════════════════════════════
INSERTION 2 — UI card (HTML)
═══════════════════════════════════════════════════════════════════

Find the existing "Refresh Small Caps" card in _build_maintenance_page().
Paste this NEW card IMMEDIATELY AFTER the closing </div> of the smallcap card.

Look for the block ending with `<div id="sc-result"...></div></div>`
then add this:

─────────────────────────────────────────────────────────────────

    <!-- Refresh Universe -->
    <div style="background:#0a1020;border:1px solid rgba(0,170,255,0.3);border-radius:10px;padding:16px">
      <div style="font-size:15px;font-weight:700;color:#00aaff;margin-bottom:6px">🌍 Refresh Watchlist Universe</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        Full rebuild of all 6 watchlists from index constituents (S&P 500/400/600, NASDAQ 100, FTSE 100/250, ASX 200/300) cross-referenced against IBKR's live tradeable universe. Takes 2-5 minutes. Current watchlists archived. Bot restart required after. PIN needed.
      </div>
      <button id="uv-btn" onclick="refreshUniverse()" class="btn"
        style="width:100%;background:rgba(0,170,255,0.1);border:1px solid rgba(0,170,255,0.4);color:#00aaff">
        🌍 Refresh Universe &amp; Watchlists
      </button>
      <div id="uv-bar" style="display:none;margin-top:12px">
        <div style="height:4px;background:rgba(0,170,255,0.15);border-radius:2px;overflow:hidden">
          <div id="uv-progress" style="height:100%;width:0%;background:linear-gradient(90deg,#00aaff,#60c4ff);border-radius:2px;transition:width 0.4s ease"></div>
        </div>
        <div id="uv-status" style="font-size:11px;color:#00aaff;margin-top:6px;text-align:center">Starting...</div>
      </div>
      <div id="uv-result" style="display:none;margin-top:12px;font-size:12px;background:rgba(0,170,255,0.05);border:1px solid rgba(0,170,255,0.2);border-radius:6px;padding:10px;line-height:1.6"></div>
    </div>

─────────────────────────────────────────────────────────────────

═══════════════════════════════════════════════════════════════════
INSERTION 3 — JavaScript handler
═══════════════════════════════════════════════════════════════════

Find the existing `refreshSmallCaps()` function in the maintenance page's
<script> block. Paste this function IMMEDIATELY AFTER it.

─────────────────────────────────────────────────────────────────

function refreshUniverse() {{
  var pin = prompt('Enter maintenance PIN to refresh universe + watchlists:');
  if (!pin) return;
  var btn = document.getElementById('uv-btn');
  var bar = document.getElementById('uv-bar');
  var prog = document.getElementById('uv-progress');
  var status = document.getElementById('uv-status');
  var result = document.getElementById('uv-result');
  btn.disabled = true;
  btn.style.opacity = '0.5';
  bar.style.display = 'block';
  result.style.display = 'none';
  prog.style.width = '5%';
  status.textContent = 'Kicking off...';
  fetch(BASE+'/maintenance/refresh-universe', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{pin: pin}})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.status === 'wrong_pin') {{
      status.textContent = 'Wrong PIN.';
      bar.style.display = 'none';
      btn.disabled = false;
      btn.style.opacity = '1';
      return;
    }}
    if (d.status !== 'ok') {{
      status.textContent = d.message || 'Error starting job';
      btn.disabled = false;
      btn.style.opacity = '1';
      return;
    }}
    // Poll status every 3s
    prog.style.width = '10%';
    status.textContent = 'Scraping universe...';
    var pollCount = 0;
    var poll = setInterval(function() {{
      pollCount++;
      fetch(BASE+'/maintenance/refresh-universe/status')
        .then(r => r.json())
        .then(s => {{
          if (s.status === 'running') {{
            // Visual progress heuristic — universe phase is longest
            var pct = s.phase === 'watchlists' ? 85 : Math.min(80, 10 + pollCount * 2);
            prog.style.width = pct + '%';
            status.textContent = s.phase === 'watchlists'
              ? 'Building watchlists from universe...'
              : 'Fetching IBKR + index data... ' + (s.elapsed_seconds || 0) + 's';
          }} else if (s.status === 'done') {{
            clearInterval(poll);
            prog.style.width = '100%';
            status.textContent = 'Complete!';
            result.style.display = 'block';
            result.style.color = '#00ff88';
            result.textContent = s.message;
            btn.disabled = false;
            btn.style.opacity = '1';
          }} else if (s.status === 'error') {{
            clearInterval(poll);
            prog.style.width = '100%';
            prog.style.background = '#ef4444';
            status.textContent = 'Failed';
            result.style.display = 'block';
            result.style.color = '#ef4444';
            result.textContent = s.message;
            btn.disabled = false;
            btn.style.opacity = '1';
          }}
        }})
        .catch(e => {{
          // Agent may be restarting — keep polling
          console.log('poll retry', e);
        }});
    }}, 3000);
  }})
  .catch(err => {{
    status.textContent = 'Network error: ' + err;
    btn.disabled = false;
    btn.style.opacity = '1';
  }});
}}

─────────────────────────────────────────────────────────────────


═══════════════════════════════════════════════════════════════════
POST-DEPLOY TEST
═══════════════════════════════════════════════════════════════════

1. git pull on VPS
2. systemctl restart alphabot-agent
3. Open maintenance page
4. Scroll to find "🌍 Refresh Watchlist Universe" card
5. Click button, enter PIN
6. Progress bar appears, polls every 3s
7. After 2-5 min: green result with counts
8. Run: systemctl restart alphabot
9. Verify: grep "WATCHLIST.*Loaded from DB" /home/alphabot/app/alphabot.log | tail -1

Expected after success:
  [WATCHLIST] Loaded from DB: US:250 | FTSE:250 | ASX:250 | SmUS:100 | SmFTSE:100 | SmASX:100


═══════════════════════════════════════════════════════════════════
CRON (optional — weekly auto-refresh)
═══════════════════════════════════════════════════════════════════

Via Termius:

    crontab -e

Add:

    # Refresh universe + watchlists every Sunday at 04:00 UTC
    0 4 * * 0  cd /home/alphabot/app && /usr/bin/python3 refresh_universe_cli.py >> /home/alphabot/app/universe-refresh.log 2>&1
    # Then restart bot
    30 4 * * 0  /bin/systemctl restart alphabot
"""
