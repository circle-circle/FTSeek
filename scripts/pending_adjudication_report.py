#!/usr/bin/env python3
"""待裁决触发通知: 影子通道"该裁的裁"清单 (通俗版)。

背景 (为什么要这条通知)
    影子通道 = 先只观察、不参与交易的试验项 (record-only)。每条都预先注册了"判据"——
    攒够样本后跑判据，够格就提案转正，不够格就停掉。但注册表过去只有**入口没有出口**:
    `current_n >= min_n` 就机械标 READY_TO_EVALUATE，而 READY 只代表"样本量够了"，
    不代表"判据通过"，于是清单越长越没人看 (2026-09-21 实测: 37 项里 19 项挂着 READY)。

本通知干什么
    把注册表的 READY 面收敛成三行白话: **需要你决定的 / 自动续观不用你管的 / 挂账不算待裁的**,
    并且只在**待裁面发生变化**时输出 (首次运行会输出全量一次)。无变化 → 零输出 = 不推送。

判据与结论出处
    `CURATED` 里的白话名与结论来自已实测的读数 (主要: docs/decisions/RFC-092-*.md 三批 +
    2026-09-21 本 agent 重跑复现 top_movers / iv_hv / gap_chase / top_movers_trade_join)。
    结论是**快照**，不是永久真值: 读数变化时判据可能翻盘，故本通知在待裁面变化时会重新抬出。

不改任何交易行为: 只读注册表 + 读产物 json + 写一份 state 文件 (data/shadow/)。
用法: `python scripts/pending_adjudication_report.py [--dry] [--force]`
    · --dry   只打印, 不写 state (供人工查看)
    · --force 忽略 state, 强制输出全量 (供首次部署/事后重看)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

STATE = REPO / "data" / "shadow" / "pending_adjudication_state.json"

# ── 结论分类 ────────────────────────────────────────────────────────────────
# NEEDS_USER : 判据已有读数且**处置需要用户裁决** (我不会自己动 —— 治理变更须用户裁)
# AUTO_WATCH : 判据未过 / 不可分辨 / 样本门未达 ⇒ 自动续观, 无需用户动作
# WINDOW     : 等预注册观察窗到期自动结算, 窗内禁改
CURATED: dict[str, dict] = {
    "futu_channel_top_movers": {
        "plain": "涨幅榜前 100（追高族）: 榜上标的之后到底是续涨还是回落",
        "conclusion": (
            "⚠️ 反直觉: 榜内横截面 IC=−0.44 / t=−13（榜首之后**续跌**）, 但实盘里买 top10 的 11 笔"
            "反而 +$15.20/笔 ⇒ 榜级与笔级**符号相反**、不可互相代入。用途未定 (排除信号? 反向候选池? "
            "并入既有追高族?) 三条路都要另立预注册"),
        "cls": "NEEDS_USER",
    },
    "futu_channel_iv_hv": {
        "plain": "财报前 IV/HV 高低能不能预告『这单摸不摸得到肉』",
        "conclusion": "三个波动桶都没测出额外效应（medium 差 +$35.7 但日聚类 t=2.03 / MDE±52 不可分辨; high 无效应）⇒ 无残存效应",
        "cls": "AUTO_WATCH",
    },
    "futu_channel_ofi_baseline": {
        "plain": "订单流失衡基线（给资金流做对照用的那把尺子）",
        "conclusion": "只测到『资金流相对它无增量』(−0.12); 它**自身**的预测力尚未单独测 ⇒ 不构成停采依据",
        "cls": "AUTO_WATCH",
    },
    "gap_chase_gate": {
        "plain": "跳空 >5% 还追进去, 拦掉这笔划不划算",
        "conclusion": "被拦 n=31 笔均 −$16.6 ✓、样本 ✓, 但单侧符号检验 p=0.93 ✗ ⇒ 3 项过 2, 继续攒样（2026-09-21 重跑复现）",
        "cls": "AUTO_WATCH",
    },
    "entry_delay_confirm": {
        "plain": "等回调确认再入场, 是不是比直接追进去好",
        "conclusion": "全样本看着过线, 但前瞻子样本全不过 + 按日聚类 MDE±7.95 ≫ 效应 ⇒ 不可分辨, 不呈裁",
        "cls": "AUTO_WATCH",
    },
    "exit_disable_cf": {
        "plain": "某些退出理由如果关掉, 结果会变好还是变差",
        "conclusion": "每条规则的样本 n<50（门 50）⇒ 未到门槛",
        "cls": "AUTO_WATCH",
    },
    "extreme_gap_ban_shadow": {
        "plain": "极端跳空当日禁止开仓, 拦掉的是不是真该拦",
        "conclusion": "被拦组净 −$1,826 ✓、样本 ✓, 但**误杀率 53% > 40% ✗** ⇒ 判据未过, 维持纯观测",
        "cls": "AUTO_WATCH",
    },
    "gap_alignment": {
        "plain": "跳空方向与趋势一致的追单, 是不是更差",
        "conclusion": "各格样本 5~22 只（门 30）⇒ 未到门槛",
        "cls": "AUTO_WATCH",
    },
    "intraday_loss_breaker_a_prime": {
        "plain": "盘中亏损熔断如果上线, 会不会挡掉本该赚的单",
        "conclusion": "被挡笔期望 **+$45**（门要求 ≤−10, 方向相反）、累计挡掉 +$9,180; 但前瞻样本仅 14<30 ⇒ 按已记录规则不下结论",
        "cls": "AUTO_WATCH",
    },
    "kr3_direction_runup_gate": {
        "plain": "已经涨很多了还做多（追高禁令）",
        "conclusion": "多头被拦 n=36 均值 −1.9 但中位 +1.1（尾部驱动）; 空头被拦 n=16<30 ⇒ 不裁决, 延长观察",
        "cls": "AUTO_WATCH",
    },
    "penetration_shadow": {
        "plain": "价格穿透关键位之后的行为（假设集由 LLM 生成）",
        "conclusion": "基线穿透率 97.4%; 假设集 None（本次未调 LLM）⇒ 不判",
        "cls": "AUTO_WATCH",
    },
    "premarket_invalidation": {
        "plain": "盘前证伪条件 A/B/C（跳空/方向失效就作废这单）",
        "conclusion": "A/C 已按 2026-09-11 处置为『不转正』; B 执行样本 n=4<20 ⇒ 攒样中",
        "cls": "AUTO_WATCH",
    },
    "reentry_quick_tp_gate": {
        "plain": "再入场的『快止盈』被门挡住, 到底亏不亏",
        "conclusion": "n=13（门 10）刚够; 门禁与快收参数在 9/1–9/30 窗口内**禁改**, 到期一次性结算",
        "cls": "WINDOW",
    },
    "vwap_gate_ab": {
        "plain": "VWAP 相位门拦下的单, 是拦对了还是误杀",
        "conclusion": "补齐『按日聚类 MDE』后: 被拦组 +0.176% 但 MDE±0.39（是效应的 2.2 倍）⇒ **不可分辨**, 不改门; 原『倾向拦错』读数**作废**",
        "cls": "AUTO_WATCH",
    },
    "da_blocked_cf": {
        "plain": "被 DA 门拦下的方向性信号, 事后到底涨还是跌（拦得对不对）",
        "conclusion": "T+1 −0.051% 未越过 ±0.15% 判断线 ⇒ 继续攒样（T+5 −0.79% 仅一致性披露, 不参与裁决）",
        "cls": "AUTO_WATCH",
    },
}

VERDICT_PLAIN = {
    "KEEP_SAMPLING": "还看不出结论, 继续攒样",
    "INSUFFICIENT_N": "样本不够, 不下结论",
    "NO_RESIDUAL": "没测出额外效应",
    "IC_SIGNIFICANT_GRADUATE_CANDIDATE": "判据显著, 但用法待定",
    "GATE_EFFECTIVE": "门拦得对, 维持",
    "OVER_BLOCKING_SUSPECT": "疑似误杀, 该裁",
    "TAIL_ONLY": "只在极端情况有用",
    "RETIRED_NO_PROMOTION": "已决定不转正",
}

_VERDICT_KEYS = ("verdict", "裁决", "verdict_label", "decision", "conclusion")


def _artifact_verdict(artifact: str) -> str:
    """尽力从产物 json 里取一个裁决码 (取不到返回空串, 不臆造)。"""
    p = Path(str(artifact).split(" (")[0].strip())
    if not p.is_absolute():
        p = REPO / p
    if not p.exists() or p.suffix != ".json":
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                             # noqa: BLE001
        return ""
    if not isinstance(d, dict):
        return ""
    for k in _VERDICT_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if d.get("ic_significant") is True:
        return "IC_SIGNIFICANT_GRADUATE_CANDIDATE"
    return ""


def build_report(*, force: bool = False, dry: bool = False) -> str:
    from app.review.criteria_status import pending_adjudications

    rows = pending_adjudications()
    ready = [r for r in rows if r.get("ready")]
    held = [r for r in rows if not r.get("ready")]

    needs, watch, window, unknown = [], [], [], []
    for r in ready:
        cur = CURATED.get(r["id"])
        if cur is None:
            unknown.append(r)          # 新条目: 未收录 ⇒ 抬到待裁面 (不静默吞掉)
            continue
        item = dict(r, **cur)
        (needs if cur["cls"] == "NEEDS_USER" else window if cur["cls"] == "WINDOW" else watch).append(item)

    # 状态变化面: 上轮之后有新条目落终态 (KILLED/SUPERSEDED/GRADUATED) 也要抬 (结论已出)
    state = {}
    if STATE.exists() and not force:
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:                                         # noqa: BLE001
            state = {}
    sig_now = {r["id"]: f"{r['status']}|{CURATED.get(r['id'], {}).get('cls', 'UNKNOWN')}" for r in rows}
    changed = sorted(k for k, v in sig_now.items() if state.get(k) != v)
    first_run = not state

    if not force and not first_run and not changed:
        return ""                                                     # 静默 = 不推送

    L = ["📋 **影子验收清单**（只看不做、不参与交易的试验项）——有事情等你决定",
         "",
         "**背景 30 秒**：影子通道 = 先只观察、不碰交易的试验项。每条都预先注册了判据，攒够样本就要做一次裁决："
         "留着 / 停掉 / 提转正。过去只有“入口”没有“出口”（样本够了就机械标 READY，但 READY 只代表样本量够、"
         "不代表判据过），所以清单越长越没人看。这条通知只在**待裁面有变化**时出现，不是日报。",
         ""]

    if needs or unknown:
        L.append(f"**① 需要你决定（{len(needs) + len(unknown)} 项）**")
        for it in needs:
            L.append(f"- **{it['plain']}**（`{it['id']}`，样本 {it['progress']}）")
            L.append(f"  结论：{it['conclusion']}")
        for r in unknown:
            v = _artifact_verdict(r["artifact"])
            L.append(f"- **{r['id']}**（新条目, 未收录白话名；样本 {r['progress']}）")
            L.append(f"  判据：{str(r['criteria'])[:120]}")
            L.append(f"  读数：{VERDICT_PLAIN.get(v, v) or '尚未取到裁决码 — 需跑 ' + str(r.get('eval_script') or '（无 eval 脚本）')}")
        L.append("")
    else:
        L.append("**① 需要你决定：当前 0 项** ✅（我不会自己动任何治理变更）")
        L.append("")

    if watch:
        L.append(f"**② 判据已读完、自动续观（{len(watch)} 项，不用你管）**")
        for it in watch:
            L.append(f"- `{it['id']}`（{it['progress']}）：{it['conclusion'][:96]}")
        L.append("")
    if window:
        L.append(f"**③ 等预注册窗口到期自动结算（{len(window)} 项，窗内禁改）**")
        for it in window:
            L.append(f"- `{it['id']}`（{it['progress']}）：{it['conclusion']}")
        L.append("")

    if changed:
        moved = [r for r in held if r["id"] in changed] + [
            r for r in ready if r["id"] in changed and r["id"] not in {i["id"] for i in needs + watch + window}]
        if moved:
            L.append(f"**④ 状态变化，结论已出（{len(moved)} 项）**")
            for r in moved:
                cur = CURATED.get(r["id"], {})
                note = cur.get("conclusion", "（未收录白话结论 — 见注册表 notes）")
                L.append(f"- `{r['id']}` → **{r['status']}**：{note[:110]}")
            L.append("")

    if first_run:
        L.append("_（首次运行 ⇒ 输出全量；之后只在待裁面变化时出现。）_")
    else:
        L.append(f"_（本次变化 {len(changed)} 项：{', '.join(changed[:8])}）_")

    if not dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sig_now, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印, 不写 state")
    ap.add_argument("--force", action="store_true", help="忽略 state, 强制输出全量")
    a = ap.parse_args()
    out = build_report(force=a.force, dry=a.dry)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())