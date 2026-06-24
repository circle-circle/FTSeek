# RFC-061: 盈利 Shadow 仓位过夜持有 A/B 实验

> **状态**: 已实施 (v2, PLAYBOOK_SHADOW/SIM/LIVE + MAIN_PIPELINE)
> **日期**: 2026-06-24
> **作者**: Claude (深度数据驱动 review)
> **审批人**: 项目所有者
> **数据附件**: `docs/ic_report_2026-06-24.md`

---

## 摘要

IC 实测发现信号 T+5 IC=+0.265 (强正) 而 T+1 IC=-0.114 (反预测), alpha 是周级; 但系统平均持仓 2.9h、仅 1 笔超 1 天, EOD 强制全平砍掉周级盈利。本 RFC 做 A/B: cohort B 盈利仓跳过 EOD 强平、持有到 ~T+5, cohort A 维持日内对照。

**v2 (2026-06-24) 5 项改进**:
1. **扩到 MAIN_PIPELINE** (IC 实测群体、有吞吐量) — 入场注入 cohort 到 position.metadata, pre_close.py 两函数读 cohort 跳过, position_exit_monitor 调 check_max_hold_t5
2. **LIVE 独立门禁** — `cohort_b_skip_allowed(source_type)`: PLAYBOOK_LIVE 默认不跳过 (须 env `RFC061_LIVE_OVERNIGHT_ENABLED=1`), 不从 sim 自动继承
3. **SL 优先级修复** — sim monitor 改 SL-first (对齐 shadow, 修原 PRE_CLOSE>TIME>SL 掩盖 SL 的 quirk)
4. **MAX_HOLD 用交易日历** — `nth_trading_day_after("US", entry, 5)` 5 交易日 (遇 Juneteenth 等假期跳过), 替代原 7 日历日
5. **决策门禁细化** — FAIL 区分"隔夜跳空亏 (SL+holding>24h)"vs"信号方向错 (SL+holding<24h)"vs"MAX_HOLD_T5 了结", 各对应不同动作

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

### 方案 A (推荐, 已实施 v1): cohort B 盈利仓过夜 (覆盖 shadow + sim 两条路径)

**Cohort 分配** — `app/storage/trade_store.py:save_trade()` 统一注入 (所有 source_type):
```python
def assign_cohort(symbol, trade_date):
    if not symbol or not trade_date: return "A"
    return "B" if (hash(f"{symbol}-{trade_date}") % 2 == 1) else "A"
# save_trade 内: metadata_json["cohort"] = assign_cohort(...)
```
确定性 hash, 幂等; 存 metadata_json (零 schema migration, 用 `json_extract` 查)。

**退出规则 (4 个监控函数, 同一套 cohort 语义)**:
- `monitor_playbook_shadow_exits` (PLAYBOOK_SHADOW, DB 平仓): cohort B + 盈利 → 跳过 `PRE_CLOSE_EXIT`; ≥7 日 → `MAX_HOLD_T5`; EOD 兜底 `_force_close_orphaned_shadow_positions` 同理
- `monitor_playbook_sim_exits` (PLAYBOOK_SIM/LIVE, **broker 平仓, 活跃路径**): cohort B + 盈利 → 跳过 `PRE_CLOSE_EXIT` (不调 broker.close_position, 持过夜); ≥7 日 → `MAX_HOLD_T5` broker 平; EOD 兜底 `_force_close_orphaned_sim_positions` 同理 (先取报价估盈利, 决定是否跳过 broker close)
- **ORB 守卫**: sim 兜底用 `source_type in ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 限制 cohort 跳过, ORB_ENGINE 即便被注入 cohort 也强制 EOD 平仓 (维持日内语义)

**保留约束** (不变): ATR 止损 (SL_ATR_STOP, 跨日有效, 最高优先级) + breakeven ratchet (盈利≥1.5%→SL 移至 entry) + overnight_loss_stop_pct 0.03。cohort B 亏损仓不跳过 (维持安全兜底)。

### 影响范围
- 服务焦点: F3 (退出执行)
- 改动指标: PLAYBOOK_SIM/LIVE/SHADOW 持仓时长分布、realized PnL by cohort
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
- **范围**: v1 覆盖 `PLAYBOOK_SHADOW` + `PLAYBOOK_SIM/LIVE` (两条 playbook 退出路径, 确保 simulate / futu_simulate 模式下 cohort 都生效)。ORB/MAIN_PIPELINE/SCALPING 排除。
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
WHERE source_type IN ('PLAYBOOK_SHADOW','PLAYBOOK_SIM','PLAYBOOK_LIVE','MAIN_PIPELINE') AND exit_time!=''
GROUP BY json_extract(metadata_json,'$.cohort');
```
**cohort B 退出分类 (v2 改动 5)** — 区分失败模式:
```sql
SELECT
  CASE WHEN exit_reason='MAX_HOLD_T5' THEN 'a_T5了结'
       WHEN exit_reason='SL_ATR_STOP' AND holding_hours>24 THEN 'b_隔夜跳空亏'
       WHEN exit_reason='SL_ATR_STOP' AND holding_hours<=24 THEN 'c_信号方向错'
       ELSE 'd_其他' END AS fail_mode,
  COUNT(*) n, ROUND(AVG(pnl_pct)*100,2) avg_pnl
FROM trade_records
WHERE json_extract(metadata_json,'$.cohort')='B' AND exit_time!=''
GROUP BY fail_mode ORDER BY fail_mode;
```

**决策门禁 (v2 三分支)**:
- **PASS**: B sum_pnl > A 且 wr 不降 → 推广 (MAIN_PIPELINE 已在 v2 覆盖, 下一步考虑放开 LIVE 或调权重)
- **FAIL-隔夜跳空** (b 模式主导): alpha 存在但隔夜风险盖过 → **不放弃持夜**, 动作: 加隔夜对冲 / 选低波标的 / 收紧 overnight_loss_stop
- **FAIL-信号方向错** (c 模式主导): 信号本身无 T+5 alpha → 放弃持夜 / 降权该 source
- **不显著**: 延长到 4 周 / 100 笔

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
**Commit**: `<待提交 v2>`
**v1 范围**: PLAYBOOK_SHADOW + PLAYBOOK_SIM/LIVE (4 个监控函数)
**v2 范围 (2026-06-24)**: + MAIN_PIPELINE (order_executor 入场 cohort + pre_close.py 跳过 + check_max_hold_t5 + position_exit_monitor 调用); LIVE 独立门禁 (cohort_b_skip_allowed); sim monitor SL-first; MAX_HOLD 用交易日历 (nth_trading_day_after); 决策门禁三分支
**验证结果**: assign_cohort/nth_trading_day_after/live_overnight_enabled 单测通过; check_max_hold_t5 隔离测试 (B 6/16→命中/6/23→未到/A→None/LIVE→None); sim force_close v2 隔离测试 (A→EOD / B盈利当天→跳过 / B 8天→MAX_HOLD_T5 / LIVE B→EOD不跳过); 待 A/B 数据积累
**配置前置**: `execute_mode: futu_simulate` (已 commit 07be5325); LIVE 隔夜须 env `RFC061_LIVE_OVERNIGHT_ENABLED=1` (默认关)
