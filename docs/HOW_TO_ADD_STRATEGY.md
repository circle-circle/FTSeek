# 如何添加新策略 (2026-05-16)

> **5 步走** + 1 个文件 + 1 行 yaml = 新策略上线
> 基于 `app/strategies/` Strategy Contract + Registry 架构

---

## 1. 架构基础 (10 秒理解)

```
策略 = 输入(MarketContext) → 输出(SignalCandidate | None) + metadata + lifecycle hooks
```

3 个核心契约 (`app/strategies/contract.py`):
- **`MarketContext`** — 主调度器传给策略的全部输入 (price/rsi/regime/...)
- **`SignalCandidate`** — 策略产出的统一格式 (direction/strength/confidence/factor_detail/...)
- **`StrategyKind`** — TICK (被动评估) vs AUTONOMOUS (自有 loop, 如 scalping)

`Strategy ABC` (`app/strategies/base_strategy.py`) 定义生命周期:
```python
class Strategy(ABC):
    meta: StrategyMeta

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> Optional[SignalCandidate]: ...

    # 可选 override:
    def effective_priority(self, ctx) -> int: ...      # T8b regime-aware
    def get_exit_policy(self) -> ExitPolicy: ...        # 策略专属退出
    def on_position_opened(self, outcome): ...          # 入场后回调
    def on_position_closed(self, outcome): ...          # 平仓后回调
```

---

## 2. 5 步添加新策略 (以"突破策略 breakout"为例)

### Step 1: 创建文件 `app/strategies/adapters/breakout.py`

```python
"""BreakoutStrategy — 20日新高/新低突破"""
from typing import Optional
from app.strategies.base_strategy import Strategy, StrategyMeta
from app.strategies.contract import (
    MarketContext, SignalCandidate, StrategyKind, Direction,
)


class BreakoutStrategy(Strategy):
    def __init__(self):
        from app.core.config_container import get_config
        cfg = get_config()
        self.meta = StrategyMeta(
            name="breakout",                                  # 唯一名
            priority=int(cfg.get_dotted("strategies.breakout.priority", 6)),
            capital_bucket="main",                            # 共享 main 桶 (不双计)
            kind=StrategyKind.TICK,
            capital_pct=float(cfg.get_dotted("strategies.breakout.capital_pct", 0.10)),
            enabled=bool(cfg.get_dotted("strategies.breakout.enabled", False)),  # 默认 false
            description="20日新高/新低突破 + 成交量确认",
            min_strength=60,
            min_confidence=0.65,
        )
        super().__init__()

    def evaluate(self, ctx: MarketContext) -> Optional[SignalCandidate]:
        """20日新高 → LONG / 新低 → SHORT, 需要成交量 > 1.5x 均量"""
        # 从 ctx.metadata 拿 20d_high/low (主调度器需在 ctx 中提供)
        high_20d = ctx.metadata.get("high_20d")
        low_20d = ctx.metadata.get("low_20d")
        vol_ratio = ctx.metadata.get("volume_ratio_20d", 1.0)

        if high_20d is None or low_20d is None or ctx.price <= 0:
            return None
        if vol_ratio < 1.5:  # 量能不足, 不触发
            return None

        if ctx.price > high_20d:
            return SignalCandidate(
                strategy_name="breakout",
                symbol=ctx.symbol,
                direction=Direction.LONG,
                strength=70,
                confidence=0.7,
                position_pct=0.05,
                stop_loss_pct=0.025,
                rationale=f"突破 20d 高 ${high_20d:.2f}, 量比 {vol_ratio:.1f}x",
                signal_tags=["breakout", "volume_confirm"],
                metadata={"high_20d": high_20d, "vol_ratio": vol_ratio},
            )
        if ctx.price < low_20d:
            return SignalCandidate(
                strategy_name="breakout",
                symbol=ctx.symbol,
                direction=Direction.SHORT,
                strength=70, confidence=0.7,
                position_pct=0.05, stop_loss_pct=0.025,
                rationale=f"跌破 20d 低 ${low_20d:.2f}, 量比 {vol_ratio:.1f}x",
                signal_tags=["breakdown", "volume_confirm"],
                metadata={"low_20d": low_20d, "vol_ratio": vol_ratio},
            )
        return None

    def effective_priority(self, ctx=None):
        """突破策略 — 高波动期 +5, 震荡期 -3"""
        if ctx is None or not ctx.regime:
            return self.priority
        if ctx.regime in ("TRENDING_UP", "TRENDING_DOWN"):
            return self.priority + 5
        if ctx.regime == "RANGING":
            return self.priority - 3
        return self.priority

    def get_exit_policy(self):
        # 复用 MainPipelineExitPolicy (默认), 或写新 BreakoutExitPolicy
        return None
```

### Step 2: 注册 (`app/strategies/adapters/__init__.py`)

```python
from .breakout import BreakoutStrategy  # 加这一行

def all_adapters():
    return [
        MainPipelineStrategy(),
        ScalpingStrategy(),
        FastLaneStrategy(),
        LLMPlanStrategy(),
        BreakoutStrategy(),    # 加这一行
    ]
```

### Step 3: yaml 配置 (`config/unified_config.yaml`)

```yaml
strategies:
  breakout:                # 加这一段
    enabled: false         # 默认关 — 灰度
    priority: 6
    capital_pct: 0.10
```

### Step 4: 启用

```bash
# 编辑 yaml: strategies.breakout.enabled = true
# 或运行时 (无重启):
python3 -c "
from app.strategies.registry import get_registry
get_registry().enable('breakout')
"
# 或一键关:
get_registry().disable('breakout')
```

### Step 5: 测试

```python
# tests/coordinator/test_breakout_strategy.py
def test_breakout_long_signal():
    from app.strategies.adapters.breakout import BreakoutStrategy
    from app.strategies.contract import MarketContext

    s = BreakoutStrategy()
    ctx = MarketContext(
        symbol="US.NVDA", price=150.0,
        metadata={"high_20d": 148.0, "low_20d": 130.0, "volume_ratio_20d": 2.0}
    )
    cand = s.evaluate(ctx)
    assert cand is not None
    assert cand.direction.value == "LONG"
    assert "breakout" in cand.signal_tags
```

---

## 3. 设计原则 (新策略必须遵守)

### ✅ DO
- **纯函数 evaluate**: 不改 ctx, 不写 DB, 不调网络. 只看输入返回 candidate.
- **失败返回 None**: 不要 raise (Registry 会捕获, 但 None 更明确)
- **metadata 透传业务数据**: 例如 `_factor_detail`, `battle_plan_match` (供下游 attribution)
- **声明 capital_bucket**: 共享桶 (例: fast_lane→main) 避免风控双计
- **默认 enabled=False 上线**: 灰度发布, yaml 启用前不参与决策

### ❌ DON'T
- 不在 evaluate 中调 LLM (会阻塞主线 evaluate_all)
- 不依赖 scheduler 内部状态 (只看 ctx)
- 不写全局变量 (策略实例隔离)
- 不修改 ctx (主调度器复用)
- 不复用别人的 capital_bucket 名 (除非显式共享)

---

## 4. 如何关闭/切换策略

### 一键关 (运行时)
```python
from app.strategies.registry import get_registry
reg = get_registry()
reg.disable("scalping")     # 立即生效 (set_enabled)
reg.disable("breakout")
```

### yaml 配置关 (重启生效)
```yaml
strategies:
  scalping:
    enabled: false
```

### 热重载 yaml (无重启)
```python
from app.core.config_container import get_config
from app.strategies.registry import get_registry
cfg = get_config().get_dotted("strategies", {})
changed = get_registry().apply_yaml_config(cfg)
print(f"已同步 {changed} 项")
```

### 灰度切换 (旧策略 → 新策略)
1. 新策略 `breakout_v2.py` enabled=false 注册
2. 跑 7 天验证 ShadowMirror 数据 (不实盘下单)
3. yaml `breakout_v1.enabled=false`, `breakout_v2.enabled=true`
4. 重启
5. 观察 1 周
6. 删除 v1 代码

---

## 5. 策略种类选择 (TICK vs AUTONOMOUS)

| 维度 | TICK | AUTONOMOUS |
|----|----|----|
| 调度 | 主调度器轮询调 evaluate | 策略自己有 loop |
| evaluate 返回 | SignalCandidate \| None | 永远 None |
| 适用场景 | 主线 / 突破 / 反转 / LLM 计划消费 | scalping (高频自循环) / 盘前一次性 (LLM Plan) |
| Registry 行为 | `evaluate_all()` 默认调它 | `evaluate_all()` 跳过 (只看 only_tick=True) |
| 启停效果 | 立即不参与评估 | 设 enabled=False 后, 策略自身 loop 应检测并停 |

---

## 6. lifecycle hooks (可选, 用于策略自管状态)

```python
class MyStrategy(Strategy):
    def on_position_opened(self, outcome: TradeOutcome):
        """仓位开成功 — 例: 更新自己的 cooldown / 信号 dedup 状态"""
        self._last_entry[outcome.symbol] = outcome.entry_price

    def on_position_closed(self, outcome: TradeOutcome):
        """仓位平仓 — 例: 更新自己的胜率统计 / 触发自适应参数调整"""
        if outcome.success:
            self._win_rate.update(...)
```

**触发位置** (待后续接入):
- `on_position_opened`: `_execute_long/short` 创仓后调
- `on_position_closed`: `_close_position` 平仓后调

---

## 7. 健康监控

```python
from app.strategies.registry import get_registry

# 全部策略快照 (供 daily_review / CLI)
snapshot = get_registry().get_health_snapshot()
# [
#   {"name": "main", "kind": "tick", "enabled": True, "priority": 5,
#    "health": {"error_count": 0, "n_candidates_today": 47, ...}},
#   ...
# ]
```

---

## 8. FAQ

**Q1: 新策略和现有 4 个策略冲突怎么办?**
A: 在 `signal_arbitrator.priority × confidence` 仲裁. 高 priority 高 confidence 赢. SymbolLock 保证同标的同时刻 1 owner.

**Q2: 新策略想接管现有持仓?**
A: 走 `ProtectionController.takeover(symbol)`. M1 模块已实现.

**Q3: 新策略需要新数据源?**
A: 在 ctx.metadata 提供, 由主调度器在 `_build_market_context` 中注入. 不要在 evaluate 中调 API.

**Q4: 新策略想覆盖默认 SL/TP?**
A: 在 SignalCandidate 中设 `stop_loss_pct` / `take_profit_pct`. 主流程 `_execute_long/short` 会用.

**Q5: 当前架构是否真正可插拔?**
A: **部分可插拔**. Strategy contract + Registry 已就绪 (本文档). 但现有 `signal_pipeline._process_symbol_impl` 主流程**尚未完全切到走 Registry** (因为重写风险大). 当前阶段: 新策略可注册到 Registry, 但消费需要在 `_process_symbol_impl` 中加 `candidates += get_registry().evaluate_all(ctx)`. 渐进迁移路径见 §9.

---

## 9. 渐进迁移路径 (现有主流程 → 完全走 Registry)

**阶段 1 (当前)**: Registry 注册 + 4 个 adapter 升级 + 主流程不动
- 收益: 添加新策略接口清晰, 启停可控
- 局限: 主流程仍硬编码主线 + fast_lane 逻辑

**阶段 2 (1 周后)**: signal_pipeline 加 Registry 路径并行
```python
# signal_pipeline._process_symbol_impl
candidates = []
# 现有路径
candidates.append(legacy_main_path(ctx))
# 新路径: Registry 收集所有 TICK 策略候选
candidates.extend(get_registry().evaluate_all(ctx))
winner = arbitrator.arbitrate(symbol, candidates, ctx=ctx)
```

**阶段 3 (1 月后)**: 删除 signal_pipeline 内联主线 + fast_lane 逻辑, 完全走 Registry
- 收益: 主流程 1400 行 → ~200 行
- 前提: 阶段 2 跑 1 月 0 偏差

---

**总结**: 添加新策略 = 1 个文件 (50 行) + 1 行 yaml. 关闭新策略 = 1 个 Python 调用 / yaml 改 false. Strategy ABC + Registry 提供契约保证不破坏其他策略.
