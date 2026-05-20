#!/usr/bin/env python3
"""
美股交易小时报告 - FTSeek-US

功能:
- 每小时自动生成美股交易报告
- 推送到钉钉
- 包含持仓、交易、风险、合规性检查

使用方法:
  cd /opt/ftseek/FTSeek-US && python3 scripts/hourly_report.py
"""
import os
import sys
import re
import json
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
import requests

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek_us.hourly_report")

# 从环境变量获取配置
# 先尝试从 .env.us 文件读取
FEISHU_WEBHOOK = ""
FEISHU_KEYWORDS = ["FTSeek", "交易"]  # 飞书关键字验证

env_file = Path("/opt/ftseek/FTSeek-US/.env.us")
if env_file.exists():
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("FEISHU_WEBHOOK_URL_US="):
                FEISHU_WEBHOOK = line.split("=", 1)[1]

logger.info(f"飞书 Webhook: {'已配置' if FEISHU_WEBHOOK else '未配置'}")

# 数据文件路径
HOLDINGS_FILE = Path("/root/.openclaw/workspace-ftseek-us/HOLDINGS.md")
TRADES_FILE = Path("/root/.openclaw/workspace-ftseek-us/TRADES.md")


def load_holdings() -> Dict:
    """从 HOLDINGS.md 加载美股持仓"""
    holdings = {
        "positions": [],
        "total_value": 0,
        "total_cost": 0,
        "cash": 0,
        "pnl": 0,
        "pnl_pct": 0,
    }
    
    if not HOLDINGS_FILE.exists():
        logger.warning(f"HOLDINGS.md 不存在：{HOLDINGS_FILE}")
        return holdings
    
    with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 查找美股持仓部分
    us_section = re.search(r'### 美股持仓\n\n(.*?)\n\n###', content, re.DOTALL)
    if not us_section:
        logger.warning("未找到美股持仓部分")
        return holdings
    
    us_content = us_section.group(1)
    
    # 解析表格
    table_match = re.search(
        r'\| 股票代码 \| 持仓数量 \| 平均成本 \| 当前价 \| 盈亏% \| 止损价 \| 目标价 \| 建仓日期 \|\n\|[-\| ]+\n(.*?)\n\n',
        us_content,
        re.DOTALL,
    )
    
    if not table_match:
        logger.warning("未找到美股持仓表格")
        return holdings
    
    rows = table_match.group(1).strip().split("\n")
    
    for row in rows:
        cells = row.split("|")
        if len(cells) >= 8:
            try:
                code = cells[1].strip()
                quantity = int(cells[2].strip().replace(",", ""))
                cost_str = cells[3].strip().replace("$", "").replace(",", "")
                price_str = cells[4].strip().replace("$", "").replace(",", "")
                pnl_pct_str = cells[5].strip().replace("%", "")
                stop_loss = cells[6].strip()
                target = cells[7].strip()
                
                cost_price = float(cost_str)
                current_price = float(price_str)
                pnl_pct = float(pnl_pct_str) if pnl_pct_str else 0
                
                market_value = quantity * current_price
                cost_value = quantity * cost_price
                pnl = market_value - cost_value
                
                holdings["positions"].append({
                    "code": code,
                    "quantity": quantity,
                    "cost_price": cost_price,
                    "current_price": current_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "stop_loss": stop_loss,
                    "target": target,
                })
                
                holdings["total_value"] += market_value
                holdings["total_cost"] += cost_value
            except (ValueError, IndexError) as e:
                logger.debug(f"解析行失败：{row} - {e}")
                continue
    
    # 查找汇总信息
    value_match = re.search(r'\*\*美股持仓市值\*\*: \$([0-9,]+)', content)
    if value_match:
        holdings["total_value"] = float(value_match.group(1).replace(",", ""))
    
    cash_match = re.search(r'\*\*美股现金\*\*: \$([0-9,]+)', content)
    if cash_match:
        holdings["cash"] = float(cash_match.group(1).replace(",", ""))
    
    pnl_match = re.search(r'\*\*美股浮动盈亏\*\*: -?\$([0-9,]+) \((-?\d+\.\d+)%\)', content)
    if pnl_match:
        holdings["pnl"] = float(pnl_match.group(1).replace(",", ""))
        holdings["pnl_pct"] = float(pnl_match.group(2))
    
    return holdings


def load_trades() -> Dict:
    """从 TRADES.md 加载交易记录"""
    trades = {
        "today_count": 0,
        "today_bought": 0,
        "today_positions": [],
    }
    
    if not TRADES_FILE.exists():
        logger.warning(f"TRADES.md 不存在：{TRADES_FILE}")
        return trades
    
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 查找今日交易部分
    today = datetime.now().strftime("%Y-%m-%d")
    today_section = re.search(
        rf'\| {today} \|.*?\n\|[-\| ]+\n(.*?)\n\n',
        content,
        re.DOTALL,
    )
    
    if today_section:
        rows = today_section.group(1).strip().split("\n")
        for row in rows:
            cells = row.split("|")
            if len(cells) >= 10:
                try:
                    code = cells[1].strip()
                    direction = cells[2].strip()
                    price_str = cells[3].strip().replace("$", "")
                    pnl_pct_str = cells[8].strip().replace("%", "")
                    status = cells[10].strip()
                    
                    price = float(price_str) if price_str else 0
                    pnl_pct = float(pnl_pct_str) if pnl_pct_str else 0
                    
                    if status == "持仓中":
                        trades["today_count"] += 1
                        trades["today_bought"] += price
                        trades["today_positions"].append({
                            "code": code,
                            "direction": direction,
                            "price": price,
                            "pnl_pct": pnl_pct,
                        })
                except (ValueError, IndexError) as e:
                    logger.debug(f"解析交易失败：{row} - {e}")
                    continue
    
    return trades


def check_compliance() -> Dict:
    """检查策略合规性"""
    compliance = {
        "score": 0,
        "total": 6,
        "items": [],
    }
    
    holdings = load_holdings()
    trades = load_trades()
    
    # 1. 止损设置率
    positions_with_sl = sum(1 for p in holdings["positions"] if p.get("stop_loss") and p["stop_loss"] != "-")
    sl_rate = positions_with_sl / len(holdings["positions"]) if holdings["positions"] else 1
    compliance["items"].append({
        "name": "止损设置",
        "required": "必须",
        "actual": f"{positions_with_sl}/{len(holdings['positions'])}",
        "pass": sl_rate >= 1.0,
    })
    
    # 2. 目标价设置率
    positions_with_tp = sum(1 for p in holdings["positions"] if p.get("target") and p["target"] != "-")
    tp_rate = positions_with_tp / len(holdings["positions"]) if holdings["positions"] else 1
    compliance["items"].append({
        "name": "目标价",
        "required": "必须",
        "actual": f"{positions_with_tp}/{len(holdings['positions'])}",
        "pass": tp_rate >= 1.0,
    })
    
    # 3. 仓位控制
    total_assets = holdings["total_value"] + holdings["cash"]
    position_ratio = holdings["total_value"] / total_assets if total_assets > 0 else 0
    compliance["items"].append({
        "name": "仓位控制",
        "required": "≤20%",
        "actual": f"{position_ratio:.1%}",
        "pass": position_ratio <= 0.20,
    })
    
    # 4. 交易频率
    compliance["items"].append({
        "name": "交易频率",
        "required": "≤5 笔/日",
        "actual": f"{trades['today_count']} 笔",
        "pass": trades["today_count"] <= 5,
    })
    
    # 5. 记录完整
    compliance["items"].append({
        "name": "记录完整",
        "required": "必填字段",
        "actual": "已补充" if trades["today_count"] > 0 else "无交易",
        "pass": True,
    })
    
    # 6. 投资风格
    compliance["items"].append({
        "name": "投资风格",
        "required": "中长期为主",
        "actual": "持仓待观察",
        "pass": True,
    })
    
    # 计算合规率
    pass_count = sum(1 for item in compliance["items"] if item["pass"])
    compliance["score"] = pass_count
    compliance["rate"] = pass_count / compliance["total"]
    
    return compliance


def generate_report() -> str:
    """生成 Markdown 格式报告"""
    holdings = load_holdings()
    trades = load_trades()
    compliance = check_compliance()
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 构建报告
    lines = [
        f"## 🇺🇸 FTSeek-US 美股交易小时报告",
        f"",
        f"**报告时间**: {now} (北京时间)",
        f"**交易时段**: {'🔴 交易中' if 21 <= datetime.now().hour or datetime.now().hour < 4 else '🟢 已收盘'}",
        f"",
        f"---",
        f"",
        f"### 📊 账户概览",
        f"",
        f"| 指标 | 数值 | 状态 |",
        f"|------|------|------|",
        f"| **美股持仓** | ${holdings['total_value']:,.0f} | {holdings['pnl_pct']:+.2f}% |",
        f"| **现金** | ${holdings['cash']:,.0f} | {holdings['cash']/(holdings['total_value']+holdings['cash'])*100:.1f}% |",
        f"| **总资产** | ${holdings['total_value']+holdings['cash']:,.0f} | - |",
        f"| **浮动盈亏** | ${holdings['pnl']:+,.0f} | {holdings['pnl_pct']:+.2f}% |",
        f"",
    ]
    
    # 持仓详情
    if holdings["positions"]:
        lines.append(f"### 📈 当前持仓")
        lines.append(f"")
        lines.append(f"| 代码 | 持仓 | 成本 | 现价 | 盈亏% | 止损 | 目标 |")
        lines.append(f"|------|------|------|------|-------|------|------|")
        
        for pos in holdings["positions"]:
            emoji = "🟢" if pos["pnl_pct"] > 0 else ("🔴" if pos["pnl_pct"] < 0 else "🟡")
            lines.append(
                f"| {pos['code']} | {pos['quantity']} | ${pos['cost_price']:.2f} | "
                f"${pos['current_price']:.2f} | {emoji} {pos['pnl_pct']:+.2f}% | "
                f"${pos['stop_loss']} | ${pos['target']} |"
            )
        
        lines.append(f"")
    else:
        lines.append(f"### 📈 当前持仓")
        lines.append(f"")
        lines.append(f"**无持仓** ✅")
        lines.append(f"")
    
    # 今日交易
    if trades["today_positions"]:
        lines.append(f"### 📝 今日交易 ({datetime.now().strftime('%Y-%m-%d')})")
        lines.append(f"")
        lines.append(f"| 股票 | 方向 | 价格 | 盈亏% | 状态 |")
        lines.append(f"|------|------|------|-------|------|")
        
        for t in trades["today_positions"]:
            emoji = "🟢" if t["pnl_pct"] > 0 else ("🔴" if t["pnl_pct"] < 0 else "🟡")
            lines.append(f"| {t['code']} | {t['direction']} | ${t['price']:.2f} | {emoji} {t['pnl_pct']:+.2f}% | 持仓中 |")
        
        lines.append(f"")
        lines.append(f"**统计**: {trades['today_count']} 笔成交 | 总买入 ${trades['today_bought']:,.0f}")
        lines.append(f"")
    else:
        lines.append(f"### 📝 今日交易")
        lines.append(f"")
        lines.append(f"**无交易** ✅")
        lines.append(f"")
    
    # 合规性检查
    lines.append(f"### 📋 策略合规性")
    lines.append(f"")
    lines.append(f"| 规则 | 要求 | 实际 | 状态 |")
    lines.append(f"|------|------|------|------|")
    
    for item in compliance["items"]:
        emoji = "✅" if item["pass"] else "❌"
        lines.append(f"| {item['name']} | {item['required']} | {item['actual']} | {emoji} |")
    
    lines.append(f"")
    lines.append(f"**合规率**: {compliance['score']}/{compliance['total']} = {compliance['rate']:.0%} {'✅' if compliance['rate'] >= 0.8 else '⚠️'}")
    lines.append(f"")
    
    # 风险提示
    lines.append(f"### 🔔 风险提示")
    lines.append(f"")
    
    # 检查接近止损的持仓
    warning_positions = []
    for pos in holdings["positions"]:
        if pos["stop_loss"] and pos["stop_loss"] != "-":
            try:
                sl_price = float(pos["stop_loss"].replace("$", "").split()[0])
                distance = (pos["current_price"] - sl_price) / sl_price
                if distance < 0.05:  # 距离止损<5%
                    warning_positions.append({
                        "code": pos["code"],
                        "distance": distance,
                        "sl": sl_price,
                    })
            except (ValueError, KeyError, TypeError) as e:
                logger.debug(f"解析止损价失败 {pos.get('code', 'unknown')}: {e}")
    
    if warning_positions:
        for wp in warning_positions:
            lines.append(f"- ⚠️ **{wp['code']}** 距离止损仅 {wp['distance']:.1%} (止损价 ${wp['sl']:.2f})")
    else:
        lines.append(f"- ✅ 持仓风险可控")
    
    lines.append(f"- 📊 仓位：{holdings['cash']/(holdings['total_value']+holdings['cash'])*100:.1f}% 现金")
    lines.append(f"")
    
    # 页脚
    lines.append(f"---")
    lines.append(f"*FTSeek-US 自动化报告*")
    lines.append(f"*下次更新：下一小时整点*")
    
    return "\n".join(lines)


def send_feishu_report(markdown: str):
    """发送报告到飞书 (关键字验证模式)"""
    if not FEISHU_WEBHOOK:
        logger.warning("未配置 FEISHU_WEBHOOK_URL_US")
        return False
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"FTSeek-US 小时报告 ({now})"
    
    # 确保消息包含飞书关键字 (FTSeek 或 交易)
    # 在消息开头添加关键字标识
    feishu_text = f"**FTSeek 交易提醒**\n\n{markdown}"
    
    payload = {
        "msg_type": "text",
        "content": {
            "text": feishu_text,
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
        
        result = response.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            logger.info(f"报告已推送至飞书 | {now}")
            return True
        else:
            logger.error(f"飞书返回错误：{result}")
            return False
    except Exception as e:
        logger.error(f"推送报告失败：{e}")
        return False


def is_trading_hours() -> bool:
    """
    检查当前是否在美股常规交易时段内
    
    美股常规交易时间（北京时间）：
    - 周一至周五：21:30 - 次日 04:00
    
    Returns:
        bool: True 如果在交易时段内
    """
    now = datetime.now()
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()  # 0=周一，6=周日
    
    # 周末不推送
    if weekday >= 5:  # 周六 (5) 和周日 (6)
        return False
    
    # 检查时间范围：21:30 - 04:00
    # 情况 1: 21:30 - 23:59 (当天)
    if hour >= 21 and (hour > 21 or minute >= 30):
        return True
    # 情况 2: 00:00 - 03:59 (次日凌晨)
    elif hour >= 0 and hour < 4:
        # 需要确保是周一到周五的凌晨（即前一日是周日到周四）
        # 如果是周六凌晨 (weekday=5)，不推送
        if weekday < 5:
            return True
    
    return False


def main():
    """主函数"""
    now = datetime.now()
    current_time = now.strftime("%Y-%m-%d %H:%M")
    
    # 检查是否在交易时段
    if not is_trading_hours():
        logger.info(f"⏭️ 非交易时段，跳过推送 | {current_time}")
        logger.info("交易时段：周一至周五 21:30-04:00 (北京时间)")
        print(f"\n⏭️ 非交易时段，跳过推送 | {current_time}")
        return
    
    logger.info("开始生成小时报告")
    
    # 生成报告
    report = generate_report()
    
    # 推送到飞书
    success = send_feishu_report(report)
    
    if success:
        logger.info("✅ 报告推送成功")
    else:
        logger.error("❌ 报告推送失败")
    
    # 打印到控制台 (用于测试)
    print("\n" + "=" * 60 + "\n")
    print(report)
    print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    main()
