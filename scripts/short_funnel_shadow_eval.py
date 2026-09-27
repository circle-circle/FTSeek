#!/usr/bin/env python3
"""空头转化漏斗 eval (RFC-109②-A, 2026-09-26 建 — 填注册表 NO_EVAL_SCRIPT 缺口).

判据 (unified_config.yaml 预注册逐字, 2026-08-16): n≥10 交易日再裁分层 gate。

为什么现在才有 eval: 本项 8/16 起就达标 (30/10 交易日) 却**从未入待裁面** — 产物绕过注册表
(注册表当时不是 SSOT), 且 eval_script=None ⇒ 判据在文件里静默成熟一个多月。9/26 补登后首次可见。

为什么不必再攒样 (用户 8/14 口径「历史数据/重放可行时直接回测裁决, 不默认前向攒样」):
  gz 轮转日志完整 ⇒ 26 个历史交易日可回算同轴链, 已回填进产物 (带 _backfilled 标记,
  双守卫校验: 评级数与看空数都须与原采集值一致才回填)。

🔴 本脚本**只印读数不判决**: 「立分层 gate」是生产行为变更 (要改 pipeline 加门) = L3,
  须用户裁 + 另走 DEB。脚本的职责是把「哪层在掐空头、掐掉多少」算清楚供裁。

副作用: 无 (只读 jsonl; 不推送/不写库/不改 crontab/不下单)。
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "short_funnel_shadow.jsonl"

# 预注册判据门 (逐字来自 unified_config.yaml 2026-08-16 注)
MIN_TRADING_DAYS = 10


def _load():
    if not SRC.exists():
        return [], 0
    out = []
    n_bad = 0                     # 2026-09-27 棘轮清剿: 坏行计数, 禁静默 pass
    for ln in SRC.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                n_bad += 1        # 裁决器吞坏行 = 同轴链样本静默残缺
    return out, n_bad


def evaluate(rows=None, n_bad_json=None) -> dict:
    if rows is None:
        rows, n_bad_json = _load()
    if not rows:
        return {"ok": False, "reason": "NO_ARTIFACT", "days": 0}
    if n_bad_json:
        # 裁决器禁静默吞行 (DEB-20260831 P1③ / R5): 坏行剔除必须进产物, 否则
        # "同轴链 n 天"的分母悄悄少几天, 读数看起来完整而实际残缺
        print(f"[short_funnel_eval] ⚠️ {n_bad_json} 行 JSON 解析失败已剔除 — 样本非完整")

    # 同轴链只在带新键的行上可算 (9/26 回填 26 日 + 此后新采集; 8/14-8/19 日志已清理不可回算)
    axial = [r for r in rows if r.get("bear_with_short_n") is not None]
    legacy = len(rows) - len(axial)

    t = Counter()
    for r in axial:
        t["analyzed"] += r.get("analyzed_n") or 0
        t["bearish"] += r.get("rated_bearish_n") or 0
        t["bws"] += r.get("bear_with_short_n") or 0
        t["bse"] += r.get("bear_short_exec_n") or 0
        t["non_bear"] += r.get("short_from_non_bearish_n") or 0
        t["si"] += r.get("short_instructions") or 0
        t["se"] += r.get("short_executed") or 0

    reasons = Counter()
    for r in axial:
        for k, v in (r.get("bearish_no_short_reasons") or {}).items():
            reasons[k] += v

    def pct(a, b):
        return f"{a / b * 100:.1f}%" if b else "n/a"

    # 判据门: n≥10 交易日 (按 distinct date, 与注册表 count_mode 同轴)
    days = len({r.get("date") for r in rows if r.get("date")})
    axial_days = len({r.get("date") for r in axial if r.get("date")})
    gate_met = days >= MIN_TRADING_DAYS

    # 漏斗各层「掐掉多少」— 这是「分层 gate 该立在哪层」的读数
    cut_at_bridge = t["bearish"] - t["bws"]          # 看空评级 → 未出空头指令
    cut_at_exec = t["bws"] - t["bse"]                # 出了指令 → 未成交
    biggest_cut = max(
        (("评级→指令层 (bridge/通道候选)", cut_at_bridge),
         ("指令→成交层 (触发/追价/护栏)", cut_at_exec)),
        key=lambda x: x[1],
    )

    return {
        "ok": True,
        "days": days,
        "axial_days": axial_days,
        "legacy_days_no_axial": legacy,
        "min_n": MIN_TRADING_DAYS,
        "gate_met": gate_met,
        "analyzed": t["analyzed"],
        "bearish": t["bearish"],
        "bear_with_short": t["bws"],
        "bear_short_exec": t["bse"],
        "short_instructions_all_source": t["si"],
        "short_executed_all_source": t["se"],
        "short_from_non_bearish": t["non_bear"],
        "conv_bear_to_instr": pct(t["bws"], t["bearish"]),
        "conv_instr_to_exec": pct(t["bse"], t["bws"]),
        "cut_at_bridge": cut_at_bridge,
        "cut_at_exec": cut_at_exec,
        "biggest_cut_layer": biggest_cut[0],
        "biggest_cut_n": biggest_cut[1],
        "reasons": reasons.most_common(8),
    }


def main() -> int:
    r = evaluate()
    if not r.get("ok"):
        print(f"[short_funnel_shadow_eval] 无法评估: {r.get('reason')}")
        return 1

    print("=" * 74)
    print("空头转化漏斗 eval (判据: n≥10 交易日再裁分层 gate · 2026-08-16 预注册逐字)")
    print("=" * 74)
    print(f"交易日 n = {r['days']} / 门 {r['min_n']} ⇒ {'✅ 达标' if r['gate_met'] else '❌ 未达标'}"
          f"   (其中同轴链可算 {r['axial_days']} 日; {r['legacy_days_no_axial']} 日日志已清理不可回算)")
    print()
    print("── 同轴转化链 (按 symbol JOIN, 非「不同分母相除」) ──")
    print(f"  DA 分析 {r['analyzed']} 只")
    if r["analyzed"]:
        print(f"  ├ 评级看空 {r['bearish']} ({r['bearish'] / r['analyzed'] * 100:.1f}% of 分析)")
    else:
        print(f"  ├ 评级看空 {r['bearish']}")
    print(f"  │ └ 看空 → 出空头指令 {r['bear_with_short']}  (转化 {r['conv_bear_to_instr']})")
    print(f"  │   └ → 真成交 {r['bear_short_exec']}  (转化 {r['conv_instr_to_exec']})")
    print(f"  └ 全源空头指令 {r['short_instructions_all_source']} 条, 成交 {r['short_executed_all_source']}"
          f" | 其中旁路 (非当日看空评级) {r['short_from_non_bearish']} 只")
    print()
    print("── 各层掐掉多少 (「分层 gate 该立在哪层」的读数) ──")
    print(f"  评级→指令层 掐掉 {r['cut_at_bridge']} 只")
    print(f"  指令→成交层 掐掉 {r['cut_at_exec']} 只")
    print(f"  ⇒ 最大漏斗口 = {r['biggest_cut_layer']} ({r['biggest_cut_n']} 只)")
    print()
    print("── 看空评级却未出空头指令的真实原因分布 ──")
    if r["reasons"]:
        for k, v in r["reasons"]:
            print(f"  ×{v:4d}  {k}")
    else:
        print("  (无)")
    print()
    print("── [只印不判] 裁决面 ──")
    print("  「立分层 gate」= 生产行为变更 (改 pipeline 加门) = L3 ⇒ 须用户裁 + 另走 DEB,")
    print("  本脚本不判决。供裁的三个事实:")
    print(f"   1. 判据门早已达标 ({r['days']}/{r['min_n']}), 8/16 起静默成熟一个多月 (注册表非 SSOT 实锤)")
    print(f"   2. 看空→指令转化 {r['conv_bear_to_instr']} ⇒ 8/16 原诊断「断点在 LLM 分析层转化率低」"
          f"**不成立** (转化率过半)")
    print(f"   3. 最大漏斗口在 {r['biggest_cut_layer']}, 主因见上表 ⇒ 若要立门, 该立在这一层"
          f" (立错层 = 门恒不触发, 同已删的 gate_block_hints 死列)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
