#!/usr/bin/env python3
"""
RSSHub 健康监控器

功能：
1. 追踪 RSSHub 实例的成功/失败状态
2. 连续失败 3 次 → 触发钉钉告警
3. 提供降级状态查询
4. 支持自动恢复检测

P0 修复 (2026-04-17): 消除 RSSHub 静默失败，增加健康监控和告警
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("ftseek.rsshub_health")


class RSSHubHealthMonitor:
    """RSSHub 实例健康监控"""
    
    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else Path(__file__).parent.parent.parent / "data" / "rsshub_health.json"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 健康状态：{instance_url: {"consecutive_failures": 0, "last_success": timestamp, "last_failure": timestamp}}
        self._state: Dict[str, dict] = {}
        self._alert_threshold = 3  # 连续失败 3 次告警
        self._alerted_instances = set()  # 已告警的实例（避免重复告警）
        
        self._load_state()
        logger.info(f"[RSSHub健康] 监控器已初始化 | 告警阈值={self._alert_threshold}次")

    def _load_state(self):
        """从文件加载状态"""
        try:
            if self.db_path.exists():
                with open(self.db_path, 'r') as f:
                    data = json.load(f)
                self._state = data.get("instances", {})
                self._alerted_instances = set(data.get("alerted", []))
                logger.debug(f"[RSSHub健康] 加载状态: {len(self._state)} 个实例")
        except Exception as e:
            logger.warning(f"[RSSHub健康] 加载状态失败: {e}")
            self._state = {}

    def _save_state(self):
        """保存状态到文件"""
        try:
            with open(self.db_path, 'w') as f:
                json.dump({
                    "instances": self._state,
                    "alerted": list(self._alerted_instances),
                    "updated_at": datetime.now().isoformat()
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[RSSHub健康] 保存状态失败: {e}")

    def record_success(self, instance_url: str):
        """记录成功"""
        now = time.time()
        if instance_url not in self._state:
            self._state[instance_url] = {
                "consecutive_failures": 0,
                "last_success": now,
                "last_failure": 0,
                "total_success": 0,
                "total_failure": 0
            }
        
        self._state[instance_url]["consecutive_failures"] = 0
        self._state[instance_url]["last_success"] = now
        self._state[instance_url]["total_success"] = self._state[instance_url].get("total_success", 0) + 1
        
        # 如果之前告警过，现在恢复了，清除告警状态
        if instance_url in self._alerted_instances:
            self._alerted_instances.discard(instance_url)
            logger.info(f"[RSSHub健康] ✅ 实例恢复: {instance_url}")
        
        self._save_state()

    def record_failure(self, instance_url: str, error: str = "") -> bool:
        """
        记录失败
        
        Returns:
            True 表示触发了告警，需要发送通知
        """
        now = time.time()
        if instance_url not in self._state:
            self._state[instance_url] = {
                "consecutive_failures": 0,
                "last_success": 0,
                "last_failure": 0,
                "total_success": 0,
                "total_failure": 0
            }
        
        self._state[instance_url]["consecutive_failures"] += 1
        self._state[instance_url]["last_failure"] = now
        self._state[instance_url]["total_failure"] = self._state[instance_url].get("total_failure", 0) + 1
        
        failures = self._state[instance_url]["consecutive_failures"]
        should_alert = failures >= self._alert_threshold and instance_url not in self._alerted_instances
        
        if should_alert:
            self._alerted_instances.add(instance_url)
            logger.error(f"[RSSHub健康] 🔴 实例连续失败 {failures} 次，触发告警: {instance_url} | 错误: {error}")
        
        self._save_state()
        return should_alert

    def get_health_summary(self) -> dict:
        """获取健康摘要"""
        total_instances = len(self._state)
        healthy = sum(1 for s in self._state.values() if s["consecutive_failures"] == 0)
        degraded = sum(1 for s in self._state.values() if 0 < s["consecutive_failures"] < self._alert_threshold)
        critical = sum(1 for s in self._state.values() if s["consecutive_failures"] >= self._alert_threshold)
        
        return {
            "total_instances": total_instances,
            "healthy": healthy,
            "degraded": degraded,
            "critical": critical,
            "all_down": (healthy == 0 and total_instances > 0),
            "instances": self._state
        }

    def is_available(self) -> bool:
        """是否至少有一个健康实例"""
        summary = self.get_health_summary()
        return summary["healthy"] > 0

    def is_fully_down(self) -> bool:
        """是否所有实例都挂了"""
        summary = self.get_health_summary()
        return summary["all_down"]


# 全局单例
_monitor: Optional[RSSHubHealthMonitor] = None


def get_rsshub_health_monitor() -> RSSHubHealthMonitor:
    global _monitor
    if _monitor is None:
        _monitor = RSSHubHealthMonitor()
    return _monitor
