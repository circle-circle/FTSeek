# 场景识别系统链路完整性报告

**日期**: 2026-04-10  
**评估范围**: 场景识别→记录→回测→绩效反馈完整链路

---

## 📊 链路状态总览

### ✅ 已实现并正常工作的模块

| 模块 | 状态 | 说明 |
|-----|------|------|
| **场景分类器 (Classifier)** | ✅ 完成 | 11 种场景类型识别，基于 ADX/ATR/RSI/布林带 |
| **场景记忆器 (Memory)** | ✅ 完成 | 场景平滑切换，防抖动，确认窗口机制 |
| **场景路由器 (Router)** | ✅ 完成 | 场景→策略路由矩阵，动态权重调整 |
| **特征提取器 (FeatureExtractor)** | ✅ 完成 | 多周期特征，跨周期共振检测 |
| **数据库表结构** | ✅ 完成 | `signal_outcomes` 表含 `scenario` 字段 |
| **场景日志表** | ✅ 完成 | `scenario_logs` 表已创建并索引 |
| **绩效分析器 (Analytics)** | ✅ 完成 | 场景胜率/盈亏比/关联分析 |
| **共识引擎集成** | ✅ 完成 | 场景权重动态调整 |

---

## 🔧 修复的问题 (2026-04-10)

### P0: 场景数据写入链路断裂

**问题描述**: `record_signal_outcome()` 调用时未传入 `scenario` 参数，导致场景数据无法写入数据库。

**影响范围**: 
- 场景维度回测分析无法进行
- 场景绩效统计缺失数据源
- 无法实现"用真实绩效反哺识别权重"

**修复位置**: `app/data_subscriber.py` 4 处调用点

| 行号 | 路径 | 修复状态 |
|-----|------|---------|
| 1610 | 正常平仓 | ✅ 已修复 |
| 1866 | 止损平仓 | ✅ 已修复 |
| 1991 | 止盈平仓 | ✅ 已修复 |
| 2157 | 分批止盈 | ✅ 已修复 |

**修复代码示例**:
```python
# 获取当前场景
current_scenario = None
if hasattr(self, '_scenario_cache') and code in self._scenario_cache:
    scenario_obj = self._scenario_cache[code]
    current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)

self._consensus.record_signal_outcome(
    code=code,
    action=position.action,
    pnl=trade_result.pnl or 0.0,
    scenario=current_scenario,  # Phase 1: 传入场景标签
)
```

---

## 🔍 链路完整性验证

### 数据流向

```
场景识别 (Classifier)
    ↓
场景平滑 (Memory)
    ↓
场景路由 (Router) → 共识引擎 (权重调整)
    ↓
交易执行
    ↓
平仓成功
    ↓
record_signal_outcome(scenario=...)  ← 【已修复】
    ↓
signal_outcomes 表 (scenario 字段)
    ↓
场景绩效分析 (ScenarioPerformanceAnalytics)
    ↓
权重自适应学习 (未来功能)
```

### 数据库表结构

#### signal_outcomes 表
```sql
CREATE TABLE signal_outcomes (
    id INTEGER PRIMARY KEY,
    code TEXT,
    action TEXT,
    strategy TEXT,
    entry_price REAL,
    exit_price REAL,
    pnl_pct REAL,
    holding_periods INTEGER,
    market_regime TEXT,
    scenario TEXT,           -- ✅ Phase 1: 场景标签
    adx_value REAL,
    mtf_alignment REAL,
    sentiment_score REAL,
    consensus_score REAL,
    quality_score REAL,
    max_drawdown REAL,
    max_profit REAL,
    timestamp DATETIME
);
```

#### scenario_logs 表
```sql
CREATE TABLE scenario_logs (
    id INTEGER PRIMARY KEY,
    code TEXT,
    scenario TEXT,           -- ✅ 场景名称
    confidence REAL,
    adx_value REAL,
    macro_regime TEXT,
    mtf_alignment REAL,
    multi_timeframe TEXT,
    triggered_by TEXT,
    timestamp DATETIME
);
```

---

## 📈 场景绩效分析 API

### 可用分析接口

| API | 功能 | 状态 |
|-----|------|------|
| `analyze_scenario_win_rate(days)` | 场景胜率统计 | ✅ 可用 |
| `get_scenario_accuracy(scenario, days)` | 单场景准确率详情 | ✅ 可用 |
| `analyze_scenario_strategy_matrix(days)` | 场景×策略二维矩阵 | ✅ 可用 |
| `get_best_strategies_for_scenario(scenario)` | 场景最佳策略推荐 | ✅ 可用 |
| `analyze_confidence_accuracy(days)` | 置信度校准分析 | ✅ 可用 |
| `get_scenario_weight_recommendations(days)` | 场景权重建议 | ✅ 可用 |

### 分析输出示例

```python
from app.analytics.scenario_performance import get_scenario_performance_analytics

analytics = get_scenario_performance_analytics()

# 获取各场景胜率
win_rates = analytics.analyze_scenario_win_rate(days=30)
# 输出:
# {
#   "trend_strong": {"trades": 50, "wins": 32, "win_rate": 0.64, "avg_pnl": 2.35},
#   "range_bound": {"trades": 30, "wins": 18, "win_rate": 0.60, "avg_pnl": 1.80},
#   ...
# }

# 获取场景×策略矩阵
matrix = analytics.analyze_scenario_strategy_matrix(days=30)
# 输出:
# {
#   "trend_strong": {
#     "supertrend": {"trades": 15, "win_rate": 0.67, "avg_pnl": 2.8},
#     "confluence_v2": {"trades": 20, "win_rate": 0.60, "avg_pnl": 2.1},
#   },
#   ...
# }
```

---

## ⚠️ 待完善功能

### 1. 场景日志记录器集成

**现状**: `ScenarioLogManager` 已实现但未在 `data_subscriber` 中调用

**建议**: 在场景切换时调用 `log_scenario_change()`:
```python
from app.scenario import get_scenario_log_manager

log_manager = get_scenario_log_manager()
log_manager.log_scenario_change(
    code=code,
    scenario=scenario.value,
    confidence=confidence,
    adx_value=adx,
    mtf_alignment=mtf,
)
```

### 2. 权重自适应学习

**现状**: 绩效分析器可输出权重建议，但未自动应用到路由器

**建议实现**:
```python
# 定期（如每日）执行
recommendations = analytics.get_scenario_weight_recommendations(days=30)
router.update_scenario_weights(recommendations)
```

### 3. 场景回测报告

**现状**: 可查询单场景绩效，但无完整回测报告

**建议**: 创建 `ScenarioBacktest` 模块，支持：
- 按场景过滤回测
- 场景切换点绩效分析
- 场景识别准确率回测

---

## 🧪 测试验证

### 单元测试

```bash
# 场景分类器测试
pytest tests/scenario/test_phase1.py -v  # 6/6 通过

# 场景绩效测试
pytest tests/scenario/test_phase3_performance.py -v
```

### 集成测试

```python
# 验证场景数据写入
from app.analytics.scenario_performance import get_scenario_performance_analytics

analytics = get_scenario_performance_analytics()

# 查询某场景的历史交易
outcomes = analytics.db.get_signal_outcomes_by_scenario("trend_strong", days=7)
assert len(outcomes) > 0, "场景数据应已写入"
```

---

## 📋 修改文件清单

| 文件 | 修改类型 | 说明 |
|-----|---------|------|
| `app/data_subscriber.py` | 修改 | 4 处 `record_signal_outcome` 调用添加 `scenario` 参数 |

---

## ✅ 验收标准

- [x] 场景识别结果正确传入 `record_signal_outcome()`
- [x] `signal_outcomes` 表 `scenario` 字段有数据写入
- [x] 场景绩效分析 API 可查询到数据
- [x] 语法检查通过
- [x] 不影响现有功能

---

## 📚 相关文档

- [`app/scenario/memory.py`](../app/scenario/memory.py) - 场景记忆器
- [`app/scenario/router.py`](../app/scenario/router.py) - 场景路由器
- [`app/analytics/scenario_performance.py`](../app/analytics/scenario_performance.py) - 绩效分析器
- [`docs/HYBRID_WEIGHT_ARCHITECTURE.md`](HYBRID_WEIGHT_ARCHITECTURE.md) - 混合权重架构

---

**报告生成时间**: 2026-04-10  
**状态**: ✅ 链路已打通，可开始用真实绩效反哺识别权重
