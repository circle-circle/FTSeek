"""
生产监控器 - 部署后持续验证预期行为与实际日志

设计目标:
1. 部署后启动后台监控线程
2. 持续对比预期行为 vs 实际日志
3. 偏离检测 - 当偏离超过阈值时自动上报
4. 飞书告警 - 通知关键偏离事件

版本: 1.0.0
日期: 2026-05-03
"""

import json
import logging
from app.core.paths import get_battle_plan_path, get_logs_dir
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger("ftseek_us.production_monitor")


class ProductionMonitor:
    """生产监控器"""
    
    def __init__(
        self,
        plan_path: str = None,
        log_path: str = None,
        alert_webhook: str = None
    ):
        from app.core.paths import get_battle_plan_path, get_logs_dir as _gld
        self.plan_path = Path(plan_path) if plan_path else get_battle_plan_path()
        self.log_path = Path(log_path) if log_path else (_gld() / "ftseek_us.log")
        self.alert_webhook = alert_webhook or os.environ.get("FEISHU_WEBHOOK_URL_US", "")
        
        # 告警阈值
        self.alert_thresholds = {
            "target_miss_rate": 0.3,       # 30%目标未出现
            "direction_accuracy": 0.6,     # 60%方向准确率
            "error_spike": 5,              # 错误数突增阈值
            "check_interval": 300          # 5分钟检查一次
        }
        
        # 监控状态
        self.expected_behavior = {}
        self.actual_behavior = []
        self._monitor_thread = None
        self._stop_event = threading.Event()
        self._last_check_time = None
        
        logger.info(f"[生产监控] 已初始化 | 计划={plan_path} | 日志={log_path}")
    
    def start_monitoring(self, debate_result: Dict):
        """
        开始监控
        
        Args:
            debate_result: 辩论结果，包含预期行为
        """
        # 提取预期行为
        self.expected_behavior = self._extract_expectations(debate_result)
        
        # 启动后台监控线程
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.warning("[生产监控] 监控已在运行")
            return
        
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="production-monitor"
        )
        self._monitor_thread.start()
        
        logger.info(
            f"[生产监控] 已开始监控 | "
            f"目标数={len(self.expected_behavior.get('targets', []))} | "
            f"检查间隔={self.alert_thresholds['check_interval']}s"
        )
    
    def stop_monitoring(self):
        """停止监控"""
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)
        logger.info("[生产监控] 已停止监控")
    
    def _monitor_loop(self):
        """监控循环"""
        self._last_check_time = datetime.now()
        
        while not self._stop_event.is_set():
            try:
                # 等待下一个检查周期
                self._stop_event.wait(timeout=self.alert_thresholds['check_interval'])
                if self._stop_event.is_set():
                    break
                
                # 1. 读取最新日志
                logs = self._read_recent_logs()
                
                # 2. 对比预期vs实际
                deviations = self._check_deviations(logs)
                
                # 3. 如果偏离超过阈值，上报
                if deviations:
                    self._alert_deviations(deviations)
                
                self._last_check_time = datetime.now()
                
            except Exception as e:
                logger.error(f"[生产监控] 监控循环异常: {e}", exc_info=True)
                time.sleep(60)  # 出错后等待1分钟再重试
    
    def _extract_expectations(self, debate_result: Dict) -> Dict:
        """从辩论结果提取预期行为"""
        targets = []
        
        # 从收敛结果中提取目标
        converged = debate_result.get("converged_result", {})
        if "targets" in converged:
            for t in converged["targets"]:
                if isinstance(t, dict) and t.get("symbol"):
                    targets.append({
                        "symbol": t["symbol"],
                        "direction": t.get("direction", "unknown"),
                        "entry_zone": t.get("entry_zone", []),
                        "stop_loss": t.get("stop_loss", 0),
                        "take_profit": t.get("take_profit", 0)
                    })
        
        return {
            "targets": targets,
            "market_regime": converged.get("market_regime", "unknown"),
            "timestamp": datetime.now().isoformat()
        }
    
    def _read_recent_logs(self) -> List[str]:
        """读取最近日志"""
        if not self.log_path.exists():
            return []
        
        logs = []
        try:
            # 读取最后100行
            with open(self.log_path, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()
                logs = all_lines[-100:]
        except Exception as e:
            logger.warning(f"[生产监控] 读取日志失败: {e}")
        
        return logs
    
    def _check_deviations(self, logs: List[str]) -> List[Dict]:
        """检查偏离"""
        deviations = []
        
        # 1. 检查目标是否出现 (交易执行)
        for target in self.expected_behavior.get("targets", []):
            symbol = target.get("symbol", "")
            if not self._target_appeared(symbol, logs):
                deviations.append({
                    "type": "target_missing",
                    "severity": "medium",
                    "target": symbol,
                    "expected": f"应有交易执行",
                    "actual": "未检测到相关日志",
                    "timestamp": datetime.now().isoformat()
                })
        
        # 2. 检查错误数突增
        error_count = self._count_errors(logs)
        if error_count > self.alert_thresholds['error_spike']:
            deviations.append({
                "type": "error_spike",
                "severity": "high",
                "expected": f"<{self.alert_thresholds['error_spike']} 错误",
                "actual": f"{error_count} 错误",
                "timestamp": datetime.now().isoformat()
            })
        
        # 3. 检查方向准确率 (如果有交易记录)
        actual_directions = self._extract_actual_directions(logs)
        if actual_directions:
            accuracy = self._calculate_direction_accuracy(actual_directions)
            if accuracy < self.alert_thresholds['direction_accuracy']:
                deviations.append({
                    "type": "direction_accuracy_low",
                    "severity": "high",
                    "expected": f">={self.alert_thresholds['direction_accuracy']:.0%} 准确率",
                    "actual": f"{accuracy:.0%} 准确率",
                    "timestamp": datetime.now().isoformat()
                })
        
        return deviations
    
    def _target_appeared(self, symbol: str, logs: List[str]) -> bool:
        """检查目标是否在日志中出现"""
        symbol_clean = symbol.replace("US.", "")
        
        for line in logs:
            if symbol_clean in line or symbol in line:
                # 检查是否是执行相关日志
                if any(kw in line.lower() for kw in ["entry", "exit", "order", "trade", "exec"]):
                    return True
        
        return False
    
    def _count_errors(self, logs: List[str]) -> int:
        """统计错误数"""
        error_count = 0
        for line in logs:
            if any(kw in line.lower() for kw in ["error", "exception", "failed", "critical"]):
                error_count += 1
        return error_count
    
    def _extract_actual_directions(self, logs: List[str]) -> List[Dict]:
        """从日志提取实际交易方向"""
        directions = []
        
        for line in logs:
            # 匹配交易执行日志
            match = re.search(r'(US\.[A-Z]+).*(LONG|SHORT)', line, re.IGNORECASE)
            if match:
                directions.append({
                    "symbol": match.group(1),
                    "direction": match.group(2).upper()
                })
        
        return directions
    
    def _calculate_direction_accuracy(self, actual_directions: List[Dict]) -> float:
        """计算方向准确率"""
        if not actual_directions:
            return 1.0
        
        correct = 0
        total = len(actual_directions)
        
        for actual in actual_directions:
            expected = next(
                (t for t in self.expected_behavior.get("targets", [])
                 if t.get("symbol") == actual["symbol"]),
                None
            )
            
            if expected and expected.get("direction") == actual["direction"]:
                correct += 1
        
        return correct / total if total > 0 else 0
    
    def _alert_deviations(self, deviations: List[Dict]):
        """上报偏离"""
        logger.warning(f"[生产监控] 检测到 {len(deviations)} 个偏离:")
        
        for d in deviations:
            logger.warning(
                f"  - [{d['severity'].upper()}] {d['type']}: "
                f"{d.get('target', d.get('actual', ''))}"
            )
        
        # 发送飞书告警
        if self.alert_webhook:
            self._send_feishu_alert(deviations)
        else:
            logger.warning("[生产监控] 飞书Webhook未配置，跳过告警")
    
    def _send_feishu_alert(self, deviations: List[Dict]):
        """发送飞书告警"""
        import requests
        
        title = "🚨 FTSeek-US 生产监控告警"
        
        content_lines = [
            f"**{title}**",
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"偏离数: {len(deviations)}",
            ""
        ]
        
        for d in deviations:
            severity_icon = "🔴" if d["severity"] == "high" else "🟡"
            content_lines.append(f"{severity_icon} **{d['type']}**")
            content_lines.append(f"  预期: {d['expected']}")
            content_lines.append(f"  实际: {d['actual']}")
            content_lines.append("")
        
        message = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "red"
                },
                "elements": [{
                    "tag": "markdown",
                    "content": "\n".join(content_lines)
                }]
            }
        }
        
        try:
            response = requests.post(
                self.alert_webhook,
                json=message,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"[生产监控] 飞书告警已发送 | {len(deviations)}个偏离")
        except Exception as e:
            logger.error(f"[生产监控] 飞书告警发送失败: {e}")
    
    def get_monitoring_status(self) -> Dict:
        """获取监控状态"""
        return {
            "is_running": self._monitor_thread.is_alive() if self._monitor_thread else False,
            "expected_targets": len(self.expected_behavior.get("targets", [])),
            "last_check": self._last_check_time.isoformat() if self._last_check_time else None,
            "alert_thresholds": self.alert_thresholds
        }


# ── 模块级单例 ──────────────────────────────────────────────

_monitor: Optional[ProductionMonitor] = None


def get_production_monitor(
    plan_path: str = None,
    log_path: str = None,
    alert_webhook: str = None
) -> ProductionMonitor:
    """获取全局生产监控器实例"""
    global _monitor
    if _monitor is None:
        if plan_path is None:
            plan_path = str(get_battle_plan_path())
        if log_path is None:
            log_path = str(get_logs_dir() / "ftseek_us.log")
        _monitor = ProductionMonitor(
            plan_path=plan_path,
            log_path=log_path,
            alert_webhook=alert_webhook
        )
    return _monitor
