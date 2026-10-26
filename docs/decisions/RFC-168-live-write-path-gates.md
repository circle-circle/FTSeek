---
title: "RFC-168: 实盘写路径闸门统一 + 交易热路径 fail-close"
status: proposed
module: web_dashboard
related_code:
  - app/web_dashboard/runtime_status.py
  - app/web_dashboard/order/executor.py
  - app/web_dashboard/order/validator.py
  - app/web_dashboard/routes/trade.py
  - app/web_dashboard/routes/dashboard.py
  - app/web_dashboard/routes/hk_trade.py
  - app/web_dashboard/hk_order_service.py
  - app/pipeline/order_executor.py
  - app/playbook/pipeline_parts/auto_execute_flow.py
  - app/execution/exit_lifecycle.py
  - tests/conftest.py
related_okr: OKR-2026-11
related_deb: DEB-20261026-p0-write-path-gates
tags: [live-trading, gates, fail-close, security, sqlite-guard, shadow]
updated: 2026-10-26
---

# RFC-168: 实盘写路径闸门统一 + 交易热路径 fail-close

> **状态**: 提案（用户 2026-10-26 裁「按照建议执行」，指本轮整体审计建议）
> **性质**: 安全收紧批。**不引入新入场假设、不改信号分布、不改退出行为、不改仓位/止损止盈/频率、不切实盘**。
> **日期**: 2026-10-26
> **作者**: Qoder (Bot)
> **审批人**: 项目所有者

## 摘要

本 RFC 处理 2026-10-26 全系统审计发现的 8 项 P0 缺口，集中在两类：

1. **Dashboard 有 5 条实盘写路径绕过统一闸门**，其中 `runtime.capabilities` 断线只读态是纯前端提示层（服务端零消费），`modify_sl` 完全不经过 `validate_order` 且不持订单锁。
2. **交易热路径 5 处 `except: pass` 把安全门降级为 fail-open**——持仓查询异常时重复开仓拦截失效、回撤期仓位乘数保护静默失效、闪退熔断状态不落盘。

外加两项基础设施缺口：测试写闸不挂 `sqlite3.connect`（19 个生产库路径无防护）；出场意图跨双库写入无事务且无 reaper（`FILLED_UNBOOKED` 可永久冻结标的）。

**核心原则（本文档最重要的一条）**：

> **统一的是「校验范式」，不是「同一套闸门」。**
> 收紧只允许作用在**开新仓**与**降低保护**两个方向；**平仓、撤单、改善止损**三个方向必须保持可达。
> 把开仓的金额上限/当日单数/购买力套到平仓路径上，会制造「更安全但更危险」的系统——它会在该减仓的时候拒绝减仓。

## 问题陈述

### 1. Dashboard 五条写路径绕过闸门

主下单路径 `POST /api/orders`（`routes/trade.py:90` → `order/executor.py:158`）闸门最全：幂等判重、`_order_lock`、Kill Switch、参数白名单、单笔 $50K、当日 20 单、购买力双确认、日回撤熔断、资本安全 BLACKOUT、手动预算 80%、限价偏离拦截。以下路径均无等价保护：

| 路径 | 缺口 | 证据 |
|---|---|---|
| `POST /api/positions/modify-sl` | 完全绕过 `validate_order`；无 kill switch / 金额上限 / 当日单数；**不持 `_order_lock`**；broker 下单失败时**仍更新 DB** | `order/executor.py:1159` |
| `POST /api/orders/cancel` | 零闸门；仅 order_id 字符白名单；**不校验订单归属本账户** | `routes/trade.py:198` |
| `POST /api/protection/confirm/{symbol}` | 零闸门；async handler 内**同步**调券商 RPC，阻塞事件循环 | `routes/dashboard.py:396-404` |
| `POST /api/hk/orders` | 无 kill switch / 无资本水位 / 无回撤熔断 / 无预算 | `hk_order_service.py:193-252` |
| `runtime.capabilities` | 只在前端消费；`executor` / `validator` 零引用 | `runtime_status.py:14` 自述「前端提示层，后端 validator/executor 仍是最终防线」 |

第 5 项是本 RFC 的核心。RFC-165 Batch 2 引入的断线只读态在**展示层**完备（横幅、按钮禁用、摘要点「离线」），但服务端无强制：券商断线或持仓 DB 兜底时，直接 `POST /api/orders` 仍会成交。**「断线只读」目前是 UI 锁，不是安全锁。**

> **审计更正（2026-10-26 实施期核实）**：`modify_stop_loss` 的「broker 失败仍更新 DB」一项**不成立**——
> DEB-20260903 #119 已于 `executor.py:1283` 提前 return（`status="broker_failed"`、错误文案「DB 未更新」），
> `executor.py:1277` 的 "DB 仍更新" 只是过期日志文本。本 RFC 不重复修该项（§边界 中注明）。
> 另：`modify_sl` 的方向判据在本文初稿中写反（写成「多头新 SL 不得高于旧 SL」）——
> 多头 SL **上移**才是收紧保护，按初稿实现会把「改善止损」拦掉，正是本文件 §提议方案 A
> 明令禁止的「更安全但更危险」。实施期已更正为方向性判据（见方案 A 表）。

### 2. 交易热路径 fail-open

```python
# app/pipeline/order_executor.py:2023  HIGH_VOL regime 门
except Exception:
    pass          # → 门失效仍继续下单

# app/pipeline/order_executor.py:4131  多仓拦截
except Exception:
    pass          # → 重复开仓拦截完全失效

# app/pipeline/order_executor.py:3407  闪退熔断状态落盘
except Exception:
    pass          # → 重启即失效

# app/playbook/pipeline_parts/auto_execute_flow.py:632  仓位水位乘数
except Exception:
    pass          # → _water_mult 停在 1.0，回撤期仓位保护静默失效
```

同一文件内 `order_executor.py:3878` 的 Kill Switch 检查是**正确的 fail-close**（异常即不下单，注释标注 RFC-060 P1.2）。两种哲学并存于同一执行路径。

### 3. 测试写闸漏挂 sqlite3

`tests/conftest.py:143-220` 的生产写闸只挂 `builtins.open` / `Path.open` / `write_text` / `write_bytes` / `os.replace` / `os.rename`。`sqlite3.connect` **完全未挂**（conftest 第 67 行自认此事）。`app/`+`scripts/` 共引用 19 个生产库文件名，`_PRODUCTION_DATA_SEAMS` 只登记 1 个。`app/event_stream/store.py:18-19` 的 `EventStore()` 默认路径即生产库 `data/event_stream.db`（1.6GB），构造即 `CREATE TABLE` 写入。

### 4. 出场意图跨库写入无事务

`app/playbook/pipeline.py:3690` 先在 DB A（`trade_records`）落账 → `:3704` 才 ack DB B（`exit_lifecycle` 独立 sqlite，`app/core/paths.py:39`）。两库无跨库事务。进程若在两者之间崩溃 → intent 停在 `FILLED_UNBOOKED` → `exit_lifecycle.py:192` 的 `exit_symbol_busy` **冻结该标的所有后续出场** → `:423` 的 `REFUSE_UNBOOKED` 拒绝人工释放（`scripts/release_stuck_exit_intent.py:196` 同样拒绝）。当前无 reaper。

> 现状：229 条 intent 全 `BOOKED`，非 BOOKED 为 0，**该窗口当前为空**——本项是预防性修复，非救火。

### 5. 日志归档密钥明文

`logs/_archive_20260910/backfill_manual.log-20260710:89` 含 Twelve Data API key 明文（URL query `apikey=` 形式）。该 key **仍在生产使用**（`app/core/atr_provider.py`、`app/risk/data_health_guard.py`、`app/pipeline/*` 等依赖）。已复核其余 9 个 `.env.us` 凭据在 `logs/` 下均无明文残留。

## 提议方案

### 方案 A：Dashboard 写路径校验范式统一

统一的是三元范式，不是同一套闸门：

```
(1) 归属校验  — 标的/订单/持仓必须属于本账户本看板
(2) 方向校验  — 该操作是否降低保护？降低保护 → 收紧；提高保护 → 保持可达
(3) 失败方向  — 校验不通过 → fail-close + 走 app/push/ hub 告警
```

逐路径设计：

| 路径 | 归属校验 | 方向校验 | 失败方向 |
|---|---|---|---|
| `modify-sl` | 标的须在持仓台账 | 多头 SL **下移** = 降低保护 → 需显式确认；上移 = 收紧 → 放行。空头反之 | 收紧（把止损改坏 → 拒绝，除非用户确认） |
| `cancel` | order_id 须属本账户（本地审计台账或 broker 订单列表） | 不适用 | 收紧（撤他人单 → 拒绝） |
| `protection/confirm` | 标的须在持仓台账 | 不适用 | 收紧（无仓可平本就无意义，不妨碍正当减仓） |
| `hk/orders` | 现有 | 无变化 | 补 kill switch + 资本水位 + 回撤熔断（**开仓侧**，收紧） |

**`modify-sl` 的方向校验是本方案的核心**：它拦「把止损移到更差的位置」，但**放行「把止损移到更好的位置」**。这是唯一能让「收紧」与「不制造更危险」同时成立的判据。

> ⚠️ **初稿方向写反（实施期已更正）**：初稿写「多头新 SL 不得高于旧 SL」—— 方向相反。
> 多头 SL **上移**才是收紧保护（更靠近现价），下移是放宽。按初稿实现会把「改善止损」
> 拦掉，即在用户最需要降低风险时收紧它。实施期改为方向性判据：
> 多头 `new_sl < old_sl` = 降低保护（拒绝，除非 `acknowledge`）；空头 `new_sl > old_sl` = 降低保护。
> 另：SL 越过入场价 = 触发即亏损离场 = 极端的降低保护，一并拒绝。

**「降低保护」的落地方式 = 需显式确认而非硬拒**：放宽止损常是用户主动规避噪音扫出的正当诉求，
一刀切禁掉会制造新的摩擦。因此 `POST /api/positions/modify-sl` 新增可选参数
`acknowledge_weaken_stop`，未带该标志时服务端拒绝并说明原因。

**判据不可判定时的失败方向（实施期补充）**：台账读失败 / broker 不可达 ⇒ 归属**不可判定**。
此时按操作性质分流：**开仓方向 fail-close**（读不到权威状态就不下单）；
**平仓 / 撤单 / 改止损方向 fail-open + 告警**（判据不可用不应阻断减仓）。
「查不到」与「不属于」必须区分——前者是判据不可用，后者是真正的归属违规。

**DB 一致性**：本项**无需修**（见 §问题陈述 的审计更正：DEB-20260903 #119 已修）。

**异步修正**：`routes/dashboard.py:404` 与 `routes/risk.py:397` 的同步券商 RPC 改 `asyncio.to_thread`（`routes/trade.py` 已全量改造，DEB-20260904 #S12，此处是漏改）。

### 方案 B：runtime capabilities 升为服务端前置判据

服务端在**开仓**前实时确认两件事（`runtime.capabilities` 的服务端等价物）：

- **账户资金可实时确认**（`accinfo_query` 成功且非空）→ 对应 runtime 的 `account_known`；
- **当前持仓可实时确认**（`position_list_query` 成功）→ 对应 runtime 的 `positions_state == 'live'`。

查不到 = 判据不可用 = **fail-close**（开仓方向读不到权威状态就不下单）。
被拒时走 `app/push/` hub 告警（「无料不推」门：拒绝事件本身是状态变化，故推）。

- **只作用于开新仓**（`BUY LONG` / `SELL SHORT`）。平仓单**不**受此约束——断线时恰恰最需要能平仓。
  判据：`submit_order` 内按 `order.action` × `order.direction` 判定开/平，与
  `order/validator.py:106` 的 `_is_opening` 口径一致。
- **开关**：**环境变量**而非 yaml。理由：热重载有 60s 延迟窗，安全闸门需要即时生效；
  且 yaml 有 1388 个键，多一个键增加漂移面（并会触发 SSOT 文档重生成）。
  - `FTSEEK_DASHBOARD_WRITE_GATES=off` → 全部闸关闭
  - `FTSEEK_DASHBOARD_GATE_<NAME>=off` → 单闸关闭（`PLACE`/`CANCEL`/`MODIFY_STOP`/`PROTECTION_CONFIRM`/`HK_PLACE`）

### 方案 C：交易热路径 fail-open 改 fail-close

- `order_executor.py:2023`（HIGH_VOL regime 门）：异常即拒绝下单。
- `order_executor.py:4131`（多仓拦截）：异常即拒绝下单（并释放标的锁，防锁泄漏）。
- `order_executor.py:3407`（闪退熔断状态落盘）：异常即抛出，由调用方
  `position_monitor.py:908` 记 **warning**（原为 debug ⇒ 静默）。保守方向是「继续拦截」。
- `auto_execute_flow.py:632`（`_water_mult`）：异常时改用**保守档 0.25**（ORANGE 档，既有档位非凭空值）
  而非初值 `1.0`；`_regime_mult` 异常时用 `CAUTIOUS` 0.5（与既有 dict 默认一致）。
  非零而非归零：归零会造成「静默停止下单」的观感故障。

### 方案 D：测试 sqlite3 写闸

`tests/conftest.py` 增加 `sqlite3.connect` 包装：生产库连接挂 **write 闸**，只读全放行。

**拦 connect 会被打成假红**——仓内 30 个测试文件直连生产库做只读断言。
故采用**写语句级**闸：只读断言零影响，写入当场红。

**机制选型（实施期实测）**：

| 手段 | 结果 |
|---|---|
| `set_trace_callback` | ❌ 回调内异常被 CPython **静默吞掉**（`PyErr_Clear`）⇒ 拦不住 |
| `set_authorizer` + `SQLITE_DENY` | ✅ prepare 阶段拒绝，抛 `not authorized`；回调内异常同样被吞，故用错误消息而非自定义异常做断言 |

- 顺序约束：`db_utils._safe_connect` 在 import 期捕获 `sqlite3.connect` 引用 ⇒
  **本 patch 必须早于任何 app 模块 import 落地**（置于 conftest 顶部）。
- 违规明细记入会话级账 `tests/conftest.py::_PROD_DB_VIOLATIONS`，会话结束统一报红——
  防生产代码 `except sqlite3.Error: pass` 把违规吞掉导致用例假绿。
- **已知存量豁免**（闸上线当日即抓出）：`data/ftseek_us.db`（`pdt_guard.py:64` 经 `get_main_db()`
  直连）与 `data/trail_state.db`（`trail_state_store.py:31` 硬编码路径）按**库级**登记豁免，
  但**计数并展示**在报告里（存量污染仍在发生，必须可见可跟踪）。
  未登记库的写入仍当场红——那才是闸要抓的「新增污染」。

### 方案 E：exit_intents reaper（**影子模式**）

新增 `scripts/exit_intent_reaper.py`：**只检测 + 告警，不改任何状态**。

- 检测条件：`exit_intents.state = 'FILLED_UNBOOKED'` 且对应 `trade_records.exit_time` 非空
  （按 `owner_json` 的 `db_path` + `trade_id` 跨库交叉核对），且超观察窗（`STALE_AFTER_S=300`，排除正在 ack 的在途）。
- 动作：写影子记录（`data/shadow/exit_intent_reaper.jsonl`）+ 走 `app/push/` hub 告警；**不调用 `ack_booked`**。
- 注册进 `app/playbook/shadow_registry.py`（未注册 = 禁止态）。
- 挂载 cron：每 5 分钟（备份 `data/crontab.backup.20261026_exit_intent_reaper.txt`）。
- 观察 ≥3 个交易日无异常后，另行提案启用自动补 ack（届时需单独立项，因为会改写出场状态机）。

### 方案 F：日志密钥脱敏

覆写 `logs/_archive_20260910/backfill_manual.log-20260710` 中的 key 明文（已完成，1 处 → 0，复扫全 `logs/` 0 命中）。**key 轮换由用户执行**——本 RFC 不代持凭据、不改 `.env.us`。

轮换提示：替换 `.env.us` 的 `TWELVEDATA_API_KEY` 后需重启 `ftseek-us`（多处模块在导入期读取）。Twelve Data 是 ATR 与数据健康守卫的主源之一，轮换窗口建议避开盘中。

## 影子先行与开关

| 子项 | 影子先行 | 开关 | 回滚 |
|---|---|---|---|
| A 写路径校验 | 拒绝计数写入影子记录 + 告警 | 逐路径独立开关（`dashboard.gate_modify_sl` / `gate_cancel` / `gate_protection_confirm`） | 关开关即回滚 |
| B capabilities | 首次启用记 `capability_denied` 影子计数 | `dashboard.runtime_capability_enforce` | 置 `false` |
| C fail-close | 不适用（纯异常路径，正常流程零影响） | 无（异常路径本无正确行为） | git revert |
| D sqlite3 闸 | 不适用（测试层） | 无 | git revert |
| E reaper | **全程影子**，只检测不动作 | `exit_intent_reaper.enabled` | 停 cron |
| F 脱敏 | 不适用 | 无 | 不可逆（仅影响归档日志证据文本） |

## 验证判据

- 定向契约：新增 `tests/web_dashboard/test_write_path_gates.py`、`tests/test_trade_hot_path_failclose.py`、`tests/test_exit_intent_reaper_shadow.py`、`tests/test_conftest_sqlite_guard.py`。
- 现有回归不得转红：`tests/web_dashboard`（164 passed）、`tests/execution`、`tests/core`、`tests/playbook`（1158 passed）。
- 全量 `pytest tests -q` 与基线对比，**不得新增失败**。
- 重启后必跑启动冒烟 7/7：`systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90`。
- 重启前核：在途出场 SQL + `git status --porcelain -- app/ scripts/`。

## 边界（明确未做）

- 不改四锁（`ALLOW_REAL_TRADING=false` / `execute_mode: futu_simulate` / `AUTO_TRADE_ACCOUNT=sim` / 毕业门禁 fail-close）。
- 不改仓位、止损止盈、交易频率、信号分布、退出逻辑。
- 不切切实盘；不新增入场假设（OKR-2026-11 §1 继承裁决 4：C1 至多一个新入场假设，本批 **0 个**）。
- 不新增直连 notifier 的推送（KR5）。
- 不改 `auto_trading_enabled` 的三处裸 regex 读盘（AGENT.md 军规：刻意设计）。
- 不动 `exit_reason` / `regime` 等枚举收敛（属 P2，另批）。
- 不代持、不轮换任何凭据。
- 不修 `modify_stop_loss` 的 DB 一致性（DEB-20260903 #119 已修，见 §问题陈述 审计更正）。
- 不启用 exit_intents 自动补 ack（影子期只检测）。
- **已知存量生产库污染未解**：`pdt_guard.py:64` 与 `trail_state_store.py:31` 在测试中仍写生产库，
  本批只做「登记豁免 + 计数展示」，未改造写入点（涉及风控与路径解析语义，另批）。

## 实施记录（2026-10-26）

### 新增

| 文件 | 作用 |
|---|---|
| `app/web_dashboard/order/write_gates.py` | 服务端写路径闸门（归属 / 方向 / fail 方向 / 环境变量开关） |
| `scripts/exit_intent_reaper.py` | 出场意图账实不符巡检（影子：只检测+告警） |
| `tests/test_conftest_sqlite_guard.py` | 生产库写闸契约（19 例） |
| `tests/web_dashboard/test_write_path_gates.py` | 写路径闸门契约（21 例） |
| `tests/test_trade_hot_path_failclose.py` | 热路径 fail-close 契约（6 例） |
| `tests/test_exit_intent_reaper_shadow.py` | reaper 影子契约（9 例） |

### 修改

| 文件 | 改动 |
|---|---|
| `tests/conftest.py` | 新增 sqlite3 生产库写闸（authorizer）+ 违规会话账 + 已知存量豁免 |
| `app/web_dashboard/order/executor.py` | `submit_order` 加开仓前置；`cancel_order` 加归属校验；`modify_stop_loss` 加闸 + 串行化 |
| `app/web_dashboard/manual_order_service.py` | `modify_stop_loss` facade 加 `acknowledge` 参数 |
| `app/web_dashboard/routes/trade.py` | modify-sl 路由传 `acknowledge_weaken_stop` |
| `app/web_dashboard/routes/dashboard.py` | `protection/confirm` + `protection/stoploss` 加归属校验；同步 RPC 改 `to_thread` |
| `app/web_dashboard/routes/hk_trade.py` | 港股下单补全局闸（kill switch / 资本水位 / 回撤熔断） |
| `app/pipeline/order_executor.py` | 3 处 fail-open → fail-close（HIGH_VOL 门 / 多仓拦截 / 闪退熔断落盘） |
| `app/pipeline/position_monitor.py` | 闪退熔断落盘失败日志 debug → warning |
| `app/playbook/pipeline_parts/auto_execute_flow.py` | 仓位乘数失败降保守档（0.25 / 0.5）而非停在 1.0 |
| `app/playbook/shadow_registry.py` | 注册 `exit_intent_reaper_shadow`（第 97 项） |

### 运维

- cron 新增 1 条（每 5 分钟）：`scripts/exit_intent_reaper.py`；备份 `data/crontab.backup.20261026_exit_intent_reaper.txt`。
- 日志脱敏：`logs/_archive_20260910/backfill_manual.log-20260710` 的 Twelve Data key 明文已覆写（1 → 0，复扫全 `logs/` 0 命中）。

### 实施期发现的三处设计修正

1. **SL 方向判据写反**（本文初稿）→ 实施期更正为方向性判据（详见 §方案 A 的警示框）。
2. **闸门的 fail 方向必须按操作性质分流**：初版一律 fail-close，实测把「改止损」这类**保护动作**
   在台账读失败时拦下——正是本文件明令禁止的「更安全但更危险」。改为开仓 fail-close /
   保护方向 fail-open + 告警。
3. **闸门机制不能用 `set_trace_callback`**：回调异常被 CPython 静默吞掉。改用 `set_authorizer`。

### 影子期结论（待观察）

- `exit_intent_reaper_shadow`：2026-10-26 起每 5 分钟巡检。观察 ≥3 个交易日无检出后，
  方可提案启用自动补 ack（须另立 RFC + 用户批准）。

## 附录：审计证据索引

| # | 发现 | 证据 |
|---|---|---|
| 1 | `modify_sl` 绕过 `validate_order`、不持锁 | `app/web_dashboard/order/executor.py:1159` |
| 2 | `cancel` 零闸门 | `app/web_dashboard/routes/trade.py:198` |
| 3 | `protection/confirm` 零闸门 + 同步 RPC | `app/web_dashboard/routes/dashboard.py:396-404` |
| 4 | HK 下单无全局闸 | `app/web_dashboard/hk_order_service.py:193-252` |
| 5 | capabilities 服务端零消费 | `app/web_dashboard/runtime_status.py:14,188` |
| 6 | 4 处 `except: pass` fail-open | `app/pipeline/order_executor.py:2023,3407,4131`；`app/playbook/pipeline_parts/auto_execute_flow.py:632` |
| 7 | conftest 未挂 sqlite3 | `tests/conftest.py:67,143-220` |
| 8 | 出场跨库无事务 + 无 reaper | `app/playbook/pipeline.py:3690,3704`；`app/execution/exit_lifecycle.py:192,423` |
| 9 | 日志 key 明文 | `logs/_archive_20260910/backfill_manual.log-20260710:89` |
