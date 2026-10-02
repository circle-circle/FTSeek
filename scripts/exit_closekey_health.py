#!/usr/bin/env python3
"""close_key 死锁体检 (INCIDENT-002 P2-5): 活仓的 close_key 被已终结 intent 占用 ⇒ P0 告警.

病灶 (2026-10-02 US.ACN 全链路实证):
  ETH 限价腿零成交被撤 → 人工按旧语义把 intent 改成 BOOKED →
  `UNIQUE(account_id, target_env, close_key)` **无 state 谓词** ⇒ BOOKED 行永久
  占用 close_key ⇒ ETH 重放链 (request 返回旧 BOOKED 行, reserve_leg=None) 与
  RTH SL 兜底 (session 不同 ⇒ exit_session_conflict) **双双发不出单**。
  且该形态**不产生任何未决意图** — list_unsettled()=0, blocked_exits 空,
  fast loop 安静, orphan watch 无触发源 ⇒ 唯一的信号是盘前每分钟一条文案误导
  的「锁盈待确认」推送。静默期无任何探针能发现它, 本模块补上这一只。

判据 (纯本地 sqlite, 零券商调用):
  ① open_positions 有活仓 + 对应 trade_records 未平仓行有 futu_order_id;
  ② exit_intents 存在 close_key='entry:<futu_order_id>' 且 state='BOOKED';
  ③ 该 intent updated_at 年龄 ≥ BOOKED_MIN_AGE_S (默认 180s, 滤掉「成交已簿记、
     open_positions 行还没被 position_sync 删掉」的正常瞬态窗口)。
  三条同时成立 = 账本说已终结、仓位库说仓还在 ⇒ 退出链对该仓全锁死。

动作面 (刻意最小): 只告警不修库 — 修复必须人工核对券商持仓后跑
  scripts/release_stuck_exit_intent.py (归档+删除, 有 --restore 回滚)。
  按 intent_id 跨进程去重 (审计 jsonl), 每意图只告一次; 告警含可直接粘贴的释放命令。

载体: scripts/extended_hours_exit_cron.py run_once, 会话门控**之前** (与孤儿单
  检测同位) — 每分钟一次, 全天覆盖 (死锁不分时段), 失败不阻塞退出链。

用法:
    PYTHONPATH=. venv/bin/python3.11 scripts/exit_closekey_health.py        # 扫一次
    PYTHONPATH=. venv/bin/python3.11 scripts/exit_closekey_health.py --dry  # 只打印

回滚: 从 extended_hours_exit_cron.run_once 删除对 scan() 的调用 (单点),
  或 env EXIT_CLOSEKEY_HEALTH=false 直接跳过 (留审计行)。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

# 点号 logger 名 (W5 卫生门: 裸名 logger 判违)
logger = logging.getLogger("ftseek.scripts.exit_closekey_health")

AUDIT = ROOT / "data" / "closekey_deadlock_alerts.jsonl"
BOOKED_MIN_AGE_S = float(os.getenv("EXIT_CLOSEKEY_BOOKED_MIN_AGE_S", "180"))
ENABLED = os.getenv("EXIT_CLOSEKEY_HEALTH", "true").lower() == "true"

# (仓位库, target_env) — 与 exit_intents.target_env 值域一致。
# 死库字面量门豁免: 双载体只读健康检查, 必须同时覆盖 sim/live 才不会漏当前
# 模式外的活仓死锁; mode-aware 解析器每次只返回一个载体。
_POSITION_DBS = (
    (ROOT / "data" / "ftseek_us.db", "sim"),
    (ROOT / "data" / "ftseek_us_live.db", "real"),
)


def _ledger_path() -> Path:
    from app.core.paths import get_exit_ledger_db
    return get_exit_ledger_db()


def _push(text: str) -> bool:
    """统一出口 — 走 PushHub (2026-09-18 用户裁「统一推送渠道」)。"""
    try:
        from app.push import push_text_safe
        from app.push.message import Level, Category
        return push_text_safe(text, level=Level.CRITICAL, category=Category.RISK_ALERT)
    except Exception:
        return False


def _open_position_keys(db_path: Path):
    """活仓 + 未平仓 trade 行 → [(symbol, direction, size, close_key)]。

    close_key 构造与 pipeline._playbook_exit_owner 逐字同构:
    有 futu_order_id ⇒ 'entry:<id>'; 没有 ⇒ 本探针不覆盖 (trade: 键含 db 路径与
    entry_time, 从仓位行反推不可靠, 宁可漏报不误报 — 该形态由 blocked_exits 兜住)。
    """
    out = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT p.symbol, p.direction, p.size, t.futu_order_id "
                "FROM open_positions p "
                "JOIN trade_records t ON t.symbol = p.symbol "
                "  AND COALESCE(t.exit_time,'')='' AND COALESCE(t.exit_price,0)=0 "
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for r in rows:
        entry_id = str(r["futu_order_id"] or "").strip()
        if entry_id:
            out.append((r["symbol"], r["direction"], r["size"], f"entry:{entry_id}"))
    return out


def _booked_intents(ledger_path: Path, close_keys):
    """close_key → BOOKED intent 行 (含年龄过滤)。"""
    hits = {}
    if not close_keys or not Path(ledger_path).exists():
        return hits
    now = time.time()
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT intent_id, symbol, close_key, session, state, updated_at "
            "FROM exit_intents WHERE state='BOOKED'"
        ).fetchall()
    finally:
        conn.close()
    wanted = set(close_keys)
    for r in rows:
        if r["close_key"] in wanted and (now - float(r["updated_at"] or 0)) >= BOOKED_MIN_AGE_S:
            hits[r["close_key"]] = dict(r)
    return hits


def _recorded_intent_ids() -> set:
    """已告警过的 intent_id 集合 (每 intent 只告警一次)。

    解析失败 **warn 留痕后跳过**, 不 except:pass (R5 模式 + 埋点禁静默死亡):
    吞掉解析错误会让「同一 intent 反复告警」这种去重失效变成不可见故障 —
    本探针存在的意义就是让静默故障可见, 自己先静默就本末倒置了。
    """
    ids = set()
    if not AUDIT.exists():
        return ids
    try:
        with AUDIT.open() as handle:
            for lineno, line in enumerate(handle, 1):
                try:
                    ids.add(json.loads(line).get("intent_id"))
                except Exception:
                    logger.warning(
                        "[closekey-health] 告警台账第 %d 行解析失败 (去重集合可能"
                        "不完整 ⇒ 本轮或重复告警): %s", lineno, line.strip()[:120],
                        exc_info=True)
    except Exception:
        logger.warning(
            "[closekey-health] 告警台账不可读 (本轮去重失效, 可能重复告警)",
            exc_info=True)
    return ids
    return ids


def _record(row: dict):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _alert_text(row: dict) -> str:
    age_min = row["intent_age_s"] / 60.0
    return (
        "🚨 [P0] 退出链死锁: 活仓 close_key 被已终结 intent 占用\n"
        f"{row['symbol']} {row['direction']} {row['size']:g}股 — 账本 BOOKED 但仓位仍在\n"
        f"intent={row['intent_id']} session={row['session']} "
        f"close_key={row['close_key']} (BOOKED 已 {age_min:.0f} 分钟)\n"
        "后果: ETH 重放与 RTH SL 兜底对该仓**均发不出单** (零保护, 无任何未决告警)。\n"
        "人工核对券商持仓后释放 (归档+删除, 可 --restore 回滚):\n"
        f"PYTHONPATH=. venv/bin/python3.11 scripts/release_stuck_exit_intent.py "
        f"--intent-id {row['intent_id']} --apply --reason \"close_key 死锁\""
    )


def scan(now=None, dry=False) -> dict:
    """扫一次。返回 dict (含命中/告警计数)。零券商调用; 无命中时零推送零写盘。"""
    if not ENABLED:
        return {"ok": True, "skip": "health_disabled"}
    ledger_path = _ledger_path()
    deadlocks, alerted, skipped = [], 0, []
    recorded = None  # 懒加载: 无命中不读审计文件
    for db_path, target_env in _POSITION_DBS:
        if not db_path.exists():
            continue
        positions = _open_position_keys(db_path)
        if not positions:
            continue
        hits = _booked_intents(ledger_path, [p[3] for p in positions])
        for symbol, direction, size, close_key in positions:
            intent = hits.get(close_key)
            if not intent:
                continue
            row = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "target_env": target_env,
                "symbol": symbol, "direction": direction,
                "size": float(size or 0),
                "close_key": close_key,
                "intent_id": intent["intent_id"],
                "session": intent["session"],
                "intent_age_s": round(time.time() - float(intent["updated_at"] or 0), 1),
                "dry": bool(dry),
                "alerted": False,
            }
            deadlocks.append(row)
            if recorded is None:
                recorded = _recorded_intent_ids()
            if row["intent_id"] in recorded:
                skipped.append(f"{symbol}:already_alerted")
                continue
            if not dry:
                row["alerted"] = _push(_alert_text(row))
                if row["alerted"]:
                    alerted += 1
                _record(row)
    return {"ok": True, "deadlocks": deadlocks, "alerted": alerted,
            "skipped": skipped, "dry": bool(dry)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印, 不写审计/不推送")
    args = ap.parse_args()
    print(json.dumps(scan(dry=args.dry), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
