"""
Entry Thesis 推断 (L3 / F16, 2026-05-14)

入场后异步调 LLM 推断"为什么入场", 写到 trade_attribution.entry_thesis.
ExitDispatcher 据此选择对应 ExitPolicy.

Thesis 枚举 (5 类):
- momentum     : 动量/趋势延续
- breakout     : 突破/前高新高
- reversion    : 反转/超卖反弹
- event        : 事件驱动 (财报/FDA/新闻)
- technical_setup : 技术形态 (MA crossover / 三角整理 / RSI 背离)

可控:
- 默认 disabled
- 异步 fire-and-forget (不阻塞下单)
- 输出枚举强制, 否则 fallback "momentum"
- 超时 15s
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Any, Dict, Optional

from app.llm.features.feature_flags import is_enabled

logger = logging.getLogger(__name__)

ALLOWED_THESIS = ["momentum", "breakout", "reversion", "event", "technical_setup"]

# 反幻觉 schema (强校验, 非法 → fallback NO_ACTION)
_THESIS_SCHEMA_REQUIRED = {
    "thesis": str,
    "key_evidence": str,
    "expected_horizon_hours": int,
    "thesis_validity_check": str,
}
_THESIS_SCHEMA_ENUMS = {"thesis": ALLOWED_THESIS}


def infer_thesis_async(symbol: str, signal_dict: Dict[str, Any], context: Dict[str, Any]):
    """异步推断 thesis. 不阻塞下单. 失败默认 momentum."""
    if not is_enabled("thesis_inferer"):
        return

    def _run():
        try:
            thesis_data = _infer_sync(symbol, signal_dict, context)
            if thesis_data:
                _persist(symbol, signal_dict.get("trace_id", ""), thesis_data)
        except Exception as e:
            logger.debug(f"[ThesisInferer] {symbol} 异常 (fallback momentum): {e}")

    thread = threading.Thread(target=_run, daemon=True, name=f"ThesisInferer-{symbol}")
    thread.start()


def _infer_sync(symbol: str, signal: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """同步推断 (异步线程内调用). 2026-05-17 迁移到 model_call.call_llm.

    变化:
      - 旧: llm_helper.call_llm(feature, model="deepseek") + 二次 parse_and_validate
      - 新: model_call.call_llm(feature, schema_required=...) 内置校验, dict 直接返回
      - model 由 yaml `llm.features.thesis_inferer` 决定 (默认 deepseek-flash)
    """
    direction = signal.get("direction", "LONG")
    strength = signal.get("strength", 50)
    confidence = signal.get("confidence", 0.5)
    factors = signal.get("factor_detail", {})

    # 简洁 ctx 摘要
    ctx_brief = (
        f"price=${context.get('price', 0):.2f}, "
        f"rsi={context.get('rsi', 50):.0f}, "
        f"macd={context.get('macd', 0):.2f}, "
        f"vol_ratio={context.get('vol_ratio', 1.0):.1f}, "
        f"vix={context.get('vix', 20):.1f}, "
        f"is_earnings_period={context.get('is_earnings_period', False)}, "
        f"high_20d={context.get('high_20d', 0):.2f}, "
        f"low_20d={context.get('low_20d', 0):.2f}, "
        f"news_sentiment={context.get('news_sentiment', 0):.2f}"
    )

    factor_brief = ", ".join(
        f"{k}={v.get('raw', 0):+.0f}" for k, v in factors.items() if isinstance(v, dict)
    )[:300]

    # 反幻觉 citation 强化 — 必须基于具体数值
    system = (
        "You are FTSeek's entry thesis classifier. "
        "Given an entry decision, identify the most likely thesis. "
        "🚨 反幻觉: key_evidence 字段必须引用上文具体数值 (例: 'RSI=72, MA20 上方', "
        "'price $145 > 20d_high $143'), 禁止空泛描述如 'strong momentum'. "
        "Output strict JSON, no extra text."
    )
    prompt = f"""Entry decision:
Symbol: {symbol} {direction}
Strength: {strength}, Confidence: {confidence:.2f}
Factor scores: {factor_brief or 'unknown'}
Context: {ctx_brief}

Classify the entry thesis:
{{
  "thesis": "momentum|breakout|reversion|event|technical_setup",
  "key_evidence": "<= 80 chars (必须引用具体数值)",
  "expected_horizon_hours": int (1-72),
  "thesis_validity_check": "<= 80 chars (what to monitor for invalidation)"
}}

Rules:
- momentum: existing trend (MA aligned, RSI rising)
- breakout: price > recent 20d high or breaking resistance
- reversion: oversold bounce (RSI < 30) or overbought fade
- event: earnings/FDA/news driven
- technical_setup: chart pattern (triangle, flag, divergence)"""

    # 新统一入口: yaml 路由 + schema 强校验 + 自动 audit
    from app.llm.model_call import call_llm
    res = call_llm(
        "thesis_inferer",
        prompt,
        system=system,
        temperature=0.0,
        max_tokens=300,
        schema_required=_THESIS_SCHEMA_REQUIRED,
        schema_enums=_THESIS_SCHEMA_ENUMS,
    )
    if not res or not res.success:
        # success=False: 模型失败 / schema 校验未过 → fallback NO_ACTION 由 ExitPolicy 兜底
        return None

    obj = res.response   # 已 parse + validated 的 dict
    if not isinstance(obj, dict):
        return None

    # 钳位 (防 LLM 输出超出合理范围)
    try:
        obj["expected_horizon_hours"] = max(1, min(72, int(obj["expected_horizon_hours"])))
    except Exception:
        obj["expected_horizon_hours"] = 24
    obj["key_evidence"] = str(obj.get("key_evidence", ""))[:160]
    obj["thesis_validity_check"] = str(obj.get("thesis_validity_check", ""))[:160]
    # 附加 audit 元数据 (供复盘对账)
    obj["_model_used"] = res.model_used
    obj["_latency_ms"] = int(res.latency_ms)
    obj["_cost_usd"] = round(res.cost_usd, 5)
    obj["_fallback_used"] = res.fallback_used
    return obj


def _persist(symbol: str, trace_id: str, thesis_data: Dict[str, Any]):
    """更新 trade_attribution.entry_thesis"""
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        # entry_thesis 字段已在 F19 schema 中存在
        # metadata_json 写完整 thesis_data
        import json as _json
        meta_json = _json.dumps(thesis_data, ensure_ascii=False)
        if trace_id:
            conn.execute(
                "UPDATE trade_attribution SET entry_thesis = ?, metadata_json = ? "
                "WHERE trace_id = ?",
                (thesis_data["thesis"], meta_json, trace_id),
            )
        else:
            # 兜底 — 用 symbol + 最近一条
            conn.execute(
                "UPDATE trade_attribution SET entry_thesis = ?, metadata_json = ? "
                "WHERE symbol = ? AND trade_date = date('now') "
                "ORDER BY created_at DESC LIMIT 1",
                (thesis_data["thesis"], meta_json, symbol),
            )
        conn.commit()
        conn.close()
        logger.info(
            f"[ThesisInferer] {symbol} thesis={thesis_data['thesis']} "
            f"horizon={thesis_data['expected_horizon_hours']}h "
            f"evidence={thesis_data['key_evidence'][:60]}"
        )
    except Exception as e:
        logger.debug(f"[ThesisInferer·persist] {symbol} 失败: {e}")
