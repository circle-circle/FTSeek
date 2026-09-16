# ADR-035: 指令-触发语义对齐 (entry_archetype 结构化字段) — 影子先行

**日期**: 2026-09-01
**状态**: 已采纳 (用户裁决 "批准选项1", 承接 DEB-20260831 SL_INITIAL/mfe=0 溯源 →
DEB-20260901 立案, 选项 1/2/3 见 `docs/debates/DEB-20260901-entry-archetype-alignment.md` §九)
**关联**: DEB-20260831 (溯源实锤) / ADR-033 (影子先行同款纪律) / C1 退出参数冻结至 9/30

## 背景

8/31 盘后全链复盘实锤 **指令-触发语义倒挂**: DA 侧 thesis 写"低吸限价单"
(确认位/分批/禁止抢筹), 但执行层只认宽区间一次市价 — re-price 重锚定 +
方向穿越, 低吸单被跑成市价追单。三案例: NOW 实盘成交 $145.09 比指令确认位
$140.50 高 **+3.26%**; AVGO/AFRM 同型。条件语义 100% 是装饰文本。

独立复核 390 笔 (6/27–8/31 回填): conf=0.7 LONG 胜率 60.2% 但净 **-286**
(赢 +49.2 / 亏 -81.4) → 胜率非瓶颈, 左尾是; 开盘窗口高开 0-4% 是唯一正期望
单元 (+594), 平开/低开多头 -1092、追 4%+ 缺口 -479。既有证据: 确认位重放
5 笔 -1412→-796 (减亏 44%); 锚限价影子 5 分钟 TTL Δ+416.8/108 对。

## 决策 (选项 1: 结构化字段 + 影子先行, 零行为变更)

**Phase A — 语义结构化 (DA 侧, 仓外 skills 目录, 备份 .bak-deb20260901)**:
execution_plan schema 增 `entry_archetype` / `confirmation` / `chase_cap_price`,
adapter 透传, pipeline `_derive_entry_semantics()` LLM 优先 + 文本确定性兜底。
8/31 全 7 候选解析实测正确 (NOW=140.5 / AVGO cap=370 / BABA cap=118 /
EIX=FADE_SHORT 63.27 / BWXT 试多句式 / AFRM=83.5)。FTSeek 侧 Instruction
dataclass +3 字段, bridge→vote→Instruction→raw_instruction_json 全链透传落库。

**Phase B — 影子 (record-only, 本 ADR 启用的唯一开关)**:
- `playbook.pullback_limit_shadow_enabled: true` — 带 entry_archetype 的指令
  触发时落 `data/shadow/pullback_limit_shadow.jsonl` (trigger/chase_cap_violation),
  re-price 越过 chase_cap 记违例遥测。**不改任何执行路径**。
- `scripts/cf_pullback_limit_replay.py` (cron 47 5 * * 2-6): 次日重放判
  would-fill / CF-PnL, 对齐确认位反事实尺子。
- `scripts/gap_alignment_shadow.py` (cron 52 5 * * 2-6): 开盘缺口对齐影子门,
  已回填 6/27–8/31 共 390 笔。
- `scripts/limit_entry_shadow_replay.py`: 哨兵两侧同按 (inst_id,日) 去重,
  修复重启重复根因 → reconciliation_ok 由 false 转 true。

**预注册判据 (四阶段影子, 全过才谈执行层接线)**: n≥20 / 期望改善≥30% /
成交率≥50% / mfe=0 占比降, 三条全过。首批影子判读 2026-09-02 05:47。

## 回滚

`pullback_limit_shadow_enabled: false` + 重启 (影子停采, 字段透传无害留存);
彻底回退 = revert 本提交 + 仓外 skills 从 .bak-deb20260901 恢复。

## 验证

全量 2206 passed / 2 failed (两失败均为存量: Hermes job 内嵌 SQL 哨兵 +
ops 脚本同步哨兵, 非本批引入)。重启冒烟 7 条断言全绿零 Traceback。

## 追加 (2026-09-01, 用户裁"同意"): 选项1② 弹药先行 — 执行路由代码默认关闭入库

用户追问"是否可直接应用进 sim", 裁决讨论结论: **不盲切, 但把等待成本降到零** —
执行层路由代码先写好藏在默认关闭开关后, 影子判据一过即 `true`+重启进 sim, 零再开发延迟。

**新增 (开关 `playbook.entry_archetype_routing_enabled: false`, 默认关闭)**:
- `app/playbook/entry_routing.py` — 纯函数路由解析: `PULLBACK_LIMIT`/`FADE_SHORT`
  + 有效 `confirmation.price_level` → 确认位限价路由; `BREAKOUT`/无字段/偏离现价
  >25%/价位与禁追线矛盾 → 现状或终态跳过。TTL = min(配置 300s, DA ttl_min×60)。
- `app/playbook/playbook_service.py` — 路由指令以 `OrderType.LIMIT@确认位` 入场;
  `_handle_routed_fill_wait` TTL 状态机: 全成→按 dealt_avg_price 真价记账 /
  到期部分成交→撤余单按已成交量记账 / 到期零成交→撤单+竞态复查+
  `skip_reason=archetype_ttl_expired` 终态 / 订单被拒→`archetype_order_dead` 终态。
  **绝不降级市价** (消除"低吸变追高"机制根因)。真相源=order_list_query,
  与入场确认窗/平仓限价腿同源。
- `app/execution/dual_broker.py::cancel_order` — 撤单 API (尽力而为, 竞态由调用方复查)。
- `app/playbook/pipeline.py` — re-price 处路由决策 (SL/TP/sizing 锚确认位);
  三种终态跳过并入 `INVALIDATED_SKIP_REASONS` 防重触发; TTL 过期不计执行失败。
- 阻塞边界声明: 路由单同步等待上限 = TTL (300s, 与 auto_execute 5min 节拍同量级);
  退出监控为独立 20s 快循环, 不受牵连。

**验证**: 契约测试 21 例 (`tests/test_entry_archetype_routing.py`: 路由判定 11 +
下单构造 2 + 状态机 5 + 默认值 3) + 定向回归 191 过 + 全量 2227 passed/2 存量失败。

**启用门**: 影子判据 (首批判读 9/2 05:47) 三条全过 → 用户裁决 → 开关翻 `true`。
未过之前任何翻 true 均违反本 ADR 预注册。

## 追加 (2026-09-16, 用户裁"要"): 五.2 C3 可达性分层判读框架 — 裁决解释规则预注册

**背景**: 9/8-9/15 窗 (配对 n=24) 实测发现 C3 (成交率≥50%) 的失败可能不是
"限价机制成交能力不足", 而是"DA 确认位定价物理不可达"。按 |确认位−触发价|/ATR%
分桶实测: dist<0.25ATR 成交率 73% (8/11), 0.25-0.5ATR 25% (3/12), ≥0.5ATR 全部 0%
(0/12)。两个不同病因的修法完全不同: 前者才需要动路由机制, 后者修指令输入质量
(DA 侧可达性校验 + pipeline 触发时校验), 与路由接线与否无关。

**预注册判读规则 (裁决日 n≥40 时生效, 本批注在 n=24 时点写入, 桶界锁定后不得再调)**:

1. **桶界锁定**: 可达 = `reach_dist_atr ≤ 0.5` (replay 脚本 `c3_reachability_split`
   字段, 参考价优先级 shadow_trigger > actual_fill > instruction_anchor, 逐行标注
   `reach_ref_source`)。0.5 这个界来自机制假设 (TTL 5min ≈ 半个日ATR 的价格旅程
   是限价单可等待的量级), **不因当前读数 47.8% 略低于 50% 而事后收紧到 0.25**
   — 那是挪门。
2. **C3 分层裁决**: 到点裁决时 C3 若 FAIL, 必须先看分层:
   - 可达桶 fill_rate ≥ 50% 且不可达桶 ≈ 0% ⇒ C3 失败归因 = **DA 定价不可达**,
     不构成否掉路由的证据; 裁决对象升级为"路由接线 + 可达性门"打包提案
     (可达性门 = DA 侧 ②校验 + pipeline 触发时 ③校验, 另行走完整提案流程)。
   - 可达桶 fill_rate 也 < 50% ⇒ C3 失败归因 = **限价机制本身成交能力不足**,
     按原预注册判据处理 (不接线或改机制)。
3. **诚实披露 (写入时点的读数)**: 当前窗可达桶 fill_rate = 47.8% (11/23),
   距 50% 一线; 不可达桶 = 0%。0.5ATR 界不是干净分界 — 内部 [0,0.25) 73% vs
   [0.25,0.5) 25%。裁决日以 n≥40 窗的分层读数为准, 本批注只锁"怎么读", 不锁"读成什么"。
4. **C1/C2/C4 不受本批注影响** — 只给 C3 增加归因维度, 四判据结构、门槛、
   生效日纪律 (MONEY_CRITERIA_BINDING_FROM=2026-09-08) 全部原样。

**配套代码 (同批, record-only 零行为变更)**: `scripts/cf_pullback_limit_replay.py`
+`_reachability()` 纯函数 / 行级 `reach_dist_atr|reach_dev_pct|reach_ref_source`
三字段 / summary `c3_reachability_split` / 读数卡 C3分层一行。
测试 fixture 对齐生产 schema (+atr_pct 列)。51 passed。

## 追加 (2026-09-16 第二批, 用户裁「按照建议执行」): 宪章第9律读数 + C3子桶 + ATR口径 + n门槛统一 — 均为读数维度, 判据数值/门槛零改动

**① 日聚类 t + MDE(80%) (宪章第9律, 裁决日必读)**: `summarize_v2` 新增
`paired_diff_day_clustered` 块 + `day_pair_counts` (单日集中度披露); 读数卡加一行。
口径与 `scripts/exit_scorecard.py` 完全一致: 先按日等权取均值, 再对日均值做单样本 t,
`|t|≥2` 才算与 0 可分辨; n_days<3 或零方差 → None (不可判, 不得当 0 或 PASS)。
**动机 (本窗实算)**: pooled 中位 −$2.30 (p=0.968) vs 日聚类 day_mean +$11.18
(t=1.28, n_days=6, MDE(80%)=±$24.56) — 两种读法连符号都不稳定, 且 9/10 单日占
24 笔配对中的 10 笔 (42%)。**同日配对高度相关, pooled 符号检验不得单独作为裁决依据**。

**② C3 子桶读数 (预注册, 桶界与门槛均不动)**: `c3_reachability_split` 新增
`sub_bucket_lt_025` / `sub_bucket_025_to_05`。实测 fill 随距离单调递减:
<0.25ATR **72.7%** (8/11) / 0.25–0.5ATR **25%** (3/12) / ≥0.5 **0%** — 规则2的二值
归因 ("可达桶<50% ⇒ 机制不足") 会把距离梯度误读成机制整体不足。**裁决日若可达桶
聚合 <50% 而 <0.25 子桶 ≥50%, 归因叙事应读作"距离梯度"**, 可达性门阈值提案
(0.25 vs 0.5) 以子桶读数为数据依据 — 读数可后加, 判据不可挪。

**③ ATR 口径披露**: 行级新增 `atr_basis` (来源 trade_records.metadata_json),
summary `atr_basis_mix` 披露 real/proxy/unknown 计数。本窗实测
`{real:15, proxy:10, unknown:10}` — real 不主导 ⇒ 0.5ATR 桶界含输入噪声,
分层读数降级方向性 (读数卡已自动标注)。

**④ n 门槛统一声明 (消歧)**: 裁决资格 = **配对 n≥40** (本 ADR §五.2 + replay 读数卡
已实现, 最新预注册); RFC-081 增补表 n≥30 = 宪章「n<30 不结论」中间态 (可说方向,
不得裁开关); RFC-081 主表 (2026-09-11 原文) 的 n≥20 系早期文字, **已被 40 覆盖**。
冲突时以最严最新预注册为准 — 防裁决日门槛争议。

**配套代码 (record-only)**: `cf_pullback_limit_replay.py` +`_day_clustered_t()` /
`_c3_reachability_split()` (子桶+ATR口径) / 行级 `atr_basis` / 读数卡三行
(日聚类 / C3子桶 / ATR口径)。

## 追加 (2026-09-16 第三批, DEB-20260916 独立复核回灌): 静态几何收益上限披露 — 判据零改动

**① re-price 几何不是当前可裁变量**: 落库 SL 跨 DA 失效线 vs 未跨的全历史
n=18/45，日配对差 **−$16.76/笔 (t=−0.35, n_days=7, MDE(80%)=±$133.61) ⇒ 与 0 不可分辨**。
pooled +$128 由 8/26 单日 SMTC +$444 支配。不得把"与0不可分辨"读成"几何无害已验证"，
也不得据此给 re-price 加硬约束 (宪章一.4: 数据不足不强行收敛)。

**② TP 触发率约束 D 案静态几何收益上限**: 全史 488 笔已平仓中 TAKE_PROFIT 仅 2 笔
(**0.41%**), SL_TRAIL_LOCK 111 笔 (23%)。D 案裁决仍以 C1-C4 配对判据为准；
静态 TP/RR 几何改善不能作为额外预期收益来源, 优先看成交率、mfe=0 下降与日聚类净差。

**③ 数据链路补充**: DA 原始确认位 `confirmation.price_level` 已在
`raw_instruction_json` 透传 (ARM=248.5)。D 案若要用确认位路由，缺口是路由/状态机
接线与裁决，不需要新增 prompt 字段。
