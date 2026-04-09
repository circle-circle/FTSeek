# 时间维度统计功能

**更新时间**: 2026-03-31 21:39  
**状态**: ✅ 已上线

---

## ✅ 新增功能

### 1. 时间维度选择器

支持按以下时间范围查看交易数据：
- **今日** - 仅显示今日 (2026-03-31) 数据
- **本周** - 最近 7 天数据
- **本月** - 最近 30 天数据
- **全部** - 从 2026-03-31 开始的所有数据

### 2. 核心指标统计

每个时间维度显示：
- **交易数** - 总交易笔数
- **胜率** - 盈利交易占比
- **盈亏** - 总盈亏金额 (HKD)
- **盈亏比** - 平均盈利/平均亏损

### 3. 趋势图表

- **收益趋势图** - 按日显示盈亏变化
- **胜率趋势图** - 按日显示胜率波动

---

## 🎯 使用方式

### Web 界面

访问：
```
http://aitz.cc:8899/simulation/
```

操作：
1. 点击时间选择器 (今日/本周/本月/全部)
2. 查看对应时间范围的统计数据
3. 图表自动更新显示趋势

### API 接口

**统计摘要**:
```bash
curl "http://aitz.cc:8899/api/v1/review/stats/summary?period=today"
curl "http://aitz.cc:8899/api/v1/review/stats/summary?period=week"
curl "http://aitz.cc:8899/api/v1/review/stats/summary?period=month"
curl "http://aitz.cc:8899/api/v1/review/stats/summary?period=all"
```

**趋势数据**:
```bash
curl "http://aitz.cc:8899/api/v1/review/stats/trend?period=month&group_by=day"
```

---

## 📊 数据说明

### 数据积累

- **起始日期**: 2026-03-31
- **数据来源**: 富途 OpenD API
- **同步频率**: 每 30 分钟自动同步

### 统计逻辑

- **交易数**: 统计周期内的总交易笔数
- **胜率**: 已平仓交易中盈利交易占比
- **盈亏**: 已平仓交易的总盈亏
- **盈亏比**: 平均盈利金额 / 平均亏损金额

### 更新频率

- **KPI 指标**: 每 60 秒自动刷新
- **图表数据**: 每 60 秒自动刷新
- **持仓列表**: 每 60 秒自动刷新

---

## 📈 示例场景

### 场景 1: 查看今日表现

1. 点击"今日"按钮
2. 查看今日交易数、胜率、盈亏
3. 分析今日交易质量

### 场景 2: 查看本周趋势

1. 点击"本周"按钮
2. 查看收益趋势图
3. 分析本周交易表现

### 场景 3: 查看本月统计

1. 点击"本月"按钮
2. 查看月度胜率、盈亏比
3. 评估月度交易策略

### 场景 4: 查看全部历史

1. 点击"全部"按钮
2. 查看从 2026-03-31 开始的所有数据
3. 长期跟踪交易表现

---

## 🔧 技术实现

### API 端点

| 端点 | 功能 | 参数 |
|------|------|------|
| `GET /stats/summary` | 统计摘要 | period, start_date, end_date |
| `GET /stats/trend` | 趋势数据 | period, group_by |
| `GET /trades` | 交易列表 | limit, date |

### 数据库查询

```sql
-- 今日统计
SELECT COUNT(*) FROM simulation_trades 
WHERE date(entry_time) = date('now');

-- 本周统计
SELECT COUNT(*) FROM simulation_trades 
WHERE date(entry_time) >= date('now', '-7 days');

-- 本月统计
SELECT COUNT(*) FROM simulation_trades 
WHERE date(entry_time) >= date('now', '-30 days');
```

---

**功能上线时间**: 2026-03-31 21:39  
**状态**: ✅ 已上线，支持日/周/月维度统计
