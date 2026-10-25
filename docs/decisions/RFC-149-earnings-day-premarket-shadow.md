# RFC-149 财报日盘前研究臂源影子 (2026-10-18)

**状态**: 已实施 (仅研究臂/SIM；严格臂与生产闸零改动)
**触发**: 用户问“盘前交易（基于财报日逻辑）逻辑和链路是否可行”；复核发现严格臂近期 0 单，
候选主要是全市场异动榜（仙股多、价格闸大量拦截），财报超预期榜/自选股源未启用，
且严格涨幅带 0.5%–2.0% 会挡掉财报大跳空。用户同意先做 20 个交易日影子观察。

**2026-10-18 复核修正**: ①旧 `research_momentum` 臂保持 10-31，新增 source 专用
`EXT_RESEARCH_SOURCE_UNTIL=2026-11-13`（10/19 起 20 个交易日），不把旧臂一起延长；
②研究源 `existing` 只排除 research_pool/严格臂已成功下单，不再用全部 movers 排除，
避免“已上异动榜但被严格带拒绝”的 beat/watchlist 票被漏掉。

## 1. 证据

- 最近 10 个交易日 `ext_entry.log`：候选 30–50/日，价格闸挡 16–42，
  动量/量闸再挡 4–26，`ordered=0`；源开关为 `rank=True beat=False watchlist=False
  calendar=True finnhub_calendar=True`。
- 10/14–10/16 无新开仓；state 显示历史成交集中在 9/17–10/13。
- 历史 60 日源回放 (2026-09-18)：beat 通过 5、watchlist 通过 51；rule 臂均值
  -0.245%、中位 +0.216%、合计 -13.7% ⇒ 只能算数据采集，不是已验证 edge。

## 2. 本批改动 (研究臂 only)

- `scripts/extended_hours_entry_executor.py`:
  - `EXT_ENTRY_RESEARCH_SOURCES=true` 时，研究臂追加 Futu `get_earnings_beat_rank`
    + 自选股源；**严格臂的 `_scan_candidates` 结果不变**。
  - 研究源独立涨幅带 `EXT_ENTRY_RESEARCH_CHG_BAND=0.5,10.0`，源头价
    `EXT_ENTRY_RESEARCH_MIN_PRICE=5`，最多 `EXT_ENTRY_RESEARCH_MAX_SYMBOLS=80`。
    （2026-10-18 实源探测：beat 源 14 只里 8 只在 0.5–2%、0 只在 2–10%
    ⇒ 2–10 会全挡掉，故取 0.5–10；严格臂仍 0.5–2.0。）
  - **旧 `research_momentum` 臂保持 `EXT_RESEARCH_ARM_UNTIL=2026-10-31`**；
    新 source 用独立死线 `EXT_RESEARCH_SOURCE_UNTIL=2026-11-13`
    （2026-10-19 起第 20 个交易日；11/26 感恩节不计，11/11 美股正常交易）。
  - 研究源 `existing` 只排除**已入 research_pool 或严格臂已成功下单**的标的，
    不再排除全部 `movers` —— 这样“已在异动榜但被严格 0.5–2% 带拒绝”的
    beat/watchlist 票可以进入研究源 0.5–10% 带，覆盖不被削弱。
  - 源级漏斗 `data/ext_entry_research_funnel.jsonl`：selected/reject/ordered，
    带 source、stage、reason、price/change/volume。
  - 研究臂真单通道新增 held / attempt 复用检查，避免重复研究单。
  - 结果/attempt/earnings stub 带 `research_source`；计数键
    `research_src_<source>_pool|ordered` 由既有研究臂日志输出。
- `.env.us`: `EXT_ENTRY_RESEARCH_SOURCES=true`、独立带/价/上限；
  `EXT_RESEARCH_ARM_UNTIL` 10-31 → 11-30（覆盖 20 个交易日窗口）。
- 严格臂源开关、`EXT_ENTRY_CHG_BAND=0.5,2.0`、价格/量/财报/流动性/结构闸全部不动。

## 3. 观察与判据

20 个交易日，按 source（research_beat / research_watchlist / rank / calendar）
看：selected → reject(reason) → ordered → 成交 → 净 PnL/MAE/MFE。
预注册裁决前不得改严格臂；若研究臂仍无成交或净 PnL 为负，按“无 edge”关闭研究源，
不改严格臂。任何转正须另立 DEB。

**2026-10-25 判据补强 (RFC-167 §6, 单笔主导挡板)**: 到期裁决必须**同时**给三行读数 ——
①全样本 ②**剔最大单笔** ③分 source；任一行不过线即按 NO_EDGE 处置，不得用全样本单行裁决。
实证: 截至 10-25 研究源臂 10 笔 +$94.64 看似正 edge，而最大单笔 PEP +$106.64 占 113%，
剔后 9 笔 **−$12.00** —— 单行读数会把这 1 笔当成"可转正"的证据。
(旧 `research_momentum` 臂 6 周 0 笔，10-31 到期自动失效，无需动作。)

## 4. 回滚

- `EXT_ENTRY_RESEARCH_SOURCES=false`（下一 tick 生效，无需重启）；
- 或 `EXT_ENTRY_RESEARCH_MOMENTUM=false`（整条研究臂关）；
- 代码单文件 revert；funnel/jsonl 留档。
