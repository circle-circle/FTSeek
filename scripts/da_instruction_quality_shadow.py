#!/usr/bin/env python3
"""DA 指令质量日报告影子 — 只读、只度量、零生产耦合 (record-only)。

回答的问题
==========
当天生产的 DA 检查点 (`data/da_checkpoint/<ET 日期>/US.*.json`) 里, 模型产出的
`_llm_output` 指令卡在**结构质量**上是否健康:

  · `valid_card`                   direction ∈ {BUY, SELL_SHORT, SKIP}
  · `research_complete`            摘要/因果链/多空论据齐全
  · `direction_plan_consistent`    SKIP 时执行字段全空; 方向单时动作不是 SKIP
  · `price_geometry_valid`         BUY: stop < entry < tp; SELL_SHORT: tp < entry < stop
  · `skip_count / skip_inconsistent`
  · `direction_counts`             方向分布
  · `per-symbol reasons`           每只标的的机器可读原因 (为何不合格 / 为何跳过)

价格类指标 (`reachable / hit / fwd`) 只在**本地 bars 存在**时计算; bars 缺失时一律
`null`, **不伪造任何价格读数** (复用 `scripts/da_model_ab_replay.py` 的 `score()` /
`day_bars()`, 判据不另抄一份)。

边界 (刻意不做的事)
==================
  · 零付费调用 (不 import / 不调用任何 LLM 客户端);
  · 零生产 DB 写入 (不 import sqlite / app 存储层);
  · 零 gate / 零下单 / 零配置与注册表改动;
  · 只写两处证据: 不可变历史 `data/shadow/da_instruction_quality/<date>/<hash>.json`
    与原子 `latest.json`; 同内容重跑不产生新哈希、不覆盖历史。

开关
====
`DA_INSTRUCTION_QUALITY_SHADOW` 默认 **false** (读 `.env.us` / 进程环境, 由
`scripts/shadow_runtime.env_flag` 统一解释)。关闭时: 打印 `DISABLED`, 返回 0,
除读开关本身外**不读任何文件、不写任何文件、不创建目录**。

用法
====
  venv/bin/python scripts/da_instruction_quality_shadow.py
  venv/bin/python scripts/da_instruction_quality_shadow.py --date 2026-11-12 --json
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:          # 允许 `python scripts/...` 与测试包导入两种入口
    sys.path.insert(0, str(ROOT))

ENV_FLAG = "DA_INSTRUCTION_QUALITY_SHADOW"
SCHEMA = "da_instruction_quality_shadow_v1"
DEFAULT_CHECKPOINT_ROOT = ROOT / "data" / "da_checkpoint"
DEFAULT_OUT_ROOT = ROOT / "data" / "shadow" / "da_instruction_quality"
EXCLUDED_NAME_MARKERS = (".raw", ".bak", ".tmp", ".old", ".orig")

CAVEATS = (
    "只做结构质量度量; 不计算反事实 PnL, 不假设任何指令会赚/亏。",
    "方向与执行计划一致性是契约判据, 不是收益判据。",
    "本地 bars 缺失时 reachable/hit/fwd 为 null, 不伪造价格读数。",
    "WARN 只是报告态, 不 gate、不下单、不改变任何生产行为。",
)


# ── 开关 ────────────────────────────────────────────────────────────────────

def _shadow_runtime():
    """延迟导入共享原语, 关闭路径不触发任何数据模块加载。"""
    from scripts import shadow_runtime
    return shadow_runtime


def env_enabled() -> bool:
    """`DA_INSTRUCTION_QUALITY_SHADOW`, 默认 false, 每次运行实读。"""
    return _shadow_runtime().env_flag(ENV_FLAG)


# ── 输入 ────────────────────────────────────────────────────────────────────

def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def list_card_files(checkpoint_dir: Path) -> List[Path]:
    """`US.*.json`, 排除 raw/bak/tmp 等旁路副本。目录缺失 → 空列表 (NO_DATA)。"""
    if not checkpoint_dir.exists():
        return []
    files: List[Path] = []
    for path in sorted(checkpoint_dir.glob("US.*.json")):
        name = path.name
        if any(marker in name for marker in EXCLUDED_NAME_MARKERS):
            continue
        files.append(path)
    return files


def _symbol_from_name(name: str) -> str:
    return name[:-5] if name.endswith(".json") else name


def _extract_card(outer: Any, replay: Any) -> Tuple[Optional[dict], Optional[str]]:
    """取 `_llm_output` 指令卡; direction 缺失时回退 action。

    返回 (card, reason)。reason 只在取卡失败时有值, 用于 per-symbol 归因。
    """
    if not isinstance(outer, dict):
        return None, "checkpoint_not_object"
    raw = outer.get("_llm_output")
    if raw is None:
        return None, "missing_llm_output"
    if isinstance(raw, str):
        card = replay.parse_card(raw)
        if not isinstance(card, dict):
            return None, "invalid_llm_output"
    elif isinstance(raw, dict):
        card = dict(raw)
    else:
        return None, "invalid_llm_output"

    if not str(card.get("direction") or "").strip():
        fallback = card.get("action") or outer.get("action")
        if fallback not in (None, ""):
            card["direction"] = fallback
    return card, None


def _reasons_for(score: dict, card: Optional[dict], bars_available: bool,
                 extract_reason: Optional[str], read_error: Optional[str]) -> List[str]:
    """机器可读原因清单 (稳定顺序, 便于跨日 diff)。"""
    reasons: List[str] = []
    if read_error:
        reasons.append("checkpoint_read_error")
    if extract_reason:
        reasons.append(extract_reason)
    if card is None:
        if not bars_available:
            reasons.append("missing_bars")
        return reasons
    if not score["valid_card"]:
        reasons.append("missing_direction" if score["direction"] is None
                       else "invalid_direction")
    else:
        if not score["research_complete"]:
            reasons.append("research_incomplete")
        if score["direction_plan_consistent"] is False:
            reasons.append("direction_plan_inconsistent")
        if score["direction"] == "SKIP":
            reasons.append("skip")
        elif score["direction"] in ("BUY", "SELL_SHORT"):
            if score["price_geometry_valid"] is False:
                reasons.append("price_geometry_invalid")
            elif score["price_geometry_valid"] is None:
                reasons.append("price_geometry_unknown")
    if not bars_available:
        reasons.append("missing_bars")
    return reasons


def _collect_records(day: str, checkpoint_root: Path) -> Tuple[List[dict], List[str], Path]:
    """读当天卡片并逐只打分; 返回 (records, source_files, checkpoint_dir)。"""
    replay = _replay_module()
    runtime = _shadow_runtime()
    checkpoint_dir = Path(checkpoint_root) / day
    files = list_card_files(checkpoint_dir)
    records: List[dict] = []
    for path in files:
        symbol = _symbol_from_name(path.name)
        outer = runtime.read_json(path)
        if outer is None:
            card, extract_reason, read_error = None, None, "unreadable_or_invalid_json"
        else:
            card, extract_reason = _extract_card(outer, replay)
            read_error = None
        bars = replay.day_bars(symbol, day)
        bars_available = bool(bars)
        score = replay.score(card, bars)
        skip_reason = None
        if isinstance(card, dict):
            skip_reason = card.get("skip_reason")
        records.append({
            "symbol": symbol,
            "direction": score["direction"],
            "valid_card": bool(score["valid_card"]),
            "research_complete": bool(score["research_complete"]),
            "direction_plan_consistent": score["direction_plan_consistent"],
            "price_geometry_valid": score["price_geometry_valid"],
            "rr": score["rr"],
            "bars_available": bars_available,
            "reachable": score["reachable"],
            "hit": score["hit"],
            "fwd_pct": score["fwd"],
            "reasons": _reasons_for(score, card, bars_available, extract_reason, read_error),
            "skip_reason": skip_reason,
        })
    return records, [path.name for path in files], checkpoint_dir


def _replay_module():
    """延迟导入回放工具 (score/day_bars 的唯一真源)。"""
    from scripts import da_model_ab_replay as replay
    return replay


# ── 汇总 ────────────────────────────────────────────────────────────────────

def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def aggregate(records: List[dict]) -> Dict[str, Any]:
    """纯函数: 对逐卡读数做日级汇总。"""
    total = len(records)
    valid = [row for row in records if row["valid_card"]]
    directional = [row for row in valid if row["direction"] in ("BUY", "SELL_SHORT")]
    skips = [row for row in valid if row["direction"] == "SKIP"]

    consistent = sum(row["direction_plan_consistent"] is True for row in valid)
    inconsistent = sum(row["direction_plan_consistent"] is False for row in valid)
    research_complete = sum(bool(row["research_complete"]) for row in valid)

    geometry_valid = sum(row["price_geometry_valid"] is True for row in directional)
    geometry_invalid = sum(row["price_geometry_valid"] is False for row in directional)
    geometry_unknown = sum(row["price_geometry_valid"] is None for row in directional)
    skip_inconsistent = sum(
        row["direction_plan_consistent"] is False for row in skips)

    bars_available = sum(bool(row["bars_available"]) for row in records)
    priced = [row for row in directional if row["bars_available"]]
    reachable = [row for row in priced if row["reachable"] is True]
    hit = [row for row in reachable if row["hit"] is True]

    direction_counts = collections.Counter(
        row["direction"] or "UNKNOWN" for row in records)

    return {
        "total": total,
        "valid_card": len(valid),
        "valid_card_rate": _rate(len(valid), total),
        "research_complete": research_complete,
        "research_complete_rate": _rate(research_complete, len(valid)),
        "direction_plan_consistent": consistent,
        "direction_plan_inconsistent": inconsistent,
        "direction_plan_consistent_rate": _rate(consistent, len(valid)),
        "directional_count": len(directional),
        "price_geometry_valid": geometry_valid,
        "price_geometry_invalid": geometry_invalid,
        "price_geometry_unknown": geometry_unknown,
        "price_geometry_rate": _rate(geometry_valid, len(directional)),
        "skip_count": len(skips),
        "skip_inconsistent": skip_inconsistent,
        "direction_counts": dict(sorted(direction_counts.items())),
        "bars_available": bars_available,
        "bars_missing": total - bars_available,
        "price_metrics_available": len(priced),
        "reachable_count": len(reachable),
        "hit_count": len(hit),
        "hit_rate_reachable": _rate(len(hit), len(reachable)),
    }


# ── 落盘 ────────────────────────────────────────────────────────────────────

def build_report(day: str, checkpoint_root: Path) -> Dict[str, Any]:
    """构造确定性报告体 (不含 generated_at, 供内容哈希)。"""
    records, source_files, checkpoint_dir = _collect_records(day, checkpoint_root)
    metrics = aggregate(records)
    notes = list(CAVEATS)
    if not source_files:
        notes.insert(0, f"NO_DATA: {checkpoint_dir} 下没有可读的 US.*.json 卡片")
    return {
        "schema": SCHEMA,
        "date": day,
        "checkpoint_dir": str(checkpoint_dir),
        "source_files": source_files,
        "metrics": metrics,
        "symbols": records,
        "notes": notes,
        "paid_calls": 0,
        "production_db_write": False,
        "gate_or_order_effect": False,
    }


def write_report(body: Dict[str, Any], out_root: Path) -> Dict[str, Any]:
    """不可变历史 + 原子 latest; 同内容重跑幂等 (不覆盖、不新增哈希)。"""
    runtime = _shadow_runtime()
    content_hash = runtime.sha256_json(body)
    day = str(body["date"])
    generated_at = datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")

    artifact = dict(body)
    artifact["generated_at"] = generated_at
    artifact["content_hash"] = content_hash
    artifact_path = Path(out_root) / day / f"{content_hash}.json"
    artifact_written = False
    if not artifact_path.exists():
        try:
            runtime.write_json_new(artifact_path, artifact)
            artifact_written = True
        except FileExistsError:
            artifact_written = False

    latest_path = Path(out_root) / "latest.json"
    latest_relative = artifact_path.relative_to(Path(out_root)).as_posix()
    latest = dict(body)
    latest["generated_at"] = generated_at
    latest["content_hash"] = content_hash
    latest["artifact"] = latest_relative
    existing = runtime.read_json(latest_path)
    latest_written = False
    if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
        runtime.atomic_write_json(latest_path, latest)
        latest_written = True

    return {
        "content_hash": content_hash,
        "artifact_path": str(artifact_path),
        "artifact": latest_relative,
        "artifact_written": artifact_written,
        "latest_path": str(latest_path),
        "latest_written": latest_written,
        "report": latest,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def _print_summary(report: Dict[str, Any], result: Dict[str, Any]) -> None:
    metrics = report["metrics"]
    print(
        "DA_INSTRUCTION_QUALITY_SHADOW date=%s total=%d valid=%d consistent=%s "
        "geometry=%s skips=%d skip_inconsistent=%d bars_missing=%d hash=%s artifact=%s"
        % (
            report["date"], metrics["total"], metrics["valid_card"],
            metrics["direction_plan_consistent_rate"],
            metrics["price_geometry_rate"], metrics["skip_count"],
            metrics["skip_inconsistent"], metrics["bars_missing"],
            result["content_hash"][:12], result["artifact"],
        )
    )
    if metrics["total"] == 0:
        print("[warn] NO_DATA: 当天没有可读的 DA 指令卡; 已落 NO_DATA 证据供评估器判读")
    if result["artifact_written"]:
        print(f"history: {result['artifact_path']}")
    else:
        print(f"history: {result['artifact_path']} (already exists; not overwritten)")
    print(f"latest:  {result['latest_path']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认今天 ET)")
    parser.add_argument("--checkpoint-root", default=str(DEFAULT_CHECKPOINT_ROOT))
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--json", action="store_true", help="打印完整机器可读报告")
    args = parser.parse_args(argv)

    # 关闭路径: 只读开关本身 (env / .env.us), 不触达任何数据文件或目录。
    if not env_enabled():
        print(f"DISABLED: {ENV_FLAG} is not enabled (default false)")
        return 0

    day = args.date or _today_et()
    body = build_report(day, Path(args.checkpoint_root))
    result = write_report(body, Path(args.out_root))
    if args.json:
        print(json.dumps(result["report"], ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(result["report"], result)
    return 0


if __name__ == "__main__":
    sys.exit(main())