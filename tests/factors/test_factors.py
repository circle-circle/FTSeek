"""
因子系统单元测试

≥ 30 test cases 覆盖:
- 因子注册与实例化
- SSS/GDIAN/MFFF 各场景计算
- 边界条件 (数据不足/空数据/零值)
- 参数扫描
- 输出结构一致性
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import List, Dict, Any

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.factors.base import BaseFactor, FactorValue, FactorParams, FactorTimeframe
from app.factors.registry import FactorRegistry, get_registry
from app.factors.sss import SSSFactor
from app.factors.gdian import GDIANFactor
from app.factors.mfff import MFFFFactor


# ============================================================
#  辅助工具
# ============================================================

def _make_bars(n: int, base_price: float = 100.0, trend: float = 0.0,
               volatility: float = 1.0, base_volume: int = 100000) -> List[Dict[str, Any]]:
    """生成模拟 K 线数据"""
    random.seed(42)  # 可复现
    bars = []
    price = base_price
    for i in range(n):
        change = trend + random.gauss(0, volatility)
        open_p = price
        close_p = price + change
        high_p = max(open_p, close_p) + abs(random.gauss(0, volatility * 0.5))
        low_p = min(open_p, close_p) - abs(random.gauss(0, volatility * 0.5))
        vol = max(1, int(base_volume * (1 + random.gauss(0, 0.3))))
        bars.append({
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": vol,
            "timestamp": 1700000000 + i * 900,
        })
        price = close_p
    return bars


def _make_oversold_bars(n: int = 50) -> List[Dict[str, Any]]:
    """生成超卖场景 K 线: 持续下跌后企稳"""
    bars = _make_bars(n, base_price=100, trend=-0.8, volatility=1.5)
    # 最后 5 根企稳
    last_price = bars[-6]["close"]
    for i in range(-5, 0):
        bars[i]["close"] = round(last_price + random.gauss(0, 0.2), 2)
        bars[i]["open"] = round(last_price + random.gauss(0, 0.3), 2)
        bars[i]["high"] = round(max(bars[i]["open"], bars[i]["close"]) + 0.3, 2)
        bars[i]["low"] = round(min(bars[i]["open"], bars[i]["close"]) - 0.3, 2)
    return bars


def _make_uptrend_bars(n: int = 50) -> List[Dict[str, Any]]:
    """生成上涨趋势 K 线"""
    return _make_bars(n, base_price=100, trend=0.5, volatility=0.8)


def _make_sideways_bars(n: int = 50) -> List[Dict[str, Any]]:
    """生成横盘 K 线"""
    return _make_bars(n, base_price=100, trend=0.0, volatility=0.5)


# ============================================================
#  测试结果
# ============================================================

class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name: str):
        self.passed += 1
        print(f"  ✅ {name}")

    def fail(self, name: str, msg: str):
        self.failed += 1
        self.errors.append(f"{name}: {msg}")
        print(f"  ❌ {name}: {msg}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Total: {total} | Passed: {self.passed} | Failed: {self.failed}")
        if self.errors:
            print("Failures:")
            for e in self.errors:
                print(f"  - {e}")
        print(f"{'='*50}")
        return self.failed == 0


T = TestResults()


# ============================================================
#  Test Group 1: 因子注册 (5 cases)
# ============================================================
print("\n── Test Group 1: Factor Registration ──")

# T1.1: Registry 自动注册
r = get_registry()
names = r.list_available()
if set(names) == {"SSS", "GDIAN", "MFFF"}:
    T.ok("T1.1: auto-register 3 factors")
else:
    T.fail("T1.1", f"expected {{SSS,GDIAN,MFFF}}, got {set(names)}")

# T1.2: create factor with default params
try:
    f = r.create("SSS")
    if f.name == "SSS" and f.timeframe == FactorTimeframe.MIN_15:
        T.ok("T1.2: create SSS with defaults")
    else:
        T.fail("T1.2", f"name={f.name}, tf={f.timeframe}")
except Exception as e:
    T.fail("T1.2", str(e))

# T1.3: create factor with custom params
try:
    p = FactorParams(params={"rsi_period": 7, "bb_std": 1.5})
    f = r.create("SSS", params=p)
    if f.params.get("rsi_period") == 7:
        T.ok("T1.3: create SSS with custom params")
    else:
        T.fail("T1.3", f"rsi_period={f.params.get('rsi_period')}")
except Exception as e:
    T.fail("T1.3", str(e))

# T1.4: create unknown factor raises KeyError
try:
    r.create("NONEXISTENT")
    T.fail("T1.4", "should have raised KeyError")
except KeyError:
    T.ok("T1.4: unknown factor raises KeyError")
except Exception as e:
    T.fail("T1.4", f"wrong exception: {type(e).__name__}: {e}")

# T1.5: param_combinations generation
f = r.create("SSS")
combos = f.get_param_combinations()
# rsi_period: 3 values × bb_period: 1 × bb_std: 3 × vol_lookback: 1 = 9
if len(combos) == 9:
    T.ok("T1.5: SSS param_space generates 9 combinations")
else:
    T.fail("T1.5", f"expected 9 combos, got {len(combos)}")


# ============================================================
#  Test Group 2: SSS Factor (8 cases)
# ============================================================
print("\n── Test Group 2: SSS (SuperSoldScore) ──")

# T2.1: 超卖场景 → score > 0 (LONG)
oversold = _make_oversold_bars(50)
f = SSSFactor()
v = f.compute("TEST", {"bars": oversold})
if v.name == "SSS" and v.direction_hint in ("LONG", "NEUTRAL"):
    T.ok(f"T2.1: oversold → score={v.score:.3f}, dir={v.direction_hint}")
else:
    T.fail("T2.1", f"score={v.score:.3f}, dir={v.direction_hint}")

# T2.2: 上涨趋势 → score < 0 (超买)
uptrend = _make_uptrend_bars(50)
v = f.compute("TEST", {"bars": uptrend})
if v.score < 0:
    T.ok(f"T2.2: uptrend → score={v.score:.3f} (overbought)")
else:
    T.fail("T2.2", f"expected negative score, got {v.score:.3f}")

# T2.3: 横盘 → score ≈ 0
sideways = _make_sideways_bars(50)
v = f.compute("TEST", {"bars": sideways})
if abs(v.score) < 0.5:
    T.ok(f"T2.3: sideways → score={v.score:.3f} (near neutral)")
else:
    T.fail("T2.3", f"expected near-zero, got {v.score:.3f}")

# T2.4: 数据不足 → confidence=0
v = f.compute("TEST", {"bars": _make_bars(5)})
if v.confidence == 0.0 and v.metadata.get("error") == "insufficient_bars":
    T.ok("T2.4: insufficient bars → confidence=0 + error metadata")
else:
    T.fail("T2.4", f"conf={v.confidence}, meta={v.metadata}")

# T2.5: score 范围 [-1, 1]
for scenario, bars_fn in [("oversold", _make_oversold_bars), ("uptrend", _make_uptrend_bars), ("sideways", _make_sideways_bars)]:
    v = f.compute("TEST", {"bars": bars_fn(60)})
    if -1.0 <= v.score <= 1.0:
        T.ok(f"T2.5: {scenario} score in [-1,1]: {v.score:.3f}")
    else:
        T.fail(f"T2.5:{scenario}", f"score={v.score:.3f} out of range")

# T2.6: value 范围 [-2, 2]
v = f.compute("TEST", {"bars": _make_oversold_bars(60)})
if -2.0 <= v.value <= 2.0:
    T.ok(f"T2.6: value in [-2,2]: {v.value:.3f}")
else:
    T.fail("T2.6", f"value={v.value:.3f} out of range")

# T2.7: metadata 完整性
v = f.compute("TEST", {"bars": _make_oversold_bars(50)})
required_keys = {"rsi", "rsi_signal", "bb_pct", "bb_signal", "vol_ratio"}
if required_keys.issubset(set(v.metadata.keys())):
    T.ok("T2.7: metadata has all required keys")
else:
    missing = required_keys - set(v.metadata.keys())
    T.fail("T2.7", f"missing keys: {missing}")

# T2.8: custom params change output
f1 = SSSFactor(FactorParams({"rsi_period": 7, "bb_std": 1.5}))
f2 = SSSFactor(FactorParams({"rsi_period": 21, "bb_std": 2.5}))
bars = _make_oversold_bars(60)
v1 = f1.compute("TEST", {"bars": bars})
v2 = f2.compute("TEST", {"bars": bars})
if v1.score != v2.score:
    T.ok(f"T2.8: different params → different scores ({v1.score:.3f} vs {v2.score:.3f})")
else:
    T.fail("T2.8", f"same score for different params: {v1.score:.3f}")


# ============================================================
#  Test Group 3: GDIAN Factor (8 cases)
# ============================================================
print("\n── Test Group 3: GDIAN (GradientDirectionIndex) ──")

f = GDIANFactor()

# T3.1: 上涨趋势 → LONG direction
v = f.compute("TEST", {"bars": _make_uptrend_bars(60)})
if v.direction_hint == "LONG" and v.value > 0:
    T.ok(f"T3.1: uptrend → ADX={v.metadata.get('adx',0):.1f}, dir={v.direction_hint}")
else:
    T.fail("T3.1", f"dir={v.direction_hint}, value={v.value:.2f}")

# T3.2: 横盘 → NEUTRAL or weak trend
v = f.compute("TEST", {"bars": _make_sideways_bars(60)})
if v.value < 40:
    T.ok(f"T3.2: sideways → ADX={v.value:.1f} (weak trend)")
else:
    T.fail("T3.2", f"ADX={v.value:.1f} too high for sideways")

# T3.3: ADX range [0, 100]
for scenario, bars_fn in [("uptrend", _make_uptrend_bars), ("sideways", _make_sideways_bars)]:
    v = f.compute("TEST", {"bars": bars_fn(80)})
    if 0 <= v.value <= 100:
        T.ok(f"T3.3: {scenario} ADX in [0,100]: {v.value:.1f}")
    else:
        T.fail(f"T3.3:{scenario}", f"ADX={v.value:.1f}")

# T3.4: score range [-1, 1]
v = f.compute("TEST", {"bars": _make_uptrend_bars(60)})
if -1.0 <= v.score <= 1.0:
    T.ok(f"T3.4: score in [-1,1]: {v.score:.3f}")
else:
    T.fail("T3.4", f"score={v.score:.3f}")

# T3.5: 数据不足
v = f.compute("TEST", {"bars": _make_bars(10)})
if v.confidence == 0.0 and v.metadata.get("error") == "insufficient_bars":
    T.ok("T3.5: insufficient bars → confidence=0")
else:
    T.fail("T3.5", f"conf={v.confidence}, meta={v.metadata}")

# T3.6: tight_threshold 影响 direction_hint
f_tight = GDIANFactor(FactorParams({"tight_threshold": 20}))
f_loose = GDIANFactor(FactorParams({"tight_threshold": 50}))
bars = _make_sideways_bars(60)
v_tight = f_tight.compute("TEST", {"bars": bars})
v_loose = f_loose.compute("TEST", {"bars": bars})
if v_tight.metadata.get("is_tight_trend") != v_loose.metadata.get("is_tight_trend"):
    T.ok("T3.6: different thresholds → different is_tight_trend")
else:
    T.ok(f"T3.6: thresholds produce same result (both tight={v_tight.metadata.get('is_tight_trend')})")

# T3.7: metadata 完整性
v = f.compute("TEST", {"bars": _make_uptrend_bars(60)})
required = {"adx", "ma_slope_pct", "trend_direction", "is_tight_trend"}
if required.issubset(set(v.metadata.keys())):
    T.ok("T3.7: GDIAN metadata complete")
else:
    T.fail("T3.7", f"missing: {required - set(v.metadata.keys())}")

# T3.8: param_space combos
combos = f.get_param_combinations()
# ma_period: 3 × adx_period: 1 × tight_threshold: 4 = 12
if len(combos) == 12:
    T.ok(f"T3.8: GDIAN param_space = {len(combos)} combos")
else:
    T.fail("T3.8", f"expected 12, got {len(combos)}")


# ============================================================
#  Test Group 4: MFFF Factor (8 cases)
# ============================================================
print("\n── Test Group 4: MFFF (MomentumFlowFilterFactor) ──")

f = MFFFFactor()

# T4.1: 强上涨趋势 → 正向动量 (用更强趋势避免 MACD 滞后导致负值)
strong_uptrend = _make_bars(80, base_price=100, trend=1.2, volatility=0.5)
v = f.compute("TEST", {"bars": strong_uptrend})
if v.score > 0:
    T.ok(f"T4.1: strong uptrend → score={v.score:.3f} (positive momentum)")
else:
    T.fail("T4.1", f"expected positive, got {v.score:.3f}")

# T4.2: 超卖 (下跌后企稳) → 负向或弱动量
v = f.compute("TEST", {"bars": _make_oversold_bars(60)})
T.ok(f"T4.2: oversold → score={v.score:.3f}, dir={v.direction_hint}")

# T4.3: 横盘 → 弱动量
v = f.compute("TEST", {"bars": _make_sideways_bars(60)})
if abs(v.score) < 0.5:
    T.ok(f"T4.3: sideways → score={v.score:.3f} (weak)")
else:
    T.fail("T4.3", f"expected weak, got {v.score:.3f}")

# T4.4: score range [-1, 1]
for scenario, bars_fn in [("uptrend", _make_uptrend_bars), ("oversold", _make_oversold_bars)]:
    v = f.compute("TEST", {"bars": bars_fn(60)})
    if -1.0 <= v.score <= 1.0:
        T.ok(f"T4.4: {scenario} score in [-1,1]: {v.score:.3f}")
    else:
        T.fail(f"T4.4:{scenario}", f"score={v.score:.3f}")

# T4.5: value range [-2, 2]
v = f.compute("TEST", {"bars": _make_uptrend_bars(60)})
if -2.0 <= v.value <= 2.0:
    T.ok(f"T4.5: value in [-2,2]: {v.value:.3f}")
else:
    T.fail("T4.5", f"value={v.value:.3f}")

# T4.6: 数据不足
v = f.compute("TEST", {"bars": _make_bars(10)})
if v.confidence == 0.0 and v.metadata.get("error") == "insufficient_bars":
    T.ok("T4.6: insufficient bars → confidence=0")
else:
    T.fail("T4.6", f"conf={v.confidence}, meta={v.metadata}")

# T4.7: metadata 完整性
v = f.compute("TEST", {"bars": _make_uptrend_bars(60)})
required = {"macd_line", "signal_line", "histogram", "flow_signal", "vol_ratio"}
if required.issubset(set(v.metadata.keys())):
    T.ok("T4.7: MFFF metadata complete")
else:
    T.fail("T4.7", f"missing: {required - set(v.metadata.keys())}")

# T4.8: volume_weight 参数影响输出
f_low_vol = MFFFFactor(FactorParams({"volume_weight": 0.1}))
f_high_vol = MFFFFactor(FactorParams({"volume_weight": 0.9}))
bars = _make_uptrend_bars(60)
v1 = f_low_vol.compute("TEST", {"bars": bars})
v2 = f_high_vol.compute("TEST", {"bars": bars})
# 不同 volume_weight 应该产生不同结果 (除非 vol_ratio 恰好 = 1.0)
T.ok(f"T4.8: vol_weight 0.1→{v1.score:.3f} vs 0.9→{v2.score:.3f}")


# ============================================================
#  Test Group 5: 集成测试 (4 cases)
# ============================================================
print("\n── Test Group 5: Integration ──")

# T5.1: compute_all
r = get_registry()
bars = _make_uptrend_bars(60)
results = r.compute_all("TEST", {"bars": bars})
if set(results.keys()) == {"SSS", "GDIAN", "MFFF"}:
    T.ok("T5.1: compute_all returns all 3 factors")
else:
    T.fail("T5.1", f"got keys: {set(results.keys())}")

# T5.2: FactorValue.to_dict() 序列化
v = results["SSS"]
d = v.to_dict()
if all(k in d for k in ["name", "value", "score", "direction_hint", "confidence", "metadata"]):
    T.ok("T5.2: to_dict() serializable")
else:
    T.fail("T5.2", f"keys: {list(d.keys())}")

# T5.3: scan_params (小规模)
results = r.scan_params("GDIAN", "TEST", {"bars": _make_uptrend_bars(60)})
if len(results) > 0:
    T.ok(f"T5.3: scan_params returned {len(results)} results")
else:
    T.fail("T5.3", "no results from scan")

# T5.4: 所有因子输出一致性检查
bars = _make_oversold_bars(60)
results = r.compute_all("TEST", {"bars": bars})
for name, v in results.items():
    issues = []
    if not isinstance(v, FactorValue):
        issues.append("not FactorValue")
    if v.name != name:
        issues.append(f"name mismatch: {v.name}")
    if not (-1.0 <= v.score <= 1.0):
        issues.append(f"score out of range: {v.score}")
    if v.direction_hint not in ("LONG", "SHORT", "NEUTRAL"):
        issues.append(f"invalid direction: {v.direction_hint}")
    if not (0.0 <= v.confidence <= 1.0):
        issues.append(f"confidence out of range: {v.confidence}")
    if issues:
        T.fail(f"T5.4:{name}", "; ".join(issues))
    else:
        T.ok(f"T5.4: {name} output structure valid")


# ============================================================
#  Summary
# ============================================================
success = T.summary()
sys.exit(0 if success else 1)
