# DEB-20260916 Full-Chain Bug-Hunt Recheck

**审计时间**: 2026-09-16 16:06 CST 之后
**修复后重启复核**: 2026-09-16 16:55 CST 执行 `systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` 通过，服务 PID `1565676`；冒烟七项全部通过（ConfigValidator、漂移检测、热重载、SSOT 0 BLOCKING、模拟盘安全、退出快循环、零 Traceback/CRITICAL）。
**当前姿态**: `AUTO_TRADE_ACCOUNT=sim`、`TRADE_ENV=1`、`ALLOW_REAL_TRADING=false`、`playbook.execute_mode=futu_simulate`、`playbook.auto_execute=true`。

本报告是“关键生产链路 + 固定缺陷模式”的闭集复核，不是全仓 187,417 行逐行审计。未覆盖部分在《负面清单》中显式列出。

## 1. 测绘产出

### 1.1 数据流阶段图

| # | 阶段 | 主要文件与入口 | 结论 |
|---|---|---|---|
| 1 | 行情与市场状态采集 | `app/data/futu_lv3_provider.py:930`、`app/playbook/pipeline.py:640` | Futu 快照/订阅/缓存与市场环境采集 |
| 2 | 候选筛选 | `app/playbook/pipeline.py:2142`，`PlaybookScreener.screen` | watchlist 空或无候选时写生成完成终态 |
| 3 | 投票/模型决策 | `app/playbook/pipeline.py:1314`、`app/playbook/pipeline.py:2267` | VotingCoordinator/Bridge/TechnicalFallback 生成方向 |
| 4 | 指令持久化 | `app/playbook/pipeline.py:2962`、`app/playbook/db.py:580` | `playbook_instructions` 是入场台账 |
| 5 | 生成完成与推送 | `app/playbook/pipeline.py:2133`、`app/playbook/pipeline.py:2312` | 完成标记持久化；推送独立幂等 |
| 6 | 触发与重定价 | `app/playbook/pipeline.py:5212`、`app/playbook/pipeline.py:4107` | 突破触发、ATR/vol bucket 重算 |
| 7 | 前置门序列 | `app/playbook/pipeline.py:3598` | kill switch → regime/risk → 熔断 → 预算/日帽 → 指令门 |
| 8 | 指令认领与执行入口 | `app/playbook/playbook_service.py:777`、`app/playbook/db.py:844` | 原子 claim + CAS 状态机 |
| 9 | 仓位计算与下单 | `app/playbook/instruction_generator.py:37`、`app/execution/dual_broker.py:198` | sizing SSOT；Futu 下单 |
| 10 | 成交确认与簿记 | `app/playbook/playbook_service.py:1574`、`app/playbook/playbook_service.py:1761` | broker 成交价/量优先，写 `trade_records` |
| 11 | 持仓与退出监控 | `app/playbook/pipeline.py:7823` | 20s 独立快循环，批量取价 |
| 12 | Durable 平仓 | `app/execution/dual_broker.py:317`、`app/execution/exit_lifecycle.py:325` | exit intent/leg 先落账再 broker I/O |
| 13 | 成交回写与收口 | `app/playbook/pipeline.py:7674` | receipt 校验后写 exit 字段并 `BOOKED` |
| 14 | 报表与归因 | `app/review/daily_review.py:898`、`app/core/scheduler_triggers.py:479` | 日报/盘后笔记/复盘入口 |

### 1.2 核心开关清单

YAML 布尔叶子共 362 个（机器枚举）。下表只列主链路关键项；其余 331 个未逐项做行为追踪，见负面清单。

| # | 开关 | 当前值 | 生效位置 | 关闭/当前语义 |
|---|---|---:|---|---|
| 1 | `playbook.enabled` | true | `run_playbook_generate` 入口 | 不生成指令 |
| 2 | `playbook.auto_execute` | true | `run_playbook_auto_execute:3615` | 不自动执行 |
| 3 | `playbook.execute_mode` | `futu_simulate` | `PlaybookService:960` | `simulate` 强制降级为 `futu_simulate` |
| 4 | `AUTO_TRADE_ACCOUNT` | sim | 进程环境与 `PlaybookService:1120` | real 需毕业门 |
| 5 | `ALLOW_REAL_TRADING` | false | `di_container:143` | real 被强制降级 sim |
| 6 | `risk.acb_fail_mode` | closed | `PlaybookService:885` | ACB 异常拒单 |
| 7 | `risk.preflight_fail_mode` | open | `pipeline:3672` | SIM 下 regime/risk 查询异常放行；live 启动断言要求 closed |
| 8 | `risk.capital_safety.playbook_hard_gate` | true | `PlaybookService:907` | 关闭后水位门不执行 |
| 9 | `risk.playbook_vix_gate_enabled` | true | `PlaybookService:919` | VIX 缺失 fail-close |
| 10 | `playbook.entry_freshness_gate_enabled` | true | config | 龄期阈值存在，但 Futu 本地时间戳使其近零生效，已在 YAML 注释披露 |
| 11 | `pretrade_quote_gate_enabled` | true | config | spread 门实际生效，60s 龄期同上近零 |
| 12 | `entry_open_blackout_enabled` | true | trigger/execution 门 | ET 09:35 前拦截 |
| 13 | `entry_vwap_gate_enabled` | true | 入场门 | VWAP 偏离拒单 |
| 14 | `intraday_vol_gate_enabled` | true | 入场门 | 量比不足拒单 |
| 15 | `liquidity_gate_enabled` | true | 入场门 | 流动性不足拒单 |
| 16 | `borrow_pool_gate_enabled` | true | SHORT 门 | 借券不足拒单 |
| 17 | `earnings_blackout_gate_enabled` | true | 入场门 | 财报窗拒单 |
| 18 | `falsification_gates_enabled` | true | 入场门 | 证伪门启用 |
| 19 | `duplicate_position_guard_enabled` | true | 执行门 | 同向持仓拒新开 |
| 20 | `portfolio_backstop.enabled` | true | 退出监控 | 组合兜底触发全平 |
| 21 | `playbook.topup.enabled` | false | 补充生成 | 当前不可达 |
| 22 | `entry_band_vol_widen_enabled` | false | 入场带放宽 | 行为关闭，仅 shadow 采集 |
| 23 | `overnight_hold_enabled` | false | 隔夜持有 | 当前主路径关闭 |
| 24 | `mfe_stall_exit_enabled` | false | 退出规则 | 机制休眠 |
| 25 | `entry_archetype_routing_enabled` | false | 限价路由 | 市价主路径；shadow 可用 |
| 26 | `confidence_sizing_enabled` | false | sizing | 当前不按 confidence 缩放 |
| 27 | `playbook.live_trial` | true | 毕业相关 | 数据口径，不代表已实盘 |
| 28 | `orb.enabled` | false | ORB 入口 | ORB 新仓路径不可达 |
| 29 | `risk.pdt_guard.enabled` | false | PDT 门 | 主路径 PDT 门休眠 |
| 30 | `scheduler.playbook_exit_fast_loop_enabled` | true | `scheduler_core:2883` | 关闭后回退慢宿主 |
| 31 | `playbook.limit_entry_shadow_observe` | false | shadow | 已按裁决 KILL |

### 1.3 跨界字段清单

| # | 字段 | 写方 | 读方 | 单位/空语义 |
|---|---|---|---|---|
| 1 | `symbol` | screener/vote/instruction | DB、service、broker、exit、report | Futu `US.*`；空=拒 |
| 2 | `direction` | vote/instruction | service、trade、exit | `BUY/SELL_SHORT`，DB 执行层转 `LONG/SHORT` |
| 3 | `confidence` | vote | instruction、sizing/排序 | 0–100 整数；`decision_conf=confidence/100` |
| 4 | `entry_price` | instruction/reprice | service、trade、exit | 美元；`<=0` 拒 |
| 5 | `stop_loss` | instruction/reprice | service、exit、open_positions | 美元；空/0 表示缺失 |
| 6 | `take_profit` | instruction/reprice | service、exit | 美元；0 可表示禁用 TP |
| 7 | `position_size` | sizing/reprice | service、broker、trade | 股数，INTEGER；缺失兜底 1 股 |
| 8 | `risk_pct` | config | sizing | 百分数，公式内 `/100` |
| 9 | `max_position_pct` | config | sizing | 小数，直接乘本金 |
| 10 | `daily_notional_cap_pct` | config | guardrail | 小数，直接乘净值 |
| 11 | `execution_state` | DB CAS | auto execute/recovery | `PENDING/UNKNOWN/SUBMITTED/FILLED_UNBOOKED/BOOKED` |
| 12 | `execution_attempt` | claim | 全部 CAS 写 | 单调递增整数 |
| 13 | `broker_order_id` | DualBroker | DB、exit owner、receipt | Futu 字符串；unknown 保持锁定 |
| 14 | `broker_env` | service | DB、恢复、对账 | `sim/real` |
| 15 | `submit_state` | DualBroker | service | `ACCEPTED/DEFINITIVE_REJECT/UNKNOWN` |
| 16 | `filled_qty` | broker query | booking/部分成交 | 股数；0 不按全量记账 |
| 17 | `filled_price` | broker query | booking | 美元；缺失保持 `FILLED_UNBOOKED` |
| 18 | `source_type` | service | guardrail/exit/report | `PLAYBOOK_SIM/PLAYBOOK_LIVE` |
| 19 | `entry_time` | service | exit owner/hold time | ET ISO；exit receipt 需同源 |
| 20 | `metadata_json` | service/exit | exit、归因、shadow | dict JSON；坏 JSON 不覆盖 |
| 21 | `exit_reason` | exit decision | booking/report | 字符串；含归因修正 |
| 22 | `exit_time` | booking | report | ET ISO |
| 23 | `pnl` / `realized_pnl` | booking | report | 美元毛额 |
| 24 | `pnl_net` | booking | report | `COALESCE(pnl_net, realized_pnl)` 净额 |
| 25 | `mfe_pct` / `mae_pct` | exit monitor | report | 百分数 |
| 26 | `exit_intent_id` | ledger | receipt | UUID；幂等身份 |
| 27 | `exit_owner` | monitor | ledger | trade 身份快照；冲突则 BLOCK |
| 28 | `receipt_json` | booking | ledger | 成交回执；不一致不 `BOOKED` |

### 1.4 判据与阈值清单

| # | 判据 | 当前值 | 失败方向 | 结论 |
|---|---|---:|---|---|
| 1 | kill switch | 文件状态 | 拦截；检查异常拦截 | 正确 |
| 2 | 交易日 | holiday calendar | 非交易日拦截 | 正常路径未发现漂移 |
| 3 | 生成时间 | ET 09:00 后 | 早于则等待 | 正确 |
| 4 | 开盘延迟 | 09:35 | 拦截 | 正确 |
| 5 | NO_TRADE / RISK_OFF / HALT | regime/risk state | 拦截；异常按 `preflight_fail_mode` | SIM 当前 fail-open，live 断言要求 closed |
| 6 | ACB | config closed | 异常拒单 | 正确 |
| 7 | capital safety hard gate | true | 异常/水位拒绝 | 正确 |
| 8 | VIX 数据缺失 | None | 拒单 | 正确；VIXGuard 内部异常仍放行但已有数据缺失门 |
| 9 | BUY 日配额 | 999 | 当前不拦截 | 有明确回滚值，非伪启用 |
| 10 | open notional budget | DB 查询 | 查询失败 fail-close | 正确 |
| 11 | daily notional | 80% 净值 | 查询失败从 0 放行；live 查错库 | 见 F-2 |
| 12 | daily count | 16 | 同上 | 见 F-2 |
| 13 | per-cycle cap | 3 | 顺延下一轮 | 正确 |
| 14 | spread | 5% | 拦截 | 活跃 |
| 15 | quote age | 60/90s | 理论拦截 | 本地时间戳导致近零样本，已在配置披露 |
| 16 | VWAP 偏离 | tolerance 0.003 | 拦截 | 活跃 |
| 17 | 量比 | 1.5 | 拦截 | 活跃 |
| 18 | 单笔风险 | 0.5% | sizing 缩小/0 股 | 正常 |
| 19 | 单名仓位 | 6% | clamp/截断 | 正常 |
| 20 | 退出 SL/时间/收盘 | exit config | 触发平仓 | 当前配置冻结 |

### 1.5 状态与分支清单

1. **入场执行状态机**: `PENDING -> UNKNOWN -> SUBMITTED -> FILLED_UNBOOKED -> BOOKED`；仅明确零成交终态可 `UNKNOWN/SUBMITTED -> PENDING`。当前库 `PENDING=1775`、`BOOKED=531`，无未决中间态。
2. **DualBroker submit**: `DEFINITIVE_REJECT / UNKNOWN / ACCEPTED`；有 order id 即持久化，不因成交查询失败丢失。
3. **Exit intent**: `PENDING / HELD / FILLED_UNBOOKED / BOOKED / BLOCKED`；`UNIQUE(account,target_env,close_key)` 与活跃 symbol 唯一索引。当前 18 个 intent 全部 `BOOKED`。
4. **Exit leg**: `PREPARED -> ACTIVE -> CANCEL_REQUESTED -> TERMINAL`；限价腿零/部分成交后可补市价腿。当前 32 个 leg 全部 `TERMINAL`。
5. **市场相位**: `PRE_MARKET / TRADING / AFTER_HOURS / CLOSED`；生成、执行、退出分别有独立时间窗。
6. **当前不可达主分支**: ORB 新仓、topup、overnight hold、MFE stall、entry band widen、limit entry shadow、PDT guard。
7. **低频少数派**: 入场部分成交、撤余单、限价超时、live 降级、组合 backstop、手动部分平仓。已复核部分成交与 durable exit；手动部分平仓发现 F-1。

### 1.6 定时与触发清单

| # | 触发 | 条件 | 幂等/崩溃语义 |
|---|---|---|---|
| 1 | `run_playbook_generate` | ET>=09:00，交易日 | DB 完成标记；半成品可重跑 |
| 2 | `run_playbook_morning_push` | 独立推送 | 文件状态 |
| 3 | `run_playbook_auto_execute` | TRADING，默认 180s | instruction CAS 防重复 |
| 4 | `monitor_playbook_sim_exits` | TRADING，10s | exit intent 唯一约束防重复 |
| 5 | `_resume_playbook_exits` | 每轮 exit loop 前先恢复 | 不确定单不会自动释放 |
| 6 | EOD force close | 收盘前/后兜底 | 当日 fail count；durable ledger 仍幂等 |
| 7 | daily review | ET>=16:00 | `data/.last_daily_review`；写失败退化内存幂等 |
| 8 | weekly/monthly review | 固定日 | scheduler 内存/文件 |
| 9 | cron | 系统 103 条活跃 | 未逐条审读；见 not_read |
| 10 | Hermes jobs | 25 条 | 仓外 JSON/脚本，未完整审读 |

## 2. 覆盖率

- 数据流阶段：14/14 已映射，14/14 已过 D1–D18 矩阵。
- 核心开关：31/31 主链路开关已给结论；362 个 YAML 布尔叶子中 331 个未逐项追踪。
- 跨界字段：28/28 关键字段已列契约；非主链路 schema 字段未全列。
- 阈值：20/20 关键阈值已判定。
- 状态机：5/5 主状态机已判定；分支 7 类已分类。
- 触发：10/10 主链路触发已判定；103 条 cron 与 25 条 Hermes 未逐条审读。
- D1–D18 显式结论：见第 3 节矩阵，共 14×18=252 个格位。
- 专项测试：60/60 通过（execution state、exit compensation、entry archetype routing）。

## 3. D1–D18 逐项矩阵

`H`=命中 finding/suspicion，`N`=本路径未发现，`A`=结构性不适用，`S`=suspicion/休眠。每格均有结论，不含“跳过”。

| 阶段 | D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 | D11 | D12 | D13 | D14 | D15 | D16 | D17 | D18 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 采集 | N | S | N | N | N | N | S | N | N | N | N | N | A | S | S | N | N | N |
| 2 筛选 | N | N | N | N | N | N | N | N | N | N | N | N | A | N | N | N | N | N |
| 3 投票 | N | N | N | N | N | N | N | N | N | N | N | N | A | N | N | N | N | N |
| 4 持久化 | N | N | N | S | N | N | S | N | N | N | N | N | A | N | N | N | N | N |
| 5 推送 | N | N | N | N | N | N | N | N | N | N | N | N | A | N | N | N | N | N |
| 6 触发 | N | N | N | S | N | N | S | N | N | N | N | N | A | N | N | N | N | N |
| 7 前置门 | N | S | N | N | N | H | S | N | H | N | N | N | N | N | N | N | H | N |
| 8 认领 | N | N | N | N | N | N | N | N | N | N | H | N | A | N | N | N | N | N |
| 9 sizing/下单 | N | N | N | N | N | N | N | N | N | N | H | N | N | N | N | N | N | N |
| 10 成交簿记 | N | N | N | N | N | N | N | N | N | N | H | N | A | N | N | N | N | S |
| 11 退出监控 | N | N | N | N | N | N | N | N | H | H | H | N | N | N | N | N | N | N |
| 12 durable exit | N | N | N | N | N | N | N | N | N | N | H | N | A | N | N | N | N | N |
| 13 回写 | N | N | N | N | N | N | N | N | N | N | H | N | A | N | N | N | N | S |
| 14 报表 | S | N | N | N | S | N | N | N | N | S | N | N | N | N | S | N | N | S |

## 4. Findings

### F-1 [已修复] 资金安全：手动部分平仓后 live SL/TP 重建必然跳过

- **pattern_id**: D1（契约漂移；同时是跨界字段错源）
- **location**: `app/web_dashboard/order/executor.py:423`、`app/web_dashboard/order/executor.py:667`、`app/web_dashboard/order/executor.py:1791`
- **claim**: `_rebuild_protective_after_partial` 声称复用仓位原有 `current_sl/initial_tp`，但传入的 `row` 来自 Futu `position_list_query`，不含这两个 DB 字段，因此读取恒为 0。
- **failure_scenario**: live 手动部分平仓 10/100 股 → 先 `cancel_order_group` 撤掉既有 SL/TP → `_sl_price/_tp_price` 均为 0 → 函数在 `app/web_dashboard/order/executor.py:1803` 直接 return → 剩余 90 股在 broker 端没有 SL/TP。
- **blast_radius**: 影响实盘资金安全；静默（只写 debug 日志）；不污染历史数据；剩余仓位仅靠本地 ExitMonitor，进程/数据断供期间无 broker 端保护。
- **refutation_attempt**: 检查了调用链与 Futu row 构造，`row` 仅来自 `position_list_query`；`current_sl/initial_tp` 是 `open_positions` 列。当前 SIM 路径在函数内提前 return，且 `ALLOW_REAL_TRADING=false`，因此当前姿态不可触发；但 live 手动路径一旦打开即必然触发。
- **fix_direction**: 删除“从 broker row 读 DB 字段”的实现；在撤销保护单前按归一化 symbol 从 `open_positions` 查询价位和订单 ID，重建失败时 fail-close 报 CRITICAL，并保留可对账状态。补 live 部分平仓契约测试。

### F-2 [已修复] 资金安全：单日 guardrail 在 live DB 路由下读 sim 库且查询失败放行

- **pattern_id**: D6 + D17（失败方向错误 + 收口路径旁路）
- **location**: `app/playbook/pipeline.py:3785`、`app/playbook/pipeline.py:3800`、`app/playbook/pipeline.py:4577`、`app/playbook/playbook_service.py:1764`、`app/core/paths.py:71`
- **claim**: 单日 notional/count 查询硬编码 `/opt/ftseek/FTSeek-US/data/ftseek_us.db`；live 簿记走 `get_trade_records_db()` 返回 `ftseek_us_live.db`。
- **failure_scenario**: `TRADE_ENV=0` 且通过 live 安全断言；当日 live DB 已有 16 笔已平仓交易。下一轮 auto execute 读取 sim DB 得 `count=0/notional=0`，而已平仓交易也不再计入 open notional budget，于是可继续开第 17 笔，突破 `max_daily_positions=16`；查询异常时也重置为 0 并继续。
- **blast_radius**: 只影响 live 启用后的新开仓资金安全；SIM 当前不可触发。查询异常为静默 warning；不会污染历史数据，但会突破单日限额。
- **refutation_attempt**: 复核了预算门：open notional 查询确实 fail-close 且走 `get_trade_records_db()`，但已平仓交易退出后不再占用 open notional，不能替代“当日开仓数”门；live 路径当前被 `ALLOW_REAL_TRADING=false` 和启动断言阻断，所以是 live 前置缺陷而非当前 sim 事故。
- **fix_direction**: 删除硬编码路径，与 booking 共用 `safe_sqlite_connect(get_trade_records_db())`；查询失败置 `count/notional=+inf` 或直接返回 fail-close。补 `TRADE_ENV=0` 的 guardrail 单测，断言读 live DB 且查询失败不开仓。

### F-3 [已修复] 报表契约：daily summary 的 `pnl_basis` 声称净额，但基础 total/win 字段仍是毛额

- **pattern_id**: D1 + D18
- **location**: `app/review/daily_review.py:898`、`app/review/daily_review.py:903`、`app/review/daily_review.py:929`
- **claim**: summary 同时输出 `total_pnl/win_rate`（gross）和 `total_pnl_net/win_rate_net`，但 `pnl_basis` 统一声明为 net。
- **failure_scenario**: 单笔交易 `realized_pnl=+$1`、费用 `$2`、`pnl_net=-$1`；summary 返回 `total_pnl=1`、`win_rate=1.0`，同时 `pnl_basis='net...'`。任何直接消费 `summary['total_pnl']` 或 `summary['win_rate']` 而未选择 `_net` 字段的下游会得出盈利/胜率结论。
- **blast_radius**: 不直接下单；可能静默误导后续决策。当前 Obsidian 渲染端已优先使用 `_net` 字段，因此用户-facing postmarket 已缓解；JSON 消费方仍暴露混合口径。
- **refutation_attempt**: 检查 `app/review/daily_review.py:1455`，渲染端确实优先读 net 字段；但函数返回的 summary 本身仍保留矛盾声明和混合字段，`fake_wins` 也证明系统已意识到毛/净差异。
- **fix_direction**: 优先删除 gross 字段，把 `total_pnl`/`win_rate` 本体改为净额；若必须保留兼容字段，重命名为 `total_pnl_gross`/`win_rate_gross`，并把 `pnl_basis` 改为逐字段说明。更新 JSON 消费方契约测试。

### 4.1 修复回写（2026-09-16）

1. **F-1 已实施**: `close_position()` 在 live 部分平仓前调用 `_load_protective_snapshot()`，从 `open_positions` 按 `symbol`/`US.symbol` 读取 `current_sl/initial_tp`；快照缺失或任一价位非正时拒绝下单。`_rebuild_protective_after_partial()` 改为消费该下单前快照，不再读取 Futu 持仓行；新增缺失行、价位非正、成功重建契约测试。
2. **F-2 已实施**: 单日 guardrail 改用 `safe_sqlite_connect(get_trade_records_db(), timeout=5)`，连接在 `finally` 关闭；查询失败返回 `reason=daily_guardrail_query_failed` 并整轮不开仓。新增 DB 路由与 fail-close 契约测试，DEB 源报告追加议题 #396。
3. **F-3 已实施**: `_generate_summary()` 的 `total_pnl/win_rate/winning_trades/losing_trades/avg_pnl` 均改为 `COALESCE(pnl_net, realized_pnl)`；`*_net` 兼容字段输出同一净口径。新增净口径契约测试。
4. **S-1 已删除**: 移除指令级 `_is_instruction_expired()` 及 auto execute/intraday trigger 两处消费逻辑，保留不相关的 `daily_active_pool.expires_at` TTL；新增测试证明未持久化的 `expires_at` 不再改变执行结果。
5. **验证**: `PYTHONPATH=. ./venv/bin/pytest -q tests/web_dashboard/test_close_position_p1_contracts.py tests/test_postmarket_drawdown_caliber.py tests/playbook/test_daily_guardrail_db_contract.py tests/playbook/test_entry_revalidation.py` → 202 passed；`PYTHONPATH=. ./venv/bin/pytest -q tests/playbook/test_m2_b1_execution_state.py tests/playbook/test_entry_revalidation.py tests/playbook/test_offline_full_chain.py tests/test_post_market_audit_exit_day.py` → 214 passed。

## 5. 接缝审查表

| 相邻阶段 | 跨界字段/状态 | 结论 |
|---|---|---|
| 采集 → 筛选 | symbol、price、bid/ask、volume、ATR、VIX、SPY、observed_at | 未发现单位错配；Futu timestamp 为本地取值时间，age gate 近零生效且已披露 |
| 筛选 → 投票/指令 | direction、confidence、conditions、entry、SL、TP、size | `confidence` 0–100，`decision_conf` 0–1，转换明确 |
| 指令 DB → auto execute | `triggered/executed/execution_state/skip_reason`、entry/SL/TP/size | CAS 正确；未持久化的 `expires_at` 消费逻辑已删除，见 S-1 |
| auto execute → service | custom_params 覆盖 entry/SL/TP/size | 值为 None 不覆盖，避免丢原值 |
| service → DualBroker | symbol、direction、quantity、price、metadata、target_env | quantity 为股数，price 为美元，market price=0 是合法哨兵 |
| DualBroker → Futu | code、qty、trd_side、order_type、trd_env、acc_id | order id 先持久化，unknown 不重下 |
| broker fill → service | submit_state、filled_price、filled_qty、order_status | 部分成交按 dealt_qty；价格缺失保持锁定 |
| service → trade_records | source_type、entry_time/price、qty、order id、metadata | live 与 guardrail 均走 `get_trade_records_db()`；F-2 已修复 |
| trade_records → exit monitor | id、symbol、direction、entry、qty、metadata、source_type、order id | 未发现字段丢失；metadata 坏 JSON 不覆盖 |
| exit monitor → durable ledger | owner 身份、quantity、direction、decision、limit/timeout | owner 冲突/同 symbol 活跃即 BLOCK |
| ledger → broker | account/env/symbol/direction/qty/price/order id/status | account/env/order 身份校验，重复 resume 不重挂 |
| ledger → booking | receipt、exit price、qty、confirmed_at | receipt 不一致不 BOOKED |
| booking → report | exit_time、pnl、pnl_net、fees、mfe/mae、source | summary 基础字段与 `*_net` 均为净口径；F-3 已修复 |
| open_positions → manual partial close | current_sl、initial_tp、sl/tp order id | 下单前读取 DB 快照，缺任一价位则拒单；F-1 已修复 |

## 6. 端到端追踪

1. **正常成功**: watchlist → screener → vote → instruction → trigger/reprice → CAS `PENDING→UNKNOWN→SUBMITTED→FILLED_UNBOOKED→BOOKED` → trade_record → exit monitor → exit intent → broker fill → receipt → trade exit 字段。未发现中途字段被静默改写；`execution_attempt` 保证旧结果不能覆盖新轮。
2. **中途拦截**: kill switch、NO_TRADE、RISK_OFF/HALT、ACB、水位、VIX 缺失、预算/日帽、already-held 均在提交前返回或跳过；`exec_gate_audit` 与 skip_reason 留痕。未发现绕过统一执行入口的 Playbook 新仓路径。
3. **上游异常/缺失**: watchlist 为空会写完成终态；VIX None 拒单；broker position 查询失败降级空表（保守依赖 already-held 门）；预算 DB 与日 guardrail 查询均 fail-close，F-2 已修复。
4. **并发/重复**: 同 instruction 被 `try_claim_execution` 原子抢占；同 symbol 有非阻塞互斥锁；exit 有 `(account,target_env,close_key)` 唯一键和活跃 symbol 唯一索引。测试 60/60 通过。
5. **时间边界**: 生成/执行/退出均使用 ET；非交易日拦截；开盘 09:35；退出收盘分钟支持半日市；exit time 来自 confirmed timestamp。未复核全部节假日数据源，见 undecidable。
6. **少数派分支**: 入场部分成交按 broker `dealt_qty` 记账，`FILLED_PART` 不回退全量；限价腿超时后可补市价腿；组合 backstop 只在 TRADING+有报价时触发。手动 live 部分平仓分支原命中 F-1，现已按 DB 保护快照修复。

## 7. Suspicions

### S-1 [已删除] 指令级 `expires_at` 机制疑似 write-only

- **pattern_id**: D4/D7
- **location**: `app/playbook/pipeline.py:3853`、`app/playbook/pipeline.py:6206`
- **claim**: 消费端读取 `inst['expires_at']`，但 `_instruction_to_db_dict` 与 `playbook_instructions` schema 均未写入该字段；从 DB 取出的指令几乎恒无值。
- **why not finding**: 当前 auto execute 只取“今日”指令，日期过滤本身已隔日失效，无法构造当日超过 24h 的生产触发路径。
- **fix_direction**: 若 SCAN TTL 有业务意义，持久化并加契约测试；若没有，删除消费端与注释，避免伪机制。
- **处置**: 已删除消费逻辑；未持久化的 `expires_at` 不再影响触发或执行。

### S-2 报价 age 阈值是已知休眠门

- **pattern_id**: D2/D7
- **location**: `config/unified_config.yaml:1196`、`app/data/futu_lv3_provider.py:970`
- **claim**: Futu 本地 `time.time()` 使 60/90s 年龄恒近 0；黑名单和 spread 门承担实际防护。
- **why not finding**: 配置注释已如实披露“今天空转，但保留为未来真时戳兜底”，不存在声称与实现的漂移。
- **fix_direction**: 保持当前披露即可；若要激活，必须先改 provider 传交易所时间戳并 shadow 对照，不得直接调阈值。

### S-3 VIXGuard 内部异常放行

- **pattern_id**: D6
- **location**: `app/playbook/playbook_service.py:944`
- **claim**: VIX 数据获取失败 fail-close，但取得数值后 VIXGuard 评估异常会放行。
- **why not finding**: 需要构造 VIX 值有效但 guard 内部异常的具体输入；当前未能复现。
- **fix_direction**: live 前统一为 fail-close，或将 VIXGuard 纯阈值逻辑改为无异常路径。

## 8. 负面清单

### not_read

范围总量：`app/` 473 个 Python 文件 / 187,417 行；`scripts/` 414 个 Python 文件 / 99,624 行；DEB 报告 2,746 行。已完整读取的文件为 0。实际读取区间如下：

| 文件 | 总行数 | 已读区间/内容 |
|---|---:|---|
| `app/playbook/pipeline.py` | 8,785 | 2140–2310、3598–3826、3828–4175、4240–4355、4540–4645、6206–6224、7786–8535、8730–8800（原快照区间；本次修复后已另读 3780–3815 与删除的 TTL 函数前后） |
| `app/playbook/playbook_service.py` | 2,543 | 760–1000、1117–2050 |
| `app/playbook/db.py` | 1,849 | 50–130、580–775、780–970、1540–1576 |
| `app/execution/dual_broker.py` | 545 | 190–350、365–437 |
| `app/execution/exit_lifecycle.py` | 535 | 45–269、270–520 |
| `app/web_dashboard/order/executor.py` | 2,008 | 390–690、1785–1848 |
| `app/core/scheduler_core.py` | 3,084 | 2180–2340、2870–2945 |
| `app/core/scheduler_triggers.py` | 1,492 | 437–510 |
| `app/review/daily_review.py` | 1,482 | 330–385、890–940、1435–1470 |
| `app/review/review_orchestrator.py` | 2,467 | 120–225、1610–1675 |
| `app/data/futu_lv3_provider.py` | 2,337 | 190–225、930–1045 |
| `config/unified_config.yaml` | 3,264 | 1120–1260、1488–1515、1780–1810、2025–2040；另机器枚举全部布尔叶子 |
| DEB 报告 | 2,746 | 标题索引、#299、#335、#346、#347、#338 相关节选；本次回写 #335/#342/#396 |
| cron/Hermes | 103/25 | 只统计数量并抽样看 root crontab；未逐条读取脚本 |

其余 app/scripts 文件、数据库 schema 以外字段、外部 cron 脚本未读取，原因是本次按生产主链路优先，且全量逐行超出单轮可验证范围。

### undecidable

1. Futu SDK 在异常/部分成交/撤单竞态下的真实返回字段组合，需要真实或录制 broker fixture。
2. `open_positions` 与 broker 持仓在真实 live 手动部分平仓时的同步时序，需要可复现实盘测试账户。
3. Hermes 25 个仓外 job 的 prompt/脚本行为，需要读取 `/root/.hermes` 并确认运行时版本。
4. 103 条 cron 的幂等键和崩溃恢复，需要逐脚本追踪状态文件。
5. 交易日历供应商未来数据的完整性，需要外部日历对账。
6. 业务方是否接受 `summary` 中 gross 字段作为兼容输出，影响 F-3 的修复形态。

### assumed

1. 进程 `/proc/<pid>/environ` 与 YAML 当前值即生产姿态。
2. `AUTO_TRADE_ACCOUNT=sim`、`TRADE_ENV=1`、`ALLOW_REAL_TRADING=false` 表示当前无 live 下单。
3. `risk.preflight_fail_mode=open` 在 SIM 姿态下是有意配置；live 断言视为业务要求。
4. 现有 `data/ftseek_us.db` 与 `data/playbook_exits.db` 的行数/状态可代表当前运行事实。
5. 本轮已修改代码，因此按 AGENT.md 要求在测试后重启并运行启动冒烟；结果通过。
6. AGENT.md 中“9 月退出参数冻结、四锁+毕业门+用户签字前禁止实盘”是当前治理约束。
7. 历史报告中的已修复裁决可信，但本次仍复核了关键实现。

### out_of_scope

1. 当前工作区已有大量无关未提交变更，本报告不判断其 diff 正确性。
2. 未审计 Hermes prompt 安全、外部模型输出质量、OpenD 版本兼容。
3. 未审计非 Playbook 旧管线、新闻源、通用 web dashboard 安全。
4. 未执行性能压测和长周期回测。

## 9. 自检

**(a) 可复现性**：F-1/F-2/F-3 与 DB/状态计数可由固定命令复现；D1–D18 矩阵对同一代码快照可复现。主观点主要是“live 前置缺陷是否按 finding 而不是 suspicion 计数”，本报告标准是：代码路径一旦通过已声明的 live 前置条件即必然触发，计 finding；当前配置完全阻断但代码错误仍在，仍属 live 前置缺陷。

**(b) 依赖假设**：若生产姿态不是 sim、或 live 断言另有豁免，F-2 会从 live 前置缺陷变为当前事故；若 `pnl_basis` 的业务含义只约束 `_net` 字段，F-3 会降级为兼容命名问题；若 `open_positions` 在调用前被外部改写并注入 broker row，F-1 不成立，但当前调用链未发现该路径。

**(c) 最可能未检出的缺陷类型**：仓外 Hermes/cron 与仓内代码的接口漂移。原因是这些触发点在 `/root/.hermes`、shell、cron 和运行时脚本中，静态 Python 检索无法完整证明字段与时间口径；现有 config census 也明确说明外部消费方是盲区。

## 10. 优化方案

1. **P0/live 前修 F-1（已完成）**: 先查 `open_positions`，再撤保护单；live 部分平仓测试已覆盖缺行、价位非正与成功重建。
2. **P0/live 前修 F-2（已完成）**: guardrail 与 booking 共用同一 DB 路径；查询失败 fail-close；DB 路由与 fail-close 测试已通过。
3. **P1 修 F-3（已完成）**: summary 基础字段改为净口径，`*_net` 兼容字段同值，净口径契约测试已通过。
4. **P1 清理 S-1（已完成）**: 删除无写入方的 `expires_at` 消费逻辑，并保留“未持久化字段不影响执行”的回归测试。
5. **保持当前强项**: 入场 CAS、exit durable ledger、receipt 校验、部分成交按 `dealt_qty` 记账不要重构。
6. **live 检查顺序（当前进度）**: F-1/F-2 已修复并通过专项测试；后续仍需 `risk.preflight_fail_mode=closed` → 启动断言通过 → 冒烟通过 → 用户签字；不要先调交易参数。
