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

- `_apply_portfolio_guardrails` 全仓唯一调用点为 `_build_instructions_from_votes`; generate 与 topup 经过, DA/UE/reentry 直写不经过 (门的真实杀伤机制见附录 B: 偏斜门经同 did 覆盖**间接**改写直写行, conf 门对直写行是虚门)。
- 9 月 active 279 条中, DA 128、UE 67、bridge 64、reentry 20; 195 条绕过组合门。(2026-10-01 按 did 权威归属复核: DA-255 / UE-81 / reentry-20 / 空did-1197, 空 did 主体为 vote_none 占位行与 conf 门 SKIP 票; 原 "bridge 64" 实为 DA 同批候选的生成侧重建行, 非独立供给 — 见附录 A。)
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

## 附录: 供给链路全景与覆盖仲裁机制 (2026-10-01 实证补充)

> 阶段 1 裁决前必读背景。以下每条均有代码行号 + 9/30 DB/bridge 文件实证。

### A. 一脑两腿: DA 辩论结果的两个出口

DA skill 每晚盘前管线 (crontab ET 20:00/21:10/21:40/22:40 前首跑) 对每只候选做
**完整双模型多空辩论** (分析师 qwen3.7-max + 挑战者 deepseek-v4.1-flash, 失败换家族
kimi-k2.6 → glm-5.2, 同模型自辩显式标记且不升档; llm_synth.py:2202-2224)。
`_write_bridge_file` (skill pipeline.py:1749) **同一循环**对每只过质量门的候选:

1. **直写 DB** (`save_instruction`, source=deep_analysis, 带完整归因字段) — 2026-07-09
   P0 修复加入, 目的是绕过 generate 幂等检查让 auto_execute 直接消费;
2. **写 bridge 文件** (`data/opportunity_scan_<date>.json`) — FTSeek
   `_try_load_bridge` (pipeline.py:1200) 读取, `BridgeVotingAdapter.vote` 消费。

⇒ 同一场辩论的同一结论**入库两次**: 早晨直写一次 (did=DA-…), 盘中 generate 轮
经 bridge 重建再一次。9/30 实证: bridge 13 候选的 decision_id 与 DB 行逐一相等。

### B. 覆盖仲裁决定门的真实杀伤力 (db.py:470-566, RFC-066)

generate 重建批过 conf 门/偏斜门后, 产物经 `save_instruction` 仲裁:

| 门产物 | decision_id | 仲裁结果 | 对已存在 DA 直写行的效果 |
|---|---|---|---|
| conf 门 SKIP (pipeline.py:1918) | **空** (构造时未透传) | 阶梯②: 空 did 遇有源行 → **拒绝写入** | **无** — 低 conf DA 行照常存活执行 |
| 偏斜门 victim (_trim_majority_direction:2311) | **保留原 did** (原地改 Instruction 对象) | 阶梯①: 同 did → **原地覆盖** | **真杀** — direction 改 SKIP 落库 |

9/30 实证: COIN/SNDK/ARM (conf=35 < 55) 直写行未被 conf 门覆盖, exec=1 照常成交;
AMD/HPE (conf=70) 直写行在 20:02 UTC generate 轮被偏斜门同 did 原地覆盖为
SKIP (skip_reason=方向偏斜超限)。updated_at 簇 (20:02×58 行, 20:13×11 行) = generate
轮触碰痕迹。

**推论 (修正 2026-10-01 会话中两个早期口径)**:
- "conf 门拦 bridge 通道 6 行" — 这 6 行是 **conf 门 SKIP 票自己的空 did 行**
  (无 DA 直写行同 symbol 并存时才落库), 不代表 conf 门真的拦住了低 conf DA 供给;
  对已直写的 DA 行, conf 门**结构性写不进去** (空 did 被仲裁拒)。
- "路径A/路径B 按 symbol 分流" — **不成立**。所有 DA 候选两腿都走; 生死差异来自
  覆盖仲裁, 不来自分流。
- 9 月 77 笔 conf<55 执行单全部来自 DA/UE 的根因即此: conf 门对直写行是**虚门**。

### C. 对本 RFC 的含义

1. 偏斜门的 batch-vision 缺陷比原文更重: 它砍的"批"是重建批, 但杀伤落在**已入库
   的直写行**上 (同 did 覆盖) — 视野错配直接改写生产台账。
2. portfolio-vision 影子的 prior 口径 (活读当日 DB + 去重本批 symbol) 仍然正确:
   本批 symbol 的直写行被去重 (它们正是本批的同一决策), prior 只计 UE/reentry/
   他轮 DA 行。9/30 探针 8B/4S=67% 即此口径。
3. 阶段 1 若 enforce portfolio-vision, 需同时裁决: conf 门空 did SKIP 票是否还应
   落库 (当前会产生与直写行并存的孤儿 SKIP 行, 9 月 6 行); 以及 generate 重建批
   对直写行的同 did 覆盖是否应保留 (它是偏斜门唯一的执行机制)。
4. reentry 通道为确定性规则 (ATR/VWAP 回踩, 无 LLM 辩论), 9 月 20 条执行 13;
   UE 通道与 DA 同款双模型辩论结构。

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
