#!/usr/bin/env python3
"""盘前/盘后事件驱动开仓安全入口 (2026-09-17, SIM fail-closed).

信号四层过滤 (宁缺毋滥, 用户原话"确定性因素第一时间执行"):
  1. 事件: Finnhub company_earnings 最近财报超预期 (surprisePercent>0) — "确定性"
  2. 价格: pre/after_price 相对昨收涨 ≥ +2% — 市场认可事件
  3. 量:   pre/after_volume ≥ 200,000 股 — 非薄量假动作
  4. 价格下限: ≥ MIN_PRICE ($5, 2026-09-17 用户裁方案A) — 挡仙股/妖股
     (实证: CTNT $0.073 +75.06% / GIPR $0.435 +19.75% 曾透过前 3 层; 仙股的"超预期>0"是季末噪声,
      而涨幅/量闸对妖股天然放行 ⇒ 日配额被噪声票用尽, 当晚真催化票开不出来)
当前 Futu SIM 不支持 STOP/STOP_LIMIT。没有券商侧止损时本脚本拒绝真实开仓；
dry 模式仅输出候选，不写 state、不下单。

护栏 (开仓风险高于锁盈, 更严):
  - 单笔仓位 $5000 (用户 9/17 拍板; 高低价股都覆盖)
  - 每日盘前+盘后合计 ≤MAX_PER_DAY 笔新仓 (2026-09-17 用户裁 2→5, 观察期; 防连环开仓)
  - SIM 无券商 STOP/STOP_LIMIT 时真实开仓 fail-closed
  - REAL 环境未授权, 一律拒绝
  - 每标的每日一次 attempt, 失败不消耗开仓配额

与锁盈执行器共用: 独立 OpenQuoteContext (主服务 quote ctx 被占用, 勿复用).
用法: PYTHONPATH=. venv/bin/python3.11 scripts/extended_hours_entry_executor.py \
        --session premarket [--dry]
回滚: 删本脚本 + 对应 cron 行.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
import subprocess
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

# ── 用户 9/17 拍板参数 ──
POSITION_USD = 5000        # 单笔仓位
MAX_PER_DAY = int(os.getenv("EXT_ENTRY_MAX_PER_DAY", "5"))
# ↑ 盘前+盘后**合计**每日新仓上限. 2026-09-17 用户裁: 2 → 5 (原话"配额可以提升到5，再观察").
#   动机: 延长时段机会面比预期宽(官方榜 top50), 2 笔在盘前窗早段就被用尽 ⇒ 当晚真催化票开不出来;
#   叠加同日上线的价格下限闸($5 挡仙股)后, 单笔噪声风险已降, 放宽数量换取样本速度。
#   暴露上限 = 5 × POSITION_USD($5000) = $25k (SIM 总资产 ~$99k)。回滚: env EXT_ENTRY_MAX_PER_DAY=2。
ENTRY_CHANGE_PCT = 2.0     # 盘前涨幅门槛
MIN_VOLUME = 200000        # 盘前量门槛
MIN_PRICE = float(os.getenv("EXT_ENTRY_MIN_PRICE", "5.0"))
# ↑ 价格下限闸 (2026-09-17 用户裁方案A): 挡 $5 以下仙股/妖股.
#   实证: CTNT $0.073(+75.06%, 量2217万, Finnhub超预期+105%) 与 GIPR $0.435(+19.75%)
#   曾透过"事件+涨幅+量"三闸 —— 仙股的第1闸(超预期>0)是季末噪声, 第2/3闸对妖股天然放行。
#   事故代价: 日配额 2 笔被噪声票用尽, 当晚真催化票开不出来。回滚=env EXT_ENTRY_MIN_PRICE=0。
STOP_PCT = -5.0            # 止损距离 (%) — 开盘写入 initial_sl, 由规则引擎盘中兜底
SCAN_TOP_N = 50            # 盘前/盘后榜扫描深度 (9/17 扩覆盖面: 官方榜 top50, 旧为15只活跃股)
# 2026-09-17 用户裁方案1: SIM 无券商STOP → 接受规则引擎兜底, 开关恢复下单.
#   止损保障: ①开盘 monitor_playbook_sim_exits 读 initial_sl 兜底 ②盘前 tighten 信号由 exit executor 平仓.
ALLOW_SIM_ENTRY = os.getenv("EXT_ENTRY_ALLOW_SIM", "true").lower() == "true"
AUDIT = ROOT / "data" / "extended_hours_entry_executions.jsonl"
# 价格闸拦下的候选打点 (按**运行**聚合一行: n + 明细). 用户 2026-09-17 令: 回答
#   "$5 闸到底挡掉了多少噪声票". 逐候选整行会膨胀(同票每 5 分钟重复出现) ⇒ 按运行聚合.
PRICE_GATE_REJECTS = ROOT / "data" / "extended_hours_price_gate_rejects.jsonl"
STATE = ROOT / "data" / "extended_hours_entry_state.json"
LOCK = ROOT / "data" / "extended_hours_entry_state.lock"
DB = ROOT / "data" / "ftseek_us.db"
SESSION_WINDOWS = {
    "premarket": (400, 929),
    "afterhours": (1600, 1959),
}

from scripts.extended_hours_order_lifecycle import (
    ACTIVE_ORDER_STATUSES,
    query_order_rows,
    wait_order_fill,
)


def _now_hhmm_et():
    now = _now_et()
    return now.hour * 100 + now.minute


def _in_session_window(session):
    start, end = SESSION_WINDOWS[session]
    return start <= _now_hhmm_et() <= end


def _is_trading_day():
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _today_et():
    return _now_et().strftime("%Y-%m-%d")


def _load_state():
    if not STATE.exists():
        return {"version": 2, "opened_today": {}, "attempts": {}}
    try:
        state = json.loads(STATE.read_text())
    except Exception as exc:
        raise RuntimeError(f"extended-hours entry state unreadable: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("extended-hours entry state is not a JSON object")
    return state


def _save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    with tmp.open("w") as handle:
        json.dump(s, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, STATE)


@contextlib.contextmanager
def _state_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _push(text):
    try:
        from app.notifier.feishu import FeishuNotifier
        FeishuNotifier().send_text(text)
    except Exception:
        pass


def _audit(rec):
    rec["ts"] = datetime.utcnow().isoformat() + "Z"
    with AUDIT.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _opened_today_total(opened_today, today):
    """当日(ET)已开仓计数 = **盘前 + 盘后合并** (key 形如 '2026-09-17:premarket').

    用户 9/17 令"配额提升到 5"针对的是**每日合计**, 不是单时段各 5 ⇒ 两个时段共享同一个上限。
    跨日残留 key 必须忽略 (state 里会留历史日期的 key, 用 startswith 过滤)。
    """
    return sum(value for key, value in opened_today.items() if key.startswith(today))


def _record_price_gate_rejects(session, rejects):
    """价格闸拦下的候选打点 + 日志一行 (用户 2026-09-17 令: 量化"$5 闸挡掉多少噪声票").

    按**运行**聚合: 一行 = 一次运行, 字段 `n` + `rejects` 明细 (symbol/price/change_pct/volume)。
    这样既能按日求和(回答"挡了多少"), 又保留标的身份(回答"挡的是谁"), 且不会让 jsonl 因
    同一只票每 5 分钟重复出现而膨胀。异常不得影响主流程。
    """
    if not rejects:
        return
    try:
        PRICE_GATE_REJECTS.parent.mkdir(parents=True, exist_ok=True)
        with PRICE_GATE_REJECTS.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": session,
                "n": len(rejects),
                "rejects": rejects,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    head = ", ".join(f"{r['symbol']} ${r['price']:g}" for r in rejects[:8])
    more = f" (+{len(rejects) - 8})" if len(rejects) > 8 else ""
    print(f"[ext_entry] 价格闸(<${MIN_PRICE:g}) 拦下 {len(rejects)} 只: {head}{more}")


def _price_in_range(price):
    """价格闸 (2026-09-17 用户裁方案A 加下限).

    下限 MIN_PRICE: 挡 $5 以下仙股/妖股 (实证 CTNT $0.073 / GIPR $0.435 曾透过前三闸)。
    上限 POSITION_USD: 单笔仓位 $5000, 超过则连 1 股都买不起。
    `price > 0` 在 MIN_PRICE 被 env 调成 0 时仍是必要的兜底。
    """
    return price > 0 and MIN_PRICE <= price <= POSITION_USD


def _scan_movers(session, qctx):
    """全市场盘前/盘后榜 (富途官方接口, 2026-09-17 实测). 返回 [{symbol,px,chg,vol,prev_close}].
    替代旧"近5日活跃股"扫描 — 覆盖面从 15 只 → 全市场真实盘前/盘后异动榜.
    字段: security / pre_market_price / pre_market_change_ratio / pre_market_volume / close_price."""
    from futu import RET_OK
    try:
        if session == "premarket":
            ret, rd = qctx.get_us_pre_market_rank(count=SCAN_TOP_N)
            pk, ck, vk = "pre_market_price", "pre_market_change_ratio", "pre_market_volume"
        else:
            ret, rd = qctx.get_us_after_hours_rank(count=SCAN_TOP_N)
            pk, ck, vk = "after_hours_price", "after_hours_change_ratio", "after_hours_volume"
        if ret != RET_OK or not isinstance(rd, tuple) or len(rd) < 2:
            return []
        df = rd[1]
        if df is None or not hasattr(df, "columns") or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            sec = str(r.get("security", ""))
            if not sec.startswith("US."):
                continue
            try:
                out.append({
                    "symbol": sec,
                    "px": float(r.get(pk, 0) or 0),
                    "chg": float(r.get(ck, 0) or 0),
                    "vol": float(r.get(vk, 0) or 0),
                    "prev_close": float(r.get("close_price", 0) or 0),
                })
            except Exception:
                continue
        return out
    except Exception:
        return []


def _held_symbols():
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        rows = conn.execute("SELECT symbol FROM open_positions WHERE size > 0").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as exc:
        raise RuntimeError(f"open_positions read failed, refusing entry: {exc}") from exc


def _open_trade_context():
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm

    return OpenSecTradeContext(
        host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPD_PORT", "11111")),
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.NONE,
    )


def _order_et_date(create_time):
    """broker 订单 create_time → ET 日期串.

    2026-09-17 实测钉死时区: 订单 8184987 的 broker create_time = '2026-09-16 23:14:45',
    对应审计 jsonl 的 UTC '2026-09-17T03:16:16Z' → 两者相差 4h15m ≈ ET(-4) 语义, 故 create_time 是 **ET**。
    解析失败返回 "" ⇒ 视为非当日 (绝不写 attempt, 宁可漏记不可误封锁)。
    """
    try:
        return datetime.strptime(str(create_time).strip()[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _order_session(row, fallback_session):
    create_time = row.get("create_time", "")
    try:
        hour = int(create_time[11:13])
        if 4 <= hour < 12:
            return "premarket"
        if 16 <= hour < 20:
            return "afterhours"
    except (ValueError, IndexError):
        pass
    return fallback_session


def _reconcile_broker_entry_orders(
    tctx, env, acc_id, fallback_session, state=None, today=None
):
    """Recover terminal ext-entry fills and expose active ext-entry orders.

    This is read-only for broker state. It never cancels an order. Filled orders
    without a local owner row are written once; conflicts are returned for triage.
    """
    rows = [
        row for row in query_order_rows(tctx, env, acc_id)
        if row.get("remark") == "ext_entry"
    ]
    active_orders = []
    conflicts = []
    for row in rows:
        # 2026-09-17 修复 (Hermes 复查实证): 跨日旧单不得写 {today}:{symbol} attempt.
        #   病灶: 本函数此前对**所有** remark=ext_entry 的历史单无条件写当日 attempt,
        #   而采集循环 431 行 `if attempt_key in attempts: continue` ⇒ 只要该标的历史上被下过
        #   ext_entry 单, 今天就被**静默跳过**(实测 US.NOK 因 ET 9/16 23:14 的旧测试单在 9/17 被封锁,
        #   且因每日重复回填而永久封锁 = 供给静默饿死). 修复: 只看订单自身 ET 日期是否为当日.
        #   活动单仍**无条件**计入 active_orders — 活单本来就该拦住新开仓, 与日期无关.
        # today 为空 = 调用方未要求按日过滤 (如 _execute 下单前的安全闸, 不传 state/today)
        #   ⇒ 保持旧行为(全量处理), 否则会把当天单也误判"跨日"而跳过。
        row_is_today = (not today) or (_order_et_date(row.get("create_time")) == today)
        attempt = None
        if row_is_today and isinstance(state, dict) and today:
            attempts = state.setdefault("attempts", {})
            attempt_key = f"{today}:{row['symbol']}"
            attempt = attempts.setdefault(attempt_key, {})
            attempt.update({
                "session": _order_session(row, fallback_session),
                "symbol": row["symbol"],
                "trade_date": today,
                "order_id": row["order_id"],
                "broker_status": row["status"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            })
        if row["status"] in ACTIVE_ORDER_STATUSES:
            if attempt is not None:
                attempt["status"] = "broker_active"
            active_orders.append(
                {
                    "symbol": row["symbol"],
                    "order_id": row["order_id"],
                    "status": row["status"],
                    "qty": row["qty"],
                    "filled_qty": row["filled_qty"],
                }
            )
            continue
        if not row_is_today:
            # 跨日旧单: 不写 attempt(防静默封锁), 也不按今日回填建仓(entry_time 会是错的).
            #   本链路未成交单 90s 内即撤, 正常不会有跨日成交; 真出现则只留痕, 由人工处置.
            print(f"[ext_entry] 对账跳过跨日旧单: {row['symbol']} {row['order_id']} "
                  f"status={row['status']} create_time={row.get('create_time')}")
            continue
        if row["filled_qty"] <= 0 or row["avg_fill_price"] <= 0:
            if attempt is not None:
                attempt["status"] = "failed"
                attempt["error"] = f"broker_{row['status']}"
            continue
        conn = sqlite3.connect(DB, timeout=30)
        try:
            existing = conn.execute(
                "SELECT id FROM trade_records WHERE futu_order_id=? "
                "AND owner_strategy='extended_hours_entry' LIMIT 1",
                (row["order_id"],),
            ).fetchone()
        finally:
            conn.close()
        if existing:
            if attempt is not None:
                attempt["status"] = "opened"
                attempt["filled_qty"] = row["filled_qty"]
                attempt["avg_fill_price"] = row["avg_fill_price"]
            continue
        try:
            _write_open_position(
                symbol=row["symbol"],
                qty=row["filled_qty"],
                entry_px=row["avg_fill_price"],
                decision_px=row["price"],
                session=_order_session(row, fallback_session),
                earn={},
                chg=0.0,
                vol=0.0,
                buy_order_id=row["order_id"],
            )
        except Exception as exc:
            if attempt is not None:
                attempt["status"] = "reconcile_conflict"
                attempt["error"] = str(exc)
            conflicts.append(
                {
                    "symbol": row["symbol"],
                    "order_id": row["order_id"],
                    "error": f"reconcile_write_failed:{exc}",
                }
            )
    return active_orders, conflicts


def _earnings_surprise(symbol):
    """Finnhub 最近财报超预期% (positive=beat). 事件门: 超预期>0 即过 (挡无财报的妖股).
    2026-09-17 用户裁方案A: 删新鲜度门 — period 是季度末非披露日, 新鲜度会误拒真催化;
    "当下正在反应"由价格门(涨≥2%)+量门(≥20万)承载, 财报只需证明"有基本面支撑"."""
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        c = _get_finnhub_client()
        if c is None:
            return None
        tick = symbol.replace("US.", "")
        e = c.company_earnings(tick, limit=1)
        if e and isinstance(e, list):
            sp = e[0].get("surprisePercent")
            period = e[0].get("period")
            return {"surprise_pct": sp, "period": period}
    except Exception:
        pass
    return None


def collect(session, dry=False):
    """扫描事件驱动开仓信号；当前 SIM 无券商 STOP 时 fail-closed。"""
    if session not in SESSION_WINDOWS:
        return [{"ok": False, "skip": "invalid_session"}]
    if not _is_trading_day() or not _in_session_window(session):
        return [{"ok": False, "skip": "outside_trading_window"}]

    # Futu SIM 已实测拒绝 STOP 与 STOP_LIMIT（DEB-20260805 §5.1）。
    # 2026-09-17 用户裁方案1: 接受规则引擎兜底止损, ALLOW_SIM_ENTRY 开关恢复下单.
    #   券商 STOP 不可用 → 止损改由 ①开盘规则引擎读 initial_sl ②盘前 tighten 由 exit executor 平仓.
    if not dry and not ALLOW_SIM_ENTRY and os.getenv("FUTU_TRD_ENV", "SIMULATE").upper() == "SIMULATE":
        return [{"ok": False, "skip": "simulate_stop_unsupported_fail_closed"}]

    if dry:
        held = _held_symbols()
        results = []
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        price_gate_rejects = []
        try:
            movers = _scan_movers(session, qctx)
            for item in movers:
                symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
                if symbol in held:
                    continue
                if not _price_in_range(price):
                    price_gate_rejects.append({"symbol": symbol, "price": price,
                                               "change_pct": round(change_pct, 2), "volume": volume})
                    continue
                if change_pct < ENTRY_CHANGE_PCT or volume < MIN_VOLUME:
                    continue
                earnings = _earnings_surprise(symbol)
                if not earnings or not earnings.get("surprise_pct") or earnings["surprise_pct"] <= 0:
                    continue
                results.append({
                    "ok": True, "dry": True, "symbol": symbol,
                    "qty": int(POSITION_USD / price), "price": price,
                    "session": session, "change_pct": round(change_pct, 2),
                    "volume": volume, "earnings": earnings,
                })
            _record_price_gate_rejects(session, price_gate_rejects)
        finally:
            qctx.close()
        return results or [{"skip": "no_signal_passed_all_gates"}]

    # REAL 通道当前未被本脚本授权；避免误用 SIM account 下 REAL 单。
    if os.getenv("FUTU_TRD_ENV", "SIMULATE").upper() != "SIMULATE":
        return [{"ok": False, "skip": "real_not_authorized"}]

    today = _today_et()
    with _state_lock() as locked:
        if not locked:
            return [{"ok": False, "skip": "another_executor_running"}]
        state = _load_state()
        state.setdefault("version", 2)
        state.setdefault("attempts", {})
        state.setdefault("opened_today", {})
        from futu import TrdEnv
        reconciliation_tctx = _open_trade_context()
        try:
            active_orders, reconcile_conflicts = _reconcile_broker_entry_orders(
                reconciliation_tctx, TrdEnv.SIMULATE,
                int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823")), session,
                state=state, today=today,
            )
        except Exception as exc:
            return [{
                "ok": False,
                "skip": "broker_order_reconcile_failed",
                "error": str(exc),
            }]
        finally:
            reconciliation_tctx.close()
        if reconcile_conflicts:
            _save_state(state)
            return [{
                "ok": False,
                "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            }]
        if active_orders:
            _save_state(state)
            return [{
                "ok": False,
                "skip": "active_ext_entry_order",
                "orders": active_orders,
            }]
        _save_state(state)
        attempts = state["attempts"]
        opened_today = state["opened_today"]
        day_key = f"{today}:{session}"
        total_today = _opened_today_total(opened_today, today)
        if total_today >= MAX_PER_DAY:
            return [{"skip": "daily_limit", "total_today": total_today}]

        held = _held_symbols()
        results = []
        price_gate_rejects = []
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        try:
            movers = _scan_movers(session, qctx)
            for item in movers:
                symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
                attempt_key = f"{today}:{symbol}"
                if attempt_key in attempts:
                    continue
                if symbol in held:
                    continue
                if not _price_in_range(price):
                    price_gate_rejects.append({"symbol": symbol, "price": price,
                                               "change_pct": round(change_pct, 2), "volume": volume})
                    continue
                if change_pct < ENTRY_CHANGE_PCT or volume < MIN_VOLUME:
                    continue
                earnings = _earnings_surprise(symbol)
                if not earnings or not earnings.get("surprise_pct") or earnings["surprise_pct"] <= 0:
                    continue

                qty = int(POSITION_USD / price)
                attempts[attempt_key] = {
                    "status": "reserved", "session": session, "symbol": symbol,
                    "trade_date": today, "reserved_at": datetime.utcnow().isoformat() + "Z",
                }
                _save_state(state)
                try:
                    result = _execute(symbol, qty, price, session, change_pct, volume, earnings, dry)
                except Exception as exc:
                    result = {
                        "ok": False, "symbol": symbol, "session": session,
                        "error": f"executor_exception:{exc}",
                    }
                    _push(f"❌ 延长时段开仓执行器异常\n{symbol}\n{exc}")
                    _audit(result)
                result["symbol"] = symbol
                result["session"] = session
                attempts[attempt_key] = {
                    "status": "opened" if result.get("ok") else "failed",
                    "session": session,
                    "trade_date": today,
                    "order_id": result.get("buy_order_id"),
                    "stop_order_id": result.get("stop_order_id"),
                    "filled_qty": result.get("filled_qty"),
                    "avg_fill_price": result.get("avg_fill_price"),
                    "error": result.get("error") or result.get("skip"),
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                results.append(result)
                if result.get("ok"):
                    total_today += 1
                    opened_today[day_key] = opened_today.get(day_key, 0) + 1
                _save_state(state)
                if total_today >= MAX_PER_DAY:
                    break
            _record_price_gate_rejects(session, price_gate_rejects)
        finally:
            qctx.close()
    return results or [{"skip": "no_signal_passed_all_gates"}]





def _write_open_position(symbol, qty, entry_px, decision_px, session, earn, chg, vol, buy_order_id=""):
    """成交后写 open_positions + trade_records (盘中监控读 trade_records.metadata_json.stop_loss).
    返回 position_id. 幂等且不覆盖既有仓位."""
    import uuid
    conn = sqlite3.connect(DB, timeout=30)
    try:
        now = _now_et().isoformat()
        pos_id = f"EXT-{uuid.uuid4().hex[:12]}"
        init_sl = round(entry_px * (1 + STOP_PCT / 100), 2)  # 规则引擎盘中兜底读这个
        meta = json.dumps({"session": session, "decision_px": decision_px,
                           "position_id": pos_id, "entry_order_id": buy_order_id,
                           "earnings_surprise_pct": earn.get("surprise_pct"),
                           "earnings_period": earn.get("period"),
                           "ext_change_pct": chg, "ext_volume": vol,
                           "cohort": "B",  # 2026-09-17 用户裁: EXT 仓可隔夜 (cohort B + PLAYBOOK_SIM 默认允许, 盈利跳过 PRE_CLOSE_EXIT)
                           "stop_loss": init_sl,           # 盘中 monitor_playbook_sim_exits 读此键做 SL
                           "stop_source": "rule_engine_initial_sl"}, ensure_ascii=False)
        conn.execute("BEGIN IMMEDIATE")
        existing_order = None
        if buy_order_id:
            existing_order = conn.execute(
                "SELECT id FROM trade_records WHERE futu_order_id=? "
                "AND owner_strategy='extended_hours_entry' LIMIT 1",
                (buy_order_id,),
            ).fetchone()
        if existing_order:
            conn.commit()
            existing_position = conn.execute(
                "SELECT position_id FROM open_positions WHERE symbol=? AND size>0",
                (symbol,),
            ).fetchone()
            return existing_position[0] if existing_position else pos_id
        existing_position = conn.execute(
            "SELECT 1 FROM open_positions WHERE symbol=? AND size>0", (symbol,)
        ).fetchone()
        if existing_position:
            conn.rollback()
            raise RuntimeError(f"open position already exists for {symbol}")
        conn.execute(
            "INSERT INTO open_positions (position_id, symbol, direction, size, avg_entry_price, entry_time, "
            " strategy_id, source, owner_strategy, owner, mode, account_type, trader_type, "
            " is_filled, version, updated_at, metadata_json, sync_status, "
            " initial_sl, current_sl, stop_loss_price) "
            "VALUES (?, ?, 'LONG', ?, ?, ?, 'extended_hours_entry', 'extended_hours_entry', "
            " 'extended_hours_entry', 'extended_hours_entry', 'normal', 'simulate', 'AUTO', 1, 1, ?, ?, 'pending', "
            " ?, ?, ?)",
            (pos_id, symbol, qty, entry_px, now, now, meta, init_sl, init_sl, init_sl))
        # P0-3 完整闭环: 写 trade_records — 盘中退出监控只读此表 (source_type PLAYBOOK_SIM 未平仓)
        conn.execute(
            "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, entry_price, "
            " quantity, source_type, trade_mode, mode, owner_strategy, strategy_id, "
            " futu_order_id, metadata_json, created_at, exit_price, exit_time) "
            "VALUES (?, ?, 'LONG', ?, ?, ?, 'PLAYBOOK_SIM', 'simulate', 'futu_simulate', "
            " 'extended_hours_entry', 'extended_hours_entry', ?, ?, ?, 0, '')",
            (_today_et(), symbol, now, entry_px, qty, buy_order_id, meta, now))
        conn.commit()
        return pos_id
    finally:
        conn.close()


def _execute(symbol, qty, price, session, chg, vol, earn, dry):
    """SIM 真下单: 买入→等成交→写 open_positions(含 initial_sl 规则引擎兜底). 用户裁方案1.
    SIM 无券商STOP, 止损靠 ①开盘 monitor_playbook_sim_exits 读 initial_sl ②盘前 tighten exit executor."""
    trd_env = os.getenv("FUTU_TRD_ENV", "SIMULATE")
    acc_id = int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823"))
    if trd_env.upper() != "SIMULATE":
        _push(f"🚫 延长时段开仓拦截: {symbol} REAL未授权")
        return {"ok": False, "skip": "real_not_authorized"}

    rec = {"symbol": symbol, "qty": qty, "price": price, "session": session,
           "change_pct": round(chg, 2), "volume": vol, "earnings": earn, "position_usd": POSITION_USD}
    from futu import TrdSide, OrderType, TrdEnv, TimeInForce, Session, RET_OK
    tctx = _open_trade_context()
    try:
        env = TrdEnv.SIMULATE
        # 显式传当日: 下单前安全闸只对**当日**已成交无主的单做建仓回补/冲突检测,
        #   跨日旧单不回补(entry_time 会是错的) — 与 collect() 同一口径。
        active_orders, reconcile_conflicts = _reconcile_broker_entry_orders(
            tctx, env, acc_id, session, today=_today_et()
        )
        if reconcile_conflicts:
            rec.update({
                "ok": False, "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            })
            _audit(rec)
            return rec
        if active_orders:
            rec.update({
                "ok": False, "skip": "active_ext_entry_order", "orders": active_orders,
            })
            _audit(rec)
            return rec
        ret, data = tctx.place_order(price=price, qty=qty, code=symbol, trd_side=TrdSide.BUY,
                                     order_type=OrderType.NORMAL, trd_env=env, acc_id=acc_id,
                                     time_in_force=TimeInForce.DAY, session=Session.ETH, remark="ext_entry")
        if ret != RET_OK:
            rec["ok"] = False
            rec["error"] = str(data)[:120]
            _push(f"❌ 盘前开仓下单失败\n{symbol} {qty}股 @ ${price:.2f}\n{rec['error']}")
            _audit(rec)
            return rec
        buy_oid = str(data["order_id"].values[0]) if data is not None else ""
        rec["buy_order_id"] = buy_oid

        # 等成交；部分成交则撤掉剩余并等待券商确认终态。
        fill = wait_order_fill(tctx, env, acc_id, buy_oid, qty, price)
        rec["fill"] = fill
        if not fill.get("cancel_ok"):
            rec["ok"] = False
            rec["error"] = f"buy_cancel_failed_{fill.get('status')}:{fill.get('error')}"
            _push(f"⚠️ 盘前开仓撤单未确认\n{symbol} 订单 {buy_oid}\n状态 {fill.get('status')}")
            _audit(rec)
            return rec
        if not fill.get("filled"):
            rec["ok"] = False
            rec["error"] = f"buy_not_filled_{fill.get('status')}"
            _push(f"⏳ 盘前开仓未成交已撤单\n{symbol} {qty}股 @ ${price:.2f}\n状态 {fill.get('status')}")
            _audit(rec)
            return rec

        fqty = float(fill["filled_qty"]); fpx = float(fill["avg_fill_price"])
        rec.update({"filled_qty": fqty, "avg_fill_price": fpx})
        # 成交 → 写 open_positions + trade_records (盘中监控闭环; metadata.stop_loss 供 SL 判定)
        try:
            pos_id = _write_open_position(symbol, fqty, fpx, price, session, earn, chg, vol, buy_oid)
        except Exception as exc:
            rec["ok"] = False
            rec["error"] = f"position_db_write_failed:{exc}"
            _push(f"🚨 盘前开仓已成交但本地建仓失败\n{symbol} 订单 {buy_oid}\n{exc}")
            _audit(rec)
            return rec
        rec["position_id"] = pos_id
        rec["initial_sl"] = round(fpx * (1 + STOP_PCT / 100), 2)
        rec["ok"] = True
        _audit(rec)
        _push(f"✅ 盘前事件开仓已成交\n{symbol} {fqty:g}股 @ ${fpx:.2f} (${fqty*fpx:,.0f})\n"
              f"财报超预期 +{earn['surprise_pct']:.1f}% | 盘前 {chg:+.2f}% 量{vol:,.0f}\n"
              f"止损(规则引擎) initial_sl=${rec['initial_sl']:.2f} (-5%)\n"
              f"⚠️ SIM无券商STOP, 盘中由 monitor_playbook_sim_exits 兜底")
        return rec
    finally:
        tctx.close()



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours"])
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    rs = collect(args.session, args.dry)
    print(json.dumps(rs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
