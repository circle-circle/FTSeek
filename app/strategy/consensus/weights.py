"""
权重计算模块 — 混合权重/策略权重/市场状态权重

从 consensus_engine.py 拆出：
- _calc_weight() — 单策略权重计算
- _get_hybrid_weight() — 混合权重
- 权重引擎集成
- 市场状态权重调整
"""

import logging
import os
from typing import Optional, Dict, Any, Tuple

from app.core.config_loader import cfg

logger = logging.getLogger("ftseek.consensus.weights")


class WeightCalculator:
    """
    权重计算器
    
    职责：
    1. 基准权重读取
    2. 市场状态权重调整
    3. 混合权重计算
    4. 权重引擎集成 (外部 Python 文件)
    """
    
    def __init__(self):
        # 基准权重 (从 unified_config.yaml 读取)
        self._base_weights = cfg.get("strategy.weights", {})
        
        # 权重引擎路径 (可选的外部 Python 文件)
        self._engine_path = cfg.get("strategy.weight_engine_path", "")
        self._engine = None
        self._init_engine()
    
    def _init_engine(self):
        """初始化外部权重引擎"""
        if not self._engine_path or not os.path.exists(self._engine_path):
            logger.debug(f"权重引擎未找到: {self._engine_path}")
            return
        
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("weight_engine", self._engine_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            if hasattr(module, 'WeightEngine'):
                self._engine = module.WeightEngine()
                logger.info(f"权重引擎已加载: {self._engine_path}")
            else:
                logger.warning(f"权重引擎未定义 WeightEngine 类")
        except Exception as e:
            logger.error(f"权重引擎加载失败: {e}")
    
    def calc_weight(self, strategy: str, regime: str = "default",
                    code: str = None) -> Tuple[float, Dict[str, Any]]:
        """
        计算单策略权重
        
        Args:
            strategy: 策略名称
            regime: 市场状态 (trending/ranging/volatile)
            code: 股票代码
        
        Returns:
            (weight, details) — 权重和明细
        """
        details = {"base": 0.0, "regime_adjust": 0.0, "engine_adjust": 0.0}
        
        # 1. 基准权重
        base = self._base_weights.get(strategy, 0.0)
        details["base"] = base
        
        # 2. 市场状态调整
        regime_adjust = self._get_regime_adjustment(strategy, regime)
        details["regime_adjust"] = regime_adjust
        
        # 3. 权重引擎调整 (如果有)
        engine_adjust = 0.0
        if self._engine and hasattr(self._engine, 'get_adjustment'):
            try:
                engine_adjust = self._engine.get_adjustment(strategy, code)
                details["engine_adjust"] = engine_adjust
            except Exception as e:
                logger.debug(f"权重引擎调整失败: {e}")
        
        final_weight = base + regime_adjust + engine_adjust
        return max(0.0, final_weight), details
    
    def _get_regime_adjustment(self, strategy: str, regime: str) -> float:
        """获取市场状态调整系数"""
        # 从配置读取
        regime_params = cfg.get(f"dynamic_exit_params.{regime}", {})
        if not regime_params:
            return 0.0
        
        # 不同策略在不同市场状态下的调整
        strategy_adjustments = {
            "confluence_v2": {"trending": 0.3, "ranging": -0.2, "volatile": 0.1},
            "supertrend": {"trending": 0.4, "ranging": -0.3, "volatile": 0.2},
            "sentiment_ai": {"trending": 0.1, "ranging": 0.2, "volatile": 0.3},
            "event_driven": {"trending": 0.0, "ranging": 0.1, "volatile": 0.5},
        }
        
        adjustments = strategy_adjustments.get(strategy, {})
        return adjustments.get(regime, 0.0)
    
    def get_hybrid_weight(self, strategy: str, regime: str,
                          news_factor: Optional[Dict] = None,
                          hk_us_factor: Optional[Dict] = None) -> float:
        """
        计算混合权重 (技术面 + 新闻面 + 跨市场)
        
        Args:
            strategy: 策略名称
            regime: 市场状态
            news_factor: 新闻因子 (可选)
            hk_us_factor: 跨市场因子 (可选)
        
        Returns:
            混合权重
        """
        # 技术面权重
        tech_weight, _ = self.calc_weight(strategy, regime)
        
        # 新闻面权重
        news_weight = 0.0
        if news_factor:
            news_weight = news_factor.get("weight", 0.0) * news_factor.get("sentiment", 0.0)
        
        # 跨市场权重
        cross_weight = 0.0
        if hk_us_factor:
            cross_weight = hk_us_factor.get("weight", 0.0)
        
        # 混合
        total = tech_weight + news_weight + cross_weight
        return max(0.0, total)
    
    def get_base_weights(self) -> Dict[str, float]:
        """获取基准权重"""
        return dict(self._base_weights)
    
    def update_base_weights(self, weights: Dict[str, float]):
        """更新基准权重"""
        self._base_weights.update(weights)
    
    @property
    def has_engine(self) -> bool:
        return self._engine is not None
