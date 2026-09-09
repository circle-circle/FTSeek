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

    text = "\n".join(lines) + "\n"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"unit_cells_{today}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"[unit_cell_readout] 报告已落盘: {out}")


if __name__ == "__main__":
    main()
