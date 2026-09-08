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
from datetime import timedelta
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
    """成交 + 持仓快照一并回补 (cron 入口). 各腿各自三态, 互不掩盖."""
    out: Dict[str, Any] = {"deals": sync_deals(days),
                           "positions": sync_position_snapshots()}
    statuses = [out["deals"].get("status"), out["positions"].get("status")]
    if all(s == "UNAVAILABLE" for s in statuses):
        out["overall"] = "UNAVAILABLE"
    elif any(s in ("UNAVAILABLE", "PARTIAL") for s in statuses):
        out["overall"] = "PARTIAL"
    else:
        out["overall"] = "OK"
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

    **诚实优先**: 若某标的的平仓量超过台账内可见的开仓量 (仓位在台账首日之前就建好了),
    该标的标 `INCOMPLETE_COST_BASIS`, 其盈亏**不计入合计** —— 宁缺毋假: 用现价或均价
    编一个成本基准, 编出来的已实现盈亏会直接进复盘结论, 比留白更坏。

    口径: 不含手续费 (券商 deal 级不提供, fee 全 NULL) ⇒ 结果是**毛已实现盈亏**,
    与本仓"毛 edge 修到 0 仍净亏"的费用判决书口径一致 —— 毛数不得当净数用。

    方向语义 (实测富途 trd_side 四值): BUY 开多 / SELL 平多 / SELL_SHORT 开空 /
    BUY_BACK 平空 (开/平的粗分类见 `OPEN_SIDES`/`CLOSE_SIDES`; 本函数需要更细的四值
    语义 —— 平多与平空的盈亏符号相反, 粗分类不够用)。
    """
    long_q: Dict[str, List[List[float]]] = {}
    short_q: Dict[str, List[List[float]]] = {}
    per_symbol: Dict[str, Dict[str, Any]] = {}
    unknown_sides: Dict[str, int] = {}

    def slot(sym: str) -> Dict[str, Any]:
        return per_symbol.setdefault(sym, {
            "symbol": sym, "closed_qty": 0.0, "realized_gross": 0.0,
            "matched": 0.0, "unmatched": 0.0, "complete_basis": True, "legs": 0,
            "open_qty_remaining": 0.0,
        })

    for d in sorted(deals, key=lambda x: (x.get("deal_time") or "", x.get("deal_id") or "")):
        sym = d.get("symbol")
        side = (d.get("trd_side") or "").upper()
        qty = float(d.get("qty") or 0.0)
        price = float(d.get("price") or 0.0)
        if not sym or qty <= 0:
            continue
        s = slot(sym)
        s["legs"] += 1
        if side == "BUY":
            long_q.setdefault(sym, []).append([qty, price])
        elif side == "SELL_SHORT":
            short_q.setdefault(sym, []).append([qty, price])
        elif side in ("SELL", "BUY_BACK"):
            q = long_q.setdefault(sym, []) if side == "SELL" else short_q.setdefault(sym, [])
            remain = qty
            while remain > 1e-9 and q:
                lot_qty, lot_price = q[0]
                take = min(lot_qty, remain)
                # SELL 平多: 卖价−成本; BUY_BACK 平空: 成本−买回价
                pnl = ((price - lot_price) if side == "SELL"
                       else (lot_price - price)) * take
                s["realized_gross"] += pnl
                s["matched"] += take
                lot_qty -= take
                remain -= take
                if lot_qty <= 1e-9:
                    q.pop(0)
                else:
                    q[0][0] = lot_qty
            if remain > 1e-9:
                s["unmatched"] = round(s["unmatched"] + remain, 6)
                s["complete_basis"] = False
            s["closed_qty"] += qty
        else:
            unknown_sides[side or "<empty>"] = unknown_sides.get(side or "<empty>", 0) + 1

    for sym, s in per_symbol.items():
        s["realized_gross"] = round(s["realized_gross"], 2)
        s["matched"] = round(s["matched"], 6)
        s["closed_qty"] = round(s["closed_qty"], 6)
        s["open_qty_remaining"] = round(
            sum(l[0] for l in long_q.get(sym, [])) + sum(l[0] for l in short_q.get(sym, [])), 6)
        s["basis"] = "COMPLETE" if s["complete_basis"] else "INCOMPLETE_COST_BASIS"

    incomplete = sorted(k for k, v in per_symbol.items() if not v["complete_basis"])
    total = round(sum(v["realized_gross"] for v in per_symbol.values()
                      if v["complete_basis"]), 2)
    return {
        "per_symbol": per_symbol,
        "realized_gross_complete_basis_only": total,
        "incomplete_basis_symbols": incomplete,
        "unknown_sides": unknown_sides,
        "fee_included": False,
        "caveat": "券商 deal 级不含手续费 ⇒ **毛**口径, 不得当净数用; "
                  "INCOMPLETE_COST_BASIS 标的的成本早于台账首日, 其盈亏未计入合计 (不编造基准)",
    }
