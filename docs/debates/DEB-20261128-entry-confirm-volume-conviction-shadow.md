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
