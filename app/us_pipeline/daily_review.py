import logging
import sqlite3
import requests
import os
from datetime import datetime
from dotenv import load_dotenv

logger = logging.getLogger("ftseek.daily_review")

class DailyReviewer:
    """
    每日复盘生成器。
    在收盘后运行，统计当日盈亏、胜率、持仓情况，并推送报告。
    """
    
    DB_PATH = "data/ftseek.db"

    def run_review(self, market: str = "US"):
        logger.info(f"开始生成 {market} 每日复盘报告...")
        
        with sqlite3.connect(self.DB_PATH) as conn:
            cursor = conn.cursor()
        
            today = datetime.now().strftime("%Y-%m-%d")
        
            # 1. 获取今日交易
            cursor.execute("SELECT COUNT(*), SUM(pnl) FROM trades WHERE code LIKE ? AND entry_time >= ?", (f"{market}.%", today))
            total_trades, total_pnl = cursor.fetchone()
            total_pnl = total_pnl or 0
        
            # 2. 获取当前持仓
            cursor.execute("SELECT COUNT(*), SUM(unrealized_pnl) FROM positions WHERE code LIKE ? AND status = 'OPEN'", (f"{market}.%",))
            open_pos, total_upnl = cursor.fetchone()
            total_upnl = total_upnl or 0
        
            # 3. 获取最大亏损交易
            cursor.execute("SELECT code, pnl FROM trades WHERE code LIKE ? AND entry_time >= ? AND pnl < 0 ORDER BY pnl ASC LIMIT 1", (f"{market}.%", today))
            worst_trade = cursor.fetchone()
        
        
        report = f"📊 **{market} 每日复盘报告 ({today})**\n\n"
        report += f"💰 **盈亏统计**\n"
        report += f"- 今日已结盈亏: **{total_pnl:+.2f}**\n"
        report += f"- 当前未结盈亏: **{total_upnl:+.2f}**\n"
        report += f"- 持仓数量: {open_pos}\n\n"
        report += f"⚠️ **风险警示**\n"
        if worst_trade:
            report += f"- 单笔最大亏损: **{worst_trade[0]}** ({worst_trade[1]:.2f})\n"
        else:
            report += "- 暂无显著亏损。\n"
        report += "\n---\n*由 FTSeek DailyReviewer 自动生成*"
        
        self._push_feishu(report)
        return report

    def _push_feishu(self, content: str):
        load_dotenv()
        webhook = os.environ.get('FEISHU_WEBHOOK_URL')
        if webhook:
            card = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": "📊 每日复盘报告"}, "template": "blue"},
                    "elements": [{"tag": "markdown", "content": content}]
                }
            }
            try:
                requests.post(webhook, json=card, timeout=10)
                logger.info("飞书推送成功")
            except Exception as e:
                logger.error(f"飞书推送失败: {e}")
