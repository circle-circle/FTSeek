#!/usr/bin/env python3
"""DA 控制/实验臂重复性探针 (DEB-20261106 §17.5/§18 → §65.4 P1; 只读 + 隔离输出).

要回答的问题: **同一输入(控制臂无事件简报 / 实验臂同一共享简报)、同温度,
重复调用是否稳定?** 若不稳定, 任何单次 A/B 的方向变化都不能归因于被测量的变量.

做法: 复用 scripts/da_event_instruction_replay.py::run_arm(...) 对同一 raw checkpoint
      重复 N 次; 记录方向/评分/提示词哈希/时延/token/回显模型/事件角色.
      不做任何生产写入; 输出目录必须不存在 (fail-closed, 防覆盖旧产物).

三种模式:
  control    只跑控制臂 (兼容旧用法, 默认)
  treatment  只跑实验臂 (必须给 --brief)
  paired     同一 run 序号内先后跑 control/treatment, 并给出配对方向变化

用法 (旧: 控制臂单标的):
  cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/da_repeatability_probe.py \
      --raw-root data/da_checkpoint/2026-11-05 --symbol AMD --runs 5 \
      --output-dir data/shadow/event_intelligence/r0_5_repeatability_v1 --execute

用法 (新: 2026-11-12 NVDA 案例, 多标的配对重复):
  venv/bin/python scripts/da_repeatability_probe.py \
      --raw-root data/da_checkpoint/2026-11-12 \
      --brief data/shadow/event_intelligence/nvda_20261112_r1_v1/market_event_brief.json \
      --symbols NVDA,AMD,AVGO,TSM,MU,SNDK,ARM,NBIS --arm paired --runs 5 \
      --session-date 2026-11-12 --valid-until 2026-11-12T16:00:00-05:00 \
      --output-dir data/shadow/event_intelligence/nvda_20261112_r2_repeat_paired_n5_v1 --execute

成本: run_arm 每次 = 主模型 1 次 + 挑战者 1 次 = 2 次付费调用.
      调用数 = 标的数 × runs × 臂数 × 2.

回滚: 删本脚本 + 删对应输出目录. 无 cron / 无生产消费者.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from da_event_instruction_replay import run_arm  # noqa: E402


def _prompt_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _run_record(index: int, arm: str, output: Dict[str, Any]) -> Dict[str, Any]:
    card = output.get("card") or {}
    prompt = output.get("user_prompt") or ""
    return {
        "run": index,
        "arm": arm,
        "direction": card.get("direction"),
        "score": card.get("score"),
        "status": card.get("status"),
        "event_role": card.get("event_role"),
        "event_ids": card.get("event_ids") or [],
        "price_in": card.get("price_in"),
        "prompt_sha256": _prompt_sha(prompt),
        "prompt_tokens_est": len(prompt) // 4,
        "model_reported": (output.get("result") or {}).get("_model_reported"),
        "challenger_model": (output.get("challenger_raw") or {}).get("_model_reported"),
        "llm_calls": len(output.get("calls") or []),
        "validation_errors": card.get("validation_errors"),
    }


def _score_key(value: Any):
    return (value is None, value if isinstance(value, (int, float)) else 0.0, str(value))


def summarize_runs(runs: Sequence[Dict[str, Any]], *, arm: str, symbol: str) -> Dict[str, Any]:
    """Summarize one arm's repeated runs (pure; no model calls)."""
    directions = collections.Counter(r["direction"] for r in runs)
    statuses = collections.Counter(r["status"] for r in runs)
    roles = collections.Counter(r["event_role"] for r in runs)
    scores = sorted({r["score"] for r in runs}, key=_score_key)
    shas = {r["prompt_sha256"] for r in runs}
    return {
        "symbol": symbol,
        "arm": arm,
        "runs_requested": len(runs),
        "identical_prompts": len(shas) == 1,
        "unique_prompt_sha256": sorted(shas),
        "direction_counts": dict(sorted(directions.items())),
        "directions": [r["direction"] for r in runs],
        "score_values": scores,
        "status_counts": dict(sorted(statuses.items())),
        "event_role_counts": dict(sorted(roles.items())),
        "stable_direction": len(directions) == 1,
        "stable_score": len(scores) == 1,
        "valid_cards": statuses.get("VALID", 0),
        "degraded_cards": statuses.get("DEGRADED", 0),
    }


def summarize_paired(
    control_runs: Sequence[Dict[str, Any]],
    treatment_runs: Sequence[Dict[str, Any]],
    *,
    symbol: str,
) -> Dict[str, Any]:
    """Pair repeated control/treatment runs by run index (pure; no model calls)."""
    pairs = list(zip(control_runs, treatment_runs))
    pair_rows = [
        {
            "run": index + 1,
            "control_direction": control["direction"],
            "treatment_direction": treatment["direction"],
            "control_score": control["score"],
            "treatment_score": treatment["score"],
            "treatment_event_role": treatment["event_role"],
            "treatment_event_ids": treatment["event_ids"],
        }
        for index, (control, treatment) in enumerate(pairs)
    ]
    return {
        "symbol": symbol,
        "paired_run_count": len(pairs),
        "direction_changes": sum(row["control_direction"] != row["treatment_direction"] for row in pair_rows),
        "score_changes": sum(row["control_score"] != row["treatment_score"] for row in pair_rows),
        "treatment_runs_citing_events": sum(bool(row["treatment_event_ids"]) for row in pair_rows),
        "pairs": pair_rows,
    }


def _symbol_list(args: argparse.Namespace) -> List[str]:
    raw = args.symbols or args.symbol
    symbols = [item.strip().upper().removeprefix("US.") for item in raw.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("至少需要一个标的")
    return symbols


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", type=Path, required=True)
    ap.add_argument("--symbol", default="AMD", help="单标的模式 (兼容旧用法)")
    ap.add_argument("--symbols", default="", help="逗号分隔多标的; 给了则覆盖 --symbol")
    ap.add_argument("--brief", type=Path, help="共享 MarketEventBrief (treatment/paired 必填)")
    ap.add_argument("--arm", choices=("control", "treatment", "paired"), default="control")
    ap.add_argument("--session-date", default="2026-11-05")
    ap.add_argument("--valid-until", default="2026-11-05T16:00:00-05:00")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    if not args.execute:
        raise SystemExit("dry-run 不调用模型; 加 --execute 才实跑")
    if args.runs < 2:
        raise SystemExit("runs 至少为 2 (单次无法度量方差)")
    if args.output_dir.exists():
        raise SystemExit(f"输出目录已存在, 拒绝覆盖: {args.output_dir}")
    if args.arm == "control" and args.brief:
        raise SystemExit("arm=control 不使用 --brief; 若要实验臂请用 --arm treatment/paired")
    if args.arm != "control" and not args.brief:
        raise SystemExit(f"arm={args.arm} 必须给 --brief")

    brief = None
    if args.brief:
        brief = json.loads(args.brief.read_text(encoding="utf-8"))
        if brief.get("validation_errors"):
            raise SystemExit(f"brief validation failed: {brief['validation_errors']}")

    symbols = _symbol_list(args)
    raw_by_symbol = {}
    for symbol in symbols:
        raw = json.loads((args.raw_root / f"US.{symbol}.raw.json").read_text(encoding="utf-8"))
        if raw.get("trade_date") != args.session_date:
            raise SystemExit(
                f"session mismatch for {symbol}: raw={raw.get('trade_date')} arg={args.session_date}"
            )
        raw_by_symbol[symbol] = raw["collected"]

    args.output_dir.mkdir(parents=True)
    arms = ("control", "treatment") if args.arm == "paired" else (args.arm,)
    symbol_results = {}
    for symbol in symbols:
        per_arm: Dict[str, List[Dict[str, Any]]] = {arm: [] for arm in arms}
        for index in range(args.runs):
            for arm in arms:
                print(f"[R0.5] {symbol} run {index + 1}/{args.runs} {arm} start", file=sys.stderr)
                output = run_arm(
                    raw_by_symbol[symbol],
                    symbol=f"US.{symbol}",
                    session_date=args.session_date,
                    valid_until=args.valid_until,
                    brief=brief if arm == "treatment" else None,
                    arm=arm,
                )
                record = _run_record(index + 1, arm, output)
                per_arm[arm].append(record)
                (args.output_dir / f"US.{symbol}.run{index + 1}.{arm}.card.json").write_text(
                    json.dumps(output.get("card") or {}, ensure_ascii=False, indent=2,
                               sort_keys=True, default=str) + "\n",
                    encoding="utf-8",
                )
                print(
                    f"[R0.5] {symbol} run {index + 1} {arm} direction={record['direction']} "
                    f"score={record['score']} sha={record['prompt_sha256'][:12]}",
                    file=sys.stderr,
                )
        summaries = {
            arm: summarize_runs(per_arm[arm], arm=arm, symbol=f"US.{symbol}")
            for arm in arms
        }
        result = {
            "symbol": f"US.{symbol}",
            "arms": summaries,
            "runs": {arm: per_arm[arm] for arm in arms},
        }
        if args.arm == "paired":
            result["paired"] = summarize_paired(
                per_arm["control"], per_arm["treatment"], symbol=f"US.{symbol}",
            )
        symbol_results[symbol] = result

    summary: Dict[str, Any] = {
        "schema": "da_repeatability_probe_v2",
        "session_date": args.session_date,
        "valid_until": args.valid_until,
        "runs_requested": args.runs,
        "arm_mode": args.arm,
        "brief_id": (brief or {}).get("brief_id"),
        "symbols": [f"US.{symbol}" for symbol in symbols],
        "results": symbol_results,
        "production_write": False,
        "notifier_calls": 0,
        "llm_calls_per_run": 2,
        "total_llm_calls": len(symbols) * args.runs * len(arms) * 2,
    }
    if len(symbols) == 1 and args.arm != "paired":
        single = symbol_results[symbols[0]]["arms"][args.arm]
        summary.update({
            "symbol": single["symbol"],
            "arm": single["arm"],
            "identical_prompts": single["identical_prompts"],
            "unique_prompt_sha256": single["unique_prompt_sha256"],
            "direction_counts": single["direction_counts"],
            "directions": single["directions"],
            "score_values": single["score_values"],
            "stable_direction": single["stable_direction"],
            "stable_score": single["stable_score"],
            "runs": [
                dict(record)
                for record in symbol_results[symbols[0]]["runs"][args.arm]
            ],
        })
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "symbols": summary["symbols"],
        "arm_mode": summary["arm_mode"],
        "runs_requested": summary["runs_requested"],
        "total_llm_calls": summary["total_llm_calls"],
        "results": {
            symbol: {
                arm: {
                    "direction_counts": data["direction_counts"],
                    "stable_direction": data["stable_direction"],
                    "stable_score": data["stable_score"],
                }
                for arm, data in result["arms"].items()
            }
            for symbol, result in symbol_results.items()
        },
    }, ensure_ascii=False))
    print(f"-> {args.output_dir / 'summary.json'}")
    return 0


def _flatten_runs(symbol_result: Dict[str, Any], arm: str) -> List[Dict[str, Any]]:
    """Return the per-run records for one arm (kept for testability)."""
    return list((symbol_result.get("runs") or {}).get(arm, []))


if __name__ == "__main__":
    raise SystemExit(main())