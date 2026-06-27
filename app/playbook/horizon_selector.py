"""
Playbook Horizon Selector — 个股阶段驱动的短线/长线决策

基于标的阶段特征、市场环境、信号质量，
为每个候选信号选择 short 或 long horizon。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.playbook.db import PlaybookDB
from app.playbook.horizon_return_model import HorizonReturnModel

logger = logging.getLogger(__name__)


DEFAULT_CONFIG = {
    "horizon_selector": {
        "enabled": True,
        "min_samples_for_model": 5,
        "margin_threshold": 0.1,
        "default_horizon": "short",
    },
    "short_horizon": {
        "rr_ratio": 1.0,
        "fixed_stop_loss_pct": 2.0,
        "max_hold_days": 1,
        "skip_pre_close_if_profit": False,
        "time_exit_hours": 2.0,
    },
    "long_horizon": {
        "rr_ratio": 2.5,
        "fixed_stop_loss_pct": 2.0,
        "max_hold_days": 5,
        "skip_pre_close_if_profit": True,
        "overnight_gap_stop_pct": 3.0,
        "time_exit_hours": 999,
    },
}


@dataclass
class HorizonDecision:
    """Horizon 选择结果"""
    horizon: str                       # "short" or "long"
    short_score: float                 # 短线综合得分
    long_score: float                  # 长线综合得分
    confidence: float                  # |short_score - long_score|
    reasoning: Dict[str, Any] = field(default_factory=dict)
    feature_weights: Dict[str, float] = field(default_factory=dict)


class HorizonSelector:
    """
    个股阶段驱动的 Horizon 选择器。

    设计原则:
      - 不预设方向与周期的关系
      - 基于标的阶段特征 + 历史收益估计 + 默认规则兜底
      - 输出可解释、可追踪的决策
    """

    def __init__(self, db: PlaybookDB = None, config_path: str = None):
        self.db = db or PlaybookDB()
        self.db.ensure_tables()
        self.config = self._load_config(config_path)
        self.return_model = HorizonReturnModel(self.db)

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """加载配置，失败时使用默认"""
        if config_path is None:
            config_path = "config/playbook_horizon.yaml"

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if cfg:
                    return cfg
        except Exception as e:
            logger.warning(f"[HorizonSelector] 读取 {config_path} 失败: {e}, 使用默认配置")

        # fallback: 使用默认配置
        return DEFAULT_CONFIG

    def select(self, screen_result, regime_ctx=None) -> HorizonDecision:
        """
        为单个候选信号选择 horizon。

        Args:
            screen_result: ScreenResult dataclass 或 dict (含 symbol, factors, data)
            regime_ctx: RegimeContext dataclass 或 dict

        Returns:
            HorizonDecision
        """
        features = self._extract_features(screen_result, regime_ctx)

        # 如果 horizon_strategy 被全局禁用，直接返回 short（保留可切换能力）
        if not self._is_enabled():
            return HorizonDecision(
                horizon="short",
                short_score=1.0,
                long_score=0.0,
                confidence=1.0,
                reasoning={"source": "disabled", "note": "horizon_strategy.enabled=false"},
                feature_weights={},
            )

        # 1. 尝试用历史收益模型
        decision = self._score_with_model(features)

        # 2. 模型数据不足时 fallback 到默认规则
        if decision is None:
            decision = self._fallback_score(features)

        # 3. 记录决策日志
        self._log_decision(features["symbol"], decision)
        return decision

    def _is_enabled(self) -> bool:
        """读取配置判断 horizon 策略是否启用"""
        return self.config.get("horizon_selector", {}).get("enabled", True)

    def _extract_features(self, screen_result, regime_ctx) -> Dict[str, Any]:
        """从 ScreenResult + RegimeContext 提取特征"""
        # 兼容 dataclass 和 dict
        def _get(obj, attr, default=None):
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return getattr(obj, attr, default)

        symbol = _get(screen_result, "symbol", "")
        data = _get(screen_result, "data")
        factors = _get(screen_result, "factors", {}) or {}
        if isinstance(factors, dict):
            factor_names = list(factors.keys())
        else:
            factor_names = list(factors) if factors else []

        # 提取 data 属性
        def _data_attr(data_obj, *path, default=None):
            if data_obj is None:
                return default
            try:
                cur = data_obj
                for p in path:
                    if isinstance(cur, dict):
                        cur = cur.get(p, default)
                    else:
                        cur = getattr(cur, p, default)
                    if cur is None:
                        return default
                return cur
            except Exception:
                return default

        technical = _data_attr(data, "technical")

        # 标的特征
        price_vs_ma20 = 0.0
        atr_pct = 0.0
        volume_ratio = 1.0
        rsi_14 = 50.0
        macd_signal = "neutral"

        if technical is not None:
            # 尝试从 technical 提取
            ma20 = _data_attr(technical, "ma20") or 0
            price = _data_attr(technical, "price") or 0
            if price and ma20:
                price_vs_ma20 = (price - ma20) / ma20
            atr_pct = _data_attr(technical, "atr_pct") or _data_attr(technical, "atr_14") or 0.0
            volume_ratio = _data_attr(technical, "volume_ratio") or _data_attr(technical, "volume_5d_avg_ratio") or 1.0
            rsi_14 = _data_attr(technical, "rsi_14") or 50.0
            macd_signal = _data_attr(technical, "macd_signal") or "neutral"

        # 市场环境
        vix = 20.0
        market_regime = "CAUTIOUS"
        spy_trend = "neutral"
        if regime_ctx is not None:
            vix = _get(regime_ctx, "vix", 20.0) or 20.0
            market_regime = (_get(regime_ctx, "regime", "CAUTIOUS") or "CAUTIOUS")
            if not isinstance(market_regime, str):
                market_regime = getattr(market_regime, "name", "CAUTIOUS")
            spy_trend = _get(regime_ctx, "spy_trend_at_generation") or "neutral"

        # 财报距离
        earnings_days = None
        if data is not None:
            earnings_days = _data_attr(data, "earnings", "days_until") or _data_attr(data, "earnings_days")

        # 信号特征
        direction = _get(screen_result, "direction") or "BUY"
        confidence = _get(screen_result, "confidence") or 0

        return {
            "symbol": symbol,
            "direction": direction,
            "factors": factor_names,
            "factor_count": len(factor_names),
            "confidence": confidence,
            "price_vs_ma20": price_vs_ma20,
            "atr_pct": atr_pct,
            "volume_ratio": volume_ratio,
            "rsi_14": rsi_14,
            "macd_signal": macd_signal,
            "vix": vix,
            "market_regime": market_regime,
            "spy_trend": spy_trend,
            "earnings_days": earnings_days,
        }

    def _score_with_model(self, features: Dict[str, Any]) -> Optional[HorizonDecision]:
        """用历史收益模型打分"""
        factor_names = features.get("factors", [])
        if not factor_names:
            return None

        # 分别生成 short / long 的 feature_key，避免把 :short 后缀的 key
        # 当成 long 来查（否则 long 统计永远为空）
        short_feature_keys = self._build_feature_keys(features, horizon="short")
        long_feature_keys = self._build_feature_keys(features, horizon="long")

        short_contributions = []
        long_contributions = []
        feature_weights = {}

        # short
        for key in short_feature_keys:
            short_stat = self.return_model.get(key, "short")
            if short_stat is not None and short_stat.total_trades >= self._cfg("min_samples_for_model"):
                score = short_stat.bayesian_win_rate * max(short_stat.avg_return, 0)
                short_contributions.append(score)
                feature_weights[f"{key}:short"] = score

        # long
        for key in long_feature_keys:
            long_stat = self.return_model.get(key, "long")
            if long_stat is not None and long_stat.total_trades >= self._cfg("min_samples_for_model"):
                score = long_stat.bayesian_win_rate * max(long_stat.avg_return, 0)
                long_contributions.append(score)
                feature_weights[f"{key}:long"] = score

        if not short_contributions and not long_contributions:
            return None

        short_score = sum(short_contributions) / len(short_contributions) if short_contributions else 0.0
        long_score = sum(long_contributions) / len(long_contributions) if long_contributions else 0.0

        margin = self._cfg("margin_threshold")
        chosen = self._choose(short_score, long_score, margin)

        return HorizonDecision(
            horizon=chosen,
            short_score=round(short_score, 6),
            long_score=round(long_score, 6),
            confidence=round(abs(short_score - long_score), 6),
            reasoning={
                "source": "historical_return_model",
                "short_feature_keys": short_feature_keys,
                "long_feature_keys": long_feature_keys,
                "vix_state": self._vix_state(features["vix"]),
                "market_regime": features["market_regime"],
            },
            feature_weights=feature_weights,
        )

    def _fallback_score(self, features: Dict[str, Any]) -> HorizonDecision:
        """历史数据不足时，使用默认规则兜底"""
        short_score = 0.0
        long_score = 0.0
        matched_rules = []

        # 规则 1: 财报临近 → 短线
        if features.get("earnings_days") is not None and features["earnings_days"] <= 3:
            short_score += 1.0
            matched_rules.append("earnings_near: short +1.0")

        # 规则 2: 高波动/VIX 高 → 短线
        if features.get("atr_pct", 0) > 0.15 or features.get("vix", 0) > 22:
            short_score += 1.0
            matched_rules.append("high_volatility: short +1.0")

        # 规则 3: RSI 极端 → 短线
        rsi = features.get("rsi_14", 50)
        if rsi > 70 or rsi < 30:
            short_score += 0.5
            matched_rules.append("rsi_extreme: short +0.5")

        # 方向提取（后续规则需要）
        direction = features.get("direction", "BUY")

        # 规则 4: 强趋势 + 放量 + 低波动 → 长线 (仅 BUY, 避免给 SHORT 多头信号加分)
        if (direction == "BUY" and
            features.get("volume_ratio", 1.0) > 1.2 and
            abs(features.get("price_vs_ma20", 0)) > 0.02 and
            features.get("vix", 20) < 20):
            long_score += 1.0
            matched_rules.append("trend_volume_lowvol: long +1.0")

        # 规则 5: 基本面/资金面因子 → 长线（方向感知）
        fundamental_long_buy = {"insider_cluster_buy", "insider_net_buy"}
        if direction == "BUY" and any(f in fundamental_long_buy for f in features.get("factors", [])):
            long_score += 0.5
            matched_rules.append("fundamental_factor_buy: long +0.5")
        # SHORT + 放量 → 恐慌性抛售，倾向短线
        if direction == "SELL_SHORT" and "volume_expansion" in features.get("factors", []):
            short_score += 0.5
            matched_rules.append("volume_expansion_short: short +0.5")
        # BUY + 放量 → 资金流入，倾向长线
        if direction == "BUY" and "volume_expansion" in features.get("factors", []):
            long_score += 0.5
            matched_rules.append("volume_expansion_buy: long +0.5")

        # 规则 6: MACD 金叉 + 价格在 MA20 上方 → 长线 (仅 BUY, 空头不因此加分)
        if (direction == "BUY" and
            features.get("macd_signal") == "golden_cross" and
            features.get("price_vs_ma20", 0) > 0):
            long_score += 0.5
            matched_rules.append("macd_golden_cross_above_ma20: long +0.5")

        # 默认短线
        if long_score > short_score * 1.1:
            chosen = "long"
        else:
            chosen = "short"

        return HorizonDecision(
            horizon=chosen,
            short_score=round(short_score, 6),
            long_score=round(long_score, 6),
            confidence=round(abs(short_score - long_score), 6),
            reasoning={
                "source": "fallback_rules",
                "matched_rules": matched_rules,
                "vix_state": self._vix_state(features["vix"]),
                "market_regime": features["market_regime"],
            },
            feature_weights={},
        )

    def _build_feature_keys(self, features: Dict[str, Any], horizon: str) -> List[str]:
        """构建特征 key 列表"""
        factor_names = features.get("factors", [])
        vix_state = self._vix_state(features["vix"])
        regime = str(features.get("market_regime", "UNKNOWN")).replace(" ", "_")
        trend_state = "up" if features.get("price_vs_ma20", 0) > 0 else "down"
        vol_state = "high" if (features.get("atr_pct", 0) > 0.15 or features.get("vix", 0) > 22) else "low"

        keys = []
        for factor in factor_names:
            # 单因子
            keys.append(f"{factor}:{horizon}")
            # 市场环境
            keys.append(f"{factor}:vix_{vix_state}:regime_{regime}:{horizon}")
            # 标的状态
            keys.append(f"{factor}:trend_{trend_state}:vol_{vol_state}:{horizon}")

        # 因子对
        if len(factor_names) >= 2:
            sorted_conds = sorted(factor_names)
            for i in range(len(sorted_conds)):
                for j in range(i + 1, len(sorted_conds)):
                    keys.append(f"{sorted_conds[i]}+{sorted_conds[j]}:{horizon}")

        return keys

    @staticmethod
    def _choose(short_score: float, long_score: float, margin: float) -> str:
        """根据得分选择 horizon"""
        if long_score > short_score * (1 + margin):
            return "long"
        elif short_score > long_score * (1 + margin):
            return "short"
        # 接近时默认短线
        return "short"

    @staticmethod
    def _vix_state(vix: float) -> str:
        if vix < 18:
            return "low"
        elif vix < 22:
            return "normal"
        elif vix < 28:
            return "high"
        return "extreme"

    def _log_decision(self, symbol: str, decision: HorizonDecision):
        """记录 horizon 决策日志"""
        logger.info(
            f"[HorizonSelector] {symbol} horizon={decision.horizon} "
            f"short={decision.short_score:.4f} long={decision.long_score:.4f} "
            f"confidence={decision.confidence:.4f}"
        )

    def _cfg(self, key: str, default=None):
        """读取配置"""
        return self.config.get("horizon_selector", {}).get(key, default)

    def get_horizon_config(self, horizon: str) -> Dict[str, Any]:
        """获取指定 horizon 的配置"""
        return self.config.get(f"{horizon}_horizon", DEFAULT_CONFIG.get(f"{horizon}_horizon", {}))
