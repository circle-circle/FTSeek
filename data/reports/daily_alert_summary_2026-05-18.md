## 📊 FTSeek 每日告警汇总 — 2026-05-18

---

### 🇺🇸 美股 (FTSeek-US)

- **交易**: 15 笔 | 盈亏: $-265.22
- **持仓**: 1 只
- **告警**: 19 错误 + 3157 警告 (共 3176 条)

**最近告警**:
- 🟡 2026-05-19 13:21:16 [WARN] s_guard] WARNING: 🚫 [SymbolDailyLossGuard] US.TME 当日 cooldown 触发: consecutive_losses=2 >= 2 (today_pnl=$-163.28 trades=2 win=0)
- 🟡 2026-05-19 13:21:16 [WARN] s_guard] WARNING: 🚫 [SymbolDailyLossGuard] US.ARM 当日 cooldown 触发: consecutive_losses=2 >= 2 (today_pnl=$-14.96 trades=2 win=0)
- 🟡 2026-05-19 13:21:16 [WARN] rd] INFO: [MarginGuard] 美股保证金风控已启动 | 滞回间隙=0.1 | 最大API失败=2次 | 缓存TTL=30.0s
- 🟡 2026-05-19 13:26:13 [WARN] llocator] WARNING: [CapitalAllocator·RECONCILE] bucket=main 账本=0.000 实际=0.291 → 修正
- 🟡 2026-05-19 16:01:28 [WARN] b 采集失败 us_bloomberg: 所有实例连接失败

---

### 🇭🇰 港股 (FTSeek)

- **交易**: 2 笔 | 盈亏: -1.66%
- **持仓**: 0 只
- **告警**: 578 错误 + 23337 警告 (共 23915 条)

**最近告警**:
- 🟡 2026-05-19 16:59:54 [WARN] _scheduler] WARNING: [PreMarketScheduler] 辩论执行失败 (非阻塞): '\n  "direction"'
- 🟡 2026-05-19 16:59:54 [WARN] NFO: 熔断器已初始化 | 连续拒绝限制：8次 | 暂停时长：300秒 | 最大回撤：15.0%
- 🟡 2026-05-19 16:59:54 [WARN] 熔断器已初始化
- 🟡 2026-05-19 16:59:55 [WARN] 订阅完成: 成功=96, 失败=0, 总计=96
- 🟡 2026-05-19 17:00:01 [WARN] [预热] 完成: 成功填充 36 组数据, 0 组失败

---

### 📋 总结

- 总告警: 597 错误 + 26494 警告
- 🔴 告警较多，建议排查