"""
信号处理器 — 共识评估、信号链记录、多周期趋势计算

从 data_subscriber.py 拆分出来，职责：
1. 提交信号到共识引擎
2. 处理共识回调 (_on_consensus)
3. 多周期趋势计算 (MTF)
4. K 线数据处理和指标计算

不执行交易，不管理订阅，只处理信号和指标。
"""

import logging
import time
import threading
from typing import Optional, Dict, Any, List, Callable, Tuple
from collections import defaultdict, deque

from app.core.config_loader import cfg
from app.core.trading_env import get_trading_env

logger = logging.getLogger("ftseek.intraday.signal_processor")


class SignalProcessor:
    """
    信号处理器
    
    职责：共识引擎交互 + 多周期趋势计算 + K 线处理。
    通过回调函数将共识结果传递给外部执行层。
    """
    
    def __init__(self, consensus_engine, adaptive_weight_manager=None):
        """
        Args:
            consensus_engine: 共识引擎实例
            adaptive_weight_manager: 自适应权重管理器（可选）
        """
        self._consensus = consensus_engine
        self._adaptive_weight_manager = adaptive_weight_manager

        # K 线缓存 (P0 修复 2026-04-29: 使用 deque 限制大小，防止内存泄漏)
        self._kline_buffers: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: {k: deque(maxlen=500) for k in ["K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"]}
        )

        # 多周期趋势缓存 (P0 修复 2026-04-29: 限制数量防止无限增长)
        self._mtf_trends: Dict[str, Dict[str, str]] = defaultdict(
            lambda: {k: "neutral" for k in ["K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"]}
        )

        # ATR 缓存: code → atr_value (使用 5M K线计算)
        self._atr_cache: Dict[str, float] = {}
        self._atr_period = 14

        self._max_codes = 100  # 最多缓存 100 只股票的K线数据
        
        # 线程安全
        self._trade_locks: Dict[str, threading.Lock] = {}
        self._lock_timestamps: Dict[str, float] = {}  # P0 修复: 分离时间戳, 避免覆盖 Lock
        
        # 回调函数
        self._on_consensus_result: Optional[Callable] = None
    
    def set_callbacks(self, on_consensus_result=None):
        """设置共识结果回调"""
        self._on_consensus_result = on_consensus_result
    
    # ===== 信号提交 =====
    
    def submit_signal(self, signal):
        """提交信号到共识引擎"""
        if signal is None:
            return
        self._consensus.submit(signal)
    
    # ===== 共识回调 =====
    
    def on_consensus(self, result):
        """
        共识引擎回调入口

        Args:
            result: ConsensusResult 实例
        """
        code = result.code

        # P0 修复 2026-04-29: 清理过期锁 (超过 5 分钟), 使用独立 _lock_timestamps
        now = time.time()
        expired = [k for k, ts in list(self._lock_timestamps.items()) if now - ts > 300]
        for k in expired:
            self._trade_locks.pop(k, None)
            self._lock_timestamps.pop(k, None)

        # 幂等锁：防止同一标的并发处理
        lock = self._trade_locks.setdefault(code, threading.Lock())
        if not lock.acquire(blocking=False):
            logger.debug(f"[幂等] {code} 上一笔交易仍在处理，跳过本次共识回调")
            return

        self._lock_timestamps[code] = now

        try:
            self._process_consensus_inner(result)
        finally:
            lock.release()
    
    def _process_consensus_inner(self, result):
        """共识结果处理（已持有锁）"""
        code = result.code
        
        # 调用外部回调（风控 Pipeline + 执行层）
        if self._on_consensus_result:
            self._on_consensus_result(result)
    
    # ===== 多周期趋势计算 =====
    
    def update_mtf_trend(self, code: str, k_type: str, kline: dict) -> Optional[str]:
        """
        更新多周期趋势状态
        
        Args:
            code: 股票代码
            k_type: K 线类型 (K_1M/K_5M/K_15M 等)
            kline: K 线数据 dict
        
        Returns:
            新的趋势方向 ("up"/"down"/"neutral") 或 None
        """
        try:
            # P0 修复 2026-04-29: 限制缓存大小，防止内存泄漏
            if len(self._kline_buffers) > self._max_codes:
                # 清理最旧的条目
                oldest_key = next(iter(self._kline_buffers))
                del self._kline_buffers[oldest_key]
                if oldest_key in self._mtf_trends:
                    del self._mtf_trends[oldest_key]
            
            buf = self._kline_buffers[code][k_type]
            
            # 推入 K 线
            closes = kline.get('close', 0)
            if closes:
                buf.append(kline)
            
            # 获取收盘价序列
            closes = [k.get('close', 0) for k in buf if k.get('close', 0) > 0]
            
            min_len_map = {
                "K_1M": 15, "K_3M": 15, "K_5M": 18,
                "K_15M": 25, "K_30M": 38, "K_60M": 38, "K_DAY": 55,
            }
            min_len = min_len_map.get(k_type, 20)
            
            if len(closes) < min_len:
                return None
            
            current_price = closes[-1]
            
            # 计算 MA
            ma_fast, ma_slow = self._compute_ma(closes, k_type)
            if ma_fast is None or ma_slow is None:
                return None
            
            # 计算 RSI
            from app.strategy.indicators import rsi as calc_rsi
            rsi_val = calc_rsi(closes, period=14)
            
            # 判断趋势
            trend = self._judge_trend(current_price, ma_fast, ma_slow, rsi_val, k_type)
            self._mtf_trends[code][k_type] = trend
            
            return trend
        except Exception as e:
            logger.debug(f"[MTF] {code} {k_type} 更新失败: {e}")
            return None
    
    def _compute_ma(self, closes: list, k_type: str) -> Tuple[Optional[float], Optional[float]]:
        """计算快慢均线"""
        ma_map = {
            "K_1M": (5, 10), "K_3M": (5, 10), "K_5M": (8, 13),
            "K_15M": (8, 21), "K_30M": (13, 34), "K_60M": (13, 34), "K_DAY": (20, 50),
        }
        
        fast_period, slow_period = ma_map.get(k_type, (10, 20))
        
        if len(closes) < slow_period:
            return None, None
        
        ma_fast = sum(closes[-fast_period:]) / fast_period
        ma_slow = sum(closes[-slow_period:]) / slow_period
        
        return ma_fast, ma_slow
    
    def _judge_trend(self, price: float, ma_fast: float, ma_slow: float,
                     rsi: Optional[float], k_type: str) -> str:
        """判断趋势方向"""
        rsi_val = rsi or 50
        
        # 不同周期的 RSI 阈值
        rsi_thresholds = {
            "K_1M": (52, 48), "K_3M": (51, 49), "K_5M": (51, 49),
            "K_15M": (50, 50), "K_30M": (50, 50), "K_60M": (50, 50), "K_DAY": (50, 50),
        }
        rsi_up, rsi_down = rsi_thresholds.get(k_type, (50, 50))
        
        rsi_up_bias = rsi_val > rsi_up
        rsi_down_bias = rsi_val < rsi_down
        
        if ma_fast > ma_slow and price > ma_fast and rsi_up_bias:
            return "up"
        elif ma_fast < ma_slow and price < ma_fast and rsi_down_bias:
            return "down"
        else:
            return "neutral"
    
    def get_mtf_alignment(self, code: str) -> Dict[str, Any]:
        """
        获取多周期对齐度
        
        Returns:
            {
                "aligned": True/False,
                "direction": "up"/"down"/"mixed",
                "score": 0.0-1.0,
                "trends": {"K_1M": "up", ...}
            }
        """
        trends = dict(self._mtf_trends.get(code, {}))
        
        up_count = sum(1 for t in trends.values() if t == "up")
        down_count = sum(1 for t in trends.values() if t == "down")
        total = len(trends)
        
        if total == 0:
            return {"aligned": False, "direction": "unknown", "score": 0.0, "trends": trends}
        
        # 计算对齐度
        max_agreement = max(up_count, down_count)
        score = max_agreement / total
        
        if up_count > down_count:
            direction = "up"
        elif down_count > up_count:
            direction = "down"
        else:
            direction = "mixed"
        
        aligned = score >= 0.7  # 70% 以上周期一致视为对齐
        
        return {
            "aligned": aligned,
            "direction": direction,
            "score": score,
            "trends": trends,
        }
    
    # ===== K 线处理 =====
    
    def process_kline(self, code: str, kline: dict, k_type: str) -> Dict[str, Any]:
        """
        处理单根 K 线

        Returns:
            {
                "trend_updated": True/False,
                "new_trend": "up"/"down"/"neutral",
                "mtf_alignment": {...},
                "atr": float or None,
            }
        """
        result = {"trend_updated": False, "new_trend": None, "mtf_alignment": None, "atr": None}

        # 更新多周期趋势
        new_trend = self.update_mtf_trend(code, k_type, kline)
        if new_trend:
            result["trend_updated"] = True
            result["new_trend"] = new_trend
            result["mtf_alignment"] = self.get_mtf_alignment(code)

        # 计算 ATR (仅对 5M K线, 用于离场管理)
        if k_type == "K_5M":
            atr = self._compute_atr(code, k_type)
            if atr is not None:
                result["atr"] = atr

        return result

    # =================================================================
    #  ATR 计算 (用于 DynamicExitManager 动态止损)
    # =================================================================

    def _compute_atr(self, code: str, k_type: str = "K_5M") -> Optional[float]:
        """计算 ATR(14) — 使用 True Range 的 EMA 平滑"""
        buf = list(self._kline_buffers.get(code, {}).get(k_type, []))
        if len(buf) < self._atr_period + 1:
            return None

        tr_values = []
        for i in range(1, len(buf)):
            h = buf[i].get('high', 0)
            l = buf[i].get('low', 0)
            prev_c = buf[i - 1].get('close', 0)
            if h <= 0 or l <= 0 or prev_c <= 0:
                continue
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_values.append(tr)

        if len(tr_values) < self._atr_period:
            return None

        # Wilder's smoothed ATR
        atr = sum(tr_values[:self._atr_period]) / self._atr_period
        for tr in tr_values[self._atr_period:]:
            atr = (atr * (self._atr_period - 1) + tr) / self._atr_period

        self._atr_cache[code] = atr
        return atr

    def get_latest_atr(self, code: str) -> Optional[float]:
        """获取最新的 ATR(14) 绝对值"""
        return self._atr_cache.get(code)

    # ===== 状态查询 =====

    def get_kline_buffer(self, code: str, k_type: str) -> list:
        """获取 K 线缓存"""
        return list(self._kline_buffers.get(code, {}).get(k_type, []))

    def get_mtf_trends(self, code: str) -> Dict[str, str]:
        """获取多周期趋势"""
        return dict(self._mtf_trends.get(code, {}))
