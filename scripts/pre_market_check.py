#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 盘前预检 (Pre-Market Check)

执行时间: 北京时间 20:00 (美东 08:00, Hermes "FTSeek-US 盘前综合预检" cron 触发)
目的: 确保开盘前系统就绪，检查昨日遗留问题

检查项 (2026-07-21 P0+P1 修订):
  1. 服务进程状态 (systemctl)
  2. 做空策略路由器初始化
  3. 退出策略兜底配置 (注: playbook 指令逐条自带退出参数)
  5. 数据源连接 (AAPL 报价)
  5b. DA bridge 就绪 (opportunity_scan_{ET今日}.json 候选数/新鲜度)
  5c. 当日指令统计 (DA/UE 来源分布)
  5d. 持仓遗留 (过夜 open 仓位)
  5e. 事件文件就绪 (event_symbols_{ET今日}.json, P2' 生成器心跳)
  6. 昨日交易回顾 (trade_records DB 权威源, 按 trade_date)
  7. 日志错误扫描 (最近500行)
  8. 系统资源 (服务 MainPID, 非预检进程自身)
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
    执行盘前预检 (检查项清单见模块 docstring, 2026-07-21 P0+P1 修订)

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
        default_method = cfg.get("default_method", "unknown")  # 2026-07-21 修复键名漂移: 配置真实键=default_method, 此前读default_short_method永远显示unknown
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
                      f"Trailing={'on' if trailing.get('enabled') else 'off'} | 注: 此为无指令兜底值, playbook指令逐条自带SL/TP/退出策略"
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

    # ── 5b. DA bridge 就绪 (2026-07-21 P0: 供给链路核心; DA 18:00任务 ~19:10 产出当日文件) ──
    try:
        from app.utils.timezone import now_et as _now_et_chk
        today_et = _now_et_chk().strftime("%Y-%m-%d")
        bp = BASE_DIR / "data" / f"opportunity_scan_{today_et}.json"
        if not bp.exists():
            results["checks"].append({"name": "DA bridge 就绪", "status": "WARN",
                                      "detail": f"{today_et} bridge 未生成 (DA 18:00任务未产出或今日非交易日)"})
            results["warnings"].append(f"bridge 未生成: opportunity_scan_{today_et}.json")
        else:
            _bd = json.loads(bp.read_text())
            _bc = _bd.get("candidates", [])
            _age = (datetime.now() - datetime.fromtimestamp(bp.stat().st_mtime)).total_seconds() / 60
            _hints = {}
            for c in _bc:
                h = c.get("position_size_hint") or "?"
                _hints[h] = _hints.get(h, 0) + 1
            _st = "PASS" if _bc and _age < 360 else "WARN"
            results["checks"].append({"name": "DA bridge 就绪", "status": _st,
                                      "detail": f"{len(_bc)} 候选 | size={_hints} | {_age:.0f}分钟前生成"})
            if _st == "WARN":
                results["warnings"].append(f"bridge 异常: {len(_bc)}候选/{_age:.0f}分钟前生成")
    except Exception as e:
        results["checks"].append({"name": "DA bridge 就绪", "status": "ERROR", "detail": str(e)})

    # ── 5c. 当日指令统计 (decision_id 前缀归因; UE 21:10注入, 预检时点通常为0属正常) ──
    try:
        import sqlite3 as _sq
        _con = _sq.connect(f"file:{BASE_DIR / 'data' / 'ftseek_us.db'}?mode=ro", uri=True)
        _r = _con.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN decision_id LIKE 'UE%' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN decision_id LIKE 'DA%' THEN 1 ELSE 0 END),0) "
            "FROM playbook_instructions WHERE date=?", (today_et,)).fetchone()
        _con.close()
        results["checks"].append({"name": "当日指令统计", "status": "PASS" if (_r[0] or 0) > 0 else "WARN",
                                  "detail": f"{today_et}: 共{_r[0] or 0}条 | DA={_r[2] or 0} | UE={_r[1] or 0} (UE 21:10注入)"})
    except Exception as e:
        results["checks"].append({"name": "当日指令统计", "status": "ERROR", "detail": str(e)})

    # ── 5d. 持仓遗留 (过夜风险第一眼) ──
    try:
        import sqlite3 as _sq
        _con2 = _sq.connect(f"file:{BASE_DIR / 'data' / 'ftseek_us.db'}?mode=ro", uri=True)
        _r2 = _con2.execute(
            "SELECT COUNT(*), COALESCE(GROUP_CONCAT(symbol),'') FROM trade_records "
            "WHERE exit_time IS NULL OR exit_time=''").fetchone()
        _con2.close()
        results["checks"].append({"name": "持仓遗留", "status": "PASS",
                                  "detail": f"open {_r2[0] or 0} 笔" + (f" ({_r2[1]})" if _r2[1] else "")})
    except Exception as e:
        results["checks"].append({"name": "持仓遗留", "status": "ERROR", "detail": str(e)})

    # ── 5e. 事件文件就绪 (P2' 新组件; 兼作 18:30 生成器 cron 心跳) ──
    try:
        ep = BASE_DIR / "data" / f"event_symbols_{today_et}.json"
        if not ep.exists():
            results["checks"].append({"name": "事件文件就绪", "status": "WARN",
                                      "detail": f"{today_et} 未生成 (18:30 生成器未运行?)"})
        else:
            _ed = json.loads(ep.read_text())
            _es = _ed.get("event_symbols", [])
            _note = ("quiet day (回退核心熟股)" if _ed.get("quiet_day_fallback")
                     else f"{len(_es)} 事件标的: {', '.join(str(x.get('symbol','')).replace('US.','') for x in _es[:6])}")
            results["checks"].append({"name": "事件文件就绪", "status": "PASS", "detail": _note})
    except Exception as e:
        results["checks"].append({"name": "事件文件就绪", "status": "ERROR", "detail": str(e)})

    # ── 6. 昨日交易回顾 ──
    try:
        from app.utils.timezone import now_et, get_last_trade_date
        from datetime import timedelta as _td
        import sqlite3 as _sq
        # 2026-07-21 修复双 bug:
        #   ① get_last_trade_date 交易日含当天 → "昨日"=前一交易日 (传 now-1d, 函数自带跳周末);
        #   ② 数据源从"当前日志文本匹配模拟成交"(北京零点轮转后永远≈0) 改为 trade_records DB 权威源,
        #      按 trade_date 统计 (与补录记录口径一致).
        yesterday = get_last_trade_date(now_et() - _td(days=1))
        _con3 = _sq.connect(f"file:{BASE_DIR / 'data' / 'ftseek_us.db'}?mode=ro", uri=True)
        _r3 = _con3.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN direction='LONG' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN direction LIKE 'SHORT%' THEN 1 ELSE 0 END),0), "
            "COALESCE(ROUND(SUM(COALESCE(pnl_net, realized_pnl)),2),0) "  # 2026-08-20 净口径切换
            "FROM trade_records WHERE trade_date=?", (yesterday,)).fetchone()
        _con3.close()
        results["checks"].append({
            "name": "昨日交易回顾",
            "status": "PASS",
            "detail": f"日期={yesterday} | 总交易={_r3[0] or 0} | 做多={_r3[1] or 0} | 做空={_r3[2] or 0} | 已实现PnL=${_r3[3] or 0}"
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
        import subprocess as _sp
        # 2026-07-21 修复: 此前 psutil.Process() 测的是预检脚本自身 (报告的104MB系误报);
        #   改测 ftseek-us 服务 MainPID; 获取失败才回退测自身并明示.
        _pid_s = _sp.run(["systemctl", "show", "-p", "MainPID", "--value", "ftseek-us.service"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
        _pid = int(_pid_s) if _pid_s.isdigit() and int(_pid_s) > 0 else 0
        if _pid > 0:
            _p = psutil.Process(_pid)
            mem_mb = _p.memory_info().rss / 1024 / 1024
            cpu_pct = _p.cpu_percent(interval=0.2)
            src = f"服务PID={_pid}"
        else:
            _p = psutil.Process()
            mem_mb = _p.memory_info().rss / 1024 / 1024
            cpu_pct = _p.cpu_percent()
            src = "预检进程(服务PID获取失败)"

        results["checks"].append({
            "name": "系统资源",
            "status": "PASS" if mem_mb < 1500 else "WARN",
            "detail": f"{src} | 内存={mem_mb:.0f}MB | CPU={cpu_pct:.1f}%"
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
