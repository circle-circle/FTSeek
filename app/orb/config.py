"""
ORB 辅线配置 — RFC-075

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
    min_quality_score: float = 0.6       # RFC-060 (2026-06-22): 突破质量门禁 (原 engine.py 硬编码 0.6; yaml shadow 段设 0.75)

    # ── 止损止盈 (ATR-relative) ──
    stop_loss_atr_mult: float = 1.5      # SL = entry ± 1.5×ATR (2026-06-07: 1.0→1.5, 避免开盘噪声扫损)
    # 2026-06-23: 止盈改为 R-multiple (盈利 × sl_distance 的倍数), 取代死配置 take_profit_atr_mult.
    #   原因: engine 一直用硬编码 2R 止盈 (profit ≥ 2×sl_distance), 先于 ORBExitStack 触发,
    #         导致 take_profit_atr_mult (3.0×ATR) 永不到达. 现在 R 倍数可配置, 与实际行为一致.
    take_profit_r_mult: float = 2.0       # TP = profit ≥ 2.0 × sl_distance (2:1 R/R)
    # 2026-06-23: 删除 trail_activation_atr / trail_multiplier —
    #   trailing 未经验证 (SSOT 主线 trailing 84笔 -$3,773), 且 ratchet 字段从不更新,
    #   与 2R TP 抢优先级. 退出路径收敛为: 2R止盈 + breakdown + FloorStop + EOD.

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
    min_hold_seconds: int = 300            # 2026-06-07: 180→300 (3→5min), ORB交易需要更多时间发展
                                            # 数据: 旧参数下3-8min交易全部被噪音扫出
                                            # ATR floor修复后trail激活需$0.47+, 给足时间
    same_symbol_loss_cooldown: bool = True   # 当日亏损退出的标的禁止重新入场
    max_position_pct: float = 0.08          # 2026-06-29: 15%→8% 对齐Playbook, 防止单仓过大
    daily_notional_cap_pct: float = 0.10   # 2026-06-29: 日总notional上限 (占equity), 10%=$9.9K

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
