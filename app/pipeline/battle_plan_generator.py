"""
盘前作战计划生成器 (Battle Plan Generator)

职责:
1. 接收 LLM 的分析结果 (PreMarketAssessment) 或 聚合的宏观分析。
2. 提取关键的 market_regime, stop_trading, 以及具体的 plans (targets/restricted)。
3. 生成 standard JSON 格式写入 `daily_battle_plan.json`。
4. 通过钉钉 (DingTalk) 推送盘前作战计划摘要。
"""
import json
import logging
import time
from datetime import date
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger("ftseek.pipeline.battle_plan_generator")

class BattlePlanGenerator:
    """盘前作战计划生成器"""
    
    def __init__(self, output_path: str = None):
        self.output_path = output_path or str(Path(__file__).resolve().parent.parent.parent / "data" / "daily_battle_plan.json")
        
    def generate_from_assessments(self, assessments: List[Any]) -> str:
        """
        从多个事件包分析结果中聚合生成作战计划
        """
        if not assessments:
            logger.warning("没有分析结果，生成保守计划")
            return self._save_fallback_plan("No analysis results")

        # 聚合逻辑 (取最悲观的 risk_level，提取所有 focus_symbols 为 targets)
        targets = []
        restricted = []
        max_risk = "LOW"
        overall_bias = "NEUTRAL"
        stop_trading = False
        
        # 简单聚合: 只要有 focus_symbols 就加入 target (去重)
        # 实际生产中可以加权重 (confidence 高的排前面)
        for a in assessments:
            if getattr(a, "direction", "NO_TRADE") != "NO_TRADE":
                for sym in getattr(a, "focus_symbols", []):
                    targets.append({
                        "symbol": sym,
                        "direction": getattr(a, "direction", "ANY").upper(),
                        "status": "VALID",
                        "urgency": "High" if getattr(a, "confidence", 0) > 0.7 else "Medium",
                        "key_trigger": getattr(a, "recommendation", "")[:50],
                        "invalidation_reason": getattr(a, "invalidating_factors", [""])[0] if getattr(a, "invalidating_factors", []) else ""
                    })
        
        # 去重
        seen = set()
        unique_targets = []
        for t in targets:
            if t['symbol'] not in seen:
                seen.add(t['symbol'])
                unique_targets.append(t)
        
        plan = {
            "date": date.today().isoformat(),
            "targets": unique_targets,
            "restricted": restricted,
            "overall_bias": overall_bias,
            "market_regime": max_risk,
            "risk_level": "MEDIUM",
            "stop_trading": stop_trading,
            "generated_at": time.time()
        }
        
        return self._save_plan(plan)

    def generate_from_llm_json(self, raw_json: Dict) -> str:
        """直接从 LLM 的结构化 JSON 生成"""
        try:
            plans = raw_json.get("plans", [])
            targets = []
            for p in plans:
                targets.append({
                    "symbol": p.get("symbol"),
                    "direction": p.get("bias", "ANY").upper(),
                    "status": "VALID",
                    "urgency": "High" if p.get("conviction", 0) > 0.7 else "Medium",
                    "key_trigger": p.get("key_trigger", ""),
                    "invalidation_reason": p.get("invalidation", "")
                })
            
            plan = {
                "date": date.today().isoformat(),
                "targets": targets,
                "restricted": raw_json.get("restricted", []),
                "overall_bias": raw_json.get("market_regime", "NEUTRAL"),
                "market_regime": raw_json.get("market_regime", "NEUTRAL"),
                "risk_level": raw_json.get("risk_level", "MEDIUM"),
                "stop_trading": raw_json.get("stop_trading", False),
                "generated_at": time.time()
            }
            return self._save_plan(plan)
        except Exception as e:
            logger.error(f"解析 LLM JSON 失败: {e}")
            return self._save_fallback_plan(str(e))

    def _save_plan(self, plan: Dict) -> str:
        """保存计划并发送通知"""
        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, 'w') as f:
            json.dump(plan, f, indent=2)
        
        logger.info(f"✅ 作战计划已生成: {self.output_path} ({len(plan['targets'])} 个目标)")
        self._send_notification(plan)
        return self.output_path

    def _save_fallback_plan(self, reason: str) -> str:
        """兜底计划"""
        plan = {
            "date": date.today().isoformat(),
            "targets": [],
            "restricted": [],
            "overall_bias": "NEUTRAL",
            "market_regime": "UNKNOWN",
            "risk_level": "HIGH",
            "stop_trading": False,
            "reason": f"Fallback: {reason}",
            "generated_at": time.time()
        }
        return self._save_plan(plan)

    def _send_notification(self, plan: Dict):
        """发送钉钉通知 (HK)"""
        try:
            from app.risk.dingtalk_alert import init_dingtalk_alert, DingTalkAlertConfig
            from app import config
            
            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET
            )
            
            count = len(plan.get("targets", []))
            regime = plan.get("market_regime", "N/A")
            risk = plan.get("risk_level", "N/A")
            
            text = (f"📊 港股盘前作战计划已生成\n"
                    f"📅 日期: {plan.get('date')}\n"
                    f"🌍 环境: {regime}\n"
                    f"⚠️ 风险: {risk}\n"
                    f"🎯 目标: {count} 只标的\n"
                    f"🛑 停止交易: {'是' if plan.get('stop_trading') else '否'}")
            
            sender.send_text(text, alert_level="INFO")
        except Exception as e:
            logger.error(f"发送盘前计划通知失败: {e}")
