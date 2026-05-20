"""
PipelineRunner — 管线编排器 + Stage 协议

Karpathy 原则: 编排与执行分离。Runner 按 YAML 配置的顺序串联各 Stage,
每个 Stage 是独立模块, 通过 P0 数据契约通信。

Stage 协议:
    每个 Stage 必须有一个 process() 函数, 接受上一阶段的输出, 返回下一阶段的输入。
    管线链: symbol → SymbolContext → SignalCandidate → OrderRequest

配置驱动:
    pipeline:
      stages:
        - name: market_data
          enabled: true
        - name: signal
          enabled: true
        - name: risk
          enabled: true

EventBus 集成:
    每个 Stage 完成后 emit 事件, Observer 异步响应 (通知/教训/审计),
    不阻塞主管线。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Any, List, Optional, Protocol, Callable
from dataclasses import dataclass, field

from app.core.pipeline_contracts import SymbolContext, SignalCandidate, OrderRequest

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Stage Protocol
# ═══════════════════════════════════════════════════════════════════

class PipelineStage(Protocol):
    """管线阶段协议 — 任何具有 name + process 的可调用对象都是合法 Stage

    典型实现:
        - 模块级 process() 函数 (stages/market_data_stage.py)
        - 带状态的类实例 (class SomeStage with process method)
    """

    name: str

    def process(self, *args, **kwargs) -> Any:
        ...


@dataclass
class StageConfig:
    """单 Stage 的运行时配置"""
    name: str
    enabled: bool = True
    timeout_sec: float = 30.0         # 单 Stage 超时
    abort_on_error: bool = False      # 此 Stage 失败是否终止管线
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    """Stage 执行结果"""
    stage_name: str
    success: bool
    output: Any = None                # 数据契约 (SymbolContext / SignalCandidate / OrderRequest)
    elapsed_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class PipelineResult:
    """管线完整执行结果"""
    symbol: str
    stages: List[StageResult] = field(default_factory=list)
    final_output: Optional[OrderRequest] = None
    total_elapsed_ms: float = 0.0
    aborted: bool = False
    abort_reason: str = ""


# ═══════════════════════════════════════════════════════════════════
#  PipelineRunner
# ═══════════════════════════════════════════════════════════════════

class PipelineRunner:
    """按配置顺序编排管线 Stage 的执行器

    每个 Stage 是独立的 process() 函数。Runner 负责:
      1. 按配置顺序调用 Stage
      2. 传递数据契约作为 Stage 间上下文
      3. 在 Stage 边界 emit EventBus 事件
      4. 超时保护 + 错误隔离
      5. 性能计时

    使用方式:
        runner = PipelineRunner(stages_config, shared_state)
        result = runner.run(symbol="US.AAPL")
    """

    def __init__(self, stages: List[PipelineStage],
                 stages_config: Optional[List[StageConfig]] = None,
                 shared_state: Optional[Dict[str, Any]] = None):
        """
        Args:
            stages: Stage 列表 (按执行顺序)
            stages_config: Stage 配置列表 (覆盖 enabled/timeout/abort_on_error)
            shared_state: 共享状态 dict (vix, spy_context, risk_engine, data_router, ...)
        """
        self._stages = stages
        self._shared_state = shared_state or {}
        self._event_bus = None

        # 构建配置索引
        self._config: Dict[str, StageConfig] = {}
        if stages_config:
            for sc in stages_config:
                self._config[sc.name] = sc

        # 默认提供所有已注册 Stage 的配置
        for stage in stages:
            if stage.name not in self._config:
                self._config[stage.name] = StageConfig(name=stage.name)

    @property
    def event_bus(self):
        if self._event_bus is None:
            from app.pipeline.observers import get_event_bus
            self._event_bus = get_event_bus()
        return self._event_bus

    def run(self, symbol: str) -> PipelineResult:
        """执行完整管线, 返回 PipelineResult"""
        t0 = time.monotonic()
        result = PipelineResult(symbol=symbol)

        current_context: Any = symbol  # 初始输入是 symbol 字符串

        for stage in self._stages:
            cfg = self._config.get(stage.name, StageConfig(name=stage.name))
            if not cfg.enabled:
                logger.info(f"[Pipeline] {symbol} Stage '{stage.name}' 已禁用, 跳过")
                continue

            sr = self._execute_stage(stage, cfg, current_context, symbol)
            result.stages.append(sr)

            if not sr.success:
                if cfg.abort_on_error:
                    result.aborted = True
                    result.abort_reason = f"Stage '{stage.name}' 失败: {sr.error}"
                    logger.error(f"[Pipeline] {symbol} 中止于 Stage '{stage.name}': {sr.error}")
                    break
                logger.warning(f"[Pipeline] {symbol} Stage '{stage.name}' 失败但继续: {sr.error}")

            if sr.output is None:
                # Stage 返回 None = 管线到此终止 (被拦截/无信号/风控拒绝)
                logger.info(f"[Pipeline] {symbol} Stage '{stage.name}' 返回 None, 管线终止")
                break

            current_context = sr.output

        # 提取最终输出
        if current_context and isinstance(current_context, OrderRequest):
            result.final_output = current_context

        result.total_elapsed_ms = (time.monotonic() - t0) * 1000

        # 管线完成事件
        self.event_bus.emit("pipeline_complete",
                            symbol=symbol,
                            order=result.final_output,
                            aborted=result.aborted,
                            reason=result.abort_reason,
                            elapsed_ms=result.total_elapsed_ms,
                            stages_passed=len([s for s in result.stages if s.success]))

        return result

    def _execute_stage(self, stage: PipelineStage, cfg: StageConfig,
                       context: Any, symbol: str) -> StageResult:
        """执行单个 Stage, 带超时和错误保护"""
        t0 = time.monotonic()
        stage_name = stage.name

        try:
            # 构建输入 — 根据 stage name 分发 shared_state 中的依赖
            stage_input = self._build_stage_input(stage_name, context)
            output = stage.process(**stage_input)

            elapsed = (time.monotonic() - t0) * 1000

            # 检查输出类型
            output_desc = self._describe_output(output)
            logger.info(f"[Pipeline] {symbol} {stage_name} ✓ {output_desc} ({elapsed:.0f}ms)")

            # emit 对应事件
            self._emit_stage_event(stage_name, symbol, output, elapsed)

            return StageResult(
                stage_name=stage_name,
                success=True,
                output=output,
                elapsed_ms=elapsed,
            )

        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            logger.error(f"[Pipeline] {symbol} {stage_name} ✗ {e} ({elapsed:.0f}ms)", exc_info=True)

            self.event_bus.emit("stage_error",
                                symbol=symbol, stage=stage_name, error=str(e))

            return StageResult(
                stage_name=stage_name,
                success=False,
                error=str(e),
                elapsed_ms=elapsed,
            )

    def _build_stage_input(self, stage_name: str, context: Any) -> Dict[str, Any]:
        """根据 Stage 名称构建 process() 的输入参数

        每个 Stage 的 process() 签名:
          - market_data: (symbol, data_router, vix, spy_context, earnings_guard, shared_kernel)
          - signal:      (ctx: SymbolContext, position_store, battle_plan_manager, voting_coordinator)
          - risk:        (candidate: SignalCandidate, risk_engine, position_store, execution_engine, polling_controller)
        """
        base = {}

        if stage_name == "market_data":
            base["symbol"] = context
            base["data_router"] = self._shared_state.get("data_router")
            base["vix"] = self._shared_state.get("vix", 0)
            base["spy_context"] = self._shared_state.get("spy_context", {})
            base["earnings_guard"] = self._shared_state.get("earnings_guard")
            base["shared_kernel"] = self._shared_state.get("shared_kernel")

        elif stage_name == "signal":
            base["ctx"] = context  # SymbolContext
            base["position_store"] = self._shared_state.get("position_store")
            base["battle_plan_manager"] = self._shared_state.get("battle_plan_manager")
            base["voting_coordinator"] = self._shared_state.get("voting_coordinator")

        elif stage_name == "risk":
            base["candidate"] = context  # SignalCandidate
            base["risk_engine"] = self._shared_state.get("risk_engine")
            base["position_store"] = self._shared_state.get("position_store")
            base["execution_engine"] = self._shared_state.get("execution_engine")
            base["polling_controller"] = self._shared_state.get("polling_controller")

        else:
            # 自定义 Stage — 传入 context + 全部 shared_state
            base["context"] = context
            base["shared_state"] = self._shared_state

        return base

    def _emit_stage_event(self, stage_name: str, symbol: str,
                          output: Any, elapsed_ms: float):
        """在 Stage 边界 emit 事件"""
        if stage_name == "signal" and isinstance(output, SignalCandidate):
            self.event_bus.emit("signal_generated",
                                symbol=symbol, candidate=output, elapsed_ms=elapsed_ms)

        elif stage_name == "risk" and isinstance(output, OrderRequest):
            if output.risk_approved:
                self.event_bus.emit("order_approved",
                                    symbol=symbol, order=output, elapsed_ms=elapsed_ms)
            else:
                self.event_bus.emit("risk_rejected",
                                    symbol=symbol,
                                    reason="risk_checks_failed",
                                    checks=output.risk_checks_passed)

    @staticmethod
    def _describe_output(output: Any) -> str:
        if output is None:
            return "→ None (拦截)"
        if isinstance(output, SymbolContext):
            return f"→ SymbolContext(price={output.price:.2f})"
        if isinstance(output, SignalCandidate):
            return f"→ SignalCandidate({output.direction} strength={output.strength})"
        if isinstance(output, OrderRequest):
            return f"→ OrderRequest(qty={output.quantity} sl={output.stop_loss:.2f})"
        return f"→ {type(output).__name__}"


# ═══════════════════════════════════════════════════════════════════
#  预定义 Stage 适配器 (包装模块级 process() 函数为 PipelineStage)
# ═══════════════════════════════════════════════════════════════════

class ModularStage:
    """将 modules/stages 中的 process() 函数包装为 PipelineStage 协议

    使用方式:
        from app.pipeline.stages import market_data_stage
        stage = ModularStage("market_data", market_data_stage.process)
    """

    def __init__(self, name: str, process_fn: Callable):
        self.name = name
        self._process_fn = process_fn

    def process(self, **kwargs) -> Any:
        return self._process_fn(**kwargs)


# ═══════════════════════════════════════════════════════════════════
#  工厂函数
# ═══════════════════════════════════════════════════════════════════

def build_default_pipeline(shared_state: Dict[str, Any]) -> PipelineRunner:
    """构建默认管线 (market_data → signal → risk)

    从 YAML pipeline.stages 读取启用/禁用状态,
    这里只定义默认 Stage 顺序, 配置中心在运行时提供覆盖。
    """
    from app.pipeline.stages import market_data_stage, signal_stage, risk_stage

    stages = [
        ModularStage("market_data", market_data_stage.process),
        ModularStage("signal", signal_stage.process),
        ModularStage("risk", risk_stage.process),
    ]

    stages_config = _load_stages_config()
    return PipelineRunner(stages, stages_config=stages_config, shared_state=shared_state)


def _load_stages_config() -> List[StageConfig]:
    """从 YAML pipeline.stages 读取 Stage 配置"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        stages_raw = cfg.get_dotted("pipeline.stages")
        if stages_raw and isinstance(stages_raw, list):
            result = []
            for item in stages_raw:
                if isinstance(item, dict):
                    result.append(StageConfig(
                        name=item.get("name", ""),
                        enabled=item.get("enabled", True),
                        timeout_sec=float(item.get("timeout_sec", 30.0)),
                        abort_on_error=item.get("abort_on_error", False),
                    ))
            if result:
                return result
    except Exception:
        pass

    # 默认: 全部启用
    return [
        StageConfig(name="market_data", enabled=True),
        StageConfig(name="signal", enabled=True),
        StageConfig(name="risk", enabled=True),
    ]
