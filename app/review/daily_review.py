"""
每日复盘生成器

生成每日交易复盘报告，包含 6 大维度分析
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from pathlib import Path
import json
import sqlite3

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek.review.daily")
logger.propagate = False

# ET timezone for US market date calculations
ET_TZ = timezone(timedelta(hours=-4))  # EDT (UTC-4)


def _get_et_date() -> str:
    """Get current date in ET timezone"""
    return datetime.now(ET_TZ).strftime('%Y-%m-%d')


class DailyReviewGenerator:
    """每日复盘生成器"""
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
        self.output_dir = Path('/opt/ftseek/FTSeek/data/daily_reviews')
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self,
                 date: str = None,
                 account_type: str = None) -> Dict[str, Any]:
        """
        生成每日复盘报告
        
        Args:
            date: 日期 YYYY-MM-DD，默认今天
            account_type: 账户类型 real/simulation，默认全部
        
        Returns:
            复盘报告字典
        """
        if date is None:
            date = _get_et_date()
        
        logger.info(f"生成每日复盘报告：{date}")
        
        # 获取当日交易记录
        trades = self.trade_recorder.get_records_by_date(date)
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        # 已平仓交易
        closed_trades = [t for t in trades if t.exit_price is not None]
        
        # 基础统计
        report = {
            'report_type': 'daily',
            'report_date': date,
            'generated_at': datetime.now().isoformat(),
            'account_type': account_type or 'all',
            'summary': self._generate_summary(trades, closed_trades),
            'decision_analysis': self._analyze_decisions(closed_trades),
            'timing_analysis': self._analyze_timing(closed_trades),
            'position_analysis': self._analyze_positions(closed_trades),
            'risk_analysis': self._analyze_risk(closed_trades),
            'psychology_analysis': self._analyze_psychology(closed_trades),
            'key_learnings': self._extract_learnings(closed_trades),
            'improvement_suggestions': self._generate_suggestions(closed_trades),
            'best_trade': self._find_best_trade(closed_trades),
            'worst_trade': self._find_worst_trade(closed_trades)
        }
        
        # 保存报告
        self._save_report(report, date)
        
        logger.info(f"每日复盘报告生成完成：{len(closed_trades)} 笔交易")
        return report
    
    def _generate_summary(self, all_trades: List, closed_trades: List) -> Dict:
        """生成基础统计摘要"""
        if not closed_trades:
            return {
                'total_trades': len(all_trades),
                'closed_trades': 0,
                'total_pnl': 0,
                'win_rate': 0,
                'avg_pnl': 0
            }
        
        total_pnl = sum(t.pnl for t in closed_trades if t.pnl)
        winning = [t for t in closed_trades if t.pnl and t.pnl > 0]
        losing = [t for t in closed_trades if t.pnl and t.pnl < 0]
        
        return {
            'total_trades': len(all_trades),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'breakeven_trades': len(closed_trades) - len(winning) - len(losing),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(closed_trades), 4) if closed_trades else 0,
            'avg_pnl': round(total_pnl / len(closed_trades), 2) if closed_trades else 0,
            'avg_holding_period': round(sum(t.holding_period or 0 for t in closed_trades) / len(closed_trades), 1)
        }
    
    def _analyze_decisions(self, trades: List) -> Dict:
        """决策维度分析"""
        if not trades:
            return {}
        
        # 决策原因分布
        decision_counts = {}
        for t in trades:
            reason = t.decision_reason or 'unknown'
            decision_counts[reason] = decision_counts.get(reason, 0) + 1
        
        # 决策信心统计
        avg_confidence = sum(t.decision_confidence or 5 for t in trades) / len(trades)
        avg_quality = sum(t.decision_quality_score or 5 for t in trades) / len(trades)
        
        # 冲动交易识别
        impulse_trades = [t for t in trades if t.decision_reason == 'impulse']
        
        return {
            'decision_reason_distribution': decision_counts,
            'avg_decision_confidence': round(avg_confidence, 2),
            'avg_decision_quality': round(avg_quality, 2),
            'impulse_trade_count': len(impulse_trades),
            'impulse_trade_ratio': round(len(impulse_trades) / len(trades), 4),
            'decision_quality_issues': [
                t for t in trades if (t.decision_quality_score or 5) < 5
            ]
        }
    
    def _analyze_timing(self, trades: List) -> Dict:
        """时机维度分析"""
        if not trades:
            return {}
        
        avg_entry_score = sum(t.entry_timing_score or 5 for t in trades) / len(trades)
        avg_exit_score = sum(t.exit_timing_score or 5 for t in trades) / len(trades)
        
        # 时机问题识别
        poor_entry = [t for t in trades if (t.entry_timing_score or 5) < 5]
        poor_exit = [t for t in trades if (t.exit_timing_score or 5) < 5]
        
        return {
            'avg_entry_timing_score': round(avg_entry_score, 2),
            'avg_exit_timing_score': round(avg_exit_score, 2),
            'timing_score_gap': round(avg_entry_score - avg_exit_score, 2),
            'poor_entry_count': len(poor_entry),
            'poor_exit_count': len(poor_exit),
            'timing_notes': [t.timing_notes for t in trades if t.timing_notes]
        }
    
    def _analyze_positions(self, trades: List) -> Dict:
        """持仓维度分析"""
        if not trades:
            return {}
        
        avg_position_size = sum(t.position_size_actual or 0 for t in trades) / len(trades)
        avg_adjustments = sum(t.position_adjustments or 0 for t in trades) / len(trades)
        
        # 仓位问题识别
        oversized = [t for t in trades if (t.position_size_actual or 0) > 0.3]
        frequent_adjustments = [t for t in trades if (t.position_adjustments or 0) > 2]
        
        return {
            'avg_position_size': round(avg_position_size, 4),
            'avg_position_adjustments': round(avg_adjustments, 2),
            'oversized_position_count': len(oversized),
            'frequent_adjustment_count': len(frequent_adjustments),
            'position_notes': [t.position_notes for t in trades if t.position_notes]
        }
    
    def _analyze_risk(self, trades: List) -> Dict:
        """风控维度分析"""
        if not trades:
            return {}
        
        # 止损执行率
        stop_loss_trades = [t for t in trades if t.stop_loss_planned and t.stop_loss_planned > 0]
        executed = [t for t in stop_loss_trades if t.stop_loss_executed]
        stop_loss_rate = len(executed) / len(stop_loss_trades) if stop_loss_trades else 1.0
        
        # 止损调整
        adjusted = [t for t in trades if t.stop_loss_adjusted]
        
        # 风险收益比
        avg_rr = sum(t.risk_reward_actual or 0 for t in trades) / len(trades)
        
        return {
            'stop_loss_planned_count': len(stop_loss_trades),
            'stop_loss_executed_count': len(executed),
            'stop_loss_execution_rate': round(stop_loss_rate, 4),
            'stop_loss_adjusted_count': len(adjusted),
            'avg_risk_reward_ratio': round(avg_rr, 2),
            'risk_notes': [t.risk_notes for t in trades if t.risk_notes]
        }
    
    def _analyze_psychology(self, trades: List) -> Dict:
        """心理维度分析"""
        if not trades:
            return {}
        
        # 情绪分布
        emotion_counts = {}
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
        
        # 情绪强度
        avg_intensity = sum(t.emotion_intensity or 3 for t in trades) / len(trades)
        
        # 信心变化
        avg_confidence_entry = sum(t.confidence_entry or 5 for t in trades) / len(trades)
        avg_confidence_exit = sum(t.confidence_exit or 5 for t in trades) / len(trades)
        
        # 心理备注
        psych_notes = [t.psychological_notes for t in trades if t.psychological_notes]
        
        return {
            'emotion_distribution': emotion_counts,
            'avg_emotion_intensity': round(avg_intensity, 2),
            'avg_confidence_entry': round(avg_confidence_entry, 2),
            'avg_confidence_exit': round(avg_confidence_exit, 2),
            'confidence_change': round(avg_confidence_exit - avg_confidence_entry, 2),
            'psychological_notes': psych_notes
        }
    
    def _extract_learnings(self, trades: List) -> List[str]:
        """提取关键学习"""
        learnings = []
        
        for t in trades:
            if t.lessons_learned:
                learnings.append(t.lessons_learned)
            if t.review_notes:
                learnings.append(t.review_notes)
        
        # 去重
        return list(set(learnings))[:10]
    
    def _generate_suggestions(self, trades: List) -> List[str]:
        """生成改进建议"""
        suggestions = []
        
        if not trades:
            return suggestions
        
        # 基于统计的建议
        summary = self._generate_summary(trades, trades)
        
        if summary['win_rate'] < 0.4:
            suggestions.append("胜率低于 40%，建议重新评估策略有效性")
        
        if summary.get('impulse_trade_ratio', 0) > 0.3:
            suggestions.append("冲动交易比例超过 30%，建议建立交易前检查清单")
        
        # 止损执行率
        risk_analysis = self._analyze_risk(trades)
        if risk_analysis.get('stop_loss_execution_rate', 1) < 0.8:
            suggestions.append("止损执行率低于 80%，需严格执行止损纪律")
        
        # 连续亏损
        losing_streak = 0
        max_losing_streak = 0
        for t in trades:
            if t.pnl and t.pnl < 0:
                losing_streak += 1
                max_losing_streak = max(max_losing_streak, losing_streak)
            else:
                losing_streak = 0
        
        if max_losing_streak >= 3:
            suggestions.append(f"出现连续{max_losing_streak}笔亏损，考虑暂停交易，调整心态")
        
        return suggestions
    
    def _find_best_trade(self, trades: List) -> Optional[Dict]:
        """找出最佳交易"""
        if not trades:
            return None
        
        profitable = [t for t in trades if t.pnl and t.pnl > 0]
        if not profitable:
            return None
        
        best = max(profitable, key=lambda t: t.pnl)
        return {
            'trade_id': best.trade_id,
            'code': best.code,
            'pnl': round(best.pnl, 2),
            'pnl_percent': round(best.pnl_percent, 4) if best.pnl_percent else 0,
            'highlight': best.lessons_learned or '严格执行策略'
        }
    
    def _find_worst_trade(self, trades: List) -> Optional[Dict]:
        """找出最差交易"""
        if not trades:
            return None
        
        losing = [t for t in trades if t.pnl and t.pnl < 0]
        if not losing:
            return None
        
        worst = min(losing, key=lambda t: t.pnl)
        return {
            'trade_id': worst.trade_id,
            'code': worst.code,
            'pnl': round(worst.pnl, 2),
            'pnl_percent': round(worst.pnl_percent, 4) if worst.pnl_percent else 0,
            'issue': worst.lessons_learned or worst.review_notes or '需要复盘分析'
        }
    
    def _save_report(self, report: Dict, date: str):
        """保存报告到文件"""
        filename = self.output_dir / f"daily_review_{date}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"报告已保存：{filename}")
    
    def get_report(self, date: str) -> Optional[Dict]:
        """获取已生成的报告"""
        filename = self.output_dir / f"daily_review_{date}.json"
        
        if not filename.exists():
            return None
        
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)


# 便捷函数
def generate_daily_review(date: str = None, account_type: str = None) -> Dict:
    """生成每日复盘报告"""
    generator = DailyReviewGenerator()
    return generator.generate(date, account_type)


def _get_today_trades(date: str, market: str = "hk") -> List[Dict]:
    """获取今日交易记录 (按市场分离)
    
    美股使用 ET 日期，同时查询 positions + trade_records 表
    (trade_records 包含 scalping 交易)
    """
    try:
        # 美股使用 ET 日期
        if market.lower() == "us":
            target_date = _get_et_date()
            db_path = '/opt/ftseek/FTSeek/data/ftseek_us.db'
        else:
            target_date = date
            db_path = '/opt/ftseek/FTSeek/data/ftseek.db'
        
        trades = []
        
        # 1. 从 positions 表获取持仓交易
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT code, action, entry_price, entry_time, realized_pnl, status
                FROM positions 
                WHERE entry_time LIKE ?
            """, (f"{target_date}%",))
            rows = cursor.fetchall()
            trades.extend([dict(row) for row in rows])
        
            # 2. 从 trade_records 表获取 scalping 交易 (仅美股)
            if market.lower() == "us":
                try:
                    cursor.execute("""
                        SELECT symbol as code, side as action, entry_price, 
                               created_at as entry_time, realized_pnl, status,
                               source_type
                        FROM trade_records 
                        WHERE date(trade_date) = ?
                    """, (target_date,))
                    scalping_rows = cursor.fetchall()
                    for row in scalping_rows:
                        d = dict(row)
                        d['is_scalping'] = d.pop('source_type', '') == 'SCALPING'
                        trades.append(d)
                except Exception as e:
                    logger.warning(f"查询 trade_records 失败: {e}")
        
        logger.info(f"[{market}] 获取 {len(trades)} 条交易记录 (date={target_date})")
        return trades
    except Exception as e:
        logger.warning(f"获取 {market} 交易记录失败: {e}")
        return []


def _get_today_battle_plan(market: str = "hk") -> Dict:
    """获取今日作战计划 (按市场分离)"""
    try:
        if market.lower() == "us":
            plan_file = '/opt/ftseek/FTSeek/data/daily_battle_plan_us.json'
        else:
            plan_file = '/opt/ftseek/FTSeek/data/daily_battle_plan.json'
        
        if os.path.exists(plan_file):
            with open(plan_file, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"获取 {market} 作战计划失败: {e}")
    return {}


def run_post_market_review():
    """
    盘后复盘入口 (16:30触发)
    
    可被crontab或状态机调用
    
    功能:
    1. 生成信号漏斗报告 (今日所有信号 + 拦截原因)
    2. 调用AttributionEngine生成归因复盘报告
    3. 合并报告推送钉钉
    4. 保存报告
    
    Returns:
        复盘报告字典
    """
    from datetime import datetime
    # 美股盘后复盘使用 ET 日期
    today = _get_et_date()
    logger.info(f"[盘后复盘] 开始生成收盘报告 (ET date: {today})...")
    
    try:
        # ── 1. Agent 盘后归因 (新增 - 2026-04-24) ──
        agent_report = {}
        try:
            from app.llm.agent_bridge import get_ftseek_agent_bridge
            
            # 港股 Agent
            hk_bridge = get_ftseek_agent_bridge("hk")
            hk_trades = _get_today_trades(today, market="hk")
            hk_plan = _get_today_battle_plan(market="hk")
            agent_report["hk"] = hk_bridge.generate_eod_review(hk_trades, hk_plan)
            
            # 美股 Agent
            us_bridge = get_ftseek_agent_bridge("us")
            us_trades = _get_today_trades(today, market="us")
            us_plan = _get_today_battle_plan(market="us")
            agent_report["us"] = us_bridge.generate_eod_review(us_trades, us_plan)
            
            logger.info("[盘后复盘] Agent 归因报告已生成 (双市场独立)")
        except Exception as e:
            logger.warning(f"[盘后复盘] Agent 归因失败: {e}")
            agent_report = {"error": str(e)}
        
        # ── 2. 计划对比 ──
        try:
            from app.review.plan_vs_actual import generate_plan_vs_actual_report
            plan_vs_actual_md = generate_plan_vs_actual_report(today)
            logger.info("[盘后复盘] 计划对比报告已生成")
        except Exception as e:
            logger.warning(f"[盘后复盘] 计划对比生成失败: {e}")
            plan_vs_actual_md = f"## 📊 FTSeek 计划复盘 | {today}\n\n### ❌ 报告生成失败: {e}"
        
        # ── 2. 信号漏斗 ──
        try:
            from app.review.signal_funnel import format_full_daily_report
            full_report_md = format_full_daily_report(today)
            logger.info("[盘后复盘] 信号漏斗报告已生成")
        except Exception as e:
            logger.warning(f"[盘后复盘] 信号漏斗生成失败: {e}")
            full_report_md = f"## 📊 FTSeek 收盘报告 | {today}\n\n### ❌ 信号漏斗读取失败: {e}"
        
        # ── 2. 归因引擎 (原有逻辑) ──
        try:
            from app.evidence.attribution_engine import AttributionEngine
            from app.storage.database import get_database
            
            try:
                db = get_database()
            except Exception as e:
                logger.warning(f"[盘后复盘] 数据库初始化失败: {e}")
                db = None
            
            engine = AttributionEngine(db=db, enable_llm_attribution=True)
            report = engine.generate_post_market_review()
            
            # 追加归因内容到完整报告
            attribution_md = _format_review_to_markdown(report)
            full_report_md = plan_vs_actual_md + "\n---\n" + full_report_md + "\n---\n" + attribution_md
            
            logger.info(
                f"[盘后复盘] 完成 | "
                f"交易{report['summary']['total_trades']}笔 | "
                f"胜率{report['summary']['win_rate']:.0%} | "
                f"盈亏{report['summary']['total_pnl']:+.2f}"
            )
        except Exception as e:
            logger.warning(f"[盘后复盘] 归因引擎失败 (继续推送计划对比和信号漏斗): {e}")
            # 归因失败时，仍然保留计划对比报告
            full_report_md = plan_vs_actual_md + "\n---\n" + full_report_md
        
        # ── 3. 推送钉钉 ──
        try:
            from app.notifier.dingtalk import get_dingtalk_notifier
            notifier = get_dingtalk_notifier()
            if notifier:
                # 钉钉 Markdown 消息有长度限制 (~20000 chars)
                # 如果超长则截断
                max_len = 18000
                if len(full_report_md) > max_len:
                    full_report_md = full_report_md[:max_len] + "\n\n_... 报告过长已截断，请查看本地日志获取完整内容。_"
                
                notifier.send_text(full_report_md)
                logger.info("[盘后复盘] 已推送钉钉")
            else:
                logger.warning("[盘后复盘] 钉钉通知器未初始化，跳过推送")
        except Exception as e:
            logger.error(f"[盘后复盘] 钉钉推送失败: {e}", exc_info=True)
        
        return report if 'report' in locals() else {'error': str(e) if 'e' in locals() else 'attribution_failed'}
        
    except Exception as e:
        logger.error(f"[盘后复盘] 生成失败: {e}", exc_info=True)
        raise


def _format_review_to_markdown(report: Dict) -> str:
    """将复盘报告转换为Markdown格式"""
    summary = report['summary']
    total_trades = summary['total_trades']
    
    # 根据交易数量调整标题
    if total_trades == 0:
        title = "## 📊 盘后归因复盘 (今日无交易)\n"
    else:
        title = "## 📊 盘后归因复盘\n"
    
    lines = [
        title,
        f"**日期**: {report['date']}",
        f"**总交易**: {total_trades}笔",
    ]
    
    # 有交易时才显示胜率和盈亏
    if total_trades > 0:
        win_rate = summary['win_rate']
        total_pnl = summary['total_pnl']
        total_pnl_percent = summary['total_pnl_percent']
        
        # 胜率图标
        win_icon = "✅" if win_rate >= 0.6 else "⚠️" if win_rate >= 0.5 else "❌"
        # 盈亏图标
        pnl_icon = "📈" if total_pnl > 0 else "📉" if total_pnl < 0 else "➡️"
        
        lines.append(f"**胜率**: {win_icon} {win_rate:.0%}")
        lines.append(f"**总盈亏**: {pnl_icon} {total_pnl:+.2f} ({total_pnl_percent:+.2%})")
    else:
        lines.append("**胜率**: - (无交易)")
        lines.append("**总盈亏**: 0.00 (0.00%)")
    
    lines.append("")  # 空行
    
    # 策略表现 (有交易时)
    if total_trades > 0 and report.get('strategy_performance'):
        lines.append("### 📈 策略表现")
        for strategy, perf in report['strategy_performance'].items():
            trades = perf['trades']
            win_rate = perf['win_rate']
            avg_pnl = perf['avg_pnl']
            
            # 策略图标
            icon = "✅" if win_rate >= 0.6 else "⚠️" if win_rate >= 0.5 else "❌"
            
            lines.append(
                f"{icon} **{strategy}**: "
                f"{trades}笔 | "
                f"胜率{win_rate:.0%} | "
                f"平均{avg_pnl:+.2f}"
            )
        lines.append("")  # 空行
    
    # 优化建议 (始终显示)
    if report.get('optimization_suggestions'):
        lines.append("### 💡 优化建议")
        for i, suggestion in enumerate(report['optimization_suggestions'][:5], 1):
            lines.append(f"{i}. {suggestion}")
        lines.append("")  # 空行
    
    # 无交易时的特殊提示
    if total_trades == 0 and not report.get('optimization_suggestions'):
        lines.append("### 💡 提示")
        lines.append("今日无交易，建议检查市场信号和策略参数\n")
    
    # 交易明细 (有交易时，最多显示5笔)
    if total_trades > 0 and report.get('trades'):
        lines.append("### 📝 交易明细 (最多5笔)")
        for i, trade in enumerate(report['trades'][:5], 1):
            symbol = trade.get('symbol', 'unknown')
            action = trade.get('action', 'UNKNOWN')
            result = trade.get('result', 'unknown')
            pnl = trade.get('pnl', 0.0)
            
            # 交易图标
            icon = "✅" if result == 'win' else "❌"
            action_icon = "🟢" if 'BUY' in action else "🔴"
            
            lesson = trade.get('lesson', '')
            
            lines.append(
                f"{icon} {action_icon} **{symbol}** {action} | "
                f"盈亏: {pnl:+.2f}"
            )
            if lesson:
                lines.append(f"   _{lesson}_")
        
        if total_trades > 5:
            lines.append(f"\n_... 及其他 {total_trades - 5} 笔交易_")
        
        lines.append("")  # 空行
    
    lines.append(f"_生成时间: {report['generated_at']}_")
    
    return "\n".join(lines)
