---
title: entry_confirm 量能确信度影子：缩量/放量作为确认条件变量 + LLM 确信度合成（方案预注册，未实施）
status: draft
module: playbook
related_code: app/playbook/entry_confirm_gate.py, app/playbook/pipeline_parts/intraday_indicators.py, app/llm/pre_entry_gate.py
related_okr: OKR-2026-11
tags: [entry_confirm, volume, llm, shadow, preregistration]
updated: 2026-11-28
---

# DEB-20261128 · entry_confirm 量能确信度影子：缩量/放量观测 + LLM 确信度合成（record-only 预注册，未实施）

> 状态：**设计中（未实施）** — 本批仅落档方案预注册。**零代码、零配置变更、零注册表变更、零重启。**
> Step 1 采集实现（code+tests+registry）须用户另行裁决后独立批落地。
> 授权链：用户问「entry_confirm 有确认逻辑，能否实现缩量/放量指标观察？从交易第一性原理，能否用量能信息
> 更高概率预测交易方向？充分利用 LLM 的分析和判断能力」→ 分析+方案呈报 → 用户裁（2026-11-28）：
> 「同意，请先按照建议落档DEB」。
> 关联：RFC-158（entry_confirm 重开 v2 现行预注册）、RFC-141（原始预注册，判据已被 10-31 裁决消耗）、
> RFC-170（确认侧追价影子 record-only 先例）、DEB-20261127-hk-us-sector-trend-context（record-only 影子范式）、
> 影子注册表 `entry_confirm_forward`（OBSERVING）。

> **⚠️ 复核修正横幅（2026-11-28）**：§13（Kimi 独立复核）+ §14（本 agent 核验整合）已追加。
> §6.1 的 `vwap_slope` 单位（实为无量纲 5min ratio，非 pct/min）、capital_flow「复用即有当日守卫」
> 隐含假设、以及「两臂都快照保证可比」表述，**以 §13/§14 更正为准**；原文 §1–§12 保留留痕不改写。
> Step 1 实施仍须用户另裁；本会话仅落地 DEB 整合 + P0-3 宿主结算器计量修复（§14.6）。

## 1. 本批裁决与依据

| 项 | 裁决 | 依据 | 变量类型 |
|---|---|---|---|
| 落档本 DEB（方案预注册） | **落地**（本批，纯 `.md`） | 用户裁「同意，请先按照建议落档DEB」 | 治理文档（零行为） |
| 确认时刻量能特征快照（Step 1） | **预注册，未实施**，须另批裁决 | entry_confirm 纯价格缺口（§5）；复用已有 primitives（§4） | 新观测面（record-only） |
| LLM 量能确信度影子（默认 OFF） | **预注册，未实施**，须另批裁决 | LLM=确信度合成器/veto，非方向 oracle（§2） | 新观测面（默认 OFF，零成本） |
| 量能条件化接入 live 判定（Step 3） | **不做，BLOCKED** | 须 Step 2 影子证据达标 + 用户单独裁决 + 一次一变量 | 待裁（后续独立批） |
| `eval_confirm` 价格判据 / 门阈值 / holdout 分配 | **任何阶段逐字不动**（本 DEB 范围内） | 改判据=重新裁决（entry_confirm_gate.py:362 注释明文；RFC-158 §10 现行） | 冻结 |
| sizing / gates / 方向判定 live 接入 | **任何阶段不接**（本 DEB 范围内） | 硬边界（§9）；Step 3 若涉及须新 DEB + 单变量 A/B + 用户签字 | 硬边界 |

## 2. 第一性原理：量能是确信度变量，不是方向预测器

**核心论点（对用户问题的直接回答）**：

1. **量能幅度恒为正、无方向符号。** 每一笔成交同时有买有卖；成交量度量的是参与度/分歧强度，
   「放量」本身不告诉你谁更急。方向信息在**价格行为**（谁主动 lift offer / hit bid）与
   **signed order flow**（买卖失衡、大单净流方向）。量能的角色是调制「价格信号为真的概率」——
   它是**确信度/确认条件变量**，不是方向预测器。因此「用量能预测方向」的正确形态不是
   「放量→做多」，而是「价格已给出方向信号时，量能状态决定该信号该不该被确信/降级/否决」。
2. **三 doctrine（全部位置依赖）**：
   - **放量确认突破**：关键位突破 + 放量 → 延续概率升（Livermore；本仓 `PivotalDetector` 明文
     「突破必须伴随着成交量的放大，否则可能是假突破」）。
   - **量价背离**：价创新高/新低但量缩，或放量但价滞（滞涨/止跌）→ 耗竭/反转警惕。
   - **缩量压缩（squeeze）**：波动收窄 + 量缩 → 蓄势，**方向未定**，突破待确认。
3. **位置依赖反例（为什么纯阈值规则不够）**：同样「放量」，在突破位=确认、在高位滞涨=出货嫌疑、
   在深跌末端=高潮见底嫌疑——**含义相反**。量能特征必须与价格结构（锚位/突破/带内位置）+ regime
   联合解读。这正是 LLM 语境推理的用武之地，也是 `pre_entry_gate.py:3` 哲学
   「LLM 应做量化做不到的事——拦截『看历史就该拒』的入场信号」的延伸。
4. **本仓实锤：naive 量能特征作独立门已证伪（先验必须保守）**：
   - 日级 `volume_ratio`「对盘中突破零预测力」（`intraday_indicators.py:484` 注释，2026-07-20
     因此重建盘中量能比）。
   - 资金流背离独立门回放四关门全不过（p=0.85 placebo），注册表 `p1_1_capital_flow` **KILLED**，
     纯 record-only（`shadow_registry.py:1087`）。
   - 结论：把「放量/缩量」或「大单净流」当**独立方向门**，本仓已有反证。本 DEB **不重提独立门**，
     只把量能作为**确认条件变量**与 **LLM 语境输入**，且必须影子先行、对照验证。
5. **LLM 正确角色 = 确信度合成器/veto，非方向 oracle。** 量化算严格特征 → 结构化喂 LLM →
   影子对照验证其 veto/降确信建议的前向经济价值；**不信 LLM 自报方向**，方向判定权属价格判据（冻结）。

## 3. 立案前强制检查（P0.5，docs/debates/template.md）

### 3.1 同轴既有影子枚举（grep REGISTRY 实载，非凭记忆）

| id（行号） | 状态 | 一句话 | 本 DEB 增量/边界 |
|---|---|---|---|
| `entry_confirm_forward`（:2446） | OBSERVING | RFC-158 入场延迟确认门 SIM 实弹前瞻（120s+20% holdout），min_n=60，计数轴=current_cohort.sim.n | **宿主**。增量=forward 行追加量能快照 + 默认 OFF LLM 确信度影子；不动其价格判据/holdout/样本门 |
| `entry_delay_confirm`（:2412） | OBSERVING | 历史 1min bar 回放延迟确认反事实（与宿主双向 cross_ref） | 判据互不替代，不碰 |
| `p1_3_vol_p50`（:1032） | KILLED（结案） | 量能门分母改同分钟 p50；p50 机制 reentry/UE 生产生效，DA 走线性对比组 | 量能门分母轴已结案；不动门阈值 0.8 / grace 15min / 分流 |
| `p1_1_vwap_slope`（:1006） | GRADUATED | VWAP 斜率证伪门，全源生产拦截 | 已毕业不重复攒样；`vwap_slope` 只作快照特征引用 |
| `p1_1_capital_flow`（:1087） | KILLED | 资金流背离独立门 p=0.85 placebo | **不以其为独立门**；仅作 LLM 语境输入/分层变量 |
| `futu_channel_capital_flow`（:1136） | OBSERVING | 真资金流增量 IC（rank_ic_marginal=0.1188, t=1.49, WEAK_EVIDENCE） | 复用其产物 `data/capital_flow_daily.jsonl` 作特征，不另建采集 |
| `entry_band_widen`（:1447） | OBSERVING | RFC-065 入场带 vol_widen | **名似轴不同**：vol_widen 的 vol=ATR 波动率（`entry_band.py:28`），非成交量；不碰 |
| `vwap_gate_ab`（:1506） | OBSERVING | VWAP wrong_side 拦截 50/50 A/B | 不同轴，不碰 |
| `eth_relative_volume_gate`（:3374） | KILLED（退役 2026-10-23） | 盘后事件相对量闸 | 已退役不重开 |
| `ue_confirm_gate`（:3889） | OBSERVING | UE 开盘确认门（llm_universe_scan） | 不同管线，不碰 |
| `short_chase_ban_shadow` / `gap_chase_gate` / `chase_high_ban` | — | 追价轴 | RFC-170 chase_shadow 已在 entry_confirm 内 record-only（:120/:252），不碰 |

**增量唯一性**：本 DEB 新轴 = 「entry_confirm 确认时刻的量能状态 × LLM 确信度建议」对前向结局的
条件化预测力。REGISTRY 无同轴项；拟 intent=`entry_confirm_volume_conviction`（唯一），
与 `entry_confirm_forward` **双向 cross_ref**（`check_intent_uniqueness` :5810 ③：目标现役须回指，
实现批须同时给宿主补互引，否则周检告警）。

### 3.2 既有裁决/判据字典撞车普查

- **RFC-158 §10 现行判据逐字不动**：减大亏优先（用户 2026-11-05 澄清），样本门 treatment resolved
  n≥60 ∧ holdout resolved n≥20 ∧ 同期配对≥10 ET 日 ∧ 两臂结局覆盖率≥95%；PASS=每分配指令
  SL_INITIAL 亏损负担下降且日聚类 80% CI 下界>0，同时总净额/分配指令不恶化。本 DEB Step 2 是
  **附加只读分层研究**，不替代/不修改其 PASS/FAIL，不占 C1。
- **RFC-141 判据已被 10-31 INSUFFICIENT 裁决消耗**，不沿用。
- `eval_confirm`（:362）注释明文「改这里=改判据=重新裁决，须先改 RFC-141 预注册文本」——任何阶段不改。
- 不重开已闭合类：`p1_1_capital_flow`（独立资金流门已证伪）、`eth_relative_volume_gate`（已退役）。
- **一次一变量 / 不污染 RFC-158 cohort**：Step 1 若实施只加 record-only 字段，不改 treatment/holdout
  分配与判定；RFC-158 正在攒样（当前 n 以 `data/shadow/entry_confirm_forward_latest.json` 为准），
  本 DEB 不与其捆绑激活。
- 判据字典 B23 功效教训（`docs/ADJUDICATED-CRITERIA.md`：净 bps sd=121 ⇒ 检出 10bps 需 2,302 笔）
  直接约束本 DEB Step 2 的 MDE/样本门设计（§7.4），不得以短窗口分层正数当证据。

## 4. 系统已有 primitives 映射（复用，不重建）

| 能力 | 载体（已核行号） | 现状 | 本 DEB 用法 |
|---|---|---|---|
| 盘中量能比 | `intraday_indicators.py:460 _calc_intraday_vol_ratio`：分子=Futu LV3 快照当日累计量（主）/K_1M 聚合（兜底）；分母=同分钟累积量 p50 曲线（`:375`，~45 票，`data/replay_bars_1m`）/线性回落；半日感知 `get_us_close_minutes_et`；>1.5=放量；None=fail-open | 生产 | 确认时刻复用计算，快照 `vol_ratio`+分母路径+grace 旗标 |
| 盘中量能门 | `trigger_flow.py:467-469`；config `intraday_vol_gate_enabled: true`（:1271）/`chase_vol_ratio_threshold: 0.8`（:1272，代码默认 1.5）/`intraday_vol_gate_grace_min: 15`（:1273）；DA 线性 vs reentry/UE p50 分流（:1613-1614） | 生产 | **不动**；快照记录门读数供分层 |
| 资金流背离 | `intraday_indicators.py:278 _capital_flow_divergence`：super+big 净流入 vs 方向，读 `data/capital_flow_daily.jsonl`，日缓存 | record-only（独立门已证伪） | 快照背离旗标+净流符号（LLM 语境，不作门） |
| VWAP 斜率 | `intraday_indicators.py:219 _calc_vwap_slope` | 生产毕业门 | 快照引用（量加权方向证据） |
| Livermore 关键点 | `app/factors/pivotal_detector.py`（config `strategy_enhancements.pivotal_points` :3000，enabled=true，volume_ratio_trigger=1.5）；消费方 `trade_quality_gate.py:60-67/115-117`（权重 0.30） | 生产（主管线评分） | 设计参照：放量确认突破 doctrine 已实现；不重建 |
| 盘中量价评分 | `app/core/intraday_scorer.py:7`「量价结合—LV3 盘口参与方向判断」，tech_volume 因子 :153 | 主管线（冻结新仓） | 设计参照 |
| 入场带 vol_widen | `app/playbook/entry_band.py:28 vol_widen_band_pct`（**ATR 波动率，非成交量**）；config :1444 false | 开关 OFF | 不碰（轴不同） |
| LLM 上下文 vol_ratio | `app/llm/thesis_inferer.py:81`（ctx_brief 已喂 vol_ratio） | 已喂 | 先例：LLM 已消费量能比 |
| LLM 事前门安全范式 | `app/llm/pre_entry_gate.py:3` 哲学；默认 disabled / LLM 失败 fail-open go=True / 5s 硬超时 / 5min 同 symbol+direction 缓存 / 历史<5 笔不调；`_build_prompt` :135 | disabled | LLM 影子的安全范式来源 |
| 确认侧 record-only 先例 | `entry_confirm_gate.py:120/:252` RFC-170 `chase_shadow_flag`（would_abandon 只标记不改判，:979-980） | record-only | Step 1 LLM 影子同范式 |
| forward 落盘基建 | `_FORWARD_PATH=data/shadow/entry_confirm_forward.jsonl`（:145）/`SCHEMA_V2="entry_confirm_forward_v2"`（:149）/`_write_forward_row`（:403，`_FORWARD_LOCK` :148 防半行 JSON，fail-open warn） | 生产 | 增字段追加载体 |
| 结算/对照基建 | `scripts/settle_entry_confirm_forward.py`（cron `37 6 * * 2-6`）+ holdout 20% 对照臂（config :1375）+ RFC-158 §10.2 resolved-assignment 口径 | 生产 | Step 2 复用联接与计数轴 |

## 5. 真正缺口（已核）

`entry_confirm_gate.py`（1205 行）的确认判据 `eval_confirm(direction, anchor, px)`（:362）
**纯价格** = `(still_triggers, opp_cost_pct)`：LONG 需 px≥锚、SHORT 需 px≤锚，opp_cost 方向归一。
120s 延迟复核（`entry_confirm_delay_sec: 120`，config :1385）只看价格仍站锚同侧 + 追价成本，
**完全不消费量能**。两个价格等价场景——「放量站稳锚」vs「缩量 drift 站稳锚」——在门内不可区分，
而第一性原理上二者「信号为真」的概率不同（§2）。

这就是可检验的增量假设：**量能状态条件化能否改善确认门的前向结局分布（尤其左尾/大亏负担）**。
注意：这是**假设不是结论**；本仓 naive 量能门已证伪（§2.4），故必须影子先行、对照验证，
且 LLM 只做确信度合成不做方向 oracle。

## 6. Step 1 设计：确认时刻量能快照 + 默认 OFF 的 LLM 确信度影子（record-only，未实施）

### 6.1 采集合同（schema / 单位 / 闸 / 原子性 / 计数轴）

- **触发点**：entry_confirm 确认评估时刻（`gate_at_execute` :426 臂定 + 10s 快循环 sweep 复核）。
  **treatment 与 holdout 两臂都快照**（holdout 无判定但锚侧报价照常留痕的先例 :534，量能快照同理），
  保证条件化研究两臂可比。
- **载体**：`entry_confirm_forward.jsonl` 现有行**追加嵌套对象** `vol_snapshot`（自描述 `v:1`）。
  顶层 `schema` 键保持 `entry_confirm_forward_v2` 不变 ⇒ 旧结算器读旧键逐字不变；
  契约测试钉死「旧键逐字不变 + 新字段量纲/枚举」。旧行无 `vol_snapshot` ⇒ 读取端按 None 处理。
- **字段草案（单位声明，军规 2026-09-02）**：

  | 字段 | 类型 | 量纲/枚举 | 说明 |
  |---|---|---|---|
  | `v` | int | 计数 | 快照 schema 版本=1 |
  | `vol_ratio` | float\|None | 无量纲比值 | 今日累计量/同时段期望量，1.0=持平（复用 :460） |
  | `vol_denom` | enum\|None | `p50`\|`linear` | 分母路径（~45 票外回落线性，须留痕） |
  | `vol_class` | enum\|None | `surge`\|`dry_up`\|`normal` | **研究分类阈值（预注册，非门）**：≥1.5 放量 / ≤0.6 缩量 / 其间正常；1.5 与 pivotal/docstring 既有口径同源 |
  | `vol_gate_grace` | bool | 布尔 | 是否处开盘豁免窗（<15min，量能不可靠旗标） |
  | `capital_flow_divergence` | bool\|None | 布尔 | 大单净流逆方向（复用 :278） |
  | `capital_flow_net_sign` | enum\|None | +1\|-1\|0 | super+big 净流符号 |
  | `vwap_slope` | float\|None | 按实现声明（pct/min） | 量加权价斜率（复用 :219） |
  | `session_min` | int | 计数（分钟） | 开盘后分钟数 |
  | `half_day` | bool | 布尔 | 半日市旗标 |
  | `ts` | str | ISO8601 ET | 快照时刻 |

  可选扩展（实现批可裁，留痕）：`vol_trend_slope`（近 N 分钟量能比斜率，ratio/min）——需分钟级
  量能序列，v1 最小集不含，避免一次引入过多采集面。
- **未知不归零**：任何特征数据不足 → None（宁缺毋滥，不伪装样本）；None 行不入对应分层分母。
  capital_flow 文件陈旧（非当日）→ None。
- **原子性**：复用 `_write_forward_row` + `_FORWARD_LOCK`（模块锁防半行 JSON），写盘失败 fail-open
  warn，**不阻断交易**。
- **计数轴**：研究计数 = **resolved assignment**（RFC-158 §10.2 同口径：ABANDONED 净=0；
  CONFIRMED/holdout 按 decision_id 联接已平 PLAYBOOK_SIM；unresolved 不补 0）× **独立 ET 日**；
  **不是 jsonl 行数**（宿主 2026-10-22 n 轴对齐教训：机制台账行≈裁决轴 5 倍虚高 ⇒ 假 READY）。
- **配额/成本**：get_quote 推送缓存 <100ms；p50 曲线日缓存；capital_flow 日缓存；每指令每确认一次，
  无新增订阅、无新增 cron（快照随既有确认链路落盘）。
- **开关（拟）**：`playbook.execution_parameters.entry_confirm_volume_snapshot_enabled`，**默认 false**。
  record-only 零行为变更，但新增落库面 ⇒ 激活（翻 true 开始攒样）仍须用户裁。

### 6.2 LLM 量能确信度影子（默认 OFF，ON 时仍 record-only）

- **开关（拟）**：`playbook.execution_parameters.entry_confirm_volume_llm_shadow_enabled`，**默认 false**；
  OFF 时零 LLM 调用、零成本、零行为。与快照开关**分离**（一次一变量：先攒特征样本，再裁是否开 LLM 影子）。
- **ON 时行为**：确认评估后，把结构化上下文（direction/anchor/px/opp_cost_pct/still_triggers +
  `vol_snapshot` + 价格结构位置 + regime 摘要）喂 LLM，问「该确认信号的确信度是否应被量能证据
  降级/否决」，输出结构化 `{conviction: high|normal|low, would_veto: bool, reason_code: enum, brief: str}`，
  落 forward 行嵌套对象 `llm_vol_shadow`。**只记录建议，不拦截、不改 still/abandon、不改 sizing**
  （RFC-170 chase_shadow 同范式 :979-980）。
- **安全范式（pre_entry_gate 同源）**：硬超时（拟 5s，ThreadPoolExecutor future.result）+
  fail-open（失败记 None 不阻断）+ 同 symbol+direction+session 桶缓存（防重复调用）+
  每日调用上限（拟，实现批定数）；走既有 `app/llm/` 基础设施，**不新建 webhook**；
  无新增推送（将来若需报告走 `app/push/` hub 默认静默）。
- **prompt 纪律**：不问「涨还是跌」，只问「确信度是否降级」；方向判定权属价格判据（冻结）。
- **消费方登记（军规 2026-09-06）**：`llm_vol_shadow` 各字段登记消费方 = Step 2 研究评估器
  （拟 `scripts/settle_entry_confirm_volume_conviction.py`，实现批建）；**无 live 消费方**（record-only
  明示）；无消费方的字段一律删（write-only 是 prompt 膨胀主因）。

### 6.3 军规三件套（随实现批落）

1. **单位/量纲声明 + 契约测试断言**（2026-09-02）：§6.1 表逐字段断言；新旧双格式（有/无 vol_snapshot）
   逐格式契约例。
2. **基数自检**（2026-07-27）：`vol_class`/`vol_denom`/`conviction`/`reason_code` 等枚举 distinct 计数体检，
   n≥10 且 distinct<2 即 WARNING（防常量退化）；接 `scripts/shadow_field_cardinality.py` 与 W2 棘轮基线
   （新增字段须按流程登记，不自动改写基线）。
3. **消费方登记**（2026-09-06）：见 §6.2；快照字段消费方 = Step 2 评估器 + 分层研究。

## 7. Step 2 预注册：影子分层研究 / A/B（读数不得事后改）

### 7.1 研究问题

- Q1：确认时刻量能状态（`vol_class` × `capital_flow_divergence` × `vol_gate_grace` × `half_day`）
  能否条件化前向结局（净 bps/笔、SL_INITIAL 大亏负担）？
- Q2：LLM `would_veto=True` 子集的实际结局是否左尾显著更差（veto 正确）或更好（veto 误杀）？

### 7.2 设计（只读分层 + 反事实对照，复用 RFC-158 结算基建）

- **基线** = 全体 resolved assignment 的前向净 bps/笔、SL_INITIAL 大亏负担（RFC-158 主轴同口径）。
- **分层** = §7.1 各轴；每层报 n、净 bps、大亏率、日聚类 CI；**跨月同号 + 剔最大单笔敏感性**
  （B23 教训：多重比较/最近窗口衰减/尾部主导）。
- **LLM 影子对照** = `would_veto=True` vs 其余（**同 vol_class 层内**配对），读「veto 掉的实际结局」
  vs「放行的实际结局」；holdout 臂提供无门对照。
- 评估器（拟）`scripts/settle_entry_confirm_volume_conviction.py`：只读，产物
  `data/shadow/entry_confirm_volume_conviction_latest.json`（注册表计数锚）。

### 7.3 样本门与 MDE（预注册草案；实现批功效估算后冻结于注册表，冻结后不得事后改）

- 样本门草案：总 resolved **n≥150** ∧ holdout resolved **n≥30** ∧ 独立 **ET 日≥20** ∧
  每 `vol_class` 主分层 resolved **n≥30**；两臂结局覆盖率≥95%（RFC-158 同口径）。
- MDE 草案：主读数 = 每分配指令 SL_INITIAL 大亏负担相对下降 **≥30%** 且日聚类 **80% CI 下界>0**
  （与 RFC-158 PASS 主轴同口径）；次读数 = 净 bps/笔差 ≥10bps **仅披露，不作短周期判据**。
- LLM veto 判据草案：`would_veto=True` 子集大亏率显著高于同层放行子集（日聚类/Fisher，n≥30 才判）。
- **功效披露（B23 教训直接适用）**：entry_confirm resolved assignment 攒样慢（RFC-158 treatment
  n≥60 以周计），分层后每层 n≥30 可能需数月；净 bps 方差大 ⇒ **短期只读描述性分层，不声称显著性**；
  实现批须按实测方差做功效估算再冻结 MDE/样本门，避免「净 bps 不可能作短周期判据」陷阱。

### 7.4 三态

- **PASS**（分层条件化有显著增量 ∧ LLM veto 子集左尾显著更差）→ 才谈 Step 3。
- **FAIL**（无增量或 veto 误杀）→ KILLED 留痕（判据原文不删）。
- 其余 **OBSERVE**。**不得以分层正数当证据**；不得事后挑格子/挪阈值。

## 8. Step 3 激活路径（BLOCKED，本 DEB 不实施）

1. 仅当 Step 2 证据达标 + **用户单独裁决** → 才考虑接 live。
2. **一次一变量**：先量能条件化或先 LLM veto，二选一，不捆绑；接入形态（确认门附加条件 /
   仅 veto / 其他）须新 DEB/RFC + §10.4 单变量 A/B + 用户签字。
3. sizing 接入（确信度→仓位折扣）**不在本 DEB 范围**，须另裁。
4. 与 RFC-158 实验互不污染：Step 1/2 期间 entry_confirm 价格判据 / holdout 分配 / 门阈值冻结。

## 9. 硬边界与收益边界

- **本批零代码零配置零注册表零重启**；纯 `.md`。
- Step 1/2 record-only：不改 `eval_confirm`、不改门阈值 0.8/grace、不改 holdout 20%、
  不接 sizing/gates/方向 live。
- **收益边界**：本 DEB **无任何收益证据**；「量能确信度增量」是待验假设；正确性/观测 ≠ 盈利。
  本仓反证（日级量比零预测力 / 资金流门 p=0.85）意味着先验偏保守，影子证据门槛应高。
- **风险登记**：LLM 成本/延迟（默认 OFF + 硬超时 + 缓存 + 上限）；特征 None 率（p50 覆盖 ~45 票外
  回落线性、capital_flow 新鲜度）；分层多重比较；半日/开盘 grace 量能不可靠窗；
  快照人群与判据人群须同集合（§11 落地后核查）。

## 10. 部署凭据（本批）

- 纯文档新增：`docs/debates/DEB-20261128-entry-confirm-volume-conviction-shadow.md`（本文件）。
- 提交 sha / 推送：见 handoff 同日条目（**凭据**/**验证**/**未验证** 三件）。
- 无测试（纯 `.md`，pre-commit 治理钩子照常）；服务不重启（PID226603 不变）；
  config / REGISTRY / cron 未动。

## 11. 待裁 / 后续（不在本批实施）

1. **Step 1 实现批**（code+tests+registry，须用户另行裁决）：
   - 注册表条目草案：`id=entry_confirm_volume_conviction`，`intent=entry_confirm_volume_conviction`，
     `status=OBSERVING`，`min_n=150`（resolved assignment 轴，草案），
     `graduation_criteria=`§7 预注册，`eval_script=scripts/settle_entry_confirm_volume_conviction.py`（拟），
     `data_path=data/shadow/entry_confirm_volume_conviction_latest.json`（拟），
     `cross_ref=("entry_confirm_forward",)` **且宿主侧补回指**（check_intent_uniqueness ③双向）；
     **USER_ADJUDICATION，无自动转正门**。
   - config 新键草案：`entry_confirm_volume_snapshot_enabled: false` /
     `entry_confirm_volume_llm_shadow_enabled: false`（两键分离，一次一变量）。
   - **落地后强制核查（template.md，实现批提交后必做）**：①快照人群 ↔ 判据人群对读
     （快照人群=entry_confirm resolved assignment 同集合，holdout 臂也采，超集/子集须登记分层）；
     ②同 inst_id 多行折叠取首/末 + 哨兵（合成台账实跑）；③生产生效核（重启时刻晚于提交时刻，
     `systemctl restart ftseek-us` + 启动冒烟 7 断言）；④若新增 cron 注释标 ET 等价；
     ⑤新哨兵字段同步进 `docs/CONTROL-LOOP-LEDGER.md` 失效模式。
2. **Step 2 评估器 + 功效估算**（实测方差 → MDE/样本门冻结）。
3. **Step 3** BLOCKED（§8）。
4. 可选扩展：`vol_trend_slope`；分钟级 signed order flow（若 `futu_channel_capital_flow` 毕业可替代
   合成代理）；regime 摘要入 LLM 上下文。

## 12. 回滚

- **本批**：revert 文档提交（纯 `.md`，无运行时影响）。
- **Step 1（将来）**：快照开关 false → 停写新字段（旧行留档不删）；或 revert 实现提交 +
  `systemctl restart ftseek-us` + 启动冒烟。
- **LLM 影子**：开关 false 下一确认即停（热重载 60s 内），零 live 影响。
- 任何阶段**无 live 行为可回滚**（record-only 设计保证）；不人工造单、不动方向/阈值/仓位/live 权限。

**最后更新：2026-11-28。**

---

## 13. 第 2 轮独立复核（2026-11-28，Kimi；仅追加，不改写原预注册）

> 性质：只读代码/产物复核 + 本节留痕；原 §1–§12 未改，状态仍为 draft / 未实施。
> 本节不授予 Step 1 实施或开关激活权；Step 1 仍须用户另行裁决。
> **总评：第一性原理、影子先行、双开关、冻结价格判据和 Step 3 BLOCKED 均合理；但两臂时点、veto 反事实、去重、资金流时点及统计功效尚有阻断性缺口。建议补齐合同后再实施，而非按现文本直接写代码。**

### 13.1 P0：实施前必须闭合的合同

1. **treatment 与 holdout 的快照时点目前不可比。** §6.1 写「两臂都快照，保证可比」，但现有 holdout 只在 arm 当时落行（`entry_confirm_gate.py:524-554`），treatment 终态行在 120s 及可能的 grace 之后落盘（`:940-970/:1065-1097`）。这会形成 holdout=T0、treatment=T0+120~150s 的时点混杂。
   - 推荐主分析采用**固定 landmark=T0+120s**：两臂都在该时点留同一特征快照；holdout 虽已成交，也只追加影子观察，不改变交易。
   - T0 的 arm 快照可作第二读数；grace 后的最终 treatment 快照只作诊断，不与固定时点 holdout 混作主对照。
   - 若不增设 holdout 的 T0+120s 观察，则只能把研究改名/降格为「arm 时刻量能」，不能声称测到了确认时刻条件化价值。

2. **「veto 子集左尾更差」只能证明风险排序，不能证明 veto 有经济价值。** RFC-158 §10.2 的 `ABANDONED净=0` 是评价现行确认门实际政策的口径，不是被 veto 信号若成交时的反事实结局。§7.2 的同 vol_class 分组也仍是观察性比较，不能消除 LLM 依据其他上下文选择 veto 的混杂。
   - 所有 assignment（包括 ABANDONED、未成交及 None 特征行）须从同一 landmark 留固定 horizons 的可成交 bid/ask 前向路径，至少报方向归一收益、MFE/MAE、费用/滑点假设与数据覆盖率。
   - veto 主读数必须同时包含**避免亏损、放弃盈利/误杀、反事实净差和总净额护栏**；不能只以大亏率更高判 PASS。
   - 影子阶段最多支持风险分层与短窗经济估计；完整出场路径的因果经济价值仍须 Step 3 单变量随机 A/B，不得在 Step 2 提前声称。

3. **重复 assignment / 冲突终态不能留给实现批再决定「取首或取末」。** 既有 `tail_objective` 当前逐行计入，未按 assignment 去重（`scripts/settle_entry_confirm_forward.py:438-461`）。本轮只读实测当前 cohort `type=entry_confirm` 200 行，按 `(inst_id, arm)` 仅 193 个键，存在 7 个重复键。`inst_id=5638` 先 CONFIRMED、后 ABANDONED，但 `decision_id=DA-20261116212614-US.GOOG-6f71` 实际有已平 PLAYBOOK_SIM 交易（trade id=1902，净 +$41.34）；机械取末会错记为弃单净 0，取首也不是通用正确规则。
   - 新研究须预注册唯一 `assignment_id`、固定 landmark 的 `event_id`、重复哨兵及冲突隔离规则；同终态重复不得重复计数，终态冲突须与实际成交对账，未核清不得进入 resolved 分母。
   - 这是**既有宿主计量缺陷，非本 DEB 引入**。宿主结算器是否修复、历史读数如何重列，应另开计量缺陷批并披露；不得借本 DEB 暗改 RFC-158 判据。

4. **`capital_flow_daily.jsonl` 不能直接承担盘中确认时刻特征。** 现有 `_capital_flow_divergence` 只取每 symbol 最新日期，并未执行 §6.1 所写的「非当日→None」（`intraday_indicators.py:307-322`）；日级聚合仅由盘后 cron 运行，盘中若严格加当日守卫，该特征会结构性全 None。
   - v1 最简方案：把资金流移出主假设，保留为后续扩展。
   - 若坚持 v1 纳入，应改用分钟级 `data/futu_capital_flow.jsonl`，以 `item_time<=snapshot_ts` 做 as-of 联接，累计窗口、最大允许龄、净流原始值/单位和缺失原因全部落 schema；禁止用当日盘后聚合值回填盘中决策。
   - 现有 bool helper 也不提供 `capital_flow_net_sign`，不能仅靠原函数复用得到草案全部字段。

5. **`vwap_slope` 单位写错。** 实现返回相邻两个 5 分钟 VWAP 的 `(recent-prior)/prior`（`intraday_indicators.py:219-235/:269-274`），是无量纲的 5 分钟窗相对变化，未除以分钟、也未乘 100；§6.1 的 `pct/min` 不正确。
   - 建议改名 `vwap_rel_change_5m` 并声明 `ratio`；若保留旧字段名，也须明确 `ratio per adjacent 5-minute windows`。
   - 契约例应钉死 100→101 返回 0.01，防止 LLM 输入和下游统计把 0.01 误读为 1%/min。

6. **累计日量比不足以刻画「确认窗放量」。** `_calc_intraday_vol_ratio` 是当日累计量/历史同时段累计量（`:460-596`）；同样的高读数可能来自开盘早已放量，也可能来自 arm→confirm 的 120s 真放量。它不能完全区分 §5 想测的「放量站稳」与「缩量 drift」。
   - 最小补充：同一数据源留 `cum_vol_arm`、`cum_vol_landmark`、`confirm_window_volume`（股数）及其 as-of 时戳；再增加按相同时段历史 120s 成交量计算的 `confirm_window_vol_ratio` 与分母版本。
   - 累计日量比保留为环境特征，局部确认窗量比才对应 doctrine；两者不得互相冒充。

### 13.2 P1：统计、LLM 与运行时补强

1. **拆成两个独立研究，而非一个联合 PASS。** Step 2A 只检验固定规则的量能条件化；Step 2B 检验 LLM 相对「同特征确定性基线」的增量。§7.4 当前要求量能与 LLM 同时显著才 PASS，会把两个变量重新捆绑，和一次一变量原则相冲突。
2. **收缩主假设并控制多重比较。** `vol_class × divergence × grace × half_day` 全交叉在 n=150 下会产生大量空格。须预先指定一个主 contrast、一个主终点、方向/分母路径等有限分层；其余明确为探索性并做 BH/FDR 或独立时间窗验证。「跨月同号」需写成可执行窗口与最低 n，而非口号。
3. **样本门按新 cohort 重起。** 旧行无快照，不得沿用宿主现有 resolved n 来满足新研究门。Step 2A 从首个有效固定时点快照起算；Step 2B 从 LLM 影子实际开启起另算，并分别要求每臂×主分层、veto/非 veto、SL_INITIAL 事件数与独立 ET 日下限。
4. **现数据已经提示 30% 大亏负担目标短期功效不足。** `entry_confirm_forward_latest.json`（2026-11-27 生成）虽已有 treatment resolved 133 / holdout 41 / paired 12 日，但结局覆盖率仅 87.5% / 89.13%，仍 INSUFFICIENT；holdout SL 负担 4.262$/assignment 的 30%=1.279，而当前该差值的日聚类 MDE80=4.4495，目标明显低于当前可分辨尺度，分层后只会更困难。实现前须用真实方差做功效/预计攒样期估算，并按 OKR-2026-11 KR4 补上**决策日期、最长观察期、到期 KILLED 与实验槽位**，不得无限 OBSERVE。
5. **缺失值须同时给完整案例与政策口径。** None 行可在特征诊断中排除，但模拟将来 fail-open 政策时必须按实际放行/不 veto 纳入 ITT，不得只分析数据齐全者；另报每臂、symbol、分母路径的 None 率。表现读数接 A9 双口径、A10 SSOT 谓词及净盈亏口径。
6. **LLM 缓存键过粗。** 同 symbol+direction+session 内不同 anchor/px/量能状态不得共享结论。只缓存完全相同输入，键至少含 assignment/event、特征快照摘要、prompt_hash、模型版本；落实际模型/别名、参数、prompt/schema hash、原始响应、解析状态、latency、token/cost、cache/fallback/error。模型或 prompt 改动须分 cohort，不能依赖 temp=0 假设确定性。
7. **LLM 不得阻塞 10s sweep 或 auto_execute。** `future.result(timeout)` 只停止等待，不会取消已运行 worker（既有 `pre_entry_gate.py:234-249` 同有限制）。建议有界异步队列 + 并发/在途/每日上限，结果以独立影子行追加并用 event_id 联接；不得在交易线程等 5s。同步写同一 forward 行与异步不阻塞二者应二选一，推荐后者。
8. **补全可复现元数据与输出不变量。** `vol_ratio` 应带原始分子/期望分母、source、quote/bar 时戳与龄、p50 历史窗日期/版本，而不是只留 ratio 和路径名；p50/linear 分别报告。`would_veto=true ⇒ conviction=low` 等一致性须进 schema 契约，或删掉冗余字段，避免自相矛盾样本。

### 13.3 建议的最小推进顺序（仍须用户另裁）

1. **先做计量前置**：明确固定 landmark、assignment/event 身份、冲突隔离、A9/A10/净值口径与功效/截止日期；宿主重复行问题另案处理。
2. **Step 1A 仅量能快照**：T0 与 T0+120s 两臂同尺；日累计量比 + 局部确认窗量比 + 完整来源/龄/None 原因；LLM 保持 OFF。
3. **Step 2A 单主假设**：先证明确定性量能条件化有增量；探索性切片不作激活证据。
4. **仅当 2A 有可分辨信号，才裁 Step 1B/2B**：LLM 异步影子与确定性基线比增量，不重复证明 vol_class 本身。
5. **Step 3 仍 BLOCKED**：影子风险分层不等于完整政策收益，任何 live/SIM 行为接入仍走独立 DEB、单变量 A/B 与用户签字。

### 13.4 本轮验证边界

- 已只读核对：DEB 原文、RFC-158、现行 config、注册表、entry_confirm 生产代码、现有结算器、量能/VWAP/资金流实现、相关 cron 与最新只读产物。
- 已用只读脚本复核重复 assignment、冲突实例和最新成交；未改 DB、config、注册表、代码、cron，未调用 LLM、未重启、未下单。
- 本轮是设计/计量复核，**未跑 pytest（无代码变更），未做任何收益回测，未证明量能有增量，也未证明现有宿主结论在修复重复行后会转向**。

---

## 14. 第 3 轮核验与整合（2026-11-28，本 agent；§13 逐条 grounded 复核 + 宿主计量缺陷根因实锤 + 修正合同）

> 性质：对 §13（Kimi 独立复核）逐条只读核验 + 本 DEB 原文实错更正 + P0-3 宿主缺陷根因取证。
> 本节不授予 Step 1 实施权；Step 1A/1B/2A/2B 仍须用户另裁。
> **落地边界（用户裁「按照下一步执行，如有把握且逻辑合理，可以直接落地，实现高质量交易」）**：本会话只落地
> ①本 DEB 整合（纯 `.md`）+ ②P0-3 宿主结算器计量缺陷修复（`scripts/settle_entry_confirm_forward.py`
> 的 `tail_objective` 裁决轴，cron 脚本，无服务重启、无 live 行为变更）。Step 1A 量能影子按 §13.3
> 共识「补合同后另批」，本会话不写影子代码；gate 重复终态行根因属 live 域，留痕另批（§14.5）。

### 14.1 §13 逐条核验结论（全部 grounded）

| §13 项 | 核验 | 证据锚 |
|---|---|---|
| P0-1 两臂时点不可比 | ✅ 成立 | holdout 行 arm 时落（gate:534-554，T0）；treatment 终态行 delay 120s+grace 后落（gate:940-970/1065-1097）。实测重复行 delay_actual_sec 138/200、146/198、130/186 均 >120 |
| P0-2 veto 左尾≠经济价值 | ✅ 成立 | RFC-158 §10.2 ABANDONED 净=0 是现行政策口径，非被 veto 信号若成交的反事实；§7.2 同 vol_class 分组仍观察性，不能消除 LLM 依其他上下文 veto 的混杂 |
| P0-3 重复 assignment | ✅ 成立且根因已实锤 | 见 §14.5（7 重复键 + gate 跨线程 stale-snapshot 重建竞态日志） |
| P0-4 capital_flow 无当日守卫 | ✅ 成立 | intraday_indicators.py:307-322 取每 symbol 最新日期（`record_date >= previous[0]`），无「非当日→None」；日级聚合仅盘后 cron（`0 7 * * 2-6`） |
| P0-5 vwap_slope 单位错 | ✅ 成立（本 DEB 实错①） | :269-274 `return (recent - prior) / prior`，无量纲相邻 5min VWAP 比值，未除分钟未乘 100，非 pct/min |
| P0-6 累计日量比≠确认窗放量 | ✅ 成立 | `_calc_intraday_vol_ratio` 当日累计量/历史同时段累计量（:460-596），高读数可来自开盘早已放量，不区分 arm→confirm 120s 局部放量 |
| P1-1…P1-8 | ✅ 全部成立 | 拆轴/单主 contrast+多重比较/新 cohort 重起/功效不足/None 的 ITT/LLM 缓存键过粗/future.result 不取消 worker（pre_entry_gate:234-249）/可复现元数据，逐条与代码及 latest.json 读数一致 |

### 14.2 共识点（10 项，双方一致）

1. 第一性原理定位不变：量能=确信度条件变量，非方向预测器；LLM=确信度合成器/veto，非方向 oracle。
2. 影子先行 + 双开关分离 + 默认 OFF + Step 3 BLOCKED + 价格判据/门阈值/holdout 分配/仓位冻结。
3. 主分析两臂固定 landmark=T0+120s 同尺快照；T0 快照第二读数；grace 后终态快照仅诊断，不与固定时点 holdout 混作主对照。
4. veto 经济价值须全 assignment 固定 horizons 可成交 bid/ask 前向路径 + 避免亏损/误杀/反事实净差/总净额护栏；Step 2 不冒充完整因果收益（须 Step 3 单变量随机 A/B）。
5. 计数轴=唯一 resolved assignment（RFC-158 §10.2 与本 DEB §6.1 早已预注册），非 jsonl 行数；同终态重复不得重复计数，终态冲突须与实际成交对账，未核清不得入 resolved 分母。
6. v1 资金流移出主假设（或改分钟级 as-of）；vwap 字段改名 `vwap_rel_change_5m`/`ratio`；补局部确认窗量比 `confirm_window_vol_ratio`，累计比与局部窗比不得互相冒充。
7. Step 2A（确定性量能条件化）/2B（LLM 相对同特征确定性基线的增量）拆轴，单主 contrast + BH/FDR，全新 cohort 重起计数（旧行无快照不得沿用宿主 resolved n）。
8. 功效前置：30%×holdout SL 负担 4.262=1.279 ≪ 当前日聚类 MDE80=4.4495，短期功效不足；须真实方差估算攒样期 + 决策日期/最长观察期/到期 KILLED/实验槽位（OKR-2026-11 KR4），不得无限 OBSERVE。
9. None 行双口径（完整案例 + ITT 政策口径）；LLM 精确输入缓存键（assignment/event+快照摘要+prompt_hash+模型版本）+ 版本留痕 + 有界异步不阻塞 10s sweep/auto_execute。
10. 推进顺序：计量前置 → Step 1A 仅量能快照（两臂同尺+局部确认窗量比，LLM OFF）→ Step 2A 单主假设 → 有可分辨信号才裁 Step 1B/2B LLM 异步影子 → Step 3 BLOCKED。

### 14.3 本 DEB 原文实错更正（不改写 §1–§12 原文，以本节为准）

- **实错①（§6.1 字段表 `vwap_slope`）**：原写「按实现声明（pct/min）」错。实现 `return (recent-prior)/prior`
  是相邻两个 5min VWAP 的无量纲比值。更正：字段改名 `vwap_rel_change_5m`，量纲 `ratio`（per adjacent
  5-minute windows）；契约例钉死 100→101 返回 0.01，防 LLM 输入与下游统计把 0.01 误读为 1%/min。
- **实错②（§6.1 `capital_flow_divergence`「复用 :278」+「非当日→None」）**：原表述隐含「复用现有函数即得
  当日守卫」，错。现有 `_capital_flow_divergence` 取每 symbol 最新日期、无当日守卫，且日级聚合仅盘后 cron
  运行 ⇒ 盘中严格加当日守卫会结构性全 None。更正：v1 资金流移出主假设；若纳入须改分钟级
  `data/futu_capital_flow.jsonl` as-of（`item_time<=snapshot_ts`）+ 原始净流/单位/最大允许龄/缺失原因落 schema；
  现有 bool helper 不提供 `capital_flow_net_sign`，不能仅靠复用得到草案全部字段。
- **表述降格（§6.1「两臂都快照保证可比」）**：现 holdout 行 T0 落、treatment 终态行 T0+120s+ 落，时点混杂。
  更正：可比性须靠两臂固定 landmark=T0+120s 同尺快照实现（P0-1），不是「两臂都落行」自动保证；若不增设
  holdout 的 T0+120s 观察，研究只能降格为「arm 时刻量能」，不得声称测到确认时刻条件化价值。

### 14.4 修正后的最小采集合同（Step 1A 另批实施前须钉死）

- assignment 身份：唯一 `assignment_id=(inst_id, arm)`（已实测 decision_id 跨多 inst=0、inst 跨 arm/decision_id=0）；
  固定 landmark `event_id`；重复哨兵 + 冲突隔离规则预注册。
- 两臂同尺：T0 与 T0+120s 各留同一特征快照；主分析用 T0+120s。
- 量能字段：`vol_ratio`（累计日量比，环境特征，带原始分子/期望分母/source/quote-bar 时戳与龄/p50 窗日期版本，
  p50/linear 分别报告）+ `cum_vol_arm`/`cum_vol_landmark`/`confirm_window_volume`（股数，带 as-of 时戳）+
  `confirm_window_vol_ratio`（按相同时段历史 120s 成交量，带分母版本）；累计比与局部窗比不得互相冒充。
- `vwap_rel_change_5m`（ratio）；资金流 v1 不入主假设。
- None 不归零 + ITT 双口径；`would_veto=true ⇒ conviction=low` 等一致性进 schema 契约或删冗余字段；LLM OFF（Step 1A）。

### 14.5 P0-3 宿主计量缺陷：根因实锤（gate 跨线程 stale-snapshot 重建竞态）

**数据面（只读 `mode=ro`+`query_only` 实测，cohort date≥2026-11-05，200 行/193 唯一 `(inst_id,arm)` 键/7 重复键全 treatment）：**

| inst | symbol | decision_id | 终态序列 | 实际已平成交 |
|---|---|---|---|---|
| 5539 | META | DA-20261110212134-US.META-8551 | ABANDONED+ABANDONED | 无 |
| 5547 | HON | DA-20261110212619-US.HON-866e | CONFIRMED+CONFIRMED | trade1843 −168.27（OVERNIGHT_GAP_STOP）|
| 5630 | CAT | DA-20261116212113-US.CAT-6e44 | ABANDONED+ABANDONED | 无 |
| 5633 | UBER | DA-20261116212343-US.UBER-6eda | CONFIRMED+CONFIRMED | trade1901 +12.1（SL_BREAKEVEN）|
| 5638 | GOOG | DA-20261116212614-US.GOOG-6f71 | **CONFIRMED(09:47:49)+ABANDONED(09:48:45) 冲突** | trade1902 +41.34（SL_TRAIL_LOCK）|
| 5650 | CAT | DA-20261117212657-US.CAT-c11d | CONFIRMED+CONFIRMED | trade1913 +31.58（SL_TRAIL_LOCK）|
| 5658 | BA | DA-20261117214211-US.BA-c4af | CONFIRMED+CONFIRMED | trade1910 +28.14（SL_TRAIL_LOCK）|

**根因（日志实锤，三日同型）：** 不是 RFC-178 fresh re-arm（7 笔 `orphan_rearms` 全 None），而是
**孤儿重建路径（gate:478 `_restore_arm`）被跨线程 stale snapshot 误触发**：

1. `[PlaybookExitFastLoop]`（10s sweep）先判终态 → `_PENDING.pop`（:967）+ 写终态 state + forward 行①；
2. `[LLMStrategyLoop]`（auto_execute 轮）持有**判定前取数的旧指令快照**，`read_state(inst)` 仍见 PENDING
   且 inst 不在 `_PENDING` ⇒ 误诊「重启孤儿」→ `_restore_arm` 按**原 armed_at/anchor/mid_arm** 重建窗口
   重入 `_PENDING` → defer（日志：11-10 22:41:11 META / 22:41:35 HON；11-16 22:48:02 CAT / 22:48:05 UBER /
   22:48:18 GOOG；11-17 22:50:24 BA / 22:50:35 CAT，均「旗标 PENDING 但内存无记录 (重启) → 按旗标重建门控窗口」）；
3. 下一 FastLoop sweep 对重建臂再判（delay 已耗尽立即到点）→ forward 行②，grace 全新生命周期、
   delay_actual_sec 续原 armed_at（实测 138→200 / 146→198 / 130→186）。

**后果**：①同终态重复行（6 键）⇒ 裁决轴重复计 assignment；②冲突终态（5638）⇒ 首次 CONFIRMED 已成交
（BOOKED/executed=1），二次 ABANDONED 覆盖 DB 终态、`mark_instruction_guardrail_skip` 因行已离开 PENDING
失败（skip_marked=False，:1019「执行竞态」保护）⇒ forward 行冲突但 trade 真实存在。**live 副作用**：重建
返回 defer 会延迟已确认指令执行；若二次判弃发生在执行前且 SKIP 落库成功，理论上可误 veto 已确认单
（本 cohort 未现该例，5638 因已成交而 SKIP 失败）。

**处置分工**：gate 重建竞态属 **live 交易行为域** ⇒ 本批不改 gate，须影子先行 + 另批裁决（候选修法：
孤儿重建前 fresh DB read 区分真重启孤儿 vs 跨线程 stale snapshot / 终态写后防重建守卫）。本批只修
**结算器裁决轴对账**（§14.6），使计量对重复/冲突终态行鲁棒，并披露读数 delta。

### 14.6 本批落地：P0-3 结算器计量修复（tail_objective 裁决轴，record-only）

- 范围：`scripts/settle_entry_confirm_forward.py::tail_objective` + main 的 `tail_joined` 取数。
  `_agg`（机制留痕行轴，按设计计 gate 事件行）/`current_cohort.sim`（实际 SIM 成交独立轴）/
  RFC-158 判据阈值文本**不动**（修计量实现 ≠ 改判据）。
- 规则（按 `(inst_id,arm)` 折叠唯一 assignment，每 assignment resolve 一次；缺 inst_id 回落 decision_id，
  两者皆缺按行——保持旧 fixture/无身份行逐行语义）：
  1. decision_id 在 joined 有已平 PLAYBOOK_SIM ⇒ resolved `closed_trade`（**实际成交 ground truth 优先**）；
  2. 无成交 + 全 ABANDONED（treatment）⇒ `abandoned` 净 0；
  3. 无成交 + 全 CONFIRMED/holdout ⇒ `unresolved`（不补 0）；
  4. 无成交 + 终态冲突 ⇒ `unresolved` + 冲突哨兵（未核清不得入 resolved 分母）；
  5. 同键多 decision_id ⇒ identity 冲突哨兵 + unresolved（防御，实测 0）。
- 哨兵披露：tail 输出新增 `assignment_integrity`（`duplicate_assignment_n` / `duplicate_extra_rows_n` /
  `terminal_conflict_n` / `terminal_conflict_resolved_by_trade_n` / `terminal_conflict_unresolved_n` /
  `identity_conflict_n`，分臂 + 合计）。
- main `tail_joined` 改取**全部** tail_rows 的 decision_id（原仅 `still_triggers` 行）⇒ 全 ABANDONED 但
  竞态有成交的 assignment 也能对账（当前数据无此例，取数超集不改既有 joined 条目）。
- before/after 重算披露见 handoff 顶条（同 200 行隔离修复效应）；verdict 仍 INSUFFICIENT（覆盖率<95%），
  无判据转向。契约 + 变异测试见 `tests/test_settle_entry_confirm_assignment_integrity.py`。

**最后更新：2026-11-28。**

---

## 15. 第 4 轮复查（2026-11-28，Kimi；§14 整合与 5b68635c1 实码复核）

> 性质：对当前磁盘版 DEB、结算器实码与契约的独立复查；仅追加记录，不改 §1–§14、不改代码。
> **结论：§14 已逐项回应 §13，方向与修正合同合理；5b68635c1 的去重/成交优先/冲突隔离实现与文档一致，当前读数可复现。发现两个当前数据未命中的防御性边界，建议小批补硬；它们不改变本次 before/after 数值，也不要求撤回当前修复。**

### 15.1 已独立验证

- 六项 P0 均已有明确修正合同：固定 T0+120s 两臂 landmark、veto 反事实经济口径、assignment 去重/冲突隔离、资金流 v1 退出主假设、VWAP 无量纲单位、局部 120s 确认窗量比。统计功效、决策日期和到期机制仍是实施前须实际填定的参数，不因写入共识就视为已有证据。
- 实码 `_assignment_key` / `_resolve_assignment` / `tail_objective` 与 §14.6 一致；main 已对全部 tail_rows 的 decision_id 取数，包含 ABANDONED。
- 独立复跑目标 + 既有契约：**21 passed, 71 deselected**（`tests/test_settle_entry_confirm_assignment_integrity.py` + `tests/test_entry_confirm_gate.py` 指定 tail 集合）。
- 全脚本以 `OUT=/tmp/entry_confirm_recheck_20261128.json` 独立跑通，rc=0；复现 treatment assigned/resolved=147/127、coverage=0.8639，holdout=46/41、0.8913，integrity dup=7/conflict=1/resolved_by_trade=1，verdict 仍 INSUFFICIENT。未覆盖生产 latest 之外的 DB/配置，生产输出路径未写。

### 15.2 两个残余防御性边界（当前数据零命中）

1. **全 ABANDONED 但实际存在未平仓成交时，仍会过早记净 0。** `load_trade_by_decision` 只返回已平仓交易（`settle_entry_confirm_forward.py:386-392`）；未平仓成交不在 joined，随后 `_resolve_assignment` 的 `states=={False}` 分支会记 abandoned=0（`:489-491`）。若未来竞态产生「全 ABANDONED + open trade」，该 assignment 会先错误 resolved，平仓后又改写成 closed_trade。
   - 建议 joined 同时携带 open-trade existence/state；只要实际成交存在且未平，就统一 unresolved，不得进入 abandoned=0。补「全弃单+open」「冲突+open」「holdout+open」三例。
2. **`bool(None)` 会把未知终态伪装成 ABANDONED。** 当前 `states={bool(r.get("still_triggers")) ...}`（`:475`）；缺失/None/非法值都会变 False。虽然当前 200 行非 bool 命中=0，但计量合同应保持「未知不归零」。
   - 建议 strict bool 校验；任何非 bool 作为 terminal_state_invalid/unresolved + 哨兵，不参与全 ABANDONED 判定。补 None、缺键、字符串 `"false"` 三例，防止字符串也被 `bool()` 误转为 True。

### 15.3 边界与后续不变量

- 当前全库无同一 decision_id 多 trade 行，当前 cohort 也无「全 ABANDONED + 任意成交」或非 bool still_triggers；因此 §15.2 是防御性硬化，**不是对当前修复数值的否定**。
- gate 跨线程 stale-snapshot 重建竞态仍是未修的 live 根因；本轮未改 app/、未重启、未下单。结算器鲁棒不等于源头已愈合。
- Step 1A/1B/2A/2B 仍未实施、仍须用户另裁；本轮不授予开关激活或 Step 3 权限。

**最后更新：2026-11-28。**

---

## 16. §15 两项防御边界硬化落地（2026-11-28；record-only 结算器）

> 授权边界：用户「继续」→ 只实施 §15.2 两项结算器计量硬化；不改 gate、config、注册表、cron，
> 不启动 Step 1 量能影子，不修 live 竞态。服务无需重启（盘后 cron fresh-import）。

### 16.1 实现

- 新增 `load_trade_existence_by_decision`：只读 `SELECT DISTINCT decision_id`，返回任意 PLAYBOOK_SIM
  实际成交集合（含 open）。与既有已平仓 PnL loader 分工，RFC-153 调用语义不变。
- `tail_objective(..., actual_trade_ids=None)` 新增可选存在性集合；main 同时传 closed PnL 与
  open/existence。已平成交仍优先；仅有 open trade ⇒ unresolved，禁止全 ABANDONED 先记净 0。
- `still_triggers` 改 strict bool；None/缺键/字符串/0/1/列表均不再布尔强转，转 unresolved。
- `assignment_integrity` 新增 `open_trade_unresolved_n` / `terminal_state_invalid_n`，逐臂+total；
  `resolution_contract` 同步。哨兵失效模式已登记 `docs/CONTROL-LOOP-LEDGER.md` 顶部现行覆盖。

### 16.2 契约与变异验证

- 新文件由 16 → **28 passed**（新增 open 三态、closed 优先、六种非 bool、缺键、main 接线等 12 个计数例）；
  与既有 tail 合同合跑 **33 passed, 71 deselected**。
- 变异①关闭 open-trade 分支 ⇒ **4 failed / 24 passed**；还原后 28 绿。
- 变异②恢复布尔强转并关闭 invalid 哨兵 ⇒ **7 failed / 21 passed**；还原后 28 绿。
  两次均以 `/tmp/settle_entry_confirm_hardening_post.py` SHA256 核验还原。
- py_compile 两文件 + `scripts/check_undefined_names.py` 均 exit 0。
- 全脚本写 `/tmp/entry_confirm_hardened_20261128.json` rc=0；arms/comparison/daily_paired/sample_gate/
  verdict/verdict_why 与硬化前 `/tmp/entry_confirm_recheck_20261128.json` **逐块相等**。
  新 open 哨兵当前=4，逐笔核为 BA/GS/META/BABA 四个 CONFIRMED 未平 assignment；全弃单+open=0，
  非法终态=0，故当前经济数值零变化。日志 `/tmp/entry_confirm_hardened_20261128.log`。

### 16.3 回归边界与未验证

- 棘轮/治理/根锚点回归：**223 passed, 4 failed**，日志 `/tmp/p03_hardening_ratchet_regress.log`。
  四红为既有/无关工作树病灶：except:pass 899→1817、pipeline.py 3700→3793、其他 app 文件 W6
  日志栈基线外增长、Hermes 幽灵 job `68cc76b16ebd`；本批未改这些文件，不顺带修、不调基线。
- 未跑全量 pytest/CI，不声称全绿；未做收益回测，计量硬化不代表策略收益改善。
- 当前为未提交工作树；回滚=还原本批 script/test/文档变更，无运行时服务回滚。下一自然 cron
  06:37 CST 需核新哨兵与临时端到端读数一致。gate live 竞态及 Step 1A 仍各待独立裁决。

**最后更新：2026-11-28。**

---

## 17. gate stale-snapshot 孤儿重建竞态源头修复（2026-11-28；用户「继续」）

> 本节是 §14.5 所述 live 根因的后续独立修复记录；不改 `eval_confirm`、门阈值、holdout、
> sizing 或方向判定。Step 1A 量能影子仍未启动。

### 17.1 修复合同

- **第一层：进程内 terminal tombstone。** sweep 在 `_PENDING.pop` 前先记
  `inst_id→(terminal_status, now_et)`；CONFIRMED/HOLDOUT/HEALED 类允许执行，
  ABANDONED/EXPIRED 继续 defer。即使 fresh DB 在 pop→终态写库窄窗仍读 PENDING，也不得重建。
  tombstone TTL=3600s、上限 1000，进程重启自然清空。
- **第二层：孤儿恢复前 fresh DB read。** 调既有 `PlaybookDB.get_instruction_by_id`；已知终态
  禁止重建并补 tombstone。fresh read 失败、无记录、仍为 PENDING 或未知态均回落既有孤儿恢复/
  fail-open，不改变真重启孤儿续窗语义。
- fresh read 期间若 `_PENDING` 已被其他线程恢复 ⇒ defer，禁止重复覆盖。
- 结算器 §16 的 open/invalid 防御保留；源头防护与计量兜底是两层，不互相替代。

### 17.2 契约、变异与部署验证

- gate 全契约 **84 passed**；真实 PlaybookDB 接线 **17 passed**；加 direction persistence 与
  assignment integrity 合跑 **131 passed**。覆盖 CONFIRMED/ABANDONED 旧快照、五种 fresh 终态、
  真 PENDING 孤儿仍恢复、真实 DB 在 tombstone 清空后仍不重建且 forward 行数不增。
- 变异①摘除 tombstone 门 ⇒ **2 failed / 82 passed**；还原 84 绿。
  变异②摘除 fresh-read 门 ⇒ **6 failed / 95 passed**（含真实 DB 例）；还原后两臂 **101 passed**。
  两次均按 `/tmp/entry_confirm_gate_racefix_post.py` SHA256 核验还原。
- py_compile + `check_undefined_names.py` exit 0；scoped diff check 通过。
- 棘轮/治理/根锚点回归 **223 passed, 4 failed**（`/tmp/entry_confirm_racefix_ratchet.log`）；
  四红与 §16 同一组无关既有病灶，本批未新增红、不修无关面、不调基线。
- 部署前核 app/scripts 仅本任务 gate+settler 改动；4 个既有 PLAYBOOK_SIM 仓 BA/GS SHORT、
  BABA/META LONG。`systemctl restart ftseek-us` 于 **2026-11-28 02:56:45 CST** 生效，
  PID **226603→242602**；启动冒烟 **7/7**，启动段零 Traceback/CRITICAL，重启前后四仓逐项一致。

### 17.3 未验证 / 回滚 / 后续

- 修复后尚无自然 stale-snapshot 竞态事件；只声明**代码生效 + 合成/真实 DB 契约通过**，不声称
  自然生产竞态已终证。后续交易日观察 `stale PENDING ... 不重建重复确认窗` 日志及
  `duplicate_assignment_n` 不再因该根因增长。
- 回滚=还原 `app/playbook/entry_confirm_gate.py` + `systemctl restart ftseek-us` + 启动冒烟；
  结算器硬化可独立保留。无 config/registry/cron 改动。
- 当前仍未 commit；Step 1A/1B/2A/2B 与任何新量能开关仍须用户另裁。

**最后更新：2026-11-28。**
