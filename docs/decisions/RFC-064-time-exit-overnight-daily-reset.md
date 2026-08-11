# RFC-064: TIME_EXIT 隔夜每日重置 (语义B)

> **状态**: 已实施
> **日期**: 2026-08-11
> **作者**: Qoder
> **审批人**: 项目所有者 (用户裁决语义B)

---

## 摘要

TIME_EXIT 判决时钟对隔夜仓从 wall-clock 改为"今日 9:30 ET 开盘以来分钟数"，每个交易日预算清零重算；日内单行为完全不变。修复隔夜仓开盘第一轮检查即被 TIME_EXIT 秒杀的设计缺口。

---

## 问题陈述

**实锤案例 (trade id=1246, US.TSM LONG, playbook_sim)**:
- 8/10 12:41:48 ET 入场 @421.155 (当日第二笔 TSM 再入场单， 经 8/6 用户裁决"再入场水下仓首夜豁免"持夜)
- 8/11 09:31:00 ET — 开盘后第一轮退出检查即 TIME_EXIT @420.91 (水下仅 0.06%, 实亏 -$3.18)
- 盘前趋势完好: MFE +0.558% (峰值 423.5, 到峰时点 09:29), 标的隔夜+盘前走强

**根因 — 两个钟口径不一致**:
- 判决钟 `held_minutes` 是 wall-clock (pipeline.py:6385, `(now-entry).total_seconds()/60`), 隔夜 ~17.5h 非交易时间全额计入 → 9:31 时 held=1249min ≫ default `time_exit_hours=6h` → 任意微亏即触发
- 显示钟 `holding_hours` 用 `get_market_hours_us()` 只算交易时段 (3.3h) — 通知口径与判决口径不一致, 误导复盘

**设计缺口本质**: 6h 阈值是按日内样本实证的 (日内 wall-clock ≈ 交易时间), 隔夜仓一隔夜即"超龄", 与隔夜豁免 (RFC-061/8-6 裁决) 的意图自相矛盾——给了持夜的权利, 却没给次日表态的时间。

---

## 提议方案

### 方案概述
语义B (用户裁决): 隔夜仓 (entry ET 日期 < 今日) 的 TIME_EXIT 时钟 = `minutes_from_open` (今日 9:30 ET 以来分钟数), 每日开盘预算清零重算。

### 详细设计
- `app/playbook/pipeline.py` `_check_playbook_exit_rules` 规则4:
  - `_te_held_minutes = held_minutes`; 若 `time_exit_overnight_daily_reset_enabled` (默认 true) 且 `_is_overnight_position(entry_time_str)` → 改用 `float(minutes_from_open or 0)`
  - 盘前 `minutes_from_open < 0` → 天然不触发; 日内单 `_is_overnight_position=False` → 走原 wall-clock, 零变化
- `config/unified_config.yaml`: 新增 `time_exit_overnight_daily_reset_enabled: true` (playbook 退出开关块)
- 复用既有件: `_is_overnight_position` (RFC-061 #6, pipeline.py:428), `minutes_from_open` (调用方已传, ET 口径)

### 不改动的部分 (显式排除)
- DEAD_SIGNAL_EXIT (60min) / MFE_STALL_EXIT (150min) / 早期紧SL / grace — 这些是"入场后 N 分钟须表态"的检查点规则, MFE 为持仓期累计值, 重置会破坏其证据基础; 它们对隔夜仓的开盘跳档问题留作后续观察项 (与 RFC-063 同类)
- `holding_hours` 显示口径 (交易时间钟) 不动 — 判决钟重置后, 通知"持有 Xh"仍显示累计交易时间, 语义为"敞口时长", 不再与 TIME_EXIT 预算直接对应

### 影响范围
- 服务焦点: F1 (退出引擎)
- 改动指标: 隔夜仓开盘后 30min 内 TIME_EXIT 笔数 → 0; TIME_EXIT 触发时点从开盘挪到午后 (6h 预算 ≈ 当日 15:30, 与 PRE_CLOSE_EXIT 15:52 衔接)
- 日内单: 零行为变化 (实证阈值基础直接继承)

---

## 风险评估

### 最坏情况
隔夜方向错单多持有一个上午, 多亏开盘后的 drift。但有界: SL/灾难兜底 (冷却期 4%) 不受影响; cohort B 跳空 >3% 有 OVERNIGHT_GAP_STOP; `time_exit_only_if_loss` 不变 (盈利仓本就不砍)。
"震荡亏损单靠每日重置无限续命"风险有界: cohort A 当日 15:52 PRE_CLOSE_EXIT 兜底; cohort B 受 max_hold_days 约束。

### 缓解措施
- 配置开关秒级回滚 (改 false 即恢复 wall-clock), 无需 revert 代码
- 仅规则4单点改动, if/elif 优先级链不变
- 3 个新测试用例钉死: ①隔夜开盘不触发 ②当日预算耗尽仍触发 ③日内单不变

---

## 验证计划

```bash
1. venv/bin/python test_playbook_exit_rules.py  # 8 例全过 (含 RFC-064 新增 3 例)
2. venv/bin/python -m pytest tests/regression/test_exit_rules.py tests/test_pipeline_horizon.py -q  # 9 passed
3. 实盘判分 (OOS): 本周隔夜仓开盘 30min 内 TIME_EXIT 笔数 → 0;
   被豁免单当日结局 vs 开盘价反事实差 (exit_scorecard / 周判)
```

## 回滚计划

```bash
# config/unified_config.yaml: time_exit_overnight_daily_reset_enabled 改 false (无需 revert)
```

**回滚触发条件**: 隔夜仓上午时段亏损显著扩大 (被豁免单当日净结局系统性劣于开盘价反事实), 或出现预期外规则交互。

---

## 替代方案

### 方案 A: 累计交易时间钟 (get_market_hours_us)
未选: 隔夜仓仍带着昨日已耗预算 (TSM 案例 3.3h/6h, 当日 12:12 即超龄), 与"隔夜后重新表态"的裁决意图不符; 且不跳节假日。

### 方案 C: 开盘 grace (隔夜仓开盘后 N 分钟不触发 TIME_EXIT)
未选: 补丁式, 只挡开盘 N 分钟, 午后仍按被隔夜污染的 wall-clock 触发, 未解决根本口径问题。

---

## 审批

- [x] 5 问自检完成
- [x] 测试覆盖完成 (test_playbook_exit_rules.py +3 例)
- [x] 回滚 playbook 创建 (配置开关, 见上)
- [x] 审批人签字 (用户裁决语义B, 2026-08-11)

---

## 实施记录

**实施日期**: 2026-08-11
**Commit**: `c74661f3` (8/11 盘后落盘包, 含本 RFC + 极端gap作废 + JD盘前锚价修复; 当日 23:18 CST 重启生效)
**验证结果**: 通过 — test_playbook_exit_rules.py 8/8; regression+horizon 9/9
