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
# 归因视角 0: 费用净额总览 (2026-07-30, DEB-fee-net-pnl G5)
# ════════════════════════════════════════════════════════════
def attribution_fee_net(db, today, window_days=7):
    """毛/费/净三列 — 净利才是真实盈利 (最低佣金$1.99×双边 → 每单固定成本≈$4-5,
    毛利<$5皆白干)。假盈利单 = 毛>0 但净<=0。"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")

    def _q(where_date, param):
        return db.execute(f"""
            SELECT COUNT(*) as cnt,
                   SUM(realized_pnl) as gross,
                   SUM(COALESCE(fees_est, 0)) as fees,
                   SUM(COALESCE(pnl_net, realized_pnl)) as net,
                   SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as net_wins,
                   SUM(CASE WHEN realized_pnl > 0 AND COALESCE(pnl_net, realized_pnl) <= 0
                       THEN 1 ELSE 0 END) as fake_wins,
                   SUM(CASE WHEN fees_est IS NOT NULL THEN 1 ELSE 0 END) as fee_rows
            FROM trade_records
            WHERE {where_date} AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
        """, (param,)).fetchone()

    # 2026-08-28 出场日记账 (同 AccountCircuitBreaker): "今日"须按出场日归集, 否则
    # 隔夜仓今日了结的盈亏 (含跳空) 整笔漏记; 7 日窗口按入场日分桶属边界语义, 不动。
    t = _q("substr(exit_time, 1, 10) = ?", today)
    w = _q("trade_date >= ?", since)

    def _pack(r):
        cnt = r["cnt"] or 0
        return {
            "cnt": cnt,
            "gross": round(r["gross"] or 0, 2),
            "fees": round(r["fees"] or 0, 2),
            "net": round(r["net"] or 0, 2),
            "net_win_rate": (r["net_wins"] or 0) / cnt * 100 if cnt else 0,
            "fake_wins": r["fake_wins"] or 0,
            "fee_coverage": (r["fee_rows"] or 0) / cnt * 100 if cnt else 0,
        }

    return {"today": _pack(t), "window": _pack(w), "window_days": window_days}


# ════════════════════════════════════════════════════════════
# 归因视角 1: 策略归因
# ════════════════════════════════════════════════════════════
def attribution_strategy(db, today, window_days=7):
    """按策略维度拆分: 笔数/胜率/PnL/均持时间"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT strategy_id, COUNT(*) as cnt,
               SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl,
               AVG(COALESCE(pnl_net, realized_pnl)) as avg_pnl,
               AVG(holding_hours) as avg_hold
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
        GROUP BY strategy_id ORDER BY total_pnl DESC
    """, (since,)).fetchall()

    today_rows = db.execute("""
        SELECT strategy_id, COUNT(*) as cnt,
               SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl
        FROM trade_records
        WHERE substr(exit_time, 1, 10) = ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
            SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
            SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl,
            AVG(COALESCE(pnl_net, realized_pnl)) as avg_pnl
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
    """退出原因分布: 哪类退出方式实际效果好
    
    Bug fix (2026-05-27): exit_reason 含嵌入百分比 (如 QUICK_EXIT_🛑 硬止损触发 (-63.0% <= -)
    导致 GROUP BY 过细。需归一化到前缀后再聚合。
    """
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT exit_reason, COUNT(*) as cnt,
               SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl,
               AVG(COALESCE(pnl_net, realized_pnl)) as avg_pnl,
               AVG(holding_hours) as avg_hold
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL AND exit_reason IS NOT NULL
        GROUP BY exit_reason ORDER BY total_pnl DESC
    """, (since,)).fetchall()

    # 归一化: 合并同类退出原因 (去掉嵌入的百分比参数)
    normalized = {}
    for r in rows:
        raw = r['exit_reason']
        # 归一化规则: 取到第一个 '(' 之前的部分作为类别名
        paren_idx = raw.find('(')
        if paren_idx > 0:
            key = raw[:paren_idx].strip()
        else:
            key = raw.strip()
        
        if key not in normalized:
            normalized[key] = {
                "reason": key,
                "cnt": 0, "wins": 0,
                "total_pnl": 0, "total_hold": 0,
                "sub_reasons": [],
            }
        n = normalized[key]
        n["cnt"] += r['cnt']
        n["wins"] += r['wins']
        n["total_pnl"] += (r['total_pnl'] or 0)
        n["total_hold"] += (r['avg_hold'] or 0) * r['cnt']
        n["sub_reasons"].append(raw)

    result = []
    for key, n in normalized.items():
        result.append({
            "reason": n["reason"],
            "cnt": n["cnt"],
            "wins": n["wins"],
            "win_rate": n["wins"] / n["cnt"] * 100 if n["cnt"] > 0 else 0,
            "total_pnl": n["total_pnl"],
            "avg_pnl": n["total_pnl"] / n["cnt"] if n["cnt"] > 0 else 0,
            "avg_hold": n["total_hold"] / n["cnt"] if n["cnt"] > 0 else 0,
            "sub_reasons": n["sub_reasons"],
        })
    
    result.sort(key=lambda x: x["total_pnl"], reverse=True)
    return result


# ════════════════════════════════════════════════════════════
# 归因视角 4: 环境归因 (市场regime)
# ════════════════════════════════════════════════════════════
def attribution_regime(db, today, window_days=7):
    """市场regime vs 胜率/PnL: 哪些regime下策略有效"""
    since = (us_eastern_now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT market_regime, COUNT(*) as cnt,
               SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl,
               AVG(COALESCE(pnl_net, realized_pnl)) as avg_pnl
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
               SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl,
               AVG(COALESCE(pnl_net, realized_pnl)) as avg_pnl,
               MAX(COALESCE(pnl_net, realized_pnl)) as best,
               MIN(COALESCE(pnl_net, realized_pnl)) as worst
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
            "n": len(recent),
        }
    else:
        avg5 = None

    today_data = next((d for d in daily if d['date'] == today), None)
    return {"daily": daily, "avg5": avg5, "today": today_data, "window_start": since_5d}


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
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
               SUM(CASE WHEN COALESCE(pnl_net, realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(COALESCE(pnl_net, realized_pnl)) as total_pnl
        FROM trade_records
        WHERE trade_date >= ? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
    """shadow策略对比: baseline vs 变种.

    2026-08-22 (结案A2): shadow_trades 表已死 — 全库无 INSERT 写入方,
    最后记录 2026-06-23 (策略改走 playbook_sim 通道). 原实现 7 天窗口恒空
    属死查询. 保留签名 (synthesize_debate 消费), 直接返回空列表.
    历史 22 行留档作影子期证据.
    """
    return []


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
        WHERE trade_date >= date(?, '-7 days') AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL
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
    """计划 vs 执行: 命中/错失/计划外

    SSOT 是 playbook_instructions 表（当天实际指令），不是 JSON 文件
    （JSON 可能是昨天的残留，导致用旧计划归因今天的交易）。
    """
    rows = db.execute("""
        SELECT symbol, direction, confidence FROM playbook_instructions
        WHERE date = ? AND direction != 'SKIP'
    """, (today,)).fetchall()

    plan_targets = {r['symbol']: r for r in rows}
    traded = db.execute("""
        SELECT DISTINCT symbol FROM trade_records WHERE trade_date = ?
    """, (today,)).fetchall()
    traded_set = {r['symbol'] for r in traded}

    missed = []
    for sym, target in plan_targets.items():
        if sym not in traded_set:
            missed.append({
                "symbol": sym,
                "direction": target["direction"],
                "urgency": "?",
                "rationale": "",
            })

    unplanned = [sym for sym in traded_set if sym not in plan_targets]

    # ponytail: confidence 从 DB 行直接取，不依赖 JSON 字段名（修 final_confidence bug）
    confidences = [r['confidence'] for r in rows if r['confidence']]
    avg_conf = round(sum(confidences) / max(len(confidences), 1), 0) if confidences else 0

    return {
        "plan_count": len(plan_targets),
        "executed": len(traded_set & set(plan_targets.keys())),
        "missed": missed,
        "unplanned": unplanned,
        "confidence": avg_conf,
        "risk_warnings": plan.get("risk_warnings", []),
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

    # 2. 退出归因: 亏损退出聚合检测
    #    (归一化后 exit_attr 中 QUICK_EXIT_🛑 等已合并)
    loss_exits = [e for e in exit_attr if e['total_pnl'] < -500]
    if loss_exits:
        total_loss = sum(e['total_pnl'] for e in loss_exits)
        total_loss_cnt = sum(e['cnt'] for e in loss_exits)
        top_loss = loss_exits[0]  # 已按 total_pnl DESC 排序, 最大亏损在首(负数最小)
        # 找最大亏损 (total_pnl 最负)
        worst = min(loss_exits, key=lambda e: e['total_pnl'])
        consensus.append(
            f"🔴 亏损退出集中: {total_loss_cnt}笔合计亏${total_loss:,.0f}，"
            f"最严重: {worst['reason']} ({worst['cnt']}笔 ${worst['total_pnl']:+,.0f})"
        )
        actions.append(f"审查 {worst['reason']} 触发条件，考虑调整参数或增加入场过滤")

    # 3. ghost/orphan 占比
    if execution['ghost_orphan_pct'] > 30:
        consensus.append(
            f"⚠️ 系统噪声严重: {execution['ghost_orphan_pct']:.0f}% 交易为ghost/orphan清理 "
            f"({execution['ghost_orphan']}/{execution['total_trades']}笔)，不产生实际盈亏"
        )
        actions.append("审计开仓→平仓链路完整性，减少ghost仓位产生")

    # 4. 趋势归因: 除今日外最近 N 个交易日 PnL方向 (N=窗口内交易日数-1, 最多6)
    if trend['avg5']:
        avg_pnl = trend['avg5']['pnl']
        avg_wr = trend['avg5']['win_rate']
        _n = trend['avg5'].get('n', 5)
        if avg_pnl < -500:
            consensus.append(
                f"📉 近{_n}个交易日持续亏损: 日均PnL ${avg_pnl:,.0f}，"
                f"日均净胜率{avg_wr:.0f}%，需评估是否暂停交易收集更多数据"
            )
        elif avg_pnl > 200:
            consensus.append(f"📈 近{_n}个交易日盈利趋势: 日均PnL ${avg_pnl:+,.0f}，净胜率{avg_wr:.0f}%")

    # ── 分歧检测 ──

    # 0. 方向亏损集中检测 (P0: 识别主要亏损来源)
    if execution['direction']:
        dir_losses = [d for d in execution['direction'] if d['total_pnl'] < -1000]
        if dir_losses:
            worst_dir = min(dir_losses, key=lambda d: d['total_pnl'])
            total_dir_loss = sum(d['total_pnl'] for d in dir_losses)
            disputes.append(
                f"方向亏损集中: {worst_dir['dir']}({worst_dir['cnt']}笔 "
                f"净胜率{worst_dir['win_rate']:.0f}% ${worst_dir['total_pnl']:+,.0f}) "
                f"占总亏损{abs(worst_dir['total_pnl']/total_dir_loss*100):.0f}%"
            )
            actions.append(
                f"审查 {worst_dir['dir']} 策略入场条件，"
                f"考虑暂停或收紧该方向参数"
            )

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
                f"方向偏好分歧: {better}({b['cnt']}笔 净胜率{b['win_rate']:.0f}% "
                f"${b['total_pnl']:+,.0f}) 显著优于 "
                f"{worse}({w['cnt']}笔 净胜率{w['win_rate']:.0f}% "
                f"${w['total_pnl']:+,.0f})"
            )

    # 2. 信号归因 vs 策略归因: voting_llm 使用高信号但仍亏
    voting = next((s for s in strategy if 'voting_llm' in s['strategy']), None)
    if voting and voting['total_pnl'] < -200:
        disputes.append(
            f"策略-信号矛盾: voting_llm策略 近7日亏${voting['total_pnl']:,.0f} "
            f"({voting['cnt']}笔 净胜率{voting['win_rate']:.0f}%)，"
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
                f"Shadow baseline: {baseline['cnt']}笔 净胜率{baseline['win_rate']:.0f}% "
                f"PnL=${baseline['total_pnl']:+,.0f}"
            )
            # 找最优变种
            best = max(shadow, key=lambda s: s['total_pnl'])
            if best['strategy'] != 'baseline' and best['total_pnl'] > baseline['total_pnl']:
                shadow_insight += (
                    f"\n  最优变种 {best['strategy']}: "
                    f"{best['cnt']}笔 净胜率{best['win_rate']:.0f}% "
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
                 execution, shadow, risk, plan_attr, lessons, debate, fee_net=None,
                 window_start=None):
    L = []
    L.append(f"### 📊 FTSeek-US 盘后归因辩论报告")
    L.append(f"**{today}** (美东) | equity=${risk['capital']['current_equity']:,.0f}" if risk['capital'] else f"**{today}**")
    L.append("")

    # ─── 费用净额总览 (2026-07-30 G5: 净利才是真实盈利) ───
    if fee_net:
        ft, fw = fee_net["today"], fee_net["window"]
        L.append("## 💰 费用净额总览 (毛/费/净)")
        if ft["cnt"]:
            L.append(
                f"  **今日**(出场日口径, substr(exit_time,1,10)): {ft['cnt']}笔 毛${ft['gross']:+,.2f} "
                f"费${ft['fees']:,.2f} → **净${ft['net']:+,.2f}** "
                f"| 净胜率{ft['net_win_rate']:.0f}%"
                + (f" | ⚠️假盈利{ft['fake_wins']}笔" if ft["fake_wins"] else "")
            )
        if fw["cnt"]:
            L.append(
                f"  **近{fee_net['window_days']}日**(入场日口径, trade_date 分桶): {fw['cnt']}笔 毛${fw['gross']:+,.2f} "
                f"费${fw['fees']:,.2f} → **净${fw['net']:+,.2f}** "
                f"| 净胜率{fw['net_win_rate']:.0f}%"
                + (f" | ⚠️假盈利{fw['fake_wins']}笔" if fw["fake_wins"] else "")
            )
        if ft["cnt"] == 0 and fw["cnt"] == 0:
            L.append("  窗口内无已平仓记录")
        L.append(f"  (费用字段覆盖率: 今日{ft['fee_coverage']:.0f}%/窗口{fw['fee_coverage']:.0f}% — fees_est非缺失占比, 非利润覆盖费用)")
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
    # 2026-09-05 修表头闭合 (用户抓 9/4 报告"近7日净-$321.82"与每日表 5 行求和 +$342
    # 对不上): attribution_trend 窗口=ET now-7 日历日按入场日分桶, 最多含 7 个交易日;
    # 旧代码 [:5] 静默截断最老行 (本次 08-28 14笔 -$664.27) → 读者把截断表当全窗口,
    # 表行求和 ≠ 近7日头条。现渲染窗口内全部交易日 + 表头标注窗口起点。
    # 5日均值语义不动: avg5 = 除今日外最近 5 个交易日均值, 即下方最后 5 行。
    L.append("---")
    _trend_win = f"窗口自{window_start}" if window_start else "近5日"
    L.append(f"## 📈 趋势归因 ({_trend_win})")
    for d in trend['daily']:
        marker = " ◀" if d['date'] == today else ""
        pnl_str = f"${d['total_pnl']:+,.0f}"
        L.append(
            f"  {d['date']}: {d['cnt']}笔 净胜率{d['win_rate']:.0f}% "
            f"PnL={pnl_str} "
            f"best=${d['best']:+,.0f} worst=${d['worst']:+,.0f}{marker}"
        )
    if trend['avg5']:
        a = trend['avg5']
        L.append(f"  **均值(除今日外最近{a['n']}个交易日)**: {a['trades']:.1f}笔/日 净胜率{a['win_rate']:.0f}% 日均PnL=${a['pnl']:+,.0f}")
    L.append("")

    # ─── 策略归因 ───
    L.append("## 🤖 策略归因 (近7日)")
    for s in strategy:
        pnl_marker = "🟢" if s['total_pnl'] > 0 else ("🔴" if s['total_pnl'] < 0 else "⚪")
        today_str = f" | 今日: {s['today_cnt']}笔 ${s['today_pnl']:+,.0f}" if s['today_cnt'] > 0 else ""
        L.append(
            f"  {pnl_marker} **{s['strategy']}**: {s['cnt']}笔 "
            f"净胜率{s['win_rate']:.0f}% PnL=${s['total_pnl']:+,.0f} "
            f"均持{s['avg_hold']:.1f}h{today_str}"
        )
    L.append("")

    # ─── 信号归因 ───
    L.append("## 📡 信号归因 (近30日)")
    for s in signal:
        pnl_marker = "🟢" if s['total_pnl'] > 0 else ("🔴" if s['total_pnl'] < 0 else "⚪")
        L.append(
            f"  {pnl_marker} {s['bucket']}: {s['cnt']}笔 "
            f"净胜率{s['win_rate']:.0f}% PnL=${s['total_pnl']:+,.0f} "
            f"avg=${s['avg_pnl']:+,.0f}"
        )
    L.append("")

    # ─── 退出归因 ───
    # 口径点名 (2026-09-24): 本表 SQL 是 `WHERE trade_date >= ?` = **按入场日分桶**,
    #   与同一份报告里"今日/近7日"头条的出场日口径 (attribution_fee_net 用 substr(exit_time,1,10))
    #   是两把尺子。此前段标题只写"(近7日)", 9/23 报告据此把本表标成"出场日口径"并写出
    #   "48笔 净-$644.34", 复算时按出场日找不到同一组数字 ⇒ 标题必须自带口径, 不得省。
    L.append("## 🚪 退出归因 (近7日·按入场日 trade_date 分桶, 非出场日)")
    total_exit_pnl = 0
    total_exit_cnt = 0
    for e in exit_attr:
        pnl_marker = "🟢" if e['total_pnl'] > 0 else ("🔴" if e['total_pnl'] < 0 else "⚪")
        L.append(
            f"  {pnl_marker} {e['reason']}: {e['cnt']}笔 "
            f"净胜率{e['win_rate']:.0f}% PnL=${e['total_pnl']:+,.0f} "
            f"avg=${e['avg_pnl']:+,.0f}"
        )
        total_exit_pnl += e['total_pnl']
        total_exit_cnt += e['cnt']
    # 退出桶口径注记 (2026-09-24): 名字与实际判据不一致的桶必须自带定义, 否则复盘会去查
    #   不存在的字段。实证 9/23 报告 P0 = "查 OVERNIGHT_GAP_STOP 的 gap%、核对触发条件",
    #   而该桶两个产出点 (exit_engine.py 规则2 / pipeline.py _sl_fallback_exit_reason)
    #   判据都只是"持仓过夜 且 亏损 ≥ overnight_gap_stop_pct(默认3%)", **没有任何 gap 校验**,
    #   metadata 里也不落 gap 字段 —— 5 笔里 CRM(09:49)/RIVN(10:23) 是盘中触发, 不是开盘跳空。
    _EXIT_REASON_LEGEND = {
        "OVERNIGHT_GAP_STOP": (
            "隔夜深亏止损 (判据=持仓过夜 且 亏损≥overnight_gap_stop_pct, 默认3%); "
            "含开盘跳空打穿与盘中触发两类 ⇒ 无 gap% 字段可查, 不要按\"跳空\"解读"
        ),
    }
    for e in exit_attr:
        _lg = _EXIT_REASON_LEGEND.get(e["reason"])
        if _lg:
            L.append(f"    ⓘ {e['reason']} 口径: {_lg}")
    L.append(
        f"  **合计**: {total_exit_cnt}笔 PnL=${total_exit_pnl:+,.0f}"
    )
    # 2026-09-25 (REVIEW-20260925 P0): 最大亏损桶占比由本脚本算好印出, 报告层禁止自行求比。
    #   9/24 报告写 "OVERNIGHT_GAP -$720 占7日窗口全部亏损的 76%" — 76.2% = 720/净亏944
    #   (净额分母), 而占**亏损笔合计**(gross losers, -1789) 仅 40.2%。两个口径都真但语义
    #   完全不同, "全部亏损"字样必须对应亏损笔合计分母, 否则亏损集中度被虚增近一倍。
    _neg_buckets = [e for e in exit_attr if e['total_pnl'] < 0]
    if _neg_buckets:
        _loss_total = sum(e['total_pnl'] for e in _neg_buckets)
        _worst_bucket = min(_neg_buckets, key=lambda e: e['total_pnl'])
        L.append(
            f"  **最大亏损源**: {_worst_bucket['reason']} {_worst_bucket['cnt']}笔 "
            f"${_worst_bucket['total_pnl']:+,.0f} | 占亏损笔合计"
            f"({_loss_total:+,.0f})的{abs(_worst_bucket['total_pnl']/_loss_total*100):.0f}%"
            + (f" | 占窗口净额({total_exit_pnl:+,.0f})的{abs(_worst_bucket['total_pnl']/total_exit_pnl*100):.0f}%"
               if total_exit_pnl < 0 else "")
            + " (两分母口径不同, 引用时必须带口径名)"
        )
    L.append("")

    # ─── 环境归因 ───
    L.append("## 🌡️ 环境归因 (近7日)")
    for r in regime:
        L.append(
            f"  {r['regime']}: {r['cnt']}笔 净胜率{r['win_rate']:.0f}% "
            f"PnL=${r['total_pnl']:+,.0f} "
            f"(信号层面: {r['signal_cnt']}条 净胜率{r['signal_wr']:.0f}%)"
        )
    L.append("")

    # ─── 方向归因 ───
    if execution['direction']:
        L.append("## 🧭 方向归因 (近7日)")
        for d in execution['direction']:
            L.append(
                f"  {d['dir']}: {d['cnt']}笔 净胜率{d['win_rate']:.0f}% "
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
            direction = p.get('direction') or '?'
            size = p.get('size') or 0
            entry = p.get('avg_entry_price')
            entry_str = f"${entry:.2f}" if entry is not None else "N/A"
            sl = p.get('current_sl')
            sl_str = f"${sl:.2f}" if sl is not None else "N/A"
            L.append(
                f"  {p['symbol']} {direction} {size}份 "
                f"@{entry_str} SL={sl_str}{mode}"
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
    # 2026-08-09 (风暴消除): 改走 PushHub 统一通道 (DingTalk>Feishu 串行备援 + 去重).
    #   原实现裸调 FeishuNotifier + DingTalkNotifier.
    #   Qoder 复核修正: ①level 必须 CRITICAL (仅 CRITICAL 广播全通道; NORMAL 是
    #   任一成功即停 → 飞书静默丢消息) ②返回真实结果, 不恒 (True, True).
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
        ok = get_push_hub().push(PushMessage(
            title=message[:60],
            body=message[60:] if len(message) > 60 else "",
            level=Level.CRITICAL,
            category=Category.SYSTEM,
        ))
        logger.info(f"[push] PushHub 推送{'成功' if ok else '失败'}")
        return ok, ok
    except Exception as e:
        logger.error(f"[push] PushHub 推送异常: {e}")
        return False, False


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
        try:
            fee_net = attribution_fee_net(db, today)
        except Exception as _fn_err:
            logger.debug(f"费用净额视角跳过 (旧库无列?): {_fn_err}")
            fee_net = None

        # 辩论合成
        logger.info("🔥 交叉验证归因视角...")
        debate = synthesize_debate(
            strategy, signal, exit_attr, regime, trend,
            execution, shadow, risk, plan_attr, lessons
        )

        # 构建报告
        report = build_report(
            today, strategy, signal, exit_attr, regime, trend,
            execution, shadow, risk, plan_attr, lessons, debate, fee_net=fee_net,
            window_start=trend.get("window_start")
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
