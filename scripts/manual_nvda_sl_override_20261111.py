#!/usr/bin/env python3
"""单仓止损线下移 (US.NVDA, 用户令: 盘前不止损 / 等盘中 / 留时间与空间)。

止损的**三个表示**, 只有两个是权威:
  ① 权威: `trade_records.metadata_json.stop_loss` —— exit_monitor_flow 读它做 SL 判定;
  ② 权威镜像: `open_positions.current_sl / initial_sl` —— 仓位面板/对账读它;
  ③ **展示列**: `open_positions.stop_loss_price` —— 下单时写入的初始止损, 无人维护,
     却是 dashboard / battle_plan 的"已跌破止损"告警来源。它是 ③ 号缺陷的来源:
     2026-11-12 核查实测 NVDA 该列仍是旧值 165.43, 而真值已下移到 156.00。

本脚本三种模式:
  (默认)          apply: 三处一起写 (① ② ③), 保证不再产生分叉;
  --sync-display  只把 ③ 对齐 ② (一次性修展示列, 不动止损真值);
  --revert        用最近一份备份还原 ①②③。

回滚依据: 备份文件 data/manual_overrides/<UTC>_<SYMBOL>_sl_backup.json,
  **整行快照** (SELECT *), 故 revert 能还原 stop_loss_price 等所有列。
幂等: 已处于目标止损时跳过写入 —— 否则重跑会把 stop_loss_prev 覆盖成当前值,
  原始止损 (165.43) 就永久丢了 (2026-11-12 修)。
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
TRADE_ID = 1864
SYMBOL = "US.NVDA"
NEW_SL = 156.00
BACKUP_DIR = Path("/opt/ftseek/FTSeek-US/data/manual_overrides")

revert = "--revert" in sys.argv
sync_display = "--sync-display" in sys.argv


def _now_cst() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_backup(tr, op) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / ("%s_%s_sl_backup.json" % (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        SYMBOL.replace(".", "_")))
    path.write_text(
        json.dumps({"trade_records": dict(tr), "open_positions": dict(op)},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    return path


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        tr = conn.execute(
            "SELECT * FROM trade_records WHERE id=?", (TRADE_ID,)).fetchone()
        op = conn.execute(
            "SELECT * FROM open_positions WHERE symbol=?", (SYMBOL,)).fetchone()
        if tr is None or op is None:
            raise SystemExit("仓位/成交记录不存在, 中止")
        md = json.loads(tr["metadata_json"] or "{}")

        if sync_display:
            target = float(op["current_sl"] or 0)
            if target <= 0:
                raise SystemExit("current_sl 缺失, 无法对齐展示列")
            if op["stop_loss_price"] is not None and abs(
                    float(op["stop_loss_price"]) - target) < 1e-6:
                print("display already aligned ->", target)
            else:
                bak = _write_backup(tr, op)
                conn.execute(
                    "UPDATE open_positions SET stop_loss_price=?, updated_at=? "
                    "WHERE symbol=?", (target, _now_cst(), SYMBOL))
                conn.commit()
                print("display synced: stop_loss_price %s -> %s; backup -> %s"
                      % (op["stop_loss_price"], target, bak))

        elif not revert:
            if float(md.get("stop_loss") or 0) == NEW_SL and md.get("sl_manual_override"):
                print("already applied (stop_loss=%s), skip — 保持 stop_loss_prev 原值"
                      % NEW_SL)
            else:
                bak = _write_backup(tr, op)
                old_sl = float(md.get("stop_loss") or 0)
                md["stop_loss_prev"] = old_sl
                md["stop_loss"] = NEW_SL
                md["sl_manual_override"] = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "from": old_sl, "to": NEW_SL, "by": "hermes",
                    "reason": "用户令: 盘前不止损, 等盘中观察, 留时间与空间; "
                              "线下移到 11-11 盘后砸盘低点 156.20 之下",
                }
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (json.dumps(md, ensure_ascii=False), TRADE_ID))
                # 三处同写: current_sl / initial_sl (权威镜像) + stop_loss_price (展示列)
                conn.execute(
                    "UPDATE open_positions SET current_sl=?, initial_sl=?, "
                    "stop_loss_price=?, updated_at=? WHERE symbol=?",
                    (NEW_SL, NEW_SL, NEW_SL, _now_cst(), SYMBOL))
                conn.commit()
                print("applied; backup ->", bak)
        else:
            cands = sorted(BACKUP_DIR.glob("*%s_sl_backup.json" % SYMBOL.replace(".", "_")))
            if not cands:
                raise SystemExit("无备份文件, 无法 revert")
            # 用**最早**一份 (= 首次干预前状态)。取最新会把后续的展示列对齐当成回滚点,
            # 变成"回滚了展示修复但仍保留止损改动" —— 语义错位 (2026-11-12 修)。
            bak = json.loads(cands[0].read_text(encoding="utf-8"))
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                         (bak["trade_records"]["metadata_json"], TRADE_ID))
            op_bak = bak["open_positions"]
            conn.execute(
                "UPDATE open_positions SET current_sl=?, initial_sl=?, "
                "stop_loss_price=?, updated_at=? WHERE symbol=?",
                (op_bak["current_sl"], op_bak["initial_sl"],
                 op_bak.get("stop_loss_price"), _now_cst(), SYMBOL))
            conn.commit()
            print("reverted from backup:", cands[0])

        # 回读验证: 三个表示必须同时给出同一个答案 (这是本脚本存在的唯一理由)
        r1 = conn.execute("SELECT metadata_json FROM trade_records WHERE id=?",
                          (TRADE_ID,)).fetchone()
        r2 = conn.execute(
            "SELECT current_sl, initial_sl, stop_loss_price FROM open_positions "
            "WHERE symbol=?", (SYMBOL,)).fetchone()
        md = json.loads(r1["metadata_json"])
        print("verify: metadata.stop_loss = %s | prev = %s | current_sl = %s | "
              "initial_sl = %s | stop_loss_price(display) = %s"
              % (md.get("stop_loss"), md.get("stop_loss_prev"), r2["current_sl"],
                 r2["initial_sl"], r2["stop_loss_price"]))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())