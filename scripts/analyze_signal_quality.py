#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号质量分析脚本

功能:
1. 加载历史信号记录
2. 按置信度区间统计胜率和盈亏比
3. 生成分析报告
4. 提供阈值调整建议

运行时间：每日收盘后或手动运行
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict
from dataclasses import dataclass

sys.path.insert(0, '/opt/ftseek/FTSeek')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.signal_analysis")

# 信号记录目录
SIGNAL_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/signal_records')
ANALYSIS_OUTPUT_DIR = Path('/opt/ftseek/FTSeek/data/analysis_output')
ANALYSIS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SignalStats:
    """信号统计"""
    bucket: str
    count: int
    executed: int
    winning: int
    losing: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    profit_factor: float
    avg_confidence: float


class SignalQualityAnalyzer:
    """信号质量分析器"""
    
    def __init__(self, days: int = 7):
        """
        初始化
        
        Args:
            days: 分析天数
        """
        self.days = days
        self.records: List[Dict] = []
        self._load_records()
    
    def _load_records(self):
        """加载历史信号记录"""
        if not SIGNAL_RECORDS_DIR.exists():
            logger.warning("信号记录目录不存在")
            return
        
        # 加载最近 N 天的记录
        today = datetime.now()
        loaded = 0
        
        for i in range(self.days):
            date = today - timedelta(days=i)
            date_str = date.strftime('%Y-%m-%d')
            records_file = SIGNAL_RECORDS_DIR / f'signals_{date_str}.json'
            
            if records_file.exists():
                try:
                    with open(records_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.records.extend(data)
                        loaded += len(data)
                        logger.info(f"加载 {date_str}: {len(data)} 条记录")
                except Exception as e:
                    logger.error(f"加载文件失败 {records_file}: {e}")
        
        logger.info(f"总加载记录：{loaded} 条")
    
    def analyze_by_confidence(self) -> Dict[str, SignalStats]:
        """
        按置信度区间分析
        
        Returns:
            dict: 各区间统计
        """
        # 定义置信度区间
        buckets = {
            '0.40-0.50': (0.40, 0.50),
            '0.50-0.60': (0.50, 0.60),
            '0.60-0.70': (0.60, 0.70),
            '0.70-0.80': (0.70, 0.80),
            '0.80-0.90': (0.80, 0.90),
            '0.90-1.00': (0.90, 1.00),
        }
        
        results = {}
        
        for bucket_name, (min_conf, max_conf) in buckets.items():
            # 筛选该区间的信号
            bucket_records = [
                r for r in self.records
                if min_conf <= r.get('confidence', 0) < max_conf
            ]
            
            if not bucket_records:
                continue
            
            # 统计
            total = len(bucket_records)
            executed = sum(1 for r in bucket_records if r.get('executed', False))
            
            # 已执行的信号
            executed_records = [r for r in bucket_records if r.get('executed', False)]
            pnl_records = [r for r in executed_records if r.get('pnl') is not None]
            
            winning = sum(1 for r in pnl_records if r.get('pnl', 0) > 0)
            losing = sum(1 for r in pnl_records if r.get('pnl', 0) <= 0)
            
            total_pnl = sum(r.get('pnl', 0) for r in pnl_records)
            avg_pnl = total_pnl / len(pnl_records) if pnl_records else 0
            
            win_rate = winning / len(pnl_records) if pnl_records else 0
            
            # 盈亏比
            winning_pnls = [r.get('pnl', 0) for r in pnl_records if r.get('pnl', 0) > 0]
            losing_pnls = [abs(r.get('pnl', 0)) for r in pnl_records if r.get('pnl', 0) < 0]
            
            profit_factor = (
                sum(winning_pnls) / sum(losing_pnls) 
                if losing_pnls and sum(losing_pnls) > 0 
                else float('inf')
            )
            
            avg_confidence = sum(r.get('confidence', 0) for r in bucket_records) / total
            
            results[bucket_name] = SignalStats(
                bucket=bucket_name,
                count=total,
                executed=executed,
                winning=winning,
                losing=losing,
                win_rate=win_rate,
                total_pnl=total_pnl,
                avg_pnl=avg_pnl,
                profit_factor=profit_factor,
                avg_confidence=avg_confidence,
            )
        
        return results
    
    def analyze_by_market_regime(self) -> Dict[str, Dict]:
        """
        按市场状态分析
        
        Returns:
            dict: 各市场状态统计
        """
        regimes = defaultdict(list)
        
        for r in self.records:
            regime = r.get('market_regime', 'unknown')
            regimes[regime].append(r)
        
        results = {}
        
        for regime, regime_records in regimes.items():
            if not regime_records:
                continue
            
            total = len(regime_records)
            executed = sum(1 for r in regime_records if r.get('executed', False))
            
            executed_records = [r for r in regime_records if r.get('executed', False)]
            pnl_records = [r for r in executed_records if r.get('pnl') is not None]
            
            winning = sum(1 for r in pnl_records if r.get('pnl', 0) > 0)
            total_pnl = sum(r.get('pnl', 0) for r in pnl_records)
            win_rate = winning / len(pnl_records) if pnl_records else 0
            
            results[regime] = {
                'total': total,
                'executed': executed,
                'winning': winning,
                'win_rate': round(win_rate, 4),
                'total_pnl': round(total_pnl, 2),
            }
        
        return results
    
    def generate_recommendations(self, confidence_stats: Dict[str, SignalStats]) -> List[str]:
        """
        生成优化建议
        
        Args:
            confidence_stats: 置信度区间统计
        
        Returns:
            list: 建议列表
        """
        recommendations = []
        
        if not confidence_stats:
            return recommendations
        
        # 找出最佳置信度区间
        best_bucket = None
        best_win_rate = 0
        best_profit_factor = 0
        
        for bucket, stats in confidence_stats.items():
            if stats.count >= 3:  # 至少 3 个信号
                # 综合评分：胜率 + 盈亏比
                score = stats.win_rate * 0.6 + min(stats.profit_factor, 3.0) / 3.0 * 0.4
                if score > best_win_rate:
                    best_win_rate = score
                    best_bucket = bucket
                    best_profit_factor = stats.profit_factor
        
        if best_bucket:
            bucket_min = float(best_bucket.split('-')[0])
            recommendations.append(f"最佳置信度区间：{best_bucket}")
            recommendations.append(f"  胜率：{best_win_rate*100:.1f}%")
            recommendations.append(f"  盈亏比：{best_profit_factor:.2f}")
            recommendations.append(f"建议 min_confidence 设置为 {bucket_min:.2f}")
        
        # 分析低置信度表现
        low_conf_stats = confidence_stats.get('0.40-0.50')
        if low_conf_stats and low_conf_stats.count >= 5:
            if low_conf_stats.win_rate < 0.45:
                recommendations.append(f"⚠️  低置信度区间 (0.40-0.50) 胜率仅{low_conf_stats.win_rate*100:.1f}%")
                recommendations.append("  建议提高 min_confidence 至 0.50 以上")
        
        # 分析高置信度表现
        high_conf_stats = confidence_stats.get('0.80-0.90') or confidence_stats.get('0.90-1.00')
        if high_conf_stats and high_conf_stats.count >= 3:
            if high_conf_stats.win_rate > 0.65:
                recommendations.append(f"✅ 高置信度区间表现优秀 (胜率{high_conf_stats.win_rate*100:.1f}%)")
                recommendations.append("  可考虑适当降低阈值以捕获更多高质量信号")
        
        return recommendations
    
    def generate_report(self) -> Dict:
        """生成分析报告"""
        confidence_stats = self.analyze_by_confidence()
        regime_stats = self.analyze_by_market_regime()
        recommendations = self.generate_recommendations(confidence_stats)
        
        # 转换为字典
        confidence_dict = {}
        for bucket, stats in confidence_stats.items():
            confidence_dict[bucket] = {
                'count': stats.count,
                'executed': stats.executed,
                'win_rate': round(stats.win_rate, 4),
                'total_pnl': round(stats.total_pnl, 2),
                'avg_pnl': round(stats.avg_pnl, 2),
                'profit_factor': round(stats.profit_factor, 4) if stats.profit_factor != float('inf') else 'inf',
                'avg_confidence': round(stats.avg_confidence, 4),
            }
        
        return {
            'analysis_date': datetime.now().isoformat(),
            'period_days': self.days,
            'total_records': len(self.records),
            'confidence_analysis': confidence_dict,
            'market_regime_analysis': regime_stats,
            'recommendations': recommendations,
        }
    
    def save_report(self, report: Dict):
        """保存报告"""
        output_file = ANALYSIS_OUTPUT_DIR / f'signal_quality_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"分析报告已保存：{output_file}")
    
    def print_report(self, report: Dict):
        """打印报告"""
        print("\n" + "=" * 80)
        print("📊 FTSeek 信号质量分析报告")
        print("=" * 80)
        print(f"分析时间：{report['analysis_date']}")
        print(f"分析周期：{report['period_days']} 天")
        print(f"总信号数：{report['total_records']}")
        print("=" * 80)
        
        # 置信度区间分析
        print("\n🎯 置信度区间分析:")
        print("-" * 80)
        
        for bucket, stats in report['confidence_analysis'].items():
            print(f"\n{bucket}:")
            print(f"  信号数：{stats['count']}")
            print(f"  执行数：{stats['executed']}")
            print(f"  胜率：{stats['win_rate']*100:.1f}%")
            print(f"  总盈亏：HK${stats['total_pnl']:.2f}")
            print(f"  平均盈亏：HK${stats['avg_pnl']:.2f}")
            print(f"  盈亏比：{stats['profit_factor']}")
        
        # 市场状态分析
        print("\n" + "=" * 80)
        print("📈 按市场状态分析:")
        print("-" * 80)
        
        for regime, stats in report['market_regime_analysis'].items():
            print(f"\n{regime.upper()}:")
            print(f"  信号数：{stats['total']}")
            print(f"  执行数：{stats['executed']}")
            print(f"  胜率：{stats['win_rate']*100:.1f}%")
            print(f"  总盈亏：HK${stats['total_pnl']:.2f}")
        
        # 优化建议
        print("\n" + "=" * 80)
        print("💡 优化建议:")
        print("-" * 80)
        
        for i, rec in enumerate(report['recommendations'], 1):
            print(f"  {i}. {rec}")
        
        print("\n" + "=" * 80)
        print()


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='信号质量分析')
    parser.add_argument('--days', type=int, default=7, help='分析天数 (默认：7 天)')
    args = parser.parse_args()
    
    logger.info("=" * 80)
    logger.info(f"开始信号质量分析 (最近{args.days}天)")
    logger.info("=" * 80)
    
    try:
        analyzer = SignalQualityAnalyzer(days=args.days)
        
        if not analyzer.records:
            logger.warning("没有信号记录，无法分析")
            print("⚠️  没有信号记录，无法生成分析报告")
            return 1
        
        # 生成报告
        report = analyzer.generate_report()
        
        # 保存报告
        analyzer.save_report(report)
        
        # 打印报告
        analyzer.print_report(report)
        
        logger.info("=" * 80)
        logger.info("✅ 分析完成")
        logger.info("=" * 80)
        
        return 0
        
    except Exception as e:
        logger.error(f"❌ 分析失败：{e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
