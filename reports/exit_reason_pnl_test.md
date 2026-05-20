# Exit Reason × PnL Distribution Report

- 周期: 最近 **90** 天
- 生成时间: 2026-05-14T11:27:53
- 总规则数: 14

## 主表 (按 avg_pnl_pct 降序)

| exit_reason | n | win_rate | avg_pnl% | median% | p25/p75% | sharpe | min/max% | recommendation |
|-------------|---:|---------:|---------:|--------:|---------:|-------:|---------:|----------------|
| `EXTERNAL_CLOSE` | 1 | 100% | +7.32 | +7.32 | +7.32 / +7.32 | +0.00 | +7.3 / +7.3 | 🔍 insufficient |
| `tp_hit` | 5 | 100% | +1.02 | +0.99 | +0.86 / +1.27 | +3.32 | +0.6 / +1.4 | ✅ keep |
| `TRAILING_STOP` | 76 | 50% | +0.70 | +0.00 | -0.39 / +0.59 | +0.20 | -9.9 / +19.6 | ✅ keep |
| `MOC 收盘强制平仓` | 2 | 50% | +0.09 | +0.09 | -0.44 / +0.62 | +0.17 | -0.4 / +0.6 | 🔍 insufficient |
| `orphan_cleaned` | 428 | 0% | +0.00 | +0.00 | +0.00 / +0.00 | +0.00 | +0.0 / +0.0 | ⚠ marginal |
| `sync_ghost_cleaned` | 1 | 0% | +0.00 | +0.00 | +0.00 / +0.00 | +0.00 | +0.0 / +0.0 | 🔍 insufficient |
| `ghost_cleanup_cruise` | 5 | 0% | +0.00 | +0.00 | +0.00 / +0.00 | +0.00 | +0.0 / +0.0 | ⚠ marginal |
| `ghost_cleanup` | 2 | 0% | +0.00 | +0.00 | +0.00 / +0.00 | +0.00 | +0.0 / +0.0 | 🔍 insufficient |
| `sl_hit` | 90 | 21% | -0.10 | -0.06 | -0.24 / +0.00 | -0.45 | -0.7 / +0.8 | ⚠ marginal |
| `PRE_CLOSE_EXIT` | 22 | 27% | -0.11 | -0.33 | -0.59 / +0.18 | -0.16 | -1.0 / +1.7 | ⚠ review |
| `STRUCTURE_EXIT` | 33 | 30% | -0.24 | -0.31 | -0.64 / +0.12 | -0.46 | -1.1 / +1.1 | ⚠ review |
| `time_stop` | 2 | 50% | -0.62 | -0.62 | -2.97 / +1.72 | -0.27 | -3.0 / +1.7 | 🔍 insufficient |
| `FLASH_EXIT` | 4 | 0% | -1.69 | -0.84 | -0.87 / -0.25 | -0.92 | -4.8 / -0.3 | 🔍 insufficient |
| `BREAKEVEN_STOP` | 1 | 0% | -2.98 | -2.98 | -2.98 / -2.98 | +0.00 | -3.0 / -3.0 | 🔍 insufficient |

## 按方向分解 (LONG vs SHORT)

| exit_reason | direction | n | win_rate | avg_pnl% |
|-------------|-----------|---:|---------:|---------:|
| `EXTERNAL_CLOSE` | SHORT | 1 | 100% | +7.32 |
| `tp_hit` | SHORT | 5 | 100% | +1.02 |
| `TRAILING_STOP` | LONG | 44 | 41% | +0.57 |
| `TRAILING_STOP` | SHORT | 32 | 62% | +0.87 |
| `MOC 收盘强制平仓` | SHORT | 2 | 50% | +0.09 |
| `orphan_cleaned` | LONG | 143 | 0% | +0.00 |
| `orphan_cleaned` | SHORT | 285 | 0% | +0.00 |
| `sync_ghost_cleaned` | LONG | 1 | 0% | +0.00 |
| `ghost_cleanup_cruise` | LONG | 3 | 0% | +0.00 |
| `ghost_cleanup_cruise` | SHORT | 2 | 0% | +0.00 |
| `ghost_cleanup` | LONG | 2 | 0% | +0.00 |
| `sl_hit` | LONG | 38 | 11% | -0.12 |
| `sl_hit` | SHORT | 52 | 29% | -0.08 |
| `PRE_CLOSE_EXIT` | LONG | 17 | 29% | -0.15 |
| `PRE_CLOSE_EXIT` | SHORT | 5 | 20% | +0.01 |
| `STRUCTURE_EXIT` | SHORT | 33 | 30% | -0.24 |
| `time_stop` | LONG | 1 | 0% | -2.97 |
| `time_stop` | SHORT | 1 | 100% | +1.72 |
| `FLASH_EXIT` | LONG | 3 | 0% | -1.99 |
| `FLASH_EXIT` | SHORT | 1 | 0% | -0.80 |
| `BREAKEVEN_STOP` | SHORT | 1 | 0% | -2.98 |

## 按 session 分解

| exit_reason | session | n | avg_pnl% |
|-------------|---------|---:|---------:|
| `EXTERNAL_CLOSE` | post_close | 1 | +7.32 |
| `tp_hit` | post_close | 1 | +0.56 |
| `tp_hit` | premarket | 4 | +1.14 |
| `TRAILING_STOP` | post_close | 16 | -0.05 |
| `TRAILING_STOP` | premarket | 60 | +0.90 |
| `MOC 收盘强制平仓` | premarket | 2 | +0.09 |
| `orphan_cleaned` | post_close | 14 | +0.00 |
| `orphan_cleaned` | premarket | 414 | +0.00 |
| `sync_ghost_cleaned` | premarket | 1 | +0.00 |
| `ghost_cleanup_cruise` | post_close | 1 | +0.00 |
| `ghost_cleanup_cruise` | premarket | 4 | +0.00 |
| `ghost_cleanup` | premarket | 2 | +0.00 |
| `sl_hit` | post_close | 36 | -0.15 |
| `sl_hit` | premarket | 54 | -0.07 |
| `PRE_CLOSE_EXIT` | premarket | 22 | -0.11 |
| `STRUCTURE_EXIT` | post_close | 11 | -0.44 |
| `STRUCTURE_EXIT` | premarket | 22 | -0.14 |
| `time_stop` | premarket | 2 | -0.62 |
| `FLASH_EXIT` | post_close | 4 | -1.69 |
| `BREAKEVEN_STOP` | post_close | 1 | -2.98 |

## ⚠ 建议审查 (avg_pnl < -0.1%)

- `PRE_CLOSE_EXIT`: avg=-0.11%, n=22
- `STRUCTURE_EXIT`: avg=-0.24%, n=33
