"""Playbook 配置读取入口 — 从 unified_config.yaml 的 playbook 节读取参数。

设计目标:
- 消灭 instruction_generator.py / pipeline.py 中的硬编码魔法数字
- 提供统一的默认值与配置访问接口
- 不引入循环依赖 (本模块只读 ConfigContainer, 不 import Playbook 业务模块)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class PlaybookConfig:
    """Playbook 配置包装器, 提供点号访问和默认值."""

    # 默认配置 (与 2026-06-26 修复对齐)
    DEFAULTS: Dict[str, Any] = {
        # risk_parameters
        "risk_base_capital": 82000.0,
        "risk_pct": 0.5,
        "rr_ratio": 1.0,
        "fixed_stop_loss_pct": 2.0,
        "max_position_pct": 0.06,
        "atr_multiplier": 2.0,
        # execution_parameters
        "intraday_breakout_pct": 0.5,
        "intraday_volume_ratio": 1.5,
        "min_confidence": 60,
        "min_tp_rr": 1.5,
        "buying_power_fallback_alert": True,
        # 2026-07-19: 下单前报价健康门 (复用 data_health_guard.check_quote_health).
        #   拦截过宽 spread / 过期报价 → 阻止市价单打在坏价上. gate 失败时 fail-open (取价异常不阻塞).
        "pretrade_quote_gate_enabled": True,
        "pretrade_max_spread_pct": 0.05,
        "pretrade_max_quote_age_sec": 60.0,
        # B4 (2026-07-20): 开仓数据新鲜度门 — 降级源 (finnhub 15min延迟等) / 过期报价禁开新仓.
        #   补 pretrade_quote_gate 盲区 (降级源无 bid/ask 整体绕过 health 门). 只拦开仓不拦平仓.
        "entry_freshness_gate_enabled": True,
        "entry_max_quote_age_sec": 90.0,
        # 2026-06-28: 单日 guardrail (仅 auto_execute 阶段生效)
        # 2026-07-06: 模拟交易期提升交易量 20%→40%, 5→10 (收集更多样本数据)
        # 2026-07-09: 40%→60% — bridge候选直写DB后, deep_analysis信号质量提升,
        #   更多资金给bridge候选执行, 积累更多交易数据验证方向+退出策略
        "daily_notional_cap_pct": 0.80,
        "max_daily_positions": 10,
        # horizon defaults
        "short_rr_ratio": 1.0,
        "long_rr_ratio": 2.5,
        "short_fixed_stop_loss_pct": 2.0,
        "long_fixed_stop_loss_pct": 2.0,
    }

    def __init__(self):
        self._config = self._load()
        self._horizon_config = self._load_horizon()

    @staticmethod
    def _load() -> Dict[str, Any]:
        """尝试从 unified_config.yaml 读取 playbook 配置, 失败则返回默认."""
        try:
            from app.core.config_container import ConfigContainer
            container = ConfigContainer.get_instance()
            cfg = container.get_dotted("playbook", {}) or {}
            # 合并 risk_parameters + execution_parameters 到扁平字典
            merged = {}
            for section in ("risk_parameters", "execution_parameters"):
                section_cfg = cfg.get(section) or {}
                if isinstance(section_cfg, dict):
                    merged.update(section_cfg)
            return merged
        except Exception as e:
            logger.warning(f"[PlaybookConfig] 读取配置失败, 使用默认: {e}")
            return {}

    def _load_horizon(self) -> Dict[str, Any]:
        """从 playbook_horizon.yaml 读取 horizon 配置, 失败则返回默认."""
        try:
            from pathlib import Path
            path = Path(__file__).resolve().parent.parent.parent / "config" / "playbook_horizon.yaml"
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"[PlaybookConfig] 读取 horizon 配置失败, 使用默认: {e}")
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置, 优先使用 PlaybookConfig.DEFAULTS 作为兜底."""
        if default is None and key in self.DEFAULTS:
            default = self.DEFAULTS[key]
        return self._config.get(key, default)

    def get_horizon(self, key: str, default: Any = None) -> Any:
        """读取 horizon 配置"""
        return self._horizon_config.get("horizon_selector", {}).get(key, default)

    def get_horizon_params(self, horizon: str) -> Dict[str, Any]:
        """读取指定 horizon 的参数"""
        return self._horizon_config.get(f"{horizon}_horizon", {})

    def get_horizon_top(self, key: str, default: Any = None) -> Any:
        """读取 playbook_horizon.yaml 顶层键 (如 factor_exit_rules, market_state_overlay)."""
        return self._horizon_config.get(key, default)

    def get_exit_config_version(self) -> str:
        """退出参数快照版本 (短哈希).

        2026-07-20 修复M: 毕业判定的 PnL/胜率维度 = 入场 edge × 退出执行,
        退出参数一变同一批入场信号的胜率就变. 用本哈希给每笔 trade_record 打标,
        使 cap_reduction / OKR 毕业统计只在同一 config_version 窗口内取样, 消除
        "边积累边调参数" 的污染. 只纳入会改变已实现 PnL 的退出参数;
        入场/仓位参数不计 (它们由 forward-return IC 侧独立评估, 天然免疫).
        """
        if getattr(self, "_exit_config_version", None):
            return self._exit_config_version
        try:
            _snapshot = {
                "factor_exit_rules": self._horizon_config.get("factor_exit_rules", {}),
                "short_vol_buckets": self._horizon_config.get("short_horizon", {}).get("vol_buckets", {}),
                "long_vol_buckets": self._horizon_config.get("long_horizon", {}).get("vol_buckets", {}),
                "short_time_exit_hours": self._horizon_config.get("short_horizon", {}).get("time_exit_hours"),
                "long_time_exit_hours": self._horizon_config.get("long_horizon", {}).get("time_exit_hours"),
                "cooldown_sl_pct": self.get("cooldown_sl_pct", 4.0),
                "early_tight_sl_enabled": self.get("early_tight_sl_enabled", False),
                "early_tight_sl_minutes": self.get("early_tight_sl_minutes", 30),
                "early_tight_sl_pct": self.get("early_tight_sl_pct", 1.0),
                "breakeven_fee_cushion_pct": self.get("breakeven_fee_cushion_pct", 0.0),
                # 2026-07-30 D4: 费用感知保本线改变保本锚位置 (已实现 PnL) — 必须入版本哈希,
                #   否则毕业统计跨参数窗口混样 (07-20 军规)
                "breakeven_fee_aware_enabled": self.get("breakeven_fee_aware_enabled", False),
                # 2026-07-24 (DEB-20260723/24): MFE停滞退出参数 — 改变已实现 PnL, 纳入版本哈希
                "mfe_stall_exit_enabled": self.get("mfe_stall_exit_enabled", False),
                "mfe_stall_exit_minutes": self.get("mfe_stall_exit_minutes", 150),
                "mfe_stall_exit_mfe_pct": self.get("mfe_stall_exit_mfe_pct", 0.5),
            }
            _blob = json.dumps(_snapshot, sort_keys=True, ensure_ascii=False, default=str)
            self._exit_config_version = "exit-" + hashlib.sha1(_blob.encode("utf-8")).hexdigest()[:12]
        except Exception as e:
            logger.warning(f"[PlaybookConfig] exit_config_version 计算失败, 用 unknown: {e}")
            self._exit_config_version = "exit-unknown"
        return self._exit_config_version

    def __getattr__(self, key: str) -> Any:
        return self.get(key)


# 全局懒加载实例
_playbook_config: PlaybookConfig | None = None

# ── R-4 (2026-08-23 DEB-20260823): 退出 SSOT 失效检测 ──
# 原实现: 单例一旦构造全仓无任何重置点 (已 grep 确认), 而 ConfigHotReloader
# (main.py:377) 只监控 unified_config.yaml 且回调只同步 ConfigContainer。
# config/playbook_horizon.yaml 是 LIVE 退出规则的真源 (factor_exit_rules /
# vol_buckets / time_exit_hours), 完全不在监控内。后果双重:
#   ① 改 yaml 后 ConfigContainer 更新而 playbook 退出逻辑仍用旧值 → 半个系统看新值;
#   ② _exit_config_version 哈希同样冻结 → trade_records 打旧 hash, "毕业统计只在同一
#      config_version 窗口取样"这条护栏 (07-20 军规) 静默失效, 直接污染终裁口径。
# 现按 mtime 自动失效重建。节流 5s: get_playbook_config() 在退出循环里每仓每轮都调,
# 不能每次 os.stat。
_HORIZON_MTIME_CHECK_INTERVAL_SEC = 5.0
_horizon_mtime: float | None = None
_horizon_mtime_checked_at: float = 0.0
_config_lock = threading.Lock()


def _horizon_yaml_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config" / "playbook_horizon.yaml"


def _current_horizon_mtime() -> float | None:
    try:
        return _horizon_yaml_path().stat().st_mtime
    except OSError:
        return None


def reset_playbook_config() -> None:
    """强制丢弃 PlaybookConfig 单例 (供热重载回调/测试显式调用).

    下次 get_playbook_config() 会重新读 unified_config.yaml + playbook_horizon.yaml,
    并重算 exit_config_version。
    """
    global _playbook_config, _horizon_mtime, _horizon_mtime_checked_at
    with _config_lock:
        _playbook_config = None
        _horizon_mtime = None
        _horizon_mtime_checked_at = 0.0
    logger.info("[PlaybookConfig] 单例已重置, 下次读取将重新加载配置")


def get_playbook_config() -> PlaybookConfig:
    """获取 PlaybookConfig 单例 (R-4: playbook_horizon.yaml 变更时自动失效重建)."""
    global _playbook_config, _horizon_mtime, _horizon_mtime_checked_at

    if _playbook_config is None:
        with _config_lock:
            if _playbook_config is None:
                _playbook_config = PlaybookConfig()
                _horizon_mtime = _current_horizon_mtime()
                _horizon_mtime_checked_at = time.time()
        return _playbook_config

    _now = time.time()
    if _now - _horizon_mtime_checked_at >= _HORIZON_MTIME_CHECK_INTERVAL_SEC:
        with _config_lock:
            _horizon_mtime_checked_at = _now
            _mt = _current_horizon_mtime()
            if _mt is not None and _horizon_mtime is not None and _mt != _horizon_mtime:
                _old_ver = getattr(_playbook_config, "_exit_config_version", None)
                _playbook_config = PlaybookConfig()
                _horizon_mtime = _mt
                _new_ver = _playbook_config.get_exit_config_version()
                logger.warning(
                    "[PlaybookConfig] 检测到 playbook_horizon.yaml 变更, 已重载退出配置 | "
                    "exit_config_version: %s → %s (跨版本样本不可混算)",
                    _old_ver or "unknown", _new_ver,
                )
    return _playbook_config


def tighten_sl_by_beta(direction: str, entry_price: float, initial_sl: float) -> float:
    """SL β收紧公式的单一真源 (2026-08-14, 止损可见性修复).

    退出评估 (pipeline._compute_dynamic_effective_sl) 与成交推送展示
    (playbook_service) 必须用同一份公式 — 此前公式只内联在退出逻辑里,
    推送展示的止损价 ≠ 实际生效的止损价 (UCTT 案: 展示-4.83% 实际-2.42%).
    参数源 sl_tighten_beta / sl_tighten_min_pct 与退出逻辑同源 (config.get).
    任一失败 → 原样返回 initial_sl (fail-open, 展示/记账增强不阻断执行).
    """
    try:
        if not initial_sl or not entry_price or initial_sl <= 0 or entry_price <= 0:
            return initial_sl
        cfg = get_playbook_config()
        beta = float(cfg.get("sl_tighten_beta", 1.0))
        min_pct = float(cfg.get("sl_tighten_min_pct", 1.5))
        if beta >= 1.0:
            return initial_sl
        # 方向兼容: pipeline 退出链用 LONG/SHORT, playbook_service 用 BUY/SELL_SHORT
        _d = str(direction).upper()
        if _d in ("LONG", "BUY"):
            # 收紧 = SL 上移向 entry; 但不得高于 -(min_pct) 线 (防过紧误扫正常回撤)
            return min(entry_price - (entry_price - initial_sl) * beta,
                       entry_price * (1 - min_pct / 100))
        # SHORT 对称: 收紧 = SL 下移向 entry; 不得低于 +(min_pct) 线
        return max(entry_price + (initial_sl - entry_price) * beta,
                   entry_price * (1 + min_pct / 100))
    except Exception:
        return initial_sl
