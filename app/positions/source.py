"""PositionSource + ProtectionState 枚举 + helpers (2026-05-16)

设计原则:
- 每笔持仓必带 source (跨 broker/manual/strategy 显式区分)
- 默认 PROTECTED (策略不得自动操作非自己产生的仓位)
- 用户主动调 takeover() 才进入 MANAGED 状态
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Optional


class PositionSource(Enum):
    """持仓来源 — 用于跨 broker/manual/strategy 归因"""
    AUTO_MAIN = "auto_main"              # 主线策略 (signal_pipeline → _execute_long/short)
    AUTO_SCALPING = "auto_scalping"      # 短线引擎 (ScalpingEngine)
    AUTO_FAST_LANE = "auto_fast_lane"    # 快车道 (battle_plan entry_zone)
    AUTO_LLM_PLAN = "auto_llm_plan"      # LLM 计划 (BATTLE_PLAN_FAST_PATH)
    MANUAL = "manual"                    # 用户在 broker app 手动开仓
    LEGACY = "legacy"                    # 系统启动前已存在的历史仓
    UNKNOWN = "unknown"                  # 兜底 (识别失败)

    @classmethod
    def from_str(cls, raw: Any) -> "PositionSource":
        """容错解析 — 兼容历史 trader_type/strategy_id 字符串 + enum value"""
        if isinstance(raw, cls):
            return raw
        s = str(raw or "").lower().strip()
        # 1. 优先匹配 enum value (新规范的 source 标签)
        for member in cls:
            if member.value == s:
                return member
        # 2. 历史字段映射 (向后兼容)
        _map = {
            "main": cls.AUTO_MAIN, "main_pipeline": cls.AUTO_MAIN,
            "voting_llm": cls.AUTO_MAIN,
            "voting_llm_option": cls.AUTO_MAIN,  # 期权仓位归主线策略管理
            "manual_sync": cls.MANUAL,  # 向后兼容：同步发现的手动仓
            "scalping": cls.AUTO_SCALPING, "scalping_engine": cls.AUTO_SCALPING,
            "fast_lane": cls.AUTO_FAST_LANE,
            "battle_plan_fast_path": cls.AUTO_LLM_PLAN,
            "llm_plan": cls.AUTO_LLM_PLAN,
            "manual": cls.MANUAL, "futu_manual": cls.MANUAL,
            "broker_recovery": cls.LEGACY, "futu_sync": cls.MANUAL,
            "simulation": cls.LEGACY,
        }
        return _map.get(s, cls.UNKNOWN)

    def is_auto(self) -> bool:
        return self.value.startswith("auto_")


class ProtectionState(Enum):
    """持仓保护状态 — 决定策略能否自动操作

    PROTECTED (默认): 策略禁止 close/add/move_sl, 仅监控告警
    MANAGED: 策略可全权管理 (用户显式 takeover 后)
    FROZEN: 临时冻结所有操作 (人工干预中, 例: 财报前)
    """
    PROTECTED = "protected"
    MANAGED = "managed"
    FROZEN = "frozen"

    @classmethod
    def default_for(cls, source: PositionSource) -> "ProtectionState":
        """默认保护策略: AUTO 自动管理, MANUAL/LEGACY 默认 PROTECTED"""
        if source.is_auto():
            return cls.MANAGED
        return cls.PROTECTED


def get_position_source(position: Any) -> PositionSource:
    """从 Position 对象提取 source. 容错读取多个字段优先级."""
    if position is None:
        return PositionSource.UNKNOWN

    # 优先级 1: metadata.source (新规范)
    meta = getattr(position, "metadata", None) or {}
    if isinstance(meta, dict):
        src = meta.get("source")
        if src:
            return PositionSource.from_str(src)

    # 优先级 2: owner_strategy (Phase A 规范)
    owner = getattr(position, "owner_strategy", None)
    if owner:
        return PositionSource.from_str(owner)

    # 优先级 3: strategy_id (旧)
    strat = getattr(position, "strategy_id", None)
    if strat:
        return PositionSource.from_str(strat)

    return PositionSource.UNKNOWN


def get_protection_state(position: Any) -> ProtectionState:
    """从 Position 提取 protection. metadata.protection 优先, 否则按 source 默认."""
    if position is None:
        return ProtectionState.PROTECTED  # 安全默认

    meta = getattr(position, "metadata", None) or {}
    if isinstance(meta, dict):
        ps = meta.get("protection")
        if ps:
            try:
                return ProtectionState(str(ps).lower())
            except ValueError:
                pass

    return ProtectionState.default_for(get_position_source(position))


def is_strategy_managed(position: Any) -> bool:
    """策略是否可对此仓位执行自动操作 (close/add/move_sl)"""
    return get_protection_state(position) == ProtectionState.MANAGED


def is_protected(position: Any) -> bool:
    """是否受保护 (策略禁止操作)"""
    return get_protection_state(position) in (ProtectionState.PROTECTED, ProtectionState.FROZEN)


def tag_position(position: Any, source: PositionSource,
                  protection: Optional[ProtectionState] = None) -> None:
    """给 Position 打 source + protection 标签 (写入 metadata)"""
    if position is None:
        return
    meta = getattr(position, "metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["source"] = source.value
    meta["protection"] = (protection or ProtectionState.default_for(source)).value
    try:
        position.metadata = meta
    except Exception:
        pass
