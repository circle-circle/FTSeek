# 议题：富途 OpenAPI 权益全量利用审计 — V2 权益实测 / 10.9 新接口实弹 / 前案落地复核

| 字段 | 值 |
|---|---|
| 状态 | 审计完成 + P1 收口 + 交叉审查完成（8/18 第 3 轮）+ 第 4 轮能力补接与落地（2026-08-21）：analyst_consensus 复活、影子判官补齐、capital_flow 首轮 IC +0.119、deriv 假说暂否、OFI 基线采集与宏观事件日程接入 + 第 5 轮交叉审查（2026-08-21 Qoder）：落地主体全部独立复现成立；一处叙事误归因勘误（"8/5 免订阅结论"不存在）；8/24 裁决窗三前置缺口点名（增量 IC 未实现 / OOS 同号判读弱 / deriv 无基线配对）+ **第 6 轮（Hermes 复算认账 + 修复）：7/7 发现全部属实，增量 IC/裁决分级/deriv 基线配对/时区/日程过滤全部落地（修尺子白名单内，record-only）；capital_flow 首轮读数降级 WEAK_EVIDENCE（t=1.384），deriv excess=-0.003% 假说更牢固证伪** (record-only，零执行路径改动) |
| 创建 | 2026-08-18（用户令：评估权益都利用起来，确保性能和真实性可用，产出落 DEB） |
| 前案 | DEB-20260812-futu-unused-channels（8/12 升级 OpenD/SDK 10.9 + 采集器落地） |
| 纪律 | 本档纯审计：19 接口逐一实弹（可用性/延迟/真实性），订阅压测退订归零，零交易行为变更 |
| 证据 | scripts/futu_capability_audit.py（可重跑）+ data/futu_capability_audit_20260818.json（原始结果） |

---

## 一、账户权益实测基线（get_user_info / query_subscription，非纸面）

| 权益项 | 实测值 | 当前使用状态 |
|---|---|---|
| 订阅额度 | **1000 席（正股）+ 200 席（期权）** | FTSeek 服务侧收敛为 QUOTE+K_1M（ADR-013），TICKER/ORDER_BOOK 零消费者 |
| 历史 K 线额度 | 1000 条/次 | 重度使用（request_history_kline 全仓 48 处） |
| 美股行情 | **LV3**（Nasdaq Basic+TotalView，逐笔级） | 仅快照轮询消费，LV3 逐笔能力未启用 |
| 港股/期权 | LV1 | 未用（美股系统，不适用） |
| OpenD / SDK | 10.9.6918 / 10.9.6908（8/12 已升级） | 新协议已通，消费方严重不足（§三） |

**核心结论：权益已付费且充足（1000 席订阅实测 0 占用），瓶颈不在额度，在消费方接线与 ADR-013 的 CPU 保护约束。**

---

## 二、前案 DEB-20260812 落地复核（独立复算，不信任声称）

| 声称落地物 | 复核结果 |
|---|---|
| capital_flow 采集器（§6.2） | ✅ 真在跑：data/futu_capital_flow.jsonl 5.6MB，最新 item_time=8/17 16:00（收盘），文件 mtime 8/18 03:59 |
| derivative_unusual 采集器（§6.2） | ✅ 真在跑：data/futu_deriv_unusual.jsonl 953KB，最新事件 8/18 03:44（结构化字段齐全） |
| 财报日历 Futu 冗余源（§6.3①） | ✅ 在位：app/data/earnings_calendar.py L288 `_fetch_futu_calendar_fallback`；本次实弹 get_earnings_calendar 返回 239 行（7 天窗口） |
| 质量闸修复（§6.4 阈值 14e9→2e9） | ✅ 在位：skill 侧 market_scanner.py L236 `mcap < 2e9` + USD 单位纠错注释 |
| **盘前榜单扩池接线（§6.3②）** | ✅ 落地属实（8/18 P1 复核勘误）：代码在 **skill 侧** deep-stock-analysis/scripts/data_collector.py L418 定义 + L468 接入 collect_premarket_movers 池；本档初轮误报"从未入库"系**搜错仓**（只搜 repo 侧，未搜 DA 生产入口 skill 仓，即前案警告过的"先确认生产入口指向哪个目录"之反面教材）。skill 仓 git 不追踪 scripts/（untracked 设计），非遗漏 |

**出入定性（8/18 P1 复核后更正）**：初轮"声称-现实出入"判定**错误**，系审计搜错仓所致。
扩池代码真实存在且已接线生产（skill 侧 data_collector.py L418/L468），实弹返回 20 条
（双向榜各 10）。前案落地基本属实，无修复需求。教训：跨仓落地核查必须覆盖全部
生产入口（repo + skill），git untracked ≠ 代码不存在。

---

## 三、19 接口实弹审计（2026-08-18 13:30-14:00 CST 实取）

### A. 10.9 SDK 新接口（8/12 验证连通，消费方仅盘前榜单 1 处）— 9/9 可用

| 接口 | 延迟 | 结果 | 潜在用途 |
|---|---|---|---|
| get_rise_fall_distribution | 20ms | 2 rows | **市场广度**（regime 检测器现仅 VIX 轴，广度是文献支持的独立轴） |
| get_top_movers_rank | 55ms | 2 rows | 盘中异动榜（候选供给） |
| get_hot_list | 105ms | 2 rows | 热度榜 |
| get_us_pre_market_rank | 58ms | 2 rows | 盘前榜单扩池（前案已接线 skill 侧，实弹 20 条验证通过） |
| get_us_overnight_rank | 50ms | 2 rows | 隔夜榜 |
| get_us_after_hours_rank | 49ms | 2 rows | 盘后榜 |
| get_period_change_rank(1d) | 77ms | 2 rows | 周期涨跌榜 |
| get_search_news(NVDA) | 86ms | 5 rows | 快讯补充源（Finnhub 单点依赖的对冲） |
| get_economic_calendar | 44ms | 50 rows | **宏观事件日历**（FOMC/CPI 避让，替代外部源） |

### B. F10 系列（零消费者）— 5/5 可用

| 接口 | 延迟 | 结果 | 潜在用途 |
|---|---|---|---|
| get_financials_statements | 31ms | 3 rows | 三大财报（DA 深度分析现用 Finnhub，此为冗余源） |
| get_valuation_detail | 169ms | 7 rows | PE/PB 历史分位 |
| get_shareholders_holding_changes | 31ms | 3 rows | 机构增减持（比 SEC Form 4 更及时） |
| get_insider_holder_list | 159ms | 3 rows | 内部人持股 |
| get_research_rating_summary | 59ms | 2 rows | 机构评级明细 |

### C. 真实性交叉验证 — 全部通过

1. **Futu 快照 vs Finnhub 价格**（外部独立源）：NVDA 225.01/225.01、AAPL 305.59/305.59、MSFT 480.35/480.35 — **diff 全部 0.0000%**，行情数据真实性硬。
2. **内部互证**：get_financials_earnings_price_history 返回 NVDA 584 行历史财报日（含 IV Crush 波动率），与 earnings_calendar 同源一致。
3. **财报日历**：8/18-8/24 窗口 239 行，字段含 eps_predict/iv/iv_rank/market_cap — 确认前案"窗口≤7 天含端点"约束属实（8 天即报错）。
4. **采集器时效**：capital_flow 最新行至 8/17 收盘（item_time 美东原样存储，与前案时区声明一致）。

### D. 订阅额度压力测试（注册→验证→退订归零）

- TICKER×10 订阅：额度 0 → 10（增量精确）✅
- 等待 62s 退订窗口（Futu 硬约束：订阅后 ≥1 分钟才可退订）→ 退订 10/10 成功 → 额度归 0 ✅
- 兜底验证：ctx.close() 自动释放本连接全部订阅（首轮误测残留 10 席，新连接查询即为 0）✅
- **结论：1000 席额度机制健康，按需订阅零残留可行；瓶颈是消费方设计，不是额度。**

---

## 四、约束与陷阱清单（实弹确认，接线时必守）

1. **ADR-013 CPU 保护（最高优先级）**：订阅类型曾全量 7 类 → SDK 逐条建 DataFrame，接收线程满载一核 → 刻意收敛为 QUOTE+K_1M。**任何新增订阅类型（尤其 TICKER）必须先过 CPU 压测，禁止盲目放量。**
2. **US.VIX 直查不可用**（实测"未知股票"）→ VIXY×0.85 每 15min 的替代链路不能删。
3. **get_earnings_calendar 单次窗口 ≤7 天含端点**（8 天报错），恰好覆盖现有消费方 days_ahead=7。
4. **榜单/新闻接口返回 (all_count, DataFrame) 元组**，非标准 (ret, data)；sort_dir/period_type/sort_field 必须传枚举（RankSortDir/RankPeriodType/HotListSortField），传字符串/int 报"type is wrong"。
5. **退订需等 ≥60s**（订阅后立即退订报"未订阅过"）；短生命周期探针用 ctx.close() 兜底。
6. **订阅额度按连接计**：query_subscription 的 total_used 是跨连接聚合；审计/压测用独立短连接，不碰生产连接。

---

## 五、缺口清单与分级建议（全部待裁，record-only）

### P1 — 前案遗漏复核（8/18 已收口：两项均无需修改）

1. ~~盘前榜单扩池接线~~ → **核实属实，无遗漏**：skill 侧 data_collector.py L418/L468
   已接线生产（collect_premarket_movers 池内，门禁不变：|涨跌幅|≥4% + 盘前量≥500）；
   实弹返回 20 条（双向榜）。初轮误报系搜错仓，已勘误。
2. ~~验证财报日历 fallback 触发~~ → **功能验证通过**：直调 `_fetch_futu_calendar_fallback`
   返回 239 条，契约字段与 AV 解析结果零缺失，HD 8/18 pre-market 与富途日历外部锚点吻合。
   生产日志显示从未触发 = AV 源一直成功，fallback 保持待命——**这是 fallback 的健康状态，
   非死代码**。下次 AV 故障/空结果时自动接管，无需干预。

### P2 — 新通道影子先行（攒样后裁决，不接交易）

3. **市场广度轴**（get_rise_fall_distribution，20ms）：regime 检测器加广度影子记录，与 VIX 轴对照；文献支持（广度恶化是日内反转前兆），但需攒样验证增量信息，禁裸边际。
4. **宏观事件日历**（get_economic_calendar，44ms/50行）：FOMC/CPI/非农日避让影子计数，先观测该窗口胜率是否显著异常，再裁是否进门禁。
5. **快讯补充源**（get_search_news，86ms）：对冲 Finnhub 单点依赖，shadow 对照覆盖率与时效，不替换现链。

### P3 — DA 上下文冗余源（低优先，DA 深度分析侧）

6. **F10 三件套**（financials_statements/valuation_detail/shareholders_holding_changes）：作为 DA 分析 Finnhub 链的冗余源接入 skill 侧采集，零行为变更。

### 交叉审查补充（8/18 第 3 轮 Qoder，7 项待裁）

- **[前置] 盘中补测**（对应自曝 #1/#2/#4，P2 裁决窗硬前置）：盘中（ET 09:35-10:00）
  补三项 — ① 生产连接 `query_subscription` 真实占用（休市 0 占用 ≠ 盘中稳态，
  QUOTE+K_1M × 扫描池规模未实测）；② 榜单接口盘中行数/延迟（"2 rows"为元组长度，
  语义=协议通≠数据新鲜）；③ Futu vs Finnhub **盘中并发**价格交叉（休市 0.0000%
  只证同源一致，不证实时性一致）。
- **P2 重排：宏观日历 > 市场广度 > 快讯**。宏观日历是唯一"一次采集、多处消费"通道：
  既服务 FOMC/CPI 避让 shadow，又是 DEB-20260818 UE 空动量过滤"事件驱动例外通道"
  的事件源。附带纪律：FOMC/CPI 日须在 be_0.4 毕业窗口判分中单独打标，防污染基线。
- **订阅卫生只读监控**（不违反 ADR-013，零新增订阅）：每日盘后记
  `query_subscription.total_used`（跨连接聚合），异常增长告警。压测证明"按需归零可行"，
  但可行需要持续证明——与 DA 供给监控同构的发现机制。
- **跨仓生产入口 manifest**（流程修复）：本案两轮翻车同源（初轮搜错仓；审查方第一步
  也踩同坑——repo 下无 `deep-stock-analysis/`，真实路径 `~/.hermes/skills/`）。
  建议 docs/adr 加一页生产入口清单（DA skill / UE / repo 路径 + git 追踪状态），审计前必读。
- **F10 保留 P3 + 显式触发条件**：P1b 票型相对化完成时（同窗 8/24-9/2）同步评估——
  valuation_detail 的 PE/PB 历史分位是 L1 认知卡缺的第三个可验证维度，提前接线=闲置。
- **"LV3 逐笔未启用"写成显式决策记录**（ADR-013 附录），避免每轮审计重复考古。
- **审计 JSON 元数据**：现为裸 19 行 list，补 `generated_at` + 环境标识
  （OpenD 端口/账号尾号），否则快照无法判归属。

### 显式不做（防范围蔓延 + 已证伪）

- **盲目扩大订阅类型**：ADR-013 CPU 教训在案；TICKER 订阅仅在出现明确消费方（如 OFI 转正需要逐笔）时按最小集启用。
- **US.VIX 直查**：实测不可用，不立项。
- **港股/A 股接口**：美股系统，不适用。
- **期权链/IV 因子化**：DEB-20260805 已否决（冻结至可证伪事件驱动假说），本档不重启。

---

## 六、决策

- 暂无（审计档，P2/P3 待裁；P1 已收口无需落地）。
- P1 复核结论：前案落地属实，两项均无需修改（详见 §五 P1 与讨论记录第 2 轮）。
- P2/P3 走影子攒样 → 8/24 或 9/2 裁决窗（与 DEB-20260812 A/B 同窗）。

## 讨论记录

### 第 1 轮（Hermes 全量实弹审计，2026-08-18）

**方法**：不信任前案声称，19 接口逐一实弹（可用性/延迟/行数），价格三标的跨源交叉（Futu vs Finnhub），订阅压测退订归零，前案四个落地物独立复核（文件存在性 + git 考古 + 代码在位性）。全部裸跑，原始结果落 data/futu_capability_audit_20260818.json。

**发现**：
1. 权益充足（1000 席订阅 0 占用），瓶颈在消费方不在额度。
2. 10.9 新接口 9/9 可用但消费方仅盘前榜单 1 处 — 升级红利大部分未兑现（第 2 轮勘误后口径）。
3. 前案"盘前榜单扩池接线"初轮误报"声称与提交不符"——系搜错仓（只搜 repo 未搜 skill 生产入口），
   P1 复核已勘误：代码真实存在且已接线（讨论记录第 2 轮）。
4. 真实性验证全过：价格 diff 0.0000%、财报日历/历史互证一致。
5. 订阅机制健康：压测 0→10→0 精确，退订 60s 硬约束与 close 兜底均已实证。

**自报局限**：
- 榜单接口在盘后时段实弹，返回行数少（2 rows），盘中数据密度需开盘后复核。
- F10 接口仅验证 NVDA 单标的，小票覆盖度未测。
- 未测 get_earnings_calendar 在财报日历 fallback 生产路径的实际触发率——P1 复核已补：
  直调验证功能可用，生产从未触发系 AV 一直成功（fallback 健康状态）。

### 第 2 轮（P1 复核与自我勘误，2026-08-18）

**背景**：用户批准 P1 两项落地。执行复核后发现**初轮审计的两处误判**，如实勘误：

**误判 1：盘前榜单扩池"从未入库"**
- 初轮只搜 repo 侧（FTSeek-US 仓）`git log -S collect_premarket_rank_codes` → 空 → 判定遗漏。
- P1 复核扩搜 skill 生产入口（deep-stock-analysis/scripts/）→ 代码真实存在：
  data_collector.py L418 定义（双向榜 descend/ascend 各取 50）+ L468 接入
  collect_premarket_movers 池，门禁不变（|涨跌幅|≥4% + 盘前量≥500）。
- 实弹验证：`collect_premarket_rank_codes('US', 10)` 返回 20 条（US.IPST/IVF/TRUG/EFSI/DEFI…）。
- 误判根因：skill 仓 git 不追踪 scripts/ 目录（194 个 untracked 文件为设计），
  untracked ≠ 不存在。这正是前案 §6.2 警告过的"先确认生产入口指向哪个目录"的反面教材。

**误判 2：财报日历 fallback"死代码风险"**
- P1 复核直调 `_fetch_futu_calendar_fallback(days_ahead=7)`：返回 **239 条**，
  契约字段 {symbol, reportDate, timeOfTheDay, estimate, currency, name, source} 与
  AV 解析结果零缺失对齐；HD 8/18 pre-market EPS 预估 4.6057 与富途日历审计锚点一致。
- 生产日志证实从未触发（AV 一直成功）——fallback 待命是**健康状态**，非死代码。
- 结论：无需任何修改，下次 AV 故障自动接管。

**P1 收口判定**：前案 DEB-20260812 §6.3 落地基本属实，两项均无需修改。
本档 §二/§五/§六 已同步更正；初轮结论中的"落地遗漏"定性撤回。

### 第 3 轮（Qoder 交叉审查，2026-08-18）

**方法**：按 §七 复核清单执行**离线可核验项**，不重跑订阅压测、不触碰生产额度/连接
（重跑审计脚本会临时占 10 席，对生产无增益）。skill 仓以文档锚点
（~/.hermes/skills/deep-stock-analysis，见 WEEKLY-SUMMARY-2026-07-11 / ADR-024）定位后直读源码。

**核验结果（6/6 过，零勘误）**：

| 声称 | 核验 |
|---|---|
| 审计 JSON 19 接口 | ✅ 19 条 list，mtime 13:59 与声称实取窗口（13:30-14:00 CST）一致，无未来时间戳 |
| skill 侧 L418 定义/L468 接线 | ✅ `/root/.hermes/skills/deep-stock-analysis/scripts/data_collector.py`：`collect_premarket_rank_codes` 定义 + `pool += collect_premarket_rank_codes(market, 50)` 接线在位 |
| scanner 真消费该池 | ✅ market_scanner.py:142 `premarket_movers` 渠道调用 `collect_premarket_movers` |
| 财报日历 fallback | ✅ app/data/earnings_calendar.py:279 调用 / :288 定义在位 |
| 采集器新鲜度 | ✅ futu_capital_flow.jsonl 5.57MB (mtime 8/18 03:59) / futu_deriv_unusual.jsonl 953KB (03:48)，与 §二 声称吻合 |
| 第 2 轮自我勘误 | ✅ 抽验成立（skill 接线真实、fallback 待命定性合理） |

**审查判定**：
1. 审计主体结论（权益充足、瓶颈在消费方、19 接口协议层可用、订阅机制健康）在
   离线证据范围内成立；第 2 轮勘误方向正确、定性诚实。
2. 自曝攻击面 #1-#4 属实且是**当前全部未决风险**——审计全部发生在休市时点
   （美东 01:30），"0 占用/可用/0.0000%"三组数字的语义边界 DEB 自己已声明，
   审查方确认这些边界就是 P2 裁决前必须补的盘中证据（见 §五 交叉审查补充 [前置] 项）。
3. 未核验项（诚实声明）：延迟数字（20-169ms）与价格交叉采信 JSON 原始结果未重放；
   get_search_news/F10 盘中行为未测——均在盘中补测范围内。

**建议 7 项**：已就地写入 §五"交叉审查补充"小节（盘中补测前置 / P2 重排 /
订阅卫生监控 / 生产入口 manifest / F10 触发条件 / LV3 决策记录 / JSON 元数据），
全部待裁。

**连带记录**：审查方本人第一步也搜了 repo 侧 `deep-stock-analysis/` 落空——
与初轮审计同坑。该路径只存在于 `~/.hermes/skills/`，跨仓 manifest 的必要性
由两方独立翻车实证（非假设性流程建议）。

### 第 4 轮（能力补接盘点与落地，2026-08-21，Hermes）

**触发**：用户令"是否还有富途存在且有用的能力但并没有启用接入的，历史上有检查过，
请确认是否都有效接入并真正有效，并请明确说明收益"。随后批准按建议执行。

**方法**：SDK 10.09.6908 全量方法 vs 代码库调用点 diff → 已接线项逐一查产物/消费方
实证 → 零消费候选 14 项实弹（可用性/延迟/字段/小票覆盖）。

**盘点结论**：
1. 已接线 6 通道全部存活实证：capital_flow（49,987 行/56 票/8/11-8/20 连续）、
   deriv_unusual（2,952 事件）、财报日历（生产启动 147 票）、空头/内部人
   （8 条指令引用）、盘前榜单（skill 侧）、核心交易链。LV3 逐笔仍显式闲置
   （ADR-013，非遗漏）。
2. 14 项零消费候选实测 9 可用：analyst_consensus/rating_change/经济日历/广度/
   快讯/期权IV概览/技术资金异动/热力图/卖空榜等。
3. **真问题实锤**：`futu_extended_provider.py` L57 死 stub —— "get_research_analyst_consensus
   不存在于 SDK"是 **SDK 10.03 时代的过时判断**，10.09.6908 已有且实测全覆盖
   （MU 30 家/目标价、连 WEAV/BULL 小票都有）。这是全场唯一"接线被误拆"的能力。

**落地（commit 0bca5129 + 本轮）**：
1. **analyst_consensus 复活**：恢复 get_data() 调用 + 实现方法体（rating/目标价/
   buy-hold-sell%，1h 缓存，失败优雅降级）。消费链已就绪：data_collector 情绪混合
   （缺失时替代/共存时 0.6 权重）+ premarket_enrichment DA 注入。e2e 实弹通过。
   收益=补 Finnhub free-tier target-price 403 缺口 + 指令情绪特征质量。
2. **影子判官补齐**：scripts/futu_channel_shadow_eval.py（DEB-20260812 §五预注册判据
   照抄）。首轮实算（8 交易日）：
   - 通道 B capital_flow：质量门 100% 过，n=136 对（≥30），rank-IC **+0.119**，
     双窗口 +0.203/+0.095 同号 → verdict=PASS 候选。**带星号**：OFI 基线缺失
     只能算裸边际 IC，违反预注册"禁裸边际"纪律，8/24 裁决证据权重降级。
   - 通道 A derivative：2,890 配对（≥30），事件后 30min 均值 +0.03%、胜率 48.5%
     → **大单流领先假说暂被数据否定**。若 8/24 仍如此，按 DEB-20260812 §五对称
     清单应同日下线 DA prompt 衍生品异动段。
3. **OFI 基线通道 C**：`futu_channel_shadow_collect.py` 新增 `_collect_ofi_baseline`
   （level-1 快照 {bid1/ask1 价量}，ΔOFI 评估侧算）。**实弹纠错**：get_order_book
   在 SDK 10.09 已要求先订阅 ORDER_BOOK（8/5 DEB-20260805 探针"免订阅"结论过时，
   若照搬将违反 ADR-013）→ 改 get_market_snapshot 批量 HTTP 免订阅（实测闭市有盘口，
   4 票实弹落盘）。独立开关 `futu_channels.ofi_collect_enabled`。
4. **宏观事件日程**：`macro_calendar_shadow_collect.py` 新增 `_fetch_futu_event_schedule`
   （未来 7 天美国 HIGH/MEDIUM 事件，FRED 段=已公布值滞后、本段=未来日程表，互补
   同源一次采齐）。实弹 22 事件/字段全/时间已转 ET。fail-open 不影响 FRED 主链。
   顺手修 CLI 直跑缺 sys.path 的 latent bug。独立开关 `macro_calendar.event_schedule_enabled`。

**验证**：全量 1383 passed · 宏观 e2e FRED 7/7 + 日程 ok · OFI 4 票实弹落盘 ·
重启零 traceback · reconcile broker 2 vs 内部 2 零漂移。

**纪律记录**：
- "历史结论不永恒"第二次实证（第一次：F10 API 不可用→10.9 可用）：本次
  get_order_book 订阅要求变更。审计结论带 SDK 版本戳，跨版本须复测。
- 只提交自己文件，未碰 sibling agent 的 vault/exit_calibration 并行改动。
- 回滚：`ofi_collect_enabled=false` / `event_schedule_enabled=false` + 重启。

**8/24 裁决窗前置状态**：判官已就位；capital_flow 续采 + OFI 攒样中（增量口径
需 OFI ≥100 行快照对）；deriv 通道若无新证据按预注册走关门+prompt 对称下线。

### 第 5 轮（Qoder 交叉审查，2026-08-21）

**触发**：用户令复查 Hermes 当日更新与优化调整（第 4 轮两笔提交 0bca5129 + d62fe088）。

**方法**：按本档 §七 精神不信任叙述逐项独立复算 — 数据文件直查（行数/票数/日期域）、
判官独立重跑（含 bar 缓存回补差异观察）、analyst_consensus 三票独立实弹（独立短连接）、
消费链代码锚点直读、SDK 行为对历史 DEB/handoff 考古、配置开关/调度接线/服务日志核验、
全量测试独立重跑。

**核验结果（13/14 过，1 项未核验）**：

| 第 4 轮声称 | 核验 |
|---|---|
| capital_flow 49,987 行/56 票/8/11-8/20 连续 | ✅ 直查逐位一致（56 票、8 个交易日无断日） |
| deriv_unusual 2,952 事件 | ✅ wc=2952 |
| 判官 capital_flow：IC +0.119/n=136/双窗 +0.203/+0.095/PASS 候选 | ✅ 独立重跑逐位复现（0.1187/136/0.2028/0.0954/同号） |
| 判官 deriv：2,890 配对/+0.03%/48.5% | ✅ 复现（重跑 n=2914，bar 缓存回补差 +24 对；均值 +0.030%/胜率 48.5% 不变，结论不受影响） |
| analyst_consensus 实弹（MU 30 家含目标价，WEAV/BULL 小票覆盖） | ✅ 独立实弹：MU n=30/rating=buy/目标价 1568.39，WEAV n=5/hold，BULL n=2/buy |
| 消费链已就绪（情绪混合 0.6 权重 + DA 注入） | ✅ data_collector.py:714-731（缺失替代 + 0.4/0.6 加权在位）；premarket_enrichment.py:122-124 → llm_pipeline.py:945-981 → debate prompt 注入闭环 |
| OFI 4 票实弹落盘（get_market_snapshot 免订阅） | ✅ futu_ofi_baseline.jsonl 4 行（SPY/MU/NVDA/WEAV 04:13 ET，价量全非零） |
| 宏观 FRED 7/7 + 日程 22 事件 ok | ✅ 7 指标全 status ok（CPI 3.30 等）+ event_schedule ok/22 |
| 独立开关 + 回滚键 | ✅ ofi_collect_enabled / event_schedule_enabled=true，注释在位 |
| 服务重启零 traceback | ✅ 17:14:07 重启 active，journal 0 traceback |
| 纪律：只提交自己文件 | ✅ 两笔 commit stat 仅自家 5 文件，未动 sibling 的 vault/exit_calibration |
| 调度接线（OFI 攒样可持续） | ✅ scheduler_core:2115-2119（TRADING）/2225-2229（AFTER_HOURS）importlib 动态装载 run_collect；宏观挂 PRE_MARKET（2205-2212）——今晚开盘起 OFI 自动攒样 |
| 全量 1383 passed | ✅ 复核方独立重跑 1383 passed / 0 failed / 0 error（收集数 1383 吻合；单进程全量跑在复核环境卡网络依赖用例——handoff 既有记载——故按 119 个文件逐文件聚合 + 1 个 `*_test.py` 模式文件补跑合计） |
| reconcile broker 2 vs 内部 2 零漂移 | ⚠️ 未核验：journal 仅 systemd 生命周期行，应用对账日志未见落点；作者时点性验证，无法复现亦无法证伪，如实登记 |

**发现（7 项，按严重度排序，均非阻断）**：

1. **[叙事误归因·勘误] "8/5 探针'免订阅'结论过时"不成立**：DEB-20260805 §5.1
   B1 探针 8/5 实测即被拒（"请先订阅OrderBook数据"），当时的结论本来就是
   "需订阅 + 降级一级快照"；全库 grep 无任何"免订阅"结论存在。8/6 勘正还记录了
   "subscribe_push=False + 裸 get_order_book + 即退"的可行绕法（S2 同款）。
   且订阅要求在 SDK 10.3 时代即成立，并非"SDK 10.09 已要求"的新变化——
   "历史结论不永恒第二次实证"的叙事建立在虚构前提上。另：get_market_snapshot
   批量免订阅方案系 **spread_depth_capture.py 已有设计**（handoff 变更日志：
   "原计划 get_order_book，实测需 ORDER_BOOK 订阅…改 get_market_snapshot 批量，
   自带 bid/ask/量，单次往返零订阅"），非本轮新发现——本轮价值 = 复用已验证模式，
   应如实归档。落地本身正确（零订阅、ADR-013 合规、实弹 4 票）。
2. **[8/24 前置缺口] 预注册"增量 IC"未实现**：§五预注册毕业检验 = "增量 rank-IC
   （在现有因子+OFI 基线上）"。判官目前只算裸边际 IC，OFI 行数仅作 flag
   （ofi_baseline_available），无残差化/增量计算代码（grep incremental/residual=0）。
   "判官已就位"表述略强——就位的是裸边际判官，预注册判官缺最后一段。OFI 按调度
   推算 ~1,500 行/交易日（20 票 × 5min × 6.5h），8/24 前样本充足，缺的是计算逻辑。
3. **[裁决语义] PASS 候选落在弱解释分支**："双窗口 OOS ≥40%"被判官解释为
   "两窗 IC 同号"（代码注释已自 flag "此处=同号"）。零假设下同号概率 ≈50%；
   且总 IC 0.1187 < 0.15 显著阈（t≈1.38，n=136，p≈0.17 未过 95% 门）。
   8/24 裁决须回预注册原文重新定语义，建议该读数按"方向性弱证据"处理而非"过门"。
4. **[口径] deriv 评估弱于脚本自述**：docstring 自称"事件后收益 vs 同票无事件基线
   （配对）"，实现只有事件后 30min 均值+胜率，无无事件基线对照；买入/卖出混算
   不预判方向（代码自注）。+0.030% 均值但 48.5% 胜率 <50% = 右偏分布，无基线
   不能排除日内漂移。"假说暂被否定"作方向性读成立，未达自述配对口径。
5. **[潜伏 bug] CST→ET 硬编码 -12h**：eval_derivative 换算 `cst - timedelta(hours=12)`
   仅 EDT（至 2026-11-01）正确，冬令时后偏 1h——时区家族教训新潜伏例。
   当前 8 月数据窗不受影响，冬季数据进入会静默错位，建议 8/24 前改 zoneinfo。
6. **[轻微] 宏观"未来 7 天"含已发布事件**：日程首事件 8/20 16:30 ET（带 actual 值），
   疑因 Futu begin/end 日期按北京时间解释（窗口左边界 = ET 前一日 12:00）。
   消费侧按 ts_et>now 过滤即可，现无过滤——fail-open 设计下无害。
7. **[卫生] 判官死代码/哑参数**：`--window` 参数两评估函数均未消费（全量口径恰逢
   数据=8 交易日无实际偏差）；deriv L236 恒真 if、L259-260 no-op 残留。

**方法边界补充**：OFI 5min 采样是真 OFI 的粗代理（真 OFI 需逐笔/秒级）——增量 IC
计算落地后，若发现 ΔOFI(5min) 与分钟资金流相关性弱，"增量口径"会退化为裸边际换皮；
8/24 读数时须一并报告两者相关系数。

**审查判定**：第 4 轮**落地主体成立**——analyst_consensus 复活真实可用（三票独立
实弹 + 消费链闭环），全部数字独立复现，纪律执行干净，零执行路径改动属实，星号/
同号等诚实标注值得肯定。但：叙事层一处误归因需勘误（#1）；8/24 裁决窗三个证据链
缺口（#2 增量 IC 计算、#3 OOS 语义确认、#4 deriv 基线配对）不补则裁决证据链不闭合。
建议 #2/#5 列入 8/24 前待办（均为修尺子白名单内），#1 就地勘误（本节即是），
#3/#4 裁决时按弱证据加权。

### 第 6 轮（Hermes 复算认账 + 修复，2026-08-21）

**触发**：用户令核查 Qoder 第 5 轮。按纪律逐条独立复算，不信任审查方描述。

**复算结果：7/7 发现全部属实，认账。**

| # | Qoder 发现 | 独立复算 |
|---|---|---|
| 1 | "8/5 免订阅结论"不存在，叙事误归因 | ✅ DEB-20260805 §5.1 表格实锤：B1 当时即被拒"请先订阅OrderBook数据"，结论本来就是"需订阅+降级一级快照"；snapshot 批量方案系 spread_depth_capture.py 已有设计（handoff L696）。第 4 轮"历史结论不永恒第二次实证"叙事撤回 |
| 2 | 增量 IC 未实现 | ✅ grep incremental/residual=0，OFI 行数仅作 flag |
| 3 | 同号即 PASS 在零假设下概率~50% | ✅ t=0.1187×√134/√0.986=1.384<1.96，p≈0.17 |
| 4 | deriv 无基线配对 | ✅ docstring 自述配对，实现仅事件后均值+胜率 |
| 5 | CST→ET -12h 硬编码冬令时偏 1h | ✅ L235 实锤；8 月窗口不受影响 |
| 6 | 日程含已发布事件 | ✅ 首事件 8/20 16:30 带 actual 值 |
| 7 | --window 哑参数 | ✅ 两函数签名含参未消费 |

**修复落地（全部修尺子白名单内，record-only 零执行路径）**：

1. **#1 就地勘误**：第 4 轮讨论记录"8/5 探针免订阅结论过时"表述撤回——
   正确表述：订阅要求在 SDK 10.3 时代即存在（8/5 即被拒），snapshot 批量方案复用
   spread_depth_capture.py 已验证设计，第 4 轮价值=复用而非新发现。
2. **#2 增量 IC 实现**：`load_ofi()` + `ofi_increments()`（Cont-Kukanov-Stoikov
   2014 增量定义）+ rank(flow) 对 rank(ΔOFI) OLS 残差化后算 rank-IC；同时报
   flow_ofi_rank_corr（弱相关则增量退化为裸边际换皮，8/24 读数必须一并报告）。
   当前 OFI 仅 4 行（8/21 闭市启动），重叠样本 0<8 → incremental_note 如实标注
   攒样中；今晚开盘起 OFI 按调度攒样（~1,500 行/交易日），8/24 前样本充足。
3. **#3 裁决分级**：verdict 三档 — PASS_SIGNIFICANT（t≥1.96 且双窗同号）/
   WEAK_EVIDENCE（同号或 |ρ|≥0.15 但不显著）/ FAIL。首轮读数从 PASS 降级为
   **WEAK_EVIDENCE**（t=1.384，方向性弱证据非过门）。
4. **#4 deriv 基线配对**：每事件对照同日同票 5min 网格 30min 收益中位数基线
   （事件点±30min 剔除），报 excess 三件套。**配对后实证：excess 均值 -0.003%、
   excess 胜率 48.7%** — 原始 +0.03% 完全来自日内漂移（Qoder 质疑被证实），
   大单流领先假说被更牢固证伪；8/24 按预注册走关门+prompt 对称下线的概率上升。
5. **#5 时区**：改 zoneinfo（冬令时自动 -13h）。
6. **#6 日程过滤**：ts_et < now 的已发布事件剔除（22→21，首事件=8/21 09:45 PMI）。
7. **#7 window 真消费**：只算最近 N 交易日（默认 8=当前全量，无读数变化）。

**方法边界继承**：OFI 5min 采样是真 OFI 粗代理（Qoder 第 5 轮），增量 IC 落地后
若 ΔOFI(5min) 与分钟资金流相关性弱，"增量口径"退化为裸边际换皮——8/24 读数时
一并报告 flow_ofi_rank_corr。

**验证**：评估器重跑通过（capital_flow WEAK_EVIDENCE / deriv excess -0.003%）·
宏观 e2e 过滤生效 · 1383 测试全过 · 仅改 2 个 record-only 脚本，零执行路径。

---

## 七、交叉审查指引（2026-08-18，用户令另一 agent 分析检查）

### 复核清单（按此逐条验证，勿信本文叙述）

1. **权益基线**（§一）：重跑 `scripts/futu_capability_audit.py` 或直接调
   get_user_info / query_subscription，核对订阅额度 1000+200、LV3、OpenD/SDK 版本。
2. **接口实弹表**（§三 A/B）：原始结果在 data/futu_capability_audit_20260818.json；
   注意榜单类返回 (all_count, DataFrame) 元组，"2 rows" = 元组长度非数据行数，
   盘中数据密度需开盘后复核（自报局限）。
3. **真实性验证**（§三 C）：价格交叉可自行重跑任意标的 Futu vs Finnhub；
   财报日历直调 `EarningsCalendar()._fetch_futu_calendar_fallback(7)` 应得 200+ 条。
4. **P1 勘误链**（§二 + 讨论记录第 2 轮）：本档自我推翻过一次（初轮误报扩池未入库）。
   审查时重点验证第 2 轮结论：skill 侧 data_collector.py L418/L468 是否真接线、
   market_scanner.py premarket_movers 渠道是否真消费该池。
5. **前案落地物**（§二 表）：四个 ✅ 各有可复现命令（文件 mtime/日志 grep/直调），
   逐条复算。

### 自曝攻击面（本档最可能被推翻的点）

1. **"瓶颈在消费方不在额度"结论的前提**：订阅压测只用 TICKER×10 短连接，
   未验证生产连接（QUOTE+K_1M × 44 只扫描池）叠加后的真实占用与 CPU；
   若审查方能拿到盘中生产的 query_subscription 全连接数据，可证伪/强化此结论。
2. **盘后时点偏差**：全部实弹在 13:30-14:00 CST（美东 01:30，休市），
   榜单/盘前榜在休市返回的是上一交易日残值——"可用"结论的语义是"协议通+有数据"，
   不等于"盘中数据新鲜"。
3. **LV3 逐笔"未启用"定性**：基于 grep TICKER 消费者为 0，但未排除
   盘中由 OFI 攒样链路（DEB-20260812 R2）临时订阅的可能——需盘中进程内确认。
4. **价格交叉 0.0000% 的解释边界**：休市时点两源都是收盘价快照，
   盘中并发取数才构成强验证；本结果只能证明"同源一致"，不能证明"实时性一致"。
5. **F10 系列只测了 NVDA 大盘股**，小票/低覆盖票的字段完整度未知。

### 复现路径

- 审计脚本：`PYTHONPATH=. venv/bin/python scripts/futu_capability_audit.py`
  （含 62s 退订等待，全程约 3 分钟；会临时占用 10 席订阅，结束自动归零）
- 原始数据：data/futu_capability_audit_20260818.json
- 前案对照：docs/debates/DEB-20260812-futu-unused-channels.md §6.2/§6.3/§6.4

## 关联文档

- docs/debates/DEB-20260812-futu-unused-channels.md（前案：升级 + 采集器 + 接线，本档复核其落地）
- docs/debates/DEB-20260805-futu-unused-capabilities.md（期权链/IV 主张 5 否决，冻结条款仍有效）
- config/unified_config.yaml futu_lv3.stock_sub_types（ADR-013 订阅收敛配置）
- 提交：6457639f（前案 repo 侧接线：财报日历 fallback；skill 侧扩池在 untracked scripts/ 中，
  P1 复核确认落地属实）
