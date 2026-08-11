---
title: UE 陈旧证据衰减(streak 硬闸) + UE→DA 供料管道 go/no-go 判据
date: 2026-08-11
status: 方案A已裁定 (2026-08-11 用户裁): 本窗口 8/12-8/24 纯 shadow 零代码,
  硬闸实施清单冻结于 §八.6, 8/24 与毛利记分卡同窗裁决后翻配置 (99→2) 即生效
type: L1 (~40 行代码 + 1 配置键)
related_code: scripts/llm_universe_scan.py, ~/.hermes/skills/deep-stock-analysis/scripts/pipeline.py
related_deb: DEB-20260808-signal-source-strategy
---

# UE 陈旧证据衰减 (streak 硬闸) + UE→DA 管道判据

## 一、背景与证据包 (全部来自真实落库, 2026-08-11 审计)

### UE 执行绩效 (playbook_instructions, decision_id LIKE 'universe_expansion:%')
- 80 条指令: 40 触发 / 42 执行 / 35 missed
- 执行 42 笔: 胜率 62% 但净 **-$23** (均笔 -$0.55) — 高胜率低质量
- 方向劈裂: SHORT 15 笔 12 胜 净 **+$140**; LONG 27 笔 净 **-$163**

### streak 前瞻切片 (score 落库; 口径按 §八 修正①②: 去重+剔埋点前, T+0 盘中窗口)
| 切片 | n | 命中率 (fwd≥1.0%) | 平均前瞻 |
|---|---|---|---|
| streak=1 (首日) | 66-67 | 52-53% | +2.72~2.80% |
| streak=2 | 6 | 33% | +0.58% |
| streak=3 | 2 | 0% | -0.31% |
| streak≥4 | 6 | 33% | +0.33% |

首日 ≫ 续期。注意: 原表"114/8/6, T+1口径"为错误口径 (含 7/14 重复评分 7 批 35 条 +
埋点前 streak=None 26 条 + 窗口实为 T+0 盘中 ~4.5h 而非 T+1), 已由 §八 修正, 判据一律
用本表口径。

### 个案
- TSM LONG: 同一 evidence_ref ("内部人集群买入") 连续浮现 8 天 (emergence_streak_days=8),
  streak≥2 的 7 天中 5 天前瞻 ≤ +0.46%, 多为负。
- ORCL: 代码注释自记 "连续 6 日空, 第 6 天 -$100 回吐" (llm_universe_scan.py:823)。
- missed 35 条的方向性 T+1 前瞻合计 +0.11 (含 PLTR +26%/GFF +15%) — 触发门逆向选择,
  另立条目, 本提案不动。

### UE→DA 供料管道现状 (pipeline.py _load_ue_short_candidates, 2026-08-03 落地)
- SHORT-only, cap 3/日, age>30h 拒绝, 开关 universe_expansion.da_candidate_feed
- 实测: 8/5 RSSS / 8/6 NTNX / 8/7 CMG / 8/8 WDC 各 +1 注入 (cap 非瓶颈)
- 结局: 3 只 DA 给 NEUTRAL 未出指令; 仅 CMG 出指令执行 +$3.45

## 二、主张拆解

### 主张①: UE→DA 管道维持现状, 不改代码, 注册退役判据
理由:
1. SHORT-only 与数据一致 (UE LONG 净 -$163; DA LONG 过量供给, DEB-20260808 §十四)
2. 吞吐 1/日非管道瓶颈, 是 UE scan SHORT 浮现本身少
3. 判据: origin=ue_candidate 的 DA 指令 n≥30 且净>0 → 维持; 否则退役

### 主张②: streak≥3 硬闸 (冻结, 8/24 裁决后视证据启用 — 方案A裁定)
规则: emergence_streak_days >= 3 的浮现不进任何执行通道; scan jsonl 照记
(score 继续评分, blocked 反事实前瞻照常积累 = 判据数据源)。

注入点 (仅两处消费端, scan 层不动, 量测不被污染):
1. scripts/llm_universe_scan.py cmd_inject() 候选过滤:
   `and (s.get("emergence_streak_days") or 1) <= max_streak`
2. skill pipeline.py _load_ue_short_candidates(): 同规则读同一 jsonl 字段

配置: unified_config.yaml 新增 `universe_expansion.max_emergence_streak: 2`
(允许复测一天, 第三天起闸; 设 99 = 全放行回滚)。

为什么不做 confidence 乘数衰减: 拍系数 = 魔数; 硬闸一行逻辑、可证伪、回滚干净。
符合"源头过滤 > 层层校验"与"删代码/简规则优先"。

## 三、预注册判据 (OOS: 2026-08-12 → 2026-08-24)

1. 被闸掉的 streak≥3 浮现, 用 score 反事实前瞻算账:
   方向性均值 ≤ 0 → 闸维持; 方向性均值 > +0.5% 且 n≥10 → 撤闸认错 (config 改 99)。
   **口径锁定 (Qoder 修正③, 已裁定)**: 只比 T+0 盘中窗口 (7/23 起 scored_at=ET13:40 批次),
   不回填长窗口混入; 按 (scan_date, symbol, direction) 去重; streak=None (埋点前) 不计 n。
2. 同期观察首日组 (streak≤2) 执行净贡献是否因供给净化上升。
3. 变更预算 (8/11 用户裁方案A): ② 本窗口**不上线** (纯 shadow, 零代码), 不占 A/B 预算,
   不与 VWAP A/B (ADR-021, 8/10-8/24) 争样本; ① 零改动不占预算;
   其余候选 (触发门/score rubric) 排队不插队。

**方案A代价 (已知晓并接受)**: streak≥3 续期单继续失血 ~2 周 ≈ -$25~45 (历史 5 笔/3 周
× -$8.5/笔, 全部 TSM 单标的) — 该失血本身即判据 1 的增量样本。

## 四、诚实声明 (样本与代价)

- streak≥3 干净样本 n=8 (去重+剔埋点前): 方向可靠、幅度不可靠; 执行侧 n=5 全是 TSM
  (单标的规则本质, 判据 1 正是要证伪/证实这一点)。
- 硬闸可能误杀"事件真实连日发酵"的续期单 (如持续升级的地缘题材) — 已知代价,
  判据 1 兜底; 若误杀显著, 备选方案是"要求增量 evidence_ref 才放行" (暂不做, 过度设计)。
- UE SHORT +$140 仅 15 笔, 不足以证明 SHORT 轴长期有 edge, 仅支持"当前不砍"。

## 五、不做 (防范围蔓延)

- 不动触发门 / entry_zone 逻辑 (missed 逆向选择另立条目, 先攒反事实)
- 不动 DA score rubric (按 DEB-20260808 预注册 n≥100 走)
- UE LONG 不接 DA 管道
- 不改 UE prompt (prompt 是另一变量, 不混入本实验)

## 六、辩论点 (供独立模型审查)

1. streak=3 起闸 vs streak=2 起闸: n=14 的切片数据是否足以定 3? (当前选 3 的理由:
   streak=2 仍是"复测确认"语义, 且样本最小, 先保守)
2. 硬闸 vs 衰减: 若事件真在发酵, 硬闸是否过于武断?
3. 两处消费端各写一行 vs 提取共享函数: 跨 repo (FTSeek vs skill scripts) 无法共享,
   两处一行是否可接受?
4. 判据 1 的"显著 > 0"如何量化? (建议: 方向性均值 > +0.5% 且 n≥10)

## 七、回滚

- 配置级: `max_emergence_streak: 99` (热改 YAML 即全放行)
- 代码级: git revert (纯新增过滤行, 无数据结构变更)

## 七b、裁决记录 (2026-08-11)

- Qoder §八 复核结论经 Hermes 独立复算逐条验证: 修正①②/8.3 执行侧表/SHORT 免疫/
  8.5 时机冲突 全部属实; 小偏差两处不影响结论 (回填批实为 T+0~T+8 非 T+1~T+9;
  streak=1 去重后 n=67 非 66, 边界差 1 条)。
- **用户裁定: 方案 A** — 8/12-8/24 纯 shadow, 零代码; 8/24 出裁决报告
  (口径: 判据 1 锁定 T+0 窗口+去重+剔 None; 通过 → 按 §八.6 清单翻配置上闸,
  不通过 → 撤案)。
- 8/24 裁决日同窗事项: 毛利记分卡 (DEB-20260808 §17.2-1) + VWAP A/B 台账复核。

---

## 八、独立复核 (Qoder, 2026-08-11, 用户令"给出建议和更新内容")

### 8.1 核实通过项 (全量逐点复核, 与提案一致)

- UE 执行绩效: 80 指令 / 40 触发 / 42 执行 ✓; 42 笔 WR=62% 净 **−$23.01** 均 −$0.55 ✓;
  SHORT 15 笔 12 胜 **+$140.14** / BUY 27 笔 14 胜 **−$163.15** ✓ (playbook_instructions 直查)
- missed 35 = 未触发 33 + thesis_drift 2 (另 3 条 admission SL_STOP_TOO_WIDE 单独归类) ✓
- TSM 轨迹: 7/30 重计首日 → 8/10 streak=8, 同 evidence_ref 连续浮现属实 ✓
- UE→DA 管道: SHORT-only / cap 3 / age>30h 拒绝 / `da_candidate_feed` 开关, 代码断言全 ✓;
  结局 RSSS/NTNX/WDC 无指令 (NEUTRAL) + CMG 出 SHORT 指令执行 +$3.45 ✓
- 配置键 `universe_expansion.max_emergence_streak` 放顶层段, 主仓 `_cfg`→get_dotted 与
  skill 侧 yaml.safe_load 两读取路径兼容 ✓ (不会重演 VWAP A/B 键层级空转事故)

### 8.2 口径修正 (3 处, 不改变方向但必须修正后才可作为判据基线)

**修正①: "T+1 方向口径"不成立 — 实为 T+0 盘中 ~4.5h 窗口。**
score 记录实证: 7/23 起全部 scored_at=UTC 17:40 (ET 13:40 **盘中**), scan 在 ET ~09:10 →
前瞻窗口实为当日上午→午后 4.5h, 非 T+1; 且 7/14-7/22 的回填批为 T+1~T+9 混合长窗口。
提案表中"T+1"应改述为 "T+0 盘中窗口 (scan→当日 13:40 ET) + 少量回填长窗口"。

**修正②: n=114/8/6 含重复计权与埋点前混入。**
jsonl 实证: 7/14 同一 scan 被重复评分 7 次 (35 条 results, 占 128 的 27%); streak 埋点
7/22 才上线, 此前 26 条无 streak 字段被默认并入"首日"桶。去重 (scan_date,symbol,direction)
+ 剔除 streak=None 后, 仅 T+0 窗口的干净切片:

| 切片 | n | 命中率 (fwd≥1.0%) | 平均前瞻 |
|---|---|---|---|
| streak=1 (首日) | 66 | 53% | **+2.80%** |
| streak=2 | 6 | 33% | +0.58% |
| streak=3 | 2 | 0% | −0.31% |
| streak≥4 | 6 | 33% | +0.33% |

方向结论 (首日 >> 续期) **修正后依然成立且更干净**, 但 streak≥3 证据 n=8 而非 14。

**修正③: 判据 1 的"显著>0"需加窗口限定** — 见 8.4 辩论点④回答。

### 8.3 关键补强证据 (提案未使用的更直接反事实)

执行侧 streak 分布 (raw_instruction_json.origin.emergence_streak_days 直 join, 42 笔):

| 分组 | n | 胜 | 净 PnL |
|---|---|---|---|
| streak=1 | 25 | 15 | **+$68.79** |
| streak=2 | 1 | 1 | +$5.65 |
| **streak≥3** | **5** | 3 | **−$42.47 (全是 TSM)** |
| streak=None (埋点前) | 11 | 7 | −$54.98 |

→ 若硬闸已生效, 直接避开 −$42.47 (UE 总净 −$23 的最大单因子)。
**且 SHORT 轴免疫**: 7/22 以来 SHORT 浮现 33 条 streak=1 + 1 条 streak=2, **从未 streak≥3**
→ 硬闸只砍 LONG 续期, 与 UE LONG −$163 证据链自洽, 不伤唯一盈利的 SHORT 轴与 UE→DA 管道。

### 8.4 辩论点回答

1. **streak=3 vs 2 起闸**: 选 3 正确。streak=2 的 score 切片 +0.58% 仍为正、执行侧 n=1 +$5.65,
   无拦截依据; streak=3 起闸的执行证据 n=5 全 TSM — 承认其**单标的规则本质**, 这正是
   预注册判据 1 要证伪的。但提案 §一表格把 2-3 合并 (n=8 +0.36%) 掩盖了 streak=2 为正的事实,
   建议按 8.2 修正②的分桶呈现。
2. **硬闸 vs 衰减**: 同意硬闸 (一行逻辑、可证伪、回滚干净); "事件真发酵"的误杀风险由
   判据 1 兜底, 且 SHORT 轴免疫 (8.3) 进一步收窄误伤面。
3. **两处一行**: 可接受 (跨 repo 无法共享是事实)。但 skill 侧 `_load_ue_short_candidates`
   当前**不传 streak 字段**, 需同时从 jsonl surfaced 读出并过滤 (一行变两行), 注释互指。
4. **判据 1 量化**: "方向性均值 > +0.5% 且 n≥10"合理, 但**必须限定统一窗口** — 只比
   T+0 盘中口径 (7/23 起), 不回填长窗口混入; 且按 (scan_date,symbol,direction) 去重后计 n。

### 8.5 时机冲突与两个方案 (最重要的一条意见)

本提案 §三-3 称"②为两周内唯一 A/B 变量" — **与现状冲突**: VWAP 门 A/B (ADR-021, 8/7 批准
上线) 正在 8/10-8/24 窗口采样 (台账已有 4 行), 每臂 n≥30 毕业目标远未达成; 且 DEB-20260808
§17.4 ⛔ 明确"入场侧再加闸"在不做清单、§17.5-2 变更预算"每两周最多 1 个 A/B 变量"。
streak 硬闸若 8/12 上线 = 同窗第二个行为变更, 挤占 VWAP A/B 样本池 (日均成交仅 6-8 笔)。

**方案 A (推荐): 本窗口纯 shadow, 零代码上线。**
判据 1 的数据采集**不依赖硬闸生效** — score 本就对全部浮现评分, 只需按 streak 分桶看
8/12-8/24 的 T+0 前瞻即可拿到同等判据。代价仅是多付 streak≥3 续期单的确定性失血
(历史频率 ~2-3 笔/周 × 均笔 −$8.5 ≈ 两周 −$40~50)。8/24 与毛利记分卡 (DEB-20260808 §17.2-1)
同窗裁决: 若续期组前瞻 ≤0 且执行继续失血 → 直接翻配置硬闸, 届时证据 n 也更大。

**方案 B (可接受但需声明): 立即上线硬闸。**
前提是用户明确裁定: ① streak 闸挤占 VWAP A/B 的两周预算 (VWAP 顺延或暂停); 或 ② 认定
两者作用位置正交 (streak 闸=UE 指令生成端, VWAP=入场确认端) 不构成归因纠缠 — 此辩护部分
成立 (UE 指令占 VWAP 样本比例小), 但 TSM 这类每日常客被闸后供给量下降会间接改变
VWAP 样本构成, 需在 ADF 周检中声明口径断点。

### 8.6 若获批实施的修正清单 (替代 §二-2 落地描述)

1. `scripts/llm_universe_scan.py` cmd_inject 候选过滤 (1097 行处) 加:
   `and (s.get("emergence_streak_days") or 1) <= _max_streak`,
   `_max_streak = int(_cfg("universe_expansion.max_emergence_streak", 99))`
   (默认 99=缺键零行为, 显式配 2 才起闸 — 比"新增即生效"更保守, 防配置未读到静默启闸)
2. skill `_load_ue_short_candidates`: out.append 加 `"ue_streak": s.get("emergence_streak_days")`
   并在 SHORT 循环加同规则过滤 (读同一 yaml 键, 默认 99)
3. yaml `universe_expansion:` 段新增 `max_emergence_streak: 99` (先 99 影子, 裁决后改 2)
4. 测试: streak=None/1/2/3 四态过滤行为 + 配置缺键默认不放行 各一例
5. 落档: 本提案 status 更新 + handoff 变更日志 + (若硬闸) ADF 周检声明口径断点
