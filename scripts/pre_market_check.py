#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 盘前预检 (Pre-Market Check)

执行时间: 美东 09:00 (北京时间 21:00)
目的: 确保开盘前系统就绪，检查昨日遗留问题
"""
import sys
import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# 添加项目路径
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger("ftseek_us.premarket")


def run_premarket_check() -> dict:
    """
    执行盘前预检

    检查项:
    1. 服务进程状态
    2. 做空策略路由器初始化
    3. 快速退出引擎状态
    4. 期权持仓恢复情况
    5. 数据源连接检查
    6. 昨日交易回顾
    7. 配置一致性检查
    8. 日志错误扫描

    Returns:
        检查结果字典
    """
    results = {
        "check_type": "premarket_check",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": "US",
        "checks": [],
        "overall_status": "PASS",
        "errors": [],
        "warnings": [],
    }

    # ── 1. 服务进程检查 ──
    try:
        import subprocess
        proc = subprocess.run(["systemctl", "is-active", "ftseek-us"], capture_output=True, text=True, timeout=10)
        is_active = proc.stdout.strip() == "active"
        results["checks"].append({
            "name": "服务进程",
            "status": "PASS" if is_active else "FAIL",
            "detail": f"ftseek-us: {proc.stdout.strip()}"
        })
        if not is_active:
            results["overall_status"] = "FAIL"
            results["errors"].append("FTSeek-US 服务未运行")
    except Exception as e:
        results["checks"].append({"name": "服务进程", "status": "ERROR", "detail": str(e)})
        results["overall_status"] = "FAIL"
        results["errors"].append(f"服务检查异常: {e}")

    # ── 2. 做空策略路由器检查 ──
    try:
        from app.execution.short_strategy import load_short_config, get_short_strategy_router
        cfg = load_short_config()
        router = get_short_strategy_router(cfg)
        strategy_count = len(router.strategies)
        default_method = cfg.get("default_short_method", "unknown")
        sim_force = cfg.get("simulated_mode_force_options", False)

        results["checks"].append({
            "name": "做空策略路由器",
            "status": "PASS",
            "detail": f"策略数={strategy_count} | 默认={default_method} | 模拟盘强制期权={sim_force}"
        })
    except Exception as e:
        results["checks"].append({"name": "做空策略路由器", "status": "FAIL", "detail": str(e)})
        results["errors"].append(f"做空策略异常: {e}")

    # ── 3. 退出策略检查 ──
    try:
        from app.core.config_container import get_config
        cfg = get_config()

        # 股票退出配置 (dynamic_exit — 这是实际管仓位的)
        dyn_exit = cfg.get_section("dynamic_exit", {})
        default_exit = dyn_exit.get("default", {})
        sl_pct = default_exit.get("stop_loss", "N/A")
        tp_pct = default_exit.get("take_profit", "N/A")
        atr_mult = default_exit.get("atr_stop", {}).get("multiplier", "N/A")
        trailing = default_exit.get("trailing_stop", {})

        results["checks"].append({
            "name": "股票退出策略",
            "status": "PASS",
            "detail": f"SL={sl_pct} | TP={tp_pct} | ATR×{atr_mult} | "
                      f"Trailing={'on' if trailing.get('enabled') else 'off'}"
        })
    except Exception as e:
        results["checks"].append({"name": "退出策略", "status": "FAIL", "detail": str(e)})
        results["warnings"].append(f"退出策略检查异常: {e}")

    # ── 4. 期权持仓检查 (2026-07-01: 已删除, 自动交易不做期权) ──

    # ── 5. 数据源连接检查 ──
    try:
        from app.data.market_data_router import MarketDataRouter
        import importlib.util
        _cfg_path = BASE_DIR.parent / "config" / "config_us.py"
        if _cfg_path.exists():
            _cfg_spec = importlib.util.spec_from_file_location("config_us", _cfg_path)
            _cfg_mod = importlib.util.module_from_spec(_cfg_spec)
            _cfg_spec.loader.exec_module(_cfg_mod)

        data_router = MarketDataRouter(
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
            alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
        )
        # 测试连接 (2026-07-01: symbol 必须带 US. 前缀, 否则 get_quote 第255行直接 return None)
        test_symbol = "US.AAPL"
        test_data = data_router.get_quote(test_symbol)
        connected = test_data is not None and test_data.get("price", 0) > 0

        results["checks"].append({
            "name": "数据源连接",
            "status": "PASS" if connected else "WARN",
            "detail": f"AAPL 价格=${test_data.get('price', 'N/A') if test_data else 'N/A'}"
        })
        if not connected:
            results["warnings"].append("数据源连接异常，可能影响开盘后交易")
    except Exception as e:
        results["checks"].append({"name": "数据源连接", "status": "ERROR", "detail": str(e)})
        results["warnings"].append(f"数据源检查异常: {e}")

    # ── 6. 昨日交易回顾 ──
    try:
        from app.utils.timezone import now_et, get_last_trade_date
        yesterday = get_last_trade_date(now_et())  # ET 最后交易日, 跳过周末

        log_path = BASE_DIR / "logs" / "ftseek_us.log"
        trades_count = 0
        short_trades = 0
        long_trades = 0

        if log_path.exists():
            # P1 修复 (2026-04-22): 添加 errors='replace' 容错处理非UTF-8字符
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if yesterday in line:
                        if "模拟成交" in line or "下单成功" in line:
                            trades_count += 1
                            if "SHORT" in line:
                                short_trades += 1
                            elif "LONG" in line:
                                long_trades += 1

        results["checks"].append({
            "name": "昨日交易回顾",
            "status": "PASS",
            "detail": f"日期={yesterday} | 总交易={trades_count} | 做多={long_trades} | 做空/期权={short_trades}"
        })
    except Exception as e:
        results["checks"].append({"name": "昨日交易回顾", "status": "ERROR", "detail": str(e)})

    # ── 7. 日志错误扫描 ──
    try:
        error_count = 0
        recent_errors = []
        log_path = BASE_DIR / "logs" / "ftseek_us.log"

        if log_path.exists():
            # P1 修复 (2026-04-22): 添加 errors='replace' 容错处理非UTF-8字符
            # 读取最后 500 行
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                for line in lines[-500:]:
                    if "ERROR" in line or "CRITICAL" in line:
                        error_count += 1
                        if len(recent_errors) < 5:
                            recent_errors.append(line.strip()[-120:])

        results["checks"].append({
            "name": "日志错误扫描",
            "status": "PASS" if error_count == 0 else "WARN",
            "detail": f"最近 500 行中有 {error_count} 个 ERROR/CRITICAL" +
                      (f" | 最新: {recent_errors[0][:80]}" if recent_errors else "")
        })
        if error_count > 10:
            results["warnings"].append(f"最近日志有 {error_count} 个错误，建议检查")
    except Exception as e:
        results["checks"].append({"name": "日志错误扫描", "status": "ERROR", "detail": str(e)})

    # ── 8. 系统资源检查 ──
    try:
        import psutil
        proc = psutil.Process()
        mem_mb = proc.memory_info().rss / 1024 / 1024
        cpu_pct = proc.cpu_percent()

        results["checks"].append({
            "name": "系统资源",
            "status": "PASS" if mem_mb < 500 else "WARN",
            "detail": f"内存={mem_mb:.0f}MB | CPU={cpu_pct:.1f}%"
        })
    except Exception:
        results["checks"].append({"name": "系统资源", "status": "SKIP", "detail": "psutil 不可用"})

    # 总结
    results["summary"] = (
        f"FTSeek-US 盘前预检 | 状态={results['overall_status']} | "
        f"检查项={len(results['checks'])} | "
        f"错误={len(results['errors'])} | 警告={len(results['warnings'])}"
    )

    return results


def format_report(results: dict) -> str:
    """格式化报告为 Markdown"""
    lines = [
        "### 🔵 FTSeek-US 盘前预检报告",
        f"**时间**: {results['timestamp']}",
        f"**状态**: {'✅ 通过' if results['overall_status'] == 'PASS' else '⚠️ 有警告' if results['warnings'] else '❌ 失败'}",
        "",
        "**检查项详情**:",
        "",
    ]

    for check in results["checks"]:
        icon = "✅" if check["status"] == "PASS" else "⚠️" if check["status"] == "WARN" else "❌" if check["status"] == "FAIL" else "⏭️"
        lines.append(f"- {icon} **{check['name']}**: {check['detail']}")

    if results["errors"]:
        lines.append("")
        lines.append("**🔴 错误**:")
        for e in results["errors"]:
            lines.append(f"  - {e}")

    if results["warnings"]:
        lines.append("")
        lines.append("**🟡 警告**:")
        for w in results["warnings"]:
            lines.append(f"  - {w}")

    lines.append("")
    lines.append("---")
    lines.append(f"*报告由 FTSeek-US Harness 自动生成*")

    return "\n".join(lines)


def push_notification(report: str, results: dict):
    """推送通知到钉钉/飞书"""
    try:
        # 尝试钉钉推送
        from app.notifier.dingtalk import DingTalkNotifier
        dingtalk = DingTalkNotifier()
        dingtalk.notify_system_alert(
            title="FTSeek-US 盘前预检",
            message=report[:500] + ("..." if len(report) > 500 else ""),
            level="INFO" if results['overall_status'] == 'PASS' else "WARN"
        )
        logger.info("钉钉推送成功")
    except Exception as e:
        logger.warning(f"钉钉推送失败: {e}")
        try:
            # 回退到飞书
            from app.notifier.feishu import FeishuNotifier
            feishu = FeishuNotifier()
            feishu.send_text(report[:1000])
            logger.info("飞书推送成功")
        except Exception as e2:
            logger.error(f"飞书推送也失败: {e2}")


if __name__ == "__main__":
    results = run_premarket_check()
    report = format_report(results)

    print(report)

    # 推送通知
    push_notification(report, results)

    # 保存报告
    report_dir = BASE_DIR / "logs" / "audit"
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / f"premarket_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存: {report_file}")
