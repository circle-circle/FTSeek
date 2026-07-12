# DEB-20260705: deep_analysis → Playbook 候选发现+预筛 升级方案审查

## 背景

外部Agent建议: 让 deep-stock-analysis 从"独立分析工具"升级为 FTSeek Playbook 的"全市场候选发现 + LLM 预筛"模块，通过:
1. 独立 source 标签
2. 方向不对称门禁
3. claim 级 IC 硬拦截
4. pattern 置信度过滤
5. shared_kernel 市场状态接入
6. 把最优质的 SHORT/快止盈信号喂给 Playbook
7. 用独立 IC 样本验证其真实 edge
8. 再谈 sim→live 毕业

## Pre-debate 代码审计 (grep/SQL验证)

### 1. 独立 source 标签 — 现状
- adapter.py L172: `"source_type": "PLAYBOOK_SIM"` — deep_analysis 输出 PLAYBOOK_SIM, 不是独立标签
- trade_records 实际 source_type: MAIN_PIPELINE(194)/SCALPING(129)/PLAYBOOK_SHADOW(23)/PLAYBOOK_SIM(19)/ORB_ENGINE(11)/MANUAL(7)
- **没有 DEEP_ANALYSIS source_type** — deep_analysis 的交易(如果有的话)被标记为 PLAYBOOK_SIM, 无法区分
- playbook_instructions 表: **无 strategy_id 列** — 无法按策略追踪 IC
- signal_snapshots DB: deep_analysis 有 6 条记录, **0 条 t1_filled** — IC backfill 从未回填

### 2. 方向不对称门禁 — 现状
- config.py L36-37: `buy_min_confidence=80, sell_short_min_confidence=65` (不对称)
- 但 pipeline.py L820-830: "不再需要方向不对称门槛, 默认 BUY=70, SHORT=70 (对称)"
- pipeline.py 实际从 strategy_registry 读取 direction_filter, **默认 70/70 对称**
- **config.py 的 80/65 是死配置, pipeline.py 不读它**

### 3. claim 级 IC 硬拦截 — 现状
- claim_ic_cache.json **已存在**: 10类claim, 96样本
  - volume_expansion IC=+0.30 (最强短线)
  - pe_below_industry IC=-0.43 (价值陷阱)
  - insider_net_sell IC=-0.30
- signal_gate.py L236-293 **已有 claim IC 软门禁**: 用了IC<0的论据→-10分, 用了IC>0.05的→+5分
- **当前是软约束(降分), 不是硬拦截(BLOCK)**
- v0.15设计原则: "LLM是软约束, Gate是硬约束但只降分不BLOCK — 市场在变, 今天无效的论据明天可能有效"

### 4. pattern 置信度过滤 — 现状
- signal_gate.py L138-156 **已有四级置信度**: very_low(<5笔)/low(5-19)/medium(20-49)/high(50+)
- 低置信pattern只加分5(原10)不强推退出方式
- **已实现, 不需要新增**

### 5. shared_kernel 市场状态接入 — 现状
- app/core/shared_kernel.py **已存在**: VIX regime/仓位乘数/时段缩放/统一时段taxonomy
- app/analysis/market_state.py **已存在**: SPY 200日均线+VIX门禁
- **deep_stock_analysis 的 pipeline.py 从未 import shared_kernel 或 market_state**
- deep_analysis 的 market_scanner 扫描全市场但不感知 VIX regime

### 6. SHORT/快止盈信号 — 现状
- claim_ic_cache: SHORT方向无独立IC统计(cache按claim_type不按方向分)
- strategy_memory: SHORT+EOD_FORCE_CLOSE WR=78% PnL=$2926 (24笔, confidence=medium)
- voting_llm: LONG T1=+1.31% SHORT T1=-0.21% (deep_analysis v0.14数据)
- FTSeek 7/3审计: LONG系统性亏损(-$2426), SHORT盈利(+$1583)

### 7. 独立 IC 样本 — 现状
- signal_snapshots DB: deep_analysis 6条, **0条t1_filled**
- graduation gate要求: 30样本+IC(T+3)>0.05
- **距离毕业门禁还差24条+全部t1回填**

### 8. sim→live 毕业 — 现状
- deep_analysis 从未通过 Playbook 下过单(trade_records无DEEP_ANALYSIS记录)
- 6条IC信号0回填 → 无法验证edge
- "再谈毕业" = 还没到能谈的阶段

## 可选方案

### 方案A: 原方案全量实施
- 新增 DEEP_ANALYSIS source_type → 需改 playbook_service + shadow_tracker + ic_tracker + pipeline.py
- 方向不对称门禁 → 与pipeline.py现有对称设计冲突
- claim IC 硬拦截 → 与v0.15软约束设计原则冲突
- shared_kernel 接入 → 需改 deep_analysis pipeline + market_scanner
- **违反7/24冻结期: 需改运行时代码**

### 方案B: 做减法精简版
- 不新增source_type — deep_analysis通过external_votes注入Playbook(已有机制)
- 不做方向不对称 — 维持70/70对称(数据驱动决策)
- claim IC维持软约束 — 已实现, 不改
- pattern置信度 — 已实现, 不改
- shared_kernel接入 — deep_analysis读VIX regime(1行import+3行使用), 不改FTSeek侧
- **0行FTSeek运行时代码改动**

### 方案C: 最小验证先行
- 先修IC backfill(0回填根因) → 积累30+样本 → 验证edge → 再谈接入
- 不改任何接入逻辑, 只修数据流断裂
- **最保守, 但可能暴露"deep_analysis根本没有edge"的风险**

## 讨论记录

### 第 1 轮：hermes

**立场: 方案B(做减法精简版) + 方案C(验证先行)的组合**

**核心论点: 原方案6项中4项已存在, 剩余2项有更低成本替代**

1. **"独立source标签"是过度工程**: deep_analysis可通过external_votes注入Playbook voting_coordinator(已支持, L459-476)。不需要新source_type, 不需要改trade_records schema。external_votes把deep_analysis当"第4个model"参与聚合, 天然获得IC追踪(voting_llm的strategy_id)。

2. **"方向不对称门禁"已被数据否定**: pipeline.py L820明确注释"LONG IC=+0.238(强), SHORT IC=-0.051(弱) → 不再需要方向不对称门槛"。config.py的80/65是死配置。原方案提议的不对称门禁与数据矛盾。

3. **"claim级IC硬拦截"与设计原则冲突**: v0.15明确"软约束(降分不BLOCK) — 市场在变"。硬拦截pe_below_industry(IC=-0.43)意味着永久禁用该论据, 但市场regime切换后价值股可能回归。软约束-10分已足够降低信号质量分。

4. **"pattern置信度过滤"已实现**: signal_gate.py L138-156, very_low/low只加分5不强推退出。不需要新增。

5. **"shared_kernel接入"值得做但需先验证IC**: deep_analysis不感知VIX regime是真实gap, 但在6条IC样本0回填的阶段接入市场状态=在没验证edge前加复杂度。先修IC回填→积累样本→验证edge→再接shared_kernel。

6. **"独立IC样本验证"是唯一真正缺失的**: 6条0回填是核心瓶颈。根因可能是trade_date非交易日(v0.12已修)或backfill cron未覆盖deep_analysis strategy_id。修这个=1行SQL+1个cron检查, 不需要新模块。

7. **"sim→live毕业"为时过早**: 6条0回填, 连edge存不存在都不知道, 谈毕业是空中楼阁。

**证据**:
- `sqlite3 signal_snapshots.db "SELECT strategy_id,COUNT(*),SUM(CASE WHEN fwd_ret_t1 IS NOT NULL THEN 1 ELSE 0 END) FROM signal_snapshots GROUP BY strategy_id"` → deep_analysis|6|0
- `grep "external_votes" voting_coordinator.py` → L381,459-476 已支持注入
- `grep "buy_min_confidence" config.py` → 80, 但 `grep "BUY_min_confidence" pipeline.py` → L826 从strategy_registry读默认70
- `cat claim_ic_cache.json` → 10类claim, signal_gate.py L236-293 已有软门禁

### 第 1 轮：claude-code

作为 CRITIC 对 Hermes 的三项核心证据提出质疑：

1. **external_votes 并未真正被 deep_analysis 使用。** 全库 grep 仅看到 `voting_coordinator.py` 的形参定义，以及 `app/pipeline/signal_pipeline.py` 两处 `external_votes=None` 的调用；`app/playbook/pipeline.py` 调用 `vote()` 时根本没有传该参数。因此所谓“通过 external_votes 注入 Playbook”只是理论可行，并非已落地的生产链路。

2. **IC backfill 确为 0，且差距巨大。** 实际查询 `data/signal_snapshots.db`：`deep_analysis|6|0`，6 条快照没有一条回填 T+1 close。毕业门槛要求 30 样本且 IC(T+3)>0.05，现在谈接入为时尚早。

3. **70/70 对称默认属实。** `app/playbook/pipeline.py` L820-827 明确默认 `BUY=70, SHORT=70`，config.py 中的 80/65 确实未被读取，Hermes 这一点成立。

结论：方案 B 的方向合理，但 Hermes 把 external_votes 描述成“已有机制”过于乐观；当前最紧迫的仍是方案 C——先修复 IC 回填断裂、积累样本，再谈接入。

### 第 2 轮：hermes

**接受 Claude Code 的修正: external_votes 未在生产中被调用**

CRITIC 验证准确: `app/playbook/pipeline.py` 调用 `vote()` 时不传 external_votes, `signal_pipeline.py` 两处显式 `=None`。我原来说"已有机制"过度乐观 — 机制存在但没有接线。

**修正后的立场: 方案C(验证先行)优先, 方案B(接入)延后**

1. **external_votes 接线≠0行改动**: 需要在 `pipeline.py` 的 `_build_instructions_from_votes` 前加 deep_analysis 信号读取+external_votes 传参。但7/24冻结期不能改 pipeline.py → 接入必须等冻结期结束后。

2. **当前唯一该做的 = 修IC backfill 0回填**: 6条0回填是所有后续讨论的前提。根因排查:
   - v0.12已修trade_date非交易日问题(ET时区+前推)
   - 但6条记录仍然是0 → 可能是backfill cron的strategy_id过滤不含deep_analysis, 或trade_date仍不匹配
   - 需查 `backfill_forward_returns.py` 的SQL: 它查哪些strategy_id? 是否包含deep_analysis?

3. **原方案8项的精简判定**:
   - 独立source标签 → **延后** (external_votes够用, 不需新label)
   - 方向不对称门禁 → **否决** (数据否定, 70/70对称已验证)
   - claim IC硬拦截 → **否决** (软约束已实现, 硬拦截违反设计原则)
   - pattern置信度 → **已完成** (signal_gate L138-156)
   - shared_kernel接入 → **延后** (先验证IC再接)
   - SHORT/快止盈喂给Playbook → **延后** (等IC回填+external_votes接线)
   - 独立IC验证 → **P0** (修backfill, 唯一阻塞项)
   - sim→live毕业 → **P2** (6条0回填, 远未到讨论阶段)

4. **方案A 6项新建 vs 方案B/C 0行新代码**: 原方案提议新建模块/标签/门禁, 但4项已存在, 1项被数据否定, 1项需延后。真正要做的只是修1个backfill断裂。

**收敛声明**: 接受CRITIC修正, 收敛到"先修IC回填(方案C), 冻结期结束后再接external_votes(方案B), 原方案6项中0项需要新建"。

## Action Items

- [✅已验证] IC backfill 0回填根因: **不是bug** — 6条记录创建于周日7/5, backfill cron运行Tue-Sat(下次7/7周二05:30 CST), 还没到backfill时间。trade_date=2026-07-02(ET周四, 有效交易日)。backfill_forward_returns.py不按strategy_id过滤(get_pending_snapshots只查realized<3 + trade_date<today), deep_analysis的记录会被正常拾取。
- [P1] 冻结期结束后: 在pipeline.py _build_instructions_from_votes前加deep_analysis信号→external_votes传参 (需改运行时代码, 7/24冻结期不做)
- [P2] 周二backfill后验证: 查 `SELECT COUNT(*), SUM(CASE WHEN fwd_ret_t1 IS NOT NULL THEN 1 ELSE 0 END) FROM signal_snapshots WHERE strategy_id='deep_analysis'` — 预期从0→6
- [P2] IC样本达30+后: 验证deep_analysis edge (IC T+3>0.05?), 再谈shared_kernel接入和sim→live毕业
- [否决] 方向不对称门禁: 数据已否定(70/70对称), 不做
- [否决] claim IC硬拦截: 软约束已实现(v0.15), 硬拦截违反"市场在变"原则
- [已完成] pattern置信度过滤: signal_gate.py L138-156, 无需新增
- [延后] shared_kernel接入: deep_analysis读VIX regime(1行import), 但等IC验证后再接
- [延后] 独立source标签: external_votes够用, 不需新label
