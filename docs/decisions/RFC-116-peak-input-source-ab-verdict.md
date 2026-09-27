# RFC-116 — BE/trailing 的 peak 输入源: quote 轮询峰 vs bar 峰 (回测已裁, **维持 quote 不切换**)

日期: 2026-09-27
裁决依据: 单变量配对回测 (492 笔, 1min bar 全日 390 根覆盖 492/492)
分类: L1 (离线回放 + 文档) — **零交易行为变更**
交易行为变更: **零** (结论是"不切换"; 唯一代码改动是修一个离线脚本的 `None` 崩溃)

---

## 1. 起因: 审计 MU 案时撞出的测量缺口 (真缺口, 但方向与直觉相反)

30 日止损审计复核时发现: 出血堆里 12 笔的 `mfe_pct` 列是 `0.0`, 而同笔
`metadata.peak_price_bar` 反算出 0.39~1.41% ⇒ 看着像"这些笔其实翻过绿、BE 本该武装却没武装"。

**根因核实 (不是猜)**:
- `peak_price` (quote 轮询峰) 全仓唯一抬高处 = `app/playbook/exit_engine.py:338-341`,
  来自轮询的 `current_price`; `app/playbook/pipeline.py:8582` 只
  `peak_price = float(meta.get("peak_price", 0))` **恢复**、从不抬高
  (注释自述: "sim 此前未做 peak tracking, 现与 shadow 对齐")。
- BE 武装判据 `breakeven_triggered = peak_profit_pct >= breakeven_at_pct` (`exit_engine.py:461`)
  读的正是这个轮询峰。
- `peak_price_bar` 由 1min K 线 high/low **并行采集** (`pipeline.py:7451+`),
  7/28 起已按末标签口径排除入场前 tick ⇒ bar 峰不含入场前污染。
- 该采集器 docstring 自己写着: 「peak/trough 现用每轮 last quote 采样, **轮询间隙的真实高/低点
  全部丢失 → MFE 系统性低估** (实证 SL_TRAIL_LOCK 批次 MFE 1.48% 仅捕获一半), trailing 判分失真,
  exit_shadow 反事实也吃同一个失真 peak」, 并预注册处置路径: 「**周度对比 bar 版 vs quote 版,
  再决定是否切换决策输入** (切换=显式行为变更, 需开新 config_version 窗口, 另行批准)」。
- 该周度对比**从未落地**: `crontab` 零命中、无产物 ⇒ 尺子造好了没挂秤。

按用户 2026-08-14 裁决偏好 (「历史数据/重放可行时直接回测裁决, 不默认前向攒样」),
本 RFC 直接跑回测裁决, 不新立影子攒样。

## 2. 漏峰是**系统性**的 (这一半结论成立)

全窗 (7/15 起, A9 清洁) n=188:

| 堆 | n | 仅 bar 达峰≥0.4% | 占比 | mfe 列低估中位 |
|---|---|---|---|---|
| 赚钱堆 `SL_TRAIL_LOCK` | 51 | 0 | 0.0% | +0.077pp |
| 白跑堆 `SL_BREAKEVEN`/`DEAD_SIGNAL*`/`PRE_CLOSE*` | 107 | 3 | 2.8% | +0.060pp |
| **出血堆 `SL_INITIAL`/`OVERNIGHT_GAP_STOP`** | 23 | **10** | **43.5%** | **+0.299pp** |

⇒ quote 峰确实系统性低于 bar 峰, 且**集中在出血堆**。测量缺口是真的。

## 3. 但"修好测量"会**变亏**: 单变量配对回测否决切换

### 3.1 设计 (纯单变量, 其余全钉实盘真值)

- 仪器: `scripts/trailing_polling_sim.py::sim_polling` 语义 (peak 用 bar high/low, 触发用 30s 采样点
  `(o+c)/2` 与 `c` — 与实盘一致: 实盘也按轮询价判 SL, 不按 bar low 触及)。
- 实盘真值 (`config/playbook_horizon.yaml` default 段, **不用代码默认值**):
  `breakeven_at_pct=0.4`、`drawdown_from_peak_pct=0.8`、费垫 8/6 前 0.15% / 后 0.4%、
  冷却期 15min SL 放宽 4%。
- **唯一变量** = bar 极值是否并入 peak。两 arm 同用 30s 采样点触发、同逐采样点重算
  `peak → mfe → 武装 → sl → 比较` 的顺序 (v1 脚本把 mfe/sl 每 bar 只重算一次 ⇒ quote arm 滞后,
  不对称, 已弃用)。
- 未触发的笔两 arm 都回落到实际净 ⇒ 同口径。

> 注: 既有 `trailing_polling_sim.py` 的网格跑的是 触发线 0.5/0.8 × 回撤 0.3/0.5/0.8 —
> **没有一个组合是实盘值, 且两变量同时动** ⇒ 其读数 (6 变体全负 −706~−1593) 不能归因到峰输入。

### 3.2 结果 (492 笔配对)

| | quote 版 (实盘) | bar 版 (假设切换) | 差 |
|---|---|---|---|
| 总净 | **−$3,390.19** | **−$4,761.02** | **−$1,370.83** |
| 模拟触发 | 345 | 366 | +21 |
| 武装过 | 362 | 382 | +20 |
| 峰中位 | +0.5696% | +0.5768% | +0.007pp |

分歧笔 **173/492 (35.2%)**: quote 版 $+2,297.22 / bar 版 $+926.38。
- bar 更好 85 笔 (+$1,079.60)
- bar 更差 88 笔 (**−$2,450.43**)

按武装差异分解 (关键):

| 类 | n | 差 |
|---|---|---|
| 「仅 bar 武装」= 漏峰线索真身 | 19 | **+$58.19** |
| 「仅 quote 武装」 | 0 | $0 |
| 「两 arm 都武装, 路径不同」 | 154 | **−$1,429.02** |

**⇒ 修好漏峰本身几乎不赚钱 (+$58), 亏的是 bar 峰把 trailing 抬得更高 ⇒ 更多单被锁在
回撤线下方更早出场 (−$1,429)。** 这与既有裁决同族:
MEMORY「贴成本线锁死两窗口一致判最差解 (净赚占比 31% vs 49~58%)」、
`exit_param_shadow` BE/DD 5 变体全 FAIL (一致性门 55%, 最好 be0.3_dd0.3 仅 38.2%)。

### 3.3 MU id=1561 自证 (最刺眼的一行)

```
id    sym     dir   exit_reason     实际净    quote版   bar版    类
1561  US.MU   LONG  SL_BREAKEVEN   +15.67   +15.67   −44.00   仅bar武装
```

用户复核认定的"BE 保本保护链成功案例"(水下 MAE −3.96% 转净 +$15.67), **换成 bar 峰会变成亏 $44**。
同一轮里既拿它当正面样本、又提议换掉让它成立的输入 ⇒ 自相矛盾, 此处记录以防复发。

「仅 bar 武装」19 笔的构成:
- 实际**盈利** 4 笔 $+69.17 → bar 版后 **−$223.73** (被贴到成本线砍掉 = 切换的代价)
- 实际亏损 15 笔 $−1,216.50 → bar 版后 −$865.41 (改善 $351, 远不抵上面 154 笔的路径损失)

### 3.4 日聚类显著性

n=50 天, 日均差 **−$27.42**, sd $76.85, **t=−2.52** ⇒ |t|≥2, **显著为负**
(不是与 0 不可分辨, 是显著更差)。最有利单日 2026-08-28 仅 +$191.53 ⇒ 非单日驱动。

## 4. 保真度披露 (不许把方向性结论当精确值)

| 检查 | 结果 |
|---|---|
| bar 原料覆盖 | 492/492 笔有入场日 bar, **全部 390 根** (整日完整) |
| 触发率 vs 实盘 | sim quote 版 345/492 = **70.1%**; 实盘 SL 类 (`SL_*`) 合计 **68.9%** ⇒ 触发面几乎一致 |
| 净结局水平 | 实际 −$1,068.85 / sim quote −$3,390.19 / sim bar −$4,761.02 ⇒ **sim 系统性悲观 $2.3k~$3.7k** |
| 跨日持仓 | 40/492 (8.1%) 只模拟入场日、之后回落实际净 ⇒ 这部分两 arm 差异被**人为压平** (若全覆盖, bar 版可能更差) |

⇒ **绝对值不可用, 配对差的方向可采信, 量级须打折**。本 RFC 的结论是
「**方向性否决**」, 不是「精确 −$1,370.83」。
水平偏差来源: 冷却期/trailing 语义与实盘不完全同、`DEAD_SIGNAL_EXIT`/`TIME_EXIT`/
`MFE_STALL_EXIT`/`PRE_CLOSE_EXIT` (合计 26%) 等非 SL 出场路径未建模。

## 5. 裁决与处置

1. **不切换 BE/trailing 的 peak 输入源** (维持 quote 轮询峰)。依据: 单变量配对回测
   bar 版显著更差 (t=−2.52), 且漏峰修复本身只值 +$58。
2. **`peak_price_bar` 维持 record-only 不参与决策** — 这正是采集器 docstring 的原始约定
   ("仅落 metadata, 不参与任何退出决策"), 本 RFC 是首次真正跑完它预注册的那个对比并给出读数。
3. **不新建影子攒样** (回测已裁, 按 8/14 偏好)。
4. **连带修正一个口径认知** (见 §6): RFC-107 把 `peak_price_bar` 并入 `mfe_pct` 的 `max()`
   ⇒ 落库的 mfe 从此是 **bar 口径**, 而 BE/trailing **决策仍读 quote 口径**
   ⇒ 两者不同轴, 读遥测时不得互相代入。
5. 代码改动仅一处: `scripts/trailing_polling_sim.py` 的 `pnl_net` 为 `None` 时崩溃
   (`TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'`),
   改 `COALESCE(pnl_net, realized_pnl, 0)`。**离线脚本, 零交易行为变更**。
   (同 MEMORY「dict.get None→or default」族。)

## 6. 连带发现: 30 日审计的"墓地族"归因是口径依赖的 (撤回)

RFC-107 把 `mfe_pct` 改为 `max(累积, peak_price 反算, peak_price_bar 反算)`,
该改动 **2026-09-27 09:00 重启后生效** (`pipeline.*` ⇒ HotReloader 穿不透)。
用新口径重算 30 日窗: **169/188 笔 (89.9%) 的 mfe/mae 会变** (中位 +0.055pp, max +1.41pp)。

| | 旧口径 (2026-09-26 审计所报) | 新口径 |
|---|---|---|
| 墓地族 mfe<0.3% | 46 笔 / **−$4,082** | 30 笔 / **−$1,622** (缩 61%) |
| 墓地族 ∩ 出血堆 | 22/23 = **96%** | 10/23 = **43%** |
| SL 绑定率 MAE/SL≥0.7 | 15 笔 (8.0%) / −$2,874 | 18 笔 (9.6%) / −$3,143 |

**⇒ 撤回上轮头号结论「MFE<0.3% 墓地族 41 笔净 −$4,082 = 全部亏损来源」**: 它约一半是测量假象
(13 笔出血堆的 mfe 从 `0.0` 变成 0.39~1.41%)。

**存活的 (口径无关, 按 `exit_reason` 前缀匹配)**: 三堆逐字不变 —
赚钱堆 51/+$2,893/94.1%、白跑堆 107/+$887/83.2%、出血堆 23/−$4,287/0.0%,
窗内净 −$498.00、毛 $434.61、费 $932.61。
「出血堆病灶在入场不在止损」仍成立, 但**不能再用 mfe<0.3% 当它的识别特征**。

⚠️ 前缀匹配陷阱: 真实值是 `DEAD_SIGNAL_EXIT` / `PRE_CLOSE_EXIT` (带 `_EXIT` 后缀),
用精确匹配会把白跑堆算成 85/+$615 而非 107/+$887。

## 7. ⚠️ 连带发现: 官方回放引擎跑在 bar 口径上, 而实盘是 quote 口径 (未登记的分叉)

`scripts/exit_replay_backtest.py` 自称「数学: 直接复用 `app.playbook.exit_engine.
_compute_dynamic_effective_sl` / `_get_factor_exit_config` — 与实盘退出同一函数,
杜绝两份数学漂移」, 并如实列了 5 条「保真度边界」。但:

- `replay_trade()` 内 `fav_px = bar["h"] if direction == "LONG" else bar["l"]`,
  然后 `peak_profit_pct = max(peak_profit_pct, _pct(fav_px))`
  ⇒ **peak 用 bar 极值**, 而实盘 `exit_engine.py:338-341` 用轮询的 `current_price`。
- `grep -cE "peak_src|quote_peak|peak_mode" scripts/exit_replay_backtest.py` = **0**
  ⇒ 该轴**不存在开关**, `_variants()` 的 20+ 个变体 (`live_baseline` / `dd_*` / `be_*` /
  `tiered_*` / `early_tight_*` / `mfe_stall_off` / `e_step1_*` / `e_full_*`) 全部跑在 bar 口径上。
- 「保真度边界」5 条**没有一条提到 peak 口径分叉** (第 3 条只说「回放跳过入场 bar
  (实盘 ~60s 轮询, 等效粒度)」⇒ 作者知道实盘是轮询的, 但没把 peak 来源列为边界)。

**为什么这不是小事**: peak 幅度直接门控 BE 武装 (`peak_profit_pct >= breakeven_at_pct`)
与 trailing 锁利线 ⇒ **peak 口径与 be/dd 阈值不是正交的**, 而是交互项。
本 RFC §3.2 实测该轴本身值 −$1,370.83 (t=−2.52) ⇒ 量级足以影响变体排序。

**受影响的历史裁决** (按 `config/playbook_horizon.yaml` 注释自述的回放依据):
- `default.breakeven_at_pct: 0.4` — 注释: 「回放n=265 pess+88/opt+161双序正」(2026-08-17 用户裁落地)
- `drawdown_from_peak_pct: 0.8` — 注释: 「0.4→0.8: handoff处方回归」
- `time_exit_hours: 6.0` — 注释: 「TIME_EXIT 7笔0%WR-$238」

⇒ 这些都是在 **bar 口径 peak** 下验的, 而 live 拿到的是 **quote 口径 peak** (系统性更低,
见 §2: 出血堆 mfe 低估中位 +0.299pp、全窗 13/188 笔仅 bar 达峰)。
**不是说这些裁决错了** — 引擎内所有变体同口径 ⇒ 变体之间的相对比较仍自洽;
而是**「be_0.4 优于 be_0.5」这个排序在 quote 口径下未经验证**, 且 bar 口径对
"更早武装"类变体系统性有利 (峰更高 ⇒ 更容易过阈值 ⇒ 看起来更划算)。

处置 (L1, 本批不做, 登记为待办):
- 给 `exit_replay_backtest.py` 加 `--peak-src {bar,quote}` 轴 (默认 bar 保持向后兼容,
  不改任何既有产物), 重跑 `be_*` / `dd_*` 网格的**双口径对照**;
- 若 quote 口径下 be_0.4 仍占优 ⇒ 裁决存活, 补一条保真度边界说明即可;
- 若排序翻转 ⇒ 按 §5 的纪律走 (改参 = L3, 需用户裁 + 新 config_version 窗口)。
- 另: `data/exit_replay_latest.json` mtime = **Aug 11 22:51** (已 stale 47 天), 且
  `crontab` 无该脚本 ⇒ 它是按需工具不是活环, 重跑不影响任何在跑链路。

## 8. 复现

```bash
# 单变量 A/B (v2, 逐采样点重算 — v1 有顺序不对称 bug 已弃)
venv/bin/python3.11 /tmp/peak_ab2.py
# 保真度核查
venv/bin/python3.11 /tmp/fidelity.py
# 漏峰面按堆分布 + 对照臂 (反面①②③)
venv/bin/python3.11 /tmp/missed_peak_control.py
# 口径迁移影响
venv/bin/python3.11 /tmp/caliber_shift.py
```

实盘真值取法: `grep -nE "breakeven_at_pct|drawdown_from_peak_pct" config/playbook_horizon.yaml`
(**不是** `config/unified_config.yaml`; 后者只有 `breakeven_fee_cushion_pct` 等费垫键)。
