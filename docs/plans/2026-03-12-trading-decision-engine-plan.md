# 统一决策引擎实施计划

**创建日期**: 2026-03-12  
**关联设计**: `2026-03-12-trading-decision-engine-design.md`  
**状态**: 待执行  
**预计工期**: 3-4 天  
**执行模式**: Subagent-driven (TDD)

---

## 任务分解

### Task 1: 创建数据库模型和迁移脚本
**预计时间**: 15 分钟  
**优先级**: P0 (基础依赖)

**目标**: 创建 SQLite 数据库表结构，支持交易模式、持仓、交易记录、绩效统计

**子任务**:
1.1 创建 `app/v4/models/trading_models.py` - SQLAlchemy 模型定义  
1.2 创建 `app/v4/storage/db_migrate.py` - 数据库迁移脚本  
1.3 编写单元测试 `tests/v4/test_trading_models.py`  
1.4 执行迁移，验证表结构

**验收标准**:
- [ ] 5 张表创建成功 (trading_modes, paper_positions, paper_trades, performance_stats, graduation_history)
- [ ] 模型支持增删改查
- [ ] 单元测试通过率 100%

---

### Task 2: 实现 SignalRateLimiter (信号限流器)
**预计时间**: 20 分钟  
**优先级**: P0

**目标**: 控制信号频率，避免过度交易

**子任务**:
2.1 创建 `app/v4/core/rate_limiter.py`  
2.2 实现限流规则:
   - 每只股票最多 2 个信号/小时
   - 同方向信号去重
   - 冷静期 15 分钟 (卖出后不再买入)
2.3 编写单元测试 `tests/v4/test_rate_limiter.py`  
2.4 集成测试：模拟高频信号，验证限流生效

**验收标准**:
- [ ] 1 小时内第 3 个信号被拒绝
- [ ] 连续买入信号只保留第 1 个
- [ ] 卖出后 15 分钟内买入信号被拒绝
- [ ] 单元测试覆盖率>90%

---

### Task 3: 实现 MarketSessionManager (市场时段管理器)
**预计时间**: 20 分钟  
**优先级**: P0

**目标**: 管理港股/美股交易时段

**子任务**:
3.1 创建 `app/v4/core/session_manager.py`  
3.2 配置港股时段 (09:30-12:00, 13:00-16:00 HKT)  
3.3 配置美股时段 (21:30-04:00 EST)  
3.4 实现时段判断 API: `is_trading_session()`, `get_session_type()`  
3.5 编写单元测试 `tests/v4/test_session_manager.py`

**验收标准**:
- [ ] 正确识别港股/美股交易时段
- [ ] 正确处理跨日时段 (美股)
- [ ] 非交易时段返回 False
- [ ] 单元测试覆盖所有边界情况

---

### Task 4: 实现 PaperTradingEngine (模拟交易引擎)
**预计时间**: 30 分钟  
**优先级**: P0

**目标**: 纸面交易执行和持仓管理

**子任务**:
4.1 创建 `app/v4/trader/paper_trader.py`  
4.2 实现 PaperPosition 和 PaperTrade 数据模型  
4.3 实现买入/卖出逻辑 (含 0.5% 滑点)  
4.4 实现持仓查询和盈亏计算  
4.5 编写单元测试 `tests/v4/test_paper_trader.py`  
4.6 集成测试：完整买入→持仓→卖出流程

**验收标准**:
- [ ] 买入时价格上浮 0.5%
- [ ] 卖出时价格下浮 0.5%
- [ ] 持仓盈亏计算准确
- [ ] 交易记录完整保存
- [ ] 支持多股票独立持仓

---

### Task 5: 实现 AutoExitManager (自动退出管理器)
**预计时间**: 25 分钟  
**优先级**: P0

**目标**: 动态止盈止损决策

**子任务**:
5.1 创建 `app/v4/core/exit_manager.py`  
5.2 实现退出策略:
   - 目标止盈 (5%)
   - 止损 (3%)
   - 时间退出 (24 小时)
   - Trailing Stop (可选)
5.3 实现 `check_exit()` API  
5.4 编写单元测试 `tests/v4/test_auto_exit.py`  
5.5 集成测试：模拟不同盈亏场景，验证退出触发

**验收标准**:
- [ ] 收益≥5% 触发止盈
- [ ] 亏损≥3% 触发止损
- [ ] 持仓超 24 小时触发超时退出
- [ ] 退出原因准确记录
- [ ] 单元测试覆盖率>90%

---

### Task 6: 实现 StrategyGraduator (策略晋级器)
**预计时间**: 20 分钟  
**优先级**: P1

**目标**: 绩效评估和晋级通知 (禁用自动切换)

**子任务**:
6.1 创建 `app/v4/core/graduator.py`  
6.2 实现绩效追踪 (连续盈利天数)  
6.3 实现晋级条件判断 (连续 3 天>5%)  
6.4 实现钉钉通知 (达到晋级条件时)  
6.5 实现人工切换 API (用户手动确认)  
6.6 编写单元测试 `tests/v4/test_graduator.py`

**验收标准**:
- [ ] 准确追踪连续盈利天数
- [ ] 达到晋级条件时发送钉钉通知
- [ ] 不自动切换模式 (等待人工确认)
- [ ] 提供 API 供用户手动切换
- [ ] 晋级历史完整记录

---

### Task 7: 实现 DecisionEngine (决策引擎)
**预计时间**: 30 分钟  
**优先级**: P0 (核心模块)

**目标**: 统一决策入口，整合所有限流/时段/执行模块

**子任务**:
7.1 创建 `app/v4/core/decision_engine.py`  
7.2 实现 `process_signal()` 主流程  
7.3 集成 RateLimiter, SessionManager, PaperTrader, AutoExitManager  
7.4 实现 TradingMode 路由 (PAPER/HYBRID/LIVE)  
7.5 编写单元测试 `tests/v4/test_decision_engine.py`  
7.6 集成测试：完整信号处理流程

**验收标准**:
- [ ] 信号按正确顺序处理 (限流→时段→执行→退出)
- [ ] 不同模式正确路由
- [ ] 异常信号正确处理 (拒绝/延迟)
- [ ] 单元测试覆盖率>85%

---

### Task 8: 实现 ReportGenerator (报告生成器)
**预计时间**: 30 分钟  
**优先级**: P1

**目标**: 生成每日/每周交易报告并推送钉钉

**子任务**:
8.1 创建 `app/v4/report/report_generator.py`  
8.2 实现日报生成逻辑 (交易明细 + 收益)  
8.3 实现周报生成逻辑 (周汇总 + 胜率 + 夏普比率)  
8.4 实现钉钉推送 (使用现有 dingtalk 模块)  
8.5 编写单元测试 `tests/v4/test_report_generator.py`  
8.6 手动测试：生成样例报告，验证格式

**验收标准**:
- [ ] 日报包含当日交易明细和收益
- [ ] 周报包含周收益率、胜率、最大回撤
- [ ] 钉钉消息格式正确 (Markdown)
- [ ] 支持多股票汇总
- [ ] 推送失败有重试机制

---

### Task 9: 实现定时任务调度器
**预计时间**: 20 分钟  
**优先级**: P1

**目标**: 配置日报/周报/绩效检查的定时任务

**子任务**:
9.1 创建 `app/v4/scheduler/trading_scheduler.py`  
9.2 配置港股日报 (16:30 HKT)  
9.3 配置美股日报 (04:30 EST)  
9.4 配置周报 (周日晚 20:00)  
9.5 配置绩效检查 (每小时)  
9.6 集成现有 cron 系统  
9.7 编写测试 `tests/v4/test_scheduler.py`

**验收标准**:
- [ ] 所有定时任务正确配置
- [ ] 时区转换正确
- [ ] 任务失败有日志记录
- [ ] 支持手动触发 (用于测试)

---

### Task 10: 创建配置文件和 API 端点
**预计时间**: 20 分钟  
**优先级**: P1

**目标**: 提供配置管理和外部 API

**子任务**:
10.1 创建 `config/trading_decision.yaml`  
10.2 创建 `app/v4/api/decision_api.py` - Flask 路由  
10.3 实现 API 端点:
   - `POST /api/v2/decision/process` - 处理信号
   - `GET /api/v2/positions/{code}` - 查询持仓
   - `GET /api/v2/performance/{code}` - 查询绩效
   - `POST /api/v2/mode/{code}` - 切换模式 (人工确认)
10.4 编写 API 测试 `tests/v4/test_decision_api.py`

**验收标准**:
- [ ] 配置文件可正确加载
- [ ] 所有 API 端点可用
- [ ] API 认证和限流 (复用现有)
- [ ] Swagger 文档 (可选)

---

### Task 11: 集成测试和回测验证
**预计时间**: 40 分钟  
**优先级**: P0 (发布前必须)

**目标**: 验证整个系统功能和收益目标可行性

**子任务**:
11.1 创建 `tests/v4/test_integration_trading.py` - 端到端集成测试  
11.2 使用历史数据回测 (腾讯 + MU, 3 个月)  
11.3 验证 5% 收益目标可行性  
11.4 优化参数 (如需要)  
11.5 性能测试 (并发信号处理)

**验收标准**:
- [ ] 集成测试通过率 100%
- [ ] 回测显示 5% 目标可达
- [ ] 并发处理无死锁
- [ ] 单次信号处理延迟<500ms

---

### Task 12: 部署和文档
**预计时间**: 20 分钟  
**优先级**: P1

**目标**: 部署到生产环境并编写使用文档

**子任务**:
12.1 创建部署脚本 `scripts/deploy_trading_engine.sh`  
12.2 更新 `.env.example` 添加新配置项  
12.3 编写用户文档 `docs/trading_decision_user_guide.md`  
12.4 编写运维文档 `docs/trading_decision_ops_guide.md`  
12.5 更新 README.md 添加新功能说明

**验收标准**:
- [ ] 部署脚本可一键部署
- [ ] 用户文档清晰易懂
- [ ] 运维文档包含故障排查
- [ ] 所有文档已提交 git

---

## 执行顺序

```
Phase 1 (基础): Task 1 → Task 2 → Task 3
Phase 2 (核心): Task 4 → Task 5 → Task 7
Phase 3 (辅助): Task 6 → Task 8 → Task 9 → Task 10
Phase 4 (验证): Task 11 → Task 12
```

---

## Subagent 分配建议

| Subagent | 任务 | 技能要求 |
|----------|------|----------|
| Implementer-1 | Task 1, 2, 3 | 数据库，限流算法 |
| Implementer-2 | Task 4, 5, 7 | 交易逻辑，核心引擎 |
| Implementer-3 | Task 6, 8, 9 | 报告生成，定时任务 |
| Implementer-4 | Task 10, 11, 12 | API, 测试，部署 |
| Reviewer-Spec | 所有任务 | 验证代码符合设计 |
| Reviewer-Quality | 所有任务 | 代码质量检查 |

---

## 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 回测显示 5% 目标不可达 | 调整参数或延长模拟期 |
| 定时任务时区错误 | 充分测试边界情况 |
| 钉钉推送失败 | 添加重试机制和备用通道 |
| 数据库锁死 | 使用 WAL 模式，添加超时 |

---

## 完成定义 (DoD)

- [ ] 所有任务完成并通过验收
- [ ] 所有单元测试通过率 100%
- [ ] 集成测试通过率 100%
- [ ] 回测验证 5% 目标可行
- [ ] 文档完整
- [ ] 代码已提交 git
- [ ] 生产环境部署完成

---

**下一步**: 用户确认计划后，启动 Subagent 执行 Task 1
