# FTSeek 复盘改进计划

**日期**: 2026-03-19
**状态**: ✅ 已完成

---

## 问题诊断与修复

### 1. ✅ 已修复：收盘统计盈亏和信号为0

**原因**：
- 复盘脚本查询的是 SQLite `signals` 表
- 实际信号存储在 `data/signal_records/signals_YYYY-MM-DD.json`（signal_tracker）

**解决方案**：
- 创建了 `scripts/daily_review_v2.py`
- 优先从 signal_tracker JSON 文件读取信号
- 回退到数据库查询

### 2. ✅ 已修复：持仓不正确

**原因**：
- 原复盘脚本只显示 `status='OPEN'` 的持仓
- 不展示已平仓交易

**解决方案**：
- 新脚本区分"当前持仓"和"已平仓交易"
- 展示已平仓交易明细和盈亏

### 3. ✅ 已修复：策略配置和损失收益关联

**原因**：
- `positions` 表没有 `signal_id` 字段
- 无法追溯交易与信号的关系

**解决方案**：
- 数据库 schema 升级，添加 `signal_id` 和 `strategy` 字段
- 修改 `position_manager.py` 支持保存信号关联
- 复盘报告展示信号-交易关联分析

### 4. ✅ 已修复：复盘内容完整性

**新增内容**：
- 今日盈亏总览
- 盈亏分解（胜率、盈亏比、平均盈亏）
- 分股票统计
- 已平仓交易明细
- 信号统计（执行率、拒绝原因、置信度分布）
- 信号-交易关联分析
- 改进建议（基于表现自动生成）

### 5. ✅ 已修复：代码错误

**问题**：`'str' object has no attribute 'value'`

**修复文件**：
- `app/trader/executor.py` - 智能路由调用
- `app/strategy/hk_specific.py` - 日志输出
- `app/data_subscriber.py` - 信号记录

**修复方法**：添加类型检查 `hasattr(x, 'value')`

### 6. ✅ 已修复：共识阈值配置

**问题**：启动脚本设置 `CONSENSUS_THRESHOLD=1.0`，但配置文件是 0.4

**修复**：更新 `start.sh` 设置 `CONSENSUS_THRESHOLD=0.4`

---

## 复盘报告示例

**日期**: 2026-03-19

| 指标 | 数值 |
|------|------|
| 已实现盈亏 | HK$-1,000 |
| 平仓交易 | 5 笔 |
| 胜率 | 0.0% |
| 总信号 | 44 |
| 已执行 | 0 |
| 被拒绝 | 38 |
| 执行率 | 0.0% |

### 拒绝原因分析

| 原因 | 数量 |
|------|------|
| 代码错误 | 4 |
| 其他 | 28 |
| 碎股限制 | 6 |

---

## 新复盘脚本使用方法

```bash
# 今日复盘（不推送）
python3 scripts/daily_review_v2.py --no-notify

# 指定日期
python3 scripts/daily_review_v2.py --date 2026-03-19

# 今日复盘并推送钉钉
python3 scripts/daily_review_v2.py
```

---

## 数据库变更

```sql
-- 添加信号关联字段
ALTER TABLE positions ADD COLUMN signal_id TEXT;
ALTER TABLE positions ADD COLUMN strategy TEXT;
```

---

## 后续优化建议

1. **提高信号执行率**：检查拒绝原因，优化执行逻辑
2. **改进策略参数**：基于回测结果调整止损/止盈
3. **监控置信度校准**：验证高置信度信号的实际表现

---

*完成时间: 2026-03-19 16:55*