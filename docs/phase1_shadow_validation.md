# Phase 1: Shadow 验证计划

## 时间：2026-06-19 ~ 2026-07-03（2周）

### 目标
验证 INSIDER_CLUSTER_BUY 策略在真实市场环境下的信号质量

### 验证指标

#### 1. 信号生成能力
- [ ] 每日扫描覆盖标的数（目标：38只）
- [ ] 信号生成频率（目标：≥2个信号/周）
- [ ] 信号分布（LONG/SHORT 比例）

#### 2. 信号质量（核心指标）
- [ ] 胜率 Win Rate（目标：≥55%）
- [ ] 平均收益 Avg Return（目标：>+0.5%）
- [ ] 盈亏比 Profit Factor（目标：>1.5）
- [ ] 最大回撤 Max Drawdown（目标：<5%）

#### 3. 执行可行性
- [ ] 信号到执行的延迟（目标：<30秒）
- [ ] 滑点影响（目标：<0.2%）
- [ ] 流动性检查通过率（目标：100%）

### 监控方法

#### 每日检查（Cron Job）
```bash
# 每日 ET 18:00 生成报告
python3 scripts/insider_signal_monitor.py --report daily
```

#### 关键日志标记
```
[InsiderScan] 🟢 集群买入信号: {symbol} {buyers}人 ${amount}
[InsiderScan] ✅ 信号质量评分: {score}
[InsiderScan] 📊 Shadow 记录: {signal_id}
```

### 成功标准
- ✅ 胜率 ≥55% AND 盈亏比 ≥1.5 → 进入 Phase 2
- ⚠️ 胜率 45-55% OR 盈亏比 1.0-1.5 → 延长验证 1 周
- ❌ 胜率 <45% OR 盈亏比 <1.0 → 调整参数或放弃

### 数据记录
- 信号日志：`logs/insider_signals.jsonl`
- 每日报告：`data/reports/insider_daily_{date}.json`
- 累积统计：`data/stats/insider_cumulative.json`

### 风险控制
- Shadow 模式不实际下单
- 所有信号仅记录和回测
- 异常信号立即告警（飞书通知）

## 当前状态
- [x] Phase 0 完成（3项修复）
- [x] 集成链路验证通过
- [ ] 设置监控 Cron Job
- [ ] 启动 Shadow 验证
- [ ] 2周后评估
