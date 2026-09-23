# RFC-102 盘前事件配对扩展: 纳入「昨日 AMC」(预注册)

- **日期**：2026-09-23 | **状态**：已落地（开关 `EXT_ENTRY_PREMARKET_AMC_PAIRING`，`.env.us` 计划置 `true`）
- **用户裁决**：2026-09-23 「**A**」= 预注册后直接转正（备选 A′ = 先影子攒 5–10 日，未选）
- **复核日**：**2026-10-08**（与 RFC-100 / RFC-101 同批，便于一次仲裁）
- **回滚**：`.env.us` 改 `EXT_ENTRY_PREMARKET_AMC_PAIRING=false`（一个词，下一 tick 生效，无需重启）
- **预注册人**：助手（本条即留痕；判据在下线前写定，事后不得追改）

---

## 1. 背景与证伪源（全部为 2026-09-23 实测，非推断）

盘前链的判据①要求「**当日同相位**财报事件」（`reportDate == today` 且相位与本 session 匹配），
实测这条配对与**数据的可得性错位**：

| 证据 | 读数 |
|---|---|
| 9/23 盘前 6 只事件票的判据台账（67 轮 / 03:24→08:10 ET） | **5 只全程 `actual_missing`**（CTAS / GIS / PAYX / WOR；MANU 迟到后为 `surprise_nonpositive`） |
| 唯一放行者 | **CBRL：08:10 ET 才转 `pass`**（actual 0.99 vs est 0.1502 ≈ +559%）⇒ actual 迟到 **1–5 小时** |
| 第二源交叉验（防单源幻觉） | Nasdaq 同日 22 行中 CTAS/PAYX/CBRL/MANU **`eps=None`** ⇒ 免费日历源当日普遍迟到，**换源不是解法** |
| 反面对照 | **9/22 的 8 行日历现在有 7 行 actual**（次日回填）⇒ **昨日 AMC 的 actual 在次日盘前拿得到** |
| 当日链上结果 | 判据② 是「0 单」在量闸/价差**之前**的第一道结构性卡点（当日 `ordered=0`） |

**推论**：对**当日 BMO** 票，判据②要到窗尾（08:0x ET 后）才可能满足；而**昨日 AMC** 票
（16:00–16:30 ET 发布、隔夜反应、次日盘前仍有交易价值）**数据齐全却被配对排除**。
本 RFC 做的就是「**人群与数据可得性对齐**」，不是放宽判据。

## 2. 变更（最小、可逆、复用既有框架）

1. **配对表唯一真源**：新增 `_session_event_pairs(session)` → `[(日偏移, 相位)]`
   - 默认：盘前 `[(0,"before")]`、盘后 `[(0,"after")]`（**与线上 byte 级一致**）
   - 开关开：盘前 `[(0,"before"), (-1,"after")]`（追加**昨日 AMC**；盘后**不受影响**）
2. `_earnings_calendar_event()` 按配对表匹配（日期集合 + 相位），保留原归因语义：
   **日期不在表内 ⇒ `calendar_missing`**（老行为不变）；同日期相位不符 ⇒ `calendar_phase_mismatch`。
3. 判据② **取事件所属日**的 Finnhub 日历行：`_finnhub_today_calendar(day=None)`、
   `_earnings_surprise_calendar_actual(ticker, day=None)`；昨日 AMC 读**昨日行**取 actual。
4. 判据① 的 union 回落 `_finnhub_event_fallback` 同样走配对表逐日查（返回 `reportDate` = 命中日）。
5. **判据② 取数日并源（§2.6，实测驱动）**：事件日取不到 actual 时，在**配对表内**的另一日兜底。
   驱动案例（2026-09-23 实测 + 第三源交叉验）：futu 把 **WOR** 记成"当日 BMO"，而 Finnhub(9/22 行)
   与 **Nasdaq(9/22 日历)** 一致记为 **9/22 AMC**（actual 0.82 vs est 0.7575 ≈ **+8.3%**）
   ⇒ 老逻辑取"事件日=今日"落空，把一只 "+8% beat、盘前 +19% 跳空" 的票读成 `actual_missing` 拦掉。
   命中结果写 `actual_day`（真实取数日）+ `event_date` 到判据② 对照台账，供审计与复核。
5. 判据② 对照台账新增 `report_date` 字段（**事件日**，不是评估日）—— 供复核时查"串日"。

## 3. 预注册判据（复核日一次性裁决）

**转正（保留开关）**需同时满足：

- P1 **产出**：观察期内盘前链产生 ≥1 笔真实成交（SIM，可含做空），且成交票的 `report_date` == 事件当日/昨日
  （即事件日无串日）；
- P2 **放行非零**：判据②对昨日 AMC 票的通过率 > 0（台账 `report_date` 维度可查），且 ≥3 个交易日出现放行；
- P3 **不退化**：现行「当日 BMO」路径的读数不得变差（同日同票的旧路径闸计数逐键不变）；
- P4 **零事故**：观察期内无 `calendar_source_error` 导致的整轮 fail-closed；无券商侧越权下单（`is_live=False` 不变量成立）。

**回滚触发（任一条即回滚）**：

1. 出现 `report_date` 早于「昨日」的成交归因（**跨日错位** = 最危险失败模式）；
2. 判据② 的 `actual_day` **超出配对表日期**（即用了"今日/昨日"以外的 actual）；或 `actual_day` 与
   第三源（Nasdaq）不一致且因此产生成交（并源本身是**设计内**行为，§2.6；但取数日必须可审计）；
3. 盘后 session 的行为发生任何变化（本 RFC 不应触及盘后）；
4. 券商侧出现非 SIM 环境订单，或闸计数不变式 `Σ(各闸) == 候选` 被破坏。

**回滚动作**：`.env.us` 置 `false`（下一 tick 生效）。**代码无需回退**（默认 false ⇒ 行为等价）。

## 4. 观测与不变式

- 闸计数主行契约不变：`[ext_entry] 闸计数[session] 候选=N k=int ...`，`Σ == 候选` 必须成立；
- 新增人群**不新增闸计数键**（昨日 AMC 票走同一条门链，归因到既有键）；
- 判据② 对照台账 `ext_entry_earnings_criterion_shadow.jsonl` 增加 `report_date`，逐票-ET日一行（不变）；
- 事件源观测行 `事件源[session] mode=... futu=.. finnhub=..` 语义不变。

## 5. 风险与保守替代

- **风险 1（主要）**：昨日 AMC 的隔夜反应可能已走完 ⇒ 追高/追低。**缓解**：仍走既有涨幅带、量闸、
  价差/深度闸与「不接飞刀」门（本 RFC 不放松任何闸）；观察期先看**成交票的日内路径**再判断。
- **风险 2**：多一次 Finnhub 调用（昨日日历）——**懒加载**，只在确有昨日 AMC 事件票时发生。
- **风险 3**：新人群 = 新的成交来源，可能放大样本噪声。**缓解**：复核日只看 4 条判据，不看单笔盈亏。
- **保守替代（未选，A′）**：先开影子档只记账不下单 5–10 日。**未选理由**：用户裁 A；
  且影子证据在此之前**已被口径漂移污染**（隔夜携带影子曾用扁平 20 万量闸判定，与生产分档不同源，
  已于本批修正），先攒也是攒错口径。

## 6. 变更清单

| 文件 | 内容 |
|---|---|
| `scripts/extended_hours_entry_executor.py` | 开关 `PREMARKET_AMC_PAIRING`、`_shift_et_day`、`_session_event_pairs`、`_earnings_calendar_event` 配对表、`_finnhub_today_calendar(day)`、`_earnings_surprise_calendar_actual(ticker, day)`、`_earnings_surprise` 取事件日、`_finnhub_event_fallback` 逐日、台账 `report_date` |
| `tests/test_ext_premarket_amc_pairing.py` | 7 例契约（默认关 / 配对表 / 匹配矩阵 / 事件日取 actual / 缺 actual fail-closed / union 回落逐日 / 日期不在表内归因） |
| `.env.us` | `EXT_ENTRY_PREMARKET_AMC_PAIRING=true`（gitignore，不入库） |