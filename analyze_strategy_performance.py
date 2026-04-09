#!/usr/bin/env python3
"""
FTSeek 策略性能分析
分析各策略的历史表现
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
# import matplotlib
# matplotlib.use('Agg')  # 非交互式后端
# import matplotlib.pyplot as plt
from typing import Dict, List, Tuple
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.analysis")

class StrategyAnalyzer:
    """策略性能分析器"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.results = {}
        
    def load_all_signals(self) -> pd.DataFrame:
        """加载所有信号数据"""
        conn = sqlite3.connect(self.db_path)
        
        query = """
        SELECT 
            code, name, action, price, quantity, strategy,
            technical_score, sentiment_score, confidence,
            reason, created_at, status
        FROM signals 
        WHERE status IN ('executed', 'confirmed', 'pending')
        ORDER BY created_at
        """
        
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if not df.empty:
            df['created_at'] = pd.to_datetime(df['created_at'])
            logger.info(f"加载了 {len(df)} 个信号")
            
        return df
    
    def analyze_by_strategy(self, signals_df: pd.DataFrame) -> Dict:
        """按策略分析性能"""
        if signals_df.empty:
            return {}
        
        analysis = {}
        
        # 按策略分组
        for strategy, group in signals_df.groupby('strategy'):
            strategy_data = {
                'total_signals': len(group),
                'buy_signals': len(group[group['action'].str.lower() == 'buy']),
                'sell_signals': len(group[group['action'].str.lower() == 'sell']),
                'avg_confidence': group['confidence'].mean() if 'confidence' in group.columns else 0,
                'avg_technical_score': group['technical_score'].mean() if 'technical_score' in group.columns else 0,
                'avg_sentiment_score': group['sentiment_score'].mean() if 'sentiment_score' in group.columns else 0,
                'recent_signals': []
            }
            
            # 最近5个信号
            recent = group.sort_values('created_at', ascending=False).head(5)
            for _, signal in recent.iterrows():
                strategy_data['recent_signals'].append({
                    'date': signal['created_at'].strftime('%Y-%m-%d %H:%M'),
                    'code': signal['code'],
                    'action': signal['action'],
                    'price': signal['price'],
                    'confidence': signal.get('confidence', 0)
                })
            
            analysis[strategy] = strategy_data
        
        return analysis
    
    def analyze_signal_timing(self, signals_df: pd.DataFrame) -> Dict:
        """分析信号时间分布"""
        if signals_df.empty:
            return {}
        
        # 按小时分析
        signals_df['hour'] = signals_df['created_at'].dt.hour
        hourly_counts = signals_df['hour'].value_counts().sort_index()
        
        # 按星期分析
        signals_df['weekday'] = signals_df['created_at'].dt.day_name()
        weekday_counts = signals_df['weekday'].value_counts()
        
        return {
            'hourly_distribution': hourly_counts.to_dict(),
            'weekday_distribution': weekday_counts.to_dict(),
            'total_by_period': {
                'morning': len(signals_df[(signals_df['hour'] >= 9) & (signals_df['hour'] < 12)]),
                'afternoon': len(signals_df[(signals_df['hour'] >= 13) & (signals_df['hour'] < 16)]),
                'evening': len(signals_df[(signals_df['hour'] >= 16) & (signals_df['hour'] < 24)]),
                'night': len(signals_df[(signals_df['hour'] >= 0) & (signals_df['hour'] < 9)])
            }
        }
    
    def analyze_confidence_distribution(self, signals_df: pd.DataFrame) -> Dict:
        """分析置信度分布"""
        if signals_df.empty or 'confidence' not in signals_df.columns:
            return {}
        
        confidence_data = signals_df['confidence'].dropna()
        
        # 分桶统计
        bins = [0, 0.3, 0.5, 0.7, 0.9, 1.0]
        labels = ['很低(0-0.3)', '低(0.3-0.5)', '中(0.5-0.7)', '高(0.7-0.9)', '很高(0.9-1.0)']
        
        if len(confidence_data) > 0:
            confidence_data = pd.cut(confidence_data, bins=bins, labels=labels, include_lowest=True)
            distribution = confidence_data.value_counts().to_dict()
        else:
            distribution = {}
        
        return {
            'distribution': distribution,
            'stats': {
                'mean': signals_df['confidence'].mean(),
                'median': signals_df['confidence'].median(),
                'std': signals_df['confidence'].std(),
                'min': signals_df['confidence'].min(),
                'max': signals_df['confidence'].max()
            } if len(signals_df['confidence'].dropna()) > 0 else {}
        }
    
    def generate_visualizations(self, signals_df: pd.DataFrame, output_dir: str):
        """生成可视化图表（简化版，不依赖matplotlib）"""
        os.makedirs(output_dir, exist_ok=True)
        
        # 生成文本格式的简单图表
        if not signals_df.empty:
            # 1. 策略信号数量文本图表
            strategy_counts = signals_df['strategy'].value_counts()
            chart_text = ["策略信号数量分布:"]
            chart_text.append("-" * 40)
            max_count = strategy_counts.max() if len(strategy_counts) > 0 else 0
            
            for strategy, count in strategy_counts.items():
                bar_length = int((count / max_count) * 50) if max_count > 0 else 0
                chart_text.append(f"{strategy:20s} | {'█' * bar_length} {count}")
            
            chart_file = os.path.join(output_dir, 'strategy_counts.txt')
            with open(chart_file, 'w', encoding='utf-8') as f:
                f.write("\n".join(chart_text))
            
            logger.info(f"文本图表已保存到: {chart_file}")
        
        logger.info(f"分析输出目录: {output_dir}")
    
    def generate_report(self, analysis_results: Dict) -> str:
        """生成分析报告"""
        report = []
        report.append("=" * 70)
        report.append("FTSeek 策略性能分析报告")
        report.append("=" * 70)
        report.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"分析信号总数: {analysis_results.get('total_signals', 0)}")
        report.append("")
        
        # 策略性能分析
        report.append("📊 策略性能分析")
        report.append("-" * 40)
        
        strategy_analysis = analysis_results.get('strategy_analysis', {})
        for strategy, data in strategy_analysis.items():
            report.append(f"\n🔹 {strategy} 策略:")
            report.append(f"   总信号数: {data['total_signals']}")
            report.append(f"   买入信号: {data['buy_signals']}")
            report.append(f"   卖出信号: {data['sell_signals']}")
            if data['avg_confidence'] > 0:
                report.append(f"   平均置信度: {data['avg_confidence']:.2f}")
            if data['avg_technical_score'] > 0:
                report.append(f"   平均技术评分: {data['avg_technical_score']:.2f}")
            
            if data['recent_signals']:
                report.append("   最近信号:")
                for sig in data['recent_signals'][:3]:
                    report.append(f"     • {sig['date']} - {sig['code']} {sig['action']} @ {sig['price']:.2f} (置信度: {sig['confidence']:.2f})")
        
        # 时间分布分析
        timing_analysis = analysis_results.get('timing_analysis', {})
        if timing_analysis:
            report.append("\n⏰ 信号时间分布")
            report.append("-" * 40)
            
            total_by_period = timing_analysis.get('total_by_period', {})
            report.append(f"   上午时段(9-12点): {total_by_period.get('morning', 0)} 个信号")
            report.append(f"   下午时段(13-16点): {total_by_period.get('afternoon', 0)} 个信号")
            report.append(f"   晚间时段(16-24点): {total_by_period.get('evening', 0)} 个信号")
            report.append(f"   夜间时段(0-9点): {total_by_period.get('night', 0)} 个信号")
        
        # 置信度分析
        confidence_analysis = analysis_results.get('confidence_analysis', {})
        if confidence_analysis:
            report.append("\n🎯 置信度分析")
            report.append("-" * 40)
            
            stats = confidence_analysis.get('stats', {})
            if stats:
                report.append(f"   平均置信度: {stats.get('mean', 0):.2f}")
                report.append(f"   中位数: {stats.get('median', 0):.2f}")
                report.append(f"   标准差: {stats.get('std', 0):.2f}")
                report.append(f"   范围: {stats.get('min', 0):.2f} - {stats.get('max', 0):.2f}")
            
            distribution = confidence_analysis.get('distribution', {})
            if distribution:
                report.append("   分布情况:")
                for level, count in distribution.items():
                    if count > 0:
                        report.append(f"     • {level}: {count} 个信号")
        
        # 建议
        report.append("\n💡 优化建议")
        report.append("-" * 40)
        
        total_signals = analysis_results.get('total_signals', 0)
        if total_signals < 10:
            report.append("1. 📈 增加信号数量: 当前信号样本较少，建议运行更长时间收集数据")
        else:
            report.append("1. ✅ 数据量充足，可以进行深入分析")
        
        if strategy_analysis:
            # 找出信号最多的策略
            top_strategy = max(strategy_analysis.items(), key=lambda x: x[1]['total_signals'])
            report.append(f"2. 🎯 重点关注策略: {top_strategy[0]} (产生 {top_strategy[1]['total_signals']} 个信号)")
        
        avg_confidence = confidence_analysis.get('stats', {}).get('mean', 0)
        if avg_confidence < 0.6:
            report.append("3. ⚠️ 置信度偏低: 平均置信度低于0.6，建议优化策略参数或增加过滤条件")
        elif avg_confidence > 0.8:
            report.append("3. ✅ 置信度良好: 平均置信度较高，策略表现稳定")
        
        report.append("4. 🔄 定期回测: 建议每周运行一次回测，监控策略表现")
        report.append("5. 📊 持续优化: 根据分析结果调整策略参数和权重")
        
        report.append("\n" + "=" * 70)
        
        return "\n".join(report)

def main():
    """主函数"""
    db_path = "data/ftseek.db"
    output_dir = "data/analysis_output"
    
    if not os.path.exists(db_path):
        logger.error(f"数据库文件不存在: {db_path}")
        return
    
    # 创建分析器
    analyzer = StrategyAnalyzer(db_path)
    
    # 加载数据
    logger.info("加载信号数据...")
    signals_df = analyzer.load_all_signals()
    
    if signals_df.empty:
        logger.warning("没有找到信号数据")
        return
    
    # 执行分析
    logger.info("分析策略性能...")
    strategy_analysis = analyzer.analyze_by_strategy(signals_df)
    
    logger.info("分析时间分布...")
    timing_analysis = analyzer.analyze_signal_timing(signals_df)
    
    logger.info("分析置信度分布...")
    confidence_analysis = analyzer.analyze_confidence_distribution(signals_df)
    
    # 生成可视化图表
    logger.info("生成可视化图表...")
    analyzer.generate_visualizations(signals_df, output_dir)
    
    # 汇总结果
    analysis_results = {
        'total_signals': len(signals_df),
        'strategy_analysis': strategy_analysis,
        'timing_analysis': timing_analysis,
        'confidence_analysis': confidence_analysis,
        'analysis_date': datetime.now().isoformat()
    }
    
    # 生成报告
    logger.info("生成分析报告...")
    report = analyzer.generate_report(analysis_results)
    print(report)
    
    # 保存报告
    report_file = os.path.join(output_dir, f"strategy_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info(f"分析报告已保存到: {report_file}")
    
    # 保存详细数据
    json_file = report_file.replace('.txt', '.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(analysis_results, f, indent=2, default=str)
    
    logger.info(f"详细数据已保存到: {json_file}")

if __name__ == "__main__":
    main()