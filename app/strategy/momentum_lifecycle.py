"""
动量生命周期策略 (Momentum Lifecycle Strategy)

提取自 MFFF/MYFF 麦语言指标的数学骨架，剥离"庄家控盘"叙事标签，
还原为核心动量振荡器 + 生命周期状态机。

核心数学:
    momentum = (EMA(EMA(close, N), N) / REF(EMA(EMA(close, N), N), 1) - 1) × 1000
    本质: 二阶低通滤波后的瞬时变化率 (basis points)

生命周期层级:
    加速期 (ACCELERATING)  — 动量正向且增强 → 趋势强化信号
    惯性期 (COASTING)      — 动量正向但减弱 → 趋势减速预警
    衰竭期 (FADING)        — 正动量消失       → 多头止盈信号
    衰退期 (DECLINING)     — 负动量且加深     → 空头加强信号
    收敛期 (CONVERGING)    — 负动量连续减弱   → 底部反转信号
    拐点期 (TURNING)       — 穿越零轴         → 方向转变确认

Strategy metadata:
    factor_type=MOMENTUM, horizon=SWING, regime_affinity=["trending"]
    共识引擎中自动获得 1.0 基准权重, 趋势市 ×1.5 增益
"""
import logging
from collections import defaultdict
from typing import Optional, Dict, List, Any

from .base import BaseStrategy, Signal, SignalAction
from .base import StrategyMetadata, FactorType, TimeHorizon, RiskLevel
from .indicators import ema_series, sma
from app.core.types import SignalSource

logger = logging.getLogger("ftseek.strategy.momentum_lifecycle")
logger.propagate = False


class MomentumLifecycleStrategy(BaseStrategy):
    """动量生命周期策略 — 基于二阶 EMA 变化率的动态信号"""

    name = "momentum_lifecycle"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MOMENTUM,
        horizon=TimeHorizon.SWING,
        regime_affinity=["trending"],
        risk_level=RiskLevel.MODERATE,
        required_klines=30,
        description="动量生命周期检测: 加速→惯性→衰竭→收敛→反转",
    )

    param_schema = {
        "ema_period": {
            "type": "int", "default": 5, "min": 3, "max": 21,
            "required": False,
            "description": "EMA 平滑周期 (5=敏感, 9=稳定)"
        },
        "convergence_bars": {
            "type": "int", "default": 3, "min": 2, "max": 5,
            "required": False,
            "description": "连续收缩 K 线数触发收敛信号"
        },
        "acceleration_bars": {
            "type": "int", "default": 3, "min": 2, "max": 5,
            "required": False,
            "description": "连续增强 K 线数触发加速信号"
        },
        "fade_signal_enabled": {
            "type": "bool", "default": True, "required": False,
            "description": "是否启用衰竭期止盈信号"
        },
    }

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.ema_period = self.config.get("ema_period", 5)
        self.convergence_bars = self.config.get("convergence_bars", 3)
        self.acceleration_bars = self.config.get("acceleration_bars", 3)
        self.fade_signal_enabled = self.config.get("fade_signal_enabled", True)

        # 内部状态 {code: {"momentum": [...], "prev_momentum": float}}
        self._state: Dict[str, Dict] = defaultdict(
            lambda: {"closes": [], "momentum": [], "prev_momentum": 0.0}
        )

        # 方向锁: 防止趋势市中频繁翻转
        self._direction_lock: Dict[str, Dict] = {}  # {code: {"dir": 1/-1/0, "bars": int}}

        logger.info(
            f"动量生命周期策略已初始化 | "
            f"EMA周期={self.ema_period} | 收敛K线={self.convergence_bars} | "
            f"加速K线={self.acceleration_bars}"
        )

    # =========================================================================
    #  核心计算
    # =========================================================================

    def _compute_momentum(self, closes: list) -> Optional[float]:
        """
        计算二阶 EMA 平滑后的瞬时变化率。

        momentum = (EMA(EMA(close, N), N) / prev_EMA(EMA(close, N), N) - 1) * 1000

        Returns:
            float: 动量值 (basis points), None if insufficient data
        """
        min_len = self.ema_period * 2 + 1
        if len(closes) < min_len:
            return None

        # 第一层 EMA
        ema1 = ema_series(closes, self.ema_period)
        if not ema1 or len(ema1) < self.ema_period:
            return None

        # 第二层 EMA
        ema2 = ema_series(ema1, self.ema_period)
        if not ema2 or len(ema2) < 2:
            return None

        prev_val = ema2[-2]
        curr_val = ema2[-1]
        if prev_val <= 0:
            return None

        return (curr_val / prev_val - 1.0) * 1000.0

    def _classify_lifecycle(self, momentum: float, prev_momentum: float,
                            history: list) -> str:
        """
        根据当前动量值和历史值判定生命周期阶段。

        Args:
            momentum: 当前动量值
            prev_momentum: 前一根 K 线的动量值
            history: 最近 N 根 K 线的动量值列表 (不含当前)

        Returns:
            生命周期阶段: "accelerating" | "coasting" | "fading" |
                          "declining" | "converging" | "turning_up" |
                          "turning_down" | "idle"
        """
        # 拐点: 穿零轴
        if prev_momentum <= 0 and momentum > 0:
            return "turning_up"
        if prev_momentum >= 0 and momentum < 0:
            return "turning_down"

        # 正动量区域
        if momentum > 0:
            if momentum > prev_momentum and prev_momentum > 0:
                return "accelerating"
            elif momentum < prev_momentum and prev_momentum > 0:
                return "coasting"
            else:
                return "accelerating"  # 从零以下刚上来走加速

        # 负动量区域
        if momentum < 0:
            if momentum < prev_momentum:  # 负得更深
                return "declining"
            else:  # 负值在减小 (回暖)
                # 检查是否连续收缩 (converging)
                if len(history) >= self.convergence_bars:
                    recent = history[-self.convergence_bars:]
                    if len(recent) == self.convergence_bars and all(
                        recent[i] < 0 and abs(recent[i]) < (abs(recent[i-1]) if i > 0 else float('inf'))
                        for i in range(len(recent))
                    ):
                        return "converging"
                return "declining"

        return "idle"

    # =========================================================================
    #  信号生成
    # =========================================================================

    def _generate_signal(self, code: str, lifecycle: str, momentum: float,
                         close: float, kline: dict) -> Optional[Signal]:
        """根据生命周期阶段生成信号"""

        # 加速期: 动量连续增强 → 趋势确认 BUY
        if lifecycle == "accelerating":
            # 检查是否三连增 (stronger signal)
            hist = self._state[code]["momentum"]
            if len(hist) >= self.acceleration_bars:
                recent = hist[-self.acceleration_bars:]
                if all(recent[i] > 0 for i in range(len(recent))):
                    is_triple = all(
                        recent[i] > (recent[i-1] if i > 0 else -float('inf'))
                        for i in range(len(recent))
                    )
                    if is_triple:
                        return Signal(
                            code=code,
                            action=SignalAction.BUY,
                            price=close,
                            source=SignalSource.TECHNICAL,
                            reason=f"动量三连增 (mom={momentum:.2f})",
                            strategy=self.name,
                        )
            # 单根加速也做 BUY (低置信度)
            return Signal(
                code=code,
                action=SignalAction.BUY,
                price=close,
                source=SignalSource.TECHNICAL,
                reason=f"动量加速 (mom={momentum:.2f})",
                strategy=self.name,
            )

        # 衰竭期: 正动量消失 → 多仓止盈 SELL
        if lifecycle == "fading" and self.fade_signal_enabled:
            return Signal(
                code=code,
                action=SignalAction.SELL,
                price=close,
                source=SignalSource.TECHNICAL,
                reason=f"动能衰竭 (mom={momentum:.2f})",
                strategy=self.name,
            )

        # 收敛期: 负动量连续减弱 → 底部反转 BUY
        if lifecycle == "converging":
            return Signal(
                code=code,
                action=SignalAction.BUY,
                price=close,
                source=SignalSource.TECHNICAL,
                reason=f"下跌动能收敛 ({self.convergence_bars}K)",
                strategy=self.name,
            )

        # 拐点: 上穿零轴 → BUY
        if lifecycle == "turning_up":
            return Signal(
                code=code,
                action=SignalAction.BUY,
                price=close,
                source=SignalSource.TECHNICAL,
                reason=f"拐点向上 (mom={momentum:.2f})",
                strategy=self.name,
            )

        # 拐点: 下穿零轴 → SELL
        if lifecycle == "turning_down":
            return Signal(
                code=code,
                action=SignalAction.SELL,
                price=close,
                source=SignalSource.TECHNICAL,
                reason=f"拐点向下 (mom={momentum:.2f})",
                strategy=self.name,
            )

        return None

    # =========================================================================
    #  策略入口
    # =========================================================================

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled:
            return None
        if kline_type != "K_1M":
            return None

        close = kline.get("close", 0)
        if close <= 0:
            return None

        state = self._state[code]

        # 累加收盘价
        closes = state["closes"]
        closes.append(close)
        if len(closes) > 200:
            state["closes"] = closes[-200:]  # 限制大小
        else:
            state["closes"] = closes

        # 计算动量
        momentum = self._compute_momentum(closes)
        if momentum is None:
            return None

        prev_momentum = state["prev_momentum"]
        state["prev_momentum"] = momentum

        # 记录动量历史
        mom_hist = state["momentum"]
        mom_hist.append(momentum)
        if len(mom_hist) > 50:
            state["momentum"] = mom_hist[-50:]
        else:
            state["momentum"] = mom_hist

        # 判定生命周期
        lifecycle = self._classify_lifecycle(momentum, prev_momentum, mom_hist)

        if lifecycle not in ("accelerating", "coasting", "idle"):
            logger.debug(
                f"[{code}] 动量生命周期: {lifecycle} | "
                f"mom={momentum:.3f} prev={prev_momentum:.3f}"
            )

        return self._generate_signal(code, lifecycle, momentum, close, kline)

    # =========================================================================
    #  状态查询
    # =========================================================================

    def get_lifecycle(self, code: str) -> Optional[str]:
        """获取当前生命周期阶段"""
        state = self._state.get(code, {})
        history = state.get("momentum", [])
        if not history:
            return None
        prev = state.get("prev_momentum", 0.0)
        return self._classify_lifecycle(history[-1], prev, history[:-1])

    def get_momentum_value(self, code: str) -> Optional[float]:
        """获取当前动量值"""
        history = self._state.get(code, {}).get("momentum", [])
        return history[-1] if history else None
