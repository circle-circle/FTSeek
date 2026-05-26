# 🔴 RFC: 作战计划交易参数注入执行层

**状态**: 待审批  
**优先级**: P2 (胜率优化)  
**风险等级**: 🔴 (交易主路径变更)  
**日期**: 2026-05-26

---

## 问题

当前作战计划 (`daily_battle_plan.json`) 中的交易参数 (`stop_loss_pct`, `take_profit_pct`, `max_position_pct`) **从未被执行引擎读取**。

### 现状

```
BattlePlanGenerator  →  daily_battle_plan.json  →  BattlePlanManager (门禁检查)
                              ↓                           ↓
                        SL/TP/仓位参数              仅 allow/block 判断
                              ↓                           ↓
                        ❌ 无人读取                executor._execute_open()
                                                      ↓
                                              使用 dynamic_exit_manager 的通用配置
                                              (stop_loss=2.5%, take_profit=8%)
```

### 根因

`executor.py:1890-1905` 获取 SL/TP 的逻辑:
```python
stop_loss = dynamic_exit_manager.get_stop_loss()      # 通用配置 2.5%
take_profit = dynamic_exit_manager.get_take_profit()   # 通用配置 8.0%
```

**没有检查作战计划是否为目标指定了特定参数。**

---

## 方案

在 `executor.py` 门禁检查通过后 (约 line 1626)，注入计划参数到 `signal.extra`:

```python
# executor.py:1626 (门禁检查通过后)
else:
    logger.debug(f"✅ [作战计划门禁] 允许开仓 {signal.code}: {reason}")
    
    # 🔴 新增: 注入计划交易参数 (如存在)
    try:
        plan_params = plan_mgr.get_target_trade_params(signal.code)
        if plan_params.get("stop_loss_pct") is not None:
            if not hasattr(signal, 'extra') or signal.extra is None:
                signal.extra = {}
            signal.extra["plan_stop_loss_pct"] = plan_params["stop_loss_pct"]
            signal.extra["plan_take_profit_pct"] = plan_params["take_profit_pct"]
            signal.extra["plan_max_position_pct"] = plan_params["max_position_pct"]
            logger.debug(
                f"[计划参数注入] {signal.code}: "
                f"SL={plan_params['stop_loss_pct']:.1%}, "
                f"TP={plan_params['take_profit_pct']:.1%}"
            )
    except Exception as e:
        logger.warning(f"⚠️ 计划参数注入失败 (不影响交易): {e}")
```

然后在 `executor.py:1890-1905` 使用这些参数:

```python
# executor.py:1890 (获取 SL/TP)
is_us = signal.code.startswith('US.')
if is_us:
    us_params = _get_us_exit_params()
    stop_loss = abs(us_params.get('stop_loss', 0.03))
    take_profit = abs(us_params.get('take_profit', 0.05))
else:
    # 🔴 新增: 优先使用计划参数 (如有)
    plan_sl = getattr(signal, 'extra', {}).get("plan_stop_loss_pct") if hasattr(signal, 'extra') else None
    plan_tp = getattr(signal, 'extra', {}).get("plan_take_profit_pct") if hasattr(signal, 'extra') else None
    
    if plan_sl is not None and plan_tp is not None:
        stop_loss = abs(plan_sl)
        take_profit = abs(plan_tp)
        logger.info(f"[计划参数] {signal.code} 使用计划 SL={stop_loss:.1%} TP={take_profit:.1%}")
    else:
        stop_loss = dynamic_exit_manager.get_stop_loss()
        take_profit = dynamic_exit_manager.get_take_profit()
```

---

## 影响分析

### 优点

1. **目标级精细化风控**: 不同标的可使用不同 SL/TP (如高波动股 -4%/+10%, 低波动股 -2%/+6%)
2. **辩论结果落地**: 辩论系统对每个标的的 SL/TP 建议能真正影响执行
3. **胜率提升**: 避免通用配置对特定标的不适用 (如 HK.00700 波动小，-2.5% 太紧)

### 风险

1. **🔴 交易主路径变更**: 修改 `_execute_open()` 的 SL/TP 获取逻辑
2. **参数质量依赖**: 如果计划的 SL/TP 不合理，会直接影响盈亏
3. **回滚复杂度**: 需要同时回滚 executor.py + battle_plan.py

### 缓解措施

1. **仅港股生效**: US 市场保持不变 (已有独立 exit params)
2. **防御性校验**: 注入前校验 SL/TP 范围 (如 1%-10%)
3. **日志记录**: 每次注入都记录，便于归因
4. **开关控制**: 可通过 `unified_config.yaml` 的 `battle_plan.inject_params_to_executor` 开关

---

## 验证计划

1. **模拟盘测试**: 先在模拟盘验证参数注入不引发异常
2. **A/B 对比**: 同一标的分别使用通用配置 vs 计划配置，对比盈亏
3. **回滚测试**: 验证开关关闭后回退到通用配置

---

## 依赖

- ✅ `BattlePlanManager.get_target_trade_params()` 已实现 (本次提交)
- ⏳ `executor.py` 注入逻辑 (待审批)

---

## 建议

**建议审批通过**。理由:
1. 作战计划参数不落地是最大的逻辑断裂
2. 已有完整接口和防御设计
3. 开关控制可随时回滚

**下一步**: 用户审批后，实施 executor.py 变更。
