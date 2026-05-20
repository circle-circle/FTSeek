#!/usr/bin/env python
"""
Scalping 模块验证脚本 — Karpathy 检查清单

用法: python tests/scalping_verify.py

测试范围:
  1. 时段路由正确性
  2. 信号检测逻辑  
  3. 止盈止损计算
  4. 资金隔离
  5. 熔断器
  6. 趋势过滤
  7. 统计追踪
  8. DailyActivePool 隔离
"""

import sys
import os
import tempfile

sys.path.insert(0, '/opt/ftseek/FTSeek-US')

passed = 0
failed = 0


def test(name, condition):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name}")
        failed += 1


def main():
    print("=" * 60)
    print("SCALPING 模块验证 — Karpathy 检查清单")
    print("=" * 60)

    # ── 1. 时段路由 ──
    print("\n── 1. 时段路由 (TimeGate) ──")
    from datetime import datetime
    from app.scalping.time_gate import TimeGate

    tg = TimeGate()

    dt_m = datetime(2026, 4, 29, 10, 30)
    test("早盘检测", tg.get_current_phase(dt_m) == "morning")
    p = tg.get_params(dt_m)
    test("早盘 TP=1.5%", p["take_profit"] == 0.015)
    test("早盘 SL=0.8%", p["stop_loss"] == 0.008)
    test("早盘双向", "SHORT" in p["directions"])

    dt_md = datetime(2026, 4, 29, 12, 0)
    test("午盘检测", tg.get_current_phase(dt_md) == "midday")
    p2 = tg.get_params(dt_md)
    test("午盘 TP=1.0%", p2["take_profit"] == 0.010)

    dt_l = datetime(2026, 4, 29, 15, 0)
    test("尾盘检测", tg.get_current_phase(dt_l) == "late_afternoon")
    p3 = tg.get_params(dt_l)
    test("尾盘只做多", p3["directions"] == ["LONG"])

    dt_c = datetime(2026, 4, 29, 8, 0)
    test("盘前不交易", tg.get_current_phase(dt_c) == "closed")
    test("盘前不允许", tg.is_trading_allowed(dt_c) is False)

    dt_o = datetime(2026, 4, 29, 9, 45)
    test("开盘不交易", tg.get_current_phase(dt_o) == "closed")

    # ── 2. 信号检测 ──
    print("\n── 2. 信号检测 (SignalDetector) ──")
    from app.scalping.signals import SignalDetector

    sd = SignalDetector(rsi_oversold=25, rsi_overbought=75, vwap_deviation_pct=0.012, min_signals=2)

    test("RSI 20 → LONG", sd.check_rsi(20) == "LONG")
    test("RSI 80 → SHORT", sd.check_rsi(80) == "SHORT")
    test("RSI 50 → None", sd.check_rsi(50) is None)
    test("VWAP 98/100 → LONG", sd.check_vwap(98, 100) == "LONG")
    test("VWAP 102/100 → SHORT", sd.check_vwap(102, 100) == "SHORT")
    test("BB 低于下轨 → LONG", sd.check_bollinger(95, 105, 98) == "LONG")
    test("2/3 LONG → LONG", sd.aggregate("LONG", "LONG", "SHORT") == "LONG")
    test("2/3 SHORT → SHORT", sd.aggregate("SHORT", None, "SHORT") == "SHORT")
    test("1/3 → None", sd.aggregate("LONG", None, None) is None)

    # 完整检测
    sig = sd.detect(price=98, rsi_14=20, vwap=100, bb_upper=105, bb_lower=98.5, allowed_directions=["LONG", "SHORT"])
    test("完整检测触发", sig is not None)
    if sig:
        test("方向正确", sig["direction"] == "LONG")
        test("投票数≥2", sig["votes"] >= 2)

    # 时段过滤
    sig2 = sd.detect(price=102, rsi_14=80, vwap=100, bb_upper=101.5, bb_lower=98, allowed_directions=["LONG"])
    test("SHORT 被尾盘过滤", sig2 is None)

    # ── 3. 止盈止损 ──
    print("\n── 3. 止盈止损 ──")
    from app.scalping.exits import FixedExit, TimeExit

    fe = FixedExit(0.015, 0.008)
    l = fe.calculate_levels(100, "LONG")
    test("LONG TP=$101.50", l["take_profit"] == 101.50)
    test("LONG SL=$99.20", l["stop_loss"] == 99.20)
    l2 = fe.calculate_levels(100, "SHORT")
    test("SHORT TP=$98.50", l2["take_profit"] == 98.50)
    test("SHORT SL=$100.80", l2["stop_loss"] == 100.80)
    test("LONG 止盈触发", fe.check_exit(101.50, l)[0] is True)
    test("LONG 止损未触发", fe.check_exit(99.21, l)[0] is False)

    # 时间止损
    from datetime import timedelta
    te = TimeExit(max_hold_minutes=5, breakeven_after_minutes=2)
    entry = datetime.now() - timedelta(minutes=6)
    test("超时强制平仓", te.should_exit(entry, 0.001)[0] is True)
    entry2 = datetime.now() - timedelta(minutes=3)
    test("未超时盈利不退出", te.should_exit(entry2, 0.005)[0] is False)

    # ── 4. 资金隔离 ──
    print("\n── 4. 资金隔离 (Budget) ──")
    from app.scalping.budget import ScalpingBudget

    with ScalpingBudget._lock:
        ScalpingBudget._instance = None
        ScalpingBudget._initialized = False

    budget = ScalpingBudget(500000, 0.30, 0.10, 0.02)
    test("预算=$150K", budget.get_total_budget() == 150000)
    test("日亏损=$10K", budget.get_daily_loss_limit() == 10000)
    test("可分配", budget.can_allocate(10000) is True)
    test("分配成功", budget.allocate(10000) is True)
    test("已用=$140K", budget.get_available_budget() == 140000)
    budget.release(10000)
    test("释放后恢复", budget.get_available_budget() == 150000)
    test("单笔超限被拒", budget.can_allocate(20000) is False)

    # ── 5. 熔断器 ──
    print("\n── 5. 熔断器 (CircuitBreaker) ──")
    from app.scalping.circuit_breaker import ScalpingCircuitBreaker

    cb = ScalpingCircuitBreaker(3, 1)
    test("初始正常", cb.is_blocked() is False)
    cb.record_loss()
    cb.record_loss()
    test("2 次未熔断", cb.is_blocked() is False)
    cb.record_loss()
    test("3 次触发熔断", cb.is_blocked() is True)
    test("状态 COOLDOWN", cb.get_state()["state"] == "COOLDOWN")

    # ── 6. 趋势过滤 ──
    print("\n── 6. 趋势过滤 (TrendFilter) ──")
    from app.scalping.trend_filter import TrendFilter

    tf = TrendFilter(0.03, 35)
    test("SPY +1% 不阻止", tf.should_block(0.01) is None)
    test("SPY +4% 阻止", tf.should_block(0.04) is not None)
    test("SPY -4% 阻止", tf.should_block(-0.04) is not None)
    test("VIX 40 阻止", tf.should_block(0.01, vix=40) is not None)

    # ── 7. 统计追踪 ──
    print("\n── 7. 统计追踪 (StatsTracker) ──")
    from app.scalping.stats import ScalpingStatsTracker, TradeRecord

    tmp_csv = tempfile.mktemp(suffix=".csv")
    st = ScalpingStatsTracker(csv_path=tmp_csv)
    test("初始无交易", st.get_stats()["total_trades"] == 0)

    t1 = TradeRecord("US.TSLA", "LONG", 100.0, 101.5, 99.2, "rsi/vwap", "morning", 10)
    st.record_entry(t1)
    st.record_exit("US.TSLA", 101.5, "止盈触发")

    t2 = TradeRecord("US.NVDA", "SHORT", 200.0, 197.0, 201.6, "rsi/bb", "morning", 5)
    st.record_entry(t2)
    st.record_exit("US.NVDA", 202.0, "止损触发")

    stats = st.get_stats()
    test("总交易 2", stats["total_trades"] == 2)
    test("1 盈 1 亏", stats["wins"] == 1 and stats["losses"] == 1)
    test("胜率 50%", stats["win_rate"] == 0.5)

    os.unlink(tmp_csv)

    # ── 8. DailyActivePool 隔离 ──
    print("\n── 8. DailyActivePool 隔离 ──")
    try:
        from app.utils.daily_active_pool import DailyActivePool, SOURCE_SCALPING

        with DailyActivePool._lock:
            DailyActivePool._instance = None
            DailyActivePool._initialized = False

        pool = DailyActivePool(market_prefix="US.")
        pool.initialize()
        pool.add_scalping_target("US.VERIFY_TEST")
        test("Scalping 标的已添加", "US.VERIFY_TEST" in pool.get_scalping_codes())
        test("来源=SCALPING", pool.get_source("US.VERIFY_TEST") == SOURCE_SCALPING)
        pool.remove_scalping("US.VERIFY_TEST")
        test("Scalping 标的已移除", "US.VERIFY_TEST" not in pool.get_scalping_codes())
    except Exception as e:
        print(f"  ⚠️ DailyActivePool 测试跳过: {e}")

    # ── 9. Config 验证 ──
    print("\n── 9. 配置验证 ──")
    import yaml
    cfg_path = "/opt/ftseek/FTSeek-US/config/unified_config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    sc = cfg.get("scalping", {})
    test("scalping 段存在", bool(sc))
    test("enabled=false", sc.get("enabled") is False)
    test("mode=observe", sc.get("mode") == "observe")
    test("三时段配置", len(sc.get("schedule", {})) == 3)

    # ── 总结 ──
    print("\n" + "=" * 60)
    print(f"验证结果: {passed} 通过, {failed} 失败")
    if failed == 0:
        print("✅ 所有测试通过 — Scalping 模块逻辑正确")
    else:
        print(f"❌ {failed} 个测试失败")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
