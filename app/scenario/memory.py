"""
场景记忆器 (Scenario Memory) - 生命周期状态机版

管理场景的完整生命周期: CANDIDATE → CONFIRMED → ACTIVE → DECAYING → EXPIRED

核心功能:
1. 五阶段生命周期状态机 (候选/确认/活跃/衰减/失效)
2. 分场景类型参数化 (确认窗口/冷却/TTL/衰减)
3. 候选与活跃分离追踪 (修复确认计数死锁BUG)
4. 衰减宽限 + 可恢复 + 超时失效
5. 高优先级场景紧急通道 (事件类绕过确认)
6. 场景持续时间统计与交易结果反馈
"""
import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from collections import deque, defaultdict
from datetime import datetime, timedelta

from .classifier import ScenarioType

logger = logging.getLogger("ftseek.scenario.memory")


# ======================================================================
#  生命周期枚举与数据结构
# ======================================================================

class ScenarioPhase(Enum):
    """场景生命周期阶段"""
    UNKNOWN   = "unknown"     # 初始态 / 失效退出后
    CANDIDATE = "candidate"   # 候选: 新场景检测到，等待连续确认
    CONFIRMED = "confirmed"   # 已确认: 连续N次确认完成（瞬态，立即进入ACTIVE）
    ACTIVE    = "active"      # 活跃: 驱动策略路由与风控
    DECAYING  = "decaying"    # 衰减: 置信度持续下降或超时
    EXPIRED   = "expired"     # 失效: 超过生命周期，回退到 UNKNOWN


@dataclass
class ScenarioRecord:
    """场景记录 (历史条目)"""
    scenario: ScenarioType
    confidence: float
    timestamp: float
    phase: ScenarioPhase = ScenarioPhase.UNKNOWN
    kline_index: int = 0


@dataclass
class ScenarioState:
    """场景状态 (增强版 - 分离活跃与候选追踪)"""
    # --- 活跃场景 ---
    active_scenario: ScenarioType = ScenarioType.UNKNOWN
    active_phase: ScenarioPhase = ScenarioPhase.UNKNOWN
    active_confidence: float = 0.0
    active_since: float = 0.0            # 进入 ACTIVE 的时间戳
    active_peak_confidence: float = 0.0  # 历史最高置信度

    # --- 候选场景（独立于活跃场景追踪） ---
    candidate_scenario: ScenarioType = ScenarioType.UNKNOWN
    candidate_count: int = 0             # 连续确认次数
    candidate_first_seen: float = 0.0    # 首次出现时间
    candidate_confidence: float = 0.0    # 最新置信度

    # --- 衰减追踪 ---
    decay_start_time: float = 0.0
    consecutive_low_confidence: int = 0

    # --- 切换控制 ---
    last_switch_time: float = 0.0

    # --- 灰区震荡检测 ---
    candidate_reset_count: int = 0       # 候选重置次数
    candidate_reset_window: float = 0.0  # 重置窗口起始时间
    gray_zone_source: str = ""           # 灰区触发源 (conflict/oscillation/decay/divergence)

    # --- 历史 ---
    history: deque = field(default_factory=lambda: deque(maxlen=100))


@dataclass
class ScenarioUpdateResult:
    """update() 的返回结果，携带生命周期上下文"""
    scenario: ScenarioType           # 当前有效场景
    phase: ScenarioPhase             # 当前阶段
    confidence: float                # 当前置信度
    duration_seconds: float = 0.0    # 活跃持续时间
    is_decaying: bool = False        # 是否正在衰减
    candidate: Optional[ScenarioType] = None   # 正在候选的场景（如有）
    candidate_progress: float = 0.0  # 候选确认进度 (0.0-1.0)
    peak_confidence: float = 0.0     # 历史最高置信度
    gray_zone_source: str = ""       # 灰区触发源 (conflict/oscillation/decay/low_conf/divergence)


# ======================================================================
#  分场景类型生命周期参数
# ======================================================================

SCENARIO_LIFECYCLE_PARAMS: Dict[ScenarioType, Dict] = {
    # 趋势类: 持续时间长，确认要求高，衰减宽容
    ScenarioType.TREND_STRONG: {
        'confirm_window': 3,
        'cooldown_seconds': 120,
        'max_duration_seconds': 7200,
        'decay_confidence_threshold': 0.45,
        'decay_grace_periods': 3,
        'expire_after_decay_seconds': 900,
    },
    ScenarioType.TREND_START: {
        'confirm_window': 2,
        'cooldown_seconds': 90,
        'max_duration_seconds': 3600,
        'decay_confidence_threshold': 0.5,
        'decay_grace_periods': 2,
        'expire_after_decay_seconds': 600,
    },
    ScenarioType.TREND_EXHAUST: {
        'confirm_window': 2,
        'cooldown_seconds': 60,
        'max_duration_seconds': 1800,
        'decay_confidence_threshold': 0.5,
        'decay_grace_periods': 2,
        'expire_after_decay_seconds': 300,
    },
    # 震荡类
    ScenarioType.RANGE_BOUND: {
        'confirm_window': 3,
        'cooldown_seconds': 120,
        'max_duration_seconds': 14400,
        'decay_confidence_threshold': 0.45,
        'decay_grace_periods': 3,
        'expire_after_decay_seconds': 1200,
    },
    # 事件类: 立即确认，短生命周期
    ScenarioType.GAP_EVENT: {
        'confirm_window': 1,
        'cooldown_seconds': 30,
        'max_duration_seconds': 1800,
        'decay_confidence_threshold': 0.4,
        'decay_grace_periods': 1,
        'expire_after_decay_seconds': 300,
    },
    ScenarioType.NEWS_DRIVEN: {
        'confirm_window': 1,
        'cooldown_seconds': 30,
        'max_duration_seconds': 3600,
        'decay_confidence_threshold': 0.4,
        'decay_grace_periods': 1,
        'expire_after_decay_seconds': 600,
    },
    # 波动类
    ScenarioType.VOL_EXPANSION: {
        'confirm_window': 2,
        'cooldown_seconds': 60,
        'max_duration_seconds': 3600,
        'decay_confidence_threshold': 0.5,
        'decay_grace_periods': 2,
        'expire_after_decay_seconds': 600,
    },
    ScenarioType.VOL_CONTRACTION: {
        'confirm_window': 3,
        'cooldown_seconds': 120,
        'max_duration_seconds': 7200,
        'decay_confidence_threshold': 0.45,
        'decay_grace_periods': 3,
        'expire_after_decay_seconds': 900,
    },
    # 反转/突破类
    ScenarioType.REVERSAL_SETUP: {
        'confirm_window': 2,
        'cooldown_seconds': 60,
        'max_duration_seconds': 1800,
        'decay_confidence_threshold': 0.5,
        'decay_grace_periods': 2,
        'expire_after_decay_seconds': 300,
    },
    ScenarioType.BREAKOUT_SETUP: {
        'confirm_window': 2,
        'cooldown_seconds': 60,
        'max_duration_seconds': 1800,
        'decay_confidence_threshold': 0.5,
        'decay_grace_periods': 2,
        'expire_after_decay_seconds': 300,
    },
    # 灰区: 短生命周期，立即生效，快速过期
    ScenarioType.GRAY_ZONE: {
        'confirm_window': 1,               # 立即生效
        'cooldown_seconds': 60,            # 短冷却 (灰区结束后快速允许新场景)
        'max_duration_seconds': 600,       # 最多持续 10 分钟
        'decay_confidence_threshold': 0.3,
        'decay_grace_periods': 1,
        'expire_after_decay_seconds': 120,  # 快速过期
    },
}

DEFAULT_LIFECYCLE_PARAMS: Dict = {
    'confirm_window': 3,
    'cooldown_seconds': 120,
    'max_duration_seconds': 3600,
    'decay_confidence_threshold': 0.5,
    'decay_grace_periods': 3,
    'expire_after_decay_seconds': 600,
}

# 高优先级场景 (紧急通道: 跳过确认窗口和冷却)
HIGH_PRIORITY_SCENARIOS = {
    ScenarioType.GAP_EVENT,
    ScenarioType.NEWS_DRIVEN,
}

# 灰区震荡检测参数
GRAY_ZONE_OSCILLATION_SECONDS = 300    # 候选震荡窗口 (5分钟)
GRAY_ZONE_OSCILLATION_THRESHOLD = 3    # 震荡次数触发灰区


# ======================================================================
#  场景记忆器 (状态机版)
# ======================================================================

class ScenarioMemory:
    """
    场景记忆器 - 生命周期状态机

    管理每个标的的场景状态，通过五阶段状态机实现:
    - 稳定: 确认窗口 + 冷却时间 → 减少来回切换
    - 灵敏: 紧急通道 → 事件类场景快速响应
    - 衰减: 置信度/超时检测 → 主动退出过时场景
    - 可解释: 阶段标签贯穿日志与路由
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}

        # 从 config 加载自定义生命周期参数 (覆盖默认值)
        self._lifecycle_overrides = self._load_lifecycle_config(config)

        # 每个标的维护一个场景状态
        self._states: Dict[str, ScenarioState] = {}

        # 场景级别交易结果反馈
        self._scenario_outcomes: Dict[str, List[Dict]] = defaultdict(list)

        # v2: 启动时从 SQLite 加载历史反馈
        self._load_historical_outcomes()

        logger.info(
            f"场景记忆器已初始化 (生命周期状态机版) | "
            f"场景参数={len(SCENARIO_LIFECYCLE_PARAMS)}个场景类型 | "
            f"历史反馈={sum(len(v) for v in self._scenario_outcomes.values())}条"
        )

    def _load_lifecycle_config(self, config: Dict = None) -> Dict:
        """从 config 加载自定义生命周期参数"""
        if not config:
            return {}
        lifecycle_cfg = config.get('lifecycle', {})
        overrides = {}
        for scenario_key, params in lifecycle_cfg.items():
            # scenario_key 可能是 'trend_strong' 或 ScenarioType 枚举值
            try:
                scenario_type = ScenarioType(scenario_key)
                overrides[scenario_type] = params
            except ValueError:
                logger.debug(f"未知场景类型配置: {scenario_key}, 跳过")
        return overrides

    def _get_lifecycle_params(self, scenario: ScenarioType) -> Dict:
        """获取场景的生命周期参数 (优先级: config覆盖 > 内置参数 > 默认)"""
        # 1. config 覆盖
        if scenario in self._lifecycle_overrides:
            merged = DEFAULT_LIFECYCLE_PARAMS.copy()
            merged.update(SCENARIO_LIFECYCLE_PARAMS.get(scenario, {}))
            merged.update(self._lifecycle_overrides[scenario])
            return merged
        # 2. 内置参数
        if scenario in SCENARIO_LIFECYCLE_PARAMS:
            merged = DEFAULT_LIFECYCLE_PARAMS.copy()
            merged.update(SCENARIO_LIFECYCLE_PARAMS[scenario])
            return merged
        # 3. 默认
        return DEFAULT_LIFECYCLE_PARAMS.copy()

    # ------------------------------------------------------------------
    #  状态获取
    # ------------------------------------------------------------------

    def get_state(self, code: str) -> ScenarioState:
        """获取标的的场景状态"""
        if code not in self._states:
            self._states[code] = ScenarioState()
        return self._states[code]

    # ------------------------------------------------------------------
    #  核心: update() 状态机
    # ------------------------------------------------------------------

    def update(self, code: str, scenario: ScenarioType, confidence: float,
               kline_index: int = None) -> ScenarioUpdateResult:
        """
        场景状态机更新 (核心方法)

        状态转移:
          UNKNOWN ─新检测─▶ CANDIDATE ─连续N次─▶ ACTIVE
          ACTIVE ─低置信度/超时─▶ DECAYING ─超时─▶ EXPIRED ─▶ UNKNOWN
          DECAYING ─置信度恢复─▶ ACTIVE
          高优先级 ─紧急通道─▶ ACTIVE (绕过确认/冷却)

        Args:
            code: 股票代码
            scenario: 分类器输出的场景类型
            confidence: 置信度
            kline_index: K线索引 (可选)

        Returns:
            ScenarioUpdateResult: 携带生命周期上下文的结果
        """
        state = self.get_state(code)
        now = time.time()
        params = self._get_lifecycle_params(state.active_scenario)

        # 记录历史
        state.history.append(ScenarioRecord(
            scenario=scenario,
            confidence=confidence,
            timestamp=now,
            phase=state.active_phase,
            kline_index=kline_index or 0,
        ))

        # ===== 阶段 0: 紧急通道（高优先级场景绕过所有检查） =====
        if scenario in HIGH_PRIORITY_SCENARIOS:
            # 2026-04-11 灰区防守一致性修复：
            # 如果当前处于灰区激活状态，高优先级场景覆盖灰区需要超过更高的置信度门槛
            # 防止弱信号事件在市场不明朗时跟难而覆盖灰区防守
            GRAY_ZONE_OVERRIDE_MIN_CONFIDENCE = float(
                self.config.get('gray_zone_high_priority_bypass_min', 0.65)
            )
            if (state.active_phase == ScenarioPhase.ACTIVE
                    and state.active_scenario == ScenarioType.GRAY_ZONE
                    and confidence < GRAY_ZONE_OVERRIDE_MIN_CONFIDENCE):
                # 置信度不足，无法覆盖灰区——记录并按常规流程处理
                logger.info(
                    f"[{code}] [紧急通道-拦截] {scenario.value} 置信度={confidence:.2f}"
                    f" < 灰区覆盖门槛={GRAY_ZONE_OVERRIDE_MIN_CONFIDENCE:.2f}"
                    f"，灰区防守不被覆盖"
                )
                # 不走紧急通道，继续按常规生命周期逐步处理
            else:
                old = state.active_scenario
                self._activate(state, scenario, confidence, now)
                if old != scenario:
                    logger.info(
                        f"[{code}] [紧急通道] {old.value} → {scenario.value} "
                        f"(置信度={confidence:.2f})"
                    )
                return self._build_result(state, now)

        # ===== 阶段 1: 活跃场景的生命周期检查 =====
        if state.active_phase == ScenarioPhase.ACTIVE:
            duration = now - state.active_since

            # 1a. 超时检查
            if duration > params['max_duration_seconds']:
                logger.info(
                    f"[{code}] [{state.active_scenario.value}] "
                    f"超时失效 (持续{duration:.0f}s > 上限{params['max_duration_seconds']}s)"
                )
                self._enter_decay(state, now, reason='max_duration')
                # 继续执行后续逻辑（可能有候选要处理）

            # 1b. 同场景更新置信度
            elif scenario == state.active_scenario:
                state.active_confidence = confidence
                state.active_peak_confidence = max(state.active_peak_confidence, confidence)

                # 置信度衰减检查
                if confidence < params['decay_confidence_threshold']:
                    state.consecutive_low_confidence += 1
                    if state.consecutive_low_confidence >= params['decay_grace_periods']:
                        logger.info(
                            f"[{code}] [{scenario.value}] 置信度衰减 "
                            f"(连续{state.consecutive_low_confidence}次 < "
                            f"{params['decay_confidence_threshold']})"
                        )
                        self._enter_decay(state, now, reason='low_confidence')
                    else:
                        logger.debug(
                            f"[{code}] [{scenario.value}] 低置信度 "
                            f"{state.consecutive_low_confidence}/{params['decay_grace_periods']}"
                        )
                else:
                    state.consecutive_low_confidence = 0

                # 同场景确认，清除候选
                state.candidate_scenario = ScenarioType.UNKNOWN
                state.candidate_count = 0
                return self._build_result(state, now)

        # ===== 阶段 2: 衰减场景检查 =====
        if state.active_phase == ScenarioPhase.DECAYING:
            decay_params = self._get_lifecycle_params(state.active_scenario)

            # 2a. 置信度恢复 → 回到 ACTIVE
            if (scenario == state.active_scenario and
                    confidence >= decay_params['decay_confidence_threshold']):
                state.active_phase = ScenarioPhase.ACTIVE
                state.active_confidence = confidence
                state.consecutive_low_confidence = 0
                state.decay_start_time = 0.0
                logger.info(
                    f"[{code}] [{scenario.value}] 置信度恢复 → ACTIVE "
                    f"(置信度={confidence:.2f})"
                )
                return self._build_result(state, now)

            # 2b. 衰减超时 → EXPIRED
            if now - state.decay_start_time > decay_params['expire_after_decay_seconds']:
                logger.info(
                    f"[{code}] [{state.active_scenario.value}] 衰减超时 → EXPIRED "
                    f"(衰减{now - state.decay_start_time:.0f}s)"
                )
                self._expire(state, now)
                # 灰区处理: 衰减超时且无继任候选 -> 灰区而非直接 UNKNOWN
                if state.candidate_scenario == ScenarioType.UNKNOWN:
                    logger.info(
                        f"[{code}] [灰区] 衰减无继任 → GRAY_ZONE"
                    )
                    self._activate(state, ScenarioType.GRAY_ZONE, 0.4, now)
                    state.gray_zone_source = 'decay'  # 衰减无继任
                    return self._build_result(state, now)
                # fall through to candidate tracking

        # ===== 阶段 3: 候选场景追踪 =====
        if scenario != state.active_scenario and scenario != ScenarioType.UNKNOWN:
            candidate_params = self._get_lifecycle_params(scenario)

            # 3a. 冷却检查
            if state.last_switch_time > 0 and now - state.last_switch_time < candidate_params['cooldown_seconds']:
                remaining = candidate_params['cooldown_seconds'] - (now - state.last_switch_time)
                logger.debug(
                    f"[{code}] 候选 {scenario.value} 冷却中 "
                    f"(剩余{remaining:.0f}s)"
                )
                return self._build_result(state, now)

            # 3b. 同一候选 → 累加确认
            if scenario == state.candidate_scenario:
                state.candidate_count += 1
                state.candidate_confidence = confidence
            else:
                # 新候选 → 重置
                # 灰区震荡检测: 候选频繁切换说明市场不确定
                if (state.candidate_scenario != ScenarioType.UNKNOWN
                        and scenario != ScenarioType.GRAY_ZONE):
                    if now - state.candidate_reset_window < GRAY_ZONE_OSCILLATION_SECONDS:
                        state.candidate_reset_count += 1
                    else:
                        state.candidate_reset_count = 1
                        state.candidate_reset_window = now

                    if state.candidate_reset_count >= GRAY_ZONE_OSCILLATION_THRESHOLD:
                        logger.info(
                            f"[{code}] [灰区] 候选震荡触发 "
                            f"(重置{state.candidate_reset_count}次/{GRAY_ZONE_OSCILLATION_SECONDS}s) "
                            f"→ GRAY_ZONE"
                        )
                        state.candidate_reset_count = 0  # 重置计数器
                        self._activate(state, ScenarioType.GRAY_ZONE, 0.5, now)
                        state.gray_zone_source = 'oscillation'  # 候选震荡
                        return self._build_result(state, now)

                if state.candidate_scenario != ScenarioType.UNKNOWN:
                    logger.debug(
                        f"[{code}] 候选切换: {state.candidate_scenario.value} → "
                        f"{scenario.value} (计数重置)"
                    )
                state.candidate_scenario = scenario
                state.candidate_count = 1
                state.candidate_first_seen = now
                state.candidate_confidence = confidence

            # 3c. 确认窗口达标 → 激活
            if state.candidate_count >= candidate_params['confirm_window']:
                old = state.active_scenario
                self._activate(state, scenario, confidence, now)
                logger.info(
                    f"[{code}] [{scenario.value}] 候选确认完成 "
                    f"({state.candidate_count}/{candidate_params['confirm_window']}) | "
                    f"{old.value} → {scenario.value} → ACTIVE"
                )
                return self._build_result(state, now)

            # 候选未达标，保持当前
            logger.debug(
                f"[{code}] 候选 {scenario.value} 确认中 "
                f"({state.candidate_count}/{candidate_params['confirm_window']})"
            )
            return self._build_result(state, now)

        # ===== 阶段 4: UNKNOWN 场景输入 或 同场景(非ACTIVE阶段) =====
        # 如果活跃场景已 EXPIRED/UNKNOWN 且输入也是 UNKNOWN，保持原状
        if (state.active_phase in (ScenarioPhase.UNKNOWN, ScenarioPhase.EXPIRED)
                and scenario != ScenarioType.UNKNOWN):
            # 初始状态收到非 UNKNOWN 场景 → 作为候选开始追踪
            candidate_params = self._get_lifecycle_params(scenario)
            state.candidate_scenario = scenario
            state.candidate_count = 1
            state.candidate_first_seen = now
            state.candidate_confidence = confidence

            if state.candidate_count >= candidate_params['confirm_window']:
                self._activate(state, scenario, confidence, now)
                logger.info(
                    f"[{code}] [{scenario.value}] 首次激活 → ACTIVE"
                )
            return self._build_result(state, now)

        return self._build_result(state, now)

    # ------------------------------------------------------------------
    #  状态转移辅助方法
    # ------------------------------------------------------------------

    def _activate(self, state: ScenarioState, scenario: ScenarioType,
                  confidence: float, now: float):
        """激活场景 (→ ACTIVE)"""
        state.active_scenario = scenario
        state.active_phase = ScenarioPhase.ACTIVE
        state.active_confidence = confidence
        state.active_since = now
        state.active_peak_confidence = confidence
        state.consecutive_low_confidence = 0
        state.decay_start_time = 0.0
        state.last_switch_time = now
        # 清除候选
        state.candidate_scenario = ScenarioType.UNKNOWN
        state.candidate_count = 0
        state.candidate_first_seen = 0.0
        state.candidate_confidence = 0.0

    def _enter_decay(self, state: ScenarioState, now: float, reason: str = ''):
        """进入衰减阶段 (→ DECAYING)"""
        state.active_phase = ScenarioPhase.DECAYING
        state.decay_start_time = now
        logger.debug(
            f"[{state.active_scenario.value}] → DECAYING (原因: {reason})"
        )

    def _expire(self, state: ScenarioState, now: float):
        """失效退出 (→ EXPIRED → UNKNOWN)"""
        old_scenario = state.active_scenario
        state.active_scenario = ScenarioType.UNKNOWN
        state.active_phase = ScenarioPhase.UNKNOWN
        state.active_confidence = 0.0
        state.active_since = 0.0
        state.active_peak_confidence = 0.0
        state.consecutive_low_confidence = 0
        state.decay_start_time = 0.0
        logger.debug(f"[{old_scenario.value}] → EXPIRED → UNKNOWN")

    def _build_result(self, state: ScenarioState, now: float) -> ScenarioUpdateResult:
        """构建返回结果"""
        duration = now - state.active_since if state.active_since > 0 else 0.0

        # 候选进度
        candidate = None
        candidate_progress = 0.0
        if state.candidate_scenario != ScenarioType.UNKNOWN and state.candidate_count > 0:
            candidate = state.candidate_scenario
            cparams = self._get_lifecycle_params(state.candidate_scenario)
            candidate_progress = min(1.0, state.candidate_count / max(1, cparams['confirm_window']))

        return ScenarioUpdateResult(
            scenario=state.active_scenario,
            phase=state.active_phase,
            confidence=state.active_confidence,
            duration_seconds=duration,
            is_decaying=(state.active_phase == ScenarioPhase.DECAYING),
            candidate=candidate,
            candidate_progress=candidate_progress,
            peak_confidence=state.active_peak_confidence,
            gray_zone_source=state.gray_zone_source if state.active_scenario == ScenarioType.GRAY_ZONE else "",
        )

    # ------------------------------------------------------------------
    #  查询方法
    # ------------------------------------------------------------------

    def get_current_scenario(self, code: str) -> ScenarioType:
        """获取当前场景"""
        state = self.get_state(code)
        return state.active_scenario

    def get_current_phase(self, code: str) -> ScenarioPhase:
        """获取当前阶段"""
        state = self.get_state(code)
        return state.active_phase

    def get_scenario_duration(self, code: str) -> float:
        """获取当前场景持续时长 (秒)"""
        state = self.get_state(code)
        if state.active_since == 0:
            return 0.0
        return time.time() - state.active_since

    def is_confirmed(self, code: str) -> bool:
        """判断当前场景是否处于 ACTIVE 阶段"""
        state = self.get_state(code)
        return state.active_phase == ScenarioPhase.ACTIVE

    def get_confirm_count(self, code: str) -> int:
        """获取候选场景的确认计数"""
        state = self.get_state(code)
        return state.candidate_count

    def get_history(self, code: str, limit: int = 10) -> List[Dict]:
        """获取场景切换历史"""
        state = self.get_state(code)
        history = list(state.history)[-limit:]
        return [
            {
                "scenario": r.scenario.value,
                "confidence": round(r.confidence, 2),
                "phase": r.phase.value,
                "timestamp": r.timestamp,
                "datetime": datetime.fromtimestamp(r.timestamp).isoformat(),
                "kline_index": r.kline_index,
            }
            for r in history
        ]

    def reset(self, code: str):
        """重置标的的场景状态"""
        if code in self._states:
            del self._states[code]
            logger.info(f"[{code}] 场景状态已重置")

    def reset_all(self):
        """重置所有标的的场景状态"""
        self._states.clear()
        logger.info("所有场景状态已重置")

    # ------------------------------------------------------------------
    #  交易结果反馈 (2026-04-10)
    # ------------------------------------------------------------------

    def record_outcome(self, scenario: str, code: str, pnl: float):
        """
        记录场景级别的交易结果

        由 data_subscriber 反馈闭环步骤调用。
        v2: 同时写入 SQLite 持久化。

        Args:
            scenario: 场景名称 (如 'TREND_STRONG')
            code: 股票代码
            pnl: 已实现盈亏
        """
        if not scenario:
            return

        self._scenario_outcomes[scenario].append({
            'pnl': pnl,
            'ts': time.time(),
            'code': code,
        })

        # 只保留最近100条
        if len(self._scenario_outcomes[scenario]) > 100:
            self._scenario_outcomes[scenario] = self._scenario_outcomes[scenario][-100:]

        # v2: 持久化到 SQLite
        try:
            from app.storage.database import get_database
            get_database().save_scenario_outcome(scenario, code, pnl)
        except Exception as e:
            logger.warning(f"[场景反馈] 持久化失败: {e}")

        stats = self._calc_scenario_stats(scenario)
        logger.info(
            f"[场景反馈] {scenario} | {code} pnl={pnl:.2f} | "
            f"胜率={stats['win_rate']:.0%} 盈亏比={stats['profit_factor']:.2f} "
            f"(样本={stats['total']}笔)"
        )

    def _calc_scenario_stats(self, scenario: str) -> Dict:
        """计算场景统计数据"""
        records = self._scenario_outcomes.get(scenario, [])
        if not records:
            return {'win_rate': 0.0, 'profit_factor': 0.0, 'total': 0, 'avg_pnl': 0.0}

        wins = [r['pnl'] for r in records if r['pnl'] > 0]
        losses = [abs(r['pnl']) for r in records if r['pnl'] < 0]

        win_rate = len(wins) / len(records) if records else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 1.0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 2.0
        avg_pnl = sum(r['pnl'] for r in records) / len(records)

        return {
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'total': len(records),
            'avg_pnl': avg_pnl,
        }

    def get_scenario_stats(self) -> Dict[str, Dict]:
        """获取所有场景的交易统计"""
        result = {}
        for scenario in self._scenario_outcomes:
            result[scenario] = self._calc_scenario_stats(scenario)
        return result

    def _load_historical_outcomes(self):
        """
        启动时从 SQLite 加载最近30天的场景交易结果 (v2 新增)

        解决数据孤岛问题: 重启后场景统计不再丢失
        """
        try:
            from app.storage.database import get_database
            db = get_database()
            historical = db.load_scenario_outcomes(days=30)
            for scenario, records in historical.items():
                self._scenario_outcomes[scenario] = records[-100:]  # 仍保留100条上限
            total = sum(len(v) for v in self._scenario_outcomes.values())
            if total > 0:
                logger.info(f"[场景记忆] 已从 SQLite 加载历史反馈: {total}条")
        except Exception as e:
            logger.warning(f"[场景记忆] 历史加载失败，从零开始: {e}")

    # ------------------------------------------------------------------
    #  状态摘要
    # ------------------------------------------------------------------

    def get_all_states(self) -> Dict[str, Dict]:
        """获取所有标的的场景状态摘要"""
        result = {}
        for code, state in self._states.items():
            candidate_info = None
            if state.candidate_scenario != ScenarioType.UNKNOWN:
                cparams = self._get_lifecycle_params(state.candidate_scenario)
                candidate_info = {
                    "scenario": state.candidate_scenario.value,
                    "count": state.candidate_count,
                    "window": cparams['confirm_window'],
                    "progress": f"{state.candidate_count}/{cparams['confirm_window']}",
                }
            result[code] = {
                "active_scenario": state.active_scenario.value,
                "phase": state.active_phase.value,
                "confidence": round(state.active_confidence, 2),
                "duration_seconds": self.get_scenario_duration(code),
                "peak_confidence": round(state.active_peak_confidence, 2),
                "is_decaying": state.active_phase == ScenarioPhase.DECAYING,
                "candidate": candidate_info,
            }
        return result


# ======================================================================
#  全局单例
# ======================================================================

_memory: Optional[ScenarioMemory] = None


def get_scenario_memory(config: Dict = None) -> ScenarioMemory:
    """获取场景记忆器单例"""
    global _memory
    if _memory is None:
        _memory = ScenarioMemory(config)
    return _memory
