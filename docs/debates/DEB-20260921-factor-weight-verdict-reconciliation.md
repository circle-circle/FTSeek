# DEB-20260921: 因子权重三方对账 — IC 通道 × 台账 verdict × 现金口径

> **状态**: 对账已实测 (本文件) · 只读告警已上线 (job `241c896213eb`) · **权重处置待裁 (本批一分未改)**
> **类型**: L1 (新增只读探测器 + 载体 + 台账登记 + 新环路登记; 零交易行为变更)
> **触发**: 2026-09-21 用户对"因子权重对账清单"回「按建议执行」(= 落只读告警 + 落本 DEB; 权重项明确留待拍板)
> **同批文件**: `docs/decisions/RFC-093-factor-weight-reconcile-loop.md` (新环路登记) ·
> `scripts/factor_weight_reconcile.py` · `ops/hermes/factor_weight_reconcile_carrier.py` ·
> `tests/test_factor_weight_reconcile.py` · `docs/CONTROL-LOOP-LEDGER.md §2.5`
> **相关既有裁决**: RFC-060 §⑤ (双计写者停用, 2026-07-31) · DEB-20260724 §7.4/§八 (IC vs avg_alpha 三分类) ·
> `docs/ADJUDICATED-CRITERIA.md` (判据字典)
> **修订记录**: 本文件初稿写完即被自己的复查推翻一条核心结论（台账 verdict 的"负证据"由**分母口径**造出）——
> 已就地改写 §0/§3/§4.1/§5/§8，不留旧版以免下游继续引用错排序。

---

## 0. 一页速览

1. screener 给每个**触发**因子的实际加分 (本文件记 `eff`) 由三处相加，代码实测（非估算）：

   `eff = base(screening_factors[*].weight) + ic_adj(factor_ic_latest.json) + Σhist(weight_adjustments 全量)`

2. **最重的一条（核心发现）**：台账 `verdict` 的胜率分母 = **全部已结算指令行**，其中 **44% 是"非结果行"**
   （`missed` 未成交/错过 380 行、`already_held` 9、`void` 9、`closed` 1；`pnl` 为 NULL/0，全窗口 900 行里 399 行）。
   ⇒ 一批因子被标 `negative` 只是"**指令没兑现的比例高**"，不是"亏钱"。剔掉非结果行后：
   `news_positive` 33.4%→**56.2%**、`above_ma20` 35.2%→**59.8%**、`earnings_beat_streak` 35.9%→**59.8%**、
   `insider_net_buy` 35.2%→**57.3%**、`pe_below_industry` 34.2%→**53.7%**（共 8 项落在 G 类）。
   这同时解释了上一轮观察到的怪象——"台账 WR 高度趋同 0.33–0.36、看起来没有区分度"：那个 33% 就是 `win/全部行`。
3. **同一个因子的三把尺子不同轴**且无人对账：信号级 IC（排序力）/ 成交级毛胜率 / 指令级净胜率。
   `news_positive` 三处 n = **665 / 297 / 476**，WR = **45.1% / 64.3% / 33.4%**。
4. 现金口径与 verdict 在 3 项上反向（`insider_net_buy` 净 **+$250**/PF 1.12、`above_ma20` **+$140**/1.06、
   `rsi_overbought` +$70/2.44）⇒ **单凭 verdict 砍权重会砍掉净额为正的因子**。
5. `Σhist` 通道有**双计存量**（7/8 批 10 条 delta 中 9 条可从 git 快照实证 `Δbase == Δdelta`）；
   写者已于 2026-07-31 停用（RFC-060 §⑤）⇒ 只增存量、不再新增。
6. 现状实测 **七类错配**（A5 / B5 / C11 / D1 / E1 / F3 / G8，跨类有重叠）。本批**未改任何权重/配置/DB**；
   待裁项见 §5（每条含建议 / 保守替代 / "不做会怎样"）。

---

## 1. 生效口径（代码实测）

| 项 | 实现位置 | 读法 |
|----|----------|------|
| `base` / `enabled` | `config/playbook_screening.yaml` → `screening_factors` | 因子级基础分与开关 |
| `ic_adj` | `app/playbook/screener.py::_load_factor_ic_adjustments` (L200-243)，调用点 L265 | `|IC|>0.05 且 n≥30` → ±1；**无条件生效**，不受 `ic_feedback_to_screener` 开关控制（该 flag 只门控 strategy 级 `_get_strategy_ic_bonus`，L365-373） |
| `Σhist` | `screener.py::_calculate_score` L352-359 | `weight_adjustments` **全量逐条** `score += delta`（只在因子触发时加） |
| 台账 verdict | `app/playbook/factor_analyzer.py::classify_factor` (L118-135) | 贝叶斯下界>55% 且 PF>1.2 → positive；上界<45% 或 PF<0.8 → negative；否则 observing |
| 台账样本 | `db.py::get_instructions_with_outcome` (L971-982) | `outcome IS NOT NULL AND outcome!='holding' AND direction!='SKIP'`，按 date DESC **最近 900 行**（`days_back*10`：名为 90 天、实为行数上限）⇒ **`missed`/`already_held`/`void`/`closed` 全进分母** |
| 台账胜率 | `factor_analyzer.compute_trade_stats` (L128-158) | `WR = wins/total`（`total` 含非结果行）；`wins` = outcome=='win' 或 pnl>0 |
| 台账金额口径 | `pipeline.py` L6773 附近 + `shadow_tracker` | 2026-08-20 用户裁"切"起 `instruction.pnl` = **净口径**（费用后）|

**复算命令**（跑一遍即可独立复现本文件所有数字）：

```bash
cd /opt/ftseek/FTSeek-US
venv/bin/python scripts/factor_weight_reconcile.py --force          # ① eff 分解 + 七类错配全表 (含剔零WR)
sed -n '200,243p;352,359p' app/playbook/screener.py                 # ② ic_adj 判据 + Σhist 全量相加
sed -n '118,136p;128,158p' app/playbook/factor_analyzer.py          # ③ verdict 判据 + WR 分母
sed -n '971,982p' app/playbook/db.py                                # ④ 台账窗口 (非结果行进分母的入口)
sqlite3 -readonly data/ftseek_us.db \
  "select factor_key,total_trades,wins,losses,win_rate,total_pnl,profit_factor,verdict \
   from playbook_factor_stats where analysis_level='single' order by total_trades desc"   # ⑤ 台账读数
```

---

## 2. 对账全表（2026-09-21 实测；台账 = sim 库 `data/ftseek_us.db`）

IC 快照 `_meta.generated_at = 2026-09-18T21:40Z`；台账 `last_updated = 2026-09-18T20:01Z`（9/19 04:01 CST 刷新）。

| 因子 | en | base | ic_adj | Σhist | **eff** | 台账n | WR | **剔零WR** | 净额$ | PF | verdict |
|------|----|------|--------|-------|---------|-------|-----|-----------|-------|----|---------|
| insider_cluster_buy | ✔ | +7 | 0 | +2 | **+9** | **1** | 100% | 100% | +7 | 999* | observing |
| macd_golden_cross | ✔ | +3 | 0 | +1 | **+4** | 38 | 31.6% | 57.1% | −195 | 0.67 | negative |
| above_ma20 ⚙ | ✔ | +1 | 0 | +1 | **+2** | 182 | 35.2% | **59.8%** | **+140** | 1.06 | negative |
| earnings_beat_streak | ✔ | +1 | +1 | 0 | **+2** | 273 | 35.9% | **59.8%** | −207 | 0.94 | negative |
| insider_net_buy | ✔ | +2 | +1 | −1 | **+2** | 179 | 35.2% | **57.3%** | **+250** | 1.12 | negative |
| news_positive | ✔ | +1 | +1 | −1 | **+1** | 476 | 33.4% | **56.2%** | −172 | 0.97 | negative |
| earnings_imminent | ✘ | −3 | 0 | 0 | −3 | — | — | — | — | — | (无台账) |
| rsi_overbought | ✔ | −4 | +1 | −1 | **−4** | 19 | 21.1% | **66.7%** | +70 | 2.44 | negative |
| volume_expansion | ✔ | −2 | +1 | −4 | **−5** | 32 | 34.4% | **64.7%** | **+313** | 2.26 | observing |
| rsi_oversold | ✘ | −3 | 0 | −3 | −6 | 4 | 0% | 0% | −31 | 0.00 | observing |
| news_negative | ✘ | −4 | 0 | −3 | −7 | 18 | 27.8% | 50.0% | −215 | 0.47 | negative |
| pe_below_industry | ✘ | −3 | +1 | −5 | −7 | 275 | 34.2% | **53.7%** | −472 | 0.87 | negative |
| insider_net_sell | ✘ | −5 | −1 | −3 | **−9** | 34 | 20.6% | 41.2% | −208 | 0.38 | negative |

`剔零WR` = `wins/(wins+losses)`；`WR` 列 = `wins/全部分行`（含非结果行）—— 两列之差就是分母口径的量级。
`⚙` = 有共振门控（`requires_co_factor`：无 `volume_expansion`/`insider_cluster_buy` 共振时该项贡献回退 0，screener L334-350）。
`*` PF 999 是 `compute_trade_stats` 对 `inf` 的占位（分母 0 亏损），不是真实 PF。

**口径注记**：本表 `eff` 是"**该因子触发时的静态贡献**"，不是某日实际打分之和；`insider_*` 三因子在 screener 里互斥（L272-304），
`above_ma20` 另有共振回退 ⇒ 本表不能按行相加当"当日总分"。

---

## 3. 七类错配（本批探测器逐类落的判据）

| # | 类 | 判据 | 实测项 |
|---|----|------|--------|
| A | 正权重买负证据 | `enabled 且 eff>0 且 verdict=negative 且 台账n≥30` | 5：`macd_golden_cross(+4,n38)`、`above_ma20(+2,n182)`、`earnings_beat_streak(+2,n273)`、`insider_net_buy(+2,n179)`、`news_positive(+1,n476)` |
| B | IC 通道与台账反号 | `ic_adj≠0 且 符号与 verdict 相反` | 5（全是 `ic_adj=+1` vs `verdict=negative`）：`news_positive`、`earnings_beat_streak`、`pe_below_industry`、`insider_net_buy`、`rsi_overbought` |
| C | Σhist 双计存量 | `weight_adjustments` 非空 | 11 个因子（见 §4.4） |
| D | 小样本高权重 | `eff>0 且 台账n<30` | 1：`insider_cluster_buy` eff **+9**（全场权重之王）/台账 n=**1**/IC 文件 n=4 |
| E | 罚错了 | `eff<0 且 verdict≠negative 且 现金>0` | 1：`volume_expansion` eff **−5** 但净 **+$313**、PF 2.26、剔零WR 64.7%、n=32 |
| F | 尺子冲突 | `verdict=negative 而净额>0`（或反向） | 3：`insider_net_buy`(+$250)、`above_ma20`(+$140)、`rsi_overbought`(+$70, n=19) |
| G | 台账分母口径 | `verdict=negative 但 剔零WR≥50%` | 8：`rsi_overbought 21.1→66.7%`、`macd_golden_cross 31.6→57.1%`、`news_negative 27.8→50.0%`、`above_ma20 35.2→59.8%`、`news_positive 33.4→56.2%`、`earnings_beat_streak 35.9→59.8%`、`insider_net_buy 35.2→57.3%`、`pe_below_industry 34.2→53.7%` |

**B 类的关键细化（把现金口径拉进来后）**：5 项里 **3 项 IC 确实错**（`news_positive` 净−172、`earnings_beat_streak` 净−207、
`pe_below_industry` 净−472：IC 说好 / verdict 说坏 / 现金也说坏），**2 项 IC 与现金同向**（`insider_net_buy` +250、`rsi_overbought` +70：
冲突只在 IC vs **胜率**口径）⇒ 对着 verdict 砍 IC 通道会连带砍错这 2 项。

**A 类的关键细化（被 G 削弱）**：5 项里 **4 项**（`news_positive`/`earnings_beat_streak`/`above_ma20`/`insider_net_buy`）
的 `negative` 落在 G 类（分母口径驱动，剔零后 56–60%）；**只有 `macd_golden_cross` 不靠分母** —— 它的 negative 来自另一条腿
`PF 0.67 < 0.8`（金额口径：净 −195 / 38 笔）。⇒ "正权重买负证据"这个说法必须按尺子拆开讲，见 §5。

---

## 4. 根因四层

### 4.1 台账 verdict 的分母含 44% 非结果行（本轮新发现，最重）

`get_instructions_with_outcome`（db.py L971-982）的过滤是 `outcome IS NOT NULL AND outcome != 'holding' AND direction != 'SKIP'`
—— 它**放行 `missed`（未成交/错过）、`already_held`（该标的已持仓）、`void`（幽灵仓清理）**；这些行 `pnl` 为 NULL/0，
`compute_trade_stats` 里 `total` 照加、`wins`/`losses` 都不加 ⇒ `WR = wins/total` 被非结果行系统性稀释。

**实测（900 行窗口全量分解，逐行数过）**：

| outcome | 行数 | 占比 | 进胜负统计? |
|---------|------|------|-------------|
| win | 283 | 31.4% | ✅ wins |
| loss | 218 | 24.2% | ✅ losses |
| **missed** | **380** | **42.2%** | ❌ 只进分母 |
| already_held | 9 | 1.0% | ❌ 只进分母 |
| void | 9 | 1.0% | ❌ 只进分母（pnl=0） |
| closed | 1 | 0.1% | ❌ 只进分母 |

**为什么这条最贵**：`classify_factor` 的 negative 分支是"贝叶斯**上界**<45% 或 PF<0.8"，分母被稀释后上界必然被压低
（把 40% 非结果行塞进分母 ≈ 人为把胜率往 0.33 拉）⇒ 8 个因子落 G 类。上一轮我把这批 negative 当"负证据"
并据此排出"最该先裁的因子"，是**在错口径上做排序**（见 §8 自曝第 1 条）。

```bash
# 复现 §4.1（只读，不改工作区）
cd /opt/ftseek/FTSeek-US && venv/bin/python - <<'PY'
import sqlite3, json
from collections import defaultdict, Counter
c = sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
rows = c.execute("""SELECT conditions_json, outcome, pnl FROM playbook_instructions
                    WHERE outcome IS NOT NULL AND outcome != 'holding' AND direction != 'SKIP'
                    ORDER BY date DESC LIMIT 900""").fetchall()
print('窗口行数', len(rows), dict(Counter(oc for _, oc, _ in rows)))
fac = defaultdict(list)
for cj, oc, pnl in rows:
    for x in (json.loads(cj or '[]') or []):
        fac[x].append((oc, pnl))
for k in ('news_positive', 'macd_golden_cross', 'above_ma20'):
    v = fac[k]; w = sum(1 for oc, p in v if oc == 'win' or (p or 0) > 0)
    l = sum(1 for oc, p in v if oc == 'loss' or (p or 0) < 0)
    print(f"{k:20} n={len(v)} win={w} loss={l} 现口径WR={w/len(v):.3f} 剔零WR={w/(w+l):.3f}")
PY
```

### 4.2 三条反馈回路的**度量轴**不同，谁也没错、但互相抵消/放大

| 回路 | 度量 | `news_positive` 实测 |
|------|------|----------------------|
| 信号级 IC（`factor_ic_latest.json` 的 `ic`/`n`） | 因子 → **前瞻收益**的横截面 rank IC（相对排序力，不含绝对盈亏） | IC **+0.083**、n=665、wr 45.1%、`avg_alpha` **−0.0027** |
| 成交级（同文件 `trade_n`/`trade_wr`） | 因子 → **成交笔**毛盈亏（`scripts/factor_ic_backfill.py` L272-303：`trade_records`，`entry_time≥2026-06-01`，`pnl_pct>0` 计胜） | n=**297**、WR **64.3%**（毛胜率）；同区块的 `trade_avg_pnl_usd` 却是**净**口径 ⇒ 一块之内毛/净混用 |
| 指令级台账（`playbook_factor_stats`） | 因子 → **指令结局**净盈亏（费用后） | n=**476**、WR **33.4%**（分母含非结果行，见 §4.1）、净 −172 |

三套 n/WR 并存（665/297/476、45.1%/64.3%/33.4%），**无人对账**。64.3% → 33.4% 的 31pp 落差不全是噪声：
一半是"毛 vs 净"（用户 2026-08-20 已定性"毛>0 但≤费用 = 假盈利"，当时实测 125 笔中 23 笔假盈利、毛胜率虚高 18pp），
一半是 §4.1 的分母。`trade_wr` 目测无交易路径消费者（只有归档诊断脚本与记分牌读它）⇒ 这处毛/净混用**当前不进打分**，属读数面待办。

### 4.3 `ic_adj` 无条件生效，且与 flag 无关

`_load_factor_ic_adjustments()` 在 L265 被**无条件**调用；`ic_feedback_to_screener` flag（L365-373）只门控 strategy 级加分。
⇒ "关掉 IC 反馈"这个动作**关不掉 factor 级 ic_adj**，本轮 5 项反号每天在打分里生效（这正是要立告警的原因）。

### 4.4 `Σhist` 双计存量（机制实锤，金额部分不可唯一分解）

**机制**：`app/playbook/weight_adjuster.py::adjust`（L44-104）在**同一调用里**既 `factors[f]["weight"] = current ± 1`
（写回 `screening_factors`）又 `append({"delta": ±1})` 到 `weight_adjustments`；而 screener 读的是 `base + Σhist` ⇒ 一次调整被计两次。

**写者状态**：`weekly_report.py` L68-73 —— 该 `adjust()` 调用已于 **2026-07-31**（commit `679b826b3`, RFC-060 §⑤"双重计权腿切断"）停用
（pipeline 侧接线更早，2026-07-04 已撤）⇒ **不再新增双计**，存量仍在打分里生效。

**存量实证（git 快照 7/3 → 7/12 逐因子对比）**：7/8 批 10 条 delta 中 **9 条** `Δbase == ΔΣhist`
（`insider_net_buy`/`rsi_oversold`/`macd_golden_cross`/`news_positive`/`volume_expansion`/`pe_below_industry`/
`rsi_overbought`/`insider_net_sell`/`news_negative` 各 −1），唯 `above_ma20` 不同步（Δbase −3 vs ΔΣhist −1，含人工改动）
⇒ **这 9 个因子当前 eff 各多算 1 分**（方向 = delta 符号）。

**不可分解的部分必须说清**：6/18–6/29 两批（`cluster_buy` +1+1 / `above_ma20` +1+1 / `macd` +1+1 …）在
**2026-06-27 RFC-062 同批人工改过 base**，git 无法把"写者双写"与"人工改参"分开 ⇒ 这些 delta 是否已含在 base 里**不可裁定**。
**保守读法（本文件采用）**：只承认能实证的那 9 条，其余记为"不可分解"，不做结算、不改数。

```
# 复现 §4.4 实证（只读, 不改工作区）
cd /opt/ftseek/FTSeek-US && venv/bin/python - <<'PY'
import subprocess, yaml
def snap(c):
    y = subprocess.run(["git","show",f"{c}:config/playbook_screening.yaml"],capture_output=True,text=True).stdout
    cfg = yaml.safe_load(y); f = cfg["screening_factors"]; h = {}
    for a in cfg.get("weight_adjustments", []) or []:
        h[a["factor"]] = h.get(a["factor"], 0) + a["delta"]
    return {k: (v.get("weight"), h.get(k, 0)) for k, v in f.items()}
a, b = snap("77852d50f"), snap("8c5048b7a")   # 7/3 与 7/12 快照
for k in a:
    (b0,h0),(b1,h1) = a[k], b[k]
    if (b1-b0, h1-h0) != (0,0):
        print(f"{k:24} Δbase={b1-b0:+d} ΔΣhist={h1-h0:+d} {'同步(双计形态)' if b1-b0==h1-h0 else '不同步'}")
PY
```

---

## 5. 待裁项（建议 / 保守替代 / 不做会怎样）

⚠️ 全部属**策略变更**（L2+，需预注册 + 回放验证），本批**一律未执行**。
下文"建议"是单选项，不是已生效的改动。

### T1 [建议·最高优先] 先把台账 verdict 的口径定谳 —— 定下来之前不要据此改任何权重

- 依据：§4.1（8 个 negative 靠 40% 非结果行撑着）；A 类 5 项里 4 项的"负证据"由此而来。
- 做什么（二选一，推荐①）：
  ① **只加读数**（低风险）：`factor_analyzer` single 层并列输出 `wr_decided = wins/(wins+losses)` 与 `nonoutcome_share`，
     verdict 判据一个字不改 —— 让"负证据"有第二把尺子对照（本批告警已在 G 类做这件事）；
  ② **改判据**（L2+，影响打分）：把 `missed`/`already_held` 从分母剔除后重算 verdict（`void` 建议直接排除，它已不是交易结果）。
- 保守替代：只做①，并把 T2–T5 的权重动作冻结到口径定谳之后。
- 不做会怎样：任何一次"按台账裁因子"都是在对 40% 噪声做排序，且改完无法回退解释（口径变了，历史不可比）。

### T2 [建议] `macd_golden_cross`：eff +4 → 先降回 +1~+2

- 依据：它是**唯一不靠分母口径**的负证据因子 —— `PF 0.67 < 0.8`（金额腿）+ 净 −195 / 38 笔；
  其 +4 = base 3 + Σhist 1（其中 1 分已实证双计）；另 +2 来自 6/28+6/29"OKR 急救"（当时依据 **PF 4.17 / PnL +1121**，与当前读数相反）。
- 保守替代：只摘已实证的双计（+4 → +3），并在 n 到 60 前不再动。
- 不做会怎样：负证据因子拿全场第二高权重（仅次于 n=1 的 `cluster_buy`）。

### T3 [建议] `insider_cluster_buy`：给"低样本高权重"加硬护栏

- 依据：eff **+9**（全场之王）来自 6/29 的"WR 75%、唯一显著正收益因子"，而**当前台账 n=1**（IC 文件 n=4）；
  同表里 `insider_net_sell` 已因小样本被打到 −9 —— 同一张表对"小样本"的处理方向相反。
- 做什么（二选一）：① 权重降至 ≤+4，并把"n≥30 才允许再加分"写进判据；② 保留 +9，但在 screener 触发详情打 `low_sample=True`（**零打分类变更**）。
- 保守替代：②。
- 不做会怎样：权重之王继续建立在 n=1 上；一旦该因子失效，最高权重正好压在错的因子上。

### T4 [建议] `volume_expansion`：eff −5 → 回到 0（不奖不罚）

- 依据：verdict 只是 `observing`，而现金 **+$313 / PF 2.26**、avg **+$9.77/笔**、剔零WR **64.7%** —— 全场最像样的正均值；
  它的 −5 来自 6/28（"平均 −$61, PF 0.34"，样本与窗口早已过期）+ 6/29（−2）+ 7/8（−1）+ 双计。
- 保守替代：只摘双计（−5 → −4）并挂观察名单。
- 不做会怎样：把一个净额为正、PF 2.26 的因子按一年多前的读数持续罚到底。

### T5 [建议] IC 通道的靶子收窄到"三口径同号"的因子（或整体停用 factor 级）

- 做什么（三选一）：① 只把 `news_positive`/`earnings_beat_streak`/`pe_below_industry` 的 `ic_adj` 由 +1 置 0；
  ② 在 `_load_factor_ic_adjustments` 加护栏"台账 verdict=negative 时不给 +1"；③ 停用 factor 级 ic_adj，只留受 flag 控制的 strategy 级。
- 依据：①的 3 项是唯一"IC 说好 / verdict 说坏 / **现金也说坏**"（净 −172/−207/−472）的方向；而
  `insider_net_buy`/`rsi_overbought` 的 IC 与现金同向 ⇒ 不该连带被砍（上一轮把它们也算进"该砍"是错的，见 §8）。
- 注意：这 3 项 verdict 的 negative 同时落在 G 类 ⇒ 落地前请先做 T1①，否则是拿一个口径未定的尺子改权重。
- 保守替代：只做 T1①，ic_adj 暂不动。
- 不做会怎样：3 个净额为负的因子继续每天拿 +1（`news_positive` 台账 n=476，是最大样本）。

### T6 [建议] `Σhist` 通道整体退役（或冻结）

- 做什么：删 `screener._calculate_score` L352-359 那 8 行 + 清 `weight_adjustments`（其 delta 已在 base 里）；
  或保守：保留通道但冻结，并在 yaml 注明"历史条目仅作溯源、不再计分"。
- 依据：§4.4 —— base 与 Σhist 是同一个数被写两处；通道留着就持续制造"哪份是真的"。
- 不做会怎样：11 个因子的 eff 里各含一份不可完全裁定的历史偏差，每次权重讨论都要先做一次考古。

### T7 [建议·低风险] 读数面口径统一（不改打分）

- 做什么：`factor_ic_backfill.py` 成交级 `trade_wr` 改用净口径（`pnl_net_eff > 0`）或改名 `trade_wr_gross`；
  产物 `_meta` 写明"信号级/成交级/指令级三套 n 的轴"。
- 依据：§4.2（同一区块内毛/净混用；`trade_wr` 无交易路径消费者 ⇒ 纯读数面）。
- 不做会怎样：下一个读这张表的人继续把 64.3% 当"这个因子的胜率"。

---

## 6. 本批已落地 / 明确未做

**已落地（零交易行为变更）**

1. `scripts/factor_weight_reconcile.py` — 纯只读对账器（台账 `mode=ro`），七类错配判据写在脚本头，落
   `data/factor_weight_reconcile_latest.json`；静默契约 = 对账面签名无变化 → 零输出。
2. `ops/hermes/factor_weight_reconcile_carrier.py` + 线上副本（sha256 相同）+ Hermes job **`241c896213eb`**
   （`50 6 * * 6`，no_agent，静默类；每轮取证头落 `logs/factor_weight_reconcile.log` ⇒"没推送≠没跑"）。
3. `tests/test_factor_weight_reconcile.py`（6 例）：静默契约 / 签名变化重抬 / 七类判据逐类可检出 / disabled 不进 A 类 /
   探测失败 rc=2 可见 / 源码级只读钉。
4. `docs/CONTROL-LOOP-LEDGER.md §2.5` 入账 + 本文件 + RFC-093。

**明确未做（防误读）**

- ❌ 未改任何权重（`config/playbook_screening.yaml` 一字未动）；❌ 未停用/修改 `ic_adj` 通道；
  ❌ 未改 `classify_factor` 或 `get_instructions_with_outcome`（§4.1 只读数、未动判据）；
  ❌ 未动 `factor_verdict_scorecard.py` 的三分类（它按 IC×avg_alpha 判，是第四把尺子，本轮未纳入对账）；
  ❌ 未回放验证任何错配的净 PnL 影响（见 §7 局限）。

---

## 7. 局限（诚实清单）

1. **`eff` 是静态推演**：第 2 节各行是"该因子触发时的贡献"，未经实弹回放验证"修正某个错配会带来多少净 PnL"。
   任何 T1②–T6 落地前必须跑回放（`backtest-rule-replay`：四关门 + 安慰剂对照）。本文件不构成已验证的收益声明。
2. **剔零口径不是第二把权威尺**：`wins/(wins+losses)` 是"成交兑现口径"，它同样不是边际贡献；
   §4.1 的主张仅是"**不要把非结果行计进胜率分母**"，不是"剔零后的 56% 就是该因子的真实胜率"。
3. **台账窗口不是"90 天"**：取最近 **900 行已结算指令**（`days_back*10`），n 随结算速率漂移；
   且 `WR` 是**指令级净胜率**，与成交笔胜率、信号级命中率都不是同一轴。
4. **单因子 verdict ≠ 边际贡献**：因子间共线性（`above_ma20` 与 `volume_expansion`/`cluster_buy` 共振、insider 三因子互斥）
   未做控制 ⇒ "negative/positive"不是"去掉这个因子会更好"。
5. **数据快照**：IC 9/18 21:40Z、台账 9/18 20:01Z 刷新；台账读 **sim 库**（REAL 库 `ftseek_us_live.db` 的
   `playbook_factor_stats` 仍是 2026-06-18 的 8 行陈旧数据 ⇒ 本对账对实盘口径不成立；切换实盘后须换库重跑，脚本支持 `FTSEEK_LEDGER_DB`）。
6. **`insider_cluster_buy` 的 n=1 是指令级读数**，IC 文件 n=4（信号级）——两个都不够，但"不够"≠"该因子无效"。
7. **`void` 是否该从分母剔除未定谳**：它是幽灵仓清理产物（已有专门防护），本轮只把它计入"非结果行"呈现，未单独下结论。
8. **`missed` 的含义未深挖**：380 行 `missed` 是"价格未进触发带"还是"其他未成交"，本轮未追；若其中含系统性偏差
   （如某类因子天然更常 missed），则 §4.1 的结论还要再收一层。

## 8. 自曝（对上一轮结论的修正）

| 上一轮（同一会话早前）的说法 | 本轮实测 | 结论 |
|---|---|---|
| "P0：正权重买『负证据』，5 个因子该优先裁" | 5 项里 **4 项的 negative 由分母口径驱动**（剔零 WR 56–60%）；只有 `macd_golden_cross` 靠金额腿（PF 0.67） | **核心结论被推翻**：这批不是"负证据"而是"指令兑现率低"；原排序作废并重排（T1/T2） |
| "IC 通道给负 verdict 因子 +1 是错" | 拉开现金口径后 5 项里 **3 项 IC 确实错**、**2 项 IC 与现金同向** | **部分证伪**：靶子收窄到 3 项，见 T5 |
| "insider_net_buy / above_ma20 是 P0，正权重买负证据" | 两者 verdict=negative 但净额为正（+250 / +140，PF 1.12 / 1.06），剔零 WR 57–60% | **修正**：从"该砍"移到 F 类"尺子冲突"，且不再主张砍它们的 IC 加成 |
| "macd_golden_cross 拿全场第二高权重、负证据" | 它确实是**唯一**在"剔零胜率刚过半 + 金额负 + PF 0.67"下都弱的正权重因子 | **升级**：从 P0 第 5 位升为 T2（仅次于口径定谳） |
| "volume_expansion 被罚错" | 成立且更强：净 +313、PF 2.26、剔零 WR 64.7% | **维持**（T4） |
| "③ base 与 history 疑被计两次（需核实）" | 机制实锤；**写者 2026-07-31 已停用**；存量 9 条可实证 | **定性成立、范围收窄**：从"每天在双计"改为"存量 9 条 + 写者已停" |
| "台账 WR 高度趋同 0.33–0.36 ⇒ 因子区分度接近 0" | 机制找到：那 33% = `win/全部行`，分母含 42% `missed` | **升级为根因**（§4.1）：不是区分度低，是分母口径 |
| "news_positive（最大样本）最该先裁" | 净 −$172 / n=476 / PF 0.97（avg −$0.36/笔，接近打平） | **降级**：仍列 T5，但理由是"三口径同号"而非"最大样本"；单看金额强度弱于 `macd_golden_cross`（T2） |

## 9. 请复查者挑战这几点（只读；每条给命令与判据）

| 待挑战声称 | 打假方式 | 打假成功判据 |
|---|---|---|
| `eff = base + ic_adj + Σhist` 三处相加 | 读 `screener.py` L265/L275-316/L352-359 + 跑 `--force` 对表 | 找到第四处写入或某处并未相加 ⇒ 表列失效 |
| 台账胜率分母含 `missed`/`already_held`/`void`（窗口 44%） | 跑 §4.1 复现脚本 | 分解出的非结果行占比明显不同 ⇒ §4.1 作废 |
| `classify_factor` 的 negative ＝ 贝叶斯上界<45% **或** PF<0.8 | 读 L118-135 | 判据另有实现 ⇒ F/G 类命名错 |
| 剔零后 4 项不再 negative | 用 `wins/(wins+losses)` 重算贝叶斯区间；或跑 §4.1 脚本看 56–60% | 剔零后仍在 0.45 上界以下 ⇒ 该条应从 G 类撤 |
| `ic_adj` 不受 `ic_feedback_to_screener` 控制 | 读 L265（无条件）与 L365-373（只门控 strategy） | flag 能门控 factor 级 ⇒ §4.3 结论错 |
| 7/8 批 9 条 delta 双计 | 跑 §4.4 复现脚本 | 任一条 Δbase≠Δdelta ⇒ 该条应撤 |
| 写者 2026-07-31 停用 | `git log -1 --format='%h %ad %s' 679b826b3` + 读 `weekly_report.py` L68-73 | 另有调用点仍在跑 ⇒ §4.4 需改"每天双计" |
| 三套 n 是三个轴（665/297/476） | 读 `factor_ic_backfill.py` L272-303 与 `db.py` L971-982 | 任一轴其实同源 ⇒ §4.2 不成立 |
| `trade_wr` 无交易路径消费者 | `grep -rn "trade_wr" app/ scripts/ ops/ tests/` | app/ 下有消费者 ⇒ T7 严重度上调 |
| 本批零交易行为变更 | `git show --stat` + `git diff HEAD~1 -- config/ app/` + 对账器只读钉 | 出现 config/app 行为改动 ⇒ 立即回滚本批 |