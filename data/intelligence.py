"""
data/intelligence.py — AlphaBot Weekly Intelligence Engine
───────────────────────────────────────────────────────────
Assembles performance data from the DB, sends to Claude API,
parses structured recommendations, and stores them for review.

Claude's mandate (baked into the system prompt):
  • Increase profits carefully and cautiously while protecting capital
  • Open to more trades per day and more exposure IF evidence supports it
  • Never recommend IS_LIVE=true — that is Garrath's decision only
  • Always cite sample size — low-sample recommendations are flagged LOW confidence
  • Err on the side of caution on stop losses and position sizing
  • OBSERVATION category for things noticed but not yet actionable

Triggered:
  • Sunday 6pm ET (scheduled, from main.py)
  • Manual via /intelligence?run=now (PIN-gated, from dashboard)
"""

import json
import uuid
import logging
import requests
from datetime import datetime

from core.config import log, CLAUDE_API_KEY, MIN_SIGNAL_SCORE
import core.config as cfg
from data.database import (
    db_missed_profit_summary, db_missed_profit_total,
    db_capacity_skips, db_threshold_sensitivity,
    db_edge_by_discipline_and_score, db_performance_by_regime,
    db_entry_gate_attribution, db_rotation_summary, db_exit_category_breakdown,
    db_get_leaderboard, db_get_skip_reason_breakdown,
    db_save_intelligence_run, db_save_recommendations,
    db_ev_by_discipline, db_get_config_history_for_intelligence,
)

# ── System prompt (the mandate) ───────────────────────────────
SYSTEM_PROMPT = """You are a quantitative trading analyst reviewing performance data for AlphaBot,
an automated multi-market trading bot running paper trades on a $1M+ portfolio.

Your mandate from the portfolio manager (Garrath):
1. PROTECT CAPITAL FIRST. Never recommend changes that increase risk without strong evidence.
2. INCREASE PROFITS carefully and cautiously. Favour quality over quantity of trades.
3. You MAY recommend more trades per day or more exposure IF the data clearly supports it
   with sufficient sample size and consistent win rates above 55%.
4. NEVER recommend setting IS_LIVE=true. That decision belongs to Garrath alone.
5. Always cite sample size. Flag LOW confidence when n < 10. Flag MEDIUM when n < 30.
   Only assign HIGH confidence at n >= 30 with consistent results.
6. When in doubt, recommend MONITOR rather than a parameter change.
7. Be specific and evidence-based. Vague recommendations are useless.
8. Consider the paper trading context — early data may not be statistically robust.

EXPECTED VALUE ANALYSIS — THIS IS YOUR PRIMARY LENS:
You will receive expected_value_by_discipline data. EV = (win_rate * avg_win) - (loss_rate * avg_loss).
This is the single most important metric per discipline. Use it as follows:
- EV > $5 and n >= 10: strong evidence of edge — consider loosening constraints for this discipline
- EV $0-$5: marginal edge — monitor, don't change yet
- EV negative and n >= 10: evidence against edge — recommend raising MIN_SIGNAL_SCORE for this discipline
- Reward/risk ratio < 1.0 means avg loss > avg win — this is structurally bad even at high win rates
- ALWAYS analyse each discipline separately. Never make global recommendations when the issue is per-discipline.

DISCIPLINE-SPECIFIC RECOMMENDATIONS:
You can recommend different MIN_SIGNAL_SCORE per discipline using the "discipline" field.
If crypto_intraday has negative EV but stock_swing has positive EV, recommend raising
crypto_intraday threshold only — not a global change.

Parameter ranges you are allowed to recommend:
  MIN_SIGNAL_SCORE: 4.0 to 9.0 (never below 4, never above 9)
  MAX_POSITIONS: 1 to 5 per discipline
  MAX_TOTAL_POSITIONS: 5 to 20
  MAX_TRADES_PER_DAY: 10 to 100
  STOP_LOSS_PCT: 2.0 to 8.0
  TRAILING_STOP_PCT: 1.0 to 5.0
  TAKE_PROFIT_PCT: 5.0 to 20.0
  MAX_TRADE_PCT: 2.0 to 10.0 (% of portfolio per trade)
  MAX_SECTOR_POSITIONS: 1 to 4
  CYCLE_SECONDS: 30 to 300

Respond ONLY with valid JSON in exactly this structure — no preamble, no markdown fences:
{
  "narrative": "2-3 paragraph plain English summary of overall performance and key insights, leading with the EV picture per discipline",
  "recommendations": [
    {
      "category": "THRESHOLD|POSITION_LIMITS|STOP_LOSS|REGIME_GATE|WATCHLIST|OBSERVATION",
      "action": "RAISE|LOWER|ADD|REMOVE|MONITOR|NONE",
      "parameter": "exact_config_key or null",
      "discipline": "all|stock_swing|crypto_swing|stock_intraday|crypto_intraday",
      "current_value": current_numeric_value_or_null,
      "recommended_value": recommended_numeric_value_or_null,
      "evidence": "specific EV, win rate, avg win/loss, sample size that support this",
      "confidence": "HIGH|MEDIUM|LOW",
      "sample_size": integer_or_null
    }
  ]
}

OBSERVATION category recs have action=NONE and no parameter — they are insights only.
Include 1-2 OBSERVATION recs per run for things worth watching but not yet actionable.
Limit total recommendations to 6 maximum. Quality over quantity."""


def _assemble_payload():
    """Pull all analytics data from DB and assemble into a structured context dict."""
    try:
        # Core performance
        from data.database import _db_all_time_stats
        # Import inline to avoid circular
        import sqlite3
        from core.config import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        total_t, total_pnl, wins, losses, avg_sc = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END), "
            "COALESCE(AVG(score),0) FROM trades WHERE side='SELL'"
        ).fetchone()
        win_rate = round(wins / total_t * 100, 1) if total_t else 0

        # Near-miss summary
        nm_total = conn.execute("SELECT COUNT(*) FROM near_misses").fetchone()[0] or 0
        nm_score = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason='SCORE'").fetchone()[0] or 0
        nm_cap   = conn.execute("SELECT COUNT(*) FROM near_misses WHERE skip_reason!='SCORE'").fetchone()[0] or 0
        conn.close()

        missed_usd, missed_count, missed_winners = db_missed_profit_total()
        missed_by_disc = db_missed_profit_summary()
        cap_skips      = db_capacity_skips(days=14)
        thresh_data    = db_threshold_sensitivity()
        edge_data      = db_edge_by_discipline_and_score()
        regime_data    = db_performance_by_regime()
        gate_data      = db_entry_gate_attribution()
        rot_data       = db_rotation_summary(days=14)
        exit_data      = db_exit_category_breakdown(days=14)
        skip_reasons   = db_get_skip_reason_breakdown()
        leaderboard    = db_get_leaderboard(limit=10)
        ev_data        = db_ev_by_discipline()
        config_history = db_get_config_history_for_intelligence(days=30)

        return {
            "overview": {
                "total_trades": int(total_t or 0),
                "total_pnl_usd": round(float(total_pnl or 0), 2),
                "win_rate_pct": win_rate,
                "avg_signal_score": round(float(avg_sc or 0), 2),
                "current_min_signal_score": float(cfg.MIN_SIGNAL_SCORE),
                "current_max_positions": int(cfg.MAX_POSITIONS),
                "current_max_total_positions": int(cfg.MAX_TOTAL_POSITIONS),
                "current_stop_loss_pct": float(cfg.STOP_LOSS_PCT),
                "current_take_profit_pct": float(cfg.TAKE_PROFIT_PCT),
                "current_max_trades_per_day": int(cfg.MAX_TRADES_PER_DAY),
                "is_live": bool(cfg.IS_LIVE),
                "paper_trading": not bool(cfg.IS_LIVE),
            },
            "config_changes_last_30d": [
                {
                    "parameter": r["parameter"],
                    "old_value": r["old_value"],
                    "new_value": r["new_value"],
                    "changed_by": r["changed_by"],
                    "date": (r["created_at"] or "")[:16],
                }
                for r in (config_history or [])
            ],
            "expected_value_by_discipline": [
                {
                    "discipline": r[0],
                    "trades": r[1],
                    "wins": r[2],
                    "losses": r[3],
                    "win_rate_pct": r[4],
                    "avg_win_usd": r[5],
                    "avg_loss_usd": r[6],
                    "ev_per_trade_usd": r[7],
                    "total_pnl_usd": r[8],
                    "avg_score": r[9],
                    "avg_hold_hours": r[10],
                    "reward_risk_ratio": round(r[5] / r[6], 2) if r[6] and r[6] > 0 else None,
                    "has_edge": r[7] > 0 and r[1] >= 10,
                }
                for r in (ev_data or [])
            ],
            "near_misses": {
                "total": int(nm_total),
                "score_based": int(nm_score),
                "capacity_blocked": int(nm_cap),
                "simulated_missed_profit_usd": round(float(missed_usd or 0), 2),
                "simulated_count": int(missed_count),
                "by_discipline": [
                    {"discipline": r[0], "count": r[1], "sim_pnl_usd": r[2],
                     "avg_pct": r[3], "would_win": r[4]}
                    for r in (missed_by_disc or [])
                ],
            },
            "capacity_skips": [
                {"reason": r[0], "count": r[1], "avg_score": r[2]}
                for r in (cap_skips or [])
            ],
            "threshold_sensitivity": [
                {"score_bucket": r[0], "count": r[1], "avg_pct_move": r[2], "winners": r[3]}
                for r in (thresh_data or [])
            ],
            "edge_by_discipline_score": [
                {"discipline": r[0], "score_bucket": r[1], "count": r[2],
                 "wins": r[3], "losses": r[4], "total_pnl": r[5]}
                for r in (edge_data or [])
            ],
            "regime_performance": [
                {"regime": r[0], "count": r[1], "wins": r[2],
                 "losses": r[3], "total_pnl": r[4], "avg_pnl": r[5]}
                for r in (regime_data or [])
            ],
            "exit_categories": [
                {"category": r[0], "count": r[1], "wins": r[2],
                 "total_pnl": r[3], "avg_pnl": r[4]}
                for r in (exit_data or [])
            ],
            "rotation_audit": [
                {"type": r[0], "verdict": r[1], "count": r[2],
                 "avg_sold_pct": r[3], "avg_bought_pct": r[4]}
                for r in (rot_data or [])
            ],
            "skip_reasons_all": [
                {"reason": r[0], "count": r[1], "avg_score": r[2]}
                for r in (skip_reasons or [])
            ],
            "leaderboard_top10": [
                {"symbol": r[0], "trades": r[1], "wins": r[2],
                 "losses": r[3], "total_pnl": r[4], "avg_score": r[7]}
                for r in (leaderboard or [])
            ],
            "entry_gate_attribution": {
                gate: [
                    {"value": str(r[0]), "count": r[1], "wins": r[2], "total_pnl": r[3]}
                    for r in rows
                ]
                for gate, rows in (gate_data or {}).items()
            },
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        log.error(f"[INTELLIGENCE] Payload assembly failed: {e}")
        return {}


def run_intelligence_analysis(triggered_by="scheduled"):
    """
    Full intelligence run:
    1. Assemble analytics payload
    2. Call Claude API with mandate system prompt
    3. Parse JSON response
    4. Store run + recommendations in DB
    Returns (run_id, rec_count, narrative) or (None, 0, error_message)
    """
    if not CLAUDE_API_KEY:
        log.warning("[INTELLIGENCE] No CLAUDE_API_KEY — skipping intelligence run")
        return None, 0, "No API key configured"

    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    log.info(f"[INTELLIGENCE] Starting run {run_id} (triggered_by={triggered_by})")

    # Assemble data
    payload = _assemble_payload()
    if not payload:
        return None, 0, "Failed to assemble analytics payload"

    # Build user message
    user_msg = (
        f"Here is AlphaBot's performance data as of {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"This is a paper trading account. Review the data and provide your recommendations.\n\n"
        f"DATA:\n{json.dumps(payload, indent=2)}"
    )

    # Call Claude
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 2000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=60,
        )
        if not resp.ok:
            log.error(f"[INTELLIGENCE] API error {resp.status_code}: {resp.text[:200]}")
            return None, 0, f"Claude API error: {resp.status_code}"

        raw_text = resp.json()["content"][0]["text"].strip()

        # Strip markdown fences if Claude adds them anyway
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        parsed = json.loads(raw_text)

    except json.JSONDecodeError as e:
        log.error(f"[INTELLIGENCE] JSON parse failed: {e} | raw: {raw_text[:300]}")
        return None, 0, f"JSON parse error: {e}"
    except Exception as e:
        log.error(f"[INTELLIGENCE] Claude call failed: {e}")
        return None, 0, f"Claude call failed: {e}"

    narrative      = parsed.get("narrative", "No narrative provided.")
    recommendations = parsed.get("recommendations", [])
    rec_count_raw  = len(recommendations)  # before validation

    # Validate + sanitise each rec
    valid_categories = {"THRESHOLD","POSITION_LIMITS","STOP_LOSS","REGIME_GATE","WATCHLIST","OBSERVATION"}
    valid_actions    = {"RAISE","LOWER","ADD","REMOVE","MONITOR","NONE"}
    valid_confidence = {"HIGH","MEDIUM","LOW"}
    clean_recs = []
    for r in recommendations[:6]:  # hard cap at 6
        if r.get("category") not in valid_categories: continue
        if r.get("action") not in valid_actions: continue
        if r.get("confidence") not in valid_confidence:
            r["confidence"] = "LOW"
        clean_recs.append(r)

    # Store in DB
    rec_count = db_save_recommendations(run_id, clean_recs)
    db_save_intelligence_run(
        run_id=run_id,
        narrative=narrative,
        raw_payload=json.dumps(payload),
        rec_count=rec_count,
        rec_count_raw=rec_count_raw,
        triggered_by=triggered_by,
    )

    log.info(f"[INTELLIGENCE] Run {run_id} complete — {rec_count} recommendations stored")
    return run_id, rec_count, narrative
