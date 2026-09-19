#!/usr/bin/env python3
"""周日综合复盘 · 本期交易汇总 — **模拟研究口径** (原 Hermes cron prompt 内嵌 SQL 的仓内化).

⚠ 口径边界 (2026-09-08, RFC-078 批C 标注): 本脚本报的是 **SIM 数据收集臂**, 不是实盘。
  依据: ①来源白名单 `TRADE_SOURCES = ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 全是模拟臂;
  ②载体 `DB_PATH` 固定 `data/ftseek_us.db` = sim 数据收集仓。
  **实盘(真钱)周报是另一个脚本**: `scripts/live_weekly_review.py` (美股段 + 港股段,
  载体 `data/ftseek_us_live.db`)。两份报告不得互相冒充 —— 此前本脚本标题只写
  "本周交易汇总", 推到飞书上无法分辨是模拟还是实盘, 而同期 `exit_scorecard.py:244`
  还写着 `AND trade_mode != 'live'` 显式排除实盘 ⇒ 全套周报里实盘根本不是一等公民。
  `DB_PATH` **刻意保持硬编码 sim 载体**而不改 `get_main_db()`: 本脚本的身份就是
  "模拟研究周报", 若随 TRADE_ENV 漂移, 将来自动管线切实盘时它会静默换载体并报
  "窗口内无已了结交易" —— 那是把"换了库"伪装成"没交易"。

为什么存在 (2026-08-28):
  Hermes job「FTSeek-US 周日综合复盘」Step 5 把 SQL 直接写在 prompt 里, 长期
  `SELECT ... SUM(pnl) ... WHERE date(entry_time)` — trade_records 根本没有 `pnl`
  列 (实为 realized_pnl / pnl_net), 实测每次运行必然 OperationalError: no such
  column: pnl, 该步骤自建立以来从未产出过一行数据。prompt 内嵌 SQL 不受版本控制、
  不被全量测试覆盖, 列名/口径漂移只能在生产运行时报错 — 这正是它必须是仓内脚本的原因。

口径 (与 scripts/daily_alert_summary.get_us_trade_summary、app/risk/symbol_day_loss 同源):
  · 归集日 = 出场 ET 日 substr(exit_time,1,10)。trade_date 是**入场**日, 隔夜仓当日
    出场会记到前一天, 按入场日归集会把跨日盈亏算进错误的窗口。
  · 盈亏   = 净额 COALESCE(pnl_net, realized_pnl) (净口径 2026-08-20 用户裁决)。
  · 只算已了结 exit_price > 0, 否则在仓行的 NULL/0 会混进胜率分母。
  · 白名单 PLAYBOOK_SIM / PLAYBOOK_LIVE = 现役交易链 (新增喂数来源须同步扩映射,
    同 app/risk/strategy_governance/account_circuit_breaker._restore_from_db 的约定);
    窗口内白名单外的已了结行单独列出, 不静默丢弃。

用法:
  python3 scripts/weekly_trade_summary.py [--days 7] [--end-date YYYY-MM-DD] [--db PATH]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from app.core.trade_identity import summarize_trade_identity

DB_PATH = os.path.join(PROJECT_DIR, "data", "ftseek_us.db")

# 现役交易链来源 (MAIN_PIPELINE/SCALPING/ORB_ENGINE 均为已冻结历史路径, 不再喂新行)
TRADE_SOURCES = ("PLAYBOOK_SIM", "PLAYBOOK_LIVE")

_AGG_SQL = """
SELECT COALESCE(direction, 'N/A')    AS direction,
       COALESCE(exit_reason, 'N/A')  AS exit_reason,
       COUNT(*)                                        AS n,
       SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)    AS wins,
       ROUND(SUM(net_pnl), 2)                          AS total_pnl
FROM (
    SELECT direction, exit_reason,
           COALESCE(pnl_net, realized_pnl) AS net_pnl
    FROM trade_records
    WHERE exit_price > 0
      AND source_type IN ({src})
      AND substr(exit_time, 1, 10) BETWEEN :start AND :end
)
GROUP BY 1, 2
ORDER BY total_pnl ASC
"""

_OUTSIDE_SQL = """
SELECT source_type,
       COUNT(*)                                    AS n,
       SUM(CASE WHEN net_pnl IS NULL THEN 1 ELSE 0 END) AS null_pnl,
       ROUND(SUM(net_pnl), 2)                      AS total_pnl
FROM (
    SELECT source_type, COALESCE(pnl_net, realized_pnl) AS net_pnl
    FROM trade_records
    WHERE exit_price > 0
      AND source_type NOT IN ({src})
      AND substr(exit_time, 1, 10) BETWEEN :start AND :end
)
GROUP BY 1
ORDER BY n DESC
"""

_IDENTITY_SQL = """
SELECT *
FROM trade_records
WHERE exit_price > 0
  AND substr(exit_time, 1, 10) BETWEEN :start AND :end
ORDER BY id
"""


_SRC_PH = ",".join(f":src_{i}" for i in range(len(TRADE_SOURCES)))
_SRC_PARAMS = {f"src_{i}": s for i, s in enumerate(TRADE_SOURCES)}


def _query(conn: sqlite3.Connection, sql: str, start: str, end: str) -> List[Dict]:
    conn.row_factory = sqlite3.Row
    params = dict(_SRC_PARAMS, start=start, end=end)
    return [dict(r) for r in conn.execute(sql.format(src=_SRC_PH), params)]


def summarize_window(conn: sqlite3.Connection, start: str, end: str) -> Dict:
    """一个出场日窗口 [start, end] 的汇总。headline 由交叉表求和得到 (二者必然自洽)."""
    rows = _query(conn, _AGG_SQL, start, end)
    outside = _query(conn, _OUTSIDE_SQL, start, end)
    conn.row_factory = sqlite3.Row
    identity_rows = [dict(row) for row in conn.execute(
        _IDENTITY_SQL, {"start": start, "end": end}
    )]
    total_n = sum(r["n"] for r in rows)
    total_wins = sum(r["wins"] for r in rows)
    # NULL 盈亏行 (如 POSITION_SYNC 同步行) 计笔数不计盈亏, 单列出来避免"胜率分母"被误读
    outside_n = sum(o["n"] for o in outside)
    return {
        "start": start,
        "end": end,
        "rows": rows,
        "identity": summarize_trade_identity(identity_rows),
        "outside": outside,
        "total_n": total_n,
        "total_wins": total_wins,
        "win_rate_pct": round(100.0 * total_wins / total_n, 1) if total_n else 0.0,
        "total_pnl": round(sum((r["total_pnl"] or 0.0) for r in rows), 2),
        "avg_pnl": round(sum((r["total_pnl"] or 0.0) for r in rows) / total_n, 2) if total_n else 0.0,
        "outside_n": outside_n,
    }


def windows(days: int, end_date: date):
    """本期 [start, end] 与上期 (紧邻的等长窗口)."""
    days = max(1, int(days))
    end = end_date.isoformat()
    start = (end_date - timedelta(days=days - 1)).isoformat()
    prev_end = (end_date - timedelta(days=days)).isoformat()
    prev_start = (end_date - timedelta(days=2 * days - 1)).isoformat()
    return (start, end), (prev_start, prev_end)


def default_end_date() -> date:
    """ET 今天 — 服务器是 CST, 直接用 date.today() 会在 CST 04:00-08:00 段多算一天."""
    try:
        from app.utils.timezone import now_et
        return now_et().date()
    except Exception as e:  # pytz/zoneinfo 缺失兜底, 不让报告脚本因时区库崩掉
        print(f"[weekly_trade_summary] now_et 不可用, 回落服务器本地日: {e}", file=sys.stderr)
        return date.today()


def render(cur: Dict, prev: Optional[Dict], days: int) -> str:
    label = "本周" if days == 7 else f"{days}天"
    lines = [
        f"=== {label}交易汇总 · **模拟研究口径 (非实盘)** (出场日 {cur['start']} ~ {cur['end']}, ET) ===",
        f"载体: {os.path.basename(DB_PATH)} (SIM 数据收集仓) | 来源白名单 "
        f"{'/'.join(TRADE_SOURCES)} | 币种 USD",
        # 刻意不在报告文本里硬编码库文件名: 载体名由 SSOT 解析器给 (dead_db 门也据此不告警)
        "实盘(真钱)周报另见 scripts/live_weekly_review.py (美股段 + 港股段, "
        "载体 = 看板实盘专属库, 由 app.core.db_utils.get_dashboard_db() 解析)",
    ]
    if not cur["total_n"]:
        # 措辞刻意保留"无已了结交易"原句 (tests/test_weekly_trade_summary.py 钉了它),
        # 口径标注追加在后 —— 改既有契约措辞不如加限定语。
        lines.append("窗口内无已了结交易 (0 笔, 模拟口径) — 若整周为空, 请核对 sim 交易链是否在跑; "
                     "本行**不代表实盘**, 实盘看 scripts/live_weekly_review.py")
    else:
        lines.append(
            f"总计: {cur['total_n']}笔 | 胜 {cur['total_wins']} ({cur['win_rate_pct']}%) | "
            f"净${cur['total_pnl']:,.2f} | 单笔均${cur['avg_pnl']:,.2f}"
        )
    if prev is not None:
        delta_pnl = round(cur["total_pnl"] - prev["total_pnl"], 2)
        delta_n = cur["total_n"] - prev["total_n"]
        lines.append(
            f"环比上期 ({prev['start']} ~ {prev['end']}): "
            f"{prev['total_n']}笔/净${prev['total_pnl']:,.2f} → Δ笔数{delta_n:+d} Δ盈亏${delta_pnl:+,.2f}"
        )
    if cur["rows"]:
        lines.append("")
        lines.append(f"{'方向':<7} {'退出原因':<26} {'笔数':>4} {'胜':>4} {'盈亏($)':>12}")
        for r in cur["rows"]:
            pnl = r["total_pnl"] or 0.0
            lines.append(
                f"{r['direction']:<7} {r['exit_reason']:<26} {r['n']:>4} {r['wins']:>4} {pnl:>12,.2f}"
            )
    if cur["identity"]:
        parts = [
            f"{r['trade_env']}/{r['source_type']}/{r['trade_phase']} {r['n']}笔"
            for r in cur["identity"]
        ]
        lines.append("")
        lines.append(f"标签: {' | '.join(parts)}")
    if cur["outside_n"]:
        parts = ", ".join(
            f"{o['source_type']} {o['n']}笔"
            + (f"(其中{o['null_pnl']}笔盈亏为空)" if o["null_pnl"] else "")
            for o in cur["outside"]
        )
        lines.append("")
        lines.append(f"口径外已了结行 (不计入上方汇总): {parts}")
    # 2026-09-18 (用户裁「周报加阶段×方向×出场原因 + 延长时段单列, ETH 单列」):
    #   与上面"方向×退出原因"表同窗口同来源白名单, 只多一个阶段维度。
    try:
        from app.review.stage_stats import rows_from_db, render_stage_section
        _rows = [r for r in rows_from_db(DB_PATH, cur["start"], cur["end"])
                 if str(r.get("source_type") or "") in TRADE_SOURCES]
        if _rows:
            lines.append("")
            lines.append(render_stage_section(_rows, f"分阶段成交统计 ({cur['start']} ~ {cur['end']})"))
    except Exception as _e:
        lines.append(f"(阶段维度渲染失败: {_e})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="周日复盘·本期交易汇总 (出场日记账 + 净额口径)")
    ap.add_argument("--days", type=int, default=7, help="窗口天数 (ET 出场日, 默认 7)")
    ap.add_argument("--end-date", default=None, help="窗口末日 YYYY-MM-DD (默认 ET 今天)")
    ap.add_argument("--db", default=DB_PATH, help=f"SQLite 路径 (默认 {DB_PATH})")
    ap.add_argument("--no-compare", action="store_true", help="不跑环比上期")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        return 1
    if args.end_date:
        try:
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--end-date 格式应为 YYYY-MM-DD, 收到: {args.end_date}")
            return 2
    else:
        end_date = default_end_date()

    (start, end), (prev_start, prev_end) = windows(args.days, end_date)
    conn = sqlite3.connect(args.db, timeout=10)
    try:
        cur = summarize_window(conn, start, end)
        prev = None if args.no_compare else summarize_window(conn, prev_start, prev_end)
    finally:
        conn.close()

    print(render(cur, prev, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
