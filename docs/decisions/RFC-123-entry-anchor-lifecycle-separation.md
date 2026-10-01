# RFC-123: 生成锚与执行锚生命周期分离

> **状态**: 提案 — 只登记风险与预注册方案, 本 RFC 不修改交易行为
> **日期**: 2026-10-01
> **类型**: L2 交易行为变更前置提案
> **关联**: RFC-120、RFC-122、DEB-20260930-skew-gate-vision-gap

---

## 摘要

`playbook_instructions.entry_price` 同时承担生成锚与逐轮执行锚: reprice 会把它改写为当前可执行价, 而触发判定继续读取同一列。RFC-120 已在 chase cap 归一化中禁用该列作为生成锚, 但其它触发/入场路径仍存在生命周期混用风险。本 RFC 提议先做双锚影子, 再裁决是否将生成锚独立持久化。

---

## 问题陈述

- reprice 组装 `_reprice_params` 时写入 `entry_price` (`app/playbook/pipeline.py` 的 reprice 块), 使 DB 行的原始入场参考价被逐轮覆盖。
- 触发路径在 `auto_execute` 中继续使用 `entry_price` 判定 pullback/chase/out_of_band (`app/playbook/pipeline.py` 触发块)。
- 这带来两个不同语义:
  - **生成锚**: 指令生成时的计划入场、入场带与确认位;
  - **执行锚**: 本轮 reprice 后的可执行价、SL/TP 与仓位。
- RFC-120 修复 chase cap 时已发现该列不可再作为生成锚, 否则 cap 会跟着追价而失效; 这说明单列复用不是抽象洁癖, 而是已出现的逻辑风险。
- `metadata_json.reprice_audit` 只有审计信息, 不是所有写入方都稳定提供 `raw_instruction_json.planned_entry`, 因此不能假设现有数据可完整回放。

---

## 提议方案

### 阶段 0: 双锚影子

- 每次触发判定记录:
  - `generated_anchor`: 从 `raw_instruction_json.planned_entry`、入场带沿反推或 confirmation 键推导, 推导失败标记 `missing`;
  - `effective_anchor`: 当前 `entry_price`;
  - 两个锚分别计算的 `entry_path` 与 `triggered`;
  - reprice 历史、`instruction_id`、`ts`、`source`。
- 判定仍完全使用现行 `effective_anchor`; 观测写失败不改变交易。
- 影子期输出分歧率、missing 率、分歧导致的触发/不触发分布。

### 阶段 1: 行为分离

- 为生成锚建立明确持久化契约 (专用列或规范化 raw field), 全写入方必须写入。
- 触发/入场带判定读取生成锚; reprice、下单、SL/TP 和仓位计算读取执行锚。
- 增加迁移与兼容策略: 历史行缺生成锚时标记 `legacy_missing`, 不用后验值伪造。

---

## 预注册判据

影子期 ≥10 个交易日:

1. 生成锚缺失率 <1%;
2. 双锚触发判定分歧率 ≥5% 且分歧不是纯数据错误;
3. 分歧样本中按生成锚判定的触发质量 (含费 PnL) 不低于现行锚;
4. 现行护栏触顶频率无异常上升。

满足后另行裁决阶段 1; 读数后不得调整入场带、确认位或触发阈值。

---

## 风险与缓解

- **最坏情况**: 生成锚过旧导致系统长期等待不可达价格, 交易供给下降。
- **缓解**: 双锚影子先量化供给影响; 阶段 1 保留 TTL/reprice/终态机制, 不移除执行锚。
- **兼容风险**: 历史行无法补齐生成锚。
- **缓解**: 显式 `legacy_missing`, 禁止用后验 `entry_price` 反填。

---

## 验证计划

```bash
./venv/bin/python -m pytest tests/test_chase_cap_inversion_fix.py -q
```

实施影子时新增:

1. 同一指令多次 reprice 后, 生成锚保持不变;
2. 触发判定分别输出双锚结果, 且现行行为字节级不变;
3. 缺失生成锚时标记 `missing`, 不回退到后验 `entry_price`。

---

## 实施记录

本 RFC 当前仅登记提案与判据, 未修改交易逻辑。
