"""
data/watchlist_refresh.py -- refresh 6 watchlists from the universe table.

Exchange values: "US" (IBKR SMART routes), "LSE", "ASX".

Standard priority cascade with intelligent fallbacks:
  us:            SP500 > NASDAQ100 > DJIA > SP400 -> 250
  us_smallcap:   SP600 > (SP400 tail fallback) -> 100
  ftse:          FTSE100 > FTSE250 -> 250
  ftse_smallcap: FTSE250 tail > (FTSE100 tail fallback) -> 100

ASX has special handling because ASX300 Wikipedia page is unreliable:
  If ASX300 healthy (>= 150 members):
    asx:           ASX200 > ASX300 -> 250
    asx_smallcap:  ASX300 not in swing -> 100
  If ASX300 broken (< 150 members):
    asx:           top 150 of ASX200 -> swing
    asx_smallcap:  bottom 48 of ASX200 + any ASX300-only tickers -> smallcap
    This guarantees asx_smallcap gets populated even when ASX300 broken.

US and FTSE use the same "split the pool when needed" logic but their primary
indices aren't broken so they behave normally.
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

MIN_LIST_SIZE = 15   # abort if any list falls below this
ASX300_HEALTHY_THRESHOLD = 150  # if ASX300 has >=150 members, use it normally


def _count_in_index(conn, index_name, exchange):
    """How many symbols are registered in this index?"""
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM universe_indices WHERE index_name = ? AND exchange = ?",
        (index_name, exchange)
    )
    return c.fetchone()[0]


def _query_by_indices(conn, index_priority_list, exchanges, target, exclude_set=None):
    """Return up to `target` symbols walking index_priority_list in order.
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


def _query_index_symbols(conn, index_name, exchange):
    """Return all symbols for an index, sorted alphabetically."""
    c = conn.cursor()
    c.execute(
        "SELECT u.symbol FROM universe u "
        "JOIN universe_indices ui "
        "  ON u.symbol = ui.symbol AND u.exchange = ui.exchange "
        "WHERE ui.index_name = ? AND u.exchange = ? "
        "ORDER BY u.symbol",
        (index_name, exchange)
    )
    return [r[0] for r in c.fetchall()]


def _build_asx_lists(conn, target_swing, target_smallcap):
    """Special ASX handling: if ASX300 broken, split ASX200 into swing+smallcap.
    Returns (asx_swing_list, asx_smallcap_list)."""
    asx300_count = _count_in_index(conn, "ASX300", "ASX")
    asx200_count = _count_in_index(conn, "ASX200", "ASX")
    log.info("ASX200 has %d members, ASX300 has %d members", asx200_count, asx300_count)

    if asx300_count >= ASX300_HEALTHY_THRESHOLD:
        # Normal flow: ASX200 for swing, ASX300 (not in swing) for smallcap
        asx = _query_by_indices(
            conn, ["ASX200", "ASX300"], ["ASX"], target_swing
        )
        asx_sm = _query_by_indices(
            conn, ["ASX300"], ["ASX"], target_smallcap, exclude_set=set(asx)
        )
        log.info("Using normal ASX flow (ASX300 healthy)")
        return asx, asx_sm

    # ASX300 broken: split ASX200 into halves
    log.info("ASX300 broken (%d < %d), splitting ASX200 pool",
             asx300_count, ASX300_HEALTHY_THRESHOLD)
    asx200_syms = _query_index_symbols(conn, "ASX200", "ASX")
    asx300_syms = _query_index_symbols(conn, "ASX300", "ASX")

    # Split point: leave the last 48 of ASX200 for smallcap
    # 150 top for swing, 48 bottom for smallcap, pad smallcap with any ASX300 not in swing
    split_point = min(150, max(50, len(asx200_syms) - 48))
    asx = asx200_syms[:split_point]

    # Smallcap: ASX200 tail + ASX300-only tickers
    swing_set = set(asx)
    asx_sm = []
    seen = set()
    for s in asx200_syms[split_point:]:
        if s not in swing_set and s not in seen:
            asx_sm.append(s)
            seen.add(s)
    for s in asx300_syms:
        if s not in swing_set and s not in seen:
            asx_sm.append(s)
            seen.add(s)
        if len(asx_sm) >= target_smallcap:
            break

    log.info("ASX split: %d for swing, %d for smallcap (tail of ASX200 + ASX300-only)",
             len(asx), len(asx_sm))
    return asx, asx_sm[:target_smallcap]


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

    # ---- ASX: special handling ----
    asx, asx_sm = _build_asx_lists(conn, TARGETS["asx"], TARGETS["asx_smallcap"])
    log.info("Built asx: %d tickers", len(asx))
    log.info("Built asx_smallcap: %d tickers", len(asx_sm))

    lists = {
        "us": us,
        "ftse": ftse,
        "asx": asx,
        "us_smallcap": us_sm,
        "ftse_smallcap": ftse_sm,
        "asx_smallcap": asx_sm,
    }

    # Abort if any list is severely short
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
