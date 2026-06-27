#!/usr/bin/env python3
"""验证方向差异化配置读取"""
from app.risk.exit_rules.scale_out import _load_atr_tiers_from_config

print("=== 验证方向差异化配置 ===\n")

# 测试 LONG 配置
long_tiers = _load_atr_tiers_from_config("LONG")
print("LONG tiers:")
for i, t in enumerate(long_tiers):
    print(f"  Tier {i+1}: profit_atr={t['profit_atr']}, close_pct={t['close_pct']}, ratchet={t['ratchet_sl_pct']}, trailing={t.get('trailing_remainder', False)}")

# 测试 SHORT 配置
short_tiers = _load_atr_tiers_from_config("SHORT")
print("\nSHORT tiers:")
for i, t in enumerate(short_tiers):
    print(f"  Tier {i+1}: profit_atr={t['profit_atr']}, close_pct={t['close_pct']}, ratchet={t['ratchet_sl_pct']}")

# 测试 fallback
default_tiers = _load_atr_tiers_from_config("")
print(f"\nDefault tiers (fallback): {len(default_tiers)} tiers")
for i, t in enumerate(default_tiers):
    print(f"  Tier {i+1}: profit_atr={t['profit_atr']}, close_pct={t['close_pct']}")

print("\n✅ 配置读取验证通过")
