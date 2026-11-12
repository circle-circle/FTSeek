---
title: 跳出 DA 单策略：外部策略研究沙盒与 12 月转向候选
status: draft
module: cross
related_okr: OKR-2026-11
tags: [strategy-research, pivot, shadow-first, preregistration]
updated: 2026-11-12
---

# DEB-20261112 · 跳出 DA 单策略：外部策略研究沙盒与 12 月转向候选

> 状态：**研究预注册 + 首轮只读探测；零交易行为变更；不新建生产影子、不加门、不改 sizing/频率**
> 关联：`DEB-20261112-umar-review-external-strategies.md`、`DEB-20261111-win-small-loss-large-diagnosis.md`、`docs/OKR-2026-11.md` §6
> 边界：本文是 12 月 pivot 的研究输入，不是 11 月新入场源。任何行为变更仍须影子先行 + 一次一变量 + 用户裁决。

## 1. 为什么必须跳出“给 DA 再加一个门”

DA-only 独立复算已经给出结构性事实：

- 全窗 603 笔：毛 +7.84bps，费 9.69bps，净 −1.85bps；费用高于毛利。
- 前瞻窗 181 笔：毛 +0.26bps，净 −9.31bps；不是单纯费用问题，毛利本身接近零。
- 尾损集中在 `SL_INITIAL` 与 `OVERNIGHT_GAP_STOP`；open15 净 −10.10bps。
- DA 最大单笔 R 仅 +1.556，右尾不足以覆盖左尾。

因此，继续优化 DA 的置信度、形态、分析深度或再加一个过滤门，大概率是在同一个低毛利、负偏斜、费用敏感的分布里做局部搜索。本文把 DA 视为**一个待验证假设**，而不是系统边界；研究问题改为：

1. 哪些收益来源在机制上不同于“LLM 个股日内/短波段方向判断”？
2. 哪些来源能把约 10bps 往返费摊薄，或把负偏斜改成正偏斜？
3. 哪些来源在本仓数据、通道、治理下可验证，而不是纸面年化？

## 2. 外部证据：只借机制，不抄业绩

| 来源 | 可借机制 | 对本仓的含义 | 不可直接照搬 |
|---|---|---|---|
| Da/Liu/Schaumburg 2014, *A Closer Look at the Short-Term Return Reversal* | 非基本面冲击的短期反转；多头侧来自流动性冲击，空头侧来自情绪/卖空约束 | 5d 横截面反转可作为低相关候选；必须区分流动性补偿与接飞刀 | 原文用分析师修正分离基本面，本仓无 PIT 分析师数据 |
| Jegadeesh/Titman 1993, *Returns to Buying Winners and Selling Losers* | 3–12 月横截面动量 | 月频/季频可摊薄费用，与 DA 日内来源低相关 | 美股个股动量拥挤、崩溃风险；不能当无风险 alpha |
| Moskowitz/Ooi/Pedersen 2012, *Time Series Momentum* | 多资产自身趋势持续性，分散组合极端市场表现好 | 趋势跟踪可制造正偏斜，对冲“赢小输大” | 原文期货/远期，本仓股票通道不能直接复制杠杆/做空便利 |
| Lou/Polk/Skouras 2019, *A tug of war* |  overnight 与 intraday 收益由不同 clientele 驱动，策略利润可能集中在一个时段 | 必须分解 DA/候选策略的 close→open 与 open→close，而不是只看 close→close | 不能据此取消隔夜；C17 已否决 |
| Boyarchenko/Larsen/Whelan 2022, *The Overnight Drift* | 欧洲开盘时段库存风险释放，卖压后隔夜反转更强 | 隔夜收益不是均匀漂移，可能与收盘订单不平衡有关 | 本仓无期货 overnight 分时数据，不能直接交易 02:00–03:00 ET |
| Gatev/Goetzmann/Rouwenhorst 1999, *Pairs Trading* | close substitutes 的相对价格收敛 | 市场中性/低 beta 候选，可能降低组合方向暴露 | 原文利润对交易成本敏感；需要 PIT 票池、借券、价差执行 |
| Almgren/Chriss 2000; Gatheral 2016 | 执行成本 = 冲击 + 点差 + 时机风险；平方根冲击 | 建立成本-aware 期望门槛与被动限价执行研究 | 当前 $5k 级订单冲击小，主要矛盾是费/毛利，不是大单冲击 |
| Avellaneda/Stoikov 2008 | 库存风险 + 订单到达率决定最优报价 | 被动做市/限价执行可研究 adverse selection | 需要 L1/L2 逐笔归档；当前稀疏 L1 基线 IC≈0 |
| Moreira/Muir 2017; Barroso/Santa-Clara 2015 | 波动率管理降低动量崩溃/负偏斜 | 组合层风险预算，而不是给 DA 再加 sizing 门 | RFC-155 R2 已占用 open15 sizing 轴，11 月不新增 |
| López de Prado, *Advances in Financial Machine Learning* | triple-barrier、meta-labeling、purged CV、deflated Sharpe | 研究沙盒必须防重叠标签泄漏和多重检验 | meta-labeling 不得复活 C12 confidence/分析质量门 |

来源抓取件：`/tmp/strategy_lab_sources/`。业绩数字一律不作为立项依据。

## 3. 研究沙盒预注册（先于本轮探测）

### 3.1 共同口径

- 数据：`data/replay_bars_daily`（QFQ OHLC，无成交量）、`data/replay_bars_1m`（QFQ OHLCV，45 只当前票）。
- 票池：`deep110` = 日 K 起点 < 2022-01-01 的 110 只；`liquid45` = 有 1m 缓存的 45 只；`broad404` = 全部日 K 文件。三者都是**当前可得票池**，存在幸存者/选择偏差，不得当 PIT 回测。
- 成本：股票/ETF 往返 10bps；空头另加借券 3%/年，按持有交易日/252 计提。该借券率是研究假设，不是实测。
- 入场：日频信号在 t 收盘生成，t+1 开盘成交；出场按预注册开盘/收盘/止损。1m ORB 在突破价成交，同一 bar 同时触及止损时按止损优先。
- 统计：净 bps/笔或组合日收益；按入场日/交易日聚类；MDE80 = 2.802 × 日簇均值 SE。n_days < 30 不下结论。
- 多重检验：每个家族只设一个 primary variant；分层/年份/多空拆分是诊断，不得事后挑最优格立项。

### 3.2 假设与固定参数

| ID | 假设 | Primary variant | 固定参数 | 主要风险 |
|---|---|---|---|---|
| H1 | 短期横截面反转 | deep110 多空 | 5d lookback，bottom/top quintile，5d hold，每 5 个交易日非重叠调仓 | 流动性/幸存者偏差；空头借券 |
| H2 | 中期横截面动量 | deep110 多空 | 252d lookback skip 21d，top/bottom quintile，21d hold，每 21 个交易日非重叠调仓 | 动量崩溃；费用虽低但尾部集中 |
| H3 | overnight/intraday 分解 | H1 反转组合的时段分解 + liquid45/QQQ 无条件隔夜/日内 | close→open 与 open→close 分账；不新增交易规则 | 只能诊断利润来源，不能直接当 alpha |
| H4 | Donchian 趋势跟踪 | deep110 long-only | 55d 突破入场，20d 反向突破出场，3×ATR20 初始止损，下一开盘执行 | 震荡市连续小亏；日 K 止损路径保守 |
| H5 | 30min ORB 日内延续 | liquid45 多空 | 09:31–10:00 区间；10:00 后首次突破；止损=区间对侧；15:55 平仓 | 个股 ORB 拥挤；滑点/假突破 |
| H6 | 经济替代对配对 | XOM/CVX、MA/V、JPM/BAC | 252d formation，126d trading，spread z>2 开仓/0 平仓/4 止损，最长 20d | 3 对样本极小；成本敏感；结构性断裂 |

### 3.3 判据

- **KILL_RESEARCH**：primary 净均值 ≤ 0，或日簇 t < 1。
- **GRAY**：净均值 > 0 且 1 ≤ t < 2.802；只允许继续只读研究，不允许生产影子。
- **RESEARCH_SIGNAL**：净均值 > 0 且 t ≥ 2.802，且 OOS 半窗同向；仍不得直接上线，因为票池非 PIT。
- **PROMOTION_READY**：本轮不可能达到；必须另补 PIT 票池、真实成本/借券、前瞻影子、独立复核与用户裁决。
- 任何分层/年份/多空格若被用来改变 primary，视为挪门柱，必须重新预注册。

## 4. 首轮只读探测结果

脚本：`/tmp/strategy_lab/daily_probes.py`、`/tmp/strategy_lab/orb_probe.py`；产物：`daily_results.json`、`orb_results.json`。零 DB 写入、零券商调用、零 Futu 盘中拉 K。

### 4.1 口径修正（本轮内自我修正）

1. **推断用日簇均值，不用 pooled 笔均**：横截面组合每日腿数不等，pooled bps 会被后期更多票数放大。判据以 `daily_cluster.mean/t/mde80` 为准。
2. **长-only 必须报 benchmark alpha**：deep110 等权日再平衡买入持有 2022-07→2026-11 总收益 +213.3%（年化 +30.1%），QQQ +145.7%（年化 +23.0%）。 raw 净收益为正不等于 alpha。
3. **ORB 突破成交改保守**：若开盘已越过突破价，按开盘价成交，不按理想突破价；同 bar 止损优先。
4. **趋势固定名义 PnL 改固定股数**：`shares = notional / entry_price`，避免日度再平衡幻觉。

### 4.2 Primary 判据结果

| 假设 | Primary | 日簇净 bps | t | MDE80 | OOS 半窗 | 判据 |
|---|---|---:|---:|---:|---|---|
| H1 5d 反转 | deep110 多空 | +0.38 | 0.03 | 32.58 | −3.01 / +3.82 | **KILL_RESEARCH**（t<1） |
| H2 12-1 动量 | deep110 多空 | +46.18 | 0.66 | 195.81 | −32.27 / +127.76 | **KILL_RESEARCH**（t<1） |
| H4 Donchian | deep110 long-only | +196.91 | 2.70 | 204.41 | +117.69 / +276.63 | **GRAY**（t<2.802） |
| H5 30min ORB | liquid45 多空 | −8.91 | −3.84 | 6.50 | −7.09 / −10.76 | **KILL_RESEARCH** |
| H6 配对 | XOM/CVX、MA/V、JPM/BAC | −13.08 | −0.94 | 39.12 | −37.06 / +11.34 | **KILL_RESEARCH** |

H3 是诊断而非独立 primary：liquid45 无条件隔夜净 −4.46bps/日（t=−2.11），日内净 −7.04bps/日（t=−2.35）；QQQ 隔夜净 −6.37bps/日（t=−2.75），日内净 −7.55bps/日（t=−2.34）。**无条件每日隔夜/日内搬运在 10bps 成本下不可交易。**

### 4.3 真正有信息量的诊断：长-only、长周期、正偏斜、overnight 驱动

| 候选 | 日簇净 bps | t | alpha vs QQQ | alpha t | skew | payoff | WR | 组合最大回撤 | 费/毛 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| H1 deep110 5d 反转 long-only | +63.45/5d | 2.38 | +45.12/5d | 2.51 | +3.22 | 1.14 | 52.6% | −32.5% | 13.6% |
| H2 deep110 12-1 动量 long-only | +357.24/21d | 2.83 | +197.10/21d | 2.24 | +2.57 | 1.50 | 56.4% | −22.2% | 2.7% |
| H4 deep110 Donchian long-only | +196.91/笔 | 2.70 | 未做同窗 beta 调整 | — | +15.45 | 2.59 | 37.9% | −41.4%（active-capital） | 3.5% |
| DA 现役对照 | −1.85/笔 | −0.32 | — | — | 负偏斜 | 0.466 | 67.2% | — | **123.6%** |

关键分解：

- H2 long-only 的 21d 毛收益中，**overnight 贡献 +230.43bps（t=3.08），intraday 仅 +8.45bps（t=0.10）**。
- H1 deep110 long-only 的 5d 毛收益中，overnight +32.22bps（t=2.15），intraday +8.09bps（t=0.41）。
- H1/H2 的**空头腿均为负**：H1 deep110 short 净 −62.78bps，H2 short 净 −266.26bps。本轮不支持“多空市场中性”照搬；正收益主要来自长-only 权益暴露 + overnight 持续性。
- H4 事件级 skew 极高（+15.45），但 active-capital 组合最大回撤 −41.4%，并不比买入持有更抗跌；**正偏斜 ≠ 组合回撤小**。
- H2 long-only raw t=2.83 达到 RESEARCH_SIGNAL，但 alpha vs QQQ t=2.24、vs universe t=1.69，仍 GRAY；且票池非 PIT。**不得直接上线。**

### 4.4 费用结构是核心约束

| 策略 | 毛 bps | 往返费 bps | 费/毛 |
|---|---:|---:|---:|
| DA 全窗 | +7.84 | 9.69 | **123.6%** |
| H5 ORB | +1.47 | 10 | ~680% |
| H6 配对 | +2.59 | ~15.3 | ~590% |
| QQQ 无条件隔夜 | +3.63 | 10 | ~275% |
| H1 deep110 long-only | +73.60 | 10 | 13.6% |
| H4 deep110 | +283.47 | 10 | 3.5% |
| H2 deep110 long-only | +366.89 | 10 | **2.7%** |

结论：DA 的问题不是“少一个门”，而是**单位交易毛利太小、费用占比太高、左尾太肥**。提升收益的结构性方向是：更长持有期 / 更低换手 / 正偏斜长-only / 费用-aware  abstention，而不是继续压缩同一分布。

## 5. 独立优化议程（按预期信息价值排序）

### P0 · 先修研究地基（零交易行为，今天即可规划）

1. **PIT 研究数据 mart**：当前 404/110/45 票池都是“今天还活着/还在缓存里”的票，幸存者偏差不可消除。必须补：历史每日票池快照、退市/停牌、复权因子、成交量、bid/ask/spread、借券费率与可得性。
2. **多 horizon 前瞻收益台账**：复用 `signal_snapshots` / `backfill_forward_returns` 思路，但扩展到 5/21/63d 与 ETF；每条信号落 PIT feature snapshot，禁止用今日票池回测历史。
3. **成本与 benchmark 强制列**：所有研究读数必须并列 gross / fee / net / alpha vs SPY-QQQ / alpha vs 等权票池 / fee-gross ratio / day-cluster MDE。raw 正收益不得单独当结论。
4. **验证协议**：purged + embargo walk-forward、deflated Sharpe / 多重检验校正、一次一变量。不建重型回放框架；沿用“脚本即一次性证据”的台账纪律。

### P1 · 12 月首选 pivot 候选：长-only 中期动量 / dual momentum

- **机制**：12-1 横截面动量 + 绝对动量过滤；月频/21d 调仓；只多不空；overnight 持续性是主要利润来源。
- **本地证据**：H2 long-only raw +357bps/21d，skew +2.57，maxDD −22.2%，费/毛 2.7%；alpha vs QQQ +197bps/21d 但 t=2.24，非 PIT。
- **下一步预注册**：PIT 票池 + 真实成本；top quintile 或 top N；绝对动量（指数 12m>0 或 200d MA 上）；组合层 vol target；禁止用 DA confidence 做门。
- **通道可行性**：股票/ETF long-only，现有 Playbook 通道可承载；不需要期权/期货/借券。
- **风险**：动量崩溃、beta 伪装 alpha、幸存者偏差。若 PIT OOS alpha t<2.802，不晋级。

### P2 · 第二候选：长-only 短期反转（流动性冲击版）

- **机制**：5d 横截面反转，只多 losers；Da et al. 提示多头侧来自流动性冲击/fire sale，不是无脑接飞刀。
- **本地证据**：H1 deep110 long-only alpha vs QQQ +45bps/5d，t=2.51，skew +3.22；但 liquid45 OOS 前半 alpha≈0，broad404 受 penny/票池漂移污染。
- **前置条件**：必须有成交量/异常换手、bid-ask bounce、PIT 票池；否则无法区分流动性补偿与基本面恶化。
- **定位**：与 P1 低相关时才可作独立 sleeve；不得把空头腿负收益强行包装成市场中性。

### P3 · 第三候选：ETF/行业趋势与 dual momentum，而非单票 Donchian

- **机制**：Antonacci absolute+relative momentum；行业 ETF 月频轮动；趋势跟踪制造正偏斜。
- **本地证据**：单票 Donchian 事件级 skew +15.45，但组合 maxDD −41.4%，未证明比 beta 更优。应转向 SPY/QQQ/IWM/行业 ETF 分散组合。
- **数据缺口**：SPY/IWM/行业 ETF 1m 与足够长日 K 缓存不足；盘后补采，不盘中拉 Futu K。
- **风险**：行业轮动文献多为小样本/事后选择；必须 PIT + 多周期 OOS。

### P4 · 执行与费用：不是 alpha，但决定净期望

- **费用-aware abstention**：若事前预期毛收益 < 往返费 + 安全边际，则不交易。这是成本约束，不是 C12 confidence/分析质量门；11 月不做，12 月若走降频 pivot 须另起预注册。
- **被动限价 / 执行时机**：Almgren-Chriss / Avellaneda-Stoikov 提供框架，但当前 $5k 级订单冲击小，主要矛盾是佣金地板与毛利不足；先修 TCA spread/age/fee floor，再谈最优执行。
- **不得抬票面摊费**：OKR 已禁止；不得用放大尺寸伪装净期望改善。

### P5 · 本轮淘汰 / 暂不投入

| 方向 | 本轮证据 | 处置 |
|---|---|---|
| 个股 30min ORB | 净 −8.91bps，t=−3.84 | KILL；不扩张形态库 |
| 3 对经济替代配对 | 净 −13.08bps，t=−0.94 | KILL；除非 PIT 大票池 + 低成本借券 |
| 无条件每日隔夜/日内搬运 | QQQ/liquid45 扣费后均负 | KILL；overnight 溢价须长持有期摊费 |
| 多空市场中性照搬 | H1/H2 空头腿均负 | 不晋级；先证明短腿独立 alpha |
| 期权 VRP / 卖波动 | 通道不支持；负偏斜会加重“赢小输大” | 不做 |
| 指数再平衡 / PEAD / 新闻事件 | 拥挤/衰减；C10 已封口 | 不重开 |
| 稀疏 L1 订单流 / footprint | 既有 OFI 基线 IC≈0 | 先补真实 tick/L1，再谈 |

## 6. 12 月决策树（供 OKR §6 三选一）

1. **若 11-28 DA 净 bps>0 且 CI 改善**：保留 DA 为小 sleeve，继续 RFC-158/R2 结算；P1/P2 仍只作研究，不抢样本。
2. **若 DA≤0 且 CI 含 0**：默认答案不是再加门；优先选择“换策略类别”——P1 长-only 中期动量/dual momentum 进入预注册影子；P2/P3 排队。
3. **若 PIT OOS 后 P1/P2/P3 均无 alpha**：承认当前数据/通道下无可靠 edge，转研究项目，不以盈利目标硬上。
4. **任何晋级**：必须 PIT + 真实成本 + 前瞻影子 + 独立复核 + 用户裁决；raw 回测正收益不是晋级理由。

## 7. 复现命令

```bash
./venv/bin/python /tmp/strategy_lab/daily_probes.py > /tmp/strategy_lab/daily_results.json
./venv/bin/python /tmp/strategy_lab/orb_probe.py > /tmp/strategy_lab/orb_results.json
./venv/bin/python -m py_compile /tmp/strategy_lab/daily_probes.py /tmp/strategy_lab/orb_probe.py
```

来源抓取：`/tmp/strategy_lab_sources/`（reversal、momentum_jt、pairs_nber、overnight_tug、overnight_drift、almgren、gatheral_impact、avellaneda、dual_momentum、sector_rotation、vrp）。

## 8. 本文未做的事

- 未改任何生产 `.py`/YAML/ENV/DB/cron/推送/影子注册；未重启；未下单；未触碰四锁。
- 未新建生产影子；未把 H2/H1 诊断当 DA 证据；未用历史分层改 11 月阈值。
- 未补 PIT 票池/退市/借券/真实 bid-ask；未测 ETF 1m；未测真实 tick/L1；未做 purged walk-forward 正式裁决。
- 未跑全仓测试；本文仅文档 + `/tmp` 研究脚本。

## 9. DA 保留式落地核查（2026-11-12 02:10 追加，用户要求不放弃 DA）

### 9.1 预注册：只核查，不启用新门

用户裁决方向：**保留现有 DA 链路**，先分析/核查可实现方式。本节只做只读诊断与代码可行性审计，不改变 admission/horizon/exit 任何阈值。

固定核查项：

1. **现有 fee/admission 仪器复算**：用 `playbook_instructions.raw_instruction_json.admission.precheck.metrics` 的 `fee_bps`、`fee_gate.would_reject`、`profit_fee_ratio`、`notional` 连接 DA 成交，按现有阈值 11.54bps / $2,500 / 价格地板 $17.33 分组；不新设阈值。
2. **DA 事前上下文分层**：entry 前一日可得的 12-1 momentum、5d reversal、20d trend、QQQ 趋势，按 quintile 诊断；只报告，不挑最优格立项。
3. **DA 收益时段分解**：用日 K open/close + 实际 entry/exit 价拆 overnight vs intraday，验证 DA edge 是否仍在日内。
4. **持有期延长禁区**：`DEB-20260823-zero-gross-edge-five-rejections` 假设 2 已否决 T+1/T+3/T+5 延长持有；本节不重开。

判据：任何分组若 n<30 或日簇 MDE80>均值，只写“不可分辨”；不得把历史分层直接转成 DA 新门。

### 9.2 只读核查结果

脚本：`/tmp/strategy_lab/da_preserving_probe.py`；产物：`da_preserving_results.json`。另跑 `scripts/playbook_fee_gate_audit.py --source DA --json`（R06  legacy 门，`playbook_consumers=[]`，当前 Playbook 路径不消费）。

**DA 收益时段分解（n=571/603，覆盖 94.7%，对账误差 0）**：

- overnight 毛 +2.27bps，intraday 毛 +5.34bps；DA 毛收益约 70% 来自日内。
- 这与 `DEB-20260823` 假设 2/4 一致：DA edge 在日内，T+1 衰减；**不得把 DA 硬改成长线**。

**现有 fee/admission 仪器复算**：

| 分组 | n | 净 bps | 日簇 t | MDE80 | 判读 |
|---|---:|---:|---:|---:|---|
| realized fee > 11.54bps | 166 | −6.57 | −1.24 | 43.98 | 高费桶更差，但单独移除仍不足以转正 |
| realized fee ≤ 11.54bps | 437 | −0.78 | +0.47 | 25.63 | 低费桶接近打平，仍非正 edge |
| precheck fee_bps（n=465） | — | 均值 12.42 vs realized 9.10 | — | — | 19 例 precheck≤线但 realized>线；53 例 precheck>线（多为未成交/SKIP） |
| price < $17.33 历史成交 | 31 | +39.22 | +2.63 | 32.93 | 多为 price_floor_enforce 前历史样本；**不得据此撤价格地板** |

执行侧已有 `admission.enforce_at_execution=true` + `fee_floor_sizing=true`，但 `fee_gate_enforce=false`（2026-11-06 用户裁）。因此当前是“省钱 sizing 仍开、费用拒单关”。可落地缺口不是再造门，而是**执行时 fee_bps 校准/遥测**：reprice 后实际费率跨线的 19 例需要逐笔归因（价格漂移/股数/settlement/数据源）。

**DA 事前上下文 quintile（全部只作诊断）**：

- `signed_rev_5d` Q5 +21.67bps（t=1.00）vs Q1 −19.67bps；`signed_mom_12_1` Q5 −22.52bps；Donchian 低位 Q1/Q2 优于高位 Q3/Q4。DA 更像**回调/短期反转**，不是追涨动量。
- `profit_fee_ratio` Q5 +20.10bps（t=1.87）vs Q1 −30.05bps（t=−1.72）；TP/费用几何越好越有利，但仍未达显著。
- 所有上下文格 n≈100–120，MDE80 大多 > 均值或 t<2；**不可分辨，不得转成 DA 新门**。
- horizon：short n=597 净 −2.17bps；long n=6 净 +29.86bps，n<30 不可裁。

### 9.3 代码/数据可行性审计

| 现有件 | 状态 | 可复用方式 | 缺口 |
|---|---|---|---|
| `app/playbook/admission.py` | 现役 | fee_bps / profit_fee_ratio / price_floor / fee_gate record-only；纯函数可单测 | `fee_gate_enforce=false`；执行时费率跨线需遥测 |
| `instruction_generator.py` + `auto_execute_flow.py` | 现役 | 生成/执行两段 fee_floor_sizing；执行前 admission 复核 | reprice 后 fee 校准归因不足 |
| `horizon_selector.py` / `playbook_horizon.yaml` | 现役 | short/long（max 5d）选择 | 延长持有已否决；不重开 |
| `scripts/trend_state_recorder.py` | record-only | weekly_bias / daily_structure 事实 | 需确认 cron 新鲜度；不可直接当门 |
| `signal_snapshots.db` | 2026-05-14→11-11，612 票 | PIT 候选池/前瞻收益台账基础 | 非全市场、无退市、价格历史不足 |
| `playbook_instructions` | 2026-06-14→11-11，566 票 | DA 候选池 PIT 快照 | 月簇不足，12-1 动量不可裁 |
| ETF 日 K | QQQ/XLE/XLRE/XLY 5y；SPY/IWM 仅 2026；XLK/SMH 缺 | ETF 趋势/轮动研究 | 需盘后补采；1m ETF 缺 |
| Playbook 执行/退出/admission | 现役 | 新 sleeve 可复用执行管线 | 新入场源须 RFC + 影子；独立 decision_id 防污染 DA KR |

### 9.4 DA 保留式落地路径（按可执行性排序）

1. **P0 零行为 · DA 保留式日报**：复用 `/tmp/strategy_lab/da_preserving_probe.py`，每日只读输出 fee bucket / profit_fee_ratio / 事前上下文 / overnight-intraday 分解。不改交易路径，不新增推送（若接日报须走 `app/push/` hub 且无料不推）。
2. **P0 零行为 · 执行费率校准审计**：逐笔连接 precheck fee_bps、执行 reprice、realized fee_bps，定位 19 例跨线根因；先修尺子，再谈是否恢复费用拒单。
3. **P1 测量 · PIT 候选 mart**：以 `signal_snapshots` / `playbook_instructions` 为每日候选池快照，补 daily volume / bid-ask / 退市 / ETF；为 12 月 sleeve 提供非幸存者样本。当前 5–6 个月 PIT 深度不足以裁 12-1 动量。
4. **P2 12 月 sleeve A · 长-only 中期动量/dual momentum**：独立 `decision_id='MOM-%'` / `owner_strategy='momentum_sleeve'`，复用 Playbook 执行/admission/退出；月频、只多、绝对动量过滤、组合层 vol target。**DA 链路不动**，DA KR1 仍只算 `DA-%`。
   - SIM 可见落地设计见 `docs/debates/DEB-20261112-sim-visible-multi-sleeve-design.md`：`owner_strategy` 短期保持 `playbook_sim` 路由不变，sleeve 隔离用 `decision_id + metadata.sleeve`；新增 scheduled trigger / swing horizon / sleeve 子帽，默认关闭。
5. **P3 12 月 sleeve B · 长-only 5d 流动性反转**：周频、只多 losers；前置 volume/异常换手/PIT。与 DA 的回调属性可能互补，但必须独立验证。
6. **P4 12 月 sleeve C · ETF 趋势/轮动**：先补 SPY/IWM/XLK/SMH 等日 K/1m；再测 dual momentum / 行业轮动。
7. **费用拒单恢复（若走降频）**：只有在执行费率校准 + 前瞻影子证明“高费桶净期望显著为负且误杀可控”后，才讨论 `fee_gate_enforce=true`；不得抬票面摊费，不得与 R2 同批。

### 9.5 明确不做（保留 DA 的边界）

- 不延长 DA 持有期（T+1/T+5 已否决）；不取消隔夜（C17）；不撤价格地板；不抬票面摊费。
- 不把 §9.2 历史 quintile 直接转成 DA confidence/形态/分析质量门（C12/C16 禁区）。
- 不把新 sleeve 收益混入 DA KR1；不用 EXT/MOM 尾部掩盖 DA 主通道。
- 不在 11 月新建生产影子/门/sizing/频率变更；本文仍只是分析与核查。
