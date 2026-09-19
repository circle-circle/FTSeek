# RFC-090: 实盘切换「开关面收敛」+ 原子切换协议 (P0 前置清单)

> **状态**: 设计先行 / 待用户裁决 (四项, 见 §8)。**零行为改动** (本文件不翻任何开关, 不触碰执行链)。
> **日期**: 2026-09-20
> **作者**: Hermes (用户 9/20 令「需要」→ 落成文档走独立复查流程)
> **红线**: 维持 C2「打标前严格不切实盘」。切换本身仍按 RFC-062 两门一梯 + 用户签字 + `CUTOVER-DECISION-PACKAGE-20260909` §一 runbook。
> **承接 (不重复发明)**:
> - `RFC-062` 实盘切换预注册 (门 1/门 2 判据已由 CUTOVER 决策包 §四 重裁沿用)
> - `docs/decisions/CUTOVER-DECISION-PACKAGE-20260909.md` — A' 两账本永久物理分离 / 翻转日 runbook / FLAT_ALL / 陈旧行
> - `docs/PROPOSAL-20260909-cutover-dual-arm-routing.md` §三·五 — **单库翻转模型** (S2 已证伪作废)
> - `RFC-078` 看板库级隔离 + `RFC-080` remark 认领白名单 / S2' 半翻互锁
> - `docs/ADJUDICATED-CRITERIA.md` C 类红线
> **本 RFC 的唯一增量**: 上述文件覆盖的是「切换后账本/归属/政策」;**本文覆盖「切换开关面本身」** —— 即同一个决定今天被 6+ 个解析器、2 族读法、4 种戳法分别表达所产生的裂缝。这些裂缝**今天就已存在** (不是切换日才出现), 其中 3 条在现网配置下即可复现。
> **变更类型**: L5 (纯文档) → 后续 P0 项各自 L1-L2 单独走 Gate。

---

## 1. 目标与非目标

**目标**: 把「切实盘」从「同时对齐七个开关」降为「翻一个开关 + 启动自检」, 并让任何半翻态在启动期硬失败而不是静默降级。

**非目标 (明确排除)**:
- 不新增台账 / 不新增配置文件 (全部改既有解析器, 见 §7)。
- 不改交易逻辑、不改阈值、不改退出规则集、不改策略白名单 (P0-5 只改**失败策略**, 不改规则)。
- 不重裁门 1/门 2 判据 (沿用 CUTOVER 决策包 §四)。
- 不动 `open_positions` 主键 (P0-4 给最小落法 + 另立评估项, 见 §4)。
- 不做实盘额度承诺 (给比例口径, 绝对金额留用户签字, 见 §8-3)。

---

## 2. 已有资产 (逐条实测, 先别重复造)

| 能力 | 位置 | 实测结论 |
|---|---|---|
| 模式判定 SSOT (含授权门) | `app/core/trading_mode.py:150-169` `_detect_mode()` | 每次读 `.env.us` (热加载), 判定源 = `AUTO_TRADE_ACCOUNT` (+`ALLOW_REAL_TRADING` 门) → `SIMULATE / LIVE_CONSERVATIVE / LIVE_NORMAL` |
| 启动校验 | `trading_mode.read_startup_mode_snapshot()` + `validate_startup_mode_config()` | 现被 `app/playbook/candidate_eligibility.py:52-66` 以 **fail-runtime** 方式使用 (不通过即抛错) |
| 启动硬门 (带授权) | `app/core/config_container.py:41-64` `_resolve_trade_mode()` | `TRADE_ENV=0` 且 `ALLOW_REAL_TRADING!=true` → 忽略并强制 `SIMULATE` (仅 `warnings.warn`) |
| 切换 UI | `app/web_dashboard/mode_switch_service.py:199-305` | 写 `.env.us` (4-5 键, 带 fcntl 锁 + 备份) + `CONFIRM` + `REAL_ACCOUNT_ID`/`FUTU_UNLOCK_PWD_MD5` 前置 + 毕业门禁 + **自动 `systemctl restart ftseek-us`** + 60s 就绪轮询 |
| 双账户 + 实盘券商 STOP | `app/execution/dual_broker.py:492-528` | real 分支真挂 `OrderType.STOP` (`remark=pb_stop_*`); sim 直接 `skipped` (由规则引擎兜底) |
| live STOP 接线现状 | `app/playbook/playbook_service.py:2053-2060` | 已接线, 但**失败只 `logger.warning`** → 见 §3.5 |
| 下单路径三态门 | `app/playbook/playbook_service.py:1216-1262` | ① S2' 半翻互锁 (real 臂要求 `ALLOW_REAL_TRADING=true` **且** DB 路由已是 live, 读不到即 fail-close 降 sim) ② 毕业门禁 (异常亦降 sim) |
| 账务列 | `open_positions`: `account_type`/`owner`/`sl_broker_order_id`/`tp_broker_order_id`; `trade_records`: `mode`/`trade_mode`/`data_quality` | 列都在 (实测 `PRAGMA table_info`), 但语义未收敛 → 见 §3.3 / §3.6 |
| 看板手动实盘账本隔离 | `app/core/db_utils.py:192-207` `get_dashboard_db()` (RFC-078) | 看板恒写 live 库, 与 `TRADE_ENV` 解耦; `FTSEEK_DASHBOARD_DB_ISOLATION=0` 一键回滚 |
| DB 路由进程内固化 | `app/core/db_utils.py:76-120` (`R-3`) | `is_live_mode()` 首调固化 (防运行期半路换库产生撕裂账本) |
| 实盘成交推送 | `FTSEEK_LIVE_DEAL_PUSH` (RFC-078 批 E) | 秒级落账 + 飞书 |

---

## 3. 七条裂缝 (逐条实测, 非推测)

> 每条给: **事实 → 证据 (可复跑) → 后果 → 现网是否可复现**。凡我此前口头版本被实测推翻的, 在 §3.8 显式更正 (不悄悄换题)。

### 3.1 裂缝①: 库路由有 6 个解析器, 授权门只有 3 处

**事实**: 「模式判定」带授权门, 「库路由」不带 —— 两者独立解析同一个 `TRADE_ENV`。

| 族 | 位置 | 读法 | 带授权门? |
|---|---|---|---|
| 模式判定 | `config_container.py:47-67` `_resolve_trade_mode()` | `os.getenv` | ✅ (`ALLOW_REAL_TRADING`) |
| 模式判定 | `trading_mode.py:163` | `.env.us` 文件 | ✅ |
| 模式判定 | `di_container.py:144` | `os.getenv` | ✅ (强制降级 sim) |
| 模式判定 | `playbook_service.py:1230` | `os.getenv` | ✅ (fail-close 降 sim) |
| **库路由** | `config_container.py:117` (`PathConfig.db_path`) | `os.getenv` | ❌ |
| **库路由** | `app/core/paths.py:74` | `os.getenv` | ❌ |
| **库路由** | `app/storage/db_pool.py:45` | `os.getenv` | ❌ |
| **库路由** | `app/plan/position_intelligence.py:118` | `os.getenv` | ❌ |
| **库路由** | `app/core/db_utils.py:62-73` → `106-134` | **文件** (首调固化) | ❌ |
| **库路由** | `scripts/daily_ic_analysis.py:53`, `scripts/update_signal_lifecycle.py:34` | `os.getenv` | ❌ |

**可复跑**:
```bash
grep -rn "TRADE_ENV" app/ scripts/ --include=*.py | grep -E "getenv|_parse_trade_env_from_file"
```
**后果**: `TRADE_ENV=0` + `ALLOW_REAL_TRADING=false` 时, 判定 = `SIMULATE` 但库 = `ftseek_us_live.db`。**该自毁路径已被本项目代码注释自己记录在案** (`app/core/db_utils.py:76-82`: 「库切了而 broker 仍是 SIMULATE; 此时 `monitor_playbook_sim_exits` 查到 `rows=[]` 直接 return 0 → 全部在仓静默失去止损监控, 零告警」)。附注: 该注释引用的 `pipeline.py:6890` 行号已漂移, 现监控函数在 `app/playbook/pipeline.py:7920-7965` (证据漂移本身说明这类注释不能替代断言)。

**现网可复现?** 现网 `.env.us` = `TRADE_ENV=1` + `ALLOW_REAL_TRADING=false` ⇒ 两族解析结果一致 (**否**)。触发条件 = 单改 `TRADE_ENV=0` (或 `FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH=1` 时的运行期改文件)。

### 3.2 裂缝②: 同一个授权开关有两族读法 (分裂脑)

**事实**: `ALLOW_REAL_TRADING` 有 7 个读取点, 分两族且不同源:

| 族 | 位置 | 语义 |
|---|---|---|
| 进程值 `os.getenv` | `config_container.py:55`, `di_container.py:144`, `scheduler_core.py:1781`, `playbook_service.py:1230` | systemd `EnvironmentFile` 注入 ⇒ **启动值冻结** |
| 文件值 | `trading_mode.py:163` (`_read_env_file`), `scripts/daily_alert_summary.py:404`, `app/web_dashboard/order/executor.py:409` (`service._read_env`) | **实时读 `.env.us`** |

**可复跑**:
```bash
grep -rn "ALLOW_REAL_TRADING" app/ scripts/ --include=*.py | grep -E "getenv|_read_env"
systemctl cat ftseek-us | grep EnvironmentFile     # → /opt/ftseek/FTSeek-US/.env.us
grep -nE "TRADE_ENV|AUTO_TRADE_ACCOUNT|ALLOW_REAL_TRADING|LIVE_CONSERVATIVE" .env.us
# 实测: TRADE_ENV=1 / AUTO_TRADE_ACCOUNT=sim / ALLOW_REAL_TRADING=false / LIVE_CONSERVATIVE=1
```
**后果**: 用看板/手改 `.env.us` 后不重启 ⇒ 文件族认为「已授权」, 进程族认为「未授权」。方向不统一 (有的 fail-close 降 sim, 有的认为可 live)。

**现网可复现?** **是** —— `LIVE_CONSERVATIVE=1` 且 `ALLOW_REAL_TRADING=false` 属"半态"(实盘系数在位、授权关闭), 任何一次手改授权位而不重启即进入分裂窗口。

### 3.3 裂缝③: 模式戳四种写法, 消费者仍在比字面量

**事实 (写方)**:

| 写方 | 值 | 落表 |
|---|---|---|
| `app/core/position_sync.py:200` | `'live'` / **`'sim'`** | `trade_records` (sync_recovery_backfill) |
| `app/core/position_sync.py:1824` | `'live'` / `'simulate'` | `trade_records` (外部平仓) |
| `app/playbook/playbook_service.py:1274` | `'live'` / `'simulate'` | `trade_records` / `open_positions` |
| `app/playbook/candidate_eligibility.py:260` | 硬编码 `"sim"` | 候选批次 |
| `app/orb/engine.py:1753` | 固定 `"simulate"` | `trade_records` / `signal_lifecycle` |

**实测落库分布 (2026-09-20)**:
```bash
sqlite3 data/ftseek_us.db      "SELECT trade_mode, COUNT(*), MIN(trade_date), MAX(trade_date) FROM trade_records GROUP BY 1;"
# → simulate | 867 | 2026-04-24 | 2026-09-18
#   sim      |   8 | 2026-07-20 | 2026-09-17   ← 7 行来自 position_sync:200, 1 行来自 archive/adopt_position_to_playbook.py:100
sqlite3 data/ftseek_us_live.db "SELECT trade_mode, COUNT(*) FROM trade_records GROUP BY 1;"   # → live | 14
```
**消费者字面量 (抽样)**: `app/plan/plan_feedback.py:246` `trade_mode != 'simulate'` (**那 8 行 `'sim'` 会被当成"非 sim"计入**)、`app/plan/llm_validator.py:502` (注释记录过 `!= 'simulate'` 曾把 313 笔 `PLAYBOOK_SIM` 全挡在门外)、`app/review/weekly_review.py:67` `trade_mode != 'live'`、`app/diagnostics/shadow_account.py:143`、`app/web_dashboard/routes/analytics.py:374` 与 `data_service.py:1187` (`COALESCE(trade_mode,'simulate')='live'`)。

**来源逐一核对 (2026-09-20 复查后精确化)**:
```bash
sqlite3 data/ftseek_us.db "SELECT id, symbol, entry_reason FROM trade_records WHERE trade_mode='sim' ORDER BY id;"
# 7 行 entry_reason='sync_recovery_backfill' (position_sync.py:200)
# 1 行 US.EOG 2026-07-20 entry_reason='adopt_to_playbook' (archive/adopt_position_to_playbook.py:100, 硬编码 'sim')
```

**附带缺口 (复查发现)**: 那唯一非 `position_sync` 来源的行出自一次性脚本 `scripts/archive/adopt_position_to_playbook.py`; 而**活跃代码仍在指引人工执行已不存在的路径** —— `app/core/position_sync.py:136` 与 `:223` 写「请人工执行 `scripts/adopt_position_to_playbook.py <symbol>`」, 实测该路径**不存在** (文件已在 `scripts/archive/` 下) ⇒ 幽灵仓/两不管仓的人工收编出口在文档层即失效 (列 §4-P1-13)。

**同族地雷 — `data_quality` 默认值**: 两库 DDL 均 `data_quality TEXT DEFAULT 'real'`, 实测 sim 库 `data_quality='real'` **603 行** (clean 265 / duplicate_external_close 5 / outlier_external 1 / direction_mismatch 1), live 库 `real` 仅 2 行 (`manual` 12 行)。⇒ 该列今天几乎不承载"真钱"语义, 任何「按 data_quality='real' 取实盘」的写法都会吞掉 600+ 行模拟数据。

**该地雷已有活体消费者 (复查实锤, 非假设)**: `scripts/daily_violation_scan.py:160` 的 `WHERE t.data_quality = 'real'`, 库路由走 `get_main_db()` (`:72`) ⇒ 现网 `TRADE_ENV=1` 下它扫的是 **sim 库**, 即每天把 603 行模拟成交当作真实成交做追高违规/熔断扫描; 调用方是 **enabled** 的 Hermes job「FTSeek-US 盘后综合复盘」(`0 4 * * 2-6`) 的 bash 块, 并与 `scripts/intraday_loss_breaker_shadow.py` 共写 `data/violation_segment_heartbeat.log` 作毕业判据 (DEB-20260904, 目标 ≥10/14 天) ⇒ **判据输入本身建立在错标的列上** (`data_quality` 修正因此是判据前置, 不只是整洁性问题)。

**现网可复现?** **是** (两值共存已在库中)。

### 3.4 裂缝④: 实盘账本文件名有三种说法

**事实**:
- `app/core/scheduler_triggers.py:673` 把 `ftseek_us_live.db` 描述为「**废弃同名单库**」(该段属已下线分支的注释, 不产生行为);
- `app/core/config_container.py:117`、`db_utils.get_main_db()`、`app/storage/db_pool.py:45` 把它当 `TRADE_ENV=0` 的**路由目标**;
- `RFC-078` 把它定为**看板手动实盘账本** (恒 live, 与 `TRADE_ENV` 解耦);
- `CUTOVER-DECISION-PACKAGE §一 A'` 要求它是翻转后**唯一实盘账本** (两账本永久物理分离)。

**后果**: 同一文件四种身份。翻转日若沿用旧描述 (「废弃」) 而实际路由已指向它, 会出现"半本账": 一部分读方绕开它, 一部分写方写进去。**切换前必须定死唯一表述**: `data/ftseek_us_live.db` = 实盘唯一账本 (看板手动 + 翻转后 auto-live), `data/ftseek_us.db` = sim 研究账本 (翻转后冻结为只读档案, 零跨库复制)。

**现网可复现?** 描述冲突**是** (文档/注释层面); 行为冲突**否** (下线的 plan_feedback 分支不执行)。

### 3.5 裂缝⑤: SIM 专属执行语义在切盘瞬间全部失效

**事实**:

| SIM 专属假设 | 现网值 | 位置 | 切盘后 |
|---|---|---|---|
| 滑点模型 | `simulate_slippage_bps: 5.0` | `config/unified_config.yaml:297-301` (消费方 `app/execution/engine.py:178-181`) | 变**实测**滑点 |
| 深度成本 | `simulate_depth_cost_bps: 2.0` | 同上 | 变真实盘口深度 |
| 成交延迟 | `simulate_fill_delay_seconds: 0.5` | 同上 | 变真实排队 |
| 部分成交 | `simulate_partial_fill: false` | 同上 (`engine.py:447/510`) | 变**可能部分成交** |
| 止损兜底 | sim **无券商 STOP**, 靠 20s 轮询 | `dual_broker.py:503-505` (sim → skipped) | 变**券商挂单**: 入场即挂, 服务断连仍有效 |
| 实盘挂单失败策略 | 仅 `logger.warning`, 不撤回不降级 | `playbook_service.py:2053-2060` | **裸仓风险**: 挂着 warning 的不设防真钱仓 |

**附 (撤回作废, 见 §12-2)**: 上一稿曾以「该键在代码中无读取点」为由**撤回**「延长时段开仓链 `EXT_ENTRY_ALLOW_SIM` 只覆盖 SIM」。独立复查实测**该撤回是错的**:
`scripts/extended_hours_entry_executor.py:102` 是活跃读取点 —— `ALLOW_SIM_ENTRY = os.getenv("EXT_ENTRY_ALLOW_SIM", "false").lower() == "true"` (2026-09-18 起默认值改为 `"false"` fail-closed; 现网 `.env.us:135` 显式 `EXT_ENTRY_ALLOW_SIM=true`), 该脚本由系统 crontab 每 5 分钟运行 (`*/5 16-21 * * 1-5` 盘前 + `*/5 4-7 * * 2-6` 盘后, 已在 `docs/CONTROL-LOOP-LEDGER.md:95` 在册)。
⇒ **原说法恢复成立**: 延长时段开仓是 SIM 专属语义的一部分 (开关名与语义双重限定 SIM), 切盘后该链的开仓前提必须单独重审, 不能跟着盘中链一起翻。

**现网可复现?** 不适用 (切盘日才暴露), 但**风向已在**: 实盘挂 STOP 失败的处置今天是 warning 级 (§3.5 末行), 与「风控路径异常必须 CRITICAL + fallback」的既有纪律冲突。

### 3.6 裂缝⑥ (更正后): 账户维度缺失, 但"今天就在串账"不成立

**我此前的说法**: 「`position_sync` 会同时查 SIMULATE + REAL 并合并持仓 ⇒ 身份可能串账」。

**实测结论: 该判断部分证伪 (记录在案, 不用于后续论证)**:
- `app/core/position_sync.py:800-805`: `_envs_to_query` 自 2026-07-03 起**跟随 `AUTO_TRADE_ACCOUNT`, 恒单元素** (sim → 只查 SIMULATE)。`:794` 的「三态融合 (2026-06-25): 同时查询 SIMULATE + REAL, 合并持仓」是**陈旧注释**。
- ⇒ 今天不存在"同轮合并两账户"的路径。

**但仍然成立的真缺口**:
- `open_positions` 主键**只有 `symbol` 单列** (两库 `PRAGMA table_info` 实测 `pk=1` 仅 symbol), `account_type` 只是普通列 ⇒ 同标的跨账户共存无身份键支撑;
- `app/core/position_sync.py:1177-1184`: 自管持仓分支 `INSERT ... account_type ... VALUES (..., 'real')` + `ON CONFLICT DO UPDATE SET ... account_type='real'` —— **硬编码 'real', 无 env 判据** ⇒ sim 路由下把模拟账户里的自管持仓也标成 `real`;
- 两库该列 DDL 默认值不同 (sim 库默认 `'sim'`, live 库默认 `'real'`) ⇒ 少写一个字段时行为随库漂移。

**风险等级**: 由「今天就在串账」下调为「**切换日 + 手动/自动同标的并存时**才暴露」。这正是 `PROPOSAL-20260909 §二 缺口4` / `S3b` 已覆盖的方向 (反向守卫已落地), 本 RFC 只补**身份键与硬编码标签**这两处。

### 3.7 裂缝⑦: 运行时切换脚枪

**事实**: `app/core/db_utils.py:112` 与 `:183` —— 设 `FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH=1` 时, `is_live_mode()` / `dashboard_db_isolation_enabled()` **每次重读文件** = 恢复"改文件即生效", 即 §3.1 撕裂账本路径的运行期版本 (同文件 `:76-82` 注释自述这是它的成因)。

**实测**: 现网 `.env.us` **无** 此键 ✓。**切换协议必须显式断言它不存在** (而不是依赖"没人会设")。

### 3.8 附: 一处已定义未接线的毁灭性函数

`app/web_dashboard/mode_switch_service.py:310-338` `_clear_live_db()`: 语义是「切到实盘时**清空** live 库 `open_positions` + `trade_records`」(先备份到 `_archive_*` 表)。

**实测无调用方** (仅 `tests/test_env_separation_shadow.py:356` 的白名单提及)。而 `RFC-078` 之后 live 库**已是实盘真账** (14 行 `trade_records` / 8 行 `open_positions`), 且 `_clear_live_db` 的判据里**没有任何"库内无实盘行"前置** ⇒ 一旦被接线即销毁实盘账本 (备份表是唯一留痕)。切换协议里必须**删除或加前置条件**。

---

## 4. 收敛清单 (P0 = 不做完不许切实盘; 全为内部收敛, 不改交易行为)

| # | 改什么 | 为什么 (对应裂缝) | 验收判据 (可跑) | 类型 |
|---|---|---|---|---|
| **P0-1** | 新增唯一库路由解析器, 从模式判定**派生**; `config_container.py:117` / `paths.py:74` / `db_pool.py:45` / `position_intelligence.py:118` 全部改调它; `TRADE_ENV` 降级为兼容字段 (保留可读、不参与判定) | ① | 在 `TRADE_ENV=0 && ALLOW_REAL_TRADING=false` 下, 5 个解析点**全部**指向 sim 库 (现网会分裂); 单测钉桩源码锚点防漂移 | L2 |
| **P0-2** | 新增唯一读取入口 `allow_real_trading()` (7 处改调); 启动时比对**文件值 vs 进程值**, 不一致 → **启动硬失败** (非 critical 日志) | ② | 改 `.env.us` 不重启 → 启动/健康检查报 `MODE_AUTHORITY_MISMATCH` 并拒绝进入任何 live 分支; 重启后一致 | L2 |
| **P0-3** | 统一模式戳: 定 `sim\|live` 两态 + 单一谓词 `is_sim_row()`; 迁移历史值 (`sim` → 统一取值); 消费者禁字面量; `data_quality` 取消 `DEFAULT 'real'` 改显式写入 | ③ | 两库 `trade_mode` 取值集合唯一; `grep -rn "trade_mode *[!=]= *'" app/` 命中 0 (除谓词模块本身); 反向测试: 构造 `'sim'` 行不再漏进"非 sim"集合 | L2 |
| **P0-4** | 账户维度: 镜像/孪生/幽灵判定与 playbook 护栏 SQL 显式带 `account_type`; `position_sync.py:1183` 硬编码 `'real'` 改跟随 env; 两库该列 DDL 默认值对齐 | ⑥ | 单测: sim 路由下自管持仓写入 `account_type='sim'`; 构造同标的 sim+real 行 → 护栏按账户分别判定, 不互相吞并 | L1 |
| **P0-5** | 实盘退出路径定死: ① live 入场挂 STOP **失败 → fail-close** (撤回建仓 或 立即市价平仓 + `critical` 告警, 由用户裁决方向) ② 明确 `monitor_playbook_sim_exits` 只服务 `PLAYBOOK_SIM` 行, `PLAYBOOK_LIVE` 行走独立退出链 (现同一条 SQL 管两臂, 见 `pipeline.py:7955-7965`) | ⑤ | 单测: STOP 挂单失败注入 → 要么撤单回滚仓位, 要么临界告警 + 平仓, **不允许"仅 warning 裸奔"**; 退出链 SQL 两臂分流有断言 | L1 |
| **P0-6** | 原子切换协议 (§5) + 删除/加前置 `_clear_live_db` + 断言 `FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH` 不存在 + 补「改了文件没重启」硬校验 | ⑦/§3.8 | 切换 runbook 全项自检脚本可跑 (退出码 0 才允许继续); 反向测试: 手改 `.env.us` 不重启 → 自检非 0 | L1 |

**P1 (切盘前该有)**
7. **前置能力演练清单** (每项先在 SIM 演练留证): `unlock_trade` 交易密码 (`FUTU_UNLOCK_PWD_MD5`)、行情权限 (LV1/LV2/RT_DATA)、借券/做空可得性、停牌/公司行动/熔断、`FTSEEK_LIVE_DEAL_PUSH` 订阅、PDT 口径复核 (RFC-062 A1: 富途香港实体不适用 — 若试点账户为 moomoo US 实体则**恢复**该判据)。
8. **报告/台账全带 mode 维度**: 新复盘 bundle / OKR / scorecard 一律按 `mode` 分片, sim 与 live **永不同池统计**; golden 基线标注 `sim-only` (与 `RFC-086` 阶段统计 SSOT 对齐)。
9. **费用/滑点校准**: 用真实成交回填 `fees_est`, 用实测滑点替换 5bps 模型。既有铁律不变: 费用 ≈ 毛利 42×, edge 必须 > ~10bps。
13. **收编路径修复**: `position_sync.py:136/223` 指引的 `scripts/adopt_position_to_playbook.py` 实测不存在 (实为 `scripts/archive/` 下一次性脚本, 且它就是 `trade_mode='sim'` 那 1 行的历史来源) ⇒ 更正指引或恢复可用入口, 否则幽灵仓/两不管仓无人能按文档收编。
14. **`data_quality` 错标修正前置化**: `scripts/daily_violation_scan.py:160` 每日按 `data_quality='real'` 扫 sim 库 603 行 (见 §3.3), 其毕业判据输入被污染 ⇒ 在 P0-3 落地前, 该判据的读数需标注"口径待修", 不得据其裁决。

**P2 (可持续)**
10. 三态机 (`sim` / `live_conservative` / `live_normal`, `LIVE_CONSERVATIVE` 键已存在) + **自动降级** (触发条件 → 自动回落 sim 并告警)。
11. 每模式独立数据目录 / 去重键前缀, 防影子文件混真钱。
12. Kill switch 三级: 全局 (`ALLOW=false` + 重启, 已具备)、策略级 (`strategy_budgets.*.allow_live`, 已具备)、日内 (日亏熔断 — 曾因 `TRADE_ENV` 恒=1 三重失效, 已 P0 修, **但必须在 live 下重新验证**)。

---

## 5. 原子切换协议 (与 CUTOVER 决策包 A' 互补, 不重复其 runbook)

**原则**: 一次事务化地改完整组键 → 重启 → 启动自检全绿 → 才允许下单; 任一步失败即回滚整组。

**键组 (必须同翻, 半翻态由 S2' 互锁 fail-close 兜住)**:
```
AUTO_TRADE_ACCOUNT=real
ALLOW_REAL_TRADING=true
TRADE_ENV=0            # P0-1 后: 不再参与判定, 仅作兼容镜像 (由派生值写入)
LIVE_CONSERVATIVE=1    # 首期保守 (0 为全量)
```
**翻转前 (T-1)**: ① 未平仓 = 0 或已登记归属 ② `git status` 改动归属确认 ③ 自检脚本 pre-flight (含 `FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH` 缺席断言, `_clear_live_db` 已删/加前置)。
**翻转 (T)**: UI `/api/mode/switch` (`CONFIRM`) → 自动重启 → 启动自检 8 项:
1. 模式判定 = `LIVE_CONSERVATIVE`; 2. 库路由 = live 库 (5 个解析点一致); 3. broker 账户 = REAL 且 `unlock_trade` 成功; 4. 行情权限 (LV1/RT_DATA) 在位; 5. 在仓归属: 每个 live 行 `account_type='real'` 且监护链可见; 6. 毕业门禁读到 live 证据 (A' 第 2 步) 或走"试点攒样中"语义; 7. `FTSEEK_LIVE_DEAL_PUSH` 订阅活体; 8. 日限额 (P2-12 日内熔断) 生效断言。
**首日**: 最小暴露 (§6) + sim 影子对照留证。
**回滚 (T+any)**: 键组同翻回 `sim`/`false`/`1`/`1` → 重启 → 自检 (模式=sim、库=sim、broker=SIMULATE、在仓归属不跨库) → 与 RFC-062 kill 条件同等即时, 无需裁决。
**⚠️ 回滚的授权锁不对称 (实测, 需修)**: 看板 `mode_switch_service.py:210-215` 的 **simulate 分支不写 `ALLOW_REAL_TRADING=false`** (只写 3 键), 而 live 分支的前置检查 (`:254-256`) **要求它已是 `true`**; 同文件 `set_auto_account` (`:552-600`) 是**同款结构** (sim 分支 `:584-587` 只写 3 键, real 分支前置 `:565-566` 要求已为 true), 全文件**没有** `_apply_env_updates` 之类统一写入口 (复查实测 grep 为空) ⇒ 两条后果: (a) 从 `false` 出发**无法用 UI 切实盘**, 必须手改 `.env.us` ——正是本 RFC 要消灭的动作; (b) 回切 sim 后授权锁**留开**, 手动实盘平仓路径 (`app/web_dashboard/order/executor.py:406-412`) 的锁被留在 open 态。建议: UI 切实盘改为「写入 4 键 + 重启 + 自检」的原子动作 (授权锁不在写入前判定), 回切时显式写 `false`。

---

## 6. 预设计解决不了的部分 (诚实边界)

**真实排队 / 滑点 / 拒单 / 停牌 / 流动性 / 部分成交, 只有实盘才暴露。** 因此过渡不是"一次全量切换", 而是**最小暴露**:
- 只开实盘**盘中已验证链** (Playbook 盘中), 延长时段/主线仍 sim;
- 1 笔/日、最小手;
- 券商 STOP **必挂**, 挂不上即不持仓 (P0-5);
- 同信号 sim 与 real **并行对照留证** (影子模式, 只记录不下单, 与 live 物理分离, 符合 CUTOVER §一 A' 与"不混为一谈"裁决);
- 攒够样本才放大 (放大判据沿用门 2, 不另立)。
**已知不可预设计项**: 真实盘口深度分布、拒单原因分布 (SSR/借券)、券商 STOP 单的实际触发质量、OpenD 断连期间 STOP 的可靠性。

---

## 7. 与既有治理文件的关系 (防重复账本)

| 既有文件 | 覆盖 | 本 RFC 与之关系 |
|---|---|---|
| `RFC-062` | 门 1/门 2 判据、kill 条件、scaling ladder | **不重裁**; 判据以 CUTOVER 决策包 §四 重裁版为准 |
| `CUTOVER-DECISION-PACKAGE-20260909` | A' 两账本分离、翻转日 runbook (证据快照/门禁重指/降级改 HALT)、FLAT_ALL、陈旧行 | **不重复**; 本 RFC §5 只补"开关组索引 + 自检项 + 授权锁不对称" |
| `PROPOSAL-20260909` §三·五 | 单库翻转模型 (S2 证伪) | 本 RFC §3.1/§4-P0-1 **依赖**该模型 (库路由派生即"同翻"的代码化) |
| `RFC-078` / `RFC-080` | 看板库隔离、remark 认领、S2' 半翻互锁 | **复用**; P0-2 把 S2' 的"下单前检查"上移到**启动期硬失败** |
| `docs/ADJUDICATED-CRITERIA.md` C 类 | C2 打标前不切实盘 | 本 RFC 不解锁任何执行 |

**不新增**: 无新表、无新配置文件、无新 cron (P0 全为既有解析器替换)。

---

## 8. 待用户裁决 (四项, 每项给建议 + 理由 + 风险 + 保守替代)

### 8-1 `TRADE_ENV` 是否降级为兼容字段 (不参与判定)?
**建议: 同意降级** (判定唯一真源 = `AUTO_TRADE_ACCOUNT` + `ALLOW_REAL_TRADING` 授权锁, 库路由从它派生)。
**理由**: ① 消灭 §3.1 的"判定/路由"两条独立路径; ② `trading_mode.py:150-169` 的 P0-2026-08-16 修复已经把判定源从 `TRADE_ENV` 改成 `AUTO_TRADE_ACCOUNT`, 信息源本已收敛一半; ③ 单库翻转模型要求二者同翻, 派生即天然同翻。
**风险**: 一次性改动 6 个解析点, 若有脚本依赖 `TRADE_ENV` 语义独立切换 (如回放/迁移) → 变化面。缓解: 保留字段可读 + 启动期一致性断言。
**保守替代**: 不删判定权, 但加"派生值 vs `TRADE_ENV` 不一致 → 启动硬失败"。代价: 切换仍需对齐两个值。

### 8-2 模式戳用两态还是三态?
**建议: 分层, 不是二选一** —— **DB 戳用两态 `sim|live`** (账本分池只需二元, 且必须能与 broker env 一一对应), **CLI/UI 模式名保留三态** (`simulate / live_conservative / live_normal`, 服务于仓位系数与限额: `trading_mode.py:217/265-289`)。
**理由**: 三态戳进 DB 会让每个消费者都要处理第三种值, 而账本维度只需要"真钱/纸面"; 三态的真实用途 (0.5× 仓位、收紧风控) 已在 `pre_trade_gate.py:43` 消费。
**风险**: 两层映射需单点定义, 否则又多一个漂移点。缓解: 映射表唯一实现 + 反向测试 (三态 → 两态恒等)。
**保守替代**: 全库统一三态 (含 `live_conservative`/`live_normal`), 代价是所有 `trade_mode='live'` 消费者 (analytics/data_service/live_weekly_review) 都要改。

### 8-3 实盘首发额度定多少?
**建议: 沿用 RFC-062 门 2 试点设计, 不新立数字** —— 最小手 / 1 股, **单笔风险 ≤ 0.25% 净值**; 首周额外收紧为 **日 ≤ 1 笔** + 单笔名义上限 (建议 ≤ $500, 需用户确认可用净值后签字)。
**理由**: ① 门 2 的试点设计已过用户裁决, 重复发明会造成"两套额度"; ② 单笔风险比例口径对净值免疫, 不随账户规模失准。
**风险**: 1 笔/日的样本增速极慢 (~20 交易日才够门 2 的 20 笔); 若首期目的是"求滑点/拒单分布"而非"求收益", 可按周评估上调到 2 笔。
**保守替代**: 只允许 1 股 (不论金额) 且当日仅 1 笔, 直到 `live_reconcile_check` 连续 5 日零漂移 (门 2 G2-3) 后再放开。
**注**: 具体金额需用户给可用净值; 本 RFC **不作绝对金额承诺**。

### 8-4 是否维持"只有 Playbook 可 live, 主线仅 sim 研究"?
**建议: 维持** (现状 `strategy_budgets.*.allow_live` 中 `main_pipeline=false`)。
**理由**: ① 切换面越小越可证伪 — 首期变量应只有"执行微结构", 不应同时引入"多策略真实成交"; ② 主线样本按 sim 口径累积, IC/OKR 不断供 (与 CUTOVER §一 研究臂归宿一致)。
**风险**: 主线 live 表现无从验证, 转正时点后延。
**保守替代**: 若确需在切换期观察主线, 用**影子记录** (record-only, 零下单), 而不是让它 live。

### 8-5 是否落 RFC-090 并为 P0-1..P0-6 单独开实施 commit?
**建议: 是** (P0 各项独立 Gate + 独立回滚, 顺序 P0-3 → P0-2 → P0-1 → P0-6 → P0-5 → P0-4, 理由: 先统一戳与读入口, 再动路由, 最后碰退出链)。

---

## 9. 验证计划 (给复查者: 每条都能独立复跑)

```bash
cd /opt/ftseek/FTSeek-US
# C-1 库路由解析器数量 (期望: 带门 3-4 处 / 裸读 5-6 处)
grep -rn "TRADE_ENV" app/ scripts/ --include=*.py | grep -E "getenv|_parse_trade_env_from_file"
# C-2 授权开关两族读法
grep -rn "ALLOW_REAL_TRADING" app/ scripts/ --include=*.py | grep -E "getenv|_read_env"
# C-3 模式戳实测分布 (期望: sim 库 出现 sim 与 simulate 两值)
sqlite3 data/ftseek_us.db "SELECT trade_mode, COUNT(*) FROM trade_records GROUP BY 1;"
# C-4 data_quality 语义 (期望: sim 库 'real' 600+ 行)
sqlite3 data/ftseek_us.db "SELECT data_quality, COUNT(*) FROM trade_records GROUP BY 1;"
sqlite3 data/ftseek_us.db "SELECT sql FROM sqlite_master WHERE name='trade_records';" | grep data_quality
# C-5 open_positions 身份键 (期望: pk 仅 symbol)
sqlite3 data/ftseek_us.db "PRAGMA table_info(open_positions);" | awk -F'|' '$6==1'
# C-6 自管持仓硬编码标签
sed -n '1177,1184p' app/core/position_sync.py
# C-7 _clear_live_db 是否有调用方 (期望: 除自身与测试白名单外为空)
grep -rn "_clear_live_db" app/ scripts/ ops/ tests/ --include=*.py
# C-8 运行时切换脚枪是否在现网 (期望: .env.us 无此键)
grep -c "FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH" .env.us
# C-9 live STOP 失败策略 (期望: 仅 logger.warning)
sed -n '2052,2061p' app/playbook/playbook_service.py
# C-10 回切 sim 是否解授权锁 (期望: simulate 分支 updates 不含 ALLOW_REAL_TRADING)
sed -n '210,235p' app/web_dashboard/mode_switch_service.py
```

---

## 10. 回滚

- 本文件 (L5) 回滚 = `git revert` 单 commit, 无运行时影响。
- P0 各项均为"替换解析器/加断言", 逐项独立 commit; 回滚 = `git revert <hash>` + `systemctl restart ftseek-us` (P0-1/P0-2 涉及启动期断言, 回滚后必须重启)。
- 若 P0-2 的启动硬失败在部署当日误伤只读脚本: 缓解是断言只在**主进程** (main.py) 路径启用, 脚本路径降级为 warning (与 `db_utils._assert_env_routing_consistent` 的既有取舍一致)。

---

## 11. 复查清单 (给独立复查 agent: 逐条打假, 不许只读文档)

| # | 待挑战的声称 | 打假方式 (必须实跑) | 打假成功的判据 |
|---|---|---|---|
| C1 | 库路由有 5-6 个裸读解析器, 授权门只 3-4 处 | 跑 §9 C-1, 并逐个确认该点**是否真的参与库文件选择** (可能只是日志/脚本参数) | 任一处属误报 (不选库) 或漏报 (还有第 7 处) |
| C2 | 授权开关两族读法, 且 systemd 注入导致进程值冻结 | 跑 §9 C-2 + `systemctl cat ftseek-us`; 另查是否有 `os.environ` 直改路径 | 存在第三条读法 (如运行时 `os.environ` 覆写) 或冻结说法不成立 |
| C3 | sim 库同时存在 `sim` / `simulate` 两值, 且 8 行 `sim` 全部来自 `position_sync:200` | 跑 §9 C-3 + 按 `strategy_id/entry_reason` 反查那 8 行的来源 | 存在第三种值, 或来源不是我声称的写方 |
| C4 | `data_quality` 该列默认 `'real'` 且 sim 库 603 行为 `real` | 跑 §9 C-4; 并 grep 有没有消费者按该列筛实盘 | 有真实消费者 → 后果比我写的更严重 (记录), 或数字不符 |
| C5 | `open_positions` 身份键只有 `symbol` | 跑 §9 C-5 两库; 另查是否有 UNIQUE INDEX 组合键 | 存在 `(symbol, account_type)` 唯一索引 ⇒ 我的结论错 |
| C6 | "position_sync 同时合并 SIM+REAL" 已被证伪 | 读 `position_sync.py:794-810` + git log 找 2026-07-03 改动; 找是否还有别的合并点 | 存在另一处真合并 (如镜像/对账路径) ⇒ §3.6 更正要再改 |
| C7 | `_clear_live_db` 无调用方且是毁灭性函数 | 跑 §9 C-7; 再查动态调用 (`getattr`/字符串反射/看板路由) | 存在动态调用 ⇒ 立即升级为 P0 阻断项 |
| C8 | live STOP 失败仅 warning ⇒ fail-open 裸仓 | 跑 §9 C-9; 再查是否有下游补偿 (如 `live_reconcile_check` 自动补挂) | 有下游补偿 ⇒ 严重度下调 (但需给出补偿代码位置) |
| C9 | 回切 sim 不解授权锁, 且从 `false` 出发无法用 UI 切实盘 | 跑 §9 C-10 + 读 `:254-260` + 实查 `.env.us` 现值 | 分支里有别处写 false (如 `_apply_env_updates`) ⇒ 结论错 |
| C10 | `EXT_ENTRY_ALLOW_SIM` 无代码读取点 ⇒ 已撤回该说法 | `grep -rn "EXT_ENTRY_ALLOW_SIM" app/ scripts/ config/` | 有读取点 ⇒ 撤回作废, 需恢复原说法 |
| C11 | 现网 `.env.us` 无运行时切换脚枪 | 跑 §9 C-8 + `systemctl show ftseek-us -p Environment` | 存在 ⇒ 升级为 P0 阻断 (立即清) |
| C12 | §2 表格中 8 项"已有资产"逐条属实 | 逐项 `sed -n` 读源码确认行号与语义 | 任一行号/语义不符 |
| C13 | §7「不新增台账/配置/cron」成立 | 检查 P0-1..P0-6 是否真的只改既有解析器 | 任一 P0 项隐含新建文件 ⇒ 违反 |
| C14 | 与既有文档不冲突 (尤其 CUTOVER 决策包 A' 与单库翻转模型) | 读 `CUTOVER-DECISION-PACKAGE §一` + `PROPOSAL §三·五` 对齐 | 存在与本 RFC §4-P0-1「库路由派生」冲突的既有裁决 |

**本次复查已行使 (2026-09-20, 两个只读 agent 并行)** —— 逐条判定见 §15: C1/C2/C5/C6/C7/C8/C9/C11/C13/C14 **成立**; C3 **数字不符 (来源需精确化, 已改)**; C4 **成立且升级** (发现活体消费者, 已补); C10 **作者撤回作废** (原说法恢复); C12 **两处行号微漂** (已改 1 处)。

**复查纪律 (沿用本仓既有教训)**: 复查 agent 的报告是**自报告**, 作者须逐条在真文件上复核; 复查者的**否决性结论**尤其要由作者重跑 (§3.6 就是一次自我更正, 记录在案)。误报与更正都要留痕, 防后续 agent 采信。

---

## 12. 自曝与局限

1. **§3.6 是我上一轮口头结论的更正**: "position_sync 合并两账户"不成立 (注释陈旧)。原表述若已进入任何下游讨论, 以本节为准。
2. **§3.5 的「撤回」本身是错的, 已作废并恢复原说法** (独立复查实锤 + 作者亲自复跑确认, 见 §15-C10)。根因三层, **全属我的检索纪律问题**, 记录以防再犯: ① 宽口径 grep (`grep -rn "EXT_ENTRY" app/ config/ scripts/`) 的输出被 `head -15` **截断**, 第 102 行落在截断之外; ② 随后的精确 grep 路径清单写成 `app/ config/ .env.us`, **漏了 `scripts/` 与 `ops/`**; ③ 同一条 grep 的 `--include=*.py --include=*.yaml` 又把**无扩展名的 `.env.us`** 挡在门外 (现值就在 `.env.us:135`)。⇒ **纪律**: 「找不到」与「撤回」和「声称」同权 —— 结论前必须确认路径清单覆盖 `app/ scripts/ ops/ ~/.hermes/scripts/`、`--include` 未排除点文件、且**不得在输出被截断的情况下下否定结论**。
3. **行号会漂移**: 本文件所有行号基于 commit `e656c4893` (2026-09-20 实测), 复查者若在更晚 commit 上核对, 请以**语义**为准并以 `grep` 重定位。
4. **未验证项 (不列入 P0 结论)**: ① 看板手动单与 auto-live 同标的并存时的 `open_positions` 行为 (PK 单列) 我只做了结构分析, 未做实证冲突; ② 券商 STOP 单在 OpenD 断连期间的实际可靠性 (只能实盘测); ③ 佣金/滑点实测分布 (无实盘成交样本)。
5. **本 RFC 不含任何收益预测**, 也不承诺实盘结果; 所有金额类判据由用户签字。

---

## 13. Gate 记录

| Gate | 结论 |
|---|---|
| Gate 0 宪法对齐 | 待查 `SYSTEM_OBJECTIVE.md` 当前 Top 3 (本 RFC 为准备性文档 + 零行为改动, 未越权执行) |
| Gate 1 分类 | **L5 (纯文档)**; 其中 P0 各项为 L1-L2, 各自另走 Gate |
| Gate 2 方案评审 | 本文件即方案; 独立复查见 §14/§15 |
| Gate 3-6 | 未开始 (P0 待裁决后逐项走) |

---

## 14. 实施记录

**无** (本 RFC 尚未批准任何落码; 无 commit 无重启)。

---

## 15. 独立复查结论 (2026-09-20, 两个只读 agent 并行 + 作者逐条复跑)

> 复查方式: 两个**只读** agent 并行 (A 组 = 数据/DB 类 C3/C4/C6/C7/C11; B 组 = 代码/配置/流程类 C1/C2/C5/C8/C9/C10/C12-C14), 两份报告回来后**由作者在真文件上逐条复跑**(本仓纪律: 子代理自报存在误报; 否决/撤回类结论尤其必须作者重跑)。
> 判定分三类: **成立** / **更正 (R-x)** / **误报留痕** (本次误报发生在**作者自己身上**, 记 R-1 以警示后续 agent 勿采信上一稿措辞)。

### 15.1 逐条判定

| # | 复查判定 | 作者复跑复核 | 处置 |
|---|---|---|---|
| C1 | 成立 | 一致: 6 个裸读点**全部**真参与库文件选择 (`config_container.py:117` / `paths.py:74` / `db_pool.py:45` / `position_intelligence.py:118` / `daily_ic_analysis.py:53` / `update_signal_lifecycle.py:34`); 授权门 4 处; 唯一额外同款裸读在 `scripts/archive/shadow_eod_replay.py` (已废弃, `scripts/shadow_board.py:14` 标注) ⇒ 无活跃漏报 | 原文保留 |
| C2 | 成立 | 一致: 进程族 4 处 / 文件族 3 处; `systemctl cat ftseek-us` 确证 `EnvironmentFile=/opt/ftseek/FTSeek-US/.env.us`; 另查 `os.environ` 直改 23 处**均不触及**这两键, `load_dotenv` 20+ 处**无一处 `override=True`** ⇒ "启动冻结"成立且无第三条路径 | 原文保留 (补"无第三条路径"结论) |
| C3 | **数字不符 (来源)** | 复核确认: 总数 867+8 正确, 但 8 行中 **1 行** (US.EOG 2026-07-20, `entry_reason='adopt_to_playbook'`) 出自 `scripts/archive/adopt_position_to_playbook.py:100` 而非 `position_sync.py:200` | §3.3 已精确化 (7+1) + 连带发现幽灵指引路径 (§4-P1-13) |
| C4 | **成立且升级** | 复核确认更强: `scripts/daily_violation_scan.py:160` 按 `data_quality='real'` 扫 sim 库, 且作者另行实证**该脚本由 enabled 的 Hermes job「FTSeek-US 盘后综合复盘」每日调用** (jobs.json 内 bash 块 + `intraday_loss_breaker_shadow.py` 共写心跳台账) ⇒ 活体污染而非潜在风险 | §3.3 已补活体消费者 + §4-P1-14 要求该判据读数标"口径待修" |
| C5 | 成立 | 一致: 两库 `pk=1` 仅 `symbol`, **无** `(symbol, account_type)` 复合唯一索引 (仅 `sqlite_autoindex` + `idx_open_owner`); `position_sync.py:1177-1184` 硬编码 `account_type='real'` 确认 | 原文保留 |
| C6 | 成立 | 一致: `_envs_to_query` 恒单元素 (`position_sync.py:794-810`), 9 处 `position_list_query` 调用点全部单 env (commit `ec90678d3a`, 2026-07-06), **无其它合并路径** ⇒ 我已作的 §3.6 更正成立 | 原文保留 |
| C7 | 成立 | 一致: 全仓 (含 `getattr`/反射/看板路由/ops) **无调用方**; live 库 14 行 `trade_records` + 8 行 `open_positions` 确认 | 原文保留 |
| C8 | 成立 (严重度维持) | 一致且更细: `scripts/live_reconcile_check.py` (190 行) **不读 `sl_broker_order_id`、不补挂 STOP**; `position_sync` 无该调用; `order/executor.py:1842` 只管手动单部分平仓后的重建; `pipeline.py:6793` 仅用该列做身份去重 ⇒ **无任何下游补偿, fail-open 裸仓** | 原文保留 |
| C9 | 成立 | 一致: simulate 分支 3 键 / live 分支 5 键 / 前置要求已 true; 并多查一处 **同款结构** `set_auto_account` (`:552-600`) | §5 已补同款位置 |
| C10 | **不成立 (作者撤回本身是错的)** | 复核确认: `scripts/extended_hours_entry_executor.py:102` 有活跃 `os.getenv("EXT_ENTRY_ALLOW_SIM","false")`, 默认 fail-closed, `.env.us:135` 显式 `=true`, 脚本 systemd crontab 每 5 分钟运行且在 `CONTROL-LOOP-LEDGER.md:95` 在册 ⇒ **原说法 (SIM 专属开仓开关) 恢复** | **R-1**: §3.5 撤回作废并恢复原说法; §12-2 记录根因 (head 截断 + 路径清单漏 `scripts/` + `--include` 滤掉 `.env.us`) |
| C11 | 成立 | 一致: `.env.us` / systemd 注入 / EnvironmentFile / `/etc/environment` / crontab / `start.sh` 全路径无 `FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH` | 原文保留 |
| C12 | 基本成立, 1 处行号微漂 | 逐项核对 9 行, 仅 `_resolve_trade_mode()` 实为 `:41-64` (原写 41-67); 其余行号在 ±2 行内 | §2 已改 |
| C13 | 成立 | 一致: P0-1..P0-6 均为"新增函数 + 改既有调用点", 无新表/新配置/cron (§5 自检脚本属 runbook 脚本, 不违反三项) | 原文保留 |
| C14 | 成立 | 一致: P0-1「库路由从模式判定派生」= 单库翻转模型的代码化, 与 A'「不跨库复制」不矛盾 (前者管路由指向, 后者管搬运禁令) | 原文保留 |

### 15.2 更正 (R-x, 均经作者复跑)

- **R-1 (§3.5 / §12-2)**: 上一稿对 `EXT_ENTRY_ALLOW_SIM` 的"撤回"作废 —— 该键有活跃读取点且**每天在跑**。教训: 一次被 `head` 截断的 grep + 一次漏 `scripts/` 的路径清单, 就足以把"存在的开关"写成"不存在", 而这与"声称不存在"是同一类错误。已在 §12-2 落为纪律。
- **R-2 (§3.3)**: 8 行 `'sim'` 的来源是 **7 + 1** (非"全部来自 position_sync:200"), 并因此发现活跃代码指引的收编脚本路径已不存在 → 新增 §4-P1-13。
- **R-3 (§3.3 升级)**: `data_quality='real'` 不是"潜在语义地雷", 而是**每天被一个 enabled job 当真实成交使用** (`daily_violation_scan.py:160`) → 该判据 (熔断/违规扫描毕业判据) 的读数在 P0-3 之前不得用于裁决。
- **R-4 (§2)**: `_resolve_trade_mode()` 行号为 `41-64`。

### 15.3 误报留痕

| 来源 | 声称 | 复核结论 |
|---|---|---|
| 作者上一稿 (自误报) | "`EXT_ENTRY_ALLOW_SIM` 在代码中无读取点, 故撤回原说法" | **错** (见 R-1)。原说法成立, 撤回作废。后续 agent 若引用"该键不存在", 以本节为准。 |
| 复查 agent | 无被推翻的结论 (两条待精确化项 C3/C4 经复核**成立方向正确**, 只是数字/严重度需改) | — |

**纪律提醒**: 复查 agent 的 C3/C4 两条都是"作者写得太窄/太轻"的方向 —— 与常见"子代理夸大"相反。说明**作者自查也有系统性偏向**, 独立复查的价值不在于是否有误报, 而在于是否覆盖了作者的口径盲区。