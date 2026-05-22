# P1c: PRICE_FAIL Blocklist Gap Fix (2026-05-22 04:34 CST)

## Problem
The `price_fail_closed.json` blocklist was only written at **100+** consecutive price fetch failures, but positions were being force-closed at **~50** failures. This meant positions closed at 50-99 failures were NOT added to the blocklist → re-imported on every service restart → repeated losses.

## Evidence
5 option symbols were force-closed at 50-53 failures but only 3 were in the blocklist:

| Symbol | Failures at Exit | Loss | Was in Blocklist? |
|--------|-----------------|------|-------------------|
| US.TSLA260603P410000 | 53 | -$7,749 | ✅ (by luck) |
| US.RIVN260605P13000 | 50 | -$4,664 | ❌ MISSING |
| US.QQQ260602P710000 | 50 | -$2,720 | ❌ MISSING |
| US.QQQ260602P700000 | 52 | -$1,958 | ✅ |
| US.SPY260602P730000 | 51 | -$689 | ✅ |

**Total avoidable loss from missing blocklist entries: -$7,384**

## Root Cause
In `position_monitor.py`, the `_fail_n >= 50` branch only logged a warning. The blocklist write was only in the `_fail_n >= 100` branch. After force-close at ~50, the counter was reset, so it never reached 100 for the same symbol → never blocklisted.

## Fix
Added blocklist write inside the `_fail_n >= 50 and _fail_n % 50 == 0` branch (position_monitor.py L552). Now at 50 failures:
1. Warning logged
2. Blocklist written (NEW)
3. At 100: force-close + blocklist write (idempotent append)

Also manually added the 2 missing symbols to `price_fail_closed.json`.

## Verification
- Service restarted, position_sync correctly skips all 5 symbols
- No BLOCKING config audit warnings
- No NameError/UnboundLocalError
- Health check: OK
