"""A2 前置实测: Futu SIM 盘对条件单 (STOP/STOP_LIMIT/TRAILING_STOP) 的受理性.

DEB-20260805(二) A2 终裁 (2026-08-05): 2026-06-27 旧结论"SIM 不支持 STOP"
基于 SDK 早期版本; 本脚本在 SDK 10.3.6308 下重测受理性。
安全设计: qty=1, 触发价远离市价 (SELL STOP @ $1, 绝无触发可能), 下单后立即撤单。
"""
import json

from futu import (
    OpenSecTradeContext, OpenQuoteContext, TrdMarket, TrdEnv, TrdSide,
    OrderType, ModifyOrderOp, RET_OK,
)

HOST, PORT, CODE = "127.0.0.1", 11111, "US.AAPL"
results = {}


def main():
    # 0) 先取现价作参照
    try:
        qctx = OpenQuoteContext(host=HOST, port=PORT)
        ret, data = qctx.get_market_snapshot([CODE])
        last_price = float(data.iloc[0]["last_price"]) if ret == RET_OK else None
        qctx.close()
    except Exception as e:
        last_price = f"ERR:{e}"
    results["ref_last_price"] = last_price

    tctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=HOST, port=PORT, is_encrypt=False)
    try:
        # 1) 定位 SIM 账户
        ret, acc = tctx.get_acc_list()
        acc_id = None
        if ret == RET_OK:
            sim_rows = acc[acc["trd_env"] == "SIMULATE"]
            if not sim_rows.empty:
                acc_id = int(sim_rows.iloc[0]["acc_id"])
        results["sim_acc_id"] = acc_id
        if acc_id is None:
            results["fatal"] = "no SIM account found"
            return

        cases = [
            # 首测 STOP(price=1.0) 被拒 "下单缺少必要参数触发价格" → 触发价走 aux_price 重试
            ("STOP_aux", dict(order_type=OrderType.STOP, price=1.0, aux_price=1.0)),
        ]
        for name, extra in cases:
            entry = {}
            order_id = None
            try:
                ret, data = tctx.place_order(
                    qty=1, code=CODE, trd_side=TrdSide.SELL,
                    trd_env=TrdEnv.SIMULATE, acc_id=acc_id, **extra,
                )
                entry["ret"] = int(ret)
                if ret == RET_OK and data is not None and not data.empty:
                    order_id = str(data.iloc[0].get("order_id", ""))
                    entry["order_id"] = order_id
                    entry["accepted"] = True
                    entry["order_status"] = str(data.iloc[0].get("order_status", ""))
                else:
                    entry["accepted"] = False
                    entry["msg"] = str(data)[:300]
            except TypeError as e:
                entry["accepted"] = False
                entry["error"] = f"TypeError: {e}"
            except Exception as e:
                entry["accepted"] = False
                entry["error"] = str(e)[:300]
            # 立即撤单
            if order_id:
                try:
                    cret, cdata = tctx.modify_order(
                        ModifyOrderOp.CANCEL, order_id=order_id, qty=1, price=1.0,
                        trd_env=TrdEnv.SIMULATE, acc_id=acc_id,
                    )
                    entry["cancel_ret"] = int(cret)
                    entry["cancel_msg"] = str(cdata)[:200]
                except Exception as e:
                    entry["cancel_error"] = str(e)[:200]
            results[name] = entry
    finally:
        tctx.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        print(json.dumps(results, ensure_ascii=False, indent=1))
