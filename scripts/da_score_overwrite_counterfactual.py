#!/usr/bin/env python3
"""丙1 离线反事实: 修挑战者覆写对 gate 判定的影响面 (PROPOSAL-20260905 P1 判据"离线先行").

背景 (A1, glm 证实): llm_synth AGREE 分支把挑战者低分覆写进 score — HIGH 档 33/43=77%
被 8.0→6.0 拉平。丙1 修复后 score=band 原值不再覆写。本脚本用 gate 日志全量回放:
"若当时 score 未被覆写 (取回 band 映射值), tier/decision/仓位档会如何变"。

回放口径 (与 signal_gate.evaluate_signal 终判段 L406-446 同构):
  受影响行 = score_band 非空 且 da_score < band 映射值 (HIGH→8.0/MEDIUM→6.0/LOW→3.0)。
  decision_conf 重算: 同向 AGREE→0.70; 一方弃权→max(0.50, score/10) (score 相关分支,
    必须用回放分); 真反向→0.35。弃权/反向不随丙1变化, 仅 score 相关分支重算。
  tier 梯子: 优(score≥7,dc≥0.7) / 良(≥6,≥0.6) / 中(≥5,≥0.5) / 差; decision=优/良→PASS,
    中→WARN, 差/SKIP→BLOCK。择时闸/破位拦截从 reasons 文本识别, 回放同样施加。
  局限: challenger_agreed 未落日志, 用 challenger_verdict=="AGREE" 近似
    (fallback-AGREE 的 outcome 不含"AGREE"字样, 近似偏差有界且偏小方向保守)。

只读 da_gate_decisions.jsonl, 不写任何文件。用法:
  venv/bin/python scripts/da_score_overwrite_counterfactual.py
"""
import json
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_GATE_LOG = _ROOT / "data" / "da_gate_decisions.jsonl"
_BAND_MAP = {"HIGH": 8.0, "MEDIUM": 6.0, "LOW": 3.0}
_TIER_LADDER = ["差", "中", "良", "优"]
_TIER_TO_DECISION = {"优": ("PASS", "normal"), "良": ("PASS", "small"),
                     "中": ("WARN", "tiny"), "差": ("BLOCK", "tiny"),
                     "SKIP": ("BLOCK", "normal")}


def _replay_conf(row: dict, score: float) -> float:
    """decision_conf 重算 (signal_gate L383-404 同构)。"""
    cd = str(row.get("challenger_direction") or "").upper()
    od = str(row.get("direction") or "").upper()
    verdict = str(row.get("challenger_verdict") or "").upper()
    outcome = str(row.get("debate_outcome") or "").upper()
    agreed = (verdict == "AGREE") or ("AGREE" in outcome)
    if cd == od:
        dc = 0.50 + (0.20 if agreed else 0.0)
    elif "SKIP" in (cd, od):
        dc = max(0.50, score / 10.0)  # 弃权分支用回放分
    else:
        dc = 0.35
    return max(0.10, min(0.95, dc))


def _tier_of(score: float, dc: float, direction: str) -> str:
    if direction not in ("BUY", "SELL_SHORT"):
        return "SKIP"
    if score >= 7 and dc >= 0.7:
        return "优"
    if score >= 6 and dc >= 0.6:
        return "良"
    if score >= 5 and dc >= 0.5:
        return "中"
    return "差"


def _apply_timing_gates(tier: str, reasons: list) -> str:
    """择时闸降2档 + 破位硬买拦截 (从 reasons 识别"实际触发"标记 — gate 的终判行
    恒含"唯一择时闸=真实动量方向冲突"规则说明, 不能用'择时闸'裸词匹配)。"""
    rs = " ".join(str(r) for r in (reasons or []))
    if tier in ("优", "良", "中") and "→ tier降2档" in rs:
        tier = _TIER_LADDER[max(0, _TIER_LADDER.index(tier) - 2)]
    if "破位硬买拦截" in rs:
        tier = "SKIP"
    return tier


def main() -> int:
    rows = []
    with _GATE_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    affected, flips = [], []
    for r in rows:
        band = str(r.get("score_band") or "").upper()
        band_score = _BAND_MAP.get(band)
        if band_score is None:
            continue  # band 契约前时代的行无法忠实回放 (无原值)
        try:
            da_score = float(r.get("da_score"))
        except (TypeError, ValueError):
            continue
        if da_score >= band_score:
            continue  # 未被覆写 (含挑战者同分/更高)
        affected.append(r)

        # 回放: score 取回 band 映射值, dc 重算, tier/decision 重推
        rep_score = band_score
        rep_dc = _replay_conf(r, rep_score)
        rep_tier = _apply_timing_gates(
            _tier_of(rep_score, rep_dc, str(r.get("direction") or "")), r.get("reasons"))
        rep_dec, rep_pos = _TIER_TO_DECISION[rep_tier]

        # 原样重放 (验证回放器保真): 用日志 da_score 走同一套重算, 应与 logged tier 一致
        orig_dc = _replay_conf(r, da_score)
        orig_tier = _apply_timing_gates(
            _tier_of(da_score, orig_dc, str(r.get("direction") or "")), r.get("reasons"))
        orig_dec, orig_pos = _TIER_TO_DECISION[orig_tier]

        flips.append({
            "date": r.get("trade_date"), "symbol": r.get("symbol"),
            "band": band, "da_score": da_score, "replay_score": rep_score,
            "logged_tier": r.get("tier"), "orig_replay_tier": orig_tier,
            "tier": (orig_tier, rep_tier),
            "decision": (orig_dec, rep_dec),
            "position": (orig_pos, rep_pos),
            "logged_decision": r.get("decision"),
        })

    # ── 回放器保真校验: orig_replay_tier 应≈logged_tier (不一致=回放口径偏差) ──
    fidelity_mismatch = [f for f in flips
                         if f["orig_replay_tier"] != f["logged_tier"]]

    print(f"gate 日志总行: {len(rows)}; band 时代行: "
          f"{sum(1 for r in rows if str(r.get('score_band') or '').upper() in _BAND_MAP)}")
    print(f"被覆写行 (da_score < band 映射值): {len(affected)}")
    if affected:
        by_band = Counter(f["band"] for f in flips)
        print(f"  分档: {dict(by_band)}")
    print(f"\n回放器保真: {len(flips) - len(fidelity_mismatch)}/{len(flips)} 行 "
          f"orig 重放 tier == logged tier")
    if fidelity_mismatch:
        print("  不一致样本 (前5):")
        for f in fidelity_mismatch[:5]:
            print(f"    {f['date']} {f['symbol']} logged={f['logged_tier']} "
                  f"replay={f['orig_replay_tier']} (dc 近似偏差)")

    tier_flips = [f for f in flips if f["tier"][0] != f["tier"][1]]
    dec_flips = [f for f in flips if f["decision"][0] != f["decision"][1]]
    pos_flips = [f for f in flips if f["position"][0] != f["position"][1]]
    print(f"\n── 若 score 未被覆写 (回放) ──")
    print(f"tier 翻转: {len(tier_flips)}/{len(flips)}")
    for f in tier_flips:
        print(f"  {f['date']} {f['symbol']} {f['band']} "
              f"score {f['da_score']}→{f['replay_score']}: "
              f"{f['tier'][0]}→{f['tier'][1]} ({f['decision'][0]}/{f['position'][0]}"
              f" → {f['decision'][1]}/{f['position'][1]})")
    print(f"decision 翻转 (PASS/WARN/BLOCK 变): {len(dec_flips)}")
    print(f"仓位档翻转 (如 small→normal): {len(pos_flips)}")
    print("\n解读提示: tier 良→优 = 仓位 small→normal (头寸放大, 非准入变化); "
          "decision 翻转=0 表示覆写未改变任何 PASS/BLOCK 准入, 只压了仓位档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
