# DEB-20261109 · 11-09 交易日复查：账面 vs 结构（含多 agent 交叉复核）

- 状态：**待裁**（本文只做只读复查 + 落地方案；**未改任何生产路径**：无 `app/` 改动、无配置改动、无 DB 写入、无重启、无 cron 变更、无 notifier 调用）
- 触发：用户要求「根据昨日美股交易过程和逻辑复查分析，是否有值得优化调整的问题点」（2026-11-10 CST，复盘对象 = 2026-11-09 ET 周一）
- 复核方式：**只读复算 + 双厂商 agent 交叉复核 + 可复现脚本**
  - 数据复核：GLM-5.3（`glm-verifier`）独立重算 10 组断言
  - 代码/配置复核：Kimi-K3.1-Pro（`kimi-reviewer`）对抗性核查 8 组断言
  - 产出尺子：`scripts/session_attribution.py`（只读，DB `mode=ro`，零写入）
    → `venv/bin/python scripts/session_attribution.py --date 2026-11-09`
- 上游：`DEB-20261106-market-risk-switch-review.md` §44/§47/§49/§53/§54/§55、`RFC-192`（v2.1 勘误态）、`RFC-191`
- 口径依据：`scripts/ticket_size_breakeven.py`（费用 SSOT）、`app/execution/fee_model.py`（费率模型）、`app/storage/trade_store.py`（写入守卫）

---

## 零、协作声明（与并行 DEB 的分工，必读）

同一天另有 **并行 agent 的 `DEB-20261110-da-postclose-chain-review.md`**（11-09 收盘后 DA 全链复盘），
其 §3 的 **P0-A（日报计划配对错通道，执行率 40% 虚增为 45%）** 与 **P0-B（已实现盈利掩盖未平仓风险）**
与本文 **P1 同源**。两边**独立复算得到同一组数字**（DA 已平 +$34.11 与三仓毛浮亏 −$208.48 并存），
互为交叉验证。

| 项 | DEB-20261110（并行） | 本文 |
|---|---|---|
| P1 / P0-B 口径与未平风险 | **已在实施**（`app/review/bundle_builder.py` +370 行 / `daily_review.py` +64 行，加 MTM 分栏） | **只复算与交叉验证，不重复实现** |
| 执行率 40% vs 45% | 已定位为日报配对错通道 | 本文引用其结论（复盘 md 的 45.0% 为虚增值） |
| P2 过夜仓无 TP + `exit_price=0` 卫生 | 未覆盖 | **本文增量** |
| P3 `be_exit_class` 归因层缺口 | 未覆盖 | **本文增量** |
| P4 闸门拦截代价 + vwap A/B 污染 | 未覆盖 | **本文增量** |
| P5 跨臂方向冲突 + EXT 配额漂移 | 未覆盖 | **本文增量** |
| P6 同标的平后立即重入 | 未覆盖 | **本文增量** |

⇒ **纪律**：`app/review/*` 当前有在途 WIP（DEB-20261110 的实施轮），本文 **Batch 0.2 / 0.3 不并行改该目录**，
改为"先在只读尺子侧出数（已完成）→ 待 DEB-20261110 收口后合并归因层改造"。
其余各批落点（`scripts/extended_hours_entry_executor.py`、`.env.us`、`scripts/session_attribution.py`、
新影子脚本）与在途 WIP 无重叠。

---

## 一、一句话结论

**11-09 的"漂亮"来自结构而不是质量**：+$312.55 里，EXT 研究臂占 +$203.73、3~4 天前开的遗留仓占 +$74.71，
DA 主线自身已平只有 +$34.11，且当天开仓未平的 3 笔按盘后水位是 **−$208.48** ⇒ **主管线当天实际是亏的（≈ −$174）**。
真正值得优化的问题不是"哪笔亏了"，而是 **4 个结构性缺口**：① 过夜仓无目标价 + 半修的数据卫生；
② 出场"漏损"已有标签（`be_exit_class`）但归因层没用；③ 闸门拦截代价从未被算过、且 vwap 门的 A/B 已被下游污染；
④ EXT 旁路臂已成主导臂却不受 DA 方向约束，且其"为盘后预留额度"的配置已失效。

---

## 二、口径声明：三个"当日收益"都不是错的，混用才错

| 口径 | n | 净额 | 产出物 |
|---|---|---|---|
| 按**开仓日**=11-09，已平 | 14 | **+$237.84** | DB 直查 |
| 按**出场日**=11-09，已平 | 19 | **+$312.55** | 盘后日报 `obsidian_vault/daily/postmarket/2026-11-09_us_postmarket.md` |
| 按**出场日**，**生成时点快照** | 18 | **+$287.82** | 每日复盘 `obsidian_vault/daily/review/2026-11-09_review.md` |

第三个数为什么少 $24.73：该 md 生成于 **2026-11-10 06:35 CST = 11-09 17:35 ET**，而 `US.QCOM` 的出场是
**11-09 19:20 ET** ⇒ 生成时它还在"未平研究仓"里。**不是口径错，是快照早于事实**。

⇒ **教训（可落档）**：任何"当日读数"必须带 **生成时点**；生成后新发生的出场不会回写旧 md。
日报与复盘必须共用一个聚合函数（落点见 §六 Batch 0.3），且必须**同时输出三口径 + 未平浮盈**。

---

## 三、当日事实（分层）

### 3.1 按来源拆

| 来源 | 笔数 | 已平 | 净额 |
|---|---|---|---|
| `extended_hours_entry`（EXT 研究臂：盘前 8 + 盘后 2） | 10 | 9 | **+$203.73** |
| `playbook_sim`（DA 主线） | 8 | 5 | **+$34.11** |
| 跨日平掉的遗留仓（MCD/GOOG/PLTR/MRVL/AVGO） | 5 | 5 | **+$74.71** |
| 当日开仓未平（BA/GOOG/CAT，按盘后水位） | 3 | — | **−$208.48** |

⇒ 已实现口径里 **EXT 臂占 65%**；把未平浮盈计入后，**DA 主线当日 ≈ −$174，账面 ≈ +$85**。

### 3.2 跨日遗留仓的开仓日（GLM 更正）

| 标的 | 开仓 | 出场 | 净额 | 来源 |
|---|---|---|---|---|
| MCD | 11-05 08:42 | 11-09 12:36 | +0.24 | EXT |
| GOOG | 11-06 10:03 | 11-09 06:23 | +8.99 | DA 主线 |
| PLTR | 11-06 16:27 | 11-09 04:06 | +7.81 | EXT |
| MRVL | 11-06 16:27 | 11-09 07:53 | +39.45 | EXT |
| AVGO | 11-06 16:42 | 11-09 04:10 | +18.22 | EXT |

⇒ 是 **3~4 天前**开的仓，不是 11-08；4 笔在 11-09 早盘（04:06–07:53 ET）被 EXT 出场规则收掉。

### 3.3 出场归因：标签混层，但仓内已有分层字段

| exit_reason | n | 亏笔% | 净额 | 每笔 | 最差 |
|---|---|---|---|---|---|
| SL_BREAKEVEN | 314 | **23.6%** | +$1,209 | +$3.85 | **−$314.27** |
| SL_TRAIL_LOCK | 206 | 4.9% | **+$9,074** | +$44.05 | −$49.30 |
| sl_hit | 115 | 88.7% | −$3,702 | −$32.19 | −$211.09 |
| PRE_CLOSE_EXIT | 87 | 43.7% | **+$8,123** | +$93.36 | −$5,069 |
| TRAILING_STOP | 87 | 62.1% | −$5,177 | −$59.51 | −$7,408 |
| DEAD_SIGNAL_EXIT | 55 | 47.3% | −$223 | −$4.06 | −$118.42 |
| TIME_EXIT | 50 | 88.0% | −$2,287 | −$45.75 | −$633 |
| SL_INITIAL | 42 | 100% | −$6,040 | −$144 | −$309 |
| STRUCTURE_EXIT | 33 | 69.7% | −$3,305 | −$100 | −$1,321 |
| MFE_STALL_EXIT | 31 | 100% | −$1,353 | −$43.63 | −$99.52 |
| EOD_FORCE_CLOSE | 14 | 14.3% | +$1,725 | +$123.21 | −$107.51 |
| **OVERNIGHT_GAP_STOP** | 14 | **100%** | **−$4,531** | **−$323.61** | −$438.00 |

**BE 出场内部（`metadata.be_exit_class`，仓内早已存在，Kimi 指出）**：

| be_exit_class | n | 净额 | 每笔 | 亏笔% |
|---|---|---|---|---|
| OK | 225 | **+$2,217** | +$9.85 | 0% |
| GAP | 16 | −$454 | −$28.36 | 100% |
| SLIP | 40 | −$203 | −$5.06 | 100% |

⇒ **"保本出场"里有 20%（56/281）其实是被打穿或滑点**，合计 −$656。
`US.MRNA`（11-09，entry 258.183 → exit 256.01，−$45.49）属 `GAP`：
BE 线 258.92 被价格穿透（decision_price 257.875），限价单未成交后**降级市价**成交（`exit_slippage_bps=72.32`，
`exit_price_source=broker_weighted_deal`，`floor_enforced=false`）。

### 3.4 闸门拦截（DA 指令 22 条 → 执行 8 条）

| 闸门 | 笔数 | 标的 | 阈值/参数 |
|---|---|---|---|
| falsification_slope | 4 | AMZN/APP/COIN/TSLA | tol 0.001（严格 `<` / `>`，Kimi 更正：不是边界 bug） |
| vwap_gate | 3 | ABNB(−0.30%)/IMVT(−0.31%)/ARM(+2.49%) | 容差 **0.003**；判据是**当前价 vs 当日 VWAP** 的逆侧偏离 |
| stale_quote | 2 | BABA(33.0s)/HON(44.8s) | reprice 门 **30,000ms**（另有 pretrade 60s / entry 90s 两档） |
| chase_cap | 2 | GS(+2.06%)/NEM(+1.43%) | DA 自带 cap |
| admission 价格地板 | 2 | IBRX $6.60 / NVCR $13.25 | 费用地板 17.33bps |
| trigger_no_execution | 1 | LMT | 资金护栏/时段/执行失败 |

`data/shadow/exec_gate_audit.jsonl` 当日 **307 行**：`skipped_max_positions` 150、`skipped_chase_cap` 127、
`skipped_stale_quote` 13、`deferred_entry_confirm` 9、`executed` 4、`submitted_unconfirmed` 4
⇒ **拦截代价的数据源齐了**（含 entry/notional/chase_cap_price/knife_gate 字段），只是从未被算。

### 3.5 跨臂方向冲突

| 标的 | DA 方向 | DA 是否执行 | EXT 方向 | EXT 入场 |
|---|---|---|---|---|
| **US.TSLA** | **SELL_SHORT** | 否（`falsification_slope_blocked(slope=+0.0014)`） | **LONG** | 11-09 07:37 ET |

Kimi 核实：EXT 方向**只由自身跳空规则决定**，`decision_id` 自造 `EXT-*`，只防"同标的已有仓"，
**不读 DA 方向、不防相反暴露**。

---

## 四、问题点（按证据强度排序）

### P1（高）当日盈亏结构与 DA 主线的真实水位

- 现象：日报 +$312.55 / 胜率 94.7%，但 DA 主线已平 +$34.11、未平 −$208.48 ⇒ 当日 ≈ −$174。
- 为什么值得管：一旦按"当日已实现"做复盘/汇报，会把 EXT 研究臂与遗留仓的利润记成主管线的功劳，
  并让 3 笔大亏不进分母。**这是口径问题，不是策略问题，但会直接误导后续所有裁决**。

### P2（高）过夜敞口：EXT 仓无目标价 + 数据卫生修了一半

- `US.CVS`（11-09 17:17 ET 盘后开，58 股 @85.285）：`open_positions.initial_tp = NULL`（只有 SL 81.02）、
  `sync_status=pending_sync`、`trade_records.exit_price=0.0`、`exit_time=NULL`、无出场记录。
- **公允更正（Kimi + 我自己复核）**：EXT 隔夜**是设计内的**，且历史盈利 ——
  EXT 50 笔成交里 12 笔跨日（24%），净 **+$272.24**（+$22.69/笔），赢家 10/12，最差 ALB −$108.30。
  ⇒ **不是"禁止隔夜"，而是"EXT 仓必须有 TP，且坏数据必须修"**。
- `exit_price=0.0` 的来源已查清：EXT 开仓 INSERT **硬编码 `exit_price = 0, exit_time = NULL`**
  （`scripts/extended_hours_entry_executor.py:3461-3469`）；`app/storage/trade_store.py:684-700` 的
  `exit_price ≤ 0` 守卫**只拦已平仓行**（`exit_reason` 非空），开仓行是允许的。
  ⇒ 这是 **10/19 已修过一半的同类问题**（那次修了 `exit_time=''` → NULL，注释在 3466-3468），
  **还差 `exit_price` 0 → NULL**。任何不过滤 `exit_time IS NULL` 的聚合都会拿到假价 0。
- 尾部代价：`OVERNIGHT_GAP_STOP` 14 次全亏、每笔 −$323.61（远超常规止损 $100~250）⇒ **跳空击穿止损，止损单在这里挡不住**。
  DEB §47 已认定隔夜缺口是真正出血点。

### P3（中高）"保本出场"的 20% 漏损已经**有标签**，但归因层没在用

- `be_exit_class` 已存在（OK/GAP/SLIP），分布与代价已量化（上表）。
- 问题在**聚合层**：日报/复盘按 `exit_reason` 分组 ⇒ "SL_BREAKEVEN +$1,209" 把 OK 与 GAP/SLIP 混在一起，
  会得出"保本出场很稳"的错觉。
- 机理链（LONG）：BE 线 = `entry × (1 + fee_cushion 0.15% + slip_cushion 0.2%)`（`app/playbook/exit_engine.py:212-218,448-459`，
  `breakeven_fee_aware_enabled=true`）⇒ 线在**入场价之上**；价格穿透 → 限价未成交 → 降级市价 → 滑点 72bps。
- **字段已存在且已有消费者**：`be_exit_class` 在 `app/playbook/pipeline_parts/close_flow.py:343-359` 写入
  （分类实现 `app/playbook/pipeline_parts/exit_classification.py:114`，契约测试 `tests/test_be_exit_class.py`），
  **推送层已在用**（`app/push/message.py:134,276,653`）⇒ **缺口只在日报/复盘的归因表**。
- ⇒ **修法不是加字段（已有），是让归因层用它**；另可给 `GAP/SLIP` 单独设影子告警。

### P4（中）闸门拦截代价从未算过；vwap 门的 A/B 已被下游污染

- 拦截数据有（307 行/日），代价**从未算**。RFC-185 §5 当初写 `exec_gate_audit(skipped_stale_quote)` 就是为了这个。
- **vwap 门在 SIM 本就 50/50 A/B 分流**（`vwap_gate_ab_enabled=true`，`app/playbook/pipeline.py:390-470`，
  台账 `data/vwap_gate_ab.jsonl`）。11-09 该台账 12 行：
  - `bypass` 臂 5 个：TSLA / COIN / CAT（空）、AMZN / BABA（多）
  - `gated` 臂 7 个：ARM / UNH / MRK / GOOG / NEM / ABNB / IMVT
  - **bypass 臂 5 个里有 4 个（TSLA/COIN/AMZN/BABA）被别的门二次拦下** ⇒ 双臂非同权，
    **vwap 门的边际效应在结构上就测不出来**。这一点代码注释自己承认（pipeline.py:400-410）。
- ⇒ 优先动作不是调阈值，而是**先修 A/B 的可比性**（把"被下游门二次拦截"标出来，分层统计）。

### P5（中）EXT 旁路臂已成主导臂，却不受 DA 方向约束 + 配额配置漂移

- 11-09：EXT 10 笔 / 名义约 $50k，DA 主线 8 笔 / 约 $30.6k；EXT 占已实现 65%。
- 但：① 方向与 DA 无关联（TSLA 冲突，§3.5）；② 入场 **14/19 是 chase 追价型**，而 DEB §44.2 已证
  "逆大跳空组 −$1,108"是最致命的单一出血点；③ EXT 买 `US.GOOG` 时盘前 **+2.49%**（大跳空），
  靠 +$19.44 侥幸盈利 —— 赢是运气，不是规则。
- 配置漂移（Kimi 核实）：`.env.us` 的 `EXT_ENTRY_MAX_PER_DAY=10` 与 `EXT_ENTRY_PREMARKET_MAX_PER_DAY=10`
  ⇒ 执行器注释里"盘后保留至少 1 笔启动额度"的意图**已完全失效**（注释 75-78 行也承认共享总额）。

### P6（中）同标的连续入场无冷却

- `US.GOOG`：11-06 10:03 开仓 → 11-09 06:23 `SL_BREAKEVEN` 平（+$8.99）→ **11-09 10:01 立即再开** → 现浮亏 **−$80.73（−2.23%）**。
- 仓内"同标的已有仓"只防**同时**持有，不防**平后立即重入**；DEB §30.3 记录的"上一笔亏了就不再开"是被证伪过的假设，
  但**没有冷却**与本条不同（这里是"刚保本离场又立刻反向情绪重入"）。

---

## 五、被推翻的初判（自我更正记录，必读）

本次复查中，我自己的三条初判被交叉复核推翻，现已更正（留档防后人重踩）：

| # | 初判 | 复核结论 | 现状 |
|---|---|---|---|
| 1 | "`exit_reason=SL_BREAKEVEN` 标签不可信 ⇒ 需拆字段" | **错**：`be_exit_class=OK/GAP/SLIP` 早已存在（`app/playbook/pipeline_parts/close_flow.py:343-359`、`app/playbook/pipeline_parts/exit_classification.py:114`），MRNA 的行明确标 `GAP` + 滑点 72.32bps。**不需要新列**，要改的是归因层 | 已改为 P3（用现有字段分层） |
| 2 | "vwap 门 = 拦价格朝不利方向移动" | **不准确**：判据是**当前价 vs 当日 VWAP** 的逆侧偏离（LONG `dev<−tol` / SHORT `dev>tol`，严格不等号），与"移动方向"无关 | 已在 §3.4 更正 |
| 3 | "falsification slope 的 AMZN 是 `>=` 边界 bug" | **错**：实现是严格 `<` / `>`；日志只保留 4 位小数（−0.0010），实际可小于 −0.001 | 已撤回该条 |
| 4 | "3 个当日收益口径互相矛盾" | **部分错**：+$287.82 与 +$312.55 是**同一出场日口径的生成时点差**（复盘 md 生成于 17:35 ET，QCOM 19:20 ET 才平），不是口径错 | 已改为"快照时点"问题（§二） |
| 5 | "CVS 是无主的裸奔仓" | **过重**：EXT 隔夜是设计内且历史盈利（12 笔跨日 +$272、10/12 赢）；真实缺陷窄化为**无 TP + 半修的数据卫生** | 已改为 P2（公允版） |

---

## 六、落地方案（三批 · 一次一变量 · 每批可回滚）

### Batch 0 — 零行为变更（只读/记账/文档；本批即可落，无重启）

| # | 动作 | 落点 | 验收 |
|---|---|---|---|
| 0.1 | EXT 开仓行 `exit_price` 由 `0` 改写 **`NULL`**（与 10/19 已修的 `exit_time` 同类，同一收口点） | `scripts/extended_hours_entry_executor.py:3461-3469`；幂等 ALTER 惯例见 `app/storage/trade_store.py:218-239` | 写后 `SELECT ... WHERE exit_price=0 AND exit_time IS NULL` 计数归零；所有按 exit_price 的聚合加 `exit_time IS NULL` 过滤 |
| 0.2 | **收益口径读数（只读侧先行；归因层改造待 DEB-20261110 收口后合并）**：`app/review/*` 有在途 WIP（见 §零），本文**不并行改该目录**，只在尺子侧出三口径 + 未平浮盈 + 按臂分账 | `scripts/session_attribution.py`（已完成） | 两份 md 的数字与 `--json` 逐位一致（待其收口后验） |
| 0.3 | 归因表增加 `be_exit_class` 分层列（OK/GAP/SLIP）—— 字段与推送层已就绪，只差日报/复盘渲染 | 日报/复盘渲染层（**待 DEB-20261110 收口后合并**） | 能直接读出"BE 出场 20% 非真保本" |
| 0.4 | **闸门拦截代价影子**：`exec_gate_audit` × bar 重放，算 `skipped_*` 的反事实 | 扩展既有 `scripts/gate_block_counterfactual.py`（JOIN 模式见其 `:59-109`；bar 源走 `app/risk/data_health_guard.py`） | 连续 20 个交易日读数；**不改任何拦截行为** |
| 0.5 | vwap A/B 可比性修复（影子）：给 `data/vwap_gate_ab.jsonl` 增加"是否被下游门二次拦截"标记 | `app/playbook/pipeline.py:390-470` 落点处（只加字段，不改分流） | 能分出"真 bypass"与"假 bypass" |
| 0.6 | 只读体检脚本：**无 TP 跨夜仓** + **同标的平后立即重入** + **跨臂相反暴露** | `scripts/session_attribution.py` 增段（不改生产） | 每日出报告，无行为影响 |
| 0.7 | 11-10 开盘前：**CVS 处置**（补 TP 或按既有 EXT 规则收掉） | 人工 + `scripts/extended_hours_exit_cron.py`（每分钟巡检，ET 04:00-20:05） | 11-10 早盘无"无 TP 跨夜仓" |

**Batch 0 纪律**：全部只读/记账；每项单独一个开关（可 env 关）；不改 DA/出场/止损/EXT 下单逻辑。

### Batch 1 — 配置与契约修正（行为可回滚，需重裁 + 用户签字）

| # | 动作 | 落点 | 判据 / 回滚 |
|---|---|---|---|
| 1.1 | EXT 配额恢复"为盘后预留"：令 `EXT_ENTRY_PREMARKET_MAX_PER_DAY < EXT_ENTRY_MAX_PER_DAY` | `.env.us:131-133`（env 面，每分钟 cron 运行时读取，**不需重启服务**） | 影子读数：盘后触发率与盘前占用分布；回滚 = 改回 10 |
| 1.2 | **EXT 仓强制落 TP**（含盘后单）；对"无 TP 且跨夜"的存量仓在次日盘前收口 | 下单处 `scripts/extended_hours_entry_executor.py` `_execute`；巡检挂 `scripts/extended_hours_exit_cron.py`（每分钟） | 影子先算"若强制 TP 会挡掉多少笔/挡掉的后来涨跌如何"；回滚 = 关开关 |
| 1.3 | **跨臂方向冲突**：EXT 开仓前读当日 DA 方向，方向相反则不执行（记录原因） | EXT 执行器已有 DB 只读连接（`:2132-2145`）+ `_execute` 已有"同标的已有仓"检查旁（`:3444-3449`）；DA 方向在 `playbook_instructions.direction`（当日、DA- 前缀） | 影子 20 日：冲突单数 + 若拦截的反事实；回滚 = 关开关 |

**Batch 1 纪律**：三件事**分别**预注册、分别上线、分别回滚；任一上线前先有 20 日影子读数。

### Batch 2 — 行为变更（须另立 RFC，不在本 DEB 内）

| # | 议题 | 前置 |
|---|---|---|
| 2.1 | **C2 跳空门**（RFC-192）：口径二选一 + 补 EXT 的 gap 采集 + 盘前口径重标阈值 | 见 §七 与 RFC-192 §10.2 |
| 2.2 | 同标的冷却（平后 N 分钟/当日不再入） | 0.6 的只读体检攒够样本 |
| 2.3 | BE 被打穿时的降级策略（限价未成交是否允许降级市价；BE 触发的滑点假设） | Batch 0.3 的 GAP/SLIP 分层读数 |

---

## 七、待裁（用户决定）

1. **C2 口径二选一（RFC-192 §10.2 的处置 (a)/(b)）**：
   - **盘前口径**（`pre_change_rate`）：可执行、8 笔 DA 成交**全覆盖**、EXT 缺 6/10；
     但阈值必须在盘前口径上重标（历史样本 148 笔）。
   - **开盘跳空口径**（与全部历史证据同源）：11-09 只覆盖 4/18（EXT 路径完全不写该字段）⇒ 需先补采集。
   - **11-09 的单日旁证**（n=1，不进判据）：盘前口径下保留 XOM/UNH（+$10.51）、
     挡掉 3 笔已平赢家（+$23.60）与 3 笔未平输家（−$208.48）⇒ **当日 DA 从 −$174 变 +$10.5，改进约 +$185**。
2. **Batch 0 是否全批**（建议全批：零行为变更、无重启、可逐项关）。
3. **Batch 1 的三项是否批**（建议：1.1 批（配置回滚最便宜）；1.2/1.3 先各攒 20 日影子）。
4. **EXT 旁路臂的定位**：它是"研究臂"还是"第二条主线"？现状是它贡献 65% 已实现利润且名义更大，
   但不受 DA 方向约束、无 TP、配额预留已失效 —— **这是本次复查里最需要一次明确裁决的治理问题**。

---

## 八、证据与复现

```bash
# 本次全部读数（只读，零写入）
venv/bin/python scripts/session_attribution.py --date 2026-11-09
venv/bin/python scripts/session_attribution.py --date 2026-11-09 --json
venv/bin/python scripts/gate_interception_daily.py --date 2026-11-09   # 闸门拦截逐笔审计
venv/bin/python scripts/gate_block_counterfactual.py                     # 既有拦截代价记分牌 (周更)
venv/bin/python scripts/rfc192_verification.py          # C2 口径与历史阈值表复核

# 交叉复核
# · 数据：GLM-5.3 (glm-verifier) 独立重算 10 组断言 → 8 OK / 2 处口径更正（见 §5）
# · 代码：Kimi-K3.1-Pro (kimi-reviewer) 对抗核查 8 组断言 → 5 成立 / 3 处表述被推翻（见 §5）
```

**本次未触碰**：生产代码路径、配置、DB、cron、notifier、券商连接。
**本次产出**：1 个只读脚本 + 本 DEB + 台账登记 + handoff 条目 + RFC-192 §10.13 指针。

## 九、落地记录（2026-11-10，用户裁「按建议优化」）

本节记录**已实际落地**的批次与验证结果；读数口径以 `scripts/session_attribution.py` 与
`scripts/gate_interception_daily.py` 为准。

### 9.1 批次结果

| # | 动作 | 状态 | 落点 | 验证 |
|---|---|---|---|---|
| 0.1 | EXT 开仓行 `exit_price` 0 → **NULL**（对齐规范写入方 `trade_store.py:823`） | ✅ 已落 | `scripts/extended_hours_entry_executor.py:3469` + 迁移脚本 `scripts/migrate_ext_open_exitprice_null.py` | 迁移 dry-run 命中 1 行（id=1828 US.CVS）→ 执行后**全库 `exit_price=0` 计数 = 0**；备份 `data/ftseek_us.db.bak-deb20261109-b01-20261110-193246`。消费方核查：全部查询已兼容 0/NULL（`exit_price>0` 过滤 / `COALESCE` / `IS NULL OR =0`），语义不变 |
| 0.2 | 收益口径只读读数 | ✅ 已落（只读侧） | `scripts/session_attribution.py` 段 A | 三口径 + 未平浮盈 + 按臂分账 + 跨日遗留 + 生成时点口径 |
| 0.3 | 归因表加 `be_exit_class` 分层 | ⏸ **待 DEB-20261110 收口后合并** | `app/review/*` 在途 WIP | 只读侧已带出分层（段 C）；渲染层等并行 DEB 收口 |
| 0.4 | 闸门拦截代价 + 贴边排行 | ✅ 已落（只读） | `scripts/gate_interception_daily.py` | 见 §9.2；与既有记分牌 `data/gate_block_counterfactual_latest.json` 对照 |
| 0.5 | vwap A/B 可比性 | ✅ 已落（只读四象限） | 同上 段 C | 11-09：**假 bypass 4 / bypass 臂 5 ⇒ 双臂被下游门污染，边际效应不可测**（量化确认） |
| 0.6 | 只读体检三件套 | ✅ 已落 | `scripts/session_attribution.py` 段 G | ① 无 TP 跨夜仓：**1（US.CVS）**；② 同标的平后立即重入：11-09 **4 次**（GOOG×3、PLTR×1），其中 3 盈 1 亏 ⇒ **"重入"本身未被 11-09 支持，候选变量应是"跨臂重入"**，继续观察；③ 跨臂相反暴露：段 E |
| 1.1 | EXT 配额恢复"盘后预留" | ✅ 已落 | `.env.us`（gitignored 部署面）`EXT_ENTRY_PREMARKET_MAX_PER_DAY` 10 → **9** | 依据：执行器注释的设计意图是"在合计上限内为盘后保留至少 1 笔"；10 = MAX 10 ⇒ 意图失效。历史 15 个交易日 EXT 盘前每日笔数 **max=8 / 均值 2.4** ⇒ 9 在观测样本内不触发 ⇒ **零行为变化 + 恢复安全边际**。env 面每分钟 cron 运行时读取，**无需重启**；回滚 = 改回 10 |
| 1.2/1.3 | EXT 影子（无 TP 跨夜 / 跨臂方向冲突） | ✅ 已落（record-only，**不拦单**） | `scripts/extended_hours_entry_executor.py::_ext_entry_risk_shadow`（写 `data/shadow/ext_entry_risk_shadow.jsonl`）；读数 `scripts/session_attribution.py` 段 H | 契约测试 `tests/test_ext_entry_risk_shadow.py` **6 passed**；盘前 dry-run **exit 0**、不写影子文件（符合预期）；开关 `EXT_ENTRY_RISK_SHADOW=false` 可关 |

### 9.2 读数更正：拦截代价**已存在**，且推翻了我的方向性假设

复查时我写了"闸门拦截代价从未被算过"——**不准确**。既有记分牌 `scripts/gate_block_counterfactual.py`
（cron：周六 06:50 CST）读 `data/trigger_miss_telemetry.jsonl`（30,781 行，11-09 占 1,018 行），
JOIN 指令前瞻收益，给出每门的"避坑/杀机会"裁决。本轮已跑并入档：

| 门 | n_inst | 裁决 | open_t1 均值 | 费后净 | 正率 |
|---|---|---|---|---|---|
| `vwap_gate_block` | 161 | **避坑✓** | −0.91% | −1.09% | 34% |
| `falsification_vwap_slope` | 169 | 中性 | +0.25% | +0.07% | 46% |
| `extreme_gap` | 41 | 避坑✓ | −0.97% | −1.15% | 49% |
| `runaway_up`（追价类） | 40 | **杀机会✗** | +0.60% | +0.42% | 59% |
| `runaway_down`（追价类） | 11 | **杀机会✗** | +1.32% | +1.14% | 45% |
| `low_atr_block` | 120 | 避坑✓ | +0.04% | −0.14% | 50% |
| `vol_gate_block` | 26 | 中性 | +0.20% | +0.02% | 44% |

⇒ **方向性更正**：我先前把注意力放在"贴边拦截"（11-09 的 vwap 两条 dev −0.30%/−0.31% 恰在容差线），
但历史读数显示 **vwap 门整体在避坑（被拦票平均 −1.09%）**，真正"杀机会"的是 **chase/追价类门
（`runaway_up/down`，被拦票 +0.42%/+1.14%）**。
⇒ **调阈值的优先级应反过来**：不是放松 vwap，而是审查追价类门的判定条件。

**11-09 贴边排行**（`scripts/gate_interception_daily.py` 段 B；比值 = 实际量/阈值，≤1.15 记贴边）：

| 标的 | 门 | 比值 | 原文 |
|---|---|---|---|
| US.ABNB | vwap_gate_blocked | **1.000** | `dev=-0.3%,逆BUY` |
| US.AMZN | falsification_slope_blocked | **1.000** | `slope=-0.0010` |
| US.NEM | chase_cap_enforced | 1.014 | `reprice=125.27,cap=123.5` |
| US.GS | chase_cap_enforced | 1.021 | `reprice=903.23,cap=885.0` |
| US.IMVT | vwap_gate_blocked | 1.033 | `dev=-0.31%,逆BUY` |
| US.BABA | stale_quote_gate | 1.101 | `age=33038ms > 30000ms` |
| US.HON | stale_quote_gate | 1.492 | `age=44764ms > 30000ms` |
| US.APP | falsification_slope_blocked | 3.100 | `slope=-0.0031` |
| US.ARM | vwap_gate_blocked | 8.300 | `dev=2.49%,逆SHORT` |

（比值基于 `skip_reason` 里**已四舍五入**的参数 ⇒ 1.000 = 恰在或刚过线。）

### 9.3 1.3 的覆盖缺口（实施中发现的硬约束，必须记档）

**DA 指令在 08:33:48 ET 批量生成**（11-09 实测：22 条 `created_at` 全在 13:33:48Z ± 0.16s），
而 **EXT 盘前入场集中在 07:30–08:20**（11-09 八笔：07:32/07:32/07:37/07:57/07:57/08:12/08:17/08:57）
⇒ **盘前 8 笔里 7 笔（87.5%）在 DA 卡生成之前**。

⇒ **跨臂方向约束（1.3）对盘前段基本不可用**；只覆盖盘后段 + 极少数盘前晚单。
若要真正约束盘前 EXT，必须把"方向"提前到 **DA 出卡之前**可得（UE/候选池方向），否则只是名义上的约束。
⇒ 影子已把 `da_available` 逐笔落盘 ⇒ **覆盖缺口本身可被量化**，20 日影子后能给出真实覆盖率，再决定 1.3 的形态。

### 9.4 本批未做 / 仍待裁

| 项 | 状态 | 原因 |
|---|---|---|
| 0.3 归因表 `be_exit_class` 分层（渲染层） | 待 | `app/review/*` 在途 WIP（并行 DEB-20261110 实施轮） |
| 1.2 强制 EXT 落 TP / 无 TP 跨夜仓次日盘前收口 | 未做（只落影子） | 行为变更 ⇒ 按 Batch 1 纪律先攒 20 日影子 |
| 1.3 跨臂方向冲突拦截 | 未做（只落影子） | 同上 + §9.3 覆盖缺口使盘前段无效 |
| 0.5 台账字段改造（`vwap_gate_ab.jsonl` 加"被下游二次拦截"标记） | 未做 | 需改 `app/playbook/pipeline.py` ⇒ **需重启** ⇒ 会连带上线并行 agent 在 `app/review/*` 的在途 WIP（AGENT.md 禁止）；已用只读四象限替代，等 WIP 收口再做 |
| Batch 2（C2 跳空门 / 同标的冷却 / BE 降级） | 未做 | 行为变更 ⇒ 须另立 RFC |
| C2 口径二选一 | 待裁 | 见 §七 ① |
| **EXT 旁路臂定位**（研究臂 vs 第二条主线） | 待裁 | 见 §七 ④；本批已把 1.2/1.3 的影子与体检铺好，等 20 日读数再裁 |

---
