#!/usr/bin/env python3
"""OKR Status Report - 标准化输出，任何 agent 可调用"""
import sys, json
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

# ─── 0. 统一口径常量 ───
# OKR 目标是 Playbook 系统, trade_records 中只统计 Playbook 相关来源.
# PLAYBOOK_SHADOW 已废弃但历史数据仍计入, 以便与 playbook_instructions 口径对齐.
PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')

# ─── 1. Playbook Instructions ───
from app.playbook.db import PlaybookDB
db = PlaybookDB()
db.ensure_tables()

instructions = db.get_recent_instructions(limit=500)
total = len(instructions)
skip_count = sum(1 for i in instructions if i.get('direction') == 'SKIP')
non_skip = total - skip_count
with_entry = sum(1 for i in instructions if i.get('entry_price') and i['entry_price'] > 0)
triggered = sum(1 for i in instructions if i.get('triggered'))
executed = sum(1 for i in instructions if i.get('executed'))

# Outcomes (只统计已执行且产生终态 win/loss 的指令, 与 OKR 目标 "outcome in ('win','loss')" 对齐)
wins = sum(1 for i in instructions if i.get('outcome') == 'win')
losses = sum(1 for i in instructions if i.get('outcome') == 'loss')
holding = sum(1 for i in instructions if i.get('outcome') == 'holding')
missed = sum(1 for i in instructions if i.get('outcome') == 'missed')
skipped = sum(1 for i in instructions if i.get('outcome') == 'skipped')
has_outcome = wins + losses

win_rate = wins / has_outcome * 100 if has_outcome > 0 else 0
# OKR PnL: 只统计 executed 且 outcome 为 win/loss 的指令, 与 OKR 目标一致
total_pnl = sum((i.get('pnl') or 0) for i in instructions if i.get('executed') and i.get('outcome') in ('win', 'loss'))

# Weekly PnL: 按周聚合, 同样只统计 executed + win/loss
weekly_pnl = {}
for inst in instructions:
    if not (inst.get('executed') and inst.get('outcome') in ('win', 'loss')):
        continue
    d = inst.get('date')
    if not d:
        continue
    dt = datetime.strptime(d, '%Y-%m-%d')
    wk = dt.strftime('%Y-W%W')
    if wk not in weekly_pnl:
        weekly_pnl[wk] = {'pnl': 0, 'trades': 0, 'wins': 0, 'losses': 0}
    weekly_pnl[wk]['trades'] += 1
    p = inst.get('pnl', 0) or 0
    weekly_pnl[wk]['pnl'] += p
    if p > 0:
        weekly_pnl[wk]['wins'] += 1
    elif p < 0:
        weekly_pnl[wk]['losses'] += 1

# ─── 2. Trade Records (Playbook only) ───
from app.storage.db_pool import get_db
conn = get_db("ftseek_us").connection()

_source_filter = ", ".join(f"'{s}'" for s in PLAYBOOK_SOURCE_TYPES)
trade_stats = {}
try:
    cursor = conn.execute(f"""
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
               SUM(realized_pnl) as total_pnl
        FROM trade_records
        WHERE trade_date >= date('now', '-30 days')
          AND source_type IN ({_source_filter})
    """)
    row = cursor.fetchone()
    trade_stats = dict(row) if row else {}
except Exception as e:
    trade_stats = {"error": str(e)}

# By strategy (Playbook only)
by_strategy = []
try:
    cursor2 = conn.execute(f"""
        SELECT strategy_id,
               COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl
        FROM trade_records
        WHERE trade_date >= date('now', '-30 days')
          AND strategy_id IS NOT NULL
          AND source_type IN ({_source_filter})
        GROUP BY strategy_id
    """)
    by_strategy = [dict(r) for r in cursor2.fetchall()]
except:
    pass

# ─── 3. IC ───
# P1 (2026-06-24): 主源改 signal_snapshots (1375+ 实测数据), playbook_instructions.fwd_return 基本空 (1 行)
# 否则 KR4 IC 永远 null. signal_snapshots 无 t3 列, 用 t1/t5 (alpha 在 T+5, 见 docs/ic_report_2026-06-24.md)
ic_data = {}
try:
    from app.analysis.signal_ic_tracker import get_ic_summary
    _ic_t1 = get_ic_summary(min_signals=20, forward_col="fwd_ret_t1", days_back=60)
    _ic_t5 = get_ic_summary(min_signals=20, forward_col="fwd_ret_t5", days_back=60)
    ic_data = {
        "overall_ic_t1": _ic_t1.get("ic_mean") if _ic_t1.get("status") == "ok" else None,
        "sample_t1": _ic_t1.get("n_signals", 0),
        "overall_ic_t5": _ic_t5.get("ic_mean") if _ic_t5.get("status") == "ok" else None,
        "sample_t5": _ic_t5.get("n_signals", 0),
        "by_strategy_t5": _ic_t5.get("by_strategy", {}),
        "source": "signal_snapshots",
    }
    # 兼容旧字段 overall_ic_t3: signal_snapshots 无 t3, 用 t5 代 (alpha 在 T+5)
    ic_data["overall_ic_t3"] = ic_data.get("overall_ic_t5")
    ic_data["sample_t3"] = ic_data.get("sample_t5", 0)
except Exception as e:
    ic_data = {"error": str(e)}
    # fallback: 旧 playbook IC (基本空, 仅兜底)
    try:
        from app.playbook.ic_tracker import compute_playbook_ic
        ic_data.update(compute_playbook_ic(db, days=30))
    except Exception:
        pass

# ─── 4. Config ───
config_state = {}
try:
    from app.core.config_container import get_config
    cfg = get_config()
    config_state = {
        "enabled": cfg.get_dotted('playbook.enabled', False),
        "execute_mode": cfg.get_dotted('playbook.execute_mode', 'simulate'),
        "auto_execute": cfg.get_dotted('playbook.auto_execute', False),
    }
except:
    config_state = {"error": "config unavailable"}

# ─── 5. Factor Stats ───
factor_stats = {}
try:
    singles = db.get_factor_stats(level="single")
    if singles:
        factor_stats = {
            "positive": [f for f in singles if f.get('verdict') == 'positive'],
            "negative": [f for f in singles if f.get('verdict') == 'negative'],
            "observing": [f for f in singles if f.get('verdict') == 'observing'],
        }
except:
    pass

# ─── 6. OKR Scorecard ───
OKR_TARGETS = {
    "win_rate": 55,
    "weekly_pnl": 1000,
    "min_trades": 30,
    "ic_t3": 0.05,
    "max_drawdown": 5,
}

def calc_okr_score():
    """Calculate overall OKR progress (0-100)"""
    scores = {}
    # KR1: Win rate
    scores["kr1_win_rate"] = min(100, win_rate / OKR_TARGETS["win_rate"] * 100) if has_outcome >= 5 else 0
    # KR2: Weekly PnL — 取最近 2 周(而非 PnL 最高的 2 周), 与 OKR "连续 2 周" 目标对齐
    sorted_weeks = sorted(weekly_pnl.keys(), reverse=True)
    recent_weeks = [weekly_pnl[wk] for wk in sorted_weeks[:2]]
    avg_weekly = sum(w['pnl'] for w in recent_weeks) / len(recent_weeks) if recent_weeks else 0
    scores["kr2_weekly_pnl"] = max(0, min(100, avg_weekly / OKR_TARGETS["weekly_pnl"] * 100))
    # KR3: Sample size
    scores["kr3_sample"] = min(100, has_outcome / OKR_TARGETS["min_trades"] * 100)
    # KR4: IC
    ic_t3 = ic_data.get('overall_ic_t3')
    scores["kr4_ic"] = min(100, max(0, ic_t3 / OKR_TARGETS["ic_t3"] * 100)) if ic_t3 else 0
    # KR5: Trigger rate
    trigger_rate = triggered / non_skip * 100 if non_skip > 0 else 0
    scores["kr5_trigger"] = min(100, trigger_rate / 50 * 100)
    
    overall = sum(scores.values()) / len(scores)
    return overall, scores

overall_score, kr_scores = calc_okr_score()

# ─── OUTPUT ───
print("=" * 65)
print("  OKR STATUS REPORT — " + datetime.now().strftime('%Y-%m-%d %H:%M'))
print("=" * 65)

print(f"\n🎯 OVERALL PROGRESS: {overall_score:.0f}%")
for kr, score in kr_scores.items():
    bar = "█" * int(score/5) + "░" * (20 - int(score/5))
    print(f"  {kr:<20} {bar} {score:.0f}%")

print(f"\n📊 PIPELINE METRICS")
print(f"  Instructions:    {total} (SKIP: {skip_count}, active: {non_skip})")
print(f"  With entry_price: {with_entry}/{non_skip} ({with_entry/non_skip*100:.0f}%)" if non_skip > 0 else "  With entry_price: 0")
print(f"  Triggered:       {triggered}/{non_skip} ({triggered/non_skip*100:.1f}%)" if non_skip > 0 else "  Triggered: 0")
print(f"  Executed:        {executed}")
print(f"  Outcomes:        {has_outcome} (W:{wins} L:{losses} hold:{holding} miss:{missed})")

if has_outcome > 0:
    print(f"\n💰 PERFORMANCE")
    print(f"  Win Rate:  {win_rate:.1f}% (target: ≥55%)")
    print(f"  Total PnL: ${total_pnl:.2f}")
    if weekly_pnl:
        for wk in sorted(weekly_pnl.keys(), reverse=True)[:3]:
            s = weekly_pnl[wk]
            print(f"  {wk}: ${s['pnl']:.2f} ({s['wins']}W/{s['losses']}L)")

ic_t3 = ic_data.get('overall_ic_t3')
print(f"\n📈 IC METRICS")
print(f"  IC(T+1): {ic_data.get('overall_ic_t1', 'N/A')} (n={ic_data.get('sample_t1', 0)})")
print(f"  IC(T+3): {ic_t3 or 'N/A'} (n={ic_data.get('sample_t3', 0)})")
print(f"  IC(T+5): {ic_data.get('overall_ic_t5', 'N/A')} (n={ic_data.get('sample_t5', 0)})")

grad = ic_data.get('graduation_check', {})
print(f"  Graduation: {grad.get('verdict', 'UNKNOWN')}")

if factor_stats:
    print(f"\n🔬 FACTORS")
    for f in factor_stats.get('positive', []):
        print(f"  ✅ {f.get('factor_key')}: WR={f.get('win_rate',0)*100:.0f}% ({f.get('total_trades',0)} trades)")
    for f in factor_stats.get('negative', []):
        print(f"  ❌ {f.get('factor_key')}: WR={f.get('win_rate',0)*100:.0f}% ({f.get('total_trades',0)} trades)")

if trade_stats and 'error' not in trade_stats:
    print(f"\n📋 TRADE RECORDS (30d)")
    print(f"  Total: {trade_stats.get('cnt', 0)}")
    print(f"  Wins: {trade_stats.get('wins', 0)}, Losses: {trade_stats.get('losses', 0)}")
    print(f"  PnL: ${trade_stats.get('total_pnl', 0):.2f}")

if by_strategy:
    print(f"\n📋 BY STRATEGY (30d)")
    for s in sorted(by_strategy, key=lambda x: x.get('total_pnl', 0), reverse=True):
        wr = s['wins'] / s['cnt'] * 100 if s['cnt'] > 0 else 0
        print(f"  {s.get('strategy_id','?')}: {s['cnt']} trades, WR={wr:.0f}%, PnL=${s.get('total_pnl',0):.2f}")

print(f"\n⚙️ CONFIG")
for k, v in config_state.items():
    print(f"  {k}: {v}")

print("\n" + "=" * 65)

# Also output JSON for programmatic consumption
output = {
    "timestamp": datetime.now().isoformat(),
    "overall_score": round(overall_score, 1),
    "kr_scores": {k: round(v, 1) for k, v in kr_scores.items()},
    "pipeline": {
        "total": total, "skip": skip_count, "active": non_skip,
        "with_entry": with_entry, "triggered": triggered, "executed": executed,
        "wins": wins, "losses": losses, "holding": holding, "missed": missed,
        "win_rate": round(win_rate, 1), "total_pnl": round(total_pnl, 2),
    },
    "ic": {
        "t1": ic_data.get('overall_ic_t1'),
        "t3": ic_data.get('overall_ic_t3'),
        "t5": ic_data.get('overall_ic_t5'),
        "graduation": grad.get('verdict', 'UNKNOWN'),
    },
    "factors": factor_stats,
    "trade_records": trade_stats,
    "by_strategy": by_strategy,
    "config": config_state,
}

data_dir = Path('/opt/ftseek/FTSeek-US/data')
data_dir.mkdir(exist_ok=True)

# Save latest
with open(data_dir / 'okr_latest.json', 'w') as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

# Append to history (one JSON per line)
with open(data_dir / 'okr_history.jsonl', 'a') as f:
    f.write(json.dumps(output, ensure_ascii=False) + '\n')

print("JSON saved to data/okr_latest.json + data/okr_history.jsonl")
