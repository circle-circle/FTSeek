# 策略模块 - 多策略支持

**P1 模块化重构**: 2026-04-06

---

## 目录结构

```
app/decision/strategies/
├── __init__.py           # 模块导出
├── base.py               # 策略基类 + 注册机制
├── price_break.py        # 价格突破策略
├── ma_cross.py           # 均线交叉策略
├── rsi_rebound.py        # RSI 反弹策略
└── README.md             # 本文档
```

---

## 策略基类接口

所有策略必须继承 `BaseStrategy` 并实现以下方法：

```python
from app.decision.strategies import BaseStrategy, StrategyResult

class MyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "my_strategy"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "我的策略描述"

    @property
    def required_data(self) -> List[str]:
        return ["price", "volume"]

    def generate_signal(self, symbol: str, market_data: Dict[str, Any]) -> StrategyResult:
        # 实现策略逻辑
        pass
```

---

## 策略注册

使用装饰器注册策略：

```python
from app.decision.strategies import register_strategy, BaseStrategy

@register_strategy
class MyStrategy(BaseStrategy):
    ...
```

---

## 使用策略

```python
from app.decision.strategies import get_strategy, list_strategies

# 获取策略实例
strategy = get_strategy("price_break", params={"strength_threshold": 60})

# 执行策略
market_data = {
    "symbol": "AAPL",
    "current_price": 180.0,
    "resistance": 175.0,
    "support": 170.0,
    "volume_ratio": 2.0,
}

result = strategy.execute("AAPL", market_data)

if result:
    print(f"方向：{result['direction']}")
    print(f"强度：{result['strength']}")
    print(f"置信度：{result['confidence']}")

# 列出所有策略
strategies = list_strategies()
for s in strategies:
    print(f"{s['name']} v{s['version']}: {s['description']}")
```

---

## 内置策略

| 策略名称 | 类名 | 版本 | 描述 |
|----------|------|------|------|
| price_break | PriceBreakStrategy | 1.0.0 | 价格突破 + 成交量确认 |
| ma_cross | MACrossStrategy | 1.0.0 | 均线交叉（金叉/死叉） |
| rsi_rebound | RSIReboundStrategy | 1.0.0 | RSI 超买超卖反弹 |

---

## 添加新策略

1. 创建策略文件：`app/decision/strategies/my_strategy.py`
2. 继承 `BaseStrategy` 并实现必要方法
3. 使用 `@register_strategy` 装饰器
4. 在 `__init__.py` 中导出（可选）

### 示例模板

```python
"""
我的策略

策略逻辑说明...
"""
from typing import Dict, Any, List
from .base import BaseStrategy, StrategyResult, register_strategy


@register_strategy
class MyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "my_strategy"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "我的策略描述"

    @property
    def required_data(self) -> List[str]:
        return ["field1", "field2"]

    def generate_signal(self, symbol: str, market_data: Dict[str, Any]) -> StrategyResult:
        # 策略逻辑
        return StrategyResult(
            direction="LONG",
            strength=70,
            confidence=0.7,
        )
```

---

## 策略参数调整

策略参数通过 `strategy_params.json` 或代码传递：

```python
# 方式 1: 构造函数传递
strategy = get_strategy("price_break", params={
    "strength_threshold": 60,
    "confidence_floor": 0.4,
})

# 方式 2: 从配置文件加载
import json
with open("config/strategy_params.json") as f:
    params = json.load(f).get("strategies", {}).get("price_break", {})
strategy = get_strategy("price_break", params=params)
```

---

## 策略版本管理

- 使用语义化版本号（如 `1.0.0`）
- 策略变更后更新版本号
- 在 `docs/STRATEGY_CHANGELOG.md` 记录变更

---

*文档更新时间：2026-04-06*
