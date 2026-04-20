"""
data/watchlist_refresh.py -- refresh 6 watchlists from the universe table

Picks highest-priority tradeable symbols per market, writes to the
`watchlists` table that main.py reads at startup.

Priority heuristic (index membership):
  US swing:      SP500 > NASDAQ100 > DJIA > SP400 -> target 250
  US smallcap:   SP600 -> target 100
  FTSE swing:    FTSE100 > FTSE250 -> target 250
  FTSE smallcap: FTSE250 tail not in swing -> target 100
  ASX swing:     ASX200 > ASX300 -> target 250
  ASX smallcap:  ASX300 not in swing -> target 100

Safety:
  - Archives current watchlists to watchlists_history before overwriting
  - If universe empty or <500 rows, aborts
  - If any watchlist <25 symbols, aborts
  - ASCII-safe logging and error messages
"""
import logging
import sqlite3
import json
import time

DB_PATH = "/home/alphabot/app/alphabot.db"


def _ascii_safe(s):
    try:
        return str(s).encode("ascii", errors="replace").decode("ascii")
    except Exception:
        return "<unencodable>"


log = logging.getLogger("watchlist_refresh")
if not log.handlers:
    import sys
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [WL-REFRESH] %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _get_conn():
    return sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)


TARGETS = {
    "us":            250,
    "ftse":          250,
    "asx":           250,
    "us_smallcap":   100,
    "ftse_smallcap": 100,
    "asx_smallcap":  100,
}


def _query_by_indices(conn, index_priority_list, exchanges, target, exclude_set=None):
    """Return up to `target` symbols ranked by index priority."""
    exclude_set = exclude_set or set()
    c = conn.cursor()
    selected = []
    seen = set()
    for idx_name in index_priority_list:
        if len(selected) >= target:
            break
        placeholders = ",".join("?" for _ in exchanges)
        c.execute(
            "SELECT u.symbol, u.exchange "
            "FROM universe u "
            "JOIN universe_indices ui "
            "  ON u.symbol = ui.symbol AND u.exchange = ui.exchange "
            "WHERE ui.index_name = ? AND u.exchange IN (%s) "
            "ORDER BY u.symbol" % placeholders,
            (idx_name, *exchanges)
        )
        for sym, exch in c.fetchall():
            key = (sym, exch)
            if key in seen or sym in exclude_set:
                continue
            seen.add(key)
            selected.append(sym)
            if len(selected) >= target:
                break
    return selected


def refresh_watchlists_from_universe():
    """
    Build 6 watchlists from universe and write to watchlists table.
    Returns dict with counts, selections, and any errors.
    """
    t0 = time.time()
    result = {
        "ok": False,
        "counts": {},
        "picked": {},
        "errors": [],
        "took_seconds": 0.0,
    }

    conn = _get_conn()
    c = conn.cursor()

    try:
        c.execute("SELECT COUNT(*) FROM universe")
        total = c.fetchone()[0]
    except Exception as e:
        result["errors"].append("Universe table missing: %s" % _ascii_safe(e))
        conn.close()
        return result

    if total < 500:
        result["errors"].append(
            "Universe has only %d symbols, run refresh_universe first" % total
        )
        conn.close()
        return result

    log.info("Universe contains %d symbols, building watchlists", total)

    us = _query_by_indices(
        conn, ["SP500", "NASDAQ100", "DJIA", "SP400"], ["NYSE", "NASDAQ"], TARGETS["us"]
    )
    us_sm = _query_by_indices(
        conn, ["SP600"], ["NYSE", "NASDAQ"], TARGETS["us_smallcap"], exclude_set=set(us)
    )
    ftse = _query_by_indices(
        conn, ["FTSE100", "FTSE250"], ["LSE"], TARGETS["ftse"]
    )
    ftse_sm = _query_by_indices(
        conn, ["FTSE250"], ["LSE"], TARGETS["ftse_smallcap"], exclude_set=set(ftse)
    )
    asx = _query_by_indices(
        conn, ["ASX200", "ASX300"], ["ASX"], TARGETS["asx"]
    )
    asx_sm = _query_by_indices(
        conn, ["ASX300"], ["ASX"], TARGETS["asx_smallcap"], exclude_set=set(asx)
    )

    lists = {
        "us": us,
        "ftse": ftse,
        "asx": asx,
        "us_smallcap": us_sm,
        "ftse_smallcap": ftse_sm,
        "asx_smallcap": asx_sm,
    }

    low_counts = {k: len(v) for k, v in lists.items() if len(v) < 25}
    if low_counts:
        msg = "Low ticker counts: %s -- aborting to preserve current watchlists" % low_counts
        log.error(msg)
        result["errors"].append(msg)
        result["counts"] = {k: len(v) for k, v in lists.items()}
        result["took_seconds"] = round(time.time() - t0, 1)
        conn.close()
        return result

    # Archive existing
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlists_history (
                archived_at TEXT DEFAULT (datetime('now')),
                market      TEXT,
                tickers     TEXT,
                updated_at  TEXT
            )
        """)
        c.execute("""
            INSERT INTO watchlists_history (market, tickers, updated_at)
            SELECT market, tickers, updated_at FROM watchlists
        """)
        conn.commit()
        log.info("Current watchlists archived to watchlists_history")
    except Exception as e:
        log.warning("Archive skipped: %s", _ascii_safe(e))

    # Write new watchlists
    try:
        c.execute("BEGIN")
        for market, tickers in lists.items():
            c.execute(
                "INSERT INTO watchlists (market, tickers, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(market) DO UPDATE SET "
                "    tickers = excluded.tickers, "
                "    updated_at = excluded.updated_at",
                (market, json.dumps(tickers))
            )
        conn.commit()
        log.info("Wrote 6 watchlists: %s",
                 ", ".join("%s=%d" % (k, len(v)) for k, v in lists.items()))
    except Exception as e:
        conn.rollback()
        result["errors"].append("DB write: %s" % _ascii_safe(e))
        conn.close()
        return result

    conn.close()

    result["ok"] = True
    result["counts"] = {k: len(v) for k, v in lists.items()}
    result["picked"] = {k: v for k, v in lists.items()}
    result["took_seconds"] = round(time.time() - t0, 1)
    return result


if __name__ == "__main__":
    import sys
    r = refresh_watchlists_from_universe()
    summary = {k: v for k, v in r.items() if k != "picked"}
    print(json.dumps(summary, indent=2))
    sys.exit(0 if r["ok"] else 1)
