## 📊 FTSeek 每日告警汇总 — 2026-06-29

---

### 🇺🇸 美股 (FTSeek-US)

- **交易**: 5 笔 | 盈亏: $+5.20
- **持仓**: 7 只
- **盘中告警**: 470 错误 + 1361 警告 (共 1831 条)
- 🔇 非盘中静默: 38 条 (已过滤)

**盘中告警**:
- 🟡 2026-06-30 03:17:10 [WARN] ING: [组合止损] 触发 WARN 告警 (×171)
- 🟡 2026-06-30 03:17:10 [WARN] ING: [组合止损·trigger] action=WARN drawdown=+2.38% equity=$98849 total_cost=$2089 total_pnl=$-0 positions=[US.UNH@417.70×5] (×34)
- 🟡 2026-06-30 03:20:56 [WARN] ING: [组合止损·trigger] action=WARN drawdown=+2.39% equity=$98845 total_cost=$2089 total_pnl=$+1 positions=[US.UNH@417.70×5] (×34)
- 🟡 2026-06-30 03:23:09 [WARN] NG: [FutuLV3·DailyK] US.UNH 富途历史K线过期 (latest=2025-08-11, >10天前) → 返回None触发TwelveData fallback (×28)
- 🔴 2026-06-30 03:23:11 [ERROR] der] ERROR: [TwelveData] 请求失败 time_series: 429 Client Error: Too Many Requests for url: https://api.twelvedata.com/time_series?symbol=UNH&interval=1da... (×27)
- 🟡 2026-06-30 03:23:11 [WARN] er] WARNING: [Router] get_daily_bars US.UNH: 所有数据源均失败 (×27)
- 🟡 2026-06-30 03:33:42 [WARN] NG: [FutuLV3·DailyK] US.PG 富途历史K线过期 (latest=2025-08-11, >10天前) → 返回None触发TwelveData fallback (×25)
- 🔴 2026-06-30 03:33:43 [ERROR] der] ERROR: [TwelveData] 请求失败 time_series: 429 Client Error: Too Many Requests for url: https://api.twelvedata.com/time_series?symbol=PG&interval=1day... (×25)

---

### 🇭🇰 港股 (FTSeek)

- **交易**: 0 笔 | 盈亏: +0.00%
- **持仓**: 0 只
- **盘中告警**: 0 错误 + 0 警告 (共 0 条)

---

### 📋 总结

- 盘中告警: 470 错误 + 1361 警告
- 🔇 非盘中静默: 38 条
- 🔴 盘中告警较多，建议排查