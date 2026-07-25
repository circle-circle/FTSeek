"""
Playbook Weight Adjuster — 自动权重调整 (spec 6.4)

基于因子分析结果自动调整 PlaybookScreener 权重:
  - 正收益因子 (positive): 权重 +1 (上限 5)
  - 负收益因子 (negative): 权重 -1 (下限 -5)
  - 调整后记录到 weight_adjustments 历史

Usage:
  from app.playbook.weight_adjuster import WeightAdjuster

  adjuster = WeightAdjuster(config_path="config/playbook_screening.yaml")
  adjustments = adjuster.adjust(factor_report)
"""
import logging
import yaml
from typing import Dict, List, Any, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/playbook_screening.yaml"
WEIGHT_UPPER_BOUND = 5
WEIGHT_LOWER_BOUND = -5


class WeightAdjuster:
    """Playbook Screener 权重自动调整"""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = Path(config_path)

    def adjust(self, factor_report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        根据因子分析报告调整权重

        Args:
            factor_report: FactorAnalyzer.generate_factor_report() 的输出

        Returns:
            调整记录列表
        """
        config = self._load_config()
        if not config:
            return []

        factors = config.get("screening_factors", {})
        adjustments = []

        # 正收益因子 → +1
        for f in factor_report.get("positive_singles", []):
            factor_key = f.get("factor_key", "")
            if factor_key in factors:
                current_weight = factors[factor_key].get("weight", 0)
                if current_weight < WEIGHT_UPPER_BOUND:
                    new_weight = current_weight + 1
                    factors[factor_key]["weight"] = new_weight
                    adjustments.append({
                        "factor": factor_key,
                        "old_weight": current_weight,
                        "new_weight": new_weight,
                        "delta": +1,
                        "reason": f"正收益因子: 贝叶斯胜率{f.get('bayesian_win_rate', 0):.0%}, "
                                  f"CI [{f.get('bayesian_lower', 0):.0%}-{f.get('bayesian_upper', 0):.0%}]",
                    })

        # 负收益因子 → -1
        for f in factor_report.get("negative_singles", []):
            factor_key = f.get("factor_key", "")
            if factor_key in factors:
                current_weight = factors[factor_key].get("weight", 0)
                if current_weight > WEIGHT_LOWER_BOUND:
                    new_weight = current_weight - 1
                    factors[factor_key]["weight"] = new_weight
                    adjustments.append({
                        "factor": factor_key,
                        "old_weight": current_weight,
                        "new_weight": new_weight,
                        "delta": -1,
                        "reason": f"负收益因子: 贝叶斯胜率{f.get('bayesian_win_rate', 0):.0%}",
                    })

        if adjustments:
            config["screening_factors"] = factors

            # 记录调整历史
            if "weight_adjustments" not in config:
                config["weight_adjustments"] = []

            from app.utils.timezone import now_utc
            now = now_utc().strftime("%Y-%m-%d")
            for adj in adjustments:
                config["weight_adjustments"].append({
                    "date": now,
                    "factor": adj["factor"],
                    "delta": adj["delta"],
                    "reason": adj["reason"],
                })

            self._save_config(config)
            logger.info(f"Adjusted {len(adjustments)} screening weights")

        return adjustments

    def get_current_weights(self) -> Dict[str, int]:
        """获取当前所有权重"""
        config = self._load_config()
        if not config:
            return {}
        factors = config.get("screening_factors", {})
        return {k: v.get("weight", 0) for k, v in factors.items()}

    def get_adjustment_history(self, limit: int = 20) -> List[Dict]:
        """获取最近的调整历史"""
        config = self._load_config()
        if not config:
            return []
        history = config.get("weight_adjustments", [])
        return history[-limit:]

    def _load_config(self) -> Optional[Dict]:
        """加载 YAML 配置"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
        return None

    def _save_config(self, config: Dict):
        """保存 YAML 配置"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
