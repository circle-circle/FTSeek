#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""富途通道「活体可用性」探针 (只读, 2026-09-20).

为什么需要它: 能力利用率审计里"某通道未接入"有两种完全不同的原因 —
  (a) SDK 里没这个接口 (真缺能力) 或本机权限拿不到数据 (真不可用);
  (b) 接口其实好用, 只是没接线/没订阅 (接线问题, 不是能力问题).
只看 grep "0 调用" 会把 (b) 误当 (a), 进而在错误前提上做取舍。
本脚本用**实弹调用**把两者分开: 每个通道 PASS = 本机 OpenD 此刻真能取到数据。

只读约束: 只调查询类接口; 订阅仅在**本探针自己的 context** 内 (quit 即释放);
不落库、不下单、不改配置、不碰 data/*.jsonl。REAL 侧只做 history_deal_list_query
+ order_fee_query 两个查询。

用法:
  ./venv/bin/python scripts/futu_capability_live_probe.py            # 默认: 全通道 + REAL 费用金标准
  ./venv/bin/python scripts/futu_capability_live_probe.py --no-real  # 跳过 REAL 账户查询
  ./venv/bin/python scripts/futu_capability_live_probe.py --json     # 只输出 JSON

产出: data/futu_capability_probe_latest.json (矩阵 + 逐项证据)
退出码: 0 = 全部核心通道 PASS; 1 = 有核心通道 FAIL (含 SDK 缺方法/权限不足/超时)
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "futu_capability_probe_latest.json"
HOST, PORT = "127.0.0.1", 11111
PROBE_SYMBOL = "US.NVDA"

# 核心通道: FAIL 即视为"能力不可用", 影响退出码
CORE = {
    "get_capital_distribution", "get_capital_flow", "get_rt_ticker", "get_rt_data",
    "get_option_chain", "get_option_underlying_his_volatility", "get_stock_filter",
    "acctradinginfo_query", "get_margin_ratio", "order_fee_query(REAL)",
}


def _rec(rows: List[Dict[str, Any]], name: str, status: str, detail: str,
         core: Optional[bool] = None) -> None:
    rows.append({
        "channel": name,
        "status": status,                      # PASS / FAIL / EXPECTED_UNAVAILABLE / NO_DATA
        "core": CORE.__contains__(name) if core is None else core,
        "detail": str(detail)[:600],
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-real", action="store_true", help="跳过 REAL 账户查询 (只验行情/能力面)")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from futu import (OpenQuoteContext, OpenSecTradeContext, TrdMarket, TrdEnv,
                      SecurityFirm, RET_OK, Market, SimpleFilter, SortDir,
                      StockField, SubType, OrderType)

    rows: List[Dict[str, Any]] = []
    ctx_ids: List[str] = []

    def log(*a):
        if not args.json:
            print(*a)

    qt = OpenQuoteContext(host=HOST, port=PORT)
    trd = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=HOST, port=PORT,
                              security_firm=SecurityFirm.FUTUSECURITIES)

    # ── 0. SDK 方法面 (缺方法 = 真缺能力) ─────────────────────────
    sdk = {}
    for m in ("get_capital_distribution", "get_capital_flow", "get_rt_ticker",
              "get_rt_data", "get_option_chain", "get_option_volatility",
              "get_option_underlying_his_volatility", "get_stock_filter",
              "get_market_snapshot", "request_history_kline"):
        sdk[m] = hasattr(qt, m)
    for m in ("acctradinginfo_query", "get_margin_ratio", "order_fee_query",
              "history_deal_list_query"):
        sdk[m] = hasattr(trd, m)
    log("== SDK 方法存在性 ==")
    for k, v in sdk.items():
        log(f"  {'✓' if v else '✗'} {k}")

    # ── 1. 资金面 ────────────────────────────────────────────────
    try:
        ret, d = qt.get_capital_distribution(PROBE_SYMBOL)
        ok = ret == RET_OK and not d.empty
        _rec(rows, "get_capital_distribution", "PASS" if ok else "FAIL",
             f"ret={ret} row={d.iloc[0].to_dict() if ok else str(d)[:200]}")
    except Exception as e:
        _rec(rows, "get_capital_distribution", "FAIL", f"{type(e).__name__}: {e}")

    try:
        ret, d = qt.get_capital_flow(PROBE_SYMBOL)
        ok = ret == RET_OK and not d.empty
        _rec(rows, "get_capital_flow", "PASS" if ok else "FAIL",
             f"ret={ret} n={len(d) if ok else 'NA'} cols={list(d.columns) if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "get_capital_flow", "FAIL", f"{type(e).__name__}: {e}")

    # ── 2. 推送型接口 (先订阅, 后取数; quit 释放) ─────────────────
    for st, needle in ((SubType.TICKER, "get_rt_ticker"), (SubType.RT_DATA, "get_rt_data")):
        try:
            ret, msg = qt.subscribe([PROBE_SYMBOL], st)
            if ret == RET_OK:
                ctx_ids.append(PROBE_SYMBOL)
            else:
                log(f"  subscribe {st} 失败: {msg}")
        except Exception as e:
            log(f"  subscribe {st} 异常: {e}")

    try:
        ret, d = qt.get_rt_ticker(PROBE_SYMBOL, num=5)
        ok = ret == RET_OK and not d.empty
        _rec(rows, "get_rt_ticker", "PASS" if ok else "FAIL",
             f"ret={ret} n={len(d) if ok else 'NA'} last={d.iloc[-1].to_dict() if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "get_rt_ticker", "FAIL", f"{type(e).__name__}: {e}")

    try:
        ret, d = qt.get_rt_data(PROBE_SYMBOL)
        ok = ret == RET_OK and not d.empty
        _rec(rows, "get_rt_data", "PASS" if ok else "FAIL",
             f"ret={ret} n={len(d) if ok else 'NA'}")
    except Exception as e:
        _rec(rows, "get_rt_data", "FAIL", f"{type(e).__name__}: {e}")

    # ── 3. 期权面 (链窗口 ≤30 天; 标的级 IV 走 underlying 接口) ───
    opt_code = None
    try:
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=20)).isoformat()
        ret, d = qt.get_option_chain(PROBE_SYMBOL, start=start, end=end)
        ok = ret == RET_OK and len(d) > 0
        if ok:
            opt_code = str(d.iloc[0]["code"])
        _rec(rows, "get_option_chain", "PASS" if ok else "FAIL",
             f"window={start}~{end} ret={ret} n={len(d) if ok else 'NA'} "
             f"sample={d.iloc[0].to_dict() if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "get_option_chain", "FAIL", f"{type(e).__name__}: {e}")

    if opt_code:
        try:
            ret, d = qt.get_option_volatility(opt_code)
            ok = ret == RET_OK and not d.empty
            _rec(rows, "get_option_volatility(合约级)", "PASS" if ok else "FAIL",
                 f"code={opt_code} ret={ret} n={len(d) if ok else 'NA'}", core=False)
        except Exception as e:
            _rec(rows, "get_option_volatility(合约级)", "FAIL", f"{type(e).__name__}: {e}", core=False)

    try:
        ret, d, _pk = qt.get_option_underlying_his_volatility(PROBE_SYMBOL)
        ok = ret == RET_OK and not d.empty
        _rec(rows, "get_option_underlying_his_volatility", "PASS" if ok else "FAIL",
             f"ret={ret} n={len(d) if ok else 'NA'} cols={list(d.columns) if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "get_option_underlying_his_volatility", "FAIL", f"{type(e).__name__}: {e}")

    # ── 4. 选股器 (121 个 StockField; 注意字段名与 App 侧不同) ────
    try:
        f = SimpleFilter()
        f.stock_field = StockField.CUR_PRICE
        f.filter_min = 1
        f.is_no_filter = False
        f.sort = SortDir.DESCEND
        ret, d = qt.get_stock_filter(market=Market.US, filter_list=[f], num=3)
        ok = ret == RET_OK
        # 返回 (last_page, all_count, stock_list); stock_list 在本 SDK 版本是 list[dict]
        lst = d[2] if ok else []
        first = lst[0] if isinstance(lst, list) and lst else None
        if first is None:
            cols = None
        elif hasattr(first, "__dict__"):
            cols = sorted(vars(first).keys())[:10]
        elif hasattr(first, "columns"):
            cols = list(first.columns)[:10]
        else:
            cols = type(first).__name__
        _rec(rows, "get_stock_filter", "PASS" if ok else "FAIL",
             f"ret={ret} us_all_count={d[1] if ok else 'NA'} got={len(lst) if ok else 'NA'} "
             f"fields={cols if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "get_stock_filter", "FAIL", f"{type(e).__name__}: {e}")

    # ── 5. 交易账户面 (SIM) ─────────────────────────────────────
    try:
        ret, d = trd.acctradinginfo_query(order_type=OrderType.NORMAL, code=PROBE_SYMBOL,
                                          price=100.0, trd_env=TrdEnv.SIMULATE)
        ok = ret == RET_OK and not d.empty
        _rec(rows, "acctradinginfo_query", "PASS" if ok else "FAIL",
             f"env=SIM ret={ret} row={d.iloc[0].to_dict() if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "acctradinginfo_query", "FAIL", f"{type(e).__name__}: {e}")

    try:
        ret, d = trd.get_margin_ratio(code_list=[PROBE_SYMBOL])
        ok = ret == RET_OK and not d.empty
        _rec(rows, "get_margin_ratio", "PASS" if ok else "FAIL",
             f"ret={ret} row={d.iloc[0].to_dict() if ok else str(d)[:150]}")
    except Exception as e:
        _rec(rows, "get_margin_ratio", "FAIL", f"{type(e).__name__}: {e}")

    # ── 6. 费用面: SIM 结构性不可得 (设计如此), REAL 金标准 ───────
    try:
        ret, d = trd.history_deal_list_query(trd_env=TrdEnv.SIMULATE,
                                             start="2026-08-01", end=date.today().isoformat())
        if ret == RET_OK:
            _rec(rows, "history_deal_list_query(SIM)", "PASS", f"n={len(d)}", core=False)
        else:
            _rec(rows, "history_deal_list_query(SIM)", "EXPECTED_UNAVAILABLE",
                 f"模拟交易不支持成交数据 (DEB-20260730 层1 已裁决, est 为 SIM 期唯一口径): {d}", core=False)
    except Exception as e:
        _rec(rows, "history_deal_list_query(SIM)", "FAIL", f"{type(e).__name__}: {e}", core=False)

    fee_cmp = None
    if not args.no_real:
        try:
            ret, deals = trd.history_deal_list_query(trd_env=TrdEnv.REAL, start="2026-08-01",
                                                     end=date.today().isoformat())
            if ret != RET_OK:
                _rec(rows, "history_deal_list_query(REAL)", "FAIL", str(deals)[:300])
            else:
                us = deals[deals["code"].astype(str).str.startswith("US.")] if "code" in deals.columns else deals
                oids, seen = [], set()
                for _, r in us.iterrows():
                    o = str(r.get("order_id", "")).strip()
                    if o and o not in seen:
                        seen.add(o)
                        oids.append(o)
                _rec(rows, "history_deal_list_query(REAL)", "PASS",
                     f"total={len(deals)} US_deals={len(us)} unique_orders={len(oids)}", core=False)
                if not oids:
                    _rec(rows, "order_fee_query(REAL)", "NO_DATA",
                         "REAL 侧无近月美股权成交通道已验证但无样本 (切实盘后自动有料)")
                else:
                    r2, fees = trd.order_fee_query(order_id_list=oids[:50], trd_env=TrdEnv.REAL)
                    if r2 != RET_OK or len(fees) == 0:
                        _rec(rows, "order_fee_query(REAL)", "FAIL", f"ret={r2} {str(fees)[:200]}")
                    else:
                        from app.execution.fee_model import estimate_fee
                        fx = {str(r["order_id"]): r for _, r in fees.iterrows()}
                        legs: Dict[str, List[Any]] = {}
                        for _, r in us.iterrows():
                            legs.setdefault(str(r["order_id"]), []).append(r)
                        deltas = []
                        for oid, rs in legs.items():
                            if oid not in fx:
                                continue
                            qty = sum(float(x["qty"]) for x in rs)
                            amt = sum(float(x["qty"]) * float(x["price"]) for x in rs)
                            if qty <= 0:
                                continue
                            side = "SELL" if any(str(x.get("trd_side", "")).upper().startswith("SELL") for x in rs) else "BUY"
                            tdate = str(rs[0].get("create_time", ""))[:10] or None
                            est = float(estimate_fee(qty, amt / qty, side, tdate)["total"])
                            act = float(fx[oid]["fee_amount"])
                            deltas.append({"order_id": oid, "code": str(rs[0].get("code")),
                                           "qty": qty, "side": side, "est": round(est, 4),
                                           "actual": round(act, 4), "delta": round(act - est, 4)})
                        within = sum(1 for x in deltas if abs(x["delta"]) <= 0.02)
                        fee_cmp = {"n": len(deltas), "within_2c": within,
                                   "max_abs_delta": max((abs(x["delta"]) for x in deltas), default=0.0),
                                   "rows": deltas[:30]}
                        _rec(rows, "order_fee_query(REAL)", "PASS",
                             f"n_orders={len(deltas)} 模型vs实收 ±$0.02 内={within}/{len(deltas)} "
                             f"max|Δ|=${fee_cmp['max_abs_delta']:.4f}")
        except Exception as e:
            _rec(rows, "order_fee_query(REAL)", "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

    qt.close()
    trd.close()

    fails = [r for r in rows if r["core"] and r["status"] == "FAIL"]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "probe_symbol": PROBE_SYMBOL,
        "sdk_methods": sdk,
        "channels": rows,
        "core_failures": [r["channel"] for r in fails],
        "fee_model_vs_real": fee_cmp,
        "verdict": "ALL_CORE_PASS" if not fails else "CORE_FAIL",
    }
    try:
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    except Exception as e:
        log(f"  (产物写入失败: {e})")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        log("\n== 通道矩阵 ==")
        for r in rows:
            log(f"  [{r['status']:<21}] {r['channel']}: {r['detail'][:120]}")
        log(f"\n verdict={payload['verdict']}  core_failures={payload['core_failures']}")
        log(f" 产物: {OUT}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())