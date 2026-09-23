#!/usr/bin/env python3
"""ETH 开空探针 (延长时段卖空可行性, DEB-20260923 §4.6 / §7.3)。

**唯一硬未知**: 延长时段 (`session=Session.ETH`) 用 `TrdSide.SELL` 开空能不能成交。
已实证 (2026-09-23, 只读): SIM 账户 = 融资融券模拟账户 (acc_type=MARGIN /
sim_acc_type=STOCK_AND_OPTION); 延长时段**买**单 6 笔 FILLED_ALL (session=ETH);
券源可逐票预检 (`acctradinginfo_query.max_sell_short` / `get_margin_ratio.is_short_permit`);
开空 = 官方语义 `TrdSide.SELL` (`SELL_SHORT` 只在查询/回报侧); 借券费 3.0/年。
**未证**: 上述两者叠加 —— 历史延长时段空单 0 笔 (trade_records 里 premarket/afterhours 9 行全 LONG)。

## 预注册判据 (2026-09-23 执行前写死, 读数不得事后改)

- 规模: **1 股**, 限价 (marketable) 卖单, `remark="eth_probe"` (**绝不用 ext_entry** —
  否则会被入场执行器的 active_ext_entry_order 闸当成自己在途单, 正是 P1 事故的机制)。
- 标的序 (执行前只读预检确认有券, 2026-09-23 读数):
  US.CBRL (max_sell_short 3535) → US.PAYX (1404) → US.CTAS (808) → US.FUL (3148) → US.MANU (7840);
  每只都必须当轮 `max_sell_short >= 1` 且 `is_short_permit=True`, 否则顺延。
- 时段: 必须落在延长时段窗内 (premarket 04:00-09:30 ET / afterhours 16:00-20:00 ET), 窗外拒跑。
- 模式: 必须 SIM (`is_live=False`) + `futu_trd_env=SIMULATE` + `acc_id=19930823`; 否则硬退出 (不下单)。
- 成功判据 (任一即"机制通过"): ① 挂单被接受 (非拒单码) 且 60s 内 `FILLED_ALL`; ② 被接受且账户出现负持仓 (-1 股)。
- 失败判据: 拒单 / 自动 `CANCELLED_ALL` / 60s 无成交且撤单后无负持仓 ⇒ 记 `eth_short_unsupported`。
- 收尾纪律: 成交即**立即买回平掉**(1 股限价), 并回读 position_list 确认空仓归零; 未成交即撤单。
- 频次: 每 ET 日最多 **2 次** (盘前一次 + 盘后一次), 之后无论结果停止 (防试探性重试)。
- 结论用途: 探针通过 **≠** 空头通道获批 (仍需 ≥40 笔/≥15 交易日回放 + 小额实跑门槛, DEB §7.3)。

用法:
  venv/bin/python scripts/probe_eth_short_open.py            # 只读 dry-run (默认)
  venv/bin/python scripts/probe_eth_short_open.py --execute  # 真下 1 股 (需用户显式批准)
"""
from __future__ import print_function

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CANDIDATES = ["US.CBRL", "US.PAYX", "US.CTAS", "US.FUL", "US.MANU"]
ACC_ID = 19930823
REMARK = "eth_probe"
LEDGER = os.path.join(ROOT, "data", "eth_short_probe.jsonl")
PRICE_CAP_USD = 400.0          # 单股价格上限 (防选到异常标的把名义额放大)
WAIT_FILL_S = 60
MAX_ATTEMPTS_PER_DAY = 2


def _et_now():
    """ET 当前时间 (用项目时区工具, 禁手写 -4/-5 偏移)。"""
    try:
        from app.utils.timezone import to_et
        return to_et(datetime.now(timezone.utc))
    except Exception as exc:
        print("[probe] ET 时区工具不可用: %s: %s" % (type(exc).__name__, exc))
        return None


def _in_eth_window(et_dt):
    """延长时段窗内 (盘前 04:00-09:30 / 盘后 16:00-20:00 ET, 工作日)。"""
    if et_dt is None or et_dt.weekday() > 4:
        return False, "non_trading_day"
    hhmm = et_dt.hour * 100 + et_dt.minute
    if 400 <= hhmm < 930:
        return True, "premarket"
    if 1600 <= hhmm < 2000:
        return True, "afterhours"
    return False, "outside_eth_window(%04d ET)" % hhmm


def _resolve_trd_env(mode):
    """取券商环境串: `TradingMode.futu_trd_env` 是**方法**(非属性), 漏调括号会得到
    '<bound method ...>' ⇒ 误判非 SIM ⇒ fail-closed 拒跑 (2026-09-23 活体测试实测)。"""
    raw = mode.futu_trd_env
    if callable(raw):
        raw = raw()
    return str(getattr(raw, "name", raw)).upper()


def _mode_ok(is_live, trd_env):
    """必须 SIM: 实盘一律拒跑 (本探针禁止触达真实账户)。"""
    return (not is_live) and str(trd_env).upper() == "SIMULATE"


def _price_ok(px):
    return isinstance(px, (int, float)) and 0 < float(px) <= PRICE_CAP_USD


def _quote_ok(bid, ask):
    """报价有效性: 防"上一时段残留的空/倒挂报价"被判成"标的不可空" (归因污染)。"""
    return (isinstance(bid, (int, float)) and isinstance(ask, (int, float))
            and float(bid) > 0 and float(ask) >= float(bid) and _price_ok(ask))


def _attempts_today(et_date_str):
    """当日 (ET 日) 已执行次数, 失败/成功都算 — 防试探性重试。"""
    n = 0
    if os.path.exists(LEDGER):
        with open(LEDGER) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    print("[probe] 台账行解析失败, 跳过计数")
                    continue
                if str(row.get("et", ""))[:10] == et_date_str:
                    n += 1
    return n


def _write_ledger(row):
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        print("[probe] 台账写入失败: %s: %s" % (type(exc).__name__, exc))


def _select_candidate(tctx, qctx, TrdEnv, OrderType, ret_ok, log=print):
    """按预注册顺序选标: 券源 max_sell_short>=1 + is_short_permit + 价格合理。返回 dict 或 None。

    `ret_ok` 由调用方传入 (futu 符号只在 main 内导入, 模块级函数不得引用)。
    """
    for sym in CANDIDATES:
        ret_ti, ti = tctx.acctradinginfo_query(order_type=OrderType.NORMAL, code=sym, price=1.0,
                                               trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        shortable = int(ti.iloc[0].get("max_sell_short", 0)) if ret_ti == ret_ok and not ti.empty else 0
        ret_mr, mr = tctx.get_margin_ratio([sym])
        permit = bool(mr.iloc[0].get("is_short_permit")) if ret_mr == ret_ok and not mr.empty else False
        ret_s, snap = qctx.get_market_snapshot([sym])
        last = bid = ask = None
        if ret_s == ret_ok and not snap.empty:
            last = float(snap.iloc[0].get("last_price") or 0)
            bid = float(snap.iloc[0].get("bid_price") or 0)
            ask = float(snap.iloc[0].get("ask_price") or 0)
        log("[probe] %s shortable=%s permit=%s last=%s bid=%s ask=%s" % (sym, shortable, permit, last, bid, ask))
        if shortable >= 1 and permit and _price_ok(last) and _quote_ok(bid, ask):
            return {"code": sym, "shortable": shortable, "last": last, "bid": bid, "ask": ask}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真下 1 股 (默认只读 dry-run)")
    ap.add_argument("--select-only", action="store_true", help="只跑到选标 (只读, 暂不执行)")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env.us"), override=True)
    from futu import (OpenQuoteContext, OpenSecTradeContext, TrdEnv, TrdMarket,  # noqa: E402
                      SecurityFirm, TrdSide, OrderType, TimeInForce, Session, RET_OK, ModifyOrderOp)

    et_dt = _et_now()
    in_win, session = _in_eth_window(et_dt)
    print("[probe] ET=%s session=%s in_window=%s" % (et_dt, session, in_win))

    tctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111,
                               security_firm=SecurityFirm.FUTUSECURITIES)
    qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    result = {"ts_cst": datetime.now().isoformat(timespec="seconds"),
              "et": str(et_dt), "session": session, "execute": bool(args.execute)}
    try:
        from app.core.trading_mode import TradingMode
        mode = TradingMode()
        env_str = _resolve_trd_env(mode)
        result["mode"] = {"is_live": bool(mode.is_live), "trd_env": env_str, "account_id": ACC_ID}
        print("[probe] mode:", result["mode"])
        if not _mode_ok(mode.is_live, env_str):
            result["verdict"] = "abort_not_sim"
            print("[probe] 非 SIM 模式 ⇒ 硬退出, 不下单")
            return
        if not in_win and not args.select_only:
            result["verdict"] = "abort_outside_window"
            return
        if _attempts_today(str(et_dt)[:10]) >= MAX_ATTEMPTS_PER_DAY:
            result["verdict"] = "abort_daily_attempt_cap"
            print("[probe] 当日已达 %d 次上限 ⇒ 停" % MAX_ATTEMPTS_PER_DAY)
            return

        # 前态
        ret_p, pos = tctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        ret_o, orders = tctx.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        result["pre_positions"] = [] if ret_p != RET_OK else [
            {"code": r["code"], "qty": float(r["qty"])} for _, r in pos.iterrows()]
        result["pre_open_orders"] = [] if ret_o != RET_OK else [
            {"code": r["code"], "side": str(r["trd_side"]), "status": str(r["order_status"])}
            for _, r in orders.iterrows()
            if str(r["order_status"]).upper() in ("SUBMITTED", "SUBMITTING", "WAITING_SUBMIT", "FILLED_PART")]
        print("[probe] 前态持仓:", result["pre_positions"])
        print("[probe] 前态在途:", result["pre_open_orders"])

        # 选标的 (券源 precheck)
        pick = _select_candidate(tctx, qctx, TrdEnv, OrderType, RET_OK)
        result["pick"] = pick
        if pick is None:
            result["verdict"] = "no_eligible_candidate"
            print("[probe] 无合格候选 (券源/权限/价格) ⇒ 本期无结论")
            return
        if args.select_only:
            result["verdict"] = "select_only_ok"
            print("[probe] --select-only: 选到 %s (未下单)" % pick["code"])
            return

        price = pick["ask"] if _price_ok(pick["ask"]) else pick["last"]
        leg = {"price": price, "qty": 1}
        if not args.execute:
            result["verdict"] = "dry_run_ok"
            result["leg"] = leg
            print("[probe] DRY-RUN: 将下 SELL 1 @ %s (session=ETH, remark=%s); 加 --execute 才真下" % (price, REMARK))
            return

        ret, data = tctx.place_order(price=price, qty=1, code=pick["code"], trd_side=TrdSide.SELL,
                                     order_type=OrderType.NORMAL, trd_env=TrdEnv.SIMULATE,
                                     acc_id=ACC_ID, time_in_force=TimeInForce.DAY,
                                     session=Session.ETH, remark=REMARK)
        result["leg"] = leg
        result["place_ret"] = int(ret)
        result["place_msg"] = str(data)[:200]
        print("[probe] place_order ret=%s msg=%s" % (ret, str(data)[:160]))
        if ret != RET_OK:
            result["verdict"] = "rejected"
            _write_ledger(result)
            return
        order_id = str(data["order_id"].values[0])
        result["order_id"] = order_id

        status, dealt = "UNKNOWN", 0.0
        deadline = time.time() + WAIT_FILL_S
        while time.time() < deadline:
            ret_q, q = tctx.order_list_query(order_id=order_id, trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
            if ret_q == RET_OK and not q.empty:
                status = str(q.iloc[0].get("order_status"))
                dealt = float(q.iloc[0].get("dealt_qty") or 0)
                if status in ("FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED"):
                    break
            time.sleep(5)
        result["order_status"] = status
        result["dealt_qty"] = dealt
        print("[probe] 60s 内状态=%s dealt=%s" % (status, dealt))

        # 收尾: 成交即平 (marketable 买回, 防残留裸空), 未成交即撤
        if dealt >= 1:
            ret_s2, snap2 = qctx.get_market_snapshot([pick["code"]])
            ask2 = last2 = 0.0
            if ret_s2 == RET_OK and not snap2.empty:
                ask2 = float(snap2.iloc[0].get("ask_price") or 0)
                last2 = float(snap2.iloc[0].get("last_price") or 0)
            ref = ask2 if ask2 > 0 else (last2 if last2 > 0 else price)
            buy_price = round(min(ref * 1.01, PRICE_CAP_USD), 2)
            ret_f, fdata = tctx.place_order(price=buy_price, qty=1, code=pick["code"],
                                            trd_side=TrdSide.BUY, order_type=OrderType.NORMAL,
                                            trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID,
                                            time_in_force=TimeInForce.DAY, session=Session.ETH,
                                            remark=REMARK + "_flat")
            result["flatten"] = {"price": buy_price, "ret": int(ret_f), "msg": str(fdata)[:160]}
            print("[probe] 平仓(买回 1 @ %s) ret=%s" % (buy_price, ret_f))
            time.sleep(10)
        elif status not in ("CANCELLED_ALL", "FAILED"):
            ret_x, xdata = tctx.modify_order(modify_order_op=ModifyOrderOp.CANCEL, order_id=order_id,
                                             qty=0, price=0, trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
            result["cancel"] = {"ret": int(ret_x), "msg": str(xdata)[:160]}
            print("[probe] 撤单 ret=%s" % ret_x)

        ret_p2, pos2 = tctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        result["post_positions"] = [] if ret_p2 != RET_OK else [
            {"code": r["code"], "qty": float(r["qty"])} for _, r in pos2.iterrows()]
        print("[probe] 后态持仓:", result["post_positions"])
        result["verdict"] = ("eth_short_fills" if dealt >= 1 else
                             "eth_short_accepted_no_fill" if status in ("SUBMITTED", "SUBMITTING", "WAITING_SUBMIT")
                             else "eth_short_unsupported")
        _write_ledger(result)
        print("[probe] 判定 =", result["verdict"])
    finally:
        qctx.close()
        tctx.close()
        if result.get("verdict"):
            print("[probe] RESULT " + json.dumps(result, ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()