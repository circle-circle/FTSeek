#!/usr/bin/env python3
"""盘后开仓影子 (2026-09-17, record-only, 用户批准) —— **同日停用, 勿重挂 cron**。

停用裁决 (2026-09-17 用户): 方向错 — 用户要的是**事件确定性**(财报/突发), 不是价格动量异动;
本影子的 momentum 判据与该目标不符, crontab 行已删。文件与已产出的 jsonl 保留 (归档留痕非删证)。
`scripts/settle_extended_hours.py` 的 SOURCES 仍列本线 jsonl ⇒ 恒空, 不报错 (待随台账清理)。

问题: 盘后 16:00-20:00 ET 财报/异动高发, 价格 spike 是"第一时间下手"的机会窗,
但当前系统盘后零入场 (自动入场只在 TRADING). 本影子记录盘后异动信号,
攒样裁决"盘后动量入场是否有正期望".

与盘后 exit_shadow 的分工 (互补非重复):
  - afterhours_exit_shadow: 已有持仓盘后该不该平仓 (退出)
  - 本 shadow: 盘后无持仓标的出现异动该不该开仓 (入场)

数据源 (LV3 实测 2026-09-17):
  get_market_snapshot 的 after_price/after_change_rate/after_volume/after_amplitude
  盘后时段全部有值. after_price = 盘后真实价 (after_volume 同步累积, 链路已验证).
  ⚠️ 不用 last_price (盘后滞后停留在盘中收盘, 9/16 实锤差1.5%).

信号规则 (常量带注释, 攒样≥30 后裁决; 保守口径对齐用户风险偏好):
  盘后动量 (双向):
    after_change_rate ≥ +MOM_UP_PCT 且 after_volume ≥ MIN_VOL → long_candidate  (跟涨)
    after_change_rate ≤ -MOM_DN_PCT 且 after_volume ≥ MIN_VOL → short_candidate (跟跌)
    after_amplitude ≥ MAX_AMPL_PCT → 波动过大, 降标 amplify_flag (薄量扫价风险)
  无催化/无量 → no_signal

结算: 次日对 long/short candidate 行用后续 K 线算"若开盘入场"的反事实收益,
      join trade_records 看是否真被盘中策略接住. 攒样后裁决是否转正.

用法: PYTHONPATH=. venv/bin/python scripts/afterhours_momentum_shadow.py [--dry]
挂载: crontab 北京 04:05-07:55 每 5min (= ET 16:05-19:55 夏令时), 交易日 gate.
      ⚠️ 11月冬令时右移1h → 北京 05:05-08:55 (列入 11 月重评)
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
OUTPUT = ROOT / "data" / "afterhours_momentum_shadow.jsonl"

# ── 扫描池: 复用 premarket_enrichment MOVER_POOL + 当前持仓 ─────────
#   (盘后异动主要在流动性好的大盘/热门股, 小票盘后薄量扫价不可靠)
MOVER_POOL = [
    "US.SPY", "US.QQQ", "US.IWM", "US.TSLA", "US.NVDA", "US.AAPL", "US.MSFT",
    "US.AMZN", "US.META", "US.GOOGL", "US.AMD", "US.NFLX", "US.BABA", "US.MU",
    "US.PLTR", "US.COIN", "US.MSTR", "US.ARM", "US.SMCI", "US.AVGO",
]

# ── 信号阈值 (9/17 初值, 攒样后裁决; 盘后薄量, 阈值比盘中更严) ──
MOM_UP_PCT = 1.5       # 盘后涨 ≥1.5% 才算异动 (盘中常用1%, 盘后收紧)
MOM_DN_PCT = -1.5      # 盘后跌 ≥1.5%
MIN_VOL = 200000       # 盘后量 ≥20万股 (比 exit 的5万更严 — 入场要真量)
MAX_AMPL_PCT = 3.0     # 盘后振幅 ≥3% = 薄量扫价风险, 降标不否决
MIN_PRICE = 5.0        # 股价 ≥$5 (防仙股薄量操控)


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
        return True


def _load_open_symbols():
    """当前持仓标的 — 已在仓的不重复开 (排他)."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute("SELECT symbol FROM open_positions WHERE size > 0").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def decide(chg: float, vol: float, ampl: float, price: float):
    """盘后动量判定. 返回 (action, amplify_flag)."""
    if price < MIN_PRICE:
        return "below_min_price", False
    if vol < MIN_VOL:
        return "low_volume", False
    amplify = ampl >= MAX_AMPL_PCT
    if chg >= MOM_UP_PCT:
        return "long_candidate", amplify
    if chg <= MOM_DN_PCT:
        return "short_candidate", amplify
    return "no_signal", amplify


def collect(dry: bool) -> int:
    today_et = _today_et()
    n_et = _now_et()
    hhmm = n_et.hour * 100 + n_et.minute
    in_window = 1600 <= hhmm <= 1959  # 盘后窗

    open_syms = _load_open_symbols()
    pool = [c for c in MOVER_POOL if c not in open_syms]  # 排他: 已在仓不重开
    if not pool:
        print(f"[collect] {today_et} 扫描池空")
        return 0

    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    snap_map = {}
    try:
        ret, snap = ctx.get_market_snapshot(pool)
        if ret == RET_OK:
            for _, r in snap.iterrows():
                snap_map[r["code"]] = r
    finally:
        ctx.close()

    n = 0
    for symbol in pool:
        s = snap_map.get(symbol)
        if s is None:
            continue
        after_px = float(s.get("after_price", 0) or 0)
        chg = float(s.get("after_change_rate", 0) or 0)
        vol = float(s.get("after_volume", 0) or 0)
        ampl = float(s.get("after_amplitude", 0) or 0)
        prev_close = float(s.get("prev_close_price", 0) or 0)
        upd = str(s.get("update_time", ""))
        action, amplify = decide(chg, vol, ampl, after_px)
        # 只落有信号的 (candidate/amplify), low_volume/no_signal/below_min 不落盘防噪音
        if action in ("long_candidate", "short_candidate"):
            row = {
                "session": "afterhours",
                "line": "entry_momentum",
                "trade_date": today_et,
                "hhmm_et": hhmm,
                "in_afterhours_window": in_window,
                "symbol": symbol,
                "after_price": after_px,       # 盘后真实价
                "after_change_pct": chg,        # 盘后涨跌幅 (vs 当日收盘)
                "after_volume": vol,
                "after_amplitude": ampl,
                "prev_close": prev_close,
                "update_time": upd,
                "action": action,
                "amplify_flag": amplify,        # 振幅过大=薄量扫价风险标记
                "settled": False,
                "recorded_at": datetime.utcnow().isoformat() + "Z",
            }
            if dry:
                print(f"[dry] {symbol} after={after_px} chg={chg}% vol={vol:.0f} ampl={ampl}% → {action}{'(ampl)' if amplify else ''}")
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
