#!/usr/bin/env python3
"""
信号质量分析器 - 结果驱动策略优化

基于实际交易结果，反向分析：
1. 哪些因子组合胜率高
2. 哪些市场状态下策略表现好/差
3. 哪些时段最适合开仓
4. 哪些标的应该被排除

使用方法:
    python -m app.analytics.signal_analyzer

输出:
    - 因子胜率排名
    - 时段胜率分析
    - 标的胜率排名
    - 策略参数调优建议
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, asdict

logger = logging.getLogger("ftseek.analytics.signal_analyzer")

# ── 配置 ──
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")
REPORT_DIR = Path("/opt/ftseek/FTSeek/data/analysis_reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
#  数据模型
# ============================================================

@dataclass
class SignalOutcome:
    """信号结果（简化版，用于内存分析）"""
    code: str
    action: str
    strategy: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    market_regime: str
    adx_value: float
    consensus_score: float
    quality_score: float
    timestamp: str
    exit_reason: str = ""
    # 因子详情 (JSON)
    factors: Optional[Dict] = None


@dataclass
class FactorAnalysis:
    """因子分析结果"""
    factor_name: str
    total_signals: int
    winning_signals: int
    win_rate: float
    avg_pnl: float
    avg_holding_minutes: float
    profit_factor: float  # 总盈利/总亏损


@dataclass
class TimeAnalysis:
    """时段分析结果"""
    time_bucket: str  # "09:30-10:00", "10:00-11:00", etc.
    total_signals: int
    winning_signals: int
    win_rate: float
    avg_pnl: float


@dataclass
class SymbolAnalysis:
    """标的分析结果"""
    code: str
    total_signals: int
    winning_signals: int
    win_rate: float
    avg_pnl: float
    avg_holding_minutes: float


# ============================================================
#  数据库操作
# ============================================================

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_signal_details_table():
    """
    创建/更新 signal_details 表
    用于存储信号触发时的完整因子信息
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL UNIQUE,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            strategy TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_pct REAL,
            -- 因子详情 (JSON)
            factors TEXT,
            bullish_score REAL DEFAULT 0,
            bearish_score REAL DEFAULT 0,
            -- 市场环境
            market_regime TEXT DEFAULT '',
            adx_value REAL DEFAULT 0,
            mtf_alignment REAL DEFAULT 0,
            -- 质量评估
            consensus_score REAL DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            confidence REAL DEFAULT 0,
            -- 风控结果
            exit_reason TEXT DEFAULT '',
            -- 时间
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            exit_time TEXT,
            -- 索引
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_details_code
        ON signal_details(code)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_details_strategy
        ON signal_details(strategy)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_details_regime
        ON signal_details(market_regime)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_details_created
        ON signal_details(created_at)
    """)

    conn.commit()
    conn.close()
    logger.info("signal_details 表已就绪")


def record_signal_detail(
    signal_id: str,
    code: str,
    action: str,
    strategy: str,
    entry_price: float,
    factors: Dict,
    market_regime: str = "",
    adx_value: float = 0,
    consensus_score: float = 0,
    quality_score: float = 1.0,
    confidence: float = 0,
):
    """
    记录信号详情（在信号产生时调用）

    Args:
        signal_id: 信号ID
        code: 标的代码
        action: BUY/SELL
        strategy: 策略名称
        entry_price: 入场价
        factors: 因子详情字典
        market_regime: 市场状态
        adx_value: ADX值
        consensus_score: 共识分数
        quality_score: 质量分数
        confidence: 置信度
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    bullish_score = factors.get("bullish_score", 0)
    bearish_score = factors.get("bearish_score", 0)

    cursor.execute("""
        INSERT OR REPLACE INTO signal_details
        (signal_id, code, action, strategy, entry_price, factors,
         bullish_score, bearish_score, market_regime, adx_value,
         consensus_score, quality_score, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        signal_id, code, action, strategy, entry_price, json.dumps(factors, ensure_ascii=False),
        bullish_score, bearish_score, market_regime, adx_value,
        consensus_score, quality_score, confidence
    ))

    conn.commit()
    conn.close()


def update_signal_result(signal_id: str, exit_price: float, pnl_pct: float, exit_reason: str):
    """更新信号结果（在平仓时调用）"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE signal_details
        SET exit_price = ?, pnl_pct = ?, exit_reason = ?, exit_time = datetime('now')
        WHERE signal_id = ?
    """, (exit_price, pnl_pct, exit_reason, signal_id))

    conn.commit()
    conn.close()


# ============================================================
#  分析引擎
# ============================================================

class SignalAnalyzer:
    """信号质量分析器"""

    def __init__(self, days: int = 30):
        self.days = days
        self.conn = get_db_connection()
        self._outcomes: List[SignalOutcome] = []
        self._load_data()

    def _load_data(self):
        """加载最近 N 天的信号结果数据"""
        since = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                sd.code, sd.action, sd.strategy, sd.entry_price, sd.exit_price,
                sd.pnl_pct, sd.market_regime, sd.adx_value, sd.consensus_score,
                sd.quality_score, sd.created_at, sd.exit_reason, sd.factors
            FROM signal_details sd
            WHERE sd.created_at >= ? AND sd.exit_price IS NOT NULL
            ORDER BY sd.created_at DESC
        """, (since,))

        for row in cursor.fetchall():
            factors = None
            try:
                if row[12]:
                    factors = json.loads(row[12])
            except Exception:
                pass

            self._outcomes.append(SignalOutcome(
                code=row[0], action=row[1], strategy=row[2],
                entry_price=row[3], exit_price=row[4], pnl_pct=row[5] or 0,
                market_regime=row[6] or "", adx_value=row[7] or 0,
                consensus_score=row[8] or 0, quality_score=row[9] or 1.0,
                timestamp=row[10], exit_reason=row[11] or "",
                factors=factors
            ))

        logger.info(f"加载了 {len(self._outcomes)} 条信号结果数据（近{self.days}天）")

    # ── 因子分析 ──

    def analyze_factors(self) -> List[FactorAnalysis]:
        """分析各因子的胜率表现"""
        factor_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0,
                                            "profit_sum": 0, "loss_sum": 0,
                                            "holdings": []})

        for outcome in self._outcomes:
            if not outcome.factors:
                continue

            # 分析每个因子
            for factor_name, factor_data in outcome.factors.items():
                if factor_name in ("bullish_score", "bearish_score"):
                    continue

                stats = factor_stats[factor_name]
                stats["total"] += 1
                stats["pnl_sum"] += outcome.pnl_pct

                if outcome.pnl_pct > 0:
                    stats["wins"] += 1
                    stats["profit_sum"] += outcome.pnl_pct
                else:
                    stats["loss_sum"] += abs(outcome.pnl_pct)

        # 构建结果
        results = []
        for factor_name, stats in factor_stats.items():
            if stats["total"] < 5:  # 样本不足跳过
                continue

            win_rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0
            avg_pnl = stats["pnl_sum"] / stats["total"] if stats["total"] > 0 else 0
            profit_factor = stats["profit_sum"] / max(stats["loss_sum"], 0.0001)

            results.append(FactorAnalysis(
                factor_name=factor_name,
                total_signals=stats["total"],
                winning_signals=stats["wins"],
                win_rate=win_rate,
                avg_pnl=avg_pnl,
                avg_holding_minutes=0,  # 简化处理
                profit_factor=profit_factor
            ))

        # 按胜率排序
        results.sort(key=lambda x: x.win_rate, reverse=True)
        return results

    # ── 时段分析 ──

    def analyze_time_buckets(self) -> List[TimeAnalysis]:
        """分析不同时段的胜率"""
        bucket_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for outcome in self._outcomes:
            try:
                dt = datetime.strptime(outcome.timestamp, "%Y-%m-%d %H:%M:%S")
                hour = dt.hour

                # 分时段
                if 9 <= hour < 10:
                    bucket = "09:30-10:00"
                elif 10 <= hour < 11:
                    bucket = "10:00-11:00"
                elif 11 <= hour < 12:
                    bucket = "11:00-12:00"
                elif 13 <= hour < 14:
                    bucket = "13:00-14:00"
                elif 14 <= hour < 15:
                    bucket = "14:00-15:00"
                else:
                    bucket = "15:00-16:00"

                stats = bucket_stats[bucket]
                stats["total"] += 1
                stats["pnl_sum"] += outcome.pnl_pct
                if outcome.pnl_pct > 0:
                    stats["wins"] += 1
            except Exception:
                continue

        results = []
        for bucket, stats in bucket_stats.items():
            if stats["total"] < 3:
                continue

            win_rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0
            avg_pnl = stats["pnl_sum"] / stats["total"] if stats["total"] > 0 else 0

            results.append(TimeAnalysis(
                time_bucket=bucket,
                total_signals=stats["total"],
                winning_signals=stats["wins"],
                win_rate=win_rate,
                avg_pnl=avg_pnl
            ))

        # 按时段排序
        bucket_order = {"09:30-10:00": 0, "10:00-11:00": 1, "11:00-12:00": 2,
                         "13:00-14:00": 3, "14:00-15:00": 4, "15:00-16:00": 5}
        results.sort(key=lambda x: bucket_order.get(x.time_bucket, 99))
        return results

    # ── 标的分析 ──

    def analyze_symbols(self) -> List[SymbolAnalysis]:
        """分析各标的的胜率"""
        symbol_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0,
                                            "holdings": []})

        for outcome in self._outcomes:
            stats = symbol_stats[outcome.code]
            stats["total"] += 1
            stats["pnl_sum"] += outcome.pnl_pct
            if outcome.pnl_pct > 0:
                stats["wins"] += 1

        results = []
        for code, stats in symbol_stats.items():
            if stats["total"] < 3:  # 样本不足跳过
                continue

            win_rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0
            avg_pnl = stats["pnl_sum"] / stats["total"] if stats["total"] > 0 else 0

            results.append(SymbolAnalysis(
                code=code,
                total_signals=stats["total"],
                winning_signals=stats["wins"],
                win_rate=win_rate,
                avg_pnl=avg_pnl,
                avg_holding_minutes=0
            ))

        results.sort(key=lambda x: x.win_rate, reverse=True)
        return results

    # ── 市场状态分析 ──

    def analyze_market_regimes(self) -> Dict[str, Dict]:
        """分析不同市场状态下的表现"""
        regime_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for outcome in self._outcomes:
            regime = outcome.market_regime or "unknown"
            stats = regime_stats[regime]
            stats["total"] += 1
            stats["pnl_sum"] += outcome.pnl_pct
            if outcome.pnl_pct > 0:
                stats["wins"] += 1

        results = {}
        for regime, stats in regime_stats.items():
            if stats["total"] < 3:
                continue
            win_rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0
            avg_pnl = stats["pnl_sum"] / stats["total"] if stats["total"] > 0 else 0
            results[regime] = {
                "total": stats["total"],
                "wins": stats["wins"],
                "win_rate": win_rate,
                "avg_pnl": avg_pnl
            }

        return results

    # ── 共识分数区间分析 ──

    def analyze_consensus_buckets(self) -> Dict[str, Dict]:
        """分析不同共识分数区间的胜率"""
        bucket_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for outcome in self._outcomes:
            score = outcome.consensus_score
            if score < 1.0:
                bucket = "< 1.0"
            elif score < 1.5:
                bucket = "1.0-1.5"
            elif score < 2.0:
                bucket = "1.5-2.0"
            else:
                bucket = ">= 2.0"

            stats = bucket_stats[bucket]
            stats["total"] += 1
            stats["pnl_sum"] += outcome.pnl_pct
            if outcome.pnl_pct > 0:
                stats["wins"] += 1

        results = {}
        for bucket, stats in bucket_stats.items():
            if stats["total"] < 3:
                continue
            win_rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0
            avg_pnl = stats["pnl_sum"] / stats["total"] if stats["total"] > 0 else 0
            results[bucket] = {
                "total": stats["total"],
                "wins": stats["wins"],
                "win_rate": win_rate,
                "avg_pnl": avg_pnl
            }

        return results

    # ── 生成报告 ──

    def generate_report(self) -> str:
        """生成分析报告"""
        lines = []
        lines.append("=" * 80)
        lines.append(f"FTSeek 信号质量分析报告 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        lines.append(f"分析周期: 近 {self.days} 天 | 信号总数: {len(self._outcomes)}")
        lines.append("=" * 80)

        # 整体统计
        total = len(self._outcomes)
        wins = sum(1 for o in self._outcomes if o.pnl_pct > 0)
        total_pnl = sum(o.pnl_pct for o in self._outcomes)
        avg_pnl = total_pnl / total if total > 0 else 0
        win_rate = wins / total if total > 0 else 0

        lines.append(f"\n[整体表现]")
        lines.append(f"  总信号数: {total}")
        lines.append(f"  盈利数: {wins} | 亏损数: {total - wins}")
        lines.append(f"  胜率: {win_rate:.1%}")
        lines.append(f"  平均盈亏: {avg_pnl:+.2%}")
        lines.append(f"  总盈亏: {total_pnl:+.2%}")

        # 因子分析
        lines.append(f"\n[因子胜率排名]")
        factor_results = self.analyze_factors()
        if factor_results:
            lines.append(f"{'因子':<15} {'信号数':<8} {'胜率':<8} {'平均盈亏':<10} {'盈亏比':<8}")
            lines.append("-" * 60)
            for r in factor_results[:10]:
                lines.append(f"{r.factor_name:<15} {r.total_signals:<8} {r.win_rate:<8.1%} "
                            f"{r.avg_pnl:<10.2%} {r.profit_factor:<8.2f}")
        else:
            lines.append("  数据不足，无法分析")

        # 时段分析
        lines.append(f"\n[时段胜率分析]")
        time_results = self.analyze_time_buckets()
        if time_results:
            lines.append(f"{'时段':<15} {'信号数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 45)
            for r in time_results:
                lines.append(f"{r.time_bucket:<15} {r.total_signals:<8} {r.win_rate:<8.1%} "
                            f"{r.avg_pnl:<10.2%}")
        else:
            lines.append("  数据不足，无法分析")

        # 市场状态分析
        lines.append(f"\n[市场状态分析]")
        regime_results = self.analyze_market_regimes()
        if regime_results:
            lines.append(f"{'市场状态':<20} {'信号数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 50)
            for regime, stats in regime_results.items():
                lines.append(f"{regime:<20} {stats['total']:<8} {stats['win_rate']:<8.1%} "
                            f"{stats['avg_pnl']:<10.2%}")
        else:
            lines.append("  数据不足，无法分析")

        # 共识分数区间分析
        lines.append(f"\n[共识分数区间分析]")
        consensus_results = self.analyze_consensus_buckets()
        if consensus_results:
            lines.append(f"{'分数区间':<15} {'信号数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 45)
            for bucket, stats in consensus_results.items():
                lines.append(f"{bucket:<15} {stats['total']:<8} {stats['win_rate']:<8.1%} "
                            f"{stats['avg_pnl']:<10.2%}")
        else:
            lines.append("  数据不足，无法分析")

        # 标的分析
        lines.append(f"\n[标的胜率排名 (Top 10)]")
        symbol_results = self.analyze_symbols()
        if symbol_results:
            lines.append(f"{'标的':<15} {'信号数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 45)
            for r in symbol_results[:10]:
                lines.append(f"{r.code:<15} {r.total_signals:<8} {r.win_rate:<8.1%} "
                            f"{r.avg_pnl:<10.2%}")
        else:
            lines.append("  数据不足，无法分析")

        # 优化建议
        lines.append(f"\n[策略优化建议]")
        lines.append(self._generate_recommendations(
            factor_results, time_results, regime_results, consensus_results, symbol_results
        ))

        report = "\n".join(lines)

        # 保存报告
        report_file = REPORT_DIR / f"signal_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"分析报告已保存到: {report_file}")

        return report

    def _generate_recommendations(self, factors, times, regimes, consensus, symbols) -> str:
        """基于数据生成优化建议"""
        recommendations = []

        # 时段建议
        if times:
            best_time = max(times, key=lambda x: x.win_rate if x.total_signals >= 5 else 0)
            worst_time = min(times, key=lambda x: x.win_rate if x.total_signals >= 5 else 1)
            if best_time.win_rate > 0.5:
                recommendations.append(f"  ● 最佳交易时段: {best_time.time_bucket} (胜率{best_time.win_rate:.1%})")
            if worst_time.win_rate < 0.4 and worst_time.total_signals >= 5:
                recommendations.append(f"  ● 避免时段: {worst_time.time_bucket} (胜率{worst_time.win_rate:.1%})")

        # 市场状态建议
        if regimes:
            best_regime = max(regimes.items(), key=lambda x: x[1]['win_rate'] if x[1]['total'] >= 5 else 0)
            worst_regime = min(regimes.items(), key=lambda x: x[1]['win_rate'] if x[1]['total'] >= 5 else 1)
            if best_regime[1]['win_rate'] > 0.5:
                recommendations.append(f"  ● 最佳市场环境: {best_regime[0]} (胜率{best_regime[1]['win_rate']:.1%})")
            if worst_regime[1]['win_rate'] < 0.4 and worst_regime[1]['total'] >= 5:
                recommendations.append(f"  ● 谨慎市场环境: {worst_regime[0]} (胜率{worst_regime[1]['win_rate']:.1%})")

        # 共识分数建议
        if consensus:
            sorted_consensus = sorted(consensus.items(), key=lambda x: x[1]['win_rate'], reverse=True)
            if len(sorted_consensus) >= 2:
                best_bucket = sorted_consensus[0]
                recommendations.append(f"  ● 建议共识分数门槛: {best_bucket[0]} (胜率{best_bucket[1]['win_rate']:.1%})")

        # 标的建议
        if symbols:
            good_symbols = [s for s in symbols if s.win_rate > 0.6 and s.total_signals >= 3]
            bad_symbols = [s for s in symbols if s.win_rate < 0.3 and s.total_signals >= 3]
            if good_symbols:
                recommendations.append(f"  ● 高胜率标的: {', '.join(s.code for s in good_symbols[:3])}")
            if bad_symbols:
                recommendations.append(f"  ● 低胜率标的(考虑排除): {', '.join(s.code for s in bad_symbols[:3])}")

        if not recommendations:
            recommendations.append("  数据积累中，建议继续运行模拟交易以收集更多数据...")

        return "\n".join(recommendations)


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="信号质量分析器")
    parser.add_argument("--days", type=int, default=30, help="分析最近 N 天的数据")
    parser.add_argument("--init", action="store_true", help="初始化数据库表")
    args = parser.parse_args()

    if args.init:
        ensure_signal_details_table()
        print("✅ 数据库表已初始化")
        return

    # 确保表存在
    ensure_signal_details_table()

    # 执行分析
    analyzer = SignalAnalyzer(days=args.days)
    report = analyzer.generate_report()
    print(report)


if __name__ == "__main__":
    main()
