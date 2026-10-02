#!/usr/bin/env python3
"""手动释放卡死的 exit_intent — 让 SL 兜底重新接管该仓 (RFC-127 §6 遗留的人工处置流程).

病灶 (2026-10-01 US.ACN 首例, 全程实证):
  ETH 出场限价腿 60s 零成交被撤 → intent 进 `exit_eth_partial_fill_held` (HELD);
  此后 `_resume_playbook_exits` 把该 symbol 加进 `blocked_exits`
  (pipeline.py: `for intent in pending: blocked.add(...)`), 主退出循环对其
  `continue` ⇒ **SL 评估被整体跳过**, fast loop 每 ~10s 告警一次 (当日 2029 条)。
  实证代价: 10/1 ACN SHORT SL=223.16, 盘中冲到 227.63 (越线 2.0%) 而未平 —
  若 SL 生效亏 −244 (可控), 实际最坏 −347 (无保护), 多暴露 42%。

为什么外部平仓也不会自愈:
  `position_sync` 的 `external_close` 对账只写 trade_records, **完全不碰 exit_intents**
  (grep 零命中) ⇒ 券商侧仓位归零后意图仍卡 PENDING, 永久告警 + 污染 RFC-127 影子
  (影子读 `result_json.dealt_qty` = 系统自己那条已撤腿的 0, 不是券商真实持仓)。

释放语义 (精确, 不新造状态):
  `exit_intents` 有 partial unique index `ON (account_id,target_env,symbol)
  WHERE state != 'BOOKED'`, 且 `list_unsettled()` = `WHERE state != 'BOOKED'`
  ⇒ **改成 BOOKED 是唯一**能同时做到「解除 symbol busy」+「退出 pending 枚举
  ⇒ 不再进 blocked_exits ⇒ SL 兜底重新接管」的态。不新增 state 值 (做减法)。

安全性质:
  1. 默认 dry-run, 必须 `--apply` 才写库;
  2. 写前打印**券商侧真实持仓**与意图量对照 — 操作者必须自己确认
     "这个仓确实该由 SL 兜底接管 / 或已被外部平掉", 脚本不替你判断;
  3. 幂等: 已 BOOKED 的意图跳过 (不重复写);
  4. 留痕: receipt_json 写 `manual_release` + 原因 + 操作时刻, 不覆盖原
     result_json (证据保全);
  5. 只碰指定 intent_id (`--intent-id` 必填), 绝不批量释放。

用法:
  venv/bin/python scripts/release_stuck_exit_intent.py --intent-id <id>            # dry-run
  venv/bin/python scripts/release_stuck_exit_intent.py --intent-id <id> --apply \
      --reason "external_close: 用户手动平仓单已撤, 交回 SL 兜底"

回滚: 该脚本不改代码/配置; 误释放可用 --restore-pending 把 state 改回 PENDING
      (receipt_json 里的 manual_release 留痕保留)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXITS_DB = ROOT / "data" / "playbook_exits.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_intent(conn: sqlite3.Connection, intent_id: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM exit_intents WHERE intent_id = ?",
                       (intent_id,)).fetchone()
    return dict(row) if row else None


def _broker_position(symbol: str, account_id: str, target_env: str) -> list[dict]:
    """只读查券商侧真实持仓 (释放决策的地面真值, 不信本地台账)。"""
    try:
        from futu import (OpenSecTradeContext, RET_OK, SecurityFirm, TrdEnv,
                          TrdMarket)
        env = TrdEnv.SIMULATE if str(target_env) != "real" else TrdEnv.REAL
        ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                                  security_firm=SecurityFirm.NONE,
                                  host="127.0.0.1", port=11111)
        try:
            ret, pos = ctx.position_list_query(trd_env=env,
                                               acc_id=int(account_id))
            if ret != RET_OK or pos is None or pos.empty:
                return [{"_note": f"查询无结果 ret={ret}"}]
            out = []
            for _, r in pos.iterrows():
                if r.get("code") != symbol:
                    continue
                out.append({"code": r.get("code"), "qty": float(r.get("qty") or 0),
                            "side": r.get("position_side"),
                            "cost": float(r.get("cost_price") or 0),
                            "nominal": float(r.get("nominal_price") or 0),
                            "pl_val": r.get("pl_val")})
            return out or [{"_note": "券商侧该标的持仓为 0 (已被外部平掉?)"}]
        finally:
            ctx.close()
    except Exception as exc:  # 券商不可达不阻断 dry-run 报告
        return [{"_note": f"券商查询失败: {type(exc).__name__}: {exc}"[:160]}]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--intent-id", required=True, help="要释放的 exit_intent id (必填, 不批量)")
    ap.add_argument("--reason", default="", help="释放原因 (写入 receipt_json 留痕)")
    ap.add_argument("--apply", action="store_true", help="真写库 (默认 dry-run)")
    ap.add_argument("--restore-pending", action="store_true",
                    help="回滚: 把该意图 state 改回 PENDING (误释放时用)")
    ap.add_argument("--db", default=str(EXITS_DB), help="exit ledger DB 路径")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        intent = _load_intent(conn, args.intent_id)
        if not intent:
            print(f"❌ intent_id 不存在: {args.intent_id}")
            return 2

        symbol = intent["symbol"]
        state = intent["state"]
        direction = intent["direction"]
        qty = float(intent["quantity"] or 0)
        print(f"═══ 意图 {args.intent_id} ═══")
        print(f"  symbol={symbol} direction={direction} qty={qty} state={state}")
        print(f"  session={intent['session']} target_env={intent['target_env']} "
              f"account={intent['account_id']}")
        print(f"  close_key={intent['close_key']} last_error={intent['last_error']!r}")
        print(f"  limit_price={intent['limit_price']} created={intent['created_at']}")

        # 腿证据 (系统自己下过什么单)
        legs = list(conn.execute(
            "SELECT sequence,kind,quantity,price,remark,order_id,state,broker_status,dealt_qty "
            "FROM exit_legs WHERE intent_id=? ORDER BY sequence", (args.intent_id,)))
        print(f"  腿数={len(legs)}")
        for lg in legs:
            print(f"    #{lg['sequence']} {lg['kind']} qty={lg['quantity']} px={lg['price']} "
                  f"oid={lg['order_id']} {lg['state']}/{lg['broker_status']} "
                  f"dealt={lg['dealt_qty']} remark={str(lg['remark'])[:44]}")
        unsafe_legs = [lg for lg in legs
                       if lg["state"] != "TERMINAL" or lg["dealt_qty"] is None]
        if unsafe_legs:
            print("  ⛔ 存在非终态/成交未知腿: 先撤销或等待其终态, 否则释放意图会把"
                  "活跃出场单变成孤儿单。")

        # 券商侧地面真值
        print(f"\n═══ 券商侧真实持仓 ({symbol}) ═══")
        for p in _broker_position(symbol, intent["account_id"], intent["target_env"]):
            print("   ", p)
        print("  ⚠️ 请自行确认: 该仓是要交回 SL 兜底接管 (仍有持仓), "
              "还是已被外部平掉 (持仓=0)?")

        # 幂等
        if args.restore_pending:
            if state == "PENDING":
                print("\n(已是 PENDING, 无需回滚)")
                return 0
            if not args.apply:
                print(f"\n[dry-run] 将把 state {state} → PENDING")
                return 0
            conn.execute("UPDATE exit_intents SET state='PENDING', version=version+1, "
                         "updated_at=?, last_error='manual_restore_pending' "
                         "WHERE intent_id=?", (time.time(), args.intent_id))
            conn.commit()
            print(f"\n✅ 已回滚为 PENDING (receipt_json 留痕保留)")
            return 0

        if state == "BOOKED":
            print("\n(已是 BOOKED = 已释放, 幂等跳过)")
            return 0

        # 释放后果说明 (知情)
        print(f"\n═══ 释放后果 (state {state} → BOOKED) ═══")
        print("  1. 解除 partial unique index 的 symbol busy ⇒ 该标的新意图可再建;")
        print("  2. 退出 list_unsettled() 枚举 ⇒ 不再进 blocked_exits "
              "⇒ **SL 兜底 (metadata.stop_loss) 重新接管该仓**;")
        print("  3. fast loop 对该意图的重复告警停止 (当日已 2029 条);")
        print("  4. RFC-127 影子不再为该意图落样本 (避免幽灵样本污染阶段1判据)。")
        print("  ⚠️ 注意: 释放**不会自动平仓** — 若券商侧仍有持仓, 平仓职责交回")
        print("     SL/BE/trailing/EOD 兜底链 (仅 MarketPhase.TRADING 真平)。")

        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply")
            return 0
        if unsafe_legs:
            print("\n❌ 检测到非终态/成交未知腿, 拒绝释放。请先处理该腿。")
            return 2

        receipt = {"manual_release": True, "released_at": _utc_now(),
                   "prior_state": state, "reason": args.reason or "(未填)",
                   "broker_position_snapshot":
                       _broker_position(symbol, intent["account_id"], intent["target_env"]),
                   "script": "scripts/release_stuck_exit_intent.py"}
        conn.execute(
            "UPDATE exit_intents SET state='BOOKED', receipt_json=?, "
            "last_error='', version=version+1, updated_at=? WHERE intent_id=?",
            (json.dumps(receipt, ensure_ascii=False), time.time(), args.intent_id))
        conn.commit()

        # 回读验证 (不信任 write 返回值)
        chk = _load_intent(conn, args.intent_id)
        unsettled = [r[0] for r in conn.execute(
            "SELECT intent_id FROM exit_intents WHERE state != 'BOOKED'")]
        print(f"\n✅ 已释放: state={chk['state']} (回读确认)")
        print(f"   receipt 留痕: manual_release=True reason={args.reason or '(未填)'!r}")
        print(f"   当前未决意图: {len(unsettled)} 个 {unsettled[:5]}")
        ok = chk["state"] == "BOOKED" and args.intent_id not in unsettled
        print(f"   验证: {'✅ 意图已退出未决枚举' if ok else '🔴 验证失败, 请人工检查'}")
        return 0 if ok else 3
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
