---
title: RFC-218 — DA 覆盖、方向、VIX 与 NO_TRADE SHORT 实验正确性评审
status: draft
module: cross
related_okr: OKR-2026-11
related_adr: ""
tags: [da, coverage, direction, vix, rfc217, correctness, independent-review]
updated: 2026-11-27
---

# RFC-218：DA 覆盖、方向、VIX 与 NO_TRADE SHORT 实验正确性评审

- 日期：2026-11-27
- 状态：**草案 · 待独立 Agent 复查 · 未批准实施**
- 作者：Claude Code（只读评审整理）
- 审批人：待独立复查后由用户裁决
- 关联：RFC-190、RFC-215、RFC-216、RFC-217；DEB-20261124、DEB-20261125、DEB-20261127；OKR-2026-11；`docs/ADJUDICATED-CRITERIA.md`
- 本文性质：**问题筛选与最小修复建议，不是生产变更授权，不是收益预注册，不替代 RFC-217 原实验判据。**

## 0. 给下一位独立复查 Agent 的任务

请按项目多 Agent 宪章做证伪式复查，不要只写“同意”。至少完成：

1. 重新核对 Git 基线、工作区、仓外 DA 实际源码和运行载体；不要把本文的源码路径当成部署证明。
2. 独立阅读源码并尝试推翻以下三个最关键断言：
   - 预 sizing SHORT 从早期 regime 等到 NO_TRADE 时，会绕过 RFC-217 sizing 和 cohort 标记；
   - `risk.global_blacklist` 标的可从动态发现及最终合并路径重新入池；
   - 正常 capital_distribution  payload 因 ticker/`code==0` 错配而无法渲染。
3. 独立复算方向漏斗，特别检查 raw、bridge、费用、确认、历史 LONG 权限和成交是否混用了分母。
4. 区分当前 HEAD 与 11-25 历史行为：当前 NO_TRADE LONG 是按轮暂缓，不再直接写终态 SKIP。
5. 对每项建议给出：确认／驳回／证据不足、最小修改闭包、行为影响、测试、回滚和是否属于收益假设。
6. 没有一致数据库快照、运行进程 manifest 或同刻现货 VIX 时，逐项写 `BLOCKED`，不得用归档材料冒充当前生产实测。

本任务只允许只读复查。代码、仓外 DA 源码、配置、prompt、数据库、订单、付费调用、实验、提交推送和部署重启均须另行授权。

---

## 1. 通俗摘要

本评审不是为了把多空调成 50/50，而是先解决四件事：

> **该禁止的标的不从侧门回来；该缩的实验仓位不漏缩；模型看到的数据不失真；每笔实验交易都能追踪。**

主要结论：

- 静态池行业构成不是当前最迫切的问题；动态扫描遗漏不可解释、黑名单动态回入和 DA 上游漏斗缺口更可落地。
- 11-20 后原始 DA 已明显偏空，发布筛选和执行权限又移除部分 LONG；不能将最终全 SHORT 全归因于模型。
- VIX 没有被直接当成个股方向指标，但 regime 同时消费于 sizing 和执行许可；代理、时点及重启状态边界需要先讲清楚，不应直接调阈值。
- RFC-217 的主权限链基本正确，但预 sizing 指令存在 sizing/cohort 绕过路径，实验身份传播也不完整。
- 资金分布、短 K 和健康空财报是正式输入正确性问题；它们会改变信号，不得包装成“纯遥测修复”直接上线。
- 当前没有修复后收益改善证据。正确性修复的主要价值是减少错误暴露、避免错误归因，不等于已找到正期望策略。

---

## 2. 审查基线与证据边界

### 2.1 Repo 基线

```text
HEAD = 078966c983951f3372e50fb976a42519ea9e2e77
历史审查基线 b914d65c99cc6419b6dc2babd721c7249e609d62 是 HEAD 的祖先。
```

审查开始时工作区已有且本评审未触碰：

```text
M docs/debates/DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md
```

该 DEB 的 §§12–13 在审查时属于未提交内容；引用时必须标明，不得与 HEAD 已提交文本混为一谈。

### 2.2 仓外 DA 正式源码

生产 carrier 调用 `/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py`，仓外 DA 逻辑不受 repo HEAD 版本约束。审查时实际读取版本：

| 文件 | SHA256 | mtime（+0800） |
|---|---|---|
| `data_collector.py` | `8413c4a6567798e827c7299facf58d4c1dd53968911284c8f67d0e2700aaeb40` | 2026-11-17 18:14:21 |
| `llm_synth.py` | `705ff7c2f43b2ca34aa77ac5b37df11302375a6dd2103670e1fc91d723609b8c` | 2026-11-17 18:15:35 |
| `pipeline.py` | `4f38df79149ee4ba37aa6a75ed6722138c753015324b11c916416aba5fe34c3c` | 2026-11-17 17:58:14 |
| `market_scanner.py` | `98ff79fadaf80468eead142f990a7ebc488887435706757e88c58ab5909e0e4b` | 2026-11-09 17:03:16 |

Repo carrier `ops/hermes/da_main_carrier.py` 审查时 SHA256 为 `b7a873cbceb0ec43924973a1edf0bbc1efeb30f9a9bf4e6e8aeffdb9f82f37f8`。这些哈希只固定本次阅读对象，不证明之后未变化，也不证明当前运行进程与之一致。

### 2.3 数据口径

- 方向和收益主要来自 11-17—11-25 归档 checkpoint、bridge、日志和只读数据库复核。
- 最新官方评分输出是 11-27 保存的归档，样本最大已平日为 11-25；不是 11-27 盘中实测。
- 评分器 `as_of` 历史上不限制 SQL 上界，不能单独充当历史快照证明。
- 本轮未创建 SQLite 一致备份，未外联现货 VIX，未调用付费 LLM，未重启服务。
- 静态代码事实、归档生产事实和当前运行状态必须分开陈述。

### 2.4 已落地、不得重复提出的修复

- OKR 评分费用/净额 fail-close 与 CI 文案：`7c5556ef2`。
- bridge 启动基线及内容 SHA 绑定、一致读取稳定性：`7c5556ef2`、`d5051134f`。
- RFC-216 VIX 20–22 连续计分：已部署，见 RFC-216。
- NO_TRADE LONG 按轮暂缓：当前 `auto_execute_flow.py:591-615`，历史终态 SKIP 不可描述为当前行为。
- RFC-217 SIM-only live-arm 能力闸：`29c4a757d`，见 RFC-217 §§7–8。
- DISAGREE＋SKIP 不再获得旧式分数抬档：`signal_gate.py:397-424` 等现有 no-lift 逻辑仍在。

---

## 3. 候选覆盖：构成事实与真实问题

### 3.1 静态池构成不是收益因果证据

当前 YAML：56 只股票、44 启用、12 disabled；8 只 ETF 全 disabled。按项目自己的 sector 标签，启用股票为：

| 标签 | 数量 |
|---|---:|
| Technology | 8 |
| Semiconductors | 6 |
| Consumer Cyclical | 6 |
| Financial Services | 5 |
| Healthcare | 5 |
| Industrials | 5 |
| Consumer Defensive | 4 |
| Communication Services | 3 |
| Energy | 2 |

Technology＋Semiconductors＝14/44＝31.8%。这不是 GICS 分类，也不代表 44 只均深度分析或最终提名。

证据：`config/unified_config.yaml:3103-3723`。

`subscription.static_watchlist_enabled:false` 是行情订阅开关，不会从 DA 删除静态名单；carrier 独立调用 `get_stock_codes()` 并传 `--symbols`，见 `ops/hermes/da_main_carrier.py:98-105,569-574,609-616`。

**边界：**不能由行业占比直接推导扩池、行业等权或收益损害。

### 3.2 Finnhub 扫描存在不可解释的覆盖损失

`app/plan/factor_engine.py`：

- `scan_pool=set(...)` 且未排序：`:164,178`。
- 只在前 30 次成功 quote 或 18 秒预检查处停止：`:179-186`。
- `api_calls` 仅在 quote 返回后递增，异常被吞：`:185-186,194-195`。因此 30 是成功响应计数，不是尝试请求计数。
- 只有命中时报告 `hits/api_calls`，没有 pool、attempted、failed、cap-skipped、deadline-skipped 和未扫清单：`:197-198,228-233`。
- docstring 声称“总超时20s”：`:112`；但 quota 等待和 SDK 请求都可能超过该边界，检查只发生在调用前，不是硬截止。

**已确认：**代码会产生不可解释的漏扫，且超时说明不准确。

**未确认：**最新生产漏扫了哪些标的、频率及 PnL 影响。

### 3.3 明确黑名单可从动态路径回入

- 静态加载应用 `risk.global_blacklist`：`app/utils/watchlist_manager.py:74-146`。
- QCOM/LCID/TME 在 `config/unified_config.yaml:2277-2282`。
- FactorEngine 主要检查自己的 ETF 排除集，而非统一黑名单：`app/plan/factor_engine.py:31-36,118-220`。
- QCOM 存在于 dragonhead 池：`app/utils/dragonhead_pool.py:199`。
- Playbook 合并明确“纯加法”，最终未复核黑名单：`app/playbook/pipeline_parts/runtime_context.py:74-108`。

**已确认：**存在禁止标的重新成为候选的条件路径。

**未确认：**实际重新入池、生成指令或成交。

普通静态 disabled 不应自动等同全局黑名单；只有明确禁止语义可无需新政策直接统一。

### 3.4 DA 上游漏斗在 Stage2 之前失明

仓外 `market_scanner.py`：

- 渠道异常统一转 `[]`，无法区分失败和健康空结果：`:148-177`。
- 质量门以裸 `continue` 删除标的：`:251-274`。
- 评分失败只写 stderr 并省略：`:76-93`。
- `candidates_total=len(scored)` 是损失后的数量：`:105-108`，随后成为 Stage2 输入。

现有 Stage2 和 selected→analysed→publishable→bridge 遥测较完整，不应重建；缺的是上游渠道、质量门和评分失败段。

---

## 4. 多空方向：层级归因而非“模型全空”

### 4.1 原始 checkpoint 方向

| 日期 | LONG | SHORT | SKIP |
|---|---:|---:|---:|
| 11-17 | 6 | 9 | 20 |
| 11-18 | 11 | 8 | 12 |
| 11-19 | 11 | 10 | 15 |
| 11-20 | 4 | 24 | 9 |
| 11-23 | 6 | 18 | 14 |
| 11-24 | 5 | 15 | 16 |

11-17—19 为 28L/27S，基本均衡；11-20、23、24 为 15L/57S，有向观点 79.2% 为 SHORT。

模型切换后前三天并未立即固定做空，因此“换模型后立即全空”不成立。市场、候选、模型/prompt 交互的独立贡献仍需对照材料。

### 4.2 11-25 正确漏斗

```text
35 raw：15 SHORT / 3 BUY / 17 SKIP
→ 17 bridge：15 SHORT / 2 BUY（META BUY 被 signal-gate BLOCK）
→ 费用后：14 SHORT / 2 BUY / 1 SKIP（RIVN 费用地板）
→ 确认后：13 SHORT / 2 BUY / 2 SKIP（MSFT SHORT 被移除）
→ 历史 RFC-217 LONG 权限后：13 SHORT / 0 BUY / 4 SKIP
→ 成交：3 SHORT（CAT/BABA/GS）
```

下游没有将 LONG 改成 SHORT，而是过滤或暂缓 LONG。浓度并非每层单调增加：费用和确认层也移除了 SHORT。

`docs/debates/DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md:83` 的“17＝15S/2B/1SKIP”混合了不同阶段；正确写法应使用上述逐层口径。本文只登记问题，未改写该 DEB。

### 4.3 当前与历史差异

当前 `auto_execute_flow.py:591-615` 对 NO_TRADE LONG/BUY 按轮暂缓，保持 PENDING，不写方向 SKIP 或 `skip_reason`；后续 regime 恢复可重新评估。因此历史“两个 LONG 被写终态 SKIP”只能用于解释 11-25 归档。

RISK_OFF 本身没有 LONG/SHORT 方向选择器：`app/playbook/market_regime.py:387-410`。组合方向偏斜修剪仍 disabled，不能把它说成当前集中度的成因。

### 4.4 不得下的结论

- 不得因近期 LONG 亏损删除多头。
- 不得因 SHORT 子窗盈利放大空头。
- 不得因 11-23 反弹就断言所有 SHORT 都错误；须逐标的核查事实时间、趋势假设、行业相对变化和失效条件。
- 不得把执行权限造成的表面集中解释为模型方向能力。

---

## 5. VIX、regime 与执行许可

### 5.1 当前测量与消费

- PRE_MARKET/TRADING 优先 Futu `US.VIXY` 并乘 0.85，回退其他来源：`app/data/market_data_router.py:1002-1042`。
- 换算为 `round(VIXY*0.85,2)`：`app/data/vix_source.py:71-83`。
- 盘前 `last_price` 是上一交易日收盘，真实盘前价在 `pre_price`；改读会改变门判据，属于 RFC-190 行为决策：`vix_source.py:212-230`。
- 全源失败时 regime 展示兜底 20，但 `value=None`、`vix_uncertain=True`，跳过 VIX 分项，不把 20 当真实观测：`app/playbook/market_regime.py:287-328,361-378`。
- 来源、caliber、as_of、provider quote_time/read_at 留痕已存在；但留痕不等于 freshness gate 或代理准确。

### 5.2 方向、波动风险与执行许可

当前 SPY 分项和 VIX 分项合成 regime：

- SPY/MA20、MA20/MA50、SPY/MA200：`market_regime.py:330-358`。
- VIX 20–22 连续计分已实施：`:131-145,361-384`。
- `>=5 RISK_ON`、`>=0 CAUTIOUS`、`>=-5 RISK_OFF`，否则 NO_TRADE：`:386-410`。
- RISK_OFF sizing 乘数 0.3，NO_TRADE 通常 0.0。

没有发现 VIX 直接决定个股方向。需要评审的是一个综合分数同时驱动 sizing 与全禁是否过度耦合，但 SPY 均线相关性和耦合设计本身不是已证明的 bug。

0.3 还要经过水位、股数取整、费用地板和其他上限，不能保证最终名义恰为原来的 30%。

### 5.3 VIXGuard 与重启边界

- 独立于 RFC-217，VIXGuard 对多空双向生效。
- `VIX>28` 新封锁；封锁后只有 `VIX<25` 持续 900 秒才解封：`app/risk/vix_guard.py:25-88`。
- 25–28 保持原 latched 状态；恰好 28 不新封锁，恰好 25 不解封。
- 状态只在内存：`:39-46`。若已在封锁状态，重启后 VIX 处于 25–28，原滞回封锁可能丢失。

重启边界是代码确认的条件问题，但未证明自然触发，也未确认项目是否接受该设计；不得借此直接新增持久状态库。

### 5.4 当前不建议

不直接改 VIX 来源、0.85、SPY 权重、NO_TRADE、28/25/900 秒或豁免高 VIX SHORT。先核同刻误差、阈值跨越、触发次数、假拦与保护效果；样本不足保持现状。

---

## 6. RFC-217 实验：新发现与既有边界

### 6.1 已确认正确的部分

- NO_TRADE preflight 仅在 SIM 实验 active 时放行，否则保持原禁：`risk_gates.py:273-311`。
- kill switch、risk-state、日亏等保护不被实验豁免。
- LONG 当前按轮暂缓，regime 恢复后重评。
- SHORT 仍经过原 trigger、quote/reprice、ATR、admission、chase、EntryConfirm、VIXGuard 和预算门。
- SIM-only active 检查包含 LIVE-capable 和 SHORT real-target live-arm 结构能力：`risk_gates.py:193-232`。
- 实验指令等待到其他 regime 成交仍保留 cohort 是既定口径，不应重划样本。

### 6.2 P0：预 sizing 指令绕过实验 sizing/cohort

当前执行代码：

```text
_inst_ps = inst.get("position_size") or 0
if _inst_ps <= 0:
    评估执行时 regime
    NO_TRADE SHORT 使用 0.3
    写 no_trade_short_exp
否则：
    原 _inst_ps 进入后续 reprice 参数
```

证据：`auto_execute_flow.py:1090-1092,1125-1144,1263-1269`。

预 sizing 是真实支持的指令形态：

- 生成时按当时 regime 计算 values：`app/playbook/pipeline.py:1650-1658`。
- `position_size` 写入 Instruction：`instruction_builder.py:343-350`。
- 生成器可返回正股数：`instruction_generator.py:588-590,629`。

因此，早期 RISK_ON/RISK_OFF 生成、尚未成交的 SHORT 若等到 NO_TRADE：

- RISK_ON 生成的较大股数可能保留；
- RISK_OFF 生成股数即使已含 0.3，cohort 仍可能缺失；
- 不能被“当前 NO_TRADE 不生成新行”排除，因为所需路径是早期 regime 的 PENDING 行。

**状态：代码缺陷确认；实际受影响生产成交未证。**

正确修复不能简单把旧股数再乘 0.3，否则 RISK_OFF 预 sizing 可能双重缩仓。应仅在 RFC-217 NO_TRADE 实验分支定义统一重算语义，保持其他 regime sizing 不变。

### 6.3 实验身份传播不完整

- 标记只写 instruction metadata：`auto_execute_flow.py:1129-1144`。
- 写入是 best-effort，返回值未用于阻止实验单；异常被吞。
- outbox 有 instruction_id 等关联：`app/playbook/db.py:2247-2260`。
- trade 有 `playbook_instruction_id`：`playbook_service.py:2446-2448`。
- outbox/trade 没有 denormalized `no_trade_short_exp`。

间接 ID 关联可以支持归因，但必须成为经过测试的报告合同；不能仅凭设计宣称当前 cohort 覆盖完整。若标记持久化失败仍成交，实验评价会出现不可归因样本。

### 6.4 未来 SHORT live-arm 前置条件

`_mirror_instruction_to_live_if_allowed` 只重跑共享 LIVE gate，不读取 `no_trade_short_exp`：`risk_gates.py:646-659`。成功 SIM 执行后镜像调用使用原始内存 `inst`：`auto_execute_flow.py:2184-2224`，而标记可能只合入数据库。

因此未来开放 SHORT real-target 前必须：

1. 在镜像边界按持久化指令身份/metadata 排除实验单；
2. 覆盖 NO_TRADE→RISK_OFF、缺 metadata、坏 metadata 和内存未刷新；
3. 不假设原始 `inst` 已含 DB 标记。

当前静态路径未见 SHORT real-target 能力；这是未来授权前置条件，不是当前实盘泄漏证明。

---

## 7. DA 输入与分析师—挑战者合同

### 7.1 七类输入核查结果

| 项目 | 当前结论 | 证据与边界 |
|---|---|---|
| 资金 `code` | **确认，但准确对象是 capital_distribution** | 渲染要求 `code==0`：`llm_synth.py:623-642`；provider 写 ticker：`get_capital_distribution.py:44-58`。35 份 11-25 checkpoint 均未渲染。capital_flow fallback 不同时存在该 gate，旧注释错误 |
| 短 K 历史 | **零填充代码确认；生产“短历史被当震荡”样本未证** | `llm_synth.py:591-621` 对不足窗口写 MA/vol/chg=0，仍标“60日K线” |
| 健康空财报 | **确认** | 健康空在 `data_collector.py:875-885`；只在 `next_e` 非空时渲染：`llm_synth.py:998-1022`。33/35 份归档复现省略 |
| 新闻时间 | **主要混淆已修；Finnhub 展示精度仍退化** | Futu/research 优先 publish_time；Finnhub prompt 只显示 UTC `%m-%d`，丢失时分和明确时区：`data_collector.py:251-260`、`llm_synth.py:704-717` |
| 跨语言重复 | **缺能力确认；实际重复对/比例未证** | `_source_key` 只支持 ID/URL/exact title：`llm_synth.py:388-403`；EventStream 也无翻译故事身份。不得把 141 个采集层 exact duplicate extras 写成 141 个 prompt 重复 |
| 采集≠prompt覆盖 | **确认** | `_quality` 可把空容器算成功：`data_collector.py:1433-1438`；严格分类看 collected，不验证相应 section 是否实际渲染：`llm_synth.py:1153-1224` |
| EventContext | **接线已修；当前是上游陈旧** | 注入和 freshness guard 已存在：`llm_synth.py:2333-2362`、`da_prompt_injection.py:298-314`。35/35 份 11-25 为 `injected=false/reason=brief_date_mismatch`；审查时 latest 仍为 11-24 |

### 7.2 分析师与挑战者

- 挑战者与分析师从同一 base `data_text` 出发，但 `_challenger_data_view` 只保留白名单段落，新闻/研报/社区截断 600 字符：`llm_synth.py:2558-2602`。
- `数据源状态` 不在白名单；`evidence_gaps` 不保证在分析师输出截断后保留。
- 没有代码强制“材料视图不完整时挑战者不得 AGREE”。所以 AGREE 不能解释为完整共享事实或缺资料情况下的有效赞同。
- 这不等于应立即新建硬 AGREE 门；先补可见性、留痕和语义，再评估行为变化。

### 7.3 DISAGREE/ABSTAIN 与 confidence

- Canonical verdict 分类和 no-lift 修复存在：`debate_state.py:31-57`、`signal_gate.py:397-424`。
- `challenger_direction=SKIP` 可规范为 ABSTAIN，即使 raw verdict 是 DISAGREE；raw 仍保留，但只读 canonical 的消费者会丢失显式反对差异。
- DB raw_instruction 发布 `debate_outcome`、`confidence_semantic`、`decision_conf`，未完整发布 canonical verdict state：`pipeline.py:2483-2520`。
- adapter 虽用概率式描述，实际为启发式 0.50 基线、+0.20 同意、−0.15 真反向，不是校准概率：`adapter.py:191-254`。
- 不得把 confidence 当胜率、加仓依据或新硬门；C12 仍适用。

---

## 8. 已确认问题与最小修复方案

| 优先级 | 调整 | 推荐状态 | 最小范围 | 行为影响 | 验收 | 回滚 |
|---|---|---|---|---|---|---|
| P0 | 动态黑名单统一守卫 | 推荐，正确性 | 复用 watchlist 谓词，动态发现及最终合并边界；保留持仓管理 | 禁止标的不再进入新候选 | QCOM/LCID/TME 各来源均被拦；普通 disabled 不误伤 | 独立守卫开关恢复旧合并；不改历史 |
| P0 | RFC-217 预 sizing 修复 | 推荐，独立小批 | `auto_execute_flow.py`＋实验契约测试 | 仅 NO_TRADE 实验单 | RISK_ON/RISK_OFF 预 sizing 到 NO_TRADE 后 sizing/cohort 正确；无双重 0.3；其他 regime 不变 | 实验开关 false 恢复 NO_TRADE 全禁 |
| P0 | 资金分布正确消费 | 待验证；首选唯一正式输入变量 | `llm_synth.py` schema/renderer＋冻结 raw 测试＋独立开关 | 改变 DA 输入，可能改变方向 | 八字段、零值、缺值、错误响应分别正确；单变量 A/B | 新开关恢复旧渲染；不得借用 DA_INPUT_FULL |
| P1 | 扫描与 DA 上游漏斗 | 推荐，record-only 先行 | `factor_engine.py`、`market_scanner.py`、既有报告 | 首轮不改变候选选择 | pool=成功+失败+cap/deadline skipped；上游剔除逐标的可解释 | 移除遥测，不动选择逻辑 |
| P1 | 实验身份与镜像排除 | 推荐；live 前置必须 | cohort 传播/报告契约、镜像入口和异常测试 | SIM 主要归因；未来 LIVE 安全隔离 | 全链可关联；标记失败不冒充完整 cohort；跨 regime 不镜像 | 归因可撤；SHORT live-arm 前不得撤销排除 |
| P1 | EventBrief freshness | 推荐修上游 | 定位生产者/调度，恢复同日 brief | 恢复既有输入，不放宽 guard | 同日可注入；旧 brief 仍拒绝并留因 | 回滚生产者调度，保留 freshness guard |
| P1 | 短 K、健康空财报、覆盖口径 | 合理但必须分批 | 各自 renderer＋契约测试 | 均可改变正式输入或解释 | 实际 bar 数/N/A；健康空显式；按渲染统计覆盖 | 各自独立开关，不捆绑 |
| P2 | 挑战者可见性和 verdict 留痕 | 待验证，先 record-only | 保留数据源状态/evidence_gaps，发布 canonical verdict | 可能影响反证解释；先不建门 | raw verdict→canonical→gate→adapter→发布可追踪 | 回滚展示/留痕，保留 no-lift |
| P2 | VIXGuard 重启语义 | 待独立裁决 | 先写重启契约测试并确认设计 | 可能影响 25–28 重启后的执行许可 | 封锁态跨重启行为符合已批语义 | 未批准前保持现状 |
| 暂不 | 扫描轮转、VIX 换源/权重/阈值、方向政策 | 当前不推荐 | — | 均为行为变更 | 先取得覆盖、同刻测量和成本后证据 | 保持现状 |

**推荐顺序：**黑名单和 RFC-217 sizing 正确性 → record-only 漏斗和身份 → EventBrief freshness → 若批准一个正式输入变量，优先资金分布。不得把所有输入修复捆绑上线。

---

## 9. 收益解释：能期待什么，不能承诺什么

### 9.1 当前官方归档读数

| 人群 | n | 独立日 | 毛 bps | 费 bps | 净 bps | 净额 | 结论 |
|---|---:|---:|---:|---:|---:|---:|---|
| DA 整窗 | 146 | 17 | −15.189 | 9.6479 | **−24.8369** | −$1,587.23 | NOT_MET |
| qwen4-max post | 39 | 6 | −6.4763 | 10.9915 | **−17.4678** | −$259.74 | 功效不足，MDE80=80.0408bps |

post 日聚类 80% CI 为 `[−45.2366,+27.9932]bps`，包含 0；不能用 39 笔证明新模型改善或恶化。

RFC-217 归档：10 条带标记指令、3 笔成交、仅 1 笔已平 CAT `+$9.96`，2 笔未平。当日全部已平 `+$215.00` 中旧仓贡献 `+$205.04`，不可归实验。

费用主要为 `fees_est`，借券、融资、数据和模型运营成本未统一计入；成交价已含滑点，不得重复扣。

### 9.2 修复收益分类

1. **风险与正确性收益**：黑名单、实验 sizing、LIVE 隔离。主要避免不该发生的暴露和尾部风险，不能预写增收金额。
2. **测量收益**：漏斗、实验身份、VIX 时点。主要避免错误归因，本身不是 alpha。
3. **待验证判断收益**：资金分布、短 K、财报、事件材料。可能改善模型事实基础，也可能改变方向或增加噪声，必须单变量验证。

覆盖更多、JSON 更合规、方向改变、confidence 更高、成交更多，都不是收益成功。

---

## 10. 验证与收益计划

### 10.1 一致数据 manifest

使用 SQLite backup API 或批准的一致事务导出，不直接复制正在写入的主 DB。至少保留：

- repo HEAD、仓外 DA SHA、prompt hash、模型和参数；
- decision/instruction/outbox/trade 身份；
- 原始及执行时 regime、生成和最终股数；
- RFC-216、RFC-217 和实验配置版本；
- 入场/退出日、已平/未平、费用来源与未知项。

### 10.2 正确性补丁测试

**动态黑名单**

- 每个动态来源分别注入 QCOM/LCID/TME，最终新仓候选均不得包含；
- 普通 disabled 可发现标的不得被全局黑名单逻辑误伤；
- 持仓管理和退出链不因候选过滤失去标的。

**RFC-217 预 sizing**

- RISK_ON 预 sizing SHORT→NO_TRADE：按实验规则重算并写 cohort；
- RISK_OFF 预 sizing SHORT→NO_TRADE：不得双重乘 0.3，cohort 完整；
- 缺 size 的现有路径结果不回归；
- 非 NO_TRADE、LONG、MOM 和非实验路径 sizing 不变；
- 标记写失败时不得将成交宣称为完整实验 cohort；
- NO_TRADE→RISK_OFF 镜像排除覆盖持久化、缺失和坏 metadata。

**扫描漏斗**

- 成功、失败、cap、deadline 分类总和与母池一致；
- 异常不得被计成健康空；
- 第一阶段不得改变 quote cap、顺序、模型、prompt 或下游门。

**正式输入**

- 冻结 raw 的合法、完整零值、部分缺失、非法值、布尔值、非有限值和真实错误响应；
- 短 K 按实际 bar 数显示 N/A，不填 0；
- 健康空财报与采集失败可区分；
- 采集覆盖与渲染覆盖分别报告。

### 10.3 RFC-217 原实验不改判据

继续 RFC-217 已批准的 **n≥30 或激活后 10 个交易日**及原硬停条件：

- cohort 以实验指令身份确定，不以成交时 regime 重划；
- 未平仓单列，不计零；
- 旧仓、qwen post、RFC-216 子组分开；
- 只回答“获准 NO_TRADE SHORT 是否有成本后正期望”，不能回答“拦 LONG 是否增利”。

### 10.4 若另批资金分布单变量实验

以下是**建议方案，未批准**：

- 对照：同一冻结 raw、模型、prompt、调用参数和候选母池，仅资金分布渲染不同。
- 搬运层：schema 和渲染正确。
- 机制层：判断变化超过模型自身随机性；采样参数不能代替随机性基线。
- 经济层：可成交、完整成本后的配对净 bps 增量。
- 建议样本门：n≥120 且至少 20 个独立 ET 日；不足只报 UNDERPOWERED，不得加仓或增频补样。
- MDE：按新 cohort 基线在运行前预注册，不照搬 post 的 80.0408bps。
- 建议 2026-12-21 仅作中期检查；正式经济裁决在样本门满足后的周度批量裁决日。该日期未获批准，不能自动顺延现有 OKR 或实验。
- 停止：身份不完整、成本不清、混入第二变量、预算超限、既有风险闸触发、效应低于 MDE。低于 MDE 只能写不可分辨。

每组必报：n、独立 ET 日、名义额、毛/费/净 bps 和金额、费用覆盖、均盈/均亏/胜率、A11、最大单笔/最大日贡献、日聚类 t/CI/MDE、版本完整度。DA/EXT、whole/pre/post、RFC-216、RFC-217、LONG/SHORT、已平/未平分别披露。

---

## 11. 不做清单

- 不强配多空 50/50，不凭近期 SHORT 盈利放大空头，不凭 LONG 亏损删除多头。
- 不为行业等权机械扩池，不把所有静态 disabled 当全局黑名单。
- 不优先增加 API、分析额度、第三模型或思考预算。
- 不把 VIX 当个股方向，不以高 VIX 豁免 SHORT 风险门，不直接调 regime/VIX 阈值。
- 不恢复严格原价、entry-archetype 路由、方向偏斜硬砍或 UE 直接打单。
- 不调 chase/VWAP/SL/TP/隔夜，不按 confidence 加仓，不增仓位或频率追样本。
- 不清 UNKNOWN、peak/tripped/cohort，不扩大实盘权限。
- 不用本 RFC 复活 C11/C12/C14/C15/C16/C17/C18 已关闭轴；重开须满足各轴原条件。

---

## 12. 授权、回滚与实施状态

### 12.1 本 RFC 不自动授权

- 修改 repo 生产代码或测试；
- 修改仓外正式 DA 源码、prompt 或输入开关；
- 付费 LLM、外部 VIX 对照或网络调用；
- 新实验、数据库写入、历史标记回填或订单操作；
- 提交、推送、部署或重启。

### 12.2 回滚原则

- 正确性补丁必须各自独立开关或独立 revert，不与其他行为变量捆绑。
- RFC-217 可由既有实验开关 false 恢复 NO_TRADE 全禁；已成交、已注入候选和历史 metadata 不自动撤销。
- 不得为回滚而清 UNKNOWN、释放订单锁、回写已认领/已成交指令或重置风险状态。
- 未来 SHORT live-arm 前，镜像实验排除不得以“回滚”为名撤除。

### 12.3 实施记录

**当前：仅完成只读评审和本文档；未实施任何生产修复，未提交、未推送、未重启。**

待独立 Agent 复查后，由用户逐项裁决。复查结论应追加到本文后续章节或关联 DEB，并同步 `handoff.md`，不得静默改写本轮历史断言。

---

## 13. 独立复查结论（2026-11-27 · 只读证伪式复查 · 未批准实施）

- 复查性质：按 §0 与多 Agent 宪章做**证伪式独立复查**，逐项重读当前源码、只读复算归档数据库/checkpoint、核对 git 历史；不重写 §§0–12 历史断言，仅追加本节。
- 边界：**全程只读**。未改任何代码/仓外 DA 源码/配置/prompt/数据库/订单；未付费调用、未外联现货 VIX、未跑实验、未提交/推送/部署/重启。DB 一律 `sqlite3.connect('file:...?mode=ro',uri=True)` + `PRAGMA query_only=ON`；Python 用 `./venv/bin/python`。
- 与草案关系：草案 §§0–12 的源码锚点与归档读数**绝大多数独立复现成立**；唯一需要实质修正的是 §6.2 对 DA 路径「预 sizing 实验泄漏」的**可达性夸大**（见 §13.2-A）。其余为口径精化与 BLOCKED 标注。

### 13.1 基线核对（§0.1）——确认

- HEAD=`078966c983951f3372e50fb976a42519ea9e2e77`；`git merge-base --is-ancestor b914d65c… HEAD` 成立，b914d65c 确为祖先。
- 工作区：`M DEB-20261125…`（其他会话 WIP，未触碰）＋`M handoff.md`＋`?? RFC-218`；`git status --porcelain -- app/ scripts/ ops/ tests/ config/` 为空。
- 仓外 DA 四文件 SHA256 与 carrier SHA **逐字匹配 §2.2**（data_collector `8413c4a6…`、llm_synth `705ff7c2…`、pipeline `4f38df79…`、market_scanner `98ff79fa…`、carrier `b7a873cb…`）。哈希只固定本次阅读对象，不证明运行进程一致（无 runtime manifest 绑定，见 §13.7）。
- 运行载体：`systemctl show ftseek-us` → MainPID=**147844**、ExecMainStartTimestamp=**2026-11-27 11:00:20 CST**、active/running、NRestarts=0；`config/unified_config.yaml:1154 execute_mode=futu_simulate`。handoff 旧条「11:00:23」与 systemctl「11:00:20」差 3 秒，属记录精度，不影响结论。

### 13.2 三大关键断言证伪（§0.2）

#### A. 预 sizing SHORT 绕过 RFC-217 sizing/cohort —— **代码缺陷确认；DA 路径「实验泄漏」可达性驳回/夸大**

**代码缺陷确认（当前源码）：**
- `auto_execute_flow.py:1090-1091` `_inst_ps = inst.get("position_size") or 0; if _inst_ps <= 0 and _new_sl > 0:` 把**整段执行时 regime 评估＋NO_TRADE SHORT 0.3＋cohort 标记**包在兜底分支内；`:1269` `"position_size": _inst_ps or 1` 把预 sizing 原样透传。`risk_gates.no_trade_short_regime_mult:235-239` 实验生效且 SHORT→0.3，否则 0.0。缺陷结构属实。

**DA 生成端结构性不预 sizing（独立证伪草案 §6.2 的 DA 场景）：**
- 仓外 DA 直写 `_inst` dict（`pipeline.py:2417-2529`，save 于 :2529）**顶层无 `position_size` 键**，仅 `raw_instruction.position_size_hint`（字符串）；:2407 注释明示「position_size 执行时才定」。
- `db.save_instruction:594-609` 的 admission 预判估算写在**副本** `_inst_est` 上，只落 `raw_instruction.admission.precheck`（`estimated=True`），**不写顶层 position_size、不写列**。故 DA 生成时列恒 NULL。
- 只读 DB 复算：11 月 DA SHORT **117 条 raw_instruction.position_size 全 None**；列分布 `>0:101 / NULL:16`；**全历史 SHORT raw_instruction.position_size>0 = 0**。101 条列>0 中 **99 带 reprice_audit**（执行时兜底回写）、57 entry_fill、44 terminal_block_reason；16 条 NULL 均为早期拦截未执行（extreme_gap/vol_gate_block/knife_above/thesis_drift/deferred/vwap_gate）。
- **11 月 DA SHORT 生成 regime 分布：cautious 22 / no_trade 14 / risk_off 81，RISK_ON = 0。** 草案 §6.2「RISK_ON 生成较大股数保留」场景在 11 月 DA 无 substrate。
- RFC-217 cohort 10 条标记 raw_ps 全 None；实验窗（11-25）三笔成交 CAT/GS/BABA **均带 cohort 标记**（走正确兜底路径）。复算「已成交 NO_TRADE DA SHORT 缺 cohort」= **空集**，未观察到绕过实际触发。
- 11 月 SHORT 来源 100% DA；topup_pool 全历史 9 条止于 2026-08-05（8/6 停）；generate 路径预 sizing SHORT（universe_expansion/intraday_reentry）仅存于历史（9 月），11 月无。

**精确可达性裁定（修正「结构上不可达」的过强表述）：**
- DA **生成端**确实永不预 sizing（结构性）；但绕过仍**潜伏可达**于两条路径：① repo generate 路径（`instruction_generator.py:588-629` 生成时算仓→`instruction_builder.py:348` 写列）若预 sizing SHORT 留到 NO_TRADE；② **跨轮携带**——`get_instructions_by_date` 用 `SELECT *` 把列读回 `inst["position_size"]`，执行回写可使 `executed=0` 行列>0（实测 5304 TSLA cautious、5468 ARM risk_off 两条 exec=0+列>0），后续 NO_TRADE 轮即可绕过 regime 复评/cohort。故不能断言「DA 结构上不可达」，准确表述是「DA 生成端不预 sizing；绕过潜伏于 generate 路径与跨轮携带；当前实验窗未观察到受害成交」。
- **裁定：代码缺陷确认；草案 §6.2 把 DA 路径描述为 P0「实验泄漏」属夸大/驳回**（DA 不生成预 sizing SHORT、11 月 0 RISK_ON、实验窗三笔成交 cohort 完整）。建议降级为**潜伏纵深防御（P1/P2）**，保留最小修复（仅 NO_TRADE 实验分支统一重算、不得对 RISK_OFF 预 sizing 双重乘 0.3、其他 regime 不变），并补 generate 路径与跨轮携带用例。**实际受影响生产成交：BLOCKED（未观察到，亦无法对未快照历史证无）。非收益假设。**

#### B. 黑名单动态回入 —— **确认，并强化**

- 静态侧 `watchlist_manager._get_global_blacklist:74`（读 `risk.global_blacklist`、剥 US. 前缀）＋`_apply_exclusions:88` 过滤黑名单＋自管；`get_enabled_stocks` 应用之。
- 动态侧 `factor_engine.py` **零 `global_blacklist` 引用**；`_EXCLUDE_SYMBOLS:33-36` 仅 ETF（SPY/QQQ/DIA/IWM/XL*），`_EXCLUDE_PREFIXES={"US.HK."}`；`scan_pool=set(MOVER_POOL)` 未排序（:164）；`api_calls>=30 or t>18` 停止（:179），`api_calls` 仅 quote 返回后递增（:186）→30 是成功计数。
- 合并侧 `runtime_context._get_watchlist:96` `merged=list(set(wl)|set(dynamic_pool))`「纯加法」，仅滤 #SIM/双前缀，**无黑名单复核**。QCOM 在 `dragonhead_pool.py:199`。
- **下游 Playbook 无黑名单闸**：`grep global_blacklist|pre_trade_check app/playbook/` 为空（旧 `app/pipeline/stages/risk_stage.py` 已冻结）。
- **强化（口径修正）**：`config:2277 global_blacklist` 实为 **5 条**——US.QCOM/US.LCID/US.TME/**HK.00700/US.HK.00700**；草案 §3.3 只列 3 条。修复闭包须覆盖全 5 条（剥前缀后 HK.00700 与 US.HK.00700 归一为 HK.00700；factor_engine 的 US.HK. 前缀排除可挡腾讯，但 QCOM/LCID/TME 无动态拦截）。普通静态 disabled 不等同全局黑名单。
- **裁定：确认（正确性，非收益）。生产实际回入/生成指令/成交：BLOCKED。**

#### C. capital_distribution 因 ticker/`code==0` 错配无法渲染 —— **确认**

- provider `get_capital_distribution.py:45` 写 `"code": code`（ticker 字符串）；`llm_synth.py:623` 渲染门 `cap_dist.get("code") == 0` 恒 False → 正式「## 资金面\n- 主力净流入:」块永不渲染；`elif capital_flow` fallback 无此门。
- 只读复算 11-25 35 份 raw checkpoint：**正式块 0/35**、fallback「## 资金面 (分时)」**34/35**、无资金面段 1/35。RIVN 文中「主力净流入」经上下文核对为**A 股研报串文**（英集芯 688209/托伦斯 301583/芯原股份 688521），非本标的正式渲染。草案「35 份均未渲染」**正确**。
- **裁定：确认（正式输入正确性，属收益假设相邻、待验证；须单变量 A/B，不得包装成纯遥测）。**

### 13.3 方向漏斗复算（§0.3）——确认，逐层匹配 §4.2

- 11-25 DB 指令：**13 SELL_SHORT + 4 SKIP**。4 SKIP = LLY/MRK（`NO_TRADE_EXP_LONG_BLOCKED`，original_direction=LONG）＋MSFT（`entry_confirm_reversed`，orig SHORT）＋RIVN（`admission: PRICE_BELOW_FEE_FLOOR(11.35<17.33)`，orig SHORT）。
- bridge BUY = LLY(conf 0.35)/MRK(0.5)；bridge SHORT 15 标的（META BUY 被 signal-gate BLOCK，未入 bridge）。
- 成交 3 笔 SHORT：BABA 47、CAT 7、GS 5（executed=1）。
- 逐层：35 raw(15S/3B/17SKIP)→17 bridge(15S/2B)→费用后 14S/2B/1SKIP(RIVN)→确认后 13S/2B/2SKIP(MSFT)→历史 LONG 权限后 13S/0B/4SKIP→成交 3S。**与 §4.2 完全一致**；下游过滤/暂缓 LONG，未把 LONG 改 SHORT；费用/确认层也移除 SHORT，浓度非单调。DEB:83「17=15S/2B/1SKIP」混层计数问题成立（本批复核未改写该 DEB）。

### 13.4 当前 HEAD 与 11-25 历史区分（§0.4）——确认，git 可证

- 当前 `auto_execute_flow.py:607-615`：`if _nt_short_exp and direction not in (SELL_SHORT,SHORT): _record_mom_defer(...,"NO_TRADE_EXP_LONG_BLOCKED",...)`。`_record_mom_defer:138-160` 明示「2026-11-12 起**不再写 skip_reason**」，只发 `exec_gate_audit(transient=True)`，指令保持 PENDING。
- git 实证：`9532275dc`（2026-11-26 14:04「保留NO_TRADE轮暂缓LONG的后续评估资格」）diff 把 `db.mark_instruction_guardrail_skip(inst["id"],"NO_TRADE_EXP_LONG_BLOCKED")`（写终态 SKIP）替换为 `_record_mom_defer(...)`。`ad44b0d34`（2026-11-25 23:23）为实验落地版。
- 故 11-25 DB LLY/MRK 的 `skip_reason=NO_TRADE_EXP_LONG_BLOCKED` 终态行属**历史行为**（ad44b0d34 的 mark_instruction_guardrail_skip）；当前 HEAD 按轮暂缓、不写终态。草案 §4.3 区分**准确**。`_nt_short_exp:462-473`=实验 active AND NO_TRADE。RISK_OFF 无方向选择器、方向偏斜修剪 disabled 均属实。

### 13.5 其余锚点抽查（§0.5）——确认

- §3.1 静态池：56 股/44 启用/12 disabled；启用行业 Technology 8/Semis 6/Consumer Cyclical 6/Financial 5/Healthcare 5/Industrials 5/Consumer Defensive 4/Communication 3/Energy 2，Tech+Semis=14/44=31.8%，逐字匹配；`subscription.static_watchlist_enabled=False`。
- §5.1 `vix_source.py:71-83` VIXY_CONTANGO_FACTOR=0.85、`round(p*0.85,2)`、无效→None 绝不兜底；market_regime 全源失败兜底 20 但 value=None/vix_uncertain=True。
- §5.2 `market_regime.py:386-410` 阈值 >=5 RISK_ON/>=0 CAUTIOUS/>=-5 RISK_OFF/else NO_TRADE，乘数 1.0/0.5/0.3/0.0。
- §5.3 `vix_guard.py:25-88` BLOCK>28/UNBLOCK<25 hold 900s；恰 28 不新封锁、恰 25 不解封；状态仅内存（singleton `__init__`），重启丢滞回锁存。
- §6.1 `risk_gates.py:193-232` SIM-only active（live-capable/live-arm→False，异常 fail-close）；`:273-311` preflight NO_TRADE 实验 active 放行本轮、否则整轮拦；kill/risk-state/日亏不豁免。
- §6.3/§6.4 cohort 标记 best-effort（try/except pass）只写 instruction metadata；outbox 有 instruction_id、trade 有 playbook_instruction_id，无 denormalized no_trade_short_exp；`_mirror_instruction_to_live_if_allowed:646-659` 不读 no_trade_short_exp，镜像调用用原始内存 `inst`（auto_execute_flow:2184+）；当前无 SHORT real-target 能力。
- §7.1 短 K 零填充（llm_synth:591-621）、健康空财报（data_collector:875-885 vs llm_synth:998-1022 仅 next_e 非空渲染）、EventContext 35/35 injected=false/brief_date_mismatch 均经草案探针复现；§7.2 挑战者白名单截断 600、「数据源状态」不在白名单；§7.3 adapter 启发式 0.50/+0.20/−0.15、signal_gate DISAGREE+SKIP no-lift。§3.4 market_scanner candidates_total=len(scored)、渠道异常→[]。

### 13.6 逐项建议裁决（§0.5 / §8）

| 草案项 | 裁定 | 最小修改闭包 | 行为影响 | 测试要点 | 回滚 | 收益假设? |
|---|---|---|---|---|---|---|
| P0 动态黑名单统一守卫 | **确认**（正确性） | 复用 watchlist 谓词于动态发现＋最终合并；覆盖全 5 条；保留持仓/退出链 | 禁止标的不再进新候选 | 各来源注入 QCOM/LCID/TME 均拦；普通 disabled 不误伤；退出链不失标的 | 独立守卫开关恢复旧合并 | 否 |
| P0 RFC-217 预 sizing 修复 | **代码缺陷确认；DA「实验泄漏」可达性驳回/夸大→降级潜伏纵深防御 P1/P2** | 仅 NO_TRADE 实验分支统一重算＋cohort；补 generate 路径与跨轮携带用例 | 仅 NO_TRADE 实验单 | RISK_ON/RISK_OFF 预 sizing→NO_TRADE 正确、无双重 0.3；跨轮携带；缺 size 不回归；非 NO_TRADE/LONG/MOM 不变 | 实验开关 false 恢复全禁 | 否 |
| P0 资金分布正确消费 | **确认**（正式输入，待验证） | llm_synth schema/renderer＋冻结 raw 测试＋独立开关 | 改变 DA 输入，可能改方向 | 八字段/零值/缺值/错误响应分别正确；单变量 A/B | 新开关恢复旧渲染；不借 DA_INPUT_FULL | **是（相邻，待验证）** |
| P1 扫描＋DA 上游漏斗 | **确认**（record-only 先行） | factor_engine/market_scanner 既有报告补遥测 | 首轮不改候选选择 | pool=成功+失败+cap/deadline skipped；异常≠健康空 | 移除遥测，不动选择 | 否（测量） |
| P1 实验身份＋镜像排除 | **确认**（测量/安全；live 前置必须） | cohort 传播/报告契约＋镜像入口排除＋异常测试 | SIM 归因；未来 LIVE 隔离 | 全链可关联；标记失败不冒充完整 cohort；跨 regime 不镜像 | 归因可撤；SHORT live-arm 前不得撤排除 | 否 |
| P1 EventBrief freshness | **确认**（恢复输入） | 定位生产者/调度，恢复同日 brief | 恢复既有输入，不放宽 guard | 同日可注入；旧 brief 仍拒并留因 | 回滚生产者调度，保留 guard | 否（恢复输入） |
| P1 短 K/健康空/覆盖口径 | **确认代码；生产样本 BLOCKED（待验证）** | 各自 renderer＋契约测试，分批 | 均可改正式输入/解释 | 实际 bar 数/N/A；健康空显式；按渲染统计覆盖 | 各自独立开关，不捆绑 | 待验证 |
| P2 挑战者可见性＋verdict 留痕 | **确认**（record-only，待验证） | 保留数据源状态/evidence_gaps，发布 canonical verdict | 可能影响反证解释；先不建门 | raw→canonical→gate→adapter→发布可追踪 | 回滚展示/留痕，保留 no-lift | 待验证 |
| P2 VIXGuard 重启语义 | **确认代码条件问题；自然触发 BLOCKED；设计未裁** | 先写重启契约测试并确认设计 | 可能影响 25–28 重启后执行许可 | 封锁态跨重启符合已批语义 | 未批准前保持现状 | 否 |
| 暂不（扫描轮转/VIX 换源权重阈值/方向政策） | **同意暂不** | — | 均行为变更 | 先取得覆盖/同刻测量/成本证据 | 保持现状 | — |

### 13.7 BLOCKED 清单（§0.6）——不得以归档冒充当前生产实测

- 无一致数据库快照（仅只读时点读，未用 backup API/一致事务导出）→ 跨表精确时点归因 BLOCKED。
- 无同刻现货 VIX 对照（VIXY×0.85 为经验代理，误差未证）→ 代理准确性/阈值跨越 BLOCKED。
- 黑名单生产实际回入/生成指令/成交：未证（结构路径确认，命中 BLOCKED）。
- 预 sizing 实际受影响成交：未观察到（DA 生成端不预 sizing；跨轮携带/generate 路径潜伏）；对未快照历史无法证无 → BLOCKED。
- 短 K「短历史被当震荡」生产样本、跨语言重复对/比例：未证 → BLOCKED。
- VIXGuard 自然触发（25–28 重启丢锁存）：未证 → BLOCKED。
- 无运行进程 manifest 绑定 DA SHA → 「当前运行进程与阅读对象一致」BLOCKED。
- 任何修复后收益增量：未证；正确性修复≠正期望。

### 13.8 复查结论与下一步

- **总体：草案问题筛选与最小修复方向大体成立、源码/归档锚点独立复现；唯一实质修正是 §6.2 对 DA 预 sizing「P0 实验泄漏」的可达性夸大（应降级潜伏纵深防御），以及 §3.3 黑名单 5 条口径精化。资金分布属正式输入正确性、收益假设相邻，须单变量验证，不得当纯遥测。**
- 本节为只读复查落档（宪章「不落档=没发生」＋§12.3）；**未实施任何生产修复，未提交、未推送、未重启**。
- 下一步：由用户按 §13.6 逐项裁决；裁决前不得修改代码/仓外 DA/配置/prompt、不得付费调用/写库/下单/跑实验/提交推送/部署重启。RFC-217 原 n≥30 或激活后 10 交易日判据与硬停不变；资金分布 n≥120/20 日及 2026-12-21 中期检查仍为未批准建议。

---

## 14. 二次独立确认（2026-11-27 · 对 §13 再复核）

### 14.1 总体裁定

**接受 §13 的主要修正：原草案将预 sizing 列为当前 DA 的 P0 实验风险，优先级确实偏高，应降为潜伏的跨轮/其他生成路径防御问题。** §13 对动态黑名单、资金分布、方向漏斗及当前/历史 LONG 行为的复查结论，经再次源码和只读数据复核成立。

本节只追加二次确认，不改写 §§0–13；未修改生产代码、仓外 DA、配置、prompt、数据库或订单，未提交、推送、部署或重启。

### 14.2 预 sizing：降级成立，但不是“DA 永远不可达”

独立复跑只读探针得到：

- 11 月 DA SHORT 指令 117 条，`raw_instruction.position_size` 为 None 的 117/117；列 `position_size>0` 为 101、NULL 为 16。
- 全历史 SHORT 的 `raw_instruction.position_size>0` 为 0；11 月 SHORT 来源均为 DA。
- RFC-217 十条 cohort 标记指令的 raw position_size 全为 None；11-25 三笔成交 CAT/GS/BABA 均有 cohort。
- 仓外 DA `_inst` 顶层确实不写 position_size（`pipeline.py:2416-2529`）；`db.save_instruction` 的 admission 估算使用 `_inst_est` 副本，只把预估写入 raw precheck，不写生成列（`app/playbook/db.py:599-623`）。

因此，“DA 生成端直接预 sizing 导致当前 P0 事故”的证据不成立，§13 的降级合理。

但潜伏路径仍真实存在：

- `get_instructions_by_date` 使用 `SELECT *`，下一轮会把已回写的正 position_size 读回：`app/playbook/db.py:880-887`。
- reprice 在最终成交/拦截前即可调用 `update_instruction_tracking` 写 position_size：`auto_execute_flow.py:1205-1214,1263-1286`；写入白名单和 UPDATE 见 `db.py:978-1015,1999-2005`。
- 因而某轮 RISK_OFF/CAUTIOUS 已回写正股数、但未成交的 DA 指令，后续轮到 NO_TRADE 时仍可能跳过 `_inst_ps<=0` 内的实验 sizing/cohort。11 月 5304 TSLA、5468 ARM 为 `executed=0` 且列>0 的实际形态例子；它们不是受害成交证明。

**最终口径：代码缺陷＋潜伏跨轮可达性确认；当前实验窗未观察到受害成交；优先级降为 P1 纵深防御，不应写成 DA 结构上绝对不可达，也不应保留原 P0 事故措辞。**

### 14.3 §13 探针的一处证明力限制

`/tmp/rfc218_reach.py` 的输出中，全部 57 笔 11 月已成交 DA SHORT 的 `sizing_regimes=[]`，样本 `regime_reads=null`。因此该脚本打印的 `EXECUTED NO_TRADE DA SHORT lacking cohort: []` **不能单独作为“已核执行时 NO_TRADE 且无缺标”的证据**，因为其 NO_TRADE 分类输入为空。

二次直接只读查询 11-25 三笔成交得到：

| id | 标的 | 指令 market_regime | cohort | reprice_audit |
|---:|---|---|---|---|
| 5765 | CAT | `risk_off` | 有 | 有 |
| 5766 | GS | `risk_off` | 有 | 有 |
| 5774 | BABA | `risk_off` | 有 | 有 |

准确结论是：

1. 三笔实验归属成交均有 cohort，未观察到这三笔缺标；
2. 三笔指令当前存储的执行 regime 均为 `risk_off`，不能描述为“三笔均在 NO_TRADE 成交”；
3. cohort 表示此前通过 NO_TRADE 实验 sizing/标记，跨 regime 成交仍属原 RFC-217 口径；
4. 历史 sizing 时点的进一步证明需使用日志/既有 sizing 凭据，不能靠该空 `regime_reads` 探针补齐。

这项限制不推翻“当前未观察到预 sizing 受害成交”，但收窄了 §13 的证据表述。

### 14.4 黑名单与资金分布再次确认

- `risk.global_blacklist` 实际为 5 条：US.QCOM、US.LCID、US.TME、HK.00700、US.HK.00700。动态侧无统一引用、最终合并无复核，且 QCOM 在 dragonhead 池，§13 的确认与五条口径修正成立。
- 11-25 35 份 raw prompt 中，正式 capital_distribution 块 0/35，分时 fallback 34/35，无资金面段 1/35。
- RIVN 中“主力净流入”字符串的上下文确为英集芯 688209、托伦斯 301583、芯原股份 688521 等 A 股研报内容，不是正式资金分布渲染。该事实支持“不能按字符串误判正式块已渲染”；是否构成更广泛的研报相关性污染，仍需另做来源与标的关联审计。

### 14.5 方向与收益结论不变

独立复跑 11-25 funnel 探针，13 条 SELL_SHORT＋4 条 SKIP、两条历史 LONG 拦截、MSFT 确认反转、RIVN 费用地板和三笔 SHORT 成交均与 §13.3 一致。当前 LONG 按轮暂缓与历史终态 SKIP 的 Git 区分不改。

本轮没有新增收益证据：预 sizing 降级是事故优先级修正，资金分布仍是待验证正式输入，黑名单和遥测均属正确性/风险修复。不得据此宣称盈利改善。

### 14.6 二次确认后的建议顺序

1. **优先评审动态黑名单正确性补丁**；
2. 扫描/DA 上游漏斗和实验身份先做 record-only；
3. RFC-217 预 sizing 按 P1 潜伏路径单独修复，补跨轮回读用例；
4. EventBrief freshness 修上游，不放宽日期守卫；
5. 资金分布仅在用户批准唯一正式输入变量后推进；其余输入继续分批，不捆绑。

---

## 15. 实施记录（2026-11-27 · 用户裁「按照建议合理优化落地」· 资金分布唯一正式输入变量 · 默认关闭影子就绪）

> 本节追加，不改 §§0–14 历史。§12.3「未实施任何生产修复」由本节 supersede：资金分布 `code` 错配修复已以「代码＋契约测试＋文档」落地，但置于**默认关闭独立开关**之后，当前生产逐字节不变；**未激活、未提交、未推送、未重启、未跑收益实验**。

### 15.1 授权与范围（一次一变量）

- 用户本轮裁「按照建议合理优化落地」，授权实施。按 §8 推荐顺序与「首选唯一正式输入变量」，本批**只实施 P0 资金分布正确消费**（§13.2-C / §14.4 已确认病灶），不捆绑黑名单／预 sizing／短 K／健康空财报／源状态／挑战者等其他修复（那些仍排队，见 §15.7）。
- 这是正式输入正确性修复、收益假设相邻 → 激活须按 §10.4 单变量 A/B（n≥120 且 ≥20 独立 ET 日，预注册 MDE）。**本批不激活开关、不声称收益增量。**

### 15.2 改动清单与哈希

仓外 DA 三文件（备份后缀 `.bak-20261127-rfc218-capital`）：

| 文件 | 改前 SHA256 | 改后 SHA256 | 改动 |
|---|---|---|---|
| `config.py` | `cfcd3ed189bf6bfad9a1115e2e5019a4c1c79f4fba672868bb8f212e24f7cf09` | `41cf45334304058dd13031adb2194762bbf3e7cd96a874b000550e2d68cf6db6` | 新增 `DA_CAPITAL_CODE_FIX` 独立开关（默认 `0`）＋注释；`DA_INPUT_FULL` 未触 |
| `llm_synth.py` | `705ff7c2f43b2ca34aa77ac5b37df11302375a6dd2103670e1fc91d723609b8c` | `51fac22320827da3534516f98a4f5ee46d8a430cc8b53c66c05e9b6eb8a315d0` | 抽出 `_render_capital_section` 单一真源（开关门），f-string 逐字保留；调用点替换 |
| `deep_analyzer.py` | `9c5407b8323f321e9a5614529f238b239c4fb59491e55a4339f1bce7c49b9ed0` | `083faae73a839cf7df5645d648de4b5dff48f7e7fcfbb1413714b311fa03e233` | `_analyze_capital` 规则降级路径同开关同口径对称修复 |

- `llm_synth.py` 改前 SHA 与 §2.2 一致；`config.py`／`deep_analyzer.py` 不在 §2.2 原四文件表内，改前 SHA 取自本批备份。
- 仓内新增测试：`tests/test_da_capital_code_fix.py`（16 例，skill 不可用即 skip）。
- 备份：`/root/.hermes/skills/deep-stock-analysis/scripts/{config,llm_synth,deep_analyzer}.py.bak-20261127-rfc218-capital`。
- diff 范围核对：三文件改动均限于资金分布判定门／渲染抽取／开关注释，**无其他行为变量混入**。

### 15.3 修复语义（四态验收）

- 病灶：provider `get_capital_distribution.py` 实写 `{"code": <ticker字符串>, 八字段}`，旧消费路径误以成功码 `code==0` 判定 → 35/35 份 11-25 有效 payload 正式「## 资金面」块被丢。
- 开关 `DA_CAPITAL_CODE_FIX`：`=0`（默认）逐字节保留旧 `code==0` 门＋旧分时 fallback（当前生产行为不变）；`=1` 以「八字段存在」判定有效分布 payload → 正式块恢复。
- 四态：八字段有效→正式块；全零（字段在）→正式块净 0（零是真实读数，非缺失）；缺字段／空壳 `{code,data:{}}`→分时 fallback；`_error`→fallback。
- 分时 fallback 不受开关影响；`main_in_flow` 缺省 `'N/A'` 字符串原样保留（不补 0）。`deep_analyzer` 规则降级路径与 `llm_synth` 同开关同口径。
- 刻意**不复用 `DA_INPUT_FULL`**（§8 回滚约束：独立开关，不与其他输入变量捆绑）。

### 15.4 验证（可复跑命令＋日志，不写「已通过」）

全部 `PYTHONDONTWRITEBYTECODE=1`，venv pytest 8.3.5；DB 未触（纯渲染／契约）。

- `py_compile` 三文件 exit0；`config.DA_CAPITAL_CODE_FIX` 默认 `False`；`DA_INPUT_FULL=True` 未触。
- 新契约 **16/16**：`PYTHONPATH=. ./venv/bin/python -m pytest tests/test_da_capital_code_fix.py -v` → `/tmp/rfc218_capital_fix_newtests.log`。
- 相邻契约 **73 passed**（output＋fundamental）：`… -m pytest tests/test_da_output_contract.py tests/test_da_fundamental_contract.py -q` → `/tmp/rfc218_capital_fix_adjacent.log`。
- 逐字节无回归（最强证明）：`./venv/bin/python -I /tmp/rfc218_capital_regression.py` → `/tmp/rfc218_capital_regression.log`：flag-off new vs backup **35/35 逐字节一致**；历史消费复现 **35/35**；flag-off formal/fallback/none=**0/34/1**（与 §13.2-C 审计一致）；flag-on formal **35/35**、changed **35/35**（搬运层效应，非收益）。
- 更广 DA 批 **70 passed / 2 pre-existing failed**：`… -m pytest tests/test_da_prompt_injection.py tests/test_da_prompt_distill_shadow.py tests/test_intraday_reentry_capital_flow.py tests/test_da_chain_contract_audit.py tests/test_da_trace_preservation.py -q` → `/tmp/rfc218_capital_fix_broader.log`。
- 2 失败属既有漂移非本批回归：`./venv/bin/python /tmp/rfc218_prompt_inject_baseline.py`（预载 backup `llm_synth` 跑同 2 例）→ `/tmp/rfc218_prompt_inject_baseline.log`：backup 同样 **2 failed**。失败断言是 system prompt 的 `MAIN_EVENT_CONTRACT` 接线（RFC-207 EventContext），与资金面无关；该 2 例为 tracked repo 测试、本批未改（`git status` 空），last touch `46e8fb9cf` 2026-11-14。

### 15.5 未验证 / BLOCKED（不得以归档冒充当前生产实测）

- 未激活开关、未跑 §10.4 单变量 A/B、未做机制层（判断变化超随机性）／经济层（可成交完整成本净 bps）验证；**收益增量未证，正确性修复≠盈利改善**。
- 无一致 DB 快照、runtime DA manifest、同刻现货 VIX（沿用 §13.7 BLOCKED）。
- flag-on 35/35 正式块恢复只是搬运层 schema／渲染正确，不证模型实际据此改变方向，更不证收益。
- 2 个 pre-existing `prompt_injection` 失败未修（属 RFC-207 系统 prompt 契约漂移，另案，不在本批一变量范围）。
- 资金分布正确进入≠有预测力；大单流入不得直接解释为「机构确定看多」（沿用用户判断）。

### 15.6 实施状态（supersede §12.3）

§12.3 原文「未实施任何生产修复」更新为：资金分布修复已以代码＋测试＋文档落地，**默认关闭独立开关之后，当前生产逐字节不变**（flag-off 35/35 证明）。未提交、未推送、未重启、未激活、未跑收益实验。

- 回滚：`.env.us` 加 `DA_CAPITAL_CODE_FIX=0`（或删除该行）→ 旧渲染；或删除开关属性 → `getattr` 默认 `False`。
- DA 经 `ops/hermes/da_main_carrier.py` 以 cron 子进程逐轮 fresh import，与 systemd 服务分离；flag-off 无行为变更，**本批无需重启**。

### 15.7 排队中的其余输入修复（本批未实施，各自独立开关，不捆绑）

按用户「先核实际消费、避免重复建设」与 §8 分批原则，以下仅记录精确锚点（当前文件行号），留待逐批裁决：

- **健康空财报三态**（`llm_synth.py:1019-1043`）：`next_earnings` 为空时整段静默省略，模型无法区分「窗口内未发现事件／源失败／未提供」；应补三态＋查询窗口＋as-of。
- **短 K UNKNOWN**（`llm_synth.py:639-651`）：`len(closes)`<要求时 `ma5/ma10/ma20/ma60/chg5/chg20` 补 0，应改 UNKNOWN＋有效根数＋末 bar 日期＋是否完成。注：11-25 无 `1≤len<60` 样本（≥60:34、=0:1 UBER），UBER kline=0 走整段省略（action=SKIP），非「短 K 当震荡」样本；零填充路径代码存在但本批无历史受害样本。
- **源状态可见性**（`llm_synth.py:564-581`）：`_source_status` 循环仅 `news/news_v2/research/sentiment` 四源，资金分布渲染层丢弃完全不可见（不在数据源状态、不在质量审计）；应扩可见性。
- **挑战者事实一致性**（`llm_synth.py:2592-2619`，`_challenger_data_view`）：`_FULL` 白名单含「资金面」（修复激活后挑战者可见正式块），但「数据源状态」不在白名单 → 被丢弃，挑战者看不到源失败状态；应补白名单并核两角色同事件版本／同时点价格／弃权是否因缺材料。
- **事件预期差／价格响应／持仓期风险／执行可行性**（用户优先补充二~五）：先核现有载体实际消费，不无差别加新闻／指标／模型；属研究→可执行指令的缺口，另案预注册。

### 15.8 下一步 / 待用户裁决

1. 本批保持默认关闭影子就绪，不激活、不重启、不捆绑。
2. 是否提交／推送（repo 测试＋RFC＋handoff；仓外 DA 三文件不在 git 跟踪）须用户明确授权。
3. 激活正式输入须用户批准 §10.4 单变量 A/B（n≥120／≥20 独立 ET 日／预注册 MDE）后方可推进。
4. 其余输入修复（§15.7）逐批单独裁决，各自独立开关。
