#!/usr/bin/env python3
"""
cleanup_dead_tickers.py -- one-shot removal of DEAD_IBKR_TICKERS from
currently-stored watchlists in the DB.

Run once after deploying the patched data/watchlist_refresh.py. The next
full universe+watchlist refresh will also apply the filter, but this lets
us clean existing state without waiting for that.

Safe:
  - Reads the blocklist from data.watchlist_refresh (single source of truth)
  - Archives existing watchlists to watchlists_history before overwriting
  - Atomic transaction (BEGIN/COMMIT or full rollback on error)
  - Prints before/after counts per market
  - Skips any market whose watchlist is already empty

Usage:
  cd /home/alphabot/app && python3 cleanup_dead_tickers.py
"""
import json
import sqlite3
import sys

sys.path.insert(0, "/home/alphabot/app")

from data.watchlist_refresh import DEAD_IBKR_TICKERS

DB_PATH = "/home/alphabot/app/alphabot.db"


def main():
    print("=" * 60)
    print("Dead-ticker cleanup from DB watchlists")
    print("=" * 60)
    print("Blocklist size: %d ticker(s)" % len(DEAD_IBKR_TICKERS))
    print()

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    c = conn.cursor()

    # Fetch current watchlists
    try:
        rows = c.execute(
            "SELECT market, tickers, updated_at FROM watchlists ORDER BY market"
        ).fetchall()
    except Exception as e:
        print("ERROR: Cannot read watchlists table: %s" % e)
        conn.close()
        sys.exit(1)

    if not rows:
        print("No watchlists in DB. Nothing to clean.")
        print("(Bot is probably using config.py defaults.)")
        conn.close()
        sys.exit(0)

    print("Current watchlists:")
    changes = []  # list of (market, new_tickers_json, removed_list)
    for market, tickers_json, updated in rows:
        try:
            tickers = json.loads(tickers_json)
        except Exception as e:
            print("  %s: SKIP (unparseable: %s)" % (market, e))
            continue

        before = len(tickers)
        removed = [t for t in tickers if t in DEAD_IBKR_TICKERS]
        clean   = [t for t in tickers if t not in DEAD_IBKR_TICKERS]
        after   = len(clean)

        if removed:
            print("  %-15s %d -> %d  (removed: %s)"
                  % (market, before, after, ", ".join(sorted(removed))))
            changes.append((market, json.dumps(clean), removed))
        else:
            print("  %-15s %d -> %d  (no change)" % (market, before, after))

    print()
    if not changes:
        print("No dead tickers found in any watchlist. Done.")
        conn.close()
        sys.exit(0)

    total_removed = sum(len(r) for _, _, r in changes)
    print("Will remove %d ticker(s) across %d watchlist(s)."
          % (total_removed, len(changes)))
    print()

    # Ensure archive table exists
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlists_history (
                archived_at TEXT DEFAULT (datetime('now')),
                market      TEXT,
                tickers     TEXT,
                updated_at  TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        print("ERROR: Cannot create watchlists_history table: %s" % e)
        conn.close()
        sys.exit(1)

    # Atomic update: archive, then overwrite in one transaction.
    try:
        c.execute("BEGIN")
        c.execute(
            "INSERT INTO watchlists_history (market, tickers, updated_at) "
            "SELECT market, tickers, updated_at FROM watchlists"
        )
        for market, new_json, _ in changes:
            c.execute(
                "UPDATE watchlists SET tickers = ?, updated_at = datetime('now') "
                "WHERE market = ?",
                (new_json, market)
            )
        conn.commit()
        print("Cleanup complete. Previous state archived to watchlists_history.")
    except Exception as e:
        conn.rollback()
        print("ERROR during write, rolled back: %s" % e)
        conn.close()
        sys.exit(1)

    conn.close()
    print()
    print("Bot will pick up cleaned lists on next watchlist reload OR restart.")
    print("To force-apply immediately: systemctl restart alphabot")


if __name__ == "__main__":
    main()
