# RFC-063: 开盘禁触窗 + 死信号退出 + 盘后净口径统计

- 日期: 2026-08-08
- 状态: 已批准 (用户"落地"指令)
- 类型: L1 功能修复 + L4 配置变更
- 依据: DEB-20260807 盘后复盘 (trade_records 全量核验)

## 背景

8/7 复盘发现三个问题:

1. **报告口径误导**: cron 盘后报告胜率用毛口径 (realized_pnl>0), 今日报 75%,
   净口径 (pnl_net>0) 实际 37.5%。近7日 64% vs 实际 38.2%。
2. **MFE_STALL 系统性失血**: 7月 17笔 + 8月 12笔 = 29笔 0% 胜率 -$1,229,
   全部 9:35-9:53 ET 开盘噪声期入场, 死扛 2.5h 后 MAE 放大到 1.5-4% 才砍。
3. **入场时段期望分化**: 9点档(开盘首小时) 7月 120笔 -$931 (93%亏损) / 8月 35笔 -$250;
   10点档 7月 42笔 +$132 / 8月 7笔 +$42 — 开盘噪声期入场期望为负。

## 变更

### 1. 盘后审计净口径统一 (post_market_audit.py)

全部归因 SQL (策略/信号/退出/环境/趋势/方向/shadow) 的 wins/total_pnl/avg_pnl/
best/worst 从 realized_pnl 统一为 COALESCE(pnl_net, realized_pnl);
shadow_trades 无 pnl_net 列, 用 realized_pnl - COALESCE(commission, 0)。
报告标签统一"净胜率"。cron prompt 强制口径纪律。

### 2. 开盘禁触窗 (pipeline.py + config)

- 新增: `entry_open_blackout_enabled: true`, `entry_open_blackout_hhmm: 1000`
- ET < 10:00 不触发新单 (与既有 14:00 cutoff 对称, fail-open 异常处理)
- 依据: 9点档负期望两月连续验证; MFE_STALL 12/12 全在窗口内入场
- 回滚: `entry_open_blackout_enabled: false`

### 3. 死信号退出 规则3.3 (pipeline.py + config)

- 新增: `dead_signal_exit_enabled: true`, `dead_signal_exit_minutes: 60`,
  `dead_signal_exit_mfe_pct: 0.2`
- 持仓 ≥60min 且峰值浮盈从未达 0.2% → DEAD_SIGNAL_EXIT 立即离场
  (不要求当前水下, 比 MFE_STALL 更早承认信号失效)
- 依据: exit_shadow dead_signal_60m_mfe0.2 shadow 毕业 —
  全历史 171 笔, 触发组 mean -1.12% vs 未触发组 +0.37%, 分离度 1.49pp (PASS)
- DEAD_SIGNAL_EXIT 加入 intraday_reentry._HARD_STOP_REASONS (0%WR 禁再入)
- 回滚: `dead_signal_exit_enabled: false`

## 验证

- py_compile 全通过
- test_playbook_exit_rules.py 全绿 (含新规则断言: DEAD_SIGNAL 接管 60min 死单,
  MFE_STALL 仍接管 peak 0.3% 深亏单, 盈利仓零质量损失)
- 配置加载验证: 4 个新键 get_playbook_config 读到
- post_market_audit.py 实跑: 今日净胜率 38% (毛 75% 不再出现)

## 风险与观察

- 开盘禁触窗会错过 9:36 入场的 LLY 类赢家 (8/7 LLY +$95/GS +$15 均 9:36 入场),
  但 9点档整体期望 -$7.1/笔, 10点档两月正期望是更稳的统计
- 死信号退出 60min 阈值基于 shadow 验证, 用户原方案 45min/0.3% 更激进,
  以 shadow 验证过的 60min/0.2% 实单化, 45min/0.3% 留 shadow 继续验证
- 观察期 2 周: 统计 DEAD_SIGNAL_EXIT 触发笔数/挽回损失 vs 误杀 (慢热赢家),
  MFE_STALL 笔数应显著下降