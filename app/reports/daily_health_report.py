#!/usr/bin/env python3
"""
FTSeek-US 收盘后交易健康大盘

功能:
- 收盘后推送当日交易总结到飞书
- 包含盈亏统计、交易记录、健康度分析
- 集成到 crontab 定时任务

使用方式:
    python3 -m app.reports.daily_health_report

定时任务 (crontab):
    0 4 * * 1-5 cd /opt/ftseek/FTSeek-US && python3 -m app.reports.daily_health_report
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.futu_integration import get_futu_integration
from app.market_data_backup import get_market_backup
from app.reports.feishu_push_service import get_feishu_push_service

from app.core.paths import get_logs_dir, get_trade_records_json
_log_path = get_logs_dir() / 'daily_report.log'
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(str(_log_path), encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek_us.daily_report")


class DailyHealthReport:
    """每日健康大盘报告服务"""
    
    def __init__(self):
        self.futu = None
        self.market_backup = None
        self.feishu_service = None
        self.records_file = str(get_trade_records_json())
        # P0-E1 (2026-05-14): 走 paths.py
        from app.core.paths import get_logs_dir
        self.audit_file = str(get_logs_dir() / "futu_trade_audit.jsonl")
        self._initialize()
    
    def _initialize(self):
        try:
            self.futu = get_futu_integration(
                host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
                trd_env="SIMULATE"
            )
            self.market_backup = get_market_backup()
            self.feishu_service = get_feishu_push_service()
            logger.info("组件初始化成功")
        except Exception as e:
            logger.error(f"组件初始化失败：{e}")
    
    def _load_daily_trades(self) -> List[Dict]:
        today = datetime.now().date()
        trades = []
        
        if os.path.exists(self.records_file):
            try:
                with open(self.records_file, 'r', encoding='utf-8') as f:
                    all_records = json.load(f)
                
                for record in all_records:
                    entry_time_str = record.get('entry_time', '')
                    if entry_time_str:
                        try:
                            entry_date = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00')).date()
                            if entry_date == today:
                                trades.append(record)
                                continue
                        except (ValueError, TypeError) as e:
                            logger.debug(f"解析 entry_time 失败：{entry_time_str}, 错误：{e}")
                            pass
                    
                    exit_time_str = record.get('exit_time', '')
                    if exit_time_str:
                        try:
                            exit_date = datetime.fromisoformat(exit_time_str.replace('Z', '+00:00')).date()
                            if exit_date == today:
                                trades.append(record)
                        except (ValueError, TypeError) as e:
                            logger.debug(f"解析 exit_time 失败：{exit_time_str}, 错误：{e}")
                            pass
            except Exception as e:
                logger.error(f"加载交易记录失败：{e}")
        
        if os.path.exists(self.audit_file):
            try:
                with open(self.audit_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            record = json.loads(line)
                            timestamp = record.get('timestamp', '')
                            if timestamp:
                                try:
                                    record_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).date()
                                    if record_date == today:
                                        trade = {
                                            'code': record.get('code', 'N/A'),
                                            'action': record.get('action', 'N/A'),
                                            'quantity': record.get('quantity', 0),
                                            'price': record.get('price', 0),
                                            'exit_reason': record.get('reason', ''),
                                            'from_audit': True
                                        }
                                        trades.append(trade)
                                except (ValueError, TypeError) as e:
                                    logger.debug(f"解析审计日志时间戳失败：{timestamp}, 错误：{e}")
                                    pass
            except Exception as e:
                logger.error(f"加载审计日志失败：{e}")
        
        return trades
    
    def _get_current_positions(self) -> List[Dict]:
        if not os.path.exists(self.records_file):
            return []
        
        try:
            with open(self.records_file, 'r', encoding='utf-8') as f:
                all_records = json.load(f)
            
            positions = []
            for record in all_records:
                if (not record.get('exit_price') or record.get('exit_price') == 0) and record.get('quantity', 0) > 0:
                    positions.append(record)
            
            return positions
        except Exception as e:
            logger.error(f"获取持仓失败：{e}")
            return []
    
    def _update_prices(self, positions: List[Dict]) -> List[Dict]:
        for pos in positions:
            code = pos.get('code')
            if not code:
                continue
            
            price = self.market_backup.get_price(code) if self.market_backup else None
            
            if price:
                pos['current_price'] = price
                entry_price = pos.get('entry_price', 0)
                
                if entry_price > 0:
                    pnl_pct = (price - entry_price) / entry_price
                    pnl_amount = (price - entry_price) * pos.get('quantity', 0)
                    pos['pnl_pct'] = pnl_pct * 100
                    pos['pnl_amount'] = pnl_amount
        
        return positions
    
    def _calculate_statistics(self, trades: List[Dict], positions: List[Dict]) -> Dict:
        stats = {
            'total_trades': len(trades),
            'buy_count': 0,
            'sell_count': 0,
            'trigger_count': 0,
            'total_pnl': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0,
            'avg_pnl_pct': 0,
            'position_count': len(positions),
            'total_position_value': 0,
            'total_position_pnl': 0,
        }
        
        for trade in trades:
            action = trade.get('action', '')
            if action == 'BUY':
                stats['buy_count'] += 1
            elif action == 'SELL':
                stats['sell_count'] += 1
            
            if trade.get('exit_reason'):
                stats['trigger_count'] += 1
        
        for pos in positions:
            pnl = pos.get('pnl_amount', 0)
            stats['total_position_pnl'] += pnl
            stats['total_position_value'] += pos.get('current_price', 0) * pos.get('quantity', 0)
            
            if pnl > 0:
                stats['winning_trades'] += 1
            elif pnl < 0:
                stats['losing_trades'] += 1
        
        total_positions = stats['winning_trades'] + stats['losing_trades']
        if total_positions > 0:
            stats['win_rate'] = (stats['winning_trades'] / total_positions) * 100
        
        if stats['position_count'] > 0:
            stats['avg_pnl_pct'] = stats['total_position_pnl'] / stats['total_position_value'] * 100
        
        return stats
    
    def _calculate_health_score(self, stats: Dict) -> int:
        score = 100
        score += min(30, max(-30, stats['total_position_pnl']))
        
        if stats['win_rate'] > 60:
            score += 20
        elif stats['win_rate'] > 50:
            score += 10
        elif stats['win_rate'] < 40:
            score -= 20
        elif stats['win_rate'] < 30:
            score -= 30
        
        if stats['trigger_count'] > 5:
            score -= 20
        elif stats['trigger_count'] > 3:
            score -= 10
        
        if stats['total_trades'] > 20:
            score -= 10
        elif stats['total_trades'] > 10:
            score -= 5
        
        return max(0, min(100, score))
    
    def _build_daily_report(self, trades: List[Dict], positions: List[Dict], 
                            stats: Dict, health_score: int) -> str:
        now = datetime.now()
        
        if health_score >= 80:
            health_emoji, health_text = "🟢", "优秀"
        elif health_score >= 60:
            health_emoji, health_text = "🟡", "良好"
        elif health_score >= 40:
            health_emoji, health_text = "🟠", "一般"
        else:
            health_emoji, health_text = "🔴", "需关注"
        
        lines = [
            f"**FTSeek-US 交易健康大盘**",
            f"日期：{now.strftime('%Y-%m-%d')} (美东交易日)",
            f"生成时间：{now.strftime('%H:%M')}",
            "",
            f"**📊 健康度**: {health_emoji} {health_text} ({health_score}分)",
            "",
            f"**📈 交易统计**",
            f"总交易数：{stats['total_trades']} 笔",
            f"买入：{stats['buy_count']} | 卖出：{stats['sell_count']}",
            f"止损止盈触发：{stats['trigger_count']} 笔",
            "",
            f"**💰 持仓概览**",
            f"持仓数量：{stats['position_count']} 只",
            f"总市值：${stats['total_position_value']:,.2f}",
            f"总浮盈：{'🟢' if stats['total_position_pnl'] >= 0 else '🔴'} ${stats['total_position_pnl']:+,.2f}",
            f"胜率：{stats['win_rate']:.1f}%",
            f"平均盈亏：{stats['avg_pnl_pct']:+.2f}%",
        ]
        
        if positions:
            lines.append("")
            lines.append("**📋 持仓明细**:")
            
            sorted_positions = sorted(positions, key=lambda x: x.get('pnl_amount', 0), reverse=True)
            
            for pos in sorted_positions[:10]:
                code = pos.get('code', 'N/A')
                qty = pos.get('quantity', 0)
                entry = pos.get('entry_price', 0)
                current = pos.get('current_price', entry)
                pnl_pct = pos.get('pnl_pct', 0)
                pnl_amt = pos.get('pnl_amount', 0)
                
                pnl_sign = "+" if pnl_amt >= 0 else ""
                pnl_emoji = "🟢" if pnl_amt >= 0 else "🔴"
                
                lines.append(
                    f"{pnl_emoji} {code}: {qty}股 "
                    f"${entry:.2f}→${current:.2f} "
                    f"({pnl_sign}{pnl_pct:.2f}%, {pnl_sign}${pnl_amt:.2f})"
                )
        
        if trades:
            lines.append("")
            lines.append("**📝 当日交易**:")
            
            for trade in trades[:10]:
                code = trade.get('code', 'N/A')
                action = trade.get('action', 'N/A')
                qty = trade.get('quantity', 0)
                price = trade.get('price', trade.get('entry_price', 0))
                reason = trade.get('exit_reason', '')
                
                action_emoji = "🟢" if action == "BUY" else "🔴"
                reason_emoji = "⚠️" if reason else ""
                
                line = f"{action_emoji} {action} {code} {qty}股 @ ${price:.2f}"
                if reason:
                    line += f" ({reason_emoji}{reason})"
                
                lines.append(line)
        
        lines.append("")
        lines.append("**⚠️ 风险提示**:")
        
        risks = []
        if stats['trigger_count'] > 5:
            risks.append("- 止损止盈触发频繁，建议检查策略参数")
        if stats['total_trades'] > 20:
            risks.append("- 交易频率过高，注意过度交易风险")
        if stats['win_rate'] < 40 and stats['position_count'] > 3:
            risks.append("- 胜率偏低，建议复盘策略")
        if stats['total_position_pnl'] < -1000:
            risks.append("- 浮亏较大，注意风险控制")
        
        if not risks:
            risks.append("- 各项指标正常，继续保持")
        
        lines.extend(risks)
        
        return "\n".join(lines)
    
    def _send_to_feishu(self, content: str) -> bool:
        if not self.feishu_service:
            logger.warning("飞书推送服务未初始化")
            return False
        
        try:
            success = self.feishu_service.send_post_message(
                title="FTSeek-US 交易健康大盘",
                content_lines=[content]
            )
            
            if success:
                logger.info("✅ 飞书推送成功")
            else:
                logger.warning("⚠️ 飞书推送失败")
            
            return success
        except Exception as e:
            logger.error(f"飞书推送异常：{e}")
            return False
    
    def run(self):
        logger.info("开始执行每日健康大盘报告...")
        
        try:
            trades = self._load_daily_trades()
            logger.info(f"获取到 {len(trades)} 笔当日交易")
            
            positions = self._get_current_positions()
            logger.info(f"获取到 {len(positions)} 个持仓")
            
            if positions:
                positions = self._update_prices(positions)
                logger.info("价格更新完成")
            
            stats = self._calculate_statistics(trades, positions)
            logger.info(f"统计完成：总交易{stats['total_trades']}笔，持仓{stats['position_count']}只")
            
            health_score = self._calculate_health_score(stats)
            logger.info(f"健康度评分：{health_score}")
            
            report = self._build_daily_report(trades, positions, stats, health_score)
            success = self._send_to_feishu(report)
            
            if success:
                logger.info("✅ 每日健康大盘推送完成")
            else:
                logger.warning("⚠️ 推送失败")
        
        except Exception as e:
            logger.error(f"执行报告失败：{e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.futu:
                self.futu.disconnect()


def main():
    report = DailyHealthReport()
    report.run()


if __name__ == "__main__":
    main()
