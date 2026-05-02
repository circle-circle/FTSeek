"""
app/strategy/time_gate.py — 港股分时段策略门控 (2026-04-30)

职责:
  根据当前港股交易时段，返回允许执行的策略列表和权重覆写。
  不参与策略逻辑、不修改信号、不侵入共识引擎 — 只做白名单过滤。

设计原则 (Karpathy):
  1. 纯函数式: get_active_gate(config, now) → GateResult
  2. 零侵入: 不修改策略代码，不修改共识引擎
  3. 降级安全: 配置缺失或非交易时段 → 返回空门控（不拦截任何策略）
  4. 港股特定: 覆盖 09:30-16:00，含午间休市 12:00-13:00

接入点:
  IntradayCoordinator._process_kline_data 中遍历策略前调用 is_active()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional, Set

logger = logging.getLogger("ftseek.strategy.time_gate")


@dataclass(frozen=True)
class GateConfig:
    """单个时段的门控配置"""
    name: str
    start: time
    end: time
    active_strategies: Set[str] = field(default_factory=set)
    weights_override: Dict[str, float] = field(default_factory=dict)
    exit_params_override: Dict[str, float] = field(default_factory=dict)  # 止损止盈覆写

    def contains(self, t: time) -> bool:
        """判断当前时间是否在该时段内"""
        return self.start <= t < self.end


@dataclass(frozen=True)
class GateResult:
    """门控查询结果"""
    phase_name: str                      # "opening" / "intraday" / "afternoon" / "closing"
    active_strategies: Set[str]          # 当前时段允许执行的策略
    weights_override: Dict[str, float]   # 权重覆写
    exit_params_override: Dict[str, float] # 止损止盈参数覆写
    is_trading_hours: bool               # 是否在交易时段内

    def is_active(self, strategy_name: str) -> bool:
        """判断某策略是否在当前时段激活"""
        return strategy_name in self.active_strategies

    def get_weight(self, strategy_name: str, default: float = 1.0) -> float:
        """获取策略权重（优先使用覆写）"""
        return self.weights_override.get(strategy_name, default)


# ── 配置缓存 ──────────────────────────────────────────────────────
_gate_cache: Optional[List[GateConfig]] = None


def _parse_time(val: str) -> time:
    """解析 "HH:MM" 字符串为 time 对象"""
    parts = val.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def _load_gate_config(config_center=None) -> List[GateConfig]:
    """从配置中心加载门控配置（懒加载 + 缓存）"""
    global _gate_cache
    if _gate_cache is not None:
        return _gate_cache

    _gate_cache = []
    try:
        if config_center is None:
            from config.config_center import get_config_center
            config_center = get_config_center()

        gates = config_center.get("strategy.time_gates", {})
        if not gates:
            logger.info("[TimeGate] 未配置 time_gates，所有策略全天激活")
            _gate_cache = []
            return _gate_cache

        for phase_name, phase_cfg in gates.items():
            if not isinstance(phase_cfg, dict):
                continue
            start = _parse_time(phase_cfg.get("start", "00:00"))
            end = _parse_time(phase_cfg.get("end", "23:59"))
            active = set(phase_cfg.get("active_strategies", []))
            weights = {k: float(v) for k, v in phase_cfg.get("weights_override", {}).items()}
            exit_params = {k: float(v) for k, v in phase_cfg.get("exit_params_override", {}).items()}

            _gate_cache.append(GateConfig(
                name=phase_name,
                start=start,
                end=end,
                active_strategies=active,
                weights_override=weights,
                exit_params_override=exit_params,
            ))
            logger.debug(f"[TimeGate] 加载时段 {phase_name}: {start}-{end} 策略={active}")

    except Exception as e:
        logger.warning(f"[TimeGate] 配置加载失败，降级为全开放: {e}")
        _gate_cache = []

    return _gate_cache


def get_current_gate(config_center=None, now: Optional[datetime] = None,
                     entry_time: Optional[datetime] = None) -> GateResult:
    """
    获取当前时段的门控结果。

    Args:
        config_center: 配置中心实例（None 则自动获取）
        now: 当前时间（None 则使用系统时间）
        entry_time: 开仓时间（如果提供，用于确定exit参数的时段；
                     策略过滤仍使用now — 开仓后策略过滤不再相关）

    Returns:
        GateResult: 当前时段的活跃策略和权重覆写
    """
    if now is None:
        now = datetime.now()
    current_time = now.time()

    gates = _load_gate_config(config_center)

    if not gates:
        # 无配置或非交易时段 → 全开放（不拦截）
        return GateResult(
            phase_name="none",
            active_strategies=set(),  # 空集合 = 不过滤
            weights_override={},
            exit_params_override={},
            is_trading_hours=False,
        )

    # 策略过滤: 始终使用当前时间 (now)
    for gate in gates:
        if gate.contains(current_time):
            # Exit参数: 如果提供了entry_time，使用开仓时段；否则使用当前时段
            exit_gate = gate
            if entry_time is not None:
                entry_time_val = entry_time
                if isinstance(entry_time_val, str):
                    try:
                        entry_time_val = datetime.fromisoformat(entry_time_val.replace(' ', 'T'))
                    except Exception:
                        entry_time_val = now
                for g in gates:
                    if g.contains(entry_time_val.time()):
                        exit_gate = g
                        break

            return GateResult(
                phase_name=gate.name,
                active_strategies=gate.active_strategies,
                weights_override=gate.weights_override,
                exit_params_override=exit_gate.exit_params_override,
                is_trading_hours=True,
            )

    # 不在任何配置时段内（如午间休市 12:00-13:00）
    return GateResult(
        phase_name="none",
        active_strategies=set(),  # 空集合 = 不过滤
        weights_override={},
        exit_params_override={},
        is_trading_hours=False,
    )


def is_strategy_active(
    strategy_name: str,
    config_center=None,
    now: Optional[datetime] = None,
    all_strategies: Optional[Set[str]] = None,
) -> bool:
    """
    便捷函数: 判断策略是否在当前时段激活。

    行为:
      - 配置存在且在交易时段 → 检查白名单
      - 配置缺失或非交易时段 → 返回 True（不拦截，向后兼容）
    """
    gate = get_current_gate(config_center, now)

    # 空 active_strategies = 无门控 = 全部允许
    if not gate.active_strategies:
        return True

    return strategy_name in gate.active_strategies


def get_exit_param(param_name: str, default: float, config_center=None,
                   now: Optional[datetime] = None,
                   entry_time: Optional[datetime] = None) -> float:
    """
    获取当前时段的止损止盈参数（优先使用时段覆写）。

    Args:
        param_name: 参数名称
        default: 默认值（时段无配置时使用）
        config_center: 配置中心实例
        now: 当前时间
        entry_time: 开仓时间（如果提供，exit参数使用开仓时段而非当前时段）

    行为:
      - 时段配置存在 → 返回覆写值
      - 时段配置缺失 → 返回 default

    参数映射:
      atr_hard_mult    → atr_hard_mult (ATR 硬止损倍数)
      atr_dynamic_mult → atr_dynamic_mult (ATR 动态止损倍数)
      trailing_atr_mult → trailing_atr_mult (移动止损 ATR 倍数)
      take_profit_atr_mult → take_profit_atr_mult (止盈 ATR 倍数)
      intraday_stop_pct → intraday_stop_pct (日内止损百分比)
      breakeven_trigger → breakeven_trigger (保本触发阈值)
      max_holding_hours → max_holding_hours (最大持仓时长)

    用法:
      # 开仓时使用当前时段参数
      mult = get_exit_param("atr_hard_mult", default=1.5)

      # 持仓检查时使用开仓时段参数（防止时段切换导致止损跳变）
      mult = get_exit_param("atr_hard_mult", default=1.5, entry_time=position.entry_time)
    """
    gate = get_current_gate(config_center, now, entry_time)
    return gate.exit_params_override.get(param_name, default)
