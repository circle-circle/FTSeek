#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理器使用示例

演示如何使用统一配置管理系统
"""

import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from config.config_manager import get_config_manager, get_config, reload_config


def example_1_basic_usage():
    """示例1: 基本用法"""
    print("=" * 60)
    print("示例1: 基本用法")
    print("=" * 60)
    
    # 获取配置管理器
    manager = get_config_manager()
    
    # 获取单个配置值
    trading_mode = manager.get('trading.mode')
    print(f"交易模式: {trading_mode}")
    
    max_daily_trades = manager.get('trading.max_daily_trades')
    print(f"日最大交易次数: {max_daily_trades}")
    
    # 获取嵌套配置
    strategy_weights = manager.get('strategy.weights')
    print(f"策略权重: {strategy_weights}")
    
    # 使用便捷函数
    consensus_threshold = get_config('strategy.consensus.threshold')
    print(f"共识阈值: {consensus_threshold}")
    
    print()


def example_2_get_sections():
    """示例2: 获取配置区块"""
    print("=" * 60)
    print("示例2: 获取配置区块")
    print("=" * 60)
    
    manager = get_config_manager()
    
    # 获取策略配置
    strategy_config = manager.get_strategy_config()
    print(f"策略配置: {list(strategy_config.keys())}")
    
    # 获取风控配置
    risk_config = manager.get_risk_config()
    print(f"风控配置: {list(risk_config.keys())}")
    
    # 获取交易配置
    trading_config = manager.get_trading_config()
    print(f"交易配置: {list(trading_config.keys())}")
    
    print()


def example_3_config_status():
    """示例3: 查看配置状态"""
    print("=" * 60)
    print("示例3: 配置状态")
    print("=" * 60)
    
    manager = get_config_manager()
    status = manager.get_config_status()
    
    print(f"版本: {status['version']}")
    print(f"环境: {status['environment']}")
    print(f"加载时间: {status['loaded_at']}")
    print(f"配置文件数: {len(status['config_files'])}")
    print(f"监听器数: {status['listeners_count']}")
    print(f"备份数: {status['backups_count']}")
    
    print()


def example_4_backup_restore():
    """示例4: 备份和恢复配置"""
    print("=" * 60)
    print("示例4: 备份配置")
    print("=" * 60)
    
    manager = get_config_manager()
    
    # 备份配置
    backup_path = manager.backup_config("手动备份示例")
    print(f"✅ 备份路径: {backup_path}")
    
    # 列出所有备份
    backups = manager.list_backups()
    print(f"📁 备份总数: {len(backups)}")
    for backup in backups[-3:]:  # 显示最近3个
        print(f"  - {backup['name']} ({backup['created']})")
    
    print()


def example_5_hot_reload():
    """示例5: 热重载配置"""
    print("=" * 60)
    print("示例5: 配置热重载")
    print("=" * 60)
    
    manager = get_config_manager()
    
    # 添加配置变化监听器
    def on_config_change(old_config, new_config):
        print("🔔 配置已更新!")
        old_version = old_config.get('version')
        new_version = new_config.get('version')
        print(f"  旧版本: {old_version}")
        print(f"  新版本: {new_version}")
    
    manager.add_listener(on_config_change)
    
    # 手动重载配置
    print("重新加载配置...")
    reload_config()
    
    print()


def example_6_export_config():
    """示例6: 导出配置"""
    print("=" * 60)
    print("示例6: 导出完整配置")
    print("=" * 60)
    
    manager = get_config_manager()
    
    # 导出配置
    output_path = manager.export_config('/tmp/ftseek_exported_config.yaml')
    print(f"✅ 配置已导出: {output_path}")
    
    print()


def main():
    """运行所有示例"""
    print("\n" + "=" * 60)
    print("FTSeek 统一配置管理器 - 使用示例")
    print("=" * 60 + "\n")
    
    try:
        example_1_basic_usage()
        example_2_get_sections()
        example_3_config_status()
        example_4_backup_restore()
        example_5_hot_reload()
        example_6_export_config()
        
        print("=" * 60)
        print("✅ 所有示例运行完成！")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 示例运行失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
