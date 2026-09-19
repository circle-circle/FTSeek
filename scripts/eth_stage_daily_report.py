#!/usr/bin/env python3
"""延长时段 (盘前/盘后) 阶段日报 — 单独一张, 有样本才推 (2026-09-18 用户裁「要, 做成独立阶段日报」)。

为什么单独一张: 延长时段链 (strategy_id=extended_hours_entry) 与盘中链**风险结构不同**
(无券商 STOP、止损靠规则引擎 initial_sl + 开盘 monitor 兜底, 出口只有锁盈/紧 SL),
  把它混进盘中统计会被稀释 —— 实测 8/1–9/18: 盘中 329 笔 +$340, 盘前 4 笔 −$649。

内容三段 (全部取真实落盘文件/库, 不估算):
  ① 漏斗: 每阶段 候选被价格闸拦 / 被盘前门槛拦 / 下单尝试 / 成交 / 失败原因
  ② 结果: 该 ET 日已平仓的 阶段×方向×出场原因 (复用 app/review/stage_stats 唯一口径)
  ③ 在场: 仍持有的延长时段仓 (open_positions, 含 initial_sl)

用法:
  python3 scripts/eth_stage_daily_report.py                  # 打印 (默认最近已收盘 ET 日)
  python3 scripts/eth_stage_daily_report.py --date 2026-09-18
  python3 scripts/eth_stage_daily_report.py --push           # 有样本才推 (无样本静默, 零噪音)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.trade_identity import phase_cn, phase_from_time  # noqa: E402
from app.review.stage_stats import render_stage_section, rows_from_db  # noqa: E402

DATA = ROOT / "data"
DB = DATA / "ftseek_us.db"
EXEC_LOG = DATA / "extended_hours_entry_executions.jsonl"
PRICE_GATE = DATA / "extended_hours_price_gate_rejects.jsonl"
PRETRADE_GATE = DATA / "extended_hours_pretrade_gate_rejects.jsonl"
EXT_SESSIONS = ("premarket", "afterhours")


def _et_date_of(ts: Any) -> str:
    """ISO 时间戳 (UTC/Z 或带偏移) → ET 日 (YYYY-MM-DD)。落盘文件多为 UTC, 必须换算。"""
    if not ts:
        return ""
    s = str(ts)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return s[:10]
    try:
        from app.utils.timezone import to_et
        return to_et(dt).strftime("%Y-%m-%d")
    except Exception:
        return dt.strftime("%Y-%m-%d")


def _now_et() -> datetime:
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def default_date() -> str:
    """最近一个**已结束**的 ET 交易日: 盘中已收盘(<20:00 ET → 昨天) 否则今天。"""
    now = _now_et()
    if now.hour * 60 + now.minute < 20 * 60:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def funnel(day: str) -> Dict[str, Dict[str, Any]]:
    """每阶段漏斗: 价格闸拦 / 盘前门槛拦 / 下单尝试 / 成交 / 失败原因。"""
    f: Dict[str, Dict[str, Any]] = {s: {"price_gate": 0, "pretrade_gate": 0, "attempts": 0,
                                        "filled": 0, "errors": [], "pretrade_reasons": {}}
                                    for s in EXT_SESSIONS}
    for row in _read_jsonl(PRICE_GATE):
        s = str(row.get("session") or "")
        if s in f and _et_date_of(row.get("ts")) == day:
            f[s]["price_gate"] += int(row.get("n") or 0)
    for row in _read_jsonl(PRETRADE_GATE):
        s = str(row.get("session") or "")
        if s in f and _et_date_of(row.get("ts")) == day:
            f[s]["pretrade_gate"] += int(row.get("n") or 0)
            for rj in row.get("rejects") or []:
                k = str(rj.get("reason") or "?")
                f[s]["pretrade_reasons"][k] = f[s]["pretrade_reasons"].get(k, 0) + 1
    for row in _read_jsonl(EXEC_LOG):
        s = str(row.get("session") or "")
        if s not in f or _et_date_of(row.get("ts")) != day:
            continue
        f[s]["attempts"] += 1
        if row.get("ok") and float(row.get("filled_qty") or 0) > 0:
            f[s]["filled"] += 1
        elif row.get("error"):
            f[s]["errors"].append(str(row.get("error"))[:60])
        else:
            f[s]["errors"].append(str(row.get("fill", {}).get("status") or "not_filled")[:60])
    return f


def holding_ext() -> List[Dict[str, Any]]:
    """仍持有的延长时段仓 (含 initial_sl) —— 过夜风险要看得见。"""
    if not DB.exists():
        return []
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select symbol, direction, size, avg_entry_price, initial_sl, entry_time, strategy_id "
            "from open_positions where strategy_id like 'extended_hours%' or position_id like 'EXT-%'"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def render(day: str, push_date_note: str = "") -> str:
    fun = funnel(day)
    trades = rows_from_db(str(DB), day, day)
    ext_trades = [t for t in trades if (t.get("stage") or "") in EXT_SESSIONS]
    lines = [f"### 延长时段阶段日报 · {day} (ET){push_date_note}", ""]
    lines += ["**① 漏斗 (每阶段)**", "",
              "| 阶段 | 价格闸拦 | 预交易闸拦 | 下单尝试 | 成交 |", "| --- | --- | --- | --- | --- |"]
    for s in EXT_SESSIONS:
        d = fun[s]
        lines.append(f"| {phase_cn(s)} | {d['price_gate']} | {d['pretrade_gate']} | "
                     f"{d['attempts']} | {d['filled']} |")
    reasons: List[str] = []
    for s in EXT_SESSIONS:
        d = fun[s]
        for e in d["errors"][:3]:
            reasons.append(f"{phase_cn(s)}: {e}")
        for k, v in sorted(d["pretrade_reasons"].items(), key=lambda kv: -kv[1])[:3]:
            reasons.append(f"{phase_cn(s)}·预交易闸: {k} ×{v}")
    if reasons:
        lines += ["", "未成交/拦截原因 (前几条): " + "；".join(reasons)]
    lines += ["", "**② 结果 (该 ET 日已平仓)**"]
    if ext_trades:
        lines.append(render_stage_section(ext_trades, f"延长时段成交 · {day}"))
    else:
        lines.append("（该日无已平仓的延长时段单）")
    holds = holding_ext()
    lines += ["", "**③ 在场 (仍持有的延长时段仓)**"]
    if holds:
        for h in holds:
            lines.append(f"- {h['symbol']} {h['size']}股 @ ${h['avg_entry_price']:.2f} · "
                         f"initial_sl ${(h['initial_sl'] or 0):.2f} · 入场阶段 "
                         f"{phase_cn(phase_from_time(h['entry_time']))} ｜ ⚠️ 无券商 STOP, "
                         f"靠规则引擎 + 开盘兜底")
    else:
        lines.append("- 无")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="延长时段 (盘前/盘后) 阶段日报")
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认最近已收盘 ET 日)")
    ap.add_argument("--push", action="store_true", help="推送 (有样本才推, 无样本静默)")
    ap.add_argument("--json", action="store_true", help="附机器可读 JSON")
    args = ap.parse_args(argv)

    day = args.date or default_date()
    fun = funnel(day)
    trades = rows_from_db(str(DB), day, day)
    ext_trades = [t for t in trades if (t.get("stage") or "") in EXT_SESSIONS]
    has_sample = any(fun[s]["attempts"] or fun[s]["price_gate"] for s in EXT_SESSIONS) \
        or bool(ext_trades) or bool(holding_ext())
    body = render(day)
    print(body)

    if args.json:
        print(json.dumps({"date": day, "funnel": fun,
                          "trades": [{k: t[k] for k in ("symbol", "stage", "dir_cn", "exit_reason",
                                                        "net", "mfe_pct")} for t in ext_trades],
                          "holding": holding_ext()}, ensure_ascii=False, indent=2))

    if args.push:
        if not has_sample:
            print(f"[skip-push] {day} 无延长时段样本 (零噪音策略), 不推送")
            return 0
        try:
            from app.push import push_text_safe
            from app.push.message import Level, Category
            ok = push_text_safe(body, level=Level.NORMAL, category=Category.REVIEW_DAILY,
                                title=f"🕒 延长时段阶段日报 · {day} (ET)")
            print(f"[push] {'已推送' if ok else '推送失败'} · 双发/主通道由 PushHub 路由")
        except Exception as e:  # 通知永不阻断 (cron 里也只是日志)
            print(f"[push][warn] {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())