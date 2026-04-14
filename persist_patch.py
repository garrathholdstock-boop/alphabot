#!/usr/bin/env python3
"""
AlphaBot Persistence Layer Patch
Adds full restart-safe persistence for:
  - near_miss_tracker (prices_since, simulation, triggered)
  - perf dict (peak_portfolio, max_drawdown, sharpe_daily, all_trades)
  - global_risk (loss_streak, paused_until)

Run: python3 /tmp/persist_patch.py
"""

import re

DB_PATH = "/home/alphabot/app/data/database.py"
MAIN_PATH = "/home/alphabot/app/app/main.py"

# ── Step 1: Add new DB functions to database.py ───────────────

DB_NEW_FUNCTIONS = '''

# ── Bot state persistence (survives restarts) ─────────────────
def db_save_bot_state(perf_data, global_risk_data):
    """Save perf and global_risk to DB so they survive restarts."""
    try:
        import json
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )""")
        for key, val in [
            ("peak_portfolio",  str(perf_data.get("peak_portfolio", 0))),
            ("max_drawdown",    str(perf_data.get("max_drawdown", 0))),
            ("sharpe_daily",    json.dumps(perf_data.get("sharpe_daily", []))),
            ("all_trades",      json.dumps(perf_data.get("all_trades", [])[-200:])),  # last 200
            ("loss_streak",     str(global_risk_data.get("loss_streak", 0))),
            ("paused_until",    str(global_risk_data.get("paused_until", ""))),
        ]:
            c.execute("INSERT INTO bot_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                      (key, val))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to save bot state: {e}")


def db_load_bot_state():
    """Load persisted perf and global_risk from DB on startup."""
    try:
        import json
        from datetime import datetime
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT DEFAULT (datetime('now')))")
        c.execute("SELECT key, value FROM bot_state")
        rows = dict(c.fetchall())
        conn.close()
        perf_data = {
            "peak_portfolio": float(rows.get("peak_portfolio", 0)),
            "max_drawdown":   float(rows.get("max_drawdown", 0)),
            "sharpe_daily":   json.loads(rows.get("sharpe_daily", "[]")),
            "all_trades":     json.loads(rows.get("all_trades", "[]")),
        }
        paused_str = rows.get("paused_until", "")
        paused_until = None
        if paused_str and paused_str != "None":
            try:
                paused_until = datetime.fromisoformat(paused_str)
            except:
                pass
        global_risk_data = {
            "loss_streak":  int(float(rows.get("loss_streak", 0))),
            "paused_until": paused_until,
        }
        log.info(f"[DB] Loaded bot state: peak=${perf_data['peak_portfolio']:.2f} drawdown={perf_data['max_drawdown']:.1f}% streak={global_risk_data['loss_streak']}")
        return perf_data, global_risk_data
    except Exception as e:
        log.warning(f"[DB] Failed to load bot state (first run?): {e}")
        return None, None


# ── Near-miss full persistence ────────────────────────────────
def db_update_near_miss(symbol, date, prices_since=None, simulation=None,
                         triggered=False, trigger_date=None, trigger_price=None, pct_move=None):
    """Update a near-miss record with price tracking and simulation results."""
    try:
        import json
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Ensure simulation_result column exists
        try:
            c.execute("ALTER TABLE near_misses ADD COLUMN simulation_result TEXT")
            conn.commit()
        except:
            pass  # Column already exists
        updates = []
        values = []
        if prices_since is not None:
            updates.append("prices_since=?")
            values.append(json.dumps(prices_since))
        if simulation is not None:
            updates.append("simulation_result=?")
            values.append(json.dumps(simulation))
        if triggered:
            updates.append("triggered=1")
            if trigger_date:
                updates.append("trigger_date=?"); values.append(trigger_date)
            if trigger_price:
                updates.append("trigger_price=?"); values.append(trigger_price)
        if pct_move is not None:
            updates.append("pct_move=?"); values.append(pct_move)
        if updates:
            values.extend([symbol, date])
            c.execute(f"UPDATE near_misses SET {', '.join(updates)} WHERE symbol=? AND date=?", values)
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to update near miss {symbol}: {e}")


def db_load_near_misses():
    """Load all recent near-misses from DB into memory on startup."""
    try:
        import json
        from datetime import datetime, timedelta
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Ensure column exists
        try:
            c.execute("ALTER TABLE near_misses ADD COLUMN simulation_result TEXT")
            conn.commit()
        except:
            pass
        cutoff = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
        c.execute("SELECT symbol, date, score, threshold, gap, price_at_miss, prices_since, pct_move, triggered, trigger_date, trigger_price, crypto, skip_reason, simulation_result FROM near_misses WHERE date >= ? ORDER BY date DESC", (cutoff,))
        rows = c.fetchall()
        conn.close()
        tracker = {}
        for row in rows:
            symbol, date, score, threshold, gap, price_at_miss, prices_since_json, pct_move, triggered, trigger_date, trigger_price, crypto, skip_reason, sim_json = row
            key = f"{symbol}_{date}"
            prices_since = json.loads(prices_since_json) if prices_since_json and prices_since_json != "None" else []
            simulation = json.loads(sim_json) if sim_json else None
            tracker[key] = {
                "symbol": symbol, "date": date, "score": score or 0,
                "threshold": threshold or 5, "gap": gap or 0,
                "price_at_miss": price_at_miss or 0, "prices_since": prices_since,
                "pct_move": pct_move, "triggered": bool(triggered),
                "trigger_date": trigger_date, "trigger_price": trigger_price,
                "crypto": bool(crypto), "skip_reason": skip_reason or "SCORE",
                "simulation": simulation, "recorded_at": date,
            }
        log.info(f"[DB] Loaded {len(tracker)} near-misses from DB")
        return tracker
    except Exception as e:
        log.warning(f"[DB] Failed to load near misses: {e}")
        return {}
'''

# ── Step 2: Code to add to main.py startup (after recovery) ──

MAIN_STARTUP_CODE = '''
    # ── Load persisted bot state ──
    log.info("=== Loading persisted bot state ===")
    try:
        from data.database import db_load_bot_state, db_load_near_misses
        _perf_data, _risk_data = db_load_bot_state()
        if _perf_data:
            perf["peak_portfolio"] = max(perf["peak_portfolio"], _perf_data["peak_portfolio"])
            perf["max_drawdown"]   = max(perf["max_drawdown"], _perf_data["max_drawdown"])
            perf["sharpe_daily"]   = _perf_data["sharpe_daily"]
            if _perf_data["all_trades"]:
                perf["all_trades"] = _perf_data["all_trades"]
        if _risk_data:
            global_risk["loss_streak"] = _risk_data["loss_streak"]
            if _risk_data["paused_until"]:
                global_risk["paused_until"] = _risk_data["paused_until"]
        _nm_data = db_load_near_misses()
        if _nm_data:
            near_miss_tracker.update(_nm_data)
        log.info(f"=== State restored: {len(near_miss_tracker)} near-misses, streak={global_risk['loss_streak']} ===")
    except Exception as e:
        log.error(f"State restore failed: {e}")
'''

# ── Step 3: Code to add to main loop (save state periodically) ──

MAIN_LOOP_SAVE_CODE = '''
            # Save bot state to DB every 10 cycles
            if cycle % 10 == 0:
                try:
                    from data.database import db_save_bot_state
                    db_save_bot_state(perf, global_risk)
                except Exception as e:
                    log.warning(f"[DB] State save failed: {e}")
'''

# ── Step 4: Patch analytics.py to write back to DB on updates ──

ANALYTICS_PATCH_UPDATE = '''
def _db_persist_near_miss(symbol, date, nm):
    """Write near-miss updates back to DB for restart safety."""
    try:
        from data.database import db_update_near_miss
        db_update_near_miss(
            symbol=symbol, date=date,
            prices_since=nm.get("prices_since"),
            simulation=nm.get("simulation"),
            triggered=nm.get("triggered", False),
            trigger_date=nm.get("trigger_date"),
            trigger_price=nm.get("trigger_price"),
            pct_move=nm.get("pct_move"),
        )
    except Exception as e:
        log.debug(f"[PERSIST] near miss write failed: {e}")
'''

# ─────────────────────────────────────────────────────────────
# Apply patches
# ─────────────────────────────────────────────────────────────

print("=== AlphaBot Persistence Patch ===")

# Patch database.py
print("\n[1/4] Patching database.py...")
with open(DB_PATH) as f:
    db_content = f.read()

if "db_save_bot_state" in db_content:
    print("  Already patched — skipping")
else:
    # Insert before the final init_db() call at bottom
    marker = "\n# ── Initialise on import"
    if marker in db_content:
        db_content = db_content.replace(marker, DB_NEW_FUNCTIONS + marker)
        with open(DB_PATH, "w") as f:
            f.write(db_content)
        print("  ✅ Done")
    else:
        # Append at end
        with open(DB_PATH, "a") as f:
            f.write(DB_NEW_FUNCTIONS)
        print("  ✅ Done (appended)")

# Patch main.py - startup load
print("\n[2/4] Patching main.py startup...")
with open(MAIN_PATH) as f:
    main_content = f.read()

if "db_load_bot_state" in main_content:
    print("  Already patched — skipping")
else:
    # Insert after the stop verification block
    marker = "    last_email_day = None\n    cycle = 0"
    if marker in main_content:
        main_content = main_content.replace(marker, MAIN_STARTUP_CODE + "\n" + marker)
        print("  ✅ Startup load inserted")
    else:
        print("  ⚠️  Could not find startup marker — check manually")

# Patch main.py - periodic save in loop
if "db_save_bot_state" in main_content:
    print("\n[3/4] Loop save already patched — skipping")
else:
    # Insert inside the every-10-cycles block
    marker = "            # Refresh account info\n            cfg.account_info"
    if marker in main_content:
        main_content = main_content.replace(marker, MAIN_LOOP_SAVE_CODE + "\n" + marker)
        print("\n[3/4] ✅ Periodic save inserted")
    else:
        print("\n[3/4] ⚠️  Could not find loop marker")

with open(MAIN_PATH, "w") as f:
    f.write(main_content)

# Patch analytics.py - write back on update
print("\n[4/4] Patching analytics.py near-miss write-back...")
ANALYTICS_PATH = "/home/alphabot/app/data/analytics.py"
with open(ANALYTICS_PATH) as f:
    an_content = f.read()

if "_db_persist_near_miss" in an_content:
    print("  Already patched — skipping")
else:
    # Add helper function after imports
    marker = "import core.config as cfg"
    if marker in an_content:
        an_content = an_content.replace(marker, marker + "\n" + ANALYTICS_PATCH_UPDATE)
        print("  ✅ Helper added")

    # Patch update_near_miss_prices to write back
    old_update = "                    nm[\"prices_since\"].append(round(price, 4))\n        except:\n            pass"
    new_update = "                    nm[\"prices_since\"].append(round(price, 4))\n                    _db_persist_near_miss(nm[\"symbol\"], nm[\"date\"], nm)\n        except:\n            pass"
    if old_update in an_content:
        an_content = an_content.replace(old_update, new_update)
        print("  ✅ Price update write-back patched")

    # Patch run_near_miss_simulations to write back
    old_sim = "                    nm[\"simulation\"] = sim\n                    updated += 1"
    new_sim = "                    nm[\"simulation\"] = sim\n                    updated += 1\n                    _db_persist_near_miss(nm[\"symbol\"], nm[\"date\"], nm)"
    if old_sim in an_content:
        an_content = an_content.replace(old_sim, new_sim)
        print("  ✅ Simulation write-back patched")

    # Patch mark_near_miss_triggered to write back
    old_trig = "            nm[\"trigger_price\"] = fetch_latest_price(symbol, crypto=nm[\"crypto\"])\n            log.info(f\"[NEAR MISS] {symbol} finally triggered!\")"
    new_trig = "            nm[\"trigger_price\"] = fetch_latest_price(symbol, crypto=nm[\"crypto\"])\n            log.info(f\"[NEAR MISS] {symbol} finally triggered!\")\n            _db_persist_near_miss(nm[\"symbol\"], nm[\"date\"], nm)"
    if old_trig in an_content:
        an_content = an_content.replace(old_trig, new_trig)
        print("  ✅ Triggered write-back patched")

    with open(ANALYTICS_PATH, "w") as f:
        f.write(an_content)

print("\n=== Patch complete ===")
print("Test: python3 -c \"from data.database import db_save_bot_state, db_load_bot_state, db_load_near_misses; print('OK')\"")
print("Then restart: bash /home/alphabot/start.sh")
