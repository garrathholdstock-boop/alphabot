"""
data/database.py — AlphaBot Trading Intelligence Database
All SQLite operations: trades, near-misses, stock stats, reports, rotations.

v2 additions (Phase 1/2/3 — 18 Apr 2026):
  • trades:       adx_at_entry, macd_bullish, breakout, rs_vs_spy, news_state,
                  regime_at_entry, vix_at_entry, exit_category
  • near_misses:  simulated_pnl_pct, simulated_pnl_usd, simulated_exit_reason,
                  simulated_exit_day, mfe_pct, mae_pct, last_checked, discipline
  • rotations:    NEW table for Logic 1 (score rotate) and Logic 2 (stale exit)
                  audit — tracks if the rotation was a good call 24h later.

All schema changes are idempotent (ALTER TABLE ADD COLUMN with try/except).
All new columns are nullable — old trades/near-misses stay intact.
All new helpers wrap every DB op in try/except so a failure here can never
kill a trading cycle.
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta

from core.config import log, DB_PATH


# ═══════════════════════════════════════════════════════════════
# INTERNAL: safe idempotent ALTER
# ═══════════════════════════════════════════════════════════════
def _safe_alter(conn, sql):
    """Run ALTER TABLE — silently pass if column already exists."""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass  # "duplicate column name" — already migrated
    except Exception as e:
        log.debug(f"[DB] alter skipped: {e}")


# ═══════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ── trades ────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        qty             REAL,
        price           REAL,
        pnl             REAL,
        score           REAL,
        rsi             REAL,
        vol_ratio       REAL,
        hold_hours      REAL,
        reason          TEXT,
        signal_breakdown TEXT,
        market          TEXT,
        date            TEXT,
        time            TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        discipline      TEXT DEFAULT 'swing'
    )""")

    # Phase 1 additive columns on trades (all nullable, all idempotent)
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN discipline TEXT DEFAULT 'swing'")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN adx_at_entry REAL")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN macd_bullish INTEGER")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN breakout INTEGER")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN rs_vs_spy REAL")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN news_state TEXT")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN regime_at_entry TEXT")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN vix_at_entry REAL")
    _safe_alter(conn, "ALTER TABLE trades ADD COLUMN exit_category TEXT")

    # ── reports ───────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        type        TEXT NOT NULL,
        date        TEXT NOT NULL,
        subject     TEXT,
        body_html   TEXT,
        body_text   TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    )""")

    # ── near_misses ───────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS near_misses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        date            TEXT NOT NULL,
        score           REAL,
        threshold       REAL,
        gap             REAL,
        price_at_miss   REAL,
        prices_since    TEXT,
        pct_move        REAL,
        triggered       INTEGER DEFAULT 0,
        trigger_date    TEXT,
        trigger_price   REAL,
        crypto          INTEGER DEFAULT 0,
        skip_reason     TEXT DEFAULT 'SCORE',
        created_at      TEXT DEFAULT (datetime('now'))
    )""")

    # Phase 1 additive columns on near_misses
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN simulated_pnl_pct REAL")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN simulated_pnl_usd REAL")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN simulated_exit_reason TEXT")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN simulated_exit_day INTEGER")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN mfe_pct REAL")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN mae_pct REAL")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN last_checked TEXT")
    _safe_alter(conn, "ALTER TABLE near_misses ADD COLUMN discipline TEXT")

    # ── stock_stats ───────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS stock_stats (
        symbol          TEXT PRIMARY KEY,
        total_trades    INTEGER DEFAULT 0,
        wins            INTEGER DEFAULT 0,
        losses          INTEGER DEFAULT 0,
        total_pnl       REAL DEFAULT 0,
        best_trade      REAL DEFAULT 0,
        worst_trade     REAL DEFAULT 0,
        avg_score       REAL DEFAULT 0,
        near_miss_count INTEGER DEFAULT 0,
        last_traded     TEXT,
        first_traded    TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""")

    # ── rotations (NEW) ───────────────────────────────────────
    # Records Logic 1 (SCORE_ROTATE) and Logic 2 (STALE_EXIT) decisions.
    # The 24h follow-up job (in main.py) populates the _24h and _pct_after fields.
    c.execute("""CREATE TABLE IF NOT EXISTS rotations (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        rotation_type         TEXT NOT NULL,
        sold_symbol           TEXT,
        sold_at_price         REAL,
        sold_at_score         REAL,
        sold_pnl              REAL,
        bought_symbol         TEXT,
        bought_at_price       REAL,
        bought_at_score       REAL,
        market                TEXT,
        created_at            TEXT DEFAULT (datetime('now')),
        sold_price_24h        REAL,
        sold_pct_after        REAL,
        bought_price_24h      REAL,
        bought_pct_after      REAL,
        rotation_verdict      TEXT,
        checked_at            TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS tuning_recommendations (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id            TEXT NOT NULL,
        category          TEXT,
        action            TEXT,
        parameter         TEXT,
        discipline        TEXT DEFAULT 'all',
        current_value     REAL,
        recommended_value REAL,
        evidence          TEXT,
        confidence        TEXT,
        sample_size       INTEGER,
        status            TEXT DEFAULT 'PENDING',
        created_at        TEXT DEFAULT (datetime('now')),
        actioned_at       TEXT,
        snoozed_until     TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS intelligence_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id           TEXT UNIQUE NOT NULL,
        narrative        TEXT,
        raw_payload      TEXT,
        rec_count        INTEGER DEFAULT 0,
        rec_count_raw    INTEGER DEFAULT 0,
        triggered_by     TEXT DEFAULT 'scheduled',
        created_at       TEXT DEFAULT (datetime('now'))
    )""")
    _safe_alter(conn, "ALTER TABLE intelligence_runs ADD COLUMN rec_count_raw INTEGER DEFAULT 0")

    c.execute("""CREATE TABLE IF NOT EXISTS config_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        parameter    TEXT NOT NULL,
        old_value    TEXT,
        new_value    TEXT NOT NULL,
        changed_by   TEXT DEFAULT 'manual',
        created_at   TEXT DEFAULT (datetime('now'))
    )""")

    conn.commit()
    conn.close()
    return True


# ═══════════════════════════════════════════════════════════════
# TRADE RECORDING — extended, backward-compatible
# ═══════════════════════════════════════════════════════════════
def db_record_trade(symbol, side, qty, price, pnl, score, rsi, vol_ratio,
                    hold_hours, reason, breakdown, market="stock", discipline="swing",
                    # Phase 1 additions — all optional, default None
                    adx_at_entry=None, macd_bullish=None, breakout=None,
                    rs_vs_spy=None, news_state=None, regime_at_entry=None,
                    vix_at_entry=None, exit_category=None):
    """Record a trade. New fields default None so pre-upgrade callers still work."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.now()

        # Coerce bools to 0/1, keep None as None
        def _b(x):
            if x is None: return None
            return 1 if x else 0

        c.execute("""INSERT INTO trades
            (symbol, side, qty, price, pnl, score, rsi, vol_ratio,
             hold_hours, reason, signal_breakdown, market, date, time, discipline,
             adx_at_entry, macd_bullish, breakout, rs_vs_spy,
             news_state, regime_at_entry, vix_at_entry, exit_category)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, side, qty, price, pnl, score, rsi, vol_ratio,
             hold_hours, reason, breakdown, market,
             now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), discipline,
             adx_at_entry, _b(macd_bullish), _b(breakout),
             rs_vs_spy, news_state, regime_at_entry, vix_at_entry, exit_category))
        conn.commit()

        # stock_stats aggregate (unchanged from original)
        today = now.strftime("%Y-%m-%d")
        c.execute("SELECT * FROM stock_stats WHERE symbol=?", (symbol,))
        row = c.fetchone()
        if row:
            wins   = row[2] + (1 if pnl and pnl > 0 else 0)
            losses = row[3] + (1 if pnl and pnl < 0 else 0)
            total  = row[4] + (pnl or 0)
            best   = max(row[5], pnl or 0)
            worst  = min(row[6], pnl or 0)
            trades = row[1] + 1
            avg_sc = ((row[7] * row[1]) + (score or 0)) / trades if trades > 0 else 0
            c.execute("""UPDATE stock_stats SET
                total_trades=?, wins=?, losses=?, total_pnl=?,
                best_trade=?, worst_trade=?, avg_score=?,
                last_traded=?, updated_at=datetime('now')
                WHERE symbol=?""",
                (trades, wins, losses, round(total,2), round(best,2),
                 round(worst,2), round(avg_sc,2), today, symbol))
        else:
            c.execute("""INSERT INTO stock_stats
                (symbol, total_trades, wins, losses, total_pnl,
                 best_trade, worst_trade, avg_score, last_traded, first_traded)
                VALUES (?,1,?,?,?,?,?,?,?,?)""",
                (symbol,
                 1 if pnl and pnl > 0 else 0,
                 1 if pnl and pnl < 0 else 0,
                 round(pnl or 0, 2), round(pnl or 0, 2),
                 round(pnl or 0, 2), round(score or 0, 2),
                 today, today))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record trade: {e}")


# ═══════════════════════════════════════════════════════════════
# NEAR-MISS RECORDING — extended to accept all skip reasons
# ═══════════════════════════════════════════════════════════════
def db_record_near_miss(symbol, score, threshold, gap, price, crypto=False,
                        skip_reason="SCORE", discipline=None):
    """
    Record a near-miss. Dedupes by (symbol, date, skip_reason) so the same
    symbol can accumulate different skip reasons across a day (e.g., SCORE in
    morning, SECTOR_CAP in afternoon). Old callers still work — skip_reason
    defaults to 'SCORE'.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT id FROM near_misses WHERE symbol=? AND date=? AND skip_reason=?",
                  (symbol, today, skip_reason))
        if not c.fetchone():
            c.execute("""INSERT INTO near_misses
                (symbol, date, score, threshold, gap, price_at_miss, crypto,
                 skip_reason, discipline, prices_since, last_checked)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, today, score, threshold, round(gap or 0, 2), price,
                 1 if crypto else 0, skip_reason, discipline,
                 json.dumps([]), datetime.now().isoformat()))
            conn.commit()
            c.execute("""INSERT INTO stock_stats (symbol, near_miss_count)
                         VALUES (?, 1)
                         ON CONFLICT(symbol) DO UPDATE SET
                         near_miss_count = near_miss_count + 1""", (symbol,))
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record near miss: {e}")


# ═══════════════════════════════════════════════════════════════
# NEAR-MISS FOLLOW-UP PERSISTENCE (new)
# ═══════════════════════════════════════════════════════════════
def db_update_near_miss_prices(symbol, date, prices_list, pct_move=None,
                               mfe_pct=None, mae_pct=None):
    """Persist prices_since array + excursion metrics for a score-based near-miss."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""UPDATE near_misses SET
            prices_since=?, pct_move=?, mfe_pct=?, mae_pct=?, last_checked=?
            WHERE symbol=? AND date=? AND skip_reason='SCORE'""",
            (json.dumps(prices_list), pct_move, mfe_pct, mae_pct,
             datetime.now().isoformat(), symbol, date))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"[DB] near-miss price update failed: {e}")


def db_mark_near_miss_triggered(symbol, date, trigger_price):
    """Mark a near-miss as triggered (we eventually bought it)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""UPDATE near_misses SET
            triggered=1, trigger_date=?, trigger_price=?
            WHERE symbol=? AND date=? AND skip_reason='SCORE'""",
            (datetime.now().date().isoformat(), trigger_price, symbol, date))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"[DB] near-miss mark triggered failed: {e}")


def db_update_near_miss_simulation(symbol, date, sim):
    """Save simulation output from simulate_near_miss_exit()."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""UPDATE near_misses SET
            simulated_pnl_pct=?, simulated_pnl_usd=?,
            simulated_exit_reason=?, simulated_exit_day=?
            WHERE symbol=? AND date=? AND skip_reason='SCORE'""",
            (sim.get("pnl_pct"), sim.get("pnl_usd"),
             sim.get("exit_reason"), sim.get("exit_day"),
             symbol, date))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"[DB] near-miss simulation update failed: {e}")


def db_load_near_miss_tracker(days_back=7):
    """
    Rehydrate the in-memory near_miss_tracker from DB on bot startup.
    Returns dict keyed by '{symbol}_{date}' matching the in-memory shape.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        since = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        rows = conn.execute("""SELECT symbol, date, score, threshold, gap,
            price_at_miss, prices_since, triggered, trigger_date, trigger_price,
            crypto, simulated_pnl_pct, simulated_pnl_usd,
            simulated_exit_reason, simulated_exit_day, mfe_pct, mae_pct
            FROM near_misses
            WHERE date >= ? AND skip_reason='SCORE'""", (since,)).fetchall()
        conn.close()

        tracker = {}
        for r in rows:
            (sym, d, sc, thr, gap, pam, prices_json, trig, td, tp,
             crypto, spp, spu, ser, sed, mfe, mae) = r
            try:
                prices = json.loads(prices_json) if prices_json else []
            except Exception:
                prices = []
            key = f"{sym}_{d}"
            entry = {
                "symbol": sym, "date": d, "score": sc, "threshold": thr,
                "gap": gap, "price_at_miss": pam, "prices_since": prices,
                "triggered": bool(trig), "trigger_date": td, "trigger_price": tp,
                "crypto": bool(crypto), "recorded_at": None,
            }
            if spp is not None:
                entry["simulation"] = {
                    "pnl_pct": spp, "pnl_usd": spu,
                    "exit_reason": ser, "exit_day": sed,
                    "mfe_pct": mfe, "mae_pct": mae,
                }
            tracker[key] = entry
        if tracker:
            log.info(f"[DB] Rehydrated {len(tracker)} near-misses from DB")
        return tracker
    except Exception as e:
        log.warning(f"[DB] near-miss tracker rehydration failed: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════
# ROTATION AUDIT (new — Phase 3b)
# ═══════════════════════════════════════════════════════════════
def db_record_rotation(rotation_type, sold_symbol, sold_price, sold_score,
                       sold_pnl, bought_symbol=None, bought_price=None,
                       bought_score=None, market="stock"):
    """
    Record a rotation at the moment it happens.
      rotation_type: 'SCORE_ROTATE' (Logic 1) or 'STALE_EXIT' (Logic 2)
    Returns new row id (needed later for follow-up) or None on failure.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""INSERT INTO rotations
            (rotation_type, sold_symbol, sold_at_price, sold_at_score, sold_pnl,
             bought_symbol, bought_at_price, bought_at_score, market)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (rotation_type, sold_symbol, sold_price, sold_score, sold_pnl,
             bought_symbol, bought_price, bought_score, market))
        rid = c.lastrowid
        conn.commit()
        conn.close()
        return rid
    except Exception as e:
        log.warning(f"[DB] rotation record failed: {e}")
        return None


def db_get_pending_rotations(hours=24):
    """Rotations older than `hours` that haven't yet been followed up."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""SELECT id, rotation_type, sold_symbol, bought_symbol,
            sold_at_price, bought_at_price, market
            FROM rotations
            WHERE checked_at IS NULL AND created_at <= ?""", (cutoff,)).fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] pending rotations failed: {e}")
        return []


def db_update_rotation_followup(rot_id, sold_price_24h=None, bought_price_24h=None):
    """Update rotation with 24h follow-up prices + compute verdict."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("""SELECT sold_at_price, bought_at_price, rotation_type
            FROM rotations WHERE id=?""", (rot_id,)).fetchone()
        if not row:
            conn.close()
            return
        sold_orig, bought_orig, rtype = row

        sold_pct = None
        bought_pct = None
        if sold_orig and sold_price_24h:
            sold_pct = round((sold_price_24h - sold_orig) / sold_orig * 100, 2)
        if bought_orig and bought_price_24h:
            bought_pct = round((bought_price_24h - bought_orig) / bought_orig * 100, 2)

        verdict = None
        if rtype == "SCORE_ROTATE" and sold_pct is not None and bought_pct is not None:
            diff = bought_pct - sold_pct
            if diff > 1.0:    verdict = "GOOD"
            elif diff < -1.0: verdict = "BAD"
            else:             verdict = "NEUTRAL"
        elif rtype == "STALE_EXIT" and sold_pct is not None:
            # Stale exit is "good" if the freed position stayed flat/down
            if sold_pct < 0.5:   verdict = "GOOD"
            elif sold_pct > 2.0: verdict = "BAD"
            else:                verdict = "NEUTRAL"

        conn.execute("""UPDATE rotations SET
            sold_price_24h=?, sold_pct_after=?,
            bought_price_24h=?, bought_pct_after=?,
            rotation_verdict=?, checked_at=?
            WHERE id=?""",
            (sold_price_24h, sold_pct, bought_price_24h, bought_pct,
             verdict, datetime.now().isoformat(), rot_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] rotation followup failed: {e}")


# ═══════════════════════════════════════════════════════════════
# REPORT RECORDING (unchanged)
# ═══════════════════════════════════════════════════════════════
def db_record_report(rtype, subject, body_html, body_text=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("INSERT INTO reports (type, date, subject, body_html, body_text) VALUES (?,?,?,?,?)",
                  (rtype, today, subject, body_html, body_text))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record report: {e}")


# ═══════════════════════════════════════════════════════════════
# EXISTING QUERIES (unchanged)
# ═══════════════════════════════════════════════════════════════
def db_search_symbol(symbol):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        sym = symbol.upper().strip()
        c.execute("SELECT * FROM trades WHERE symbol=? ORDER BY created_at DESC", (sym,))
        trades = c.fetchall()
        c.execute("SELECT * FROM near_misses WHERE symbol=? ORDER BY date DESC LIMIT 20", (sym,))
        misses = c.fetchall()
        c.execute("SELECT * FROM stock_stats WHERE symbol=?", (sym,))
        stats = c.fetchone()
        conn.close()
        return {"trades": trades, "near_misses": misses, "stats": stats}
    except Exception as e:
        log.warning(f"[DB] Search failed: {e}")
        return {"trades": [], "near_misses": [], "stats": None}


def db_get_leaderboard(limit=20, period_days=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if period_days:
            since = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%d")
            c.execute("""SELECT symbol,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl),2) as total_pnl,
                ROUND(MAX(pnl),2) as best,
                ROUND(MIN(pnl),2) as worst,
                ROUND(AVG(score),1) as avg_score
                FROM trades WHERE side='SELL' AND date >= ?
                GROUP BY symbol ORDER BY total_pnl DESC LIMIT ?""",
                (since, limit))
        else:
            c.execute("""SELECT symbol, total_trades, wins, losses, total_pnl,
                best_trade, worst_trade, avg_score
                FROM stock_stats ORDER BY total_pnl DESC LIMIT ?""", (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.warning(f"[DB] Leaderboard failed: {e}")
        return []


def db_get_skip_reason_breakdown():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT skip_reason, COUNT(*) as count, ROUND(AVG(score),2) as avg_score
                     FROM near_misses GROUP BY skip_reason ORDER BY count DESC""")
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def db_get_reports(limit=30, rtype=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if rtype:
            c.execute("SELECT id, type, date, subject FROM reports WHERE type=? ORDER BY date DESC LIMIT ?", (rtype, limit))
        else:
            c.execute("SELECT id, type, date, subject FROM reports ORDER BY date DESC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def db_get_report_by_id(report_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM reports WHERE id=?", (report_id,))
        row = c.fetchone()
        conn.close()
        return row
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# PHASE 2/3 ANALYTICS QUERIES (new)
# ═══════════════════════════════════════════════════════════════
def db_missed_profit_summary(days=None):
    """
    Total simulated missed profit from near-misses, grouped by discipline.
    Returns rows: (discipline, count, total_usd, avg_pct, winners_count)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        disc_expr = ("COALESCE(discipline, "
                     "CASE WHEN crypto=1 THEN 'crypto_swing' ELSE 'stock_swing' END)")
        base = f"""SELECT {disc_expr} as d,
                COUNT(*),
                ROUND(SUM(COALESCE(simulated_pnl_usd, 0)), 2),
                ROUND(AVG(simulated_pnl_pct), 2),
                SUM(CASE WHEN simulated_pnl_pct > 0 THEN 1 ELSE 0 END)
                FROM near_misses
                WHERE skip_reason='SCORE' AND simulated_pnl_pct IS NOT NULL"""
        if days:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = conn.execute(base + " AND date >= ? GROUP BY d", (since,)).fetchall()
        else:
            rows = conn.execute(base + " GROUP BY d").fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] missed profit summary failed: {e}")
        return []


def db_missed_profit_total(days=None):
    """Scalar totals for dashboard headline card: (total_usd, count, winners)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        base = """SELECT
            ROUND(SUM(COALESCE(simulated_pnl_usd, 0)), 2),
            COUNT(*),
            SUM(CASE WHEN simulated_pnl_pct > 0 THEN 1 ELSE 0 END)
            FROM near_misses
            WHERE skip_reason='SCORE' AND simulated_pnl_pct IS NOT NULL"""
        if days:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            r = conn.execute(base + " AND date >= ?", (since,)).fetchone()
        else:
            r = conn.execute(base).fetchone()
        conn.close()
        return (r[0] or 0.0, r[1] or 0, r[2] or 0)
    except Exception:
        return (0.0, 0, 0)


def db_capacity_skips(days=7):
    """
    Count skips where signal was fine but capacity/regime blocked us.
    Returns rows: (skip_reason, count, avg_score)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute("""SELECT skip_reason, COUNT(*), ROUND(AVG(score), 2)
            FROM near_misses
            WHERE date >= ? AND skip_reason != 'SCORE'
            GROUP BY skip_reason ORDER BY COUNT(*) DESC""", (since,)).fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] capacity skips failed: {e}")
        return []


def db_threshold_sensitivity():
    """
    Average outcome of near-misses, bucketed by score (0.5 bins).
    Uses persisted pct_move so this survives restarts.
    Returns rows: (score_bucket, count, avg_pct_move, winners_count)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""SELECT
            ROUND(score * 2) / 2.0 as bucket,
            COUNT(*),
            ROUND(AVG(pct_move), 2),
            SUM(CASE WHEN pct_move > 0 THEN 1 ELSE 0 END)
            FROM near_misses
            WHERE skip_reason='SCORE' AND pct_move IS NOT NULL
            GROUP BY bucket ORDER BY bucket""").fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] threshold sensitivity failed: {e}")
        return []


def db_edge_by_discipline_and_score():
    """
    Win rate and total PnL bucketed by (discipline, score floor).
    Returns rows: (discipline, score_bucket, count, wins, losses, total_pnl)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""SELECT
            COALESCE(discipline, 'swing') as disc,
            CAST(score AS INTEGER) as sb,
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),
            ROUND(SUM(pnl), 2)
            FROM trades
            WHERE side='SELL' AND score IS NOT NULL
            GROUP BY disc, sb
            ORDER BY disc, sb""").fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] edge by discipline failed: {e}")
        return []


def db_performance_by_regime():
    """
    Performance segmented by regime_at_entry.
    Returns rows: (regime, count, wins, losses, total_pnl, avg_pnl)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""SELECT
            COALESCE(regime_at_entry, 'UNKNOWN'),
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),
            ROUND(SUM(pnl), 2),
            ROUND(AVG(pnl), 2)
            FROM trades WHERE side='SELL'
            GROUP BY regime_at_entry""").fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] regime performance failed: {e}")
        return []


def db_entry_gate_attribution():
    """
    Win rate per entry gate (breakout, macd_bullish, ADX band).
    Returns dict: {gate_name: [(value, count, wins, total_pnl), ...]}
    """
    out = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        for gate in ("breakout", "macd_bullish"):
            rows = conn.execute(f"""SELECT
                {gate},
                COUNT(*),
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                ROUND(SUM(pnl), 2)
                FROM trades
                WHERE side='SELL' AND {gate} IS NOT NULL
                GROUP BY {gate}""").fetchall()
            out[gate] = rows
        adx_rows = conn.execute("""SELECT
            CASE
                WHEN adx_at_entry IS NULL THEN 'unknown'
                WHEN adx_at_entry < 20 THEN 'choppy_<20'
                WHEN adx_at_entry < 25 THEN 'building_20-25'
                ELSE 'strong_25+'
            END as bucket,
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            ROUND(SUM(pnl), 2)
            FROM trades WHERE side='SELL'
            GROUP BY bucket""").fetchall()
        out["adx"] = adx_rows
        conn.close()
    except Exception as e:
        log.debug(f"[DB] gate attribution failed: {e}")
    return out


def db_rotation_summary(days=30):
    """
    Rotation audit: verdict breakdown.
    Returns rows: (rotation_type, verdict, count, avg_sold_pct_after, avg_bought_pct_after)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute("""SELECT rotation_type, rotation_verdict, COUNT(*),
            ROUND(AVG(sold_pct_after), 2), ROUND(AVG(bought_pct_after), 2)
            FROM rotations
            WHERE created_at >= ? AND rotation_verdict IS NOT NULL
            GROUP BY rotation_type, rotation_verdict""", (since,)).fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] rotation summary failed: {e}")
        return []


def db_exit_category_breakdown(days=30):
    """
    % of trades by exit category (STOP/TP/TRAIL/SIGNAL/MAXHOLD/EOD/ROTATE/STALE).
    Returns rows: (exit_category, count, wins, total_pnl, avg_pnl)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute("""SELECT
            COALESCE(exit_category, 'UNKNOWN'),
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            ROUND(SUM(pnl), 2),
            ROUND(AVG(pnl), 2)
            FROM trades
            WHERE side='SELL' AND date >= ?
            GROUP BY exit_category ORDER BY COUNT(*) DESC""", (since,)).fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.debug(f"[DB] exit category failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# INTELLIGENCE — recommendations + runs
# ═══════════════════════════════════════════════════════════════
def db_save_recommendations(run_id, recs):
    """Store recommendation list from an intelligence run. Returns count saved."""
    try:
        conn = sqlite3.connect(DB_PATH)
        count = 0
        for r in recs:
            conn.execute("""INSERT INTO tuning_recommendations
                (run_id, category, action, parameter, discipline,
                 current_value, recommended_value, evidence, confidence, sample_size)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, r.get("category"), r.get("action"), r.get("parameter"),
                 r.get("discipline","all"), r.get("current_value"),
                 r.get("recommended_value"), r.get("evidence"),
                 r.get("confidence"), r.get("sample_size")))
            count += 1
        conn.commit(); conn.close()
        return count
    except Exception as e:
        log.debug(f"[DB] save_recommendations failed: {e}")
        return 0


def db_save_intelligence_run(run_id, narrative, raw_payload, rec_count, triggered_by="scheduled", rec_count_raw=0):
    """Archive an intelligence run."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT OR REPLACE INTO intelligence_runs
            (run_id, narrative, raw_payload, rec_count, rec_count_raw, triggered_by)
            VALUES (?,?,?,?,?,?)""",
            (run_id, narrative, raw_payload, rec_count, rec_count_raw, triggered_by))
        conn.commit(); conn.close()
    except Exception as e:
        log.debug(f"[DB] save_intelligence_run failed: {e}")


def db_get_pending_recommendations():
    """Return all PENDING recommendations as list of dicts, newest first."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT * FROM tuning_recommendations
            WHERE status='PENDING' ORDER BY created_at DESC""").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug(f"[DB] get_pending_recommendations failed: {e}")
        return []


def db_get_recommendation_history(limit=20):
    """Return actioned/dismissed/snoozed recommendations."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT * FROM tuning_recommendations
            WHERE status != 'PENDING' ORDER BY actioned_at DESC LIMIT ?""",
            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug(f"[DB] get_recommendation_history failed: {e}")
        return []


def db_apply_recommendation(rec_id):
    """Mark a recommendation APPLIED."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""UPDATE tuning_recommendations
            SET status='APPLIED', actioned_at=datetime('now') WHERE id=?""", (rec_id,))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        log.debug(f"[DB] apply_recommendation failed: {e}")
        return False


def db_dismiss_recommendation(rec_id):
    """Mark a recommendation DISMISSED."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""UPDATE tuning_recommendations
            SET status='DISMISSED', actioned_at=datetime('now') WHERE id=?""", (rec_id,))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        log.debug(f"[DB] dismiss_recommendation failed: {e}")
        return False


def db_snooze_recommendation(rec_id, days=7):
    """Snooze a recommendation for N days."""
    try:
        until = (datetime.now() + timedelta(days=days)).isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""UPDATE tuning_recommendations
            SET status='SNOOZED', snoozed_until=?, actioned_at=datetime('now') WHERE id=?""",
            (until, rec_id))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        log.debug(f"[DB] snooze_recommendation failed: {e}")
        return False


def db_get_latest_intelligence_run():
    """Return most recent intelligence run as dict, or None."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""SELECT * FROM intelligence_runs
            ORDER BY created_at DESC LIMIT 1""").fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.debug(f"[DB] get_latest_intelligence_run failed: {e}")
        return None


def db_get_intelligence_runs(limit=10):
    """Return recent intelligence run archive."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT run_id, rec_count, triggered_by, created_at, narrative
            FROM intelligence_runs ORDER BY created_at DESC LIMIT ?""",
            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug(f"[DB] get_intelligence_runs failed: {e}")
        return []


def db_log_config_change(parameter, old_value, new_value, changed_by="manual"):
    """Write a config change to the audit log."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT INTO config_history (parameter, old_value, new_value, changed_by)
            VALUES (?,?,?,?)""",
            (parameter, str(old_value) if old_value is not None else None,
             str(new_value), changed_by))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"[DB] log_config_change failed: {e}")


def db_get_config_history(limit=30):
    """Return recent config changes, newest first."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT parameter, old_value, new_value, changed_by, created_at
            FROM config_history ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug(f"[DB] get_config_history failed: {e}")
        return []


def db_get_config_history_for_intelligence(days=30):
    """Return config changes for the intelligence payload — what changed and when."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute("""SELECT parameter, old_value, new_value, changed_by, created_at
            FROM config_history WHERE created_at >= ? ORDER BY created_at DESC""",
            (since,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug(f"[DB] get_config_history_for_intelligence failed: {e}")
        return []


def db_ev_by_discipline(days=None):
    """
    Expected Value per discipline.
    EV = (win_rate * avg_win) - (loss_rate * avg_loss)
    Returns rows: (discipline, trades, wins, losses, win_rate, avg_win, avg_loss, ev, total_pnl)
    Only disciplines with >= 3 trades are included.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        base = """SELECT
            COALESCE(discipline, 'stock_swing') as disc,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
            ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2) as avg_win,
            ROUND(ABS(AVG(CASE WHEN pnl <= 0 THEN pnl END)), 2) as avg_loss,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(score), 2) as avg_score,
            ROUND(AVG(hold_hours), 2) as avg_hold_h
            FROM trades WHERE side='SELL'"""
        if days:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = conn.execute(base + " AND date >= ? GROUP BY disc HAVING trades >= 2 ORDER BY disc", (since,)).fetchall()
        else:
            rows = conn.execute(base + " GROUP BY disc HAVING trades >= 2 ORDER BY disc").fetchall()
        conn.close()

        result = []
        for r in rows:
            disc, trades, wins, losses, avg_win, avg_loss, total_pnl, avg_score, avg_hold = r
            wins   = wins   or 0
            losses = losses or 0
            avg_win  = avg_win  or 0.0
            avg_loss = avg_loss or 0.0
            win_rate  = round(wins  / trades * 100, 1) if trades else 0
            loss_rate = round(losses / trades * 100, 1) if trades else 0
            ev = round((win_rate / 100 * avg_win) - (loss_rate / 100 * avg_loss), 2)
            result.append((disc, trades, wins, losses, win_rate, avg_win, avg_loss,
                           ev, total_pnl, avg_score or 0, avg_hold or 0))
        return result
    except Exception as e:
        log.debug(f"[DB] ev_by_discipline failed: {e}")
        return []


def db_discipline_detail(discipline, days=None):
    """
    Full breakdown for a single discipline — for the per-discipline panel.
    Returns dict with trades, ev, exit_categories, score_buckets, recent_trades.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        since_clause = ""
        params_base  = [discipline]
        if days:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            since_clause = " AND date >= ?"
            params_base  = [discipline, since]

        # Core stats
        row = conn.execute(f"""SELECT COUNT(*), COALESCE(SUM(pnl),0),
            SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),
            ROUND(AVG(CASE WHEN pnl>0 THEN pnl END),2),
            ROUND(ABS(AVG(CASE WHEN pnl<=0 THEN pnl END)),2),
            ROUND(AVG(score),2), ROUND(AVG(hold_hours),2)
            FROM trades WHERE side='SELL' AND discipline=?{since_clause}""",
            params_base).fetchone()

        trades, total_pnl, wins, avg_win, avg_loss, avg_score, avg_hold = row
        trades  = trades  or 0
        wins    = wins    or 0
        losses  = trades - wins
        avg_win  = avg_win  or 0.0
        avg_loss = avg_loss or 0.0
        win_rate  = round(wins   / trades * 100, 1) if trades else 0
        loss_rate = round(losses / trades * 100, 1) if trades else 0
        ev = round((win_rate / 100 * avg_win) - (loss_rate / 100 * avg_loss), 2)

        # Exit categories
        exit_cats = conn.execute(f"""SELECT
            COALESCE(exit_category,'UNKNOWN'), COUNT(*), ROUND(SUM(pnl),2)
            FROM trades WHERE side='SELL' AND discipline=?{since_clause}
            GROUP BY exit_category ORDER BY COUNT(*) DESC""", params_base).fetchall()

        # Score buckets
        score_bkts = conn.execute(f"""SELECT
            CAST(score AS INTEGER) as sb, COUNT(*),
            SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), ROUND(SUM(pnl),2)
            FROM trades WHERE side='SELL' AND discipline=? AND score IS NOT NULL{since_clause}
            GROUP BY sb ORDER BY sb""", params_base).fetchall()

        # Recent 5 trades
        recent = conn.execute(f"""SELECT symbol, pnl, score, hold_hours, exit_category, date
            FROM trades WHERE side='SELL' AND discipline=?{since_clause}
            ORDER BY created_at DESC LIMIT 5""", params_base).fetchall()

        conn.close()
        return {
            "trades": trades, "wins": wins, "losses": losses,
            "total_pnl": round(total_pnl or 0, 2),
            "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
            "ev": ev, "avg_score": avg_score or 0, "avg_hold_h": avg_hold or 0,
            "exit_cats": list(exit_cats),
            "score_buckets": list(score_bkts),
            "recent": list(recent),
        }
    except Exception as e:
        log.debug(f"[DB] discipline detail failed for {discipline}: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════
# INITIALISE ON IMPORT
# ═══════════════════════════════════════════════════════════════
try:
    init_db()
    log.info("[DB] Trading Intelligence Database ready (v2 schema)")
except Exception as e:
    log.warning(f"[DB] Database init failed: {e}")
