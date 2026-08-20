# 议题：全系统双维审计（架构 × 量化）— 三个结构性出血点与修复路线

## 状态

- [x] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> 角色：本文作者 = 起案方（外部视角全系统审计，4 路并行深读 + DB 直查）。
> 按宪章三角色制，**辩论方证伪式攻击待补**（合格标准：独立重跑 §三 全部数字 + 至少一个攻击点）。
> 本文不触碰八月终裁框架（`docs/specs/august-verdict-framework-20260831.md`，判据已冻结），
> 也不预设 G1~G5 结果；所有执行项带验证期合规标签。
> **2026-08-20 R2 进展**：用户裁"先做收益大、风险相对低的方面"→ 四个白名单项落地
> （P0-3/P0-4 脚本+cron 已上线，P2-1 ARCHITECTURE.md 重写，P2-2 索引脚本）；机制类改动继续排队终审后。

## 背景

2026-08-20 应用户要求，以"顶级代码架构师 + 成熟量化交易员"双视角对 FTSeek-US 做全系统审计。
方法：4 个独立 Agent 并行深读（Playbook 管线 / 风控与执行 / 数据信号层 / 工程卫生）+ 主控直查
`data/ftseek_us.db`、`config/unified_config.yaml`、`docs/` 权威文档与 git 史交叉验证。
审计基线日：2026-08-20（盘中）。环境：富途模拟盘（`playbook.execute_mode: futu_simulate`），
净值 $97,042，当前回撤 -1.22%，峰值回撤 -3.33%，水位 GREEN（`capital_state` 表直读）。

立项理由：项目正处于 8/29 终裁前 9 天。此时需要一份**不掺进展叙事**的冷读——
工程迭代速度（8 月 320 commits）与账本读数（每笔期望持续为负）之间的剪刀差，
是本文所有问题的出发点。

## 目标

1. 回答：**当前系统有没有已证明可交易的 alpha？**（量化视角的根问题）
2. 回答：**如果没有，结构性原因是什么、哪些可修、哪些不可修？**
3. 给出按验证期纪律（宪章 §四）合规的修复路线，供终裁输入与 9 月排期。

---

## 数据与实证（全部可复现，口径声明在前）

**口径声明（对宪章七律逐条自检）**：
- ① source_type 白名单 `IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')`（简写 `LIKE 'PLAYBOOK%'`，
  实测该窗口内仅这两种，`normal` 模式 4 笔已并入——两口径差 < $60，不影响结论）；
- ② 窗口 = OKR-2026-08 定义的干净窗口 `trade_date >= '2026-07-11'`；
- ③ 毛/净分离：`realized_pnl` = 毛，`pnl_net` = 净，费用 = 两者之差（fee_model 已实证与 fees_est 分毫不差，DEB-20260730）；
- ④ **Jul vs Aug 对比跨多个 config_version（8/6 reentry、8/15-8/20 帽调整、8/19 冷却窗豁免等），
  归因为假设级，不是结算级结论**——见 §五 分歧 1；
- ⑤ 所有 n<30 的子格只作观察不作结论（下文逐处标注）。

### 复现命令（sqlite3 data/ftseek_us.db）

```sql
-- [R1] 总账：毛/费/净三列
SELECT COUNT(*) n, ROUND(SUM(realized_pnl),2) gross, ROUND(SUM(pnl_net),2) net,
       ROUND(SUM(realized_pnl-pnl_net),2) fees, ROUND(AVG(realized_pnl-pnl_net),2) avg_fee
FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND trade_date >= '2026-07-11';
-- 实测: n=257 | gross=-1210.92 | net=-2353.63 | fees=1142.71 | avg_fee=4.46

-- [R2] 名义与费用强度
SELECT ROUND(AVG(entry_price*quantity),0) avg_notional
FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND trade_date >= '2026-07-11';
-- 实测: $3,717 → 总换手≈$955K → 毛≈-12.7bps / 费≈+12.0bps / 净≈-24.7bps 每往返

-- [R3] Jul vs Aug 每笔期望
SELECT CASE WHEN trade_date<'2026-08-01' THEN 'Jul' ELSE 'Aug' END era,
       COUNT(*) n, ROUND(SUM(pnl_net),2) net, ROUND(AVG(pnl_net),2) per_trade
FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND trade_date >= '2026-07-11' GROUP BY era;
-- 实测: Jul n=143 net=-1050.75 (-7.35/笔) | Aug n=114 net=-1302.88 (AVG=-11.53, 含1笔NULL净, SUM口径-11.43/笔)

-- [R4] 方向结构
SELECT direction, COUNT(*) n, ROUND(SUM(pnl_net),2) net,
       ROUND(100.0*SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr
FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND trade_date >= '2026-07-11' GROUP BY direction;
-- 实测: LONG n=224 net=-2198.34 WR40.2 | SHORT n=33 net=-155.29 WR42.4 (SHORT 仅占供给 12.8%)

-- [R5] 退出原因账本 (top 出血口/利润源)
SELECT exit_reason, COUNT(*) n, ROUND(SUM(pnl_net),2) net,
       ROUND(100.0*SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr
FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND trade_date >= '2026-07-11'
GROUP BY exit_reason ORDER BY net;
-- 实测利润源: SL_TRAIL_LOCK n=61 +2109.71 WR91.8 / PRE_CLOSE_EXIT n=29 +564.32 WR69.0
-- 实测出血口: SL_INITIAL n=8 -1541.72 / MFE_STALL_EXIT n=31 -1352.64 WR0 / TIME_EXIT n=31 -941.34 WR0

-- [R6] 资金状态直读
SELECT * FROM capital_state ORDER BY rowid DESC LIMIT 1;
-- 实测: 净值 97042.17 | 当前回撤 -1.22% | 峰值回撤 -3.33% | GREEN
```

### 工程侧实证锚点（file:line，4 路深读结论，抽样如下）

| # | 事实 | 锚点 |
|---|------|------|
| E1 | `pipeline.py` 7,687 行 / 92 defs / **237 个 except Exception** / 16+ 模块级可变状态；最长单函数 806 行（`monitor_playbook_sim_exits` :6607） | `app/playbook/pipeline.py` |
| E2 | `order_executor.py` 5,396 行 god object，**无专属测试文件** | `app/pipeline/order_executor.py` |
| E3 | regime 乘数表同一 dict 硬编码 3 份 | `pipeline.py:1078, :3535, :3543` |
| E4 | guardrail（sector 40%/方向偏斜）只挂 generate 路径；DA/UE 直写 DB 绕过（DA 219 条 conf=36 BUY 直写执行 123 笔为已记录事实，DEB-20260806） | `pipeline.py:_apply_portfolio_guardrails :1681` |
| E5 | 退出双路径地雷：`trailing_stop`（=实盘 ATR 止损）在 exit_policy.yaml **无 key**，仅靠 registry"缺省=启用"存活；注释明写"切勿改 fail-close 否则关掉实盘止损" | `app/risk/position_exit_monitor.py:278-279` |
| E6 | 三态切换靠 env 变量组合；`position_sync` 只查当前 env，切 env 后旧环境仓位不可见 | `app/core/position_sync.py:576-577` |
| E7 | sector map 硬编码 ~80 票，未知标的**直接放行**集中度检查 | `app/risk/sector_limits.py:139-141` |
| E8 | 回撤">5x 突变自动重置"后门已删除改纯日志（fail-dangerous→fail-safe 正确修复） | `app/risk/capital_safety.py:202-216` |
| E9 | 两套并行 IC 系统（playbook ic_tracker + signal_ic_tracker），口径分裂风险 | `app/playbook/ic_tracker.py` / `app/analysis/signal_ic_tracker.py` |
| E10 | ARCHITECTURE.md 停在 2026-06-20（仍述"两条入场线 LIVE"），与 AGENT.md 7/26 事实（Playbook 唯一下单管线 + 主管线冻结）不一致 | `docs/ARCHITECTURE.md:1` |
| E11 | 13+ record-only 影子实验全部 try/except 隔离 + 判据预注册（vol_widen :4488 / VWAP A/B :4752 / BE A/B/C :6091 等）——**顶级实验基建，修复时不得破坏** | `pipeline.py` |
| E12 | 测试 140 个文件，四条命脉（admission/退出/资金安全/执行）有覆盖；CI 仅 pytest，无 lint/类型/覆盖率，`pip install \|\| true` 静默吞失败 | `tests/`, `.github/workflows/ci.yml` |
| E13 | 100 份辩论 / 31,152 行 / 145 个 DEB 引用 commit；50+ cron 散养；handoff.md 295KB 无索引 | `docs/debates/`, crontab |

---

## 四、关键发现

### A. 量化侧（根问题：钱从哪来、漏到哪去）

**A1. 方向预测无 edge——三方证据链闭合，不再重开。**
STRATEGY-ROADMAP 二项检验 p=0.42（97 笔）；DEB-20260815 §13.2 复算：信号层 IC 弱正
（DA t1 +0.04~+0.12）但选股+执行链 -0.15~-0.20 负 alpha，正信号池被选成更差子集；
E2 重放正收益剥 beta 后 100% 来自 SPY。本窗口 [R4] 独立复核：LONG n=224 净 -$2,198，延续同方向。

**A2. 唯一已验证的两个相对优势都是"减法"：**
- Gate 负向筛选有真 edge（7/21 被 BLOCK 37 只当日方向胜率仅 27%、均值 -1.14%，反事实实测）；
- 退出锁利是利润引擎：[R5] SL_TRAIL_LOCK n=61 +$2,110 WR 91.8%，是全部净利来源。
- 出血口同样是结构性的：SL_INITIAL/MFE_STALL/TIME_EXIT 合计 n=70 **-$3,835**（后两者 0% WR）——
  "死单"治理（dead_signal 60min/MFE0.2% 已毕业）方向正确但尚未在账本上兑现（MFE_STALL/TIME_EXIT 8 月仍各 31 笔）。

**A3. 费用结构是判决书（本审计最高优先级发现）。**
[R1][R2]：平均名义 $3,717，单腿佣金地板 $1.99 → **$4.46/笔 ≈ 12.0bps 往返**；
毛 edge -12.7bps 背 12.0bps 成本——**即使毛 edge 修到 0，仍净亏 12bps**。费用占净亏损 49%。
DEB-ruler §六 已证换套餐≈零收益（三套餐地板同为 $1.99/单），唯一杠杆 = **提名义 + 降笔数**。
当前 max_daily_positions 12→16（8/20，DEB §51-54）是攒样本期的合理裁决，但它与费用结构相悖：
**样本量目标与单位经济学目标在现客单价下不可兼得**，终裁后必须二选一或改客单价。

**A4. 方向结构错配：系统饿死自己最有 edge 的方向。**
[R4] SHORT 仅 33/257（12.8%）。而历史证据（STRATEGY-ROADMAP：SHORT+closing_drive 唯一 alpha；
memory：T+1 IC 反预测由 BUY 稀释、SHORT 侧正）指向 SHORT 是唯一稳健 edge。
DEB-20260819 §51 已实证根因：DA prompt 三条结构性约束全 LONG 侧、无对称 SHORT 触发规则；
IC 门代码对称但喂门数据单边（137 BUY 样本算的 IC 否决 SHORT，AVGO 案实锤）。
⚠️ 口径自律：本窗口 SHORT n=33 刚过门槛且净 -$155（非正），"SHORT 有 edge"的结论
**只能引用历史 era 证据 + 本窗口"亏得更少"的方向性观察**，不能引用为本窗口证明。
📎 2026-08-20 交叉更新：DEB-20260816 §19 方向反事实尺子补充周期维度——BUY T+1 命中
41.4%（n=203，连续5周<50%，反指）但 T+5 57.5%（n=174，正指）；UE SHORT 池亦首日亏、
五日赚（T+5 费后 +2.369%，n=45）。即"方向错配"之外还叠着**周期错配**（日级收割周级
alpha）；DA 池反事实做空费后 -0.035% → §13.4"DA 无空头 edge"在 n=440 下维持。
方向再权重属机制变更，排队终裁后（§19.6 M1-M3）。
📎 2026-08-20 Qoder 复核更正（详见讨论记录第 3 轮）：本段引 §51 三处已过时 —
① "DA prompt 三条结构性约束全 LONG 侧"：该三约束位于 legacy battle plan prompt（死代码，
legacy_battle_plan_enabled=false）；DA 现役 prompt（deep_research_prompts.py）方向未预设，
真实根因在候选生成层（conviction 择优 + ATR≥3% prefilter，§54.0 根源表）。
② "137 BUY 样本算的 IC"：IC 真实样本基是 signal_snapshots（832 LONG/229 SHORT/903 NEUTRAL），
问题是 pooled 跨方向聚合掩盖方向异质性，非"数据单边"。
③ "AVGO 案实锤"：该 SHORT 实经 UE 通道成交、无实际损失（§53），作为损失案例不成立。
IC 门方向条件化已于当日落地（§54 刀三），"喂门污染"批评已修复。

**A5. 元问题：小样本上的高频迭代 = 过拟合工厂。**
8 月 19 个交易日：320 commits / unified_config 被改 56 次 / pipeline.py 45 次 /
单日 53 节辩论（DEB-20260819），对应新增样本仅 114 笔。~30 道入场门 + 十余退出参数
在同一 257 笔样本上反复切片，任何子格 n<30 的"改善"不可辨识。
[R3] 的剪刀差（-7.35 → -11.4/笔）~~与该机制相容~~ **复查轮已裁决为 regime 换档（见分歧 1 ✅），
不再作为过拟合证据引用**（原归因假设级，现已结案：构成重加权 -7.47 ≈ Jul 实际 -7.35）。
迭代纪律工具是齐的（OKR"一次一变量"、预注册、ADF、GO/PIVOT/STOP），
但**辩论过程本身每天在同一小样本上产生多个隐式假设检验**——这是纪律的盲区。

**A6. 对 8/29 终裁的预判（只摆读数，不替框架说话）。**
handoff 基线：日净均值 -$40.96（t=-1.44）、日毛均值 -$2.71（t=-0.10）。
四项 G 判据（OOS≥15 日 / 日净>0 / 日毛利 t>1 / ADF 平稳）当前全部不达标。
除非最后两周 OOS 结构性逆转，诚实输出是 PIVOT 或 STOP。
**建议提前书面承诺接受该输出**——防止裁决日口径购物（本项目已发生过三次口径事故）。

### B. 架构侧

**B1. 出色的（修复中不得破坏）**：fail-close 风控文化（6 层熔断 + 水位模型 + DB 失败禁开仓）；
影子实验基建（E11）；MADP 对抗审计协议；关键路径测试（E12）；数据层容错（多源降级 + ET 归一 + 推送看门狗）。

**B2. 结构性债务（按严重度）**：
1. **God module**（E1/E2）——近期事故（8/17 DA 17 条指令被 generate 覆盖、审计痕迹丢失、冷却窗压制）全部集中在这两个文件，相关性不是巧合；
2. **旁路架构**（E4）——最大供给源不受 guardrail 约束，任何新门只有挂在 `admission.py` 唯一写入口才不是装饰；
3. **退出双路径地雷**（E5）——"改错一个 yaml 键就静默关闭主止损"，靠注释守护的结构必须灰度统一；
4. **三态非状态机**（E6）——毕业通道（sim→live）本身是全项目最该被测试、目前最脆的一环；
5. **SSOT 重复**（E3/E9）——regime 表 3 份、IC 系统 2 套、SL 计算 3 处，是下三次口径事故的伏笔；
6. **文档漂移**（E10）+ handoff 295KB 无索引 + 50 cron 散养（E13）；
7. **小地雷**：sector map 未知标的放行（E7）、SQLite 多写者并发、meta JSON 写回失败丢 peak_price → trailing 失忆、时区混用（`datetime.now()` vs `now_et()`）。

---

## 可选方案

### 方案 A：维持现状（继续退出/gate 微迭代，等样本积累）

- 提出者：现状惯性（非本审计推荐）
- 核心思路：按现有节奏攒样本至终裁。
- 优点：零变更风险，不占验证期名额。
- 缺点：[R3] 显示每笔期望在恶化；费用结构（A3）保证继续亏 12bps+；
  迭代本身在消耗样本独立性（A5）。**数学上没有出路**。
- 适用条件：无。

### 方案 B：结构化修复（本审计推荐）——"先修确定性项，终审后再动机制"

- 提出者：本审计（起案方）
- 核心思路：三阶段，严格区分"现在可做（白名单内）"与"排队（终审后/知情破例）"：
  - **现在（验证期内，全部白名单合规）**：修尺子（费用/单位经济学记分牌）+ 做减法（文档对齐、死代码盘点）+ 攒样本（SHORT 供给只开影子观测）；
  - **8/29 终审**：严格按预注册判据，提前承诺接受 PIVOT/STOP；
  - **终审后（9 月）**：按 §六 执行项 P1/P2 动结构（名义下限、SHORT 供给侧、双路径统一、god module 拆分、三态状态机）。
- 优点：每一步都在宪章纪律内；先解决唯一确定性的亏损源（费用）；把机制变更集中到样本窗口切换之后，避免污染 OOS。
- 缺点：9 月前账本会继续以 ~-25bps/往返 流血（sim 环境，代价可接受）；需要终裁不软化的纪律。
- 适用条件：8/29 终裁输出非 GO。若 GO（概率低但存在），P1 中"降频提名义"改为毕业前置整改项。

### 方案 C：提前 STOP，立即转赛道研究

- 提出者：预设立场（供辩论方对照）
- 核心思路：毛 edge 统计为零 + 负费用结构，不必等到 8/29，现在就停 sim 执行、只留测量。
- 优点：止损最快，研究资源 100% 转向"是否存在 edge"的根问题。
- 缺点：**违反预注册**（终裁日 8/29 已冻结，提前 STOP 属挪门柱的反向形式）；
  且 OOS 窗口仍在产生判据所需样本，提前停止会使终裁本身无法完成。
- 适用条件：仅在出现资金安全事件（水位 RED/熔断失效）时成立。当前 GREEN，不成立。

---

## 关键分歧（预判辩论方攻击面 + 我方立场 + 可验证方式）

1. **分歧点 1："-7.35 → -11.4/笔 恶化 = 过拟合迭代"是因果还是 regime 噪声？**
   - 起案方：跨 config_version 对比只能算假设级（已在口径声明 ④ 自认）。但即使把恶化全部归因 regime，
     "毛 edge≈-12.7bps、费≈12bps"是窗口内静态事实，不依赖该归因。
   - 可验证方式：按 VIX/regime 分层重算 [R3]（`metrics` 表或 entry_vix 字段），若 HIGH_VOL 子格解释全部恶化，则归因改判 regime。
   - ✅ **已裁决 (2026-08-20, 复查轮直查 DB)**：归因 = **regime 换档，非迭代过拟合**。
     构成重加权：若 Aug 按 Jul 的 regime 构成加权，期望 **-7.47/笔 ≈ Jul 实际 -7.35**，
     恶化量 ~97% 由构成解释。机制：Jul = cautious 62.9%+混合；Aug = risk_on 98.2%（112/114），
     而 risk_on 恰为全窗口最差 regime（-11.45/笔, n=130）。
     残差：risk_on 组内 Jul(-8.73, n=18 ⚠️n<30) vs Aug(-11.89, n=112) 差 3.2/笔，方向仍负但量级小、
     Jul 侧样本不足，只作观察。entry_vix 窄带（15.8-18.7）分层无额外判别力，regime 标签是更好的分层变量。
     **对 A5 的影响**：过拟合工厂的担忧仍成立（方法论批评不依赖该归因），但 [R3] 剪刀差本身
     不再是过拟合的证据，从论据中撤下。

2. **分歧点 2：名义下限/降频是"新机制"，验证期白名单不允许。**
   - 起案方：完全承认。故方案 B 将其列入**终审后 P1**，验证期内只做"修尺子"：
     落一个 `fee_unit_economics_scorecard.py`（每笔 fee_bps × 名义分布 × 毛利分位，只读），
     属白名单第一类。若用户要求提前上硬门 → 须按宪章记"知情破例 + 归因影响评估"。
   - 可验证方式：记分牌跑两周，验证"fee≥8bps 笔占比"与"毛利分位"的关系方向。

3. **分歧点 3：SHORT 侧 edge 引用了历史 era 数据，违反七律 ④（禁跨窗口拼接）。**
   - 起案方：认账并已预防——§四 A4 明确本窗口 SHORT n=33 净 -$155 **不构成** edge 证明，
     本审计主张的是"修复 SHORT 供给枯竭"（供给侧事实：DA prompt 无对称规则，DEB §51 已实证），
     而非"加大 SHORT 仓位"。供给修复后 edge 是否存在由新窗口数据判。
   - 可验证方式：SHORT 供给影子观测（record-only）两周，统计 DA/UE 候选中 SHORT 占比变化。

4. **分歧点 4：god module 拆分在验证期动 live 代码，风险 > 收益。**
   - 起案方：同意风险存在，故拆分列终审后，且顺序为"先补 order_executor 测试（现在可做，
     测试不属机制变更）→ 终审后按模块灰度拆，每块全量回归 + 启动冒烟七断言"。
   - 可验证方式：拆分前后 `pytest tests/ -q` 全绿 + `startup_smoke_check.py --wait 90` 七断言通过。

5. **分歧点 5：sim fill 乐观偏差未计入 -24.7bps。**
   - 起案方：成立。WDC 案（DEB §50）已实证市价 fill 漂离线上方 $1.70；本审计的 bps 是 sim 口径，
     live 只会更差。这加强而非削弱"费用结构优先"与"毕业门槛从严"两个结论。
   - 可验证方式：`data/` 内 fill 漂移样本（order_tracking vs 1min bar）统计漂移分布中位数。

---

## 决策

- 选择方案：**待用户终裁**（起案方推荐方案 B）。
- **2026-08-20 用户中途裁决（第 5 轮）**：
  1. **P0-1 终裁预承诺：签**——已落终裁框架防挪门柱第 5 条；
  2. **P0-2 名义下限+降频：不批**——终裁后不自动执行，9 月重提须附新证据；
  3. **P1-1 DA 生成层 SHORT 供给侧：批，排队第一顺位（终裁后）**。
- 决策依据：见 §四（数据）与 §五（分歧预判）。
- 保留空间：若 8/29 终裁为 GO，执行项 P0-2/P1-1 转为毕业前置整改；若 PIVOT，按 §六 全表执行；若 STOP，仅保留 P2 卫生项作遗产整理。
- 负责人：起案方（本文作者）起案；辩论方待指派（建议换模型）；用户终裁。
- 截止时间：辩论方首轮攻击建议 8/22 前（给终裁留一周缓冲）。

---

## 执行项（验证期合规标签：白名单=现在可做 / 排队=终审后 / 破例=需知情破例记录）

### P0 — 生死线
- [ ] **P0-1 [白名单·纪律]** 8/29 终审严格按 `august-verdict-framework-20260831.md` 执行，
      用户提前书面承诺接受 GO/PIVOT/STOP 任一输出；终裁日禁止新口径引入。
      ✅ **已签 (2026-08-20, 用户裁"签")**：预承诺落终裁框架"防挪门柱"第 5 条 + 变更记录
      （判据一字未动，仅加用户侧背书）。
- [ ] **P0-2 [排队]** 费用结构整改：单笔名义下限（建议 ≥$5K，fee≤8bps，理想 3bps）+ 降频。
      验证期内只落记分牌（见 P0-3），硬门 9 月上。
      ⚠️ **2026-08-20 用户裁决：不批**。排队顺序取消，终裁后不自动执行；
      费用侧证据（fee 随名义单调下降，纯算术）仍由 P0-3 记分牌继续积累，
      若 9 月重新提案须附新证据（如名义下限影子反事实）再裁。
- [x] **P0-3 [白名单·修尺子]** 新增只读记分牌 `scripts/fee_unit_economics_scorecard.py`：
      fee_bps 分布 × 名义分位 × 毛利分位，落 `data/fee_unit_economics_latest.json`。
      **已落地 (2026-08-20)**：全窗口复算与 §三 R2 逐位一致（n=256 已平仓 +1 未平仓 WEAV，
      毛 -1210.92 / 费 -1142.71 / 净 -2353.63，费用占净亏 48.6%）；新增发现：
      **名义 Q5 (≥~$5K) 是唯一毛利为正的分位 (+4.0bps, fee 8.4bps)**，Q1 (~$1.9K) fee 高达 23.3bps；
      59.4% 的单笔毛利跑不赢自身费用。cron 06:41 CST (Tue-Sat)。
      📎 2026-08-20 复查轮更正：**"Q5 唯一毛利正分位"不稳健，降级表述**——Q5 净 +$169.54
      去掉最大单笔(+$259.2)后变 -$89.66，正号由单笔驱动非结构性。稳健部分仅为
      **fee 随名义单调下降（Q1 23.3bps → Q5 8.4bps，纯算术）**：P0-2 的费用侧理由成立，
      但引用口径改为"Q5 费用最低、毛利最接近盈亏线"，禁止引用"Q5 已能赚钱"。
      记分牌已内建 `net_total_excl_max_trade` + `n_lt_30_flag` 字段防终裁误引。
- [x] **P0-4 [白名单·攒样本]** SHORT 供给枯竭影子观测（record-only）：每日统计 DA/UE/generate
      三路候选与成交中 SHORT 占比、拒因分布。零行为变化。
      **已落地 (2026-08-20)** `scripts/short_supply_funnel_report.py`：漏斗各层 SHORT 占比
      instructions 13.3% → triggered 13.1% → executed 12.3% → closed 12.9% — **占比全程平坦，
      枯竭发生在生成层而非门禁层**（DA 仅产 11 SHORT 指令 vs 303 BUY）；UE 是 SHORT 主供给
      （成交 SHORT 18/33 来自 UE，且唯一净正通道 +$30.71）。cron 06:44 CST (Tue-Sat)。
- [x] **P0-5 [白名单·纪律]** n<30 子格机器断言（Qoder 攻击点3 补登，用户裁"执行"）：
      记分牌/审计脚本对任何 n<30 子格打 `n_lt_30_flag` 只读标记，防小样本读数驱动参数变更
      （A5 过拟合工厂的唯一机器防线，纯记录不拦行为）。
      **已落地 (2026-08-20 复查轮)**：`fee_unit_economics_scorecard.py` 名义分位与费用分档
      全部内建 `n_lt_30_flag` + `net_total_excl_max_trade`（单笔稳健性）两字段，实跑验证输出
      （Q5 去最大单笔 -$389.85 与独立复算逐位一致）。

### P1 — 结构性（终审后）
- [ ] **P1-1 [排队]** SHORT 供给侧修复：DA prompt 对称 SHORT 触发规则 + IC 门**分方向独立估计**
      （喂门数据按 direction 切分，修 A4/DEB §51 样本污染）。prompt 属信号源改动 → scan-eval 离线 A/B 先行（宪章 §四.3）。
      📎 2026-08-20 Qoder 复核：IC 门分方向独立估计**已落地**（§54 刀二 ic_long/ic_short 输出 +
      刀三 `_direction_conditioned_ic_net`，契约测试 7/7、回归 103 passed、服务重启确认）；
      本项剩余范围收窄为 DA 候选生成层供给侧（bearish 保底席位 / ATR prefilter 方向对称豁免，
      §54.3 排队）。"DA prompt 对称 SHORT 触发规则"表述应更正为候选生成层（DA prompt 本身已方向中性）。
      ✅ **2026-08-20 用户裁"批"：确认排队第一顺位（终裁后）**。范围 = DA 候选生成层 bearish
      保底席位 + ATR prefilter 方向对称豁免，影子先行攒证据再上硬规则。
- [ ] **P1-2 [排队]** 退出双路径统一：dispatcher 收编 trailing_stop，exit_policy.yaml 补 key，
      灰度上线；删除"缺省=启用"注释守护地雷（E5）。
- [ ] **P1-3 [排队]** god module 拆分：顺序 = 补 `order_executor` 测试（此项可提前，测试非机制）→
      按"门序列 / 订单构造 / 执行 / 通知归因"四块拆 pipeline.py 与 order_executor.py，每块独立 PR + 全量回归。
- [ ] **P1-4 [排队]** 三态状态机：shadow/simulate/live 显式状态机 + 双 env 仓位同步 +
      切换演练测试（毕业通道最高测试优先级）。
- [ ] **P1-5 [排队]** guardrail 上收：sector/方向偏斜/名义帽统一挂 `admission.py` 唯一写入口，
      消灭 DA/UE 直写旁路（E4）。分两步：先 record-only 对齐判分，再施规。

### P2 — 卫生（低风险，可与 P1 并行，但 live 行为相关项仍终审后）
- [x] **P2-1 [白名单·减法]** ARCHITECTURE.md 重写到当前事实（Playbook 唯一管线 + 主管线冻结 + 三态现状），
      消除 6/20 版与 AGENT.md 的矛盾（E10）。**已落地 (2026-08-20)**：全文按 file:line 逐条复核后重写。
- [x] **P2-2 [白名单·减法]** 辩论库索引脚本：解析 100 份 DEB 状态栏生成 index.md（防议题重开）。
      **已落地 (2026-08-20)** `scripts/build_debate_index.py` → `docs/debates/index_generated.md`：
      101 份 DEB 全量清单；对账发现 **README 手工索引漏登 92 份**（8/04 后基本停登）—
      漏登清单已落生成文件，逐份补摘要不在本轮范围。
- [ ] **P2-3 [排队]** CI 加强：lint + 类型检查 + 覆盖率门槛；去掉 `pip install || true`。
- [ ] **P2-4 [排队]** sector map 未知标的改 fail-closed（或接动态行业源）（E7）。
- [ ] **P2-5 [排队]** SSOT 收敛：regime 乘数表合一（E3）、两套 IC 系统合一（E9）、SL 计算归一。
- [ ] **P2-6 [排队]** cron 登记收敛 + handoff 拆分/索引工具。
- [ ] **P2-7 [排队]** 时区混用清理：`datetime.now()` 裸调用全部归一 `now_et()`（B2.7）。

---

## 验证项

- [x] §三 [R1]~[R6] 全部数字由辩论方独立重跑（合格标准第一条）。**已完成 (2026-08-20 复查轮)：
      R1/R3/R4/R5/R6 + 落地项 + 工程锚点全部复现 ✅，另抓 Q5 单笔驱动弱点（已写回）。**
- [x] 分歧点 1 裁决实验（VIX/regime 分层重算 R3）。**已完成 (2026-08-20)：regime 换档结案，
      见 §五 分歧 1 ✅。**
- [ ] P0-3 记分牌运行两周后，"费用占净亏 ≥40%"结论复验（本窗口 49%）。
- [ ] P0-4 影子两周后出 SHORT 供给漏斗报告（候选 → 指令 → 触发 → 成交逐级损耗）。
- [ ] 8/29 终裁输出落档；若与本文 §四 A6 预判（PIVOT/STOP）不符，须附 OOS 逆转的逐日证据。
- [ ] 关闭条件：终裁落档 + P0 四项完成 + P1/P2 排期表被用户批准。

---

## 讨论记录

### 第 1 轮（2026-08-20，起案方）

- **起案方（外部审计 Agent）**：提交全文。核心主张三条：
  1. **量化根问题已闭合**：方向预测无 edge（三方证据链），唯一验证过的优势在"负向筛选 + 退出锁利"；
     当前毛 edge -12.7bps 背 12bps 费用，单位经济学在结构上保证亏损——**费用/规模修复优先于一切策略微调**。
  2. **工程纪律顶级但债务集中在两个 god module 与多条旁路**；近期事故全部落在这批债务上。
     修复顺序 = 先测试后拆分、先 record-only 后施规、一切机制变更让位于 8/29 终裁。
  3. **过程风险预警**：8 月 320 commits / 114 笔新样本的迭代强度与小样本统计容量不匹配；
     建议把"任何 n<30 子格不得驱动参数变更"升级为硬门禁（与七律 ⑤ 对齐，落为 admission/scorecard 机器断言）。
- **自我声明（宪章要求）**：本审计未改动任何 live 代码/配置；全部数字附 SQL 可复现；
  Jul/Aug 对比的归因局限已显式标注（口径声明 ④）；SHORT edge 未做跨窗口拼接（分歧 3）。
- 待：辩论方指派（建议非本模型担任），按 §五 五个分歧点逐一攻击。

### 第 2 轮（2026-08-20，用户裁 + 起案方执行）

- **用户裁**："另外 Agent 已检查，请先做收益大、风险相对的（低）的方面进行修复"。
  解读：批准白名单内高收益低风险四项先行；机制类变更（名义下限、SHORT prompt 对称化、
  退出双路径统一、god module 拆分、三态状态机、guardrail 上收）维持排队至 8/29 终裁后。
- **起案方执行（当日完成，零 live 行为变更）**：
  1. **P0-3** `scripts/fee_unit_economics_scorecard.py` 上线（只读，mode=ro 连接）。
     全窗口复算与 R2 账本逐位一致；新增证据：**名义 Q5 是唯一毛利正分位**
     （+4.0bps gross / 8.4bps fee），Q1 fee 23.3bps — 为 P0-2 的"名义下限 ≥$5K"
     提供了首个分位级尺子（仍排队终审后实施）。
  2. **P0-4** `scripts/short_supply_funnel_report.py` 上线（record-only）。
     关键新发现：**SHORT 占比在漏斗各层平坦（13.3%→12.3%）**，证明枯竭在生成层，
     门禁/执行层无不对称 — 这直接为 P1-1 的修复指向"DA prompt 供给侧"而非门禁侧。
     UE 通道 SHORT 成交占全 SHORT 的 55%（18/33）且唯一净正（+$30.71）。
  3. **P2-1** ARCHITECTURE.md 重写：逐条 file:line 复核后按当前事实重写
     （双锁冻结 / playbook_executor 唯一写入口 / exit 三路径现状 / 8 线程调度 / 三态共存）。
  4. **P2-2** `scripts/build_debate_index.py` → index_generated.md：
     101 份 DEB 全量索引 + README 对账（漏登 92 份已清单化）。
  5. cron 两条（Tue-Sat 06:41/06:44 CST，带备份与单行回滚说明）；两脚本均 mode=ro 只读连接，
     无 service restart、无配置变更、无 DB 写入。
- **纪律自检（宪章 §四）**：本轮全部产出 = 修尺子×2 + 攒样本×1 + 做减法×2，白名单内；
  未引入任何新交易机制；未触碰终裁框架判据；数字均可由脚本一键复现。
- 待：辩论方对 R1 五个分歧点的攻击仍开放；P0-3/P0-4 两周后按 §验证项复验。

### 第 3 轮（2026-08-20，Qoder 独立复核 — 辩论方资格轮）

- **Qoder**：应用户令执行 §三 全部数字独立重跑（§决策 辩论方合格标准第一条）+ 三处口径/时效攻击点。
  自我声明：本轮零 live 行为变更，仅本文件文档更正；全部数字本会话 SQL/JSON 重跑可复现；
  已平仓口径 n=256（另 1 笔 WEAV 未平仓无已实现盈亏，金额与原文 n=257 逐位一致）。
- **数字复核：R1-R6 及第 2 轮两脚本产出全部复现 ✅**
  - R1：毛 -1210.92 / 费 -1142.71 / 净 -2353.63；单腿费地板 $1.99 → $4.46/笔 ≈ 12.0bps；
  - R3：Jul n=143 -1050.75（-7.35/笔）→ Aug n=113 -1302.88（-11.5/笔）；
  - R4：LONG n=223 -$2,198.34 WR 40.4% / SHORT n=33 -$155.29 WR 42.4%（12.9%）；
  - R5：SL_TRAIL_LOCK n=61 +$2,109.71 WR 91.8%；SL_INITIAL/MFE_STALL/TIME_EXIT n=70 -$3,835.70；
  - 记分牌 JSON：fee 占净亏 48.6%，费主导单笔 152/256=59.4%，fee 分位 p25=8.0/p50=12.2/p75=16.5bps；
  - 漏斗 JSON：SHORT 占比 instructions 13.3% → triggered 13.1% → executed 12.3% 全程平坦。
- **攻击点 1 — A4/P1-1 引用 §51 已被 §53/§54 推翻或修复的旧叙事（本文件已落两处 📎 更正注）**：
  1. "AVGO 案实锤：net_ic+0.09 拦空" —— §53 实证该 SHORT 经 UE 通道成交（decision_id 可查），
     **无实际损失**，作为损失案例不成立；机制批评保留一半：pooled 门确曾误拦方向正确的 DA 侧
     指令（§54 重放：新门 net=+0.2278 放行 vs 旧法 pooled +0.09 误拦，当日 -5.2%）。
  2. "137 BUY 样本算的 IC" —— 137 为 gate 决策行口径；IC 真实样本基 = signal_snapshots
     （832 LONG / 229 SHORT / 903 NEUTRAL）。"喂门数据单边"表述不准确，真实问题 = **pooled
     跨方向聚合掩盖方向异质性**（news_positive pooled +0.0913/n=519 vs SHORT 侧 -0.3859/n=45
     vs LONG 侧 +0.1083/n=474）。
  3. **IC 门分方向独立估计已于当日落地**（§54 刀二+刀三，契约测试 7/7、全量回归 103 passed、
     服务重启确认）；"DA prompt 三条结构性约束全 LONG 侧"亦已过时（该三约束在 legacy battle
     plan 死代码中，DA 现役 prompt 方向未预设，真实根因在候选生成层 conviction 择优 + ATR
     prefilter，§54.0 根源表）。**P1-1 剩余范围收窄为 DA 候选生成层供给侧**（§54.3 排队）。
- **攻击点 2 — §54.3 "Qoder 196 行未提交 WIP" 不可证实**：工作树（git status 仅 obsidian_vault
  改动）与近期 commit 均无 app/ 或 skill pipeline WIP 痕迹。该证据陈述应删除或更正（落点：
  DEB-20260819-symbol-pangan.md §54.3）；不影响 Q1 暂缓结论——收益证据薄（14 天 n=3）与
  下游瓶颈已由刀一/刀三打开两条独立理由仍成立。
- **攻击点 3 — A5 自我建议未登记为执行项**：第 1 轮提议"n<30 子格不得驱动参数变更"升级硬门禁，
  但 P0/P1/P2 无对应条目。建议补登白名单执行项：scorecard/审计脚本机器断言（只读告警、
  非行为拦截，与七律 ⑤ 对齐）。待用户裁决。
- **立场（对主体结论）**：A3 费用结构为全场最高价值发现（即使毛 edge 归零仍净亏 ~12bps），
  无保留支持方案 B 与 A6 预承诺；周期错配（BUY T+1 41.4% vs T+5 57.5%、UE SHORT 首日亏五日赚）
  建议列入 9 月机制评估序列：P0-2 名义下限 → P1-1 剩余（DA 生成层）→ 周期错配 → P1-2/P1-3。
- 待：用户裁决三处更正注与 n<30 补登项；辩论方正式指派（本轮为资格轮，非正式指派）。

### 第 4 轮（2026-08-20，复查轮 — 用户裁"执行"，第一梯队三项落地）

- **复查方**：对第 1-3 轮全部数字与落地项独立复算（不信描述，逐项直查 DB/文件/crontab），
  并完成分歧点 1 裁决实验与 Qoder 攻击点 3 补登。自我声明：本轮零 live 行为变更
  （记分牌 mode=ro 只读，无 service restart，无配置变更）；产出 = 文档写回 + 只读脚本升级 + git 归档。
- **复算结果：R1/R3/R4/R5/R6 全部逐位复现 ✅**；P0-3/P0-4/P2-1/P2-2 四项落地全实
  （脚本存在、cron 06:41/06:44 在册、产出文件当日新鲜、ARCHITECTURE.md 已按事实重写）；
  E3/E5/E7 工程锚点全部属实（行号微漂：regime 三份 dict 现 1108/3548/3556）；
  Qoder 攻击点 2 核实成立（app/ 零改动，untracked 全是当日审计脚本）。
- **新弱点（本审计未察觉）**："Q5 唯一毛利正分位"不稳健：Q5 净 +$169.54 去掉最大单笔
  (+$259.2) 后变 -$89.66，正号单笔驱动。稳健部分仅 fee 随名义单调下降（纯算术）。
  已写回 P0-3 📎 更正注，引用口径降级为"Q5 费用最低、毛利最接近盈亏线"。
- **分歧点 1 裁决实验 ✅ 结案 = regime 换档，非迭代过拟合**：
  构成重加权（Aug 按 Jul regime 构成加权）= **-7.47/笔 ≈ Jul 实际 -7.35**，恶化量 ~97% 由构成解释。
  Jul = cautious 62.9% + 混合；Aug = risk_on 98.2%，而 risk_on 为全窗口最差 regime
  （-11.45/笔, n=130）。残差 risk_on 组内 Jul -8.73 (n=18 ⚠️n<30) vs Aug -11.89 (n=112)，
  量级小且 Jul 侧样本不足，只作观察。entry_vix 窄带 15.8-18.7 无判别力，regime 标签为更好分层变量。
  A5 过拟合工厂担忧仍成立（方法论批评独立于此归因），但 [R3] 剪刀差从过拟合论据中撤下。
- **P0-5 落地（Qoder 攻击点 3 补登）**：`fee_unit_economics_scorecard.py` 全部子格
  （名义五分位 + 费用分档）内建 `n_lt_30_flag` + `net_total_excl_max_trade` 两字段，
  实跑验证（Q5 去最大单笔 -$389.85 与独立 SQL 复算逐位一致）。只读打标，不拦行为。
- **归档**：本轮全部产出（本文件、四个只读脚本、index_generated.md、ARCHITECTURE.md 重写、
  README/handoff/DEB-20260816 §17-19 追加）单 commit 入库；记分牌产出 JSON 按惯例不入库
  （脚本一键可复现，DEB 内附口径与基线数字）。
- 待：第二梯队（P0-2 名义下限 / P1-1 剩余 / P0-1 预承诺）仍排队等用户裁决。

### 第 5 轮（2026-08-20，用户裁决第二梯队）

- **用户裁**："只签 1 和 3"——即 P0-1 预承诺签、P1-1 SHORT 供给侧批准排队；
  **P0-2 名义下限+降频不批**。
- **执行**：
  1. P0-1 落 `docs/specs/august-verdict-framework-20260831.md` 防挪门柱第 5 条 + 变更记录
     （性质=纪律加固非阈值修改，G1~G5 判据一字未动）；
  2. P1-1 标记排队第一顺位（终裁后），范围 = bearish 保底席位 + ATR prefilter 方向对称豁免，
     影子先行；
  3. P0-2 标记"不批"：终裁后不自动执行；费用侧证据继续由 P0-3 记分牌积累，
     重提须附新证据（如名义下限影子反事实）。
- **备注**：用户不批 P0-2 与 sim 期"数据量优先"偏好一致（降频与攒样本目标冲突，
  用户选择保样本供给）。尊重该裁决，不再游说；费用证据链保持完整记录以备 9 月复审。
