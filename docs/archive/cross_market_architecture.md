# FTSeek 跨市场模块 - 架构迭代总结

> 日期: 2026-04-13
> 状态: ✅ 已完成并通过自查 (16/16 检查项通过)

---

## 🏗️ 架构设计原则

### 1. 模块化 + 低耦合
```
app/data/
  ├── us_data_fetcher.py     # 数据获取 (不关心映射关系)
  └── us_data_config.py      # 数据配置管理

app/utils/
  └── cross_market_mapper.py # 映射关系 (不关心数据获取)

app/pipeline/
  └── premarket_analyst.py   # 盘前分析 (使用接口，不关心实现)

config/
  ├── cross_market_mapping.yaml  # 映射数据
  └── us_data_config.yaml        # 数据源配置
```

每个模块职责单一：
- `us_data_fetcher` 只负责获取数据
- `cross_market_mapper` 只负责映射关系
- `premarket_analyst` 只负责分析

### 2. 配置集中
```
环境变量 (.env) 
    ↓
.env.us (FTSeek-US 共享)
    ↓
config/us_data_config.yaml (默认值)
```

API Key 自动加载逻辑：
```python
def get_api_key(provider):
    # 1. 环境变量
    if os.getenv(f"{provider}_API_KEY"):
        return ...
    # 2. .env.us 共享
    if os.path.exists("/opt/ftseek/FTSeek-US/.env.us"):
        return ...
    # 3. 默认空
    return ""
```

### 3. 优雅降级
```
数据源优先级:
1. Finnhub (实时报价，最快)
2. TwelveData (技术指标、历史 K 线)
3. Alpha Vantage (备用 + VIX)

任一步骤失败不阻塞整体流程：
- 数据获取失败 → 返回降级消息
- LLM 分析失败 → 返回 fallback 结果
```

### 4. 自查自纠
- 内置自检接口 (`self_check()`、`get_health_status()`)
- 独立自查脚本 (`scripts/self_check_cross_market.py`)
- 生成 JSON 报告 (`logs/cross_market_self_check.json`)

---

## 📋 变更清单

### 新增文件
| 文件 | 用途 |
|------|------|
| `config/us_data_config.yaml` | 美股数据源集中配置 |
| `app/data/us_data_config.py` | 配置管理器 (API Key 自动加载) |
| `app/data/us_data_fetcher.py` | 三数据源架构 (参考 FTSeek-US) |
| `scripts/self_check_cross_market.py` | 自查自纠脚本 |

### 修改文件
| 文件 | 变更 |
|------|------|
| `config/config.yaml` | 新增 `watch_list_us` (28 只美股) |
| `app/pipeline/premarket_analyst.py` | SYSTEM_PROMPT 增加跨市场分析原则，`_build_prompt()` 注入美股上下文 |

---

## 📊 数据流

```
美股收盘 (04:00 HKT)
    │
    ▼
[USMarketDataRouter] 三数据源获取
  ├── TwelveData (SPY/QQQ 大盘)
  ├── Finnhub (实时报价)
  └── Alpha Vantage (VIX)
    │
    ▼
[CrossMarketMapper] 查找映射关系
  ├── 23 条直接映射
  └── 5 对套利配对
    │
    ▼
[PreMarketAnalyst] 注入美股上下文
  ├── 大盘指数 (SPY/QQQ/VIX)
  └── 重点个股 (涨跌 + 名称)
    │
    ▼
LLM 分析 → 输出盘前交易指导
```

---

## ✅ 自查结果

```
检查项: 16/16 通过
  ✅ 文件存在性: 7/7
  ✅ 配置加载: 跨市场映射 (23 条)
  ✅ 数据源状态: TwelveData/Finnhub/AlphaVantage 全启用
  ✅ 数据连通性: US.SPY ($679), US.BABA ($125)
  ✅ 盘前分析器: _get_us_market_context 方法 (19 行上下文)
  ✅ 配置一致性: 核心美股代码全部在 watch_list_us 中

总体状态: ✅ 健康
```

---

## 🔧 运行自查

```bash
cd /opt/ftseek/FTSeek
python3.8 scripts/self_check_cross_market.py
```

报告保存在: `logs/cross_market_self_check.json`

---

## 📝 使用示例

```python
# 获取跨市场映射
from app.utils.cross_market_mapper import get_cross_market_mapper
mapper = get_cross_market_mapper()

# NVDA 影响哪些港股?
print(mapper.get_hk_codes_by_us("US.NVDA"))
# → ['HK.00981', 'HK.01347', 'HK.02382']

# 获取美股数据
from app.data.us_data_fetcher import get_us_data_fetcher
fetcher = get_us_data_fetcher()

quote = fetcher.get_quote("US.BABA")
print(f"BABA: ${quote['price']} ({quote['change_pct']:+.2f}%)")

# 盘前上下文
context = fetcher.get_premarket_context(mapper.get_core_us_codes())
print(context)
```

---

## ⚠️ 注意事项

1. **API Key**: 自动从 FTSeek-US 的 `.env.us` 共享，无需单独配置
2. **限速**: TwelveData 8 credits/min，批量获取限制 15 只
3. **缓存**: 大盘数据 15 分钟，个股报价 1 分钟
4. **非交易时段**: 获取的是上一交易日收盘数据
