"""
持仓实时查询模块 (Live Position Query)

以富途服务端数据为权威来源，实时查询当前持仓状态。
替代本地 DB 缓存，确保仓位判断始终基于真实数据。

作者: FTSeek Team
日期: 2026-04-15
"""

import logging
import time
import threading
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("ftseek.factors.live_position_query")

# Futu imports
try:
    from futu import (
        SysConfig, TrdEnv,
        OpenHKTradeContext, OpenUSTradeContext,
    )
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False


@dataclass
class LivePosition:
    """实时持仓数据"""
    code: str
    name: str
    quantity: float = 0.0
    can_sell_qty: float = 0.0
    cost_price: float = 0.0
    current_price: float = 0.0
    market_val: float = 0.0
    pl_val: float = 0.0           # 盈亏金额
    pl_ratio: float = 0.0         # 盈亏百分比
    position_side: str = "LONG"   # LONG / SHORT
    market: str = "HK"            # HK / US / CN
    unrealized_pl: float = 0.0
    realized_pl: float = 0.0
    today_pl_val: float = 0.0
    query_time: float = 0.0       # 查询时间戳

    @property
    def is_active(self) -> bool:
        """是否为有效持仓 (数量 > 0)"""
        return abs(self.quantity) > 0

    @property
    def is_long(self) -> bool:
        return self.position_side == "LONG"

    @property
    def is_short(self) -> bool:
        return self.position_side == "SHORT"


@dataclass
class AccountInfo:
    """账户信息"""
    total_assets: float = 0.0
    market_val: float = 0.0
    cash: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    buying_power: float = 0.0
    currency: str = "HKD"


class LivePositionQuery:
    """
    实时持仓查询器

    直接查询富途 OpenD 交易接口，获取真实持仓数据。
    内置 30 秒缓存，避免频繁 API 调用。
    """

    CACHE_TTL = 30  # 缓存有效期 (秒)

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._cache: List[LivePosition] = []
        self._cache_time: float = 0.0
        self._account_cache: Dict[str, AccountInfo] = {}
        self._account_cache_time: float = 0.0

    def _safe_float(self, val, default=0.0) -> float:
        """安全转换 float"""
        if val is None:
            return default
        s = str(val).strip()
        if s in ('N/A', 'NaN', 'null', '--', ''):
            return default
        try:
            return float(s)
        except (ValueError, TypeError):
            return default

    def get_positions(self, force_refresh: bool = False) -> List[LivePosition]:
        """
        获取所有持仓 (含已清仓)

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            持仓列表
        """
        with self._lock:
            now = time.time()
            if not force_refresh and self._cache and (now - self._cache_time) < self.CACHE_TTL:
                return list(self._cache)

        # 缓存过期或未命中，查询富途
        positions = self._query_all_positions()

        with self._lock:
            self._cache = positions
            self._cache_time = time.time()

        return positions

    def get_active_positions(self, force_refresh: bool = False,
                             market: Optional[str] = None) -> List[LivePosition]:
        """
        获取有效持仓 (quantity > 0)

        Args:
            force_refresh: 强制刷新
            market: 过滤市场 (HK / US / CN), None 为全部

        Returns:
            有效持仓列表
        """
        all_pos = self.get_positions(force_refresh)
        active = [p for p in all_pos if p.is_active]
        if market:
            active = [p for p in active if p.market == market]
        return active

    def count_positions(self, market: Optional[str] = None) -> int:
        """
        统计有效持仓数

        Args:
            market: 过滤市场, None 为全部

        Returns:
            有效持仓数量
        """
        return len(self.get_active_positions(market=market))

    def has_position(self, code: str) -> bool:
        """检查是否持有某只股票"""
        active = self.get_active_positions()
        return any(p.code == code for p in active)

    def get_position(self, code: str) -> Optional[LivePosition]:
        """获取某只股票的持仓信息"""
        active = self.get_active_positions()
        for p in active:
            if p.code == code:
                return p
        return None

    def can_buy(self, code: str, max_positions: int = 12,
                market: Optional[str] = None) -> tuple:
        """
        检查是否可以买入

        Args:
            code: 股票代码
            max_positions: 最大持仓数
            market: 市场限制 (HK / US)

        Returns:
            (can_buy: bool, reason: str, current_count: int)
        """
        current_count = self.count_positions(market=market)

        # 已持有 → 可以加仓
        if self.has_position(code):
            return True, f"已持有 {code}，可以加仓", current_count

        # 未持有但仓位已满
        if current_count >= max_positions:
            return False, (
                f"持仓已满 ({current_count}/{max_positions})，"
                f"需先卖出才可买入 {code}"
            ), current_count

        # 可以买入
        return True, f"当前持仓 {current_count}/{max_positions}，可买入 {code}", current_count

    def _query_all_positions(self) -> List[LivePosition]:
        """查询所有市场持仓"""
        if not FUTU_AVAILABLE:
            logger.warning("Futu 模块不可用，返回空列表")
            return []

        all_positions = []
        now = time.time()

        # HK
        try:
            ctx = OpenHKTradeContext(self.host, self.port)
            ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
            if ret == 0:
                for _, row in data.iterrows():
                    pos = LivePosition(
                        code=str(row.get('code', '')),
                        name=str(row.get('stock_name', '')),
                        quantity=self._safe_float(row.get('qty', 0)),
                        can_sell_qty=self._safe_float(row.get('can_sell_qty', 0)),
                        cost_price=self._safe_float(row.get('cost_price', 0)),
                        current_price=self._safe_float(row.get('nominal_price', 0)),
                        market_val=self._safe_float(row.get('market_val', 0)),
                        pl_val=self._safe_float(row.get('pl_val', 0)),
                        pl_ratio=self._safe_float(row.get('pl_ratio', 0)),
                        position_side=str(row.get('position_side', 'LONG')),
                        market='HK',
                        unrealized_pl=self._safe_float(row.get('unrealized_pl', 0)),
                        realized_pl=self._safe_float(row.get('realized_pl', 0)),
                        today_pl_val=self._safe_float(row.get('today_pl_val', 0)),
                        query_time=now,
                    )
                    all_positions.append(pos)
            ctx.close()
        except Exception as e:
            logger.error(f"查询港股持仓失败: {e}")

        # US
        try:
            ctx = OpenUSTradeContext(self.host, self.port)
            ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
            if ret == 0:
                for _, row in data.iterrows():
                    pos = LivePosition(
                        code=str(row.get('code', '')),
                        name=str(row.get('stock_name', '')),
                        quantity=self._safe_float(row.get('qty', 0)),
                        can_sell_qty=self._safe_float(row.get('can_sell_qty', 0)),
                        cost_price=self._safe_float(row.get('cost_price', 0)),
                        current_price=self._safe_float(row.get('nominal_price', 0)),
                        market_val=self._safe_float(row.get('market_val', 0)),
                        pl_val=self._safe_float(row.get('pl_val', 0)),
                        pl_ratio=self._safe_float(row.get('pl_ratio', 0)),
                        position_side=str(row.get('position_side', 'LONG')),
                        market='US',
                        unrealized_pl=self._safe_float(row.get('unrealized_pl', 0)),
                        realized_pl=self._safe_float(row.get('realized_pl', 0)),
                        today_pl_val=self._safe_float(row.get('today_pl_val', 0)),
                        query_time=now,
                    )
                    all_positions.append(pos)
            ctx.close()
        except Exception as e:
            logger.error(f"查询美股持仓失败: {e}")

        logger.info(f"实时持仓查询: {len(all_positions)} 条, "
                     f"{sum(1 for p in all_positions if p.is_active)} 只有效")
        return all_positions

    def get_account_summary(self) -> Dict[str, Any]:
        """获取账户摘要"""
        active = self.get_active_positions()
        hk_count = sum(1 for p in active if p.market == 'HK')
        us_count = sum(1 for p in active if p.market == 'US')
        total_pl = sum(p.pl_val for p in active)

        return {
            'total_positions': len(active),
            'hk_positions': hk_count,
            'us_positions': us_count,
            'total_market_val': sum(p.market_val for p in active),
            'total_pl': total_pl,
            'best_position': max(active, key=lambda p: p.pl_ratio).code if active else 'N/A',
            'worst_position': min(active, key=lambda p: p.pl_ratio).code if active else 'N/A',
            'query_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }


# 全局单例
_query = None

def get_live_position_query(host: str = "127.0.0.1", port: int = 11111) -> LivePositionQuery:
    """获取全局单例"""
    global _query
    if _query is None:
        _query = LivePositionQuery(host, port)
    return _query


def reset_query():
    """重置 (测试用)"""
    global _query
    _query = None
