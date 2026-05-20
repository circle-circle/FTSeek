"""
FTSeek-US Pipeline Stages — 模块化管线组件

每个 Stage 是独立模块, 通过 P0 数据契约通信:
  SymbolContext → SignalCandidate → OrderRequest

使用方式 (新代码):
    from app.pipeline.stages.runner import PipelineRunner, ModularStage, build_default_pipeline
    runner = build_default_pipeline(shared_state)
    result = runner.run(symbol="US.AAPL")
"""

from app.pipeline.stages.market_data_stage import process as market_data_process
from app.pipeline.stages.signal_stage import process as signal_process
from app.pipeline.stages.risk_stage import process as risk_process
from app.pipeline.stages.runner import (
    PipelineRunner,
    ModularStage,
    PipelineStage,
    StageConfig,
    StageResult,
    PipelineResult,
    build_default_pipeline,
)

__all__ = [
    "market_data_process",
    "signal_process",
    "risk_process",
    "PipelineRunner",
    "ModularStage",
    "PipelineStage",
    "StageConfig",
    "StageResult",
    "PipelineResult",
    "build_default_pipeline",
]
