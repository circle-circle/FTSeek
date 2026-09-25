#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLM A' 日内累计亏损熔断影子 (record-only, 零交易行为变更)
================================================================
出处: DEB-20260904-experience-generalization-system §11.5 一期①
      (三模型互辩收敛, 阈值 -400 预注册冻结 — 修改阈值 = 推翻预注册, 须重新裁决)

语义 (冻结, 与实盘闸对齐的事件流口径):
  按 ET 日分组, 日内事件按时间排序 (exit 事件贡献已实现 pnl_net 累计;
  同日同时刻 tie 时 exit 先于 entry 结算)。当日累计已实现 pnl_net ≤ -400
  触发后, 该日余下全部新开仓记为"被挡" — 影子不拦任何单, 只记录:
    · 被挡信号清单 (symbol/direction/entry_time/触发时累计值)
    · 假想轨迹 = 该笔在模拟盘的真实结局 (exit_time/pnl_net; 未平仓记 settled=0)
  被挡笔期望 = settled 被挡笔 pnl_net 均值; 主判据: 期望 ≤ -10 且 n≥30 → 毕业呈裁。

口径冻结 (kimi 复算基线, 2026-09-04):
  trade_mode='simulate' AND pnl_net IS NOT NULL; ET 日分桶 (RFC-070 公共尺子)。
  与 DEB 预验证 (-400→挡28笔 -685.04) 的差异声明: 预验证用 entry 序简化累加,
  本影子用 exit 时刻事件流 (更贴近实盘闸语义), 首日回补数字可能略异 — 属口径
  升级而非数据错误, 基线以本影子首跑为准。
  近似声明 (承 daily_loss_halt_calibration): 实盘闸含浮亏触发更早 → 本影子
  偏保守 (低估节损); 被挡笔结局视为独立 (剔除不影响其余成交)。

持久化 (既有库 ftseek_us.db, 派生表全量重建幂等):
  cb_shadow_daily   — 每触发日一行
  cb_shadow_blocked — 每被挡笔一行
  data/shadow/cb_shadow_latest.json — 汇总 (shadow_registry 计数 + 04:00 报告段消费)

PYTEST_CURRENT_TEST 守卫: 测试环境下若目标库解析为生产库, 自动复制到临时文件,
绝不写生产库。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(BASE / "scripts"))
from utils.et_time import naive_cst_to_et, parse_et_date  # noqa: E402  [RFC-070] 公共时间尺子

PROD_DB = BASE / "data" / "ftseek_us.db"
OUT_JSON = BASE / "data" / "shadow" / "cb_shadow_latest.json"

# ── 预注册冻结常量 (DEB-20260904 §11.5; 修改=重新裁决) ──
THRESHOLD = -400.0
GRAD_MIN_N = 30          # n≥30 顺延毕业门槛 [判据 A1]
GRAD_MAX_EXPECT = -10.0  # 主判据: 被挡笔期望 ≤ -10
CALIBER = "trade_mode=simulate AND pnl_net IS NOT NULL; ET日; exit事件流"


def _parse_ts(ts: str):
    """返回 (epoch_utc, et_date_str)。RFC-070 公共尺子:
    epoch: aware 戳直接取真值; 裸戳视为 CST 经 naive_cst_to_et (DST 正确)。
    et_date: parse_et_date 三格式混杂 → ET 交易日 (带偏移按真值换算到 ET 取日)。

    2026-09-05 自审查修复: 初版复制 daily_loss_halt_calibration.epoch 的
    "d+delta 后取 strftime 日期"写法 — 该写法得到的是 UTC 墙钟日, 盘后/盘前
    带偏移戳 (如 20:15-04:00) 会被错桶到次日; 现 epoch 与日期均走公共尺子。
    常规时段 (9:30-16:00 ET) 两实现逐行等值, 仅隔夜/盘后市场出场桶位修正。
    """
    ts = str(ts)
    day = parse_et_date(ts)
    parsed = dt.datetime.fromisoformat(ts)
    if parsed.tzinfo is not None:
        epoch = parsed.timestamp()
    else:
        epoch = naive_cst_to_et(parsed, keep_aware=True).timestamp()
    return epoch, day


def load_trades(conn: sqlite3.Connection):
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_time, exit_time, pnl_net
           FROM trade_records
           WHERE trade_mode='simulate' AND entry_time IS NOT NULL"""
    ).fetchall()
    trades = []
    skipped = 0
    for tid, sym, dirn, e_ts, x_ts, pnl in rows:
        try:
            e_ep, e_day = _parse_ts(e_ts)
        except Exception:
            skipped += 1
            continue
        x_ep = x_day = None
        if x_ts and pnl is not None:
            try:
                x_ep, x_day = _parse_ts(x_ts)
            except Exception:
                x_ep = x_day = None
        trades.append(dict(id=tid, symbol=sym, direction=dirn,
                           entry_time=e_ts, exit_time=x_ts, pnl_net=pnl,
                           e_ep=e_ep, e_day=e_day, x_ep=x_ep, x_day=x_day))
    return trades, skipped


def replay(trades):
    """事件流回放。返回 (daily_rows, blocked_rows)。"""
    days: dict[str, list] = {}
    for t in trades:
        # entry 事件归入 entry 日; exit 事件归入 exit 日 (隔夜仓出场计出场日, 实盘口径)
        days.setdefault(t["e_day"], []).append((t["e_ep"], "entry", t))
        if t["x_ep"] is not None:
            days.setdefault(t["x_day"], []).append((t["x_ep"], "exit", t))

    daily_rows, blocked_rows = [], []
    for day in sorted(days):
        evs = sorted(days[day], key=lambda e: (e[0], 0 if e[1] == "exit" else 1))
        cum = 0.0
        triggered = False
        trigger_time = None
        cum_at_trigger = None
        blocked = []
        day_realized = 0.0
        for ep, kind, t in evs:
            if kind == "exit":
                cum += t["pnl_net"]
                day_realized += t["pnl_net"]
                if not triggered and cum <= THRESHOLD:
                    triggered = True
                    trigger_time = t["exit_time"]
                    cum_at_trigger = cum
            else:  # entry
                if triggered:
                    blocked.append((t, cum))
        if triggered:
            daily_rows.append(dict(
                trade_date=day, trigger_time=trigger_time,
                cum_at_trigger=round(cum_at_trigger, 2),
                n_blocked=len(blocked),
                n_blocked_settled=sum(1 for t, _ in blocked if t["pnl_net"] is not None),
                blocked_pnl_net=round(sum(t["pnl_net"] for t, _ in blocked
                                          if t["pnl_net"] is not None), 2),
                day_pnl_net_actual=round(day_realized, 2)))
            for t, cum_at_entry in blocked:
                blocked_rows.append(dict(
                    trade_date=day, trade_record_id=t["id"], symbol=t["symbol"],
                    direction=t["direction"], entry_time=t["entry_time"],
                    exit_time=t["exit_time"], pnl_net=t["pnl_net"],
                    cum_at_entry=round(cum_at_entry, 2),
                    settled=1 if t["pnl_net"] is not None else 0))
    return daily_rows, blocked_rows


def persist(conn: sqlite3.Connection, daily_rows, blocked_rows):
    """派生表全量重建 (幂等); 单事务。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS cb_shadow_daily (
        trade_date TEXT PRIMARY KEY,
        trigger_time TEXT, cum_at_trigger REAL,
        n_blocked INTEGER, n_blocked_settled INTEGER,
        blocked_pnl_net REAL, day_pnl_net_actual REAL,
        threshold REAL, computed_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS cb_shadow_blocked (
        trade_date TEXT, trade_record_id INTEGER,
        symbol TEXT, direction TEXT, entry_time TEXT, exit_time TEXT,
        pnl_net REAL, cum_at_entry REAL, settled INTEGER,
        threshold REAL, PRIMARY KEY (trade_date, trade_record_id))""")
    with conn:
        conn.execute("DELETE FROM cb_shadow_daily")
        conn.execute("DELETE FROM cb_shadow_blocked")
        for r in daily_rows:
            conn.execute(
                """INSERT INTO cb_shadow_daily
                   (trade_date, trigger_time, cum_at_trigger, n_blocked,
                    n_blocked_settled, blocked_pnl_net, day_pnl_net_actual, threshold)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["trade_date"], r["trigger_time"], r["cum_at_trigger"],
                 r["n_blocked"], r["n_blocked_settled"], r["blocked_pnl_net"],
                 r["day_pnl_net_actual"], THRESHOLD))
        for r in blocked_rows:
            conn.execute(
                """INSERT INTO cb_shadow_blocked
                   (trade_date, trade_record_id, symbol, direction, entry_time,
                    exit_time, pnl_net, cum_at_entry, settled, threshold)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (r["trade_date"], r["trade_record_id"], r["symbol"], r["direction"],
                 r["entry_time"], r["exit_time"], r["pnl_net"], r["cum_at_entry"],
                 r["settled"], THRESHOLD))


def summarize(daily_rows, blocked_rows):
    settled = [r for r in blocked_rows if r["settled"] == 1]
    n = len(settled)
    total = round(sum(r["pnl_net"] for r in settled), 2)
    expect = round(total / n, 2) if n else None
    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    last30 = [r for r in settled if r["trade_date"] >= cutoff]
    l30_total = round(sum(r["pnl_net"] for r in last30), 2)
    graduated_ready = (n >= GRAD_MIN_N and expect is not None
                       and expect <= GRAD_MAX_EXPECT)
    return {
        "threshold": THRESHOLD, "caliber": CALIBER,
        "n": n,                      # registry 计数键: 全期 settled 被挡笔数
        "n_target": GRAD_MIN_N,
        "blocked_pnl_net_total": total,
        "blocked_expectancy": expect,      # 主判据: ≤ -10
        "expect_target": GRAD_MAX_EXPECT,
        "n_trigger_days": len(daily_rows),
        "last30d": {"n": len(last30), "blocked_pnl_net": l30_total},
        "graduation_ready": graduated_ready,
        "recent_trigger_days": daily_rows[-10:],
        "last_run": dt.datetime.now().isoformat(timespec="seconds"),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="GLM A' 熔断影子 (record-only)")
    ap.add_argument("--db", default=None, help="默认生产库; 测试请显式指定")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else PROD_DB
    if os.environ.get("PYTEST_CURRENT_TEST") and not args.db:
        # PYTEST_CURRENT_TEST 守卫: 测试下绝不写生产库, 复制到临时文件
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy(PROD_DB, tmp.name)
        db = Path(tmp.name)
        out = Path(tempfile.mktemp(suffix=".json"))
    else:
        out = Path(args.out)

    conn = sqlite3.connect(db)
    trades, skipped = load_trades(conn)
    daily_rows, blocked_rows = replay(trades)
    persist(conn, daily_rows, blocked_rows)
    conn.close()

    summary = summarize(daily_rows, blocked_rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    # ── stdout 报告段 (04:00 日报可原样引用) ──
    # 2026-09-25 判读防呆 (REVIEW-20260925 P2 实锤: 报告层把 期望+43.98 vs 判据≤-10
    #   读成"已达标", 方向反了 ⇒ 若照它裁决会启用一个平均挡掉 +$44/笔盈利单的熔断)。
    #   纪律: 判定行由本脚本算好印出, 下游报告只许照抄"判据判定"行, 禁止自行比较数字下结论。
    #   "假想节损"标签同步更名"被挡笔净PnL" — 正值语义是"启用熔断会放弃这些盈利", 不是节省。
    _exp = summary["blocked_expectancy"]
    if summary["graduation_ready"]:
        _verdict = f"MET (期望 {_exp} ≤ {GRAD_MAX_EXPECT} 且 n≥{GRAD_MIN_N}) → READY 呈裁"
    elif _exp is not None and _exp > GRAD_MAX_EXPECT:
        _verdict = (f"NOT_MET·方向相反 (期望 {_exp} > 判据≤{GRAD_MAX_EXPECT}: "
                    f"被挡笔在赚钱 ⇒ 启用熔断=放弃盈利, 不得呈裁)")
    else:
        _verdict = f"NOT_MET (n={summary['n']} < {GRAD_MIN_N}) 攒样中"
    print(f"[GLM-A' 熔断影子] 阈值 {THRESHOLD} (预注册冻结) | 口径: {CALIBER}")
    print(f"触发日 {summary['n_trigger_days']} 个; 被挡 settled n={summary['n']}"
          f"/{GRAD_MIN_N}; 被挡笔期望={_exp}"
          f" (判据≤{GRAD_MAX_EXPECT}); 被挡笔净PnL合计={summary['blocked_pnl_net_total']}"
          f" (正=熔断会挡掉盈利)")
    print(f"判据判定: {_verdict}")
    print(f"近30天: n={summary['last30d']['n']}, 被挡笔净PnL={summary['last30d']['blocked_pnl_net']}")
    if skipped:
        print(f"(跳过 entry_time 无法解析 {skipped} 笔)")
    for d in summary["recent_trigger_days"]:
        print(f"  {d['trade_date']}: 触发@{d['trigger_time']} 累计{d['cum_at_trigger']}"
              f" → 挡{d['n_blocked']}笔 被挡笔净{d['blocked_pnl_net']}"
              f" (当日实际净{d['day_pnl_net_actual']})")
    print(f"毕业状态: {'READY — 呈裁' if summary['graduation_ready'] else 'OBSERVING 攒样中'}"
          f" | 明细表 ftseek_us.db: cb_shadow_daily/cb_shadow_blocked | {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
