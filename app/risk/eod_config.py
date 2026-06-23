"""
共享 EOD (End-of-Day) 配置 — 单一真相源

C4 修复 (2026-05-13): 主线 pre_close_exit 和 force_close_at
从同一全局配置读取，消除两套引擎收盘行为不一致的问题。

配置来源: unified_config.yaml → risk_limits.end_of_day
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EODConfig:
    """全局收盘协议 — 主线 + ORB 共享"""

    no_new_entry_after_et: str = "15:50"
    force_close_loss_at_et: str = "15:55"
    overnight_threshold_atr: float = 1.0
    min_hold_minutes_before_eod_exit: int = 0

    # 派生: 解析为当日美东分钟数
    no_new_entry_minutes: int = field(init=False)
    force_close_minutes: int = field(init=False)

    def __post_init__(self):
        self.no_new_entry_minutes = self._parse_et_time(self.no_new_entry_after_et)
        self.force_close_minutes = self._parse_et_time(self.force_close_loss_at_et)

    @staticmethod
    def _parse_et_time(et_str: str) -> int:
        """解析 'HH:MM' 美东时间 → 当日分钟数"""
        try:
            parts = et_str.strip().split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return hour * 60 + minute
        except Exception:
            return 16 * 60  # fallback 16:00


def load_eod_config() -> EODConfig:
    """从 unified_config.yaml 加载全局 EOD 配置"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        eod = cfg.get_dotted("risk_limits.end_of_day", {})
        if isinstance(eod, dict) and eod:
            return EODConfig(
                no_new_entry_after_et=eod.get("no_new_entry_after_et", "15:50"),
                force_close_loss_at_et=eod.get("force_close_loss_at_et", "15:55"),
                overnight_threshold_atr=float(eod.get("overnight_threshold_atr", 1.0)),
                min_hold_minutes_before_eod_exit=int(eod.get("min_hold_minutes_before_eod_exit", 0)),
            )
    except Exception as e:
        logger.warning(f"[EOD] 加载全局配置失败, 使用默认值: {e}")

    return EODConfig()