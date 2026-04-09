#!/usr/bin/env python3
"""
模拟交易验证测试脚本

测试内容:
- 数据库表创建验证
- 日志记录功能测试 (模拟一笔完整交易)
- 绩效统计准确性验证
- dashboard 数据接口测试
- 端到端流程测试

测试流程:
1. 初始化数据库
2. 模拟一笔买入交易
3. 验证日志记录
4. 模拟卖出交易
5. 验证绩效统计
6. 输出测试报告

执行方式:
    python tests/simulation_validation_test.py

输出文件:
    /root/.openclaw/workspace-planner/SIMULATION_VALIDATION_TEST_REPORT.md
"""
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
import uuid

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.simulation_tables import SimulationDatabase, get_simulation_database, reset_simulation_database
from app.logging.simulation_logger import SimulationLogger, get_simulation_logger, reset_simulation_logger
from app.performance.simulation_metrics import SimulationMetricsCalculator, get_simulation_metrics_calculator, reset_simulation_metrics_calculator

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("simulation_validation_test")

# 测试报告输出路径
REPORT_PATH = Path.home() / ".openclaw" / "workspace-planner" / "SIMULATION_VALIDATION_TEST_REPORT.md"


class TestResult:
    """测试结果记录"""
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.message = ""
        self.details = {}
        self.error = None
    
    def success(self, message: str = "通过", details: Dict = None):
        self.passed = True
        self.message = message
        if details:
            self.details = details
    
    def fail(self, message: str, error: Exception = None):
        self.passed = False
        self.message = message
        self.error = str(error) if error else None


class SimulationValidationTest:
    """模拟交易验证测试器"""
    
    def __init__(self):
        """初始化测试器"""
        self.results: List[TestResult] = []
        self.start_time = datetime.now()
        
        # 初始化组件
        logger.info("初始化测试组件...")
        reset_simulation_database()
        reset_simulation_logger()
        reset_simulation_metrics_calculator()
        
        self.db = get_simulation_database()
        self.logger = get_simulation_logger()
        self.metrics_calc = get_simulation_metrics_calculator()
        
        logger.info("测试组件初始化完成")
    
    def run_all_tests(self):
        """运行所有测试"""
        logger.info("=" * 60)
        logger.info("开始模拟交易验证测试")
        logger.info("=" * 60)
        
        # 1. 数据库表创建验证
        self.test_database_tables()
        
        # 2. 账户初始化验证
        self.test_account_initialization()
        
        # 3. 日志记录功能测试
        self.test_logging()
        
        # 4. 模拟完整交易流程
        self.test_complete_trade()
        
        # 5. 绩效统计验证
        self.test_metrics_calculation()
        
        # 6. Dashboard 数据接口测试
        self.test_dashboard_api()
        
        # 7. 端到端流程测试
        self.test_end_to_end()
        
        # 生成报告
        self.generate_report()
    
    def test_database_tables(self):
        """测试 1: 数据库表创建验证"""
        result = TestResult("数据库表创建验证")
        
        try:
            tables = ['simulation_trades', 'simulation_positions', 'simulation_account', 'simulation_logs']
            missing_tables = []
            
            for table in tables:
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT name FROM sqlite_master 
                        WHERE type='table' AND name=?
                    """, (table,))
                    if not cursor.fetchone():
                        missing_tables.append(table)
            
            if missing_tables:
                result.fail(f"缺少表：{', '.join(missing_tables)}")
            else:
                result.success("所有表已创建", {'tables': tables})
                logger.info("✅ 数据库表验证通过")
        
        except Exception as e:
            result.fail("表验证失败", e)
            logger.error(f"❌ 数据库表验证失败：{e}")
        
        self.results.append(result)
    
    def test_account_initialization(self):
        """测试 2: 账户初始化验证"""
        result = TestResult("账户初始化验证")
        
        try:
            account = self.db.get_simulation_account()
            
            if not account:
                result.fail("账户未初始化")
            elif account['initial_capital'] != 1000000.0:
                result.fail(f"初始资金不正确：{account['initial_capital']}")
            elif account['current_capital'] != 1000000.0:
                result.fail(f"当前资金不正确：{account['current_capital']}")
            else:
                result.success("账户初始化正确", {
                    'initial_capital': account['initial_capital'],
                    'current_capital': account['current_capital'],
                    'available_cash': account['available_cash']
                })
                logger.info("✅ 账户初始化验证通过")
        
        except Exception as e:
            result.fail("账户验证失败", e)
            logger.error(f"❌ 账户验证失败：{e}")
        
        self.results.append(result)
    
    def test_logging(self):
        """测试 3: 日志记录功能测试"""
        result = TestResult("日志记录功能测试")
        
        try:
            # 记录信号日志
            signal_id = self.logger.log_signal(
                code='09988',
                action='BUY',
                price=78.50,
                quantity=500,
                strategy='test_strategy',
                confidence=0.85,
                reason='测试信号'
            )
            
            # 记录订单日志
            order_id = self.logger.log_order_created(
                code='09988',
                action='BUY',
                quantity=500,
                price=78.50,
                order_type='MARKET',
                signal_id=signal_id
            )
            
            # 记录成交日志
            fill_id = self.logger.log_order_filled(
                order_id=order_id,
                code='09988',
                action='BUY',
                filled_quantity=500,
                filled_price=78.50,
                commission=50.0,
                signal_id=signal_id
            )
            
            # 验证日志记录
            signals = self.logger.get_signals(days=1)
            orders = self.logger.get_orders(days=1)
            fills = self.logger.get_fills(days=1)
            
            if len(signals) >= 1 and len(orders) >= 1 and len(fills) >= 1:
                result.success("日志记录功能正常", {
                    'signal_id': signal_id,
                    'order_id': order_id,
                    'fill_id': fill_id,
                    'signals_count': len(signals),
                    'orders_count': len(orders),
                    'fills_count': len(fills)
                })
                logger.info("✅ 日志记录验证通过")
            else:
                result.fail(f"日志数量不正确：signals={len(signals)}, orders={len(orders)}, fills={len(fills)}")
        
        except Exception as e:
            result.fail("日志记录失败", e)
            logger.error(f"❌ 日志记录验证失败：{e}")
        
        self.results.append(result)
    
    def test_complete_trade(self):
        """测试 4: 模拟完整交易流程"""
        result = TestResult("模拟完整交易流程")
        
        try:
            # 生成交易 ID
            trade_id = f"TEST-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            entry_time = datetime.now().isoformat()
            
            # 1. 买入交易
            trade_data = {
                'id': trade_id,
                'code': '09988',
                'name': '阿里巴巴',
                'action': 'BUY',
                'entry_price': 78.50,
                'quantity': 500,
                'strategy': 'test_strategy',
                'signal_id': f"SIG-{uuid.uuid4().hex[:8]}",
                'entry_time': entry_time,
                'status': 'open'
            }
            
            self.db.save_simulation_trade(trade_data)
            logger.info(f"记录买入交易：{trade_id}")
            
            # 验证买入记录
            trade = self.db.get_simulation_trade(trade_id)
            if not trade or trade['status'] != 'open':
                result.fail("买入交易记录失败")
                self.results.append(result)
                return
            
            # 2. 更新持仓
            position_data = {
                'code': '09988',
                'quantity': 500,
                'avg_cost': 78.50,
                'current_price': 78.50,
                'direction': 'long',
                'entry_time': entry_time
            }
            self.db.save_simulation_position(position_data)
            logger.info("更新持仓记录")
            
            # 3. 模拟卖出 (平仓)
            exit_time = (datetime.now() + timedelta(hours=2)).isoformat()
            exit_price = 82.30
            pnl = (exit_price - 78.50) * 500 - 50  # 减去佣金
            pnl_percent = pnl / (78.50 * 500)
            
            self.db.update_simulation_trade_exit(
                trade_id=trade_id,
                exit_price=exit_price,
                pnl=pnl,
                pnl_percent=pnl_percent,
                exit_reason='止盈'
            )
            logger.info(f"记录卖出交易：盈亏={pnl:.2f}")
            
            # 验证交易记录
            trade = self.db.get_simulation_trade(trade_id)
            if trade['status'] == 'closed' and trade['exit_price'] == exit_price:
                result.success("完整交易流程正常", {
                    'trade_id': trade_id,
                    'entry_price': 78.50,
                    'exit_price': exit_price,
                    'pnl': pnl,
                    'pnl_percent': pnl_percent
                })
                logger.info("✅ 完整交易流程验证通过")
            else:
                result.fail("交易状态更新失败")
        
        except Exception as e:
            result.fail("交易流程失败", e)
            logger.error(f"❌ 交易流程验证失败：{e}")
        
        self.results.append(result)
    
    def test_metrics_calculation(self):
        """测试 5: 绩效统计准确性验证"""
        result = TestResult("绩效统计准确性验证")
        
        try:
            # 计算绩效指标
            metrics = self.metrics_calc.calculate_metrics(days=30)
            
            # 验证基本指标
            checks = {
                'total_trades': metrics.total_trades >= 1,
                'win_rate': 0 <= metrics.win_rate <= 1,
                'total_pnl': isinstance(metrics.total_pnl, (int, float)),
                'max_drawdown': 0 <= metrics.max_drawdown <= 1,
                'sharpe_ratio': isinstance(metrics.sharpe_ratio, (int, float)),
            }
            
            failed_checks = [k for k, v in checks.items() if not v]
            
            if failed_checks:
                result.fail(f"指标验证失败：{', '.join(failed_checks)}")
            else:
                result.success("绩效统计准确", metrics.to_dict())
                logger.info("✅ 绩效统计验证通过")
        
        except Exception as e:
            result.fail("绩效统计失败", e)
            logger.error(f"❌ 绩效统计验证失败：{e}")
        
        self.results.append(result)
    
    def test_dashboard_api(self):
        """测试 6: Dashboard 数据接口测试"""
        result = TestResult("Dashboard 数据接口测试")
        
        try:
            # 测试数据获取接口
            trades = self.db.get_simulation_trades(days=30)
            positions = self.db.get_simulation_positions()
            logs = self.db.get_simulation_logs(days=7)
            stats = self.db.get_simulation_statistics(days=30)
            
            # 验证数据结构
            api_checks = {
                'trades_api': isinstance(trades, list),
                'positions_api': isinstance(positions, list),
                'logs_api': isinstance(logs, list),
                'stats_api': isinstance(stats, dict),
            }
            
            failed_checks = [k for k, v in api_checks.items() if not v]
            
            if failed_checks:
                result.fail(f"接口验证失败：{', '.join(failed_checks)}")
            else:
                result.success("Dashboard 接口正常", {
                    'trades_count': len(trades),
                    'positions_count': len(positions),
                    'logs_count': len(logs),
                    'has_stats': 'account' in stats
                })
                logger.info("✅ Dashboard 接口验证通过")
        
        except Exception as e:
            result.fail("接口测试失败", e)
            logger.error(f"❌ Dashboard 接口验证失败：{e}")
        
        self.results.append(result)
    
    def test_end_to_end(self):
        """测试 7: 端到端流程测试"""
        result = TestResult("端到端流程测试")
        
        try:
            # 1. 创建新交易
            trade_id = f"E2E-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            entry_time = datetime.now().isoformat()
            
            # 2. 记录信号
            signal_id = self.logger.log_signal(
                code='00700',
                action='BUY',
                price=385.00,
                quantity=200,
                strategy='e2e_test',
                confidence=0.90,
                reason='端到端测试'
            )
            
            # 3. 创建订单
            order_id = self.logger.log_order_created(
                code='00700',
                action='BUY',
                quantity=200,
                price=385.00,
                signal_id=signal_id
            )
            
            # 4. 记录成交
            self.logger.log_order_filled(
                order_id=order_id,
                code='00700',
                action='BUY',
                filled_quantity=200,
                filled_price=385.00,
                commission=40.0,
                signal_id=signal_id
            )
            
            # 5. 保存交易记录
            trade_data = {
                'id': trade_id,
                'code': '00700',
                'name': '腾讯控股',
                'action': 'BUY',
                'entry_price': 385.00,
                'quantity': 200,
                'strategy': 'e2e_test',
                'signal_id': signal_id,
                'order_id': order_id,
                'entry_time': entry_time,
                'status': 'open'
            }
            self.db.save_simulation_trade(trade_data)
            
            # 6. 更新持仓
            self.db.save_simulation_position({
                'code': '00700',
                'quantity': 200,
                'avg_cost': 385.00,
                'current_price': 385.00
            })
            
            # 7. 验证数据一致性
            trade = self.db.get_simulation_trade(trade_id)
            position = self.db.get_simulation_position('00700')
            signals = self.logger.get_signals(code='00700', days=1)
            
            if trade and position and len(signals) > 0:
                result.success("端到端流程完整", {
                    'trade_id': trade_id,
                    'signal_id': signal_id,
                    'order_id': order_id,
                    'trade_status': trade['status'],
                    'position_quantity': position['quantity']
                })
                logger.info("✅ 端到端流程验证通过")
            else:
                result.fail("数据一致性检查失败")
        
        except Exception as e:
            result.fail("端到端流程失败", e)
            logger.error(f"❌ 端到端流程验证失败：{e}")
        
        self.results.append(result)
    
    def generate_report(self):
        """生成测试报告"""
        end_time = datetime.now()
        duration = (end_time - self.start_time).total_seconds()
        
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        success_rate = passed / total if total > 0 else 0
        
        # 生成 Markdown 报告
        report = f"""# 模拟交易验证测试报告

**生成时间**: {end_time.strftime('%Y-%m-%d %H:%M:%S')}  
**测试耗时**: {duration:.2f} 秒  
**通过率**: {success_rate:.1%} ({passed}/{total})

---

## 测试结果汇总

| 测试项 | 状态 | 说明 |
|--------|------|------|
"""
        
        for r in self.results:
            status = "✅ 通过" if r.passed else "❌ 失败"
            report += f"| {r.name} | {status} | {r.message} |\n"
        
        report += f"""
---

## 详细测试结果

"""
        
        for i, r in enumerate(self.results, 1):
            report += f"""### {i}. {r.name}

**状态**: {"✅ 通过" if r.passed else "❌ 失败"}  
**说明**: {r.message}

"""
            if r.details:
                report += "**详情**:\n```json\n"
                report += json.dumps(r.details, indent=2, ensure_ascii=False)
                report += "\n```\n\n"
            
            if r.error:
                report += f"**错误**: {r.error}\n\n"
        
        report += f"""---

## 测试环境信息

- **Python 版本**: {sys.version}
- **数据库路径**: {self.db.db_path}
- **日志目录**: {self.logger.log_dir}

---

## 结论

"""
        
        if passed == total:
            report += """✅ **所有测试通过**，模拟盘验证基础设施运行正常。

可以开始进行模拟交易测试。
"""
        else:
            report += f"""⚠️ **{total - passed} 项测试失败**，请检查失败项并修复。

失败项:
"""
            for r in self.results:
                if not r.passed:
                    report += f"- {r.name}: {r.message}\n"
        
        # 写入报告文件
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report, encoding='utf-8')
        
        logger.info("=" * 60)
        logger.info(f"测试报告已生成：{REPORT_PATH}")
        logger.info(f"通过率：{success_rate:.1%} ({passed}/{total})")
        logger.info("=" * 60)
        
        # 打印摘要
        print("\n" + report)


def main():
    """主函数"""
    logger.info("启动模拟交易验证测试...")
    
    try:
        tester = SimulationValidationTest()
        tester.run_all_tests()
        
        # 返回退出码
        passed = sum(1 for r in tester.results if r.passed)
        total = len(tester.results)
        
        if passed == total:
            logger.info("✅ 所有测试通过")
            sys.exit(0)
        else:
            logger.warning(f"⚠️  {total - passed} 项测试失败")
            sys.exit(1)
    
    except Exception as e:
        logger.error(f"❌ 测试执行失败：{e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
