#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策缓冲层 (Decision Buffer) - FTSeek v4.0

功能：
1. 解耦共识引擎与执行层，实现"策略只输出想法，风险决定能不能做，执行决定怎么做"
2. 信号去重过滤（同标的同方向冷却期检查）
3. 风控前置检查（仓位限制、熔断器、账户风险）
4. 优先级排队（高置信度信号优先执行）
5. 决策审计日志（记录每个信号的决策过程）

架构位置：
  信号 → 共识引擎 → [决策缓冲层] → 风控检查 → 执行路由 → 成交回报
                        ↑
                   本模块所在位置
"""

import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

from app.strategy.consensus_engine import ConsensusResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.decision_buffer")
logger.propagate = False  # 防止日志重复


# ============================================================
#  决策状态枚举
# ============================================================
class DecisionStatus(str, Enum):
    """决策状态"""
    PENDING = "PENDING"           # 待处理
    APPROVED = "APPROVED"         # 已批准（通过风控）
    REJECTED = "REJECTED"         # 已拒绝（未通过风控）
    EXECUTING = "EXECUTING"       # 执行中
    COMPLETED = "COMPLETED"       # 已完成
    FAILED = "FAILED"             # 执行失败


# ============================================================
#  决策记录数据类
# ============================================================
@dataclass
class DecisionRecord:
    """决策记录（用于审计和回溯）"""
    decision_id: str
    code: str
    action: SignalAction
    consensus_result: ConsensusResult
    status: DecisionStatus = DecisionStatus.PENDING
    rejection_reason: str = ""
    created_at: float = field(default_factory=time.time)
    approved_at: Optional[float] = None
    executed_at: Optional[float] = None
    completed_at: Optional[float] = None
    execution_time_ms: Optional[float] = None  # 执行耗时（毫秒）

    def to_dict(self) -> Dict:
        """转换为字典（用于日志和 API）"""
        return {
            "decision_id": self.decision_id,
            "code": self.code,
            "action": self.action.value,
            "score": self.consensus_result.score,
            "confidence": self.consensus_result.confidence,
            "status": self.status.value,
            "rejection_reason": self.rejection_reason,
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(),
            "execution_time_ms": self.execution_time_ms,
        }


# ============================================================
#  决策缓冲层主类
# ============================================================
class DecisionBuffer:
    """
    决策缓冲层

    核心职责：
    1. 接收共识引擎的信号
    2. 执行去重检查（同标的同方向冷却期）
    3. 调用风控检查（仓位、熔断、账户风险）
    4. 优先级排队（高置信度优先）
    5. 输出决策结果给执行层

    2026-04-09 优化:
    - 增加持仓数量前置检查
    - 已持仓股票生成平仓信号优先
    - 动态仓位模式：模拟交易时 max_positions = 股票池数量，充分验证加仓/平仓
    """

    def __init__(
        self,
        signal_cooldown: int = 10,         # 信号冷却期（秒）
        max_pending: int = 10,             # 最大待处理队列长度
        min_confidence: float = 0.35,      # 最低置信度阈值
        max_positions: Optional[int] = None,  # 最大持仓数量（None=根据股票池动态计算）
        dynamic_position_mode: bool = True,   # 动态仓位模式
    ):
        self.signal_cooldown = signal_cooldown
        self.max_pending = max_pending
        self.min_confidence = min_confidence

        # 2026-04-09 动态仓位模式：
        # - 模拟交易 (TRADE_ENV=1): max_positions = 股票池数量，充分验证加仓/平仓
        # - 实盘交易 (TRADE_ENV=0): max_positions = 配置值或股票池的 50%
        self.dynamic_position_mode = dynamic_position_mode
        if max_positions is not None:
            # 显式指定值，优先使用
            self.max_positions = max_positions
        elif dynamic_position_mode:
            # 动态计算：根据股票池大小
            self.max_positions = self._calculate_dynamic_max_positions()
        else:
            # 回退到默认值
            self.max_positions = 4

        # 决策队列（按置信度排序）
        self._pending_queue: List[DecisionRecord] = []
        self._queue_lock = threading.Lock()

        # 信号冷却记录：code -> (action, timestamp)
        self._signal_cooldowns: Dict[str, Tuple[str, float]] = {}
        self._cooldown_lock = threading.Lock()

        # 决策历史记录（最近 100 条）
        self._decision_history: deque = deque(maxlen=100)

        # 统计信息
        self._stats = {
            "total_received": 0,
            "total_approved": 0,
            "total_rejected": 0,
            "rejection_reasons": {},
        }
        self._stats_lock = threading.Lock()

        logger.info(
            f"决策缓冲层已初始化：冷却期={signal_cooldown}s, "
            f"最大队列={max_pending}, 最低置信度={min_confidence}, "
            f"max_positions={self.max_positions} (dynamic={dynamic_position_mode})"
        )

    def _calculate_dynamic_max_positions(self) -> int:
        """
        动态计算最大持仓数量

        规则：
        1. 从 config.py 获取股票池列表
        2. 模拟交易 (TRADE_ENV=1): max_positions = 股票池数量
        3. 实盘交易 (TRADE_ENV=0): max_positions = min(股票池数量 × 50%, 配置值)

        Returns:
            动态计算的最大持仓数
        """
        try:
            # 导入配置
            from app.config import WATCH_LIST, TRADE_ENV, MAX_OPEN_POSITIONS

            watchlist_size = len(WATCH_LIST)

            if TRADE_ENV == 1:
                # 模拟交易：允许满仓测试，充分验证加仓/平仓功能
                dynamic_max = watchlist_size
                logger.info(
                    f"[动态仓位] 模拟交易模式：max_positions = {watchlist_size} (股票池数量)"
                )
            else:
                # 实盘交易：保守策略，不超过股票池的 50% 或配置值
                dynamic_max = min(
                    max(1, int(watchlist_size * 0.5)),  # 股票池的 50%
                    MAX_OPEN_POSITIONS  # 配置上限
                )
                logger.info(
                    f"[动态仓位] 实盘交易模式：max_positions = {dynamic_max} "
                    f"(股票池={watchlist_size}×50%, 配置上限={MAX_OPEN_POSITIONS})"
                )

            return dynamic_max

        except ImportError as e:
            logger.warning(f"[动态仓位] 无法导入配置，使用默认值 4: {e}")
            return 4
        except Exception as e:
            logger.warning(f"[动态仓位] 计算失败，使用默认值 4: {e}")
            return 4

    def process_signal(self, consensus_result: ConsensusResult) -> DecisionRecord:
        """
        处理共识信号（主入口）

        Args:
            consensus_result: 共识引擎输出结果

        Returns:
            DecisionRecord: 决策记录
        """
        code = consensus_result.code
        action = consensus_result.action
        confidence = consensus_result.confidence

        # 生成决策 ID
        decision_id = f"{code}_{action.value}_{int(time.time())}"

        # 创建决策记录
        record = DecisionRecord(
            decision_id=decision_id,
            code=code,
            action=action,
            consensus_result=consensus_result,
        )

        # 更新统计
        with self._stats_lock:
            self._stats["total_received"] += 1

        logger.info(
            f"[决策缓冲] 收到信号：{code} {action.value} | "
            f"分数={consensus_result.score:+.2f} | 置信度={confidence:.2f}"
        )

        # ── 第 1 步：基础检查 ─────────────────────────────────────
        rejection_reason = self._check_basic(record)
        if rejection_reason:
            return self._reject(record, rejection_reason)

        # ── 第 1.5 步：持仓状态检查 (2026-04-09 新增) ───────────────
        rejection_reason = self._check_position_status(record)
        if rejection_reason:
            return self._reject(record, rejection_reason)

        # ── 第 2 步：信号去重检查 ──────────────────────────────────
        rejection_reason = self._check_duplicate(record)
        if rejection_reason:
            return self._reject(record, rejection_reason)

        # ── 第 3 步：置信度检查 ────────────────────────────────────
        if confidence < self.min_confidence:
            return self._reject(
                record,
                f"置信度过低：{confidence:.2f} < {self.min_confidence}"
            )

        # ── 第 4 步：加入待处理队列 ────────────────────────────────
        with self._queue_lock:
            if len(self._pending_queue) >= self.max_pending:
                # 队列已满，按置信度排序，移除最低的
                self._pending_queue.sort(key=lambda r: r.consensus_result.confidence)
                removed = self._pending_queue.pop(0)  # 移除置信度最低的
                self._reject(removed, "队列已满，被更高优先级信号替换")
                logger.warning(
                    f"[决策缓冲] 队列已满，移除低优先级信号："
                    f"{removed.code} {removed.action.value}"
                )

            # 插入新信号（按置信度降序）
            self._pending_queue.append(record)
            self._pending_queue.sort(
                key=lambda r: r.consensus_result.confidence,
                reverse=True
            )

            # 队列使用率告警
            usage_rate = len(self._pending_queue) / self.max_pending
            if usage_rate >= 0.8:
                logger.warning(
                    f"[决策缓冲] ⚠️ 队列拥堵告警：{len(self._pending_queue)}/{self.max_pending} "
                    f"({usage_rate:.0%})"
                )

        # 标记为已批准
        record.status = DecisionStatus.APPROVED
        record.approved_at = time.time()

        # 记录冷却期
        with self._cooldown_lock:
            self._signal_cooldowns[code] = (action.value, time.time())

        # 更新统计
        with self._stats_lock:
            self._stats["total_approved"] += 1

        # 添加到历史
        self._decision_history.append(record)

        logger.info(
            f"[决策缓冲] ✅ 批准信号：{code} {action.value} | "
            f"置信度={confidence:.2f} | 队列长度={len(self._pending_queue)}"
        )

        return record

    def get_next_signal(self) -> Optional[DecisionRecord]:
        """
        获取下一个待执行信号（按优先级）

        Returns:
            DecisionRecord 或 None
        """
        with self._queue_lock:
            if not self._pending_queue:
                return None

            # 取出置信度最高的信号
            record = self._pending_queue.pop(0)
            record.status = DecisionStatus.EXECUTING
            record.executed_at = time.time()

            logger.info(
                f"[决策缓冲] 取出信号执行：{record.code} {record.action.value} | "
                f"置信度={record.consensus_result.confidence:.2f}"
            )

            return record

    def complete_signal(self, decision_id: str, success: bool, execution_time_ms: float = 0):
        """
        标记信号执行完成

        Args:
            decision_id: 决策 ID
            success: 是否执行成功
            execution_time_ms: 执行耗时（毫秒）
        """
        # 在历史中查找
        for record in self._decision_history:
            if record.decision_id == decision_id:
                record.status = DecisionStatus.COMPLETED if success else DecisionStatus.FAILED
                record.completed_at = time.time()
                record.execution_time_ms = execution_time_ms

                logger.info(
                    f"[决策缓冲] 信号执行{'成功' if success else '失败'}: "
                    f"{record.code} {record.action.value} | "
                    f"耗时={execution_time_ms:.0f}ms"
                )
                return

        logger.warning(f"[决策缓冲] 未找到决策记录：{decision_id}")

    def _check_basic(self, record: DecisionRecord) -> Optional[str]:
        """
        基础检查

        Returns:
            拒绝原因 或 None
        """
        code = record.code
        action = record.action

        # 检查股票代码有效性
        if not code or not isinstance(code, str):
            return f"无效股票代码：{code}"

        # 检查动作有效性
        if action not in [SignalAction.BUY, SignalAction.SELL]:
            return f"无效交易动作：{action}"

        return None

    def _check_position_status(self, record: DecisionRecord) -> Optional[str]:
        """
        持仓状态检查 (2026-04-09 新增)

        检查内容:
        1. 当前持仓数量是否已达上限
        2. 是否已有同标的持仓（区分加仓和开仓）

        Returns:
            拒绝原因 或 None
        """
        code = record.code
        action = record.action

        try:
            # 导入持仓管理器
            from app.trader.position_manager import PositionManager
            pm = PositionManager()

            # 获取当前持仓
            positions = pm.get_all_positions()
            open_positions = [p for p in positions if p.code != code]
            current_position = pm.get_position(code)

            # 检查 1: 开仓信号时，检查持仓数量上限
            if action == SignalAction.BUY and current_position is None:
                if len(positions) >= self.max_positions:
                    return (
                        f"已达最大持仓数 ({len(positions)}/{self.max_positions})，拒绝开仓"
                    )

            # 检查 2: 已有持仓时，BUY 信号应转为加仓逻辑（交给执行层处理）
            # 这里只做记录，不拒绝
            if current_position is not None and action == SignalAction.BUY:
                logger.info(
                    f"[决策缓冲] {code} 已有持仓，BUY 信号将转为加仓/平仓逻辑"
                )
                # 不拒绝，让执行层处理

            # 检查 3: 无持仓时，SELL 信号需要拒绝（除非是做空开仓）
            if current_position is None and action == SignalAction.SELL:
                # 如果是做空开仓，检查持仓上限
                if len(positions) >= self.max_positions:
                    return (
                        f"已达最大持仓数 ({len(positions)}/{self.max_positions})，拒绝做空开仓"
                    )
                # 否则允许做空开仓

        except Exception as e:
            logger.warning(f"[决策缓冲] 持仓状态检查异常：{e}")
            # 安全第一：持仓状态无法确认时，拒绝信号，避免超出持仓上限
            return f"持仓状态检查异常，安全拒绝：{e}"

        return None

    def _check_duplicate(self, record: DecisionRecord) -> Optional[str]:
        """
        信号去重检查（2026-04-09 优化：冷却期已移至风控 Pipeline）

        此处只做"极短时间内（10s）同标的同策略同方向"去重，
        冷却期（120s）由 Pipeline 的 CooldownCheck 统一管理。

        Returns:
            拒绝原因 或 None
        """
        code = record.code
        action = record.action.value

        with self._cooldown_lock:
            if code in self._signal_cooldowns:
                last_action, last_ts = self._signal_cooldowns[code]

                # 同标的同方向，10 秒内视为重复（防止极短时间重复信号）
                if last_action == action:
                    elapsed = time.time() - last_ts
                    if elapsed < 10:  # 从 120s 改为 10s，只防极短时间重复
                        remaining = 10 - elapsed
                        return (
                            f"极短时间重复信号：{code} {action} | "
                            f"上次触发={elapsed:.0f}秒前 | "
                            f"剩余={remaining:.0f}秒"
                        )

        return None

    def _reject(self, record: DecisionRecord, reason: str) -> DecisionRecord:
        """拒绝信号"""
        record.status = DecisionStatus.REJECTED
        record.rejection_reason = reason

        # 更新统计
        with self._stats_lock:
            self._stats["total_rejected"] += 1

            # 统计拒绝原因
            reason_category = reason.split(":")[0]  # 提取原因类别
            self._stats["rejection_reasons"][reason_category] = \
                self._stats["rejection_reasons"].get(reason_category, 0) + 1

        # 添加到历史
        self._decision_history.append(record)

        logger.warning(
            f"[决策缓冲] ❌ 拒绝信号：{record.code} {record.action.value} | "
            f"原因：{reason}"
        )

        return record

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._stats_lock:
            stats = self._stats.copy()
            stats["pending_count"] = len(self._pending_queue)
            stats["history_count"] = len(self._decision_history)

            # 计算批准率
            total = stats["total_received"]
            if total > 0:
                stats["approval_rate"] = round(
                    stats["total_approved"] / total, 3
                )
            else:
                stats["approval_rate"] = 0.0

        return stats

    def get_pending_signals(self) -> List[Dict]:
        """获取待处理信号列表"""
        with self._queue_lock:
            return [record.to_dict() for record in self._pending_queue]

    def get_recent_decisions(self, limit: int = 20) -> List[Dict]:
        """获取最近决策记录"""
        recent = list(self._decision_history)[-limit:]
        return [record.to_dict() for record in reversed(recent)]

    def clear_queue(self):
        """清空待处理队列（紧急情况下使用）"""
        with self._queue_lock:
            count = len(self._pending_queue)
            self._pending_queue.clear()
            logger.warning(f"[决策缓冲] 清空队列，丢弃{count}个待处理信号")
            return count


# ============================================================
#  全局单例
# ============================================================
_decision_buffer: Optional[DecisionBuffer] = None
_buffer_lock = threading.Lock()


def get_decision_buffer() -> DecisionBuffer:
    """获取决策缓冲层单例"""
    global _decision_buffer

    if _decision_buffer is None:
        with _buffer_lock:
            if _decision_buffer is None:
                _decision_buffer = DecisionBuffer()

    return _decision_buffer


def init_decision_buffer(
    signal_cooldown: Optional[int] = None,
    max_pending: Optional[int] = None,
    min_confidence: Optional[float] = None,
    max_positions: Optional[int] = None,  # 支持动态计算
    dynamic_position_mode: Optional[bool] = None,    # 启用动态仓位模式
) -> DecisionBuffer:
    """
    初始化决策缓冲层

    如果未传入参数，则从配置中心读取：
    - strategy.decision_buffer.signal_cooldown
    - strategy.decision_buffer.max_pending
    - strategy.decision_buffer.min_confidence
    - strategy.decision_buffer.max_positions
    - strategy.decision_buffer.dynamic_position_mode
    """
    global _decision_buffer

    # 从配置中心读取默认值
    try:
        from config.config_center import get_config_center
        config_center = get_config_center()

        if signal_cooldown is None:
            signal_cooldown = int(config_center.get('strategy.decision_buffer.signal_cooldown', 10))
        if max_pending is None:
            max_pending = int(config_center.get('strategy.decision_buffer.max_pending', 50))
        if min_confidence is None:
            min_confidence = float(config_center.get('strategy.decision_buffer.min_confidence', 0.35))
        if max_positions is None:
            max_positions_cfg = config_center.get('strategy.decision_buffer.max_positions')
            # null 或 None 表示动态计算
            max_positions = None if max_positions_cfg is None else int(max_positions_cfg)
        if dynamic_position_mode is None:
            dynamic_position_mode_val = config_center.get('strategy.decision_buffer.dynamic_position_mode', True)
            dynamic_position_mode = str(dynamic_position_mode_val).lower() in ('true', '1', 'yes')

    except Exception as e:
        logger.warning(f"从配置中心读取失败，使用默认值：{e}")

    with _buffer_lock:
        _decision_buffer = DecisionBuffer(
            signal_cooldown=signal_cooldown,
            max_pending=max_pending,
            min_confidence=min_confidence,
            max_positions=max_positions,
            dynamic_position_mode=dynamic_position_mode,
        )
        logger.info(
            f"决策缓冲层全局单例已初始化："
            f"signal_cooldown={signal_cooldown}s, "
            f"max_pending={max_pending}, "
            f"min_confidence={min_confidence}, "
            f"max_positions={_decision_buffer.max_positions} (dynamic={dynamic_position_mode})"
        )
        return _decision_buffer
