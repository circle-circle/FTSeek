"""
三重屏障标注 (Triple Barrier Method)

逻辑来源: Advances in Financial Machine Learning (Marcos Lopez de Prado)
用途: 为机器学习模型或策略评估提供高质量的标签 (Labeling)
      解决传统固定时间收益无法反映真实交易路径风险的问题。

更新时间: 2026-04-14
"""

import logging
import pandas as pd
import numpy as np
from typing import Tuple, Dict, Optional

logger = logging.getLogger("ftseek.factors.triple_barrier")


class TripleBarrierLabeler:
    def __init__(self, pt_sl: Tuple[float, float] = (2.0, 1.0),
                 t_exp_bars: int = 24, min_ret: float = 0.0):
        """
        初始化
        
        Args:
            pt_sl: (止盈倍数，止损倍数)。基于 ATR 计算屏障距离。
                   例如 (2.0, 1.0) 表示止盈距离是 2 倍 ATR，止损是 1 倍 ATR。
            t_exp_bars: 最大持仓 K 线数量 (垂直屏障)。
            min_ret: 垂直屏障触发时的最小收益率阈值 (用于判定正负)。
        """
        self.pt_sl = pt_sl
        self.t_exp_bars = t_exp_bars
        self.min_ret = min_ret

    def label_trades(self, trades: list, ohlcv_df: pd.DataFrame, 
                     atr_series: pd.Series = None) -> pd.DataFrame:
        """
        对一系列交易记录进行三重屏障标注
        
        Args:
            trades: 交易记录列表 (对象需有 entry_price, enter_date, exit_price, exit_date)
            ohlcv_df: OHLCV 数据，用于获取持仓期间的最高/最低价
            atr_series: ATR 序列，用于计算动态屏障 (可选)
            
        Returns:
            包含标注信息的 DataFrame
        """
        if not trades or ohlcv_df.empty:
            return pd.DataFrame()

        results = []

        # 确保索引是 DatetimeIndex
        if not isinstance(ohlcv_df.index, pd.DatetimeIndex):
            ohlcv_df.index = pd.to_datetime(ohlcv_df.index)

        for trade in trades:
            entry_time = pd.to_datetime(trade.enter_date)
            exit_time = pd.to_datetime(trade.exit_date)
            entry_price = trade.enter_price
            
            # 获取持仓期间的切片数据
            # 注意：切片包含 entry_time 到 exit_time
            try:
                mask = (ohlcv_df.index >= entry_time) & (ohlcv_df.index <= exit_time)
                chunk = ohlcv_df[mask]
            except Exception:
                continue

            if chunk.empty:
                continue

            # 计算屏障 (如果没有提供 ATR，使用固定百分比)
            if atr_series is not None and entry_time in atr_series.index:
                atr_val = atr_series.loc[entry_time]
                pt_price = entry_price + (atr_val * self.pt_sl[0])
                sl_price = entry_price - (atr_val * self.pt_sl[1])
            else:
                # Fallback to fixed percentage if no ATR
                # Assume 2% TP, 1% SL if not specified via ATR
                pt_price = entry_price * 1.02
                sl_price = entry_price * 0.99

            # 判定逻辑: 三重屏障 -- 必须判断哪个屏障先被触碰
            high_series = chunk['high']
            low_series = chunk['low']
            close_series = chunk['close']

            barrier_hit = 'time'  # 默认
            label = 0  # 默认亏损/中性

            # 找到第一个触碰止损的索引
            sl_touches = low_series <= sl_price
            sl_idx = sl_touches.idxmax() if sl_touches.any() else None

            # 找到第一个触碰止盈的索引
            pt_touches = high_series >= pt_price
            pt_idx = pt_touches.idxmax() if pt_touches.any() else None

            if sl_idx is not None and pt_idx is not None:
                # 两个屏障都被触碰, 按时间顺序判定
                if sl_idx <= pt_idx:
                    barrier_hit = 'sl'
                    label = 0
                else:
                    barrier_hit = 'pt'
                    label = 1
            elif sl_idx is not None:
                barrier_hit = 'sl'
                label = 0
            elif pt_idx is not None:
                barrier_hit = 'pt'
                label = 1
            else:
                # 垂直屏障 (持仓结束)
                exit_p = close_series.iloc[-1]
                ret = (exit_p - entry_price) / entry_price
                label = 1 if ret > self.min_ret else 0
                barrier_hit = 'time'

            results.append({
                'trade_id': getattr(trade, 'trade_id', 'unknown'),
                'entry_price': entry_price,
                'exit_price': trade.exit_price,
                'pt_price': pt_price,
                'sl_price': sl_price,
                'barrier_hit': barrier_hit,
                'label': label,
                'actual_return': (trade.exit_price - entry_price) / entry_price
            })

        return pd.DataFrame(results)

    def get_quality_score(self, labeled_df: pd.DataFrame) -> float:
        """
        计算信号质量得分 (Label = 1 的比例)
        """
        if labeled_df.empty:
            return 0.0
        return labeled_df['label'].mean()

    def get_barrier_stats(self, labeled_df: pd.DataFrame) -> Dict:
        """
        获取屏障命中率统计
        """
        if labeled_df.empty:
            return {}
        return labeled_df['barrier_hit'].value_counts().to_dict()
