---
title: RFC-206 — DA SKIP 方向—执行计划一致性契约（离线对照先行）
status: offline_contract_pass_with_reachability_warning
date: 2026-11-13
approved_by: 用户「按建议执行」
scope: scripts/da_model_ab_replay.py 隔离回放；无生产 Prompt/指令/订单变更
---

# RFC-206 — DA SKIP 方向—执行计划一致性契约

## 1. 问题与边界

2026-11-12 NVDA/ARM 的 qwen 首轮回放中，卡片 `direction=SKIP`，却仍填写
`entry_archetype=FADE_SHORT/PULLBACK_LIMIT` 及 entry/stop/target。下游虽然会忽略 SKIP，
但这属于指令语义自相矛盾，不能用 JSON 可解析冒充完整合格。

全量 checkpoint 只读扫描进一步确认：现有 **124/124 张 SKIP 卡**均带至少一个方向性执行字段，
覆盖 **21 个分析日**（2026-10-13 至 2026-11-12）。因此这是旧 schema 的系统性矛盾，不是单股、
单模型或单日噪声；但没有证据表明这些字段已造成错误下单，不将其夸大为交易事故。

本 RFC 只验证一条确定性输出契约，不换模型、不加事件信息、不改入场阈值：

> `direction=SKIP` 时，`execution_plan` 的方向性执行字段必须为 null；右侧研究观察只能写入
> 研究文本、invalidation_triggers 或 tripwires，不得同时给出可执行 BUY/SELL_SHORT 计划。

**当前状态**：仅离线探索/对照。生产 `SYSTEM_PROMPT`、USER_CONTRACT、prompt_hash、Playbook、
Gate、仓位、订单、cron 均未改变。首轮 8 标的只用于发现病灶，不是确认性证据。

## 2. 已冻结输入与噪声基线

- 日期：2026-11-12；标的：NVDA、AMD。
- 原始输入：`data/da_checkpoint/2026-11-12/US.{NVDA,AMD}.raw.json`，SHA256 随样本清单落盘。
- 模型：qwen3.7-max；生产 temperature=0、thinking=true、budget=8192、max_tokens=16192。
- 控制臂：原始 Prompt，重复 10 次/标的，产物
  `data/shadow/da_model_ab_nvda_amd_repeat10_v1.jsonl`。
- 实验臂：同一原始 Prompt + RFC §1 契约，重复 10 次/标的，产物
  `data/shadow/da_prompt_skip_contract_nvda_amd_repeat10_v1.jsonl`。
- 除契约外，模型、样本、调用预算、并发与评价代码保持一致；每行保留原始及生效 Prompt SHA256。

控制臂已读基线（在实验臂揭盲前记录）：

| 指标 | NVDA | AMD | 合计 |
|---|---:|---:|---:|
| direction_plan_consistent | 3/10 | 10/10 | 13/20 |
| stable direction | 否（7 SKIP / 3 SELL_SHORT） | 是（10 BUY） | 1/2 标的 |
| valid_card | 10/10 | 10/10 | 20/20 |

## 3. 实验臂揭盲前判据

主要判据（必须全部满足才算 **OFFLINE_CONTRACT_PASS**，不等于生产批准）：

1. 合计 `direction_plan_consistent_rate >= 95%`，且 NVDA 单项 >=90%。
2. `valid_card_rate` 不低于控制臂 -5pp。
3. `research_complete_rate` 不低于控制臂 -5pp。
4. API 成功率 >=95%，不得靠 `finish_reason=length` 或空正文掩盖失败。

次要安全指标（必须并列披露，不用于美化主判据）：

- 各标的方向分布与 stable_direction_rate；不得声称契约能提升方向 alpha。
- 可达率、可达后的方向收益、RR、成本与时延。可达率下降或尾损恶化必须标 WARNING，
  不得以一致性 100% 直接申请生产接线。
- 若方向变化落在控制臂重复噪声带内，只能记“供给差异，归因不足”。

任一主要判据失败 → `KEEP_OFFLINE / NO_PROPOSAL`。样本只有 1 个交易日、2 个标的，
即使通过也只允许进入下一步生产**只读影子**评审，不允许直接改生产 Prompt。

## 4. 后续生产影子门槛（另批，不自动启动）

只有 OFFLINE_CONTRACT_PASS 后，才可另起实施批申请：

- 独立 prompt_hash；模型与 EventContext 不变，影子 >=10 个交易日。
- 记录原始 direction、执行字段一致性、有效卡率、研究完整率、实际方向供给与 Gate 翻转。
- 新字段必须有消费方、基数自检和单位/枚举契约；无消费方字段不得入库。
- 不把一致性改善直接换算成收益；任何生产行为改变仍需用户单独批准和一行回滚。

## 5. 回滚与停止

- 离线回滚：删除 `--skip-contract` 臂及对应隔离产物，不影响控制臂或生产。
- 若未来影子出现有效卡率下降、方向异常集中、可达率显著下降或指令语义回归，
  立即停止，不进入生产；生产 Prompt 若获批实施，则删除补充契约恢复原 prompt_hash。

## 6. 10 次重复对照结果（2026-11-13 揭盲）

| 指标 | 控制臂 | 契约臂 | 判定 |
|---|---:|---:|---|
| valid_card | 20/20 | 20/20 | 通过 |
| research_complete | 20/20 | 20/20 | 通过 |
| direction_plan_consistent | 13/20 | **20/20** | 通过，合计 ≥95% |
| NVDA direction_plan_consistent | 3/10 | **10/10** | 通过，单项 ≥90% |
| API / finish=stop | 20/20 | 20/20 | 通过 |
| stable direction（标的） | 1/2 | 2/2 | 仅作观察 |
| 可达率 | 3/20 | **0/20** | **WARNING** |
| 成本 | $4.50397 | $4.40295 | 非判据 |

方向分布：控制臂 NVDA=7 SKIP/3 SELL_SHORT、AMD=10 BUY；契约臂 NVDA=10 SKIP、AMD=10 BUY。
契约消除了全部矛盾卡，但也把 NVDA 的 SHORT 供给全部变为 SKIP，且三个可达 SHORT 样本消失；
这只能解释为**供给语义变化**，不能解释为收益改善或方向 alpha。可达率从 15% 降至 0%，
按 §3 必须标 WARNING。

**机械结论：OFFLINE_CONTRACT_PASS + REACHABILITY_WARNING。** 允许作为后续生产只读影子的候选，
不批准直接修改生产 Prompt，不声称费后 edge。若要继续，须先补多日期/多标的的可达性与方向供给对照，
并按 §4 另行启动影子评审。

证据：`data/shadow/da_model_ab_nvda_amd_repeat10_v1.jsonl`、
`data/shadow/da_prompt_skip_contract_nvda_amd_repeat10_v1.jsonl` 及各自 `.quality.json`。

## 7. 仍未验证

- 未证明费后 edge、方向命中率或可达率改善；契约臂可达率反而下降。
- 只有 1 个交易日、2 个标的，尚不能排除日期/标的选择效应。
- 未验证 Claude：配置网关上 `claude-fable-5-2` / `claude-opus-5-5` 的 Models API 均 404。
- 未接生产、未写 playbook_instructions、未下单、未改 cron/DB/config。
