# DEB-20261025 · 富途实盘保护 STOP 与券商容量影子

**状态**: 已执行（P0 最小补丁 + P1 只读观测）
**日期**: 2026-10-25
**触发**: 用户确认“P0-1 不过度设计、P0-2 不做大改，其他 P1 可做”。

## 1. 问题

富途 App 暂时不可用时，Web Dashboard 仍负责观察和处置实盘。但自动交易链有两处信息缺口：

1. 规则退出前会先撤掉券商保护 STOP，再提交平仓。若平仓在撤 STOP 后明确失败，
   仓位会短暂失去券商侧兜底；若无条件自动补挂，又可能在“平仓其实已提交/已部分成交”时
   重复卖出。
2. 下单前没有记录券商侧真实可交易量。富途 `acctradinginfo_query` 已实测可用，但
   直接做硬拦截会改变下单行为，证据不足。

## 2. 裁决

### P0-1 一次性保护回挂

- 不建常驻巡检、不做自动重挂循环、不自动处理部分成交。
- 规则退出仍先撤本策略的券商 STOP，再走现有 durable exit 链。
- 只有同时满足以下条件，才尝试回挂 **一次**：
  - LIVE 仓位、券商标的、`close_res` 明确失败（非 `*_pending` / 空错误）；
  - 券商刷新持仓仍是原方向、原完整数量；
  - 券商同标的没有平仓侧活动单（含 STOP/限价/市价、外部手动单）；
  - STOP 价格可从 `metadata.stop_price`、`metadata.broker_stop.stop_price`
    或 `metadata.stop_loss` 取得。
- 回挂前先持久化 `stop_rehang_attempted/count`，崩溃或重复轮次都不会再挂第二张。
- 回挂失败、持仓不一致、活动单无法确认、平仓状态未知/在途/可能已成交：
  **不重复挂卖单**，发 HIGH 告警并 `HALT_NEW_LIVE`。
- 回挂 guard 已持久化但无成功回执（进程崩溃窗口），或回挂后的 STOP 再次被规则退出撤掉
  且平仓再次失败：不静默重试，同样 HIGH 告警并 `HALT_NEW_LIVE`。
- STOP 自身部分成交：保持现有 `hold`，只发聚合告警，不自动平残余。

### P0-2 券商容量影子（第一阶段，`DualBroker.submit_order` 路径）

- 只在 REAL 且经 `DualBroker.submit_order` 的下单前调用 `acctradinginfo_query`，
  记录券商最大可交易量与请求数量；含显式 `is_exit` 的旧下单路径，但**不覆盖**
  durable `ExitLifecycle` 规则退出。
- **不拦截、不改下单路径、不改变返回值**。接口异常、字段缺失一律 fail-open 放行。
- `ExitLifecycle.send` 是平仓时效关键路径，额外两次券商 RPC 会直接推迟止损/规则退出；
  本期不为影子增加该延迟。因此本影子的样本口径是“经 `submit_order` 的真实下单”，
  不能宣称已覆盖全部 LIVE 平仓。
- 方向映射：
  - LONG 开仓 → `max_cash_buy`
  - LONG 平仓 → `max_position_sell`
  - SHORT 开仓 → `max_sell_short`
  - SHORT 回补 → `max_buy_back`
- 记录同标的活跃单与外部手动单数量，供后续评估“券商容量/并发订单”风险。
- 第二阶段的有限拒绝（`BROKER_CAPACITY_REJECT`）必须等影子样本达标后另行裁决。

### P1 只读项

- 账户风险字段从 `accinfo_query` 透传到标准化账户和 Dashboard。
- 新增 `get_rehab` 盘前公司行动只读检查脚本。
- 记录在线 effective SL 与券商 STOP 的漂移影子；不调用 `modify_order`。

## 3. 事实依据

- Futu SDK `10.09.6908`，本机 OpenD 可用。
- `acctradinginfo_query` REAL 实测字段：
  `max_cash_buy / max_cash_and_margin_buy / max_position_sell / max_sell_short /
  max_buy_back / long_required_im / short_required_im / session`。
- `accinfo_query` REAL 实测包含
  `risk_status / risk_level / exposure_level / exposure_limit / used_limit /
  remaining_limit / is_pdt / pdt_seq / beginning_dtbp / dt_status / remaining_dtbp /
  dt_call_amount`。
- `get_rehab` 属于 `OpenQuoteContext`，签名 `get_rehab(code)`，不是交易 context。

## 4. 边界与回滚

- 本批不建立完整 LIVE 订单状态机；触发升级条件仍是：LIVE 单笔放大、LIVE 多仓并行、
  LIVE 日名义额提高。
- 撤 STOP 后、平仓请求/回挂前仍有极窄进程崩溃窗口；本批不新增启动巡检或自动状态机，
  该窗口需人工对账。
- 本批不自动修改券商 STOP 价格；动态止损只观测。
- 回滚：
  - 容量影子：删除 `dual_broker.submit_order` 的 record-only 调用，或设
    `BROKER_CAPACITY_SHADOW_PATH` 指向临时文件仅用于隔离；不影响下单。
  - 动态止损影子：删除 `exit_monitor_flow` 的观测调用；不影响退出规则。
  - 保护回挂：移除三处 close-failure 处理回调；既有 STOP 释放与 durable exit 链不变。

## 5. 验证

- 定向契约测试覆盖：四方向容量映射、容量充足/不足/未知、查询异常 fail-open、
  活跃单识别、JSONL 写失败不影响下单。
- 保护回挂覆盖：明确失败且券商状态安全时只挂一次、状态未知不挂、活动单/持仓变化不挂、
  guard 持久化、部分成交只告警。
- 账户字段与 `get_rehab` 脚本有离线契约测试。

## 6. 后续

1. 容量影子攒到至少 30 笔 REAL 下单、10 个交易日，并单独审核不足样本。
2. 动态止损影子攒到至少 20 个独立 LIVE STOP，再决定是否需要 `modify_order`。
3. 只有用户二次批准后，才启用 `BROKER_CAPACITY_REJECT` 有限拦截。
