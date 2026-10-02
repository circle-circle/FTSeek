# DEB-20261002: 标的范围与策略方向 — 能否降低「入场即错」

> **状态**: 提案待裁决 + 五项减法刀**已被本提案自己的预注册判据否决**(撤回登记)
> **待谁裁**: 用户(frontier F1–F5);复查 agent 先跑 §5 证据表与 §9 claims 表
> **目标读者**: 复查者(§5/§9/§11)→ 裁决者(§7/§10)→ 执行者(§8)
> **交易行为变更**: 本文档落地前 **零**。全部动作 = record-only 影子腿 / 遥测补字段 / 撤回登记。
> **样本**: `trade_records` 入场时间 ≥ 2026-06-27(playbook 参数冻结时代)且已平仓、`pnl_net` 非空 = **n=583**
> **回放脚本**: `scripts/deb20261002_*.py`(12 个,已入库可复跑,全部只读 `mode=ro`)

---

## 1. 一句话目标

用户问:「从已有数据、筛选逻辑、全交易阶段看,选什么样的标的范围和策略方向能降低**入场即错**的问题?」

**回放裁决结论(反直觉,是本提案的头条)**:

> **标的范围过滤几乎不降低「入场即错」率(26.8% → 25.6%,仅 −1.2pp),它降低的是费用。**
> 「入场即错」不是某类**标的**的属性,是**入场时点**的属性 —— 死单率在来源/方向/费用线各切片间波动 <20pp 且无一切片 <15%,但随**入场时刻**呈单调梯度(09时段 22.7% → 10时段 32.5% → 11时段 35.0%)。
> 因此「选股轴」这条问题路径**方向错了**;真正的活口只有两个:①**择时轴**(已有 `entry_delay_confirm` 影子在攒,不新开)②**一条方向×状态加法腿**(低波平开空头,n=9,t=+2.60)。

以及一项必须先说的自我否决:**我上一轮口头提出的四刀(K1 费用线/K2/K3/K4),按本仓已预注册的误杀率门(≤40%,`extreme_gap_ban_shadow` 同口径判据)复算,被砍人群误杀率 43%–67%,四刀全部不过门,不予落地。** 唯一过门的是**现役闸本身**(37%)。

### 可判定子目标

| # | 子目标 | 判定命令 | 结论 |
|---|---|---|---|
| S1 | 标的范围过滤能否降低死单率 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_universe_replay.py` | **否**(−1.2pp) |
| S2 | 死单率是否有可识别的人群轴 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_dead_rate_scan.py` | **弱**(来源轴极差 23pp 但最低切片仍 24.0%) |
| S3 | 死单率是否有时刻轴 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | **是**(22.7%→35.0% 单调) |
| S4 | 四刀是否过本仓预注册误杀率门 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_cut_tail.py` | **全否**(43–67% > 40%) |
| S5 | 是否存在统计显著的加法腿 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | **是**(SHORT 低波平开 t=+2.60, n=9) |

---

## 2. 问题陈述(含普查实测)

### 2.1 触发点

上一轮 RFC-129 §③ 用绑定预检否决了「振幅自适应 SL」轴,证据是 `SL_INITIAL` 出血笔 **19/20 MFE < 0.3%**(均 0.04%)⇒ 病灶在**入场即错**,不在 SL 宽度。用户据此追问:那选什么标的/方向能少犯「入场即错」。

### 2.2 「入场即错」的口径定义(陷阱 15)

```
入场即错(死单) := COALESCE(mfe_pct, 0) < 0.3        -- 百分比口径, mfe_pct 权威源 = trade_records
样本 := entry_time >= '2026-06-27'                   -- playbook 参数冻结起点
        AND exit_time IS NOT NULL AND pnl_net IS NOT NULL  -- 未平仓行 mfe 恒 0/NULL, 必排
n = 583 (死单 156 = 26.8%)
```

⚠️ 未平仓行若不排,当天在仓单会全被判死单。本轮回放全部探针都带这两个条件。

### 2.3 死单是真入场错,不是费用问题

| 人群 | n | 毛额 | 费用 | 净额 | 笔均 |
|---|---|---|---|---|---|
| 死单 | 156 | **−9714.3** | 792.6 | −10506.9 | −67.35 |
| 活单 | 427 | +11548.1 | 2535.2 | +9012.9 | +21.11 |

**死单毛额本身 −9714** ⇒ 不是「赚得不够付费用」,是方向/时点错。这与 RFC-129 §③ 的绑定预检结论同源。

### 2.4 既有实现与既有裁决普查(P0,防第二套账本/防重复裁决)

```bash
# ① 同轴影子/门(注册表 + config + 代码)
grep -nE 'id="[a-z_0-9]+"' app/playbook/shadow_registry.py | grep -iE "atr|gap|vol|notional|fee"
#   命中 6 项: p1_3_vol_p50 / gap_chase_gate / gap_alignment / extreme_gap_ban_shadow
#             / fee_gate_shadow / eth_relative_volume_gate
# ② 同轴既有裁决文档
grep -rln "min_notional\|费用线\|NOTIONAL_TOO_SMALL" docs/decisions docs/debates docs/adr
#   命中 12 份, 关键: RFC-071(名义地板 3000→2500) / RFC-118(entry-price reachability)
#                    / RFC-103(volume relaxation shadow) / REVIEW-20260913(shadow verdicts)
grep -rln "low_atr\|低ATR\|低波动" docs/decisions docs/debates docs/adr
#   命中 10 份, 关键: RFC-060(low_atr_gate 2.5%→3.0%, d7f51c4d) / DEB-20260723(低ATR硬门三开关保留ON)
#                    / DEB-20260902-ue-low-atr-precheck(UE 侧预检读同键) / DEB-20260819-symbol-pangan F3
grep -rln "gap_chase\|gap_pct\|跳空" docs/decisions docs/debates docs/adr
#   命中 14 份, 关键: extreme_gap_ban_shadow 已判 NO_ADJUDICATION_FAIL_ON_FALSE_KILL(9/13)
#                    / gap_chase_gate KEEP_SAMPLING(9/21, 判据 2/3, 符号检验 p=0.9252)
```

**普查结论(决定本提案范围收敛)**:

| 我的候选刀 | 撞上的既有裁决/影子 | 处置 |
|---|---|---|
| K1 费用线 | `fee_gate_shadow`(OBSERVING,**n=57 已过 min_n=30**,`graduation_criteria` 明写「10/11 结合影子读数与双窗口回放终裁真门槛」);现役 `admission.min_notional_usd=2500`(RFC-071)+ `min_entry_price=5` | **不新开轴**,只给既有影子**追加候选腿**(F1) |
| K2 SHORT \|gap\|≥8% | `extreme_gap_ban_shadow`(9/13 判 FAIL_ON_FALSE_KILL,误杀率 50%>40%);`gap_chase_gate`(KEEP_SAMPLING) | **撤回**(F3) |
| K3 SHORT 原始ATR≥8% | `low_atr_gate`(RFC-060 收紧至 0.03,DEB-20260723 裁保留 ON);`DEB-20260902-ue-low-atr-precheck` | **撤回**(F3),但暴露口径缺陷(F4) |
| K4 LONG \|gap\|3–8% | `extreme_gap_ban_shadow` 同轴(ag≤−3 腿),已判误杀率不过门 | **撤回**(F3) |
| 砍 UE·LONG | RFC-117(9/29 **用户亲裁**重开 LONG 腿,附反事实证据);RFC-129 §②(UE 转正前必须复核,本人 10/2 登记) | **撤回**(F3),与用户裁决正面冲突且无 P3 数据 |
| 死单率→择时 | `entry_delay_confirm`(D2',OBSERVING,min_n=20,动机正是「SL_INITIAL 出血族尸检 19笔16笔MFE=0/11笔≤25min」) | **移交既有影子**,不新开轴(F5) |

⇒ **本提案的唯一真增量 = F1(影子追加腿)+ F2(一条加法腿影子)+ F4(遥测补字段)**,其余全是撤回与移交。

### 2.5 一处必须自曝的「差点当活缺口」

回放中看到 `US.CTNT` 以 **$0.072**、`US.GIPR` 以 **$0.431** 在 2026-09-17 成交(净 −314.3 / −181.3),而 `admission.min_entry_price=5` 现役 ⇒ 一度判为「EXT 通道绕过价格闸的活缺口」。核时间线后**推翻**:

```bash
git log --format="%h %ad %s" --date=iso -S "EXT_ENTRY_MIN_PRICE" -- scripts/extended_hours_entry_executor.py
# ce54f64eb 2026-09-17 16:59:36 +0800 [hotfix][DEB-20260917 §15.5] 用户裁方案A: 加价格下限闸 $5 挡仙股 (实盘 CTNT/GIPR 教训)
```
成交时刻 `2026-09-17T04:06:14-04:00`(ET)= 当日 16:06 CST,**早于闸上线 16:59 CST**;代码注释 `extended_hours_entry_executor.py:170-172` 逐字点名这两笔为事故依据,`:879-886` `_price_in_range()` 已是真实拒绝路径(非遥测标签)。

⇒ **该缺口已于当天由用户裁方案A 修复,不是活缺口,本提案不重提。** 全窗口 `entry_price<5` 成交 n=5 净 −482.8,其中 2 笔即此事故;闸上线后 0 笔。

---

## 3. 非目标(明确不做)

1. **不改任何现役门阈值**(含 `min_notional_usd` / `min_entry_price` / `low_atr_gate_min_atr_pct`)。F1 只给影子加候选腿,真门槛由 `fee_gate_shadow` 既有 `graduation_criteria` 在 10/11 终裁。
2. **不重开 `extreme_gap_ban_shadow` / `gap_chase_gate` 已裁的 gap 轴**。
3. **不否定 RFC-117(UE LONG 腿)** —— 无 P3 数据(9/29 后 UE 已平仓成交 n=0)。
4. **不建「标的白名单/黑名单」类新工件**。计划/名单类工件的历史坑:被后来人读成「有这只票就是合格这只票」。
5. **不动出场链**(SL/TP/trailing/breakeven)。RFC-129 §③ 已裁「振幅自适应 SL」轴否决,本提案不复活。
6. **不新增台账**。F1 复用 `data/fee_gate_shadow.jsonl`,F2 新增一个 jsonl 但走既有 `shadow_registry` + 既有 cron 载体形态。

---

## 4. 数据与实证表(E 表:每行可复算,期望列 = 作者实跑值)

所有命令工作目录 `/opt/ftseek/FTSeek-US`,解释器 `venv/bin/python`,前缀 `PYTHONPATH=.`。
脚本已入库(`scripts/deb20261002_*.py`),**全部以 `file:...?mode=ro` 只读打开 DB**,零副作用。

| 编号 | 声称 | 复现命令 | 期望输出(作者实跑值,逐字) |
|---|---|---|---|
| **E1** | 死单是真入场错(毛额亦负),非费用问题 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_dead_rate_scan.py` | `死单 n=156 净=-10506.9 均=-67.35 毛=-9714.3 费=792.6`<br>`活单 n=427 净=+9012.9 均=+21.11 毛=+11548.1 费=2535.2`<br>`⇒ 死单毛额=-9714.3 (毛也为负=真入场错)` |
| **E2** | 标的范围过滤**不降**死单率(−1.2pp),降的是费用(费bps 12.1→7.6) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_final_combo.py` | `基线: n=583 毛=+1833.8 费=3327.7 净=-1494.0 均=-2.56 WR=57% 死率=26.8% 费bps=12.1`<br>`K1+K2+K3: n=305 (52%) 毛=+2049.3 费=1314.8 净=+734.4 均=+2.41 WR=63% 死率=25.6% 毛bps=11.8 费bps=7.6`<br>`Δ净=+2228.4 Δ死率=-1.2pp 剔3亏=+2058.0✅ 剔3赚=-382.8🔴`<br>`LONG: n=268 毛= +1971.5 净=  +822.9 均= +3.07 WR= 66% 死率= 24%`<br>`SHORT: n= 37 毛=   +77.8 净=   -88.5 均= -2.39 WR= 43% 死率= 35%` |
| **E3** | 死单率随**入场时刻**上升(择时轴) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | `09:00 死 92 活313 死率= 22.7%`<br>`10:00 死 38 活 79 死率= 32.5%`<br>`11:00 死  7 活 13 死率= 35.0%`<br>`12:00 死  5 活 10 死率= 33.3%` |
| **E4** | 死单率无一切片 <15%(不是某类标的的属性) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | `DA       LONG  n=371 死率= 24.0%`<br>`DA       SHORT n= 37 死率= 16.2%`<br>`UE       LONG  n= 51 死率= 31.4%`<br>`reentry  LONG  n= 17 死率= 47.1%`<br>`LONG  过费用线       n=268 死率= 24.3%`<br>`SHORT 不过费用线      n= 38 死率= 28.9%` |
| **E5** | **四刀被砍人群误杀率全超 40% 预注册门**(48/51/43/67/53%) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_cut_tail.py` | `K1 全拒 (价<26 或 名义<4200)              240   90   -1775.3     -893.0    -2509.3    48%🔴>40% ✅剔尾仍负(该砍)`<br>`K1a 拒 (价<26)                         45   23    -775.3     +107.1    -1441.3    51%🔴>40% 🔴剔3亏翻正(尾部主导)`<br>`K2 拒 (SHORT \|gap\|>=8%)                7    5    -429.4      +17.4     -372.2    43%🔴>40% 🔴剔3亏翻正(尾部主导)`<br>`K3 拒 (SHORT 原始ATR>=8%)               42   22    -535.2      -42.6     -757.2    67%🔴>40% ✅剔尾仍负(该砍)`<br>`K4 拒 (LONG \|gap\|3-8%) [已撤]           51   34   -1368.4     -309.2    -1828.1    53%🔴>40% ✅剔尾仍负(该砍)` |
| **E6** | 唯一过误杀率门的是**现役闸**(37%) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_cut_tail.py` | `K1c 拒 (价<5 或 名义<2500)            n=  71 净=  -1114.0 剔最亏3票=   -442.8 误杀率= 37% ✅` |
| **E7** | K1 全(价≥26∧名义≥4200)**双窗反号**(IS −4.91 / OOS +4.22),不可落地 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_k1_components.py` | `基线 n=583 净=-1494.0 均=-2.56 死率=26.8% 费bps=12.1`<br>`K1 全 (价>=26 且 名义>=4200)         343   +281.3   +0.82 25.1%    7.6   -4.91   +4.22  +1566.8   -820.0 4/5  🔴双窗反号 剔3赚翻负🔴`<br>`K1a 仅价>=26                      538   -718.7   -1.34 27.1%    9.1   +0.69   +2.42   +662.6  -1762.1 3/5  ✅双窗改善 剔3赚翻负🔴`<br>`K1c 价>=5 且名义>=2500 (现役)         512   -380.0   -0.74 25.6%    9.4   +0.49   +1.82   +998.4  -1450.0 4/5  ✅双窗改善 剔3赚翻负🔴` |
| **E8** | 加法腿 SHORT×原始ATR<3%×\|gap\|<1% 显著(n=9, t=+2.60, 日聚类 t=+2.67) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | `n=9 均=+67.14 σ=77.57 t=+2.60 (df=8)`<br>`t 分布临界值 (单侧 p=0.05, df=8) ≈ 1.860 ⇒ 过`<br>`日聚类: 交易日=8 日净均=+75.53 σ=80.16 t=+2.67` |
| **E9** | 同腿死单率 **0%**(基线 SHORT 28.3%),逐月全正,双向剔尾稳 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_universe_replay.py` | `n=9 毛=+680.3 净=+604.3 均=+67.14 WR=89% **死率=0%** (基线SHORT死率28.3%)`<br>`逐月: 07:n3/+159 08:n2/+120 09:n3/+317 10:n1/+9`<br>`low_atr_gate 上线(7/24)后: n=9 净=+604.3 ⇒ 该腿活着 (止损口径 atr_pct 与原始 atr 不同物, 门未拦)` |
| **E10** | `low_atr_gate` 上线后 SHORT 侧原始 ATR 轴**单调**(<3% +699 → ≥8% −559) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_short_atr_bands.py` | `门后 SHORT n=79 (atr 有效 79)`<br>`ATR<3%     n= 19 净=  +699.2 均= +36.80 死率= 21% 日期=2026-07-28→2026-10-01`<br>`3-5%       n= 10 净=   +83.6 均=  +8.36 死率= 10% 日期=2026-07-30→2026-09-14`<br>`5-8%       n= 10 净=   -10.1 均=  -1.01 死率= 20% 日期=2026-07-27→2026-10-01`<br>`>=8%       n= 40 净=  -558.8 均= -13.97 死率= 25% 日期=2026-07-24→2026-09-30` |
| **E10b** | 同轴全期读数:SHORT 低波双窗正+剔尾稳,LONG 同档反号 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_atr_zero_diagnosis.py` | `ATR<3%         SHORT    21   +857.5   +677.7  +32.27  62%   24%   +74.5  +16.45  +42.01   +919.7 ✅双窗正+剔尾稳`<br>`3-5%           SHORT    13   +204.6   +148.4  +11.41  46%    8%   +36.5   +8.14  +16.65   +310.3 ✅双窗正+剔尾翻负🔴`<br>`5-8%           SHORT    18    -88.5   -166.2   -9.23  56%   33%    -9.2  -16.59   -1.88    +10.3 ⚠️双负`<br>`8-12%          SHORT    10    -96.5   -138.6  -13.86  60%   30%   -18.5  -19.25  -10.27   +127.6 ⚠️双负`<br>`12-20%         SHORT    13   -239.2   -294.0  -22.62  85%    8%   -33.3   +0.00  -26.92    +48.9 ⚠️窗内n不足`<br>`>=20%          SHORT    19    -23.5   -102.5   -5.40  58%   32%    -2.4  -18.50   +2.25   +168.6 🔴反号` |
| **E11** | `metadata.atr` 与门的 `inst.atr_pct` **不是同一物**(18/18 不一致) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_atr_caliber_check.py` | `⇒ md<3% 中 inst 也 <0.03 (门该拦): 0/18`<br>`⇒ md<3% 中 inst >=0.03 (门不拦): 18/18` |
| **E12** | `metadata.atr` 是**百分数**口径;`atr=0` 是缺值哨兵(43 行,全 7 月,净 +323.6) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_atr_zero_diagnosis.py` | `n=583 \| atr 为 NULL: 11 \| atr == 0 (缺值哨兵): 43`<br>`非零 atr: n=529 min=0.097 中位=8.400 p25=4.230 p75=20.290 max=109.430`<br>`⇒ 口径 = **百分数** (中位 8.40%)`<br>`零值行结局: n=43 净=+323.6 均=+7.53 死率=33% 月份=['2026-07']` |
| **E13** | `low_atr_gate` 上线(7/24)后 SHORT 低波腿仍成交 19 笔 ⇒ 门对该腿不生效 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_short_atr_bands.py` | `门上线 (2026-07-24) 前: 2 笔 净=-21.5`<br>`门上线后:            19 笔 净=+699.2`<br>`✅ 门上线后该腿仍有 19 笔成交, 净=+699.2`<br>`⇒ 该腿现在活着, 不是样本饿死` |
| **E14** | `low_atr` skip 行**无法从 DB 判定意图方向**(direction 列全 `'SKIP'`) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_k1_components.py` | `2026-09-14 US.LLY    direction列='SKIP' atr_pct=None 意图方向=None \| low_atr(0.0246<0.03, 低波票过不了执行门)`<br>`2026-09-29 US.RTX    direction列='SKIP' atr_pct=None 意图方向=None \| low_atr(0.0198<0.03, 低波票过不了执行门)`<br>`意图方向集合: {'None'}` |
| **E15** | UE·LONG 刀无 P3 数据(n=0),且亏损 84% 落在不过费用线侧 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_ue_long_period_split.py` | `P1 <8/3 (LONG双向开·旧)            LONG  n= 22 毛=  -145.5 费= 112.5 净=  -258.0 均= -11.73 WR= 45% 死率= 36% 费bps= 13.8`<br>`P1 <8/3 (LONG双向开·旧)            SHORT n= 12 毛=  +225.9 费=  72.2 净=  +153.6 均= +12.80 WR= 83% 死率= 25% 费bps= 16.3`<br>`P2 8/3-9/28 (只取SHORT)          LONG  n= 29 毛=  -387.6 费= 195.6 净=  -583.2 均= -20.11 WR= 55% 死率= 28% 费bps= 13.6`<br>`P2 8/3-9/28 (只取SHORT)          SHORT n= 24 毛=   +39.3 费= 148.7 净=  -109.4 均=  -4.56 WR= 58% 死率= 29% 费bps= 11.2`<br>`P3 >=9/29 (RFC-117重开LONG)      LONG  n=0`<br>`P3 >=9/29 (RFC-117重开LONG)      SHORT n=0` |
| **E16** | P2 期 UE·LONG 29 笔全走 `strategy_id=playbook_sim`(DA 侧消化,非 UE 直写) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_ue_long_period_split.py` | `strategy_id 分布: {'playbook_sim': 29}` |
| **E17** | 名义 $2500–4200 带净 −641.8,逐月全负 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_notional_band.py` | `n=138 毛=-13.2 费=628.6 净=-641.8 均=-4.65 死率=28%`<br>`2026-07: n=121 净=  -109.2`<br>`2026-08: n=  8 净=  -472.4`<br>`2026-09: n=  9 净=   -60.2` |
| **E18** | 「该带无降仓单」**不可判定**(metadata 无任何 halv/flip/size 类键) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_final_combo.py` | `metadata 顶层键共 137 个; 含 halv/flip/revers/size/reduce/trim 的键: 无`<br>`⇒ 若无任何此类键: '0/138 带降仓标记' 不是'无降仓单'的证据, 只是词表不存在 (自证陷阱)` |
| **E19** | `fee_gate_shadow` 活着且已 READY(n=57>30),4 条候选腿**无 price<26 腿** | `wc -l data/fee_gate_shadow.jsonl && venv/bin/python -c "import json;d=json.load(open('data/fee_gate_shadow_latest.json'));print(d['n_total'], sorted(d['gates']))"` | `57 data/fee_gate_shadow.jsonl`<br>`57 ['combo_price_1_notional_4000', 'notional_lt_4000', 'notional_lt_5000', 'price_lt_1']` |
| **E20** | `price_lt_1` 腿 blocked n=0 ⇒ 该腿零命中($5 闸已挡) | `venv/bin/python -c "import json;d=json.load(open('data/fee_gate_shadow_latest.json'));print(json.dumps(d['gates']['price_lt_1']['blocked']))"` | `{"exp_per_trade": null, "fee_bps": null, "n": 0, "pnl_sum": 0.0, "win_rate": null}` |
| **E21** | 当前时代(9/1起)不过费用线 = 32/185 (17%),净 −522.4 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_k1_current_era.py` | `不过费用线: 32/185 (17%)`<br>`合计不过线        n= 32 毛=  +295.8 费= 818.2 净=  -522.4 死率= 19%` |
| **E22** | 当前时代名义中位 $5753、p10 $4783 ⇒ 名义 4200 闸现世只咬 6% 流量 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_k1_current_era.py` | `n=185 min=2280 p10=4783 p25=5268 中位=5753 p75=5878 max=6076`<br>`名义<$4200: 11/185 (6%)` |
| **E23** | 当前时代价格闸才咬人:价<$26 = 22/185 (12%) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_k1_current_era.py` | `min=0.07 p10=18.89 中位=213.39 max=1803.29`<br>`价<$26: 22/185 (12%)` |
| **E24** | 被拒人群**毛额为正**(=费用问题非入场问题);费bps 按价格档 337/36/17 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_cut_tail.py` | `n=45 毛=+262.0 费=1037.3 净=-775.3 费bps=44.6 死率=22%`<br>`⇒ 毛额 +262.0 (正=费用问题), 费 1037.3 是净亏的主因`<br>`<$5      n=  4 毛=  +116.2 费= 599.0 净=  -482.8 费bps= 337.3`<br>`$5-10    n=  6 毛=  +337.9 费= 124.7 净=  +213.1 费bps=  36.0`<br>`$10-26   n= 35 毛=  -192.0 费= 313.6 净=  -505.6 费bps=  17.4` |
| **E25** | 9月起指令行 `atr_pct<0.03` 仅 27/344 ⇒ F2 不能复用 `atr_pct` 分档 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | `9月起 指令行 atr_pct<0.03 (止损口径): 27`<br>`9月起 指令行有 atr_pct: 344`<br>`⇒ 影子若要按原始 ATR 分档, 需要新增采集字段, 不能复用 atr_pct` |
| **E26** | CTNT/GIPR $0.07 成交早于价格闸上线 53 分钟 ⇒ 缺口当天已修,非活缺口 | `git log --format="%h %ad %s" --date=iso -S "EXT_ENTRY_MIN_PRICE" -- scripts/extended_hours_entry_executor.py` | `ce54f64eb 2026-09-17 16:59:36 +0800`<br>`用户裁方案A`<br>`CTNT/GIPR` |

> 期望列为脚本实跑输出的**逐字行**(含全角空格),已用 `rebuild_e_table2.py` 逐格验证 27/27 命中。
> 复查者跑同一命令后 `grep -F` 任一行即可比对;行内 `|` 已转义。

**integrity 块(产物类数字统一声明)**:

```
n               = 583 (E1/E2 分母), 各切片 n 见表内
window          = entry_time ∈ [2026-06-27, 2026-10-01] (playbook 参数冻结起点 → 最近交易日)
universe        = trade_records 全量已平仓 PLAYBOOK 类交易 (source_type ∈ PLAYBOOK_SIM/
                  PLAYBOOK_LIVE/PLAYBOOK_SHADOW/MANUAL_PLAYBOOK, 另含 POSITION_SYNC 5 行/
                  MAIN_PIPELINE 3 行, 占比 <2% 未剔除 — 见 §10 R6)
truncation_checked = True — 逐笔 SQL 单次全表读取 (无分块/无分页/无 LIMIT),
                  且 exit_time IS NOT NULL ∧ pnl_net IS NOT NULL 双条件;
                  未平仓行排除数已在 E1 分母差 (583 vs 全史 932) 中体现
双窗            = IS [<2026-08-20] n=229 / OOS [>=2026-08-20] n=354
逐月            = 2026-06..2026-10 共 5 个月 (06/10 月 n<3 的月份只标注不判定)
```

---

## 5. 方案设计

### 5.1 架构(2 句)

**不新建任何门。** 三个动作:①给既有 `fee_gate_shadow` 追加 2 条候选腿(价格/名义真门槛由它 10/11 既有终裁一并裁);②为唯一显著的加法腿(SHORT 低波平开)开一个 record-only 影子,预注册毕业判据;③补两处遥测字段(`low_atr` skip 行的意图方向、原始 ATR),否则 F2/F4 无法事后归因。

### 5.2 口径铁律

1. **两个 ATR 不是一个东西**(E11/E12):`metadata.atr` = 原始盘前口径、**百分数**、含 `0` 缺值哨兵;`playbook_instructions.atr_pct` = 止损口径(含 VIX 缩放/三层底限/分钟兜底)、**比例**。任何跨这两者的阈值比较都是错的 —— 本轮我自己先错了一次(§11 自曝 ①)。
2. **误杀率口径统一**用 `extreme_gap_ban_shadow` 的注册判据:被拦笔中**盈利笔占比 ≤40%**。不用 `mfe<0.05%` 死单子集口径(9/13 已留痕纠错:两口径不可混用)。
3. **剔尾检验算被砍人群,不算保留侧**(§11 自曝 ③):一把刀只被少数极端亏损票支撑时,`剔最亏3票` 会翻正 ⇒ 尾部主导,不该砍。
4. **毛/净双列**(README 预注册门径规范 §4):费用 era 均值 $4.48/笔 = 名义 14.6bps,口径翻转可逆转结论。本轮 E17/E24 就是靠双列才把「费用问题」和「入场问题」分开。
5. **死单率分母**必须排未平仓行(§2.2)。

### 5.3 F1:`fee_gate_shadow` 追加候选腿(零新台账)

```python
# scripts/fee_gate_shadow.py:29-45 — 现状 (E19)
CANDIDATE_GATES = ("price_lt_1", "notional_lt_4000", "notional_lt_5000",
                   "combo_price_1_notional_4000")
# 提案追加 2 条 (阈值来自 E23/E24 的费bps 分档, 不是我拍的):
#   "price_lt_26"                -> entry_price < 26.0
#   "combo_price_26_notional_4200" -> entry_price < 26.0 or notional < 4200.0
```

- **为什么是这两条**:`price_lt_1` 已零命中(E20,$5 闸挡住了),现役候选腿无法回答「真门槛该是 $5 还是 $26」。E23 显示费bps 在 $5 以下 337.3、$5–10 36.0、$10–26 17.4 ⇒ 价格轴有梯度,值得让影子自己量。
- **为什么不直接改 config**:E7 显示 K1 全**双窗反号**(IS Δ−4.91),E5 显示误杀率 48% > 40% 门 ⇒ 按本仓判据**不够格直接落地**,只够格进影子。
- **不改 `graduation_criteria`**:既有判据逐字保留(n≥30 ∧ 各门 blocked/kept 净期望/胜率/费bps/n-loss 机械产出 ∧ 10/11 终裁 ∧「样本不足或读数反向时禁止提议转正」)。追加腿 = 只加数据列,不加判据。
- **schema 兼容**:`build_shadow_row()` 输出 dict 由 `CANDIDATE_GATES` 驱动 ⇒ 追加后新行多两个键,旧行缺键。`fee_gate_shadow_report.py` 需对缺键腿 fail-open(读不到就跳过,不当 0)。

### 5.4 F2:SHORT 低波平开腿影子(record-only,新 jsonl)

**假设**:空头产出集中在「低波动 + 平开」的阴跌结构票,不在高波爆款票(E8/E9/E10)。

```
影子 id   : short_lowvol_flat_open
人群      : direction=SHORT ∧ 原始ATR<3%(百分数口径) ∧ |aligned_gap_pct|<1%
数据源    : 入场时点新增采集 raw_atr_pct (F4 字段) + 既有 metadata.entry_gap
产物      : data/shadow/short_lowvol_flat_open.jsonl (append-only)
零副作用  : 不改入场/仓位/订单/配置/退出参数;不下单;不拦单
毕业判据  : (预注册,见 §7) n≥30 ∧ 笔均净>0 ∧ 日聚类 t≥1.86 ∧ 死单率≤15% ∧ 误杀率口径不适用(加法腿)
```

- **为什么必须新增采集字段**:`atr_pct`(止损口径)9 月起 `<0.03` 仅 27/344(E25),而本腿判据用的是原始口径 ⇒ 复用 `atr_pct` 会把人群认错。E11 的 18/18 不一致是硬证。
- **为什么是加法不是减法**:E5 证明减法轴全不过误杀率门;E8/E9 的加法腿死单率 **0%**、t=+2.60 过显著 ⇒ 唯一有统计支撑的方向是「多给这类空头机会」,不是「拦掉某类票」。这也正面服务用户核心原则「必须有真空头产出」。
- **供给面疑问(frontier F2-b)**:该腿历史出现率 ≈ 0.134 笔/交易日(E8),攒到 n=30 需 ≈ 224 交易日 ⇒ **纯前向攒样不可达**。按用户 8/14 方法论偏好(「历史数据/重放可行时直接回测裁决,不默认前向攒样」),正解是**离线回放器**(按日重扫候选池,标记本腿命中)而非在线影子。这是 F2 待裁的核心。

### 5.5 F4:两处遥测补字段(零行为变更)

| 缺口 | 现状(E14/E25) | 补法 |
|---|---|---|
| `low_atr` skip 行意图方向 | `direction` 列写 `'SKIP'`(被 skip 覆写)、`atr_pct` 列 `NULL`、`raw_instruction_json` 无 direction 键 ⇒ **8 行全部无法判定方向** | skip 覆写 `direction` 前,把原意图方向写入 `metadata_json.intent_direction`;`atr_pct`/`_atr_basis` 一并落列(代码里 `_atr_basis` 已算出但只进日志) |
| 原始 ATR | 只有止损口径 `atr_pct`;`metadata.atr` 是成交后才有的入场快照,指令行没有 | 指令生成时落 `raw_atr_pct`(原始口径、比例单位、与 `atr_pct` 并列不替换) |

**为什么这是 F2/F4 的前置**:没有 `intent_direction`,「`low_atr_gate` 该不该加 LONG-only」永远无法判定(§11 自曝 ④:我一度声称「零行为变更」,依据是 8 行里无 SHORT —— 但那 8 行的方向字段全是 `'SKIP'`,等于没看);没有 `raw_atr_pct`,F2 的人群定义在生产链上无法复现。

### 5.6 模块线索表

| 模块 | 生产者 | 消费者 | 工件 | 幂等键 | 失败模式 | 回滚 |
|---|---|---|---|---|---|---|
| F1 影子腿 | `scripts/fee_gate_shadow.py`(cron `45 6 * * 2-6`,现役) | `fee_gate_shadow_report.py`(cron `47 6`,现役)→ 10/11 终裁 | `data/fee_gate_shadow.jsonl`(57 行→增长) | `(trade_id, gate)`;既有去重不变 | 新腿键缺失使 report 抛 KeyError | 从 `CANDIDATE_GATES` 删两条 + report 加缺键 fail-open;**不动既有四腿** |
| F2 回放器 | 新 `scripts/short_lowvol_replay.py`(手动/按需,**不挂 cron**) | 裁决时人读 + 本 DEB §12 追加 | `data/reports/short_lowvol_replay_<date>.md` | 报告文件名含日期 | 回放宇宙与实盘候选池不一致 ⇒ 读数不可外推 | 删脚本 + 删报告;零生产影响 |
| F4 遥测 | `app/playbook/pipeline.py`(skip 覆写点)+ 指令生成点 | 事后归因(SQL)+ F2 回放器 | `playbook_instructions.metadata_json` / 新列 | 随指令行 UPSERT,既有幂等 | 字段缺失 ⇒ 归因回到「不可判定」(现状),不产生错误结论 | 删字段写入行;旧行留空,fail-open |

**接线点唯一性**:F1 只改 `CANDIDATE_GATES` 元组 + `gate_decisions()` 两处(同一函数);F4 的 `intent_direction` 只在 skip 覆写 `direction` 的那一处写(不是多处补写)。

### 5.7 历史坑映射

| 历史坑 | 本设计如何规避 |
|---|---|
| `mfe` 未平仓行恒 0 ⇒ 假死单(陷阱 15) | §2.2 口径写死双条件;所有脚本共用同一 WHERE |
| 列存在≠有数据 / 假设值自证(MEMORY) | E18 显式登记「metadata 无 halv/flip 键 ⇒ 该带无降仓单**不可判定**」,不写成结论 |
| 量纲混用(`dict.get None→or default`) | §5.2 铁律 1 + E11/E12 实锤;F4 新字段命名带单位语义(`raw_atr_pct`) |
| 尾部主导当轴信号(T18) | E5/E9 双向剔尾(剔最亏3票 + 剔最赚3票);K1a/K2 因剔3亏翻正被否 |
| 单窗口/单月主导(T10-9、陷阱 3b) | E7 双窗 + 逐月改善计数;K4 因逐月混合被降级 |
| 跨期样本否定当期裁决 | E15/E16:UE·LONG 按三个配置期切分,P3 n=0 ⇒ 明示「无发言权」,撤回该刀 |
| 同批笔挂两个判据(RFC-109⑥ 双转正打架) | F1 不新建影子(复用 `fee_gate_shadow` 同一批笔同一判据);F2 人群与 gap 轴**正交**(gap<1% vs gap≥3%/≥5%),无重叠笔 |
| 影子只有写者没有结算者 = 死影子 | F1 消费者现役(cron + report + 10/11 终裁);F2 **明确不开在线影子**(供给率不可达),改离线回放器 + 一次性裁决,避免造死账本 |
| 计划/名单类工件被读成资格凭证 | §3 非目标 4:不建白名单;F2 只产报告,不产名单 |
| 死门/恒 False 门(MEMORY 死门检测) | E11+E13+E25 三连:`low_atr_gate` 对低波空单**结构上不生效**(18/18 被止损口径底限抬过阈值)⇒ 登记为 F4 待核,不假设它在工作 |

---

## 6. 决策依据最小充分集(README 预注册门径规范对齐)

| 项 | 本提案状态 |
|---|---|
| ① 固定样本来源 | ✅ `trade_records` 单表、WHERE 写死(§2.2)、9 个脚本入库可复跑 |
| ② 多窗口(≥2 段) | ✅ IS [<8/20] n=229 / OOS [≥8/20] n=354;另加逐月 5 段 + UE 三配置期 |
| ③ 配对/对照臂 | ✅ 每刀对照「基线全量」;K1 另设 6 个变体互为对照(K1a/K1b/K1c现役/K1d/K1e);SHORT 侧以 LONG 同档为方向对照(E10 vs LONG 档) |
| ④ 尾部(最差 5% / 单笔集中度) | ✅ 剔最亏3票 + 剔最赚3票双向(E5/E9);标的级聚合后剔(非笔级),防同票多笔被当独立 |
| ⑤ 费用后口径 | ✅ 全部毛/净/费bps 三列;E24 用毛额符号区分「费用问题 vs 入场问题」 |
| ⑥ 机制诊断 | ✅ E3(时刻梯度)+ E4(切片无差异)+ E23(费bps 价格梯度)+ E11(口径不一致机制) |
| ⑦ 功效/显著性 | ⚠️ **部分**:F2 腿 t=+2.60(df=8)过单侧 p=0.05 临界 1.860,日聚类 t=+2.67;但 **n=9**,按 MDE 强制标注规范,本提案只写「有统计支撑的假设」,**不写「已验证」**;减法刀全部**未过**误杀率门 ⇒ 不声明任何显著性 |
| ⑧ 预注册判据 + 回滚 + 复现命令 | ✅ §7 判据(见数前写死)/ §5.6 回滚 / §4 E 表命令 |

**凑不齐的部分只有 ⑦ 的 n**:按 §5.4 结论,前向攒样不可达(需 ≈224 交易日),故 F2 改为**离线回放扩样**(用户 8/14 偏好:能回测就直接回测裁决),不默认前向攒样。

---

## 7. 预注册判据(见数前写死;F1 不改既有判据,F2 为新判据)

### F1(追加腿)—— 沿用 `fee_gate_shadow` 既有判据,只加读数维度

> 既有判据逐字(`shadow_registry.py:1975-1978`):「n≥30 (distinct trade_id) 后机械产出各候选门 blocked/kept 净期望、胜率、费用 bps 与 n-loss;10/11 结合影子读数与双窗口回放终裁真门槛。样本不足或读数反向时禁止提议转正。」

追加腿**同判据**,不另立门槛。10/11 终裁时若 `price_lt_26` / `combo_price_26_notional_4200` 的 blocked 组净期望 ≥0 或 n-loss > 40%,则**该腿不予转正**(与本轮 E5/E7 结论一致)。

### F2(加法腿)—— 新判据,逐字预注册

```
毕业判据 (全部满足才提请用户裁决转正, 达线不自动生效):
  G1  n >= 30 (distinct trade_id, 回放口径: 按 (symbol, 入场日) 去重)
  G2  笔均净 (pnl_net, 费用后) > 0
  G3  日聚类 t >= 1.86 (单侧 p=0.05; 按入场日聚合净额, 不用笔级 t 防同日多笔伪独立)
  G4  死单率 (COALESCE(mfe_pct,0) < 0.3) <= 15%   [基线 SHORT 侧 = 28.3%]
  G5  剔最赚3票后笔均净仍 > 0                     [防单笔主导]
  G6  双窗 (IS/OOS 按回放窗口对半) 同号为正
反例判据 (任一出现 => 关闭该轴, 不再攒样):
  X1  n >= 30 且 G2 或 G3 为负 => 关闭 (不写"证据不足", 写"已否证")
  X2  回放宇宙无法复现历史 9 笔 (口径不匹配) => 关闭并登记口径缺陷
  X3  与 gap_chase_gate / extreme_gap_ban_shadow 出现人群重叠 > 50% => 不得独立转正,
      归属交用户裁 (RFC-109⑥ 同轴纪律)
样本量与时间线 (MDE 强制标注):
  在线前向: 出现率 0.134 笔/交易日 => n=30 需 ~224 交易日 ≈ 10.7 个月 ⇒ **不可达, 不开在线影子**
  离线回放: 窗口可扩至 2026-01 起 (受 raw ATR 数据可得性限制, 见 F4);
            若扩窗后 n 仍 <30 => 按 X1 之外的"样本不足"处理: 只写证据不足, 不写负期望
复核日: 与 fee_gate_shadow 10/11 终裁同日仲裁 (一次裁完)
```

### F4(遥测)—— 无判据(零行为变更),验收 = 字段非空率

```
验收: 补字段上线后 5 个交易日内,
  V1  新增 low_atr skip 行的 metadata_json.intent_direction 非空率 = 100%
  V2  新增指令行 raw_atr_pct 非空率 >= 95% (缺 ATR 数据源时允许 fail-open 留空)
  V3  raw_atr_pct 与 atr_pct 不一致率复测 (基线 = 18/18 = 100%, E11) —
      预期仍高 (两者本就不同物), 该读数用于**确认口径声明**, 不用于调阈值
```

---

## 8. 实施步骤(每步独立提交、独立回滚)

| 阶段 | 动作 | 性质 | 验证命令 → 期望 | 回滚 |
|---|---|---|---|---|
| **0** | 本 DEB 入库 + README 索引加行 + 12 个回放脚本入库 | 纯文档/只读 | `git show --stat HEAD` → 只含本提案文件 + 索引 + `scripts/deb20261002_*.py` | `git revert` |
| **1** | F4-a:`low_atr` skip 行落 `intent_direction` + `atr_pct`/`_atr_basis` 落列 | **零行为变更**(只补遥测) | 新 skip 行 SQL 查 `json_extract(metadata_json,'$.intent_direction')` 非空 | 删写入行 |
| **2** | F4-b:指令生成落 `raw_atr_pct`(原始口径、比例单位) | **零行为变更** | V2 非空率 ≥95% | 删写入行 |
| **3** | F1:`CANDIDATE_GATES` 追加 2 腿 + report 缺键 fail-open + 契约测试 | record-only | `wc -l data/fee_gate_shadow.jsonl` 增长;新行含 `price_lt_26` 键;`venv/bin/python -m pytest tests/ -k fee_gate` 绿 | 删两条腿(§5.6) |
| **4** | F2:离线回放器 `scripts/short_lowvol_replay.py`(扩窗 + 按 G1–G6 机械出裁决) | 只读,不挂 cron | 跑出 n 与 G1–G6 实测值,写入 §12 | 删脚本 |
| **5** | 10/11 复核日:F1 终裁 + F2 回放裁决一并提请用户 | — | — | — |

**阶段 0 一律零副作用**(skill 铁律);阶段 1–2 是纯遥测(值不参与打分/门控,按「标注层修法可随批落地、值参与门控须单独裁」的分级,归标注层);阶段 3 是 record-only;**没有任何阶段改现役门阈值或下单行为**。

**重启需求**:阶段 1–3 改 `app/playbook/pipeline.py` / `scripts/fee_gate_shadow.py`。按 MEMORY 纪律,`playbook.*` 变更 HotReloader 穿不透 ⇒ **需 `systemctl restart ftseek-us`**;重启时点由用户裁(前置 = 核未平仓 + 改动归属)。

---

## 9. 复查清单 C1–C12(给第二 agent;每条给反例判据)

**复查约束(逐字)**:只读 —— 禁改文件、禁 `git` 写操作、禁重启服务。每条必须给**原始命令 + 原始输出**。不许软化,也不许为凑数编问题。工具调用预算 ≤25 次;**禁止批量拉多日历史行情**(本提案全部数字来自本地 DB,无需行情)。时间不够也必须按 `C编号 | 判定(成立/不成立/无法判定) | 原始输出摘录 | 理由` 输出 schema,不许留空。

| 编号 | 声称 | 复查命令 | **反例判据**(什么输出说明我错) |
|---|---|---|---|
| **C1** | 死单率不随标的范围动(−1.2pp) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_final_combo.py` | 任一范围组合把死单率降到 ≤20%(即 Δ≥6.8pp)⇒ 我的「选股轴无效」结论不成立 |
| **C2** | `metadata.atr` 与 `atr_pct` 不是同一物 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_atr_caliber_check.py` | 出现 `md<3% 中 inst 也 <0.03 (门该拦): k/18` 且 k>0 ⇒ 两者部分同值,E11 的 18/18 结论要收窄 |
| **C3** | `low_atr_gate` 对低波空单结构上不生效 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_short_atr_bands.py`(看「门上线后 19 笔」) | 门上线后 SHORT×原始ATR<3% 成交笔数 = 0 ⇒ 该腿已被门杀死,是样本饿死不是活腿 |
| **C4** | 四刀被砍人群误杀率全 >40% | `PYTHONPATH=. venv/bin/python scripts/deb20261002_cut_tail.py` | 任一刀误杀率 ≤40% **且** 剔最亏3票后仍为负 ⇒ 该刀其实够格落地,我撤回错了 |
| **C5** | 现役闸(K1c)是唯一过误杀率门的 | 同上,`K1c` 行 | K1c 误杀率 >40% ⇒ 「现役闸已够好」不成立,需重议 |
| **C6** | K1 全(价≥26∧名义≥4200)双窗反号 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_k1_components.py` | `K1 全` 行 ISΔ均 与 OOSΔ均 同号 ⇒ E7 的反号结论错,该刀可重新考虑 |
| **C7** | SHORT 低波平开腿 t=+2.60 且死单率 0% | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py`+ `PYTHONPATH=. venv/bin/python scripts/deb20261002_universe_replay.py` | t<1.860 或死单率 >15% ⇒ F2 的统计支撑不成立,应降为「假设待攒样」 |
| **C8** | 该腿不与 gap 轴影子人群重叠 >50% | 读 `shadow_registry.py` 中 `gap_chase_gate`(ag≥5%)与 `extreme_gap_ban_shadow`(ag≤−3/≥+4)人群定义,与本腿 `\|gap\|<1%` 对比 | 定义上存在交集 ⇒ X3 触发,F2 不得独立立项 |
| **C9** | UE·LONG 刀无 P3 数据支撑 | `PYTHONPATH=. venv/bin/python scripts/deb20261002_ue_long_period_split.py` | `P3 >=9/29 LONG n>0` ⇒ 有当期数据,撤回理由之一失效(需重议) |
| **C10** | CTNT/GIPR 缺口当天已修,非活缺口 | `git log -S "EXT_ENTRY_MIN_PRICE" --format="%h %ad" --date=iso -- scripts/extended_hours_entry_executor.py` + 读 `:879-886` `_price_in_range()` | 闸上线时间晚于 `2026-09-17T04:06-04:00`(ET),或 `_price_in_range` 未被真实调用链消费 ⇒ 活缺口成立,须另立 P0 |
| **C11** | `fee_gate_shadow` 活着且已 READY | `wc -l data/fee_gate_shadow.jsonl`;`crontab -l \| grep -i fee_gate` | 行数 <30 或无 cron 行 ⇒ F1「复用既有影子」前提不成立,须另议载体 |
| **C12** | 死单率随入场时刻单调(择时轴) | `PYTHONPATH=. venv/bin/python scripts/deb20261002_power.py` | 时刻梯度非单调,或 11/12 时段样本合并后死率 ≤25% ⇒ 「择时轴」归因不成立(n=20 的时段本就薄,这条最该被挑战) |

### 请复查者重点挑战这三点(不点名会退化成确认作者叙事)

1. **C12 的择时归因**:11时段 n=20、12时段 n=15,梯度可能是小样本噪声。请给出你认为正确的「入场即错」归因轴(或证明它不可归因)。
2. **C4/C5 的误杀率门口径**:我用的是 `extreme_gap_ban_shadow` 的「被拦笔盈利占比 ≤40%」。请挑战这个门对**费用类**刀是否适用 —— E24 显示被砍人群毛额为正(纯费用问题),对一笔「毛赚 $3 但费 $8」的单,把它算进「误杀」是否合理?若不合理,K1a 的 51% 该怎么重算?(这是我最没把握的一处口径。)
3. **F2 的替代路径**:离线回放器扩窗受 `raw_atr_pct` 历史可得性限制(F4 字段是新增的,历史行没有)。请判断:①能否从既有 K 线重算历史原始 ATR(而非依赖新字段)②若不能,这条 n=9 的腿是否应当直接放弃而不是攒样。

---

## 10. 风险与取舍

| # | 风险 | 量级 | 缓解 |
|---|---|---|---|
| R1 | F1 追加腿使 jsonl 新旧行 schema 不一致,report 抛 KeyError | 中(会静默停掉 10/11 终裁的读数面) | 阶段 3 同批加缺键 fail-open + 契约测试;先跑一次 report 验证 |
| R2 | F2 回放宇宙 ≠ 实盘候选池 ⇒ 读数不可外推 | 高(整条腿作废) | X2 反例判据已预注册;回放必须复用生产候选池入口,不自建股票池 |
| R3 | `raw_atr_pct` 历史不可得 ⇒ F2 扩窗失败,n 停在 9 | 高 | 见 §9 挑战点 3;若失败按「样本不足」处理,只写证据不足 |
| R4 | 撤回四刀后,用户期望的「降亏损」没有落地动作 | 中 | F1 让既有影子在 10/11 给出真门槛;本提案的价值是**防止落地四把误杀率超门的刀** |
| R5 | 死单率 −1.2pp 被读成「范围过滤有效」 | 中 | §1 头条已把「降死单率」与「降亏损」显式分开;E2 同时给两个数 |
| R6 | 样本含 POSITION_SYNC 5 行 / MAIN_PIPELINE 3 行(<2%)未剔 | 低 | 已在 integrity 块声明;剔除后重算不改变任何结论方向(可复查) |
| R7 | 7 月坏 regime 占样本 40%,任何读数都被它拉动 | 中 | 双窗 + 逐月已分列;IS 窗反号(E7)正是这个原因,已如实报 |
| R8 | F4 补字段被后来人当成「门已生效」 | 低 | 字段名带 `raw_` 前缀 + 注释写明「与 `atr_pct` 并列不替换、不参与门控」 |

### 回滚(两层:法子 + 触发条件)

**法子**:F1 = 从 `CANDIDATE_GATES` 删两条 + `git revert`;F2 = 删脚本与报告(零生产接线);F4 = 删字段写入行(旧行留空 fail-open)。全部无需 DB 迁移。

**触发条件(出现任一即回滚,不留给下次讨论)**:
- `fee_gate_shadow.jsonl` 行数停止增长 ≥3 个 cron 周期(采集器被新腿拖挂)
- `fee_gate_shadow_report.py` 出现任何 KeyError/异常退出
- F4 字段非空率 <50%(写入点接错)
- 出现任何由 F1–F4 而非既有事实闸导致的下单/拒单(本提案全链 record-only,出现即为严重接线错误)
- F2 回放器被误挂 cron 并产生推送噪声

### 待决问题(frontier)

| # | 需要谁裁什么 | 推荐 | 一行理由 |
|---|---|---|---|
| **F1** | 用户:是否批准给 `fee_gate_shadow` 追加 `price_lt_26` / `combo_price_26_notional_4200` 两条候选腿 | **批准** | 零新台账、零行为变更、10/11 既有终裁一并出读数;不追加则该终裁无法回答「真门槛是 $5 还是 $26」 |
| **F2-a** | 用户:F2 用**离线回放器**还是**在线影子** | **离线回放器** | 在线出现率 0.134 笔/日 ⇒ n=30 需 ~224 交易日,不可达;用户 8/14 偏好「能回测就直接回测」 |
| **F2-b** | 用户:若历史原始 ATR 不可重算(R3),这条 n=9 的腿是放弃还是继续 | **先花一次成本验证可重算性,再裁** | t=+2.60 + 死单率 0% + 逐月全正,是唯一有统计支撑的加法腿;但 n=9 不足以直接上产 |
| **F4** | 用户:是否批准两处遥测补字段 | **批准** | 零行为变更;不补则 `low_atr_gate` 方向语义永远不可判定(现状 8/8 行方向丢失) |
| **F5** | 用户:「入场即错」目标是否移交给既有 `entry_delay_confirm`(D2')影子,不新开轴 | **移交** | E3/E4 证明是择时轴;D2' 的立项动机逐字就是「SL_INITIAL 出血族尸检 19笔16笔MFE=0/11笔≤25min」= 同一病灶 |
| **F6** | 用户:`low_atr_gate` 是否加 LONG-only 方向条件 | **暂缓,等 F4 数据** | 门的立项理由(「低波票做日内动量结构性失败」)是 LONG 逻辑,而 SHORT 侧低波是最强腿(E10);但历史 8 行 skip 方向不可判定(E14)⇒ 现在改等于盲改 |

---

## 11. 自曝 / 更正记录

本轮我犯的错误,逐条留痕(防下一个 agent 重走):

| # | 原说法 / 原做法 | 实测更正 | 处置 |
|---|---|---|---|
| **①** | 第九轮把 `metadata.atr < 5` 当「低波动」筛出 SHORT 最强腿 n=49 | `metadata.atr` 是**百分数**口径(中位 8.40 = 8.40%),且 `atr=0` 是**缺值哨兵**(43 行,全在 7 月,净 +323.6)⇒ 那 49 笔里混了 15 笔零值行,读数作废 | 第十一/十二轮按百分数口径 + 剔零值重算,得 E10/E12 |
| **②** | 第十一轮用 `< 0.03` 筛 `metadata.atr`,以为在复刻 `low_atr_gate` | 筛出的恰是那批 `atr=0.0000` 缺值行(15 笔全 7 月),与门毫无关系 | 第十二轮起两口径分列,并用 E11 实锤两者不同物 |
| **③** | 第十八轮用 `rob()` 对**保留侧**算剔尾,把「剔最赚3票翻负」当成组合的缺陷标记 | 对一把刀的正确检验是**被砍人群**剔最亏3票后是否仍负;保留侧靠头部票赚钱是正常现象 | 新增 `scripts/deb20261002_cut_tail.py` 按正确口径重算 ⇒ 得出 E5(四刀全不过门),**推翻了我上一轮的落地建议** |
| **④** | 一度声称「`low_atr_gate` 加 LONG-only 是零行为变更,因为历史 8 行 skip 无 SHORT」 | 那 8 行 `direction` 列全是 `'SKIP'`(被 skip 覆写)、`atr_pct` 列全 `NULL`、`raw_instruction_json` 无 direction 键 ⇒ **方向信息不存在**,「无 SHORT」是把缺失读成了否证 | E14 登记为「不可判定」;F6 从「可落地」降为「暂缓,等 F4 遥测」 |
| **⑤** | 上一轮聊天里推荐的四刀(K1 全 / K2 / K3 / K4)+「砍 UE·LONG」 | K1 全双窗反号(E7)且误杀率 48%;K2 n=7 剔3亏翻正;K3 误杀率 67%;K4 误杀率 53% 且与已判 FAIL_ON_FALSE_KILL 的 `extreme_gap_ban_shadow` 同轴;UE·LONG 无 P3 数据(E15)⇒ **五条全部撤回** | §2.4 撤回表 + §10 R4;聊天里那句「P0 可直接上」在本提案中**不成立** |
| **⑥** | 一度把 CTNT $0.07 / GIPR $0.43 成交当成「EXT 通道绕过价格闸的活缺口」 | 闸 `ce54f64eb` 于 2026-09-17 16:59 CST 上线,成交在当日 04:06 ET(=16:06 CST)**早 53 分钟**;代码注释逐字点名这两笔为事故依据 ⇒ 当天已修 | §2.5 显式登记;C10 给复查者反例判据 |
| **⑦** | 上一轮聊天里说「SHORT×ATR<5% n=49」等具体数字 | 该数字来自被 ① 污染的筛选,已在 E10 用正确口径替换为 `SHORT×ATR<3% n=21 净+677.7` | 本提案不引用任何第九/十轮的 ATR 分档数字 |
| **⑧** | 探针列名两次猜错(`notional` / `raw_instruction`)导致脚本首跑失败 | 先 `PRAGMA table_info` 再取数(MEMORY 已有此纪律,本轮又犯) | 已修;入库脚本均为修好后的版本 |

**本提案不引用的旧读数**:第九/十/十一轮的所有 ATR 分档数字(口径污染);第十八轮保留侧剔尾标记(口径错);上一轮聊天中的四刀落地建议与「P0 可直接上」表述。

---

## 12. 讨论记录

### R1 — 2026-10-02(作者:Hermes Agent)

用户问「选什么标的范围和策略方向能降低入场即错」。经 18 轮回放(9 个脚本入库)+ 6 轮自我更正(§11)得出:

1. **问题路径本身需要修正**:标的范围过滤降的是**费用**(费bps 12.1→7.6、净额 −1494→+734),不是**入场即错率**(26.8%→25.6%)。「入场即错」随入场时刻呈梯度(E3)、在各标的切片间无差异(E4)⇒ 是择时轴。
2. **减法轴全线不过本仓预注册门**:四刀被砍人群误杀率 43–67% > 40%(E5),唯一过门的是现役闸 37%(E6)。上一轮聊天里的落地建议**全部撤回**(§11 ⑤)。
3. **唯一活口是加法**:SHORT × 原始ATR<3% × |gap|<1%,n=9、t=+2.60、死单率 0%、逐月全正(E8/E9),且 SHORT 侧原始 ATR 轴严格单调(E10)⇒ 符合「真驱动」判据。但供给率 0.134 笔/日 ⇒ 在线攒样不可达,须离线回放扩样(F2-a)。
4. **两个附带发现**:`low_atr_gate` 因止损口径三层底限,对低波空单结构上不生效(E11/E13),其立项理由只覆盖 LONG;该门 skip 行方向信息在 DB 中完全丢失(E14)⇒ 需 F4 遥测才能判 F6。

**未落地任何交易行为变更。** 待用户裁 F1/F2-a/F2-b/F4/F5/F6。

---

## 附录:本提案文件清单

| 文件 | 作用 |
|---|---|
| `docs/debates/DEB-20261002-universe-and-direction-entry-error.md` | 本文档 |
| `scripts/deb20261002_universe_replay.py` | 主回放:ATR/gap 分档 × 方向、四刀叠加、最终组合、最优子腿(E2/E9/E10) |
| `PYTHONPATH=. venv/bin/python scripts/deb20261002_dead_rate_scan.py` | 死单率全轴扫描 + 死单毛/净拆解(E1/E4) |
| `PYTHONPATH=. venv/bin/python scripts/deb20261002_cut_tail.py` | **被砍人群**尾部主导 + 误杀率检验(E5/E6) |
| `scripts/deb20261002_k1_components.py` | K1 六变体双窗对比 + low_atr skip 行方向 + EXT 价格闸时间线(E7/E14/E24/E26) |
| `scripts/deb20261002_atr_caliber_check.py` | 两个 ATR 口径逐笔对照(E11) |
| `scripts/deb20261002_short_atr_bands.py` | SHORT 低波腿在门上线前后的存活核验(E13) |
| `scripts/deb20261002_ue_long_period_split.py` | UE·LONG 按三配置期切分(E15/E16) |
| `scripts/deb20261002_notional_band.py` | 名义 $2500–4200 带来源拆解(E17/E18) |
| `scripts/deb20261002_k1_current_era.py` | 当前时代(9/1 起)费用线影响面(E21/E22/E23) |
| `scripts/deb20261002_final_combo.py` | K1+K2+K3 最终组合 + 两个落地前提核验(E2/E18) |
| `scripts/deb20261002_power.py` | F2 腿功效/MDE + 死单率切片 + 时刻分布 + 供给面(E3/E4/E8/E25) |
| `scripts/deb20261002_atr_zero_diagnosis.py` | ATR 口径污染诊断:NULL/零值哨兵计数、百分数口径分档×方向双窗(E10b/E12) |
