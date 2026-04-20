"""
data/watchlist_refresh.py — refresh 6 watchlists from the universe table

Picks the highest-priority tradeable symbols per market, writes to the
`watchlists` table that main.py reads at startup.

Priority heuristic (index membership, no external data needed):
  US swing:      SP500 > NASDAQ100 > DJIA > SP400 → target 250
  US smallcap:   SP600 → target 100
  FTSE swing:    FTSE100 > FTSE250 → target 250
  FTSE smallcap: FTSE250 tail (rank 100-250 by alphabetical) → target 100
                 (FTSE SmallCap Wikipedia page not reliable — use FTSE250 tail as proxy)
  ASX swing:     ASX200 → target 250 (padded with ASX300 if needed)
  ASX smallcap:  ASX300 excluding ASX200 → target 100

Safety:
  - Archives current watchlists to watchlists_history before overwriting
  - If universe table is empty or <500 rows, aborts with clear error
  - Returns detailed counts + what was picked for audit

Usage:
    from data.watchlist_refresh import refresh_watchlists_from_universe
    result = refresh_watchlists_from_universe()
"""
import logging
import sqlite3
import json
import time

DB_PATH = "/home/alphabot/app/alphabot.db"

log = logging.getLogger("watchlist_refresh")
if not log.handlers:
    import sys
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [WL-REFRESH] %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _get_conn():
    return sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)


# ═══════════════════════════════════════════════════════════════
# TARGETS
# ═══════════════════════════════════════════════════════════════
TARGETS = {
    "us":            250,
    "ftse":          250,
    "asx":           250,
    "us_smallcap":   100,
    "ftse_smallcap": 100,
    "asx_smallcap":  100,
}


# ═══════════════════════════════════════════════════════════════
# SELECTORS
# ═══════════════════════════════════════════════════════════════
def _query_by_indices(conn, index_priority_list, exchanges, target, exclude_set=None):
    """
    Return up to `target` symbols from universe that belong to at least one
    of the indices in `index_priority_list`, ranked by priority (first index wins).
    Symbols in exclude_set are skipped.
    """
    exclude_set = exclude_set or set()
    c = conn.cursor()
    selected = []
    seen = set()
    for idx_name in index_priority_list:
        if len(selected) >= target:
            break
        placeholders = ",".join("?" for _ in exchanges)
        c.execute(f"""
            SELECT u.symbol, u.exchange
            FROM universe u
            JOIN universe_indices ui
              ON u.symbol = ui.symbol AND u.exchange = ui.exchange
            WHERE ui.index_name = ?
              AND u.exchange IN ({placeholders})
            ORDER BY u.symbol
        """, (idx_name, *exchanges))
        for sym, exch in c.fetchall():
            key = (sym, exch)
            if key in seen or sym in exclude_set:
                continue
            seen.add(key)
            selected.append(sym)
            if len(selected) >= target:
                break
    return selected


# ═══════════════════════════════════════════════════════════════
# MAIN REFRESH
# ═══════════════════════════════════════════════════════════════
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

    # Sanity check universe
    try:
        c.execute("SELECT COUNT(*) FROM universe")
        total = c.fetchone()[0]
    except Exception as e:
        result["errors"].append(f"Universe table missing: {e}")
        conn.close()
        return result

    if total < 500:
        result["errors"].append(
            f"Universe has only {total} symbols — run refresh_universe first"
        )
        conn.close()
        return result

    log.info(f"Universe contains {total} symbols — building watchlists")

    # ── Build each list ──
    # US swing: SP500 (priority) → NASDAQ100 → DJIA → SP400
    us = _query_by_indices(
        conn,
        ["SP500", "NASDAQ100", "DJIA", "SP400"],
        ["NYSE", "NASDAQ"],
        TARGETS["us"],
    )
    # US smallcap: SP600
    us_sm = _query_by_indices(
        conn,
        ["SP600"],
        ["NYSE", "NASDAQ"],
        TARGETS["us_smallcap"],
        exclude_set=set(us),
    )

    # FTSE swing: FTSE100 then FTSE250 (first ~150 after FTSE100 is already 100)
    ftse = _query_by_indices(
        conn,
        ["FTSE100", "FTSE250"],
        ["LSE"],
        TARGETS["ftse"],
    )
    # FTSE smallcap: take remaining FTSE250 not already in swing (up to 100)
    ftse_sm = _query_by_indices(
        conn,
        ["FTSE250"],
        ["LSE"],
        TARGETS["ftse_smallcap"],
        exclude_set=set(ftse),
    )

    # ASX swing: ASX200 then pad with ASX300
    asx = _query_by_indices(
        conn,
        ["ASX200", "ASX300"],
        ["ASX"],
        TARGETS["asx"],
    )
    # ASX smallcap: ASX300 excluding what's in swing
    asx_sm = _query_by_indices(
        conn,
        ["ASX300"],
        ["ASX"],
        TARGETS["asx_smallcap"],
        exclude_set=set(asx),
    )

    lists = {
        "us": us,
        "ftse": ftse,
        "asx": asx,
        "us_smallcap": us_sm,
        "ftse_smallcap": ftse_sm,
        "asx_smallcap": asx_sm,
    }

    # Quality check — if any list has <25 symbols, something's wrong
    low_counts = {k: len(v) for k, v in lists.items() if len(v) < 25}
    if low_counts:
        msg = f"Low ticker counts: {low_counts} — aborting to preserve current watchlists"
        log.error(msg)
        result["errors"].append(msg)
        result["counts"] = {k: len(v) for k, v in lists.items()}
        result["took_seconds"] = round(time.time() - t0, 1)
        conn.close()
        return result

    # ── Archive existing watchlists before overwriting ──
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
        log.warning(f"Archive skipped: {e}")

    # ── Write new watchlists ──
    try:
        c.execute("BEGIN")
        for market, tickers in lists.items():
            c.execute("""
                INSERT INTO watchlists (market, tickers, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(market) DO UPDATE SET
                    tickers = excluded.tickers,
                    updated_at = excluded.updated_at
            """, (market, json.dumps(tickers)))
        conn.commit()
        log.info(f"Wrote 6 watchlists: {', '.join(f'{k}={len(v)}' for k, v in lists.items())}")
    except Exception as e:
        conn.rollback()
        result["errors"].append(f"DB write: {e}")
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
    # Don't print full ticker lists, just counts
    summary = {k: v for k, v in r.items() if k != "picked"}
    print(json.dumps(summary, indent=2))
    sys.exit(0 if r["ok"] else 1)
