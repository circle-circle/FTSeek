"""
Pattern Miner — 从历史交易中挖掘可执行的亏损模式

目标: 不是"描述发生了什么"，而是"发现下次怎么避免"

输出示例:
{
    "pattern": "HK.00981 午后(13:00+)追高(RSI>65)买入 → 4/5 亏损",
    "trigger_conditions": ["time > 13:00", "RSI > 65"],
    "evidence_count": 5,
    "win_rate_when_triggered": 0.20,
    "win_rate_baseline": 0.55,
    "actionable_rule": "HK.00981 午后 RSI>65 时拒绝开仓",
    "severity": "HIGH",  # 影响胜率 -35%
    "suggested_action": "add_guard"  # add_guard / adjust_sl / reduce_size / avoid
}
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger("ftseek.review.pattern_miner")

TRADE_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/trade_records')


class PatternMiner:
    """从历史交易中挖掘可执行模式"""
    
    MIN_EVIDENCE = 3  # 至少 3 次证据才认定为 pattern
    
    def mine(self, days: int = 30) -> List[Dict]:
        """挖掘模式
        
        Args:
            days: 回溯天数
            
        Returns:
            模式列表，按 severity 排序
        """
        trades = self._load_trades(days)
        closed = [t for t in trades if t.get('exit_price') is not None and t.get('pnl') is not None]
        
        if not closed:
            logger.info(f"[PatternMiner] 无平仓交易，跳过模式挖掘")
            return []
        
        logger.info(f"[PatternMiner] 分析 {len(closed)} 笔平仓交易")
        
        patterns = []
        
        # 1. 个股 × 时段模式
        patterns.extend(self._mine_stock_time_patterns(closed))
        
        # 2. 个股 × 信号强度模式
        patterns.extend(self._mine_stock_signal_patterns(closed))
        
        # 3. 止损有效性模式
        patterns.extend(self._mine_stop_loss_patterns(closed))
        
        # 4. 策略胜率模式
        patterns.extend(self._mine_strategy_patterns(closed))
        
        # 去重 + 排序
        patterns = self._deduplicate(patterns)
        patterns.sort(key=lambda p: p.get('severity_score', 0), reverse=True)
        
        logger.info(f"[PatternMiner] 发现 {len(patterns)} 个模式")
        return patterns
    
    def _load_trades(self, days: int) -> List[Dict]:
        """加载最近 N 天的交易记录"""
        trades = []
        for f in sorted(TRADE_RECORDS_DIR.glob('*.json')):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                trades.extend(data)
            except Exception as e:
                logger.warning(f"[PatternMiner] 加载 {f} 失败: {e}")
        return trades
    
    def _mine_stock_time_patterns(self, trades: List[Dict]) -> List[Dict]:
        """挖掘「个股 × 时段」模式"""
        patterns = []
        
        # 按 (stock_code, time_bucket) 分组
        groups = defaultdict(list)
        for t in trades:
            code = t.get('code', '')
            ts = t.get('timestamp', '')
            if not code or not ts:
                continue
            
            # 提取时段: morning (09:30-12:00) / afternoon (13:00-16:00)
            try:
                hour = int(ts[11:13])
                minute = int(ts[14:16])
                time_bucket = 'morning' if (hour < 12 or (hour == 9 and minute >= 30)) else 'afternoon'
            except (ValueError, IndexError):
                time_bucket = 'unknown'
            
            groups[(code, time_bucket)].append(t)
        
        for (code, time_bucket), group_trades in groups.items():
            if len(group_trades) < self.MIN_EVIDENCE:
                continue
            
            winning = [t for t in group_trades if t.get('pnl', 0) > 0]
            win_rate = len(winning) / len(group_trades)
            
            # 如果胜率显著低于 50%，标记为模式
            if win_rate < 0.40:
                avg_pnl = sum(t.get('pnl', 0) for t in group_trades) / len(group_trades)
                patterns.append({
                    'pattern': f"{code} {time_bucket}时段开仓 → {len(winning)}/{len(group_trades)} 盈利 (胜率{win_rate:.0%})",
                    'trigger_conditions': [f"code={code}", f"time={time_bucket}"],
                    'evidence_count': len(group_trades),
                    'win_rate_when_triggered': round(win_rate, 3),
                    'win_rate_baseline': 0.50,
                    'avg_pnl': round(avg_pnl, 2),
                    'actionable_rule': f"{code} {time_bucket}时段减少或禁止开仓",
                    'severity': 'HIGH' if win_rate < 0.25 else 'MEDIUM',
                    'severity_score': (0.50 - win_rate) * len(group_trades),
                    'suggested_action': 'avoid' if win_rate < 0.25 else 'reduce_size',
                    'category': 'stock_time',
                })
        
        return patterns
    
    def _mine_stock_signal_patterns(self, trades: List[Dict]) -> List[Dict]:
        """挖掘「个股 × 信号置信度」模式"""
        patterns = []
        
        groups = defaultdict(list)
        for t in trades:
            code = t.get('code', '')
            confidence = t.get('signal_confidence', 0) or 0
            if not code:
                continue
            
            # 分组: high (>=0.7) / medium (0.5-0.7) / low (<0.5)
            if confidence >= 0.7:
                bucket = 'high_confidence'
            elif confidence >= 0.5:
                bucket = 'medium_confidence'
            else:
                bucket = 'low_confidence'
            
            groups[(code, bucket)].append(t)
        
        for (code, bucket), group_trades in groups.items():
            if len(group_trades) < self.MIN_EVIDENCE:
                continue
            
            winning = [t for t in group_trades if t.get('pnl', 0) > 0]
            win_rate = len(winning) / len(group_trades)
            
            # 高置信度但低胜率 → 信号不可靠
            if bucket == 'high_confidence' and win_rate < 0.40:
                avg_pnl = sum(t.get('pnl', 0) for t in group_trades) / len(group_trades)
                patterns.append({
                    'pattern': f"{code} 高置信度信号(>=0.7) → {len(winning)}/{len(group_trades)} 盈利 (胜率{win_rate:.0%})",
                    'trigger_conditions': [f"code={code}", f"signal_confidence>=0.7"],
                    'evidence_count': len(group_trades),
                    'win_rate_when_triggered': round(win_rate, 3),
                    'win_rate_baseline': 0.50,
                    'avg_pnl': round(avg_pnl, 2),
                    'actionable_rule': f"{code} 高置信度信号实际胜率低，需额外确认条件",
                    'severity': 'HIGH',
                    'severity_score': (0.50 - win_rate) * len(group_trades) * 1.5,
                    'suggested_action': 'add_guard',
                    'category': 'stock_signal',
                })
            
            # 低置信度但高胜率 → 可能是好机会被过滤
            elif bucket == 'low_confidence' and win_rate > 0.60:
                patterns.append({
                    'pattern': f"{code} 低置信度信号(<0.5) → {len(winning)}/{len(group_trades)} 盈利 (胜率{win_rate:.0%})",
                    'trigger_conditions': [f"code={code}", f"signal_confidence<0.5"],
                    'evidence_count': len(group_trades),
                    'win_rate_when_triggered': round(win_rate, 3),
                    'win_rate_baseline': 0.50,
                    'actionable_rule': f"{code} 低置信度信号实际胜率高，考虑放宽过滤",
                    'severity': 'LOW',
                    'severity_score': (win_rate - 0.50) * len(group_trades),
                    'suggested_action': 'review_filter',
                    'category': 'stock_signal',
                })
        
        return patterns
    
    def _mine_stop_loss_patterns(self, trades: List[Dict]) -> List[Dict]:
        """挖掘止损有效性模式"""
        patterns = []
        
        stopped_out = [t for t in trades if t.get('exit_reason') and '止损' in str(t.get('exit_reason', ''))]
        
        if len(stopped_out) < self.MIN_EVIDENCE:
            return patterns
        
        # 分析止损后的表现（如果有后续数据）
        # 这里简化：看止损是否频繁发生在 entry 后很短时间内
        quick_stops = [t for t in stopped_out if t.get('holding_period') is not None and t.get('holding_period', 999) < 15]
        
        if len(quick_stops) >= self.MIN_EVIDENCE:
            ratio = len(quick_stops) / len(stopped_out)
            patterns.append({
                'pattern': f"止损 {len(quick_stops)}/{len(stopped_out)} 发生在入场后15min内 (占比{ratio:.0%})",
                'trigger_conditions': ["exit_reason=止损", "holding_period<15min"],
                'evidence_count': len(stopped_out),
                'win_rate_when_triggered': 0.0,  # 止损=亏损
                'actionable_rule': f"止损触发过快，可能太紧，考虑从固定%改为ATR止损",
                'severity': 'MEDIUM' if ratio > 0.5 else 'LOW',
                'severity_score': ratio * len(stopped_out),
                'suggested_action': 'adjust_sl',
                'category': 'stop_loss',
            })
        
        return patterns
    
    def _mine_strategy_patterns(self, trades: List[Dict]) -> List[Dict]:
        """挖掘策略胜率模式"""
        patterns = []
        
        groups = defaultdict(list)
        for t in trades:
            strategy = t.get('strategy', 'unknown')
            groups[strategy].append(t)
        
        for strategy, group_trades in groups.items():
            if len(group_trades) < self.MIN_EVIDENCE:
                continue
            
            winning = [t for t in group_trades if t.get('pnl', 0) > 0]
            win_rate = len(winning) / len(group_trades)
            total_pnl = sum(t.get('pnl', 0) for t in group_trades)
            
            if win_rate < 0.35:
                patterns.append({
                    'pattern': f"策略「{strategy}」→ {len(winning)}/{len(group_trades)} 盈利 (胜率{win_rate:.0%}, 总盈亏{total_pnl:.0f})",
                    'trigger_conditions': [f"strategy={strategy}"],
                    'evidence_count': len(group_trades),
                    'win_rate_when_triggered': round(win_rate, 3),
                    'win_rate_baseline': 0.50,
                    'total_pnl': round(total_pnl, 2),
                    'actionable_rule': f"策略「{strategy}」胜率过低，建议暂停或优化",
                    'severity': 'HIGH' if total_pnl < 0 else 'MEDIUM',
                    'severity_score': (0.50 - win_rate) * len(group_trades) * (2 if total_pnl < 0 else 1),
                    'suggested_action': 'avoid' if total_pnl < 0 else 'review_filter',
                    'category': 'strategy',
                })
        
        return patterns
    
    def _deduplicate(self, patterns: List[Dict]) -> List[Dict]:
        """去重：保留 severity_score 最高的"""
        seen = {}
        for p in patterns:
            key = p.get('pattern', '')[:50]  # 前50字符作为key
            if key not in seen or p.get('severity_score', 0) > seen[key].get('severity_score', 0):
                seen[key] = p
        return list(seen.values())
    
    def save_to_knowledge_db(self, patterns: List[Dict], db_path: str = '/opt/ftseek/FTSeek/data/knowledge.db'):
        """保存模式到 knowledge.db"""
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
        
            today = datetime.now().strftime('%Y-%m-%d')
        
            for i, p in enumerate(patterns):
                pattern_id = f"pattern_{today}_{i}"
                c.execute("""INSERT OR REPLACE INTO patterns 
                    (id, title, description, evidence_count, first_seen, last_seen, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                    pattern_id,
                    p.get('pattern', '')[:200],
                    p.get('actionable_rule', ''),
                    p.get('evidence_count', 0),
                    today,
                    today,
                    1.0 - p.get('win_rate_when_triggered', 0.5),
                ))
        
            conn.commit()
        logger.info(f"[PatternMiner] 保存 {len(patterns)} 个模式到 knowledge.db")


def get_pattern_miner() -> PatternMiner:
    """获取 PatternMiner 单例"""
    return PatternMiner()
