"""
策略标准契约 (Strategy Contracts)

定义策略系统的最小接口契约，消除上下游硬编码耦合。

3 个硬契约:
  1. MetadataContract: 策略元信息声明 (类级别)
  2. SignalContract: 标准信号输出 (实例级别)
  3. ParamsContract: 参数 Schema 校验 (启动 + 热更新)

2 个软契约:
  4. LifecycleContract: warmup / state / health
  5. AdapterContract: 回测/实盘统一管线

Usage:
    from app.strategy.strategy_contract import (
        FactorType, TimeHorizon, StrategyMetadata,
        SignalContract, ParamField, StrategyRegistry,
    )
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple, Type, Callable

logger = logging.getLogger("ftseek.strategy.contracts")


# ================================================================
#  Hard Contract 1: MetadataContract (策略元信息)
# ================================================================

class FactorType(str, Enum):
    """因子类型 -- 用于共识引擎自动分类"""
    MOMENTUM = "momentum"              # 趋势/动量 (MACD, SuperTrend, MA Cross)
    MEAN_REVERSION = "mean_reversion"  # 均值回归 (RSI, Bollinger)
    HYBRID = "hybrid"                  # 混合因子 (Confluence, Fusion)
    EVENT_DRIVEN = "event_driven"      # 事件驱动 (EventDriven, Sentiment)
    CROSS_MARKET = "cross_market"      # 跨市场 (Arbitrage)
    FUNDAMENTAL = "fundamental"        # 基本面 (Valuation)
    TIME_BASED = "time_based"          # 时间驱动 (Open/Close Effect)


class TimeHorizon(str, Enum):
    """预期持仓时长"""
    INTRADAY = "intraday"   # 日内 (< 1 天)
    SWING = "swing"         # 波段 (1-5 天)
    POSITION = "position"   # 仓位 (5 天+)
    SCALP = "scalp"         # 超短 (< 30 分钟)


class RiskLevel(int, Enum):
    """风险等级 1-5"""
    LOW = 1
    MEDIUM = 2
    MODERATE = 3
    HIGH = 4
    VERY_HIGH = 5


@dataclass(frozen=True)
class StrategyMetadata:
    """
    策略元信息 -- 策略必须在类级别声明。
    冻结 dataclass 防止运行时篡改。
    """
    factor_type: FactorType
    horizon: TimeHorizon
    regime_affinity: List[str] = field(default_factory=list)  # ["trending", "ranging", "neutral"]
    risk_level: RiskLevel = RiskLevel.MEDIUM
    required_klines: int = 30       # 预热所需 K 线数
    description: str = ""
    author: str = ""
    version: str = "1.0.0"
    created_at: str = ""

    def is_compatible_with_regime(self, regime: str) -> bool:
        """判断策略是否适配当前市场状态"""
        if not self.regime_affinity:
            return True  # 未声明 affinity → 全兼容
        return regime in self.regime_affinity

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict (供 JSON 输出/注册表展示)"""
        return {
            "factor_type": self.factor_type.value,
            "horizon": self.horizon.value,
            "regime_affinity": self.regime_affinity,
            "risk_level": self.risk_level.value,
            "required_klines": self.required_klines,
            "description": self.description,
            "author": self.author,
            "version": self.version,
        }


# ================================================================
#  Hard Contract 2: SignalContract (标准信号输出)
# ================================================================

STANDARD_EXTRA_FIELDS = {"technical_score", "conviction", "factor_readings"}


class SignalContract:
    """
    信号标准字段校验器。

    Signal.extra 必须包含以下标准字段:
      - technical_score: float [0, 1]  技术得分
      - conviction: float [0, 1]       信号置信度
      - factor_readings: dict          原始因子读数 (审计用)

    允许额外字段，但标准字段必须存在且类型正确。

    用法:
        ok, errors = SignalContract.validate(signal)
    """

    @staticmethod
    def validate(signal: Any) -> Tuple[bool, List[str]]:
        """校验 Signal.extra 是否包含标准字段"""
        errors = []
        extra = getattr(signal, "extra", None) or {}

        # technical_score
        ts = extra.get("technical_score")
        if ts is None:
            errors.append("缺少 technical_score")
        elif not isinstance(ts, (int, float)):
            errors.append(f"technical_score 类型错误: {type(ts).__name__}")
        elif ts < 0 or ts > 1:
            errors.append(f"technical_score 超出范围 [0,1]: {ts}")

        # conviction
        cv = extra.get("conviction")
        if cv is None:
            errors.append("缺少 conviction")
        elif not isinstance(cv, (int, float)):
            errors.append(f"conviction 类型错误: {type(cv).__name__}")
        elif cv < 0 or cv > 1:
            errors.append(f"conviction 超出范围 [0,1]: {cv}")

        # factor_readings
        fr = extra.get("factor_readings")
        if fr is None:
            errors.append("缺少 factor_readings")
        elif not isinstance(fr, dict):
            errors.append(f"factor_readings 类型错误: {type(fr).__name__}")

        return len(errors) == 0, errors

    @staticmethod
    def normalize(strategy_name: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        """
        将策略特有 extra 映射为标准字段。

        每种策略有自己的字段名，这里做统一映射。
        新策略需要在此添加映射规则。
        """
        normalized = dict(extra)  # 保留原始字段

        mapper = SIGNAL_MAPPERS.get(strategy_name)
        if mapper:
            mapped = mapper(extra)
            normalized.update(mapped)

        # 兜底: 如果标准字段仍缺失，填充默认值
        if "technical_score" not in normalized:
            normalized["technical_score"] = 0.5  # 中性
        if "conviction" not in normalized:
            normalized["conviction"] = 0.5
        if "factor_readings" not in normalized:
            normalized["factor_readings"] = {}

        return normalized

    @staticmethod
    def fill_standards(signal: Any, strategy_name: str) -> Any:
        """
        就地为 Signal 补充标准字段 (向后兼容: 不改变 Signal 构造函数)。

        用法 (在 Pipeline Adapter 中调用):
            signal = SignalContract.fill_standards(signal, strategy.name)
        """
        extra = getattr(signal, "extra", None) or {}
        normalized = SignalContract.normalize(strategy_name, extra)
        signal.extra = normalized
        return signal


# ── 策略特有字段 → 标准字段映射 ────────────────────────────────────

SIGNAL_MAPPERS: Dict[str, Callable[[Dict], Dict]] = {}


def _register_mapper(strategy_name: str):
    """装饰器: 注册策略的信号映射规则"""
    def decorator(func):
        SIGNAL_MAPPERS[strategy_name] = func
        return func
    return decorator


@_register_mapper("confluence_v2")
def _map_confluence_v2(extra: Dict) -> Dict:
    bullish = extra.get("bullish_score", 0)
    conviction = extra.get("conviction", 0.5)
    return {
        "technical_score": min(1.0, max(0.0, bullish / 3.0)),  # 3 因子 = 满分
        "conviction": conviction,
        "factor_readings": extra.get("factors", {}),
    }


@_register_mapper("sentiment_ai")
def _map_sentiment_ai(extra: Dict) -> Dict:
    sentiment = extra.get("sentiment_score", 0)
    return {
        "technical_score": 0.0,  # 情绪策略不参与技术评分
        "conviction": abs(sentiment),
        "factor_readings": {"sentiment_score": sentiment},
    }


@_register_mapper("event_driven")
def _map_event_driven(extra: Dict) -> Dict:
    confidence = extra.get("confidence", 50) / 100.0
    sentiment = extra.get("sentiment_score", 0)
    return {
        "technical_score": 0.0,
        "conviction": confidence,
        "factor_readings": {"sentiment_score": sentiment, "impact": extra.get("impact_level", "low")},
    }


@_register_mapper("rsi")
def _map_rsi(extra: Dict) -> Dict:
    rsi_val = extra.get("rsi", 50)
    # RSI 越低 (超卖) → 做多技术得分越高
    if rsi_val <= 30:
        score = 1.0 - (rsi_val / 30.0)  # 0→1.0
    elif rsi_val >= 70:
        score = (rsi_val - 70.0) / 30.0  # 0→1.0 (做空信号)
    else:
        score = 0.3  # 中性区
    return {
        "technical_score": max(0.0, min(1.0, score)),
        "conviction": 0.5,
        "factor_readings": {"rsi": rsi_val, "prev_rsi": extra.get("prev_rsi")},
    }


@_register_mapper("macd")
def _map_macd(extra: Dict) -> Dict:
    hist = extra.get("histogram", 0)
    return {
        "technical_score": 0.5,  # MACD 是方向性指标，不直接映射 0-1
        "conviction": min(1.0, abs(hist) * 100),
        "factor_readings": {"macd": extra.get("macd"), "histogram": hist},
    }


@_register_mapper("bollinger")
def _map_bollinger(extra: Dict) -> Dict:
    pctb = extra.get("pctb", 0.5)
    # %B < 0.1 → 高分 (超卖做多), %B > 0.9 → 高分 (超买做空)
    if pctb < 0.1:
        score = 1.0 - pctb * 10
    elif pctb > 0.9:
        score = (pctb - 0.9) * 10
    else:
        score = 0.3
    return {
        "technical_score": max(0.0, min(1.0, score)),
        "conviction": 0.5,
        "factor_readings": {"pctb": pctb},
    }


@_register_mapper("supertrend")
def _map_supertrend(extra: Dict) -> Dict:
    direction = extra.get("direction", 0)
    return {
        "technical_score": 0.7 if direction != 0 else 0.3,
        "conviction": extra.get("conviction", 0.5),
        "factor_readings": {"supertrend_direction": direction},
    }


@_register_mapper("open_close_effect")
def _map_open_close(extra: Dict) -> Dict:
    return {
        "technical_score": 0.5,
        "conviction": extra.get("confidence", 0.5),
        "factor_readings": {"effect_type": extra.get("effect_type", "unknown")},
    }


@_register_mapper("cross_market_arbitrage")
def _map_cross_market(extra: Dict) -> Dict:
    return {
        "technical_score": 0.5,
        "conviction": extra.get("correlation", 0.5),
        "factor_readings": {"correlation": extra.get("correlation"), "spread": extra.get("spread")},
    }


@_register_mapper("enhanced_confluence")
def _map_enhanced(extra: Dict) -> Dict:
    score = extra.get("multi_timeframe_score", 0.5)
    return {
        "technical_score": max(0.0, min(1.0, score)),
        "conviction": extra.get("conviction", 0.5),
        "factor_readings": extra.get("factor_details", {}),
    }


# ================================================================
#  Hard Contract 3: ParamsContract (参数 Schema)
# ================================================================

@dataclass
class ParamField:
    """参数字段定义"""
    type: str  # "int", "float", "str", "bool", "list"
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[List[str]] = None  # 枚举值
    required: bool = True
    description: str = ""

    def validate(self, value: Any) -> Tuple[bool, str]:
        """校验单个参数值"""
        # 类型检查
        try:
            if self.type == "int":
                value = int(value)
            elif self.type == "float":
                value = float(value)
            elif self.type == "bool":
                if isinstance(value, str):
                    value = value.lower() in ("true", "1", "yes")
                value = bool(value)
            elif self.type == "str":
                value = str(value)
            elif self.type == "list":
                if isinstance(value, str):
                    value = [x.strip() for x in value.split(",")]
                value = list(value)
        except (ValueError, TypeError):
            return False, f"类型错误: 期望 {self.type}"

        # 范围检查
        if self.min is not None and value < self.min:
            return False, f"值 {value} < 最小值 {self.min}"
        if self.max is not None and value > self.max:
            return False, f"值 {value} > 最大值 {self.max}"

        # 枚举检查
        if self.choices and value not in self.choices:
            return False, f"值 {value} 不在允许范围 {self.choices}"

        return True, ""


class ParamsContract:
    """参数 Schema 校验器"""

    @staticmethod
    def validate(schema: Dict[str, ParamField], params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        校验一组参数是否符合 schema

        Returns:
            (ok, errors) -- 如果 ok=False, errors 列出所有不合规项
        """
        errors = []

        # 检查未知参数
        for key in params:
            if key not in schema:
                errors.append(f"未知参数: {key}")

        # 校验已知参数
        for key, field_def in schema.items():
            if key not in params:
                if field_def.required:
                    errors.append(f"缺少必填参数: {key}")
                continue

            ok, msg = field_def.validate(params[key])
            if not ok:
                errors.append(f"{key}: {msg}")

        return len(errors) == 0, errors

    @staticmethod
    def coerce(schema: Dict[str, ParamField], params: Dict[str, Any]) -> Dict[str, Any]:
        """将参数值按 schema 类型转换 (用于 update_params 前的预处理)"""
        result = {}
        for key, value in params.items():
            field_def = schema.get(key)
            if field_def is None:
                result[key] = value
                continue
            try:
                if field_def.type == "int":
                    result[key] = int(value)
                elif field_def.type == "float":
                    result[key] = float(value)
                elif field_def.type == "bool":
                    if isinstance(value, str):
                        result[key] = value.lower() in ("true", "1", "yes")
                    else:
                        result[key] = bool(value)
                elif field_def.type == "list":
                    if isinstance(value, str):
                        result[key] = [x.strip() for x in value.split(",")]
                    else:
                        result[key] = list(value)
                else:
                    result[key] = value
            except (ValueError, TypeError):
                result[key] = value  # 转换失败，保留原值 (由 validate 报错)
        return result

    @staticmethod
    def from_dict(raw: Dict[str, Dict]) -> Dict[str, ParamField]:
        """从原始 dict 构建 ParamField 映射"""
        result = {}
        for key, spec in raw.items():
            result[key] = ParamField(
                type=spec.get("type", "str"),
                default=spec.get("default"),
                min=spec.get("min"),
                max=spec.get("max"),
                choices=spec.get("choices"),
                required=spec.get("required", True),
                description=spec.get("description", ""),
            )
        return result


# ================================================================
#  Soft Contract 4: LifecycleContract (最小可用)
# ================================================================

class LifecycleContract:
    """
    策略生命周期最小接口。

    子类可选择性实现，不强制。
    """

    @staticmethod
    def check_warmed_up(strategy, code: str) -> bool:
        """
        检查策略对某代码是否已预热。
        优先调用策略的 is_warmed_up(code)，否则返回 True (兜底)。
        """
        if hasattr(strategy, "is_warmed_up"):
            return strategy.is_warmed_up(code)
        return True

    @staticmethod
    def get_health_report(strategy) -> Dict[str, Any]:
        """
        获取策略健康报告。
        优先调用策略的 get_health_report()，否则返回基础信息。
        """
        if hasattr(strategy, "get_health_report"):
            return strategy.get_health_report()
        return {
            "name": getattr(strategy, "name", "unknown"),
            "enabled": getattr(strategy, "enabled", False),
            "status": "unknown",
        }

    @staticmethod
    def save_state(strategy) -> Optional[Dict[str, Any]]:
        """保存策略状态 (如果策略实现了 get_state)"""
        if hasattr(strategy, "get_state"):
            return strategy.get_state()
        return None

    @staticmethod
    def restore_state(strategy, state: Dict[str, Any]) -> bool:
        """恢复策略状态 (如果策略实现了 restore_state)"""
        if hasattr(strategy, "restore_state"):
            return strategy.restore_state(state)
        return False


# ================================================================
#  Soft Contract 5: AdapterContract (管线适配器)
# ================================================================

class SignalSink:
    """
    信号接收器接口 -- 回测/实盘的共同抽象。

    策略只负责产生 Signal，adapter 决定送往哪里。
    """

    def consume(self, signal) -> Optional[Any]:
        """
        消费信号。

        Returns:
            处理结果 (实盘: ConsensusResult, 回测: Trade) 或 None (被拒绝)
        """
        raise NotImplementedError


class StrategyPipeline:
    """
    统一管线 -- 策略 → 标准化 → 输出。

    回测和实盘都走同一个 pipeline，区别仅在于 sink 不同。
    """

    def __init__(self, strategy, sink: SignalSink, validate: bool = True):
        """
        Args:
            strategy: BaseStrategy 实例
            sink: 信号接收器 (回测引擎 / 共识引擎适配器)
            validate: 是否校验 SignalContract (生产环境必须 True)
        """
        self.strategy = strategy
        self.sink = sink
        self.validate = validate
        self._signal_count = 0
        self._rejected_count = 0

    def on_kline(self, code: str, kline: Dict[str, Any], kline_type: str) -> Optional[Any]:
        """统一 K 线入口 -- 回测/实盘共用"""
        signal = self.strategy.on_kline(code, kline, kline_type)
        if signal is None:
            return None
        return self._process_signal(signal)

    def on_quote(self, code: str, quote: Dict[str, Any]) -> Optional[Any]:
        """统一报价入口 -- 回测/实盘共用"""
        signal = self.strategy.on_quote(code, quote)
        if signal is None:
            return None
        return self._process_signal(signal)

    def _process_signal(self, signal) -> Optional[Any]:
        """处理信号: 标准化 → 校验 → 发送到 sink"""
        self._signal_count += 1

        # 标准化: 补充标准字段
        SignalContract.fill_standards(signal, self.strategy.name)

        # 校验
        if self.validate:
            ok, errors = SignalContract.validate(signal)
            if not ok:
                self._rejected_count += 1
                logger.warning(
                    f"[Pipeline] {self.strategy.name} 信号校验失败: {errors} "
                    f"| code={signal.code} action={signal.action.value}"
                )
                return None

        # 发送到 sink
        return self.sink.consume(signal)

    def stats(self) -> Dict[str, int]:
        return {
            "total_signals": self._signal_count,
            "rejected_signals": self._rejected_count,
        }


# ================================================================
#  Registry + 查询接口 (consensus_engine 替代硬编码)
# ================================================================

@dataclass
class StrategyEntry:
    """注册表条目: 策略类 + 元信息 + Schema"""
    cls: Type
    metadata: Optional[StrategyMetadata] = None
    param_schema: Optional[Dict[str, ParamField]] = None

    @property
    def name(self) -> str:
        return getattr(self.cls, "name", "unknown")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "class": self.cls.__name__,
            "metadata": self.metadata.to_dict() if self.metadata else {},
            "params": {k: {"type": v.type, "default": v.default, "min": v.min, "max": v.max}
                       for k, v in (self.param_schema or {}).items()},
        }


class StrategyRegistry:
    """
    策略注册表 -- 替代 consensus_engine 的硬编码策略集合。

    用法:
        registry = StrategyRegistry()
        registry.register("confluence_v2", ConfluenceStrategyV2)

        # 查询
        trend_strategies = registry.by_factor_type(FactorType.MOMENTUM)
        low_risk = registry.by_risk_level(RiskLevel.LOW)
    """

    def __init__(self):
        self._entries: Dict[str, StrategyEntry] = {}

    def register(self, name: str, strategy_cls: Type,
                 metadata: Optional[StrategyMetadata] = None,
                 param_schema: Optional[Dict[str, ParamField]] = None):
        """注册策略"""
        # 优先使用类级别声明的元信息
        if metadata is None:
            metadata = getattr(strategy_cls, "metadata", None)

        # 优先使用类级别声明的 schema
        if param_schema is None:
            raw_schema = getattr(strategy_cls, "param_schema", None)
            if raw_schema:
                param_schema = ParamsContract.from_dict(raw_schema)

        self._entries[name] = StrategyEntry(
            cls=strategy_cls,
            metadata=metadata,
            param_schema=param_schema,
        )
        logger.info(f"[Registry] 已注册: {name} ({strategy_cls.__name__})")

    def get(self, name: str) -> Optional[StrategyEntry]:
        return self._entries.get(name)

    def list_all(self) -> Dict[str, StrategyEntry]:
        return dict(self._entries)

    def by_factor_type(self, factor_type: FactorType) -> List[str]:
        """按因子类型查询策略名称列表"""
        return [name for name, entry in self._entries.items()
                if entry.metadata and entry.metadata.factor_type == factor_type]

    def by_horizon(self, horizon: TimeHorizon) -> List[str]:
        """按持仓时长查询"""
        return [name for name, entry in self._entries.items()
                if entry.metadata and entry.metadata.horizon == horizon]

    def by_regime_affinity(self, regime: str) -> List[str]:
        """按市场状态亲和度查询"""
        return [name for name, entry in self._entries.items()
                if entry.metadata and entry.metadata.is_compatible_with_regime(regime)]

    def by_risk_level(self, max_risk: RiskLevel) -> List[str]:
        """按风险等级查询 (≤ max_risk)"""
        return [name for name, entry in self._entries.items()
                if entry.metadata and entry.metadata.risk_level.value <= max_risk.value]

    def create(self, name: str, config: Optional[Dict] = None):
        """创建策略实例"""
        entry = self._entries.get(name)
        if not entry:
            raise ValueError(f"未知策略: {name}, 可用: {list(self._entries.keys())}")
        return entry.cls(config)

    def info(self, name: str = None) -> Dict[str, Any]:
        """获取策略信息 dict"""
        if name:
            entry = self._entries.get(name)
            return entry.to_dict() if entry else {}
        return {n: e.to_dict() for n, e in self._entries.items()}

    def has(self, name: str) -> bool:
        return name in self._entries
