#!/usr/bin/env python3
"""
每周归因优化脚本

功能:
1. 扫描trade_attribution表过去7天数据
2. 识别低效策略/场景组合
3. 生成unified_config.yaml调整建议
4. 钉钉推送优化报告

使用方式:
    python -m scripts.weekly_attribution_optimizer
    
或者添加到crontab:
    0 9 * * 1 cd /opt/ftseek/FTSeek && python -m scripts.weekly_attribution_optimizer
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class WeeklyAttributionOptimizer:
    """每周归因优化器"""
    
    def __init__(self, db=None, notifier=None):
        """
        初始化优化器
        
        Args:
            db: 数据库连接
            notifier: 钉钉通知器
        """
        self.db = db
        self.notifier = notifier
        self.min_trades_for_analysis = 5  # 至少5笔交易才分析
        self.low_win_rate_threshold = 0.30  # 胜率低于30%
        self.high_win_rate_threshold = 0.70  # 胜率高于70%
        self.good_sharpe_threshold = 1.5  # 夏普比率高于1.5
        
    def run_weekly_optimization(self) -> Dict:
        """
        执行每周优化分析
        
        Returns:
            优化报告字典
        """
        logger.info("=" * 60)
        logger.info("[每周优化] 开始执行归因优化分析")
        logger.info("=" * 60)
        
        try:
            # 1. 数据提取
            trades = self._query_trades_last_7_days()
            if not trades:
                logger.info("[每周优化] 过去7天无交易数据，跳过分析")
                return None
            
            logger.info(f"[每周优化] 提取到 {len(trades)} 笔交易")
            
            # 2. 策略×场景矩阵分析
            matrix = self._analyze_strategy_scene_matrix(trades)
            
            # 3. 生成调整建议
            suggestions = self._generate_suggestions(matrix)
            
            # 4. LLM一致性检查
            llm_issues = self._check_llm_consistency_issues(trades)
            
            # 5. 执行质量分析
            execution_quality = self._analyze_execution_quality(trades)
            
            # 6. 生成报告
            report = {
                "report_date": datetime.now().strftime("%Y-W%W"),
                "period_start": (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
                "period_end": datetime.now().strftime("%Y-%m-%d"),
                "total_trades": len(trades),
                "suggestions": suggestions,
                "llm_issues": llm_issues,
                "execution_quality": execution_quality,
                "matrix": matrix,
            }
            
            # 7. 钉钉推送
            if self.notifier:
                report_text = self._format_report(report)
                try:
                    self.notifier.send_message(report_text, title=f"每周归因优化报告 {report['report_date']}")
                    logger.info("[每周优化] 报告已推送到钉钉")
                except Exception as e:
                    logger.error(f"[每周优化] 钉钉推送失败: {e}")
            
            # 8. 保存到数据库
            if self.db:
                try:
                    self.db.save_weekly_optimization_report(report)
                    logger.info("[每周优化] 报告已保存到数据库")
                except Exception as e:
                    logger.error(f"[每周优化] 数据库保存失败: {e}")
            
            logger.info("=" * 60)
            logger.info("[每周优化] 分析完成")
            logger.info("=" * 60)
            
            return report
            
        except Exception as e:
            logger.error(f"[每周优化] 执行失败: {e}", exc_info=True)
            return None
    
    def _query_trades_last_7_days(self) -> List[Dict]:
        """查询过去7天的交易数据"""
        if not self.db:
            return []
        
        try:
            from app.storage.database import Database
            
            # 查询trade_attribution表
            cutoff_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            
            query = """
                SELECT 
                    ta.trace_id,
                    ta.symbol,
                    ta.strategy_mode,
                    ta.primary_scene,
                    ta.pnl,
                    ta.pnl_percent,
                    ta.direction_correct,
                    ta.scene_correct,
                    ta.overall_score,
                    ta.result_tags,
                    ta.strategy_tags,
                    ta.execution_quality,
                    ta.slippage_bps,
                    ta.holding_period_minutes,
                    te.llm_confidence,
                    te.trading_session,
                    te.timestamp_open
                FROM trade_attribution ta
                LEFT JOIN trade_evidence te ON ta.trace_id = te.trace_id
                WHERE te.timestamp_open >= ?
                ORDER BY te.timestamp_open DESC
            """
            
            return self.db.execute_query(query, (cutoff_date,))
            
        except Exception as e:
            logger.error(f"[每周优化] 查询交易数据失败: {e}")
            return []
    
    def _analyze_strategy_scene_matrix(self, trades: List[Dict]) -> Dict:
        """
        分析策略×场景矩阵
        
        Returns:
            {(strategy, scene): {trade_count, win_rate, avg_pnl, sharpe, ...}}
        """
        # 按策略×场景分组
        groups = defaultdict(list)
        
        for trade in trades:
            strategy = trade.get('strategy_mode') or trade.get('primary_strategy', 'unknown')
            scene = trade.get('primary_scene', 'unknown')
            groups[(strategy, scene)].append(trade)
        
        # 计算统计指标
        matrix = {}
        
        for (strategy, scene), group_trades in groups.items():
            trade_count = len(group_trades)
            
            # 计算胜率
            wins = sum(1 for t in group_trades if t.get('pnl', 0) > 0)
            win_rate = wins / trade_count if trade_count > 0 else 0
            
            # 计算平均盈亏
            pnl_values = [t.get('pnl_percent', 0) or 0 for t in group_trades]
            avg_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else 0
            
            # 计算夏普比率（简化版）
            if len(pnl_values) > 1:
                import statistics
                avg_return = statistics.mean(pnl_values)
                std_return = statistics.stdev(pnl_values) if len(pnl_values) > 1 else 0
                sharpe = (avg_return / std_return) if std_return > 0 else 0
            else:
                sharpe = 0
            
            # 计算方向正确率
            direction_correct_count = sum(1 for t in group_trades if t.get('direction_correct'))
            direction_accuracy = direction_correct_count / trade_count if trade_count > 0 else 0
            
            # 计算平均滑点
            slippage_values = [t.get('slippage_bps', 0) or 0 for t in group_trades]
            avg_slippage = sum(slippage_values) / len(slippage_values) if slippage_values else 0
            
            matrix[(strategy, scene)] = {
                'trade_count': trade_count,
                'win_rate': win_rate,
                'avg_pnl': avg_pnl,
                'sharpe': sharpe,
                'direction_accuracy': direction_accuracy,
                'avg_slippage': avg_slippage,
                'total_pnl': sum(pnl_values),
            }
        
        return matrix
    
    def _generate_suggestions(self, matrix: Dict) -> List[Dict]:
        """生成调整建议"""
        suggestions = []
        
        for (strategy, scene), stats in matrix.items():
            if stats['trade_count'] < self.min_trades_for_analysis:
                continue
            
            # 降权建议：胜率低
            if stats['win_rate'] < self.low_win_rate_threshold:
                suggestions.append({
                    "type": "strategy_weight_decrease",
                    "strategy": strategy,
                    "scene": scene,
                    "current_weight": None,  # 需要从config读取
                    "suggested_change": "-15%",
                    "reason": f"胜率{stats['win_rate']:.1%}低于阈值{self.low_win_rate_threshold:.0%} ({stats['trade_count']}笔)",
                    "priority": "high",
                })
            
            # 加权建议：胜率高且夏普好
            elif (stats['win_rate'] > self.high_win_rate_threshold and 
                  stats['sharpe'] > self.good_sharpe_threshold):
                suggestions.append({
                    "type": "strategy_weight_increase",
                    "strategy": strategy,
                    "scene": scene,
                    "current_weight": None,
                    "suggested_change": "+15%",
                    "reason": f"胜率{stats['win_rate']:.1%}，夏普{stats['sharpe']:.2f} ({stats['trade_count']}笔)",
                    "priority": "high",
                })
            
            # 执行质量警告：滑点高
            if stats['avg_slippage'] > 5:  # >5bps
                suggestions.append({
                    "type": "execution_improvement",
                    "strategy": strategy,
                    "scene": scene,
                    "issue": "high_slippage",
                    "avg_slippage": stats['avg_slippage'],
                    "suggestion": "考虑使用限价单代替市价单",
                    "priority": "medium",
                })
        
        return suggestions
    
    def _check_llm_consistency_issues(self, trades: List[Dict]) -> List[Dict]:
        """检查LLM一致性问题"""
        issues = []
        
        for trade in trades:
            llm_confidence = trade.get('llm_confidence') or 0
            pnl = trade.get('pnl_percent') or 0
            
            # 规则1: 过度自信（高置信度但亏损）
            if llm_confidence > 0.8 and pnl < -2:
                issues.append({
                    "type": "llm_overconfident",
                    "trace_id": trade.get('trace_id', 'unknown'),
                    "symbol": trade.get('symbol', 'unknown'),
                    "llm_confidence": llm_confidence,
                    "pnl": pnl,
                    "severity": "high",
                    "suggestion": "降低LLM权重或增加置信度校准",
                })
            
            # 规则2: 方向错误（方向判断错误）
            if trade.get('direction_correct') == False and llm_confidence > 0.7:
                issues.append({
                    "type": "llm_direction_wrong",
                    "trace_id": trade.get('trace_id', 'unknown'),
                    "symbol": trade.get('symbol', 'unknown'),
                    "llm_confidence": llm_confidence,
                    "severity": "medium",
                    "suggestion": "检查LLM推理逻辑或增加风控检查",
                })
        
        return issues
    
    def _analyze_execution_quality(self, trades: List[Dict]) -> Dict:
        """分析执行质量"""
        # 按时段分析滑点
        session_slippage = defaultdict(list)
        
        for trade in trades:
            session = trade.get('trading_session', 'unknown')
            slippage = trade.get('slippage_bps', 0) or 0
            session_slippage[session].append(slippage)
        
        # 计算各时段平均滑点
        session_stats = {}
        for session, slippages in session_slippage.items():
            avg_slippage = sum(slippages) / len(slippages)
            session_stats[session] = {
                'trade_count': len(slippages),
                'avg_slippage': avg_slippage,
                'max_slippage': max(slippages),
                'min_slippage': min(slippages),
            }
        
        # 总体统计
        all_slippages = [t.get('slippage_bps', 0) or 0 for t in trades]
        
        return {
            'overall_avg_slippage': sum(all_slippages) / len(all_slippages) if all_slippages else 0,
            'session_stats': session_stats,
            'high_slippage_trades': sum(1 for s in all_slippages if s > 5),
        }
    
    def _format_report(self, report: Dict) -> str:
        """格式化报告为钉钉消息"""
        lines = []
        
        # 标题
        lines.append(f"**每周归因优化报告 ({report['report_date']})**")
        lines.append(f"分析周期: {report['period_start']} ~ {report['period_end']}")
        lines.append(f"总交易数: {report['total_trades']}笔")
        lines.append("")
        
        # 策略权重调整建议
        suggestions = report.get('suggestions', [])
        if suggestions:
            lines.append("**策略权重调整建议**")
            lines.append("")
            lines.append("| 策略 | 场景 | 当前权重 | 建议权重 | 原因 |")
            lines.append("|------|------|---------|---------|------|")
            
            for sug in suggestions:
                if sug['type'] in ['strategy_weight_decrease', 'strategy_weight_increase']:
                    current = sug.get('current_weight', 'N/A')
                    if current is None:
                        current = "待确认"
                    suggested = f"{current} {sug['suggested_change']}" if isinstance(current, str) else sug['suggested_change']
                    
                    lines.append(
                        f"| {sug['strategy']} | {sug['scene']} | "
                        f"{current} | {suggested} | {sug['reason']} |"
                    )
            
            lines.append("")
        
        # LLM一致性检查
        llm_issues = report.get('llm_issues', [])
        if llm_issues:
            lines.append("**LLM一致性检查**")
            lines.append("")
            lines.append(f"- 发现问题: {len(llm_issues)}笔")
            
            for issue in llm_issues[:5]:  # 最多显示5个
                pnl_val = issue.get('pnl', 0)
                lines.append(
                    f"- {issue['trace_id']}: {issue['type']} "
                    f"(置信度{issue['llm_confidence']:.2f}, 盈亏{pnl_val:.2f}%)"
                )
            
            lines.append("")
        
        # 执行质量
        exec_quality = report.get('execution_quality', {})
        if exec_quality:
            lines.append("**执行质量**")
            lines.append("")
            lines.append(f"- 平均滑点: {exec_quality.get('overall_avg_slippage', 0):.1f}bps")
            lines.append(f"- 高滑点交易(>5bps): {exec_quality.get('high_slippage_trades', 0)}笔")
            
            # 时段滑点
            session_stats = exec_quality.get('session_stats', {})
            if session_stats:
                lines.append("- 时段滑点对比:")
                for session, stats in session_stats.items():
                    lines.append(
                        f"  - {session}: {stats['avg_slippage']:.1f}bps "
                        f"({stats['trade_count']}笔)"
                    )
            
            lines.append("")
        
        # 总结
        lines.append("---")
        lines.append("报告由FTSeek归因系统自动生成")
        
        return "\n".join(lines)


def main():
    """主函数 - 独立运行入口"""
    import sys
    import os
    
    # 添加项目根目录到Python路径
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('logs/weekly_optimization.log', encoding='utf-8')
        ]
    )
    
    # 初始化数据库和通知器
    try:
        from app.storage.database import get_database
        from app.notifier.dingtalk import DingTalkNotifier
        
        db = get_database()
        notifier = DingTalkNotifier()
        
        optimizer = WeeklyAttributionOptimizer(db=db, notifier=notifier)
        report = optimizer.run_weekly_optimization()
        
        if report:
            logger.info(f"[每周优化] 报告生成成功: {report['report_date']}")
        else:
            logger.warning("[每周优化] 报告生成失败")
        
    except Exception as e:
        logger.error(f"[每周优化] 初始化失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
