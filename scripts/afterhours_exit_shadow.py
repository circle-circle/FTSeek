#!/usr/bin/env python3
"""盘后兑现 shadow (2026-09-16; **2026-09-17 用户裁「方案1」后已非 record-only** — 见下).

问题: 盘前已有 exit_shadow/reprice_shadow 两条线, 盘后 16:00-20:00 ET
持仓盈利管理完全空白 — 财报/异动盘后 spike 无监控, 由亏转盈机会流失.

与盘前 exit_shadow 对称 (复用骨架, 差异三处):
  1. 时段窗: 盘后 16:00-20:00 ET (北京夏令时 04:00-08:00, 冬令时 05:00-09:00)
  2. 分母:   盘前用 prev_close (昨日正式收盘), 盘后用当日正式收盘
             (盘后时段 snapshot.last_price=盘后市场最新价, prev_close_price
              在盘后仍=当日收盘, 与盘前语义一致 — 实测 prev_close 在盘后窗稳定)
  3. 数据源: 盘后 LV3 同样有实时快照 (与盘前同通道, 9/16 已实测)

决策规则 (与盘前 exit 同阈值, 攒样后一起裁决):
  pnl_vs_entry = (last - entry)/entry × 方向符号 × 100
  - pnl ≥ LOCK_IN_AT_PCT 且量≥MIN_SESSION_VOL → lock_in (建议兑现)
  - pnl ≤ TIGHTEN_AT_PCT                      → tighten (建议收紧)
  - 其余                                       → hold

记录: 与盘前 exit 同构 jsonl, 增 session="afterhours" 标签, 供统一结算器
      聚合时段收益台账 (盘前/盘后 lock_in 命中+反事实delta).

执行 (2026-09-17 用户裁「方案1」, 详见 DEB-20260917 §12):
  lock_in / breakeven_lock(回本+0.3%有量) / tighten 三信号且窗内 → 调
  extended_hours_exit_executor.py SIM 真平仓 (session=ETH 限价单) + 飞书推结果.
  tighten 是延长时段止损的**唯一兜底** (盘中 monitor_playbook_sim_exits 只管 TRADING 相位).
  其余信号仍只落 jsonl. 回滚: 删调用块 (退回 record-only).

用法: PYTHONPATH=. venv/bin/python scripts/afterhours_exit_shadow.py [--dry]
挂载: crontab 北京 04:00-07:55 每 5min (= ET 16:00-19:55 夏令时), 交易日 gate.
      ⚠️ 11月冬令时 ET窗右移1h → 北京 cron 改 05:00-08:55 (列入 11 月重评)
回滚: 删 crontab 行 + 删本脚本 (jsonl 保留).
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

DB = ROOT / "data" / "ftseek_us.db"
OUTPUT = ROOT / "data" / "afterhours_exit_shadow.jsonl"

# ── 决策阈值 (与盘前 exit 同值, 对称攒样一起裁决) ──────────────────
LOCK_IN_AT_PCT = 2.0       # 相对入场价 ≥+2% → 建议兑现
MIN_SESSION_VOL = 50000    # 盘后量 ≥5万股 (盘后更薄, 阈值同盘前先攒样)
BREAKEVEN_LOCK_PCT = 0.3   # 回本线锁盈 (2026-09-17 用户批准, 与盘前对称): 盘后市价>成本+0.3% 且有量
TIGHTEN_AT_PCT = -1.5      # 相对入场价 ≤-1.5% → 建议收紧


def _now_et():
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _is_trading_day() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return True  # fail-open


def _dir_sign(direction: str) -> int:
    return -1 if str(direction).upper() in ("SHORT", "SELL_SHORT") else 1


def decide(pnl_pct: float, sess_vol: float) -> str:
    if pnl_pct is None:
        return "no_data"
    if pnl_pct >= LOCK_IN_AT_PCT and (sess_vol or 0) >= MIN_SESSION_VOL:
        return "lock_in"
    # 回本线锁盈 (2026-09-17, 与盘前对称): 回本即锁
    if pnl_pct >= BREAKEVEN_LOCK_PCT and (sess_vol or 0) >= MIN_SESSION_VOL:
        return "breakeven_lock"
    if pnl_pct <= TIGHTEN_AT_PCT:
        return "tighten"
    return "hold"


def _load_open_positions():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT symbol, direction, size, avg_entry_price, "
            "       COALESCE(current_sl, 0), entry_time, signal_id "
            "FROM open_positions WHERE size > 0").fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


def collect(dry: bool) -> int:
    today_et = _today_et()
    n_et = _now_et()
    hhmm = n_et.hour * 100 + n_et.minute
    # 盘后窗 16:00-19:59 ET (夏令时); 窗外 cron 也会拦, 双保险
    in_window = 1600 <= hhmm <= 1959

    positions = _load_open_positions()
    if not positions:
        print(f"[collect] {today_et} {hhmm:04d}ET 无持仓")
        return 0

    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    snap_map = {}
    try:
        ret, snap = ctx.get_market_snapshot([p[0] for p in positions])
        if ret == RET_OK:
            for _, r in snap.iterrows():
                snap_map[r["code"]] = r
    finally:
        ctx.close()

    n = 0
    for symbol, direction, qty, entry, sl, entry_time, signal_id in positions:
        s = snap_map.get(symbol)
        # 盘后真实价 = after_price (盘后窗内权威); last_price 在盘后滞后停留在盘中收盘
        # (2026-09-17 实测: NOK 盘后 after_price=10.29 vs last=10.14 差1.5%, after_vol在动)
        # 窗外回退 last_price (非盘后窗运行时兜底).
        last_raw = float(s.get("last_price", 0) or 0) if s is not None else 0.0
        after_px = float(s.get("after_price", 0) or 0) if s is not None else 0.0
        last = after_px if (in_window and after_px > 0) else last_raw
        # 盘后分母: prev_close_price 在盘后窗 = 当日正式收盘 (盘后涨跌基准)
        prev_close = float(s.get("prev_close_price", 0) or 0) if s is not None else 0.0
        # 盘后量字段: after_volume (实测字段名, 9/16 verify)
        sess_vol = float(s.get("after_volume", 0) or 0) if s is not None else 0.0
        upd = str(s.get("update_time", "")) if s is not None else ""
        sign = _dir_sign(direction)
        if last > 0 and entry > 0:
            pnl_pct = round((last - entry) / entry * 100 * sign, 3)
            pnl_usd = round((last - entry) * qty * sign, 2)
            action = decide(pnl_pct, sess_vol)
            # 2026-09-17 用户裁方案1: lock_in锁盈 + tighten止损 调 exit executor 真平仓 (EXT仓盘后止损兜底).
            if action in ("lock_in", "breakeven_lock", "tighten") and in_window and not dry:
                try:
                    import subprocess as _sp
                    # 2026-09-17 修复(与盘前同批): 原 capture_output=True + except: pass ⇒ 子进程异常/超时
                    #   完全不可见 (券商侧已成交但本地无审计/无 DB 回写时无法定位)。现打 rc/stdout/stderr 尾部。
                    _proc = _sp.run(
                        [sys.executable, str(ROOT / "scripts" / "extended_hours_exit_executor.py"),
                         "--symbol", symbol, "--direction", direction, "--qty", str(qty),
                         "--price", str(last), "--session", "afterhours", "--reason", action],
                        capture_output=True, text=True, timeout=90)
                    _tail = ((_proc.stdout or "")[-300:] + " | " + (_proc.stderr or "")[-300:]).strip()
                    if _proc.returncode != 0:
                        print(f"[shadow_exit] {symbol} {action} exit_executor rc={_proc.returncode} :: {_tail}")
                    else:
                        print(f"[shadow_exit] {symbol} {action} exit_executor ok :: {_tail[-200:]}")
                except Exception as _exc:
                    print(f"[shadow_exit] {symbol} {action} exit_executor 异常: {type(_exc).__name__}: {_exc}")
        else:
            pnl_pct, pnl_usd, action = None, None, "no_data"
        row = {
            "session": "afterhours",
            "line": "exit_lockin",
            "trade_date": today_et,
            "hhmm_et": hhmm,
            "in_afterhours_window": in_window,
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry_price": entry,
            "current_sl": sl,
            "signal_id": signal_id,
            "entry_time": entry_time,
            "prev_close": prev_close,        # 盘后窗 = 当日收盘 (盘后涨跌分母)
            "last_price": last,              # 盘后窗内 = after_price (真实盘后价)
            "after_price": after_px,         # 原始盘后价 (审计留痕)
            "session_volume": sess_vol,
            "update_time": upd,
            "pnl_pct_vs_entry": pnl_pct,
            "pnl_usd": pnl_usd,
            "action": action,
            "settled": False,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        }
        if dry:
            print(f"[dry] {symbol} last={last} close={prev_close} pnl={pnl_pct}% ${pnl_usd} vol={sess_vol:.0f} → {action}")
        else:
            with OUTPUT.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if not _is_trading_day():
        print("[collect] 非交易日, 跳过")
        return
    n = collect(args.dry)
    print(f"[collect] 落 {n} 行 → {OUTPUT}")


if __name__ == "__main__":
    main()
