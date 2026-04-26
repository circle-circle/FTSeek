# FTSeek 跨市场映射实施文档

> 更新时间: 2026-04-13
> 状态: ✅ 已完成

## 📋 实施概述

完成美股 → 港股的跨市场映射系统，实现:
1. **34 只美股** 监控名单
2. **23 条直接映射** 关系 (美股 → 港股)
3. **5 对跨市场套利** 配对
4. **23 只港股** 有关联美股
5. **盘前 LLM 分析** 自动注入真实美股数据

---

## 🗂️ 新增文件

| 文件 | 用途 |
|------|------|
| `config/cross_market_mapping.yaml` | 跨市场映射配置 (美股名单、映射关系、套利配对) |
| `app/utils/cross_market_mapper.py` | 跨市场映射器 (加载配置、查询映射) |
| `app/data/us_data_fetcher.py` | 美股数据获取器 (快照、K 线、技术指标) |
| `scripts/test_cross_market_mapping.py` | 测试脚本 |

## 🔧 修改文件

| 文件 | 修改内容 |
|------|---------|
| `config/config.yaml` | 新增 `watch_list_us` (28 只美股) |
| `app/config.py` | 增强 `_load_watch_list_us()` 日志 |
| `app/pipeline/premarket_analyst.py` | SYSTEM_PROMPT 增加跨市场分析原则，`_build_prompt()` 注入美股上下文 |

---

## 🗺️ 映射关系示例

### 直接映射 (双重上市)
| 美股 | 港股 | 相关性 |
|------|------|--------|
| US.BABA | HK.09988 | 0.95 |
| US.JD | HK.09618 | 0.93 |
| US.BIDU | HK.09888 | 0.92 |
| US.LI | HK.02015 | 0.94 |
| US.XPEV | HK.09868 | 0.93 |

### 板块传导
| 美股 | 影响港股 | 类型 |
|------|---------|------|
| US.NVDA | HK.00981, HK.01347, HK.02382 | 半导体 |
| US.TSLA | HK.01211, HK.02015, HK.09868, HK.01810 | 新能源车 |
| US.GS | HK.00388, HK.02318, HK.01299 | 金融情绪 |

### 大盘指数
| 指数 | 影响范围 | 权重 |
|------|---------|------|
| US.SPY | 全市场 | 1.0 |
| US.QQQ | 科技股 | 0.9 |
| US.UVXY | 全市场 (反向) | 0.8 |

---

## 🔄 数据流

```
美股收盘 (04:00 HKT)
    │
    ▼
[USDataFetcher] 获取快照 + K 线
    │
    ▼
[CrossMarketMapper] 查找映射关系
    │
    ▼
[PreMarketAnalyst] 注入美股上下文到 LLM prompt
    │
    ▼
LLM 分析 → 输出盘前交易指导
    │
    ▼
[StrategyRouter] 路由到执行/确认/观察
```

---

## 📊 订阅说明

**当前状态**: 美股实时行情**未订阅**

美股数据获取方式:
1. **盘前一次性获取**: 在 08:30 HKT 盘前分析时，通过 `get_market_snapshot` 获取上一交易日收盘数据
2. **不占用实时订阅槽位**: 使用独立 API 调用，不影响港股 100 槽位限制

**如果需要实时美股行情** (盘中监控):
- 需要额外订阅美股 QUOTE
- 每只美股占用 1 个槽位
- 建议只订阅核心 10 只 (SPY/QQQ/BABA/JD/NVDA/TSLA/AAPL/GS 等)

---

## 🧪 测试

```bash
cd /opt/ftseek/FTSeek
python3.8 scripts/test_cross_market_mapping.py
```

预期输出:
```
✅ mapper: 通过 (34 只美股, 23 条映射)
✅ config: 通过 (28 只美股从 config.yaml 加载)
✅ us_data: 通过 (快照获取成功)
✅ analyst: 通过 (美股上下文注入成功)
```

---

## 📝 使用示例

### Python 代码

```python
from app.utils.cross_market_mapper import get_cross_market_mapper
from app.data.us_data_fetcher import get_us_data_fetcher

mapper = get_cross_market_mapper()
fetcher = get_us_data_fetcher()

# 查询美股影响的港股
hk_codes = mapper.get_hk_codes_by_us("US.NVDA")
# → ['HK.00981', 'HK.01347', 'HK.02382']

# 查询港股关联的美股
us_codes = mapper.get_us_codes_by_hk("HK.00981")
# → ['US.NVDA', 'US.AMD', 'US.TSM']

# 获取美股快照
snapshot = fetcher.get_us_snapshot(["US.SPY", "US.AAPL", "US.NVDA"])

# 获取盘前上下文
context = fetcher.get_premarket_context(mapper.get_core_us_codes())

fetcher.close()
```

---

## ⚠️ 注意事项

1. **VIX 指数**: 富途不支持直接订阅 VIX 指数，使用 `US.UVXY` (VIX 短期期货 ETF) 替代
2. **非交易时段**: 美股非交易时段 (港股白天) 获取的是上一交易日收盘数据
3. **代码格式**: 美股代码必须带 `US.` 前缀 (如 `US.SPY` 不是 `SPY`)
4. **槽位限制**: 美股快照获取不占用实时订阅槽位

---

## 🔜 后续优化

1. **美股实时订阅**: 如需盘中监控，添加核心美股 QUOTE 订阅
2. **历史相关性计算**: 基于历史数据计算更精确的跨市场相关系数
3. **自动套利信号**: 当双重上市价差超过阈值时自动生成套利信号
4. **情绪指标聚合**: 将 UVIX/SPY/QQQ 变化聚合为单一"美股情绪分数"
