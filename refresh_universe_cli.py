#!/usr/bin/env python3
"""
refresh_universe_cli.py -- one-shot universe + watchlist refresh.

Manual:  cd /home/alphabot/app && python3 refresh_universe_cli.py
Cron:    add to crontab with appropriate schedule

Safe: atomic DB writes, archives existing before overwrite. If any step
fails, current watchlists remain intact.
"""
import sys
import time
import json
import os

# Force UTF-8 output regardless of VPS locale
os.environ["PYTHONIOENCODING"] = "utf-8"

sys.path.insert(0, "/home/alphabot/app")


def main():
    t0 = time.time()
    print("=" * 60)
    print("AlphaBot Universe + Watchlist Refresh")
    print("=" * 60)

    # Step 1: refresh universe
    print()
    print("[1/2] Fetching IBKR universe + index memberships...")
    print("      This takes 1-5 minutes (polite rate-limiting).")
    print()
    from data.universe_loader import refresh_universe
    u_result = refresh_universe()
    print(json.dumps(u_result, indent=2, default=str))
    if not u_result["ok"]:
        print()
        print("FAILED: Universe refresh errored. Aborting watchlist refresh.")
        print("Errors: %s" % u_result["errors"])
        sys.exit(1)

    # Step 2: build watchlists
    print()
    print("[2/2] Building watchlists from universe...")
    print()
    from data.watchlist_refresh import refresh_watchlists_from_universe
    w_result = refresh_watchlists_from_universe()
    summary = {k: v for k, v in w_result.items() if k != "picked"}
    print(json.dumps(summary, indent=2, default=str))
    if not w_result["ok"]:
        print()
        print("FAILED: Watchlist refresh errored.")
        print("Errors: %s" % w_result["errors"])
        sys.exit(1)

    # Summary
    took = round(time.time() - t0, 1)
    print()
    print("=" * 60)
    print("COMPLETE in %.1fs" % took)
    print("=" * 60)
    print()
    print("Final watchlist sizes:")
    for market, count in w_result["counts"].items():
        print("  %-15s %4d tickers" % (market, count))
    print()
    print("Bot restart required to apply: systemctl restart alphabot")


if __name__ == "__main__":
    main()
