"""
诊断 Overlay 框架 V2 — FTSeek-US 非交易路径诊断模块

基于 Vibe-Trading 源码深度适配:
  1. ShadowAccount V2 — KMeans 策略提取 + 五因子归因分解
  2. StatisticalValidator V2 — Monte Carlo + Bootstrap + Walk-Forward + K-fold CV
  3. GeopoliticalReasoner — 地缘事件→行业→个股推理 (规则引擎)

统一调度: 通过 scheduler 在盘后/非交易时段异步触发
零侵入: 不修改任何现有交易管线代码
"""

from typing import Dict

from app.diagnostics.base import BaseDiagnostic, DiagnosticResult
from app.diagnostics.shadow_account import ShadowAccountDiagnostic, create_shadow_diagnostic


# 2026-07-26 减法R2: statistical_validator/geopolitical_reasoner 零调用方已删 (git revert 恢复)
DIAGNOSTIC_REGISTRY: Dict[str, callable] = {
    "shadow_account": create_shadow_diagnostic,
}


def get_diagnostic(name: str) -> BaseDiagnostic:
    """从注册表获取诊断模块"""
    factory = DIAGNOSTIC_REGISTRY.get(name)
    if factory is None:
        available = ", ".join(DIAGNOSTIC_REGISTRY.keys())
        raise ValueError(f"未知诊断模块 '{name}'。可用: {available}")
    return factory()


def list_diagnostics() -> Dict[str, str]:
    """列出所有可用诊断模块"""
    return {name: factory().name for name, factory in DIAGNOSTIC_REGISTRY.items()}


def run_all_diagnostics(schedule: str = "post_market", **kwargs) -> Dict[str, DiagnosticResult]:
    """
    批量运行指定调度时段的所有诊断模块

    Args:
        schedule: 调度时段 ("pre_market" / "post_market" / "midday" / "on_demand")
        **kwargs: 传递给各模块的参数 (db_path, news_items, trade_pnl_series 等)

    Returns:
        {module_name: DiagnosticResult, ...}
    """
    results = {}
    for name, factory in DIAGNOSTIC_REGISTRY.items():
        diag = factory()
        if diag.schedule_hint == schedule or schedule == "all":
            try:
                results[name] = diag.execute(**kwargs)
            except Exception as e:
                results[name] = DiagnosticResult(
                    module_name=name,
                    status="error",
                    data={"error": str(e)},
                    summary=f"执行失败: {e}",
                )
    return results
