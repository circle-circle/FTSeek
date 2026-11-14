#!/usr/bin/env python3
"""RFC-203 早期风险 record-only 影子 — 只读汇总评估器。

输入: data/early_risk_shadow.jsonl (写方: app/playbook/early_risk_shadow.py,
以及 exit_monitor_flow / auto_execute_flow 的三个钩子)。

输出: 按日 / rule / symbol 的候选计数与影响面。只回答"观测是否活着、哪些
候选被识别", **不计算反事实 PnL**, 不假设被跳过或提前退出会带来何种收益。
3% / 2% / 5min 都是影子候选阈值, 不是生产规则; 任何转生产动作须另立 RFC + 用户裁。

机器可读状态 (刻意只有两态, 与需求一致):
  OK       至少 1 行有效数据 (可带 --date 过滤)。
  NO_DATA  文件缺失 / 该日无行 / 所有行都不可解析 / 文件不可读。

退出码: OK=0 / NO_DATA=1。

用法:
  ./venv/bin/python scripts/early_risk_shadow_eval.py
  ./venv/bin/python scripts/early_risk_shadow_eval.py --date 2026-11-13 --json
  ./venv/bin/python scripts/early_risk_shadow_eval.py --path /tmp/x.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "early_risk_shadow.jsonl"
SCHEMA = "early_risk_shadow_eval_v1"

STATE_OK = "OK"
STATE_NO_DATA = "NO_DATA"

KNOWN_RULES = ("grace_candidate", "anchor_candidate", "queue_candidate")

CAVEATS = (
    "本器只汇总候选计数/影响面; 不计算反事实 PnL, 不假设被跳过或提前退出会赚/亏。",
    "grace/anchor/queue 的 2%/3%/0s 阈值是本影子候选阈值, 不是生产规则。",
    "进程内去重键为 (date, inst_id/trade_id, rule); 重启后同一键可能再落一行, "
    "离线 read 时按同键去重。",
)


def _norm_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None


def _rule_of(row: Dict[str, Any]) -> str:
    return str(row.get("kind") or row.get("rule") or "unknown").strip() or "unknown"


def _symbol_of(row: Dict[str, Any]) -> str:
    return str(row.get("symbol") or "UNKNOWN").strip() or "UNKNOWN"


def _date_of(row: Dict[str, Any]) -> str:
    return _norm_date(row.get("date")) or "UNKNOWN_DATE"


def load_rows(path: Path) -> tuple[List[Dict[str, Any]], int]:
    """读 JSONL; 返回 (valid_rows, bad_line_count)。文件缺失/不可读 → ([], 0)。"""
    if not path.exists():
        return [], 0
    rows: List[Dict[str, Any]] = []
    bad = 0
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def summarize(
    rows: Iterable[Dict[str, Any]],
    *,
    bad_lines: int = 0,
    date: Optional[str] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """纯函数: 对候选行做按日/rule/symbol 汇总。"""
    date_filter = _norm_date(date) if date else None
    selected: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if date_filter and _date_of(row) != date_filter:
            continue
        selected.append(row)

    if not selected:
        return {
            "schema": SCHEMA,
            "state": STATE_NO_DATA,
            "path": str(path) if path is not None else None,
            "date_filter": date_filter,
            "n_rows": 0,
            "n_bad_lines": bad_lines,
            "n_days": 0,
            "n_symbols": 0,
            "by_rule": {},
            "by_symbol": {},
            "days": [],
            "caveats": list(CAVEATS),
        }

    by_rule: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "symbols": Counter()})
    by_symbol: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "rules": Counter()})
    by_day: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "by_rule": Counter(), "by_symbol": Counter()})

    for row in selected:
        rule = _rule_of(row)
        symbol = _symbol_of(row)
        day = _date_of(row)
        by_rule[rule]["n"] += 1
        by_rule[rule]["symbols"][symbol] += 1
        by_symbol[symbol]["n"] += 1
        by_symbol[symbol]["rules"][rule] += 1
        by_day[day]["n"] += 1
        by_day[day]["by_rule"][rule] += 1
        by_day[day]["by_symbol"][symbol] += 1

    days = []
    for day in sorted(by_day):
        data = by_day[day]
        days.append({
            "date": day,
            "n": data["n"],
            "by_rule": dict(sorted(data["by_rule"].items())),
            "by_symbol": dict(sorted(data["by_symbol"].items())),
            "affected_symbols": sorted(data["by_symbol"]),
        })

    return {
        "schema": SCHEMA,
        "state": STATE_OK,
        "path": str(path) if path is not None else None,
        "date_filter": date_filter,
        "n_rows": len(selected),
        "n_bad_lines": bad_lines,
        "n_days": len(days),
        "n_symbols": len(by_symbol),
        "by_rule": {
            rule: {
                "n": data["n"],
                "symbols": dict(sorted(data["symbols"].items())),
                "affected_symbols": sorted(data["symbols"]),
            }
            for rule, data in sorted(by_rule.items())
        },
        "by_symbol": {
            symbol: {
                "n": data["n"],
                "rules": dict(sorted(data["rules"].items())),
            }
            for symbol, data in sorted(by_symbol.items())
        },
        "days": days,
        "caveats": list(CAVEATS),
    }


def evaluate(path: Any = DEFAULT_PATH, date: Optional[str] = None) -> Dict[str, Any]:
    target = Path(path)
    try:
        rows, bad = load_rows(target)
    except Exception as error:
        result = summarize([], bad_lines=0, date=date, path=target)
        result["state"] = STATE_NO_DATA
        result["error"] = f"READ_ERROR: {error}"
        return result
    return summarize(rows, bad_lines=bad, date=date, path=target)


def _render_text(result: Dict[str, Any]) -> str:
    lines = [
        f"state={result['state']} rows={result['n_rows']} "
        f"bad_lines={result['n_bad_lines']} days={result['n_days']} "
        f"symbols={result['n_symbols']}",
    ]
    if result.get("date_filter"):
        lines.append(f"date_filter={result['date_filter']}")
    if result.get("error"):
        lines.append(f"error={result['error']}")
    if result["state"] == STATE_OK:
        lines.append("by_rule:")
        for rule, data in result["by_rule"].items():
            lines.append(
                f"  {rule}: n={data['n']} symbols={','.join(data['affected_symbols'])}")
        lines.append("by_day:")
        for day in result["days"]:
            rule_txt = ",".join(f"{k}:{v}" for k, v in day["by_rule"].items())
            lines.append(
                f"  {day['date']}: n={day['n']} [{rule_txt}] "
                f"symbols={','.join(day['affected_symbols'])}")
    lines.append("caveat: 非反事实 PnL — 仅候选计数/影响面, 阈值非生产规则。")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--date", default=None, help="只汇总某个 ET 日 (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    result = evaluate(args.path, args.date)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_text(result))
    return 0 if result["state"] == STATE_OK else 1


if __name__ == "__main__":
    sys.exit(main())