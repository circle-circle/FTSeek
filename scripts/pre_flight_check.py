#!/usr/bin/env python3
"""FTSeek Pre-Market P0 Readiness Check"""

import sys
import os
import importlib
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("pre_flight_check")

def check_import(module_name, class_name=None):
    try:
        mod = importlib.import_module(module_name)
        logger.info(f"✅ Import {module_name}")
        if class_name:
            cls = getattr(mod, class_name)
            logger.info(f"   └─ Class {class_name} found")
            return True
    except Exception as e:
        logger.error(f"❌ Import {module_name} FAILED: {e}")
        return False
    return True

def check_config(path):
    try:
        import yaml
        with open(path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Verify Phase 2 Config presence
        sf = config.get('signal_fusion', {})
        if 'hk' in sf and 'us' in sf:
            logger.info(f"✅ Config {path} Valid (Phase 2 signal_fusion present)")
            return True
        else:
            logger.warning(f"⚠️ Config {path} Missing signal_fusion section")
            return True # Non-blocking
    except Exception as e:
        logger.error(f"❌ Config {path} FAILED: {e}")
        return False

def check_pytz():
    try:
        import pytz
        logger.info("✅ Dependency pytz available")
        return True
    except ImportError:
        logger.warning("⚠️ pytz not found (Auction strategy might fallback)")
        return False

def main():
    logger.info("🔥🔥 FTSeek Pre-Market P0 Readiness Check 🔥🔥")
    
    checks_passed = 0
    total_checks = 0

    # 1. Dependencies
    total_checks += 1
    if check_pytz(): checks_passed += 1

    # 2. Core Modules
    total_checks += 1
    if check_import("app.core.trade_executor", "TradeExecutor"): checks_passed += 1

    total_checks += 1
    if check_import("app.pipeline.strategy_router", "StrategyRouter"): checks_passed += 1
    
    total_checks += 1
    if check_import("app.pipeline.premarket_analyst", "PreMarketAnalyst"): checks_passed += 1
    
    total_checks += 1
    if check_import("app.strategy.base", "Signal"): checks_passed += 1

    # 3. Configuration
    total_checks += 1
    if check_config("config/config.yaml"): checks_passed += 1

    # Summary
    logger.info("="*40)
    if checks_passed == total_checks:
        logger.info("🟢 ALL CHECKS PASSED. SYSTEM READY.")
        sys.exit(0)
    else:
        logger.error(f"🔴 {total_checks - checks_passed} CHECKS FAILED. DO NOT START.")
        sys.exit(1)

if __name__ == "__main__":
    # Ensure CWD is correct
    os.chdir("/opt/ftseek/FTSeek")
    if "." not in sys.path:
        sys.path.insert(0, ".")
    main()
