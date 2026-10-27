"""
全局风险状态机 (Global Risk State Machine) - V2

职责:
1. 维护全局风险状态 (NORMAL, CAUTION, RISK_OFF, HALT)
2. 提供交易准入检查 (Action Matrix)
3. 记录状态变更审计日志 (Audit Log)
4. 提供自动恢复机制 (Auto Recovery)
"""

import time
import json
import os
import logging
import fcntl
import threading
import tempfile
from enum import Enum
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger("ftseek.us_pipeline.risk_state_machine")

# ============================================================================
#  状态定义
# ============================================================================
class RiskState(str, Enum):
    NORMAL = "NORMAL"       # 正常运行
    CAUTION = "CAUTION"     # 谨慎 (降低仓位，预警)
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
    RiskState.CAUTION: [RiskAction.OPEN, RiskAction.ADD, RiskAction.REVERSE, RiskAction.CLOSE, RiskAction.REDUCE], # 仅记录警告，不拦截
    RiskState.RISK_OFF: [RiskAction.CLOSE, RiskAction.REDUCE], # 禁止开仓、加仓、反手
    RiskState.HALT: [] # 任何操作都不允许
}

class RiskStateMachine:
    _instance = None
    _init_lock = threading.Lock()  # P0-3a 修复 (2026-05-03): 防止多实例竞争
    
    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                # 双重检查: 可能在等待锁期间其他线程已初始化
                if cls._instance is None:
                    cls._instance = super(RiskStateMachine, cls).__new__(cls)
                    cls._instance._state = RiskState.NORMAL
                    cls._instance._reason = "System Init"
                    cls._instance._state_change_time = time.time()
                    cls._instance._cooldowns: Dict[str, float] = {}
                    cls._instance._audit_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'data', 'risk_audit.json'
                    )
                    cls._instance.restore_state()
        return cls._instance

    def restore_state(self):
        """启动时检查审计日志，若近期有严重风险则保持 RISK_OFF
        
        P0 修复 (2026-06-05): 从最后一条 RISK_OFF 向前扫描，
        若存在手动/系统 NORMAL 重置则跳过恢复，避免已确认事件反复触发。
        """
        if not os.path.exists(self._audit_path):
            return

        try:
            with open(self._audit_path, 'r') as f:
                logs = json.load(f)
            
            if not logs:
                return

            now = datetime.now()

            # 从末尾向前找最近的 RISK_OFF/HALT 记录
            risk_off_record = None
            risk_off_idx = -1
            for i in range(len(logs) - 1, -1, -1):
                rec = logs[i]
                if rec.get("new_state") in [RiskState.RISK_OFF.value, RiskState.HALT.value]:
                    risk_off_record = rec
                    risk_off_idx = i
                    break

            if not risk_off_record:
                return  # 无 RISK_OFF 记录，无需恢复

            last_time = datetime.fromisoformat(risk_off_record.get("time"))
            if (now - last_time).total_seconds() >= 14400:
                return  # 超过 4 小时，忽略

            # P0: 检查 RISK_OFF 之后是否有手动/自动 NORMAL 重置
            # 如果有，说明事件已被确认或冷却恢复，不应再恢复 RISK_OFF
            for j in range(risk_off_idx + 1, len(logs)):
                rec = logs[j]
                if rec.get("new_state") == "NORMAL":
                    source = rec.get("source", "")
                    reason = rec.get("reason", "")
                    logger.info(
                        f"✅ 状态恢复跳过: RISK_OFF 事件后已有 {source} 重置 ({reason})"
                    )
                    return

            # 无重置记录，确实需要恢复 RISK_OFF
            logger.warning(
                f"🚨 状态恢复: 检测到近期风险事件 "
                f"({risk_off_record.get('reason', 'Unknown')})，保持 {risk_off_record['new_state']}"
            )
            self._state = RiskState(risk_off_record["new_state"])
            self._reason = f"Restored from Audit: {risk_off_record.get('reason', 'Unknown')}"
            self._state_change_time = last_time.timestamp()
                    
        except Exception as e:
            logger.error(f"状态恢复失败: {e}")

    def get_state(self) -> RiskState:
        """获取当前状态"""
        return self._state

    @staticmethod
    def _get_running_data_router():
        """获取运行中的 MarketDataRouter 全局实例，避免重新创建 (P1 修复 2026-05-07)"""
        try:
            from app.core.di_container import DIContainer
            container = DIContainer()
            return container.data_provider()
        except Exception:
            return None

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

        # P0-3 (2026-05-14): 关键状态变更走 CriticalNotifier (5x 重试 + 多通道 + flag 兜底)
        critical_states = (RiskState.RISK_OFF, RiskState.HALT)
        if state in critical_states:
            try:
                from app.utils.critical_alerts import send_critical
                send_critical(
                    f"🚨 风险状态切换: {old_state.value} → {state.value}\n"
                    f"原因: {reason}\n"
                    f"来源: {source}",
                    async_mode=True,
                )
            except Exception as e:
                logger.error(f"[RiskStateMachine] CriticalNotifier 调用失败: {e}")

    def is_trade_allowed(self, action: RiskAction) -> bool:
        """检查动作是否允许"""
        allowed_actions = ACTION_MATRIX.get(self._state, [])
        
        if action in allowed_actions:
            return True
        else:
            logger.warning(f"⛔ 交易拦截: {action.value} | 当前状态: {self._state.value}")
            return False

    def is_in_cooldown(self, key: str, duration: int = 1800) -> bool:
        """
        防抖检查
        Args:
            key: 防抖键 (如 symbol, topic)
            duration: 冷却时间 (秒)
        """
        now = time.time()
        last_seen = self._cooldowns.get(key, 0)
        if now - last_seen < duration:
            return True
        self._cooldowns[key] = now
        return False

    def auto_recovery(self):
        """
        自动恢复机制 (P2-3 修复: 增加市场状态二次确认)
        
        检查是否满足恢复条件:
        1. 距离上次状态变更超过 30 分钟
        2. VIX 回落到可接受水平 (<= 30)
        3. 大盘未继续下跌 (SPY 变化 >= -2%)
        """
        if self._state == RiskState.RISK_OFF:
            elapsed = time.time() - self._state_change_time
            if elapsed <= 1800:  # 30 分钟内不恢复
                return
            
            # P2-3: 市场状态二次确认
            confirmed, detail = self._confirm_market_recovery()
            if confirmed:
                self.set_state(
                    RiskState.NORMAL,
                    f"Auto Recovery: Cooldown + Market Confirmed ({detail})",
                    "System"
                )
                logger.info(f"✅ 状态自动恢复至 NORMAL (市场确认: {detail})")
            else:
                # 每 10 分钟打一次日志，避免日志风暴
                if elapsed % 600 < 60:
                    logger.info(
                        f"⏳ 冷却时间已过 ({elapsed/60:.0f}min)，"
                        f"但市场状态未确认恢复: {detail}"
                    )
                # P2-2 修复 2026-04-26: fail-open 策略化配置，防止“清仓→恢复→再清仓”抖动
                # FORCE_RECOVERY_HOURS (默认 2h) 初始强制恢复时间
                # FORCE_RECOVERY_MAX_HOURS (默认 6h) 渐进延长上限
                # 若 _confirm_market_recovery 持续拒绝，每次延长 50%，直至上限
                force_hours = float(os.getenv("RSM_FORCE_RECOVERY_HOURS", "2"))
                max_hours = float(os.getenv("RSM_FORCE_RECOVERY_MAX_HOURS", "6"))
                rejection_count = getattr(self, '_force_reject_count', 0)
                effective_hours = min(force_hours * (1.5 ** rejection_count), max_hours)
                if elapsed > effective_hours * 3600:
                    self._force_reject_count = 0  # 恢复后重置
                    self.set_state(
                        RiskState.NORMAL,
                        f"Auto Recovery: Force after {effective_hours:.1f}h "
                        f"(market unconfirmed: {detail}, rejects={rejection_count})",
                        "System"
                    )
                    logger.warning(
                        f"⚠️ 强制恢复至 NORMAL ({effective_hours:.1f}小时超时，"
                        f"市场未确认: {detail}，历史拒绝次数={rejection_count})"
                    )
                else:
                    # 未达到强制恢复时间，记录拒绝次数以供下次渐进延长
                    if not hasattr(self, '_force_reject_count'):
                        self._force_reject_count = 0
                    # 每 30 分钟计数一次，避免每次 auto_recovery 调用都累加
                    if elapsed % 1800 < 60:
                        self._force_reject_count += 1

    def _confirm_market_recovery(self) -> tuple:
        """
        P2-3: 市场状态二次确认
        
        检查 VIX 和 SPY 状态，确认市场已从恐慌中恢复。
        
        P1 修复 (2026-05-07): 不再创建无参数 MarketDataRouter (触发 Futu 重连 + 数据源丢失ERROR)。
        改为通过 scheduler_core 的全局实例获取，或直接降级到 Yahoo/CSV。
        
        Returns:
            (confirmed: bool, detail: str)
        """
        vix = None
        spy_change = None
        checks = []
        
        # 1. VIX 检查 — 优先从运行中的 scheduler_core 复用 data_router
        router = self._get_running_data_router()
        if router:
            try:
                vix_data = router.get_vix()
                if vix_data and float(vix_data) > 0:
                    vix = float(vix_data)
            except Exception:
                pass
        
        # 降级: VIXMonitor (FRED→AlphaVantage→Finnhub VIXY→Yahoo REST)
        # 2026-10-27: 原走 YahooFinanceProvider().get_vix(), 该方法只是纯转发到
        #   VIXMonitor().get_vix() ⇒ 直连, 行为逐字不变, 去掉 yfinance 依赖。
        if vix is None:
            try:
                from app.risk.vix_monitor import VIXMonitor
                vix = VIXMonitor().get_vix()
                if vix is None:
                    # 保留原 provider 的降级留痕 (键名/文案逐字不变, 看板口径不漂)
                    try:
                        from app.core.degradation_monitor import get_degradation_monitor
                        get_degradation_monitor().record(
                            "yahoo_vix_fallback", "VIXMonitor returned None")
                    except Exception:
                        pass
            except Exception:
                pass
        
        if vix is not None:
            if vix > 30:
                checks.append(f"VIX={vix:.1f}>30")
                return False, f"VIX={vix:.1f} 市场恐慌未缓解"
            checks.append(f"VIX={vix:.1f}✓")
        
        # 2. SPY 走势检查 — 同样复用已有实例
        if router:
            try:
                spy_quote = router.get_quote("US.SPY")
                if spy_quote:
                    change_pct = spy_quote.get("change_percent", 0)
                    if isinstance(change_pct, (int, float)):
                        if abs(change_pct) > 1:
                            spy_change = change_pct / 100.0
                        else:
                            spy_change = change_pct
                        if abs(spy_change) > 0.15:
                            checks.append(f"SPY={spy_change:.2%}(异常值，忽略)")
                            spy_change = None
            except Exception:
                pass
        
        if spy_change is not None:
            if spy_change < -0.05:
                checks.append(f"SPY={spy_change:.2%}")
                return False, f"SPY={spy_change:.2%} 大盘暴跌"
            checks.append(f"SPY={spy_change:.2%}✓")
        
        # 3. 结果判断
        if checks:
            return True, " ".join(checks)
        
        # P1 修复 (2026-05-01): 无法获取市场数据时 fail-open
        # 之前返回 (False, "无法获取市场数据") 会导致 RISK_OFF 永远无法恢复，
        # 因为 API 可能只是暂时不可用，不是市场真的有问题。
        # 改为允许恢复 + 日志提醒。
        logger.warning("[RiskStateMachine] _confirm_market_recovery: 无法获取市场数据，fail-open 允许恢复")
        return True, "市场数据不可用，fail-open 恢复"

    def _audit_log(self, old_state: str, new_state: str, reason: str, source: str):
        """写入审计日志 (P1-2 修复: 文件锁 + 原子写入)"""
        record = {
            "time": datetime.now().isoformat(),
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason,
            "source": source
        }
        
        lock_path = self._audit_path + ".lock"
        try:
            # 使用文件锁防止多进程并发写入
            with open(lock_path, 'w') as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    logs = []
                    if os.path.exists(self._audit_path):
                        try:
                            with open(self._audit_path, 'r') as f:
                                logs = json.load(f)
                        except (json.JSONDecodeError, IOError):
                            logs = []
                    
                    logs.append(record)
                    logs = logs[-100:]  # 只保留最近 100 条
                    
                    # 原子写入: 先写临时文件，然后 rename
                    dir_name = os.path.dirname(self._audit_path)
                    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
                    try:
                        with os.fdopen(fd, 'w') as tmp_f:
                            json.dump(logs, tmp_f, indent=2)
                        os.replace(tmp_path, self._audit_path)
                    except Exception:
                        # 清理临时文件
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                        raise
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"写入审计日志失败: {e}")


# ============================================================================
#  恢复观察窗口 (P1-1 新增)
# ============================================================================

class RecoveryWindow:
    """
    恢复观察窗口 (P1-1 新增)

    修复后不能立刻判定恢复成功，需要连续观察若干轮。
    避免 "修复 → 立刻抖回异常 → 再修复" 的振荡循环。

    使用方式:
        window = RecoveryWindow(required_consecutive_ok=5, check_interval=60)
        # 每轮健康检查后:
        if is_healthy:
            window.record_ok()
        else:
            window.record_fail()

        if window.is_recovered():
            # 连续 N 轮正常，确认恢复
            state_machine.set_state(RiskState.NORMAL, ...)
    """

    def __init__(self, required_consecutive_ok: int = 5, max_history: int = 20):
        self.required = required_consecutive_ok
        self.max_history = max_history
        self._history: List[Dict[str, Any]] = []  # [{ok: bool, time: float, detail: str}]

    def record_ok(self, detail: str = "") -> None:
        """记录一次正常检查"""
        self._history.append({"ok": True, "time": time.time(), "detail": detail})
        self._history = self._history[-self.max_history:]

    def record_fail(self, reason: str = "") -> None:
        """记录一次异常检查 (重置连续计数)"""
        self._history.append({"ok": False, "time": time.time(), "reason": reason})
        self._history = self._history[-self.max_history:]

    def is_recovered(self) -> bool:
        """
        是否满足恢复条件 (连续 N 轮正常)
        """
        if len(self._history) < self.required:
            return False
        recent = self._history[-self.required:]
        return all(r["ok"] for r in recent)

    def progress(self) -> Dict[str, Any]:
        """获取当前进度"""
        if not self._history:
            return {"status": "not_started", "consecutive_ok": 0, "required": self.required}

        consecutive_ok = 0
        for r in reversed(self._history):
            if r["ok"]:
                consecutive_ok += 1
            else:
                break

        return {
            "status": "recovered" if consecutive_ok >= self.required else "observing",
            "consecutive_ok": consecutive_ok,
            "required": self.required,
            "total_checks": len(self._history),
            "last_check": self._history[-1],
        }

    def reset(self) -> None:
        """重置观察窗口"""
        self._history.clear()


# ============================================================================
#  异常冷却器 (P1-2 新增)
# ============================================================================

class ExceptionCooler:
    """
    异常冷却器 (P1-2 新增)

    同一类异常在短期内只允许触发一次修复动作，避免修复风暴。
    连续失败自动升级处理级别。

    冷却级别:
        Level 0: 首次异常，正常修复
        Level 1: 短时间内第 2 次同类异常，延长冷却
        Level 2: 第 3 次，标记 CAUTION 状态
        Level 3+: 冻结高风险交易，建议人工介入
    """

    def __init__(self, base_cooldown: int = 300, escalation_threshold: int = 3):
        self.base_cooldown = base_cooldown       # 基础冷却时间 (秒)
        self.escalation_threshold = escalation_threshold  # 升级阈值
        self._exceptions: Dict[str, Dict[str, Any]] = {}  # key -> {count, last_time, level, frozen}

    def _make_key(self, exception_type: str, symbol: str = "") -> str:
        return f"{exception_type}:{symbol}" if symbol else exception_type

    def check_and_record(self, exception_type: str, symbol: str = "") -> Dict[str, Any]:
        """
        检查并记录异常

        Returns:
            {
                "allow_repair": bool,    # 是否允许执行修复
                "level": int,            # 当前冷却级别
                "count": int,            # 同类异常累计次数
                "cooldown_remaining": int, # 剩余冷却秒数
                "recommendation": str,   # 建议操作
            }
        """
        key = self._make_key(exception_type, symbol)
        now = time.time()

        if key not in self._exceptions:
            self._exceptions[key] = {
                "count": 0,
                "last_time": 0,
                "level": 0,
                "frozen": False,
            }

        info = self._exceptions[key]

        # 计算冷却时间 (指数退避)
        cooldown = self.base_cooldown * (2 ** info["level"])

        # 检查是否在冷却中
        elapsed = now - info["last_time"]
        if elapsed < cooldown and info["last_time"] > 0:
            return {
                "allow_repair": False,
                "level": info["level"],
                "count": info["count"],
                "cooldown_remaining": int(cooldown - elapsed),
                "recommendation": f"冷却中 (剩余 {int(cooldown - elapsed)}s)",
            }

        # 允许修复，更新计数
        info["count"] += 1
        info["last_time"] = now

        # 升级逻辑
        if info["count"] >= self.escalation_threshold:
            info["level"] = min(info["count"] - self.escalation_threshold + 1, 3)
            if info["level"] >= 3:
                info["frozen"] = True

        return {
            "allow_repair": True,
            "level": info["level"],
            "count": info["count"],
            "cooldown_remaining": 0,
            "recommendation": self._get_recommendation(info["level"], info["count"]),
        }

    def is_frozen(self, exception_type: str, symbol: str = "") -> bool:
        """检查指定异常类型是否被冻结"""
        key = self._make_key(exception_type, symbol)
        if key not in self._exceptions:
            return False
        return self._exceptions[key].get("frozen", False)

    def get_frozen_types(self) -> List[str]:
        """获取所有被冻结的异常类型"""
        return [k for k, v in self._exceptions.items() if v.get("frozen", False)]

    def unfreeze(self, exception_type: str, symbol: str = "") -> None:
        """手动解冻"""
        key = self._make_key(exception_type, symbol)
        if key in self._exceptions:
            self._exceptions[key]["frozen"] = False
            self._exceptions[key]["level"] = 0
            self._exceptions[key]["count"] = 0
            logger.info(f"[ExceptionCooler] 已解冻 {key}")

    def cleanup(self, max_age: int = 3600) -> int:
        """清理超过指定时间的记录"""
        now = time.time()
        expired = [k for k, v in self._exceptions.items() if now - v["last_time"] > max_age]
        for k in expired:
            del self._exceptions[k]
        return len(expired)

    def _get_recommendation(self, level: int, count: int) -> str:
        if level == 0:
            return "正常修复"
        elif level == 1:
            return "重复异常，建议检查根因"
        elif level == 2:
            return "频繁异常，建议降低仓位"
        else:
            return f"异常风暴 ({count}次)，冻结高风险交易，建议人工介入"
