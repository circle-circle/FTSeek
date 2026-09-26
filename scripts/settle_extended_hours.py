#!/usr/bin/env python3
"""延长时段统一结算器 (2026-09-17, record-only, 用户批准).

结算对象: 两条在役延长时段影子线的决策点 jsonl
  - premarket_exit_shadow.jsonl      (盘前 exit: lock_in/tighten)
  - afterhours_exit_shadow.jsonl     (盘后 exit: lock_in/tighten)
(2026-09-17 清理: 原第三源 afterhours_momentum_shadow.jsonl 已移除 — 该线同日被用户裁停(方向错:
 价格动量≠事件确定性), 且从未产出过任何行(data/ 下无该文件), 属死源. 脚本本体保留归档,
 见 scripts/afterhours_momentum_shadow.py 头部停用说明; 历史结算产物零该源行, 清理无数据损失.)

设计要点 (用户 9/17 明确):
  1. 多时点结算 — 不只看次日, 对每个决策点记录一串观测点的价格路径:
       T+0 开盘(09:30) / T+0 收盘(16:00) / T+1 盘前(次09:25) / T+1 收盘 / T+2 收盘
     → 回答"该建议在不同持有尺度下分别对不对".
  2. 翻转分析 — 若当时建议的方向短期亏损, 继续持有多久能翻红? 翻转所需天数/最大回撤.
     → 回答"亏损是暂时回撤还是方向错了", 支撑"留空间不硬扛"的哲学.
  3. 收益最大化/最低止损 — 用 5m K 线还原决策后的最优可达成路径(MFE)与最劣(MAE),
     对照"若照影子做"的退出点, 量化"早卖/晚卖/死扛"三种选择的损益差.

零行为变更: 只读 jsonl + 拉历史K线 + 写结算报告 json, 不改影子文件不交易.
产物: data/extended_hours_settle_latest.json (atomic write) + 每行 settle 回填独立 jsonl.

用法: PYTHONPATH=. venv/bin/python scripts/settle_extended_hours.py [--days N] [--dry]
挂载: crontab 北京 06:30 每日 (盘前窗后, 当日K线已完整). 回滚=删 crontab + 删脚本.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
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
SETTLE_OUT = ROOT / "data" / "extended_hours_settle.jsonl"
REPORT = ROOT / "data" / "extended_hours_settle_latest.json"

SOURCES = [
    ("premarket_exit", ROOT / "data" / "premarket_exit_shadow.jsonl"),
    ("afterhours_exit", ROOT / "data" / "afterhours_exit_shadow.jsonl"),
]

# 观测点: 决策后第 N 个交易日 (0=当日)
HOLD_HORIZONS = [0, 1, 2, 3, 5]   # 当日/次日/3日/5日收盘
FLIP_MAX_DAYS = 5                  # 翻转分析最长看 5 日


def _dir_sign(direction: str) -> int:
    return -1 if str(direction or "").upper() in ("SHORT", "SELL_SHORT", "SELL") else 1


def _dir_from_action(action: str) -> str:
    """entry 影子行从 action 推方向."""
    if "short" in action:
        return "SHORT"
    return "LONG"


def _load_rows():
    """读全部影子行, 标 source/line. 返回 list of dict (含 _source)."""
    out = []
    for source, path in SOURCES:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict) or not r.get("symbol"):
                continue
            r["_source"] = source
            out.append(r)
    return out


def _in_source_window(row):
    source = str(row.get("_source") or row.get("source") or "")
    try:
        hhmm = int(row.get("hhmm_et") or 0)
    except (TypeError, ValueError):
        return False
    if source == "premarket_exit":
        return bool(row.get("in_premarket_window")) and 400 <= hhmm <= 929
    if source == "afterhours_exit":
        return bool(row.get("in_afterhours_window")) and 1600 <= hhmm <= 1959
    return False


def _settled_record_in_window(record):
    source = str(record.get("source") or "")
    try:
        hhmm = int(record.get("hhmm_et") or 0)
    except (TypeError, ValueError):
        return False
    if source == "premarket_exit":
        return 400 <= hhmm <= 929
    if source == "afterhours_exit":
        return 1600 <= hhmm <= 1959
    return False


def _already_settled():
    """已结算键 (source,symbol,trade_date,hhmm) 防重复."""
    if not SETTLE_OUT.exists():
        return set()
    s = set()
    for line in SETTLE_OUT.read_text().splitlines():
        try:
            r = json.loads(line)
            s.add((r.get("source"), r.get("symbol"), r.get("trade_date"), r.get("hhmm_et")))
        except Exception:
            continue
    return s


class _KlineUnavailable(Exception):
    """历史K线接口不可用(限频/连接). 该行保持未结算, 下次重跑补 — 绝不静默当"无数据"."""


_KLINE_MIN_INTERVAL = float(os.getenv("SETTLE_KLINE_MIN_INTERVAL", "0.6"))
_KLINE_RETRIES = int(os.getenv("SETTLE_KLINE_RETRIES", "3"))
_last_kline_ts = [0.0]
_day_bars_cache = {}
_intraday_cache = {}


def _kline_call(ctx, code, ktype, start, end, max_count):
    """限频 + 重试的历史K线请求. 返回 kl (DataFrame, 可为空表).

    2026-09-17 实测根因: 富途 OpenD 历史K线有请求预算(约 60 次/30 秒), 超限后**整体拒绝**
    且错误文本为 None. 原实现 `ret, kl, _ = ...; if ret != RET_OK: return []` 把"拒绝"当成
    "无数据" ⇒ 结算循环跑到约第 30 行(≈60 次调用)后, **后续行全部静默返回 None 永不结算**。
    实测证据: 颠倒 SOURCES 顺序后变成"盘后 30 行 OK、其后 119 盘后 + 138 盘前全 None"
    ⇒ 故障随累计调用数走、与数据源无关。故: 调用间隔 ≥ _KLINE_MIN_INTERVAL, 失败按 3/6/9s
    退避重试, 仍失败则抛 _KlineUnavailable (由 main 计为 api_unavailable, 下轮重跑补)。
    """
    from futu import RET_OK
    for attempt in range(_KLINE_RETRIES):
        wait = _KLINE_MIN_INTERVAL - (time.time() - _last_kline_ts[0])
        if wait > 0:
            time.sleep(wait)
        _last_kline_ts[0] = time.time()
        try:
            ret, kl, _msg = ctx.request_history_kline(
                code, ktype=ktype, start=start, end=end, max_count=max_count)
        except Exception:
            ret, kl = -1, None
        if ret == RET_OK and kl is not None:
            return kl
        time.sleep(3 * (attempt + 1))
    raise _KlineUnavailable(f"{code} {start}")


def _day_bars(ctx, symbol, start, days):
    """拉 symbol 从 start 起 days 个日K. 记忆化: 同(标的,起始,长度)只请求一次.

    376 行影子数据只对应 15 个唯一 (symbol, trade_date) 对 ⇒ 记忆化把 ~752 次 K线调用
    降到 ~30 次, 这本身就落在限频预算内。
    """
    key = (symbol, start, days)
    if key in _day_bars_cache:
        return _day_bars_cache[key]
    from futu import KLType
    end = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=days + 10)).strftime("%Y-%m-%d")
    kl = _kline_call(ctx, symbol, KLType.K_DAY, start, end, days + 10)
    bars = []
    if len(kl) > 0:
        for _, r in kl.iterrows():
            bars.append({
                "date": str(r["time_key"])[:10],
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
            })
    _day_bars_cache[key] = bars
    return bars


def _intraday_path(ctx, symbol, date_str):
    """当日 5m K 线, 用于 MFE/MAE 路径还原. 返回 (highs, lows, closes) 或 None(确实无数据).

    记忆化同 (标的,日期). 接口不可用会抛 _KlineUnavailable (区别于"确实没有5m数据")。
    """
    key = (symbol, date_str)
    if key in _intraday_cache:
        return _intraday_cache[key]
    from futu import KLType
    kl = _kline_call(ctx, symbol, KLType.K_5M, date_str, date_str, 200)
    if len(kl) == 0:
        _intraday_cache[key] = None
        return None
    highs = [float(r["high"]) for _, r in kl.iterrows()]
    lows = [float(r["low"]) for _, r in kl.iterrows()]
    closes = [float(r["close"]) for _, r in kl.iterrows()]
    _intraday_cache[key] = (highs, lows, closes)
    return highs, lows, closes


def settle_row(r, ctx):
    """对单条影子行结算. 返回 settle dict 或 None(数据不足)."""
    symbol = r["symbol"]
    trade_date = r.get("trade_date")
    if not trade_date:
        return None
    # 决策点价格 (延长时段当时价)
    decision_px = r.get("last_price") or r.get("after_price")
    if not decision_px or decision_px <= 0:
        return None
    # 方向: exit 线用 direction 字段, entry 线从 action 推
    direction = r.get("direction") or _dir_from_action(r.get("action", ""))
    sign = _dir_sign(direction)
    qty = r.get("qty") or 0

    bars = _day_bars(ctx, symbol, trade_date, max(HOLD_HORIZONS) + 1)
    if not bars:
        return None
    # T+0 当日 bar
    d0 = bars[0]

    settle = {
        "decision_price": decision_px,
        "direction": direction,
        # T+0 开盘对照 (决策后第一个定价时刻)
        "t0_open": d0["open"],
        "t0_open_ret_pct": round((d0["open"] - decision_px) / decision_px * 100 * sign, 3),
        "t0_close": d0["close"],
        "t0_close_ret_pct": round((d0["close"] - decision_px) / decision_px * 100 * sign, 3),
    }
    # 多时点持有收益 (若照决策方向持有 N 日收盘)
    holdout = {}
    for h in HOLD_HORIZONS:
        if h < len(bars):
            b = bars[h]
            holdout[f"d{h}_close_ret_pct"] = round((b["close"] - decision_px) / decision_px * 100 * sign, 3)
    settle["holdout"] = holdout

    # MFE/MAE 当日路径 (5m): 决策后最优/最劣可达成 (基线=决策价, 与 trade_records 口径同源)
    #   MFE = max(high-decision_px)/px (顺向最大偏移); MAE = min(low-decision_px)/px (逆向最大偏移)
    path = _intraday_path(ctx, symbol, trade_date)
    if path:
        highs, lows, closes = path
        mfe = max((h - decision_px) for h in highs) / decision_px * 100 * sign
        mae = min((l - decision_px) for l in lows) / decision_px * 100 * sign
        settle["mfe_pct"] = round(mfe, 3)
        settle["mae_pct"] = round(mae, 3)
        # 捕获率 (capture_pct): T+0 收盘实际拿到的 / MFE 峰值可拿到的
        #   >1=收盘超峰(收盘=新高), <0=回吐成亏, 0~1=正常兑现率. 对齐 trade_records 口径.
        t0c = settle["t0_close_ret_pct"]
        settle["capture_pct"] = round(t0c / mfe * 100, 1) if mfe > 0 else None
        # MFE 达成时点 (决策后第几根5m bar 到峰 → 分钟)
        try:
            peak_i = max(range(len(highs)), key=lambda i: (highs[i] - decision_px) * sign)
            settle["time_to_mfe_min"] = (peak_i + 1) * 5
        except Exception:
            settle["time_to_mfe_min"] = None

    # 翻转分析: 若 T+0 收盘浮亏, 后续几日内是否翻红 (收盘口径)
    flip = {"initial_loss": False, "flipped": False, "flip_day": None, "max_dd_pct": 0.0}
    if settle["t0_close_ret_pct"] < 0:
        flip["initial_loss"] = True
        worst = settle["t0_close_ret_pct"]
        for i, b in enumerate(bars[1:FLIP_MAX_DAYS + 1], start=1):
            ret = (b["close"] - decision_px) / decision_px * 100 * sign
            worst = min(worst, ret)
            if ret > 0 and not flip["flipped"]:
                flip["flipped"] = True
                flip["flip_day"] = i
                break
        flip["max_dd_pct"] = round(worst, 3)
    settle["flip"] = flip
    return settle


def _aggregate(rows):
    """聚合成时段收益台账."""
    agg = {}
    for r in rows:
        s = r.get("settle")
        if not s:
            continue
        key = (r.get("_source"), r.get("action"))
        a = agg.setdefault(key, {"n": 0, "t0_close": [], "holdout_best": [], "flips": 0, "initial_losses": 0, "mfe": [], "capture": []})
        a["n"] += 1
        a["t0_close"].append(s["t0_close_ret_pct"])
        if s.get("mfe_pct") is not None:
            a["mfe"].append(s["mfe_pct"])
        if s.get("capture_pct") is not None:
            a["capture"].append(s["capture_pct"])
        f = s.get("flip", {})
        if f.get("initial_loss"):
            a["initial_losses"] += 1
            if f.get("flipped"):
                a["flips"] += 1
        # 最优持有尺度
        ho = s.get("holdout", {})
        if ho:
            a["holdout_best"].append(max(ho.values()))
    # 汇总 (含 MFE 捕获率/翻转率/最优持有尺度 — 用户 9/17 要求的成熟指标)
    out = {}
    for (source, action), a in agg.items():
        n = a["n"]
        caps = a["capture"]
        entry = {
            "n": n,
            # T+0 收盘均值 (当日兑现)
            "avg_t0_close_ret": round(sum(a["t0_close"]) / n, 3),
            # 最优持有尺度均值 (多时点里最好的那个 — 回答"该拿多久")
            "avg_best_holdout_ret": round(sum(a["holdout_best"]) / len(a["holdout_best"]), 3) if a["holdout_best"] else None,
            # MFE/捕获率 (决策后峰值可达成 vs 实际兑现 — 回答"卖早了吗")
            "avg_mfe": round(sum(a["mfe"]) / len(a["mfe"]), 3) if a["mfe"] else None,
            "avg_capture_pct": round(sum(caps) / len(caps), 1) if caps else None,
            # 翻转 (初始浮亏后翻红比例 — 回答"亏损是暂时还是方向错")
            "initial_loss_n": a["initial_losses"],
            "flip_rate": round(a["flips"] / a["initial_losses"] * 100, 1) if a["initial_losses"] else None,
        }
        out[f"{source}:{action}"] = entry
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="只结算最近N日决策点")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    rows = _load_rows()
    if not rows:
        print("[settle] 无影子行")
        return
    done = _already_settled()
    cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    settled_rows = []
    n_new = 0
    n_api_unavailable = 0
    n_out_of_window = 0
    try:
        for r in rows:
            if not _in_source_window(r):
                n_out_of_window += 1
                continue
            if r.get("trade_date", "") < cutoff:
                continue
            key = (r.get("_source"), r.get("symbol"), r.get("trade_date"), r.get("hhmm_et"))
            if key in done:
                # 已结算: 从 settle jsonl 读回 (用于台账聚合)
                continue
            try:
                s = settle_row(r, ctx)
            except _KlineUnavailable:
                # K线接口不可用: 该行保持未结算 ⇒ 下轮重跑自动补. 计数进报告, 不静默.
                n_api_unavailable += 1
                continue
            if s:
                r["settle"] = s
                settled_rows.append(r)
                if not args.dry:
                    with SETTLE_OUT.open("a") as f:
                        rec = {"source": r["_source"], "symbol": r["symbol"],
                               "trade_date": r["trade_date"], "hhmm_et": r["hhmm_et"],
                               "action": r.get("action"), "settle": s}
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_new += 1
    finally:
        ctx.close()

    # 聚合 = 历史已结算 + 本轮新结算
    all_settled = []
    historical_out_of_window = 0
    if SETTLE_OUT.exists():
        for line in SETTLE_OUT.read_text().splitlines():
            try:
                record = json.loads(line)
            except Exception:
                continue
            if _settled_record_in_window(record):
                all_settled.append(record)
            else:
                historical_out_of_window += 1
    # 台账用 source:action 聚合
    ledger_src = []
    for rec in all_settled:
        rec["_source"] = rec.get("source")
        ledger_src.append(rec)
    ledger = _aggregate(ledger_src)

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "newly_settled": n_new,
        "total_settled": len(all_settled),
        "out_of_window_skipped": n_out_of_window,
        "historical_out_of_window_excluded": historical_out_of_window,
        # 接口不可用而未结算的行数 (>0 说明台账本轮有缺口, 下轮重跑自动补)
        "api_unavailable": n_api_unavailable,
        "ledger": ledger,
    }
    # ── B21/B22 判据 n 轴计数锚 (2026-09-26 登记; 零行为变更, 纯只读统计) ──
    # 为什么放这里而不是让注册表数 jsonl 行数: 两条判据的 n 轴都是**执行类 action 子集**
    # (B21 且需已结算 / B22 且需 quote_age 非空), 而源 jsonl 5739 行里 89% 是
    # hold_no_stop (亏损侧按 9-17 裁决本不执行) ⇒ count_mode=lines 会让 current_n
    # 立刻虚假达标迁 READY_TO_EVALUATE (gap_chase_gate 同型假象)。
    # 注册表用 count_mode="json_key:<本处键名>" 取轴, 见 shadow_registry._count_samples。
    _EXEC_B21 = frozenset({"SL_BREAKEVEN", "SL_TRAIL_LOCK", "TAKE_PROFIT",
                           "lock_in", "tighten", "breakeven_lock"})
    # B22 判据文另含 pre_close_flatten (与 B21 集合刻意不同, 差一个值 — 逐字照判据,
    # 不做"顺手统一"; 改集合 = 改判据 = 重裁)
    _EXEC_B22 = _EXEC_B21 | {"pre_close_flatten"}
    report["b21_exec_settled_n"] = sum(
        1 for _r in all_settled if str(_r.get("action") or "") in _EXEC_B21)
    report["b22_exec_aged_n"] = sum(
        1 for _r in rows
        if str(_r.get("action") or "") in _EXEC_B22
        and _r.get("quote_age_seconds") is not None)
    # 只印不判: 两条判据的判决权在 eval/裁决日 (min_n=20), 本处只给计数与既有读数,
    # 不写 verdict — 避免"报告里写死方向性结论随数据翻转把裁决带反" (9/23 实锤)。
    report["b22_aged_by_bucket"] = {
        ">120s": sum(1 for _r in rows
                     if str(_r.get("action") or "") in _EXEC_B22
                     and _r.get("quote_age_seconds") is not None
                     and float(_r["quote_age_seconds"]) > 120),
        "<=30s": sum(1 for _r in rows
                     if str(_r.get("action") or "") in _EXEC_B22
                     and _r.get("quote_age_seconds") is not None
                     and float(_r["quote_age_seconds"]) <= 30),
    }
    if args.dry:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"[settle] 新结算 {n_new} 行, API不可用跳过 {n_api_unavailable} 行(下轮重跑补), "
              f"窗外跳过 {n_out_of_window} 行, 历史窗外排除 {historical_out_of_window} 行, "
              f"累计 {len(all_settled)}, 台账 → {REPORT}")


if __name__ == "__main__":
    main()
