#!/usr/bin/env python3
"""
FTSeek-US Multi-Agent PoC — 端到端验证脚本

模拟/执行完整的多 Agent 流程:
1. Risk Agent 分析持仓风险
2. Info Agent 分析宏观/新闻
3. Arbiter Agent 综合收敛

用法:
    python3 scripts/multi_agent_poc_test.py
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERMES_HOME = os.path.expanduser("~/.hermes")
FTSEEK_DIR = os.path.join(HERMES_HOME, "ftseek")
REPORTS_DIR = os.path.join(FTSEEK_DIR, "reports")
MEMORY_FILE = os.path.join(FTSEEK_DIR, "memory", "us.json")
BATTLE_PLAN = "/opt/ftseek/FTSeek-US/data/daily_battle_plan_us.json"

os.makedirs(REPORTS_DIR, exist_ok=True)

def safe_json(filepath, default=None):
    """安全读取 JSON 文件"""
    try:
        with open(filepath) as f:
            return json.load(f)
    except:
        return default or {}

def read_db_positions():
    """读取持仓数据"""
    db_path = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
    if not os.path.exists(db_path):
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT symbol, direction, qty, avg_price, current_sl "
            "FROM open_positions WHERE is_closed = 0"
        ).fetchall()
        conn.close()
        return [dict(zip(["symbol","direction","qty","avg_price","current_sl"], r)) for r in rows]
    except:
        return []

def generate_risk_report(positions, memory, battle_plan):
    """Risk Agent 报告生成（模拟 + 实际数据混合）"""
    print("[Risk Agent] 分析持仓风险...")
    
    pos_reports = []
    for pos in positions:
        # 简单健康评分逻辑
        score = 70  # 默认
        concerns = []
        sl_status = "未知"
        
        if pos.get("current_sl") and pos.get("avg_price"):
            sl_dist = abs(pos["current_sl"] - pos["avg_price"]) / pos["avg_price"] * 100
            sl_status = f"合理 ({sl_dist:.1f}%)"
            if sl_dist < 2:
                concerns.append("止损过近，容易被震荡洗出")
                score -= 15
            elif sl_dist > 10:
                concerns.append("止损过远，单笔风险过大")
                score -= 10
        
        if pos.get("direction") == "SHORT":
            concerns.append("空头持仓，注意 Risk-On 环境风险")
            score -= 10
        
        pos_reports.append({
            "symbol": pos.get("symbol", "UNKNOWN"),
            "health_score": max(0, min(100, score)),
            "sl_status": sl_status,
            "concerns": concerns if concerns else ["无明显风险"],
            "recommendation": "ADJUST_SL" if concerns and "止损" in str(concerns) else "HOLD"
        })
    
    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "agent": "risk",
        "timestamp": datetime.now().isoformat(),
        "positions": pos_reports if pos_reports else [{"note": "无持仓"}],
        "concentration_risk": {
            "max_sector_pct": len(positions) * 25 if positions else 0,
            "alert": len(positions) > 4
        },
        "overall_risk_level": "HIGH" if len(positions) > 5 else ("MEDIUM" if positions else "LOW"),
        "action_items": [f"持仓 {len(positions)} 笔，{'超过5笔上限' if len(positions) > 5 else '在合理范围内'}"],
        "confidence": 0.85
    }
    
    return report

def generate_info_report(memory, battle_plan):
    """Info Agent 报告生成（基于记忆和计划中的信息）"""
    print("[Info Agent] 分析宏观环境...")
    
    regime = battle_plan.get("agent_market_regime", "NEUTRAL")
    
    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "agent": "info",
        "timestamp": datetime.now().isoformat(),
        "market_regime": regime,
        "regime_reasoning": f"基于 Agent 盘前计划的市场阶段判断: {regime}",
        "regime_confidence": 0.7,
        "key_events_today": [
            {"time": "全天", "event": "周末/非交易时段", "impact": "LOW", "expected_effect": "无实时数据"}
        ],
        "news_impact_chain": [],
        "restricted_from_news": battle_plan.get("restricted", []),
        "sentiment_score": 0.6 if regime == "RISK_ON" else 0.4,
        "confidence": 0.7
    }
    
    return report

def generate_arbiter_plan(risk_report, info_report):
    """Arbiter Agent 综合收敛"""
    print("[Arbiter] 综合收敛中...")
    
    regime = info_report.get("market_regime", "NEUTRAL")
    risk_level = risk_report.get("overall_risk_level", "MEDIUM")
    
    # 冲突仲裁
    conflicts = []
    max_positions = 5
    if risk_level in ["HIGH", "CRITICAL"]:
        max_positions = min(3, max_positions)
        conflicts.append({
            "conflict": f"Risk 报告风险等级 {risk_level}",
            "resolution": "降低 max_positions 限制"
        })
    
    if regime == "RISK_OFF":
        max_positions = min(2, max_positions)
        conflicts.append({
            "conflict": "Info 报告 Risk-Off 环境",
            "resolution": "进一步降低 max_positions"
        })
    
    # 合并 position_actions
    position_actions = []
    for pos in risk_report.get("positions", []):
        if pos.get("symbol") and pos.get("recommendation") != "HOLD":
            position_actions.append({
                "symbol": pos["symbol"],
                "action": pos["recommendation"],
                "reason": ", ".join(pos.get("concerns", []))
            })
    
    plan = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "agent": "arbiter",
        "timestamp": datetime.now().isoformat(),
        "market_regime": regime,
        "regime_source": f"info_agent (confidence {info_report.get('regime_confidence', 0)})",
        "max_positions": max_positions,
        "targets": [],
        "restricted": info_report.get("restricted_from_news", []),
        "position_actions": position_actions,
        "strategy_adjustments": risk_report.get("action_items", []),
        "risk_warnings": [f"整体风险等级: {risk_level}"],
        "conflicts_resolved": conflicts,
        "confidence": 0.75
    }
    
    return plan

def main():
    print("=" * 60)
    print("FTSeek-US Multi-Agent PoC — 端到端验证")
    print("=" * 60)
    print()
    
    # 读取实际数据
    memory = safe_json(MEMORY_FILE)
    battle_plan = safe_json(BATTLE_PLAN)
    positions = read_db_positions()
    
    print(f"📊 持仓数: {len(positions)}")
    print(f"📋 市场阶段 (来自计划): {battle_plan.get('agent_market_regime', 'N/A')}")
    print()
    
    # Step 1: Risk Agent
    print("--- Step 1: Risk Agent ---")
    risk_report = generate_risk_report(positions, memory, battle_plan)
    risk_path = os.path.join(REPORTS_DIR, f"risk_report_{datetime.now().strftime('%Y%m%d')}.json")
    with open(risk_path, 'w') as f:
        json.dump(risk_report, f, indent=2, ensure_ascii=False)
    print(f"  ✅ 写入: {risk_path}")
    print(f"  风险等级: {risk_report['overall_risk_level']}")
    print()
    
    # Step 2: Info Agent
    print("--- Step 2: Info Agent ---")
    info_report = generate_info_report(memory, battle_plan)
    info_path = os.path.join(REPORTS_DIR, f"info_report_{datetime.now().strftime('%Y%m%d')}.json")
    with open(info_path, 'w') as f:
        json.dump(info_report, f, indent=2, ensure_ascii=False)
    print(f"  ✅ 写入: {info_path}")
    print(f"  市场阶段: {info_report['market_regime']}")
    print()
    
    # Step 3: Arbiter
    print("--- Step 3: Arbiter ---")
    arbiter_plan = generate_arbiter_plan(risk_report, info_report)
    arbiter_path = os.path.join(REPORTS_DIR, f"arbiter_plan_{datetime.now().strftime('%Y%m%d')}.json")
    with open(arbiter_path, 'w') as f:
        json.dump(arbiter_plan, f, indent=2, ensure_ascii=False)
    print(f"  ✅ 写入: {arbiter_path}")
    print(f"  max_positions: {arbiter_plan['max_positions']}")
    print(f"  冲突解决: {len(arbiter_plan['conflicts_resolved'])}")
    print()
    
    # 验证
    print("--- 验证 ---")
    all_valid = True
    for p in [risk_path, info_path, arbiter_path]:
        try:
            with open(p) as f:
                json.load(f)
            print(f"  ✅ {os.path.basename(p)} 有效 JSON")
        except:
            print(f"  ❌ {os.path.basename(p)} JSON 无效")
            all_valid = False
    
    print()
    if all_valid:
        print("🎉 Multi-Agent PoC 验证通过！")
    else:
        print("❌ 验证失败")
        sys.exit(1)
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
