# DEB-20261112 · Umar 方法论独立复核 + 外部策略本地探测

> 状态：**独立复核完成；只读；零交易行为变更；不立项、不加门、不新建影子**
> 数据时点：2026-11-11 ET 已平仓成交；2026-11-12 复核
> 关联：`docs/analysis/2026-11-12-umar-method-borrowing.md`、`DEB-20261111-win-small-loss-large-diagnosis.md`、RFC-155 §6、RFC-158、OKR-2026-11
> 口径：DA = `source_type='PLAYBOOK_SIM' AND decision_id LIKE 'DA-%'`；A10 现役时代；A9 干净口径为主，含/不含并列。全 Playbook 对照 = `source_type LIKE 'PLAYBOOK%'`。

## 1. 一句话结论

原文“机械 Umar 形态不可照搬、先做度量、本月不加门”的方向正确；但 §10 的 R/死单/inside-day 读数混用了全 Playbook，不能直接指导 DA。
DA-only 复算后，核心问题更精确：**负偏斜 + 开盘/隔夜尾损 + 费用高于毛利**。外部可借鉴的不是“早盘冲高做空”，而是**风险尺寸管理、事前结构分层、成本-aware 期望门槛**；其中尺寸管理已由 RFC-155 R2 占用，本月只能继续攒样。

## 2. DA-only 独立复算

脚本：`/tmp/umar_review/da_metrics.py`；产物：`/tmp/umar_review/da_metrics.json`。A9 含/不含在 DA 人群相同（603/603），因为 4 笔首跑测试批不在 DA。

| 窗口 | n | 日簇 | 毛 bps | 费 bps | 净 bps | 净额 | WR | 笔均赢 | 笔均亏 | payoff | 打平 WR | 死单率 | R/笔 | 日聚类 t | MDE80(日 bps) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 全窗 07-01→11-11 | 603 | 88 | +7.84 | 9.69 | **−1.85** | −$537.00 | 67.2% | +$27.15 | −$58.24 | 0.466 | 68.2% | 21.4% | −0.0140 | −0.32 | 20.41 |
| 前瞻 10-06→11-11 | 181 | 27 | +0.26 | 9.58 | **−9.31** | −$878.40 | 79.0% | +$19.79 | −$97.61 | 0.203 | 83.1% | 18.2% | −0.0164 | −0.37 | 39.03 |
| 10-23→10-31 | 30 | 6 | −51.81 | 7.84 | **−59.65** | −$1,024.04 | 70.0% | +$16.24 | −$151.67 | 0.107 | — | 23.3% | — | −1.56(USD) | 307.32(USD) |
| 11-01→11-11 | 70 | 8 | +16.21 | 9.08 | **+7.13** | +$231.63 | 80.0% | +$17.62 | −$53.93 | 0.327 | 75.4% | 18.6% | +0.0117 | +1.02(USD) | 79.66(USD) |

关键修正：

1. **DA 右尾比原文更窄**：DA-only `max R=+1.556`，不是全 Playbook 的 +3.99；p10 −0.318R / p90 +0.216R。原文用 p10/p90 描述“每笔亏是每笔赢 2.5 倍”不严谨；DA 平均赢 R +0.112 vs 平均亏 R −0.271，payoffR≈0.412。
2. **DA 费用不是“≈ΣR”那么轻**：全窗 `ΣR_net=−8.41`，其中 `ΣR_gross=+5.78`、`ΣR_fee=14.19`。费用 R 是毛 R 的 2.46 倍；全窗费/毛=123.6%，前瞻窗费/毛≈3622%，11 月费/毛≈56%。DA 要净正，毛 edge 必须稳定超过约 9–10bps 往返费。
3. **“删掉死单就转正”是事后 MFE 选样**，不是可交易规则；C12 已封口，不得用 confidence/分析质量/事后 MFE 建门。
4. **10-23→10-31 是近期恶化主窗**：毛 bps 已为 −51.81，不只是费用问题；11 月毛 bps 回到 +16.21。不能把 10 月尾部灾难和 11 月恢复混成一个稳定状态。

## 3. 尾损归因：开盘段 + 隔夜跳空 + 原始/early-tight SL

全窗 DA 出场桶：

| exit_reason | n | 净额 | R/笔 | 死单率 |
|---|---:|---:|---:|---:|
| `SL_INITIAL` | 34 | **−$4,817.56** | −0.597 | 82.4% |
| `OVERNIGHT_GAP_STOP` | 11 | **−$3,616.05** | −1.222 | 72.7% |
| `MFE_STALL_EXIT` | 21 | −$752.04 | −0.291 | 81.0% |
| `TIME_EXIT` | 28 | −$717.96 | −0.204 | 64.3% |
| `SL_TRAIL_LOCK` | 161 | +$6,728.60 | +0.169 | 0% |
| `SL_BREAKEVEN` | 249 | +$1,351.49 | +0.020 | 0% |
| `PRE_CLOSE_EXIT` | 37 | +$1,479.00 | +0.183 | 13.5% |

`SL_INITIAL + OVERNIGHT_GAP_STOP` 合计 **−$8,433.61**，而 DA 全窗净仅 −$537.00；其余桶合计为正。`SL_INITIAL` 必须拆：early-tight 18 笔 −$2,012.68，原始 SL 16 笔 −$2,804.88。

入场时段（ET，相对 09:30）：

| 时段 | n | 净额 | 净 bps | WR | payoff |
|---|---:|---:|---:|---:|---:|
| 0–15min | 327 | **−$1,530.83** | −10.10 | 63.0% | 0.464 |
| 15–60min | 215 | +$680.94 | +6.24 | 74.4% | 0.411 |
| 60min+ | 61 | +$312.89 | +10.48 | 63.9% | 0.784 |

11 笔 `OVERNIGHT_GAP_STOP` 中 7 笔入场在 open15、10 笔在 open30。10-23→10-31 最差 3 笔全是隔夜跳空 LONG：LMT 10-30 −$438.00、CRM 10-27 −$356.09、MRK 10-30 −$264.35，合计 −$1,058.44。11 月已平仓 DA 没有 `OVERNIGHT_GAP_STOP`。

这支持继续验证 **RFC-155 R2 open15 0.5×尺寸** 和 **RFC-158 entry_confirm v2**，不支持“放宽 SL / 补仓 / 取消隔夜 / EOD 减半 / 抬 BE / 固定 TP 重校”。

## 4. 原文 B1–B5 的可行性修正

### B1 订单流背离：当前基线无预测力，前置条件不满足

- 代码核对：`app/playbook/pipeline_parts/intraday_indicators.py::_capital_flow_divergence` 是 record-only，但读取 `data/capital_flow_daily.jsonl` 的**日级聚合** `super_in_flow+big_in_flow` 符号，不是“价格上涨但主动买盘递减”的分钟/逐笔背离。`aggregated_at` 可晚于 `date`，若盘中消费有前视风险。
- OFI 基线探测：`/tmp/umar_review/ofi_probe.py`。`data/futu_ofi_baseline.jsonl` 现有 62,176 行、247 票、58 日；与 1m bar 重叠 37 票。因果对齐后有效观测 29,248，57 日。
  - L1 imbalance、microprice premium、稀疏 OFI proxy 对 1/5/15min 前向收益的 pooled Pearson |r|≤0.0044；日聚类 Spearman IC 均值 |mean|≤0.0153，|t|≤1.78，全部低于 MDE80。
  - 三分位前向收益差 <2bps，远低于约 10bps 往返费。
- 结论：**当前稀疏 L1 快照基线不可用作准入证据**。这不等价于“真实逐笔订单流无效”；真 tick/footprint 仍无历史归档，未测。不得据此新建采集或门；若未来要测，先 RFC + 静默窗采集 + 基线预测力预注册。

### B2 不交易日：只能描述性分层，不能本月设门

- 原文 inside day 用“当日高低落在前日区间内”，对入场过滤是**前视**。改为事前可知的“前一日 inside”后，DA n=61/37 日，净 +24.59bps，日聚类 t=1.43，MDE80=50.91bps ⇒ 方向正但不可裁。
- FOMC 周已补测：官方 2026 日历为 Jan 27–28、Mar 17–18、Apr 28–29、Jun 16–17、Jul 28–29、Sep 15–16、Oct 27–28、Dec 8–9。按 Umar“周一至周三不做”映射，DA n=49/9 日，净 −27.33bps，日聚类 t=−1.21，MDE80=37.94bps ⇒ 描述性负向，样本不足，不立项。
- 跳空分层：DA 低开≤−1% n=147 净 −15.90bps；高开≥1% n=152 净 −11.54bps；平开±1% n=276 净 +10.80bps。两侧跳空都弱，不是单纯“低开做空/高开做多”问题。§11 已否决“早·跳空”新门，新 cohort 方向未复现，继续只读。

### B3 供给区：与既有价位/追价轴共线，不新增硬约束

`key_levels.support/resistance` 可继续 record-only，但 C16 已关闭入场形态/追价帽轴。若要做，只能作为 RFC-158 同实验的人群刻画，不能另立“多次拒绝位”硬门。

### B4 极低频选择性：功效不足，且不得用事后 MFE 选样

“top decile 才做”会把 n 压到 KR1 样本门以下；若用历史 MFE/exit_reason 选子集，是事后选样恒等式。12 月若走降频 pivot，必须用事前可识别字段 + 独立 OOS/前瞻 cohort，不能本月加门。

### B5 R 倍数：应做，但必须 DA-only 并列 bps

R 口径有价值，但不能替换 KR1 的 DA 净 bps/笔。建议日报并列：DA `ΣR_net / R_gross / R_fee`、avg winR/lossR、payoffR、初始 SL 距离、赢家 MFE 捕获率、open15/15–60/60+、`SL_INITIAL` early/original、OGS。复用 `scripts/session_attribution.py`，不新增影子/推送。

## 5. 外部策略调查与本地探测

### 5.1 Umar Ashraf 本人方法：定性可参考，业绩不可作立项依据

可抓取二手/课程页（Chart Academy instructor、TradeZella 5-stage framework、opentools 视频转录摘要）支持以下定性描述：每月约 20 个交易日中只做 4–5 个好日子；FOMC/CPI 等噪声周观望；主要做开盘后 1–2 小时；只用 VWAP，不依赖 RSI/MACD/均线；读关键位订单流；最佳 setup 加仓、不利条件小仓。
边界：业绩数字未经审计；没有可执行 SL/TP/ATR/仓位公式；“inside day / 供给区”细节仍主要来自二手转述。机械可定义部分已在原文 §9 证伪：冲高失败做空 −10.4bps vs 无条件 −9.7bps，Δ<MDE。

### 5.2 时间序列动量（Moskowitz/Ooi/Pedersen 2012）

文献：58 个期货/远期品种，1–12 个月自身收益持续性，分散组合在极端市场表现好。
本地探测：`/tmp/umar_review/tsmom_probe.py`，45 只当前票池日 K，20d lookback → 5d hold，下一开盘入场、5 日后收盘出场，10bps 费。10,104 事件/675 个非重叠块：pooled 净 −5.74bps、毛 +4.26bps；多头侧净 +30.97bps，空头侧净 −49.41bps；多头日聚类 t=2.48 但 MDE80=67.83bps > 均值 60.10bps ⇒ 功效不足。
判读：有“多头趋势侧可能优于空头侧”的探索信号，但票池幸存者偏差、非 DA 人群、非当前持仓周期。**不得据此改 DA 方向配额或新建门**；12 月换策略类别时才可预注册。

### 5.3 波动率管理 / 风险管理动量（Moreira/Muir 2017；Barroso/Santa-Clara 2015）

文献：按前期已实现方差反向缩放暴露，高波动时降风险；可显著降低动量崩溃/负偏斜/峰度，提高 Sharpe。核心是**风险尺寸管理，不是新增 alpha**。
本仓映射：RFC-155 R2 open15 0.5×尺寸正是“特定高风险入场段降暴露”的本地化候选；当前 `PRE_REGISTERED / TOO_EARLY`。继续按 §6 判据攒样，不新增 sizing 轴、不借机加仓/提频。C14 已否决 EOD 减半隔夜仓；R2 是入场时尺寸，不是 EOD 减仓，二者不得混同。

### 5.4 市场日内动量（Gao/Han/Li/Zhou 2018；Baltussen/Da/Lammers/Martens 2021）

文献：SPY 第一半小时收益（前收→10:00）预测最后半小时（15:30→16:00），高波动/高成交量/宏观新闻日更强；机制与日内对冲/处置效应有关。
本地探测：`/tmp/umar_review/intraday_mom_probe.py`，45 只个股 1m，9,518 symbol-day/215 日。`sign(r1)` 做最后半小时、10bps 费：净 −10.73bps（毛约 −0.73bps），日聚类 t=−12.16；`r1+r12` 同向才交易：净 −5.12bps。高低 |r1| 三分位均无正净 edge。
判读：**指数 ETF 的日内动量不能直接搬到本 45 只大盘股池**。SPY/QQQ 1m 缓存缺失，未测指数本体；交易时段禁开 Futu 拉 K，不补采。不得据此新建个股尾盘动量入场源。

### 5.5 ORB / 形态库扩张

外部 ORB 综述亦提示简单 ORB edge 随拥挤衰减；本仓 §9 冲高失败/镜像/无条件对照均无增量。不扩张形态库，不做“高 RVOL×逆向”独立门（N5 已列反向证据）。

## 6. 合规建议优先级

1. **P0 度量修正（今天可做，零行为）**：DA-only R/费/MFE 捕获率 + open15/15–60/60+ + `SL_INITIAL` early/original + OGS + 毛/费/净三段。复用现有日报，不新增影子/推送。
2. **P1 RFC-158 entry_confirm v2**：继续攒样，不改 120s/mid/grace/holdout；附录 B“早·跳空”只读。样本门未达前不裁。
3. **P2 RFC-155 R2 open15 0.5×**：继续 2026-11-11→11-27 前瞻确认；只降美元左尾/回撤，不宣称 alpha；到期功效不足只能 `INCONCLUSIVE_NO_DEPLOY`。
4. **P3 费用第二变量**：DA 费/毛全窗 123.6%，必须跟踪；但不得抬票面摊费（RFC-191 Option B 撤回/Option C 作废）。任何费用行为变更另起当前 cohort 预注册，且不得与 R2 同批。
5. **P4 12 月 C1/三选一**：当前 B1 基线无预测力、B2 FOMC/inside 样本不足、B4 top-decile 功效不足 ⇒ 本月不立项。11-28 若 DA 净 bps≤0 且 CI 含 0，按 OKR 降频/换策略/承认无 edge 三选一，不默认再加门。

明确不做：机械 Umar fade、inside-day 硬门、分批止盈、期权/ES、footprint 自建、取消隔夜、confidence/分析质量门、放宽 SL/补仓跳空亏损、EOD 减半、BE 0.4→0.8、固定 TP 重校、个股尾盘动量新源。

## 7. 复现与来源

```bash
./venv/bin/python /tmp/umar_review/da_metrics.py > /tmp/umar_review/da_metrics.json
./venv/bin/python /tmp/umar_review/da_structure.py
./venv/bin/python /tmp/umar_review/ofi_probe.py
./venv/bin/python /tmp/umar_review/intraday_mom_probe.py
./venv/bin/python /tmp/umar_review/tsmom_probe.py
./venv/bin/python /tmp/umar_probe/readout.py
```

外部来源（2026-11-12 抓取）：
- Umar Ashraf 课程/框架：`https://www.chartacademy.com/instructors/umar-ashraf`、`https://www.tradezella.com/strategies/5-stage-trading-framework`、`https://opentools.ai/youtube-summary/i-spent-a-day-with-the-world-1-day-trader-umar-ashraf`
- FOMC 2026 官方日历：`https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`
- Moskowitz/Ooi/Pedersen, *Time Series Momentum*：`https://elmwealth.com/wp-content/uploads/2017/06/timeseriesmomentum.pdf`
- Moreira/Muir, *Volatility-Managed Portfolios*：`https://www.nber.org/system/files/working_papers/w22208/w22208.pdf`
- Barroso/Santa-Clara, *Momentum has its moments*：`https://thetabruv.com/risorse/cta/32-barroso-momentum-has-its-moments.pdf`
- Gao/Han/Li/Zhou, *Intraday Momentum*：`https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf`
- Baltussen/Da/Lammers/Martens, *Hedging demand and market intraday momentum*：`https://academicweb.nd.edu/~zda/intramom.pdf`

## 8. 未验证 / 未做

- 未测真实逐笔订单流/footprint；OFI 结论仅限当前稀疏 L1 快照代理。
- 未测 SPY/QQQ 指数日内动量本体（1m 缓存缺失，交易时段禁开 Futu）。
- FOMC/inside-day 分层只是历史描述，未前瞻验证；R2/RFC-158 新 cohort 未达样本门。
- 未改任何 `.py`/YAML/ENV/DB/cron/推送/影子注册；未重启服务；未调用券商；未跑全仓测试。

## 9. P0 读数落地与现役实验进度（2026-11-12 01:31 追加）

本节继续只读，不改交易行为。P0 扩展读数脚本：`/tmp/umar_review/p0_readout.py`；产物：`/tmp/umar_review/p0_readout.json`。
现有 `scripts/session_attribution.py --date 2026-11-11 --json` 的 I 段已覆盖 DA 11 月毛/费/净、payoff、打平 WR、open15/15–60/60+、`SL_INITIAL` early/original；本节补 R/MFE 捕获率、名义额分层、open15 半尺寸旧样本反事实。

### 9.1 DA-only R / MFE 捕获率 / 名义额分层

| 窗口 | 人群 | n | 净 bps | R/笔 | 赢家 MFE 捕获率中位 |
|---|---|---:|---:|---:|---:|
| 全窗 | DA | 603 | −1.85 | −0.0140 | 54.0% |
| 全窗 | open15 | 327 | −10.10 | −0.0323 | 53.4% |
| 全窗 | 15–60min | 215 | +6.24 | +0.0106 | 55.9% |
| 全窗 | 60min+ | 61 | +10.48 | −0.0023 | 52.8% |
| 11 月 | DA | 70 | +7.13 | +0.0117 | 57.8% |
| 11 月 | open15 | 38 | −14.96 | −0.0329 | 52.9% |
| 11 月 | 15–60min | 24 | +41.06 | +0.0772 | 59.3% |
| 11 月 | 60min+ | 8 | +16.85 | +0.0270 | 58.5% |

名义额分层（描述性，不是费用门裁决）：

| 窗口 | <$4,000 | ≥$4,000 |
|---|---|---|
| 全窗 | n=180，净 −$1,213.61 / −22.98bps | n=423，净 +$676.61 / +2.85bps |
| 11 月 | n=32，净 −$57.70 / −4.91bps | n=38，净 +$289.33 / +13.96bps |

这与既有费用影子“`<$4,000` 被拦组净负、`<$5,000` 被拦组净正”同向，支持未来把**成本-aware 期望门槛**作为 P3 单独变量；但本月不得设门，不得与 R2 同批，不得抬票面摊费。

### 9.2 open15 半尺寸旧样本反事实（仅描述，非 R2 裁决）

| 窗口 | 受影响 n | 半尺寸净 Δ | 反事实总净 | 日级 Δ 均值 | MDE80(日级) | 判读 |
|---|---:|---:|---:|---:|---:|---|
| 全窗 | 327 | +$765.41 | +$228.41 | +$10.21 | $32.95 | 效应 < MDE，不可裁 |
| 11 月 | 38 | +$133.28 | +$364.92 | +$19.04 | $35.11 | 效应 < MDE，不可裁 |
| 10-23→10-31 | 15 | +$302.80 | −$721.24 | +$50.47 | $160.87 | 减尾但仍净亏 |

结论：半尺寸能降低美元左尾，但**不是 alpha**；10 月末毛 edge 为负时，仅靠尺寸不能转正。继续按 RFC-155 §6 新 cohort 裁决，旧样本不得凑数。

### 9.3 RFC-155 R2 当前进度

复跑：`PYTHONPATH=. ./venv/bin/python scripts/risk_size_tier_forward_eval.py --out /tmp/umar_review/risk_size_forward.json`。

- 新确认窗 2026-11-11→2026-11-27：`affected=0/30`、`days=1/10`、`resolved=1/3`、`delta=0.0`。
- verdict=`TOO_EARLY`；样本门、覆盖率、经济/稳健性/统计判据均未达。不得提前上线 0.5×。

### 9.4 RFC-158 entry_confirm v2 当前进度

复跑：`PYTHONPATH=. ./venv/bin/python` 内调用 `scripts.settle_entry_confirm_forward.main()`，输出重定向到 `/tmp/umar_review/entry_confirm_forward_latest.json`。

- verdict=`INSUFFICIENT`；裁决窗自 2026-11-05。
- treatment：assigned 51 / resolved 45 / coverage 88.24%；holdout：assigned 18 / resolved 15 / coverage 83.33%。覆盖率 <95%，样本门未达。
- 每 resolved assignment 净额：treatment +$2.6707 vs holdout +$0.0273；`SL_INITIAL` 负担/每 resolved assignment：2.608 vs 7.3413。方向偏正，但不可裁。
- 机制量：避免率 41.18%，CONFIRMED opp_cost 均值 0.129%（中位 0.0892%）。不得据此改 120s/mid/grace/holdout 或提前转硬门。

### 9.5 本轮动作边界

- 新增/更新仅 `/tmp` 读数产物与本 DEB/handoff 文档；未改 repo 内 `.py`/YAML/ENV/DB/cron/推送/影子注册。
- 未重启服务，未调用券商，未跑全仓测试。R2/RFC-158 继续按原预注册攒样。

### 9.6 执行质量补查：open15 尾损不是滑点主导

复跑：`PYTHONPATH=. ./venv/bin/python scripts/execution_tca_report.py --out /tmp/umar_review/execution_tca.json`；DA-only 滑点从 `trade_records.metadata_json.execution.slippage_bps` 只读提取。

- RFC-152 TCA 窗 2026-10-20→11-11：尝试 124 / 成交 74 / join 70，样本门 PASS。点差 p50=10.81bps、p90=29.46bps；报价年龄 p50=1747ms、p90=18483ms。计划→成交缺口均值 90.19bps / 中位 113.56bps，但重定价→成交均值 −4.73bps / 中位 −2.77bps。
- 点差桶与报价年龄桶的净 bps 均未过日聚类 MDE80；`provisional_would_block` n=32 净 −9.54bps vs pass n=38 净 −20.77bps，差异不可裁。不得据此新设点差/报价年龄硬门。
- DA-only 入场滑点：open15 均值 1.005bps / 中位 1.14 / p90 11.06；15–60min 均值 1.149 / 中位 0.675 / p90 8.84；60min+ 均值 0.758。open15 并不比 15–60min 更差。最差 OGS 滑点：LMT 1.23、CRM 7.97、MRK 未列前六、ORCL 14.27、GAP −4.10、AMD −0.97。
- 判读：**open15/隔夜尾损主要是方向与跨夜路径问题，不是成交滑点主导**；执行侧点差/报价年龄是成本与容量问题，应留在 P3 单独变量，不得与 R2/RFC-158 混批，也不得用不可裁的分桶差新建门。
