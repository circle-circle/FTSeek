#!/usr/bin/env python3
"""实盘最小额探针 (默认只读; 下单需显式确认) —— 切实盘前补齐"代码取证不到"的三项:

  1. REAL 账户真实购买力/融资参数 (acctradinginfo_query) —— `--probe buying-power` (只读)
  2. STOP 单在 US 实盘的真实语义 (受理/触发/有效期) —— `--probe stop-order` (下单, 需确认)
  3. 成交确认延迟分布 —— 由上面两个探针的时间戳自行记录 (下单→终态耗时)

硬闸 (fail-close, 任一不满足即退出):
  - 下单类探针必须: TradingMode=live 且 `--confirm-real-order <REAL_ACCOUNT_ID>` 与 .env.us
    的 REAL_ACCOUNT_ID 完全一致 (防止误在模拟/错误账户上跑)
  - qty 恒为 1 股; 止损价必须距现价 ≥15% (保证**不可能成交**), 且下单后立即撤单
  - 冻结期内不接生产链路: 本脚本**独立进程**, 不写 trade_records / open_positions / 审计 jsonl

用法:
  PYTHONPATH=. venv/bin/python scripts/real_order_probe.py --probe buying-power
  PYTHONPATH=. venv/bin/python scripts/real_order_probe.py --probe stop-order \
      --symbol US.NVDA --stop-price 150 --confirm-real-order <REAL_ACCOUNT_ID>
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _slow_print(tag, obj):
    print(f"[{tag}] {obj}")


def probe_buying_power():
    from app.core.trading_mode import TradingMode
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK, OrderType

    tm = TradingMode()
    for env in ("sim", "real"):
        acc = tm.account_id(env)
        if not acc:
            _slow_print("skip", f"{env}: 账户号未配置")
            continue
        ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                  port=int(os.getenv("FUTU_OPD_PORT", "11111")),
                                  security_firm=SecurityFirm.FUTUSECURITIES, is_encrypt=False)
        try:
            te = TrdEnv.REAL if env == "real" else TrdEnv.SIMULATE
            r, d = ctx.accinfo_query(trd_env=te, acc_id=acc)
            if r == RET_OK and not d.empty:
                row = d.iloc[0]
                _slow_print(env, {k: row[k] for k in row.index if k in (
                    "power", "max_power_short", "cash", "total_assets", "securities_assets",
                    "market_val", "currency", "usd_assets", "hkd_assets")})
            else:
                _slow_print(env, f"accinfo_query FAILED ret={r} {str(d)[:120]}")
            r2, d2 = ctx.acctradinginfo_query(order_type=OrderType.NORMAL, code="US.NVDA", price=200.0,
                                              trd_env=te, acc_id=acc)
            _slow_print(f"{env}·tradinginfo", (d2.iloc[0].to_dict() if r2 == RET_OK and not d2.empty
                                               else f"FAILED ret={r2} {str(d2)[:120]}"))
        finally:
            ctx.close()


def probe_stop_order(symbol: str, stop_price: float, confirm: str):
    from app.core.trading_mode import TradingMode
    from futu import (OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, OrderType, TrdSide,
                      TimeInForce, Session, RET_OK)

    tm = TradingMode()
    real_acc = tm.account_id("real")
    if not tm.is_live:
        raise SystemExit("❌ 拒绝: TradingMode 非实盘 (需 .env.us AUTO_TRADE_ACCOUNT=real + ALLOW_REAL_TRADING=true)")
    if not real_acc or str(confirm) != str(real_acc):
        raise SystemExit(f"❌ 拒绝: --confirm-real-order 未与 REAL_ACCOUNT_ID 一致 (期望账户 {real_acc})")

    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                              port=int(os.getenv("FUTU_OPD_PORT", "11111")),
                              security_firm=SecurityFirm.FUTUSECURITIES, is_encrypt=False)
    try:
        # 现价用行情上下文 (交易上下文无常价接口)
        from futu import OpenQuoteContext
        q = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                             port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        rq, ds = q.get_market_snapshot([symbol])
        last = float(ds.iloc[0]["last_price"]) if rq == RET_OK and not ds.empty else 0.0
        q.close()
        if last <= 0:
            raise SystemExit("❌ 拒绝: 现价不可得 (fail-close)")
        gap = (last - stop_price) / last
        if gap < 0.15:
            raise SystemExit(f"❌ 拒绝: stop={stop_price} 距现价 {last} 仅 {gap:.1%} (<15%) — 探针要求不可能成交")

        t0 = time.time()
        ret, data = ctx.place_order(price=stop_price, qty=1, code=symbol, trd_side=TrdSide.SELL,
                                    order_type=OrderType.STOP, trd_env=TrdEnv.REAL, acc_id=real_acc,
                                    time_in_force=TimeInForce.DAY, remark="real_probe_stop")
        _slow_print("place_stop", f"ret={ret} accept_s={time.time()-t0:.2f} {str(data)[:200]}")
        if ret != RET_OK:
            _slow_print("verdict", "STOP 单**不被受理** (记录原始错误, 据此决定实盘止损主从关系)")
            return
        oid = str(data["order_id"].values[0]) if data is not None and not data.empty else ""
        for i in range(6):
            time.sleep(1.0)
            rq2, dq = ctx.order_list_query(order_id=oid, trd_env=TrdEnv.REAL, acc_id=real_acc)
            st = str(dq.iloc[0].get("order_status")) if rq2 == RET_OK and not dq.empty else "?"
            _slow_print(f"status+{i+1}s", st)
            if "SUBMITTED" in st or "FILLED" in st or "FAIL" in st or "CANCEL" in st:
                break
        rc, dc = ctx.modify_order(modify_order_op=__import__("futu").ModifyOrderOp.CANCEL,
                                  order_id=oid, qty=0, price=0, trd_env=TrdEnv.REAL, acc_id=real_acc)
        _slow_print("cancel", f"ret={rc} {str(dc)[:160]}")
        rq3, dq3 = ctx.order_list_query(order_id=oid, trd_env=TrdEnv.REAL, acc_id=real_acc)
        _slow_print("final", str(dq3.iloc[0].get("order_status")) if rq3 == RET_OK and not dq3.empty else "?")
        _slow_print("verdict", "已撤单; 请把上面的时序 (受理/状态推进/撤单延迟) 抄进 docs 分析, 不要凭记忆")
    finally:
        ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", choices=["buying-power", "stop-order"], required=True)
    ap.add_argument("--symbol", default="US.NVDA")
    ap.add_argument("--stop-price", type=float, default=0.0)
    ap.add_argument("--confirm-real-order", default="")
    a = ap.parse_args()
    if a.probe == "buying-power":
        probe_buying_power()
    else:
        if not a.stop_price:
            raise SystemExit("❌ 需要 --stop-price")
        probe_stop_order(a.symbol, a.stop_price, a.confirm_real_order)


if __name__ == "__main__":
    main()