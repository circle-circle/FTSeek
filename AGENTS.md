# AGENTS.md

This file provides guidance for AI agents (Hermes, Claude, OpenAI, Cursor, etc.) working with this repository.

## ⚠️ 必读 (Start Here)

Before any work, read `SYSTEM_OBJECTIVE.md` in the project root. It defines:
- Current phase and North Star metrics
- Top 3 focus areas (out-of-scope work requires approval)
- Agent autonomy levels (🟢🟡🔴🚫)
- Decision rules (5-question test)
- Anti-metrics and historical lessons

`SYSTEM_OBJECTIVE.md` is the project constitution. All agent work must operate within its framework.

---

## Quick Reference

```
Phase:        Shadow validation (模拟盘验证期)
North Star:   Win rate ≥ 50% + Daily PnL ≥ $500
Current:      Win rate 31.3% 🔴 / Daily avg $362 🟡
Scope:        F1 (trading rules) / F2 (stability) / F3 (architecture)
Decision test: Which focus? What metric? How to verify? Worst case? Rollback?
Forbidden:    Real trading, delete DB, delete credentials, force push, enable disabled strategies
```

## Project Structure

```
app/                # Main application code
config/             # unified_config.yaml (SSOT for config)
data/               # SQLite databases (ftseek_us.db is primary)
logs/               # Application logs
scripts/            # Analysis and utility scripts
tests/              # Test suite
docs/               # Documentation
```

## Commands

```bash
systemctl start ftseek-us      # Start service
systemctl status ftseek-us     # Check status
curl http://localhost:8899/health  # Health check
pytest tests/                  # Run tests
```

## Database

Primary DB: `data/ftseek_us.db`
Key tables: `trade_records`, `open_positions`, `shadow_trades`, `option_positions`

```sql
-- Quick performance check
SELECT COUNT(*) n, ROUND(SUM(realized_pnl),0) pnl,
  ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr
FROM trade_records
WHERE date(entry_time) >= date('now','-7 days') AND realized_pnl IS NOT NULL;
```

## Working Principles

1. Data > Intuition (SQL evidence, sample ≥ 30)
2. Simple > Complex (one-line fix > refactoring)
3. Stop bleeding > Optimize (is it losing money now?)
4. Code change ≠ Done (verify service health + logs)
