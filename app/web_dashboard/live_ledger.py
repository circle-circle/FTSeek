r"""实盘券商成交流水层 + 每日持仓快照层 (RFC-078 批B, 2026-09-08).

## 为什么需要"第二层账本"

本仓原有账本是**回合级**的 `trade_records` (一行 = 一次开→平), 由下单/平仓代码在成交
当场写。它在实盘侧**断过链**: `manual_order_service._record_manual_close` 的委派签名自
2026-06-19 (768e443f) 起少一个 `entry_reason=` 形参 ⇒ 每次手动平仓抛 TypeError, 被
`order/executor.py` 的 `except Exception` 降级成一行 warning ⇒ **券商侧已平仓、账本侧
零记录** (已于 33d5568c 修复, 但 2026-06-19~09-08 的历史无法从本地回补)。

港股更彻底: 此前**不进任何 DB**, 只有 `data/audit/hk_manual_orders.jsonl` 的
submit/cancel 留痕 (63 行, 无成交价/量/盈亏), 而券商侧港股是真钱真有仓
(2026-09-08 实测总资产 HK$715,975.88, HK.02513 浮亏 −33,900)。

⇒ 需要一层**不依赖下单代码是否记得写**的账本: 直接向券商要成交流水。本模块就是这层。
两层分工:
  · 券商真值层 (本模块): `live_deal_records` 逐笔成交 + `live_position_snapshots` 每日仓位
    —— 券商怎么说就怎么记, 是**执行事实**;
  · 回合台账层 (既有): `trade_records` —— 带 exit_reason / thesis / attribution, 是**决策记录**。
  周报两层都用: 成交笔数/现金流/期末仓位取自真值层, 退出原因与决策归因取自台账层。

## 一个必须记档的实测发现 (它改变了表设计)

首版把表建成 `hk_trade_records` 并假定"港股 ctx 只返回港股成交"。**实测为假**:
`hk_order_service` 的 ctx 是 `filter_trdmarket=TrdMarket.HK`, 但账户是**综合账户**,
`history_deal_list_query` 一次返回 99 笔里 **HK 47 笔 + US 52 笔** (2026-07-10~09-08)。
若按首版落表, 美股成交会被记进"港股表"、且 `currency` 被硬编码成 HKD ⇒
**跨市场污染 + 币种错记**, 正是本 RFC 要消灭的那类错误。
故: 表名改为市场中性的 `live_deal_records`, `market` / `currency` 由**每行自带的
`deal_market` 字段**推导 (而非由"哪个 ctx 返回的"推导), 且**两个市场的 ctx 都查** ——
upsert 以 `deal_id` 为幂等键, 两腿重叠返回同一笔也只落一行。

## 第二个实测发现: `create_time` 是**各市场本地时** (它决定了窗口与快照日的取法)

2026-09-08 对已回补的 99 笔做小时直方图: 港股 47 笔全落 09-16 点 (HKT 盘中 09:30-16:00),
美股 52 笔集中在 09-16 点、并带 `04:02` (ET 盘前起点) 与 `00:26 / 22:xx / 23:xx`
(ET 夜盘段) 的尾巴 ⇒ 美股那腿**不是** HKT。故:
  · `deal_date = create_time[:10]` 就是**该市场的交易日**, 不需再换算 (换算反而会错);
  · 但**查询窗口**与**快照日**必须按各市场本地日取 —— 服务器是 CST(UTC+8), 比 ET 快
    12~13h, 用不带尺的裸 now 取"今天"会在 ET 上午把窗口整体推后一天, 边界那天
    的真钱成交静默漏掉 (正是 tests/test_naive_datetime_now_ratchet.py 要拦的病灶族)。
  · 港股本地日 == 服务器 CST 日 (HKT 与 CST 同为 UTC+8), 故 HK 腿用 `now_cst()`。
`synced_at` 例外: 它是"我们什么时候去拉的"的审计时刻而非交易口径, 存 **UTC** 带
`+00:00` 偏移 (自描述、无 DST、单调), 不与任何交易日列混用。

## 纪律 (与本仓既有约定一致)

1. **三态, 绝不伪装空**: 券商不可达/查询失败 ⇒ 该腿 `UNAVAILABLE` + detail,
   **不得**降级成 `NO_DEALS` (后者语义是"窗口内确实零成交")。两条腿各自独立判态,
   一条挂了不得掩盖另一条 ⇒ overall ∈ {OK, PARTIAL, UNAVAILABLE}。
   持仓快照同法: `per_market[m] = -1` 表示"本腿未更新", 与 `0` (真的空仓) 严格区分。
2. **不伪造数据**: 富途 deal 级返回**不含手续费** (实测 13 列: code/stock_name/
   deal_market/deal_id/order_id/qty/price/trd_side/create_time/counter_broker_id/
   counter_broker_name/status/jp_acc_type) ⇒ `fee` 存 **NULL** 而不是 0;
   `amount = qty*price` 是**成交额非净额**, 周报必须如实标注口径。
3. **deal_id 存 TEXT**: 实测形如 `4657813206881206341` (19 位 > 2^53), 经 float 一转
   就静默变位 ⇒ 幂等键失效 ⇒ 同一笔反复入账。
4. **必须 disconnect**: futu OpenD 上下文持**非守护线程**, 不 close 则 cron 脚本跑完
   也不退出 (实测被 timeout 杀, exit=124)。

回滚: `DROP TABLE live_deal_records; DROP TABLE live_position_snapshots;` + 删本文件
      (纯增量, 不改任何既有读写路径)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.utils.timezone import now_cst, now_et, now_utc

logger = logging.getLogger("ftseek_us.live_ledger")

MARKETS: Tuple[str, ...] = ("US", "HK")
_CURRENCY_BY_MARKET = {"HK": "HKD", "US": "USD"}

# ── 时间尺度 (见文件头"第二个实测发现") ─────────────────────────────
_TZ_LABEL = {"HK": "HKT", "US": "ET"}


def market_now(market: str):
    """该市场的本地"现在" (aware; 刻意不用不带尺的裸 now = 服务器 CST 墙钟)."""
    return now_cst() if market == "HK" else now_et()


def market_today(market: str) -> str:
    """该市场的本地日 YYYY-MM-DD (= 富途 create_time 的日粒度口径)."""
    return market_now(market).strftime("%Y-%m-%d")


def window_for(market: str, days: int) -> Tuple[str, str]:
    """该市场的查询/读取窗口 [start, end] (本地日, 闭区间).

    start 比"days 天前"再放宽 1 天: 富途窗口是**日**粒度, 而 upsert 以 deal_id 为
    幂等键 ⇒ 放宽只会多覆盖边界日、不产生重复行; 反之少一天会**静默漏掉**一整天成交。
    """
    end = market_now(market).date()
    start = end - timedelta(days=max(int(days), 1))
    return start.isoformat(), end.isoformat()


_DDL_DEALS = """
CREATE TABLE IF NOT EXISTS live_deal_records (
    deal_id             TEXT PRIMARY KEY,
    market              TEXT NOT NULL,
    currency            TEXT,
    order_id            TEXT,
    symbol              TEXT NOT NULL,
    stock_name          TEXT,
    deal_market         TEXT,
    trd_side            TEXT,
    qty                 REAL,
    price               REAL,
    amount              REAL,
    fee                 REAL,
    deal_time           TEXT,
    deal_date           TEXT,
    counter_broker_id   TEXT,
    counter_broker_name TEXT,
    status              TEXT,
    synced_at           TEXT
)
"""

_DDL_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS live_position_snapshots (
    snapshot_date TEXT NOT NULL,
    market        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    stock_name    TEXT,
    qty           REAL,
    can_sell_qty  REAL,
    cost_price    REAL,
    cur_price     REAL,
    market_val    REAL,
    pl_val        REAL,
    pl_ratio      REAL,
    position_side TEXT,
    currency      TEXT,
    synced_at     TEXT,
    PRIMARY KEY (snapshot_date, market, symbol)
)
"""

_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_live_deals_mkt_date ON live_deal_records(market, deal_date)",
    "CREATE INDEX IF NOT EXISTS idx_live_deals_symbol ON live_deal_records(symbol, deal_date)",
    "CREATE INDEX IF NOT EXISTS idx_live_snap_mkt_date ON live_position_snapshots(market, snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_live_rt_mkt_exit ON live_round_trips(market, exit_date)",
    "CREATE INDEX IF NOT EXISTS idx_live_rt_symbol ON live_round_trips(symbol, exit_date)",
)


def _db_path() -> Path:
    """实盘台账库 = 看板库 (RFC-078 批A 后恒 live 库); 与 sim 数据收集仓物理隔离."""
    from app.core.db_utils import get_dashboard_db
    return get_dashboard_db()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_tables(db_path: Optional[Path] = None) -> None:
    """幂等建表 (DDL 全部 IF NOT EXISTS, cron 每次跑都调也安全)."""
    conn = sqlite3.connect(str(db_path or _db_path()), timeout=30)
    try:
        conn.execute(_DDL_DEALS)
        conn.execute(_DDL_SNAPSHOTS)
        conn.execute(_DDL_ROUND_TRIPS)
        for stmt in _IDX:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _s(row: Dict[str, Any], key: str) -> Optional[str]:
    """安全取字符串 (None/NaN/'N/A' → None, 不落成 'nan' 字面量)."""
    v = row.get(key)
    if v is None:
        return None
    s = str(v)
    return None if s in ("", "nan", "None", "N/A") else s


def _f(row: Dict[str, Any], key: str) -> Optional[float]:
    v = row.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN 自检


def market_of(deal_market: Optional[str], symbol: Optional[str]) -> str:
    """按**每行自带的** deal_market 推市场, 回落 symbol 前缀.

    刻意不按"哪个 ctx 返回的"推: 综合账户下一次查询会同时返回两市场成交 (实测),
    按 ctx 推会把美股成交记成港股。
    """
    dm = (deal_market or "").upper()
    if dm in MARKETS:
        return dm
    sym = (symbol or "").upper()
    if sym.startswith("HK."):
        return "HK"
    if sym.startswith("US."):
        return "US"
    return dm or "OTHER"


# ── 成交流水同步 ──────────────────────────────────────────────────

def _fetch_deals_from(market: str, start: str, end: str) -> Dict[str, Any]:
    """向单个市场的券商 ctx 要成交流水 → 三态 dict (失败必带 detail, 不返回空列表充数)."""
    from futu import RET_OK, TrdEnv

    svc = None
    try:
        if market == "HK":
            from app.web_dashboard.hk_order_service import get_hk_order_service
            svc = get_hk_order_service()
        else:
            from app.web_dashboard.manual_order_service import get_manual_order_service
            svc = get_manual_order_service()
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNAVAILABLE", "reason": "import_failed", "detail": str(exc),
                "records": []}

    try:
        if not svc.connect():
            return {"status": "UNAVAILABLE", "reason": "broker_connect_failed",
                    "detail": f"{market} broker 连接/解锁失败 (OpenD 或 FUTU_UNLOCK_PWD_MD5)",
                    "records": []}
        ret, data = svc._broker_ctx.history_deal_list_query(
            start=start, end=end, trd_env=TrdEnv.REAL, acc_id=svc._acc_id(),
        )
        if ret != RET_OK:
            return {"status": "UNAVAILABLE", "reason": "history_deal_query_failed",
                    "detail": str(data), "records": []}
        records = data.to_dict("records") if hasattr(data, "to_dict") else list(data or [])
        return {"status": "OK", "records": records}
    except Exception as exc:  # noqa: BLE001 — 券商侧任何异常都不得伪装成"零成交"
        logger.warning("[实盘台账] %s 成交查询异常: %s", market, exc)
        return {"status": "UNAVAILABLE", "reason": "exception", "detail": str(exc),
                "records": []}
    finally:
        # 必须显式断开: futu OpenD 上下文持非守护线程, 不 close 则脚本跑完也不退出
        try:
            if svc is not None:
                svc.disconnect()
        except Exception as exc:  # noqa: BLE001 — 清理路径不得再抛, 但必须留痕 (R5)
            logger.warning("[实盘台账] %s 成交腿 disconnect 失败 (futu 非守护线程可能残留, "
                           "进程或需外部 timeout 兜底): %s", market, exc)


def _count_deals() -> int:
    try:
        conn = _connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM live_deal_records").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0


def sync_deals(days: int = 30) -> Dict[str, Any]:
    """回补最近 `days` 天的实盘成交流水 (两市场都查; 幂等 upsert by deal_id).

    综合账户下两腿会返回重叠集合, upsert 以 deal_id 为键 ⇒ 重叠不产生重复行。
    """
    # 每市场各自的本地日窗口 (不共用一个"服务器今天"): 见文件头第二个实测发现。
    windows = {m: window_for(m, days) for m in MARKETS}
    union = (min(w[0] for w in windows.values()), max(w[1] for w in windows.values()))

    legs: Dict[str, Any] = {}
    pooled: Dict[str, Dict[str, Any]] = {}
    for market in MARKETS:
        leg = _fetch_deals_from(market, *windows[market])
        legs[market] = {k: v for k, v in leg.items() if k != "records"}
        legs[market]["fetched"] = len(leg.get("records") or [])
        legs[market]["window"] = list(windows[market])
        for raw in leg.get("records") or []:
            # deal_id 必须走 str(): 19 位整数经 float 会静默变位 ⇒ 幂等键失效
            deal_id = _s(raw, "deal_id")
            if not deal_id:
                logger.warning("[实盘台账] 跳过无 deal_id 的成交行: %s", raw)
                continue
            pooled[deal_id] = raw

    bad = [m for m, v in legs.items() if v.get("status") == "UNAVAILABLE"]
    if len(bad) == len(MARKETS):
        return {"status": "UNAVAILABLE", "window_days": int(days), "window": list(union),
                "windows": windows,
                "legs": legs, "unavailable_legs": bad, "inserted": 0, "updated": 0,
                "total_rows": 0,
                "detail": "两个市场的券商腿均不可达 —— 台账本次未更新, 这**不是**零成交"}

    if not pooled:
        return {"status": "PARTIAL" if bad else "NO_DEALS", "window_days": int(days),
                "window": list(union), "windows": windows,
                "legs": legs, "unavailable_legs": bad,
                "inserted": 0, "updated": 0, "total_rows": _count_deals()}

    ensure_tables()
    # 审计时刻 (不是交易口径) ⇒ UTC 带偏移, 自描述且无 DST 跳变
    now = now_utc().isoformat(timespec="seconds")
    per_market: Dict[str, int] = {m: 0 for m in MARKETS}
    unknown_market = inserted = updated = 0
    conn = _connect()
    try:
        # 先取既存 deal_id 集合再逐行分类: upsert 的 rowcount 对"新增"与"覆写"都是 1,
        # 靠它区分 inserted/updated 是空转 (首版就写了个恒 False 的辅助函数, 已删)。
        existing = {r[0] for r in conn.execute("SELECT deal_id FROM live_deal_records")}
        for deal_id, raw in pooled.items():
            symbol = _s(raw, "code")
            market = market_of(_s(raw, "deal_market"), symbol)
            qty, price = _f(raw, "qty"), _f(raw, "price")
            amount = round(qty * price, 4) if qty is not None and price is not None else None
            deal_time = _s(raw, "create_time")
            currency = _CURRENCY_BY_MARKET.get(market)
            if currency is None:
                # 未知市场 ⇒ 币种留 NULL, 不按 HKD/USD 猜 (猜错会让金额跨币种相加)
                unknown_market += 1
            conn.execute(
                """INSERT INTO live_deal_records (
                       market, currency, order_id, symbol, stock_name, deal_market,
                       trd_side, qty, price, amount, fee, deal_time, deal_date,
                       counter_broker_id, counter_broker_name, status, synced_at, deal_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(deal_id) DO UPDATE SET
                       market=excluded.market, currency=excluded.currency,
                       order_id=excluded.order_id, symbol=excluded.symbol,
                       stock_name=excluded.stock_name, trd_side=excluded.trd_side,
                       qty=excluded.qty, price=excluded.price, amount=excluded.amount,
                       deal_time=excluded.deal_time, deal_date=excluded.deal_date,
                       status=excluded.status, synced_at=excluded.synced_at""",
                (market, currency, _s(raw, "order_id"), symbol, _s(raw, "stock_name"),
                 _s(raw, "deal_market"), _s(raw, "trd_side"), qty, price, amount,
                 None,  # fee: 券商 deal 级不提供 ⇒ NULL, 不伪造 0
                 deal_time, (deal_time or "")[:10] or None,
                 _s(raw, "counter_broker_id"), _s(raw, "counter_broker_name"),
                 _s(raw, "status"), now, deal_id),
            )
            per_market[market] = per_market.get(market, 0) + 1
            if deal_id in existing:
                updated += 1
            else:
                inserted += 1
                existing.add(deal_id)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM live_deal_records").fetchone()[0]
    finally:
        conn.close()

    return {"status": "PARTIAL" if bad else "OK", "window_days": int(days),
            "window": list(union), "windows": windows,
            "legs": legs, "unavailable_legs": bad,
            "fetched_unique": len(pooled), "inserted": inserted, "updated": updated,
            "per_market": per_market, "total_rows": total,
            "unknown_market_rows": unknown_market, "synced_at": now}


# ── 持仓快照同步 ──────────────────────────────────────────────────

def _fetch_positions_hk() -> Dict[str, Any]:
    from app.web_dashboard.hk_order_service import get_hk_order_service
    svc = get_hk_order_service()
    try:
        # 契约实测: {"rows": [...], "error": None|str} —— 首版按 {"ok":…,"positions":…} 读,
        # 结果**拿着 3 个真持仓报 UNAVAILABLE**; 读错契约比读不到更坏 (带着数据报失败,
        # 会让人去查券商而不是查自己的代码)。
        res = svc.get_positions_with_status()
        if res.get("error"):
            return {"status": "UNAVAILABLE", "reason": "positions_failed",
                    "detail": str(res["error"]), "rows": []}
        rows = [{"symbol": _s(p, "symbol"), "stock_name": _s(p, "name"),
                 "qty": _f(p, "qty"), "can_sell_qty": _f(p, "can_sell_qty"),
                 "cost_price": _f(p, "cost_price"), "cur_price": _f(p, "cur_price"),
                 "market_val": _f(p, "market_val"), "pl_val": _f(p, "pl_val"),
                 "pl_ratio": _f(p, "pl_ratio"), "position_side": _s(p, "position_side")}
                for p in (res.get("rows") or [])]
        return {"status": "OK", "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNAVAILABLE", "reason": "exception", "detail": str(exc), "rows": []}
    finally:
        try:
            svc.disconnect()
        except Exception as exc:  # noqa: BLE001 — 清理路径不得再抛, 但必须留痕 (R5)
            logger.warning("[实盘台账] HK 持仓腿 disconnect 失败 (futu 非守护线程可能残留): %s",
                           exc)


def _fetch_positions_us() -> Dict[str, Any]:
    from futu import RET_OK

    from app.web_dashboard.manual_order_service import get_manual_order_service
    svc = get_manual_order_service()
    try:
        if not svc.connect():
            return {"status": "UNAVAILABLE", "reason": "broker_connect_failed",
                    "detail": "US broker 连接/解锁失败", "rows": []}
        ret, data = svc._broker_ctx.position_list_query(
            trd_env=svc._trd_env(), acc_id=svc._acc_id())
        if ret != RET_OK:
            return {"status": "UNAVAILABLE", "reason": "query_failed",
                    "detail": str(data), "rows": []}
        rows = []
        for _, r in data.iterrows():
            code = str(r.get("code") or "")
            if not code.startswith("US."):
                continue  # 综合账户会返回全市场, 本腿只认美股
            qty = _f(r, "qty")
            if not qty:
                continue  # 券商返回的 0 仓行不入快照 (与港股腿同口径)
            rows.append({"symbol": code, "stock_name": _s(r, "stock_name"), "qty": qty,
                         "can_sell_qty": _f(r, "can_sell_qty"), "cost_price": _f(r, "cost_price"),
                         "cur_price": _f(r, "nominal_price"), "market_val": _f(r, "market_val"),
                         "pl_val": _f(r, "pl_val"), "pl_ratio": _f(r, "pl_ratio"),
                         "position_side": _s(r, "position_side")})
        return {"status": "OK", "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNAVAILABLE", "reason": "exception", "detail": str(exc), "rows": []}
    finally:
        try:
            svc.disconnect()
        except Exception as exc:  # noqa: BLE001 — 清理路径不得再抛, 但必须留痕 (R5)
            logger.warning("[实盘台账] US 持仓腿 disconnect 失败 (futu 非守护线程可能残留): %s",
                           exc)


def sync_position_snapshots(snapshot_date: Optional[str] = None) -> Dict[str, Any]:
    """落两市场的当日持仓快照 (幂等 upsert by (snapshot_date, market, symbol)).

    为什么需要: 成交只有流水, 没有成本基准与浮动盈亏; 而实盘在仓此前**两地都不落库**
    (live 库 open_positions 8 行全是 size=0 的 closed_by_sync 墓碑) ⇒
    "这周仓位怎么变的" 无从回答。
    """
    # 快照日按**各市场本地日**取: 主键是 (snapshot_date, market, symbol) ⇒ 两腿本可以各有各的日,
    # 而统一用服务器 CST 日会把美股当日仓位标成 ET 的"明天" (CST 快 12~13h)。
    # 显式传 snapshot_date 时两腿同串 —— 那是回归/复算用的确定性入口。
    days_by_market = {m: (snapshot_date or market_today(m)) for m in MARKETS}
    # HK 本地日恒 ≥ ET 本地日 (UTC+8 vs UTC-4/-5) ⇒ 取 HK 日作"本次运行"的标签日
    label_day = snapshot_date or days_by_market["HK"]
    fetchers = {"HK": _fetch_positions_hk, "US": _fetch_positions_us}
    legs: Dict[str, Any] = {m: fetchers[m]() for m in MARKETS}

    bad = [m for m in MARKETS if legs[m].get("status") == "UNAVAILABLE"]
    if len(bad) == len(MARKETS):
        return {"status": "UNAVAILABLE", "snapshot_date": label_day,
                "snapshot_dates": days_by_market, "legs": legs,
                "unavailable_legs": bad, "rows_today": 0, "per_market": {},
                "detail": "两市场持仓查询均失败 —— 这**不是**空仓"}

    ensure_tables()
    now = now_utc().isoformat(timespec="seconds")  # 审计时刻 ⇒ UTC 带偏移, 见文件头
    per_market: Dict[str, int] = {}
    conn = _connect()
    try:
        for market in MARKETS:
            day = days_by_market[market]
            leg = legs[market]
            if leg.get("status") == "UNAVAILABLE":
                per_market[market] = -1  # -1 = 本腿未更新; 与 0 (真的空仓) 严格区分
                continue
            n = 0
            for p in leg.get("rows") or []:
                if not p.get("symbol"):
                    continue
                conn.execute(
                    """INSERT INTO live_position_snapshots (
                           snapshot_date, market, symbol, stock_name, qty, can_sell_qty,
                           cost_price, cur_price, market_val, pl_val, pl_ratio,
                           position_side, currency, synced_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(snapshot_date, market, symbol) DO UPDATE SET
                           stock_name=excluded.stock_name, qty=excluded.qty,
                           can_sell_qty=excluded.can_sell_qty,
                           cost_price=excluded.cost_price, cur_price=excluded.cur_price,
                           market_val=excluded.market_val, pl_val=excluded.pl_val,
                           pl_ratio=excluded.pl_ratio,
                           position_side=excluded.position_side,
                           synced_at=excluded.synced_at""",
                    (day, market, p["symbol"], p.get("stock_name"), p.get("qty"),
                     p.get("can_sell_qty"), p.get("cost_price"), p.get("cur_price"),
                     p.get("market_val"), p.get("pl_val"), p.get("pl_ratio"),
                     p.get("position_side"), _CURRENCY_BY_MARKET.get(market), now),
                )
                n += 1
            per_market[market] = n
        conn.commit()
        total = 0
        for m in MARKETS:
            total += conn.execute(
                "SELECT COUNT(*) FROM live_position_snapshots "
                "WHERE market=? AND snapshot_date=?", (m, days_by_market[m])).fetchone()[0]
    finally:
        conn.close()

    return {"status": "PARTIAL" if bad else "OK", "snapshot_date": label_day,
            "snapshot_dates": days_by_market,
            "unavailable_legs": bad, "rows_today": total, "per_market": per_market}


def sync_all(days: int = 30) -> Dict[str, Any]:
    """成交 + 持仓快照 + **派生回合** 一并回补 (cron 入口 / POST 端点). 各腿各自三态, 互不掩盖.

    派生回合放在这里而不是各消费方各派一次: 派生是**本地只读计算 + 幂等落表**, 跟着回补走
    ⇒ 台账一更新, 看板与周报看到的回合就是同一份 (否则会出现"成交是新的、回合是旧的")。
    `overall` **只由两条券商腿决定** —— 派生失败不得把券商回补的成功掩盖成 PARTIAL,
    反之派生的 status 单独如实上报。
    """
    out: Dict[str, Any] = {"deals": sync_deals(days),
                           "positions": sync_position_snapshots()}
    statuses = [out["deals"].get("status"), out["positions"].get("status")]
    if all(s == "UNAVAILABLE" for s in statuses):
        out["overall"] = "UNAVAILABLE"
    elif any(s in ("UNAVAILABLE", "PARTIAL") for s in statuses):
        out["overall"] = "PARTIAL"
    else:
        out["overall"] = "OK"
    try:
        out["round_trips"] = sync_round_trips()
    except Exception as exc:  # noqa: BLE001
        # 不静默吞: 派生失败 = 回合段会停留在上一次的旧数据而读者无从得知 (三态纪律)
        logger.warning("[实盘台账] 回合派生失败 (成交/快照回补不受影响): %s", exc)
        out["round_trips"] = {"status": "DERIVE_FAILED", "detail": str(exc),
                              "total_rows": None}
    return out


# ── 只读查询 (供周报/接口使用, 不连券商) ────────────────────────────

def read_deals(start: str, end: str, market: Optional[str] = None) -> List[Dict[str, Any]]:
    """按 `deal_date` 闭区间读成交流水 (纯本地 ⇒ 券商挂了周报照样能出, 只标注口径)."""
    ensure_tables()
    sql = ("SELECT * FROM live_deal_records WHERE deal_date BETWEEN ? AND ?"
           + (" AND market = ?" if market else "") + " ORDER BY deal_time ASC")
    params: Tuple[Any, ...] = (start, end, market) if market else (start, end)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def read_snapshots(start: str, end: str, market: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_tables()
    sql = ("SELECT * FROM live_position_snapshots WHERE snapshot_date BETWEEN ? AND ?"
           + (" AND market = ?" if market else "")
           + " ORDER BY snapshot_date ASC, market ASC, symbol ASC")
    params: Tuple[Any, ...] = (start, end, market) if market else (start, end)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def latest_snapshot(market: str, on_or_before: Optional[str] = None) -> List[Dict[str, Any]]:
    """某市场**最近一个有快照的日**的仓位 (不是"今天", 避免周末/假日误报空仓)."""
    ensure_tables()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT MAX(snapshot_date) FROM live_position_snapshots WHERE market=?"
            + (" AND snapshot_date <= ?" if on_or_before else ""),
            (market, on_or_before) if on_or_before else (market,),
        ).fetchone()
        day = row[0] if row else None
        if not day:
            return []
        return [dict(r) for r in conn.execute(
            "SELECT * FROM live_position_snapshots WHERE market=? AND snapshot_date=? "
            "ORDER BY symbol", (market, day)).fetchall()]
    finally:
        conn.close()


def ledger_coverage() -> Dict[str, Any]:
    """台账覆盖面自检 — 让"某市场零留痕"这类缺口以后能被一行查询发现."""
    ensure_tables()
    conn = _connect()
    try:
        out: Dict[str, Any] = {"db": str(_db_path()), "deals": {}, "snapshots": {}}
        for m in MARKETS:
            d = conn.execute(
                "SELECT COUNT(*), MIN(deal_date), MAX(deal_date), COUNT(DISTINCT symbol) "
                "FROM live_deal_records WHERE market=?", (m,)).fetchone()
            s = conn.execute(
                "SELECT COUNT(*), MIN(snapshot_date), MAX(snapshot_date), "
                "COUNT(DISTINCT snapshot_date) FROM live_position_snapshots WHERE market=?",
                (m,)).fetchone()
            out["deals"][m] = {"rows": d[0], "first": d[1], "last": d[2], "symbols": d[3]}
            out["snapshots"][m] = {"rows": s[0], "first": s[1], "last": s[2], "days": s[3]}
        out["deals"]["fee_null_rows"] = conn.execute(
            "SELECT COUNT(*) FROM live_deal_records WHERE fee IS NULL").fetchone()[0]
        out["deals"]["unknown_market_rows"] = conn.execute(
            "SELECT COUNT(*) FROM live_deal_records WHERE currency IS NULL").fetchone()[0]
        # 派生回合覆盖面 (批 D): 让"港股零回合"这类缺口同样能被一行查询发现
        out["round_trips"] = {}
        for m in MARKETS:
            r = conn.execute(
                "SELECT COUNT(*), MIN(exit_date), MAX(exit_date), COUNT(DISTINCT symbol), "
                "SUM(CASE WHEN basis='COMPLETE' THEN 1 ELSE 0 END) "
                "FROM live_round_trips WHERE market=?", (m,)).fetchone()
            out["round_trips"][m] = {"rows": r[0], "first": r[1], "last": r[2],
                                     "symbols": r[3], "complete_rows": r[4] or 0}
        out["round_trips"]["exit_reason_not_captured"] = conn.execute(
            "SELECT COUNT(*) FROM live_round_trips "
            "WHERE exit_reason IS NULL").fetchone()[0]
    finally:
        conn.close()
    return out


# ── 成交额汇总 (看板端点与实盘周报**共用同一份方向语义**) ──────────────
# 富途 trd_side 实测四值: BUY 开多 / SELL_SHORT 开空 / SELL 平多 / BUY_BACK 平空。
# 为什么放在这里而不是各消费方各写一份: 方向判错 = 开仓当平仓 ⇒ 净现金流符号整体反,
# 而这种错在看板和周报上会**同时看起来合理**(两边都错得一样), 极难被发现。
OPEN_SIDES: Tuple[str, ...] = ("BUY", "SELL_SHORT")
CLOSE_SIDES: Tuple[str, ...] = ("SELL", "BUY_BACK")


def summarize_deals(deals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """成交流水的**毛**汇总: 开/平仓额、净现金流、fee 缺失行数、未知方向行数.

    口径: `amount = qty*price` 是**成交额非净额** (fee 全 NULL, 富途 deal 级不返回手续费)
    ⇒ 三个金额都是毛数, 消费方必须标注。未知方向行**不计入**开/平任何一侧 (只计数上报),
    与 `fifo_realized` 的 `unknown_sides` 同一处理方式: 不猜方向。
    """
    open_amount = close_amount = 0.0
    fee_null = unknown_side = 0
    symbols = set()
    for d in deals:
        side = (d.get("trd_side") or "").upper()
        amt = d.get("amount") or 0.0
        if side in OPEN_SIDES:
            open_amount += amt
        elif side in CLOSE_SIDES:
            close_amount += amt
        else:
            unknown_side += 1
        if d.get("fee") is None:
            fee_null += 1
        if d.get("symbol"):
            symbols.add(d["symbol"])
    return {"n": len(deals), "symbols": len(symbols),
            "open_amount": round(open_amount, 2), "close_amount": round(close_amount, 2),
            "net_cash": round(close_amount - open_amount, 2),
            "fee_rows_null": fee_null, "unknown_side_rows": unknown_side,
            "amount_caliber": "gross_deal_amount_no_fee"}


# ── FIFO 已实现盈亏 (只在成本基准完整时才给数, 否则明示不完整) ────────

def fifo_realized(deals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按标的 FIFO 配对成交流水, 算**毛**已实现盈亏.

    **本函数不再自己实现 FIFO** —— 批 D (RFC-078 §8) 把配对逻辑收敛到 `build_round_trips`,
    本函数只取它的 per-symbol 汇总。为什么必须收敛: 若"回合派生"与"已实现盈亏"各写一份
    FIFO, 两者会在**同一个页面上给出两个不同的已实现盈亏**, 而且都看起来合理 (方向语义
    各判一次 = 两边错得一样的同族错误)。一份实现、两个视图。

    **诚实优先**: 若某标的的平仓量超过台账内可见的开仓量 (仓位在台账首日之前就建好了),
    该标的标 `INCOMPLETE_COST_BASIS`, 其盈亏**不计入合计** —— 宁缺毋假: 用现价或均价
    编一个成本基准, 编出来的已实现盈亏会直接进复盘结论, 比留白更坏。

    口径: 不含手续费 (券商 deal 级不提供, fee 全 NULL) ⇒ 结果是**毛已实现盈亏**,
    与本仓"毛 edge 修到 0 仍净亏"的费用判决书口径一致 —— 毛数不得当净数用。

    方向语义 (实测富途 trd_side 四值): BUY 开多 / SELL 平多 / SELL_SHORT 开空 /
    BUY_BACK 平空 (开/平的粗分类见 `OPEN_SIDES`/`CLOSE_SIDES`; 配对需要更细的四值
    语义 —— 平多与平空的盈亏符号相反, 粗分类不够用)。
    """
    rt = build_round_trips(deals)
    return {
        "per_symbol": rt["per_symbol"],
        "realized_gross_complete_basis_only": rt["realized_gross_complete_basis_only"],
        "incomplete_basis_symbols": rt["incomplete_basis_symbols"],
        "unknown_sides": rt["unknown_sides"],
        "fee_included": False,
        "caveat": "券商 deal 级不含手续费 ⇒ **毛**口径, 不得当净数用; "
                  "INCOMPLETE_COST_BASIS 标的的成本早于台账首日, 其盈亏未计入合计 (不编造基准)",
    }


# ══════════════════════════════════════════════════════════════════
#  批 D · 回合台账 (券商真值派生, 市场中立) — RFC-078 §8
# ══════════════════════════════════════════════════════════════════
# 为什么需要: 港股此前**没有任何回合级数据** (不进 trade_records, jsonl 只有 submit/cancel),
# 美股回合台账又断链过 3 个月 ⇒ 复盘只能看流水、看不到"一次进出赚亏多少、持有多久"。
# 派生源只用 live_deal_records (券商真值层) ⇒ 不依赖下单代码是否记得写。
#
# 只能派生**执行事实**, 不能派生**决策理由**: hk_manual_orders.jsonl 63 行里 reason 非空仅
# 7 行 ⇒ 靠时间邻近去"推断"exit_reason, 推断出来的理由会以事实的样子进复盘结论 (与"用现价
# 编成本基准"同族) ⇒ 第一版 exit_reason 恒 NULL + exit_reason_state='NOT_CAPTURED'。

_DDL_ROUND_TRIPS = """
CREATE TABLE IF NOT EXISTS live_round_trips (
    rt_id             TEXT PRIMARY KEY,
    market            TEXT NOT NULL,
    currency          TEXT,
    symbol            TEXT NOT NULL,
    stock_name        TEXT,
    direction         TEXT,
    qty               REAL,
    entry_deal_id     TEXT,
    entry_time        TEXT,
    entry_date        TEXT,
    entry_price       REAL,
    exit_deal_id      TEXT,
    exit_time         TEXT,
    exit_date         TEXT,
    exit_price        REAL,
    pnl_gross         REAL,
    pnl_pct           REAL,
    holding_hours     REAL,
    basis             TEXT,
    fee_included      INTEGER NOT NULL DEFAULT 0,
    exit_reason       TEXT,
    exit_reason_state TEXT,
    status            TEXT,
    source            TEXT,
    derived_at        TEXT
)
"""

_RT_COLUMNS: Tuple[str, ...] = (
    "rt_id", "market", "currency", "symbol", "stock_name", "direction", "qty",
    "entry_deal_id", "entry_time", "entry_date", "entry_price",
    "exit_deal_id", "exit_time", "exit_date", "exit_price",
    "pnl_gross", "pnl_pct", "holding_hours", "basis", "fee_included",
    "exit_reason", "exit_reason_state", "status", "source", "derived_at",
)

# 港股本地时 (HKT) 与 Asia/Shanghai 同为 UTC+8 且**均无 DST** ⇒ 同尺, 用 CST 即可。
_RT_TZ = {"HK": "CST", "US": "ET"}

# 胜率闸门: 计分回合占比低于此值 ⇒ **不给胜率** (返回 None + 状态), 而不是照算。
# 为什么需要 (2026-09-08 实测, 判据 3 顺带咬出来的真问题): 标的级排除会把"有任一未配对腿"
# 的标的**整段**排掉, 而实盘里亏损往往正集中在这些老仓上 ⇒ 实测全量 99 笔成交派生 59 回合,
# 计分只剩 28 (覆盖率 47.5%), 胜率算出来 **96.4%** (27胜1负); 美股段更极端: 38 回合计分 11,
# 9 笔亏损全落在被排除的 US.SNDK/US.BABA 里 ⇒ 该段胜率 **100%**。
# 一个"看起来很漂亮"的胜率比没有胜率更坏: 它会直接进复盘结论并强化"我做对了"的错觉。
# 金额合计仍照给 (它是"已知为真的部分之和", 且明示只覆盖 N/M 回合), 但胜率必须过闸门。
_RT_MIN_SCORED_COVERAGE = 0.7

# 小样本闸门: 计分回合少于此数 ⇒ 给**胜负计数**(事实)但不给**胜率**(推断)。
# 为什么 (2026-09-08 实测): 7 天窗口下美股只有 2 笔、港股 6 笔派生回合, 覆盖率都是 100%
# ⇒ 覆盖率闸门放行, 报告上就出现"胜 2/2 (100.0%)"。2 笔样本的 100% 与 50 笔样本的 100%
# 在纸面上长得一模一样, 而前者毫无信息量 —— 与本仓"n≥8 才调 LLM 出题、n≥20 才毕业"
# 的样本纪律同族: 计数是事实, 比率是推断, 推断要过样本门。
_RT_MIN_SCORED_N = 5


def _parse_deal_dt(market: str, raw: Optional[str]):
    """把券商给的**市场本地 naive** `deal_time` 附上该市场时区.

    实测格式 `2026-07-10 04:02:38.179` (各市场本地时, 见文件头"第二个实测发现")。
    附尺而不是裸相减: 美股跨 DST 的回合裸减会差 1 小时; 且**只在同市场内相减**
    (进场腿与出场腿必然同市场 —— 表按 symbol 分队列, symbol 自带市场前缀)。

    ⚠ **附尺还不够** (2026-09-08 实测踩到): CPython 的 datetime 相减有一条既有语义 ——
    两个 aware 对象若 `tzinfo` 是**同一个对象**, 相减时会**忽略 utcoffset**
    ("both are aware and have the same tzinfo attribute, the tzinfo attributes are ignored")。
    同市场的进场/出场腿正是同一个 tz 对象 ⇒ `exit_dt - entry_dt` 得到的是**墙钟差**而不是
    绝对时刻差。实测 2026-03-07 09:00 EST → 03-08 09:00 EDT (跨 DST): 直接相减 **24.0h**,
    而 `timestamp()` 差与转 UTC 相减都是 **23.0h** (真值 23h)。故一律用 `.timestamp()` 相减。
    """
    if not raw:
        return None
    from app.utils.timezone import CST as _CST, ET as _ET, _localize
    txt = str(raw).strip()
    naive = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(txt, fmt)
            break
        except ValueError:
            continue
    if naive is None:
        logger.warning("[实盘台账] deal_time 解析失败, 持有时长留空 (不猜): %r", raw)
        return None
    return _localize(naive, _CST if _RT_TZ.get(market) == "CST" else _ET)


def build_round_trips(deals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """FIFO 配对成交流水 → **回合行** + per-symbol 汇总 (本仓唯一的 FIFO 实现).

    返回:
      `rows`       派生回合行 (列名与 `live_round_trips` 一致, 可直接 upsert)
      `per_symbol` / `realized_gross_complete_basis_only` / `incomplete_basis_symbols`
                   / `unknown_sides`  —— 与批 B 的 `fifo_realized` 返回结构逐字一致
                   (该函数现委派本函数, 保证看板/周报/回合三处同一个数)
      `open_lots`  未闭环的开仓批次 (= 在仓, **不落回合表**, 但必须可见:
                   否则"派生 0 回合"会被读成"没有持仓" —— 三态纪律同族)

    口径: `pnl_gross` 是**毛** (fee 全 NULL); `basis='INCOMPLETE_COST_BASIS'` 的行
    盈亏为 **NULL** 且不计入合计 (开仓腿早于台账首日 ⇒ 不用现价/均价编基准)。
    """
    long_q: Dict[str, List[Dict[str, Any]]] = {}
    short_q: Dict[str, List[Dict[str, Any]]] = {}
    per_symbol: Dict[str, Dict[str, Any]] = {}
    unknown_sides: Dict[str, int] = {}
    rows: List[Dict[str, Any]] = []
    derived_at = now_utc().isoformat(timespec="seconds")

    def slot(sym: str) -> Dict[str, Any]:
        return per_symbol.setdefault(sym, {
            "symbol": sym, "closed_qty": 0.0, "realized_gross": 0.0,
            "matched": 0.0, "unmatched": 0.0, "complete_basis": True, "legs": 0,
            "open_qty_remaining": 0.0,
        })

    ordered = sorted(deals, key=lambda x: (x.get("deal_time") or "", x.get("deal_id") or ""))
    for d in ordered:
        sym = d.get("symbol")
        side = (d.get("trd_side") or "").upper()
        qty = float(d.get("qty") or 0.0)
        price = float(d.get("price") or 0.0)
        if not sym or qty <= 0:
            continue
        market = d.get("market") or market_of(d.get("deal_market"), sym)
        currency = d.get("currency") or _CURRENCY_BY_MARKET.get(market)
        s = slot(sym)
        s["legs"] += 1

        if side in ("BUY", "SELL_SHORT"):
            q = long_q if side == "BUY" else short_q
            q.setdefault(sym, []).append({
                "qty": qty, "price": price, "deal_id": d.get("deal_id"),
                "time": d.get("deal_time"), "date": d.get("deal_date"),
                "market": market, "currency": currency,
                "stock_name": d.get("stock_name"),
                "direction": "LONG" if side == "BUY" else "SHORT",
            })
            continue

        if side not in ("SELL", "BUY_BACK"):
            key = side or "<empty>"
            unknown_sides[key] = unknown_sides.get(key, 0) + 1
            continue

        # SELL 只吃 long 队列 / BUY_BACK 只吃 short 队列 (混用会把空单盈利报成亏损)
        direction = "LONG" if side == "SELL" else "SHORT"
        q = (long_q if side == "SELL" else short_q).setdefault(sym, [])
        exit_dt = _parse_deal_dt(market, d.get("deal_time"))
        s["closed_qty"] += qty
        remain, seq = qty, 0
        while remain > 1e-9 and q:
            lot = q[0]
            take = min(lot["qty"], remain)
            pnl = ((price - lot["price"]) if direction == "LONG"
                   else (lot["price"] - price)) * take
            entry_dt = _parse_deal_dt(lot["market"], lot["time"])
            notional = lot["price"] * take
            rows.append({
                # rt_id 确定性: exit_deal_id 券商全局唯一 + seq 区分"一笔平仓吃掉多个批次"
                "rt_id": f"{d.get('deal_id')}#{seq}",
                "market": market, "currency": currency, "symbol": sym,
                "stock_name": d.get("stock_name") or lot.get("stock_name"),
                "direction": direction, "qty": round(take, 6),
                "entry_deal_id": lot["deal_id"], "entry_time": lot["time"],
                "entry_date": lot["date"], "entry_price": lot["price"],
                "exit_deal_id": d.get("deal_id"), "exit_time": d.get("deal_time"),
                "exit_date": d.get("deal_date"), "exit_price": price,
                "pnl_gross": round(pnl, 6),
                "pnl_pct": round(pnl / notional * 100.0, 4) if notional else None,
                # 用绝对时刻相减, 不用 `exit_dt - entry_dt`: 同 tzinfo 对象会被 CPython
                # 忽略偏移 ⇒ 跨 DST 的美股回合会多算 1 小时 (见 _parse_deal_dt 的实测记档)
                "holding_hours": (round((exit_dt.timestamp() - entry_dt.timestamp()) / 3600.0, 2)
                                  if (exit_dt is not None and entry_dt is not None) else None),
                "basis": "COMPLETE", "fee_included": 0,
                "exit_reason": None, "exit_reason_state": "NOT_CAPTURED",
                "status": "CLOSED", "source": "broker_fifo", "derived_at": derived_at,
            })
            s["realized_gross"] += pnl
            s["matched"] += take
            lot["qty"] -= take
            remain -= take
            seq += 1
            if lot["qty"] <= 1e-9:
                q.pop(0)
            else:
                q[0]["qty"] = lot["qty"]
        if remain > 1e-9:
            # 平仓量超出台账内可见开仓量 ⇒ 单落一行, 进出场腿与盈亏全 NULL (不编基准)。
            # 刻意**留行**而不是丢弃: 丢了就会让"平仓笔数"与成交流水对不上, 读者无从发现。
            rows.append({
                "rt_id": f"{d.get('deal_id')}#{seq}",
                "market": market, "currency": currency, "symbol": sym,
                "stock_name": d.get("stock_name"),
                "direction": direction, "qty": round(remain, 6),
                "entry_deal_id": None, "entry_time": None,
                "entry_date": None, "entry_price": None,
                "exit_deal_id": d.get("deal_id"), "exit_time": d.get("deal_time"),
                "exit_date": d.get("deal_date"), "exit_price": price,
                "pnl_gross": None, "pnl_pct": None, "holding_hours": None,
                "basis": "INCOMPLETE_COST_BASIS", "fee_included": 0,
                "exit_reason": None, "exit_reason_state": "NOT_CAPTURED",
                "status": "CLOSED_NO_ENTRY_IN_LEDGER", "source": "broker_fifo",
                "derived_at": derived_at,
            })
            s["unmatched"] = round(s["unmatched"] + remain, 6)
            s["complete_basis"] = False

    open_lots: List[Dict[str, Any]] = []
    for sym, s in per_symbol.items():
        # 先按标的把毛盈亏圆到 2 位再求总 (与批 B 原实现逐字同序 ⇒ 两处同一个数)
        s["realized_gross"] = round(s["realized_gross"], 2)
        s["matched"] = round(s["matched"], 6)
        s["closed_qty"] = round(s["closed_qty"], 6)
        lots = [(l, "LONG") for l in long_q.get(sym, [])] + \
               [(l, "SHORT") for l in short_q.get(sym, [])]
        s["open_qty_remaining"] = round(sum(l["qty"] for l, _ in lots), 6)
        s["basis"] = "COMPLETE" if s["complete_basis"] else "INCOMPLETE_COST_BASIS"
        for lot, dirn in lots:
            open_lots.append({
                "symbol": sym, "market": lot["market"], "currency": lot["currency"],
                "direction": dirn, "qty": round(lot["qty"], 6),
                "entry_price": lot["price"], "entry_time": lot["time"],
                "entry_date": lot["date"], "entry_deal_id": lot["deal_id"],
            })
    open_lots.sort(key=lambda x: (x["symbol"], x["entry_time"] or "", x["entry_deal_id"] or ""))

    incomplete = sorted(k for k, v in per_symbol.items() if not v["complete_basis"])
    total = round(sum(v["realized_gross"] for v in per_symbol.values()
                      if v["complete_basis"]), 2)
    return {
        "rows": rows,
        "per_symbol": per_symbol,
        "realized_gross_complete_basis_only": total,
        "incomplete_basis_symbols": incomplete,
        "unknown_sides": unknown_sides,
        "open_lots": open_lots,
        "n_rows": len(rows),
        "n_complete": sum(1 for r in rows if r["basis"] == "COMPLETE"),
        "n_incomplete": sum(1 for r in rows if r["basis"] != "COMPLETE"),
    }


def read_all_deals() -> List[Dict[str, Any]]:
    """全量成交流水 (派生回合**必须**用全量, 见 `sync_round_trips` 的口径说明)."""
    ensure_tables()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM live_deal_records ORDER BY deal_time, deal_id").fetchall()]
    finally:
        conn.close()


def sync_round_trips() -> Dict[str, Any]:
    """从**全量**台账派生回合并 upsert (确定性 + 幂等).

    ⚠ 派生用全量而不是某个窗口: 窗口截断会把"进场腿落在窗口外"的完整回合误标成
    `INCOMPLETE_COST_BASIS` (盈亏变 NULL) ⇒ 窗口只能用于**展示**, 不能用于派生。
    同理, 重跑会用新派生集**剪掉**已不存在的 rt_id (派生是确定性的 ⇒ 剪枝安全);
    台账为空时**不剪** (那会把已有回合清空, 把"没同步"变成"没交易")。
    """
    ensure_tables()
    deals = read_all_deals()
    if not deals:
        return {"status": "NO_DATA", "inserted": 0, "updated": 0, "pruned": 0,
                "total_rows": _count_round_trips(),
                "detail": "成交流水为空 — 台账还没回补过, 这**不是**零回合"}
    rt = build_round_trips(deals)
    now = now_utc().isoformat(timespec="seconds")
    conn = _connect()
    try:
        existing = {r[0] for r in conn.execute("SELECT rt_id FROM live_round_trips")}
        cols = ", ".join(_RT_COLUMNS)
        marks = ", ".join("?" * len(_RT_COLUMNS))
        updates = ", ".join(f"{c}=excluded.{c}" for c in _RT_COLUMNS if c != "rt_id")
        new_ids = set()
        inserted = updated = 0
        for row in rt["rows"]:
            row = dict(row)
            row["derived_at"] = now
            rid = row["rt_id"]
            new_ids.add(rid)
            if rid in existing:
                updated += 1
            else:
                inserted += 1
            conn.execute(
                f"INSERT INTO live_round_trips ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(rt_id) DO UPDATE SET {updates}",
                tuple(row.get(c) for c in _RT_COLUMNS))
        pruned = 0
        stale = sorted(existing - new_ids)
        if stale:
            conn.executemany("DELETE FROM live_round_trips WHERE rt_id = ?",
                             [(x,) for x in stale])
            pruned = len(stale)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM live_round_trips").fetchone()[0]
    finally:
        conn.close()
    return {
        "status": "OK", "deals": len(deals), "inserted": inserted, "updated": updated,
        "pruned": pruned, "total_rows": total,
        "n_complete": rt["n_complete"], "n_incomplete": rt["n_incomplete"],
        "open_lots": len(rt["open_lots"]),
        "incomplete_basis_symbols": rt["incomplete_basis_symbols"],
        "unknown_sides": rt["unknown_sides"],
        "realized_gross_complete_basis_only": rt["realized_gross_complete_basis_only"],
    }


def _count_round_trips() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM live_round_trips").fetchone()[0]
    finally:
        conn.close()


def read_round_trips(start: Optional[str] = None, end: Optional[str] = None,
                     market: Optional[str] = None) -> List[Dict[str, Any]]:
    """读派生回合 (窗口按**出场日**, 与周报"出场日归集"同口径; 不传窗口 = 全量)."""
    ensure_tables()
    sql = "SELECT * FROM live_round_trips WHERE 1=1"
    args: List[Any] = []
    if market:
        sql += " AND market = ?"
        args.append(market)
    if start:
        sql += " AND exit_date >= ?"
        args.append(start)
    if end:
        sql += " AND exit_date <= ?"
        args.append(end)
    sql += " ORDER BY exit_time DESC, rt_id DESC"
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


def round_trip_panel(market: str, start: str, end: str) -> Dict[str, Any]:
    """看板"一行"用的派生回合视图 (港美**共用**, 端点层不再各写一份).

    三态与成交层同族且措辞自陈: `OK` / `NO_ROUNDS_IN_WINDOW` (台账有回合、本窗口没有
    = 真没了结) / `NO_DATA` (台账一行回合都没有 = 还没派生过, **不是**零回合)。
    """
    rows = read_round_trips(start, end, market=market)
    summary = summarize_round_trips(rows)
    lots = [lot for lot in build_round_trips(read_all_deals())["open_lots"]
            if lot.get("market") == market]
    cov = (ledger_coverage().get("round_trips") or {}).get(market) or {}
    ledger_rows = cov.get("rows") or 0
    return {
        "state": "OK" if rows else ("NO_DATA" if not ledger_rows else "NO_ROUNDS_IN_WINDOW"),
        "summary": summary,
        "n_in_window": len(rows),
        "ledger_rows": ledger_rows,
        "ledger_span": [cov.get("first"), cov.get("last")],
        # 未闭环批次 = **在仓**, 不算回合; 必须给, 否则"0 回合"会被读成"没有持仓"
        "open_lots_n": len(lots),
        "open_lots_symbols": sorted({lot["symbol"] for lot in lots}),
        "window": [start, end],
        "window_tz": _TZ_LABEL.get(market),
        "source": "broker_fifo(live_round_trips)",
        "caliber": "gross_no_fee",
    }


def summarize_round_trips(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """派生回合的**毛**小结 (看板一行 / 周报一段共用).

    合计口径与 `fifo_realized` **逐字一致** —— 这是判据 3 (看板/周报/回合三处不可能给出
    两个不同的已实现盈亏) 的落点, 第一版就是在这里咬出真问题的:

      · 排除粒度是**标的级**而不是行级: 某标的只要有任一条腿 `basis != COMPLETE`
        (平仓量超出台账内可见开仓量), 该标的**全部**回合都不计分 —— 包括那些配对完整的行。
        为什么不用行级 (行级会把已知为真的行留下, 数字更大更好看): 该标的的成本基准
        已被证明不完整 ⇒ 它的 FIFO 队列本身就是残的, 残队列上配出来的"完整行"依赖
        "先来的先卖"这个在缺腿情况下并不成立的假设 ⇒ 留着就是半真半假。宁缺毋假。
      · 先按标的圆到 2 位再求总 (与 `build_round_trips` 的 per-symbol 圆整同序)。
    """
    per_symbol: Dict[str, float] = {}
    wins = losses = flat = 0
    incomplete = 0
    reason_missing = 0
    symbols = set()
    holdings: List[float] = []
    bad_symbols = {r["symbol"] for r in rows
                   if r.get("basis") != "COMPLETE" or r.get("pnl_gross") is None}
    for r in rows:
        if r.get("symbol"):
            symbols.add(r["symbol"])
        if (r.get("exit_reason_state") or "NOT_CAPTURED") == "NOT_CAPTURED":
            reason_missing += 1
        if r["symbol"] in bad_symbols or r.get("pnl_gross") is None:
            incomplete += 1
            continue
        per_symbol[r["symbol"]] = per_symbol.get(r["symbol"], 0.0) + float(r["pnl_gross"])
        pnl = float(r["pnl_gross"])
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        else:
            flat += 1
        if r.get("holding_hours") is not None:
            holdings.append(float(r["holding_hours"]))
    n_scored = wins + losses + flat
    gross = round(sum(round(v, 2) for v in per_symbol.values()), 2)
    coverage = round(n_scored / len(rows), 4) if rows else 0.0
    if not rows:
        win_rate_state = "NO_DATA"
    elif n_scored == 0:
        win_rate_state = "NO_SCORED_ROW"
    elif coverage < _RT_MIN_SCORED_COVERAGE:
        win_rate_state = "INSUFFICIENT_COVERAGE"
    elif n_scored < _RT_MIN_SCORED_N:
        win_rate_state = "SMALL_SAMPLE"
    else:
        win_rate_state = "OK"
    return {
        "n": len(rows), "symbols": len(symbols), "n_scored": n_scored,
        "wins": wins, "losses": losses, "flat": flat,
        # 胜率**只在过闸门时给**: 覆盖率不足时给数字 = 把"排除掉的正是亏损行"藏起来
        "win_rate_pct": (round(100.0 * wins / n_scored, 1)
                         if win_rate_state == "OK" else None),
        "win_rate_state": win_rate_state,
        "scored_coverage": coverage,
        "pnl_gross_complete_basis_only": gross,
        "incomplete_basis_rows": incomplete,
        "incomplete_basis_symbols": sorted(bad_symbols),
        "exit_reason_not_captured": reason_missing,
        "median_holding_hours": (round(sorted(holdings)[len(holdings) // 2], 2)
                                 if holdings else None),
        "pnl_caliber": "gross_no_fee",
        "caveat": ("毛口径 (fee 全 NULL); 排除粒度=**标的级** — "
                   "有任一未配对腿的标的整段不计分 (含其配对完整的行), 与 fifo_realized 同口径; "
                   f"金额合计只覆盖 {n_scored}/{len(rows)} 回合, 覆盖率不足 "
                   f"{_RT_MIN_SCORED_COVERAGE:.0%} 或计分样本 < {_RT_MIN_SCORED_N} 笔时"
                   "**不给胜率** (前者排除掉的往往正是亏损行; 后者是小样本比率无信息量)"),
        "win_rate_caliber": ("分母只含**基准完整标的**的回合 — 基准不完整的标的盈亏未知, "
                             "计入分母会虚高/虚低胜率"),
    }
