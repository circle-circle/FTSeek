#!/usr/bin/env python3
"""RFC-104 阶段 0 毕业裁定器 (9/24 增强批).

## 职责
读 data/loops/ext_entry_plan/reconcile_ledger.jsonl, 按 shadow_registry
``rfc104_eth_event_plan`` 的预注册判据逐条判, 输出终端报告 + JSON 工件.

## 预注册判据 (shadow_registry REGISTRY 原文)
- ① ≥ 7 个交易日对账记录 (min_n=7)
- ② 每日 coverage 100% (missed_count == 0)
- ③ 计划条数带 7-9 (graduation_criteria 原文: "计划条数 7-9, 非 44")

## 诚实纪律
- 判据按预注册字面判, 不为凑绿改判据
- 条数超带 (实测多源并集 16 行已超 7-9 带) → 如实 ❌ + 附注
  "多源并集使条数超起草时估算带, 判据带是否修订须用户裁决, 裁定器不自行放宽"
- 任何 ❌ → 打印差多少

## 产物
- 终端报告 (人可读)
- data/loops/ext_entry_plan/graduation_check_latest.json

## 调度
零 cron (人工日 7 跑), 零推送.

## CLI
``PYTHONPATH=. venv/bin/python scripts/eth_event_plan_graduation_check.py``
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLAN_DIR = ROOT / "data" / "loops" / "ext_entry_plan"
LEDGER_PATH = PLAN_DIR / "reconcile_ledger.jsonl"
OUTPUT_PATH = PLAN_DIR / "graduation_check_latest.json"

# ── 预注册判据 (shadow_registry rfc104_eth_event_plan 原文) ──
MIN_TRADING_DAYS = 7       # ① ≥7 个交易日对账记录
MIN_COVERAGE_PCT = 100.0   # ② 每日 coverage 100%
PLAN_ROWS_MIN = 7          # ③ 计划条数带下界
PLAN_ROWS_MAX = 9          # ③ 计划条数带上界


def _load_ledger() -> list[dict]:
    """读 reconcile_ledger.jsonl, 返回记录列表."""
    if not LEDGER_PATH.exists():
        return []
    records = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _distinct_trading_days(records: list[dict]) -> int:
    """不同交易日数 (et_date 去重)."""
    return len({r.get("et_date") for r in records if r.get("et_date")})


def _all_coverage_100(records: list[dict]) -> tuple[bool, list[dict]]:
    """每日 coverage 100% (missed_count == 0). 返回 (pass, fail_records)."""
    fails = [r for r in records if r.get("missed_count", 0) > 0]
    return len(fails) == 0, fails


def _plan_rows_in_band(records: list[dict]) -> tuple[bool, list[dict], str]:
    """计划条数带 7-9. 返回 (pass, out_of_band_records, note).

    诚实纪律: 实测多源并集超带时如实输出 ❌ + 附注, 不自行放宽.
    """
    out = [r for r in records
           if r.get("plan_rows", 0) < PLAN_ROWS_MIN
           or r.get("plan_rows", 0) > PLAN_ROWS_MAX]
    if not out:
        return True, [], ""

    # 附注: 分析超带方向
    over = [r for r in out if r.get("plan_rows", 0) > PLAN_ROWS_MAX]
    under = [r for r in out if r.get("plan_rows", 0) < PLAN_ROWS_MIN]
    note = ""
    if over and not under:
        avg_rows = sum(r.get("plan_rows", 0) for r in over) / len(over)
        note = (
            f"多源并集使条数超起草时估算带 (实测均值 {avg_rows:.1f} > 上界 {PLAN_ROWS_MAX}); "
            f"判据带是否修订须用户裁决, 裁定器不自行放宽"
        )
    elif under:
        note = f"部分交易日计划条数低于下界 {PLAN_ROWS_MIN}"

    return False, out, note


def run_check() -> dict:
    """运行毕业裁定. 返回裁定报告字典."""
    records = _load_ledger()
    now_iso = datetime.now().isoformat(timespec="seconds")

    if not records:
        return {
            "checked_at": now_iso,
            "verdict": "NOT_READY",
            "reason": "reconcile_ledger.jsonl 无记录 (尚未开始对账)",
            "criteria": {},
        }

    n_days = _distinct_trading_days(records)
    coverage_ok, coverage_fails = _all_coverage_100(records)
    band_ok, band_out, band_note = _plan_rows_in_band(records)

    criteria = {
        "trading_days": {
            "required": f">= {MIN_TRADING_DAYS}",
            "actual": n_days,
            "pass": n_days >= MIN_TRADING_DAYS,
            "gap": max(0, MIN_TRADING_DAYS - n_days),
        },
        "coverage_100pct": {
            "required": "每日 missed_count == 0",
            "actual": f"{len(records) - len(coverage_fails)}/{len(records)} 日 PASS",
            "pass": coverage_ok,
            "fail_dates": [r.get("et_date") for r in coverage_fails],
        },
        "plan_rows_band": {
            "required": f"{PLAN_ROWS_MIN}-{PLAN_ROWS_MAX}",
            "actual": sorted(set(r.get("plan_rows", 0) for r in records)),
            "pass": band_ok,
            "out_of_band_dates": [r.get("et_date") for r in band_out],
            "note": band_note,
        },
    }

    all_pass = all(c["pass"] for c in criteria.values())

    result = {
        "checked_at": now_iso,
        "ledger_records": len(records),
        "verdict": "GRADUATE" if all_pass else "NOT_READY",
        "criteria": criteria,
    }

    if all_pass:
        result["recommendation"] = "提请阶段 1 立项"
    else:
        gaps = []
        for name, c in criteria.items():
            if not c["pass"]:
                if name == "trading_days":
                    gaps.append(f"交易日数差 {c['gap']} 天 (需 {MIN_TRADING_DAYS}, 实 {c['actual']})")
                elif name == "coverage_100pct":
                    gaps.append(f"覆盖缺口: {len(c['fail_dates'])} 日有漏票 {c['fail_dates']}")
                elif name == "plan_rows_band":
                    gaps.append(f"条数超带: {c['note'] or c['out_of_band_dates']}")
        result["gaps"] = gaps

    return result


def _print_report(report: dict) -> None:
    """终端可读报告."""
    print("=" * 60)
    print("RFC-104 阶段 0 毕业裁定报告")
    print("=" * 60)
    print(f"裁定时刻: {report['checked_at']}")
    print(f"台账记录: {report.get('ledger_records', 0)} 条")
    print()

    if report["verdict"] == "NOT_READY" and "reason" in report:
        print(f"❌ 未就绪: {report['reason']}")
        return

    criteria = report.get("criteria", {})

    for name, c in criteria.items():
        icon = "✅" if c["pass"] else "❌"
        if name == "trading_days":
            print(f"  {icon} ① 交易日数: {c['actual']} (需 {c['required']})")
            if not c["pass"]:
                print(f"      差 {c['gap']} 天")
        elif name == "coverage_100pct":
            print(f"  {icon} ② 每日覆盖 100%: {c['actual']}")
            if not c["pass"]:
                print(f"      漏票日期: {c['fail_dates']}")
        elif name == "plan_rows_band":
            print(f"  {icon} ③ 计划条数带 {c['required']}: 实测 {c['actual']}")
            if not c["pass"]:
                print(f"      ⚠ {c['note']}")

    print()
    if report["verdict"] == "GRADUATE":
        print("🎓 裁定: 全部判据通过 → 提请阶段 1 立项")
    else:
        print("📋 裁定: 未满足毕业条件")
        for gap in report.get("gaps", []):
            print(f"  - {gap}")
    print("=" * 60)


def main(argv=None) -> int:
    report = run_check()

    # 写 JSON 工件
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # 终端报告
    _print_report(report)

    print(f"\n工件已写入: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
