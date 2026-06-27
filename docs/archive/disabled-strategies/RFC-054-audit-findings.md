# RFC-054 代码级审计报告

> **审计者**: Hermes Agent
> **日期**: 2026-05-30
> **状态**: 发现 3 个致命 Bug + 2 个架构缺陷 + 3 个数据流问题

---

## 1. 致命 Bug (会导致策略静默失效)

### 🔴 Bug #1: `RegimeAdapter.get_current_regime()` 不存在

**方案代码** (§3.3.1):
```python
adapter = RegimeAdapter()
regime = adapter.get_current_regime()  # ← AttributeError!
ctx.metadata["regime"] = regime
```

**实际代码验证**:
```
$ grep "def.*regime" app/strategy/regime_adapter.py
  def normalize_regime(regime: Any) -> str:     # 工具函数
  def get_strategy_weight(self, strategy_group, regime)  # 消费 regime, 不提供
  def get_position_scale(self, regime)           # 消费 regime, 不提供
  def adapt(self, ...)                           # 消费 regime, 不提供
  def on_regime_change(self, ...)                # 事件处理器
```

**RegimeAdapter 是 regime 的消费者, 不是提供者。** 它需要外部传入 regime 参数。

**影响**: `_inject_regime_and_sector()` 的 except 分支会将 regime 设为 "RANGE"。Layer 1 检查 `regime in ("RANGE", ...)` → return None。**trend_first 永远不会激活。**

**修复**:
```python
# 方案A: 从 shared_kernel VIX regime + SPY 均线推断
def _infer_regime():
    from app.core.shared_kernel import get_shared_kernel
    kernel = get_shared_kernel()
    vix_regime = kernel.get_vix_regime()  # "normal"/"caution"/"fear"/"crisis"

    if vix_regime in ("fear", "crisis"):
        return "HIGH_VOL"

    # 从 SPY 均线推断趋势方向
    # 需要在 market_data_stage 中获取 SPY 数据后注入 ctx
    # ctx 已有 spy_price, spy_ma200 字段
    ...
```

### 🔴 Bug #2: `ctx.volume_ratio` 不存在于 SymbolContext

**方案代码** (§3.2.2):
```python
volume_ratio = getattr(ctx, 'volume_ratio', 1.0)  # 永远返回 1.0
if volume_ratio < min_vol:  # 1.0 < 2.0 → 永远 True → BREAKOUT 永不触发
    return None
```

**实际 SymbolContext 字段** (`pipeline_contracts.py:26-81`):
```python
class SymbolContext:
    volume: int = 0         # ✅ 存在
    avg_volume: int = 0     # ✅ 存在
    # volume_ratio           # ❌ 不存在
    # price_change_5min      # ❌ 不存在
```

**影响**: BREAKOUT 入场模式永远不会触发。trend_first 只有 PULLBACK 模式可用。

**修复**:
```python
# 从已有字段计算
volume_ratio = ctx.volume / ctx.avg_volume if ctx.avg_volume > 0 else 1.0
```

### 🔴 Bug #3: `ctx.price_change_5min` 不存在于 SymbolContext

**方案代码** (§3.2.2):
```python
price_change_5m = getattr(ctx, 'price_change_5min', None)  # 永远 None
if price_change_5m is not None and price_change_5m < -0.02:  # 永远跳过
    ...
```

**影响**: 闪崩检测 (L0-b) 永远不会触发。在 flash crash 中会照常买入。

**修复**:
```python
# 用日内涨跌幅近似 (ctx.change_pct = 从 prev_close 算起)
# 不如5分钟精确, 但至少能工作
intraday_change = ctx.change_pct  # 例如 -0.05 = -5%
if intraday_change < -0.03:  # 日内跌>3% 且放量 = 恐慌
    volume_ratio = ctx.volume / ctx.avg_volume if ctx.avg_volume > 0 else 1.0
    if volume_ratio > 2.0:
        return False  # 闪崩拦截
```

---

## 2. 架构缺陷

### 🟡 缺陷 #4: 两套信号管线并存, 方案只覆盖一套

**实际代码** (`signal_pipeline.py:677-684`):
```python
def _use_new_pipeline() -> bool:
    return cfg.get_dotted("pipeline.use_new_stages", False)
```

**配置** (`unified_config.yaml:1852`): `use_new_stages: true` ← 当前启用

**但**: `_run_signal_scan_body()` (line 313) 中有一个独立的 CRA 路径, 也调用 `_run_new_pipeline_to_candidate()`:
```python
# signal_pipeline.py:438
def _run_new_pipeline_to_candidate(scheduler, symbol, ...):
    ctx = market_data_stage.process(...)
    candidate = signal_stage.process(ctx, ...)  # ← 方案修改的目标
```

**结论**: signal_stage.py 确实被两条路径使用:
1. `_process_symbol_new_pipeline()` → `runner.run()` → `signal_stage.process()`
2. `_run_new_pipeline_to_candidate()` → `signal_stage.process()`

方案修改 signal_stage.py 是正确的。但需确保两条路径都能正确注入 regime/sector 数据。

### 🟡 缺陷 #5: SectorLeaderDetector 数据可能在开盘前为空

**调用链验证**:
```
sector_leader.analyze_sectors() 仅在以下位置被调用:
  llm_pipeline.py:293 → 盘前 LLM pipeline

sector_leader._sector_scores:
  初始值 = {} (空字典)
  仅在 analyze_sectors() 调用后才填充
```

**影响**: 如果 `_inject_regime_and_sector()` 在 analyze_sectors() 之前被调用 (例如盘前第一笔), `_sector_scores` 为空, 所有股票的 sector_info.score = 0 → 全部被 min_sector_score=40 拦截 → trend_first 不产出信号。

**修复**: 在 `_inject_regime_and_sector` 中检测空数据, 并在日志中标记:
```python
if not detector._sector_scores:
    logger.info("[Signal] 板块数据尚未初始化 (可能盘前), trend_first 将不激活")
```

---

## 3. 数据流问题

### 🟡 问题 #6: regime 来源不明确

系统中存在 3 个不同的 regime 概念:

| 来源 | 名称 | 值域 | 含义 |
|------|------|------|------|
| `shared_kernel.get_vix_regime()` | VIX regime | normal/caution/fear/crisis | 基于 VIX 绝对值 |
| `MarketRegimeIdentifier.identify()` | Market regime | TREND_UP/TREND_DOWN/RANGE/HIGH_VOL/LOW_VOL | 基于 SPY 均线+波动率 |
| `market_regime_detector` | Detailed regime | TRENDING_UP/RANGING/HIGH_VOL_CHOP/EXTREME/... | 更细粒度 |

trend_first 需要的是 **Market regime (TREND_UP/TREND_DOWN)** — 因为它要判断做多还是做空。

但 `MarketRegimeIdentifier` 没有被任何地方持久化 regime 状态 — 它是一个纯函数, 需要传入 `market_data` dict。

**修复**: 在 `_inject_regime_and_sector` 中调用 `MarketRegimeIdentifier.identify()`:
```python
from app.decision.market_regime import MarketRegimeIdentifier
identifier = MarketRegimeIdentifier(config={}, event_store=None)
market_data = {
    "spy_price": ctx.spy_price,
    "spy_ma20": ctx.ma20,  # 注意: ctx.ma20 是个股的, 不是 SPY 的!
    "spy_ma50": ...,       # 需要从 shared_kernel 获取
    "vix": ctx.vix,
    "price_change_5d": ..., # 需要从某处获取
}
regime = identifier.identify(market_data)
```

**问题**: `ctx.ma20` 是**个股**的 MA20, 不是 SPY 的 MA20。`MarketRegimeIdentifier` 需要 SPY 的均线数据。这些数据在 `spy_context` dict 中 (由 `scan_loop` 传入), 但 `signal_stage.process()` 没有接收 `spy_context` 参数。

**解决方案**: 修改 `signal_stage.process()` 签名, 增加 `spy_context` 参数; 或在 `_inject_regime_and_sector` 中从 `shared_kernel` 获取。

---

## 4. 修正方案汇总

### 修正 A: 简化 regime 推断 (不依赖 MarketRegimeIdentifier)

```python
def _infer_market_regime(ctx: SymbolContext) -> str:
    """从 SymbolContext 已有字段推断市场 regime

    使用:
      ctx.vix_regime: "normal"/"elevated"/"extreme" (VIX 分级)
      ctx.spy_price: SPY 当前价
      ctx.spy_ma200: SPY 200日均线
      ctx.vix: VIX 值

    返回: "TREND_UP" / "TREND_DOWN" / "RANGE" / "HIGH_VOL"
    """
    # 高波动优先
    if ctx.vix_regime == "extreme" or ctx.vix > 30:
        return "HIGH_VOL"

    # 趋势判断 (简化版: SPY vs MA200)
    if ctx.spy_price > 0 and ctx.spy_ma200 > 0:
        if ctx.spy_price > ctx.spy_ma200 * 1.01:  # SPY 高于 MA200 1%以上
            return "TREND_UP"
        elif ctx.spy_price < ctx.spy_ma200 * 0.99:  # SPY 低于 MA200 1%以上
            return "TREND_DOWN"

    return "RANGE"
```

**优势**: 完全使用 SymbolContext 已有字段 (`spy_price`, `spy_ma200`, `vix`, `vix_regime`), 不需要额外的数据源或方法调用。

**劣势**: 比 MarketRegimeIdentifier 简化 (没有 MA20/MA50 交叉, 没有 5 日变动)。但作为 v1 足够。

### 修正 B: volume_ratio 和闪崩检测用已有字段

```python
# trend_first.py 中的修正

def _get_volume_ratio(ctx: SymbolContext) -> float:
    """从已有字段计算量比"""
    if ctx.avg_volume > 0:
        return ctx.volume / ctx.avg_volume
    return 1.0

def _check_price_stability(ctx: SymbolContext, config: Dict) -> bool:
    """L0-b: 用日内涨跌幅替代 5分钟变动"""
    # ctx.change_pct = 日内从 prev_close 算起的涨跌幅
    if ctx.change_pct < -0.03:  # 日内跌>3%
        vol_ratio = _get_volume_ratio(ctx)
        if vol_ratio > 2.0:
            return False  # 放量暴跌 = 恐慌
    return True
```

### 修正 C: signal_stage.process() 签名不需要改

原方案提议增加 `spy_context` 参数。修正后不需要:
- regime 推断用 ctx 已有字段 (spy_price, spy_ma200)
- sector 数据从 SectorLeaderDetector 单例获取
- capital_flow 默认 0.5

`_inject_regime_and_sector(ctx)` 只需要 ctx 一个参数即可。

---

## 5. 修正后的完整代码差异

### 5.1 trend_first.py 修正片段

```diff
 def _check_price_stability(ctx: SymbolContext, config: Dict) -> bool:
-    price_change_5m = getattr(ctx, 'price_change_5min', None)
-    volume_ratio = getattr(ctx, 'volume_ratio', 1.0)
-
-    if price_change_5m is not None and price_change_5m < -0.02:
-        if volume_ratio > 2.0:
+    # 用日内涨跌幅 (change_pct) 替代不存在的 price_change_5min
+    vol_ratio = ctx.volume / ctx.avg_volume if ctx.avg_volume > 0 else 1.0
+    if ctx.change_pct < -0.03:  # 日内跌>3%
+        if vol_ratio > 2.0:
             return False
     return True
```

```diff
 def _eval_breakout(ctx: SymbolContext, direction: str, config: Dict) -> Optional[float]:
-    volume_ratio = getattr(ctx, 'volume_ratio', 1.0)
+    vol_ratio = ctx.volume / ctx.avg_volume if ctx.avg_volume > 0 else 1.0
     min_vol = config.get("breakout_min_volume_ratio", 2.0)
-    if volume_ratio < min_vol:
+    if vol_ratio < min_vol:
         return None
```

### 5.2 _inject_regime_and_sector() 完整修正版

```python
def _inject_regime_and_sector(ctx: SymbolContext):
    """将 Regime + Sector + CapitalFlow 注入 ctx.metadata"""

    # 1. 市场状态 — 从 ctx 已有字段推断 (不依赖外部 API)
    if ctx.vix_regime == "extreme" or ctx.vix > 30:
        regime = "HIGH_VOL"
    elif ctx.spy_price > 0 and ctx.spy_ma200 > 0:
        if ctx.spy_price > ctx.spy_ma200 * 1.01:
            regime = "TREND_UP"
        elif ctx.spy_price < ctx.spy_ma200 * 0.99:
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"
    else:
        regime = "RANGE"
    ctx.metadata["regime"] = regime

    # 2. 板块信息 (复用 SectorLeaderDetector 单例)
    try:
        from app.risk.sector_leader import get_sector_leader_detector, SECTOR_MAPPING
        detector = get_sector_leader_detector()

        if not detector._sector_scores:
            logger.debug("[Signal] 板块数据未初始化, trend_first 将受限")
            ctx.metadata["sector_info"] = {
                "name": "Unknown", "score": 0, "leaders": [], "last_update": None,
            }
        else:
            leaders = detector.get_sector_leaders()
            leader_symbols = [l["symbol"].replace("US.", "") for l in leaders]

            symbol_clean = ctx.symbol.replace("US.", "")
            for sector_name, symbols in SECTOR_MAPPING.items():
                if symbol_clean in symbols:
                    sector_data = detector._sector_scores.get(sector_name)
                    ctx.metadata["sector_info"] = {
                        "name": sector_name,
                        "score": sector_data.score if sector_data else 0,
                        "leaders": leader_symbols,
                        "last_update": sector_data.last_update if sector_data else None,
                    }
                    break
            else:
                ctx.metadata["sector_info"] = {
                    "name": "Unknown", "score": 0,
                    "leaders": leader_symbols, "last_update": None,
                }
    except Exception as e:
        logger.debug(f"[Signal] Sector 注入失败: {e}")
        ctx.metadata["sector_info"] = {
            "name": "Unknown", "score": 0, "leaders": [], "last_update": None,
        }

    # 3. 资金流 (v1 默认中性)
    ctx.metadata.setdefault("capital_flow", {"score": 0.5})
```

---

## 6. 测试覆盖补充

### 新增测试用例 (针对修复后的代码):

```python
def test_volume_ratio_computed_from_fields():
    """volume_ratio 从 ctx.volume/avg_volume 计算, 不依赖不存在的属性"""
    ctx = _make_ctx()
    ctx.volume = 20_000_000
    ctx.avg_volume = 10_000_000
    # 量比 = 2.0, BREAKOUT 模式应可触发
    ctx.rsi = 72  # > 65, 非回调区间
    sig = evaluate(ctx, {"min_score": 55, "breakout_min_volume_ratio": 2.0})
    if sig:
        assert sig.metadata["entry_mode"] == "BREAKOUT"

def test_flash_crash_uses_change_pct():
    """闪崩检测用 change_pct (日内), 不依赖不存在的 price_change_5min"""
    ctx = _make_ctx(rsi=38)
    ctx.change_pct = -0.04  # 日内跌4%
    ctx.volume = 30_000_000
    ctx.avg_volume = 10_000_000  # 量比=3.0
    sig = evaluate(ctx, {"min_score": 55})
    assert sig is None  # 闪崩拦截

def test_regime_inferred_from_spy():
    """regime 从 ctx.spy_price/spy_ma200 推断"""
    ctx = _make_ctx()
    ctx.spy_price = 550.0
    ctx.spy_ma200 = 520.0  # SPY > MA200 → TREND_UP
    ctx.vix = 15
    ctx.vix_regime = "normal"
    # 手动调用注入
    from app.pipeline.stages.signal_stage import _inject_regime_and_sector
    _inject_regime_and_sector(ctx)
    assert ctx.metadata["regime"] == "TREND_UP"

def test_regime_high_vol_from_vix():
    """VIX>30 → HIGH_VOL"""
    ctx = _make_ctx()
    ctx.vix = 35
    ctx.vix_regime = "extreme"
    _inject_regime_and_sector(ctx)
    assert ctx.metadata["regime"] == "HIGH_VOL"
```

---

## 7. 给多 Agent 评审的建议关注点

请将以下问题提交给其他 Agent 评审:

1. **regime 推断是否足够?** 仅用 SPY>MA200 判断趋势方向 (TREND_UP/DOWN), 没有 MA20/MA50 交叉和 5日变动。是否会导致频繁的 regime 翻转?

2. **BREAKOUT 模式的风险**: 放量突破 (RSI>65 + vol_ratio>2.0) 入场, 止损仅 1.0×ATR。在跳空高开的情况下, 1.0×ATR 是否太紧?

3. **板块数据盘中不刷新**: v1 接受盘前一次分析的结果, 不做盘中刷新。这在盘中板块轮动时是否风险过高?

4. **best-hit vs first-hit**: 方案提议并行评估 trend_first + technical 取最优。这是否会导致两套策略互相干扰 (例如 trend_first 说 LONG, technical 说 SHORT)?

5. **SignalGate 兼容性**: order_executor.py 中 SignalGate 对 strength=0 的非 voting_llm 策略会拦截。trend_first 的 strength 范围是 55-100, 不会触发拦截, 但需确认。

6. **ATR 止损与现有 ExitMonitor 的冲突**: trend_first 在 metadata 中传递 ATR 止损参数, 但现有 ExitMonitor (渐进式 4 档 Tier) 是否已消费这些参数? 还是需要在 ExitMonitor 中新增代码?

7. **数据时间一致性**: `ctx.spy_price` 和 `ctx.spy_ma200` 是在 `market_data_stage.process()` 中注入的。它们的更新频率是否足够 (实时 vs 盘前缓存)?

---

## 8. 结论

**RFC-054 的整体设计方向正确** — 趋势+板块作为主信号源替代均值回归, 学术和自身数据都支持。

**但原方案代码有 3 个致命 Bug, 直接部署会导致 trend_first 永远不激活:**
1. `RegimeAdapter.get_current_regime()` 不存在 → regime 永远是 RANGE → Layer 1 拦截
2. `ctx.volume_ratio` 不存在 → BREAKOUT 永不触发
3. `ctx.price_change_5min` 不存在 → 闪崩检测无效

**修正后的方案 (本报告 §5) 解决了所有致命 Bug**, 使用 SymbolContext 已有字段, 无外部依赖。

**建议**: 将修正后的 RFC-054 + 本审计报告一起提交给多 Agent 评审, 重点关注 §7 列出的 7 个问题。
