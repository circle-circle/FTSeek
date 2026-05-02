"""
策略管线适配器 (Pipeline Adapters)

回测和实盘统一走同一个 Pipeline 接口。
策略只负责产生 Signal，adapter 决定送往哪里。

Sink 实现:
  - ConsensusSink: 实盘 → 共识引擎
  - BacktestSink: 回测 → 模拟执行器

Usage:
    # 实盘
    from app.strategy.pipeline_adapter import ConsensusSink, StrategyPipeline
    pipeline = StrategyPipeline(strategy, ConsensusSink(consensus_engine))

    # 回测
    from app.strategy.pipeline_adapter import BacktestSink, StrategyPipeline
    pipeline = StrategyPipeline(strategy, BacktestSink(backtester))
"""
import logging
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger("ftseek.strategy.pipeline_adapter")


# ================================================================
#  实盘 Sink: 信号 → 共识引擎
# ================================================================

class ConsensusSink:
    """
    实盘信号接收器 -- 将 Signal 送入共识引擎。

    用法:
        sink = ConsensusSink(consensus_engine)
        result = sink.consume(signal)
    """

    def __init__(self, consensus_engine):
        self._engine = consensus_engine

    def consume(self, signal) -> Optional[Any]:
        """将信号提交到共识引擎"""
        if self._engine is None:
            logger.warning("[ConsensusSink] 共识引擎未就绪，信号丢弃")
            return None

        try:
            self._engine.submit(signal)
            logger.debug(f"[ConsensusSink] 信号已提交: {signal.strategy} → {signal.code} {signal.action.value}")
            return signal
        except Exception as e:
            logger.error(f"[ConsensusSink] 提交失败: {e}")
            return None


# ================================================================
#  回测 Sink: 信号 → 模拟执行器
# ================================================================

class BacktestSink:
    """
    回测信号接收器 -- 将 Signal 送入回测引擎。

    用法:
        sink = BacktestSink(backtester)
        result = sink.consume(signal)  # 返回 Trade 或 None
    """

    def __init__(self, backtester, on_signal: Callable = None):
        """
        Args:
            backtester: 回测引擎实例 (有 handle_signal 或 on_signal 方法)
            on_signal: 可选的自定义信号处理回调
        """
        self._backtester = backtester
        self._on_signal = on_signal

    def consume(self, signal) -> Optional[Any]:
        """将信号送入回测引擎"""
        if self._on_signal:
            return self._on_signal(signal)

        if self._backtester is None:
            logger.warning("[BacktestSink] 回测引擎未就绪")
            return None

        # 尝试调用回测引擎的信号处理方法
        if hasattr(self._backtester, "handle_signal"):
            return self._backtester.handle_signal(signal)
        elif hasattr(self._backtester, "on_signal"):
            return self._backtester.on_signal(signal)
        else:
            logger.warning(f"[BacktestSink] 回测引擎不支持信号处理")
            return None


# ================================================================
#  统一管线工厂
# ================================================================

def create_pipeline(strategy, mode: str = "paper", **kwargs) -> "StrategyPipeline":
    """
    创建统一管线 -- 回测/实盘共用。

    Args:
        strategy: BaseStrategy 实例
        mode: "paper" (实盘) / "backtest" (回测)
        **kwargs: 传递给 sink 的参数

    Returns:
        StrategyPipeline 实例
    """
    from app.strategy.strategy_contract import StrategyPipeline

    if mode == "paper":
        consensus_engine = kwargs.get("consensus_engine")
        sink = ConsensusSink(consensus_engine)
    elif mode == "backtest":
        backtester = kwargs.get("backtester")
        sink = BacktestSink(backtester)
    else:
        raise ValueError(f"未知模式: {mode}, 可用: paper, backtest")

    return StrategyPipeline(strategy, sink, validate=kwargs.get("validate", True))


# ================================================================
#  Registry 辅助: 自动根据 metadata 构建管线
# ================================================================

def build_pipelines_from_registry(registry, strategy_configs: Dict[str, Dict],
                                   mode: str = "paper", **sink_kwargs) -> Dict[str, "StrategyPipeline"]:
    """
    从注册表批量创建管线。

    Args:
        registry: StrategyRegistry 实例
        strategy_configs: {"策略名": {"param1": val1, ...}} 配置字典
        mode: "paper" / "backtest"
        **sink_kwargs: 传递给 sink 的参数

    Returns:
        {"策略名": StrategyPipeline, ...}
    """
    pipelines = {}

    for name, config in strategy_configs.items():
        entry = registry.get(name)
        if not entry:
            logger.warning(f"[PipelineFactory] 未知策略: {name}, 跳过")
            continue

        # 校验配置
        if entry.param_schema:
            ok, errors = entry.param_schema.validate(config)
            if not ok:
                logger.warning(f"[PipelineFactory] {name} 参数校验失败: {errors}, 跳过")
                continue

        # 创建策略实例
        strategy = entry.cls(config)

        # 创建管线
        pipeline = create_pipeline(strategy, mode, **sink_kwargs)
        pipelines[name] = pipeline

        logger.info(
            f"[PipelineFactory] 已创建: {name} (mode={mode}, "
            f"factor_type={entry.metadata.factor_type.value if entry.metadata else 'unknown'})"
        )

    return pipelines
