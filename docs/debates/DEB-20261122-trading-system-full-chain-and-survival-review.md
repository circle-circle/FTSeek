---
title: 交易系统全链路与长期生存能力审查
status: draft
module: cross
related_code: app/playbook/pipeline.py
related_okr: OKR-2026-11
tags: [trading-system, risk, execution, measurement, expectancy]
updated: 2026-11-22
---

# DEB-20261122 · 交易系统全链路与长期生存能力审查

## 1. 状态、背景与授权边界

- [x] 进行中：全链诊断与独立事实复核完成，建议已落档。
- [ ] 待决策：其余实施项尚需补齐其专属预注册、权限与验收条件。
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

**首轮状态：立案完成，政策与实施待裁。** 用户要求先分析、随后「请将建议写入deb或者rfc文档」；首轮授权仅为文档落档，不等于批准以下风险政策、实验或交易行为变更。选择 DEB 而非执行 RFC，是因为这是跨环节诊断与优先级建议，尚不是已批准的实施规格。

**后续 scoped 落地（R3，2026-11-22）**：用户裁「请基于deb分析检查 确认无问题请落地」。核查后仅 F4 只读口径披露与新发现的 F6 判分展示缺陷符合零交易行为落地边界，已修改既有评分脚本与对应测试；F1/F2 的数值/自动处置政策、F3 的自然事件、F5 的全调度核名不在该一般授权的可安全默认范围，仍待裁/待前向证据。详见 §11。

**R4 补充（2026-11-22）**：用户要求通俗解释逻辑/收益后再落地。已说明 F4/F6 是“少误判”而非直接增利；随后落地 F1/F2 的 test-only 安全护栏（不改监控/告警/交易行为），F5 只读盘点进行中。F1 具体预算/期限/再暂停动作、F2 自动核证/恢复/解锁仍待用户裁决。详见 §12。F5 只读盘点已完成，详见 §14。

用户原始理念：

> 交易系统是一套覆盖每笔交易全过程的、可重复执行的完整规则，它把人从靠感觉做决策的“赌徒”，变成靠概率和纪律稳定盈利的“执行者”。它客观、可重复、长期正期望，不保证每笔都赚，却能保证你永远不下牌桌，让概率优势在时间中兑现。

本题回答：这套理念还缺哪些生存、执行、核算与优势验证条件；对照 FTSeek-US 的实际消费路径，哪些已做到，哪些仍需完善。**长期研究方向可以不限 DA，但本文件不改已批准的 11 月 DA 主 KR。**

立案轮仅阅读代码、文档及只读统计；立案批只新增本 DEB、向 debates 索引与 handoff 追加记录，不改代码/配置/交易状态、不下单、不调用付费 LLM、不重启、不提交或推送。后续 scoped 改动另见 §11。

## 2. 建议修订的系统定义

> 交易系统是一套覆盖研究、决策、资金配置、执行、退出、对账与复盘的规则体系。它在明确的适用条件下检验并执行概率优势，在优势不明、风险超限或系统失效时停止承担新风险，以有限损失预算争取长期费后正增长。

需要补足的五个限定：

1. **完整规则不等于优势**：纪律能执行正期望，也能稳定执行负期望；工程可靠性不能代替 alpha 证据。
2. **正期望需要持续检验**：适用市场、成本、容量与信号分布可能变化；历史有效不保证未来有效。
3. **稳定是过程与风险受控**：不是保证每笔、每天或每月盈利，更不能保证永不破产。
4. **生存不等于永远运行当前策略**：保留未来参与能力，可能要求按预算和期限暂停、换路或停止。
5. **可重复以证据回放为基础**：保存输入、原回包、模型/prompt/配置版本和规则结果；重新调用 LLM 不保证同答案。

当前定位：**执行闭环正在补强，长期经济优势尚未证实。** 建议主线不是继续堆规则，而是闭合「有限风险、完整执行、真实核算、优势验证、到期裁决」。

## 3. 立案前撞车检查与现行约束

### 3.1 本题不新建并行机制

| 既有载体 | 本题增量与边界 |
|---|---|
| [稳定盈利可行性 DEB](DEB-20260815-stable-profit-feasibility.md) §11.4、§43.2 | 承接累计止损与人工恢复事实；建议补恢复后的预算/期限/再暂停责任，不改原线、不重置 tripped，不用 8 月旧数据推断今天。 |
| [出场全链审计 DEB](DEB-20261118-exit-profitability-full-chain-audit.md)、[MU 源头修复 DEB](DEB-20261120-mu-exit-protection-and-state-hygiene.md) | 承接既有恢复/记账/唯一宿主，不再造退出器、收养器或第二套账本；仅指出自然链路验收与 UNKNOWN 安全核证的残余缺口。 |
| [DA 推理链审计 DEB](DEB-20261119-da-reasoning-chain-audit.md) §2.1、§5、§12 | 复用资金双路径坏门和诊断尺子验收证据；正式输入修改仍按原线程排序，不以本 DEB 插队。 |
| [DA 输入卫生 DEB](DEB-20261118-da-input-hygiene-preregistration.md)、[模型切换 DEB](DEB-20261117-da-analyst-qwen4max.md) | 不新开输入/模型实验，不改变臂或终点；引用原线程的预注册、R2 顺序和 cohort 边界。 |
| `shadow_registry.py`：`blocked_exit_opportunity`、`entry_confirm_forward` | 前者已有 blocked 机会成本观察/结算历史，后者为入场确认前瞻；本题不重开前者旧轴，不以确认影子替代订单恢复验收。 |
| [熔断分层地图](../circuit-breaker-map.md) | 先认清现役分层，不合并正交风控、不添加平行熔断，不把 LIQUIDATE 名字当作已接强制全平。 |

**P0.5 限制**：本题没有新增影子/采集器，因此不申请实验名额。以上仅为本轮已核的相关载体，不冒充全注册表同轴枚举或全调度审计；任何后续机制立案须按 [模板](template.md) 补齐同轴 id、实际采集人群、消费方、既有裁决冲突检查，未补齐不得开工。

### 3.2 不松动的约束

- Playbook 为唯一下单/持仓管理管线；主管线新仓冻结不变，不能整体 gate 掉承载复盘/快照/心跳的 `scan_loop`（`AGENT.md:16–24`）。
- 四锁保持：`AUTO_TRADE_ACCOUNT=sim`、`ALLOW_REAL_TRADING=false`、`execute_mode=futu_simulate`、毕业门禁 fail-close；实盘仍需用户签字。
- 不擅改 A9/A10/A11，不重开 C8–C18 已关闭轴；不据事后 exit_reason、少数尾部赢家或单周样本新增交易门。
- 行为变更仍需原议题预注册、影子先行、一次一变量。修复明显输入 bug 也不能偷换成「零信号变化」。
- 不为攒样本强制下单、放大仓位/频率；不清 UNKNOWN 锁、不重置峰值或累计止损状态。
- 本批纯文档无重启。后续获准部署仍按 `AGENT.md` 两核、systemctl 受控重启与启动冒烟；新增推送仍走 `app/push/` 且默认静默。

判据与阶段权威：[判据字典](../ADJUDICATED-CRITERIA.md)、[11 月 OKR](../OKR-2026-11.md)、[协作宪章](../specs/multi-agent-collaboration-charter.md)。本文件不是替代判据或第二份进度队列。

## 4. 当前经济证据与解释限制

### 4.1 实际复算

主线程实际运行（exit 0，仅 stdout，无 `--out`）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python scripts/okr_2026_11_scoring.py --as-of 2026-11-20
```

GLM 另以同一现行取数器 JSON 输出只读复算；主线程亲读实现并复跑人类输出。下表是**2026-11-22 会话查询当前数据库已平记录、指定 `--as-of 2026-11-20` 的结果**。取数 SQL 只有 cohort 起点等条件，**没有 `trade_date<=as_of` 上界**；`as_of` 在取数后传入评估/外推（`scripts/okr_2026_11_scoring.py:532–550,590–592`），不是截至 11-20 的成交切片或历史数据库快照。后续正常新已平成交也会进入重跑，不仅补账/迟成交会改变读数；不能假称可逐字冻结复现。

| DA 窗口 | 已平 n | 名义加权净 bps | 净额 USD | 日等权均值的 80% CI，bps | MDE80，bps |
|---|---:|---:|---:|---|---:|
| whole | 131 | -31.5021 | -1832.82 | [-52.1116, -23.6282] | 31.1327 |
| pre | 107 | -27.0715 | -1327.49 | [-46.3358, -15.1131] | 34.1268 |
| post | 24 | -55.2618 | -505.33 | [-95.1634, -32.9762] | 67.9712 |

- **KR1：NOT_MET**。whole 已过 n≥120，但净 bps 为负；毛 -21.9856 bps、费用 9.5165 bps。whole 日聚类 t=-3.4079、14 日。相对 -2.25 bps 不回归线的差约 29.25 bps，小于当前 MDE，沿用脚本「不可分辨，不得宣称已回归」限制。
- **KR2：MET**。唯一判分口径 A11 落库 `mfe_pct<0.3%`，29/131=22.1374%≤23%。中间指标过关不能代替费后经济目标。
- **post n=24<30 且 <120**：只报告描述性读数，不凭负 CI 绕过样本纪律，不裁新模型无优势；也不能用先前少数已平盈利宣称模型已成功。
- `PLAYBOOK_SIM`、A9 排污、A10 era-clean 与现行模型边界均沿用取数器；不把其他源、未平仓或完整账户净值拼进 DA 分母。

### 4.2 两个不同的统计对象必须并列标注

`scripts/okr_2026_11_scoring.py:182–223` 的实现：

- `net_bps = 10000 × Σ净额 / Σ入场名义额`：名义加权总体净 bps。
- 日聚类均值、t、CI、MDE：先算各 `trade_date` 的净额/名义额，再对每日 bps **等权**统计。

两者都可披露，**后者不是前者的严格置信区间**。例：日 1 净 +9、名义 10000；日 2 净 -1、名义 100，总净 +7.9208 bps，但日等权均值 -45.5 bps。

Kimi 独立核实现后确认：这是估计对象的披露澄清，不能仅据此裁定取数器存在实现 bug，更不能自行改已裁评分常量、阈值或统计方法。未来若要变更估计目标/区间方法，须另立预注册并保留旧口径并列。

### 4.3 版本与账户边界

- OKR 的模型 whole/pre/post 三行继续保留；11-21 风险预算与执行修复生效后，后续 post 不是完整系统单版本窗口。建议另列风险/执行/输入版本分段，不把这些收益变化全部归因于模型切换。
- 11-21 SIM 5% 预算与相关修复**已经部署重启，冒烟 7/7**，不是仍待部署；生产主工作树 HEAD/index 未随隔离 release 提交改变，不能据 HEAD 单独否认补丁生效。凭据见 `handoff.md` 11-21 15:50 条与 [DA 推理链审计 DEB](DEB-20261119-da-reasoning-chain-audit.md) §11.14（`docs/debates/DEB-20261119-da-reasoning-chain-audit.md:665`）。
- 启动健康不代表自然成交或长期收益。最新交接尚未验证符合信号及全部风控的自然 `entry→claim→send→fill→BOOKED`。
- 已平净账不包含未平 LLY 的最终结局；周末陈旧资本/报价不用于 fresh 放行，陈旧 LIVE 本地记录不当作当前券商真值。本题不裁完整账户收益或实盘当前敞口。

## 5. 八环全链路审查与补全建议

下表是建议验收形态，不是本轮已经完成的验收或已获准的新功能。

| 环节 | 应补全的规则 | 当前缺口/误读风险 | 建议验收 |
|---|---|---|---|
| 1. 目标与适用边界 | 同时定义收益、可承受回撤、资金占用、研究成本、退出/停止条件；明确市场与容量边界 | 「稳定盈利/永不下牌桌」容易被读成收益与生存保证；DA 主 KR 不能被 EXT 少数尾部盈利掩盖 | 目标、损失预算、评估日、停止条件彼此一致；长期换路另裁，不改本月 OKR |
| 2. 数据与实际消费 | 对来源、单位、所属时点、采集时点、空窗和故障分态；核采到的数据实际进入决策 | 正式资金两消费路径仍有符号 `code` 被当状态 0 的坏门；诊断尺子验收不等于正式输入已修 | 原材料→渲染→prompt→消费方逐项回连；缺失与陈旧显式呈现，修改输入按原 R2 排序 |
| 3. 推理与决策 | 事实/推断/不确定/弃权分开；留输入、原回包与版本，支持规则回放 | 重调用 LLM 不确定；挑战者 SKIP 不应简单变硬否决；输出字段存在不等于被使用 | 用保存回包复现门禁/分级；新字段登记真实消费方、单位与基数，不新增交易决策层 |
| 4. 入场与仓位 | 单笔止损距离、现仓、在途预占、相关性、流动性与跳空压力一起纳入 | SIM 5% 是准入预算，不是无条件损失上限；不能只看单笔 RR 或剩余现金 | 压力场景/预占重复/跨 stop 价格逐项核；相关性或新缩量政策须另裁，不能顺带改变 sizing |
| 5. 订单执行与恢复 | claim、send、部分成交、回包丢失、迟成交、BOOKED、重启都有可核契约 | 只读超时 3 秒不等于完整 SDK/send/close 有界；冒烟不等于不双单不漏账 | 自然黄金链与隔离故障链分别验；订单精确身份→唯一交易账→唯一宿主→最终净账贯通 |
| 6. 持仓与退出 | 生效 SL/TP 与展示同源；唯一管理宿主；冻结、失败与人工升级具名 | 止损触发不保证成交；UNKNOWN 保锁正确，但 blocked 状态会跳过整条退出规则 | 无行情、跳空、冻结/部分成交场景单列；核证责任人与期限，不超时自动解锁/重发 |
| 7. 对账与完整经济成本 | 已实现、未实现、账户净值分开；毛/费/净分开；缺失成本标识 | SIM 往返佣金/监管费为估算，借券/资金/数据/模型成本不能因模型存在就当已入账 | 保留 A9 净账，同时单列完整经济成本敏感性；成交价已含的滑点不得重复扣 |
| 8. 复盘与演进 | 教训先作假设；证据、版本、适用范围与判据冲突检查；实验有终点/预算/截止日 | 复盘叙事可能越权成为新规则；注册状态不等于实际主动实验或采集运行 | 冲突教训不直接注入为命令；主动实验/永久测量/历史留痕按真实调度分开，按既有到期规则结算 |

依据：资金问题见原 DA 审计 §2.1（两处 `llm_synth.py:624` / `deep_analyzer.py:304`）；诊断尺子本地更新见同文 §12 与 handoff 11-21 17:11 条。费用入口见 `app/playbook/pipeline_parts/close_flow.py:323–340`（`fee_breakdown.source="est"`），成本模型见 `app/decision/cost_model.py`；教训加载/排序/渲染见 `app/review/lesson_injector.py:101–172`，其注入路径没有已裁 C 类冲突核对。上述静态事实不证明任一新机制能提高收益。

## 6. 四项需要优先闭合的具体缺口

### 6.1 人工恢复后的有限预算与责任闭环

**事实**：累计止损监控器在 `tripped=true` 时直接 return（仓内镜像 `ops/hermes/deb0815_stoploss_monitor.py:31–40`；实际载体位于 `/root/.hermes/scripts/`）。11-20 用户知情恢复 `auto_execute` 后，原 DEB §43.2 明确这一个累计止损层退出自动值守，风险转人工与其他既有闸；不是所有账户保护都失效。

**建议，待裁**：在原风险政策线程补齐「恢复后允许承担多少额外风险、有效到何时、何证据再次暂停、谁负责核证」。优先复用现有分层/预算，不另造平行熔断。SIM 5% 已生效也不自动回答累计亏损层恢复后的期限与责任。

**不做**：不运行能写配置/state 的监控脚本，不 reset tripped/peak、不改累计止损线，不自动接强制全平。`app/playbook/playbook_service.py:1220–1236` 中 HALT/LIQUIDATE 当前均拒新仓，不能把 LIQUIDATE 名字当全平保护。

### 6.2 UNKNOWN 的安全核证与升级

**事实**：`exit_monitor_flow.py:267–288` 对 blocked symbol 记录观察后 `continue`；`pipeline.py:3339–3387` 提供 30 分钟 HIGH / 4 小时 CRITICAL 年龄告警，并提示先核券商身份。该函数是 record/alert-only，不释放、不重评、不下单。

**建议，待裁**：依据现有滞留存活分布、机会成本与安全恢复证据，预注册核证时限、责任人、升级路径。告警送达不等于风险已处置；长期只报警、无人核证不是完整闭环。

**不做**：不把超时视为「未成交」或许可重发；不未经精确订单/回执证据释放锁，不新增绕过 UNKNOWN 的退出通道；受控重评/重提交仍归原出场线程裁决。

### 6.3 自然交易链与完整执行有界性

**事实**：已部署批的 READY/读侧有界化及启动冒烟只覆盖其已测范围；最新交接明确完整 SDK/GIL/send/close 仍未证，自然入场尚未发生。保留 UNKNOWN 和精确回执恢复比盲目重试更安全，但不能据此宣称全过程已验。

**建议**：下一 RTH 只观察满足信号及全部现役门禁的自然 `entry→claim→send→fill→BOOKED→唯一宿主→exit→账实/净账`。若无合格信号就报告「未发生」，不人工造单。回包丢失、部分成交、迟成交、重启与并发在隔离环境验证，不用生产故障注入证明可靠。

**验收重点**：精确订单身份、不重复提交、不丢成交、预占不提前释放、账实一致、原 SL/TP/同仓峰值保留；读超时、调用返回、锁等待、send/close 分段有证据，不能拿单个 3 秒读时限代替全路径时限。

### 6.4 真实尺子与收益归因

**事实**：净 bps 与日等权 CI/MDE 的估计对象不同（§4.2）；现行 SIM 费用是估算（§5）；11-21 风险/执行生效构成新的系统版本边界。

**建议，待裁**：先在读数中标明估计对象、费用覆盖/缺失、已平/未平/账户边界和版本；如需工具变更另明确零行为范围。完整成本单列敏感性，既有评分行并列保留，不将 post 收益全归模型，也不把运营成本重复扣进 A9 已裁口径。

**R3 已落地（仅本项披露）**：`collect_and_score().caliber` 新增 `sample_scope`、`net_bps_estimator`、`day_cluster_estimator`、`fee_coverage` 四个文案键；`render_lines` 仅在这些键存在时打印，`--as-of` help 明示不限制取数上界。SQL、评分公式、A9/A10/A11、schema 与阈值未改。完整成本敏感性与风险/执行版本分段未在本批实现，不冒称 F4 所有研究方向都已完成。

## 7. 关键分歧与可选路线

本节列的是待裁取舍，不虚构已有人员争论或投票结论。

| 取舍 | 路线 A | 路线 B（本轮建议） | 验证/裁决依据 |
|---|---|---|---|
| 继续运行还是保留退出选项 | 以修好工程为由持续延长当前策略 | 有限风险继续验证，同时保留按预算/到期停止或换路 | 既有评分与方案 B 日期纪律；修 bug 不能单独构成续期证据 |
| 继续加深推理还是先补基础 | 新模型/更多字段/更多规则先行 | 先核输入实际消费、执行恢复、真实成本与归因 | 前者需新增因果证据与预注册；后者不能冒称已有收益改善 |
| UNKNOWN 解锁便利还是成交安全 | 到时间就释放重发 | 保锁并具名核证升级，确认精确状态后才处置 | 原订单/回执身份、可安全恢复条件；不能用等待时长证明零成交 |
| 一个“净收益”还是分层经济账 | 将策略净账、账户 mark、运营成本混算 | 分口径并列，保留旧判分与完整成本敏感性 | 统一 source/cohort/版本/时间/价格基准，避免双扣成本与估计对象偷换 |

**推荐路线：先补闭环，不扩复杂度。** 代价是不能承诺短期多成交或转正；收益是减少对坏尺子、过期叙事和工程绿灯的错误依赖。

## 8. 优先级、待裁项与检查点

以下为建议队列，实际进度唯一入口仍是 `handoff.md`。未列具体金额/自动动作的项目是刻意待裁，不能解释成可自行设置默认值。

| 优先级/编号 | 建议与可能改动面 | 验收条件 | 时间/责任边界 |
|---|---|---|---|
| P0 / F1 | 在原累计止损/账户风险线程明确恢复后的有限预算、期限、再暂停与核证责任；如需配置/state 行为另立 RFC | 各层触发/恢复不冲突、状态可审计，预算不是绝对赔付保证；不重置旧事实 | 下一次风险政策变更前裁定；用户裁金额/期限/动作，执行责任待指定 |
| P0 / F2 | 原出场线程补 UNKNOWN 核证升级责任；若改自动恢复行为须原影子证据与新预注册 | 准确状态与唯一身份先行，未知不贸然解锁；告警有核证闭环 | 自动恢复行为改变前裁定；不改当前30min/4h告警档 |
| P0 / F3 | 承接已部署批的自然入场/记账/宿主/退出验收；完整有界性另作隔离证据 | §6.3 全链同源；故障与自然黄金链证据分开，未触发就不报通过 | 下一 RTH（2026-11-23 ET）起仅观察合格自然信号，不保证当日发生；原执行线程承接 |
| P1 / F4 | 披露估计对象、成本覆盖和风险/执行版本；可能涉及评分/只读报表，正式输入与教训注入各归原线程 | 旧 A9/A10/A11 保留、无跨窗拼接/双扣；输入改动不插 R2 队，冲突教训不越权成规则 | 下一次结果解读先附局限；工具/输入/注入修改须分别裁 scope，不打包上线 |
| P2 / F5 | 核真正主动实验、永久测量、历史留痕的分类与名额，不增新采集器 | 注册条目→调度→实际产物→消费/裁决链逐一核；状态数不能冒充主动实验数 | 申请新名额之前核清，治理执行责任待指定；任何退役另跑六面零引用 |
| P3 / F6 | 按既有结算/损失预算保留换路与停止选项；长期策略不限 DA，但不覆写本月 OKR | 到期按预注册报告达标/未达标/功效不足，不因修一个 bug 无限顺延 | 11-30 §5结算不顺延；§6 Kill/Pivot 按原方案 B，不新增另一个自动终止日 |

### 8.1 实验账面数不等于主动名额

主线程与 Kimi 分别以 AST 读取 `shadow_registry.py`，未 import 生产模块：120 个 `ShadowItem` 调用；KILLED 48、OBSERVING 66、GRADUATED 2、SUPERSEDED 4；OBSERVING 中 26 个有显式 `no_adjudication_reason`。

这只是本轮源文件构造计数。**66 OBSERVING 不等于 66 个主动实验**，其余 40 个也不能直接全算主动项，不能据此裁 KR4 超编。`collection_retired` 字段不保证每个 writer 停采；必须核调度/消费者，不批量删配置或退役。

可复算方式：对 `ast.parse(Path("app/playbook/shadow_registry.py").read_text())` 遍历 `ast.Call`，筛 `func` 为 `ast.Name("ShadowItem")`，按关键字 `status` 的 `ShadowStatus` 属性计数，再筛 OBSERVING 中非空 `no_adjudication_reason`。源码修改后数字允许漂移，报告须注明查勘时点。

### 8.2 既有裁决日不被本题改写

沿用 `docs/OKR-2026-11.md:264–334`：方案 B 仅顺延 §6 Kill/Pivot，触发为 post n≥120 或 12 月开跑会，取先到者；11-30 §5结算不顺延。当前 post n=24，触发为 False。取数器按两种速率给出的 [2026-12-08, 2026-12-15] 只是动态规划预测，不是固定承诺或新裁决日。

## 9. 决策、执行与验证状态

**首轮已获授权且执行的只有文档落档（历史，后续 R3 见 §11）**：

- [x] 建立本 DEB，保留理念修订、经济读数、八环审查、四项缺口、优先级、冲突与局限。
- [x] 向 `docs/debates/README.md` 注册，向 `handoff.md` 顶部追加凭据/验证/未验证/下一步。
- [ ] F1–F6 的政策、工具或行为实施：未获本轮授权，不因落档自动进入执行。
- [ ] 自然完整交易链、全 SDK 有界性、输入修复增益、完整成本后正期望：均未验证。

关闭条件：不是「文档写完」或「模型同意」，而是相关待裁项逐项被采纳并验证、被否决/移交或明确不做，且原线程与 handoff 状态一致；本题没有新设盈利保证或自动交易门。

## 10. 讨论与复核记录

### R1 · 全链诊断与事实复核（2026-11-22）

- **主线程**：形成修订定义与八环审查；亲读评分统计实现、累计止损镜像、blocked 退出跳过、年龄告警、LIQUIDATE 门和教训注入；亲跑现行评分人类输出（exit 0），不运行具有配置写入能力的止损脚本。
- **general-purpose / lifecycle-audit**：只读核实际调用链，报告七项残余：读侧时限不等于完整SDK有界、UNKNOWN符号级冻结、准入预算非绝对亏损上限、自然链未验、正式资金双坏门、教训缺冲突核对、费用估算与版本分段。没有生产测试/交易/API操作。
- **GLM / profit-verifier**：真实只读复算评分 JSON 及账本；主线程复跑评分，不冒称亲复算其全部 SQL。资本/订单/outbox 的瞬时零未决读数不能证明未来故障链可靠，也不是完整账户收益裁决。
- **Kimi / claim-reviewer**：独立核五项关键声明与注册表 AST，确认累计止损层不再自动重触发、UNKNOWN 冻结/告警语义、CI 估计对象不同、注册状态不能直接裁主动实验超编、11-21后 post 系统版本异质。对 CI 问题限定为披露澄清，不能据此裁实现 bug；复核不是部署动作复现。

### R2 · 文档交付核对（2026-11-22）

主线程文档检查：相对链接存在；draft 元数据、表格行、代码围栏与新文档尾空格检查通过；只读 AST 再算得到 §8.1 同一计数，未 import 生产模块。`git diff --check -- docs/debates/README.md handoff.md docs/debates/DEB-20261122-trading-system-full-chain-and-survival-review.md` exit 0；未跟踪的新 DEB 格式另由前述检查覆盖。

本批索引新增 1 行、handoff 新增 9 行；剥离这两段后 SHA256 与本批改前完全一致，原有 WIP 保留。全部证据位于本会话工具 stdout，没有落盘日志；这些检查只证明文档链接、计数与差集，不证明交易行为或经济建议有效。

**成品独立复核与更正**：Kimi 全文静态复核指出两项必须修正：① `--as-of` 不设成交 SQL 上界，后续正常新已平成交也进入重跑；② SIM5% §11.14 凭据在 DA 推理链审计 DEB，而非 MU DEB。主线程亲读取数器与实际章节后，已更正 §4.1、§4.3、下方教训及本批 handoff。其余关键风险/统计/待裁表述核对成立；Kimi 实算 AST 120/66/26、核相对链接与 diff-check，未复现三窗经济值、部署动作或测试，不借其静态复核替代这些证据。历史交接的简写 DEB 锚不在本批改写范围，本题引用已写全。

### R6 · 独立复查复核（2026-11-22 20:2x CST，today-execution-audit，只读）

独立复查对象 = 本文件 §11/§12 scoped 批（commit `7afe36c5e`，8 文件：4 MD + 1 script + 3 tests）。全部检查本轮亲跑，零写入生产、零下单、零重启、零付费。

- **真实路径测试复跑**（cwd=/tmp、`PYTHONPATH=/opt/ftseek/FTSeek-US`、venv pytest、`--noconftest -p no:cacheprovider`、`PYTHONDONTWRITEBYTECODE=1`）：`tests/test_okr_2026_11_scoring.py` ⇒ **46 passed / 0.32s / exit 0**；`tests/test_deb0815_stoploss_monitor_contract.py` + `tests/test_blocked_exit_opportunity_shadow.py` ⇒ **21 passed / 2.83s / exit 0**（3+18 例构成）。与 §11.2/§12 宣称的 46/21 逐条吻合。
- **真实数据复算**（只读 CLI，`mode=ro` 亲读 :569-583）：`scripts/okr_2026_11_scoring.py --as-of 2026-11-20` exit 0 ⇒ whole n=131 净 **-31.5021bps**（毛 -21.9856/费 9.5165）净$ -1832.82、KR1 NOT_MET、KR2 22.1374%（29/131）MET；pre n=107 净 -27.0715；post n=24 净 -55.2618 净$ -505.33；**Kill/Pivot triggered=False**、顺延 2026-12-15；F4 四行口径披露均实际打印。与 §11.2 记载逐位一致。
- **保护范围 SHA 与 diff-check**：`git diff --name-only 7afe36c5e^ 7afe36c5e -- app/ config/ ops/ data/ main.py` ⇒ **空**（本批零触碰运行时/配置/止损 state）；`ops/hermes/deb0815_stoploss_monitor.py` 对 HEAD 干净（本批未改监控脚本，F1 是 test-only）；`data/deb0815_stoploss_state.json` mtime 仍 2026-11-20 02:31（tripped 态未动）。research worktree 内 4 个同名副本（scripts/okr_2026_11_scoring.py + 3 tests）SHA256 与 `7afe36c5e` blob **逐文件全等**（51321c0f…/8d28863c…/acb5d399…/c7edb399… 四个 MATCH）。
- **代码审读**：F6 旧 `bool(verdict=="NOT_MET")` 会把 n<120 功效不足也展示成触发；新逻辑 n≥120 ∧ net≤0 ∧ CI 含 0 才触发、CI=None 不触发，符合 §12.5 已裁口径；`pivot_evaluation` 无自动交易消费方（grep 无），报告不自动停单。F4 仅增 caliber 披露键与打印行。F1/F2 测试全部 monkeypatch 隔离 tmp DB/CFG/STATE，不触生产路径。
- **记录差异（如实登记，不改他人原文）**：§11.3 与 handoff R3-R5 条写「未提交/推送」，但实态是本批已入 commit `7afe36c5e`（15:23:42）且**已推送 origin/rfc-060-phase1-hotfixes**（`git rev-parse HEAD origin/rfc-060-phase1-hotfixes` 同值）。代码正确性不受影响，台账自述滞后于 git 实态。
- **未验证**：①收益未证——F4/F6 为判分/披露修复，F1/F2 为 test-only 护栏，均无利润/命中率/执行改善证据；②F6 新触发分支（post n≥120）在真实数据上**未发生**（当前 post n=24 走「功效不足」路径），仅有参数化测试级证据；③本批未部署亦无需部署（服务零 import `okr_2026_11_scoring`，grep 实证）；今日 15:41:48 服务重启（PID 3829947）属**另一批次**的部署，非本修复；④F3 自然链、完整 SDK 有界性、F1 预算政策、F2 自动处置仍待裁，不在本复查范围。


## 11. Scoped 实施记录（R3，2026-11-22）

### 11.1 授权与落地范围

用户后续明确「请基于deb分析检查 确认无问题请落地」。审查结论不是六项都可直接实施：F1 的金额/期限/再暂停动作、F2 的自动恢复/解锁政策仍需具体裁决，F3 必须等待自然事件，F5 尚无全调度核名证据；因此本批只落地 F4 披露与审查新发现的 F6 判分展示修复。

| 项 | 精确实现 | 不变边界 |
|---|---|---|
| F4 | `caliber` 增加样本/as_of、两类估计对象与费用覆盖四个文案键；旧 block 无这些键时不新增行；CLI help 标明 as_of 不限制取数 | 不改 SQL、净账、公式、A9/A10/A11、阈值、schema，未实现完整成本/版本分段 |
| F6 | `post n≥120 且 net_bps≤0 且 ci80_contains_zero is True` 才置 `triggered=true`；新增 `net_bps`/`ci_contains_zero` 展示字段，CI=None 不触发；note 明示报告不自动停单 | 不改 KR1/KR2 verdict、样本门、§5/§6 日期、统计对象、四锁或任何订单/退出行为 |

F6 是本轮核查发现的**既有实现缺陷**：旧代码 `bool(post_gate.get("verdict") == "NOT_MET")` 漏掉已裁 OKR §12.5 第 2 条「净≤0且日聚类CI含0」，会把净负但 CI 不含 0、以及 CI 不可算也展示成触发。修复按原判据纠正展示，不是用户新批准的风险政策，不会自动 Kill/Pivot 或停单。

### 11.2 实际验证与独立复核

- **目标差集**：`scripts/okr_2026_11_scoring.py` + `tests/test_okr_2026_11_scoring.py`，79 增 6 删；无既有目标 WIP。`scripts/okr_status.py` 只复用 `collect_and_score`/`render_lines`，本批未改它；`Grep` 未发现 `pivot_evaluation` 自动交易消费方。
- **基线与最终测试**：改前 `--noconftest` 41 passed（`/tmp/deb20261122-f4-baseline-pytest.log`）；最终测试运行用 `python -P` 预加载并断言 `SCORING_MODULE=/opt/ftseek/FTSeek-US/scripts/okr_2026_11_scoring.py`，随后执行 `pytest --noconftest -q tests/test_okr_2026_11_scoring.py`，**46 passed / 0.50s / exit 0**，日志 `/tmp/deb20261122-f4-f6-original-module-pytest.log`。纯函数测试未跑全仓，不借用带生产 fixture 的其他批次计数。
- **基线语义对比**：`/tmp/deb20261122-f4-f6-semantic-equivalence.log` 锁定同一原仓模块；保护 SQL、判据常量与 `window_stats`/`kr1_verdict`/`kr2_dead_rate`/三窗/日历/分源等 AST 不变；合成 block 去掉六个新增披露键后与 HEAD 基线相同；旧人类输出一致，旧 block 缺 `caliber` 也不新增打印。负净且 CI 不含 0 的旧误触发 `True` 修为 `False`，KR1/KR2 完全相同。
- **真实只读取数**：`/tmp/deb20261122-f4-f6-readonly-scoring.log`，CLI exit 0，n=131、净 -31.5021 bps、KR1 NOT_MET、KR2 MET、post n=24/触发 False/规划外推 12-15 均保持；新增四行口径提示可见。`as_of` 仍不限制取数，不把该读数当历史快照。
- **Kimi 独立复核**：按原仓绝对路径与 HEAD diff 审查，独立复算 F4/F6 测试数值，确认无阻断、无必须修改项；其结论仅限披露与判分展示，不构成收益/自然链证明，也不代替用户提交或交易政策裁决。GLM 另报原仓 46 passed 与只读 CLI 一致；主线程最终凭据仍以已锁路径日志为准。
- **过程纠错不隐藏**：F4 测试曾把首日名义额写小 10 倍、误算 CI 标准误；F6 初版 fixture 又把零方差 CI=None 当显式 False、单日只造 60 行。均修正测试，不改生产公式。原工作目录被切到旧 worktree，同名副本曾造成导入歧义；最终 runner 已锁定原仓路径，旧 worktree 副本不作为交付或部署源，未删除他人文件。

### 11.3 归账与剩余事项

本批另更新本 DEB、debates 索引、handoff 与 OKR §12.7 既有测试计数（41→46），不改 OKR 判据文字。未提交/推送，未改 `app/`、交易配置、订单、UNKNOWN/峰值/tripped、cron 或交易 daemon，不需要重启。

仍未实施：F1 风险数值/期限/责任与再暂停动作；F2 自动核证/恢复/解锁；F3 下一 RTH 自然全链与完整 SDK 有界性；F4 的完整成本/版本分段；F5 全调度核名。F6 只修复报告逻辑，不推迟 11-30 结算，不新增自动终止日，也不证明长期正期望。

## 12. F1/F2 测试护栏落地（R4，2026-11-22）

用户要求「通俗易懂解释逻辑和收益 再执行落地」。已说明：F4 像仪表盘标签，区分“总净 bps”和“日等权 CI/MDE”，避免把后者当前者的置信区间；F6 像刹车提示逻辑，旧报告把 post `NOT_MET` 直接当 §6 触发，现严格按已裁「n≥120 且净≤0 且 CI 含 0」才标记，CI=None 不触发、不自动停单。两项收益是**减少误判/误停/误宣称盈利**，不直接增加交易利润；当前 DA 仍为负，不能承诺转正。

随后落地不需要新风险授权的 test-only 护栏：

| 项 | 新增/修改 | 验证 | 不变边界 |
|---|---|---|---|
| F1 | 新 `tests/test_deb0815_stoploss_monitor_contract.py` 3 例：tripped 早退不重写 CFG/STATE、未触线静默、隔离 tmp 触线写路径 | 两份文件 21 passed / 1.64s，`--noconftest` + `python -P` 原仓路径 | 不改 `ops/hermes/deb0815_stoploss_monitor.py`、生产配置/state、累计止损线、tripped/peak |
| F2 | `tests/test_blocked_exit_opportunity_shadow.py` 增 1 例：年龄告警 record-only，不释放/不记账 | 同上 | 不改 `pipeline._alert_blocked_exit_age`、30min/4h 档位、UNKNOWN 保锁/退出规则 |

F1 预算/期限/再暂停/责任、F2 自动核证/恢复/解锁仍待裁；本批只锁现有安全语义，不新增自动动作。F5 只读盘点进行中，不新增采集器/不退役/不写状态。

## 13. 方法教训与未验证清单

1. 工程绿、输入可见、订单台账全 BOOKED 都不能单独证明正期望；严格区分静态实现、瞬时状态、隔离测试、自然生产与经济证据。
2. 已平 cohort 不等于完整账户；`--as-of` 不限制成交取数上界，也不等于历史数据库快照；后续正常新已平成交、费用、迟成交与补账均可能改变重跑值。
3. 资金坏门已确证不代表修好后一定提高收益；诊断尺子本地更新不等于正式 DA 输入已修改。
4. F5 只读盘点已完成（§14），但主动实验名额归属/退役仍待裁；未自然验证新链，未证完整 SDK/send/close 有界，未建立完整经济成本后正期望。
5. 除 §11/§12 已落地的 F4/F6 判分改进与 F1/F2 测试护栏外，本 DEB 其余项目只提供补全建议与实施前检查点；已落地项不修改已裁判据，不重开关闭轴、不覆盖已有裁决或偷换为新增交易规则。

## 14. F5 只读盘点结果（R5，2026-11-22）

后台 agent 在原仓做 AST + crontab + Hermes 43 jobs + systemd + 产物 mtime 只读盘点，零写入/零退役；主线程复核 120/66/26 计数及 KEEP5/候选 id 存在。GLM 另独立验证 F1/F2 护栏 21 passed 且生产 config/state/db 跑前跑后零变化。

- **计数**：120 项 = OBSERVING 66 / KILLED 48 / GRADUATED 2 / SUPERSEDED 4（历史留痕 54）。66 中 26 带 `no_adjudication_reason`（19 永久测量 + 7 裁决冻结/保真门/样本门）；余 40 除 `ssr_shadow_daily`、`ticket_size_breakeven` 自述无毕业门/已结案外，约 38 有毕业门。
- **KEEP5（DEB-20261114 §8 用户已批，占满 5 名额）**：`fee_gate_shadow`（cron 06:45/06:47，产物 1.3d 新）、`execution_tca_spread_shadow`、`risk_size_tier_forward`（config gate=true，1.6d 新）、`budget_p3_sizing`（读数停 11-07，eval 无 cron/Hermes 载体）、`da_frequency_cap_pivot_shadow`（cohort 11-16 起，读数 as_of 11-14 未刷，settle 无调度，notes 自述 stale_expected）。
- **超编在采候选（有预注册门，事实占名额，归属待用户路由）**：`entry_confirm_forward`（11-05 重开，enabled=true，SIM 80/20，cron 06:37）、`be_half_at_arm_d`（D 臂前瞻，cron 06:38，fresh）、`s1_short_after_open`（RFC-212 真钱 $4200×≤2 笔/日，forward_n=4/40，读数手动停 11-16）、`thesis_management_shadow`（11-18 批，notes 明示计 1 个实验名额，复核 12-02）、`exit_session_window`（gate=true 但仅 3 行，慢攒样）。另 A 类 ~24 已闭轴仍在采（11-14 六面审计全 STOP 命中消费方，未路由前不能算已腾位）。
- **UNKNOWN**：`da_event_prompt_canary`（.env.us `DA_EVENT_CONTEXT_INJECT=1` 但产物冻结 11-13，`da_event_inject_report` 无调度——注入开着、记录停了，须查）；`broker_capacity`/`broker_stop_drift`/`exit_intent_reaper`/`backfill_fidelity` 产物缺失 = 设计内休眠（SIM 无 REAL 单/零检出/探针未跑），非断采；`instruction_conflict_shadow` 12.6d 未刷，疑事件驱动无冲突。

**边界**：本盘点不能据此裁 KR4 或退役——逐项六面零引用未跑、生产消费方路由未定（DEB 已证不可 mass-batch）、`current_n` 恒 0 须 `refresh_counts`、mtime 只证写方活不证判据可裁；KEEP5 之外名额归属须用户裁决。

## 15. VIX 与方向设计优化：Phase A 本地 record-only 落地（2026-11-26）

### 15.1 授权与问题拆分

用户要求「我需要检查当前的逻辑和设计是否合理，不要极端偏单向」，随后「按建议执行优化调优落地」。本批先修可核证的口径与时点披露，不把这句话扩张成所有交易门的豁免。

VIX 表示波动而非涨跌方向；VIXY×0.85 是经验代理，不是现货恒等换算，偏差可双向。方向集中还涉及 SPY 趋势评分和 RFC-217 已批准的 NO_TRADE SHORT-only SIM 权限，不能通过强制多空 50/50、调整 VIX 阈值或随意放开 LONG 来解决。普通生成侧 NO_TRADE 拦截不等于 DA 直写指令的生成门；不能把 DA 样本都称作生成/执行换档。

**本批仅 Phase A**：如实记录原始读数与原有分别评估。没有实现/激活同轮快照 reuse/enforce 开关，没有修改任何方向、仓位、阈值或实验判据。Phase B 快照消费以及方向权限调整须独立评审、影子/前向证据和具体授权；RFC-217 继续原实验，不结案。

### 15.2 实际改动与消费端

| 落点 | 本地实现 | 保持不变 |
|---|---|---|
| `vix_source` / Router / AV | ContextVar 回执绑定同一次标量取数；保留 source、as_of、provider 原始 quote_time | 原优先级、0.85、窗口、标量返回、TTL、重试、缓存发布保护 |
| `market_regime` | `vix_observation` 明示口径、单位、UTC read_at；SPY basis/quote_time、UTC assessed_at、trend_score/volatility_score | 原分项、分档、top_n、乘数、min_strength；缺 VIX 跳过分项、展示20及原降级行为 |
| `risk_gates` / pipeline / auto_execute | 函数局部审计 sink 记录原 preflight、实验态、逐指令 sizing；复用既有 sizing_delta/reprice 行；附原 generation 文本 | 原 assess 调用与原 gate；未新增取数或全局共享；成交时 `playbook_service` 仍独立评估 |
| `shadow_field_cardinality` | 显式 `--regime-audit PATH` 只读既有 sizing_delta JSONL，输出 stage/field 的 n、missing、unique、unit；n≥10 且 unique<2 标 WARNING_CONST | 默认 CLI、既有 DEGEN 判据/退出码、cron/通知不变；新模式始终 exit0，不产生交易动作 |

新回执中的现货为 `vix_points`，代理为 `vix_points_estimated`，未知为 `unknown`；趋势/波动分项是评分点，非收益率。全源失败的 observation.value=None，旧 ctx.vix=20 仅为兜底展示，不冒充真实20点观测。Futu SPY 真读到报价才标 `futu_last_price`；AV 未提供 basis 时仍标 unknown。MA 保持日K计算，不把价格一律称为昨收。

### 15.3 必须保留的解读限制

1. `read_at` 是取数返回时间，不是行情观测时间；`assessed_at` 不是成交时间。Futu quote_time 不臆造时区，Finnhub 保留原 epoch；缺 provider 时间就如实缺失，未验证报价新鲜度。
2. 三处分别调用 assess **不等于三次新取数**：原类缓存正常120s、不确定30s仍保留；`publish_cache=False` 也可读已有效缓存。`regime_snapshot_used=False` 仅表示本批没有新增显式共享快照消费，**不表示没有缓存复用**。
3. 覆盖仅为到达既有载体的指令：预检早退未持久化该 sink；LONG 早拦不进入 reprice；MOM 专用 reprice 未接此字段；sizing 分支未评估就没有 sizing 键。缺键不等于无风险或许可，不把这批记录当完整多空机会池。
4. generation_regime_text 是原文本，不反向推算为结构化生成快照；成交时评估保持独立，不用轮首时间冒充成交时间。审计失败不改变原门禁，sizing_delta 的存量异常吞行方式未改。
5. 基数报告的 row_count/missing 基于有效 dict 行，不是物理行数；缺文件/坏行可能为空/跳过，exit0 不是数据完好证明。重复轮/缓存、稳定行情、单一来源也会恒定；WARNING_CONST 只提示核查。VIX value 的 distinct 可跨 source/unit，unit 是 observation.unit 指针，不作同口径统计；设计常量 `regime_snapshot_used=False` 不参与退化判定。

### 15.4 验证凭据与过程纠错

- **范围**：7 个 app 源 + 1 个纯读 scripts 消费端 + 6 个既有测试文件；另更新本 DEB、RFC-190、RFC-217、handoff，共18文件；未提交/推送/重启。证据目录 `/tmp/regime-caliber-20261126-k1wfp6l1/`，源码 SHA 锁定于 `final-scope.json`，前像 `before.zip`，文档当前前像 `docs-before-final.zip`。
- **正式整合**：`env -C /tmp PYTHONDONTWRITEBYTECODE=1 FUTU_DISABLE_DAEMON=1 /opt/ftseek/FTSeek-US/venv/bin/python /tmp/regime-caliber-20261126-k1wfp6l1/run_checks.py` ⇒ **539 passed / 43.93s / exit0**，`final-integrated-v2.log`；禁止真实 socket/DNS、生产非只读 SQLite、生产 data 文件写，`BOUNDARY_VIOLATIONS=[]`，三个主模块路径均原仓。使用 `--noconftest -p no:cacheprovider`，并逐例清 regime 类缓存，不借生产 fixture 豁免；没有跑全仓。
- **分组定位**：`final-caliber.log` 113、`final-entry.log` 360、`final-compat-v3.log` 59 均 exit0/边界空；合计532是追加基数7例前的同组结果，不与最终539累加。
- **静态保护**：`protected-policy-check.json` 核2配置、止损 state、AGENT/AGENTS 五项 SHA 与前像相同，7个取数/评分关键函数 AST 相同；`branch-conditions-check.json` 核五函数全部 if 条件序列相同。仅证明这些保护范围，不外推全部生产数据或他人 WIP 无变化。
- **测试守卫**：真实 Router/FRED/Finnhub 叶函数在 fake provider/HTTP 下运行；覆盖代理/现货/未知/缺数、时间/单位、缓存不伪刷新、线程/重入、禁止迟到发布，分别调用和 SHORT 0.3/LONG 拦截保持；基数新模式禁止调用旧 DB collector，旧 CLI 路径也保留。工程绿不证明净期望。
- **过程纠错**：首轮隔离捕获旧夹具访问生产影子/库，未放宽守卫。后修假依赖 now_utc、数字 sizing、fake package 纯模块、S1 kwargs、黑窗默认观测隔离及全局冻结 datetime 污染；保留失败定位日志，不用旧红/绿当最终证据。另一代理曾并发插入未纳入本批的 Phase B/global shared state；主线程暂停两文件写入，由原代理精确逆操作，未整文件还原，产物移至 `agent-unused/`，亲核最终 diff 无残留后才重跑。旧 `/tmp/rgbd-*` 试跑非正式凭据。
- **独立审查**：Kimi 只读实际差集、原上下文与日志，核心 Phase A 未发现严重/必须修改项；未亲跑 pytest，其日志复核不冒充独立实测。缓存/AV unknown/早退/MOM 覆盖限制已按意见明确。

### 15.5 当前状态与下一步

这是**本地技术口径/留痕落地，尚未部署**，不证明已减少亏损、提高命中率或修正了当前单向权限。四锁、毕业门、UNKNOWN、峰值/tripped、出入场风控不动；不重启 scan_loop、不复活已退役 regime_divergence_shadow、不新增采集器/经济实验/cron/prompt/推送。

下一步先获本批发布/重启授权，核在途出场与 app/scripts WIP，不夹带其他批次；部署后按现有管线观察来源/时间/缺键覆盖与自然多阶段记录，再决定是否进入独立 Phase B。评估方向权限须分清生成、门禁、执行质量、成交、费用后双向样本，不强凑50/50；RFC-217 的 n≥30或10交易日/提前裁停及所有硬闸继续原样。
