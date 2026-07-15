# ADR-018: Universe-Scan clue brief 增补财报日历 + 内部人集群硬事件

- 状态: Accepted
- 日期: 2026-07-14
- 关联: ADR-017 (硬事件段/新闻分类), `app/data/earnings_calendar.py`, `data/reports/insider_daily_*.json`

## 背景

ADR-017 接入了来源 1 (CatalystClassifier 从新闻分类硬事件)。安静日新闻命中少 →
`HARD_EVENT` 仍稀疏。项目内已有两个成熟的"日历型/结构化"硬事件能力未接入扫描线:

1. **财报日历** `EarningsCalendar` (Alpha Vantage): 近期已发布 + 临近财报。
2. **内部人集群** `data/reports/insider_daily_{date}.json` (InsiderScan 每 4h 持久化):
   集群买入信号。

## 决策

在 `_build_clue_brief` 的 `[硬事件]` 段增补两个来源, 各自独立 config 开关:

- `_gather_earnings_events()`: `EarningsCalendar.get_recently_reported(2d)` +
  `get_watchlist_earnings(3d)` → `[HIGH/EARNINGS]` (已发布, 关注财报后动量) +
  `[MEDIUM/EARNINGS_UPCOMING]` (临近, 时点风险)。**仅用 1 次日历调用** (内置 1h 缓存);
  **不用逐股 `analyze_post_earnings_opportunity`/history 接口** —— 实测该接口频繁触发
  Alpha Vantage 5/min 限流返回"历史数据不可用", 不稳定。
- `_gather_insider_clusters()`: 读**今日** `insider_daily_{date}.json` 的 `stats.by_symbol`
  → `[HIGH/INSIDER_CLUSTER] TICKER x{n} → 看多`。纯文件读, 无网络; 只读今日 (不用陈旧数据)。

开关 (代码默认 `false` 安全回退, yaml 显式 `true`):
- `universe_expansion.clue_earnings_events`
- `universe_expansion.clue_insider_clusters`

两者**分别独立**于 `clue_hard_events` —— 因风险面不同: 新闻分类=本地正则(零成本);
财报=网络 API(可能限流); 内部人=本地文件。独立开关便于单独回退 (如 AV 限流时只关财报)。

## 影响范围

- 仅**丰富 clue brief 输入**; 经 V2 → `min_confidence`/`max_targets_per_day` 闸门后进富途模拟盘。
  不碰下单/退出链路; 仅 sim。
- 任一来源失败 (导入/网络/解析) 均降级返回 `[]`, 不影响其余来源与原 clue brief。
- 财报: 每次 scan 最多 1 次 Alpha Vantage 日历调用 (受 500/天 额度约束, 已 1h 缓存)。

## 预期与边界 (诚实)

- **限流即空**: Alpha Vantage 免费额度紧张时日历返回空 → 财报段当次为空, 属预期降级。
- **内部人数据质量**: 现有 `insider_daily` 部分信号 `buyers/amount=0` (source=log),
  仅 symbol + 次数可靠; 故仅注入 symbol 与集群次数, 不臆造金额。
- 安静日 (无财报窗口 + 无集群) 仍可能整体为 0 —— "宁缺毋滥", 非缺陷。

## 验证

- `py_compile scripts/llm_universe_scan.py` 通过。
- 实测: 财报路径返回 JNJ/BAC/JPM(近期) + NFLX/TSM/UNH(临近) (未限流时);
  内部人路径对非空样本 (07-07) 正确输出 `TSM x3 → 看多`; 今日 (07-14, 0 信号) 返回 []。
- 均含 try/except 降级。

## 回退

`config/unified_config.yaml` → 相应 `clue_earnings_events` / `clue_insider_clusters` 置
`false`, 重启服务。
