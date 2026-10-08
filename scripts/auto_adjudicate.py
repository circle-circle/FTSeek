#!/usr/bin/env python3
"""统一自动验证总线 runner (RFC-139 L2) — 确定性执行 + 统一呈报.

定位 (用户裁 2026-10-08「那就执行吧」):
  77 项影子 / ~120 个验证脚本 / 判据自由文本 ⇒ 每次人工写结算脚本都是口径自由发挥。
  本 runner 是「一个入口」: 遍历注册表待裁面 (与 pending_adjudications 同一 SSOT 读层),
  按 RFC-139 L1 spec 渲染判据门, 读/跑 eval_script, 产出统一 verdict + 通俗背景。

**agent 不进判定层** (用户裁「agent 是秘书不是法官」): 本脚本零 LLM 调用,
同输入同输出; 判分权在预注册判据 + 用户, runner 只「执行并呈报」。

模式:
  --read-only (默认)  只读既有 verdict 产物, 不执行脚本 (快, 适合日常分诊/推送);
  --execute           实跑待裁项 eval_script (裁决批用; 脚本可能耗时/拉数据)。

输出分类 (如实, 不臆造):
  VERDICT_ARTIFACT  产物 json 含 verdict 码 ⇒ 结构化读数 + 白话;
  COLLECTOR_ONLY    脚本能跑但不产 verdict (采集器≠结算器) ⇒ 暴露 NO_SETTLER 缺口;
  NO_EVAL_SCRIPT    注册表未挂结算入口 ⇒ 有入口无出口;
  SCRIPT_FAILED     实跑 rc≠0 / 超时 ⇒ 照实报错, 不猜结论。

零行为变更: 只读注册表/产物/DB(era); 写一份报告 JSON (OUT_LATEST); 不改注册表状态。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.review.criteria_spec import get_spec  # noqa: E402
from app.review.criteria_status import pending_adjudications  # noqa: E402

OUT_LATEST = REPO / "data" / "review" / "auto_adjudication_latest.json"

# 产物 verdict json 里可抬读的数值键 (统一呈报, 不判分)
_READOUT_KEYS = ("n", "n_settled", "n_days", "mean", "median", "sum", "ci95_lo", "ci95_hi",
                 "sign_p", "perm_p_worse", "day_t", "hit_rate", "win_rate", "ic", "t_stat")
_VERDICT_KEYS = ("verdict", "verdict_label", "decision", "conclusion")
_TIMEOUT_S = 600


def _load_artifact(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = REPO / p
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return None
    return d if isinstance(d, dict) else None


def _readout(d: dict | None) -> dict:
    """从 verdict 产物抽统一读数 (数值键白名单; 取不到不臆造)。"""
    if not d:
        return {}
    out = {}
    for k in _READOUT_KEYS:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    main = d.get("main")
    if isinstance(main, dict):
        for k in _READOUT_KEYS:
            v = main.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in out:
                out[k] = v
    return out


def _verdict_code(d: dict | None) -> str:
    if not d:
        return ""
    for k in _VERDICT_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _run_script(script: str, timeout: int) -> dict:
    """确定性实跑 eval_script (subprocess; 同输入同输出, 零 LLM)。"""
    try:
        r = subprocess.run(
            [sys.executable, script], cwd=REPO, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "stdout_tail": r.stdout[-500:], "stderr_tail": r.stderr[-300:]}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "stdout_tail": "", "stderr_tail": f"timeout after {timeout}s"}
    except Exception as e:                                        # noqa: BLE001
        return {"rc": -2, "stdout_tail": "", "stderr_tail": f"{type(e).__name__}: {e}"}


def _classify(row: dict, spec_artifact: str | None, exec_result: dict | None) -> str:
    if not row.get("eval_script"):
        return "NO_EVAL_SCRIPT"
    artifact = _load_artifact(spec_artifact)
    if _verdict_code(artifact):
        return "VERDICT_ARTIFACT"
    if exec_result is not None and exec_result.get("rc", 0) != 0:
        return "SCRIPT_FAILED"
    return "COLLECTOR_ONLY"


def evaluate(rows: list[dict], *, execute: bool = False, timeout: int = _TIMEOUT_S) -> dict:
    """主入口: 待裁面 → 统一 verdict 结构 (确定性)。"""
    pending = [r for r in rows if r.get("needs_adjudication")]
    items = []
    for row in pending:
        spec = get_spec(row["id"])
        artifact_path = spec.verdict_artifact if spec else None
        exec_result = None
        if execute and row.get("eval_script"):
            exec_result = _run_script(row["eval_script"], timeout)
        artifact = _load_artifact(artifact_path)
        cls = _classify(row, artifact_path, exec_result)
        items.append({
            "id": row["id"],
            "status": row["status"],
            "n": row.get("n"),
            "min_n": row.get("min_n"),
            "classification": cls,
            "verdict": _verdict_code(artifact) or None,
            "readout": _readout(artifact),
            "spec": {
                "source": spec.source if spec else None,
                "metric": spec.metric if spec else None,
                "control": spec.control if spec else None,
                "min_effect": spec.min_effect if spec else None,
                "sample_gates": spec.sample_gates if spec else None,
                "known_gap": spec.known_gap if spec else None,
            } if spec else None,
            "criteria_text": row.get("criteria"),
            "eval_script": row.get("eval_script"),
            "exec_result": exec_result,
            "verdict_era": row.get("verdict_era"),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "execute" if execute else "read-only",
        "pending_count": len(pending),
        "items": items,
    }


_PLAIN_VERDICT = {
    "NOT_ESTABLISHED_KEEP_SAMPLING": "还看不出结论, 继续攒样 (不翻开关)",
    "INSUFFICIENT_N": "样本不够, 不下结论",
}


def render(report: dict) -> str:
    """通俗 stdout (只复述 runner 输出, 不补充判断 — riskbound skill 同款纪律)。"""
    L = []
    L.append("=" * 64)
    L.append("  AUTO ADJUDICATION RUNNER (RFC-139 · 确定性 · 零 LLM)")
    L.append(f"  待裁面: {report['pending_count']} 项 | 模式: {report['mode']}")
    L.append("=" * 64)
    for it in report["items"]:
        L.append(f"── {it['id']} (n {it.get('n') or 0}/{it.get('min_n') or '?'}, {it['status']}) ──")
        sp = it.get("spec")
        if sp:
            L.append(f"  这是什么: {sp['metric']}")
            L.append(f"  决策源: {sp['source']}")
            if sp.get("min_effect"):
                L.append(f"  判据门: {sp['min_effect']}")
        else:
            L.append(f"  这是什么: {it['id']} (L1 spec 未收录, 以注册表判据为准)")
            L.append(f"  判据原文: {(it.get('criteria_text') or '')[:80]}…")
            L.append("  决策源: shadow_registry.graduation_criteria (SSOT; L1 spec 未收录)")
        ro = it.get("readout") or {}
        if ro:
            keys = [k for k in ("n", "n_settled", "mean", "ci95_lo", "ci95_hi", "sign_p") if k in ro]
            L.append("  实测: " + ", ".join(f"{k}={ro[k]:.4g}" if isinstance(ro[k], float) else f"{k}={ro[k]}" for k in keys))
        if it.get("verdict"):
            L.append(f"  verdict: {it['verdict']} ({_PLAIN_VERDICT.get(it['verdict'], '见产物原文')})")
        cls = it["classification"]
        if cls == "COLLECTOR_ONLY":
            gap = (sp or {}).get("known_gap") or "eval_script 能跑但不产 verdict (采集器≠结算器)"
            L.append(f"  ⚠️ 缺口: {gap}")
        elif cls == "NO_EVAL_SCRIPT":
            L.append("  ⚠️ 缺口: 无 eval_script (有入口无出口, 须补结算器或降纯观察)")
        elif cls == "SCRIPT_FAILED":
            L.append(f"  ⚠️ 实跑失败: rc={it['exec_result']['rc']} — {it['exec_result']['stderr_tail'][:120]}")
        era = it.get("verdict_era")
        if era and era.get("applies") is False:
            L.append(f"  ⚠️ 参数时代漂移: {era.get('note')}")
        elif era and era.get("applies") is True:
            L.append(f"  era: ✅ {era.get('note')}")
        else:
            L.append("  era: 未裁项 (本次读数即现役参数读数, 裁决后自动带 era)")
        L.append("")
    L.append("  边界: runner 只执行并呈报, 不判过/不过; 判分权在预注册判据 + 用户。")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RFC-139 统一自动验证 runner (确定性)")
    ap.add_argument("--execute", action="store_true", help="实跑待裁项 eval_script (默认只读产物)")
    ap.add_argument("--timeout", type=int, default=_TIMEOUT_S, help="单脚本超时秒 (默认 600)")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只打印, 不写 OUT_LATEST")
    args = ap.parse_args(argv)
    rows = pending_adjudications()
    report = evaluate(rows, execute=args.execute, timeout=args.timeout)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    if not args.dry:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json_only:
            print(f"  报告已落盘: {OUT_LATEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
