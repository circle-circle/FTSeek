# RFC-204 · entry_confirm 确认后主线程提前派发

- 日期: 2026-11-13
- 状态: 已实现，待部署验证
- 关联: RFC-141（entry_confirm）、RFC-203（queue 延迟观测）
- 回滚: `playbook.entry_confirm_fast_dispatch_enabled=false` + 重启

## 1. 问题

entry_confirm 在 10 秒快循环中确认后，指令此前还要等下一个最长 180 秒的
`auto_execute` 轮次。确认窗本身 120 秒，但 treatment 的触发到成交可进一步达到数分钟，
产生不必要的追价和 chase cap 重复拦截。该延迟不是策略意图。

## 2. 实施

`TradingScheduler._maybe_auto_execute` 新增确认代际：

- `entry_confirm_ready_generation`：快循环每确认一批指令，只增不减；
- `_entry_confirm_dispatched_generation`：主线程已派发的最新代际。

主线程发现新代际时，提前调用**原有** `run_playbook_auto_execute`；不在退出快线程下单。
执行路径、报价、reprice、chase cap、费用、admission、预算、重复持仓和订单幂等全部不变。
普通 180 秒 interval 不变。

执行期间如果又有新确认，只把 dispatched 推进到本轮捕获的代际，下一轮会再次提前执行，
不会吞掉新事件。执行异常不推进 dispatched，下轮重试。

## 3. 配置

```yaml
playbook:
  entry_confirm_fast_dispatch_enabled: true
```

`false` + 重启恢复只按 180 秒 interval 执行。

## 4. 验证

`tests/test_entry_confirm_fast_dispatch.py` 钉住：

1. 无确认时普通 interval 不变；
2. 新确认只提前一次；
3. 执行期间新到确认不会丢失；
4. 回滚恢复 interval；
5. 执行失败不会吞掉确认代际。

## 5. 边界

- 这不是把 120 秒确认窗改成 0 秒，仍保留原 entry_confirm 判定和 20% holdout。
- 这不是绕过 chase cap/admission/费用/重复仓检查，所有检查仍在 auto_execute 内执行。
- 提前成交不保证改善收益，后续用 RFC-203 `queue_candidate` 对比确认→执行延迟与净结果。

## 6. 黑窗到点立即执行（同批）

`_maybe_auto_execute` 同时识别 `last < entry_open_blackout_hhmm <= now` 的首次跨点，
到点后下一轮立即执行，不再因为 180 秒 interval 额外等待。配置：

```yaml
playbook:
  open_floor_immediate_dispatch_enabled: true
```

`false` + 重启恢复原轮询。`tests/test_entry_confirm_fast_dispatch.py` 额外钉住跨点立即执行、
到点后不重复提前、回滚在子间隔内恢复原 interval。
