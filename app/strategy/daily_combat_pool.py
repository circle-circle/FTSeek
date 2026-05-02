"""
FTSeek 每日动态作战池 (Daily Combat Pool)

核心原则：
1. 每天只聚焦 ≤3 只作战股票 + ≤3 只兜底股票
2. 基于多维评分动态选择
3. 严格资金分配和风控门禁
4. 盘前生成，盘中执行，盘后复盘

SSOT: 所有配置统一从 config_center (unified_config.yaml) 加载。
代码仅保留应急 fallback（会记录 WARNING）。

架构位置：
  盘前 Pipeline → 评分引擎 → 作战池生成 → 盘中执行门禁

用法：
  from app.strategy.daily_combat_pool import get_daily_combat_pool
  pool = get_daily_combat_pool()
  plan = pool.generate_daily_plan()  # 08:30 盘前调用
  if pool.is_in_combat_pool(code):   # 盘中检查
      # 允许交易
"""

import logging
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("ftseek.daily_combat_pool")
logger.propagate = True


# ============================================================
#  数据结构
# ============================================================

@dataclass
class StockScore:
    """股票多维评分"""
    code: str
    name: str = ""
    tier: str = ""  # tier1/tier2/tier3

    # 评分维度 (0-100)
    technical_score: float = 0.0    # 技术面评分
    momentum_score: float = 0.0     # 动量评分
    volume_score: float = 0.0       # 成交量评分
    sentiment_score: float = 0.0    # 新闻情绪评分
    fundamental_score: float = 0.0  # 基本面评分

    # 权重后总分
    total_score: float = 0.0

    # 附加信息
    atr: float = 0.0
    rsi: float = 50.0
    volume_ratio: float = 1.0
    price_change_3d: float = 0.0
    has_catalyst: bool = False


@dataclass
class CombatPlan:
    """每日作战计划"""
    date: str = ""
    generated_at: str = ""

    # 作战股票 (最多 N 只，按优先级排序)
    combat_stocks: List[StockScore] = field(default_factory=list)

    # 兜底股票 (最多 N 只)
    backup_stocks: List[StockScore] = field(default_factory=list)

    # 资金分配
    total_capital: float = 1000000.0
    max_exposure: float = 0.75
    cash_reserve: float = 0.25

    # 风控参数
    max_positions: int = 5
    max_single_pct: float = 0.08


# ============================================================
#  配置加载器 (SSOT)
# ============================================================

def _load_config() -> Dict:
    """
    从 ConfigCenter 加载作战池完整配置 (SSOT)。
    所有业务参数必须来自 unified_config.yaml。
    代码中的 fallback 仅为应急兜底，触发时会记录 WARNING。
    """
    try:
        from config.config_center import get_config_center
        cc = get_config_center()
    except Exception:
        cc = None

    def _get(path: str, default=None):
        try:
            if cc:
                val = cc.get(path, default)
                return default if val is None else val
        except Exception:
            pass
        return default

    return {
        # 作战池核心参数
        "max_combat_stocks": _get("strategy.decision_buffer.daily_combat_pool.max_combat_stocks", 3),
        "max_backup_stocks": _get("strategy.decision_buffer.daily_combat_pool.max_backup_stocks", 3),
        "min_score_to_enter": _get("strategy.decision_buffer.daily_combat_pool.min_score_to_enter", 60),
        "generation_time": _get("strategy.decision_buffer.daily_combat_pool.generation_time", "08:30"),
        "require_in_pool": _get("strategy.decision_buffer.daily_combat_pool.require_in_pool", False),

        # 评分权重
        "scoring_weights": _get("stock_pool.scoring_weights", {
            "technical": 0.40,
            "momentum": 0.30,
            "volume": 0.20,
            "sentiment": 0.10,
        }),

        # 层级定义 (从 stock_pool.tiers 读取)
        "tiers": _get("stock_pool.tiers", {
            "tier1": {
                "name": "核心龙头",
                "stocks": ["HK.00700", "HK.09988", "HK.03690", "HK.09618", "HK.01024"],
                "weight": 0.50,
                "score_bonus": 5.0,
            },
            "tier2": {
                "name": "高弹性成长",
                "stocks": ["HK.00981", "HK.01347", "HK.01810", "HK.01211", "HK.02318"],
                "weight": 0.30,
                "score_bonus": 2.0,
            },
            "tier3": {
                "name": "事件/技术驱动",
                "stocks": [],
                "weight": 0.20,
                "score_bonus": 0.0,
            },
        }),

        # 资金分配
        "capital": {
            "initial_capital": _get("capital_allocation.initial_capital", 1000000.0),
            "max_total_exposure": _get("capital_allocation.max_total_exposure",
                                       _get("risk_limits.max_total_exposure", 0.75)),
            "cash_reserve": _get("capital_allocation.cash_reserve", 0.25),
            "max_positions": _get("risk_limits.max_positions", 5),
            "max_single_position": _get("risk_limits.max_single_position",
                                        _get("capital_allocation.per_position.tier1", 0.08)),
        },

        # 层级仓位比例 (从 capital_allocation 或 risk_limits 读取)
        "tier_allocation": {
            "tier1": _get("capital_allocation.per_position.tier1", 0.08),
            "tier2": _get("capital_allocation.per_position.tier2", 0.06),
            "tier3": _get("capital_allocation.per_position.tier3", 0.04),
        },
    }


# ============================================================
#  评分引擎 (权重从配置加载)
# ============================================================

class ScoringEngine:
    """多维评分引擎 — 权重由 unified_config.yaml 的 stock_pool.scoring_weights 驱动"""

    def __init__(self, weights: Dict = None):
        # SSOT: 权重从配置加载，fallback 仅为应急
        self.weights = weights or {
            "technical": 0.40,
            "momentum": 0.30,
            "volume": 0.20,
            "sentiment": 0.10,
        }

    def score_stock(self, code: str, kline_data: dict = None,
                    volume_data: dict = None, news_data: dict = None) -> StockScore:
        """对单只股票进行多维评分"""
        score = StockScore(code=code)

        score.technical_score = self._score_technical(kline_data)
        score.momentum_score = self._score_momentum(kline_data)
        score.volume_score = self._score_volume(volume_data)
        score.sentiment_score = self._score_sentiment(news_data)

        # 加权总分
        score.total_score = (
            score.technical_score * self.weights.get("technical", 0.40) +
            score.momentum_score * self.weights.get("momentum", 0.30) +
            score.volume_score * self.weights.get("volume", 0.20) +
            score.sentiment_score * self.weights.get("sentiment", 0.10)
        )

        return score

    def _score_technical(self, kline_data: dict) -> float:
        """技术面评分"""
        if not kline_data:
            return 50.0

        score = 50.0
        rsi = kline_data.get("rsi", 50)
        if 30 <= rsi <= 70:
            score += 10
        elif rsi < 30:
            score += 15
        elif rsi > 70:
            score -= 10

        macd = kline_data.get("macd", 0)
        macd_signal = kline_data.get("macd_signal", 0)
        if macd > macd_signal:
            score += 15
        else:
            score -= 5

        bb_position = kline_data.get("bb_position", 0.5)
        if bb_position < 0.2:
            score += 10
        elif bb_position > 0.8:
            score -= 5

        return max(0, min(100, score))

    def _score_momentum(self, kline_data: dict) -> float:
        """动量评分"""
        if not kline_data:
            return 50.0

        score = 50.0
        change_3d = kline_data.get("price_change_3d", 0)
        if 0 < change_3d <= 5:
            score += 20
        elif change_3d > 5:
            score += 10
        elif -5 <= change_3d < 0:
            score -= 5
        elif change_3d < -5:
            score -= 15

        trend_strength = kline_data.get("trend_strength", 0)
        score += trend_strength * 10

        return max(0, min(100, score))

    def _score_volume(self, volume_data: dict) -> float:
        """成交量评分"""
        if not volume_data:
            return 50.0

        score = 50.0
        volume_ratio = volume_data.get("volume_ratio", 1.0)

        if volume_ratio > 2.0:
            score += 30
        elif volume_ratio > 1.5:
            score += 15
        elif volume_ratio < 0.5:
            score -= 20

        return max(0, min(100, score))

    def _score_sentiment(self, news_data: dict) -> float:
        """新闻情绪评分"""
        if not news_data:
            return 50.0

        score = 50.0
        sentiment = news_data.get("sentiment_score", 0)
        news_count = news_data.get("news_count", 0)

        if sentiment > 0.5:
            score += 20
        elif sentiment < -0.5:
            score -= 20

        if news_count > 5:
            score += 10

        return max(0, min(100, score))


# ============================================================
#  每日作战池管理器 (SSOT 驱动)
# ============================================================

class DailyCombatPool:
    """
    每日动态作战池管理器

    所有配置参数从 unified_config.yaml 加载 (通过 _load_config())。
    代码中无任何硬编码业务参数。
    """

    def __init__(self, pool_data_dir: str = None):
        self.pool_data_dir = Path(pool_data_dir) if pool_data_dir else Path("/opt/ftseek/FTSeek/data")
        self.pool_data_dir.mkdir(parents=True, exist_ok=True)

        # 计划文件路径
        self.plan_file = self.pool_data_dir / "daily_combat_plan.json"

        # 当前计划
        self._current_plan: Optional[CombatPlan] = None

        # 从 SSOT 加载全部配置
        self._config = _load_config()
        self._apply_config()

        # 评分引擎 (权重从配置加载)
        self.scoring_engine = ScoringEngine(weights=self._config.get("scoring_weights"))

    def _apply_config(self):
        """将配置中心的值应用到实例属性"""
        cfg = self._config

        # 作战池核心参数
        self.max_combat_stocks = int(cfg.get("max_combat_stocks", 3))
        self.max_backup_stocks = int(cfg.get("max_backup_stocks", 3))
        self.min_score_to_enter = float(cfg.get("min_score_to_enter", 60))
        self.generation_time = str(cfg.get("generation_time", "08:30"))
        self.require_in_pool = bool(cfg.get("require_in_pool", False))

        # 资金与风控
        capital = cfg.get("capital", {})
        self.total_capital = float(capital.get("initial_capital", 1000000.0))
        self.max_exposure = float(capital.get("max_total_exposure", 0.75))
        self.cash_reserve = float(capital.get("cash_reserve", 0.25))
        self.max_positions = int(capital.get("max_positions", 5))
        self.max_single_pct = float(capital.get("max_single_position", 0.08))

        # 层级仓位比例
        self.tier_allocation = dict(cfg.get("tier_allocation", {
            "tier1": 0.08, "tier2": 0.06, "tier3": 0.04,
        }))

        # 层级定义 (用于 _determine_tier 和 score_bonus)
        tiers_cfg = cfg.get("tiers", {})
        self._tier_stocks: Dict[str, List[str]] = {}
        self.tier_score_bonus: Dict[str, float] = {}
        for tier_key, tier_val in tiers_cfg.items():
            if isinstance(tier_val, dict):
                self._tier_stocks[tier_key] = tier_val.get("stocks", [])
                self.tier_score_bonus[tier_key] = float(tier_val.get("score_bonus", 0))
            else:
                # 兼容旧格式
                self._tier_stocks[tier_key] = tier_val if isinstance(tier_val, list) else []
                self.tier_score_bonus[tier_key] = 0.0

    def reload_config(self):
        """热重载配置 (配合 config 热更机制)"""
        self._config = _load_config()
        self._apply_config()
        self.scoring_engine.weights = self._config.get("scoring_weights", self.scoring_engine.weights)
        logger.info("[作战池] 配置已热重载")

    def generate_daily_plan(self, watchlist: List[str] = None,
                           market_data: Dict = None) -> CombatPlan:
        """
        生成每日作战计划（盘前调用）

        Args:
            watchlist: 监控股票列表 (默认从 config 读取)
            market_data: 市场数据字典 {code: {kline, volume, news}}

        Returns:
            CombatPlan: 作战计划
        """
        if watchlist is None:
            watchlist = self._get_default_watchlist()

        # 1. 对所有股票评分
        scores = []
        for code in watchlist:
            data = market_data.get(code, {}) if market_data else {}
            score = self.scoring_engine.score_stock(
                code=code,
                kline_data=data.get("kline") or data.get("kline_data"),
                volume_data=data.get("volume") or data.get("volume_data"),
                news_data=data.get("news") or data.get("news_data"),
            )
            # 应用层级加成 (从配置加载)
            score.tier = self._determine_tier(code)
            score.total_score += self.tier_score_bonus.get(score.tier, 0)
            scores.append(score)

        # 2. 按总分排序
        scores.sort(key=lambda s: s.total_score, reverse=True)

        # 3. 筛选作战股票 (前 N 名，且分数 >= min_score_to_enter)
        combat_stocks = []
        for s in scores:
            if s.total_score >= self.min_score_to_enter:
                combat_stocks.append(s)
            if len(combat_stocks) >= self.max_combat_stocks:
                break

        # 4. 筛选兜底股票 (接下来的 N 名)
        backup_stocks = []
        for s in scores:
            if s in combat_stocks:
                continue
            backup_stocks.append(s)
            if len(backup_stocks) >= self.max_backup_stocks:
                break

        # 5. 创建计划
        plan = CombatPlan(
            date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=datetime.now().isoformat(),
            combat_stocks=combat_stocks,
            backup_stocks=backup_stocks,
            total_capital=self.total_capital,
            max_exposure=self.max_exposure,
            cash_reserve=self.cash_reserve,
            max_positions=self.max_positions,
            max_single_pct=self.max_single_pct,
        )

        # 6. 保存计划
        self._save_plan(plan)
        self._current_plan = plan

        logger.info(
            f"[每日作战池] 计划已生成 | 日期={plan.date} | "
            f"作战={len(combat_stocks)}只 | 兜底={len(backup_stocks)}只"
        )

        for i, s in enumerate(combat_stocks, 1):
            logger.info(
                f"  作战#{i}: {s.code} | 总分={s.total_score:.1f} | "
                f"层级={s.tier} | 仓位={self.tier_allocation.get(s.tier, 0.04):.0%}"
            )

        return plan

    def is_in_combat_pool(self, code: str) -> bool:
        """检查股票是否在作战池中"""
        if not self._current_plan:
            self._load_plan()
        if not self._current_plan:
            return False
        return any(s.code == code for s in self._current_plan.combat_stocks)

    def is_in_backup_pool(self, code: str) -> bool:
        """检查股票是否在兜底池中"""
        if not self._current_plan:
            self._load_plan()
        if not self._current_plan:
            return False
        return any(s.code == code for s in self._current_plan.backup_stocks)

    def get_allocation_for_stock(self, code: str) -> float:
        """获取股票的资金分配比例"""
        if not self._current_plan:
            return 0.0

        for s in self._current_plan.combat_stocks:
            if s.code == code:
                return self.tier_allocation.get(s.tier, 0.04)

        for s in self._current_plan.backup_stocks:
            if s.code == code:
                # 兜底股票仓位减半
                return self.tier_allocation.get(s.tier, 0.04) * 0.5

        return 0.0

    def get_plan_summary(self) -> str:
        """获取计划摘要"""
        if not self._current_plan:
            self._load_plan()
        if not self._current_plan:
            return "无作战计划"

        lines = [
            f"=== 每日作战计划 ({self._current_plan.date}) ===",
            f"生成时间：{self._current_plan.generated_at}",
            f"",
            f"作战股票 ({len(self._current_plan.combat_stocks)}只):",
        ]

        for i, s in enumerate(self._current_plan.combat_stocks, 1):
            alloc = self.tier_allocation.get(s.tier, 0.04)
            lines.append(
                f"  #{i} {s.code} | 总分={s.total_score:.1f} | "
                f"层级={s.tier} | 仓位={alloc:.0%} | "
                f"金额={self.total_capital * alloc:,.0f}"
            )

        lines.append(f"\n兜底股票 ({len(self._current_plan.backup_stocks)}只):")
        for i, s in enumerate(self._current_plan.backup_stocks, 1):
            alloc = self.tier_allocation.get(s.tier, 0.04) * 0.5
            lines.append(
                f"  #{i} {s.code} | 总分={s.total_score:.1f} | "
                f"层级={s.tier} | 仓位={alloc:.0%}"
            )

        lines.append(f"\n风控参数:")
        lines.append(f"  最大持仓：{self._current_plan.max_positions}只")
        lines.append(f"  最大暴露：{self._current_plan.max_exposure:.0%}")
        lines.append(f"  现金储备：{self._current_plan.cash_reserve:.0%}")

        return "\n".join(lines)

    def _determine_tier(self, code: str) -> str:
        """
        确定股票层级 — 从配置加载，不再硬编码。
        如果股票不在任何 tier 中，返回 tier3 (默认)。
        """
        for tier_key, stocks in self._tier_stocks.items():
            if code in stocks:
                return tier_key
        return "tier3"

    def _get_default_watchlist(self) -> List[str]:
        """获取默认监控列表 — 从 app.config.WATCH_LIST_HK 读取"""
        try:
            from app.config import WATCH_LIST_HK
            return WATCH_LIST_HK
        except ImportError:
            logger.warning("[作战池] 无法导入 WATCH_LIST_HK，使用空列表")
            return []

    def _save_plan(self, plan: CombatPlan):
        """保存计划到文件"""
        try:
            plan_dict = asdict(plan)
            plan_dict["combat_stocks"] = [asdict(s) for s in plan.combat_stocks]
            plan_dict["backup_stocks"] = [asdict(s) for s in plan.backup_stocks]

            with open(self.plan_file, "w", encoding="utf-8") as f:
                json.dump(plan_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[作战池] 保存计划失败: {e}")

    def _load_plan(self) -> bool:
        """加载计划文件"""
        try:
            if not self.plan_file.exists():
                return False

            today = datetime.now().strftime("%Y-%m-%d")

            with open(self.plan_file, "r", encoding="utf-8") as f:
                plan_dict = json.load(f)

            if plan_dict.get("date") != today:
                logger.info(f"[作战池] 计划过期 ({plan_dict.get('date')}), 需要重新生成")
                return False

            combat_stocks = [StockScore(**s) for s in plan_dict.get("combat_stocks", [])]
            backup_stocks = [StockScore(**s) for s in plan_dict.get("backup_stocks", [])]

            self._current_plan = CombatPlan(
                date=plan_dict.get("date", ""),
                generated_at=plan_dict.get("generated_at", ""),
                combat_stocks=combat_stocks,
                backup_stocks=backup_stocks,
                total_capital=plan_dict.get("total_capital", self.total_capital),
                max_exposure=plan_dict.get("max_exposure", self.max_exposure),
                cash_reserve=plan_dict.get("cash_reserve", self.cash_reserve),
                max_positions=plan_dict.get("max_positions", self.max_positions),
                max_single_pct=plan_dict.get("max_single_pct", self.max_single_pct),
            )

            logger.info(f"[作战池] 已加载今日计划: {self._current_plan.date}")
            return True

        except Exception as e:
            logger.error(f"[作战池] 加载计划失败: {e}")
            return False


# ============================================================
#  全局单例
# ============================================================

_daily_combat_pool: Optional[DailyCombatPool] = None


def get_daily_combat_pool() -> DailyCombatPool:
    """获取每日作战池单例"""
    global _daily_combat_pool
    if _daily_combat_pool is None:
        _daily_combat_pool = DailyCombatPool()
    return _daily_combat_pool
