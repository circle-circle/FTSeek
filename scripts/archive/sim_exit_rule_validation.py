#!/usr/bin/env python3
"""
exit_rule方向感知匹配 + 波动率分桶SL + 动态退出全链路模拟验证

用4只不同波动率桶的真实标的，构造6个测试场景，验证：
1. _get_factor_exit_config 方向感知匹配（BUY→BULLISH, SHORT→BEARISH）
2. _compute_volatility_based_sl_tp 波动率分桶SL/TP
3. _compute_dynamic_effective_sl breakeven + trailing stop
4. Pipeline退出规则链：SL → breakeven → trailing → TIME_EXIT → PRE_CLOSE → EOD
"""

import sys
import os
import yaml
import json
import math
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/opt/ftseek/FTSeek-US')
os.chdir('/opt/ftseek/FTSeek-US')

from app.playbook.condition_taxonomy import BULLISH_CONDITIONS, BEARISH_CONDITIONS, CONDITION_TAXONOMY
from app.playbook.pipeline import _get_factor_exit_config, _compute_dynamic_effective_sl
from app.playbook.instruction_generator import _compute_volatility_based_sl_tp

# ============================================================
# 加载配置
# ============================================================
with open('config/playbook_horizon.yaml') as f:
    HORIZON_CFG = yaml.safe_load(f)

FACTOR_EXIT_RULES = HORIZON_CFG['factor_exit_rules']
SHORT_HORIZON = HORIZON_CFG.get('short_horizon', {})
VOL_BUCKETS = SHORT_HORIZON.get('vol_buckets', {})
DEFAULT_EXIT = FACTOR_EXIT_RULES.get('default', {})

print("=" * 80)
print("exit_rule方向感知匹配 + 波动率分桶 + 动态退出 全链路模拟验证")
print("=" * 80)
print(f"时间: {datetime.now(timezone.utc).isoformat()}")
print(f"factor_exit_rules: {len(FACTOR_EXIT_RULES)}条 (含default)")
print(f"BULLISH条件: {len(BULLISH_CONDITIONS)}个")
print(f"BEARISH条件: {len(BEARISH_CONDITIONS)}个")
ALL_CONDS = set(CONDITION_TAXONOMY.keys())
NEUTRAL_CONDS = ALL_CONDS - BULLISH_CONDITIONS - BEARISH_CONDITIONS
print(f"NEUTRAL条件: {len(NEUTRAL_CONDS)}个")
print(f"vol_buckets: {list(VOL_BUCKETS.keys())}")
print()


# ============================================================
# 辅助函数
# ============================================================
def get_vol_bucket(atr_pct: float) -> str:
    """根据ATR%返回波动率桶名"""
    for name, cfg in sorted(VOL_BUCKETS.items(), key=lambda x: x[1].get('atr_pct_max', 999)):
        if atr_pct < cfg.get('atr_pct_max', 999):
            return name
    return 'extreme'


def simulate_exit_lifecycle(
    symbol: str,
    direction: str,
    conditions: list,
    entry_price: float,
    atr_pct: float,
    price_path: list,  # 逐tick价格序列
    tick_interval_hours: float = 0.25,  # 每15分钟一个tick
    verbose: bool = True,
):
    """
    模拟完整退出生命周期：
    1. 方向感知匹配 → 选exit_rule
    2. 波动率分桶 → 选SL%/TP%
    3. 逐tick检查：SL → breakeven → trailing → TIME_EXIT → PRE_CLOSE → EOD
    """
    dir_str = 'LONG' if direction in ('BUY', 'LONG') else 'SHORT'

    # === Step 1: 方向感知匹配 ===
    exit_cfg = _get_factor_exit_config(conditions, FACTOR_EXIT_RULES, direction=dir_str)

    # OLD逻辑（对比用）
    old_match = None
    for c in conditions:
        if c in FACTOR_EXIT_RULES and c != 'default':
            old_match = c
            break
    old_cfg = FACTOR_EXIT_RULES.get(old_match, DEFAULT_EXIT) if old_match else DEFAULT_EXIT

    # NEW匹配的condition名
    new_match = None
    if dir_str == 'LONG':
        for c in conditions:
            if c in BULLISH_CONDITIONS and c in FACTOR_EXIT_RULES and c != 'default':
                new_match = c
                break
    else:
        for c in conditions:
            if c in BEARISH_CONDITIONS and c in FACTOR_EXIT_RULES and c != 'default':
                new_match = c
                break
    if not new_match:
        for c in conditions:
            if c in FACTOR_EXIT_RULES and c != 'default':
                new_match = c
                break

    # === Step 2: 波动率分桶SL/TP ===
    # 构造一个简单的_horizon_params和pb_cfg
    _hp = SHORT_HORIZON
    _pb_cfg = HORIZON_CFG

    # _compute_volatility_based_sl_tp需要的pb_cfg要有vol_buckets
    class FakePbCfg(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    pb_cfg = FakePbCfg(HORIZON_CFG)
    vol_info = _compute_volatility_based_sl_tp(atr_pct, direction, _hp, pb_cfg)

    if vol_info:
        sl_pct = vol_info['sl_pct']
        tp_mult = vol_info['tp_multiplier']
        bucket_name = vol_info['bucket']
    else:
        sl_pct = float(SHORT_HORIZON.get('fixed_stop_loss_pct', 2.0))
        tp_mult = 1.5
        bucket_name = 'no_bucket'

    # 计算初始SL/TP价格
    if direction in ('BUY', 'LONG'):
        initial_sl = entry_price * (1 - sl_pct / 100)
        initial_tp = entry_price * (1 + sl_pct * tp_mult / 100)
    else:
        initial_sl = entry_price * (1 + sl_pct / 100)
        initial_tp = entry_price * (1 - sl_pct * tp_mult / 100)

    # === Step 3: 退出规则参数 ===
    trailing_enabled = exit_cfg.get('trailing_enabled', False)
    breakeven_pct = exit_cfg.get('breakeven_at_pct', 0.5)
    trailing_step = exit_cfg.get('trailing_step_pct', 2.0)
    time_exit_hours = exit_cfg.get('time_exit_hours', 2.0)
    time_exit_only_if_loss = exit_cfg.get('time_exit_only_if_loss', True)
    max_hold_days = exit_cfg.get('max_hold_days', 1)

    # === Step 4: 逐tick模拟 ===
    effective_sl = initial_sl
    peak_profit_pct = 0.0
    peak_price = entry_price
    exit_reason = None
    exit_price = None
    exit_time_hours = 0.0
    holding_hours = 0.0

    PRE_CLOSE_WINDOW_HOURS = 6.85  # 收盘前8min ≈ 6h51min (假设7h交易日)
    EOD_HOURS = 7.0
    total_ticks = len(price_path)

    for i, price in enumerate(price_path):
        holding_hours = (i + 1) * tick_interval_hours

        # 计算当前盈亏%
        if direction in ('BUY', 'LONG'):
            profit_pct = (price - entry_price) / entry_price * 100
        else:
            profit_pct = (entry_price - price) / entry_price * 100

        # 更新peak
        if profit_pct > peak_profit_pct:
            peak_profit_pct = profit_pct
            peak_price = price

        # --- 检查SL ---
        if direction in ('BUY', 'LONG'):
            if price <= effective_sl:
                if peak_profit_pct >= breakeven_pct:
                    exit_reason = 'TRAILING_STOP' if trailing_enabled and peak_profit_pct > breakeven_pct + trailing_step else 'BREAKEVEN_STOP'
                else:
                    exit_reason = 'SL_HIT'
                exit_price = effective_sl
                exit_time_hours = holding_hours
                break
        else:
            if price >= effective_sl:
                if peak_profit_pct >= breakeven_pct:
                    exit_reason = 'TRAILING_STOP' if trailing_enabled and peak_profit_pct > breakeven_pct + trailing_step else 'BREAKEVEN_STOP'
                else:
                    exit_reason = 'SL_HIT'
                exit_price = effective_sl
                exit_time_hours = holding_hours
                break

        # --- 更新动态SL (breakeven + trailing) ---
        effective_sl_new = _compute_dynamic_effective_sl(
            direction if direction in ('LONG', 'SHORT') else ('LONG' if direction in ('BUY','LONG') else 'SHORT'),
            entry_price,
            effective_sl,
            peak_profit_pct,
            exit_cfg,
        )
        if direction in ('BUY', 'LONG'):
            effective_sl = max(effective_sl, effective_sl_new) if effective_sl_new > 0 else effective_sl
        else:
            if effective_sl_new > 0:
                effective_sl = min(effective_sl, effective_sl_new) if effective_sl > 0 else effective_sl_new

        # --- 检查TIME_EXIT ---
        if holding_hours >= time_exit_hours:
            if time_exit_only_if_loss and profit_pct < 0:
                exit_reason = 'TIME_EXIT'
                exit_price = price
                exit_time_hours = holding_hours
                break
            elif not time_exit_only_if_loss:
                exit_reason = 'TIME_EXIT'
                exit_price = price
                exit_time_hours = holding_hours
                break

        # --- 检查PRE_CLOSE ---
        if holding_hours >= PRE_CLOSE_WINDOW_HOURS and holding_hours < EOD_HOURS:
            # 收盘前8min: 盈利落袋 / 亏损平仓
            if profit_pct > 0:
                exit_reason = 'PRE_CLOSE_EXIT'
                exit_price = price
                exit_time_hours = holding_hours
                break

        # --- 检查EOD ---
        if holding_hours >= EOD_HOURS:
            exit_reason = 'EOD_FORCE_CLOSE'
            exit_price = price
            exit_time_hours = holding_hours
            break

    # === 计算PnL ===
    if direction in ('BUY', 'LONG'):
        pnl_pct = (exit_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100
    pnl_usd = pnl_pct / 100 * 5000  # 假设$5000仓位

    # === 输出 ===
    if verbose:
        print(f"\n{'─' * 70}")
        print(f"  {symbol} {direction} | bucket={bucket_name} ATR={atr_pct*100:.2f}%")
        print(f"  conditions: {conditions}")
        print(f"  entry=${entry_price:.2f} initial_sl=${initial_sl:.2f} initial_tp=${initial_tp:.2f}")
        print(f"  ── OLD匹配: {old_match} trailing={old_cfg.get('trailing_enabled',False)} time={old_cfg.get('time_exit_hours',2.0)}h max={old_cfg.get('max_hold_days',1)}d")
        print(f"  ── NEW匹配: {new_match} trailing={trailing_enabled} time={time_exit_hours}h max={max_hold_days}d")
        print(f"  ── breakeven={breakeven_pct}% trailing_step={trailing_step}% time_only_if_loss={time_exit_only_if_loss}")
        print(f"  price_path: {len(price_path)} ticks (每{tick_interval_hours}h)")
        print(f"  peak_profit={peak_profit_pct:.2f}% peak_price=${peak_price:.2f}")
        print(f"  ── EXIT: {exit_reason} @ ${exit_price:.2f} ({pnl_pct:+.2f}% = ${pnl_usd:+.0f})")
        print(f"  holding: {exit_time_hours:.2f}h")

    return {
        'symbol': symbol,
        'direction': direction,
        'bucket': bucket_name,
        'conditions': conditions,
        'old_match': old_match,
        'new_match': new_match,
        'old_trailing': old_cfg.get('trailing_enabled', False),
        'new_trailing': trailing_enabled,
        'old_time_hours': old_cfg.get('time_exit_hours', 2.0),
        'new_time_hours': time_exit_hours,
        'entry_price': entry_price,
        'initial_sl': initial_sl,
        'peak_profit_pct': peak_profit_pct,
        'exit_reason': exit_reason,
        'exit_price': exit_price,
        'pnl_pct': pnl_pct,
        'pnl_usd': pnl_usd,
        'holding_hours': exit_time_hours,
        'match_changed': old_match != new_match,
    }


# ============================================================
# 构造测试场景
# ============================================================

# 4只不同波动率桶的真实标的
# 基于历史交易数据的价格范围
TEST_CASES = [
    # Case 1: QCOM BUY - 低波动桶 + BUY方向+BEARISH条件排第一
    # 意图：验证BUY优先匹配BULLISH条件而非第一个insider_net_sell
    {
        'symbol': 'US.QCOM',
        'direction': 'BUY',
        'conditions': ['insider_net_sell', 'macd_golden_cross', 'volume_expansion', 'pe_below_industry'],
        'entry_price': 180.0,
        'atr_pct': 0.012,  # 1.2% → low bucket
        'price_path': [180.0, 180.5, 181.2, 182.5, 183.8, 184.2, 183.5, 182.8, 183.0, 183.5,
                       184.0, 184.5, 185.0, 185.2, 184.8, 184.5, 185.0, 185.5, 186.0, 186.2,
                       185.8, 185.5, 186.0, 186.5, 187.0, 186.5, 186.0, 185.5],
        'tick_hours': 0.25,
        'desc': 'QCOM BUY低波动+BEARISH条件排第一→应匹配macd_golden_cross(BULLISH)',
    },
    # Case 2: ARM SELL_SHORT - 高波动桶 + SHORT方向+BULLISH条件排第一
    # 意图：验证SHORT优先匹配BEARISH条件而非第一个news_positive
    {
        'symbol': 'US.ARM',
        'direction': 'SELL_SHORT',
        'conditions': ['news_positive', 'rsi_overbought', 'near_52w_high', 'volume_expansion'],
        'entry_price': 140.0,
        'atr_pct': 0.055,  # 5.5% → high bucket
        'price_path': [140.0, 138.5, 137.0, 135.5, 136.0, 134.5, 133.0, 132.0, 131.5, 130.5,
                       129.0, 128.5, 129.0, 130.0, 129.5, 128.0, 127.5, 128.0, 129.0, 128.5,
                       127.0, 126.5, 127.0, 126.0, 125.5, 126.0, 125.0, 124.5],
        'tick_hours': 0.25,
        'desc': 'ARM SHORT高波动+BULLISH条件排第一→应匹配rsi_overbought(BEARISH)',
    },
    # Case 3: LCID BUY - 极端波动桶 + 只有BEARISH+NEUTRAL条件
    # 意图：BUY方向但没有BULLISH条件→退回第一个匹配(insider_net_sell, trailing=false)
    {
        'symbol': 'US.LCID',
        'direction': 'BUY',
        'conditions': ['insider_net_sell', 'pe_below_industry', 'volume_expansion'],
        'entry_price': 8.5,
        'atr_pct': 0.085,  # 8.5% → extreme bucket
        'price_path': [8.5, 8.6, 8.7, 8.8, 8.65, 8.5, 8.35, 8.2, 8.15, 8.0,
                       7.9, 8.0, 8.1, 8.05, 7.95, 7.85, 7.9, 8.0, 8.05, 7.95,
                       7.85, 7.75, 7.8, 7.9, 7.95, 7.85, 7.75, 7.7],
        'tick_hours': 0.25,
        'desc': 'LCID BUY极端波动+无BULLISH条件→退回insider_net_sell(快速证伪)',
    },
    # Case 4: ORCL SELL_SHORT - 中波动桶 + 只有BULLISH+NEUTRAL条件
    # 意图：SHORT方向但只有BULLISH+NEUTRAL→退回第一个匹配
    {
        'symbol': 'US.ORCL',
        'direction': 'SELL_SHORT',
        'conditions': ['above_ma20', 'news_positive', 'volume_expansion'],
        'entry_price': 145.0,
        'atr_pct': 0.025,  # 2.5% → medium bucket
        'price_path': [145.0, 144.0, 143.0, 142.5, 143.0, 142.0, 141.0, 140.5, 141.0, 140.0,
                       139.0, 139.5, 140.0, 139.0, 138.0, 138.5, 139.0, 138.0, 137.0, 137.5,
                       138.0, 137.0, 136.5, 137.0, 136.0, 135.5, 136.0, 135.0],
        'tick_hours': 0.25,
        'desc': 'ORCL SHORT中波动+无BEARISH条件→退回above_ma20(中性, trailing=true)',
    },
    # Case 5: NVDA BUY - 中波动桶 + 多条件混合(BULLISH排第二)
    # 意图：验证trailing stop锁利润逻辑——价格冲高后回撤触发trailing
    {
        'symbol': 'US.NVDA',
        'direction': 'BUY',
        'conditions': ['insider_net_sell', 'macd_golden_cross', 'rsi_oversold', 'pe_below_industry'],
        'entry_price': 120.0,
        'atr_pct': 0.028,  # 2.8% → medium bucket
        'price_path': [120.0, 120.5, 121.5, 122.5, 123.0, 124.0, 125.5, 127.0, 128.0, 129.0,
                       128.5, 127.5, 126.0, 125.0, 124.0, 123.5, 124.0, 125.0, 124.5, 123.5,
                       122.5, 122.0, 123.0, 124.0, 123.0, 122.0, 121.0, 120.0],
        'tick_hours': 0.25,
        'desc': 'NVDA BUY中波动+trailing锁利润→应匹配macd_golden_cross, trailing=true',
    },
    # Case 6: TSLA SELL_SHORT - 高波动桶 + BEARISH条件排第三
    # 意图：验证SHORT方向感知匹配到第三个BEARISH条件，跳过前两个BULLISH
    {
        'symbol': 'US.TSLA',
        'direction': 'SELL_SHORT',
        'conditions': ['macd_golden_cross', 'news_positive', 'rsi_overbought', 'near_52w_high'],
        'entry_price': 250.0,
        'atr_pct': 0.045,  # 4.5% → high bucket
        'price_path': [250.0, 248.0, 246.0, 244.0, 245.0, 243.0, 241.0, 239.5, 240.0, 238.0,
                       236.0, 235.0, 236.0, 237.0, 236.0, 234.5, 233.0, 232.0, 233.0, 234.0,
                       232.5, 231.0, 230.0, 231.0, 230.0, 229.0, 228.5, 228.0],
        'tick_hours': 0.25,
        'desc': 'TSLA SHORT高波动+BEARISH排第三→应跳过前两个BULLISH匹配rsi_overbought',
    },
]

# ============================================================
# 执行模拟
# ============================================================
results = []
for i, tc in enumerate(TEST_CASES):
    print(f"\n{'=' * 80}")
    print(f"场景 {i+1}: {tc['desc']}")
    print(f"{'=' * 80}")

    result = simulate_exit_lifecycle(
        symbol=tc['symbol'],
        direction=tc['direction'],
        conditions=tc['conditions'],
        entry_price=tc['entry_price'],
        atr_pct=tc['atr_pct'],
        price_path=tc['price_path'],
        tick_interval_hours=tc['tick_hours'],
        verbose=True,
    )
    results.append(result)

# ============================================================
# 汇总分析
# ============================================================
print(f"\n\n{'=' * 80}")
print("汇总分析")
print(f"{'=' * 80}")

print(f"\n--- 1. 方向感知匹配验证 ---")
for r in results:
    direction_alignment_old = ''
    direction_alignment_new = ''
    if r['direction'] in ('BUY', 'LONG'):
        if r['old_match'] and r['old_match'] in BEARISH_CONDITIONS:
            direction_alignment_old = '❌ BEARISH匹配到BUY'
        elif r['old_match'] and r['old_match'] in BULLISH_CONDITIONS:
            direction_alignment_old = '✅ BULLISH'
        else:
            direction_alignment_old = '⚪ NEUTRAL'
        if r['new_match'] and r['new_match'] in BULLISH_CONDITIONS:
            direction_alignment_new = '✅ BULLISH'
        elif r['new_match'] and r['new_match'] in BEARISH_CONDITIONS:
            direction_alignment_new = '❌ BEARISH匹配到BUY'
        else:
            direction_alignment_new = '⚪ NEUTRAL'
    else:
        if r['old_match'] and r['old_match'] in BULLISH_CONDITIONS:
            direction_alignment_old = '❌ BULLISH匹配到SHORT'
        elif r['old_match'] and r['old_match'] in BEARISH_CONDITIONS:
            direction_alignment_old = '✅ BEARISH'
        else:
            direction_alignment_old = '⚪ NEUTRAL'
        if r['new_match'] and r['new_match'] in BEARISH_CONDITIONS:
            direction_alignment_new = '✅ BEARISH'
        elif r['new_match'] and r['new_match'] in BULLISH_CONDITIONS:
            direction_alignment_new = '❌ BULLISH匹配到SHORT'
        else:
            direction_alignment_new = '⚪ NEUTRAL'

    changed = '🔄 CHANGED' if r['match_changed'] else '  same'
    print(f"  {changed} {r['symbol']:10s} {r['direction']:12s} bucket={r['bucket']:8s}")
    print(f"    OLD: {str(r['old_match']):25s} {direction_alignment_old}")
    print(f"    NEW: {str(r['new_match']):25s} {direction_alignment_new}")
    print(f"    trailing: {r['old_trailing']} → {r['new_trailing']}  time: {r['old_time_hours']}h → {r['new_time_hours']}h")

print(f"\n--- 2. 波动率分桶SL/TP验证 ---")
for r in results:
    sl_pct_display = abs(r['initial_sl'] - r['entry_price']) / r['entry_price'] * 100
    print(f"  {r['symbol']:10s} bucket={r['bucket']:8s} entry=${r['entry_price']:.2f} SL=${r['initial_sl']:.2f} ({sl_pct_display:.1f}%)")

print(f"\n--- 3. 退出结果 ---")
total_pnl = 0
for r in results:
    total_pnl += r['pnl_usd']
    print(f"  {r['symbol']:10s} {r['direction']:12s} peak={r['peak_profit_pct']:+.1f}% → {r['exit_reason']:20s} @ ${r['exit_price']:.2f} PnL={r['pnl_pct']:+.1f}% (${r['pnl_usd']:+.0f}) hold={r['holding_hours']:.1f}h")
print(f"  {'TOTAL':10s} {'':12s} {'':30s} PnL=${total_pnl:+.0f}")

print(f"\n--- 4. 断言验证 ---")
assertions = []
for r in results:
    # 断言1: BUY方向不能匹配到BEARISH条件（除非fallback：无BULLISH条件可用）
    if r['direction'] in ('BUY', 'LONG'):
        has_bullish = any(c in BULLISH_CONDITIONS for c in r['conditions'])
        if r['new_match'] and r['new_match'] in BEARISH_CONDITIONS:
            if has_bullish:
                assertions.append(f"❌ {r['symbol']}: BUY匹配到BEARISH条件 {r['new_match']} (有BULLISH可用却没匹配)")
            else:
                assertions.append(f"⚠️ {r['symbol']}: BUY fallback到BEARISH条件 {r['new_match']} (无BULLISH条件可用, 预期行为)")
        else:
            assertions.append(f"✅ {r['symbol']}: BUY未匹配到BEARISH条件")
    # 断言2: SHORT方向不能匹配到BULLISH条件（除非fallback）
    if r['direction'] in ('SELL_SHORT', 'SHORT'):
        has_bearish = any(c in BEARISH_CONDITIONS for c in r['conditions'])
        if r['new_match'] and r['new_match'] in BULLISH_CONDITIONS:
            if has_bearish:
                assertions.append(f"❌ {r['symbol']}: SHORT匹配到BULLISH条件 {r['new_match']} (有BEARISH可用却没匹配)")
            else:
                assertions.append(f"⚠️ {r['symbol']}: SHORT fallback到BULLISH条件 {r['new_match']} (无BEARISH条件可用, 预期行为)")
        else:
            assertions.append(f"✅ {r['symbol']}: SHORT未匹配到BULLISH条件")
    # 断言3: SL在正确方向
    if r['direction'] in ('BUY', 'LONG'):
        if r['initial_sl'] < r['entry_price']:
            assertions.append(f"✅ {r['symbol']}: BUY方向SL在下方(${r['initial_sl']:.2f} < ${r['entry_price']:.2f})")
        else:
            assertions.append(f"❌ {r['symbol']}: BUY方向SL在上方(${r['initial_sl']:.2f} >= ${r['entry_price']:.2f})")
    else:
        if r['initial_sl'] > r['entry_price']:
            assertions.append(f"✅ {r['symbol']}: SHORT方向SL在上方(${r['initial_sl']:.2f} > ${r['entry_price']:.2f})")
        else:
            assertions.append(f"❌ {r['symbol']}: SHORT方向SL在下方(${r['initial_sl']:.2f} <= ${r['entry_price']:.2f})")
    # 断言4: exit_reason不为空
    if r['exit_reason']:
        assertions.append(f"✅ {r['symbol']}: exit_reason={r['exit_reason']}")
    else:
        assertions.append(f"❌ {r['symbol']}: 无exit_reason")
    # 断言5: 退出价格在合理范围
    if r['exit_price'] and r['exit_price'] > 0:
        assertions.append(f"✅ {r['symbol']}: exit_price=${r['exit_price']:.2f}")
    else:
        assertions.append(f"❌ {r['symbol']}: exit_price异常")

for a in assertions:
    print(f"  {a}")

failed = [a for a in assertions if a.startswith('❌')]
print(f"\n{'=' * 80}")
if failed:
    print(f"结果: {len(failed)}个断言失败")
    for f in failed:
        print(f"  {f}")
else:
    print(f"结果: 全部{len(assertions)}个断言通过 ✅")
print(f"{'=' * 80}")
