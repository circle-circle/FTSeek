"""
交易阶段抽象 — 定义盘前/盘中/盘后三阶段的接口和生命周期

目标：
1. 明确每个阶段的输入/输出
2. 消除跨阶段直接依赖
3. 通过阶段管理器统一调度
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from app.core.config_loader import cfg
from app.core.trading_env import get_trading_env

logger = logging.getLogger("ftseek.stages")


# ===== 阶段枚举 =====

class TradingStage(Enum):
    PRE_MARKET = "pre_market"     # 盘前 (09:00-09:30 HKT)
    INTRADAY = "intraday"          # 盘中 (09:30-16:00 HKT)
    POST_MARKET = "post_market"    # 盘后 (16:00-次日)
    CLOSED = "closed"              # 休市


# ===== 阶段数据模型 =====

@dataclass
class BattlePlan:
    """盘前作战计划 — 盘前阶段的输出"""
    date: str
    targets: List[Dict[str, Any]]      # 允许交易的标的列表
    restricted: List[str]               # 禁止交易的标的
    market_outlook: str = ""            # 市场展望
    strategy_params: Dict = field(default_factory=dict)
    created_at: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
    
    def is_allowed(self, symbol: str) -> bool:
        """检查标的是否允许交易"""
        if symbol in self.restricted:
            return False
        for t in self.targets:
            if t.get("symbol") == symbol:
                return True
        return False


@dataclass
class TradeSignal:
    """交易信号 — 盘中阶段的输出"""
    code: str
    action: str              # BUY/SELL
    price: float
    quantity: int
    strategy: str
    confidence: float
    reason: str = ""
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class ReviewReport:
    """复盘报告 — 盘后阶段的输出"""
    date: str
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    lessons: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    strategy_performance: Dict = field(default_factory=dict)


# ===== 阶段接口 =====

class IPreMarketStage(ABC):
    """盘前阶段接口"""
    
    @abstractmethod
    def generate_plan(self, date: Optional[str] = None) -> BattlePlan:
        """生成盘前作战计划"""
        ...
    
    @abstractmethod
    def get_watchlist(self) -> List[str]:
        """获取当日监控标的列表"""
        ...


class IIntradayStage(ABC):
    """盘中阶段接口"""
    
    @abstractmethod
    def start(self, battle_plan: BattlePlan):
        """启动盘中监控（接收盘前计划）"""
        ...
    
    @abstractmethod
    def stop(self):
        """停止盘中监控"""
        ...
    
    @abstractmethod
    def process_quote(self, code: str, quote: Dict[str, Any]):
        """处理实时行情"""
        ...
    
    @abstractmethod
    def process_kline(self, code: str, kline: Dict[str, Any], k_type: str):
        """处理 K 线数据"""
        ...
    
    @abstractmethod
    def process_signal(self, signal: TradeSignal):
        """处理交易信号"""
        ...


class IPostMarketStage(ABC):
    """盘后阶段接口"""
    
    @abstractmethod
    def generate_review(self, date: Optional[str] = None) -> ReviewReport:
        """生成复盘报告"""
        ...
    
    @abstractmethod
    def get_suggestions(self) -> List[str]:
        """获取次日建议"""
        ...


# ===== 阶段管理器 =====

class StageManager:
    """
    阶段管理器 — 统一调度三阶段生命周期
    
    职责：
    1. 根据时间自动切换阶段
    2. 管理阶段间的输入/输出传递
    3. 阶段切换时执行清理和初始化
    """
    
    def __init__(self):
        self._current_stage = TradingStage.CLOSED
        self._pre_market: Optional[IPreMarketStage] = None
        self._intraday: Optional[IIntradayStage] = None
        self._post_market: Optional[IPostMarketStage] = None
        self._battle_plan: Optional[BattlePlan] = None
    
    def register_pre_market(self, stage: IPreMarketStage):
        self._pre_market = stage
    
    def register_intraday(self, stage: IIntradayStage):
        self._intraday = stage
    
    def register_post_market(self, stage: IPostMarketStage):
        self._post_market = stage
    
    @property
    def current_stage(self) -> TradingStage:
        return self._current_stage
    
    @property
    def battle_plan(self) -> Optional[BattlePlan]:
        return self._battle_plan
    
    def detect_stage(self) -> TradingStage:
        """根据当前时间检测交易阶段"""
        from datetime import datetime
        import pytz
        
        hkt = datetime.now(pytz.timezone("Asia/Hong_Kong"))
        hour = hkt.hour
        minute = hkt.minute
        weekday = hkt.weekday()  # 0=Monday
        
        # 周末休市
        if weekday >= 5:
            return TradingStage.CLOSED
        
        # 盘前: 09:00-09:30
        if hour == 9 and minute < 30:
            return TradingStage.PRE_MARKET
        
        # 盘中: 09:30-12:00, 13:00-16:00
        if (hour == 9 and minute >= 30) or (hour == 10) or (hour == 11) or \
           (hour == 12 and minute == 0) or \
           (hour >= 13 and hour < 16):
            return TradingStage.INTRADAY
        
        # 盘后: 16:00-次日 09:00
        if hour >= 16 or hour < 9:
            return TradingStage.POST_MARKET
        
        return TradingStage.CLOSED
    
    def transition_to(self, new_stage: TradingStage):
        """阶段切换"""
        if new_stage == self._current_stage:
            return
        
        old_stage = self._current_stage
        logger.info(f"阶段切换: {old_stage.value} → {new_stage.value}")
        
        # 停止旧阶段
        if old_stage == TradingStage.INTRADAY and self._intraday:
            self._intraday.stop()
        
        # 启动新阶段
        if new_stage == TradingStage.PRE_MARKET and self._pre_market:
            self._battle_plan = self._pre_market.generate_plan()
            logger.info(f"盘前计划已生成: {len(self._battle_plan.targets)} 个标的")
        
        elif new_stage == TradingStage.INTRADAY and self._intraday:
            if self._battle_plan:
                self._intraday.start(self._battle_plan)
            else:
                logger.warning("盘中启动但无盘前计划，使用全量 watchlist")
                # Fallback: 生成万能计划
                self._intraday.start(BattlePlan(
                    date=datetime.now().strftime("%Y-%m-%d"),
                    targets=[{"symbol": s, "direction": "ANY", "status": "VALID"}
                             for s in cfg.get("trading.watch_list_hk", [])]
                ))
        
        elif new_stage == TradingStage.POST_MARKET and self._post_market:
            review = self._post_market.generate_review()
            logger.info(f"盘后复盘完成: {review.total_trades} 笔交易, 胜率 {review.win_rate:.1%}")
        
        self._current_stage = new_stage
    
    def check_and_transition(self):
        """检测当前阶段并自动切换"""
        detected = self.detect_stage()
        if detected != self._current_stage:
            self.transition_to(detected)


# ===== 全局实例 =====

stage_manager = StageManager()
