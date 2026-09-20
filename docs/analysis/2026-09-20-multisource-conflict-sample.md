# 跨源同指标一致性抽样（多源冗余冲突定谳）— 2026-09-20

> 目的：把上一轮数据面审计里「多源冗余 → 同一指标跨源数值可能不一致」从**结构性观察**
> 推进到**可量化结论**。全程只读；未改任何代码/配置/门/阈值，未重启服务，未下单。
> 第三方 API 额度消耗：finnhub 336 次（免费档 60/min）、TwelveData 30 次（8/min）、
> AlphaVantage **14/25 次日额度**（周日非交易时段，生产侧 AV 仅为其降级链末位，影响≈0）。

## §1 方法与预注册阈值（采集前定死，事后不改）

样本：`trade_records` 8/1 以来实际交易过的全部美股标的 = **112 只**（去重后）。

| 指标 | A 源 | B 源 | 冲突阈值（预注册）|
| --- | --- | --- | --- |
| I1 昨收 | futu `snapshot.prev_close_price` | finnhub `/quote.pc` | \|Δ\| > 5bps |
| I1b 昨收 | futu 同上 | TwelveData `/quote.previous_close` | \|Δ\| > 5bps |
| I2 昨收（同源跨路径）| futu 快照 | futu 日K `[-2].close` | \|Δ\| > 5bps |
| I3 PE(TTM) | futu `pe_ttm_ratio` | finnhub `/metric.peTTM` | 相对 > 10% |
| I3b PE(TTM) | futu `pe_ttm_ratio` | AlphaVantage `OVERVIEW.PERatio` | 相对 > 10% |
| I4 共识方向 | futu `buy%` | finnhub `(strongBuy+buy)/total` | 一侧 ≥60% 且另侧 ≤40% |
| I5 覆盖家数 | futu `total` | finnhub total | 记录比值（无阈值）|
| I6 目标价 | futu `average` | TwelveData `/price_target.median`；AlphaVantage `AnalystTargetPrice` | 相对 > 5% |
| I7 当日涨跌% | futu `(last-prev_close)/prev_close` | finnhub `/quote.dp`；TwelveData `percent_change` | \|Δ\| > 10bps |

## §2 结果（2026-09-20 单日快照）

| 指标 | 可比 n | 冲突数 | 冲突率 | 中位 Δ | p90 \|Δ\| |
| --- | --- | --- | --- | --- | --- |
| I1 昨收 futu vs finnhub | 112 | **0** | **0%** | 0.00bps | 0.0bps |
| I1b 昨收 futu vs TwelveData | 15 | **0** | **0%** | 0.00bps | 0.0bps |
| I2 昨收 futu 快照 vs futu 日K | 20 | **0** | **0%** | 0.00bps | 0.0bps |
| I3 PE(TTM) futu vs finnhub | 87 | 11 | **13%** | 1.97% | 14.7% |
| I3b PE(TTM) futu vs AlphaVantage | 8 | 0 | 0% | 0.03% | 1.2% |
| I4 共识方向 futu vs finnhub | 73 | 8 | 11% | 2.13pp | 17.9pp |
| I5 覆盖家数 比值 futu/finnhub | 73 | — | — | **0.50x** | 0.6x |
| I6 目标价 futu vs TwelveData | 1 | 0 | 0% | 2.29% | 2.3% |
| I6b 目标价 futu vs AlphaVantage | 9 | 3 | 33% | 2.89% | 6.8% |
| I7 当日涨跌% futu vs finnhub | 109 | **0** | **0%** | -0.00% | 0.0% |
| I7b 当日涨跌% futu vs TwelveData | 15 | 0 | 0% | -0.00% | 0.0% |

注：I7 可比 n=109（非 112）是**工具侧近零保护**所致：双方数值均 <0.05pp 时相对差
不具意义，计为不可比而非冲突（例：HON 两值同为 -0.0145% 量级）。
**I1/I1b/I2/I7/I7b 全项零冲突。**

`futu 共识回填 73/112`（其余 39 只本轮回填失败，为接口限速所致，非"无数据"）→
共识类指标 n=73，其余（昨收/涨跌）n=112 为全量。

## §3 分歧仲裁：PE 的离群源是 finnhub

对 PE 分歧最大的 4 只取第三方（AlphaVantage，独立于 futu/finnhub）：

| 标的 | futu | finnhub | AlphaVantage | 判定 |
| --- | --- | --- | --- | --- |
| US.IBKR | 35.99 | **136.62** | 35.99 | finnhub 离群（~3.8×）|
| US.GS | 14.55 | 13.08 | 14.55 | futu=AV，finnhub 偏低 11% |
| US.FPS | 125.21 | 146.70 | 127.23 | futu≈AV，finnhub 偏高 17% |
| US.CRWD | 6253.95 | 4162.47 | 4753 | 三者互不相同（近零 EPS 使 PE 本身无意义）|

**结论：futu 与 AlphaVantage 在 3/4 例上一致，finnhub `peTTM` 是离群源**；分歧来自
口径（finnhub 侧另有 `peNormalizedAnnual` / `peExclExtraAnnual` 等归一化口径）。

## §4 决策暴露：冲突是"条件性"的，不是稳态的

代码事实（`app/data/providers/fundamental_provider.py`）：

- `get_data()` 是**显式降级链**：`Futu(主) → Finnhub → Yahoo → AlphaVantage`，
  命中即 return，并写 `financials.source` / `data_source` 标注来源。
- 今日 futu snapshot 112/112 命中 → **稳态下基本面 PE 实际由 futu 提供**，
  finnhub 的离群 PE 只在 **futu 失败时**才进入。

所以上一轮报告"没有'单指标单一权威源'约束"**部分不成立**：优先级链存在，且稳态即
单一权威源。真正剩下的风险是**降级时的静默口径切换**：一旦落到 finnhub，
`pe_ttm` 的口径换了（`peNormalizedAnnual`）、数值可差 ~4×，而消费方
（`data_collector.py:282` 取 `pe_ttm/peTTM/pe_ratio/pe`）**不校验 `source`**。

## §5 结论（对上一轮报告的四点修正）

1. **昨收 / 当日涨跌：不存在冲突**（三源 + 同源跨路径，n=112/15/20 全部 0 差异）。
2. **PE(TTM)：真实冲突，13% 的标的跨源差 >10%（p90 14.7%），且离群方是 finnhub**；
   futu≈AlphaVantage。
3. **分析师共识：数值接近（中位差 2.1pp），方向标签 11%（8/73）不一致 —— 其中 6 例是
   60/40 阈值边界翻转（bull↔neutral），2 例为真方向对立（US.UMC、US.WEAV）**；
   后者的根因不是数据错误，而是**富途覆盖家数过小导致比例取极值**：
   UMC 富途 n=2（两家全 sell → buy 0%）vs finnhub n=33（61% 看多）；
   WEAV 富途 n=5（全 hold）vs finnhub n=10（70% 看多）；
   反例 NOK：富途 n=7 → buy 100%（极端）vs finnhub n=35 → 57%（neutral）。
   即 **buy% 这类"比例型"指标在小样本下不可比**，真正的跨源差别是覆盖家数（0.50x）。
4. **"成本重复"成立但价值中性**：两源各自服务不同管线（playbook 走 futu 主源、
   DA 走 finnhub 深析），重复调用换来的是一致性风险而非浪费。

## §6 局限

- 单日快照（周日，非交易时段）：昨收/涨跌类一致性的最强检验窗口是**盘前**，本轮未覆盖。
- TwelveData(15)/AlphaVantage(10) 子集受免费额度限制，样本小；AV 目标价仅 n=9。
- 共识类 n=73（39 只限速未回填）。
- 富途共识 `buy%` 与 finnhub"分档家数占比"**口径本就不同**（前者按分析师人数占比、
  后者为分档计数）→ I4 的差值含口径成分，不能全归为"数据不一致"。

## §7 复现

```bash
# 本批分析 (不发外部请求, 用已落缓存)
PYTHONPATH=. venv/bin/python scripts/multisource_consistency_sample.py --from-cache
# 重新采集 (需 OpenD 在线; 会消耗 finnhub/TwelveData/AlphaVantage 免费额度)
PYTHONPATH=. venv/bin/python scripts/multisource_consistency_sample.py --run --limit 112 --td-limit 15 --av-limit 10
```

- 工具：`scripts/multisource_consistency_sample.py`（只读；冲突阈值写死在 `TOL`，禁止事后改）。
- 产物：`data/multisource_consistency_latest.json`（`data/*` 为运行时目录，不入库）。
- 缓存：`data/multisource_cache/ms_*.json`（断点续跑用）。
- **建议复测窗口：盘前时段**（昨收/涨跌类一致性的最强检验窗口，本批为周日休市快照）。