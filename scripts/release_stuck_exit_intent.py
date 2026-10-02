#!/usr/bin/env python3
"""手动释放卡死的 exit_intent — 让退出链重新接管该仓 (RFC-127 §6 遗留的人工处置流程).

病灶 (2026-10-01 US.ACN 首例, 全程实证):
  ETH 出场限价腿 60s 零成交被撤 → intent 进 `exit_eth_partial_fill_held` (HELD);
  此后 `_resume_playbook_exits` 把该 symbol 加进 `blocked_exits`
  (pipeline.py: `for intent in pending: blocked.add(...)`), 主退出循环对其
  `continue` ⇒ **SL 评估被整体跳过**, fast loop 每 ~10s 告警一次 (当日 2029 条)。

释放语义 (2026-10-02 订正 — 原「改成 BOOKED」方案已被实证**证伪**):
  原版 docstring 声称「BOOKED 是唯一能同时做到解除 symbol busy + 退出
  pending 枚举 ⇒ SL 兜底重新接管的态」。10/1 20:27 ET 按此执行后, 10/2 盘前
  全链路复查证伪了后半句 (INCIDENT-002, DB 副本四项检验):
    - BOOKED 只解除 `list_unsettled()` 枚举与 partial unique index 的 symbol busy;
    - 但 `exit_intents` 还有 **UNIQUE(account_id, target_env, close_key) 无 state
      谓词** ⇒ BOOKED 行**永久占用 close_key**:
      ① ETH 链每分钟 `request()` 命中旧 BOOKED 行原样返回 → `reserve_leg` 对
         BOOKED 直接 None → 一单发不出 (10/2 盘前 32 条「锁盈待确认」误导推送);
      ② RTH 链 `close_position` 默认 session='RTH' ≠ 旧行 session='ETH' →
         `exit_session_conflict` BLOCKED → **SL 兜底同样被封死** (零保护静默期);
    - 把旧行 session 改成 RTH 也不够 (仍 BOOKED ⇒ reserve_leg=None)。
  ⇒ 正确释放 = **归档并删除**该 intent 及其 legs, 让 close_key 真正空出来,
  ETH 重放与 RTH SL 兜底都能重建意图。10/2 09:21 ET 生产实证: 归档删除后
  下一轮 cron 即重建 intent 并 FILLED_ALL 23@211.5, 仓位正常收口。

SSOT (2026-10-02 一并改造批): 写操作**不在本脚本内联** — 归档/删除/回读验证/
  回插全部走 `ExitLedger.archive_and_delete()` 与 `restore_archived()`
  (app/execution/exit_lifecycle.py), 与 RFC-128 自动释放
  (`position_sync.release_exit_intents_on_external_close` → `release_unsettled`)
  **共用同一实现** ⇒ 两调用方永不漂移 (协议 Gate 4: 同一逻辑多处 inline = 漂移根源)。
  归档目录 = ledger DB 同级的 `exit_intents_archive/`
  (生产: `data/exit_intents_archive/<intent_id>_<ts>_<unique>.json`)。

安全性质:
  1. 默认 dry-run, 必须 `--apply` 才写库;
  2. 写前打印**券商侧真实持仓**与意图量对照, 并作为 evidence 随档保存 —
     操作者必须自己确认 "这个仓确实该交回退出链 / 或已被外部平掉", 脚本不替你判断;
  3. ledger 守卫 fail-closed: 非终态/成交未知腿 ⇒ 拒 (防活跃出场单变孤儿);
     state=FILLED_UNBOOKED ⇒ 拒 (成交已发生簿记未完成, 删行会掐断
     `acknowledge_close` 落账 — 让 fast loop 先完成簿记);
  4. 留痕: 整行 intent (含 receipt_json/result_json 原文) + 全部 legs 先写归档
     文件, 归档写成功才 DELETE (ledger 内同函数先档后删, 档失败即中止);
  5. 只碰指定 intent_id (`--intent-id` 必填), 绝不批量释放;
  6. 回滚: `--restore` 经 `restore_archived()` 从归档回插 (close_key 被现役
     意图占用时拒绝, 不覆盖)。

用法:
  venv/bin/python3.11 scripts/release_stuck_exit_intent.py --intent-id <id>          # dry-run
  venv/bin/python3.11 scripts/release_stuck_exit_intent.py --intent-id <id> --apply \
      --reason "ETH 腿已撤且 TERMINAL, 仓位仍在, 交回退出链"
  venv/bin/python3.11 scripts/release_stuck_exit_intent.py --intent-id <id> --restore  # 回滚

契约测试: tests/test_release_stuck_exit_intent.py
  (钉住「释放后同 close_key + session='RTH'/'ETH' 的 request() 能重建意图并
  reserve_leg 拿到腿」— 即本脚本存在的唯一理由)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXITS_DB = ROOT / "data" / "playbook_exits.db"


def _ledger(db_path):
    """释放语义 SSOT 的入口 — 本脚本不自己写库。"""
    from app.execution.exit_lifecycle import ExitLedger
    return ExitLedger(str(db_path))


def _load_intent(conn: sqlite3.Connection, intent_id: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM exit_intents WHERE intent_id = ?",
                       (intent_id,)).fetchone()
    return dict(row) if row else None


def _load_legs(conn: sqlite3.Connection, intent_id: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence",
        (intent_id,))]


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
    ap.add_argument("--reason", default="", help="释放原因 (写入归档文件留痕)")
    ap.add_argument("--apply", action="store_true", help="真写库 (默认 dry-run)")
    ap.add_argument("--restore", action="store_true",
                    help="回滚: 从归档文件把 intent+legs 原样插回")
    ap.add_argument("--db", default=str(EXITS_DB), help="exit ledger DB 路径")
    args = ap.parse_args()

    ledger = _ledger(args.db)

    if args.restore:
        path, refuse = ledger.restore_archived(args.intent_id)
        if path is not None:
            conn = sqlite3.connect(args.db, timeout=30)
            try:
                chk = _load_intent(conn, args.intent_id)
                n_legs = len(_load_legs(conn, args.intent_id))
            finally:
                conn.close()
            print(f"✅ 已从 {Path(path).name} 回插: "
                  f"state={chk['state'] if chk else '?'} legs={n_legs}")
            return 0
        print(f"❌ 回插失败: {refuse}"
              + (" (找不到归档文件)" if refuse == "exit_restore_archive_missing" else ""))
        return 2 if refuse == "exit_restore_archive_missing" else 3

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        intent = _load_intent(conn, args.intent_id)
        if not intent:
            # 新语义下「行不存在」= 已释放 (或从未存在) — 幂等出口
            arch_dir = ledger.archive_dir
            archives = sorted(arch_dir.glob(f"{args.intent_id}_*.json")) \
                if arch_dir.is_dir() else []
            if archives:
                print(f"(该意图已释放, 归档在 {archives[-1].name}; 幂等跳过)")
                return 0
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

        legs = _load_legs(conn, args.intent_id)
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

        # 券商侧地面真值 (随档保存为 evidence)
        print(f"\n═══ 券商侧真实持仓 ({symbol}) ═══")
        broker_snapshot = _broker_position(symbol, intent["account_id"],
                                           intent["target_env"])
        for p in broker_snapshot:
            print("   ", p)
        print("  ⚠️ 请自行确认: 该仓是要交回退出链重建意图 (仍有持仓), "
              "还是已被外部平掉 (持仓=0)?")

        if state == "FILLED_UNBOOKED":
            print("\n❌ state=FILLED_UNBOOKED: 成交已发生、簿记未完成 — 删除会掐断 "
                  "acknowledge_close 落账。等 fast loop 完成簿记 (→BOOKED) 后再处置。")
            return 2

        # 释放后果说明 (知情)
        print(f"\n═══ 释放后果 (归档并删除 intent + {len(legs)} legs) ═══")
        print(f"  1. close_key={intent['close_key']} 真正空出 ⇒ ETH 重放链与 RTH")
        print("     SL 兜底 (close_position 默认 session='RTH') 都能重建意图下单;")
        print("  2. 解除 partial unique index 的 symbol busy + 退出 list_unsettled()")
        print("     枚举 ⇒ 不再进 blocked_exits, SL 评估恢复;")
        print("  3. 整行证据 (含 receipt_json/result_json) 保全到归档文件, 可 --restore;")
        print("  4. RFC-127 影子不再为该意图落样本。")
        print("  ⚠️ 注意: 释放**不会自动平仓** — 若券商侧仍有持仓, 平仓职责交回")
        print("     SL/BE/trailing/EOD 兜底链 (仅 MarketPhase.TRADING 真平)。")
        print("  ⚠️ 历史教训 (INCIDENT-002): 旧版「改成 BOOKED」会永久占用 close_key,")
        print("     把可见告警换成静默零保护 — 已被 10/2 ACN 全链路实证证伪, 勿回退。")

        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply")
            return 0
        if unsafe_legs:
            print("\n❌ 检测到非终态/成交未知腿, 拒绝释放。请先处理该腿。")
            return 2

        # SSOT: 归档 + 删除 + 回读验证 (intent行/close_key/legs 三项) 全在 ledger 内,
        # 与 RFC-128 自动释放共用同一实现 — 本脚本不自己写库。
        path, refuse = ledger.archive_and_delete(
            args.intent_id, reason=args.reason or "(未填)", now=time.time(),
            evidence={"broker_position_snapshot": broker_snapshot,
                      "script": "scripts/release_stuck_exit_intent.py"},
            kind="manual_release")
        if path is None:
            print(f"\n❌ ledger 拒绝释放: {refuse} (fail-closed, 意图保留)")
            return 2 if refuse == ledger.REFUSE_UNSAFE_LEG else 3

        unsettled = [r[0] for r in conn.execute(
            "SELECT intent_id FROM exit_intents WHERE state != 'BOOKED'")]
        print(f"\n✅ 已释放: 归档 → {path}")
        print(f"   reason={args.reason or '(未填)'!r}")
        print("   验证: intent行删除=✅ close_key空出=✅ legs删除=✅ (ledger 内回读)")
        print(f"   当前未决意图: {len(unsettled)} 个 {unsettled[:5]}")
        print("   ✅ 退出链可对该仓重建意图 (误释放可 --restore 回插)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
