# FTSeek 跨市场联动 - 实施完成报告

> 日期: 2026-04-13
> 状态: ✅ 29/29 检查项通过

---

## ⏰ 时间线分析 (严格基于事实)

### 港股 vs 美股交易时间

| 市场 | 交易时间 (HKT) |
|------|---------------|
| 港股早盘 | 09:30 - 12:00 |
| 港股午盘 | 13:00 - 16:00 |
| 美股交易 | 21:30 - 04:00 (次日) |

**结论：港股和美股交易时间完全不重叠。**

### 可行的联动方式

| 联动方式 | 可行性 | 状态 |
|---------|--------|------|
| 美股收盘 → 港股盘前 (08:30) | ✅ | 已实现 |
| 美股事件 → 缓存 → 次日盘前 | ✅ | 已实现 |
| 双重上市套利 (延迟) | ✅ | 已实现 |
| 美股盘中 → 港股实时 | ❌ | 不可行 |

---

## 📋 实施清单

### 1. ✅ 跨市场事件缓存机制

**文件**: `app/data/cross_market_event_cache.py`

**功能**:
- 美股交易时段 (21:30-04:00 HKT) 采集价格异动和新闻
- 持久化到 SQLite (`data/cross_market_events.db`)
- 次日港股盘前 (08:30 HKT) 读取缓存
- 24 小时 TTL 自动清理

**Cron 脚本**: `scripts/cross_market_event_cron.py`
```bash
# 每 15 分钟 (美股交易时段)
*/15 21-23 * * 1-5 cd /opt/ftseek/FTSeek && python3.8 scripts/cross_market_event_cron.py
*/15 0-3 * * 2-6 cd /opt/ftseek/FTSeek && python3.8 scripts/cross_market_event_cron.py
```

### 2. ✅ 双重上市套利策略

**文件**: `app/strategy/arb_dual_listed.py`

**适用标的**:
| 美股 | 港股 | 名称 | 阈值 |
|------|------|------|------|
| US.BABA | HK.09988 | 阿里巴巴 | 2.0% |
| US.JD | HK.09618 | 京东 | 2.0% |
| US.BIDU | HK.09888 | 百度 | 2.0% |
| US.LI | HK.02015 | 理想 | 2.5% |
| US.XPEV | HK.09868 | 小鹏 | 2.5% |

**逻辑**:
```
美股收盘 → 计算理论港股价格 = 美股价格 × 7.8 × 换股比例
港股开盘 → 比较实际价格 vs 理论价格
         → 如果 |价差| > 阈值 → 生成套利信号
```

### 3. ✅ 盘前分析器集成

**修改文件**: `app/pipeline/premarket_analyst.py`

**新增内容**:
- `_get_us_market_context()` - 组合三个数据源
- `_get_cross_market_events_context()` - 注入事件缓存
- `_get_arb_context()` - 注入套利检查

---

## 📊 数据流

```
美股交易时段 (21:30-04:00 HKT)
    │
    ▼
[cross_market_event_cron.py] 每 15 分钟
  ├── 采集价格异动 (>±2%)
  ├── 采集市场新闻 (Finnhub)
  └── 写入 SQLite 缓存
    │
    ▼
[cross_market_events.db] 持久化 (24h TTL)

次日港股盘前 (08:30 HKT)
    │
    ▼
[PreMarketAnalyst._get_us_market_context()]
  ├── 美股市场收盘数据 (SPY/QQQ/个股)
  ├── 跨市场事件缓存 (最近 12 小时)
  └── 双重上市套利检查
    │
    ▼
LLM 盘前分析 → 输出交易指导
```

---

## 📁 文件清单

### 新增文件
| 文件 | 用途 |
|------|------|
| `app/data/cross_market_event_cache.py` | 事件缓存管理器 |
| `app/strategy/arb_dual_listed.py` | 双重上市套利策略 |
| `scripts/cross_market_event_cron.py` | 事件采集 Cron 脚本 |
| `config/us_data_config.yaml` | 集中配置 (含套利配置) |
| `app/data/us_data_config.py` | 配置管理器 |
| `app/data/us_data_fetcher.py` | 三数据源架构 |
| `scripts/self_check_cross_market.py` | 自查脚本 (v2) |

### 修改文件
| 文件 | 变更 |
|------|------|
| `app/pipeline/premarket_analyst.py` | 集成事件缓存 + 套利检查 |
| `config/config.yaml` | 新增 watch_list_us |
| `app/config.py` | 增强日志 |

---

## ✅ 自查结果

```
检查项: 29/29 通过
  ✅ 文件存在性: 10/10
  ✅ 配置加载: 跨市场映射 (23 条), 数据源 (3 个)
  ✅ 数据连通性: SPY $679, BABA $125
  ✅ 事件缓存: 写入/读取/清理 全部成功
  ✅ 套利策略: 5 对配置, 信号生成正常
  ✅ 盘前分析器: 3 个方法全部集成
  ✅ 配置一致性: 28 只美股一致

总体状态: ✅ 健康
```

---

## 🔧 运行自查

```bash
cd /opt/ftseek/FTSeek
python3.8 scripts/self_check_cross_market.py
```

报告: `logs/cross_market_self_check_v2.json`

---

## ⚠️ 已知限制

1. **港股收盘价获取**: 盘前分析时港股未开盘，`_get_hk_prev_close_prices()` 目前返回 0
   - 解决方案: 从富途 API 获取上一交易日收盘价 (需要富途连接)

2. **期货数据**: TwelveData 不支持 ES/NQ 期货
   - 解决方案: 使用 SPY/QQQ 作为大盘替代 (已实现)

3. **套利执行**: 当前仅生成信号，未接入执行层
   - 解决方案: 后续接入 DecisionBuffer

---

## 📝 使用示例

```python
# 事件缓存
from app.data.cross_market_event_cache import get_cross_market_event_cache, CrossMarketEvent
import time

cache = get_cross_market_event_cache()

# 写入事件
event = CrossMarketEvent(
    event_id="price_NVDA_1776091929",
    timestamp=time.time(),
    event_type="price_move",
    us_code="US.NVDA",
    hk_codes=["HK.00981", "HK.01347"],
    title="NVDA 价格异动 -3.5%",
    content="NVDA 下跌 3.5%，报 $185.00",
    impact_score=-0.35,
)
cache.write(event)

# 盘前读取
events = cache.read_for_premarket(hours=12)
for e in events:
    print(f"{e.title} → {e.hk_codes}")

# 套利检查
from app.strategy.arb_dual_listed import get_dual_listed_arb
arb = get_dual_listed_arb()

us_prices = {"US.BABA": 126.0, "US.JD": 28.5}
hk_prices = {"HK.09988": 980.0, "HK.09618": 220.0}

signals = arb.generate_signals(us_prices, hk_prices)
for s in signals:
    print(f"{s.hk_code} {s.direction} 价差: {s.spread_pct:+.2f}%")
```
