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
    AUTO_ORB = "auto_orb"                # ORB 开盘区间突破策略 (Task #6 2026-06-15)
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
            "auto_main": cls.AUTO_MAIN,  # position_sync 新发现的策略仓 (P0 2026-06-11: 补漏)
            "voting_llm": cls.AUTO_MAIN,
            "voting_llm_option": cls.AUTO_MAIN,  # 期权仓位归主线策略管理
            "manual_sync": cls.MANUAL,  # P0-fix (2026-06-15): position_sync 发现的手动仓 → MANUAL (非 AUTO_MAIN)
            "orb": cls.AUTO_ORB, "orb_engine": cls.AUTO_ORB,
            "auto_orb": cls.AUTO_ORB,  # Task #6 (2026-06-15): ORB 来源标记
            "fast_lane": cls.AUTO_FAST_LANE,
            "auto_fast_lane": cls.AUTO_FAST_LANE,  # 补漏
            "battle_plan_fast_path": cls.AUTO_LLM_PLAN,
            "llm_plan": cls.AUTO_LLM_PLAN, "auto_llm_plan": cls.AUTO_LLM_PLAN,  # 补漏
            "manual": cls.MANUAL, "futu_manual": cls.MANUAL,
            "broker_recovery": cls.LEGACY,
            "futu_sync": cls.UNKNOWN,  # P0-fix (2026-06-15): 来源不明 → UNKNOWN+PROTECTED (非 AUTO_MAIN)
            "futu_discovery": cls.UNKNOWN,  # P0-fix (2026-06-15): broker 发现 → UNKNOWN+PROTECTED (非 AUTO_MAIN, MU 事件)
            "broker_reconcile": cls.LEGACY,  # P0-fix (2026-06-15): 对账恢复 → LEGACY+PROTECTED (需用户显式 takeover)
            "recovered": cls.UNKNOWN,  # P0-fix (2026-06-15): position_monitor restore → UNKNOWN+PROTECTED
            "simulation": cls.LEGACY,
        }
        return _map.get(s, cls.UNKNOWN)

    def is_auto(self) -> bool:
        """实例方法: 当前来源是否为自动交易 (兼容现有调用 PositionSource.AUTO_MAIN.is_auto())"""
        return self.value.startswith("auto_")

    def is_manual(self) -> bool:
        """实例方法: 当前来源是否为手动开仓 (与 is_auto() 对称, Task #6 2026-06-15)"""
        return self == PositionSource.MANUAL

    @classmethod
    def is_auto_source(cls, source: Any) -> bool:
        """类方法: 任意来源标识 (enum / 字符串) 是否为自动交易 (Task #6 2026-06-15)

        自动调用 from_str 容错解析, 兼容 owner_strategy 等历史字段值.
        """
        return cls.from_str(source).is_auto()

    @classmethod
    def is_manual_source(cls, source: Any) -> bool:
        """类方法: 任意来源标识 (enum / 字符串) 是否为手动开仓 (Task #6 2026-06-15)"""
        return cls.from_str(source) == cls.MANUAL


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
        """源头隔离 (2026-06-11): 按 source 决定默认保护状态
        
        根因修复: broker-first (2026-06-03) 把所有仓位统一 MANAGED,
        导致手动持仓被 portfolio drawdown / exit_monitor / signal_pipeline 误操作。
        正确做法: 源头区分 — MANUAL/LEGACY 默认 PROTECTED, 系统仓位 MANAGED。
        
        消费者只需调 is_strategy_managed() 即可正确过滤, 无需各自查 DB。
        """
        if source in (PositionSource.MANUAL, PositionSource.LEGACY, PositionSource.UNKNOWN):
            return cls.PROTECTED
        return cls.MANAGED


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


# ═══════════════════════════════════════════════════════════════════
#  Task #35 (2026-06-16): 三类持仓 owner 分类
# ═══════════════════════════════════════════════════════════════════
#
#  目的: CapitalAllocator 需要把持仓拆成 3 个互斥池, 分别计入不同的资本约束:
#    AUTO              — 策略管理仓位, 占 auto bucket cap
#    MANUAL            — 用户主动注册的手动仓位 (manual_hold_registry / source=MANUAL)
#    LEGACY_PROTECTED  — 系统启动前已存在的历史仓 (LEGACY/UNKNOWN), 只读保护
#    MANUAL_ORPHAN     — broker 发现但既不在 registry 也不在 today_orders 的孤儿仓
#                        归入 LEGACY_PROTECTED 同等待遇 (默认 PROTECTED)
#
#  使用:
#    cls = classify_owner(position)   # → OWNER_AUTO / OWNER_MANUAL / ...
#    if is_legacy_protected(position): # 不进 auto/manual 池, 走单独保留
# ═══════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# Task #35 (2026-06-16): owner_strategy 分类 — CapitalAllocator 资本隔离
# ─────────────────────────────────────────────────────────────────────────────
OWNER_AUTO = "auto"                         # auto pipeline 当前开仓
OWNER_LEGACY_PROTECTED = "legacy_protected" # 启动时已存在的历史持仓 (不自动平仓)
OWNER_MANUAL = "manual"                     # ManualHoldRegistry 中已注册的手工持仓
OWNER_MANUAL_ORPHAN = "manual_orphan"       # 用户手工持仓但未在 ManualHoldRegistry 注册

# 四类喂入 StrategyCoordinator.reconcile() 时的合法标签集
OWNER_CLASSES = (OWNER_AUTO, OWNER_MANUAL, OWNER_LEGACY_PROTECTED, OWNER_MANUAL_ORPHAN)


def classify_owner(position: Any) -> str:
    """Task #35: 推断 owner 分类 — 用于 CapitalAllocator 资本隔离.

    返回值之一: 'auto' / 'legacy_protected' / 'manual' / 'manual_orphan'.

    用途:
        - auto             → 计入 current_auto_exposure (占 auto_pool)
        - legacy_protected → 不计入 auto_pool, 单独保留, 仅监控告警
        - manual           → 不计入 auto_pool, 单独保留, ManualGuard 监控
        - manual_orphan    → 同 manual (隔离), 但未在 Registry 注册 (待补登记)

    字段缺失或异常时 fallback 为 'legacy_protected' (保护用户当前仓位).
    """
    if position is None:
        return OWNER_LEGACY_PROTECTED

    src = get_position_source(position)

    # 1. auto_*  (策略产生且元数据完整)
    if src.is_auto():
        return OWNER_AUTO

    # 2. MANUAL  (区分 Registry 已注册 vs 孤儿)
    if src == PositionSource.MANUAL:
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            sym = getattr(position, "symbol", None)
            if sym and manual_hold_registry.is_manual_held(sym):
                return OWNER_MANUAL
            return OWNER_MANUAL_ORPHAN
        except Exception:
            return OWNER_LEGACY_PROTECTED

    # 3. LEGACY / UNKNOWN / 其他 → legacy_protected (兜底保护)
    return OWNER_LEGACY_PROTECTED


def is_legacy_protected(position: Any) -> bool:
    """Task #35: 是否为启动时已存在的历史持仓 (LEGACY/UNKNOWN+PROTECTED).

    供 CapitalAllocator/ManualGuard 决定是否纳入 auto_pool / 是否仅告警.
    """
    return classify_owner(position) == OWNER_LEGACY_PROTECTED


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
