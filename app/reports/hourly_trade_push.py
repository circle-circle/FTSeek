#!/usr/bin/env python3
"""
FTSeek-US 每小时交易推送

功能:
- 每小时推送交易记录到飞书
- 包含持仓盈亏、当日交易、触发记录
- 集成到 crontab 定时任务

使用方式:
    python3 -m app.reports.hourly_trade_push

定时任务 (crontab):
    0 * * * * cd /opt/ftseek/FTSeek-US && python3 -m app.reports.hourly_trade_push
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.futu_integration import get_futu_integration
from app.market_data_backup import get_market_backup
from app.reports.feishu_push_service import get_feishu_push_service

# 配置日志
from app.core.paths import get_logs_dir, get_trade_records_json
_log_path = get_logs_dir() / 'hourly_push.log'
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(str(_log_path), encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek_us.hourly_push")


class HourlyTradePush:
    """每小时交易推送服务"""
    
    def __init__(self):
        """初始化推送服务"""
        self.futu = None
        self.market_backup = None
        self.feishu_service = None
        self.records_file = str(get_trade_records_json())
        
        # 初始化组件
        self._initialize()
    
    def _initialize(self):
        """初始化组件"""
        try:
            self.futu = get_futu_integration(
                host=os.getenv("FUTU_HOST", "127.0.0.1"),
                port=int(os.getenv("FUTU_PORT", "11111")),
                trd_env="SIMULATE"
            )
            self.market_backup = get_market_backup()
            self.feishu_service = get_feishu_push_service()
            logger.info("组件初始化成功")
        except Exception as e:
            logger.error(f"组件初始化失败：{e}")
    
    def _load_trade_records(self, hours: int = 24) -> List[Dict]:
        """加载最近 N 小时的交易记录"""
        if not os.path.exists(self.records_file):
            return []
        
        try:
            with open(self.records_file, 'r', encoding='utf-8') as f:
                all_records = json.load(f)
            
            cutoff_time = datetime.now() - timedelta(hours=hours)
            recent_records = []
            
            for record in all_records:
                entry_time_str = record.get('entry_time', '')
                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                        if entry_time >= cutoff_time:
                            recent_records.append(record)
                            continue
                    except (ValueError, TypeError) as e:
                        logger.debug(f"解析 entry_time 失败：{entry_time_str}, 错误：{e}")
                        pass
                
                exit_time_str = record.get('exit_time', '')
                if exit_time_str:
                    try:
                        exit_time = datetime.fromisoformat(exit_time_str.replace('Z', '+00:00'))
                        if exit_time >= cutoff_time:
                            recent_records.append(record)
                    except (ValueError, TypeError) as e:
                        logger.debug(f"解析 exit_time 失败：{exit_time_str}, 错误：{e}")
                        pass
            
            return recent_records
        except Exception as e:
            logger.error(f"加载交易记录失败：{e}")
            return []
    
    def _get_current_positions(self) -> List[Dict]:
        """获取当前持仓"""
        if not os.path.exists(self.records_file):
            return []
        
        try:
            with open(self.records_file, 'r', encoding='utf-8') as f:
                all_records = json.load(f)
            
            positions = []
            for record in all_records:
                if record.get('exit_price') is None and record.get('quantity', 0) > 0:
                    positions.append(record)
            
            return positions
        except Exception as e:
            logger.error(f"获取持仓失败：{e}")
            return []
    
    def _update_prices(self, positions: List[Dict]) -> List[Dict]:
        """更新持仓的当前价格"""
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
    
    def _build_hourly_report(self, positions: List[Dict], recent_trades: List[Dict]) -> str:
        """构建每小时推送报告"""
        now = datetime.now()
        
        lines = [
            f"**FTSeek-US 交易快报**",
            f"时间：{now.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"**📊 持仓概览**",
            f"持仓数量：{len(positions)}",
        ]
        
        if positions:
            total_pnl = 0
            total_value = 0
            
            lines.append("")
            lines.append("**持仓明细**:")
            
            for pos in positions:
                code = pos.get('code', 'N/A')
                qty = pos.get('quantity', 0)
                entry = pos.get('entry_price', 0)
                current = pos.get('current_price', entry)
                pnl_pct = pos.get('pnl_pct', 0)
                pnl_amt = pos.get('pnl_amount', 0)
                
                pnl_sign = "+" if pnl_amt >= 0 else ""
                pnl_color = "🟢" if pnl_amt >= 0 else "🔴"
                
                lines.append(
                    f"{pnl_color} {code}: {qty}股 @ ${current:.2f} "
                    f"({pnl_sign}{pnl_pct:.2f}%, {pnl_sign}${pnl_amt:.2f})"
                )
                
                total_pnl += pnl_amt
                total_value += current * qty
            
            lines.append("")
            lines.append(
                f"**总盈亏**: {'🟢' if total_pnl >= 0 else '🔴'} "
                f"${total_pnl:+.2f} (总市值：${total_value:,.2f})"
            )
        else:
            lines.append("暂无持仓")
        
        if recent_trades:
            lines.append("")
            lines.append("**📝 最近交易**:")
            
            for trade in recent_trades[:5]:
                code = trade.get('code', 'N/A')
                action = trade.get('action', 'N/A')
                qty = trade.get('quantity', 0)
                price = trade.get('entry_price', 0)
                time_str = trade.get('entry_time', '')[:16] if trade.get('entry_time') else 'N/A'
                
                action_emoji = "🟢" if action == "BUY" else "🔴"
                lines.append(
                    f"{action_emoji} {action} {code} {qty}股 @ ${price:.2f} ({time_str})"
                )
        
        trigger_count = len([t for t in recent_trades if t.get('exit_reason')])
        if trigger_count > 0:
            lines.append("")
            lines.append(f"**⚠️ 止损止盈触发**: {trigger_count} 笔")
        
        return "\n".join(lines)
    
    def _send_to_feishu(self, content: str) -> bool:
        """发送到飞书"""
        if not self.feishu_service:
            logger.warning("飞书推送服务未初始化")
            return False
        
        try:
            success = self.feishu_service.send_post_message(
                title="FTSeek-US 交易快报",
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
        """执行推送"""
        logger.info("开始执行每小时交易推送...")
        
        try:
            positions = self._get_current_positions()
            logger.info(f"获取到 {len(positions)} 个持仓")
            
            if positions:
                positions = self._update_prices(positions)
                logger.info("价格更新完成")
            
            recent_trades = self._load_trade_records(hours=1)
            logger.info(f"获取到 {len(recent_trades)} 笔最近交易")
            
            report = self._build_hourly_report(positions, recent_trades)
            success = self._send_to_feishu(report)
            
            if success:
                logger.info("✅ 每小时推送完成")
            else:
                logger.warning("⚠️ 推送失败")
        
        except Exception as e:
            logger.error(f"执行推送失败：{e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.futu:
                self.futu.disconnect()


def main():
    """主函数"""
    push = HourlyTradePush()
    push.run()


if __name__ == "__main__":
    main()
