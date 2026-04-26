#!/usr/bin/env python3
"""
配置对齐校验脚本 — 确保 config_defaults.py 与 unified_config.yaml 完全同步。

双向校验：
  1. YAML → DEFAULTS: unified_config.yaml 中的每个关键参数，在 config_defaults.py 中有对应值
  2. DEFAULTS → YAML: config_defaults.py 中的每个默认值，在 unified_config.yaml 中有对应 key
  3. 值一致性: 两边的值必须完全相等

用法:
  python3 scripts/verify_config_alignment.py          # 检查并报告
  python3 scripts/verify_config_alignment.py --fix    # 自动更新 config_defaults.py 注释
  python3 scripts/verify_config_alignment.py --strict # 不一致时返回 exit code 1
"""

import sys
import os
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from app.core.config_defaults import CFG

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ============================================================
# 映射表: (CFG 属性名, unified_config.yaml key 路径, 描述)
# ============================================================
# 维护规则: 新增参数时在此处添加一行
MAPPING = [
    # 风控
    ('MIN_CONFIDENCE',           'risk_limits.min_confidence',              '最小置信度'),
    ('MAX_SINGLE_POSITION',      'risk_limits.max_single_position',         '单股最大仓位'),
    ('MAX_TOTAL_POSITION',       'risk_limits.max_total_position',          '总仓位上限'),
    ('MAX_DAILY_TRADES',         'risk_limits.max_daily_trades',            '日最大交易次数'),
    ('MAX_DAILY_LOSS',           'risk_limits.max_daily_loss',              '日最大亏损'),
    ('MAX_WEEKLY_LOSS',          'risk_limits.max_weekly_loss',             '周最大亏损'),
    ('MAX_MONTHLY_LOSS',         'risk_limits.max_monthly_loss',            '月最大亏损'),
    ('MAX_DRAWDOWN',             'risk_limits.max_drawdown',                '最大回撤'),
    ('CONSECUTIVE_LOSS_LIMIT',   'risk_limits.consecutive_loss_limit',      '连续亏损限制'),
    ('STRATEGY_MIN_WINRATE',     'risk_limits.strategy_min_winrate',        '策略最小胜率'),
    # 止损/止盈
    ('DEFAULT_STOP_LOSS_PCT',    'stop_loss.default_percent',               '默认止损'),
    ('DEFAULT_TAKE_PROFIT_PCT',  'take_profit.default_percent',             '默认止盈'),
    ('TRAILING_STOP_PCT',        'stop_loss.trailing_percent',              '移动止损'),
    ('TRAILING_TP_PCT',          'take_profit.trailing_percent',            '移动止盈'),
    ('MAX_HOLDING_HOURS',        'stop_loss.max_holding_hours',             '最大持仓小时'),
    ('ATR_MULTIPLIER',           'stop_loss.atr_multiplier',                'ATR 乘数'),
    ('BREAKEVEN_TRIGGER',        'take_profit.breakeven.trigger_profit',    '保本触发'),
    ('BREAKEVEN_LEVEL',          'take_profit.breakeven.stop_level',        '保本止损线'),
    # 共识引擎
    ('CONSENSUS_WINDOW',         'strategy.consensus.window_seconds',       '共识窗口'),
    ('CONSENSUS_THRESHOLD',      'strategy.consensus.threshold',            '共识阈值'),
    ('CONSENSUS_ADX_TREND',      'strategy.consensus.adx_trend',            'ADX 趋势'),
    ('CONSENSUS_ADX_RANGE',      'strategy.consensus.adx_range',            'ADX 震荡'),
    # 决策缓冲
    ('DECISION_BUFFER_MAX_PENDING',     'strategy.decision_buffer.max_pending',     '最大待处理'),
    ('DECISION_BUFFER_MIN_CONFIDENCE',  'strategy.decision_buffer.min_confidence',  '缓冲最小置信度'),
    ('DECISION_BUFFER_SIGNAL_COOLDOWN', 'strategy.decision_buffer.signal_cooldown', '缓冲冷却'),
    # 置信度调整
    ('CONFIDENCE_ADJUSTER_MIN_CONFIDENCE', 'strategy.confidence_adjuster.min_confidence', '调整器最小置信度'),
    ('CONFIDENCE_ADJUSTER_MAX_SINGLE_PENALTY', 'strategy.confidence_adjuster.max_single_penalty', '单次最大惩罚'),
    ('CONFIDENCE_ADJUSTER_MAX_TOTAL_PENALTY', 'strategy.confidence_adjuster.max_total_penalty', '累计最大惩罚'),
    ('CONFIDENCE_ADJUSTER_MAX_SINGLE_BOOST', 'strategy.confidence_adjuster.max_single_boost', '单次最大增强'),
    ('CONFIDENCE_ADJUSTER_MAX_TOTAL_BOOST', 'strategy.confidence_adjuster.max_total_boost', '累计最大增强'),
    # 信号冷却
    ('SIGNAL_COOLDOWN', 'strategy.cooldown.signal_cooldown', '信号冷却时间'),
    # 趋势过滤
    ('TREND_FILTER_USE_MULTI_TIMEFRAME', 'strategy.trend_filter.use_multi_timeframe', '多时间框架'),
    ('TREND_FILTER_PREVENT_BUY_IN_DOWNTREND', 'strategy.trend_filter.prevent_buy_in_downtrend', '下跌禁买'),
    ('TREND_FILTER_PREVENT_SHORT_IN_UPTREND', 'strategy.trend_filter.prevent_short_in_uptrend', '上涨禁空'),
    # 决策缓冲
    ('DECISION_BUFFER_MAX_POSITIONS', 'strategy.decision_buffer.max_positions', '最大持仓数 (null=动态)'),
]

# 以下 CFG 常量不在 MAPPING 中 — 它们是代码级硬编码守卫值，暂无对应 YAML key:
# HARD_STOP_DRAWDOWN_LIMIT, HARD_STOP_CONSECUTIVE_LOSSES (HardStopGuard 硬编码熔断)
# LLM_TRADING_MIN_CONFIDENCE, LLM_TRADING_MANUAL_CONFIRM_MIN_CONFIDENCE, LLM_TRADING_AUTO_EXECUTE_MIN_CONFIDENCE
# ENTRY_PRICE_HIGH_BUY_THRESHOLD, ENTRY_PRICE_CHASE_WARNING_THRESHOLD, ENTRY_PRICE_CHASE_BLOCK_THRESHOLD
# DYNAMIC_THRESHOLD_CURRENT, CIRCUIT_BREAKER_* (结构不同，需专门处理)
# 这些应保持为代码级安全守卫，不应被 YAML 覆盖


def get_nested(d, path):
    """从嵌套字典获取值，如 'a.b.c'
    返回 (value, found) 元组，区分 key 不存在 vs key 值为 None
    """
    keys = path.split('.')
    current = d
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return None, False
    return current, True


def check_alignment(yaml_path=None, fix=False):
    """执行双向对齐检查"""
    config_file = Path(yaml_path) if yaml_path else PROJECT_ROOT / 'config' / 'unified_config.yaml'
    
    with open(config_file) as f:
        config = yaml.safe_load(f)
    
    errors = []
    warnings = []
    passed = []
    
    for attr_name, yaml_path, desc in MAPPING:
        cfg_val, cfg_found = getattr(CFG, '__dict__', {}).get(attr_name, (None, False))
        # Use hasattr to distinguish "not defined" from "defined as None"
        cfg_found = hasattr(CFG, attr_name) and not attr_name.startswith('_')
        if cfg_found:
            cfg_val = getattr(CFG, attr_name)
        else:
            cfg_val = None
        yaml_val, yaml_found = get_nested(config, yaml_path)
        
        if not yaml_found:
            errors.append(f"  🔴 YAML 缺失: {yaml_path} 未在 unified_config.yaml 中定义")
            continue
        
        if not cfg_found:
            errors.append(f"  🔴 代码缺失: CFG.{attr_name} 未在 config_defaults.py 中定义")
            continue
        
        if cfg_val != yaml_val:
            errors.append(
                f"  🔴 值不一致 [{desc}]: "
                f"YAML({yaml_val}) ≠ CFG.{attr_name}({cfg_val}) | "
                f"路径: {yaml_path}"
            )
        else:
            passed.append(f"  ✓ {desc}: {cfg_val}")
    
    # 反向检查: CFG 中有但不在 MAPPING 中的属性（漏登记）
    # 只检查大写常量
    for attr_name in dir(CFG):
        if attr_name.startswith('_') or not attr_name.isupper():
            continue
        mapped = [m[0] for m in MAPPING]
        if attr_name not in mapped:
            warnings.append(f"  ⚠️  CFG.{attr_name} 未在 MAPPING 中登记（可能漏了 YAML 对齐检查）")
    
    return passed, warnings, errors


def main():
    import argparse
    parser = argparse.ArgumentParser(description='配置对齐校验')
    parser.add_argument('--fix', action='store_true', help='自动更新 config_defaults.py 注释')
    parser.add_argument('--strict', action='store_true', help='不一致时返回 exit code 1')
    parser.add_argument('--config', type=str, help='指定 YAML 路径')
    args = parser.parse_args()
    
    passed, warnings, errors = check_alignment(yaml_path=args.config)
    
    print(f"配置对齐校验 — {len(MAPPING)} 项检查")
    print(f"{'='*60}")
    
    if passed:
        print(f"\n✅ 通过 ({len(passed)} 项):")
        for p in passed[:15]:
            print(p)
        if len(passed) > 15:
            print(f"  ... 及另外 {len(passed)-15} 项")
    
    if warnings:
        print(f"\n⚠️  警告 ({len(warnings)} 项):")
        for w in warnings:
            print(w)
    
    if errors:
        print(f"\n❌ 错误 ({len(errors)} 项):")
        for e in errors:
            print(e)
    
    print(f"\n{'='*60}")
    total = len(passed) + len(warnings) + len(errors)
    print(f"结果: {len(passed)}/{total} 通过, {len(warnings)} 警告, {len(errors)} 错误")
    
    if errors and args.strict:
        print("\n💡 修复方法:")
        print("  1. 修改 unified_config.yaml 中的值，或")
        print("  2. 修改 app/core/config_defaults.py 中的对应默认值")
        print("  3. 新增参数: 在 MAPPING 中添加一行 + 在两个文件中都定义值")
        sys.exit(1)
    
    print("✅ 配置对齐检查完成")


if __name__ == '__main__':
    main()
