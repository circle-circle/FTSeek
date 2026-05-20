#!/usr/bin/env python3
"""生成并推送盘前作战计划 (Generate & Push Pre-Market Battle Plan)

流程:
1. 读取最近 12h 的 LLM 盘前评估结果 (PreMarketAssessment)。
2. 过滤出 tradeable / high confidence 的标的。
3. 生成 `data/daily_battle_plan.json`。
4. 推送钉钉 Markdown 通知。

运行: 09:00 HKT
"""
import os
import sys
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any

sys.path.insert(0, "/opt/ftseek/FTSeek")

BATTLE_PLAN_PATH = "/opt/ftseek/FTSeek/data/daily_battle_plan.json"
DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"

logger = logging.getLogger("battle_plan")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(handler)

def get_latest_assessments(hours: int = 12) -> List[Dict]:
    """从 trade_evidence 或相关表读取最近的评估"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        # 尝试从 trade_evidence 表读取 (包含 llm_reasoning)
        rows = c.execute("""
            SELECT symbol, llm_reasoning, scene_confidence, primary_strategy
            FROM trade_evidence 
            WHERE created_at > ?
            AND llm_reasoning IS NOT NULL
            ORDER BY created_at DESC
        """, (cutoff,)).fetchall()
        conn.close()
        
        results = []
        for sym, reasoning, conf, strat in rows:
            try:
                reason_json = json.loads(reasoning) if reasoning.startswith('{') else {}
                results.append({
                    "symbol": sym,
                    "confidence": reason_json.get("confidence", conf or 0.0),
                    "direction": reason_json.get("direction", "NO_TRADE"),
                    "rationale": reason_json.get("second_order_insight", ""),
                    "strategy": strat
                })
            except Exception:
                pass
        return results
    except Exception as e:
        logger.warning(f"读取评估失败: {e}")
        return []

def build_plan(assessments: List[Dict]) -> Dict:
    targets = []
    avoid = []
    
    for a in assessments:
        # 过滤: 必须是 BUY/SELL 且置信度 > 0.6
        if a.get("direction") in ("BUY", "SELL") and a.get("confidence", 0) > 0.6:
            # 计算建议仓位 (简化: 基于置信度动态调整 100-500股)
            suggested_qty = int(100 + (a.get("confidence", 0.7) - 0.6) * 4000)
            suggested_qty = max(100, min(1000, suggested_qty)) # 限制范围
            
            a['quantity'] = suggested_qty # 注入数量供执行器使用
            targets.append(a)
        elif a.get("direction") == "SELL":
            avoid.append(a.get("symbol"))
            
    # 去重
    seen = set()
    unique_targets = []
    for t in targets:
        if t["symbol"] not in seen:
            seen.add(t["symbol"])
            unique_targets.append(t)
            
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "market_bias": "NEUTRAL", # TODO: 接入 VIX 分析
        "confidence": sum(t.get("confidence",0) for t in unique_targets) / max(len(unique_targets), 1),
        "target_positions": unique_targets,
        "avoid": list(set(avoid)),
        "execution_time": "09:30 HKT"
    }

def push_to_dingtalk(plan: Dict):
    """推送钉钉通知"""
    from app.notifier import get_dingtalk_notifier
    notifier = get_dingtalk_notifier()
    if not notifier: return
    
    lines = [f"[FTSeek] 盘前作战计划 | {plan['date']}"]
    lines.append(f"置信度: {plan['confidence']:.2f}")
    
    if plan['target_positions']:
        lines.append("🎯 目标:")
        for t in plan['target_positions']:
            emoji = "🔴买入" if t['direction'] == 'BUY' else "🟢卖出"
            lines.append(f"- {t['symbol']} {emoji}")
    else:
        lines.append("📊 今日无高确信度机会，保持观察。")
        
    if plan['avoid']:
        lines.append(f"⚠️ 回避: {', '.join(plan['avoid'])}")
        
    lines.append("⏰ 执行: 09:30 HKT 自动按盘前计划下单")
    lines.append("> 此为全自动计划，仅供知晓。")
    
    notifier.send_text("\n".join(lines))
    logger.info("钉钉推送成功")

if __name__ == "__main__":
    logger.info("开始生成作战计划...")
    assessments = get_latest_assessments()
    plan = build_plan(assessments)
    
    # 保存
    with open(BATTLE_PLAN_PATH, 'w') as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    
    # 推送
    push_to_dingtalk(plan)
    logger.info(f"计划已保存: {BATTLE_PLAN_PATH}")
