# FTSeek-US 美股交易项目包
## 打包日期: 2026-05-16

## ⚠️ 安全提示
- `.env.us` 已脱敏，需手动填入真实密钥
- 使用 `/tmp/ftseek-us-extras/.env.us.sanitized` 作为模板

## 📦 内容概览
- `ftseek-us-package.tar.gz` - 主项目代码+数据 (约30MB压缩后)

## 📊 关键交易数据库
- `data/ftseek_us.db` (4.8MB) — signals, positions, trade_records, performance_stats, stoploss_events, position_snapshots, capital_state
- `data/decisions.db` — Agent决策记录 (votes, arbitration, trade_outcomes)
- `data/rejections.db` — 信号拒绝记录
- `data/trade_records.db` — 交易记录
- `data/trade.db` — 交易日志
- `data/orders.db` — 订单记录
- `data/positions.db` — 持仓记录

## 🏗️ 架构
- 独立管线: Planner → Gate → Interceptor
- RiskStateMachine V2 + 幂等拦截器
- 多模型投票系统 (voting_coordinator + model_agent)
- PositionExitMonitor (退出监控)
- CircuitBreaker (熔断器)
- 当前状态: TRADE_ENV=1 (模拟盘)

## 🔧 Agent优化建议方向
1. **信号过滤优化**: CONSENSUS_LOW是最大拒绝原因，需要优化投票阈值或模型权重
2. **退出策略**: 参考 `scripts/exit_reason_pnl_report.py` 和 `reports/exit_reason_pnl_test.md`
3. **多策略架构**: 参考 `docs/MULTI_STRATEGY_ARCHITECTURE.md`
4. **LLM集成**: 参考 `docs/LLM_AUDIT_AND_ENHANCEMENT_2026-05-14.md`
5. **Shadow对比**: 参考 `docs/SHADOW_REVIEW_2026-05-15.md`
6. **P1修复**: 参考 `docs/P1_DEEP_FIXES_2026-05-15.md`
7. **Scalping策略**: 参考 `scalping.md`
8. **Optimizer**: 参考 `OPTIMIZATION_REPORT_2026-05-12.md`

## 📁 解压后结构
```
FTSeek-US/
├── app/           # 353 Python文件，核心交易逻辑
├── config/        # unified_config.yaml (SSOT)
├── data/          # SQLite数据库 + JSON状态文件
├── scripts/       # 运维/诊断/审计脚本
├── tests/         # 测试套件
├── docs/          # 架构文档和审计报告
├── logs/          # 运行日志 (已截断至最近5000行)
├── analysis/      # 分析文件
├── reports/       # 报告
├── obsidian_vault/ # 知识库
├── main.py        # 入口
├── CLAUDE.md      # Agent工作指南
└── ...
```
