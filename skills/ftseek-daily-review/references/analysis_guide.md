# FTSeek 复盘分析参考

## 复盘数据源

### 1. 数据库查询路径

```sql
-- 当日交易记录
SELECT * FROM positions WHERE date(entry_time) = 'YYYY-MM-DD';

-- 信号记录
SELECT * FROM signals WHERE date(created_at) = 'YYYY-MM-DD';
```

### 2. 日志文件路径

- 主日志: `logs/ftseek.log`
- 风控日志: `logs/risk_monitor.log`
- 信号监控: `logs/signal_monitor.log`

### 3. JSON数据路径

- 信号记录: `data/signal_records/signals_YYYY-MM-DD.json`
- 监控数据: `data/monitoring/monitor_YYYY-MM-DD.json`

---

## 核心问题类型

### 问题分类

| 类型 | 严重程度 | 识别条件 |
|------|----------|----------|
| 大额亏损 | critical | 单笔亏损 > 1000 HKD 或跌幅 > 5% |
| 止损未触发 | high | 出场价 < 止损价 (BUY) 或 > 止损价 (SELL) |
| 重复开仓 | high | 同一标的同一秒内多次开仓 |
| 胜率过低 | medium | 当日胜率 < 50% |
| 执行率过低 | medium | 信号执行率 < 30% |

---

## 拒绝原因分类

| 原因 | 关键字 | 说明 |
|------|--------|------|
| 已有持仓 | existing_position | 持仓占用导致无法开新仓 |
| 碎股限制 | 碎股、不足1手 | 平仓数量不是整手 |
| 弱信号 | weak_signal | 置信度不足 |
| 趋势过滤 | trend_filter、趋势过滤 | 趋势方向不符合 |
| 价格检查 | 价格检查、追高、杀跌 | 入场价格不合理 |
| 持仓限制 | 持仓数、最大持仓 | 超过持仓数量限制 |
| 冷却中 | cooldown | 信号冷却期内 |

---

## 优化建议模板

### P0 紧急

- 修复系统Bug
- 止损逻辑问题
- 数据异常处理

### P1 重要

- 策略参数优化
- 风控规则调整
- 执行效率提升

### P2 优化

- 信号质量提升
- 推送内容增强
- 监控完善

---

## 复盘报告格式

```markdown
# FTSeek 今日复盘报告（YYYY-MM-DD）

## 一、今日盈亏汇总
[交易表格]

## 二、核心问题分析
[问题描述]

## 三、优化建议
[建议列表]

## 四、待办事项
[待办列表]
```

---

## 关键指标

### 盈亏指标

- 总盈亏
- 胜率
- 盈亏比
- 最大回撤

### 执行指标

- 信号执行率
- 平均滑点
- 下单延迟

### 风控指标

- 拦截率
- 熔断次数
- 最大敞口