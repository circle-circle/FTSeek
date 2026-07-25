# 退出侧调优 — MFE/MAE 归因 × capture 影子 (数据锚点)

- **创建**: 2026-07-13
- **状态**: Executing (影子采集中, record-only)
- **原则**: 数据驱动 · 一次一变量 · 影子先行 · hard-to-reverse 先确认 · 度量用 pnl_pct/capture_ratio
- **关联代码**: `app/analysis/trade_attribution.py`、`app/playbook/exit_shadow.py`、`app/playbook/pipeline.py`(`_check_playbook_exit_rules`/`_compute_dynamic_effective_sl`/`_close_trade_record`)、`config/playbook_horizon.yaml`(`factor_exit_rules`)

---

## 0. 缘起 (实测发现, 非臆测)

### 0.1 MFE/MAE 记录不完整 + 碎片化
MFE/MAE 同时存在三处、单位不一:
- `trade_records.metadata_json.mfe_pct/mae_pct`(**%**, 盘中每~5min 轮询)+ `peak_price/trough_price`($)
- `playbook_instructions.max_favorable_excursion/mae`(**$**, shadow_tracker 用日线 t+1 回填)
- Position 对象属性(内存态)

实测(近90天/近81笔): `trade_records.mfe_pct>0` 仅 **2%**、`peak_price>0` **25%**;`playbook_instructions` MFE **42%**。盈利单本应必有正 MFE, 实测仅 29%~60% 有 → **记录曾断裂**。根因: 07-10 前 MFE 只在 `if not exit_reason` 分支累积, 触发退出那轮跳过(pipeline 自注 L2706-2711)。07-09 加采集 + 07-11 加 `trough_price`/量纲夹取后**新单已修复**(今日 KO/TSM/LLY 全有 mfe_pct+peak+trough)。历史无 intraday 缓存, 不可回填。

### 0.2 capture_ratio 分布揭示 giveback 问题
`capture_ratio = 已实现有利% / 最大浮盈%`。实测 n=55(有可信 MFE 的样本, 偏小、含修复前脏数据):
- MFE% 中位 **0.92%** / 均值 1.39% — 交易普遍摸到过 ~1% 浮盈
- **capture 中位仅 0.17**;**40% 盈利单浮盈变亏损**(cap<0);仅 35% 卖在峰值附近
- 结论: 问题**不在入场识别**(能摸到浮盈), 在**退出侧到手浮盈没锁住**

---

## 1. 已落地 (纯观测, 零改交易动作)

### 1.1 A — trough_price 接入归因
`trade_attribution.compute_attribution` 加 `trough_price` 参数, 对称 `peak_price` 补进 `all_lows`;
`_close_trade_record` 的 playbook_sim 归因调用处从 `metadata.trough_price` 传入。
**效果**: 入场时机(D2)/止损效率(D5)不再退化用 `min(entry,exit)`。单测: 同一 LONG 盈利单接入前误判「入场精准」, 接入真实 trough 后正确识别「入场过晚(差距2.0%)」。

### 1.2 B — capture 分布分析 (只读)
见 §0.2。作为退出侧调优的**现状基线**(样本偏小, 仅作假设不作定论)。

### 1.3 退出参数影子 `exit_param_shadow` (record-only)
- **模块** `app/playbook/exit_shadow.py`: 忠实复刻 `_compute_dynamic_effective_sl`(trailing 分支)纯逻辑, 只替换 `breakeven_at_pct`/`drawdown_from_peak_pct`。
- **变体**: `baseline_be1.0_dd0.5`(对照=现行 default)、`be0.5_dd0.4`、`be0.3_dd0.3`(贴合 MFE 中位 ~0.9%)。
- **忠实性边界**: **只测比现行更紧的变体** —— 更紧变体必在实盘退出前触发, 反事实退出价落在已观测价格路径内可评估;更松的会骑过实盘退出点、路径不可观测, 故排除。
- **接线**: ① 巡检循环每轮回放各变体、记首次触发到 `meta._exit_shadow`(仿 `invalidation_shadow`);② `_close_trade_record` 对比各变体 vs 实盘 capture, 落 `data/shadow/exit_param_shadow.jsonl`。
- **安全**: 全入口 try/except, 异常吞掉, 绝不影响实盘退出;feature flag `exit_param_shadow`(默认 on, 随时可关)。
- **单测验证**(giveback 场景 peak+0.9% 反转成 -1%): baseline(be=1.0 从不触发)capture **-1.11**; be0.5 锁 **+0.50%**(0.55); be0.3 锁 **+0.60%**(0.66)。即更紧 breakeven 把亏损单变锁利单。

---

## 2. 报告 / 推送 (关注数据变化)
- **脚本** `scripts/exit_shadow_report.py`(只读): 聚合各变体 capture 中位/均值 + 相对实盘的 uplift, 附样本量门槛。
- **cron**(拟): `0 6 * * 2-6`(盘前 ET06:00)`--push` 飞书, 与 `sltp_shadow_pnl_report` 同族。
- **读法**: 关注「某变体 capture 中位是否稳定 > 实盘/baseline」+ 样本量是否够(建议 ≥20 已平仓且有正 MFE 的单)。

---

## 3. Go-forward 门槛 (调参前必须满足)
1. 积累 **2-4 周**新单(记录完整度已修)`exit_param_shadow.jsonl`。
2. 某变体 capture 中位**稳定优于** baseline 且样本 ≥20 → 才提议改 `playbook_horizon.yaml` 的 `breakeven_at_pct`/`drawdown_from_peak_pct`。
3. 改配置属 **hard-to-reverse 实盘参数变更** → 先与用户确认、一次一变量、可回滚(yaml 单点、保留原值注释)。
4. **不得**用 §0.2 的历史小样本(含修复前脏数据)直接调参 = 避免「用旧数据审判新策略」。

---

## 4. 回滚
- 影子: flag `exit_param_shadow=false` 即停(秒级)。
- trough 接入: 归因是派生显示, 无副作用;如需回退删参数即可。
- 未改任何实盘退出参数(`breakeven_at_pct=1.0`/`dd=0.5` 保持现行)。

---

## 5. 验收锚点
- 每周复跑 `exit_shadow_report.py` 看 capture 分布迁移。
- 记录完整度: 新平仓单 `metadata.mfe_pct/peak_price/trough_price` 应 ~100% 有值(抽查)。
- 达门槛后另起「退出参数调参」执行阶段(独立 spec 或本 spec §6 追加)。
