---
title: RFC-213 — 结构识别与论点驱动的 TradePlan / 持仓状态机
status: proposal_shadow_only
created: 2026-11-18
related_deb: DEB-20261118-exit-profitability-full-chain-audit
---

> **复核入口**：供另一 Agent 独立分析、证伪和补充的完整底稿及 12 个开放问题，已写入 `docs/debates/DEB-20261118-exit-profitability-full-chain-audit.md` 的「R2 设计正文」。后续讨论以该 DEB 的逐轮追加记录为准，本 RFC 为同步设计副本。

# RFC-213 — 结构识别与论点驱动的 TradePlan / 持仓状态机

## 0. 结论

不再对统一的 SL/TP/BE 百分比做网格调参。新增一个**单影子、零交易行为**的研究方向：把 DA 现有 `entry_archetype + invalidation_triggers` 升级为结构化 TradePlan，并用多时间级别市场结构驱动持仓状态机。

- 不推翻 Playbook，不新增下单管线；最终仍由 `DualBrokerAdapter` 执行。
- LLM 不直接下单、不放宽止损、不逆势加仓、不翻仓；只输出 `HOLD / TIGHTEN / EXIT / ABSTAIN`。
- 快环灾难止损、组合风险、价格合法性、仓位上限和订单状态始终由确定性代码控制。
- 第一阶段只接 record-only 影子；现役 qwen4、入场、尺寸和退出参数不变。

## 1. 理论如何转成可执行机制

| 理论 | 借鉴的不是口号 | 可计算表达 | 管理用途 |
|---|---|---|---|
| 利弗莫尔 | 领头羊、关键点、自然反应、让利润奔跑 | 个股相对 SPY/板块 RS、 pivot、突破后接受、最后确认的更高低点 | 趋势延续入场；结构完整时不用固定小回撤杀掉赢家 |
| 缠论（简化） | 多级别、中央重叠区、回踩、背驰 | 已确认 swing、HH/HL/LH/LL、重叠价值区、价格与量能/动量背离 | 区分正常回踩与结构破坏；背驰只作收紧警告，不单独反向 |
| Wyckoff / 拍卖理论 | spring/upthrust、接受与拒绝 | 扫过区间极值后收回、区间外停留时间、回踩是否再被拒绝 | 假突破/反转入场与快速失效 |
| Elder 三屏 | 高阶定方向，中阶定形态，低阶定触发 | 60m regime → 15m setup → 5m trigger → 1m 执行 | 防止用 1m 噪声推翻高阶论点 |
| Van Tharp | 以 R 衡量期望，不以胜率或固定百分比自欺 | 初始结构风险 1R、MFE/MAE(R)、各状态净期望 | 判分与尺寸，不让 LLM 改写风险预算 |

不实现“全自动标准缠论递归笔段”。其定义分歧和 repaint 风险过高；只实现无未来函数的 chan-lite 结构特征。

## 2. 先复用现有资产

- DA 已有 `BREAKOUT / PULLBACK_LIMIT / FADE_SHORT`（Hermes `llm_synth.py:1340-1360`）。**不用于准入过滤或配额**（形态轴已有负证据），只用于确定管理语义。
- DA 已有 `invalidation_triggers`，但指标只有收盘价、量比、5日涨跌、盘前涨跌，且下游不实平。
- `app/playbook/invalidation_shadow.py` 已预留 Stage 2b 结构化规则；本 RFC 是该设计的扩展，不另起炉灶。
- `app/playbook/adaptive_trailing.py` 当前按 MFE 比例给回撤；新增结构锚影子与它对比，不直接替换。
- `app/playbook/pipeline_parts/exit_monitor_flow.py` 已采集 peak/trough/MFE/MAE 并调用 invalidation shadow；新管理器在同一观察点旁路记录。

## 3. TradePlan v1 契约

```json
{
  "schema_version": "trade_plan_v1",
  "setup": "BREAKOUT",
  "as_of": "2026-11-18T09:35:00-05:00",
  "regime": {
    "daily": "UP",
    "m60": "UP",
    "sector_rs": "LEADING"
  },
  "structure": {
    "pivot": 101.20,
    "range_low": 98.40,
    "range_high": 101.20,
    "last_confirmed_swing_low": 99.10
  },
  "entry": {
    "trigger": "m5_close_above_pivot_then_retest_holds",
    "entry_zone": [101.20, 101.55],
    "ttl_bars_5m": 6
  },
  "invalidation": {
    "hard_stop": 98.90,
    "structural": "m5_close_back_in_range_and_failed_reclaim",
    "time": "no_acceptance_within_3_bars_5m",
    "event": "cached_event_risk_rule"
  },
  "management": {
    "trail_anchor": "last_confirmed_m15_swing_low_minus_0.2_atr",
    "on_momentum_divergence": "REVIEW_TIGHTEN_ONLY",
    "overnight": "ONLY_IF_STRUCTURE_INTACT_AND_EVENT_CLEAR"
  },
  "evidence_ids": ["bar:...", "news:...", "sector:..."],
  "uncertainty": "missing_level_2"
}
```

### 硬校验

1. 所有价格带单位=USD，所有 pct 单位=百分点，所有 ATR 倍数无量纲。
2. 每个条件必须包含 `metric / operator / value / timeframe / price_basis / known_at`；不能只写“走弱”“企稳”。
3. re-price 后必须同时保存 `planned / effective / fill` 三时点结构锚；不得把旧 pivot 机械平移到新价。
4. LLM 给出的新止损只能向有利方向移动；违反即丢弃整个动作并告警。
5. 数据不足只能 `ABSTAIN`，由现有确定性规则继续管理，禁止编造结构。

## 4. 两种首发 setup（不再扩第三种）

### A. BREAKOUT：趋势关键点突破—回踩

利弗莫尔关键点 + 缠论中央区突破/回踩 + 拍卖接受。

1. 60m 同向、板块不拖累、个股 RS 不落后。
2. 15m 已形成重叠区间与明确 pivot。
3. 5m 收盘突破，随后回踩 pivot 不失；不接受单纯瞬时穿越。
4. 初始灾难止损在回踩结构外侧；尺寸仍走现有风险预算。
5. 若突破后若干 5m bar 内重新被区间接受且 reclaim 失败，属 FAILED_BREAKOUT，应比统一 60min 死信号更早复核。
6. 若形成连续 HH/HL，进入 TREND，止损跟随最后确认的 15m higher low，而非始终固定 0.8% 回撤。

### B. PULLBACK_LIMIT / FADE_SHORT：趋势回踩或假突破反转

- 趋势回踩：高阶趋势未坏，低阶回到结构区后出现承接；两个连续低级别低点继续下移说明不是回踩而是趋势破坏。
- 假突破反转：扫过区间极值后重新回区，反弹/回踩再被拒绝才入场；不接尚未出现拒绝的飞刀。
- 失效锚是 sweep extreme 或承接结构，不使用统一百分比。
- 时间退出按“应该多久出现拒绝/承接”定义，而非所有 setup 共用 60min。

以上 bar 数与 0.2ATR 都只是**影子初始假设**，不是已证明的生产参数。

## 5. 持仓状态机

```text
WATCH
  → ARMED               结构条件满足，等待触发
  → WAIT_CONFIRMATION   已入场，等待市场证明
  → TREND               HH/HL 或 LH/LL 延续，使用结构 trailing
  → RANGE               高阶无方向，使用区间失效，不假装趋势
  → FAILED              突破/承接未被接受，快速复核退出
  → THESIS_BROKEN       结构或事件论点失效，确定性退出
```

- `CLIMAX / DIVERGENCE` 是风险提示标签，不直接等价反向信号。
- 状态只能由当时已知数据推进；每个 pivot 保存 `confirmed_at`，禁止右侧未来 bar 回填成当时已知。
- LLM 只能在状态边界提供判断；10s 快环不调用 LLM。

## 6. LLM 如何真正替代人力盯盘

### 事件触发而非轮询烧钱

只在以下事件调用：

- 新 confirmed swing、突破/回踩/假突破；
- 价格触及计划失效位或 trail anchor；
- 5m/15m 状态改变、量能异常、板块相对强弱翻转；
- 固定 15min 持仓摘要与 EOD 隔夜复核；
- 重大新闻/财报事件（只引用已有事件链）。

30s 合并重复事件，同 symbol 非硬事件 5min 最多一次；灾难止损不等待模型。

### 每次输入是 compact StatePacket

包含：原 TradePlan、当前状态、已确认 swing、最近有限 bars、MFE/MAE(R)、板块/市场状态、事件风险、当前 effective SL、报价龄与数据缺口。共享市场/板块上下文复用缓存；每 symbol 只发送增量。

### 输出只有四类动作

```json
{
  "action": "HOLD",
  "state": "TREND",
  "reason_code": "RETEST_HELD",
  "new_stop": null,
  "ttl_bars_5m": 2,
  "evidence_ids": ["swing:...", "bar:..."],
  "confidence_note": "低级别量能不足，但结构未坏"
}
```

允许 `HOLD / TIGHTEN / EXIT / ABSTAIN`；禁止 `ADD / WIDEN / REVERSE`。`confidence_note` 仅解释，不参与仓位或门控。

LLM 不可用/超时/schema 失败时：`ABSTAIN + 现有规则继续`，绝不变成“无止损”。

## 7. 隔夜逻辑的新影子臂

当前 W_WEEK 主要按是否水下决定多夜豁免。新臂比较：

- 不是“亏了所以继续拿”，而是“论点是否仍有效”；
- 不是“盈利所以必须收”，而是“趋势是否完整、隔夜事件是否可控”；
- 结构破坏即使亏损也不能借修复窗扛单；结构完整也不能仅因浮盈机械砍掉；
- 任何隔夜仍受现有组合风险、事件风险和硬止损约束。

这只是与现行臂配对的新假设，不重开 C14/C17 的全砍/减半隔夜方案，也不直接关闭 W_WEEK。

## 8. 实施切片（严格顺序）

### Slice 0：纯确定性契约，零 LLM 成本

建议新增：

- `app/playbook/market_structure.py`：无未来函数 swing/区间/状态特征；
- `app/playbook/trade_plan.py`：schema、单位、几何和 only-tighten 校验；
- `app/playbook/position_state_machine.py`：确定性状态转换；
- `app/playbook/thesis_manager_shadow.py`：record-only 决策与反事实台账。

测试：pivot 确认时点、reprice 锚、LONG/SHORT、缺数据、LLM 超时、错误 JSON、放宽止损、重复事件、重启恢复。

### Slice 1：现有仓位前向影子

- DA prompt 只增加 `management_plan`，不改方向、评分、候选池或入场参数。
- 现有 Playbook 仓仍按原规则出场；影子记录新状态机与 LLM 会做什么。
- 注册表只登记一个 `thesis_management_shadow`，合并失效契约/结构 trailing 研究，不再繁衍多个同轴影子。

### Slice 2：机制灰度（仍需用户另裁）

先仅允许 `TIGHTEN` 或结构性 `EXIT` 中一种，不同时改入场和尺寸。任何灰度都可一键回到 `mode=shadow`。

### Slice 3：入场触发状态机

只有出场侧机制与净结果均过门后，才把 BREAKOUT/PULLBACK 的触发条件接入入场；不同时改仓位、模型和隔夜规则。

## 9. 判据与回滚

### 工程门（不宣称 alpha）

- schema 有效率 ≥99%，结构字段覆盖率 ≥95%；
- only-tighten 违规执行数=0，未来函数案例=0；
- LLM 故障全部落入确定性旧规则，无无保护仓；
- 每次动作可由 `plan_hash + state_hash + evidence_ids` 重放。

### 机制门

- 对 FAILED_BREAKOUT 的识别不晚于现行 60min DEAD_SIGNAL；
- early-tight 误杀与 OGS 损失分开报告，不能用赢家减少掩盖左尾扩大；
- TREND 组 MFE capture 改善时，净收益和最大单笔亏损不得显著恶化。

### 收益门

- 机制灰度样本只证明行为，不证明盈利；
- 收益判断至少 n≥120 配对仓、≥20 ET 日，按日聚类报告 paired Δ、80%CI 与 MDE80；
- paired Δ 的 CI 下界>0 且均值>MDE，才讨论扩大；
- 同期不得改 qwen 模型、入场、尺寸、费用门或其他退出参数。

回滚：关闭影子无行为；灰度期 `thesis_manager_mode=shadow` 即恢复当前全部出场。

## 10. 明确不做

- LLM 每 10 秒看一次全市场或直接生成订单；
- 用未来确认的缠论笔段回测当时交易；
- 让 LLM 自由修改硬止损、加仓摊平或反手；
- 同时上线几十种理论/形态，或把 archetype 用作准入配额；
- 用历史最终赢家/输家文本训练“事后诸葛亮”提示词；
- 第一阶段就替换 qwen4、重写入出场和尺寸。
