---
title: 多LLM真反向与指令冲突解决影子
date: 2026-10-01
status: 阶段0已实施 (record-only, 待影子攒样与用户裁决)
related_code: app/playbook/db.py, app/playbook/conflict_shadow.py, app/playbook/true_reverse_shadow.py, scripts/da_true_reverse_shadow.py
related_docs: RFC-066, RFC-122, DEB-20260910
tags: [DA, UE, 多LLM, 冲突, 去重, 真反向, 影子]
---

# RFC-125: 多LLM真反向与指令冲突解决影子

## 1. 裁决与目标

用户裁定方向: **不要冲突、不要重复；充分利用分析师与独立挑战者的正反向结论**，
并认同唯一身份、唯一权威入口、集中仲裁与冲突矩阵。

本批只实施**阶段0 record-only**:
- 不改变 `save_instruction` 仲裁、不改变 DA 合成、不改变 UE/DA/reentry 供给;
- 不把真反向直接变成 SKIP、降仓或反手;
- 只把身份冲突、通道方向冲突、独立真反向及其反证证据集中落盘。

## 2. 已确认的病灶

1. **一脑两腿**: DA skill 同一候选既直写 DB 又写 bridge; generate 后续用同一
   `decision_id` 重建并覆盖。非空 `decision_id` 不产生多行，但会产生同身份材料漂移。
2. **门禁借覆盖执行**: conf 门 SKIP 票为空 `decision_id`, 对 DA 直写行是虚门;
   偏斜门保留原 `decision_id` 并原地把 DA 行改为 SKIP。
3. **真反向证据不进指令账本**: 9/23 TSLA 与 9/29 ORCL 均为分析师与独立挑战者
   BUY/SELL_SHORT 真反向, 但二者未进入 bridge 候选/指令行; 原有逻辑只把结论压缩为
   `debate_outcome`, 挑战者反证、失效机制和翻向条件无法被下游稳定消费。

## 3. 阶段0实现

### 3.1 写库边界身份/通道冲突影子

`PlaybookDB.save_instruction` 在读取同日同标的现存行后调用
`emit_save_conflict_shadow()`:
- 同非空 `decision_id` 多行 → `duplicate_decision_id`;
- 同非空 `decision_id` 且 direction/entry/SL/TP/confidence/skip_reason 变化
  → `same_decision_id_material_conflict`;
- DA/UE/reentry/other 活跃行方向相反 → `channel_direction_conflict`;
- 输出 `data/shadow/instruction_conflict.jsonl`, `event_id` 由冲突签名哈希生成并幂等去重。

### 3.2 分析师 vs 独立挑战者真反向影子

`scripts/da_true_reverse_shadow.py` 扫描 `data/da_checkpoint/<date>/*.json`:
- 只把 BUY↔SELL_SHORT 判为真反向; SKIP 是弃权, 不算反向;
- 要求挑战者 `challenger_self_debate=false`;
- `challenger_independent_direction` 与挑战者方向一致才标记
  `independent_true_reverse`;
- 保留挑战者模型、层级、理由、`falsification_target`、`flip_condition`;
- 输出 `data/shadow/da_true_reverse.jsonl` 并按 `event_id` 幂等去重。
- 系统 crontab: `50 20 * * 1-5`, 位于 DA 常规完成后、bridge 推送前;
  DA 超时晚完成时该日可能缺失, 次日运行可幂等补扫但须人工核对。

9月实跑结果: 2 条独立真反向:
- 2026-09-23 `US.TSLA`: 分析师 SELL_SHORT vs 独立挑战者 BUY;
- 2026-09-29 `US.ORCL`: 分析师 BUY vs 独立挑战者 SELL_SHORT。

## 4. 目标冲突矩阵 (未实施 enforcement)

| 冲突 | 阶段0行为 | 未来候选规则 |
|---|---|---|
| 同 `decision_id` + 同材料 | 不记录 | 原行 no-op, 返回原 id |
| 同 `decision_id` + 材料变化 | 记录 `same_decision_id_material_conflict` | 拒绝覆盖或追加新事件; 原始计划不可变 |
| DA vs UE 同日同标的 | 记录方向冲突 (如有) | DA 深度行胜出, UE 反向结论只进影子/裁决, 不覆盖 |
| reentry | 新 `decision_id` 新行 | 维持 RFC-066 多轮共存 |
| 独立真反向 score_gap>3 | 记录 | 维持现有 DA SKIP 语义 |
| 独立真反向 score_gap≤3 | 记录 + 建议裁决 | 候选规则: no-trade pending independent adjudication, 不自动反手 |
| 非独立/锚定反向 | 记录 | 只作挑战者质量观测, 不当独立反向证据 |

## 5. 预注册判据

阶段1 enforcement 前须同时满足:
1. 影子连续 **≥5 个交易日** 正常落盘;
2. 非空 `decision_id` 多行数恒为 0;
3. checkpoint 扫描与落盘事件的独立真反向数量一致;
4. 影子写失败率 <1%, 且失败不阻断写库/交易路径;
5. 用户显式裁决启用 enforcement。

## 6. 回滚

- 删除 `app/playbook/db.py` 中 `emit_save_conflict_shadow` 调用即退回无写库影子;
- 停止运行/删除 `scripts/da_true_reverse_shadow.py`;
- 删除两个新模块与本 RFC;
- `data/shadow/*.jsonl` 为只读产物, 可保留复盘或直接删除。

## 7. 验证

```bash
PYTHONPATH=. venv/bin/python -m pytest \
  tests/test_true_reverse_shadow.py \
  tests/test_instruction_conflict_shadow.py \
  tests/playbook/test_generate_da_priority_contract.py \
  tests/test_playbook_db.py::TestRFC066MultiRoundLedger \
  tests/test_stale_bridge_guard.py -q
```

结果: **117 passed** (含控制环路台账守卫 10 例);
服务重启后启动冒烟七项全绿。
