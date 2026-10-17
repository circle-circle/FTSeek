---
title: 门误杀台账 (跨门案例累计 + 门级基率)
status: active
module: governance
related_code: scripts/settle_shadow_portfolio.py, scripts/gate_block_counterfactual.py, scripts/settle_entry_confirm_forward.py, scripts/cf_pullback_limit_replay.py
related_okr: OKR-2026-10
tags: [gate, miskill, ledger, governance]
---

# 门误杀台账 — 跨门案例累计 + 门级基率

> 缘起: 用户 2026-10-16 令「误杀写入, 累计此类问题, 后续做共性分析提升调整」。
> 定位: 本文件是**案例流水 + 门级基率的索引**, **不是新账本** —— 逐笔真值仍在 §2 的三个既有面
> (jsonl / DB), 本文只登记「谁在何时被哪道门拦、事后反事实是多少、判读是什么」。
> 纪律: 单日/单案例**只登记不调阈值** (误杀预算纪律, 见 §1)。

## 1. 口径 (先读)

- **误杀 (miskill)** = 被门拦下的单, 事后反事实为**盈利**。比例口径 = 误杀笔数 ÷ 被拦笔数。
- 两个已冻结的门限 (不可在本台账里放宽):
  - ①D 案裁决卡: 误杀率 ≤ **0.40** (expired 口径, 分母 RFC-081 §六 冻结)
  - ②B19 `eod_depth_gate`: `miskill ≤ 1` **绝对计数** (已满 ⇒ 任何新案例都不得当放宽理由)
- **两种量化口径不可混用**:

| 口径 | 定义 | 用在哪 |
|---|---|---|
| 影子结算净额 | 影子按**生产退出栈**重放被拦单 → 净额 > 0 即误杀 | `shadow_portfolio_outcomes` / D 案重放 |
| 收盘盯市 | 被拦价 → 当日收盘 (不含退出栈) | 本台账 §4 快速登记, **粗于上者** |

- **禁止**: 拿案例反推阈值; 拿轮询级**事件数**当样本 (实测放大 **46.7×**, 24853 事件 = 532 指令);
  用「影子组合」做单门归因 (它按 (inst_id,date) 先到先占, 跨原因去重)。

## 2. 数据面 (真值在这里, 不在本文)

| 面 | 文件 | 覆盖的门 | 结算/消费载体 | 去重口径 |
|---|---|---|---|---|
| 影子组合 | `data/shadow_portfolio_entries.jsonl` → `_outcomes.jsonl` | vwap_gate / falsification_vwap_slope / band / vol_gate | `ops/hermes/shadow_settle_daily.sh` (Hermes job `1a4a63fd3dae`, 08:10 CST) | (inst_id,date) **先到先占, 跨原因去重** |
| 触发未中遥测 | `data/trigger_miss_telemetry.jsonl` | 全部触发门 (streak_ban / low_atr / after_cutoff / thesis_drift …) | `scripts/gate_block_counterfactual.py` (**未挂 cron, 按需跑**) | 轮询级, 按 inst_id 去重 |
| 入场确认门 | `data/shadow/entry_confirm_forward.jsonl` | entry_confirm 弃单 | `scripts/settle_entry_confirm_forward.py` (crontab `37 6 * * 2-6`) + `scripts/entry_confirm_postmortem_v1.py` | 一 (inst, 臂) 一行 |
| D 案重放 | `data/shadow/pullback_limit_replay_v2_latest.json` | chase_cap / 确认位等待 | crontab `47 5 * * 2-6` | 按 (inst,date) 配对臂 |

刷新配方 (全只读, 仓根执行):

```bash
PYTHONPATH=. venv/bin/python scripts/gate_block_counterfactual.py     # 门级 30d 拦截质量 → data/gate_block_counterfactual_latest.json
PYTHONPATH=. venv/bin/python scripts/settle_shadow_portfolio.py      # 影子组合结算 (已在 cron, 手动跑幂等)
PYTHONPATH=. venv/bin/python scripts/cf_pullback_limit_replay.py --from-date 2026-09-08
```

## 3. 门级基率 (2026-10-16 读数; 逐日漂, 引用须带日期)

### 3.1 影子组合 (全史 193 行, 已全结算)

| 门 | n | 净合计 $ | 净均 $ | 误杀率 (净>0) | 读法 |
|---|---|---|---|---|---|
| vwap_gate | 87 | −803.0 | −9.23 | 0.74 | 拦的多是小赢家, 但净额被少数大亏单拉负 ⇒ **净避坑** |
| falsification_vwap_slope | 85 | −530.4 | −6.24 | 0.72 | 同上 |
| band | 16 | +172.2 | +10.76 | 0.75 | **拦了赚钱的一群** (n 小, 只登记) |
| vol_gate | 5 | −24.8 | −4.95 | 0.40 | n<10 不判 |

> ⚠️ 「误杀率高 + 净额为负」不是矛盾: 这些小单的反事实是小赢家, 门避的是尾部大亏 ⇒
> 判门好坏**看净额, 误杀率只用来量化代价**。反向形态 (净额为正) 才是真问题。

### 3.2 触发门 30 日拦截质量 (`gate_block_counterfactual`, 基准 `fwd_return_t1_open`, 剔费 0.18%)

| 门 | 指令 n | t1_open 均 | 净均 (剔费) | 判读 |
|---|---|---|---|---|
| vwap_gate_block | 121 | −1.16% | −1.34% | 避坑 ✓ |
| low_atr_block | 115 | +0.10% | −0.08% | 中性 |
| **falsification_vwap_slope** | 93 | **+0.39%** | **+0.21%** | **杀机会 ✗** |
| before_open_blackout | 50 | +0.06% | −0.12% | 避坑 ✓ |
| runaway_up | 38 | +0.50% | +0.32% | 杀机会 ✗ |
| extreme_gap | 30 | −0.96% | −1.14% | 避坑 ✓ |
| vol_gate_block | 22 | −0.02% | −0.20% | 避坑 ✓ |
| **streak_ban** (连败封禁) | 21 | −0.05% | −0.23% | 避坑 ✓ (盈利方向占比 60% ⇒ 拦的多是小赢家) |
| thesis_drift_invalidate | 11 | −4.51% | −4.69% | 避坑 ✓ (最强) |
| runaway_down | 10 | +1.03% | +0.85% | 杀机会 ✗ |

n<10 不判: borrow_pool_gate(7) / reanchored(5) / knife_below(4) / earnings_blackout(3) / liquidity_gate(2)。
台账全表另见同脚本输出 (15 行)。

### 3.3 另两面

- **entry_confirm**: v2 判据 `INSUFFICIENT` (2026-10-17 06:37: n=4<60 ∧ 交易日 1<10, 不作 PASS/FAIL);
  v1 窗事后分解 (至 2026-10-14, n=33): 真避免 9 / **误杀 5** / 口径存疑 2 / 无数据 17。
- **D 案 (chase_cap / 确认位等待)**: 配对 n=163 → D 误杀 **87/148 = 58.8%** (>0.40 门 ❌),
  不成交腿净放弃 **+$1,391.75**; 配对差中位 −$10.61/笔, 日聚类 t=0.81 (MDE ±$11.22) ⇒ 与 0 不可分辨;
  成交率 0.33 (<0.50) ⇒ ADR-035 接线维持关闭。

## 4. 案例流水 (append-only; 新案例按日期追加在表尾)

| 日期 | 标的 | 方向 | 门 | 被拦价 / 帽 | 反事实 (收盘盯市口径) | 判读 | 证据 |
|---|---|---|---|---|---|---|---|
| 2026-10-16 | US.NBIS | SHORT | entry_confirm 弃单 | 锚 178.69 | 收盘 165.47 → **+7.40% (+$436, 33 股)** | **误杀** | `entry_confirm_forward.jsonl` inst 4313 (ABANDONED, grace 2/2 persisted) |
| 2026-10-16 | US.COIN | SHORT | streak_ban (连败封禁) | 182.50 | 收盘 173.16 → **+5.12% (≈+$299, 32 股)**; 触发条件 (反弹≥入场价) 09:56 起满足 35 根 bar | **误杀** | `trigger_miss_telemetry.jsonl` streak_ban (当日 85 行) |
| 2026-10-16 | US.AEM | LONG | entry_confirm 弃单 | 锚 189.00 | 收盘 189.53 → +0.28% (+$16) | 轻度误杀 | inst 4311 |
| 2026-10-16 | US.TEAM | LONG | entry_confirm 弃单 | 锚 218.99 | SL 208.04 于 10:28 被打穿; 收盘 207.82 → −5.10% (−$296) | 真避免 | inst 4312 |
| 2026-10-16 | US.CRM | LONG | entry_confirm 弃单 | 锚 249.36 | SL 238.14 于 11:06 被打穿; 收盘 240.70 → −3.47% (−$258) | 真避免 | inst 4320 |
| 2026-10-16 | US.CMC | LONG | chase_cap (25 轮) | 帽 66.50 (被拦价 66.62→68.00) | 落帽后 12:06 成交 @66.38 → **净 +$12.26** | **非误杀** (延迟放行, 成交价优于首轮被拦价 0.36%) | `pullback_limit_shadow.jsonl` + `trade_records` id 1680 |
| 2026-10-16 | US.NOW | LONG | vwap_gate | 150.155 (锚 151.16) | 生产栈重放 −$298.21 | 真避免 | `shadow_portfolio_outcomes` |
| 2026-10-16 | US.META | LONG | vwap_gate | 731.02 (锚 736.5) | 生产栈重放 −$177.72 | 真避免 | 同上 |
| 2026-10-16 | US.RIVN | SHORT | vwap_gate | 13.13 (锚 13.37) | 生产栈重放 +$9.63 | 轻度误杀 (该单后经确认门成交并净 +$8.43) | 同上 |
| 2026-10-16 | US.AMD / AVGO / ADSK / CMC | LONG | falsification_vwap_slope | 502→… | 生产栈重放 +16.44 / +17.93 / +8.75 / +12.70 | **不宜计误杀** — 四单同日**后续实际成交** (影子行只记首次拦截) | 同上 |

**当日合计 (仅 entry_confirm 门, 无门反事实)**: 混合臂毛 −$101.0 → 净 ≈ −$118.6
⇒ 该门当日净贡献 **≈ +$119** (避免 2 / 误杀 2; 误杀金额 $452.7 ≈ 避免金额 $553.8 的 **82%**)。

## 5. 待共性分析轴 (供后续调优, 各有载体)

| 轴 | 观察 | 需要的数据 |
|---|---|---|
| A. **方向** | 10-16 两条误杀 (NBIS / COIN) **都是 SELL_SHORT** | §3 两个面目前**未分方向** → 需按 direction 重切 (一行改动) |
| B. **"等待型"门同源?** | entry_confirm (等 2min 反转) + chase_cap (等回落帽下) + D 案 (等确认位) 都是"等价格回来才放行" ⇒ 天然错过已启动行情。CMC 是活标本: 帽的放行点落在**峰值后 1.6%** | D 案已配对重放; entry_confirm 需 n≥60 (v2 窗现 n=4) |
| C. **封禁类门** | streak_ban 净避坑 −0.23% 但盈利方向占比 60% ⇒ 拦的多是小赢家; 若小赢家恰好是"高胜率低赔率"型, 封禁会拉低笔数而不改善期望 | 需按 MFE 分层 + 与 `mfe<0.5%` 判据对齐 |
| D. **四象限定位** | 门级 (净额, 误杀率) 二维: 净<0 且误杀高 = 正常 (小赢家换大亏); **净>0 = 真问题** (现仅 `falsification_vwap_slope` +0.21%、`runaway_up` +0.32%、`runaway_down` +0.85%) | §3.2 全表 + 分方向细化 |

## 6. 局限

- 影子组合「先到先占」去重 ⇒ 单门归因必须走遥测面 (§3.2); 用 §3.1 分组会系统性漏计后发原因。
- §4「收盘盯市」粗于生产退出栈 (CMC 一例: 盯市 −$146 / 生产栈 +$72 界内), 逐笔判读须回落 §2 的口径。
- 案例 n 均为个位数, **只登记不裁决**; 门级判据仍以各自预注册文本 (判据字典 B14/B19、RFC-141 §4) 为准。
- `gate_block_counterfactual.py` **未挂 cron** ⇒ 其读数会陈旧; 引用前先看产物 mtime, 或当场重跑。