# 切实盘：执行链路 + 滑点 现状、SIM↔REAL 差异与优化清单（2026-09-20）

> 触发：用户问「按未来切实盘的要求，当前链路流程和滑点如何优化调整，实盘与 SIM 的差距
> 和注意点在哪」。本文只做**只读取证 + 清单**；未改任何代码/配置/门/阈值，未重启服务，
> 未下单。取证源：`app/execution/*`、`app/pipeline/order_executor.py`、
> `app/core/scheduler_core.py`、`scripts/extended_hours_*`、`playbook_exits.db`、
> `trade_records`、`extended_hours_exit_executions.jsonl`、`crontab -l`。

## §1 现状链路（截至 2026-09-20）

### RTH（盘中）

```
信号 → 风控门 → (开仓前刷新 buying_power) → broker_adapter.place_order
        ├ 限价 NORMAL / 市价 MARKET, session=Session.RTH, time_in_force=DAY
        ├ broker 前兜底门禁 _notional_sanity_check (平仓单豁免)
        └ 下单后**单次** order_list_query 确认, 不 sleep
             └ 延迟性拒单兜底: position_sync (30s)
出场: playbook_exit_fast_loop 每 20s 一轮 (配置 PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC,
      端到端退出延迟 ~25s), 走 ExitLifecycle:
        限价腿(带 deadline) --超时--> 撤单 --> 剩余量发 MARKET 补腿(30s deadline)
```

### ETH（盘前/盘后）

```
入场: extended_hours_entry_executor, cron */5 (16-21 / 4-7)
        └ place_order(order_type=NORMAL 限价, session=Session.ETH,
                      time_in_force=DAY, remark="ext_entry")
        └ wait_order_fill: 部分成交 → 撤余量 → 等券商终态
出场: extended_hours_exit_cron, cron * * * * * (每分钟)
        └ extended_hours_exit_executor → dual_broker.close_position(session="ETH")
        └ ExitLifecycle: 限价 deadline = EXT_EXIT_LIMIT_TIMEOUT_S=60(现网 env)
        └ ETH 特例: 限价腿终态且未全成 → **HELD, 不转市价**
                    (exit_lifecycle.py:519-522 `exit_eth_partial_fill_held`)
```

### 滑点/延迟的测量（已接线，非空白）

`order_executor._build_execution_metadata` 每笔记录
`signal_price / fill_price / slippage_pct(正=不利) / slippage_alert / execution_latency_ms`
入 `trade_records.metadata_json`；滑点 >0.5% **仅告警**（`slippage_control.is_acceptable`
在 broker 前**未被用于拦截**；`app/risk/engine.py:394` 亦仅 warning）。

入场下单价：`_get_current_price` → adapter 实时价，失败降级 `data_router.get_quote`
**优先 bid/ask 中价**，最后才用 `last_price`（并 warn）。持仓股数一律 `int()`，
库内**0 行小数股**（`open_positions` / `trade_records` 实测）。

## §2 SIM 与 REAL 的差距（按"切盘会立刻踩到"排序）

| # | 差异 | 代码事实 | 切盘后果 |
| --- | --- | --- | --- |
| 1 | **ETH 链有"仅模拟"硬闸** | `extended_hours_exit_executor.py:183-189`：`FUTU_TRD_ENV` 非 `SIMULATE` → `skip=real_not_authorized` 并推告警；同处 `acc_id` 取自 **`SIMULATE_ACCOUNT_ID`**（默认 19930823）；`close_position(..., target_env="sim")` **硬编码** | 切盘需逐点解除，否则 ETH 出场静默不执行（有推送，不会静默） |
| 2 | **ETH 入场 env 硬编码** | `extended_hours_entry_executor.py:1778` `env = TrdEnv.SIMULATE`（常量，非读配置/环境变量） | 切盘后 ETH 入场仍打模拟盘 → 实盘无 ETH 仓 |
| 3 | **券商止损单 SIM 不支持** | `broker_adapter.place_stop_order`：`trd_env==SIMULATE` → `simulate_not_supported` graceful degrade；ETH 入场记录 `sim_no_broker_stop: True` 并推送提示 | **REAL 下才会真挂 STOP 单** —— 止损从"软件轮询"变为"软件+券商双保险"；SIM 的止损时延（RTH 20s / ETH 1min）暴露在实盘是**未被度量的** |
| 4 | **成交语义** | SIM 不建模点差、队列、部分成交：实测保本出场 82%（146/177）限价未在 3s 内成交→转市价，且按经济方向换算**净 +$3,937 有利** | 真实盘口下这一项很可能翻成**成本**；SIM 滑点**不可外推**（入场滑点中位绝对值 4.0bps 只是 SIM 数字） |
| 5 | **成交确认延迟** | EXT 实测：执行开始→成交 26s / 31s（ACN、WBD）；原 20s 超时导致 8/15 条 `ok=false` **假负**（其中 2 例经 trade_records 核对已成交）→ 现网已改 60s | REAL 回报更慢，超时值需按真实回报分布重新标定，否则"未成交"误判会触发重复下单 |
| 6 | **时段×单型约束** | ETH 路径已是 NORMAL 限价 ✓；但 `exit_lifecycle.send` 对 `limit_price==0` 发 **MARKET + session=ETH**（当前调用点均保证 price>0，属**潜在**而非现存缺陷）；RTH 路径硬编码 `Session.RTH` | 实盘 ETH 市价单会被拒；需在 ExitLifecycle 内加断言（fail-close） |
| 7 | **购买力/融资/可借量** | 开仓前刷新 broker `power` 并推送（`position_sync`）；`acctradinginfo_query`（`max_cash_buy`）**已验证可用但 0 处调用**；借券预检已按 DEB-20260823 删除（死门） | SIM 无购买力约束；REAL 需下单前**硬门**（否则拒单=漏信号） |
| 8 | **费用口径** | `fees_est` 为模型值；REAL 实测 30 单误差 29/30 ≤$0.02（模型可信），真成本是 **$1.99/腿地板**（90% 往返至少一腿触，中位名义 $5,692 → 6.99bps/往返） | SIM 期费用只有估算；REAL 才结算 |
| 9 | **防重/幂等** | 同进程：`SymbolLock` + push dedup；ETH 出场：读审计文件跨进程防重；ETH 入场：券商活动单对账 | REAL 重复下单=真钱损失；RTH 主链路的**跨进程**防重需再核 |
| 10 | **风控模式** | 滑点仅告警（0.5%）；`_notional_sanity_check` 为 broker 前最后一道墙 | REAL 建议升级为分档处置（先影子记录再预注册） |

## §3 优化清单（按"切盘必需 → 值钱"排序）

### P0 — 切盘前必须完成（都为小改 + 可 dry-run 验证）

1. ✅ **SIM→REAL 开关清单化（2026-09-20 已完成，见 §6）**：ETH 链已从"自带一套 legacy 判定"
   收敛到与主链路同源的 `TradingMode`；新增可执行验收 `scripts/live_switch_audit.py`。
   ~~含 `ext_entry` 的 `env` 常量、`ext_exit` 的 `real_not_authorized` 闸 + `target_env="sim"`、
   账号环境变量命名、`app/web_dashboard/order/executor.py` 12 处 `TrdEnv.SIMULATE`~~
   —— 更正：dashboard 侧**本就是** `TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE`
   ＋`REAL_ACCOUNT_ID/SIMULATE_ACCOUNT_ID`（已是 SSOT 模式），不属于待改项；上一轮
   "12 处硬编码"的说法不准确。
2. **REAL 专属下单前硬门（fail-close）**：①`acctradinginfo_query` 真实购买力
   ②做空前的可借量/保证金 ③ETH 断言"必须限价"。三项都在 broker 前，且**只对 REAL 生效**。
3. **STOP 单实盘语义先实测再当主止损**：`place_stop_order` 未传 `session/time_in_force`
   → 用最小额（1 股）在 1 个标的上实测触发与有效期（先例 `scripts/probe_sim_stop_20260805.py`），
   再决定券商止损与软件循环的主从关系。
4. **端到端退出延迟入监控**：把 RTH(~25s) / ETH(1min cron + 60s 限价，最坏 ~2min)
   的实测延迟做成指标 + 告警阈值；ETH 出场评估对齐 RTH 的 20s 快循环。

### P1 — 值钱且可量化

5. **用 REAL 成交校准滑点模型**：现有 `entry_fill_impact_report`、`order_fee_query` 金标准、
   `slippage_pct` 字段齐备 → 切盘后按周出"信号价 vs 成交价"分档表（分 RTH/ETH、分单型）。
6. **滑点分档处置**（先影子）：>0.5% 告警 / >1.5% 拒单候选；按用户纪律**先 record-only 攒样**，
   预注册判据后再上线拦。
7. **下单价单一入口 + 断言**（上轮 P0-2 延伸）：现中价优先已实现，缺"禁 snapshot
   `last_price` 当实时价"的显式断言。

### 明确不做

- 不为了"实盘而实盘"改成交模型/加复杂路由；TWAP 已于 2026-04-26 移除（风控层已约束规模）。
- 不在参数冻结期动任何阈值；不新建影子通道（除非上述 P1-6 预注册）。

## §4 切盘操作注意点（红线）

1. **SIM 的执行层结论一律不可外推**，尤其 ETH 薄盘（成交率、滑点、限价兜底方向）。
2. **第一步必须是最小额度**（1 股 / 单标的 / 单时段），并做三处回读对齐：
   券商订单 ↔ `data/extended_hours_exit_executions.jsonl` ↔ `trade_records`。
3. **确认延迟 ≠ 未成交**：判据固定为"jsonl/日志失败 但 trade_records 有 exit_price 且券商有成交"。
4. **重复下单是实盘头号风险**：任何"超时重试"必须先去券商查活动单，再决定补单。
5. **费用在 REAL 才结算**：$1.99/腿地板是切盘后必然吃到的固定成本（T3 批次覆盖其判据）。

## §5 局限

- 本文结论基于**代码静态取证**（未在 REAL 账户上实弹）；STOP 单在 US 的真实触发/有效期、
  REAL 回报延迟分布、部分成交率均**未实测**，需切盘前用最小额探针补齐。
- 滑点/成交率数字全部来自 SIM，已在 §2 第 4 条标注不可外推。

## §6 本批落地（2026-09-20，开关清单化 + 可执行验收）

**代码改动（SIM 行为零变化，已回归）**

| 文件 | 改动 |
| --- | --- |
| `app/core/trading_mode.py` | 新增 `target_env()` / `account_id()` / `futu_trd_env()`（沿用既有 SSOT：`.env.us` 热读 + `AUTO_TRADE_ACCOUNT`+`ALLOW_REAL_TRADING`）；新增 `ext_channel_real_authorized()`（ETH 链路实盘**独立闸**，默认 false） |
| `scripts/extended_hours_entry_executor.py` | 5 处 legacy 收敛：`env` 常量 → `_mode().futu_trd_env()`；对账调用 → `_mode().account_id()`；两处"仅 SIM"守卫 → `_mode().is_live and not _ext_real_authorized()` |
| `scripts/extended_hours_exit_executor.py` | 裸读 `FUTU_TRD_ENV` + 裸读 `SIMULATE_ACCOUNT_ID`（且除守卫外**无他用**）→ SSOT；`target_env="sim"` → SSOT 解析 |
| `scripts/live_switch_audit.py` | **新增**：打印当前模式 + 全仓解析点分类（SSOT/映射 / BLOCKER 常量 / LEGACY 裸读 / INFO）；`--assert-ready` 退出码门 |
| `scripts/real_order_probe.py` | **新增**：实盘最小额探针。只读档 `--probe buying-power` 可直接跑；下单档 `--probe stop-order` 需 `TradingMode=live` ＋ `--confirm-real-order <REAL_ACCOUNT_ID>` 完全一致，qty 恒 1 股，止损价强制距现价 ≥15%（保证不可能成交），下单后立即撤单并回读终态 |

**验收证据**

- `live_switch_audit.py --assert-ready` → **EXIT 0**：BLOCKER **0**、LEGACY **0**、SSOT/映射点 225。
- 行为等价：`extended_hours_entry_executor --session premarket --dry` 与
  `extended_hours_exit_executor ... --dry` 均返回 `outside_trading_window`（周日，零下单）。
- 回归：`test_ops_scripts_sync` + `test_control_loop_ledger` + `test_extended_hours_order_execution`
  **51 passed**。

**REAL 账户实测（只读，2026-09-20 Sunday）**

| | SIM (19930823) | **REAL (281756480166019917)** |
| --- | --- | --- |
| `currency` | N/A | **HKD** |
| `total_assets` | 99,392.09 | 660,457.63 |
| `cash` | 99,392.09 | 84,157.63 |
| `market_val` | 0.0 | **576,300.0**（已有在手持仓） |
| `power` | 198,784.19 | 494,450.72 |
| `hkd_assets / usd_assets` | N/A | **−63,947.15 / 92,339.62** |

推论（切盘注意点，新增）：
1. **REAL 是 HKD 计价且币种余额一正一负**（HKD 负、USD 正）→ 净值/购买力必须走
   `broker_adapter.get_account_info` 的 HKD→USD 换算路径（`usd_assets/hkd_assets` 优先、
   固定 7.80 兜底）。`broker_adapter.py:885` 那句"美股账户 HKD 分支本就不可达"**已过时**。
2. **实盘账户已有在手持仓**（HKD 576,300）→ 切实盘第一天的"总敞口/单笔名义帽"必须把既有
   持仓计入，否则 30% 名义帽会被错误地当成"空闲额度"。
3. `acctradinginfo_query` 的 `max_cash_buy` —— **已定谳，见 §7.1**。

## §7 REAL 账户会计口径修正 + 切盘硬阻塞（2026-09-21 同日落地）

### 7.1 定谳：`acctradinginfo_query.max_cash_buy` 的单位是**股数**（given price）

同账户同标的改 `price` 观察缩放：SIM 1987→993→496→248（price 50/100/200/400，比值 4.01）、
REAL 1500→750→375→187（比值 4.00）→ **股数**（金额假设下应不变）。
故它可作"该价位最多能买几股"，**不是**"可用金额"，接硬门时勿混用。

### 7.2 修正：非 USD 账户的 fx / 净值 / 现金口径（旧实现三处错）

REAL 账户是 **HKD 计价且带本币负腿**（`hkd_assets = −63,947.15`），旧实现系统性偏差：

| 字段 | 旧实现 | 实测真值 | 偏差 |
| --- | --- | --- | --- |
| `_fx_rate` | `usd_assets/total_assets` = 0.1398（**USD 侧占比，不是汇率**） | 0.12747（隐含 7.8452 HKD/USD） | +9.68% |
| `total_assets`（净值=风险基准） | 取 `usd_assets` = 92,339.62（USD **毛**侧，未扣本币负腿） | `660,473.79 × fx` = **84,188.48** | **+9.68%** |
| `cash` | 优先 `us_cash` = 92,339.62 | `84,173.79 × fx` = **10,729.55** | **+8.6×** |
| `market_val` | 576,300 × 0.1398 = 80,564 | **73,459.12** | +9.68% |
| `buying_power` | `usd_net_cash_power` = 31,513.56 | 同左（与 `avl_withdrawal_cash/fx` 精确互证） | 0（保留） |

推导依据（两条独立互证，非猜测）：
1. Futu 字段自洽式 `total_assets(本币) = usd_assets × FX + hkd_assets` →
   FX = (660,473.79 + 63,947.15)/92,339.62 = **7.8452**（回代误差 0.00）；
2. `us_avl_withdrawal_cash`(31,513.55) == `avl_withdrawal_cash`(247,229.47)/7.8452 = 31,513.6 ✅
   → 证明 `us_*` 本义是"对应字段的 USD 折算"，而本账户 `us_cash` 却等于 `usd_assets`
   （≠ `cash/fx`）→ **`us_cash` 在此账户下语义不可信**（SIM 下二者恰好相等，使旧分支看似正确）。

**为什么必须修**：净值是水位/回撤/`_notional_sanity_check`（30% equity 兜底帽）的基准，高估 9.68%
= 帽子放大 9.68%；`cash` 高估 8.6× 会让"现金 < 5% 初始资金"的告警/锁定阈值失真。

**改动**：`app/execution/broker_adapter.py::get_account_info` 非 USD 分支 —— fx 改由上述自洽式推导
（退化到 `_get_fx_rate` 固定值）；净值改 `total_assets(本币) × fx`；`cash` 改为"折算值 vs `us_cash`
交叉校验（≤1% 才采信券商字段）"，并记 `_cash_source`；新增审计键 `_usd_side_assets` /
`_usd_cash_field` / `_buying_power_source`。
**SIM（`currency='N/A'`）不进该分支 → 行为逐点不变**（`accinfo_before/after.json` 快照 diff 已验）。
**回归**：新增 `tests/test_accinfo_fx_conversion.py`（录下券商原始行做桩，5 项断言锁死口径）；
连同 `test_extended_hours_order_execution` / `test_playbook_position_sizing` / `test_daily_loss_circuit`
等 **89 passed**。

### 7.3 更正：解锁密码**不是**生产阻塞（自证伪，同日撤回）

初次只用 `python script.py` 直接跑探针时，`FutuBrokerAdapter.connect()`（`trade_env=0`）拒绝：

```
[富途·实盘] ❌ 解锁密码未设置！无法解锁真实交易账户，下单将被拒绝。
```

我当时判为"切盘硬阻塞"。**该结论是错的**（同日自证伪）：

- `.env.us` 中 `FUTU_UNLOCK_PWD_MD5` **已配置且非空**（32 位 MD5）；
- systemd 单元含 `EnvironmentFile=/opt/ftseek/FTSeek-US/.env.us` → 生产进程 env 里确有该键
  （实测 `tr '\0' '\n' < /proc/<MainPID>/environ` 命中）；
- 该键由 `os.getenv` 读取，**只认进程环境**；我的探针没加载 `.env.us` 才读不到。
- 加载后（`set -a && . ./.env.us`）REAL `connect()` **成功**，端到端取证通过（见 §7.5）。

⇒ 正确结论：**这是自建探针的配方坑，不是切盘阻塞**。凡是要在独立进程里连 REAL 的脚本，
必须先注入 `.env.us`（且**不得打印任何凭证值**）。生产侧无需额外动作。

### 7.4 既有持仓与敞口（切盘注意）

- 实盘账户**已有在手持仓**：`market_val` = 576,300 HKD ≈ **$73,459 = 87% 净值**。
- 仓位基准 `risk_base` 取 `buying_power`（= `usd_net_cash_power` ≈ $31,518，已扣持仓占用）→ 这点是对的；
  但 `_notional_sanity_check` 的 30% 帽只看 equity，**不看既有敞口**。
- ⇒ 切盘首日沿用 30% 单笔帽，理论上可把总敞口推到 equity 的 ~117%。**建议**新增"总敞口帽
  =（既有 `market_val` + 新笔名义）≤ equity × X%"，X 由用户定（候选：100% 保守 / 150% 允许融资）。
  **未擅自设定**——这是风控数字，须用户裁决后预注册再落地。

### 7.5 端到端复验（加载 `.env.us` 后走真实代码路径）

| 字段 | 修正前（实测） | 修正后（实测） |
| --- | --- | --- |
| `total_assets`（净值） | 92,339.62 | **84,188.45** |
| `cash` | 92,339.62 | **10,729.05**（`_cash_source` = "cash×0.1275(us_cash 不一致, 弃用)"） |
| `market_val` | 80,564（经伪汇率折算） | **73,459.40** |
| `buying_power` | 31,513.56 | 31,518.33（券商 `usd_net_cash_power`，保留） |
| `_fx_rate` | 0.1398（伪汇率） | **0.12747** |
| 30% 兜底帽 | 27,701.89 | **25,256.53** |
| SIM 侧 | 99,392.093 / 198,784.186 | **逐点不变**（快照 diff） |

注：`market_val` 修正前是经伪汇率 0.1398 折算的 80,564（≈636,006 HKD 口径下的口径混用读数）；
修正后 = 576,300 × 0.12747 = 73,459.40，与"净值 ≈ 现金 + 持仓市值"恒等式自洽。