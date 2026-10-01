# RFC-122: 组合门写入方作用域与组合视野影子

> **状态**: 阶段 0 已落地 (record-only; 2026-10-01 修订为活读 DB); 阶段 1 待前向影子期与用户二次裁决
> **日期**: 2026-10-01
> **类型**: L2 行为变更前置提案; 本 RFC 不实施交易行为变更
> **关联**: RFC-121、DEB-20260930-skew-gate-vision-gap

---

## 摘要

方向偏斜与 sector 集中度门声称度量"组合级"风险, 但唯一调用点位于 `_build_instructions_from_votes`, 只覆盖 generate/topup 构建侧; DA skill 直写、UE 与 reentry 不进入该分母。历史库是 UPSERT 后验状态, 不能重建历史判定视野。2026-10-01 实施修订为: 在活体判定点直接读当日 DB, 记录 batch-vision 与 portfolio-vision 双口径; 判定仍使用 batch-vision。事件快照不实施, 仅在需要重建历史事件序列时另行立项。

---

## 问题陈述

- `_apply_portfolio_guardrails` 全仓唯一调用点为 `_build_instructions_from_votes`; generate 与 topup 经过, DA/UE/reentry 不经过。
- 9 月 active 279 条中, DA 128、UE 67、bridge 64、reentry 20; 195 条绕过组合门。
- 9/30 偏斜门日志 86% 只由 bridge 侧 6B/1S 构成, 而当日后验并集为 9B/4S=69%, 触发 AMD/HPE 被砍。
- 9 月 sector 门实际 11 次 skip, 原称零触发不成立; sector 门与偏斜门同患作用域风险。
- `playbook_generate_state.completed_at` 与日志只能定位个别轮次; DB 行的 `updated_at` 会被后续 tracking 覆盖, 不能作为通用事件时间轴。

---

## 提议方案

### 阶段 0: 只读影子 (已落地)

- 在 `_apply_portfolio_guardrails` 判定点双算 `batch-vision` 与 `portfolio-vision`。
- portfolio-vision 活读当日 DB: 过滤终态作废行, 并按 symbol 去重本批候选。
- 影子行 append-only 写入 `data/shadow/skew_gate_vision.jsonl`; 交易判定仍使用 batch-vision。
- DB 读/影子写失败仅告警, 不影响交易判定。
- 该读数是活体判定点视野, 不回填历史; 历史回放仍不得使用后验 DB 伪造事件序列。
- 本批影子只覆盖方向偏斜门, 不覆盖 sector 门。

### 阶段 1: 行为变更

仅在影子期数据满足预注册判据后, 将 generate/topup 判定分母切换为 portfolio-vision, 并提供 `playbook.guardrails.portfolio_vision: shadow|enforce|off` 开关, 默认 `shadow`。注意: 该方案仍不会在 DA/UE/reentry 写入时执行组合门; 后续写入方是否需要同门, 必须在阶段 1 前另行裁决。

---

## 预注册判据

影子期 ≥10 个交易日后同时满足:

1. 组合视野分歧日占比 ≥10%;
2. ~~事件快照缺失率 <1%~~ → **(2026-10-01 阶段0 落地时修订)** 影子行 DB 读失败率 <1%
   (`偏斜门影子双算失败` WARNING 次数 / 判定次数); 事件台账已删, 原判据随之失效;
3. 被组合视野放行且实际成交的人群, 含费净 PnL ≥ 0;
4. 单日 notional 或购买力护栏触顶频率未较现状上升超过 20%。

任一不满足则继续 shadow 或关闭提案; 禁止在影子读数出来后调整既有 80% 偏斜阈值或 sector 阈值。

---

## 风险与缓解

- **最坏情况**: DB 读失败导致 portfolio-vision 漏计, enforce 后误放行。
- **缓解**: DB 读失败 fail-open 回到 batch-vision 并输出 WARNING; 影子期以该 WARNING / 判定次数作为失败率。
- **口径限制**: topup 调用方在 guardrail 后只保存满足补量数量的子集, 影子记录的是决策面组合, 不必然等于最终落库组合; 阶段 1 裁决前必须用落库/成交行复核。
- **覆盖限制**: 后写入的 DA/UE/reentry 行仍绕过组合门; sector 门未进入本影子。
- **回滚**: 阶段 0 删除 emit; 阶段 1 配置改回 `shadow`。

---

## 验证计划

```bash
./venv/bin/python scripts/replay_skew_gate_vision.py
./venv/bin/python -m pytest tests/test_skew_gate_vision_replay.py tests/test_chase_cap_inversion_fix.py -q
```

实施验证:

1. 契约测试 `tests/test_skew_gate_vision_shadow.py` 全绿;
2. 真实 9/30 探针输出 batch 6B/1S=86%、portfolio 8B/4S=67%、divergent=true;
3. 阶段 1 前用影子行关联落库/成交记录, 复核 topup 子集与后写入方造成的口径差。

---

## 实施记录

- **2026-10-01 阶段0 已落地** (commit d769a7beb, 用户令「继续」): 按做减法**精简掉
  原设计的 0a 全量事件台账** — 结构核实 builder 内不 save (`_build_instructions_from_votes`
  2020 `return instructions`, save 在调用方 run_playbook_generate/topup 2405/2646),
  ⇒ guardrail 判定那一刻本批尚未入库, **活读当日 DB 即 point-in-time 真值**
  (8 日实测 DA first_created_at 恒早于 generate completed_at: 12:35-13:00 vs 13:05-13:07 UTC)。
  事件台账只能服务事后历史回放 (已被 tracking 覆盖、不可重建), 对活体判定是多余环节 → 删。
  实现: `_emit_skew_gate_vision_shadow` 判定点双算 batch-vision vs portfolio-vision
  (prior=当日 DB 活跃行, 去重本批 symbol 防 UPSERT 双计 + 过滤 INVALIDATED 终态),
  落 `data/shadow/skew_gate_vision.jsonl` append-only; 判定仍用 batch-vision 零行为变更;
  双层防护 (emit 自吞错 + 调用点保险丝)。契约测试 12 例 + 活体探针 (真 DB 9/30:
  batch 86% 砍 vs portfolio 8B/4S=67% 放, JBL thesis_drift 终态正确剔除, divergent=True)。
- **阶段 0a 事件台账**: 不实施 (上述结构核实证明对活体判定多余; 若未来需要重建
  **历史**某日判定视野, 另行立项, 不阻塞本影子)。
- **阶段 1 (判定切 portfolio-vision)**: 仍待影子期 ≥10 交易日 + 预注册判据 (§预注册判据) +
  用户二次裁决。回滚: 删 `_emit_skew_gate_vision_shadow` 调用 (1 处) 即退回纯 batch-vision。
