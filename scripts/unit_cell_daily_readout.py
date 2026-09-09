#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unit_cell_daily_readout — 单元级盈亏每日读数 (record-only, 零交易行为变更).

数据源单一真源: data/shadow/gap_alignment_shadow.jsonl (由 gap_alignment_shadow.py
每日 cron 52 5 * * 2-6 落, 桶/窗口口径唯一真源在 bucket_gap, 本脚本只聚合不重算)。

为什么三窗并列: 9月攒样期实证单元表现随 regime 漂移 — flat_or_down_open 历史窗
(6/27-8/31) 净 -1092 而 9/1-9/8 +219 (n=10, WR90%)。单窗读数会误导裁决, 故:
  全历史窗 | 滚动20交易日窗 | 当月窗 三窗并列。

裁决触发器 (预注册判据, 见 docs/proposals/unit-cell-shutdown-criteria-20260909.md):
  SHUTDOWN_PROPOSE: 全历史 n>=30 且 净<0, 且 滚动20日 n>=10 且 净<0 (双窗同向出血)
  CONFLICT_WATCH:   全历史 净<-300 而 滚动20日 净>0 (或反之) ⇒ 证据冲突, 维持观察, 严禁单窗裁决
  本脚本只输出提案文本, 不执行任何关停。

附加小节 (DEB-20260909 §5.2, 2026-09-09 用户裁决「同意」落地, 均 record-only):
  D1' 极端跳空禁入影子: 全日窗×双向×双阈值腿(−3/−3.5)×按源分层的被拦篮读数,
    毕业判据 每腿 n≥30 ∧ 被拦净<0 ∧ 误杀率≤40% ∧ 两腿同向。
  D3' 按源监控: 每源 SL_INITIAL 率 / mfe0 率 (UE 通道独立风险因子监控, 无毕业判据)。

幂等: 纯读 jsonl + 覆写当日报告文件。盘后 cron 55 5 * * 2-6 (gap_alignment_shadow 之后)。
用法: python3 scripts/unit_cell_daily_readout.py [--date YYYY-MM-DD]
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
JSONL = REPO / "data" / "shadow" / "gap_alignment_shadow.jsonl"
REPORT_DIR = REPO / "data" / "reports"

# 预注册触发阈值 (判据推导: 血池单元历史均亏 ~$11/笔, 滚动20日约10-15笔/单元
# ⇒ 双窗同向阈值取 -150/-300 与血池量级匹配; 冲突阈值 300 同上)
ROLL_TRADING_DAYS = 20
SHUTDOWN_HIST_MIN_N = 30
SHUTDOWN_ROLL_MIN_N = 10
CONFLICT_ABS_NET = 300.0

# 桶名白名单 (契约钉住: 本脚本不得引入新桶, 新桶必须先在 bucket_gap 落地)
KNOWN_BUCKETS = {
    "gap_up_0_4", "flat_or_down_open", "gap_up_4plus", "unknown",  # LONG
    "gap_down_2plus", "flat_slight_down",  # SHORT 侧专有
}
KNOWN_WINDOWS = {"open_0930_1000", "after_1000"}

# ── D1' 极端跳空禁入影子 (DEB-20260909 §5.2, 2026-09-09 用户裁决「同意」落地) ──
# 规格 (逐字): 全日窗 (去掉开盘窗限定) × 双向 (aligned_gap_pct 已方向归一 ⇒ LONG/SHORT
# 同判) × 双阈值并行腿 (−3 与 −3.5, 阈值扫描不锁单点) × 按源分层 (DA/UE/REENTRY)。
# 禁入谓词: ag ≤ −T (极端逆向) 或 ag ≥ +4 (极端顺向追高)。
# 被拦净额 = 被拦笔实际 pnl_net 合计 (影子不拦 ⇒ 实际结局=反事实结局, <0 ⇒ 禁入省钱);
# 误杀率 = 被拦笔中 pnl_net>0 占比。
# 毕业判据 (逐字): 每腿 n≥30 ∧ 被拦单反事实净<0 ∧ 误杀率≤40% ∧ 两腿结论同向;
# 任一不满足则降纯观测。本脚本只读数不执行任何禁入。
D1_AGAINST_LEGS = (3.0, 3.5)
D1_CHASE_LIMIT = 4.0
D1_MIN_N = 30
D1_MAX_KILL_RATE = 0.40

# D3' 按源监控口径 (DEB-20260909 §5.2): SL_INITIAL 率 = exit_reason 计数占比;
# mfe0 率 = mfe_pct 精确为 0 的占比 (mfe 缺失行不计入分母)。
# 监控项无毕业判据; UE 通道独立风险因子 (SL_INITIAL 率 26.1% vs DA 7.5%, 取证见 DEB 第2轮)。


def _d1_banned(ag, leg):
    """D1' 禁入谓词: 方向归一跳空 ag 极端逆向 (≤−leg) 或极端顺向追高 (≥+CHASE_LIMIT)."""
    return ag <= -leg or ag >= D1_CHASE_LIMIT


def _d1_collect(rows):
    """按 (腿, 源) 聚被拦篮. 返回 ({leg: {src|ALL: {n, net, kills}}}, ag缺失行数)."""
    legs = {t: defaultdict(lambda: {"n": 0, "net": 0.0, "kills": 0})
            for t in D1_AGAINST_LEGS}
    n_no_gap = 0
    for r in rows:
        ag = r.get("aligned_gap_pct")
        if ag is None:
            n_no_gap += 1
            continue
        ag = float(ag)
        net = float(r.get("pnl_net") or 0)
        src = r.get("source") or "?"
        for t in D1_AGAINST_LEGS:
            if not _d1_banned(ag, t):
                continue
            for key in (src, "ALL"):
                a = legs[t][key]
                a["n"] += 1
                a["net"] += net
                a["kills"] += 1 if net > 0 else 0
    return legs, n_no_gap


def _d1_leg_verdict(n, net, kills):
    """腿内三判据 (n门/被拦净<0 严格/误杀率≤上限). 返回 (ok_n, ok_net, ok_kr)."""
    kr = kills / n if n else None
    return (n >= D1_MIN_N, net < 0, kr is not None and kr <= D1_MAX_KILL_RATE)


def _fmt_d1(rows, roll_rows):
    legs, n_no_gap = _d1_collect(rows)
    roll_legs, _ = _d1_collect(roll_rows)
    lines = ["", "### D1' 极端跳空禁入影子 (DEB-20260909 §5.2, record-only)", "",
             "> 全日窗 × 双向 (ag 方向归一 ⇒ 双向同判) × 双阈值腿; "
             "被拦篮 = 「若禁入则不交易」反事实, 净额 = 被拦笔实际 pnl_net 合计 "
             "(<0 ⇒ 禁入省钱); 误杀率 = 被拦笔中盈利占比。",
             f"> 判读基数 = 全历史窗 (毕业判据所在); 滚动 {ROLL_TRADING_DAYS} 日单列一行看 regime 漂移。"
             f"ag 缺失跳过 {n_no_gap} 行。"
             f"毕业: 每腿 n≥{D1_MIN_N} ∧ 被拦净<0 ∧ 误杀率≤{D1_MAX_KILL_RATE:.0%} "
             "∧ 两腿同向; 任一不满足降纯观测。", ""]
    verdicts = {}
    for t in D1_AGAINST_LEGS:
        by_src = legs[t]
        a = by_src.get("ALL") or {"n": 0, "net": 0.0, "kills": 0}
        ok_n, ok_net, ok_kr = _d1_leg_verdict(a["n"], a["net"], a["kills"])
        verdicts[t] = ok_n and ok_net and ok_kr
        kr_s = f"{a['kills']/a['n']:.0%}" if a["n"] else "—"
        ra = roll_legs[t].get("ALL") or {"n": 0, "net": 0.0, "kills": 0}
        lines.append(f"**腿「ag≤−{t} 或 ≥+{D1_CHASE_LIMIT}」**: 被拦 n={a['n']} "
                     f"(门 {D1_MIN_N}) 净 {a['net']:+.2f} 误杀率 {kr_s} "
                     f"| 滚动{ROLL_TRADING_DAYS}日 n={ra['n']} 净 {ra['net']:+.2f} ⇒ "
                     f"{'腿内三判据全过' if verdicts[t] else '未过'} "
                     f"(n{'✓' if ok_n else '✗'} 净{'✓' if ok_net else '✗'} "
                     f"误杀{'✓' if ok_kr else '✗'})")
        lines += ["", "| 源 | 被拦 n | 被拦净额$ | 误杀率 |", "|---|---|---|---|"]
        for src in sorted(k for k in by_src if k != "ALL"):
            s = by_src[src]
            skr = f"{s['kills']/s['n']:.0%}" if s["n"] else "—"
            lines.append(f"| {src} | {s['n']} | {s['net']:+.2f} | {skr} |")
        lines.append(f"| **ALL** | **{a['n']}** | **{a['net']:+.2f}** | **{kr_s}** |")
        lines.append("")
    agree = len(verdicts) == len(D1_AGAINST_LEGS) and len(set(verdicts.values())) == 1
    status = " / ".join(f"−{t}腿{'过' if v else '不过'}" for t, v in verdicts.items())
    graduated = agree and all(verdicts.values())
    lines.append(f"**两腿同向**: {'✅ 同向' if agree else '❌ 分歧'} ({status}) ⇒ "
                 + ("🎓 **达毕业线 — 提请用户裁决** (本脚本不执行任何禁入)"
                    if graduated else "未达毕业线, 维持纯观测攒样。"))
    return lines


def _fmt_by_source(rows, title):
    """D3' 按源监控表 (DEB-20260909 §5.2 ①): 每源 n/净额/胜率/SL_INITIAL率/mfe0率."""
    by = defaultdict(lambda: {"n": 0, "net": 0.0, "wins": 0,
                              "sl_init": 0, "mfe0": 0, "mfe_known": 0})
    for r in rows:
        a = by[r.get("source") or "?"]
        net = float(r.get("pnl_net") or 0)
        a["n"] += 1
        a["net"] += net
        a["wins"] += 1 if net > 0 else 0
        if r.get("exit_reason") == "SL_INITIAL":
            a["sl_init"] += 1
        m = r.get("mfe_pct")
        if m is not None:
            a["mfe_known"] += 1
            if float(m) == 0:
                a["mfe0"] += 1
    lines = [f"\n### D3' 按源监控 — {title} (DEB-20260909 §5.2 ①, 纯监控无毕业判据)", "",
             "| 源 | n | 胜率 | 净额$ | SL_INITIAL | mfe0 |",
             "|---|---|---|---|---|---|"]
    for src in sorted(by):
        a = by[src]
        wr = f"{a['wins']/a['n']*100:.0f}%" if a["n"] else "—"
        sr = f"{a['sl_init']/a['n']*100:.1f}%({a['sl_init']})" if a["n"] else "—"
        mr = (f"{a['mfe0']/a['mfe_known']*100:.1f}%({a['mfe0']})"
              if a["mfe_known"] else "—")
        lines.append(f"| {src} | {a['n']} | {wr} | {a['net']:+.2f} | {sr} | {mr} |")
    lines.append("> mfe0 = mfe_pct 精确为 0 (mfe 缺失不计分母); "
                 "UE 通道 SL_INITIAL 率 26.1% vs DA 7.5% (post-8/14) 为监控基线 (DEB 第2轮)。")
    return lines


def _load_rows():
    if not JSONL.exists():
        return []
    rows = []
    for ln in JSONL.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception as _e:
            print(f"[unit_cell_readout] jsonl 坏行跳过: {_e} | {ln[:100]!r}")
    return rows


def _agg(rows):
    """按 (direction, gap_bucket, window) 聚合 n/净额/胜率. 净额口径 = pnl_net (费后)."""
    agg = defaultdict(lambda: {"n": 0, "net": 0.0, "wins": 0})
    for r in rows:
        b, w = r.get("gap_bucket"), r.get("window")
        if b not in KNOWN_BUCKETS or w not in KNOWN_WINDOWS:
            print(f"[unit_cell_readout] 未知桶/窗口, 不计入: {b}|{w} (trade_id={r.get('trade_id')})")
            continue
        a = agg[(r.get("direction", "?"), b, w)]
        net = float(r.get("pnl_net") or 0)
        a["n"] += 1
        a["net"] += net
        a["wins"] += 1 if net > 0 else 0
    return agg


def _fmt_table(title, agg):
    lines = [f"\n### {title}", "",
             "| 单元 (方向\\|跳空桶\\|窗口) | n | 胜率 | 净额$ |",
             "|---|---|---|---|"]
    for k in sorted(agg):
        a = agg[k]
        wr = f"{a['wins']/a['n']*100:.0f}%" if a["n"] else "—"
        lines.append(f"| {'|'.join(k)} | {a['n']} | {wr} | {a['net']:+.2f} |")
    total_net = sum(a["net"] for a in agg.values())
    total_n = sum(a["n"] for a in agg.values())
    lines.append(f"| **合计** | {total_n} | | **{total_net:+.2f}** |")
    return lines


def _check_triggers(hist_agg, roll_agg, month_agg):
    """预注册裁决触发器. 返回提案文本列表 (只读, 不执行).

    当月豁免 (2026-09-09 首跑实证补入): 滚动窗出血但当月窗 n>=10 且净>0 ⇒
    降级 CONFLICT_WATCH — flat_or_down_open 全历史-873/滚动-322 触发关停,
    但当月 n=10 WR90% +219 反阳 (regime 可能切换), 双窗判据会误杀正在修复的单元。
    判据设计声明: 本豁免见过 9 月样本 (后见之明), 自落地日起向前生效。"""
    out = []
    cells = set(hist_agg) | set(roll_agg)
    for cell in sorted(cells):
        h, r, m = hist_agg.get(cell), roll_agg.get(cell), month_agg.get(cell)
        h_n, h_net = (h["n"], h["net"]) if h else (0, 0.0)
        r_n, r_net = (r["n"], r["net"]) if r else (0, 0.0)
        m_n, m_net = (m["n"], m["net"]) if m else (0, 0.0)
        name = "|".join(cell)
        _month_rebound = m_n >= SHUTDOWN_ROLL_MIN_N and m_net > 0
        if (h_n >= SHUTDOWN_HIST_MIN_N and h_net < 0
                and r_n >= SHUTDOWN_ROLL_MIN_N and r_net < 0):
            if _month_rebound:
                out.append(f"- 🟡 CONFLICT_WATCH `{name}`: 全历史 n={h_n} 净{h_net:+.2f}, "
                           f"滚动{ROLL_TRADING_DAYS}日 n={r_n} 净{r_net:+.2f} 出血, "
                           f"但当月 n={m_n} 净{m_net:+.2f} 反阳 — 可能 regime 切换, "
                           f"维持观察严禁关停")
            else:
                out.append(f"- 🔴 SHUTDOWN_PROPOSE `{name}`: 全历史 n={h_n} 净{h_net:+.2f}, "
                           f"滚动{ROLL_TRADING_DAYS}日 n={r_n} 净{r_net:+.2f} — 双窗同向出血, "
                           f"建议按提案文档裁决关停该单元 (本脚本不执行)")
        elif (h_net < -CONFLICT_ABS_NET and r_net > 0 and r_n >= SHUTDOWN_ROLL_MIN_N) or \
             (h_net > CONFLICT_ABS_NET and r_net < 0 and r_n >= SHUTDOWN_ROLL_MIN_N):
            out.append(f"- 🟡 CONFLICT_WATCH `{name}`: 全历史 净{h_net:+.2f} vs "
                       f"滚动{ROLL_TRADING_DAYS}日 净{r_net:+.2f} (n={r_n}) — 证据冲突, "
                       f"维持观察, 严禁单窗裁决")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    args = ap.parse_args()
    today = args.date or datetime.now().strftime("%Y-%m-%d")

    rows = _load_rows()
    dates = sorted({r["date"] for r in rows if r.get("date")})
    roll_dates = set(dates[-ROLL_TRADING_DAYS:])
    month_prefix = today[:7]

    hist_agg = _agg(rows)
    roll_agg = _agg([r for r in rows if r.get("date") in roll_dates])
    month_agg = _agg([r for r in rows if (r.get("date") or "").startswith(month_prefix)])

    lines = [
        f"# 单元级盈亏每日读数 — {today}",
        "",
        "> 口径: PLAYBOOK_SIM 已平仓, **净额 (pnl_net, 费后)**; 桶×窗口口径 = "
        "`gap_alignment_shadow.py::bucket_gap` (单一真源); record-only, 零交易行为变更。",
        f"> 样本: {len(rows)} 行, {dates[0] if dates else '—'} → {dates[-1] if dates else '—'}",
    ]
    lines += _fmt_table("全历史窗", hist_agg)
    lines += _fmt_table(f"滚动 {ROLL_TRADING_DAYS} 交易日窗 "
                        f"({min(roll_dates) if roll_dates else '—'} → "
                        f"{max(roll_dates) if roll_dates else '—'})", roll_agg)
    lines += _fmt_table(f"当月窗 ({month_prefix})", month_agg)

    triggers = _check_triggers(hist_agg, roll_agg, month_agg)
    lines += ["", "### 裁决触发器 (预注册判据, 只提案不执行)", ""]
    lines += triggers if triggers else ["- 无触发"]

    roll_rows = [r for r in rows if r.get("date") in roll_dates]
    lines += _fmt_d1(rows, roll_rows)
    lines += _fmt_by_source(rows, "全历史窗")
    lines += _fmt_by_source(roll_rows, f"滚动 {ROLL_TRADING_DAYS} 交易日窗")

    text = "\n".join(lines) + "\n"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"unit_cells_{today}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"[unit_cell_readout] 报告已落盘: {out}")


if __name__ == "__main__":
    main()
