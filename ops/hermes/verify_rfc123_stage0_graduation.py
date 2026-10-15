#!/usr/bin/env python3
"""RFC-123 阶段0 双锚影子毕业核验 + 第2层预注册提醒 (一次性载体).

约定 (follow verify_rfc135_expiry.py): 永远 exit 0; READY/TOO_EARLY 经 stdout 表达,
不用退出码传状态 (cron 把非零退出当载体级故障)。

fire: 2026-10-21 09:00 CST — 第10交易日 (10-20) session 数据齐后 (10-20 16:00 ET
= 10-21 04:00 CST 落齐, 日更 cron 05:00-07:20 CST 已跑)。10月 NYSE 无假日,
故 10-13/14/15/16/19/20 = 第5-10交易日, 第10交易日确为 10-20。

只读: /opt/ftseek/FTSeek-US/data/entry_timing_shadow.jsonl 的 dual_anchor 块。
不自动裁决: 判据③ (bar-level exit sim, 生成锚 vs 现行锚 含费PnL) + ④ 是用户
开跑会级决策; 本脚本只 verify 天数 + 判据①② + 报③候选池 + 提示, 零行为变更。

纪律锚: RFC-123 附E/G + DEB-20261009 §41。
"""
import json
import os
import sys

REPO = "/opt/ftseek/FTSeek-US"
SHADOW = os.path.join(REPO, "data", "entry_timing_shadow.jsonl")
GRAD_DAYS = 10  # RFC-123 预注册: 影子期 ≥10 交易日


def main() -> int:
    try:
        with open(SHADOW, errors="replace") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        print(f"[RFC-123阶段0] ERROR: 影子文件不存在 {SHADOW} (阶段0 未接线?)")
        return 0

    da = [r["dual_anchor"] for r in rows if r.get("dual_anchor")]
    if not da:
        print("[RFC-123阶段0] ERROR: entry_timing_shadow.jsonl 无 dual_anchor 行 (影子未接线?)")
        return 0

    days = sorted({x.get("date") for x in da if x.get("date")})
    n = len(da)
    miss = sum(1 for x in da if x.get("gen_source") == "missing")
    div = sum(1 for x in da if x.get("trigger_divergent"))
    miss_pct = miss / n * 100.0
    div_pct = div / n * 100.0
    c1 = "PASS" if miss_pct < 1.0 else "FAIL"
    c2 = "PASS" if div_pct >= 5.0 else "FAIL"

    print(f"[RFC-123阶段0毕业核验] 双锚影子 {n} 行 / {len(days)} 交易日 {days}")
    print(f"  判据① 生成锚缺失率 = {miss_pct:.2f}% (门槛 <1%) → {c1}")
    print(f"  判据② 触发分歧率   = {div_pct:.2f}% (门槛 ≥5%) → {c2}")

    if len(days) < GRAD_DAYS:
        print(f"  ⇒ TOO_EARLY: {len(days)}/{GRAD_DAYS} 交易日, 未达毕业窗。")
        print("     第10交易日预计 2026-10-20 (10月 NYSE 无假日), 数据齐于 10-21 04:00 CST。")
        print("     若因故顺延, 请重核天数后再裁。本提醒一次性, 不重复 fire。")
        return 0

    print(f"  判据③ 候选池 = 分歧样本 {div} 行 (trigger_divergent); 需匹配已平仓 + bar-level exit sim")
    print("           裁「按生成锚判定的含费PnL 不低于现行锚」— 开跑会级, 非本脚本自动裁。")
    print("  判据④ 现行护栏触顶频率无异常上升 → 待核。")
    print(f"  ⇒ READY: 阶段0 满 {GRAD_DAYS} 交易日, 判据① {c1} / ② {c2}。")
    print("     请用户裁:")
    print("       (a) 判据③ — 分歧样本 生成锚 vs 现行锚 含费PnL (bar-level exit sim);")
    print("       (b) 若③过 → 阶段1 拆锚; 并启动第2层 (or_low 规则锚) 独立预注册")
    print("           (自己的影子 + 判据, 不与阶段0/1 混批, 防变量纠缠)。")
    print("     纪律 (RFC-123 附E/G + DEB-20261009 §41):")
    print("       · 只筛方向不裁决; 第2层不预设阈值 (防购物);")
    print("       · ⛔ 现在不改 planned_entry 生成 — 阶段0 测现状 LLM 锚, 改了=换被测量变量=作废重攒;")
    print("       · 第2层 NET benefit 须自证 (cf_pnl 有偏 + 漏赢家 + 同样本非独立 + 被动挂价 KILL 前科)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
