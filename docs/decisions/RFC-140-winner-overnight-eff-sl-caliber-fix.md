# RFC-140: 赢家隔夜影子 B16 口径修复 — 初始 SL 冻结 → era-faithful 生效 SL 重放

- 日期: 2026-10-09
- 类型: L1 (测量面修复, <200 行; 生产退出链零行为变更)
- 用户授权: 2026-10-09 「确认无误，确保精准后修复，希望基于这些统计数据做策略分析和调整方向指导」
- 判据关联: B16 winner_overnight (ADJUDICATED-CRITERIA.md), DEB-20260902-overnight-exit-timing
- 先例纪律: RFC-095 (B19 口径修复, 判据/阈值逐字未动, 双读数留档)

## 1. 问题 (尺子级缺陷, 可翻转裁决方向)

RFC-074 原结算器用 `stop_loss_at_arm = meta.stop_loss` (**初始 SL**) 冻结判定次日反事实结局。
但真实策略在 arm 时刻 (15:52 PRE_CLOSE) 的生效 SL 早已不是初始 SL:

- 浮盈 ≥ breakeven_at_pct(0.4%) → BE 武装 (entry + fee + slip cushion)
- adaptive trailing (9/17 起, SIM-only): 锁盈线 = peak × (1 − clamp(40%×MFE, 0.4%, 1.2%))
- 静态因子 trailing (era 全程): 锁盈线 = peak × (1 − drawdown_from_peak_pct)
- profit_override (trailing=false 因子): 浮盈 ≥0.8% 强制锁盈 dd 1.0%

「带 SL 持一夜」判据的本意是**真实策略的 SL 状态**过夜; 初始 SL 口径让反事实臂对
BE/锁盈线免疫 ⇒ **结构性高估持夜收益, SL_touch 读数系统性偏少** (注册表旧注记原文自认
「生效SL更紧, SL_touch 偏少知悉」, 但偏差量级从未测过)。

## 2. 实测偏差 (存量 4 笔, era-faithful 重放, 悲观序)

| symbol | arm_date | era 因子 | 旧口径(初始SL) | 新口径(生效SL) | 新结局 |
|---|---|---|---|---|---|
| US.META | 09-04 | default (trailing dd 0.8, adaptive OFF) | −26.64 EOD | **+16.15** | SL_touch @锁盈线 618.23 (次日峰值 1.68% ratchet) |
| US.NOK | 09-17 | default (adaptive ON, BE 未武装) | +19.43 EOD | **+50.74** | SL_touch @锁盈线 10.7364 |
| US.BA | 09-29 | news_negative (trailing **False** → profit_override) | +51.15 EOD | **−134.85** | **SL_gap** 开盘 192.05 穿 BE 线 189.36 |
| US.FRO | 10-01 | news_positive (trailing dd 0.8, adaptive ON) | +139.20 EOD | **−74.28** | SL_touch @锁盈线 50.96 |
| **合计** | | | **+183.14** | **−142.24** | SL 类出场 **4/4** (门: <40%) |

方向翻转: 旧口径「0 次 SL、+$183、胜率 3/4」→ 新口径「4/4 SL 类出场、−$142、胜率 1/2」。
若照旧口径攒到 n=20, B16 三门可能全过而提案「赢家持夜」——真实持夜是带 BE/锁盈线过夜、
会被开盘跳空与日内回踩打穿。**旧口径下继续攒样 = 负价值。**

注: 修复提案阶段的手算验证 (−185.91) 与生产 SSOT 重放 (−142.24) 有差 — 手算版漏了
slip 垫与静态因子 trailing ratchet (META/NOK 因此被低估)。定稿以 SSOT 函数重放为准,
这正是「复用生产数学、不写第二份」的价值实证。

## 3. 改动 (全部实测验证)

1. **arm 埋点 +14 era 字段** (`app/playbook/exit_engine.py`, record-only, 零行为变更):
   `eff_sl_at_arm / peak_profit_pct_at_arm / era_be_pct / era_trailing_enabled /
   era_factor_dd_pct / era_fee_cushion_pct / era_slip_cushion_pct / era_adaptive_on /
   era_target_capture_pct / era_min_dd_pct / era_max_dd_pct / era_cooldown_sl_pct`。
   全部取本轮规则链已算好的作用域变量, 零新计算。**参数时代随 arm 行冻结** —
   未来 BE/dd/adaptive 怎么调, 反事实臂自动按当时 era 重放, 免「参数时代混算」。
2. **结算器主口径切换** (`scripts/settle_winner_overnight_shadow.py`):
   `_replay_eff_sl` 逐 bar 调生产 SSOT `_compute_dynamic_effective_sl` +
   `adaptive_trailing_drawdown_pct` 重放; 悲观序 (不利极值先触, exit_replay_backtest
   同约定); 峰值日内升高 → BE/锁盈线 ratchet; 冷却豁免同生产 (BE 已武装不放宽)。
   旧初始 SL 口径保留为 `overnight_delta_init_sl / settle_outcome_init_sl` 双列留档。
   era 字段缺失 (legacy 行) → fail-safe 回退初始 SL 口径并标 `sl_caliber=init_sl_legacy`,
   不静默套用当前 config。
3. **`--reset-settled` 通道**: 口径修复后清结算字段全量重放 (arm 行本身不动)。
4. **存量 4 笔回填重结算**: arm 时刻状态从 trade_records.metadata_json 重建
   (identity 锚: symbol + PRE_CLOSE_EXIT + entry_price±0.02, 4/4 唯一命中; 字段
   `era_backfilled=true` 留痕)。BA 按真实因子 `news_negative` (trailing=False →
   profit_override 分支) 重建 — 首轮回填误用 default 配置, 已修正重放。
   jsonl 原子重写, 备份 `winner_overnight_shadow.jsonl.bak-20261009`。
5. **注册表注记更新** (shadow_registry.py notes, 判据文/阈值/min_n 逐字未动)。
6. **测试**: 既有 W1-W7 11 例全绿 (legacy 回退保住原语义) + 新增 W8 4 例
   (方向翻转实证 / fail-safe 回退 / ratchet 语义 / reset 通道), 15 passed。
   触及面回归: exit_engine/winner_overnight/pre_close 相关 75 passed, 1 skipped。

## 4. 判据处置 (B16)

- 判据文/阈值**逐字未动**: n≥20 ∧ 均值>费用线(~0.15%) ∧ ≥60% 正 ∧ SL_touch<40%。
- 口径修复 = 对齐判据原文「带 SL 持一夜」本意 (RFC-095 同款: 修实现不修判据)。
- SL_touch 门按字面计**全部 SL 类出场** (SL_gap + SL_touch, 含锁盈线触发) — 门变严
  不变松, 防提前毕业; 锁盈出场细分只在 by_outcome 披露。若要把「锁盈触发」从尾部门
  剔出 = 改判据语义, 须另立 DEB + 用户裁, 本 RFC 不做。
- 既有 4 笔重结算读数入 B16 样本 (era_backfilled 留痕可审计), n 轴不清零 —
  arm 事实未变, 变的只是结算尺子。

## 5. 影响面 / 回滚

- 下游消费者: latest.json 仅结算器自己写读 (grep 实证无其他消费者); jsonl 新增字段
  向后兼容 (旧读方按 key 取值不受影响)。
- 生产退出链: **零行为变更** — 埋点在 PRE_CLOSE else 分支内的 record-only 块,
  KR2 冻结红线 (窗内禁改 PRE_CLOSE/EOD 链) 不触碰; exit 参数哈希不受影响
  (无新 config 键, era 字段落 arm 行不落 config)。
- 生效: 结算器为 cron 短命进程 (06:25), 下次运行即用新口径, 无需重启服务;
  exit_engine 埋点新字段随下次服务重启窗口生效 (盘后编辑纪律) — 重启前 arm 行
  无 era 字段 → fail-safe 走 legacy 口径并标记, 不产生错误读数。
- 回滚: `git revert` 本 commit 即回旧口径; 存量行旧读数在 `*_init_sl` 双列完整保留,
  任何时候可对照重算。

## 6. 未尽事项 / 保真度限制

- bar 缺失自动补拉 (结算器 cron 前置, ~20 行): 与本 RFC 独立, 仍留用户裁。
- 服务重启后首个 arm 行需回读验证 era 字段落档 (活体验证, TOO_EARLY 纪律:
  重启当日无 PRE_CLOSE 盈利仓 arm = 不可判, 非 DEAD)。
- **保真度限制 (如实披露)**: `_compute_dynamic_effective_sl` 内部有两个子分支读
  **当前** config 而非 arm 行 era 字段 — ① profit_override (仅 trailing=false 因子走,
  当前 threshold 0.8/dd 1.0) ② tighten_sl_by_beta (sl_tighten_beta=1.0 恒 no-op,
  4 个 arm era 实证均 1.0)。①对本批无实际影响: 4 笔中唯一 trailing=false 的 BA
  (9/29 arm) 其 era 值 == 当前值 (git 实证); 其余 3 笔 trailing=true 不走该分支
  (META 9/04 era 的 1.5/0.5 旧值不适用)。未来若 profit_override/sl_tighten_beta
  变更, 旧 era 行的这两个子分支会按新值重放 = 局部参数时代混算。变更这两键属
  退出参数裁决 (入 exit 哈希), 届时须评估是否把 era 值补落 arm 行 (一行改动)。
