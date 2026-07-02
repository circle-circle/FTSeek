# Strategy Registry 设计方案

## 目标
策略增删改只改 `unified_config.yaml` 一处。IC计算/过滤/归因/方向门禁全部从config读取，零硬编码strategy_id字符串。

## 原则
- 做减法：不新建模块，只改现有代码让它们从config读
- 向后兼容：config没写的策略，用当前默认值
- 渐进式：一次消灭一个硬编码点

## 设计

### 1. unified_config.yaml 新增 `strategy_registry` 段

```yaml
strategy_registry:
  # 每个策略的自描述定义
  # strategy_id = 信号快照中记录的strategy_id字段值
  # config_name = strategies段下的配置键名
  TECHNICAL_MULTI_FACTOR:
    config_name: technical          # 对应 strategies.technical
    enabled: true                   # 是否启用（也从strategies.technical.enabled读取，这里冗余备份）
    direction_filter:
      BUY_min_strength: 50          # BUY方向最低strength（当前硬编码60）
      BUY_max_std: 15               # BUY方向最大std（当前硬编码12）
      SHORT_min_confidence: 65      # SHORT方向最低confidence
    ic_gate:
      hard_block_threshold: -0.15   # IC低于此值硬拦截
      warning_threshold: 0.05       # IC低于此值warning
      min_signals: 30               # 最少样本数
    regime_filter: true             # 是否受regime门控
  
  technical_intraday:
    config_name: technical          # 共用technical配置
    enabled: true
    direction_filter: {}            # 用默认值
    ic_gate:
      hard_block_threshold: -0.15
      warning_threshold: 0.05
      min_signals: 30
    regime_filter: false
  
  BATTLE_PLAN_FAST_PATH:
    config_name: fast_lane
    enabled: true
    direction_filter: {}
    ic_gate:
      hard_block_threshold: -0.15
      warning_threshold: 0.05
      min_signals: 10               # 小样本策略降低门槛
    regime_filter: true
  
  voting_llm:
    config_name: voting_llm
    enabled: false                  # 已禁用
    direction_filter: {}
    ic_gate:
      hard_block_threshold: -0.15
      warning_threshold: 0.05
      min_signals: 30
    regime_filter: false
  
  EARNINGS_REVISION_MOMENTUM:
    config_name: earnings_revision
    enabled: false                  # 已禁用
    direction_filter: {}
    ic_gate:
      hard_block_threshold: -0.15
      warning_threshold: 0.05
      min_signals: 30
    regime_filter: false
  
  trend_first:
    config_name: trend_first
    enabled: false                  # 已禁用
    direction_filter: {}
    ic_gate:
      hard_block_threshold: -0.15
      warning_threshold: 0.05
      min_signals: 30
    regime_filter: false
```

### 2. 新增 `app/core/strategy_registry.py`（轻量读取器，不是新模块）

```python
"""Strategy Registry — 从unified_config.yaml读取策略定义。
所有需要查strategy_id属性的代码都通过这里，不硬编码。"""

_registry_cache = None
_registry_cache_ts = 0.0

def get_strategy_config(strategy_id: str) -> dict:
    """获取策略配置。未注册的策略返回默认值。"""
    _load()
    entry = _registry_cache.get(strategy_id, {})
    return {
        "enabled": entry.get("enabled", True),
        "config_name": entry.get("config_name", strategy_id),
        "direction_filter": entry.get("direction_filter", {}),
        "ic_gate": entry.get("ic_gate", {
            "hard_block_threshold": -0.15,
            "warning_threshold": 0.05,
            "min_signals": 30,
        }),
        "regime_filter": entry.get("regime_filter", False),
    }

def get_disabled_strategy_ids() -> list:
    """返回所有enabled=false的策略strategy_id列表。"""
    _load()
    return [sid for sid, entry in _registry_cache.items() 
            if not entry.get("enabled", True)]

def get_all_strategy_ids() -> list:
    """返回所有注册的strategy_id列表。"""
    _load()
    return list(_registry_cache.keys())

def _load():
    """从config加载（5分钟缓存）。"""
    import time, yaml
    global _registry_cache, _registry_cache_ts
    if _registry_cache and (time.time() - _registry_cache_ts) < 300:
        return
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        _registry_cache = cfg.get_dotted("strategy_registry", {}) or {}
        _registry_cache_ts = time.time()
    except Exception:
        _registry_cache = {}
```

### 3. 改造点（逐个消灭硬编码）

#### 3.1 signal_ic_tracker.py — 替换 _get_disabled_strategy_ids()
```python
# 旧: 硬编码 cfg_to_sid mapping
# 新:
from app.core.strategy_registry import get_disabled_strategy_ids
# 直接调用，不再维护mapping
```

#### 3.2 pipeline.py:485 — BUY strength门槛从config读
```python
# 旧: if _final_strength < 60:
# 新:
from app.core.strategy_registry import get_strategy_config
_strat_cfg = get_strategy_config(vote_result.get("strategy_id", ""))
_buy_min_str = _strat_cfg["direction_filter"].get("BUY_min_strength", 50)
if direction == "BUY" and _final_strength < _buy_min_str:
    # SKIP
```

#### 3.3 pipeline.py:495 — BUY std门槛从config读
```python
# 旧: if _vote_std > 12:
# 新:
_buy_max_std = _strat_cfg["direction_filter"].get("BUY_max_std", 15)
if direction == "BUY" and _vote_std > _buy_max_std:
    # SKIP
```

#### 3.4 order_executor.py:3228 — IC gate门槛从config读
```python
# 旧: _IC_HARD_BLOCK = -0.15
# 新:
from app.core.strategy_registry import get_strategy_config
_strat_cfg = get_strategy_config(_sig_strategy)
_ic_hard_block = _strat_cfg["ic_gate"]["hard_block_threshold"]
_ic_min_signals = _strat_cfg["ic_gate"]["min_signals"]
```

#### 3.5 signal_stage.py:130-131 — 不再硬编码strategy_id常量
```python
# 旧: _TECHNICAL_STRATEGY_ID = "TECHNICAL_MULTI_FACTOR"
# 新: 从config读，或者从策略函数的返回值获取
# signal_stage调用 eval_strategy("technical", ctx) 返回的SignalCandidate已有strategy_id字段
```

### 4. IC计算的自动适配

关键：IC tracker的 `get_ic_summary()` 已经从DB查 `GROUP BY strategy_id`。
只要信号快照的strategy_id正确，IC自动按策略分组。

**当前已经工作的部分**：
- 信号生成时 `SignalCandidate(strategy_id="TECHNICAL_MULTI_FACTOR")` → 写入signal_snapshots
- IC tracker `SELECT strategy_id, ... FROM signal_snapshots GROUP BY strategy_id`
- IC gate读 `by_strategy[strategy_id]`

**缺的部分**：
- disabled策略过滤 → 用registry自动发现（3.1已修）
- IC gate门槛 → 按策略从registry读（3.4待改）
- 方向过滤门槛 → 按策略从registry读（3.2/3.3待改）

### 5. 演进到方案B（Signal Envelope）的路径

方案A完成后，每个策略的属性都在config里了。
方案B只需：SignalCandidate增加一个 `strategy_config` 字段，从registry注入。
下游不再查config，直接读信号自带的config。

但这不是必须的——方案A已经消灭了所有硬编码，方案B只是减少运行时config查询。
