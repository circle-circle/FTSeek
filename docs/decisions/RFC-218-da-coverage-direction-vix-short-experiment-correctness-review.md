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

§12.3 原文「未实施任何生产修复」更新为：资金分布修复已以代码＋测试＋文档落地，**默认关闭独立开关之后，当前生产逐字节不变**（flag-off 35/35 证明）。仓内产物已提交 `1c6d625ce1917a072209d3912e759860bd6382de` 并推送 `origin/main`（`078966c98..1c6d625ce`，用户裁「该重启、推送、提交做的」）；**未重启**（本批无 `app/` 运行时改动＋仓外 DA 默认关 cron fresh-import，无需重启）、未激活、未跑收益实验。

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
2. 仓内产物已提交推送 `origin/main`（`078966c98..1c6d625ce`）；仓外 DA 三文件不在 git 跟踪（已改、默认关）。本批无需重启。
3. 激活正式输入须用户批准 §10.4 单变量 A/B（n≥120／≥20 独立 ET 日／预注册 MDE）后方可推进。
4. 其余输入修复（§15.7）逐批单独裁决，各自独立开关。

## 16. 实施记录 II（2026-11-27 · 用户裁「基于合理性和有效性执行…该重启、推送、提交做的」· 动态黑名单统一守卫 · 已激活＋重启冒烟通过）

> 本节追加，不改 §§0–15 历史。本批唯一变量＝**Playbook 新候选边界全局黑名单守卫**（§8 顺序 #1、§13.2-B／§14.4 确认病灶）。这是**正确性／风控修复，非收益假设相邻的正式 DA 输入变量**——被禁标的本就不该进入候选，恢复配置既有语义无需 §10.4 A/B 即可激活（与 §15 资金修复「默认关等 A/B」区分）。资金修复开关 `DA_CAPITAL_CODE_FIX` 本批**未激活、未捆绑**（重启后进程 env 无该变量，默认 OFF）。

### 16.1 病灶复述与归因修正（先证伪，后动手）

- 配置 `config/unified_config.yaml:2277` `risk.global_blacklist` 共 5 条：`US.QCOM / US.LCID / US.TME / HK.00700 / US.HK.00700`（归一化后 4 个标的）。静态侧 `get_stock_codes` 已滤；但三条侧门无复核：
  1. `scheduler_core._load_watchlist` 经 `daily_active_pool` 合并入 `scheduler.watchlist`（无黑名单复核）；
  2. `runtime_context._get_watchlist` 纯加法合并 `factor_engine._get_stock_pool()` 动态池（dragonhead/异动扫描/daily_active_pool，零黑名单引用）；
  3. `generation_flow.run_playbook_topup_supplement` 的 `roles.topup_eligible` 派生不看 `enabled`/黑名单——**QCOM `enabled: false`（yaml:3282-3286）却仍在 `topup_eligible`**。
- DB 实证（只读 `mode=ro`+`query_only`）：QCOM 11 月 playbook_instructions 5144/5374/5454/5519（11-02/05/06/09）全部 deterministic SKIP「非当前通道候选(bridge未提名)」→ **证明被禁标的确实反复回入 Playbook 候选 watchlist**（合并路径；若未回入不会生成指令行）。
- **归因修正（本轮新证据，不得沿用旧口径）**：QCOM 11 月 4 笔 PLAYBOOK_SIM LONG 成交（trade 1829/1847/1853/1882，11-09→11-12）的 `decision_id` 均为 `EXT-<date>-US.QCOM-afterhours/premarket`，**在 playbook_instructions 中零匹配** → 成交来自 **EXT 延长时段 cron 执行器**（`scripts/extended_hours_entry_executor.py`，crontab 313/314 行在运行），**不是合并路径泄漏下单**。旧表述「QCOM 经黑名单漏洞实际成交」需拆成两件事：合并路径泄漏=候选面污染（已证）；实际成交=EXT 独立泄漏面（见 16.5）。
- 凭据：`/tmp/rfc218_blacklist_evidence.py|.log`、`/tmp/rfc218_blacklist_evidence2.py|.log`。

### 16.2 改动清单（本批提交范围）

| 文件 | 改动 |
|---|---|
| `app/utils/watchlist_manager.py` | 新增 `_blacklist_guard_enabled()`（env `PLAYBOOK_BLACKLIST_GUARD`，默认 `"0"`，truthy=`1/true/yes/on`，未知值 fail-closed 回旧行为）＋ `filter_global_blacklist(symbols)`（复用 `_get_global_blacklist()` 谓词，剥 `US.` 前缀与 `_apply_exclusions` 同口径；OFF 返回**同一对象**；黑名单空/读取失败 fail-open；仅拦截时打 INFO `[BLACKLIST] playbook 候选守卫拦截…`） |
| `app/playbook/pipeline_parts/runtime_context.py` | `_get_watchlist`：`wl` 在 #SIM/双前缀清理后过守卫；`merged` 在既有 try 内再过守卫（合并＋fallback 同口径）；守卫 import 失败 fail-open 直通 |
| `app/playbook/pipeline_parts/generation_flow.py` | topup `_pool`（roles.topup_eligible）在既有 try 内过同一守卫（topup 当前 `enabled: false`，守卫＝将来重开时的闭合保证） |
| `tests/playbook/test_blacklist_merge_guard.py`（新增） | 24 例契约：开关语义/OFF 同一对象/ON 拦截/空黑名单与配置故障 fail-open/合并与 fallback 与 topup 三边界/`#SIM` 与双前缀交互/不动 disabled/self_managed/持仓退出链 |
| `.env.us`（不入库） | 追加注释＋`PLAYBOOK_BLACKLIST_GUARD=1`（激活） |

- **只消费 `risk.global_blacklist`（明确禁止语义）**；不动 `enabled=false` 普通暂停、不动 `self_managed`（仍由静态侧负责）、不触碰持仓/退出链、`scheduler_core._load_watchlist` 未改（监控/持仓面保留）、`intraday_reentry` 已关闭轴未触。

### 16.3 验证（可复跑命令＋日志）

全部 `PYTHONDONTWRITEBYTECODE=1`、`./venv/bin/python`；DB 全程只读。

- `py_compile` 三文件 exit0；`scripts/check_undefined_names.py` exit0。
- 新契约 **24/24**：`PYTHONPATH=. ./venv/bin/python -m pytest tests/playbook/test_blacklist_merge_guard.py -q` → `/tmp/rfc218_blacklist_guard_newtests.log`。
- 相邻批 **1540 passed / 18 failed**：`… -m pytest tests/playbook/ tests/test_symbol_registry_p1p2.py -q` → `/tmp/rfc218_blacklist_guard_adjacent.log`。**18 失败均为既有**：`git stash push` 本批 3 个 app 文件后重跑同失败子集 → **同样 18 failed/86 passed、FAILED 清单逐条一致** → `/tmp/rfc218_blacklist_guard_baseline.log`（随后 `stash pop` 复原）。失败分布 5 文件：falsification_capital_flow_shadow 2 / low_atr_gate_screening_basis 10 / m2_c2_live_quote_gate 4 / offline_full_chain 1 / risk_scale_wiring 1，另案不在本批一变量范围。
- 变异测试（证明测试真的咬合）：把开关解析强制 `return False` → **10 failed** `/tmp/rfc218_blacklist_guard_mutation_red.log`；还原 → **24 passed** `/tmp/rfc218_blacklist_guard_restore_green.log`。
- 影子探针 `/tmp/rfc218_blacklist_shadow.py` → `/tmp/rfc218_blacklist_guard_shadow.log`：真实黑名单归一化 {HK.00700,LCID,QCOM,TME}；今日重建池 55 标的 **off→on 差分 ∅**（今日池本就干净，无自然拦截样本）；合成注入全部 5 条黑名单：flag-off 泄漏 5、flag-on 拦截 5、**误伤 ∅**。**边界**：探针以 `daily_active_pool` 存档替身 `_get_stock_pool`（离线、不触发 Finnhub 扫描），非当日实时动态池全量。

### 16.4 激活／重启／冒烟（实际发生）

- 重启前快照 `/tmp/rfc218_blacklist_pre_restart.json`：SIM SHORT BA 23 / BABA 47 / GS 5，LIVE 0，`entry_booking_outbox` 22 全 BOOKED，`git status app/ scripts/` 仅本批 3 文件（无他人在途 WIP 混入）。
- `systemctl restart ftseek-us` → `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` → **7/7 全过 exit0** `/tmp/rfc218_blacklist_startup_smoke.log`。新 MainPID **161734**（2026-11-27 14:28:30 CST）。
- `/proc/161734/environ` 含 `PLAYBOOK_BLACKLIST_GUARD=1`；**无 `DA_CAPITAL_CODE_FIX`**（资金修复保持默认 OFF，未捆绑）。
- 重启后快照 `/tmp/rfc218_blacklist_post_restart.json`：BA/BABA/GS 23/47/5 逐一不变、LIVE 0、outbox 22 全 BOOKED → 持仓/退出链零扰动。
- 回滚：`.env.us` 改 `PLAYBOOK_BLACKLIST_GUARD=0`（或删行）＋ `systemctl restart ftseek-us` → 逐字节旧口径（OFF 同一对象已契约证明）；或 revert 本批 commit。

### 16.5 新确认的独立泄漏面：EXT 执行器零黑名单（本批不修，排队）

- `scripts/extended_hours_entry_executor.py` 全文 **0 处 blacklist 引用**；候选宇宙＝Futu 排行榜＋用户 Futu App 自选分组＋财报日历（`_scan_candidates:2353`、`_watchlist_symbols:2196`），完全绕过 `risk.global_blacklist`。QCOM 4 笔 EXT SIM LONG（16.1）即此面实证。
- 研究臂已按预注册到期（`.env.us:179` `EXT_RESEARCH_SOURCE_UNTIL=2026-11-13`，11-13 后 EXT 零成交）；**财报门控严格臂仍在运行**（crontab 313/314）。
- **本批不改 EXT**：它是独立预注册实验臂，改其供给面＝改实验变量，须单独裁决/预注册修订，不得混入本批一变量。列为下一批候选：EXT 候选边界接入同一 `filter_global_blacklist` 谓词（或 EXT 侧独立开关），先影子差分再激活。

### 16.6 未验证 / BLOCKED（不得以归档冒充当前生产实测）

- **收益增量未证，也非本批目标**：本批收益边界＝正确性/风控（被禁标的不再进入新候选），不声称任何盈利改善；守卫不改变任何未被禁标的的判断/执行。
- 生产自然拦截**未观测**：今日真实池 off→on 差分 ∅（池干净），拦截效力目前由合成注入＋契约测试证明；待未来被禁标的自然进入 `daily_active_pool`/topup 时看 `[BLACKLIST]` INFO 日志实证。
- EXT 面仍未守卫（16.5，已排队）；`scheduler_core._load_watchlist` 监控面仍可见被禁标的（刻意保留，非新候选路径）。
- 全量 pytest/CI 未跑（只跑相邻批）；无一致 DB 快照／runtime DA manifest／同刻现货 VIX（沿用 §13.7 BLOCKED）。
- 18 个既有相邻测试失败未修（基线证明在案，另案）。
- 资金分布修复激活仍 BLOCKED 于 §10.4 A/B（§15.8 不变）。

### 16.7 下一步

1. EXT 黑名单守卫批（16.5）：独立裁决＋影子差分先行。
2. §15.7 输入修复队列不变（健康空财报三态／短 K UNKNOWN／源状态可见性／挑战者事实一致性），逐批单变量。
3. 观察 `[BLACKLIST]` 生产拦截日志，积累自然样本后在 handoff 补记。

## 17. 实施记录 III（2026-11-27 · 用户裁「继续」· EXT 黑名单守卫影子先行 · 默认 OFF 未激活 · 激活须独立裁决）

> 本节追加，不改 §§0–16 历史。本批唯一变量＝**EXT 候选边界全局黑名单守卫（默认关）＋record-only 影子**。§16.5「本批不改 EXT」由本节 supersede 为：守卫代码＋影子已落地，但 **EXT 当前供给面逐字节不变**（`EXT_BLACKLIST_GUARD=0`）。EXT 是独立预注册实验臂，改供给面＝改实验变量 ⇒ **激活须用户单独裁决（预注册修订），本批不激活**。与 §16 Playbook 守卫（正确性恢复配置既有语义、已激活）刻意区分：EXT 的黑名单缺失是否属「正确性」还是「臂设计」存在裁决空间，且历史差分显示被拦成交全部盈利（17.4），不能以「正确性」名义绕过裁决直接改变实验臂成交集合。

### 17.1 授权与范围

- 用户「继续」＝按 §16.7 队列推进 #1「EXT 黑名单守卫批：独立裁决＋影子差分先行」。本批交付＝守卫（默认关）＋影子（record-only）＋历史差分证据＋注册表登记，**不含激活**。
- 不捆绑：§15.7 输入修复、预 sizing P1、漏斗 record-only、资金修复激活（`DA_CAPITAL_CODE_FIX` 仍 OFF，重启后进程 env 无该变量）均未触。

### 17.2 改动清单（当前文件锚点）

| 文件 | 改动 |
|---|---|
| `scripts/extended_hours_entry_executor.py` | 新增 `EXT_BLACKLIST_DIR`(:216)＋`_ext_blacklist_guard_enabled`(:219, env `EXT_BLACKLIST_GUARD` 默认 `"0"`, truthy=`1/true/yes/on`, 未知值 fail-closed)＋`_ext_blacklist_shadow_enabled`(:228, env `EXT_BLACKLIST_SHADOW` 默认 `"1"`, 显式 falsy 关)＋`_ext_blacklist_set`(:234, 谓词真源=`watchlist_manager._get_global_blacklist`, 失败 fail-open 空集)＋`_record_ext_blacklist`(:243, 按日 JSON 去重 `session:symbol`, ticks 累加, 写盘失败绝不影响下单路径)。严格臂汇合口 `_scan_candidates._add`(:2445 起, 与 test/ETF 同级)＋研究臂边界 `_research_source_candidates`(:657 起) 同谓词同开关复核 |
| `app/playbook/shadow_registry.py` | 注册 `ext_blacklist_guard_shadow`(:5126, OBSERVING, `dormant_reason`＝命中才写盘/无命中=无目录=通过态, `stale_expected=True`, `no_adjudication_reason=USER_ADJUDICATION`, `config_key=None`——影子开关不门控守卫 ON 时的 blocked 审计, 勿照 config_key 判停) |
| `tests/test_ext_blacklist_guard.py`（新增） | 36 例契约：开关/影子语义、OFF 供给面不变＋shadow 台账、ON 拦截＋blocked 审计零误伤、空黑名单/读取失败/import 失败 fail-open、test/ETF 优先级、跨 tick 去重、写盘失败不炸候选、研究臂 OFF/ON/到期零流量、**与 Playbook §16 谓词同源同口径 parity** |
| `.env.us`（不入库） | `EXT_BLACKLIST_GUARD=0`＋`EXT_BLACKLIST_SHADOW=1`＋注释（激活/回滚锚点） |

- 语义：OFF＝候选原样通过（供给面逐字节旧行为），命中只落 `mode=shadow` 台账；ON＝汇合口拦截＋`mode=blocked` 审计（不受影子开关影响）。研究臂当前已按 `EXT_RESEARCH_SOURCE_UNTIL=2026-11-13` 到期＝零流量，守卫＝将来重注册时的闭合保证。
- 谓词单一真源＝`_get_global_blacklist()`（config 读取＋剥 `US.`），与 §16 Playbook 守卫同源；EXT 侧候选剥 `US.` 同一行谓词，parity 契约钉死。**未复用 `filter_global_blacklist` 包装**（其门是 `PLAYBOOK_BLACKLIST_GUARD`，复用会把两条臂的开关耦合）。
- 消费面核对：`cancel_orphan_ext_order.py` 只 import `_save_state/_state_lock`（未触）；其余为文本引用；cron 313/314 每 tick fresh-import，无签名级破坏。

### 17.3 验证（可复跑命令＋日志）

全部 `PYTHONDONTWRITEBYTECODE=1`；DB 只读 `mode=ro`+`query_only`。

- `py_compile`（`./venv/bin/python` 与 cron 实际解释器 `venv/bin/python3.11` 双核）exit0；`scripts/check_undefined_names.py`（executor＋registry）exit0。
- 新契约 **36/36**：`PYTHONPATH=. ./venv/bin/python -m pytest tests/test_ext_blacklist_guard.py -q` → `/tmp/rfc218_ext_blacklist_newtests.log`。
- 相邻批 `tests/test_ext_*.py tests/playbook/test_blacklist_merge_guard.py` **276 passed / 7 failed** → `/tmp/rfc218_ext_blacklist_adjacent.log`。**7 失败均既有**：`git stash push -- scripts/extended_hours_entry_executor.py` 后复跑同 7 例 → **同样 7 failed、清单逐条一致** → `/tmp/rfc218_ext_blacklist_baseline.log`（stash pop 复原）。分布：ext_admission_attribution 1 / ext_entry_research_arm 2 / ext_entry_short_arm 3 / ext_entry_test_symbol_guard 1，另案不在本批范围。
- 变异：守卫解析强制 `return False` → **12 failed** `/tmp/rfc218_ext_blacklist_mutation_red.log`；还原（sha256 核对一致）→ **36 passed** `/tmp/rfc218_ext_blacklist_restore_green.log`。
- 注册表相邻 **41 passed**（intent 唯一性/看板守卫/RFC-207/判据可见性/风险影子注册）→ `/tmp/rfc218_ext_blacklist_registry_tests.log`；`check_mounting()` 对本条目 **0 告警**（dormant 生效），`_count_samples` 目录不存在返回 None（不伪装 0）。
- 真实 env 加载冒烟：`PYTHONPATH=. ./venv/bin/python -c "import scripts.extended_hours_entry_executor as m; …"` → blacklist_set={HK.00700,LCID,QCOM,TME}、guard=False、shadow=True（IMPORT_SMOKE_OK）。
- 重启（因 registry 属 `app/`，保持服务＝仓代码一致）：前快照 `/tmp/rfc218_ext_blacklist_pre_restart.json` → `systemctl restart ftseek-us` → 冒烟 **7/7 exit0** `/tmp/rfc218_ext_blacklist_startup_smoke.log` → 新 MainPID **163251**（14:44:58 CST）→ 后快照 `/tmp/rfc218_ext_blacklist_post_restart.json`：BA/BABA/GS 23/47/5 逐一不变、LIVE 0、outbox 22 全 BOOKED。进程 env：`EXT_BLACKLIST_GUARD=0`、`EXT_BLACKLIST_SHADOW=1`、`PLAYBOOK_BLACKLIST_GUARD=1`（§16 保持）、无 `DA_CAPITAL_CODE_FIX`。

### 17.4 历史影子差分（只读，激活裁决的证据底座）

`/tmp/rfc218_ext_blacklist_history.py|.log`（DB 只读）：全历史 `decision_id LIKE 'EXT-%'` 成交 **76 笔**，黑名单命中 **4 笔＝全部 QCOM**（trade 1829/1847/1853/1882，11-09→11-12，与 §16.1 归因一致）；LCID/TME/HK.00700 零 EXT 成交。

**必须如实呈现的反事实**：这 4 笔**全部盈利**（realized_pnl 合计 **+$92.57**，pnl_net 合计 +$75.66）。即守卫 ON 的历史反事实＝移除 4 笔盈利 SIM 成交。结论：
- 激活依据只能是**禁止语义/风控一致性**（用户/配置明令禁止的标的不该被任何臂交易），**不是收益**；
- n=4 的反事实既不能证明守卫「损失收益」，也不能证明「改善收益」——描述性读数，非收益检验；
- 这正是「正确性≠盈利」的实例：若以收益论，黑名单本身可能需要复核（QCOM 为何被禁、禁令是否仍成立），那是另一个裁决，不在本批。

### 17.5 未验证 / BLOCKED

- **守卫未激活**（`EXT_BLACKLIST_GUARD=0`），EXT 供给面/成交集合当前逐字节不变；激活须用户单独裁决（预注册臂供给面变量）。
- **自然影子命中未观测**：影子自下一个 cron tick（16:02 CST 盘前族）起在采；今日池是否含被禁标的未知，无命中=无目录=通过态（dormant 已登记）。
- 历史差分为描述性反事实（n=4），非收益证明；全量 pytest/CI 未跑；7 个既有相邻失败未修（基线在案）。
- 研究臂守卫为「重注册闭合保证」，当前零流量，无自然样本。
- 无一致 DB 快照/runtime DA manifest/同刻现货 VIX（沿用 §13.7）。

### 17.6 激活与回滚路径（供裁决）

- 激活：用户裁决后 `.env.us` 改 `EXT_BLACKLIST_GUARD=1` → cron 每 tick fresh-import 即生效（**无需重启服务**；服务进程不运行 EXT 执行器）。建议裁决输入：本节日志＋影子自然命中台账 `data/shadow/ext_blacklist/`＋「QCOM 禁令是否仍成立」复核。
- 回滚：`EXT_BLACKLIST_GUARD=0`（或删行）→ 旧供给面；`EXT_BLACKLIST_SHADOW=0` 且守卫 OFF → 影子停写；或 revert 本批 commit。产物留档不删。

### 17.7 下一步

1. 观察 `data/shadow/ext_blacklist/` 自然命中（周检哨兵已登记，dormant 不误报）；有命中即提交用户裁决激活。
2. §15.7 输入修复队列不变，逐批单变量；资金修复激活仍 BLOCKED 于 §10.4 A/B。
3. Playbook §16 守卫生产自然拦截样本继续观察（`[BLACKLIST]` INFO）。

---

## 18. 最新实施项独立复核（2026-11-27 · §§15–17）

> 本节是对资金分布修复、Playbook 黑名单守卫、EXT 黑名单影子的再次只读复核。结论：**默认开关隔离总体有效，当前无需因本节发现而盲目回滚；但资金开启态、Playbook 全链黑名单闭环、EXT 自然命中待裁链均有未闭合项，不能按原记录宣称全部完成。**

### 18.1 基线与总体裁定

- 复核 HEAD=`18724eda5071e422be7843d400ebeda97adcf411`，与 `origin/main` 一致；生产代码/配置/测试工作区为空，既有 DEB-20261125 WIP 未触碰。
- 三批核心提交：资金=`1c6d625ce`；Playbook=`2732a0927`；EXT=`29609e456`。
- 服务 MainPID=163251，2026-11-27 14:44:58 CST 启动，active/running，NRestarts=0；既有启动冒烟日志为 7/7。
- 截至 2026-11-27 15:59 CST，当前日志无 Playbook 自然 `[BLACKLIST]` 拦截，`data/shadow/ext_blacklist/` 尚不存在。无自然命中不等于证明无需守卫。

| 批次 | 可保留部分 | 尚需修正 | 当前建议 |
|---|---|---|---|
| §15 资金分布 | 默认 OFF 旧行为保持；正常 ticker payload 在两个消费者均可恢复 | 部分/非法输入和上游零填充合同未闭合；CI 可全 skip | **保持 OFF，修正后再谈 A/B** |
| §16 Playbook 黑名单 | 三处候选边界、五配置项、开关和持仓边界基本正确 | DA 直写/执行层未闭环；新增 W4 棘轮；topup 异常语义及若干低风险口径 | **已激活三边界可保留，但不得称全链完成** |
| §17 EXT 黑名单影子 | 默认 OFF 供给不变；命中才写；研究/严格臂边界覆盖 | 目录不计数、不形成待裁提示；mode 覆盖历史；只读文案不准确 | **保持 GUARD OFF；先修观测链再依赖自然命中裁决** |

### 18.2 资金分布：两个激活阻塞项

**A. 开启态把部分字段当成完整有效数据。**

- `llm_synth.py:443-467` 与 `deep_analyzer.py:306-328` 实际使用四个 inflow 键的 `any(...)`，不是注释/RFC 所称“八字段存在”。
- 任一 inflow 键存在后，其余字段经 `.get(...,0) or 0` 补零；单个 `capital_in_big=100` 即可形成正式块及 100% 主力占比，而不是回退分时。
- 字符串可触发 TypeError；`True` 会被当数值 1；NaN/inf 可进入 prompt 或 analyzer 结果。
- 新测试 `tests/test_da_capital_code_fix.py:105-141` 覆盖完整零、空壳和 `_error`，但没有部分字段、字符串、布尔、非有限值及真实 provider `{"error":...}` 形态。

**B. Provider 已提前抹掉零与缺失的区别。**

- `get_capital_distribution.py:44-54` 对非空上游行总构造八键。
- `common.py:588-604` 的 `safe_get(...,default=0)`/`safe_float(...,default=0)` 会把缺键、None、非数值提前转 0。
- 因此只把消费者从 `any` 改为 `all`，仍不能区分真实全零与上游缺失/非法。

**激活前最小合同：**

1. Provider 保留字段级有效性/来源状态，不能提前把缺失或非法静默写成真实零；
2. 两个消费者使用同一完整 schema：八键齐全、数值、有限、非 bool；不完整/非法按明确合同回退；
3. 增加部分 inflow/outflow、字符串、bool、NaN/inf、空行、真实 error shape、上游缺值与真实零的成对测试；
4. 保持 OFF 的历史 35/35 逐字节回归；
5. 仓外测试在通用 CI 缺 skill 时目前会整体 skip（`tests/test_da_capital_code_fix.py:28-39`），至少应有 CI 可见性或不依赖仓外 import 的合同夹具，避免“绿色但零覆盖”。

现有 35/35 证明的是正常归档输入的搬运层恢复，不证明异常输入正确、模型判断改善或收益增加。

### 18.3 Playbook：三边界有效，但主 DA 直写仍绕过

**高优先级缺口：**

- 仓外 DA 正式管线直接 `_pb_db.save_instruction(_inst)`：`pipeline.py:2416-2529`。
- 新增守卫只在 `runtime_context._get_watchlist` 和 topup；DA 自有 scanner 提名不依赖这两个边界。
- `PlaybookDB.save_instruction` 本身不做全局黑名单拦截；当前 `auto_execute_flow`/`playbook_service` 也未发现 `global_blacklist` 复核。
- 因而 DA 若从动态 scanner 提名 QCOM/LCID/TME，仍可形成可执行指令。当前未证明新的受害成交，但“被禁标的已不能进入 Playbook 执行”不成立。

**建议：**黑名单闭包至少覆盖最终执行入口；写入侧可在 `save_instruction` 对明确禁止的新指令转 SKIP＋保留 original_direction/原因，执行侧再拦截历史 PENDING 和其他直写方。只滤新指令，不影响已有持仓和退出链。执行时配置读取失败的政策需明确且可告警，不能只在 DEBUG 静默放行。

**W4 棘轮回归确认：**

- 新 `runtime_context.py:104` 在 except 内使用 `logger.debug`。
- AST 只读复算当前 debug-swallow 为 4，基线 `data/debug_swallow_baseline.json` 为 3，`grew=True`。
- 夜检会报 `W4_debug_swallow_grew`。修复用 `logger.warning(..., exc_info=True)`，不得提高基线；若顺带修改其他 except 日志，须同步满足 W6 栈要求并跑完整棘轮家族。

**低优先级但应排队：**

- topup 把原池读取和 guard import/call 放在同一 try，guard 异常会令 `_pool=[]`：`generation_flow.py:366-376`。当前 topup disabled，不是现役 outage；未来重开前应将 guard 异常单独回退原池并给正确告警。
- 当前守卫只消费 global_blacklist，self-managed 仍只在静态 `_apply_exclusions` 使用：`watchlist_manager.py:89-121`。若 daily_active_pool/topup 可携带自管标的，需另核是否违背自管语义；当前自管两项与 HK 黑名单归一重叠，不据此虚构新增 US 事故。
- 符号比较用简单 `.replace("US.","")`，与写侧 `normalize_symbol` 的大小写/空白/GOOGL→GOOG 别名不完全一致；属于将来扩展黑名单时的潜伏边界。
- 合并日志在静态列表已过滤后计算 static/overlap，命中时算术语义会失真；守卫应另报 blocked count。
- 24 项测试中的 positions 用例只调用从不读 watchlist 的函数，不能单独证明退出链不回归；应补“guard ON 不改 scheduler.watchlist/持仓与退出输入”的真实缝测试。

### 18.4 EXT：交易路径无误，观测与待裁链未闭合

**P2 观测缺口：**

- 注册项 `data_path=data/shadow/ext_blacklist` 是目录：`shadow_registry.py:5136`。
- `_count_samples` 对不存在路径、`.jsonl`、`.json` 有分支，目录最终 `return None`：`:5261-5269,5336-5369`。即使自然命中文件产生，`current_n` 仍不会反映命中数。
- `no_adjudication_reason=USER_ADJUDICATION` 阻止自动 READY，待裁队列也要求该字段为空：`app/review/criteria_status.py:109-118`。因此“首个自然命中提交用户裁决”目前只能靠人工看目录，没有注册表/队列提示。
- `check_mounting()` 干净不等于此命中→待裁链已验证；本项无 eval_script，dormant/stale_expected 又抑制缺产物和 stale 告警。

**建议：**增加目录感知计数或明确的 hit evaluator，并在首次命中/状态变化时通过既有待裁载体提醒用户；只提示裁决，不自动激活。补临时目录计数、首次命中、重复命中、坏 JSON、无命中 dormant 和“提醒≠启用”的测试。

**P3 历史 provenance：**

- `_record_ext_blacklist` 对同一 `(session,symbol)` 的后续命中直接覆盖 `mode`，保留原 first_ts 并累计 ticks：`extended_hours_entry_executor.py:263-278`。
- shadow→blocked→rollback shadow 会抹掉各阶段真实次数。应分别保存 shadow/blocked 次数与首次/末次时间或追加 mode transition，不能把历史影子行整体改成 blocked。

**文案勘误：**

- §17.3“DB 只读”不能覆盖整个相邻 pytest：保留日志 `/tmp/rfc218_ext_blacklist_adjacent.log:10-13` 明确记录既有测试对生产主库写入 5 次。
- 准确说法应为：“历史差分查询使用 mode=ro/query_only；相邻测试触发了已登记的存量主库写入 5 次，非本 patch 新增。”该污染应另案治理，不能从文字中抹掉。

### 18.5 测试与经济证据边界

- 资金 16 项、Playbook 24 项、EXT 36 项契约均有当前主机通过证据；这些通过不覆盖本节新增的异常/闭环场景。
- Playbook 相邻 18 失败与基线失败清单哈希一致；EXT 相邻 7 失败与基线一致；资金更广批 70 passed/2 failed 与 backup 的两项 RFC-207 失败一致。不得写成全量测试全绿。
- 资金保持 OFF，当前生产收益影响为零；开启后 35/35 prompt 改变仍只是搬运层。
- Playbook 历史 QCOM 候选指令是 deterministic SKIP，实际四笔盈利成交属 EXT；不能把 EXT 的盈亏当 Playbook 守卫收益/损失。
- EXT 四笔 QCOM 净额合计 +$75.66 是单标的、四日、小样本历史差分，且未建模替换候选/组合互动。激活只能依据禁止语义是否仍有效，不依据该小样本追收益。
- 本节没有任何修复后收益改善证据，不触发加仓、扩权或新方向政策。

### 18.6 建议执行顺序

1. **先补 Playbook 黑名单最终执行/写入闭环和 W4 修复**，保持既有三边界开启；自然命中继续观察。
2. **资金保持 OFF**，先完成 provider→消费者的完整有效性合同和异常矩阵，再评审是否进入单变量 A/B。
3. **EXT GUARD 保持 OFF**，先修目录计数/首命中待裁提示和 mode provenance；再由用户裁 QCOM 禁令是否仍适用于 EXT。
4. topup、self-managed、符号归一和测试缝作为低优先级同域收口，不与资金或 EXT 经济实验捆绑。
5. 在上述正确性缺口闭合前，不继续扩张 §15.7 的其他正式输入调整。

## 19. 实施记录 IV（2026-11-27 · 用户裁「请检查，有把握收益的请执行优化落地…该重启、推送、提交做的请你基于合理性和有效性执行」· §18 缺口闭合 · 正确性批次非收益补丁）

> 本节按 §18.6 顺序落地：**① Playbook 黑名单写入/执行闭环＋W4 修复（激活）；② 资金 provider→消费者完整有效性合同（保持 OFF）；③ EXT 目录计数/首命中待裁提醒/mode provenance（GUARD 保持 OFF）**。全部为正确性/风控/观测修复，**没有任何新的收益改善证据，也不声称收益**（§9/§18.5 边界不变）。正式输入变量零新增激活：`DA_CAPITAL_CODE_FIX` 与 `EXT_BLACKLIST_GUARD` 均保持 OFF，激活仍分别 BLOCKED 于 §10.4 单变量 A/B 与用户独立裁决。

### 19.1 P0 · Playbook 黑名单写入/执行闭环（§18.3 高优先级缺口）＋W4 棘轮修复

**病灶回顾（§18.3 确认）**：§16 守卫只在 `runtime_context._get_watchlist` 与 topup 候选边界；仓外 DA 正式管线 `pipeline.py:2529` 直写 `PlaybookDB.save_instruction` 绕过全部候选边界，执行层亦无 `global_blacklist` 复核 ⇒ 被禁标的（QCOM/LCID/TME）仍可形成并执行指令。

**本批修复（独立开关 `PLAYBOOK_BLACKLIST_FINAL_GUARD`，默认 OFF，与 §16 `PLAYBOOK_BLACKLIST_GUARD` 刻意分离、独立回滚）**：

1. **单标的谓词**：`app/utils/watchlist_manager.py:365` `is_global_blacklisted(symbol)` — 与 §16 `filter_global_blacklist` 同源同口径（只消费 `risk.global_blacklist`，剥 `US.` 前缀同一行谓词）；读取失败/空黑名单 → False（fail-open，守卫故障不瘫痪写入/执行），`warning+exc_info` 保证可见。开关读取 `:352` 每次实读不缓存，仅显式 truthy（1/true/yes/on）开启。
2. **写入侧收口**：`app/playbook/db.py:589-616` `save_instruction` 在 `normalize_symbol` 之后、准入门之前统一复核 —— 开关 ON ∧ 被禁 ∧ `direction!=SKIP` → 转 `direction="SKIP"`＋`skip_reason="GLOBAL_BLACKLIST_FINAL"`，`raw_instruction.original_direction` 与 `blacklist_final_guard` 标记保留原方向（审计不丢事实，与静态侧 SKIP 同形态；准入门对 SKIP 返回 PASS 使 skip_reason 存活）。覆盖 DA 直写/UE/topup/generate 全部写入方。异常 fail-open 照常入库＋`warning+exc_info`（审计降级优于丢指令，不添 W4）。
3. **执行侧兜底**：`app/playbook/pipeline_parts/auto_execute_flow.py:591-606` 轮级读一次黑名单集合（避免逐条 N 次配置读；OFF=空集=逐字节旧行为；读取失败 fail-open 空集＋warning）；`:618-627` 逐条在 SKIP/已执行检查之后拦截被禁标的 → `db.mark_instruction_guardrail_skip(id,"GLOBAL_BLACKLIST_FINAL")`＋`skipped_count++`。该 DB 方法自带 `executed=0 ∧ execution_state='PENDING'` 闩（`db.py:1078`），**绝不触 booked/in-flight/持仓/退出链**；覆盖历史 PENDING 与任何绕过写入侧的直写方。
4. **W4 修复**：`runtime_context.py:104` except 内 `logger.debug` → `logger.warning(..., exc_info=True)`。AST 实测单文件 debug-swallow **4→3=基线**（`data/debug_swallow_baseline.json` 该文件=3），全仓夜检棘轮 `grew=[]`，`W4_debug_swallow_grew` 不再触发；W5/W6 对全部本批编辑文件相对 HEAD 中性。fail-open 语义不变（守卫不可用保持旧口径，但不再静默）。

**激活裁定**：`.env.us` 置 `PLAYBOOK_BLACKLIST_FINAL_GUARD=1`（与 §16 同纪律：依据=**禁止语义/风控一致性**，非收益）。激活前影子探针（`/tmp/rfc218_s19_final_guard_shadow.log`）：今日真实指令集差分 ∅（今日 0 条指令，半日市）；合成注入 OFF 泄漏被禁 3/3、ON 拦截 3/3、ON 误伤非禁 0/2 ⇒ 激活对当前生产候选零误伤。历史 2 条 QCOM PENDING（id 264/457，6/7 月）被日期过滤今日不可执行，执行侧兜底对其仍有效。

**部署凭据**：提交 `84a66bc1d457836529aa72b0e30b3a5622b6cdbe` 已推送 origin/main（`18724eda5..84a66bc1d`）；`systemctl restart ftseek-us` → 新 PID **171822**（16:42:50 CST），`/proc/171822/environ` 实含 `PLAYBOOK_BLACKLIST_FINAL_GUARD=1`（且 `DA_CAPITAL_CODE_FIX` 不存在、`EXT_BLACKLIST_GUARD=0`）；启动冒烟 **7/7 exit0**（`/tmp/rfc218_s19_startup_smoke.log`）。重启前/后快照（`/tmp/rfc218_s19_pre_restart.json`/`_post_restart.json`）：SIM SHORT BA 23/BABA 47/GS 5、LIVE 0、outbox 22 全 BOOKED 逐字不变；重启后日志 0 Traceback/CRITICAL。

**刻意不做（§18.3 低优先级排队不变）**：topup 异常清空整池（当前 disabled）、self-managed 边界、符号别名/大小写归一、合并日志 blocked count、positions 真实缝测试 —— 同域低优先级另批收口，不与本批捆绑。

### 19.2 P1 · 资金 provider→消费者完整有效性合同（§18.2 两个激活阻塞项闭合 · 保持 OFF）

1. **Provider 保留字段级有效性**：仓外 `futuapi/scripts/quote/get_capital_distribution.py:40` 新增模块级 `_capital_fields_missing(row)` —— 在 `safe_get/safe_float` 强制转换**之前**按原始值判定（None/bool/非数值/非有限 → 记名；numpy 标量经 `.item()` 归一），结果新增 `capital_fields_missing` 列表；八个数值键形态不变（向后兼容）。缺失/非法不再被静默写成「真实零」而无痕。
2. **消费者单一真源**：仓外 `deep-stock-analysis/scripts/llm_synth.py:436-463` `_CAPITAL_FIELDS`（八字段）＋`_capital_dist_valid`：**八键齐全 ∧ int/float 非 bool ∧ math.isfinite ∧ `capital_fields_missing` 存在则必须为空**。flag-ON `_render_capital_section` 改用该判定；不完整/非法 → 明确回退分时 fallback（不再 `any` 四 inflow 键＋其余补 0 伪装完整）。
3. **对称消费**：`deep_analyzer._analyze_capital:305-320` flag-ON 惰性 `from llm_synth import _capital_dist_valid`（循环导入安全），两路径同一合同；**flag-OFF 逐字节旧 `code==0` 路径不变**。
4. **测试矩阵＋32=48**（`tests/test_da_capital_code_fix.py`）：部分 inflow/outflow、字符串、bool、NaN/inf、空行、真实 provider `{"error":...}` 形态、上游缺值 vs 真实零成对、`capital_fields_missing` 传递；本机 provider 测试全部 RAN 零 skip（通用 CI 缺 skill 整体 skip 的可见性问题仍排队，见 §19.6）。
5. **变异测试**：把 `_capital_dist_valid` 变异回旧 `any()` 四键 → **18 红**（`/tmp/rfc218_s19_cap_mutation_red.log`）；按 SHA 恢复 → 48 绿（`_restore_green.log`）。证明新合同真实钉死行为而非恒真。
6. **OFF 零漂移复验**：§19 改动后重跑 `/tmp/rfc218_capital_regression.py` → **flag-off 35/35 逐字节一致**、历史消费 35/35、flag-off formal/fallback/none=0/34/1、flag-on formal 35/35（`/tmp/rfc218_s19_cap_off_regression_rerun.log`）。

**保持 OFF**：`DA_CAPITAL_CODE_FIX` 未置位（进程 env 无此键）。合同闭合只是解除 §18.2 的**代码层**激活阻塞；激活仍须用户批准 §10.4 单变量 A/B（n≥120/≥20 独立 ET 日/预注册 MDE）。35/35 恢复仍只是搬运层，不证预测力/收益；大单流入不得直接解释为「机构确定看多」。

### 19.3 P2/P3 · EXT 观测链闭合（§18.4 · GUARD 保持 OFF · 交易路径逐字不变）

1. **目录计数**：`app/playbook/shadow_registry.py:5264-5283` `_count_samples` 新增目录分支 —— 按日聚合 `*.json` 中含 `rows` dict 的命中行数；坏 JSON 跳过；空目录/无 rows 文件 → None（不伪装 0，与「无命中=无目录=通过态」一致）。只影响目录型条目（当前仅 `ext_blacklist_guard_shadow`）；`.json/.jsonl` 文件分支逐字不变。自然命中自此可驱动 `current_n`。
2. **首命中待裁提醒（提醒≠启用）**：`scripts/extended_hours_entry_executor.py:328` `_notify_ext_blacklist_first_hit` —— 仅真实生产规范路径（`_EXT_BLACKLIST_CANON`，hermetic 测试 monkeypatch 到 tmp 即跳过，防误发）＋新建行触发；经 `app/push` hub `push_text_safe(sync=True)`（hub 纪律，禁直连 notifier）；按标的去重（`data/shadow/ext_blacklist_first_hit_notified.json`）；异常永不上抛交易路径。文案明示「激活须用户单独裁决，本提醒≠启用」。`USER_ADJUDICATION` 不进普通待裁队列的语义不变 —— 提醒是补充载体，不改注册表裁决政策。
3. **mode provenance**：`_record_ext_blacklist:265-310` 顶层 `mode/ticks` 保留（最新态/总次数，既有 36 契约向后兼容），新增 `modes` 分模式 `{ticks, first_ts, last_ts}`；shadow→blocked→rollback 不再抹掉各阶段次数。升级旧行（无 modes）保守把既有 ticks 归「上一已知 mode」并标 `migrated=True`（重建非实测）。**顺序 bug 实测修复**：`_prev_mode` 必须在 `prev["mode"]=_mode` 覆写前捕获（初版读新 mode → 迁移误归，测试红→修复绿）；本轮再补 `_prev_last_ts` 覆写前捕获（`:276`），迁移记录的旧 mode 末次时点=真实旧观测而非升级命中新时点，测试钉死。
4. **测试**：EXT 契约＋8=44（`tests/test_ext_blacklist_guard.py`：分模式新行/升级/回滚/迁移时点、首命中去重/非规范路径跳过/push 异常不上抛）；新增 `tests/test_shadow_registry_dir_count.py` 8 例（目录计数/坏 JSON/空目录/文件分支不变）。
5. **生效方式**：EXT 本体 cron 313/314 fresh-import 即生效（无需重启）；`shadow_registry.py` 属 `app/` 随本批服务重启一致性生效。**`EXT_BLACKLIST_GUARD` 保持 0、`EXT_BLACKLIST_SHADOW` 保持 1**。激活仍须用户裁「QCOM 禁令是否仍适用于 EXT」；§17.4 历史差分 4 笔 QCOM 全部盈利（净 +$75.66，单标的四日小样本）⇒ 激活依据只能是禁止语义，不是该小样本追收益。

### 19.4 验证矩阵（全部可复跑）

| 项 | 结果 | 凭据 |
|---|---|---|
| 新契约合计 | **129 passed**（黑名单 29＋资金 48＋EXT 44＋目录计数 8） | `PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python -m pytest tests/playbook/test_blacklist_final_guard.py tests/test_da_capital_code_fix.py tests/test_ext_blacklist_guard.py tests/test_shadow_registry_dir_count.py -q` |
| Playbook 相邻 | 18 failed/1569 passed，FAILED 清单与基线 **IDENTICAL** | `/tmp/rfc218_s19_pb_adjacent.log`＋`_pb_base_failed.txt` diff ∅ |
| EXT 相邻（含本轮 last_ts 精化后复跑） | 7 failed/284 passed，FAILED 与基线 **IDENTICAL** | `/tmp/rfc218_s19_ext_adjacent_rerun.log`＋`_ext_base_norm.txt` diff ∅ |
| 资金 broader | `tests/test_da_prompt_injection.py` **23 passed**（§15 时 2 失败为日期敏感 EventContext 伪影，本轮日期下通过，与本批无关） | `/tmp/rfc218_s19_cap_broader.log` |
| 资金 OFF 逐字节 | **35/35 一致**（§19 改后复验） | `/tmp/rfc218_s19_cap_off_regression_rerun.log` |
| W4/W5/W6 | runtime_context 单文件 4→3=基线；全仓 `grew=[]`；W5/W6 中性 | `daily_invariant_check._check_log_hygiene` 只读复算 |
| 变异测试 | 资金 validator→旧 any()=18 红→恢复 48 绿；黑名单谓词→False=4 红→恢复 29 绿 | `/tmp/rfc218_s19_cap_mutation_red.log`/`_restore_green.log`/`_bl_*` |
| 激活影子探针 | 今日差分 ∅；OFF 泄漏 3/ON 拦截 3/误伤 0 | `/tmp/rfc218_s19_final_guard_shadow.log` |
| 静态 | `check_undefined_names.py` exit0；双解释器（venv py3.11＋EXT cron py3.11）py_compile 全过（含仓外 DA 三文件） | 本批命令留痕 |

既有 18＋7 相邻失败为基线存量（另案治理，本批不修不掩盖）；未跑全量 pytest/CI，不声称全绿。

### 19.5 收益边界（与 §9/§18.5 一致，不得越界表述）

- **本批没有任何收益改善证据**。最终黑名单守卫=禁止语义风控（历史 Playbook QCOM 候选本就是 deterministic SKIP，四笔盈利成交属 EXT，不能记为 Playbook 守卫损失）；资金修复 OFF=生产影响为零；EXT 观测修复不改供给面。
- 正确性≠盈利：指令更有依据、更可解释、被禁标的不再可执行，是**风控与事实正确性**收益；能否提高净收益须独立验证（§10 分层），不能从「模型看得更多/守卫更严」直接推导。
- 不触发加仓、扩权、新方向政策；正式输入/角色逻辑调整仍受 R2、预注册与实验名额约束。

### 19.6 未闭合排队（不变＋新增）

1. 资金激活 BLOCKED 于 §10.4 单变量 A/B 用户裁决；通用 CI 缺 skill 整体 skip 的可见性/仓内合同夹具仍排队（§18.2-5）。
2. EXT GUARD 激活 BLOCKED 于用户裁「QCOM 禁令是否仍适用于 EXT」；首个自然命中提醒到达后携台账裁决。
3. §18.3 低优先级：topup 异常语义、self-managed 边界、符号别名归一、合并日志 blocked count、positions 真实缝测试。
4. §15.7 输入修复队列（健康空财报三态/短 K UNKNOWN/源状态/挑战者一致性）逐批单变量，不与本批捆绑。
5. EXT 相邻测试存量主库写 5 次（§18.4 勘误）另案治理。

### 19.7 回滚

- 最终守卫：`.env.us` 改 `PLAYBOOK_BLACKLIST_FINAL_GUARD=0`（或删行）＋`systemctl restart ftseek-us`＋启动冒烟；或 revert 本批 commit。与 §16 三边界开关互不牵连。
- EXT 观测：additive（modes 键/目录计数/提醒），回滚=revert；`EXT_BLACKLIST_GUARD=0` 不变即供给面旧行为。
- 资金：本就 OFF，无需回滚动作；仓外三文件备份 `*.bak-20261127-rfc218-capital`（§15）＋本批 `/tmp/rfc218_s19_llm_synth.bak/.sha`。

## 20. 实施记录 V（2026-11-27 · 用户裁「请按照建议做优化修复处理」· 盘前复查 Finding 1 · EXT 盘后半日市窗口感知 · 影子先行默认 OFF · 正确性批次非收益补丁）

> 本批只落地盘前复查 **Finding 1**（EXT 盘后静态窗口不识别半日市）。**Finding 2（VIX proxy 19.79 vs estimate 25.4 分歧）维持 §5.4 裁定原样，本批未触**。EXT 是独立预注册实验臂，改窗口=改供给面变量 ⇒ 守卫默认 OFF＋record-only 影子，激活须独立裁决（与 §17/§19 同纪律）。**无 app/ 改动、无重启**（EXT cron 313/314 每 tick fresh-import）。正式输入变量零新增激活。

### 20.1 病灶与口径（盘前复查确认）

- `SESSION_WINDOWS["afterhours"]=(1600,1959)`、`ENTRY_WINDOWS["afterhours"]` A=(1620,1830)/B=(1620,1730) 硬编码常规日。NYSE 半日市（感恩节次日等）13:00 ET 收盘，盘后实际 **13:00–17:00 ET** ⇒ 静态窗在 **17:00–19:59 ET 仍放行**，会对已收市的市场下「盘后」单。今日 2026-11-27 即半日市（`US_EARLY_CLOSE_DATES` 含 `date(2026,11,27)`，`app/utils/holiday_calendar.py:99`）。
- cron 314 冬令时 `2-59/5 4-8 * * 2-6` CST = **ET 15:02–19:57**（CST=UTC+8、EST=UTC-5，差 13h；16:02 起算为夏令时映射，本批按冬令时纠正表述）⇒ 今日真实暴露 tick 为 **ET 17:02–19:57**，其中 A 级窗暴露至 18:30、B 级窗至 17:30。
- **口径换算**：本文件窗口为 **HHMM 整数**（hour*100+min），`get_us_close_minutes_et` 返回**分钟数**（常规 960/半日 780）。盘后时长 4h ⇒ `end_min=close_min+239`：常规 1199→HHMM **1959**（=静态，零变化）；半日 1019→HHMM **1659**（16:59 ET，含末分钟）。日期显式取 `_now_et().date()`（单一时钟源，防 cron CST 日与 ET 日错位）。
- 残余确认（本批不修，排队）：`_pretrade_gate`（:1634 区域）spread/drift/depth/fee fail-closed，但**无报价时间戳 freshness 检查** ⇒ 即便窗口正确，窗内 stale quote 仍可能通过。

### 20.2 修复设计（独立开关，影子先行，只 cap 不扩）

1. **`EXT_AFTERHOURS_HALF_DAY_GUARD` 默认 OFF**：truthy 口径与 `EXT_BLACKLIST_GUARD` 一致（1/true/yes/on），未知值 fail-closed 旧行为，每次实读不缓存。**OFF=逐字节旧静态窗**。
2. **`_in_session_window` / `_entry_window_reason`**：afterhours ∧ ON → `end=min(静态end, _afterhours_end_hhmm())`。**只 cap 结束，不扩起点**——半日市 13:00–16:00 ET 盘后未覆盖属供给面扩张（=改预注册实验变量），非正确性，且 cron 小时段不覆盖，留独立裁决。盘前窗与常规日零变化。
3. **`_afterhours_end_hhmm` fail-safe**：日历 import/调用异常、bool、NaN/inf、None、越界（≤0 或 >960）→ 回退常规 960 ⇒ 1959=静态窗（日历故障不改变旧行为，**绝不因兜底扩窗**；与 §19 资金合同「非 bool/有限」同纪律）。
4. **`EXT_AFTERHOURS_HALF_DAY_SHADOW` 默认 ON**：`_record_afterhours_halfday_divergence` 挂 collect 窗闸前，仅**提前收盘日 ∧ now_hhmm ∈ (半日结束, 静态结束]** 写盘 `data/shadow/ext_afterhours_halfday/YYYY-MM-DD.json`（常规日/区间外零足迹）；OFF 落 `mode=shadow`（供给面不变的差分证据），ON 落 `mode=blocked`（审计）；**分模式 ticks/首末时点从 tick1 留痕**（§18.4 mode 覆写教训，新台账不重蹈）；原子写（tmp+os.replace），写盘失败绝不上抛交易路径。
5. **`VWAP_ANCHOR_WINDOWS`（:856）属分析面非入场闸，本批不动**（半日市尾部无 bar 自然空，不影响入场判定正确性；`EXT_VWAP_ANCHOR_MATURITY=false` 回落分支 :1707 同理保持静态）。
6. **刻意不做**：shadow_registry 注册（属 app/，会触发重启，排队另批）；首命中 push 提醒（本台账先人工/注册表后续批消费）；起点扩窗；VIX（§5.4）；`_pretrade_gate` freshness（独立批）。

### 20.3 验证矩阵（全部可复跑，`PYTHONDONTWRITEBYTECODE=1`）

| 项 | 结果 | 凭据 |
|---|---|---|
| 新契约 | **77 passed**（开关语义/口径换算/fail-safe/常规零变化/半日 OFF 旧行为/ON cap/影子 provenance/collect 集成） | `PYTHONPATH=. ./venv/bin/python -m pytest tests/test_ext_afterhours_halfday.py -q` → `/tmp/rfc218_s20_newtests.log` |
| EXT 相邻 | **7 failed/284 passed**，FAILED 清单与 §19 基线 **IDENTICAL** | `/tmp/rfc218_s20_ext_adjacent.log`＋`_ext_failed_norm.txt` vs `/tmp/rfc218_s19_ext_base_norm.txt` diff ∅ |
| OFF 逐字节全分钟扫描 | 1440 分钟 ×（session＋premarket/afterhours A/B entry）×（半日＋常规）**mismatches=0**；ON 常规日=0；ON 半日 **expansion=0**（只 cap 不扩），分歧=session 180min [1700..1959] / A 91min [1700..1830] / B 31min [1700..1730] | `./venv/bin/python -I /tmp/rfc218_s20_off_sweep.py` → `/tmp/rfc218_s20_off_sweep.log` |
| 变异测试 | 守卫 reader→恒 False = **12 红**（6 truthy＋2 session 越界＋2 entry 越界＋1 provenance＋1 collect）→ 按备份恢复 **77 绿**，SHA `b634db91…` 逐字一致 | `/tmp/rfc218_s20_mutation_red.log`/`_restore_green.log`/`_executor.bak` |
| 静态 | py_compile 双解释器（venv python＋cron 实际 python3.11）exit0；`check_undefined_names.py` exit0 | 本批命令留痕 |
| 真实 env import 冒烟 | guard=False / shadow=True / 今日 end=1659 / SESSION_WINDOWS 静态字典原样 | `/tmp/rfc218_s20_env_import_smoke.log` |

既有 7 个 EXT 相邻失败为基线存量（另案治理，本批不修不掩盖）；未跑全量 pytest/CI，不声称全绿。

### 20.4 收益边界（与 §9/§18.5/§19.5 一致，不得越界表述）

- **本批没有任何收益改善证据**。守卫 OFF ⇒ 当前生产供给面/执行逐字节不变；影子 record-only 不参与判定。
- 半日市收盘后仍可下「盘后」单属**正确性病灶**（closed-market entry＋stale quote 暴露）；修复=交易时段语义/风控正确性。**激活依据只能是时段语义，不是追收益**；正确性≠盈利，能否提高净收益须独立验证（§10 分层）。
- 今日自然分歧影子预计自 **ET 17:02（CST 周六 06:02）**起由 cron fresh-import 落台账（shadow 默认 ON）；落档时点（ET 05:47）尚无自然样本。不触发加仓、扩权、新方向政策。

### 20.5 未闭合排队（新增＋不变）

1. **激活 BLOCKED 于用户独立裁决**（EXT 预注册臂供给面变量）。裁决输入=本节扫描矩阵＋今日自然影子台账＋时段语义复核。激活路径=`.env.us` `EXT_AFTERHOURS_HALF_DAY_GUARD=1`（cron fresh-import 即生效，无需重启）；回滚=改 0/删行。
2. shadow_registry 注册＋目录计数接入＋首命中提醒（app/ 批，需重启，另批）。
3. 半日市 13:00–16:00 ET 盘后起点扩窗（供给面扩张＋cron 小时段变更，独立裁决）。
4. `_pretrade_gate` 报价时间戳 freshness（残余暴露，独立批）。
5. §19.6 队列不变：资金激活 BLOCKED §10.4 A/B；EXT 黑名单 GUARD 激活 BLOCKED 用户裁 QCOM 禁令适用性；topup/self-managed/符号别名等低优先级同域收口；§15.7 输入修复队列逐批单变量。

### 20.6 回滚

- 守卫：`.env.us` 改 `EXT_AFTERHOURS_HALF_DAY_GUARD=0`（或删行），cron 下一 tick 即回旧静态窗，**无需重启**；影子：`EXT_AFTERHOURS_HALF_DAY_SHADOW=0` 停写盘；或 revert 本批 commit。与 §16/§17/§19 各开关互不牵连。

## 21. 实施记录 VI（2026-11-27 · 用户裁「请基于合理方案进行优化，按需重启」· §20.5 #2 EXT 半日市分歧观测闭环 · 单观测变量不改交易行为 · 正确性批次非收益补丁）

### 21.1 病灶与范围

§20 落地后影子台账**有写方、无观测闭环**（与 §18.4 EXT 黑名单同型病灶，§19 已修黑名单侧，本批修半日市侧）：

1. `shadow_registry` 未登记 `ext_afterhours_halfday` ⇒ 看板/周检/挂载核对不可见；
2. `_count_samples` 目录分支只认 `rows` dict 形态（§19 为黑名单建），半日市**按日单文档 ticks 形态**仍落 `return None` ⇒ 自然分歧永不计数；
3. USER_ADJUDICATION 不进普通待裁队列 ⇒ 首个自然分歧日不会自动形成待裁提醒，只能人工翻目录。

本批只补这三点。**单观测变量**：`EXT_AFTERHOURS_HALF_DAY_GUARD` 保持 OFF，供给面/执行逐字节不变；零交易决策输入新增；VIX（§5.4）未触。

### 21.2 改动

1. `app/playbook/shadow_registry.py`（+47/-1）：
   - 登记 `ext_afterhours_halfday_guard_shadow`（OBSERVING；`data_path=data/shadow/ext_afterhours_halfday` 目录；`min_n=1`；USER_ADJUDICATION 不机械迁 READY；`dormant_reason`=仅半日市分歧区间写盘/无目录=通过态/每年约 2-4 天；`stale_expected`＋`eval_on_demand`；`config_key=None` 防误判停；notes 写明真停写机制=`SHADOW=0 ∧ GUARD=0`、计数轴、首命中提醒、cron fresh-import 与注册表随批重启的生效差异）。
   - `_count_samples` 目录分支新增 ticks 形态：`ticks` 为 int ∧ 非 bool ∧ ≥1 ⇒ 该日计 **1 样本（计数轴=分歧日数，不是 ticks 累加**；跨 2 日 ticks 18+3=2 不是 21）。`rows` 形态（黑名单）与文件型分支逐字不变；非法 ticks（0/bool/字符串/缺失）不计、不伪装样本。
2. `scripts/extended_hours_entry_executor.py`（+41）：
   - `_EXT_AFTERHOURS_HALF_DAY_CANON` 生产规范路径守卫：hermetic 测试 monkeypatch DIR⇒跳过提醒，防测试误发真实推送（§19 `_EXT_BLACKLIST_CANON` 同纪律）。
   - 写盘成功后 `ticks==1` ⇒ `_notify_ext_afterhours_halfday_first_hit(day, blocked)`：按 **ET 日**去重（state 文件 `data/shadow/ext_afterhours_halfday_first_hit_notified.json`；半日市每日=一个独立事件，不按 tick 重复推）；经 `app/push/hub.push_text_safe(sync=True)`（cron 脚本硬退出安全，禁直连 notifier）；文案明示「提醒≠启用、激活须用户单独裁决」＋mode（shadow=守卫OFF/blocked=守卫ON）＋裁决输入指引；异常 print-only 永不上抛、失败不写去重 state。

### 21.3 验证矩阵（全部可复跑，`PYTHONDONTWRITEBYTECODE=1`）

| 项 | 结果 | 凭据 |
|---|---|---|
| 批内契约 | **99 passed**（77 既有半日市＋8 既有目录计数＋**8 新首命中**＋**6 新半日市计数**） | `PYTHONPATH=. ./venv/bin/python -m pytest tests/test_ext_afterhours_halfday.py tests/test_shadow_registry_dir_count.py -q` |
| EXT 相邻 | **7F/284P**，FAILED 清单与 §19 基线 **IDENTICAL** | `/tmp/rfc218_s21_ext_adj.log`＋`_ext_adj_norm.txt` vs `/tmp/rfc218_s19_ext_base_norm.txt` diff ∅ |
| 注册表相邻 | **19 passed**（rfc207＋criteria_provenance＋shadow_board_guard） | `/tmp/rfc218_s21_registry_adj.log` |
| 变异测试 | **3 红→恢复 99 绿**：①删 ticks 非 bool 守卫→invalid_ticks 红；②删 ET 日去重→dedup 红；③sync=True→False→canon_once 红（只动 canon 守卫之后逻辑，防测试误发真实推送） | 本批命令留痕（try/finally 恢复，`git diff --stat` 核对） |
| 静态 | py_compile 双解释器（venv python＋cron python3.11）exit0；`check_undefined_names.py` exit0 | 本批命令留痕 |
| 挂载核对 | `check_mounting()` 本条目 **0 告警**（全局 5 条均为既有存量：budget_p3_sizing/confidence_tier_gate/instruction_conflict/backfill_fidelity/us_overnight）；real-base `_count_samples`=None（无目录=通过态） | 本批命令留痕 |

既有 7 个 EXT 相邻失败为基线存量（另案治理，本批不修不掩盖）；未跑全量 pytest/CI，不声称全绿。

### 21.4 部署（重启批：注册表属 app/）

- 提交 `a7dd63c00`（4 文件 +236/-1：executor＋registry＋两测试文件；DEB-20261125 其他会话 WIP 未触碰未提交）。
- 重启前快照 `/tmp/rfc218_s21_pre_restart.json` 与 §19 快照**逐项 IDENTICAL**：SIM SHORT BA 23/BABA 47/GS 5、outbox 22 全 BOOKED、PENDING 4395、今日 0 指令、被禁 PENDING 28 条存量（含历史 BUY id 264/457）。在途出场核对：`trade_records exit_time IS NULL`=BA/BABA/GS（与持仓一致）、`exit_ledger.exit_intents/exit_legs` 空；`git status --porcelain -- app/ scripts/` 干净。
- `systemctl restart ftseek-us` → 新 PID **182608**（19:08:55 CST）；启动冒烟 **7/7 exit0** `/tmp/rfc218_s21_startup_smoke.log`。
- 重启后快照 `/tmp/rfc218_s21_post_restart.json` **逐项 IDENTICAL**；`/proc/182608/environ`：`EXT_AFTERHOURS_HALF_DAY_GUARD=0`/`SHADOW=1`、`EXT_BLACKLIST_GUARD=0`/`SHADOW=1`、`PLAYBOOK_BLACKLIST_GUARD=1`/`FINAL_GUARD=1`、**无 `DA_CAPITAL_CODE_FIX`**；重启后日志 0 Traceback/CRITICAL。
- 生效路径：写方＋提醒经 cron 313/314 每 tick fresh-import（无需再重启）；注册表随本次重启已加载。cron 314（`2-59/5 4-8 * * 2-6`）覆盖今日分歧窗。

### 21.5 收益边界（与 §9/§18.5/§19.5/§20.4 一致）

- **本批没有任何收益改善证据**。观测闭环不改供给面/执行；守卫 OFF ⇒ 生产交易行为逐字节不变。
- 首个自然分歧日预计 **ET 17:02（CST 周六 06:02）**起落台账并**自动推送待裁提醒**（提醒≠启用）；落档时点（CST 19:10）尚无自然样本。
- 激活仍 **BLOCKED 于用户独立裁决**（EXT 预注册臂供给面变量；裁决输入=§20.3 全分钟扫描＋自然影子台账＋时段语义复核；激活依据=交易时段正确性，不是收益）。

### 21.6 未闭合排队（更新）

1. 半日市守卫激活 BLOCKED 于用户独立裁决（不变；本批补齐其裁决输入的自动送达）。
2. ~~shadow_registry 注册＋目录计数接入＋首命中提醒~~ → **本批闭合**。
3. 半日市 13:00–16:00 ET 盘后起点扩窗（供给面扩张＋cron 小时段变更，独立裁决；不变）。
4. `_pretrade_gate` 报价时间戳 freshness（残余暴露，独立批；不变）。
5. §19.6 队列不变：资金激活 BLOCKED §10.4 A/B；EXT 黑名单 GUARD 激活 BLOCKED 用户裁 QCOM 禁令适用性；topup/self-managed/符号别名等低优先级同域收口；§15.7 输入修复队列逐批单变量。

### 21.7 回滚

- revert 本批 commit＋重启（注册表回退）；或仅删注册项（看板不可见但写方/提醒仍在）。
- 停提醒/停写盘：`.env.us` `EXT_AFTERHOURS_HALF_DAY_SHADOW=0`（cron fresh-import 即生效，无分歧落盘⇒无首命中）；守卫回滚=`EXT_AFTERHOURS_HALF_DAY_GUARD=0`（本就 OFF）。
- 与 §16/§17/§19/§20 各开关互不牵连；state 文件删除仅导致对应日重新提醒一次，无交易影响。

## §22 独立审计正确性批（2026-11-27）：未知净额统一合同＋EXT 推送送达合同＋NO_TRADE 实验跨轮预 sizing＋黑名单异常可见性（3 P1＋1 P2，纯正确性，无收益声称）

### 22.0 背景与病灶（用户转发独立审计，隔离合成检查实锤）

审计以真实函数隔离合成复核确认四处合同分歧/缺口（**函数合同分歧已确认；未证明当前生产存在缺失行/已污染正式 DA 判断/已发生实盘泄漏**）：

1. **P1-A 净额三套读数分叉**（`daily_review.py:1167` / `lesson_injector.py:339`）：
   | 输入 | 官方评分净额 | 日报净额(旧) | 教训helper净额(旧) |
   |---|---|---|---|
   | 毛10、费未知、净未知 | 不可测 | 10 | 10 |
   | 毛10、费4、净缺失 | 6 | 10 | 6 |
   日报可能把毛利当净利；缺费时教训 helper 回退毛收益；教训验证存在用该 helper 更新经验分值/有效期的消费链 ⇒ 未知收益可自证经验正确。
2. **P1-B EXT 推送失败仍记"已通知"**（`extended_hours_entry_executor.py:348/1009`＋`app/push/hub.py:448`）：`push_text_safe(sync=True)` 返回 bool，**False=失败不抛异常**。两个首命中 helper 不查返回值直接写去重 state。审计以真实 helper AST＋内存路径复现：第一次 hub False→仍登记 notified；第二次因 notified 存在→永不重试。且写方只在首命中调 helper ⇒ 只修"失败不登记"不够，还需有限重试路径。
3. **P1-C（潜伏）NO_TRADE 实验跨轮预 sizing 绕过**（`auto_execute_flow.py:1116` / `db.py:908`）：某轮 reprice 写正 position_size→暂缓未下单→下一轮 SELECT * 回读已有股数→进入 NO_TRADE 实验轮→正股数跳过部分 regime 重评、实验 sizing 及 cohort 写入。隔离分支复核确认已有数量被原样保留。**非已证明的当前 DA 事故**（DA 生成端通常不预 sizing，归档三笔实验成交有标记）；风险在跨轮/其他生成路径，生产命中次数未知。
4. **P2 黑名单配置异常静默变空名单**（`watchlist_manager.py:81–87`）：内部读取异常被捕获后只打 debug 返回空列表，外层守卫当正常空名单，外层 warning 接不到真实异常。**已确认的是异常可见性缺口**；既定 fail-open 行为本批不改 fail-close（须另行裁决）。

### 22.1 P1-A 修复：未知净额单一真源（app/review/net_basis.py 新增）

- `resolve_net_pnl(trade)->Optional[float]`，与官方评分器 `scripts/okr_2026_11_scoring._row_net/_num` **逐字同语义**（490 组合 parity 矩阵契约钉死）：①`pnl_net` 已知有限值→原样保留；②净缺失→**仅当** `realized_pnl`(毛)∧`fees_est`(费) 均已知才回算 毛−费；③其余→None（不可测）。**未知绝不归零、绝不用毛顶替**；None/""/NaN/inf/非数→缺失。
- `daily_review._generate_summary`：聚合只覆盖可测样本（`_measurable_pairs`）；胜率/均额/losing/fake_wins/最大单笔分母=可测笔数；新增覆盖面披露键 `net_measurable_n`/`net_unknown_count`（空日 base 亦含）；`pnl_basis` 保持 "net:" 前缀；毛口径 `total_pnl_gross` 仅供渲染不参与裁决。`_calculate_review_score` 胜率同改 closed∧可测净口径。
- `lesson_injector._trade_net_pnl` 委托 helper（Optional）；`validate_lessons` 剔除不可测样本（premarket/high_signal 两臂），**无可测样本→不更新**（未知收益不自证经验正确）；返回 dict 形态不变。**不清空、不重打分、不改写历史教训**。
- `scheduler_triggers._format_daily_review`：`net_unknown_count>0` 时追加「（N 笔净额未知, 未计入净额/净胜率）」；旧 JSON 无键→不追加（向后兼容）。
- **完整旧数据（pnl_net 在场）结果逐字不变**（契约测试以完整数据 fixture 原断言 5/5/3 钉死）。

### 22.2 P1-B 修复：EXT 首命中推送送达合同（确认成功才登记＋有限重试）

- 两 helper（黑名单/半日市）同改：`_ok=push_text_safe(...,sync=True)`；**True→state[key]=ISO 时间戳字符串（旧格式保留=已通知）**；**False→`{"notified":False,"attempts":N,"last_attempt":ts}`＋print 警告，保留待处理**；旧字符串条目=成功→去重跳过；dict 已 notified 或 `attempts≥_FIRST_HIT_PUSH_MAX_ATTEMPTS(=3)`→跳过（防无限刷屏）；**抛异常→不写 state**（既有契约钉死）。
- **调用方改为每次命中/tick 都调 helper**（移除 `if _is_new`/`if ticks==1` 门），重试路径真实存在；helper 自身去重保证成功后不重发。
- 测试覆盖**真实 False 合同**（spy 返回 False，不只覆盖"桩抛异常"）：False→下次重试→成功登记；3 次耗尽停止；旧字符串去重；后续 tick 重试。仍走 `app/push/hub`，零直连 notifier/webhook。
- 改善待裁事项可见性，**不改交易规则**；两守卫保持 OFF。

### 22.3 P1-C 修复：NO_TRADE 实验放行独立核对最终数量（同源重算，非二次缩放）

- `auto_execute_flow`：`_nt_exp_resizing = (_nt_short_exp ∧ _inst_ps>0 ∧ direction∈{SELL_SHORT,SHORT})`；放行条件 `if (_inst_ps<=0 or _nt_exp_resizing) and _new_sl>0:` → 实验轮携带正股数也**强制走同源 SSOT 重算**（regime 重评＋实验 sizing＋cohort 写入），并 info log 留痕。
- **不是把已有股数再乘 0.3**：重算从 SSOT 出发，已按 RISK_OFF 缩过的数量不会重复缩小（幂等契约：12→12 钉死）；实验轮外携带股数逐字节不变（控制契约钉死）。零新开关、零方向/阈值/仓位政策变更。

### 22.4 P2 修复：黑名单读取异常可见性（fail-open 保持）

- `_get_global_blacklist` except：`logger.debug`→`logger.warning(..., exc_info=True)`（W4/W6 双棘轮合规），**保留 fail-open 返回 []**。异常即停新仓（fail-close）须另行裁决，本批不改。测试覆盖"真实 getter 内部吞异常"（WARNING＋栈＋fail-open 空列表）。

### 22.5 相邻测试隔离修补（本批附带，零生产语义变更）

1. `tests/test_daily_review.py` fixture 原只有 `realized_pnl`(毛) 无 `pnl_net`/`fees_est` — 旧断言把"毛当净"缺口掩盖。补齐两列成**完整旧数据**，原断言 (5/5/3) 逐字保留钉死不变性；新增全未知（0 胜/覆盖 5 笔未知/毛口径如实披露）与部分未知（分母=可测 3 笔，win_rate=1.0 非 3/5）两例。
2. `tests/playbook/test_blacklist_final_guard.py` 两个 **DB 闩**测试（`test_guardrail_skip_latch_pending_only`/`_non_pending_state`）未隔离开关：§19 激活（`.env.us:232 PLAYBOOK_BLACKLIST_FINAL_GUARD=1`，16:41）后写入侧守卫把 QCOM/LCID/TME 在 save 时即转 SKIP，污染"已执行不改"断言。**生产闩本身正确**（mark() 三断言全过；`PLAYBOOK_BLACKLIST_FINAL_GUARD=0` 环境覆跑 2 passed 实证）。修补=两测试显式 `_flag(monkeypatch,"0")` 与激活态解耦。诊断链：单独跑红→stash 本批 app/scripts 重跑**仍红**（HEAD 既有）→env 覆跑绿→定位激活泄漏。
3. `tests/test_review_source_attribution.py::test_weekly_signal_funnel_reports_source_direction`（`no such column: row_kind`）为**已知既有红点**（handoff.md 2026-11-11 前后条目已列「weekly row_kind 1」；fixture 表结构落后于 10-27 `ROW_KIND_EXCLUDE_PLACEHOLDER_SQL`），stash 重跑同样红 ⇒ 非本批引入，**本批不修不掩盖**（另案）。

### 22.6 验证（全部 PYTHONDONTWRITEBYTECODE=1，venv pytest）

- 批内契约 **869 passed/1 skipped**（skip=既有 real-JSON 门）：`PYTHONPATH=. ./venv/bin/python -m pytest tests/review/test_net_basis_contract.py tests/review/test_daily_pnl_basis_render.py tests/review/test_lesson_outcome_validation.py tests/test_daily_review.py tests/test_watchlist_blacklist_visibility.py tests/test_ext_blacklist_guard.py tests/test_ext_afterhours_halfday.py tests/playbook/test_entry_data_bounded_fetch.py tests/playbook/test_blacklist_final_guard.py -q` → `/tmp/rfc218_s22_contracts.log`。新增契约含：490 组合 parity 矩阵、审计合成表两行、summary 覆盖面/分母、review-score 净胜率、教训未知剔除、EXT False→重试→成功/耗尽/旧格式去重/后续 tick 重试（两 helper）、跨轮 40→12＋cohort vix=22/score=-5、幂等 12→12、实验轮外 40 不变、黑名单 WARNING+栈+fail-open。
- EXT 相邻全 glob **7F/376P** FAILED 与 §19 基线 **IDENTICAL**（`/tmp/rfc218_s22_ext_adj.log`＋`_norm.txt` diff ∅）；§21 选择集（glob 减半日市）**7F/288P** IDENTICAL（`/tmp/rfc218_s22_ext_adj_s21sel.log`；284 基线+4 新增；383=291+88+4 口径对账）。
- Playbook 相邻 `tests/playbook/ tests/test_symbol_registry_p1p2.py` **18F/1572P** FAILED 与 §19 基线 **IDENTICAL**（`/tmp/rfc218_s22_pb_adj_rerun.log`＋`_norm.txt` diff ∅；1569 基线+3 新增）。首跑 20F/1570P 的 2 额外失败经 §22.5#2 诊断为激活泄漏并修补后复跑一致。
- Review 相邻 **1F/822P/1s**（`/tmp/rfc218_s22_review_adj_rerun.log`）：唯一失败=§22.5#3 已知既有 weekly row_kind；其余全绿。
- 变异 **5 红→恢复绿**（sha256 逐字节还原核验）：M1 net_basis 毛回退+归零（216 红）；M2a/M2b EXT 忽略返回值（各 3 红）；M3 sizing 条件回退（2 红）；M4 watchlist warning→debug（2 红）。日志 `/tmp/rfc218_s22_M*_{red,green}.log`， runner `/tmp/rfc218_s22_mutate.py`。
- `py_compile` venv＋cron python3.11 双解释器 exit0；`scripts/check_undefined_names.py` 7 生产文件 exit0。
- W4/W5/W6 日志卫生：`_check_log_hygiene` 全仓扫描 **W4 status=ok grew=[] new_files=[]**，watchlist_manager 2→1 **healed**；W5/W6 本批 7 文件逐文件 HEAD 对比 **零新增**（W6 全仓 drift 为 HEAD 既有存量：position_sync/risk_gates/trail_state_store/late_fill_evidence 等，非本批文件，另案）。
- DB 全程 `mode=ro`+`query_only`；零订单/付费 LLM/方向/阈值/仓位政策/live 权限变更；零清空/重打分/历史教训改写。

### 22.7 部署（重启批：app/ 运行时改动）

- 提交 `5405c5f7a`（14 文件 +755/-49：7 生产＋7 测试；`git show --stat` 可核）＋本 §22＋handoff 条目（补记提交）；推送 origin/main。
- 重启前快照 `/tmp/rfc218_s22_pre_restart.json`（§21 形态，脚本 `/tmp/rfc218_s22_pre_restart.py`）：SIM SHORT BA 23/BABA 47/GS 5、outbox 22 全 BOOKED、PENDING 4395、今日 0 指令、被禁 PENDING 28 存量；在途出场核对＝trade_records 未平=3 仓一致（全 PLAYBOOK_SIM）、LIVE 0、`exit_ledger.exit_intents/exit_legs` 0/0；`git status --porcelain -- app/ scripts/` 干净（仅本批已提交）。
- `systemctl restart ftseek-us` → 新 PID **191013**（20:15:52 CST）；启动冒烟 **7/7 exit0** `/tmp/rfc218_s22_startup_smoke.log`；重启后快照 `/tmp/rfc218_s22_post_restart.json` 除 PID 外**逐项 IDENTICAL**；`/proc/191013/environ`：EXT_AFTERHOURS_HALF_DAY_GUARD=0/SHADOW=1、EXT_BLACKLIST_GUARD=0/SHADOW=1、PLAYBOOK_BLACKLIST_GUARD=1/FINAL_GUARD=1、**无 DA_CAPITAL_CODE_FIX**；重启后日志段（272 行）0 Traceback/CRITICAL。
- 生效路径：日报/教训/复盘评分随服务重启加载；EXT 两 helper 经 cron fresh-import 即生效；无新开关、无 .env 变更（FINAL_GUARD=1/BLACKLIST_GUARD=1 维持 §19 激活态，EXT 两守卫与半日市守卫维持 OFF，DA_CAPITAL_CODE_FIX 维持未设）。

### 22.8 收益边界（与 §9/§18.5/§19.5/§20.4/§21.5 一致）

- **本批没有任何收益改善证据**。四处修复均为读数/送达/实验隔离/可见性正确性，不改方向/阈值/仓位/退出/live 权限；完整旧数据结果不变；未知样本从"伪净利/伪胜场/自证经验"中剔除是**口径正确化**，不是盈利补丁。
- 资金分布修复**正确表述**（ supersede 早期"修复尚未实现"）：双路径修复＋八字段/有限数值/非 bool/缺失 provenance 合同＋独立开关**已有实现与测试记录**（§8/§15，35/35 逐字节复验），但 `DA_CAPITAL_CODE_FIX` 保持未设、正式输入未激活，**开启态的模型及收益效果未证明**；本体在仓外，本轮未读取验证。

### 22.9 未闭合排队（审计边界，本批不修不掩盖）

1. **adapter 刷新≠新版候选进指令库**（`generation_flow.py:111–117` 按日完成标记先检）：bridge 更新 AAA+BBB→adapter 刷新成功→当日完成标记存在→不检查新版 supply/不补 BBB。**语义未裁决**：同日重跑仅更新研究候选，还是构成新指令生成事件？裁决前不强制全量重生成、不删完成标记（删标记可能恢复重复生成/长步阻塞旧问题）。
2. **bridge 稳定性检查硬化空间**：目前主要比较 mtime/size，相同 mtime/size 的身份替换旧快照可能被接受；但 SHA 绑定实际读到的内容，下一轮原则上能发现新 SHA。**不夸大为已发生混合字节/错误成交**。
3. **EXT 半日市守卫仍 OFF**：实现＋观测闭环已有（§20/§21），开启只缩结束至 16:59 不扩 13:00–16:00 起点；半日市 17:00–19:59 旧窗仍开放=**未激活的已知政策残余**，非新增代码回归。激活裁决优先排队（本批无授权未启用）。
4. **EXT 观测副作用与并发边界**：规范路径 dry=True 可能仍写观测并触发提醒；半日市观测 writer 位于 executor 互斥锁之前；无锁读-改-写＋共享临时文件重叠调用可能漏计/丢 mode。**生产是否有外层互斥未核，不声称已发生并发损坏**。dry 是否必须静默＋观测写入/通知去重原子性须明确（独立批）。
5. **未来 SHORT 实盘镜像隔离未闭合**（`risk_gates.py:646–659`）：镜像入口未按 no_trade_short_exp 排除历史实验指令；未来 SHORT 真实目标门获允许时跨 regime 遗留实验指令仍可能被镜像。**当前非实盘泄漏证明；SHORT 实盘授权前必须先补该隔离**。
6. 净额合同同域残余（本批范围外）：Obsidian 明细 'pnl' 映射（`daily_review.py:1767`，writer `obsidian_writer.py:863` `:+.2f` 遇 None 会崩）；周/月报 SQL COALESCE（~1533–1538）；`_analyze_overnight_risk`（~1107）。
7. §19.6/§21.6 队列不变：资金激活 BLOCKED §10.4 A/B；EXT 黑名单 GUARD 激活 BLOCKED 用户裁 QCOM 禁令适用性；短 K UNKNOWN/健康空财报/来源状态/挑战者事实一致性逐批单变量；watchlist 异常即停新仓（fail-close）另行裁决。

### 22.10 回滚

- revert 本批 commit＋`systemctl restart ftseek-us`（日报/教训/评分/sizing 回退）；EXT helper 经 cron fresh-import 随 revert 即回退。
- 无开关可回滚（本批零新开关）；state 文件新格式条目（dict）旧代码读为"非字符串→未通知"会重发一次提醒，无交易影响；删除 state 文件仅导致重新提醒一次。
- 与 §16/§17/§19/§20/§21 各开关互不牵连；测试隔离修补（§22.5）随 revert 一并回退。

## 23. 实施记录 VIII（2026-11-27 · 用户裁「请按照建议执行落地优化」· 审计边界闭合批：SHORT 实盘镜像实验隔离＋bridge 身份硬化＋EXT 观测原子性＋半日市守卫激活）

> 本节追加，不改 §§0–22 历史。本批承接用户转发独立审计「四、不能当作已完全闭合的边界」中可代码闭合的三项（§22.9 #2/#4/#5）＋#3 激活裁决。**授权表述（显式）**：审计原文「建议优先完成该项激活裁决，但本轮没有授权启用」；用户随后裁「请按照建议执行落地优化」——本批将该指令解读为对半日市守卫激活的裁决（审计建议本身即「优先完成激活裁决」），依据=**时段语义正确性**（今日 NYSE 半日 13:00 ET 收盘，静态窗 17:00–19:59 ET 对已收市市场放行），非收益。若用户本意不含激活，回滚=`.env.us` 改 `EXT_AFTERHOURS_HALF_DAY_GUARD=0`，下一 tick 即生效，首个暴露 tick（CST 周六 06:02）前有约 9 小时零差异窗口。**EXT_BLACKLIST_GUARD 与 DA_CAPITAL_CODE_FIX 保持 OFF，未捆绑**。

### 23.1 P1 潜伏 · SHORT 实盘镜像实验隔离（§22.9 #5 闭合）

- 病灶：`risk_gates.py` 镜像入口未按 `no_trade_short_exp` 排除历史实验指令。当前 live 闸只收 LONG/BUY（`live_arm_gate.py:235–240` `LIVE_PROBE_DIRECTION_REJECTED`）⇒ **本路径今日零流量、零行为变化**；未来 SHORT 真实目标门获允许时，跨 regime 遗留 SIM-only 实验指令仍可能被镜像。
- 合同（纵深防御，新增 `_parse_metadata_blob`/`_no_trade_short_exp_marker`/`_no_trade_sizing_regime`/`_is_no_trade_short_experiment_instruction`）：
  1. **标记存在性判定（非 truthy）**：`no_trade_short_exp` 键 `is not None` 即标记——空 dict `{}` 标记也是标记（truthy 判定会漏，变异测试钉死）；内存 ∨ 持久化任一源命中 → 排除，任意方向。
  2. **次级证据**：SHORT ∧ `reprice_audit.regime_reads.sizing.regime=="no_trade"`（大小写不敏感）→ 排除（覆盖标记写失败但 reprice 审计在库）。
  3. **SHORT fail-closed**：两源 metadata 均不可读（缺失/坏 blob/行缺失/读失败/无 id）→ 排除（`short_metadata_unverifiable_fail_closed`）；行存在且 `metadata_json` NULL = 一次成功读（库内无标记）→ 放行。
  4. **LONG/BUY 无标记永不被本谓词排除**（实验仅 SHORT）；谓词自身异常 → SHORT fail-closed / LONG fail-open。
  5. 持久化源=`route_playbook_db("sim").get_instruction_by_id`（镜像恒 SIM→LIVE）；内存 metadata 可能滞后于 sizing 期 merge ⇒ 持久化读为权威，内存只能追加排除不能撤销。
- 隔离**先于臂闸**；拒绝走同一 `(None, gate)` 合同，`reasons=[{code:"NO_TRADE_SHORT_EXP_ISOLATION",...}]`，调用方日志/推送路径不变；`experiment_check` 可注入（测试缝）。`pipeline.py` 转发器签名不变。
- **SHORT 实盘授权裁决时必须复核本谓词**（隔离是授权前置条件，不是授权本身）。

### 23.2 bridge 稳定性身份硬化（§22.9 #2 闭合）

- `_read_bridge_raw` 旧校验只比 `(mtime_ns, size)`；保持 mtime/size 的身份替换（rename 覆盖＋utime 还原）可穿透。现四元组 `(st_dev, st_ino, st_mtime_ns, st_size)` 四 stat 三比对：路径 stat(读前) ↔ fstat(打开 inode) ↔ fstat(读后) ↔ 路径 stat(读后)；任一不等 → None＋warning（不解析、不注入、不消耗基线）。
- 稳定文件四点全等 ⇒ 逐字节旧行为；返回合同 `(raw, st_after.st_mtime)` 不变。
- **残余（不夸大）**：原地写＋同 size＋utime 还原仍理论可穿透；SHA 基线绑定实际读入内容，下一轮重载原则上发现新 SHA。未发生混合字节/错误成交的声称不变。

### 23.3 EXT 观测原子性（§22.9 #4 部分闭合：并发面；dry 语义仍待裁）

- 新增 `_observation_lock`（flock LOCK_EX 阻塞式短临界区，锁文件与台账同目录族）＋`_atomic_write_json`（mkstemp 唯一 tmp＋os.replace，替代共享 `.json.tmp`）。
- 包裹四处读-改-写：`_record_ext_blacklist`（`ext_blacklist.lock`）、`_record_afterhours_halfday_divergence`（`ext_afterhours_halfday.lock`）、两个 `_notify_*_first_hit` 的 state RMW（`*_first_hit_notified.lock`，**含 sync push 在锁内串行**防重叠调用双推/丢 attempts）。notify 调用在台账锁**外**（无嵌套锁序）。
- 半日市 writer 仍在 executor 主互斥锁之前（§22.9 #4 原观察）——本批以观测面独立 flock 闭合漏计/丢 mode，不移动 writer 位置（移动=改执行时序，另批）。
- **dry=True 语义不变**（规范路径 dry 仍写观测并触发提醒）——「dry 是否必须静默」仍为用户待裁项，本批不擅改。
- 首命中提醒文案按 mode 分流（§23 激活配套）：blocked(守卫ON) → 「已按日历 cap 至 16:59 ET＋审计留痕＋回滚锚点」，**不再发「激活须裁决/提醒≠启用」**（激活后该措辞失真）；shadow(守卫OFF) 文案逐字保留。推送仍走 `app/push` hub，零直连。

### 23.4 半日市守卫激活（§22.9 #3 · 预注册臂供给面变量修订留痕）

- **激活**：`.env.us` `EXT_AFTERHOURS_HALF_DAY_GUARD=0→1`（21:15 CST）；cron 313/314 每 tick fresh-import 即生效，**无需重启**（服务进程不运行 EXT 执行器）。影子保持 ON：分歧 tick 落 `mode=blocked` 审计。
- 裁决输入复核：① 今日真实日历 `get_us_close_minutes_et(2026-11-27)=780`（常规周一=960）；② off_sweep 全分钟复跑 **SWEEP_ALL_OK**（OFF 半日/常规 mismatches=0；ON 常规=0；ON 半日 **expansion=0**，分歧 session 180min[1700..1959]/A 91min[1700..1830]/B 31min[1700..1730]）；③ 时段语义＝§20.1（半日盘后实际 13:00–17:00 ET，静态窗尾对已收市市场放行）。
- **自然影子台账尚不存在**（首个分歧 tick=CST 周六 06:02=ET 17:02，cron 314 `2-59/5 4-8 * * 2-6`）。§20.5 原裁决输入含「今日自然影子台账」；本批在窗口前激活的理由：**守卫 OFF 等待自然样本=接受今日真实 closed-market 暴露**（观测窗即暴露窗），而分歧机制是日历确定性的（扫描已全分钟证明）。激活后影子继续以 blocked 模式留痕，审计链不断。
- **预注册修订留痕**：EXT 臂供给面仅在「半日市∧盘后∧ET 17:00–19:59」收窄（只 cap 不扩；13:00–16:00 起点扩窗仍不做）；常规日/盘前逐字节不变。今日之前零行为差异（激活时点 21:15 CST，今日剩余 tick 全为盘前，不受影响）。
- 激活冒烟（fresh-import 干净 env，dotenv 自加载）：guard=True/shadow=True/静态 SESSION_WINDOWS 原样/今日 end=1659/常规 end=1959；afterhours@1702=False、@1659=True、@1959=False、premarket@800=True；A/B 入场窗同口径 cap。

### 23.5 验证矩阵（全部可复跑，`PYTHONDONTWRITEBYTECODE=1`）

| 项 | 结果 | 凭据 |
|---|---|---|
| 新契约（镜像 22＋原子性 7＋半日市 89 含文案分流 1 新增） | **118 passed** | `/tmp/rfc218_s23_newtests_final.log` |
| EXT 相邻全 glob＋bridge | **7F/405P**，FAILED 与 §22 基线 **IDENTICAL**（diff ∅） | `/tmp/rfc218_s23_ext_bridge_adj.log`＋`_ext_failed_norm.txt` |
| Playbook 相邻 | **21F/1591P**＝18F 基线 IDENTICAL＋3 负载 flake（DA 同机运行，200ms 实时超时）＋22 新镜像测试；3 flake 隔离复跑 **3P** | `/tmp/rfc218_s23_pb_adj.log`＋`_pb_failed_norm.txt` diff；`/tmp/rfc218_s23_flake_rerun.log` |
| 变异测试（3 红→恢复绿，sha256 逐字核验） | 标记存在性→truthy **2 红**；bridge sig 删 inode **1 红**（S20）；观测 flock 删除 **1 红**（并发丢更新） | `/tmp/rfc218_s23_pre_mutation_sha.txt`；恢复后 SHA d5f2340c…/9b6862f9…/9eaf3cd8… 一致 |
| off_sweep 复跑（.env.us=1 后探针修正：import 后显式压 0） | **SWEEP_ALL_OK** expansion=0 | `/tmp/rfc218_s23_off_sweep_rerun.log`（探针 `/tmp/rfc218_s23_off_sweep.py`） |
| 激活冒烟 | guard=True/end 今日 1659/常规 1959/只 cap 不扩 | 本节 §23.4 命令留痕 |
| 静态 | py_compile venv＋cron `venv/bin/python3.11` 双解释器 exit0；`check_undefined_names.py` exit0 | 本批命令留痕 |
| W4/W5/W6 日志卫生 | W4/W5 **grew=[] new_files=[]**；W6 risk_gates 本批编辑后 **HEAD 11=WT 11 中性**（新增 warning 补 exc_info=True） | `_check_log_hygiene` 只读复跑 |

- **W6 全仓既有 HEAD 存量 drift（非本批引入，另案）**：risk_gates L84（P2 wave hotfix）/position_sync 14→16（1c7cc31bc）/auto_execute_flow 13→17（ad44b0d34 Nov25 实验＋567f09cbc）/trail_state_store 9→10（567f09cbc）/late_fill_evidence 新文件（ebc36d4e1）。基线 last tightened 80d8817ba（10-08）。本批不修不掩盖不升基线。
- 既有 7＋18 相邻失败为基线存量未修；未跑全量 pytest/CI，不声称全绿。DB 全程 `mode=ro`+`query_only`；零订单/付费 LLM/方向/阈值/仓位政策/live 权限变更。

### 23.6 部署（重启批：app/ 运行时改动）

- DA 主批（21:02 CST 起跑，timeout 3270s）**~21:41 自然完成**（bridge 8 候选写库＋Obsidian 盘前归档 ✅，进程退出，非超时 kill）；重启在其后执行，未中断 DA。
- 重启前核：① 在途出场＝trade_records 未平 3 仓一致全 SIM SHORT（BA 23/BABA 47/GS 5），LIVE 0，exit_ledger intents/legs 0/0；② `git status --porcelain -- app/ scripts/` 干净（代码先提交，无其他 agent WIP）。
- 代码提交 `8acc5bdab`（7 文件 +835/-155：pipeline/risk_gates/executor＋4 测试〔镜像 22 例/原子性 7 例新文件〕，`git show --stat` 可核）；落档提交（本节＋DEB＋ANALYSIS＋handoff）sha 见 handoff 同日条目补记。
- 重启前快照 `/tmp/rfc218_s23_pre_restart.json`（PENDING 4403＝§22 的 4395＋今日 DA 8；today_instructions=8；outbox 22 全 BOOKED；被禁 PENDING 28 存量）。
- `systemctl restart ftseek-us` → 新 PID **208810**（21:46:09 CST）；启动冒烟 **7/7 exit0** `/tmp/rfc218_s23_startup_smoke.log`。
- `/proc/208810/environ`：HALF_DAY_GUARD=**1**/SHADOW=1、EXT_BLACKLIST_GUARD=0/SHADOW=1、PLAYBOOK_BLACKLIST_GUARD=1/FINAL_GUARD=1、无 DA_CAPITAL_CODE_FIX/DA_SHORTK_NA。
- 重启后快照 `/tmp/rfc218_s23_post_restart.json` 除 PID 外逐项 **IDENTICAL**。
- HK 只读探针（DEB §5）：ret_code=0，HK.00700 快照可读（腾讯 352.0，+3.23%，update_time 16:07:57 HKT）——行情权限可用；**未建采集器/未加 cron**。

### 23.7 收益边界（与 §9/§18.5/§19.5/§20.4/§21.5/§22.8 一致）

- **本批没有任何收益改善证据**。镜像隔离今日零流量（LONG/BUY-only 闸）；bridge 硬化只拒不稳定读；观测原子性只防漏计/双推；半日市激活只缩已收市窗口。全部为正确性/风控/观测修复，不改方向/阈值/仓位/退出/live 权限。
- 半日市激活依据=时段语义，**不是追收益**；被 cap 的 17:00–19:59 ET 窗对已收市市场，历史该窗成交质量未单独验证（不声称「避免了亏损」）。

### 23.8 未闭合排队（本批不修不掩盖）

1. **adapter 同日重跑语义**（§22.9 #1）：研究候选更新 vs 新指令生成事件未裁决；不删完成标记、不强制全量重生成。
2. **EXT dry=True 静默语义**（§22.9 #4 残余）：规范路径 dry 仍写观测/触发提醒，待用户裁决。
3. **watchlist fail-close**（P2 后续）：异常即停新仓另行裁决；本批仅 §22 已落 warning+栈。
4. **资金分布激活**：BLOCKED §10.4 单变量 A/B（n≥120/≥20 独立 ET 日/预注册 MDE）；`DA_CAPITAL_CODE_FIX` 保持未设。
5. **EXT_BLACKLIST_GUARD 激活**：BLOCKED 用户裁 QCOM 禁令适用性（§17.4 差分 4 笔全盈利净+$75.66，依据只能是禁止语义）。
6. **半日 13:00–16:00 ET 起点扩窗**＋`_pretrade_gate` 报价 freshness：各自独立裁决/独立批。
7. **§15.7 输入修复队列**：短 K UNKNOWN（下一批单变量，默认 OFF 影子就绪，不激活）/健康空财报三态/来源状态/挑战者事实一致性/news UTC→ET。
8. **P3 score_band×gate 耦合**：record-only 留痕 `docs/analysis/ANALYSIS-20261127-score-band-gate-coupling.md`（MEDIUM 结构性封顶良；11 月 n=619 分布）；任何映射/阈值调整属正式判定变量须 A/B。
9. **HK-US 联动**：现有链接面=HKEX 新闻适配器 7 ADR 映射（`app/catalyst/sources/hkex_source.py`）；行情联动探针与预注册见 DEB-20261127-rfc218-s23（本批只读探针，不接 sizing/gates）。
10. W6 既有 HEAD 存量 drift（§23.5 清单）另案治理。

### 23.9 回滚

- 半日市守卫：`.env.us` 改 `EXT_AFTERHOURS_HALF_DAY_GUARD=0`（或删行），cron 下一 tick 即回旧静态窗，无需重启；影子 `EXT_AFTERHOURS_HALF_DAY_SHADOW=0` 停写盘。
- 镜像隔离/bridge 硬化/观测原子性：revert 本批 commit＋`systemctl restart ftseek-us`（app/ 部分）；EXT 观测部分经 cron fresh-import 随 revert 即回退。无新开关（三者今日零流量/只拒不稳定/只防并发，回滚无运行时差异风险）。
- 与 §16/§17/§19/§20/§21/§22 各开关互不牵连。

## 24. 实施记录 IX（2026-11-27 · 短K样本不足显式 N/A · `DA_SHORTK_NA` 独立开关默认 OFF · 正式输入变量未激活）

> 本节追加，不改 §§0–23 历史。承接 §23.8 #7 输入修复队列（短K UNKNOWN＝下一批单变量）。
> 授权：用户裁「请按照建议执行落地优化」＋审计输入层队列；本批**影子就绪、不激活**——
> 这是正式输入呈现变量（模型可见内容变化），激活 BLOCKED §10.4 单变量 A/B，不得当纯遥测直接上线。

### 24.1 病灶（隔离合成检查实锤）

- skill 仓 `llm_synth._format_data_for_prompt` K线技术面块：窗口不足时 MA5/10/20/60（需 5/10/20/60 根）、
  chg5（需 6）、chg20（需 21）、量比（需 20）全部**零填充** → prompt 渲染
  `MA60: 0.00 / +0.0% / 0.00x` 伪值。LLM 被要求判趋势/支撑阻力，却可能把「不可算」当「真实为 0」
  （短K「震荡」误读样本已列 §0/§15.7）。
- 本批探针：n=3 时 OFF 渲染 MA5..MA60 全 0.00、5/20 日涨跌全 +0.0%、量比 0.00x（`/tmp/rfc218_s24_off_sweep.py` C 段对照）。

### 24.2 修复合同（只动呈现层，计算一行不动）

- `config.py` 新增独立开关 `DA_SHORTK_NA`（默认 0；刻意不复用 `DA_CAPITAL_CODE_FIX`/`DA_INPUT_FULL`，互不捆绑）。
- **OFF＝逐字节旧渲染**（else 分支原 f-string 逐字保留）；ma*/avg_vol_20/vol_ratio/chg* 计算行逐字未动。
- ON：
  1. 样本不足指标 → `N/A(仅N根,需M)`（逐指标窗口：MA5/10/20/60=5/10/20/60、chg5=6、chg20=21、量比=20）；
  2. 标题实际根数 `## 技术面 ({n}日K线)`（OFF 保留「60日」旧标题）；
  3. 覆盖行 `K线覆盖: {close>0根数}/{n}根close>0 | 末根: {time}`（缺数据不编数据）；
  4. 警示「N/A=样本不足不可算, 不得按0解读; 趋势/均线结论只能基于可算指标, 不得以N/A指标推断震荡或方向」；
  5. 量比 ≥20 根但 20 日均量=0 → `N/A(20日均量为0)`（与样本不足文案区分）；
  6. K线明细行（近N日K线）与非技术面段落逐字节不动；`DA_INPUT_FULL` 截断正交（ON 标题=实际根数，明细仍按截断）。
- 空 K线（n=0）两态均无技术面段（既有 `if klines:` 门不动）。

### 24.3 验证矩阵（全部可复跑，`PYTHONDONTWRITEBYTECODE=1`）

| 项 | 结果 | 凭据 |
|---|---|---|
| 新契约 `tests/test_da_shortk_na.py` | **30 passed** | `/tmp/rfc218_s24_newtests.log` |
| OFF 逐字节扫描：合成 n 网格 0/1/3/5/6/10/20/21/60/61＋零量 n=25/61＋零close n=61 × DA_INPUT_FULL T/F（26 组）＋35 份 11-25 真实冻结 checkpoint＋ON 抽查 18 项 | **checks=79 fails=0 SWEEP_ALL_OK** | `/tmp/rfc218_s24_off_sweep.log`（探针 `/tmp/rfc218_s24_off_sweep.py`） |
| 变异（`_sk` 样本不足仍返回数值 → ON 契约红）→恢复 sha 核验 | **3 红→恢复 30 绿**，sha `4b535772ed1b` 一致 | `/tmp/rfc218_s24_mutation_{red,green}.log` |
| py_compile venv＋cron `venv/bin/python3.11` 双解释器 | exit0×2 | `/tmp/rfc218_s24_transform.log` |
| DA 相邻（capital/output/fundamental/prompt_injection） | **1F/143P**；唯一失败＝既有 PROMPT_HASH 漂移（backup 预载同断言同值 `80176956bfbb≠c7444d67ce58`，非本批引入；§15 的 2 个 prompt_injection 失败现已绿） | `/tmp/rfc218_s24_adjacent.log`＋`_output_baseline.log` |

- 本测试只验呈现层正确，不证收益；未跑全量 pytest/CI。DB 未触；零订单/付费 LLM/激活。
- 既有 1 相邻失败（output_contract PROMPT_HASH 漂移）为基线存量 record-only 不修（repo 测试 vs 仓外 skill 模板指纹漂移，另案）；不声称全绿。

### 24.4 部署

- 仓外 DA 两文件（`config.py`/`llm_synth.py`），carrier/cron fresh-import 即生效；**无 app/ 改动、无需重启**。
- 备份 `*.bak-20261127-rfc218-shortk`＋SHA256：config pre=`41cf45334304` post=`2ddbb5947c51`；llm_synth pre=`5136c1e43685` post=`4b535772ed1b`（备份=pre 逐字核验）。`.env.us` **未加** `DA_SHORTK_NA` 行（未设＝默认 OFF，fresh-import 冒烟 `DA_SHORTK_NA=False`，生产逐字节不变）。
- 仓内产物：`tests/test_da_shortk_na.py`＋本节＋handoff 条目，提交推送（sha 见 handoff 同日条目）。
- 变换在 DA 主批（21:00 CST 起跑）结束后执行，脚本自检 pgrep 拒跑（防中断在跑 DA）。

### 24.5 收益边界（与 §9/§15/§22.8/§23.7 一致）

- **无任何收益证据**。呈现层正确化≠盈利；ON 态模型方向/分值行为变化未验证。
- 激活须 §10.4 单变量 A/B（n≥120/≥20 独立 ET 日/预注册 MDE），且不与资金分布/其他输入变量捆绑；
  「N/A 进 prompt」不得直接当「模型更保守/更准」的证据。

### 24.6 回滚

- `.env.us` 加 `DA_SHORTK_NA=0`（或删行）→ 逐字节旧渲染；或恢复备份（sha 核验）。
- 与 `DA_CAPITAL_CODE_FIX`/`DA_INPUT_FULL`/EXT 各开关互不牵连。

### 24.7 未闭合排队

- §23.8 队列不变；健康空财报三态/来源状态/挑战者事实一致性/news UTC→ET 各自独立批单变量。
- 短K ON 态的「震荡误读率」前后对比属 A/B 预注册指标，本批不测不声称。

## 25. 实施记录 X（2026-11-27 · HK→US 时间差行业/趋势参考输入 · 影子采集先行 record-only · DA 注入未实施未激活）

> 用户裁（2026-11-27）：「港股的波动趋势，不仅仅只是看个股，看行业和动态趋势，可以作为信息参考输入，
> 打港股和美股的时间差」。承接 §23 DEB §4/§7#8「HK-US 联动采集器是否推进」待裁，**范围从单股 ADR 扩展到
> 指数＋行业篮子＋动态趋势**。本批＝影子先行第一步（DEB-20261127-hk-us-sector-trend-context）：只读 OpenD
> 快照落日内序列，**零判定消费**；DA 参考输入注入＝后续独立批（`DA_HK_CONTEXT` 默认 OFF＋§10.4 A/B），本批不实施。

### 25.1 范围与采集面

- 三层采集：①指数 HSI/HSTECH/HSCEI（HK.800000/800700/800100，含 high/low chg）；②8 行业等权篮子（32 成分股，
  探针全 NORMAL）→ 等权 chg/中位数/离散度/breadth（涨/平/跌）/成交额；③动态趋势＝日内 chg 序列（~24 tick/日）＋
  翻红/翻绿事件（相邻 tick 符号穿越 0）＋全日 rollup（从 tick_rows 幂等重算）。另落 7 ADR 港股腿。
- **单日描述性实证**（2026-11-27 收盘 forced verify tick）：恒生科技 +6.45%、半导体篮子 **+14.86%（3/3 涨）**、
  科技互联网 +5.47%（8/8 涨），而银行 -1.30%（1涨3跌）、能源 -1.84%（1涨2跌）——科技/半导体强、银行/能源弱的
  **行业轮动**，单看个股（腾讯 +3.23%）无法呈现。此为单日描述性事实，**不构成传导强度证据**（§25.5）。
- OTC ADR（TCEHY/MPNGY/XIAOMY）行情不可用已探针实证，只标 `OTC_UNAVAILABLE`；交易所上市 ADR 标
  `EXCHANGE_LISTED_DEFERRED`（美股侧采集后续扩展）。ADR 映射与 `hkex_source.py::HK_STOCK_MAPPING` 同源（parity 契约钉死）。

### 25.2 采集合同

- 载体 `scripts/hk_market_context_collector.py::record_tick`；产物 `data/shadow/hk_us_linkage/<HK交易日>.json`（gitignored）。
- 只读 `get_market_snapshot`（无订阅无K线，§23 §5 探针 ret_code=0）；`UNITS` 量纲声明（军规）；未知不归零（chg_pct 非法→None）。
- 时段闸（CST=HKT 早/午盘＋两收盘锚）＋假日/停市闸（指数 update_time 日期≠今日∨sec_status≠NORMAL→stale 跳过）。
- flock 锁内 RMW＋mkstemp/os.replace 原子写（复用 §23 范式）；HH:MM 去重＋MAX_TICK_ROWS=40；fail-safe 恒 exit0。
- **计数轴＝HK 交易日数**（每日 1 文档 ticks=int≥1 计 1，非 ticks 累加）；`--force` 落 `_verify/`（glob 不递归不污染计数）；`--dry-run` 零写盘。
- 注册表 `hk_us_linkage_context_shadow`（OBSERVING/min_n=60/USER_ADJUDICATION/intent=hk_us_linkage_context/config_key=None/stale_expected=False）。

### 25.3 验证

| 项 | 结果 | 凭据 |
|---|---|---|
| 采集器契约 | **48 passed** | `/tmp/rfc218_s25_newtests.log` |
| 注册表相邻（intent 唯一/rfc207/目录计数/判据可见/meta 推送去重/看板守卫/EXT 风险注册） | **58 passed** | `/tmp/rfc218_s25_registry_adj.log` |
| 变异（chg_pct→恒 0.0）→恢复 sha 核验 | **2 红→恢复 48 绿**，sha256 一致 | `/tmp/rfc218_s25_mutation_{red,green}.log`＋`_pre.sha256` |
| 台账 crontab 守卫（loops ledgered/names carrier/no ghost） | **3 passed** | `tests/test_control_loop_ledger.py`（整组 1F＝既有幽灵 job_id `68cc76b16ebd` 基线存量，HEAD 同红，非本批引入） |
| 挂载核对 `check_mounting()` | 本条目 **0 新告警**（全局 5 条均既有存量） | 进程内探针 |
| py_compile venv＋cron python3.11 双解释器＋check_undefined_names | exit0×2＋rc0 | 进程内 |
| cron 原命令手动跑（Fri 22:11 CST 窗外） | exit0 `skipped_session`（零写盘） | `logs/hk_market_context.log` |
| 真实收盘 forced verify tick | `_verify/2026-11-27.json` 落盘（HSI +2.11%/HSTECH +6.45%/semis +14.86%） | `data/shadow/hk_us_linkage/_verify/` |

- 未跑全量 pytest/CI，不声称全绿；既有台账幽灵 job_id 基线存量 record-only 不修（另案）。

### 25.4 部署

- 代码提交 `d5e47ba9c`（采集器＋48 契约＋注册表＋台账 `crontab-hk-market-context`）；DEB＋本节＋handoff 落档随后提交。
- cron 两行（系统 crontab，CST=HKT）：`7,22,37,52 9-11,13-15 * * 1-5`＋`2 12,16 * * 1-5`，timeout 120，日志 `logs/hk_market_context.log`；
  装机前备份 `/opt/ftseek/backups/crontab-20261127-221103-pre-hk-context.txt`（447 行，diff 仅新增）；首个真实 tick＝周一 2026-11-30 09:37 CST。
- 注册表属 app/ ⇒ 随批重启：`systemctl restart ftseek-us` → 新 PID **212536**（22:14:00 CST）；启动冒烟 **7/7 exit0** `/tmp/rfc218_s25_startup_smoke.log`。
- 重启前快照 `/tmp/rfc218_s25_pre_restart.json` 与重启后 `_post_restart.json` **除 PID 外逐项 IDENTICAL**
  （SIM SHORT BA 23/BABA 47/GS 5、LIVE 0、outbox 22 全 BOOKED、PENDING 4403、今日 8 指令、被禁 PENDING 28 存量、exit_ledger 在途 0）；重启后日志 0 Traceback/CRITICAL。
- DB 全程 `mode=ro`+`query_only`；零订单/付费 LLM/实验激活；`.env.us` 未改（采集器无开关，cron fresh-import）。

### 25.5 收益边界（与 §9/§15/§22.8/§23.7/§24.5 一致）

- **无任何收益证据**，纯观测面新增（record-only）。单日行业轮动只是描述性事实，**不构成 HK→US 传导强度/时滞/可交易性证据**。
- 传导命中率/经济增量属预注册研究（n≥60 HK 交易日＋|chg|≥2% 子集＋无条件基线对比＋MDE）＋§10.4 A/B 的待验指标，本批不测不声称。
- DA 注入激活须用户单独裁决，不与资金分布/其他输入变量捆绑；「采集到行业轮动」不得直接当「DA 更准/更赚」的证据。

### 25.6 回滚

- 删 cron 两行（产物留档；无 config 键，勿照 config_key 判停）；注册表条目/采集器可 revert `d5e47ba9c`＋重启。
- 与 `DA_CAPITAL_CODE_FIX`/`DA_SHORTK_NA`/`DA_INPUT_FULL`/EXT 各开关互不牵连。

### 25.7 未闭合排队

- DA 参考输入注入（`DA_HK_CONTEXT`）BLOCKED §25.5 研究＋§10.4 A/B＋用户裁；美股侧 ADR 盘前/开盘采集、全市场 113 板块 breadth（plate API）、篮子成分动态维护各自后续扩展。
- §23.8/§24.7 队列不变（adapter 同日语义/EXT dry 静默/watchlist fail-close/健康空财报/来源状态/挑战者一致性逐批单变量）。
