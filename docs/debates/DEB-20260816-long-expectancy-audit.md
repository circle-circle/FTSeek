# DEB-20260816: 做多侧期望值审查 — 现役多头执行单的钱去哪了

> **立案日**: 2026-08-16
> **状态**: 已立案 (纯只读分析; L-E1 台账脚本待批落地; 任何供给/门变更需预注册判据触发)
> **立案人**: Qoder (应用户要求以量化策略师视角评估策略优缺点后立案; 不预设立场)
> **关联**: DEB-20260816-short-generation-funnel (如何生空) / DEB-20260816-regime-entry-gate
> (披露日何时拦多) / DEB-20260816-no-trigger-direction-edge (未执行 BUY 反事实, open 基准) /
> DEB-20260815-stable-profit-feasibility §39 (W_WEEK) / DEB-20260805-mfe-capture-stall /
> DEB-20260806-asymmetric-exit-hold-losers / DEB-20260723-crash-day-long-bias-postmortem

---

## 零、立案人自我修正 (口径事故, 先于一切结论)

立案人 8/16 首轮向用户报告"**LONG 连亏 4 个月 (365笔 -$14k)**"并据此提出"追错了问题"。
时间/来源归因后该表述**过夸**: -$13.2k 出血集中在 5-6 月且主体是**已退役的 main 管线**
(owner='main', LONG -$8,399/62笔, 最后交易日 2026-06-04)。现役口径 (source=PLAYBOOK_SIM,
7/1 起) 是 214笔 毛额 -$893 / 费 $944 / **净 -$1,837** — 零毛 edge + 费用净拖累, 非深亏。

同理 6 月 SHORT 风口 (+$12.6k, 63%胜率) 也出自已退役 main (main SHORT +$12,386/101) —
**不得作为现役空头 edge 证据**。本案所有裁决数字以现役口径 (§2.3) 为准, 全期数仅作时代背景。

教训与 no-trigger 案 §零同源: 不做来源/时代归因的总量数字会制造错误的紧迫感。

### 零.1 二次自纠 (8/16 同日, 辩论前必须知悉): §2.7 首版是假事实

首版 §2.7 称"signal_confidence 342/343 为 0/NULL, conf 从未与结果对账"。**该数字是立案人
查询口径错误**: signal_confidence 是 0-1 量纲 (playbook_service L1337: confidence/100,
2026-07-11 起前向接线一直存在), 首版查询按 1-10 量纲分桶 → 0.55 落进"0 桶"被误读为缺失。
实测 (PLAYBOOK_SIM, 7/1 起, n=260): **0 个 NULL, 0 个 0** — conf 数据链路完好。

更正后的真实发现比原表述更重要: **conf 与结果反相关** (见更正后 §2.7)。

---

## 一、主张拆解

| # | 主张 | 类型 | 现状 |
|---|---|---|---|
| P1 | 现役多头的真问题是"**零毛 edge + 费用净拖累**" (avg pnl ≈ -0.001%, 费后 -$1,837/214笔), 不是灾难性方向错误 | 诊断 | 实测 §2.3/2.8 |
| P2 | 机制候选 A (入场质量): 现役 LONG **avg MAE 0.88% > avg MFE 0.76%** — 平均先亏更深后赚更浅, 与 DEB-20260724-mfe-small-rootcause 一脉 | 机制 | 实测 §2.5, 未判别主因 |
| P3 | 机制候选 B (时间类退出): 现役亏损集中 MFE_STALL (-$941/27) + TIME_EXIT (-$931/33); 全期 TRAILING_STOP -$4.4k/45 属退役时代, 不得作为现役证据 | 机制 | 实测 §2.4, 未判别主因 |
| P4 | 两种互斥假设: **H-L1** 现役多头信号毛期望=0, 任何分桶无显著正 edge (需无差别降频/等待校准) vs **H-L2** 存在可分离正桶 (会话/regime/来源/conf, 定向保留)。判别 = L-E1 台账 | 假设 | 未判别 |
| P5 | 任何多头供给削减/扩产/门变更需 L-E 判据预注册并触发; 台账口径 SSOT = trade_records (source 过滤) + pnl_net (费后) | 纪律 | — |

**争议核心**: P2 vs P3 决定修入场还是修退出 — 方向修错则白做 (修退出不救 MAE>MFE 的入场,
修入场不救 MFE 给回)。P4 决定是无差别做减法还是定向保留。**裁决错误的代价**: H-L2 误判为
H-L1 → 无差别削供给, 杀掉唯一的正桶; H-L1 误判为 H-L2 → 在噪声桶上过拟合"定向优化"。

---

## 二、证据包 (全部实测, 8/16, data/ftseek_us.db:trade_records)

### 2.1 全期方向月表 (时代背景, 含退役源)

| 月份 | LONG n | LONG pnl | LONG胜率 | SHORT n | SHORT pnl | SHORT胜率 |
|---|---|---|---|---|---|---|
| 2026-05 | 112 | -4,651 | 21% | 148 | +1,257 | 39% |
| 2026-06 | 37 | -8,500 | 46% | 43 | +12,573 | 63% |
| 2026-07 | 147 | -177 | 52% | 37 | +193 | 49% |
| 2026-08* | 69 | -716 | 43% | 9 | -147 | 67% |

*8月截至 8/15 (8/16 为周日)。

### 2.2 退役归因 (关键更正)

- owner='main': LONG **-8,399/62笔** (全部 5-6月), SHORT **+12,386/101笔** — 已退役 (末日 6/4)
- entry_session IS NULL 桶 (-$12,799/172) = 5-6月+7月初的旧行, 会话标签 ~7月中才开始记
- → 全期多头 70%+ 的亏损与全部空头风口都来自同一个已死源, 与现役配置无关

### 2.3 现役口径 (source=PLAYBOOK_SIM) — 本案 SSOT

| 月份 | n | 毛 pnl | 费 | 净 pnl | 胜率 |
|---|---|---|---|---|---|
| 2026-06 | 5 | -2,358 | 26 | — | 20% |
| 2026-07 | 145 | -177 | 631 | -817 | 52% |
| 2026-08* | 69 | -716 | 313 | -1,029 | 43% |
| **7/1起合计** | **214** | **-893** | **944** | **-1,837** | ~50% |

现役 SHORT 对照 (PLAYBOOK_SIM, 7/1起): ~44笔 毛约 +$203 — 同为零edge量级, 无显著优差。

### 2.4 现役退出规则盈亏 (7/1起, LONG | SHORT 对照, n≥3)

| exit_reason | LONG n | LONG pnl | SHORT n | SHORT pnl |
|---|---|---|---|---|
| SL_TRAIL_LOCK | 40 | **+1,079** | 10 | +385 |
| PRE_CLOSE_EXIT | 31 | +425 | 5 | +64 |
| BROKER_GHOST_CLOSE | 6 | +158 | 4 | -150 |
| SL_BREAKEVEN | 46 | +142 | 8 | +11 |
| SL_FIXED_PCT | 5 | +2 | 4 | -8 |
| DEAD_SIGNAL_EXIT | 9 | -184 | — | — |
| PATIENT_EXIT_STOP/EOD | 7 | -454 | — | — |
| SL_INITIAL | 4 | -450 | — | — |
| TIME_EXIT | 33 | **-931** | 3 | -112 |
| MFE_STALL_EXIT | 27 | **-941** | 4 | -282 |
| EOD_FORCE_CLOSE | — | — | 6 | +295 |

现役多头: 盈利靠 trail-lock 与尾盘退出; 亏损**全部**集中在时间类规则
(MFE_STALL+TIME_EXIT+PATIENT 合计 -$1,780/76笔) + 初始止损。注: 8/16 落地的退出几何修复
(P1-5/P1-10/P1-6, DEB-fullchain §十一/十四) 之后零交易日, 8/17 起首周需对照本表基线。

### 2.5 现役几何 (7/1起, LONG): **avg MFE 0.76% < avg MAE 0.88%**, avg pnl -0.001%

平均"最大不利偏移"大于"最大有利偏移" — 持仓期平均先被压得更深。这是入场时机/价格质量的
直接证据 (P2), 独立于退出规则怎么选。

### 2.6 regime 无差别亏损 (全期 LONG): cautious -3,031/110, UNKNOWN **-5,152/26** (均-198),
HIGH_VOL -1,805/55, RANGE -1,734/28, TREND_UP **-1,287/40**, risk_on -898/95
— 上涨行情做多也不赚; 现役细分待 L-E1 台账。

### 2.7 (更正版) conf 数据链路完好, 但校准**反向**: conf 越高结果越差

PLAYBOOK_SIM 7/1 起 n=260, signal_confidence 0 NULL / 0 零值 (0-1 量纲, confidence/100):

| conf 桶 | n | 胜率 | avg pnl |
|---|---|---|---|
| 0.3 (DA 35) | 135 | 53% | -0.04% |
| **0.5 (UE capped)** | **55** | **64%** | **+0.02%** |
| 0.6 | 7 | 29% | -0.25% |
| 0.7 | 16 | 36% | -0.34% |
| 0.9 (DA 高确信) | 37 | 35% | -0.03% |

唯一正桶是 0.5 (UE capped 常规档); 最高确信 0.9 桶胜率反而最低。0.5 vs 0.9 两比例
z≈2.7 (n=55/37, 多重比较未校正, 方向性参考)。**含义**: 现有 conf 门 (如 supply_conf_gate=55)
建立在"conf 越高越好"的未验证假设上, 现役证据指向相反 — DA 高确信 (0.9) 与 UE 常规档 (0.5)
谁该获得供给配额, 是 L-E1 台账的核心判别项之一。decision_conf (fullchain #27, 8/16 起写入)
用于 pipeline 路径交叉验证, 攒满后并入。

### 2.8 费用即全部净亏损来源: 现役 avg pnl ≈ 0, 月费 $300-631 → 净负全由费差构成 (P1)。

### 2.9 数据卫生: 全期混入 live_conservative×1 / manual×1 / PLAYBOOK_SHADOW×18 / sandbox×8 /
shadow×1 — 台账与一切裁决查询必须 `source_type='PLAYBOOK_SIM'` 过滤。

---

## 三、推导链与判别

1. P1 成立 (零毛edge+费拖累) → "优化信号"前先承认: **频率本身是负贡献**。降频是唯一无需
   edge 假设就能改善净值的动作 — 但降频会拖长样本积累, 与校准目标冲突, 需权衡。
2. P2 (MAE>MFE) 若为主因 → 修入场时机 (regime/时段/锚), 与 regime-entry-gate、
   no-trigger 反事实案天然衔接。
3. P3 (时间类退出) 若为主因 → 修/隔离 MFE_STALL 与 TIME_EXIT, 引用 DEB-20260723-time-exit-censoring
   (删失偏差) 与 20260805-mfe-capture (毕业判据), 避免"删规则=把亏损转移到别的腿"的假修复
   (W_WEEK §39 的教训: 名义5日窗口实际2日)。
4. H-L1/H-L2 判别 = L-E1 台账分桶 + no-trigger 案 open 基准反事实互补
   (已执行单用真实成交价, 无 anchor 折价问题)。

---

## 四、实验设计 (全部只读/影子, 零执行风险)

### L-E1 期望值台账 (主实验)
- 维度: 月 × 方向 × source × entry_session × market_regime × exit_reason × (decision_conf 桶, 攒满后加入)
- 指标: n / 毛 / 净(pnl_net) / 胜率 / avg MFE / avg MAE / t 值; 双周刷新, 输出
  `data/long_expectancy_ledger_latest.json`
- **预注册判据** (写于任何数据再收集之前):
  - 触发"多头供给调整预案"辩论: 现役 LONG 累计 n≥300 且净期望 95%CI 上界 < 0
  - 保护条款: 任一桶 n≥60 且 t≥1.5 → 该桶豁免于无差别削减
  - 现役 n=214, 预计 9 月上旬达 n=300

### L-E2 费用感知准入 (影子)
- 对每条候选指令: 估算 fee+spread ÷ 历史同桶 avg MFE, 比例 >50% 打 shadow 标签 (不拦截)
- 攒 30 天后裁决是否升为 SKIP 门; 与 P1 (费=净亏全部来源) 直接对证

### L-E3 时间类退出规则复查
- 对象: MFE_STALL / TIME_EXIT / PATIENT (现役合计 -$1,780/76笔)
- 工具: 刷新 exit_calibration.json (现 stale 于 2026-06-06) + 删失校正重放
- 产出: 每规则的 n/净/删失比例/删除后亏损转移仿真, 供后续辩论

---

## 五、红线

1. **红线①**: 不改任何生效阈值/门/供给量, 除非对应 L-E 判据预注册并触发 (镜像 short-funnel)
2. **红线②**: 裁决口径 = `pnl_net` (费后) + `source_type='PLAYBOOK_SIM'` 过滤;
   禁止 anchor 基准数字进裁决 (引 no-trigger §零: 未成交限价 anchor 制造 +3.7pp 纸面 edge)
3. **红线③**: 不重审已裁决项 (crash-day-long-bias / asymmetric-exit / W_WEEK), 仅引用其结论
4. **红线④**: 与 regime-entry-gate 的边界: 披露日事件门属彼案, 本案不做事件门;
   两案证据 (regime 桶) 互惠共享
5. **红线⑤**: 多空对照必须同口径同源 (SHORT 也走 L-E1 台账), 防止"只审多头"造成新的偏倚

---

## 六、四案分工 (2026-08-16 并行立案生态)

| 案 | 问题 | 链路位置 | 口径 |
|---|---|---|---|
| **本案** long-expectancy | 现役执行单的钱去哪 (多空) | 执行后 (trade_records) | 真实成交价 |
| no-trigger-direction-edge | 未触发 BUY 是否被误拦 | 执行前反事实 | open 基准 |
| regime-entry-gate | 披露日何时拦多 | 事件/时机 | 日历+回溯检测 |
| short-generation-funnel | 如何生空 | 生成/评级层 | 漏斗影子 |

---

## 七、待办

1. L-E1 台账脚本落地 (`scripts/long_expectancy_ledger.py`, 只读) — **待用户批准后实施**
2. ~~exit_calibration.json 刷新~~ **已落地** (2026-08-16, `scripts/exit_calibration_refresh.py`,
   commit 223754e8; 见 §八)
3. conf 校准初版已出 (§2.7, 反向); decision_conf (8/16 起写入) 攒至 n≥100 做交叉验证,
   并按 source (DA vs UE) 分层复核反向是否稳定
4. 8/17 起首周术后观察 — **已接线** (`scripts/exit_geometry_postfix_watch.py` + crontab
   Tue-Sat 06:32 CST, 见 §八)
5. 双周复核回执 (首个: 2026-08-30)

---

## 八、落地快照 (Qoder, 2026-08-16 晚; 辩论人请勿重复落地)

用户裁: 本案辩论分析交另一 agent; 以下非辩论依赖项已由 Qoder 执行落地 (commit 223754e8 + 本提交):

1. **`scripts/exit_calibration_refresh.py`** (只读): 重建被删的校准分析器 (768e443f), 只恢复
   分析侧, **不复活** ExitMonitor 自动调参环 (红线①)。分类阈值沿用旧版; 单位契约锁定
   (mfe_pct/mae_pct 列存百分数, 加载归一分数 — 契约测试 11/11)。
   77 天现役读数: profit_giveback **27%** (2026-06-06 快照为 0%) — 与 P2/P3"利润给回"诊断一致;
   conf 反向第三次复证 (high 36%胜率/净-$1,490 vs mid 53%); TIME_EXIT n=33 **胜率 0%** (L-E3 重点对象)。
   输出: `config/exit_calibration.json` (schema 兼容历史) + `data/exit_rule_expectancy_latest.json`
2. **`scripts/exit_geometry_postfix_watch.py`** (只读): 基线 (7/1-8/16) vs 术后 (≥8/17) 分
   exit_reason×方向 对照, 止损/追踪族均亏参考线 $600/笔 (非触发器); 输出
   `data/exit_geometry_watch_latest.json`。8/17 首个交易日后自动出术后读数
3. **crontab** (备份 `data/crontab.backup.20260816_222042.txt`): Tue-Sat 06:30/06:32 CST
   每日自动跑上述两脚本
4. **§零.1/§2.7 更正** (commit 3a8ae2b7): conf 链路完好 + 反向校准 — 辩论人请以更正版为准
