#!/usr/bin/env python3
"""
跨市场套利策略层 (增强版)

利用美港股白天/夜间双向表现,实现开盘快速下单和获取收益。

核心逻辑:
1. 美股夜间表现 → 港股开盘套利 (9:30 AM) - 主导时段 09:30-10:00
2. 港股白天表现 → 美股盘前/盘后套利 (9:30 PM HK)
3. ADR 溢价/折价套利
4. 期货联动套利

增强特性 (2026-04-14):
- Lead-Lag Strength 6维度评分
- Arbitrage Window Score 5维度评分
- 分层评分框架 (arb_score)
- 信号元数据框架 (CrossMarketMetadata)
- 时间窗口管理 (CrossMarketSessionWindow)
- 信号优先级 (SignalPriority.CRITICAL)

配置:
- config/hk_us_mapping.json: 港股 - 美股映射关系
- config.yaml cross_market 节: 策略参数
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum, IntEnum

from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.stock_pool import get_stock_pool_manager, CrossMarketPair
from app.strategy.cross_market_history import get_history_tracker, CrossMarketHistoryTracker

# ============================================================
#  跨市场套利增强模块 (2026-04-11)
# ============================================================
from app.strategy.lead_lag_tracker import get_lead_lag_tracker        # Lead-Lag 追踪器
from app.risk.dynamic_sizer import get_dynamic_positionSizer          # 动态仓位计算器
from app.factors.news_mapper import get_news_mapper                   # 新闻映射器
from app.factors.duration_predictor import get_duration_predictor     # 持续时间预测器
from app.consensus.event_performance_tracker import get_event_performance_tracker  # 事件绩效追踪器

logger = logging.getLogger("ftseek.strategy.cross_market")
logger.propagate = False


def _get_cross_market_config():
    """获取 cross_market 配置节，返回 dict 或 None"""
    try:
        from config.config_center import get_config_center
        cc = get_config_center()
        val = cc.get('cross_market')
        if val is not None:
            if hasattr(val, 'to_dict'):
                return val.to_dict()
            if isinstance(val, dict):
                return val
    except Exception as e:
        logger.warning(f"[cross_market] 读取配置失败，使用默认值: {e}")
    return {}


# ============================================================
#  信号优先级枚举
# ============================================================
class SignalPriority(IntEnum):
    """
    信号优先级 (数值越大优先级越高)
    
    用于:
    - 决策缓冲层排队
    - 同股票多信号冲突解决
    - 时间窗口优先级调整
    """
    LOW = 1           # 低优先级 (如震荡策略)
    MEDIUM = 2        # 中优先级 (如趋势策略)
    HIGH = 3          # 高优先级 (如事件驱动)
    CRITICAL = 4      # 关键优先级 (如跨市场套利 - 时间敏感)
    EMERGENCY = 5     # 紧急优先级 (如风控平仓)


# ============================================================
#  跨市场信号元数据
# ============================================================
@dataclass
class CrossMarketMetadata:
    """
    跨市场信号元数据 - 携带完整评分和联动信息
    
    设计目标:
    1. 不修改 ScenarioType,保持场景系统纯净
    2. 通过 Signal.extra 传递完整元数据
    3. 支持复盘分析和人工审核
    4. 自带风控参数,不继承场景模板
    
    使用示例:
        signal = Signal(
            code="00700.HK",
            action=SignalAction.BUY,
            strategy='cross_market_arbitrage',
            priority=SignalPriority.CRITICAL,
            extra=CrossMarketMetadata(...).to_dict()
        )
    """
    
    # ── 基础信息 ──────────────────────────────────────────────
    arb_type: str  # ArbitrageType.value (us_to_hk / hk_to_us / adr_premium)
    lead_market: str  # 领涨/领跌市场 (US/HK)
    lag_market: str   # 滞后市场 (HK/US)
    lead_code: str    # 领涨/领跌标的 (如 TCEHY)
    lag_code: str     # 滞后标的 (如 00700.HK)
    correlation: float  # 历史相关系数 (0.6-0.92)
    
    # ── 评分框架 ──────────────────────────────────────────────
    lead_lag_strength: float = 0.0  # Lead-Lag 强度 (0-1)
    arb_window_score: float = 0.0   # 套利窗口分 (0-1)
    event_impact: str = "normal"    # 事件强度 (critical/high/normal/low)
    response_speed: float = 0.0     # 历史响应速度 (0-1)
    liquidity_score: float = 0.0    # 流动性评分 (0-1)
    
    # 综合得分
    arb_total_score: float = 0.0    # 最终套利评分 (0-1)
    arb_decision: str = "observe"   # 决策建议 (execute/observe/skip)
    
    # ── 评分明细 (用于复盘) ───────────────────────────────────
    score_breakdown: Dict = field(default_factory=dict)
    # 示例: {
    #   'lag_corr': 0.25, 'event_hit_rate': 0.20, 'momentum': 0.20,
    #   'event_strength': 0.15, 'response_speed': 0.10, 'trend_align': 0.10
    # }
    
    # ── 联动详情 ──────────────────────────────────────────────
    lead_change_pct: float = 0.0    # 领涨标的涨跌幅 (%)
    lag_change_pct: float = 0.0     # 滞后标的预期涨跌幅 (%)
    price_gap_pct: float = 0.0      # 当前价差偏离 (%)
    
    # ── 风控参数 (跨市场专属) ────────────────────────────────
    risk_params: Dict = field(default_factory=lambda: {
        'max_position_pct': 0.05,      # 最大仓位 5%
        'stop_loss_pct': -0.02,        # 止损 2%
        'take_profit_pct': 0.03,       # 止盈 3%
        'max_holding_minutes': 30,     # 最长持有 30 分钟
        'max_slippage_pct': 0.005,     # 滑点上限 0.5%
        'force_exit_time': '10:00',    # 强制退出时间
    })
    
    # ── 会话元数据 ────────────────────────────────────────────
    session_id: str = ""            # 会话 ID (日期_批次)
    signal_timestamp: float = 0.0   # 信号生成时间戳
    push_time: str = "09:30"        # 计划推送时间
    execution_deadline: str = "10:00"  # 执行截止时间
    
    # ── 审核追踪 (半自动流程) ────────────────────────────────
    review_status: str = "pending"  # pending/approved/rejected/auto_executed
    reviewer_id: Optional[str] = None
    review_comment: str = ""
    
    # ── 方向信息 ─────────────────────────────────────────────
    side: str = 'long'  # 操作方向: long/short
    
    def to_dict(self) -> Dict:
        """转换为字典 (用于 Signal.extra)"""
        return {
            'arb_type': self.arb_type,
            'lead_market': self.lead_market,
            'lag_market': self.lag_market,
            'lead_code': self.lead_code,
            'lag_code': self.lag_code,
            'correlation': self.correlation,
            'lead_lag_strength': self.lead_lag_strength,
            'arb_window_score': self.arb_window_score,
            'event_impact': self.event_impact,
            'response_speed': self.response_speed,
            'liquidity_score': self.liquidity_score,
            'arb_total_score': self.arb_total_score,
            'arb_decision': self.arb_decision,
            'score_breakdown': self.score_breakdown,
            'lead_change_pct': self.lead_change_pct,
            'lag_change_pct': self.lag_change_pct,
            'price_gap_pct': self.price_gap_pct,
            'risk_params': self.risk_params,
            'session_id': self.session_id,
            'signal_timestamp': self.signal_timestamp,
            'push_time': self.push_time,
            'execution_deadline': self.execution_deadline,
            'review_status': self.review_status,
            'reviewer_id': self.reviewer_id,
            'review_comment': self.review_comment,
            'side': self.side,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'CrossMarketMetadata':
        """从字典重建 (用于复盘)"""
        return cls(
            arb_type=data.get('arb_type', ''),
            lead_market=data.get('lead_market', ''),
            lag_market=data.get('lag_market', ''),
            lead_code=data.get('lead_code', ''),
            lag_code=data.get('lag_code', ''),
            correlation=data.get('correlation', 0.0),
            lead_lag_strength=data.get('lead_lag_strength', 0.0),
            arb_window_score=data.get('arb_window_score', 0.0),
            event_impact=data.get('event_impact', 'normal'),
            response_speed=data.get('response_speed', 0.0),
            liquidity_score=data.get('liquidity_score', 0.0),
            arb_total_score=data.get('arb_total_score', 0.0),
            arb_decision=data.get('arb_decision', 'observe'),
            score_breakdown=data.get('score_breakdown', {}),
            lead_change_pct=data.get('lead_change_pct', 0.0),
            lag_change_pct=data.get('lag_change_pct', 0.0),
            price_gap_pct=data.get('price_gap_pct', 0.0),
            risk_params=data.get('risk_params', {
                'max_position_pct': 0.05,
                'stop_loss_pct': -0.02,
                'take_profit_pct': 0.03,
                'max_holding_minutes': 30,
                'max_slippage_pct': 0.005,
                'force_exit_time': '10:00',
            }),
            session_id=data.get('session_id', ''),
            signal_timestamp=data.get('signal_timestamp', 0.0),
            push_time=data.get('push_time', '09:30'),
            execution_deadline=data.get('execution_deadline', '10:00'),
            review_status=data.get('review_status', 'pending'),
            reviewer_id=data.get('reviewer_id'),
            review_comment=data.get('review_comment', ''),
            side=data.get('side', 'long'),
        )
    
    def to_markdown(self) -> str:
        """转换为 Markdown (用于钉钉推送)"""
        return (
            f"### 📊 跨市场套利信号\n\n"
            f"---\n\n"
            f"**联动方向**: {self.lead_market}({self.lead_code}) → {self.lag_market}({self.lag_code})\n\n"
            f"**历史相关性**: {self.correlation:.2f}\n\n"
            f"---\n\n"
            f"### 📈 评分详情\n\n"
            f"- **Lead-Lag 强度**: {self.lead_lag_strength:.2f}\n\n"
            f"- **套利窗口分**: {self.arb_window_score:.2f}\n\n"
            f"- **综合得分**: **{self.arb_total_score:.2f}**\n\n"
            f"- **决策建议**: {self.arb_decision}\n\n"
            f"- **事件强度**: {self.event_impact}\n\n"
            f"---\n\n"
            f"### 💡 风控参数\n\n"
            f"- **仓位**: {self.risk_params['max_position_pct']:.0%}\n\n"
            f"- **止损**: {self.risk_params['stop_loss_pct']:.0%}\n\n"
            f"- **止盈**: {self.risk_params['take_profit_pct']:.0%}\n\n"
            f"- **最长持有**: {self.risk_params['max_holding_minutes']}分钟\n\n"
            f"---\n\n"
        )


# ============================================================
#  时间窗口管理
# ============================================================
class CrossMarketSessionWindow:
    """
    跨市场套利时间窗口管理
    
    功能:
    1. 判断当前是否在套利窗口 (09:30-10:00)
    2. 获取当前时段相位 (盘前/套利/趋势)
    3. 判断是否港股交易日
    """
    
    # 套利窗口配置 (默认值，可被 config.yaml cross_market.session 覆盖)
    MORNING_ARB_START = "09:30"
    MORNING_ARB_END = "10:00"
    PRE_MARKET_START = "09:00"

    @classmethod
    def _load_session_config(cls):
        """从 config.yaml cross_market.session 加载时间窗口配置"""
        cfg = _get_cross_market_config()
        session = cfg.get('session', {}) if cfg else {}
        if not isinstance(session, dict):
            try:
                session = session.to_dict()
            except Exception:
                session = {}
        if session:
            cls.MORNING_ARB_START = session.get('morning_arb_start', cls.MORNING_ARB_START)
            cls.MORNING_ARB_END = session.get('morning_arb_end', cls.MORNING_ARB_END)
            cls.PRE_MARKET_START = session.get('pre_market_start', cls.PRE_MARKET_START)
            logger.info(
                f"[CrossMarketSessionWindow] 时间窗口已从配置加载: "
                f"PRE={cls.PRE_MARKET_START}, ARB={cls.MORNING_ARB_START}-{cls.MORNING_ARB_END}"
            )
    
    @classmethod
    def is_arb_session(cls, current_time: Optional[datetime] = None) -> bool:
        """
        是否在套利窗口 (09:30-10:00)
        
        Args:
            current_time: 当前时间 (默认 now())
        
        Returns:
            是否在套利窗口
        """
        if current_time is None:
            current_time = datetime.now()
        
        if not cls.is_trading_day(current_time):
            return False
        
        t = current_time.time()
        start = datetime.strptime(cls.MORNING_ARB_START, "%H:%M").time()
        end = datetime.strptime(cls.MORNING_ARB_END, "%H:%M").time()
        
        return start <= t <= end
    
    @classmethod
    def is_pre_market(cls, current_time: Optional[datetime] = None) -> bool:
        """是否在盘前准备时段 (09:00-09:30)"""
        if current_time is None:
            current_time = datetime.now()
        
        t = current_time.time()
        start = datetime.strptime(cls.PRE_MARKET_START, "%H:%M").time()
        arb_start = datetime.strptime(cls.MORNING_ARB_START, "%H:%M").time()
        
        return start <= t < arb_start
    
    @classmethod
    def get_session_phase(cls, current_time: Optional[datetime] = None) -> str:
        """
        获取当前时段相位
        
        Returns:
            'pre_market' (09:00-09:30)
            'arb_window' (09:30-10:00)
            'trend_session' (10:00-16:00)
            'after_hours' (其他)
        """
        if current_time is None:
            current_time = datetime.now()
        
        if not cls.is_trading_day(current_time):
            return 'after_hours'
        
        t = current_time.time()
        pre_start = datetime.strptime(cls.PRE_MARKET_START, "%H:%M").time()
        arb_start = datetime.strptime(cls.MORNING_ARB_START, "%H:%M").time()
        arb_end = datetime.strptime(cls.MORNING_ARB_END, "%H:%M").time()
        market_close = datetime.strptime("16:00", "%H:%M").time()
        
        if pre_start <= t < arb_start:
            return 'pre_market'
        elif arb_start <= t <= arb_end:
            return 'arb_window'
        elif arb_end < t <= market_close:
            return 'trend_session'
        else:
            return 'after_hours'
    
    @classmethod
    def is_trading_day(cls, current_time: Optional[datetime] = None) -> bool:
        """
        判断是否港股交易日 (简化版，暂不考虑节假日)

        注：完整实现需要接入港股交易日历 API
        """
        if current_time is None:
            current_time = datetime.now()
        
        # 周一=0, 周日=6
        return current_time.weekday() < 5  # 周一至周五
    
    @classmethod
    def get_arb_window_minutes(cls) -> int:
        """获取套利窗口时长 (分钟)"""
        return 30  # 09:30-10:00
    
    @classmethod
    def get_seconds_to_arb_end(cls, current_time: Optional[datetime] = None) -> int:
        """获取距离套利窗口结束的秒数"""
        if current_time is None:
            current_time = datetime.now()
        
        if not cls.is_arb_session(current_time):
            return 0
        
        end_t = datetime.combine(
            current_time.date(),
            datetime.strptime(cls.MORNING_ARB_END, "%H:%M").time()
        )
        
        delta = end_t - current_time
        return max(0, int(delta.total_seconds()))


class ArbitrageType(str, Enum):
    """套利类型"""
    US_TO_HK = "us_to_hk"     # 美股→港股
    HK_TO_US = "hk_to_us"     # 港股→美股
    ADR_PREMIUM = "adr_premium"  # ADR 溢价
    ADR_DISCOUNT = "adr_discount"  # ADR 折价
    FUTURES_LINK = "futures_link"  # 期货联动


@dataclass
class OvernightChange:
    """隔夜涨跌数据"""
    code: str
    name: str
    close_price: float
    change_pct: float  # 涨跌幅 (%)
    volume: float
    turnover: float
    market: str  # US/HK
    timestamp: datetime


@dataclass
class ArbitrageSignal:
    """套利信号"""
    type: ArbitrageType
    hk_code: str
    us_code: str
    direction: str  # long/short
    reason: str
    confidence: float  # 0-100%
    expected_return: float  # 预期收益 (%)
    entry_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None


class CrossMarketArbitrage(BaseStrategy):
    """
    跨市场套利策略

    利用美港股时间差和价格联动进行套利
    """

    name = "cross_market_arbitrage"

    # 配置参数
    US_TO_HK_THRESHOLD = 0.03   # 美股涨>3% 触发港股套利
    HK_TO_US_THRESHOLD = 0.03   # 港股涨>3% 触发美股套利
    ADR_PREMIUM_THRESHOLD = 0.05  # ADR 溢价>5% 触发套利
    MIN_CONFIDENCE = 70          # 最小置信度

    def __init__(self, config: dict = None):
        super().__init__(config or {})

        self.stock_pool = get_stock_pool_manager()

        # 隔夜数据缓存
        self._us_overnight: Dict[str, OvernightChange] = {}
        self._hk_daytime: Dict[str, OvernightChange] = {}

        # 信号冷却
        self._signal_cooldown: Dict[str, datetime] = {}

        # ============================================================
        #  跨市场套利增强模块初始化 (2026-04-11)
        # ============================================================
        self.lead_lag = get_lead_lag_tracker()                    # Lead-Lag 追踪器
        self.sizer = get_dynamic_positionSizer()                  # 动态仓位计算器
        self.news_mapper = get_news_mapper(use_ai=False)          # 新闻映射器 (默认规则模式)
        self.duration_predictor = get_duration_predictor()        # 持续时间预测器
        self.perf_tracker = get_event_performance_tracker()       # 事件绩效追踪器

        # 从 config.yaml cross_market.scoring 加载评分权重，保留默认值作 fallback
        _cm_cfg = _get_cross_market_config()
        _scoring_cfg = _cm_cfg.get('scoring', {}) if _cm_cfg else {}
        if not isinstance(_scoring_cfg, dict):
            try:
                _scoring_cfg = _scoring_cfg.to_dict()
            except Exception:
                _scoring_cfg = {}
            
        def _get_weights(section_key, defaults):
            section = _scoring_cfg.get(section_key, {})
            if not isinstance(section, dict):
                try:
                    section = section.to_dict()
                except Exception:
                    section = {}
            result = dict(defaults)
            result.update({k: float(v) for k, v in section.items() if k in defaults})
            return result
            
        self._scoring_weights = {
            'lead_lag': _get_weights('lead_lag', {
                'lag_correlation': 0.25,
                'event_hit_rate': 0.20,
                'momentum': 0.20,
                'event_strength': 0.15,
                'response_speed': 0.10,
                'trend_alignment': 0.10,
            }),
            'window_score': _get_weights('window_score', {
                'time_decay': 0.30,
                'price_deviation': 0.20,
                'trading_session': 0.20,
                'liquidity': 0.15,
                'news_digestion': 0.15,
            }),
            'final_score': _get_weights('final_score', {
                'lead_lag_strength': 0.25,
                'event_impact': 0.20,
                'price_gap': 0.20,
                'response_speed': 0.15,
                'liquidity': 0.10,
                'confidence': 0.10,
            }),
        }
            
        # 决策阀值 (from config.yaml cross_market.scoring.thresholds)
        _thresholds_cfg = _scoring_cfg.get('thresholds', {})
        if not isinstance(_thresholds_cfg, dict):
            try:
                _thresholds_cfg = _thresholds_cfg.to_dict()
            except Exception:
                _thresholds_cfg = {}
        self._decision_thresholds = {
            'execute': float(_thresholds_cfg.get('execute', 0.75)),
            'observe': float(_thresholds_cfg.get('observe', 0.55)),
        }
            
        logger.info(
            f"[CrossMarketArbitrage] 评分权重已加载 (来源: "
            f"{'config.yaml' if _scoring_cfg else 'default'}): "
            f"execute={self._decision_thresholds['execute']}, "
            f"observe={self._decision_thresholds['observe']}"
        )

        # 历史事件统计追踪器（用于事件命中率 & 响应速度评分）
        try:
            self.history_tracker: Optional[CrossMarketHistoryTracker] = get_history_tracker()
            logger.info("[CrossMarketArbitrage] 历史统计追踪器初始化成功")
        except Exception as _e:
            self.history_tracker = None
            logger.warning(f"[CrossMarketArbitrage] 历史统计追踪器初始化失败，将使用相关性推断: {_e}")

        # 新闻消化度计算器（Task #7: 替代 news_digestion 硬编码）
        try:
            from app.strategy.cross_market_news_digest import get_news_digest
            self.news_digest = get_news_digest()
            logger.info("[CrossMarketArbitrage] 新闻消化度计算器初始化成功")
        except Exception as _e:
            self.news_digest = None
            logger.warning(f"[CrossMarketArbitrage] 新闻消化度计算器初始化失败，将使用时间衰减 fallback: {_e}")

        # 从 config.yaml cross_market.risk_params 加载风控参数，保留默认值作 fallback
        _risk_cfg = _cm_cfg.get('risk_params', {}) if _cm_cfg else {}
        if not isinstance(_risk_cfg, dict):
            try:
                _risk_cfg = _risk_cfg.to_dict()
            except Exception:
                _risk_cfg = {}
        self._risk_params = {
            'max_position_pct': float(_risk_cfg.get('max_position_pct', 0.05)),
            'stop_loss_pct': float(_risk_cfg.get('stop_loss_pct', -0.02)),
            'take_profit_pct': float(_risk_cfg.get('take_profit_pct', 0.03)),
            'max_holding_minutes': int(_risk_cfg.get('max_holding_minutes', 30)),
            'max_slippage_pct': float(_risk_cfg.get('max_slippage_pct', 0.005)),
            'force_exit_time': str(_risk_cfg.get('force_exit_time', '10:00')),
        }
        logger.info(
            f"[CrossMarketArbitrage] 风控参数已加载 (来源: "
            f"{'config.yaml' if _risk_cfg else 'default'}): "
            f"stop_loss={self._risk_params['stop_loss_pct']:.1%}, "
            f"take_profit={self._risk_params['take_profit_pct']:.1%}, "
            f"max_holding={self._risk_params['max_holding_minutes']}min"
        )
        logger.info(f"  美股→港股阈值：{self.US_TO_HK_THRESHOLD:.1%}")
        logger.info(f"  港股→美股阈值：{self.HK_TO_US_THRESHOLD:.1%}")
        logger.info(f"  ADR 溢价阈值：{self.ADR_PREMIUM_THRESHOLD:.1%}")
        logger.info("跨市场套利策略已初始化 (增强版)")

    def analyze_us_to_hk(self, us_data: List[OvernightChange]) -> List[Signal]:
        """
        分析美股夜间表现，生成港股开盘套利信号

        逻辑:
        - 美股 ADR 隔夜大涨→港股开盘追涨
        - 美股 ADR 隔夜大跌→港股开盘做空/回避

        Args:
            us_data: 美股隔夜数据列表

        Returns:
            港股套利信号列表
        """
        # 缓存数据
        for item in us_data:
            self._us_overnight[item.code] = item

        # ============================================================
        #  Lead-Lag 价格记录 (2026-04-11)
        # ============================================================
        # 记录美股价格变动到 Lead-Lag 追踪器
        current_time = datetime.now().timestamp()
        for us_change in us_data:
            self.lead_lag.record_price_move(
                code=us_change.code,
                timestamp=current_time,
                price_change_pct=us_change.change_pct * 100,  # 转换为百分比
                volume=us_change.volume if hasattr(us_change, 'volume') else 0
            )

        signals = []

        for us_change in us_data:
            # 获取对应港股
            pair = self.stock_pool.get_us_hk_pair(us_change.code)
            if not pair or not pair.enabled:
                continue

            hk_code = pair.hk_code

            # 检查冷却
            if self._is_in_cooldown(hk_code):
                logger.debug(f"{hk_code} 在冷却期，跳过")
                continue

            # 生成信号
            if us_change.change_pct > self.US_TO_HK_THRESHOLD:
                # 大涨→做多
                signal = self._create_arb_signal(
                    pair=pair,
                    direction='long',
                    us_change=us_change,
                    entry_type='market_open'
                )
                if signal:
                    signals.append(signal)

            elif us_change.change_pct < -self.US_TO_HK_THRESHOLD:
                # 大跌→做空
                signal = self._create_arb_signal(
                    pair=pair,
                    direction='short',
                    us_change=us_change,
                    entry_type='market_open'
                )
                if signal:
                    signals.append(signal)

        logger.info(f"分析美股→港股：生成 {len(signals)} 个信号")
        return signals

    def analyze_hk_to_us(self, hk_data: List[OvernightChange]) -> List[Signal]:
        """
        分析港股白天表现，生成美股盘前/盘后套利信号

        逻辑:
        - 港股大涨→美股盘前做多对应 ADR/期货
        - 港股大跌→美股盘前做空对应 ADR/期货

        Args:
            hk_data: 港股白天数据列表

        Returns:
            美股套利信号列表
        """
        # 缓存数据
        for item in hk_data:
            self._hk_daytime[item.code] = item

        signals = []

        for hk_change in hk_data:
            # 获取对应美股
            pair = self.stock_pool.get_hk_us_pair(hk_change.code)
            if not pair or not pair.enabled:
                continue

            us_code = pair.us_code

            # 检查冷却
            if self._is_in_cooldown(us_code):
                logger.debug(f"{us_code} 在冷却期，跳过")
                continue

            # 生成信号
            if hk_change.change_pct > self.HK_TO_US_THRESHOLD:
                # 大涨→做多
                signal = self._create_us_arb_signal(
                    pair=pair,
                    direction='long',
                    hk_change=hk_change.change_pct,
                    entry_type='pre_market'
                )
                if signal:
                    signals.append(signal)

            elif hk_change.change_pct < -self.HK_TO_US_THRESHOLD:
                # 大跌→做空
                signal = self._create_us_arb_signal(
                    pair=pair,
                    direction='short',
                    hk_change=hk_change.change_pct,
                    entry_type='pre_market'
                )
                if signal:
                    signals.append(signal)

        logger.info(f"分析港股→美股：生成 {len(signals)} 个信号")
        return signals

    def analyze_adr_premium(self) -> List[Signal]:
        """
        分析 ADR 溢价/折价套利机会

        逻辑:
        - ADR 相对港股溢价>5%→做空 ADR/做多港股
        - ADR 相对港股折价>5%→做多 ADR/做空港股

        Returns:
            ADR 套利信号列表
        """
        signals = []
        pairs = self.stock_pool.get_enabled_correlation_pairs()

        for pair in pairs:
            # 获取价格数据 (实际应从 API 获取)
            hk_price = self._get_hk_price(pair.hk_code)
            us_price = self._get_us_price(pair.us_code)

            if not hk_price or not us_price:
                continue

            # 计算等价价格 (考虑汇率)
            usd_hkd_rate = 7.8  # 简化处理
            hk_equiv_usd = hk_price / usd_hkd_rate

            # 除零保护：当 hk_equiv_usd 为 0 或很小时跳过
            if hk_equiv_usd <= 0.001:
                logger.debug(f"跳过 ADR 溢价计算：{pair.hk_code} 等价价格={hk_equiv_usd:.4f}")
                continue

            # 计算溢价率
            premium = (us_price - hk_equiv_usd) / hk_equiv_usd

            # 检查冷却
            if self._is_in_cooldown(pair.hk_code):
                continue

            if premium > self.ADR_PREMIUM_THRESHOLD:
                # ADR 溢价→做空 ADR/做多港股
                signal = Signal(
                    code=pair.hk_code,
                    action=SignalAction.BUY,
                    price=hk_price,
                    quantity=100,
                    reason=f"ADR 溢价套利：溢价{premium:.1%} > {self.ADR_PREMIUM_THRESHOLD:.1%}",
                    strategy=self.name,
                    extra={
                        'arb_type': 'adr_premium',
                        'us_code': pair.us_code,
                        'premium': premium,
                    }
                )
                signals.append(signal)

            elif premium < -self.ADR_PREMIUM_THRESHOLD:
                # ADR 折价→做多 ADR/做空港股
                signal = Signal(
                    code=pair.hk_code,
                    action=SignalAction.SELL,
                    price=hk_price,
                    quantity=100,
                    reason=f"ADR 折价套利：折价{abs(premium):.1%} > {self.ADR_PREMIUM_THRESHOLD:.1%}",
                    strategy=self.name,
                    extra={
                        'arb_type': 'adr_discount',
                        'us_code': pair.us_code,
                        'premium': premium,
                    }
                )
                signals.append(signal)

        logger.info(f"分析 ADR 溢价：生成 {len(signals)} 个信号")
        return signals

    def _create_arb_signal(
        self,
        pair: CrossMarketPair,
        direction: str,
        us_change: OvernightChange,
        entry_type: str,
        event_impact: str = "normal",
    ) -> Optional[Signal]:
        """
        创建港股套利信号 (增强版 - 评分驱动)
        
        Args:
            pair: 美港股映射对
            direction: long/short
            us_change: 美股隔夜涨跌数据
            entry_type: market_open/pre_market
            event_impact: 事件强度
        
        Returns:
            Signal (带 CrossMarketMetadata) 或 None
        """
        # ── 1. 计算 Lead-Lag Strength ─────────────────────────
        lead_lag_strength, lead_lag_breakdown = self.calculate_lead_lag_strength(
            pair=pair,
            us_change=us_change,
            event_impact=event_impact,
        )
        
        # ── 2. 计算 Arbitrage Window Score ────────────────────
        window_score, window_breakdown = self.calculate_arb_window_score(
            pair=pair,
            us_change=us_change,
        )
        
        # ── 3. 计算最终套利评分 ──────────────────────────────
        # 基础置信度 (旧版兼容)
        base_confidence = min(1.0, (50 + abs(us_change.change_pct) * 10) / 100.0)
        
        # 响应速度和流动性 (从 Lead-Lag 中提取)
        response_speed = lead_lag_breakdown.get('response_speed', 0.0) / 0.10  # 归一化
        liquidity_score = pair.correlation / 0.80  # 0.8 相关系数为满分
        
        arb_total_score, score_breakdown, arb_decision = self.calculate_arb_total_score(
            lead_lag_strength=lead_lag_strength,
            window_score=window_score,
            price_gap_pct=us_change.change_pct,
            response_speed=response_speed,
            liquidity_score=min(1.0, liquidity_score),
            confidence=base_confidence,
            event_impact=event_impact,
        )
        
        # ── 4. 决策判断 ─────────────────────────────────────
        if arb_decision == 'skip':
            logger.debug(
                f"[跨市场评分] {pair.hk_code} 评分过低 ({arb_total_score:.2f}), 跳过"
            )
            return None
        
        # ── 5. 生成元数据 ───────────────────────────────────
        session_id = datetime.now().strftime("%Y%m%d") + "_morning"
        
        metadata = CrossMarketMetadata(
            arb_type='us_to_hk',
            lead_market='US',
            lag_market='HK',
            lead_code=pair.us_code,
            lag_code=pair.hk_code,
            correlation=pair.correlation,
            lead_lag_strength=lead_lag_strength,
            arb_window_score=window_score,
            event_impact=event_impact,
            response_speed=response_speed,
            liquidity_score=min(1.0, liquidity_score),
            arb_total_score=arb_total_score,
            arb_decision=arb_decision,
            score_breakdown={
                'lead_lag': lead_lag_breakdown,
                'window': window_breakdown,
                'final': score_breakdown,
            },
            lead_change_pct=us_change.change_pct,
            lag_change_pct=us_change.change_pct * 0.7,  # 保守估计
            price_gap_pct=us_change.change_pct,
            risk_params=dict(self._risk_params),  # 从实例属性读取配置化风控参数
            session_id=session_id,
            signal_timestamp=time.time(),
            push_time='09:30',
            execution_deadline='10:00',
            review_status='pending' if arb_decision == 'execute' else 'observe',
            side=direction,  # 设置操作方向
        )
        
        # ── 6. 生成 Signal ──────────────────────────────────
        action = SignalAction.BUY if direction == 'long' else SignalAction.SELL
        
        signal = Signal(
            code=pair.hk_code,
            action=action,
            price=0.0,  # 开盘市价单
            quantity=0,  # 由 executor 动态计算
            reason=(
                f"跨市场套利: {pair.us_name}隔夜{us_change.change_pct:+.1%} "
                f"→ {pair.hk_name} | 评分={arb_total_score:.2f} ({arb_decision})"
            ),
            strategy=self.name,
            priority=SignalPriority.CRITICAL,  # 关键优先级
            extra=metadata.to_dict(),
        )

        # 设置冷却
        self._set_cooldown(pair.hk_code)

        # ============================================================
        #  事件绩效追踪记录 (2026-04-11)
        # ============================================================
        event_id = f"cross_market_{session_id}_{pair.hk_code}"
        self.perf_tracker.record_event(
            event_id=event_id,
            event_type='cross_market_arbitrage',
            predicted_direction=direction,
            target_stocks=[{'code': pair.hk_code, 'direction': direction, 'weight': 1.0}],
            arb_score=arb_total_score,
            news_sentiment=0.0,
        )

        logger.info(
            f"[跨市场信号] {pair.hk_code} {direction.upper()} | "
            f"评分={arb_total_score:.2f} ({arb_decision}) | "
            f"Lead-Lag={lead_lag_strength:.2f} | Window={window_score:.2f}"
        )

        return signal

    def _create_us_arb_signal(self, pair: CrossMarketPair, direction: str,
                             hk_change: float, entry_type: str) -> Optional[Signal]:
        """创建美股套利信号"""
        # 计算置信度
        confidence = min(100, 50 + abs(hk_change) * 10)

        if confidence < self.MIN_CONFIDENCE:
            return None

        # 确定操作方向
        action = SignalAction.BUY if direction == 'long' else SignalAction.SELL

        # 预期收益
        expected_return = hk_change * 0.7

        signal = Signal(
            code=pair.us_code,
            action=action,
            price=0.0,  # 盘前市价单
            quantity=100,
            reason=f"跨市场套利：{pair.hk_name} 白天{direction} {hk_change:.1%} → {pair.us_name}",
            strategy=self.name,
            extra={
                'arb_type': 'hk_to_us',
                'hk_code': pair.hk_code,
                'hk_change': hk_change,
                'direction': direction,
                'entry_type': entry_type,
                'confidence': confidence,
                'expected_return': expected_return,
            }
        )

        # 设置冷却
        self._set_cooldown(pair.us_code)

        return signal

    def _get_hk_price(self, hk_code: str) -> Optional[float]:
        """获取港股价格 (实际应从 API 获取)"""
        # 简化实现
        if hk_code in self._hk_daytime:
            return self._hk_daytime[hk_code].close_price
        return None

    def _get_us_price(self, us_code: str) -> Optional[float]:
        """获取美股价格 (实际应从 API 获取)"""
        # 简化实现
        if us_code in self._us_overnight:
            return self._us_overnight[us_code].close_price
        return None

    def _is_in_cooldown(self, code: str) -> bool:
        """检查是否在冷却期"""
        if code not in self._signal_cooldown:
            return False

        cooldown_end = self._signal_cooldown[code]
        return datetime.now() < cooldown_end

    def _set_cooldown(self, code: str, hours: int = 12):
        """设置冷却时间"""
        self._signal_cooldown[code] = datetime.now() + timedelta(hours=hours)

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """K 线回调 (不支持)"""
        return None

    def set_us_overnight_data(self, us_data: List[OvernightChange]):
        """设置美股隔夜数据"""
        self._us_overnight = {item.code: item for item in us_data}
        logger.info(f"更新美股隔夜数据：{len(self._us_overnight)} 只")

    def set_hk_daytime_data(self, hk_data: List[OvernightChange]):
        """设置港股白天数据"""
        self._hk_daytime = {item.code: item for item in hk_data}
        logger.info(f"更新港股白天数据：{len(self._hk_daytime)} 只")

    def get_arbitrage_summary(self) -> Dict:
        """获取套利摘要"""
        return {
            'us_overnight_count': len(self._us_overnight),
            'hk_daytime_count': len(self._hk_daytime),
            'enabled_pairs': len(self.stock_pool.get_enabled_correlation_pairs()),
            'active_cooldowns': len([k for k, v in self._signal_cooldown.items()
                                    if datetime.now() < v]),
        }
    
    # ============================================================
    #  评分框架 (Task 2-4)
    # ============================================================
    
    def calculate_lead_lag_strength(
        self,
        pair: CrossMarketPair,
        us_change: OvernightChange,
        event_impact: str = "normal",
    ) -> Tuple[float, Dict]:
        """
        计算 Lead-Lag Strength (6维度)
        
        Args:
            pair: 美港股映射对
            us_change: 美股隔夜涨跌
            event_impact: 事件强度 (critical/high/normal/low)
        
        Returns:
            (lead_lag_strength, score_breakdown)
        """
        weights = self._scoring_weights['lead_lag']
        breakdown = {}
        
        # 1. 滞后相关系数 (0.25)
        lag_corr = pair.correlation  # 0.6-0.92
        breakdown['lag_correlation'] = round(lag_corr * weights['lag_correlation'], 4)
        
        # 2. 事件命中率 (0.20) - 基于历史统计
        try:
            if self.history_tracker is not None:
                event_hit_rate = self.history_tracker.get_event_hit_rate(
                    pair.hk_code, pair.us_code, event_impact, fallback_corr=lag_corr
                )
            else:
                raise ValueError("history_tracker 不可用")
        except Exception as _e:
            logger.debug(f"[calculate_lead_lag_strength] 事件命中率 fallback: {_e}")
            event_hit_rate = min(1.0, lag_corr * 1.1)  # fallback: 相关性推断
        breakdown['event_hit_rate'] = round(event_hit_rate * weights['event_hit_rate'], 4)
        
        # 3. 当前动量 (0.20) - 美股涨跌幅绝对值
        # 涨跌幅越大,动量越强 (但需归一化)
        momentum = min(1.0, abs(us_change.change_pct) / 0.10)  # 10% 为满分
        breakdown['momentum'] = round(momentum * weights['momentum'], 4)
        
        # 4. 事件强度 (0.15)
        event_strength_map = {
            'critical': 1.0,
            'high': 0.75,
            'normal': 0.50,
            'low': 0.25,
        }
        event_strength = event_strength_map.get(event_impact, 0.50)
        breakdown['event_strength'] = round(event_strength * weights['event_strength'], 4)
        
        # 5. 响应速度 (0.10) - 历史平均响应时间
        try:
            if self.history_tracker is not None:
                response_speed = self.history_tracker.get_response_speed(
                    pair.hk_code, pair.us_code, fallback_corr=lag_corr
                )
            else:
                raise ValueError("history_tracker 不可用")
        except Exception as _e:
            logger.debug(f"[calculate_lead_lag_strength] 响应速度 fallback: {_e}")
            response_speed = min(1.0, lag_corr * 1.2)  # fallback: 相关性推断
        breakdown['response_speed'] = round(response_speed * weights['response_speed'], 4)
        
        # 6. 趋势一致性 (0.10) - 美股趋势是否明确
        # 涨跌幅 > 5% 趋势明确
        trend_align = min(1.0, abs(us_change.change_pct) / 0.05)
        breakdown['trend_alignment'] = round(trend_align * weights['trend_alignment'], 4)
        
        # 总分
        lead_lag_strength = sum(breakdown.values())
        
        return round(lead_lag_strength, 4), breakdown
    
    def calculate_arb_window_score(
        self,
        pair: CrossMarketPair,
        us_change: OvernightChange,
        current_time: Optional[datetime] = None,
    ) -> Tuple[float, Dict]:
        """
        计算 Arbitrage Window Score (5维度)
        
        Args:
            pair: 美港股映射对
            us_change: 美股隔夜涨跌
            current_time: 当前时间
        
        Returns:
            (window_score, score_breakdown)
        """
        weights = self._scoring_weights['window_score']
        if current_time is None:
            current_time = datetime.now()
        
        breakdown = {}
        
        # 1. 时间衰减 (0.30) - 开盘后衰减
        # 09:30 = 1.0, 10:00 = 0.5
        session_phase = CrossMarketSessionWindow.get_session_phase(current_time)
        if session_phase == 'arb_window':
            seconds_to_end = CrossMarketSessionWindow.get_seconds_to_arb_end(current_time)
            total_seconds = 30 * 60  # 30 分钟
            time_decay = 0.5 + 0.5 * (seconds_to_end / total_seconds)  # 0.5-1.0
        elif session_phase == 'pre_market':
            time_decay = 1.0  # 盘前满分
        else:
            time_decay = 0.3  # 其他时段低分
        breakdown['time_decay'] = round(time_decay * weights['time_decay'], 4)
        
        # 2. 价格偏离 (0.20) - 当前价差偏离度
        # 假设港股尚未开盘,价格偏离 = 美股涨跌幅
        price_deviation = min(1.0, abs(us_change.change_pct) / 0.08)  # 8% 为满分
        breakdown['price_deviation'] = round(price_deviation * weights['price_deviation'], 4)
        
        # 3. 交易时段 (0.20) - 是否在港股交易时段
        if session_phase in ['arb_window', 'pre_market']:
            trading_session = 1.0
        else:
            trading_session = 0.3
        breakdown['trading_session'] = round(trading_session * weights['trading_session'], 4)
        
        # 4. 流动性 (0.15) - 基于相关性推断
        # 相关性越高,流动性越好
        liquidity = min(1.0, pair.correlation / 0.80)  # 0.8 相关系数为满分
        breakdown['liquidity'] = round(liquidity * weights['liquidity'], 4)
        
        # 5. 消息消化程度 (0.15) - 接入新闻情绪系统（Task #7）
        news_digest_detail = {}
        if self.news_digest is not None:
            try:
                news_digestion, news_digest_detail = self.news_digest.calculate_news_digestion(
                    hk_code=pair.hk_code,
                    us_code=pair.us_code,
                    session_phase=session_phase,
                    current_time=current_time,
                )
            except Exception as _nd_e:
                logger.warning(
                    f"[calculate_arb_window_score] 新闻消化度计算失败，使用时间衰减 fallback: {_nd_e}"
                )
                news_digestion = None  # 触发下方 fallback
        else:
            news_digestion = None

        if news_digestion is None:
            # fallback: 原时间衰减模型
            if session_phase == 'pre_market':
                news_digestion = 0.7
            elif session_phase == 'arb_window':
                seconds_elapsed = 1800 - CrossMarketSessionWindow.get_seconds_to_arb_end(current_time)
                news_digestion = min(1.0, 0.7 + 0.3 * (seconds_elapsed / 1800))
            else:
                news_digestion = 1.0
            news_digest_detail['data_source'] = 'fallback_time_decay'

        breakdown['news_digestion'] = round(news_digestion * weights['news_digestion'], 4)
        # 写入 detail 供调试（单独存储，不参与评分求和）
        if news_digest_detail:
            breakdown['news_digestion_detail'] = news_digest_detail
        
        # 总分（仅对 float 维度求和，跳过 detail dict）
        window_score = sum(v for v in breakdown.values() if isinstance(v, (int, float)))
        
        return round(window_score, 4), breakdown
    
    def calculate_arb_total_score(
        self,
        lead_lag_strength: float,
        window_score: float,
        price_gap_pct: float,
        response_speed: float,
        liquidity_score: float,
        confidence: float,
        event_impact: str = "normal",
    ) -> Tuple[float, Dict, str]:
        """
        计算最终套利评分 (分层框架)
        
        Args:
            lead_lag_strength: Lead-Lag 强度 (0-1)
            window_score: 套利窗口分 (0-1)
            price_gap_pct: 价差偏离 (%)
            response_speed: 响应速度 (0-1)
            liquidity_score: 流动性评分 (0-1)
            confidence: 基础置信度 (0-1)
            event_impact: 事件强度
        
        Returns:
            (arb_total_score, score_breakdown, decision)
        """
        weights = self._scoring_weights['final_score']
        breakdown = {}
        
        # 事件强度映射
        event_strength_map = {
            'critical': 1.0,
            'high': 0.80,
            'normal': 0.60,
            'low': 0.40,
        }
        event_impact_score = event_strength_map.get(event_impact, 0.60)
        
        # 价格偏离归一化 (0-1)
        price_gap_normalized = min(1.0, abs(price_gap_pct) / 0.08)  # 8% 为满分
        
        # 计算各项
        breakdown['lead_lag_strength'] = round(
            lead_lag_strength * weights['lead_lag_strength'], 4
        )
        breakdown['event_impact'] = round(
            event_impact_score * weights['event_impact'], 4
        )
        breakdown['price_gap'] = round(
            price_gap_normalized * weights['price_gap'], 4
        )
        breakdown['response_speed'] = round(
            response_speed * weights['response_speed'], 4
        )
        breakdown['liquidity'] = round(
            liquidity_score * weights['liquidity'], 4
        )
        breakdown['confidence'] = round(
            confidence * weights['confidence'], 4
        )
        
        # 总分
        arb_total_score = sum(breakdown.values())
        
        # 决策建议
        if arb_total_score >= self._decision_thresholds['execute']:
            decision = 'execute'
        elif arb_total_score >= self._decision_thresholds['observe']:
            decision = 'observe'
        else:
            decision = 'skip'
        
        return round(arb_total_score, 4), breakdown, decision


# 模块导入时即加载时间窗口配置 (from config.yaml cross_market.session)
try:
    CrossMarketSessionWindow._load_session_config()
except Exception as _e:
    logger.warning(f"[CrossMarketSessionWindow] 时间窗口配置加载失败，使用默认值: {_e}")


# 全局单例
_strategy: Optional[CrossMarketArbitrage] = None


def get_cross_market_strategy(config: dict = None) -> CrossMarketArbitrage:
    """获取跨市场套利策略单例"""
    global _strategy
    if _strategy is None:
        _strategy = CrossMarketArbitrage(config)
    return _strategy


def init_cross_market_strategy(config: dict = None) -> CrossMarketArbitrage:
    """初始化跨市场套利策略"""
    global _strategy
    _strategy = CrossMarketArbitrage(config)
    return _strategy


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    strategy = get_cross_market_strategy()

    # 模拟美股隔夜数据
    us_data = [
        OvernightChange(
            code='BABA',
            name='Alibaba',
            close_price=85.5,
            change_pct=0.045,  # +4.5%
            volume=15000000,
            turnover=1280000000,
            market='US',
            timestamp=datetime.now()
        ),
        OvernightChange(
            code='JD',
            name='JD.com',
            close_price=28.3,
            change_pct=-0.038,  # -3.8%
            volume=8000000,
            turnover=226000000,
            market='US',
            timestamp=datetime.now()
        ),
    ]

    # 分析美股→港股
    signals = strategy.analyze_us_to_hk(us_data)

    print("\n📊 美股→港股套利信号:")
    for signal in signals:
        print(f"  {signal.code} {signal.action.value} | {signal.reason[:60]}")

    # 套利摘要
    print("\n套利摘要:")
    summary = strategy.get_arbitrage_summary()
    print(f"  美股隔夜数据：{summary['us_overnight_count']} 只")
    print(f"  启用配对：{summary['enabled_pairs']} 对")
