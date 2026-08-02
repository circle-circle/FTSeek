# DEB-20260802 · 实盘切换路径设计: 两门一梯预注册

> **状态**: 🟢 已终裁 — 用户令"按建议先写文档和DEB", RFC-062 落档生效 (判据冻结)
> **起案方**: Qoder, 2026-08-02 (用户问"离切实盘还需要做哪些准备和逻辑调整和设计" → "基本认同, 还需要补充什么指标和数据以及代码配置")
> **关联**: `docs/decisions/RFC-062-live-cut-preregistration.md` · handoff R6 · 宪章验证期纪律 · R2 费用口径 · R4 账面修复

---

## §0 一句话

SIM 与实盘之间存在四个结构性差异 (撮合/合规/故障/费用), 全部历史样本基于模拟撮合而 edge 薄 (净期望 −$10.57/笔) — 因此"切实盘"拆为**两门一梯**, 判据冻结预注册 (RFC-062), 门 2 数据前提 (arrival_mid 落库等六件) 按纯测量/影子性质在验证期内建成。

---

## §一 事实链: SIM 与实盘的四个结构性差异

### 1.1 撮合 (最大缺口)
- SIM 市价单按报价成交: 无滑点、无部分成交、无流动性约束
- 全部验证样本 (胜率 50.8%, 净期望 −$10.57/笔, R:R 1.07) 建立其上; edge 薄 → 实盘滑点+价差可能吃掉全部 margin
- **系统当前无任何成交质量数据**: trade 记录无 arrival 价, 滑点无从算起

### 1.2 合规 (SIM 完全无视)
- **PDT**: 账户 <$25k → 5 交易日最多 3 次日内回转。在产退出规则 (1h 超时退出/盘末 8 分钟退出) 天然制造日内交易 — **设计级冲突**, 与"初始资金/账户类型"联动, 须同案裁决
- **SSR**: 个股跌 10% 后 SHORT 仅可 uptick, SIM 不拦截, 实盘拒单
- **借券**: SIM 做空永远成功; UE `short_check.borrow_feasible` 是 LLM 估计非券商实查; 借券费未进任何费用口径

### 1.3 故障 (SIM 断线无损失, 实盘=真实敞口)
- R4 已实证 SIM 账面漂移 50 项 — 实盘漂移即资金风险 → 对账环必须在 SIM 期先跑起来
- 无幂等键防重复单 (R4 后 decision_id 已通, 具备实现条件)

### 1.4 费用
- R2 确立 est 费用 = SIM 唯一可行口径; 实盘为真实佣金+SEC/FINRA 费+借券费, 净期望公式整体右移

---

## §二 论证: 为什么是"两门一梯"而不是一次跳转

| 路径 | 问题 |
|---|---|
| 8-29 直接全量实盘 | 滑点假设未实测, 薄 edge 下等于赌 |
| 无限期 SIM | SIM 撮合永远测不出成交质量, 样本价值递减 |
| **两门一梯 (选定)** | 最小仓位买真实滑点分布, SIM 并行做对照组; 每档晋级有样本纪律, 期望转负即降档 |

关键设计: **SIM 并行对照** — 门 2 的 G2-5 是相对口径 (实盘 vs SIM 同期), 消除行情漂移干扰, 直接隔离"撮合质量"这一个变量 (宪章 §四.2 一次一变量)。

---

## §三 补充指标/数据/代码配置清单

| 项 | 指标/数据 | 代码 | 配置 | 性质 |
|---|---|---|---|---|
| ① 成交质量 | arrival_mid (下单时刻买卖中价), slippage_bps, time_to_fill, 部分成交率 | `order/executor.py` 挂钩落库; scorecard 折算期望列 | `live_readiness.slippage_haircut_bps: 15` | 测量, 现可建 |
| ② PDT | pdt_daytrades_5d (滚动 5 日同日开平计数) | `scripts/pdt_counter.py` 报表 | `risk.pdt_guard` (占位 false) | 测量, 现可建 |
| ③ SSR | ssr_flag (昨收跌≥10%) | SHORT 路径影子标记, 只记录不拦截 | `risk.ssr_aware` (占位 false) | 影子, 现可建 |
| ④ 借券 | borrow_fee_bps | 试点期手工录入兜底 | `live_readiness.borrow_fee_bps` | 实盘侧 |
| ⑤ 对账 | reconcile_drift (券商持仓 vs 内部 open) | `scripts/live_reconcile_check.py` + cron 30min | — | 测量, 现可建 |
| ⑥ 幂等 | 下单前 decision_id 查重 | executor 下单路径 | — | 现可建 |
| ⑦ kill switch | 全局闸 + REAL RSA 解锁 | `mode_switch_service._is_live()` 已有锚点 | — | **留裁决** |
| ⑧ 门径汇总 | 两门读数一页 | `scripts/live_readiness_report.py` | — | 测量, 现可建 |

下单收口点 (本次定位): `app/web_dashboard/order/executor.py` + `manual_order_service.py` (`_trd_env()`/`_is_live()`, SIMULATE/REAL_ACCOUNT_ID 环境分离已存在)。

### §三补 在建状态 (2026-08-02 当日执行, 用户令"继续 并说明")

| 项 | 状态 | 实证 |
|---|---|---|
| ① 成交质量 | **已在产, 取消重建** | playbook_service.py:948-961 arrival 中价抓取, :1141-1157 slippage_bps 方向调整落 `metadata_json.execution`; 07-20 起 96% 覆盖, SIM 均值 +3.1/|9.9|bps。折算列已上岗: direction_hit_scorecard `dir_ret_t1_slip_adj` + exit_scorecard `expectancy_slip_adj` |
| ② PDT | **A1 取消** (§七: 富途香港不适用) | 开关 `risk.pdt_guard.enabled: false` 已实施 |
| ③ SSR | **已建成** | `scripts/ssr_shadow_daily.py` (影子只记不拦); 首周 17 条 SHORT 标 16, 5 条日低触 −10% (≈31% 保守上界) |
| ⑤ 对账 | **已建成** | `scripts/live_reconcile_check.py` + `data/audit/reconcile_live.jsonl`; 首跑发现共享 SIM 账户 4 外部持仓 → 白名单 `live_readiness.reconcile_external_symbols` |
| ⑥ 幂等 | **已在产, 取消重建** | playbook_service.py:1024 `try_claim_execution` |
| ⑧ 门径汇总 | **已建成** | `scripts/live_readiness_report.py` → `data/live_readiness_latest.json`; 首读: 门1 当前 1/4 达标 (仅 G1-2 样本量过), G2-3 对账 streak 0 (首日含白名单前漂移记录, 保守计) |

cron 待用户注册: SSR EOD + 对账盘中 30min (建议行见各脚本 docstring)。

---

## §四 留裁决清单 (RFC-062 未授权, 逐项需用户裁)

1. SSR 拦截生效 (影子→硬 gate = 行为改动)
2. PDT gate 生效 (同上)
3. kill switch REAL 环境接线 + 解锁流程
4. 借券集成 (moomoo 美股借券 API 能力未证实 — 见 §五)
5. **初始资金与账户类型** (≥$25k PDT 豁免 vs 现金账户 T+1) — 与 1h/盘末退出规则联动, 必须同案裁决

## §五 未决与风险

- **15bps 是假设非实测** (依据: 流动性大盘股典型滑点 5-15bps 取上限), 门 2 以实测中位替换 — 若实测系统性超标, haircut 修订需新 DEB
- 最小仓位滑点分布可能优于全码 (流动性占位小), L4 滑点外推是已知近似
- 若门 1 折算后净期望 <0 → 维持验证期, 本预注册不判"失败", 只判"未到试点条件"

---

## §六 裁决登记

- 用户 2026-08-02: "基本认同" 两门一梯框架 → "按建议先写文档和DEB" → RFC-062 + 本 DEB 落档生效
- 在建六件 (§三 ①②③⑤⑥⑧) 获执行授权 — 纯测量/影子, 符合验证期"修尺子/攒样本/做减法"纪律
- §四 五项留待后续裁决

---

## §七 勘正 (2026-08-02 当日, 用户提供富途官方规则) — PDT 不适用

### 7.1 事实更新
用户提供富途官方口径: 经富途证券(香港)交易港美股**无日内交易次数与资金门槛限制** — PDT 是 FINRA 针对美国本土券商的要求, 富途香港受香港证监会监管, 该规则不直接适用。(边界: moomoo US 即 Futu Securities Inc. 美国实体账户仍适用。)

### 7.2 RFC-062 修订 (A1, 已落档)
G2-4 移除 · 背景合规缺口移除 PDT · PDT 报表取消 (做减法: 无决策支撑) · 资金决策与 PDT 解绑 (≥$25k 不再是硬理由, 融资账户仍为 SHORT 前提)。

### 7.3 更大的在产发现 (本次 grep 实证)
1. **在产 pdt_guard 前提失效**: `app/risk/pdt_guard.py` + playbook 路径 fail-close (`playbook_service.py:919-944`) + 主链 (`order_executor.py:3583`) 均按 FINRA PDT 构建。
2. **"sim 也守 PDT" (2026-06-24 决策) 的设计意图反转**: 原意"避免 sim 达标 live 跑不了的假达标" — 现在 live (富途香港) 本无 PDT, SIM 反而被幻影规则约束。
3. **样本已被截断**: DEB-20260802-short-overnight-hold 记载"8 月 PDT 已触线 3 次" — 即验证样本的日内频率被一条实盘不存在的规则压制; 该 DEB 的"豁免 PDT/PDT 计数下降"论据同步降级 (其余论据独立成立)。
4. **SSR/借券不受影响**: SSR 是 SEC 市场级规则 (交易所层执行, 与券商属地无关), 借券是做空的物理前提 — 两项保留。

### 7.4 裁决落实 (2026-08-02, 用户裁"按照建议关闭")
**pdt_guard 处置 — 已实施**: 开关化于单点收口 `PDTGuard.check_trade_permission` (`risk.pdt_guard.enabled: false`), 覆盖全部 5 个调用点 (playbook_service / order_executor / pre_trade_check / validator / orb)。放行理由串含裁决日期; `get_status_report` 增 `enabled` 字段供监控如实显示。`record_close` 保留运行 (日内计数仍可观测, 零行为影响)。**解除日 2026-08-02 = 样本分层标记**, 此前批次受 PDT 截断 (8月触线3次), 读数分批 (照"配额批 vs 自然批"先例)。遗留: `app/core/trade_guard.py` 的 PDTComplianceManager 属冻结主链死路径 (DEB-20260721 必要性原则), 不动; 若主链复活需同步开关。验证: py_compile ✓ + 冒烟 (关闭→放行+理由串) ✓ + YAML 解析 ✓。
