#!/usr/bin/env python3
"""
策略禁用实施脚本 — 基于 ADR-042 审计发现

用法:
  python3 scripts/disable_bad_strategies.py --dry-run   # 预览变更
  python3 scripts/disable_bad_strategies.py              # 执行变更

依赖: 用户审批后方可执行 (🔴级)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 基于审计数据的禁用列表
# 格式: frozenset(策略组合) → 原因
BLACKLIST_COMBINATIONS = {
    frozenset({'st_factor', 'rsi_factor', 'bb_factor'}): 
        "10笔 0%WR -$1,550 (ADR-042)",
    frozenset({'st_factor', 'rsi_factor', 'bb_factor', 'vol_factor'}):
        "包含st_factor，历史0%WR (ADR-042)",
    frozenset({'rsi_factor', 'bb_factor', 'vol_factor'}):
        "5笔 0%WR -$2,260 (ADR-042)",
}

BLACKLIST_STRATEGIES = {
    'confluence_v2': "5笔 0%WR -$125 (ADR-042)",
}

def check_current_config():
    """检查当前配置中是否已有黑名单"""
    try:
        from config.config_center import get_config_center
        cc = get_config_center()
        current_blacklist = cc.get('strategy.blacklist_strategies', [])
        current_combo_blacklist = cc.get('strategy.blacklist_combinations', [])
        print(f"当前 strategy.blacklist_strategies: {current_blacklist}")
        print(f"当前 strategy.blacklist_combinations: {current_combo_blacklist}")
        return current_blacklist, current_combo_blacklist
    except Exception as e:
        print(f"配置读取失败: {e}")
        return [], []

def generate_yaml_patch():
    """生成需要添加到 unified_config.yaml 的配置"""
    yaml_patch = """
# === ADR-042: 策略黑名单 (基于 2026-05-26 审计) ===
# 以下策略/组合历史胜率为0%，禁用后预期止损 +$3,935
strategy:
  blacklist_strategies:
    - confluence_v2        # 5笔 0%WR -$125
  blacklist_combinations:
    - [st_factor, rsi_factor, bb_factor]                    # 10笔 0%WR -$1,550
    - [st_factor, rsi_factor, bb_factor, vol_factor]        # 包含st_factor
    - [rsi_factor, bb_factor, vol_factor]                   # 5笔 0%WR -$2,260
"""
    return yaml_patch

def verify_impact():
    """验证禁用后的预期影响"""
    try:
        import sqlite3
        conn = sqlite3.connect('data/ftseek.db')
        cur = conn.cursor()
        
        # 统计将被禁用的交易
        affected = []
        for combo, reason in BLACKLIST_COMBINATIONS.items():
            combo_str = ' + '.join(sorted(combo))
            strategy_name = f"共识({combo_str})"
            cur.execute("""
                SELECT count(*), round(sum(pnl),2) 
                FROM trades WHERE status='CLOSED' AND strategy = ?
            """, (strategy_name,))
            row = cur.fetchone()
            if row and row[0] > 0:
                affected.append((combo_str, row[0], row[1], reason))
        
        for strategy, reason in BLACKLIST_STRATEGIES.items():
            cur.execute("""
                SELECT count(*), round(sum(pnl),2) 
                FROM trades WHERE status='CLOSED' AND strategy = ?
            """, (strategy,))
            row = cur.fetchone()
            if row and row[0] > 0:
                affected.append((strategy, row[0], row[1], reason))
        
        conn.close()
        
        print("\n=== 预期影响 ===")
        total_saved = 0
        for name, count, pnl, reason in affected:
            print(f"  {name}: {count}笔, PnL={pnl}, 原因: {reason}")
            if pnl and pnl < 0:
                total_saved += abs(pnl)
        print(f"\n  预期止损总额: +{total_saved:.0f} HKD")
        return affected
    except Exception as e:
        print(f"影响评估失败: {e}")
        return []

def main():
    dry_run = '--dry-run' in sys.argv
    
    print("=" * 60)
    print("策略禁用实施脚本 (ADR-042)")
    print("=" * 60)
    
    # 1. 检查当前配置
    print("\n[1/3] 当前配置:")
    check_current_config()
    
    # 2. 验证影响
    print("\n[2/3] 影响评估:")
    affected = verify_impact()
    
    # 3. 生成配置补丁
    print("\n[3/3] YAML 配置补丁:")
    patch = generate_yaml_patch()
    print(patch)
    
    if dry_run:
        print("\n🔍 DRY-RUN 模式 — 未执行任何变更")
        print("   确认后运行: python3 scripts/disable_bad_strategies.py")
    else:
        print("\n⚠️  实施模式")
        print("   请将上述 YAML 补丁手动添加到 config/unified_config.yaml")
        print("   然后重启服务: sudo systemctl restart ftseek")
        print("   或在 consensus_engine.py 中添加代码级黑名单 (需🔴审批)")

if __name__ == '__main__':
    main()
