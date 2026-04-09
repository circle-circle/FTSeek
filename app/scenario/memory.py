"""
场景记忆器 (Scenario Memory)

用于场景平滑和防抖动，避免场景频繁切换导致策略路由不稳定。

核心功能:
1. 场景确认窗口 (confirm_window) - 新场景需连续出现 N 次才生效
2. 切换冷却时间 (cool_down) - 场景切换后需等待 M 分钟
3. 优先级覆盖 - 高优先级场景 (事件类) 可跳过确认
4. 场景历史追踪 - 记录场景切换历史供回测分析
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque
from datetime import datetime, timedelta

from .classifier import ScenarioType

logger = logging.getLogger("ftseek.scenario.memory")
logger.propagate = False


@dataclass
class ScenarioRecord:
    """场景记录"""
    scenario: ScenarioType
    confidence: float
    timestamp: float
    kline_index: int = 0  # K 线索引


@dataclass
class ScenarioState:
    """场景状态"""
    current_scenario: ScenarioType
    confirm_count: int = 0  # 确认计数
    last_switch_time: float = 0.0  # 上次切换时间
    history: deque = field(default_factory=lambda: deque(maxlen=100))  # 历史记录


class ScenarioMemory:
    """
    场景记忆器

    维护每个标的的场景状态，实现平滑切换。
    """

    # 配置参数
    CONFIRM_WINDOW = 3         # 场景确认 K 线数
    COOL_DOWN_MINUTES = 2      # 切换冷却时间 (分钟)
    COOL_DOWN_SECONDS = 120    # 冷却时间 (秒)

    # 高优先级场景 (可跳过确认)
    HIGH_PRIORITY_SCENARIOS = {
        ScenarioType.GAP_EVENT,
        ScenarioType.NEWS_DRIVEN,
    }

    def __init__(self, config: Dict = None):
        """
        初始化场景记忆器

        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.CONFIRM_WINDOW = self.config.get('confirm_window', 3)
        self.COOL_DOWN_MINUTES = self.config.get('cool_down_minutes', 2)
        self.COOL_DOWN_SECONDS = self.COOL_DOWN_MINUTES * 60

        # 每个标的维护一个场景状态
        self._states: Dict[str, ScenarioState] = {}

        logger.info(
            f"场景记忆器已初始化 | "
            f"确认窗口={self.CONFIRM_WINDOW}K 线 | "
            f"冷却时间={self.COOL_DOWN_MINUTES}分钟"
        )

    def get_state(self, code: str) -> ScenarioState:
        """获取标的的场景状态"""
        if code not in self._states:
            self._states[code] = ScenarioState(
                current_scenario=ScenarioType.UNKNOWN,
                confirm_count=0,
                last_switch_time=0.0,
            )
        return self._states[code]

    def update(self, code: str, scenario: ScenarioType, confidence: float,
               kline_index: int = None) -> ScenarioType:
        """
        更新场景状态，返回平滑后的场景

        Args:
            code: 股票代码
            scenario: 新场景分类结果
            confidence: 置信度
            kline_index: K 线索引 (可选)

        Returns:
            ScenarioType: 平滑后的场景
        """
        state = self.get_state(code)
        current_time = time.time()

        # 记录历史
        record = ScenarioRecord(
            scenario=scenario,
            confidence=confidence,
            timestamp=current_time,
            kline_index=kline_index or 0,
        )
        state.history.append(record)

        # --- 高优先级场景直接生效 ---
        if scenario in self.HIGH_PRIORITY_SCENARIOS:
            if scenario != state.current_scenario:
                logger.info(
                    f"[{code}] 高优先级场景覆盖：{state.current_scenario.value} → {scenario.value}"
                )
                state.current_scenario = scenario
                state.confirm_count = 1
                state.last_switch_time = current_time
            return scenario

        # --- 场景相同，增加确认计数 ---
        if scenario == state.current_scenario:
            state.confirm_count = min(state.confirm_count + 1, self.CONFIRM_WINDOW)
            return scenario

        # --- 场景不同，检查冷却时间 ---
        elapsed_seconds = current_time - state.last_switch_time
        if elapsed_seconds < self.COOL_DOWN_SECONDS:
            remaining = self.COOL_DOWN_SECONDS - elapsed_seconds
            logger.debug(
                f"[{code}] 场景切换冷却中：{state.current_scenario.value} → {scenario.value} "
                f"(剩余{remaining:.0f}秒)"
            )
            return state.current_scenario

        # --- 检查确认计数 ---
        if state.confirm_count >= self.CONFIRM_WINDOW:
            # 新场景确认完成，切换
            old_scenario = state.current_scenario
            state.current_scenario = scenario
            state.confirm_count = 1
            state.last_switch_time = current_time

            logger.info(
                f"[{code}] 场景切换：{old_scenario.value} → {scenario.value} "
                f"(已确认{self.CONFIRM_WINDOW}次)"
            )
            return scenario
        else:
            # 确认计数不足，保持原场景，但累积新场景计数
            state.confirm_count = min(state.confirm_count + 1, self.CONFIRM_WINDOW - 1)
            logger.debug(
                f"[{code}] 场景确认中：{scenario.value} "
                f"(计数{state.confirm_count}/{self.CONFIRM_WINDOW})"
            )
            return state.current_scenario

    def get_current_scenario(self, code: str) -> ScenarioType:
        """获取当前场景"""
        state = self.get_state(code)
        return state.current_scenario

    def get_confirm_count(self, code: str) -> int:
        """获取当前场景的确认计数"""
        state = self.get_state(code)
        return state.confirm_count

    def get_history(self, code: str, limit: int = 10) -> List[Dict]:
        """
        获取场景切换历史

        Args:
            code: 股票代码
            limit: 返回最近 N 条记录

        Returns:
            历史记录列表
        """
        state = self.get_state(code)
        history = list(state.history)[-limit:]

        return [
            {
                "scenario": r.scenario.value,
                "confidence": round(r.confidence, 2),
                "timestamp": r.timestamp,
                "datetime": datetime.fromtimestamp(r.timestamp).isoformat(),
                "kline_index": r.kline_index,
            }
            for r in history
        ]

    def get_scenario_duration(self, code: str) -> float:
        """
        获取当前场景持续时长 (秒)

        Args:
            code: 股票代码

        Returns:
            持续时长 (秒)
        """
        state = self.get_state(code)
        if state.last_switch_time == 0:
            return 0.0

        return time.time() - state.last_switch_time

    def is_confirmed(self, code: str) -> bool:
        """
        判断当前场景是否已确认

        Args:
            code: 股票代码

        Returns:
            是否已确认
        """
        state = self.get_state(code)
        return state.confirm_count >= self.CONFIRM_WINDOW

    def reset(self, code: str):
        """
        重置标的的场景状态

        Args:
            code: 股票代码
        """
        if code in self._states:
            del self._states[code]
            logger.info(f"[{code}] 场景状态已重置")

    def reset_all(self):
        """重置所有标的的场景状态"""
        self._states.clear()
        logger.info("所有场景状态已重置")

    def get_all_states(self) -> Dict[str, Dict]:
        """获取所有标的的场景状态摘要"""
        result = {}
        for code, state in self._states.items():
            result[code] = {
                "current_scenario": state.current_scenario.value,
                "confirm_count": state.confirm_count,
                "duration_seconds": self.get_scenario_duration(code),
                "is_confirmed": self.is_confirmed(code),
            }
        return result


# 全局单例
_memory: Optional[ScenarioMemory] = None


def get_scenario_memory(config: Dict = None) -> ScenarioMemory:
    """获取场景记忆器单例"""
    global _memory
    if _memory is None:
        _memory = ScenarioMemory(config)
    return _memory
