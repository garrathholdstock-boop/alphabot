"""
data/database.py — AlphaBot Trading Intelligence Database
All SQLite operations: trades, near-misses, stock stats, reports.
"""

import sqlite3
import logging
from datetime import datetime, timedelta

from core.config import log, DB_PATH

# ── Schema ────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

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
        created_at      TEXT DEFAULT (datetime('now'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        type        TEXT NOT NULL,
        date        TEXT NOT NULL,
        subject     TEXT,
        body_html   TEXT,
        body_text   TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    )""")

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

    conn.commit()
    conn.close()
    return True


# ── Trade recording ───────────────────────────────────────────
def db_record_trade(symbol, side, qty, price, pnl, score, rsi, vol_ratio,
                    hold_hours, reason, breakdown, market="stock"):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.now()
        c.execute("""INSERT INTO trades
            (symbol, side, qty, price, pnl, score, rsi, vol_ratio,
             hold_hours, reason, signal_breakdown, market, date, time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, side, qty, price, pnl, score, rsi, vol_ratio,
             hold_hours, reason, breakdown, market,
             now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")))
        conn.commit()

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


# ── Near-miss recording ───────────────────────────────────────
def db_record_near_miss(symbol, score, threshold, gap, price, crypto=False, skip_reason="SCORE"):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT id FROM near_misses WHERE symbol=? AND date=?", (symbol, today))
        if not c.fetchone():
            c.execute("""INSERT INTO near_misses
                (symbol, date, score, threshold, gap, price_at_miss, crypto, skip_reason)
                VALUES (?,?,?,?,?,?,?,?)""",
                (symbol, today, score, threshold, round(gap,2), price,
                 1 if crypto else 0, skip_reason))
            conn.commit()
            c.execute("""INSERT INTO stock_stats (symbol, near_miss_count)
                         VALUES (?, 1)
                         ON CONFLICT(symbol) DO UPDATE SET
                         near_miss_count = near_miss_count + 1""", (symbol,))
            conn.commit()
        else:
            c.execute("UPDATE near_misses SET skip_reason=? WHERE symbol=? AND date=?",
                      (skip_reason, symbol, today))
            conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[DB] Failed to record near miss: {e}")


# ── Report recording ──────────────────────────────────────────
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


# ── Queries ───────────────────────────────────────────────────
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
    except:
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
    except:
        return []

def db_get_report_by_id(report_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM reports WHERE id=?", (report_id,))
        row = c.fetchone()
        conn.close()
        return row
    except:
        return None


# ── Initialise on import ──────────────────────────────────────
try:
    init_db()
    log.info("[DB] Trading Intelligence Database ready")
except Exception as e:
    log.warning(f"[DB] Database init failed: {e}")
