"""
DataService — 统一数据获取层 (2026-06-11 重构)

解决问题:
  - 首页加载 8+ API 各独立调 Futu, accinfo 重复 3 次, position_list 重复 4 次
  - 阻塞式同步 Futu SDK 调用卡住 async event loop
  - 分散的 sqlite3.connect 无连接管理

设计:
  - 共享 TTL 缓存: 多路由共用同一份 Futu 数据
  - run_in_executor: 所有阻塞调用在线程池执行, 不卡 event loop
  - 单一 snapshot 端点: 前端一次请求获取全部指挥中心数据
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("ftseek_us.data_service")

# ── 时间辅助 (美股以 ET 为准) ──
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except ImportError:
    import pytz
    _ET_TZ = pytz.timezone("America/New_York")


def et_now() -> datetime:
    return datetime.now(_ET_TZ)


def et_today() -> str:
    return et_now().strftime("%Y-%m-%d")


def et_days_ago(n: int) -> str:
    return (et_now() - timedelta(days=n)).strftime("%Y-%m-%d")


def battle_plan_is_stale(plan: dict) -> bool:
    """判断作战计划是否过期 (非今日 ET).

    旧 US discovery 生成链已于 2026-07-13 停用, daily_battle_plan_us.json
    冻结在最后一次生成日。实际交易已迁移至 DA + UE → playbook_instructions。
    Web 展示端据此判断: 计划 date != 今日 ET → 视为过期, 不应作为"今日交易计划"展示。
    """
    if not isinstance(plan, dict):
        return True
    _d = plan.get("date", "")
    if not _d:
        return True
    return _d != et_today()


def safe_float(v, default: float = 0.0) -> float:
    """统一 safe_float (SSOT: app.utils.numeric) — 向后兼容 re-export"""
    from app.utils.numeric import safe_float as _sf
    return _sf(v, default)


# ── 共享 TTL 缓存 ──
class _SharedCache:
    """线程安全 TTL 缓存, 多路由共享"""

    def __init__(self):
        self._store: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry and time.time() < entry[1]:
                return entry[0]
        return None

    def set(self, key: str, value, ttl: float):
        with self._lock:
            self._store[key] = (value, time.time() + ttl)

    def invalidate(self, key: str = None):
        with self._lock:
            if key:
                self._store.pop(key, None)
            else:
                self._store.clear()


# ── DataService 单例 ──
class DataService:
    """
    统一数据获取 + 缓存层

    - 所有 Futu RPC 通过 run_in_executor 异步执行
    - 结果存入共享 TTL 缓存, 多路由复用
    - DB 查询统一走这里, 避免散落各处的 sqlite3.connect
    """

    # 哨兵值: 区分 "未缓存" 与 "缓存了 None/空结果"
    _CACHE_MISS = object()

    def __init__(self):
        self.cache = _SharedCache()
        # 请求级去重: 防 Thundering Herd (缓存 TTL 过期时多协程穿透到 Futu RPC)
        self._fetch_locks: Dict[str, asyncio.Lock] = {}
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._last_snapshot: Dict[str, Any] = {}  # delta 检测用
        self._last_reconcile_time: float = 0.0  # 幽灵仓对账限流 (5min)

    async def _emit_change(self, key: str, new_value: Any):
        """缓存刷新后发 WS 事件 (非阻塞, 失败静默)"""
        try:
            from app.web_dashboard.routes.ws import publish_event
            old = self._last_snapshot.get(key)
            if not self._has_meaningful_change(key, old, new_value):
                return
            self._last_snapshot[key] = new_value
            # 构造推送 payload
            event_type = self._key_to_event(key)
            if event_type:
                await publish_event(event_type, self._build_payload(key, new_value))
        except Exception:
            pass  # WS 推送失败不影响主流程

    def _has_meaningful_change(self, key: str, old: Any, new: Any) -> bool:
        """智能 delta 过滤 — 避免无意义推送"""
        if old is None:
            return True  # 首次数据, 必须推
        if key == "positions_raw":
            # 持仓数量变化 或 任何 unrealized_pnl 变 > $1
            if len(old or []) != len(new or []):
                return True
            for o, n in zip(old, new):
                if abs(safe_float(n.get("unrealized_pl", 0)) - safe_float(o.get("unrealized_pl", 0))) > 1:
                    return True
            return False
        elif key == "account_raw":
            # NAV 变 > $1
            old_nav = safe_float((old or {}).get("nav", 0))
            new_nav = safe_float((new or {}).get("nav", 0))
            return abs(new_nav - old_nav) > 1
        elif key.startswith("quotes_d_"):
            # 任何价格变 > $0.01
            for sym in (new or {}):
                old_p = safe_float((old or {}).get(sym, {}).get("cur_price", 0))
                new_p = safe_float(new.get(sym, {}).get("cur_price", 0))
                if abs(new_p - old_p) > 0.01:
                    return True
            return False
        return old != new

    @staticmethod
    def _key_to_event(key: str) -> str:
        _MAP = {
            "positions_raw": "positions_update",
            "account_raw": "account_update",
        }
        if key in _MAP:
            return _MAP[key]
        if key.startswith("quotes_d_"):
            return "quote_update"
        return None

    @staticmethod
    def _build_payload(key: str, value: Any) -> dict:
        """构造精简推送 payload"""
        if key == "positions_raw":
            return {"positions": [
                {"code": p.get("code"), "qty": p.get("qty"),
                 "unrealized_pl": round(safe_float(p.get("unrealized_pl", 0)), 2),
                 "cur_price": round(safe_float(p.get("cur_price", 0)), 2)}
                for p in (value or [])
            ]}
        elif key == "account_raw":
            v = value or {}
            return {"nav": round(safe_float(v.get("nav", 0)), 2),
                    "cash": round(safe_float(v.get("cash", 0)), 2),
                    "power": round(safe_float(v.get("power", 0)), 2)}
        elif key.startswith("quotes_d_"):
            return {"quotes": {
                sym: {"cur_price": d.get("cur_price"), "prev_close": d.get("prev_close")}
                for sym, d in (value or {}).items()
            }}
        return {"data": value}

    # ── 获取 ManualOrderService 单例 ──
    @staticmethod
    def svc():
        from app.web_dashboard.manual_order_service import get_manual_order_service
        return get_manual_order_service()

    def _get_env_ctx(self):
        """返回 (svc, trd_env, acc_id) 三元组"""
        svc = self.svc()
        if not svc.connect():
            return None, None, None
        from futu import TrdEnv
        is_live = svc._is_live()
        trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
        acc_id = int(svc._read_env(
            "REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"
        ))
        return svc, trd_env, acc_id

    # ═══════════════════════════════════════════
    #  Futu 数据获取 (async + cached)
    # ═══════════════════════════════════════════

    async def get_account_raw(self) -> dict:
        """Futu accinfo_query (10s cache), 返回原始 row dict"""
        async def _fetch():
            # 添加超时: 10秒内必须返回
            try:
                r = await asyncio.wait_for(self._run(self._fetch_account), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("[DataService] _fetch_account timeout (10s)")
                r = None
            if not r:
                return None
            await self._emit_change("account_raw", r)
            return r

        result = await self._deduplicated_fetch("account_raw", _fetch, ttl=10)
        return result or {}

    @staticmethod
    def to_usd(raw: dict) -> dict:
        """将 Futu accinfo_query 原始行统一转换为 USD。

        HKD 账户: 使用 usd_assets/us_cash/usd_net_cash_power 等 USD 子账户字段，
                  其余字段按实际汇率 (hkd_assets / usd_assets) 折算。
        USD 账户: 直接返回原始值。

        返回 dict 所有金额字段均为 USD。
        """
        if not raw:
            return {}
        currency = str(raw.get("currency", "")).upper()
        is_hkd = currency == "HKD"

        if not is_hkd:
            return {
                "total_assets": safe_float(raw.get("total_assets")),
                "cash": safe_float(raw.get("cash")),
                "avl_cash": safe_float(raw.get("avl_withdrawal_cash")),
                "power": safe_float(raw.get("power")),
                "market_val": safe_float(raw.get("market_val")),
                "long_mv": safe_float(raw.get("long_mv")),
                "short_mv": abs(safe_float(raw.get("short_mv"))),
                "initial_margin": safe_float(raw.get("initial_margin")),
                "maintenance_margin": safe_float(raw.get("maintenance_margin")),
                "frozen_cash": safe_float(raw.get("frozen_cash")),
                "currency": "USD",
                "fx": 1.0,
                "_raw": raw,
            }

        # HKD → USD 转换
        hkd_assets = safe_float(raw.get("total_assets"))
        usd_assets = safe_float(raw.get("usd_assets"))
        fx = hkd_assets / usd_assets if usd_assets > 0 else 7.8

        # 可用现金: 优先 us_avl_withdrawal_cash (扣除保证金后)
        avl_cash = safe_float(raw.get("us_avl_withdrawal_cash"))
        if avl_cash <= 0:
            avl_cash = safe_float(raw.get("us_cash"))

        # 购买力: 优先 usd_net_cash_power
        power = safe_float(raw.get("usd_net_cash_power"))
        if power <= 0:
            power = safe_float(raw.get("power")) / fx

        return {
            "total_assets": usd_assets,
            "cash": safe_float(raw.get("us_cash")),
            "avl_cash": avl_cash,
            "power": power,
            "market_val": safe_float(raw.get("market_val")) / fx,
            "long_mv": safe_float(raw.get("long_mv")) / fx,
            "short_mv": abs(safe_float(raw.get("short_mv"))) / fx,
            "initial_margin": safe_float(raw.get("initial_margin")) / fx,
            "maintenance_margin": safe_float(raw.get("maintenance_margin")) / fx,
            "frozen_cash": safe_float(raw.get("frozen_cash")) / fx,
            "currency": "USD",
            "fx": round(fx, 4),
            "_raw": raw,
        }

    def _fetch_account(self) -> dict:
        svc, trd_env, acc_id = self._get_env_ctx()
        if not svc:
            return {}
        try:
            from futu import RET_OK
            ret, funds = svc._broker_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            if ret == RET_OK and not funds.empty:
                return dict(funds.iloc[0])
        except Exception as e:
            logger.error(f"[DataService] accinfo failed: {e}")
        return {}

    async def get_positions_raw(self) -> list:
        """Futu position_list_query (5s cache), 返回原始 row dict 列表"""
        async def _fetch():
            # 添加超时: 10秒内必须返回，否则使用 DB fallback
            try:
                r = await asyncio.wait_for(self._run(self._fetch_positions), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("[DataService] _fetch_positions timeout (10s), using DB fallback")
                r = self._fetch_positions_from_db()
            if not r:
                return None
            await self._emit_change("positions_raw", r)
            return r

        result = await self._deduplicated_fetch("positions_raw", _fetch, ttl=5)
        return result or []

    def _fetch_positions(self) -> list:
        svc, trd_env, acc_id = self._get_env_ctx()
        if not svc:
            logger.warning("[DataService] svc is None, using DB fallback")
            return self._fetch_positions_from_db()
        try:
            from futu import RET_OK
            logger.info(f"[DataService] calling position_list_query(trd_env={trd_env}, acc_id={acc_id})")
            ret, data = svc._broker_ctx.position_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            logger.info(f"[DataService] position_list_query ret={ret}, data.empty={data.empty if hasattr(data, 'empty') else 'N/A'}")
            if ret == RET_OK and not data.empty:
                positions = [dict(row) for _, row in data.iterrows()]
                logger.info(f"[DataService] Futu API returned {len(positions)} positions")
                return positions
            else:
                logger.warning(f"[DataService] Futu API returned empty or error, using DB fallback")
        except Exception as e:
            logger.error(f"[DataService] positions failed: {e}")
        # Fallback: 从 DB 读取持仓 (盘后或 Futu 不可用时)
        return self._fetch_positions_from_db()

    def _fetch_positions_from_db(self) -> list:
        """从主 DB (get_main_db) 读取持仓作为 fallback, 排除已关闭的幽灵记录"""
        import sqlite3
        from app.core.db_utils import get_main_db
        db_path = str(get_main_db())
        if not os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT symbol, direction, size, avg_entry_price, account_type, owner
                FROM open_positions
                WHERE size > 0
                  AND LOWER(COALESCE(account_type,'')) != 'simulate'
                  AND (sync_status IS NULL
                       OR sync_status NOT IN ('closed_by_daemon','closed_by_scheduler','closed_by_sync',
                                               'closed_emergency','stale_not_in_broker'))
            """)
            rows = cur.fetchall()
            conn.close()
            # 转换为 Futu API 格式
            positions = []
            for symbol, direction, size, avg_entry_price, account_type, owner in rows:
                code = f"US.{symbol}" if not symbol.startswith("US.") else symbol
                qty = size if direction == "LONG" else -size
                cost = avg_entry_price or 0
                positions.append({
                    "code": code,
                    "qty": qty,
                    "cost_price": cost,
                    "cur_price": cost,  # Fallback: 使用成本价（盘后无法获取实时价格）
                    "val_price": cost * abs(qty),  # 用于 _parse_positions 的 fallback
                    "position_side": direction,
                    "pl_val": 0,
                    "unrealized_pl": 0,
                    "today_pl_val": 0,
                    # Phase A (2026-07-12): 标记数据来源, 供前端如实展示 (现价=成本占位, 盈亏不可知)
                    # 券商实时不可用时的降级数据, 绝不能让前端把 $0 浮盈当作真实持平
                    "data_source": "db_fallback",
                    # Phase B (2026-07-12): 携带账户归属, 供 _parse_positions 防御性过滤 (实盘页不渲染 sim 仓)
                    "account_type": account_type,
                    "owner": owner,
                })
            if positions:
                logger.info(f"[DataService] positions fallback from DB: {len(positions)} positions")
            return positions
        except Exception as e:
            logger.error(f"[DataService] positions DB fallback failed: {e}")
            return []

    async def get_today_orders_raw(self) -> list:
        """Futu order_list_query (10s cache)"""
        async def _fetch():
            return await self._run(self._fetch_orders)

        result = await self._deduplicated_fetch("orders_raw", _fetch, ttl=10)
        return result or []

    def _fetch_orders(self) -> list:
        svc, trd_env, acc_id = self._get_env_ctx()
        if not svc:
            return []
        try:
            from futu import RET_OK
            ret, data = svc._broker_ctx.order_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret == RET_OK and not data.empty:
                return [dict(row) for _, row in data.iterrows()]
        except Exception as e:
            logger.error(f"[DataService] orders failed: {e}")
        return []

    async def get_market_snapshot(self, codes: list, ttl: int = 10) -> dict:
        """Futu get_market_snapshot, 返回 {code: row_dict} (带 cache)"""
        import hashlib
        _h = hashlib.md5(','.join(sorted(codes)).encode()).hexdigest()[:12]
        cache_key = f"snapshot_{_h}"

        async def _fetch():
            r = await self._run(self._fetch_snapshot, codes)
            return r or None

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=ttl)
        return result or {}

    def _fetch_snapshot(self, codes: list) -> dict:
        svc = self.svc()
        if not svc.connect():
            return {}
        try:
            from futu import RET_OK
            ret, snap = svc._quote_ctx.get_market_snapshot(codes)
            if ret == RET_OK and not snap.empty:
                return {str(row.get("code", "")): dict(row) for _, row in snap.iterrows()}
        except Exception as e:
            logger.error(f"[DataService] snapshot failed: {e}")
        return {}

    async def get_batch_quotes(self, symbols: list) -> Dict[str, float]:
        """批量报价 (5s cache), 返回 {symbol: price}
        
        统一从 detailed 缓存派生, 避免同一组 symbols 产生 2 次 Futu RPC。
        """
        detailed = await self.get_batch_quotes_detailed(symbols)
        return {sym: d["cur_price"] for sym, d in detailed.items() if d.get("cur_price", 0) > 0}

    async def get_batch_quotes_detailed(self, symbols: list) -> Dict[str, dict]:
        """批量详细报价 (5s cache), 返回 {symbol: {cur_price, prev_close, name, ...}}"""
        if not symbols:
            return {}
        import hashlib
        _h = hashlib.md5(','.join(sorted(symbols)).encode()).hexdigest()[:12]
        cache_key = f"quotes_d_{_h}"

        async def _fetch():
            r = await self._run(self._fetch_quotes_detailed, symbols)
            if not r:
                return None
            await self._emit_change(cache_key, r)
            return r

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=5)
        return result or {}

    def _fetch_quotes_detailed(self, symbols: list) -> Dict[str, dict]:
        svc = self.svc()
        if not svc.connect():
            return {}
        try:
            from futu import RET_OK
            ret, snap = svc._quote_ctx.get_market_snapshot(symbols[:400])
            if ret != RET_OK or snap.empty:
                return {}
            result = {}
            for _, row in snap.iterrows():
                code = str(row.get("code", ""))
                cur = safe_float(row.get("cur_price"))
                if cur <= 0:
                    cur = safe_float(row.get("last_price"))
                if cur <= 0:
                    cur = safe_float(row.get("prev_close_price"))
                if cur > 0:
                    prev_close = safe_float(row.get("prev_close_price"))
                    result[code] = {
                        "cur_price": round(cur, 2),
                        "prev_close": round(prev_close, 2) if prev_close > 0 else 0,
                        "name": str(row.get("name_en", row.get("name", ""))),
                        "high_price": round(safe_float(row.get("high_price")), 2),
                        "low_price": round(safe_float(row.get("low_price")), 2),
                        "volume": int(row.get("volume", 0)),
                        # P2 (2026-06-21): 富途snapshot扩展字段 — 一次调用替代Finnhub+StockTwits
                        "pe_ratio": round(safe_float(row.get("pe_ratio")), 2) or None,
                        "pe_ttm": round(safe_float(row.get("pe_ttm_ratio")), 2) or None,
                        "pb_ratio": round(safe_float(row.get("pb_ratio")), 2) or None,
                        "eps": round(safe_float(row.get("earning_per_share")), 2) or None,
                        "market_cap": round(safe_float(row.get("total_market_val")), 0) or None,
                        "net_asset": round(safe_float(row.get("net_asset")), 0) or None,
                        "net_profit": round(safe_float(row.get("net_profit")), 0) or None,
                        "dividend_ttm": round(safe_float(row.get("dividend_ttm")), 2) or None,
                        "turnover_rate": round(safe_float(row.get("turnover_rate")), 2) or None,
                        "amplitude": round(safe_float(row.get("amplitude")), 2) or None,
                        "bid_ask_ratio": round(safe_float(row.get("bid_ask_ratio")), 2) or None,
                        "volume_ratio": round(safe_float(row.get("volume_ratio")), 2) or None,
                        "high_52w": round(safe_float(row.get("highest52weeks_price")), 2) or None,
                        "low_52w": round(safe_float(row.get("lowest52weeks_price")), 2) or None,
                        "after_price": round(safe_float(row.get("after_price")), 2) or None,
                        "pre_price": round(safe_float(row.get("pre_price")), 2) or None,
                    }
            return result
        except Exception as e:
            logger.error(f"[DataService] batch detailed quotes failed: {e}")
            return {}

    # ═══════════════════════════════════════════
    #  VIX (文件缓存)
    # ═══════════════════════════════════════════

    async def get_vix(self) -> Optional[float]:
        async def _fetch():
            return await self._run(self._fetch_vix)

        return await self._deduplicated_fetch("vix", _fetch, ttl=30)

    def _fetch_vix(self) -> Optional[float]:
        try:
            vix_path = Path(__file__).parent.parent.parent / "data" / "vix_cache" / "vix_data.json"
            if vix_path.exists():
                # 新鲜度校验: 文件修改时间超过 10 分钟视为 stale (返回 None, 前端显示"--")
                file_age = time.time() - vix_path.stat().st_mtime
                if file_age > 600:  # 10min
                    logger.debug(f"[DataService] VIX cache stale ({file_age:.0f}s old)")
                    return None
                with open(vix_path) as f:
                    data = json.load(f)
                v = data.get("vix")
                if v and v > 0:
                    return round(float(v), 2)
        except Exception:
            pass
        return None

    # ═══════════════════════════════════════════
    #  Futu 扩展 API (板块/关联股票)
    # ═══════════════════════════════════════════

    async def get_owner_plate(self, symbol: str) -> dict:
        """获取股票所属板块 (Futu get_owner_plate)"""
        cache_key = f"plate_{symbol}"

        async def _fetch():
            r = await self._run(self._fetch_owner_plate, symbol)
            return r or None

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=3600)  # 板块信息低频变化
        return result or {}

    def _fetch_owner_plate(self, symbol: str) -> dict:
        svc = self.svc()
        if not svc.connect():
            return {}
        try:
            from futu import RET_OK
            ret, data = svc._quote_ctx.get_owner_plate([symbol])
            if ret == RET_OK and not data.empty:
                plates = []
                for _, row in data.iterrows():
                    plates.append({
                        "name": str(row.get("plate_name", "")),
                        "code": str(row.get("plate_code", "")),
                        "type": str(row.get("plate_type", "")),
                    })
                return {"symbol": symbol, "plates": plates}
        except Exception as e:
            logger.error(f"[DataService] owner_plate failed: {e}")
        return {}

    async def get_reference_stocks(self, symbol: str) -> list:
        """获取关联股票 (Futu get_referencestocklist)"""
        cache_key = f"ref_{symbol}"

        async def _fetch():
            r = await self._run(self._fetch_reference_stocks, symbol)
            return r or None

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=1800)
        return result or []

    def _fetch_reference_stocks(self, symbol: str) -> list:
        svc = self.svc()
        if not svc.connect():
            return []
        try:
            from futu import RET_OK
            ret, data = svc._quote_ctx.get_referencestocklist([symbol])
            if ret == RET_OK and not data.empty:
                stocks = []
                for _, row in data.iterrows():
                    stocks.append({
                        "symbol": str(row.get("code", "")),
                        "name": str(row.get("name", "")),
                    })
                return stocks
        except Exception as e:
            logger.debug(f"[DataService] reference_stocks failed: {e}")
        return []

    # ═══════════════════════════════════════════
    #  DB 查询 (统一连接)
    # ═══════════════════════════════════════════

    def _get_db(self) -> str:
        from app.core.db_utils import get_main_db
        return str(get_main_db())

    async def db_query(self, sql: str, params: tuple = (), db_path: str = None) -> list:
        """异步 DB 查询, 返回 list of dict"""
        return await self._run(self._db_query_sync, sql, params, db_path)

    def _db_query_sync(self, sql: str, params: tuple = (), db_path: str = None) -> list:
        path = db_path or self._get_db()
        try:
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[DataService] DB query failed: {e}")
            return []

    async def db_query_one(self, sql: str, params: tuple = (), db_path: str = None) -> Optional[dict]:
        rows = await self.db_query(sql, params, db_path)
        return rows[0] if rows else None

    async def db_execute(self, sql: str, params: tuple = (), db_path: str = None) -> int:
        """执行写操作 (UPDATE/INSERT/DELETE), 返回 rowcount"""
        return await self._run(self._db_execute_sync, sql, params, db_path)

    def _db_execute_sync(self, sql: str, params: tuple = (), db_path: str = None) -> int:
        path = db_path or self._get_db()
        try:
            conn = sqlite3.connect(path, timeout=10)
            cursor = conn.execute(sql, params)
            rowcount = cursor.rowcount
            conn.commit()
            conn.close()
            return rowcount
        except Exception as e:
            logger.error(f"[DataService] DB execute failed: {e}")
            raise

    # ═══════════════════════════════════════════
    #  K线 / 盘口 / 逐笔 (带缓存)
    # ═══════════════════════════════════════════

    async def get_kline(self, symbol: str, num: int = 60, ktype: str = "K_DAY") -> list:
        """K线数据 (缓存: 日K=300s, 分钟K=15s)"""
        ttl = 300 if ktype == "K_DAY" else 15
        cache_key = f"kline_{symbol}_{ktype}_{num}"

        async def _fetch():
            r = await self._run(self._fetch_kline, symbol, num, ktype)
            return r or None

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=ttl)
        return result or []

    def _fetch_kline(self, symbol: str, num: int, ktype: str) -> list:
        svc = self.svc()
        if not svc.connect():
            return []
        try:
            from futu import RET_OK
            svc._quote_ctx.subscribe([symbol], [ktype])
            import time; time.sleep(0.3)
            ret, data = svc._quote_ctx.get_cur_kline(symbol, num, ktype=ktype)
            if ret == RET_OK and not data.empty:
                return [dict(row) for _, row in data.iterrows()]
        except Exception as e:
            logger.debug(f"[DataService] kline {symbol} failed: {e}")
        return []

    async def get_orderbook(self, symbol: str, num: int = 10) -> dict:
        """盘口数据 (5s cache)"""
        cache_key = f"orderbook_{symbol}"

        async def _fetch():
            r = await self._run(self._fetch_orderbook, symbol, num)
            return r or None

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=5)
        return result or {}

    def _fetch_orderbook(self, symbol: str, num: int) -> dict:
        svc = self.svc()
        if not svc.connect():
            return {}
        try:
            from futu import RET_OK
            ret, data = svc._quote_ctx.get_order_book(symbol, num=num)
            if ret == RET_OK:
                return {
                    "Bid": data.get("Bid", []),
                    "Ask": data.get("Ask", []),
                }
        except Exception as e:
            logger.debug(f"[DataService] orderbook {symbol} failed: {e}")
        return {}

    async def get_rt_ticker(self, symbol: str, num: int = 100) -> list:
        """逐笔成交 (5s cache)"""
        cache_key = f"ticker_{symbol}"

        async def _fetch():
            r = await self._run(self._fetch_ticker, symbol, num)
            return r or None

        result = await self._deduplicated_fetch(cache_key, _fetch, ttl=5)
        return result or []

    def _fetch_ticker(self, symbol: str, num: int) -> list:
        svc = self.svc()
        if not svc.connect():
            return []
        try:
            from futu import RET_OK
            ret, data = svc._quote_ctx.get_rt_ticker(symbol, num=num)
            if ret == RET_OK and not data.empty:
                return [dict(row) for _, row in data.iterrows()]
        except Exception as e:
            logger.debug(f"[DataService] ticker {symbol} failed: {e}")
        return []

    # ═══════════════════════════════════════════
    #  单一持仓网关 (修复幽灵仓 2026-06-15)
    # ═══════════════════════════════════════════

    async def get_enriched_positions(self) -> List[Dict]:
        """统一持仓视图: 以 Futu 实时持仓为权威, SQLite open_positions 仅供元数据 (SL/TP/created_at)。

        修复幽灵仓: Dashboard 之前从两源各自读取后拼接, 不同步时会显示已平仓品种
        (例如 EXIT AMD 但实际无 AMD 持仓)。本方法保证只输出 Futu 中真实存在的持仓。
        顺便触发幽灵记录对账 (限流 5min)。

        返回字段对齐 position_intelligence.analyze_all_positions() 的输入要求。

        请求级去重: 复用 positions_raw 的 5s 缓存策略, 避免并发调用重复加载元数据。
        """
        result = await self._deduplicated_fetch(
            "enriched_positions", self._build_enriched_positions, ttl=5
        )
        return result or []

    async def _build_enriched_positions(self) -> List[Dict]:
        """实际构建 enriched 持仓视图 (供 _deduplicated_fetch 调用)"""
        raw_positions = await self.get_positions_raw()
        # 加载全量元数据 (含 is_anomalous), 同时用于合并与对账
        meta_map_full = await self._run(self._load_position_meta, False)

        enriched: List[Dict] = []
        futu_symbols: Set[str] = set()

        for p in (raw_positions or []):
            code = str(p.get("code", "") or "")
            if not code or not code.startswith("US."):
                continue
            # 过滤期权 (代码尾部含数字)
            bare = code[3:]
            if any(c.isdigit() for c in bare):
                continue
            qty = int(safe_float(p.get("qty", 0)))
            if qty == 0:
                continue

            avg_cost = safe_float(p.get("average_cost"))
            cur_price = safe_float(p.get("nominal_price"))
            # 优先用 pl_val (总盈亏, 含分批已实现), 与 Futu App 一致
            pl_val = safe_float(p.get("pl_val"))
            unrealized = pl_val if pl_val != 0 else safe_float(p.get("unrealized_pl"))
            direction = "SHORT" if qty < 0 else "LONG"
            futu_symbols.add(code)

            meta = meta_map_full.get(code) or {}
            # 异常记录 (is_anomalous=1) 不参与元数据合并 — 防止过期 SL/TP 误导决策
            if int(meta.get("is_anomalous", 0) or 0) == 1:
                meta = {}

            enriched.append({
                "symbol": code,
                "direction": direction,
                "size": abs(qty),
                "avg_entry_price": avg_cost,
                "current_price": cur_price,
                "unrealized_pnl": round(unrealized, 2),
                "stop_loss_price": meta.get("stop_loss_price"),
                "take_profit_price": meta.get("take_profit_price"),
                "created_at": meta.get("created_at", "") or "",
            })

        # 对账 (内部限流, 失败静默)
        try:
            await self._run(self._reconcile_ghosts, futu_symbols, meta_map_full)
        except Exception as e:
            logger.debug(f"[DataService] reconcile_ghosts skipped: {e}")

        # 即使空列表也返回 (会被缓存): 0 持仓账户避免重复 DB 加载
        return enriched

    def _load_position_meta(self, exclude_anomalous: bool = True) -> Dict[str, Dict]:
        """读取 open_positions 元数据映射 {symbol: {sl, tp, created_at, is_anomalous, updated_at}}.

        - 仅返回 size > 0 的真实持仓记录
        - exclude_anomalous=True 时过滤掉 is_anomalous=1 的记录
        - 使用实际 schema 列名: current_sl → stop_loss, initial_tp → take_profit, entry_time → created_at
        """
        sql = (
            "SELECT symbol, current_sl AS stop_loss_price, initial_tp AS take_profit_price, "
            "entry_time AS created_at, updated_at, is_anomalous "
            "FROM open_positions WHERE size > 0"
        )
        if exclude_anomalous:
            sql += " AND COALESCE(is_anomalous, 0) = 0"

        result: Dict[str, Dict] = {}
        path = self._get_db()
        try:
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
            conn.close()
            for r in rows:
                sym = r["symbol"] or ""
                if not sym:
                    continue
                sl = r["stop_loss_price"]
                tp = r["take_profit_price"]
                result[sym] = {
                    "stop_loss_price": float(sl) if sl else None,
                    "take_profit_price": float(tp) if tp else None,
                    "created_at": r["created_at"] or "",
                    "updated_at": r["updated_at"] or "",
                    "is_anomalous": int(r["is_anomalous"] or 0),
                }
        except Exception as e:
            logger.error(f"[DataService] _load_position_meta failed: {e}")
        return result

    def _reconcile_ghosts(self, futu_symbols: Set[str], meta_map: Dict[str, Dict]) -> None:
        """对账: 处理 SQLite 有但 Futu 无 的幽灵记录。

        - 限流: 5 分钟内最多执行一次
        - 未标记的 → 标 is_anomalous=1, updated_at=now
        - 已标记且 updated_at 超过 24h 的 → 直接 DELETE
        """
        now = time.time()
        if now - self._last_reconcile_time < 300:
            return
        self._last_reconcile_time = now

        ghosts_to_mark: List[str] = []
        ghosts_to_delete: List[str] = []

        for sym, meta in (meta_map or {}).items():
            if sym in futu_symbols:
                continue
            if int(meta.get("is_anomalous", 0) or 0) == 1:
                if self._is_meta_older_than(meta.get("updated_at", ""), hours=24):
                    ghosts_to_delete.append(sym)
            else:
                ghosts_to_mark.append(sym)

        if not ghosts_to_mark and not ghosts_to_delete:
            return

        now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        path = self._get_db()
        try:
            conn = sqlite3.connect(path, timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            for sym in ghosts_to_mark:
                conn.execute(
                    "UPDATE open_positions SET is_anomalous = 1, updated_at = ? "
                    "WHERE symbol = ?",
                    (now_iso, sym),
                )
            for sym in ghosts_to_delete:
                conn.execute("DELETE FROM open_positions WHERE symbol = ?", (sym,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[DataService] reconcile_ghosts DB write failed: {e}")
            return

        if ghosts_to_mark:
            logger.warning(
                f"[GhostReconcile] 标记幽灵仓 is_anomalous=1 ({len(ghosts_to_mark)}): {ghosts_to_mark}"
            )
        if ghosts_to_delete:
            logger.warning(
                f"[GhostReconcile] 删除已确认幽灵仓 (>24h, {len(ghosts_to_delete)}): {ghosts_to_delete}"
            )

    @staticmethod
    def _is_meta_older_than(updated_at: str, hours: int = 24) -> bool:
        """判断 SQLite updated_at 字符串是否早于 now-hours (UTC 比较, 容错多种格式)"""
        if not updated_at:
            return False
        s = str(updated_at).strip().replace("T", " ")
        for sep in ("+", "Z"):
            if sep in s:
                s = s.split(sep)[0].strip()
        if "." in s:
            s = s.split(".")[0].strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                ts = datetime.strptime(s, fmt)
                return (datetime.utcnow() - ts).total_seconds() > hours * 3600
            except ValueError:
                continue
        return False

    # ═══════════════════════════════════════════
    #  通用 async 执行器
    # ═══════════════════════════════════════════

    async def _deduplicated_fetch(self, cache_key: str, fetch_fn, ttl: float):
        """请求级去重: 缓存失效时只允许一个请求穿透到底层 (Thundering Herd 防护).

        使用 double-check locking 模式:
          1. 快速路径: 缓存命中直接返回 (无锁)
          2. 否则获取/创建该 key 的 asyncio.Lock
          3. 锁内 double-check: 其他协程可能已填充缓存
          4. 真正调用 fetch_fn 并写入缓存

        P0 修复 (2026-06-16):
          - 空结果也缓存 (哨兵值), 防止空数据时穿透
          - 锁获取超时 (30s), 防止 Futu 挂起时协程永久阻塞
          - 异常隔离: 穿透请求失败时缓存短 TTL 错误哨兵, 防止级联重试
          - setdefault 防锁字典竞态

        Args:
            cache_key: 缓存键 (与 self.cache 复用)
            fetch_fn: 无参 async 可调用对象, 返回拉取结果或 None
            ttl: 缓存有效期 (秒)
        """
        # 快速路径: 缓存命中直接返回 (用 _CACHE_MISS 区分 None 和未缓存)
        cached = self.cache.get(cache_key)
        if cached is not None:
            if cached is self._CACHE_MISS:
                return None
            return cached

        # 获取/创建该 key 的锁 (setdefault 防止竞态创建重复锁)
        lock = self._fetch_locks.setdefault(cache_key, asyncio.Lock())

        # 标准 try/finally 模式: 确保锁始终释放, 防止 CancelledError 导致锁泄漏
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), timeout=30)
            acquired = True

            # Double-check: 锁内再查一次, 可能其他协程已填充
            cached = self.cache.get(cache_key)
            if cached is not None:
                if cached is self._CACHE_MISS:
                    return None
                logger.debug(
                    f"[DataService] dedup hit (Thundering Herd avoided): {cache_key}"
                )
                return cached

            # 真正执行获取
            result = await fetch_fn()
            if result is not None:
                self.cache.set(cache_key, result, ttl=ttl)
            else:
                # 空结果也缓存 (短 TTL), 防止空数据穿透
                self.cache.set(cache_key, self._CACHE_MISS, ttl=min(ttl, 5))
            return result
        except asyncio.TimeoutError:
            # 锁获取超时 (Futu RPC 挂起), 降级返回 None
            logger.warning(f"[DataService] 锁超时 30s, 降级: {cache_key}")
            return None
        except Exception as e:
            # 异常隔离: 缓存短 TTL 错误哨兵, 防止级联重试
            logger.warning(f"[DataService] fetch 异常 {cache_key}: {e}")
            self.cache.set(cache_key, self._CACHE_MISS, ttl=2)
            return None
        finally:
            if acquired:
                lock.release()
                # LRU: 释放后若无竞争者, 从字典移除, 防止长期运行内存泄漏
                if not lock.locked():
                    self._fetch_locks.pop(cache_key, None)

    async def _run(self, func, *args, **kwargs):
        """在线程池中执行阻塞调用"""
        loop = asyncio.get_running_loop()
        if kwargs:
            from functools import partial
            return await loop.run_in_executor(self._pool, partial(func, *args, **kwargs))
        return await loop.run_in_executor(self._pool, func, *args, **kwargs)

    def invalidate_cache(self, key: str = None):
        """清除缓存 (下单/平仓后调用)"""
        self.cache.invalidate(key)

    # ═══════════════════════════════════════════
    #  领域查询 (2026-06-20: 从路由层下沉)
    # ═══════════════════════════════════════════

    @staticmethod
    def _snap_db() -> str:
        """signal_snapshots.db 路径"""
        from app.analysis.signal_ic_tracker import _get_db_path
        return str(_get_db_path())

    async def get_signal_counts(self, today: str) -> dict:
        """信号快照计数: 总数 + 有效(strength≥30)"""
        snap_db = self._snap_db()
        row1 = await self.db_query_one(
            "SELECT COUNT(*) as cnt FROM signal_snapshots WHERE trade_date = ? "
            "AND COALESCE(trade_mode, 'simulate') = 'live'",
            (today,), db_path=snap_db,
        )
        row2 = await self.db_query_one(
            "SELECT COUNT(*) as cnt FROM signal_snapshots WHERE trade_date = ? "
            "AND strength >= 30 AND confidence > 0 AND COALESCE(trade_mode, 'simulate') = 'live'",
            (today,), db_path=snap_db,
        )
        return {"total": row1["cnt"] if row1 else 0, "qualified": row2["cnt"] if row2 else 0}

    async def get_executed_count(self, today: str) -> int:
        """当日已平仓交易数 (排除已废弃/禁用策略)"""
        row = await self.db_query_one(
            "SELECT COUNT(*) as cnt FROM trade_records "
            "WHERE trade_date = ? AND exit_price > 0 "
            "AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP')",
            (today,),
        )
        return row["cnt"] if row else 0

    async def get_attribution_score(self, trace_id: str = "", symbol: str = "", trade_date: str = "") -> Optional[dict]:
        """交易归因评分"""
        clause = []
        params = []
        if trace_id:
            clause.append("trace_id = ?")
            params.append(trace_id)
        if symbol:
            clause.append("symbol = ?")
            params.append(symbol)
        if trade_date:
            clause.append("trade_date = ?")
            params.append(trade_date)
        where = " AND ".join(clause) if clause else "1=1"
        return await self.db_query_one(
            f"SELECT overall_score FROM trade_attribution WHERE {where} ORDER BY rowid DESC LIMIT 1",
            tuple(params),
        )

    async def get_diagnostic_data(self, key: str) -> Optional[str]:
        """诊断结果 JSON"""
        row = await self.db_query_one(
            "SELECT data FROM diagnostic_results WHERE key = ? ORDER BY rowid DESC LIMIT 1",
            (key,),
        )
        return row["data"] if row else None

    async def get_recent_trades(self, symbol: str = "", limit: int = 20) -> list:
        """最近交易记录"""
        if symbol:
            return await self.db_query(
                "SELECT * FROM trade_records WHERE symbol=? AND exit_price>0 ORDER BY exit_time DESC LIMIT ?",
                (symbol, limit),
            )
        return await self.db_query(
            "SELECT * FROM trade_records WHERE exit_price>0 ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )

    async def get_open_position_owners(self) -> list:
        """持仓归属列表"""
        return await self.db_query(
            "SELECT symbol, COALESCE(owner,'auto') as owner, entry_time FROM open_positions"
        )

    async def get_total_pnl(self, days: int = 0) -> float:
        """累计已实现盈亏 — 仅计已平仓交易 (exit_price > 0)"""
        date_filter = f"AND trade_date >= date('now', '-{days} days')" if days > 0 else ""
        row = await self.db_query_one(
            f"SELECT COALESCE(SUM(realized_pnl),0) as pnl FROM trade_records "
            f"WHERE source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') AND exit_price > 0 {date_filter}"
        )
        return float(row["pnl"]) if row else 0.0

    async def get_recent_pnl_ids(self, limit: int = 20) -> list:
        """最近交易ID和PnL (用于归因匹配)"""
        return await self.db_query(
            f"SELECT id, realized_pnl FROM trade_records ORDER BY id DESC LIMIT ?",
            (limit,),
        )


# ── 全局单例 ──
_data_service: Optional[DataService] = None


def get_data_service() -> DataService:
    global _data_service
    if _data_service is None:
        _data_service = DataService()
    return _data_service
