# DEB-20260804-factor-system-audit: 因子体系三层断层 — 伪因子/错尺子/断反馈 + 五处价值遗漏 (用户令"写入DEB, 多模型核查")

> **状态**: 🔶 新起案 (作者 Qoder) · **自查第3轮已完成** (C3 重算 · intraday IC · regime 分拆 · BH 多重校正 — 分歧#1-#4 全部了结; 黑名单三次收窄至 insider_net_buy 硬名单) · **待多模型+agent 核查证伪** → 待用户裁决
> **分类自评**: 修尺子+堵源头为主 (F2/F3 零 live 信号变更; F1 影响 DA prompt 内容; F4 只读审计; F5 只读清洗)
> **用户命题**: "微观结构之外, 基于已有数据和量化交易因子当前设置是否合理, 是否有遗漏最优价值" (2026-08-04)
> **前置已执行**: MFE_STALL 21 笔反事实 → P1 (低ATR门 0.025→0.030) + P2 (浅亏持有) 已于 06:21 重启激活, 见 handoff 2026-08-04 首节。本案是其后续深挖。

## 一、为什么有这道题

今日盘后复盘确认系统连续正期望 (08-03 净+$58.69, 每笔+$5.87), 但 MFE_STALL 深挖暴露"检查点卖在午餐低谷+低波票贴线入门"两处结构性失血。用户追问: 把微观结构 (时段/费用/点差) 排除后, **因子体系本身**是否合理。审计发现的问题比预期严重: 评估层、定义层、反馈层各有一处断层, 且存在可量化的稳定亏损模式在持续交易。

## 二、核心主张 (C1-C6, 每条附证伪方式 — 核查方请逐条挑战)

### C1 · f1-f5 是关键词 0/1 伪因子, 真数值因子"算了就扔"
- **证据**: `app/playbook/playbook_service.py:1293-1297` — f1_trend/f2_momentum/f3_volume/f4_orderflow/f5_lifecycle 全部 = 对 `conditions_json` 文本做关键词匹配的 **0/1 标记** (如 `"ma" in c or "macd" in c`)。
- 同时 `app/data/market_data_router.py` 每周期计算完整数值上下文 (RSI/MACD/ATR/VWAP偏离/volume_ratio/order_flow_imbalance, 见 ts_ctx 注入段 ~L471-680), 仅 price 进触发判断, 其余进 DEBUG 日志, **从不落库、从未进 IC 检验**。
- **证伪方式**: 找出 f1-f5 作为数值 (非 0/1) 参与评分/加权的代码路径; 或找出 ts_ctx 数值因子落库/进 IC 的现有管道。找到即推翻。
- **补充 (二值 IC 的正确读法)**: 对 0/1 变量算 Spearman/ Pearson IC 等价于 point-biserial 相关 — 其符号含义仅为"**命中该关键词的交易组 vs 未命中组的收益差**", 不含任何数值灵敏度信息 (无法区分 RSI=31 与 RSI=85)。因此 f1-f5 全负 IC 的准确解读是"这些关键词命中的指令组近期跑输", 而非"趋势/动量/量能因子无效" — 这恰恰是 C1 的核心: 尺子量的不是因子, 是词。

### C2 · factor_ic 用 T+1 尺子量日内系统 → "全负 IC" 含口径噪声
- **证据**: `scripts/factor_ic_backfill.py:103-116` — IC 配对 = (raw strength, `fwd_ret_t1` 方向调整后)。本系统设计上当日必平 (PRE_CLOSE/TIME_EXIT), T+1 收益与交易决策无关。
- 实测 (data/ic_analysis_latest.json, 90 日窗): f1 -0.099 / f2 -0.064 / f3 -0.076 / f4 -0.090 / f5 -0.075 全负; 但同表 SHORT 方向 ic_t5=**+0.1275** — 同一信号 T+1 负 T+5 正, 说明 horizon 选择直接翻转结论。
- **关键限定 (自评弱点)**: C2 不声称"因子其实很好", 只声称"现有全负 IC 不能作为因子死刑证据"。日内系统的正确尺子是 signal→close (数据已存在: ~~signal_snapshots.close_t0~~ **勘误: signal_snapshots 表不存在**, 实际为 `playbook_instructions.fwd_return_intraday` + open 锚定孪生列 `fwd_return_intraday_open`, ic_tracker 已回填 424 条)。
- **证伪方式**: 论证 T+1 IC 对当日平仓系统仍有决策含义 (例如作为隔夜风险评估), 或证明 intraday IC 与 T+1 IC 在本样本高度一致 (则口径不重要)。
- **🔴 自查第2轮已跑 intraday IC (2026-08-04, /tmp/intraday_ic.py, n=424 全信号)** — 结论对 C2 不利:
  - **口径陷阱先行确认**: `fwd_return_intraday` 是 entry_price (limit 价) 锚定, 未成交指令的 limit 价不可成交 → 制造纸面 edge (entry 锚定全样本均值 +1.0%, **open 锚定孪生列跌到 ≈0.0%**, 与 ic_tracker.py:223 注释警告一致)。任何用 entry 锚定评"信号质量"的分析都需 open 锚定复核。
  - **open 锚定下, 关键词条件的 intraday 组差普遍平-负**: insider_net_buy -0.68%, rsi_overbought -0.83%, rsi_oversold -0.62%, macd_golden_cross -0.60%, news_positive -0.37%, earnings_beat_streak -0.24%; 仅 sentiment_bullish +0.47% / volume_expansion +0.23% 为正。
  - **判决**: "全负 IC 只是 horizon 噪声"的辩护**不成立** — 换成正确尺子 (intraday, open 锚定) 后信号层依然无正 alpha。C2 保留的部分: T+1 IC 的具体数值确实不可比 (量纲/幅度不同), 但**方向结论一致**。分歧#1 → 信号层问题坐实, f1-f5 修尺子 (F2/F3) 之外, DA 条件生成质量本身是更上游的问题 (归 F1 管辖)。

### C3 · 条件级双窗口实证: 稳定亏损模式在持续交易

**✅ 干净重算已完成 (2026-08-04, 作者自查, 脚本 /tmp/c3_clean_recompute.py)** — 严格 JSON 解析 conditions_json + metadata_json.playbook_instruction_id 精确 join (fallback 仅当日单标的唯一指令) + set 去重防爆 + 5000 次 bootstrap CI。**旧 CTE 表作废, 以下为准。** 无法归因交易: era 35/147 (24%) / 全史 89/272 (33%) — 多为 MAIN_PIPELINE 单与多指令日, 不计入条件表。

era 窗 (2026-07-14→08-03, 147 笔可归因 112):

| condition | n | 净额$ | $/笔 | bootstrap CI95 | 判决 |
|---|---|---|---|---|---|
| news_positive | 102 | -692.88 | -6.79 | [-13.18, **-0.30**] | ⚠️ 单测 CI 排除零, 但 **BH 校正差 0.001 未过** → 软黑名单 |
| insider_net_buy | 36 | -456.66 | -12.68 | [-21.97, **-4.09**] | ✅ CI 排除零 + BH 校正通过 (p=0.002) → **唯一硬黑名单** |
| pe_below_industry | 73 | -371.64 | -5.09 | [-12.76, +2.38] | 方向确认未显著 + **regime 翻正** → 移出名单, 观察 |
| above_ma20 | 15 | -186.56 | -12.44 | [-25.68, +0.71] | 追趋势入场失血 (n 小) |
| earnings_beat_streak | 59 | -35.84 | -0.61 | [-9.21, +8.07] | 成交层最优 ≈breakeven, 但**信号层双 regime 负** → 仅不拉黑, 非偏好 |

全史窗 (2026-06-01→08-03, 272 笔可归因 183):

| condition | n | 净额$ | $/笔 | bootstrap CI95 | 判决 |
|---|---|---|---|---|---|
| pe_below_industry | 99 | -2476.48 | -25.01 | [-60.17, +6.93] | 最大失血源 |
| news_positive | 129 | -2078.92 | -16.12 | [-36.41, **-0.33**] | ✅ CI 排除零 |
| rsi_oversold | 7 | -943.91 | -134.84 | — | ⚠️ **单票主导** (LMT -$605.87), 全为 6 月样本, era 零交易 → 观察名单, 不入黑名单 |
| insider_net_buy | 40 | -733.84 | -18.35 | [-58.26, +15.12] | 双窗口同号确认 |
| volume_expansion | 20 | -619.34 | -30.97 | [-147.36, +48.29] | 旧表 -$66.9 系 join 噪声; 仍负但 CI 宽, 维持"存疑不可裁" |
| earnings_beat_streak | 64 | -68.94 | -1.08 | [-9.05, +7.16] | 双窗口最优 (旧表 +$3.1 系 join 噪声, 干净值 ≈ -$1/笔 breakeven) |

- **对旧表的修正**: ①earnings_beat_streak 从"全史+$3.1/笔盈利"下修为"≈breakeven" — "唯一双窗口不亏"表述不成立, 准确表述为"**双窗口每笔期望均为全条件最高, era 窗损失仅为次优条件的 1/8~1/20**"; ②volume_expansion/pe_below_industry 每笔亏损被旧 join 夸大 ~2x, 结论方向不变。
- **显著性检验 (对决分歧#2)**: era 窗 earnings_beat_streak vs insider_net_buy 每笔差 bootstrap — **P(差≤0)=0.024**, CI95=[0.05, 24.89] → 差异显著, F1 的"黑名单 vs 偏好"区分**通过统计检验**。
- **自评弱点① 仍有效**: era 窗整体是 OKR-07 MISSED 亏损期, 有效信息在相对量级。
- **新增弱点④**: insider_net_buy 与 pe_below_industry/news_positive 高度共现 (同一指令常同时命中), 表中各条件的亏损有重叠归因 — 黑名单真正的作用是"拒绝含这些条件的指令组合", 而非单条件独立效果。
- **新增弱点⑤ (选择效应)**: factor_ic_latest.json 里多个 pattern 的 IC 基于全部**生成信号** (如 product_catalyst n=382) 但 **0 笔成交** — C3 表只覆盖"信号→触发→成交"漏斗的最终子集。条件表为负≠条件本身负, 也可能是触发层把该条件的好交易过滤掉了。核查方若要彻底, 应对黑名单候选条件补算"生成未成交"信号的反事实收益。
- **新增弱点⑥ (regime 依赖)**: 全窗口跨 BULL→震荡市切换 (07-23 暴跌), era 窗集中在震荡段 — 条件期望可能是 regime 条件期望, 非无条件期望。黑名单应注明"当前 regime 下", 并随 market_regime 变化复审。

### C4 · 置信度反向: 高分信号系统性更差, min_conf=0.70 门未审计
- **证据**: ic_analysis_latest.json — signal_strength IC -0.039 / signal_confidence IC -0.045 / consensus_level IC -0.018 (全负); 04:00 复盘 job 独立发现: **confidence 90+ 桶 23 笔仅 26% 胜率** (50-69 中档 63%)。
- **推论 (待证)**: 若低分信号更好, 则 `min_conf=0.70` 门 (unified_config 配置SSOT) 在系统性滤好留差。sizing 按 confidence 加权已于前期拍平 (正确), 但**门本身从未做过反事实审计**。
- **自评弱点**: "被挡信号"的反事实尚未计算 — 需要 gate_decisions/扫描落库里的 BLOCK 记录 + 其后续 intraday 表现。IC 负相关也可能只反映"高分单集中在追涨场景"的构成效应, 而非因果。
- **证伪方式**: 计算被挡信号 (conf<0.70) 的 intraday 收益分布 vs 通过信号; 若无显著差异或更差, C4 不成立。
- **🔴 自查第2轮快速反事实已跑 (2026-08-04, open 锚定, n=424)** — **C4 原推论"门在滤好留差"被部分证伪, 修正如下**:
  - confidence 分桶 (open 锚定 intraday): [0,70) **-0.03%** (n=252) / [70,80) +0.69% (n=37) / [80,90) +0.21% (n=34) / [90,101) **-0.03%** (n=101) — 呈**倒U形**: 中档 70-90 最好, 两端最差。
  - **min_conf=0.70 门本身无罪**: 被挡组 (-0.03%) 不优于通过组加权 (+0.17%), "降门放行低分信号"不可取; entry 锚定下被挡组的 +1.67% 是 limit 价纸面 edge (见 C2 口径陷阱), 此前 IC 全负的"低分更好"错觉主要源于此。
  - **真问题在顶端**: 90+ 桶 (n=101, 占通过信号 59%) open 锚定 -0.03%, 显著劣于 70-90 中档 — 与 04:00 复盘"90+ 桶 26% 胜率"互相印证。可操作方向变为: **对 conf≥90 的指令降权/降 size 或要求额外确认**, 而非动 0.70 下门。
  - 注意: [70,80)/[80,90) 桶 n 仅 37/34, 倒U形的顶端差需更多样本或 bootstrap 确认后才可入裁决。
  - **🔴 第4轮 (2026-08-04) bootstrap 已出: 顶端差不显著 (p=0.203), 详见 F4 节 — C4 的可操作结论最终收敛为"门不动、顶端不降权、维持观察"。**

### C5 · (已执行, 追认复核) MFE_STALL 检查点系统性卖在午餐低谷
- 21 笔全样本: 实际 -$732.42; 持到收盘 +$158.22 (13/21 改善); 退出后 30min 内 10/15 反弹; T+1 开盘 -$92.92 更差 (隔夜不采纳); T+1 收盘 +$519.44 但肥尾 (ORCL 一笔+$252 占半)。
- 入场侧: 2.5-3.0% ATR 桶 21 笔净 -$105.6 (62%WR 被费用吃光); 3.0-3.8% 桶唯一净正 +$31.78。→ P1/P2 已落地。
- **复核请求**: 反事实用日 K 收盘价, 忽略滑点与盘中流动性; "持到收盘"在 PRE_CLOSE 窗口 (15:52-16:00) 的实际成交价可能系统性劣于日 K 收盘。请用 position_paths 1-min bar 重算精确 PRE_CLOSE 反事实。
- **🔴 复核已出 (2026-08-04)**: `scripts/preclose_counterfactual.py` 用 Futu 1-min K VWAP (15:52-16:00 ET) 重算: 反事实@VWAP **-569.75** vs 反事实@日K收 -574.20 vs 实际 -732.42 → VWAP 口径比日K收**还好 $4.45** (均值 +4.79bps), 担忧的尾盘流动性折价在样本内不存在。**C5 判决维持, P2 浅亏持有政策无纸面水分**。残余误差仅市价单冲击成本 (SIM 单笔 ~$3.5k, 可忽略)。

### C6 · SHORT 信号 T+5 IC +0.1275 — 多头日内系统漏收空头多日 alpha
- **证据**: ic_analysis_latest.json ic_by_direction: SHORT ic_t1=-0.109 但 ic_t5=+0.1275 (n=1235), LONG ic_t1/t5 双负。
- **解读**: 空头信号方向预测力在 5 日尺度成立, 但系统当日平仓只吃噪声段。与隔夜纪律 (DEB-20260802-short-overnight-hold) 冲突 — **本案仅记录, 不建议立即开隔夜仓**; 若未来设 swing 轨道, SHORT 候选为第一优先。
- **证伪方式**: 检查 SHORT 样本的 era 稳定性 (是否集中于 07-23 暴跌日附近的几笔); 若集中于单周, 结论降为" regime 特定"。

## 三、建议清单 (F1-F5 裁决点, 按期望价值排序)

**F1 · condition 级期望反馈注入 DA prompt (堵源头, 推荐最先做)**
- 内容: 每周 (或每日 05:00 记忆更新时) 由脚本生成 condition×era 净期望表, 作为 DA prompt 的固定段; dynamic_blacklist.json 目前是 symbol 级且为空, 本提案是 **pattern 级负期望清单**, 互补。
- **⚠️ 第3轮 regime 分拆 + BH 校正后名单修订**: 硬黑名单仅 **insider_net_buy** (双 regime 同号负 + BH 校正唯一通过 p=0.002); **news_positive** 为软黑名单 (p=0.018 差 0.001 未过校正, 但双 regime 同号负 + 交易层 CI 排除零, 保留标注); **pe_below_industry 移出** (BULL -0.42% 但震荡段 +0.40% 翻正, regime 依赖, 降级观察); **earnings_beat_streak 从"偏好"降级为"仅不拉黑"** — 信号层双 regime 均为负, 其"最优"地位只在成交层成立, 不足以正向引导。
- 前置依赖: ~~C3 的 era 表经核查方干净重算确认~~ **已完成** (含 regime 分拆); 核查方转为复核脚本逻辑 (/tmp/c3_clean_recompute.py) 与 regime 分拆口径。
- **设计抉择**: (a) **prompt 级**: 给 DA 看期望表让模型自行规避, 软约束; (b) **门级否决**: 硬挡含黑名单条件的指令, 但触发层目前只看 price cross 不评估条件, 需新增解析。建议先 (a) 观察 2 周, 若模型不理会再升 (b)。
- 风险: 样本期内过拟合 — era 仅 ~15 个交易日; 缓解: 滚动 30 日窗 + "n≥8 且双窗口同号**且跨 regime 同号**"才入黑名单 + regime 标签随市复审。
- 回滚: 删 prompt 段。

**F2 · 触发时真因子值落库 (修定义层, 零风险)**
- 内容: 触发落库时在 trade_records.factor_detail (已有 JSON 列) 扩展写入 ts_ctx 快照 {rsi, macd, atr_pct, vwap_dev_pct, volume_ratio, order_flow_imbalance}; 只加字段不改行为, 2 周后首次可算真数值 IC。
- 回滚: 停写 (历史数据保留无害)。

**F3 · factor_ic_backfill 增 intraday horizon (修尺子, 零风险)**
- 内容: IC 配对增加 (strength, signal→close 收益) 列, 与 T+1 并列输出; 数据源 ~~close_t0/price_at_signal 已在 signal_snapshots~~ **勘误: `playbook_instructions.fwd_return_intraday` + `_open` 孪生列** (ic_tracker 已回填, 424 条)。
- 回滚: 删列。

**F4 · ~~min_conf 门反事实审计~~ → ~~conf≥90 顶端桶降权提案~~ → 提案撤销 (bootstrap 未过门)**
- 审计快速版已跑: 0.70 下门无罪 (被挡组不更好); 原观察 conf≥90 (n=101, open 锚定 -0.03%) vs 70-90 (n=71, +0.46%)。
- **🔴 bootstrap 显著性已跑 (2026-08-04, /tmp/f4_bootstrap.py, 预注册门径未过)**:
  - 差值 (70-90 减 90+) = +0.484%, pooled bootstrap B=10000 → **p=0.203, 不显著**; 90+ 桶 CI95 [-0.54%, +0.48%] 横跨零 (与无差异不可分), 70-90 CI95 [-0.12%, +1.05%] 同样含零。
  - **"跨 regime 稳定倒挂"被部分推翻**: cautious 段 90+ -0.008% vs 70-90 +0.697% (倒挂存), 但 risk_on 段 90+ +0.254% vs 70-90 +0.119% (**顶端反而更好**)。
  - 构成效应排查: 90+ 桶 above_ma20 占比 51% vs 70-90 的 20% (追涨集中), 方向 53/48 vs 42/29 基本均衡, symbol 无单票垄断 (最高 CRM/NFLX 各 6 笔)。构成差异存在但不足以支撑因果。
  - **判决**: 按本 DEB 自设门径 ("落地前先补 bootstrap 显著性") — 证据不足, **conf≥90 降权不实施**, 0.70 下门维持, confidence 维持原样 (sizing 早已拍平, 无 live 影响)。保留观察: 若 90+ 桶样本翻倍后差值仍不显著, 此议题关闭。
- 回滚: 无 (未动代码)。

**F5 · volume_expansion 清洗重判 (只读) — ✅ 已完成 (2026-08-04, /tmp/f5_volume_expansion.py)**
- 全史 n=12, WR 42%, total -$803 (avgHold 9.28h, 含隔夜/ghost 仓); 剔除 ≤07-08 旧账后 **era 仅剩 n=2** (07-09 CAT/META, PRE_CLOSE_EXIT, 0-0.1h 即退, 合计 -$4.2)。
- **判决**: ① 原疑点 "0.5h time_exit 执行杀" **不成立** — era 内无 time_exit, 2 笔均为快速 PRE_CLOSE; ② -$803 全部来自旧账 era (含 BROKER_GHOST_CLOSE/EOD_FORCE_CLOSE/隔夜持仓), 与当前 playbook era 执行路径无关; ③ era 样本枯竭至 n=2, **无法判定好坏** — 维持"明确不做"名单: 不进黑名单, 攒样观察。

**明确不做**: 隔夜/swing 仓 (C6 仅记录); volume_expansion 直接进黑名单 (样本不足); 按当前全负 IC 关停任何因子 (C2 限定)。

## 四、关键分歧 (留给辩论)

1. ~~**T+1 IC 对日内系统有无决策含义** (C2)~~ **✅ 已了结 (自查第2轮, 2026-08-04)**: intraday IC (open 锚定) 已跑, 条件组差平-负与 T+1 方向一致 → "horizon 噪声"辩护不成立, 信号层弱势坐实; 但发现 entry 锚定的纸面 edge 陷阱 (未成交 limit 价), 为后续所有信号质量分析立了口径规矩。
2. ~~**era 相对量级能否支撑黑名单** (C3/F1)~~ **✅ 已了结 (作者自查, 2026-08-04)**: bootstrap 5000 次 — earnings_beat_streak vs insider_net_buy 每笔差 P(≤0)=0.024, CI95=[0.05, 24.89] → 差异显著, 黑名单/偏好区分成立; 但 earnings 本身下修为 ≈breakeven (非盈利), F1 措辞相应调整。
3. ~~**置信度反向是因果还是构成** (C4)~~ **✅ 大体了结 (自查第2轮)**: open 锚定分桶呈倒U形 — 0.70 下门无罪 (被挡组不更好), 真问题是 conf≥90 顶端桶; "低分更好"主要是 entry 锚定构成效应。留给核查方: 顶端差的 bootstrap 显著性 (中档桶 n 仅 37/34)。
4. ~~**(新) DA 条件生成质量是否上游根因**~~ **✅ 已了结 (自查第3轮 regime 分拆, 2026-08-04)**: open 锚定分 regime — BULL 段 (≤07-22, n=265) 全池 **+0.155%**, 震荡段 (07-23+, n=159) **-0.115%** → "alpha≈0" 是**结构性**的 (牛市段也仅 +0.16%, 覆盖不了每笔 ~$4 费用), regime 只是加深。F1 从"堵漏"升格"主修"成立。**两条修正**必须入裁决: ① **pe_below_industry 是 regime 依赖的** — BULL -0.42% 但震荡段 **+0.40%** 翻正, 不得无条件拉黑, 需 regime 标签或降级观察; ② insider_net_buy 双 regime 同号负 (BULL -0.71% / 震荡 -0.58%), news_positive 双负 (-0.28% / -0.41%); 叠加 BH 校正 → 名单为 **insider_net_buy 硬 + news_positive 软**。
5. **(新增) conf≥90 顶端差的结构稳定性已证**: 震荡段 conf≥70 样本几乎枯竭 (仅 5/159) → 90+ 问题桶 (n=101) 几乎全是 BULL 段样本; BULL 段内 90+ 仍仅 +0.05% vs 70-90 **+0.56%** → 顶端倒挂**与 regime 无关, 是 DA 置信度校准的结构性缺陷**。"conf≥90 降权/要求额外确认"提案证据充分 (跨 regime 稳定)。留给核查方: bootstrap 显著性 + 构成效应排查 (90+ 是否集中在特定 condition 组合)。

## 五、核查方任务单 (多模型/agent 请按此执行)

1. ~~**重算 C3 era 表**~~ **✅ 已完成 (作者, 2026-08-04)**: /tmp/c3_clean_recompute.py — 严格 JSON 解析 + 指令 ID join + set 去重 + bootstrap CI, 判决表 2.0 已并入 C3; 核查方转为**复核脚本逻辑与 join 正确性** (重点: metadata_json.playbook_instruction_id 覆盖率 era 76%/全史 67%, fallback 唯一指令规则是否引入偏差);
2. ~~**跑 intraday IC**~~ **✅ 已完成 (作者第2轮)**: /tmp/intraday_ic.py + open 锚定复核, 结论并入 C2; 核查方转为**复核口径** (entry vs open 锚定的选择是否正确, 分 regime 重跑是否翻转);
3. ~~**精确 PRE_CLOSE 反事实** (C5 复核)~~ **✅ 已完成 (作者, 2026-08-04)**: `scripts/preclose_counterfactual.py` — Futu 1-min K (含 volume, autype=NONE) 算 15:52-16:00 ET 窗口 VWAP 作真实成交价: 21 笔 stall 实际 -732.42 / **反事实@VWAP -569.75 (+$162.67)** / 反事实@日K收 -574.20 (+$158.22)。VWAP vs 日K收均值 **+4.79bps** (区间 -28.8~+34.9bps) → **日K收盘口径不仅没高估反而略保守, C5/P2 结论经诚实尺子验证成立**。该脚本同时是 P2 观察期每周尺子 (浅亏持有仓实际成交 vs VWAP)。输出 `data/preclose_counterfactual_latest.json`, bar 缓存 `data/preclose_cf_cache/`;
4. ~~**F4 反事实审计**~~ **✅ 快速版已完成 (作者第2轮)**: 被挡组 [0,70) open 锚定 -0.03% 不优于通过组 → 0.70 门维持; 剩余: conf≥90 顶端桶 vs 70-90 中档差的 bootstrap 显著性;
5. **挑战 C1**: 全库搜索真数值因子的既有落库/评估管道, 找到即推翻 C1 并指出位置;
6. ~~**(弱点⑤) 选择效应反事实**~~ **✅ 初判已完成 (作者第2轮)**: executed=0 组 open 锚定 +0.09% vs executed=1 组 +0.02% — 差异微小, 触发层未见系统性滤好; 核查方可分条件细拆复核;
7. ~~**(分歧#4) 分 regime 重跑 intraday 信号质量**~~ **✅ 已完成 (作者第3轮)**: BULL 段全池 +0.155% / 震荡段 -0.115% → 弱信号是结构性的; pe_below_industry regime 翻正, insider_net_buy/news_positive 双负; 震荡段 conf≥70 样本枯竭 (5/159) → 90+ 顶端差是 BULL 段内稳定现象。结论并入分歧#4/#5 与 F1/F4;
8. ~~**(剩余) conf≥90 vs 70-90 顶端差的 bootstrap 显著性 + 构成效应排查**~~ **✅ 已完成 (作者, 2026-08-04)**: /tmp/f4_bootstrap.py — p=0.203 不显著, 90+ CI 横跨零, risk_on 段不倒挂; 构成上 90+ 桶 above_ma20 追涨集中 (51% vs 20%) 但不构成因果 → **F4 降权提案撤销**, 详见 F4 节。
9. ~~**(多重比较警示)**~~ **✅ 已完成 (作者第3轮, BH 校正)**: era 窗 6 条件 bootstrap p(均值≥0): insider_net_buy **0.002 (BH 阈值 0.008, 唯一通过)** / news_positive 0.018 (阈值 0.017, **差 0.001 边缘失守**) / above_ma20 0.032 / pe_below_industry 0.096 / sentiment_bullish 0.307 / earnings_beat_streak 0.445 → **F1 黑名单再收窄: insider_net_buy 硬名单; news_positive 软名单 (边缘 + 双 regime 同号负, 保留但标注未过校正)**。
10. **(移交接收, DEB-ruler §六裁定 2026-08-04) S7 · 触发条件有效性立案**: 06-29 从 MA20 突破改为**当日高低点突破+量确认**后从无有效性评估。对照组天然存在: `data/trigger_miss_telemetry.jsonl` (07-11 起, 未触发原因+gap)。**混杂警告 (裁定附)**: 触发 vs 未触发非随机分组 (未触发者 setup 本身不同), 必须按 miss 原因码分层或倾向得分匹配, 裸比会产生选择偏差假阳性。关联观察: 开盘30min 占 era 入场 84% 是"高低点突破+5min轮询"的机械必然 (高点早段形成早段被破), 与尺子DEB事实#7"裸持回放各时点皆负"自洽 — 真命题是"开盘段的突破是否退化为对开盘噪声的随机抽样"。门径规范: 双侧 null-pooled bootstrap + MDE/功效标注 (尺子DEB §六6.2/6.5 通用规范)。
11. **(移交接收确认) 尺子DEB事实#7 三分版**: ①裸持回放 (无费无滑点任意时点) 均值全负 → **指令方向性 edge ≈ 0, 信号生成侧承接举证**; ②实盘退出栈剔暴跌日毛 +272.5 (n=101) → 退出栈在加毛价值, 不能笼统怪信号; ③净亏构成 = 暴跌3日 -636.6 (61%) + 费用 -676.1 (65%) - 剔暴跌毛盈 +272.5。信号侧要回答的是①, 同时②③界定了它的量级上限: 信号修复不改费用与 regime 集中度。
12. **(新立, 完整性审计 2026-08-04) regime 过滤假说 — 暴跌日事前可检测性**: era 净亏 61% 集中于 07-21/22/23 三个暴跌日 (毛 -636.6), 是单项最大杠杆但此前无任务承接。**⚠️ 事后谬误警告**: "剔掉这 3 天" 是 hindsight — 真命题是"暴跌日是否存在**事前可观测**状态 (指数 breadth/VIX/前日 regime 标记等), 据此降频/降仓能否在样本外保住 +272.5 毛盈"。门径 (按新规范预注册): ①先落检测器原型 + 事前特征集, 冻结判定规则再检验; ②双侧 null-pooled bootstrap; ③MDE 标注: 3 事件/era 功效极低, 须先积累 regime 事件数或改用日频面板口径, 功效不足则显式写"冻结至 X"; ④pnl 毛/净双列。归属: 本辩 (信号/regime 侧); 与 F-S4 尺寸杠杆 (A-ex) 互为前置。

## 六、时间锚

- 分析窗口: trades 2026-06-23→08-03 (全史) / 07-14→08-03 (playbook era); IC 窗 90 日 (生成于 2026-08-03 20:02);
- 已落地: P1/P2 (06:21 重启); 观察口径: "2.5-3.0%桶零新仓" + "浅亏持有仓 PRE_CLOSE vs stall 反事实差额", 入每周五记分牌;
- 关联案: DEB-20260730-fee-net-pnl (费用口径), DEB-20260802-short-overnight-hold (隔夜纪律), 高ATR stall shadow (08-07 裁决点), markout 攒样 (08-29 终裁)。
