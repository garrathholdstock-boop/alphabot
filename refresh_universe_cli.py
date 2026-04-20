#!/usr/bin/env python3
"""
refresh_universe_cli.py — one-shot script to refresh universe + watchlists.

Can be run manually or via cron. After completion, bot needs restart for
new watchlists to take effect.

Usage:
    cd /home/alphabot/app
    python3 refresh_universe_cli.py

Safe to run: all DB writes are atomic + archived. If anything fails mid-run,
existing watchlists are preserved.
"""
import sys
import time
import json

sys.path.insert(0, "/home/alphabot/app")


def main():
    t0 = time.time()
    print("=" * 60)
    print("AlphaBot Universe + Watchlist Refresh")
    print("=" * 60)

    # Step 1: refresh universe
    print("\n[1/2] Fetching IBKR universe + index memberships...")
    print("      This takes 1-5 minutes (polite rate-limiting).")
    print()
    from data.universe_loader import refresh_universe
    u_result = refresh_universe()
    print(json.dumps({k: v for k, v in u_result.items() if k != "picked"}, indent=2, default=str))
    if not u_result["ok"]:
        print("\n❌ Universe refresh failed — aborting watchlist refresh")
        print(f"Errors: {u_result['errors']}")
        sys.exit(1)

    # Step 2: build watchlists from universe
    print("\n[2/2] Building watchlists from universe...")
    print()
    from data.watchlist_refresh import refresh_watchlists_from_universe
    w_result = refresh_watchlists_from_universe()
    summary = {k: v for k, v in w_result.items() if k != "picked"}
    print(json.dumps(summary, indent=2, default=str))
    if not w_result["ok"]:
        print("\n❌ Watchlist refresh failed")
        print(f"Errors: {w_result['errors']}")
        sys.exit(1)

    # Summary
    took = round(time.time() - t0, 1)
    print("\n" + "=" * 60)
    print(f"✅ COMPLETE in {took}s")
    print("=" * 60)
    print("\nFinal watchlist sizes:")
    for market, count in w_result["counts"].items():
        print(f"  {market:15s} {count:4d} tickers")
    print("\n⚠️  Bot restart required to apply: systemctl restart alphabot")


if __name__ == "__main__":
    main()
