#!/usr/bin/env python3
"""盘前重定价 shadow (2026-08-18, record-only, 用户批准方案1)。

问题: 隔夜仓的 SL/be 入场后整夜静态 — 盘前利好不抬 be、盘前利空不收紧,
利润保护要等开盘 9:30 才恢复。本 shadow 记录"盘前价信号 → 重定价建议",
次轮运行用次日完整日 K 结算实际结局, 攒样裁决"盘前动态重定价是否有正期望"。

与既有 overnight_hold_shadow 的分工 (互补非重复):
  - overnight_hold_shadow: 问"benign 退出该不该改持仓过夜" (退出决策反事实)
  - 本 shadow: 问"继续持有的仓, 盘前该不该重定价" (持有中仓的保护线动态化)

决策规则 (常量带注释, 裁决后可配置化):
  pre_ret_signed = 盘前涨跌幅 × 方向符号 (正=有利)
  - ≥ +1.0%  → be_raise   建议抬保本线至 入场价±净费用线 (净口径, 对齐 7c4b1f7f)
  - ≤ −2.0%  → tighten    建议开盘观察收紧 SL (记录, 不定义具体幅度)
  - 其余     → hold       维持现 SL (broker 硬止损单是主保护)
  - 盘前无成交 (pre_price=0) → no_data/hold, 不算信号

结算 (同脚本次轮运行): trade_date < 今日 ET 的行, 用该日完整日 K 算
  gap / 全日收益 / 路径下界(×方向), be_raise 行另算 be 线是否被触及。
实际结局 join trade_records (exit_reason/pnl_net)。

用法: PYTHONPATH=. venv/bin/python scripts/premarket_reprice_shadow.py [--dry]
挂载: crontab 北京 21:02 (ET 09:02, 盘前数据已足 + 开盘前), 交易日 gate。
回滚: 删 crontab 行 + 删本脚本 (jsonl 保留供分析)。
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
OUTPUT = ROOT / "data" / "premarket_reprice_shadow.jsonl"

# ── 决策阈值 (8/18 初值, 攒样后裁决) ──────────────────────────────
BE_RAISE_AT_PRE_RET = 1.0    # 盘前顺向 ≥+1% → 建议抬 be 线
TIGHTEN_AT_PRE_RET = -2.0    # 盘前逆向 ≤−2% → 建议收紧观察


def _today_et() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().strftime("%Y-%m-%d")
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")


def _is_trading_day() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return True  # fail-open: gate 故障不误拦记录


def _dir_sign(direction: str) -> int:
    return -1 if str(direction).upper() in ("SHORT", "SELL_SHORT") else 1


def _fee_pct(qty: float, price: float, direction: str) -> float:
    """往返费用占比 (净口径 be 线用, 对齐 fee_model.estimate_round_trip)。"""
    try:
        from app.execution.fee_model import estimate_round_trip
        fees = estimate_round_trip(qty, price, price, direction)
        notional = qty * price
        return fees["total"] / notional if notional > 0 else 0.004
    except Exception:
        return 0.004  # fail-safe: 0.4% 上限近似 (历史大仓档实测 0.086%, 保守)


def _be_line_price(entry: float, direction: str, fee_pct: float) -> float:
    sign = _dir_sign(direction)
    return round(entry * (1 + sign * fee_pct), 4)


def decide(pre_ret_signed: float) -> str:
    if pre_ret_signed >= BE_RAISE_AT_PRE_RET:
        return "be_raise"
    if pre_ret_signed <= TIGHTEN_AT_PRE_RET:
        return "tighten"
    return "hold"


# ── 采集 ──────────────────────────────────────────────────────────
def _load_overnight_positions(today_et: str):
    """隔夜仓清单 = trade_records 未平仓且入场日 < 今日 ET (权威 ET 源)。

    ⚠️ 不用 open_positions.entry_time 做隔夜判定: 它是 CST 本地时间 (无时区后缀),
    ET 15:55 入场 = CST 次日 03:55 → CST 日期=ET+1, 会把隔夜仓漏判为当日仓
    (8/18 实弹踩坑: UBER entry 22:50 CST)。trade_records.entry_time 带 ET 时区,
    date() 过滤即正确口径。SL/仓位参数再从 open_positions join。
    返回行: (symbol, direction, qty, entry_price, current_sl, entry_time, signal_id, entry_date_et)
    """
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT t.symbol, t.direction, t.quantity, t.entry_price, "
            "       COALESCE(o.current_sl, 0), t.entry_time, t.decision_id, "
            "       date(t.entry_time) "
            "FROM trade_records t "
            "LEFT JOIN open_positions o ON o.symbol = t.symbol AND o.size > 0 "
            "WHERE t.exit_time IS NULL AND date(t.entry_time) < ?",
            (today_et,)).fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


def _seen_keys():
    if not OUTPUT.exists():
        return set()
    seen = set()
    for line in OUTPUT.read_text().splitlines():
        try:
            r = json.loads(line)
            seen.add((r.get("symbol"), r.get("trade_date")))
        except Exception:
            continue
    return seen


def collect(today_et: str, dry: bool) -> int:
    from futu import OpenQuoteContext, RET_OK
    positions = _load_overnight_positions(today_et)
    if not positions:
        print(f"[collect] {today_et} 无隔夜仓")
        return 0

    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    try:
        ret, snap = ctx.get_market_snapshot([p[0] for p in positions])
        snap_map = {}
        if ret == RET_OK:
            for _, r in snap.iterrows():
                snap_map[r["code"]] = r
    finally:
        ctx.close()

    seen = _seen_keys()
    rows, n_new = [], 0
    for symbol, direction, qty, entry, sl, entry_time, signal_id, entry_date_et in positions:
        if (symbol, today_et) in seen:
            continue  # 唯一键去重 (防轮询放大)
        s = snap_map.get(symbol)
        pre_px = float(s.get("pre_price", 0) or 0) if s is not None else 0.0
        prev_close = float(s.get("prev_close_price", 0) or 0) if s is not None else 0.0
        sign = _dir_sign(direction)
        if pre_px > 0 and prev_close > 0:
            pre_ret_signed = round((pre_px - prev_close) / prev_close * 100 * sign, 3)
            action = decide(pre_ret_signed)
        else:
            pre_ret_signed, action = None, "no_data"
        fee_pct = _fee_pct(qty, entry, direction)
        rows.append({
            "trade_date": today_et,            # 决策作用日 = 当日 session
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry_price": entry,
            "current_sl": sl,
            "signal_id": signal_id,
            "entry_time": entry_time,
            "entry_date_et": entry_date_et,    # 结算 join 权威键 (ET 口径)
            "prev_close": prev_close,
            "pre_price": pre_px,
            "pre_ret_signed": pre_ret_signed,
            "action": action,
            "be_line_price": _be_line_price(entry, direction, fee_pct) if action == "be_raise" else None,
            "fee_pct": round(fee_pct, 5),
            "settled": False,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        })
        n_new += 1

    if dry:
        for r in rows:
            print(f"[dry] {r['symbol']} pre={r['pre_ret_signed']} → {r['action']}")
        return n_new

    if rows:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[collect] {today_et}: {n_new} 条新记录 "
          f"({sum(1 for r in rows if r['action']=='be_raise')} be_raise / "
          f"{sum(1 for r in rows if r['action']=='tighten')} tighten / "
          f"{sum(1 for r in rows if r['action']=='hold')} hold / "
          f"{sum(1 for r in rows if r['action']=='no_data')} no_data)")
    return n_new


# ── 结算 ──────────────────────────────────────────────────────────
def settle(today_et: str, dry: bool) -> int:
    """trade_date < 今日 ET 的未结算行 → 用该日完整日 K 结算。"""
    if not OUTPUT.exists():
        return 0
    lines = OUTPUT.read_text().splitlines()
    pending = []
    for i, line in enumerate(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("settled") and r.get("trade_date") and r["trade_date"] < today_et:
            pending.append(i)
    if not pending:
        print("[settle] 无待结算行")
        return 0

    from futu import OpenQuoteContext, RET_OK, KLType
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    # 实际结局 (trade_records)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    n_done = 0
    try:
        for i in pending:
            r = json.loads(lines[i])
            d = r["trade_date"]
            try:
                ret, kl, _page = ctx.request_history_kline(
                    r["symbol"], ktype=KLType.K_DAY, start=d, end=d, max_count=2)
                if ret != RET_OK or kl is None or len(kl) == 0:
                    continue  # 该日无 K 线 (停牌/未收盘), 留待下轮
                bar = kl.iloc[0]
                o, h, lo, c = (float(bar["open"]), float(bar["high"]),
                               float(bar["low"]), float(bar["close"]))
                pc = r.get("prev_close") or 0
                if pc <= 0:
                    continue
                sign = _dir_sign(r["direction"])
                r["settle"] = {
                    "gap_pct": round((o - pc) / pc * 100 * sign, 3),
                    "day_ret_pct": round((c - pc) / pc * 100 * sign, 3),
                    "path_worst_pct": round((lo - pc) / pc * 100 * sign, 3) if sign > 0
                                      else round((h - pc) / pc * 100 * sign, 3),
                }
                if r.get("be_line_price"):
                    be = r["be_line_price"]
                    r["settle"]["be_touched"] = bool(lo <= be) if sign > 0 else bool(h >= be)
                # 实际结局: 该仓最终退出 (symbol + ET入场日匹配, entry_date_et 权威键)
                entry_date = r.get("entry_date_et") or str(r.get("entry_time", ""))[:10]
                tr = conn.execute(
                    "SELECT exit_reason, ROUND(pnl_net,2), ROUND(pnl_pct*100,2), "
                    "       substr(COALESCE(exit_time,'OPEN'),1,16) "
                    "FROM trade_records WHERE symbol=? AND date(entry_time)=? "
                    "ORDER BY entry_time DESC LIMIT 1",
                    (r["symbol"], entry_date)).fetchone()
                if tr:
                    r["settle"]["actual_exit_reason"] = tr[0] or "OPEN"
                    r["settle"]["actual_pnl_net"] = tr[1]
                    r["settle"]["actual_pnl_pct"] = tr[2]
                r["settled"] = True
                lines[i] = json.dumps(r, ensure_ascii=False)
                n_done += 1
            except Exception as e:
                print(f"[settle] {r.get('symbol')} {d} 失败: {e}")
    finally:
        ctx.close()
        conn.close()

    if not dry and n_done:
        OUTPUT.write_text("\n".join(lines) + "\n")
    print(f"[settle] 结算 {n_done} 条")
    return n_done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()

    today_et = _today_et()
    if not _is_trading_day():
        print(f"[skip] {today_et} 非交易日")
        return
    settle(today_et, args.dry)
    if not args.collect_only:
        collect(today_et, args.dry)


if __name__ == "__main__":
    main()
