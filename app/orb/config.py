"""
ORB 辅线配置 — RFC-056

Opening Range Breakout 独立管线配置加载。
从 unified_config.yaml::orb 段读取参数。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class ORBConfig:
    """ORB 辅线全部配置"""

    # ── 总控 ──
    enabled: bool = False
    mode: str = "observe"  # observe | shadow | active

    # ── 标的池 (ETF, 与主线零重叠) ──
    universe: List[str] = field(default_factory=lambda: [
        "US.SPY", "US.QQQ", "US.IWM", "US.DIA",
        "US.XLE", "US.XLF", "US.XLK", "US.XLV",
    ])

    # ── 入场参数 ──
    range_minutes: int = 15              # 开盘区间长度 (分钟)
    min_range_width_pct: float = 0.3     # 最小区间宽度 %
    max_range_width_pct: float = 2.5     # 最大区间宽度 %
    breakout_buffer_pct: float = 0.1     # 突破缓冲 %
    min_volume_ratio: float = 1.1        # 最低量能倍数 (放宽, 原1.3太严)
    entry_start_et: str = "09:45"        # 入场窗口开始
    entry_end_et: str = "11:00"          # 入场窗口结束

    # ── 止损止盈 (ATR-relative) ──
    stop_loss_atr_mult: float = 1.5      # SL = entry ± 1.5×ATR (2026-06-07: 1.0→1.5, 避免开盘噪声扫损)
    take_profit_atr_mult: float = 3.0    # TP = entry ± 3.0×ATR (2026-06-07: 2.0→3.0, R/R 2:1)
    trail_activation_atr: float = 1.5    # 2026-06-07: 1.0→1.5, 盈利 1.5 ATR 后才激活追踪 (给交易发展空间)
    trail_multiplier: float = 1.5        # 2026-06-07: 1.2→1.5, peak - 1.5×ATR (更宽松追踪, 减少噪音扫出)

    # ── 风控 ──
    max_positions: int = 2               # 同时最多 2 个 ORB 仓位
    max_daily_trades: int = 4            # 每日最多 4 笔
    capital_pct: float = 0.10            # 10% 总资金
    position_risk_pct: float = 0.005     # 0.5% 单笔风险

    # ── 退出 ──
    eod_exit_time: str = "15:55"         # 盘末强制退出时间
    polling_interval_seconds: int = 5    # 轮询间隔

    # ── 方向偏见 (2026-06-03): 基于真实数据, SHORT在下跌市中胜过LONG 10:1 ──
    regime_short_bias_quality: float = 0.85  # TREND_DOWN: LONG 突破需要 Q≥0.85
    regime_long_bias_quality: float = 0.85   # TREND_UP: SHORT 突破需要 Q≥0.85
    low_vol_short_bias_quality: float = 0.85 # LOW_VOL+下行: LONG 突破需要 Q≥0.85
    min_hold_seconds: int = 180              # 持仓 < N秒 跳过 FLOOR_STOP 退出 (噪音过滤)
    same_symbol_loss_cooldown: bool = True   # 当日亏损退出的标的禁止重新入场

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ORBConfig":
        """从 unified_config.yaml::orb dict 构造"""
        if not isinstance(raw, dict):
            return cls()

        # 类型安全: 只取已知字段, 给默认值
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in raw.items() if k in known_fields}

        try:
            return cls(**filtered)
        except TypeError as e:
            logger.warning("[ORBConfig] 解析失败, 回退默认值: %s", e)
            return cls()

    @property
    def is_active(self) -> bool:
        """是否实际下单 (active 模式)"""
        return self.enabled and self.mode == "active"

    @property
    def is_shadow(self) -> bool:
        """是否记录但不执行 (shadow 模式)"""
        return self.enabled and self.mode == "shadow"
