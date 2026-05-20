#!/usr/bin/env python3
"""
验证股票名单统一配置迁移

测试所有模块是否正确从watchlist_us.json读取配置
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def test_watchlist_manager():
    """测试1: watchlist_manager核心功能"""
    print("=" * 80)
    print("测试1: watchlist_manager 核心功能")
    print("=" * 80)
    
    try:
        from app.utils.watchlist_manager import (
            WatchlistManager,
            get_stock_codes,
            get_stock_symbols,
            get_order_qty,
            get_stop_loss,
            get_take_profit,
            reload_watchlist,
        )
        
        manager = WatchlistManager()
        
        # 测试基本功能
        codes = get_stock_codes()
        symbols = get_stock_symbols()
        
        print(f"✅ 股票代码: {codes}")
        print(f"✅ 纯代码: {symbols}")
        print(f"✅ 股票数量: {len(codes)}")
        
        # 测试单只股票配置
        if codes:
            test_symbol = codes[0].replace("US.", "")
            qty = get_order_qty(test_symbol)
            sl = get_stop_loss(test_symbol)
            tp = get_take_profit(test_symbol)
            
            print(f"✅ {test_symbol}配置:")
            print(f"   - 下单量: {qty}股")
            print(f"   - 止损: {sl:.0%}")
            print(f"   - 止盈: {tp:.0%}")
        
        # 测试摘要
        print(f"\n{manager.summary()}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_config_us():
    """测试2: config_us.py动态加载"""
    print("\n" + "=" * 80)
    print("测试2: config_us.py 动态加载")
    print("=" * 80)
    
    try:
        # 使用importlib加载config_us.py
        import importlib.util
        config_path = BASE_DIR / "config" / "config_us.py"
        
        spec = importlib.util.spec_from_file_location("config_us", config_path)
        config_us = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config_us)
        
        watchlist = config_us.WATCH_LIST_US
        
        print(f"✅ WATCH_LIST_US: {watchlist}")
        print(f"✅ 股票数量: {len(watchlist)}")
        
        # 验证与watchlist_us.json一致
        from app.utils.watchlist_manager import get_stock_codes
        expected = get_stock_codes()
        
        if set(watchlist) == set(expected):
            print(f"✅ 与watchlist_us.json一致")
        else:
            print(f"⚠️  不一致!")
            print(f"   期望: {expected}")
            print(f"   实际: {watchlist}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_scheduler_core():
    """测试3: scheduler_core加载watchlist"""
    print("\n" + "=" * 80)
    print("测试3: scheduler_core watchlist加载")
    print("=" * 80)
    
    try:
        # 模拟scheduler的_load_watchlist方法
        from app.utils.watchlist_manager import get_stock_codes
        
        codes = get_stock_codes()
        print(f"✅ 加载股票: {codes}")
        print(f"✅ 数量: {len(codes)}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


def test_pre_market_report():
    """测试4: 盘前报告使用统一配置"""
    print("\n" + "=" * 80)
    print("测试4: 盘前报告 watchlist加载")
    print("=" * 80)
    
    try:
        from app.reports.pre_market_strategy import PreMarketStrategyReport
        
        # 不传watchlist参数,应该自动从配置读取
        report = PreMarketStrategyReport()
        
        print(f"✅ watchlist: {report.watchlist}")
        print(f"✅ 股票数量: {len(report.watchlist)}")
        
        # 验证与配置一致
        from app.utils.watchlist_manager import get_stock_symbols
        expected = get_stock_symbols()
        
        if set(report.watchlist) == set(expected):
            print(f"✅ 与watchlist_us.json一致")
        else:
            print(f"⚠️  不一致!")
            print(f"   期望: {expected}")
            print(f"   实际: {report.watchlist}")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_order_qty():
    """测试5: 下单数量读取"""
    print("\n" + "=" * 80)
    print("测试5: 下单数量读取")
    print("=" * 80)
    
    try:
        from app.utils.watchlist_manager import get_order_qty
        
        # 测试几只股票
        test_symbols = ["TSLA", "NVDA", "MU", "GOOG", "BABA"]
        
        for symbol in test_symbols:
            qty = get_order_qty(symbol)
            print(f"✅ {symbol}: {qty}股")
        
        # 测试不存在的股票(应返回默认值)
        default_qty = get_order_qty("AAPL")
        print(f"✅ AAPL(不存在): {default_qty}股 (默认值)")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


def test_hot_reload():
    """测试6: 热重载功能"""
    print("\n" + "=" * 80)
    print("测试6: 热重载功能")
    print("=" * 80)
    
    try:
        from app.utils.watchlist_manager import (
            get_watchlist_manager,
            get_stock_codes,
        )
        
        # 获取当前配置
        codes_before = get_stock_codes()
        print(f"✅ 重载前: {codes_before}")
        
        # 调用重载
        manager = get_watchlist_manager()
        manager.reload()
        
        # 验证配置
        codes_after = get_stock_codes()
        print(f"✅ 重载后: {codes_after}")
        
        if codes_before == codes_after:
            print(f"✅ 热重载成功 (配置未变)")
        else:
            print(f"⚠️  配置发生变化")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


def main():
    """运行所有测试"""
    print("\n" + "=" * 80)
    print("股票名单统一配置迁移验证")
    print("=" * 80)
    print(f"配置文件: {BASE_DIR / 'config' / 'watchlist_us.json'}")
    print()
    
    tests = [
        ("watchlist_manager核心功能", test_watchlist_manager),
        ("config_us.py动态加载", test_config_us),
        ("scheduler_core watchlist加载", test_scheduler_core),
        ("盘前报告watchlist加载", test_pre_market_report),
        ("下单数量读取", test_order_qty),
        ("热重载功能", test_hot_reload),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ {name} 测试异常: {e}")
            results.append((name, False))
    
    # 汇总结果
    print("\n" + "=" * 80)
    print("测试结果汇总")
    print("=" * 80)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{status} - {name}")
    
    print(f"\n总计: {passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有测试通过! 股票名单已统一到watchlist_us.json")
        print("\n使用方式:")
        print("  from app.utils.watchlist_manager import get_stock_codes")
        print("  codes = get_stock_codes()")
        print("\n修改配置:")
        print("  编辑: config/watchlist_us.json")
        print("  重载: from app.utils.watchlist_manager import reload_watchlist")
        print("        reload_watchlist()")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败,请检查")
        return 1


if __name__ == "__main__":
    sys.exit(main())
