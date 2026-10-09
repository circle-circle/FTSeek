---
title: 事件驱动的盘前/盘中/盘后采集-推理-交易链路设计
date: 2026-10-09
status: 方案 U 已采纳并已落地（§14 P0 前置 + §15 Phase 0 + §16–§18 Phase 1/1.5/1.6 + §19 Phase 1.7 UE 接入，§20/§22 两轮实测通过，§21 DA bridge 事故已修复恢复）· §23 主线程落地复查：记录全部属实 · §24 修复批已落地：F1 发现源多 query/term 化 + F2 EDGAR 取链 + F4/F5/F6 小修，生产 cron 已自动生效 · 通道 B / live 未开启
trigger: 用户要求以 SpaceX/FCC/Grain 新闻为示例，泛化设计一条覆盖盘前、盘中、盘后的信息采集、关联推理与可验证交易链路，并强调如无必要不重复造轮子
---

# DEB-20261009 · 事件驱动采集-推理-交易链路

## 0. 结论（待裁决）

**当前系统不具备“第一时间获知 → 结构化关联推理 → 分时段交易执行”的完整链路；但不应重启旧 `news_v2`，也不应新建第二条自动交易主管线。**

推荐路线：

1. 新建轻量 **Event Stream**，职责限定为多源采集、原始事实存储、事件归一化、实体/传导图、事件信号与影子执行记录。
2. 复用现有 `SourceAdapter`、`UnifiedNewsItemV2`、去重/相关性、LLM 分类、风险与执行护栏，不重写采集和交易基础设施。
3. 事件存储使用 append-only SQLite/WAL 起步，不引入 Kafka/Redis；只有在单机写入或消费者规模明显不足时再升级。
4. 推理分两层：确定性事件模板先行，LLM 作为异步校验、抽取补全、反证与解释器，不放在首延迟关键路径。
5. 交易分四级推进：`observe → shadow → sim → live`。盘前/盘后先只做影子成交记录，零真实订单。
6. 自动下单、出场与持仓管理继续唯一走 Playbook；禁止新建 23 小时平行交易主管线。实盘接入必须另行显式裁决。

本 DEB 是设计稿，落地前须用户逐 Phase 裁决。

## 1. 背景

### 1.1 触发样本

用户给出的样本叙事为：SpaceX 宣布收购覆盖全美的 800 MHz 低频段频谱，获 FCC 授权部署 15,000 颗直连手机卫星，AT&T/T-Mobile/Verizon 盘后齐跌逾 6%。

已核验事实：

- FCC order `DA-26-1078A1`：
  - URL：`https://docs.fcc.gov/public/attachments/DA-26-1078A1.pdf`
  - adopted/released：`2026-10-06`
  - grants-in-part / defers-in-part SpaceX authority for 15,000 NGSO satellites
  - 官方频率口径为 AWS-3 unpaired、PCS G Block、AWS-H Block、2 GHz MSS；样本叙事中的 “800 MHz” 未在已核验官方文本中得到同等口径，不能直接作为交易事实。
- 已观察到的最早索引时间：
  - `2026-10-07 02:06 UTC`：PCMag 覆盖 FCC 15,000 卫星授权
  - `2026-10-08 20:17 UTC`：Yahoo/Google News 索引 Grain/SpaceX 消息
  - `2026-10-08 20:19 UTC`：Reuters
  - `2026-10-08 20:34 UTC`：Investing.com
  - `2026-10-08 20:43 UTC`：CNBC
- 主源访问受限：Grain Management HTTP 429、SpaceX update page HTTP 403、Reuters HTTP 401。
- Yahoo extended-hours 粗读数：`T -6.86%`、`TMUS -7.13%`、`VZ -5.89%`。该读数只能作行情核对，不能替代券商行情。
- 未核验、不得入交易事实层：`$17B`、`$93.5B cash`、`12M subscribers`。交易金额公开口径为 terms undisclosed，媒体报道估算约 `$8B`。

该样本暴露的不是“缺一个新闻 API”，而是链路断点：

1. 没有官方源与新闻 wire 的分层采集。
2. 没有 raw artifact 与 claim 级溯源。
3. 没有公司/监管/标的/竞争关系传导图。
4. 没有“事件模板 → 影响标的 → 方向/时间窗”的推理。
5. 没有盘前/盘中/盘后统一的信号路由。
6. 没有影子成交与事件后收益评估，无法判断是否值得实盘。

### 1.2 当前系统事实

已核验配置与代码状态：

- `news_v2.enabled: false`，NewsV2 已退役，零有效消费者。
- `news_v2.skip_market_hours: true`，事件发生时采集循环不会工作。
- `data/rsshub_us/news_us.db` 最后更新为 `2026-10-05T15:15:06`，早于事件。
- 现有源没有 FCC、SpaceX、Grain、X 或一手公司页 watcher。
- `news_v2.immediate_scan.enabled: false`、`require_trading_hours: true`、`min_urgency: IMMEDIATE`，盘前/盘后事件无法触发。
- `llm_pushes.after_hours_scan_enabled: false`。
- `app/core/scheduler_core.py` 的 `_on_news_v2_immediate` 只做 force-poll，不生成结构化事件信号，也不下单。
- `news_relevance.entities` 为空；watchlist 有 `US.VZ`，缺 `US.T`、`US.TMUS`。
- 没有电信竞争、频谱交易或监管审批传导模板。
- 主 Futu broker 路径以 `Session.RTH` 为主，不能直接承担盘后事件执行。
- `scripts/extended_hours_entry_executor.py` 已有 `Session.ETH`、券源检查、限价单、撤单生命周期与安全护栏，但当前是 earnings/momentum 专用，不是通用事件执行器。
- `data/llm_scan_state.json` 上一次盘后扫描为 `16:01 ET`，早于事件 `16:17 ET`；调度器在约两小时内不会再次扫描。

### 1.3 与既有设计的关系

- `DEB-20260917-extended-hours-auto-trading.md`：延长时段执行必须治理先行，不能因事件速度需求直接放开。
- `DEB-20260918-extended-hours-ssot-and-observability.md`：SSOT 与全链路可观测性前置。
- `DEB-20260919/20260920` 延长时段系列：价格、点差、券源、时间窗、订单生命周期与 SIM-only 边界不得绕过。
- `DEB-20261009-unified-session-scan-dispatch.md`：市场时段/扫描调度应走统一 resolver 与 dispatcher；Event Stream 消费该口径，不另造市场时段语义。
- `AGENT.md` 架构约束：Playbook 是唯一下单/持仓管理管线。

## 2. 目标

1. **早发现**：对 Tier 0/Tier 1 事件形成可测量的 `published_at` 与 `first_seen_at` 差值，按源统计延迟。
2. **可溯源**：每条事件保留原始 artifact、URL、抓取时间、HTTP 状态、内容 hash、来源 tier、corroboration 状态与推理 lineage。
3. **可泛化**：不是针对 SpaceX 写死，而是支持监管审批、频谱/资产交易、竞争者进入、财报指引、诉讼、产品发布等模板。
4. **可推理**：输出结构化影响标的、方向、时间窗、置信度、反证条件与事件过期时间。
5. **分时段交易**：盘前/盘中/盘后统一采集，但执行能力分级：shadow → sim → live。
6. **可评估**：每个事件模板、每个来源、每个 session 都有延迟、准确率、期望收益与滑点统计。
7. **可回滚**：所有新增能力独立开关，默认关闭，一行配置可回滚。

## 3. 非目标

- 不重启 `news_v2`，不把它当生产事件链路。
- 不新建 Kafka/Redis 流平台，不先做分布式架构。
- 不新建平行自动交易主管线。
- 不绕过 Playbook、风险闸、券商能力检查或盘后执行护栏。
- 不把 X firehose、付费 wire 或私有数据源默认加入；涉及费用、授权与条款须单独裁决。
- 不把未证实媒体数字当作已确认交易事实。
- 不因单一样本表现开启实盘。

## 4. 总体架构

```text
Source Watchers
  → Raw Event Store
  → Dedup / Corroboration
  → Event Normalizer
  → Entity Graph
  → Deterministic Reasoner
  → LLM Verifier
  → Event Signal Router
  → Shadow Executor
  → Evaluation
  → Playbook (live, 后置且需显式裁决)
```

### 4.1 采集层

#### 4.1.1 Source tier

| Tier | 类型 | 示例 | 用途 | 目标 |
|---|---|---|---|---|
| T0 | 官方/一手 | FCC、SEC EDGAR、公司 IR、交易所公告 | 事实锚点 | 15–30s 轮询或官方 API |
| T1 | wire/权威媒体 | Reuters、Bloomberg、CNBC、AP | 快速确认与扩展事实 | 10–60s |
| T2 | 聚合/索引 | Google News RSS、Bing News、NewsNow、RSSHub | 发现与竞速对照 | 30–120s |
| T3 | 社媒/论坛 | X、Reddit、StockTwits | 线索与舆情，不入事实层 | 需另行授权与治理 |

T2 可以先发 `early_warning`，但交易信号必须满足 corroboration gate。T3 默认不做交易输入。

#### 4.1.2 Source watcher

复用 `app/news_v2/sources/base.py` 的 `SourceAdapter`：

- 保留失败隔离、健康监控、间隔控制。
- 扩展返回 metadata：`source_tier`、`fetch_started_at`、`fetch_completed_at`、`http_status`、`payload_hash`、`raw_artifact`。
- 每个 watcher 只负责抓取与保存，不做交易推理。

建议新增：

```text
app/event_stream/sources/fcc_edocs_source.py
app/event_stream/sources/sec_edgar_source.py
app/event_stream/sources/company_ir_source.py
app/event_stream/sources/google_news_source.py
app/event_stream/sources/wire_source.py
```

Phase 0 先做源能力矩阵，不预设所有官方页都有稳定 RSS/API。

#### 4.1.3 降级与镜像

对 403/429/401 的主源：

1. 保存失败状态，不允许静默失败。
2. 使用官方 RSS/API、公开镜像或搜索索引作为 discovery fallback。
3. fallback 只能产生 `UNVERIFIED` 状态，不能冒充 T0。
4. 后续异步重试主源，成功后升级 corroboration。
5. 所有 fallback 均须记录，禁止事后覆盖原始来源。

### 4.2 事件存储

#### 4.2.1 存储选型

第一阶段使用 SQLite + WAL：

- 单机已有大量 SQLite 使用经验。
- 事件量初期远小于流平台必要规模。
- 便于审计、回放与备份。
- append-only 表天然满足溯源需求。

暂不引入 Kafka/Redis。升级触发条件须另行定义，例如持续写入超过单机 SQLite 可承受量、多进程消费者一致性成本过高、回放延迟影响生产。

建议路径：

```text
~/.hermes/ftseek/data/event_stream.db
```

实际路径以配置解析为准，不硬编码用户目录。

#### 4.2.2 数据表草案

`raw_events`：

- `raw_id`
- `source_id`
- `source_tier`
- `source_url`
- `source_published_at`
- `fetch_started_at`
- `fetch_completed_at`
- `first_seen_at`
- `http_status`
- `payload_hash`
- `raw_artifact`
- `parse_status`
- `error`

`canonical_events`：

- `event_id`
- `event_type`
- `event_time`
- `first_authoritative_seen_at`
- `headline`
- `summary`
- `facts_json`
- `status`: `UNVERIFIED / CORROBORATED / CONTRADICTED / RETRACTED / STALE`
- `confidence`
- `reasoning_version`

`entities`：

- `entity_id`
- `canonical_name`
- `entity_type`: `COMPANY / REGULATOR / ASSET / PERSON / PRODUCT`
- `ticker`
- `aliases`

`entity_relations`：

- `subject_entity_id`
- `relation`
- `object_entity_id`
- `weight`
- `valid_from`
- `valid_to`
- `source`

`signals`：

- `signal_id`
- `event_id`
- `symbol`
- `direction`
- `strength`
- `confidence`
- `horizon`
- `expiry_at`
- `gate_result`
- `lineage`

`shadow_executions`：

- `signal_id`
- `symbol`
- `session`
- `snapshot_at`
- `quote`
- `spread`
- `hypothetical_order`
- `hypothetical_fill`
- `markout`

`source_health` 与 `consumer_cursors`：

- 源失败、暂停、恢复、延迟统计。
- 各消费者读取位点，避免重复消费。

### 4.3 归一化与事实抽取

原则：**claim-level，不是 article-level sentiment**。

每条事件解析为：

```json
{
  "event_type": "SPECTRUM_TRANSACTION",
  "facts": {
    "regulator": "FCC",
    "acquirer": "SpaceX",
    "seller": "Grain Management",
    "asset": "spectrum licenses",
    "bands": [],
    "coverage": "US",
    "authorization_status": "grants_in_part",
    "satellite_count": 15000,
    "price_confirmed": false
  },
  "claims": [
    {"text": "...", "confidence": 0.0, "source_tier": 2, "verified_by": []}
  ]
}
```

规则：

- 未验证数字进入 `claims`，不进入 `confirmed facts`。
- 数字、单位、币种、频率、带宽、coverage 必须分开存储。
- 官方文本优先级最高。
- 冲突事实必须标记，不允许用后到报道静默覆盖。
- 事件 ID 使用稳定内容/实体/时间 hash，不用数据库自增。

### 4.4 实体与传导图

最小图不需要图数据库，SQLite 表 + YAML 种子数据即可。

静态关系：

- 公司 aliases / tickers。
- 行业分类。
- 竞争关系。
- 供应链关系。
- 监管者与行业关系。

动态关系：

- `ACQUIRES_ASSET`
- `RECEIVES_AUTHORIZATION`
- `COMPETES_WITH`
- `SUPPLIES`
- `CUSTOMER_OF`

示例：

```text
SpaceX/Starlink
  → RECEIVES_AUTHORIZATION(FCC)
  → ACQUIRES_ASSET(spectrum)
  → COMPETES_WITH(T, TMUS, VZ)
  → SUPPLIES_OR_BENEFITS(satellite infrastructure)
```

实体解析必须输出置信度；无法确认公司或 ticker 时不能生成交易信号，只能生成观察记录。

### 4.5 推理层

#### 4.5.1 确定性模板优先

首批模板：

- `REGULATORY_APPROVAL`
- `REGULATORY_DENIAL`
- `SPECTRUM_TRANSACTION`
- `COMPETITOR_ENTRY`
- `MATERIAL_GUIDANCE_CHANGE`
- `M&A`
- `FDA_APPROVAL`
- `LITIGATION`
- `MACRO_SHOCK`

每个模板预注册：

- 必需 facts。
- 允许的置信度区间。
- 受益/受损传导路径。
- 默认时间窗。
- 反证条件。
- 信号过期时间。

示例：`COMPETITOR_ENTRY`

```text
trigger:
  new entrant obtains critical asset/authorization
transmission:
  incumbents: negative
  entrant suppliers: positive
  incumbents' suppliers: uncertain
direction:
  incumbent: SHORT candidate
  supplier: LONG candidate
gates:
  corroboration >= T0 or 2*T1
  tradable liquidity
  borrow availability
  spread within cap
```

#### 4.5.2 LLM verifier

LLM 不决定首信号，只做：

1. JSON schema 校验后的抽取补全。
2. 冲突事实识别。
3. 反证与第二路径推理。
4. 解释性摘要。
5. 事件类型不确定时的分类建议。

约束：

- 输入新闻内容视为不可信文本，防 prompt injection。
- 输出必须通过 schema 与枚举校验。
- 不得输出未在 facts 中存在的数字。
- 超时则使用确定性结果，不阻塞首延迟。
- 成本与 token 计入现有 LLM budget。

### 4.6 信号路由

信号状态机：

```text
CREATED
→ ENTITY_RESOLVED
→ CORROBORATED
→ GATED
→ ROUTED
→ SHADOW_RECORDED
→ EVALUATED
```

路由规则：

| Session | Phase 1–4 行为 | 后续可能行为 |
|---|---|---|
| premarket | 采集 + 推理 + shadow | SIM，需裁决 |
| regular | 采集 + 推理 + immediate scan / Playbook candidate | Playbook，需现有闸门 |
| afterhours | 采集 + 推理 + shadow | SIM，需裁决 |
| closed/holiday | 采集 + 推理 + scheduled shadow | 不交易 |

重要边界：

- Event Stream 不直接下单。
- RTH 只能把事件候选送入现有扫描/Playbook 候选路径。
- 盘前/盘后先记录假设成交。
- 信号必须有 `expiry_at`，过期后不得追单。

### 4.7 影子执行

影子执行不做真实订单，只记录：

- `first_seen_at`
- 信号生成时间。
- 订单拟提交时间。
- `+0s/+1s/+5s/+30s/+60s/+300s` quote snapshot。
- bid/ask、mid、spread、成交量、quote age。
- 假设限价单、可成交性、部分成交假设。
- borrow availability。
- 滑点、手续费、融资成本、税前/税后收益。
- 次日 open、5 分钟、30 分钟、收盘 markout。

每条 shadow 记录必须能回放到：

```text
raw source → canonical event → entity graph
→ reasoner output → signal → gates → hypothetical execution
```

### 4.8 SIM 与 live

SIM 阶段：

- 复用 `scripts/extended_hours_entry_executor.py` 的护栏思想，抽取为通用模块。
- 继续使用 `Session.ETH`、限价单、券源检查、点差 gate、订单生命周期、撤单与审计。
- 不复制现有 Playbook 风控，而是调用同一类 gate。

Live 阶段：

- 仅通过 Playbook entry/exit 主管线。
- 独立配置，默认 `false`。
- 须另行用户显式批准。
- 须预登记标的范围、名义本金、频率、订单类型、日损上限、kill switch 与回滚方式。

## 5. 不重复造轮子清单

| 需求 | 复用 | 不新建 |
|---|---|---|
| 源接口与健康监控 | `SourceAdapter` | 自写 source loop |
| 初始统一模型 | `UnifiedNewsItemV2` | 新新闻对象体系 |
| 去重 | `DedupEngineV2` | 新 SimHash |
| 相关性 | `RelevanceFilter` | 新关键词 relevance |
| LLM 分类 | `news_classifier` feature | 新 LLM 客户端 |
| 事件信号形态 | `CatalystEvent` / `EventSignalGenerator` 概念 | 第二套信号协议 |
| 市场时段 | 既有 phase / 后续 SessionResolver | 第二套市场时段语义 |
| 风险闸 | 现有 risk/kill switch | 平行风控 |
| 执行 | Playbook / DualBrokerAdapter | 平行自动下单 |
| 盘后订单护栏 | extended-hours executor 经验 | 新旁路下单脚本 |
| 推送 | `app/push/` hub | 直连 notifier/webhook |

### 5.1 外采能力现状与边界

本地 Codex skills 中没有可直接复用的新闻采集、事件流或交易链路 skill；现有 skill 只覆盖图像生成、OpenAI 文档、review、skill 创建与安装。

| 类别 | 可复用选项 | 定位 | 边界 |
|---|---|---|---|
| 仓库已有采集 | `SEC EDGAR`、`RSSHub`、`Finnhub`、财经媒体 adapter | 先复用 `SourceAdapter` 接口与失败隔离 | 不是完整事件流，且 `news_v2` 已退役 |
| 开源采集/编排 | RSSHub、changedetection.io、Huginn、n8n | 页面/RSS/路由采集与工作流编排，可做外挂采集 | 延迟与可靠性参差；不是一手事实层，也不负责交易推理 |
| 官方一手 API | SEC EDGAR、FCC EDOCS/ECFS、Federal Register、FDA、BLS/FRED、公司 IR/PR wire | T0 事实锚点 | 需逐源审计 API、时间字段、频率限制与条款 |
| 商业事件 feed | Reuters/LSEG、Bloomberg、Dow Jones、FactSet、RavenPack、Dataminr、Benzinga、Polygon、GDELT | 低延迟 wire、实体事件、情绪与舆情 | 采购成本、授权、再分发与延迟均须合同核验 |
| 社交渠道 | X filtered/stream API、Reddit API、StockTwits API | early warning、情绪、讨论热度 | 必须使用授权 API；默认只作 T3 线索，不作交易事实 |

结论：**没有一个开源项目或商业产品同时解决一手采集、事实归一、实体传导、分时段推理与交易执行。** 合理路线是把成熟采集器和商业 feed 全部放在 FTSeek `SourceAdapter` 后面，Event Store、corroboration、reasoner、signal gate 与 Playbook 执行继续由本系统统一治理。

## 6. 分阶段实施

### Phase 0：源与延迟审计（无行为变更）

产出：

- 源能力矩阵：API/RSS/page、许可、频率限制、时间字段、阻塞状态。
- 延迟基准：`source_published_at`、`first_seen_at`、earliest observed。
- 主源失败 fallback 策略。
- 事件样本标注集。

验收：

- 每个候选源都有可访问性、授权与时间字段结论。
- 明确 Google News 只能作 Tier 2 discovery。
- 明确付费 wire/X 是否引入，交用户裁决。

### Phase 1：Event Store + observe 采集

新增：

- `app/event_stream/models.py`
- `app/event_stream/store.py`
- `app/event_stream/source_registry.py`
- `app/event_stream/ingest_service.py`
- `app/event_stream/sources/*`

配置草案：

```yaml
event_stream:
  enabled: false
  mode: observe
  store_path: ~/.hermes/ftseek/data/event_stream.db
```

验收：

- raw artifact append-only。
- SQLite WAL 与并发读写测试通过。
- 源失败不互相传染。
- `published_at` 与 `first_seen_at` 全链路保留。
- 不修改 `news_v2`。

### Phase 2：归一化与实体图

新增：

- `app/event_stream/normalizer.py`
- `app/event_stream/entity_resolver.py`
- `app/event_stream/entity_graph.py`
- `config/event_templates.yaml`
- `config/entity_seeds.yaml`

验收：

- 事件 schema 校验。
- claim 与 confirmed fact 分离。
- entity resolution 有置信度。
- 冲突事实可见。

### Phase 3：推理器

新增：

- `app/event_stream/reasoner/deterministic.py`
- `app/event_stream/reasoner/templates.py`
- `app/event_stream/reasoner/llm_verifier.py`

验收：

- 首批模板可跑样本回放。
- LLM 失败不阻塞确定性路径。
- 输出 direction、symbol、confidence、horizon、expiry、反证条件。

### Phase 4：信号路由

新增：

- `app/event_stream/signal_router.py`

修改：

- `app/core/scheduler_core.py` 仅挂接事件候选路由，不直接下单。

验收：

- 盘前/盘后事件不再因 `skip_market_hours` 丢失。
- RTH 事件走 immediate scan 或 Playbook candidate。
- 所有 reject reason 落库。

### Phase 5：影子执行

新增：

- `app/event_stream/shadow_executor.py`

验收：

- 零真实订单。
- 多时点 quote 与 markout 完整。
- spread、quote age、borrow、可成交性、滑点可统计。
- lineage 可从原始 artifact 回放到假设成交。

### Phase 6：评估与毕业

新增：

- `scripts/event_stream_shadow_eval.py`
- `scripts/event_stream_latency_report.py`

指标：

- Source latency P50/P95。
- Earliest-source hit rate。
- Entity resolution precision/recall。
- Event classification precision/recall。
- Signal PPV/NPV。
- Fill feasibility。
- Slippage。
- Post-cost expectancy。
- Session 分层收益。
- Event class 分层收益。
- 按日/事件聚类后的统计，避免同一事件多标的重复计样本。

毕业门建议：

- 每个事件类至少 50 个有效样本；低频类可另行预注册。
- source tier 与 corroboration gate 稳定。
- entity/classification 错误率低于预注册阈值。
- 扣除点差、滑点、费用后正期望。
- 盘前/盘后分开评估，不得用 RTH 结果外推。
- 无未解释的失败撤单或行情缺失。

## 7. 安全与治理

### 7.1 事实安全

- T2 只能 early warning，不能单独触发交易。
- 数字未验证不进入 facts。
- 冲突、撤稿、更正必须传播到 signal。
- 报道时间与官方发布时间不得混用。
- 同一事件多标的按 event cluster 统计，不得当作独立样本。

### 7.2 数据合规

- 付费源、wire、X firehose 先确认授权、费用、重分发和存储限制。
- 不绕过登录墙或付费墙。
- 官方公开文档优先。
- 尊重 robots/ToS 与 rate limit。

### 7.3 执行安全

- quote 不可得、quote age 超限、spread 超限、borrow 不足、风险状态异常均 fail-closed。
- 事件信号只作为候选，不绕过现有扫描、风险、Playbook。
- 盘前/盘后默认 shadow，不允许 silent live。
- live 开关与 Event Stream 总开关分离。

### 7.4 可观测性

必须输出：

- source health。
- fetch latency。
- parse failure。
- dedup hit。
- corroboration state。
- entity resolution confidence。
- reasoner version。
- LLM verifier result。
- signal gate reject reason。
- shadow markout。
- event lineage。

新增推送必须走 `app/push/` hub，禁止直连 notifier。

## 8. 测试矩阵

| 测试 | 目的 |
|---|---|
| source adapter contract | 统一字段、失败隔离、健康监控 |
| raw store append-only | 不可覆盖、不可丢失 lineage |
| dedup/corroboration | 同事件多源聚合，不误合并不同事件 |
| fact schema | 未验证数字不进入 confirmed facts |
| entity resolver | ticker/别名/竞争关系正确 |
| reasoner template | 传导路径、方向、时间窗、反证条件 |
| LLM verifier | schema、timeout、prompt injection 防护 |
| signal router | session 分层与 fail-closed |
| shadow executor | 零订单、多时点 markout |
| latency report | published/first_seen 口径一致 |
| evaluation | 按事件聚类、扣除成本 |

## 9. 待用户裁决

1. 是否批准 Phase 0 源与延迟审计。
2. 是否引入付费 Tier 1 wire / X firehose，或先用公开源。
3. 是否批准 Phase 1 observe-only Event Store。
4. Event Stream 是否与统一 SessionResolver DEB 合并为同一实施批次，还是保持独立分批。
5. 影子样本量和毕业阈值是否接受本 DEB 建议。
6. 盘前/盘后是否永远禁止 live，或达到毕业门后允许另行签署。

## 10. 变更记录

- 2026-10-09 v1：初版设计稿。只新增本文档，未修改生产代码、配置、crontab 或 Hermes 任务。
- 2026-10-09 v1.1：追加 §11 主线程只读复查（实测复核 + 新发现 + 待裁项建议）。§1–§9 正文逐字未动（含已被 §11 推翻/更正的 §1.2 第 7、12 条与 §5 幻影引用，改法见 §11，待正文修订窗口统一处理）。
- 2026-10-09 v1.2：追加 §12 用户方向输入与方案 U（UE 融合路线）。**§12 与 §4.6–§4.8、§6、§9 冲突处以 §12 为准**；§1–§11 正文仍逐字未动。
- 2026-10-09 v1.3：Codex 复核 §11–§13，实跑探针与验证器；修正当前 `event_symbols` 产物数为 59（10/59）与 `news_quality.db` “当前仍在写”的表述（实为退役链遗留死表）。未修改生产代码、配置、crontab 或交易行为。
- 2026-10-09 v1.4：用户裁“按建议执行”。落地 §14 P0 前置：双向监管/频谱词表、telecom 传导、UE/gen_event_symbols 复用 sector_mapping、RSSHub 空结果陈旧告警、工作日采集 cron。未新建 Event Stream，未开启通道 B，未修改交易行为。
- 2026-10-09 v1.5：追加 §23 主线程落地复查（§14–§21 全量实测复核；落档时编号 §22，后因 §22 被 21:10 premarket 实测占用而重编号）。复查动作仅 2 处 except:continue 棘轮清剿（`event_stream_ue_check.py` / `da_bridge_dingtalk_push.py` 坏行计数 + stderr 留痕，零控制流变更），其余零改动。新发现 F1–F10 见 §23.2。另补登记：§15–§21 落地批当时未写变更记录，本节一并追认。
- 2026-10-09 v1.6：用户裁「请修复」。落地 §24 修复批：F1 发现源多 query/term 化（配置入 `event_stream.*`）+ F2 EDGAR href 取链 + F4 clue 路径同源 + F5 dedupe 时间口径 + F6 ticker 连字符守卫；F7 复核后撤回（非问题）。生产 cron 已自动生效，实测证据见 §24.2。


## 11. 主线程复查（Claude · 2026-10-09 · 只读实测）

> 复查人：Claude（主线程）。方法：不信任本文档描述，对 §1.2「当前系统事实」12 条与 §5 复用清单 11 行逐条活体实测（只读：grep / 文件读 / 配置解析；未触发 OpenD，未修改任何文件）。
> 本节**只追加**，§1–§10 正文逐字未动。所有证据命令见 §11.7 E 表，逐条可跑。

### 11.1 总评

| 维度 | 判定 |
|---|---|
| §3 非目标 + §7 治理边界（不重启 news_v2 / 不建新交易主管线 / Playbook 唯一执行 / 四级推进） | ✅ **本 DEB 最有价值的部分，完全认可**，与 OKR 策略隔离纪律一致 |
| §1.2 十二条系统事实 | ✅ **9 条实测为真**；⚠️ 1 条不精确（#7）；❌ 2 条语义误读（#4 半误读、#12 误读，见 11.2） |
| §5 复用清单 | ⚠️ 10/11 行成立；**`EventSignalGenerator` 为幻影引用**（全仓 grep 0 命中）；**重大遗漏：`app/catalyst/` 同轴引擎未盘点**（11.4 N1/N2） |
| 立项必要性 | ✅ 成立：链路断点六条中五条实测为真；且 #12 修正后论点**更强**（盘后扫描是整体关停，不是"两小时内不扫"） |
| 最大风险 | 不是技术方案，是**资产盘点不全**：Phase 2/3 若不与 `app/catalyst` 做取舍，会长出第二套并行事件引擎——恰是 §3 自己禁止的重复造轮子 |

### 11.2 §1.2 逐条实测复核

| # | 文档声称 | 实测 | 判定 |
|---|---|---|---|
| 1 | `news_v2.enabled: false`，已退役零消费者 | ✅ `config/unified_config.yaml:870-871`；退役日志分支实证 `scheduler_core.py:686-687`（"产出零消费者, DEB-20260731 C2"） | ✅ 真 |
| 2 | `skip_market_hours: true` | ✅ `:889` | ✅ 真（但注意一级闸是 `enabled=false`，skip 是二级闸，叙述因果主次可再精确） |
| 3 | `news_us.db` 最后更新 2026-10-05T15:15 | ✅ mtime 实测一致（`10月 5 15:15`） | ✅ 真 |
| 4 | 现有源没有 FCC/SpaceX/Grain/X/一手公司页 watcher | 部分真：确无此五者；**但 `sec_edgar`（T0 官方源）adapter 已存在**：`app/news_v2/sources/sec_edgar_source.py`，配置面 `unified_config.yaml:908-910` 定义但 `enabled: false` | ⚠️ 半误读：§6 Phase 1 把 `sec_edgar_source.py` 列为"新增"，实际应是**迁移/适配既有件**；同类件还有 finnhub/rsshub_us adapter |
| 5 | `immediate_scan.enabled:false / require_trading_hours:true / min_urgency:IMMEDIATE` | ✅ `:891-894` 逐字一致 | ✅ 真 |
| 6 | `llm_pushes.after_hours_scan_enabled: false` | ✅ `:914` | ✅ 真 |
| 7 | `_on_news_v2_immediate` 只做 force-poll，不生成结构化事件信号，**也不下单** | 前半真（force-poll、无结构化事件信号）；**"也不下单"不精确**：`scheduler_core.py:2680-2684` docstring 自述链路终点是「现有信号扫描流水线 (因子/LLM/**风控/下单**)」——回调自身不下单，但它触发的 force_poll 进入的是**可以下单**的既有管线 | ⚠️ 不精确。对设计含义重要：RTH 时段「突发新闻→信号扫描→下单」接线**已存在**（仅被 enabled=false + require_trading_hours 闸住），Phase 4 是"接通并加闸"而非"新建通路" |
| 8 | `news_relevance.entities` 为空 | ✅ `:787` `entities: {}` | ✅ 真 |
| 9 | watchlist 有 `US.VZ`，缺 `US.T`/`US.TMUS` | ✅ 实测 `unified_config.yaml` 内联 watchlist 块共 **64** 个 code：VZ∈、T∉、TMUS∉ | ✅ 真 |
| 10 | 无电信竞争/频谱/监管审批传导模板 | ✅ 真；但注意 `catalyst.sector_mapping`（`unified_config.yaml:102`）已有**行业/板块传导**种子，可作 §4.4 实体图 YAML 种子的一部分来源 | ✅ 真（有可利用存量） |
| 11 | Futu 主路径 RTH 为主；`scripts/extended_hours_entry_executor.py` 已有 ETH 护栏但 earnings/momentum 专用 | ✅ 文件存在 | ✅ 真 |
| 12 | `data/llm_scan_state.json` 上一次**盘后扫描**为 16:01 ET，早于事件 16:17 ET，调度器约两小时内不会再扫 | ❌ **语义误读 + 现已不可复现**：① 当前文件内容为 `{"last_afterhours_run": "2026-10-09T04:01:05.207682-04:00"}`；② `scheduler_core.py:2372-2374 / 2450-2452 / 2585-2587` 实测 `_persist_ah(now)` 在 `run_after_hours_llm_scan` 返回后**无条件执行**，且 PRE_MARKET/AFTER_HOURS/OVERNIGHT 三个分支共用同一字段；③ `llm_pipeline.py:593-594` 实测 `after_hours_scan_enabled=false` 时**进门即返回**。⇒ 该字段记录的是"LLM 策略循环上次路过盘前/盘后/夜盘分支的时刻"，**不是"扫描发生过"**；在开关关停下根本不存在盘后扫描 | ❌ 误读。**修正后论点更强**：不是"两小时内不会再扫"，而是"盘后 LLM 扫描整体关停，任何时刻都不会扫"。§1.2 该条建议按此改写 |

### 11.3 §5 复用清单实测

| 行 | 判定 |
|---|---|
| `SourceAdapter`（失败隔离/健康监控/间隔控制） | ✅ `app/news_v2/sources/base.py:73`，`SourceHealthMonitor` + `fetch_safe` + `should_fetch` 三项能力实证存在 |
| `UnifiedNewsItemV2` / `DedupEngineV2` / `RelevanceFilter` | ✅ `models.py:48` / `dedup_v2.py:18` / `relevance_filter.py:18` |
| `CatalystEvent` | ✅ `app/catalyst/models.py:14`（event_id 内容 hash、event_type、severity、tickers、published_at 齐全） |
| **`EventSignalGenerator`** | ❌ **全仓 grep 0 命中，不存在**。§5 该行后半是幻影引用，须删除或改写为 `catalyst_debate`（真实存在的多模型辩论引擎） |
| `news_classifier` feature / `app/push/` hub / Playbook / DualBrokerAdapter | ✅ 存在（push hub 含 aggregator/channels/dedup/health/hub 五件） |

### 11.4 新发现（§1–§10 未覆盖）

- **N1：`app/catalyst/` 整模块是本文档最大的盘点遗漏。** 该模块已含 `event_classifier.py`（关键词粗筛→LLM 确认事件类型+关联标的+中英摘要）、`catalyst_debate.py`（多模型辩论）、`detector.py`、`notifier.py`、`postmarket_review.py`、`reverse_catalyst.py` 与 `sources/`（HKEX/CSRC 两手，已在 `scheduler_core.py:564-578` 接线，限 HK/CN）。其设计原则与本文档 §4.3/§4.5 高度同构（关键词 O(n) 粗筛滤 95%、`call_llm(feature="catalyst_classifier")` 路由、`sector_mapping` 传导）。当前 `catalyst.enabled: false`（`unified_config.yaml:87-88`）。
- **N2：catalyst 的输入依赖是已退役管线。** `event_classifier.py` docstring 自述「复用 NewsPipelineV2 输出（已去重+过滤的新闻）」。若 Event Stream 未来复用 catalyst，必须先把它从 news_v2 输入解耦、改吃 Event Store。**这决定 Phase 2/3 的复用边界，本文档只字未提。**
- **N3：Phase 2 开工前必须先出 catalyst 取舍结论**（三选一）：a) Event Stream reasoner 取代 catalyst，catalyst 走退役流程；b) catalyst 改造为 §4.5.2 LLM verifier 的实现载体；c) 明确分工（如 catalyst 管 HK/CN、Event Stream 管 US）。不定论 = 第二套事件引擎并行演化，违反 §3。
- **N4：`paths.watchlist: config/watchlist_us.json`（`unified_config.yaml:1038`）指向的文件不存在**（仅存于 `config/deprecated/`）；实际生效的是内联 `watchlist:` 块。与本 DEB 无直接冲突，但 Phase 2 实体种子若以 paths 键取 watchlist 会踩空。
- **N5：`last_afterhours_run` 字段语义已误导一次**（11.2#12）。任何未来消费该字段的设计（含本文档 §1.2 这类取证）都应注意它是"循环路过时刻"而非"扫描发生时刻"。

### 11.5 设计层面补充建议（并入对应 Phase 验收）

- **D1 时间戳纪律**：`first_seen_at/published_at/event_time/fetch_*` 一律 UTC ISO8601 带时区存储；禁 naive `datetime.now()`（仓内 naive-now 棘轮基线只降不升，见时钟病灶台账）。Phase 1 验收加一条「naive-now 棘轮不升」。
- **D2 非交易路径判交易日**：ingest/router 的交易日判定只许用纯本地 `shadow_tracker.US_MARKET_HOLIDAYS_2026`；`holiday_calendar`/`market_hours` 会开 Futu 抢 OpenD 槽位（既有病灶）。写入 Phase 1 验收。
- **D3 OpenD 槽位预算**：§4.7 影子执行要在 +0s..+300s 多时点抓 quote，盘后/夜盘行情订阅会占 OpenD 槽位。Phase 5 验收加「槽位预算 + 生产订阅零饥饿」判据。
- **D4 raw_artifact 存储**：整篇 HTML/PDF 直接入 SQLite 行会快速膨胀。建议 content-addressed 文件存储（`payload_hash` 作文件名）+ DB 只存 hash/路径/大小。Phase 1 决定，回头成本高。
- **D5 LLM verifier 配置纪律**：不得新增 `llm.models` 实体别名（30/38 feature 共享别名的爆炸半径教训）；温度/thinking_budget 显式 pin（沿用 DA 单旋钮惯例）；token 计入现有 budget（§4.5.2 已写，此处加配置面约束）。
- **D6 毕业门补两条**：(a) 每事件类输出 T+1/T+5 IC 衰减曲线，口径对齐既有 signal_ic_measurement 方法论（9/23 复测显示周级 alpha 侵蚀 80%——模板的 `horizon/expiry_at` 默认值应由衰减数据驱动而非拍脑袋）；(b) 除 50 样本/类外，加**最短运行周期 ≥8 周**以覆盖 regime 变化（对齐 pyramid 影子 n≥40+21 天的既有纪律）。
- **D7 Phase 0 验收补一条**：每个 T0/T1 源的 rate limit **实测值**与 ToS 结论（§4.1.1 的 15–30s 轮询目标须由实测背书，尤其对 FCC EDOCS 这类无正式 API 承诺的源）。

### 11.6 对 §9 待裁项的逐条建议

| # | 待裁项 | 建议 | 理由 |
|---|---|---|---|
| 1 | Phase 0 源与延迟审计 | **批准** | 零行为变更；11.2#12 的误读恰好证明"先实测后设计"的必要性 |
| 2 | 付费 Tier 1 wire / X firehose | **暂缓，默认公开源** | 影子期先用公开源测出延迟分布，Phase 6 用数据回答"付费能买多少秒"再裁决；T3 社交源维持非交易输入（合规与治理成本前置未清） |
| 3 | Phase 1 observe-only Event Store | **批准，附条件** | 条件：D1/D2/D4 并入 Phase 1 验收；N3 的 catalyst 取舍结论作为 Phase 2 开工前置 |
| 4 | 与统一 SessionResolver DEB 合批？ | **不合批，但建立单向依赖** | 姊妹 DEB §13 实测已把 SessionResolver 降级为"夜盘 profile 加载器"（方案 B′ 先减法后新件），其范围正在收缩；本 DEB §4.6 已正确声明不另造时段语义，Phase 4 路由只需消费"届时唯一口径"（`get_market_phase` + 未来夜盘 profile）。建议正文修订时在 §1.3 补一句「继承 DEB-20261009-unified §13.5 B′ 结论」 |
| 5 | 影子样本量与毕业阈值 | **接受 50/类 + 低频类预注册，附加 D6** | 补 IC 衰减对齐与 ≥8 周最短周期两条 |
| 6 | 盘前/盘后 live | **维持"永远需另行签署"，不预设永久禁止** | 与 DEB-20260917 治理先行一致；毕业门达标后另行签署即可，无需现在锁死 |

### 11.7 E 表（可复算证据，命令逐条可跑）

| # | 命令 | 期望输出 |
|---|---|---|
| E1 | `sed -n '870,895p' config/unified_config.yaml` | `enabled: false`(871) / `skip_market_hours: true`(889) / immediate_scan 三旗标(891-894) |
| E2 | `grep -n "after_hours_scan_enabled" config/unified_config.yaml` | `914:  after_hours_scan_enabled: false` |
| E3 | `cat data/llm_scan_state.json` | `{"last_afterhours_run": "2026-10-09T04:01:05.207682-04:00"}`（随循环路过更新，非静止证据） |
| E4 | `sed -n '2372,2374p;2450,2452p;2585,2587p' app/core/scheduler_core.py` | 三分支均在 `run_after_hours_llm_scan` 后无条件 `_persist_ah(now)` |
| E5 | `sed -n '593,594p' app/pipeline/llm_pipeline.py` | `after_hours_scan_enabled=false` 时进门即返回 |
| E6 | `sed -n '2680,2697p' app/core/scheduler_core.py` | docstring 链路终点含「下单」 |
| E7 | `python3 -c` 解析 `unified_config.yaml` 内联 watchlist 块 | 64 个 code；`US.VZ`∈、`US.T`∉、`US.TMUS`∉ |
| E8 | `grep -rn "EventSignalGenerator" app/ scripts/ config/ tests/` | 0 命中 |
| E9 | `ls app/catalyst/ app/catalyst/sources/` | event_classifier/catalyst_debate/detector/notifier/postmarket_review/reverse_catalyst + hkex/csrc |
| E10 | `sed -n '87,99p' config/unified_config.yaml` + `head -30 app/catalyst/event_classifier.py` | `catalyst.enabled: false`；docstring 自述依赖「NewsPipelineV2 输出」 |
| E11 | `ls app/news_v2/sources/` | `sec_edgar_source.py` 已存在（Phase 1 应迁移适配而非新建） |
| E12 | `grep -n "entities: {}" config/unified_config.yaml` | `787:  entities: {}` |

## 12. 用户方向输入与方案 U（UE 融合路线 · 2026-10-09）

> 本节记录用户对架构方向的重要修正，及据此修订的实施方案。**凡本节与 §4.6–§4.8、§6、§9 冲突处，以本节为准。** 本节为设计修订稿，落地前仍须用户逐条裁决（§12.6）。

### 12.1 用户输入（原话要点）

1. 现有的 **UE（llm_universe_scan / universe_expansion）本来就是扫描各种信息源的机制**，新设计应补充和完善它，而不是另起一条链路。
2. 目前全系统处于 SIM 模拟交易阶段，**可以不建影子（shadow）执行层**。
3. 事件机制产出的**标的与指令方向，作为来源之一**汇入现有体系即可。

### 12.2 UE 实况实测（用户判断成立的证据）

| # | 实测事实 | 证据 |
|---|---|---|
| U1 | UE 自述定位就是"读今日新闻事件 + 市场佐证 → LLM 关联因果推理 → 浮现受影响标的（受益+受损双方）→ 代码出数值 → 写 playbook_instructions → 与 DA 共用同一执行轨道在模拟盘执行" | `scripts/llm_universe_scan.py` docstring（:6-14） |
| U2 | UE **已在 SIM 真实下单**：`universe_expansion.entry_enabled: true` | `config/unified_config.yaml:2183` |
| U3 | UE 已是多源汇聚：RSSHub 新闻 + finnhub 付费新闻 + **SEC EDGAR clue（8-K/13DG，带噪声过滤）** + 板块异动 + 宏观 brief | `llm_universe_scan.py:1006-1036`、`:844-860` |
| U4 | UE 自带 observe/SIM 两级：`scan` 只落 jsonl 不写作战计划；`inject` 才写 playbook_instructions | docstring 子命令段 |
| U5 | UE 已支持**事件注入接口**：`scan --clue "手动线索文本"`——事件触发加跑**零新代码**，拼 brief 文本即可 | docstring :23 |
| U6 | UE 治理件已成熟：同标的冷却（ORCL 连亏 6 天教训）、续期硬闸、schema 契约、evidence grounding、开盘+10min confirm gate（price-in 检测）、价格地板、DA 供料通道 | `unified_config.yaml:2182-2230` |
| U7 | UE 归因已物理隔离：`strategy_id=universe_expansion`、`source=LLM_EVENT_SCAN`，与 playbook_sim 样本天然可分桶 | `llm_universe_scan.py` 头部常量 |
| U8 | UE 已有"先 scan-only 攒样 → 判据达标再开 inject"的毕业范式与**判据 FAIL 即退役**的先例（午间影子 10-03 实跑 FAIL 按判据退役） | `unified_config.yaml:2185-2189` 注释 |
| U9 | UE 时段盲区实测：主跑 cron 在盘前（21:10/22:10 CST 双挂 = 09:10 ET）+ 08:00 ET da-feed 早跑 + 09:40 ET confirm；**盘后 16:00–20:00 ET 无任何 UE 触发**——SpaceX 类盘后事件要等 ~17 小时 | crontab :103-104、:110-111、:117-118 |

**结论：用户判断成立。** UE 就是现成的"信息源 → 标的+方向 → SIM"主管线，且带归因隔离与治理纪律。原方案（§4–§6）的下游三层——signal_router（§4.6）、shadow_executor（§4.7）、独立评估毕业门（§6 Phase 6）——**全部与 UE/Playbook/DA 既有能力重复，应砍掉**。真正缺的只是 UE 的**上游**：T0 采集时效、事实存证与印证、实体传导的结构化输入。

### 12.3 方案 U 架构

```text
【新建 · 上游供料层】
T0/T1 Source Watchers（FCC/SEC/公司IR/wire）
  → Raw Event Store（append-only 存证，§4.2 不变）
  → Dedup / Corroboration / claim-fact 分离（§4.3 不变）
  → Entity Graph + 确定性事件模板（§4.4/§4.5.1 不变，产出"结构化事件 brief"）

【薄适配器 · 唯一的新接线】
事件 brief ──► UE clue 源之一（两条既有通道）：
  通道 A（定时）：注入 _build_clue_brief 的新增 clue 段，UE 盘前主跑自然消费
  通道 B（事件触发）：llm_universe_scan.py scan --clue <brief> 加跑（U5，零新代码）

【完全复用 · 下游零新建】
UE LLM 推理（标的+方向+thesis+置信度）→ confirm gate（price-in 检测）
  → inject → playbook_instructions → DA 统一执行轨道（SIM）
  → score/report 评估（LLM_EVENT_SCAN 归因桶）
```

LLM 的角色顺势修正：§4.5 原设计"确定性模板出首信号、LLM 只做复核"是为独立链路设计的；方案 U 下 **UE 的 LLM 仍是推理主力**（其产业链因果能力是既有优势），确定性模板与实体图降级为 **LLM 的结构化输入与事后校验**（事实锚定、防编造数字、反证条件），与 UE 既有 evidence grounding（U6）同向叠加，不另立"LLM 不得出首信号"的规矩。

### 12.4 原 Phase 对照（保留 / 瘦身 / 砍掉）

| 原方案 | 方案 U 处置 | 说明 |
|---|---|---|
| Phase 0 源与延迟审计 | **保留，不变** | 零行为变更，T0 源能力矩阵仍是开工前提 |
| Phase 1 Event Store + observe 采集 | **保留，瘦身** | 只存证+供料，不接任何路由；验收沿用 §11.5 D1/D2/D4 |
| Phase 2 归一化 + 实体图 | **保留** | 产出形态从"独立事件库"改为"UE clue 的结构化输入"；§11.4 N3 catalyst 取舍结论仍是开工前置 |
| Phase 3 推理器 | **改造** | 确定性模板 + 实体图 → clue brief 生成器；LLM verifier 职能并入 UE evidence grounding，不单立 |
| Phase 4 信号路由 | **砍掉** | 状态机/路由表/session 分层全部不需要；时段分流由"clue 通道选择 + confirm gate"天然实现 |
| Phase 5 影子执行 | **砍掉**（用户裁决输入） | 不建假设成交记录器；观察职能由 UE scan jsonl + confirm gate 覆盖；**成交记录直接要 SIM 真实模拟单**（比假设成交更真实：含 playbook 闸、滑点模型、退出轨道） |
| Phase 6 评估毕业 | **并入既有体系** | 复用 `llm_universe_scan.py score/report` + DEB 复盘；毕业判据沿用 UE 范式（scan-only 攒样 → 预注册判据 → 开 inject），不立新门 |

### 12.5 "不用影子"的精确解读（边界声明）

- **砍掉的是**：新建的 shadow_executor 基础设施（多时点假设成交快照、独立 markout 表）。理由成立：SIM 成交单包含真实闸门与退出轨道，信息量是假设成交的超集；且当前阶段零真实资金，无风险敞口。
- **保留的不是影子，是 UE 既有准入纪律**：新 clue 源（T0 事件流）接入时**先只进 scan brief 落盘攒样**（通道 A only），按预注册判据评估"该源对产出质量与前瞻收益的贡献"，达标后才开事件触发加跑（通道 B）与 inject 权重。这与 da_candidate_feed、confirm_gate 的毕业路径同构（U8 先例：不达标按判据退役），不新增任何基础设施。
- **时段语义**：盘后事件触发加跑产出的候选，默认**进次日 confirm gate**（开盘+10min price-in 检测）再 inject；是否允许"盘后 SIM 即时成交"（复用 extended_hours_entry_executor 的 ETH 护栏思想）留作独立待裁项（§12.6-4），默认不开。

### 12.6 修订后待裁项（取代 §9 与本节冲突部分）

1. **路线二选一（一号待裁）**：方案 U（UE 融合，本节）vs 原方案（§4–§6 全栈六 Phase）。§11 建议采纳方案 U。
2. Phase 0 源与延迟审计是否批准（不变，两方案共用前置）。
3. 事件触发加跑（通道 B）的准入判据：建议沿用 UE 范式——新 clue 源 scan-only 攒样 ≥10 交易日，贡献达标后开 `--clue` 加跑；判据数值随 Phase 0 审计结果预注册。
4. 盘后事件候选是否允许走盘后 SIM 即时成交（默认：否，一律次日 confirm gate）。
5. 付费 Tier 1 wire / X firehose 是否引入（§11.6 建议维持：暂缓，公开源先行）。
6. `app/catalyst` 取舍结论（§11.4 N3：取代/改造/分工三选一）——方案 U 下建议优先评估"catalyst 的事件分类能力改造为 clue 源"这一选项，与 T0 watcher 互补。

§9 第 4 项（与 SessionResolver DEB 关系）不受方案 U 影响，§11.6 建议不变：不合批，单向依赖其时段口径。

### 12.7 E 表补充（本节新增证据）

| # | 命令 | 期望输出 |
|---|---|---|
| E13 | `sed -n '1,30p' scripts/llm_universe_scan.py` | docstring：信息源/四子命令/`--clue` 参数/SIM 执行轨道自述 |
| E14 | `sed -n '2182,2184p' config/unified_config.yaml` | `entry_enabled: true` |
| E15 | `crontab -l \| grep universe_scan` | 21:10/22:10 scan 双挂、08:00 ET da-feed 双挂；盘后时段无条目 |
| E16 | `sed -n '2185,2230p' config/unified_config.yaml` | 午间影子 FAIL 退役先例 + 冷却/续期/schema/grounding/confirm_gate 治理件 |

## 13. 第二 Agent 独立复查（Hermes · 2026-10-09 · 只读实测）

> 复查人：Hermes。方法：不信任 §1.2/§11/§12 的描述，对事件识别链逐层活体实测（探针 `scripts/deb20261009_event_pipeline_probe.py`，只读，不触发 OpenD/外部 HTTP）。
> **本节在 §12（方案 U）已成文后追加，不推翻方案 U** —— 用户方向输入成立，我实测复核其 U2/U5/U9 全部为真（见 13.1）。本节只补 §11/§12 都未覆盖的**上游缺口**，其中两条是现在就在坏的既有件，比 Phase 0 新建 T0 采集层更紧急。
> 只追加：§1–§12 正文逐字未动。

### 13.1 对 §12 方案 U 的复核：用户判断成立，且比我预期的更强

| §12 声称 | 我的独立实测 | 判定 |
|---|---|---|
| U2 `universe_expansion.entry_enabled: true`（UE 已在 SIM 真实下单） | `unified_config.yaml:2183` 逐字为真 | ✅ |
| U5 `scan --clue` 事件注入接口零新代码 | `llm_universe_scan.py:4923` `p_scan.add_argument("--clue", ...)` 实证存在 | ✅ |
| U9 盘后 16:00–20:00 ET 无 UE 触发 | crontab 全部 UE/scan 条目最晚 = `22:10 CST` = 10:10 ET；盘后时段零条目 | ✅ |

⇒ **方案 U 的"下游零新建、只补上游"定位正确**，我认可 §12.6-1 采纳方案 U。以下 13.2–13.5 是方案 U 落地前**必须先处理的上游事实**，§12.4 把它们大多归入"保留/改造"，但没量化它们**当前就是坏的**。

### 13.2 修正 §11 N1：`app/catalyst` 不是死资产，它已是 UE 上游的活件

§11 写「当前 `catalyst.enabled: false`」，读起来像死模块。实测**相反**：

| 实测 | 读数 |
|---|---|
| `CatalystClassifier` 的生产消费方（排除 `app/catalyst/` 自身与本探针） | **6 个文件**：`gen_event_symbols.py`(4)、`llm_universe_scan.py`(3, ADR-017)、`scheduler_core.py`(2)、`post_market_audit.py`(1)、`us_pipeline/emergency_interceptor.py`(1)、`event_stream/relevance.py`(1，Phase 1.6 复用 SECTOR_TRIGGERS) |
| `catalyst.enabled` 门控范围 | `scheduler_core.py:564` `_gc_cat().get_dotted("catalyst")` **只管 HK/CN detector 链** |
| `CatalystClassifier` 是否受 `catalyst.enabled` 门控 | **否** —— 消费方直接 `from app.catalyst.event_classifier import CatalystClassifier` 使用 |

⇒ **§12.6-6「catalyst 取舍三选一」的答案已经确定**：不是"取代/退役"，而是**它已经在 UE 上游活着**（`gen_event_symbols` 产出 `event_symbols_*.json` → `llm_universe_scan` 消费）。方案 U 的"catalyst 改造为 clue 源"= **扩展一条现役链**，不是新建，也不是取舍。这一条把 §11 N3 的"Phase 2 开工前置取舍"直接消解了。

### 13.3 决定性缺口：事件词表**结构性单向**，SpaceX 案例 0/4 命中

用提案 §1.1 的四条真实事件标题跑**生产分类器** `CatalystClassifier().classify(use_llm=False)`（逐字照抄 `gen_event_symbols.py:120-130` 的构造方式）：

| 标题 | 分类结果 |
|---|---|
| SpaceX granted FCC authority to deploy 15,000 satellites... | ❌ 零命中 |
| SpaceX acquires 800 MHz spectrum licenses from Grain Management | ❌ 零命中 |
| AT&T, T-Mobile, Verizon shares fall 6% after SpaceX satellite authorization | ❌ 零命中 |
| FCC order DA-26-1078A1 grants-in-part SpaceX NGSO satellite authority | ❌ 零命中 |
| **对照**·SEC investigation into company accounting | ✅ REGULATORY/CRITICAL |
| **对照**·US adds firm to entity list, export ban | ✅ REGULATORY/HIGH |

**事件样本 0/4 命中，阳性对照 2/2 命中** ⇒ 不是探针假阴，是词表结构性缺口。根因两条：

1. **`REGULATORY` 全 9 条 direction=SHORT，零 LONG/授权型**（`KEYWORD_EVENT_MAP` 实测：`SEC investigation`/`sanctions`/`entity list`/`export ban`/`证监会`/`立案调查`… 全 SHORT）。词表只认"被查/被制裁"，不认"获批/获授权/收购资产"。提案 §4.5.1 的 `COMPETITOR_ENTRY` 模板正确写了双向（`incumbents: negative` + `entrants' suppliers: positive`），但**现役词表只有负面半边**。
   - ⚠️ **这与用户既定哲学直接冲突**：MEMORY「8/2 市场双向，反对不对称限流」「核心=真正有空头产出禁纯单向」——而 REGULATORY 轴恰恰是**纯单向 SHORT**。SpaceX 案里 incumbents(T/TMUS/VZ) 跌 6% 是 SHORT 机会，但授权方供应商是 LONG 机会，词表结构性地只能看见前者。
2. **`catalyst.sector_mapping` 仅 5 条**（ai_chip/big_tech/chinese_adr/chinese_broker/ev），**无电信/通信/频谱**（实测 grep telecom/comm/wireless/电信 = 0）⇒ 即便词表命中，也没有"频谱授权 → 电信运营商"的传导种子。

⇒ **这是方案 U 落地前最该先修的一件，且是数据改动不是新引擎**：给 `KEYWORD_EVENT_MAP` 补授权/收购/牌照类正面条目 + 给 `sector_mapping` 补 telecom/spectrum 种子。零新代码路径，直接让现役 `gen_event_symbols` 链就能识别下一个 SpaceX 类事件。

### 13.4 现在就坏的两件（不等 Phase 0）

**① RSSHub 新闻源停采 + 采集/消费 cron 星期错配**（提案 §1.2#3 只报了 mtime，未查因果）：

| 实测 | 读数 |
|---|---|
| `news_us.db` mtime / 内容跨度 | **2026-10-05 15:15（周一）**；`us_news` 273 行，`fetched_at` 止于 `2026-10-05T15:15` |
| RSSHub 采集 cron 星期 | `15 */3 * * 6,0` + `15 0-15/3 * * 1` = **只在周六/周日/周一** |
| 消费方 `gen_event_symbols` cron 星期 | `30 18 * * 1-5` = **周一~周五** |
| `get_recent_news` 取数方式 | **只读 DB 不实时拉取**（`rsshub_us_news.py:586-600`，`WHERE fetched_at > now-hours`） |

⇒ **周二~周五 RSSHub 腿恒返回 0 行**（DB 停在周一），HARD_EVENT 只能靠 `_gather_news` 的 finnhub 降级腿。产物面实证：`event_symbols_*.json` 59 个产物里 HARD_EVENT 总产出率仅 **10/59 = 16.9%**，且 **周三 0/12、周五 0/11**（与"周中 RSSHub 断粮"同向）。SpaceX 事件当天（10-06 周二/10-07 周三/10-08 周四）RSSHub 腿全空。

**② 静默失效：2026-09-17 用户裁的降级告警在真失效形态下永不触发**（`llm-decision-input-audit` 死门形态⑥同族）：

| 实测 | 读数 |
|---|---|
| `get_recent_news` 函数体内 `raise` 数 | **0**（无数据时 `return []`，不抛异常） |
| `_gather_news` 降级告警位置 | 在 `except Exception` 块**内**（`llm_universe_scan.py:1019-1035`） |
| finnhub 腿位置 | `if bool(_cfg(...))` **无条件并入**，不在 except 内 |

⇒ **DB 陈旧 → 查询返回 0 行 → 不抛异常 → 告警永不触发**。P2（2026-09-17 用户裁「接收建议」加的"静默降级=病D，无人知晓信息层断腿"告警）**恰好对它要防的真失效形态全盲**：它只在 RSSHub 抛异常时响，而"数据停更"不抛异常。这是比"建 T0 采集层"更紧急的**既有告警失效**。修法一行级：把"返回 0 行且 DB mtime 超过 N 小时"也纳入告警条件（不只 except）。

### 13.5 铁律 3：`news_quality.db` 是死表，Phase 1 新建 `event_stream.db` 前必须先处置它

提案 §4.2 拟新建 `event_stream.db` 存 canonical_events/signals。实测仓内**已有一个 schema 高度重合的死表**：

| 实测 | 读数 |
|---|---|
| `data/news_quality.db::news_classifications` | 37 行 |
| `data/news_quality.db` mtime | `2026-07-26 10:45`；结合 `news_v2.enabled: false`，当前不是活跃写入，而是退役链遗留死表 |
| 列 | `event_type / primary_tickers / impacted_tickers_json / sentiment / horizon_min / credibility / action_hint / rationale / classified_at`（与 §4.3 canonical_events 字段几乎一一对应） |
| 内容 | `event_type` **全 = 'other'**（37/37）；`primary_tickers` 非空仅 **2/37**；`sentiment≠0` **0/37** |
| 写入方 | `app/news_v2/processor/llm_classifier.py:227`（属**已退役** news_v2 链，`news_v2.enabled: false`） |

⇒ 这是"**退役链遗留、内容恒为兜底值**"的死表，而非当前仍在写（`llm-decision-input-audit` 死门形态⑨同类）。**Phase 1 若直接新建 `event_stream.db` 会长出第二张事件分类表**（违反铁律 3"不新增台账"）。处置二选一，须在 Phase 1 开工前定：a) 显式退役 `news_quality.db`（走 P7 归档，注明由 event_stream 接管）；b) event_stream 复用/迁移该表结构。不定论 = 两张同 schema 表并存漂移。

### 13.6 对 §12.6 待裁项的补充建议

| §12.6 项 | 我的补充 |
|---|---|
| 1 路线二选一 | **采纳方案 U**（已复核 U2/U5/U9 为真）；但把 13.3 词表补齐 + 13.4 两件修复**提为 Phase 0 之前的 P0 前置**（它们是现役链的既有缺陷，不依赖任何新基建） |
| 3 通道 B 准入判据 | 补一条前置：**先修 13.4② 静默失效告警**，否则"scan-only 攒样 ≥10 交易日"期间 RSSHub 断粮无人知晓，攒到的是空样本（与 winner_overnight 影子"bar 缺失无限重试"同型） |
| 6 catalyst 取舍 | **已消解**（13.2）：catalyst 不是待取舍的死资产，是现役 UE 上游。改为"扩展 `KEYWORD_EVENT_MAP` + `sector_mapping`"的数据补齐任务 |
| 新增 7 | `news_quality.db` 死表处置（13.5）：Phase 1 开工前 a/b 二选一 |
| 新增 8 | 词表方向对称性守卫：新增事件类型条目时，`REGULATORY`/`COMPETITOR_ENTRY` 类必须同时登记 LONG 与 SHORT 传导（对齐用户"禁纯单向"哲学），建议做成 CI 断言（词表按 event_type 分组，单方向条目占比 >80% 报警） |

### 13.7 E 表（可复算证据，命令逐条可跑）

| # | 命令 | 期望输出 |
|---|---|---|
| E17 | `venv/bin/python scripts/deb20261009_event_pipeline_probe.py` | `事件样本词表命中               : 0/4` |
| E18 | 同上 | `阳性对照命中                   : 2/2` |
| E19 | 同上 | `CatalystClassifier 消费方文件数 : 6` |
| E20 | 同上 | `get_recent_news 内 raise 数    : 0` |
| E21 | 同上 | `HARD_EVENT 总产出率            : 10/59` |
| E22 | 同上 | `news_us.db 行数                : 273` |
| E23 | 同上 | `news_quality.db event_type     : [('other', 37)]` |
| E24 | `grep -n "entry_enabled" config/unified_config.yaml` | `2183:  entry_enabled: true` |
| E25 | `grep -c "\-\-clue" scripts/llm_universe_scan.py` | `3` |

### 13.8 复查自曝

1. **探针自指（第二次犯同型错）**：探针 B 段自己 `from app.catalyst.event_classifier import CatalystClassifier`，A 段普查若不排除会把探针自己算成"生产消费方"。已加 `SELF_PROBE` 排除（与 §13 unified-session DEB 的同型自曝#3 是同一个坑，本次是**我第二次踩**，已 patch 进 skill）。
2. **B 段计数 bug**：初版用 `dict[tag]=n` 存结果，而两个样本共用 tag「事件主体」⇒ key 覆盖，计数报 `0/3`（实为 `0/4`）。改用 list 后修正。若不核，我会把一个正确的"零命中"结论配上错误的分母。
3. **`KEYWORD_EVENT_MAP` 遍历崩溃**：首次按 `direction` 排序时 `TypeError: '<' not supported between str and NoneType`（词表有 4 条 direction=None）。改 `str(d)` 后正常。这本身是个信号：词表存在无方向条目，13.6 新增项 8 的方向守卫要考虑 None 情况。
4. **差点误判 §12 主链表**：我用 awk 提取 crontab 字段时输出被截断，一度以为 da-feed 无双挂。改用原始行 `cut` 复核后确认 §12 U9 与姊妹 DEB §7.2 主链表都是对的。**截断后的输出不得用于下否定结论**（同 unified-session §13.7 自曝#4）。

### 13.9 上游 E 表可复跑性实测（§11.7 / §12.7，只登记不代改）

§11.7/§12.7 是并行 agent 的节，本节**不修改其正文**（且对方仍在活跃编辑），只登记实测结果供其自行处置。
逐条实跑 16 条命令后的精确分类：

| 分类 | E# | 实测 |
|---|---|---|
| ✅ 命令可跑 + 期望列为字面片段（可机械比对） | E2, E12, E14 | 3 条，验证器直接绿 |
| ⚠️ 命令可跑，但期望列是**描述文字**而非字面片段 | E1, E3, E4, E5, E6, E9, E11, E13, E15, E16 | 10 条。例：E6 期望「docstring 链路终点含「下单」」、E15 期望「21:10/22:10 scan 双挂…盘后时段无条目」——命令输出的是源码/crontab 原文，期望列是**人读的概括**，机器无法比对。复查者只能人眼核，等于 E 表失去"逐格实跑"的牙 |
| ❌ 命令**真不可跑** | E7, E10 | 2 条。E7 命令是 `python3 -c 解析 unified_config.yaml 内联 watchlist 块`（自然语言，非 Python 代码，shell 报 `unexpected EOF`）；E10 用 `+` 拼接两条命令（`sed -n '87,99p' … + head -30 …`），shell 不认 `+` ⇒ `sed: invalid option -- '3'` |

**处置建议**（交并行 agent / 用户裁，不在本节代改）：

1. E7 补成可跑命令（如 `venv/bin/python -c "import yaml;d=yaml.safe_load(open('config/unified_config.yaml'));w=d['watchlist']['codes'];print(len(w), 'US.VZ' in w, 'US.T' in w, 'US.TMUS' in w)"`），E10 拆成 E10a/E10b 或改用 `;`。
2. 10 条"期望列是描述文字"的行，建议把期望列改成**输出里必然出现的字面片段**（如 E6 期望改为 `风控/下单`、E15 改为 `22:10`），描述性概括移到 E 表外的说明列。这是 DEB-20261007/20261008 两批验证器已确立的形态（期望片段必须程序化提取，不手抄、不写概括）。
3. 我的验证器 `scripts/verify_deb20261009_event.py` 已把上游表与 §13.7 分区：§13.7 失败计入 rc，上游表只报告不计 rc（不代他人守门，但可跑性可见）。

> ⚠️ **本节自曝#5**：我给上游表做可跑性分类时，初版把 **E8 误判为"命令坏"**——E8 是 `grep -rn "EventSignalGenerator" …`，其**空输出正是"0 命中"的期望结果**，我却把"输出为空"当成命令失败。这是"测量工具自身的假阳"第三例（前两例见 13.8 自曝#1/#2）。修正后真不可跑 = E7/E10 共 2 条，不是 3 条。若不核，我会把一条正确的幻影引用证据（§11.3 已核实 `EventSignalGenerator` 全仓 0 命中）报成"E 表命令有错"。


## 14. P0 前置执行记录（2026-10-09）

> 用户裁决：按 §13.6 建议执行。本节只覆盖 **Phase 0 之前的现役链修复**；外部源能力/延迟/授权审计仍待 Phase 0 单独裁决。

### 14.1 已执行

| 项 | 执行 | 边界 |
|---|---|---|
| 事件词表 | 新增 `REGULATORY_APPROVAL`、`REGULATORY_DENIAL`、`SPECTRUM_TRANSACTION`、`COMPETITOR_ENTRY` 关键词 | 只补关键词，不改交易方向决策 |
| 电信传导 | `catalyst.sector_mapping` 新增 `telecom: [T, TMUS, VZ]`；分类器增加 direct-to-cell/spectrum 等触发 | 供 UE/事件标的生成，不自动下单 |
| UE/gen_event_symbols | 两处 `CatalystClassifier` 均传入生产 `sector_mapping` | 不新建第二事件引擎 |
| 陈旧源告警 | RSSHub 空结果且最后落库超过 `rsshub_stale_hours=6h` 时发 Feishu 告警 | 告警 best-effort，不阻断主链 |
| 采集覆盖 | RSSHub cron 由周一 `0-15/3` 扩为周一~周五 `*/3`，保留周末 `*/3` | 未改变采集频率与数据源 |
| 测试 | 新增 `tests/test_event_pipeline_p0.py` | 覆盖样本命中、双向词表、映射接线、陈旧/新鲜空结果 |

### 14.2 复跑读数

| 读数 | 修复前 | 修复后 |
|---|---|---|
| SpaceX/FCC 样本词表命中 | `0/4` | `3/4` |
| 阳性对照 | `2/2` | `2/2` |
| RSSHub 采集星期 | 周六/周日/周一 | 周一~周五 + 周末 |
| 空结果且 DB 陈旧 | 不告警 | 告警 |

P0 后复跑口径：事件样本 3/4 命中，阳性对照 2/2 命中；`CatalystClassifier` 消费方 **6 个文件**（Phase 1.6 新增 `event_stream/relevance.py` 复用 `SECTOR_TRIGGERS`）；`HARD_EVENT` **10/59 = 16.9%**。

未命中的第 3 条是“AT&T/T-Mobile/Verizon shares fall 6%”这类市场结果描述；其上游授权/频谱事件已被前两条与官方 order 条目命中，市场结果本身不应重复作为事实触发词。

### 14.3 E 表（P0 后可复算）

| # | 命令 | 期望输出 |
|---|---|---|
| E26 | `venv/bin/python scripts/deb20261009_event_pipeline_probe.py` | `事件样本词表命中               : 3/4` |
| E27 | 同上 | `阳性对照命中                   : 2/2` |
| E28 | 同上 | `CatalystClassifier 消费方文件数 : 6` |
| E29 | 同上 | `get_recent_news 内 raise 数    : 0` |
| E30 | 同上 | `HARD_EVENT 总产出率            : 10/59` |
| E31 | 同上 | `news_us.db 行数                : 273` |
| E32 | 同上 | `news_quality.db event_type     : [('other', 37)]` |
| E33 | `rg -n "rsshub_stale_hours" config/unified_config.yaml` | `2190:  rsshub_stale_hours: 6.0` |
| E34 | `rg -n "telecom:" config/unified_config.yaml` | `103:    telecom:` |
| E35 | `crontab -l \| grep -v '^#' \| grep 'RSSHubUSNewsFetcher'` | `15 */3 * * 1-5` |
| E36 | `venv/bin/python -m pytest tests/test_event_pipeline_p0.py -q` | `7 passed` |

### 14.4 自曝与边界

1. 初版测试把 `scripts.llm_universe_scan` 与 `llm_universe_scan` 当成同一模块，monkeypatch 没有作用到 `gen_event_symbols` 的实际 import，导致一次真实 Feishu API 调用；该调用因参数错误返回 `code=9499`，未成功发出消息。已改为 `scripts.llm_universe_scan` 同模块导入，并在该测试中显式 stub `_send_rsshub_alert`。
2. `news_quality.db` 仍未处置；Phase 1 开工前仍须在“退役归档”与“复用迁移”之间裁决。
3. 通道 B（事件触发 `--clue`）未开启，仍须 scan-only 攒样与判据裁决。
4. 本批不引入 X firehose、付费 wire、Kafka/Redis，也不改变 Playbook/SIM/live 边界。

## 15. Phase 0 外源审计执行记录（2026-10-09）

> 只读执行，无交易行为。工具：`scripts/audit_event_sources_phase0.py`；离线测试：`tests/test_event_source_phase0_audit.py`。本轮审计时间：`2026-10-09T11:12:31Z`。

### 15.1 实测矩阵

| 来源 | Tier | HTTP | 延迟 | 条数 | 时间字段 | 结论 |
|---|---|---:|---:|---:|---|---|
| SEC EDGAR current 8-K Atom | T0 | 200 | 265ms | 40 | `updated/published` | 可作为官方申报流首选 |
| SEC EDGAR full-text search | T0 | 200 | 446ms | 39 | `file_date` | 适合按需检索，不适合首延迟流 |
| FCC EDOCS order PDF | T0 | 200 | 541ms | 1 | 无 | 官方 artifact 可直连，但缺机器可读索引 |
| Federal Register API | T0 | 200 | 648ms | 183 | `publication_date` | 稳定官方监管通知源 |
| SpaceX IR | T0 | 403 | 78ms | 0 | 无 | 当前环境不可直采 |
| Grain IR | T0 | 429 | 1110ms | 0 | 无 | 当前环境不可直采 |
| Google News RSS | T2 | 200 | 451ms | 100 | `pubDate` | 可做 discovery 与竞速对照 |
| RSSHub root | T2 | 200 | 1555ms | 1 | 无 | 根路径活着 |
| RSSHub Bloomberg route | T2 | 超时 | 20.608s | 0 | 无 | 具体路由当前不可用 |
| Finnhub general news | T1 | 200 | 268ms | 100 | `datetime` | 现有授权可用，作补充 wire |

### 15.2 样本命中

- Google News RSS 命中：
  - `FCC Green Lights SpaceX's 15,000 Satellites for Starlink Mobile - PCMag`
  - `FCC to vote on auctioning 25MHz of 'prime spectrum' ... - CNBC`
  - `SpaceX Seeks $40 Billion To Buy Nvidia AI Chips ... - Investor's Business Daily`
- Finnhub 命中：
  - `SpaceX’s plans to buy more Nvidia GPUs is keeping Jim Cramer bullish on the chip stock - CNBC`
  - `published_at = 2026-10-07T20:24:48Z`
- FCC direct PDF：
  - `DA-26-1078A1` 可直连，但当前无机器可读时间戳；搜索页/门户在当前环境返回 403。

### 15.3 结论与建议

1. **Phase 1 首批源**：`SEC EDGAR current 8-K Atom + Federal Register API + Google News RSS + Finnhub general news`。前三者提供事实/发现分层，Finnhub 作为既有授权的补充 wire。
2. **FCC**：直接 PDF 只能作为已知 artifact 的 T0 fallback；要做通用发现，仍需找到机器可读 EDOCS/ECFS 索引或改用 Federal Register/权威媒体交叉确认。
3. **公司 IR**：SpaceX/Grain 当前分别 403/429，不能作为可靠直采源；后续只能走官方 API、公开 feed 或授权数据源。
4. **RSSHub**：根路径可用但具体路由超时，当前不适合作为核心 T2 discovery；Google News RSS 更稳定。
5. **付费 wire / X firehose**：本轮未引入；若要求亚秒级首源，需单独商务与合规裁决。
6. 本轮测得的是**端点响应延迟**，不是事件从发布到系统首见的真实传播延迟；后者需 Phase 1 observe-only watcher 连续采样后才能量化。

### 15.4 下一步

- 若用户批准，进入 Phase 1：只做 observe-only Event Store 与上述四个源的 watcher。
- 不接交易，不开启通道 B，不引入 Kafka/Redis。
- `news_quality.db` 处置仍须在 Phase 1 开工前裁决。

## 16. Phase 1 执行记录（2026-10-09）

> 本轮仍为 **observe-only**：只采集、落库、生成可给 UE 使用的 clue 文本；不修改 Playbook/DA，不触发交易，不开启通道 B，不引入 Kafka/Redis。

### 16.1 落地模块

| 层 | 文件 | 作用 | 独立性 |
|---|---|---|---|
| 数据模型 | `app/event_stream/models.py` | 定义 raw event、canonical event、source result 与稳定 ID | 不依赖现有 UE/交易代码 |
| 存储 | `app/event_stream/store.py` | SQLite append-only 三表：`raw_events`、`canonical_events`、`source_health` | 可独立替换或迁移 |
| 注册器 | `app/event_stream/source_registry.py` | 聚合首批四个 fetcher | 新源只要实现返回 `SourceResult` 的 callable 即可注册 |
| 采集服务 | `app/event_stream/ingest_service.py` | 统一 fetch、幂等落库、写 source health | 不直接调用 UE/交易 |
| 官方源 | `app/event_stream/sources/sec_edgar_atom.py`、`app/event_stream/sources/federal_register.py` | T0 申报/监管通知 | 独立 parser，可单独测试 |
| 发现源 | `app/event_stream/sources/google_news_rss.py`、`app/event_stream/sources/finnhub_news.py` | T2/T1 新闻 discovery/wire | 独立 parser，可单独测试 |
| UE 适配 | `app/event_stream/ue_clue_adapter.py` | 将最近 canonical events 渲染成 clue 文本 | 只读 EventStore，不修改 UE |
| CLI | `scripts/event_stream_ingest.py`、`scripts/event_stream_clue.py` | 手动采集与手动输出 clue | 可独立运行，可显式传入 DB |

### 16.2 端到端实测

- 实测库：`/tmp/event_stream_phase1_1066.db`（临时验证库，非生产库）。
- 四源 ingest 全部成功，共写入 250 条 canonical events。
- 逐源结果：

| source_id | tier | 状态 | 延迟 | events |
|---|---|---|---:|---:|
| `SEC_EDGAR_8K_ATOM` | T0 | OK | 10247ms | 40 |
| `FEDERAL_REGISTER_API` | T0 | OK | 662ms | 10 |
| `GOOGLE_NEWS_RSS` | T2 | OK | 781ms | 100 |
| `FINNHUB_GENERAL_NEWS` | T1 | OK | 269ms | 100 |

- 表计数：`raw_events=4`，`canonical_events=250`，`source_health=4`。
- 原因说明：Phase 1 每次成功 fetch 先保存 1 个 raw artifact，再解析出 canonical events；`raw_events` 按“源抓取批次”计数，`canonical_events` 按“事件条目”计数，二者不是同一粒度。
- 幂等验证：测试覆盖重复 raw/canonical append 均返回 `False`；重复执行 ingest 不应重复插入同 event ID。

### 16.3 UE 接入方式

- 当前选择：`scripts/event_stream_clue.py` 输出最近事件 clue 文本，可显式作为 `llm_universe_scan.py scan --clue "..."` 的输入。
- 本轮未把该输出直接接入 `_build_clue_brief`，也未新增 cron/watcher；这是刻意保留的手动接口，先验证数据质量与边界。
- 独立扩展原则：EventStream 是 UE 的可选上游信息输入，不要求 UE 改造；新采集源、去重器、相关性筛选器、推理器都可独立开发后以独立模块进入链路。
- 后续若要自动接入，应先通过独立 DEB 裁决默认 DB、调度频率、clue 事件窗口、跨源去重和优先级，而不是在 UE 主链内隐式拼接。

### 16.4 验证与边界

- 测试：`tests/test_event_stream_phase1.py`（10 passed）。
- 边界：
  1. 采集延迟是 HTTP fetch 延迟，尚未度量“事件发布到系统首见”的真实传播延迟。
  2. 事件仍为 `UNVERIFIED`，未做跨源一致性确认、标的抽取、方向推理或收益估计。
  3. Google/Finnhub 当前按 API 默认窗口取数，不代表全量新闻流；SEC/Federal Register 的条数也不代表事件重要性。
  4. 本轮没有将 `/tmp/event_stream_phase1_1066.db` 迁移为生产库，也没有注册任何常驻调度任务。
  5. `news_quality.db` 仍未裁决，后续仍需在“退役归档”与“复用迁移”之间二选一。

### 16.5 下一步候选

1. **生产化最小闭环**：选定生产 `event_stream.db`、增加低频 cron、保留 observe-only；先不接交易。
2. **延迟度量**：为每条事件记录 `published_at` 与 `first_seen_at`，输出源级/事件级传播延迟。
3. **相关性筛选**：按 watchlist、公司/行业/监管关键词先过滤，再交给 UE，避免 clue 被泛新闻淹没。
4. **跨源去重**：以 URL、标题和实体时间构建候选 cluster，再按 T0/T1/T2 优先级选择主证据。
5. **自动 clue 接入**：上述三项稳定后，再裁决是否接入 `_build_clue_brief`。

## 17. Phase 1.5 生产化执行记录（2026-10-09）

> 本节执行 §16.5 的第 1/2 项，仍是 **observe-only**。不调用 UE、不生成交易信号、不修改 Playbook/DA、不开启通道 B。

### 17.1 变更

| 项 | 执行 | 边界 |
|---|---|---|
| 生产库 | 默认路径改为 `data/event_stream.db`；已实际初始化并采集 | 独立 SQLite/WAL，不迁移 `news_quality.db` |
| 首见时间 | `canonical_events` 新增 `first_seen_at` 与 `published_to_seen_ms` | 仅在首次插入事件时写入，重复插入不覆盖 |
| 迁移 | 对 Phase 1 旧 schema 自动补列，并对空延迟元数据做只读回填 | 不修改事件正文/事实，只补派生字段 |
| 时间解析 | 支持 ISO8601 与 Google RSS 的 RFC822 时间 | 无法解析则延迟为空，不伪造成 0 |
| 报告 | 新增 `scripts/event_stream_report.py`，输出表计数、源健康、首见延迟 | 纯读 DB，无通知、无交易 |
| 调度 | crontab 每 5 分钟执行 `scripts/event_stream_ingest.py`，`timeout=120s` | 只写 EventStore 与日志，不调用 UE |

### 17.2 首次生产实测

| source_id | HTTP 状态 | HTTP 延迟 | events | 首见延迟样本 |
|---|---|---:|---:|---|
| `SEC_EDGAR_8K_ATOM` | OK | 1222ms | 40 | n=40，avg=33.6s，min=0.29s，max=53.3s |
| `FEDERAL_REGISTER_API` | OK | 641ms | 10 | n=10；但 publication_date 只有日期，延迟是“见报日零点到首见”，不是真实发布延迟 |
| `GOOGLE_NEWS_RSS` | OK | 482ms | 100 | n=100；初始样本含旧闻，不能把 max 当作当前传播能力 |
| `FINNHUB_GENERAL_NEWS` | OK | 283ms | 100 | n=100，avg约32.5h，受 feed 返回窗口影响 |

- 生产表计数：`raw_events=4`，`canonical_events=250`，`source_health=4`。
- 报告命令：`PYTHONPATH=. ./venv/bin/python scripts/event_stream_report.py`。
- 口径警告：`published_to_seen_ms` 是“本系统首次看见该条 event_id”的相对延迟；它不是“全球第一个信息源看见该事件”的延迟，也不能替代同事件跨源对齐。

### 17.3 挂载与回滚

- 挂载：`*/5 * * * * cd /opt/ftseek/FTSeek-US && PYTHONPATH=. timeout 120 .../scripts/event_stream_ingest.py >> logs/event_stream_ingest.log 2>&1`。
- 备份：`data/backups/crontab-before-event-stream-20261009.txt`。
- 回滚：恢复备份 crontab，或删除该 cron 行；`data/event_stream.db` 可保留作审计，不会影响主链。
- 频率说明：5 分钟用于先量化系统首见能力；若后续接入付费 wire 或高频源，应按源分别调度，而不是把所有源统一拉到亚秒级轮询。

### 17.4 验证

- `tests/test_event_stream_phase1.py`：`14 passed`。
- 覆盖点：首见延迟只写一次、RFC822 时间解析、旧 schema 迁移、缺失延迟元数据回填、四源 parser、幂等 append、source health、UE clue。
- 尚未覆盖：跨源同事件对齐、真实“事件发生时间”校准、付费 wire 对照、盘前/盘中/盘后分时段延迟统计。

## 18. Phase 1.6 相关性与跨源去重执行记录（2026-10-09）

> 继续保持 observe-only。新增模块只处理 EventStream 输出，不修改 UE 主链、Playbook/DA 或交易行为。

### 18.1 落地

| 模块 | 作用 | 边界 |
|---|---|---|
| `app/event_stream/relevance.py` | 读取现役 `watchlist.us_stocks` 与 `catalyst.sector_mapping`，过滤 watchlist/板块相关事件 | 只读配置，不新建第二 watchlist |
| `app/catalyst/event_classifier.py` | 将原私有 sector trigger 表提为 `SECTOR_TRIGGERS` 供 EventStream 复用 | 不改变分类与交易方向逻辑 |
| `app/event_stream/dedupe.py` | 以 URL/标题相似度/时间窗聚类，按 T0>T1>T2 选择主证据 | 只在 clue 生成前内存去重，不改 DB |
| `scripts/event_stream_clue.py` | 新增 `--relevant-only` 与 `--dedupe` 开关 | 默认行为不变，仍为原始 recent clue |

### 18.2 生产实测

- 命令：`PYTHONPATH=. ./venv/bin/python scripts/event_stream_clue.py --relevant-only --dedupe --limit 20`。
- 实测结果从泛新闻收敛到 Federal Register/Google News 的 spectrum、direct-to-cell、FCC、AT&T/T-Mobile/Verizon 相关事件。
- 发现并修复一个假阳：`COST` 曾把普通单词 `cost` 当 ticker；现在 ticker 只按原文独立大写 token 匹配，公司名仍大小写不敏感匹配。
- 去重策略保守：URL 归一化相同，或标题相似度≥0.60 且时间差≤24h 才归并；因此同一主题的不同措辞可能保留多条，宁可漏合并也不误合并。

### 18.3 验证与边界

- `tests/test_event_stream_phase1.py`：`18 passed`。
- 新增覆盖：telecom competitor 事件保留、无关泛新闻剔除、普通小写 `cost` 不误判为 `COST`、T0 优先于 T2 的跨源去重。
- 仍未接入 `_build_clue_brief`；当前是显式 CLI clue 输入，便于先人工检查相关性/去重质量。
- 下一步：连续观察数个交易日后校准标题阈值与 relevance 规则，再裁决是否自动接入 UE。

## 19. Phase 1.7 今晚实盘窗检验方案（2026-10-09）

> 用户已授权完成实施。本节把 EventStream 接入 UE 材料，但不新增直接交易行为；下游继续走既有 UE/DA 风控、确认与执行轨道。

### 19.1 实施

| 项 | 实施 | 边界 |
|---|---|---|
| UE 接入 | `_build_clue_brief` 新增 EventStream 结构化事实段 | 只扩输入材料，不改 prompt 放行规则 |
| 配置 | `universe_expansion.event_stream_clue_enabled=true`，24h 窗口，扫描 500 条，输出 20 条，去重开启 | 回滚改 false，无需删代码 |
| 证据索引 | 每条 EventStream 线索登记 source/published_at/symbols/event_key | `brief_material_stats.event_stream` 可直接验收 |
| 验收工具 | `scripts/event_stream_ue_check.py` | 只读最新 scan 记录与 source health，不改库 |
| 测试 | `tests/test_event_stream_ue_integration.py` | 覆盖 helper、禁用开关、brief/evidence 登记 |

### 19.2 今晚时间线

| 北京时间 | ET | 检验点 |
|---|---|---|
| 20:00 | 08:00 | 既有 `scan --da-feed` 自动消费 EventStream clue |
| 21:10 | 09:10 | 既有 premarket scan 自动消费 EventStream clue，并写 `brief_material_stats` |
| 21:30 | 09:30 | 开盘后观察价格/量能确认，不因事件线索绕过确认 |
| 21:40 | 09:40 | 既有 confirm gate 影子裁决 price-in/confirmed |

验收命令：`PYTHONPATH=. ./venv/bin/python scripts/event_stream_ue_check.py`。通过条件：最新 scan 存在、`event_stream` 材料数>0、四源 health 全 OK。

### 19.3 实施前实测

- `_gather_event_stream_clues()` 当前返回 `EventStream:11`。
- 线索集中覆盖 SpaceX/Grain 800MHz、FCC 15,000 satellite、direct-to-device、AT&T/T-Mobile/Verizon 相关事件。
- 相关性过滤输出的 symbols 为 `T/TMUS/VZ`，符合 `catalyst.sector_mapping.telecom` 传导。

### 19.4 风险与止损

- 若 20:00 前发现配置/导入错误，立即设 `universe_expansion.event_stream_clue_enabled=false` 并重启服务；采集 cron 可保留。
- 若 EventStream 输出噪声或证据索引异常，同样关闭该 feature flag；不影响既有新闻、EDGAR、事件标的输入。
- 今晚检验重点是“信息是否第一时间进入 UE 材料且可追溯”，不是证明事件策略可盈利；收益判断仍需后续多日样本。

## 20. 今晚 DA-feed 首轮实测（2026-10-09 20:02 CST）

### 20.1 验收结果

| 项 | 结果 |
|---|---|
| 最新 scan | `2026-10-09T08:02:17.779683-04:00`，`scan_window=da_feed`，`batch_status=complete` |
| EventStream 材料 | `event_stream=11`，总材料 `brief_material_count=91` |
| 源健康 | SEC/Federal Register/Google/Finnhub 四源全部 `OK` |
| 验收脚本 | `scripts/event_stream_ue_check.py` 返回 `passed=true` |
| UE 输出 | 5 个 surfaced 标的 |

### 20.2 EventStream 证据消费

- `ASTS` 输出 `SHORT`，confidence `0.55`，`evidence_matched_source=event_stream`。
- 证据引用：`ASTS Stock Slides After-Hours As SpaceX Snags Grain Spectrum...`
- 该证据与 `premarket_rank` 材料交叉，符合“事件线索 + 市场确认”的既有 UE 推理结构。

### 20.3 判读

1. **信息链验收通过**：采集 → 存储 → 相关性过滤 → 去重 → UE 材料 → LLM 引证 → scan 落盘全链路已经真实跑通。
2. **不是收益证明**：ASTS 单样本只能证明链路可用，不能证明方向或收益有效；需 21:40 confirm gate 与后续多日评分。
3. **执行边界不变**：20:00 轮为 DA-feed scan；21:10 的 `da_fresh_event_batch.py` 默认仍是 isolated shadow，不加 `--execute` 不写生产 DA 指令。

## 21. DA bridge 断链修复与生产恢复（2026-10-09 20:48 CST）

### 21.1 现象与根因

- `da_main_schedule.jsonl` 显示 08:00 ET 主流程完成：`candidates_total=56`、`analyses_count=12`。
- 但 `data/opportunity_scan_2026-10-09.json` 为 `candidate_count=0`，`playbook_instructions` 当日 0 条，健康报告为 `NO_PLAN`。
- 根因：`/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py` 的 `_write_bridge_file()` 引用了仅存在于 `run_full()` 局部作用域的 `_symbol_sectors`，导致 12 个分析在 bridge 组装阶段逐一异常并被跳过。
- 修复：在 `_write_bridge_file()` 内从 `scan_result.sector_rotation.symbol_sectors` 本地派生同名映射；不改变候选、方向、置信度或 gate 规则。

### 21.2 恢复

- 使用已保存的 `/root/.hermes/cache/scratch/da_pipeline_out_20261009.json` 重放 bridge；未重跑 LLM。
- 先备份空 bridge 与恢复前 DB 到 `data/backups/da_bridge_20261009_2048/`。
- 临时目录 `db_write=false` 复现验证：10 个候选，0 条 DB 写入。
- 生产重放 `db_write=true`：bridge 10 个候选，DB 10 条可交易指令。

### 21.3 复验

- `da_chain_health_report.py --date 2026-10-09 --days 1`：从 `NO_PLAN` 变为 `指令10(可执行10/SKIP0)`。
- `py_compile pipeline.py` 通过。
- `da_bridge_dingtalk_push.py --date 2026-10-09 --dry --force-window` 通过，显示 DB 可交易 `10/10`。
- 对账脚本在 ET 08:43 跳过属预期：其监测窗口为 09:02-10:30。

## 22. 21:10 premarket scan 实测（2026-10-09 21:19 CST)

### 22.1 扫描结果

- `llm_universe_scan.py scan` 于 ET 09:12:17 完成，`scan_window=premarket`、`batch_status=complete`。
- 材料 `138` 条，其中 `EventStream=20`；另有 earnings、SEC 8-K/13DG、新闻、peer surge、premarket rank 等既有输入。
- surfaced 4 条：`ASTS SHORT 0.55`（`event_stream`）、`HUBS SHORT 0.42`（`sec_8k`）、`NAVN LONG 0.42`（`sec_8k`）、`DAL SKIP 0.30`（`earnings_calendar`）。
- `da_supply_reconcile_check.py` 同窗口执行：bridge 10、DB 10、signal snapshots 12，判定 `OK`。

### 22.2 fresh-event 影子批

- UE 输出 3 个候选：`ASTS SHORT`、`HUBS SHORT`、`NAVN LONG`。
- `da_fresh_event_batch.py` 影子批完成，但 0 条进入深分析与生产指令：
  - `ASTS/HUBS`：`borrow_feasible=False`，不可借券 SHORT，按风控跳过。
  - 剩余 1 条：`stale_quote`，报价时效不足，按资格规则拒绝。
- 结论：这是准入/风控拒绝，不是 DA 链路执行失败；生产 DA 仍保持当日恢复后的 10 条指令。

### 22.3 源健康瞬时异常

- 21:15 的 EventStream 周期采集中 SEC EDGAR 一次 `ReadTimeout`，导致 `event_stream_ue_check.py` 临时返回 `source_health_ok=False`。
- 该异常发生在 21:12 scan 完成之后，不影响本轮 scan 已消费的 20 条线索。
- 手动触发一次 `event_stream_ingest.py` 后四源恢复 `OK`，复验 `passed=true`。

## 23. 落地复查（Claude 主线程 · 2026-10-09 晚 · 只读实测 + 棘轮清剿）

> 方法：不信任 §14–§21 执行记录，对**未提交工作树**逐条活体复核——diff 全读（26 文件 +1385/−95 + 新增 12 模块）、四新测试与六棘轮实跑、生产 DB/cron/台账实查、E 表抽样复跑。
> 本批动作：仅 2 处 except:continue 棘轮清剿（坏行计数 + stderr 留痕，零控制流变更），其余零改动。

### 23.1 总评：执行记录全部属实，边界纪律全守住

| 声称 | 实测 | 判定 |
|---|---|---|
| §14 P0 词表/传导/cron/告警 | E26–E32 探针复跑一致（3/4、2/2、消费方 6、raise 0）；`rsshub_stale_hours: 6.0`、`telecom: [T,TMUS,VZ]` 在 config:103/2188 区域实证 | ✅ |
| §17.3 cron `*/5` 挂载 + crontab 备份 | 均在（备份 57KB @19:30）；`source_health` 四源全 OK、last_success 新鲜 | ✅ |
| §18.2 COST 假阳修复 / 保守去重 | relevance.py 独立大写 token 匹配实证；dedupe T0>T1>T2 主证据排序实证 | ✅ |
| §19/§20 UE 接入与首轮实测 | `event_stream_ue_check.py` 复跑 `passed=true`；scan jsonl 实证 `event_stream=11 / 总材料91 / ASTS SHORT 0.55 / batch complete` | ✅ |
| §21 DA bridge 修复 | `pipeline.py:1270` 本地派生 `_symbol_sectors` 实证；py_compile 通过；bridge `candidate_count=10` | ✅ |
| 方案 U 边界（§12） | 未建 signal_router/shadow_executor/独立毕业门；Playbook/DA 零改动；`llm.models` 零新增别名；crontab 仅 +1 采集行与 RSSHub dow 扩档 | ✅ |
| §11.5 D2（采集侧不碰 futu 槽位） | 导入纯净实测：`app.event_stream.*` 全量 import 后 futu/market_hours/holiday_calendar 模块数 = **0** | ✅ |
| §11.5 D1（时区纪律） | naive-now 棘轮 PASS；event_stream 全部 `datetime.now(timezone.utc)` | ✅ |
| 测试 | 4 新件 + 2 触动件 = **130 passed** | ✅ |

### 23.2 新发现（按严重度）

- **F1（P1）发现源查询词写死样本，违反本文档 §2.3「可泛化」**：`google_news_rss.py` 默认 `query="SpaceX FCC"`（且 parser 内 `facts["query"]` 也写死）、`federal_register.py` 默认 `term="SpaceX"`。生产库实证：GOOGLE_NEWS_RSS **107/107 行** `facts.query="SpaceX FCC"`。含义：四源中两个发现源**只盯着样本事件**——§20 的「EventStream:11」证明的是链路通，**不证明覆盖面**；今晚若发生非 SpaceX 事件，这两个源是瞎的。修复配方：query 列表入 config（watchlist 公司名 + sector 触发词 + 通用监管词如 FCC/SEC/FTC），fetcher 按 query 循环，registry 支持多 query 实例注册。
- **F2（P1）EDGAR `source_url` 全空，断 §2.2 可溯源**：Atom 的 `<link>` 是 `href` 属性不是元素文本，`sec_edgar_atom.py` 取 `node.text` 恒得空串。生产库实证：SEC_EDGAR_8K_ATOM **52/52 行 source_url 为空**。连锁影响：T0 锚点源不可回溯原文、URL 去重对 EDGAR 失效、clue 行 `source=` 退化为 source_id。修复配方：改取 `node.attrib.get("href")`。
- **F3（P2，已清剿）except 棘轮 921→923 红线**：`event_stream_ue_check.py:32`（本批）与 `da_bridge_dingtalk_push.py:139`（P0-1 批）两处 `except json.JSONDecodeError: continue` 静默吞行。已按 R5 模式修复（坏行计数 + stderr 留痕，`da_bridge` 顺带早退 break 等价改写），棘轮回绿（31 passed）。**教训登记：落地批未跑棘轮**——见记忆「Ratchet Guards Skipped In Verification」。
- **F4（P2）`EventStore()` 名不副实的"只读"**：ue_clue_adapter/UE clue 路径打开 store 即可能 CREATE 表 + 迁移 + 回填（`_initialize` 每次构造都扫 `published_to_seen_ms IS NULL`）；且 clue 侧默认路径 `_ROOT/data/event_stream.db` 与 store 默认 `get_data_dir()/event_stream.db` 是双口径，若分歧则 clue 静默为空。建议：clue/UE 侧改只读连接（`mode=ro`）或统一路径 helper。
- **F5（P3）dedupe 时间条件宽于自述**：`_close_in_time` 任一侧时间缺失即返回 True → 实际合并条件比 §18.2「相似度≥0.60 且时间差≤24h」宽（缺时间时无视时间窗）。与「宁可漏合并」的保守声明方向相反，建议缺失时不合并或补一行注释说明取舍。
- **F6（P3）ticker 正则休眠坑**：watchlist 匹配用「独立大写 token」，若 `T` 未来加入 watchlist，`T-Mobile` 标题会误命中 T（当前 T 不在 watchlist，坑休眠）。登记备查。
- **F7（P3）da_bridge 窗口注释 off-by-one**：docstring 写 08:50–09:25，代码 `<09:26`。cosmetic。
- **F8（观察）UE 集成测试牵出 futu 线程**：`test_event_stream_ue_integration.py` 经 `llm_universe_scan` 重导入拉起 `futu network_manager`（DeprecationWarning 实证）。存量重依赖，非本批引入；hermetic 面登记。
- **F9（观察）raw_artifact 内联增长**：每 5 分钟 4 源全量 payload 入库（有变化才插入，当前 42 行）；§11.5 D4 的 content-addressed 外置建议留待 DB 体积成为问题时再裁。
- **F10（观察）ingest cron 整体死亡无告警**：单源失败有 source_health 行，但 cron 本身挂掉（超时/环境）只有日志。observe-only 阶段可接受，接 clue 自动消费前建议补探活。

### 23.3 验收结论

§20 的「信息链验收通过」**属实**：采集→存证→相关性→去重→UE 材料→LLM 引证→落盘全链真实跑通，且方案 U 的边界（UE 上游供料、下游全复用、零新交易通路）被完整守住。**但在 F1 修复前，覆盖面对非样本事件不成立**——建议：F1/F2 修复（均属小改）后再进入多日攒样；通道 B 维持关闭；§12.6 待裁项 3/4/6 不变。

### 23.4 E 表（本节证据，逐条可复跑）

| # | 命令 | 期望输出 |
|---|---|---|
| E37 | `sqlite3 data/event_stream.db "SELECT COUNT(DISTINCT json_extract(facts_json,'\$.query')) FROM canonical_events WHERE source_id='GOOGLE_NEWS_RSS';"` | `1`（全部同一写死 query） |
| E38 | `sqlite3 data/event_stream.db "SELECT COUNT(*), SUM(source_url='') FROM canonical_events WHERE source_id='SEC_EDGAR_8K_ATOM';"` | `52\|52`（url 全空） |
| E39 | `PYTHONPATH=. ./venv/bin/python -m pytest tests/test_except_pass_ratchet.py tests/test_naive_datetime_now_ratchet.py tests/test_hardcoded_utc4_ratchet.py tests/test_w456_log_hygiene_ratchet.py -q` | 清剿后全 pass |
| E40 | `PYTHONPATH=. ./venv/bin/python scripts/event_stream_ue_check.py` | `passed=true` |
| E41 | `crontab -l \| grep -E "event_stream_ingest\|RSSHubUSNewsFetcher"` | `*/5` 采集行 + `15 */3 * * 1-5` 工作日行 |
| E42 | `PYTHONPATH=. ./venv/bin/python -m pytest tests/test_event_pipeline_p0.py tests/test_event_source_phase0_audit.py tests/test_event_stream_phase1.py tests/test_event_stream_ue_integration.py -q` | 全 pass（本批实跑 130 passed 含触动件） |
| E43 | `PYTHONPATH=. ./venv/bin/python -c "import sys, app.event_stream.store, app.event_stream.ue_clue_adapter; print([m for m in sys.modules if m.split('.')[0] in ('futu',)])"` | `[]` |
| E44 | `grep -n "_symbol_sectors" ~/.hermes/skills/deep-stock-analysis/scripts/pipeline.py \| head -2` | `:1270` 本地派生（`_write_bridge_file` 内） |

## 24. §23 发现修复批（2026-10-09 晚 · 用户裁「请修复」）

### 24.1 修复清单

| # | 修复 | 文件 | 形态 |
|---|---|---|---|
| F1 | 发现源多 query/term 化：`fetch_google_news_rss(queries)` / `fetch_federal_register(terms)` 按列表轮询合并（event_id 去重、0.5s 礼貌间隔、单 query 失败不拖垮整源、error 字段留痕）；query/term 列表入配置 `event_stream.google_news_queries`（4 条：监管机关/频谱电信/半导体/并购）与 `event_stream.federal_register_terms`（2 条机构名）；`facts.query`/`facts.term` 记真实口径 | `google_news_rss.py`、`federal_register.py`、`unified_config.yaml` | 行为变更（覆盖面扩大），observe-only 不变 |
| F2 | EDGAR Atom `<link>` 改取 `href` 属性（兼容旧文本形态兜底） | `sec_edgar_atom.py` | bug 修复 |
| F3 | （§23 已清剿）2 处 except:continue 加坏行计数 + stderr | `event_stream_ue_check.py`、`da_bridge_dingtalk_push.py` | 留痕，零控制流变更 |
| F4 | UE clue 路径与 EventStore 默认解析同源（`db_path=None` → `get_data_dir()`），消除双口径静默空 clue 风险 | `llm_universe_scan.py` | 等价重构 |
| F5 | dedupe 时间口径：naive 一律按 UTC 解释（根治 FR 纯日期 × RFC822 混合的 TypeError 隐患）；时间缺失**不合并**（对齐 §18.2「宁可漏合并」自述） | `dedupe.py` | bug 修复 + 口径对齐 |
| F6 | ticker 匹配 lookahead 加 `-`：`T-Mobile` 不再误命中 `T`（休眠坑拆除） | `relevance.py` | 健壮性 |
| F7 | **复核后撤回**：docstring「08:50-09:25」与代码 `<09:26` 在「含分钟」语义下一致，非问题 | — | 不动 |

### 24.2 实测证据

- 测试：`test_event_stream_phase1.py` 等 4 件 + 触动 2 件 = **137 passed**（含新增 7 用例：EDGAR 真实 Atom 形态取 href、多 query 合并/全失败 ERROR/部分失败留痕、多 term 合并、缺失时间不合并、naive×aware 混合不炸、T-Mobile 守卫、配置回退）；六棘轮 **41 passed** 全绿。
- 生产实测（cron 自动生效，无需重启）：
  - 首轮新代码采集（13:10 批）Google 4 条新 query 落 386 行回填；稳态（13:12/13:15/13:17 三批）每 5 分钟仅 +5~10 条新事件 ⇒ event_id 跨轮去重有效，headline 级 398/401 无重复。
  - SEC 新行 `source_url` 已带真实 `sec.gov/Archives/...` 链接（13:10 起）；存量 57 行空 URL 为旧代码遗留，不回填（append-only 纪律，首见语义不可改）。
  - clue 烟测（`--relevant-only --dedupe`）：输出已覆盖半导体出口管制、Charter/SpaceX 移动报价、Supermicro 走私指控等非样本事件 ⇒ F1 修复后泛化成立。
- 新观察（登记不处理）：Google News RSS 会回流旧闻（重标 pubDate 的 4 月/6 月条目）——UE 侧 24h lookback 窗口（`_gather_event_stream_clues`）已天然拦截，仅影响 CLI 原始视图。

### 24.3 边界

- 仍未开启通道 B；UE clue 开关 `event_stream_clue_enabled` 不变；Playbook/DA/风控零改动。
- F8（UE 集成测试牵出 futu 线程，存量）/ F9（raw_artifact 内联增长）/ F10（ingest cron 整体死亡无告警）维持观察项，不随本批处理。
- 全部改动仍在未提交工作树，待用户裁决后提交。
