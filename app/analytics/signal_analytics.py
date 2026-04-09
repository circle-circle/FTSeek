"""
信号质量分析模块 (P1-1 新增)

功能:
- 信号质量记录持久化 (SQLite)
- 回溯分析 API
- 策略表现分析
- 市场状态分析
- 信号质量趋势分析
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

from app.storage.database import get_database
from app.strategy.consensus_engine import SignalOutcome

logger = logging.getLogger("ftseek.analytics.signal")
logger.propagate = False


class SignalAnalytics:
    """
    信号质量分析器 (P1-1 新增)
    
    提供信号质量记录的查询、分析和回溯功能
    """
    
    def __init__(self):
        self.db = get_database()
        self._lock = threading.Lock()
        logger.info("信号质量分析器已初始化")
    
    # ============================================================
    #  数据写入接口
    # ============================================================
    
    def record_outcome(self, outcome: SignalOutcome) -> bool:
        """
        记录信号成交结果到数据库

        Args:
            outcome: SignalOutcome 数据对象

        Returns:
            是否成功写入
        """
        try:
            data = {
                'code': outcome.code,
                'action': outcome.action,
                'strategy': outcome.strategy,
                'entry_price': outcome.entry_price,
                'exit_price': outcome.exit_price,
                'pnl_pct': outcome.pnl_pct,
                'holding_periods': outcome.holding_periods,
                'market_regime': outcome.market_regime,
                'scenario': outcome.scenario,  # Phase 1: 场景标签
                'adx_value': outcome.adx_value,
                'mtf_alignment': outcome.mtf_alignment,
                'sentiment_score': outcome.sentiment_score,
                'consensus_score': outcome.consensus_score,
                'quality_score': outcome.quality_score,
                'max_drawdown': outcome.max_drawdown,
                'max_profit': outcome.max_profit,
                'timestamp': datetime.fromtimestamp(outcome.timestamp).isoformat() if outcome.timestamp else datetime.now().isoformat(),
            }
            return self.db.save_signal_outcome(data)
        except Exception as e:
            logger.error(f"记录信号结果失败：{e}")
            return False
    
    def record_outcome_dict(self, data: Dict[str, Any]) -> bool:
        """
        记录信号成交结果到数据库 (字典版本)
        
        Args:
            data: SignalOutcome 数据字典
        
        Returns:
            是否成功写入
        """
        try:
            return self.db.save_signal_outcome(data)
        except Exception as e:
            logger.error(f"记录信号结果失败：{e}")
            return False
    
    # ============================================================
    #  查询 API
    # ============================================================
    
    def query_by_code(self, code: str, days: int = 90, limit: int = None) -> List[Dict]:
        """
        查询某股票的信号记录
        
        Args:
            code: 股票代码
            days: 查询天数范围
            limit: 限制返回数量
        
        Returns:
            信号记录列表
        """
        return self.db.get_signal_outcomes_by_code(code, days, limit)
    
    def query_by_strategy(self, strategy: str, days: int = 90, limit: int = None) -> List[Dict]:
        """
        查询某策略的信号记录
        
        Args:
            strategy: 策略名称
            days: 查询天数范围
            limit: 限制返回数量
        
        Returns:
            信号记录列表
        """
        return self.db.get_signal_outcomes_by_strategy(strategy, days, limit)
    
    def query_by_regime(self, market_regime: str, days: int = 90, limit: int = None) -> List[Dict]:
        """
        查询某市场状态下的信号记录

        Args:
            market_regime: 市场状态 (trending/ranging/neutral)
            days: 查询天数范围
            limit: 限制返回数量

        Returns:
            信号记录列表
        """
        return self.db.get_signal_outcomes_by_regime(market_regime, days, limit)

    def query_by_scenario(self, scenario: str, days: int = 90, limit: int = None) -> List[Dict]:
        """
        查询某场景下的信号记录 (Phase 1 新增)

        Args:
            scenario: 场景标签 (trend_strong, range_bound, etc.)
            days: 查询天数范围
            limit: 限制返回数量

        Returns:
            信号记录列表
        """
        return self.db.get_signal_outcomes_by_scenario(scenario, days, limit)
    
    def query_by_period(self, start_date: str, end_date: str, 
                       code: str = None, strategy: str = None) -> List[Dict]:
        """
        查询指定时间段的信号记录
        
        Args:
            start_date: 开始日期 (ISO 格式)
            end_date: 结束日期 (ISO 格式)
            code: 可选，股票代码过滤
            strategy: 可选，策略名称过滤
        
        Returns:
            信号记录列表
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            
            query = """
                SELECT * FROM signal_outcomes 
                WHERE timestamp BETWEEN ? AND ?
            """
            params = [start_date, end_date]
            
            if code:
                query += " AND code = ?"
                params.append(code)
            
            if strategy:
                query += " AND strategy = ?"
                params.append(strategy)
            
            query += " ORDER BY timestamp DESC"
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_summary(self, days: int = 90, group_by: str = None) -> Dict:
        """
        获取信号质量统计摘要
        
        Args:
            days: 查询天数范围
            group_by: 分组字段 ('strategy', 'market_regime', 'code', None)
        
        Returns:
            统计摘要字典
        """
        return self.db.get_signal_outcomes_summary(days, group_by)
    
    # ============================================================
    #  分析工具
    # ============================================================
    
    def analyze_strategy_performance(self, days: int = 90) -> Dict[str, Dict]:
        """
        分析各策略的表现
        
        Args:
            days: 分析天数范围
        
        Returns:
            {策略名: {trades, wins, losses, win_rate, avg_pnl, total_pnl, ...}}
        """
        summary = self.get_summary(days, group_by='strategy')
        return {row['strategy']: row for row in summary.get('data', [])}
    
    def analyze_regime_performance(self, days: int = 90) -> Dict[str, Dict]:
        """
        分析各市场状态下的表现

        Args:
            days: 分析天数范围

        Returns:
            {市场状态：{trades, wins, losses, win_rate, avg_pnl, ...}}
        """
        summary = self.get_summary(days, group_by='market_regime')
        return {row['market_regime']: row for row in summary.get('data', [])}

    def analyze_scenario_performance(self, days: int = 90) -> Dict[str, Dict]:
        """
        分析各场景下的表现 (Phase 1 新增)

        Args:
            days: 分析天数范围

        Returns:
            {场景标签：{trades, wins, losses, win_rate, avg_pnl, ...}}
        """
        summary = self.get_summary(days, group_by='scenario')
        return {row['scenario']: row for row in summary.get('data', [])}
    
    def analyze_code_performance(self, days: int = 90) -> Dict[str, Dict]:
        """
        分析各股票的表现
        
        Args:
            days: 分析天数范围
        
        Returns:
            {股票代码：{trades, wins, losses, win_rate, avg_pnl, ...}}
        """
        summary = self.get_summary(days, group_by='code')
        return {row['code']: row for row in summary.get('data', [])}
    
    def analyze_strategy_by_regime(self, days: int = 90) -> Dict[str, Dict[str, Dict]]:
        """
        分析各策略在不同市场状态下的表现 (二维分析)
        
        Args:
            days: 分析天数范围
        
        Returns:
            {策略名：{市场状态：{trades, win_rate, avg_pnl, ...}}}
        """
        outcomes = self.db.get_signal_outcomes_summary(days)
        
        # 获取所有记录后进行内存分组
        all_outcomes = []
        for regime in ['trending', 'ranging', 'neutral']:
            outcomes_regime = self.query_by_regime(regime, days)
            all_outcomes.extend([(o, regime) for o in outcomes_regime])
        
        # 按策略×市场状态分组
        result = defaultdict(lambda: defaultdict(list))
        for outcome, regime in all_outcomes:
            result[outcome['strategy']][regime].append(outcome)
        
        # 计算统计
        analysis = {}
        for strategy, regimes in result.items():
            analysis[strategy] = {}
            for regime, outcomes_list in regimes.items():
                if not outcomes_list:
                    continue
                
                wins = sum(1 for o in outcomes_list if o['pnl_pct'] > 0)
                losses = sum(1 for o in outcomes_list if o['pnl_pct'] < 0)
                total = len(outcomes_list)
                
                analysis[strategy][regime] = {
                    'trades': total,
                    'wins': wins,
                    'losses': losses,
                    'win_rate': wins / total if total > 0 else 0,
                    'avg_pnl': sum(o['pnl_pct'] for o in outcomes_list) / total,
                    'total_pnl': sum(o['pnl_pct'] for o in outcomes_list),
                    'avg_adx': sum(o['adx_value'] for o in outcomes_list) / total,
                    'avg_mtf_alignment': sum(o['mtf_alignment'] for o in outcomes_list) / total,
                }
        
        return analysis
    
    def analyze_quality_trend(self, code: str = None, days: int = 90, 
                             bucket_days: int = 7) -> List[Dict]:
        """
        分析信号质量趋势 (按时间分桶)
        
        Args:
            code: 可选，股票代码
            days: 总天数范围
            bucket_days: 每个桶的天数
        
        Returns:
            [{period, trades, win_rate, avg_pnl, avg_quality}, ...]
        """
        now = datetime.now()
        buckets = []
        
        for i in range(0, days, bucket_days):
            end = now - timedelta(days=i)
            start = end - timedelta(days=bucket_days)
            
            outcomes = self.query_by_period(
                start.isoformat(), 
                end.isoformat(),
                code=code
            )
            
            if not outcomes:
                continue
            
            wins = sum(1 for o in outcomes if o['pnl_pct'] > 0)
            total = len(outcomes)
            
            buckets.append({
                'period': f"{start.strftime('%m-%d')} ~ {end.strftime('%m-%d')}",
                'start_date': start.isoformat(),
                'end_date': end.isoformat(),
                'trades': total,
                'wins': wins,
                'win_rate': wins / total if total > 0 else 0,
                'avg_pnl': sum(o['pnl_pct'] for o in outcomes) / total,
                'avg_quality': sum(o['quality_score'] for o in outcomes) / total,
            })
        
        # 按时间正序排列
        buckets.reverse()
        return buckets
    
    def analyze_best_conditions(self, strategy: str = None, days: int = 90) -> Dict:
        """
        分析最佳交易条件 (什么条件下某策略表现最好)
        
        Args:
            strategy: 可选，策略名称
            days: 分析天数范围
        
        Returns:
            {
                'best_regime': ...,
                'best_adx_range': ...,
                'best_mtf_alignment_range': ...,
                'best_sentiment_range': ...,
            }
        """
        if strategy:
            outcomes = self.query_by_strategy(strategy, days)
        else:
            outcomes = self.get_summary(days)
            # 获取所有记录
            all_outcomes = []
            for code_summary in self.analyze_code_performance(days).values():
                pass  # 简化处理，直接查询全部
            outcomes = []
            for regime in ['trending', 'ranging', 'neutral']:
                outcomes.extend(self.query_by_regime(regime, days))
        
        if not outcomes:
            return {}
        
        # 按市场状态分组
        regime_perf = defaultdict(list)
        for o in outcomes:
            regime_perf[o['market_regime']].append(o)
        
        best_regime = None
        best_regime_winrate = 0
        for regime, reg_outcomes in regime_perf.items():
            wins = sum(1 for o in reg_outcomes if o['pnl_pct'] > 0)
            win_rate = wins / len(reg_outcomes) if reg_outcomes else 0
            if win_rate > best_regime_winrate:
                best_regime_winrate = win_rate
                best_regime = regime
        
        # 按 ADX 范围分组
        adx_ranges = {
            'low (<20)': [],
            'mid (20-25)': [],
            'high (>25)': [],
        }
        for o in outcomes:
            adx = o.get('adx_value', 0)
            if adx < 20:
                adx_ranges['low (<20)'].append(o)
            elif adx <= 25:
                adx_ranges['mid (20-25)'].append(o)
            else:
                adx_ranges['high (>25)'].append(o)
        
        best_adx_range = None
        best_adx_winrate = 0
        for adx_range, range_outcomes in adx_ranges.items():
            if not range_outcomes:
                continue
            wins = sum(1 for o in range_outcomes if o['pnl_pct'] > 0)
            win_rate = wins / len(range_outcomes)
            if win_rate > best_adx_winrate:
                best_adx_winrate = win_rate
                best_adx_range = adx_range
        
        # 按 MTF 对齐度分组
        mtf_ranges = {
            'low (<0.5)': [],
            'mid (0.5-0.7)': [],
            'high (>0.7)': [],
        }
        for o in outcomes:
            mtf = o.get('mtf_alignment', 0)
            if mtf < 0.5:
                mtf_ranges['low (<0.5)'].append(o)
            elif mtf <= 0.7:
                mtf_ranges['mid (0.5-0.7)'].append(o)
            else:
                mtf_ranges['high (>0.7)'].append(o)
        
        best_mtf_range = None
        best_mtf_winrate = 0
        for mtf_range, range_outcomes in mtf_ranges.items():
            if not range_outcomes:
                continue
            wins = sum(1 for o in range_outcomes if o['pnl_pct'] > 0)
            win_rate = wins / len(range_outcomes)
            if win_rate > best_mtf_winrate:
                best_mtf_winrate = win_rate
                best_mtf_range = mtf_range
        
        return {
            'best_regime': best_regime,
            'best_regime_winrate': round(best_regime_winrate, 3),
            'best_adx_range': best_adx_range,
            'best_adx_winrate': round(best_adx_winrate, 3),
            'best_mtf_range': best_mtf_range,
            'best_mtf_winrate': round(best_mtf_winrate, 3),
            'total_analyzed': len(outcomes),
        }
    
    def generate_report(self, days: int = 90) -> str:
        """
        生成信号质量分析报告
        
        Args:
            days: 分析天数范围
        
        Returns:
            格式化的报告文本
        """
        summary = self.get_summary(days)
        strategy_perf = self.analyze_strategy_performance(days)
        regime_perf = self.analyze_regime_performance(days)
        
        lines = [
            "=" * 60,
            f"信号质量分析报告 (最近{days}天)",
            "=" * 60,
            "",
            "## 总体统计",
            f"  总交易数：{summary.get('total_trades', 0)}",
            f"  盈利交易：{summary.get('winning_trades', 0)}",
            f"  亏损交易：{summary.get('losing_trades', 0)}",
            f"  胜率：{summary.get('win_rate', 0):.1%}",
            f"  平均盈亏：{summary.get('avg_pnl_pct', 0):.2f}%",
            f"  总盈亏：{summary.get('total_pnl_pct', 0):.2f}%",
            "",
            "## 策略表现",
        ]
        
        for strategy, stats in sorted(strategy_perf.items(), 
                                      key=lambda x: x[1].get('win_rate', 0), 
                                      reverse=True):
            lines.append(f"  {strategy}:")
            lines.append(f"    交易数：{stats.get('total_trades', 0)}, "
                        f"胜率：{stats.get('win_rate', 0):.1%}, "
                        f"平均盈亏：{stats.get('avg_pnl_pct', 0):.2f}%")
        
        lines.extend([
            "",
            "## 市场状态表现",
        ])
        
        for regime, stats in regime_perf.items():
            lines.append(f"  {regime}:")
            lines.append(f"    交易数：{stats.get('total_trades', 0)}, "
                        f"胜率：{stats.get('win_rate', 0):.1%}, "
                        f"平均盈亏：{stats.get('avg_pnl_pct', 0):.2f}%")
        
        lines.extend([
            "",
            "=" * 60,
            f"报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
        ])
        
        return "\n".join(lines)


# 全局单例
_analytics: Optional[SignalAnalytics] = None


def get_analytics() -> SignalAnalytics:
    """获取信号分析器单例"""
    global _analytics
    if _analytics is None:
        _analytics = SignalAnalytics()
    return _analytics


def init_analytics() -> SignalAnalytics:
    """初始化信号分析器单例"""
    global _analytics
    _analytics = SignalAnalytics()
    return _analytics
