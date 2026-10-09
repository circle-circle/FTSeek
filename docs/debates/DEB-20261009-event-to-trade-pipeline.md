---
title: 事件驱动的盘前/盘中/盘后采集-推理-交易链路设计
date: 2026-10-09
status: 待决策 · 设计稿已落档，未修改代码、配置、crontab、Hermes 任务或交易行为 · §11 主线程只读复查已追加：§1.2 十二条实测 9 真 / 2 语义误读 / 1 不精确；发现 app/catalyst 同轴引擎盘点遗漏 + §5 一处幻影引用；待裁项逐条建议见 §11.6 · §12 用户方向输入：改采 UE 融合路线（Event Stream 降级为 UE 上游供料层，下游 signal_router/shadow_executor/独立毕业门全砍，候选直接进 SIM），修订方案 U 见 §12，原 §6/§9 与 §12 冲突处以 §12 为准 · §13 Hermes 独立复查已追加：复核 §12 方案 U 成立 (U2/U5/U9 实测为真)，另查出四条上游缺口 (事件词表结构性单向致 SpaceX 案 0/4 命中 / RSSHub 停采+星期错配 / 降级告警对真失效形态全盲 / news_quality.db 死表)，其中两件是现役链既有缺陷，建议提为 Phase 0 前置
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
| `CatalystClassifier` 的生产消费方（排除 `app/catalyst/` 自身与本探针） | **5 个文件**：`gen_event_symbols.py`(4)、`llm_universe_scan.py`(3, ADR-017)、`scheduler_core.py`(2)、`post_market_audit.py`(1)、`us_pipeline/emergency_interceptor.py`(1) |
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

⇒ **周二~周五 RSSHub 腿恒返回 0 行**（DB 停在周一），HARD_EVENT 只能靠 `_gather_news` 的 finnhub 降级腿。产物面实证：`event_symbols_*.json` 58 个产物里 HARD_EVENT 总产出率仅 **10/58 = 17.2%**，且 **周三 0/12、周五 0/11**（与"周中 RSSHub 断粮"同向）。SpaceX 事件当天（10-06 周二/10-07 周三/10-08 周四）RSSHub 腿全空。

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
| 列 | `event_type / primary_tickers / impacted_tickers_json / sentiment / horizon_min / credibility / action_hint / rationale / classified_at`（与 §4.3 canonical_events 字段几乎一一对应） |
| 内容 | `event_type` **全 = 'other'**（37/37）；`primary_tickers` 非空仅 **2/37**；`sentiment≠0` **0/37** |
| 写入方 | `app/news_v2/processor/llm_classifier.py:227`（属**已退役** news_v2 链，`news_v2.enabled: false`） |

⇒ 这是"**表在写、内容恒为兜底值**"的死输出（`llm-decision-input-audit` 死门形态⑨"判据字面量≠生产值域 / 空写入"）。**Phase 1 若直接新建 `event_stream.db` 会长出第二张事件分类表**（违反铁律 3"不新增台账"）。处置二选一，须在 Phase 1 开工前定：a) 显式退役 `news_quality.db`（走 P7 归档，注明由 event_stream 接管）；b) event_stream 复用/迁移该表结构。不定论 = 两张同 schema 表并存漂移。

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
| E19 | 同上 | `CatalystClassifier 消费方文件数 : 5` |
| E20 | 同上 | `get_recent_news 内 raise 数    : 0` |
| E21 | 同上 | `HARD_EVENT 总产出率            : 10/58` |
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
