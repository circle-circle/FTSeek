# Exit Monitor `held_hours` NameError Fix (2026-05-27)

## Symptom
`[退出监控] US.NVDA 检查异常: name 'held_hours' is not defined` repeating every ~15s for NVDA and all other symbols in open_positions.

## Root Cause
`position_exit_monitor.py` L537 had:
```python
held_minutes = held_hours * 60 if held_hours else 0
```
But `held_hours` is only defined at L1052 (much later in the same function). This was introduced during the P5 min_hold_minutes optimization (2026-05-22).

L510 already correctly computes `held_minutes`:
```python
held_minutes = (now_ts - entry_time_ts) / 60.0
```

## Fix
Removed the L537 line. `held_minutes` is already available from L510 calculation.

```diff
-                held_minutes = held_hours * 60 if held_hours else 0
+                # held_minutes already computed at L510 from (now_ts - entry_time_ts) / 60.0
```

## Deployment
- File: `app/risk/position_exit_monitor.py` L537
- Time: 2026-05-27 04:02 CST
- `__pycache__` cleared before restart

## Verification
```bash
grep "NameError.*held_hours" logs/ftseek_us.log  # should be 0 after restart
grep "退出监控.*检查异常" logs/ftseek_us.log | tail -3  # should show no new errors
```

## Impact
- The min_hold_minutes gate (10 min) was failing for ALL positions due to the NameError
- This means the P5 protection against premature exits (<5min, 21.7% WR) was NOT active
- Every exit rule evaluation for every position hit this exception on each ~15s cycle
- After fix: min_hold_minutes gate is restored, using correctly computed held_minutes from L510
