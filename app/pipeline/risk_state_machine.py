"""
港股全局风险状态机 (HK Risk State Machine)

职责:
1. 维护全局风险状态 (NORMAL, CAUTION, RISK_OFF, HALT)
2. 提供交易准入检查 (Action Matrix)
3. 记录状态变更审计日志 (Audit Log)
4. 提供自动恢复机制 (Auto Recovery)
5. [港股适配] 启动时检查隔夜风险 (Overnight Risk Check)
"""

import time
import json
import os
import logging
from enum import Enum
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger("ftseek.pipeline.risk_state_machine")

# ============================================================================
#  状态定义
# ============================================================================
class RiskState(str, Enum):
    NORMAL = "NORMAL"       # 正常运行
    CAUTION = "CAUTION"     # 谨慎 (降低仓位，预警 - 港股受隔夜影响大)
    RISK_OFF = "RISK_OFF"   # 避险 (禁止新开仓，允许减仓/平仓)
    HALT = "HALT"           # 停机 (系统级异常，所有交易停止)

class RiskAction(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    REVERSE = "REVERSE"
    CLOSE = "CLOSE"
    REDUCE = "REDUCE"

# 动作允许矩阵
ACTION_MATRIX = {
    RiskState.NORMAL: [RiskAction.OPEN, RiskAction.ADD, RiskAction.REVERSE, RiskAction.CLOSE, RiskAction.REDUCE],
    RiskState.CAUTION: [RiskAction.OPEN, RiskAction.ADD, RiskAction.REVERSE, RiskAction.CLOSE, RiskAction.REDUCE], 
    # CAUTION 不拦截交易，但可在外部逻辑中限制仓位（此处仅作状态标记，或可改为仅允许 CLOSE/REDUCE 视严格程度而定）
    # 为保持灵活性，CAUTION 允许交易，但需配合外部仓位控制
    RiskState.RISK_OFF: [RiskAction.CLOSE, RiskAction.REDUCE], # 禁止开仓、加仓、反手
    RiskState.HALT: [] # 任何操作都不允许
}

class RiskStateMachine:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RiskStateMachine, cls).__new__(cls)
            cls._instance._state = RiskState.NORMAL
            cls._instance._reason = "System Init"
            cls._instance._state_change_time = time.time()
            cls._instance._cooldowns: Dict[str, float] = {}
            
            # Audit log path
            cls._instance._audit_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'data', 'risk_audit.json'
            )
            
            # HK Adaptation: 启动时恢复状态 & 隔夜风险检查
            cls._instance.restore_state() 
        return cls._instance

    def restore_state(self):
        """
        启动时检查审计日志，若近期有严重风险则保持 RISK_OFF
        同时结合港股特性：若隔夜有重大风险事件，默认 CAUTION
        """
        if not os.path.exists(self._audit_path):
            return

        try:
            with open(self._audit_path, 'r') as f:
                logs = json.load(f)
            
            if not logs:
                return

            # 检查最近一条记录
            last_record = logs[-1]
            last_state = last_record.get("new_state")
            last_time_str = last_record.get("time")
            
            # 解析时间
            last_time = datetime.fromisoformat(last_time_str)
            now = datetime.now()
            
            # 如果 4 小时内发生了 RISK_OFF 或 HALT
            if (now - last_time).total_seconds() < 14400: # 4 hours
                if last_state in [RiskState.RISK_OFF.value, RiskState.HALT.value]:
                    logger.warning(f"🚨 状态恢复: 检测到近期风险事件 ({last_record.get('reason', 'Unknown')})，保持 {last_state}")
                    self._state = RiskState(last_state)
                    self._reason = f"Restored from Audit: {last_record.get('reason', 'Unknown')}"
                    self._state_change_time = last_time.timestamp()
                    return # Don't downgrade to CAUTION if we were already RISK_OFF

            # 如果之前是 CAUTION，且超过 12 小时，尝试恢复 NORMAL
            # 否则保持 CAUTION (港股隔夜风险持续)
            if last_state == RiskState.CAUTION.value and (now - last_time).total_seconds() > 43200:
                 logger.info("✅ 状态恢复: 隔夜谨慎期已过，恢复 NORMAL")
                 self._state = RiskState.NORMAL
            else:
                 # 默认保持 NORMAL 或恢复原有状态 (如果是 CAUTION)
                 if last_state == RiskState.CAUTION.value:
                     logger.warning("⚠️ 状态恢复: 保持 CAUTION (隔夜风险影响)")
                     self._state = RiskState.CAUTION

        except Exception as e:
            logger.error(f"状态恢复失败: {e}")

    def get_state(self) -> RiskState:
        """获取当前状态"""
        return self._state

    def set_state(self, state: RiskState, reason: str = "", source: str = "System"):
        """切换状态"""
        old_state = self._state
        if old_state == state:
            return # 状态未变，不重复记录

        self._state = state
        self._reason = reason
        self._state_change_time = time.time()
        
        logger.warning(f"🚨 状态变更: {old_state.value} -> {state.value}")
        logger.warning(f"   原因: {reason}")
        logger.warning(f"   来源: {source}")

        # 记录审计日志
        self._audit_log(old_state.value, state.value, reason, source)

    def is_trade_allowed(self, action: RiskAction) -> bool:
        """检查动作是否允许"""
        allowed_actions = ACTION_MATRIX.get(self._state, [])
        
        if action in allowed_actions:
            return True
        else:
            logger.warning(f"⛔ 交易拦截: {action.value} | 当前状态: {self._state.value}")
            return False

    def is_in_cooldown(self, key: str, duration: int = 1800) -> bool:
        """防抖检查"""
        now = time.time()
        last_seen = self._cooldowns.get(key, 0)
        if now - last_seen < duration:
            return True
        self._cooldowns[key] = now
        return False

    def auto_recovery(self):
        """自动恢复机制"""
        if self._state == RiskState.RISK_OFF:
            # 30 分钟无新增高危事件，尝试恢复
            if time.time() - self._state_change_time > 1800:
                self.set_state(RiskState.CAUTION, "Auto Recovery: Cooldown passed", "System")
                logger.info("✅ 状态自动恢复至 CAUTION (观察中)")

    def _audit_log(self, old_state: str, new_state: str, reason: str, source: str):
        """写入审计日志"""
        record = {
            "time": datetime.now().isoformat(),
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason,
            "source": source
        }
        
        logs = []
        if os.path.exists(self._audit_path):
            try:
                with open(self._audit_path, 'r') as f:
                    logs = json.load(f)
            except json.JSONDecodeError as e:
                logger.warning(f"[风险状态机] 审计日志解析失败: {self._audit_path} → {e}")
            except Exception as e:
                logger.error(f"[风险状态机] 读取审计日志失败: {e}")
        
        logs.append(record)
        logs = logs[-100:] # 保留最近 100 条
        
        try:
            with open(self._audit_path, 'w') as f:
                json.dump(logs, f, indent=2)
        except Exception as e:
            logger.error(f"写入审计日志失败: {e}")
