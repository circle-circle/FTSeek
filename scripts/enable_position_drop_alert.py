#!/usr/bin/env python3
"""
启用持仓跌幅告警集成脚本 - FTSeek-US

功能:
- 修改 risk_monitor.py 集成持仓跌幅监控
- 添加 position_drop_alert 导入和调用
- 备份原文件

使用方法:
  python3 scripts/enable_position_drop_alert.py
"""
import logging
import shutil
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek.enable_position_drop_alert")

# 文件路径
RISK_MONITOR_FILE = Path("/opt/ftseek/FTSeek/app/risk/risk_monitor.py")
BACKUP_DIR = Path("/opt/ftseek/FTSeek/backups")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def backup_file():
    """备份原文件"""
    if not RISK_MONITOR_FILE.exists():
        logger.error(f"文件不存在：{RISK_MONITOR_FILE}")
        return False
    
    backup_name = f"risk_monitor.py.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_path = BACKUP_DIR / backup_name
    
    shutil.copy2(RISK_MONITOR_FILE, backup_path)
    logger.info(f"已备份原文件：{backup_path}")
    return True


def integrate_position_drop_alert():
    """
    集成持仓跌幅告警到 risk_monitor.py
    
    需要添加的代码:
    1. 导入 position_drop_alert 模块
    2. 在 RiskMonitor 类中添加 position_drop_monitor 属性
    3. 在 _check_all_risks() 方法中调用持仓跌幅检查
    """
    
    # 读取原文件
    with open(RISK_MONITOR_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 检查是否已集成
    if "position_drop_alert" in content:
        logger.info("持仓跌幅告警已集成，跳过")
        return True
    
    # 1. 添加导入语句 (在文件开头的 import 区域)
    import_statement = "\nfrom app.risk.position_drop_alert import PositionDropMonitor\n"
    
    # 找到第一个 import 块的位置
    lines = content.split("\n")
    insert_line = 0
    for i, line in enumerate(lines):
        if line.startswith("import ") or line.startswith("from "):
            insert_line = i
            break
    
    if insert_line > 0:
        lines.insert(insert_line, import_statement.strip())
        logger.info("已添加导入语句")
    else:
        logger.warning("未找到合适的导入位置")
        return False
    
    # 2. 在 RiskMonitor.__init__ 中添加 position_drop_monitor 初始化
    # 查找 __init__ 方法中的 dingtalk_notifier 初始化
    init_addition = """
        # 持仓跌幅监控器
        self.position_drop_monitor = PositionDropMonitor(
            futu_api=None,  # TODO: 传入 futu_api 实例
            dingtalk_webhook=getattr(dingtalk_notifier, 'webhook_url', None),
            check_interval=60,
        )
"""
    
    # 简单替换：在 dingtalk_notifier 赋值后添加
    content = "\n".join(lines)
    if "self.dingtalk_notifier = dingtalk_notifier" in content:
        content = content.replace(
            "self.dingtalk_notifier = dingtalk_notifier",
            "self.dingtalk_notifier = dingtalk_notifier\n" + init_addition,
        )
        logger.info("已添加 position_drop_monitor 初始化")
    else:
        logger.warning("未找到 dingtalk_notifier 初始化语句")
        return False
    
    # 3. 在 _check_all_risks 方法中添加持仓跌幅检查
    # 查找 _check_all_risks 方法
    check_addition = """
        # 持仓跌幅检查
        try:
            positions = self._get_positions_for_drop_check()
            if positions:
                alerts = self.position_drop_monitor.check_positions(positions)
                for alert in alerts:
                    self.position_drop_monitor.send_alert(alert)
        except Exception as e:
            logger.error(f"持仓跌幅检查失败：{e}")
"""
    
    # 在 _check_all_risks 方法末尾添加 (在方法结束前)
    # 简单策略：在方法中找到最后一个检查调用后添加
    if "self._check_portfolio_risks()" in content:
        content = content.replace(
            "self._check_portfolio_risks()",
            "self._check_portfolio_risks()\n" + check_addition,
        )
        logger.info("已添加持仓跌幅检查调用")
    else:
        logger.warning("未找到 _check_portfolio_risks 调用")
        return False
    
    # 4. 添加辅助方法 _get_positions_for_drop_check
    helper_method = """
    def _get_positions_for_drop_check(self):
        \"\"\"
        获取持仓列表用于跌幅检查
        
        Returns:
            持仓列表，每项包含 {code, name, current_price, cost_price}
        \"\"\"
        positions = []
        try:
            # 从数据库或 API 获取持仓
            if self.database:
                # TODO: 实现从数据库获取持仓
                pass
            
            # 临时实现：从 HOLDINGS.md 读取
            holdings_file = Path("/root/.openclaw/workspace-ftseek-us/HOLDINGS.md")
            if holdings_file.exists():
                import re
                with open(holdings_file, "r") as f:
                    md_content = f.read()
                
                # 解析 Markdown 表格
                table_match = re.search(
                    r'\| 股票代码 \| 持仓数量 \| 平均成本 \| 当前价 \|.*?\n\|[-\| ]+\n(.*?)\n\n',
                    md_content,
                    re.DOTALL,
                )
                if table_match:
                    rows = table_match.group(1).strip().split("\n")
                    for row in rows:
                        cells = row.split("|")
                        if len(cells) >= 5:
                            code = cells[1].strip()
                            current_price_str = cells[4].strip().replace("$", "").replace(",", "")
                            cost_price_str = cells[3].strip().replace("$", "").replace(",", "")
                            
                            try:
                                current_price = float(current_price_str)
                                cost_price = float(cost_price_str)
                                positions.append({
                                    "code": code,
                                    "name": code,  # TODO: 获取股票名称
                                    "current_price": current_price,
                                    "cost_price": cost_price,
                                })
                            except ValueError:
                                continue
        except Exception as e:
            logger.error(f"获取持仓列表失败：{e}")
        
        return positions
"""
    
    # 在类末尾添加辅助方法 (在最后一个方法后)
    content = content.rstrip() + "\n" + helper_method
    logger.info("已添加辅助方法 _get_positions_for_drop_check")
    
    # 写入修改后的文件
    with open(RISK_MONITOR_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    
    logger.info(f"已更新文件：{RISK_MONITOR_FILE}")
    return True


def main():
    """主函数"""
    logger.info("开始集成持仓跌幅告警")
    
    # 备份原文件
    if not backup_file():
        logger.error("备份失败，中止")
        return False
    
    # 集成
    if integrate_position_drop_alert():
        logger.info("✅ 持仓跌幅告警集成成功")
        logger.info("⚠️ 注意：需要手动配置 futu_api 实例")
        logger.info("⚠️ 注意：建议测试后重启 risk_monitor 服务")
        return True
    else:
        logger.error("❌ 集成失败，已恢复备份")
        # 恢复备份
        backup_files = sorted(BACKUP_DIR.glob("risk_monitor.py.backup.*"))
        if backup_files:
            latest_backup = backup_files[-1]
            shutil.copy2(latest_backup, RISK_MONITOR_FILE)
            logger.info(f"已恢复备份：{latest_backup}")
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
