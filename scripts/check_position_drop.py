#!/usr/bin/env python3
"""
持仓跌幅检查脚本 - FTSeek-US

独立脚本，用于 crontab 定时执行持仓跌幅检查
不依赖 risk_monitor.py 集成

使用方法:
  python3 scripts/check_position_drop.py
"""
import logging
import os
import sys
import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek.check_position_drop")

# 跌幅预警阈值
DROP_WARNING_THRESHOLD = -0.05   # -5% 预警
DROP_CRITICAL_THRESHOLD = -0.08  # -8% 紧急告警


def load_holdings() -> List[Dict]:
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


def send_dingtalk_alert(alerts: List[Dict]):
    """
    发送警报到钉钉
    
    Args:
        alerts: 警报列表
    """
    webhook = os.getenv("DINGTALK_WEBHOOK_URL", "")
    
    if not webhook:
        logger.warning("未配置 DINGTALK_WEBHOOK_URL")
        return
    
    if not alerts:
        logger.info("无警报需要推送")
        return
    
    # 构建 Markdown 消息
    title = "⚠️ 持仓跌幅警报"
    markdown_lines = [f"## {title}", ""]
    
    for alert in alerts:
        emoji = "🔴" if alert["level"] == "CRITICAL" else "🟡"
        markdown_lines.append(f"{emoji} **{alert['code']}**: 跌幅 {alert['drop']:.1%}")
        markdown_lines.append(f"   {alert['message']}")
        markdown_lines.append("")
    
    markdown_lines.append("**建议**: 请评估是否执行止损")
    markdown_lines.append("")
    markdown_lines.append("---")
    markdown_lines.append(f"*FTSeek-US 风险监控系统*")
    markdown_lines.append(f"*{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    
    markdown_text = "\n".join(markdown_lines)
    
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text,
        },
    }
    
    try:
        response = requests.post(
            webhook,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"警报已推送 | 数量={len(alerts)}")
    except Exception as e:
        logger.error(f"推送警报失败：{e}")


def main():
    """主函数"""
    logger.info("开始执行持仓跌幅检查")
    
    # 加载持仓
    positions = load_holdings()
    
    if not positions:
        logger.info("无持仓或加载失败")
        return
    
    # 检查警报
    alerts = check_drop_alerts(positions)
    
    # 推送警报
    send_dingtalk_alert(alerts)
    
    logger.info("持仓跌幅检查完成")


if __name__ == "__main__":
    main()
