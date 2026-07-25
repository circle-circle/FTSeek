#!/usr/bin/env python3
"""C0 三门首夜验证报告 (2026-07-24, DEB-20260723 第 8 轮判据自动化).

用法: ./venv/bin/python scripts/first_night_gate_report.py [YYYY-MM-DD]
      日期缺省 = 最近一个有 PLAYBOOK 成交的交易日。

判据 (DEB-20260723-crash-day §第8轮, 预注册):
  1. MFE_STALL_EXIT 出现且触发单均为水下低MFE; TIME_EXIT 中不再有 MFE<0.5% 且持有>3h 的单
     (2026-07-25 尺子修正: "水下"判定优先读 metadata.stall_exit.cur_at_trigger — 触发现场;
      无埋点的存量单回退落库 PnL 容差 ±半tick×qty, 消零附近边界假阳性 — 首夜 MRK
      cur=-0.03% 触发、成交后 +$0.12 被误报 ❌ 的教训)
  2. trigger_miss_telemetry 出现 streak_ban / low_atr_block (若当日有命中标的)
  3. 当日指令 market_regime/vix_at_generation 非 NULL
  4. 当日 TIME_EXIT 桶亏损 < $100 (vs 07-23 的 -$349); MFE≥0.5% 赢家仍由
     BREAKEVEN/TRAIL_LOCK/EOD/PRE_CLOSE 接住 (零质量损失核验)
附加: 出场滑点首夜数据 (C1 补测, 2026-07-24 埋点)。
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
TELEMETRY = ROOT / "data" / "trigger_miss_telemetry.jsonl"

OK, BAD, WARN = "✅", "❌", "⚠️"


def main() -> int:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    if len(sys.argv) > 1:
        day = sys.argv[1]
    else:
        r = conn.execute(
            "SELECT MAX(trade_date) d FROM trade_records "
            "WHERE source_type LIKE 'PLAYBOOK%' AND exit_time IS NOT NULL AND exit_time!=''"
        ).fetchone()
        day = r["d"]
    if not day:
        print("无可验证交易日")
        return 1
    print(f"═══ C0 三门首夜验证报告: {day} ═══\n")
    issues = 0

    trades = conn.execute(
        "SELECT symbol, direction, realized_pnl, mfe_pct, mae_pct, holding_hours, "
        "quantity, exit_reason, metadata_json FROM trade_records "
        "WHERE trade_date=? AND source_type LIKE 'PLAYBOOK%' "
        "AND exit_time IS NOT NULL AND exit_time!=''", (day,)
    ).fetchall()
    print(f"当日平仓: {len(trades)} 笔, 净PnL {sum(t['realized_pnl'] or 0 for t in trades):+.1f}")

    # ── 判据1: 停滞退出行为 ──
    stall = [t for t in trades if t["exit_reason"] == "MFE_STALL_EXIT"]
    print(f"\n[判据1] MFE_STALL_EXIT: {len(stall)} 笔")
    for t in stall:
        # 水下判定 (2026-07-25 尺子修正): 优先触发现场 cur_at_trigger, 回退 pnl 容差 ±半tick×qty
        try:
            _se = (json.loads(t["metadata_json"] or "{}").get("stall_exit") or {})
        except Exception:
            _se = {}
        _cur = _se.get("cur_at_trigger")
        if _cur is not None:
            underwater = float(_cur) < 0
            uw_src = f"cur@trigger={float(_cur):+.2f}%"
        else:
            _tol = 0.005 * float(t["quantity"] or 0)  # 半 tick ($0.005) × 股数
            underwater = (t["realized_pnl"] or 0) <= _tol
            uw_src = f"pnl容差±{_tol:.2f}"
        legit = (t["mfe_pct"] or 0) < 0.5 and underwater \
            and (t["holding_hours"] or 0) >= 2.4
        mark = OK if legit else BAD
        if not legit:
            issues += 1
        print(f"  {mark} {t['symbol']} pnl={t['realized_pnl']:+.1f} "
              f"mfe={t['mfe_pct']:.2f}% held={t['holding_hours']:.1f}h ({uw_src})")
    leak = [t for t in trades if t["exit_reason"] == "TIME_EXIT"
            and (t["mfe_pct"] or 0) < 0.5 and (t["holding_hours"] or 0) > 3]
    if leak:
        issues += 1
        print(f"  {BAD} TIME_EXIT 漏网 (低MFE持有>3h 本应被停滞退出接管): "
              f"{[(t['symbol'], t['holding_hours']) for t in leak]}")
    else:
        print(f"  {OK} TIME_EXIT 中无 MFE<0.5% 且持有>3h 的漏网单")

    # ── 判据2: 触发侧闸门遥测 (注意: 遥测 date 为 CST, 覆盖 ET 当日晚间需查两天) ──
    counts = {}
    syms = {}  # 2026-07-25 小项④: 每轮重评重复计数 (~49次/标的/日), 去重标的数才是真拦截面
    if TELEMETRY.exists():
        d0 = datetime.strptime(day, "%Y-%m-%d")
        days = {day, (d0 + timedelta(days=1)).strftime("%Y-%m-%d")}
        for line in TELEMETRY.open():
            try:
                rrow = json.loads(line)
            except Exception:
                continue
            if rrow.get("date") in days and rrow.get("reason") in ("streak_ban", "low_atr_block"):
                counts[rrow["reason"]] = counts.get(rrow["reason"], 0) + 1
                syms.setdefault(rrow["reason"], set()).add(rrow.get("symbol", ""))
    print(f"\n[判据2] 闸门遥测: streak_ban={counts.get('streak_ban', 0)} "
          f"({len(syms.get('streak_ban', set()))} 标的) "
          f"low_atr_block={counts.get('low_atr_block', 0)} "
          f"({len(syms.get('low_atr_block', set()))} 标的) "
          f"(0 不算失败 — 当日可能无命中标的; 次数含每轮重评重复计数)")

    # ── 判据3: regime 落库 ──
    rg = conn.execute(
        "SELECT COUNT(*) n, SUM(market_regime IS NOT NULL AND market_regime!='') ok "
        "FROM playbook_instructions WHERE date=?", (day,)
    ).fetchone()
    mark = OK if (rg["n"] or 0) > 0 and rg["ok"] == rg["n"] else (WARN if rg["n"] else BAD)
    if rg["n"] and rg["ok"] != rg["n"]:
        issues += 1
    print(f"\n[判据3] regime 落库: {rg['ok']}/{rg['n']} {mark}")

    # ── 判据4: TIME_EXIT 桶 + 赢家路径 ──
    te = [t for t in trades if t["exit_reason"] == "TIME_EXIT"]
    te_pnl = sum(t["realized_pnl"] or 0 for t in te)
    mark = OK if te_pnl > -100 else BAD
    if te_pnl <= -100:
        issues += 1
    print(f"\n[判据4] TIME_EXIT 桶: {len(te)} 笔 {te_pnl:+.1f} (目标 >-100, 07-23 为 -349) {mark}")
    winners = [t for t in trades if (t["mfe_pct"] or 0) >= 0.5]
    bad_w = [t for t in winners if t["exit_reason"] == "MFE_STALL_EXIT"]
    if bad_w:
        issues += 1
        print(f"  {BAD} 零质量损失违约: MFE≥0.5% 被停滞退出砍掉 {[t['symbol'] for t in bad_w]}")
    else:
        print(f"  {OK} MFE≥0.5% 的 {len(winners)} 笔全部由既有盈利路径接住 (零质量损失)")

    # ── 附加: 出场滑点首夜数据 (C1 补测) ──
    xs = []
    for t in trades:
        try:
            m = json.loads(t["metadata_json"] or "{}")
        except Exception:
            continue
        v = m.get("exit_slippage_bps")
        if v is None:
            v = (m.get("execution") or {}).get("exit_slippage_bps")
        if v is not None:
            xs.append(float(v))
    if xs:
        xs.sort()
        print(f"\n[附加·C1] 出场滑点: n={len(xs)} 中位={xs[len(xs)//2]:+.1f}bps "
              f"均值={sum(xs)/len(xs):+.1f}bps 最差={max(xs):+.1f}bps")
    else:
        print(f"\n[附加·C1] 出场滑点: 无记录 (埋点 2026-07-24 上线, 首夜后应有数据)")

    print(f"\n═══ 结论: {'全部通过 ' + OK if issues == 0 else f'{issues} 项异常 {BAD} — 按 DEB 回滚预案评估'} ═══")
    return 0 if issues == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
