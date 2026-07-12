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
        ma20 = 0.0
        ma50 = 0.0
        trend_5d = ""

        if technical is not None:
            # 尝试从 technical 提取
            ma20 = _data_attr(technical, "ma20") or 0
            ma50 = _data_attr(technical, "ma50") or 0
            price = _data_attr(technical, "price") or 0
            if price and ma20:
                price_vs_ma20 = (price - ma20) / ma20
            # atr_pct: SymbolContext 有原生 atr_pct(已是百分比); TechnicalData 只有 atr_14(绝对值),
            # 需除以 price 转换为百分比 (如 ATR=3.5, price=150 → 0.0233)
            _atr_pct_native = _data_attr(technical, "atr_pct")
            if _atr_pct_native is not None:
                atr_pct = _atr_pct_native
            else:
                _atr_abs = _data_attr(technical, "atr_14") or 0.0
                _price_for_atr = _data_attr(technical, "price") or 0
                atr_pct = (_atr_abs / _price_for_atr) if (_atr_abs and _price_for_atr) else 0.0
            # TechnicalData 用 volume_5d_avg_ratio, SymbolContext 用 volume_ratio
            volume_ratio = _data_attr(technical, "volume_ratio") or _data_attr(technical, "volume_5d_avg_ratio") or 1.0
            rsi_14 = _data_attr(technical, "rsi_14") or _data_attr(technical, "rsi") or 50.0
            macd_signal = _data_attr(technical, "macd_signal") or "neutral"
            trend_5d = _data_attr(technical, "trend_5d") or ""

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
            "ma20": ma20,
            "ma50": ma50,
            "trend_5d": trend_5d,
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
        """历史数据不足时，使用趋势强度规则兜底。

        设计逻辑 (2026-07-08):
          数据显示 T3(3天) 胜率最优 (BUY 55%, SHORT 60%), T1/T5 均回落。
          6-12h 持仓是利润甜区 (avg +$131, 70%WR), <2h 被扫损是亏损主因。
          → 强趋势标的选 long (波段, max_hold=3天, 宽SL让趋势发展)
          → 高波动/震荡/无趋势 选 short (日内, 快进快出)

        趋势强度评分 (trend_strength) 范围 0-6:
          momentum (price vs MA20): |偏离|>3% → +2, >1.5% → +1
          均线排列: MA20>MA50 同向 → +1 (BUY), MA20<MA50 同向 → +1 (SHORT)
          MACD: 金叉→+1 (BUY), 死叉→+1 (SHORT)
          volume_ratio >1.3: +1
          trend_5d: 明确方向 → +1
        """
        short_score = 0.0
        long_score = 0.0
        matched_rules = []

        direction = features.get("direction", "BUY")
        is_buy = direction == "BUY"

        # ── 规则 1: 财报临近 → 短线 (事件风险, 不论趋势) ──
        if features.get("earnings_days") is not None and features["earnings_days"] <= 3:
            short_score += 1.5
            matched_rules.append("earnings_near: short +1.5")

        # ── 规则 2: 高波动 → 短线 (VIX>22 或 ATR%>4%, 波段持有风控成本过高) ──
        vix = features.get("vix", 20)
        atr_pct = features.get("atr_pct", 0)
        if vix > 22 or atr_pct > 0.04:
            short_score += 1.0
            matched_rules.append(f"high_volatility: short +1.0 (vix={vix}, atr_pct={atr_pct:.3f})")

        # ── 规则 3: RSI 极端 → 短线 (均值回归概率高, 趋势延续概率低) ──
        rsi = features.get("rsi_14", 50)
        if rsi > 70 or rsi < 30:
            short_score += 0.5
            matched_rules.append(f"rsi_extreme: short +0.5 (rsi={rsi})")

        # ── 规则 4: 趋势强度评分 → 长线候选 ──
        # 核心逻辑: 强趋势标的适合波段持有 (T3 数据支持)
        trend_strength = 0

        # 4a: momentum (price vs MA20 偏离度)
        price_vs_ma20 = features.get("price_vs_ma20", 0)
        abs_mom = abs(price_vs_ma20)
        if abs_mom > 0.03:
            trend_strength += 2
            matched_rules.append(f"momentum_strong: +2 (price_vs_ma20={price_vs_ma20:+.3f})")
        elif abs_mom > 0.015:
            trend_strength += 1
            matched_rules.append(f"momentum_moderate: +1 (price_vs_ma20={price_vs_ma20:+.3f})")

        # 4b: 均线排列 (MA20 vs MA50 方向一致性)
        ma20 = features.get("ma20", 0) or 0
        ma50 = features.get("ma50", 0) or 0
        if ma20 > 0 and ma50 > 0:
            if is_buy and ma20 > ma50:
                trend_strength += 1
                matched_rules.append("ma_alignment_bullish: +1 (MA20>MA50)")
            elif not is_buy and ma20 < ma50:
                trend_strength += 1
                matched_rules.append("ma_alignment_bearish: +1 (MA20<MA50)")

        # 4c: MACD 方向一致
        macd_signal = features.get("macd_signal", "neutral")
        if is_buy and macd_signal == "golden_cross":
            trend_strength += 1
            matched_rules.append("macd_golden_cross: +1")
        elif not is_buy and macd_signal == "dead_cross":
            trend_strength += 1
            matched_rules.append("macd_dead_cross: +1")

        # 4d: 放量确认
        volume_ratio = features.get("volume_ratio", 1.0)
        if volume_ratio > 1.3:
            trend_strength += 1
            matched_rules.append(f"volume_confirm: +1 (vr={volume_ratio:.2f})")

        # 4e: 5日趋势方向
        trend_5d = features.get("trend_5d", "")
        if is_buy and trend_5d == "up":
            trend_strength += 1
            matched_rules.append("trend_5d_up: +1")
        elif not is_buy and trend_5d == "down":
            trend_strength += 1
            matched_rules.append("trend_5d_down: +1")

        # 趋势强度 → 长线候选, 但高波动一票否决 (波段持有风控成本过高)
        # 数据支持: T3 WR 55-60% > T1 39-40%, 但 ATR>4% 时波段回撤超SL概率高
        _high_vol = vix > 22 or atr_pct > 0.04
        if _high_vol:
            if trend_strength >= 3:
                long_score += 0.5
                matched_rules.append(f"strong_trend_but_high_vol: long +0.5 (ts={trend_strength}, blocked by vol)")
            else:
                matched_rules.append(f"no_trend_high_vol: short (ts={trend_strength})")
        elif trend_strength >= 4 and vix < 20:
            long_score += 2.0
            matched_rules.append(f"strong_trend_low_vol: long +2.0 (trend_strength={trend_strength})")
        elif trend_strength >= 3:
            long_score += 1.5
            matched_rules.append(f"strong_trend: long +1.5 (trend_strength={trend_strength})")
        elif trend_strength >= 2:
            long_score += 0.5
            matched_rules.append(f"moderate_trend: long +0.5 (trend_strength={trend_strength})")

        # ── 规则 5: 基本面/资金面因子 → 长线加成（方向感知）──
        fundamental_long_buy = {"insider_cluster_buy", "insider_net_buy"}
        if is_buy and any(f in fundamental_long_buy for f in features.get("factors", [])):
            long_score += 0.5
            matched_rules.append("fundamental_factor_buy: long +0.5")

        # ── 规则 6: 方向-因子共振 ──
        # SHORT + 放量 → 恐慌性抛售，倾向短线
        if not is_buy and "volume_expansion" in features.get("factors", []):
            short_score += 0.5
            matched_rules.append("volume_expansion_short: short +0.5")
        # BUY + 放量 → 资金流入，倾向长线
        if is_buy and "volume_expansion" in features.get("factors", []):
            long_score += 0.5
            matched_rules.append("volume_expansion_buy: long +0.5")

        # ── 决策: long_score > short_score * 1.1 时选 long ──
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
                "trend_strength": trend_strength,
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
        vol_state = "high" if (features.get("atr_pct", 0) > 0.04 or features.get("vix", 0) > 22) else "low"

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
