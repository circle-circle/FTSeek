# RFC-057: 全局 min_hold_minutes_after_entry 入场冷却

> **状态**: 草稿
> **日期**: 2026-06-01
> **作者**: Claude (深度数据驱动 review)
> **审批人**: 项目 owner

---

## 摘要

90 天数据显示 <30min 持仓占总亏损 188% (60 笔亏 -$27,437), 而 30min-1h 持仓盈利 +$14,970 (24 笔)。建议给所有新仓位加 30 分钟"证明期", 期间只允许硬底限止损 (HARD_STOP -15%) 或闪崩 (FLASH_EXIT) 触发, 屏蔽其他敏感退出规则.

---

## 问题陈述

**SQL 证据**:

```
hold_bucket   n      wr_pct   total_pnl
<30min        60     31.7%    -$27,437   ← 亏损主源
30min-1h      24     25.0%    +$14,970   ← 盈利
1-3h          41     41.5%    +$766
3-6h          32     46.9%    +$3,548
6-24h         7      71.4%    +$1,230
```

**根因假设**: 短命交易多数是入场后立即反转的烂信号 (15% 胜率正常情况下), 但当前退出规则太敏感, sl_hit 在 5-10 分钟内触发, 没给信号"反应时间"。30min-1h bucket 反而盈利 — 说明放过这个噪声窗口能提取真信号。

**当前配置**:
- `exit_policy.yaml::exit_monitor.min_hold_minutes: 20` (RFC-055 设的)
- `exit_policy.yaml::pre_close_exit.min_hold_minutes_before_pre_close: 30` (仅盘末)
- 但 sl_hit / STRUCTURE_EXIT / DAY_TRADE / TRAILING_STOP **不受 min_hold_minutes 约束**, 可以 1 分钟内触发

---

## 提议方案

### 方案 A (推荐): exit_policy 新增全局 min_hold 屏蔽
```yaml
exit_policy.yaml:
  global_hold_protection:
    enabled: true
    min_hold_minutes_after_entry: 30       # 入场后 30min 内仅允许硬底限退出
    allowed_during_grace:
      - HARD_STOP                           # -15% 硬底限永远生效
      - FLASH_EXIT                          # 闪崩瞬间逃命
    suppressed_during_grace:                # 这些规则 30min 内被压制
      - sl_hit
      - STRUCTURE_EXIT
      - day_trade_initial_stop
      - trailing_stop                       # trailing 30min 后才激活
      - structure_breach
```

代码改动点: `app/risk/position_exit_monitor.py::scan_positions` 入口加 `_in_grace_period(position)` 闸门.

### 影响范围
- 服务焦点: 退出规则触发时机
- 改动指标: <30min 退出笔数
- 预期变化: <30min 笔数 60 → ~20 (-67%), 减亏 ~$15-20K

---

## 风险评估

### 最坏情况
真正烂信号在 grace period 内继续亏损, HARD_STOP -15% 才平仓 → 单笔最大损失放大 3-5 倍。

### 缓解措施
1. HARD_STOP -15% 兜底永远生效 (yaml 已有)
2. FLASH_EXIT (60s 内 1.0×ATR 不利波动) 永远生效
3. 加 max_grace_loss_pct: -3% (新增, grace 期间一旦 -3% 立即解除保护)

---

## 验证计划

T+7 SQL (n 必须 ≥ 30):
```sql
SELECT 
  CASE WHEN holding_hours < 0.5 THEN '<30min' ELSE '>=30min' END AS h,
  COUNT(*), AVG(realized_pnl > 0) * 100 AS wr,
  SUM(realized_pnl)
FROM trade_records WHERE entry_time >= datetime('now', '-7 days')
GROUP BY h;
```
**期望**: <30min 笔数 < 25%, PnL > -$5,000.

T+1 即时验证:
```bash
grep "[grace_period]" /opt/ftseek/FTSeek-US/logs/ftseek_us.log | head -20
# 应该能看到 grace 期间被压制的退出尝试日志
```

---

## 回滚计划

```yaml
# 单行 yaml 回滚
global_hold_protection:
  enabled: false
```

**回滚触发**: 单笔损失 > $1,000 且 exit_reason = HARD_STOP (说明 grace 包过紧导致大亏)。

---

## 替代方案

### 方案 B: 提高入场置信度阈值
数据显示 conf>=0.65 仍亏损, 提阈值无效 (反直觉发现 §4)。

### 方案 C: 完全禁用 sl_hit
过激, 失去尾部保护。

---

## 审批

- [ ] 5 问自检 (sl_hit 占 38% 是否还有别的根因? — 部分确实信号烂, 但短命窗口是放大器)
- [ ] 数据 n 充分: ✅ 60 笔 > 30
- [ ] HARD_STOP/FLASH 兜底验证
- [ ] 审批人签字
