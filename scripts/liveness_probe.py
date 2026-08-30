#!/usr/bin/env python3
"""
活性探针 (Liveness Probe) — "日志正常但逻辑失效"族的通用解
=================================================================
背景 (2026-08-29 周, 同族 4 例):
  1. G6 财报门: SQLite 死载体有表无数据 → 表级审计误判"门从未拦截", 活门被误下线
  2. direction_attribution: 44 份归档恒 "No data found" 而 job 状态恒 ok
  3. 财报日历主源被写成 fallback → 最需要冗余时冗余不在
  4. TD 降级路径 prev_close 键名错配 → 门读不到数据静默放行
共同点: 表级/文本断言测不出数据流是否接通。本探针对每条关键数据流/门核查
"最后一次真实产出的时间戳"或直接行为级复跑, 超龄/异常即 STALE。

探针形态:
  db_max      SQLite 查询最大日期/时间戳, 与市场历最后交易日比对
  json_ts     读 JSON 状态文件的时间戳字段 (ISO 或 epoch), 按小时计龄
  behavioral  运行只读脚本, 断言输出不含失败标记 (端到端行为级)

用法:
  python3 scripts/liveness_probe.py            # 打印报告, 有 STALE/ERROR 时 exit 1
  python3 scripts/liveness_probe.py --feishu   # 有异常时推飞书
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import now_et  # noqa: E402


@dataclass
class Probe:
    name: str
    kind: str                      # db_max | json_ts | behavioral
    description: str               # 对应的失明族案例
    db: str = ""                   # db_max: 相对 REPO 的库路径
    sql: str = ""                  # db_max: 返回单个 'YYYY-MM-DD' 或 ISO 时间戳
    lag_trading_days: int = 0      # db_max: 允许落后最后交易日的交易日数
    path: str = ""                 # json_ts: 相对 REPO 的 JSON 状态文件
    ts_field: str = ""             # json_ts: 时间戳字段名
    max_age_hours: float = 26.0    # json_ts: 超龄判 STALE
    cmd: list = field(default_factory=list)  # behavioral: 只读命令 (argv 列表, 无 shell)
    fail_marker: str = ""          # behavioral: 输出含此串 = STALE
    trading_day_only: bool = True  # 非交易日跳过 (美股周末/假日不喂数)
    status: str = ""
    detail: str = ""


PROBES = [
    Probe(
        name="trade_records_flow",
        kind="db_max",
        description="交易流水入账 (playbook 平仓落库)",
        db="data/ftseek_us.db",
        sql="SELECT MAX(trade_date) FROM trade_records",
        lag_trading_days=0,
    ),
    Probe(
        name="signal_snapshot_ingest",
        kind="db_max",
        description="信号快照采集 (signal_snapshots 当日快照)",
        db="data/signal_snapshots.db",
        sql="SELECT MAX(trade_date) FROM signal_snapshots",
        lag_trading_days=0,
    ),
    Probe(
        name="signal_backfill_t1",
        kind="db_max",
        description="T+1 收益回填 (05:30 CST 回填昨日; 归因报告的数粮)",
        db="data/signal_snapshots.db",
        sql="SELECT MAX(trade_date) FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL",
        lag_trading_days=1,   # 允许落后 1 个交易日 (当日 T1 次日才回填)
    ),
    Probe(
        name="earnings_guard_feed",
        kind="json_ts",
        description="G6 财报门内存活载体喂数 (2026-08-29 起死表误判案的探针)",
        path="data/liveness/earnings_guard_feed.json",
        ts_field="last_update",
        max_age_hours=26,
        trading_day_only=False,   # scheduler 每日喂数(含周末)
    ),
    Probe(
        name="futu_shadow_collect",
        kind="json_ts",
        description="富途影子通道采集 (资金流/大单异动/OFI/IV-HV)",
        path="data/.futu_channel_shadow_state.json",
        ts_field="last_collect_ts",
        max_age_hours=26,
        # 2026-08-31 误报修正 (8/31 06:00 CST 告警实锤): 采集器仅挂 _llm_strategy_loop
        #   的 TRADING/AFTER_HOURS 分支, 周末调度器 WEEKEND phase 完全休眠不请求 API
        #   (scheduler_core.py:2386) — 周末本就不采集。原注释"含周末"与调度器行为矛盾,
        #   周五盘后→周一开盘 ~65h 天然空窗必然撞 26h 阈值。最后采集=周五 16:00 ET
        #   盘后收尾且五通道数据完整 = 健康。交易日探针运行时采集 300s/次, 真断流仍会报。
        trading_day_only=True,
    ),
    Probe(
        name="direction_attribution_report",
        kind="behavioral",
        description="方向归因日报端到端复跑 (44 份恒空报告的失败模式: 返回 'No data found')",
        cmd=[str(REPO / "venv/bin/python"), str(REPO / "scripts/daily_direction_attribution.py"), "--days", "1"],
        fail_marker="No data found",
        trading_day_only=True,
    ),
]


def _recent_trading_days(n: int) -> list[str]:
    """最近 n 个美股交易日 (含今日若今日是交易日), 降序。"""
    from app.utils.holiday_calendar import get_holiday_calendar
    days = []
    d = now_et().date()
    while len(days) < n:
        if get_holiday_calendar().is_trading_day("US", d):
            days.append(d.isoformat())
        d -= timedelta(days=1)
    return days


def _is_us_trading_day_today() -> bool:
    from app.utils.holiday_calendar import get_holiday_calendar
    return get_holiday_calendar().is_trading_day("US", now_et().date())


def _run_db_max(p: Probe) -> tuple[str, str]:
    db_path = REPO / p.db
    if not db_path.exists():
        return "ERROR", f"库不存在: {p.db}"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        row = conn.execute(p.sql).fetchone()
        conn.close()
    except Exception as e:
        return "ERROR", f"查询失败: {e}"
    max_raw = row[0] if row else None
    if not max_raw:
        return "STALE", "查询返回空 (无任何行)"
    max_date = str(max_raw)[:10]
    days = _recent_trading_days(p.lag_trading_days + 1)
    frontier = days[-1]  # 允许落后的前沿: 最后交易日 - lag
    if max_date >= frontier:
        return "OK", f"最新={max_date} (前沿={frontier})"
    return "STALE", f"最新={max_date} 落后于前沿={frontier}"


def _run_json_ts(p: Probe) -> tuple[str, str]:
    fp = REPO / p.path
    if not fp.exists():
        return "STALE", f"状态文件不存在: {p.path} (从未产出或链路未接通)"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        raw = data.get(p.ts_field)
        if raw is None:
            return "ERROR", f"字段缺失: {p.ts_field}"
        ts = (datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
              if isinstance(raw, str) else float(raw))
        age_h = (datetime.now().timestamp() - ts) / 3600.0
    except Exception as e:
        return "ERROR", f"解析失败: {e}"
    if age_h <= p.max_age_hours:
        return "OK", f"{age_h:.1f}h 前"
    return "STALE", f"{age_h:.1f}h 前 (阈值 {p.max_age_hours}h)"


def _run_behavioral(p: Probe) -> tuple[str, str]:
    try:
        r = subprocess.run(p.cmd, capture_output=True, text=True,
                           timeout=120, cwd=str(REPO))
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "ERROR", f"执行失败: {e}"
    if p.fail_marker and p.fail_marker in out:
        return "STALE", f"输出含失败标记 '{p.fail_marker}' (仪器空转)"
    if r.returncode != 0:
        return "ERROR", f"exit={r.returncode}: {out[-150:]}"
    return "OK", "复跑产出正常"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feishu", action="store_true", help="有异常时推飞书")
    args = ap.parse_args()

    trading_today = _is_us_trading_day_today()
    lines = [f"=== 活性探针 {now_et().strftime('%Y-%m-%d %H:%M %Z')} "
             f"(今日美股交易日={trading_today}) ==="]
    worst = 0  # 0=OK 1=SKIP 2=STALE/ERROR
    bad: list[Probe] = []

    for p in PROBES:
        if p.trading_day_only and not trading_today:
            p.status, p.detail = "SKIP", "非交易日"
        elif p.kind == "db_max":
            p.status, p.detail = _run_db_max(p)
        elif p.kind == "json_ts":
            p.status, p.detail = _run_json_ts(p)
        elif p.kind == "behavioral":
            p.status, p.detail = _run_behavioral(p)
        else:
            p.status, p.detail = "ERROR", f"未知探针类型 {p.kind}"

        icon = {"OK": "✅", "SKIP": "⏭️", "STALE": "🔴", "ERROR": "🟠"}.get(p.status, "❓")
        lines.append(f"{icon} {p.name:30} {p.status:6} {p.detail}")
        lines.append(f"   └ 守护: {p.description}")
        if p.status in ("STALE", "ERROR"):
            worst = 2
            bad.append(p)
        elif p.status == "SKIP" and worst < 1:
            worst = 1

    report = "\n".join(lines)
    print(report)

    if bad and args.feishu:
        try:
            from app.notifier.feishu import send_text as _feishu_send
            _feishu_send(
                "🚨 活性探针异常\n" + "\n".join(f"[{p.status}] {p.name}: {p.detail}" for p in bad))
        except Exception as e:
            print(f"[liveness_probe] 飞书推送失败: {e}", file=sys.stderr)

    return 1 if worst == 2 else 0


if __name__ == "__main__":
    sys.exit(main())
