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
from typing import Any, Dict, List, Optional

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

    def __init__(self):
        self.cache = _SharedCache()
        self._pool = ThreadPoolExecutor(max_workers=8)

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
        cached = self.cache.get("account_raw")
        if cached is not None:
            return cached
        result = await self._run(self._fetch_account)
        if result:
            self.cache.set("account_raw", result, ttl=10)
        return result or {}

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
        cached = self.cache.get("positions_raw")
        if cached is not None:
            return cached
        result = await self._run(self._fetch_positions)
        if result:
            self.cache.set("positions_raw", result, ttl=5)
        return result or []

    def _fetch_positions(self) -> list:
        svc, trd_env, acc_id = self._get_env_ctx()
        if not svc:
            return []
        try:
            from futu import RET_OK
            ret, data = svc._broker_ctx.position_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret == RET_OK and not data.empty:
                return [dict(row) for _, row in data.iterrows()]
        except Exception as e:
            logger.error(f"[DataService] positions failed: {e}")
        return []

    async def get_today_orders_raw(self) -> list:
        """Futu order_list_query (10s cache)"""
        cached = self.cache.get("orders_raw")
        if cached is not None:
            return cached
        result = await self._run(self._fetch_orders)
        if result is not None:
            self.cache.set("orders_raw", result, ttl=10)
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
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        result = await self._run(self._fetch_snapshot, codes)
        if result:
            self.cache.set(cache_key, result, ttl=ttl)
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
        """批量报价 (5s cache), 返回 {symbol: price}"""
        if not symbols:
            return {}
        import hashlib
        _h = hashlib.md5(','.join(sorted(symbols)).encode()).hexdigest()[:12]
        cache_key = f"quotes_{_h}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        result = await self._run(self._fetch_quotes, symbols)
        if result:
            self.cache.set(cache_key, result, ttl=5)
        return result or {}

    def _fetch_quotes(self, symbols: list) -> Dict[str, float]:
        svc = self.svc()
        if not svc.connect():
            return {}
        try:
            from futu import RET_OK
            ret, snap = svc._quote_ctx.get_market_snapshot(symbols[:400])
            if ret != RET_OK or snap.empty:
                return {}
            prices = {}
            for _, row in snap.iterrows():
                code = row.get("code", "")
                cur = safe_float(row.get("cur_price"))
                if cur <= 0:
                    cur = safe_float(row.get("last_price"))
                if cur <= 0:
                    cur = safe_float(row.get("prev_close_price"))
                if cur > 0:
                    prices[code] = cur
            return prices
        except Exception as e:
            logger.error(f"[DataService] batch quotes failed: {e}")
            return {}

    async def get_batch_quotes_detailed(self, symbols: list) -> Dict[str, dict]:
        """批量详细报价 (5s cache), 返回 {symbol: {cur_price, prev_close, name, ...}}"""
        if not symbols:
            return {}
        import hashlib
        _h = hashlib.md5(','.join(sorted(symbols)).encode()).hexdigest()[:12]
        cache_key = f"quotes_d_{_h}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        result = await self._run(self._fetch_quotes_detailed, symbols)
        if result:
            self.cache.set(cache_key, result, ttl=5)
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
                    }
            return result
        except Exception as e:
            logger.error(f"[DataService] batch detailed quotes failed: {e}")
            return {}

    # ═══════════════════════════════════════════
    #  VIX (文件缓存)
    # ═══════════════════════════════════════════

    async def get_vix(self) -> Optional[float]:
        cached = self.cache.get("vix")
        if cached is not None:
            return cached
        result = await self._run(self._fetch_vix)
        if result is not None:
            self.cache.set("vix", result, ttl=30)
        return result

    def _fetch_vix(self) -> Optional[float]:
        try:
            vix_path = Path(__file__).parent.parent.parent / "data" / "vix_cache" / "vix_data.json"
            if vix_path.exists():
                with open(vix_path) as f:
                    data = json.load(f)
                v = data.get("vix")
                if v and v > 0:
                    return round(float(v), 2)
        except Exception:
            pass
        return None

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

    # ═══════════════════════════════════════════
    #  通用 async 执行器
    # ═══════════════════════════════════════════

    async def _run(self, func, *args, **kwargs):
        """在线程池中执行阻塞调用"""
        loop = asyncio.get_running_loop()
        if kwargs:
            from functools import partial
            return await loop.run_in_executor(self._pool, partial(func, *args, **kwargs))
        return await loop.run_in_executor(self._pool, func, *args)

    def invalidate_cache(self, key: str = None):
        """清除缓存 (下单/平仓后调用)"""
        self.cache.invalidate(key)


# ── 全局单例 ──
_data_service: Optional[DataService] = None


def get_data_service() -> DataService:
    global _data_service
    if _data_service is None:
        _data_service = DataService()
    return _data_service
