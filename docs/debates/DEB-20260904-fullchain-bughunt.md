# DEB-20260904-fullchain-bughunt — 双模型三任务评测: ①全链路深入找bug ②交易复盘·创新盈利方案 ③项目减法·清理冗余冲突: 逻辑异常/bug 候选 112 项 (共性 15) — 待独立核实裁决

- **立案日**: 2026-09-04
- **发起人**: llm-eval-harness bug-hunt 链路 (qwen3.8-max-0902, kimi-k3 @ 百炼 compatible-mode)
- **状态**: 待决策 — 112/112 簇已独立核实裁决 (CONFIRMED 101 / REJECTED 11), 由 harness merge_verdicts_deb 于 2026-09-04 10:27 回注
- **纪律**: 同一题面同一采样口径 `{"temperature": 0.0, "max_tokens": 8192, "seed": 20260904}` (所有臂共用, max_retries=0); 答案全文落盘不截断; 模型自报 severity 仅参考, 以裁决后 severity 为准; off_manifest/越界引用=幻觉证据, 计入该模型误报
- **关联**: 评测报告 report_deb20260904.md · 台账 findings.db (run=deb20260904) · 原始答案 bug_hunt/deb20260904/answers/

## 一、审计设置

| 模型 | 调用 | 失败 | 契约违规 | findings | 输入tok | 输出tok |
|---|---|---|---|---|---|---|
| qwen3.8-max-0902 | 13 | 0 | 0 | 118 | 735418 | 307833 |
| kimi-k3 | 13 | 11 | 0 | 26 | 103958 | 50817 |

- 目标仓 `/opt/ftseek/FTSeek-US` @ `589ef26fb7bc` 全程只读。
- 打包与盲区: s1_entry_config: 22 in / 0 truncated / 4 omitted; s2_market_data: 11 in / 1 truncated / 29 omitted; s3_scheduling_pipeline: 14 in / 2 truncated / 34 omitted; s4_signals_sources: 34 in / 0 truncated / 36 omitted; s5_decision_orchestration: 26 in / 1 truncated / 21 omitted; s6_playbook_engine: 17 in / 2 truncated / 39 omitted; s7_risk_execution: 13 in / 1 truncated / 61 omitted; s8_monitoring_surface: 24 in / 1 truncated / 33 omitted; s9_review_backtest: 17 in / 1 truncated / 30 omitted; t2_strategy_exit: 14 in / 1 truncated / 40 omitted; t2_trade_review: 1 in / 0 truncated / 0 omitted; t3_code_subtraction: 1 in / 0 truncated / 0 omitted; t3_project_subtraction: 1 in / 1 truncated / 0 omitted
- 局限: 每片段单次采样 (能力 vs 运气不可分); 超长文件中段不可见; 召回是相对口径 (对三模型并集+核实证实的池)。

## 二、裁决回写规范 (分析 agent 阅读)

对每个议题, 把 `裁决: 待回写` 行替换为以下格式之一 (标记行 `<!-- DEB-VERDICT -->` 保留):

```
裁决: CONFIRMED | severity=4 | 置信=high | 理由: <对照代码的论证, 引具体行为证>
裁决: REJECTED | 置信=high | 理由: <为何不成立: 路径不可达/上游已守卫/语义误读>
裁决: UNCERTAIN | 置信=low | 理由: <需要运行态才能判定, 说明缺什么证据>
```

判定标准: 该缺陷在**真实运行路径**上是否会造成错误行为 (错单/漏单/资损/风控失效/状态错乱 = severity 4-5)。
发现同一根因被拆到不同文件的两议题时, 在理由中注明 `merge=<另一议题号>`。
> 本轮任务分三类, 裁决口径随任务类型切换 (每议题标注环节前缀 s*=T1找bug, t2*=T2方案, t3*=T3减法):
> - **T2 方案类 (t2_trade_review / t2_strategy_exit)**: CONFIRMED = 该方案的数据证据在材料中真实存在、机制逻辑成立、且现有代码未实现同等逻辑; severity=核实者认可的预期收益影响 (1-5); REJECTED = 数据不支持/机制方向错/与现有实现重复; UNCERTAIN = 需回测或运行态才能判。理由里须写明复算或核对的证据。
> - **T3 减法类 (t3_code_subtraction / t3_project_subtraction)**: CONFIRMED = 对照真实仓 grep/引用面核实确属冗余/无价值/冲突且删除不破坏运行路径 (含动态加载/cron/文档流程); severity=清理价值 (1-5); REJECTED = 仍在用、删了会坏、或事实误读; UNCERTAIN = 需运行态确认。

## 三、共性议题 (≥2 模型命中 — 高置信候选, 优先裁决)

### 议题 #183 [共性 2/2] `app/execution/broker_adapter.py`:184-249 — 实盘指定账户不存在时 _discover_real_account 的阻断被 connect() 无条件覆盖, 仍返回连接成功

环节: s7_risk_execution · 类别: logic_anomaly · 自报最高 severity: S4
- **kimi-k3** (S4, logic_anomaly, app/execution/broker_adapter.py:184-188): 实盘指定账户不存在时 _discover_real_account 的阻断被 connect() 无条件覆盖, 仍返回连接成功
  - 证据引用: `L248-249: `# 不 fallback，阻断连接` / `self.connected = False`; 但 connect() 在调用 `self._discover_real_account()` 之后 L186 无条件 `self.connected = True`, L188 `return True``
  - 影响声称: 触发路径: 实盘 (trade_env=0) 且环境变量 REAL_ACCOUNT_ID 设置为不在 get_acc_list 返回中的账户。_discover_real_account 走 L243 elif 分支, 打 error 并置 connected=False 意图阻断, 但 connect() 随后把它改回 True 并返回成功。此时 self.account_id 仍是构造时的旧值
  - 建议: 让 _discover_real_account 返回 bool, connect() 中 `if not self._discover_real_account(): self.connected=False; return False`; 或 connect() 在 L186 前检查 self.connected 是否已被置 False。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/execution/broker_adapter.py:184-249): 实盘指定账户不存在时阻断连接逻辑被 connect() 随后置为 True 覆盖
  - 证据引用: `self._discover_real_account()

            self.connected = True
...
                # 不 fallback，阻断连接
                self.connected = False`
  - 影响声称: _discover_real_account 在 REAL_ACCOUNT_ID 指定账户不存在时设置 self.connected = False 试图阻断连接，但 connect() 在调用后无条件执行 self.connected = True，导致阻断失效；实盘可能继续以旧 account_id、空 account_id 或非目标账户执行后续下单。
  - 建议: 让 _discover_real_account 返回 bool 或抛出异常，connect() 只有在其明确成功后才设置 self.connected = True。

<!-- DEB-VERDICT cluster=183 -->
裁决: CONFIRMED | severity=5 | 置信=high | 理由: 实盘指定账户不存在时 _discover_real_account 在 L249 设 self.connected=False 意图阻断，但该方法无返回值/不抛异常，控制流回到 connect() 后 L186 无条件 self.connected=True、L188 return True，阻断完全失效。后果：指定账户不存在时系统仍认为连接成功，后续下单可能使用错误账户或 account_id 未更新导致下单失败/错单。属风控失效级别。

### 议题 #190 [共性 2/2] `app/risk/position_exit_monitor.py`:188-195 — 节假日检查异常被吞掉后仍按时间判断开市

环节: s7_risk_execution · 类别: error_swallowing, time_boundary · 自报最高 severity: S3
- **kimi-k3** (S2, time_boundary, app/risk/position_exit_monitor.py:194-195): is_us_market_open 收盘边界含整个 16:00 分钟, 收盘后 60 秒内仍判定开市
  - 证据引用: ``t = now_et.hour * 60 + now_et.minute` / `return 570 <= t <= 960``
  - 影响声称: 触发路径: 16:00:00–16:00:59 (ET) 之间 t=960 仍返回 True。ExitMonitor 60s 扫描若落在此窗口, ATR 止损/pre-close 等退出会以市价/限价单提交到已收盘市场, 被 broker 拒绝 → close 失败计入 error, 仓位被迫隔夜; 若与 FLAT_ALL 冷却叠加, 次日开盘前无法重试。
  - 建议: 改为 `570 <= t < 960` (16:00:00 起即视为收盘), 与 pre_close 15:55 强平时间留出缓冲。
- **qwen3.8-max-0902** (S3, error_swallowing, app/risk/position_exit_monitor.py:188-195): 节假日检查异常被吞掉后仍按时间判断开市
  - 证据引用: `try:
    from app.utils.holiday_calendar import get_holiday_calendar
    if get_holiday_calendar().is_us_holiday(now_et.date()):
        return False
except Exception as _silent_e:
    logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
t = now_et.hour * 60 + now_et.minute
return`
  - 影响声称: 如果 holiday_calendar 导入、初始化或查询失败，函数不会返回 False，而是继续按 09:30-16:00 判断；在美股节假日但日历异常时，系统会误认为市场开盘，可能提交退出订单。
  - 建议: 节假日检查失败时应 fail-closed，在 except 分支直接 return False，并保留告警。

<!-- DEB-VERDICT cluster=190 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 两个子问题均成立：(1) L194-195 return 570<=t<=960，其中 960=16:00*60，含整个 16:00 分钟（16:00:00~16:00:59 均返回 True），美股 16:00 已收盘，边界过宽约 60 秒。(2) L188-193 节假日检查 try/except 吞掉异常后仅 logger.warning，继续执行时间判断；若 holiday_calendar 持续故障，节假日会被误判为开市，可能导致假日提交退出订单面临跳空风险。未在旧基线中。

### 议题 #184 [共性 2/2] `app/execution/broker_adapter.py`:476-502 — 订单状态字符串匹配把 CANCELLING_ALL 当作已取消失败

环节: s7_risk_execution · 类别: dataflow_inconsistency, race_condition · 自报最高 severity: S4
- **kimi-k3** (S3, dataflow_inconsistency, app/execution/broker_adapter.py:481-502): CANCELLED_PART 部分成交仅打日志, 返回值仍声明整单 submitted 且不带 dealt_qty
  - 证据引用: `L481-482: `elif 'CANCELLED_PART' in status:` / `logger.warning(f"⚠️ [部分成交后取消] {order.symbol} Status={status}, dealt={...}")`; L497-502 返回 `{"order_id": ..., "futu_order_id": futu_id, "futu_order_ids": [futu_id], "status": "submitted"}` — 无 dealt_qty/filled_quantity 字段`
  - 影响声称: 触发路径: IOC/限价单部分成交后余量被取消 (order_type_selector 会返回 IOC; 或流动性不足时)。调用方拿到 status='submitted' 且无从得知实际成交量, 若按 order.quantity 全额登记持仓, 持仓数量虚高, 需等 position_sync 30s 周期才纠正 — 期间退出监控/组合止损按错误 size 计算 PnL 与减仓数量。
  - 建议: 在返回 dict 中增加 `dealt_qty`/`partial_fill` 字段 (query_order 已能取 dealt_qty), 或将 status 置为 'partially_filled' 让上游按实际成交量登记。
- **qwen3.8-max-0902** (S4, race_condition, app/execution/broker_adapter.py:476-480): 订单状态字符串匹配把 CANCELLING_ALL 当作已取消失败
  - 证据引用: `_is_hard_fail = 'FAIL' in status or 'DEL' in status
_is_full_cancel = 'CANCELLED_ALL' in status or ('CANCEL' in status and 'PART' not in status)
if _is_hard_fail or _is_full_cancel:
    logger.error(f"🚨 [订单失败] {order.symbol} Status={status}")
    raise RuntimeError(f"订单提交后失败: {order.symbol} status={`
  - 影响声称: CANCELLING_ALL 只是撤单请求中，并非已取消；该匹配会把它当作整单失败并抛出异常。若此时订单后续仍可能成交，上游会把提交视为失败并可能重试或丢失真实成交，造成重复下单或持仓状态不一致。
  - 建议: 区分 CANCELLING/CANCELLED 状态，只有 CANCELLED_ALL 等终态才判定失败；对 CANCELLING 应继续查询终态或返回 pending_cancel。
- **qwen3.8-max-0902** (S4, dataflow_inconsistency, app/execution/broker_adapter.py:481-502): CANCELLED_PART 被当作普通 submitted 返回且未回传 dealt_qty
  - 证据引用: `elif 'CANCELLED_PART' in status:
    logger.warning(f"⚠️ [部分成交后取消] {order.symbol} Status={status}, dealt={data_q.iloc[0].get('dealt_qty', 0)}")
...
return {
    "order_id": order.order_id,
    "futu_order_id": futu_id,
    "futu_order_ids": [futu_id],
    "status": "submitted",
}`
  - 影响声称: 部分成交后取消的订单仍返回 status='submitted'，且返回结构不包含 dealt_qty；上游可能按完整订单已提交处理，导致仓位、预算、风控占用与实际部分成交数量不一致。
  - 建议: 对 CANCELLED_PART 返回明确的 partial_filled/canceled 状态，并携带 dealt_qty 与剩余数量，让订单状态机和持仓同步按部分成交处理。

<!-- DEB-VERDICT cluster=184 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: 两个子问题均成立：(1) L481-482 CANCELLED_PART 仅 logger.warning，随后 L497-502 返回 {status:submitted} 且不含 dealt_qty，调用方误以为整单成功提交，部分成交信息丢失→错单/漏单。(2) L477 字符串匹配 _is_full_cancel='CANCEL' in status and 'PART' not in status 对 CANCELLING_ALL 状态（L1203-1204 证实为合法中间态，映射 CANCEL_REQUESTED）也为 True，触发 L480 raise RuntimeError 把撤单进行中误判为已取消失败。未在旧基线中。

### 议题 #191 [共性 2/2] `app/risk/position_exit_monitor.py`:577-592 — 组合回撤取价返回 0/None 时静默跳过，不计入取价异常

环节: s7_risk_execution · 类别: dataflow_inconsistency, error_swallowing · 自报最高 severity: S4
- **kimi-k3** (S4, dataflow_inconsistency, app/risk/position_exit_monitor.py:579-582): 组合回撤 PnL 的方向判断把字符串 'SHORT' 误判为 LONG, 空头盈亏符号反转
  - 证据引用: `L579: `if getattr(p, 'direction', None) and getattr(p.direction, 'value', 'LONG') == 'LONG':` — 对比同文件 L800 的防御写法 `_dir_str = _dir.value if hasattr(_dir, 'value') else str(_dir) if _dir else 'LONG'``
  - 影响声称: 触发路径: 任一 managed 空头持仓其 direction 属性为纯字符串 'SHORT' (非枚举) 时, getattr('SHORT','value','LONG') 落到默认值 'LONG' → 走多头分支 `(price - entry)*size`。结果: 空头亏损(价格上涨)被算成盈利 → 组合回撤被低估, FLAT_ALL/REDUCE_50 该触发不触发; 空头盈利(价格下
  - 建议: 抽取统一的方向归一化 helper (同 L800 逻辑), _check_portfolio_drawdown 与 FLAT_ALL/REDUCE_50 的 pnl 计算共用, 禁止再内联 getattr(...,'value','LONG')。
- **qwen3.8-max-0902** (S4, dataflow_inconsistency, app/risk/position_exit_monitor.py:579-582): 组合止损 PnL 对字符串方向 SHORT 误判为 LONG
  - 证据引用: `if getattr(p, 'direction', None) and getattr(p.direction, 'value', 'LONG') == 'LONG':
    total_pnl += (price - p.avg_entry_price) * getattr(p, 'size', 0)
else:
    total_pnl += (p.avg_entry_price - price) * getattr(p, 'size', 0)`
  - 影响声称: 若 position.direction 是字符串 'SHORT'，由于字符串没有 .value 属性，getattr(p.direction, 'value', 'LONG') 会返回默认值 'LONG'，导致空头仓位按多头公式计算盈亏；空头亏损可能被记成盈利，从而低估组合回撤，漏触发 FLAT_ALL/REDUCE_50，或在空头盈利时误触发减仓。
  - 建议: 统一方向归一化后再比较，例如沿用本文件 800/833 行方式：_dir_str = _dir.value if hasattr(_dir, 'value') else str(_dir) if _dir else 'LONG'，再判断 _dir_str == 'LONG'。
- **qwen3.8-max-0902** (S3, error_swallowing, app/risk/position_exit_monitor.py:577-592): 组合回撤取价返回 0/None 时静默跳过，不计入取价异常
  - 证据引用: `price = get_price_fn(p.symbol)
if price and price > 0:
...
except Exception as _silent_e:
    _pnl_errors += 1
...
if _pnl_errors > 0:`
  - 影响声称: get_price_fn 返回 0 或 None 时仓位被静默排除，但 _pnl_errors 只在异常时增加；行情缺失但函数返回 0 的场景不会触发不完整数据告警，组合回撤可能被系统性低估，漏触发 FLAT_ALL/REDUCE_50。
  - 建议: 将 price<=0/None 也视为取价异常，计入 _pnl_errors，并在关键风控路径上 fail-closed 或显式降低风控置信度。

<!-- DEB-VERDICT cluster=191 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: L579 getattr(p.direction,'value','LONG')=='LONG'：当 p.direction 是字符串 'SHORT'（非枚举）时，str 无 .value 属性，getattr 返回默认 'LONG'，导致空头仓位被当作多头计算 PnL，盈亏符号反转。对比同文件 L800 的防御写法 _dir.value if hasattr(_dir,'value') else str(_dir) 可见此处缺少 str() fallback。后果：组合回撤计算对空头仓位盈亏方向错误，可能漏触发或误触发止损。另 L577-578 price=0/None 时静默跳过不计入 _pnl_errors，可低估回撤。未在旧基线中（#105 修了异常追踪但未修方向判断）。

### 议题 #186 [共性 2/2] `app/execution/broker_adapter.py`:707-707 — 持仓查询 current_price 用 max(1, qty) 做分母会扭曲小数/负数持仓价格

环节: s7_risk_execution · 类别: numeric_unit · 自报最高 severity: S3
- **kimi-k3** (S3, numeric_unit, app/execution/broker_adapter.py:707-707): get_positions 对空头持仓计算 current_price 错误 (max(1, qty) 把负 qty 变成 1)
  - 证据引用: ``"current_price": float(row.get("market_val", 0)) / max(1, float(row.get("qty", 1))),``
  - 影响声称: 触发路径: 系统经 try_direct_short 持有空头后调用 get_positions()。Futu 空头 qty 为负(如 -100), max(1, -100)=1, current_price = market_val/1 = 整仓市值(且为负), 例如 -$5,000 被当作'每股价格'。任何消费该字段做估值/盈亏/退出判断的下游都会拿到数量级错误且符号错误的负价格。
  - 建议: 改为 `qty_abs = abs(float(row.get('qty', 0))); current_price = market_val/qty_abs if qty_abs > 0 else 0.0`, 并对 qty==0 行跳过。
- **qwen3.8-max-0902** (S3, numeric_unit, app/execution/broker_adapter.py:707-707): 持仓查询 current_price 用 max(1, qty) 做分母会扭曲小数/负数持仓价格
  - 证据引用: `"current_price": float(row.get("market_val", 0)) / max(1, float(row.get("qty", 1))),`
  - 影响声称: 当 qty 小于 1、为 0 或为负数时，max(1, qty) 会错误地用 1 做分母；若 market_val 带方向符号，负数量/小数仓位会得到错误的 current_price，进而污染持仓同步、风控取价或盈亏计算。
  - 建议: 使用真实数量并显式处理符号与零：若 qty==0 则价格为 0 或跳过；否则用 market_val / abs(qty)，方向由 side/qty 符号单独表达。

<!-- DEB-VERDICT cluster=186 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: L707 current_price = market_val / max(1, qty)：当 qty 为负（空头持仓，如 -100）时 max(1,-100)=1，current_price=market_val（应为 market_val/|qty|）；当 qty 为小数（如 0.5 股）时同理扭曲。该值用于持仓展示和可能的下游风控判断，价格计算错误属功能缺陷。未在旧基线中。

### 议题 #192 [共性 2/2] `app/risk/position_exit_monitor.py`:792-806 — FLAT_ALL 锁存在执行前置位，平仓失败后 30 分钟内不再重试

环节: s7_risk_execution · 类别: logic_anomaly, state_machine · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, app/risk/position_exit_monitor.py:803-804): FLAT_ALL/REDUCE_50 不检查 execute_close_fn 返回值, 平仓失败仍计入 closed 列表
  - 证据引用: `L803-804: `execute_close_fn(p, {"reason": "PORTFOLIO_DRAWDOWN_HARD_STOP", "close_ratio": 1.0})` / `closed.append({"symbol": p.symbol, "reason": "PORTFOLIO_DRAWDOWN_HARD_STOP", "pnl_pct": _pnl})`; REDUCE_50 同样模式见 L836-837`
  - 影响声称: 触发路径: 组合回撤触发 FLAT_ALL, 某持仓 execute_close_fn 返回 False (如 broker 拒单、持仓校验失败 — 不抛异常)。该仓位仍被 append 进 closed, scan_positions 的调用方按 docstring 'Returns: 已平仓的持仓列表' 认为其已平, 可能跳过后续补救/告警; 实际仓位滞留, 回撤继续扩大。只有抛异常的失败才被
  - 建议: `ok = execute_close_fn(...); if ok: closed.append(...)`; 失败时单独记录 failed 列表并 logger.error, 供调用方重试或升级告警。
- **qwen3.8-max-0902** (S3, state_machine, app/risk/position_exit_monitor.py:792-806): FLAT_ALL 锁存在执行前置位，平仓失败后 30 分钟内不再重试
  - 证据引用: `if portfolio_action == "FLAT_ALL":
    self._flat_all_last_triggered_ts = time.time()
    logger.critical("[组合止损] 触发 FLAT_ALL, 全平所有持仓")
    for p in managed_positions:
        try:
...
        except Exception as _e:
            logger.error(f"[组合止损] 平仓 {p.symbol} 失败: {_e}")
    return closed`
  - 影响声称: FLAT_ALL 冷却时间戳在实际平仓执行前写入；若部分或全部平仓因 broker 断连、拒单等原因失败，系统仍会进入 30 分钟锁存，紧急全平失败后无法及时重试，可能让高风险仓位继续暴露。
  - 建议: 将锁存改为仅在所有平仓动作成功提交后生效，或对失败标的单独记录并允许短周期重试，同时保留全局防抖。

<!-- DEB-VERDICT cluster=192 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: 两个子问题均成立：(1) L803 execute_close_fn(p,{...}) 调用后 L804 直接 closed.append，不检查返回值；据 L698 签名 ->bool 及 L2051 success=execute_close_fn(...) 可知返回值有意义，此处忽略导致平仓失败仍计入 closed 列表→状态错乱。(2) L793 self._flat_all_last_triggered_ts=time.time() 在 for 循环之前设置，即使所有平仓都失败（L805-806 except），30 分钟冷却期（L784 FLAT_ALL_COOLDOWN_SECONDS）仍生效，失败后 30 分钟内不重试。属错单/漏单级别。未在旧基线中。

### 议题 #193 [共性 2/2] `app/risk/position_exit_monitor.py`:946-952 — Playbook 护栏加载失败时逐仓路径降级为不过滤，与组合层安全跳过不一致

环节: s7_risk_execution · 类别: error_swallowing, logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S2, logic_anomaly, app/risk/position_exit_monitor.py:946-952): 逐仓 playbook 护栏异常时降级为不过滤 (fail-open), 与 R-7c 组合层 fail-closed 口径相反
  - 证据引用: `L949-952: `_pb_open_syms = set()` / `_pb_check_em = lambda symbol, _syms: False  # noqa: E731 — 降级: 不过滤`; 对比 L746-752 R-7c 同模块导入失败时 `_pb_filter_ok = False` 并整段跳过组合止损`
  - 影响声称: 触发路径: load_playbook_open_symbols 运行期抛异常 (如 playbook DB 锁/损坏), 同时存在重启恢复后 owner_strategy 为空的 playbook 仓 (L959-963 注释描述的 2026-07-16 P0 场景)。此时 L969 护栏恒 False → 该仓落入主线 6 规则退出, effective_atr=0 触发 ATR 底限误平 p
  - 建议: 与 R-7c 对齐: 加载失败时本轮跳过全部逐仓退出检查 (fail-closed), 或至少对 owner 为空的仓位跳过; 并保留 error 告警。
- **qwen3.8-max-0902** (S3, error_swallowing, app/risk/position_exit_monitor.py:946-952): Playbook 护栏加载失败时逐仓路径降级为不过滤，与组合层安全跳过不一致
  - 证据引用: `try:
    from app.playbook.pipeline import load_playbook_open_symbols, playbook_open_check as _pb_check_em
    _pb_open_syms = load_playbook_open_symbols()
except Exception as _pb_e:
    logger.error(f"[退出监控] 加载 playbook 未平仓集合异常 (降级为不过滤): {_pb_e}")
    _pb_open_syms = set()
    _pb_check_em = lambda`
  - 影响声称: 当 playbook 未平仓集合加载失败时，逐仓退出路径把 _pb_check_em 降级为恒 False，不再拦截 Playbook 仓；若仓位 owner_strategy 为空或漏标记，主线退出可能误平本应由 monitor_playbook_sim_exits 自管的 Playbook 仓位。
  - 建议: Playbook 护栏不可用时应采用与组合层一致的安全侧策略：跳过疑似/无法判定的 Playbook 仓，或本轮放弃逐仓退出，而不是降级为不过滤。

<!-- DEB-VERDICT cluster=193 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: L946-952 逐仓路径 playbook 加载失败时降级为 _pb_open_syms=set() + _pb_check_em=lambda:False（不过滤=fail-open），所有仓位照常进入逐仓退出检查。对比组合层 L746-752 同一导入失败时 _pb_filter_ok=False，L778-779 整段跳过组合止损检查（fail-closed）。两层安全口径不一致：逐仓层可能误处理 playbook 自管仓（触发不该触发的退出），而组合层同时跳过。与旧 #107 有关但 #107 修了异常保护（已加 try/except），新问题是降级行为不一致。

### 议题 #208 [共性 2/2] `app/review/daily_review.py`:76-85 — DailyReview 以开仓日取数，当日平仓的隔夜仓不进入当日复盘

环节: s9_review_backtest · 类别: dataflow_inconsistency, logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, app/review/daily_review.py:79-84): 主查询 exit_reason != 'PHANTOM_RECONCILE_VOID' 把 NULL（未平仓）行一并过滤，open_trades 恒空、过夜持仓段永不渲染
  - 证据引用: `主查询: "AND exit_reason != 'PHANTOM_RECONCILE_VOID' " (line 79); 下游: 'open_trades': [t for t in trades if not (t.get('exit_price') and t.get('exit_price') > 0)] (line 110-111)。NULL 语义铁证见同包 orchestrator line 286: "FROM trade_records WHERE exit_reason IS NOT NULL" 被注释为"已平仓交易"——即未平仓行 exit_reason 为 NULL，而`
  - 影响声称: 2026-08-27 收盘审计④ 的修复（line 108-109 注释"补过夜仓行…消除口径差"）完全失效：任何持夜交易日，trades 里根本没有未平仓行，open_trades 恒为空列表，postmarket 笔记永不渲染"过夜持仓"段，summary.total_trades 也不含未平仓单，口径差依旧存在。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/review/daily_review.py:76-85): DailyReview 以开仓日取数，当日平仓的隔夜仓不进入当日复盘
  - 证据引用: `        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date = ? "
            "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
            "AND exit_reason != 'PHANTOM_RECONCILE_VOID' "
            # DEB-20260903 #127: 删除 trade_mode != 'live' 硬排除 —
            # 实盘`
  - 影响声称: 调用 DailyReviewGenerator.generate(date) 复盘某交易日时，若仓位在昨日开仓、今日平仓，trade_date 不等于今日，该笔已实现盈亏不会进入 trades/closed_trades；而同文件 _analyze_shadow_vs_real 又使用 substr(exit_time,1,10)=date 统计，导致同一日报内开仓口径与出场口径并存，当日真实成交
  - 建议: 当日已实现统计改为按出场日取数：WHERE trade_date = ? OR substr(exit_time,1,10) = ?；或明确拆分 opened_today 与 closed_today 两个口径，并让 summary/评分使用 closed_today。

<!-- DEB-VERDICT cluster=208 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 两处缺陷均成立：① line 79 SQL `exit_reason != 'PHANTOM_RECONCILE_VOID'` 在 SQLite 中 NULL != 常量 = NULL(≈FALSE)，实测确认 NULL 行被过滤，导致 line 110-111 open_trades 恒为空，过夜持仓段永不渲染；② line 77 仅按 trade_date(开仓日) 取数，未像已修复的 review_orchestrator.py:104 那样补 `OR substr(exit_time,1,10)=?` 出场日匹配，多日持仓在出场日复盘不可见（同旧 DEB #131 修而不彻，仅修 collect_daily 未修 daily_review 自身查询）。

### 议题 #205 [共性 2/2] `app/reports/feishu_push_service.py`:128-136 — 飞书服务在 SDK/APP 凭据缺失时直接返回 None，阻断 Webhook 降级

环节: s9_review_backtest · 类别: config_effect, logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S2, logic_anomaly, app/reports/feishu_push_service.py:132-136): 飞书工厂在 SDK 缺失时直接返回 None，架空 send_text 已有的 webhook 回退路径
  - 证据引用: `工厂: "if not SDK_AVAILABLE:\n    logger.error(\"❌ 飞书 SDK 未加载\")\n    return None" (line 132-134); 但 send_text 本有回退: "if not SDK_AVAILABLE:\n    # 回退到 webhook 方式\n    return self._send_via_webhook(text)" (line 44-46)，且 webhook 仅需 "webhook_url = os.getenv(\"FEISHU_WEBHOOK_URL_US\")" (line 54)，不依赖 SDK/a`
  - 影响声称: lark_oapi 导入失败（line 19-20 仅 warning）的环境中，get_feishu_push_service() 恒 None，复盘/告警推送整体静默丢失——尽管 webhook 通道完全可用。
- **qwen3.8-max-0902** (S3, config_effect, app/reports/feishu_push_service.py:128-134): 飞书服务在 SDK/APP 凭据缺失时直接返回 None，阻断 Webhook 降级
  - 证据引用: `        if not app_id or not app_secret or not chat_id:
            logger.warning("⚠️ 飞书配置不完整")
            return None
        
        if not SDK_AVAILABLE:
            logger.error("❌ 飞书 SDK 未加载")
            return None`
  - 影响声称: send_text 声明可通过 FEISHU_WEBHOOK_URL_US 走 Webhook，但 get_feishu_push_service 在 SDK 未加载或缺少 APP_ID/SECRET/CHAT_ID 时返回 None。只配置 Webhook 的部署将拿不到服务实例，告警/复盘推送直接失败。
  - 建议: 区分 SDK 消息与 Webhook 消息：当 FEISHU_WEBHOOK_URL_US 存在时允许构造 webhook-only service；或将 Webhook 发送抽成独立函数，不依赖 SDK/APP 凭据。

<!-- DEB-VERDICT cluster=205 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: get_feishu_push_service() 工厂在 line 132-134 检查 SDK_AVAILABLE 为 False 时直接 return None，阻止了 FeishuChatService 实例化；但 send_text (line 44-46) 和 _send_via_webhook (line 51-78) 均不依赖 SDK（用 requests 直发 webhook），本可独立工作。SDK 缺失但 FEISHU_WEBHOOK_URL_US 已配时，webhook 降级路径被工厂架空，推送静默丢失。属监控通道失效，非交易核心路径。

### 议题 #204 [共性 2/2] `app/backtest/walk_forward.py`:152-183 — WalkForwardEngine 训练/测试各自调用同一 signal_generator，无参数传递机制——'测试集用训练集参数跑'不成立，OOS 实为 IS（前视）；param_stability 恒 1.0 假读数

环节: s9_review_backtest · 类别: logic_anomaly, time_boundary · 自报最高 severity: S4
- **kimi-k3** (S4, logic_anomaly, app/backtest/walk_forward.py:152-155): WalkForwardEngine 训练/测试各自调用同一 signal_generator，无参数传递机制——'测试集用训练集参数跑'不成立，OOS 实为 IS（前视）；param_stability 恒 1.0 假读数
  - 证据引用: `"# 训练集: 找到最优参数 (简化: 直接跑)" / "is_pnl = signal_generator(train_bars)" / "# 测试集: 用训练集参数跑" / "oos_pnl = signal_generator(test_bars)" (line 152-155)——契约 callable(bars_slice)->pnl_list 没有参数出口/入口，WindowResult.optimal_params 从未被填充; "# 参数稳定性 (简化: 暂时返回 1.0)" / "param_stability = 1.0" (line 182-183)。`
  - 影响声称: 任何在切片内部调参的信号生成器都会在测试集上重新拟合→OOS Sharpe 与 degradation_ratio 系统性偏乐观，过拟合参数可经 StatisticalGate 反哺实盘；param_stability 恒 1.0 使"参数跨窗口稳定性"检查形同虚设；另 is_sharpe 为负时 degradation=oos/is 符号失真（line 180）。
- **qwen3.8-max-0902** (S4, time_boundary, app/backtest/walk_forward.py:153-183): Walk-forward OOS 未绑定训练参数，且参数稳定性恒为 1.0
  - 证据引用: `            is_pnl = signal_generator(train_bars)
            # 测试集: 用训练集参数跑
            oos_pnl = signal_generator(test_bars)
        # 参数稳定性 (简化: 暂时返回 1.0)
        param_stability = 1.0`
  - 影响声称: 注释声称测试集应使用训练集参数，但代码只是再次调用同一个 signal_generator(test_bars)，没有从训练集提取/传递最优参数。若 signal_generator 内部会根据传入 bars 做参数寻优或状态学习，OOS 结果实际上是在 OOS 数据上重新拟合，产生前视偏差；同时 param_stability 恒为 1.0 会掩盖参数不稳定问题，可能让统计门禁误判通过。
  - 建议: 定义 signal_generator(bars, params) 或 optimizer(train_bars)->params；训练阶段产出 params，测试阶段强制使用 train_params，并真实计算跨窗口参数稳定性。

<!-- DEB-VERDICT cluster=204 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: L152-155 训练集和测试集各自调用 signal_generator(bars) 但 callable 签名为 (bars_slice)->pnl_list，无参数导出/传递机制——注释称'用训练集参数跑'但代码无法实现，OOS 与 IS 跑的是同一函数同一参数，非真正样本外验证。L183 param_stability=1.0 硬编码恒为常数，参数稳定性指标为假读数。Walk-forward 分析结果无实际参考价值，属功能错误。未在旧基线中。

### 议题 #214 [共性 2/2] `app/review/review_orchestrator.py`:161-194 — collect_weekly/collect_monthly 仍只按进场日归集，#131 的出场日补匹配只修了 daily

环节: s9_review_backtest · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **kimi-k3** (S3, dataflow_inconsistency, app/review/review_orchestrator.py:161-164): collect_weekly/collect_monthly 仍只按进场日归集，#131 的出场日补匹配只修了 daily
  - 证据引用: `#131 修复声明: "-- DEB-20260903 #131: 原仅按 trade_date(开仓日) 归集, 多日持仓在出场日 复盘不可见 — 补出场日匹配" (line 101-103, collect_daily); 未修: "SELECT * FROM trade_records WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date" (line 162, weekly) 与 "SELECT * FROM trade_records WHERE trade_date >= ? AND trade_date < ?" (line 1`
  - 影响声称: 跨日持仓若进场日在周/月区间外、出场日在区间内，周/月复盘完全看不到该笔交易及其盈亏；且周度逐日分布按毛额计，与日复盘净额数字对不上。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/review/review_orchestrator.py:162-194): 周/月复盘仍按开仓日取数，未同步日复盘已修正的出场日口径
  - 证据引用: `"SELECT * FROM trade_records WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date"
"SELECT * FROM trade_records WHERE trade_date >= ? AND trade_date < ? ORDER BY trade_date"`
  - 影响声称: collect_weekly/collect_monthly 只按 trade_date 归集；跨周或跨月持仓在期末/月内平仓时，若开仓日不在本周期内，closed_trades 与已实现盈亏不会计入该周/月复盘。与 collect_daily 已声明补出场日匹配的口径不一致，导致日/周/月统计不可加总。
  - 建议: 周/月 closed 统计同样增加出场日匹配：WHERE trade_date BETWEEN ? AND ? OR substr(exit_time,1,10) BETWEEN ? AND ?；或统一提供一个按会计日取数的查询函数。

<!-- DEB-VERDICT cluster=214 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: DEB-20260903 #131 在 collect_daily (line 101-104) 补了出场日匹配 `OR substr(tr.exit_time,1,10)=?`，但 collect_weekly (line 162 `WHERE trade_date BETWEEN ? AND ?`) 和 collect_monthly (line 194 `WHERE trade_date >= ? AND trade_date < ?`) 仍仅按 trade_date(开仓日) 归集，多日持仓在出场周/月复盘中不可见。同旧 #131 修而不彻，仅修 daily 未修 weekly/monthly。

### 议题 #215 [共性 2/2] `app/review/review_orchestrator.py`:283-297 — RichContextBuilder/_verify_trend_evidence 仍用毛额 realized_pnl + 进场日口径，与同文件已声明的净额/出场日口径自相矛盾

环节: s9_review_backtest · 类别: dataflow_inconsistency, numeric_unit · 自报最高 severity: S2
- **kimi-k3** (S2, dataflow_inconsistency, app/review/review_orchestrator.py:283-297): RichContextBuilder/_verify_trend_evidence 仍用毛额 realized_pnl + 进场日口径，与同文件已声明的净额/出场日口径自相矛盾
  - 证据引用: `同文件声明: "# DEB-20260903 #131: 净口径 COALESCE(pnl_net, realized_pnl) — 与同文件 :2012/:2137/:2208 净口径一致 (原 realized_pnl 毛额不一致)" (line 117-118); 未改处: "SELECT symbol, direction, realized_pnl, pnl_pct ... AND trade_date >= date(?, '-30 days')" (line 284-287), "ROUND(SUM(realized_pnl), 2) as total_pnl" (line 47`
  - 影响声称: 喂给多模型辩论的 30 天历史模式、14 天逐日趋势、趋势证据均按毛额+进场日计，与 collector 的净额+出场日 by_source 数字对不上（毛赢净亏单在一处计胜、另一处计负），辩论在自相矛盾的统计上归因。
- **qwen3.8-max-0902** (S2, numeric_unit, app/review/review_orchestrator.py:296-297): RichContext 30 天历史模式仍用 realized_pnl 毛口径
  - 证据引用: `        wins = sum(1 for t in trades if t.get("realized_pnl", 0) > 0)
        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades)`
  - 影响声称: daily_review 已声明 pnl_net 为对外净口径并回退 realized_pnl，但 RichContextBuilder._build_historical_patterns 仍用 realized_pnl 判断胜负和汇总盈亏。若存在毛盈利但费用后净亏损的交易，辩论上下文会把它算成胜场，与正式复盘/账户净口径不一致。
  - 建议: 统一改为 COALESCE(pnl_net, realized_pnl)：SQL 中 SELECT COALESCE(pnl_net, realized_pnl) AS pnl，Python 侧按该字段统计。

<!-- DEB-VERDICT cluster=215 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: line 117-118 注释声明净口径 `COALESCE(pnl_net, realized_pnl)` (DEB-20260903 #131 修复)，但 _build_historical_patterns (line 284) SQL 仍 SELECT realized_pnl，line 296 用 `t.get('realized_pnl',0)>0` 判胜，line 297 用 `t.get('realized_pnl',0)` 累总盈亏，均为毛额口径，与同文件声明的净口径自相矛盾。30 天历史模式的胜率/总盈亏偏差约 $5/笔 (费差)。

### 议题 #211 [共性 2/2] `app/review/daily_review.py`:1186-1261 — pnl_outliers/signal_arbitration 用 date(...,'unixepoch','localtime') 按服务器 CST 日期匹配 ET 复盘日

环节: s9_review_backtest · 类别: time_boundary · 自报最高 severity: S2
- **kimi-k3** (S2, time_boundary, app/review/daily_review.py:1186-1191): pnl_outliers/signal_arbitration 用 date(...,'unixepoch','localtime') 按服务器 CST 日期匹配 ET 复盘日
  - 证据引用: `"FROM pnl_outliers WHERE date(detected_at,'unixepoch','localtime')=? " (line 1188); "SELECT COUNT(*) FROM signal_arbitration WHERE date(timestamp,'unixepoch','localtime')=?" (line 1261, 同样模式 1266/1272); 对照同文件已确认的事实: "# DEB-20260903 #130: last_update 为 CST 裸时间戳 ... 服务器 CST 下美股盘中 12:00-16:00 ET 对应 CST`
  - 影响声称: 服务器 CST 时，ET 盘中/盘后（CST 次日凌晨）产生的 PnL 离群值与仲裁记录被归到 CST 次日，对 ET 复盘日 date 的查询落空或错归前一天——同文件已修过两处同类问题，这两处漏修。
- **qwen3.8-max-0902** (S2, time_boundary, app/review/daily_review.py:1188-1261): pnl_outliers/coordinator 使用 localtime 解析 Unix 时间，与 ET 墙钟口径不一致
  - 证据引用: `"FROM pnl_outliers WHERE date(detected_at,'unixepoch','localtime')=? "
"SELECT COUNT(*) FROM signal_arbitration WHERE date(timestamp,'unixepoch','localtime')=?"`
  - 影响声称: 系统其他位置明确以 ET 墙钟日作为当日口径，而这里用 SQLite localtime 解释 Unix 时间。若服务器位于 CST/UTC，ET 盘中生成的记录可能被归到错误日期，导致当日 outlier/arbitration 统计漏计或串日。
  - 建议: 不要依赖 localtime；在 Python 侧用 ET 时区转换 detected_at/timestamp，或落库时直接保存 ET date 字段并按该字段查询。

<!-- DEB-VERDICT cluster=211 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: line 1236 `date(detected_at,'unixepoch','localtime')=?` 和 line 1309 `date(timestamp,'unixepoch','localtime')=?` 用 SQLite localtime 修饰符将 UTC 时间戳转为服务器本地时区日期，而传入的 date 参数是 ET 日期。若服务器在 CST(UTC+8)，ET 20:00 后的事件会被归入次日，与复盘日错位。同文件 _analyze_shadow_vs_real (line 1201) 已改用 substr(exit_time,1,10) 规避此问题，但 pnl_outliers 和 signal_arbitration 查询未同步修复。属分析诊断层时区不一致。

### 议题 #212 [共性 2/2] `app/review/daily_review.py`:1366-1368 — Obsidian 明细 pnl 回退未处理 pnl_net 为 NULL 的情况

环节: s9_review_backtest · 类别: dataflow_inconsistency, logic_anomaly · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, app/review/daily_review.py:1366-1368): Obsidian 明细映射 t.get('pnl_net', 默认) 对 NULL 值不回退，旧行 pnl 输出 None
  - 证据引用: `'pnl': t.get('pnl_net', t.get('realized_pnl', 0)) (line 1367)——dict.get 的 default 不防"键存在但值为 None"; 同文件正确写法: "net_pnls = [(t.get('pnl_net') if t.get('pnl_net') is not None else (t.get('realized_pnl', 0) or 0))" (line 846)，且 line 845 注释承认"旧行无 pnl_net"。`
  - 影响声称: pnl_net 为 NULL 的旧交易在 postmarket 笔记明细中 pnl=None，而 frontmatter 的 total_pnl_net 走了正确回退——明细加总≠总数，正是 line 1341-1342 注释要消除的口径问题在边界行上复发。
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/review/daily_review.py:1366-1368): Obsidian 明细 pnl 回退未处理 pnl_net 为 NULL 的情况
  - 证据引用: `                'trades': [{'code': str(t.get('symbol', '') or ''),
                            'pnl': t.get('pnl_net', t.get('realized_pnl', 0)),
                            'id': str(t.get('id', i))}`
  - 影响声称: dict.get('pnl_net', default) 在键存在但值为 None 时返回 None，不会回退到 realized_pnl。若 trade_records.pnl_net 为 NULL 而 realized_pnl 有值，Obsidian 盘后笔记会写入 pnl=None，造成明细缺数或渲染/汇总异常。
  - 建议: 使用与 summary 相同的 COALESCE 语义：pnl = t.get('pnl_net') if t.get('pnl_net') is not None else (t.get('realized_pnl', 0) or 0)。

<!-- DEB-VERDICT cluster=212 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: line 1415 `t.get('pnl_net', t.get('realized_pnl', 0))` — dict.get 的 default 仅在键缺失时生效，若 pnl_net 键存在但值为 None（旧行常见），返回 None 而非 realized_pnl 回退值，导致 Obsidian 明细行 pnl 输出 None。同文件正确写法见 line 894 `t.get('pnl_net') if t.get('pnl_net') is not None else ...` 和 review_orchestrator.py:119 同款。属净口径迁移遗漏。

### 议题 #219 [共性 2/2] `app/review/review_orchestrator.py`:2089-2124 — 漏斗验证仍读已停写的 decisions.db，未切换到 playbook_instructions

环节: s9_review_backtest · 类别: dataflow_inconsistency, time_boundary · 自报最高 severity: S3
- **kimi-k3** (S3, time_boundary, app/review/review_orchestrator.py:2118-2119): DEB-20260903 #132 只修了 _validate_execution：_validate_data_integrity 与 _verify_log_evidence 仍用 now_et() 当日而非复盘 date 匹配日志
  - 证据引用: `修复声明: "# DEB-20260903 #132: 用传入复盘 date 而非当日 — 原 now_et() 当日使 历史复盘日志匹配永远落空, 验证结果必然错误" (line 2064-2065, _validate_execution 内); 未修的同款: _validate_data_integrity 中 "today = _lg_now_et().strftime(\"%Y-%m-%d\")" (line 2119) 与 _verify_log_evidence 中 "today = _lg_now_et().strftime(\"%Y-%m-%d\")" (line 2253)`
  - 影响声称: 对历史日期复盘时，data_integrity 类和日志证据类 claim 的匹配窗口永远落在"今天"：今天无相关日志则历史真问题 verified=False；今天有无关 error/timeout 日志则历史 claim 被误 verified=True，ValidationAgent 的证据背书系统性失真。
- **kimi-k3** (S3, dataflow_inconsistency, app/review/review_orchestrator.py:2089-2094): _validate_funnel/_verify_db_evidence 仍查询 07-12 已停写的 decisions.db，漏斗类 claim 永远 'no decision data'
  - 证据引用: `采集层已迁移: "# 决策漏斗 (2026-08-01: decisions.db 07-12 停写恒 0 → 改读 playbook_instructions, 与 daily_review 同源)" (line 138); 验证层未迁移: "FROM decisions WHERE date(created_at) = ?", (date,), db_path=self.decisions_db (line 2092-2093)，及 _verify_db_evidence 中 "SELECT COUNT(*) as total FROM decisions WHERE date(creat`
  - 影响声称: decisions.db 自 07-12 停写恒 0，所有 funnel 类 LLM claim 的验证恒走 total=0 分支返回 verified=False/'no decision data'，辩论结论中漏斗相关的证据验证形同虚设；这是典型的"修了采集、漏了验证"的修而不彻。
- **qwen3.8-max-0902** (S2, time_boundary, app/review/review_orchestrator.py:2118-2124): _validate_data_integrity 忽略传入 date，按当日日志验证历史 claim
  - 证据引用: `                from app.utils.timezone import now_et as _lg_now_et
                today = _lg_now_et().strftime("%Y-%m-%d")  # 2026-07-28: CST→ET (日期切片口径)
                keywords = ["timeout", "connection", "refused", "failed", "error",
                           "断流", "降级", "不可用", "熔断"]
        `
  - 影响声称: 验证器接收 date 参数但实际用 now_et() 生成 today。对历史日期做复盘验证时，日志匹配的是当前日期：当前无异常会误判历史无证据，当前有异常会误算到历史 claim 上。
  - 建议: 与 _validate_execution 一样使用传入 date：today = date or _lg_now_et().strftime('%Y-%m-%d')。
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/review/review_orchestrator.py:2089-2093): 漏斗验证仍读已停写的 decisions.db，未切换到 playbook_instructions
  - 证据引用: `                decisions = self._query(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN direction != 'NEUTRAL' THEN 1 ELSE 0 END) as actionable "
                    "FROM decisions WHERE date(created_at) = ?",
                    (date,), db_path=self.decisions_`
  - 影响声称: 同文件 ReviewDataCollector 已注释 decisions.db 停写并改读 playbook_instructions，但 _validate_funnel 仍查 decisions 表。近期日期会返回 no decision data，使本可由 playbook_instructions 支持的漏斗 claim 被错误标记为未验证。
  - 建议: 将漏斗验证改为查询 playbook_instructions：SELECT COUNT(*) total, SUM(executed) ... WHERE date = ?，与 daily_review/_build_decision_funnel 同源。

<!-- DEB-VERDICT cluster=219 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 两处修而不彻：① _validate_data_integrity (line 2118-2119) 仍用 `_lg_now_et().strftime('%Y-%m-%d')` 取当日，忽略传入 date 参数，历史复盘的数据完整性验证必然落空（同旧 #132 只修了 _validate_execution line 2066，未修此方法）；② _validate_funnel (line 2089-2093) 仍查询已停写的 decisions.db (`FROM decisions WHERE date(created_at)=?`, db_path=self.decisions_db)，而采集层 line 138 已于 2026-08-01 迁至 playbook_instructions，验证层未同步迁移，漏斗 claim 恒返 'no decision data'。

## 四、独有议题 (单模型命中 — 核实其真伪与独到价值)

### 议题 #139 [独有 1/2] `ftseek_us.sh`:35-39 — restart 固定 sleep 2 后启动，可能与优雅关闭竞争导致重启失败

环节: s1_entry_config · 类别: race_condition · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, race_condition, ftseek_us.sh:35-39): restart 固定 sleep 2 后启动，可能与优雅关闭竞争导致重启失败
  - 证据引用: `    restart)
        $0 stop
        sleep 2
        $0 start
        ;;`
  - 影响声称: main.py 的优雅关闭包含 scheduler.stop()、PostTradeHook wait_sec=5、DataRouter/PushHub 关闭等步骤，2 秒内未必释放进程锁；此时 start 会因 flock 检测到已有实例而退出，造成 restart 后服务实际不可用。
  - 建议: stop 后轮询等待进程退出/锁释放，直到超时后再 start；或使用 systemd 的 Restart/ExecStopPost 保证停止完成。

<!-- DEB-VERDICT cluster=139 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: ftseek_us.sh:35-39 restart 先调 $0 stop(L36)再固定 sleep 2(L37)后 $0 start(L38)。stop 发送 pkill(L31)后立即返回，未等待进程实际退出并释放 flock(main.py:162-167 cleanup_lock)。若优雅关闭>2s(常见于持仓/订单状态落盘)，start 会因 LOCK_EX|LOCK_NB(main.py:140)失败。与同脚本 start 的锁互斥逻辑冲突。

### 议题 #140 [独有 1/2] `main.py`:129-711 — --stop/--status 只检查 /run 锁，但启动允许回退到数据目录锁

环节: s1_entry_config · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, main.py:129-711): --stop/--status 只检查 /run 锁，但启动允许回退到数据目录锁
  - 证据引用: `    _lock_candidates = ["/run/ftseek-us.lock"]
    lock_file = "/run/ftseek-us.lock"`
  - 影响声称: 当 /run 不可写时，_run_new_scheduler 会回退并使用数据目录下的 .ftseek-us.lock 继续运行；但 --stop/--status 仍只读取 /run/ftseek-us.lock，导致实际运行中的进程被误报为未运行，且 --stop 无法发送 SIGTERM，运维生命周期状态不一致。
  - 建议: 抽取共享的锁文件候选解析函数，_stop_service/_show_status 按与启动相同的候选顺序查找实际锁文件，或在获得锁后写入统一可读的 pid/lock 路径。

<!-- DEB-VERDICT cluster=140 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 启动路径 main.py:129-159 _lock_candidates=['/run/ftseek-us.lock', data_dir/'.ftseek-us.lock']，/run 不可写时回退到数据目录锁。但 _stop_service(L708-729) 和 _show_status(L732-750) 均硬编码 lock_file='/run/ftseek-us.lock'(L711,L734)，不查 fallback 路径。若服务以 fallback 锁启动，--stop 报'未在运行'并 exit(1)(L718)，--status 同理(L741)，运行中实例变孤儿。状态错乱。

### 议题 #136 [独有 1/2] `app/utils/watchlist_manager.py`:154-237 — ETF 未套用全局黑名单/自管名单过滤，黑名单对 ETF 失效

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, config_effect, app/utils/watchlist_manager.py:154-237): ETF 未套用全局黑名单/自管名单过滤，黑名单对 ETF 失效
  - 证据引用: `    def get_etfs(self) -> List[Dict[str, Any]]:
        """获取启用的ETF列表"""
        etfs = self._watchlist_data.get("etfs", [])
        return [e for e in etfs if e.get("enabled", False)]
    def get_all_enabled(self) -> List[Dict[str, Any]]:
        """获取所有启用的标的 (股票+ETF)"""
        stocks = self.get_e`
  - 影响声称: get_enabled_stocks() 会过滤 risk.global_blacklist 与 risk.self_managed_symbols，但 get_etfs() 不做同样过滤；get_stock_codes()/get_all_enabled() 将未过滤 ETF 合入扫描池。若黑名单包含 ETF（如 US.SPY/US.QQQ），风险黑名单会被绕过。
  - 建议: 将全局黑名单与 self_managed 过滤抽取为通用函数，对 stocks 与 ETF 同样执行；或在 get_all_enabled() 层统一过滤。

<!-- DEB-VERDICT cluster=136 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: watchlist_manager.py:154-157 get_etfs 仅过滤 enabled 字段，未调用 _get_global_blacklist(L73-85) 和 risk.self_managed_symbols(L113-122) 过滤。对比 get_enabled_stocks(L87-129) 有三重过滤(enabled+黑名单+自管)。get_all_enabled(L233-237)=stocks+etfs 拼合后，被黑名单禁止的 ETF 仍可通过 get_stock_codes(L131-142) 进入扫描/交易流程。风控门禁对 ETF 类标的失效，属错单/风控失效。

### 议题 #138 [独有 1/2] `config/config_us.py`:198-219 — watchlist_us.json 存在时 config_us.get_watchlist 仍走 JSON，形成 YAML/JSON 双真源

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, config_effect, config/config_us.py:198-219): watchlist_us.json 存在时 config_us.get_watchlist 仍走 JSON，形成 YAML/JSON 双真源
  - 证据引用: `WATCHLIST_FILE = CONFIG_DIR / "watchlist_us.json"
if WATCHLIST_FILE.exists():
    with open(WATCHLIST_FILE, "r") as f:
        WATCHLIST_CONFIG = json.load(f)
    us_stocks = WATCHLIST_CONFIG.get("us_stocks", [])`
  - 影响声称: WATCH_LIST_US 已声明以 unified_config.yaml 为唯一真源，但只要 config/watchlist_us.json 存在，get_watchlist() 仍会返回 JSON 中的名单；若 YAML 与 JSON 不一致，消费 config_us.get_watchlist 的链路会与 WatchlistManager/YAML 链路使用不同标的池，造成扫描、风控或交
  - 建议: 删除 JSON 读取路径，或仅在 YAML 不可用时显式 fallback 并告警；确保 get_watchlist() 也通过 _loader/WatchlistManager 读取 unified_config.yaml。

<!-- DEB-VERDICT cluster=138 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: config_us.py:198-219 WATCHLIST_FILE.exists() 为真时(L199)直接从 JSON 加载 WATCHLIST_CONFIG(L200-201)，绕过 WatchlistManager(走 unified_config.yaml)。两真源可独立漂移: get_watchlist(L209)返回与 WatchlistManager.get_enabled_stocks 不一致的标的集。当前 config/watchlist_us.json 不存在(ls 确认)，走 else 分支(L203)无实质影响，但代码逻辑(L198-207)确构成双真源隐患。

### 议题 #137 [独有 1/2] `config/config_loader.py`:208-216 — 环境变量解析只递归 dict，不处理 list 内元素

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, config_effect, config/config_loader.py:208-216): 环境变量解析只递归 dict，不处理 list 内元素
  - 证据引用: `        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = self._resolve_dict_env(v)
            else:
                result[k] = self._resolve_env_vars(v)`
  - 影响声称: _resolve_env_vars 只处理字符串；当 YAML 值是 list（包括 list[dict]，如 watchlist.us_stocks）时不会递归，列表内任何 ${VAR} 占位符都会保持原样。若未来在列表元素中配置环境变量占位符，会出现字面量 ${VAR} 进入运行时。
  - 建议: 增加 list 递归：对 list 逐项调用 _resolve_env_vars/_resolve_dict_env/_resolve_list_env，确保 list[dict] 也能解析环境变量。

<!-- DEB-VERDICT cluster=137 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: config_loader.py:208-216 _resolve_dict_env 只递归 dict 分支(L212-213) 并对叶子调 _resolve_env_vars(L215)。后者仅处理 str(L200)，list 直接返回原值。YAML 中形如 `items: [${VAR1}, ${VAR2}]` 的列表元素环境变量占位符永不被解析。当前 config/unified_config.yaml 无 ${} 引用(grep 零命中)，属潜在卫生问题。

### 议题 #134 [独有 1/2] `app/config_mgmt/validator.py`:225-232 — 范围校验未先做数值类型判断，非数值配置可抛 TypeError 并使启动校验降级

环节: s1_entry_config · 类别: error_swallowing · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, error_swallowing, app/config_mgmt/validator.py:225-232): 范围校验未先做数值类型判断，非数值配置可抛 TypeError 并使启动校验降级
  - 证据引用: `        for key, (low, high, label) in range_checks.items():
            value = self.config.get(key)
            if value is None:
                continue
            if not (low <= value <= high):`
  - 影响声称: 若 risk.signal_quality.min_confidence 等键因未解析环境变量或错误配置而是字符串，_check_types 会记录错误，但随后范围比较 low <= value 会抛 TypeError；main.py 外层 except 只重新抛出 RuntimeError/ConfigAuditError，TypeError 会被当作普通异常降级运行，导致本应阻断启动的配置校
  - 建议: 范围校验前判断 isinstance(value, (int, float)) 且排除 bool，或将每个键校验包在 try/except 中并转为 validation error。

<!-- DEB-VERDICT cluster=134 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: validator.py:229 `if not (low <= value <= high)` 未对 value 做数值类型守卫。self.config.get(key) 在 YAML 误配时可能返回 str/bool，Python3 对 int<=str 抛 TypeError，使整个 _check_ranges 崩溃。同文件 _check_consistency (L234) 也无守卫但比较语义不同。属启动校验降级(卫生问题)，不直接致错单。旧基线 #4 为同文件不同缺陷(min_length)，不冲突。

### 议题 #133 [独有 1/2] `analyze_signals.py`:249-252 — signal_snapshots 为空时除以 len(data) 触发 ZeroDivisionError

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, analyze_signals.py:249-252): signal_snapshots 为空时除以 len(data) 触发 ZeroDivisionError
  - 证据引用: `    all_wins = sum(1 for r in data
                   if r['fwd_ret_t1'] * (1 if r.get('direction', 'LONG') == 'LONG' else -1) > 0)
    all_losses = len(data) - all_wins
    avg_ret = sum(r['fwd_ret_t1'] for r in data) / len(data)`
  - 影响声称: 当 signal_snapshots 表没有满足 fwd_ret_t1 IS NOT NULL 的行时，data 为空，后续 / len(data) 直接崩溃，分析脚本无法输出空结果。
  - 建议: 在计算整体胜率/平均收益前检查 if not data: print(...); return。

<!-- DEB-VERDICT cluster=133 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: analyze_signals.py line 252 `avg_ret = sum(...)/len(data)` 和 line 254 `all_wins/len(data)` 无空数据守卫。若 signal_snapshots.db 无有效行（line 91 data=[]），len(data)=0 触发 ZeroDivisionError。全文未见 `if not data` 提前退出。属 CLI 分析脚本崩溃风险，非生产交易路径。

### 议题 #135 [独有 1/2] `app/utils/timezone.py`:264-279 — get_last_trade_date 只跳过周末，不跳过美股节假日

环节: s1_entry_config · 类别: time_boundary · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, time_boundary, app/utils/timezone.py:264-279): get_last_trade_date 只跳过周末，不跳过美股节假日
  - 证据引用: `    et_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    weekday = et_naive.weekday()  # 0=Mon … 5=Sat 6=Sun
    if weekday == 6:  # Sunday → Friday (-2)
        return (et_naive - timedelta(days=2)).strftime("%Y-%m-%d")
    elif weekday == 5:  # Saturday → Friday (-1)
        return (et_naiv`
  - 影响声称: 函数名与注释均表达“最近一个美股交易日”，但实现未调用 holiday_calendar；在节假日当天或节假日后一天调用会把节假日当作交易日/上一交易日，导致盘前预检、审计或日计数使用错误交易日。
  - 建议: 从当前日期开始回退，直到 weekday < 5 且 holiday_calendar.is_us_holiday(date) 为 False；与 is_market_open 使用同一节假日真源。

<!-- DEB-VERDICT cluster=135 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: timezone.py:264-279 get_last_trade_date 仅按 weekday 跳周末(L275-278)，未调用同仓已有的 holiday_calendar.is_us_holiday。对比同文件 is_market_open(L163-164)已接入 holiday_calendar，口径不一致。节假日后一天(如 2026-07-06 周一=独立日次日)会返回节假日当天作为'上一交易日'，导致盘前预检/盘后审计(scripts/pre_market_check.py:221, scripts/daily_alert_summary.py:378)查错日期数据。属功能错误。

### 议题 #141 [独有 1/2] `main.py`:760-760 — --test 连通性检查仍使用裸 int() 解析 FUTU_OPEND_PORT

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, main.py:760-760): --test 连通性检查仍使用裸 int() 解析 FUTU_OPEND_PORT
  - 证据引用: `    port = int(os.environ.get("FUTU_OPEND_PORT", 11111))`
  - 影响声称: _run_new_scheduler 已修复 FUTU_OPEND_PORT 异常值解析失败回退 11111，但 --test 入口仍直接 int()；若环境变量被写成非数字，main.py --test 会抛 ValueError 而不是输出连通性检查结果。
  - 建议: 复用与 _run_new_scheduler 相同的安全解析逻辑，异常时回退 11111 并告警。

<!-- DEB-VERDICT cluster=141 -->
裁决: REJECTED | 置信=high | 理由: main.py:760 `port = int(os.environ.get('FUTU_OPEND_PORT', 11111))` — 默认值 11111 是安全整数，int() 仅在环境变量被显式设为非数值字符串时才抛 ValueError，属运维配置错误而非代码缺陷。--test 是手动诊断工具(--test 实现 L753)，不进入生产交易路径，且异常会立即暴露给运维人员。与'裸 int() 不安全'的泛化批评不符实际运行场景。

### 议题 #147 [独有 1/2] `app/data/split_event_guard.py`:73-82 — 仅凭价格变化超过 30% 就自动标记拆股，真实跳空/暴跌会被误判并跳过信号与风控

环节: s2_market_data · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, logic_anomaly, app/data/split_event_guard.py:73-82): 仅凭价格变化超过 30% 就自动标记拆股，真实跳空/暴跌会被误判并跳过信号与风控
  - 证据引用: `change = abs(current_price - prev_close) / prev_close
if change > 0.30:
    logger.warning(
        f"[SplitGuard·heuristic] {symbol} 价格突变 {change*100:.1f}% "
        f"(prev=${prev_close:.2f}, now=${current_price:.2f}) — 疑似拆股, 当日跳过信号"
    )
    self.mark_split(symbol)
    return True`
  - 影响声称: 文件头说明命中标的会跳过 scoring 并让 ExitMonitor 跳过 FLASH_EXIT。若个股因财报、退市、重大新闻出现真实 >30% 跳空或暴跌，该启发式会把它误标为拆股，导致当日信号被跳过且闪崩退出检查被绕过；注释声称 Phase A 仅告警不阻塞，但代码实际已 mark_split。
  - 建议: 将启发式降级为只记录/告警，或要求静态名单/Polygon/券商公司行动确认后才 mark_split；对 >30% 跳空至少保留 FLASH_EXIT 等风控路径。

<!-- DEB-VERDICT cluster=147 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: split_event_guard.py:75-82 仅凭 abs(current_price-prev_close)/prev_close > 0.30 就调用 mark_split (line 81)，无条件区分拆股与真实暴跌/跳空。intraday_scorer.py:310-312 在信号评估中调用 detect_split_anomaly 后立即检查 is_split_today，命中则返回 _empty_signal（跳过该 symbol 当日全部信号）。真实场景：财报暴跌 35% 会被误标为拆股，当日该标的的信号（包括退出信号）全部被抑制。注释 (line 71) 承认'Phase A 仅日志告警'，但实际 mark_split 已产生功能性影响（信号屏蔽），与注释不符。

### 议题 #148 [独有 1/2] `app/storage/daily_log_store.py`:81-105 — record_tick 每次新建 SQLite 连接且异常仅 debug 吞掉，并发锁或 IO 失败会静默丢 tick

环节: s2_market_data · 类别: error_swallowing · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, error_swallowing, app/storage/daily_log_store.py:81-105): record_tick 每次新建 SQLite 连接且异常仅 debug 吞掉，并发锁或 IO 失败会静默丢 tick
  - 证据引用: `conn = sqlite3.connect(self.db_path)
conn.commit()
except Exception as e:
    logger.debug(f"[ShadowLog] 记录失败: {e}")`
  - 影响声称: 该表用于盘后影子回放，record_tick 被调度循环频繁调用。多线程/多循环并发写入时 SQLite 可能短暂锁表；db 路径、磁盘、类型适配异常也会导致 INSERT 失败。当前仅 logger.debug 且无重试、无计数、无降级路径，tick 会静默丢失，导致影子回放数据缺口且难以诊断。
  - 建议: 增加 busy_timeout/WAL、失败计数与节流告警；对可重试错误做有限重试，或写入内存队列批量落库；至少把失败提升到可观测指标。

<!-- DEB-VERDICT cluster=148 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: 修而不彻 (旧 DEB-20260903 #28)。daily_log_store.py:96-99 的 conn.commit() 修复了未提交 INSERT 回滚问题，但 line 81 仍每次新建 SQLite 连接（无连接池），line 104-105 的 except Exception 仅 logger.debug 吞掉所有异常。并发写入时 SQLite 'database is locked' 错误、磁盘 IO 失败等均被静默忽略，tick 数据丢失无任何告警或重试。作为 shadow log 非主数据路径，严重度较低，但静默丢数据仍是隐患。

### 议题 #142 [独有 1/2] `app/data/borrow_crowding.py`:150-168 — format_for_context 对可能为 None 的 pool 做强格式化，费率型信号会触发 TypeError

环节: s2_market_data · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/data/borrow_crowding.py:150-168): format_for_context 对可能为 None 的 pool 做强格式化，费率型信号会触发 TypeError
  - 证据引用: `"fee_rate": row.get("fee_rate"), "pool": row.get("pool"),
f"pool={f['pool']:,.0f}股 — {'; '.join(f['reasons'][:2])}"`
  - 影响声称: compute_signal 可以仅因 fee_rate 超过阈值产生 WATCH/WARNING/CRITICAL，此时 pool 可能缺失为 None。scan() 会把 pool=None 原样放入 flagged，format_for_context 再用 {f['pool']:,.0f} 格式化，直接抛 TypeError，导致盘前 enrichment 注入失败或上层捕获后丢失拥挤度信
  - 建议: 格式化前判断 pool 是否为数值；缺失时显示 N/A 或 0，并把 pool 缺失写入 reasons，而不是直接格式化 None。

<!-- DEB-VERDICT cluster=142 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: format_for_context (borrow_crowding.py:167-168) 对 f['pool'] 做 :,.0f 格式化，但 scan() 在 line 152 用 row.get('pool') 取值，pool 可为 None。compute_signal 仅在 fee 维度触发信号（line 87-95），pool 不参与门槛判定，故 row 缺 pool 字段时 flagged 条目 pool=None，format 时 TypeError。premarket_enrichment.py:1126 直接调用且无 try-except，整个盘前上下文生成会崩溃。fee_rate 因信号触发保证为数值不会出错，但 pool 无此保证。

### 议题 #143 [独有 1/2] `app/data/futu_lv3_provider.py`:348-356 — 健康守护只在常规交易时段检查，盘前已连接/已订阅的断线不会自动重连

环节: s2_market_data · 类别: state_machine · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, state_machine, app/data/futu_lv3_provider.py:348-356): 健康守护只在常规交易时段检查，盘前已连接/已订阅的断线不会自动重连
  - 证据引用: `if not self._is_us_market_open():
    continue
if not self._subscribed_symbols:
    continue
if not self.is_healthy(log_change=True):`
  - 影响声称: _ensure_connected 明确允许盘前连接，目的是避免重启后 subscribe 被阻塞；但 watchdog 只按 _is_us_market_open() 检查，不包含 _is_us_pre_market()。若盘前连接断开或推送停止，系统会一直等到 09:30 ET 才触发重连，盘前锚定/盘前信号会长时间使用陈旧或缺失行情。
  - 建议: watchdog 的健康检查窗口应覆盖 _is_us_pre_market() 或按市场阶段配置不同阈值；盘前至少检测连接存活与订阅恢复。

<!-- DEB-VERDICT cluster=143 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: Watchdog (futu_lv3_provider.py:352) 仅检查 _is_us_market_open()（09:30-16:00 ET，line 290-291），不覆盖盘前时段。同文件 line 374/390 证明系统在盘前也会建立连接和订阅（_is_us_pre_market 分支），但 watchdog 在盘前不运行健康检查。若盘前断连，直到 09:30 后首个 30s 周期才检测并触发 force_reconnect (line 359)，开盘初期数据可能缺失或延迟。

### 议题 #146 [独有 1/2] `app/data/market_data_router.py`:757-775 — 期权在仅拿到 quote_only 上下文后仍会触发 TwelveData 降级，违背期权跳过降级源的设计

环节: s2_market_data · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/data/market_data_router.py:757-775): 期权在仅拿到 quote_only 上下文后仍会触发 TwelveData 降级，违背期权跳过降级源的设计
  - 证据引用: `if is_option and (not ts_ctx or ts_ctx.get("price", 0) <= 0):
    logger.debug(f"[Router] {symbol} 期权代码 Futu 无数据 (非交易时段/已过期)，跳过 TwelveData")
    return None
_lv3_full = bool(ts_ctx) and ts_ctx.get("data_source") in (
    "futu_lv3", "futu_lv3_daily", "futu_lv3_1m_fallback"
)
_needs_td_fallback = not`
  - 影响声称: 期权分支只会产生 data_source=futu_lv3_quote_only 的 ts_ctx，不会进入 _lv3_full 白名单，因此 _needs_td_fallback 为 True，随后仍调用 self.td.get_full_market_context(symbol)。但前文已说明 TwelveData 不支持期权格式，这会造成无效 API 调用、错误日志和限流压力；若 TD 返
  - 建议: 对 is_option 单独短路：只要 Futu 已返回有效期权报价，直接返回 quote-only 上下文；仅在确有需要且存在期权兼容数据源时才降级。

<!-- DEB-VERDICT cluster=146 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: market_data_router.py:757-759 的期权跳过 TD 守卫仅在 ts_ctx 为 None 或 price<=0 时生效。当期权获得 quote_only 上下文 (price>0, data_source='futu_lv3_quote_only') 时，line 768-771 的 _lv3_full 白名单不含 'futu_lv3_quote_only'，导致 _needs_td_fallback=True (line 771)，line 772-788 触发 TwelveData 降级。这与 line 755-756 注释'期权代码仅 Futu 支持，跳过 TwelveData'的设计意图矛盾。TD 不支持期权符号格式，调用浪费 API 配额且可能返回垃圾数据被合并到期权上下文中 (line 777-786 merge 分支)。

### 议题 #144 [独有 1/2] `app/data/futu_lv3_provider.py`:880-885 — Push 命中路径直接返回缓存并原地修改共享对象，且缺少 change/change_percent 等契约字段

环节: s2_market_data · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/data/futu_lv3_provider.py:880-885): Push 命中路径直接返回缓存并原地修改共享对象，且缺少 change/change_percent 等契约字段
  - 证据引用: `push_data = self.get_push_quote(symbol)
if push_data:
    push_data["data_source"] = "futu_lv3_push"
    push_data["symbol"] = symbol
    return push_data`
  - 影响声称: get_push_quote 返回的是 _push_quote_cache 中的共享 dict，get_quote 直接写入 data_source/symbol，造成缓存污染且存在并发读写窗口；同时 PushQuoteHandler 写入字段只有 price/open/high/low/prev_close/volume/turnover/bid/ask/bid_vol/ask_vol/book
  - 建议: 返回 push 缓存副本；在返回前用 prev_close 补齐 change/change_percent，并统一 delay_ms/pre_price 等字段口径，避免 push/snapshot/subscribed 三条路径字段语义漂移。

<!-- DEB-VERDICT cluster=144 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: futu_lv3_provider.py:881-885 从 get_push_quote 获取缓存 dict（line 667-669 直接返回 _push_quote_cache 内引用），然后原地修改 push_data['data_source'] 和 push_data['symbol']。这污染了共享缓存对象——后续同一 symbol 的 push handler 更新会覆盖这些字段，但不同调用者可能读到被前次调用修改过的脏数据。且 get_push_quote 返回的 dict 不含 change/change_percent 等市场数据契约字段（push handler 写入的字段集取决于 handler 实现），下游若依赖这些字段会 KeyError 或得到 None。

### 议题 #145 [独有 1/2] `app/data/futu_lv3_provider.py`:2119-2207 — close/unsubscribe_all 默认退订列表遗漏 K_DAY，配置若订阅 K_DAY 会无法释放

环节: s2_market_data · 类别: resource_leak, state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/data/futu_lv3_provider.py:2163-2207): unsubscribe_all 成功时清空全部订阅状态，即使只退订部分标的或部分类型
  - 证据引用: `symbols_list = symbols or list(self._subscribed_symbols)
if failed == 0:
    with self._sub_lock:
        self._subscribed_symbols.clear()`
  - 影响声称: 若调用 unsubscribe_all(symbols=['US.AAPL']) 或 unsubscribe_all(sub_types=['QUOTE']) 且请求的退订全部成功，代码会清空整个 _subscribed_symbols，而不是只移除本次退订的标的/类型。OpenD 侧其余标的/类型仍在订阅并推送，但本地状态已遗忘：close() 不再补退订造成槽位泄漏，watchdog 看到 _
  - 建议: 成功路径只从 _subscribed_symbols 移除本次 symbols_list；若指定 sub_types，只清理对应类型级订阅/失败记录，不能无条件 clear 全量状态。
- **qwen3.8-max-0902** (S2, resource_leak, app/data/futu_lv3_provider.py:2119-2165): close/unsubscribe_all 默认退订列表遗漏 K_DAY，配置若订阅 K_DAY 会无法释放
  - 证据引用: `all_sub_types = [
    SubType.QUOTE, SubType.K_1M, SubType.K_5M,
    SubType.K_15M, SubType.K_30M, SubType.K_60M,
    SubType.ORDER_BOOK, SubType.TICKER,
]
sub_types = ['QUOTE', 'K_1M', 'K_5M', 'K_15M', 'K_30M', 'K_60M', 'ORDER_BOOK', 'TICKER']`
  - 影响声称: _futu_sub_type_map 和可配置 stock_sub_types 均支持 K_DAY；一旦配置启用 K_DAY，close() 与 unsubscribe_all() 的默认退订集合都不包含 K_DAY，导致关闭/释放槽位时 K_DAY 订阅残留在 OpenD 侧，形成订阅槽位泄漏和重启后重复订阅风险。
  - 建议: 将 K_DAY 加入 close() 的 all_sub_types 与 unsubscribe_all() 的默认 sub_types，或统一复用 _futu_sub_type_map.keys() 生成退订列表。

<!-- DEB-VERDICT cluster=145 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 修而不彻 (旧 DEB-20260903 #19)。(1) unsubscribe_all (line 2163/2203-2206): 当传入 symbols 子集且全部退订成功 (failed==0) 时，line 2206 self._subscribed_symbols.clear() 清空全部已订阅状态，而非仅移除 symbols_list 中的标的。#19 修复仅加了 failed==0 条件，未修部分 symbol 退订误清空问题。(2) close() (line 2119-2123) 和 unsubscribe_all 默认 sub_types (line 2165) 均不含 K_DAY，但 sub_type_map (line 2174) 有 K_DAY 映射。若代码某路径订阅了 K_DAY，close/unsubscribe_all 无法退订该类型，造成 OpenD 槽位泄漏。

### 议题 #150 [独有 1/2] `app/pipeline/order_executor.py`:239-273 — _refresh_broker_position_qty 把持仓查询失败/空返回误判为 broker 持仓 0

环节: s3_scheduling_pipeline · 类别: dataflow_inconsistency · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, dataflow_inconsistency, app/pipeline/order_executor.py:239-257): _refresh_broker_position_qty 把持仓查询失败/空返回误判为 broker 持仓 0
  - 证据引用: `返回 None 表示查询失败 (无法判定); 0.0 表示 broker 已无持仓.
_positions = _adapter.get_positions() or []
return 0.0  # broker 列表中无此 symbol → 持仓为 0`
  - 影响声称: 当 broker 查询失败返回 None 或短暂返回空列表时，该函数返回 0.0；_close_pre_check 会进入 broker=0 分支，删除 PositionStore 内存仓位并释放资金账本，可能把真实持仓弄成幽灵仓/账本失真。
  - 建议: 区分 get_positions() 的 None 与 []：raw is None 时返回 None；仅当确认完整持仓列表且不包含 symbol 时才返回 0.0。
- **qwen3.8-max-0902** (S4, dataflow_inconsistency, app/pipeline/order_executor.py:267-273): _query_broker_open_orders 把无法查询误报为确认无未结订单
  - 证据引用: `None 表示查询失败 (无法判定, 与"确认无未结订单"严格区分).
if not _adapter or not getattr(_adapter, "trade_ctx", None):
    return []`
  - 影响声称: 平仓前 _wait_or_cancel_open_orders 收到 [] 会直接返回安全；当 trade_ctx 断连/缺失但 broker 侧仍可能有挂单时，系统会继续发新平仓单，可能触发交叉交易或重复订单。
  - 建议: adapter/trade_ctx 不可用时应返回 None 表示不可判定，由 _wait_or_cancel_open_orders 按不安全跳过本轮；仅在明确无 broker 上下文且不可能有历史挂单时返回 []。

<!-- DEB-VERDICT cluster=150 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: 第一个声称( refresh 返回 0.0 表示失败)已被 DEB-20260903 #37 修复(line 253-256 现在返回 None)。但第二个声称成立: _query_broker_open_orders 在 line 273 当 adapter/trade_ctx 不可用时仍返回 [] 而非 None, 与 line 267 文档约定(None=查询失败)矛盾。DEB-20260903 #38 仅修复了 ImportError(line 297-300)和其他异常(line 301-303)路径, adapter 缺失路径(line 272-273)属于修而不彻的残留。后果: 调用方 _wait_or_cancel_open_orders 收到 [] 后在 line 321 判为'确认无未结订单', 在 broker 连接断开时错误放行平仓。

### 议题 #151 [独有 1/2] `app/pipeline/order_executor.py`:328-341 — _wait_or_cancel_open_orders 超时后可能迭代 None 导致 TypeError

环节: s3_scheduling_pipeline · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, logic_anomaly, app/pipeline/order_executor.py:328-341): _wait_or_cancel_open_orders 超时后可能迭代 None 导致 TypeError
  - 证据引用: `while _waited < timeout_sec:
    _open = _query_broker_open_orders(scheduler, symbol)
    if _open is None:
        continue
for _od in _open:`
  - 影响声称: 若最后一轮轮询返回 None 且超时，_open 保持 None，随后 for _od in _open 抛 TypeError，导致平仓前置检查异常中断。
  - 建议: 超时后若 _open is None 返回 (False, [])，不要进入取消循环。

<!-- DEB-VERDICT cluster=151 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 超时后 line 341 'for _od in _open' 可能迭代 None。当 while 循环最后一次 poll 返回 None(line 331)时, line 333 continue 跳过本次迭代, 但 _open 变量仍为 None。若此后循环因 _waited>=timeout_sec 退出(line 328), _open 保持 None, line 341 触发 TypeError: 'NoneType' is not iterable。此属旧 DEB-20260903 #39 同一根因(超时路径未防御 None), 当前代码未修复。触发需最后一次查询恰好失败(罕见), 但一旦触发会使平仓流程崩溃。

### 议题 #152 [独有 1/2] `app/pipeline/order_executor.py`:526-531 — manual_unblock 修改 _close_retry_state 未持 _close_retry_lock

环节: s3_scheduling_pipeline · 类别: race_condition · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, race_condition, app/pipeline/order_executor.py:526-531): manual_unblock 修改 _close_retry_state 未持 _close_retry_lock
  - 证据引用: `_dirs = [direction.upper()] if direction else ["LONG", "SHORT"]
for _d in _dirs:
    _key = (str(symbol), _d)
    if _key in _close_retry_state:
        _close_retry_state.pop(_key, None)`
  - 影响声称: Dashboard 人工解锁与平仓失败记录并发时，check-then-pop 未加锁，可能丢失退避状态或与 _record_close_failure 竞争，导致冷却/永久阻塞状态不一致。
  - 建议: manual_unblock 中清除 _close_retry_state 的读-改-写统一包在 with _close_retry_lock: 内。

<!-- DEB-VERDICT cluster=152 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: _close_retry_lock 定义于 line 90, 三处正常访问(line 151/184/225)均持锁, 但 manual_unblock 在 line 529-530 对同一 _close_retry_state 执行 in 检查 + pop 操作未持锁。注释 line 88-89 明确说明加锁理由(并发下 fail_count 复合操作可丢失增量)。manual_unblock 与正常退避写入并发时可造成数据竞态。属旧 DEB-20260903 #36 同根因的第四个遗漏访问点。因 manual_unblock 通常为运维手动调用, 并发概率低, 影响有限。

### 议题 #153 [独有 1/2] `app/pipeline/order_executor.py`:635-636 — _record_execution_metrics 使用 naive 本地时间写入 ET 链路

环节: s3_scheduling_pipeline · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/pipeline/order_executor.py:635-636): _record_execution_metrics 使用 naive 本地时间写入 ET 链路
  - 证据引用: `"signal_time": datetime.fromtimestamp(signal_time).isoformat() if signal_time else None,
"fill_time": datetime.fromtimestamp(fill_time).isoformat() if fill_time else None,`
  - 影响声称: metadata 中 signal_time/fill_time 使用服务器本地 naive 时间，而 order.filled_at/now_et 为 ET aware；跨时区部署或入库比较时会出现时区错位。
  - 建议: 使用 datetime.fromtimestamp(signal_time, tz=ET) 和 datetime.fromtimestamp(fill_time, tz=ET)。

<!-- DEB-VERDICT cluster=153 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: Line 635-636 使用 datetime.fromtimestamp(signal_time/fill_time) 生成 naive datetime, 依赖服务器本地时区。项目其他位置(如 line 354 的 now_et)明确使用 US/Eastern, 若服务器不在 ET 时区, 此处 signal_time/fill_time 与其他 ET 时间戳语义不一致, 影响下游执行指标分析精度。与旧 DEB-20260903 #22 (market_data_router 同类 naive datetime 问题)根因相同, 属同模式残留。不影响交易执行, 仅影响 metrics 数据质量。

### 议题 #154 [独有 1/2] `app/pipeline/order_executor.py`:691-697 — 实盘/模拟判定使用 AUTO_TRADE_ACCOUNT，而 DB 路由使用 TRADE_ENV，双键可漂移

环节: s3_scheduling_pipeline · 类别: config_effect · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, config_effect, app/pipeline/order_executor.py:691-697): 实盘/模拟判定使用 AUTO_TRADE_ACCOUNT，而 DB 路由使用 TRADE_ENV，双键可漂移
  - 证据引用: `# 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT (TRADE_ENV 仅控制 DB 路由)
_auto_acc_noengine = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()`
  - 影响声称: AUTO_TRADE_ACCOUNT 控制是否实盘拒单，而 trade_records DB 由 TRADE_ENV 路由；若 AUTO_TRADE_ACCOUNT=real 但 TRADE_ENV 未切实盘库，真实订单会写入模拟/默认 DB，冷却恢复、交易归因和审计读取错误数据源。
  - 建议: 统一实盘标识，DB 路由从同一 SSOT 派生，或启动时校验 AUTO_TRADE_ACCOUNT 与 TRADE_ENV 一致。

<!-- DEB-VERDICT cluster=154 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: Line 692-693 使用 AUTO_TRADE_ACCOUNT 判定实盘/模拟(决定是否 REJECTED 无引擎订单), 而 TRADE_ENV 仅控制 DB 路由(见测试 tests/core/test_trading_mode_contract.py:76/103 确认此设计)。若运维误配两变量不同步(如 AUTO_TRADE_ACCOUNT=sim + TRADE_ENV=0), 会导致模拟成交写入实盘 DB, 或实盘成交写入模拟 DB, 造成仓位/盈亏数据不一致。虽注释 line 692 记录了设计意图, 但缺少启动时一致性校验, 属配置漂移风险。

### 议题 #155 [独有 1/2] `app/pipeline/order_executor.py`:932-936 — 反手平仓忽略 _wait_or_cancel_open_orders 的不安全返回值

环节: s3_scheduling_pipeline · 类别: logic_anomaly · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, logic_anomaly, app/pipeline/order_executor.py:932-936): 反手平仓忽略 _wait_or_cancel_open_orders 的不安全返回值
  - 证据引用: `if not _close_confirmed:
    _wait_or_cancel_open_orders(scheduler, symbol, timeout_sec=30, poll_interval_sec=3)
    _remain = _refresh_broker_position_qty(scheduler, symbol)
    _close_confirmed = (_remain is not None and _remain <= 0)`
  - 影响声称: 反手平仓未确认时调用 _wait_or_cancel_open_orders 但忽略其 safe 返回值；即使返回 False（未结订单未清算/不可判定），只要 broker 持仓查询为 0 就确认并设置冷却，后续可能反向开仓，遗留挂单成交会形成幽灵仓/方向冲突。
  - 建议: safe, canceled = _wait_or_cancel_open_orders(...)；若 safe 为 False，则不得确认平仓/设置冷却，应保留重试或告警。

<!-- DEB-VERDICT cluster=155 -->
裁决: REJECTED | 置信=high | 理由: 声称'忽略 _wait_or_cancel_open_orders 的不安全返回值'部分属实: line 934 确实未用其返回值。但代码在 line 935-936 使用 _refresh_broker_position_qty 独立核实持仓, 且 line 936 '_remain is not None and _remain <= 0' 正确防御了 None(查询失败时 _close_confirmed=False, 不会误判已平仓)。_refresh_broker_position_qty 在 line 244-245/258-260 对 adapter 缺失和异常均返回 None 而非 0.0, 不存在误判 0 持仓的风险。line 937-941 在确认失败时正确走错误日志+不设置冷却路径。整体逻辑安全。

### 议题 #149 [独有 1/2] `app/core/scheduler_core.py`:3031-3044 — 延迟审计队列异常后仍切片丢弃前 10 条，失败审计不可重试

环节: s3_scheduling_pipeline · 类别: error_swallowing · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, error_swallowing, app/core/scheduler_core.py:3031-3044): 延迟审计队列异常后仍切片丢弃前 10 条，失败审计不可重试
  - 证据引用: `for item in queue[:10]:  # 最多处理 10 条，防止过载
except Exception as e:
    logger.warning(f"[LLM 审计] 延迟审计失败 | %s: %s", item.get("symbol", "?"), e)
self._deferred_audit_queue = self._deferred_audit_queue[10:]`
  - 影响声称: 延迟审计处理失败时仍按固定 10 条切片队列，失败项被丢弃且无重试，导致盘后审计数据缺失。
  - 建议: 只移除成功处理项，失败项重新入队或限制重试次数并保留可观测状态。

<!-- DEB-VERDICT cluster=149 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: scheduler_core.py:3031-3044 处理延迟审计队列时，line 3031 遍历 queue[:10]，line 3039-3040 捕获每条异常但仅 warning 后继续。line 3044 无论处理成功与否都执行 self._deferred_audit_queue = self._deferred_audit_queue[10:]，将前 10 条全部切片丢弃。若 LLM 引擎临时不可用 (line 3025-3028 有单独处理) 或全部 10 条审计异常，这些审计记录永久丢失不可重试。processed 变量 (line 3038/3043) 仅用于日志，不影响切片逻辑。

### 议题 #156 [独有 1/2] `app/pipeline/order_executor.py`:5548-5549 — Obsidian 平仓记录 code 重复拼接 US. 前缀

环节: s3_scheduling_pipeline · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/pipeline/order_executor.py:5548-5549): Obsidian 平仓记录 code 重复拼接 US. 前缀
  - 证据引用: `symbol: 标的代码 (如 "US.MU")
'code': f"US.{position.symbol}",`
  - 影响声称: position.symbol 在链路中通常已含 US. 前缀（如 US.MU），再拼 US. 会生成 US.US.MU，破坏 Obsidian 记录与标的关联。
  - 建议: code 使用标准化后的 symbol；若 symbol 已带 US. 前缀则不要再拼接。

<!-- DEB-VERDICT cluster=156 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: Line 5549 'code': f"US.{position.symbol}" 会双重拼接 US. 前缀。证据: position.symbol 已含 US. 前缀 — emergency_interceptor.py:798 'position.symbol.replace("US.", "")' 显式剥离前缀; order_executor.py:513 文档示例 symbol='US.MU'; order_executor.py:2443 CORE_ETFS={"US.SPY",...}。假设 position.symbol='AAPL', 实际存储为 'US.AAPL', 此代码生成 'US.US.AAPL' 写入 Obsidian 交易笔记, 导致文件名/链接/标签全部错误。line 5548 的 id 字段 f"US-{position.symbol.replace('.', '_')}" 也会生成 'US-US_AAPL-...' 但替换了所有点号, 虽冗余但不破坏功能。

### 议题 #158 [独有 1/2] `app/factors/trade_quality_gate.py`:37-105 — quality_gate.weights 配置部分覆盖时未合并默认值，缺失键会在评分时 KeyError

环节: s4_signals_sources · 类别: config_effect, error_swallowing · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, error_swallowing, app/factors/trade_quality_gate.py:70-103): 质量门模块加载失败后当前 evaluate 仍访问空 _modules，产生 KeyError 而非 fail-closed
  - 证据引用: `        except Exception as e:
            logger.error(f"[交易质量门] 模块加载失败: {e}")
            self.enabled = False
            self._init_failed = True  # DEB-20260903 #54
        self._init_modules()
        res = self._modules['regime'].check(symbol, history, market_ctx)`
  - 影响声称: 当 RegimeFilter/PivotalDetector 等依赖导入或初始化失败时，_init_modules 只把 enabled/_init_failed 置位并返回；本次 evaluate 不重新检查初始化结果，直接取 self._modules['regime']，导致 KeyError。调用方若吞掉异常，可能把门控异常当作未知状态处理，失去 fail-closed 语义。
  - 建议: 在 _init_modules 返回后检查 self._initialized；若失败立即返回 passed_gate=False/reject_reason='quality_gate_init_failed'，不要继续访问 _modules。
- **qwen3.8-max-0902** (S2, config_effect, app/factors/trade_quality_gate.py:37-105): quality_gate.weights 配置部分覆盖时未合并默认值，缺失键会在评分时 KeyError
  - 证据引用: `        self.weights = self.config.get('weights', {
            'regime': 0.15,
            'pivotal': 0.30,
            'capital_flow': 0.20,
            'leader': 0.15,
            'execution': 0.20
        })
        total_score += res.score * self.weights['regime']`
  - 影响声称: 如果 YAML 只配置了 weights 的部分键（例如只调整 regime），self.weights 会整体替换默认 dict，后续 evaluate 对 pivotal/capital_flow/leader/execution 直接下标访问会抛 KeyError，导致质量门评估中断。
  - 建议: 读取 weights 后与默认字典做 deep merge/缺键补齐，再访问各门控权重；或使用 self.weights.get(key, default_weight)。

<!-- DEB-VERDICT cluster=158 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: S3: DEB-20260903 #54 修而不彻。修复在 evaluate() 入口(line 88-94)增加 _init_failed fail-closed 分支，但仅对后续调用生效；首次调用时 enabled 仍为 True，跳过 line 88 检查后执行 _init_modules()(line 96) 失败，_modules 保持空 dict，代码继续到 line 103 self._modules['regime'].check() 仍触发 KeyError。S2: weights 配置(line 37-43) 使用 config.get('weights', default_dict)，若 YAML 提供部分 weights(如仅 regime:0.2)，get() 返回不完整的用户 dict 而非合并默认值，line 105/113 等访问缺失键会 KeyError。

### 议题 #164 [独有 1/2] `app/news_v2/sources/wallstreetcn_source.py`:65-72 — WallstreetCN fetch 内部吞掉渠道异常并返回空列表，SourceHealthMonitor 永远不会记录失败

环节: s4_signals_sources · 类别: error_swallowing · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, error_swallowing, app/news_v2/sources/wallstreetcn_source.py:65-72): WallstreetCN fetch 内部吞掉渠道异常并返回空列表，SourceHealthMonitor 永远不会记录失败
  - 证据引用: `        all_items: List[UnifiedNewsItemV2] = []
        for ch in self._channels:
            try:
                items = await self._fetch_channel(ch)
                all_items.extend(items)
            except Exception as e:
                logger.warning(f"[WallstreetCN·{ch}] 拉取失败 (跳过): {e}")
  `
  - 影响声称: SourceAdapter.fetch_safe 只有在 fetch 抛异常时才 record_failure；该源把所有渠道异常吞掉并返回 []，即使全部渠道持续失败，健康监控仍记录 success，连续失败暂停机制失效，系统会持续认为新闻源健康。
  - 建议: 区分无新文章和采集失败；若至少一个渠道失败或全部渠道失败，应抛出异常或返回可被 fetch_safe 识别的失败状态，让 SourceHealthMonitor 记录 failure。

<!-- DEB-VERDICT cluster=164 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: fetch()(line 64-72) 对每个 channel 的异常在 line 70-71 内部捕获并 logger.warning，返回空 list 而非抛出异常。base.py 的 fetch_safe()(line 105-119) 依赖 fetch() 抛异常来触发 health.record_failure(line 117)；由于 WallstreetCN fetch 从不抛出，fetch_safe 始终走 record_success 分支(line 112)，SourceHealthMonitor 的连续失败计数永远为 0，源永远不会被自动暂停，持续发起失败的 API 调用。

### 议题 #162 [独有 1/2] `app/news_v2/sources/finnhub_news_source.py`:105-154 — Finnhub 增量水位是全源全局值，单个标的的新文章会误抑制其他标的的较旧但仍有效文章

环节: s4_signals_sources · 类别: time_boundary · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, time_boundary, app/news_v2/sources/finnhub_news_source.py:105-154): Finnhub 增量水位是全源全局值，单个标的的新文章会误抑制其他标的的较旧但仍有效文章
  - 证据引用: `        for symbol in self.watchlist_codes[:5]:
        if timestamp < self._last_timestamp:
            return None
        if timestamp > self._last_timestamp:
            self._last_timestamp = timestamp`
  - 影响声称: fetch 依次遍历多个 watchlist symbol，但 _parse_article 使用同一个 _last_timestamp。若第一个 symbol 返回一条较新文章并推进水位，同轮后续其他 symbol 中时间早于该水位但本来应采集的文章会被跳过，造成新闻漏采和下游信号缺失。
  - 建议: 将增量水位按 symbol 维度维护，或在批处理结束时用本批最小/最大时间并结合 per-symbol seen 集合判断；不要用一个 symbol 的时间戳全局推进所有 symbol 的水位。

<!-- DEB-VERDICT cluster=162 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: Finnhub 增量水位 self._last_timestamp 是全源共享单一变量(line 40/154)。fetch() 按 watchlist_codes 顺序遍历标的(line 105)，若前一标的有新文章(如 timestamp=1000)推进水位(line 153-154)，后续标的的较旧但仍有效文章(timestamp=999) 在 _parse_article line 139 被 timestamp < self._last_timestamp 过滤返回 None，造成跨标的新闻漏采。DEB #63 修复(引入增量过滤)引入了此跨标的抑制副作用。

### 议题 #163 [独有 1/2] `app/news_v2/sources/rsshub_us_source.py`:136-144 — RSS 条目缺少 pubDate 时仍用当前时间入库，旧新闻可能被当作即时新闻

环节: s4_signals_sources · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/news_v2/sources/rsshub_us_source.py:136-144): RSS 条目缺少 pubDate 时仍用当前时间入库，旧新闻可能被当作即时新闻
  - 证据引用: `                        timestamp = time.time()
                        if pub_date:
                            try:
                                from email.utils import parsedate_to_datetime
                                dt = parsedate_to_datetime(pub_date)
                                tim`
  - 影响声称: 修复只处理了 pubDate 存在但解析失败的情况；若 pubDate 为空/缺失，timestamp 仍初始化为 time.time()。这类条目会带当前年龄进入新闻时效判断，可能通过 is_expired/时效衰减并触发基于即时性的信号。
  - 建议: pubDate 缺失时也丢弃该条，或至少标记为 stale/低紧急并使用 collected_at 做保守时效处理；不要让无发布时间条目获得即时新闻时间戳。

<!-- DEB-VERDICT cluster=163 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: DEB-20260903 #64 修而不彻。修复在 line 142-144 处理了 pubDate 存在但解析失败的情况(continue 丢弃)，但 pubDate 完全缺失时(line 124 findtext 返回空字符串)，line 137 if pub_date 为 False，跳过解析块，timestamp 保持 line 136 的 time.time() 当前时间(line 136)，该条目仍以当前时间入库。旧新闻在无 pubDate 的 RSS 条目中仍被当作即时新闻。

### 议题 #157 [独有 1/2] `app/factors/sss.py`:143-143 — SSS threshold=0 被 `or 0.5` 静默改回 0.5，参数空间中的 0 实际无效

环节: s4_signals_sources · 类别: config_effect · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, config_effect, app/factors/sss.py:143-143): SSS threshold=0 被 `or 0.5` 静默改回 0.5，参数空间中的 0 实际无效
  - 证据引用: `            _thr = abs(float(self.params.get("threshold", 0.5))) or 0.5`
  - 影响声称: param_space 明确包含 threshold=0，但 abs(0.0) 为 0，`or 0.5` 会把 0 替换成 0.5。任何希望用 0 作为方向触发阈值的参数扫描或配置都会被无声覆盖，导致因子方向判定与参数预期不一致。
  - 建议: 改为显式判空：thr = self.params.get('threshold', 0.5); _thr = abs(float(thr)) if thr is not None else 0.5；不要用 `or` 处理合法 0 值。

<!-- DEB-VERDICT cluster=157 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: Line 143 '_thr = abs(float(self.params.get("threshold", 0.5))) or 0.5' — Python 中 0.0 or 0.5 == 0.5, 故 threshold=0 被静默替换为 0.5。DEB-20260903 #52 修复了 polarity 标注和参数透传(line 161-164), 但保留了 'or 0.5' 兜底。threshold=0 在参数搜索/优化场景中是合理值(使因子始终输出方向信号而非 NEUTRAL), 当前代码将其等同于默认值, 导致该参数空间点实际无效。属参数语义静默收窄, 不影响默认行为, 但影响参数调优。

### 议题 #165 [独有 1/2] `app/news_v2/sources/wallstreetcn_source.py`:174-175 — _seen_ids 超过上限后从无序 set 任意保留一半，去重状态可能丢失最近文章 ID

环节: s4_signals_sources · 类别: state_machine · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, state_machine, app/news_v2/sources/wallstreetcn_source.py:174-175): _seen_ids 超过上限后从无序 set 任意保留一半，去重状态可能丢失最近文章 ID
  - 证据引用: `        if len(self._seen_ids) > self._max_seen:
            self._seen_ids = set(list(self._seen_ids)[-self._max_seen // 2:])`
  - 影响声称: set 不保证插入顺序，list(self._seen_ids)[-500:] 保留的是任意子集而非最近 500 条。若最近文章 ID 被丢弃，而 API 后续仍返回相同文章，会导致重复新闻重新进入管线，增加去重/信号层压力并可能重复触发冷却外信号。
  - 建议: 用 OrderedDict、deque 或带时间戳的 LRU 结构保存 seen id，按插入顺序淘汰最旧 ID。

<!-- DEB-VERDICT cluster=165 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: line 174-175: self._seen_ids 是 set(无序)，list(self._seen_ids) 产生任意顺序，[-self._max_seen//2:] 保留的'后一半'并非最近添加的 ID，而是任意子集。正确做法应使用有序结构(如 OrderedDict 或带时间戳的 list)保留最近 ID。当前实现会导致最近入库的文章 ID 被随机丢弃，下次采集时这些文章被重新当作新文章处理，产生重复新闻。

### 议题 #159 [独有 1/2] `app/llm/agent_bridge.py`:726-728 — wait_for_response 对毒响应只日志重试，不隔离/删除，导致同一坏文件反复读到超时

环节: s4_signals_sources · 类别: error_swallowing · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, error_swallowing, app/llm/agent_bridge.py:726-728): wait_for_response 对毒响应只日志重试，不隔离/删除，导致同一坏文件反复读到超时
  - 证据引用: `                except Exception as e:
                    logger.warning(f"[Agent-US] 读取响应失败: {e}")
            time.sleep(2)`
  - 影响声称: 响应文件存在但 JSON 非法/结构异常时，wait_for_response 不删除也不 quarantine 该文件，而是每 2 秒重复读取同一毒文件直到 timeout。期间真实响应即使后来写入也可能被旧毒文件遮蔽，最终只得到 fallback，且后续同 request_id 调用仍可能中毒。
  - 建议: 在 except 分支调用 _quarantine_response(response_file) 或删除该文件，然后继续等待新响应；对结构非 dict 的 result 也应做 schema 校验。

<!-- DEB-VERDICT cluster=159 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: wait_for_response(line 690-728) 在 while 循环中读取 response_file(line 693-694)，json.loads 失败时(line 705) 异常被 line 726 捕获仅日志，不删除/移动毒文件，sleep(2) 后(line 728) 循环重新读取同一坏文件，反复失败直至 300s 超时。超时后(line 730-737) 也未清理 response_file。与旧 DEB #56(line 999-1021) 同根因但不同方法。

### 议题 #160 [独有 1/2] `app/llm/agent_bridge.py`:765-769 — _update_memory 对 LLM 返回的 memory_updates 不做类型校验，字符串会被逐字符写入教训列表

环节: s4_signals_sources · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/llm/agent_bridge.py:765-769): _update_memory 对 LLM 返回的 memory_updates 不做类型校验，字符串会被逐字符写入教训列表
  - 证据引用: `        if "historical_lessons" in updates:
            for lesson in updates["historical_lessons"]:
                if lesson not in self._memory["historical_lessons"]:
                    self._memory["historical_lessons"].append(lesson)`
  - 影响声称: 若 Hermes/LLM 返回 historical_lessons 为字符串而非列表，for 会迭代字符串字符，把单字符碎片写入长期记忆；若返回 dict/数字等异常类型，还可能造成后续 prompt 污染或 TypeError。记忆是盘前计划输入，错误会跨日放大。
  - 建议: 更新前校验 updates['historical_lessons'] 是 list[str]，symbol_lessons 是 dict[str,str]，strategy_adjustments 是 list；不合法则丢弃并记录审计。

<!-- DEB-VERDICT cluster=160 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: _update_memory(line 765-768) 对 updates['historical_lessons'] 直接 for 遍历并 append，无 isinstance(updates['historical_lessons'], list) 类型校验。若 LLM 返回字符串而非数组，Python 对字符串迭代会逐字符执行，每个字符作为独立教训写入 self._memory['historical_lessons'] 列表(line 768)，污染记忆数据。同理 line 777 strategy_adjustments 也存在相同风险。

### 议题 #161 [独有 1/2] `app/llm/agent_bridge.py`:1222-1224 — call_agent_with_tools 的降级分支假设 _call_llm_direct 必返回 dict，JSON 数组会导致 TypeError

环节: s4_signals_sources · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/llm/agent_bridge.py:1222-1224): call_agent_with_tools 的降级分支假设 _call_llm_direct 必返回 dict，JSON 数组会导致 TypeError
  - 证据引用: `        fallback = self._call_llm_direct(prompt=prompt, system_prompt=system_prompt, timeout=timeout)
        if fallback:
            fallback["mode"] = "fallback_bailian"`
  - 影响声称: _call_llm_direct 通过 json.loads 解析 LLM 输出，可能返回 list（例如模型返回 JSON array）。此时 fallback['mode'] 会抛 TypeError，使降级路径本身失败；调用方可能拿不到本应可用的降级结果。
  - 建议: 先判断 isinstance(fallback, dict)，非 dict 时包装为 {'result': fallback} 或返回统一结构；不要直接对未知 JSON 类型做下标赋值。

<!-- DEB-VERDICT cluster=161 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: call_agent_with_tools 降级分支 line 1222 调用 _call_llm_direct，后者 line 1137 json.loads(text) 可返回 list(当 LLM 输出 JSON 数组时)。line 1224 fallback['mode']='fallback_bailian' 对 list 执行字符串索引赋值会触发 TypeError。虽然 line 1225 有 isinstance(fallback, dict) 守卫，但在 line 1224 之后，无法保护。降级路径崩溃导致调用方收到异常而非降级响应。

### 议题 #168 [独有 1/2] `app/decision/strategies/rsi_rebound.py`:45-49 — RSI=0 被 falsy 判断误当作无效数据

环节: s5_decision_orchestration · 类别: numeric_unit · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, numeric_unit, app/decision/strategies/rsi_rebound.py:45-49): RSI=0 被 falsy 判断误当作无效数据
  - 证据引用: `        rsi = market_data.get("rsi", 50)
        price = market_data.get("current_price", 0)

        if not rsi or not price:
            return StrategyResult()`
  - 影响声称: RSI=0 是合法且极端的超卖值，但 not rsi 为 True，策略直接返回无信号。真实触发路径是指标计算返回 0 时，系统会错过最强的超卖反弹信号。
  - 建议: 将空值判断改为 if rsi is None or not price，或显式校验类型/范围，不要把 0 当作缺失。

<!-- DEB-VERDICT cluster=168 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: rsi_rebound.py:48 `if not rsi or not price:` 对 RSI=0 做了 falsy 判断——RSI 合法范围 0-100，RSI=0 是极端超卖应触发最强做多信号，但 `not 0` 为 True 会直接返回空 StrategyResult()。price=0 做 falsy 过滤合理（无价格不可交易），但 RSI 应用 `rsi is None` 或显式范围校验。该缺陷在同文件 #74（强度公式反向）之外，属独立新发现。会导致极端超卖行情下信号被静默丢弃，severity 3（功能错误/漏单）。

### 议题 #169 [独有 1/2] `app/orb/engine.py`:75-80 — ORBPosition.r_multiple 使用原始入参而非兜底后的有效 sl_distance

环节: s5_decision_orchestration · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/orb/engine.py:75-80): ORBPosition.r_multiple 使用原始入参而非兜底后的有效 sl_distance
  - 证据引用: `        self.sl_distance = sl_distance if sl_distance > 0 else atr_at_entry  # 兜底

        self.order_id = order_id

        # 2026-06-08: 清理分层止盈死代码 (2R 固定止盈替代)
        self.r_multiple = sl_distance     # 风险单位`
  - 影响声称: ORBPosition 允许 sl_distance=0 构造；此时 self.sl_distance 会被 atr_at_entry 兜底，但 r_multiple 仍为 0。任何下游用 r_multiple 表示风险单位、归因、风控或退出距离时都会把有效止损距离误读为 0，造成风险量纲漂移。
  - 建议: 改为 self.r_multiple = self.sl_distance，确保 r_multiple 始终等于兜底后的有效止损距离。

<!-- DEB-VERDICT cluster=169 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: engine.py:75 用兜底逻辑 `sl_distance if sl_distance > 0 else atr_at_entry` 赋给 self.sl_distance，但 line 80 `self.r_multiple = sl_distance` 直接使用原始入参。当 sl_distance 传入 0 或负值时，self.sl_distance 正确回退为 atr_at_entry，但 self.r_multiple 仍为 0，导致风险单位与实际止损距离不一致。r_multiple 被列在 _POSITION_FIELDS (line 43) 供序列化/导出，下游消费方会拿到错误的风险倍数。

### 议题 #167 [独有 1/2] `app/coordinator/symbol_lock.py`:137-149 — reconcile_with_positions 不纠正锁 holder 与真实持仓 owner 不一致

环节: s5_decision_orchestration · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/coordinator/symbol_lock.py:137-149): reconcile_with_positions 不纠正锁 holder 与真实持仓 owner 不一致
  - 证据引用: `            # 锁里有但持仓没有 → release
            for sym in list(self._locks.keys()):
                if sym not in actual_owners:
                    holder = self._locks[sym][0]
                    del self._locks[sym]
                    self._observers.pop(sym, None)  # DEB-20260903 #70: 同步清理观察者
   `
  - 影响声称: observe 模式下 A/B 同时请求同一 symbol 时，holder 仍记为 A；若后续真实持仓只剩 B，reconcile 因 sym 已在 self._locks 而不更新 holder，owner() 继续返回幽灵 A。B 后续 release 可能只清除观察者而不能解除锁，导致强制模式下错误拒绝/错误路由，锁状态与持仓长期漂移。
  - 建议: reconcile 时若 sym 在锁中但 holder != actual_owners[sym]，应显式更新 holder/清理 observers 并记录 RECONCILE_TAKEOVER 或先 release 再 acquire。

<!-- DEB-VERDICT cluster=167 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: symbol_lock.py:137-149 的 reconcile_with_positions 仅处理'锁有持仓无→release'和'持仓有锁无→acquire'两种情况。当锁 holder 与 actual_owners 中 owner 不一致时（如策略 A 的锁仍在但持仓已归策略 B），代码既不释放也不纠正：line 139 `sym not in actual_owners` 为 False 跳过释放；line 147 `sym not in self._locks` 为 False 跳过获取。结果锁 holder 仍指向旧策略，新策略 B 无法 release（line 100-104 校验 holder 匹配），导致状态错乱。

### 议题 #166 [独有 1/2] `app/coordinator/dual_mode_params.py`:158-162 — 手动重置交易计数使用服务器本地日期而非 ET 交易日

环节: s5_decision_orchestration · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/coordinator/dual_mode_params.py:158-162): 手动重置交易计数使用服务器本地日期而非 ET 交易日
  - 证据引用: `def reset_today_auto_trade_count() -> None:
    """重置计数 (供测试/Dashboard 手动调用)."""
    with _counter_lock:
        _counter_state["date"] = date.today()
        _counter_state["count"] = 0`
  - 影响声称: _maybe_reset_counter 已改为优先使用 now_et().date()，但手动重置仍写 date.today()。若服务器时区与 ET 日期不一致，手动重置后存储的 date 与后续 ET 跨日判断不一致，可能导致计数被再次清零或日配额边界审计不一致。
  - 建议: reset_today_auto_trade_count 也应使用 now_et().date()，失败时再回退 date.today()，与 _maybe_reset_counter 保持同一时间基准。

<!-- DEB-VERDICT cluster=166 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: reset_today_auto_trade_count() 在 dual_mode_params.py:161 直接用 date.today() 设置日期，而同文件 _maybe_reset_counter() (line 131-137) 已为 DEB-20260903 #68 修复为 ET 交易日。手动重置函数绕过了修复路径，当 Dashboard/测试调用后写入本地日期，后续 _maybe_reset_counter 会检测到日期不一致再次重置计数，导致计数被意外归零。属卫生问题(手动调用入口)，非自动交易主路径，severity 2。

### 议题 #170 [独有 1/2] `app/orb/engine.py`:405-422 — 开盘区间 fallback 被标记 ready 后阻止同窗口重建

环节: s5_decision_orchestration · 类别: state_machine · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, state_machine, app/orb/engine.py:405-422): 开盘区间 fallback 被标记 ready 后阻止同窗口重建
  - 证据引用: `            if symbol in self._ranges and self._ranges[symbol].get("ready"):
                continue  # 已构建, 跳过
            try:
                # 获取 1-min klines (至少 range_minutes 根)
                klines = self._get_1min_klines(symbol, count=self._cfg.range_minutes + 5)
                if not kl`
  - 影响声称: 开盘初 K 线不足时生成 fallback 区间并 ready=True；随后 09:30-09:45 的 Phase 1 会因 ready 跳过重建。即使几秒后 K 线恢复，该标的也无法重建真实开盘区间，而 _check_breakout 又会拒绝 fallback，导致整个开盘窗口错过有效突破。
  - 建议: fallback 区间不应等同于 ready 开盘区间；可标记 ready=False/fallback_pending，或在 range_end 前允许对 fallback 区间持续重试重建。

<!-- DEB-VERDICT cluster=170 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: engine.py:405 `if self._ranges[symbol].get('ready'): continue` 阻止已标记 ready 的区间被重建。line 419-428 的 fallback 伪区间设 `ready=True, quality_score=0.3, fallback=True`。在 Phase 1 (09:30-09:45) 内，若首次因 K 线不足生成 fallback 后真实 K 线到达，line 405 会跳过该标的，伪区间持续生效至 Phase 2 开始才被 _build_rolling_ranges 覆盖。期间若有突破检测基于伪区间做交易决策（width_pct=0.3% 极窄），可产生假突破信号。

### 议题 #171 [独有 1/2] `app/orb/engine.py`:645-656 — 滚动区间价格校验失败后保留旧 ready 区间继续可交易

环节: s5_decision_orchestration · 类别: state_machine · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, state_machine, app/orb/engine.py:645-656): 滚动区间价格校验失败后保留旧 ready 区间继续可交易
  - 证据引用: `                if rolling_high > 0 and rolling_low > 0:
                    range_mid = (rolling_high + rolling_low) / 2
                    cp = self._get_current_price(symbol)
                    if cp and cp > 0:
                        dev = abs(range_mid - cp) / cp
                        if d`
  - 影响声称: 当新拉取的滚动 K 线与现价偏差超过 10% 时只 continue，不清空/不降级已有 self._ranges[symbol]。若旧开盘或旧滚动区间仍 ready，_evaluate_breakouts 会继续消费旧边界，可能基于数小时前或异常陈旧的区间触发开仓。
  - 建议: 校验失败时应将旧区间标记 stale/ready=False 或设置更短过期，避免不可信新数据与可交易旧状态并存。

<!-- DEB-VERDICT cluster=171 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: engine.py:645-656 滚动区间价格校验失败后 `continue` 跳过当前标的更新，但 self._ranges[symbol] 中旧的 ready 滚动区间（可能已 5 分钟以上）仍保留。line 625-628 的 5 分钟过期检查仅跳过未过期区间；过期后重试若新数据仍不通过校验，旧区间持续以 ready=True 存在，_evaluate_breakouts 会继续用陈旧 high/low 做突破检测。当 Futu 数据源异常时，此路径可能导致基于过时区间的假突破交易。

### 议题 #172 [独有 1/2] `app/orb/engine.py`:842-860 — 隔夜止损放宽按日期幂等，残留仓位会导致 sl_distance 复合放大

环节: s5_decision_orchestration · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/orb/engine.py:842-860): 隔夜止损放宽按日期幂等，残留仓位会导致 sl_distance 复合放大
  - 证据引用: `        if hasattr(self, '_overnight_adjusted_date') and self._overnight_adjusted_date == today:
            return

        logger.info("[ORB] 15:30 后调整隔夜持仓止损")

        for symbol, pos in self._positions.items():
            # 放宽止损距离 (隔夜波动更大)
            original_sl_distance = pos.sl_distance
    `
  - 影响声称: 若 15:55 EOD 平仓失败或仓位因异常残留到次日，次日 15:30 后 _overnight_adjusted_date != today，会对已经被放宽过的 pos.sl_distance 再乘 1.5。同一仓位跨多个交易日残留时止损距离复合放大，风险敞口失控。
  - 建议: 改为按 position 记录是否已隔夜调整，或基于初始 sl_distance 计算目标值而非原地乘法；同时对超过 EOD 应平未平仓位强制告警/隔离。

<!-- DEB-VERDICT cluster=172 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: engine.py:842-860 隔夜止损放宽按 server 本地 date 幂等（同日不重复），但跨日重置代码 (lines 323-338) 未清理 _overnight_adjusted_date。持仓每跨一个交易日，15:30 后 sl_distance 再乘 1.5：第1天 1.5x，第2天 2.25x，第3天 3.375x。止损距离复合放大意味着风控逐渐失效——原本 $1 止损 3 天后变成 $3.38，潜在损失远超预期。line 850 `pos.sl_distance *= 1.5` 基于已放宽的值而非原始值，属风控失效，severity 4。

### 议题 #173 [独有 1/2] `app/orb/engine.py`:2274-2283 — 冷却门禁在 scheduler 单例不可得时静默放行

环节: s5_decision_orchestration · 类别: error_swallowing · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, error_swallowing, app/orb/engine.py:2274-2283): 冷却门禁在 scheduler 单例不可得时静默放行
  - 证据引用: `                _scheduler = getattr(self, '_scheduler', None) or getattr(self, 'scheduler', None)
                if _scheduler is None:
                    try:
                        import importlib as _il
                        _scheduler = getattr(_il.import_module("app.core.scheduler_core")`
  - 影响声称: 当 scheduler_core 未导入、属性名变化或单例未初始化时，_scheduler 为 None，冷却检查直接跳过并继续下单；这与同函数中其他风控门禁异常时 fail-close 的语义不一致，可能允许刚止损标的立即被 ORB 重入。
  - 建议: 若冷却门禁不可评估，应返回失败或显式降级拒绝；只有在明确配置禁用时才放行，并记录告警。

<!-- DEB-VERDICT cluster=173 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: engine.py:2274-2283 冷却门禁在 scheduler 单例不可得时静默放行。line 2274 尝试 getattr 获取 _scheduler，line 2275-2280 fallback import 若也失败则 _scheduler=None。line 2281 `if _scheduler and ...` 条件为 False，冷却检查被跳过——不是异常所以不触发 line 2284-2287 的 fail-closed 异常处理。DEB-20260903 #80 修复了异常路径的静默吞掉，但 _scheduler=None 的正常路径仍未覆盖，属修而不彻。近期止损标的可被立即重新入场。

### 议题 #174 [独有 1/2] `app/orb/engine.py`:2375-2385 — get_orb_engine 单例一旦创建后忽略后续依赖注入

环节: s5_decision_orchestration · 类别: config_effect · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, config_effect, app/orb/engine.py:2375-2385): get_orb_engine 单例一旦创建后忽略后续依赖注入
  - 证据引用: `def get_orb_engine(**kwargs) -> ORBEngine:
    """获取 ORB 引擎单例"""
    global _orb_engine_instance
    if _orb_engine_instance is None:
        from app.orb.config import ORBConfig

        raw_cfg = kwargs.pop("config", {})
        cfg = ORBConfig.from_dict(raw_cfg) if isinstance(raw_cfg, dict) else `
  - 影响声称: 若任何代码先以无参或缺少 execution_engine/data_router/position_store 的方式调用 get_orb_engine()，单例会以空依赖构造；之后编排层再传入完整依赖时只返回旧实例，ORB daemon 可能以无行情/无执行/无仓位账本状态运行，导致漏信号、无法下单或退出监控缺失。
  - 建议: 首次构造必需依赖显式校验；若后续需要重配置，应提供显式 reconfigure/reset 或在已存在实例且关键依赖缺失时补齐/报错，而不是静默忽略 kwargs。

<!-- DEB-VERDICT cluster=174 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: get_orb_engine() 仅在 _orb_engine_instance is None 时构建单例 (engine.py:2378-2383), 后续调用直接返回旧实例, kwargs 被静默忽略 (engine.py:2385). 若热重载或重启后以新依赖调用, 引擎仍持有旧 config/依赖引用. 与旧 DEB #71 (symbol_lock 单例) / #103 (capital_safety 单例) 同根因模式. 当前主流程通常单次初始化, 实际触发概率低, 属卫生/健壮性问题.

### 议题 #175 [独有 1/2] `app/playbook/pipeline.py`:76-80 — 影子行防重在写盘前就占位，写盘失败后键残留会阻断后续补写

环节: s6_playbook_engine · 类别: state_machine · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, state_machine, app/playbook/pipeline.py:76-80): 影子行防重在写盘前就占位，写盘失败后键残留会阻断后续补写
  - 证据引用: `        _k = (inst_id, kind)
        if _k in seen:
            return False
        seen.add(_k)
        return True`
  - 影响声称: _shadow_dedup_ok 在返回允许写入前就把 (inst_id, kind) 加入内存 seen；如果调用方随后 append 文件失败，该键仍被占用，后续重试会返回 False，导致影子记录永久丢失。该模式与同文件中已修复的写盘后才入 set 的幂等纪律不一致。
  - 建议: 把防重占位改为两阶段：先检查，写盘成功后再 commit 键；或由调用方在写盘失败时显式回滚 seen 键。

<!-- DEB-VERDICT cluster=175 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: 同旧 deb20260903 #84 未修. _shadow_dedup_ok 在 pipeline.py:79 (seen.add(_k)) 于返回 True 前即将键加入内存集, 调用方拿到 True 后执行写盘, 若写盘失败该键已残留于 _SHADOW_SEEN (pipeline.py:41), 后续补写被 pipeline.py:77-78 的 if _k in seen 拦截返回 False, 该行永久丢失. 函数内无任何写盘失败回滚机制. 影子行漏写影响 playbook 回测与审计完整性, 严重度 4 (漏单).

### 议题 #176 [独有 1/2] `app/playbook/pipeline.py`:546-551 — naive entry_time 直接取本地 date，跨时区部署会导致交易日/隔夜判定错日

环节: s6_playbook_engine · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/playbook/pipeline.py:546-551): naive entry_time 直接取本地 date，跨时区部署会导致交易日/隔夜判定错日
  - 证据引用: `            _entry = datetime.fromisoformat(entry_time_str)
            # entry_time 可能 naive (CST 服务器存), 取 date; 若 tz-aware 转 ET
            if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
                _entry_date = _entry.astimezone(_et).date()
            else:
                _ent`
  - 影响声称: 注释已说明 entry_time 可能是 CST 服务器 naive 时间，但代码直接取 date，不按来源时区转换到 ET。若服务器时区与 ET 跨日，MAX_HOLD_T5、隔夜仓判定可能提前/延后一天触发，导致误豁免或误强平。
  - 建议: 写入侧统一存 tz-aware ET/UTC 时间；读取侧对 naive 时间显式声明来源时区并转换到 ET，再取 trade_date。

<!-- DEB-VERDICT cluster=176 -->
裁决: REJECTED | 置信=high | 理由: pipeline.py:546-551 对 tz-aware entry_time 已正确 astimezone(_et).date() (line 549); naive 路径 (line 551) 按项目约定服务器时区=ET, .date() 给出正确 ET 日期. 声称的'跨时区部署导致错日'前提不成立: 全仓多处 (shadow_signal_logger, market_data_router 等) 统一假设服务器 ET, 非跨时区部署场景. 属代码健壮性建议而非当前实际缺陷. 与旧 #22/#23/#29 同时区系列发现, 那些已确认的具体位置与本簇不同.

### 议题 #177 [独有 1/2] `app/playbook/pipeline.py`:819-820 — TechnicalFallbackAdapter 用 or 兜底会把合法 0 值改写成 50/0.5

环节: s6_playbook_engine · 类别: numeric_unit · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, numeric_unit, app/playbook/pipeline.py:819-820): TechnicalFallbackAdapter 用 or 兜底会把合法 0 值改写成 50/0.5
  - 证据引用: `        strength = int(getattr(cand, "strength", 50) or 50)
        confidence = float(getattr(cand, "confidence", 0.5) or 0.5)`
  - 影响声称: 如果纯函数策略返回 strength=0 或 confidence=0.0，这两个合法零值会因 Python falsy 被替换成 50/0.5；低置信度/零强度信号会被伪装成中性可过门信号，可能进入后续执行链。
  - 建议: 改为显式 None 判断：仅当字段缺失时才用默认值；数值 0 应保留并按原值参与门槛判定。

<!-- DEB-VERDICT cluster=177 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: pipeline.py:819 用 int(getattr(cand, 'strength', 50) or 50), Python 中 0 为 falsy, 合法 strength=0 被 or 改写为 50; 同 line 820 confidence=0.0 被 or 0.5 改写为 0.5. TechnicalFallbackAdapter 是 DA 模块缺席时的兜底路径 (pipeline.py:800-830), 错误 strength/confidence 直接进入下游投票/仓位计算, 产生错误交易信号. 功能错误, 严重度 3.

### 议题 #179 [独有 1/2] `app/playbook/playbook_service.py`:940-944 — custom_params 键存在但值为 None 仍会覆盖指令原始价格/仓位

环节: s6_playbook_engine · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/playbook/playbook_service.py:940-944): custom_params 键存在但值为 None 仍会覆盖指令原始价格/仓位
  - 证据引用: `        _cp = custom_params or {}
        entry_price = _cp.get("entry_price", inst.get("entry_price", 0))
        stop_loss = _cp.get("stop_loss", inst.get("stop_loss", 0))
        take_profit = _cp.get("take_profit", inst.get("take_profit", 0))
        position_size = _cp.get("position_size", inst`
  - 影响声称: 注释声称修复了缺键回退，但若调用方传入 {"entry_price": None, "stop_loss": None}，dict.get 会返回 None 并覆盖指令原值；后续 admission/下单参数可能拿到 None/0，position_size 还会被兜成 1，造成风控判据空转或错误下单参数。
  - 建议: 逐键判断 key in _cp and _cp[key] is not None 再覆盖；或在入口拒绝包含 None 的 custom_params。

<!-- DEB-VERDICT cluster=179 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: playbook_service.py:940-944 是旧 deb20260903 #90 的残留: 原整体切换已改为逐键回退 (line 938 注释), 但 .get('entry_price', default) 在键存在且值为 None 时仍返回 None (非 default), 覆盖 inst 原始 entry_price/stop_loss/take_profit. 例如 custom_params={'entry_price': None} 会使 entry_price=None, 后续下单缺少价格保护. 旧 #90 修了'键不存在'场景, '键存在值 None'场景未修. 严重度 4 (错单 — 价格/风控字段丢失).

### 议题 #180 [独有 1/2] `app/playbook/playbook_service.py`:1002-1003 — execute_instruction 对非 BUY 方向一律映射为 SHORT，缺少方向白名单

环节: s6_playbook_engine · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, logic_anomaly, app/playbook/playbook_service.py:1002-1003): execute_instruction 对非 BUY 方向一律映射为 SHORT，缺少方向白名单
  - 证据引用: `        mos_direction = "LONG" if direction == "BUY" else "SHORT"`
  - 影响声称: 执行路径只显式拦截 SKIP，未校验 direction 是否为 BUY/SELL_SHORT；若指令方向因上游语义漂移写成 LONG、空串或其他非法值，会被默认映射为 SHORT，可能直接下错方向。
  - 建议: 在执行前对 direction 做白名单校验，仅允许 BUY/SELL_SHORT；如需兼容 LONG/SHORT，应显式映射，非法值直接拒单并记录 skip_reason。

<!-- DEB-VERDICT cluster=180 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: playbook_service.py:1003 将任何非 BUY 的 direction 一律映射为 'SHORT', 无方向白名单校验. direction 来自 inst['direction'] (line 932). 虽然当前 playbook 主流程仅生成 BUY/SELL 两方向, 但若上游数据异常 (如 direction 为空串/None/'HOLD' 等), 静默映射到 SHORT 可触发反向开仓. 二元映射无防御性校验是功能缺陷, 严重度 3 (潜在错单/反向开仓风险).

### 议题 #181 [独有 1/2] `app/playbook/playbook_service.py`:2110-2135 — 路由限价单 TTL 后撤单失败仍进入终态放弃，可能留下无账实仓

环节: s6_playbook_engine · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, state_machine, app/playbook/playbook_service.py:2110-2135): 路由限价单 TTL 后撤单失败仍进入终态放弃，可能留下无账实仓
  - 证据引用: `        _cancel_res = _dual.cancel_order(str(futu_order_id), target_env)
        if not _cancel_res.get("success"):
            logger.warning(f"[archetype路由] {symbol} 撤单失败 (继续按放弃处理): {_cancel_res.get('error')}")
        time.sleep(2.0)  # 等撤单传播, 随后竞态复查
        return _skip_terminal(
            "ar`
  - 影响声称: TTL 到期后若 cancel_order 返回失败但订单仍存活，函数仅 warning 后就把指令置为 archetype_ttl_expired 终态并释放执行占位；之后该限价单若真实成交，会产生真实券商持仓但没有对应 trade_records/退出监控，形成无 SL 守护的孤儿仓。
  - 建议: 撤单失败时不得直接进入终态放弃；应重试撤单并查询确认订单已 CANCELLED/DELETED/FAILED/REJECTED，或转入人工 reconcile/保持执行占位，只有在确认订单已死或已按成交量记账后才释放 claim。
- **qwen3.8-max-0902** (S3, state_machine, app/playbook/playbook_service.py:2116-2135): TTL 复查到部分成交但成交均价为 0 时会终态跳过，可能丢失真实持仓
  - 证据引用: `        _status2, _dealt2, _davg2 = _query_once()
        if _status2 is not None:
            _last_status = _status2
        _fin_dealt = max(_dealt, _dealt2)
        if _fin_dealt > 0 and (_davg2 > 0 or _davg > 0):
        return _skip_terminal(
            "archetype_ttl_expired",
            f"`
  - 影响声称: 当竞态复查确认 dealt_qty>0 但 dealt_avg_price 异常为 0 时，条件不满足，代码会走到 archetype_ttl_expired 跳过；此时真实券商可能已有部分持仓，但系统不落账，产生无 trade_records 的孤儿仓。
  - 建议: 只要 _fin_dealt>0 就不能进入 skip 终态；成交均价缺失时应回退到 limit_price 或标记人工核价入账，并同步检查剩余订单是否已撤。

<!-- DEB-VERDICT cluster=181 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: 两子声称均成立. 子1 (line 2110-2112): cancel_order 失败后仅 log warning, 仍走 _skip_terminal (line 2133) 放弃; broker 侧订单仍活, 后续成交系统已不跟踪 → 孤儿仓. 与旧 #91 同区域 (line 2071-2092 修了终态部分成交, 但 TTL 撤单失败路径未修). 子2 (line 2116-2120): 条件 _davg2>0 or _davg>0 要求至少一个正均价, 若部分成交但 broker 暂未返均价 (两者=0), 条件失败落入 _skip_terminal, 丢失已成交量. 对比 line 2076-2089 终态路径用 _limit_px 兜底, TTL 路径缺少此兜底. 严重度 4 (错单/状态错乱/孤儿仓).

### 议题 #178 [独有 1/2] `app/playbook/pipeline.py`:8920-8923 — EOD 兜底连续失败 3 次后永久跳过，失败计数无重置会残留孤儿仓

环节: s6_playbook_engine · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, state_machine, app/playbook/pipeline.py:8920-8923): EOD 兜底连续失败 3 次后永久跳过，失败计数无重置会残留孤儿仓
  - 证据引用: `            _fail_count = _meta.get("eod_close_fail_count", 0)
            if _fail_count >= 3:
                logger.debug(f"[playbook dual] EOD 兜底放弃 {symbol} ({_row_target}) — 连续失败 {_fail_count} 次")
                continue`
  - 影响声称: EOD 平仓若因瞬时报价/券商异常连续失败 3 次，eod_close_fail_count 被持久化到 metadata_json；后续轮次仍会读到 >=3 并直接 continue，导致该 open trade_record 永久不再被 EOD 兜底处理。若真实券商仍有仓位，则形成无人管理、无退出规则覆盖的残留仓位。
  - 建议: 失败计数应按日重置或在报价/券商恢复后重置；达到阈值后应升级人工处理或进入强制对账队列，而不是永久跳过。

<!-- DEB-VERDICT cluster=178 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: pipeline.py:8920-8923: eod_close_fail_count >= 3 后永久 continue 放弃该 symbol. 成功路径 (line 9042-9065) 不重置 fail_count (但记录已关闭不再被扫到, 此点无影响). 关键问题: 若 EOD 连续 3 次失败后放弃, 仓位仍 open, 当 broker/网络恢复后该仓不再被 EOD 路径重试, 残留为孤儿仓 — 隔夜风险/保证金占用. 虽为 P1 fix (2026-06-25, line 8919 注释) 有意为之停止日志刷屏, 但代价是孤儿仓风险. 严重度 4 (状态错乱/漏单).

### 议题 #182 [独有 1/2] `app/execution/broker_adapter.py`:81-82 — broker 兜底名义上限对市价单/零价单完全失效 (price<=0 直接放行, 卖空市价单按 $1 计价)

环节: s7_risk_execution · 类别: logic_anomaly · 自报最高 severity: S4
- **kimi-k3** (S4, logic_anomaly, app/execution/broker_adapter.py:81-82): broker 兜底名义上限对市价单/零价单完全失效 (price<=0 直接放行, 卖空市价单按 $1 计价)
  - 证据引用: `L81-82: `if qty <= 0 or price <= 0:` / `return True  # 无效参数不在此拦截（上游应已处理）`; L445 以 `order.price` 调用(市价单 price=0); L948 卖空 `self._notional_sanity_check(symbol, qty, price if price > 0 else 1.0)`; L1076 期权市价单 premium 按 1.0 计`
  - 影响声称: 触发路径: 任何 MARKET 类型订单 (order_type_selector 对 strength>=80 的信号返回 MARKET, price 通常为 0) 经 submit_order → _notional_sanity_check 直接 return True, '最后一道墙' 对市价单不存在; try_direct_short(price=0) 时 1000 股 $500 股票的
  - 建议: price<=0 时用最近行情价 (data_router.get_quote) 估算名义; 取价失败应 fail-close 拒绝而非 return True; 卖空/期权市价单同理, 不得以 1.0 代替真实价格。

<!-- DEB-VERDICT cluster=182 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: broker_adapter.py:81-82 `if qty <= 0 or price <= 0: return True` 使 _notional_sanity_check 对市价单(price=0)完全放行。L445 常规下单以 order.price 传入(市价单 price=0 直接命中 L81 条件); L948 卖空路径用 `price if price > 0 else 1.0` 使名义=qty×$1, 恒过 30% equity 上限; L1076 期权同理用 $1 兜底。该函数文档自称'最后一道墙'(L66-70), 但对市价单/卖空市价单完全失效, 上游风控若有漏洞则无兜底。属风控失效。

### 议题 #185 [独有 1/2] `app/execution/broker_adapter.py`:540-542 — cancel_order 将撤单请求成功视为撤单成功，未确认终态

环节: s7_risk_execution · 类别: race_condition · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, race_condition, app/execution/broker_adapter.py:540-542): cancel_order 将撤单请求成功视为撤单成功，未确认终态
  - 证据引用: `if ret == 0:  # RET_OK
    logger.info(f"[富途] 取消订单成功 {order_id}")
    return True`
  - 影响声称: modify_order 返回成功可能只表示撤单请求被接受，不代表订单已最终取消；若订单在撤单前已成交或部分成交，上游按已取消处理会造成订单/持仓状态漂移，可能引发重复下单或漏记成交。
  - 建议: cancel_order 成功后应再次查询订单终态，或返回 CANCEL_REQUESTED，由订单状态机根据最终 order_status 确认是否真正取消。

<!-- DEB-VERDICT cluster=185 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: broker_adapter.py:540-542 cancel_order 在 modify_order 返回 RET_OK 时即 log '取消订单成功' 并 return True, 但富途 RET_OK 仅表示撤单请求已被接受, 不代表订单已达 CANCELLED 终态(可能仍在部分成交)。调用方 order_tracker.py:558 依赖此返回值, L585 直接将订单标记为 OrderState.CANCELLED 而不向 broker 复核实际状态。pipeline/order_executor.py:354-376 有后置验证(L362 查询未结订单), 但 order_tracker 路径无此防护。属状态错乱风险。

### 议题 #187 [独有 1/2] `app/risk/capital_safety.py`:561-567 — buying_power<=0 时用 equity×80% 编造可用预算, 把'真实零购买力'与'数据缺失'混为一谈

环节: s7_risk_execution · 类别: logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, app/risk/capital_safety.py:561-567): buying_power<=0 时用 equity×80% 编造可用预算, 把'真实零购买力'与'数据缺失'混为一谈
  - 证据引用: `L561-567: `bp = float(self._state.buying_power or 0)` / `if bp <= 0:` / `# 降级: 用 equity × 80% 兜底` / `bp = equity * 0.80`; get_available_budget_for_manual L602-607 同构`
  - 影响声称: 触发路径: 实盘满仓/保证金耗尽导致 broker 返回 power=0, 或 accinfo 'power' 字段为 'N/A' 被 _safe_float 归一为 0.0 (broker_adapter L753-765)。此时 auto 预算 = equity×80%×level% (normal 即 48% 净值), 上游按此幻象预算 sizing 并提交订单 — 轻则被 broker 批
  - 建议: 区分 '字段缺失/未刷新' 与 '真实 0': 仅当 buying_power 从未成功刷新 (如 _state.last_update 为空) 时才允许 equity 兜底, 且兜底值应远小于 80%; 真实 bp<=0 应返回 0.0 停止开仓。

<!-- DEB-VERDICT cluster=187 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: capital_safety.py:561-567: `bp = float(self._state.buying_power or 0); if bp <= 0: bp = equity * 0.80`。当 buying_power 因保证金耗尽真实为 0 时, 此兜底将购买力编造为 equity×80%, 混淆'真实零购买力'与'数据缺失'。broker 已拒绝下单的账户仍可在此层获得预算。L602-607 get_available_budget_for_manual 同构(L603 `if bp <= 0` → L607 `bp = equity * 0.80`)。属风控失效——绕过 broker 保证金约束。

### 议题 #188 [独有 1/2] `app/risk/capital_safety.py`:737-737 — _load_state 按 id DESC 取最新行, 双环境共存时加载到另一 trade_env 的状态行

环节: s7_risk_execution · 类别: state_machine · 自报最高 severity: S3
- **kimi-k3** (S3, state_machine, app/risk/capital_safety.py:737-737): _load_state 按 id DESC 取最新行, 双环境共存时加载到另一 trade_env 的状态行
  - 证据引用: ``c.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1")` — 表经 L723 唯一索引按 trade_env 各存 1 行, INSERT OR REPLACE 使最近写入的 env 行 id 最大`
  - 影响声称: 触发路径: 同一 main DB 上交替运行模拟与实盘 (表设计本身就支持 2 行)。上次跑 live, 本次启动 sim: SELECT 取到 id 最大的 live 行 → L744 saved_env != self.trade_env → 走环境切换重置分支, _peak_initialized=False 直接 return — 尽管 DB 里存在本 env 的有效行。结果: 该 env
  - 建议: 改为 `SELECT * FROM capital_state WHERE trade_env = ? ORDER BY id DESC LIMIT 1`; 仅当本 env 无行时才走初始化分支。

<!-- DEB-VERDICT cluster=188 -->
裁决: REJECTED | 置信=high | 理由: capital_safety.py:737 的 `ORDER BY id DESC LIMIT 1` 确实可能加载另一 trade_env 的行, 但 L723 已建 `CREATE UNIQUE INDEX ... ON capital_state(trade_env)` 确保每 env 最多一行; 更关键的是 L742-750 显式检查 `saved_env != self.trade_env`, 命中后设 `_peak_initialized=False` 并立即 return(L750), 不加载任何旧状态。P0 修复已完整覆盖此场景, 声称的'加载到另一环境状态行'不会发生。

### 议题 #189 [独有 1/2] `app/risk/capital_safety.py`:846-858 — reset_capital(显式值) 未置 _peak_initialized=True, 首次 update_account 会用真实净值覆盖手动基准

环节: s7_risk_execution · 类别: state_machine · 自报最高 severity: S2
- **kimi-k3** (S2, state_machine, app/risk/capital_safety.py:846-858): reset_capital(显式值) 未置 _peak_initialized=True, 首次 update_account 会用真实净值覆盖手动基准
  - 证据引用: `L846-854: 设置 `self._state.initial_capital = new_capital; peak_equity = new_capital; ...` 但全分支无 `self._peak_initialized = True`; 对照 L195-203 `if not self._peak_initialized: self._state.initial_capital = current_equity; self._state.peak_equity = current_equity; ...``
  - 影响声称: 触发路径: 在 _peak_initialized=False 的状态下 (首次启动未同步、或环境切换重置后、或 reset_capital(None) 之后) 调用 reset_capital(50000) 想固定基准; 下一次 update_account 进入 L195 分支, initial/peak 被当前真实净值 (如 $977K) 覆盖, 手动重置静默失效, 回撤基准与操作者预期不符
  - 建议: 在 new_capital 有效分支末尾加 `self._peak_initialized = True`, 使显式基准对后续 update_account 粘性生效。

<!-- DEB-VERDICT cluster=189 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: capital_safety.py:846-858 reset_capital(new_capital>0) 设置了 initial_capital/peak_equity/current_equity 等状态, 但全分支未设 `self._peak_initialized = True`。随后 update_account(L195-203) 检查 `if not self._peak_initialized`(L195) 为 True, 会用 broker 实时 current_equity 覆盖手动设定的 initial_capital 和 peak_equity(L196-197), 使显式重置值被静默丢弃。仅 else 分支(L859-861, new_capital=None/<=0)才设 `_peak_initialized=False`, 但显式值分支遗漏了 True。属功能错误。

### 议题 #194 [独有 1/2] `app/risk/position_exit_monitor.py`:2326-2327 — ATR 日 K 请求使用服务器本地 date.today() 而非美东日期

环节: s7_risk_execution · 类别: time_boundary · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, time_boundary, app/risk/position_exit_monitor.py:2326-2327): ATR 日 K 请求使用服务器本地 date.today() 而非美东日期
  - 证据引用: `_end_a = _d.today().isoformat()
_start_a = (_d.today() - _td(days=400)).isoformat()`
  - 影响声称: 如果服务进程运行在非美东时区，date.today() 的日期边界与美股交易日不一致；在美股盘中可能取到错误末日 K 线或未来日期边界，导致 ATR 基线失真，进而影响止损/追踪止损定价。
  - 建议: 统一使用 _now_et().date() 或美东交易日作为请求日期边界，避免本地时区漂移。

<!-- DEB-VERDICT cluster=194 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: position_exit_monitor.py:2326-2327 `_end_a = _d.today().isoformat()` / `_start_a = (_d.today() - _td(days=400)).isoformat()` 使用服务器本地 date.today() 而非美东日期。同文件 L101-102 已 import ET=timezone('US/Eastern'), L107 有 now_et, 但此处 ATR 日 K 请求未使用。服务器若在 UTC/CST 时区, 美股盘后(ET 16:00-20:00)时本地日期已超前一日, K 线 end 日期指向非交易日或被 API 截断, ATR 底限计算偏差。同模式 L2296-2297 VIXY 同理。影响有限(ATR 为 14 日均值), 属卫生问题。

### 议题 #195 [独有 1/2] `app/push/dedup.py`:21-35 — DedupCache.should_send 只读不预占，多线程并发下同 key 可重复发送

环节: s8_monitoring_surface · 类别: race_condition · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, race_condition, app/push/dedup.py:21-35): DedupCache.should_send 只读不预占，多线程并发下同 key 可重复发送
  - 证据引用: `ts = self._cache.get(key); if ts is not None and (now - ts) < self.ttl: return False`
  - 影响声称: should_send 返回 True 时不写入缓存，必须等发送成功后 mark_sent。若两个线程/协程在 mark_sent 前同时处理同一 dedup_key，二者都会判定可发送，造成重复推送与告警风暴。
  - 建议: 改为原子 check-and-reserve：should_send 在返回 True 时先写入临时占位或返回 token，发送失败再回滚；或使用独立 in-flight 集合防止并发同 key。

<!-- DEB-VERDICT cluster=195 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: dedup.py:21-35 should_send 在 `with self._lock` 内只 peek 缓存(L32-34), 返回 True 时不预占 key; mark_sent(L37-44) 在发送成功后才写入。hub.py:96 调 should_send → L113-117 异步发送 → 发送完才 mark_sent。多线程/异步并发下, 同 key 两条消息可在 mark_sent 前均通过 should_send 检查, 导致重复推送。此为 DEB-20260903 #110 修复(将记录从 should_send 推迟到 mark_sent)的副作用, 是新引入的竞态窗口, 与旧 #110(过早记录)不同。影响限于重复通知, 无资损。

### 议题 #198 [独有 1/2] `app/web_dashboard/order/executor.py`:120-126 — 无前端幂等键时使用固定 30s bucket，跨桶边界无法防重复下单

环节: s8_monitoring_surface · 类别: time_boundary · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, time_boundary, app/web_dashboard/order/executor.py:120-126): 无前端幂等键时使用固定 30s bucket，跨桶边界无法防重复下单
  - 证据引用: `bucket = int(time.time() // 30); return f"auto:{order.symbol}:{order.action}:{order.quantity}:{bucket}"`
  - 影响声称: 同一手动订单在 30s bucket 边界附近提交成功并重试时，新请求落入新 bucket，生成不同幂等 key，check_idempotency 不拦截，可能在极短时间内重复下出第二笔真实订单。
  - 建议: 改为滑动窗口去重：成功提交后记录 symbol+action+quantity 指纹与实际时间戳，30s 内任意时间都判重；或至少同时检查当前 bucket 与上一个 bucket。

<!-- DEB-VERDICT cluster=198 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: executor.py:125 bucket = int(time.time() // 30) 在 30s 桶边界处存在防重失效: 用户双击若分别落在 t=29.9 (桶 N) 和 t=30.1 (桶 N+1), 生成不同 idempotency_key (126 行), 两笔相同订单均可通过幂等校验。虽然窗口极窄 (<1s), 但确属功能缺陷, 跨桶边界时可产生重复下单 (错单)。代码 docstring (122-123 行) 明确承认此设计取舍, 但客观上构成防重漏洞。

### 议题 #199 [独有 1/2] `app/web_dashboard/order/executor.py`:229-236 — 订单提交成功即注册 ManualHoldRegistry，但可见撤单路径无对应注销

环节: s8_monitoring_surface · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/web_dashboard/order/executor.py:229-236): 订单提交成功即注册 ManualHoldRegistry，但可见撤单路径无对应注销
  - 证据引用: `manual_hold_registry.register(order.symbol, order.direction, int(order.quantity), reason=f"manual_order:{order.reason}")`
  - 影响声称: 手动订单提交成功后立即让自动管线跳过该标的；若该订单随后被撤销、过期或未成交，可见 cancel_order 路径没有注销 ManualHoldRegistry，残留标记可能长期阻断该 symbol 的自动扫描/退出逻辑。
  - 建议: 在订单生命周期终态（cancelled/expired/rejected/unfilled）统一注销 ManualHoldRegistry，或为该注册增加与订单状态/持仓存在性联动的自动过期清理。

<!-- DEB-VERDICT cluster=199 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: executor.py:231-234 手动开仓成功后调用 manual_hold_registry.register(), 但 cancel_order (executor.py:847-901) 撤单路径无任何 unregister 调用。全仓唯一注销点仅在 close_position 的 _effective_full 分支 (578 行)。开仓限价单被用户撤销后, 注册表残留该标的, is_manual_held 返回 True 阻止自动管线。实际影响因多进程架构有所缓解: 自动交易主进程依赖 SSOT (open_positions DB, 175-178 行) 而非 web_dashboard 进程内存 dict, 但 web_dashboard 进程内状态仍不一致。

### 议题 #196 [独有 1/2] `app/web_dashboard/data_service.py`:279-301 — HKD 账户汇率计算未防御 fx=0，后续除法可抛 ZeroDivisionError

环节: s8_monitoring_surface · 类别: numeric_unit · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, numeric_unit, app/web_dashboard/data_service.py:279-301): HKD 账户汇率计算未防御 fx=0，后续除法可抛 ZeroDivisionError
  - 证据引用: `fx = hkd_assets / usd_assets if usd_assets > 0 else 7.8 ... market_val / fx`
  - 影响声称: 当 HKD 账户 usd_assets>0 但 hkd_assets 为 0（或字段口径导致 total_assets 为 0）时，fx=0，后续 market_val/long_mv/short_mv/margin/frozen_cash 除 fx 会抛异常，使账户数据接口失败。
  - 建议: 对 fx 增加下限保护：if fx <= 0: fx = 7.8 或返回降级数据；同时校验 hkd_assets/usd_assets 的币种口径与负值场景。

<!-- DEB-VERDICT cluster=196 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: data_service.py:279 `fx = hkd_assets / usd_assets if usd_assets > 0 else 7.8` 仅防御 usd_assets<=0(兜底 7.8), 但当 hkd_assets=0 且 usd_assets>0 时 fx=0。后续 L289 `safe_float(raw.get('power')) / fx`、L296 `market_val / fx`、L297-301 long_mv/short_mv/initial_margin/maintenance_margin/frozen_cash 均除以 fx, 触发 ZeroDivisionError。账户初始化或数据异常时 hkd_assets 可为 0, 整条 HKD→USD 转换路径崩溃。属功能错误。

### 议题 #200 [独有 1/2] `app/web_dashboard/order/executor.py`:492-505 — 平仓单未成交路径撤单后不检查撤单结果，也不复核是否已成交

环节: s8_monitoring_surface · 类别: race_condition · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, race_condition, app/web_dashboard/order/executor.py:492-505): 平仓单未成交路径撤单后不检查撤单结果，也不复核是否已成交
  - 证据引用: `service._cancel_broker_order(order.order_id); order.error_msg = "平仓单未成交, 已自动撤单"`
  - 影响声称: 若平仓单在查询后、撤单前已成交，撤单可能失败但代码仍按“未成交已撤单”处理：不写 trade_records、释放竞态锁，导致实际仓位变化无记账，并可能再次触发重复平仓或退出监控误判。
  - 建议: 检查 _cancel_broker_order 返回值；撤单失败或疑似已成交时必须重新 order_list_query 复核 dealt_qty/order_status，并按实际成交记账与释放状态。

<!-- DEB-VERDICT cluster=200 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:492-505 平仓单未成交路径: (1) 495 行调用 _cancel_broker_order 但 496-499 行 except 仅 log warning, 撤单失败后仍设置 error_msg='已自动撤单' (500 行), 对外谎称撤单成功; (2) 撤单后未重新查询 broker 订单状态复核是否真正撤销; (3) 未检查在 status 查询与撤单之间订单是否已成交 (快速市场中可能已 fill)。撤单失败时残留 broker 挂单 + 系统认为已撤 = 后续意外 fill 导致双倍平仓, 属错单/状态错乱。

### 议题 #201 [独有 1/2] `app/web_dashboard/order/executor.py`:576-586 — 部分平仓也会取消 SL/TP 订单组，但不为剩余仓位重建保护

环节: s8_monitoring_surface · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, state_machine, app/web_dashboard/order/executor.py:576-586): 部分平仓也会取消 SL/TP 订单组，但不为剩余仓位重建保护
  - 证据引用: `manual_hold_registry.unregister(symbol) ... cancel_order_group(service, symbol)`
  - 影响声称: 部分成交时不会注销 ManualHoldRegistry，自动管线仍会跳过该标的；但同一成功分支无条件 cancel_order_group 会撤掉原 broker 端 SL/TP。剩余仓位可能同时失去 broker 保护与自动退出保护。
  - 建议: 部分平仓后应按剩余数量重建/调整 SL/TP，或仅在确认替代保护已恢复后才取消原订单组；不要无条件清空保护单。

<!-- DEB-VERDICT cluster=201 -->
裁决: CONFIRMED | severity=5 | 置信=high | 理由: executor.py:582-586 cancel_order_group(service, symbol) 缩进为 16 空格, 与 566 行 if _effective_full 同级, 即在部分平仓时同样执行。manual_hold_registry.unregister (578 行, 缩进 20) 仅在 _effective_full 内, 但 cancel_order_group 不受此限制。部分止盈/止损后, 剩余仓位的 SL/TP 保护性订单被全部取消, 且无重建逻辑, 剩余仓位完全裸露于市场风险中, 无止损保护。属风控失效, 若剩余仓位方向剧烈反转可造成重大资损。

### 议题 #202 [独有 1/2] `app/web_dashboard/order/executor.py`:719-722 — 全平成功路径不释放竞态锁，position_store.remove 失败时状态残留

环节: s8_monitoring_surface · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/web_dashboard/order/executor.py:719-722): 全平成功路径不释放竞态锁，position_store.remove 失败时状态残留
  - 证据引用: `if not _effective_full: _release_close_locks(symbol)`
  - 影响声称: 当 _effective_full 为 True 时不调用 _release_close_locks；若随后 position_store.remove 异常失败，_close_in_progress/_pending_close 仍残留在 position metadata 中，退出监控可能永久跳过该标的。
  - 建议: 无论全平/部分平仓，都在平仓流程最终 finally 或确认持仓清理失败后调用 _release_close_locks(symbol)，确保竞态锁不依赖 remove 成功。

<!-- DEB-VERDICT cluster=202 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:721-722 if not _effective_full: _release_close_locks(symbol) — 全平成功路径 (_effective_full=True) 不释放竞态锁, 依赖 572 行 position_store.remove 将整个仓位 (含 _close_in_progress/_pending_close 元数据) 删除。但 574-575 行 remove 异常被 debug 级日志吞掉, 仓位残留带锁元数据, 退出监控 (if _meta.get('_pending_close'): continue) 永久跳过该标的。旧 deb20260903 #117 仅在 740 行添加了外层 except 兜底, 修复了异常分支, 但全平成功 + position_store.remove 失败的组合路径仍未覆盖, 属修而不彻。

### 议题 #203 [独有 1/2] `app/web_dashboard/order/executor.py`:808-837 — 审计 DB 的今日订单过滤硬编码 ET+12，忽略美东冬令时

环节: s8_monitoring_surface · 类别: dataflow_inconsistency, time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/web_dashboard/order/executor.py:808-815): 审计 DB 的今日订单过滤硬编码 ET+12，忽略美东冬令时
  - 证据引用: `_lo = _et_now.strftime("%Y-%m-%dT12:00:00")`
  - 影响声称: 注释假定 ET 与北京固定差 12 小时，但 EST 冬令时差 13 小时。审计 DB fallback 的今日订单窗口会偏移 1 小时，导致今日订单列表多算/漏算边界订单。
  - 建议: 以 ET 当日 00:00 与次日 00:00 真正转换为写入时区（或统一 UTC/ISO 带时区存储）后过滤，不要硬编码 12:00 边界。
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/web_dashboard/order/executor.py:826-837): 审计 DB fallback 订单 filled_qty 恒为 0，成交口径失真
  - 证据引用: `"filled_qty": 0,`
  - 影响声称: 当 broker 查询失败降级到 manual_order_audit 时，即使状态包含 FILLED，filled_qty 也固定返回 0，面板对账与成交数量指标会低估实际成交量。
  - 建议: 在 manual_order_audit 或 trade_records 中持久化 dealt_qty/filled_qty，fallback 时读取真实成交量；至少在状态为 FILLED 时用 qty 标注为估算值。

<!-- DEB-VERDICT cluster=203 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: 两子问题均成立: (1) executor.py:814 _lo = _et_now.strftime('%Y-%m-%dT12:00:00') 硬编码 ET+12h 偏移, 注释 (809 行) 明确对齐 -12h 约定, 但冬令时 EST=UTC-5 时正确偏移应为 13h, 审计 DB 今日订单过滤在冬令时下边界偏移 1h (实际影响限于非交易时段, 盘中订单不受影响); (2) executor.py:836 filled_qty: 0 硬编码为 0, 审计 DB fallback 路径不区分已成交/未成交, 所有订单显示未成交, 成交口径失真。两者均为数据展示质量问题, 不影响交易执行。

### 议题 #197 [独有 1/2] `app/web_dashboard/data_service.py`:1028-1033 — asyncio Lock 释放后仅凭 lock.locked() 清理，可能误删仍有等待者的锁

环节: s8_monitoring_surface · 类别: race_condition · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, race_condition, app/web_dashboard/data_service.py:1028-1033): asyncio Lock 释放后仅凭 lock.locked() 清理，可能误删仍有等待者的锁
  - 证据引用: `if not lock.locked(): self._fetch_locks.pop(cache_key, None)`
  - 影响声称: asyncio.Lock 释放后即使存在已被唤醒但尚未恢复执行的等待者，lock.locked() 也可能为 False；此时从 _fetch_locks 弹出锁会让后续请求创建新锁，导致同一 cache_key 并发穿透，削弱 Thundering Herd 防护。
  - 建议: 不要仅用 lock.locked() 判断是否可清理；应记录等待者计数、使用弱引用缓存，或延迟清理到无等待且缓存已填充。

<!-- DEB-VERDICT cluster=197 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: asyncio Lock 释放后 lock.locked() 检查存在竞态窗口: data_service.py:1030 lock.release() 后, 等待协程尚未获得锁 (asyncio 单线程, 需 await 才能切换), lock.locked() 在 1032 行返回 False, 导致 self._fetch_locks.pop(cache_key, None) 移除锁。新请求在 993 行 setdefault 创建新锁, 与旧等待者并发获取同一 cache_key 数据, 击穿 thundering-herd 防护, 产生重复 API 调用。影响为缓存击穿/冗余请求, 不直接导致错单或资损。

### 议题 #206 [独有 1/2] `app/reports/post_market_summary.py`:45-73 — 盘后报告单笔盈亏误用总盈亏符号：总盈时亏损交易显示 '+$-50.00'，最佳交易硬编码 '+'

环节: s9_review_backtest · 类别: logic_anomaly · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, app/reports/post_market_summary.py:45-73): 盘后报告单笔盈亏误用总盈亏符号：总盈时亏损交易显示 '+$-50.00'，最佳交易硬编码 '+'
  - 证据引用: `pnl_sign = "+" if total_pnl >= 0 else "" (line 46，按总盈亏计算); 单笔明细却复用它: "lines.append(f\"• {trade_emoji} {symbol} {action}: {pnl_sign}${pnl:.2f} ({pnl_pct:+.2%})\")" (line 73); 最佳交易: "lines.append(f\"• 最佳: {symbol} +{pnl_pct:.2%}\")" (line 86)。`
  - 影响声称: total_pnl>0 而某笔亏损时，该笔显示 '+$-50.00'（双符号/错符号）；全日皆亏时"最佳交易"显示 '+-3.50%'——推送给人的盘后报告在金额符号上说谎。

<!-- DEB-VERDICT cluster=206 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: post_market_summary.py:46 pnl_sign = '+' if total_pnl >= 0 else '' 按总盈亏计算符号, 但在 73 行被复用于单笔交易明细: lines.append(f'• {trade_emoji} {symbol} {action}: {pnl_sign}${pnl:.2f}')。当总盈 (+) 而单笔亏 (pnl=-50) 时输出 '+$-50.00', 符号矛盾。此外 86 行最佳交易 lines.append(f'• 最佳: {symbol} +{pnl_pct:.2%}') 硬编码 '+' 前缀, 若所有交易均亏损 (最佳 pnl_pct 仍为负) 则显示 '+-2.00%' 等矛盾输出。属报告展示 bug, 不影响交易逻辑。

### 议题 #207 [独有 1/2] `app/reports/pre_market_strategy.py`:66-66 — 盘前报告 VIX 低波动文案混入俄语 'благоприятный'

环节: s9_review_backtest · 类别: other · 自报最高 severity: S1
- **kimi-k3** (S1, other, app/reports/pre_market_strategy.py:66-66): 盘前报告 VIX 低波动文案混入俄语 'благоприятный'
  - 证据引用: `vix_status = "低波动 ( благоприятный)" (line 66)`
  - 影响声称: 推送给中文用户的盘前报告出现俄文单词，疑似跨市场版本复制残留，损害报告可信度。

<!-- DEB-VERDICT cluster=207 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: app/reports/pre_market_strategy.py:66 硬编码 vix_status = "低波动 ( благоприятный)"，俄语词 'благоприятный' (意为'有利的') 直接混入中文文案。运行路径上该字符串会在 VIX<20 时写入盘前报告并推送，属于显示卫生问题，不影响交易逻辑或风控。

### 议题 #213 [独有 1/2] `app/review/review_orchestrator.py`:99-108 — collect_daily 无 PHANTOM_RECONCILE_VOID 排除与 source 白名单，与 daily_review 主查询口径分裂

环节: s9_review_backtest · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **kimi-k3** (S2, dataflow_inconsistency, app/review/review_orchestrator.py:99-108): collect_daily 无 PHANTOM_RECONCILE_VOID 排除与 source 白名单，与 daily_review 主查询口径分裂
  - 证据引用: `orchestrator: "FROM trade_records tr LEFT JOIN playbook_instructions pi ON tr.decision_id = pi.decision_id ... WHERE tr.trade_date = ? OR substr(tr.exit_time, 1, 10) = ?" (line 99-104)——无任何过滤; daily_review 主查询: "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') " "AND exit_reason != 'PHANTOM_RECO`
  - 影响声称: 幽灵对账行进 trades 列表（exit_price>0 时还会进 closed/by_source 统计），非 playbook 来源交易也混入 LLM 辩论上下文；同一日期 orchestrator 与 daily_review 两份"日复盘"的笔数/胜率对不上，辩论基于被污染的数据归因。

<!-- DEB-VERDICT cluster=213 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: review_orchestrator.py:99-104 collect_daily 查询 WHERE tr.trade_date = ? OR substr(tr.exit_time, 1, 10) = ? 无任何 source_type 白名单及 PHANTOM_RECONCILE_VOID 排除。对照 daily_review.py:78-79 主查询同时有 source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') 和 exit_reason != 'PHANTOM_RECONCILE_VOID'。旧 DEB-20260903 #131 仅修了出场日匹配 (line 101-103 注释)，过滤口径分裂仍未修。orchestrator 聚合可含非 playbook 来源及幽灵冲销记录。

### 议题 #216 [独有 1/2] `app/review/review_orchestrator.py`:497-504 — RichContext 资金轨迹不按复盘日期截断，历史复盘会引入未来资金状态

环节: s9_review_backtest · 类别: time_boundary · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, time_boundary, app/review/review_orchestrator.py:497-504): RichContext 资金轨迹不按复盘日期截断，历史复盘会引入未来资金状态
  - 证据引用: `        capital = self._query(
            "SELECT current_equity, peak_equity, drawdown_pct, water_level, "
            "cash, market_value, last_update "
            "FROM capital_state ORDER BY last_update DESC LIMIT 14"
        )
        return list(reversed(capital))  # 旧→新`
  - 影响声称: RichContextBuilder.build(date) 可传入历史复盘日期，但 _build_capital_trajectory 忽略 date，只取最新 14 条 capital_state。补跑历史复盘或回看某日时，辩论上下文会包含该日之后才出现的资金权益/回撤/水位，形成前视偏差。
  - 建议: 按 target_date 截断：WHERE last_update <= 对应日期或先取最近 N 条再在 Python 侧过滤 last_update <= target_date 23:59:59 ET。

<!-- DEB-VERDICT cluster=216 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: review_orchestrator.py:497-504 _build_capital_trajectory 查询 ORDER BY last_update DESC LIMIT 14 完全不接受 date 参数过滤，始终返回最新 14 条资金快照。历史日期复盘时 RichContext 资金轨迹展示的是当前而非复盘时点的资金变化曲线，与 #210 同根因但影响的是 trajectory 可视化而非单日评分。

### 议题 #217 [独有 1/2] `app/review/review_orchestrator.py`:668-674 — 辩论线程超时后未取消，后台线程可继续写入已返回的 results

环节: s9_review_backtest · 类别: race_condition · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, race_condition, app/review/review_orchestrator.py:668-674): 辩论线程超时后未取消，后台线程可继续写入已返回的 results
  - 证据引用: `        deadline = time.time() + timeout
        for t in threads:
            remaining = max(0, deadline - time.time())
            if remaining > 0:
                t.join(timeout=remaining)
        
        return results, success_count`
  - 影响声称: 若某个模型调用超过 timeout，join 超时后函数直接返回 results/success_count，但后台线程仍可能在之后写入 results。调用方已开始消费/序列化 results 时，late write 会造成辩论结果不确定、模式判断与实际内容不一致。
  - 建议: 使用可取消的并发原语（如 concurrent.futures 并在超时后忽略晚到结果），或在返回前拷贝 results 并加锁冻结；后台线程写入前检查 run_id/generation 是否仍有效。

<!-- DEB-VERDICT cluster=217 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: review_orchestrator.py:668-674 join(timeout=remaining) 超时后线程未取消 (Python 无线程取消机制)。daemon 线程继续运行，call_one (line 638-660) 仍可通过 with self._lock 写入 results dict 及 success_count，而此时 _run_round 已返回 (line 674)。后续 round 读取被污染的 results 可影响辩论收敛判断。线程安全竞态，属状态错乱类。

### 议题 #209 [独有 1/2] `app/review/daily_review.py`:781-785 — _get_plan_vs_actual 实际成交查询漏掉 PHANTOM_RECONCILE_VOID 排除，与主查询口径不一致

环节: s9_review_backtest · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **kimi-k3** (S2, dataflow_inconsistency, app/review/daily_review.py:781-785): _get_plan_vs_actual 实际成交查询漏掉 PHANTOM_RECONCILE_VOID 排除，与主查询口径不一致
  - 证据引用: `主查询: "AND exit_reason != 'PHANTOM_RECONCILE_VOID' " (line 79); plan_vs_actual: "SELECT symbol, direction FROM trade_records WHERE trade_date = ? " "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')" (line 782-783)——注释自称"D3 白名单, 与主查询同口径"(line 779) 但缺 phantom 排除。`
  - 影响声称: 幽灵对账行被当作真实成交参与 (symbol,direction) 匹配：可把未执行指令误判为"已执行"，或制造假的 unplanned_trades（line 754 注释称计划外">0 即链路 bug"），执行质量维度产生误报。

<!-- DEB-VERDICT cluster=209 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 主查询 daily_review.py:79 有 AND exit_reason != 'PHANTOM_RECONCILE_VOID' 排除，但 _get_plan_vs_actual 的实际成交子查询 (daily_review.py:829-832) 仅过滤 source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')，缺少 PHANTOM 排除。幽灵对账冲销记录可被计入 actual_pairs，导致计划执行比对把已冲销交易误标为'已执行'，属数据口径不一致。

### 议题 #210 [独有 1/2] `app/review/daily_review.py`:965-966 — 历史日期复盘时 capital_state 静默回退为最新快照，风控维度/评分用错日期的回撤

环节: s9_review_backtest · 类别: time_boundary · 自报最高 severity: S2
- **kimi-k3** (S2, time_boundary, app/review/daily_review.py:965-966): 历史日期复盘时 capital_state 静默回退为最新快照，风控维度/评分用错日期的回撤
  - 证据引用: `"# 未匹配到当日 → 回退最新一条 (优于返回空)" / "rows = [_matched] if _matched else rows[:1]" (line 965-966); orchestrator 更直接: "SELECT * FROM capital_state ORDER BY last_update DESC LIMIT 1" (line 133-135) 完全不看复盘日期。`
  - 影响声称: 补跑/复盘历史日期时，_analyze_risk_compliance、_calculate_review_score、_detect_market_regime、_generate_suggestions_v2 全部把"今天"的 equity/drawdown 当作该日状态，评分与风控建议失真且返回 dict 中无任何 stale 标记。

<!-- DEB-VERDICT cluster=210 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: daily_review.py:1014 当 _matched 为 None (历史日期无匹配快照) 时静默回退 rows[:1] 即最新一条，不告知调用方。orchestrator:133-134 更直接 SELECT * FROM capital_state ORDER BY last_update DESC LIMIT 1 完全不按复盘日期过滤。历史复盘时回撤/水位/评分均使用最新资金状态而非当日快照，风控维度失真。

### 议题 #218 [独有 1/2] `app/review/review_orchestrator.py`:2041-2047 — drawdown_pct 符号/量纲语义跨文件漂移：daily_review 取 abs 按百分点比 5/10/15，orchestrator 按有符号小数比 -0.1/-0.05

环节: s9_review_backtest · 类别: numeric_unit · 自报最高 severity: S3
- **kimi-k3** (S3, numeric_unit, app/review/review_orchestrator.py:2041-2047): drawdown_pct 符号/量纲语义跨文件漂移：daily_review 取 abs 按百分点比 5/10/15，orchestrator 按有符号小数比 -0.1/-0.05
  - 证据引用: `orchestrator: "dd = c.get(\"drawdown_pct\", 0)" (line 2041), "severity\": \"P0\" if dd < -0.1 else \"P1\" if dd < -0.05 else \"P2\"" (line 2047); daily_review 对同一字段: "'drawdown_pct': round(r.get('drawdown_pct', 0) * 100, 2)" (line 973) 后 "if abs(dd) < 5:" (line 319)——abs() 的广泛使用说明写入侧符号约定不可靠。`
  - 影响声称: 若 capital_state.drawdown_pct 存正幅值，orchestrator 风控验证在 20% 回撤下也只会落 P2，P0/P1 永不触发，风控类 claim 严重性被系统性低估；另外 dd 为 NULL 时 c.get 返回 None，dd < -0.1 直接 TypeError。

<!-- DEB-VERDICT cluster=218 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: DB 存储 drawdown_pct 为有符号小数 (如 -0.08)。orchestrator:2041 取原始值，line 2047 按 dd < -0.1 / dd < -0.05 有符号比较分 P0/P1/P2 (正确)。daily_review:1021 乘 100 转百分点 (如 -8.0)，line 497/586/1150 取 abs 后按 5/10/15 阈值评分 (亦正确)。两套量纲当前均能正确运算，但跨文件符号/单位语义分裂 (一处有符号小数、一处绝对值百分点)，属卫生隐患——任何一方改存储格式即触发对方静默错误。

### 议题 #220 [独有 1/2] `app/review/review_orchestrator.py`:2360-2366 — ReviewOrchestrator 单例首次初始化后忽略后续传入的 notifier/agent_bridge

环节: s9_review_backtest · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/review/review_orchestrator.py:2360-2366): ReviewOrchestrator 单例首次初始化后忽略后续传入的 notifier/agent_bridge
  - 证据引用: `def get_review_orchestrator(notifier=None, agent_bridge=None) -> ReviewOrchestrator:
    global _orchestrator
    # P0 修复 (2026-05-03): 加锁防止并发初始化竞态
    with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = ReviewOrchestrator(notifier=notifier, agent_bridge=agent_brid`
  - 影响声称: 若系统早期以 get_review_orchestrator() 或空 agent_bridge 初始化单例，后续即使传入可用 agent_bridge，也仍返回旧实例；MultiModelDebateRunner 可能拿到 None/旧 bridge，导致辩论失败或走 fallback，且调用方难以察觉参数未生效。
  - 建议: 若后续参数非空，应更新单例的 notifier/agent_bridge，或提供显式 reset/重建接口；至少在参数冲突时记录告警。

<!-- DEB-VERDICT cluster=220 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: review_orchestrator.py:2360-2366 get_review_orchestrator 仅首次调用时以 notifier/agent_bridge 构造实例 (line 2364-2365 if _orchestrator is None)，后续传入不同参数被静默忽略。与旧 DEB-20260903 #103 (capital_safety 单例) 及 #71 (symbol_lock 单例) 同模式。正常运行中若调用方先后传不同 notifier，第二个 notifier 不会生效，属卫生/设计缺陷。

### 议题 #221 [独有 1/2] `trade_digest_summary_2026-08-04_2026-09-03.md`:42-98 — SL_INITIAL 立即错误仓：把早期硬止损上限从 2.5% 收到 1.5%

环节: t2_strategy_exit · 类别: profit_strategy · 自报最高 severity: S5
- **qwen3.8-max-0902** (S4, profit_strategy, trade_digest_summary_2026-08-04_2026-09-03.md:72-73): risk_on 市况降权：只允许高置信或小仓位通过
  - 证据引用: `L72: | risk_on | 172 | -417.49 | -2.43 | 111 |; L73: | cautious | 63 | +1358.46 | +21.56 | 55 |`
  - 影响声称: risk_on 占 172/236 笔，但毛 PnL 为 -417.49，单均 -2.43；cautious 只有 63 笔却贡献 +1358.46。若只过滤一半低质 risk_on 交易，毛 PnL 改善约 +208.7；另按 L36 合计毛 +1134.89 / 净 -8.92 推算总费用约 1143.81、单均费用约 4.85，减少 86 笔可节省费用约 417；合计保守月改善约 $625。
  - 建议: 在 playbook admission / instruction_generator 增加 market_regime 分支：当 market_regime=='risk_on' 时，min_confidence 从 60 提高到 75，并将 position_scale 压到 0.3；或新增 risk_on_min_confidence=75、risk_on_position_scale=0
- **qwen3.8-max-0902** (S5, profit_strategy, trade_digest_summary_2026-08-04_2026-09-03.md:42-42): SL_INITIAL 立即错误仓：把早期硬止损上限从 2.5% 收到 1.5%
  - 证据引用: `L42: | SL_INITIAL | 17 | -2732.47 | -160.73 | 0 | 17 | 2.2 | 0.02 | 3.27 |`
  - 影响声称: SL_INITIAL 17 笔全亏，毛亏 -2732.47，是当月总毛盈利 1134.89 的 2.4 倍；均 MFE 仅 0.02pp，说明这类仓入场后几乎没有正向验证。若把 30 分钟内无浮盈仓的早期止损上限从 2.5% 压到 1.5%，即使只保守估计平均亏损降低 30%，也可改善约 2732.47*0.30≈$820/月；若同时减少此类入场，还可节省约 17*4.85≈$82 费用。
  - 建议: 修改 playbook.execution_parameters：early_tight_sl_pct_max 从 2.5 改为 1.5；early_tight_sl_atr_mult 从 0.5 改为 0.35；保留 early_tight_sl_mfe_gate=0.3，并新增：入场后 15 分钟 MFE<0.05% 且水下>0.8% 时直接采用 1.0% 硬止损。ATR 缺失时回退固定 1.
- **qwen3.8-max-0902** (S3, profit_strategy, trade_digest_summary_2026-08-04_2026-09-03.md:45-49): 停用或大幅收紧 patient_exit，避免死信号等待变成扩大亏损
  - 证据引用: `L45: | PATIENT_EXIT_STOP | 3 | -274.19 | -91.40 | 0 | 3 | 3.3 | 0.00 | 1.62 |; L46: | DEAD_SIGNAL_EXIT | 29 | -194.83 | -6.72 | 17 | 12 | 4.0 | 0.11 | 1.44 |; L47: | PATIENT_EXIT_EOD | 4 | -180.43 | -45.11 | 0 | 4 | 5.6 | 0.09 | 1.13 |; L49: | PATIENT_EXIT_FILL | 1 | +0.00 | +0.00 | 0 | 0 | 1.0 | 0.`
  - 影响声称: PATIENT_EXIT_STOP/EOD/FILL 合计 8 笔，毛亏 -274.19-180.43+0=-454.62，且 0 胜；同窗口普通 DEAD_SIGNAL_EXIT 均亏仅 -6.72。若这 8 笔按立即死信号出场，估算毛亏约 8*(-6.72)=-53.76，可改善毛 PnL 约 $400.9/月。该规则当前没有体现出保本优势，反而把浅亏拖成深亏。
  - 建议: 将 patient_exit_enabled 置 false。若希望保留该机制，改为更严格版本：patient_exit_max_underwater_pct 从 1.0 降到 0.3；patient_exit_stop_pct 从 1.5 降到 0.8；patient_exit_deadline_hhmm 从 1550 提前到 1430；且只有入场后曾达到 MFE>=0.2% 的死信号仓才允许进
- **qwen3.8-max-0902** (S3, profit_strategy, trade_digest_summary_2026-08-04_2026-09-03.md:98-98): 6-16h 持仓是最大亏损桶：重启水下隔夜深度上限并缩短豁免天数
  - 证据引用: `L98: | 6-16h | 23 | -1044.36 | -45.41 | 65% |`
  - 影响声称: 6-16h 持仓桶 23 笔毛亏 -1044.36，是所有持仓桶中唯一大额负贡献；虽然胜率 65%，但单笔亏损足够大，说明隔夜/持有时间拉长后尾部风险失控。若通过水下深度上限和缩短豁免天数只砍掉一半该桶亏损，月改善约 +$522；即使只避免类似 OVERNIGHT_GAP_STOP 的尾部单笔，也能减少约 $380 级别损失。
  - 建议: 修改 playbook.execution_parameters：loser_overnight_max_depth_pct 从 99 改为 1.5；loser_overnight_max_hold_days 从 5 改为 2；同时在 15:50 EOD 检查中，对水下超过 1.5% 的 PLAYBOOK_SIM 仓强制不再享受隔夜豁免，直接走 PRE_CLOSE/止损。盈利仓可继续由 winne
- **qwen3.8-max-0902** (S3, profit_strategy, trade_digest_summary_2026-08-04_2026-09-03.md:91-91): 把首层止盈从 5% 降到 1.5%，捕获 73 笔 1pp+ 浮盈让渡
  - 证据引用: `L91: - 达峰≥1pp 但只落袋不到一半的笔数: 73 (浮盈让渡候选池)`
  - 影响声称: 当月有 73 笔曾达到至少 1pp 浮盈但最终落袋不到一半。以 L53 SL_TRAIL_LOCK 的 62 笔 +3666.24、均 MFE 2.02pp 粗略推算，每 pp 每笔约贡献 3666.24/(62*2.02)≈$29.3；若只对 73 笔候选池额外捕获 0.25pp，月毛改善约 73*0.25*29.3≈$535。即使只修复 L89 的 21 笔回吐亏损 -325.21，也有约 $
  - 建议: 在 playbook 退出路径新增日内分批止盈：浮盈达到 1.5% 时先平 25%，剩余仓位止损移动到保本+0.4% 费用垫；第二层可设在 3.0%。若沿用 ATR 表达，可把追踪/锁盈激活门槛从约 1.2ATR 降到 0.8ATR，但必须同时保留趋势仓的剩余 75% 继续参与。新增字段建议：scale_out_first_pct=1.5、scale_out_first_ratio=0.25、sc

<!-- DEB-VERDICT cluster=221 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 五条数字均可由材料L42/45-49/72-73/91/98复算。代码: risk_on无降权(pipeline.py:4053 mult=1.0); SL收紧半修(early_tight_sl启用但max仍2.5%, config L1105); patient_exit仍enabled(L1138); loser_overnight深度=99%(L1281); TP仍5%(L269)。均未实现。

### 议题 #222 [独有 1/2] `trade_digest_2026-08-04_2026-09-03.md`:36-73 — risk_on 状态降仓并提高信号门槛，把风险预算转移给 cautious

环节: t2_trade_review · 类别: profit_strategy · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, profit_strategy, trade_digest_2026-08-04_2026-09-03.md:36-73): risk_on 状态降仓并提高信号门槛，把风险预算转移给 cautious
  - 证据引用: `| **合计** | 236 | **+1134.89** | **-8.92** | | | | | |
| risk_on | 172 | -417.49 | -2.43 | 111 |
| cautious | 63 | +1358.46 | +21.56 | 55 |`
  - 影响声称: 合计毛PnL +1134.89、净PnL -8.92（line 36），按净盈亏=毛盈亏-估算费用倒推，总费用≈1134.89-(-8.92)=1143.81，平均≈4.85/笔。risk_on 172笔毛亏 -417.49；若 risk_on 降仓50%，毛亏减少≈208.75，同时费用节省≈172*0.5*4.85≈417，月净改善≈626；若完全过滤 risk_on，改善≈417.49+83
  - 建议: 在下单函数中增加 regime gate：market_regime=='risk_on' 时仓位乘数设为0.5，并只接受置信>=0.7、初始止损<=1.5pp 的信号；market_regime=='cautious' 时仓位乘数保持1.0。再加滚动保护：最近5个交易日 risk_on 净PnL<0 时，risk_on 仓位乘数降至0.25。风险与反例：单月样本可能高估 risk_on 劣势；若
- **qwen3.8-max-0902** (S4, profit_strategy, trade_digest_2026-08-04_2026-09-03.md:42-43): 用更窄初始止损和进场后快速失效压缩 SL_INITIAL / MFE_STALL 大亏
  - 证据引用: `| SL_INITIAL | 17 | -2732.47 | -160.73 | 0 | 17 | 2.2 | 0.02 | 3.27 |
| MFE_STALL_EXIT | 10 | -490.66 | -49.07 | 0 | 10 | 2.8 | 0.16 | 1.96 |`
  - 影响声称: 两类合计27笔、毛亏 -2732.47 + -490.66 = -3223.13。SL_INITIAL 平均MAE 3.27pp 但平均MFE仅0.02pp，说明进场后几乎没有有利偏移；若把初始硬止损压到1.5pp，理论损失降幅=(3.27-1.5)/3.27≈54%，对应 -2732.47*54%≈1476；考虑跳空/滑点只按一半实现，改善≈738。MFE_STALL 若提前10-15分钟失效并
  - 建议: 修改止损模块：初始硬止损=min(当前策略止损, 1.5pp)；若信号需要更宽止损，则仓位按1.5/原止损等比缩小。新增进场快速失效：入场后5-10分钟内若实时MFE<0.15pp且当前价低于入场价-0.5pp，立即市价退出；若前5分钟实时MAE>1.0pp，直接退出。风险与反例：开盘噪声可能导致更多扫损，并误杀少量后续反转单；建议先对同一信号集做1.5pp止损与现行止损的A/B模拟。
- **qwen3.8-max-0902** (S3, profit_strategy, trade_digest_2026-08-04_2026-09-03.md:44-47): 收盘前清理无浮盈的长持仓，限制隔夜跳空损失
  - 证据引用: `| OVERNIGHT_GAP_STOP | 1 | -380.29 | -380.29 | 0 | 1 | 6.3 | 0.00 | 6.42 |
| PATIENT_EXIT_STOP | 3 | -274.19 | -91.40 | 0 | 3 | 3.3 | 0.00 | 1.62 |
| DEAD_SIGNAL_EXIT | 29 | -194.83 | -6.72 | 17 | 12 | 4.0 | 0.11 | 1.44 |
| PATIENT_EXIT_EOD | 4 | -180.43 | -45.11 | 0 | 4 | 5.6 | 0.09 | 1.13 |`
  - 影响声称: OVERNIGHT_GAP_STOP、PATIENT_EXIT_STOP、PATIENT_EXIT_EOD 三类合计8笔，毛亏 -380.29 + -274.19 + -180.43 = -834.91，且共同特征是持仓较长、MFE接近0；同时6-16h持仓桶整体毛亏 -1044.36（line 98）。若收盘前强制清掉无浮盈长持仓，保守按减少上述三类损失50%计，月改善≈834.91*50%≈4
  - 建议: 增加EOD审查：15:25-15:35若持仓预计隔夜且 mfe_pct<0.5pp 或当前浮盈<2*fees_est，则市价平仓；只有 mfe_pct>=1.0pp 且浮盈>=2*fees_est 的单才允许隔夜，并设置更紧的隔夜跳空止损。风险与反例：6-16h桶胜率65%（line 98），一刀切会牺牲部分正收益；应只清理无浮盈/费用覆盖不足的单，保留有峰值浮盈的锁盈单。

<!-- DEB-VERDICT cluster=222 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 三条数字均可由材料L36-47复算。risk_on降权未实现(pipeline.py:4053 mult均1.0); SL部分修(early_tight_sl启用但max=2.5%/L1105); MFE_STALL已关闭(config L1144,该子项冗余); patient/dead_signal仍enabled(L1115/L1138),EOD清理未动。

### 议题 #223 [独有 1/2] `__OFF_MANIFEST__:app/review/improvement_generator.py`:34-38 — review 三个零 import 小模块疑似未挂载，先标注待动态加载确认

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S2
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S2, cleanup_redundancy, app/review/improvement_generator.py:34-38): review 三个零 import 小模块疑似未挂载，先标注待动态加载确认 ⚠️OFF-MANIFEST
  - 证据引用: `L34: | app/review/improvement_generator.py | 0 | 2 | 3701 | 93 | 2026-06-16 |  |
L37: | app/review/psychology_analyzer.py | 0 | 2 | 2783 | 78 | 2026-06-16 |  |
L38: | app/review/strategy_analysis.py | 0 | 3 | 2551 | 72 | 2026-06-16 |  |`
  - 影响声称: 三个模块 import=0 且无 __main__，静态看没有入口；但 string 分别为 2/2/3，按清单口径可能是文档、测试或 importlib 动态加载提及。影响半径：review_orchestrator、daily_review 或面板 review 页可能通过字符串加载这些模块；在未定位这 2-3 处文本提及前不能删除，否则会误伤动态链路。
  - 建议: 保留但在文件头标注状态：在三个文件头注明‘零静态 import，待确认动态加载；若某日前无引用再删除’。验证：grep -RInF 'improvement_generator' .、grep -RInF 'psychology_analyzer' .、grep -RInF 'strategy_analysis' . 逐条归类提及；运行 review_orchestrator/daily_revi

<!-- DEB-VERDICT cluster=223 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: 三模块在review/__init__.py:9-12有import(L18-21 export),'0 imports'严格不准确。但全仓grep确认ImprovementGenerator/PsychologyAnalyzer/StrategyAnalyzer无外部消费者,属死导出,功能冗余可处置。

### 议题 #228 [独有 1/2] `__OFF_MANIFEST__:data/backups_r9_r13/ftseek_us.db.backup.20260823_093339`:51-54 — 迁移/旧版大型 DB 备份残留，超出当前备份目录语义

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, data/backups_r9_r13/ftseek_us.db.backup.20260823_093339:51-54): 迁移/旧版大型 DB 备份残留，超出当前备份目录语义 ⚠️OFF-MANIFEST
  - 证据引用: `L51: | data/backups_r9_r13/ftseek_us.db.backup.20260823_093339 | 18870272 | 2026-08-23 | 12 |
L52: | data/backups/ftseek_us.db.bak-20260813-153822-pre-dedup | 15306752 | 2026-08-13 | 22 |
L53: | data/ftseek_us.db.bak_20260804 | 13467648 | 2026-08-04 | 30 |
L54: | data/archive/ftseek_us.db.bak_r4_202`
  - 影响声称: 这些是 r9_r13、pre-dedup、r4 等一次性迁移或去重前备份，和 data/backups/ 常规日期备份目录并存，会让恢复时误选旧模式备份。它们不是代码依赖，但可能被人手工恢复引用。影响半径：运维恢复脚本或人工恢复流程；需要确认当前备份目录已覆盖所需恢复点。
  - 建议: 归档到仓外：将这四份大型旧备份移出仓库或主数据盘，不在仓内保留；若合规需长期保存，放对象存储并登记。验证：grep -RInF 'backups_r9_r13' .、grep -RInF 'bak-20260813-153822-pre-dedup' .、grep -RInF 'bak_20260804' .、grep -RInF 'bak_r4_20260802' . 确认无脚本引用；运行当前 

<!-- DEB-VERDICT cluster=228 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: ls确认: backups_r9_r13/ftseek_us.db.backup.20260823_093339=18870272B(=声称L51), backups/ftseek_us.db.bak-pre-dedup=15306752B(=L52)。迁移/去重前快照超出标准备份语义,~34MB残留无引用。

### 议题 #224 [独有 1/2] `__OFF_MANIFEST__:config/unified_config.yaml.bak.nodiscount-0815`:140-147 — unified_config.yaml 多份历史备份残留，易被误当成当前配置

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S4
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S4, cleanup_redundancy, config/unified_config.yaml.bak.nodiscount-0815:140-147): unified_config.yaml 多份历史备份残留，易被误当成当前配置 ⚠️OFF-MANIFEST
  - 证据引用: `L140: | config/unified_config.yaml.bak.nodiscount-0815 | 66398 | 2026-08-15 | 19 |
L141: | config/unified_config.yaml.bak.q12-081520 | 65283 | 2026-08-15 | 19 |
L142: | config/unified_config.yaml.bak.debcap-081519 | 60202 | 2026-08-15 | 19 |
L143: | config/unified_config.yaml.bak.deb0815-0815 | 5953`
  - 影响声称: 多份 .bak 文件名携带 nodiscount、q12、debcap、reanchor_off、topup_quota 等不同实验语义，且分布在 config/ 与 config/backups/，会让新人或脚本误把过期参数当成当前统一配置。影响半径：配置加载通常只读 config/unified_config.yaml，但人工排障、grep、复制粘贴可能引用错误文件；需确认没有脚本、syste
  - 建议: 归档到仓外：将 config/unified_config.yaml.bak.* 与 config/backups/unified_config.yaml.bak_* 移出仓库，仅保留当前 config/unified_config.yaml。验证：grep -RInF 'unified_config.yaml.bak' . 确认无活跃引用；运行配置校验或应用启动；ftseek-us.servic

<!-- DEB-VERDICT cluster=224 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: ls确认5份.bak: nodiscount-0815(66398B)/q12-081520(65283B)等与声称L140-142吻合。全仓grep无代码引用.bak文件,属历史残留,有误操作混淆风险。

### 议题 #230 [独有 1/2] `__OFF_MANIFEST__:data/trade_records.json.deprecated_20260715`:200-200 — 已显式 deprecated 的 trade_records.json 旧数据残留

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, data/trade_records.json.deprecated_20260715:200-200): 已显式 deprecated 的 trade_records.json 旧数据残留 ⚠️OFF-MANIFEST
  - 证据引用: `L200: | data/trade_records.json.deprecated_20260715 | 2457 | 2026-03-26 | 161 |`
  - 影响声称: 文件名明确 deprecated，且 161 天未变化；真实交易记录更可能由 trade_records.db / trade_store.py 等现行存储链路管理。保留旧 JSON 会让历史分析误选过期口径。影响半径：主要影响人工分析或未知一次性脚本；需确认无代码读取该文件名，尤其避免误当作交易记录事实源。
  - 建议: 归档到仓外后删除仓内文件。验证：grep -RInF 'trade_records.json.deprecated_20260715' . 与 grep -RInF 'trade_records.json' . 确认无生产读取；运行 trade_store / dashboard 交易记录相关冒烟，确认交易页仍读取 trade_records.db 或现行数据源。

<!-- DEB-VERDICT cluster=230 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: data/trade_records.json.deprecated_20260715 文件存在（2457行），grep 全仓无任何生产代码引用，仅 debates 文档提及。文件名已显式标记 deprecated，系统已迁移至 trade_records.db。属可安全清理的死数据残留。

### 议题 #227 [独有 1/2] `__OFF_MANIFEST__:data/backups/2026-09-04/ftseek_us_live.db`:205-205 — ftseek_us_live.db 多日备份同哈希，备份链存在虚假多版本

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S4
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S4, cleanup_redundancy, data/backups/2026-09-04/ftseek_us_live.db:205-205): ftseek_us_live.db 多日备份同哈希，备份链存在虚假多版本 ⚠️OFF-MANIFEST
  - 证据引用: `L205: - 1159168B ×7: data/backups/2026-08-31/ftseek_us_live.db, data/backups/2026-09-03/ftseek_us_live.db, data/backups/2026-09-01/ftseek_us_live.db, data/backups/2026-09-04/ftseek_us_live.db, data/backups/2026-08-29/ftseek_us_live.db, data/backups/2026-08-23/ftseek_us_live.db, data/backups/2026-09-`
  - 影响声称: 7 个日期备份同 sha256，说明这些备份点没有数据差异；全部保留会制造‘每日可恢复’的错觉并占用空间。影响半径：备份/恢复运维流程，尤其可能存在的活跃 db_backup 任务；不能删除唯一最新可恢复点，需保留代表副本并确认备份任务仍能生成新文件。
  - 建议: 合并去重：保留最新一份 data/backups/2026-09-04/ftseek_us_live.db，其余同哈希副本删除；如合规要求留存，先移出仓外再删除。验证：sha256sum 确认仅保留代表副本；sqlite3 data/backups/2026-09-04/ftseek_us_live.db 'PRAGMA integrity_check' 通过；手工触发或等待下一次 DB 备份 c

<!-- DEB-VERDICT cluster=227 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: md5sum验证7份(08-23至09-04)ftseek_us_live.db均1159168B且MD5=2722fde4277ba35409b1476dc9f09b7d,与声称L205吻合。7份日备内容相同,虚假多版本致恢复操作版本混淆。

### 议题 #225 [独有 1/2] `__OFF_MANIFEST__:data/_quarantine/20260903_test_pollution/playbook_reconciliation.all_rows.jsonl`:206-206 — 隔离区内 playbook_reconciliation 全量与 .orig 同哈希重复

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, data/_quarantine/20260903_test_pollution/playbook_reconciliation.all_rows.jsonl:206-206): 隔离区内 playbook_reconciliation 全量与 .orig 同哈希重复 ⚠️OFF-MANIFEST
  - 证据引用: `L206: - 1156010B ×2: data/_quarantine/20260903_test_pollution/playbook_reconciliation.all_rows.jsonl, data/_quarantine/20260903_test_pollution/playbook_reconciliation.jsonl.orig`
  - 影响声称: 同 sha256 说明两份文件字节完全一致；.orig 已作为隔离样本保留，all_rows.jsonl 若只是同内容副本，不增加审计信息，反而制造‘哪份是权威样本’的歧义。影响半径：只限隔离/审计脚本或人工排查；需确认没有脚本固定读取 all_rows.jsonl 或 .orig。清单未提供该路径的文本引用明细，因此必须先全仓搜索与 crontab 排查，不能仅凭重复哈希直接删除。
  - 建议: 合并去重：保留 playbook_reconciliation.jsonl.orig 作为隔离样本，删除 playbook_reconciliation.all_rows.jsonl；若审计流程明确要求 all_rows 命名，则反向保留一份，另一份删除。验证：grep -RInF 'playbook_reconciliation.all_rows.jsonl' . 与 grep -RInF 'p

<!-- DEB-VERDICT cluster=225 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: md5sum验证两文件均1156010B且MD5=2208cd39e7c7342bfb7436a4ebfaefea,与声称L206吻合。同处隔离区内.orig与.all_rows内容完全一致,冗余可删。

### 议题 #226 [独有 1/2] `__OFF_MANIFEST__:data/archive/test_pollution_cleanup_20260813/daily_reviews/daily_review_2026-05-07.json`:208-254 — test_pollution_cleanup 归档与 data/daily_reviews 现存同哈希重复

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, data/archive/test_pollution_cleanup_20260813/daily_reviews/daily_review_2026-05-07.json:208-254): test_pollution_cleanup 归档与 data/daily_reviews 现存同哈希重复 ⚠️OFF-MANIFEST
  - 证据引用: `L208: - 501375B ×2: data/archive/test_pollution_cleanup_20260813/daily_reviews/daily_review_2026-05-07.json, data/daily_reviews/daily_review_2026-05-07.json
L254: - 21414B ×2: data/archive/test_pollution_cleanup_20260813/daily_reviews/daily_review_2026-08-03.json, data/daily_reviews/daily_review_202`
  - 影响声称: 多组归档文件与 data/daily_reviews 同名文件同 sha256，说明 2026-08-13 的污染清理归档只是当前活跃目录的镜像副本。活跃消费方通常读 data/daily_reviews；归档副本若被误读，会造成同一日报两个来源并产生时间戳混淆。影响半径：daily_review、review_orchestrator 及面板日报页；需排除有脚本把 archive 路径作为历史数
  - 建议: 归档到仓外：将 data/archive/test_pollution_cleanup_20260813/daily_reviews/ 下与 data/daily_reviews 同哈希的文件移出仓库或对象存储，然后删除仓内副本。验证：grep -RInF 'test_pollution_cleanup_20260813/daily_reviews' . 确认无生产引用；运行 daily_revi

<!-- DEB-VERDICT cluster=226 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: md5sum验证两文件均501375B且MD5=4d0cbafc6d8084925be19de47b2fabb6,与声称L208吻合。归档目录为2026-08-13测试污染快照,与data/daily_reviews重复。

### 议题 #229 [独有 1/2] `__OFF_MANIFEST__:data/sec_cache/form4_1769628_40d.json`:211-240 — SEC Form4 缓存 30d/40d 键同内容，存在窗口语义冲突

环节: t3_code_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, data/sec_cache/form4_1769628_40d.json:211-240): SEC Form4 缓存 30d/40d 键同内容，存在窗口语义冲突 ⚠️OFF-MANIFEST
  - 证据引用: `L211: - 110780B ×2: data/sec_cache/form4_1769628_40d.json, data/sec_cache/form4_1769628_30d.json
L239: - 59200B ×2: data/sec_cache/form4_1596532_40d.json, data/sec_cache/form4_1596532_30d.json
L240: - 52630B ×2: data/sec_cache/form4_1137789_30d.json, data/sec_cache/form4_1137789_40d.json`
  - 影响声称: 缓存键包含 30d/40d 两种窗口语义，但同 sha256 表明两个窗口返回或落盘内容完全相同；若下游按窗口做分析，会静默得到同一数据，属于潜在冲突。影响半径：SEC 缓存读取路径与使用 form4 的分析器；删除缓存可能触发重拉，需要确认外部限流与缓存重建逻辑。
  - 建议: 合并去重：将这些同哈希缓存键移出仓外或删除，让 SEC 缓存按当前逻辑重新生成；若重拉后仍相同，则在缓存元数据或文件头标注‘30d/40d 同源’。验证：grep -RInF 'form4_' data/sec_cache 与 grep -RInF 'form4' app scripts 找到读取方；触发或模拟一次 SEC 缓存刷新；运行相关分析测试并确认 30d/40d 请求不报错。

<!-- DEB-VERDICT cluster=229 -->
裁决: REJECTED | 置信=high | 理由: 30d/40d 缓存文件均由活跃代码路径生成：data_collector.py:501 调用 fetch_company_form4s(max_age_days=30) 产生 _30d.json，sec_form4_provider.py:397 调用 fetch_company_form4s(max_age_days=40) 产生 _40d.json。两者服务于不同语义（直接查询 vs 集群检测窗口），CIK 1769628 内容相同仅因 30-40 天区间无交易，属数据巧合非设计冗余。

### 议题 #236 [独有 1/2] `__OFF_MANIFEST__:docs/adr/ADR-007-positionstore-consolidation.md`:23-23 — ADR-007 编号冲突：Dashboard 与 PositionStore 两个不同决策共用 ADR-007

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S4
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S4, cleanup_redundancy, docs/adr/ADR-007-positionstore-consolidation.md:23-23): ADR-007 编号冲突：Dashboard 与 PositionStore 两个不同决策共用 ADR-007 ⚠️OFF-MANIFEST
  - 证据引用: `L11: | docs/ADR-007-dashboard-architecture-upgrade.md | 11 | 81 | # ADR-007: Dashboard 架构加固 — 推流·缓存·错误统一 || **状态**: 设计中 |
L23: | docs/adr/ADR-007-positionstore-consolidation.md | 1 | 80 | # ADR-007: PositionStore 写入路径收拢 || **状态**: 已规划 (待实施) |`
  - 影响声称: 两个不同决策使用同一 ADR 编号，后续按 ADR-007 引用、复盘或实施跟踪时可能指向错误文档，属于治理冲突。影响半径主要是文档索引、ADR 引用和人工评审；未见运行时代码直接依赖，但需检索所有 ADR-007 文本引用。
  - 建议: 处置：合并去重（不是删正文，而是去重编号）：保留两份内容，但将其中一份重编号到下一个可用 ADR 号，建议优先重编号 docs/adr/ADR-007-positionstore-consolidation.md；同步更新 docs/debates/index_generated.md 和全部引用。验证：grep -R 'ADR-007' docs scripts app config ops 后

<!-- DEB-VERDICT cluster=236 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: docs/ 下存在两份 ADR-007：docs/ADR-007-dashboard-architecture-upgrade.md（2026-06-15，Dashboard 架构加固，状态设计中）与 docs/adr/ADR-007-positionstore-consolidation.md（2026-06-16，PositionStore 写入路径收拢，状态已规划）。两不同决策共用编号造成引用歧义，违反 ADR 编号唯一性原则，需重新编号消除冲突。

### 议题 #237 [独有 1/2] `__OFF_MANIFEST__:docs/archive/handoff-archive-pre-2026-08-24.md`:57-57 — 349KB 的 handoff 历史档案与当前 handoff.md 并存，适合移出运行仓

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S2
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S2, cleanup_redundancy, docs/archive/handoff-archive-pre-2026-08-24.md:57-57): 349KB 的 handoff 历史档案与当前 handoff.md 并存，适合移出运行仓 ⚠️OFF-MANIFEST
  - 证据引用: `L57: | docs/archive/handoff-archive-pre-2026-08-24.md | 349 | 2 | # FTSeek-US · HANDOFF 档案 (2026-08-23 及更早) |
L988: - handoff.md · 178KB · 2026-09-03`
  - 影响声称: 该文件自我定位为 2026-08-23 及更早的 handoff 档案，当前活跃交接文件是根目录 handoff.md。大体积历史档案留在仓内会增加检索噪声，并可能让人误读旧状态为当前状态。影响半径主要是文档链接和人工交接流程。
  - 建议: 处置：归档到仓外。先 grep -R 'handoff-archive-pre-2026-08-24' docs AGENT.md handoff.md ops；若有链接，改为指向外部存档或注明已归档，然后移出运行仓。验证：handoff.md 和 AGENT.md 中交接路径仍完整；文档索引重生成无断链；仓体积下降且当前 handoff 唯一。

<!-- DEB-VERDICT cluster=237 -->
裁决: REJECTED | 置信=high | 理由: 档案文件确属有意切档归档，非冗余。handoff.md L131/L373/L490 三处明确引用该档案路径 (docs/archive/handoff-archive-pre-2026-08-24.md) 作为旧条目去向指针；文件已在 docs/archive/ 归档目录，位置合理。349KB 体积对仓影响微小。移除会破坏 handoff.md 的文档链完整性。模型声称的'冗余并存'是对切档机制的误读。

### 议题 #238 [独有 1/2] `__OFF_MANIFEST__:docs/debates/qoder_comment_cleanup_unified_config.patch.deprecated-20260816`:197-197 — 已明确废弃且禁止 apply 的 patch 文件仍留在辩论目录

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S4
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S4, cleanup_redundancy, docs/debates/qoder_comment_cleanup_unified_config.patch.deprecated-20260816:197-197): 已明确废弃且禁止 apply 的 patch 文件仍留在辩论目录 ⚠️OFF-MANIFEST
  - 证据引用: `L197: | docs/debates/qoder_comment_cleanup_unified_config.patch.deprecated-20260816 | 7 | 19 | ⛔ DEPRECATED 2026-08-16 (Hermes 变更复核, 用户裁废弃) — 禁止 apply。 |`
  - 影响声称: 文件自身已声明 DEPRECATED 且禁止 apply，继续留在仓内只剩下误用价值；若后续清理或自动化补丁流程误扫描到它，可能产生不必要的复核成本。影响半径主要是文档与人工流程；不应影响运行时。
  - 建议: 处置：删除。若必须留痕，依赖 Git 历史即可。验证：git grep 'qoder_comment_cleanup_unified_config' 无残留引用；重生成 docs/debates/index_generated.md；确认辩论索引和补丁流程不再列到该文件。

<!-- DEB-VERDICT cluster=238 -->
裁决: REJECTED | 置信=high | 理由: 文件头部明确声明'保留仅作历史证据链'，属治理性保留而非遗忘。ADR-027-daily-position-cap-30-to-5.md:L63 主动引用该 patch 作为决策证据链；DEB-20260815-stable-profit-feasibility.md:L1073 亦引用。删除会破坏 ADR 引用完整性。虽标注 DEPRECATED 且禁止 apply，但保留目的正当，非冗余。

### 议题 #239 [独有 1/2] `__OFF_MANIFEST__:docs/decisions/RFC-056-orb-auxiliary-pipeline.md`:202-202 — RFC-056 编号冲突：HIGH_VOL 过滤与 ORB 辅线两个不同提案共用 RFC-056

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S4
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S4, cleanup_redundancy, docs/decisions/RFC-056-orb-auxiliary-pipeline.md:202-202): RFC-056 编号冲突：HIGH_VOL 过滤与 ORB 辅线两个不同提案共用 RFC-056 ⚠️OFF-MANIFEST
  - 证据引用: `L201: | docs/decisions/RFC-056-high-vol-regime-filter.md | 2 | 95 | # RFC-056: HIGH_VOL 市况过滤阈值收紧 || > **状态**: 草稿 |
L202: | docs/decisions/RFC-056-orb-auxiliary-pipeline.md | 10 | 95 | # RFC-056: 独立 ORB 辅线 (Opening Range Breakout Auxiliary Pipeline) |`
  - 影响声称: 两个不同 RFC 共用 RFC-056 编号，会让决策引用、实施状态跟踪和文档检索产生歧义；若后续有人按编号落地或废弃，可能误伤另一个提案。影响半径为 docs/decisions 及引用 RFC-056 的文档；未见必然运行时依赖。
  - 建议: 处置：合并去重（去重编号）：保留两份文档，但将 RFC-056-orb-auxiliary-pipeline.md 重编号到下一个可用 RFC 号，并更新所有引用与索引。验证：grep -R 'RFC-056' docs scripts app config ops 后每个命中都能唯一识别；文档索引重新生成后无重复 RFC 号。

<!-- DEB-VERDICT cluster=239 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: 真实仓确认两个不同提案共用 RFC-056 编号：docs/decisions/RFC-056-high-vol-regime-filter.md (HIGH_VOL 过滤, 草稿) 与 docs/decisions/RFC-056-orb-auxiliary-pipeline.md (ORB 辅线)。生产代码对 RFC-056 有大量引用：app/risk/pre_trade_check.py:L10/L65/L264 指向 HIGH_VOL 过滤；app/core/scheduler_core.py:L685/L1879 及 app/orb/* 指向 ORB 辅线。RFC-059:L143 也引用 'RFC-056 (HIGH_VOL 过滤)'。编号歧义使代码注释和决策追踪存在误指风险，若废弃其中一个可能误伤另一个。属显著维护面问题。

### 议题 #240 [独有 1/2] `__OFF_MANIFEST__:scripts/__pycache__`:262-426 — scripts 下大量 .pyc 编译缓存文件无仓内引用且可再生

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S1
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S1, cleanup_redundancy, scripts/__pycache__:262-426): scripts 下大量 .pyc 编译缓存文件无仓内引用且可再生 ⚠️OFF-MANIFEST
  - 证据引用: `L262: | scripts/__pycache__/_tmp_adf_check.cpython-311.pyc | 14 | 7 | 0 |
L426: | scripts/__pycache__/swing_hold_replay.cpython-311.pyc | 17 | 20 | 0 |`
  - 影响声称: __pycache__ 是 Python 编译产物，清单中引用数均为 0；保留它们只增加仓体积和 diff 噪声，没有运行价值。影响半径为零，但应确保 .gitignore 覆盖，避免再次入库。
  - 建议: 处置：删除。清理 scripts/__pycache__、config/__pycache__、scripts/utils/__pycache__ 等全部编译缓存目录。验证：pytest -q 通过；python -m compileall 或正常入口运行可重新生成缓存；git status 不再跟踪 __pycache__。

<!-- DEB-VERDICT cluster=240 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: scripts/__pycache__/ 目录实际存在 195 个 .pyc 编译缓存文件 (如 admission_replay.cpython-311.pyc 等)。.gitignore L2/L3/L37/L38 已配置 __pycache__/ 和 *.pyc 排除规则，但这些文件仍存在于仓内，属应排除而未排除的遗留。.pyc 完全可由 Python 运行时自动再生，无任何仓内引用价值。属小清理项，不产生运行时风险。

### 议题 #241 [独有 1/2] `__OFF_MANIFEST__:scripts/install_heartbeat_cron.sh`:728-728 — 0KB 且引用数为 0 的 install_heartbeat_cron.sh 是空壳安装脚本

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, scripts/install_heartbeat_cron.sh:728-728): 0KB 且引用数为 0 的 install_heartbeat_cron.sh 是空壳安装脚本 ⚠️OFF-MANIFEST
  - 证据引用: `L728: | scripts/install_heartbeat_cron.sh | 0 | 11 | 0 |`
  - 影响声称: 脚本大小为 0KB，无法安装任何 cron；文件名精确引用为 0，说明仓内未见文档或脚本提及。它仍可能被人工记忆或外部笔记调用，因此需检查真实 crontab 与 ops 文档，但材料本身显示它是静默无效文件。
  - 建议: 处置：删除。验证：crontab -l 和 docs/references/crontab-loop-baseline.txt 中不依赖该脚本；grep -R 'install_heartbeat_cron' docs ops scripts .github 无命中；删除后现有 heartbeat/健康检查入口 scripts/health_check.sh 或相关 cron 仍正常。

<!-- DEB-VERDICT cluster=241 -->
裁决: REJECTED | 置信=high | 理由: 事实错误：模型声称文件 0KB，实际 scripts/install_heartbeat_cron.sh 为 759 字节，包含完整的 crontab 幂等安装逻辑 (备份→检测→追加 business_heartbeat.sh 定时任务→确认输出)。脚本内容功能完整，注释标明 '2026-08-24 用户已批准'。虽 grep 未发现其他文件引用该安装脚本，但这是安装型脚本的正常特征 (一次性执行后无需被引用)。非空壳，非冗余。

### 议题 #242 [独有 1/2] `__OFF_MANIFEST__:scripts/settle_shadow_portfolio.py`:797-799 — 三个 settle shadow 脚本名称相近，疑似影子结算逻辑重复实现

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S2
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S2, cleanup_redundancy, scripts/settle_shadow_portfolio.py:797-799): 三个 settle shadow 脚本名称相近，疑似影子结算逻辑重复实现 ⚠️OFF-MANIFEST
  - 证据引用: `L797: | scripts/settle_overnight_shadow.py | 7 | 25 | 6 |
L798: | scripts/settle_shadow_portfolio.py | 8 | 1 | 10 |
L799: | scripts/settle_winner_overnight_shadow.py | 6 | 2 | 5 |`
  - 影响声称: 三个脚本都围绕 settle/shadow/overnight，存在功能边界重叠或重复实现的可能。它们引用数均大于 0，说明可能有报告、cron 或 shadow_board 消费，不能因重名直接删；需先映射调用方，确认是否同一语义被拆成多套脚本。
  - 建议: 处置：合并去重（先审计后合并）。列出每个脚本的调用方、输出表和 cron/shadow_board 消费点；若语义重叠，合并为一个结算影子脚本并用子命令区分 overnight/winner/portfolio；若语义确实不同，在文件头标注边界。验证：合并后原调用方全部可运行；scripts/shadow_board.py 和相关日报脚本输出不缺失；crontab -l 中涉及脚本全部成功。

<!-- DEB-VERDICT cluster=242 -->
裁决: REJECTED | 置信=high | 理由: 三个脚本职责各异，非重复实现：settle_overnight_shadow.py 结算 overnight_hold_shadow.jsonl 的'持仓过夜 vs 已退出'反事实 (scheduler_core.py:L2446 调度)；settle_shadow_portfolio.py 结算 shadow_portfolio_entries.jsonl 中被门拦指令的盘后表现 (ops/hermes/shadow_settle_daily.sh:L17 调度, tests/test_shadow_portfolio.py 测试)；settle_winner_overnight_shadow.py 结算 winner_overnight_shadow.jsonl 的'带SL持一夜'反事实 (shadow_registry.py:L503 引用)。三者数据源、退出栈逻辑、业务场景均不同，名称相近是命名惯例而非冗余。

### 议题 #233 [独有 1/2] `__OFF_MANIFEST__:config/backups`:872-889 — config 下多份 unified_config.yaml 备份文件会误导当前配置基线

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S2
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S2, cleanup_redundancy, config/backups:872-889): config 下多份 unified_config.yaml 备份文件会误导当前配置基线 ⚠️OFF-MANIFEST
  - 证据引用: `L872: - config/backups/unified_config.yaml.bak_20260725_topup_quota · 56132B · 2026-07-25
L873: - config/backups/unified_config.yaml.bak_reanchor_off_20260729_133913 · 56543B · 2026-07-29
L885: - config/unified_config.yaml.bak.20260713_165252 · 51250B · 2026-07-13
L886: - config/unified_config.yaml.`
  - 影响声称: 这些备份与当前 unified_config.yaml 并存，最大风险不是运行时自动读取，而是人工排障时误把旧快照当作当前基线，或误恢复旧开关。影响半径包括 config 目录人工 diff、回滚操作和配置审计；需要先确认 config_loader 不会 glob 到备份文件。
  - 建议: 处置：归档到仓外。将全部 unified_config.yaml.bak* 与 config/backups 下旧配置快照移出运行仓，只保留 Git 历史或外部备份。验证：python -c 加载 config/config_loader.py 或现有配置冒烟测试仍通过；grep -R 'unified_config.yaml.bak' config app scripts ops 无运行依赖；仓

<!-- DEB-VERDICT cluster=233 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: config/backups/ 目录含 2 个 unified_config.yaml 备份（bak_20260725_topup_quota、bak_reanchor_off_20260729_133913），grep 全仓无任何代码或脚本引用此目录或文件。属历史配置快照，无运行时价值，可清理。

### 议题 #234 [独有 1/2] `__OFF_MANIFEST__:config/deprecated/watchlist_us.json`:876-876 — deprecated 目录下的旧 watchlist 与 static_watchlist_enabled=false 形成静默死数据嫌疑

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, config/deprecated/watchlist_us.json:876-876): deprecated 目录下的旧 watchlist 与 static_watchlist_enabled=false 形成静默死数据嫌疑 ⚠️OFF-MANIFEST
  - 证据引用: `L876: - config/deprecated/watchlist_us.json · 13758B · 2026-04-19
L953:   L2296: static_watchlist_enabled: false`
  - 影响声称: 文件位于 deprecated 目录且时间很早，而当前配置中 static_watchlist_enabled 显式为 false；若没有代码再读取它，就是静默失效的历史资产。影响半径需要人工确认：可能存在硬编码路径、动态加载或人工脚本仍引用 watchlist_us.json；引用计数材料未给出 config 文件引用数，不能仅凭 0 判定。
  - 建议: 处置：删除（前置人工确认）。先 grep -R 'watchlist_us.json' app scripts config ops docs，并检查 static_watchlist 相关加载路径；确认无读取后删除。验证：启动冒烟脚本 scripts/startup_smoke_check.py 通过；盘前检查 scripts/premarket_data_check.py 或相关测试通过；配置

<!-- DEB-VERDICT cluster=234 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: config/deprecated/watchlist_us.json 存在（13758B，2026-04-19），生产代码（config_us.py:198、watchlist_manager.py:26、paths.py:177）均指向 config/watchlist_us.json（不存在）或 unified_config.yaml:2297 的 watchlist 节。static_watchlist_enabled=false（unified_config.yaml:2296）确认静态 watchlist 已停用。系统已完全迁移至 unified_config.yaml，deprecated 文件为死数据。

### 议题 #235 [独有 1/2] `__OFF_MANIFEST__:config/unified_config.yaml`:898-926 — legacy_battle_plan_enabled 对应的 legacy track 已被 ADR-012 废弃，配置开关疑似死键

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, config/unified_config.yaml:921-926): entry_reanchor_enabled 在同一配置开关清单中出现两次，存在重复开关或语义分裂风险 ⚠️OFF-MANIFEST
  - 证据引用: `L921:   L1064: entry_reanchor_enabled: false
L926:   L1313: entry_reanchor_enabled: false`
  - 影响声称: 同一开关名在材料中两次出现，可能位于不同 YAML 节，也可能是重复配置。若它们表达同一语义，维护者可能只改其中一处，导致配置读取路径和人工理解不一致；若是不同语义，同名键也会增加误读风险。影响半径为 config/unified_config.yaml、config_loader、以及所有读取 entry_reanchor 的代码；需人工确认 YAML 路径。
  - 建议: 处置：合并去重。用 YAML 路径定位两处 entry_reanchor_enabled 的节；若语义相同，保留唯一路径并删除另一处；若语义不同，重命名其中一个以消除同名歧义。验证：python 解析 config/unified_config.yaml 并打印所有 entry_reanchor 路径；app/config_mgmt/validator.py 通过；grep -R 'entry_r
- **qwen3.8-max-0902** (S3, cleanup_redundancy, config/unified_config.yaml:923-923): mfe_stall_exit_enabled 已被 ADR-026 撤销，但配置仍保留关闭开关 ⚠️OFF-MANIFEST
  - 证据引用: `L40: | docs/adr/ADR-026-symbol-cap-and-mfe-stall-rollback.md | 1 | 20 | # ADR-026: 单名帽收紧 0.25→0.08 + 撤销已否决 mfe_stall_exit (DEB-20260815 P0) || **日期**: 2026-08-15 | **状态**: Accepted | **提交分支**: rfc-060-phase1-hotfi |
L923:   L1144: mfe_stall_exit_enabled: false    # DEB-20260815 P0-B (2026-08-15): §1`
  - 影响声称: ADR 已明确撤销 mfe_stall_exit，但配置中仍保留 disabled 开关。这类已否决功能的开关即使为 false，也会让后续维护者误以为功能仍可开启，或在清理退出逻辑时遗漏死分支。影响半径为退出相关代码与配置校验；需先确认代码中是否仍读取该键。
  - 建议: 处置：删除。先 grep -R 'mfe_stall_exit' app scripts config tests；若代码只剩开关判断且 ADR 已撤销，则同步删除配置键和死代码分支。验证：pytest -q 相关退出测试；scripts/startup_smoke_check.py 通过；config 校验无缺失键；grep 后仅保留 ADR/DEB 文档历史记录。
- **qwen3.8-max-0902** (S3, cleanup_redundancy, config/unified_config.yaml:898-898): legacy_battle_plan_enabled 对应的 legacy track 已被 ADR-012 废弃，配置开关疑似死键 ⚠️OFF-MANIFEST
  - 证据引用: `L27: | docs/adr/ADR-012-deprecate-legacy-battle-plan-track.md | 3 | 51 | --- || **状态**: 已实施 |
L898:   L321: legacy_battle_plan_enabled: false`
  - 影响声称: ADR-012 文件名和状态表明 legacy battle plan track 已废弃且已实施，但 unified_config 仍保留 legacy_battle_plan_enabled=false。若代码仍检查该键，会留下一个永远不会启用的死分支；若代码已不读取，则配置键是纯噪声。影响半径为 strategy/coordinator 或 playbook 相关旧路径；需 grep 确认。
  - 建议: 处置：删除。先 grep -R 'legacy_battle_plan' app scripts config tests docs；确认无生产路径依赖后，从 unified_config.yaml 删除该键，并删除或标注仅服务该键的代码分支。验证：app/config_mgmt/validator.py 通过；pytest -q 相关策略协调测试；scripts/startup_smoke_ch

<!-- DEB-VERDICT cluster=235 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: 三子项中仅 entry_reanchor_enabled 重复成立：unified_config.yaml:1064（execution_parameters 节）与 :1313（risk_parameters 节）同键同值 false，pipeline.py:5538 读取时存在语义分裂风险。mfe_stall_exit_enabled（:1144）虽被 ADR-026 撤销但仍被 shadow_registry.py:453、playbook/config.py:153、pipeline.py:7645 活跃读取，false 值为回滚基线非死键。legacy_battle_plan_enabled（:321）仍被 llm_pipeline.py:1130、scheduler_core.py:1426 读取，ADR-012 实施后为可回滚开关非死键。仅第一项确认为配置重复。

### 议题 #231 [独有 1/2] `__OFF_MANIFEST__:.DS_Store`:976-976 — .DS_Store 是操作系统噪声文件

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S1
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S1, cleanup_redundancy, .DS_Store:976-976): .DS_Store 是操作系统噪声文件 ⚠️OFF-MANIFEST
  - 证据引用: `L976: - .DS_Store · 8KB · 2026-05-19`
  - 影响声称: .DS_Store 无项目语义，只会在不同开发者机器上反复生成，可能污染 diff。影响半径为零，但应确保 .gitignore 已覆盖。
  - 建议: 处置：删除。验证：git status 不再出现 .DS_Store；.gitignore 中包含 .DS_Store；本地重新浏览目录后不提交该文件。

<!-- DEB-VERDICT cluster=231 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: .DS_Store 文件存在（8KB，2026-05-19），系 macOS Finder 自动生成的元数据文件，无任何业务价值，不应纳入版本控制。属标准清理项。

### 议题 #232 [独有 1/2] `__OFF_MANIFEST__:.env.us.bak.before-key-unify`:981-981 — 根目录保留旧 .env 备份，存在过期凭据与配置基线混淆风险

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S3
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S3, cleanup_redundancy, .env.us.bak.before-key-unify:981-981): 根目录保留旧 .env 备份，存在过期凭据与配置基线混淆风险 ⚠️OFF-MANIFEST
  - 证据引用: `L978: - .env.backup.20260531_094640 · 0KB · 2026-06-16
L981: - .env.us.bak.before-key-unify · 4KB · 2026-07-02`
  - 影响声称: 旧 env 备份可能被误当作当前环境配置，且可能包含过期或敏感凭据。运行时通常只读 .env/.env.us，但人工恢复、脚本 source 或排障时可能误用备份。影响半径为本地环境加载和密钥管理流程。
  - 建议: 处置：删除；如合规需要留档，先移到仓外加密备份再删除。验证：应用仍以 .env.us 或现行环境文件启动；grep -R '.env.backup' 与 '.env.us.bak' scripts ops docs 无引用；确认密钥已轮换或不再敏感后清理。

<!-- DEB-VERDICT cluster=232 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: .env.us.bak.before-key-unify 文件存在（4945B，2026-07-02），grep 全仓无生产代码引用。mode_switch_service.py:138 仅创建/管理 .env.us.bak.{timestamp} 格式的新备份，不读取此特定文件。此为键统一前的配置快照，含过期凭据风险，可归档后删除。

### 议题 #243 [独有 1/2] `__OFF_MANIFEST__:start_dashboard.sh`:995-995 — 0KB 的 start_dashboard.sh 是无效启动入口

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S2
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S2, cleanup_redundancy, start_dashboard.sh:995-995): 0KB 的 start_dashboard.sh 是无效启动入口 ⚠️OFF-MANIFEST
  - 证据引用: `L995: - start_dashboard.sh · 0KB · 2026-06-10`
  - 影响声称: 启动脚本为空，说明当前不可能通过它启动 dashboard；若 Dashboard 仍被 ADR 或人工流程提及，这个空文件会造成入口假象。影响半径包括 dashboard 相关文档、人工启动习惯和可能的 systemd/cron 挂载；材料未给出外部引用，需人工确认。
  - 建议: 处置：删除。验证：grep -R 'start_dashboard' docs ops scripts .github config 无命中；crontab -l 和 systemctl 列表不指向该脚本；若 Dashboard 仍是活跃方向，应另建真实入口而不是保留空文件。

<!-- DEB-VERDICT cluster=243 -->
裁决: REJECTED | 置信=high | 理由: 事实错误：模型声称文件 0KB，实际 start_dashboard.sh 为 145 字节，包含有效的 bash 启动脚本 (cd→source venv→exec uvicorn app.web_dashboard.app:create_app --port 8901)。app/web_dashboard/ 模块实际存在 (含 app.py/routes/templates 等完整子模块)，该脚本是其有效启动入口。虽 ops/systemd 未发现引用，但作为手动启动入口仍有价值。非空文件，非无效。

### 议题 #244 [独有 1/2] `__OFF_MANIFEST__:test_direction_config.py`:996-996 — 根目录 0KB 测试文件 test_direction_config.py 无测试内容

环节: t3_project_subtraction · 类别: cleanup_redundancy · 自报最高 severity: S2
- ⚠️ 本簇含 off_manifest/越界引用 (幻觉证据候选, 核实其是否根本不存在)
- **qwen3.8-max-0902** (S2, cleanup_redundancy, test_direction_config.py:996-996): 根目录 0KB 测试文件 test_direction_config.py 无测试内容 ⚠️OFF-MANIFEST
  - 证据引用: `L996: - test_direction_config.py · 0KB · 2026-06-20`
  - 影响声称: 0KB 的 test_*.py 会被 pytest 发现但不提供任何断言，只增加收集噪声，并可能让人误以为 direction config 有测试保护。影响半径很小，但需确认没有其他工具把它当作占位契约。
  - 建议: 处置：删除。验证：pytest --collect-only 不再收集该文件；pytest -q 现有测试套件通过；grep -R 'test_direction_config' .github ops scripts docs 无残留引用。

<!-- DEB-VERDICT cluster=244 -->
裁决: REJECTED | 置信=high | 理由: 事实错误：模型声称文件 0KB，实际 test_direction_config.py 为 1017 字节，包含完整的方向差异化配置验证代码 (调用 _load_atr_tiers_from_config 验证 LONG/SHORT/Default 三组 ATR tiers)。文件有实质测试逻辑，非空文件。虽然 DEB-20260823-system-health-check.md:L58 曾提议将其移至 scripts/ 目录 (因 pytest.ini testpaths=tests 不收集根目录)，但文件本身有内容，'0KB 无测试内容'的声称与事实不符。

---
处置记录 (2026-09-04): #239 已处置 — docs/decisions/RFC-056-orb-auxiliary-pipeline.md 重编号为 RFC-075-orb-auxiliary-pipeline.md，引用已同步 (app/core/scheduler_core.py ×4, app/orb/* ×8)；RFC-056 现唯一归属 HIGH_VOL 过滤。#236 已处置 — docs/adr/ADR-007-positionstore-consolidation.md 重编号为 docs/adr/ADR-037-positionstore-consolidation.md；ADR-007 现唯一归属 docs/ADR-007-dashboard-architecture-upgrade.md。

---
处置记录 (2026-09-04 第二批, Qoder 会话: 补充分析 kimi-k3 未完成环节 + 全量 CONFIRMED 修复):

**标记编号约定**: 会话内扫描新增、文档中无对应议题号的发现使用 #S1-#S12 系列标记，避免与官方议题号冲突 (会话早期误用 #203-#217 与官方议题撞号，已全量重编号)。#S1=order_executor LONG 门 str-Enum 归一, #S2=signal_arbitrator direction 归一, #S3=_resolve_trd_env 兜底链 (order_executor+position_monitor), #S4=空 futu_id 平仓 fail-closed, #S5=earnings_revision 负值钳制, #S6=void 终态集合 (pipeline+shadow_tracker), #S7=周报幂等 (playbook db marker), #S8=dedup_v2 过期僵尸记录, #S9=SEC ticker 词边界, #S10=execution claim 释放, #S11=data_service 幽灵持仓回退, #S12=dashboard trade 路由 broker 调用 to_thread。

**S3 批 (本机直改)**: #133 analyze_signals 空数据除零, #134 config validator 非数值跳过, #135 timezone 节假日回走, #137 config_loader 嵌套 list 环境变量, #157 sss threshold 负值 abs, #158 trade_quality_gate 权重部分覆盖合并, #161 agent_bridge fallback 非 dict 归一 (2 处), #163 rsshub 缺 pubDate 丢弃, #166 auto_trade 计数 ET 重置, #167 symbol_lock reconcile 重分配, #168 rsi_rebound None 守卫, #169 ORB r_multiple 量纲, #170 ORB range 重建条件, #171 ORB rolling range 失效旧值, #173 ORB scheduler 注入 (冷却门禁复活), #177 TFA vote None 守卫, #194 VIXY/ATR K线窗 ET 日期 (2 处), #196 data_service fx 除零。

**S3 批 (三个后台 Agent, 各自全量回归 2496/37/0)**: 复盘簇 #208/#209/#210/#213/#214/#215/#216/#217/#219; 数据层 #143/#144/#145/#146/#147; 杂项 #140/#154/#162/#198/#204。

**S2/S1 批 (本机直改)**: #138 config_us 股池 YAML 单真源 (JSON 再现则告警忽略; 生产磁盘无该文件, 行为不变), #139 ftseek_us.sh restart 等待旧进程退出 (30s 上限, 防双实例), #141 main.py --test FUTU_OPEND_PORT 安全解析, #148 daily_log_store tick 丢失 debug→warning, #149 scheduler 延迟审计队列失败条目保留重试, #152 manual_unblock 持 _close_retry_lock, #153 执行元数据 signal/fill_time ET aware, #155 反手平仓两处检查 _wait_or_cancel_open_orders 的 _safe 返回值 (不安全不据持仓快照判已平), #156 Obsidian 平仓记录 US. 前缀去重, #159 agent_bridge 毒响应 .poison 隔离, #160 _update_memory 全字段类型校验, #164 wallstreetcn 全渠道失败抛出让 HealthMonitor 记录, #165 _seen_ids 插入序 dict 保留最近一半, #174 ORB 单例晚绑定推广到全部可注入依赖, #195 已确认无需修 (DEB-20260903 #110 的 should_send/mark_sent 已覆盖), #197 data_service 锁清理改使用者计数 (替代 lock.locked() 误判), #203 审计 DB 今日订单窗口真实 ET→北京时区换算 (DST 感知, 替代硬编码 ±12h), #205 飞书 SDK/凭据缺失时降级 Webhook-only 服务, #206 盘后报告单笔/最佳交易盈亏符号, #207 盘前报告俄文残留改中文, #211 pnl_outliers+signal_arbitration 三处 localtime(CST) 口径改 DST 感知 ET 日界 epoch 区间, #212 Obsidian 明细 pnl_net NULL 回退, #218 drawdown_pct NULL 防 TypeError, #220 ReviewOrchestrator 单例晚绑定 notifier/agent_bridge, #219 补充 _validate_data_integrity 按复盘日 CST 覆盖窗 {D,D+1} 匹配日志 (原按 now_et 当日, 历史复盘扫错天)。

**扫描补充修复**: web_dashboard/order/executor.py TP 防重挂 — 部分成交跟踪/TP 重试路径每 monitor 周期重入 place_protective_stop, 原仅有 _sl_attached 守卫 (#121) 而 TP 无对称守卫, 每轮再挂一张 GTC TP 卖单, 累计卖出量可超持仓→意外做空; 已加 _tp_attached/_tp_order_id 对称守卫。

**验证**: 全量测试 `venv/bin/pytest tests/ -q` 2496 passed / 37 skipped / 0 failed (110s); KR2 退出参数守卫 tests/test_w1_exit_version_guard.py + test_kr2_freeze_exemption.py 10 passed → exit-de49b1299290 不变; 全部 16 个改动文件 py_compile + ftseek_us.sh bash -n 通过; 标记清单打点无撞号。

**行为变更披露 (需用户知悉)**: #S1 修复使 order_executor 的 LONG 高门槛 (strength≥50 AND confidence≥0.6) 从"因 str(Enum) 比较失效而从未生效"恢复生效 — LONG 入场实际变严, 属文档原意的恢复但仍是入场行为变化。#138 同理恢复了 YAML 单真源 (生产本无 JSON 文件, 零实际变化)。

**待用户裁决 (未动)**: T2 #221 (SL_INITIAL 2.5%→1.5%) 与 #222 (risk_on 降仓/提门槛) = 交易参数, 冻结纪律不代裁; T3 #223-#234/#240-#244 删除类处置 (备份/归档/隔离区/空壳脚本), 破坏性操作待确认; #235 legacy_battle_plan_enabled 死键 (config 删除) 待确认; 19 处 ADR-007 悬空引用 (重编号后残留的旧编号引用) 待清理确认。

**.py 改动需重启生效, 本批未重启服务、未提交 git。**

