---
title: 入场门张力 backlog — 触发带/VWAP/confidence 结构改进待办
status: active
module: playbook
related_code: app/playbook/entry_band.py, app/playbook/pipeline.py, scripts/eval_vwap_gate.py
related_okr: OKR-2026-07
tags: [playbook, entry, vwap, confidence, shadow, backlog]
updated: 2026-07-18
---

# 入场门张力 backlog

> 用途: 记录 2026-07-18 对"信号获取→筛选→标的范围→门阈值"深度梳理中发现的**结构性张力**及其
> 改进路径。这些**不是 bug**, 是口径/可解释性/哲学一致性问题, 不影响安全带兜底。
> 原则: 先测量再动刀; 能不改行为就先只提可解释性 (见 ponytail 原则)。

## 背景 (一次性梳理结论)

实盘走量信号几乎全来自 **DA-bridge (ET06:00) / UE 扫描 (ET08:10) 直写 DB**, 它们**绕过**
`_generate_instructions` 的投票/因子/技术仲裁/G6 confidence 门。盘中真正拦人的只有:
**触发带 (entry_band) + VWAP 方向门 + 敞口三闸 (单仓4%/日名义80%/日30笔) + vol_bucket SL 钳制**。
即 confidence/IC 在活路径近乎退化, 风控靠"安全带", 这与"未校准数据不作门"的理念一致。

## 已完成 (2026-07-18, commit 60afab7e)

- [x] **张力④ 去黑盒**: `conf<50 减半` → 命名 `_model_disagreement`, 注释点明是"2模型分歧"代理
  (DA 分歧写 conf=35 / 一致 ~70), 非低信心质量门。零行为变更。
- [x] **张力⑤ 显式化**: `_emit_exec_gate_audit` docstring 写明 G6 绕过是刻意设计 (未校准数据不作门)。
- [x] **VWAP 门离线评估工具**: `scripts/eval_vwap_gate.py` (纯只读), 用影子价格轨迹+真实SL/TP
  模拟被拦单假想盈亏。

## 待办 (按优先级)

### P0 — 攒样本 (零风险, 持续)
- [ ] **定期跑 `scripts/eval_vwap_gate.py` 攒样本到 30+ 事件再判定 VWAP 门去留。**
  - 现状 (7/15–7/17): 被拦 7 个去重事件, 放行假想盈亏 正4/负3 合计 +2.53% 均值 +0.36%
    → 方向性暗示"倾向拦错(扼杀)", 但样本过小不足以动刀。
  - 数据实锤: 触发带放行 202 单, VWAP 门否决 180 (89%); 被拦 66% 是 BUY (回调买),
    vwap_dev 中位 -1.28%, 仅 11% 擦边(<1%)被拦 → 容差 0.3% 可能偏紧。
  - 评估局限 (务必如实): 影子轨迹是离散采样非连续行情; 对照组(aligned)放行后变持仓不再被扫描
    → 退化为单点, 当前无法作对照。需补真实分钟行情回补才能得可靠对照组。

### P1 — VWAP 门行为调整 (等 P0 样本坐实后, 二选一)
- [ ] 放宽容差 `entry_vwap_gate_tolerance` 0.3%→~1% (config, 最小改动); 或
- [ ] **场景化**: "追入(突破)"场景加 VWAP 门, "回调买/反弹空"场景免 VWAP —— 直接消解
  张力② (触发带赌回归 vs VWAP 门赌动量的哲学冲突), 而非废掉整个门。
  - 关联代码: `pipeline.py` 触发判定 ~2914-2931 (回调/追入分支), VWAP 门 ~3010-3028。

### P2 — 触发带波动率化 (张力①, 中期, 方案现成)
- [ ] 让 `compute_entry_band` 用**已预留的 `volatility_pct` 入参** (entry_band.py:27) 按 vol_bucket
  缩放 band 半宽, 使触发带与 SL/TP **同源** (当前扁平 ±3%/5% 一刀切, 与分桶 SL 是两把尺子)。
  - 现状风险: 低波动股 band(±3%) 比其 SL(2%) 还宽, 靠"触发后 SL 越界检查"(pipeline.py:2969) 兜底。
  - 关联代码: `entry_band.py:24-53` (预留入参), `_compute_volatility_based_sl_tp` (分桶口径来源),
    触发调用处需把该股 atr_pct/桶传入。需影子验证缩放系数, 防高波动股 band 放太宽变"什么价都追"。

### P3 — 开盘 VWAP 盲区 (张力③, 已降级)
- [ ] 开盘 VWAP 缺失→fail-open 放行的方向盲区。**实测很小** (open 43 条仅 3 条 vwap_missing=7%),
  优先级低。若 P1 确认 VWAP 门有价值再补: 开盘用开盘价/昨收做临时方向锚, 或前 15min 收紧触发带。

### P? — confidence 校准 (远期, 理念前置)
- [ ] confidence 校准为正之前, **不**对 DA/UE 走量激活 G6 质量门 (用户理念: IC 是历史 legacy 数据,
  新统一管线需新实测)。持续用 `_emit_exec_gate_audit` + edge_gate 影子观测"G6 假设生效"的影响,
  样本充足且校准为正后再评估。

## 关键文件
- `app/playbook/entry_band.py` — 触发带单一真源 (:20-21 常量, :27 预留波动率入参)
- `app/playbook/pipeline.py` — 触发判定 (~2880-3056), VWAP 门 (~3010-3028), conf 减半 (~2555),
  G6 绕过审计 (`_emit_exec_gate_audit` ~2161), 影子写入 `_emit_entry_shadow` (~2784)
- `scripts/eval_vwap_gate.py` — VWAP 门离线评估 (只读)
- `data/entry_timing_shadow.jsonl` — 每次轮询的 gate 判定 + 价格采样
- `config/unified_config.yaml` — `entry_vwap_gate_tolerance` / `entry_vwap_gate_enabled`
