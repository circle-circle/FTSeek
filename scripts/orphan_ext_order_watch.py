#!/usr/bin/env python3
"""孤儿 ETH 单检测器（**观察期: 只告警, 不撤单**）— DEB-20260918 §23.3 方案 A.

背景（R6 实锤）: 延长时段的卖单在 `ExitLifecycle.preflight` 被 fail-closed 守卫拦下
(`exit_unowned_active_order`) 后, 孤儿单会一直存活到券商侧自行撤销/成交 —— US.CRM 那次
**4 小时**（05:50 → 09:48:52 ET）, 期间该多仓**没有有效下行保护**（挂单在价格上方）,
而系统每分钟都在重复"被同一张单挡住"的失败（205 条 ERROR 日志）。

本检测器的动作面（刻意最小）:
  ① 触发源 = durable 账本里最近的"被守卫拦下"意图（**结构化**, 且**无事件时零券商调用**）;
  ② 确认 = 复用共享 `query_order_rows` 查**一次**订单表, 本地筛:
     `remark` 前缀 `ext_exit_` + 状态仍是活单 + `create_time` 年龄 ≥ 阈值;
  ③ 命中 ⇒ 写 `data/orphan_ext_orders.jsonl`（按 (symbol, order_id) 去重）+ 飞书告警（每单一次, 每日有上限）;
  ④ **不撤单、不改 state**（撤当日陈旧孤儿单会消耗该标的当日 attempt ⇒ 静默掐掉当日机会,
     必须配合 attempt 清理才安全 —— 那是方案 B, 需另行签字）。

用法:
    PYTHONPATH=. venv/bin/python scripts/orphan_ext_order_watch.py            # 扫描一次
    PYTHONPATH=. venv/bin/python scripts/orphan_ext_order_watch.py --dry      # 只打印, 不写不推

回滚: 从 `scripts/extended_hours_exit_cron.py::run_once` 删除对 `scan()` 的调用即可（单点）;
      或 env `EXT_ORPHAN_WATCH=false` 让扫描直接跳过（留审计行）。
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

_log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    _log.warning("dotenv load failed", exc_info=True)

from scripts.extended_hours_order_lifecycle import ACTIVE_ORDER_STATUSES, query_order_rows

AUDIT = ROOT / "data" / "orphan_ext_orders.jsonl"
REMARK_PREFIX = "ext_exit_"
# 只认"我们自己的单挡住我们自己"这一类拦截原因; 其他 HELD 原因（持仓归属冲突等）不是孤儿单问题。
BLOCKED_HINTS = ("exit_unowned_active_order",)
LOOKBACK_S = float(os.getenv("EXT_ORPHAN_LOOKBACK_S", "900"))       # 触发回看窗口
MIN_AGE_MIN = float(os.getenv("EXT_ORPHAN_MIN_AGE_MIN", "15"))      # 多老才算孤儿（分钟）
ALERT_MAX_PER_DAY = int(os.getenv("EXT_ORPHAN_ALERT_MAX_PER_DAY", "5"))   # 每日告警上限（防刷屏）
ENABLED = os.getenv("EXT_ORPHAN_WATCH", "true").lower() == "true"


def _now_et():
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            from datetime import timezone
            return datetime.now(timezone(timedelta(hours=-4)))


def _ledger_path():
    from app.core.paths import get_exit_ledger_db
    return get_exit_ledger_db()


def _open_trade_context():
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm
    return OpenSecTradeContext(
        host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPD_PORT", "11111")),
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.NONE,
    )


def _blocked_symbols(now_ts):
    """① 触发源: 最近被守卫拦下的意图 → {symbol: [ {intent_id,last_error,updated_at} ]}.

    用 durable 账本而不是解析日志: 结构化、有 intent 身份、且**无事件时零券商调用**。
    只取 state ∈ (PENDING, HELD) —— 已 BOOKED/FILLED 的意图说明阻塞已解除。
    """
    path = _ledger_path()
    if not path or not Path(path).exists():
        return {}
    con = sqlite3.connect(str(path), timeout=10)
    try:
        rows = con.execute(
            "SELECT symbol, intent_id, last_error, updated_at FROM exit_intents "
            "WHERE updated_at >= ? AND state IN ('PENDING','HELD') AND last_error != ''",
            (now_ts - LOOKBACK_S,),
        ).fetchall()
    finally:
        con.close()
    out = {}
    for symbol, intent_id, last_error, updated_at in rows:
        if not any(hint in str(last_error) for hint in BLOCKED_HINTS):
            continue
        out.setdefault(str(symbol), []).append({
            "intent_id": str(intent_id), "last_error": str(last_error),
            "updated_at": float(updated_at or 0),
        })
    return out


def _age_minutes(create_time, reference):
    """券商订单 create_time 是 **ET**（实测钉死）→ 与 ET 参考时刻算年龄（分钟）。"""
    try:
        naive = datetime.strptime(str(create_time).strip()[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    tz = reference.tzinfo
    created = naive.replace(tzinfo=tz) if tz else naive
    return round((reference - created).total_seconds() / 60.0, 1)


def _recorded_keys():
    """已记账的 (symbol, order_id) —— 跨进程去重靠审计文件（cron 每轮新进程）。"""
    keys = set()
    if not AUDIT.exists():
        return keys
    try:
        with AUDIT.open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    _log.warning("_recorded_keys: malformed jsonl line", exc_info=True)
                    continue
                keys.add((row.get("symbol"), row.get("order_id")))
    except Exception:
        _log.warning("_recorded_keys: audit file read failed", exc_info=True)
    return keys


def _alerts_today(today):
    if not AUDIT.exists():
        return 0
    count = 0
    try:
        with AUDIT.open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    _log.warning("_alerts_today: malformed jsonl line", exc_info=True)
                    continue
                if row.get("trade_date") == today and row.get("alerted"):
                    count += 1
    except Exception:
        _log.warning("_alerts_today: audit file read failed", exc_info=True)
    return count


def _record(row):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _push(text):
    """统一出口 — 走 PushHub (2026-09-18 用户裁「统一推送渠道」)。

    原直连 `FeishuNotifier().send_text()`: 与盘中链 (PushHub→钉钉) 分流, 同一
    类事件在两个平台各出现一半, 且丢掉 Hub 的去重/失败兜底。
    """
    try:
        from app.push import push_text_safe
        from app.push.message import Level, Category
        return push_text_safe(text, level=Level.CRITICAL, category=Category.RISK_ALERT)
    except Exception:
        return False


def _alert_text(row, blocked_events):
    intents = ",".join(sorted({str(e["intent_id"])[:8] for e in blocked_events})) or "-"
    return (
        "🚨 孤儿延长时段单存活（仅告警, 未撤单）\n"
        f"{row['symbol']} {row['side']} {row['qty']:g}股 @ {row['price']:g} | 状态 {row['status']}\n"
        f"订单 {row['order_id']} | remark {row['remark']}\n"
        f"创建 {row['create_time']} ET（已 {row['age_min']:g} 分钟）| intent {intents}\n"
        "该仓此刻无有效下行保护（挂单在价上方, 守卫只能 fail-closed）。\n"
        "人工确认后处理 —— 注意: 直接撤单会消耗该标的当日 attempt。"
    )


def scan(now=None, dry=False):
    """扫一次。返回 dict（含审计/告警计数）。无触发事件时不连券商。"""
    if not ENABLED:
        return {"ok": True, "skip": "watch_disabled"}
    if os.getenv("FUTU_TRD_ENV", "SIMULATE").upper() != "SIMULATE":
        return {"ok": True, "skip": "non_sim_env"}   # 延长时段链路当前只在 SIM 内

    now_et = now or _now_et()
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=_now_et().tzinfo)
    today = now_et.strftime("%Y-%m-%d")

    blocked = _blocked_symbols(now_et.timestamp())
    if not blocked:
        return {"ok": True, "skip": "no_blocked_intent"}   # 零券商调用

    recorded = _recorded_keys()
    alerted_today = _alerts_today(today)
    env = None
    ctx = None
    rows = []
    try:
        from futu import TrdEnv
        env = TrdEnv.SIMULATE
        ctx = _open_trade_context()
        rows = query_order_rows(ctx, env, int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823")))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                "blocked_symbols": sorted(blocked)}
    finally:
        if ctx is not None and hasattr(ctx, "close"):
            try:
                ctx.close()
            except Exception:
                _log.warning("scan: ctx.close() failed", exc_info=True)

    found, alerted, skipped = [], 0, []
    for row in rows:
        events = blocked.get(row["symbol"])
        if not events:
            continue
        if not str(row["remark"]).startswith(REMARK_PREFIX):
            continue
        if row["status"] not in ACTIVE_ORDER_STATUSES:   # 正判据: 只把"活单"当孤儿
            continue
        age_min = _age_minutes(row["create_time"], now_et)
        if age_min is None or age_min < MIN_AGE_MIN:
            skipped.append(f"{row['symbol']}:fresh")
            continue
        key = (row["symbol"], row["order_id"])
        if key in recorded:
            skipped.append(f"{row['symbol']}:already_recorded")
            continue

        record = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "trade_date": today,
            "symbol": row["symbol"],
            "order_id": row["order_id"],
            "side": row["side"],
            "qty": row["qty"],
            "filled_qty": row["filled_qty"],
            "price": row["price"],
            "status": row["status"],
            "remark": row["remark"],
            "create_time": row["create_time"],
            "age_min": age_min,
            "action": "alert_only",
            "intent_ids": sorted({str(e["intent_id"]) for e in events}),
            "blocked_reasons": sorted({str(e["last_error"]) for e in events}),
            "alerted": False,
            "dry": bool(dry),
        }
        if alerted_today + alerted >= ALERT_MAX_PER_DAY:
            record["alert_skipped"] = "daily_cap"
        else:
            record["alerted"] = False if dry else _push(_alert_text(record, events))
            if record["alerted"]:
                alerted += 1
        if not dry:
            _record(record)
        found.append(record)

    return {"ok": True, "blocked_symbols": sorted(blocked), "orphans": found,
            "alerted": alerted, "skipped": skipped, "dry": bool(dry)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印, 不写审计/不推送")
    ap.add_argument("--at", default=None, help="覆盖当前时刻 (ISO, 便于回放验证)")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.at) if args.at else None
    print(json.dumps(scan(now=now, dry=args.dry), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()