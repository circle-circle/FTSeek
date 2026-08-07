# DEB-20260807-grilling-e1-e2: 筛选链路五问 (grilling Q1-Q5) + IC 归因 (E1) + 入场动能确认 (E2)

- **立案日**: 2026-08-07
- **发起人**: 用户（对筛选链路数据质量的连环追问，代号 grilling）
- **状态**: 代码已入库 (372d2ce8 / 75d93d44 / 8335f554, 全部 record-only 零行为变化) + 跨 Agent 独立复核完成 (P2 修复已闭环) — **E2 shadow 采集中，攒满 20-30 笔再由用户裁决是否落地硬门**
- **变更纪律**: 宪章 §四 验证期冻结窗口内，全部改动为**观测口径/shadow 采集**，无行为变化；E2 若转硬门属新机制，需用户裁决
- **关联**: DEB-20260806-source-control（源头把控，本文档 Q1/Q4/Q5 延续其 conf 议题）、ADR-021（VWAP 门 A/B，同属入场侧观测体系）

---

## 一、grilling 五问结论

| 问 | 结论 | 证据 |
|---|---|---|
| Q1 conf 是否死字段 | **DA 侧是**：8月 DA confidence 恒 35（54 条零方差），任何 conf 判据/分层在 DA 侧失效 | `get_summary_stats` 新增 `conf_stats`（DA n=245 avg 36.7 / UE n=75 / OTHER n=54），恒值现已可观测，下游 IC/切片应对零方差来源排除 |
| Q2 生成时 admission 判据是否空转 | **是**：8月 0 条生成时拦截——生成时 `position_size=None` → notional=0 判据全空转 | 修复为 record-only 预判：`_estimate_admission_qty` 估算 qty 跑 `admission.evaluate`，结果挂 `raw_instruction.admission.precheck`（`estimated=True, enforced=False`），只审计不硬拦 |
| Q3 ATR=0 硬丢弃量化 | 丢失量不可见 → 已补 shadow | `instruction_generator` ATR=0 丢弃前落一行 `data/atr_missing_shadow.jsonl`；暂不放行（固定 SL 回退会给高波标的开噪声扫损侧门） |
| Q4 执行序是否 conf 排序 | **已关闭（伪冲突）**：执行序=`ORDER BY id`，非 conf 排序；UE cap 50 vs DA 35 是伪冲突 | 代码走查 |
| Q5 9点档集中入场质量 | **全部亏损**：8月 9点档 29 笔 WR34% −277；结构=盘前生成、开盘集中入场 | 交易切片统计 |

## 二、E1：IC 归因隔离（"负 IC 因子"是旧链路污染假象）

- **核心发现**：DA 链路字段 IC **+0.11**（90/30 天双窗口稳定）；负 IC 全来自已废弃 voting_llm/scalping 链路。`factors_json` 字符串化形态污染因子名（`strategy_type::` 232 次）。
- **落地**：
  1. `scripts/ic_attribution_by_link.py`（新）：按 decision_id 前缀分组隔离重算 IC，修复版因子提取（4 种形态兼容），双窗口对照 → 产物 `data/ic_attribution_latest.json`
  2. `scripts/daily_ic_analysis.py`：`analyze_factors` 通过 decision_id JOIN `signal_snapshots.strength` 取真实信号强度（修正镜像值 35 被压平），DA-/other 链路分段输出
- **反转发现**：真实 strength 回填后交易层 IC 更负（DA −0.12 vs 镜像 −0.04）；分桶 strength=60 WR60% 盈利 vs strength=70 WR52% 亏损 → **执行层真实 alpha 反转，不是信号层问题**。P0 从"淘汰因子"改判为"修观测口径"。

## 三、E2：入场动能确认（counterfactual 已验证，shadow 采集中）

- **动机**：DA strength=70 段 107 笔 WR37% −791，亏损集中 09:36-09:40 集中入场段（61 笔 −422）。
- **判据**：入场后 15min 窗口 `mfe ≥ 0.5%` 判"有动能"，否则 counterfactual 拦截。
- **全历史回放**（`e2_momentum_confirm_replay.py`，分钟级 replay_bars）：
  - 拦截 86 笔 −963 WR34% ｜ 放行 43 笔 **+97 WR51%**
  - 全参数网格（窗口 5-60min × θ 0.3-1.0%）**20 组方向全一致**：拦截组全亏（−828~−1406），放行组全正（+27~+540），无参数依赖
  - 保守下界（全程 mfe）同向：θ=0.5% 拦 53 笔 −1567 / 通过 79 笔 +698 WR66%
- **落地**：`e2_momentum_shadow_daily.py` + cron `45 4 * * 2-6`（盘后 replay_bars 04:35 就绪后），每日增量 append `data/e2_momentum_shadow.jsonl`；**攒满 20-30 笔再定是否转硬门，不砍供给**（sim 采集期数据量优先）。

## 四、跨 Agent 独立复核记录（2026-08-07，用户令"核查"+"执行"）

另一 Agent 三提交整体质量良好，"record-only 零行为变化"声称逐点核实属实，关键数字复现一致。复核揪出并修复 3 个 P2 瑕疵（均不影响已产数据结论，仅口径/防御）：

| # | 瑕疵 | 修复 |
|---|---|---|
| P2-1 | `_estimate_admission_qty` 缺 `max_position_pct` 截断 + `risk_pct` 兜底默认 0.02 与 `PlaybookConfig.DEFAULTS=1.0` 不符（yaml 缺键时高估 qty 50 倍） | 补截断（与 `compute_position_size` 完全同构：默认对齐 DEFAULTS=0.08，显式 0/None 不截断）；risk_pct 默认改 1.0。实证样例 entry=150/stop=147：未截断 136 股 → 截断后 32 股（yaml 实际 max_position_pct=0.06） |
| P2-2 | Q3 atr_missing shadow `ts` 用本地 naive 时间 | 统一 `now_et()`（带 −04:00 offset） |
| P2-3 | E2 shadow `ts` 用 `utcnow()` | 统一 `now_et()` |

**验证**：新增 `_estimate_admission_qty` 单测 4 例；相关回归 85 例全绿；E2 脚本手动回填 42 条历史，产物 ts 带 ET offset 实证 ✓。

**E2 shadow 最新累计**（回填后，窗口 15min θ=0.5%，8/3 起增量口径）：
拦截 n=28 net=−168.2 WR=36% ｜ 放行 n=18 net=+114.0 WR=61% — 与全历史回放方向一致。

## 五、后续观察点

1. **E2 shadow 毕业判据**：累计 20-30 笔后若"拦截组全亏、放行组盈利"仍成立，提请用户裁决是否转硬门（属新机制，冻结期内需知情破例或排队 9 月）。
2. **Q1 后续**：DA conf 恒 35 的修复（让 confidence 有区分度）属 DEB-20260806-source-control §六 待终裁项，本文档不重复展开。
3. **Q3 后续**：`atr_missing_shadow.jsonl` 攒 1-2 周后评估丢失量级，再定回退策略。
4. **坑记录**：replay_bars 文件名 symbol 下划线（`US.GS` → `US_GS_DATE.json`）。
