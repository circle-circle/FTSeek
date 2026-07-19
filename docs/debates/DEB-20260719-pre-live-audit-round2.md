# Agent-vs-Agent Debate: 出场链路 / 资金口径 / 重启恢复 / 并发 / 静默异常 — 第二轮上线前审计

---

# 议题：第二轮盲区扫描发现 ~20 个新问题，请博弈裁定「上线前必修 vs 上线后偿还 vs 不修」，并给出排序后的一致修复方案。核心约束不变：Playbook 唯一管线、无双重管理、不新增分叉、不过度工程、可回滚可配置。

## 状态
- [x] 第一轮：hotfix agent 起草盲区扫描（F/R/E/C/D/S 六类 ~20 项）
- [x] 第二轮：复核 Agent-α 源码逐项核实 + 全链路交叉验证 + 裁定（见下方报告）
- [x] 第三轮：辩论 Agent-β 实证博弈（α 遗留 5 焦点全部裁决 + §7 五项实证待办全部闭环，见下方 β 报告）
- [x] 第四轮：终裁 Agent-Ω 独立复验 + 三方收敛（确认 β 修正成立，P0 收敛为 F1/R1b 2 项，见 Ω 报告）
- [ ] 执行：按 Ω.4 清单逐项落地（每项完成后对照 Ω.5 实证门验证）

## 阅读须知（给复核 agent）

- 本轮是**第一轮审计(DEB-20260719-pre-live-audit.md)之后的盲区扫描**：出场/止损链路、资金口径 SSOT、重启恢复、SQLite 并发、静默异常吞噬——这些第一轮未系统覆盖。
- **以下已在前两轮修复，请勿重复报告或反复讨论**：入场幂等抢占(try_claim_execution)、部分成交入场记账(_effective_qty)、PDT时区(now_et)、报价健康门(check_quote_health)、Finnhub float timestamp(C3)、ACB/PDT风控接线、IC符号bug、profit_override、dual_broker DST、acb_fail_mode配置、client_order_id透传remark(B4)、position_sync量级校验(B3)、edge_weekly量纲(C5)、di_container死投票脑清理(C2)。
- 铁律：**Playbook 唯一下单/管理管线**；主线 order_executor/position_monitor 仅服务遗留/手动/紧急，不可删除但不应为其新增功能。
- 当前处于 SIM 数据积累期，实证：**83笔已平仓 / 胜率43.4% / 净PnL -$976.93**（edge 未验证）。
- 所有 file:line 已由起草 agent 抽样核实（下方标注 [已核实] 的为亲验项）。

---

## 类别一：资金正确性（最高优先——直接关系真金白银）

### F1. 兜底仓位计算缺水位/regime 乘数 → 回撤期仓位放大 2-4x [已核实]
- **位置**：`app/playbook/pipeline.py:2545-2578`
- **现象**：bridge/DA 直写指令 `position_size<=0` 时，auto_execute 兜底计算 `_risk_amount = _risk_base * (_risk_pct/100) * _conf_factor`，**无 `_water_mult` 也无 `regime_multiplier`**。
- **对比**：盘前主路径 `instruction_generator.py:404-416` 有 `_water_mult = min(_pos_mult, _risk_mult)`（YELLOW=0.5 / ORANGE=0.25）。
- **影响**：账户回撤进入 YELLOW(-5~-10%)/ORANGE(-10~-20%) 时，兜底路径仓位比主路径大 2x(YELLOW)~4x(ORANGE)，**水位减仓防御被绕过**。
- **触发**：DA/UE bridge 直写、当前非 GREEN 水位。
- **博弈点**：是否必修？兜底路径是否应强制套用与主路径同一个 sizing 函数（消除双实现）？

### F2. PnL 用 DB quantity 而非 broker 实际成交量 [已核实]
- **位置**：`app/playbook/pipeline.py:4270`（及 SL 兜底 4041、EOD 4494）
- **现象**：`pnl = (exit-entry)*qty`，`qty=r["quantity"]`（DB量）；但 `close_position` 实际按 `int(broker_qty_before)` 平仓（dual_broker.py:297）。
- **缓解现状**：前一轮已修入场按 `_effective_qty` 记账，DB.quantity 通常==broker量。**残留触发面**：入场部分成交且 broker 查询失败退回下单量、手动干预、隔夜量变。
- **影响**：账实不一致时 PnL 失真（示例 DB100/实际60 → PnL 放大 1.67x）。
- **博弈点**：是否用 close_res 返回的 `dealt_qty`（B4 已让 close_position 返回该字段）重算 PnL？还是 B3 量级校验告警已够？

### F3. int(broker_qty) 截断碎股 → 永久裸留 [已核实]
- **位置**：`app/execution/dual_broker.py:297` `close_qty = int(broker_qty_before)`
- **影响**：99.7 股 → 平 99，残 0.7 股无法平掉。SIM 一般整数，live/拆股/股息再投资可能出现小数。
- **博弈点**：改 `round()` 还是 `math.ceil()`？（ceil 可能超卖，round 可能残留 <0.5）——需定语义。

---

## 类别二：风控真实性（静默失效同源——建议统一治理）

### R1. 一类同源缺陷：风控/预算查询异常被吞 → 放行 [已核实]
起草 agent 判断这几处**本质同源**（异常即放行），建议**统一用 fail 模式治理**，而非逐个补丁：

| 编号 | 位置 | 异常时行为 | 危险 |
|---|---|---|---|
| R1a | `pipeline.py:2288` | regime/risk_state 检查异常→放行 | NO_TRADE/HALT 状态仍开仓 |
| R1b | `pipeline.py:2320` `except: pass` | buying_power 查询异常→`_max_deploy=0`→预算门恒 False | **2026-06-25「$82K账户下$388K」事故同类根因** |
| R1c | `pipeline.py:2419` | broker 持仓检查异常→放行 | 对已有持仓 symbol 重复下单 |

- **博弈点**：是否把这三处纳入与 `acb_fail_mode` 统一的 fail-mode 治理（live 前默认 fail-close）？还是逐处单独处理？R1b 是否应无条件 fail-close（预算门失效历史上已出过事故）？

### R2. 退出监控单仓位异常 → 本轮完全失去止损保护
- **位置**：`app/risk/position_exit_monitor.py:1145`（每仓 try/except，异常则跳过该仓本轮）
- **影响**：某仓价格获取/ATR/规则判断抛异常 → 该仓本轮无止损，下一轮才重试。
- **博弈点**：单仓异常是否应升级告警/推送（而非仅 logger.error）？SIM 期可接受否？

---

## 类别三：出场链路正确性

### E1. max_drawdown 列写入的是 MAE，语义错误
- **位置**：`app/playbook/pipeline.py:3261,3267`（`max_drawdown = _mae_pct`）
- **说明**：max_drawdown 应 peak→trough，MAE 是 entry→trough。下游报表读 max_drawdown 全错。
- **博弈点**：修列 or 改下游？属数据可信度，非交易安全。

### E2. EOD ghost close 用入场价当出场价 → PnL 恒 0 污染统计
- **位置**：`pipeline.py:4436`（`_eod_price = entry_price`）、正常路径 ghost `pipeline.py:4207`
- **影响**：盘后/周末所有报价源失败时，该笔记为"保本出场"污染胜率/PnL 统计。
- **博弈点**：无报价时应 (a) 用最后已知 last_price / (b) 标记 `void` 不计统计 / (c) 保持现状？

### E3. monitor 与 EOD force_close 竞态 → trade_records 重复 UPDATE
- **位置**：`scheduler_core.py:1894`(monitor) + `pipeline.py:1882`(EOD)，查同一 SQL
- **说明**：`_close_trade_record` 的 `outcome NOT IN (win,loss,void)` 幂等只保护 playbook_instructions，**不保护 trade_records 的无条件 UPDATE**。
- **博弈点**：与并发项(C1/C2 见下)合并治理？

### E4. SL/TP 取价 last→回退 price，非 bid/ask [方向已验证正确]
- **位置**：`pipeline.py:3947`（provider 均无 "last" 键 → 恒回退 "price"=最新成交价）
- **影响**：SL/TP 触发用成交价而非 bid/ask，快速行情下滑点略大于预期。**方向 LONG/SHORT 比较已核实无写反**。
- **博弈点**：属精度优化，是否上线后再议？

### E5. EOD holding_hours 用 wall-clock 含非交易时段
- **位置**：`pipeline.py:4496-4500`（隔夜仓 72h 而非 ~6.5h，若 _close_trade_record 重算失败则保留）
- **博弈点**：数据可信度项。

---

## 类别四：重启恢复（CRITICAL——裸仓风险）

### C1. 重启丢失 peak/trough/MFE/MAE → trailing 锁利重置
- **位置**：`app/pipeline/position_monitor.py:697-718`
- **说明**：恢复只读 open_positions 的 trail_state/entry_time，**不读 trade_records.metadata_json 的 peak_price/trough_price**。重启后 peak_price=0 → breakeven/trailing 计算重置，已实现浮盈的锁利保护丢失。
- **博弈点**：恢复流程是否应回填 metadata_json 的 peak/trough？这是**盈利保护正确性**问题，倾向必修。

### C2. Broker 不可用时全仓变裸仓（无止损）
- **位置**：`position_monitor.py:641`、`position_recovery.py:138`（broker 预查询失败→`return 0` 不恢复任何仓位）
- **影响**：券商有真实持仓但系统内存空白，**无任何止损保护直到下次成功重启**。
- **博弈点**：broker 不可用时应 (a) 从 DB 恢复并标记"待券商确认" / (b) 持续重试 / (c) 告警人工介入？现状"空仓等待"最危险。

### C3. 恢复 entry_time 丢失 → held_minutes=0
- **位置**：`position_monitor.py:711-717`（fallback now_et → 冷却期重置、TIME_EXIT 永不触发）

### C4. 两套 restore_positions_from_db 并存
- **位置**：`scheduler_core.py:1780`→`position_monitor.py:615`（实际运行）vs `core/position_recovery.py`（更完整，含幽灵检测，疑似死代码或误调用）
- **博弈点**：属 C2 整洁度——是否收敛为一个 SSOT 恢复函数？

---

## 类别五：SQLite 并发（CRITICAL——database is locked + 重入双平）

### D1. monitor_playbook_sim_exits 裸连接绕过 WAL 池 [已核实]
- **位置**：`pipeline.py:3884` `conn = sqlite3.connect(db_path)`（无 WAL/busy_timeout/timeout）；EOD `pipeline.py:4322` 同样裸连接
- **对比**：`app/storage/db_pool.py` 单例已配 WAL+busy_timeout，但关键路径绕过它。db_pool 自述有 199 处散落 `sqlite3.connect()`。
- **影响**：与 scan_loop/monitor_loop 写库并发 → database is locked。

### D2. monitor_playbook_sim_exits 无重入保护 [已核实]
- **位置**：`scheduler_core.py:1894`（每轮~60s 调用，无 `_last_run` 时间戳、无锁）
- **对比**：相邻 `auto_execute` 有 `_playbook_auto_execute_last_run` 300s 保护（scheduler_core.py:1880），**sim_exit 完全没有**。
- **影响**：一轮执行 >60s（broker 延迟/DB锁等待）→ 下一轮重入 → **同一仓位双平 + conn.commit 交叉不一致**。
- **博弈点**：D1+D2 是并发问题的核心，倾向必修：sim_exit 改用 db_pool + 加重入保护（`_last_run` 或线程锁）。

### D3. _close_trade_record 无显式事务边界
- **位置**：`pipeline.py:3068-3333`（UPDATE trade_records → UPDATE playbook_instructions → DELETE open_positions，中间无 BEGIN，metadata 更新在调用前已 commit）
- **影响**：步骤间失败 → trade_records 已平但 open_positions 未删（或反之）→ 账实不一致/幽灵仓。
- **博弈点**：是否包裹单一事务（BEGIN...COMMIT）保证原子性？

### D4. is_playbook_open_position 每次新建连接
- **位置**：`pipeline.py:3548`（被 position_monitor/recovery/exit_monitor 高频调用，每次 `sqlite3.connect(timeout=5)`）
- **博弈点**：改用 db_pool 复用连接？

---

## 类别六：资金口径 SSOT（双尺子）

### S1. buying_power(含杠杆) vs equity(不含) 双尺子
| 路径 | risk_base 来源 | 位置 |
|---|---|---|
| Playbook | current_equity | pipeline.py:274-295 |
| position_sizer（已修） | equity | position_sizer.py:141-146 |
| **ORB** | **buying_power→cash→equity** | orb/engine.py:1959-1984 |
| **capital_safety.calc_position_size** | **buying_power→cash→equity*0.2** | capital_safety.py:419-430 |
- **说明**：Futu SIMULATE buying_power 可达 equity×1.3。ORB/capital_safety 用含杠杆口径，同一资金仓位比 Playbook 大 ~30%。**注**：ORB 已停用（orb.enabled=false），capital_safety.calc_position_size 需确认是否在 Playbook 活跃路径被调。
- **博弈点**：ORB 已停用是否降级为"上线后偿还"？capital_safety.calc_position_size 是否有活跃调用者？

### S2. 兜底资金基准不一致：capital_safety 100K vs playbook 82K
- **位置**：`capital_safety.py:132`(100_000) vs `pipeline.py:293-295`(82000)
- **影响**：broker 未同步时，熔断/水位用 100K、仓位/额度用 82K，两风控子系统基准矛盾（水位偏松、仓位偏保守）。
- **博弈点**：统一兜底常量（SSOT）？

### S3. 日名义额度未按 source_type 过滤
- **位置**：`pipeline.py:2357-2366`（`SUM(entry_price*quantity)` 无 source_type 过滤）
- **对比**：ORB 查询有 `WHERE source_type='ORB_ENGINE'`
- **影响**：当日手动/ORB 交易 notional 挤占 Playbook 的 daily_notional_cap。
- **博弈点**：加 `source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 过滤？

### S4. 日额度累计口径是下单量非成交量（保守方向）
- **位置**：`pipeline.py:2693-2696`（`_max_deploy += _new_notional`，部分成交时高估消耗）
- **说明**：方向保守（不会超额，只会误拦后续信号）。低优先。

---

## 需复核 agent 产出的结论

1. **逐项裁定**：F1-F3 / R1-R2 / E1-E5 / C1-C4 / D1-D4 / S1-S4，每项给：**上线前必修 / 上线后偿还 / 不修（说明理由）**。
2. **同源治理判断**：R1a/R1b/R1c 是否统一纳入 fail-mode（避免逐个补丁）？D1+D2+D3+D4 是否作为"并发一揽子"统一用 db_pool + 事务 + 重入保护治理？
3. **去重/SSOT 判断**：F1 兜底 sizing 是否应复用主路径同一函数？C4 两套恢复函数是否收敛？S1/S2 口径是否统一？——目标是"减少双实现"，符合"框架干净无包袱"。
4. **误报排查**：标注哪些是误报或已被其他机制覆盖（如 S1 ORB 已停用、F2 已被入场记账缓解）。
5. **排序后的最终修复方案**：含明确的**不做项及理由**，避免过度工程。

## 原则约束（所有 agent 必须遵守）
- 复用现有模块（db_pool、fail-mode、sizing 主路径函数），不造轮子、不新增分叉。
- 不过度工程：SIM 期可接受的风险明确标注"上线后偿还"，不为假设需求设计。
- 数据积累期：edge 未证明前，优先"资金正确 + 风控真实 + 账实一致"，而非加策略功能。
- 改动可回滚、可配置化优先。
- **修复本身不得引入新的双重管理或分叉**（如新增第二套 sizing/恢复/连接逻辑）。

---

## 复核 Agent 深度分析报告（Agent-α，第二轮盲区审计，2026-07-19）

> 方法：不复用起草 agent 结论，独立读源码逐项核实 file:line，并基于「入场→风控→出场→记账→对账→重启恢复」交易全链路做交叉验证，再给出裁定。

### 0. 全链路核查视角（先建立坐标系）

Playbook 唯一管线的真实数据流（核实后）：

```
DA/UE bridge 直写 instruction
  → run_playbook_auto_execute (pipeline.py:2280+, 每5min, 有300s重入保护)
      ├─ 风控门: regime/risk_state(R1a) → buying_power预算(R1b) → broker持仓(R1c) → ACB/PDT
      ├─ sizing: 主路径 instruction_generator:416 (含 water_mult×regime) vs 兜底 pipeline:2572 (无) ← F1
      └─ 下单: dual_broker.submit_order (remark=B4, _effective_qty=A5)
  → monitor_playbook_sim_exits (pipeline.py:3862, 每轮~60s, 无重入保护 ← D2)
      ├─ 裸连接 sqlite3.connect (无WAL ← D1)
      ├─ 取价失败 → continue 跳过该仓本轮 (← R2 同源, 但位置在3950非1145)
      └─ 触发退出 → _close_trade_record (pipeline.py:3068, 无事务边界 ← D3)
  → EOD: _force_close_orphaned_sim_positions (pipeline.py:4305, 与monitor竞态 ← E3)
  → 重启: position_monitor.restore (615) — playbook仓被跳过(684), 由monitor自管
```

**关键架构事实（影响多项裁定）**：
1. Playbook 仓位**不进入主线 PositionStore**（`position_monitor.py:679-689` 显式跳过），由 `monitor_playbook_sim_exits` 独占管理退出。因此 **R2 引用的 `position_exit_monitor.py:1145` 是主线监控，不直接管 Playbook 仓**——但 Playbook 自己的监控在 `pipeline.py:3950-3951` 有同源行为（取价失败→跳过该仓本轮）。R2 不是误报，但定位需修正。
2. `monitor_playbook_sim_exits` 与 `auto_execute` 的重入保护**不对称**（后者有 300s `_last_run`，前者完全没有）——这是 D2 的核心，也是 E3 竞态的放大器。
3. 记账落点有三处独立 UPDATE（trade_records / playbook_instructions / open_positions DELETE），无统一事务——D3 与 E3 同根。

### 1. 逐项裁定

#### 类别一：资金正确性

**F1. 兜底 sizing 缺水位/regime 乘数 — 【上线前必修，P0】**
- 核实属实：`pipeline.py:2572` `_risk_amount = _risk_base * (_risk_pct/100) * _conf_factor`，确无 `_water_mult`/`regime_multiplier`；对比 `instruction_generator.py:416` `risk_amount = _risk_base * (risk_pct/100) * regime_multiplier * _water_mult`。
- 危害定级：**真金白银**。回撤进 ORANGE 时兜底路径仓位放大 4x，恰好在水位减仓防御最该生效时反向放大——这是"反身性"bug（越亏越冒进）。
- **裁定**：必修。**且不应逐处补乘数（会留下第三套 sizing 实现），而应抽一个 `_compute_position_size(risk_base, risk_pct, sl_distance, regime_mult, water_mult, conf_factor, max_pos_pct)` 公共函数，主路径与兜底路径共用**——符合"减少双实现"铁律。这是 F1 与"去重 SSOT"诉求的合并解。

**F2. PnL 用 DB quantity 而非 broker 成交量 — 【上线后偿还，P2】**
- 核实属实：`pipeline.py:4270` 用 `qty=r["quantity"]`（DB量）；`close_position` 按 `int(broker_qty_before)` 平仓。
- 缓解评估：A5 已让入场按 `_effective_qty` 记账，DB.quantity 常态==broker量；B3 已加量级校验告警。残留触发面窄（入场部分成交+broker查询失败退回下单量、手动干预、隔夜量变）。
- **裁定**：上线后偿还。close_res 已返回 `dealt_qty`（B4 核实），未来用其重算 PnL 是 1 行改动，但当前 B3 告警已能暴露偏差，SIM 期不阻塞。**不做"静默用 dealt_qty 覆盖"，因为这会掩盖入场记账 bug——应保持 DB.quantity 为记账真源，偏差走 B3 告警人工核。**

**F3. int(broker_qty) 截断碎股 — 【上线后偿还，P2，但需定语义】**
- 核实属实：`dual_broker.py:297` `close_qty = int(broker_qty_before)`。
- SIM 期：模拟盘整数股，无触发。Live/拆股/股息再投资才有小数。
- **裁定**：上线后偿还。**语义裁定：用 `round()` 而非 `ceil()`**——ceil 在 99.5→100 时会超卖（broker 拒单或裸卖空），round 残留 <0.5 股的尾差由"碎股自清理"兜底（残留 <0.5 股价值极小，下次对账标记清理）。配合 B3 量级校验，残留碎股会被告警发现。

#### 类别二：风控真实性

**R1a/R1b/R1c. 同源异常放行 — 【统一治理，R1b 上线前必修 P0，R1a/R1c 上线前 P1】**
- 核实属实：
  - R1a `pipeline.py:2288-2289` regime/risk_state 异常→放行
  - R1b `pipeline.py:2320-2321` buying_power 查询异常→`pass`→`_max_deploy=0`→预算门恒 False（**$388K/$82K 事故同类根因，已核实注释 pipeline.py:2306 明确记载该事故**）
  - R1c `pipeline.py:2419-2420` broker 持仓检查异常→放行
- **同源治理裁定**：三处本质同源（异常即放行），**应统一纳入与 `acb_fail_mode` 同款的 fail-mode 配置治理，而非逐个补丁**。建议扩展为 `risk.preflight_fail_mode: open|closed`（默认 open，live 翻 closed），三处共用一个 `_preflight_guard(check_fn, name)` 包装器。
- **R1b 单独升级**：R1b 是预算门，失效=无上限开仓（已出过 $388K 事故），**应无条件 fail-close，不受配置开关控制**——即异常时 `_max_deploy = +inf`（视为已用满预算，拦后续所有单），而非 `_max_deploy=0`。这是"预算门失效安全"原则：宁可误拦不可漏放。
- R1a/R1c：纳入统一 fail-mode，SIM 期默认 open，live 前翻 closed。

**R2. 退出监控单仓异常 → 本轮失去止损 — 【定位修正 + 上线前 P1】**
- **定位修正**：起草 agent 引用 `position_exit_monitor.py:1145`（主线监控），但 Playbook 仓不走主线（被 `position_monitor.py:684` 跳过）。Playbook 真正的同源点在 `pipeline.py:3950-3951`：`if not current_price: continue`——取价失败静默跳过该仓本轮，**无止损保护直到下一轮**。
- 危害：单仓价格源持续失败（如 Finnhub 限流该 symbol）→ 该仓多轮无止损。
- **裁定**：上线前 P1。**不改"跳过"行为**（单仓异常不应阻断整批监控，这是正确的隔离），但**必须升级可观测性**：连续 N 轮（建议 3 轮）取价失败的 symbol 触发飞书告警，避免"静默失保护"。SIM 期可接受跳过，但不可接受静默。

#### 类别三：出场链路正确性

**E1. max_drawdown 列写入 MAE — 【上线后偿还，P2，数据可信度】**
- 核实属实：`pipeline.py:3261,3267` `max_drawdown=?` 绑定 `round(_mae_pct, 4)`。max_drawdown 语义应 peak→trough，MAE 是 entry→trough。
- **裁定**：上线后偿还。属数据可信度，非交易安全。**修法裁定：改下游而非改列**——因为 peak_price 已在 metadata_json 落库（A 部分核实），真实 max_drawdown 可由 `(peak-trough)/peak` 重算；直接改列写入需保证 peak/trough 时序正确，回归面更大。建议报表层从 metadata 重算，列保留兼容。

**E2. EOD ghost close 用入场价 → PnL 恒 0 污染统计 — 【上线前必修，P1】**
- 核实属实：`pipeline.py:4436` `_eod_price = entry_price`（无报价兜底）→ 该笔 PnL=0 记为"保本"，污染胜率/净PnL（当前 43.4%/-\$976.93 的统计基数可能被这类假保本单稀释）。
- **裁定**：上线前必修。**选项裁定：(b) 标记 `void` 不计统计**，而非 (a) 用 last_price（无报价时 last_price 也不可信）或 (c) 保持现状。理由：graduation 门槛（B5）依赖胜率/净PnL，假保本单直接污染毕业判定，必须在数据积累期就保证统计纯净。`void` outcome 已被 `_close_trade_record` 的 `outcome NOT IN (win,loss,void)` 幂等支持，落地成本低。

**E3. monitor 与 EOD force_close 竞态 → trade_records 重复 UPDATE — 【上线前必修，P1，与 D2/D3 合并】**
- 核实属实：`scheduler_core.py:1894` monitor + `pipeline.py:1882` EOD 查同一 SQL；`_close_trade_record` 幂等只保护 playbook_instructions（`outcome NOT IN`），**trade_records UPDATE 无条件**（pipeline.py:3257-3267 无 WHERE outcome 过滤）。
- **裁定**：上线前必修，**与 D2（重入保护）+ D3（事务边界）合并为"并发一揽子"治理**（见 D 部分）。根因是 monitor 无重入保护 + 记账无原子事务，单修 E3 是治标。

**E4. SL/TP 取价 last→price 非 bid/ask — 【上线后偿还，P3，精度优化】**
- 核实属实：`pipeline.py:3947` `quote.get("last", 0) or quote.get("price", 0)`，provider 无 "last" 键恒回退 "price"。方向已核实无写反。
- **裁定**：上线后偿还。属精度优化，快速行情滑点略增，SIM 期不阻塞。live 小仓验证后视滑点数据（B1 同源决策依据）再议。

**E5. EOD holding_hours 用 wall-clock 含非交易时段 — 【上线后偿还，P3，数据可信度】**
- 核实属实：`pipeline.py:4496-4500` 隔夜仓算 72h 而非 ~6.5h。
- **裁定**：上线后偿还。数据可信度项，不影响交易安全。与 E1 同属"报表口径"治理，可合并一次性修。

#### 类别四：重启恢复（CRITICAL）

**C1. 重启丢失 peak/trough → trailing 锁利重置 — 【上线前必修，P0】**
- 核实属实：`position_monitor.py:697-718` 恢复只读 trail_state/entry_time，**不读 trade_records.metadata_json 的 peak_price/trough_price**（line 718 仅 `position.trail_state = row.get("trail_state", {})`）。
- 危害定级：**盈利保护正确性**。重启后 peak_price=0 → breakeven/trailing 重置，已实现浮盈的锁利保护丢失 → 盈利单变亏损单。这是"赚到的钱守不住"，直接侵蚀 edge。
- **关键 nuance**：Playbook 仓虽被主线恢复跳过（684），但 `monitor_playbook_sim_exits` 重启后从 trade_records 重建 meta（pipeline.py:3933-3938 读 metadata_json），**peak/trough 本就在 metadata_json 里**——需核实 monitor 是否正确读取并续算。若 monitor 每轮从 metadata 读 peak/trough 并回写，则 Playbook 路径 C1 影响较小；若 monitor 也依赖内存态，则同样中招。**裁定前先验证 monitor 的 peak/trough 持久化闭环**（见待办）。
- **裁定**：上线前必修（盈利保护不可妥协）。但修法取决于上述验证结果——若 monitor 已自洽持久化，则 C1 仅影响主线遗留仓（降级）；否则需在恢复流程回填 metadata peak/trough。

**C2. Broker 不可用 → 全仓裸仓 — 【上线前必修，P0】**
- 核实属实：`position_monitor.py:641-643` 与 `position_recovery.py:138-140` 均 `return 0`（broker 预查询失败→不恢复任何仓位）。
- 危害定级：**最高**。券商有真实持仓但系统内存空白 → 无任何止损保护直到下次成功重启。服务断连+broker 短暂不可用窗口 = 真实持仓裸奔。
- **裁定**：上线前必修。**选项裁定：(a) 从 DB 恢复并标记"待券商确认"(pending_broker_confirm)**，而非 (b) 持续重试（阻塞启动）或 (c) 仅告警人工（夜间无人响应）。DB 恢复保证止损规则立即接管，待 broker 恢复后再对账修正——"先有保护，再校准确"。配合飞书告警（broker 不可用是严重事件）。

**C3. 恢复 entry_time 丢失 → held_minutes=0 — 【上线前 P1】**
- 核实属实：`position_monitor.py:711-717` fallback `now_et()` → 冷却期重置、TIME_EXIT 永不触发。
- **裁定**：上线前 P1。entry_time 在 open_positions/trade_records 均有落库，恢复时应读取而非 fallback now。与 C1 同属"恢复完整性"，合并修。

**C4. 两套 restore_positions_from_db 并存 — 【上线后偿还，P2，整洁度】**
- 核实属实：`scheduler_core.py:1780`→`position_monitor.py:615`（实际运行）vs `core/position_recovery.py`（更完整含幽灵检测）。
- **裁定**：上线后偿还。属整洁度，非交易安全。**但 C2 修复时应顺手明确哪个是 SSOT**——建议以 position_monitor.restore 为 SSOT（实际运行），position_recovery.py 若确认无调用者则标记 deprecated（不删，AGENT.md 铁律）。避免 C2 修复时误用第二套。

#### 类别五：SQLite 并发（CRITICAL）

**D1+D2+D3+D4. 并发一揽子 — 【上线前必修，P0/P1，统一治理】**

这是本轮最系统性的一类问题，**必须作为一揽子治理，不可拆散逐个补丁**（拆散会留下"修了 D1 没修 D2，重入仍双平"的半拉子）。

- **D2（重入保护）= 核心，P0**：核实 `scheduler_core.py:1893-1897` monitor 无任何 `_last_run`/锁，对比 auto_execute（1880-1888）有 300s 保护。一轮 >60s（broker 延迟/DB锁）→ 下一轮重入 → **同一仓位双平**。这是"双平"事故的直接引信。**修法：复用 auto_execute 同款 `_playbook_sim_exit_last_run` 时间戳 + threading.Lock 重入保护**（不造新轮子）。
- **D1（裸连接）= P0**：核实 `pipeline.py:3884` `sqlite3.connect(db_path)` 无 WAL/busy_timeout；db_pool.py 自述 199 处散落连接。**修法：monitor/EOD 改用 db_pool 单例**（已配 WAL+busy_timeout=5000）。这是 D2 重入时"conn.commit 交叉不一致"的根因。
- **D3（事务边界）= P1**：核实 `_close_trade_record`（pipeline.py:3068-3333）三处 UPDATE/DELETE 无 BEGIN...COMMIT。**修法：包裹单一事务**，保证 trade_records UPDATE + playbook_instructions UPDATE + open_positions DELETE 原子。与 E3 竞态同根。
- **D4（is_playbook_open_position 每次新建连接）= P2**：核实 `pipeline.py:3548` 高频调用每次 `sqlite3.connect(timeout=5)`。**修法：改用 db_pool 复用**。属性能优化，非正确性，可上线后。

**一揽子裁定**：D2（重入锁）+ D1（db_pool）+ D3（事务）三者**必须同批上线**，构成"并发安全基线"。D4 可独立后置。

#### 类别六：资金口径 SSOT

**S1. buying_power vs equity 双尺子 — 【ORB 降级上线后；capital_safety 需查调用者】**
- 核实属实：`capital_safety.py:419-430` 用 buying_power→cash→equity*0.2；Playbook 用 current_equity。
- **误报排查**：ORB 已停用（orb.enabled=false，A 部分已核实 di_container 清理）→ ORB 路径降级为"上线后偿还/不修"。
- **capital_safety.calc_position_size 需验证**：若其无 Playbook 活跃调用者（Playbook 用自己的 sizing），则 S1 在 Playbook 唯一管线下**实际无影响**——需 grep 确认调用者（见待办）。若无活跃调用，S1 降级为"文档化口径差异，上线后偿还"。

**S2. 兜底基准 100K vs 82K — 【上线前 P1，SSOT 统一】**
- 核实属实：`capital_safety.py:132` `initial_capital = 100_000` vs `pipeline.py` 82000（risk_base_capital）。
- 危害：broker 未同步时，熔断/水位用 100K、仓位/额度用 82K，两风控子系统基准矛盾（水位偏松+仓位偏保守=风险敞口被低估）。
- **裁定**：上线前 P1。**统一兜底常量为 SSOT**——建议都指向 `playbook.risk_parameters.risk_base_capital`（82K，已是 Playbook 真源），capital_safety 的 100K fallback 改为读取同一配置。符合"减少双实现"。

**S3. 日名义额度未按 source_type 过滤 — 【上线前 P1】**
- 核实属实：`pipeline.py:2360-2362` `SUM(entry_price*quantity)` 仅按 `substr(entry_time,1,10)=today` 过滤，**无 source_type 过滤**；对比 ORB 有 `WHERE source_type='ORB_ENGINE'`。
- 危害：当日手动/ORB/主线交易 notional 挤占 Playbook 的 daily_notional_cap → Playbook 被误拦（保守方向，不会超额但会漏信号）。
- **裁定**：上线前 P1。加 `source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 过滤。1 行 SQL 改动，低风险高收益（让 Playbook 额度自治）。

**S4. 日额度累计用下单量非成交量 — 【不修，保守方向】**
- 核实属实：`pipeline.py:2693-2696` 部分成交时高估消耗。
- **裁定**：不修。方向保守（只会误拦不会超额），与 F2 同理——保持下单量为口径，偏差由 B3 告警暴露。修它反而引入"成交量回填"复杂度，违反不过度工程。

### 2. 同源治理判断（回答复核要求 2）

| 同源簇 | 成员 | 治理方式 | 裁定 |
|--------|------|----------|------|
| **fail-mode 簇** | R1a/R1b/R1c + 已有 acb_fail_mode | 统一 `risk.preflight_fail_mode` 配置 + `_preflight_guard` 包装器；**R1b 例外：预算门无条件 fail-close** | 采纳统一治理，R1b 单独升级 |
| **并发安全簇** | D1/D2/D3 + E3 | db_pool（D1）+ 重入锁（D2）+ 事务边界（D3）三件套同批上线 | 采纳一揽子，不可拆散 |
| **恢复完整性簇** | C1/C3 (+C2 关联) | 恢复流程统一回填 metadata（peak/trough/entry_time）+ broker 不可用 DB 兜底 | C1/C3 合并修，C2 独立但同批 |
| **报表口径簇** | E1/E5 (+F2 关联) | 报表层从 metadata 重算真实口径，列保留兼容 | 上线后一次性合并修 |

**反对逐个补丁的理由**：R1 三处若逐个改，会留下三个风格不一的 except 块，下次又漏；D1-D3 若拆散，"修了连接没修重入"仍会双平。同源同治才是"框架干净无包袱"。

### 3. 去重 / SSOT 判断（回答复核要求 3）

| 双实现 | 收敛方案 | 优先级 |
|--------|----------|--------|
| **sizing 双实现**（F1：主路径 vs 兜底） | 抽 `_compute_position_size()` 公共函数，两路径共用 | **P0**（与 F1 合并） |
| **恢复双实现**（C4：position_monitor vs position_recovery） | 以 position_monitor.restore 为 SSOT，position_recovery 标 deprecated（不删） | P2（C2 修时顺手明确） |
| **资金口径双尺子**（S1/S2） | S2 统一兜底基准指向 risk_base_capital；S1 待查调用者 | S2=P1，S1=待定 |
| **连接双轨**（D1：裸连接 vs db_pool） | 关键路径统一走 db_pool | P0（与 D 簇合并） |

**原则**：收敛≠删除。所有 SSOT 收敛都保留旧实现（标 deprecated/注释），符合 AGENT.md "不删主线" 铁律，仅切断活跃调用。

### 4. 误报排查（回答复核要求 4）

| 项 | 判定 | 理由 |
|----|------|------|
| **S1（ORB 部分）** | ✅ 误报/已覆盖 | ORB 已停用（orb.enabled=false），buying_power 口径在 Playbook 唯一管线下无活跃影响 |
| **S1（capital_safety 部分）** | ⚠️ 待验证 | 需 grep `calc_position_size` 调用者；若 Playbook 不调则降级 |
| **F2** | ⚠️ 已被缓解 | A5 入场记账 + B3 量级校验已覆盖主触发面，残留窄，降级 P2 |
| **R2（定位）** | ⚠️ 定位偏差 | 引用主线 position_exit_monitor:1145，但 Playbook 同源点在 pipeline.py:3950；问题真实存在但位置需修正 |
| **S4** | ✅ 保守方向不修 | 下单量口径只会误拦不会超额，B3 告警已暴露偏差 |
| **C1（Playbook 路径）** | ⚠️ 待验证 | monitor 可能已从 metadata 自洽持久化 peak/trough，需验证后定级 |

### 5. 排序后的最终修复方案（回答复核要求 5）

#### 第一梯队：上线前 P0（阻塞上线，真金白银/裸仓/双平）

1. **F1 — sizing SSOT 公共函数**：抽 `_compute_position_size()`，主路径+兜底共用，兜底补 water_mult×regime。
2. **R1b — 预算门无条件 fail-close**：异常时 `_max_deploy=+inf`（拦后续所有单），不受配置开关控制。
3. **C2 — broker 不可用 DB 兜底恢复**：从 DB 恢复 + 标记 pending_broker_confirm + 飞书告警。
4. **C1 — 重启回填 peak/trough**（验证 monitor 持久化闭环后定修法）。
5. **并发一揽子 D1+D2+D3**：db_pool + 重入锁 + 事务边界，三件套同批。

#### 第二梯队：上线前 P1（强烈建议）

6. **R1a/R1c — 统一 fail-mode**：`risk.preflight_fail_mode` 配置，SIM 默认 open。
7. **R2 — 取价失败连续告警**：连续 3 轮失败 symbol 飞书告警（不改跳过行为）。
8. **E2 — EOD 无报价标 void**：不计统计，保毕业判定纯净。
9. **E3 — 与 D 簇合并**（已含在 5）。
10. **S2 — 兜底基准 SSOT**：capital_safety 100K → 读 risk_base_capital。
11. **S3 — source_type 过滤**：日额度 SQL 加 `IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')`。
12. **C3 — 恢复读 entry_time**（与 C1 合并）。

#### 第三梯队：上线后偿还（P2/P3）

13. F2 — PnL 用 dealt_qty 重算（P2，B3 告警已覆盖）
14. F3 — int→round 碎股语义（P2，live 触发）
15. C4 — 恢复函数 SSOT 收敛（P2，C2 修时顺手）
16. D4 — is_playbook_open_position 走 db_pool（P2，性能）
17. S1 — 口径文档化（P2，待查调用者）
18. E1 — max_drawdown 报表层重算（P2）
19. E4 — SL/TP 用 bid/ask（P3，精度）
20. E5 — holding_hours 交易时段口径（P3）

#### 明确不做项

| 项 | 理由 |
|----|------|
| S4 日额度改成交量口径 | 保守方向，B3 告警已暴露，修它引入回填复杂度（过度工程） |
| S1 ORB 口径修复 | ORB 已停用，无活跃影响 |
| F2 静默用 dealt_qty 覆盖 DB.quantity | 会掩盖入场记账 bug，应保持 DB 为真源走告警 |
| F3 用 ceil() | 超卖风险（裸卖空/拒单），用 round() |
| R2 改"不跳过" | 单仓异常阻断整批监控更危险，隔离正确，只补告警 |
| 删除 position_recovery.py / 主线 order_executor | AGENT.md 铁律：不删主线，仅标 deprecated |

### 6. 遗留待辩论焦点（供下一轮 Agent-β 博弈）

1. **R1b fail-close 的"无条件"是否过激**？预算门异常即拦全部，可能在 broker 短暂抖动时全停。是否应配"连续 N 次异常才 fail-close"的退化策略？（α 倾向：宁可全停不可无上限，但接受辩论）
2. **C2 DB 兜底恢复的"待券商确认"态如何退出**？broker 恢复后对账发现 DB 仓 broker 无 → 是 ghost 还是已外部平仓？需与 position_sync 的外部平仓检测（_is_external_close）协同，避免误判。
3. **C1 的 Playbook 路径是否真受影响**？取决于 monitor_playbook_sim_exits 的 peak/trough 持久化闭环验证结果——这是本轮最大不确定项，β 应优先实证。
4. **sizing 公共函数（F1）的回归面**：抽函数会同时触碰主路径（instruction_generator）与兜底（pipeline），两处测试覆盖是否足够？是否需要先补 sizing 单测再重构？
5. **并发一揽子（D1-D3）的上线顺序**：db_pool 切换（D1）与重入锁（D2）哪个先？α 倾向 D2 先（重入锁立即止血双平，1 行级改动），D1 次之（db_pool 切换需回归测试）。

### 7. 给终裁轮的实证待办（每条可验证）

1. grep `calc_position_size` 全仓调用者，确认 S1 capital_safety 路径是否有 Playbook 活跃调用。
2. 验证 `monitor_playbook_sim_exits` 是否每轮从 metadata_json 读 peak/trough 并回写（定 C1 Playbook 路径定级）。
3. 核实 `position_recovery.py` 是否有任何活跃调用者（定 C4 SSOT 收敛方向）。
4. 验证 db_pool 单例在 monitor/EOD 切换后的线程安全（多 loop 共用连接池）。
5. 实证当前 83 笔已平仓中有多少笔是 E2 类"无报价 ghost 保本"（量化 E2 对毕业判定的污染程度）。

---

## 辩论 Agent-β 实证博弈报告（第三轮，2026-07-19）

> 方法：不复用 α 结论。对 α §6 五个焦点与 §7 五项实证待办逐一用源码亲验（file:line）+ DB 真实数据交叉验证；对 α 已裁 20 项做独立抽样复核。产出：待办闭环 / 定级修正 / 焦点裁决 / 共识清单 / 终裁待决项。
> 状态区笔误修正：α §7 实列 **5 项**实证待办（状态区写的"7 项"为笔误）。

### β.0 总评

α 报告质量高：20 项中 **13 项 β 复核后完全同意**（F1/F2/F3/R1a/R1b/R1c/R2/E1/E2/E4/E5/S3/S4）。但 α 在**并发一揽子（D1/D2/D3/E3）与 C1/C2/C3** 上存在系统性高估——其隐含假设是"多线程并发 + monitor 依赖内存态"，β 实证发现两个前提均不成立。修正后：**上线前 P0 从 α 的 5 项收敛为 2 项（F1、R1b）**，"并发一揽子"解散，必修工作量下降约 60%。

### β.1 α §7 实证待办闭环（5/5 完成）

| # | 待办 | 实证方法与结果 | 裁定影响 |
|---|------|----------------|----------|
| 1 | grep `calc_position_size` 调用者 | 全仓 grep：仅 `order_executor.py:1268/1298`（主线 position_sizer）、`capital_safety.py:368`（自身）、`portfolio_constructor.py:340`（capital_safety 方法）。**无任何 Playbook 活跃调用** | **S1 闭环降级**：ORB 已停用 + capital_safety 路径无 Playbook 调用 → S1 移出必修，文档化口径差异（P2） |
| 2 | monitor 的 peak/trough 持久化闭环 | **确认无状态设计**：`pipeline.py:3933-3938` 每轮从 `metadata_json` 读出 peak/trough/mfe/mae/breakeven_triggered → `4073` `peak_price=float(meta.get("peak_price",0))` 续算 → `4159-4170` 写回 `meta[...]` + `UPDATE trade_records SET metadata_json` + commit。**monitor 进程内存不持有任何关键状态，DB 即状态，重启后第一轮即从 DB 续算** | **C1 的 Playbook 路径实证免疫** → C1 从 P0 降级（详见 β.2） |
| 3 | `position_recovery.py` 活跃调用者 | grep import **零匹配**（scheduler 实际走 `position_monitor.restore:615`）→ **死代码确认** | C4 收敛方向确认：position_monitor.restore 为 SSOT，position_recovery.py 标 deprecated 不删。附带：**C2 的第二实例（position_recovery.py:138-140）同在死代码中，C2 生效点仅 `position_monitor.py:641-643` 一处** |
| 4 | db_pool 线程安全 | `db_pool.py`：`threading.local()` 线程本地连接缓存（95）+ `PRAGMA journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL` + `check_same_thread=False`（145-157） | **线程安全，monitor/EOD 可直接切换**；且 monitor 与 EOD 同在 `_llm_thread` 单线程（见 β.2-D2），切换后甚至无跨线程场景 |
| 5 | E2 类 ghost 保本污染量化 | DB 实证：83 笔 PLAYBOOK_SIM 平仓中 `realized_pnl==0` **0 笔**、`entry_price==exit_price` **0 笔** | **当前零污染**。α 的"稀释毕业判定"现实依据当前不存在；但机制活着（`4207`/`4436` entry_price 兜底路径仍在），ghost 场景（盘后全部报价源失败）迟早触发 → 维持 P1，理由改写为"**趁零污染窗口修复最便宜（无需清洗存量），防未来污染**" |

### β.2 与 α 的关键分歧：6 项定级修正（均附实证）

#### 修正 1：D2（重入保护）——α P0 → β 判定【误报，不修】

α 的论据："一轮执行 >60s → 下一轮重入 → 同一仓位双平"。**该论据在单线程顺序循环模型下不成立**：

- `monitor_playbook_sim_exits`（`scheduler_core.py:1893-1897`）运行在 `_llm_strategy_loop`（`_llm_thread` 单线程，`1489-1492` 启动）的 `phase == TRADING` 分支内。
- while 循环体内**顺序执行**：一轮 monitor 没 return，控制流永远到不了下一轮迭代——"一轮 >60s"的结果只是本轮拉长，不存在"下一轮叠加上来"。重入需要多线程/回调/信号机制，此处均无。
- 相邻 auto_execute 的 300s `_last_run`（1880-1888）是**节流**（循环 60s 一轮，执行限频 5min），不是防重入。monitor 每轮都该跑（60s 节奏是设计意图），无 `_last_run` **不是缺陷**。

**裁定：D2 误报，移出必修。** 残留风险仅一种：未来有人把 monitor 迁移到其他线程做高频化，重入风险复活 → 修法为**文档化**：在 `monitor_playbook_sim_exits` docstring 标注"线程亲和性：仅允许 `_llm_strategy_loop` 单线程调用；迁移线程前必须先加重入保护"。零代码改动。

#### 修正 2：E3（monitor 与 EOD 竞态）——α P1 → β 判定【前提不成立，不修】

- EOD `_force_close_orphaned_sim_positions` 的调用点 `pipeline.py:1882` 位于 `run_playbook_track`（1807），由 `scheduler_core.py:1981` 调用——**同在 `_llm_strategy_loop` 单线程**，且在 `phase == AFTER_HOURS` 分支（1967-1986）。
- 同一迭代 phase 只取一次（1850），TRADING 与 AFTER_HOURS 分支**互斥**；跨迭代顺序执行。**monitor 与 EOD 不可能并发**，E3 竞态前提证伪。
- 时序上 monitor（TRADING 末轮平仓）→ `_close_trade_record` 写入 exit_time → EOD（AFTER_HOURS）查询"未平仓"时已查不到该仓 → 天然无重复。

**裁定：E3 移出必修，与 D2 同按"线程亲和性文档化"处理。** trade_records 的无条件 UPDATE（3257-3267）在单写者模型下无并发冲突。

#### 修正 3：D3（事务边界）——α P1 → β 判定【大部分误报，不修】

- 实证 `_close_trade_record` 写序：trade_records UPDATE（3257-3267）→ playbook_instructions UPDATE（3290+）→ ACB/PDT record（3305-3319）→ DELETE open_positions（3327）→ **`conn.commit()`（3333）**，**中间无任何 commit**。
- Python sqlite3 默认隔离级别（legacy 模式）：首条 DML 自动开启隐式事务，直到 commit/rollback；连接关闭时未提交事务自动回滚。故 3257-3327 的全部写入**本就在一个隐式事务内，3333 一次性提交**——α 担心的"trade_records 已平但 open_positions 未删"半提交态**不可能出现**（中间异常 → 回滚 → 下轮 monitor 重试平仓，幂等）。
- 唯一真实缺口（α 已注）：metadata 更新（4282-4284）在调用 `_close_trade_record` **之前**单独 commit → metadata 与平仓记录不在同一事务。但该缺口无害：metadata（mfe/mae 终值）会被下一轮重算覆盖，幂等。

**裁定：D3 不修**（α 的"无 BEGIN...COMMIT"判断源于对 Python sqlite3 默认事务语义的误读）。可在函数注释补一句"依赖隐式事务，禁止在中间插入 commit"防回归。

#### 修正 4：C1（重启丢 peak/trough）——α P0 → β 降级【P2，上线后偿还】

- **Playbook 路径实证免疫**（β.1-2）：monitor 无状态，peak/trough 每轮读写 metadata_json，重启不丢。α 自己也标注了此不确定性（"若 monitor 已自洽持久化则降级"）——β 已完成验证，**自洽持久化成立**。
- 主线遗留仓：`position_monitor.py:718` 恢复了 `trail_state`（主线锁利状态主存于 `trail_state_json`，schema 已确认该列存在），丢失的仅是峰值参考。影响真实但有限。
- **裁定：C1 从第一梯队移除，降级 P2**（上线后为主线仓回填 peak/trough，非阻塞项）。

#### 修正 5：C3（恢复 entry_time 丢失）——α P1 → β 判定【大部分误报，不修】

- `position_monitor.py:711-715`：`position.entry_time = datetime.fromisoformat(entry_time_raw)` **已从 DB 恢复 entry_time**；717 行 `now_et()` fallback 仅在 `entry_time_raw` 缺失时触发。
- schema 实证：`open_positions.entry_time` 列存在且 NOT NULL → 正常路径永不走 fallback。

**裁定：C3 移出必修**，残留仅"历史脏数据 entry_time 为空"的边缘情形，不值得代码改动。

#### 修正 6：C2（broker 不可用全仓裸奔）——α P0 → β 降级【P2，修法从简】

三点实证收窄影响面：

1. **Playbook 仓免疫**：playbook 仓被主线恢复显式跳过（`position_monitor.py:683-689`），重启后由 monitor 从 **trade_records DB 查询**（3884+）自重建仓位列表——不依赖 broker 预查询。broker 不可用时 playbook 仓的止损监控循环（取价→规则判断）照常运行，仅平仓执行会失败（下轮重试）。
2. **当前主线影响面为零**：DB 实证（2026-07-19）`open_positions` **0 行**——当前无任何主线遗留/手动仓。C2 的"全仓裸奔"场景当前无对象。
3. **生效点仅一处**（`position_monitor.py:641-643`；另一处在死代码）。

**裁定：C2 降级 P2，修法从简为"broker 预查询失败 → 飞书告警（严重事件可见）+ 保留下次重启重试"**——不采纳 α 的"DB 兜底恢复 + pending 标记"全量方案，理由：给主线新增恢复路径与铁律"主线不应新增功能"有张力，且当前零影响面。**若终裁坚持 α 版修法**，β 提供一个降本发现：`open_positions.sync_status` 列已存在（schema 确认，默认 'synced'）→ "待券商确认"可直接复用该列标记 `pending_broker_confirm`，**零 schema 成本**；退出协同复用现存 `position_sync._is_external_close`（见焦点 2 裁决）。

#### 补充收窄：D1（裸连接）——α P0 → β 降级【P2】

- α 称裸连接"绕过 WAL/无 busy_timeout"。两处事实修正：①**WAL 是数据库级持久属性**（存于 DB 文件头），db_pool 设置一次后，裸连接同样读写于 WAL 模式——"绕过 WAL"不成立；②Python `sqlite3.connect` **默认 timeout=5.0s**，与 db_pool 的 `busy_timeout=5000ms` 等值——锁冲突时同样等待 5s 才报 `database is locked`。
- 真实残留差异仅 `synchronous`（裸连接默认 FULL，更慢更安全）与连接不复用。风险形态：极端并发下本轮 monitor 抛 `OperationalError` → 异常隔离（4299-4302）→ 下轮（60s 后）重试。**不丢数据、不双平**。
- **裁定：D1 降级 P2**。统一走 db_pool 消双轨仍值得做（与 D4 合并，上线后），但不是 P0。α 的"三件套同批上线不可拆散"在 D2/D3 证伪后失去前提。

#### 补充收窄：S2（兜底基准）——α P1 维持，但问题重新定性

- 实证：`capital_safety.py:124` 读 `capital.fixed_amount`（yaml:86 = **82000**，默认 82_000），`132` 的 100_000 兜底仅在**配置读取异常**时触发；`_resolve_sizing_base` 读 `playbook.risk_base_capital`（yaml:1236 = **82000.0**）。**当前两口径数值一致**（82K=82K），α 描述的"100K vs 82K 矛盾"仅在 capital_safety 配置读取失败的罕见窗口成立。
- **真实风险是双 key 漂移**：`capital.fixed_amount` 与 `risk_base_capital` 是两个独立配置项，运维改一个忘改另一个 → 静默分叉。
- **裁定：维持 P1，修法调整**——不是"100K→82K 改值"，而是 SSOT 收敛：①`risk_base_capital` 标注派生自 `capital.fixed_amount`（config 加载时未显式覆盖则跟随）；或②启动校验两 key 不一致即 warning。防漂移优先于改当前值。

### β.3 α §6 五焦点裁决

**焦点 1：R1b"无条件 fail-close"是否过激？→ 裁决：不过激，采纳 α 方案，无需 N 次熔断退化。**
β 补充决定性论据：R1b 的 deployed 查询（`pipeline.py:2313-2317`）是**本地 SQLite 只读查询**，不是 broker API 网络调用。本地只读查询失败 = DB 损坏/锁死级严重事件，不是"broker 短暂抖动"——"连续 N 次才熔断"是为网络调用设计的模式，对本地查询是过度设计。且 auto_execute 每 5 分钟一轮，fail-close（`_max_deploy=+inf` 视为预算已用满）的代价 = 跳过本轮、下轮自愈，不是"全停"。**宁可误拦一轮，不可无上限开仓**（$388K/$82K 事故同类根因，pipeline.py:2306 注释为证）。另确认 `_bp_limit` 路径（2339-2342）已有静态兜底（0.8×base），预算上限本身不失效，无需改动。

**焦点 2：C2"待券商确认"态如何退出？→ 裁决：复用现存 `_is_external_close`，无需新建机制。**
实证：`position_sync.py:1352-1368` 外部平仓检测已存在——当日订单成交中"LONG 持仓+SELL 成交 / SHORT 持仓+BUY 成交"→ 用实际成交价创建正确 trade record + 通知。broker 恢复后对账三分支：(a) broker 有仓 → 确认转正常监管；(b) broker 无仓 + 当日有平仓成交 → `_is_external_close` 识别外部平仓、按实价记账；(c) broker 无仓 + 无成交 → ghost，标记清理。另实证 `position_sync.py:1123` 跳过 playbook 仓 → 该机制专服务主线仓，与 playbook 自管互不干扰。**β 与 α 共识**：若 C2 采纳 DB 兜底，退出协同零新代码。

**焦点 3：C1 的 Playbook 路径是否真受影响？→ 已实证闭环：不受影响。**
见 β.1-2 与修正 4。monitor 无状态设计（每轮 `3933-3938` 读 / `4073` 续算 / `4159-4170` 写回+commit），DB 即状态，重启安全。**C1 从 P0 降级 P2，α 第一梯队相应收缩。**

**焦点 4：sizing 公共函数（F1）回归面？→ 裁决：必须先补 sizing 单测再重构，且公共函数采用"参数全开放"契约。**
β 实证触发面：近 14 天已执行 62 笔中 **5 笔（8.1%）走兜底分支**——非罕见路径，每月约 10 笔绕过水位/regime 乘数，正在真实发生；指令层面近 14 天 177 条中 90 条（51%）`position_size<=0/NULL`，一旦触发全部走兜底，潜在面过半。两路径存在**三个语义差异点**（非机械抽取）：①主路径有 `water_mult×regime_multiplier`（instruction_generator.py:404-416），兜底无；②兜底有 `conf_factor`（默认关闭，2555-2557）与 LONG 模型分歧降仓 50%（2565-2567），主路径无。合并契约裁定：**公共函数 `_compute_position_size(risk_base, risk_pct, sl_distance, max_pos_pct, *, regime_mult=1.0, water_mult=1.0, conf_factor=1.0)` 参数全开放、函数体只做确定性计算**；主路径调用传 conf_factor=1.0（保持现状语义），兜底调用补传 regime_mult/water_mult（本次修复点）。两路径各自行为变化最小化。单测先行清单：①兜底现状公式锁定；②主路径乘数链锁定；③max_pos_pct 截断；④分歧降仓 50%；⑤统一后兜底含乘数的新行为。

**焦点 5：D1-D3 上线顺序？→ 裁决：前提已被证伪，"并发一揽子"解散。**
D2 误报（修正 1）、D3 大部分误报（修正 3）、E3 前提不成立（修正 2）——α"同批上线不可拆散"的必要性不复存在。剩余 D1 降级 P2（修正 6 补充），与 D4 合并为"连接双轨统一"上线后偿还。**β 承认 α 方向上的谨慎合理（并发缺陷排查成本极高），但实证不支持 P0 定级**；若终裁仍担忧，最低成本止血 = monitor/EOD 换 `safe_sqlite_connect` 一行级改动，可独立先行，无需捆绑。

### β.4 β 修正后的修复方案（对照 α 三梯队）

#### 第一梯队：上线前 P0（β 版，5 项 → 2 项）

1. **F1 — sizing SSOT 公共函数**（维持 α）："参数全开放"契约 + 先补 5 条单测（焦点 4）。
2. **R1b — 预算门无条件 fail-close**（维持 α）：异常时 `_max_deploy=+inf`；β 补论据（本地查询失败≠网络抖动，焦点 1）。

~~C2~~（→P2）、~~C1~~（→P2）、~~并发一揽子 D1+D2+D3~~（解散）。

#### 第二梯队：上线前 P1（β 版，5 项）

3. **R1a/R1c — 统一 `risk.preflight_fail_mode`**（维持 α，SIM 默认 open，live 前翻 closed）。
4. **R2 — 连续 3 轮取价失败 symbol 飞书告警**（维持 α，定位已修正至 `pipeline.py:3950-3951`，不改跳过行为）。
5. **E2 — 无报价 ghost 标 `void` 不计统计**（维持 α P1；β 实证当前零污染，趁窗口修复最便宜）。
6. **S3 — 日额度 SQL 加 `source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')`**（维持 α；β 注：与同函数内 R1b 的 2315 过滤口径对齐，消除同函数双口径）。
7. **S2 — 双 key SSOT 收敛**（维持 P1，修法改为派生/启动校验，防漂移，见修正 6）。

~~C3~~（误报移除）。

#### 第三梯队：上线后偿还（P2/P3）

8. F2（P2，维持）、9. F3（P2，`round()` 语义——β 补闭环：`dual_broker.py:290` 已有 `<0.5 → 跳过` 前置，round 残留 <0.5 尾差由下轮该前置识别，自洽）、10. C1 主线仓 peak/trough 回填（P2，β 降级）、11. C2 broker 不可用告警+重试（P2，β 降级+修法从简）、12. C4 position_recovery.py 标 deprecated（P2，维持，死代码已实证）、13. D1+D4 连接统一走 db_pool（P2，β 降级合并）、14. S1 口径文档化（P2，β 实证闭环）、15. E1/E5 报表口径（P2/P3，维持）、16. E4 bid/ask（P3，维持）。

#### 不做项（α 6 项全保留 + β 新增 4 项）

| 项 | 理由 |
|----|------|
| S4 改成交量口径 | α：保守方向，B3 告警已暴露（β 同意） |
| S1 ORB 口径修复 | α：ORB 已停用（β 同意） |
| F2 静默 dealt_qty 覆盖 | α：掩盖入场记账 bug（β 同意） |
| F3 用 ceil() | α：超卖风险（β 同意，且 290 前置已兜 <0.5） |
| R2 改"不跳过" | α：单仓阻断整批更危险（β 同意） |
| 删 position_recovery.py / 主线 | 铁律不删主线（β 同意，标 deprecated） |
| **D2 加重入锁**（β 新增） | 误报：单线程 + phase 互斥无重入；改文档化线程亲和性 |
| **D3 包显式事务**（β 新增） | 误报：Python 隐式事务已保证原子性；改注释防回归 |
| **E3 竞态修复**（β 新增） | 前提不成立：monitor 与 EOD 同线程互斥分支 |
| **C3 恢复 entry_time**（β 新增） | 已存在（position_monitor.py:711-715），仅脏数据边缘情形 |

### β.5 双方共识清单（α+β 已一致，可直接进入终裁执行）

1. F1 P0：sizing SSOT 公共函数（契约细节按焦点 4 裁决）。
2. R1b P0：预算门异常 `_max_deploy=+inf` 无条件 fail-close，不配 N 次熔断。
3. R1a/R1c P1：统一 `risk.preflight_fail_mode` 配置治理，SIM 默认 open。
4. R2 P1：取价失败连续 3 轮告警，不改跳过行为。
5. E2 P1：ghost 无报价标 void（当前零污染，防未来）。
6. S3 P1：日额度加 source_type 过滤。
7. S2 P1：双 key 收敛（派生或启动校验）。
8. C4：position_recovery.py 死代码标 deprecated 不删。
9. S1：闭环降级 P2 文档化（无 Playbook 活跃调用者）。
10. F2/F3/E1/E4/E5/S4：维持 α 定级与语义裁定。

### β.6 留给终裁轮的待决项（每条可验证）

1. **确认 β 对 D2/D3/E3 的误报判定**（β 核心分歧，决定"并发一揽子"存废）。验证锚点：`scheduler_core.py:1850`（phase 单取）+ `1893-1897`（monitor，TRADING 分支）+ `1978-1986`（track，AFTER_HOURS 分支）+ `1489-1492`（单 `_llm_thread`）；D3 锚点 `pipeline.py:3257→3290→3327→3333`（中间无 commit）+ Python sqlite3 legacy 隔离级别语义。**若终裁推翻 β，回退 α 三件套同批方案。**
2. **确认 C1 降级 P0→P2**（锚点：`pipeline.py:3933-3938` / `4073` / `4159-4170` 无状态闭环）。
3. **确认 C2 降级 P0→P2 + 修法从简**（锚点：`open_positions` 0 行实证 / playbook 仓 `683-689` 跳过 / 生效点仅 `641-643`）。若终裁坚持 α 版 DB 兜底：复用 `sync_status` 列标 `pending_broker_confirm`（零 schema 成本）+ `_is_external_close` 退出协同，**不得新增第二套恢复函数**。
4. **确认 F1 公共函数契约**：`conf_factor` 与 LONG 分歧降仓是否仅限兜底路径（β 建议：参数全开放、主路径传中性值，保持两路径现状语义）。
5. **确认 S2 收敛方式二选一**：config 派生（`risk_base_capital` 跟随 `capital.fixed_amount`）vs 启动校验（不一致 warning）。
6. 执行前最后一道实证：F1 修复合入后，复盘下一周 PLAYBOOK_SIM 新平仓的 position_size 分布，确认兜底分支仓位含水位乘数（回归验证）。

---

## 终裁报告（Agent-Ω，第四轮，2026-07-19）

> 方法：不复用 α/β 任何一方结论。对 α↔β 的核心分歧（并发簇 D1-D3/E3、恢复簇 C1/C2/C3）用源码亲验 + DB 实证做第三方独立复核，再对 β.6 六项待决项逐条终裁，产出唯一可执行清单。

### Ω.0 终裁总评

**β 的核心反驳成立，α 在并发簇上系统性高估，应予纠正。** Ω 独立复验确认：

1. **单线程顺序模型属实**：`monitor_playbook_sim_exits` 唯一调用点 `scheduler_core.py:1895`（`_llm_thread` 的 TRADING 分支）；`_force_close_orphaned_sim_positions` 唯一调用点 `pipeline.py:1882`（`run_playbook_track`，AFTER_HOURS 分支）。`phase` 每轮单取（1850），TRADING/PRE_MARKET/AFTER_HOURS 是 `if/elif` 互斥分支，TRADING 末尾 60s sleep+continue（1901-1905）顺序执行。**同一线程不可能自我重入，互斥分支不可能并发。**
2. **Python sqlite3 隐式事务属实**：`_close_trade_record` 写序 trade_records UPDATE(3257)→playbook_instructions UPDATE(3290)→DELETE open_positions(3327)→`conn.commit()`(3333)，**中间零 commit**。默认隔离级别下首条 DML 开启隐式事务，3333 一次性提交，半提交态不可能出现。
3. **monitor 无状态闭环属实**：`pipeline.py:4159-4170` 每轮写回 peak/trough/breakeven 到 metadata_json+commit，3933-3938 读取。DB 即状态，重启第一轮即续算。
4. **DB 实证**：open_positions=0 行、playbook_sim 未平仓=0、E2 ghost 保本（realized_pnl=0 / entry==exit）=0 笔。
5. **S2 配置实证**：`fixed_amount: 82000`（yaml:86）== `risk_base_capital: 82000.0`（yaml:1236），当前相等。

**结论**：α 的"并发一揽子 P0 不可拆散"前提（多线程并发 + monitor 依赖内存态）被双重证伪。Ω 裁定 β 修正成立，P0 从 α 的 5 项收敛为 **2 项（F1、R1b）**。

### Ω.1 并发簇终裁（D1/D2/D3/E3）

| 项 | α | β | Ω 终裁 | 依据 |
|----|----|----|--------|------|
| **D2 重入保护** | P0 | 误报 | **误报，不修代码** | 单调用点+单线程，无重入路径。改 docstring 标注线程亲和性（防未来迁移线程）|
| **E3 monitor/EOD 竞态** | P1 | 不修 | **不修** | TRADING/AFTER_HOURS 互斥分支，时序天然隔离 |
| **D3 事务边界** | P1 | 不修 | **不修** | 隐式事务已原子；补注释"禁止中间插 commit"防回归 |
| **D1 裸连接** | P0 | P2 | **P2** | WAL 库级持久 + 默认 timeout=5s；真实跨线程并发（_monitor_loop/_scan_loop）由 WAL 处理 |

**Ω 补充（α/β 均未充分展开的一点）**：真实存在的跨线程 DB 并发是 `_llm_thread`（Playbook 写 trade_records/open_positions）↔ `_monitor_thread`（主线 monitor_loop 写同表）↔ `_scan_thread`。这三者**确实并行**，但 WAL 模式支持单写多读、busy_timeout=5s 等待锁。残留风险形态：极端并发下某轮 monitor 抛 `OperationalError: database is locked` → 异常隔离（pipeline.py:4299-4302）→ 下轮 60s 后重试，**不丢数据、不双平**。故 D1 是性能/健壮性优化（P2），非正确性缺陷。**D1+D4 合并为"连接双轨统一走 db_pool"上线后偿还**，β 裁定采纳。

### Ω.2 恢复簇终裁（C1/C2/C3）

| 项 | α | β | Ω 终裁 | 依据 |
|----|----|----|--------|------|
| **C1 重启丢 peak/trough** | P0 | P2 | **P2** | Playbook 路径无状态闭环实证免疫（Ω 复验 4159-4170）；仅主线遗留仓受影响，当前 open_positions=0 |
| **C2 broker 不可用裸仓** | P0 | P2 | **P2 + live 复审条件** | Playbook 仓从 trade_records DB 重建（不依赖 broker）；当前主线仓=0。**但 Ω 附加：live 转换时必须复审**（届时可能有手动/主线仓） |
| **C3 恢复 entry_time** | P1 | 误报 | **误报，不修** | position_monitor.py:711-715 已从 DB 恢复；fallback 仅脏数据边缘 |

**Ω 对 C2 的附加裁定（修正 β 但保留其降级结论）**：β 的 P2 降级在**当前 SIM 期 + open_positions=0** 下完全成立。但 β 的"修法从简为告警+重试"对 **live 期不足**——live 时若存在手动/主线仓且 broker 短暂不可用，"空仓等待"=真实持仓裸奔。故 Ω 裁定：
- **SIM 期（现在）**：采纳 β 简版（飞书告警 + 下次重启重试），P2。
- **live 转换门禁**：将"C2 DB 兜底恢复（复用 sync_status 标 pending_broker_confirm + _is_external_close 退出协同）"列为 **live graduation 的前置检查项**（写入 B5 graduation gate 的附加 checklist），而非现在做。这样既不过度工程（SIM 期零影响面不投入），又不留 live 隐患（毕业前必须闭环）。

### Ω.3 β.6 六项待决项终裁

1. **D2/D3/E3 误报判定 → ✅ 确认 β 正确。** Ω 独立复验单线程模型（Ω.0-1）+ 隐式事务（Ω.0-2）全部成立。并发一揽子解散。**回退条件**：若未来 monitor 迁移出 `_llm_thread`（如独立高频线程），D2 重入保护必须复活——故 docstring 线程亲和性标注是必须的（零代码，但强约束）。

2. **C1 降级 P0→P2 → ✅ 确认。** monitor 无状态闭环实证（4159-4170 写回 / 3933-3938 读取），Playbook 路径重启安全。

3. **C2 降级 P0→P2 + 修法从简 → ✅ 确认降级，但修正修法（见 Ω.2）**：SIM 期简版（告警+重试）；live 兜底方案列为 graduation 前置检查项，复用 sync_status 列（零 schema 成本）+ _is_external_close（零新代码），**不得新增第二套恢复函数**。

4. **F1 公共函数契约 → ✅ 采纳 β "参数全开放"契约。** 终裁锁定签名：
   ```python
   def _compute_position_size(risk_base, risk_pct, sl_distance, max_pos_pct, *,
                              regime_mult=1.0, water_mult=1.0, conf_factor=1.0) -> int
   ```
   - 函数体只做确定性计算（risk_amount = risk_base×risk_pct%×regime_mult×water_mult×conf_factor → /sl_distance → max_pos_pct 截断）。
   - 主路径（instruction_generator）调用传 `conf_factor=1.0`（保持现状，主路径本无 conf 加权）。
   - 兜底路径（pipeline）调用补传 `regime_mult`/`water_mult`（本次修复点），保留其 conf_factor + LONG 分歧降仓 50% 逻辑（在调用前算好 conf_factor 传入）。
   - **LONG 分歧降仓 50% 留在兜底路径调用侧**（不进公共函数），因为主路径无此语义。
   - **单测先行**（β 焦点 4 清单）：①兜底现状公式锁定 ②主路径乘数链锁定 ③max_pos_pct 截断 ④分歧降仓 50% ⑤统一后兜底含乘数新行为。

5. **S2 收敛方式二选一 → Ω 裁决：启动校验（不一致 warning）优于 config 派生。** 理由：派生（risk_base_capital 隐式跟随 capital.fixed_amount）引入隐式依赖，运维看不到第二个值从哪来，违反"显式可观测"；启动校验（ConfigValidator 检查两 key 不一致即 warning）更显式、可回滚、零运行时开销。当前 82K=82K 无需改值，仅加校验防漂移。

6. **F1 修复后回归验证 → ✅ 采纳，列为执行清单最后一道门。** F1 合入后复盘下一周 PLAYBOOK_SIM 新平仓的 position_size 分布，确认兜底分支仓位含水位乘数（YELLOW/ORANGE 时应缩小）。

### Ω.4 最终收敛执行清单（唯一权威版，α/β/Ω 三方收敛）

#### 第一梯队：上线前 P0（2 项，阻塞上线）

1. **F1 — sizing SSOT 公共函数**：按 Ω.3-4 契约抽取，先补 5 条单测再重构，兜底补 regime_mult×water_mult。
2. **R1b — 预算门无条件 fail-close**：buying_power 查询异常时 `_max_deploy=+inf`（视为预算用满，拦后续所有单），不配 N 次熔断（本地 SQLite 只读查询失败=DB 级严重事件，非网络抖动）。

#### 第二梯队：上线前 P1（5 项）

3. **R1a/R1c — 统一 `risk.preflight_fail_mode`**：配置驱动（open|closed），SIM 默认 open，live 前翻 closed。
4. **R2 — 取价失败连续告警**：连续 3 轮取价失败的 symbol 飞书告警（定位 pipeline.py:3950-3951，不改跳过行为）。
5. **E2 — ghost 无报价标 void**：趁当前零污染窗口修复（防未来盘后全报价源失败污染毕业判定）。
6. **S3 — 日额度 source_type 过滤**：SQL 加 `IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')`，与同函数 R1b 的 2315 口径对齐。
7. **S2 — 启动校验防漂移**：ConfigValidator 检查 `capital.fixed_amount` vs `risk_base_capital` 不一致即 warning。

#### 第三梯队：上线后偿还（P2/P3）

8. F2 PnL 用 dealt_qty 重算（P2，B3 告警已覆盖）
9. F3 int→round 碎股（P2，dual_broker.py:290 已有 <0.5 跳过前置，自洽）
10. C1 主线仓 peak/trough 回填（P2，Playbook 路径已免疫）
11. C2 broker 不可用告警+重试（P2 简版；**live 兜底列为 graduation 前置检查**）
12. C4 position_recovery.py 标 deprecated（P2，死代码已实证零调用）
13. D1+D4 连接统一走 db_pool（P2，性能/健壮性）
14. S1 口径文档化（P2，无 Playbook 活跃调用者）
15. E1/E5 报表口径重算（P2/P3）
16. E4 SL/TP 用 bid/ask（P3，精度）
17. D2/D3 线程亲和性 + 隐式事务 docstring 标注（P3，零代码防回归）

#### 明确不做项（三方共识）

| 项 | 理由 |
|----|------|
| D2 加重入锁 | 误报：单线程+phase 互斥无重入（Ω 复验确认）|
| D3 包显式事务 | 误报：Python 隐式事务已原子（Ω 复验确认）|
| E3 竞态修复 | 前提不成立：monitor/EOD 同线程互斥分支 |
| C3 恢复 entry_time | 已存在（711-715），仅脏数据边缘 |
| S4 改成交量口径 | 保守方向，B3 告警已暴露 |
| S1 ORB 口径修复 | ORB 已停用 |
| F2 静默 dealt_qty 覆盖 | 掩盖入场记账 bug |
| F3 用 ceil() | 超卖风险 |
| R2 改"不跳过" | 单仓阻断整批更危险 |
| 删 position_recovery.py/主线 | 铁律不删主线，标 deprecated |

### Ω.5 执行前最后实证门（每条可验证，承接 β.6 + Ω 补充）

1. F1 单测 5 条全部通过后再合入重构（焦点 4）。
2. F1 合入后复盘下一周 PLAYBOOK_SIM 新平仓 position_size 分布，确认兜底分支含水位乘数（Ω.3-6）。
3. R1b 修复后注入 buying_power 查询异常，确认 `_max_deploy=+inf` 拦截后续所有单。
4. E2 修复后注入"无报价 ghost"场景，确认 outcome=void 且不计入胜率/净PnL。
5. S2 启动校验：临时改 risk_base_capital≠fixed_amount，确认启动 warning 触发。
6. **live 转换前**：C2 DB 兜底恢复闭环验证（复用 sync_status + _is_external_close），作为 graduation 附加门禁。

### Ω.6 三方收敛状态

- α 起草 20 项裁定 → β 修正 6 项定级（D2/E3/D3/C1/C2/C3）+ 解散并发一揽子 → Ω 独立复验**全部确认 β 修正成立**。
- **最终 P0 = 2 项（F1、R1b）**，P1 = 5 项，其余 P2/P3 或不修。
- 三方对 F1 契约、R1b 无条件 fail-close、S2 启动校验、C2 live 复审条件达成一致。
- **本轮辩论收敛，可进入执行阶段。** 执行按 Ω.4 清单逐项落地，每项完成后对照 Ω.5 实证门验证。
