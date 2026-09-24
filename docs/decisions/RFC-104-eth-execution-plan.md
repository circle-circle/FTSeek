# RFC-104 ETH 执行计划（Execution Plan）：入场侧「盲扫-发现」→「计划-消费」

- **日期**：2026-09-23 | **状态**：**设计定稿，未落地**（阶段 0 待批；本文不改任何交易逻辑）
- **用户裁决**：2026-09-23「各公司财报日是固定的，当日的通过循环拉取并不合理，是否可以类似通过 DA 或者 UE 模式生成盘前/盘后的执行计划」→「先做设计」→「**先写入 RFC**」
- **设计依据**：`docs/designs/DESIGN-20260923-eth-execution-plan.md`（设计树 12 问自问自答 + 底牌清单 + 模块线索）
- **复核日**：**2026-10-08**（与 RFC-100 / RFC-101 / RFC-102 / RFC-103 同批一次仲裁）
- **回滚**：删本 RFC 对应的 cron 行 + 计划目录（计划**只读工件**，删除即静默；消费侧开关回"盲扫路径"一行）
- **非目标（先钉死）**：退出链（`extended_hours_exit_cron` / SL/TP / 孤儿单 / EOD）**一行不动**；RTH 钱链（`app/playbook/*`、`playbook_instructions`）**不动**；RFC-100/102/103 的**闸取值**不动。本 RFC 只改"**谁进闸**"

---

## 1. 命题裁定（先纠预期，防"计划化=多出单"的误读）

| 命题 | 裁定 | 驱动证据（2026-09-23 实测） |
|---|---|---|
| 事件人群可**提前枚举** | ✅ 成立 | 日历窗口已是 **[today−2, today+6]**（futu `days_back=2/days_ahead=7`），实测 40 行 / 7 个交易日，**36 行带相位**；9/24 有 8 行（7 带相位）⇒ **T−1 晚间可生成次日计划** |
| 当天靠循环"发现"不合理 | ✅ 成立 | 今日 **110 轮**，每轮候选 37–52（含 **37–40 只异动榜**）⇒ ≈**4,300 票次**，对事件人群贡献 **0**；反证：9/22（当时**尚无**日历候选源）事件票 **5/5 全靠榜单撞上** |
| 计划化能**多出单** | ❌ **不成立，本文明确写死** | 卡点在 ①量闸墙（event 票量 1 万上下 vs 档位 2 万(04-07h)/20 万(08h+·盘后)）②盘前价差（CBRL 实测 12.4%）③actual 迟到（当日 BMO 5/6 到 08:10 ET 仍缺）—— 三者均与"如何发现人群"无关 |

**本 RFC 的收益定义**（验收以此为准，不以出单数）：

| 收益 | 可测形态 | 现值 → 目标 |
|---|---|---|
| 去无用功 | 结构性死票占用（票次/日） | ≈4,300 → **≈50**（= 计划内条数 × 轮次） |
| 可见性 | 可读工件数 | 4 本拒绝台账 + 3 个影子 → **1 份计划 + 1 本对账** |
| 语义正确 | `waiting_data` 命中率（数据何时到） | 无 → 有（直接服务 RFC-102 复核判据） |

## 2. 设计（数据契约）

### 2.1 工件与幂等键

- 路径：`data/loops/ext_entry_plan/<et_date>_<session>.json`（`session ∈ {premarket, afterhours}`）
- 幂等键：`(et_date, symbol, session, phase)`；**每票一行**，一份计划 = 一个数组
- 写：**原子写**（`app/utils/atomic_write.py`）；读-改-写带文件锁（同 `_state_lock` 惯例）
- 载体现选**文件工件**（不开新表、不碰 `playbook_instructions`）—— 理由见 §2.5

### 2.2 计划卡字段（初版，宁少勿多）

```
{ symbol, et_date, session, phase,                 # phase ∈ {before, after}（多源并集判定）
  sources: [{name, reportDate, timeOfTheDay, asof}],# 每源证据，供相位打架审计（WOR 先例）
  data_expectation: {basis, expected_available_after_et, fallback_days},  # BMO→'08:05'/AMC→'04:00'，复用 RFC-102 §2.6
  gate_snapshot: {min_volume_now, min_price, ...},  # 只作审计与漂移检测，**不作放行依据**
  state, state_since, attempts, reject_reason,      # 状态机（§2.3）
  generated_at, refreshed_at, source_versions, plan_hash }
```

**不含**：方向、入场带、SL/TP、费用/可执行性数字（Q-D 待裁；默认不带 ⇒ 防止"计划里的数字被当成能做"）。

### 2.3 状态机（每票只走一次，替代"每轮从头重算"）

```
planned ──(相位窗到)──▶ armed ──(数据未到)──▶ waiting_data ──(数据齐+过闸)──▶ ordered
                             │                     │
                             └──(闸拒/失效)──▶ rejected(reason)     └──(窗尾)──▶ expired
```

### 2.4 时刻表（生成 / 刷新 / 消费）

| 计划 | 生成 | 刷新 | 消费 |
|---|---|---|---|
| 盘前 | **T−1 20:30 ET** | T−0 **04:00** / **07:00** ET | 现有 `*/5 16-22 CST` 载体，每轮**只推进状态** |
| 盘后 | **T−0 15:45 ET**（BMO 的 actual 那时必已可得） | 可选 17:00 ET | 现有 `*/5 4-8 CST` 载体 |

### 2.5 消费契约（**三条硬不变量**，写进代码注释 + 测试钉死）

1. **计划不含方向**（方向由下单时的门链与方向判据决定）；
2. **计划不含放行**（判定权 100% 在执行时门链；`gate_snapshot` 只是审计）；
3. **计划损坏/缺失 ⇒ 整轮 fail-closed**（不下单、只告警）—— 计划过期最多导致**错过**，绝不下错单。

> 该契约直接对齐框架**已有**的 UE 语义（`enforce_at_execution=true, applied_here=false`）—— 我们借的是"计划-消费"的**形状**，不是它的 LLM。

**为什么不复用 `playbook_instructions`**：实测 `app/playbook/*` 与 `app/execution/*` 均**无延长时段维度**（纯 RTH），加 `session` 维度 = 改 RTH **钱链** schema + 消费者 + 全量回归面；而 ETH 的门与 RTH 完全不同 ⇒ 计划卡大半字段无用。故本 RFC 选**零碰钱链**的最小载体。**榜单/beat/watchlist 三源保留**（降级为观测 + 补漏候选），不删任何源。

## 3. 分阶段（每阶段独立可回滚，禁止合批）

| 阶段 | 做什么 | 通过判据 | 回滚 |
|---|---|---|---|
| **0 · 只读影子** | `scripts/ext_entry_plan.py` 生成计划 + 每日对账；**不消费** | 当日事件票 ⊆ 计划 = **100%**；计划条数 7–9（不是 44） | 删 cron 行 |
| **1 · 影子消费** | 执行器读计划并产出 `would_order / would_reject / waiting_data`，**仍走原判定** | 计划消费结果 vs 现行结果**逐字一致** | 关开关 |
| **2 · 切换消费 + 盲扫降级** | 计划内票进闸；榜单等降级观测 | 出单数**不下降**（预期不变）；票次 4,300 → ≈50 | 开关回盲扫（一行） |
| **3 · 可选入库** | 跨日读数证明字段够用后迁 DB（复用 UE 生命周期语义） | 迁移前后对账一致 | 保留文件工件 |

## 4. 回滚触发（任一成立即回滚到盲扫，无需再议）

1. 计划覆盖率 <100%（当日事件票有落在计划外）——**静默漏票不可接受**；
2. 阶段 1 对账出现任何"计划内票的判定与现行判定不一致"；
3. 出现任何一笔因计划字段（而非事实闸）导致的下单；
4. 计划工件缺失/损坏却未 fail-closed（= 不变量被破）。

## 5. 历史坑映射（防重踩）

| 坑 | 规避 |
|---|---|
| RFC-101（L2 缓存，收益实测 0 已回滚）/ α（现场证伪） | 本 RFC 取代其动机（正解=**人群缩小**，不是"每轮重复"）；**不复用其任何代码**；§1 留痕防重走 |
| 死门三路径（门恒不触发） | `gate_snapshot` + 对账台账标注"量闸在 event 票上当前是**墙**（RFC-103 已量化）"，不假装它是活门 |
| 观测与决策解耦 | 计划只读；榜单降级观测；计划不参与放行（§2.5） |
| 观测量同源 | 门槛快照**必须**调 `EXEC._min_volume_now(session, hour)` 等现函数，禁复制常量 |
| 双实现漂移（相位判据现已 2 处） | 生成器**必须**调 `_session_event_pairs()` + `_parse_earnings_phase()`，并加**第三处**源码契约测试 |
| N4 环境耦合（`.env.us` 一改，依赖"默认关"的用例即红） | 生成/消费均**显式注入**（`session`/`plan_path`），测试用 tmp 路径；开关默认值做源码断言 |
| 棘轮 `except: pass` | 新代码零 `except: pass`；异常必须显式告警 + 降级 |
| 测试污染生产（P1 事故：测试真下 SIM 单） | 生成器**无任何交易调用**；消费侧新测试一律 mock `_open_trade_context` |
| 时区三格式混存 | 计划内一律 **ET 字符串**（`utils/et_time`），禁手写偏移 |
| 共有文件纪律 | 生成器为新文件；改动 `extended_hours_entry_executor.py`（他 agent WIP 在场）须 worktree + 只提自己 hunk |
| 一次一变量 | 本 RFC 只改"人群发现方式"；阶段 0/1/2 分批，不与闸取值改动合批 |

## 6. 审计局限（如实写死）

1. "榜单补漏 = 0"是 **n=1 日**（9/23）读数；9/22 反向为 100%（当时无日历源）⇒ **必须常设读数**（跨日）才能定论；
2. 日历窗口实测有 **5/40 行相位为空**（`EMPTY`）⇒ 相位未知票的计划只能"双窗待命"或"等刷新"；
3. 日历**不含发布时刻**（行字段无 clock time）⇒ 计划只能按**相位窗**调度，不能排到分钟；
4. 单源覆盖有缺口史实（futu 单源 39%）⇒ 生成必须**多源并集**，单源缺失不得导致空计划；
5. 量闸"墙"的读数来自 RFC-103 影子档（每轮 ~1 只在带内），样本仍小。

## 7. 待裁（frontier；推荐已在设计稿 §6 给出）

| # | 问题 | 推荐 |
|---|---|---|
| Q-A | 载体：文件工件 / 新表 / 复用 `playbook_instructions` | **文件工件 + 影子入库**（零碰钱链） |
| Q-B | 计划生成时是否先过**便宜闸**预筛（<$5 等） | **做**，但必须调同一函数，且被筛掉写入 `prefiltered_out[]` 可审计 |
| Q-C | 榜单归宿 | **保留观测 + 补漏**，10/08 跨日读数证明恒 0 再谈关 |
| Q-D | 计划是否带费用/可执行性字段 | **先不带**（防止"看起来能做"的数字） |
| Q-E | 实现方式 | **先本文（RFC-104）→ 再实现阶段 0** |

---

## Addendum: 9/24 阶段 0 补齐批（用户批准）

**日期**：2026-09-24 | **状态**：已落地（工作区改动，未 commit）
**载体**：`scripts/eth_event_plan.py`（升级为单一真源 + 多源生产 + 刷新轮 + actual 到达观测 + 每日对账）

### 改动摘要

#### A. 单一真源（消灭双实现漂移面）
- `parse_earnings_phase = EXEC._parse_earnings_phase`（同一函数对象，非拷贝）
- `session_event_pairs = EXEC._session_event_pairs`（同一函数对象，非拷贝）
- 契约测试：`assert plan_mod.parse_earnings_phase is EXEC._parse_earnings_phase`
- 删除本地拷贝（旧 l.157/173），彻底消除 RFC-104 §5 所列"双实现漂移"历史坑

#### B. 多源并集（默认 futu ∪ Finnhub）
- 默认多源生产（`--finnhub-only` 保留作离线降级）
- futu 连接模式照抄 `rth_fade_shadow.py:futu_rows()`（短生命周期 OpenQuoteContext + finally close）
- Finnhub 复用 `EXEC._finnhub_today_calendar`（共享缓存与客户端）

#### C. 刷新模式（`--refresh`，RFC §2.4 T-0 刷新轮）
- 重拉多源日历 → 与既有计划合并
- 合并语义硬不变量：
  1. state/attempts/state_since **只前进不回退**（按 `_STATE_ORDER` 序数）
  2. ambiguous 行拿到新相位证据 → 迁 planned（记 resolution 来源）
  3. 新发现的票 append 为 planned
  4. plan_hash 幂等（同数据重跑不变）；refreshed_at 更新
- 刷新时顺带推进状态机（advance_state）

#### D. actual 到达观测（新增字段，观测非策略）
- 计划行新增 `actual_arrived_at`（初始 null）与 `actual_value`
- 生成/刷新时查 Finnhub actual（复用 `EXEC._finnhub_today_calendar`）
- **只记第一次**：已有非 null 值时后续刷新不覆盖
- 与 §2.2"宁少勿多"的关系：到达时间是**观测字段**（服务 RFC-102 复核判据与 DEB-20260923 A 十月对照），**不是策略字段**（不影响放行/拒绝判定）

#### E. 每日对账（`--reconcile`）
- 读 executor 真实台账（earnings_gate_rejects / price_gate_rejects / entry_executions）
- 提取"当日实际出现的事件票"集合，与计划比较
- 产物：`data/loops/ext_entry_plan/reconcile_<date>.json` + `reconcile_ledger.jsonl`（累计）
- 判据：missed==[] ⇒ PASS；任何漏票 ⇒ FAIL（exit code 非 0）
- **只读**：不修改任何生产台账

### CLI 用法示例

```bash
# 生成（多源）
PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --dry

# 生成（离线降级）
PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --finnhub-only

# 刷新（T-0 04:00/07:00 ET）
PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --refresh

# 对账
PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --reconcile --date 2026-09-24
```

### cron 行建议（ET → CST 换算，含夏令时注记）

| 时刻 (ET) | CST (冬令 UTC-6) | CST (夏令 UTC-5) | 模式 | 说明 |
|---|---|---|---|---|
| 20:30 T-1 | 08:30 T (周二-六) | 07:30 T | 生成 | T-1 晚间首次生成 |
| 04:00 T-0 | 16:00 T-1 (周一-五) | 15:00 T-1 | 刷新 | 盘前第一轮刷新（ambiguous 自愈窗口） |
| 07:00 T-0 | 19:00 T-1 (周一-五) | 18:00 T-1 | 刷新 | 盘前第二轮刷新（actual 开始到达 BMO 票） |
| 15:45 T-0 | 03:45 T (周二-六) | 02:45 T | 生成 | 盘后计划生成（BMO actual 已可得） |
| 21:00 T-0 | 09:00 T (周二-六) | 08:00 T | 对账 | 收盘后对账（所有事件票已定论） |

**夏令时注记**：CST = UTC-6（11 月初 ~ 3 月中）；CDT = UTC-5（3 月中 ~ 11 月初）。cron 以 ET 为准，系统时区换算自动处理 DST 跳变。

### 禁区（未改动）
- `scripts/extended_hours_entry_executor.py`：零改动（只读 import）
- crontab / CONTROL-LOOP-LEDGER：不动（主会话统一改）
- 无交易行为变更（计划只读工件）

### 测试覆盖
- 契约同一性（2 例）
- 刷新合并语义（6 例：state 不回退 / ambiguous 自愈 / append 新票 / 幂等 / actual 保留 / sources 更新）
- actual_arrived_at 只记首次（2 例）
- 对账 PASS/FAIL 两路 + missed 检出 + arrival stats + ledger append + execution 计入（6 例）
- query_actual_value（5 例：actual present / missing / ticket not found / error / US. prefix）
- 既有 40 例适配重构（REQUIRED_FIELDS 新增 actual_arrived_at/actual_value）