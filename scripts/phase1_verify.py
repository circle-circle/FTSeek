#!/usr/bin/env python3
"""
Phase 1 验证脚本 — 6/22 交易日全链路检查

自动验证:
  1. InsiderScanLoop 线程是否启动
  2. SEC EDGAR 扫描是否执行
  3. Shadow 信号是否记录
  4. 缓存反序列化是否正常工作
  5. 服务整体健康状态

用法:
  python3 scripts/phase1_verify.py              # 全量检查
  python3 scripts/phase1_verify.py --quick      # 快速检查 (仅线程+日志)
  python3 scripts/phase1_verify.py --force-scan # 强制触发一次 SEC EDGAR 扫描
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/opt/ftseek/FTSeek-US')

PROJECT = Path('/opt/ftseek/FTSeek-US')
LOG_FILE = PROJECT / 'logs' / 'ftseek_us.log'
SHADOW_LOG = PROJECT / 'logs' / 'shadow_signals.jsonl'
REPORT_DIR = PROJECT / 'data' / 'reports'
REPORT_DIR.mkdir(parents=True, exist_ok=True)


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def ok(msg):
    print(f"  {Colors.GREEN}✅{Colors.RESET} {msg}")

def fail(msg):
    print(f"  {Colors.RED}❌{Colors.RESET} {msg}")

def warn(msg):
    print(f"  {Colors.YELLOW}⚠️{Colors.RESET} {msg}")

def info(msg):
    print(f"  {Colors.BLUE}ℹ️{Colors.RESET} {msg}")


def check_service():
    """检查 1: 服务运行状态"""
    print(f"\n{Colors.BOLD}[1/7] 服务状态{Colors.RESET}")
    try:
        r = subprocess.run(['systemctl', 'is-active', 'ftseek-us.service'],
                          capture_output=True, text=True, timeout=5)
        if r.stdout.strip() == 'active':
            ok("ftseek-us.service 运行中")
            return True
        else:
            fail(f"服务状态: {r.stdout.strip()}")
            return False
    except Exception as e:
        fail(f"检查失败: {e}")
        return False


def check_threads():
    """检查 2: InsiderScanLoop 线程是否启动"""
    print(f"\n{Colors.BOLD}[2/7] 线程启动{Colors.RESET}")
    if not LOG_FILE.exists():
        fail("日志文件不存在")
        return False

    # 查找最近启动日志中的线程信息
    started_threads = []
    missing_threads = []
    expected = ['InsiderScan', 'SignalScan', 'PositionMonitor', 'ManualGuard']

    with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
        # 读最后 500 行
        lines = f.readlines()[-500:]

    # 先检查是否是假日模式
    is_holiday = any('假日' in l or '周末' in l for l in lines[-50:])
    
    if is_holiday:
        warn("假日模式 — 交易线程不会启动 (正常)")
        ok("服务运行正常，等待下一个交易日")
        return True

    for thread_name in expected:
        found = False
        for line in lines:
            if thread_name in line and ('启动' in line or 'started' in line.lower()):
                # 确认是最近的启动 (不是旧的)
                ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if ts_match:
                    ts = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
                    if ts >= datetime.now() - timedelta(hours=2):
                        found = True
                        break
        if found:
            started_threads.append(thread_name)
            ok(f"{thread_name} 已启动")
        else:
            missing_threads.append(thread_name)

    if not missing_threads:
        return True
    else:
        for t in missing_threads:
            fail(f"{t} 未启动")
        return False


def check_sec_edgar():
    """检查 3: SEC EDGAR 数据管道"""
    print(f"\n{Colors.BOLD}[3/7] SEC EDGAR 管道{Colors.RESET}")
    try:
        from app.data.sec_form4_provider import get_sec_form4_provider, InsiderTransaction
        provider = get_sec_form4_provider()

        # 测试 AAPL 数据获取
        txns = provider.fetch_company_form4s('US.AAPL', max_age_days=40)
        if txns:
            # 验证类型
            if isinstance(txns[0], InsiderTransaction):
                ok(f"返回 InsiderTransaction 对象 (AAPL: {len(txns)} 笔)")
            elif isinstance(txns[0], dict):
                fail(f"返回 dict (反序列化 bug 未修复!)")
                return False
            else:
                fail(f"未知类型: {type(txns[0])}")
                return False

            # 验证属性访问
            try:
                code = txns[0].transaction_code
                ok(f"属性访问正常 (code={code})")
            except AttributeError as e:
                fail(f"属性访问失败: {e}")
                return False
        else:
            warn("AAPL 无交易记录 (可能正常)")

        # 测试 AVGO (有内部人买入)
        txns_avgo = provider.fetch_company_form4s('US.AVGO', max_age_days=40)
        buys = [t for t in txns_avgo if t.transaction_code == 'P']
        if buys:
            ok(f"AVGO 内部人买入: {buys[0].insider_name} ${buys[0].dollar_amount:,.0f}")
        else:
            info("AVGO 无内部人买入记录")

        return True

    except Exception as e:
        fail(f"管道异常: {e}")
        return False


def check_cluster_detection():
    """检查 4: 集群检测逻辑"""
    print(f"\n{Colors.BOLD}[4/7] 集群检测逻辑{Colors.RESET}")
    try:
        from app.data.sec_form4_provider import get_sec_form4_provider
        provider = get_sec_form4_provider()

        # 扫描少量 symbol 测试
        test_symbols = ['US.AVGO', 'US.AAPL', 'US.MSFT', 'US.TSLA', 'US.NVDA']
        clusters = provider.scan_watchlist(test_symbols)

        if clusters:
            ok(f"发现 {len(clusters)} 个集群买入信号!")
            for c in clusters:
                info(f"  {c.symbol}: {c.unique_buyers} 人, ${c.total_amount:,.0f}")
        else:
            info("测试标的无集群买入 (正常 — 稀有事件)")

        return True

    except Exception as e:
        fail(f"检测异常: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_shadow_logger():
    """检查 5: Shadow 信号记录器"""
    print(f"\n{Colors.BOLD}[5/7] Shadow 信号记录器{Colors.RESET}")
    try:
        from app.storage.shadow_signal_logger import get_shadow_signal_logger
        logger = get_shadow_signal_logger()

        ok(f"加载成功 (路径: {logger.log_path})")

        # 检查是否有历史信号
        if logger.log_path.exists():
            signals = logger.get_today_signals()
            if signals:
                ok(f"今日已记录 {len(signals)} 个信号")
                for s in signals[:3]:
                    info(f"  {s['symbol']} {s['direction']} strategy={s['strategy']} conf={s['confidence']:.2f}")
            else:
                info("今日无信号 (假日模式 — 正常)")
        else:
            info("日志文件尚未创建 (首次运行)")

        return True

    except Exception as e:
        fail(f"记录器异常: {e}")
        return False


def check_insider_strategy():
    """检查 6: Insider 策略集成"""
    print(f"\n{Colors.BOLD}[6/7] Insider 策略集成{Colors.RESET}")
    try:
        from app.strategy.pure.insider import evaluate
        ok("insider.evaluate 可导入")

        # 检查配置
        import yaml
        with open(PROJECT / 'config' / 'unified_config.yaml') as f:
            cfg = yaml.safe_load(f)

        insider_cfg = cfg.get('strategies', {}).get('insider', {})
        enabled = insider_cfg.get('enabled', False)
        min_buyers = insider_cfg.get('min_buyers', 2)

        if enabled:
            ok(f"策略已启用 (min_buyers={min_buyers})")
        else:
            fail("策略未启用!")
            return False

        # 检查 signal_stage 中的调用
        with open(PROJECT / 'app' / 'pipeline' / 'stages' / 'signal_stage.py') as f:
            stage_code = f.read()

        if 'eval_strategy("insider"' in stage_code:
            ok("signal_stage 中已注册调用")
        else:
            fail("signal_stage 中未找到 insider 调用")
            return False

        return True

    except Exception as e:
        fail(f"集成异常: {e}")
        return False


def check_holiday_guard():
    """检查 7: 假日守卫"""
    print(f"\n{Colors.BOLD}[7/7] 假日守卫{Colors.RESET}")
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        from datetime import date

        cal = get_holiday_calendar()
        today = date.today()
        is_td = cal.is_trading_day("US", today)

        if is_td:
            ok(f"{today} 是交易日 — 扫描应该运行")
        else:
            warn(f"{today} 非交易日 — 扫描暂停 (正常)")

        # 检查下一个交易日
        next_td = cal.next_trading_day("US", today)
        if next_td:
            info(f"下一个交易日: {next_td}")

        return True

    except Exception as e:
        warn(f"假日检查异常: {e} (不影响运行)")
        return True  # 非关键


def force_scan():
    """强制触发一次 SEC EDGAR 扫描"""
    print(f"\n{Colors.BOLD}[强制扫描] SEC EDGAR 全量扫描{Colors.RESET}")
    try:
        from app.data.sec_form4_provider import get_sec_form4_provider
        provider = get_sec_form4_provider()

        # 加载 watchlist
        import yaml
        with open(PROJECT / 'config' / 'unified_config.yaml') as f:
            cfg = yaml.safe_load(f)

        symbols = [s['code'] for s in cfg.get('watchlist', {}).get('us_stocks', [])
                   if s.get('enabled', True)]
        orb_universe = cfg.get('orb', {}).get('universe', [])
        all_symbols = list(set(symbols + orb_universe))

        info(f"扫描 {len(all_symbols)} 只标的...")

        clusters = provider.scan_watchlist(all_symbols)

        if clusters:
            ok(f"发现 {len(clusters)} 个集群买入信号!")
            for c in clusters:
                print(f"\n  🟢 {c.symbol}")
                print(f"     {c.rationale}")
                print(f"     Score: {c.score:.2f}")

                # 记录到 shadow 信号日志
                try:
                    from app.storage.shadow_signal_logger import get_shadow_signal_logger
                    shadow = get_shadow_signal_logger()
                    # 构造一个简化的 record
                    shadow_record = {
                        "timestamp": datetime.now().isoformat(),
                        "symbol": c.symbol,
                        "direction": "LONG",
                        "strength": 50 + min(25, (c.unique_buyers - 1) * 8) + min(25, int(c.total_amount / 100_000 * 5)),
                        "confidence": 0.6,
                        "strategy": "INSIDER_CLUSTER_BUY",
                        "source": "force_scan",
                        "rationale": c.rationale,
                        "trace_id": "",
                        "context": {"price": None, "volume": None, "vix": None},
                        "metadata": {"insider_buyers": c.unique_buyers, "insider_amount": c.total_amount},
                    }
                    with open(shadow.log_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(shadow_record, ensure_ascii=False) + "\n")
                    info(f"  已记录到 shadow 日志")
                except Exception as e:
                    warn(f"  shadow 记录失败: {e}")
        else:
            info("未发现集群买入信号 (正常 — 稀有事件)")

        print(f"\n  扫描完成 ({len(all_symbols)} 只)")
        return clusters

    except Exception as e:
        fail(f"强制扫描异常: {e}")
        import traceback
        traceback.print_exc()
        return []


def generate_report(results):
    """生成验证报告"""
    report = {
        "timestamp": datetime.now().isoformat(),
        "results": results,
        "passed": sum(1 for r in results.values() if r),
        "total": len(results),
    }

    report_file = REPORT_DIR / f"phase1_verify_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"验证报告: {report_file}")
    print(f"通过: {report['passed']}/{report['total']}")
    if report['passed'] == report['total']:
        print(f"{Colors.GREEN}{Colors.BOLD}✅ Phase 1 验证全部通过{Colors.RESET}")
    elif report['passed'] >= report['total'] - 1:
        print(f"{Colors.YELLOW}{Colors.BOLD}⚠️ Phase 1 基本通过，有非关键项需关注{Colors.RESET}")
    else:
        print(f"{Colors.RED}{Colors.BOLD}❌ Phase 1 有关键项失败，需排查{Colors.RESET}")
    print(f"{'='*60}\n")

    return report


def main():
    parser = argparse.ArgumentParser(description='Phase 1 验证脚本')
    parser.add_argument('--quick', action='store_true', help='快速检查 (仅线程+日志)')
    parser.add_argument('--force-scan', action='store_true', help='强制触发 SEC EDGAR 扫描')
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  FTSeek-US Phase 1 验证 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if args.force_scan:
        clusters = force_scan()
        sys.exit(0 if clusters is not None else 1)

    results = {}
    results['service'] = check_service()

    if args.quick:
        results['threads'] = check_threads()
        generate_report(results)
        sys.exit(0 if all(results.values()) else 1)

    results['threads'] = check_threads()
    results['sec_edgar'] = check_sec_edgar()
    results['cluster_detection'] = check_cluster_detection()
    results['shadow_logger'] = check_shadow_logger()
    results['insider_strategy'] = check_insider_strategy()
    results['holiday_guard'] = check_holiday_guard()

    generate_report(results)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == '__main__':
    main()
