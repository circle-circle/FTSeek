# FTSeek 收盘统计和周报复盘数据修复

## 问题描述

2026-03-14 用户反馈：FTSeek 推送到钉钉的收盘统计和周报复盘数据不准确。

## 问题根源

经过分析，发现以下问题：

1. **数据源不一致**：
   - `daily_stats.py` 和 `weekly_review.py` 从本地数据库 (`ftseek.db`) 读取数据
   - 数据库中只有测试信号，没有实际交易记录
   - 实际交易通过富途 OpenD 执行，但没有同步到 FTSeek 数据库

2. **持仓数据缺失**：
   - `get_current_positions()` 从数据库的未平仓交易读取
   - 实际持仓在富途账户中，数据库中没有记录

3. **交易统计错误**：
   - 今日信号数、执行数从数据库统计
   - 实际交易数据在 OpenD 中

## 修复方案

### 1. 修改 `daily_stats.py`

**修改内容**：
- `get_today_stats()`: 优先从风险管理器获取实际账户数据，数据库作为备选
- `get_current_positions()`: 优先从风险管理器的 `account.positions` 获取实际持仓

**数据流**：
```
风险管理器 (account.positions) → 实际持仓数据
     ↓ (如果失败)
本地数据库 → 备选数据
```

### 2. 修改 `weekly_review.py`

**修改内容**：
- `_get_trades_in_period()`: 优先从富途 OpenD API 获取历史订单，数据库作为备选

**数据流**：
```
OpenD API (get_history_order_list) → 实际交易记录
     ↓ (如果失败)
本地数据库 → 备选数据
```

### 3. 新增 `risk_manager.py` 方法

**新增方法**：
- `get_closed_trades_today()`: 获取今日已平仓交易列表
  - 优先从内部交易历史获取
  - 备选从 OpenD API 获取

## 修复文件

1. `/opt/ftseek/FTSeek/app/reporters/daily_stats.py`
   - 修改 `get_today_stats()` 方法
   - 修改 `get_current_positions()` 方法

2. `/opt/ftseek/FTSeek/app/performance/weekly_review.py`
   - 修改 `_get_trades_in_period()` 方法

3. `/opt/ftseek/FTSeek/app/risk/risk_manager.py`
   - 新增 `get_closed_trades_today()` 方法

## 测试方法

运行测试脚本：
```bash
cd /opt/ftseek/FTSeek
python3 test_daily_stats_fix.py
```

## 验证步骤

1. **检查持仓数据**：
   - 对比钉钉推送的持仓 vs 富途账户实际持仓
   - 应该显示相同的股票和数量

2. **检查交易统计**：
   - 对比钉钉推送的今日信号数 vs 实际执行交易数
   - 应该一致

3. **检查盈亏数据**：
   - 对比钉钉推送的今日盈亏 vs 富途账户实际盈亏
   - 应该接近（考虑未实现盈亏）

## 后续优化建议

1. **数据同步**：考虑将 OpenD 的交易记录自动同步到本地数据库，提高查询性能

2. **缓存机制**：为 API 调用添加缓存，避免频繁请求 OpenD

3. **错误处理**：增强错误处理，当 OpenD 不可用时优雅降级

4. **监控告警**：添加数据一致性检查，当数据库和 OpenD 数据差异过大时告警

## 修复日期

2026-03-14

## 修复人员

OpenClaw Assistant
