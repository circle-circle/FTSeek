# RFC-061: 盈利 Shadow 仓位过夜持有 A/B 实验

> **状态**: 已实施 (v1, PLAYBOOK_SHADOW)
> **日期**: 2026-06-24
> **作者**: Claude (深度数据驱动 review)
> **审批人**: 项目所有者
> **数据附件**: `docs/ic_report_2026-06-24.md`

---

## 摘要

IC 实测发现信号 T+5 IC=+0.265 (强正) 而 T+1 IC=-0.114 (反预测), alpha 是周级; 但系统平均持仓 2.9h、仅 1 笔超 1 天, EOD 强制全平砍掉周级盈利。本 RFC 对 `PLAYBOOK_SHADOW` 仓位做 A/B: cohort B 盈利仓跳过 EOD 强平、持有到 ~T+5 (7 日历日硬上限), cohort A 维持日内对照。验证 PASS 后 Phase 2 扩 MAIN_PIPELINE。

---

## 问题陈述

**SQL 证据** (ftseek_us.db, 375 笔已平仓):

| 持仓时长 | n | avg_pnl_pct |
|---|---|---|
| <1h | 101 | -7.54% |
| 1-4h | 69 | 0.00% |
| 4-8h | 36 | +1.23% |
| 隔夜<1d | 14 | -0.02% |
| 1-5天 | 1 | -2.98% |

← 平均持仓 2.9h; `EOD_FORCE_CLOSE` 7 笔 +3.62% (方向对但被收盘平掉)

**IC 证据** (signal_snapshots.db, n=1375, Spearman ρ):

| 前瞻 | IC | p | hit |
|---|---|---|---|
| T+1 | -0.114 | <0.001 | 49.2% |
| T+5 | +0.265 | <0.001 | 52.1% |

**根因假设**: 信号是周级 alpha 被装进日内壳。EOD 强平是那堵墙 — 系统吃 T+1 亏损 (入场即反向被止损) + 砍 T+5 盈利 (撑过当天的被收盘平掉)。

**当前配置**:
- `app/playbook/pipeline.py:monitor_playbook_shadow_exits` 规则4: 收盘前 8min `PRE_CLOSE_EXIT` 全平
- `_force_close_orphaned_shadow_positions`: 盘后兜底 `EOD_FORCE_CLOSE` 全平孤儿仓
- 团队 backtest (exit_policy.yaml:23-25): 4-24h 持仓 70.7% WR +$21K 最好, 已在往延长方向挪 (time_exit 1h→2h)

---

## 提议方案

### 方案 A (推荐, 已实施 v1): PLAYBOOK_SHADOW cohort B 盈利仓过夜

**Cohort 分配** — `app/storage/trade_store.py:save_trade()` 统一注入:
```python
def assign_cohort(symbol, trade_date):
    if not symbol or not trade_date: return "A"
    return "B" if (hash(f"{symbol}-{trade_date}") % 2 == 1) else "A"
# save_trade 内: metadata_json["cohort"] = assign_cohort(...)
```
确定性 hash, 幂等; 存 metadata_json (零 schema migration, 用 `json_extract` 查)。

**退出规则** — `monitor_playbook_shadow_exits`:
- 规则4: cohort B + `profit_pct>0` → 跳过 `PRE_CLOSE_EXIT` (持过夜); 否则原行为
- 规则5 (新): cohort B + 持仓 ≥7 日历日 → `MAX_HOLD_T5` 强平
- EOD 兜底: cohort B + 盈利 + <7日 → 跳过; cohort B + ≥7日 → `MAX_HOLD_T5`; 亏损仓仍兜底

**保留约束** (不变): ATR 止损 (规则1, 跨日有效) + breakeven ratchet (盈利≥1.5%→SL 移至 entry) + overnight_loss_stop_pct 0.03。

### 影响范围
- 服务焦点: F3 (退出执行)
- 改动指标: PLAYBOOK_SHADOW 持仓时长分布、realized PnL by cohort
- 预期变化: cohort B 持仓中位数 2.9h → 1-5 天; 若假设成立 sum_pnl(B) > sum_pnl(A)

---

## 风险评估

### 最坏情况
隔夜跳空反向吃掉盈利, cohort B 净亏, 且持仓久导致单笔风险敞口放大。

### 缓解措施
1. 仅盈利仓过夜 (亏损仓维持 EOD 兜底, 不放大风险)
2. ATR 止损 + max_loss 2% + overnight 3% 止损仍生效 (硬约束跨日)
3. MAX_HOLD_T5 (7 日历日) 硬上限 + breakeven ratchet 自然了结

---

## 实验设计 (A/B)

- **分组**: `assign_cohort(symbol, trade_date)` hash → A (对照, 日内全平) / B (试验, 盈利过夜)
- **范围**: v1 仅 `PLAYBOOK_SHADOW` (OKR 焦点层, 自有监控, 入场即写 metadata)。ORB/MAIN_PIPELINE/SCALPING 排除 (见范围说明)
- **周期**: 跑满 2 周 或 每组 ≥30 笔
- **主指标**: sum_pnl by cohort, win_rate by cohort, holding 中位数迁移
- **提前终止**: cohort B 单笔亏损 > max_loss 2% ×3 (跳空穿透止损) → 立即回滚

---

## 验证计划

**T+1 即时日志**:
```bash
grep "cohort=B.*跳过 PRE_CLOSE\|MAX_HOLD_T5\|EOD 兜底跳过" logs/*.log
```

**T+7 A/B 度量 SQL** (每组 n≥30 后):
```sql
SELECT json_extract(metadata_json,'$.cohort') cohort,
       COUNT(*) n, ROUND(AVG(pnl_pct)*100,2) avg_pnl,
       ROUND(SUM(realized_pnl),0) sum_pnl,
       SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END)*100.0/COUNT(*) wr_pct,
       ROUND(AVG(holding_hours),1) avg_hold_h
FROM trade_records
WHERE source_type='PLAYBOOK_SHADOW' AND exit_time!=''
GROUP BY json_extract(metadata_json,'$.cohort');
```
按 exit_reason 看 cohort B 的 MAX_HOLD_T5 / SL_ATR_STOP / 隔夜 SL 分布。

**决策门禁**: B sum_pnl > A 且 wr 不降 → PASS → Phase 2 (MAIN_PIPELINE); B 隔夜跳空亏损 → FAIL → 回滚; 不显著 → 延长 4 周。

---

## 回滚计划

`pipeline.py` 三处 cohort 分支删除即恢复原 EOD 全平 (cohort 字段留在 metadata 无副作用)。
触发条件: cohort B 单笔亏损 > $1,000 且 exit_reason ∈ {SL_ATR_STOP, MAX_HOLD_T5} 连续 3 笔。

---

## 替代方案

### 方案 B: 维持日内, 修信号让它有日内 edge
不选 — IC 证明日内根本没 edge (T+1 反预测), 逆数据。
### 方案 C: 全 source_type 一步到位过夜
不选 — ORB 纯日内语义不成立; MAIN_PIPELINE 需改 entry metadata + dispatcher max-hold, 风险大。先 PLAYBOOK_SHADOW 验证假设再推广。

---

## 审批
- [x] 5 问自检完成 (数据先行 / 回滚明确 / 风险缓解 / 范围隔离 / 决策门禁)
- [x] 测试覆盖完成 (assign_cohort 确定性 + dry-run monitor)
- [x] 回滚 playbook 创建
- [ ] 审批人签字 (待 A/B 结果)

---

## 实施记录
**实施日期**: 2026-06-24
**Commit**: `<待提交>`
**v1 范围**: PLAYBOOK_SHADOW only
**验证结果**: 待 A/B 数据积累 (2 周 / 每组 ≥30 笔)
