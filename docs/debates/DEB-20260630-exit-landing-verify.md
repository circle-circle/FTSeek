# DEB-20260630: 统一退出策略落地验证

## 背景

实施了基于波动率桶的统一退出策略(7项改动)。需要Claude Code验证改动在正确执行路径上、无矛盾。

## 改动清单

| # | 文件 | 改动 | 验证点 |
|---|------|------|--------|
| 1 | exit_policy.yaml | atr_stop加vol_bucket_multipliers(4桶)+hysteresis_pct | grep vol_bucket_multipliers |
| 2 | exit_policy_guardian.py | get_atr_multiplier加atr_pct参数+_select_vol_bucket+get_max_loss_pct | grep _select_vol_bucket |
| 3 | position_exit_monitor.py | L1638-1656: 按ATR%选桶动态选atr_mult | grep vol_bucket_multipliers in scan_positions路径 |
| 4 | playbook_horizon.yaml | vol_buckets加extreme桶+position_mult=0.5 | grep extreme |
| 5 | exit_policy.yaml | pre_close enabled=true+loss_flat=true | 已验证(上一场辩论) |
| 6 | exit_rule_registry.py | hard_stop_reasons加HARD_STOP/ATR_STOP | 已验证 |
| 7 | playbook/pipeline.py | _check_playbook_exit_rules禁用5规则 | 已验证 |

## 验证问题

1. position_exit_monitor.py L1638-1656的vol_bucket选择代码是否在scan_positions的实际执行路径上？
2. exit_policy_guardian.py的_select_vol_bucket是否被调用？还是position_exit_monitor.py内联了桶选择逻辑？
3. exit_policy.yaml的vol_bucket_multipliers配置是否被正确读取？
4. playbook_horizon.yaml的extreme桶是否被instruction_generator的vol_buckets逻辑匹配？
5. SNDK(owner=manual)是否被ExitMonitor监控？还是被跳过？

## 讨论记录

### 第 1 轮：hermes

**立场：7项改动已落地，请Claude Code验证执行路径**

关键验证点：
1. position_exit_monitor.py L1638的vol_bucket选择代码在_update_trail_state函数中，被scan_positions调用 → 在执行路径上
2. exit_policy_guardian.py的_select_vol_bucket是新增方法，但position_exit_monitor.py没有调用它（内联了桶选择逻辑）→ 两条路径需要统一
3. SNDK的owner=manual → 可能被ExitMonitor的is_protected跳过 → 需要验证

### 第 1 轮：claude-code

1. 是。`_update_trail_state` 被 `scan_positions` 在 L1089 调用，vol_bucket 选择代码在其内 L1638-1656，位于实际执行路径。
2. `position_exit_monitor.py` 内联了桶选择逻辑（L1646-1656），未调用 `_select_vol_bucket`；后者仅在 `exit_policy_guardian.py` 的 L216/L275 被调用。两条路径并存，逻辑未统一。
3. 是。`exit_policy.yaml` 的 `vol_bucket_multipliers` 在 `position_exit_monitor.py` L1646 和 `exit_policy_guardian.py` L212/L272 均被正确读取。
4. 是。`playbook_horizon.yaml` extreme 桶（L164-172）含 `atr_pct_max: 999.0`，`instruction_generator.py` 的排序匹配逻辑会按从小到大命中 extreme 桶。
5. 否。`owner=manual` 仓位被 `is_protected()` 判定为 PROTECTED，`position_exit_monitor.py` L793 直接跳过，不纳入 ExitMonitor 监控。

### 收敛: Hermes接受Claude Code验证

**验证结果**:
1. ✅ vol_bucket选择代码在scan_positions执行路径上
2. ⚠️ position_exit_monitor内联桶逻辑, 未调_select_vol_bucket → 两条路径不统一(P2, 功能正确但维护成本高)
3. ✅ exit_policy.yaml vol_bucket_multipliers正确读取
4. ✅ playbook_horizon.yaml extreme桶能被instruction_generator匹配
5. ❌ SNDK owner=manual被is_protected跳过 → 已修复(DB改owner=auto)

**行动**:
- [已修复] SNDK owner: manual→auto
- [P2] 统一桶选择: position_exit_monitor调guardian._select_vol_bucket(功能正确, 暂不修)
- [通过] 7项改动验证完成, 无阻断性问题
