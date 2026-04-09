"""
场景绩效分析模块 (Scenario Performance Analytics) - Phase 3

功能:
- 场景胜率/盈亏比统计
- 场景→策略绩效关联分析
- 场景准确率计算
- 权重自适应学习基础数据
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict

from app.storage.database import get_database

logger = logging.getLogger("ftseek.analytics.scenario_performance")
logger.propagate = False


class ScenarioPerformanceAnalytics:
    """
    场景绩效分析器 (Phase 3 新增)

    提供场景维度的绩效分析和准确率统计
    """

    def __init__(self):
        self.db = get_database()
        self._lock = threading.Lock()
        logger.info("场景绩效分析器已初始化")

    # ============================================================
    #  场景胜率统计
    # ============================================================

    def analyze_scenario_win_rate(self, days: int = 30) -> Dict[str, Dict]:
        """
        分析各场景的胜率

        Args:
            days: 分析天数范围

        Returns:
            {
                "trend_strong": {
                    "total_trades": 50,
                    "wins": 32,
                    "losses": 18,
                    "win_rate": 0.64,
                    "avg_pnl_pct": 2.35,
                    "total_pnl_pct": 117.5
                },
                ...
            }
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()

            cursor.execute("""
                SELECT
                    scenario,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl_pct < 0 THEN 1 ELSE 0 END) as losses,
                    AVG(pnl_pct) as avg_pnl_pct,
                    SUM(pnl_pct) as total_pnl_pct,
                    AVG(adx_value) as avg_adx,
                    AVG(mtf_alignment) as avg_mtf_alignment
                FROM signal_outcomes
                WHERE scenario IS NOT NULL AND scenario != '' AND timestamp > ?
                GROUP BY scenario
                ORDER BY total_trades DESC
            """, (cutoff,))

            rows = cursor.fetchall()
            result = {}
            for row in rows:
                scenario = row['scenario']
                total = row['total_trades']
                wins = row['wins']
                losses = row['losses']

                result[scenario] = {
                    'total_trades': total,
                    'wins': wins,
                    'losses': losses,
                    'win_rate': wins / total if total > 0 else 0,
                    'avg_pnl_pct': round(row['avg_pnl_pct'] or 0, 2),
                    'total_pnl_pct': round(row['total_pnl_pct'] or 0, 2),
                    'avg_adx': round(row['avg_adx'] or 0, 2),
                    'avg_mtf_alignment': round(row['avg_mtf_alignment'] or 0, 2),
                }

            return result

    def get_scenario_accuracy(self, scenario: str, days: int = 30) -> Dict[str, Any]:
        """
        获取某场景的准确率详情

        Args:
            scenario: 场景名称
            days: 分析天数范围

        Returns:
            {
                "scenario": "trend_strong",
                "days": 30,
                "total_trades": 50,
                "accuracy": 0.64,
                "avg_pnl": 2.35,
                "best_trade": 8.5,
                "worst_trade": -4.2,
                "consecutive_wins": 5,
                "consecutive_losses": 2,
            }
        """
        outcomes = self.db.get_signal_outcomes_by_scenario(scenario, days)

        if not outcomes:
            return {
                'scenario': scenario,
                'days': days,
                'total_trades': 0,
                'accuracy': 0,
                'avg_pnl': 0,
                'error': 'no_data'
            }

        total = len(outcomes)
        wins = sum(1 for o in outcomes if o.get('pnl_pct', 0) > 0)
        losses = sum(1 for o in outcomes if o.get('pnl_pct', 0) < 0)

        pnl_values = [o.get('pnl_pct', 0) for o in outcomes]
        avg_pnl = sum(pnl_values) / total if total > 0 else 0
        best_trade = max(pnl_values) if pnl_values else 0
        worst_trade = min(pnl_values) if pnl_values else 0

        # 计算连胜/连败
        consecutive_wins = 0
        consecutive_losses = 0
        current_wins = 0
        current_losses = 0

        for o in outcomes:
            pnl = o.get('pnl_pct', 0)
            if pnl > 0:
                current_wins += 1
                current_losses = 0
                consecutive_wins = max(consecutive_wins, current_wins)
            elif pnl < 0:
                current_losses += 1
                current_wins = 0
                consecutive_losses = max(consecutive_losses, current_losses)

        return {
            'scenario': scenario,
            'days': days,
            'total_trades': total,
            'accuracy': round(wins / total if total > 0 else 0, 2),
            'avg_pnl': round(avg_pnl, 2),
            'best_trade': round(best_trade, 2),
            'worst_trade': round(worst_trade, 2),
            'consecutive_wins': consecutive_wins,
            'consecutive_losses': consecutive_losses,
        }

    # ============================================================
    #  场景→策略关联分析
    # ============================================================

    def analyze_scenario_strategy_matrix(self, days: int = 30) -> Dict[str, Dict[str, Dict]]:
        """
        分析各场景×策略的二维表现矩阵

        Args:
            days: 分析天数范围

        Returns:
            {
                "trend_strong": {
                    "supertrend": {"trades": 15, "win_rate": 0.67, "avg_pnl": 2.8},
                    "confluence_v2": {"trades": 20, "win_rate": 0.60, "avg_pnl": 2.1},
                    ...
                },
                ...
            }
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    scenario,
                    strategy,
                    COUNT(*) as trades,
                    SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                    AVG(pnl_pct) as avg_pnl
                FROM signal_outcomes
                WHERE scenario IS NOT NULL AND scenario != '' AND timestamp > ?
                GROUP BY scenario, strategy
                ORDER BY scenario, trades DESC
            """, (cutoff,))

            rows = cursor.fetchall()

            result = defaultdict(dict)
            for row in rows:
                scenario = row['scenario']
                strategy = row['strategy']
                trades = row['trades']
                wins = row['wins']

                result[scenario][strategy] = {
                    'trades': trades,
                    'win_rate': round(wins / trades if trades > 0 else 0, 2),
                    'avg_pnl': round(row['avg_pnl'] or 0, 2),
                }

            return dict(result)

    def get_best_strategies_for_scenario(self, scenario: str, days: int = 30,
                                          min_trades: int = 3) -> List[Dict]:
        """
        获取某场景下表现最好的策略

        Args:
            scenario: 场景名称
            days: 分析天数范围
            min_trades: 最小交易数过滤

        Returns:
            按胜率排序的策略列表
        """
        matrix = self.analyze_scenario_strategy_matrix(days)

        if scenario not in matrix:
            return []

        strategies = matrix[scenario]

        # 过滤交易数不足的策略
        filtered = [
            {'strategy': s, **data}
            for s, data in strategies.items()
            if data['trades'] >= min_trades
        ]

        # 按胜率排序
        filtered.sort(key=lambda x: x['win_rate'], reverse=True)

        return filtered

    # ============================================================
    #  场景置信度校准
    # ============================================================

    def analyze_confidence_accuracy(self, days: int = 30,
                                    confidence_buckets: int = 5) -> Dict[str, Dict]:
        """
        分析置信度区间的准确率 (置信度校准)

        Args:
            days: 分析天数范围
            confidence_buckets: 置信度分桶数

        Returns:
            {
                "0.5-0.6": {"trades": 20, "win_rate": 0.45, "avg_pnl": 1.2},
                "0.6-0.7": {"trades": 35, "win_rate": 0.52, "avg_pnl": 1.8},
                "0.7-0.8": {"trades": 40, "win_rate": 0.60, "avg_pnl": 2.3},
                ...
            }
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # 获取所有带置信度的记录
            cursor.execute("""
                SELECT
                    scenario,
                    confidence,
                    pnl_pct
                FROM signal_outcomes
                WHERE scenario IS NOT NULL AND confidence IS NOT NULL AND timestamp > ?
                ORDER BY confidence
            """, (cutoff,))

            rows = cursor.fetchall()

            if not rows:
                return {'error': 'no_data'}

            # 按置信度分桶
            bucket_size = 1.0 / confidence_buckets
            buckets = defaultdict(list)

            for row in rows:
                confidence = row['confidence'] or 0.5
                pnl = row['pnl_pct'] or 0

                # 确定桶
                bucket_idx = min(int((confidence - 0.5) / bucket_size), confidence_buckets - 1)
                bucket_idx = max(0, bucket_idx)
                bucket_label = f"{0.5 + bucket_idx * bucket_size:.1f}-{0.5 + (bucket_idx + 1) * bucket_size:.1f}"

                buckets[bucket_label].append(pnl)

            # 计算每桶的统计
            result = {}
            for label, pnls in sorted(buckets.items()):
                wins = sum(1 for p in pnls if p > 0)
                total = len(pnls)

                result[label] = {
                    'trades': total,
                    'win_rate': round(wins / total if total > 0 else 0, 2),
                    'avg_pnl': round(sum(pnls) / total if total > 0 else 0, 2),
                }

            return result

    # ============================================================
    #  场景切换绩效
    # ============================================================

    def analyze_scenario_switch_performance(self, code: str = None,
                                            days: int = 7) -> Dict[str, Any]:
        """
        分析场景切换后的绩效表现

        Args:
            code: 股票代码 (None=全部)
            days: 分析天数范围

        Returns:
            {
                "switches_analyzed": 50,
                "avg_performance_after_switch": 1.8,
                "by_switch_type": {
                    "range_bound→trend_strong": {"count": 10, "avg_pnl": 2.5},
                    ...
                }
            }
        """
        from app.scenario import get_scenario_log_manager
        log_manager = get_scenario_log_manager()

        # 获取场景切换记录
        if code:
            switches = log_manager.get_scenario_switches(code, days=days)
        else:
            # 获取所有股票的切换
            all_codes = log_manager.get_all_active_codes()
            switches = []
            for c in all_codes[:10]:  # 限制前 10 个股票
                switches.extend([{'code': c, **s} for s in log_manager.get_scenario_switches(c, days=days)])

        if not switches:
            return {
                'switches_analyzed': 0,
                'error': 'no_switches'
            }

        # 分析切换后的绩效
        switch_performance = defaultdict(list)

        for switch in switches:
            to_scenario = switch.get('to_scenario')
            switch_time = switch.get('timestamp')

            if not to_scenario or not switch_time:
                continue

            # 查询切换后的交易记录
            outcomes = self.db.get_signal_outcomes_by_scenario(to_scenario, days=days)

            for outcome in outcomes:
                outcome_time = outcome.get('timestamp', '')
                if outcome_time > switch_time:
                    pnl = outcome.get('pnl_pct', 0)
                    switch_type = f"{switch.get('from_scenario', 'unknown')}→{to_scenario}"
                    switch_performance[switch_type].append(pnl)

        # 计算统计
        result = {
            'switches_analyzed': len(switches),
            'by_switch_type': {}
        }

        all_pnls = []
        for switch_type, pnls in switch_performance.items():
            if pnls:
                avg_pnl = sum(pnls) / len(pnls)
                all_pnls.extend(pnls)
                result['by_switch_type'][switch_type] = {
                    'count': len(pnls),
                    'avg_pnl': round(avg_pnl, 2),
                }

        result['avg_performance_after_switch'] = round(
            sum(all_pnls) / len(all_pnls) if all_pnls else 0, 2
        )

        return result

    # ============================================================
    #  场景权重建议
    # ============================================================

    def get_scenario_weight_recommendations(self, days: int = 30) -> Dict[str, Dict]:
        """
        基于历史绩效获取场景权重建议

        Args:
            days: 分析天数范围

        Returns:
            {
                "trend_strong": {
                    "current_weight": 1.5,
                    "recommended_weight": 1.7,
                    "reason": "win_rate=0.65, avg_pnl=2.8"
                },
                ...
            }
        """
        performance = self.analyze_scenario_win_rate(days)

        recommendations = {}
        for scenario, stats in performance.items():
            win_rate = stats.get('win_rate', 0)
            avg_pnl = stats.get('avg_pnl_pct', 0)
            total_trades = stats.get('total_trades', 0)

            # 数据不足时不推荐
            if total_trades < 5:
                continue

            # 简单权重计算：基于胜率和盈亏
            base_weight = 1.0
            if win_rate > 0.6:
                base_weight += 0.3
            elif win_rate > 0.5:
                base_weight += 0.1

            if avg_pnl > 2.0:
                base_weight += 0.2
            elif avg_pnl > 1.0:
                base_weight += 0.1

            # 确定当前权重（假设主策略为 1.5）
            current_weight = 1.5

            recommendations[scenario] = {
                'current_weight': current_weight,
                'recommended_weight': round(min(base_weight, 2.0), 1),
                'reason': f"win_rate={win_rate:.2f}, avg_pnl={avg_pnl:.1f}, trades={total_trades}",
            }

        return recommendations


# 全局单例
_analytics: Optional[ScenarioPerformanceAnalytics] = None


def get_scenario_performance_analytics() -> ScenarioPerformanceAnalytics:
    """获取场景绩效分析器单例"""
    global _analytics
    if _analytics is None:
        _analytics = ScenarioPerformanceAnalytics()
    return _analytics
