#!/usr/bin/env python3
"""出场意图账实不符 reaper — **影子模式: 只检测 + 告警, 不改任何状态**

背景 (RFC-168 方案 E, 2026-10-26):
  app/playbook/pipeline.py:3690 先在 DB A (trade_records) 落账 → :3704 才 ack
  DB B (exit_intents, 独立 sqlite)。两库无跨库事务, 进程若在两者之间崩溃 →
  intent 停在 FILLED_UNBOOKED →
    · app/execution/exit_lifecycle.py:192 的 exit_symbol_busy **冻结该标的所有后续出场**
    · app/execution/exit_lifecycle.py:423 的 REFUSE_UNBOOKED 拒绝人工释放
    · scripts/release_stuck_exit_intent.py:196 同样拒绝 (删行会掐断 ack 落账路径)
  ⇒ 该标的被永久锁死: 仓还在, 出场发不出去, 且**无 reaper**。

本脚本为影子:
  - 只读两个库, 只写影子 JSONL, 只经 app/push/ hub 告警;
  - **不调用 ack_booked, 不改 exit_intents 任何状态** —— 启用自动补 ack 会改写
    出场状态机, 须另立提案并观察期 (宪章: 行为变更影子先行)。
  - 现状 (2026-10-26): 229 条 intent 全 BOOKED, 非 BOOKED 为 0 ⇒ 本项是
    **预防性**巡检, 不是救火。

用法:
  python scripts/exit_intent_reaper.py            # 采集 + 告警
  python scripts/exit_intent_reaper.py --dry-run  # 只打印, 不落影子/不推
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("ftseek_us.exit_intent_reaper")

SHADOW_PATH = ROOT / "data" / "shadow" / "exit_intent_reaper.jsonl"
# 账实不符的观察窗: intent 更新时间早于该窗 ⇒ 属"崩溃窗口残留"而非正在处理中
STALE_AFTER_S = 300


def _ledger_db() -> Path:
    """出场意图账本路径 (SSOT 解析器, 不硬编码)。"""
    from app.core.paths import get_exit_ledger_db
    return Path(get_exit_ledger_db())


def _read_unbooked(ledger_db: Path) -> list:
    """读出处于 FILLED_UNBOOKED 的意图 (只读)。"""
    conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True, timeout=10)
    try:
        cur = conn.execute(
            "SELECT intent_id, symbol, direction, quantity, owner_json, state, "
            "created_at, updated_at FROM exit_intents WHERE state='FILLED_UNBOOKED'"
        )
        return cur.fetchall()
    finally:
        conn.close()


def _trade_closed(owner_json: str) -> dict | None:
    """按 owner_json 里的 (db_path, trade_id) 核对成交台账是否已落账。

    返回 {"db_path":..., "trade_id":..., "exit_time":..., "exit_price":...} 或 None。
    """
    try:
        owner = json.loads(owner_json or "{}")
    except (TypeError, ValueError):
        return None
    db_path = owner.get("db_path")
    trade_id = owner.get("trade_id")
    if not db_path or trade_id is None:
        return None
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT exit_time, exit_price FROM trade_records WHERE id=?",
            (trade_id,),
        )
        row = cur.fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    exit_time = row[0]
    if not exit_time:
        return None            # 成交台账未落账 ⇒ 不是账实不符, 是正常在途
    return {
        "db_path": str(p),
        "trade_id": trade_id,
        "exit_time": exit_time,
        "exit_price": row[1],
    }


def detect(ledger_db: Path, now: float) -> list:
    """检出"成交已落账但 intent 未 ack"且已超观察窗的意图。"""
    findings = []
    for intent_id, symbol, direction, quantity, owner_json, state, created_at, updated_at in _read_unbooked(ledger_db):
        age = now - float(updated_at or created_at or now)
        closed = _trade_closed(owner_json)
        if closed is None:
            continue                      # 台账未落账 ⇒ 在途, 不算账实不符
        if age < STALE_AFTER_S:
            continue                      # 刚落账, 可能正在 ack
        findings.append({
            "intent_id": intent_id,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "state": state,
            "age_seconds": round(age, 1),
            "exit_time": closed["exit_time"],
            "exit_price": closed["exit_price"],
            "trade_id": closed["trade_id"],
            "trade_db": closed["db_path"],
            "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": "detected_only",     # 影子: 不改状态
        })
    return findings


def _append_shadow(findings: list) -> None:
    if not findings:
        return
    SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_PATH.open("a", encoding="utf-8") as f:
        for item in findings:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _notify(findings: list) -> None:
    """走 app/push/ hub 告警 (军规: 新增推送必须走 hub, 且默认静默 ——
    无料不推: 仅在真的检出账实不符时才推)。"""
    if not findings:
        return
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
    except Exception as e:
        logger.warning(f"[reaper] push 模块导入失败, 跳过告警: {e}", exc_info=True)
        return
    lines = []
    for item in findings[:10]:
        lines.append(
            f"· {item['symbol']} {item['direction']} qty={item['quantity']} "
            f"intent={item['intent_id'][:12]} age={item['age_seconds']}s "
            f"成交已落账({item['exit_time']}) 但 intent 仍 {item['state']}"
        )
    body = (
        f"检出 {len(findings)} 条出场意图账实不符:\n" + "\n".join(lines) +
        "\n\n该状态下 exit_symbol_busy 会冻结该标的后续出场, 且 REFUSE_UNBOOKED "
        "拒绝人工释放。\n本脚本为影子: 只告警不改状态, 需人工核对后处理。"
    )
    try:
        get_push_hub().push(PushMessage(
            title=f"🚨 出场意图账实不符 ({len(findings)} 条)",
            body=body,
            level=Level.CRITICAL,
            category=Category.RISK_ALERT,
            dedup_key=f"exit_intent_reaper:{len(findings)}",
        ))
    except Exception as e:
        logger.warning(f"[reaper] 告警发送失败: {e}", exc_info=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="出场意图账实不符 reaper (影子)")
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不落影子/不推告警")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ledger = _ledger_db()
    if not ledger.exists():
        logger.info(f"[reaper] 账本不存在, 跳过: {ledger}")
        return 0

    findings = detect(ledger, datetime.now(timezone.utc).timestamp())

    if args.dry_run:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
        return 0

    _append_shadow(findings)
    _notify(findings)
    if findings:
        logger.error(f"[reaper] 检出 {len(findings)} 条账实不符 (已落影子 + 告警, 未改状态)")
    else:
        logger.info("[reaper] 未检出账实不符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
