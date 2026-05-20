#!/usr/bin/env python3
"""
跨市场历史事件统计追踪器

统计每个美港股配对的：
1. 事件命中率 - 美股大涨/大跌后港股是否跟随（用盈利交易作代理）
2. 响应速度 - 美股变动到港股响应的平均时间（用持仓时间作代理）

数据来源：/opt/ftseek/FTSeek/data/trade_records/{YYYY-MM-DD}.json
筛选策略为 cross_market_arbitrage 的记录作为有效跨市场样本。
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek.strategy.cross_market_history")
logger.propagate = False

# 交易记录目录
TRADE_RECORDS_DIR = Path("/opt/ftseek/FTSeek/data/trade_records")

# 默认最大持仓时间 (分钟), 对应 risk_params.max_holding_minutes
_DEFAULT_MAX_HOLDING_MINUTES = 30.0

# 配置 key 常量
_CFG_KEY = "cross_market"
_CFG_HISTORY_KEY = "history"


def _get_history_config() -> Dict:
    """从 unified_config.yaml cross_market.history 节读取配置，失败时返回默认值"""
    defaults = {
        "lookback_days": 30,
        "min_samples": 5,
        "prior_hit_rate": 0.5,
        "cache_ttl_hours": 24,
        "max_holding_minutes": 30,
    }
    try:
        from config.config_center import get_config_center
        cc = get_config_center()
        cm_cfg = cc.get(_CFG_KEY)
        if cm_cfg is None:
            return defaults
        if hasattr(cm_cfg, "to_dict"):
            cm_cfg = cm_cfg.to_dict()
        if not isinstance(cm_cfg, dict):
            return defaults
        hist_cfg = cm_cfg.get(_CFG_HISTORY_KEY, {})
        if hasattr(hist_cfg, "to_dict"):
            hist_cfg = hist_cfg.to_dict()
        if not isinstance(hist_cfg, dict):
            return defaults
        merged = dict(defaults)
        merged.update({k: v for k, v in hist_cfg.items() if k in defaults})
        return merged
    except Exception as e:
        logger.debug(f"[CrossMarketHistoryTracker] 读取配置失败，使用默认值: {e}")
        return defaults


class CrossMarketHistoryTracker:
    """跨市场历史事件统计追踪器

    统计每个美港股配对的：
    1. 事件命中率 - 美股大涨/大跌后港股是否跟随
    2. 响应速度 - 美股变动到港股响应的平均时间

    缓存策略：
    - 每日仅加载一次历史数据（按 cache_ttl_hours 控制）
    - 手动调用 refresh() 可强制重新加载
    """

    def __init__(self):
        # 配置
        self._cfg: Dict = _get_history_config()
        self._lookback_days: int = int(self._cfg.get("lookback_days", 30))
        self._min_samples: int = int(self._cfg.get("min_samples", 5))
        self._prior_hit_rate: float = float(self._cfg.get("prior_hit_rate", 0.5))
        self._cache_ttl_hours: float = float(self._cfg.get("cache_ttl_hours", 24))
        self._max_holding_minutes: float = float(
            self._cfg.get("max_holding_minutes", _DEFAULT_MAX_HOLDING_MINUTES)
        )

        # 缓存：{(hk_code, us_code): {'hit_count': int, 'total': int, 'holding_minutes': List[float]}}
        self._pair_stats: Dict[Tuple[str, str], Dict] = {}
        # 按事件强度分层：{(hk_code, us_code, impact): {'hit_count': int, 'total': int}}
        self._pair_impact_stats: Dict[Tuple[str, str, str], Dict] = {}

        self._cache_loaded_at: Optional[datetime] = None
        self._load_history()

    # ------------------------------------------------------------------ #
    #  公开 API
    # ------------------------------------------------------------------ #

    def get_event_hit_rate(
        self,
        hk_code: str,
        us_code: str,
        event_impact: str = "normal",
        fallback_corr: float = 0.7,
    ) -> float:
        """获取事件命中率（0.0 ~ 1.0）

        Args:
            hk_code: 港股代码，如 "00700.HK"
            us_code: 美股代码，如 "TCEHY"
            event_impact: 事件强度 critical/high/normal/low
            fallback_corr: 无数据时的相关系数（用于先验估计）

        Returns:
            命中率评分 0.0 ~ 1.0
        """
        self._maybe_refresh()

        # 先查分层统计
        impact_key = (hk_code, us_code, event_impact)
        impact_stat = self._pair_impact_stats.get(impact_key, {})
        impact_total = impact_stat.get("total", 0)
        impact_hits = impact_stat.get("hit_count", 0)

        # 再查总体统计
        pair_key = (hk_code, us_code)
        total_stat = self._pair_stats.get(pair_key, {})
        total = total_stat.get("total", 0)
        hits = total_stat.get("hit_count", 0)

        # 优先使用分层数据，若分层样本充足；否则回退到总体
        if impact_total >= self._min_samples:
            raw_rate = impact_hits / impact_total
            n = impact_total
        elif total >= self._min_samples:
            raw_rate = hits / total
            n = total
        elif total > 0:
            # 小样本：贝叶斯收缩 (posterior = (hits + α) / (total + α + β))
            # 简化为向先验靠拢
            alpha = self._prior_hit_rate * self._min_samples
            beta = (1 - self._prior_hit_rate) * self._min_samples
            raw_rate = (hits + alpha) / (total + alpha + beta)
            n = total
        else:
            # 无数据：使用相关系数推断先验
            prior = fallback_corr * 0.9
            logger.debug(
                f"[CrossMarketHistoryTracker] 无历史数据 {hk_code}/{us_code} "
                f"event_impact={event_impact}, 使用先验={prior:.3f}"
            )
            return round(min(1.0, prior), 4)

        hit_rate = max(0.0, min(1.0, raw_rate))
        logger.debug(
            f"[CrossMarketHistoryTracker] 命中率 {hk_code}/{us_code} "
            f"impact={event_impact} n={n} rate={hit_rate:.3f}"
        )
        return round(hit_rate, 4)

    def get_response_speed(
        self,
        hk_code: str,
        us_code: str,
        fallback_corr: float = 0.7,
    ) -> float:
        """获取响应速度评分（0.0 ~ 1.0）

        持仓时间越短 = 响应越快 = 评分越高
        评分 = 1.0 - (avg_holding_minutes / max_holding_minutes)

        Args:
            hk_code: 港股代码
            us_code: 美股代码
            fallback_corr: 无数据时的相关系数

        Returns:
            响应速度评分 0.0 ~ 1.0
        """
        self._maybe_refresh()

        pair_key = (hk_code, us_code)
        stat = self._pair_stats.get(pair_key, {})
        holding_minutes: List[float] = stat.get("holding_minutes", [])

        if not holding_minutes:
            # 无数据：使用相关系数作为先验
            prior = fallback_corr * 1.0
            logger.debug(
                f"[CrossMarketHistoryTracker] 无持仓数据 {hk_code}/{us_code}, "
                f"使用先验={prior:.3f}"
            )
            return round(min(1.0, prior), 4)

        avg_holding = sum(holding_minutes) / len(holding_minutes)
        # 归一化：持仓 0 分钟 = 速度 1.0；持仓 max_holding_minutes = 速度 0.0
        speed = 1.0 - (avg_holding / self._max_holding_minutes)
        speed = max(0.0, min(1.0, speed))

        logger.debug(
            f"[CrossMarketHistoryTracker] 响应速度 {hk_code}/{us_code} "
            f"avg_holding={avg_holding:.1f}min speed={speed:.3f}"
        )
        return round(speed, 4)

    def refresh(self):
        """强制重新加载历史数据"""
        logger.info("[CrossMarketHistoryTracker] 手动刷新历史数据")
        self._cache_loaded_at = None
        self._load_history()

    def get_pair_summary(self, hk_code: str, us_code: str) -> Dict:
        """获取指定配对的统计摘要（用于调试/监控）"""
        pair_key = (hk_code, us_code)
        stat = self._pair_stats.get(pair_key, {})
        total = stat.get("total", 0)
        hits = stat.get("hit_count", 0)
        holding = stat.get("holding_minutes", [])
        return {
            "hk_code": hk_code,
            "us_code": us_code,
            "total_trades": total,
            "hit_count": hits,
            "hit_rate": round(hits / total, 4) if total > 0 else None,
            "avg_holding_minutes": round(sum(holding) / len(holding), 2) if holding else None,
            "sample_count": len(holding),
        }

    # ------------------------------------------------------------------ #
    #  内部实现
    # ------------------------------------------------------------------ #

    def _maybe_refresh(self):
        """根据 cache_ttl_hours 决定是否自动刷新"""
        if self._cache_loaded_at is None:
            self._load_history()
            return
        elapsed = (datetime.now() - self._cache_loaded_at).total_seconds() / 3600
        if elapsed >= self._cache_ttl_hours:
            logger.info(
                f"[CrossMarketHistoryTracker] 缓存已过期 ({elapsed:.1f}h > "
                f"{self._cache_ttl_hours}h), 自动刷新"
            )
            self._load_history()

    def _load_history(self):
        """加载过去 lookback_days 天的跨市场交易记录"""
        self._pair_stats = {}
        self._pair_impact_stats = {}

        if not TRADE_RECORDS_DIR.exists():
            logger.warning(
                f"[CrossMarketHistoryTracker] 交易记录目录不存在: {TRADE_RECORDS_DIR}"
            )
            self._cache_loaded_at = datetime.now()
            return

        cutoff = datetime.now() - timedelta(days=self._lookback_days)
        loaded_files = 0
        total_records = 0
        cross_market_records = 0

        for file_path in sorted(TRADE_RECORDS_DIR.glob("*.json")):
            # 文件名格式 YYYY-MM-DD.json
            try:
                file_date = datetime.strptime(file_path.stem, "%Y-%m-%d")
            except ValueError:
                continue
            if file_date < cutoff:
                continue

            records = self._load_file(file_path)
            loaded_files += 1
            total_records += len(records)

            for rec in records:
                # 只统计跨市场套利策略的记录
                if not self._is_cross_market_record(rec):
                    continue
                cross_market_records += 1
                self._process_record(rec)

        self._cache_loaded_at = datetime.now()
        logger.info(
            f"[CrossMarketHistoryTracker] 加载完成: "
            f"文件={loaded_files}, 总记录={total_records}, "
            f"跨市场样本={cross_market_records}, "
            f"配对数={len(self._pair_stats)}"
        )

    def _load_file(self, file_path: Path) -> List[Dict]:
        """加载单个 JSON 文件，容错处理"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            logger.warning(f"[CrossMarketHistoryTracker] 文件格式异常（非列表）: {file_path}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"[CrossMarketHistoryTracker] JSON 解析失败 {file_path}: {e}")
            return []
        except Exception as e:
            logger.warning(f"[CrossMarketHistoryTracker] 读取文件失败 {file_path}: {e}")
            return []

    def _is_cross_market_record(self, rec: Dict) -> bool:
        """判断是否为跨市场套利交易记录"""
        strategy = rec.get("strategy", "") or ""
        strategy_type = rec.get("strategy_type", "") or ""
        decision_reason = rec.get("decision_reason", "") or ""

        # 主要判断：strategy 字段
        if "cross_market" in strategy.lower():
            return True
        # 备用判断：strategy_type 字段（调度器写入时使用）
        if "cross_market" in strategy_type.lower():
            return True
        # 备用：decision_reason 包含 cross_market
        if "cross_market" in decision_reason.lower():
            return True

        # 检查 factor_scores 中的 extra_context 或 arb_type
        factor_scores = rec.get("factor_scores", {}) or {}
        if isinstance(factor_scores, dict):
            if factor_scores.get("arb_type") or factor_scores.get("cross_market"):
                return True
            extra = factor_scores.get("extra_context", {}) or {}
            if isinstance(extra, dict) and extra.get("arb_type"):
                return True

        return False

    def _process_record(self, rec: Dict):
        """处理单条跨市场交易记录，提取配对信息和统计数据"""
        hk_code, us_code = self._extract_pair_codes(rec)
        if not hk_code or not us_code:
            return

        # 判断命中（盈利 = 跟随成功）
        pnl = rec.get("pnl")
        exit_price = rec.get("exit_price")
        is_hit = (
            exit_price is not None
            and pnl is not None
            and float(pnl) > 0
        )

        # 持仓时间（分钟）—— 从 holding_period（天）转换，但跨市场通常是分钟级
        # 尝试从 entry/exit timestamp 计算精确分钟数
        holding_minutes = self._calc_holding_minutes(rec)

        pair_key = (hk_code, us_code)
        if pair_key not in self._pair_stats:
            self._pair_stats[pair_key] = {
                "hit_count": 0,
                "total": 0,
                "holding_minutes": [],
            }
        stat = self._pair_stats[pair_key]

        # 只统计已平仓记录
        if exit_price is not None:
            stat["total"] += 1
            if is_hit:
                stat["hit_count"] += 1
            if holding_minutes is not None:
                stat["holding_minutes"].append(holding_minutes)

        # 分层统计（按 event_impact）
        event_impact = self._extract_event_impact(rec)
        if event_impact and exit_price is not None:
            impact_key = (hk_code, us_code, event_impact)
            if impact_key not in self._pair_impact_stats:
                self._pair_impact_stats[impact_key] = {"hit_count": 0, "total": 0}
            impact_stat = self._pair_impact_stats[impact_key]
            impact_stat["total"] += 1
            if is_hit:
                impact_stat["hit_count"] += 1

    def _extract_pair_codes(self, rec: Dict) -> Tuple[Optional[str], Optional[str]]:
        """从交易记录中提取港股/美股代码"""
        code = rec.get("code", "") or ""

        # 尝试从 factor_scores / extra_context 提取配对信息
        factor_scores = rec.get("factor_scores", {}) or {}
        hk_code = None
        us_code = None

        if isinstance(factor_scores, dict):
            hk_code = factor_scores.get("hk_code") or factor_scores.get("hk")
            us_code = factor_scores.get("us_code") or factor_scores.get("us")
            if not hk_code or not us_code:
                extra = factor_scores.get("extra_context", {}) or {}
                if isinstance(extra, dict):
                    hk_code = hk_code or extra.get("hk_code")
                    us_code = us_code or extra.get("us_code")

        # 根据 code 字段推断：港股代码通常含 .HK
        if not hk_code and not us_code:
            if ".HK" in code.upper():
                hk_code = code
                # 无法推断 us_code，使用空字符串作为通配
                us_code = "UNKNOWN"
            else:
                # 美股代码，无法确定配对
                return None, None

        if not hk_code:
            hk_code = code if ".HK" in code.upper() else None
        if not us_code:
            us_code = "UNKNOWN"

        return hk_code, us_code

    def _calc_holding_minutes(self, rec: Dict) -> Optional[float]:
        """计算持仓时间（分钟）"""
        entry_ts = rec.get("timestamp")
        exit_ts = rec.get("exit_timestamp")

        if entry_ts and exit_ts:
            try:
                entry_dt = datetime.fromisoformat(str(entry_ts)[:19])
                exit_dt = datetime.fromisoformat(str(exit_ts)[:19])
                minutes = (exit_dt - entry_dt).total_seconds() / 60
                # 跨市场套利正常持仓时间 0-30 分钟，过滤异常值
                if 0 <= minutes <= self._max_holding_minutes * 3:
                    return round(minutes, 2)
                return None
            except (ValueError, TypeError):
                pass

        # fallback：holding_period 字段（天为单位）
        holding_days = rec.get("holding_period")
        if holding_days is not None:
            try:
                minutes = float(holding_days) * 24 * 60
                # 同样过滤异常值
                if 0 <= minutes <= self._max_holding_minutes * 3:
                    return round(minutes, 2)
            except (ValueError, TypeError):
                pass

        return None

    def _extract_event_impact(self, rec: Dict) -> Optional[str]:
        """从交易记录中提取事件强度"""
        factor_scores = rec.get("factor_scores", {}) or {}
        if isinstance(factor_scores, dict):
            impact = factor_scores.get("event_impact")
            if impact:
                return str(impact)
            extra = factor_scores.get("extra_context", {}) or {}
            if isinstance(extra, dict):
                impact = extra.get("event_impact")
                if impact:
                    return str(impact)
        return None


# ------------------------------------------------------------------ #
#  全局单例
# ------------------------------------------------------------------ #
_history_tracker: Optional[CrossMarketHistoryTracker] = None


def get_history_tracker() -> CrossMarketHistoryTracker:
    """获取历史统计追踪器单例"""
    global _history_tracker
    if _history_tracker is None:
        _history_tracker = CrossMarketHistoryTracker()
    return _history_tracker
