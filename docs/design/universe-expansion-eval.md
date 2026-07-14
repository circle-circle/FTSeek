---
title: LLM 选股宇宙扩张·分析质量验证机制
status: active
module: universe_expansion
related_code: scripts/llm_universe_scan.py
related_okr: OKR-2026-07
related_adr:
tags: [llm, eval, shadow, universe-expansion]
updated: 2026-07-10
---

# LLM 选股宇宙扩张 · 分析质量验证机制

> 配套 plan「LLM 事件推理驱动的选股宇宙扩张」。本文档记录**如何度量并提升 LLM 选股分析质量**,
> 核心是「提升 LLM 分析能力(意图对齐+引证自律), 而非加代码硬规则」这一路线, 以及配套的验证闭环。

## 1. 背景与定位

选股宇宙扩张让 LLM 做「发现哪些标的」的关联因果推理。早期风险: 现状 prompt (V1) 无引证约束,
LLM 倾向**凑数做板块联想**(为报满而报), 产出不可核验、跑批发散。

**路线纠偏 (ponytail 审查结论)**: 不用代码布尔门 / 拍系数 (0.9/0.75) 去过滤——那是数据截断 +
代码越界做判断。正确做法是**提升驱动 LLM 的分析能力**, 让 LLM 自己更克制、强制自我引证,
再用**可度量的近端指标 + 远端交易结果**验证是否真的变好。

## 2. Prompt 双版本 (唯一变量)

| 版本 | 常量 | 定位 |
|---|---|---|
| V1 | `SYSTEM_PROMPT` | 现状生产 prompt (基线) |
| V2 | `SYSTEM_PROMPT_V2` | P0 意图对齐: 宁缺毋滥 + 授权空集 + 强制 `evidence_ref` 引证 + `self_tier` 自评 + 自律约束(无据→THESIS_ONLY 且 conf≤0.60) |

A/B 的**唯一变量 = system prompt**, clue brief / temperature / schema 完全一致。

`self_tier` 三级 (LLM 自评, 归因标签**非过滤门**):
`HARD_EVENT`(锚定具体事件) / `MARKET_CORROBORATED`(真实市场异动佐证) / `THESIS_ONLY`(纯逻辑联想)。

**Track A 夹逼** (`_normalize_surfaced(with_tier=True)`): 兜底 LLM 不守自律——`evidence_ref` 为空则强制
`self_tier=THESIS_ONLY` 且 `confidence≤0.60`, 记录 `raw_confidence`/`clamped` 供审。

## 3. 三层验证闭环

| 层 | 命令 | 度量什么 | 依赖 | 状态 |
|---|---|---|---|---|
| L0 收集 | `scan-eval` | 同 clue 跑 V1/V2 并排对比, 落 `prompt_eval.jsonl`, 抓 `scan_price` 供日后前瞻 | LLM | ✅ 已落地 |
| L1 近端 | `eval-report` | 浮现质量(当天可得): 克制比 V2/V1、引证率、tier 分布、夹逼率、Jaccard 稳定性 | 无(纯聚合) | ✅ 已落地 |
| L2 远端 | (拟 `eval-score`) | T+1/T+3/T+5 前瞻收益 × `self_tier` 切片, 验证"高 tier 是否真更赚" | 市场/时间 | ⏳ **未落地** (待 L0 数据积累后再建) |

> L2 依赖真实前瞻收益, 现阶段数据不足, 刻意不提前实现 (YAGNI)。`scan_price` 已在 L0 落库, 为 L2 预留。

## 4. 已验证成果 (shadow 实测)

- **平淡日**: V1 浮现 8 → V2 浮现 2 (EOG/ORCL, 自标 MARKET_CORROBORATED + 引证异动 + 降 conf); HAL/OXY/CRDO/DVN/XLY 被 V2 自己弃选。
- **强事件日 (霍尔木兹封锁)**: V2 不误伤——FRO/LMT/DAL 保留 HARD_EVENT 置信不降反升, 且挖出 V1 遗漏的 NOC/CVX。
- **稳定性 (3 连跑同场景)**: V2 三次完全一致; V1 发散 (8/8/10, 去重 15 只, 10 只仅现 1 次)。近端指标: 克制比 0.25 / 引证率 100% / 稳定性 V2=1.00 vs V1=0.51。

结论: V2 更克制、可核验、稳定; V1 发散性量化印证"凭猜测"。

## 5. 集成方案 (嵌入每日交易, 非 crontab)

作为**盘前参考环节**接入, 而非服务器定时任务:

- 挂载点: `app/scheduler.py` 的 `run_premarket_planning` (盘前编排入口)。
- 范例: 参照 `app/playbook/pipeline.py` 的 `run_playbook_generate` —— 节假日跳过 + 每日幂等标志 + 错误隔离 (`_run_with_error_handling`)。
- 产出: 盘前跑一次 scan-eval, 结果作为**人看的选股参考**, 不自动 inject/下单 (`entry_enabled` 仍默认关)。

> 状态: **规划中**, 尚未接入 scheduler。当前靠手动跑命令。

## 6. A/B 版本治理

- **单一 active 生产 prompt**: 生产只认一个 (拟 config `universe_expansion.prompt_active`, 默认 V1)。A/B 是**有终点的实验**, 非长期双跑。
- **晋级靠数据 + 人确认**: 待 L2 远端收益数据充分后, 由人决定是否把 active 切到 V2。晋级门槛**待数据后再定**, 不预先拍魔数。
- **长期只留一个**: 晋级后旧版存档 (代码保留常量即可), 不维护版本注册表。
- **刻意不做** (避免过度设计): 版本注册表 / candidate 指针 / eval-report 自动达标判定 / 回归基线双跑。
- **最小落地**: 仅 `prompt_active` 单指针 + scan 读取。**当前未实现** (P0 阶段 V2 仅走 scan-eval, 不进生产)。

## 7. 命令速查 (当前真实可用)

```bash
# 纯 shadow A/B 对比 (不下单)
PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py scan-eval [--clue "事件文本"]
# 近端质量聚合
PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py eval-report [--days 14]
# 现状生产链路 (V1)
PYTHONPATH=. ./venv/bin/python scripts/llm_universe_scan.py scan|inject|score|report
```

## 8. 回滚

- 代码: `git revert <commit>` (V2/scan-eval/eval-report 均为新增, 不改 V1 路径, 回滚无副作用)。
- 数据: 删 `data/shadow/prompt_eval.jsonl` (纯 shadow 日志, 无生产依赖)。

## 9. ponytail 一致性

- **LLM 出判断代码算数**: V2 提升的是 LLM 的引证自律 (判断), 代码只做 Track A 夹逼归一化 (不做选股判断)。
- **影子先行**: 全程 shadow, 不碰生产 inject/下单。
- **一次一变量**: A/B 唯一变量 = prompt。
- **实证不虚构**: 成果均为真实 scan 实测; L2/集成/prompt_active 未落地处**如实标注为规划中**, 不谎报。
- **避免过度设计**: L2 与治理设施待数据/需求出现再建。
