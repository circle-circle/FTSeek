#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场状态动态参数回测脚本

功能:
1. 加载历史交易记录
2. 按不同市场状态分组统计
3. 对比固定参数 vs 动态参数
4. 生成回测报告

回测周期：2026-01-01 至 2026-03-16
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict
from dataclasses import dataclass, asdict

sys.path.insert(0, '/opt/ftseek/FTSeek')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.backtest.regime")

# 数据目录
TRADE_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/trade_records')
BACKTEST_OUTPUT_DIR = Path('/opt/ftseek/FTSeek/data/backtest_output')
BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TradeRecord:
    """交易记录"""
    trade_id: str
    code: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    strategy: str
    confidence: float
    consensus_score: float
    market_regime: str  # 市场状态


class RegimeBacktester:
    """市场状态回测器"""
    
    def __init__(self):
        self.records: List[TradeRecord] = []
        self._load_trade_records()
    
    def _load_trade_records(self):
        """加载历史交易记录"""
        if not TRADE_RECORDS_DIR.exists():
            logger.warning("交易记录目录不存在")
            return
        
        for file in TRADE_RECORDS_DIR.glob('*.json'):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for record in data:
                        if record.get('exit_price'):  # 只加载已平仓记录
                            self.records.append(TradeRecord(
                                trade_id=record.get('trade_id', ''),
                                code=record.get('code', ''),
                                entry_time=record.get('entry_time', ''),
                                exit_time=record.get('exit_time', ''),
                                entry_price=float(record.get('entry_price', 0)),
                                exit_price=float(record.get('exit_price', 0)),
                                quantity=int(record.get('quantity', 0)),
                                pnl=float(record.get('pnl', 0)),
                                pnl_pct=float(record.get('pnl_pct', 0)),
                                strategy=record.get('strategy', ''),
                                confidence=float(record.get('confidence', 0)),
                                consensus_score=float(record.get('consensus_score', 0)),
                                market_regime=record.get('market_regime', 'unknown'),
                            ))
                
                logger.info(f"从 {file.name} 加载 {len([r for r in data if r.get('exit_price')])} 条记录")
                
            except Exception as e:
                logger.error(f"加载文件失败 {file}: {e}")
        
        logger.info(f"总交易记录：{len(self.records)} 条")
    
    def group_by_regime(self) -> Dict[str, List[TradeRecord]]:
        """按市场状态分组"""
        groups = defaultdict(list)
        
        for record in self.records:
            regime = record.market_regime or 'unknown'
            groups[regime].append(record)
        
        return dict(groups)
    
    def analyze_group(self, records: List[TradeRecord], group_name: str) -> Dict:
        """分析单组数据"""
        if not records:
            return {}
        
        total_trades = len(records)
        winning = sum(1 for r in records if r.pnl > 0)
        losing = sum(1 for r in records if r.pnl <= 0)
        win_rate = winning / total_trades if total_trades > 0 else 0
        
        total_pnl = sum(r.pnl for r in records)
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        
        winning_pnls = [r.pnl for r in records if r.pnl > 0]
        losing_pnls = [r.pnl for r in records if r.pnl < 0]
        
        avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0
        avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0
        
        profit_factor = abs(sum(winning_pnls) / sum(losing_pnls)) if losing_pnls else float('inf')
        
        # 置信度分析
        avg_confidence = sum(r.confidence for r in records) / total_trades if total_trades > 0 else 0
        
        # 按置信度区间分组
        confidence_buckets = {
            '0.40-0.50': [],
            '0.50-0.60': [],
            '0.60-0.70': [],
            '0.70-0.80': [],
            '0.80-0.90': [],
            '0.90-1.00': [],
        }
        
        for r in records:
            if 0.40 <= r.confidence < 0.50:
                confidence_buckets['0.40-0.50'].append(r)
            elif 0.50 <= r.confidence < 0.60:
                confidence_buckets['0.50-0.60'].append(r)
            elif 0.60 <= r.confidence < 0.70:
                confidence_buckets['0.60-0.70'].append(r)
            elif 0.70 <= r.confidence < 0.80:
                confidence_buckets['0.70-0.80'].append(r)
            elif 0.80 <= r.confidence < 0.90:
                confidence_buckets['0.80-0.90'].append(r)
            elif r.confidence >= 0.90:
                confidence_buckets['0.90-1.00'].append(r)
        
        bucket_stats = {}
        for bucket, bucket_records in confidence_buckets.items():
            if bucket_records:
                bucket_wins = sum(1 for r in bucket_records if r.pnl > 0)
                bucket_win_rate = bucket_wins / len(bucket_records)
                bucket_avg_pnl = sum(r.pnl for r in bucket_records) / len(bucket_records)
                bucket_stats[bucket] = {
                    'count': len(bucket_records),
                    'win_rate': round(bucket_win_rate, 4),
                    'avg_pnl': round(bucket_avg_pnl, 2),
                }
        
        return {
            'group': group_name,
            'total_trades': total_trades,
            'winning': winning,
            'losing': losing,
            'win_rate': round(win_rate, 4),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl': round(avg_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 4) if profit_factor != float('inf') else 'inf',
            'avg_confidence': round(avg_confidence, 4),
            'confidence_buckets': bucket_stats,
        }
    
    def generate_report(self) -> Dict:
        """生成回测报告"""
        groups = self.group_by_regime()
        
        report = {
            'backtest_date': datetime.now().isoformat(),
            'total_records': len(self.records),
            'groups': {},
            'summary': {},
            'recommendations': [],
        }
        
        # 分析每组
        for regime, records in groups.items():
            report['groups'][regime] = self.analyze_group(records, regime)
        
        # 总体统计
        all_stats = self.analyze_group(self.records, 'all')
        report['summary'] = all_stats
        
        # 生成建议
        report['recommendations'] = self._generate_recommendations(report)
        
        return report
    
    def _generate_recommendations(self, report: Dict) -> List[str]:
        """生成优化建议"""
        recommendations = []
        
        # 分析不同置信度区间的表现
        summary = report.get('summary', {})
        confidence_buckets = summary.get('confidence_buckets', {})
        
        # 找出最佳置信度区间
        best_bucket = None
        best_win_rate = 0
        
        for bucket, stats in confidence_buckets.items():
            if stats['count'] >= 5:  # 至少 5 笔交易
                if stats['win_rate'] > best_win_rate:
                    best_win_rate = stats['win_rate']
                    best_bucket = bucket
        
        if best_bucket:
            recommendations.append(f"最佳置信度区间：{best_bucket} (胜率{best_win_rate*100:.1f}%)")
            
            # 建议阈值
            bucket_min = float(best_bucket.split('-')[0])
            recommendations.append(f"建议 min_confidence 设置为 {bucket_min:.2f}")
        
        # 分析不同市场状态的表现
        groups = report.get('groups', {})
        
        best_regime = None
        best_regime_pnl = -float('inf')
        
        for regime, stats in groups.items():
            if stats.get('total_pnl', 0) > best_regime_pnl:
                best_regime_pnl = stats['total_pnl']
                best_regime = regime
        
        if best_regime:
            recommendations.append(f"最佳市场状态：{best_regime} (总盈亏 HK${best_regime_pnl:.2f})")
        
        return recommendations
    
    def save_report(self, report: Dict):
        """保存报告"""
        output_file = BACKTEST_OUTPUT_DIR / f'regime_backtest_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"回测报告已保存：{output_file}")
    
    def print_report(self, report: Dict):
        """打印报告"""
        print("\n")
        print("=" * 80)
        print("📊 FTSeek 市场状态回测报告")
        print("=" * 80)
        print(f"回测时间：{report['backtest_date']}")
        print(f"总交易数：{report['total_records']}")
        print("=" * 80)
        
        # 按市场状态分组统计
        print("\n📈 按市场状态分组:")
        print("-" * 80)
        
        for regime, stats in report['groups'].items():
            print(f"\n{regime.upper()}:")
            print(f"  交易数：{stats['total_trades']}")
            print(f"  胜率：{stats['win_rate']*100:.1f}%")
            print(f"  总盈亏：HK${stats['total_pnl']:.2f}")
            print(f"  平均盈亏：HK${stats['avg_pnl']:.2f}")
            print(f"  盈亏比：{stats['profit_factor']}")
            print(f"  平均置信度：{stats['avg_confidence']:.3f}")
        
        # 置信度区间分析
        print("\n" + "=" * 80)
        print("🎯 置信度区间分析:")
        print("-" * 80)
        
        summary = report.get('summary', {})
        confidence_buckets = summary.get('confidence_buckets', {})
        
        for bucket, stats in confidence_buckets.items():
            print(f"  {bucket}: {stats['count']}笔，胜率{stats['win_rate']*100:.1f}%, 平均盈亏 HK${stats['avg_pnl']:.2f}")
        
        # 建议
        print("\n" + "=" * 80)
        print("💡 优化建议:")
        print("-" * 80)
        
        for i, rec in enumerate(report['recommendations'], 1):
            print(f"  {i}. {rec}")
        
        print("\n" + "=" * 80)
        print()


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("开始市场状态回测")
    logger.info("=" * 80)
    
    try:
        backtester = RegimeBacktester()
        
        if not backtester.records:
            logger.warning("没有交易记录，无法回测")
            print("⚠️  没有交易记录，无法生成回测报告")
            return 1
        
        # 生成报告
        report = backtester.generate_report()
        
        # 保存报告
        backtester.save_report(report)
        
        # 打印报告
        backtester.print_report(report)
        
        logger.info("=" * 80)
        logger.info("✅ 回测完成")
        logger.info("=" * 80)
        
        return 0
        
    except Exception as e:
        logger.error(f"❌ 回测失败：{e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
