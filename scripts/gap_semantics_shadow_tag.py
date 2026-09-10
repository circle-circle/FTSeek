#!/usr/bin/env python3.11
"""缺口语义影子打标 (2026-08-17, record-only, 零执行路径改动).

背景 (DEB-20260817-symbol-profile-market-sense):
  A3 票型驱动冻结 (gap_filler 标签基率 74.5% 零区分度), 但 gap-bucket 级
  语义 (市场级, 不依赖票型标签) 有实证: 小缺口回补率高→等回踩, 大缺口低→
  追延续。本影子对每条带 entry_zone 的活跃指令打"等 vs 追"语义判定 +
  A1 开盘重锚反事实, 攒 OOS 样本裁决 gap 语义门 (n≥60)。

数据基座: plan_vs_actual_report 的干净 rows (日K已于 8/17 增量刷新修复,
  7/24 后伪缺口已清除)。本脚本 = 判定薄层, 不重算测量。

判定 (干净版基线: small fill=0.873 / mid=0.596 / big=0.333):
  wait_correct  : 带跨昨收(等回踩) 且 缺口当日回补 → 等对了
  wait_wrong    : 带跨昨收 且 缺口未回补 → 白等 (A1/A3 靶子)
  chase_correct   : 带不跨昨收(追延续) 且 缺口未回补 → 追对了
  chase_wrong     : 带不跨昨收 且 缺口回补 → 追在顶上
  A1 反事实: 开盘5min中枢重锚 band (扁平宽度不变), 30min 内反事实带触达
    vs 实际带触达对照。

回滚: 配置键 gap_semantics_shadow_enabled=false + 重启。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "gap_semantics_shadow.jsonl"
DB = REPO / "data" / "ftseek_us.db"
RB_DIR = REPO / "data" / "replay_bars"         # 分钟bar: US_{SYM}_{DATE}.json
DAILY_DIR = REPO / "data" / "replay_bars_daily"  # 日K: US_{SYM}.json (prev_close)
OPEN_CENTER_MIN = 5  # 开盘后5min中枢 (09:30-09:35, A1 重锚窗口)

# 2026-09-10 (可观测性批): print → logger。本脚本经 scheduler._load_script_module 在服务
#   进程内执行, print 无时间戳/级别/来源。命名沿用 in-process 脚本约定 ftseek.<name>;
#   消息正文保持逐字不变, 以免打断既有 grep 口径。
logger = logging.getLogger("ftseek.gap_semantics_shadow")


def load_bars(sym: str, date: str):
    """分钟bar [ts,o,h,l,c] 升序; 无缓存返回 None。

    自包含内联 (与 plan_vs_actual_report 同口径): 不依赖他agent未提交文件。
    """
    fp = RB_DIR / f"{sym.replace('.', '_')}_{date}.json"
    if not fp.exists():
        return None
    try:
        rows = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    out = []
    for r in rows:
        try:
            out.append((datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S"), r[1], r[2], r[3], r[4]))
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


def prev_close(sym: str, date: str):
    """前一交易日收盘 (日K缓存, 自包含)。"""
    fp = DAILY_DIR / f"{sym.replace('.', '_')}.json"
    if not fp.exists():
        return None
    try:
        rows = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    px = None
    for r in rows:
        if r[0] < date and r[4]:
            px = r[4]
        elif r[0] >= date:
            break
    return px


def _seen_keys() -> set:
    keys = set()
    if OUT.exists():
        for ln in OUT.read_text().splitlines():
            try:
                d = json.loads(ln)
                keys.add((d["date"], d["id"]))
            except Exception:
                continue
    return keys


def _bucket(gap_abs: float) -> str:
    if gap_abs < 0.05:
        return "flat"
    if gap_abs < 0.5:
        return "small"
    if gap_abs < 1.5:
        return "mid"
    return "big"


def run_check(scheduler=None) -> None:
    """调度器入口 (AFTER_HOURS)。"""
    main()


def main() -> int:
    try:
        from app.core.config_container import ConfigContainer
        if not bool(ConfigContainer.get_instance().get_dotted(
                "playbook.gap_semantics_shadow_enabled", True)):
            return 0
    except Exception:
        pass

    # 自包含 bar/prev_close 读取 (内联, 不依赖他agent未提交文件)

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    insts = [dict(r) for r in c.execute(
        "SELECT id, date, symbol, direction, entry_price, entry_low, entry_high, "
        "stop_loss, take_profit, skip_reason, triggered, executed, decision_id "
        "FROM playbook_instructions WHERE date >= '2026-06-14' AND entry_price > 0 "
        "ORDER BY date").fetchall()]
    trades = {r["decision_id"]: dict(r) for r in c.execute(
        "SELECT decision_id, COALESCE(pnl_net, realized_pnl) pnl "
        "FROM trade_records WHERE decision_id != ''")}
    c.close()

    seen = _seen_keys()
    new_rows = []
    for inst in insts:
        if inst["skip_reason"] or inst["entry_low"] is None:
            continue
        key = (inst["date"], inst["id"])
        if key in seen:
            continue
        bars = load_bars(inst["symbol"], inst["date"])
        pc = prev_close(inst["symbol"], inst["date"])
        if not bars or not pc:
            continue
        day_open = bars[0][1]
        gap_pct = (day_open - pc) / pc * 100
        gap_abs = abs(gap_pct)
        bucket = _bucket(gap_abs)
        # 方向语义: BUY 关心向上跳空 (gap>0 远离锚), SELL_SHORT 关心向下
        adverse = gap_pct > 0 if inst["direction"] == "BUY" else gap_pct < 0
        gap_filled = (min(b[3] for b in bars) <= pc <= max(b[2] for b in bars)) \
            if adverse else True
        lo, hi = inst["entry_low"], inst["entry_high"]
        straddles = lo <= pc <= hi

        # 等 vs 追 判定 (仅对不利跳空有意义; 顺向跳空带天然不跨昨收)
        if adverse:
            if straddles:
                verdict = "wait_correct" if gap_filled else "wait_wrong"
            else:
                verdict = "chase_wrong" if gap_filled else "chase_correct"
        else:
            verdict = "gap_favorable"

        # A1 反事实: 开盘5min中枢重锚 (band 宽度不变, 整体平移)
        oc_bars = bars[:OPEN_CENTER_MIN]
        oc = sum(b[4] for b in oc_bars) / len(oc_bars) if oc_bars else day_open
        width_lo = inst["entry_price"] - lo
        width_hi = hi - inst["entry_price"]
        ra_lo, ra_hi = oc - width_lo, oc + width_hi
        first30 = bars[:30]
        ra_touch = any(b[3] <= ra_hi and b[2] >= ra_lo for b in first30)
        orig_touch = any(b[3] <= hi and b[2] >= lo for b in first30)

        tr = trades.get(inst["decision_id"] or "")
        new_rows.append({
            "date": inst["date"], "id": inst["id"], "symbol": inst["symbol"],
            "direction": inst["direction"], "gap_pct": round(gap_pct, 3),
            "gap_bucket": bucket, "adverse": adverse, "gap_filled": gap_filled,
            "straddles_prev_close": straddles, "verdict": verdict,
            "open_center": round(oc, 4),
            "reanchor_band": [round(ra_lo, 4), round(ra_hi, 4)],
            "orig_touch_30m": orig_touch, "reanchor_touch_30m": ra_touch,
            "triggered": inst["triggered"], "executed": inst["executed"],
            "exec_pnl": tr.get("pnl") if tr else None,
        })

    if new_rows:
        with OUT.open("a") as f:
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total = len(seen) + len(new_rows)
    logger.info("[gap_semantics_shadow] 累计 %d 行 (+%d)", total, len(new_rows))
    return 0


if __name__ == "__main__":
    # 独立 CLI 运行需自配 handler (同 ah_exit_shadow.py:452), 否则 print→logger 后输出静默变空。
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    sys.exit(main())
