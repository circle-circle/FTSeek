#!/usr/bin/env python3
"""da_prompt_distill_shadow — DEB-20260901c 阶段二影子采集 (2026-09-01, 纯旁路零生产触碰)

目的: 蒸馏版 system prompt (v2) 与生产版 (v1) 同夜同料并跑, 攒样回答
  "结构精简是否改变模型行为/方向质量" (判据见
   docs/debates/DEB-20260901-da-prompt-distillation-shadow.md §四)。

架构 (仿 da_glm_shadow, 零生产触碰):
  读当晚 da_checkpoint/{ET日期}/{sym}.raw.json (生料, da_raw_inputs/v1→v2 均可)
  → 生产 checkpoint 存在 (V1 对照臂已在) 的候选:
     DeepAnalyzer(collected, synth_module=V2垫片).analyze()
     垫片 = llm_synth.synthesize(..., system_override=DISTILLED_V2) —
     组装链路与生产逐字节同源 (同函数同注入), 唯一变量=system 文本;
     思考模式双臂同开 (继承生产现状, DEB-20260901b 直切后)。
  → append data/shadow/da_prompt_distill_shadow.jsonl (run_mode 区分)
  零落库 / 零 bridge / 零指令 / 不动 da_checkpoint。

预注册判据 (窗口 9/1-9/15, 10 交易日, 不含 9/7 劳动节; 首采夜=9/1 ET 非 9/2,
  2026-09-01 红队复查勘误): ①JSON有效+违约率≤V1 ②分布漂移≤10pp
  ③T+1 配对非劣 (下界≥-0.10%) ④token 降幅≥15%。9/16 一次性评估裁决。
归因数据随采集捕获 (2026-09-01 红队复查修复): 生产检查点 _CKPT_KEEP_DAYS=3 自删,
  窗口末评估时 9 月初检查点已不存在 → V1 的 finish_reason/契约违约/entry_price
  在采集时即从生产检查点捕获落行, 判据①③不依赖评估日重读检查点。
成本: 每候选 ≈1 次生产同型调用 (V1 臂复用生产不重跑)。
回滚: 删 crontab 一行 (40 21 * * 1-5 ... da_prompt_distill_shadow.py) + 删本文件。
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FTSEEK_ROOT = "/opt/ftseek/FTSeek-US"
SKILL_SCRIPTS = "/root/.hermes/skills/deep-stock-analysis/scripts"
SHADOW_PATH = os.path.join(FTSEEK_ROOT, "data", "shadow", "da_prompt_distill_shadow.jsonl")
CKPT_ROOT = os.path.join(FTSEEK_ROOT, "data", "da_checkpoint")
ET = ZoneInfo("America/New_York")

sys.path.insert(0, SKILL_SCRIPTS)


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _append_jsonl(record: dict) -> None:
    os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
    with open(SHADOW_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class _DistilledV2Synth:
    """V2 垫片: 生产组装链不变, 唯一变量=分析师 system 文本 (DEB-20260901c)。"""

    @staticmethod
    def synthesize(collected, trade_experience=""):
        import llm_synth
        from prompt_distill_v2 import DISTILLED_SYSTEM_PROMPT_V2
        return llm_synth.synthesize(collected, trade_experience=trade_experience,
                                    system_override=DISTILLED_SYSTEM_PROMPT_V2)


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] 读取失败 {path}: {e}", file=sys.stderr)
        return None


def run(run_mode: str = "live") -> int:
    day = _today_et()
    ck_dir = os.path.join(CKPT_ROOT, day)
    if not os.path.isdir(ck_dir):
        print(f"[distill-shadow] {day} checkpoint 目录不存在 (DA 未跑?), 退出")
        return 0

    import llm_synth  # noqa: F401 — 提前暴露导入错误
    n_proc, n_skip_nov1, n_err = 0, 0, 0
    for raw_path in sorted(Path(ck_dir).glob("*.raw.json")):
        sym = raw_path.name.replace(".raw.json", "")
        prod_path = os.path.join(ck_dir, f"{sym}.json")
        if not os.path.exists(prod_path):
            n_skip_nov1 += 1  # 生产臂未完成/失败 → 无对照, 跳过
            continue
        raw = _load_json(str(raw_path))
        prod = _load_json(prod_path)
        if not raw or not prod:
            n_err += 1
            continue
        collected = raw.get("collected") or {}
        if not collected.get("symbol"):
            collected = {**collected, "symbol": raw.get("symbol", sym)}
        plo = prod.get("_llm_output") or {}
        v1_contract = plo.get("_contract") or {}
        v1_plan = plo.get("execution_plan") or {}
        t0 = datetime.now(timezone.utc)
        try:
            from deep_analyzer import DeepAnalyzer
            v2_analysis = DeepAnalyzer(collected, synth_module=_DistilledV2Synth).analyze()
        except Exception as e:
            print(f"[warn] {sym} V2 臂异常: {e}", file=sys.stderr)
            n_err += 1
            continue
        v2lo = (v2_analysis or {}).get("_llm_output") or {}
        v2_dir = str(v2lo.get("direction") or v2_analysis.get("rating") or "?").upper()
        v1_dir = str(plo.get("direction") or "?").upper()
        rec = {
            "type": "da_prompt_distill_shadow",
            "run_mode": run_mode,
            "date": day,
            "symbol": raw.get("symbol", sym),
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "prompt_v1_hash": plo.get("_prompt_hash"),
            "v1_direction": v1_dir,
            "v1_score_band": plo.get("score_band"),
            "v1_rating": plo.get("rating"),
            "v2_direction": v2_dir,
            "v2_score_band": v2lo.get("score_band"),
            "v2_rating": v2lo.get("rating"),
            "v2_executive_summary": str(v2lo.get("executive_summary") or "")[:300],
            "v2_skip_reason": v2lo.get("skip_reason"),
            "v2_latency_ms": v2lo.get("_llm_latency_ms"),
            "v2_thinking_enabled": v2lo.get("_thinking_enabled"),
            "v2_finish_reason": v2lo.get("_finish_reason"),
            "v2_tokens_out": (v2lo.get("_usage") or {}).get("completion_tokens"),
            "v2_tokens_in": (v2lo.get("_usage") or {}).get("prompt_tokens"),
            "v1_tokens_in": (plo.get("_usage") or {}).get("prompt_tokens"),
            "v1_tokens_out": (plo.get("_usage") or {}).get("completion_tokens"),
            "v1_finish_reason": plo.get("_finish_reason"),
            "v1_thinking_enabled": plo.get("_thinking_enabled"),
            "v1_contract_violations": list(v1_contract.get("violations") or []),
            "v1_entry_price": v1_plan.get("entry_price"),
            "v2_entry_price": (v2lo.get("execution_plan") or {}).get("entry_price"),
            "v2_contract_violations": list((v2lo.get("_contract") or {}).get("violations") or []),
            "v2_error": v2lo.get("_error"),
            "direction_agree": (v1_dir == v2_dir),
            "elapsed_s": round((datetime.now(timezone.utc) - t0).total_seconds(), 1),
        }
        _append_jsonl(rec)
        n_proc += 1
        print(f"[distill-shadow] {sym}: v1={v1_dir}/{plo.get('score_band')} "
              f"v2={v2_dir}/{v2lo.get('score_band')} agree={rec['direction_agree']} "
              f"({rec['elapsed_s']}s)")
    print(f"[distill-shadow] {day} 完成: 处理 {n_proc}, 无生产对照跳过 {n_skip_nov1}, 异常 {n_err}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="DEB-20260901c 蒸馏影子采集 (只读生产料, 旁路落 shadow)")
    ap.add_argument("--test", action="store_true", help="run_mode=test (不计入样本)")
    a = ap.parse_args()
    sys.exit(run(run_mode="test" if a.test else "live"))
