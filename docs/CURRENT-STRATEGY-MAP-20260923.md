# 当前交易策略与逻辑总览 (盘前 / 盘后 / 盘中)

> 生成 2026-09-23 (CST, ET 9/22 夜盘后) · 用户令「调整变化比较大，请给出当前盘前盘后以及盘中的策略和逻辑，我确认是否符合预期」
> **口径**: 全部**实读**配置/代码/DB, 不引用注释与记忆。凡标 `[源]` 者可直接 grep 复核。
> 模式: `AUTO_TRADE_ACCOUNT=sim` / `ALLOW_REAL_TRADING=false` ⇒ `is_live=False`、券商 `SIMULATE` (19930823)。

---

## 0. 总览: 三条独立通道

| 通道 | 载体 | 人群 | 方向 | 出场 | 近期实测 |
| --- | --- | --- | --- | --- | --- |
| **盘前** ET 04:00–09:00 | crontab `*/5 16-22 * * 1-5` → `scripts/extended_hours_entry_executor.py --session premarket` | futu 异动榜 + **当日 bmo 财报票** + 研究臂 | **只做多** | 9:25 收口强平(仅当日仓) + 利润保护; **亏损侧不止损** | 最后成交 9/18; 近 8 日 0 笔 |
| **盘后** ET 16:00–20:00 | crontab `*/5 4-8 * * 2-6` → 同脚本 `--session afterhours` | 异动榜 + **当日 amc 财报票** | **只做多** | 同上 + 隔夜交接 | 最后成交 9/18 |
| **盘中 (RTH)** 09:30–16:00 | `main.py` 主进程(systemd) → DA 决策 → `playbook_instructions` → auto_execute → SIM | Screener / UE 选股 | **双向 (BUY + SELL_SHORT)** | 退出引擎: SL / 保本 / trailing / 时间 / DEAD_SIGNAL / EOD | 9/21 7 笔, 9/22 3 笔 |

三者关系: 延长时段是**独立通道**(自带入场/出场/收口), 盘中是**唯一管道**(DA→指令→执行→退出栈)。交点在"仓": 延长时段进的仓 09:25 收口只平**当日仓**, 隔夜仓交给盘中退出栈接管。当前 1 仓 `US.RIVN` 属后者。

---

## 1. 盘前 (premarket) —— 只多、只买当日财报票、只买温和涨

**触发**: 每 5 分钟一轮, ET 04:00–09:00 (夏令时)。冬令时 11/1 后 ET 窗右移 1h, 已用小时段 `16-22 CST` 覆盖到 09:29 ET (窗内零风险的 DST 免疫)。
**候选来源** (三源取并集): ① futu 异动榜 ② 当日财报票 —— futu 日历 **∪** Finnhub 日历 (`EXT_EARNINGS_EVENT_SOURCE=calendar_finnhub_union`, 2026-09-23 上线) ③ 研究臂 momentum 池 (`EXT_ENTRY_RESEARCH_MOMENTUM=true`)。

**买什么 (全部必须同时成立)** `[源: .env.us + scripts/extended_hours_entry_executor.py]`
1. **当日同相位财报事件**: `EXT_EARNINGS_CRITERION=calendar_actual` —— 必须**当天**发财报, 且相位(bmo/amc)与当前时段一致
2. **财报超预期**: 当日报表 `epsActual − epsEstimate > 0`; est 缺失/为 0/相位未标定 ⇒ **fail-close** (三态挡)
3. 价格 ≥ **$5**; 市值 ≥ **$10 亿**; 成交额 ≥ **$1000 万** (A 档 ≥ $2000 万)
4. **涨幅带 0.5% ~ 2.0%** (`EXT_ENTRY_CHG_BAND`) —— 只买"温和上涨", 涨过头的不追、跌的不接
5. 量确认比 ≥ 0.5; 盘口深度比 ≥ 1.0 (A 档 2.0); 价差 ≤ 1.0% (A 档 0.5%); 距 VWAP ≤ 0.8%; 报价漂移 ≤ 0.5%; 往返费 ≤ 0.5%
6. 日内 ≤ **10** 笔、同时 ≤ **5** 仓; 单笔 **$5,000** (B 档 $2,500)

**出场** `[源: crontab 每分钟行 → scripts/extended_hours_exit_cron.py (包装) → scripts/premarket_exit_shadow.py (2026-09-17 起已非 record-only) + scripts/extended_hours_profit_exit_rules.py]`
- 延长时段 **只做止盈、不做止损** (2026-09-17 用户令; 2026-10-08 用户进一步要求未触发利润保护时移交盘中)。实读 `premarket_exit_shadow.py`
- 止盈/回本锁: `decide_original_profit_exit` + B 档浮盈 ≥ **+3%** 启动保本锁 (`EXT_GRADE_B_CERT_LOCK_PCT=3.0`, 出场标签 `SL_CERT_LOCK`; A 档 0.0 = 不锁)
- **09:25 ET 不再默认收口强平**: 今日盘前仓未触发利润保护时持有到 RTH, 交盘中 Playbook SL/BE/trailing/EOD 接管 (`EXT_PREMARKET_FLATTEN=false` 默认; `true` 为旧语义回滚开关)
- ETH 限价腿**零成交**后进入 HELD 时, RTH 开盘且本地持仓仍在 ⇒ 自动归档删除该 intent, 解除 `blocked_exits`, 盘中策略立即恢复评估; 部分成交仍需人工先核对部分仓位/账本, 不自动释放
- 不给亏损仓"免死金牌"(设计不变量); 挂单超时 60s (`EXT_EXIT_LIMIT_TIMEOUT_S=60`), 新 ETH 单最后可提交时间=时段边界前 60s 等待+10s 撤单余量 (`EXT_EXIT_CANCEL_MARGIN_S=10`); 孤儿单检测 on

**实测 (为什么它安静)**: 一周被**价格闸**拦 ~1 万票次, 其中 **99% 是 <$5 的微盘泵票(价中位 $1.4)**; 过价格闸的票再被市值/深度/价差/涨幅带层层拦掉; 最终 **0–2 笔/日**, 最近一次成交 **9/18**。⇒ 不是故障, 是"**财报票人群天然低频**" + "异动榜与闸不匹配"。

---

## 2. 盘后 (afterhours) —— 同规则, 相位换成 amc

与盘前**同脚本、同判据、同闸、同仓位帽**, 差别只有三处:
- 时段 ET 16:00–20:00 (crontab `*/5 4-8 * * 2-6`)
- 财报相位 = **amc** (盘后发财报的票)
- 出场多了**持夜与次日交接**语义 `[源: scripts/premarket_exit_shadow.py]`:
  `EXT_AFTERHOURS_HOLD_TO_OPEN=false` (2026-10-08 起), 未触发利润保护不做次日开盘限价兑现, 下一 RTH 交盘中退出栈接管; `true` 为旧开盘兑现回滚开关且才会启用 exit cron 的 09:30–09:45 `next_open` 分支

---

## 3. 盘中 (RTH) —— 双向、DA 逐单生成参数、退出栈接管

**链路** `[源: app/playbook/*.py]`
`main.py` (systemd 常驻) → 调度触发 → Screener / UE 选股 → **DA 决策**(LLM: decision → bridge → DB 直写) → `playbook_instructions` → auto_execute → SIM 下单 → `open_positions` → 退出引擎

**方向: 双向** `[源: playbook_instructions 近 30 天]`
| 方向 | 指令数 | 平均 SL | 平均 TP | 平均 confidence |
| --- | --- | --- | --- | --- |
| BUY | 251 | **−4.69%** | **+7.67%** | 60 |
| SELL_SHORT | 80 | **−5.30%** | **+8.27%** | 56 |
| SKIP | 55 | — | — | 68 |

SL/TP **由 DA 逐单生成**(非固定百分比), TP 另有硬门 **RR ≥ 1.5** (`_MIN_TP_RR`, pipeline 硬编码)。

**入场门** `[源: app/playbook/pipeline.py + config/unified_config.yaml]`
- VWAP 方向一致硬门 (`entry_vwap_gate_enabled: true`, 容差 0.003, **开盘 VWAP 未就绪时 fail-open**)
- 追入区 +5% (`CHASE_ZONE_PCT`), 极端 gap 8% 过滤
- 禁追线 `chase_cap` (方向感知: BUY 上限 / SHORT 下限), **enforce 已激活** (`unified_config.yaml:1530` `chase_cap_enforce_enabled: true`, 违例指令终态跳过不下单, 遥测带 enforced=true; 回滚=置 false 热重载即退回 record-only)
- B 门 (接飞刀硬门)、UE tier 门、min_confidence (档位 0.45~0.7)
- 开盘前 15 分钟 SL 放宽到 `cooldown_sl_pct: 4.0`

**退出栈** `[源: config/playbook_horizon.yaml (Pipeline 退出唯一配置源)]`
- 保本线 `breakeven_at_pct: 0.4%` (触发即移 SL 到保本)
- 峰值回撤 `drawdown_from_peak_pct: 0.8%` (trailing 锁利)
- 时间退出 `time_exit_hours: 6h` **仅亏损时**触发
- 最大持有 `max_hold_days: 1` (由 EOD 兜底 MAX_HOLD_T5 执行)
- EOD 强平兜底 (水下仓可豁免持夜)、赢家隔夜、盘中再入场另有专属规则

**实测 (近 30 日 508 笔 playbook_sim)** `[源: config/exit_calibration.json + trade_records]`
- 胜率 **66.7%**, 均 pnl **+0.03%**, 利润回吐占比 32.7%, 均 MAE/MFE ≈ 0.88%/0.89%
- 出场原因 top: **保本止损 99** ≫ 跟踪止损 53 > DEAD_SIGNAL 21 > 初始 SL 15 —— 即"**多数单在保本线附近了结**"
- 分档异常: high-confidence 24 笔 **WR 33% / 净 −$366**, 而 mid 档 320 笔 **WR 73% / 净 +$353**

---

## 4. 2026-09 这轮**动了什么 / 没动什么** (解释"变模糊")

**动了 (只在延长时段通道内)**
| 变更 | 内容 |
| --- | --- |
| 判据② 换口径 (RFC-097) | 旧: 拿 Finnhub `period` 当新鲜度锚(off-calendar 票龄 ≪ −7 误判 stale); 新: 读**当日**报表 `actual vs estimate` |
| 涨幅带 | `EXT_ENTRY_CHG_BAND=0.5,2.0` (只买温和涨) |
| 候选源并源 | futu 日历 ∪ Finnhub 日历 (`calendar_finnhub_union`), 零资格放宽 |
| 可见性 | 闸计数独立行 / 事件源行 / 日报 `--always` / 周复盘卡 / 影子看板段12-13 |
| 影子 (零下单) | RTH fade 影子 + −3.5% 平行止损臂; 毕业裁定器 |

**没动 (盘中通道)**: 入场门、SL/TP、退出栈参数、仓位帽 —— **一字未改**(冻结基线 2026-06-27 + 7/19~8/17 既有调整)。这几轮对盘中只做**只读核查**。

---

## 5. 待确认清单 (请逐条对预期)

1. 盘前/盘后 **只做多** + **延长时段亏损侧不止损** + **09:25 只平当日仓** —— 是否符合预期?
2. 盘前/盘后 **人群 = 当日财报票**(异动榜只是辅助筛) —— 是否仍是你要的? 代价: 0–2 笔/日, 多数日子 0
3. 盘中 SL ≈ **−4.7%~−5.3%** / TP ≈ **+7.7%~+8.3%** (DA 逐单) / RR 底线 **1.5** / 保本 **0.4%** / 回撤 **0.8%** / 时间退出 **6h 仅亏损** / 最大持有 **1 日** —— 是否符合预期?
4. 现役 1 仓 `US.RIVN` LONG 382 @15.615, SL 14.81 (−5.16%), 归属 `playbook_sim`, 走盘中退出栈 —— 是否同意继续持有至 RTH 规则触发?
5. 新增观察: **保本止损是最大出场原因(99/508)** + **high-confidence 档反而最差(WR 33%)** —— 要不要单独立项核查?
