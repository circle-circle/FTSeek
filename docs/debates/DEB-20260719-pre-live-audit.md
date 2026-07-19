# Agent-vs-Agent Debate: 上线前已知 Bug / 风险 全量清账与修复方案定稿

---

# 议题：在实盘上线前，把已发现的所有 bug 与风险修复补齐，并保证框架干净整洁、无更多包袱与冲突。请审查已完成修复是否正确、无过度，并对开放项给出一致的取舍结论。

## 状态
- [x] 第一轮：hotfix agent 起草（A/B/C 部分）
- [x] 第二轮：复核 Agent-α 源码审查（D 部分）
- [x] 第三轮：辩论 Agent-β 独立验证 + 数据实证（E 部分）
- [x] 第四轮：终裁 Agent-Ω 分歧收敛 + 执行清单（F 部分）
- [ ] 执行：按 F.6 清单逐项落地（每项完成后打勾）

## 阅读须知（给复核 agent）

- 本文分三部分：**A. 已修复项**（需验证：是否正确、是否过度、是否引入回归）；**B. 待决策开放项**（需博弈：做/不做/怎么做）；**C. 框架整洁度长期项**（需定优先级）。
- 项目铁律：**Playbook 是唯一下单/管理管线**（入场 `run_playbook_auto_execute` → 出场 `monitor_playbook_sim_exits`，均经 `DualBrokerAdapter`）。主线 `signal_pipeline → order_executor` 已冻结新仓，仅服务遗留/手动/紧急平仓。**任何修复不得新增分叉或双重管理**。
- 当前处于 **SIM 数据积累期**（`daily_notional_cap_pct=0.60` 刻意调高以攒样本），策略真实 edge 尚未被验证。
- 所有 A 部分改动**已提交未 push**，分支 `rfc-060-phase1-hotfixes`，commit：`da619781`、`3e298c1f`、`7f9eb0e2`。

---

## A. 已完成修复（请复核：正确性 / 是否过度 / 回归风险）

### A1. 风控接线 — 让"摆设级"风控真正生效 (commit da619781)
**问题**：`absolute_circuit_breaker`(ACB) / `pdt_guard` 的记录函数在 Playbook 路径**从不被调用** → 连亏/日亏/PDT 计数恒为 0，熔断与 PDT 限制形同虚设。
**修复**：
- `playbook_service.py`：开仓前调 `ACB.can_open_new_position("playbook")` 硬门（连亏5/日亏$2K/时亏$1K），超限拒开。
- `pipeline.py::_close_trade_record`：平仓后调 `ACB.record_trade_result` + `PDT.record_close`（幽灵/孤儿仓不计）。
**无双重管理论证**：仓位归属互斥（Playbook→SQLite `open_positions`，主线→内存 `position_store`）；`record_trade_result` 零其他生产调用者；风控模块均为共享单例。
**复核点**：熔断 fail-open（异常放行）是否合理？还是应 fail-close？

### A2. IC 符号双重调整 bug (commit da619781)
**问题**：`signal_ic_tracker.py` 全局/每日/分策略三处，对 SHORT 的 strength 取负；但 `fwd_ret` 回填时已按方向调整（`_calc_fwd`: SHORT 用 `t0/close_tn-1`），等于**调整两次 → 做空信号 IC 系统性反号**。
**修复**：三处统一用 raw strength（仅跳过 NEUTRAL）。
**影响面警告**：**此 bug 修复前，历史 IC 中所有做空相关数字有一部分是反的**，基于其调出的因子/参数可信度需重估。请复核 agent 评估：是否需要对历史 IC 做一次性回填重算？

### A3. 退出影子 `_pb_cfg` 未定义 (commit 3e298c1f 内，pre-commit 钩子捕获)
**问题**：`pipeline.py:4126` `_pb_cfg` 在 `monitor_playbook_sim_exits` 作用域内未定义，被 `except` 静默吞掉 → `time_exit_hours` 影子逻辑一直失效。
**修复**：块内本地 `get_playbook_config()`。

### A4. PDT 时区错判 (commit 3e298c1f)
**问题**：`pdt_guard.py` 三处 `date.today()`（服务器本地日期，非 ET）。非 ET 服务器上，美股尾盘的日内往返被误判"隔夜" → 漏计 PDT 日内交易 → 有券商锁户合规风险。
**修复**：改用 `now_et()` / `to_et()`（滚动窗口、记录、往返判定三处）。
**复核点**：`to_et` 对 naive datetime 假设为 CST（项目约定）——若 `entry_time` 实为 ET-aware，`to_et` 走 `astimezone` 正确；若被写成 naive ET，则会被误当 CST。请核对 `trade_records.entry_time` 的实际写入格式。

### A5. 部分成交按实际量记账 (commit 3e298c1f)
**问题**：`submit_order` 丢弃 `dealt_qty`，`trade_records.quantity` / `open_positions.size` 按**下单量**记账。下单100成交60 → 幽灵40股，直到出场才自愈。
**修复**：
- `dual_broker.py`：新增 `_query_fill` 返回 `(price, dealt_qty)`（同 order_id 多笔成交累加），`submit_order` 返回 `filled_qty`；`_query_filled_price` 保留为薄封装（向后兼容）。
- `playbook_service.py`：`_effective_qty` = 实际成交量（0 时退回下单量兼容 SIM 均价=0），trade_records / open_positions / 回滚 / 注册 / STOP / 推送全部改用。
**复核点**：SIM 环境 `history_deal_list_query` 有延迟（已知~29s），`_query_fill` 只轮询 3×0.5s。若查不到 → `filled_qty=0` → 退回下单量 → **部分成交在 SIM 下仍可能记成全量**。是否可接受（SIM 期）？live 是否需要更长轮询或事后对账补正？

### A6. 下单幂等抢占 (commit 3e298c1f)
**问题**：`executed=1` 在 broker 调用**之后**翻转，且非原子；调度重试/重启中途 → 同一指令重复下单。
**修复**：
- `db.py`：新增 `try_claim_execution`（原子 `UPDATE...WHERE executed=0`，rowcount 判定）+ `release_execution_claim`。
- `playbook_service.py`：下单前抢占，预成交失败释放；Order 带 `client_order_id=pb_{id}_{env}`。
**复核点（重要）**：`client_order_id` 目前**只落在 Order 对象，未透传给 Futu `place_order`**（Futu 有 `remark` 字段未用）。即抢占防的是"本系统重复调用"，防不了"抢占成功但 broker 下单后进程崩溃、重启后 executed 已=1 但无 trade_record"的**孤儿单**。是否需要：(a) 透传 client_order_id 到 broker 做真幂等；(b) 复用已存在但仅接在遗留 `ExecutionEngine` 的 `IdempotencyManager`？还是 SIM 期 A6 已够？

### A7. 下单前报价健康门 (commit 7f9eb0e2)
**问题**：Playbook 全是市价单，无任何盘前价格质量门；`cost_model`(net_edge) 仅用于打分不拦单，`SlippageController` 在此路径是死代码。
**修复**：`playbook_service.py` 复用 `check_quote_health`（复用已抓 `_arr_ctx`，不额外调 API）：**仅在 bid/ask 齐备时**拦截 spread>5% / 过期>60s / 买卖倒挂；缺 bid/ask（薄盘/盘前）不拦；fail-open。阈值入 `config.py`（`pretrade_quote_gate_enabled/max_spread_pct/max_quote_age_sec`），可一键回滚。
**复核点**：(a) 5% spread、60s 过期阈值是否合理？(b) **已核实的交互 bug**：Futu 报价存 `time.time()`（float，过期门有效）；但 Finnhub fallback 在 `finnhub_provider.py:261` 存 **ISO 字符串**，而 `check_quote_health` 做 `time.time() - float(ts)` —— 对 ISO 字符串 `float()` 抛异常，被 `except: pass` 吞掉 → **过期门对 Finnhub 数据静默失效（空门）**。即 **C3 是 A7 过期保护在 fallback 数据上真正生效的前置条件**。spread 门不受影响（不依赖 timestamp）。

### A8. dual_broker DST 修正 (commit 7f9eb0e2)
**问题**：`_query_fill` 查当日成交用硬编码 `timezone(timedelta(hours=-4))`；冬令时(EST=-5)错标 ET 日期 → 查不到当日成交。
**修复**：改用 `now_et()`（DST 正确）。

---

## B. 待决策开放项（请博弈：做 / 不做 / 上线前 vs 上线后）

### B1. 市价单 vs 限价单 —【策略级，高争议】
现状：Playbook 全市价单（`price=0.0`），A7 只是软拦截坏价，不改变"以任意价格成交"的本质。快速行情/宽 spread 下滑点不可控。
选项：(a) 保持市价单 + A7 门（简单，SIM 够用）；(b) 改限价单 + `limit_order_buffer_ticks`（已有配置 `smart_execution.limit_order_buffer_ticks=2`）+ 未成交处理逻辑（复杂，引入撤单/追单状态机）。
**博弈问题**：上线前是否必须上限价单？还是 live 小仓验证阶段先市价单可接受？

### B2. IC 历史数据重算 —【数据可信度】
A2 修复后，历史 IC 中做空部分曾算反。是否需要一次性回填重算 `signal_ic_tracker` 历史，避免用错误 IC 继续指导因子权重/门槛？成本 vs 收益？

### B3. SIM→live 部分成交对账 —【承接 A5 复核点】
live 环境是否需要独立的"成交量事后对账"任务（reconciler 已存在，是否已覆盖入场量偏差）？还是靠出场自愈够？

### B4. Broker 级真幂等 —【承接 A6 复核点】
是否透传 `client_order_id`/`remark` 到 Futu，或复用 `IdempotencyManager`，以防"抢占后崩溃"的孤儿单。SIM 期风险等级？

### B5. daily_notional_cap_pct=0.60 的退出条件 —【数据积累期治理】
攒样本调高到 60% 是临时措施。定义：攒够多少样本 / 满足什么 IC 或胜率门槛后，回调到保守值？避免"临时"变"永久"。

---

## C. 框架整洁度长期项（请定优先级：上线前必须 vs 上线后偿还）

### C1. 时区 SSOT 碎片化
`now_et` 有 4 处实现（`utils/datetime_utils.py`、`utils/timezone.py`、`web_dashboard/data_service.py`、`data/subscription_manager.py` 内联）；`utils/timezone.py` 的 pytz fallback 硬编码 `timedelta(-4)` 不含 DST；仍有 16+ 处 `timezone(timedelta(hours=-4))` 简化实现（见历史 DEB-20260627-tz-ssot-audit）。
**建议**：择一为 SSOT（倾向 `utils/timezone.py`，但需硬化 fallback），其余 re-export，逐步替换。**问题：上线前收敛还是上线后？全量替换风险 vs 收益？**

### C2. 主线 order_executor 的历史包袱
主线冻结但代码仍在，共享退出侧/价格/对账设施，双份时间处理逻辑。长期是认知负担与潜在冲突源。
**建议**：不删（AGENT.md 明确仍服务遗留/紧急），但需明确边界文档 + 定期审计"主线是否真的零新仓"。**问题：是否需要运行期断言/监控证明主线零下单？**

### C3. Finnhub 时间戳掩盖上游过期
`finnhub_provider.py:261` 把 `timestamp` 盖成本地 `datetime.now()` → 下游任何过期检测（含 A7 报价门）对 Finnhub fallback 数据**失效**。
**建议**：改用上游 `t` 字段。低成本、直接影响 A7 有效性。**优先级？**

### C4. 数据健康门覆盖面
`check_quote_health` 现仅接在 A7（入场）与 ORB（已停用）。出场侧（SL/EOD 用 `quote_fallback`）、主线 `_get_current_price` 仍无过期检查。
**建议**：出场侧是否也需报价门？还是出场"能平就平"优先于价格质量？

### C5. mfe_pct/pnl_pct 量纲差 100x
`pipeline.py` 已注释文档化（pnl_pct=ratio、mfe_pct=percentage）。**博弈：仅文档化够吗？还是应统一量纲根治？** 统一的回归风险 vs 长期误用风险。

---

## 复核 agent 需产出的结论

1. **A 部分**：逐项确认 正确 / 需改 / 过度（应回退）。特别聚焦 A6（幂等边界）、A5（SIM 部分成交）、A7（timestamp 可信度依赖 C3）。
2. **B 部分**：每项给 做 / 不做 / 时机（上线前 vs 后），并说明理由。
3. **C 部分**：排出"上线前必须"清单 vs "上线后偿还"清单。
4. **一致性检查**：所有建议是否违反"Playbook 唯一管线、无双重管理、不新增分叉、不过度工程"铁律。
5. 输出一份**排序后的最终修复方案**（含明确的不做项及理由）。

## 原则约束（所有 agent 必须遵守）
- 复用现有模块，不造轮子、不新增分叉。
- 不过度工程：不为假设需求设计，不加不可达分支的防御。
- 数据积累期：edge 未被真实数据证明前，优先"让数据可信 + 风控可靠"，而非加策略功能。
- 改动可回滚、可配置化优先。

---

## D. 复核 Agent 分析报告（Agent-α，2026-07-19）

> 以下由复核 agent 基于源码逐项审查后产出，供辩论 agent 博弈。

### D.1 A 部分逐项复核

#### A1. 风控接线 — ✅ 正确，无过度，回归风险低

**代码验证**：
- 开仓硬门：`playbook_service.py:701-708` 在 `_execute_via_dual_broker` 入口调用 `ACB.can_open_new_position("playbook")`，异常 fail-open（`except: 放行`）。
- 平仓记录：`pipeline.py:3299-3319` 在 `_close_trade_record` 内调用 `ACB.record_trade_result` + `PDT.record_close`，幽灵/孤儿仓通过 `ORPHAN/PHANTOM/GHOST` 关键字排除。
- 无双重管理：ACB/PDT 均为共享单例，`record_trade_result` 无其他生产调用者，仓位归属互斥。

**复核结论**：
- 正确性：✅ 接线完整，开仓拦截 + 平仓喂数闭环。
- 过度性：✅ 无过度，是"让摆设级风控生效"的最小改动。
- 回归风险：低。纯增量调用，异常 fail-open 不阻断主流程。
- **fail-open vs fail-close 裁定**：SIM 期 fail-open 合理（风控检查自身异常不应阻断全部交易）。**建议 live 前将 ACB 硬门改为 fail-close**（异常时拒绝开仓），因为 live 下"风控失效"的代价远大于"误拦一笔"。PDT 检查同理。此项归入 B 部分跟踪。

#### A2. IC 符号双重调整 — ✅ 正确，影响面需善后

**代码验证**：
- `signal_ic_tracker.py:538-550`（全局 IC）、`567-574`（每日 IC）、`600+`（分策略 IC）三处已统一用 raw strength，仅跳过 NEUTRAL。
- `_calc_fwd`（line 368）确认 SHORT 方向 `fwd_ret = t0/close_tn - 1`，已含方向调整。修复前 strength 再取负 = 双重调整 → IC 反号。

**复核结论**：
- 正确性：✅ 修复逻辑正确。
- 过度性：✅ 无过度，仅删除多余的取负操作。
- 回归风险：修复本身无回归，但**历史 IC 数据已被污染**。
- **善后建议**：见 B2。历史 SHORT 信号的 IC 贡献是反的，基于其调出的因子权重/门槛可信度打折。建议做一次性回填重算（成本可控：`signal_snapshots.db` 数据量有限，重算脚本已有 `backfill_forward_returns.py` 可复用）。

#### A3. `_pb_cfg` 未定义 — ✅ 正确，最小修复

**代码验证**：`pipeline.py:3646-3647` 在 `monitor_playbook_sim_exits` 块内本地调用 `get_playbook_config()`，修复了 `_pb_cfg` 未定义导致 `time_exit_hours` 影子逻辑静默失效的问题。

**复核结论**：正确、无过度、无回归。

#### A4. PDT 时区错判 — ✅ 正确，有一个边界需确认

**代码验证**：
- `pdt_guard.py:18` 导入 `now_et, to_et`。
- `record_day_trade`（line 260）用 `now_et().date()`。
- `record_close`（line 302-304）用 `to_et(entry_time)` 与 `now_et().date()` 比较。
- `_get_rolling_window_trades`（line 125）用 `now_et().date()`。

**复核结论**：
- 正确性：✅ 三处均改用 ET 时区。
- **边界风险**：`to_et()`（`utils/timezone.py:67-73`）对 naive datetime 假设为 CST（`dt.replace(tzinfo=CST)`）。`trade_records.entry_time` 由 `playbook_service.py:1099` 写入 `now_et().isoformat()`（ET-aware ISO 字符串），`datetime.fromisoformat()` 解析后带 tzinfo → `to_et` 走 `astimezone(ET)` 正确。**结论：当前写入格式下无问题**。但若未来有其他路径写入 naive ET 时间，会被误当 CST（偏移 12h）。建议在 `to_et` 的 docstring 中明确标注 naive=CST 假设。

#### A5. 部分成交按实际量记账 — ✅ 正确，SIM 期可接受

**代码验证**：
- `dual_broker.py:379-440` `_query_fill` 返回 `(price, dealt_qty)`，同 order_id 多笔成交 qty 累加。
- `playbook_service.py:992-1003` `_effective_qty` 逻辑：`0 < filled_qty < req_qty` → 用 filled_qty；否则用 req_qty（filled_qty=0 时兼容 SIM 均价=0 场景）。
- trade_records / open_positions / 回滚 / 注册 / STOP / 推送全部改用 `_effective_qty`。

**复核结论**：
- 正确性：✅ 逻辑完整。
- **SIM 局限**：`_query_fill` 轮询 3×0.5s=1.5s，SIM 环境 `history_deal_list_query` 延迟可达 ~29s → 大概率查不到 → `filled_qty=0` → 退回下单量。**SIM 期部分成交仍记全量，可接受**（SIM 环境部分成交概率极低，且不影响策略 edge 验证）。
- **Live 前必须**：(a) 增加轮询时长或 (b) 依赖已有 reconciler 做事后对账补正。见 B3。

#### A6. 下单幂等抢占 — ✅ 正确，边界明确

**代码验证**：
- `db.py:246-261` `try_claim_execution`：原子 `UPDATE...WHERE executed=0`，rowcount 判定。
- `playbook_service.py:935-937`：下单前抢占，失败放弃。
- `playbook_service.py:945,950`：下单异常/失败时 `release_execution_claim`。
- `client_order_id` 在 `Order` 对象上设置（line 931），但 `dual_broker.py:227-231` `place_order` 未透传（Futu 有 `remark` 参数未用）。

**复核结论**：
- 正确性：✅ 防"本系统重复调用"有效。
- **孤儿单风险**：抢占成功 → broker 下单成功 → 进程崩溃 → 重启后 executed=1 但无 trade_record → 孤儿单。此场景 A6 防不住。
- **SIM 期评估**：孤儿单在 SIM 中 = 数据不一致，position_sync 30s 会同步 broker 持仓到 open_positions，不会造成资金损失。可接受。
- **Live 前建议**：透传 `client_order_id` 到 Futu `remark` 字段（1 行改动），实现 broker 级幂等。见 B4。

#### A7. 下单前报价健康门 — ✅ 正确，C3 是前置依赖

**代码验证**：
- `playbook_service.py:895-921`：复用 `_arr_ctx`（不额外调 API），仅在 bid/ask 齐备时拦截，fail-open。
- `check_quote_health`（`data_health_guard.py:24-73`）：spread 门 + 过期门 + 倒挂门 + mid 偏差门。
- **过期门对 Finnhub 失效确认**：`finnhub_provider.py:261` 存 `datetime.now().isoformat()`（ISO 字符串），`check_quote_health:61` 做 `float(ts)` → 对 ISO 字符串抛 ValueError → `except: pass` → 过期门静默跳过。**spread 门不受影响**。

**复核结论**：
- 正确性：✅ 门逻辑正确，阈值可配置。
- 阈值评估：5% spread 对大盘股偏宽（AAPL 通常 <0.01%），对小盘/低流动性合理。60s 过期合理。**建议上线后按标的流动性分层**（大盘 1%、小盘 5%），但非上线前必须。
- **C3 是 A7 过期保护在 Finnhub fallback 数据上生效的前置条件**，优先级提升。见 C3。

#### A8. dual_broker DST 修正 — ✅ 正确

**代码验证**：`dual_broker.py:400-405` 改用 `now_et()`（DST 正确），替代硬编码 `timezone(timedelta(hours=-4))`。

**复核结论**：正确、无过度、无回归。

---

### D.2 B 部分博弈结论

#### B1. 市价单 vs 限价单 —【不做（上线前），上线后 P2】

**结论：上线前保持市价单 + A7 门。上线后视滑点数据决定是否引入限价单。**

理由：
1. 数据积累期，仓位小（`daily_notional_cap_pct=0.60` 且 SIM），滑点绝对金额可控。
2. A7 报价健康门已拦截 spread>5% / 过期>60s 的坏价。
3. 限价单引入撤单/追单/部分成交状态机，复杂度与回归风险高，违反"不过度工程"原则。
4. 已有 `smart_execution.limit_order_buffer_ticks=2` 配置，上线后需要时可直接启用。
5. **决策依据**：A5/A7 已落地滑点 bps 追踪（`_slippage_bps`），上线后若滑点中位数 >20bps 再考虑限价单。

#### B2. IC 历史数据重算 —【做，上线前】

**结论：做一次性回填重算，成本低、收益高。**

理由：
1. A2 修复前，SHORT 信号 IC 系统性反号 → 基于错误 IC 调出的因子权重/门槛不可信。
2. 数据量有限（`signal_snapshots.db`，每日数十条），重算成本低。
3. 可复用 `scripts/backfill_forward_returns.py` 的批处理框架。
4. 不做 = 用错误数据继续指导策略，违反"让数据可信"原则。
5. **实施方案**：写一次性脚本，对 `signal_snapshots` 全量重算 IC（不修改原始 strength/fwd_ret，仅重算 IC 聚合指标并覆盖 `factor_ic_latest.json`）。

#### B3. SIM→Live 部分成交对账 —【做，上线前 P1】

**结论：Live 前增加入场量事后对账，SIM 期不做。**

理由：
1. SIM 期部分成交概率极低（模拟盘通常全量成交），且 `_query_fill` 的 1.5s 轮询在 SIM 下大概率查不到 → 退回下单量，可接受。
2. Live 环境部分成交是真实风险（快速行情/低流动性），记错量 → 幽灵仓 → 退出侧按错误量平仓 → 残留仓位。
3. 已有 `auto_position_reconcile`（09:30 ET 运行），**建议扩展其覆盖入场量偏差**（对比 trade_records.quantity vs broker position qty），而非新建 reconciler。
4. **实施方案**：在现有 reconciler 中增加"入场量校验"步骤，发现偏差时修正 trade_records + open_positions 并告警。

#### B4. Broker 级真幂等 —【做，上线前 P1，最小改动】

**结论：透传 `client_order_id` 到 Futu `remark` 字段。不复用 `IdempotencyManager`。**

理由：
1. A6 的 `try_claim_execution` 防的是"本系统重复调用"，防不了"抢占后崩溃"的孤儿单。
2. Futu `place_order` 支持 `remark` 字段（≤64 字符），透传 `client_order_id`（`pb_{id}_{env}`）即可在 broker 侧留痕。
3. 这不是"真幂等"（Futu 不按 remark 去重），但让崩溃恢复时能通过 remark 查到 broker 侧订单，避免孤儿单。
4. `IdempotencyManager` 接在遗留 `ExecutionEngine` 上，复用会引入分叉，违反铁律。
5. **实施方案**：`dual_broker.py:227` `place_order` 增加 `remark=order.client_order_id`（1 行改动）。

#### B5. daily_notional_cap_pct=0.60 退出条件 —【做，立即定义】

**结论：立即在配置中定义明确的 graduation 条件，避免"临时变永久"。**

建议条件（需博弈确认）：
- **样本量门槛**：≥50 笔 PLAYBOOK_SIM 已平仓交易（当前约 30+ 笔）。
- **IC 门槛**：全局 Signal IC ≥ 0.03（当前需重算后确认）。
- **胜率门槛**：PLAYBOOK_SIM 胜率 ≥ 45%（含手续费后净 PnL）。
- **最大回撤门槛**：期间最大回撤 ≤ 8%。
- 满足全部条件后，`daily_notional_cap_pct` 回调至 0.30（保守值）。
- **治理机制**：在 `unified_config.yaml` 中增加 `playbook.cap_reduction_trigger` 配置块，由盘后审计脚本自动检查并告警。

---

### D.3 C 部分优先级排序

#### 上线前必须（P0/P1）

| 编号 | 项目 | 优先级 | 理由 | 改动量 |
|------|------|--------|------|--------|
| C3 | Finnhub 时间戳修复 | **P0** | A7 过期门的前置条件，1 行改动 | 极小 |
| B2 | IC 历史重算 | **P1** | 数据可信度基础 | 小（一次性脚本） |
| B4 | Broker remark 透传 | **P1** | 孤儿单防护，1 行改动 | 极小 |
| A1+ | ACB/PDT fail-close（live 前） | **P1** | 风控可靠性 | 小 |
| B3 | 入场量对账扩展 | **P1** | 部分成交防护 | 中 |

#### 上线后偿还（P2/P3）

| 编号 | 项目 | 优先级 | 理由 |
|------|------|--------|------|
| C1 | 时区 SSOT 收敛 | P2 | 16+ 处替换风险高，当前 `utils/timezone.py` + pytz 工作正确 |
| C2 | 主线 order_executor 边界审计 | P2 | 不删（AGENT.md 明确），加运行期断言证明零新仓 |
| C4 | 出场侧报价门 | P3 | 出场"能平就平"优先于价格质量，SL/EOD 时间敏感 |
| C5 | mfe_pct/pnl_pct 量纲统一 | P3 | 已文档化，统一回归风险高，SIM 期够用 |
| B1 | 限价单 | P2 | 视滑点数据决定 |
| B5 | cap 回调执行 | P2 | 达标后执行 |

---

### D.4 一致性检查

逐项验证所有建议是否违反铁律：

| 铁律 | 检查结果 |
|------|----------|
| Playbook 唯一管线 | ✅ 所有修复均在 Playbook 路径内，未触及主线 signal_pipeline → order_executor |
| 无双重管理 | ✅ ACB/PDT 为共享单例，仓位归属互斥（Playbook→SQLite，主线→内存） |
| 不新增分叉 | ✅ 未引入新的执行路径或订单管理器。B4 明确拒绝复用 IdempotencyManager |
| 不过度工程 | ✅ 所有建议均为最小改动。B1 明确拒绝上线前引入限价单状态机 |
| 改动可回滚 | ✅ A7 有 `pretrade_quote_gate_enabled` 开关；A1 有 fail-open 兜底；C3 仅改数据源 |

---

### D.5 排序后的最终修复方案

#### 第一梯队：上线前必须（P0，阻塞上线）

1. **C3 — Finnhub 时间戳修复**（`finnhub_provider.py:261`）
   - 改 `datetime.now().isoformat()` → 用上游 `data.get("t")` 的 Unix 时间戳
   - 使 A7 过期门对 Finnhub fallback 数据生效
   - 改动量：1 行

2. **A1+ — ACB/PDT 硬门 fail-close**（`playbook_service.py:707,876`）
   - 将 `except: 放行` 改为 `except: 拒绝开仓`（仅 live 路径，SIM 保持 fail-open）
   - 改动量：2 处 except 块

#### 第二梯队：上线前建议（P1，强烈建议）

3. **B4 — Broker remark 透传**（`dual_broker.py:227`）
   - `place_order` 增加 `remark=getattr(order, 'client_order_id', '')`
   - 改动量：1 行

4. **B2 — IC 历史重算**（一次性脚本）
   - 重算 `signal_snapshots` 全量 IC，覆盖 `factor_ic_latest.json`
   - 改动量：新建脚本 ~80 行

5. **B3 — 入场量对账扩展**（现有 reconciler）
   - 在 `auto_position_reconcile` 中增加 trade_records.quantity vs broker qty 校验
   - 改动量：~30 行

6. **B5 — cap 退出条件定义**（`unified_config.yaml`）
   - 增加 `playbook.cap_reduction_trigger` 配置块
   - 改动量：~10 行配置

#### 第三梯队：上线后偿还（P2/P3）

7. C1 — 时区 SSOT 收敛（P2）
8. C2 — 主线边界审计 + 运行期断言（P2）
9. B1 — 限价单（P2，视滑点数据）
10. C4 — 出场侧报价门（P3）
11. C5 — 量纲统一（P3）

#### 明确不做项

| 项目 | 理由 |
|------|------|
| 复用 IdempotencyManager | 接在遗留 ExecutionEngine 上，复用会引入分叉 |
| 删除主线 order_executor | AGENT.md 明确仍服务遗留/紧急平仓 |
| 出场侧报价门（上线前） | 出场时间敏感，"能平就平"优先 |
| 全量时区替换（上线前） | 16+ 处改动回归风险过高 |
| mfe_pct/pnl_pct 量纲统一（上线前） | 已文档化，统一回归风险高 |

---

### D.6 遗留待辩论焦点

以下问题 Agent-α 给出了倾向性意见，但需辩论 agent 博弈确认：

1. **A1+ fail-close 的边界**：是否仅 live 路径 fail-close、SIM 保持 fail-open？还是统一 fail-close？
2. **B2 IC 重算范围**：是否仅重算 IC 聚合指标，还是连 `fwd_ret` 也重算？（`fwd_ret` 本身是正确的，只是 IC 聚合时 strength 符号错了）
3. **B5 graduation 门槛数值**：50 笔 / IC≥0.03 / 胜率≥45% / 回撤≤8% 是否合理？
4. **C3 修复方式**：用上游 `t` 字段（Unix 时间戳）还是 `datetime.fromtimestamp(t).isoformat()`？需确认下游所有消费者的 timestamp 格式预期。

---

## E. 辩论 Agent-β 分析报告（2026-07-19，第三轮）

> 方法：不复用 Agent-α 结论，独立读源码 + git 提交 + 生产 DB 实证数据逐条验证，再给出裁定。

### E.1 独立验证结果（源码 / git / DB 三方交叉）

**git 验证**：分支 `rfc-060-phase1-hotfixes`，三个 commit `da619781`/`3e298c1f`/`7f9eb0e2` 与文档声明一致，工作区另有无关改动未混入。

| 项 | 验证结论 | 关键证据 |
|---|---|---|
| A1 | ✅ 属实 | `playbook_service.py:701-708` ACB 硬门 fail-open；`pipeline.py:3299-3319` record 闭环含 ORPHAN/PHANTOM/GHOST 排除 |
| A2 | ✅ 属实 | `signal_ic_tracker.py:368` `_calc_fwd` 方向感知；538/567/600 三处已用 raw strength |
| A3 | ✅ 属实 | `pipeline.py:3646-3647` 块内本地 `get_playbook_config()` |
| A4 | ✅ 属实，边界可控 | `to_et`（`utils/timezone.py:67-73`）naive→CST 假设确认；`entry_time` 写入为 `now_et().isoformat()`（ET-aware），`fromisoformat` 保留 tzinfo → `astimezone` 路径正确 |
| A5 | ✅ 属实 | `_query_fill`（`dual_broker.py:379-440`）qty 累加；`_effective_qty`（`playbook_service.py:992-1003`）0 时退回下单量 |
| A6 | ✅ 属实，补一处细节 | `try_claim_execution`（`playbook/db.py:246-261`）原子 UPDATE。**补充**：`playbook_service.py:989-991` 下单成功但取价失败的路径**故意不释放 claim**（释放会允许重试 → 重复下单），行为正确，但这就是孤儿单场景的实证来源 |
| A7 | ✅ 属实 | gate 在 `playbook_service.py:895-921`；配置在 `playbook/config.py:42-44`；`check_quote_health:61` `float(ts)` 对 ISO 字符串抛 ValueError 被吞 → 过期门对 ISO 格式静默失效 |
| A8 | ✅ 属实 | `dual_broker.py:394,403` 已用 `now_et()` |

**Agent-α 未发现的一个缓解事实**：`futu_lv3_provider.py:848-851` 中 Futu 内部 fallback 到 Finnhub 时会用 `time.time()` **覆盖** timestamp（float）——因此过期门失效仅限 **router 级 fallback**（`MarketDataRouter.get_quote` 直接落 Finnhub provider 的路径），范围比文档描述的略窄，但根因与修复不变。

**B5 前提纠正（重要）**：文档称 `daily_notional_cap_pct=0.60`，实际 `config/unified_config.yaml:1165` 已为 **0.80**（2026-07-10 由 0.60 上调）。B5 的博弈基线必须按 0.80 重述。

### E.2 生产 DB 实证（2026-07-19 实时查询）

| 指标 | 数值 | 含义 |
|---|---|---|
| PLAYBOOK_SIM 已平仓 | **83 笔**（36 胜）| 样本量门槛 ≥50 已达成 |
| PLAYBOOK_SIM 胜率 | **43.4%** | 低于 α 建议的 45% 门槛，未达标 |
| PLAYBOOK_SIM 累计净 PnL | **-$976.93** | **净亏损**，edge 尚未被证明 |
| 近 7 天已平仓 | 35 笔 | 符合项目"近 7 天数据"时效规范的最小分析窗 |
| signal_snapshots SHORT | 1185 条（1172 有 fwd 值）| A2 污染面大，B2 重算必要性确认 |

**实证结论**：当前处于"高水位(0.80) + 净亏损 + 胜率未达标"状态——数据积累期仍在，但**回调 cap 的触发条件远未满足**，B5 不是近期项；B2 重算会改变 IC 基准，graduation 的 IC 门槛必须在重算后标定。

### E.3 A 部分裁定

全部确认为 **正确 / 无过度 / 可保留**，与 Agent-α 一致。两处强化建议：

1. **A1+ fail-close 实现方式**：不按 env 硬分支（`if live`），改为配置驱动 `risk.acb_fail_mode: open|closed`（默认 `open`，live 切换时翻 `closed`）。理由：符合"改动可回滚、可配置化"铁律；且 SIM 期即可用 `closed` 做演练验证。**这是对 D.6 焦点 1 的裁定：统一 fail-close，但以配置开关控制启用时机，而非代码分叉。**
2. **A6 补充认知**：claim 在"下单成功但取价失败"路径不释放是**正确设计**（防重复下单优先于允许重试），文档应明确此语义，避免后续 agent 误判为遗漏。

### E.4 B 部分裁定（含对 D.6 焦点的裁决）

| 项 | 裁定 | 与 α 的差异 |
|---|---|---|
| B1 限价单 | **不做（上线前）**，与 α 一致 | 补触发器：`_slippage_bps` 中位数 >20bps（≥30 笔成交样本）再复议 |
| B2 IC 重算 | **做，上线前**。**范围裁定（D.6 焦点 2）：仅重算 IC 聚合指标，不重算 `fwd_ret`**（其方向逻辑 `_calc_fwd:368` 本身正确）| 补约束：重算必须**按 strategy_id 分段**产出，遵守"校准数据一致性"原则（禁混策略 IC 指导单策略调参）；覆盖物为 `factor_ic_latest.json` + `ic_analysis_latest.json` |
| B3 入场量对账 | **做，上线前 P1**，扩展既有 `positions/reconciler.py` + `check_position_reconcile`（09:30 ET），**不新建模块** | 与 α 一致；下一轮的待办 3 已要求先核实现有对账覆盖面再定点扩展 |
| B4 remark 透传 | **做，上线前 P1**（`dual_broker.py:227` 加 `remark`，1 行）。**措辞裁定**：这是"崩溃后可审计/可定位"，**不是真幂等**（Futu 不按 remark 去重），文档与代码注释不得夸大语义 | α 定性一致；β 要求把"真幂等"措辞降级，防止后续误判防护等级 |
| B5 cap 退出条件 | **做，但修正基线与门槛**（见下）| 基线 0.60→**0.80**；门槛数值修正见下 |

**B5 修正后的 graduation 条件（建议终裁锁定）**：
- 样本量：≥50 笔 PLAYBOOK_SIM 已平仓（**已达成：83 笔**，此项单独不构成约束）
- 胜率：≥45% 且 **累计净 PnL > 0**（当前 43.4% / -$976.93，**未达成**）
- IC：重算后全局 Signal IC ≥ 0.03（待 B2 完成后标定）
- 最大回撤：≤8%
- 数据窗口：按项目时效规范，**以近 7 天 PLAYBOOK_SIM 数据为准**
- 达标后 cap 0.80 → 0.30；机制：配置块 + 盘后审计脚本检查告警（不自动改配置，人审确认）

### E.5 C 部分裁定

**D.6 焦点 4 裁决：C3 必须用 Unix float，禁止 ISO 字符串。**

- 根因：`check_quote_health:61` 做 `time.time() - float(ts)`，ISO 字符串必抛异常被吞。`datetime.fromtimestamp(t).isoformat()` 等于换汤不换药，门依然失效。
- 修复（`finnhub_provider.py:261`，1 行）：`"timestamp": float(data.get("t") or 0) or time.time()`——优先上游报价时间（更真实反映过期），无 `t` 时退取 fetch 时间。
- 消费者兼容性已核实：quote timestamp 唯一下游消费者就是 `check_quote_health`（期望 float）；`futu_lv3_provider` 契约文档（:718）本就声明 `timestamp: float`，本次修复是**对齐既有契约**而非破坏。
- 顺带到 `data_health_guard.py` docstring 补一句契约声明（"timestamp 必须为 Unix float"），防未来 provider 再犯。不改动 guard 解析逻辑（不做双格式兼容，避免防御性过度工程）。

**C 部分排序**：与 α 的 P0/P1/P2/P3 表一致，C3 维持 P0 阻塞项。

### E.6 共识 / 分歧 / 给终裁轮的待办

**已达成共识（α 与 β 一致，可直接进入执行）**：
1. A1-A8 全部保留，无回退项。
2. C3 P0：Unix float 修复 + guard 契约注释（共 2 行级改动）。
3. B4 P1：remark 透传 1 行，措辞降级为"可审计性"。
4. B1 上线前不做限价单。
5. B2 范围 = 仅重算聚合 IC，按 strategy_id 分段。
6. B3 复用既有 reconciler，不新建模块。

**β 提出、待终裁确认的分歧/修正**：
1. B5 基线 0.60→0.80 的事实纠正；graduation 门槛增加"净 PnL>0"与"近 7 天窗口"两项约束。
2. A1+ 用配置开关 `risk.acb_fail_mode` 实现 fail-close，而非 env 代码分支。
3. B4 语义措辞降级（审计性 ≠ 幂等）。

**给终裁轮的具体待办（每条可验证）**：
1. 核实现有 `check_position_reconcile`（`scheduler_triggers.py:1354`）对账覆盖面，给出 B3 扩展的精确插入点。
2. B2 重算脚本验收标准：重算前后 `factor_ic_latest.json` 的 SHORT 相关因子 IC 差值报告 + 分策略 IC 表。
3. C3 修复后验证：注入过期 timestamp 的 Finnhub quote，确认 A7 过期门实际拦截（1 个单测或脚本验证）。
4. 实测 Futu sim 账户 `place_order` 的 `remark` 字段可用性与 64 字符约束。
5. 终裁确认 B5 修正版 graduation 门槛并写入 `unified_config.yaml` 配置块（仅定义，不启用自动回调）。

---

## F. 终裁分析报告（Agent-Ω，2026-07-19，第四轮终裁）

> 方法：针对 E.6 五项待办逐一源码实证，对 α/β 分歧项做最终裁定，产出可执行清单。

### F.1 待办 1：现有对账覆盖面核实 + B3 精确插入点

**源码实证**：

`check_position_reconcile`（`scheduler_triggers.py:1354-1388`）→ `USPositionSync._verify_and_sync_capital`（`position_sync.py:1648-1824`）：

| 覆盖维度 | 是否覆盖 | 证据 |
|----------|----------|------|
| Symbol 存在性（Broker vs DB） | ✅ | `position_sync.py:1702-1708`：`futu_set != db_set` 检测 |
| Symbol 存在性（Broker vs Memory） | ✅ | `position_sync.py:1712-1728`：排除自管+playbook后比较 |
| **Quantity 一致性（DB.size vs Broker.qty）** | ❌ **未覆盖** | 全文无 `size`/`qty` 数值比较逻辑 |
| **Entry price 一致性** | ❌ **未覆盖** | 无 `entry_price`/`cost` 比较 |
| Capital Safety 更新 | ✅ | `position_sync.py:1786-1821` |

**结论**：现有对账仅覆盖"有没有"（symbol 级），**不覆盖"对不对"（quantity/price 级）**。B3 的入场量偏差恰好落在这个盲区。

**B3 精确插入点**：

在 `_verify_and_sync_capital`（`position_sync.py:1700` 附近，`issues = []` 之后）增加 quantity 校验步骤：

```python
# ── B3: 入场量一致性校验 (trade_records.quantity vs broker qty) ──
# 仅校验 playbook 仓位 (open_positions 有记录且 owner_strategy='playbook_sim')
for sym in (futu_set & db_set):  # 两层都有的 symbol
    broker_qty = futu_positions[sym].get('qty', 0)
    db_qty = db_positions[sym].get('size', 0)  # 需 _get_db_positions 返回 size
    if broker_qty > 0 and db_qty > 0 and abs(broker_qty - db_qty) >= 1:
        issues.append(
            f"⚠️ {sym} 量偏差: DB={db_qty} vs Broker={broker_qty} "
            f"(差{abs(broker_qty-db_qty):.0f}股)"
        )
```

**前置改动**：`_get_db_positions`（`position_sync.py:1836-1868`）当前仅返回 symbol 集合，需改为返回 `{symbol: {size, direction, entry_price}}` 字典。改动量 ~15 行。

**裁定**：B3 扩展方案确认，插入点明确，不新建模块，复用 `_verify_and_sync_capital` 09:30 ET 触发窗口。

---

### F.2 待办 2：B2 重算脚本验收标准

**裁定验收标准**：

1. **输入**：`signal_snapshots.db` 全量（含 1185 条 SHORT，1172 有 fwd 值）。
2. **处理**：
   - 不修改 `strength`/`fwd_ret_t1/t5/t20` 原始列（其方向逻辑 `_calc_fwd:368` 本身正确）。
   - 仅重算 IC 聚合：全局 IC / 每日 IC / 分策略 IC（按 `strategy_id` 分段，禁混策略）。
   - LONG/SHORT 均用 raw strength（与 A2 修复后逻辑一致）。
3. **输出物**：
   - `data/factor_ic_latest.json`（覆盖）
   - `data/ic_analysis_latest.json`（覆盖）
   - 控制台/日志：重算前后 SHORT 相关因子 IC 差值表（`factor_name | old_ic | new_ic | delta`）
   - 控制台/日志：分策略 IC 表（`strategy_id | n_signals | ic_t1 | ic_t5 | ic_positive_rate`）
4. **验收断言**：
   - 重算后全局 IC 与重算前差异应 >0.01（否则说明 SHORT 污染面不够大，需排查）。
   - 分策略 IC 表至少覆盖 3 个 strategy_id。
   - 脚本幂等（重复运行结果一致）。
5. **复用**：基于 `scripts/backfill_forward_returns.py` 的 DB 连接与批处理框架。

---

### F.3 待办 3：C3 修复方案 + 验证

**修复方案（终裁锁定）**：

`finnhub_provider.py:261`：
```python
# 修复前
"timestamp": datetime.now().isoformat(),
# 修复后
"timestamp": float(data.get("t") or 0) or time.time(),
```

**理由**：
- Finnhub `/quote` API 返回 `t` = 最后成交的 Unix 时间戳（秒）。
- `check_quote_health:61` 做 `time.time() - float(ts)`，期望 float。
- `futu_lv3_provider` 契约（:718）声明 `timestamp: float`，本修复是对齐既有契约。
- `float(data.get("t") or 0) or time.time()`：优先上游报价时间（更真实），无 `t` 时退取 fetch 时间。

**附带改动**：`data_health_guard.py` docstring 补契约声明：
```python
"""
检查报价是否健康。

Args:
    quote: 报价字典, 需要包含 bid, ask, price.
        timestamp 必须为 Unix float (time.time() 兼容), 不接受 ISO 字符串.
    ...
"""
```

**验证方案**（1 个单测）：
```python
def test_c3_finnhub_expired_gate():
    """C3 修复后, Finnhub 过期报价应被 A7 门拦截."""
    import time
    from app.risk.data_health_guard import check_quote_health
    # 模拟 120s 前的 Finnhub 报价
    stale_quote = {
        "bid": 150.0, "ask": 150.10, "price": 150.05,
        "timestamp": time.time() - 120,  # 120s 前
        "source": "finnhub",
    }
    ok, reason = check_quote_health(stale_quote, max_age_seconds=60.0)
    assert not ok, f"过期门应拦截, 但返回 ok: {reason}"
    assert "过期" in reason
```

**裁定**：C3 修复方案锁定为 Unix float，禁止 ISO 字符串。验证用单测确认。

---

### F.4 待办 4：Futu `remark` 字段可用性

**源码实证**：

Futu OpenAPI `place_order` 签名（`futu-api` Python SDK）：
```python
def place_order(self, price, qty, code, trd_side, order_type,
                adjust_limit=0, trd_env=TrdEnv.REAL, acc_id=0,
                remark=None, ...):
```

- `remark`：可选，str 类型，≤64 字符，用于客户端自定义标记。
- 当前 `dual_broker.py:227-231` 未传 `remark`。
- `client_order_id` 格式 `pb_{instruction_id}_{env}`（如 `pb_123_sim`），长度 ~12 字符，远在 64 字符限制内。

**修复方案**：

`dual_broker.py:227-231`（3 处 `place_order` 调用均需加）：
```python
ret, data = ctx.place_order(
    price=order.price, qty=order.quantity, code=order.symbol,
    trd_side=trd_side, order_type=futu_order_type,
    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
    remark=getattr(order, 'client_order_id', '') or None,  # B4: 崩溃后可审计
)
```

**语义裁定（采纳 β 意见）**：代码注释与文档中**不得使用"真幂等"措辞**。正确表述为"崩溃后可审计/可定位"（Futu 不按 remark 去重，仅留痕）。

**裁定**：B4 修复方案确认，3 处 `place_order` 均加 `remark`，语义降级为"可审计性"。

---

### F.5 待办 5：B5 修正版 graduation 门槛终裁

**事实确认**：
- `unified_config.yaml:1165` 当前值 `daily_notional_cap_pct: 0.80`（β 纠正属实，α 引用的 0.60 已过时）。
- 生产 DB 实证（β 提供）：83 笔已平仓 / 43.4% 胜率 / -$976.93 净 PnL。

**终裁锁定的 graduation 条件**：

```yaml
# unified_config.yaml 新增配置块 (仅定义, 不启用自动回调)
playbook:
  cap_reduction_trigger:
    enabled: false  # 人审确认后手动改 true
    min_closed_trades: 50        # 已达成 (83笔)
    min_win_rate: 0.45           # 未达成 (43.4%)
    require_positive_net_pnl: true  # 未达成 (-$976.93)
    min_global_ic: 0.03          # 待 B2 重算后标定
    max_drawdown_pct: 0.08
    data_window_days: 7          # 近 7 天 PLAYBOOK_SIM 数据
    target_cap_pct: 0.30         # 达标后回调目标
    current_cap_pct: 0.80        # 当前值 (SSOT 显式化)
```

**裁定理由**：
1. 基线 0.80 确认（β 纠正采纳）。
2. 增加 `require_positive_net_pnl: true`（β 建议采纳）——净亏损状态下谈 graduation 无意义。
3. 增加 `data_window_days: 7`（对齐项目"近 7 天数据"时效规范）。
4. `min_global_ic` 暂设 0.03，B2 重算后根据实际分布标定（可能上调或下调）。
5. `enabled: false` + 人审确认（不自动改配置，防止脚本误触发）。

---

### F.6 终裁共识清单（可直接进入执行）

| 序号 | 项目 | 改动量 | 执行优先级 | 验证方式 |
|------|------|--------|------------|----------|
| 1 | C3 Finnhub timestamp → Unix float | 1 行 + docstring | **P0 阻塞** | F.3 单测 |
| 2 | B4 remark 透传（3 处 place_order） | 3 行 | **P1** | SIM 下单后查 remark 字段 |
| 3 | A1+ ACB fail-close 配置开关 | ~10 行 | **P1** | 配置 `risk.acb_fail_mode: closed` 后异常测试 |
| 4 | B2 IC 重算脚本 | ~80 行新脚本 | **P1** | F.2 验收标准 |
| 5 | B3 入场量对账扩展 | ~15 行 | **P1** | 注入量偏差后 09:30 对账告警 |
| 6 | B5 graduation 配置块定义 | ~10 行 YAML | **P1** | 配置存在性检查 |

### F.7 终裁分歧裁定记录

| 分歧项 | α 意见 | β 意见 | 终裁 |
|--------|--------|--------|------|
| A1+ fail-close 实现 | 按 env 代码分支 | 配置开关 `risk.acb_fail_mode` | **采纳 β**：配置驱动，默认 `open`，live 切换时翻 `closed`，符合"可回滚"铁律 |
| B4 语义 | "broker 级幂等" | "可审计性，非真幂等" | **采纳 β**：措辞降级，防后续误判防护等级 |
| B5 基线 | 0.60 | 0.80（实证纠正） | **采纳 β**：基线 0.80 |
| B5 门槛 | 4 项 | 6 项（+净PnL>0, +7天窗口） | **采纳 β**：6 项门槛 |
| B2 范围 | 仅 IC 聚合 | 仅 IC 聚合 + 按 strategy_id 分段 | **采纳 β**：补充分策略约束 |
| C3 修复方式 | 未明确 | Unix float，禁 ISO | **采纳 β**：`float(data.get("t") or 0) or time.time()` |

### F.8 执行后验证清单

每项修复完成后，须逐条验证：

- [ ] C3：运行 F.3 单测，确认过期 Finnhub quote 被 A7 门拦截
- [ ] B4：SIM 下单后 `order_list_query` 查 remark 字段有值
- [ ] A1+：配置 `risk.acb_fail_mode: closed`，模拟 ACB 异常，确认开仓被拒
- [ ] B2：重算脚本输出 SHORT IC 差值报告，确认 `factor_ic_latest.json` 已覆盖
- [ ] B3：注入量偏差（DB.size ≠ Broker.qty），09:30 对账触发告警
- [ ] B5：`unified_config.yaml` 含 `cap_reduction_trigger` 块，`enabled: false`

### F.9 状态更新

- [x] 第一轮：hotfix agent 起草（A/B/C 部分）
- [x] 第二轮：复核 Agent-α 源码审查（D 部分）
- [x] 第三轮：辩论 Agent-β 独立验证 + 数据实证（E 部分）
- [x] 第四轮：终裁 Agent-Ω 分歧收敛 + 执行清单（F 部分）
- [ ] 执行：按 F.6 清单逐项落地（每项完成后打勾）
