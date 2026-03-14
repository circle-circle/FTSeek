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
        """获取今日统计数据 - 从富途 OpenD API 获取实际交易数据"""
        today = datetime.now().strftime("%Y-%m-%d")
        today_start = f"{today} 00:00:00"
        today_end = f"{today} 23:59:59"
        
        # 2026-03-14 修复：从富途 OpenD API 获取实际交易数据，而非本地数据库
        today_pnl = 0.0
        today_signals = 0
        executed_signals = 0
        ignored_signals = 0
        
        try:
            # 从风险管理器获取实际账户数据
            if self.risk_manager and hasattr(self.risk_manager, 'account'):
                account = self.risk_manager.account
                
                # 获取今日盈亏 (从已平仓交易)
                closed_trades = self.risk_manager.get_closed_trades_today()
                if closed_trades:
                    today_pnl = sum(t.get('pnl', 0) for t in closed_trades)
                    executed_signals = len(closed_trades)
                
                # 获取今日信号数 (从 signal_store)
                try:
                    from app.signal_store import get_signal_store
                    store = get_signal_store()
                    today_signals = store.count_today()
                    ignored_signals = max(0, today_signals - executed_signals)
                except Exception as e:
                    logger.debug(f"获取信号统计失败：{e}")
                    today_signals = executed_signals
                    
        except Exception as e:
            logger.warning(f"从 OpenD 获取今日统计失败：{e}，使用备选方案")
            # 备选方案：尝试从数据库获取
            try:
                signals = self.db.get_statistics(today_start, today_end)
                signal_stats = signals.get('signals', {}) or {}
                today_signals = signal_stats.get('total_signals', 0) or 0
                executed_signals = signal_stats.get('executed_signals', 0) or 0
                ignored_signals = max(0, today_signals - executed_signals)
            except Exception:
                pass
        
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
        """获取本周统计数据"""
        today = datetime.now()
        # 本周一
        monday = today - timedelta(days=today.weekday())
        week_start = monday.strftime("%Y-%m-%d") + " 00:00:00"
        week_end = today.strftime("%Y-%m-%d") + " 23:59:59"
        
        # 交易统计
        try:
            stats = self.db.get_statistics(week_start, week_end)
            trade_stats = stats.get('trades', {}) or {}
        except Exception as e:
            logger.warning(f"获取本周统计失败：{e}")
            trade_stats = {}
        
        weekly_pnl = trade_stats.get('total_pnl', 0.0) or 0.0
        
        # 计算本周盈亏百分比
        initial_cash = self.risk_manager.config.initial_cash if self.risk_manager else 1000000.0
        weekly_pnl_percent = (weekly_pnl / initial_cash) * 100 if initial_cash > 0 else 0.0
        
        return {
            'weekly_pnl': weekly_pnl,
            'weekly_pnl_percent': weekly_pnl_percent
        }
    
    def get_current_positions(self) -> List[Dict]:
        """获取当前持仓 - 优先从富途 OpenD API 获取实际持仓"""
        positions = []
        
        # 2026-03-14 修复：首先尝试从风险管理器获取实际持仓数据
        try:
            if self.risk_manager and hasattr(self.risk_manager, 'account'):
                account = self.risk_manager.account
                account_positions = account.get('positions', {})
                
                if account_positions:
                    for code, pos_data in account_positions.items():
                        positions.append({
                            'code': code,
                            'name': pos_data.get('name', ''),
                            'quantity': pos_data.get('quantity', 0),
                            'entry_price': pos_data.get('entry_price', 0),
                            'current_price': pos_data.get('current_price', 0),
                            'unrealized_pnl': pos_data.get('unrealized_pnl', 0),
                            'unrealized_pnl_percent': pos_data.get('unrealized_pnl_percent', 0)
                        })
                    
                    logger.info(f"从风险管理器获取 {len(positions)} 条持仓")
                    return positions
                    
        except Exception as e:
            logger.warning(f"从风险管理器获取持仓失败：{e}，回退到数据库查询")
        
        # 备选方案：从未平仓交易中获取持仓
        try:
            open_trades = self.db.get_open_trades()
            
            # 按股票代码聚合
            position_map = {}
            for trade in open_trades:
                code = trade['code']
                if code not in position_map:
                    position_map[code] = {
                        'code': code,
                        'name': trade.get('name', ''),
                        'quantity': 0,
                        'entry_price': 0.0,
                        'total_cost': 0.0
                    }
                position_map[code]['quantity'] += trade.get('quantity', 0)
                position_map[code]['total_cost'] += trade.get('quantity', 0) * trade.get('entry_price', 0)
            
            # 计算平均成本和当前盈亏
            for code, pos in position_map.items():
                if pos['quantity'] > 0:
                    pos['entry_price'] = pos['total_cost'] / pos['quantity']
                    # 获取当前价格 (从风险管理器或市场数据)
                    current_price = self._get_current_price(code)
                    pos['current_price'] = current_price
                    
                    # 计算盈亏
                    if current_price > 0:
                        pnl = (current_price - pos['entry_price']) * pos['quantity']
                        pnl_percent = ((current_price - pos['entry_price']) / pos['entry_price']) * 100
                    else:
                        pnl = 0.0
                        pnl_percent = 0.0
                    
                    pos['unrealized_pnl'] = pnl
                    pos['unrealized_pnl_percent'] = pnl_percent
                    
                    positions.append(pos)
            
            logger.info(f"从数据库获取 {len(positions)} 条持仓")
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
        """发送收盘统计报告"""
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
            
            # 使用 notifier 的 send_daily_stats 方法
            success = self.notifier.send_daily_stats(stats_dict)
            
            if success:
                logger.info(f"收盘统计推送成功：{stats.date}")
            else:
                logger.error(f"收盘统计推送失败：{stats.date}")
            
            return success
            
        except Exception as e:
            logger.error(f"发送收盘统计报告异常：{e}", exc_info=True)
            return False


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
