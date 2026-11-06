#!/usr/bin/env python3
"""DA 控制臂重复性探针 (DEB-20261106 §17.5 → R0.5; 只读 + 隔离输出).

要回答的问题: **同一控制输入(无事件简报)、同温度, 重复调用是否稳定?**
若不稳定, 任何单次 A/B 的方向变化都不能归因于被测量的变量(事件简报/模型/提示词)。

做法: 复用 scripts/da_event_instruction_replay.py::run_arm(arm='control', brief=None),
      对同一 raw checkpoint 重复 N 次, 记录方向/评分/提示词哈希/时延/token/回显模型。
      不做任何生产写入; 输出目录必须不存在 (fail-closed, 防覆盖旧产物)。

用法:
  cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python3.11 scripts/da_repeatability_probe.py \
      --raw-root data/da_checkpoint/2026-11-05 --symbol AMD --runs 5 \
      --output-dir data/shadow/event_intelligence/r0_5_repeatability_v1 --execute

回滚: 删本脚本 + 删对应输出目录。无 cron / 无生产消费者。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from da_event_instruction_replay import run_arm  # noqa: E402


def _prompt_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", type=Path, required=True)
    ap.add_argument("--symbol", default="AMD")
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

    symbol = args.symbol.strip().upper().removeprefix("US.")
    raw = json.loads((args.raw_root / f"US.{symbol}.raw.json").read_text(encoding="utf-8"))
    if raw.get("trade_date") != args.session_date:
        raise SystemExit(f"session mismatch: raw={raw.get('trade_date')} arg={args.session_date}")
    collected = raw["collected"]

    args.output_dir.mkdir(parents=True)
    runs = []
    for index in range(args.runs):
        print(f"[R0.5] run {index + 1}/{args.runs} start", file=sys.stderr)
        out = run_arm(
            collected,
            symbol=f"US.{symbol}",
            session_date=args.session_date,
            valid_until=args.valid_until,
            brief=None,
            arm="control",
        )
        card = out.get("card") or {}
        prompt = out.get("user_prompt") or ""
        rec = {
            "run": index + 1,
            "direction": card.get("direction"),
            "score": card.get("score"),
            "status": card.get("status"),
            "event_role": card.get("event_role"),
            "prompt_sha256": _prompt_sha(prompt),
            "prompt_tokens_est": len(prompt) // 4,
            "model_reported": (out.get("result") or {}).get("_model_reported"),
            "challenger_model": (out.get("challenger_raw") or {}).get("_model_reported"),
            "llm_calls": len(out.get("calls") or []),
            "validation_errors": card.get("validation_errors"),
        }
        runs.append(rec)
        (args.output_dir / f"US.{symbol}.run{index + 1}.card.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"[R0.5] run {index + 1} direction={rec['direction']} score={rec['score']} "
              f"sha={rec['prompt_sha256'][:12]}", file=sys.stderr)

    dirs = collections.Counter(r["direction"] for r in runs)
    scores = sorted({r["score"] for r in runs})
    shas = {r["prompt_sha256"] for r in runs}
    summary = {
        "schema": "da_repeatability_probe_v1",
        "symbol": f"US.{symbol}",
        "session_date": args.session_date,
        "arm": "control",
        "runs_requested": args.runs,
        "identical_prompts": len(shas) == 1,
        "unique_prompt_sha256": sorted(shas),
        "direction_counts": dict(dirs),
        "directions": [r["direction"] for r in runs],
        "score_values": scores,
        "stable_direction": len(dirs) == 1,
        "stable_score": len(scores) == 1,
        "production_write": False,
        "runs": runs,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: summary[k] for k in (
        "symbol", "runs_requested", "identical_prompts", "direction_counts",
        "score_values", "stable_direction", "stable_score")}, ensure_ascii=False))
    print(f"-> {args.output_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())