#!/usr/bin/env python3
"""复盘历史回填锚定 (DEB-20260919 §13.5 / §20.2①, 2026-09-24 用户裁决落地).

为什么不做全量回填
    4–7 月 34 个单日窗口刚被时间口径修正重基 (DEB-20260919 §19.3),
    在口径还在动的时候回填 = 把"会变的历史"写进产物, 之后每次口径微调都要重写一遍,
    且无法区分"当时真发生的"和"今天口径算出来的" — 正是 §13.5 担心的**事后合理化**。

本脚本做什么 (保守替代, §20.2①)
    1. 生成 `bundle_<上线日>.json` 一个锚点 (上线日 = 2026-09-18, 首次产物日)
    2. 生成 `REVIEW_backfill_30d.md` 对照单 (以**今日口径**重算最近 30 个交易日,
       口径标签写死"以今日口径重算, 非当时口径", 不推送、不进 vault)
    3. 默认 dry-run: **零推送、零 vault 写入、零数据库修改**

等口径连续 30 天零变更后, 再议回填 (判据: `scripts/review_coverage_scan.py`
的 `bundle_schema.py` 修改时间距今 ≥30 天)。

用法
    # dry-run (默认): 只看输出, 不落盘
    python scripts/review_backfill_anchor.py

    # 落盘 (写 bundle_<上线日>.json + REVIEW_backfill_30d.md 到 data/reviews/)
    python scripts/review_backfill_anchor.py --write

    # 自定义上线日与窗口天数
    python scripts/review_backfill_anchor.py --launch-date 2026-09-18 --days 30 --write

回滚
    删除产物: rm data/reviews/bundle_<上线日>.json data/reviews/REVIEW_backfill_30d.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.review.bundle_builder import build
from app.review.bundle_render import render_md

DB_PATH = ROOT / "data" / "ftseek_us.db"
OUTPUT_DIR = ROOT / "data" / "reviews"

# 口径标签: 写死在产物里, 防"事后合理化" (DEB-20260919 §20.2① 风险缓解)
CALIBER_LABEL = "以今日口径重算, 非当时口径 (DEB-20260919 §20.2①)"


def _trading_days_ending(end: date, n: int) -> list[str]:
    """往回数 n 个工作日 (简化: 只跳过周末; 不含假日; 对照单用途足够)。"""
    days: list[str] = []
    cursor = end
    while len(days) < n:
        if cursor.weekday() < 5:  # Mon-Fri
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


def build_anchor(report_date: str, connection: sqlite3.Connection) -> dict | None:
    """构建上线日锚定 bundle; 返回 payload dict (或 None 如果那天无数据)。"""
    try:
        bundle = build(connection, report_date)
    except Exception as exc:
        print(f"[backfill] 锚定日 {report_date} 构建失败: {exc}", file=sys.stderr)
        return None
    payload = bundle.to_dict()
    payload["_backfill_note"] = CALIBER_LABEL
    return payload


def build_comparison_sheet(
    days: list[str],
    connection: sqlite3.Connection,
) -> str:
    """30 天对照单: 每行一个交易日的 headline 净额/笔数/胜率 (以今日口径重算)。"""
    rows = []
    for day in days:
        try:
            bundle = build(connection, day)
            payload = bundle.to_dict()
            h = payload["headline"]
            rows.append(
                f"| {day} | {h['trade_count']['value']} | "
                f"${h['net_pnl']['value']:+.2f} | "
                f"${h['gross_pnl']['value']:+.2f} | "
                f"{h['win_rate']['value'] * 100:.1f}% |"
            )
        except Exception as exc:
            rows.append(f"| {day} | — | — | — | — |  # 构建失败: {exc}")
    return (
        f"# 复盘 30 天对照单 (口径标签: {CALIBER_LABEL})\n\n"
        f"生成日: {date.today().isoformat()}\n"
        f"窗口: {days[0]} → {days[-1]} ({len(days)} 个交易日)\n\n"
        "| 日期 | 笔数 | 净额 | 毛额 | 胜率 |\n"
        "|---|---:|---:|---:|---:|\n"
        + "\n".join(rows)
        + "\n\n> **口径纪律**: 本表数字以生成当日的口径重算,"
        " 不代表当时实际的推送/报告读数。口径变更历史见 `scripts/review_coverage_scan.py` 的"
        " `bundle_schema.py` 修改时间。\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch-date", default="2026-09-18",
                        help="上线日 (锚定 bundle 日期, YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=30, help="对照单交易日数 (默认 30)")
    parser.add_argument("--write", action="store_true",
                        help="落盘 (默认 dry-run: 只打印路径, 不写文件)")
    args = parser.parse_args()

    launch = date.fromisoformat(args.launch_date)
    end = launch - timedelta(days=1)  # 对照单在上线日之前
    days = _trading_days_ending(end, args.days)

    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        anchor = build_anchor(args.launch_date, connection)
        sheet = build_comparison_sheet(days, connection)
    finally:
        connection.close()

    anchor_path = OUTPUT_DIR / f"bundle_{args.launch_date}.json"
    sheet_path = OUTPUT_DIR / "REVIEW_backfill_30d.md"

    if args.write:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if anchor is not None:
            anchor_path.write_text(
                json.dumps(anchor, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"[backfill] 锚定产物: {anchor_path}")
        sheet_path.write_text(sheet, encoding="utf-8")
        print(f"[backfill] 对照单: {sheet_path}")
    else:
        print(f"[backfill] dry-run: 将写 {anchor_path} (anchor) + {sheet_path} (sheet)")
        print(f"[backfill] 锚定日 {args.launch_date}: {'OK' if anchor else '无数据'}")
        print(f"[backfill] 对照单 {len(days)} 个交易日 ({days[0]} → {days[-1]})")
        print("[backfill] 要落盘请加 --write")
        # dry-run 把对照单内容打到 stdout 供检查
        print()
        print(sheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
