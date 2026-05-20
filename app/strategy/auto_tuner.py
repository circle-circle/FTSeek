"""
AutoTuner 占位模块 (2026-05-02 清理)

原模块使用 random.uniform() 生成假数据，从未接入真实回测引擎。
此文件保留为安全占位，防止 DI Container 启动失败。

如需启用自动参数优化，请接入 app/backtest/ 真实回测引擎。
"""

import logging

logger = logging.getLogger("ftseek.strategy.auto_tuner")


class AutoTuner:
    """
    AutoTuner 占位实现 — 所有方法返回无操作结果。
    ⚠️ NOT_FOR_PRODUCTION: 尚未接入真实回测引擎。
    """
    
    def __init__(self, *args, **kwargs):
        logger.debug("[AutoTuner] 占位初始化 (未接入真实回测)")
    
    def tune(self, *args, **kwargs):
        """返回空结果，不执行任何优化"""
        logger.warning("[AutoTuner] tune() 被调用但未接入真实回测引擎 — 返回空结果")
        return {"status": "disabled", "message": "AutoTuner not connected to real backtest engine"}
    
    def get_recommendations(self, *args, **kwargs):
        """返回空推荐列表"""
        return []
