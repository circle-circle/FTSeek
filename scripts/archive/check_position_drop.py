#!/usr/bin/env python3
"""
持仓跌幅检查脚本 - FTSeek-US

独立脚本，用于 crontab 定时执行持仓跌幅检查
从数据库或配置文件读取持仓，检查跌幅并推送警报

使用方法:
  cd /opt/ftseek/FTSeek-US && python3 scripts/check_position_drop.py
"""
import logging
import os
import sys
import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import requests

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek_us.check_position_drop")

# 从环境变量获取配置
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK_URL_US", "")

# 跌幅预警阈值
DROP_WARNING_THRESHOLD = -0.05   # -5% 预警
DROP_CRITICAL_THRESHOLD = -0.08  # -8% 紧急告警


def load_holdings_from_file() -> List[Dict]:
    """
    从 HOLDINGS.md 加载持仓数据
    
    Returns:
        持仓列表
    """
    holdings_file = Path("/root/.openclaw/workspace-ftseek-us/HOLDINGS.md")
    positions = []
    
    if not holdings_file.exists():
        logger.warning(f"HOLDINGS.md 不存在：{holdings_file}")
        return positions
    
    with open(holdings_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 解析 Markdown 表格
    # 表格格式：| 股票代码 | 持仓数量 | 平均成本 | 当前价 | 盈亏% | 止损价 | 目标价 | 建仓日期 |
    table_match = re.search(
        r'\| 股票代码 \| 持仓数量 \| 平均成本 \| 当前价 \|.*?\n\|[-\| ]+\n(.*?)\n\n',
        content,
        re.DOTALL,
    )
    
    if not table_match:
        logger.warning("未在 HOLDINGS.md 中找到持仓表格")
        return positions
    
    rows = table_match.group(1).strip().split("\n")
    
    for row in rows:
        cells = row.split("|")
        if len(cells) >= 5:
            try:
                code = cells[1].strip()
                cost_price_str = cells[3].strip().replace("$", "").replace(",", "")
                current_price_str = cells[4].strip().replace("$", "").replace(",", "")
                
                cost_price = float(cost_price_str)
                current_price = float(current_price_str)
                
                # 计算跌幅
                drop_percent = (current_price - cost_price) / cost_price
                
                positions.append({
                    "code": code,
                    "cost_price": cost_price,
                    "current_price": current_price,
                    "drop_percent": drop_percent,
                })
                
                logger.info(f"加载持仓：{code} 成本=${cost_price:.2f} 现价=${current_price:.2f} 跌幅={drop_percent:.1%}")
            except (ValueError, IndexError) as e:
                logger.debug(f"解析行失败：{row} - {e}")
                continue
    
    return positions


def load_holdings_from_db() -> List[Dict]:
    """
    从 SQLite 数据库加载持仓数据
    
    Returns:
        持仓列表
    """
    db_path = Path("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
    positions = []
    
    if not db_path.exists():
        logger.warning(f"数据库不存在：{db_path}")
        return positions
    
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 查询持仓表 (假设表名为 positions)
        cursor.execute("""
            SELECT symbol, quantity, avg_entry_price, current_price
            FROM positions
            WHERE quantity > 0
        """)
        
        for row in cursor.fetchall():
            code = row["symbol"]
            cost_price = row["avg_entry_price"]
            current_price = row["current_price"] or cost_price
            
            drop_percent = (current_price - cost_price) / cost_price if cost_price > 0 else 0
            
            positions.append({
                "code": code,
                "cost_price": cost_price,
                "current_price": current_price,
                "drop_percent": drop_percent,
            })
            
            logger.info(f"加载持仓 (DB): {code} 成本=${cost_price:.2f} 现价=${current_price:.2f} 跌幅={drop_percent:.1%}")
        
        conn.close()
    except Exception as e:
        logger.error(f"从数据库加载持仓失败：{e}")
    
    return positions


def check_drop_alerts(positions: List[Dict]) -> List[Dict]:
    """
    检查跌幅警报
    
    Args:
        positions: 持仓列表
    
    Returns:
        警报列表
    """
    alerts = []
    
    for pos in positions:
        drop = pos["drop_percent"]
        
        if drop <= DROP_CRITICAL_THRESHOLD:
            alerts.append({
                "code": pos["code"],
                "level": "CRITICAL",
                "drop": drop,
                "message": f"🔴 {pos['code']} 跌幅 {drop:.1%} (阈值 {DROP_CRITICAL_THRESHOLD:.0%})",
            })
            logger.warning(f"紧急警报：{pos['code']} 跌幅={drop:.1%}")
        elif drop <= DROP_WARNING_THRESHOLD:
            alerts.append({
                "code": pos["code"],
                "level": "WARNING",
                "drop": drop,
                "message": f"🟡 {pos['code']} 跌幅 {drop:.1%} (阈值 {DROP_WARNING_THRESHOLD:.0%})",
            })
            logger.warning(f"预警：{pos['code']} 跌幅={drop:.1%}")
    
    return alerts


def send_feishu_alert(alerts: List[Dict]):
    """
    发送警报到飞书 (关键字验证模式)
    
    Args:
        alerts: 警报列表
    """
    if not FEISHU_WEBHOOK:
        logger.warning("未配置 FEISHU_WEBHOOK_URL_US")
        return
    
    if not alerts:
        logger.info("无警报需要推送")
        return
    
    # 构建文本消息 (必须包含 FTSeek 或 交易 关键字)
    text_lines = [
        "🔴 FTSeek 交易风险警报",
        "",
        f"**持仓跌幅预警** ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
        "",
    ]
    
    for alert in alerts:
        emoji = "🔴" if alert["level"] == "CRITICAL" else "🟡"
        text_lines.append(f"{emoji} *{alert['code']}*: 跌幅 {alert['drop']:.1%}")
        text_lines.append(f"   {alert['message']}")
        text_lines.append("")
    
    text_lines.append("**建议**: 请评估是否执行止损")
    text_lines.append("")
    text_lines.append("---")
    text_lines.append("*FTSeek-US 风险监控系统*")
    
    text_message = "\n".join(text_lines)
    
    payload = {
        "msg_type": "text",
        "content": {
            "text": text_message,
        },
    }
    
    try:
        response = requests.post(
            FEISHU_WEBHOOK,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"警报已推送至飞书 | 数量={len(alerts)}")
    except Exception as e:
        logger.error(f"推送警报失败：{e}")


def main():
    """主函数"""
    logger.info("开始执行持仓跌幅检查")
    
    # 优先从数据库加载，失败则从文件加载
    positions = load_holdings_from_db()
    if not positions:
        positions = load_holdings_from_file()
    
    if not positions:
        logger.info("无持仓或加载失败")
        return
    
    # 检查警报
    alerts = check_drop_alerts(positions)
    
    # 推送警报
    send_feishu_alert(alerts)
    
    logger.info("持仓跌幅检查完成")


if __name__ == "__main__":
    main()
