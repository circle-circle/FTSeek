#!/usr/bin/env python3
"""
数据收集器测试脚本

执行: python scripts/data_collector.py --test
"""
import sys
import os
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.data_collector import DataCollector
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("data_collector_test")


def test_data_collector():
    """测试数据收集器"""
    logger.info("=" * 60)
    logger.info("数据收集器测试")
    logger.info("=" * 60)
    
    try:
        collector = DataCollector()
        
        # 获取今天的日期
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"测试日期: {today}")
        
        # 测试各个功能
        logger.info("\n--- 测试 1: 收集交易数据 ---")
        trades_count = collector.collect_trades(today)
        logger.info(f"✓ 收集到 {trades_count} 笔交易")
        
        logger.info("\n--- 测试 2: 收集信号数据 ---")
        signals_count = collector.collect_signals(today)
        logger.info(f"✓ 收集到 {signals_count} 个信号")
        
        logger.info("\n--- 测试 3: 收集绩效指标 ---")
        metrics = collector.collect_metrics(today)
        logger.info(f"✓ 绩效指标: {metrics}")
        
        logger.info("\n--- 测试 4: 验证数据完整性 ---")
        completeness = collector.verify_completeness(today)
        logger.info(f"✓ 完整性: {completeness}")
        
        logger.info("\n--- 测试 5: 生成复盘报告 ---")
        report = collector.generate_report(today)
        logger.info(f"✓ 报告生成: {report}")
        
        logger.info("\n--- 测试 6: 完整收集流程 ---")
        results = collector.run_daily_collection(today)
        logger.info(f"✓ 完整收集完成")
        
        # 验证结果
        if results['status'] == 'success':
            logger.info("\n✓ 所有测试通过!")
        else:
            logger.error("\n✗ 测试失败")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = test_data_collector()
    sys.exit(0 if success else 1)
