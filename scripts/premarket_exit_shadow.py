#!/usr/bin/env python3
"""盘前兑现 shadow (2026-09-16; **2026-09-17 用户裁「方案1」后已非 record-only** — 见下).

问题: 隔夜仓盘前 spike (NOK 9/16 06:10 ET pre_high 10.35 = +3.2% vs entry)
到 09:02 单日单点 premarket_reprice_shadow 已回落至 -1.9% — 盘前盈利在
开盘前蒸发, 现有影子捕不到 spike, 也从不回答"该不该趁盘前直接平仓锁盈".

与既有 shadow 的分工 (互补非重复):
  - premarket_reprice_shadow: 09:02 单点问"该不该重定价 be/SL" (持有中保护)
  - 本 shadow: 04:00-09:30 高频轮询问"该不该盘前直接平仓兑现" (退出决策)

数据源 (LV3 实测 2026-09-16 06:25 ET):
  get_market_snapshot.last_price = 盘前实时可成交价 (非昨收, update_time=当前分钟)
  pre_volume = 盘前累计成交量 (薄量 spike 过滤用)
  prev_close_price = 昨收 (分母, 盘前窗稳定)
  无需订阅推送 — 盘前逐笔太稀疏, 轮询快照即得准实时价.

决策规则 (常量带注释, 攒样≥30 后裁决):
  pnl_vs_entry = (last - entry)/entry × 方向符号 × 100  (相对入场价, 用户口径)
  - pnl ≥ LOCK_IN_AT_PCT 且 pre_volume ≥ MIN_PRE_VOLUME → lock_in  (建议兑现)
  - pnl ≤ TIGHTEN_AT_PCT                              → tighten  (建议收紧)
  - 其余                                              → hold
  - 盘前无成交 (last≤0 或 update_time 非盘前窗)        → no_data

执行 (2026-09-17 用户裁「方案1」, 详见 DEB-20260917 §12):
  lock_in / breakeven_lock(回本+0.3%有量) / tighten 三信号且窗内 → 调
  extended_hours_exit_executor.py SIM 真平仓 (session=ETH 限价单) + 飞书推结果.
  tighten 是延长时段止损的**唯一兜底** (盘中 monitor_playbook_sim_exits 只管 TRADING 相位).
  其余信号仍只落 jsonl. 回滚: 删调用块 (退回 record-only).

结算: 次轮运行对 trade_date < 今日 ET 的行, 用该日完整日 K + trade_records
      结局 join, 算"若按 lock_in 兑现 vs 实际结局"的反事实 delta.

用法: PYTHONPATH=. venv/bin/python scripts/premarket_exit_shadow.py [--dry]
挂载: crontab 北京 16:00-21:25 每 5min (= ET 04:00-09:25 夏令时), 交易日 gate.
      ⚠️ 11 月冬令时 ET 窗右移 1h → 北京 cron 需改为 17:00-22:25 (列入 11 月重评)
回滚: 删 crontab 行 + 删本脚本 (jsonl 保留供分析).
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
OUTPUT = ROOT / "data" / "premarket_exit_shadow.jsonl"

# ── 决策阈值 (9/16 初值, 攒样后裁决; 保守口径对齐用户风险偏好) ──
LOCK_IN_AT_PCT = 2.0      # 相对入场价 ≥+2% → 建议兑现 (NOK 今日 spike +3.2%)
MIN_PRE_VOLUME = 50000    # 盘前累计量 ≥5万股 才算"有价有量", 薄量 spike 不算
TIGHTEN_AT_PCT = -1.5     # 相对入场价 ≤-1.5% → 建议收紧 (盘前逆向)
BREAKEVEN_LOCK_PCT = 0.3  # 回本线锁盈 (方案二, 9/17 用户批准): 盘前价>成本+0.3% 且有量 → 推送锁盈
                          # 语义: "回本线以上出现盈利就锁" 而非"涨2%才锁" — 用户原话


def _today_et() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().strftime("%Y-%m-%d")
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            from datetime import timezone, timedelta
            return datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")


def _now_et_hhmm() -> int:
    try:
        from app.utils.timezone import now_et
        n = now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        n = datetime.now(ZoneInfo("America/New_York"))
    return n.hour * 100 + n.minute


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


def decide(pnl_pct: float, pre_volume: float) -> str:
    if pnl_pct is None:
        return "no_data"
    if pnl_pct >= LOCK_IN_AT_PCT and (pre_volume or 0) >= MIN_PRE_VOLUME:
        return "lock_in"
    # 方案二: 回本线以上锁盈 (比 lock_in 更低门槛, 用户核心诉求 — 盘前回本即锁)
    if pnl_pct >= BREAKEVEN_LOCK_PCT and (pre_volume or 0) >= MIN_PRE_VOLUME:
        return "breakeven_lock"
    if pnl_pct <= TIGHTEN_AT_PCT:
        return "tighten"
    return "hold"


_BE_LOCK_SENT = set()  # (symbol, trade_date) 防重复推送


def _push_lock_suggestion(symbol, direction, qty, entry, px, pnl_pct, pnl_usd, pre_vol):
    """盘前回本锁盈 → 飞书半自动推送 (每标的每日一次). 失败不阻断影子落盘."""
    key = (symbol, _today_et())
    if key in _BE_LOCK_SENT:
        return
    try:
        from app.notifier.feishu import FeishuNotifier
        nf = FeishuNotifier()
        action = "卖出" if _dir_sign(direction) > 0 else "买回"
        msg = (f"📈 盘前回本锁盈建议\n"
               f"{symbol} {direction} {int(qty)}股\n"
               f"成本 ${entry:.2f} → 盘前 ${px:.2f} ({pnl_pct:+.2f}% / ${pnl_usd:+.0f})\n"
               f"盘前量 {pre_vol:,.0f} 股 (有量)\n"
               f"建议: 盘前{action}锁盈 (回本线以上)\n"
               f"⚠️ 半自动 — 确认后我执行; 开盘 9:30 前有效")
        if nf.send_text(msg):
            _BE_LOCK_SENT.add(key)
    except Exception:
        pass  # 推送失败不影响影子落盘


def _load_open_positions():
    """当前全部持仓 (不限隔夜 — 盘前 spike 对当日仓无意义, 但 04:00-09:30
    时段 open_positions 里都是隔夜仓; 不过滤, 结算侧按 entry_date<today 核).
    返回: (symbol, direction, qty, entry_price, current_sl, entry_time, signal_id)
    """
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
    hhmm = _now_et_hhmm()
    # 盘前窗 04:00-09:29 ET (夏令时); 窗外 cron 也会拦, 双保险
    in_window = 400 <= hhmm <= 929

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
        # 盘前真实价 = pre_price (盘前窗内权威); last_price 在盘前滞后停留在昨收
        # (2026-09-16 实锤: NOK 盘前 last 全程卡9.84不动, pre_high却从10.35涨到10.47 — 真价在pre_price)
        # 窗外回退 last_price (非盘前窗运行时兜底).
        last_raw = float(s.get("last_price", 0) or 0) if s is not None else 0.0
        pre_px = float(s.get("pre_price", 0) or 0) if s is not None else 0.0
        last = pre_px if (in_window and pre_px > 0) else last_raw
        pre_vol = float(s.get("pre_volume", 0) or 0) if s is not None else 0.0
        prev_close = float(s.get("prev_close_price", 0) or 0) if s is not None else 0.0
        pre_high = float(s.get("pre_high_price", 0) or 0) if s is not None else 0.0
        upd = str(s.get("update_time", "")) if s is not None else ""
        sign = _dir_sign(direction)
        if last > 0 and entry > 0:
            pnl_pct = round((last - entry) / entry * 100 * sign, 3)
            pnl_usd = round((last - entry) * qty * sign, 2)
            action = decide(pnl_pct, pre_vol)
            # 2026-09-17 用户裁方案1 (SIM真下单+规则引擎兜底): lock_in锁盈 + tighten止损 都调 exit executor 真平仓.
            #   tighten 是 EXT 仓盘前止损的唯一兜底 (盘中 monitor 只管 TRADING 时段).
            if action in ("lock_in", "breakeven_lock", "tighten") and in_window and not dry:
                try:
                    import subprocess as _sp
                    # 2026-09-17 修复(实盘发现): 原为 capture_output=True + except: pass ⇒ 子进程异常/超时
                    #   **完全不可见** (04:10/04:20 CTNT/GIPR 平仓单在券商侧已成交, 但本地无审计行、无 DB 回写,
                    #   根因因输出被丢弃而无法定位)。现把 rc/stdout/stderr 尾部打进日志 (cron 重定向到本 log)。
                    _proc = _sp.run(
                        [sys.executable, str(ROOT / "scripts" / "extended_hours_exit_executor.py"),
                         "--symbol", symbol, "--direction", direction, "--qty", str(qty),
                         "--price", str(last), "--session", "premarket", "--reason", action],
                        capture_output=True, text=True, timeout=90)
                    _tail = ((_proc.stdout or "")[-300:] + " | " + (_proc.stderr or "")[-300:]).strip()
                    if _proc.returncode != 0:
                        print(f"[shadow_exit] {symbol} {action} exit_executor rc={_proc.returncode} :: {_tail}")
                    else:
                        print(f"[shadow_exit] {symbol} {action} exit_executor ok :: {_tail[-200:]}")
                except Exception as _exc:
                    print(f"[shadow_exit] {symbol} {action} exit_executor 异常: {type(_exc).__name__}: {_exc}")
                _push_lock_suggestion(symbol, direction, qty, entry, last, pnl_pct, pnl_usd, pre_vol)
        else:
            pnl_pct, pnl_usd, action = None, None, "no_data"
        # 距 pre_high 回撤幅度 (spike 可持续性指标)
        pullback = round((pre_high - last) / pre_high * 100, 3) if pre_high > 0 and last > 0 else None
        row = {
            "trade_date": today_et,
            "hhmm_et": hhmm,
            "in_premarket_window": in_window,
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry_price": entry,
            "current_sl": sl,
            "signal_id": signal_id,
            "entry_time": entry_time,
            "prev_close": prev_close,
            "last_price": last,          # LV3 盘前实时价 (非昨收)
            "pre_high": pre_high,
            "pre_volume": pre_vol,
            "update_time": upd,
            "pnl_pct_vs_entry": pnl_pct,  # 相对入场价 (用户口径)
            "pnl_usd": pnl_usd,
            "pullback_from_pre_high_pct": pullback,
            "action": action,
            "settled": False,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        }
        if dry:
            print(f"[dry] {symbol} last={last} pnl={pnl_pct}% ${pnl_usd} vol={pre_vol:.0f} → {action}")
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
