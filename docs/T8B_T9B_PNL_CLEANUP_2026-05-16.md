# T8b / T9b / PnLOutlier 清理 交付

> **日期**: 2026-05-16
> **背景**: 用户要求"思考合理性再执行" — 三项原始建议经验证后改方案
> **回归**: 135/135 全绿

---

## 1. 合理性分析 (做之前)

### T8 原始: RSI 反向 vs 动量正向 合并为 reversal+trend 两个信号源
**验证 5/15 数据**:
```
US.ARM   SHORT  MAIN  +  SHORT SCALPING (×2)  ← 全部同方向
US.BA    LONG   MAIN  +  LONG  SCALPING       ← 全部同方向
US.SNDK  SHORT  MAIN  +  SHORT SCALPING (×3)  ← 全部同方向
... 26 笔交易, 0 笔反向冲突
```
**结论**: 原假设"RSI 反向 vs 动量正向打架"在数据上**完全不成立**. 实际问题是同方向重复持仓 (T4 已修复).

**改方案 T8b**: regime-aware adapter priority — 让 SignalArbitrator 按市场状态智能分流, 而非强行合并信号定义.

### T9 原始: LLM Plan prompt 显式因子, 避免与主线 F8/F10/F11/F12 重复加权
**验证**: `BATTLE_PLAN_FAST_PATH` 是唯一 strategy_id, LLM Plan 走 FastLane 路径, 不进 SignalScorer.
**结论**: 当前架构下 LLM Plan 与主线 F8/F10/F11/F12 **不存在重复加权**. 原假设前提不成立.

**改方案 T9b**: rationale 结构化字段 `factors_used: [...]` 落入 `trade_attribution.signal_tags`, 用于复盘归因 (不动权重, 强化可追溯).

### PnLOutlier 历史污染清理
**验证**: 28 行污染数据来源不是历史遗留, 而是 `tests/coordinator/test_y_data_driven_fixes.py:97` **每次 pytest 都写真实 DB** + 发飞书告警.
```
2026-05-16 06:25  GOOGL=-6000 rolling_n=100  ← 早上还在产生
2026-05-16 06:01  ORCL=17000 rolling_n=100
```
**结论**: 不修测试只清数据是治标不治本.

**改方案**: 修测试 (monkeypatch `_persist` + `send_critical`) + 清污染 (28 行).

---

## 2. T8b: Regime-aware adapter priority

### 设计
| Regime | ScalpingAdapter | MainPipelineAdapter | 理由 |
|--------|:---:|:---:|------|
| **RANGING / HIGH_VOL_CHOP** | priority+5 (10→15) | priority-2 (5→3) | 震荡市 mean-reversion 占优 |
| **TRENDING_UP / TRENDING_DOWN** | priority-3 (10→7) | priority+5 (5→10) | 趋势市动量延续占优 |
| **EXTREME** | priority-5 (10→5) | priority+5 (5→10) | 极端市场 main 多层风控更稳 |
| **UNKNOWN / TRANSITION** | priority 不变 | priority 不变 | 无判据时保持静态 |

### 落地点
- `app/strategies/base_adapter.py:60` — `effective_priority(ctx)` 接口
- `app/strategies/adapters/scalping.py:25` — Scalping 实现
- `app/strategies/adapters/main_pipeline.py:25` — Main 实现
- `app/coordinator/signal_arbitrator.py:64` — `arbitrate(symbol, candidates, ctx=)` 调 `strategy.effective_priority(ctx)`
- `app/pipeline/order_executor.py:1866` + `app/scalping/engine.py:2384` — 调用方传 `ctx=context/signal_context`

### 关键代码
```python
# scalping.py
def effective_priority(self, ctx) -> int:
    regime = getattr(ctx, "regime", "") or ctx.get("regime", "")
    if regime in ("RANGING", "HIGH_VOL_CHOP"):
        return self.priority + 5
    if regime in ("TRENDING_UP", "TRENDING_DOWN"):
        return self.priority - 3
    if regime == "EXTREME":
        return self.priority - 5
    return self.priority

# signal_arbitrator.py
priority = int(strat.effective_priority(ctx)) if hasattr(strat, "effective_priority") else int(strat.priority)
score = priority * confidence  # 仲裁评分
```

### 效果预期
**5/15 ARM SHORT 场景**:
- regime = "HIGH_VOL_CHOP" (5/15 多数时段)
- scalping priority 10→**15**, confidence 0.7 → score = **10.5**
- main priority 5→**3**, confidence 0.65 → score = **1.95**
- → scalping wins, 主线让位 (避免同向重复持仓)

**反例 5/14 趋势日**:
- regime = "TRENDING_UP"
- scalping 10→**7** × 0.7 = 4.9
- main 5→**10** × 0.65 = 6.5
- → main wins, scalping 让位

---

## 3. T9b: LLM Plan factors_used 结构化

### Prompt 增强 (`premarket_planner.py:124`)
```json
"targets": [{
  "symbol": "US.NVDA",
  ...
  "factors_used": ["news_sentiment", "regime_fit", "trend"]  // 新增 T9b
}]
```

可选标签清单 (复盘 SQL 友好):
- `trend` / `momentum` / `volume` / `orderflow`
- `news_sentiment` / `regime_fit` / `risk_reward`
- `market_align` / `history_performance`
- `earnings_catalyst` / `breakout` / `mean_reversion`

### 透传链 (3 步)
1. **LLM Plan 生成** → battle_plan.json 含 `factors_used: [...]`
2. **FastLane 创建 SignalScore** (`signal_pipeline.py:870`):
   ```python
   _factors_used = battle_plan_match.get("factors_used", [])
   _signal_tags = "/".join(_factors_used) if _factors_used else "llm_plan"
   signal.metadata["factors_used"] = _factors_used
   signal.metadata["signal_tags"] = _signal_tags
   ```
3. **`_insert_trade_attribution` 写库** (`order_executor.py:1573`):
   ```python
   _sig_tags = signal.metadata.get("signal_tags") or signal.strategy_id
   # → trade_attribution.signal_tags = "news_sentiment/regime_fit/trend"
   ```

### 复盘 SQL (理想用法)
```sql
-- 按 LLM 宣称的因子分解 PnL, 看哪个因子标签真有 alpha
SELECT signal_tags, COUNT(*) n, AVG(realized_pnl) avg_pnl, SUM(realized_pnl) total
FROM trade_attribution
WHERE owner_strategy IS NOT NULL
  AND trade_date >= date('now', '-7 days')
GROUP BY signal_tags
ORDER BY total DESC;
```
预期输出 (7 天后):
```
signal_tags                            n   avg_pnl   total
news_sentiment/earnings_catalyst       8   +$87     +$696   ← LLM 财报判断有效
trend/momentum                        12   +$45     +$540   ← 主线趋势捕捉
mean_reversion/orderflow              15   -$23     -$345   ← 反转策略亏损, 待调整
```

### 不动权重
**关键**: T9b 不修改 SignalScorer 权重逻辑. `factors_used` 仅作为元数据落库, 用于 PostTrade 归因 — 完全无副作用.

---

## 4. PnLOutlier 修复 + 清理

### 测试修复 (`tests/coordinator/test_y_data_driven_fixes.py:97`)
```python
def test_y5_outlier_monitor_extreme_alerts(monkeypatch):  # 增加 monkeypatch
    m = PnLOutlierMonitor(sigma_threshold=5.0)
    m._mean = 0.0; m._std = 100.0; m._n = 100; m._last_refresh = 9e10
    # 阻止副作用: 不写 DB, 不发告警
    monkeypatch.setattr(m, "_persist", lambda *a, **kw: None)
    monkeypatch.setattr(_ca, "send_critical", lambda *a, **kw: None)
    assert m.check("ORCL", 17000) is True
    assert m.check("GOOGL", -6000) is True
```

### DB 清理 (单次)
```sql
DELETE FROM pnl_outliers
WHERE rolling_n=100 AND rolling_mean=0 AND rolling_std=100;
-- 28 行删除
```

### 验证
```
pytest tests/coordinator/test_y_data_driven_fixes.py → 11/11 passed
pytest 后 DB 行数: 0  ← 测试不再污染
全量回归: 135/135 passed
```

---

## 5. 风险与回滚

### T8b
- **风险**: regime_classifier 失效 (返回 UNKNOWN) → priority 退化为静态, 无负面影响.
- **回滚**: 在 `effective_priority` 起始加 `return self.priority` 即可.

### T9b
- **风险**: LLM 不输出 `factors_used` 字段 → fallback `"llm_plan"` 默认值, 无报错.
- **回滚**: 删 prompt 中 T9b 段落 + 删 signal.metadata 注入, 完全可逆.

### PnLOutlier
- **风险**: DELETE 不可逆, 但已二次验证 (rolling_n=100 AND mean=0 AND std=100 是测试特征值, 真实数据 mean=$59 std=$1278).
- **回滚**: 无 (28 行明显伪数据, 无业务价值).

---

## 6. 整套 T1-T9b 完整状态

| ID | 状态 | 影响层 |
|----|:---:|:---:|
| T1 scalping trace_id | ✅ | 可追溯 |
| T2 _insert_trade_attribution 写齐 | ✅ | 可追溯 |
| T3 scalping 写 trade_attribution | ✅ | 可追溯 |
| T4 sandbox SymbolLock enforce | ✅ | 风险敞口 |
| T5 双层评分去重 | ✅ | 信号质量 |
| T6 周期 reconcile | ✅ | 风险敞口 |
| T7 因子 → PnL 归因模型 | ⏸ | 等 30+ 笔数据 |
| **T8b** regime-aware priority | ✅ | 信号分流 |
| **T9b** LLM Plan factors_used | ✅ | 归因可追溯 |
| **PnLOutlier 修测试 + 清污染** | ✅ | 数据健康 |

---

**结论**: T8 / T9 原始建议假设不成立 (5/15 数据 + 当前架构反证), 改做 T8b/T9b 更对路. PnLOutlier 找到真凶 (测试污染), 治标治本一起做.

今晚开盘 → SQL 验证 `signal_tags` 是否含 LLM Plan 的因子标签 + `signal_arbitration` 是否含不同 regime 下 winner 变化.
