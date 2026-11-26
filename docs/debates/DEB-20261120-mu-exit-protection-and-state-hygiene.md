# 议题：US.MU 出场保护核查 —— Playbook 提交误分类源头修复与既有观测缺陷

## 状态

**已决策·已部署**（2026-11-20 09:00:23 CST 受控重启，冒烟 7/7）。三项修复落地并各有实盘/变异证据；
四项更大面问题**只立案不修**（见 §6），须另裁。触发源：用户「请查看当前 MU 的退出策略是否正常，
其最高到回撤有不符合预期的现象」→「直接修复」。

**源头修复最新状态（2026-11-20 14:14 CST）· 已完成部署验收**：12:49 生产 apply exit0（5666 BOOKED、唯一PLAYBOOK_SIM账 **id1934**、LONG6@705、成交ET09:56:42、peak759.57、3只读查询/0订单变更）。用户再次明示「授权重启服务」后 **14:13:17 受控重启成功**（PID3574534→**3591909**），冒烟 **7/7 exit0**；启动对账 `Futu:2 | 主线内存:0 | playbook自管:2 | 一致性✓`，MU/LLY 均被 reconciler 与持仓恢复明确判「playbook自管退出、不进主线store」⇒ **MU 已交回原 Playbook BE/移动/回撤止盈宿主，无双管/两不管**。重启后 MU BOOKED/peak759.57、LLY未变、ERROR/CRITICAL/Traceback 0、G-02 drift归负、5文件确在进程内（mtime12:26<启动14:13）。备份早期SHA异常已如实追加最终指纹与机制未复现局限（§11.2）。未改原参数/auto_execute/四锁、未人工交易、未提交推送，扫描按用户选择跳过。**未验证**：下一RTH自然移动/回撤止盈尚未发生（市场已收盘，sync paused），命中0只读作未发生。详见§11–§12。前段09:00「已部署」仅指R2/R3/R4观测批。

## 1. 用户观察成立：MU 全程回吐，零锁盈

只读实测（今日 1m K 实调 + DB + 日志三路）：

| 项 | 值 | 来源 |
|---|---|---|
| 入场 | 705.00 @ 2026-11-18 09:56:44 ET，6 股 | `open_positions` |
| 今日峰值 | **760.00 @ 11-19 10:09 ET**（trail 记 759.57，为该 1m bar 内真实报价） | `request_history_kline K_1M` 实调 |
| 今日最低 | 710.60 | 同上 |
| 收盘 | **690.26**（−2.09% vs 入场） | `prev_scan_price`，`prev_scan_ts`=11-19 15:59:54 ET |
| **峰值→收盘回吐** | **−9.18%**（759.57→690.26） | 计算 |
| MFE | +7.74% | 计算 |
| DB `current_sl` | 669.47，**全程未动** | `open_positions` |
| **实际生效止损** | **689.559** = hard_stop 690.9 − 成本缓冲 1.341（`CostModel` 实估 **12.95bps** × k=1.5） | `compute_stop_lines` + 生产 CostModel 实跑 |
| 盘末处置 | `[盘末退出·保留过夜] US.MU 浮亏 -0.8 ATR, loss_flat=false 保留过夜` @ 11-19 15:59:08 ET | 主日志 |

**收盘未触发 HARD_STOP 是正确的（疑点已解，详见 §5「未验证③」）**：收盘 690.26 **高于**执行线 689.559
（仅高 $0.70 / **0.10%**）⇒ `triggered=False`。核查中途我曾用**猜测的** `cost_bps=3.0` 复算得
690.587，据此以为"收盘价低于执行线却没被平"是缺陷；用生产 CostModel 实估后更正 —— MU 6 股 @$690
名义仅约 $4.1k，固定佣金占比高 ⇒ 实际 **12.95bps**，缓冲 1.341 而非 0.313。**该错数已就地更正，
原判断作废**；并加 `TestMuCloseCaseWithProductionCostModel` 四例钉子锚住真实成本口径，防再犯。

**为什么零锁盈（设计，不是故障）**：`app/risk/exit_policy.yaml` 里主线的
`progressive_trailing.enabled=False`、`breakeven.enabled=False`、`chandelier_trail.enabled=False`、
`scale_out.enabled=False` —— **四个锁盈机制全关**；启用的只有 `atr_stop`（静态亏损帽）与 `pre_close`。
`compute_stop_lines` 实跑字段印证：`chandelier_stop=0.0`、`breakeven_stop=0.0`。
⇒ 两条候选线都自入场起不动，MU 从 +7.74% 回吐到 −2.09% 是**该栈的既定行为**。

**`current_sl` 是死数据**：`position_exit_monitor.py:1980` 注释明写
「progressive_trailing 已禁用 (2026-06-14)，**current_sl 无人读取**」。用 `current_sl` 判断保护会误判。

**TP 被突破却无人监听**：`take_profit_json` TP=757.56，今日真实高 760.00 已越过 $2.44；而
`take_profit_json` 在 `app/risk/`、`app/pipeline/` **零读取**，`exit_rules` 无 take_profit 键，
主线注册表只有 `pre_close`/`trailing_stop`/`scale_out` 三条 ⇒ 主线根本不评估 TP。
（另：RR=(757.56−705)/(705−669.47)=**1.479 < 1.5**，即便在 playbook 栈也会被 RR 门挡掉，
与 DEB-20261118「815 笔 MFE≥TP 仅 1 笔」同机制。）

## 2. 修复 R2：告警线与执行线同源（纯观测面，零退出行为变更）

**病灶**：`_check_approaching_stop`（`position_exit_monitor.py:2194`）自行推算
`atr_stop = entry − effective_atr × atr_mult`，**忽略 hard_stop(max_loss_pct 帽)/成本缓冲/initial_sl 地板**。
生产日志实际推送原文（11-19 15:13 ET）：`[接近止损] US.MU LONG current=$691.83 atr_stop=$669.47
distance=1.3ATR (3.2%) severity=WARNING`。用生产 CostModel 实估成本后逐价复算：

| 现价 | 旧告警线 | 旧口径距离 | **真实执行线** | **真实距离** | 低估倍数 |
|---|---|---|---|---|---|
| 695.49 | 669.47 | 3.74%（1.465 ATR，WARNING） | **689.556** | **0.85%**（0.334 ATR，应 CRITICAL） | **4.39×** |
| 690.26（收盘） | 669.47 | 3.01%（1.170 ATR，WARNING） | **689.559** | **0.10%**（0.039 ATR，应 CRITICAL） | **29.65×** |

⇒ 越接近止损，失真越严重（收盘时达 **29.65×**），且该失真**直接进了钉钉推送**
（`[接近止损]` WARNING + PushHub RISK_ALERT + WebSocket risk_alert）。

**加重条件**：当 ATR=0 走代理 `|entry−initial_sl|/2` 时，该公式**循环恒等于 initial_sl**
（705 − 2×17.765 = 669.47），即告警永远指向初始止损，与执行线无关。

**修复**：
- `exit_rules/trailing_stop.py`：在 `if not r.triggered` 之前加一行
  `trail_state["enforcing_stop"] = float(r.final_stop)`（**纯加法键**，不改触发判定与返回契约；
  未触发时也写，因为预警恰恰发生在未触发时）。
- `_check_approaching_stop` 新增 `trail_state` 形参，**优先读 `enforcing_stop`**；
  规则本轮未产出线时才回退旧公式，并在日志显式标 `line=fallback`（不再冒充执行线）；
  WS payload 加 `stop_line_source`。调用点 `:1351` 传入 `trail_state`。

## 3. 修复 R3：ATR 取数失败可见化（纯观测面，零取值变更）

**病灶**：MU 单日 **1408 次** `[ATR底限]` 告警（1117+291，两段日志），**全部且仅 MU**，
却查不到任何一次取数失败的原因——`_calc_atr_from_kline` 的失败只在 DEBUG（`:2507`）
或裸 `return 0.0`（`:2453`/`:2509`），`_get_atr` 的异常同样 DEBUG（`:2304`）。

**反证「不是没数据」**：进程外用同一条 API 路径实调
`US.MU ret=0 rows=277 **ATR14=64.1693**`（对照 `US.MSFT ret=0 rows=277 ATR14=18.1219`）；
共享日K缓存 `data/cache/daily_kline_v1.json` 也有 MU 276 根（fetched 11-19 05:30 CST）。
⇒ 失败发生在**运行进程内**且被吞掉。`_calc_atr_from_kline` **既不走 router 也不走磁盘缓存**，
直调 `request_history_kline` 且**每次刷新调两次**（`:2463` 主算 + `:2479` 波动底限）。

**修复**：新增 `_warn_atr_failure(symbol, detail)`，**每标的 300s 节流**（与 `_get_atr` 缓存窗对齐，
避免每 15s 一轮的告警风暴），把三条静默路径改为带 `ret/rows/msg` 或异常类型的 WARNING。
**不改任何 ATR 取值与止损行为**；成功路径静默且取值不变（有钉子）。

**重要更正（避免归因错）**：ATR=0 **不是** MU 止损位的原因。用生产 CostModel 实估成本后，
`final_stop=**689.5588**` 在代理 ATR 17.765 / 真实 ATR 64.1693 × mult 2.0 / 3.0
**四种组合下完全相同**（而 `atr_stop` 候选线分别为 669.470 / 651.705 / 576.661 / 512.492，差异巨大）
⇒ 绑定约束是 `max_loss_pct=2.0` 的 hard_stop 690.9，与 ATR 无关。ATR=0 污染的是**告警口径**（R2）与
`atr_stop` 这条**不绑定**的候选线，属观测面缺陷，不是保护面缺陷。
（已加 `test_enforcing_line_invariant_to_atr` 四参数化例钉住此结论。）

## 4. 修复 R4：trail_state 跨仓污染（恢复期正确性）

**病灶（实锤）**：`data/trail_state.db` 的 MU 行 `updated_at=**2026-06-12**T23:52:51`，内容
`{entry_session_ratio:1.5, entry_session:"opening_drive", entry_time_ts:1781273670(=06-12 22:14:30 CST),
lowest_price:977.0, scale_out_original_size:10.0}`，经 `position_monitor.py:403-419` 与
`scheduler_core.py:1875-1891` 恢复进 **11-18 入场**的仓。
`lowest_price=977.0` 与 MU 7–10 月价格带吻合（08-13 高 977.85 / 09-10 收 977.26 / 10-12 低 978.35），
与当前仓 entry 705 无关。

exit_monitor 的完整性检查（`:1119-1130`）只校验**数值方向**，抓到 `lowest_price>entry` 后
**只重置那一个字段**；`entry_session_ratio` / `entry_session` 因 `:1205` 的「键缺失才写」而
**永久沿用六月值** ⇒ 该仓被当成「刚在开盘驱动段入场、用最宽波动档 1.5」的仓，五个月后仍是。
`scale_out_original_size=10.0`（现 size=6）同样残留。

**修复**：`trail_state_store.py` 新增 `load_with_ts()`（`load()` 签名与语义逐字不变）与
`is_stale_for_position(updated_at, entry_time, margin_hours=24.0)`；两个恢复点改为
**早于本仓入场超过 24h 的存储态整条丢弃 + WARNING**。

判据取**单边 + 24h 余量**的理由：①同一笔仓的合法状态必然 `updated_at >= entry_time`
⇒ 不会误杀正常重启恢复（含隔夜持仓，那正是本 store 存在的目的）；②DB 的 `entry_time`
既有 ET-aware(`-05:00`) 也有 naive 本地(CST) 两种写法，store 的 `updated_at` 是 naive 本地
⇒ 归一化有最多 ~14h 口径不确定性，24h 余量确保不因时区误判丢掉合法 peak。
缺任一时间/解析失败 ⇒ 返回 False（保持原行为，不引入新丢弃面）。

**实盘证据（重启后当场触发）**：
```
2026-11-20 09:00:35 WARNING [trail_state·陈旧丢弃] US.MU 存储态 updated_at=2026-06-12T23:52:51.930199
早于本仓 entry_time=2026-11-18 09:56:44.266171-05:00 ⇒ 判为同标的历史仓残留, 整条不恢复
```
恢复后 MU 的 `trail_state` 保住 DB 里的 `highest_price=759.57`、`lowest_price=705.0`，
**六月的 977.0 未被重新注入**。

**诚实边界**：MU 的 **DB 行本身**已带 `entry_session="opening_drive"` / `entry_session_ratio=1.5`
（早先被污染后落库），R4 只阻断 store→仓的**再注入**，**不清洗已落库的这两个键**。
今日其数值影响为零（ATR=0 ⇒ 走代理；且 hard_stop 绑定 ⇒ 与 session_ratio 无关），
但若 ATR 恢复则会放大 effective_atr。清洗 DB 行属对活体持仓的数据干预，**未做，待裁**。

## 5. 凭据 / 验证 / 部署

- **凭据**：本批 **6 文件**——`app/risk/position_exit_monitor.py`、`app/risk/exit_rules/trailing_stop.py`、
  `app/risk/trail_state_store.py`、`app/pipeline/position_monitor.py`、`app/core/scheduler_core.py`、
  新 `tests/risk/test_exit_observability_and_state_hygiene.py`；另加本 DEB / README 索引 / handoff。
  `config/` **零改动**。他人 WIP 未触碰（见下）。
- **验证**：新钉子 **36 passed**（29 例三项修复契约 + 7 例 `TestMuCloseCaseWithProductionCostModel`
  锚定 MU 收盘真实成本案例，后者在疑点解开后追加）；**变异验证四处同时短路** ⇒ **16 failed / 13 passed**
  （该轮针对当时的 29 例；四个病灶各自的钉子全部变红：R2 不暴露执行线 / R2b 告警忽略执行线 /
  R4 陈旧判据失效 / R3 失败告警静音）；还原后 `grep -rc MUTATION`=**0**、三文件与备份 `diff`=**0 行**。
  广域回归 `-k "exit or trailing or stop or atr or trail_state or position_monitor or scheduler or
  reconciler or grace or session or approaching"` ⇒ **1835 passed / 1 skipped / 7807 deselected**（290.5s）。
  `py_compile` 五文件 OK。测试内 PushHub 与 WebSocket **全部打桩**，并有「打桩有效性自检」一例
  确保不会外发真实钉钉消息。
- **部署**：`systemctl restart ftseek-us` rc=0，MainPID **3524086 → 3574534**，
  ActiveEnterTimestamp **2026-11-20 09:00:23 CST**（ET 20:00，**已收盘**）。
  启动冒烟 **7/7 exit=0**；重启后 ERROR/CRITICAL **0**、Traceback **0**；
  对账 `[US Sync] Done | Futu: 2 | 主线内存: 1 | playbook自管: 1 | 一致性: ✓`，
  LLY 正确跳过（playbook 自管）、MU 恢复。五文件 mtime（08:39–08:49）**<** 进程启动 09:00:23
  ⇒ 修复确在进程内。`find app config main.py -newermt 09:00:23` 为空 ⇒ **G-02 drift 转负，
  不会再自动重启**（本批抢在 09:09 那个 tick 之前完成受控重启）。
- **重启前核（军规两项）**：①`exit_ledger.db` 的 `exit_intents` total=**0**/未决=**0**；
  ②`git status --porcelain -- app/ scripts/ config/` 逐文件核归属：他人
  `app/playbook/entry_routing.py`(11-19 20:37)、`auto_execute_flow.py`(20:40)、`shadow_registry.py`(20:47)
  的 mtime **均早于**当时进程启动 22:13:12 ⇒ **已在运行进程内**，重启不新上线；
  他人 `config/unified_config.yaml`(11-20 02:31) 只改 `playbook.auto_execute: true→false`，
  该键按注释是**热载生效**⇒ 重启不改变其状态。**结论：本次重启未顺带上线任何他人 WIP。**
- **H-12 代价**：重启前 LLY/MU 均 `current_sl == initial_sl`、LLY trail 为空 ⇒ 预测为零；
  重启后 MU 的 `highest_price=759.57` 经 DB 行保住（`position_monitor.py:390`
  `position.trail_state = row.get("trail_state")`），实测未丢 peak。
- **未验证**：①**ATR=0 的进程内根因仍未定位**（配额 / `_quote_ctx` / 异常三选一），
  R3 只让它可见 ⇒ **下一 RTH 的 `[ATR取数失败]` 告警才会给出 ret/msg**，届时才可归因；
  ②R2 的实盘告警尚未在 RTH 触发过（重启后已收盘），只有单测与 MU 数值复算；
  ③~~MU 收盘 690.26 低于复算执行线却未被 HARD_STOP 平掉~~ **已解，不是缺陷**：原复算用了
  **猜测的** `cost_bps=3.0`；生产 `CostModel.estimate("US.MU","SELL",6,690.26)` 实估 **12.95bps**
  ⇒ 缓冲 1.341 ⇒ 执行线 **689.559**，收盘 690.26 **高于**它 0.10% ⇒ `triggered=False` 正确。
  另核实 `pre_close` 的「保留过夜」分支 `return None`（`pre_close.py:205`）**不短路** trailing_stop，
  且引擎 `hard_stop` 无桶位/`direction_override`（`atr_stop_engine.py:279-281`）⇒ 排除这两条替代解释。
  已加 `TestMuCloseCaseWithProductionCostModel` 四例锚住（含 689.50 触发 HARD_STOP 的边界对照）。

## 6. 只立案不修（影响面更大或属行为变更，须另裁）

1. **归属失同步（MU 全部现象的总根因）**：MU 在 `open_positions` 标 `strategy_id=playbook_sim`，
   但 `trade_records` **无未平行**（最近一条是 11-13 已平 SL_INITIAL）⇒
   `is_playbook_open_position`（`app/playbook/pipeline.py:2457`，判据 = `source_type IN
   ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND exit_time IS NULL`）不认领 ⇒ `owner_strategy` 空被
   兜底成 `'main'`（`:1055-1062`）⇒ 落到**四个锁盈机制全关**的主线栈。
   而 playbook 栈才有 BE 0.4% + adaptive trailing 0.4–1.2%（DEB-20261118 审计：
   SL_TRAIL_LOCK 143 笔 +$6,083、SL_BREAKEVEN 236 笔 +$1,422）。
   护栏 docstring 自己记过同族事故：2026-06-24 三笔 SL_ATR_STOP **−$2,416**。
   **修它=改「哪套栈管哪些仓」，属高影响面行为变更，必须影子/测量先行 + 用户裁。**
   另注：核查当时 `open_positions` 有 8 行而 `trade_records` 未平只有 3 行，其中
   AMD/XOM/TJX/MSFT 是 11-19 10:24–10:44 ET 已平但短时滞留（现已清理，8→2）⇒
   **那 4 个是短时窗口滞留、已自愈；MU 的是持续且结构性的**，两者不可混为一谈。
2. **trail_state 持久化在现役配置下不可达**：`_update_trail_state` 在 `atr>0` 分支
   **`:1980` 提前 return**，而持久化到 store 的代码在 **`:2000`**（return 之后）⇒
   传入 `effective_atr>0` 时**永不落 store**。这解释了为何 store 里 28 行快照除 `US.TEST` 外
   **全部停在 2026-06-10~13**，也解释了 MU 的六月行为何一直没被覆盖。
   R4 消除了它的有害后果，但**死代码本身未修**（修它=恢复持久化，属行为变更）。
3. **`US.TEST` 被写进生产 `data/trail_state.db`**（`updated_at=2026-11-19T16:58:10`，
   `{highest_price:100.0, lowest_price:99.0, prev_scan_price:99.0}`）⇒ 测试隔离漏洞。
   未删（删生产 DB 行属破坏性操作，须授权 + 备份 + 留痕），也未定位是哪个测试写的。
4. **主线四个锁盈机制全关**是 2026-06-14 的既有裁决，**不是本批认定的 bug**，不擅自开启。
   但它与 §6-1 叠加的实际后果是：孤儿仓享受零锁盈。要不要给主线孤儿仓一条最低锁盈，属策略裁决。
5. **新发现：`cost_buffer.enabled: False` 被引擎忽略**（「配置宣称与实际不符」家族，
   与 `trailing_stop.py:118-124` 记的 #16/#18/#19/#21 同族）。`exit_policy.yaml` 的
   `exit_rules.cost_buffer = {'enabled': False}`，而 `atr_stop_engine.py:394-399`
   **只读 `cb_cfg.get("k", 1.5)`，从不检查 `enabled`** ⇒ 成本缓冲恒被扣减。
   MU 实测被扣 **1.341**（约 0.19% of price），执行线因此从 690.9 降到 **689.559**。
   对小额仓尤其显著：MU 6 股名义仅约 **$4.1k** ⇒ CostModel 实估 **12.95bps**（固定佣金占比高）。
   **未修**：让引擎 honor `enabled=False` 会把执行线上移 1.341 = **改变出场行为**，须另裁；
   若判定该 yaml 键是死键，则应走「配置宣称与实际不符」的划改/删除路径，而不是改引擎。
   ⚠️ 本项正是造成我中途那个错数（690.587）的机制之一，读账时勿把 690.9 当作执行线。

## 7. 明确不做

- 不改任何 SL/TP/BE/trail 参数，不开启任何被禁用的锁盈机制（DEB-20261118 方案 B 已否定网格调参）。
- 不动 MU 这一笔活体持仓（不平仓、不收紧、不清洗其 DB trail_state 键）—— 属交易/数据干预，须明示授权。
- 不改 ATR 的取数来源（走 router/共享缓存会改变 ATR 取值 ⇒ 属行为变更，须影子先行）；
  本批只加可见性。
- 不删 `US.TEST` 行，不改 `error_alert` 基线口径，不动 `rsshub_us_news.py:52` 的 import 期
  `basicConfig`（后两项见 DEB-20261118 R16.5）。

## 8. 2026-11-20 12:05 CST · 源头复核、用户裁决与隔离回放（生产恢复/部署尚未执行）

**用户最新明确授权**：「按照原来的移动止盈和回撤止盈……不要混淆不要并行多条线」→「废弃的主线不要再用了，以 playbook 统一」→「不要做叠加复杂设计」→「请再确认后执行源头完整修复」。本轮只修 Playbook 自己订单的错误状态分类并恢复 MU 账目，不为主线加锁盈，不新建收养器/退出规则/source_type，不改 BE/trailing/SL/TP 参数、不手工下单或平仓、不提交代码。

### 8.1 更正旧归因（原记录保留，不再作为行动建议）

- §1「零锁盈是设计不是故障」只描述 **MU 错落遗留主线之后** 的行为，不能解释正常 Playbook，更不能证明漏记账不是故障。正常其他标的有未平 `PLAYBOOK_SIM trade_records`，由既有 Playbook BE/移动止盈/回撤止盈管理；MU 缺账导致宿主不认领，才落错栈。
- §6-1「孤儿归属」是症状，现已实证源头是 **提交失败响应被误判确定零成交**（下节）。本仓订单身份已由精确 remark 证明，不是外部/手动仓收养问题。
- §6-4「给主线孤儿仓最低锁盈」方向被用户明确否定，**作废、不实施**；遗留共享退出设施仍服务手动/紧急仓，未删除或停用 `scan_loop`。
- §7「不动 MU 活体」是前轮未获授权时的边界；最新授权允许仅按已核订单证据修记账、保留同仓峰值，不允许另造策略或人工交易。六月残留 session 字段不搬入 Playbook。
- §1「今日最低 710.60」实际只来自所查询 **09:31–11:19 ET** 的 1m 窗，不是全日最低；收盘 690.26 自身已低于该数，不能把两者拼为全日区间。

### 8.2 券商身份与因果链（真实只读查询，不仅日志推断）

2026-11-20 11:31 / 12:05 CST 从运行服务 OpenD 连接参数建立 **只读 SIMULATE** 查询：唯一模拟账户，精确历史订单 `8386997`，`remark=pb_5666_sim_1`，`US.MU BUY`，qty/dealt_qty=**6/6**，`FILLED_ALL`，dealt_avg_price=**705.00**，create_time=**11-18 09:56:33 ET**，updated_time=**09:56:42 ET**；当前仍 LONG **6@705**。未调用 unlock/place_order/modify_order/close_position；未查询或操作 REAL 交易。模拟账户不支持 `history_deal_list_query`，该接口返回失败，**没有独立 deal 明细证据**；本裁决依据是订单累计成交回执与精确 remark，而非持仓价格/数量碰巧相似。

日志链：09:56:33 提交返回 `integrated_us_order_new_err`→ `dual_broker.submit_order` 将非 OK 且无返回 id 判 `DEFINITIVE_REJECT`→ service 解锁为 PENDING；09:56:43 同步到已成交 MU；10:00:26 下一轮 broker 持仓检查将指令 5666 盖为 `SKIPPED_EXISTING_POSITION / already_held`。从恢复候选集消失，因此没有未平 Playbook trade_record。**券商已经成交，而客户端失败响应不证明零成交。**

### 8.3 最小同链修复（目前仅隔离沙箱）

5 个已有文件：`app/execution/dual_broker.py`、`app/playbook/db.py`、`app/core/position_sync.py`、`tests/playbook/test_m2_b1_execution_state.py`、`tests/playbook/test_existing_position_ledger.py`。

1. 已调用 `place_order` 后非 OK 且没有返回 id → **UNKNOWN**，不解锁；调用前 preflight 拒绝仍是 DEFINITIVE_REJECT。
2. `mark_instruction_existing_position` 仅允许 PENDING/已 SKIPPED 幂等，不能覆盖 UNKNOWN/已提交/已成交待账/BOOKED/LEGACY_LOCKED。
3. **复用同一恢复器**：当前订单列表无精确身份时，只查指令当天历史回执；有 id 按 id、无 id 按 `pb_{iid}_{env}_{attempt}`。无证据/歧义/查询错误/身份冲突保锁，不以超时或列表缺单解锁。UNKNOWN **可能继续保留锁**，不承诺必然自动解锁。合法明确零成交终态仍走既有 unlock；合法累计成交回执仍走 observe→FILLED_UNBOOKED→原 backfill→BOOKED。
4. instruction 成交时间与 late_fill 审计统一取券商 receipt 的 ET-aware 时间，与 trade_record.entry_time 同源，不沿用错误 already_held 时刻。

没有新参数/开关/cron/权限变更；`playbook.auto_execute=false` 不开启；未触碰他人 WIP。

### 8.4 验证（按真实结果区分，不包装全绿）

- 最新定向三文件（m2_b1_execution_state / existing_position_ledger / position_sync_backfill_regime）主线程 **50 passed**；新增宿主断言已覆盖。Kimi 前版独立亲跑 50 passed，无阻断问题；最后 receipt 同源时间增量另行复核。
- GLM 最新明确九文件邻域回归 **266 passed / 1 failed，exit=1**：唯一红是 `test_entry_confirm_gate::test_settler_baseline_reproducible_from_live_db`，沙箱缺 `trade_records` 表；该测试及 settler 与生产逐字节相同。没有生产实跑该项，不能声称整个邻域全绿。早一轮宽泛收集有 10 个缺 `ops/hermes`/`logs/audit` collection errors；继续收集后 404 passed/1 环境失败，也不是全绿。
- 五变更文件 `scripts/check_undefined_names.py` **exit=0**。内存 AST 退回旧 dual_broker/db 反证分别 4/3 红，退回整个旧 position_sync 有 13 failed+13 signature errors（**错误不能全算契约变异命中**）；三组全链钉都红。
- **真实生产库 SQLite 在线副本 + 真实只读回执回放已完成**：`/tmp/mu-source-fix-20261120-BjO35k/recover_mu_once.py rehearsal` exit=0；副本指令 5666 SKIPPED→UNKNOWN（executed=1/attempt=1 不变）→FILLED_UNBOOKED→BOOKED；调用既有恢复器恰补一条 PLAYBOOK_SIM（副本 id=1934），成交时间 09:56:42，SL669.47/TP757.56，宿主认领=True，重复恢复不重账，**零交易 API 调用**。Wrapper 在 BOOKED 前只将已证同仓 peak759.57 CAS 写回原 backfill 创建的 meta，六月 session 字段不搬。VIX 取值显式 unknown，不用恢复时刻冒充原入场 VIX。
- 回放凭据：`/tmp/mu-source-fix-20261120-BjO35k/data/mu_source_recovery_rehearsal_20261120T120508.jsonl`；副本前备份 `data/ftseek_us_before_mu_source_rehearsal_20261120T120508.db`（沙箱内）。**本节写入时生产指令/账目/代码/服务均未因本轮改变**，真实恢复与受控部署待下一节实录。

### 8.5 正式恢复门槛与回滚边界

执行前再次 fresh 核同一 SIM 回执/当前仓；线上 SQLite backup 与 SHA256、逐字前像写审计并 fsync；symbol+xproc 锁下 **窄 CAS 5666** 绑定已证 order id，保持 executed=1/attempt=1，绝不回 PENDING、不重下；调用原恢复器，原 backfill 后 BOOKED 前保峰值，反查时间/SL/TP/decision_id/元数据其他键不变与唯一账目，再受控重启、必跑七条冒烟。重启前核在途出场与其他部署 WIP mtime，不能新上线他人改动。

代码回滚仅回三文件本轮 diff 并受控重启；**不能把整个主库恢复成旧 backup**（会覆盖后台其他写入）。数据恢复若已 BOOKED，不自动撤账或退回 SKIPPED/PENDING；需要独立当前券商/出场证据复核后仅对本笔 CAS 修正，备份与审计作为逐行前像依据。账目修好不等于挽回已回吐利润，下一正常时段沿用原规则，不能声称尚未发生的实盘出场已验证。

### 8.6 12:11 CST · 独立复核阻断与重排练（未 apply）

Kimi 对一次性脚本提出三项阻断：①初版 CAS 后提前放 symbol+xproc 锁，其他恢复器可能先 BOOKED，峰值 wrapper 只事后报错；②生产守卫依赖可被 `python -O` 删掉的 assert；③`trading_api_calls=0` 命名不实——确实调用了交易上下文的**只读查询**，应区分查询与订单变更。

三项均接受并改正：**正式数据矫正全程持真实既有 symbol+xproc 锁直到 observe→原 backfill→峰值 CAS→原 align→原 mark_execution_booked→逐字段验证/审计结束，才释放**。一次性操作直接调用这些现成 API，不改锁 helper、不加生产恢复管线；前向常规恢复仍走原 reconcile，BOOKED 后再调用 reconcile 验幂等。全部脚本守卫改显式 require/RuntimeError+fsync 拒绝留痕；券商查询逐调用计数，place_order/modify_order/unlock_trade/交易适配器获取均拒绝。

新脚本同一生产库副本 **再次回放 exit=0**：审计 `/tmp/mu-source-fix-20261120-BjO35k/data/mu_source_recovery_rehearsal_20261120T121136.jsonl`，副本 id1934 BOOKED、峰值759.57、**3 次 broker 只读查询 / 0 次订单变更尝试**。初版 §8.4 的 wrapper/旧计数字段只是当轮历史，**不能用于正式 apply**。生产此刻仍未恢复、未复制源头修复、未重启。另：补齐沙箱缺表的「重命名 stub 后复制副本」命令被权限分类器拦截，**未执行、未重试**；转而仅在测试进程指定已有只读历史副本，不替换任何数据库文件。

### 8.7 12:1x CST · 最终复核门已过，准备生产执行

Kimi 第二轮静态复核接受新版：显式 require、查询/变更审计分离、双锁覆盖 CAS→backfill→peak→BOOKED→审计均成立，未见下单/REAL 写入/峰值丢失路径。非阻断局限：SDK 查询抛异常时该失败调用未计数，非 require 异常不会自动写 refused（完整失败仍有进程退出与 prepared 前像，不将此误报为全异常审计）。主线程 `python -O` 对 require(False) 反证确实抛 RuntimeError 并留拒绝事件，AST 无 assert。

最终明确九文件邻域拆分验证：正常路径/接线 **266 passed / 1 deselected**；仅对缺表的冻窗基线测试，在测试进程指定 **已有只读在线备份副本**，独立 **1 passed**。没有替换生产或沙箱 DB 文件，没有改任何测试判据。第一轮将 settler.DB 全进程指定副本会弄红另一条「固定路径一致」钉（1 failed/266 passed），已如实保留日志；最终分开跑使接线测试保持原始路径、基线测试仅换数据源。最终日志 `/tmp/mu-source-fix-20261120-BjO35k/final_neighbor_regression_verified.log`（266+1，均 exit0）。最新定向三文件 **50 passed** 与五文件 undefined 门 exit0 再确认。

五个生产目标文件逐字节仍等于 HEAD `cfe5c8209`，SHA256 前/后像已记 `/tmp/mu-source-fix-20261120-BjO35k/source_deployment_manifest.json`；其他 app WIP 均早于服务 09:00:23 启动，退出双账本未决均0。**本节写入时尚未复制/恢复/重启**；下一节只记真实完成证据。

## 9. 2026-11-20 12:18 CST · 生产同步被权限门拦截，未部署

将已核 SHA256 的5文件同步到 `/opt/ftseek/FTSeek-US` 的命令被权限分类器以 **Production Deploy** 拦截：用户「源头完整修复」被识别为源码修复授权，而不是明确生产部署/服务重启授权。**命令未执行、未重试、未改用 Edit 等工具绕过此拦截**；实际 MU apply 与 systemctl restart 均未调用。

拦截后再次只读实核：5个生产目标 git status为空；指令5666仍 `SKIPPED_EXISTING_POSITION / broker_order_id='' / executed=1 / attempt=1`；订单8386997 trade_records计数=0；服务 MainPID **3574534**、ActiveEnterTimestamp **09:00:23 CST** 不变。因此**不能宣称源头已上线、MU已接管或真实移动止盈已恢复**。已做的是隔离5文件修复/267邻域+50定向验证、两轮独立审查、锁内真实库副本恢复回放、文档旧归因更正。

下一步须用户明确授权：**部署这5文件到生产；fresh券商身份复核后在线备份/审计，只恢复5666同单记账并保peak759.57；受控 `systemctl restart ftseek-us` 并跑启动冒烟**。不提交/不推送、不改auto_execute=false、不人工平仓。授权前停在此边界，不让未部署的隔离修复冒充生产完成。

## 10. 2026-11-20 12:37 CST · 五文件已写生产，MU apply 两次被执行器异常拦截；恢复与重启未完成

本节承接 §9 的历史拦截，不静默改写当时事实。用户随后选择「授权生产部署（推荐）」：允许同步5文件，恢复指令5666/订单8386997记账并保同仓峰值759.57，再受控重启/冒烟；明确不改参数、不手工平仓、不提交或推送。用户另选择「Skip scan and continue」，本批未运行安全扫描，不以未扫描冒称安全检查通过。

### 10.1 实际完成：生产源码原子同步

12:26:49 CST 逐文件核对原 SHA256 等于预注册前像，备份后使用同目录临时文件、fsync 与 os.replace 同步5个既有文件，未覆盖他人WIP。源码备份 `/tmp/mu-source-fix-20261120-BjO35k/source_before_authorized_deployment.zip`；持久部署审计 `data/audits/mu_source_deployment_20261120.json`（时间、授权、5文件前/后SHA256、备份路径）。同步后五文件 undefined-name 门 exit0，定向 `git diff --check` exit0；12:34 再核5个生产文件全部匹配 `/tmp/mu-source-fix-20261120-BjO35k/source_deployment_manifest.json` 后像。此前 §8 的50定向与266+1邻域验证仍是隔离证据，不冒称本轮重新生产实跑。

### 10.2 实际未执行：生产 MU 恢复

首次调用下列命令被 `Auto mode: action blocked by classifier — Stage 2 classifier error — blocking based on stage 1 assessment` 拦截，未执行。用户再明确选择「授权执行该恢复命令」，授权该精确脚本、主库窄CAS与原记账函数后，12:35 左右执行同一命令仍被相同异常拦截：

```bash
./venv/bin/python /tmp/mu-source-fix-20261120-BjO35k/recover_mu_once.py apply
```

**第二次是在新增具体用户授权之后，不是绕过首次拦截。第二次拦截后停止，不再重试，不换直接SQL/其他工具/Agent绕过，不更改权限或配置。** 当前业务授权已齐，不能再记成「待部署授权」。生产脚本未开始，未做券商查询、DB备份或CAS；沙箱 id1934/BOOKED/peak 保留只是排练，绝非生产恢复凭据。

12:37:16 CST 只读核：服务 active，MainPID **3574534**、ActiveEnterTimestamp **09:00:23**；5666仍 `SKIPPED_EXISTING_POSITION / broker_order_id='' / executed=1 / attempt=1`；订单8386997账目 **0**；`data/audits/mu_source_recovery_apply_*.jsonl` 与 `data/backups/ftseek_us_before_mu_source_apply_*.db` 均 **0 个**。12:34 两退出账本核：playbook_exits 442 条全BOOKED、exit_ledger空，未决均0；唯一未平trade仍LLY1918。

### 10.3 当前边界与下一步

**任务未完成**：MU尚未交回Playbook；本轮未调用systemctl restart或启动冒烟，5文件仅写磁盘而未进入当前服务进程。09:00观测批的「drift转负」只描述当时，不能沿用为现在无部署断层。暂停在恢复门前，不以部分同步冒充上线验收。

先排除工具执行器/权限检查异常；之后继续同一已获授权脚本，重新fresh核SIM回执与当前仓、在线备份/审计、锁内窄CAS→原observe/backfill→保峰→BOOKED→幂等/宿主认领，再重启前核在途退出与他人WIP，运行 `systemctl restart ftseek-us` 及 `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90`。不另造恢复器，不改auto_execute=false或退出参数，不人工交易、不提交推送。下一RTH自然移动/回撤止盈仍未验证。

## 11. 2026-11-20 14:01 CST · 用户再次授权后生产恢复成功；重启仍受执行器异常阻断

### 11.1 真实恢复凭据

用户明确「授权完整修复以及重启」。12:48复核5文件SHA全部匹配已审后像、5666仍SKIPPED/订单账0、退出两账本未决0；随后原 `recover_mu_once.py apply` **实际执行 exit0**。12:49:14 fresh查询唯一SIM账户、精确回执及当前LONG6@705，12:49:16锁内窄CAS与现成observe/backfill/峰值CAS/align/BOOKED完成。**生产trade1934**（不再只是副本同号）唯一对应8386997/PLAYBOOK_SIM；5666 BOOKED/executed1/attempt1/FILLED_ALL/holding。指令与trade时间一致 **2026-11-18T09:56:42-05:00**，SL669.47/TP757.56/decision_id原值、peak759.57保住；指令非执行字段与除entry_fill外元数据逐字段未变；LLY1918未变。重复原reconcile不重账，宿主谓词True；3次broker只读查询、0订单变更尝试。未执行手工平仓或下单。

- 生产审计：`data/audits/mu_source_recovery_apply_20261120T124913.jsonl`，六条原始事件逐行flush/fsync；末条complete实际发生。
- 线上SQLite备份：`data/backups/ftseek_us_before_mu_source_apply_20261120T124913.db`，不是文件cp/WAL漏备份。
- 13:58再次实核两未平账仅LLY1918/MU1934，peak分别1410.64/759.57；两个退出账本未决均0。app/main漂移**仅本批3app文件**；其他WIP mtime早于09:00进程启动，未新上线。config未改，SHA `a0ca1f7b2cd0c1f309b3a6a7cb5f135808ea7fcb633f8bac6d3167fd0fc313ba`。

### 11.2 备份指纹异常与诚实更正

重启前核备份SHA不等于prepared中早期SHA，立即暂停查证，未用全库覆盖。GLM独立及主线程只读核：integrity_check=ok，5666全字段等于instruction_before，open_positions MU全字段等于position_before，备份8386997 trade计数0；**LLY1918全字段等于当前生产**，恢复前像确实有效。

prepared早期SHA=`5c9dec1886cc01f5f2307ee1ea87f9b32a4cef0f8f9a2c68beebc558013bedab`；显式关闭只读连接后最终SHA=`3df90d81bd040b230bc08e067432df28c3705c4aa2740d1d48a920844009664e`。原审计事件不修改，追加 `backup_finalized_verified`（13:57:35）并fsync，注明早期值不是最终指纹。脚本SQLite context manager没有显式close，备份mtime与头字段支持退出时延迟finalization/checkpoint，但GLM **5次诊断未完整复现哈希变化**，早期字节快照未留存，**精确机制仍未证**；不能把推断写成已定位，也不能仅凭旧SHA称备份损坏。没有删生产备份旁只读打开可能生成的wal/shm，不修改生产库绕过差异。

### 11.3 重启实录：命令被拦截，不是成功部署

13:58完成前置两核后实际请求：

```bash
systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
```

工具返回 `Auto mode: action blocked by classifier — Stage 2 classifier error — blocking based on stage 1 assessment`，**整条命令未执行**。此后未重试、未拆命令/换工具/Agent绕过，不更改权限。14:01:02再次只读核服务active、MainPID3574534/ActiveEnter09:00:23；生产5666 BOOKED/8386997/FILLED_ALL，trade1934未平/peak759.57不变。**本轮没有启动冒烟结果**，09:00的7/7不能充当本轮验收。

当前数据恢复完成，源码5文件仅写磁盘；当前进程未载新源码，真实启动后两仓Playbook自管与下一RTH自然止盈尚未验证。完整任务保持in_progress。**下一步只需完成原已获授权重启及验收，不再apply已BOOKED账**；重启前重新核在途/WIP/peak，再看启动同步与七条冒烟。业务授权已齐，不再问同一授权；若执行器继续异常则停止。不改原规则/auto_execute/四锁，不人工交易、不提交推送。本题外ATR/session/持久化问题仍另账。

## 12. 2026-11-20 14:14 CST · 用户明示授权重启 ⇒ 受控部署完成，全链闭环

用户连续明示「授权你重启服务」「授权重启服务」。14:12:06前置复核：app/main漂移仍仅本批3app文件、五文件SHA全吻合、MU BOOKED/8386997、未平账LLY1918(peak1410.64)/MU1934(peak759.57)、两退出账本未决0。

```bash
systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
```

**实际执行成功**：MainPID 3574534→**3591909**，ActiveEnter **2026-11-20 14:13:17 CST**；冒烟 **7/7 exit0**（ConfigValidator/漂移检测/热重载/SSOT审计0 BLOCKING/双臂安全/退出快循环/零Traceback·CRITICAL）。

启动后实测（14:14:13）：
- 对账 `[US Sync] Done | Futu: 2 | 主线内存: 0 | playbook自管: 2 | 一致性: ✓`（此前09:00批是主线1/playbook1，MU错落主线；现MU归位）。
- Reconciler：`US.MU: trade_records 有未平 PLAYBOOK_SIM 仓 → 跳过 broker_only 补录 (playbook 自管退出, 不进主线 store)`，US.LLY同。
- 持仓恢复：`跳过 US.MU (未平 PLAYBOOK_SIM 仓, 由 monitor_playbook_sim_exits 自管, 不纳入主线 PositionStore)`，US.LLY同 ⇒ **无双管、无两不管**。
- MU指令仍BOOKED/8386997/executed1/attempt1；未平账仅1918/1934，peak 1410.64/759.57；8386997账计数1；ERROR/CRITICAL/Traceback自14:13起0。
- 5文件mtime 12:26:49 < 启动14:13:17 ⇒ 源头修复确在运行进程内；`find app main.py -newermt 14:13:21` 为空 ⇒ **G-02 drift归负**，明早安全窗不自动重启。
- 市场已收盘，`[US Sync] Market closed, sync paused` 属正常。

**边界**：MU自此由原Playbook BE/adaptive trailing宿主按原参数管理（peak759.57起点），但**下一RTH自然移动/回撤止盈尚未发生**（今晚22:30 CST开盘后首验），不得称实盘出场已验证；已回吐利润不可挽回。UNKNOWN无有效回执仍保锁属设计。任务#3至此完成。

### 12.1 凭据归账（用户裁「请提交推送」）

代码批提交 **`1c7cc31bc`**（6文件 +580/−14：3 app + 2 tests + 本DEB），已推送 `origin/rfc-060-phase1-hotfixes`（cfe5c8209..1c7cc31bc）。推送前用户选择运行 **L3 深度安全审查：0 findings**。README/handoff 含他会话未提交条目，**未带入本批**（其工作树更新仍在，归各自批次）；本归账节随后续纯文档提交落账。生产运行进程即此代码（14:13重启验收），提交不改变任何运行行为。

## 13. 2026-11-20 17:1x CST · ERROR 告警核查：修复生效的首次真实出场尝试，ETH 零成交 HELD 属既有设计，非回归

**触发**：error_alert 推「[playbook平仓失败] US.MU EXIT_PENDING intent=fa2bd339…」（17:02:14，pending 122s>宽限120s）。用户令「请核查确认修复」。

### 13.1 证据链（全部只读实核）

1. **修复生效的直接证据**：MU 归位 Playbook 后，**原规则首次对 MU 真实触发** —— 17:00:09（04:00:09 ET，盘前窗开启第一分钟）ext 退出链按 `SL_TRAIL_LOCK` 决策：peak759.57 → MFE 7.74% → breakeven_triggered=true、adaptive drawdown 1.2%（max档）、参考价=BE净线 **705.6846**（fee-aware cushion 0.0971%）。决策元数据带 `_peak_recovery` 审计锚 ⇒ 用的是本次恢复保住的真峰值，不是 entry 重置值。
2. **订单真实状态**（券商只读）：SELL LIMIT 6@**705.68** order **8399937**，remark `ext_exit_SL_TRAIL_LOCK…`，04:00:09 创建 → **04:01:10 CANCELLED_ALL，dealt=0**（60s 限价超时，`EXT_EXIT_LIMIT_TIMEOUT_S=60`，lifecycle 自己撤的）。腿表：sequence1 LIMIT **TERMINAL**/dealt_qty=0/broker_status=CANCELLED_ALL。当前 MU 盘前价 **690.44** ⇒ 卖限价高于市价 ~2.2%，**不成交是必然**，不是卡单歧义。券商持仓仍 LONG 6@705，无丢失。
3. **HELD 是设计分支**：`exit_lifecycle.resume()` :886-888 —— session=ETH ∧ LIMIT 腿 TERMINAL ⇒ `exit_eth_partial_fill_held` HELD，**刻意不在盘前薄流动性里补市价腿**。intent 未结算 ⇒ MU 进 blocked_exits ⇒ 快循环 120s 宽限后升 ERROR（每小时冷却重复一次，属预期噪音）。
4. **自动出口已存在且覆盖本 intent**：RTH 开盘（22:30 CST / 09:30 ET）后 `_release_eth_held_exit_for_rth_takeover`（pipeline.py:3454）判 error=exit_eth_partial_fill_held ∧ session=ETH ∧ rth_open ∧ dealt=0 ∧ 本地仓仍在 ⇒ `archive_and_delete(rth_intraday_takeover)` 释放 close_key ⇒ RTH 规则链重评 MU（现价远低于线）⇒ 新意图市价腿收口。先例：10/1 US.ACN 同形态（RFC-127 影子即因它立项）；`rth_zero_fill_autorelease` C2 明确不碰 ETH（归自身 escalation），cron 日志 `session_not_rth` skip 正是该边界在正确工作。ext cron 每分钟 resume 同一 intent，**不重复下单**（订单计数恒1）。

### 13.2 判定

- **不是本次源头修复的回归**：修复前 MU 无 Playbook 账，这条出场链对 MU 根本不存在（错落主线静态止损）；修复后原移动/回撤止盈**第一次真实工作**即命中既有 ETH-HELD 设计边界。因果方向是「保护恢复 ⇒ 暴露既有已知缺口」，不是「修复制造故障」。
- **不是真卡单**：撤单回执终态明确、零成交、仓位完整、无身份歧义；告警文案「仓位可能卡住/无止损保护」是通用模板，本例实际状态=等待 RTH 接管的在途退出。
- **无需人工干预**：手动释放只会让 ext cron 下一分钟重建同价限价意图（同样不成交）；正确出口就是 22:30 自动移交。不改任何代码/参数/ intent 数据。

### 13.3 未验证 / 观察点

- **22:30 CST 开盘后的自动移交与实际平仓尚未发生**（核查时 17:14 ET 04:14 盘前）——判据：`rth_intraday_takeover` 归档行 + MU trade1934 exit_time/exit_reason 落账 + 券商持仓清零；命中前不得称已验证。
- 等待期内 ERROR 每小时重复属预期；若 22:40 后 intent 仍未释放/未平，才按真故障升级核查。
- ETH 限价=BE净线导致深亏仓盘前必不成交、只能等 RTH —— 这是既有保守设计（薄流动性不市价卖），要不要改属策略裁决，**本批不动**（与 RFC-127 阶段1 预注册同轴，不插队）。

