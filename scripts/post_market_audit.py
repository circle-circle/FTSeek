#!/usr/bin/env python3
"""
FTSeek-US 盘后归因辩论报告 (Post-Market Attribution Debate)
=============================================================
多Agent归因辩论式审计：每个数据维度作为独立"归因视角"，
交叉验证后形成辩论式结论推送。

归因视角:
  1. 策略归因   — 哪个策略在赚钱/亏钱
  2. 信号归因   — 信号强度 vs 实际PnL，校准偏差
  3. 退出归因   — 退出原因分布，哪类退出有效
  4. 环境归因   — 市场regime对胜率的影响
  5. 趋势归因   — 今日 vs 近5日均值
  6. 执行归因   — 系统健康，失败率，ghost/orphan
  7. 影子归因   — shadow策略对比
  8. 风控归因   — 回撤/资金/持仓

推送: 飞书(主) + 钉钉(备)

运行: python scripts/post_market_audit.py
"""

import sys
import os
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

# ─── 路径 ───
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

DB_PATH = BASE_DIR / "data" / "ftseek_us.db"
BATTLE_PLAN = BASE_DIR / "data" / "daily_battle_plan_us.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PostAudit] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "logs" / "audit" / "post_market_audit.log")
    ]
)
logger = logging.getLogger("post_market_audit")

US_EASTERN = pytz.timezone("America/New_York")

def us_eastern_now():
    return datetime.now(US_EASTERN)

def load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default or {}

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

# ════════════════════════════════════════════════════════════
# 归因视角 1: 策略归因
# ════════════════════════════════════════════════════════════
def attribution_strategy(db, today, window_days=7):
    """按策略维度拆分: 笔数/胜率/PnL/均持时间"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT strategy_id, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl,
               AVG(realized_pnl) as avg_pnl,
               AVG(holding_hours) as avg_hold
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL
        GROUP BY strategy_id ORDER BY total_pnl DESC
    """, (since,)).fetchall()

    today_rows = db.execute("""
        SELECT strategy_id, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl
        FROM trade_records
        WHERE trade_date = ? AND realized_pnl IS NOT NULL
        GROUP BY strategy_id ORDER BY total_pnl DESC
    """, (today,)).fetchall()
    today_map = {r['strategy_id']: dict(r) for r in today_rows}

    result = []
    for r in rows:
        sid = r['strategy_id'] or '(未标记)'
        wr = r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0
        t = today_map.get(r['strategy_id'], {})
        result.append({
            "strategy": sid,
            "cnt": r['cnt'], "wins": r['wins'], "win_rate": wr,
            "total_pnl": r['total_pnl'] or 0, "avg_pnl": r['avg_pnl'] or 0,
            "avg_hold": r['avg_hold'] or 0,
            "today_cnt": t.get('cnt', 0),
            "today_pnl": t.get('total_pnl', 0) or 0,
        })
    return result


# ════════════════════════════════════════════════════════════
# 归因视角 2: 信号归因
# ════════════════════════════════════════════════════════════
def attribution_signal(db, today, window_days=30):
    """信号强度 vs PnL 校准: 高强度信号是否真的更赚钱"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT
            CASE
                WHEN signal_strength >= 90 THEN '90+极高'
                WHEN signal_strength >= 70 THEN '70-89高'
                WHEN signal_strength >= 50 THEN '50-69中'
                WHEN signal_strength >= 30 THEN '30-49低'
                WHEN signal_strength IS NOT NULL AND signal_strength > 0 THEN '<30极低'
                WHEN signal_strength = 0 THEN '0(无信号)'
                ELSE 'NULL'
            END as strength_bucket,
            signal_strength,
            COUNT(*) as cnt,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL
        GROUP BY strength_bucket
        ORDER BY COALESCE(signal_strength, -1) DESC
    """, (since,)).fetchall()

    return [{
        "bucket": r['strength_bucket'],
        "cnt": r['cnt'], "wins": r['wins'],
        "win_rate": r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0,
        "total_pnl": r['total_pnl'] or 0,
        "avg_pnl": r['avg_pnl'] or 0,
    } for r in rows]


# ════════════════════════════════════════════════════════════
# 归因视角 3: 退出归因
# ════════════════════════════════════════════════════════════
def attribution_exit(db, today, window_days=7):
    """退出原因分布: 哪类退出方式实际效果好"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT exit_reason, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl,
               AVG(realized_pnl) as avg_pnl,
               AVG(holding_hours) as avg_hold
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_reason IS NOT NULL
        GROUP BY exit_reason ORDER BY total_pnl DESC
    """, (since,)).fetchall()
    return [{
        "reason": r['exit_reason'],
        "cnt": r['cnt'], "wins": r['wins'],
        "win_rate": r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0,
        "total_pnl": r['total_pnl'] or 0,
        "avg_pnl": r['avg_pnl'] or 0,
        "avg_hold": r['avg_hold'] or 0,
    } for r in rows]


# ════════════════════════════════════════════════════════════
# 归因视角 4: 环境归因 (市场regime)
# ════════════════════════════════════════════════════════════
def attribution_regime(db, today, window_days=7):
    """市场regime vs 胜率/PnL: 哪些regime下策略有效"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT market_regime, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl,
               AVG(realized_pnl) as avg_pnl
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL
        GROUP BY market_regime ORDER BY cnt DESC
    """, (since,)).fetchall()

    # signal_outcomes 补充 regime 分析
    signal_rows = db.execute("""
        SELECT market_regime, COUNT(*) as cnt,
               AVG(pnl_pct) as avg_pnl_pct,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins
        FROM signal_outcomes
        WHERE timestamp >= ?
        GROUP BY market_regime ORDER BY cnt DESC
    """, (since,)).fetchall()
    signal_map = {r['market_regime']: dict(r) for r in signal_rows}

    return [{
        "regime": r['market_regime'] or '(未知)',
        "cnt": r['cnt'], "wins": r['wins'],
        "win_rate": r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0,
        "total_pnl": r['total_pnl'] or 0,
        "signal_cnt": signal_map.get(r['market_regime'], {}).get('cnt', 0),
        "signal_wr": (signal_map.get(r['market_regime'], {}).get('wins', 0) /
                      max(signal_map.get(r['market_regime'], {}).get('cnt', 1), 1) * 100),
    } for r in rows]


# ════════════════════════════════════════════════════════════
# 归因视角 5: 趋势归因 (今日 vs 近5日)
# ════════════════════════════════════════════════════════════
def attribution_trend(db, today):
    """今日 vs 近5日均值: 是改善还是恶化"""
    since_5d = (us_eastern_now() - timedelta(days=7)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT trade_date, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl,
               AVG(realized_pnl) as avg_pnl,
               MAX(realized_pnl) as best,
               MIN(realized_pnl) as worst
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL
        GROUP BY trade_date ORDER BY trade_date DESC
    """, (since_5d,)).fetchall()

    daily = [{
        "date": r['trade_date'], "cnt": r['cnt'],
        "wins": r['wins'],
        "win_rate": r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0,
        "total_pnl": r['total_pnl'] or 0,
        "avg_pnl": r['avg_pnl'] or 0,
        "best": r['best'] or 0, "worst": r['worst'] or 0,
    } for r in rows]

    # 5日均值(排除今日)
    recent = [d for d in daily if d['date'] != today]
    if recent:
        avg5 = {
            "trades": sum(d['cnt'] for d in recent) / len(recent),
            "win_rate": sum(d['win_rate'] for d in recent) / len(recent),
            "pnl": sum(d['total_pnl'] for d in recent) / len(recent),
        }
    else:
        avg5 = None

    today_data = next((d for d in daily if d['date'] == today), None)
    return {"daily": daily, "avg5": avg5, "today": today_data}


# ════════════════════════════════════════════════════════════
# 归因视角 6: 执行归因 (系统健康)
# ════════════════════════════════════════════════════════════
def attribution_execution(db, today, window_days=7):
    """系统健康: 失败率/ghost/orphan/failures"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")

    # failures
    failures = db.execute("""
        SELECT failure_type, COUNT(*) as cnt
        FROM failures WHERE timestamp >= ?
        GROUP BY failure_type ORDER BY cnt DESC
    """, (since,)).fetchall()

    # ghost/orphan 占比
    total_trades = db.execute("""
        SELECT COUNT(*) FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL
    """, (since,)).fetchone()[0]

    ghost_orphan = db.execute("""
        SELECT COUNT(*) FROM trade_records
        WHERE trade_date >= ? AND exit_reason IN
            ('ghost_cleanup_cruise', 'ghost_cleanup', 'orphan_cleaned',
             'stale_scalping_cleanup', 'sync_ghost_cleaned')
    """, (since,)).fetchone()[0]

    # 方向分布
    direction = db.execute("""
        SELECT direction, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL
        GROUP BY direction
    """, (since,)).fetchall()

    return {
        "failures": [{"type": r['failure_type'], "cnt": r['cnt']} for r in failures],
        "total_trades": total_trades,
        "ghost_orphan": ghost_orphan,
        "ghost_orphan_pct": ghost_orphan / total_trades * 100 if total_trades > 0 else 0,
        "direction": [{
            "dir": r['direction'], "cnt": r['cnt'],
            "wins": r['wins'],
            "win_rate": r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0,
            "total_pnl": r['total_pnl'] or 0,
        } for r in direction],
    }


# ════════════════════════════════════════════════════════════
# 归因视角 7: 影子归因
# ════════════════════════════════════════════════════════════
def attribution_shadow(db, today, window_days=7):
    """shadow策略对比: baseline vs 变种"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT strategy, COUNT(*) as cnt,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(realized_pnl) as total_pnl,
               AVG(pnl_pct) as avg_pnl_pct,
               AVG(holding_minutes) as avg_hold
        FROM shadow_trades
        WHERE DATE(entry_time) >= ? AND closed = 1
        GROUP BY strategy ORDER BY total_pnl DESC
    """, (since,)).fetchall()
    return [{
        "strategy": r['strategy'],
        "cnt": r['cnt'], "wins": r['wins'],
        "win_rate": r['wins'] / r['cnt'] * 100 if r['cnt'] > 0 else 0,
        "total_pnl": r['total_pnl'] or 0,
        "avg_pnl_pct": r['avg_pnl_pct'] or 0,
        "avg_hold": r['avg_hold'] or 0,
    } for r in rows]


# ════════════════════════════════════════════════════════════
# 归因视角 8: 风控归因
# ════════════════════════════════════════════════════════════
def attribution_risk(db, today):
    """风控: 回撤/资金/持仓/scalping预算"""
    # 资金曲线
    capital = db.execute("""
        SELECT current_equity, peak_equity, cash, market_value,
               drawdown_pct, max_drawdown_pct, water_level, trade_env
        FROM capital_state ORDER BY id DESC LIMIT 1
    """).fetchone()

    # 资金趋势 (每日首条)
    capital_trend = db.execute("""
        SELECT substr(last_update, 1, 10) as d,
               MIN(current_equity) as equity,
               MIN(drawdown_pct) as dd
        FROM capital_state
        WHERE last_update >= date('now', '-7 days')
        GROUP BY d ORDER BY d DESC LIMIT 7
    """).fetchall()

    # 持仓
    positions = db.execute("""
        SELECT symbol, direction, size, avg_entry_price, current_sl,
               initial_tp, strategy_id, mode, entry_time
        FROM open_positions
    """).fetchall()

    # scalping 预算
    scalping = db.execute("SELECT * FROM scalping_budget_state").fetchone()

    # 最大连续亏损
    recent_trades = db.execute("""
        SELECT realized_pnl FROM trade_records
        WHERE trade_date >= date(?, '-7 days') AND realized_pnl IS NOT NULL
        ORDER BY entry_time
    """, (today,)).fetchall()

    max_consec_loss = 0
    current_streak = 0
    for t in recent_trades:
        if t['realized_pnl'] and t['realized_pnl'] < 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0

    return {
        "capital": dict(capital) if capital else None,
        "capital_trend": [{"date": r['d'], "equity": r['equity'], "dd": r['dd']}
                          for r in capital_trend],
        "positions": [dict(p) for p in positions],
        "scalping": dict(scalping) if scalping else None,
        "max_consec_loss": max_consec_loss,
    }


# ════════════════════════════════════════════════════════════
# 作战计划归因
# ════════════════════════════════════════════════════════════
def attribution_plan(db, today, plan):
    """计划 vs 执行: 命中/错失/计划外"""
    plan_targets = {t.get("symbol"): t for t in plan.get("targets", [])}
    traded = db.execute("""
        SELECT DISTINCT symbol FROM trade_records WHERE trade_date = ?
    """, (today,)).fetchall()
    traded_set = {r['symbol'] for r in traded}

    missed = []
    for sym, target in plan_targets.items():
        if sym not in traded_set:
            missed.append({
                "symbol": sym,
                "direction": target.get("direction", "?"),
                "urgency": target.get("urgency", "?"),
                "rationale": (target.get("rationale", "") or "")[:60],
            })

    unplanned = [sym for sym in traded_set if sym not in plan_targets]

    return {
        "plan_count": len(plan_targets),
        "executed": len(traded_set & set(plan_targets.keys())),
        "missed": missed,
        "unplanned": unplanned,
        "confidence": plan.get("final_confidence", 0),
        "risk_warnings": plan.get("final_risk_warnings", []),
        "debate_summary": plan.get("debate_summary", ""),
    }


# ════════════════════════════════════════════════════════════
# 经验教训
# ════════════════════════════════════════════════════════════
def attribution_lessons(db):
    """活跃lessons: 按impact排序"""
    rows = db.execute("""
        SELECT lesson_category, lesson_text, impact_score
        FROM lessons WHERE status = 'active'
        ORDER BY impact_score DESC LIMIT 5
    """).fetchall()
    return [{
        "category": r['lesson_category'],
        "text": r['lesson_text'][:80],
        "impact": r['impact_score'] or 0,
    } for r in rows]


# ════════════════════════════════════════════════════════════
# 辩论合成: 交叉验证 → 结论
# ════════════════════════════════════════════════════════════
def synthesize_debate(strategy, signal, exit_attr, regime, trend,
                      execution, shadow, risk, plan_attr, lessons):
    """
    交叉验证多个归因视角，生成辩论式结论:
    - 共识 (多视角一致)
    - 分歧 (视角间矛盾)
    - 行动项 (基于数据的具体建议)
    """
    consensus = []
    disputes = []
    actions = []

    # ── 共识检测 ──

    # 1. 策略归因 + 信号归因: 信号强度是否影响策略表现?
    high_sig = next((s for s in signal if '90' in s['bucket']), None)
    low_sig = next((s for s in signal if '50-69' in s['bucket']), None)
    if high_sig and high_sig['win_rate'] < 15 and high_sig['cnt'] > 10:
        consensus.append(
            f"🔴 信号强度校准失效: strength≥90 信号 {high_sig['cnt']}笔 "
            f"胜率仅{high_sig['win_rate']:.0f}%，不赚反亏(${high_sig['total_pnl']:,.0f})"
        )
        actions.append("重新校准信号强度权重，或提高 strength≥90 的额外过滤条件")

    # 2. 退出归因: sl_hit 胜率 vs trailing_stop
    sl = next((e for e in exit_attr if e['reason'] == 'sl_hit'), None)
    trail = next((e for e in exit_attr if e['reason'] == 'TRAILING_STOP'), None)
    if sl and trail:
        if sl['win_rate'] < 20 and trail['win_rate'] > 30:
            consensus.append(
                f"📊 退出策略分化: 止损({sl['cnt']}笔 胜率{sl['win_rate']:.0f}% "
                f"亏${sl['total_pnl']:,.0f}) vs "
                f"追踪止盈({trail['cnt']}笔 胜率{trail['win_rate']:.0f}% "
                f"赚${trail['total_pnl']:+,.0f})"
            )
            actions.append(f"止损触发率{sl['win_rate']:.0f}%偏低，检查SL设置是否过紧或入场点位偏差")

    # 3. ghost/orphan 占比
    if execution['ghost_orphan_pct'] > 30:
        consensus.append(
            f"⚠️ 系统噪声严重: {execution['ghost_orphan_pct']:.0f}% 交易为ghost/orphan清理 "
            f"({execution['ghost_orphan']}/{execution['total_trades']}笔)，不产生实际盈亏"
        )
        actions.append("审计开仓→平仓链路完整性，减少ghost仓位产生")

    # 4. 趋势归因: 近5日PnL方向
    if trend['avg5']:
        avg_pnl = trend['avg5']['pnl']
        avg_wr = trend['avg5']['win_rate']
        if avg_pnl < -500:
            consensus.append(
                f"📉 近5日持续亏损: 日均PnL ${avg_pnl:,.0f}，"
                f"日均胜率{avg_wr:.0f}%，需评估是否暂停交易收集更多数据"
            )
        elif avg_pnl > 200:
            consensus.append(f"📈 近5日盈利趋势: 日均PnL ${avg_pnl:+,.0f}，胜率{avg_wr:.0f}%")

    # ── 分歧检测 ──

    # 1. 方向分歧: LONG vs SHORT
    long_d = next((d for d in execution['direction'] if d['dir'] == 'LONG'), None)
    short_d = next((d for d in execution['direction'] if d['dir'] == 'SHORT'), None)
    if long_d and short_d:
        if abs(long_d['total_pnl'] - short_d['total_pnl']) > 500:
            better = "SHORT" if short_d['total_pnl'] > long_d['total_pnl'] else "LONG"
            worse = "LONG" if better == "SHORT" else "SHORT"
            b = short_d if better == "SHORT" else long_d
            w = long_d if better == "SHORT" else short_d
            disputes.append(
                f"方向偏好分歧: {better}({b['cnt']}笔 胜率{b['win_rate']:.0f}% "
                f"${b['total_pnl']:+,.0f}) 显著优于 "
                f"{worse}({w['cnt']}笔 胜率{w['win_rate']:.0f}% "
                f"${w['total_pnl']:+,.0f})"
            )

    # 2. 信号归因 vs 策略归因: voting_llm 使用高信号但仍亏
    voting = next((s for s in strategy if 'voting_llm' in s['strategy']), None)
    if voting and voting['total_pnl'] < -200:
        disputes.append(
            f"策略-信号矛盾: voting_llm策略 近7日亏${voting['total_pnl']:,.0f} "
            f"({voting['cnt']}笔 胜率{voting['win_rate']:.0f}%)，"
            f"但其使用高信号强度入场——说明LLM投票逻辑可能需要审查"
        )

    # 3. 计划 vs 执行
    if plan_attr['plan_count'] > 0 and plan_attr['executed'] == 0:
        disputes.append(
            f"计划执行脱节: 作战计划{plan_attr['plan_count']}个标的，"
            f"实际执行0个，信心度{plan_attr['confidence']}"
        )

    # ── 影子对比 ──
    shadow_insight = None
    if shadow:
        baseline = next((s for s in shadow if s['strategy'] == 'baseline'), None)
        if baseline and baseline['cnt'] > 0:
            shadow_insight = (
                f"Shadow baseline: {baseline['cnt']}笔 胜率{baseline['win_rate']:.0f}% "
                f"PnL=${baseline['total_pnl']:+,.0f}"
            )
            # 找最优变种
            best = max(shadow, key=lambda s: s['total_pnl'])
            if best['strategy'] != 'baseline' and best['total_pnl'] > baseline['total_pnl']:
                shadow_insight += (
                    f"\n  最优变种 {best['strategy']}: "
                    f"{best['cnt']}笔 胜率{best['win_rate']:.0f}% "
                    f"PnL=${best['total_pnl']:+,.0f}"
                )

    return {
        "consensus": consensus,
        "disputes": disputes,
        "actions": actions,
        "shadow_insight": shadow_insight,
    }


# ════════════════════════════════════════════════════════════
# 报告构建
# ════════════════════════════════════════════════════════════
def build_report(today, strategy, signal, exit_attr, regime, trend,
                 execution, shadow, risk, plan_attr, lessons, debate):
    L = []
    L.append(f"### 📊 FTSeek-US 盘后归因辩论报告")
    L.append(f"**{today}** (美东) | equity=${risk['capital']['current_equity']:,.0f}" if risk['capital'] else f"**{today}**")
    L.append("")

    # ─── 辩论结论 (最重要，放最前面) ───
    if debate['consensus'] or debate['disputes']:
        L.append("## 🔥 辩论结论")
        if debate['consensus']:
            L.append("**共识**:")
            for c in debate['consensus']:
                L.append(f"  {c}")
        if debate['disputes']:
            L.append("**分歧**:")
            for d in debate['disputes']:
                L.append(f"  {d}")
        L.append("")

    if debate['actions']:
        L.append("**🎯 行动项**:")
        for i, a in enumerate(debate['actions'], 1):
            L.append(f"  {i}. {a}")
        L.append("")

    # ─── 趋势归因 ───
    L.append("---")
    L.append("## 📈 趋势归因 (近5日)")
    for d in trend['daily'][:5]:
        marker = " ◀" if d['date'] == today else ""
        pnl_str = f"${d['total_pnl']:+,.0f}"
        L.append(
            f"  {d['date']}: {d['cnt']}笔 胜率{d['win_rate']:.0f}% "
            f"PnL={pnl_str} "
            f"best=${d['best']:+,.0f} worst=${d['worst']:+,.0f}{marker}"
        )
    if trend['avg5']:
        a = trend['avg5']
        L.append(f"  **5日均值**: {a['trades']:.1f}笔/日 胜率{a['win_rate']:.0f}% 日均PnL=${a['pnl']:+,.0f}")
    L.append("")

    # ─── 策略归因 ───
    L.append("## 🤖 策略归因 (近7日)")
    for s in strategy:
        pnl_marker = "🟢" if s['total_pnl'] > 0 else ("🔴" if s['total_pnl'] < 0 else "⚪")
        today_str = f" | 今日: {s['today_cnt']}笔 ${s['today_pnl']:+,.0f}" if s['today_cnt'] > 0 else ""
        L.append(
            f"  {pnl_marker} **{s['strategy']}**: {s['cnt']}笔 "
            f"胜率{s['win_rate']:.0f}% PnL=${s['total_pnl']:+,.0f} "
            f"均持{s['avg_hold']:.1f}h{today_str}"
        )
    L.append("")

    # ─── 信号归因 ───
    L.append("## 📡 信号归因 (近30日)")
    for s in signal:
        pnl_marker = "🟢" if s['total_pnl'] > 0 else ("🔴" if s['total_pnl'] < 0 else "⚪")
        L.append(
            f"  {pnl_marker} {s['bucket']}: {s['cnt']}笔 "
            f"胜率{s['win_rate']:.0f}% PnL=${s['total_pnl']:+,.0f} "
            f"avg=${s['avg_pnl']:+,.0f}"
        )
    L.append("")

    # ─── 退出归因 ───
    L.append("## 🚪 退出归因 (近7日)")
    for e in exit_attr[:8]:
        pnl_marker = "🟢" if e['total_pnl'] > 0 else ("🔴" if e['total_pnl'] < 0 else "⚪")
        L.append(
            f"  {pnl_marker} {e['reason']}: {e['cnt']}笔 "
            f"胜率{e['win_rate']:.0f}% PnL=${e['total_pnl']:+,.0f} "
            f"avg=${e['avg_pnl']:+,.0f}"
        )
    L.append("")

    # ─── 环境归因 ───
    L.append("## 🌡️ 环境归因 (近7日)")
    for r in regime:
        L.append(
            f"  {r['regime']}: {r['cnt']}笔 胜率{r['win_rate']:.0f}% "
            f"PnL=${r['total_pnl']:+,.0f} "
            f"(信号层面: {r['signal_cnt']}条 胜率{r['signal_wr']:.0f}%)"
        )
    L.append("")

    # ─── 方向归因 ───
    if execution['direction']:
        L.append("## 🧭 方向归因 (近7日)")
        for d in execution['direction']:
            L.append(
                f"  {d['dir']}: {d['cnt']}笔 胜率{d['win_rate']:.0f}% "
                f"PnL=${d['total_pnl']:+,.0f}"
            )
        L.append("")

    # ─── 执行健康 ───
    ghost_pct = execution['ghost_orphan_pct']
    fail_str = ", ".join(f"{f['type']}({f['cnt']})" for f in execution['failures']) if execution['failures'] else "无"
    L.append("## 🔧 执行健康")
    L.append(
        f"  ghost/orphan占比: {ghost_pct:.0f}% "
        f"({execution['ghost_orphan']}/{execution['total_trades']}笔)"
    )
    L.append(f"  failures: {fail_str}")
    L.append(f"  最大连续亏损(7日): {risk['max_consec_loss']}笔")
    L.append("")

    # ─── 风控 ───
    if risk['capital']:
        c = risk['capital']
        env_label = "模拟盘" if c['trade_env'] == 1 else "⚠️ 实盘"
        L.append(f"## 💰 风控状态 ({env_label})")
        L.append(f"  净值: ${c['current_equity']:,.0f} | 峰值: ${c['peak_equity']:,.0f}")
        L.append(f"  回撤: {c['drawdown_pct']:.2%} | 最大回撤: {c['max_drawdown_pct']:.2%} | 水位: {c['water_level']}")
        L.append(f"  现金: ${c['cash']:,.0f} | 市值: ${c['market_value']:,.0f}")
        if risk['scalping']:
            sc = risk['scalping']
            L.append(f"  scalping预算: 日损=${sc['daily_loss']:.0f} 余额=${sc['used_budget']:,.0f} 并发={sc['concurrent_positions']}")

        # 资金趋势
        if risk['capital_trend']:
            L.append("  资金趋势:")
            for ct in risk['capital_trend'][:5]:
                L.append(f"    {ct['date']}: ${ct['equity']:,.0f} (dd={ct['dd']:.2%})")
    L.append("")

    # ─── 持仓 ───
    if risk['positions']:
        L.append(f"## 📌 当前持仓 ({len(risk['positions'])}只)")
        for p in risk['positions']:
            mode = f" [{p['mode']}]" if p.get('mode') else ""
            L.append(
                f"  {p['symbol']} {p['direction']} {p['size']}份 "
                f"@${p['avg_entry_price']:.2f} SL=${p['current_sl']:.2f}{mode}"
            )
    else:
        L.append("## 📌 当前无持仓")
    L.append("")

    # ─── 影子归因 ───
    if debate['shadow_insight']:
        L.append("## 👻 影子策略归因")
        L.append(f"  {debate['shadow_insight']}")
        L.append("")

    # ─── 作战计划 ───
    if plan_attr['plan_count'] > 0 or plan_attr['unplanned']:
        L.append("## 📋 计划 vs 执行")
        L.append(
            f"  计划标的: {plan_attr['plan_count']} | "
            f"执行: {plan_attr['executed']} | "
            f"错失: {len(plan_attr['missed'])} | "
            f"计划外: {len(plan_attr['unplanned'])}"
        )
        L.append(f"  信心度: {plan_attr['confidence']}")
        if plan_attr['missed']:
            L.append("  错失:")
            for m in plan_attr['missed'][:3]:
                L.append(f"    {m['symbol']} ({m['direction']}) urgency={m['urgency']}")
        if plan_attr['unplanned']:
            L.append(f"  计划外: {', '.join(plan_attr['unplanned'][:5])}")
        if plan_attr.get('debate_summary'):
            L.append(f"  盘前辩论摘要: {str(plan_attr['debate_summary'])[:100]}")
        L.append("")

    # ─── 经验教训 ───
    if lessons:
        L.append("## 📚 Top Lessons")
        for ls in lessons[:3]:
            L.append(f"  [{ls['category']}] impact={ls['impact']:.0f}: {ls['text']}")
        L.append("")

    L.append(f"> 生成时间: {us_eastern_now().strftime('%Y-%m-%d %H:%M:%S ET')}")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════
# 推送
# ════════════════════════════════════════════════════════════
def notify(message: str):
    ok_feishu = False
    ok_dingtalk = False

    try:
        from app.notifier.feishu import FeishuNotifier
        feishu = FeishuNotifier()
        if feishu.webhook_url:
            ok_feishu = feishu.send_text(message)
            logger.info(f"[飞书] 推送{'成功' if ok_feishu else '失败'}")
        else:
            logger.warning("[飞书] FEISHU_WEBHOOK_URL_US 未配置")
    except Exception as e:
        logger.error(f"[飞书] 推送异常: {e}")

    try:
        from app.notifier.dingtalk import DingTalkNotifier
        dingtalk = DingTalkNotifier()
        if dingtalk.enabled:
            ok_dingtalk = dingtalk.send_text(message)
            logger.info(f"[钉钉] 推送{'成功' if ok_dingtalk else '失败'}")
        else:
            logger.warning("[钉钉] DINGTALK_WEBHOOK_URL_US 未配置")
    except Exception as e:
        logger.error(f"[钉钉] 推送异常: {e}")

    return ok_feishu, ok_dingtalk


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════
def run_audit():
    logger.info("🚀 开始美股盘后归因辩论报告...")

    today = us_eastern_now().strftime("%Y-%m-%d")
    plan = load_json(BATTLE_PLAN)
    db = get_db()

    try:
        # 并行收集 8 个归因视角
        logger.info("📊 收集归因数据...")
        strategy = attribution_strategy(db, today)
        signal = attribution_signal(db, today)
        exit_attr = attribution_exit(db, today)
        regime = attribution_regime(db, today)
        trend = attribution_trend(db, today)
        execution = attribution_execution(db, today)
        shadow = attribution_shadow(db, today)
        risk = attribution_risk(db, today)
        plan_attr = attribution_plan(db, today, plan)
        lessons = attribution_lessons(db)

        # 辩论合成
        logger.info("🔥 交叉验证归因视角...")
        debate = synthesize_debate(
            strategy, signal, exit_attr, regime, trend,
            execution, shadow, risk, plan_attr, lessons
        )

        # 构建报告
        report = build_report(
            today, strategy, signal, exit_attr, regime, trend,
            execution, shadow, risk, plan_attr, lessons, debate
        )

        # 推送
        ok_feishu, ok_dingtalk = notify(report)

        if ok_feishu or ok_dingtalk:
            logger.info(f"✅ 归因辩论报告已推送 (飞书:{'✓' if ok_feishu else '✗'} 钉钉:{'✓' if ok_dingtalk else '✗'})")
        else:
            logger.warning("⚠️ 推送失败，报告已保存到日志")
            print(report)

        # 同时保存完整报告到文件
        report_dir = BASE_DIR / "logs" / "audit"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"attribution_debate_{today}.md"
        report_path.write_text(report, encoding="utf-8")
        logger.info(f"📄 报告已保存: {report_path}")

        # ── 催化回看闭环 (RFC-Catalyst 2026-05-27) ──
        # 盘前催化预测 vs 实际涨跌 → 教训注入 → 下次盘前自动参考
        try:
            from app.catalyst.postmarket_review import run_postmarket_catalyst_review
            logger.info("🧪 开始催化回看...")
            review_result = run_postmarket_catalyst_review(scheduler=None)
            if review_result and not review_result.get("skipped"):
                logger.info(
                    "✅ 催化回看完成: %d/%d 命中 (%.0f%%), %d 条教训",
                    review_result.get("hits", 0),
                    review_result.get("total", 0),
                    review_result.get("accuracy", 0) * 100,
                    review_result.get("lessons_count", 0),
                )
            else:
                logger.info("🧪 催化回看: 无数据或已禁用")
        except Exception as e:
            logger.warning(f"🧪 催化回看失败 (不影响主报告): {e}")

    finally:
        db.close()


if __name__ == "__main__":
    run_audit()
