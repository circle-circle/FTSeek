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

from app.utils.session_detector import SessionDetector
from app.strategy.consensus_engine import ConsensusResult

# 初始化全局时段检测器 (延迟加载)
_session_detector: Optional[SessionDetector] = None

def get_session_detector(time_aware_config: Dict = None) -> SessionDetector:
    global _session_detector
    if _session_detector is None and time_aware_config:
        _session_detector = SessionDetector(time_aware_config)
    return _session_detector
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.decision_buffer")
# 2026-04-10 修复: 移除 propagate=False，让日志通过 ftseek 父级 logger 输出


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
    """决策记录（用于审计和回溯，2026-04-15 新增归因追踪）"""
    decision_id: str
    code: str
    action: SignalAction
    consensus_result: ConsensusResult
    trace_id: str = ""           # 全链路归因 ID (Phase 1)
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
        signal_cooldown: int = 120,           # 2026-04-16 对齐 config.yaml (120s)
        max_pending: int = 500,               # 2026-04-22: 200→500 防止队列溢出
        min_confidence: float = 0.35,         # 2026-04-24 统一: 对齐 risk_limits.min_confidence
        max_positions: Optional[int] = None,
        dynamic_position_mode: bool = True,
        source_risk_config: Optional[Dict] = None,
        time_aware_config: Optional[Dict] = None,
    ):
        self.signal_cooldown = signal_cooldown
        self.max_pending = max_pending
        self.min_confidence = min_confidence
        self.dynamic_position_mode = dynamic_position_mode
        self.source_risk_config = source_risk_config
        
        # Phase 2: 初始化时段检测器
        self.session_detector = get_session_detector(time_aware_config)

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
            self.max_positions = 12  # 2026-04-16 对齐 config.yaml (12)

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

        # LLM 决策复核
        self._llm_analyst = None
        self._llm_review_enabled = False
        self._llm_review_min_confidence = 0.80  # 触发复核的最低置信度
        self._llm_review_min_position = 0.08    # 触发复核的最低仓位比例(8%)

        logger.info(
            f"决策缓冲层已初始化：冷却期={signal_cooldown}s, "
            f"最大队列={max_pending}, 最低置信度={min_confidence}, "
            f"max_positions={self.max_positions} (dynamic={dynamic_position_mode})"
        )

    def init_llm_review(self, llm_analyst, min_confidence: float = 0.80, min_position: float = 0.08):
        """注入 LLM 市场研判引擎用于决策复核"""
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        if isinstance(llm_analyst, LLMMarketAnalyst) and llm_analyst.enabled:
            self._llm_analyst = llm_analyst
            self._llm_review_min_confidence = min_confidence
            self._llm_review_min_position = min_position
            self._llm_review_enabled = True
            logger.info(f"决策缓冲LLM复核已启用: min_conf={min_confidence}, min_pos={min_position}")

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
            from app.config import WATCH_LIST_HK, TRADE_ENV, MAX_OPEN_POSITIONS

            watchlist_size = len(WATCH_LIST_HK)

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
            return 12  # 2026-04-16 对齐 config.yaml (12)
        except Exception as e:
            logger.warning(f"[动态仓位] 计算失败，使用默认值 4: {e}")
            return 12  # 2026-04-16 对齐 config.yaml (12)

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

        # 获取 trace_id（2026-04-10 新增：贯穿链路追踪，Phase 1 写入记录）
        trace_id = getattr(consensus_result, 'trace_id', 'N/A')

        # 创建决策记录 (包含 TraceID)
        record = DecisionRecord(
            decision_id=decision_id,
            code=code,
            action=action,
            consensus_result=consensus_result,
            trace_id=trace_id,
        )

        # 更新统计
        with self._stats_lock:
            self._stats["total_received"] += 1

        logger.info(
            f"[决策缓冲] [{trace_id}] 收到信号：{code} {action.value} | "
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

        # ── 第 3 步：置信度检查 (Phase 2: 来源感知 + 时段感知) ──────────────────────────
        # 获取信号来源
        signal_source = getattr(record.consensus_result, 'source', 'TECHNICAL')
        if hasattr(signal_source, 'value'):
            signal_source = signal_source.value
        else:
            signal_source = str(signal_source).lower()

        # 获取当前时段配置
        current_session_config = {}
        if self.session_detector:
            current_session_config = self.session_detector.get_current_config()
            session_name = self.session_detector.get_current_session().name
            logger.debug(f"[Session-Detector] 当前时段: {session_name}")

        # 优先级：时段配置 > 来源配置 > 默认配置
        min_conf = self.min_confidence
        
        # 1. 应用来源配置
        source_risk = getattr(self, 'source_risk_config', None)
        if source_risk and isinstance(source_risk, dict):
            source_cfg = source_risk.get(signal_source, source_risk.get('technical', {}))
            source_conf = source_cfg.get('min_confidence')
            if source_conf is not None:
                min_conf = source_conf

        # 2. 应用时段配置 (覆盖)
        session_conf = current_session_config.get('min_confidence')
        if session_conf is not None:
            min_conf = session_conf
            logger.debug(f"[Session-Confidence] 覆盖为 {min_conf} (时段: {session_name})")

        # ── 第 3.5 步：A股联动置信度调整 (2026-05-04 新增) ──────
        try:
            from app.factors.hk_market_data import get_hk_market_data
            market_data = get_hk_market_data()
            if market_data and market_data.southbound_flow_valid:
                sb_net = market_data.southbound_net
                action_val = str(record.consensus_result.action.value) if hasattr(record.consensus_result.action, 'value') else str(record.consensus_result.action)
                if action_val == 'BUY':
                    if sb_net > 80:
                        min_conf *= 0.9  # 大额流入 → 降低门槛
                    elif sb_net < -50:
                        min_conf *= 1.2  # 大额流出 → 提高门槛
                elif action_val == 'SELL':
                    if sb_net < -80:
                        min_conf *= 0.9
                    elif sb_net > 50:
                        min_conf *= 1.2
                logger.debug(f"[A股联动] 置信度门槛调整后: {min_conf:.3f} (南向={sb_net:+.0f}亿)")
        except Exception:
            pass  # 降级放行，不影响主流程

        if confidence < min_conf:
            return self._reject(
                record,
                f"置信度过低：{confidence:.2f} < {min_conf} (来源={signal_source}, 时段={session_name})"
            )

        # ── 第 4 步：加入待处理队列 ────────────────────────────────
        with self._queue_lock:
            if len(self._pending_queue) >= self.max_pending:
                # 队列已满，智能淘汰：综合置信度+时效性
                import time as _time
                now = _time.time()
                
                def _eviction_score(r):
                    """越低越应该被淘汰: 置信度权重70% + 时效性权重30%"""
                    age = now - r.created_at
                    age_factor = max(0, 1 - age / 300)  # 5分钟后时效性归零
                    return r.consensus_result.confidence * 0.7 + age_factor * 0.3
                
                # 按淘汰分数排序，移除最低分
                self._pending_queue.sort(key=_eviction_score)
                removed = self._pending_queue.pop(0)
                self._reject(removed, "队列已满，被更高优先级信号替换")
                logger.debug(
                    f"[决策缓冲] 队列已满，智能淘汰低优先级信号："
                    f"{removed.code} {removed.action.value} "
                    f"(置信度={removed.consensus_result.confidence:.2f}, 年龄={now - removed.created_at:.0f}s)"
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
                logger.debug(
                    f"[决策缓冲] ⚠️ 队列拥堵告警：{len(self._pending_queue)}/{self.max_pending} "
                    f"({usage_rate:.0%})"
                )

        # ── 第 5 步：LLM 决策复核（仅对高置信度信号）─────────────────
        if not self._check_llm_review(record):
            # 先从队列中移除已加入的 record
            with self._queue_lock:
                try:
                    self._pending_queue.remove(record)
                except ValueError:
                    pass
            return self._reject(record, record.rejection_reason or "LLM复核不通过")

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

        # --- Phase 2 Attribution Hook (Approval) ---
        try:
            from app.attribution.chain import record_approval
            record_approval(
                trace_id=trace_id,
                code=code,
                action=action.value,
                market_regime="unknown", # Will be enriched later by executor
                scores={
                    "consensus": confidence,
                    "source": record.consensus_result.source.value if hasattr(record.consensus_result.source, 'value') else "unknown"
                }
            )
        except Exception as e:
            logger.debug(f"归因记录失败 (Approve): {e}")

        logger.info(
            f"[决策缓冲] [{trace_id}] ✅ 批准信号：{code} {action.value} | "
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

            # 清理过期信号（5分钟）
            import time as _time
            now = _time.time()
            expired = [r for r in self._pending_queue if now - r.created_at > 300]
            for r in expired:
                self._pending_queue.remove(r)
                self._reject(r, "信号过期（>5分钟），自动清除")
            if expired:
                logger.info(f"[决策缓冲] 清理 {len(expired)} 个过期信号")

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

    def _check_llm_review(self, record: DecisionRecord) -> bool:
        """LLM 决策复核 — 同步非阻塞，仅对高置信度信号生效

        Returns:
            True: 通过复核（或不需要复核）
            False: 复核建议拒绝
        """
        if not self._llm_review_enabled or not self._llm_analyst:
            return True

        consensus_result = record.consensus_result
        if not consensus_result:
            return True

        confidence = getattr(consensus_result, 'confidence', 0)
        # 只对高置信度信号做复核（这些信号可能直接大仓位执行）
        needs_review = confidence >= self._llm_review_min_confidence

        if not needs_review:
            return True

        # 获取 LLM 缓存的研判结果（同步、非阻塞）
        try:
            code = record.code
            assessment = self._llm_analyst.get_cached_assessment(code)

            if not assessment or not assessment.is_valid():
                # 无缓存结果，不阻塞，放行
                logger.debug(f"LLM复核: {code} 无缓存研判，放行")
                return True

            # 检查方向冲突
            action = record.action
            action_str = str(action.value).upper() if action else ''

            is_buy = 'BUY' in action_str
            llm_direction = assessment.direction

            # 严重冲突：信号买入但LLM强烈看空（或反之）
            severe_conflict = False
            if is_buy and llm_direction in ('STRONG_SELL', 'SELL'):
                severe_conflict = True
            elif not is_buy and llm_direction in ('STRONG_BUY', 'BUY'):
                severe_conflict = True

            if severe_conflict and assessment.confidence_level in ('HIGH', 'MEDIUM'):
                # LLM 中高置信度地反对该方向
                logger.warning(
                    f"LLM复核拒绝: {code} action={action_str} "
                    f"vs LLM={llm_direction}({assessment.confidence_level}) "
                    f"reason={assessment.reasoning}"
                )
                record.rejection_reason = (
                    f"LLM复核冲突: 信号{action_str} vs LLM{llm_direction}"
                    f"({assessment.confidence_level})"
                )
                return False

            # 风险标记检查
            if assessment.risk_flags:
                critical_flags = [
                    f for f in assessment.risk_flags
                    if 'critical' in f.lower() or 'halt' in f.lower()
                ]
                if critical_flags:
                    logger.warning(f"LLM复核: {code} 存在关键风险标记: {critical_flags}")
                    # 关键风险但不直接拒绝，只记录

            logger.debug(f"LLM复核通过: {code} action={action_str} LLM={llm_direction}")
            return True

        except Exception as e:
            logger.warning(f"LLM复核异常(降级放行): {e}")
            return True

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
            pm = PositionManager.get_instance()

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
        """拒绝信号 (2026-04-15 Phase 2: 增加归因记录)"""
        record.status = DecisionStatus.REJECTED
        record.rejection_reason = reason
        
        trace_id = getattr(record, 'trace_id', 'N/A')

        # 更新统计
        with self._stats_lock:
            self._stats["total_rejected"] += 1

            # 统计拒绝原因
            reason_category = reason.split(":")[0]  # 提取原因类别
            self._stats["rejection_reasons"][reason_category] = \
                self._stats["rejection_reasons"].get(reason_category, 0) + 1

        # 添加到历史
        self._decision_history.append(record)

        # --- Phase 2 Attribution Hook ---
        try:
            from app.attribution import record_rejection
            record_rejection(trace_id, record.code, reason, {
                "confidence": record.consensus_result.confidence,
                "source": record.consensus_result.source.value if hasattr(record.consensus_result.source, 'value') else str(record.consensus_result.source)
            })
        except Exception as e:
            logger.debug(f"归因记录失败 (Reject): {e}")

        logger.warning(
            f"[决策缓冲] [{trace_id}] ❌ 拒绝信号：{record.code} {record.action.value} | "
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
    source_risk_config: Optional[Dict] = None,       # Phase 2: 来源感知配置
    time_aware_config: Optional[Dict] = None,        # Phase 2: 分时段配置
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
            signal_cooldown = int(config_center.get('strategy.decision_buffer.signal_cooldown', 120))
        if max_pending is None:
            max_pending = int(config_center.get('strategy.decision_buffer.max_pending', 500))  # 2026-04-22: 200→500
        if min_confidence is None:
            min_confidence = float(config_center.get('strategy.decision_buffer.min_confidence', 0.35))  # 2026-04-24 统一
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
            source_risk_config=source_risk_config,
            time_aware_config=time_aware_config,
        )
        # Phase 2: 注入来源风控配置
        if source_risk_config:
            _decision_buffer.source_risk_config = source_risk_config
            logger.info(f"决策缓冲层已启用来源感知风控")
        
        logger.info(
            f"决策缓冲层全局单例已初始化："
            f"signal_cooldown={signal_cooldown}s, "
            f"max_pending={max_pending}, "
            f"min_confidence={min_confidence}, "
            f"max_positions={_decision_buffer.max_positions} (dynamic={dynamic_position_mode})"
        )
        return _decision_buffer
