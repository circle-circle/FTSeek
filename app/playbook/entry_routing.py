"""[DEB-20260901 选项1② · ADR-035] 入场 archetype 路由解析 (默认关闭, 弹药先行).

病因回顾: DA thesis 写"低吸限价/确认位/禁止追高"是自然语言装饰文本, 执行层只认
宽区间一次市价 + re-price 重锚定 → 低吸单被跑成市价追单 (NOW 8/31 实盘 $145.09
比指令确认位 $140.50 高 +3.26%)。

本模块把结构化入场语义 (Phase A 字段: entry_archetype/confirmation/chase_cap_price,
存于 raw_instruction_json) 解析为执行路由决策:

  PULLBACK_LIMIT / FADE_SHORT + 有效 confirmation.price_level
      → action="limit": 确认位限价单 + TTL 撤单 (TTL 内不成交=放弃, 绝不市价追)。
  BREAKOUT / 无 archetype / 无有效确认位
      → None: 维持现状 (市价), 零行为变更。

启用前提: playbook.entry_archetype_routing_enabled=true (默认 false)。
启用判据预注册于 ADR-035: 影子 n≥20 对 / 期望改善≥30% / 成交率≥50% / mfe=0 占比降。
回滚: 开关置 false + 重启 (本模块返回全 None, 等价不存在)。

纯函数设计: 不触 DB/不下单/不写文件, 供 pipeline 与契约测试直调。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 可路由 archetype: 带"等回踩/等反抽"限价语义的入场类型。
# BREAKOUT 不路由 (追突破=市价语义本身, DA 提案缺失→BREAKOUT=现状)。
ROUTABLE_ARCHS = frozenset({"PULLBACK_LIMIT", "FADE_SHORT"})

# 限价位与现价偏离超此比例 → 视为 DA 数据错误 (笔误/单位错), 不路由,
# 回退现状市价并留 warning (宁可保守不错杀)。
_MAX_LEVEL_DEV = 0.25

# TTL 兜底 (秒): 配置读取失败时用。证据源 = DEB-20260806 锚限价影子
# 5分钟 TTL Δ+416.8/108对 (30分钟 TTL -224.4 → 短TTL限价优于追单)。
_DEFAULT_TTL_SECONDS = 300.0


def routing_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """路由总开关 (默认 false)。cfg 缺省时从 config_container 读。"""
    if cfg is not None and "entry_archetype_routing_enabled" in cfg:
        return bool(cfg.get("entry_archetype_routing_enabled", False))
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(
            "playbook.entry_archetype_routing_enabled", False))
    except Exception:
        return False


def _read_ttl_seconds(cfg: Optional[Dict[str, Any]] = None) -> float:
    if cfg is not None and "entry_archetype_ttl_seconds" in cfg:
        try:
            return max(30.0, float(cfg.get("entry_archetype_ttl_seconds") or _DEFAULT_TTL_SECONDS))
        except Exception:
            return _DEFAULT_TTL_SECONDS
    try:
        from app.core.config_container import get_config
        return max(30.0, float(get_config().get_dotted(
            "playbook.entry_archetype_ttl_seconds", _DEFAULT_TTL_SECONDS) or _DEFAULT_TTL_SECONDS))
    except Exception:
        return _DEFAULT_TTL_SECONDS


def parse_raw_semantics(raw_instruction_json: Any) -> Dict[str, Any]:
    """从 raw_instruction_json (str|dict) 提取结构化入场语义。

    返回 {"entry_archetype": str|None, "confirmation": dict|None,
          "chase_cap_price": float|None} — 缺失/损坏一律 None (按旧指令现状行为)。
    """
    raw = raw_instruction_json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    arch = raw.get("entry_archetype")
    arch = str(arch).upper() if arch else None
    conf = raw.get("confirmation")
    if not isinstance(conf, dict):
        conf = None
    cap = raw.get("chase_cap_price")
    try:
        cap = float(cap) if cap not in (None, "", "null") else None
    except Exception:
        cap = None
    return {"entry_archetype": arch, "confirmation": conf, "chase_cap_price": cap}


def resolve_route(raw_instruction_json: Any, direction: str,
                  market_price: float,
                  cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """解析执行路由决策。纯函数。

    Args:
        raw_instruction_json: playbook_instructions.raw_instruction_json (str|dict)。
        direction: 指令方向 ("BUY" | "SHORT"/"SELL_SHORT")。
        market_price: 触发时刻市价 (BUY 用 ask / SHORT 用 bid 更佳, 调用方传入)。
        cfg: 可选 playbook 配置 dict (测试注入); 缺省走 config_container。

    Returns:
        None — 不路由 (开关关 / 无 archetype / BREAKOUT / 无有效确认位 / 偏离过大),
               调用方维持现状市价路径。
        {"action": "skip", "reason": "archetype_level_conflict", ...}
             — DA 价位自相矛盾 (确认位在禁追线外侧), 数据质量保护, 指令终态跳过。
        {"action": "limit", "limit_price": float, "ttl_seconds": float,
         "archetype": str, "chase_cap_price": float|None}
             — 确认位限价路由。
    """
    if not routing_enabled(cfg):
        return None
    sem = parse_raw_semantics(raw_instruction_json)
    arch = sem["entry_archetype"]
    if arch not in ROUTABLE_ARCHS:
        return None
    conf = sem["confirmation"] or {}
    try:
        level = float(str(conf.get("price_level", "")).replace(",", ""))
    except Exception:
        level = None
    if not level or level <= 0:
        return None  # 无有效确认位 → 现状 (市价), 不猜价位

    is_short = str(direction or "").upper() in ("SHORT", "SELL_SHORT")
    cap = sem["chase_cap_price"]

    # DA 价位自相矛盾保护: BUY 确认位不应在禁追线上方 (空: 对称)。
    if cap and cap > 0:
        if not is_short and level > cap:
            return {"action": "skip", "reason": "archetype_level_conflict",
                    "archetype": arch, "limit_price": level, "chase_cap_price": cap}
        if is_short and level < cap:
            return {"action": "skip", "reason": "archetype_level_conflict",
                    "archetype": arch, "limit_price": level, "chase_cap_price": cap}

    # 数据质量保护: 确认位与现价偏离 >25% → 疑似笔误, 不路由 (回退现状)。
    if market_price and market_price > 0:
        if abs(level / market_price - 1.0) > _MAX_LEVEL_DEV:
            return None

    # TTL: 配置上限 ∧ DA 显式 ttl_min (取短)。DA 默认 ttl_min=15 → 配置 300s 生效。
    ttl = _read_ttl_seconds(cfg)
    try:
        _ttl_min = conf.get("ttl_min")
        if _ttl_min:
            ttl = min(ttl, float(_ttl_min) * 60.0)
    except (TypeError, ValueError):
        # R5 纪律留痕: 异常不吞 — ttl_min 畸形时退回配置 TTL 并 warn (DEB-20260901 Phase C)
        logger.warning("[entry_routing] ttl_min 解析失败, 退回配置 TTL: %r", conf.get("ttl_min"))

    return {"action": "limit", "limit_price": round(level, 4),
            "ttl_seconds": ttl, "archetype": arch, "chase_cap_price": cap}
