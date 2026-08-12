# DEB-20260812: entry_reference 动态锚接线 (入场价格机制 Plan A)

> **立案日**: 2026-08-12
> **状态**: 提案裁决中 (未实施; 前置影子 Plan B 已上线 5fb13699)
> **立案人**: Hermes (应用户要求, 供跨模型辩论; 不预设立场)
> **关联**: Plan B 影子 `data/stale_anchor_shadow.jsonl` (record-only, 每日 AFTER_HOURS 打标)

---

## 一、主张拆解

| # | 主张 | 类型 | 现状 |
|---|---|---|---|
| P1 | DA 指令应支持动态锚 (entry_reference 7 枚举), 替代纯静态 entry_price | 主提案 | 未接线 (`entry_reference.py` L13: "Phase 2 才接线") |
| P2 | `pullback_to_support` 必须是默认锚类型; 动态锚 (orb/breakout) 仅限强趋势股 | 保护条款 | — |
| P3 | ORB/VWAP 类锚在盘前生成时**不可解析** (区间未形成), 必须定义解析时机链 | 工程前提 | 无设计 |
| P4 | 接线必须走影子 OOS, 判据预注册后才可动真实触发路径 | 纪律条款 | — |

**争议核心**: P1 的收益是"减少死锚空转" (已证实), 但风险是"把回调接货换成追高成交" (未证伪)。P2 是风险对冲, P3 是工程可行性的硬前提——P3 若无解, P1 的 orb/vwap 两枚举就是空谈。

---

## 二、证据包 (全部实测, 8/12 核实)

### 2.1 现状机制

- 触发: 静态锚 × band [-3%, +5%] (`entry_band.py`) → 成交: MARKET (`playbook_service.py` L1065)
- 既有失效门: drift 5% 作废、极端 gap 8% 逆向作废、出带影子 (`shadow_portfolio_entries.jsonl`)

### 2.2 Plan B 影子回填 (539 笔指令, 6/30→8/11)

| 陈旧分类 | n | 触发率 | 有成交 | 说明 |
|---|---|---|---|---|
| too_low (锚太保守) | **51** | **0%** | 0 | 价格全天高于锚, 等不到回调 |
| too_high (锚太乐观) | 12 | 0% | 0 | 追不上 |
| in_band | 114 | 44% | — | 正常区间 |
| below/above_center | 23 | — | — | 半带内 |
| **no_data** | **339 (63%)** | — | 215 | ⚠️ 无遥测观测价: 多数是快速触发成交, 未进 miss 观测窗 |

**too_low 偏离分布**: p25=-8.3%, med=-6.3%, p75=-5.1% (锚比实际价低 5-8%)
**典型**: US.BAC 7/15 锚 $58.18 vs 观测 $61.2-61.9; LLY 8/10 锚 $1135 vs $1207+; NEM 锚 $92.4 vs $111+

⚠️ **方向归一化 (2026-08-12 复审补)**: 上表是价格空间标签 (打标器初版 `_classify` 未用 direction, LONG-only 语义)。交易空间语义: BUY.too_low ≡ SELL_SHORT.too_high = **conservative_miss** (锚太保守, 趋势错过)。归一化后强陈旧 = 49 BUY.too_low + 9 SS.too_high = **58 (非 51), 触发率同为 0%**——病症结论不变且更强。镜像 optimistic_miss = 5。打标器已补 `stale_dir_normalized` 字段, 既有 539 行已回填 (新代码全量再生与原分类位一致)。

⚠️ **口径声明**: 539 的分母只覆盖"进入轮询观测窗的指令"; 快速触发的 215 笔不在陈旧统计内。conservative_miss 58 笔 (旧口径 too_low 51) 证明**锚空转** (事实), 不证明"按观测价入场会盈利" (反事实不可观测)。

### 2.3 要保护的核心资产: 回调接货优势

⚠️ **口径修正 (2026-08-12 复审)**: 原文"已执行 32 笔全部在锚点或锚点下方成交 (median -1.63%, max +0.00%), 净 +$235"**不可复现**——任何可复算口径下 max 均 >0 (已执行集本身存在追高成交), 净额随窗口大幅波动。可复算口径 (playbook_instructions: execution_price vs entry_price; BUY=(成交价−锚)/锚, SHORT 取反; 正值=比锚差):

| 口径 | n | 偏离中位 | max | ≤锚占比 | 净PnL | 胜率 |
|---|---|---|---|---|---|---|
| 全部已执行 | 273 | 0.00% | **+3.76%** | 67% | -$1298 | 51% |
| 回填窗 6/30→8/11 | 233 | -0.00% | +3.76% | 62% | +$47 | 53% |
| 最近 32 笔 (8/5→8/11) | 32 | -0.01% | +0.16% | 78% | -$39 | 53% |

(trade_records.entry_price 交叉验证同向: med≈0, max +1.67%。)

**修正结论**: "多数成交 ≤ 锚 (中位≈0)"成立; "全部 ≤ 锚"不成立——最高 +3.76% 的追高成交恰是本 DEB 要防的病。保护条款由"铁律"降级为"多数倾向", 阶段 2 的 chase kill switch 由防线升为刚需。
**第一性原理** (不变): DA thesis 是"回调承接型"——锚下方接到=thesis 兑现=赚钱; 追高成交=thesis 未兑现。任何接线方案不得破坏这一优势。
复算: `SELECT (execution_price-entry_price)/entry_price*100 FROM playbook_instructions WHERE executed=1 AND execution_price>0` (SHORT 取反)。

### 2.4 同族病例 (出带影子, n=4)

LLY 8/10 out_of_band、HON 8/10 chase、LLY 8/11 chase、ORCL 8/11 out_of_band——死锚在趋势日的两种死法: 等不到 (out_of_band) 或追价 (chase)。

### 2.5 今日 (8/12) 实例

- **AVGO**: 锚 $403 vs 盘前 $421 (-4.3%) → too_low 型, 若开盘不回踩 403 则空转
- **HON**: 锚 $239.5 vs 盘前 $231 (+3.7%) → 锚悬空在已破位支撑区, 若反弹回 band 会在破位区接刀 (8/11 ORCL 再入场 -$58 同款剧本)

### 2.6 entry_reference 已有能力 (`entry_reference.py`)

7 枚举: market / pullback_to_support / breakout_above_resistance / vwap / ma20 / ma50 / orb
安全回退链: 引用源缺失→market→price→(0.0, no_price); 未知枚举按 market。levels 由调用方注入 (pivotal_detector/orb/atr_provider), 低耦合可测。**代码已存在且有单测, 缺的只是 DA 路径调用。**

---

## 三、辩论点 (反方必须攻击的 5 处)

1. **机会成本 ≠ 损失**: too_low 51 笔是"错过的机会", 不是"亏掉的钱"。动态锚提高触发率, 但触发的是追高单——现有数据里追高 (chase/out_of_band) 恰恰是亏损族。反方要求: 给出"动态锚成交价质量"的正向证据, 否则接线可能把 0 触发变成负期望触发。
2. **锚类型由谁决定**: LLM 选枚举 (幻觉风险: 给回调股配 breakout) vs 规则映射 (盘前涨幅>X% → orb)? 若 LLM 选, prompt 约束+枚举校验够不够? 若规则选, 规则本身需要多少样本标定?
3. **P3 时机链**: DA 盘前生成指令时, ORB/VWAP 当日值不存在。orb 锚必须等开盘 30min (且 blackout 到 10:15) 后才解析——指令"武装"与"锚解析"的时序如何设计? 解析失败 (无 ORB 数据) 回退 market 是否等于变相市价追单?
4. **样本量**: too_low n=51 是 6 周回填; OOS 期日增量约 1-3 笔。接线裁决需要多少 OOS 样本才够? 参照系: blackout DEB 用 n=8 首波齐射做判据, 被自评"显著性薄"。
5. **简单替代**: 不接线, 仅把 band 上限 +5% 放宽 (或锚生成时用盘前价修正), 能否以 1/10 复杂度拿到 80% 收益? 反方须量化: 若锚在盘前重校准 (anchor × 盘前价/昨收), 51 笔 too_low 里多少笔会回到 band 内。

---

## 四、观察点 (影子持续积累, 裁决日读数)

1. `stale_anchor_shadow.jsonl` 日增 conservative_miss (方向归一化) 笔数与偏离分布 (确认非回填偶然)
2. 今日 AVGO/HON 实况: AVGO 是否空转、HON 是否在破位区触发 (P1 的现实案例)
3. `shadow_portfolio_entries.jsonl` 出带/chase 新增 (动态锚要解决的病是否持续发生)
4. 已执行单成交价 vs 锚偏离 (回调接货优势是否保持, median 应 <0)

---

## 五、预注册判据 (若辩论裁决"接线", 分两阶段)

**阶段 1 — 病症确认 (8/12→8/26, 10 交易日, 纯影子)**:
- OOS 期 **conservative_miss** (方向归一化: BUY.too_low + SELL_SHORT.too_high) 新增 ≥10 笔 且触发率 ≈0% → 病症持续, 进入阶段 2 设计
- OOS 期 conservative_miss 新增 ≤5 笔 → 回填的 58 笔含大量特殊行情, **不接线** (维持现状+band 观察)
- *(判据修正 2026-08-12: 原"too_low"漏 SELL_SHORT 镜像病 (9 笔); OOS 首日凌晨修正, 未读任何 OOS 数据)*

**阶段 2 — 接线影子 OOS (实施后 n≥30)**:
- 双臂记录: 动态锚解析价 vs 原死锚, 各自触发/成交/结局
- 判据: 动态锚臂触发后成交价 median ≤ 锚 (不追高) 且净 PnL ≥ 死锚臂 → 转正
- 止损: 动态锚臂出现 3 笔 chase 型亏损 (成交价 > 锚+1%) → 暂停接线回影子
- *(复审注 2026-08-12: 死锚臂对错过集 PnL≡0, "净 PnL ≥ 死锚臂"近乎恒真; 启动阶段 2 前须补 WR 下限 (建议 ≥40%) 与单笔 chase 亏损上限, 具体值由辩论双方裁定)*

**回滚**: 接线未实施, 无运行时回滚需求; 若实施后回滚 = revert 接线 commit + 保留影子打标。

---

## 六、裁决请求

请辩论双方就 P1-P4 逐条表态, 重点回应辩论点 1 与 3。禁止只引用本文件结论——
too_low 统计可自行复算: `python3 scripts/stale_anchor_shadow_tag.py` (幂等) +
`data/stale_anchor_shadow.jsonl`。

---

## 七、修正记录 (2026-08-12 复审落地)

1. **§2.3 回调接货优势不可复现** (Qoder 独立复核发现): 原"32 笔 median -1.63% / max +0.00% / 净 +$235"在任何可复算口径下不成立, 已改写为三口径可复算表——"多数 ≤ 锚 (中位≈0)"成立, "全部 ≤ 锚"证伪 (已执行集存在 max +3.76% 追高成交)。保护条款降级为多数倾向, 阶段 2 kill switch 升为刚需。
2. **方向归一化**: 打标器初版 `_classify` 为 LONG-only 语义 (direction 参数未使用), SELL_SHORT 镜像病 (too_high, n=9) 被漏统。已补 `stale_dir_normalized` 字段 (conservative_miss=58, 触发率 0%), 既有 539 行已回填, 新代码全量再生与原分类位一致; 阶段 1 判据同步改归一化口径 (OOS 首日凌晨修正, 未读任何 OOS 数据)。
3. **死代码清理**: 删 `_load_price_paths` 的 anchors 累积器 (载入后从未消费)。
4. **阶段 2 判据恒真风险**: 已加注 (死锚臂 PnL≡0), WR 下限与 chase 亏损上限留待辩论双方定值。

---

## 八、独立复查记录 (2026-08-12 Hermes, 逐行读码+独立复算)

对 §七 四项修正逐条独立复算 (不信描述, 只信 DB/jsonl 实证):

| 项 | Qoder 声称 | 复查值 | verdict |
|---|---|---|---|
| conservative_miss | 49 BUY.too_low + 9 SS.too_high = 58, 触发 0 | 58 / 触发 0 / 执行 0 (jsonl 逐行复算) | ✅ |
| optimistic_miss | 5 (3 BUY.too_high + 2 SS.too_low) | 同 | ✅ |
| 归一化字段回填 | 539 行全补, 与映射一致 | 539/539 一致, 去重键 0 重复 | ✅ |
| 标签再生一致性 | 全量再生与原分类位一致 | 用落盘 obs_lo/obs_hi/obs_med 反算 200 行 0 mismatch | ✅ |
| §2.2 表 | too_low 51 / in_band 114 (44%) / no_data 339 | 全部复现 | ✅ |
| §2.3 行1/行2 | n=273 max +3.76% 净 -$1298 / n=233 +$47 | 逐字复现 (含 COIN 7/2 +3.76% 追高单实锤) | ✅ |
| §2.4 出带影子 | 4 例 | shadow_portfolio_entries.jsonl 恰 4 行, 日期/标的/entry_path 全对 | ✅ |
| §2.6 七枚举 | 代码已存在有单测 | entry_reference.py L21-27 七枚举 + 回退链确认 | ✅ |
| 死代码清理 | anchors 累积器已删 | grep 零残留 | ✅ |

**复查发现的出入 (不影响主结论, 口径需补注)**:
1. §2.3 "≤锚占比" 67/62/78% 无法用单一容差复现: 严格 dev≤0 得 66.3/60.9/71.9%; 容差 0.0005 得 67.4/62.2/75.8%; 行3 的 78% 需容差 0.005。三行隐含容差不一致且未注明。"多数 ≤ 锚、中位≈0" 的方向结论不受影响。
2. §2.3 "最近 32 笔" 行集规则未注明: 8/5→8/11 窗口实际有 **33** 笔已执行, Qoder 的 pnl=-$39 恰等于剔除 US.PLTR 8/5 (id 最小, pnl -$59.3) 后的集合——疑似 ORDER BY id DESC LIMIT 32。该 32 笔窗口的净值对行集选择敏感 (-$39 vs 全 33 笔 -$99), 引用时须注明规则。
3. §2.2 no_data "有成交 215": 复查得 executed=223; 215 = executed 且 skip_reason 为空。口径未注明, 建议改 223 或加脚注。
4. §2.2 窗口标签 "539 笔, 6/30→8/11" 与文件实际跨度 (6/18→8/12) 不符: 表内计数是全文件口径 (窗外 69+6 行全为 no_data), 陈旧分类数不受影响, 但标签应改 "全文件" 或收窄计数。
5. §2.2 典型案例 NEM "观测 111+": jsonl 落盘 obs_hi=106.36 (8/6, 锚 92.38, too_low 成立), 111+ 无法从落盘数据复现, 引用时以 obs_hi 为准。

**复查实锤的真 bug (已修, 本轮最重要的发现)**: **OOS 首日样本早产污染**。
原回填 run (北京 20:11 ≈ ET 08:11, 盘前) 把当日 6 条指令 (GS/BWXT/GOOG/AVGO/CRM/HON) 在遥测尚空时打成 no_data 落盘, executed=False 冻结; 其中 **BWXT/GOOG/AVGO 随后 ET 10:18 实际成交**。幂等 seen-keys 会让盘后重跑跳过这 6 行 → OOS 首日样本 (含 §2.5 点名的 AVGO/HON) 永久锁死成 no_data。
修复: ① 移除 6 条早产行 (备份 data/archive/stale_anchor_shadow.jsonl.bak-20260812-premature-oos-fix), 今晚 AFTER_HOURS 用全天遥测重新打标; ② 脚本加源头守卫: 指令日期 D 只在 D 的 ≥16:05 ET 后才打标 (手动盘中运行自动延迟当日指令), 已实测生效 (ET 10:53 重跑零当日落盘)。阶段 1 判据的 OOS 计数从今晚重打标起算。
