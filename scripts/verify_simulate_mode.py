#!/usr/bin/env python3
"""
模拟交易模式验证脚本
确保系统在模拟环境下运行，保护测试阶段资金安全

Usage:
    python scripts/verify_simulate_mode.py
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

def check_config_files():
    """检查配置文件中的交易模式"""
    import yaml
    import json
    
    print("=" * 60)
    print("📋 配置文件检查")
    print("=" * 60)
    
    issues = []
    
    # 检查 app_config.yaml
    app_config_path = PROJECT_ROOT / "config" / "app_config.yaml"
    if app_config_path.exists():
        with open(app_config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        trading_mode = config.get('trading', {}).get('mode', 'unknown')
        trade_env = config.get('futu', {}).get('trade_env', 0)
        
        print(f"  app_config.yaml:")
        print(f"    trading.mode: {trading_mode}")
        print(f"    futu.trade_env: {trade_env}")
        
        if trading_mode != 'paper':
            issues.append(f"app_config.yaml trading.mode 应为 'paper'，当前为 '{trading_mode}'")
        if trade_env != 1:
            issues.append(f"app_config.yaml futu.trade_env 应为 1，当前为 {trade_env}")
    
    # 检查 risk_config.yaml
    risk_config_path = PROJECT_ROOT / "config" / "risk_config.yaml"
    if risk_config_path.exists():
        with open(risk_config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        trading_mode = config.get('trading', {}).get('mode', 'unknown')
        
        print(f"  risk_config.yaml:")
        print(f"    trading.mode: {trading_mode}")
        
        if trading_mode != 'paper':
            issues.append(f"risk_config.yaml trading.mode 应为 'paper'，当前为 '{trading_mode}'")
    
    # 检查 strategy_params.json
    strategy_params_path = PROJECT_ROOT / "config" / "strategy_params.json"
    if strategy_params_path.exists():
        with open(strategy_params_path, 'r') as f:
            config = json.load(f)
        
        trading_mode = config.get('trading_mode', 'unknown')
        
        print(f"  strategy_params.json:")
        print(f"    trading_mode: {trading_mode}")
        
        if trading_mode not in ['simulate', 'paper']:
            issues.append(f"strategy_params.json trading_mode 应为 'simulate'，当前为 '{trading_mode}'")
    
    return issues


def check_env_variables():
    """检查环境变量"""
    print("\n" + "=" * 60)
    print("🔧 环境变量检查")
    print("=" * 60)
    
    issues = []
    
    trade_env = os.getenv("TRADE_ENV", "1")
    force_simulate = os.getenv("FORCE_SIMULATE", "false")
    simulate_account = os.getenv("SIMULATE_ACCOUNT_ID", "")
    
    print(f"  TRADE_ENV: {trade_env}")
    print(f"  FORCE_SIMULATE: {force_simulate}")
    print(f"  SIMULATE_ACCOUNT_ID: {simulate_account}")
    
    if trade_env != "1":
        issues.append(f"TRADE_ENV 应为 '1' (模拟环境)，当前为 '{trade_env}'")
    
    return issues


def check_futu_account():
    """检查富途账户状态"""
    print("\n" + "=" * 60)
    print("🏦 富途账户检查")
    print("=" * 60)
    
    try:
        from futu import OpenSecTradeContext, RET_OK
        
        host = os.getenv("OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("OPEND_PORT", "11111"))
        
        ctx = OpenSecTradeContext(host=host, port=port)
        ret, data = ctx.get_acc_list()
        
        if ret == RET_OK:
            simulate_accounts = []
            real_accounts = []
            
            for _, row in data.iterrows():
                acc_id = row['acc_id']
                acc_type = row['trd_env']
                
                if acc_type == 'SIMULATE':
                    simulate_accounts.append(acc_id)
                    print(f"  ✅ 模拟账户: {acc_id}")
                else:
                    real_accounts.append(acc_id)
                    print(f"  ⚠️ 实盘账户: {acc_id}")
            
            print(f"\n  模拟账户数: {len(simulate_accounts)}")
            print(f"  实盘账户数: {len(real_accounts)}")
            
            if len(real_accounts) > 0:
                print("\n  ⚠️ 警告: 检测到实盘账户，请确保交易时使用模拟账户!")
            
            ctx.close()
            return simulate_accounts, real_accounts
        else:
            print(f"  ❌ 查询失败: {data}")
            ctx.close()
            return [], []
            
    except Exception as e:
        print(f"  ❌ 连接失败: {e}")
        return [], []


def check_current_positions():
    """检查当前持仓"""
    print("\n" + "=" * 60)
    print("📊 当前持仓检查")
    print("=" * 60)
    
    try:
        from futu import OpenSecTradeContext, RET_OK, TrdEnv
        
        host = os.getenv("OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("OPEND_PORT", "11111"))
        
        # 使用模拟环境查询
        ctx = OpenSecTradeContext(host=host, port=port)
        
        # 查询模拟账户持仓
        ret, positions = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        
        if ret == RET_OK:
            if len(positions) > 0:
                print(f"  模拟账户持仓数: {len(positions)}")
                total_pnl = 0
                for _, pos in positions.iterrows():
                    pnl = pos.get('pl_val', 0)
                    total_pnl += pnl if pnl else 0
                    print(f"    {pos['code']}: {pos['qty']} 股, 盈亏: {pnl}")
                print(f"  总盈亏: {total_pnl}")
            else:
                print("  模拟账户无持仓")
        else:
            print(f"  查询失败: {positions}")
        
        ctx.close()
        
    except Exception as e:
        print(f"  检查失败: {e}")


def main():
    """主检查流程"""
    print("\n🔍 FTSeek 模拟交易模式验证")
    print("   测试阶段安全检查\n")
    
    all_issues = []
    
    # 1. 检查配置文件
    config_issues = check_config_files()
    all_issues.extend(config_issues)
    
    # 2. 检查环境变量
    env_issues = check_env_variables()
    all_issues.extend(env_issues)
    
    # 3. 检查富途账户
    simulate_accounts, real_accounts = check_futu_account()
    
    # 4. 检查当前持仓
    check_current_positions()
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("📋 检查结果汇总")
    print("=" * 60)
    
    if all_issues:
        print("\n❌ 发现以下问题:\n")
        for i, issue in enumerate(all_issues, 1):
            print(f"  {i}. {issue}")
        print("\n⚠️ 请修复以上问题后再运行交易系统！")
        return False
    else:
        print("\n✅ 所有检查通过！")
        print("\n📌 测试阶段配置确认:")
        print("   - 交易模式: 模拟盘")
        print("   - 交易环境: SIMULATE")
        print(f"   - 可用模拟账户: {len(simulate_accounts)} 个")
        print("\n🚀 系统已准备好进行模拟交易测试！")
        return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)