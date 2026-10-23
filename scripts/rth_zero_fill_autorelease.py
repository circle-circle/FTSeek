#!/usr/bin/env python3
"""RTH 零成交卡死意图的自动释放 (RFC-157, 预注册判据; INCIDENT-003 缺口补丁)。

病灶 (2026-10-21 US.TSLA 全链路实证):
  RTH 出场腿在**不可成交窗口**被券商撤 (CANCELLED_ALL, dealt=0) ⇒ leg=TERMINAL。
  `ExitLifecycle.resume()` 只在 `if not legs` 时 `reserve_leg` ⇒ 已有终态腿的
  intent **永远不会补新腿**, 于是永久停在 PENDING/HELD ⇒
  `_resume_playbook_exits` 把该 symbol 放进 `blocked_exits` ⇒ SL/TP/时间/因子
  规则**整行 continue** (SIM 无券商止损 ⇒ 零保护静默期), 唯一出口是人工跑
  `scripts/release_stuck_exit_intent.py` (本例人工花了 4h42m)。

  ETH 侧早有同类自动出口 (`pipeline._release_eth_held_exit_for_rth_takeover`
  + `eth_held_exit_shadow` 守卫); **RTH 侧缺这一只** — 本模块补的就是这一只。

释放语义: `ExitLedger.archive_and_delete` (INCIDENT-002 订正 — 归档+删除让
  close_key 真正空出; 改 BOOKED 会永久占用 close_key, 10/2 ACN 已证伪)。

预注册判据 (全满足才释放; 任一不满足 → skip + 具名原因, 见 verdict()):
  C1 intent.state ∉ {BOOKED, FILLED_UNBOOKED}
  C2 intent.session == 'RTH'          (ETH 归自身 escalation, 本模块不碰)
  C3 legs 非空且**所有** leg.state == 'TERMINAL'
  C4 所有 leg.dealt_qty 已知且 sum(dealt_qty) == 0
     (**部分成交必须走簿记, 绝不释放** — 释放会让已成交数量丢账)
  C5 age = now - intent.created_at ≥ MIN_AGE_S (默认 1800s)
     ⚠️ 锚必须是 `created_at`, **不能是 `updated_at`**: 每次 resume 都会
     `ledger.result()` / `observe()` 把 intent+leg 的 updated_at 刷成"刚刚"
     (2026-10-22 实测: TSLA intent 建 16:45:52, updated_at 21:27:17 = 释放前 2 秒),
     锚在 updated_at 上 ⇒ 年龄恒 0 ⇒ 判据恒 False ⇒ 自愈功能静默死掉 (死门)。
     取 created_at = "距首次尝试平仓已过多久", 语义也更对。
  C6 本地 open_positions 仍有该 symbol+方向活仓 (True; None=查询失败 ⇒ 不释放)
  C7 当日同 symbol 自动释放次数 < DAILY_CAP (默认 3; 按审计台账跨进程计数,
     因为 cron 每次是新进程, 内存计数无效)

不做的事 (刻意边界): 不撤单、不建新意图、不平仓、不改 trade_records/open_positions。
拒绝方向 fail-closed: 任何异常 ⇒ 不释放 (宁可人工, 不可误删账目)。

载体: `scripts/extended_hours_exit_cron.py run_once` (每分钟/5 分钟, 已在台账内,
  与 orphan_ext_order_watch / exit_closekey_health 同位) — 不新增 cron 环路。

用法:
    PYTHONPATH=. venv/bin/python3.11 scripts/rth_zero_fill_autorelease.py         # 只打印 (dry)
    PYTHONPATH=. venv/bin/python3.11 scripts/rth_zero_fill_autorelease.py --apply # 真释放

回滚: env EXIT_RTH_ZERO_FILL_AUTORELEASE=false (跳过并留审计行);
      或从 extended_hours_exit_cron.run_once 删调用 (单点);
      已释放的意图用 release_stuck_exit_intent.py --restore 从归档回插。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

# 点号 logger 名 (W5 卫生门: 裸名 logger 判违)
logger = logging.getLogger("ftseek.scripts.rth_zero_fill_autorelease")

AUDIT = ROOT / "data" / "rth_zero_fill_autorelease.jsonl"
MIN_AGE_S = float(os.getenv("EXIT_RTH_ZERO_FILL_MIN_AGE_S", "1800"))
DAILY_CAP = int(os.getenv("EXIT_RTH_ZERO_FILL_DAILY_CAP", "3"))
ENABLED = os.getenv("EXIT_RTH_ZERO_FILL_AUTORELEASE", "true").lower() == "true"
KIND = "rth_zero_fill_autorelease"

# (仓位库, target_env) — 与 exit_intents.target_env 值域一致 (同 exit_closekey_health)
from app.core.paths import get_trade_records_db_for_arm as _trade_db_for_arm

_POSITION_DBS = (
    (_trade_db_for_arm("sim"), "sim"),
    (_trade_db_for_arm("real"), "real"),
)


def _ledger_path() -> Path:
    from app.core.paths import get_exit_ledger_db
    return get_exit_ledger_db()


def _now_ts() -> float:
    return time.time()


def _et_today() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── 判据 (纯函数, 契约测试钉真值表) ────────────────────────────────────
def verdict(intent, legs, *, now, position_open, min_age_s=None):
    """→ (action, reason, evidence)。action ∈ {'release', 'skip'}。

    只做判定, 不写库。evidence 随归档落盘, 让事后能复核"凭什么释放的"。
    """
    min_age_s = MIN_AGE_S if min_age_s is None else float(min_age_s)
    state = str(intent.get("state") or "")
    if state in ("BOOKED", "FILLED_UNBOOKED"):
        return "skip", "state_%s_owned_by_booking" % state.lower(), {}
    if str(intent.get("session") or "") != "RTH":
        return "skip", "session_not_rth", {}
    if not legs:
        return "skip", "no_leg", {}

    dealt = []
    for leg in legs:
        if str(leg.get("state") or "") != "TERMINAL":
            return "skip", "leg_not_terminal", {"sequence": leg.get("sequence"),
                                               "leg_state": leg.get("state")}
        qty = _num(leg.get("dealt_qty"))
        if qty is None:
            return "skip", "dealt_qty_unknown", {"sequence": leg.get("sequence")}
        dealt.append(qty)
    total_dealt = sum(dealt)
    if total_dealt > 0:
        # 部分成交: 必须由簿记流程收口, 释放会让已成交数量丢账
        return "skip", "partial_fill_booked_elsewhere", {"total_dealt": total_dealt}

    anchor = _num(intent.get("created_at")) or 0.0
    age = max(0.0, float(now) - anchor)
    if age < min_age_s:
        return "skip", "too_young", {"age_s": round(age, 1)}

    if position_open is not True:
        # False = 本地已无仓 (external_close 路径负责); None = 查询失败 (fail-closed)
        return "skip", "position_open_%s" % ("absent" if position_open is False else "unknown"), {}

    evidence = {
        "session": "RTH",
        "intent_age_s": round(age, 1),
        "total_dealt": total_dealt,
        "position_open": True,
        "legs": [
            {"sequence": leg.get("sequence"), "kind": leg.get("kind"),
             "state": leg.get("state"), "broker_status": leg.get("broker_status"),
             "order_id": leg.get("order_id"), "dealt_qty": _num(leg.get("dealt_qty"))}
            for leg in legs
        ],
        "source": "scripts/rth_zero_fill_autorelease",
    }
    return "release", "all_legs_terminal_zero_fill", evidence


# ── 只读查询面 ────────────────────────────────────────────────────────
def _open_intents(ledger_path):
    if not Path(ledger_path).exists():
        return []
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM exit_intents WHERE state NOT IN ('BOOKED')").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _legs(ledger_path, intent_id):
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence",
            (intent_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _canon_direction(value):
    """方向归一与 eth_held_exit_shadow 同源 (SHORT/SELL_SHORT/SELL → SHORT)。"""
    from app.playbook.eth_held_exit_shadow import _canon_direction as _impl
    return _impl(value)


def _position_open(symbol, direction, target_env):
    """本地 open_positions 是否仍有该 symbol+方向活仓。

    严格三态: True 有 / False 明确无 / None 查询失败或参数不全。
    本模块是**写操作**, 故 None 一律不释放 (fail-closed) — 与影子打点
    (fail-open 保样本) 的方向刻意相反。
    """
    canon = _canon_direction(direction)
    if not symbol or not canon or not target_env:
        return None
    db_path = None
    for path, env in _POSITION_DBS:
        if env == str(target_env):
            db_path = path
            break
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT direction, COALESCE(size, 0) FROM open_positions WHERE symbol = ?",
                (symbol,)).fetchall()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("[rth-zero-fill] 持仓守卫查询失败 %s %s: %s (不释放)",
                       symbol, direction, error, exc_info=True)
        return None
    return any(_canon_direction(r[0]) == canon and float(r[1] or 0.0) > 0 for r in rows)


def _released_today(symbol):
    """当日该 symbol 已自动释放次数 (跨进程: cron 每次新进程, 只能用台账)。"""
    if not AUDIT.exists():
        return 0
    today = _et_today()
    count = 0
    try:
        with AUDIT.open() as handle:
            for lineno, line in enumerate(handle, 1):
                try:
                    row = json.loads(line)
                except Exception:
                    logger.warning("[rth-zero-fill] 审计台账第 %d 行解析失败: %s",
                                   lineno, line.strip()[:120], exc_info=True)
                    continue
                if row.get("symbol") == symbol and row.get("et_date") == today \
                        and row.get("status") == "released":
                    count += 1
    except Exception:
        logger.warning("[rth-zero-fill] 审计台账不可读 (当日计数视为满额, 本轮不释放)",
                       exc_info=True)
        return DAILY_CAP
    return count


def _record(row):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _push(text):
    try:
        from app.push import push_text_safe
        from app.push.message import Level, Category
        return push_text_safe(text, level=Level.HIGH, category=Category.RISK_ALERT)
    except Exception:
        logger.warning("[rth-zero-fill] 释放通知发送失败 (审计台账已留痕)", exc_info=True)
        return False


def _release_text(row):
    return (
        "🔓 [退出链自愈] RTH 零成交卡死意图已自动释放\n"
        f"{row['symbol']} {row['direction']} {row['quantity']:g}股 "
        f"intent={row['intent_id']} (卡 {row['age_min']:.0f} 分钟)\n"
        "判据: 全部腿 TERMINAL + dealt=0 + 仓位仍在本地 (RFC-157 预注册)\n"
        "动作: 归档+删除 (close_key 空出) ⇒ SL/TP/EOD 兜底重新接管;\n"
        "未平仓 — 平仓职责交回退出链。当日该标的已自愈 "
        f"{row['released_today']}/{DAILY_CAP} 次"
    )


def scan(dry=False, now=None):
    """扫一轮。返回结构化摘要 (供 cron 结果字段消费)。"""
    now = _now_ts() if now is None else float(now)
    out = {"ok": True, "enabled": ENABLED, "dry": bool(dry),
           "intents": 0, "released": [], "skipped": [], "errors": []}
    if not ENABLED:
        out["skipped_disabled"] = True
        return out
    try:
        ledger_path = _ledger_path()
    except Exception as error:
        out["ok"] = False
        out["errors"].append("ledger_path: %s" % error)
        return out
    try:
        intents = _open_intents(ledger_path)
    except Exception as error:
        out["ok"] = False
        out["errors"].append("open_intents: %s" % error)
        return out
    out["intents"] = len(intents)
    if not intents:
        return out

    ledger = None
    for intent in intents:
        intent_id = intent.get("intent_id")
        try:
            legs = _legs(ledger_path, intent_id)
            pos = _position_open(intent.get("symbol"), intent.get("direction"),
                                 intent.get("target_env"))
            action, reason, evidence = verdict(
                intent, legs, now=now, position_open=pos)
        except Exception as error:
            out["errors"].append("%s: %s" % (intent_id, error))
            logger.warning("[rth-zero-fill] 判据计算异常 intent=%s (不释放): %s",
                           intent_id, error, exc_info=True)
            continue
        base = {"intent_id": intent_id, "symbol": intent.get("symbol"),
                "direction": intent.get("direction"),
                "quantity": _num(intent.get("quantity")) or 0.0,
                "et_date": _et_today(), "reason": reason}
        if action != "release":
            out["skipped"].append(base)
            continue
        already = _released_today(intent.get("symbol"))
        if already >= DAILY_CAP:
            base["reason"] = "daily_cap_reached"
            out["skipped"].append(base)
            logger.warning(
                "[rth-zero-fill] %s 当日已自动释放 %d 次达到上限 (%d), 本轮不释放 "
                "(持续零成交需人工看腿价/流动性)", intent.get("symbol"), already, DAILY_CAP)
            continue
        base["age_min"] = (_num(evidence.get("intent_age_s")) or 0.0) / 60.0
        base["released_today"] = already + 1
        if dry:
            base["status"] = "would_release"
            out["released"].append(base)
            continue
        try:
            if ledger is None:
                from app.execution.exit_lifecycle import ExitLedger
                ledger = ExitLedger(str(ledger_path))
            path, refuse = ledger.archive_and_delete(
                intent_id,
                reason="RFC-157 rth_zero_fill_autorelease: %s (age=%.0fmin, 仓位仍在) "
                       "⇒ 交回退出链重建意图" % (reason, base["age_min"]),
                now=now, evidence=evidence, kind=KIND)
        except Exception as error:
            out["errors"].append("%s: %s" % (intent_id, error))
            logger.warning("[rth-zero-fill] 释放异常 intent=%s (保持原状): %s",
                           intent_id, error, exc_info=True)
            continue
        if path is None:
            base["status"] = "refused"
            base["refuse"] = refuse
            out["skipped"].append(base)
            logger.warning("[rth-zero-fill] 释放被 ledger 守卫拒绝 intent=%s refuse=%s "
                           "(腿非终态/成交未知 ⇒ 需人工先撤腿)", intent_id, refuse)
            continue
        base["status"] = "released"
        base["archive"] = str(path)
        out["released"].append(base)
        logger.warning("🔓 [rth-zero-fill] 释放零成交卡死意图 %s %s intent=%s "
                       "age=%.0fmin archive=%s",
                       base["symbol"], base["direction"], intent_id,
                       base["age_min"], path)
        _record(base)
        _push(_release_text(base))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="真释放 (缺省只打印 dry-run 结果)")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)
    result = scan(dry=not args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    print("intents=%d released=%d skipped=%d errors=%d%s" % (
        result["intents"], len(result["released"]), len(result["skipped"]),
        len(result["errors"]), "" if args.apply else " (dry-run)"))
    for row in result["released"]:
        print("  RELEASE %s %s intent=%s age=%.0fmin %s" % (
            row.get("symbol"), row.get("direction"), row.get("intent_id"),
            row.get("age_min") or 0.0, row.get("status")))
    for row in result["skipped"]:
        print("  skip    %s %s intent=%s reason=%s" % (
            row.get("symbol"), row.get("direction"), row.get("intent_id"),
            row.get("reason")))
    for row in result["errors"]:
        print("  error   %s" % row)
    return 0


if __name__ == "__main__":
    sys.exit(main())