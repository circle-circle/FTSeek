# RFC-118: 入场价机制升级 — 可达性门 + 回退市价 (限价路由 2.0) 与配套源头治理

> **状态**: 影子已实施 (2026-09-30 用户裁「按建议执行」; record-only forward 攒样中, 接线另裁)
> **日期**: 2026-09-30
> **作者**: Hermes (qwen3.8-max, 主持+第4辩手) + 3×glm-5.2 子代理 (反幻觉/架构/实证, 两轮交叉证伪)
> **审批人**: 用户 (裁决日按 forward 冻结值提请)
> **关联**: ADR-035 (限价路由影子+§五.2 C3分层判读规则), RFC-081 (§六 分流人群+误杀口径冻结), DEB-20260901 (archetype 立项), DEB-20260806 (TTL 证据)
> **辩论转录**: docs/debates/DEB-20260930-entry-price-mechanism/ (00-问题陈述 + R1×4 + R2-交叉证伪收敛)

---

## 摘要

D 案 (确认位限价路由) 配对样本已达裁决门槛 (n=86≥40), 但按 ADR-035 §五.2 预注册判读规则, C3 失败 (成交率 0.317<0.5) 的归因是 **DA 定价不可达** (可达桶 fill 50% vs 不可达桶 0%), 不构成否掉路由的证据; 裁决对象升级为"路由接线 + 可达性门"打包提案。本 RFC 即该打包提案: **触发时刻按 |确认位−触发价|/ATR 做可达性门, 门内挂限价 (原 ADR-035 状态机不动), 门外回退现状市价 (绝不跳过、绝不降级挂单)**。离线双臂同尺重放显示 θ=0.5 腿每笔 +$8.14 (+15.1bp), 越过费用线缺口 (~6.9bp/笔), 把 D 案基线 (+$5.79/笔, 统计不可分辨) 翻为点估计优于现状; 但日聚类 t=1.92 仍不可分辨且门内过期误杀未修, **须 forward 影子攒样至预注册判据过门后才提请接线裁决, 本 RFC 批准的是影子阶段, 不是接线**。

---

## 问题陈述

### 用户症状 → 数据对应 (2026-09, n=180, 全部 Hermes 实算可复现)

| 用户症状 | 数据实测 | 真实病灶 |
|---|---|---|
| "MFE 捕获率不高" | 捕获率 median 0.50; <30% 捕获占 23% | 捕获率是幸存者问题 — **死亡族 (mfe<0.3%) 20% 笔数净 -$3529 根本没有 MFE 可捕获**; 翻绿族 +$2857 |
| "赢小输大" | 中位赢 $12 vs 中位输 $84; 胜率 75% 期望 -$3.8/笔 | **费/笔 $7.93 > 毛/笔 $4.19 (费用=毛的 189%)**; 现状毛利已有 7.8bp, 费 14.7bp → **打平缺口 = 6.9bp/笔** (增量口径; DEB-20260930 R2 认账修正, 初稿误写 15bp=全费负担口径) |
| "成交后价格有偏离" | 执行滑点 median 仅 -0.026% (小); DA 锚偏离 median -0.52%, 62% 超 0.5%; D案确认位 >0.5ATR 的单 fill=0% | 病不在执行滑点, 在**入场锚的选择**: DA 确认位按日线支撑思维定价, 但限价单只活 5 分钟 — 物理不可达 |

### D 案影子现状 (窗 2026-09-08→09-29, n_paired=86)

- 四判据: C1✅(n=86≥40) C2✅(+89%) **C3❌(fill 0.317<0.5) C4❌(mfe0 占比 cf 0.051 vs actual 0.0)** → 读数卡结论"维持关闭"
- C3 分层 (ADR-035 §五.2 预注册桶界): <0.25ATR fill **74.4%** | 0.25-0.5ATR fill 25.6% | >0.5ATR fill **0%** (57/57 全到期作废)
- 误杀 (expired 口径冻结): 58.3% ❌ 门 (≤40%), 放弃净利 +$726 — 其中远距 (>0.5ATR) expired 赢家 23 笔 $699 = **挂太远把好单饿死**, "躲烂单"叙事不成立
- 两臂 MFE 对照 (n=31): 限价臂 mean MFE **0.620% vs 市价臂 0.465% (+15.5bp)** — 恰好是费用线量级, 证明"更低的入场价"方向本身有效
- ATR 口径: real 22 / proxy 65 / unknown 52 → 距离尺子一半是估的, 分层读数降级方向性

### 为什么不能"直接接线 D 案"也不能"直接否掉 D 案"

- 直接接线: C3/C4 两判据 FAIL, 违反 ADR-035 预注册"四条全过才谈接线"
- 直接否掉: 违反 §五.2 规则 2 — C3 失败归因=DA 定价不可达时, "不构成否掉路由的证据"
- **预注册的唯一出路**: "路由接线 + 可达性门"打包提案, 可达性门走完整提案流程 = 本 RFC

---

## 提议方案

### 方案概述 (H1: 可达性门 + 回退市价)

触发时刻 (pipeline re-price 后, 实时价与当日 ATR 已知) 计算:

```
dist = |确认位 − 触发价| / ATR
dist ≤ θ_reach  → 挂限价单 @确认位, TTL 300s, 过期放弃 (ADR-035 状态机原样, 绝不降级市价)
dist > θ_reach  → 不挂单不跳过, 回退现状市价路径 (等价"无有效确认位")
```

- **θ_reach 双腿并行攒样** (0.25 与 0.5, 均为预注册桶界非事后挑选; 复用 D1' 双阈值先例), forward 窗内**禁止选边**, 到裁决日两腿各自过判据才呈裁
- 回退语义 = 最坏情况等于现状 (门外单本来就是市价成交的), 结构性消除"远距挂单饿死 $699 赢家"
- BREAKOUT / 无 archetype / 无确认位 → 原样市价, 零变化 (ADR-035 路由域不变)

### 离线预验证 (双臂同尺, L1a 许可口径; in-sample 只作立项地板)

模拟规则在 v2_latest rows 上逐笔重放 (`dist≤θ→限价臂 cf_net, 过期=0; dist>θ→市价臂 act_replay_net`):

| 臂 | paired n | 差/笔 | fill | 门内过期误杀 | 放弃经济(同尺) | 日聚类 t (16天) |
|---|---|---|---|---|---|---|
| 纯D案基线 | 86 | +$5.79 | 0.317 | — | +726(DB口径) | 0.90 |
| θ=0.25 | 87 | +$2.62 (4.8bp) | 0.706 | 8/10 | +$160 | 1.07 |
| θ=0.5 | 87 | **+$8.14 (15.1bp)** | 0.500 | 23/31 | +$399 | 1.92 |

关键读数: ①θ=0.5 腿每笔 +15.1bp, 越过打平缺口 6.9bp 一倍以上; ②**θ=0.25 腿反而更差** — fill 高 ≠ 净贡献高 (0.25-0.5ATR 桶的限价成交单是净贡献者), 天真按"fill 最高的桶"选 θ 会选错, 这是双腿并行的实证理由; ③t=1.92 < df15 临界 2.131, 仍不可分辨 ⇒ 只够立影子不够接线; ④门内 C3 按官方冻结分母 (entered/(entered+expired) 路由腿口径, 与 D 案 0.317 同源): **θ=0.25 腿 0.737 ✅ / θ=0.5 腿 0.494 ❌ (差 1 行到线, 边界敏感 in-sample 抠无意义)**; ⑤门内过期误杀 23/31 未修 — H1 修"门外饿死"不修"门内过期", 误杀判据可能仍 FAIL, 呈裁时三读数同屏 (RFC-081 §六盲区3)。

### 配套提案 (H3: DA 源头治理, 双轨同批)

- instruction_generator 生成时校验 `|price_level − 快照价|/atr_pct > θ_gen(0.5)` → **confirmation 置 null** (=按市价语义处理)。不夹位 (夹位=伪造 DA 意图, 把低吸改追高=ADR-035 原病), 不降信心 (LLM 遵守不可验证)
- prompt 同步加可达性要求 (双轨: prompt 治意图, 代码治兜底), 影子对比遵守率
- H4 (度量层): 所有入场类影子结算统一加 `bp_per_trade_vs_market` 读数行, 与当月费用线同屏 — 只加读数不加判据

### 被否方向 (留痕防复活)

| 方向 | 死因 |
|---|---|
| 延长 TTL | DEB-20260806 证据反向: 5min Δ+416.8 vs 30min Δ-224.4; 且拖长=资金挂死 |
| 全局限价化 (含 BREAKOUT) | BREAKOUT 语义=市价追突破, 限价化=自相矛盾 |
| 按时间窗禁入 (开盘档) | C8 前科: 9点档 n=134 中死亡族仅 17 笔, 按时间拦误杀 87% 人群, 无区分度 |
| 远价"夹"到近处照挂 | 伪造 DA 意图, 低吸单变追高单 = ADR-035 立项要治的原病 |
| θ=0.25 单腿直接上 | 离线预验证显示净贡献反而更低 (+2.62<+5.79 基线) |

### 影响范围

- 服务焦点: 入场执行链 (playbook pipeline → entry_routing → playbook_service 下单)
- 改动指标: 每笔毛利 (θ=0.5 腿离线 +15.1bp, 目标 ≥6.9bp 打平缺口), C3 fill, 误杀率, 放弃经济
- 影子阶段零交易影响 (record-only); 接线阶段 (未来另裁) 影响 PULLBACK_LIMIT/FADE_SHORT 指令的下单方式

### 详细设计 (接线点, 架构方 R1 已核实行号)

1. `app/playbook/entry_routing.py`: 新增纯函数 `reachability_gate(level, market_price, atr_pct, theta) -> bool` (与 resolve_route 同文件同风格, 无 IO); ATR 缺失 → fail-open 回退市价 (不路由), 与 `_MAX_LEVEL_DEV` 保守语义一致
2. `app/playbook/pipeline.py`: 两个候选接线点 (R2 选定后者 = 保守替代): ①L4254 `resolve_route()` 调用处扩签名传 atr_pct — 但 ATR 在 L4279-4290 才算出, 需重排计算顺序, 侵入大; ②**L4272-4277 路由消费点追加复核** — 此时 `_atr`/`_new_entry` 均已就绪, `|limit_price − _new_entry| / (_atr × _new_entry) > θ` → 覆写 `_arch_route = None` (自然回落现状市价路径, 零新分支) + 遥测 `level_reachability` 字段进 reprice_audit (L4543 merge_instruction_metadata 同处, 零新调用点)
3. `scripts/cf_pullback_limit_replay.py`: 加第三臂 `gated_arm` (同轴同文件, 不新建影子载体), 输出 G1-G6 读数 + `bp_per_trade_vs_market` 行 (H4/F6)
4. `app/playbook/instruction_generator.py` (H3): 生成尾部校验确认位可达性, 超限置 confirmation=null + 留痕字段; prompt 同步加可达性要求 (双轨, 影子对比遵守率)
5. 画像分层 (架构4保守版): metadata_json 只记客观特征 (is_open_window/anchor_dev_pct/vol_bucket/regime), **不算 risk_score** (权重无据, 防看数据凑权重); 消费者 = gated_arm 影子分层
6. ATR 喂数治理 (前置工程项): 影子窗 atr_basis real 仅 22/139, 距离尺子不准则门判不准 — 生产路径 re-price 时刻 `_atr` 是实时算的 (real), 该风险主要影响影子读数分层, 行级 atr_basis 标注沿用

---

## 预注册判据 (forward 影子, 窗起点=接线日, 冻结后不得挪)

| # | 判据 | 门 | 口径 |
|---|---|---|---|
| G1 | 门后配对样本 | n≥40 (每腿独立计) | 双臂同尺 paired 群 |
| G2 | 门后 C3 fill | ≥50% | 门内单成交率 (门外回退不计入分母) |
| G3 | 每笔改善 | ≥ +6.9bp (费用线缺口, 按 forward 窗当月费/笔动态计) | paired_diff mean, 日聚类同屏 |
| G4 | 日聚类 | \|t\|≥2 且按 df 校正临界复核 | per-day mean canonical 口径 |
| G5 | 误杀 (expired) | ≤40% (RFC-081 §六冻结分母) | 门内过期口径 |
| G6 | 放弃经济 | ≤0 (不再净放走钱) | 同尺口径 |

裁决规则: **双腿各自独立过全部判据才呈裁; 一腿过一腿不过 → 呈裁过腿并披露另一腿读数; 都不过 → 影子退役留痕**。任一窗内改 θ = 重新裁决。

## 风险评估

- **最坏情况**: 影子阶段零风险 (record-only); 若未来接线后门判错 (ATR 噪声), 门外单回退市价=现状, 门内单与 D 案相同 — 下行有界
- **缓解**: ATR unknown 治理列为前置; 影子行级继续标 atr_basis; real 不主导时分层降级方向性 (ADR-035 诚实披露条款沿用)
- **残留风险**: 门内过期误杀 (23/31) H1 未修 — 若 G5 持续 FAIL, 需另立"门内 TTL 复核价"提案 (不在本 RFC 范围)

## 验证计划

```bash
# 影子阶段 (本 RFC 批准范围)
1. cf_pullback_limit_replay.py 加 gated_arm 后跑窗模式: 复现上表离线读数 (等基)
2. 契约测试: reachability_gate 纯函数 (θ边界/ATR缺失→fail-open回退市价/方向对称)
3. forward 攒样 ≥3 周, 日报 (dac32b02d402 job) 加 gated_arm 读数行 (展示层, 判据零改动)
4. 到 G1-G6 全过 → 提请接线裁决 (另走 RFC, 翻 entry_archetype_routing_enabled=true 需用户裁)
```

## 回滚计划

```bash
git revert <shadow-commit>   # 影子阶段: 删 gated_arm 读数, 生产路径零触碰
# 接线阶段 (未来): config entry_archetype_routing_enabled=false + 重启 (entry_routing 返回全 None, 等价不存在)
```

**回滚触发条件**: 影子产物停更 >26h (推送陈旧警告已有); forward 窗内发现 dist 计算前视 (用了触发时刻不可知的 ATR) → 立即停影子复核

## 审批

- [x] 5 问自检 (能更赚钱吗: 15.1bp>6.9bp 缺口, 离线证据; 更简单方案: 一个纯函数+一个分支, 复用 ADR-035 状态机; 辩论结论可以是不做: R2 若证伪离线读数则本 RFC 自动作废)
- [x] 多agent辩论 R2 收敛 (DEB-20260930: 4方19提案→6族, F1双腿并行采纳/F4拆出范围/F5降级/VWAP降级, 认账2处口径错误, 详见 R2-cross-falsification.md)
- [x] 测试覆盖 (契约钉: 臂语义/fail-open/θ双腿forward分离/adr035_criteria 判据零改动, tests/test_cf_pullback_limit_replay_v2.py 48 passed)
- [x] 审批人签字 — 影子阶段 (2026-09-30 用户「按建议执行」; 接线阶段另裁)

## 实施记录

**影子阶段实施日期**: 2026-09-30 (用户裁「按建议执行」= 批准影子阶段; 接线仍另裁)
**Commit**: 见 git log [RFC-118] 影子实施提交
**实施内容 (全部 record-only, 生产路径零触碰)**:
1. `scripts/cf_pullback_limit_replay.py`: `_rfc118_gate_sim()` 模拟臂 (θ=0.25/0.5 双腿 × forward≥2026-09-30 / 参照窗分层; 门内限价过期=放弃, 门外+ATR缺失 fail-open 回退市价; G2 官方分母 / G5 RFC-081 冻结分母 / G6 双口径) + 行级 `notional` 字段 (bp 换算分母) + summary `rfc118_gate_sim` 块 + 读数卡两行观测 (只印不判)
2. `ops/hermes/entry_delay_shadow_push.py`: 日报加 RFC118 双腿 forward 攒样行 + G3 门槛 (近30d PLAYBOOK_SIM 费用缺口 bp, DB 动态算) — 生产镜像 sha256 同步
3. `tests/test_cf_pullback_limit_replay_v2.py`: +4 契约钉 (臂语义/fail-open与人群过滤/θ双腿与forward分离/summary+读数卡接线+adr035_criteria 判据零改动钉), 48 passed
4. 台账 `docs/CONTROL-LOOP-LEDGER.md`: crontab-cf-pullback-window 行 + dac32b02d402 行扩展留痕

**验证结果**: ①窗模式实跑 rc=0, 参照窗读数与 R1 离线预验证逐字吻合 (θ0.25 +2.62$/4.8bp/fill0.706, θ0.5 +8.14$/15.0bp/fill0.5); ②forward 腿 n=0 (窗起点 2026-09-30, 明日 cron 起攒样, 按 9 月节奏 ~4笔/日 → n≥40 约需 3-4 周); ③推送脚本实跑输出正常, G3 门槛动态值 8.3bp; ④守卫+契约测试 89 passed (test_ops_scripts_sync 3 + control_loop_ledger 10 + cf_pullback_v2 48 + entry_archetype_routing 21+7); ⑤`adr035_criteria` 仍恰为 C1-C4 四条 (契约钉锁死, 判据零改动)

**2026-09-30 复核修正 (观测口径, 判据零改动)**:
1. G1 经济读数改用 `v2.paired && actual_net 非空` 过滤, 未结算实盘重放行不再进入配对样本; 参照窗修正为 θ0.25 n=86/+$2.65/4.9bp, θ0.5 n=86/+$8.23/15.2bp。
2. G2 改按全部门内 `entered+expired` 路由腿计算, 不要求经济配对; 参照窗官方 fill 为 θ0.25 29/39=0.744, θ0.5 39/78=0.500。
3. G5 按 RFC-081 冻结语义改为全部门内 `expired_unfilled` 分母, `actual_net>0` 为误杀; 参照窗 θ0.25 9/10=0.900, θ0.5 26/39=0.667 (披露 `n_actual_known`)。
4. G4 输出补 `p_two_sided`, `distinguishable` 同时要求 `|t|≥2` 且双侧 p≤0.05 (df=n_days-1); 参照窗 θ0.5 t=1.88/p=0.0799, 仍不可分辨。
5. 日报 G3 标签改为“近30d全PLAYBOOK_SIM费用缺口”, 明示该参照门槛与 G1 配对人群不同尺; 判据与阈值均未改变。

**注**: 接线阶段 (pipeline L4272 消费点 gate + entry_routing.reachability_gate 纯函数 + instruction_generator H3 源头校验) 按本 RFC「详细设计」节待 forward 判据过门后另走裁决, 本次未实施。
