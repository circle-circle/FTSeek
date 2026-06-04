# RFC-059: sandbox mode 出口策略评估

> **状态**: 草稿
> **日期**: 2026-06-01
> **作者**: Claude (深度数据驱动 review)
> **审批人**: 项目 owner

---

## 摘要

90 天数据: sandbox 模式 53 笔交易 13.2% 胜率亏损 -$28,184, normal 模式 268 笔 30.6% 胜率盈利 +$13,607。沙盒"加速实验"的初衷与"持续亏损实验场"的现实严重不符. 建议三选一: (A) 关闭 sandbox 重启 normal 主路径; (B) 大幅收紧 sandbox 风控参数; (C) 限定 sandbox 仅纸交易不入实仓.

---

## 问题陈述

**SQL 证据**:
```
mode      n     wr_pct   total_pnl
normal    268   30.6%    +$13,607      ← 主路径正盈利
sandbox   53    13.2%    -$28,184      ← 沙盒纯亏
```

**当前 sandbox 状态** (`config/unified_config.yaml:18-57`):
- `sandbox.enabled: true` (5/15 启用)
- universe 9 只标的 (TSLA AMD RIVN SNDK ARM BWXT MU SPY QQQ)
- overrides:
  - scan_interval: 10→3 min (3.3× 加速)
  - signal_cooldown: 120→30s (4× 加速)
  - MAX_DAILY_DRAWDOWN: 0.03→0.05 (放宽 67%)
  - max_per_sector: 0.40→0.55 (放宽 38%)
  - reversal_seconds: 180→60s (砍 67%)

**结论**: 沙盒**全方位放宽风控**, 加速亏损 — sandbox 设计目的是"实验+学到东西反哺主线", 但 voting_predictions 回填断 (RFC-XXX 修复中), 实验数据没回流, 实验白做且亏钱.

---

## 提议方案

### 方案 A (推荐 - 短期): 暂停 sandbox 直到反馈链路修复
```yaml
mode:
  sandbox:
    enabled: false              # 暂停
    # 重启条件: voting_predictions 回填率 ≥ 80% + 连续 7 天无 BLOCKING 报警
```
**前提**: RFC 修复 voting outcome 回填后才有意义重启.

### 方案 B (中期 - 收紧): 沙盒变小心实验场
```yaml
mode:
  sandbox:
    enabled: true
    overrides:
      MAX_DAILY_DRAWDOWN: 0.02     # 0.05 → 0.02 (比 normal 还严)
      max_per_sector: 0.30         # 0.55 → 0.30 (比 normal 严)
      scan_interval_minutes: 5     # 3 → 5 (适度放慢)
      signal_cooldown: 90          # 30 → 90 (3× 收紧)
    sandbox_loss_circuit_breaker:
      monthly_max_loss: -3000      # 月内累计亏 $3000 自动关闭
```

### 方案 C (彻底 - 隔离): sandbox 只纸交易
```yaml
mode:
  sandbox:
    enabled: true
    paper_only: true             # 不发真实订单, 只记录 shadow_trades
```
真实资金永远不动, 但失去了实战验证。

### 影响范围
- 服务焦点: 实验体系完整性
- 改动指标: 月度净 PnL, 沙盒亏损贡献
- 预期变化: 方案 A 立即减少 -$3K~-$5K/月, 方案 B 减亏但保留实验

---

## 风险评估

### 方案 A 最坏情况
关沙盒 → 主路径无新数据点验证策略改动 → 治理周期变慢

### 方案 A 缓解
保留 `shadow_trades` 表 (shadow_bridge), 用 paper-mode 替代 sandbox-with-real-money.

### 方案 B 最坏情况
收紧后 sandbox 还在亏, 只是亏得慢

### 方案 B 缓解
sandbox_loss_circuit_breaker 月内累计 -$3K 自动关闭 (硬底线)

---

## 验证计划

T+30 SQL (sandbox 改动需要长一点周期):
```sql
SELECT mode, COUNT(*), AVG(realized_pnl > 0) * 100 AS wr, SUM(realized_pnl)
FROM trade_records WHERE entry_time >= datetime('now', '-30 days') GROUP BY mode;
-- 期望 (方案 A): sandbox 笔数 = 0
-- 期望 (方案 B): sandbox 笔数 < 20, PnL > -$2K
```

每日健康脚本会监控:
```bash
python3 scripts/validate_strategy_health.py
# 触发 sandbox_loss_circuit_breaker 阈值时 exit_code=2
```

---

## 回滚计划

```yaml
# 一行回滚
mode.sandbox.enabled: true   # (恢复任何选项前的状态)
```

**回滚触发**: 方案 A 实施后 normal 模式 7 天 PnL < -$5,000 (说明 sandbox 实际有 hedge 效果, 关掉后反而更亏 — 需要重新评估).

---

## 替代方案

### 方案 D: 维持现状不动
**反对**: 沙盒每月持续亏 -$3K~-$5K, 北极星 PnL≥0 不可能达成.

---

## 审批

- [ ] 5 问自检 (沙盒亏损是放大器还是必经之痛? — 数据看是放大器, 净亏)
- [ ] n 充分: ✅ 53 sandbox vs 268 normal
- [ ] 方案 A 与 voting outcome 回填的依赖关系明确
- [ ] 审批人签字

---

## 附注

此 RFC 与 RFC-056 (HIGH_VOL 过滤) / RFC-057 (min_hold) / RFC-058 (LONG 削弱) 是组合拳, 单独实施任意一个边际收益有限。
**优先级建议**: 057 (min_hold, 减<30min 亏损) → 056 (HIGH_VOL filter) → 058 (LONG 折扣) → 059 (sandbox 评估).
