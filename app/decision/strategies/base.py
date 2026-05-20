"""
策略基类

定义所有策略必须实现的接口和方法
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# 全局策略注册表
_strategy_registry: Dict[str, type] = {}


@dataclass
class StrategyResult:
    """
    策略执行结果

    Attributes:
        direction: "LONG" | "SHORT" | None
        strength: 0-100 信号强度
        confidence: 0.0-1.0 置信度
        expected_horizon: 预期持有周期（分钟）
        volatility_fit: 是否适合当前波动率
        market_regime: 适合的市场状态
        risk_reward: 风险收益比
        score_breakdown: 评分明细
        metadata: 策略特定元数据
    """
    direction: Optional[str] = None
    strength: int = 50
    confidence: float = 0.5
    expected_horizon: int = 60
    volatility_fit: bool = True
    market_regime: str = "RANGE"
    risk_reward: float = 2.0
    score_breakdown: Dict[str, Any] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.score_breakdown is None:
            self.score_breakdown = {}
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "direction": self.direction,
            "strength": self.strength,
            "confidence": self.confidence,
            "expected_horizon": self.expected_horizon,
            "volatility_fit": self.volatility_fit,
            "market_regime": self.market_regime,
            "risk_reward": self.risk_reward,
            "score_breakdown": self.score_breakdown,
            "metadata": self.metadata,
        }

    def is_valid(self) -> bool:
        """检查结果是否有效（有交易信号）"""
        return self.direction in ("LONG", "SHORT")


class BaseStrategy(ABC):
    """
    策略基类

    所有策略必须继承此类并实现以下方法:
    - name(): 策略名称
    - version(): 策略版本
    - description(): 策略描述
    - generate_signal(): 生成信号
    - required_data(): 所需数据
    """

    # 策略参数（可被子类覆盖）
    DEFAULT_PARAMS = {
        "strength_threshold": 50,
        "confidence_floor": 0.3,
        "confidence_ceiling": 0.9,
    }

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        """
        初始化策略

        Args:
            params: 策略参数字典，会覆盖默认参数
        """
        self.params = {**self.DEFAULT_PARAMS, **(params or {})}
        self.created_at = datetime.now()
        self.execution_count = 0

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称（唯一标识）"""
        pass

    @property
    @abstractmethod
    def version(self) -> str:
        """策略版本号（语义化版本，如 1.0.0）"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """策略描述"""
        pass

    @property
    def required_data(self) -> List[str]:
        """
        所需数据字段列表

        返回策略执行需要的市场数据字段
        例如：["price", "volume", "ma20", "ma50"]
        """
        return []

    @abstractmethod
    def generate_signal(self, symbol: str, market_data: Dict[str, Any]) -> StrategyResult:
        """
        生成交易信号

        Args:
            symbol: 股票代码
            market_data: 市场数据字典

        Returns:
            StrategyResult: 策略执行结果
        """
        pass

    def validate_data(self, market_data: Dict[str, Any]) -> bool:
        """
        验证数据是否完整

        Args:
            market_data: 市场数据字典

        Returns:
            bool: 数据是否满足策略执行要求
        """
        for field in self.required_data:
            if field not in market_data:
                logger.debug(f"[{self.name}] 缺少数据字段：{field}")
                return False
        return True

    def _normalize_strength(self, raw_value: float, min_val: float = 0, max_val: float = 100) -> int:
        """
        将原始值归一化到 0-100 强度

        Args:
            raw_value: 原始值
            min_val: 最小值
            max_val: 最大值

        Returns:
            int: 0-100 的强度值
        """
        if max_val == min_val:
            return 50
        normalized = (raw_value - min_val) / (max_val - min_val)
        strength = int(normalized * 100)
        return max(0, min(100, strength))

    def _calculate_confidence(self, factors: Dict[str, float]) -> float:
        """
        根据多个因子计算置信度

        Args:
            factors: 因子字典 {因子名：分数 0-1}

        Returns:
            float: 0.0-1.0 置信度
        """
        if not factors:
            return 0.5

        # 简单平均（子类可以覆盖）
        return round(sum(factors.values()) / len(factors), 2)

    def execute(self, symbol: str, market_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        执行策略（包装方法）

        Args:
            symbol: 股票代码
            market_data: 市场数据

        Returns:
            dict: 信号字典或 None（无信号）
        """
        # 验证数据
        if not self.validate_data(market_data):
            logger.debug(f"[{self.name}] {symbol} 数据不完整，跳过")
            return None

        # 生成信号
        result = self.generate_signal(symbol, market_data)

        # 更新执行计数
        self.execution_count += 1

        # 检查信号有效性
        if not result.is_valid():
            logger.debug(f"[{self.name}] {symbol} 无有效信号")
            return None

        # 转换为字典格式
        return result.to_dict()

    def get_stats(self) -> Dict[str, Any]:
        """获取策略统计信息"""
        return {
            "name": self.name,
            "version": self.version,
            "execution_count": self.execution_count,
            "created_at": self.created_at.isoformat(),
            "params": self.params,
        }


# ============================================================
# 策略注册机制
# ============================================================

def register_strategy(strategy_class: type) -> type:
    """
    注册策略到全局注册表（装饰器）

    Usage:
        @register_strategy
        class MyStrategy(BaseStrategy):
            ...
    """
    if not issubclass(strategy_class, BaseStrategy):
        raise TypeError(f"{strategy_class.__name__} 必须继承 BaseStrategy")

    # 创建实例获取名称
    instance = strategy_class()
    strategy_name = instance.name

    _strategy_registry[strategy_name] = strategy_class
    logger.info(f"[策略注册] {strategy_name} v{instance.version}")

    return strategy_class


def get_strategy(strategy_name: str, params: Optional[Dict[str, Any]] = None) -> Optional[BaseStrategy]:
    """
    获取策略实例

    Args:
        strategy_name: 策略名称
        params: 策略参数（可选）

    Returns:
        BaseStrategy 实例或 None
    """
    strategy_class = _strategy_registry.get(strategy_name)
    if strategy_class is None:
        logger.warning(f"[策略] 未找到策略：{strategy_name}")
        return None

    return strategy_class(params=params)


def list_strategies() -> List[Dict[str, str]]:
    """
    列出所有已注册的策略

    Returns:
        策略信息列表
    """
    result = []
    for name, cls in _strategy_registry.items():
        instance = cls()
        result.append({
            "name": name,
            "version": instance.version,
            "description": instance.description,
            "required_data": instance.required_data,
        })
    return result
