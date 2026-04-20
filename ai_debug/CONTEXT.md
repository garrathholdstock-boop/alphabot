# AlphaBot Debug Agent - Persistent Context

## CRITICAL — HOW TO START/RESTART SERVICES
**We use systemd — NOT screen sessions, NOT start.sh**

- `systemctl restart alphabot`           → restarts trading bot
- `systemctl restart alphabot-dashboard` → restarts dashboard  
- `systemctl restart alphabot-agent`     → restarts this agent
- `systemctl status alphabot`            → check if running
- `journalctl -u alphabot -n 50`         → systemd logs

**NEVER use:** `bash /home/alphabot/start.sh` or `screen` commands

## Architecture
- VPS: 178.104.170.58 (Hetzner Ubuntu 24.04), user: root, Paris = UTC+2
- Git root: /home/alphabot/app/ (branch: main)
- Dashboard: port 8080 | Debug agent: port 8000
- DB: /home/alphabot/app/alphabot.db (NEVER delete)
- Log: /home/alphabot/app/alphabot.log
- GitHub: https://github.com/garrathholdstock-boop/alphabot
- API key: /home/alphabot/app/.claude_api_key (update via maintenance page button)

## File Structure
- app/main.py — main trading loop (10 disciplines)
- app/dashboard.py — web dashboard port 8080
- core/config.py — all config + watchlists
- core/execution.py — order execution (IBKR + Binance), unique clientIds per thread
- data/database.py — DB operations (bot_status + smallcap_watchlists tables added)
- ai_debug/main.py — this agent (port 8000)
- trading_config.json — hot-reloaded every cycle (no restart needed)

## 10 Trading Disciplines & IBKR ClientIds
1. MainThread → clientId=1
2. US-Swing → clientId=2
3. Intraday → clientId=3
4. FTSE → clientId=4
5. ASX → clientId=5
6. Smallcap-US → clientId=6
7. Smallcap-FTSE → clientId=7
8. Smallcap-ASX → clientId=8
9. Crypto-Swing → clientId=9
10. Bear → clientId=10

## Market Hours (Paris time)
- US: 3:30pm–10pm Mon–Fri
- FTSE: 9am–5:30pm Mon–Fri
- ASX: 1am–7am Mon–Fri (IBKR opens Sunday 11pm UTC)
- Crypto: 24/7

## Database Tables
- trades, near_misses, rotations, tuning_recommendations
- intelligence_runs, stock_stats, agent_events, reports, config_history
- bot_status — DB-backed status snapshot (replaces status.json)
- smallcap_watchlists — written by Refresh Small Caps, loaded on bot startup

## Log Rotation
To archive old log and start fresh:
`mv /home/alphabot/app/alphabot.log /home/alphabot/app/alphabot.log.old && systemctl restart alphabot`

## Trading Config (hot-reload, no restart needed)
- trading_config.json — edit values, bot picks up next cycle
- IS_LIVE=false (paper trading until 2-week period complete ~30 Apr)
- MIN_SIGNAL_SCORE=5 (raise to 7 before going live)
