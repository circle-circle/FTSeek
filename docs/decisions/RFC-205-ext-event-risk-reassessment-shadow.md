# RFC-205 · EXT 在途事件再评估影子（offline record-only）

- 日期: 2026-11-13
- 状态: **初版离线评估已实施；后续接入见 RFC-207（PERMANENT_MEASURE + cron）/RFC-208（生产 canary）**
- 用户裁决依据: DEB-20261106 §65.4 P2「EXT 持仓遇到新重大事件时，只记录 `NO_CHANGE/REVIEW/INVALIDATE` 及反事实，不自动平仓、不改尺寸；另立 RFC/注册，不能与 P1 同批上线」
- 关联: DEB-20261106 §65（NVDA 事件案例）、§17/§18（Event Intelligence v1 离线原型）、RFC-198（离线原型 intake）
- 脚本: `scripts/ext_event_risk_shadow.py`
- 单测: `tests/test_ext_event_risk_shadow.py`
- 产物: `data/shadow/ext_event_risk/ext-risk-<trade_id>-<decision_id>-<evaluation_sha12>.json`
- 首个案例: `trade_records.id=1864` / `decision_id=EXT-2026-11-11-US.NVDA-afterhours`

## 1. 目标与非目标

### 1.1 目标

对一笔**已平仓**的 EXT 交易，用 EventStore 的 `first_seen_at` 时间线回答：

1. 持仓窗口内是否出现了与标的**直接相关**的新事件？
2. 该事件是重大信息（`REVIEW`），还是官方/一手来源的**确定性反证**（`INVALIDATE`），还是无事发生（`NO_CHANGE`）？
3. 把判定所需的全部输入留痕：DB 路径、as_of、原始 `event_time`/`first_seen_at`、source ID、证据层级、去重记录、哈希、原始决策元数据。

### 1.2 非目标（本 RFC 明确不授权）

- 不自动平仓、不减仓、不改尺寸、不改止损/止盈、不下单；
- 不写生产 DB、不写 EventStore、不写 `handoff.md`/DEB、不改 `app/`/`config/`/cron/registry；
- 不计算“避免了多少亏损”的反事实 PnL：**没有事件窗口 bars 时 `counterfactual.status=unavailable`**；
- 不把 `REVIEW` 当成卖出信号，不把 `INVALIDATE` 当成已执行动作；
- 不做模型回放、不做事件真伪鉴定、不做跨标的组合裁决。

## 2. 数据源与只读纪律

| 输入 | 路径（默认） | 读取方式 |
|---|---|---|
| 交易记录 | `data/ftseek_us.db::trade_records` | `file:...?mode=ro` + `PRAGMA query_only=ON` |
| 规范事件 | `data/event_stream.db::canonical_events` | 同上 |

- 两个库都以 SQLite URI `mode=ro` 打开；脚本不 import `EventStore`（其构造会 `CREATE TABLE`）。
- `PRAGMA query_only=ON`，且只执行 `SELECT`/`PRAGMA`。
- 单测以 `tmp_path` 建最小库，验证评估前后主库文件 SHA256 不变、WAL 无已提交内容。
- 产物只写到 `data/shadow/ext_event_risk/` 或用户显式 `--out` 路径；CLI 用 `O_CREAT|O_EXCL` **拒绝覆盖已存在文件**。

## 3. 评估口径（预注册，单案执行前固定）

### 3.1 时间窗

```text
entry_time < first_seen_at <= as_of
```

- `as_of` 默认 = `trade_records.exit_time`，可用 `--as-of` 覆盖（UTC ISO，且必须严格晚于 entry）。
- 只使用 `first_seen_at` 判定“当时是否可见”；`event_time` 仅作为原始发布时刻留痕，不参与窗口判定。
- `first_seen_at <= entry_time` 的事件排除；`first_seen_at > as_of` 的**未来事件**排除。
- 输出 `window_audit` 记录扫描数、窗口内数、入场前/入场时排除数、未来排除数、无法解析数。

### 3.2 直接相关性

事件必须至少满足一项：

- `event_meta.entities.symbols` 含标的 ticker；
- `event_meta.entities.company` 含 ticker/别名；
- 同一 CIK（由 `symbols` 含本标的的事件反查）；
- `headline` / `summary` / `facts_json` 文本含 ticker 或别名（短代码用词边界匹配，防 `ARM` 命中 `alarm`）。

别名种子来自 DEB-20261106 §65 冻结案例集（NVDA/AMD/AVGO/TSM/MU/SNDK/ARM/NBIS），可用 `--alias` 追加。**只做直接相关；不做板块、同业、供应链的语义外推。**

### 3.3 去重

- URL 归一：小写 scheme/host、去 `www.`、去 query/fragment、去尾斜杠；
- 标题归一：小写、折叠空白、去常见“ - Publisher”后缀、去标点；
- 同一 URL 或同一标题只保留 `first_seen_at` 最早的一条；重复项逐条记入 `dedup.duplicates`（含 kept/duplicate event_id、原因、时间）。
- **组内重大性提升**：同一 URL/标题组内只要任一副本 material，代表事件（最早可见）也按 material 处理，并在 `material_from_duplicate_event_ids` 记录来源。这样“早到的 T2 低重大副本”不会把“晚到的 T1 重大副本”去重掉。
- **官方反证不被去重隐藏**：`INVALIDATE` 检查使用**去重前**的直接官方事件集合；即使官方副本与更早的媒体副本同标题，官方确认仍会被检查。代表事件仍保留最早 `first_seen_at`，以符合“当时何时首次可见”的口径。

### 3.4 证据层级与三态判定

| 层级 | 判定 | 能否 `INVALIDATE` |
|---|---|---|
| `OFFICIAL_PRIMARY` | `source_tier=T0` 或 `source_id` 以 `SEC_` 开头 或 `event_type` 以 `SEC_` 开头 或公司 IR 前缀 | **可以**，且必须命中显式反证短语 |
| `MAJOR_MEDIA` | `source_tier=T1` | 否，只能 `REVIEW` |
| `DISCOVERY_RSS` | `source_tier=T2` | 否，只能 `REVIEW` |
| `SOCIAL_UNVERIFIED` | source_id 含 REDDIT/STOCKTWITS/TWITTER/SOCIAL | 否，只能 `REVIEW` |

判定顺序：

1. 任一官方/一手事件命中**显式反证** ⇒ `INVALIDATE`；
2. 否则存在 material 直接事件 ⇒ `REVIEW`；
3. 否则 ⇒ `NO_CHANGE`。

显式反证必须是**同一段 headline/summary 文本**同时满足：

- 提到标的/别名；且
- LONG 方向命中：CEO/CFO/COO/founder/president 级离职/被免短语，或会计欺诈/重述/持续经营/破产/退市，或下调指引，或 SEC/DOJ/商务部级别的指控/禁令/传票，或出口禁令/制裁；
- SHORT 方向命中：官方并购/私有化（含溢价/全现金/已完成），或上调指引，或财报超预期。

**刻意排除**：

- `Item 5.02: Departure of Directors or Certain Officers` 这类 8-K 通用样板不算显式反证（没有 CEO 级角色、没有人名、没有“对某笔持仓构成确定性矛盾”的语义）；
- 所有 `MAJOR_MEDIA`/`DISCOVERY_RSS`/`SOCIAL_UNVERIFIED` 的“CEO 可能被罢免”“已辞职（媒体报道）”一律只能 `REVIEW`；
- 未验证传闻、单一匿名来源、聚合 RSS 标题不得触发 `INVALIDATE`。

### 3.5 主事件选择（仅影响展示，不影响三态）

- `INVALIDATE`：取最早命中显式反证的官方事件。
- `REVIEW`：在 material 直接事件中按严重度排序，取最高者作为 `assessment_event`；同严重度取最早。
  - 严重度 3 = CEO 级离职/被免短语；
  - 严重度 2 = 官方 filing，或 probe/investigation/subpoena/export ban/sanction/lawsuit/fraud/resignation 等；
  - 严重度 1 = 其他 material 事件。
- 同时输出 `first_material_event`（最早 material 事件），避免“主事件”掩盖更早的市场综述。

### 3.6 反事实

```json
{
  "status": "unavailable",
  "claimed": false,
  "avoided_pnl_usd": null,
  "reason": "No event-window bars were loaded..."
}
```

没有事件窗口 bars 就**不计算**反事实出场价、不计算 avoided PnL、不声称“若当时退出会少亏/多赚”。交易的 `realized_pnl` / `pnl_net` 只作为已发生结果的上下文保留，不得被包装成影子的收益归因。

## 4. 产物字段

每个产物至少包含：

- `schema` / `mode=offline_read_only` / `production_write=false` / `auto_action=false`；
- `decision_id` / `trade_id` / `ticker` / `aliases`；
- `as_of` / `as_of_source` / `window`；
- `db_paths`（trade/event 两个绝对路径）；
- `input_hashes`：两个主库文件 SHA256（注明不含 WAL）、trade 行 SHA256、窗口事件集 SHA256；
- `evaluation_sha256`：trade 行 + 窗口事件 + 参数 + verdict + 主事件 ID 的稳定哈希；
- `payload_sha256`：产物自身（不含 `payload_sha256`/`output_path`）的稳定哈希；
- `verdict` / `verdict_reason` / `assessment_event` / `first_material_event` / `material_events`；
- 每条事件保留原始 `event_time`、`first_seen_at`，另列 `*_utc` 归一值、`source_id`、`source_tier`、`evidence_tier`、`materiality`、`relevance.reasons/matched_terms`、`published_to_seen_ms`；
- `dedup` / `window_audit` / `contradiction_check.official_primary_events_examined`；
- `counterfactual`；
- `trade_record`（整行）与 `decision_metadata`（`metadata_json` 解析结果）及 `decision_metadata_sha256`；
- `limitations`。

## 5. 首个案例结果（2026-11-13 实测）

命令：

```bash
./venv/bin/python scripts/ext_event_risk_shadow.py
```

结果摘要（详见 `data/shadow/ext_event_risk/` 下产物）：

| 字段 | 值 |
|---|---|
| trade | `id=1864` / `EXT-2026-11-11-US.NVDA-afterhours` / LONG 28 @174.14 |
| 窗口 | `2026-11-11T21:32:40.391220Z` → `2026-11-12T14:30:42.518331Z`（exit） |
| 窗口内事件 | 675（扫描 28,418） |
| 直接相关去重后 | 31（去重 19 条） |
| material 直接事件 | 5 |
| verdict | **`REVIEW`** |
| 主事件 | `853ef3ee05478043a7c9`，`FINNHUB_GENERAL_NEWS`/T1，`first_seen_at=2026-11-12T01:44:03.138077Z`（20:44 ET），headline “Cramer's stance on Nvidia's stock amid reports that CEO Jensen Huang may be ousted” |
| 最早 material | `fabf4cf163ff1d4ed5aa`（23:19:04Z，媒体市场综述“internal probe”） |
| 官方事件 | `89a87482e048675a75ef`（SEC 8-K，T0，13:59:03Z）：Item 5.02 通用样板，**不构成显式反证** ⇒ 不 `INVALIDATE` |
| 反事实 | `unavailable`；`avoided_pnl_usd=null`；`claimed=false` |
| 产物 | `data/shadow/ext_event_risk/ext-risk-1864-EXT-2026-11-11-US.NVDA-afterhours-635bb8df79c8.json` |
| `evaluation_sha256` | `635bb8df79c8e53249c9a32ee97d4936eb5f26405224e76c5a5ce3e63dbdd122` |
| `payload_sha256` | `6454ddea1afffd1e9511a2b3af2f4728b4b2a75bf1a2cd0ff615eff100856a2c` |

解读：这是一次**要求人工复核**的影子输出，不是卖出指令。01:44Z 的 T1 报道明确提到“CEO may be ousted”，但来源是媒体、状态未验证；13:59Z 的官方 8-K 只给出通用 Item 5.02 离职/任命样板，未点名 CEO、未给出“Huang 已辞职”的显式官方表述，因此按预注册规则**不能**自动 `INVALIDATE`。该案例同时证明了“官方来源存在 ≠ 可以反证”。

## 6. 当前状态与硬边界

- **offline only**：脚本只在人工命令下运行；没有 cron、没有 `shadow_registry` 注册、没有被任何生产模块 import、没有消费者。
- **不写生产**：两个 DB 只读；产物是新的 JSON 文件；`production_write=false` 写进产物。
- **无自动动作**：不产生订单、不修改 `trade_records`/`open_positions`/`playbook_instructions`。
- **未注册原因**：按用户本轮授权，注册表/cron/交接文件均不在本批改动面；影子先以离线单案跑通并冻结口径。注册与调度须另立 RFC/审批。

## 7. 未来影子预注册标准（未达标前不得宣称有效）

后续若要把本影子扩成持续样本，必须**先满足**以下门槛，再谈任何行为变更：

1. **样本量**：`>= 30` 个 position-event pairs，覆盖 `>= 10` 个 ET 交易日；同一事件命中多标的按多个 pair 计数，但同一 (position, event) 只计一次。
2. **事件可见性/覆盖率**：
   - 每个持仓窗口内至少 1 条直接相关事件的覆盖率；
   - `first_seen_at - event_time` 的采集时延分布（p50/p90/p99）；
   - 因“事件晚到”而落在窗口外的事件数（单独列，不得回填）。
3. **相关性精度**：人工复核抽样（全部 `INVALIDATE` + 随机 20% `REVIEW`）的 precision；以 Wilson 95% 下界报告，目标下界 `>= 0.85`。未达标先修相关性规则，不得扩样本口径。
4. **误反证**：`false INVALIDATE = 0`。任何一条被人工复核判定为误反证的 `INVALIDATE` 都阻断影子升级，必须回滚判据并重新预注册。
5. **经济性（仅在 bars 到位后）**：
   - 以事件窗口 bars 计算事件后固定窗口的净 bps（扣费用/滑点）；
   - 按 ET 交易日聚类报告 CI95 与 MDE；
   - 同时报告“误杀赢家”的比例与尾部损失变化；
   - **在 bars 与预注册退出规则到位前，不得报告任何 net bps / avoided PnL / MDE。**
6. **无自动动作**：影子只输出 `NO_CHANGE/REVIEW/INVALIDATE` 标签与证据；任何 live hook（自动平仓、减仓、改尺寸、改止损、通知触发交易）都必须**另立 RFC + 用户显式裁决**，不得随本影子上线。
7. **稳定性**：判据、阈值、别名表、证据层级在样本期内冻结；任何改动 = 新预注册 + 断点哈希，不得跨断点混算。

## 8. CLI 与可复现性

```bash
# 默认：trade 1864 / NVDA afterhours / as_of=exit_time
./venv/bin/python scripts/ext_event_risk_shadow.py

# 指定 as_of（只看到该时刻为止的事件）
./venv/bin/python scripts/ext_event_risk_shadow.py --as-of 2026-11-12T01:44:03Z

# 指定产物路径；若文件已存在则退出码 2，不覆盖
./venv/bin/python scripts/ext_event_risk_shadow.py --out /tmp/nvda-review.json

# 打印完整 JSON
./venv/bin/python scripts/ext_event_risk_shadow.py --print-json
```

- 同一 DB 快照 + 同一参数 ⇒ 相同 `evaluation_sha256` / `payload_sha256`（单测覆盖）。
- `input_hashes.*_db_file_sha256` 是主 `.db` 文件哈希，不含 WAL；活库运行期间文件会变化，跨时点复跑应以 `event_window_rows_sha256` 与 `evaluation_sha256` 对齐口径。
- CLI 对已存在 `--out` 返回 2 并保留原文件；默认产物名含 `evaluation_sha256` 前 12 位，避免无意覆盖。

## 9. 单测覆盖

`tests/test_ext_event_risk_shadow.py`（全部 hermetic tmp DB）：

- NVDA 案例：`REVIEW`、主事件 853、官方 8-K 不反证、原始时间保留；
- 未来事件排除（`first_seen_at > as_of`）；
- 入场前/入场时刻排除（严格 `>`）；
- URL 去重 + 标题去重，保留最早；低重大副本不遮蔽晚到的高重大副本；官方副本不被同标题媒体副本去重隐藏；
- 未验证媒体“CEO 辞职”只 `REVIEW`，不 `INVALIDATE`；
- 官方 + 显式 CEO 辞职 ⇒ `INVALIDATE`；
- 官方 8-K Item 5.02 通用样板 ⇒ 不 `INVALIDATE`；
- 反事实 `unavailable`、`avoided_pnl_usd=null`、`claimed=false`；
- `--as-of` 覆盖后未来官方事件不可见；
- 评估确定性 + 只读不改库；
- CLI 拒绝覆盖已存在产物、成功写新产物含必备字段；
- `decision_id` 不匹配 / trade 不存在时拒绝；
- 无直接相关的板块新闻 ⇒ `NO_CHANGE`。

## 10. 回滚

1. 初版离线阶段停止使用：不再运行脚本即可；当时无 cron/消费者，零生产影响。**后续已通过 RFC-207 注册 PERMANENT_MEASURE 并安装只读 cron，停止方式改为对应环境开关置 0；无自动交易动作。**
2. 彻底摘除：删除 `scripts/ext_event_risk_shadow.py`、`tests/test_ext_event_risk_shadow.py`、本 RFC；产物 JSON 保留作历史证据，不删。
3. 若未来已注册：先从 registry/cron 摘除，再删脚本；任何已产生的 `INVALIDATE` 标签保留但不得回写生产决策。

## 11. 明确禁止的误读

- `REVIEW` ≠ 应该平仓；它只表示“有新重大信息，需要人或后续流程复核”。
- `INVALIDATE` ≠ 已经平仓；本影子没有订单路径。
- `NO_CHANGE` ≠ 事件不存在；它只表示“窗口内没有达到 material 门槛的直接相关事件”，可能包含未采到、未识别、未达阈值的事件。
- `counterfactual.status=unavailable` ≠ 事件没有影响；它表示**没有 bars 就不能量化**，任何“避免了 $X”的说法都是伪造。
- 官方来源存在 ≠ 可以反证；必须命中显式、与标的同文本的反证短语。
- 单案 `REVIEW` 不构成对 Event Intelligence、模型、事件上下文或任何 live hook 的有效性证明。