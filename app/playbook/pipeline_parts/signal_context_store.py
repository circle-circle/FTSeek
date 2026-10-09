"""信号上下文存储域 (阶段 8 迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional


def _compute_key_levels(data: Any) -> Dict[str, Any]:
    """从 PlaybookData 计算支撑/阻力位; 异常返回空列表。"""
    try:
        fund = getattr(data, "fundamental", None)
        high_52w = getattr(fund, "high_52w", None) or 0 if fund else 0
        low_52w = getattr(fund, "low_52w", None) or 0 if fund else 0

        tech = getattr(data, "technical", None)
        ma20 = getattr(tech, "ma20", None) or 0 if tech else 0
        ma50 = getattr(tech, "ma50", None) or 0 if tech else 0

        supports = []
        resistances = []
        if low_52w > 0:
            supports.append(round(low_52w, 2))
        if ma50 > 0:
            supports.append(round(ma50, 2))
        if ma20 > 0:
            supports.append(round(ma20, 2))
        if high_52w > 0:
            resistances.append(round(high_52w, 2))

        supports = sorted(set(supports))
        resistances = sorted(set(resistances))
        return {"support": supports, "resistance": resistances}
    except Exception:
        return {"support": [], "resistance": []}


def _load_theme_context_cached(tc_path, *, cache: Optional[Dict[str, Any]] = None):
    """读 theme_context.json, 按 mtime 缓存; 失败返回 None。"""
    import json
    cache = cache if cache is not None else {"mtime": None, "data": None}
    path = Path(tc_path)
    try:
        mtime = path.stat().st_mtime
    except Exception:
        return None
    if cache.get("mtime") == mtime and cache.get("data") is not None:
        return cache.get("data")
    try:
        with path.open(encoding="utf-8") as source:
            cache["data"] = json.load(source)
        cache["mtime"] = mtime
        return cache.get("data")
    except Exception:
        return None


def _persist_signal_context(
    date: str,
    symbol: str,
    data: Any,
    vote_result: Optional[Dict] = None,
    *,
    module_file: str,
    compute_key_levels: Callable[[Any], Dict[str, Any]],
    load_theme_context: Callable,
    logger,
) -> None:
    """持久化信息层上下文; 单步异常隔离, 不影响主流程。"""
    try:
        from app.playbook.db import PlaybookDB
        from dataclasses import asdict, is_dataclass
        import json as _json

        db = PlaybookDB()
        ctx: Dict[str, Any] = {"date": date, "symbol": symbol}

        news = getattr(data, "news", None)
        if news:
            ctx["news_headlines"] = list(getattr(news, "headlines", []) or [])[:5]
            ctx["news_sentiment"] = getattr(news, "sentiment_score", 0) or 0

        insider = getattr(data, "insider", None)
        if insider:
            ctx["insider_signal"] = getattr(insider, "signal", "neutral")
            ctx["insider_cluster_score"] = getattr(insider, "cluster_score", 0) or 0
            ctx["insider_net_shares"] = getattr(insider, "net_shares_30d", 0) or 0
            ctx["insider_cluster_buyers"] = list(
                getattr(insider, "cluster_buyers", []) or []
            )

        earnings = getattr(data, "earnings", None)
        if earnings:
            ctx["earnings_next_date"] = getattr(earnings, "next_date", None)
            ctx["earnings_days_until"] = getattr(earnings, "days_until", None)
            ctx["earnings_beat_rate"] = getattr(earnings, "beat_rate", 0) or 0

        fundamental = getattr(data, "fundamental", None)
        if fundamental:
            ctx["pe"] = getattr(fundamental, "pe", None)
            ctx["market_cap"] = getattr(fundamental, "market_cap", None)
            ctx["industry"] = getattr(fundamental, "industry", None)
            ctx["revenue_growth_yoy"] = getattr(
                fundamental, "revenue_growth_yoy", None
            )
            ctx["roe"] = getattr(fundamental, "roe", None)
            ctx["debt_to_equity"] = getattr(fundamental, "debt_to_equity", None)
            ctx["high_52w"] = getattr(fundamental, "high_52w", None)
            ctx["low_52w"] = getattr(fundamental, "low_52w", None)

        try:
            root = Path(module_file).resolve().parent.parent.parent
            theme_context_path = root / "data" / "theme_context.json"
            theme_context = load_theme_context(theme_context_path)
            if theme_context is not None:
                bare_symbol = symbol.replace("US.", "")
                theme_symbols = theme_context.get("symbols") or {}
                if bare_symbol in theme_symbols:
                    symbol_value = theme_symbols[bare_symbol]
                    ctx["theme_momentum"] = symbol_value.get("net_momentum")
                    ctx["theme_wind"] = symbol_value.get("net_wind")
                    ctx["theme_narratives"] = list(
                        symbol_value.get("narratives", []) or []
                    )
                if "theme_momentum" not in ctx:
                    for symbol_domain in (
                        theme_symbols.get(bare_symbol, {}).get("domains") or []
                    ):
                        domain_name = symbol_domain.get("domain", "")
                        domain_data = (
                            theme_context.get("domains") or {}
                        ).get(domain_name, {})
                        if domain_data:
                            ctx["theme_momentum"] = domain_data.get("theme_momentum")
                            ctx["theme_wind"] = domain_data.get("sentiment_direction")
                            ctx["theme_narratives"] = list(
                                domain_data.get("key_narratives", []) or []
                            )[:3]
                            break
        except Exception:
            pass

        if vote_result:
            decision_id = vote_result.get("decision_id", "")
            if decision_id:
                try:
                    import sqlite3
                    root = Path(module_file).resolve().parent.parent.parent
                    decisions_db = root / "data" / "decisions.db"
                    if decisions_db.exists():
                        connection = sqlite3.connect(str(decisions_db))
                        connection.row_factory = sqlite3.Row
                        rows = connection.execute(
                            "SELECT model_id, direction, strength, confidence, reasoning "
                            "FROM model_votes WHERE decision_id = ?",
                            (decision_id,),
                        ).fetchall()
                        connection.close()
                        ctx["llm_reasoning"] = [
                            {
                                "model_id": row["model_id"],
                                "direction": row["direction"],
                                "strength": row["strength"],
                                "confidence": row["confidence"],
                                "reasoning": row["reasoning"] or "",
                            }
                            for row in rows
                        ]
                except Exception:
                    pass

        try:
            raw_data = asdict(data) if is_dataclass(data) else {}
        except Exception:
            raw_data = {}

        raw_data["key_levels"] = compute_key_levels(data)

        try:
            reasoning_value = ""
            if vote_result:
                reasoning_value = str(vote_result.get("reasoning", "") or "")
            elif ctx.get("llm_reasoning"):
                first = (
                    ctx["llm_reasoning"][0]
                    if isinstance(ctx["llm_reasoning"], list) else {}
                )
                reasoning_value = (
                    str(first.get("reasoning", "") or "")
                    if isinstance(first, dict) else ""
                )
            if "|" in reasoning_value:
                json_part = reasoning_value.rsplit("|", 1)[-1].strip()
                brief_data = (
                    _json.loads(json_part) if json_part.startswith("{") else {}
                )
                for brief_field in (
                    "brief_conclusion", "brief_reasons", "brief_risks",
                    "entry_strategy", "key_support", "key_resistance", "stop_trigger",
                ):
                    if brief_field in brief_data:
                        raw_data[brief_field] = brief_data[brief_field]
        except Exception:
            pass

        ctx["raw_data"] = raw_data
        db.save_signal_context(ctx)
    except Exception as error:
        logger.warning(
            f"[playbook] signal_context 持久化失败 {symbol}: {error}"
        )
