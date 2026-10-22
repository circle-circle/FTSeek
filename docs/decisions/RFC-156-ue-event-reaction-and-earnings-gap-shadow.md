---
title: "RFC-156 UE 事件时效/事件后价格反应字段 + 财报 gap low_atr 影子验证"
status: draft
module: universe_expansion
related_code: scripts/llm_universe_scan.py, app/event_stream/event_schema.py, app/playbook/pipeline_parts/intraday_indicators.py
related_okr: OKR-2026-10
related_adr: ADR-035
tags: [ue, event-meta, event-reaction, shadow, low-atr, earnings]
updated: 2026-10-21
---

# RFC-156 UE 事件时效/事件后价格反应字段 + 财报 gap low_atr 影子验证

**状态**: active（Phase 0A 数据补全已实施；Shadow A/B 未启用，交易门未改）

**类型**: 输入信息补全（event_reaction_v1） + 两个只读影子（事件时效队列、财报 gap low_atr 豁免）

**关联**: RFC-141（入场确认）、RFC-148（DA 为主/UE 观察）、RFC-153（FADE_SHORT 开盘禁追）、
RFC-155（风险尺寸影子）、`docs/analysis/2026-10-20-left-tail-autopsy.md`。

**C1 名额**: 本 RFC **Phase 0 不占用** OKR-2026-10 C1 新入场假设名额。
若 Shadow B 未来毕业并提议放宽 low_atr，该变更属执行/预筛门，须另立 RFC + 用户批准；
10 月 C1 名额已被 RFC-153 占用，任何放宽最早 11 月，或需用户明确破例。

---

## 1. 背景（2026-10-21 08:00 ET da_feed 实盘读数）

10/21 da_feed scan（08:02:59 ET，v2.15/ue_brief_v7，11 只 surfaced）暴露两个输入层缺口：

### 1.1 事件时效与“事件后价格反应”缺失

| 标的 | 扫描判断 | 事件时间 | 事件日收盘 | 10/21 08:16 盘前 |
|---|---|---|---:|---:|
| HIMS | SHORT 0.55，`MARKET_CORROBORATED` | 10/20 08:49 ET（约 23h 旧） | **+3.77%** | +0.5% |
| BAX | SHORT 0.45，`price_volume_confirmed=false` | 10/20 10:02 ET（约 22h 旧） | -1.92% | +0.16% |
| CBRE | LONG 0.45，`price_volume_confirmed=false` | 10/20 13:18 ET（约 19h 旧） | **+2.65%** | +0.57% |

- HIMS 的新闻原文称“股价大幅下跌”，但事件日收盘其实上涨 3.77%；扫描只看到新闻措辞，
  把“旧闻 + 反向收盘”当成 `MARKET_CORROBORATED`。
- BAX/CBRE 的事件日收盘已经给出方向确认（-1.92% / +2.65%），但扫描只看当天盘前榜
  （两者不在榜上），标成“未确认”。
- 根因：event fact 只有绝对时间戳，没有相对事件龄；也没有“事件后第一个交易时段的
  价格反应”字段。模型无法判断“这条新闻是刚发生还是昨天的”，也没有收盘方向可对照。

### 1.2 财报 gap 被 low_atr 预筛误杀

- NFLX 10/20 盘后财报，10/21 盘前 **-6.64%**、成交约 167 万股；
- 但 `screening_atr_ratio=0.0271 < playbook.low_atr_gate_min_atr_pct=0.03`，
  被 UE 预筛标 `low_atr` → 只作 context，不作直接候选；
- ATR 是历史平均波幅，财报后首日的波动是事件驱动，与历史 ATR 不是同一分布；
  当前门槛把“平时安静、今天有大事”的标的系统性排除。
- 执行层还有第二道 `low_atr` 门；即使只放宽 UE 预筛，playbook 仍会拦，
  因此任何放宽必须两层一起验证，不能只改一层。

### 1.3 不是本 RFC 目标的问题

- HIMS 的 squeeze 4/5、crowding=false；SPCX/SMCI 的纯盘前追高（预期波幅已被盘前走完）：
  由 DA quality gate / chase cap 处理，本 RFC 不为单日样本新增硬门。
- 事件新旧不应成为自动否决：法律/监管事件可能持续发酵，年龄是上下文，不是 veto。

---

## 2. 目标与非目标

### 2.1 目标

1. **Phase 0A（数据补全，零交易行为）**：为 UE brief 的每条事件事实附加
   `event_reaction_v1`，至少包含 `age_h`、`post_event_move_pct`、`reaction_basis`；
   让模型能看见“这条事件多久以前、事件后价格怎么走”。
2. **Phase 0B（只读影子 A）**：验证 `age_h + post_event_move_pct` 是否对 UE 信号质量
   有区分力，尤其是“旧事件 + 方向相反”队列是否系统性更差。
3. **Phase 0C（只读影子 B）**：验证“财报后首日 + 大 gap + 放量 + 流动性达标”的标的
   若豁免 `low_atr`，是否值得；同时在 UE 预筛与 playbook 执行门两层记录
   `would_surface` / `would_block`，不做实际放宽。
4. 为未来可能的 prompt 软规则或 low_atr 窄豁免提供预注册证据；任何生效变更另立 RFC。

### 2.2 非目标

- 不因 HIMS 一天样本新增“事件龄 > Xh 禁止做空/做多”硬门。
- 不删除/放宽现有 `low_atr` 门；Phase 0 只记录。
- 不改 chase cap、entry-confirm、archetype routing、仓位、止损、退出。
- 不新增第二条 LLM 链；event_reaction 是确定性数据，不调用 LLM。
- 不改变 `event_meta_v1` 的身份/事件族语义；reaction 独立成 `event_reaction_v1`。
- 不在 Phase 0 消耗 C1 名额；不放开 LIVE。

---

## 3. 数据 schema：`event_reaction_v1`

### 3.1 设计选择

**不修改 `event_meta_v1`**。原因：

- `event_meta` 是事件的稳定身份（source/family/form/entities/materiality），在 ingest
  时写入；`event_reaction` 依赖“扫描时刻”和价格数据，同一事件在不同时间读数不同
  （age 增长、盘前→收盘→次日盘前），不适合在 ingest 时固化。
- 单独 schema 允许对旧事件在 brief 构建时**按需计算**，无需回填/重 ingest；
  旧记录缺字段时按 `None` 降级，向后兼容。
- 避免 `event_meta` 版本升级迫使所有消费者迁移。

**计算时机**：`_build_clue_brief` / `_gather_*_clues` 构建事实时，按事实的 symbols
与 event_time 计算；不写入 `canonical_events`，不新增 DB 列。计算失败一律 `None` +
`reason`，不阻断 clue/扫描。

### 3.2 事实层字段

在 `_fact(...)` 产出的 evidence entry 中新增（`EVIDENCE_SCHEMA` 增加 `event_reaction`）：

```json
{
  "source": "event_stream",
  "text": "[T2/NEWS_DISCOVERY] 2026-10-20T12:49:00Z | Hims & Hers ... | source=...",
  "symbols": ["HIMS"],
  "event_meta": {"schema": "event_meta_v1", "...": "..."},
  "event_reaction": {
    "schema": "event_reaction_v1",
    "computed_at": "2026-10-21T08:02:59-04:00",
    "by_symbol": {
      "HIMS": {
        "age_h": 23.2,
        "event_session": "premarket",
        "post_event_move_pct": 3.77,
        "reaction_basis": "same_day_close",
        "reaction_at": "2026-10-20T16:00:00-04:00",
        "reaction_price": 29.21,
        "reference_price": 28.15,
        "reference_at": "2026-10-19T16:00:00-04:00",
        "source": "futu_daily_kline",
        "reason": null
      }
    }
  }
}
```

字段定义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | str | 固定 `event_reaction_v1` |
| `computed_at` | ISO str | brief 构建时刻（ET，带时区） |
| `by_symbol` | dict | key=裸 ticker（无 `US.`）；最多计算 3 个主体；无 symbol 时为空 |
| `age_h` | float/null | `computed_at - event_time` 的小时数；event_time 缺失/非法/未来 → null，`reason` 说明 |
| `event_session` | enum | `premarket` / `regular` / `after_hours` / `overnight` / `closed`，按 ET 与当日实际收盘（含半日市）判定 |
| `post_event_move_pct` | float/null | `(reaction_price / reference_price - 1) * 100`；正=上涨；无法计算 → null |
| `reaction_basis` | enum | 见 §3.3；无法计算 → `pending`/`unknown` |
| `reaction_at` | ISO str/null | 反应价的时间 |
| `reaction_price` | float/null | 反应价 |
| `reference_price` | float/null | 参照价（通常为事件前最后一个常规收盘） |
| `reference_at` | ISO str/null | 参照价时间 |
| `source` | str | `futu_daily_kline` / `futu_quote` / `none` |
| `reason` | str/null | `no_event_time` / `no_symbol` / `no_kline` / `premarket_not_started` / `non_trading_day` / `stale_event` / `other` |

### 3.3 事件时段与反应口径（核心规则）

使用 ET 时区、`app.utils.holiday_calendar` 的交易日与 `get_us_close_minutes_et`
（含 13:00 半日市）。事件时间解析失败 → `age_h=null`、`reaction_basis="unknown"`。

| `event_session` | 判定（ET） | 反应口径 `reaction_basis` | 参照价 / 反应价 |
|---|---|---|---|
| `premarket` | 04:00 ≤ t < 09:30（交易日） | 事件发生在**之前交易日** → `same_day_close`；发生在**当日**且收盘未到 → `same_session_premarket` | 参照=事件前最后收盘；反应=当日收盘 / 扫描时盘前价 |
| `regular` | 09:30 ≤ t < 当日实际收盘 | 当日收盘已到 → `same_day_close`；未到（midday）→ `intraday` | 参照=事件前最后收盘；反应=当日收盘 / 当前价 |
| `after_hours` | 实际收盘 ≤ t < 20:00 | `next_premarket`（次日盘前已开）→ 否则 `next_open` → 否则 `pending` | 参照=事件日收盘；反应=次日盘前/开盘 |
| `overnight` | 20:00 ≤ t 或 t < 04:00 | 同 `after_hours` | 参照=事件前最后收盘；反应=次日盘前/开盘 |
| `closed` | 周末/假日 | 同 `after_hours` | 同上 |

补充：

- **盘后财报**（如 NFLX 10/20 盘后）必须看 **10/21 盘前/开盘**，不能拿 10/20 收盘当反应；
- **盘中事件**（BAX/CBRE）用当日收盘 vs 事件前收盘，属一阶近似；日线包含事件前漂移，
  这是已知偏差，Phase 0 接受并在 `reaction_basis` 标注；若后续需要精确事件窗收益，
  Phase 1 再用 1m bar 计算 `event_time → close` 收益，不在此 RFC 直接做。
- 同一事件多标的：`by_symbol` 分别计算；brief 渲染最多 3 个主体，避免 token 膨胀。
- 事件超过 3 个交易日：仍可计算，但 `reason="stale_event"` 可选留痕；影子按 age 分桶。
- 数据缺失/超时：`post_event_move_pct=null`，不阻断扫描；记录 `reason` 供覆盖率审计。

### 3.4 渲染与消费

1. **Brief 文本**：在每条事件事实末尾追加一行（短）：
   - 单标的：` | 事件后: +3.77% (同日收盘, 23h前)`
   - 多标的：` | 事件后: HIMS +3.77% (同日收盘, 23h前); BAX -1.92% (...)`
   - 无法计算：` | 事件后: 未知 (无K线/盘前未开)`
   - `BRIEF_TEMPLATE_VERSION` 从 `ue_brief_v7` 升 **`ue_brief_v8`**（文本模板变更）。
   - `PROMPT_VERSION` 不变（Phase 0 只加信息，不加结论规则）。
2. **Surfaced 输出**：`surfaced[]` 增加
   `evidence_matched_event_reaction`（该标的的 by_symbol 子对象）；
   `quality_shadow.py` 增加 `event_age_h` / `post_event_move_pct` / `reaction_basis` /
   `reaction_source` 字段，供 cohort 报表读取。
3. **DA 透传**：外部 `pipeline.py` 的 UE candidate 字段增加
   `ue_event_reaction`；`_ue_extra` 增一行
   `UE 事件反应: +3.77% (同日收盘, 23h前)`。
   这是已有事件上下文的补充，不改变 `_source` 门控。
4. **Health**：`scripts/ue_da_chain_health.py` 增加
   `event_reaction_coverage`（scan surfaced）与 `prompt_event_reaction`（DA checkpoint）
   两行读数；旧字段保持。

### 3.5 计算实现与数据源

- 新增纯函数模块 `app/event_stream/event_reaction.py`：
  - `compute_event_reaction(event_time, symbols, computed_at, kline_provider, calendar)`
  - `classify_event_session(event_time, calendar)`；
  - 输入注入，不触网/不写库，便于单测。
- `scripts/llm_universe_scan.py` 在 `_build_clue_brief` 内接线：
  - 复用现有 Futu 数据路由/K线缓存（每 symbol 近 5 个交易日日线）；
  - 每轮最多计算 60 个 symbol，单 symbol 超时/失败 → `None`；
  - 进程内缓存当日 K线，避免每条 fact 重复请求；
  - 不新增外部付费源；不调用 LLM。
- `sec_8k` / `sec_13dg` / `news_headline` / `event_stream` 四条 clue 路径统一调用；
  `event_stream` 的 symbols 用 `relevance.symbols`，直连通道用 fact 自带 symbols。
- 旧 canonical event 无需回填；在 brief 构建时按 event_time 现算。

---

## 4. Shadow A：事件时效/反应队列（只读）

### 4.1 目的

回答：**“旧事件 + 方向相反的事件后反应”是否系统性更差？** 以及
`age_h + post_event_move_pct` 是否值得未来升级为 prompt 软规则/降权。

### 4.2 数据与队列

- 每轮 da_feed / premarket scan 的 surfaced 行（含 SKIP）落一份只读 JSONL：
  `data/shadow/ue_event_reaction_shadow.jsonl`。
- 字段：`date`、`scan_window`、`symbol`、`direction`、`confidence`、`self_tier`、
  `event_family`、`source`、`age_h`、`post_event_move_pct`、`reaction_basis`、
  `fwd_intraday_pct`、`fwd_t1_pct`、`mfe_pct`、`mae_pct`、`left_tail`。
- 前瞻口径（写死，读数前不改）：
  - entry = 扫描日下一个常规开盘（09:30 ET；半日市按实际开盘）；
  - `fwd_intraday_pct` = 当日收盘 / entry - 1；
  - `fwd_t1_pct` = 次日收盘 / entry - 1（主判据）；
  - 方向归一并扣费（沿用 `quality_shadow_report.py` 的费用口径）；
  - 无法取 bar 的行标 `missing_bar`，不参与均值。
- 队列（方向归一后，“顺向”= 对交易方向有利）：
  - `A1 fresh_aligned`：`age_h ≤ 12` 且 `post_event_move_pct` 与方向同号；
  - `A2 stale_aligned`：`age_h > 12` 且反应同号；
  - `A3 stale_opposite`：`age_h > 12` 且反应反号（HIMS 型）；
  - `A4 fresh_opposite`：`age_h ≤ 12` 且反应反号；
  - `A5 missing`：无 reaction 或 `pending`。
- 报告：n、均值/中位 fwd bps、胜率、左尾率、MFE/MAE、按日聚类 t、MDE80。

### 4.3 预注册裁决（读数前写死）

- **样本门**：≥10 个 ET 交易日且 A3+A4 合计 ≥30 行，方可裁决；不足 → `INSUFFICIENT`。
- **支持规则候选**：A3 的方向归一 `fwd_t1_pct` 均值 ≤ -0.15%（跟随该信号会亏）
  且按日聚类 `t ≤ -2.0` 且 n≥30；同时 A3 明显差于 A1（差值 ≥0.30pp）。
- **不支持**：A3 与 A1 无显著差异，或 A3 反而为正 → 只保留信息字段，不加规则。
- 即使支持，也只产出 **prompt 软规则/降权候选**，须另立 RFC + 用户批准；
  不自动改扫描/执行门。
- 死线：2026-11-30 或样本达标，先到者为准；到点不足则如实记 `INSUFFICIENT`，
  不得延长观察来凑显著。

---

## 5. Shadow B：财报 gap low_atr 豁免（只读）

### 5.1 目的

回答：当前 `low_atr` 门是否系统性误杀“财报后首日 + 大 gap + 放量 + 流动性达标”的
标的；若豁免，扣费后是否值得，且左尾/点差是否可控。

### 5.2 记录点（两层）

1. **UE 预筛**（`scripts/llm_universe_scan.py` 的 input_prescreen）：
   - 对 `low_atr` 拒绝的候选，若同时满足 §5.3 条件，记
     `would_surface=True`；**不**放进 surfaced/LLM。
2. **Playbook 执行门**（`app/playbook/pipeline_parts/intraday_indicators.py`
   的 low ATR gate）：
   - 若某指令到达该门且被拦，记 `would_block_low_atr=True`；
   - 若 UE 预筛未来放宽，这一层仍会拦，因此必须两层同时测量。
3. 产物：`data/shadow/earnings_gap_low_atr_shadow.jsonl`；
   评估器：`scripts/earnings_gap_low_atr_shadow.py`（只读，不写库、不下单）。

### 5.3 影子候选条件（预注册，阈值可先按建议值）

- 财报事件：当日发布，或上一交易日盘后发布；`earnings_age_h ≤ 24`；
- `abs(premarket_gap_pct) ≥ 4.0%`；
- 相对量能 `rvol ≥ 3.0x`（若无法算 rvol，用盘前成交额 ≥ $20M 替代并标注）；
- `price ≥ $10`；
- `ADV_usd ≥ $50M`；
- `spread_bps ≤ 50`（无法取得则记 null，且该行不参与“毕业”判定）；
- 仅 `low_atr` 一个拒绝原因；若同时触发价格下限/流动性/其他门，保持排除，不纳入影子。
- 以上阈值是 Phase 0 预注册值；若用户要调整，须在读数前改并留痕，读数后不得改。

### 5.4 反事实口径

- entry = 财报后首个常规交易日的 **09:35 ET** 价格（避开开盘集合竞价噪声；
  与现有 entry-confirm 的 09:35 观察窗一致）；
- 主口径：当日收盘 / entry - 1；次口径：次日收盘 / entry - 1；
- 报告 MFE/MAE、左尾率（≤ -2.5%）、点差/流动性分桶；
- 不合成止损作为主判据（避免用退出规则混淆“门的机会成本”）；次口径可套用
  现有 2.5% early tight SL 估算净额，但必须标注为“含止损反事实”；
- 费用沿用现有 fee 口径，报扣费净 bps。

### 5.5 预注册裁决

- **样本门**：≥10 个 ET 交易日且 ≥20 个干净前瞻样本。
- **毕业候选**（仅提请另立 RFC，不自动生效）：
  1. 按日聚类 `fwd_t1` 净 bps 均值 > 0 且中位 > 0，`t ≥ 2.0`，效应 > MDE80；
  2. 左尾率不高于现有已执行 cohort + 5pp；
  3. 点差/流动性条件通过率 ≥80%，无 OTC/低价股；
  4. 误杀核算：被豁免的增量亏损净额 < 增量盈利净额，且两类都列 n/净额；
  5. UE 预筛 + playbook 两层均已记录，不允许只改一层。
- **失败/不显著**：维持 low_atr 门；产物标 `GATE_VALIDATED` 或 `INCONCLUSIVE`；
  不得因“样本不够”无限续采。
- 死线：2026-11-30 或 n≥20，先到者为准。
- 任何放宽最早 11 月，且须 RFC + 用户批准；10 月 C1 名额已被 RFC-153 占用。

---

## 6. 实施阶段与回滚

### Phase 0A（数据补全，零交易行为）
- 新增 `app/event_stream/event_reaction.py` + `event_reaction` 接线 + brief v8；
- 单测/契约测试；不改变 surfaced 的准入、不改变 DA 的方向判断规则；
- 回滚：配置 `universe_expansion.event_reaction_enabled=false` + `brief_template_version`
  回 v7；或删除接线（纯数据，无数据迁移）。

### Phase 0B（Shadow A，只读）
- 落 `ue_event_reaction_shadow.jsonl` + 评估器；
- 回滚：停评估 cron / 删产物，不影响生产。

### Phase 0C（Shadow B，只读）
- UE 预筛与 playbook 门各加记录钩子；
- 回滚：`earnings_gap_low_atr_shadow_enabled=false`；无生产行为。

### Phase 1（裁决）
- 按 §4.3/§5.5 读数；只产出提案；另立 RFC + 用户批准后才改门。

### Phase 2（若批准）
- 仅在获批的窄条件下放宽两层门；一行配置回滚；继续影子对照。

---

## 7. 验收与测试

- 单元：event_time 解析、ET 时段（含半日市/节假日）、DST、age_h、同日收盘、
  次日盘前/开盘、pending、无 K线、多标的、future event → None。
- 契约：`EVIDENCE_SCHEMA` 含 `event_reaction`；旧 evidence 无法字段仍可读；
  `_classify_evidence_citation` 保留 reaction；surfaced 输出含
  `evidence_matched_event_reaction`；quality_shadow/health 字段可读。
- 影子：合成样本验证 A1–A5 分桶、费用、左尾、t/MDE；无生产写入。
- 回归：现有 315 定向测试 + 外部 DA 28 测试全绿；扫描延迟增量 ≤2s（K线缓存后）；
  brief token 增量 ≤5%（只加短行，最多 3 标的）。
- 观测：新字段覆盖率（有 event_time 的 fact 中 `age_h` 非空比例）、
  `reaction_basis` 分布、`pending/unknown` 原因分布。

---

## 8. 开放问题（需用户裁决/确认）

1. **反应参照**：Phase 0 用“事件前最后收盘 vs 事件后首个可得价格”；是否接受
   盘中事件的一阶近似（日线包含事件前漂移），还是首版就用 1m bar 精算？
2. **age 阈值**：Shadow A 的 12h 是建议值；6h/24h 哪个更符合事件驱动持仓？
3. **gap/rvol/流动性阈值**：4% / 3x / $10 / $50M / 50bps 是否合适？
4. **Shadow A 前瞻口径**：复用 `quality_shadow` T+1，还是统一用“次日开盘 entry”的
   确定性口径？两者不可混读。
5. **C1**：确认 Phase 0 不占 C1；未来若 Shadow B 毕业，是否接受“最早 11 月”？
6. **是否在 Phase 0 就把 `age_h` 写进 DA prompt**：本 RFC 建议写（信息补全），
   但不加“旧闻禁做”的结论规则；确认是否同意。

---

## 9. 实施记录（2026-10-21）

**Phase 0A（数据补全，已实施）**:

- 新增纯函数模块 `app/event_stream/event_reaction.py`（`event_reaction_v1`）；
  `event_meta_v1` 未改动，旧事件无需回填。
- `scripts/llm_universe_scan.py`：`EVIDENCE_SCHEMA` 增加 `event_reaction`；
  `event_stream` / `sec_8k` / `sec_13dg` / `news_headline` 四条 clue 路径均附加；
  surfaced 增加 `evidence_matched_event_reaction` / `evidence_event_reaction`；
  brief 渲染 `事件后: HIMS +3.77% (同日收盘, 23.2h前)`；
  `BRIEF_TEMPLATE_VERSION` → `ue_brief_v8`，`PROMPT_VERSION` 保持 v2.15。
- 配置 `universe_expansion.event_reaction_enabled: true`（回滚 false）。
- 事件后反应使用现有 `MarketDataRouter.get_daily_bars(count=40)` 与批量报价，
  进程内按 symbol 缓存；报价懒取上限 30 个 symbol/轮（避免事件多时卡扫描）。
- 下游：外部 DA candidate/prompt/raw 增加 `ue_event_reaction`；
  `quality_shadow.py` 增加 `event_age_h`/`post_event_move_pct`/`reaction_basis`/
  `reaction_source`/`reaction_reason`；`ue_da_chain_health.py` 增加
  `event_reaction_coverage`/`reaction_basis`/prompt 实收读数。

**验证**:

- FTSeek 定向回归 **458 passed**；外部 DA **28 passed**；`py_compile` 通过。
- 09:15 ET premarket scan 实盘已用 `ue_brief_v8`；surfaced 9 只中
  event_reaction 覆盖 4 只（3 个 `same_day_close`、1 个 `same_session_premarket`）。
  该批 SPCX/TSLA 因当时 quote cap=10 显示 `pending`；cap 已提到 30，下一轮生效。
- 08:02 ET 回放：HIMS `+3.77% (same_day_close, 23.2h前)`、BAX `-1.92% (22h前)`、
  CBRE `+2.65% (18.7h前)`、NFLX `-5.98% (next_premarket, 16h前)`。
- 09:20 ET live brief 重建：SPCX/TSLA/BAX/CBRE 均有反应行；扫描总耗时仍在
  30 分钟 timeout 内（live 重建约 39s，含全部材料抓取）。

**未生效/未做**:

- 2026-10-21 21:30 CST（09:30 ET）已 `systemctl restart ftseek-us`
  （PID 2924305）+ 启动冒烟 7/7；`quality_shadow.py` 新代码已入进程。
  4 个在途仓 ASML/MSFT/RIVN/SPCX 已重新订阅。
- Shadow A/B 仅预注册，未启用；`low_atr`、chase cap、routing、仓位、止损未改。
- Phase 0A 不占 OKR-2026-10 C1 名额。

## 10. 变更记录

- 2026-10-21 v1：初稿。依据 10/21 08:00 ET da_feed review（HIMS/BAX/CBRE/NFLX），
  定义 `event_reaction_v1` 与两个只读影子；Phase 0 不改交易行为，C1 不占用。
- 2026-10-21 v1.1：Phase 0A 实施（event_reaction_v1 + brief v8 + DA/quality/health
  透传）；458+28 测试通过；09:15 ET premarket scan 实盘验证。Shadow A/B 仍未启用。
