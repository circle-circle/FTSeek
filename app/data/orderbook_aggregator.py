"""
FTSeek Phase 3: 盘口数据异步聚合器 (OrderBook Aggregator)

设计原则:
- 异步计算盘口因子，不阻塞主 DataSubscriber 推送循环。
- 将原始 OrderBook/Tick 数据降维成 1 个 dict 因子，供策略层直接读取。
- 支持 Tier2 短线策略快速决策。

因子列表:
- bid_ask_imbalance: (买1-5量 - 卖1-5量) / (买1-5量 + 卖1-5量) -> 范围 [-1, 1]
- large_buy_flow: 最近 N 笔大单买入量 (需结合 Tick 数据)
- updated_at: 最后一次更新时间

作者: FTSeek Team
日期: 2026-04-16
"""

import time
import threading
import logging
from typing import Dict, Any, Optional
from collections import deque

logger = logging.getLogger("ftseek.ob_aggregator")

class OrderBookAggregator:
    """异步盘口因子聚合器 (单例模式)"""
    
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'OrderBookAggregator':
        """获取全局单例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        # {code: {"bid_ask_imbalance": 0.5, "updated_at": 1680000000.0, ...}}
        self._factors: Dict[str, Dict[str, Any]] = {}
        self._update_lock = threading.Lock()
        
        # 历史 Tick 缓冲区 (用于计算大单流向) - 保留最近 100 笔
        self._tick_buffer: Dict[str, deque] = {}
        self._max_buffer_size = 100
        
        logger.info("✅ 盘口聚合器已初始化")

    def update_orderbook(self, code: str, order_book_data: Dict):
        """
        更新盘口数据并计算因子 (由 DataSubscriber 的 on_push 回调触发)
        
        Args:
            code: 股票代码
            order_book_data: 富途 OrderBook 原始数据
        """
        try:
            # 计算买卖盘失衡 (Bid-Ask Imbalance)
            # 格式通常: {'Bid': [{'price':..., 'volume':...}, ...], 'Ask': [...]}
            bid_vols = [b.get('volume', 0) for b in order_book_data.get('Bid', [])]
            ask_vols = [a.get('volume', 0) for a in order_book_data.get('Ask', [])]
            
            bid_total = sum(bid_vols)
            ask_total = sum(ask_vols)
            
            # 防止除以 0
            total_vol = bid_total + ask_total
            if total_vol == 0:
                imbalance = 0.0
            else:
                imbalance = (bid_total - ask_total) / total_vol

            with self._update_lock:
                self._factors[code] = {
                    "bid_ask_imbalance": round(imbalance, 4),
                    "bid_vol_1_5": bid_total,
                    "ask_vol_1_5": ask_total,
                    "updated_at": time.time()
                }
        except Exception as e:
            logger.debug(f"[盘口] 计算因子异常 {code}: {e}")

    def update_tick(self, code: str, tick_data: Dict):
        """
        更新逐笔明细 (用于计算大单流向)
        
        Args:
            code: 股票代码
            tick_data: 富途 Ticker 原始数据
        """
        try:
            with self._update_lock:
                if code not in self._tick_buffer:
                    self._tick_buffer[code] = deque(maxlen=self._max_buffer_size)
                
                # 记录大单 (成交量 > 500股)
                vol = tick_data.get('volume', 0)
                if vol >= 500:
                    direction = tick_data.get('direction', 'NEUTRAL') # BUY, SELL, NEUTRAL
                    self._tick_buffer[code].append({
                        "time": tick_data.get('time', ''),
                        "vol": vol,
                        "dir": direction
                    })
                    
                    # 实时计算大单净买入
                    buf = self._tick_buffer[code]
                    buy_vol = sum(t['vol'] for t in buf if t['dir'] == 'BUY')
                    sell_vol = sum(t['vol'] for t in buf if t['dir'] == 'SELL')
                    
                    if code in self._factors:
                        self._factors[code]['large_buy_flow'] = buy_vol - sell_vol
                        self._factors[code]['updated_at'] = time.time()
        except Exception as e:
            logger.debug(f"[逐笔] 计算因子异常 {code}: {e}")

    def get_factor(self, code: str) -> Dict[str, Any]:
        """获取某只股票的当前盘口因子"""
        with self._update_lock:
            return self._factors.get(code, {})

    def is_stale(self, code: str, max_age_sec: float = 5.0) -> bool:
        """判断盘口数据是否过期 (超过 5 秒视为失效)"""
        factor = self.get_factor(code)
        if not factor:
            return True
        return (time.time() - factor.get('updated_at', 0)) > max_age_sec
