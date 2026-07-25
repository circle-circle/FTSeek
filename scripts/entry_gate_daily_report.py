#!/usr/bin/env python3
"""Entry Gate v3 · 每日复盘报告生成器.

读 data/entry_timing_shadow.jsonl (VWAP 门特征+决策) + join data/ftseek_us.db 的
playbook_instructions (成交/PnL, 按 inst_id), 输出结构一致的 markdown 复盘 section.
可 --append-log 直接追加到 docs/proposals/entry-gate-v3-live-log.md, 积累证据链.

度量边界 (见 log 文件头): 实盘模式被拦单无 PnL 反事实; 靠"立即 vs 延迟入场"间接看择时价值;
先看 vwap_populated 率判断门是否真在工作 (低=fail-open 空转).

用法:
    python scripts/entry_gate_daily_report.py [--date YYYY-MM-DD] [--append-log]
    # 不带 --date 默认取 jsonl 中最新交易日
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = ROOT / "data" / "entry_timing_shadow.jsonl"
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_LOG = ROOT / "docs" / "proposals" / "entry-gate-v3-live-log.md"


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.0f}%" if d else "n/a"


def _load_rows(jsonl: Path, date: str | None):
    """读 jsonl, 按 ts 日期过滤; 返回 (rows, resolved_date)."""
    if not jsonl.exists():
        return [], date
    rows = []
    with open(jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows.append(r)
    if not rows:
        return [], date
    if date is None:
        date = max((str(r.get("ts", ""))[:10] for r in rows), default=None)
    rows = [r for r in rows if str(r.get("ts", ""))[:10] == date]
    return rows, date


def _fetch_instruction_outcomes(db: Path, inst_ids: list):
    """按 id 批量查 playbook_instructions 成交/PnL; best-effort."""
    out = {}
    ids = [i for i in inst_ids if i is not None]
    if not ids or not db.exists():
        return out
    try:
        conn = sqlite3.connect(str(db))
        qmarks = ",".join("?" * len(ids))
        cur = conn.execute(
            f"SELECT id, executed, triggered, execution_price, pnl, pnl_pct, "
            f"outcome, exit_reason FROM playbook_instructions WHERE id IN ({qmarks})",
            ids,
        )
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            out[d["id"]] = d
        conn.close()
    except Exception as e:
        print(f"# [warn] DB join 失败 (报告仍出, PnL 段留空): {e}")
    return out


def _is_filled(o: dict | None) -> bool:
    if not o:
        return False
    if int(o.get("executed") or 0) != 1:
        return False
    if float(o.get("execution_price") or 0) <= 0:
        return False
    if str(o.get("outcome") or "") == "already_held":
        return False
    return True


def _pnl_stats(pnls: list):
    """closed pnl 列表 → (n_closed, wins, wr_str, net, avg)."""
    closed = [p for p in pnls if p is not None]
    if not closed:
        return 0, 0, "n/a", 0.0, 0.0
    wins = sum(1 for p in closed if p > 0)
    net = sum(closed)
    return len(closed), wins, _pct(wins, len(closed)), net, net / len(closed)


def build_section(rows: list, date: str, db: Path) -> str:
    if not rows:
        return f"## {date}\n\n_无 entry_timing_shadow 样本 (当日 auto_execute 未产生触发评估, 或 regime/risk 闸门整轮跳过)。_\n"

    n_rows = len(rows)
    # 按 inst_id 聚合
    by_inst: dict = defaultdict(list)
    for r in rows:
        by_inst[r.get("inst_id")].append(r)
    n_inst = len(by_inst)

    # 源分布按指令计 (每指令取第一行的 source)
    src_counter = Counter()
    for rs in by_inst.values():
        src_counter[str(rs[0].get("source") or "?")] += 1

    # A. 门是否在工作
    pop_rows = sum(1 for r in rows if r.get("vwap_populated"))
    inst_pop = sum(1 for rs in by_inst.values() if any(r.get("vwap_populated") for r in rs))
    block_rows = sum(1 for r in rows if r.get("gate_block"))

    # B. 拦截行为 (按指令)
    outcomes = _fetch_instruction_outcomes(db, list(by_inst.keys()))
    blocked_ever = {iid for iid, rs in by_inst.items() if any(r.get("gate_block") for r in rs)}
    filled = {iid for iid in by_inst if _is_filled(outcomes.get(iid))}
    b_then_filled = blocked_ever & filled
    b_never_filled = blocked_ever - filled
    immediate = filled - blocked_ever

    # C. 成交单表现
    def _pnls(iids):
        return [outcomes.get(i, {}).get("pnl") for i in iids if _is_filled(outcomes.get(i))]

    n_all, w_all, wr_all, net_all, avg_all = _pnl_stats(_pnls(filled))
    _, _, wr_imm, net_imm, avg_imm = _pnl_stats(_pnls(immediate))
    _, _, wr_del, net_del, avg_del = _pnl_stats(_pnls(b_then_filled))

    # A 段自动判定
    pop_ratio = pop_rows / n_rows if n_rows else 0
    if pop_ratio < 0.2:
        gate_verdict = f"⚠️ populated 率仅 {_pct(pop_rows, n_rows)} — 门大概率 fail-open 空转, 过滤效果近零, 下方拦截/PnL 仅供参考"
    elif block_rows == 0:
        gate_verdict = f"✅ 数据充足 (populated {_pct(pop_rows, n_rows)}) 但本日 0 拦截 — 所有触发均与 VWAP 同向"
    else:
        gate_verdict = f"✅ 门在工作: populated {_pct(pop_rows, n_rows)}, 拦截 {block_rows} 行"

    src_str = " / ".join(f"{k}={v}" for k, v in src_counter.most_common())

    lines = [
        f"## {date}",
        "",
        f"**样本**: rows={n_rows} | 指令数={n_inst} | 源: {src_str}",
        "",
        "### A. 门是否在工作",
        f"- vwap_populated: 行级 {pop_rows}/{n_rows} ({_pct(pop_rows, n_rows)}) · 指令级 {inst_pop}/{n_inst} 至少一次有值",
        f"- gate_block 事件: {block_rows} 次 (占 populated 行 {_pct(block_rows, pop_rows)})",
        f"- 判定: {gate_verdict}",
        "",
        "### B. 拦截行为 (按指令)",
        f"- 曾被拦 blocked_ever: {len(blocked_ever)}  →  拦后成交(延迟) {len(b_then_filled)} / 从未成交(反事实丢失) {len(b_never_filled)}",
        f"- 全程未拦直接成交 immediate: {len(immediate)}",
        "",
        "### C. 成交单表现 (join playbook_instructions.pnl)",
        f"- 全部成交: {n_all} 单 · 胜率 {wr_all} · 净PnL ${net_all:.2f} · 均值 ${avg_all:.2f}",
        f"- 立即入场: WR {wr_imm} · 均值 ${avg_imm:.2f}  ‖  延迟入场(拦后): WR {wr_del} · 均值 ${avg_del:.2f}",
        "",
        "### D. 备注 / 异常",
        f"- (自动) {gate_verdict}",
        "- (人工补充: )",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD; 默认取 jsonl 最新交易日")
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--append-log", action="store_true", help="追加到 log 文件")
    args = ap.parse_args()

    rows, date = _load_rows(Path(args.jsonl), args.date)
    if date is None:
        print(f"# 无数据: {args.jsonl} 不存在或为空 (门今日尚未产生任何触发评估)。")
        return
    section = build_section(rows, date, Path(args.db))
    print(section)

    if args.append_log:
        if not rows:
            print(f"# [skip] {date} 无样本, 不 append。")
            return
        log = Path(args.log)
        if not log.exists():
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("# Entry Gate v3 · 实盘每日复盘 Log\n\n", encoding="utf-8")
        with open(log, "a", encoding="utf-8") as f:
            f.write("\n" + section + "\n---\n")
        print(f"# ✅ 已 append 到 {log}")


if __name__ == "__main__":
    main()
