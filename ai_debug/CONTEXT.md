# AlphaBot Debug Agent - Persistent Context
## Last Updated
18-Apr-2026 10:20 Paris (auto-updated by agent)

## Architecture
- VPS: 178.104.170.58 (Hetzner), user: root, Paris = UTC+2
- Git root: /home/alphabot/app/ (branch: main)
- Bot start: bash /home/alphabot/start.sh → screen session "alphabot"
- start.sh runs: python3 -m app.main (NOT python3 app/main.py)
- Dashboard: port 8080 | Debug agent: port 8000
- DB: /home/alphabot/app/alphabot.db
- GitHub: https://github.com/garrathholdstock-boop/alphabot

## File Structure
- app/main.py — main trading loop (6 disciplines)
- app/dashboard.py — web dashboard port 8080
- core/config.py — all config + watchlists
- core/execution.py — order execution (IBKR + Binance)
- core/risk.py — risk management
- data/analytics.py — signal scoring
- data/database.py — DB operations
- ai_debug/main.py — this agent (port 8000)
- start.sh — starts bot in screen session

## Config (core/config.py + .env)
- MIN_SIGNAL_SCORE=5 (RAISE TO 7 BEFORE GOING LIVE)
- IS_LIVE=false (paper trading — DUQ191770)
- MAX_POSITIONS=3 per discipline, MAX_TOTAL_POSITIONS=15
- CYCLE_SECONDS=60
- STOP_LOSS_PCT=5%
- Brokers: IBKR (US stocks + ASX + FTSE), Binance TESTNET (crypto)
- BINANCE_TESTNET=true — BINANCE_SECRET may be corrupted (check .env)

## Bot Architecture — 6 Disciplines
1. US Stocks (state) — 9am ET daily scan
2. US Intraday (intraday_state) — 9:30am-4pm ET
3. Small Cap (smallcap_state) — US hours
4. ASX (asx_state) — 2am-8am Paris
5. FTSE (ftse_state) — 9am-5:30pm Paris
6. Crypto Intraday (crypto_intraday_state) — 24/7 Binance testnet

## Current Status
- Bot running: YES
- All-time P&L: $-0.36 (13 trades, 23% win rate)
- Open positions: HOOD x7540, TSLA x257, PLUG x70702 (check dashboard for live)

## KNOWN COSMETIC ERRORS — ALWAYS IGNORE
- Error 10089, Error 300 — market data subscription, harmless
- BrokenPipeError in dashboard — client disconnected, harmless
- DeprecationWarning utcnow() — Python 3.12, cosmetic
- reqHistoricalData Timeout for SPY — harmless, retries next cycle
- Can't find EId with tickerId — harmless IBKR cosmetic

## Priority Matrix
- P1 SAFETY: bot down, stop not firing, IBKR disconnect, kill switch, daily loss limit
- P2 EFFICIENCY: no trades 90+ mins, Binance failing, zero scans, execution block
- P3 BUGS: dashboard mismatches, near-miss anomalies, new unknown errors

## Remaining Roadmap
- P1: Verify BINANCE_SECRET not corrupted; raise MIN_SIGNAL_SCORE 5→7 before live
- P2: Minimum hold time on rotation (10-15 min); Weekly Tuning Tracker
- P3: Pre-open news 9:30am Paris; earnings calendar
- P4: ATR stops (after 2wk paper); CYCLE_SECONDS 60→300
- P5: IS_LIVE=true, IBKR live account DUQ191770

## Deploy Workflow
cd /home/alphabot/app && git pull origin main
screen -S alphabot -X quit && sleep 2 && bash /home/alphabot/start.sh

## Emergency
pkill -9 -f python3 && screen -wipe && bash /home/alphabot/start.sh
