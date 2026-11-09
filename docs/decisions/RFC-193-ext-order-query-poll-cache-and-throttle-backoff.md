# RFC-193 EXT 订单查询轮询口径收敛 + 研究臂限频自退避

- 状态：**已落地（2026-11-09 21:2x CST）** · 用户裁「要」（答复「这个限流报警正常吗」后的第 1+2 步）
- 类型：**L1 缺陷修复** — 只动「订单状态读数」的取数口径（缓存 vs 刷新）+ 研究臂的告警分类，
  **不改任何入场/出场判据、阈值、下单参数、仓位与配额**；每笔持仓的成交集合在改动前后相同。
- 触发：2026-11-09 盘前两次 `❌ 延长时段开仓执行器异常 (研究臂)`，内容为
  `executor_exception:order_list_query failed: 查询未完成订单频率太高，请求失败，每30秒最多10次。`
  （`logs/ext_entry.log` L1231 / L1655，标的 US.TSM，研究臂候选，未成真仓）。

## 1. 根因（不是"并发太多"，是刷新被写进了轮询循环）

Futu 官方文档 *查询未完成订单*：

> 接口限制 — 同一账户ID(acc_id) 每 30 秒内最多请求 10 次查询未完成订单接口
> **调用此接口，只有在刷新缓存时，才受到限频限制**

`refresh_cache=False` 走 OpenD 本地缓存（服务端有更新会推送到 OpenD），**不占这 10/30s 额度**。
`refresh_cache=True` 才是"立刻向富途服务器重新请求"，才受限频。

旧实现把 `refresh_cache=True` **硬写死在轮询路径**：

| 位置 | 旧行为 | 折算 |
|---|---|---|
| `scripts/extended_hours_order_lifecycle.py::query_order` | 每次调用都 `refresh_cache=True` | `wait_order_fill` 3s 一轮 × 90s = **30 次刷新/单笔** ≈ 60 次/分钟 |
| 同上 `query_order_rows` | 每次调用都刷新 | 每个候选下一单前 1 次 + 撤单确认 + 终态复核 |
| `app/execution/exit_lifecycle.py`（EXT 退出链） | 证据闸 `refresh_cache=True` | 退出 cron 每分钟 1–2 次（**本批不动**，见 §5） |

一个账户的额度是 10 次/30 秒，而**单笔等成交就要 30 次刷新** ⇒ 等成交期间必然打爆，
entry/exit 的其它查询被连坐。这是"共享限流设计债"的具象：额度没变，是单笔请求量超了 3 倍。

## 2. 决定性证据（只读探针，2026-11-09 21:09 CST，SIM 19930823）

```
[A1] refresh=True  ret=0 rows=12 elapsed=0.077s
[A2] refresh=False ret=0 rows=12 elapsed=0.015s
[A3] 行集一致=True  n1=12 n2=12
[B] 缓存读 ok=12/12 elapsed=2.94s (刷新模式在 10 次/30s 处必报限频)
```

即：**缓存读与刷新读返回逐行一致的数据，且 2.9 秒内 12 次缓存读零报错**（>10 次，
若缓存读受限频则第 11 次必失败）。同一窗口下 12 次刷新读必然报
`每30秒最多10次`，这也解释了故障复现条件。

## 3. 改动

| # | 文件 | 内容 | 语义影响 |
|---|---|---|---|
| T1 | `scripts/extended_hours_order_lifecycle.py` | `query_order()` 默认 `refresh=False`（轮询走缓存）；`query_order_rows()` 保留"允许刷新"语义但过 **6s/账户** 节流 | 无（缓存由服务端推送同步，探针证行集一致） |
| T2 | 同上 | 刷新撞限频 → **自动退回缓存读**（`_query_with_throttle_fallback`），不把可观测性读取升级成异常；缓存读也失败才按原语义抛 `RuntimeError` | 失败路径少一次异常；不掩盖真故障（cache 也失败仍抛） |
| T3 | 同上 `wait_order_fill` | 轮询全部缓存读；刷新只留 3 个时点：①缓存连续两轮看不到该单（推送延迟兜底，≤1 次）②**撤单前**强制刷新一次确认真值 ③撤单后终态仍未确认时复核一次 | 每笔等成交刷新上限 **2–3 次**（旧 30 次）；②顺带消除"已成交却按缓存判未成交去撤单 → 报 cancel 失败"的旧缺陷 |
| T4 | 同上 | 每次等成交打一行口径留痕 `[ext_orders] wait_order_fill oid=.. polls=.. refreshes=.. elapsed=.. status=..` | 只加日志 |
| T5 | `scripts/extended_hours_entry_executor.py` | 新增 `_is_throttle_error()`；研究臂 `_execute` 异常里区分限频：限频 ⇒ `skip=throttled_order_query` + 研究漏斗 `reject` + **不打 `❌ 执行器异常` 推送** + **本轮剩余候选让位**（break，下一轮 cron 重试）；非限频 ⇒ 照旧推送 | 只影响研究臂（record-only 影子，无真仓）；严格臂告警逐字未动 |

## 4. 验证

| 项 | 读数 |
|---|---|
| 新增用例 | `tests/test_rfc193_order_query_poll_cache.py` **12 passed** |
| 变异验证（承重） | 变异1「节流=0 + 轮询回 refresh=True」⇒ **5 red**；变异2「去掉限频兜底」⇒ 1 red；变异3「研究臂恢复无条件告警」⇒ 1 red。修前 12 例全 ERROR（缺 `reset_refresh_state`），即断言钉的是改动后行为 |
| 回归面 | `test_extended_hours_order_execution` / `test_ext_entry_research_arm` / `test_extended_hours_entry_executor` / `test_orphan_ext_order_watch` / `test_cancel_orphan_ext_order` / `test_extended_hours_exit_cron` = **155 passed** |
| 活体（只读，真 OpenD） | `首次 rows=14 40.1ms | 6s 内二次 rows=14 14.1ms`，`行集一致=True`，`stats={'allowed':1,'suppressed':1}`，`query_order()` 走缓存读返回 `FILLED_ALL` |
| **生产实弹（cron 真单，本批最容易骗人的一环）** | 21:12 / 21:17 两笔研究臂真成交 `[ext_orders] wait_order_fill oid=8350496 polls=4 refreshes=0 elapsed=9.1s status=FILLED_ALL` / `oid=8350501 polls=8 refreshes=0 elapsed=21.2s status=FILLED_ALL` ⇒ 成交**仅靠缓存读**就在 9.1s / 21.2s 确认（旧实现同样两笔要 4 / 8 次刷新，慢单要 30 次）；同窗无 Traceback |

**未验证（诚实标注）**：①上述两笔是**未触发撤单**的路径（`refreshes=0`），T3 的"撤单前强制刷新 +
只此一次"仍只有用例枚举证明，尚无实弹样本；②生产限频是否彻底消失要看下一个完整盘前/盘后窗口
`grep '频率太高' logs/ext_entry.log` 是否归零 —— 判据写在此处，若仍出现则按 §6 继续收敛
`exit_lifecycle` 的刷新点。

## 5. 明确不做（边界）

- **不碰** `app/execution/exit_lifecycle.py` 的两处 `refresh_cache=True`：它们是退出证据闸
  （`exit_legacy_order_evidence_pending` / `exit_unowned_active_order`），读到陈旧缓存会**误拦或误放退出**，
  属交易行为面；频率也只有 1–2 次/分钟，不是本批病灶。
- **不碰** `position_sync` / `dual_broker` 的刷新（持仓与对账，另有 10/30s 额度与幻影仓语义）。
- **不做**跨进程令牌桶：本批证明根因是"单笔请求量"，不是"队列缺限速"；再加桶只是把浪费变成排队。
- **不改** `wait_order_fill` 的 `timeout_s=90 / poll_s=3`（成交确认时限不变）。

## 5.1 与既有资料冲突的地方（诚实标注）

`futuapi` skill 有一句「当美股模拟账号 `acc_type == STOCK_AND_OPTION` 时，查询持仓/资金/订单**必须**
传 `refresh_cache=True`，否则可能返回过时缓存」。本批**没有**全盘推翻它，而是把它收窄到"需要服务端真值"的时点：

- 反证（同账户实测）：同刻 `refresh=True` 与 `False` 返回**逐行一致**的 12 行；21:12 / 21:17 两笔研究臂
  真成交单，缓存轮询分别在 `polls=4 / 9.1s` 与 `polls=8 / 21.2s` 就读到 `FILLED_ALL`（`refreshes=0`）。
- 兜底仍在：`wait_order_fill` 对"缓存连续两轮看不到该单"强制刷新一次、撤单前强制刷新一次 —— 即那条
  skill 警告描述的场景（新单不可见 / 缓存与券商不一致）仍有服务端真值兜底，只是不再**每次轮询**都打。
- 若后续观测到"SIM 缓存滞后导致漏成交/误撤单"，回滚路径见 §7；判据是 `[ext_orders] wait_order_fill`
  行出现 `status=no_order_row` 或 `cancel_not_confirmed` 的频率上升。

**共享 skill 编辑台账**（multi-agent 协作纪律）：`~/.hermes/skills/devops/ftseek-troubleshooting-playbook/SKILL.md`
新增「Futu `order_list_query` 10/30s 限频只打在 refresh_cache=True 上」一节（+13 行，含实测读数与本批边界），
未修改该文件任何既有条目。

## 6. 残留债（EXT 链路，登记待办）

1. `exit_lifecycle.py` × 2 处刷新 = 退出 cron 每分钟 1–2 次；若下一窗口仍见限频，优先把它改成
   "缓存读 + 只在证据不足时刷新一次"（需单独预注册，因涉退出证据闸）。
2. `broker_capacity_shadow.py::_read_active_orders` 的 `refresh_cache=True` 目前**只在 REAL 下单路径被调用**
   （`dual_broker.submit_order` 的 `target_env == "real"`），SIM 下是死路 —— 保留但不得被当成"已在用"。
3. `web_dashboard` 的 `order_list_query(refresh_cache=True)` 属人工操作，频率受人为控制，未纳入本批口径。

## 7. 回滚

`git revert` 本提交即可（两个脚本文件 + 一个新测试 + 本文档；无 schema / 无配置 / 无 cron 变更）。
回滚后行为 = 旧的"轮询即刷新"，即 10/30s 限频会按原样复发。