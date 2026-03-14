"""
成交量异动策略
成交量大幅放大时产生提醒信号
"""
import logging
from collections import defaultdict, deque
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from app import config

logger = logging.getLogger("ftseek.strategy.volume_alert")


class VolumeAlertStrategy(BaseStrategy):
    name = "volume_alert"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("volume_alert", {}))
        self.ratio_threshold = self.config.get("volume_ratio_threshold", 2.0)
        self.lookback = self.config.get("lookback_periods", 20)
        self._volumes = defaultdict(lambda: deque(maxlen=self.lookback + 5))

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        volume = kline.get("volume", 0)
        close = kline.get("close", 0)
        if volume <= 0:
            return None

        vol_deque = self._volumes[code]
        vol_deque.append(volume)

        if len(vol_deque) < self.lookback:
            return None

        recent = list(vol_deque)[-self.lookback - 1:-1]  # 不含当前
        if not recent:
            return None

        avg_vol = sum(recent) / len(recent)
        if avg_vol <= 0:
            return None

        ratio = volume / avg_vol

        if ratio >= self.ratio_threshold:
            logger.info(f"[{code}] 成交量异动 ratio={ratio:.2f}x, vol={volume}, avg={avg_vol:.0f}")
            return Signal(
                code=code,
                name=kline.get("name", code),
                action=SignalAction.ALERT,
                price=close,
                reason=f"成交量异动: 当前{volume:,.0f}, 均值{avg_vol:,.0f}, 倍率{ratio:.1f}x",
                strategy=self.name,
            )

        return None
