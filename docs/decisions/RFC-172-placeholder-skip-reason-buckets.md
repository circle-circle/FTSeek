# RFC-172 占位候选 SKIP 文案归因分桶（record-only）

- 状态: 已落地（2026-10-27）
- 类型: L1 可观测性修复（只改 SKIP 文案，零交易行为变更）
- 触发: 用户批准 2026-10-27「去分桶修文案可做」

## 1. 病灶（2026-10-26 实证）

playbook 侧把「bridge 未提名」的所有候选统一写成一条文案
`非当前通道候选(bridge未提名/无方向)`（`app/playbook/pipeline.py` vote_result=None 分支），
把四种完全不同的原因压成同一桶：

| 真实原因 | 10-26 只数 | 处置归属 |
|---|---|---|
| DA 有方向但被 signal_gate 拦（全部为「破位硬买」） | **11** | DA 侧 gate |
| DA 自己给观望/SKIP | 8（另有 1 只未进池） | DA 侧评级 |
| 从未进入 DA 评分（ATR<3% 资格门 / 预算 cut / 不在 DA 自选口径） | 36 | DA 候选生成 |
| DA 侧异常 | 0 | — |

后果不是"少写一句备注"，而是**把排查引到错误的那一层**：上一轮据此把最大漏斗判为
"bridge/通道候选层"，实际 bridge 消费率 100%（19 有方向 → 11 被 gate 拦 → 8 进 bridge →
8 进 DB），真正掐单的是 DA 侧 `signal_gate` 的破位硬买门。归因桶与处置层错配 ⇒ 立在错层的
门永远不触发（同 `shadow_lexicon` 里已记录的"第二个死门"复发模式）。

## 2. 改动

`app/playbook/pipeline.py`（唯一改动文件 + 测试）：
1. `_da_gate_buckets(trade_date)`：读当日 `data/da_gate_decisions.jsonl`，按 symbol 分桶 → 
   `gate:破位硬买` / `gate:其它拦截` / `DA观望` / `DA其它`；按 (date, mtime) 记忆化，一日一次读取；
   文件缺失/解析失败 → `{}`（回退原文案，不阻断生成）。
2. `_placeholder_skip_reason(symbol, buckets)`：分桶命中 → 
   `非当前通道候选(bridge未提名; DA=gate:破位硬买)`；未命中 → **旧文案逐字不变**。
3. **前缀 `非当前通道候选` 保持不变**（Guard-First）：下游 `scripts/observe_nightly_supply.py`
   用 `startswith("非当前通道候选")` 把占位行从"真实供给"里剔除，改前缀会让占位行被算成供给；
   `feishu_push.py` 按 reason 聚合展示，桶变多只是行变多（`test_playbook_feishu_push` 仍绿）。

## 3. 验收

- 新单测 `tests/test_da_gate_bucket_label.py`：分桶正确性 / 跨日隔离 / 文件缺失回退 /
  **前缀契约**与"无归因=旧文案逐字一致"。
- 回归：`test_playbook_feishu_push.py` + `test_da_gate_bucket_label.py` 22 passed；
  `test_playbook_pipeline.py` 74 passed；`test_topup_fallback_adapter.py` + 
  `test_da_candidate_selection.py` 73 passed。
- 真实数据核对（2026-10-26 当日 55 只占位重新打标）：36 旧文案 / **11 破位硬买** / 8 DA观望
  —— 与独立复核的分桶结果逐字一致。

## 4. 回滚

删掉两处调用（回到硬编码文案）或整文件 revert 本 RFC 提交即可；无配置、无落库 schema 变更、
无判据语义变更 ⇒ 不影响任何成交集合与历史行（历史行留旧文案）。

## 5. 遗留

- 历史行（≤2026-10-26）仍是旧文案，不回溯重写（改历史=改口径，需另立 RFC）。
- 「DA 观望」与「资格门剔除」的进一步细分（低 ATR / 预算 cut / 不在自选口径）需要 DA 侧
  漏斗产物 `data/shadow/candidate_funnel.jsonl`，本次未接（跨仓依赖，另议）。