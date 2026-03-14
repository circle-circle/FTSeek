# FTSeek 绩效追踪与复盘系统 - 实施计划

**设计文档**: `docs/plans/2026-03-02-performance-tracker-design.md`  
**创建日期**: 2026-03-02  
**预计时间**: 2-3 天  
**执行模式**: 子代理驱动 (TDD + 双重审查)  
**交易模式**: 模拟盘观测

---

## 📋 任务列表

### Task 1: 创建绩效追踪器模块 (performance_tracker)

**目标**: 实现交易配对和收益率计算

**文件**:
- `app/performance/tracker.py` - 核心追踪器
- `app/performance/__init__.py` - 模块导出
- `tests/test_performance_tracker.py` - 单元测试

**功能**:
```python
class PerformanceTracker:
    - match_trades()      # 配对买卖交易
    - calculate_return()   # 计算单笔收益率
    - get_position()       # 获取当前持仓
    - get_total_return()   # 计算总收益率
```

**验收标准**:
- [ ] 单元测试通过率 100%
- [ ] 能正确配对买卖交易
- [ ] 收益率计算准确
- [ ] 支持多股票同时追踪

---

### Task 2: 扩展数据库支持绩效数据

**目标**: 添加绩效相关表结构

**文件**:
- `app/storage/database.py` - 扩展表结构
- `scripts/migrate_performance.py` - 数据迁移脚本

**新增表**:
```sql
-- 交易配对记录
CREATE TABLE trade_pairs (
    id TEXT PRIMARY KEY,
    buy_signal_id TEXT,
    sell_signal_id TEXT,
    code TEXT,
    buy_price REAL,
    sell_price REAL,
    quantity INTEGER,
    return_rate REAL,
    profit REAL,
    open_time DATETIME,
    close_time DATETIME,
    status TEXT
);

-- 绩效汇总
CREATE TABLE performance_summary (
    id TEXT PRIMARY KEY,
    period TEXT,
    total_trades INTEGER,
    win_rate REAL,
    total_return REAL,
    avg_return REAL,
    max_drawdown REAL,
    created_at DATETIME
);
```

**验收标准**:
- [ ] 表结构创建成功
- [ ] 数据迁移无错误
- [ ] 向后兼容现有功能

---

### Task 3: 创建周报复盘模块 (weekly_review)

**目标**: 生成周度绩效报告

**文件**:
- `app/performance/weekly_review.py` - 周报复盘
- `scripts/generate_weekly_report.py` - CLI 工具

**功能**:
```python
class WeeklyReviewer:
    - generate_report()     # 生成周报
    - analyze_strategies()  # 策略分析
    - compare_benchmark()   # 基准对比
```

**报告内容**:
- 本周交易汇总
- 各策略表现
- 胜率统计
- 收益率分析
- 改进建议

**验收标准**:
- [ ] 能生成完整周报
- [ ] 数据准确
- [ ] 支持推送到钉钉

---

### Task 4: 创建回测对比模块 (backtest_compare)

**目标**: 实盘 vs 回测基础对比

**文件**:
- `app/performance/backtest_compare.py` - 对比分析

**功能**:
```python
class BacktestComparer:
    - compare_returns()     # 收益率对比
    - compare_win_rates()   # 胜率对比
    - generate_report()     # 生成对比报告
```

**对比指标**:
- 实盘收益率 vs 回测收益率
- 实盘胜率 vs 回测胜率
- 交易次数对比
- 平均收益对比

**验收标准**:
- [ ] 对比数据准确
- [ ] 报告清晰易懂
- [ ] 支持按策略分别对比

---

### Task 5: 集成到实时行情监控

**目标**: 在交易执行后自动计算收益

**文件**:
- `scripts/trade_executor_manual.py` - 扩展执行器
- `scripts/realtime_monitor.py` - 扩展监控

**集成点**:
```python
# 交易执行完成后
executor.execute(signal_id, action)
→ 调用 tracker.record_trade()
→ 计算已实现收益
→ 推送到钉钉 (添加收益信息)
```

**验收标准**:
- [ ] 交易完成后自动计算收益
- [ ] 钉钉推送包含收益信息
- [ ] 不影响现有交易流程

---

### Task 6: 集成到钉钉推送

**目标**: 增强钉钉消息包含绩效信息

**文件**:
- `app/notifier/dingtalk.py` - 扩展推送模板

**推送增强**:
1. **交易确认消息** - 添加历史收益统计
2. **交易结果消息** - 添加该笔交易收益
3. **周报推送** - 每周自动推送

**验收标准**:
- [ ] 消息模板美观
- [ ] 信息完整准确
- [ ] 推送时机正确

---

### Task 7: 创建定时任务

**目标**: 自动执行周报复盘

**文件**:
- `scripts/weekly_performance_report.py` - 周报脚本

**Crontab 配置**:
```bash
# 每周一 09:00 生成上周周报
0 9 * * 1 cd /opt/ftseek/FTSeek && python3 scripts/weekly_performance_report.py
```

**验收标准**:
- [ ] 定时任务配置正确
- [ ] 周报自动生成
- [ ] 推送到钉钉

---

### Task 8: 创建管理命令和查询工具

**目标**: 方便查询绩效数据

**文件**:
- `scripts/query_performance.py` - 绩效查询 CLI

**功能**:
```bash
# 查询今日收益
python3 scripts/query_performance.py --today

# 查询本周收益
python3 scripts/query_performance.py --week

# 查询某股票收益
python3 scripts/query_performance.py --stock HK0700

# 导出绩效数据
python3 scripts/query_performance.py --export performance.csv
```

**验收标准**:
- [ ] 查询功能完整
- [ ] 输出格式友好
- [ ] 支持导出

---

### Task 9: 文档和测试

**目标**: 完善文档和测试覆盖

**文件**:
- `docs/PERFORMANCE_TRACKING.md` - 使用文档
- `tests/` - 完整测试套件

**文档内容**:
- 安装配置指南
- 使用示例
- API 参考
- 常见问题

**验收标准**:
- [ ] 文档完整
- [ ] 测试覆盖率 >80%
- [ ] 所有测试通过

---

## 📊 执行顺序

```
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9
   ↓        ↓        ↓        ↓        ↓        ↓        ↓        ↓        ↓
 基础     数据库   周报    回测    集成    推送    定时    工具    文档
 功能     支持     模块    对比    监控    增强    任务    查询    测试
```

---

## ✅ 验收标准汇总

### 功能验收
- [ ] 交易配对准确
- [ ] 收益率计算正确
- [ ] 周报自动生成
- [ ] 回测对比可用
- [ ] 钉钉推送正常

### 质量验收
- [ ] 单元测试通过率 100%
- [ ] 代码审查通过
- [ ] 文档完整
- [ ] 无严重 Bug

### 业务验收
- [ ] 模拟盘运行稳定
- [ ] 绩效数据可查询
- [ ] 周报包含所需信息
- [ ] 支持后续扩展到实盘

---

## 🔧 技术栈

- **Python**: 3.8+
- **数据库**: SQLite
- **测试**: pytest
- **代码风格**: PEP 8
- **TDD**: 必须

---

## 📝 注意事项

1. **模拟盘模式** - 所有交易先使用模拟模式
2. **向后兼容** - 不影响现有功能
3. **数据一致性** - 确保数据库事务正确
4. **性能考虑** - 查询需要索引优化
5. **安全性** - 敏感信息不写入日志

---

*最后更新：2026-03-02 | FTSeek V3*
