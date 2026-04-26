"""
MemoryCompressionEngine - 记忆压缩引擎

功能:
- 将原始交易日志压缩为结构化摘要
- 计算交易统计和 6 大维度评分
- 识别错误模式
- 生成改进建议
- 输出 MEMORY.md 格式报告

设计文档：/root/.openclaw/workspace-context/MEMORY_COMPRESSION_SCHEME.md
"""

import json
from app.storage.repository import get_db
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum
import logging

logger = logging.getLogger("ftseek.core.memory_compression")
logger.propagate = False


class CompressionLevel(str, Enum):
    """压缩级别"""
    FULL = "full"           # 完整压缩（包含所有分析）
    PARTIAL = "partial"     # 部分压缩（仅基础统计）
    BASIC = "basic"         # 基础压缩（仅聚合数据）


class MemoryCompressionEngine:
    """记忆压缩引擎"""
    
    def __init__(
        self,
        db_path: str = 'data/ftseek.db',
        output_dir: str = 'data/compressed_memory/',
        compression_level: CompressionLevel = CompressionLevel.FULL
    ):
        """
        初始化记忆压缩引擎
        
        Args:
            db_path: 数据库路径
            output_dir: 输出目录
            compression_level: 压缩级别
        """
        self.db_path = db_path
        self.output_dir = output_dir
        self.compression_level = compression_level
        
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        
        self._conn: Optional[sqlite3.Connection] = None
    
    def _connect(self) -> sqlite3.Connection:
        """获取数据库连接"""
        if self._conn is None:
            with get_db(self.db_path) as conn:
                pass
            self._conn.row_factory = sqlite3.Row
        return self._conn
    
    def _close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def fetch_daily_trades(self, date: datetime) -> List[Dict]:
        """获取指定日期的交易记录"""
        conn = self._connect()
        
        # 使用日期字符串格式匹配（兼容 SQLite 的日期格式）
        date_str = date.strftime('%Y-%m-%d')
        next_date = (date + timedelta(days=1)).strftime('%Y-%m-%d')
        
        # 根据实际数据库 schema 调整字段名
        cursor = conn.execute("""
            SELECT 
                id, signal_id, code as symbol, name, action, entry_price, exit_price,
                quantity, pnl, pnl_percent, fees, strategy,
                entry_time as exec_time, exit_time, exit_reason,
                order_id, status
            FROM trades
            WHERE entry_time >= ? AND entry_time < ?
            ORDER BY entry_time
        """, (date_str, next_date))
        
        trades = []
        for row in cursor.fetchall():
            trade = dict(row)
            # 添加默认评分字段（如果数据库中没有）
            trade['confidence_score'] = trade.get('confidence_score', 0.7)
            trade['timing_score'] = trade.get('timing_score', 0.7)
            trade['position_score'] = trade.get('position_score', 0.7)
            trade['risk_score'] = trade.get('risk_score', 0.7)
            trade['emotion_state'] = trade.get('emotion_state', 'neutral')
            trade['decision_reason'] = trade.get('decision_reason', '')
            trades.append(trade)
        
        return trades
    
    def calculate_stats(self, trades: List[Dict]) -> Dict:
        """计算基础统计"""
        if not trades:
            return self._empty_stats()
        
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if (t.get('pnl') or 0) > 0)
        losing_trades = sum(1 for t in trades if (t.get('pnl') or 0) < 0)
        
        total_pnl = sum((t.get('pnl') or 0) for t in trades)
        pnl_percents = [(t.get('pnl_percent') or 0) for t in trades if t.get('pnl_percent') is not None]
        
        avg_pnl_percent = sum(pnl_percents) / len(pnl_percents) if pnl_percents else 0
        
        # 计算盈亏比
        gross_profit = sum((t.get('pnl') or 0) for t in trades if (t.get('pnl') or 0) > 0)
        gross_loss = abs(sum((t.get('pnl') or 0) for t in trades if (t.get('pnl') or 0) < 0))
        profit_loss_ratio = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # 最大单笔盈利/亏损
        pnl_values = [(t.get('pnl') or 0) for t in trades]
        max_win = max(pnl_values) if pnl_values else 0
        max_loss = min(pnl_values) if pnl_values else 0
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': round(winning_trades / total_trades, 4) if total_trades > 0 else 0,
            'total_pnl': round(float(total_pnl), 2),
            'avg_pnl_percent': round(float(avg_pnl_percent), 4),
            'profit_loss_ratio': round(profit_loss_ratio, 2) if profit_loss_ratio != float('inf') else 'N/A',
            'max_win': round(float(max_win), 2),
            'max_loss': round(float(max_loss), 2),
            'gross_profit': round(float(gross_profit), 2),
            'gross_loss': round(float(gross_loss), 2)
        }
    
    def _empty_stats(self) -> Dict:
        """返回空统计"""
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'avg_pnl_percent': 0,
            'profit_loss_ratio': 'N/A',
            'max_win': 0,
            'max_loss': 0,
            'gross_profit': 0,
            'gross_loss': 0
        }
    
    def analyze_dimensions(self, trades: List[Dict]) -> Dict:
        """6 大维度评分分析"""
        if not trades:
            return self._empty_dimensions()
        
        # 提取各维度评分
        decision_scores = [t.get('confidence_score', 0) for t in trades if t.get('confidence_score')]
        timing_scores = [t.get('timing_score', 0) for t in trades if t.get('timing_score')]
        position_scores = [t.get('position_score', 0) for t in trades if t.get('position_score')]
        risk_scores = [t.get('risk_score', 0) for t in trades if t.get('risk_score')]
        
        # 情绪状态分析
        emotion_states = [t.get('emotion_state', 'unknown') for t in trades]
        emotion_distribution = self._count_distribution(emotion_states)
        
        # 策略效果分析
        strategy_performance = self._analyze_strategy_performance(trades)
        
        def avg(scores):
            return round(sum(scores) / len(scores), 2) if scores else 0
        
        return {
            'decision_quality': {
                'score': avg(decision_scores),
                'count': len(decision_scores),
                'description': '决策质量评分'
            },
            'timing_quality': {
                'score': avg(timing_scores),
                'count': len(timing_scores),
                'description': '时机把握评分'
            },
            'position_management': {
                'score': avg(position_scores),
                'count': len(position_scores),
                'description': '仓位管理评分'
            },
            'risk_execution': {
                'score': avg(risk_scores),
                'count': len(risk_scores),
                'description': '风控执行评分'
            },
            'psychology': {
                'distribution': emotion_distribution,
                'dominant_state': max(emotion_distribution, key=emotion_distribution.get) if emotion_distribution else 'unknown',
                'description': '交易心理状态'
            },
            'strategy_effectiveness': {
                'by_strategy': strategy_performance,
                'description': '策略有效性分析'
            }
        }
    
    def _empty_dimensions(self) -> Dict:
        """返回空维度分析"""
        return {
            'decision_quality': {'score': 0, 'count': 0, 'description': '决策质量评分'},
            'timing_quality': {'score': 0, 'count': 0, 'description': '时机把握评分'},
            'position_management': {'score': 0, 'count': 0, 'description': '仓位管理评分'},
            'risk_execution': {'score': 0, 'count': 0, 'description': '风控执行评分'},
            'psychology': {'distribution': {}, 'dominant_state': 'unknown', 'description': '交易心理状态'},
            'strategy_effectiveness': {'by_strategy': {}, 'description': '策略有效性分析'}
        }
    
    def _count_distribution(self, items: List[str]) -> Dict[str, int]:
        """统计分布"""
        dist = {}
        for item in items:
            dist[item] = dist.get(item, 0) + 1
        return dist
    
    def _analyze_strategy_performance(self, trades: List[Dict]) -> Dict:
        """分析各策略表现"""
        strategy_stats = {}
        
        for trade in trades:
            strategy = trade.get('strategy', 'unknown')
            pnl = trade.get('pnl') or 0
            
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    'count': 0,
                    'wins': 0,
                    'losses': 0,
                    'total_pnl': 0
                }
            
            strategy_stats[strategy]['count'] += 1
            strategy_stats[strategy]['total_pnl'] += pnl
            
            if pnl > 0:
                strategy_stats[strategy]['wins'] += 1
            elif pnl < 0:
                strategy_stats[strategy]['losses'] += 1
        
        # 计算胜率
        for strategy in strategy_stats:
            stats = strategy_stats[strategy]
            if stats['count'] > 0:
                stats['win_rate'] = round(stats['wins'] / stats['count'], 4)
            else:
                stats['win_rate'] = 0
            stats['total_pnl'] = round(stats['total_pnl'], 2)
        
        return strategy_stats
    
    def identify_error_patterns(self, trades: List[Dict]) -> List[Dict]:
        """识别错误模式"""
        patterns = []
        
        # 模式 1: 追涨杀跌
        chase_count = sum(1 for t in trades if t.get('emotion_state') in ['FOMO', 'panic'])
        if chase_count >= 2:
            patterns.append({
                'pattern': '追涨杀跌',
                'count': chase_count,
                'severity': 'high' if chase_count >= 3 else 'medium',
                'examples': [t['symbol'] for t in trades if t.get('emotion_state') in ['FOMO', 'panic']][:3]
            })
        
        # 模式 2: 过早止盈
        early_exit_count = sum(1 for t in trades if t.get('exit_reason') == 'early_profit' and t.get('pnl_percent', 0) < 2)
        if early_exit_count >= 2:
            patterns.append({
                'pattern': '过早止盈',
                'count': early_exit_count,
                'severity': 'medium',
                'examples': [t['symbol'] for t in trades if t.get('exit_reason') == 'early_profit'][:3]
            })
        
        # 模式 3: 延迟止损
        late_stop_count = sum(1 for t in trades if (t.get('pnl_percent') or 0) < -5 and t.get('exit_reason') == 'stop_loss')
        if late_stop_count >= 2:
            patterns.append({
                'pattern': '延迟止损',
                'count': late_stop_count,
                'severity': 'high',
                'examples': [t['symbol'] for t in trades if (t.get('pnl_percent') or 0) < -5][:3]
            })
        
        # 模式 4: 重仓亏损
        heavy_loss_count = sum(1 for t in trades if (t.get('pnl') or 0) < -100 and (t.get('position_score') or 10) < 5)
        if heavy_loss_count >= 1:
            patterns.append({
                'pattern': '重仓亏损',
                'count': heavy_loss_count,
                'severity': 'critical',
                'examples': [t['symbol'] for t in trades if (t.get('pnl') or 0) < -100][:3]
            })
        
        return patterns
    
    def generate_improvements(
        self,
        stats: Dict,
        dimensions: Dict,
        patterns: List[Dict]
    ) -> List[Dict]:
        """生成改进建议"""
        improvements = []
        
        # 基于胜率
        if stats.get('win_rate', 0) < 0.5:
            improvements.append({
                'priority': 'high',
                'category': 'strategy',
                'suggestion': '提高信号质量阈值，减少低胜率交易',
                'expected_impact': '胜率提升 5-10%'
            })
        
        # 基于盈亏比
        plr = stats.get('profit_loss_ratio')
        if isinstance(plr, (int, float)) and plr < 1.5:
            improvements.append({
                'priority': 'high',
                'category': 'exit_strategy',
                'suggestion': '优化止盈止损策略，提高盈亏比',
                'expected_impact': '盈亏比提升至 1.5+'
            })
        
        # 基于错误模式
        for pattern in patterns:
            if pattern['severity'] == 'critical':
                improvements.append({
                    'priority': 'critical',
                    'category': 'risk_control',
                    'suggestion': f'立即处理：{pattern["pattern"]}问题（出现{pattern["count"]}次）',
                    'expected_impact': '避免重大亏损'
                })
            elif pattern['severity'] == 'high':
                improvements.append({
                    'priority': 'high',
                    'category': 'discipline',
                    'suggestion': f'加强纪律：{pattern["pattern"]}（出现{pattern["count"]}次）',
                    'expected_impact': '减少非理性交易'
                })
        
        # 基于心理状态
        psychology = dimensions.get('psychology', {})
        dominant = psychology.get('dominant_state', 'unknown')
        if dominant in ['FOMO', 'panic', 'greedy']:
            improvements.append({
                'priority': 'medium',
                'category': 'psychology',
                'suggestion': f'调整交易心态：主导情绪为{dominant}',
                'expected_impact': '提升决策质量'
            })
        
        return improvements
    
    def extract_key_decisions(self, trades: List[Dict]) -> List[Dict]:
        """提取关键决策"""
        decisions = []
        
        for trade in trades:
            # 高置信度决策
            if (trade.get('confidence_score') or 0) >= 0.7:
                pnl = trade.get('pnl') or 0
                decisions.append({
                    'time': (trade.get('exec_time') or '')[:16].replace('T', ' '),
                    'symbol': trade.get('symbol', ''),
                    'action': trade.get('action', ''),
                    'reason': trade.get('decision_reason', ''),
                    'confidence': trade.get('confidence_score', 0),
                    'outcome': 'win' if pnl > 0 else 'loss'
                })
        
        return decisions
    
    def compress(
        self,
        date: datetime,
        output_format: str = 'markdown'
    ) -> Dict:
        """
        执行压缩
        
        Args:
            date: 目标日期
            output_format: 输出格式 (markdown/json)
        
        Returns:
            压缩结果
        """
        logger.info(f"Starting compression for {date.strftime('%Y-%m-%d')}")
        
        # Step 1: 获取原始数据
        trades = self.fetch_daily_trades(date)
        logger.info(f"Fetched {len(trades)} trades")
        
        # Step 2: 根据压缩级别执行
        if self.compression_level == CompressionLevel.BASIC:
            stats = self.calculate_stats(trades)
            result = {
                'date': date.strftime('%Y-%m-%d'),
                'stats': stats,
                'compression_level': 'basic'
            }
        elif self.compression_level == CompressionLevel.PARTIAL:
            stats = self.calculate_stats(trades)
            dimensions = self.analyze_dimensions(trades)
            result = {
                'date': date.strftime('%Y-%m-%d'),
                'stats': stats,
                'dimensions': dimensions,
                'compression_level': 'partial'
            }
        else:  # FULL
            stats = self.calculate_stats(trades)
            dimensions = self.analyze_dimensions(trades)
            patterns = self.identify_error_patterns(trades)
            improvements = self.generate_improvements(stats, dimensions, patterns)
            key_decisions = self.extract_key_decisions(trades)
            
            result = {
                'date': date.strftime('%Y-%m-%d'),
                'generated_at': datetime.now().isoformat(),
                'stats': stats,
                'dimensions': dimensions,
                'error_patterns': patterns,
                'improvements': improvements,
                'key_decisions': key_decisions,
                'compression_level': 'full'
            }
        
        # Step 3: 保存结果
        if output_format == 'json':
            self._save_json(result, date)
        if output_format == 'markdown':
            self._save_markdown(result, date)
        
        return result
    
    def _save_json(self, result: Dict, date: datetime) -> str:
        """保存为 JSON"""
        filename = f"memory_{date.strftime('%Y-%m-%d')}.json"
        filepath = os.path.join(self.output_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"Saved JSON to {filepath}")
        return filepath
    
    def _save_markdown(self, result: Dict, date: datetime) -> str:
        """保存为 Markdown"""
        filename = f"memory_{date.strftime('%Y-%m-%d')}.md"
        filepath = os.path.join(self.output_dir, filename)
        
        md = self._generate_markdown(result)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md)
        
        logger.info(f"Saved Markdown to {filepath}")
        return filepath
    
    def _generate_markdown(self, result: Dict) -> str:
        """生成 Markdown 报告"""
        date = result['date']
        stats = result['stats']
        
        md = f"""# 交易记忆摘要 - {date}

**生成时间**: {result.get('generated_at', 'N/A')}  
**压缩级别**: {result.get('compression_level', 'unknown')}

---

## 📊 交易统计

| 指标 | 数值 |
|------|------|
| 总交易数 | {stats['total_trades']} |
| 盈利交易 | {stats['winning_trades']} |
| 亏损交易 | {stats['losing_trades']} |
| 胜率 | {stats['win_rate']*100:.1f}% |
| 总盈亏 | {stats['total_pnl']} |
| 平均盈亏 | {stats['avg_pnl_percent']*100:.2f}% |
| 盈亏比 | {stats['profit_loss_ratio']} |
| 最大盈利 | {stats['max_win']} |
| 最大亏损 | {stats['max_loss']} |

---
"""
        
        # 6 大维度
        if 'dimensions' in result:
            dims = result['dimensions']
            md += """## 🎯 6 大维度评分

| 维度 | 评分 | 样本数 |
|------|------|--------|
"""
            for key in ['decision_quality', 'timing_quality', 'position_management', 'risk_execution']:
                d = dims.get(key, {})
                md += f"| {d.get('description', key)} | {d.get('score', 0)} | {d.get('count', 0)} |\n"
            
            # 心理状态
            psych = dims.get('psychology', {})
            md += f"\n**主导情绪**: {psych.get('dominant_state', 'unknown')}\n"
            
            md += "\n---\n\n"
        
        # 错误模式
        if 'error_patterns' in result and result['error_patterns']:
            md += """## ⚠️ 错误模式

| 模式 | 出现次数 | 严重程度 |
|------|---------|---------|
"""
            for p in result['error_patterns']:
                severity_cn = {'critical': '🔴 严重', 'high': '🟠 高', 'medium': '🟡 中', 'low': '🟢 低'}.get(p['severity'], p['severity'])
                md += f"| {p['pattern']} | {p['count']} | {severity_cn} |\n"
            md += "\n---\n\n"
        
        # 改进建议
        if 'improvements' in result and result['improvements']:
            md += """## 💡 改进建议

"""
            for i, imp in enumerate(result['improvements'], 1):
                priority_cn = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}.get(imp['priority'], '')
                md += f"{i}. {priority_cn} **{imp['category']}**: {imp['suggestion']}\n"
                md += f"   - 预期效果：{imp['expected_impact']}\n\n"
            md += "\n---\n\n"
        
        # 关键决策
        if 'key_decisions' in result and result['key_decisions']:
            md += """## 🔑 关键决策

| 时间 | 标的 | 操作 | 置信度 | 结果 |
|------|------|------|--------|------|
"""
            for d in result['key_decisions'][:10]:  # 限制 10 条
                result_icon = '✅' if d['outcome'] == 'win' else '❌'
                md += f"| {d['time']} | {d['symbol']} | {d['action']} | {d['confidence']:.2f} | {result_icon} |\n"
        
        md += "\n---\n\n*此报告由 MemoryCompressionEngine 自动生成*\n"
        
        return md
    
    def compress_date_range(
        self,
        start_date: datetime,
        end_date: datetime,
        output_format: str = 'markdown'
    ) -> List[Dict]:
        """压缩日期范围内的所有交易日"""
        results = []
        current = start_date
        
        while current <= end_date:
            # 跳过周末
            if current.weekday() < 5:
                try:
                    result = self.compress(current, output_format)
                    results.append(result)
                except Exception as e:
                    logger.error(f"Failed to compress {current}: {e}")
            
            current += timedelta(days=1)
        
        return results
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._close()


# 便捷函数
def compress_today(
    db_path: str = 'data/ftseek.db',
    output_dir: str = 'data/compressed_memory/',
    level: str = 'full'
) -> Dict:
    """压缩今日交易数据"""
    level_map = {
        'full': CompressionLevel.FULL,
        'partial': CompressionLevel.PARTIAL,
        'basic': CompressionLevel.BASIC
    }
    
    engine = MemoryCompressionEngine(
        db_path=db_path,
        output_dir=output_dir,
        compression_level=level_map.get(level, CompressionLevel.FULL)
    )
    
    try:
        return engine.compress(datetime.now())
    finally:
        engine._close()


if __name__ == '__main__':
    import sys
    
    # 测试模式
    if len(sys.argv) > 1 and sys.argv[1] == '--demo':
        print("=== MemoryCompressionEngine 演示模式 ===\n")
        
        engine = MemoryCompressionEngine(
            db_path='data/ftseek.db',
            output_dir='data/compressed_memory/',
            compression_level=CompressionLevel.FULL
        )
        
        try:
            # 压缩今日数据
            result = engine.compress(datetime.now(), output_format='markdown')
            
            print(f"压缩完成!")
            print(f"日期：{result['date']}")
            print(f"交易数：{result['stats']['total_trades']}")
            print(f"胜率：{result['stats']['win_rate']*100:.1f}%")
            print(f"总盈亏：{result['stats']['total_pnl']}")
            
            if result.get('error_patterns'):
                print(f"\n发现 {len(result['error_patterns'])} 个错误模式:")
                for p in result['error_patterns']:
                    print(f"  - {p['pattern']} ({p['count']}次)")
            
            if result.get('improvements'):
                print(f"\n生成 {len(result['improvements'])} 条改进建议:")
                for i in result['improvements'][:3]:
                    print(f"  - [{i['priority']}] {i['suggestion']}")
            
            print(f"\n输出文件：data/compressed_memory/memory_{result['date']}.md")
            
        finally:
            engine._close()
    else:
        print("用法：python3 memory_compression.py --demo")
