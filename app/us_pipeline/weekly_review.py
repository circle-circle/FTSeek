
import logging
import sqlite3
import requests
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

logger = logging.getLogger("ftseek.weekly_review")

class WeeklyReviewer:
    """
    每周复盘生成器。
    汇总一周数据，分析策略胜率，评估参数调整。
    """
    
    DB_PATH = "data/ftseek.db"

    def run_review(self, market: str = "US"):
        logger.info(f"开始生成 {market} 每周复盘报告...")
        
        with sqlite3.connect(self.DB_PATH) as conn:
            cursor = conn.cursor()
        
            # 获取过去 7 天
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            today = datetime.now().strftime("%Y-%m-%d")
        
            # 1. 交易汇总
            cursor.execute("""
                SELECT 
                    COUNT(*), 
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
                    SUM(pnl)
                FROM trades 
                WHERE code LIKE ? AND entry_time >= ?
            """, (f"{market}.%", week_ago))
            row = cursor.fetchone()
            total, wins, losses, total_pnl = row
            win_rate = (wins / total * 100) if total > 0 else 0
        
            # 2. 策略表现
            cursor.execute("""
                SELECT strategy, COUNT(*), SUM(pnl) 
                FROM trades 
                WHERE code LIKE ? AND entry_time >= ?
                GROUP BY strategy
            """, (f"{market}.%", week_ago))
            strategy_stats = cursor.fetchall()
        
        
        report = (f"📅 **{market} 每周复盘报告 ({week_ago} ~ {today})**\n"
                  f"📊 **总体表现**\n"
                  f"- 总交易数: **{total}**\n"
                  f"- 胜率: **{win_rate:.1f}%** (Win: {wins}, Loss: {losses})\n"
                  f"- 总盈亏: **{total_pnl:+.2f}**\n\n"
                  f"🧠 **策略表现**\n")
        for strat, count, pnl in strategy_stats:
            report += f"- `{strat}`: {count} 笔 | 盈亏 {pnl:+.2f}\n"
        report += "\n---\n*由 FTSeek WeeklyReviewer 自动生成*"
        
        self._push_feishu(report)
        return report

    def _push_feishu(self, content: str):
        load_dotenv()
        webhook = os.environ.get('FEISHU_WEBHOOK_URL')
        if webhook:
            card = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": "📅 每周复盘报告"}, "template": "purple"},
                    "elements": [{"tag": "markdown", "content": content}]
                }
            }
            try:
                requests.post(webhook, json=card, timeout=10)
                logger.info("飞书推送成功")
            except Exception as e:
                logger.error(f"飞书推送失败: {e}")
