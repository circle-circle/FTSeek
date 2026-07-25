"""
持仓期 thesis/invalidation 动态复核 ExitPolicy (轨道B, 2026-07-10)

填补缺口: 现有退出全机械 (ATR/trailing/scale/pre_close) + exit_policies 纯规则,
没有任何环节拿"盘中新信息"重新判断当初的 thesis 是否已被打破。本模块用 LLM 做这件
ATR 规则做不到、LLM 不可替代的判断。

安全不变量 (硬约束, 不可放松):
- LLM 只能触发【更早退出】(CLOSE/REDUCE), 永不放松硬止损。
- thesis_intact / 任何失败 / 禁用 / 节流 → NO_ACTION → 交回 PositionExitMonitor 6 机械规则,
  机械止损照跑。→ 下行有界。
- LLM 超时 / 不可用 / schema 不合 → NO_ACTION (等于纯机械, 零退化)。

默认 feature flag OFF (feature_flags: thesis_recheck)。B1 阶段只实现 + 单测, 不接线到
live dispatcher; B2 shadow, B3 sim 开启实际执行。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.strategy.exit_policies import ExitDecision, _BaseExitPolicy, _read_entry_thesis

logger = logging.getLogger(__name__)

# LLM 输出枚举 (反幻觉强校验; 非法 → success=False → NO_ACTION)
ALLOWED_VERDICTS = ["thesis_intact", "thesis_broken", "catalyst_played_out"]

_SCHEMA_REQUIRED = {"verdict": str, "reason": str}
_SCHEMA_ENUMS = {"verdict": ALLOWED_VERDICTS}

_DEFAULT_MIN_INTERVAL_MIN = 15  # 每仓最短复核间隔 (控成本/延迟)

# 节流: symbol -> 上次复核 epoch 秒
_last_recheck: Dict[str, float] = {}


def _min_interval_sec() -> float:
    cfg = get_feature_config("thesis_recheck") or {}
    try:
        return float(cfg.get("min_interval_min", _DEFAULT_MIN_INTERVAL_MIN)) * 60.0
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTERVAL_MIN * 60.0


def _throttle_ok(symbol: str) -> bool:
    """距上次复核已超过最短间隔返回 True。"""
    last = _last_recheck.get(symbol, 0.0)
    return (time.time() - last) >= _min_interval_sec()


def _mark_recheck(symbol: str) -> None:
    _last_recheck[symbol] = time.time()


def reset_throttle(symbol: Optional[str] = None) -> None:
    """测试/运维用: 清节流状态。"""
    if symbol is None:
        _last_recheck.clear()
    else:
        _last_recheck.pop(symbol, None)


def _read_invalidation(position: Any) -> Dict[str, str]:
    """best-effort 读 DA invalidation + thesis_validity_check。

    - thesis_validity_check: thesis_inferer 写入 trade_attribution.metadata_json。
    - invalidation: DA 直写 playbook_instructions.invalidation (本轮 attribution plan 落列)。
    读不到返回空串, 不抛异常。
    """
    out = {"invalidation": "", "thesis_validity_check": ""}
    trace_id = getattr(position, "trace_id", "") or ""
    symbol = getattr(position, "symbol", "") or ""

    # 1) thesis_validity_check ← trade_attribution.metadata_json
    try:
        import json as _json
        import sqlite3
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()), timeout=3)
        try:
            row = None
            if trace_id:
                row = conn.execute(
                    "SELECT metadata_json FROM trade_attribution WHERE trace_id = ? LIMIT 1",
                    (trace_id,),
                ).fetchone()
            if row and row[0]:
                meta = _json.loads(row[0])
                out["thesis_validity_check"] = str(meta.get("thesis_validity_check", ""))[:200]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("[ThesisRecheck] 读 thesis_validity_check 失败: %s", e)

    # 2) invalidation ← playbook_instructions (DA 直写)
    try:
        from app.storage.db_pool import get_db
        with get_db().connect("ftseek_us") as conn:
            row = conn.execute(
                "SELECT invalidation FROM playbook_instructions "
                "WHERE symbol = ? AND invalidation IS NOT NULL AND invalidation != '' "
                "ORDER BY rowid DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if row and row[0]:
                out["invalidation"] = str(row[0])[:300]
    except Exception as e:  # noqa: BLE001
        logger.debug("[ThesisRecheck] 读 invalidation 失败: %s", e)

    return out


def _get_recent_news(symbol: str) -> str:
    """best-effort 拉盘中新增新闻摘要; 取不到返回空串 (不影响复核, LLM 仅据价格行为判断)。"""
    try:
        from app.catalyst.detector import _get_recent_news as _grn  # type: ignore
        items = _grn(None, hours=6) or []
        texts = []
        for it in items:
            if symbol and symbol.upper() in str(it).upper():
                texts.append(str(it.get("title") or it.get("headline") or it)[:120])
        return " | ".join(texts[:3])
    except Exception:
        return ""


def _gather_context(position: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """收集复核输入。thesis 缺失是硬前提 (无 thesis 无从复核 → 上层 NO_ACTION)。"""
    symbol = getattr(position, "symbol", "") or ""
    thesis = _read_entry_thesis(position)
    if not thesis:
        return {}

    entry = float(getattr(position, "avg_entry_price", 0) or 0)
    cur = float((ctx or {}).get("current_price", 0) or 0)
    is_long = "LONG" in str(getattr(position, "direction", "LONG")).upper()
    pnl_pct = 0.0
    if entry > 0 and cur > 0:
        pnl_pct = (cur - entry) / entry if is_long else (entry - cur) / entry

    inv = _read_invalidation(position)
    return {
        "symbol": symbol,
        "direction": "LONG" if is_long else "SHORT",
        "thesis": thesis,
        "invalidation": inv["invalidation"],
        "thesis_validity_check": inv["thesis_validity_check"],
        "entry_price": entry,
        "current_price": cur,
        "pnl_pct": pnl_pct,
        "holding_hours": float(getattr(position, "holding_hours", 0) or 0),
        "recent_news": _get_recent_news(symbol),
    }


_SYSTEM = (
    "You are FTSeek's hold-time thesis auditor. Given the ORIGINAL entry thesis and its "
    "invalidation condition, plus fresh intraday news and price action, judge whether the "
    "reason to hold is STILL valid. You do NOT set prices or stops — you only classify. "
    "🚨 反幻觉: reason 必须引用具体信息 (价格/新闻/pnl), 禁止空泛。Output strict JSON, no extra text."
)


def _build_prompt(c: Dict[str, Any]) -> str:
    return f"""Position under review:
Symbol: {c.get('symbol')} {c.get('direction')}
Original thesis (why we entered): {c.get('thesis')}
Invalidation condition (plan-time): {c.get('invalidation') or 'n/a'}
What to monitor for invalidation: {c.get('thesis_validity_check') or 'n/a'}
Now: entry=${c.get('entry_price', 0):.2f} current=${c.get('current_price', 0):.2f} \
pnl={c.get('pnl_pct', 0) * 100:.1f}% held={c.get('holding_hours', 0):.1f}h
Fresh news (intraday): {c.get('recent_news') or 'none'}

Judge the hold-time thesis validity:
{{
  "verdict": "thesis_intact|thesis_broken|catalyst_played_out",
  "reason": "<= 120 chars (必须引用具体价格/新闻/pnl)"
}}

Rules:
- thesis_intact: original reason to hold still stands → keep holding (defer to mechanical exits)
- thesis_broken: invalidation condition met / narrative contradicted by new info → exit
- catalyst_played_out: the catalyst has already realized, edge is gone → reduce/de-risk"""


def _default_llm_call(c: Dict[str, Any]) -> Dict[str, Any]:
    """真实 LLM 调用 (yaml 路由 + schema 强校验 + 自动 audit)。返回归一化 dict。"""
    from app.llm.model_call import call_llm

    res = call_llm(
        "thesis_recheck",
        _build_prompt(c),
        system=_SYSTEM,
        temperature=0.0,
        max_tokens=200,
        schema_required=_SCHEMA_REQUIRED,
        schema_enums=_SCHEMA_ENUMS,
    )
    if not res or not res.success or not isinstance(res.response, dict):
        return {"success": False}
    obj = res.response
    return {
        "success": True,
        "verdict": obj.get("verdict"),
        "reason": str(obj.get("reason", ""))[:120],
        "model": res.model_used,
        "latency_ms": int(res.latency_ms),
        "cost_usd": round(res.cost_usd, 5),
    }


def _verdict_to_decision(verdict: str, reason: str, meta: Dict[str, Any]) -> ExitDecision:
    """类别 → action 映射。安全: 只产出 CLOSE/REDUCE/NO_ACTION, 未知一律 NO_ACTION。"""
    reason = (reason or "")[:120]
    if verdict == "thesis_broken":
        return ExitDecision(
            action="CLOSE", qty_pct=1.0,
            reason=f"THESIS_BROKEN:{reason}"[:160], priority=9, metadata=meta,
        )
    if verdict == "catalyst_played_out":
        return ExitDecision(
            action="REDUCE", qty_pct=0.5,
            reason=f"CATALYST_PLAYED_OUT:{reason}"[:160], priority=7, metadata=meta,
        )
    # thesis_intact / 未知 → 交回机械层
    return ExitDecision(
        action="NO_ACTION",
        reason=f"THESIS_INTACT:{reason}"[:160], metadata=meta,
    )


def recheck_thesis(
    position: Any,
    ctx: Optional[Dict[str, Any]] = None,
    *,
    force: bool = False,
    llm_call: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> ExitDecision:
    """持仓期 thesis 复核主入口。

    Args:
        position: 仓位对象 (需 .symbol/.trace_id/.avg_entry_price/.direction/.holding_hours)。
        ctx: PositionExitMonitor 传入的上下文 (含 current_price/atr)。
        force: 跳过 flag + 节流 (测试/手动复核用)。
        llm_call: 注入的 LLM 调用 (测试用); None → _default_llm_call。
        context: 注入的已收集上下文 (测试用); None → _gather_context。

    Returns:
        ExitDecision。任何异常/禁用/节流/无上下文/LLM 失败 → NO_ACTION (零退化)。
    """
    meta: Dict[str, Any] = {"policy": "thesis_recheck"}

    if not force and not is_enabled("thesis_recheck"):
        return ExitDecision(action="NO_ACTION", reason="thesis_recheck_disabled", metadata=meta)

    symbol = getattr(position, "symbol", "") or ""
    if not force and not _throttle_ok(symbol):
        return ExitDecision(action="NO_ACTION", reason="thesis_recheck_throttled", metadata=meta)

    try:
        c = context if context is not None else _gather_context(position, ctx or {})
    except Exception as e:  # noqa: BLE001
        logger.debug("[ThesisRecheck] %s 收集上下文异常: %s", symbol, e)
        return ExitDecision(action="NO_ACTION", reason="thesis_recheck_ctx_error", metadata=meta)

    if not c or not c.get("thesis"):
        return ExitDecision(action="NO_ACTION", reason="thesis_recheck_no_context", metadata=meta)

    call = llm_call or _default_llm_call
    try:
        res = call(c)
    except Exception as e:  # noqa: BLE001
        logger.debug("[ThesisRecheck] %s LLM 调用异常: %s", symbol, e)
        return ExitDecision(action="NO_ACTION", reason="thesis_recheck_llm_error", metadata=meta)

    if not res or not res.get("success"):
        return ExitDecision(action="NO_ACTION", reason="thesis_recheck_llm_fail", metadata=meta)

    verdict = str(res.get("verdict") or "")
    reason = str(res.get("reason") or "")
    _mark_recheck(symbol)

    meta.update({
        "thesis": c.get("thesis"),
        "verdict": verdict,
        "model": res.get("model"),
        "pnl_pct": c.get("pnl_pct"),
    })
    decision = _verdict_to_decision(verdict, reason, meta)
    if decision.action != "NO_ACTION":
        logger.info(
            "[ThesisRecheck] %s verdict=%s → %s (%s)",
            symbol, verdict, decision.action, reason[:80],
        )
    return decision


class ThesisRecheckExitPolicy(_BaseExitPolicy):
    """LLM 持仓期 thesis 复核 ExitPolicy (轨道B)。默认 flag OFF; NO_ACTION 时回退机械 6 规则。"""

    name = "thesis_recheck_exit"
    owner = "thesis_recheck"

    def evaluate(self, position: Any, ctx: Any) -> ExitDecision:
        return recheck_thesis(position, ctx if isinstance(ctx, dict) else {})
