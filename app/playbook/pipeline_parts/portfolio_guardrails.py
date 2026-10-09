"""组合闸域 (阶段 3b 迁自 app/playbook/pipeline.py)。

迁移纪律: 判定顺序、默认参数、影子输出 schema 与 monkeypatch 缝不变。
pipeline 转发器通过显式回调注入旧公开名, 保证测试替换仍作用于迁移后的本体。
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.playbook.pipeline_parts.instruction_builder import (
    _trim_majority_direction,
    _trim_majority_sector,
)

logger = logging.getLogger(__name__)

_VAULT_INDUSTRY_CACHE: Optional[Dict[str, str]] = None
_VAULT_INDUSTRY_CACHE_TS: float = 0.0


def _get_vault_industry_map(max_age_s: float = 3600.0) -> Dict[str, str]:
    """obsidian_vault/symbols/*.md frontmatter industry → {US.MU: Semiconductors}"""
    global _VAULT_INDUSTRY_CACHE, _VAULT_INDUSTRY_CACHE_TS
    now = time.time()
    if _VAULT_INDUSTRY_CACHE is not None and (
        now - _VAULT_INDUSTRY_CACHE_TS
    ) < max_age_s:
        return _VAULT_INDUSTRY_CACHE

    import re
    industry_map: Dict[str, str] = {}
    try:
        vault_dir = Path(__file__).resolve().parents[3] / "obsidian_vault" / "symbols"
        pattern = re.compile(r'^industry:\s*"?([^"\n]+)"?', re.M)
        if vault_dir.is_dir():
            for vault_file in vault_dir.glob("*.md"):
                try:
                    head = vault_file.read_text(
                        encoding="utf-8", errors="ignore"
                    )[:2000]
                except Exception:
                    continue
                match = pattern.search(head)
                if match and match.group(1).strip():
                    industry_map[
                        vault_file.stem.replace("_", ".")
                    ] = match.group(1).strip()
    except Exception as error:
        logger.warning(
            f"[playbook] vault industry 读取失败 (fail-open, sector 维持原值): {error}"
        )
    _VAULT_INDUSTRY_CACHE = industry_map
    _VAULT_INDUSTRY_CACHE_TS = now
    return industry_map


def portfolio_guardrail_enabled() -> bool:
    """组合层方向/行业闸总开关; 配置失败 fail-safe 保持开启。"""
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(
            "playbook.portfolio_guardrail_enabled", False
        ))
    except Exception as error:
        logger.warning(
            f"[playbook] 组合闸开关读取失败 (fail-safe 保持开启): {error}",
            exc_info=True,
        )
        return True


def _apply_portfolio_guardrails(
    instructions: List[Any],
    *,
    max_sector_pct: float = 0.40,
    max_direction_skew: float = 0.80,
    min_rr: float = 1.0,
    today: str = "",
    emit_shadow: Optional[Callable] = None,
    guardrail_enabled: Optional[Callable[[], bool]] = None,
    industry_map_provider: Optional[Callable[[], Dict[str, str]]] = None,
    trim_direction: Optional[Callable] = None,
    trim_sector: Optional[Callable] = None,
) -> List[Any]:
    """组合级风险 guardrail; F1 摘闸后影子照算、方向/行业不砍。"""
    from collections import Counter

    emit_shadow = emit_shadow or _emit_skew_gate_vision_shadow
    guardrail_enabled = guardrail_enabled or portfolio_guardrail_enabled
    industry_map_provider = (
        industry_map_provider or _get_vault_industry_map
    )
    trim_direction = trim_direction or _trim_majority_direction
    trim_sector = trim_sector or _trim_majority_sector

    active = []
    for inst in instructions:
        if getattr(inst, "direction", None) in ("SKIP", None, ""):
            active.append(inst)
            continue
        risk_reward = getattr(inst, "risk_reward", 0) or 0
        if risk_reward < min_rr:
            logger.warning(
                f"[playbook guardrail] {inst.symbol} R:R "
                f"{risk_reward:.2f} < {min_rr}, 转为 SKIP"
            )
            inst.direction = "SKIP"
            inst.skip_reason = f"R:R {risk_reward:.2f} < 最低 {min_rr}"
        active.append(inst)

    valid = [
        inst for inst in active
        if inst.direction in ("BUY", "SELL_SHORT")
    ]
    if valid:
        industry_map = industry_map_provider()
        for inst in valid:
            if not (getattr(inst, "sector", "") or ""):
                vault_sector = industry_map.get(
                    getattr(inst, "symbol", ""), ""
                )
                if vault_sector:
                    inst.sector = vault_sector

    if len(valid) <= 1:
        return active

    try:
        emit_shadow(valid, today, max_direction_skew, max_sector_pct)
    except Exception as error:
        logger.warning(
            f"[playbook guardrail] 偏斜门影子调用失败 (不影响判定): {error}",
            exc_info=True,
        )

    if not guardrail_enabled():
        return active

    total = len(valid)
    buy_count = sum(1 for inst in valid if inst.direction == "BUY")
    short_count = total - buy_count
    majority_pct = max(buy_count, short_count) / total if total > 0 else 0

    if majority_pct > max_direction_skew:
        majority_dir = "BUY" if buy_count > short_count else "SELL_SHORT"
        logger.warning(
            f"[playbook guardrail] 方向偏斜 {majority_pct:.0%} 超限, "
            f"{trim_direction(valid, majority_dir, max_direction_skew)} "
            f"条信号被降级为 SKIP"
        )

    sectors = Counter(getattr(inst, "sector", "") or "" for inst in valid)
    for sector, count in sectors.items():
        if not sector or sector == "Unknown":
            continue
        if count / total > max_sector_pct:
            logger.warning(
                f"[playbook guardrail] sector {sector} 占比 "
                f"{count / total:.0%} 超限, "
                f"{trim_sector(valid, sector, max_sector_pct)} "
                f"条信号被降级为 SKIP"
            )

    return active


def _emit_skew_gate_vision_shadow(
    valid: List[Any],
    today: str,
    max_direction_skew: float,
    max_sector_pct: float = 0.40,
    *,
    module_file: Optional[str] = None,
) -> None:
    """偏斜门双口径只读影子; 零交易行为变更。"""
    try:
        if not today:
            return
        batch_buy = sum(1 for inst in valid if inst.direction == "BUY")
        batch_short = sum(
            1 for inst in valid if inst.direction == "SELL_SHORT"
        )
        batch_total = batch_buy + batch_short
        if batch_total == 0:
            return
        batch_pct = max(batch_buy, batch_short) / batch_total
        batch_violates = (
            batch_total > 1 and batch_pct > max_direction_skew
        )

        active = [
            inst for inst in valid
            if inst.direction in ("BUY", "SELL_SHORT")
        ]
        sector_counts: Dict[str, int] = {}
        for inst in active:
            sector = str(getattr(inst, "sector", "") or "")
            if sector and sector != "Unknown":
                sector_counts[sector] = sector_counts.get(sector, 0) + 1

        sector_violations = []
        for sector, count in sorted(sector_counts.items()):
            pct = count / batch_total if batch_total else 0.0
            if pct <= max_sector_pct:
                continue
            would_skip_count = math.ceil(
                (count - max_sector_pct * batch_total)
                / max(1.0 - max_sector_pct, 1e-9)
            )
            candidates = [
                inst for inst in active
                if str(getattr(inst, "sector", "") or "") == sector
            ]
            candidates.sort(
                key=lambda inst: getattr(inst, "confidence", 0) or 0
            )
            sector_violations.append({
                "sector": sector,
                "count": count,
                "pct": round(pct, 4),
                "threshold": max_sector_pct,
                "would_skip_count": would_skip_count,
                "would_skip_symbols": [
                    str(getattr(inst, "symbol", "") or "")
                    for inst in candidates[:would_skip_count]
                ],
            })

        from app.playbook.db import PlaybookDB
        rows = PlaybookDB().get_instructions_by_date(today)
        batch_symbols = {getattr(inst, "symbol", "") for inst in valid}
        prior_buy = prior_short = 0
        for row in rows:
            from app.playbook.pipeline import INVALIDATED_SKIP_REASONS
            if (row.get("skip_reason") or "") in INVALIDATED_SKIP_REASONS:
                continue
            if row.get("symbol") in batch_symbols:
                continue
            direction = row.get("direction")
            if direction == "BUY":
                prior_buy += 1
            elif direction == "SELL_SHORT":
                prior_short += 1
        portfolio_buy = batch_buy + prior_buy
        portfolio_short = batch_short + prior_short
        portfolio_total = portfolio_buy + portfolio_short
        portfolio_pct = (
            max(portfolio_buy, portfolio_short) / portfolio_total
            if portfolio_total else 0.0
        )
        portfolio_violates = (
            portfolio_total > 1 and portfolio_pct > max_direction_skew
        )

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "date": today,
            "batch_vision": {
                "buy": batch_buy,
                "short": batch_short,
                "pct": round(batch_pct, 4),
                "violates": batch_violates,
            },
            "portfolio_vision": {
                "buy": portfolio_buy,
                "short": portfolio_short,
                "prior_buy": prior_buy,
                "prior_short": prior_short,
                "pct": round(portfolio_pct, 4),
                "violates": portfolio_violates,
            },
            "divergent": bool(batch_violates != portfolio_violates),
            "batch_symbols": sorted(
                symbol for symbol in batch_symbols if symbol
            ),
            "sector_vision": {
                "counts": dict(sorted(sector_counts.items())),
                "violations": sector_violations,
                "threshold": max_sector_pct,
            },
            "threshold": max_direction_skew,
            "note": "record-only; 判定仍用 batch_vision (RFC-122 阶段0)",
        }
        output_file = (
            Path(module_file or __file__).resolve().parents[2]
            / "data" / "shadow" / "skew_gate_vision.jsonl"
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
        if record["divergent"]:
            logger.info(
                f"[playbook guardrail] 偏斜门双口径分歧 (record-only): "
                f"batch {batch_pct:.0%}({'砍' if batch_violates else '放'}) vs "
                f"portfolio {portfolio_pct:.0%}({'砍' if portfolio_violates else '放'}) "
                f"prior B{prior_buy}/S{prior_short} — 判定仍按 batch"
            )
    except Exception as error:
        logger.warning(
            f"[playbook guardrail] 偏斜门影子双算失败 (不影响判定): {error}",
            exc_info=True,
        )
