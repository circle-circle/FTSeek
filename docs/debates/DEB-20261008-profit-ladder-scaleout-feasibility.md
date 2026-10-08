# DEB-20261008 · 盈利百分比阶梯分批止盈（2%/3%/4% 出一半或全出）可行性辩论

| 项 | 值 |
|---|---|
| 编号 | DEB-20261008 |
| 状态 | **PROPOSED → 证据已收齐，待用户裁决** |
| 立案日期 | 2026-10-08（ET 2026-10-07） |
| 提案来源 | 用户讨论点（原话见 §1） |
| 主题 | 抓住方向且有盈利的标的，因退出过早导致 MFE/MAE 捕获率过低、盈利回吐过多 ⇒ 是否可按盈利百分比达到 2%–3%–4% 时先出一半或全出（落袋为安） |
| 关联 | DEB-20261007（退出时机轴）、DEB-20260627（scale_out 禁用）、DEB-20260928（trail_lock_pyramid）、OKR-2026-10 KR1（退出参数冻结）、C12/C13 红线 |
| 复算脚本 | `scripts/deb20261008_scaleout_binding.py` / `deb20261008_scaleout_cf.py` / `deb20261008_scaleout_arith.py` |
| 验证器 | `scripts/verify_deb20261008.py`（E 表逐格实跑比对） |
| 冻结约束 | OKR-2026-10 KR1：2026-09-17 → 10-31 退出哈希冻结，任何实装 = `W1_oct_cohort_broken` |

---

## 1. 提案原文与重述

> 讨论点，目前对于抓住方向 且有盈利的标的股 但是有由于退出过早 导致mfe/mae捕获率过低，盈利回吐过多，是否可以考虑按照盈利百分比达到多少 比如 2%-3% -4% 先出一半或者全出了，先落袋为安 。 一方面费用问题一方面是qty数量问题 ， 先立项辩论讨论 看看是否可行

**重述为可检验命题**：对「已摸到 ≥T% 浮盈」的持仓，在 T ∈ {2%, 3%, 4%} 触发时平掉 50%（或 100%），剩余按现役退出链走完。期望收益 = 挽回的回吐 − 提前卖出的机会损失 − 分批增量费用。用户已点名两个成本轴：**费用**与 **qty**。

---

## 2. 结论先行（一句话版）

**否决实装。** 三重独立证据同向：

1. **历史裁决**：scale_out 阶梯 2026-06-21 已真跑过后**禁用**（`exit_policy.yaml` DEAD RULE，理由「4笔触发形同虚设，分批状态机增加复杂度无收益，50%锁利反而少赚 ORCL $2,258→$1,328」）。`trade_records` 里 `SCALE_OUT` 恰好 4 笔 Σ+$412.43 —— 注释与数据逐字吻合。
2. **今日独立复算**（不知道历史裁决前完成）：粗筛反事实全阈值 Σ 为负（2% 档 −$14,021，一致性 27.3% ≪ 毕业门 55%）；受益/受损笔数比 26:73。
3. **同族影子权威读数**：`exit_shadow_graduation.py` 里 tiered 家族（阶梯回撤变体）Δ 为正但一致性 11.2%/12.2% FAIL = skill 记载的「总账幻觉」，禁翻开关。

**但提案的直觉指向的痛点是真的**（捕获率中位仅 49.7%，DEB-20261007 E1）。痛点成立、药方错：**阶梯止盈救不了「捕获失败」人群**（他们 MFE 中位仅 52bp，够不到 2% 线），反而把现役机制已经捕获良好的大赢家（MFE≥2% 人群 WR 99.0%）砍一半。**正确药方方向见 §9**。

---

## 3. 前提核查（P0.5：这轴是否已有裁决/影子/死配置）

| 项 | 状态 | 证据 |
|---|---|---|
| 生产退出链有无部分平仓能力 | **无** | `app/risk/exit_policy.yaml`：`scale_out: enabled:false, tiers:[]`（DEAD RULE 2026-06-21）；生产退出枚举无 scale-out 腿 |
| `config/exit_profiles.yaml` 的 `take_profit_type: scale_out` | **死配置** | 文件头自述「harness/shadow_bridge 影子系统专用（回测/辩论）」，`grep take_profit_type app/` 零消费方 |
| 影子注册表有无同轴变体 | **有，tiered 家族** | `exit_shadow.py:63-64`：`tiered_dd0.4_tier1.2`（be0.5/dd0.8/dd_low0.4/tier_at1.2）—— 语义 = **回撤线分档、全仓一次出**，与「分批半仓」不同；部分止盈 scale-out **无现役影子** |
| 历史真跑记录 | **有** | `trade_records.exit_reason='SCALE_OUT'` n=4 Σ+$412.43（与禁用注释「仅4笔」吻合） |
| 既有裁决文档 | DEB-20260627 §「FTSeek当前: scale_out已禁用(4笔触发, 形同虚设), TP=SL(rr=1.0)」 | |
| OKR-2026-10 KR1 冻结 | 实装任何退出参数变更 = `W1_oct_cohort_broken`（窗至 10-31） | `scripts/daily_invariant_check.py:_OCT_COHORT_START` |

⇒ **本 DEB 不是重复立案**：「按盈利%分批半仓」的精确形态（partial scale-out ladder）没有现役影子、没有专门 DEB；但它的近亲（tiered 回撤分档）已 FAIL、它的祖先（scale_out）已被真跑禁用。

---

## 4. 证据表 E（每行可复算；期望输出=程序落盘实跑值，非手抄）

工作目录 `/opt/ftseek/FTSeek-US`，全部只读（`mode=ro`）。一键验证：`venv/bin/python scripts/verify_deb20261008.py`。

| # | 断言 | 命令 | 期望输出片段 |
|---|---|---|---|
| E1 | MFE 分布：中位仅 0.44%，2% 线在 p90 之外 | `venv/bin/python scripts/deb20261008_scaleout_binding.py` | `中位=0.44  p75=0.99  p90=2.08  p95=3.11  max=19.58` |
| E2 | 阶梯触发人群极小：MFE≥2% 仅 99/958=10.3%，≥3% 5.2%，≥4% 2.9% | 同 E1 | `      2.0%       99   10.3%` / `      3.0%       50    5.2%` / `      4.0%       28    2.9%` |
| E3 | **触发人群现状 WR 99%+**：摸到 2% 的 99 笔里落袋为负仅 **1 笔** | 同 E1 | `      2.0%    99    3.07%     2.28%    74.4%     0.79%          1` |
| E4 | 4% 档现状捕获率已 100%（回吐=0，阶梯无残存空间） | 同 E1 | `      4.0%       28    2.9%  $    37,879.00        5.92%  100.0%` |
| E5 | 分批增量费用：中位 +$1.99/笔 = **3.4bp**（现状单边 7.6bp 的 45%） | 同 E1 | `增量 bp 中位=3.4bp  均值=4.7bp  p90=7.3bp` |
| E6 | qty 物理约束：中位 36 股，min commission 绑定率 **83.8%**（qty<199 吃 $1.99 地板） | 同 E1 | `① 现状单腿: min commission 绑定率 = 83.8%` |
| E7 | qty 小尾：≤5 股 10.4%（半仓=2 股以下，费用占比爆炸）；奇数 48.5%（对半分产生碎股，Futu 碎股卖出单独排队） | 同 E1 | `qty 小尾: ≤5股 100 笔 (10.4%) \| 奇数 465 笔 (48.5%, 对半分产生碎股)` |
| E8 | 粗筛反事实（MAE 近似级）：全阈值 Σ$ 为负；2% 档 Σ−$14,021、一致性 27.3%、日聚类 t=−4.40 | `venv/bin/python scripts/deb20261008_scaleout_cf.py` | `2.0%     99    -17.9    -69.2 $-14,020.97    27    72     0   27.3%    -4.40     51` |
| E9 | 4% 档最惨：一致性 **14.3%**（28 笔里 24 笔更差） | 同 E8 | `4.0%     28   -103.6   -126.4 $ -8,142.50     4    24     0   14.3%    -3.78     19` |
| E10 | 最受伤单笔 = ORCL SHORT 2026-04-24（MFE 7.32% 落袋 7.32%，捕获 100% → 半仓砍掉 Δ−$6,494.04）；2% 档最受伤 5 笔里 3 笔出场=EXTERNAL_CLOSE/TRAILING_STOP（现役机制已捕获良好） | 同 E8 | `US.ORCL    2026-04-24 SHORT     MFE=  7.32% 实际落袋=  7.32%  Δ=$-6,494.04  出场=EXTERNAL_CLOSE` |
| E11 | 决定性算术：**现状落袋中位 > 阈值 ⇒ Δ 结构性为负**；4% 档 −0.99pp（阈值越高越负） | `venv/bin/python scripts/deb20261008_scaleout_arith.py` | `4.0%   28    5.92%        5.92%     4.92%  -0.99              5` / `2.0%   99    3.07%        2.28%     2.11%  -0.17             31` |
| E12 | 受益/受损人群（2% 档）：受益 26 笔 Σ+6.77pp vs 受损 73 笔 Σ−73.61pp ⇒ 净 −66.84pp，一致性 26.3% | 同 E11 | `受益 (Δ>0) n= 26  Δpp 中位=+0.214  Σ=+6.77pp` / `受损 (Δ≤0) n= 73  Δpp 中位=-0.419  Σ=-73.61pp` |
| E13 | 历史 SCALE_OUT 真跑 4 笔 Σ+$412.43（与禁用注释吻合） | `venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True);print(c.execute(\"SELECT COUNT(*),ROUND(SUM(pnl_net),2) FROM trade_records WHERE exit_reason='SCALE_OUT'\").fetchone())"` | `(4, 412.43)` |
| E14 | 禁用注释原文（逐字） | `grep -n -A3 "DEAD RULE: scale_out" app/risk/exit_policy.yaml` | `# 4笔触发形同虚设, 分批状态机增加复杂度无收益` / `# 数据: 50%锁利反而少赚 (ORCL $2,258→$1,328)` |
| E15 | 同族 tiered 影子权威读数：一致性 11.2%/12.2% FAIL（毕业门 55%）。口径注：毕业报告的 `cap`/`Δ` 均为 **capture-ratio 口径**（Δ=变体均值−基线均值，`exit_shadow_graduation.py:109`），非美元；skill 记载的「美元正信号」指 `_exit_shadow` 逐笔美元聚合，两口径可反号，本案 capture 口径已 FAIL | `timeout 400 venv/bin/python scripts/exit_shadow_graduation.py 2>&1 \| grep tiered` | `❌ tiered_dd0.4_tier1.2: FAIL \| N=419 cap=-1.002 Δ=+0.319 一致性=11.2%` / `❌ tiered_dd0.5_tier1.5: FAIL \| N=419 cap=-0.997 Δ=+0.324 一致性=12.2%` |
| E16 | 捕获失败人群（DEB-20261007 E4 的 157 笔）MFE 中位 52.2bp ≪ 2% 线 ⇒ **阶梯够不到他们** | `venv/bin/python scripts/deb20261007_capture_failure.py 2>&1 \| grep -A1 落袋失败` | `落袋失败     n= 157  MFE bps: 中位=   52.2` |
| E17 | 回吐比 Top-12 全是 MFE 7–34bp 的入场即错单（OVERNIGHT_GAP_STOP/SL_INITIAL），无一摸到 2% ⇒ 回吐痛点人群与阶梯触发人群**零交集** | `venv/bin/python scripts/deb20261007_capture_failure.py 2>&1 \| grep -A13 回吐最狠` | `US.CRM    LONG       2026-09-16 MFE=    7.4bp` / `US.SNDK   LONG       2026-09-25 MFE=   10.6bp` |
| E18 | 现役机制对大赢家的捕获已良好：SL_TRAIL_LOCK n=154 Σ+$7,058；DAY_TRADE_TP n=13 Σ+$12,494 | `venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True);[print(r) for r in c.execute(\"SELECT exit_reason,COUNT(*),ROUND(SUM(pnl_net),2) FROM trade_records WHERE exit_reason IN ('SL_TRAIL_LOCK','DAY_TRADE_TP','PRE_CLOSE_EXIT') GROUP BY 1\")]"` | `('SL_TRAIL_LOCK', 154, 7058.29)` / `('DAY_TRADE_TP', 13, 12493.93)` |
| E19 | ORCL 全史 39 笔（MFE>0）：捕获率 <60% 有 22 笔，但其中 **MFE≥2% 仅 2 笔**（另 20 笔阶梯够不到）；捕获率 ≥95% 有 11 笔（其中 4 笔 MFE≥2% = 阶梯会砍它们）⇒ 痛点人群与触发人群几乎不重叠 | `venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True);rows=c.execute(\"SELECT mfe_pct,pnl_pct FROM trade_records WHERE symbol='US.ORCL' AND exit_time IS NOT NULL AND mfe_pct>0\").fetchall();cap=[100*l*100/m for m,l in rows];low=[(m,x) for (m,l),x in zip(rows,cap) if x<60];hi=[(m,x) for (m,l),x in zip(rows,cap) if x>=95];print('n=',len(cap),'low60=',len(low),'low60_mfe_ge2=',sum(1 for m,_ in low if m>=2.0),'hi95=',len(hi),'hi95_mfe_ge2=',sum(1 for m,_ in hi if m>=2.0))"` | `n= 39 low60= 22 low60_mfe_ge2= 2 hi95= 11 hi95_mfe_ge2= 4` |
| E20 | dead_signal 家族是毕业报告唯二 PASS（分离度 +1.109pp/+1.184pp，N=527），且已接线生产（`dead_signal_exit_enabled: true`） | `timeout 400 venv/bin/python scripts/exit_shadow_graduation.py 2>&1 \| grep dead_signal_60m` | `✅ dead_signal_60m_mfe0.3: PASS \| N=527 triggered=-0.764% not=0.345% 分离=+1.109pp` |
| E21 | 出血口在入场即错人群：SL_INITIAL n=28 Σ−$4,422 + OVERNIGHT_GAP_STOP n=7 Σ−$2,176 + sl_hit n=115 Σ−$3,702 + TRAILING_STOP n=87 Σ−$5,177 | `venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True);[print(r) for r in c.execute(\"SELECT exit_reason,COUNT(*),ROUND(SUM(pnl_net),2) FROM trade_records WHERE exit_reason IN ('SL_INITIAL','OVERNIGHT_GAP_STOP','sl_hit','TRAILING_STOP') GROUP BY 1 ORDER BY 3\")]"` | `('TRAILING_STOP', 87, -5177.14)` / `('SL_INITIAL', 28, -4421.99)` |

---

## 5. 机制分析：为什么阶梯结构性必败（不是参数没调好）

**决定性算术（E11/E12）**：

```
阶梯落袋 = 0.5×阈值 + 0.5×(剩余半仓按现状路径) − 3.4bp
Δ = 阶梯落袋 − 现状落袋 = 0.5×(阈值 − 现状落袋) − 3.4bp
```

⇒ **Δ>0 当且仅当 现状落袋 < 阈值 − 6.8bp**。实测各档「现状落袋中位」：1%=1.13%、2%=2.28%、3%=3.61%、4%=5.92% —— **全部高于阈值**（因为能摸到 T% 的单，现役 trail/TP 机制大多能把落袋守在 T% 之上）。所以中位数上 Δ 必负，且**阈值越高负得越多**（4% 档 −0.99pp：那批现状落袋中位 5.92%，砍一半到 4% 纯亏）。

受益人群 = 「摸到阈值但回吐到阈值以下」= 实测 2% 档仅 26 笔（26.3%），且他们平均只赚 +0.214pp；受损人群 73 笔平均亏 −0.419pp（尾部是 ORCL 这种 −$6,494 的大赢家）。**这就是「让利润奔跑」与「提前卖一半」的结构冲突**：现役机制（SL_TRAIL_LOCK/TRAILING_STOP/DAY_TRADE_TP/EXTERNAL_CLOSE）在大赢家上捕获率已 95–100%，阶梯唯一能做的事就是把它们的一半仓位在 2–4% 处提前卖掉。

**费用轴（E5/E6/E7，用户点名）**：
- 分批 = 多一腿 = 多一份 $1.99 min commission。qty 中位 36 股 ⇒ 83.8% 的单腿本来就吃地板，分批后**两腿都吃地板** ⇒ 增量费用中位恰好 $1.99 = 3.4bp = 现状单边费用的 **45%**。
- 系统毛利缺口本来就只有 −33.9bp（DEB-20261007 E25 forward 窗）～ −8.5bp（全历史），再加 3.4bp 是直接挖毛利。
- qty≤5 股的 10.5% 单：半仓 = 2 股以下，$1.99 地板占名义可达 1%+；奇数 qty 48.5% 对半分产生碎股，Futu 碎股卖单流动性差、可能挂不出去 ⇒ **部分成交状态机**（半仓卖出一半挂着）会把「落袋为安」变成「仓位卡在半路」，SIM 环境无券商止损兜底（skill `ftseek-extended-hours-trading` 记载）。

**触发人群与痛点人群零交集（E16/E17，最关键）**：
- 用户直觉针对的「捕获率过低、回吐过多」人群 = DEB-20261007 的 157 笔捕获失败批：**MFE 中位仅 52.2bp**，回吐比 Top-12 全是 MFE 7–34bp 的入场即错单（OVERNIGHT_GAP_STOP/SL_INITIAL）。他们**根本摸不到 2% 线** ⇒ 阶梯对他们零作用。
- 阶梯实际触发的 99 笔（MFE≥2%）：WR 99.0%、落袋为负仅 1 笔、4% 档捕获率已 100% ⇒ **这批不需要救**。
- ⇒ 提案把「想救的人」和「实际被砍的人」完全错配。这与 DEB-20261007 自曝#4 同型：「不是利润来了拿不住，而是失败组 MFE 幅度本就只有一半」。

---

## 6. 反方最强论证（steelman，按纪律不缺席）

| # | 反方论点 | 检验结果 |
|---|---|---|
| R1 | 「粗筛是 MAE 近似级，假设摸到即成交，系统性偏差可能低估阶梯收益」 | **方向反了**：bar 触及回放只会**高估**触发率（skill 记载 62% vs 实际 22 笔）⇒ 真实 Σ 比 −$14,021 **更负**，粗筛已是对阶梯最有利的层次 |
| R2 | 「受益人群 26 笔平均 +0.214pp，若只对他们触发（加条件门）呢？」 | 条件门 = 「MFE≥2% 且预期将回吐」——预期回吐不可观测（可观测时已回吐）。DEB-20261007 已裁：penetration_shadow（快跌vs V回判别器）KILLED，不建穿透判别器。用「落袋<T%」事后筛选 = 拿已结算案例反推阈值（阈值购物，C12 红线） |
| R3 | 「全出（100%）而不是半仓呢？」 | 全出 = 把 4% 档捕获率 100%、2% 档 WR 99% 的人群直接清仓。E4：4% 档 28 笔现状 Σ+$37,879、落袋中位 5.92% ⇒ 全出在 4% 线 = 每笔平均砍掉 ~2pp 尾部。且「全出」与现役 DAY_TRADE_TP/SL_TRAIL_LOCK 功能重叠，只剩更早触发 = 纯机会损失 |
| R4 | 「SIM 里费用模型可能失真，实盘 IBKR 分批费用更低？」 | `fee_model.py` 是生产 SSOT（commission_min_per_order=$1.99 来自实盘费率表）；即便增量费用减半到 1.7bp，Δ 公式里 3.4bp→1.7bp 只把 2% 档 Σ 从 −$14,021 抬到约 −$12,3xx，**符号不变、一致性不变**（受损 73 笔是机会损失主导，不是费用主导） |
| R5 | 「qty 大的单（p75=97 股）分批费用占比小，对它们可行？」 | qty>199 才不吃地板（16.1%）。实测：qty≥200 且 MFE≥2% 仅 **17 笔**（qty≥200 全量 154 笔 = 16.1%），**n 不足 30 预注册门槛**，且大 qty 单正是 ORCL $17,843 这类大赢家 —— 伤害最大的恰是它们 |

---

## 7. 与既有裁决/红线的关系

| 裁决 | 关系 |
|---|---|
| `exit_policy.yaml` DEAD RULE scale_out（2026-06-21） | **直接命中**：本提案 = 重新启用已禁用的 scale_out 并加阶梯。重新立案需要新证据，本 DEB 的结论是新证据**反向** |
| C12（禁拿已结算案例反推阈值） | 若按「哪档阈值历史表现好」选 T = 阈值购物。本 DEB 不做该操作：全档位同测同报 |
| C13/C9 家族（禁据错失收益改门） | 不适用（本轴是退出非入场），但同型逻辑：「未触发人群的假设收益」不构成改门依据 |
| OKR-2026-10 KR1 退出哈希冻结（至 10-31） | 即使证据为正也不能实装；本案证据为负 ⇒ 冻结约束不 binding，但**记录在案**：任何阶梯实装讨论自动延后至 11 月 |
| skill `exit-parameter-validation`「阶梯类方案先交两组数字」 | 已交：① tiered 家族毕业报告读数（E15，capture 口径，一致性 11-12% FAIL）② 分批费用/qty 成本（E5-E7） |
| DEB-20261007 方案 C（早达峰 variant） | **互补不冲突**：方案 C 针对「MFE 停滞弱单提前离场」（dead_signal 族方向），本 DEB 针对「盈利单提前卖一半」—— 前者救失败组，后者砍成功组。本 DEB 结论强化了方案 C 的定位（H0 前置门不变） |

---

## 8. 建议（按优先级）

| # | 建议 | 类型 | 理由 |
|---|---|---|---|
| **B1** | **否决 2%/3%/4% 分批止盈阶梯实装**（全出与半仓同否） | 裁决 | §5 机制 + E8-E12 数据 + §3 历史裁决，三重同向 |
| **B2** | 不新开影子攒样 | 裁决 | 粗筛一致性 26.3% 距毕业门 55% 差 28.7pp，且机制上 Δ 符号由「现状落袋 vs 阈值」决定、不随样本积累翻转 ⇒ 攒样是浪费影子预算（对照：tiered 家族攒了 419 行仍 FAIL） |
| **B3** | 用户痛点（捕获率 49.7% 过低）的正确药方 = **DEB-20261007 方案 C 的 H0 路径**：先补 `time_to_mfe` 采集覆盖（现 45.8%，SL_INITIAL 仅 14.3%），再谈早达峰处置 variant | 转介 | E16/E17：痛点人群 = 入场即错 + MFE 停滞弱单，不是「盈利回吐」人群 |
| **B4** | 出血口治理保持 DEB-20261007 F 表路径（SL_INITIAL/OVERNIGHT_GAP_STOP 轴），不与本轴混装 | 转介 | E21：四腿 Σ−$15,477，全部是「没摸到 2% 就死」的单 |
| B5 | （可选，零行为变更）把本 DEB 三脚本挂进周六 `pending_adjudication_carrier` 的报告附录，防止未来重复立案 | 治理 | scale_out 已是第二次被提案（`exit_profiles.yaml` 死配置仍在，易误导后人以为可用） |
| B6 | （可选）给 `config/exit_profiles.yaml` 头部加一行「scale_out/tiered 已被 DEB-20260627+DEB-20261008 双否决，仅回测 harness 用」 | 治理 | 死配置语义自述已有，但缺裁决指针 |

---

## 9. 待用户裁决

- **F1 = B1**：是否接受否决（半仓与全出同否）？
- **F2 = B2**：是否同意不攒样（省影子预算）？
- **F3 = B3**：痛点转介到方案 C H0 路径（补 time_to_mfe 采集）是否立项？
- **F4 = B5/B6**：两个零行为变更治理项是否顺手做？

---

## 10. 复查挑战点（给第二 agent）

1. **E8 粗筛的乐观偏差方向**（R1）：如果你能证明 bar 触及假设在本数据集**低估**触发（例如 MFE 采样粒度粗到漏峰），Σ 符号才可能翻转。检验：`_exit_shadow` 的 10s 轮询 MFE 序列 vs trade_records.mfe_pct 的分布差。
2. **E3 的「落袋为负仅 1 笔」是否被 exit_reason 混合人群污染**：`trade_records` 含旧架构出口（sl_hit/TRAILING_STOP 等，DEB-20261007 自曝#10 证其 time_to_mfe 缺失 100%）。若只取新架构八腿重算 E2/E3，触发人群和 WR 是否 materially 不同？（我的预判：新架构腿捕获更好 ⇒ WR 只会更高 ⇒ 结论更强，但须实测。）
3. **E15 tiered 家族与本提案的语义距离**：tiered = 回撤线分档全仓出，本提案 = 盈利线触发半仓出。若你认为 tiered FAIL 不能外推到 partial scale-out，请指出 E8-E12 独立粗筛的漏洞 —— 注意 E8-E12 **不依赖** tiered 数据，是独立证据腿。
4. **反事实里「剩余半仓按原路径」假设**（`deb20261008_scaleout_cf.py` 公式）：分批后剩余仓位的 trail/BE 线是否应按半仓名义重算？若重算，Δ 会更负还是更正？（我的预判：BE/trail 按价格百分比定义，与 qty 无关 ⇒ 路径不变；但 min commission 地板使半仓腿费用占比更高 ⇒ 更负。）
5. **§5 公式的 6.8bp 盈亏平衡线**：`Δ>0 ⇔ 现状落袋 < 阈值 − 2×3.4bp`。若实盘增量费用是 SIM 的一半（R4），平衡线移到 −3.4bp，受益人群从 26 → ~29 笔，一致性 26.3%→29.3%，仍 ≪55%。请独立验证该敏感性。

---

## 11. 自曝（本 DEB 起草过程中我自己的错误，全部已修正）

| # | 错误 | 修正 |
|---|---|---|
| ① | `deb20261008_scaleout_binding.py` 初版查 `trade_records.trade_id` 列 → 无此列（真列名 `id`） | 先 PRAGMA 后改列名；按纪律「列存在≠有数据」同时核了填充率 |
| ② | `arith` 脚本初版硬编码「受益仅 1 笔」——把 binding 的「落袋<0」口径（1 笔）错当「落袋<阈值」口径（31 笔） | 重写为受益/受损双人群逐笔表（E12），硬编码判读全部删除；文字只陈述实测数 |
| ③ | 我上一轮对话口头说「ORCL 04-24 在 DEB-20261007 回吐比 Top-12 榜上」——实跑核实榜上是 ORCL **LONG 09-23/09-24**（入场即错单），04-24 SHORT 是捕获率 100% 的大赢家 | 记忆错误未进任何文档；本 DEB 所有 ORCL 引用以实跑输出为准（E10/E19） |
| ⑤ | E15 初版把毕业报告 `Δ` 列标注为「美元列指标无效」——实核源码 `Δ=improvement=mean_cap−mean_baseline` 是 **capture 口径**非美元；我把 skill「均值 pp 与美元 ΣΔ 可反号」的教训错扣到该列 | 已改为正确口径注（capture 口径 FAIL 本身有效），skill 的「美元正信号」另指 `_exit_shadow` 逐笔聚合 |
| ④ | 粗筛反事实是 MAE 近似级，按 skill 纪律**不可定稿** | 已在脚本文档头 + E8 行内双层声明；但本案 Σ 距零 −$14k、t=−4.4，层次偏差不改变符号 ⇒ 定稿级模拟不必要（省算力），若复查者不同意请按 §10-1 挑战 |

---

## 12. 附录

**复算脚本**（全部只读、`sys.path` 自举、无 scratch 依赖）：

| 脚本 | 内容 | 证据行 |
|---|---|---|
| `scripts/deb20261008_scaleout_binding.py` | 绑定预检三问：触发人群/现状结局/qty 费用成本 | E1-E7, E19 |
| `scripts/deb20261008_scaleout_cf.py` | 粗筛反事实（MAE 近似级）+ 日聚类 t + 逐笔受益/受损榜 | E8-E10 |
| `scripts/deb20261008_scaleout_arith.py` | 决定性算术（Δ 公式）+ 受益/受损人群分解 | E11-E12 |
| `scripts/verify_deb20261008.py` | E 表逐格实跑比对验证器（同 DEB-20261007 契约） | 全部 |

**数据源**：`data/ftseek_us.db`（trade_records n=958 已结算）、`data/shadow/exit_param_shadow.jsonl`、`app/risk/exit_policy.yaml`、`config/exit_profiles.yaml`、`app/execution/fee_model.py`。

**量纲铁律**：`mfe_pct/mae_pct`=百分点，`pnl_pct`=小数比例（×100 得百分点）——DEB-20261007 §5 已裁，本 DEB 全部脚本遵守。
