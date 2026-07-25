#!/usr/bin/env python3
"""链路提前验收 · 分阶段检查 (2026-07-24, DEB-20260724 §九配套).

用法: ./venv/bin/python scripts/early_acceptance_check.py [--phase PRE|OPEN|MID]
      缺省按 ET 时间自动判相位。

定位: 回答"链路是否正常"——可提前到盘前/盘中分阶段验收;
      不回答"策略是否赚钱"——那需要 ≥2 周样本 (08-07 裁决点), 无法提前。

相位与检查项:
  PRE  (ET <09:30):  三门配置加载 / 服务存活 / 当日指令(regime+atr覆盖) /
                     闸门干跑预演 (低ATR门+连败封禁 对真实指令逐条预判)
  OPEN (09:30-12:00): 触发与入场 / 闸门遥测命中 / 入场滑点记录 / entry_path 落库
  MID  (12:00-16:00): 停滞退出资格与触发 / 出场滑点记录 / TIME_EXIT 漏网监测
"""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
TELEMETRY = ROOT / "data" / "trigger_miss_telemetry.jsonl"
ET = ZoneInfo("America/New_York")
OK, BAD, WARN, INFO = "✅", "❌", "⚠️", "ℹ️"

# 与 live 一致的预演参数 (unified_config playbook.execution_parameters)
ATR_MIN = 0.025
BAN_LOOKBACK_D = 7
BAN_STREAK = 2
BAN_MFE = 0.5


def et_now():
    return datetime.now(ET)


def phase_auto() -> str:
    t = et_now()
    hm = t.hour * 100 + t.minute
    if hm < 930:
        return "PRE"
    if hm < 1200:
        return "OPEN"
    return "MID"


def latest_inst_date(conn, today_et: str) -> str:
    r = conn.execute("SELECT MAX(date) d FROM playbook_instructions WHERE date<=?",
                     (today_et,)).fetchone()
    return r["d"] or today_et


def streak_banned(conn, symbol: str) -> tuple:
    """复刻 pipeline._symbol_streak_banned 的 SQL 逻辑 (只读预演)."""
    rows = conn.execute(
        "SELECT realized_pnl, mfe_pct FROM trade_records "
        "WHERE symbol=? AND source_type LIKE 'PLAYBOOK%' "
        "AND exit_time IS NOT NULL AND exit_time!='' "
        "AND trade_date >= date('now', ?) "
        "ORDER BY trade_date DESC, exit_time DESC LIMIT 10",
        (symbol, f"-{BAN_LOOKBACK_D} day")).fetchall()
    streak = 0
    for pnl, mfe in rows:
        if pnl is not None and pnl < 0 and mfe is not None and mfe < BAN_MFE:
            streak += 1
        else:
            break
    return streak >= BAN_STREAK, streak


def check_pre(conn, today_et: str) -> int:
    issues = 0
    # 1) 服务 + 配置
    try:
        act = subprocess.run(["systemctl", "is-active", "ftseek-us"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        act = "unknown"
    print(f"[服务] ftseek-us: {act} {'%s' % OK if act == 'active' else BAD}")
    if act != "active":
        issues += 1
    sys.path.insert(0, str(ROOT))
    try:
        from app.playbook.config import get_playbook_config
        cfg = get_playbook_config()
        gates = {k: cfg.get(k) for k in
                 ("mfe_stall_exit_enabled", "symbol_streak_ban_enabled", "low_atr_gate_enabled")}
        all_on = all(gates.values())
        print(f"[配置] 三门开关: {gates} {'%s' % OK if all_on else WARN}")
    except Exception as e:
        print(f"[配置] 读取失败: {e} {BAD}")
        issues += 1

    # 2) 当日指令 + 覆盖率
    d = today_et
    r = conn.execute(
        "SELECT COUNT(*) n, SUM(market_regime IS NOT NULL AND market_regime!='') rg, "
        "SUM(atr_pct IS NOT NULL AND atr_pct>0) atr "
        "FROM playbook_instructions WHERE date=?", (d,)).fetchone()
    if not r["n"]:
        d = latest_inst_date(conn, today_et)
        print(f"[指令] {today_et} 尚未生成 (盘前 ET 05:00-09:00 生成) → 用 {d} 干跑预演 {INFO}")
        r = conn.execute(
            "SELECT COUNT(*) n, SUM(market_regime IS NOT NULL AND market_regime!='') rg, "
            "SUM(atr_pct IS NOT NULL AND atr_pct>0) atr "
            "FROM playbook_instructions WHERE date=?", (d,)).fetchone()
    else:
        print(f"[指令] {d}: {r['n']} 条 | regime覆盖 {r['rg']}/{r['n']} "
              f"{'%s' % OK if r['rg'] == r['n'] else WARN} | atr覆盖 {r['atr']}/{r['n']}")

    # 3) 闸门干跑预演: 对该日真实指令逐条预判
    rows = conn.execute(
        "SELECT symbol, direction, atr_pct FROM playbook_instructions "
        "WHERE date=? AND direction IN ('BUY','SELL_SHORT')", (d,)).fetchall()
    atr_block, ban_block = [], []
    for row in rows:
        a = row["atr_pct"]
        if a is not None and 0 < float(a) < ATR_MIN:
            atr_block.append(f"{row['symbol']}({float(a) * 100:.1f}%)")
        banned, streak = streak_banned(conn, row["symbol"])
        if banned:
            ban_block.append(f"{row['symbol']}(连败{streak})")
    print(f"[干跑·低ATR门] 将拦 {len(atr_block)}/{len(rows)}: {', '.join(atr_block) or '无'}")
    print(f"[干跑·连败封禁] 将拦 {len(ban_block)}/{len(rows)}: {', '.join(ban_block) or '无'}")
    if len(atr_block) + len(ban_block) >= len(rows) and rows:
        print(f"  {WARN} 全部指令都会被拦 — 检查阈值是否过紧")
        issues += 1
    return issues


def check_open_mid(conn, today_et: str, phase: str) -> int:
    issues = 0
    trades = conn.execute(
        "SELECT symbol, direction, entry_time, exit_time, exit_reason, realized_pnl, "
        "mfe_pct, holding_hours, metadata_json FROM trade_records "
        "WHERE trade_date=? AND source_type LIKE 'PLAYBOOK%'", (today_et,)).fetchall()
    opened = [t for t in trades if t["entry_time"]]
    closed = [t for t in trades if t["exit_time"]]
    print(f"[入场] 今日开仓 {len(opened)} 笔, 已平 {len(closed)} 笔")

    # 入场滑点
    slip = []
    for t in opened:
        try:
            ex = (json.loads(t["metadata_json"] or "{}").get("execution") or {})
        except Exception:
            continue
        if ex.get("slippage_bps") is not None:
            slip.append(float(ex["slippage_bps"]))
    print(f"[入场滑点] 记录 {len(slip)}/{len(opened)}"
          + (f" 中位 {sorted(slip)[len(slip) // 2]:+.1f}bps" if slip else ""))

    # 闸门遥测 (CST 日期, 覆盖 ET 当日需查两天)
    counts = {}
    syms = {}  # 2026-07-25 小项④: 去重标的数 (次数含每轮重评重复计数)
    if TELEMETRY.exists():
        d0 = datetime.strptime(today_et, "%Y-%m-%d")
        days = {today_et, (d0 + timedelta(days=1)).strftime("%Y-%m-%d")}
        for line in TELEMETRY.open():
            try:
                rr = json.loads(line)
            except Exception:
                continue
            if rr.get("date") in days:
                counts[rr.get("reason")] = counts.get(rr.get("reason"), 0) + 1
                syms.setdefault(rr.get("reason"), set()).add(rr.get("symbol", ""))
    print(f"[闸门遥测] streak_ban={counts.get('streak_ban', 0)} ({len(syms.get('streak_ban', set()))} 标的) "
          f"low_atr_block={counts.get('low_atr_block', 0)} ({len(syms.get('low_atr_block', set()))} 标的) "
          f"(其他: vol={counts.get('vol_gate_block', 0)} vwap={counts.get('vwap_gate_block', 0)})")

    if phase == "MID":
        # 停滞退出: 已触发 + 在场待判
        stall = [t for t in closed if t["exit_reason"] == "MFE_STALL_EXIT"]
        print(f"[停滞退出] 已触发 {len(stall)} 笔: "
              + ", ".join(f"{t['symbol']}({t['realized_pnl']:+.1f})" for t in stall) if stall
              else "[停滞退出] 尚未触发 (首批资格 ≈ 入场后 150min)")
        leak = [t for t in closed if t["exit_reason"] == "TIME_EXIT"
                and (t["mfe_pct"] or 0) < 0.5 and (t["holding_hours"] or 0) > 3]
        if leak:
            print(f"  {BAD} TIME_EXIT 漏网: {[t['symbol'] for t in leak]}")
            issues += 1
        # 出场滑点
        xslip = []
        for t in closed:
            try:
                m = json.loads(t["metadata_json"] or "{}")
            except Exception:
                continue
            if m.get("exit_slippage_bps") is not None:
                xslip.append(float(m["exit_slippage_bps"]))
        print(f"[出场滑点] 记录 {len(xslip)}/{len(closed)}"
              + (f" 中位 {sorted(xslip)[len(xslip) // 2]:+.1f}bps" if xslip else ""))
        if closed and not xslip:
            print(f"  {WARN} 有平仓但无出场滑点记录 — 检查埋点")
            issues += 1
    return issues


def main() -> int:
    phase = None
    if "--phase" in sys.argv:
        phase = sys.argv[sys.argv.index("--phase") + 1].upper()
    phase = phase or phase_auto()
    today_et = et_now().strftime("%Y-%m-%d")
    print(f"═══ 链路提前验收 · 相位 {phase} · ET {et_now().strftime('%m-%d %H:%M')} ═══")
    print(f"{INFO} 本脚本验收链路机械性; 策略盈亏有效性需 ≥2 周样本 (08-07 裁决点), 不可提前\n")

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    issues = check_pre(conn, today_et) if phase == "PRE" else check_open_mid(conn, today_et, phase)
    print(f"\n═══ {phase} 相位: {'通过 ' + OK if issues == 0 else f'{issues} 项异常 {BAD}'} ═══")
    return 0 if issues == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
