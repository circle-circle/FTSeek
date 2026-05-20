"""Pre-Entry Signal Gate (2026-05-19)

第一性原则: LLM 应做量化做不到的事 — 用 LLM 拦截"看历史就该拒"的入场信号.

替代 thesis_inferer 现状的"事后 tag" — 改为入场前的"事前 gate".
直接影响交易决策: gate.go=False → 拒绝入场, 跳过整笔 trade.

设计安全要点:
1. 默认 disabled (yaml.llm_features.pre_entry_gate.enabled=false)
2. LLM 失败 → go=True (向后兼容, 不阻塞主线)
3. **硬超时** (默认 5s) — ThreadPoolExecutor + future.result(timeout) 防 model_call retry/fallback 拖延
4. 5min 缓存同 symbol+direction (减成本 + 减延迟)
5. 历史 < 5 笔时不调 LLM (数据不足)

5/12-5/18 数据验证场景:
- LCID LONG 14 笔历史 0 胜率 → LLM 应 NO
- ARM SHORT main 1.2h trail 历史 +$1219 → LLM 应 YES
- 全新 symbol (无历史) → fallback go=True (不阻塞机会)
"""
from __future__ import annotations
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.llm.features.feature_flags import is_enabled, get_feature_config

logger = logging.getLogger("ftseek_us.llm.pre_entry_gate")

# 单一 ThreadPool 用于 LLM 调用硬超时 — 避免每次 entry 创建新线程
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """惰性初始化 ThreadPool, 用于 LLM 调用硬超时. max_workers=2 够用."""
    global _executor
    if _executor is not None:
        return _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="PreEntryGate")
    return _executor


@dataclass
class GateDecision:
    """LLM 入场前判定结果"""
    go: bool                    # 是否允许入场 (失败默认 True)
    reason: str = ""            # LLM 给出的拒绝/允许理由
    confidence: float = 0.0     # LLM 自评置信度 0-1
    source: str = "fallback"    # "llm" | "cache" | "fallback" | "no_history"
    latency_ms: int = 0


_GATE_SCHEMA_REQUIRED = {
    "go": bool,
    "reason": str,
    "confidence": float,
}

# 进程内缓存: (symbol, direction) → (decision, expire_ts)
_cache: Dict[Tuple[str, str], Tuple[GateDecision, float]] = {}


def _get_cache(symbol: str, direction: str, ttl: int) -> Optional[GateDecision]:
    key = (symbol, direction)
    entry = _cache.get(key)
    if entry is None:
        return None
    decision, expire_ts = entry
    if time.time() > expire_ts:
        _cache.pop(key, None)
        return None
    cached = GateDecision(
        go=decision.go, reason=decision.reason, confidence=decision.confidence,
        source="cache", latency_ms=0,
    )
    return cached


def _set_cache(symbol: str, direction: str, decision: GateDecision, ttl: int):
    _cache[(symbol, direction)] = (decision, time.time() + ttl)


def _query_history(symbol: str, direction: str, days: int = 7) -> Dict[str, Any]:
    """查 trade_records 该 symbol+direction 7 天历史表现.

    Returns:
        {"trades": int, "wins": int, "win_rate": float, "total_pnl": float, "avg_pnl": float,
         "sample_exit_reasons": [str, ...], "sample_holding_hours": [float, ...]}
    """
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT realized_pnl, exit_reason, holding_hours, trade_date
            FROM trade_records
            WHERE symbol = ? AND direction = ? AND realized_pnl IS NOT NULL
              AND date(entry_time) >= date('now', ?)
            ORDER BY id DESC
            LIMIT 30
            """,
            (symbol, direction, f"-{int(days)} days"),
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return {"trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                    "sample_exit_reasons": [], "sample_holding_hours": []}
        pnls = [r[0] for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "trades": len(rows),
            "wins": wins,
            "win_rate": round(wins / len(rows), 3),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(rows), 2),
            "sample_exit_reasons": [r[1] for r in rows[:5] if r[1]],
            "sample_holding_hours": [round(r[2], 1) for r in rows[:5] if r[2]],
        }
    except Exception as e:
        logger.debug(f"[PreEntryGate·history] {symbol}/{direction} 查询失败: {e}")
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "sample_exit_reasons": [], "sample_holding_hours": []}


def _build_prompt(symbol: str, direction: str, signal: Dict[str, Any],
                   history: Dict[str, Any], market_ctx: Dict[str, Any]) -> Tuple[str, str]:
    """构造 system + user prompt. 重点是历史失败/成功模式 + 当前 context."""
    system = (
        "You are a quantitative trading risk analyst. Your job: decide if a trade should proceed "
        "based on historical performance and current market context.\n\n"
        "Decision principle:\n"
        "- Reject if symbol+direction has 7d win_rate <= 20% AND >=5 trades (proven failure mode).\n"
        "- Reject if regime CONTRADICTS direction (e.g., LONG in TREND_DOWN).\n"
        "- Reject if avg_pnl is consistently negative (<-$30) over 5+ trades.\n"
        "- Approve if 7d performance positive OR regime aligned.\n"
        "- When in doubt with limited data, lean toward approve (avoid missing alpha).\n\n"
        "Output strict JSON: {\"go\": bool, \"reason\": str (<=80 chars, cite specific data), "
        "\"confidence\": float 0-1}"
    )
    user = (
        f"Trade candidate:\n"
        f"  symbol={symbol} direction={direction} strength={signal.get('strength', 0)} "
        f"confidence={signal.get('confidence', 0):.2f}\n\n"
        f"7-day history ({symbol} {direction}):\n"
        f"  trades={history['trades']} wins={history['wins']} "
        f"win_rate={history['win_rate']:.1%} total_pnl=${history['total_pnl']} "
        f"avg_pnl=${history['avg_pnl']}\n"
        f"  recent_exits={history['sample_exit_reasons']}\n"
        f"  recent_holding_hours={history['sample_holding_hours']}\n\n"
        f"Market context:\n"
        f"  regime={market_ctx.get('regime', 'UNKNOWN')} "
        f"vix={market_ctx.get('vix', 0):.1f} "
        f"spy_5m_momentum={market_ctx.get('spy_momentum_pct', 0):.2f}%\n\n"
        f"Decide: should this trade proceed?"
    )
    return system, user


def check_gate(
    symbol: str,
    direction: str,
    signal: Dict[str, Any],
    market_ctx: Optional[Dict[str, Any]] = None,
) -> GateDecision:
    """主入口: 在 _execute_long/short 之前调.

    Args:
        symbol: 标的代码 (e.g., "US.LCID")
        direction: "LONG" or "SHORT"
        signal: {"strength": int, "confidence": float, "factor_detail": dict, ...}
        market_ctx: {"regime": str, "vix": float, "spy_momentum_pct": float, ...}

    Returns:
        GateDecision (失败时 go=True)
    """
    # 1. Feature flag 检查 — 默认 disabled 不影响线上
    if not is_enabled("pre_entry_gate"):
        return GateDecision(go=True, reason="gate_disabled", source="fallback")

    cfg = get_feature_config("pre_entry_gate")
    cache_ttl = int(cfg.get("cache_ttl_sec", 300))
    min_history = int(cfg.get("min_history_trades", 5))
    timeout_sec = int(cfg.get("timeout_sec", 5))

    # 2. 缓存命中 — 同 symbol+direction 5min 内复用
    cached = _get_cache(symbol, direction, cache_ttl)
    if cached:
        logger.debug(f"[PreEntryGate·cache] {symbol}/{direction} hit: go={cached.go}")
        return cached

    # 3. 历史数据查询
    history = _query_history(symbol, direction, days=7)

    # 4. 历史不足 → 放行 (避免新机会被拒)
    if history["trades"] < min_history:
        decision = GateDecision(
            go=True,
            reason=f"insufficient_history ({history['trades']}<{min_history})",
            confidence=0.5,
            source="no_history",
        )
        _set_cache(symbol, direction, decision, cache_ttl)
        return decision

    # 5. LLM 调用 (yaml.llm.features.pre_entry_gate 路由)
    # 硬超时: model_call 内部有 retry + fallback 可能 ~22s, 主线 entry 不能等.
    # 用 ThreadPool + future.result(timeout) 强制超时, 主线 ≤ timeout_sec 必返回.
    t0 = time.time()

    def _run_llm():
        from app.llm.model_call import call_llm
        system, user = _build_prompt(symbol, direction, signal, history, market_ctx or {})
        return call_llm(
            "pre_entry_gate",
            user,
            system=system,
            temperature=0.0,
            max_tokens=200,
            timeout=timeout_sec,  # model_call 内部单次超时
            schema_required=_GATE_SCHEMA_REQUIRED,
        )

    try:
        future = _get_executor().submit(_run_llm)
        # 硬超时 (含外层 buffer): timeout_sec + 1s 给单次 LLM, 不等 retry/fallback
        res = future.result(timeout=timeout_sec + 1)
    except FutureTimeout:
        latency_ms = int((time.time() - t0) * 1000)
        logger.warning(
            f"⏱️ [PreEntryGate·HARD_TIMEOUT] {symbol}/{direction} "
            f"超 {timeout_sec + 1}s 强制返回, 主线放行 ({latency_ms}ms)"
        )
        # 短缓存 1min, 防止持续超时反复调
        decision = GateDecision(
            go=True, reason=f"hard_timeout_{timeout_sec+1}s",
            source="fallback", latency_ms=latency_ms,
        )
        _set_cache(symbol, direction, decision, 60)
        return decision
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        logger.warning(f"[PreEntryGate·exception] {symbol}/{direction} {e}, 默认放行")
        return GateDecision(go=True, reason=f"exception:{type(e).__name__}",
                              source="fallback", latency_ms=latency_ms)

    latency_ms = int((time.time() - t0) * 1000)
    if not res or not res.success or not isinstance(res.response, dict):
        # LLM 失败 → 降级放行 (核心安全保证)
        logger.warning(
            f"[PreEntryGate·fallback] {symbol}/{direction} LLM 失败 "
            f"(error={res.error if res else 'no_result'}), 默认放行"
        )
        decision = GateDecision(go=True, reason="llm_failed", source="fallback",
                                 latency_ms=latency_ms)
        # 短缓存 (1min) 防止持续失败时反复调 LLM
        _set_cache(symbol, direction, decision, 60)
        return decision

    obj = res.response
    decision = GateDecision(
        go=bool(obj["go"]),
        reason=str(obj.get("reason", ""))[:120],
        confidence=float(obj.get("confidence", 0.5)),
        source="llm",
        latency_ms=latency_ms,
    )
    _set_cache(symbol, direction, decision, cache_ttl)

    # 决策日志 — 拒绝时 WARNING (重要事件), 放行时 INFO
    if decision.go:
        logger.info(
            f"[PreEntryGate·GO] {symbol}/{direction} "
            f"({history['win_rate']:.0%}/{history['trades']}笔历史) "
            f"reason={decision.reason} ({latency_ms}ms)"
        )
    else:
        logger.warning(
            f"🚫 [PreEntryGate·NO] {symbol}/{direction} "
            f"({history['win_rate']:.0%}/{history['trades']}笔历史) "
            f"reason={decision.reason} ({latency_ms}ms)"
        )
    return decision


def reset_cache():
    """测试用 — 清空进程内缓存"""
    _cache.clear()
