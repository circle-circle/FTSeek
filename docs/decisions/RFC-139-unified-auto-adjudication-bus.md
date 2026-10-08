# RFC-139: 统一自动验证总线 (判据 schema + 确定性 runner + 通俗决策背景)

> **状态**: 已实施 (2026-10-08, 用户裁「那就执行吧 + 推送做好通俗背景说明」)
> **类型**: L2 (零交易行为变更 — 只读评估 + 呈报增强)
> **来源**: RFC-138 后续 (riskbound 借鉴评估 §L1-L4); 用户裁「agent 是秘书不是法官」

## 1. 动机

77 项影子、~120 个验证脚本、判据全是自由文本 ⇒ 每次人工写结算脚本都是一次口径自由发挥。
用户需要「所有测试/验证/影子走一套自动逻辑」, 且推送必须带通俗背景, 避免遗忘和混淆决策源。

## 2. 设计 (三层, 全部确定性 — agent 不进判定层)

### 2.1 L1 — 判据 schema (`app/review/criteria_spec.py`)

机器可读判据投影, **不是新判据**: 预注册文本仍以注册表 `graduation_criteria` 为 SSOT;
schema 只是它的结构化编码 (metric/control/min_effect/predicted_sign/sample_gates/source)。
**改 schema = 改判据 = 重新裁决** (与 RFC-138 改 fixture 同一纪律)。
未收录 spec 的项 runner 回落 eval_script, 不臆造。

### 2.2 L2 — 确定性 runner (`scripts/auto_adjudicate.py`)

一个入口遍历注册表待裁面 (与 `pending_adjudications` 同一 SSOT 读层):
- 有 eval_script ⇒ subprocess 实跑 (确定性代码, 同输入同输出);
- 解析产物 verdict + 实测读数;
- 附 era 适用性 (复用 RFC-138 `verdict_era`);
- 统一 verdict JSON + 白话 stdout。
**边界**: 不跑 LLM、不判过/不过 (判分权在预注册判据 + 用户); runner 只「执行并呈报」。
**预注册纪律衔接** (METHODOLOGY-INDEX §判分代码): runner 执行的是**已冻结**的判据/脚本;
schema 是冻结判据的投影。这消除「每次人工现写脚本 = 口径漂移」, 而不是「先写判分凑数据」。

### 2.3 L4 — 通俗决策背景 (`pending_adjudication_report.py` + `shadow_lexicon.py`)

每个待裁条目推送时带四行白话背景 (全部从注册表 SSOT 字段推导, 不另写第二真源):
- **这是什么**: description 一句话;
- **决策源**: 判据出处 (RFC/DEB 编号 + graduation_criteria 原文首 80 字);
- **现在卡在哪**: 样本 n/min_n + 判据实测读数/缺口;
- **你要决定什么**: 明确动作 (立 DEB 裁 / 续观 / 停采)。
白话词典补 `entry_band_widen` / `eth_relative_volume_gate` 两条 (此前「未收录白话名」)。

## 3. 零行为变更声明

- 交易链路零接触: 新模块仅 scripts/ + app/review/ 呈报面; app/core|pipeline|playbook 零改动。
- 只读: runner 读注册表 + 产物 + DB(era), 写一份报告 JSON (data/review/ OUT_LATEST 模式)。
- 不新增 cron: 手动/既有调度入口均可跑; 无料不推纪律不动。

## 4. 预注册验收判据

| # | 判据 | 验证 |
|---|---|---|
| A1 | criteria_spec 单测: schema 字段/来源引用/未收录回落 | pytest tests/test_criteria_spec.py |
| A2 | auto_adjudicate: stub eval_script ⇒ 统一 verdict + era + 读数 | pytest tests/test_auto_adjudicate.py |
| A3 | 实跑 auto_adjudicate --dry rc=0, 输出含待裁 2 项与 era | 实跑 |
| A4 | pending 报告: 两条待裁均有通俗背景块 (这是什么/决策源/卡在哪/裁什么) | 实跑 + 单测 |
| A5 | 等基回归: review 面新增失败 = 0 | pytest tests/review |

## 4.1 验收实测 (2026-10-08 落地批)

| # | 结果 | 实测 |
|---|---|---|
| A1 | ✅ | tests/test_criteria_spec.py 5 passed (schema/source 溯源/注册表一致/未收录回落/缺口披露) |
| A2 | ✅ | tests/test_auto_adjudicate.py 8 passed (四分类/确定性/待裁过滤/背景渲染/采集器缺口) |
| A3 | ✅ | `auto_adjudicate.py --dry` rc=0: 待裁 2 项; entry_band_widen=VERDICT_ARTIFACT(n=36/mean=-17.73/sign_p=0.309); eth_relative_volume_gate=COLLECTOR_ONLY(NO_SETTLER) |
| A4 | ✅ | pending 报告 `--dry --force`: 2 项待裁均带背景块 (这是什么/决策源/判据原文/你要决定); tests/review/test_pending_background.py 5 passed |
| A5 | ✅ | review 面回归新增失败 = 0 (见验收命令) |
| 附加 | ✅ | **连带修复 (实施中发现)**: `_artifact_readout` 过去读 data_path(jsonl) 永远「产物无读数」; 现经 spec.verdict_artifact 读 verdict json — entry_band_widen 实测栏从「产物无读数」变为「NOT_ESTABLISHED_KEEP_SAMPLING; n_settled=36」 |
| 边界 | ✅ | 交易链路零接触: 新代码仅 app/review/criteria_spec.py + scripts/auto_adjudicate.py + 呈报增强; rg 确认 app/core|pipeline|playbook 零引用 |

## 5. 已知局限

- L1 spec 先覆盖当前待裁面 (2 项) + schema 骨架; 77 项全量收录属后续批次 (防一次改大)。
- runner 不改注册表状态 (裁决权在用户); 产物只呈报。
- 17 项无 eval_script 的「有入口无出口」债不在本批清 (需逐项判: 补 spec 还是降纯观察)。
