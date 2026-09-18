#!/usr/bin/env python3
"""孤儿 `ext_exit_*` 单的**人工确认撤销**工具（方案 B 的"人工"那一半）— DEB-20260918 §23.3/§23.5.

背景（R6 实锤）: 延长时段卖单被 `ExitLifecycle` 的 fail-closed 守卫拦下后, 孤儿单会一直存活到
券商自行撤销/成交 —— US.CRM 那次 **4 小时**（05:50:13 → ~09:48:52 ET），期间该多仓**没有有效下行保护**
（限价挂在市价上方），且系统每分钟重复"被同一张单挡住"的失败。方案 A 只告警、不撤单；
按复核建议 3 的语义（"**告警并（人工确认后）撤销**"）把**撤销动作**交给人：本工具。

刻意**不做自动化**：撤掉当日 `ext_exit_*` 单会消耗该标的当日 `attempt` 键
（`_reconcile_broker_entry_orders` 当日已撤单仍写 `attempt.status=failed`）⇒ 等于静默掐掉当日机会。
所以是否顺手清 attempt 必须由操作者显式决定（`--clear-attempt`），默认不清。

安全门（全部满足才动手，否则拒绝）:
  1. 订单存在, 且 `remark` 前缀是 `ext_exit_` —— **绝不碰别人的单**（`ext_entry`/`pbe_*`/手动单一律拒绝）;
  2. 状态仍是活单（`ACTIVE_ORDER_STATUSES`）—— 终态单无需撤销;
  3. 年龄 ≥ `EXT_ORPHAN_MIN_AGE_MIN`（默认 15 分钟, 与检测器同阈值）—— 防抢我们自己的正常退出;
  4. `--symbol` 给了就必须一致;
  5. 部分成交但拿不到成交均价 ⇒ 拒绝（不是干净的挂单）;
  6. 默认 **dry-run**，必须显式 `--apply` 才真撤单。

撤销后**回查终态**：撤单 API 成功 ≠ 订单已撤（历史教训 #185），所以 `--apply` 会 `query_order` 复查,
未变终态则报失败（退出码 2），绝不静默宣称成功。

用法:
    PYTHONPATH=. venv/bin/python scripts/cancel_orphan_ext_order.py --order-id 8185508            # dry-run
    PYTHONPATH=. venv/bin/python scripts/cancel_orphan_ext_order.py --order-id 8185508 --apply
    ... --apply --clear-attempt     # 同时清掉该标的当日 attempt 键（备份 state 文件）

回滚: 本工具不挂在任何 cron/循环里, 只是人工入口 ⇒ 不用时放着即可（无行为变更）。
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

from scripts.extended_hours_order_lifecycle import (  # noqa: E402
    ACTIVE_ORDER_STATUSES,
    cancel_order,
    query_order,
    query_order_rows,
)

AUDIT = ROOT / "data" / "orphan_ext_orders.jsonl"
STATE = ROOT / "data" / "extended_hours_entry_state.json"
REMARK_PREFIX = os.getenv("EXT_ORPHAN_REMARK_PREFIX", "ext_exit_")
MIN_AGE_MIN = float(os.getenv("EXT_ORPHAN_MIN_AGE_MIN", "15"))


def _now_et():
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from datetime import timezone as _tz
        return datetime.now(_tz.utc).astimezone()


def _age_minutes(create_time, now=None):
    """订单年龄（分钟）。create_time 是 ET 墙钟字符串; 不可解析返回 None（⇒ 调用方拒绝）。"""
    raw = str(create_time or "").strip()
    if not raw:
        return None
    now = now or _now_et()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return (now.replace(tzinfo=None) - dt).total_seconds() / 60.0
    return None


def decide(order, now=None, min_age_min=None, symbol=None, remark_prefix=None):
    """纯判定：返回 (ok: bool, reason: str, age_min: float|None)。不触券商/不写盘。"""
    min_age_min = MIN_AGE_MIN if min_age_min is None else min_age_min
    remark_prefix = REMARK_PREFIX if remark_prefix is None else remark_prefix
    if not order:
        return False, "order_not_found", None
    row_symbol = str(order.get("symbol") or "")
    if symbol and row_symbol != symbol:
        return False, f"symbol_mismatch:{row_symbol}!={symbol}", None
    remark = str(order.get("remark") or "")
    if not remark.startswith(remark_prefix):
        return False, f"not_our_exit_order:remark={remark!r}", None
    status = str(order.get("status") or "")
    if status not in ACTIVE_ORDER_STATUSES:
        return False, f"already_terminal:{status}", None
    age = _age_minutes(order.get("create_time"), now)
    if age is None:
        return False, "create_time_unparseable", None
    if age < min_age_min:
        return False, f"too_fresh:{age:.1f}min<{min_age_min:g}min", age
    filled = float(order.get("filled_qty") or 0.0)
    if filled > 0 and float(order.get("avg_fill_price") or 0.0) <= 0:
        return False, "partial_fill_without_avg_price", age
    return True, "eligible", age


def clear_attempt(symbol, today_et=None, state_path=None, apply=False):
    """清理 `{today_et}:{symbol}` attempt 键（默认只报不删）。返回 (changed, message)。"""
    state_path = Path(state_path or STATE)
    today_et = today_et or _now_et().date().isoformat()
    if not state_path.exists():
        return False, f"state 文件不存在: {state_path}"
    try:
        state = json.loads(state_path.read_text())
    except Exception as exc:
        return False, f"state 解析失败(fail-closed, 不动): {exc}"
    key = f"{today_et}:{symbol}"
    attempts = state.get("attempts") or {}
    if key not in attempts:
        return False, f"attempt 键不存在: {key}（无需清理）"
    if not apply:
        return False, f"[dry-run] 将删除 attempt 键 {key} = {json.dumps(attempts[key], ensure_ascii=False)}"
    backup = state_path.with_suffix(f".json.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(state_path, backup)
    removed = attempts.pop(key)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return True, f"已删除 attempt 键 {key}（原值 {json.dumps(removed, ensure_ascii=False)}）; 备份 {backup}"


def _audit(payload, path=None):
    path = Path(path or AUDIT)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="人工确认撤销孤儿 ext_exit_* 单（默认 dry-run）")
    ap.add_argument("--order-id", required=True)
    ap.add_argument("--symbol", default="", help="可选: 交叉核对标的")
    ap.add_argument("--apply", action="store_true", help="真的撤单（默认只打印）")
    ap.add_argument("--clear-attempt", action="store_true", help="同时清理该标的当日 attempt 键")
    ap.add_argument("--min-age-min", type=float, default=MIN_AGE_MIN)
    args = ap.parse_args(argv)

    from scripts.extended_hours_entry_executor import _open_trade_context

    env = os.getenv("FUTU_TRD_ENV", "SIMULATE")
    acc_id = int(
        os.getenv("SIMULATE_ACCOUNT_ID", "19930823")
        if env.upper() == "SIMULATE"
        else os.getenv("REAL_ACCOUNT_ID", "0")
    )
    ctx = _open_trade_context()
    try:
        order = query_order(ctx, env, acc_id, args.order_id)
        ok, reason, age = decide(
            order, min_age_min=args.min_age_min, symbol=args.symbol or None
        )
        print(f"订单 {args.order_id}: {json.dumps(order, ensure_ascii=False) if order else '未找到'}")
        print(f"判定: {reason}" + (f" (年龄 {age:.1f} min)" if age is not None else ""))
        if not ok:
            print("⇒ 拒绝执行（安全门未全过）")
            _audit({"ts": datetime.now().isoformat(), "order_id": args.order_id,
                    "action": "refused", "reason": reason,
                    "symbol": (order or {}).get("symbol", "")})
            return 1

        if not args.apply:
            print("⇒ dry-run: 加 --apply 才会真撤单（闸门: 上面三条 + --apply）")
            if args.clear_attempt:
                print(clear_attempt(order["symbol"], apply=False)[1])
            return 0

        cancel_ok, data = cancel_order(
            ctx, env, acc_id, args.order_id,
            qty=order.get("qty", 0), price=order.get("price", 0),
        )
        after = query_order(ctx, env, acc_id, args.order_id)
        after_status = str((after or {}).get("status") or "")
        terminal = after_status not in ACTIVE_ORDER_STATUSES
        print(f"撤单请求: cancel_ok={cancel_ok} | 回查状态={after_status or '未找到'}")
        if not terminal:
            # 撤单 API 成功 ≠ 订单已撤（历史 #185）⇒ 不宣称成功
            print("⇒ 撤单未生效（回查仍是活单）—— 视为失败, 请人工到券商端确认")
            _audit({"ts": datetime.now().isoformat(), "order_id": args.order_id,
                    "action": "cancel_unconfirmed", "cancel_ok": cancel_ok,
                    "status_after": after_status, "symbol": order["symbol"]})
            return 2
        _audit({"ts": datetime.now().isoformat(), "order_id": args.order_id,
                "action": "cancelled_by_operator", "symbol": order["symbol"],
                "age_min": age, "status_after": after_status})
        print(f"⇒ 已确认撤销: {order['symbol']} {args.order_id} → {after_status}")
        if args.clear_attempt:
            print(clear_attempt(order["symbol"], apply=True)[1])
        return 0
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())