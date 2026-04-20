"""
data/watchlist_refresh.py -- refresh 6 watchlists from the universe table.

Exchange values: "US" (IBKR SMART routes), "LSE", "ASX".

Priority cascade with smart fallbacks:
  us:            SP500 > NASDAQ100 > DJIA > SP400 -> 250
  us_smallcap:   SP600 > (SP400 tail if SP600 short) -> 100
  ftse:          FTSE100 > FTSE250 -> 250
  ftse_smallcap: FTSE250 tail not in swing > (FTSE100 tail if FTSE250 short) -> 100
  asx:           ASX200 > ASX300 -> 250
  asx_smallcap:  ASX300 not in swing > (ASX200 tail fallback if ASX300 short) -> 100

Fallbacks kick in when an index page is broken/short, so we still build a
usable watchlist rather than returning 0.
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

# Minimum acceptable size per list -- below this we call it a failure.
# Lowered from 25 to 15 to tolerate thin fallback cases.
MIN_LIST_SIZE = 15


def _query_by_indices(conn, index_priority_list, exchanges, target, exclude_set=None):
    """Return up to `target` symbols, walking index_priority_list in order.
    Deduplicates against exclude_set."""
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
        result["errors"].append("Universe has only %d symbols, run refresh_universe first" % total)
        conn.close()
        return result

    log.info("Universe contains %d symbols, building watchlists", total)

    # ---- US SWING ----
    us = _query_by_indices(
        conn, ["SP500", "NASDAQ100", "DJIA", "SP400"], ["US"], TARGETS["us"]
    )
    log.info("Built us: %d tickers", len(us))

    # ---- US SMALLCAP (with SP400 tail fallback) ----
    us_sm = _query_by_indices(
        conn, ["SP600"], ["US"], TARGETS["us_smallcap"], exclude_set=set(us)
    )
    if len(us_sm) < TARGETS["us_smallcap"]:
        # Fallback: grab SP400 tail not already used
        extra = _query_by_indices(
            conn, ["SP400"], ["US"],
            TARGETS["us_smallcap"] - len(us_sm),
            exclude_set=set(us) | set(us_sm)
        )
        if extra:
            log.info("us_smallcap fallback: added %d from SP400 tail", len(extra))
        us_sm.extend(extra)
    log.info("Built us_smallcap: %d tickers", len(us_sm))

    # ---- FTSE SWING ----
    ftse = _query_by_indices(
        conn, ["FTSE100", "FTSE250"], ["LSE"], TARGETS["ftse"]
    )
    log.info("Built ftse: %d tickers", len(ftse))

    # ---- FTSE SMALLCAP (with FTSE100 tail fallback) ----
    ftse_sm = _query_by_indices(
        conn, ["FTSE250"], ["LSE"], TARGETS["ftse_smallcap"], exclude_set=set(ftse)
    )
    if len(ftse_sm) < TARGETS["ftse_smallcap"]:
        extra = _query_by_indices(
            conn, ["FTSE100"], ["LSE"],
            TARGETS["ftse_smallcap"] - len(ftse_sm),
            exclude_set=set(ftse) | set(ftse_sm)
        )
        if extra:
            log.info("ftse_smallcap fallback: added %d from FTSE100 tail", len(extra))
        ftse_sm.extend(extra)
    log.info("Built ftse_smallcap: %d tickers", len(ftse_sm))

    # ---- ASX SWING ----
    asx = _query_by_indices(
        conn, ["ASX200", "ASX300"], ["ASX"], TARGETS["asx"]
    )
    log.info("Built asx: %d tickers", len(asx))

    # ---- ASX SMALLCAP (with ASX200 tail fallback when ASX300 broken) ----
    asx_sm = _query_by_indices(
        conn, ["ASX300"], ["ASX"], TARGETS["asx_smallcap"], exclude_set=set(asx)
    )
    if len(asx_sm) < TARGETS["asx_smallcap"]:
        # Fallback: take the tail of ASX200 not already in asx swing.
        # Useful when ASX300 Wikipedia page is broken/short.
        extra = _query_by_indices(
            conn, ["ASX200"], ["ASX"],
            TARGETS["asx_smallcap"] - len(asx_sm),
            exclude_set=set(asx) | set(asx_sm)
        )
        if extra:
            log.info("asx_smallcap fallback: added %d from ASX200 tail", len(extra))
        asx_sm.extend(extra)
    log.info("Built asx_smallcap: %d tickers", len(asx_sm))

    lists = {
        "us": us,
        "ftse": ftse,
        "asx": asx,
        "us_smallcap": us_sm,
        "ftse_smallcap": ftse_sm,
        "asx_smallcap": asx_sm,
    }

    # Sanity check -- abort only if any list is very small
    low_counts = {k: len(v) for k, v in lists.items() if len(v) < MIN_LIST_SIZE}
    if low_counts:
        msg = "Low ticker counts: %s, aborting to preserve current watchlists" % low_counts
        log.error(msg)
        result["errors"].append(msg)
        result["counts"] = {k: len(v) for k, v in lists.items()}
        result["took_seconds"] = round(time.time() - t0, 1)
        conn.close()
        return result

    # Archive existing watchlists
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
