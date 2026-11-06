# RFC-186 · 板块快照缺失不再伪装 0 涨跌（输入保真 / 报表口径）

- 日期: 2026-11-06
- 类型: L1 缺陷修复（输入保真 + 报表口径），**零交易行为变更**：不改任何阈值/门禁/仓位/方向判定
- 触发: 2026-11-06 09:00 ET 审计今日 DA 产出时发现「最强/最弱板块整体反转」
- 状态: 已落地（skill 仓 `ac6f92c` + 本仓本提交）；P0-2 / P0-4 见 §6「未做，已排期」

---

## 1. 事实（全部当日实测）

| 项 | 值 | 来源 |
|---|---|---|
| prompt 断言 | `板块表现均衡，最强Technology(+0.0%)`；strongest=Technology/Financials/Healthcare 全为 `+0.00%` | `data/da_checkpoint/2026-11-06/US.AMZN.raw.json` 的 user_prompt；`da_pipeline_stderr.log` |
| 真实值（同公式 `last vs prev_close`） | XLK **−8.00%**（176.50→162.38）、XLI −4.12%、XLY −3.83%、XLB −2.91%、XLF −2.87%、XLV −2.50%、XLRE −1.82%；仅 XLE/XLU/XLP/XLC 与文件一致 | Futu 快照 2026-11-06 08:42 ET 逐只复算 |
| 结论方向 | 文件 `strongest` 与真实 `weakest` 几乎互换：把昨**最弱**板块写成**最强** | 同上 |
| 代码根因 | `market_scanner.py:590` `pct = ((_last-_prev)/_prev)*100 if _prev and _prev>0 else 0` → 缺失写 **0.00**；`:595` `sorted(...)` 让 0.00 参与排序 | 源码 |
| 传播面 | ①DA prompt（30/36 checkpoint 提及板块）②UE prompt（`llm_universe_scan.py:1383`）③`data/deep_sector_rotation.json` | grep |
| 已发生的污染 | META 挑战者原文：「板块上下文显示 Communication(−0.59%) 今日弱于 Technology(+0.00%)」 | `US.META.json` |
| 附带口径错误 | 该行标签写"当前快照板块ETF涨跌"，实际是 last vs 前收 = **上一交易日日涨跌**（今日盘前 XLK 实为 +2.42%） | 同上 + 08:42 快照 |
| 弃权率口径 | `da_pipeline_stderr` 报「挑战者弃权率 50%(18/36)」；checkpoint 实测 AGREE 5 / PARTIAL 30 / DISAGREE 1 ⇒ **真弃权 0**（旧判据把分析师自己 SKIP 计入） | checkpoint 聚合 |

**为什么它不是"陈旧"问题**：文件 date 是今日、`generated_at` 08:35，新鲜；错在**缺失被写成零值并参与排序**。

---

## 2. 改动（两仓，共 4 处）

### skill 仓 `/root/.hermes/skills/deep-stock-analysis`（commit `ac6f92c`）

1. `scripts/market_scanner.py::_collect_sector_data`
   - 缺失（`_error` / `prev<=0` / `last<=0`）→ 记入 `missing_sectors`，**不入** `sector_changes`
   - 覆盖率 `<0.7` 或有效板块 `<3` → `strongest/weakest` 置空（名次只允许由真实涨跌产生）
   - 返回值新增 `missing_sectors` / `coverage`
2. `scripts/market_scanner.py::_sector_narrative`
   - 有缺失 → 输出 `板块快照不完整(n/m 缺失: …)，不据此判断轮动。`；不再用剩余/零值讲"最强/均衡"
3. `scripts/pipeline.py::build_sector_context`
   - 标签改为 `板块ETF涨跌(口径: last_price vs 前收 = 上一交易日日涨跌, 非今日盘前)`
   - 有缺失 → 追加 `⚠️ 板块快照不完整 x/y, 缺失: … — 缺失板块不参与强弱判断`
4. `scripts/pipeline.py` 弃权监控
   - 保留 `challenger_abstain_rate`（原口径，下游有读取方）**不动**
   - 新增 `challenger_abstain_rate_true`，日志同屏打印 `任一方SKIP口径 x% | 挑战者真弃权 y%`
   - ⚠️ 关于 `results["challenger_abstain_rate"]` 语义：登记为**已知口径缺陷但暂不改**（避免一次性改变下游对比基线），真值走新键。

### 本仓 `FTSeek-US`（本提交）

5. `scripts/llm_universe_scan.py::_read_sector_rotation`（主源分支）
   - 有 `missing_sectors` → `strong_sectors/weak_sectors` 置空 + `sector_note` 显式声明不完整
   - 无缺失 → 行为与之前逐字一致

---

## 3. 验证（实跑，含变异验证）

| 验证 | 结果 |
|---|---|
| 假 collector：XLK `prev=0` | 新码：Technology 不入表、不成 strongest、进 `missing_sectors`；覆盖率 0.18 → `strongest=[]`；narrative 含"不完整"且不含"最强" |
| 同一输入跑 **改动前** 代码 | `sector_changes['Technology']=0`、`strongest=['Technology','Utilities','Energy']`、narrative 为 `板块表现均衡，最强Technology(+0.0%)` ⇒ **3 项断言在旧码上全红**（变异验证通过：断言能咬住缺陷） |
| `build_sector_context` 直调 | 缺失场景：`⚠️ 板块快照不完整 2/5, 缺失: …— 缺失板块不参与强弱判断`；完整场景：无告警、仍出名次 |
| skill 测试集 | 130 passed / 1 skipped；2 项失败为**既有环境问题**（`test_prompt_source_truth::test_prev_close_apply_*` 断言、`test_fresh_event_origin` futu protobuf import），涉及文件 `llm_synth.py` / `fresh_event_origin` 本次**未改动** |
| 静态 | 三个改动文件 `py_compile` 全通过 |
| 数据修复 | 08:59 ET 按**生产者同公式**重算 `data/deep_sector_rotation.json`（11/11 有效），`strongest=Communication/Utilities/Consumer Staples`、`weakest=Consumer Discretionary/Industrials/Technology`；原文件留 `.bak-20261106-0856`，新增 `_repair` 字段记口径与替换值 |

**生效面**：skill 文件由 cron 每次起新进程读取 ⇒ 明早 DA 自动生效；本仓 `llm_universe_scan.py` 由 cron 同上。**无需重启 ftseek-us**（本批不含 `app/` 代码）。

---

## 4. 回滚

- skill 仓：`scripts/*.bak-rfc186-20261106` 覆盖（HEAD 整文件）；或 `git revert ac6f92c`
- 本仓：`git revert <本提交>`
- 数据：`cp data/deep_sector_rotation.json.bak-20261106-0856 data/deep_sector_rotation.json`
- 无配置键、无 DB schema、无 cron 行变更。

---

## 5. 明确不做（防复活）

1. 不建"板块最强/最弱 → 方向或仓位门"。价格动能类轴已在 DEB-20260924 §三.6（71 交易日 / 789 条 LONG）关闭；DEB-20261106 轴 A/B 复测无判别力（≤−0.5% 日聚类 CI95 含 0）；P3 `market_premise` 当日 v3 仍 REJECTED（常态误伤 3/31 = 9.7% > 5%）。
2. 不改 `regime` 的方向语义：`risk_off → ×0.3` 的仓位联动已在跑（11-06 每笔 410→123），方向否决无证据。
3. 不动 `min_notional` / 交易参数（RIVN/INTT 被费用门拒是正确行为）。
4. 不启用 `regime_classifier`（零实现）。

---

## 6. 未做，已排期（同批审计发现，需独立验证/重启）

| 项 | 证据（2026-11-06） | 为什么不当场做 | 计划 |
|---|---|---|---|
| **P0-2 生成时 admission 预览** | 20 条全 `admission.verdict=PASS` 但 `shares=0/notional=0`（生成时无仓位 ⇒ 判据空转）；按 `risk_off×0.3`（risk 123/笔）实算，**RIVN(≈1850)、INTT(≈2440) 会撞 `min_notional 2500`** | 需要 provisional sizing 接入点，且 DA 今日已跑完，无当日收益 | 收盘后实现 record-only `admission_preview`，次日核对与执行端一致率 |
| **P0-4 bridge/adapter 时序** | 服务 08:13:50 与 08:18:09 两次启动均报 `bridge文件不存在 → TechnicalFallbackAdapter`，DA 08:35 才写 bridge；`voting_coordinator` 一旦 fallback 直到下次重启不重试。盘前预检却报「DA bridge 就绪 20 候选」（读文件）⇒ 进程态与观测面不一致 | 改 `app/core/scheduler_core.py`，需重启才生效；盘中重启违纪律 | 收盘后改为每轮惰性重解析 + 每轮 `adapter=` 遥测，收盘后重启 |

另：今日 `supply_placeholder` = 0 条（10-28 起每日 47–61）。**未定因**，列为观察项，不在本批处理；排查顺序：写占位行的 pipeline 轮是否运行 → 是否被 DA 主链改造后停写（设计变更 vs 回归）。