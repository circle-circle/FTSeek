# Phase 1 信号质量治理 — 证伪前置 设计方案（已实施）

> 状态: 2026-08-23 已实施 (1552 passed / 1 skipped)
> 用户裁决: 知情破例（宪章验证期白名单外新机制，DEB-20260823 §八 P1 项）
> 前置已完成: Phase 0 全部 4 项（校准原则注入 / confidence score差值衰减 / UE冷却 / reentry豁免DEAD_SIGNAL）

---

## 实施结论与设计前提偏差（2026-08-23 落地时校核）

原设计有三处前提与代码/数据现实不符，实施时按以下方式修正（均经用户裁决或数据核实）：

| 项 | 原设计前提 | 实测现实 | 实际实现 |
|---|---|---|---|
| P1-2 | 指令带结构化 `tripwires` 字段 `[{metric,threshold,deadline,source}]`，record-only 落库 | `tripwires` 只存在于 bridge 文件 `opportunity_scan_*.json`，**未落库**；且**不可自动核销** — 极性未编码（"从-37.5回升至>-10" 是确认条件不是失效条件）、指标常为外部标的（金价/SOX指数/三星HBM出货/期权OI）、threshold 为自由文本。真正可用的是自然语言 `invalidation` 字段（近30日覆盖 243/319 = 76%，极性明确） | 复用既有 `app/playbook/invalidation_shadow.py` 解析器 + 新增析取/合取分解与时间限定词守卫 |
| P1-1 资金流门 | 作为硬拦门（fail-open） | `data/capital_flow_replay_verdict.json` 显示该信号**四关门全不过**: inflow t=-0.48 / outflow t=-0.70 / placebo p=0.85 / IS-OOS 符号翻转。fail-open 只保证"缺数据不拦"，有数据时仍按已证伪信号拦单 | **用户裁决改 record-only 影子**，落 `data/falsification_gate_shadow.jsonl`，攒样后按同一四关门判据重裁 |
| P1-1 信息新鲜度门 | 从 bridge `market_condition` 读新闻时效 | FTSeek 侧**无任何新闻时间戳数据源**（`news_us.db` 空表 / `news_v2/` 空目录 / `news_quality.db` 仅 37 行且只有 `classified_at`）。新闻在 Hermes DA skill 内部，bridge 输出不含 news 时间 | **未实现**。若要做需先在 Hermes 侧把 `newest_news_ts` 写入 bridge 候选（同 Phase 0 的 skill 补丁路径） |
| P1-3 | 用 `data/replay_bars/` 的 1-min bar 建量能曲线 | `data/replay_bars/` **无 volume 字段**（`[time,o,h,l,c]`，`preclose_counterfactual.py:11` 已注明）。有 volume 的是 `data/replay_bars_1m/`（`[time,o,h,l,c,volume]`，160 日 × 390 根，由 `pangan_daily_rebuild.py` 每日 cron 增量维护），但只覆盖 36/109 近30日指令标的 | 改用 `replay_bars_1m`；**用户裁决**"有数据的走新模型"，覆盖外自动回落线性 |

### P1-3 修门效果实测（8/21，44 票）

| 开盘后 | 线性 ratio 中位 | p50 ratio 中位 | 线性过门率 | p50 过门率 |
|---|---|---|---|---|
| 15min | 1.44 | 0.70 | 84% | 45% |
| 30min | 1.03 | 0.63 | 64% | 41% |
| 60min | 0.90 | 0.61 | 52% | 34% |
| 240min | 0.51 | 0.60 | 16% | 25% |

证实原诊断：线性折算在开盘时段分母系统性偏小 → ratio 虚高 → 门在成交最集中、最该确认的时段恒开。
阈值 0.8 保持不变，语义从"几乎恒过"变为真实过滤（仅影响 chase 路径，pullback 不过量能门）。

### 落地清单

| 文件 | 内容 |
|---|---|
| `scripts/tripwire_checker.py` | P1-2 核销脚本（新增） |
| `app/playbook/pipeline.py` | `INVALIDATED_SKIP_REASONS` 集合化 + 两处头检消费；`_calc_vwap_slope` / `_capital_flow_divergence` / `_intraday_vol_curve_p50` 新增；触发链插入证伪门；`_calc_intraday_vol_ratio` 分母双路径 |
| `config/unified_config.yaml` | `playbook.execution_parameters` 下 Phase 1 六个开关 |
| `tests/test_phase1_signal_quality.py` | 20 个回归测试（新增）；`test_playbook_pipeline.py` SEALED_CFG 加密封 |

---

## P1-1: DA/UE 加入证伪门子集

### 设计

从 reentry 的 10 道证伪门中选 **4 个通用的**作为 DA/UE 的前置证伪门
（位于 `pipeline.py` 的 `run_playbook_auto_execute` 触发链内，在当前 VWAP 方向门之后）：

| 门 | 判据 | fail 语义 | 数据源 |
|---|---|---|---|
| **资金流背离** | 大单净流与 direction 相反（BUY 但大单净流出 > 阈值） | fail-open（缺数据不拦） | `capital_flow_daily_latest.json` |
| **VWAP 斜率** | 5min slope 与方向不一致（BUY 但 VWAP 下倾） | fail-open | MarketDataRouter |
| **ATR 地板** | ATR% < 0.03（波动率过低，随机噪声吃掉 SL） | 已有（:4470），保持 | — |
| **信息新鲜度** | 全部新闻 > 4h 且无盘前异动（>2% pre-market move），降 conf 或 SKIP | fail-open | bridge 文件 `market_condition` |

### 实施位置
`app/playbook/pipeline.py` 触发链 `:4118-4900` 区间，在 VWAP 方向门（`:4813`）之后新增。
每门独立配置开关（`playbook.execution_parameters.falsification_gate_<name>_enabled`）。

### 回滚
各门开关置 false；或整体开关 `playbook.execution_parameters.falsification_gates_enabled: false`。

---

## P1-2: Tripwire 盘中自动核销

### 设计

DA 指令包含 `tripwires` 字段（`[{metric, threshold, deadline, source}]`），目前只 record-only 落库。
新增 cron 脚本 `scripts/tripwire_checker.py`，盘中每 60 分钟运行一次：

1. 读取当日 `playbook_instructions` 中未执行（`executed=0`）的指令
2. 解析 `tripwires` JSON 字段
3. 对每条 tripwire，判断是否已触发失效条件（如 "stock下破支撑位 $X"）
4. 已失效的指令 → UPDATE `skip_reason='TRIPWIRE_INVALIDATED'`，后续 auto_execute 循环自动跳过

### 数据获取
- price 类: 直接用 MarketDataRouter 实时报价
- volume 类: 同上
- news/event 类: 暂不自动核销（需 LLM 判断，成本高），只做 price/volume

### 回滚
删 cron 条目；已 SKIP 的指令可手动清除 skip_reason。

---

## P1-3: 量能门换 VWAP-relative 模型

### 问题
当前量能确认门（`pipeline.py:4533-4566`）用 `elapsed/390` 线性折算预期已成交量，
而真实成交量呈 U 型分布（开盘/收盘高、午间低）→ **开盘时段（成交最集中时）ratio 被系统性高估，
门在最需要时恒开**。

### 设计
替换为 **VWAP-relative intraday volume curve**：
- 用过去 20 日的逐分钟成交量分布建立该标的/该时段的"正常累积量"曲线
- 当日实际累积量 / 历史同时段 p50 = ratio
- 阈值保持 0.8（与原实现一致）

### 数据依赖
需要 `data/replay_bars/` 中的 1-min K线 bar（已存在，由 exit_replay_backtest 使用）。
如果某标的无历史 bar，回落原线性模型。

### 实施位置
`pipeline.py` 现有 `_calc_intraday_vol_ratio()` 函数（约 `:3974`），整体替换其内部逻辑。

### 回滚
配置开关 `playbook.execution_parameters.volume_gate_vwap_relative: false` → 退回线性模型。

---

## 执行计划

Phase 1 三项按以下顺序实施（每项带独立开关，一次一变量）：

| 序号 | 项 | 预计工作量 | 先决条件 |
|---|---|---|---|
| 1 | P1-2 tripwire 核销 | 新脚本 + cron | 无 |
| 2 | P1-1 证伪门子集 | pipeline 插入 4 门 | P1-2（核销提供了"该指令的 tripwire 已失效"这个信号） |
| 3 | P1-3 量能门升级 | 函数替换 | 独立 |

---

## 知情破例记录

按宪章 §四.1 要求记录：
- **破例项**: Phase 1 三项属"新机制"，不在验证期白名单内
- **破例理由**: DEB-20260823 已证明毛期望=0 且退出侧调优数学上无效 → 验证期"只改退出"的前提已不成立；
  入场侧质量治理是唯一可能产生 edge 的杠杆；等到 9 月再做等于又浪费一个月的费用捐赠
- **归因影响评估**: 三项全部带独立开关 + 只做减法（减少无效信号，不增加信号数量）→
  不增加费用支出，最坏情况=漏掉部分本就期望为零的信号；样本量减少但纯度提升
- **裁决人**: 用户（2026-08-23 对话中明确选择"知情破例，现在做"）
