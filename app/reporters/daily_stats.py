"""
FTSeek 收盘统计报告生成器

每日 16:30 自动推送收盘统计，包括：
- 今日信号数 / 执行数 / 忽略数
- 今日盈亏 (已平仓)
- 当前持仓
- 本周累计
- 风控状态
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from app.storage.database import get_database
from app.risk.risk_manager import get_risk_manager
from app.notifier.dingtalk import DingTalkNotifier
from app import config

logger = logging.getLogger("ftseek.reporters.daily_stats")
logger.propagate = False  # 防止日志重复


@dataclass
class DailyStats:
    """收盘统计数据"""
    date: str
    total_signals: int = 0
    executed_signals: int = 0
    ignored_signals: int = 0
    today_pnl: float = 0.0
    today_pnl_percent: float = 0.0
    weekly_pnl: float = 0.0
    weekly_pnl_percent: float = 0.0
    positions: List[Dict] = None
    consecutive_losses: int = 0
    current_drawdown: float = 0.0
    max_drawdown_limit: float = 0.15
    risk_status: str = "normal"
    
    def __post_init__(self):
        if self.positions is None:
            self.positions = []


class DailyStatsReporter:
    """收盘统计报告生成器"""
    
    def __init__(self):
        self.db = get_database()
        self.risk_manager = get_risk_manager()
        self.notifier = DingTalkNotifier()
    
    def get_today_stats(self) -> Dict[str, Any]:
        """获取今日统计数据 - 直接从 positions 表读取真实数据"""
        import sqlite3
        today = datetime.now().strftime("%Y-%m-%d")

        today_pnl = 0.0
        executed_signals = 0
        ignored_signals = 0
        today_signals = 0

        # 主路径：直接查 positions 表（最可靠数据源）
        try:
            db_path = getattr(self.db, 'db_path', None) or getattr(self.db, '_db_path', None)
            if db_path is None:
                # 兼容不同 db 实现
                import os
                db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ftseek.db')
                db_path = os.path.abspath(db_path)

            conn = sqlite3.connect(db_path)
            c = conn.cursor()

            # 今日已平仓记录（realized_pnl 已写入，is_add=0 排除加仓流水）
            c.execute(
                """SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0)
                   FROM positions
                   WHERE date(exit_time) = ? AND status = 'CLOSED'
                   AND (is_add IS NULL OR is_add = 0)""",
                (today,)
            )
            row = c.fetchone()
            executed_signals = row[0] or 0
            today_pnl = float(row[1] or 0.0)

            # 今日信号总数：从 signal_tracker 获取
            try:
                from app.data_subscriber import _signal_tracker_instance
                if _signal_tracker_instance:
                    reject_records = _signal_tracker_instance.get_today_rejections(today) or []
                    ignored_signals = len(reject_records)
                    today_signals = executed_signals + ignored_signals
                else:
                    today_signals = executed_signals
            except Exception:
                today_signals = executed_signals

            conn.close()
        except Exception as e:
            logger.warning(f"从 positions 表获取今日统计失败：{e}")

        # 计算今日盈亏百分比 (相对于初始资金)
        initial_cash = self.risk_manager.config.initial_cash if self.risk_manager else 1000000.0
        today_pnl_percent = (today_pnl / initial_cash) * 100 if initial_cash > 0 else 0.0

        return {
            'total_signals': today_signals,
            'executed_signals': executed_signals,
            'ignored_signals': ignored_signals,
            'today_pnl': today_pnl,
            'today_pnl_percent': today_pnl_percent
        }
    
    def get_weekly_stats(self) -> Dict[str, float]:
        """获取本周统计数据 - 直接从 positions 表读取"""
        import sqlite3
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        week_start = monday.strftime("%Y-%m-%d")

        weekly_pnl = 0.0

        try:
            db_path = getattr(self.db, 'db_path', None) or getattr(self.db, '_db_path', None)
            if db_path is None:
                import os
                db_path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ftseek.db')
                )

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0)
                   FROM positions
                   WHERE date(exit_time) >= ? AND status = 'CLOSED'
                   AND (is_add IS NULL OR is_add = 0)""",
                (week_start,)
            )
            row = c.fetchone()
            weekly_pnl = float(row[0] or 0.0)
            conn.close()
        except Exception as e:
            logger.warning(f"从 positions 表获取本周统计失败：{e}")

        initial_cash = self.risk_manager.config.initial_cash if self.risk_manager else 1000000.0
        weekly_pnl_percent = (weekly_pnl / initial_cash) * 100 if initial_cash > 0 else 0.0

        return {
            'weekly_pnl': weekly_pnl,
            'weekly_pnl_percent': weekly_pnl_percent
        }
    
    def get_current_positions(self) -> List[Dict]:
        """获取当前持仓 - 直接从 positions 表读取 OPEN 仓位"""
        import sqlite3
        positions = []

        try:
            db_path = getattr(self.db, 'db_path', None) or getattr(self.db, '_db_path', None)
            if db_path is None:
                import os
                db_path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ftseek.db')
                )

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                """SELECT code, action, quantity, entry_price, stop_loss, take_profit
                   FROM positions WHERE status = 'OPEN'
                   AND (is_add IS NULL OR is_add = 0)
                   ORDER BY code"""
            )
            rows = c.fetchall()
            conn.close()

            for row in rows:
                code, action, qty, cost, sl, tp = row
                current_price = self._get_current_price(code)
                if current_price > 0 and cost > 0:
                    pnl = (current_price - cost) * qty
                    pnl_pct = ((current_price - cost) / cost) * 100
                else:
                    pnl, pnl_pct = 0.0, 0.0

                positions.append({
                    'code': code,
                    'name': '',
                    'quantity': qty,
                    'entry_price': cost,
                    'current_price': current_price,
                    'unrealized_pnl': pnl,
                    'unrealized_pnl_percent': pnl_pct,
                    'stop_loss': sl,
                    'take_profit': tp,
                })

            logger.info(f"从 positions 表获取 {len(positions)} 条 OPEN 仓位")
        except Exception as e:
            logger.error(f"获取持仓失败：{e}")

        return positions
    
    def _get_current_price(self, code: str) -> float:
        """获取当前价格 (简化实现，实际应从市场数据获取)"""
        try:
            # 尝试从风险管理器获取持仓价格
            if self.risk_manager:
                account = getattr(self.risk_manager, 'account', {})
                positions = account.get('positions', {})
                if code in positions:
                    pos_data = positions[code]
                    return pos_data.get('current_price', pos_data.get('entry_price', 0.0))
        except Exception:
            pass
        
        # 默认返回 0 (表示无法获取)
        return 0.0
    
    def get_risk_status(self) -> Dict[str, Any]:
        """获取风控状态"""
        if not self.risk_manager:
            return {
                'status': 'normal',
                'consecutive_losses': 0,
                'current_drawdown': 0.0,
                'max_drawdown_limit': 0.15
            }
        
        try:
            # 获取风控状态
            status_data = self.risk_manager.get_status()
            
            # 解析回撤数据
            drawdown_str = status_data.get('capital', {}).get('drawdown', '0%')
            current_drawdown = float(drawdown_str.replace('%', '').replace('-', '')) / 100.0
            
            consecutive_losses = status_data.get('system', {}).get('consecutive_losses', 0)
            max_drawdown = self.risk_manager.config.max_drawdown
            
            # 判断风控状态
            if current_drawdown >= max_drawdown:
                status = 'critical'
            elif consecutive_losses >= self.risk_manager.config.consecutive_loss_limit:
                status = 'warning'
            else:
                status = 'normal'
            
            return {
                'status': status,
                'consecutive_losses': consecutive_losses,
                'current_drawdown': current_drawdown,
                'max_drawdown_limit': max_drawdown
            }
            
        except Exception as e:
            logger.warning(f"获取风控状态失败：{e}")
            return {
                'status': 'normal',
                'consecutive_losses': 0,
                'current_drawdown': 0.0,
                'max_drawdown_limit': 0.15
            }
    
    def generate_report(self) -> DailyStats:
        """生成收盘统计报告"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 获取各项统计数据
        today_stats = self.get_today_stats()
        weekly_stats = self.get_weekly_stats()
        positions = self.get_current_positions()
        risk_status = self.get_risk_status()
        
        stats = DailyStats(
            date=today,
            total_signals=today_stats['total_signals'],
            executed_signals=today_stats['executed_signals'],
            ignored_signals=today_stats['ignored_signals'],
            today_pnl=today_stats['today_pnl'],
            today_pnl_percent=today_stats['today_pnl_percent'],
            weekly_pnl=weekly_stats['weekly_pnl'],
            weekly_pnl_percent=weekly_stats['weekly_pnl_percent'],
            positions=positions,
            consecutive_losses=risk_status['consecutive_losses'],
            current_drawdown=risk_status['current_drawdown'],
            max_drawdown_limit=risk_status['max_drawdown_limit'],
            risk_status=risk_status['status']
        )
        
        return stats
    
    def format_message(self, stats: DailyStats) -> str:
        """格式化推送消息"""
        # 今日盈亏符号和颜色
        today_pnl_symbol = "+" if stats.today_pnl >= 0 else ""
        today_pnl_emoji = "🟢" if stats.today_pnl >= 0 else "🔴"
        
        # 本周盈亏符号和颜色
        weekly_pnl_symbol = "+" if stats.weekly_pnl >= 0 else ""
        weekly_pnl_emoji = "🟢" if stats.weekly_pnl >= 0 else "🔴"
        
        # 风控状态
        risk_emoji = "✅" if stats.risk_status == "normal" else "⚠️" if stats.risk_status == "warning" else "🚨"
        risk_text = "正常" if stats.risk_status == "normal" else "预警" if stats.risk_status == "warning" else "危险"
        
        # 构建消息
        lines = [
            f"📊 FTSeek 收盘统计 ({stats.date})",
            "",
            f"今日信号：{stats.total_signals} 次",
            f"已执行：{stats.executed_signals} 次",
            f"忽略/延期：{stats.ignored_signals} 次",
            "",
            f"今日盈亏：{today_pnl_symbol}HK${stats.today_pnl:,.0f} ({today_pnl_symbol}{stats.today_pnl_percent:.2f}%) {today_pnl_emoji}",
            f"本周盈亏：{weekly_pnl_symbol}HK${stats.weekly_pnl:,.0f} ({weekly_pnl_symbol}{stats.weekly_pnl_percent:.2f}%) {weekly_pnl_emoji}",
            "",
            f"当前持仓：{len(stats.positions)} 只",
        ]
        
        # 添加持仓详情
        for pos in stats.positions:
            pnl_percent = pos.get('unrealized_pnl_percent', 0)
            pnl_symbol = "+" if pnl_percent >= 0 else ""
            pnl_emoji = "🟢" if pnl_percent >= 0 else "🔴"
            name = pos.get('name', '')
            lines.append(f"- {pos['code']} {name} {pnl_symbol}{pnl_percent:.1f}% {pnl_emoji}")
        
        lines.extend([
            "",
            f"风控状态：{risk_emoji} {risk_text}",
            f"连续亏损：{stats.consecutive_losses} 次",
            f"总回撤：{stats.current_drawdown:.1%} (安全线 {stats.max_drawdown_limit:.0%})",
        ])
        
        return "\n".join(lines)
    
    def send_report(self, stats: Optional[DailyStats] = None) -> bool:
        """发送收盘统计报告（含深度复盘）"""
        try:
            if stats is None:
                stats = self.generate_report()
            
            # 转换为 notifier 需要的格式
            stats_dict = {
                'date': stats.date,
                'today_signals': stats.total_signals,
                'today_executed': stats.executed_signals,
                'today_ignored': stats.ignored_signals,
                'today_pnl': stats.today_pnl,
                'today_pnl_pct': stats.today_pnl_percent,
                'week_pnl': stats.weekly_pnl,
                'week_pnl_pct': stats.weekly_pnl_percent,
                'positions': [
                    {
                        'code': pos['code'],
                        'name': pos.get('name', ''),
                        'pnl_pct': pos.get('unrealized_pnl_percent', 0)
                    }
                    for pos in stats.positions
                ],
                'risk_ok': stats.risk_status == 'normal',
                'consecutive_loss': stats.consecutive_losses,
                'drawdown': stats.current_drawdown * 100,  # 转换为百分比
                'max_drawdown': stats.max_drawdown_limit * 100
            }
            
            # 生成深度复盘数据
            try:
                review = self.generate_review_report()
            except Exception as e:
                logger.warning(f"生成复盘报告失败，降级发送基础统计：{e}")
                review = None
            
            # 使用 notifier 的 send_daily_stats 方法
            success = self.notifier.send_daily_stats(stats_dict, review=review)
            
            if success:
                logger.info(f"收盘统计推送成功：{stats.date}")
            else:
                logger.error(f"收盘统计推送失败：{stats.date}")
            
            return success
            
        except Exception as e:
            logger.error(f"发送收盘统计报告异常：{e}", exc_info=True)
            return False


    def get_signal_rejection_stats(self) -> dict:
        """
        获取今日信号拒绝统计
        从 signal_tracker 提取各类拒绝原因的数量
        """
        stats = {
            'rr_filter': 0,
            'time_filter': 0,
            'circuit_breaker': 0,
            'low_confidence': 0,
            'other': 0,
        }
        try:
            from app.data_subscriber import _signal_tracker_instance
            if _signal_tracker_instance:
                today = datetime.now().strftime("%Y-%m-%d")
                reject_records = _signal_tracker_instance.get_today_rejections(today)
                for rec in (reject_records or []):
                    reason = (rec.get('reason') or '').lower()
                    if 'r:r' in reason or 'rr' in reason:
                        stats['rr_filter'] += 1
                    elif '时段' in reason or 'time' in reason:
                        stats['time_filter'] += 1
                    elif '熔断' in reason or 'circuit' in reason:
                        stats['circuit_breaker'] += 1
                    elif '置信' in reason or 'confidence' in reason:
                        stats['low_confidence'] += 1
                    else:
                        stats['other'] += 1
        except Exception as e:
            logger.debug(f"获取信号拒绝统计失败：{e}")
        return stats

    def get_today_closed_trades(self) -> list:
        """
        获取今日已平仓交易详情
        """
        trades = []
        try:
            if self.risk_manager and hasattr(self.risk_manager, 'get_closed_trades_today'):
                raw = self.risk_manager.get_closed_trades_today() or []
                for t in raw:
                    entry_price = t.get('entry_price', 0) or 0
                    exit_price = t.get('exit_price', t.get('current_price', 0)) or 0
                    quantity = t.get('quantity', 0) or 0
                    pnl = t.get('pnl', 0) or 0
                    pnl_pct = 0.0
                    if entry_price > 0 and quantity > 0:
                        pnl_pct = (pnl / (entry_price * quantity)) * 100

                    entry_time = t.get('entry_time', '')
                    exit_time = t.get('exit_time', datetime.now().strftime('%H:%M:%S'))
                    hold_mins = 0
                    try:
                        from datetime import datetime as _dt
                        fmt = '%Y-%m-%d %H:%M:%S'
                        t_entry = _dt.strptime(str(entry_time)[:19], fmt)
                        t_exit = _dt.strptime(str(exit_time)[:19], fmt)
                        hold_mins = int((t_exit - t_entry).total_seconds() / 60)
                    except Exception:
                        hold_mins = 0

                    trades.append({
                        'code': t.get('code', ''),
                        'direction': t.get('direction', 'long'),
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'quantity': quantity,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'hold_mins': hold_mins,
                        'exit_reason': t.get('exit_reason', '信号触发'),
                    })
        except Exception as e:
            logger.debug(f"获取平仓交易详情失败：{e}")
        return trades

    def get_market_regime_summary(self) -> dict:
        """
        获取今日市场状态摘要
        """
        summary = {'regime': 'neutral', 'adx': 0.0, 'description': '中性市场'}
        try:
            from app.data_subscriber import _data_subscriber_instance
            if _data_subscriber_instance and hasattr(_data_subscriber_instance, '_consensus'):
                regime = getattr(_data_subscriber_instance._consensus, '_last_market_regime', 'neutral')
                adx = getattr(_data_subscriber_instance._consensus, '_last_adx', 0.0)
                summary['regime'] = regime
                summary['adx'] = adx or 0.0
                desc_map = {
                    'trending': '趋势市（信号质量高）',
                    'strong_trend_up': '强上涨趋势',
                    'weak_trend_up': '弱上涨趋势',
                    'ranging': '震荡市（信号噪音多）',
                    'range_bound': '窄幅震荡',
                    'weak_trend_down': '弱下跌趋势',
                    'strong_trend_down': '强下跌趋势',
                    'high_volatility': '高波动',
                    'neutral': '中性市场',
                }
                summary['description'] = desc_map.get(str(regime), str(regime))
        except Exception as e:
            logger.debug(f"获取市场状态摘要失败：{e}")
        return summary

    def get_strategy_stats_today(self) -> dict:
        """
        获取今日策略排行（来自 AdaptiveWeightManager）
        """
        try:
            from app.data_subscriber import _data_subscriber_instance
            if _data_subscriber_instance and hasattr(_data_subscriber_instance, '_adaptive_weight_manager'):
                mgr = _data_subscriber_instance._adaptive_weight_manager
                return mgr.get_strategy_stats() or {}
        except Exception as e:
            logger.debug(f"获取策略统计失败：{e}")
        return {}

    def get_at_risk_positions(self) -> list:
        """
        获取浮亏超过 2% 的持仓（下日预警）
        """
        at_risk = []
        try:
            positions = self.get_current_positions()
            for pos in positions:
                pnl_pct = pos.get('unrealized_pnl_percent', 0)
                if pnl_pct < -2.0:
                    at_risk.append(pos)
        except Exception as e:
            logger.debug(f"获取风险持仓失败：{e}")
        return at_risk

    def generate_review_report(self) -> dict:
        """
        生成深度复盘报告数据
        包含：信号归因、个股盈亏、市场状态、策略排行、预警持仓
        """
        return {
            'rejection_stats': self.get_signal_rejection_stats(),
            'closed_trades': self.get_today_closed_trades(),
            'market_regime': self.get_market_regime_summary(),
            'strategy_stats': self.get_strategy_stats_today(),
            'at_risk_positions': self.get_at_risk_positions(),
        }


def generate_daily_stats() -> str:
    """便捷函数：生成并返回格式化的收盘统计消息"""
    reporter = DailyStatsReporter()
    stats = reporter.generate_report()
    return reporter.format_message(stats)


def send_daily_stats() -> bool:
    """便捷函数：生成并发送收盘统计"""
    reporter = DailyStatsReporter()
    return reporter.send_report()


if __name__ == "__main__":
    # 测试运行
    logging.basicConfig(level=logging.INFO)
    reporter = DailyStatsReporter()
    stats = reporter.generate_report()
    print(reporter.format_message(stats))
