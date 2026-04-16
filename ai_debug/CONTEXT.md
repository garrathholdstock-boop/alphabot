# AlphaBot Debug Agent - Persistent Context

## Architecture
- VPS: 178.104.170.58 (Hetzner), user: root
- Git root: /home/alphabot/app/ (branch: master)
- Bot start: bash /home/alphabot/start.sh -> screen session "alphabot"
- **Dashboard: port 8080** (app/dashboard.py - separate thread, NOT in main loop logs)
- **Debug agent: port 8000** (ai_debug/main.py)
- DB: /home/alphabot/app/alphabot.db
- GitHub: https://github.com/garrathholdstock-boop/alphabot

## File Structure
- app/main.py (1154 lines) - main trading loop
- app/dashboard.py - web dashboard on port 8080
- core/config.py - all config and watchlists
- core/execution.py - order execution
- core/risk.py - risk management
- data/analytics.py - analytics
- data/database.py - DB operations
- start.sh - starts bot in screen session

## Config (core/config.py + .env)
- MIN_SIGNAL_SCORE=5 (raise to 7 before going live)
- IS_LIVE=false (paper trading)
- MAX_POSITIONS=3 per discipline, MAX_TOTAL_POSITIONS=15
- CYCLE_SECONDS=60
- STOP_LOSS_PCT=5%
- Brokers: Alpaca (US paper), IBKR (stops+ASX+FTSE), Binance (401 error ongoing)

## KNOWN COSMETIC ERRORS - ALWAYS IGNORE THESE
- Error 10089 HOOD/SOFI/FCEL - market data subscription, harmless
- Binance POST 401 - API permissions not fully set, crypto trades blocked but not crashing
- BrokenPipeError in dashboard - client disconnected, harmless
- Stock(symbol='candidates'...) - was a one-time glitch, already fixed

## Fixes Made By Agent (most recent first)
1. Removed sub-cent meme coins SHIB/PEPE/FLOKI/BONK from CRYPTO_WATCHLIST_BINANCE (caused scoring issues)
2. Fixed MIN_SIGNAL_SCORE default 2->5 in config
3. Fixed buys[:3]->[:10] in smallcap scan (was only passing 3 candidates)
4. Fixed tm_pct_fmt typo in dashboard

## Dashboard Notes
- Dashboard runs as a daemon thread started from app/main.py line 888-890
- It imports from app.dashboard and runs on PORT (default 8080)
- If dashboard not loading: check if port 8080 is accessible, check screen log for errors
- Dashboard has 5 stat cards + near misses + trades table

## Current Paper Positions (as of 15-Apr-26)
- SOFI, MSFT, HOOD (paper trades)
- Alpaca paper balance: ~$1,014,299

## Remaining Roadmap
- P1: Fix Binance 401 (check API key permissions in Binance dashboard)
- P1: Raise MIN_SIGNAL_SCORE 5->7 before going live
- P2: Weekly Tuning Tracker on analytics page
- P3: Pre-open news check, earnings calendar
- P5: IBKR migration for ASX+FTSE+US (account DUQ191770 approved)

## Last Updated
Never (initial setup)
