# 议题：富途 OpenAPI 权益全量利用审计 — V2 权益实测 / 10.9 新接口实弹 / 前案落地复核

| 字段 | 值 |
|---|---|
| 状态 | 审计完成 + P1 收口，**待交叉审查**（record-only，零执行路径改动） |
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
