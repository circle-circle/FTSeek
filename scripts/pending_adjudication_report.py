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
# 白话词典已提为 SSOT: app/review/shadow_lexicon.py (判决书推送与影子验收清单共用一份)。
# 本模块只做分类与排版, 不再自带白话 —— 两处各写一份会导致同一影子两种说法。
from app.review.shadow_lexicon import SHADOW_LEXICON as CURATED  # noqa: E402
# 推送截断 SSOT (RFC-109④收口): 原裸切片 conclusion[:96]/[:100] 无句读边界也无「…」标记
# ⇒ 长结论被拦腰截成半句且读者看不出被截断 (实测 exit_param_shadow/premarket_invalidation 两条)。
from app.review.bundle_render import clip_for_push  # noqa: E402
# RFC-139 L1: 判据 schema (决策源机器可读投影; 未收录回落注册表原文)
from app.review.criteria_spec import get_spec  # noqa: E402

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


# ── 判据实测值栏 (DEB-20261007 F4, 2026-10-07 用户裁「按建议执行」) ──────────
# 为什么: READY 只代表样本量够, 不代表判据过 (review-system-design P4; 2026-09-13 实锤
# 5 个 READY 逐个跑 eval_script 0 个过门)。通知面过去只给白话结论快照, 用户裁决时
# 还得自己去跑脚本 — 本栏把产物 json 里的**实测读数**直接抬到待裁行下方。
# 边界: 只读产物 json 的裁决码与少量数值键, **不同步跑 eval_script** (报表是通知载体,
# 跑脚本可能拉行情/耗时; 判据实测的权威入口仍是 eval_script 本身, 栏内给出命令)。
_READOUT_NUM_KEYS = ("n", "n_days", "n_settled", "n_pairs", "coverage", "ic", "t_stat",
                     "expectancy", "blocked_expectancy", "hit_rate", "p_value", "sign_p")


def _era_line(era) -> str:
    """RFC-138 借鉴①: 参数时代漂移 ⇒ 一行 ⚠️ 标注 (旧结论防无标注复活)。

    只在 applies=False 时输出; True/None(不可判定) 静默 — 报告不为无漂移条目加噪音,
    fail-open 也不冒充判定 (era 的 note 已含失败原因, 调试走 --json/日志面)。
    """
    if not isinstance(era, dict) or era.get("applies") is not False:
        return ""
    return (f"⚠️ 参数时代漂移（RFC-138）：该结论裁决于退出参数 `{era.get('then')}` 时代"
            f"（{era.get('note', '')}）— 引用前须复算或重裁")


def _background_block(it: dict) -> list[str]:
    """RFC-139 L4 通俗决策背景: 每条待裁推送带「这是什么/决策源/卡在哪/裁什么」四行。

    全部从注册表 SSOT 字段 + 白话词典 + L1 spec 推导, 不另写第二真源;
    未收录项回落注册表原文 (判据首 80 字) 并标注「未收录白话名」。
    """
    spec = get_spec(it.get("id", ""))
    lines = ["  📎 背景（防遗忘/决策源）:"]
    # 这是什么: 词典 plain; 未收录回落 id + progress
    lex = CURATED.get(it.get("id", ""), {})
    if lex.get("plain"):
        lines.append(f"    · 这是什么: {lex['plain']}")
    else:
        lines.append(f"    · 这是什么: 未收录白话名（`{it.get('id')}`）— 以注册表 description 为准")
    # 决策源: spec.source 优先 (机器可读出处); 回落判据原文首 80 字
    if spec and spec.source:
        lines.append(f"    · 决策源: {spec.source}")
    crit = (it.get("criteria") or "").strip()
    if crit:
        shown = crit if len(crit) <= 80 else crit[:80] + "…"
        lines.append(f"    · 判据原文: {shown}")
    if spec and spec.known_gap:
        lines.append(f"    · 已知缺口: {spec.known_gap}")
    # 你要决定什么: 按 lexicon 分类给出明确动作
    cls = lex.get("cls", "")
    if cls == "NEEDS_USER":
        lines.append("    · 你要决定: 立刻处置（补结算器/裁停采/裁转正）— 不要放着, 会混淆决策源")
    else:
        lines.append("    · 你要决定: 暂无（自动续观; 下次达门/漂移时再抬出）")
    return lines


def _artifact_readout(artifact: str, eval_script, item_id: str = "") -> str:
    """产物 json → 一行「判据实测」读数 (取不到 → 指路 eval_script, 不臆造)。

    RFC-139 修复: `artifact` 是注册表 data_path (采集 jsonl), 判据读数在 verdict 产物 json。
    L1 spec 有 `verdict_artifact` 时优先读它 (如 entry_band_widen_verdict_latest.json);
    未收录 spec 回落原行为 (读 data_path, jsonl 取不到 ⇒ 指路 eval_script)。
    """
    spec = get_spec(item_id) if item_id else None
    if spec and spec.verdict_artifact:
        artifact = spec.verdict_artifact
    v = _artifact_verdict(artifact)
    p = Path(str(artifact).split(" (" )[0].strip())
    if not p.is_absolute():
        p = REPO / p
    nums = {}
    if p.exists() and p.suffix == ".json":
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k in _READOUT_NUM_KEYS:
                    val = d.get(k)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        nums[k] = round(float(val), 4) if isinstance(val, float) else val
        except Exception:                                         # noqa: BLE001
            pass
    parts = []
    if v:
        parts.append(VERDICT_PLAIN.get(v, v))
    if nums:
        parts.append(" ".join(f"{k}={val}" for k, val in list(nums.items())[:6]))
    if parts:
        return "判据实测：" + "；".join(parts)
    if eval_script:
        return f"判据实测：产物无读数 — 跑 `{eval_script}` 取 (勿凭 READY 状态当判据过)"
    return "判据实测：无产物读数且未挂 eval_script (有入口无出口 — 须先补结算器)"


def build_report(*, force: bool = False, dry: bool = False) -> str:
    from app.review.criteria_status import pending_adjudications

    rows = pending_adjudications()
    ready = [r for r in rows if r.get("ready")]
    held = [r for r in rows if not r.get("ready")]

    needs, watch, window = [], [], []
    for r in ready:
        cur = CURATED.get(r["id"])
        # 待裁面以 SSOT 为准 (`criteria_status.needs_adjudication` —— 已算进"已读判据登记 + 重看闹钟");
        # 本脚本只补白话名与结论。旧 schema/打桩无该键时回落到人工分类 (cls == NEEDS_USER)。
        decided = r.get("needs_adjudication")
        if decided is None:
            decided = bool(cur) and cur["cls"] == "NEEDS_USER"
        fallback = {"plain": f"{r['id']}（未收录白话名）",
                    "conclusion": "（未收录白话结论 — 请跑 " + str(r.get("eval_script") or "无 eval 脚本") + " 取读数）"}
        item = dict(r, **(cur or fallback))
        if decided:
            needs.append(item)
        elif cur is None or cur["cls"] == "WINDOW":
            window.append(item)
        else:
            watch.append(dict(r, **cur))

    # 状态变化面: 上轮之后有新条目落终态 (KILLED/SUPERSEDED/GRADUATED) 也要抬 (结论已出)
    state = {}
    if STATE.exists() and not force:
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:                                         # noqa: BLE001
            state = {}
    sig_now = {r["id"]: f"{r['status']}|{r.get('needs_adjudication')}|{r.get('recheck_after')}|"
                         f"{CURATED.get(r['id'], {}).get('cls', 'UNKNOWN')}"
                for r in rows}
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

    if needs:
        L.append(f"**① 需要你决定（{len(needs)} 项）**")
        for it in needs:
            L.append(f"- **{it['plain']}**（`{it['id']}`，样本 {it['progress']}）")
            L.append(f"  结论：{it['conclusion']}")
            # F4 (DEB-20261007): 判据实测值栏 — READY≠判据过, 裁决前先看实测读数
            L.append(f"  {_artifact_readout(it.get('artifact', ''), it.get('eval_script'), it.get('id', ''))}")
            # RFC-139 L4: 通俗决策背景 (这是什么/决策源/判据原文/已知缺口/要裁什么)
            L.extend(_background_block(it))
            # RFC-138: 参数时代漂移标注 (applies=False 才出现; 漂移时裁决参照已失效)
            _el = _era_line(it.get("verdict_era"))
            if _el:
                L.append(f"  {_el}")
        L.append("")
    else:
        L.append("**① 需要你决定：当前 0 项** ✅（我不会自己动任何治理变更）")
        L.append("")

    if watch:
        L.append(f"**② 判据已读完、自动续观（{len(watch)} 项，不用你管）**")
        for it in watch:
            due = it.get("recheck_after") or "未登记"
            L.append(f"- `{it['id']}`（{it['progress']}，重看 {due}）：{clip_for_push(it['conclusion'], 96)}")
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
                v = _artifact_verdict(r.get("artifact", ""))
                extra = f"（产物裁决码：{VERDICT_PLAIN.get(v, v)}）" if v else ""
                L.append(f"- `{r['id']}` → **{r['status']}**：{clip_for_push(note, 100)}{extra}")
                _ro = _artifact_readout(r.get("artifact", ""), r.get("eval_script"), r.get("id", ""))
                if not v:  # 有裁决码时 extra 已含, 不重复
                    L.append(f"  {_ro}")
                # RFC-138: 终态结论被抬出时同样受「旧结论复活」风险, 漂移即标注
                _el = _era_line(r.get("verdict_era"))
                if _el:
                    L.append(f"  {_el}")
                # RFC-139 L4: 终态抬出也带背景块 (防遗忘/混淆决策源)
                L.extend(_background_block(r))
            L.append("")

    if first_run:
        L.append("_（首次运行 ⇒ 输出全量；之后只在待裁面变化时出现。）_")
    else:
        L.append(f"_（本次变化 {len(changed)} 项：{', '.join(changed[:8])}）_")

    # DEB-20261008 F4/B5 (2026-10-08 用户裁「继续」): 已否决轴附录 — 防重复立案。
    # 只在报告本就非空时附加 (静默契约不动: 无变化即无推送)。
    L.append("**⑤ 已否决轴（防重复立案，附复算脚本）**")
    L.append("- scale_out 盈利阶梯分批止盈（2%/3%/4% 出半仓/全出）：DEB-20260627 真跑禁用 "
             "+ DEB-20261008 三重证据否决（复算: `scripts/deb20261008_scaleout_binding.py` / "
             "`_cf.py` / `_arith.py`，验证器 `scripts/verify_deb20261008.py`）")
    L.append("")

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