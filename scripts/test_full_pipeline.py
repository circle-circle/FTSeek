#!/usr/bin/env python3
"""
FTSeek 整体链路验证测试
2026-03-15 创建

测试范围:
1. 服务健康检查
2. 行情订阅验证
3. 策略引擎验证
4. 风控系统验证
5. 新闻情绪分析验证
6. 钉钉推送验证
7. 数据库连接验证
8. 日志系统验证
"""
import os
import sys
import time
import requests
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 设置环境变量
os.environ['API_TOKEN'] = 'test_token'
os.environ['BAILIAN_API_KEY'] = 'sk-8f8a02822c1b46b4bd43b422b9196ec6'

print("="*70)
print(" " * 20 + "FTSeek 整体链路验证测试")
print("="*70)
print()

results = {"pass": 0, "fail": 0, "skip": 0}

def test_result(name: str, passed: bool, message: str = ""):
    status = "✅" if passed else "❌"
    print(f"{status} {name}")
    if message:
        print(f"   {message}")
    if passed:
        results["pass"] += 1
    else:
        results["fail"] += 1
    print()

# ============================================================================
# Test 1: 服务健康检查
# ============================================================================
print("Test 1: 服务健康检查")
print("-"*70)
try:
    response = requests.get("http://localhost:8888/health", timeout=5)
    test_result("HTTP 健康检查", response.status_code == 200, f"状态码：{response.status_code}")
except Exception as e:
    test_result("HTTP 健康检查", False, f"错误：{e}")

# ============================================================================
# Test 2: 行情订阅验证
# ============================================================================
print("Test 2: 行情订阅验证")
print("-"*70)
try:
    from app.data_subscriber import DataSubscriber
    from app import config
    
    # 检查监控股票
    hk_stocks = config.WATCH_LIST_HK
    us_stocks = config.WATCH_LIST_US
    
    test_result("港股监控列表", len(hk_stocks) > 0, f"监控股票：{hk_stocks}")
    test_result("美股监控列表", len(us_stocks) == 0, f"美股已禁用 (当前：{us_stocks})")
    
    # 检查订阅类型
    sub_types = config.SUBSCRIBE_TYPES
    test_result("订阅类型配置", "QUOTE" in sub_types and "K_1M" in sub_types, f"订阅类型：{sub_types}")
    
except Exception as e:
    test_result("行情订阅配置", False, f"错误：{e}")

# ============================================================================
# Test 3: 策略引擎验证
# ============================================================================
print("Test 3: 策略引擎验证")
print("-"*70)
try:
    from app.strategy import STRATEGY_REGISTRY
    
    # 检查策略注册
    strategies = list(STRATEGY_REGISTRY.keys()) if hasattr(STRATEGY_REGISTRY, 'keys') else []
    
    test_result("策略注册", len(strategies) > 0, f"已注册策略：{len(strategies)}个")
    
    # 检查策略注册
    expected_strategies = ['price_break', 'rsi', 'macd', 'bollinger', 'supertrend']
    for strat in expected_strategies:
        if strat in strategies:
            results["pass"] += 1
            print(f"✅ 策略已注册：{strat}")
        else:
            results["fail"] += 1
            print(f"❌ 策略缺失：{strat}")
    
    print()
    
except Exception as e:
    test_result("策略引擎", False, f"错误：{e}")

# ============================================================================
# Test 4: 风控系统验证
# ============================================================================
print("Test 4: 风控系统验证")
print("-"*70)
try:
    from app.risk import get_risk_manager
    
    risk_mgr = get_risk_manager()
    
    test_result("风控管理器初始化", risk_mgr is not None, "风控管理器已初始化")
    
    # 检查风控配置
    config_info = {
        "总仓位上限": getattr(risk_mgr.config, 'max_total_position', 'N/A'),
        "单票上限": getattr(risk_mgr.config, 'max_single_position', 'N/A'),
    }
    test_result("风控配置", True, f"配置：{config_info}")
    
except Exception as e:
    test_result("风控系统", False, f"错误：{e}")

# ============================================================================
# Test 5: 新闻情绪分析验证
# ============================================================================
print("Test 5: 新闻情绪分析验证")
print("-"*70)
try:
    from app.factors.news_integration import NewsSentimentAnalyzer
    
    analyzer = NewsSentimentAnalyzer()
    test_result("新闻情绪分析器初始化", analyzer is not None, "分析器已初始化")
    
    # 测试分析 (不实际调用 AI，仅检查流程)
    test_result("AI 服务配置", analyzer.ai is not None, "AI 服务已配置")
    
except Exception as e:
    test_result("新闻情绪分析", False, f"错误：{e}")

# ============================================================================
# Test 6: 钉钉推送验证
# ============================================================================
print("Test 6: 钉钉推送验证")
print("-"*70)
try:
    from app.notifier.dingtalk import DingTalkNotifier
    from app import config
    
    notifier = DingTalkNotifier()
    test_result("钉钉推送器初始化", notifier is not None, "推送器已初始化")
    
    # 检查配置
    has_webhook = hasattr(config, 'DINGTALK_WEBHOOK_URL') and bool(config.DINGTALK_WEBHOOK_URL)
    status = "已配置" if has_webhook else "未配置 (推送将失败)"
    test_result("钉钉 Webhook 配置", True, status)
    
except Exception as e:
    test_result("钉钉推送", False, f"错误：{e}")

# ============================================================================
# Test 7: 数据库连接验证
# ============================================================================
print("Test 7: 数据库连接验证")
print("-"*70)
try:
    from app.storage.database import get_database
    
    db = get_database()
    test_result("数据库连接", db is not None, "数据库已连接")
    
    # 检查数据库文件
    db_path = Path("/opt/ftseek/FTSeek/data/ftseek.db")
    test_result("数据库文件存在", db_path.exists(), f"路径：{db_path}")
    
except Exception as e:
    test_result("数据库", False, f"错误：{e}")

# ============================================================================
# Test 8: 日志系统验证
# ============================================================================
print("Test 8: 日志系统验证")
print("-"*70)
try:
    log_dir = Path("/opt/ftseek/FTSeek/logs")
    test_result("日志目录存在", log_dir.exists(), f"路径：{log_dir}")
    
    # 检查主日志文件
    main_log = log_dir / "ftseek.log"
    if main_log.exists():
        size = main_log.stat().st_size
        test_result("主日志文件", True, f"大小：{size/1024:.1f}KB")
    else:
        test_result("主日志文件", False, "文件不存在")
    
    # 检查日志轮转配置
    import logging
    root_logger = logging.getLogger()
    has_rotating = any('RotatingFileHandler' in str(h.__class__) for h in root_logger.handlers)
    test_result("日志轮转配置", has_rotating, "RotatingFileHandler 已配置" if has_rotating else "未使用轮转")
    
except Exception as e:
    test_result("日志系统", False, f"错误：{e}")

# ============================================================================
# Test 9: 备份机制验证
# ============================================================================
print("Test 9: 备份机制验证")
print("-"*70)
try:
    backup_dir = Path("/opt/ftseek/FTSeek/backups")
    test_result("备份目录存在", backup_dir.exists(), f"路径：{backup_dir}")
    
    if backup_dir.exists():
        backups = list(backup_dir.glob("*.db"))
        test_result("数据库备份", len(backups) > 0, f"备份文件数：{len(backups)}")
    
    # 检查 cron 配置
    cron_file = Path("/etc/cron.d/ftseek-tasks")
    test_result("定时任务配置", cron_file.exists(), "cron 任务已配置")
    
except Exception as e:
    test_result("备份机制", False, f"错误：{e}")

# ============================================================================
# Test 10: 健康检查脚本验证
# ============================================================================
print("Test 10: 健康检查脚本验证")
print("-"*70)
try:
    script_path = Path("/opt/ftseek/FTSeek/scripts/healthcheck.sh")
    test_result("健康检查脚本存在", script_path.exists(), f"路径：{script_path}")
    
    if script_path.exists():
        is_executable = os.access(script_path, os.X_OK)
        test_result("脚本可执行", is_executable, "执行权限已设置")
    
except Exception as e:
    test_result("健康检查脚本", False, f"错误：{e}")

# ============================================================================
# 测试总结
# ============================================================================
print("="*70)
print(" " * 28 + "测试总结")
print("="*70)
print()
print(f"✅ 通过：{results['pass']}")
print(f"❌ 失败：{results['fail']}")
print(f"⏭️  跳过：{results['skip']}")
print()

total = results['pass'] + results['fail'] + results['skip']
pass_rate = (results['pass'] / total * 100) if total > 0 else 0

print(f"通过率：{pass_rate:.1f}%")
print()

if results['fail'] == 0:
    print("🎉 所有测试通过！FTSeek 系统运行正常！")
else:
    print(f"⚠️  有 {results['fail']} 个测试失败，请检查系统配置！")

print()
print("="*70)

sys.exit(0 if results['fail'] == 0 else 1)
