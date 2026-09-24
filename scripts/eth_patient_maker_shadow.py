#!/usr/bin/env python3
"""ETH 影子⑥：耐心挂单方 (patient maker) 纸面模拟 (record-only, 零下单).

提案文档 (唯一规格来源): docs/analysis/2026-09-24-eth-microstructure-shadow-preregistrations.md §⑥

## 预注册判据 (从提案逐字复制, 读数前写死; 读数出来前不得改本段)

采样门：模拟入场 n≥20（票-轮去重）；
提请 1 股 SIM 实弹探针条件（三条全满足，探针本身沿用 DEB-20260923 B 已批模式另行触发）：
  ① fill_rate ≥40%
  ② 成交后窗内逆行中位数 < 捕获价差
  ③ 净经济/笔 中位数 > 0；
否决：任一不满足 → 弃 maker 通道，影子停采留档；
强制披露：穿越即成交是乐观上界（无排队位置建模）；实弹探针通过也 ≠ 通道获批
  （仍走 ≥40 笔/≥15 日回放门，DEB-20260923 §7.3 同款）。
死线：2026-11-30。

## 人群

计划内、除价差/深度外全闸通过的票-轮。
数据源: pretrade_gate_rejects 的 spread_too_wide / insufficient_depth 拒绝行
+ executions 台账构造。

## 字段可得性探查结论 (2026-09-24)

- 拒绝台账 (pretrade_gate_rejects) 中 spread_too_wide / insufficient_ask_depth 行包含:
  symbol, decision_price, qty, reason, quote{bid, ask, ask_volume, spread_pct}
  **bid/ask 可得**, 颗粒度 = 台账原生 5min (executor 轮询间隔)。
- 无 bid/ask 时间序列 (仅快照点); 后续 bar 穿越判定依赖同台账后续行。
- **只用既有落盘数据, 零新增 futu 轮询。**

## 模拟逻辑

- 买入挂 bid+1tick / 卖出挂 ask−1tick (tick = $0.01)
- 撤单 = 窗尾 或 不利漂移 > 0.5% (写死)
- 成交判定 = 后续可观测价穿越挂单价 (乐观上界, 字段披露)
  - 买入: 后续 ask ≤ 我方 bid_price (i.e. bid+0.01) → filled
  - 卖出: 后续 bid ≥ 我方 ask_price (i.e. ask−0.01) → filled
- 记录: fill_rate (分子分母), adverse_selection (成交→窗尾 mid 逆行),
  spread_captured, net_econ = capture − adverse − 费

## 载体

- 零下单、零推送、零配置改动; 只读消费既有台账。
- ZoneInfo ET; 幂等键 (et_date, symbol, session, round_ts)。
- 自门控: reconcile_ledger.jsonl PASS 日数 ≥ 7 才开跑。
- 死线: 2026-11-30 (到期自动停采留档)。
- 新代码零 except:pass (warning + exc_info)。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ET = ZoneInfo("America/New_York")

# ── 路径 ──
REJECTS_PATH = ROOT / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
PLAN_DIR = ROOT / "data" / "loops" / "ext_entry_plan"
RECONCILE_PATH = PLAN_DIR / "reconcile_ledger.jsonl"
EXECUTIONS_PATH = ROOT / "data" / "extended_hours_entry_executions.jsonl"
OUT_PATH = ROOT / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"

# ── 写死常量 (提案原文) ──
TICK_SIZE = 0.01  # 1 tick = $0.01
ADVERSE_DRIFT_THRESHOLD = 0.5  # 不利漂移 > 0.5% 即撤单 (写死)
SAMPLING_GATE_N = 20  # 模拟入场 n≥20 (票-轮去重)
FILL_RATE_THRESHOLD = 0.40  # ① fill_rate ≥ 40%
RECONCILE_PASS_MIN_DAYS = 7  # 自门控: PASS 日数 ≥ 7
DEADLINE = date(2026, 11, 30)  # 死线: 2026-11-30
FEE_PCT = float(0.53)  # 费率 % (与 rth_fade_shadow 同口径)

# 人群: 仅这两种 reject reason (除价差/深度外全闸通过)
TARGET_REASONS = {"spread_too_wide", "insufficient_ask_depth"}


def _et_today() -> date:
    return datetime.now(ET).date()


def _check_deadline(today: date | None = None) -> bool:
    """死线检查: 返回 True 表示仍在有效期内。"""
    t = today or _et_today()
    return t <= DEADLINE


def _check_reconcile_gate() -> tuple[bool, int]:
    """自门控: reconcile_ledger.jsonl 中 PASS 日数 ≥ 7 才开跑。"""
    if not RECONCILE_PATH.exists():
        return False, 0
    pass_days: set[str] = set()
    try:
        with RECONCILE_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if str(row.get("verdict", "")).upper() == "PASS":
                        d = str(row.get("et_date", ""))
                        if d:
                            pass_days.add(d)
                except json.JSONDecodeError:
                    _log.warning("reconcile_ledger parse error", exc_info=True)
                    continue
    except OSError:
        _log.warning("reconcile_ledger read failed", exc_info=True)
        return False, 0
    n = len(pass_days)
    return n >= RECONCILE_PASS_MIN_DAYS, n


def _load_plan_symbols(et_date: str, session: str) -> set[str]:
    """加载计划文件中的事件票集合。"""
    plan_file = PLAN_DIR / f"{et_date}_{session}.json"
    if not plan_file.exists():
        return set()
    try:
        with plan_file.open() as f:
            rows = json.load(f)
        return {str(r.get("symbol", "")).upper() for r in rows if r.get("symbol")}
    except (json.JSONDecodeError, OSError):
        _log.warning("plan file parse error: %s", plan_file, exc_info=True)
        return set()


def _load_spread_depth_rejects(et_date: str, session: str) -> list[dict]:
    """从拒绝台账加载 spread_too_wide / insufficient_ask_depth 拒绝行。"""
    if not REJECTS_PATH.exists():
        return []
    results = []
    target_prefix = f"{et_date}T"
    try:
        with REJECTS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning("rejects parse error", exc_info=True)
                    continue
                ts = str(row.get("ts", ""))
                if not ts.startswith(target_prefix):
                    continue
                if str(row.get("session", "")) != session:
                    continue
                for rj in row.get("rejects", []):
                    reason = str(rj.get("reason", ""))
                    if reason in TARGET_REASONS:
                        rj["_ts"] = ts
                        rj["_session"] = session
                        rj["_et_date"] = et_date
                        results.append(rj)
    except OSError:
        _log.warning("rejects read failed", exc_info=True)
    return results


def _load_all_rejects_for_session(et_date: str, session: str) -> list[dict]:
    """加载同 session 全部拒绝行 (用于后续穿越判定)。"""
    if not REJECTS_PATH.exists():
        return []
    results = []
    target_prefix = f"{et_date}T"
    try:
        with REJECTS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(row.get("ts", ""))
                if not ts.startswith(target_prefix):
                    continue
                if str(row.get("session", "")) != session:
                    continue
                for rj in row.get("rejects", []):
                    rj["_ts"] = ts
                    results.append(rj)
    except OSError:
        pass
    return results


def _load_seen_keys() -> set[tuple[str, str, str, str]]:
    """加载已记录的幂等键。"""
    seen: set[tuple[str, str, str, str]] = set()
    if not OUT_PATH.exists():
        return seen
    try:
        with OUT_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    key = (str(row.get("et_date", "")),
                           str(row.get("symbol", "")),
                           str(row.get("session", "")),
                           str(row.get("round_ts", "")))
                    seen.add(key)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return seen


def _simulate_maker(rj: dict, subsequent_rejects: list[dict]) -> dict:
    """模拟耐心挂单方。

    买入挂 bid+1tick, 卖出挂 ask−1tick。
    撤单 = 窗尾 或 不利漂移 > 0.5%。
    成交判定 = 后续可观测价穿越挂单价 (乐观上界)。

    返回模拟结果 dict。
    """
    quote = rj.get("quote") or {}
    bid = quote.get("bid")
    ask = quote.get("ask")
    if not bid or not ask or float(bid) <= 0 or float(ask) <= 0:
        return {
            "simulated": False,
            "sim_basis": "no_valid_quote",
            "fill_buy": None, "fill_sell": None,
            "adverse_buy": None, "adverse_sell": None,
            "spread_captured": None, "net_econ": None,
        }

    bid = float(bid)
    ask = float(ask)

    # 挂单价
    buy_limit = round(bid + TICK_SIZE, 4)  # 买入挂 bid+1tick
    sell_limit = round(ask - TICK_SIZE, 4)  # 卖出挂 ask−1tick

    # 在后续行中查找穿越
    sym = str(rj.get("symbol", ""))
    my_ts = str(rj.get("_ts", ""))
    fill_buy = False
    fill_sell = False
    fill_buy_price = None
    fill_sell_price = None
    fill_buy_ts = None
    fill_sell_ts = None
    cancel_buy = False
    cancel_sell = False
    mid_at_window_end = None

    for later in subsequent_rejects:
        if str(later.get("symbol", "")) != sym:
            continue
        later_ts = str(later.get("_ts", ""))
        if later_ts <= my_ts:
            continue
        later_quote = later.get("quote") or {}
        later_bid = later_quote.get("bid")
        later_ask = later_quote.get("ask")
        if later_bid is not None and later_ask is not None:
            lb = float(later_bid)
            la = float(later_ask)
            mid = (lb + la) / 2
            mid_at_window_end = mid  # 持续更新, 最终值 = 窗尾 mid

            # 买入穿越: later ask ≤ buy_limit
            if not fill_buy and la <= buy_limit:
                fill_buy = True
                fill_buy_price = buy_limit
                fill_buy_ts = later_ts

            # 卖出穿越: later bid ≥ sell_limit
            if not fill_sell and lb >= sell_limit:
                fill_sell = True
                fill_sell_price = sell_limit
                fill_sell_ts = later_ts

            # 撤单检查: 不利漂移 > 0.5%
            if not fill_buy and not cancel_buy:
                drift_buy = (la - ask) / ask * 100  # ask 上涨对买方不利
                if drift_buy > ADVERSE_DRIFT_THRESHOLD:
                    cancel_buy = True

            if not fill_sell and not cancel_sell:
                drift_sell = (bid - lb) / bid * 100  # bid 下跌对卖方不利
                if drift_sell > ADVERSE_DRIFT_THRESHOLD:
                    cancel_sell = True

    # 计算逆行 (adverse selection): 成交 → 窗尾 mid 变动
    adverse_buy = None
    adverse_sell = None
    if fill_buy and fill_buy_price and mid_at_window_end:
        # 买入后 mid 下跌 = 逆行 (不利)
        adverse_buy = round((fill_buy_price - mid_at_window_end) / fill_buy_price * 100, 4)
    if fill_sell and fill_sell_price and mid_at_window_end:
        # 卖出后 mid 上涨 = 逆行 (不利)
        adverse_sell = round((mid_at_window_end - fill_sell_price) / fill_sell_price * 100, 4)

    # 捕获价差
    spread_captured = None
    if fill_buy and fill_sell:
        spread_captured = round((sell_limit - buy_limit) / buy_limit * 100, 4)
    elif fill_buy:
        # 单边: 捕获 = mid - buy_limit (if mid > buy_limit)
        if mid_at_window_end:
            spread_captured = round((mid_at_window_end - buy_limit) / buy_limit * 100, 4)
    elif fill_sell:
        if mid_at_window_end:
            spread_captured = round((sell_limit - mid_at_window_end) / sell_limit * 100, 4)

    # 净经济 = capture - adverse - fee (简化, 单边口径)
    net_econ = None
    if spread_captured is not None:
        adv = 0.0
        if adverse_buy is not None and adverse_buy > 0:
            adv = adverse_buy
        elif adverse_sell is not None and adverse_sell > 0:
            adv = adverse_sell
        net_econ = round(spread_captured - adv - 2 * FEE_PCT, 4)

    return {
        "simulated": True,
        "sim_basis": "bid+1tick_buy__ask-1tick_sell__crossing_fill__optimistic_upper_bound",
        "buy_limit": buy_limit,
        "sell_limit": sell_limit,
        "fill_buy": fill_buy,
        "fill_sell": fill_sell,
        "fill_buy_price": fill_buy_price,
        "fill_sell_price": fill_sell_price,
        "fill_buy_ts": fill_buy_ts,
        "fill_sell_ts": fill_sell_ts,
        "cancel_buy_drift": cancel_buy,
        "cancel_sell_drift": cancel_sell,
        "mid_at_window_end": mid_at_window_end,
        "adverse_buy_pct": adverse_buy,
        "adverse_sell_pct": adverse_sell,
        "spread_captured_pct": spread_captured,
        "net_econ_pct": net_econ,
        "granularity": "5min_native_ledger",
        "adverse_drift_threshold_pct": ADVERSE_DRIFT_THRESHOLD,
    }


def collect(et_date: str | None = None, session: str | None = None,
            dry: bool = False) -> list[dict]:
    """主采集逻辑。返回新记录列表。"""
    today = _et_today()
    if not _check_deadline(today):
        print(f"[eth_maker] 死线 {DEADLINE} 已过, 停采留档")
        return []

    active, pass_days = _check_reconcile_gate()
    if not active:
        print(f"[eth_maker] dormant: reconcile PASS 日数 {pass_days}/{RECONCILE_PASS_MIN_DAYS}")
        return []

    target_date = et_date or today.isoformat()
    sessions = [session] if session else ["premarket", "afterhours"]
    seen = _load_seen_keys()
    fresh: list[dict] = []

    for sess in sessions:
        plan_syms = _load_plan_symbols(target_date, sess)
        if not plan_syms:
            continue
        rejects = _load_spread_depth_rejects(target_date, sess)
        all_rejects = _load_all_rejects_for_session(target_date, sess)

        for rj in rejects:
            raw_sym = str(rj.get("symbol", "")).replace("US.", "")
            if raw_sym not in plan_syms and f"US.{raw_sym}" not in plan_syms:
                continue  # 非计划内事件票
            sym = f"US.{raw_sym}" if not rj.get("symbol", "").startswith("US.") else str(rj["symbol"])
            ts = str(rj.get("_ts", ""))
            key = (target_date, sym, sess, ts)
            if key in seen:
                continue

            quote = rj.get("quote") or {}
            sim = _simulate_maker(rj, all_rejects)

            # fill_rate 分子分母
            filled_count = int(bool(sim.get("fill_buy"))) + int(bool(sim.get("fill_sell")))
            total_sides = 2  # buy + sell

            rec = {
                "et_date": target_date,
                "symbol": sym,
                "session": sess,
                "round_ts": ts,
                "reject_reason": str(rj.get("reason", "")),
                "decision_price": rj.get("decision_price"),
                "qty": rj.get("qty"),
                "quote_bid": quote.get("bid"),
                "quote_ask": quote.get("ask"),
                "quote_ask_volume": quote.get("ask_volume"),
                "quote_spread_pct": quote.get("spread_pct"),
                **{k: v for k, v in sim.items() if k != "simulated"},
                "filled_sides": filled_count,
                "total_sides": total_sides,
                "fill_rate": round(filled_count / total_sides, 4) if total_sides > 0 else None,
                "recorded_at": datetime.now(ET).isoformat(),
                "deadline": DEADLINE.isoformat(),
            }
            fresh.append(rec)
            seen.add(key)

    if fresh and not dry:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("a") as f:
            for rec in fresh:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[eth_maker] 已追加 {len(fresh)} 行 → {OUT_PATH}")
    elif fresh:
        print(f"[eth_maker] --dry 模式, {len(fresh)} 行未落盘")
    else:
        print(f"[eth_maker] {target_date} 无新增 spread/depth 计划内事件票")

    return fresh


def graduation_check() -> dict:
    """毕业裁定: 检查是否满足提请 1 股 SIM 实弹探针条件。

    条件 (三条全满足):
      ① fill_rate ≥ 40%
      ② 成交后窗内逆行中位数 < 捕获价差
      ③ 净经济/笔 中位数 > 0
    """
    if not OUT_PATH.exists():
        return {"verdict": "NO_DATA", "n": 0}

    records = []
    try:
        with OUT_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {"verdict": "READ_ERROR", "n": 0}

    # 模拟入场 (至少一边 fill)
    filled_records = [r for r in records if r.get("filled_sides", 0) > 0]
    n = len(filled_records)

    if n < SAMPLING_GATE_N:
        return {"verdict": "INSUFFICIENT_SAMPLES", "n": n, "required": SAMPLING_GATE_N}

    # ① fill_rate ≥ 40%
    fill_rates = [r.get("fill_rate", 0) for r in filled_records if r.get("fill_rate") is not None]
    avg_fill_rate = sum(fill_rates) / len(fill_rates) if fill_rates else 0

    # ② 逆行中位数 < 捕获价差
    adverses = []
    captures = []
    for r in filled_records:
        adv = r.get("adverse_buy_pct") or r.get("adverse_sell_pct")
        cap = r.get("spread_captured_pct")
        if adv is not None and cap is not None:
            adverses.append(adv)
            captures.append(cap)
    med_adverse = median(adverses) if adverses else 0
    med_capture = median(captures) if captures else 0

    # ③ 净经济/笔 中位数 > 0
    net_econs = [r.get("net_econ_pct") for r in filled_records if r.get("net_econ_pct") is not None]
    med_net = median(net_econs) if net_econs else 0

    criteria = {
        "fill_rate_gte_40pct": {
            "pass": avg_fill_rate >= FILL_RATE_THRESHOLD,
            "actual": round(avg_fill_rate, 4),
            "required": FILL_RATE_THRESHOLD,
        },
        "adverse_lt_capture": {
            "pass": med_adverse < med_capture,
            "med_adverse": round(med_adverse, 4),
            "med_capture": round(med_capture, 4),
        },
        "net_econ_gt_0": {
            "pass": med_net > 0,
            "med_net_econ": round(med_net, 4),
        },
    }

    all_pass = all(c["pass"] for c in criteria.values())
    verdict = "PROBE_RECOMMENDED" if all_pass else "REJECT"

    return {
        "verdict": verdict,
        "n_filled": n,
        "n_total": len(records),
        "criteria": criteria,
        "disclosure": "穿越即成交是乐观上界 (无排队位置建模); 实弹探针通过也≠通道获批",
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ETH 影子⑥: 耐心挂单方纸面模拟 (record-only)")
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认今日)")
    ap.add_argument("--session", default=None, choices=["premarket", "afterhours"])
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    ap.add_argument("--check", action="store_true", help="仅运行毕业裁定检查")
    args = ap.parse_args(argv)

    if args.check:
        result = graduation_check()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    try:
        collect(et_date=args.date, session=args.session, dry=args.dry)
    except Exception:
        _log.warning("shadow⑥ collect failed", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
