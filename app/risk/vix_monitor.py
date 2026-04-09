"""
市场波动率监控器 (VIX Monitor)

功能:
- 追踪市场波动率指标 (VIX, ATR, 历史波动率)
- 提供波动率状态判断 (低/中/高/极端)
- 动态调整缓存 TTL 和风控参数
"""
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import numpy as np

logger = logging.getLogger("ftseek.risk.vix_monitor")
logger.propagate = False


# 波动率状态定义
# low: VIX < 15, 市场平静
# normal: VIX 15-25, 正常波动
# high: VIX 25-35, 高波动
# extreme: VIX > 35, 极端波动

VIX_THRESHOLDS = {
    "low_max": 15,       # 低波动上限
    "normal_max": 25,    # 正常波动上限
    "high_max": 35,      # 高波动上限
    # > 35 为极端波动
}


@dataclass
class VolatilityState:
    """波动率状态"""
    vix_value: float = 0.0           # VIX 值
    atr_percentile: float = 0.0      # ATR 百分位
    historical_vol: float = 0.0      # 历史波动率 (20 日)
    state: str = "normal"            # low/normal/high/extreme
    updated_at: str = ""

    def get_volatility_factor(self) -> float:
        """
        获取波动率因子 (用于调整缓存 TTL)

        Returns:
            因子值 (0.1-1.0, 越高表示越稳定)
        """
        factors = {
            "low": 1.0,        # 低波动，使用完整 TTL
            "normal": 0.7,     # 正常波动，TTL 打 7 折
            "high": 0.4,       # 高波动，TTL 打 4 折
            "extreme": 0.2,    # 极端波动，TTL 打 2 折
        }
        return factors.get(self.state, 0.5)

    def is_high_volatility(self) -> bool:
        """是否处于高波动状态"""
        return self.state in ("high", "extreme")


class VIXMonitor:
    """
    市场波动率监控器

    追踪 VIX 指数和其他波动率指标，提供市场状态判断
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        初始化监控器

        Args:
            db_path: 数据库路径，默认使用 data/vix_state.db
        """
        if db_path is None:
            db_path = "/opt/ftseek/FTSeek/data/vix_state.db"

        self.db_path = db_path
        self._cache: Dict[str, VolatilityState] = {}

        # 确保目录存在
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._init_db()
        self._load_state()

        logger.info(f"波动率监控器已初始化 | 数据库：{db_path}")

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS vix_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vix_value REAL,
                    atr_percentile REAL,
                    historical_vol REAL,
                    state TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_created_at ON vix_state(created_at)
            ''')

            conn.commit()
        finally:
            conn.close()

    def _load_state(self):
        """从数据库加载最新状态"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT vix_value, atr_percentile, historical_vol, state, created_at
                FROM vix_state
                ORDER BY created_at DESC
                LIMIT 1
            ''')

            row = cursor.fetchone()
            if row:
                self._cache['latest'] = VolatilityState(
                    vix_value=row[0] or 0.0,
                    atr_percentile=row[1] or 0.0,
                    historical_vol=row[2] or 0.0,
                    state=row[3] or "normal",
                    updated_at=row[4] or "",
                )
            else:
                self._cache['latest'] = VolatilityState()
        finally:
            conn.close()

    def update_vix(self, vix_value: float) -> VolatilityState:
        """
        更新 VIX 值

        Args:
            vix_value: VIX 指数值

        Returns:
            VolatilityState
        """
        state = self._determine_state(
            vix_value=vix_value,
            atr_percentile=self._cache.get('latest', VolatilityState()).atr_percentile,
            historical_vol=self._cache.get('latest', VolatilityState()).historical_vol,
        )

        self._save_state(state)
        self._cache['latest'] = state

        logger.info(f"VIX 更新：{vix_value:.2f} -> 状态：{state.state}")
        return state

    def update_atr_percentile(self, code: str, atr_percentile: float):
        """
        更新个股的 ATR 百分位

        Args:
            code: 股票代码
            atr_percentile: ATR 百分位 (0-100)
        """
        if 'latest' not in self._cache:
            self._cache['latest'] = VolatilityState()

        state = self._determine_state(
            vix_value=self._cache['latest'].vix_value,
            atr_percentile=atr_percentile,
            historical_vol=self._cache['latest'].historical_vol,
        )

        self._save_state(state)
        self._cache['latest'] = state

        logger.debug(f"{code} ATR 百分位：{atr_percentile:.1f}% -> 状态：{state.state}")

    def update_historical_vol(self, code: str, prices: list) -> float:
        """
        计算并更新历史波动率

        Args:
            code: 股票代码
            prices: 价格列表 (最近 20 日收盘价)

        Returns:
            历史波动率 (年化)
        """
        if len(prices) < 2:
            return 0.0

        # 计算对数收益率
        prices = np.array(prices)
        returns = np.diff(np.log(prices))

        # 计算 20 日波动率 (年化)
        vol_20d = np.std(returns) * np.sqrt(252) * 100  # 转换为百分比

        if 'latest' not in self._cache:
            self._cache['latest'] = VolatilityState()

        state = self._determine_state(
            vix_value=self._cache['latest'].vix_value,
            atr_percentile=self._cache['latest'].atr_percentile,
            historical_vol=vol_20d,
        )

        self._save_state(state)
        self._cache['latest'] = state

        logger.debug(f"{code} 历史波动率：{vol_20d:.2f}% -> 状态：{state.state}")
        return vol_20d

    def _determine_state(self, vix_value: float = 0,
                         atr_percentile: float = 0,
                         historical_vol: float = 0) -> VolatilityState:
        """
        根据指标确定波动率状态

        Args:
            vix_value: VIX 值
            atr_percentile: ATR 百分位
            historical_vol: 历史波动率

        Returns:
            VolatilityState
        """
        # 综合判断 (VIX 权重 50%, ATR 权重 30%, HV 权重 20%)
        score = 0.0

        if vix_value > 0:
            score += (vix_value / 40.0) * 0.5  # VIX 40 为满分

        if atr_percentile > 0:
            score += (atr_percentile / 100.0) * 0.3

        if historical_vol > 0:
            score += (historical_vol / 50.0) * 0.2  # HV 50% 为满分

        # 确定状态 (调整阈值)
        if score < 0.25:
            state = "low"        # VIX < 20
        elif score < 0.50:
            state = "normal"     # VIX 20-35
        elif score < 0.70:
            state = "high"       # VIX 35-45
        else:
            state = "extreme"    # VIX > 45

        return VolatilityState(
            vix_value=vix_value,
            atr_percentile=atr_percentile,
            historical_vol=historical_vol,
            state=state,
            updated_at=datetime.now().isoformat(),
        )

    def _save_state(self, state: VolatilityState):
        """保存状态到数据库"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO vix_state (vix_value, atr_percentile, historical_vol, state)
                VALUES (?, ?, ?, ?)
            ''', (
                state.vix_value,
                state.atr_percentile,
                state.historical_vol,
                state.state,
            ))
            conn.commit()
        finally:
            conn.close()

    def get_state(self) -> VolatilityState:
        """获取最新波动率状态"""
        if 'latest' not in self._cache:
            self._load_state()
        return self._cache.get('latest', VolatilityState())

    def get_cache_ttl_multiplier(self) -> float:
        """
        获取缓存 TTL 乘数

        Returns:
            乘数 (0.1-1.0)
        """
        state = self.get_state()
        return state.get_volatility_factor()

    def is_high_volatility(self) -> bool:
        """是否处于高波动状态"""
        state = self.get_state()
        return state.is_high_volatility()

    def should_use_fast_track(self) -> bool:
        """
        判断是否应该使用快速通道

        Returns:
            True 如果处于高波动状态
        """
        return self.is_high_volatility()

    def get_history(self, hours: int = 24) -> list:
        """
        获取历史波动率数据

        Args:
            hours: 回溯时间 (小时)

        Returns:
            [(vix, state), ...]
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

            cursor.execute('''
                SELECT vix_value, state, created_at
                FROM vix_state
                WHERE created_at > ?
                ORDER BY created_at DESC
            ''', (cutoff,))

            history = [
                (row[0], row[1], row[2])
                for row in cursor.fetchall()
            ]

            return history
        finally:
            conn.close()

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        state = self.get_state()

        return {
            "vix_value": state.vix_value,
            "atr_percentile": state.atr_percentile,
            "historical_vol": state.historical_vol,
            "state": state.state,
            "is_high_volatility": state.is_high_volatility(),
            "ttl_multiplier": state.get_volatility_factor(),
        }


# 全局单例
_monitor: Optional[VIXMonitor] = None


def get_vix_monitor() -> VIXMonitor:
    """获取波动率监控器单例"""
    global _monitor
    if _monitor is None:
        _monitor = VIXMonitor()
    return _monitor


if __name__ == "__main__":
    # 测试
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    monitor = get_vix_monitor()

    # 模拟 VIX 更新
    if len(sys.argv) > 1:
        vix = float(sys.argv[1])
        state = monitor.update_vix(vix)
        print(f"VIX: {vix:.2f} -> 状态：{state.state}")
    else:
        state = monitor.get_state()
        print(f"当前 VIX: {state.vix_value:.2f}")
        print(f"状态：{state.state}")
        print(f"TTL 乘数：{state.get_volatility_factor():.2f}")

    print(f"\n统计信息：{monitor.get_statistics()}")
