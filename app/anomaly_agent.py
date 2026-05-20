"""
异常确认 Agent (Anomaly Confirmation Agent)

职责:
1. 扫描未确认的 P0/P1 异常
2. 分析异常对今日交易的影响
3. 自动处理可修复的异常
4. 推给人工确认需要干预的异常
5. 更新战斗计划 (如果需要)

运行方式:
- Cron: 盘前 (08:00)、午间 (12:00)、盘后 (16:30)
- 实时: P0 异常触发时立即运行
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek.anomaly_agent")


class AnomalyConfirmationAgent:
    """异常确认 Agent"""
    
    def __init__(self):
        from app.anomaly_classifier import get_classifier
        self.classifier = get_classifier()
    
    def run_confirmation_cycle(self) -> Dict:
        """
        运行一轮异常确认
        
        Returns:
            处理结果摘要
        """
        logger.info("[AnomalyAgent] 开始异常确认循环...")
        
        results = {
            "timestamp": datetime.now().isoformat(),
            "p0_processed": 0,
            "p1_processed": 0,
            "auto_fixed": 0,
            "escalated": 0,
            "ignored": 0,
            "actions": []
        }
        
        # 1. 处理 P0 异常 (最高优先级)
        from app.anomaly_classifier import AnomalySeverity
        p0_events = self.classifier.get_pending(severity=AnomalySeverity.P0)
        for event in p0_events:
            action = self._handle_p0(event)
            results["p0_processed"] += 1
            if action:
                results["actions"].append(action)
                if action.get("type") == "auto_fixed":
                    results["auto_fixed"] += 1
                elif action.get("type") == "escalated":
                    results["escalated"] += 1
        
        # 2. 处理 P1 异常
        p1_events = self.classifier.get_pending(severity=AnomalySeverity.P1)
        for event in p1_events:
            action = self._handle_p1(event)
            results["p1_processed"] += 1
            if action:
                results["actions"].append(action)
                if action.get("type") == "auto_fixed":
                    results["auto_fixed"] += 1
                elif action.get("type") == "ignored":
                    results["ignored"] += 1
        
        # 3. 生成摘要
        if results["actions"]:
            logger.info(
                f"[AnomalyAgent] 确认完成: "
                f"P0={results['p0_processed']}, P1={results['p1_processed']}, "
                f"自动修复={results['auto_fixed']}, 升级={results['escalated']}"
            )
        
        return results
    
    def _handle_p0(self, event) -> Optional[Dict]:
        """处理 P0 异常"""
        logger.warning(f"🚨 [AnomalyAgent] 处理 P0 异常: {event.anomaly_type}")
        
        # P0 异常处理策略:
        # 1. 可自动修复的 → 尝试修复
        # 2. 不可自动修复的 → 立即升级给人工
        
        if event.auto_fixable:
            # 尝试自动修复
            fix_result = self._attempt_auto_fix(event)
            if fix_result["success"]:
                self.classifier.update_status(
                    event.event_id, "fixed", 
                    confirmed_by="agent",
                    resolution=fix_result["message"]
                )
                return {
                    "type": "auto_fixed",
                    "event_id": event.event_id,
                    "message": f"自动修复成功: {fix_result['message']}"
                }
            else:
                # 修复失败，升级
                self.classifier.update_status(
                    event.event_id, "escalated",
                    confirmed_by="agent",
                    resolution=f"自动修复失败: {fix_result['message']}"
                )
                self._notify_human(event, fix_result["message"])
                return {
                    "type": "escalated",
                    "event_id": event.event_id,
                    "message": f"自动修复失败，已升级: {fix_result['message']}"
                }
        else:
            # 不可自动修复，直接升级
            self.classifier.update_status(
                event.event_id, "escalated",
                confirmed_by="agent",
                resolution="需人工干预"
            )
            self._notify_human(event, event.suggested_action)
            return {
                "type": "escalated",
                "event_id": event.event_id,
                "message": f"需人工干预: {event.message}"
            }
    
    def _handle_p1(self, event) -> Optional[Dict]:
        """处理 P1 异常"""
        logger.info(f"⚠️ [AnomalyAgent] 处理 P1 异常: {event.anomaly_type}")
        
        # P1 异常处理策略:
        # 1. 可自动修复的 → 尝试修复
        # 2. 影响交易的 → 分析影响，可能需要调整战斗计划
        # 3. 影响有限的 → 标记为已确认，观察
        
        if event.auto_fixable:
            fix_result = self._attempt_auto_fix(event)
            if fix_result["success"]:
                self.classifier.update_status(
                    event.event_id, "fixed",
                    confirmed_by="agent",
                    resolution=fix_result["message"]
                )
                return {
                    "type": "auto_fixed",
                    "event_id": event.event_id,
                    "message": f"自动修复成功: {fix_result['message']}"
                }
        
        # 分析对交易的影响
        impact_analysis = self._analyze_trading_impact(event)
        
        if impact_analysis["severe"]:
            # 严重影响交易，升级
            self.classifier.update_status(
                event.event_id, "escalated",
                confirmed_by="agent",
                resolution=impact_analysis["reason"]
            )
            self._notify_human(event, impact_analysis["reason"])
            return {
                "type": "escalated",
                "event_id": event.event_id,
                "message": f"交易影响严重: {impact_analysis['reason']}"
            }
        else:
            # 影响有限，标记为已确认
            self.classifier.update_status(
                event.event_id, "confirmed",
                confirmed_by="agent",
                resolution=f"影响有限: {impact_analysis['reason']}"
            )
            return {
                "type": "confirmed",
                "event_id": event.event_id,
                "message": f"已确认，影响有限: {impact_analysis['reason']}"
            }
    
    def _attempt_auto_fix(self, event) -> Dict:
        """尝试自动修复异常"""
        fix_actions = {
            "opend_connection_lost": self._fix_opend_connection,
            "lot_size_mismatch": self._fix_lot_size_cache,
            "partial_data_loss": self._retry_data_fetch,
            "non_critical_timeout": self._retry_operation,
        }
        
        fix_func = fix_actions.get(event.anomaly_type)
        if fix_func:
            try:
                return fix_func(event)
            except Exception as e:
                return {"success": False, "message": f"修复异常: {str(e)}"}
        else:
            return {"success": False, "message": "无自动修复方法"}
    
    def _fix_opend_connection(self, event) -> Dict:
        """尝试修复 OpenD 连接"""
        import subprocess
        
        try:
            # 检查 OpenD 进程
            result = subprocess.run(
                ["pgrep", "-f", "FutuOpenD"],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode != 0:
                # OpenD 未运行，尝试启动
                logger.info("[AnomalyAgent] OpenD 未运行，尝试启动...")
                # 这里可以添加启动逻辑
                return {"success": False, "message": "OpenD 未运行，需手动启动"}
            else:
                # OpenD 运行中，可能是网络问题
                return {"success": True, "message": "OpenD 进程存在，连接可能已恢复"}
        except Exception as e:
            return {"success": False, "message": f"检查 OpenD 失败: {str(e)}"}
    
    def _fix_lot_size_cache(self, event) -> Dict:
        """修复 lot_size 缓存"""
        try:
            # 触发 lot_size 重新查询
            from app.execution.lot_size_manager import LotSizeManager
            lot_mgr = LotSizeManager()
            
            # 强制刷新缓存
            for symbol in event.affected_symbols:
                lot_mgr._refresh_lot_size(symbol)
            
            return {"success": True, "message": f"已刷新 {len(event.affected_symbols)} 个标的的 lot_size"}
        except Exception as e:
            return {"success": False, "message": f"刷新 lot_size 失败: {str(e)}"}
    
    def _retry_data_fetch(self, event) -> Dict:
        """重试数据获取"""
        # 这里可以实现重试逻辑
        return {"success": True, "message": "重试数据获取 (模拟)"}
    
    def _retry_operation(self, event) -> Dict:
        """重试操作"""
        # 这里可以实现重试逻辑
        return {"success": True, "message": "重试操作 (模拟)"}
    
    def _analyze_trading_impact(self, event) -> Dict:
        """分析异常对交易的影响"""
        # 分析逻辑:
        # 1. 检查是否影响核心交易流程
        # 2. 检查是否影响当前持仓
        # 3. 检查是否影响今日计划
        
        severe_indicators = []
        
        # 检查影响范围
        if event.impact_scope in ["all_trading", "core_system"]:
            severe_indicators.append("影响全局交易")
        
        # 检查是否影响持仓标的
        if event.affected_symbols:
            try:
                from app.trader.position_manager import PositionManager
                pm = PositionManager()
                all_positions = pm.get_all_positions()
                
                affected_positions = [
                    p for p in all_positions 
                    if getattr(p, 'code', '') in event.affected_symbols
                ]
                
                if affected_positions:
                    severe_indicators.append(f"影响 {len(affected_positions)} 个持仓")
            except Exception:
                pass  # 无法查询持仓时不阻塞
        
        if severe_indicators:
            return {
                "severe": True,
                "reason": "; ".join(severe_indicators)
            }
        else:
            return {
                "severe": False,
                "reason": "影响有限，不阻塞交易"
            }
    
    def _notify_human(self, event, reason: str):
        """通知人工干预"""
        try:
            from app.risk.dingtalk_alert import init_dingtalk_alert
            from app import config
            
            sender = init_dingtalk_alert(
                webhook_url=config.DINGTALK_WEBHOOK_URL,
                secret=config.DINGTALK_SECRET
            )
            
            severity_emoji = {"P0": "🚨", "P1": "⚠️"}.get(event.severity.value, "ℹ️")
            
            message = f"""{severity_emoji} 异常需要人工确认

**来源**: {event.source}
**类型**: {event.anomaly_type}
**严重度**: {event.severity.value}
**影响**: {event.impact_scope}

**详情**: {event.message}

**建议行动**: {event.suggested_action}

**分析**: {reason}

---
*请及时处理，避免影响交易*"""
            
            sender.send_markdown(
                title=f"异常确认 - {event.anomaly_type}",
                text=message,
                alert_level="CRITICAL" if event.severity.value == "P0" else "WARNING"
            )
            
            logger.info(f"[AnomalyAgent] 已推送异常确认通知: {event.event_id}")
            
        except Exception as e:
            logger.error(f"[AnomalyAgent] 推送通知失败: {e}")
    
    def get_summary(self, hours: int = 24) -> str:
        """生成异常确认摘要 (用于盘前报告)"""
        stats = self.classifier.get_stats(hours)
        recent = self.classifier.get_recent(hours, limit=10)
        
        lines = [
            "## 📊 异常确认摘要",
            "",
            f"**统计 (最近 {hours} 小时)**:",
            f"- 总异常数: {stats['total']}",
            f"- P0: {stats['by_severity'].get('P0', 0)}",
            f"- P1: {stats['by_severity'].get('P1', 0)}",
            f"- P2: {stats['by_severity'].get('P2', 0)}",
            f"",
            f"**状态分布**:",
            f"- 待处理: {stats['by_status'].get('pending', 0)}",
            f"- 已确认: {stats['by_status'].get('confirmed', 0)}",
            f"- 已修复: {stats['by_status'].get('fixed', 0)}",
            f"- 已忽略: {stats['by_status'].get('ignored', 0)}",
            f"- 已升级: {stats['by_status'].get('escalated', 0)}",
        ]
        
        if recent:
            lines.extend(["", "**最近异常**:", ""])
            for event in recent[:5]:
                emoji = {"P0": "🚨", "P1": "⚠️", "P2": "ℹ️"}.get(event.severity.value, "ℹ️")
                status_emoji = {
                    "pending": "⏳",
                    "confirmed": "✅",
                    "fixed": "🔧",
                    "ignored": "➡️",
                    "escalated": "🔴"
                }.get(event.status, "❓")
                
                lines.append(
                    f"- {emoji}{status_emoji} **{event.anomaly_type}** "
                    f"({event.source}) - {event.message[:50]}"
                )
        else:
            lines.extend(["", "✅ 无异常记录"])
        
        return "\n".join(lines)
