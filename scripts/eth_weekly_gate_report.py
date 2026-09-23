#!/usr/bin/env python3
"""延长时段**周复盘卡**: 每天/每阶段到底死在哪一道闸 (2026-09-23 用户令「先可见再打磨优化」).

为什么单独一张: 日报只回答"今天有没有成交", 回答不了"**一周下来瓶颈在哪一道闸**" ——
实测 (ET 9/22) 盘前价格闸拦 1827 / 盘后价格闸拦 1355, 而盘后预交易闸 197 里 market_cap_too_low ×133;
这两类"结构性供给问题"与"闸参数问题"要分开看, 否则容易误调闸。

数据源 (全部是真实落盘台账, 带时间戳 ⇒ 可按 ET 日聚合; 不估算):
  ① data/extended_hours_price_gate_rejects.jsonl      — 价格闸 (含 <$5/微盘的原始候选)
  ② data/extended_hours_pretrade_gate_rejects.jsonl   — 预交易闸 (市值/价差/深度/窗口/费用… 逐票原因)
  ③ data/extended_hours_earnings_gate_rejects.jsonl   — 财报门 (判据①②子原因 + coverage 覆盖度)
  ④ data/extended_hours_entry_executions.jsonl        — 下单尝试/成交
  ⑤ 执行器 stdout 的"闸计数"行 (无时间戳) ⇒ 以"最近轮次"形式附注, 不冒充某日

用法:
  python3 scripts/eth_weekly_gate_report.py                 # 最近 7 个 ET 日
  python3 scripts/eth_weekly_gate_report.py --days 14 --json
  python3 scripts/eth_weekly_gate_report.py --push          # 推一张周卡
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.eth_stage_daily_report import (  # noqa: E402
    EXEC_LOG, PRICE_GATE, PRETRADE_GATE, EXT_SESSIONS, _et_date_of, _read_jsonl, recent_rounds,
)

DATA = ROOT / "data"
EARN_GATE = DATA / "extended_hours_earnings_gate_rejects.jsonl"
ET = timedelta(hours=-4)


def default_end() -> str:
    """最近已收盘 ET 日 (20:00 前算前一天)."""
    now = datetime.utcnow() + ET
    return (now.date() if now.hour >= 20 else now.date() - timedelta(days=1)).isoformat()


def collect(days: List[str]) -> Dict[str, Any]:
    want = set(days)
    out: Dict[str, Any] = {d: {s: {"price_gate": 0, "pretrade": 0, "prete_reasons": Counter(),
                                   "earn_reasons": Counter(), "attempts": 0, "filled": 0,
                                   "cand_in_cal": 0, "cov": {}} for s in EXT_SESSIONS}
                           for d in days}
    for row in _read_jsonl(PRICE_GATE):
        d, s = _et_date_of(row.get("ts")), str(row.get("session") or "")
        if d in want and s in EXT_SESSIONS:
            out[d][s]["price_gate"] += int(row.get("n") or 0)
    for row in _read_jsonl(PRETRADE_GATE):
        d, s = _et_date_of(row.get("ts")), str(row.get("session") or "")
        if d in want and s in EXT_SESSIONS:
            out[d][s]["pretrade"] += int(row.get("n") or 0)
            for rj in row.get("rejects") or []:
                out[d][s]["prete_reasons"][str(rj.get("reason") or "?")] += 1
    for row in _read_jsonl(EARN_GATE):
        d, s = _et_date_of(row.get("ts")), str(row.get("session") or "")
        if d in want and s in EXT_SESSIONS:
            for k, v in (row.get("counts") or {}).items():
                out[d][s]["earn_reasons"][k] += int(v)
            cov = row.get("coverage") or {}
            if cov:
                out[d][s]["cand_in_cal"] = max(out[d][s]["cand_in_cal"], int(cov.get("candidates_in_calendar") or 0))
                out[d][s]["cov"] = cov
    for row in _read_jsonl(EXEC_LOG):
        d, s = _et_date_of(row.get("ts")), str(row.get("session") or "")
        if d in want and s in EXT_SESSIONS:
            out[d][s]["attempts"] += 1
            if row.get("ok") and float(row.get("filled_qty") or 0) > 0:
                out[d][s]["filled"] += 1
    return out


def render(days: List[str], agg: Dict[str, Any]) -> str:
    tot_pg = tot_pre = tot_att = tot_fill = 0
    pre_reasons: Counter = Counter()
    earn_reasons: Counter = Counter()
    lines = [f"### 🕒 延长时段周复盘 (最近 {len(days)} 个 ET 日: {days[0]} ~ {days[-1]})", "",
             "**① 每日 × 每阶段 (闸拦 → 下单尝试 → 成交)**", "",
             "| ET 日 | 阶段 | 价格闸拦 | 预交易闸拦 | 财报门拦 | 尝试 | 成交 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for d in days:
        for s in EXT_SESSIONS:
            v = agg[d][s]
            if not any([v["price_gate"], v["pretrade"], sum(v["earn_reasons"].values()), v["attempts"]]):
                continue
            tot_pg += v["price_gate"]
            tot_pre += v["pretrade"]
            tot_att += v["attempts"]
            tot_fill += v["filled"]
            pre_reasons.update(v["prete_reasons"])
            earn_reasons.update(v["earn_reasons"])
            lines.append(f"| {d} | {'盘前' if s=='premarket' else '盘后'} | {v['price_gate']} | {v['pretrade']} | "
                         f"{sum(v['earn_reasons'].values())} | {v['attempts']} | {v['filled']} |")
    lines += ["", f"**合计**: 价格闸拦 {tot_pg} · 预交易闸拦 {tot_pre} · 下单尝试 {tot_att} · 成交 {tot_fill}", ""]
    if pre_reasons:
        lines += ["**② 瓶颈排名 (预交易闸, 一周累计)** — 这行才是「该调哪个参」的依据", ""]
        for k, v in pre_reasons.most_common(8):
            lines.append(f"- `{k}` ×{v} ({100*v/max(tot_pre,1):.0f}%)")
        lines.append("")
    if earn_reasons:
        lines += ["**③ 财报门子原因 (判据①/②, 一周累计)**", ""]
        for k, v in earn_reasons.most_common(8):
            lines.append(f"- `{k}` ×{v}")
        lines.append("")
    cov = next((agg[d][s]["cov"] for d in reversed(days) for s in EXT_SESSIONS if agg[d][s]["cov"]), {})
    if cov:
        lines += ["**④ 覆盖度 (最近一次财报门读数)**",
                  f"- 当日日历行 {cov.get('calendar_rows_today')} 只 · 按源 {cov.get('calendar_by_source')} · "
                  f"同相位 {cov.get('calendar_same_phase')}", ""]
    r = recent_rounds(8)
    if r:
        lines += ["**⑤ 最近轮次 闸计数 (执行器 stdout, 无时间戳 ⇒ 只作活跃度佐证)**", r, ""]
    lines += ["_口径: 全部取真实落盘台账按 ET 日聚合; 价格闸与预交易闸**不可直接相加**"
              "(同一票可能先过价格闸再被预交易闸拦)。_"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="延长时段周复盘卡 (闸在哪)")
    ap.add_argument("--days", type=int, default=7, help="回溯 ET 天数 (默认 7)")
    ap.add_argument("--end", default=None, help="结束 ET 日 (默认最近已收盘)")
    ap.add_argument("--push", action="store_true", help="推送该卡")
    ap.add_argument("--json", action="store_true", help="附机器可读 JSON")
    args = ap.parse_args(argv)

    end = datetime.strptime(args.end or default_end(), "%Y-%m-%d").date()
    days = [(end - timedelta(days=i)).isoformat() for i in range(args.days - 1, -1, -1)]
    agg = collect(days)
    body = render(days, agg)
    print(body)
    if args.json:
        print(json.dumps({d: {s: {k: (dict(v) if isinstance(v, Counter) else v)
                                  for k, v in agg[d][s].items()} for s in EXT_SESSIONS} for d in days},
                         ensure_ascii=False, indent=1, default=str))
    if args.push:
        try:
            from app.push import push_text_safe
            from app.push.message import Level, Category
            ok = push_text_safe(body, level=Level.NORMAL, category=Category.REVIEW_DAILY,
                                title=f"🕒 延长时段周复盘 · {days[0]}~{days[-1]}")
            print(f"[push] {'已推送' if ok else '推送失败'}")
        except Exception as exc:                       # 通知永不阻断
            print(f"[push][warn] {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())