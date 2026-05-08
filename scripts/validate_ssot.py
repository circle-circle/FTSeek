#!/usr/bin/env python3
"""
SSOT Configuration Validation Script
Verifies that unified_config.yaml is the Single Source of Truth
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek')
os.chdir('/opt/ftseek/FTSeek')

import yaml
import json
from pathlib import Path

def validate_ssot():
    print("=" * 60)
    print("FTSeek SSOT Configuration Validation")
    print("=" * 60)
    
    errors = []
    warnings = []
    
    # 1. Load unified_config
    uc_path = Path('config/unified_config.yaml')
    if not uc_path.exists():
        print("❌ unified_config.yaml not found")
        return False
    
    with open(uc_path) as f:
        uc = yaml.safe_load(f)
    
    print("\n✅ Step 1: unified_config.yaml loaded successfully")
    
    # 2. Check for deprecated independent config files
    deprecated_files = [
        'config/dynamic_exit_params.yaml',
        'config/risk_config.yaml', 
        'config/app_config.yaml',
        'config/strategy_params.json',
        'config/position_strategy.yaml',
    ]
    
    print("\nStep 2: Checking deprecated config files...")
    for f in deprecated_files:
        if Path(f).exists():
            warnings.append(f"⚠️ Deprecated file still exists: {f}")
            print(f"  ⚠️ {f} EXISTS (should be removed or marked)")
        else:
            print(f"  ✅ {f} removed")
    
    # 3. Verify exit parameter consolidation
    print("\nStep 3: Verifying exit parameter SSOT...")
    has_dynamic_exit = 'dynamic_exit_params' in uc
    has_deprecated_exit = any(k in uc for k in ['stop_loss', 'take_profit', 'profit_loss'])
    
    if has_dynamic_exit:
        print("  ✅ dynamic_exit_params section exists")
    else:
        errors.append("Missing dynamic_exit_params section")
    
    # Check for _deprecated markers
    for section in ['stop_loss', 'take_profit', 'profit_loss']:
        if section in uc:
            if '_deprecated' in uc[section]:
                print(f"  ✅ {section} marked as deprecated")
            else:
                warnings.append(f"{section} not marked as deprecated")
    
    # 4. Verify watchlist consistency
    print("\nStep 4: Verifying watchlist consistency...")
    wl_trading = uc.get('trading', {}).get('watch_list_hk', [])
    wl_hk = uc.get('watchlist', {}).get('hk_stocks', [])
    
    if wl_trading == wl_hk:
        print(f"  ✅ Watchlist synchronized ({len(wl_trading)} stocks)")
    else:
        errors.append(f"Watchlist mismatch: trading={len(wl_trading)}, hk_stocks={len(wl_hk)}")
    
    # Check all stocks in tiers
    tier_stocks = []
    for tier_data in uc.get('stock_pool', {}).get('tiers', {}).values():
        tier_stocks.extend(tier_data.get('stocks', []))
    
    missing = set(wl_trading) - set(tier_stocks)
    if not missing:
        print(f"  ✅ All watchlist stocks in tiers")
    else:
        errors.append(f"{len(missing)} stocks missing from tiers: {missing}")
    
    # 5. Verify strategy configuration
    print("\nStep 5: Verifying strategy configuration...")
    disabled = uc.get('strategy', {}).get('disabled', [])
    factors = ['rsi', 'macd', 'bollinger', 'volume_alert']
    
    for factor in factors:
        if factor in disabled:
            errors.append(f"Factor {factor} in disabled list but enabled individually")
    
    confluence_v2_enabled = uc.get('strategy', {}).get('confluence_v2', {}).get('enabled')
    if confluence_v2_enabled:
        print(f"  ✅ confluence_v2 enabled")
    else:
        warnings.append("confluence_v2 disabled")
    
    # 6. Verify circuit breaker alignment
    print("\nStep 6: Verifying circuit breaker alignment...")
    cb_daily = uc.get('circuit_breaker', {}).get('daily_loss_pct', 0)
    risk_daily = uc.get('risk_limits', {}).get('max_daily_loss_pct', 0)
    
    # Convert to comparable values (assuming 1M capital)
    # Now cb_daily is already a percentage (0.03)
    if abs(cb_daily - risk_daily) < 0.001:
        print(f"  ✅ Circuit breaker aligned ({cb_daily:.0%} == {risk_daily:.0%})")
    else:
        errors.append(f"Circuit breaker mismatch: {cb_pct:.0%} vs {risk_daily:.0%}")
    
    # 7. Verify LLM configuration
    print("\nStep 7: Verifying LLM configuration...")
    ai_provider = uc.get('ai', {}).get('provider')
    llm_provider = uc.get('llm', {}).get('provider')
    
    if llm_provider and (ai_provider == llm_provider or '_deprecated' in uc.get('ai', {})):
        print(f"  ✅ LLM provider unified ({llm_provider})")
    else:
        warnings.append(f"LLM providers may differ: ai={ai_provider}, llm={llm_provider}")
    
    # 8. Code reference check
    print("\nStep 8: Checking code references...")
    import subprocess
    result = subprocess.run(
        ['grep', '-r', 'dynamic_exit_params.yaml', 'app/', '--include=*.py'],
        capture_output=True, text=True, cwd='/opt/ftseek/FTSeek'
    )
    
    if result.returncode == 0:
        lines = result.stdout.strip().split('\n')
        active_refs = [l for l in lines if l and not l.strip().startswith('#')]
        if active_refs:
            errors.append(f"Active code references to dynamic_exit_params.yaml:")
            for ref in active_refs[:5]:
                errors.append(f"  {ref}")
        else:
            print("  ✅ No active code references to deprecated files")
    else:
        print("  ✅ No code references to deprecated files")
    
    # 9. Verify US Watchlist consistency
    print("\nStep 9: Verifying US watchlist consistency...")
    us_stocks = uc.get('watchlist', {}).get('us_stocks', [])
    us_trading = uc.get('trading', {}).get('watch_list_us', [])
    
    if set(us_stocks) == set(us_trading):
        print(f"  ✅ US Watchlist synchronized ({len(us_stocks)} stocks)")
    else:
        errors.append(f"US Watchlist mismatch: watchlist={len(us_stocks)}, trading={len(us_trading)}")
    
    # 10. Verify US Exit Params
    print("\nStep 10: Verifying US exit parameters...")
    us_exit = uc.get('dynamic_exit_params', {}).get('us', {})
    if us_exit:
        print(f"  ✅ dynamic_exit_params.us exists")
    else:
        errors.append("Missing dynamic_exit_params.us section")
    
    # Summary
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    
    if errors:
        print(f"\n❌ ERRORS ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
    
    if warnings:
        print(f"\n⚠️ WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  {w}")
    
    if not errors and not warnings:
        print("\n✅ ALL CHECKS PASSED - SSOT COMPLIANT")
        return True
    elif not errors:
        print("\n✅ SSOT COMPLIANT (with warnings)")
        return True
    else:
        print(f"\n❌ SSOT VIOLATIONS DETECTED")
        return False

if __name__ == '__main__':
    success = validate_ssot()
    sys.exit(0 if success else 1)
