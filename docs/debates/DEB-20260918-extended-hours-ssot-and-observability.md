# DEB-20260918 · 盘前/盘后新开仓 SSOT 与全链路可观测性前置设计

> 状态：Codex 已执行 P0 修复（§17） · 新开仓仍关闭 | 日期：2026-09-18 | 作者：Codex | 复查人：Hermes（§16 独立复核） | 用户裁决：SIM 先行、盘前/盘后同批开放、统一止盈优先、先治理再开仓

## 1. 背景

用户重新评估盘前/盘后新开仓后形成新共识：

1. **先 SIM**：继续禁止实盘，延长时段新开仓只允许 `FUTU_TRD_ENV=SIMULATE`。
2. **盘前/盘后同批开放**：两阶段共享每日新开仓上限，不采用各阶段独立配额。
3. **统一止盈逻辑**：`TAKE_PROFIT` / `SL_BREAKEVEN` / `SL_TRAIL_LOCK` 是最高优先级信号；盘前收口、盘后持到次日开盘只能兜底，不能覆盖有效止盈信号。
4. **初始亏损止损不放在延长时段**：`SL_INITIAL` 继续推迟到盘中处理，避免延长时段薄流动性噪声触发亏损止损。
5. **治理先行**：在打开新开仓前，把策略参数、阶段差异、日志、统计、推送、对账统一到可审计口径。

当前事实：

- `.env.us` 中 `EXT_ENTRY_ALLOW_SIM=false`，新开仓当前处于关闭状态。
- `scripts/extended_hours_entry_executor.py` 中仍存在本地默认参数：`MAX_PER_DAY=5`、`ENTRY_CHANGE_PCT=2.0`、`MIN_PRICE=5`、`MIN_VOLUME=200000`。
- `scripts/afterhours_exit_shadow.py` 中 `HOLD_TO_NEXT_OPEN` 会把当日在盘后开的仓覆盖为 `hold_to_next_open`，与本次“有效止盈不被覆盖”的新共识冲突。
- `scripts/premarket_exit_shadow.py` 中 `pre_close_flatten` 在 `09:20-09:25` 覆盖其它信号，也应降级为兜底：先执行有效止盈，仅无有效止盈时收口。
- `app/playbook/config.py` 已承担 Playbook 配置读取职责；退出策略已存在较清晰真源，不能另造一套止盈系统。
- 工作区另有一批未提交的交易身份标签治理改动。策略改造必须先提交或隔离该批次，避免两类变更混在同一提交。

## 2. 目标

1. 建立盘前、盘中、盘后共用的策略语义与阶段覆盖结构，减少代码内分散默认值。
2. 在打开 SIM 新开仓前补齐事件流水、净收益统计、MFE 捕获率、推送分级、preflight、对账和自动停止机制。
3. 让每次交易结果可归因到：候选来源、硬闸、排序、订单成交、执行滑点、退出策略、阶段兜底和 policy 版本。
4. 30 笔 SIM 试点通过后再讨论实盘，本轮不触碰实盘通道。

## 3. 非目标

- 不新建复杂规则引擎。
- 不重写 Playbook 止盈体系。
- 不把所有策略参数继续放在 `.env`。
- 不在本 DEB 阶段直接打开 `EXT_ENTRY_ALLOW_SIM`。
- 不处理与本议题无关的历史失败测试。

## 4. 数据与实证

### 4.1 2026-09-17 实盘前 SIM 实际样本

当日 4 笔延长时段开仓。**口径为净额**（`trade_records.pnl_net`，费用来自逐笔 per-share 费用模型，
`fees_source='est'`）：

| 标的 | 入场价 | 入场名义额 | 毛利 | 费用 | **净额** | 费用/名义 |
|---|---:|---:|---:|---:|---:|---:|
| CTNT | $0.072 | $4,931.50 | +$136.99 | $451.26 | **−$314.27** | **9.15%** |
| GIPR | $0.431 | $4,953.91 | −$80.46 | $100.86 | −$181.32 | 2.04% |
| NBIS | $230.05 | $4,831.05 | +$21.84 | $4.22 | +$17.62 | 0.09% |
| AEHL | $12.524 | $4,972.03 | −$160.39 | $10.50 | −$170.89 | 0.21% |
| **合计** | — | $19,688.49 | **−$82.02** | **$566.84** | **−$648.86** | 2.88% |

> **2026-09-18 Hermes 复核更正 (§23 R1)**：本表原用**毛口径**（+136.99 / −80.46 / +21.84 / −160.39
> ⇒ 合计 −$82.02），把当日最大亏损低估近 8 倍（−82.02 → −648.86），并把 CTNT 记成"盈利样本"——
> 它实际是当日最大亏损（费用占名义 **9.15%**，即低价股的费用直接超过毛利的 3 倍）。
> 这与 §6.2.4 自己写的"所有胜率和均值必须使用净收益口径"、§17 已回填 `fees_est/pnl_net` 相矛盾。
> 结论方向（候选质量 / 新鲜度 / 执行质量）不变，但"提高笔数不解决问题"的量级被严重低估：
> 真实教训是**低价档费用吃掉全部毛利**，`$5` 价格闸是费用闸而非仅仅是"妖股闸"。

该样本说明：仅提高每日笔数上限不解决问题，候选质量、事件新鲜度和执行质量才是核心
（并叠加：入场价越低，往返费用占名义额越高 ⇒ 单笔期望必须先过费用线）。

### 4.2 60 笔盘前代理回放

有效回放文件：`data/reports/replay_ext_entry_sources_latest_pretrailing.json`。

> 注意：`replay_ext_entry_sources_latest.json` 的 0 笔**不能作为本议题证据** —— 归因已由 §16.3-① 更正：不是 fail-closed 造成，而是回放工具 `scripts/replay_ext_entry_sources.py:288-301` 的 `decide` 导入在 9/18 退出链重构后失效，被 `except ImportError` 静默降级为恒返回 `hold`（summary 只留 `rule_arm_available: false`，日志零报错）。**修好工具前，本议题的闸门/排序/盘后 cohort 都无法复现。**
>
> 另：下表 cap 隐含一个**未声明**的日内排序口径（=回放文件序；改按涨幅降序排 cap1 为 +0.17%/79.2% 而非 +0.043%/75.0%）；且同一 JSON 内的退出臂表（rule −0.306% / 收口 −0.171%）与 ≥3%/≥5% 分臂读数（rule ≥3% = **−0.104%**，收口 ≥3% = +0.327%）见 §16.2 —— 它们直接决定 §5.2 / Phase 2 的优先级方向是否成立。

核心结果：

| 口径 | n | 净均值 | 胜率 |
|---|---:|---:|---:|
| cap 1 | 24 | +0.043% | 75.0% |
| cap 2 | 39 | -0.155% | 66.7% |
| cap 3 | 49 | -0.288% | 61.2% |
| cap 5 | 58 | -0.331% | 60.3% |
| 全量 | 60 | -0.306% | 61.7% |

涨幅分档：

| 涨幅区间 | n | 净均值 | 胜率 |
|---|---:|---:|---:|
| 2%-3% | 12 | -1.113% | 25.0% |
| 3%-5% | 19 | -0.432% | 57.9% |
| 5%-10% | 19 | +0.139% | 78.9% |
| >10% | 10 | +0.058% | 80.0% |
| >=5% | 29 | +0.111% | 79.3% |

解释边界：

1. 样本主要来自 watchlist 代理，不代表当前默认异动榜源。
2. 同窗回放，存在前视与样本内偏差。
3. 未完整包含真实盘口深度、滑点、部分成交和撤单损失。
4. 结论只支持方向判断：`3` 次上限降低暴露，但必须配合更严质量闸，不能单独视为收益优化。

## 5. 方案设计

### 5.1 策略 SSOT

在 `config/unified_config.yaml` 新增 `trade_session_policy`，采用“共享默认 + 阶段覆盖”。

建议结构：

```yaml
trade_session_policy:
  policy_version: 2026-09-18-01
  environment:
    allowed:
      - SIM
    real_enabled: false

  shared:
    entry:
      max_new_positions_per_day: 3
      position_usd: 5000
      min_price: 5.0
      min_change_pct: 3.0
      min_volume: 200000
      earnings_surprise_min_pct: 0.0
      earnings_max_age_days: 7
      max_spread_pct: 0.5
      max_quote_age_sec: 90
      reject_held_symbols: true
      reject_duplicate_symbol_today: true
    exit:
      profit_actions:
        - TAKE_PROFIT
        - SL_BREAKEVEN
        - SL_TRAIL_LOCK
      initial_loss_stop: defer_to_regular
      profit_signal_priority: highest

  sessions:
    premarket:
      entry:
        enabled: true
      fallback:
        type: flatten
        from_time: "09:20"
    regular:
      entry:
        provider: playbook_admission
      exit:
        provider: playbook_exit_engine
    afterhours:
      entry:
        enabled: true
      fallback:
        type: hold_to_next_open
```

原则：

1. 一个策略语义只有一个 owner。
2. 阶段差异必须显式写在 `sessions`，不能藏在脚本分支。
3. 盘前与盘后共享每日 3 笔上限。
4. 止盈参数继续由 Playbook 退出配置负责，不在 `trade_session_policy` 中复制。
5. `.env` 只保留运行环境和实盘安全硬闸，不再承载可调策略参数。

### 5.2 策略读取层

新增薄层模块，建议命名为 `app/playbook/session_policy.py`：

1. 读取 `trade_session_policy`。
2. 校验 schema、类型、范围和互斥关系。
3. 输出 immutable policy snapshot。
4. 提供 `evaluate_entry(candidate, session, state)`，返回 `allow / reject / reason / policy_version`。
5. 提供退出优先级裁决：有效止盈信号 > 阶段兜底 > 持有。
6. 不直接下单、不访问行情、不写 DB，保持可测试。

非法配置必须 fail-closed，不能静默回退到代码默认值。

### 5.3 候选排序

硬闸不改变，排序仅决定配额内优先级：

1. 事件新鲜度：财报披露时间越近越优先。
2. 执行质量：spread、盘口深度、报价年龄。
3. 涨幅区间：`5%-10%` 优先，其次 `>10%`，再其次 `3%-5%`。
4. 流动性：延长时段成交量。

禁止排序绕过硬闸。排序分数必须随事件记录，便于后续归因。

## 6. 可观测性设计

### 6.1 统一事件流水

新增 append-only 事件流，建议 `data/trading_events.jsonl`，并同步提供 DB 聚合表或由报告器读取 JSONL。

每条事件必须包含：

```json
{
  "event_type": "entry_gate",
  "event_id": "uuid",
  "occurred_at_et": "2026-09-18T04:00:00-04:00",
  "policy_version": "2026-09-18-01",
  "config_hash": "sha256",
  "trade_env": "SIM",
  "session": "premarket",
  "source_type": "PLAYBOOK_SIM",
  "symbol": "US.X",
  "trade_id": null,
  "order_id": null,
  "gate_reason": null,
  "score": null
}
```

事件类型：

| 类型 | 含义 |
|---|---|
| `candidate_observed` | 候选进入扫描 |
| `entry_gate` | 硬闸/排序/配额结果 |
| `entry_order_submitted` | 下单请求已发出 |
| `entry_order_filled` | 成交、部分成交、撤单 |
| `position_opened` | 本地 DB 建仓完成 |
| `exit_signal_observed` | 退出决策与有效价格 |
| `exit_order_submitted` | 止盈/兜底平仓请求 |
| `exit_order_filled` | 平仓成交结果 |
| `position_closed` | 最终结算 |
| `reconciliation` | 券商、订单、DB、审计对账 |
| `risk_stop` | 自动停止条件触发 |

要求：

1. `event_id` 幂等，重复投递不得产生双计。
2. `trade_id` / `order_id` 能串起完整生命周期。
3. JSONL 是原始事实，报告不得依赖推送消息反推。
4. 每条事件写入失败必须告警，不允许静默丢数据。

### 6.2 统计指标

#### 候选漏斗

```text
candidate_observed
  -> hard_gate_passed
  -> ranked_attempted
  -> order_submitted
  -> order_filled
```

统计：通过率、拦截原因分布、排序命中率、配额命中率。

#### 执行质量

- 限价成交率。
- 部分成交率。
- 超时撤单率。
- 请求价与成交价差异。
- 实际滑点。
- 报价年龄和 spread。

#### 净收益

- 毛收益。
- 手续费。
- 滑点。
- 净收益。

所有胜率和均值必须使用净收益口径。

#### 退出质量

按退出动作分组：

- `TAKE_PROFIT`
- `SL_BREAKEVEN`
- `SL_TRAIL_LOCK`
- `pre_close_flatten`
- `hold_to_next_open`
- 盘中引擎接管

核心指标：

```text
MFE capture = net_realized_pct / gross_mfe_pct
```

仅当 `gross_mfe_pct` 大于费用垫和最小阈值时纳入捕获率，避免小额噪声污染统计。

#### 风险指标

- 每日新开仓数。
- 单日净亏损。
- 单标的重复拦截。
- 峰值暴露。
- 最大单笔亏损。
- 未完成对账数。

### 6.3 推送分级

#### P0 立即推送

- 实盘环境尝试下单。
- TEST / US.TEST / *.TEST 进入真实推送或生产 DB。
- 券商已成交但本地 DB 写入失败。
- 订单与 DB 对账冲突。
- 部分成交长时间未收敛。
- 自动停止条件触发。
- preflight 失败且该失败会阻断交易安全。

#### P1 交易事件

- SIM 新开仓成交。
- 有效止盈触发并成交。
- 盘前收口兜底成交。
- 盘后持到开盘兜底执行。
- 达到每日 3 笔上限。

#### P2 汇总

- 日报：漏斗、成交、净收益、MFE 捕获率、异常。
- 周报：30 笔试点进度、阶段对比、参数版本对比、通过/失败状态。

降噪规则：

1. 不推送每次轮询和每个被拒候选。
2. 相同 key 的重复告警在 15 分钟内聚合。
3. 标题显式带 `SIM`、阶段、策略、来源、policy 版本。
4. SIM 与真实通道分离。
5. TEST 标的一律不得进入真实通道。

## 7. Preflight 与安全闸

打开新开仓前必须执行 preflight：

1. `FUTU_TRD_ENV=SIMULATE`。
2. `ALLOW_REAL_TRADING=false`。
3. `trade_session_policy` schema 校验通过。
4. 当前 ET 时间处于对应 session 窗口。
5. 报价字段存在且未过期。
6. spread / 深度字段可用。
7. DB 可写。
8. 无未收敛 active order。
9. 当日 state 日期正确。
10. TEST 标的写入守卫有效。
11. 推送通道可用。
12. 自动停止状态未触发。

Preflight 失败时停止新开仓，但不禁止已有持仓的退出管理。

## 8. 自动停止与通过标准

### 8.1 停止条件

满足任一条件立即停止新开仓：

1. 累计 10 笔后净均值 `< -0.3%`。
2. 单日净亏损 `> 1.5%`。
3. 订单/DB/审计发生不可自动修复的不一致。
4. preflight 连续失败且影响资金安全。

停止后：

- 新开仓 fail-closed。
- 已有持仓退出继续运行。
- 必须输出风险停止日报。

### 8.2 通过标准

至少 30 笔 SIM 样本：

| 指标 | 目标 |
|---|---:|
| 净均值 | > 0.15% |
| 胜率 | > 55% |
| 限价成交率 | > 70% |
| MFE 捕获率 | >= 40% |
| 无未收敛对账冲突 | 必须 |

通过后才进入下一轮讨论，不自动升级实盘。

## 9. 实施步骤

### Phase 0 · 仓库隔离

- [x] 提交或明确隔离当前交易身份标签治理改动（commit `e03b30874`）。
- [x] 确认本 DEB 不与该改动混合提交。

### Phase 1 · SSOT

- [ ] 新增 `trade_session_policy`。
- [ ] 新增 policy loader 和 schema 校验。
- [ ] 将延长时段入场参数改为读取 policy。
- [ ] 增加非法配置 fail-closed 测试。
- [ ] 增加盘前/盘后共享 3 笔上限测试。

### Phase 2 · 退出优先级

- [x] 修改盘后逻辑：有效止盈信号优先，`hold_to_next_open` 只兜底。
- [x] 修改盘前逻辑：有效止盈信号优先，`pre_close_flatten` 只兜底。
- [x] 保持 `SL_INITIAL` 延长时段不执行。
- [x] 增加信号优先级测试。

### Phase 3 · 候选质量

- [ ] 财报新鲜度硬闸。
- [ ] spread / 深度硬闸。
- [ ] 已持仓和同日重复标的硬闸。
- [ ] 候选质量排序。
- [ ] 排序分数与 gate reason 落事件。

### Phase 4 · 可观测性

- [ ] 统一事件写入器。
- [ ] 事件幂等与完整性测试。
- [ ] 日报聚合器。
- [ ] 周报试点进度。
- [ ] 推送分级模板。

### Phase 5 · Preflight 与对账

- [ ] Preflight 脚本。
- [ ] 券商订单 / open_positions / trade_records / 审计文件对账。
- [ ] 对账冲突停止新开仓。
- [ ] 自动停止状态文件。

### Phase 6 · SIM 开启

- [ ] Hermes 复查通过。
- [ ] 全部 P0 验收通过。
- [ ] 将 `EXT_ENTRY_ALLOW_SIM` 或后续 policy 等效开关改为开启。
- [ ] 观察前 10 笔。
- [ ] 未触发停止条件则继续到 30 笔。

## 10. Hermes 复查清单

Hermes 需要重点复核：

1. **SSOT 边界**：`trade_session_policy` 是否只管 session 硬闸，是否侵入 Playbook 退出参数真源。
2. **优先级正确性**：盘后持有逻辑和盘前收口逻辑是否确实降级为兜底。
3. **共享配额**：盘前和盘后是否严格共享 3 笔，而非各 3 笔。
4. **事件幂等**：`event_id` 能否防止重试和 cron 重复导致双计。
5. **净收益口径**：日报、周报、自动停止是否统一扣费并纳入滑点。
6. **MFE 口径**：是否使用入场后的路径峰值，是否避免前视，是否排除费用以下的小 MFE。
7. **财报新鲜度**：Finnhub `period` 与披露时间语义是否正确，不能继续用财报周期误当披露日期。
8. **盘口质量**：spread 阈值 `0.5%` 是否适配 `$5-$5000` 全价格带，是否需要分价格档。
9. **状态恢复**：进程重启、cron 重复、日期切换、冬令时切换后 state 是否正确。
10. **推送治理**：SIM / REAL / TEST 标签是否贯穿推送、日报、周报。
11. **未提交变更隔离**：当前身份标签治理改动是否会与本计划混入同一提交。
12. **回放结论边界**：watchlist 代理样本是否被误用于证明当前异动榜源。

## 11. 主要风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 盘后新仓隔夜跳空 | 单笔亏损可能远超延长时段可控范围 | SIM 先行、3 笔上限、事件新鲜度、单日亏损停止 |
| 薄流动性执行 | 成交率低、滑点高、部分成交 | spread/深度闸、限价单、成交率指标 |
| 旧财报误判 | 把历史基本面当当前催化 | 披露时间硬闸，不复用 period 当 release date |
| 参数分散回归 | 实验结果不可归因 | SSOT、config hash、policy version |
| 事件双计 | 日报与风控误判 | event_id 幂等、对账 |
| 推送噪声 | 关键告警被淹没 | 分级、聚合、禁止逐轮询推送 |
| 优先级冲突 | 止盈信号被兜底覆盖 | 明确测试：有效止盈 > 兜底 |

## 12. 回滚方案

1. 关闭新开仓开关，保持已有持仓退出运行。
2. 若 SSOT 读取异常，直接 fail-closed，不允许回退旧代码默认值。
3. 若推送异常，只停推送，不停交易安全事件写入。
4. 若事件流异常，停止新开仓，退出侧继续运行。
5. 若对账冲突，停止新开仓，人工确认前禁止修复性写账。

## 13. 待决问题

1. spread 阈值是否统一 `0.5%`，还是按价格带分档。
2. 财报新鲜度 7 天是否需要按盘前/盘后分别调整。
3. MFE 捕获率分母是否使用可执行 bid/ask 路径，而非 last price 路径。
4. 事件流只使用 JSONL，还是同时落 DB 表。
5. 30 笔通过后是否进入新一轮 SIM 复盘，还是直接讨论实盘准入。

## 14. 验收清单

- [ ] 策略参数全部来自 SSOT，脚本无策略业务默认值。
- [ ] 盘前/盘后共享每日 3 笔。
- [ ] 有效止盈优先于盘前收口和盘后持有。
- [ ] `SL_INITIAL` 只在盘中执行。
- [ ] 实盘和 TEST 硬闸有测试。
- [ ] 事件流水完整串起 candidate -> order -> position -> exit -> settlement。
- [ ] 日报/周报使用净收益和统一标签。
- [ ] Preflight 通过后才允许新开仓。
- [ ] 对账冲突自动停止新开仓。
- [ ] 10 笔停止条件和 30 笔通过标准落地。
- [ ] Hermes 复查意见全部关闭。

## 15. 讨论记录

### 第 1 轮 · 用户战略裁决

1. SIM 先行。
2. 盘前/盘后同批开放。
3. 止盈使用统一逻辑，不被阶段兜底覆盖。
4. 候选质量按建议：新鲜财报、价格、涨幅、量、spread、深度、去重。
5. 达到现有盈利条件即可确定性落袋。
6. 接受 10 笔和单日亏损停止条件。
7. 优先稳定性，再追求收益上限。

### 第 2 轮 · 本 DEB 结论

1. 先做 SSOT 和可观测性，再打开新开仓。
2. 不重复造轮子，退出继续复用 Playbook。
3. 3 次上限是风险控制，不是质量提升本身。
4. 当前回放证据不足以直接支持放开，必须以 SIM 实验重新验证。

## 16. Hermes 独立复核（2026-09-18）— 逐条复现 / 需修正 / 阻塞项

> 复核人：Hermes · 方法：读码（file:line 锚点）+ DB 实查（`data/ftseek_us.db`）+ 重跑既有产物 + 统计量独立计算。
> 纪律：不采信文档自述（宪章律7：遥测实测 > 文档自述）。本轮**不覆盖**原轮结论，只回执与增补。
> 本文档未提交；本轮未触碰工作区在途的身份标签批（mtime 07:50–07:55，codex 进程仍在运行）。

### 16.1 复现判定表

| 原文声称 | 复核结果 | 证据 |
|---|---|---|
| §4.1 四笔合计 −$82.02 | ✅ 精确复现 | `trade_records`: CTNT +136.99 / GIPR −80.46 / NBIS +21.84 / AEHL −160.39 |
| §4.1 AEHL 财报 period = 2012 | ✅ | `metadata_json.earnings.period` = 2012-06-30 |
| §4.2 cap 表（1/2/3/5/全量） | ✅ 逐格复现（net_pct, rule 臂） | 24/+0.043/75.0、39/−0.155/66.7、49/−0.288/61.2、58/−0.331/60.3、60/−0.306/61.7 |
| §4.2 涨幅分档表 | ✅ 逐格复现 | 12/−1.113/25.0、19/−0.432/57.9、19/+0.139/78.9、10/+0.058/80.0、29/+0.111/79.3 |
| §4.2 latest.json 不能作证据 | ✅ 结论对 / 🔴 归因错（16.3-①） | 真因是 rule 臂静默降级，非 fail-closed |
| §1 `EXT_ENTRY_ALLOW_SIM=false` | ✅ | `.env.us:136` |
| §1 脚本内本地默认 5 / 2.0 / $5 / 20万 | ✅ | `extended_hours_entry_executor.py:51/62/63/64` |
| §1 盘后 `HOLD_TO_NEXT_OPEN` 覆盖当日盘后仓 | ✅ 且在码更宽 | `afterhours_exit_shadow.py:200-201`（无条件覆盖，连 `TAKE_PROFIT` 也压） |
| §1 盘前 09:20–09:25 收口覆盖其它信号 | ⚠️ 措辞需修 | `premarket_exit_shadow.py:285-288`：条件为 `hhmm ≥ FLATTEN_FROM_HHMM(920)` 且 `in_window`；窗止于 09:29 非 09:25 |
| §1 工作区有未提交身份标签批 | ⚠️ 范围更大（16.3-⑤） | 入口执行器本身在该批内，且 import 未跟踪模块 |
| §5.1 盘前/盘后共享日配额 | ✅ 机制已存在 | state key `{ET日期}:{session}` + 按 ET 日期前缀汇总（跨日 key 已忽略） |
| §10.4 事件幂等 / §10.5 净收益口径 | 🔴 现状不支持 | 无 `event_id` 概念；ETH 四行 `pnl_net` 与 `fees_est` 全为 NULL（4/4） |

### 16.2 复核方新增复算（原文未做）

**(a) 同一文件内的退出臂表**（原文 §4.2 整表省略，n 均 60）：`rule` −0.306% / `pre_close`（收口）−0.171% / `rth_close` +0.201% / `intraday_sl` +0.602% / `sl_only` −1.280% / `cat_4pct` −0.663%。

**(b) 涨幅闸 × 退出臂**（原文只给 rule 臂）：

| 涨幅 | rule 净均值 / WR | 收口净均值 / WR |
|---|---:|---:|
| 全量 n=60 | −0.306% / 61.7% | −0.171% / 43.3% |
| ≥3% n=48 | **−0.104%** / 70.8% | **+0.327%** / 52.1% |
| ≥5% n=29 | +0.111% / 79.3% | +0.690% / 58.6% |

⇒ 在本 DEB 自己引用的证据文件里，「3% 涨幅闸 + 有效止盈优先 / 收口降级为兜底」这条组合**仍是负期望**；正期望来自「≥3%（或 ≥5%）+ 收口强平」。
⚠️ **口径警告**：该 `rule` 臂调的是**已退役**的 shadow `decide()`（即 9/18 用户勘误作废的 +0.3%/+2% 那套），既不能证伪新的 `TAKE_PROFIT`/`SL_BREAKEVEN`/`SL_TRAIL_LOCK`，也不能给 Phase 2 背书 ⇒ 正确结论是「Phase 2 目前缺证据」，不是「Phase 2 错」。

**(c) §8 判据的统计功效**（60 笔实测单笔净 σ = 1.175%）：
- 检出 +0.15% 边缘（80% power）需 **n≈481**；n=30 时均值 95% CI 半宽 ±0.42%。
- 真实边缘 = 回放最好读数 +0.26% 时，30 笔判据仍有 **30%** 概率判失败；真实 +0.11% 时 **57%**。
- 10 笔止损：真边缘 +0.11% 时 **13.5%** 概率被噪声打停；真值 0 时 **21%**（纯随机游走也有 1/5）。
⇒ 「30 笔净均值>0.15%」与「10 笔净均值<−0.3% 停机」两个门目前主要由噪声决定。

**(d) 涨幅档排序敏感性**：cap 表隐含「回放文件内顺序」这一未声明口径；按涨幅降序重排后 cap1 = **+0.17% / WR 79.2%**（原文 +0.043% / 75.0%）。§5.3 又规定了第四套排序（新鲜度/执行质量/涨幅档/流动性）⇒ 排序口径必须先写死，否则同一份 cap 证据随排序规则漂移。

### 16.3 原文需修正的 5 处

**① §4.2 注脚归因（必改）**：`replay_ext_entry_sources_latest.json` 的 0 笔不是 fail-closed 造成。真因：`scripts/replay_ext_entry_sources.py:288-301` 的 `from scripts.premarket_exit_shadow import decide` 已被 9/18 退出链重构移除 → `except ImportError` → `_rule_arm_ok=False` → `decide` 恒返回 `hold` → 无 `ret_pct` → `gates_passed=0`、arms 整表消失（summary 仅留 `rule_arm_available: false`，日志零报错）。
**后果比注脚大**：Phase 3 的闸门 / 排序 / 盘后 cohort **目前无法用该工具验证**，而本 DEB 的「先治理再开仓」正依赖它。（静默 `except ImportError` 降级是本仓第 2 次同型死法）

**② §4.2 应补**：退出臂表 + ≥3%/≥5% 分臂表（见 16.2 a/b）。否则读者会误以为证据支持「3% 闸 + 止盈优先」。

**③ §4.2 应写明 cap 表的日内排序口径**（见 16.2 d）。

**④ §1 / §5.1 两处未标注的决策回退**：
- `max_new_positions_per_day: 3` —— 9/17 用户裁为 2→5（`extended_hours_entry_executor.py:51` 注释 + 两条 entry cron 注记均写 5）。§5.1 直接写 3 = 回退，应列为**待裁项**而非既定值。
- `min_change_pct: 3.0` —— DEB-20260917 §16.9 白纸黑字「未擅自改默认值，交出裁决」；本轮在 yaml 落 3.0 = 行为变更混入治理阶段。建议治理批保持值=现值（2.0），另开一项裁决。

**⑤ §1 身份标签批的范围**：`scripts/extended_hours_entry_executor.py` **本身已在该批内**（新增模块级 `from app.core.trade_identity import ensure_test_symbol_not_in_production_db`），且 `app/core/trade_identity.py` 当前为未跟踪 `??` ⇒ Phase 1 若提交该文件的 SSOT 改造，从 HEAD 取出的副本立即 `ModuleNotFoundError`（守卫测试只扫 tracked 文件，拦不住）。「隔离该批次」在此文件上不可行，只能先落身份批或先回退该 hook。

### 16.4 阻塞项（开仓前必做，8 项）

1. **先修回放工具，再改生产优先级**：`decide` 改为 import 共享纯函数 `decide_original_profit_exit`；降级路径必须显式报错（禁静默 `hold`）。修好后跑两个实验：(a) 新利润保护臂 vs 收口臂（同样本、同费垫）；(b) `--session afterhours` 入场 cohort（`data/replay_cache` 已有 224 只 × 06-22→09-16 的 16:00–19:55 bar）。Phase 2 的「有效止盈 > 兜底」必须先有这两个读数。
2. **延长时段净口径补全**：ETH 四行 `pnl_net` / `fees_est` 全 NULL（4/4，只有毛 `realized_pnl`）⇒ §6.2 与 §8 的净收益判据当前算不出来。退出链需算净额 + 费。
3. **`entry_session` 写入**：`trade_records` 已有该列（regular 行 115/115 有值），但 7 行延长时段/同步行**全为 NULL**（session 只藏在 `metadata_json`）⇒ 任何按 `entry_session` 分层的报表会静默漏掉延长时段仓。这条优先级高于新建事件流。
4. **事件流走「扩列不加文件」**：仓库已有五套在产台账（entry/exit executions jsonl、price_gate_rejects jsonl、settle jsonl+latest、exit_reconciliation json、entry_state json）。真正缺的只有三件：①每候选漏斗（现在只有价格闸有打点，事件/涨幅/量三闸无 rejects 明细）②幂等键 ③policy 版本。policy 版本可直接复用 `config_version`（近 30 日 211/211 + 4/4 已填充，`edge_weekly_report` 已按它分层）。**不新建 `trading_events.jsonl`** —— 本仓「多载体同名概念」有先例（死表 vs 活门 / 双合成漂移），第 5 套必然漂移。
5. **标签唯一生产者**：§6.1 的 `trade_env`/`session` 与 §6.2 的分组统计直接调在途的 `app.core.trade_identity.trade_identity()` / `identity_summary()`（已含 `entry_session`→`metadata.session` 回退与 `pnl_net`→gross 回退）。否则会出现第 4 套标签词汇（`BUY`/`SELL_SHORT` vs `LONG`/`SHORT` 错配历史已 3 次）。
6. **Phase 0 重写**：先落身份批或先回退该文件 hook；若落批，未跟踪的 `trade_identity.py` 必须**同批提交**（提交闭包），并在 HEAD 干净 worktree 里 `import` 一次验证。
7. **`next_open` 死路二选一**：`extended_hours_exit_executor.py:165-166` 已返回 `next_open_exit_disabled`，而 `premarket_exit_shadow.py:294-313` 仍为 carry 仓下发 `next_open_exit` 并调 `--session next_open` ⇒ §5.1 把 `afterhours.fallback.type: hold_to_next_open` 写进 SSOT = 把「配置有 / 执行没有」固化。要么恢复执行体，要么按「删门 > 加守卫」从 SSOT 与影子里一并删除该动作。
8. **§8 判据改口径**（见 16.2c）：通过/停止门改为高信噪比二值项（限价成交率、部分成交/超时撤单、对账零冲突、闸门拦截账、跳空尾部），净均值降为观察项并注明最小 n 或改累计/双窗口。另：§8.1「单日净亏损 > 1.5%」缺分母定义；且仓库已有**六套**「当日亏损」权威（登记于 `app/playbook/risk_circuit_breaker.py:29-45`：AccountCircuitBreaker / AbsoluteCircuitBreaker / symbol_day_loss / SymbolDailyLossGuard / RiskCircuitBreaker / DrawdownController，其中 RiskCircuitBreaker 头注明写「从未接线」）⇒ 本节必须写明「扩展其中哪一套 + 其余六套如何处置」，否则成为第 7 套。

### 16.5 可选与不做

**可选**
- spread 阈值分价格带：窗内跑一次 `extended_hours_entry_executor.py --dry`（实测拦截条件是 `not dry and not ALLOW_SIM_ENTRY`，dry 分支不受总开关影响）+ `get_market_snapshot`，当天即可得 top50 的 bid/ask/spread 价格带分布，直接回答 §13.1，不必等活样本。
- `ALLOW_SIM_ENTRY` 代码默认由 `"true"` 翻成 `"false"`（fail-open → fail-closed），与 §5.1 `real_enabled: false` 形成双保险（现安全开关吊在 `.env.us` 上）。
- §10.7 财报新鲜度必须**源限定**：榜单源用 `released_date`（`replay_ext_entry_sources.py:113-129` 已在用）并收窄到 {前一交易日, 当日}；Finnhub 侧维持「超预期>0 即过」（用户已裁删除新鲜度门，`period` 是季末）。按现措辞落地 = 重启已决事项，且会把榜单源供给打到≈0。
- §13 待决 5 问中至少 3 问（spread 分档 / MFE 分母用 bid-ask 路径 / 事件流是否落 DB）可用现有产物当天收口。

**不做**
- 不在治理阶段顺带改 cap(3) / 涨幅闸(3.0) 的值。
- 不把止盈参数复制进 `trade_session_policy`（原文已说对，保持）。
- 不新建第 5 套事件载体。

### 16.6 §10 复查清单回执（12 条）

| # | 回执 |
|---|---|
| 1 SSOT 边界 | 结构合理；但落 3.0 与 cap3 已越界成行为裁决（16.3-④） |
| 2 优先级正确性 | 两处覆盖确认在码；改「止盈优先」目前无证据支持（16.2b），且盘后兜底执行体已禁用（16.4-7） |
| 3 共享配额 | 机制已存在且确为盘前+盘后合计（非各 3 笔）；值 3 与用户裁 5 冲突 |
| 4 事件幂等 | 现无 `event_id` 概念，仅 5 分钟 cron + flock + `_already_executed(symbol,today)`；建议唯一键 = (ET日, session, 标的, 事件类型)，不用 UUID（对账/重跑更友好） |
| 5 净收益口径 | ETH 行 `pnl_net` 全空，现在做不到（16.4-2） |
| 6 MFE 口径 | `settle_extended_hours` 已在算 `avg_mfe` / `avg_capture_pct`（含 `initial_loss_n`、翻转率），公式应挂它而非新造；注意现有 capture 出现 −599% / −447%（n=2）离群值，阈值过滤须先做 |
| 7 财报新鲜度 | 必须源限定（16.5），否则重启已决事项 |
| 8 盘口质量 | 0.5% 未定标；可用一次 `--dry` 扫描出价格带分布 |
| 9 状态恢复 | state 的 ET 日期键 / 跨日残留已处理；另一半在「同一标的 attempt 键」——当日被撤孤儿单会永久占 attempt（配额未满时表现为「少一次机会」），§7.8 / §6.4 应写进去 |
| 10 推送治理 | SIM/REAL/TEST 三标签的生产者就是在途身份模块，接它即可（16.4-5） |
| 11 未提交变更隔离 | 见 16.3-⑤ / 16.4-6 |
| 12 回放结论边界 | 原文已标 watchlist 代理与前视 ✅；漏「排序口径未声明」一条（16.3-③） |

### 16.7 复核边界（如实）

- **未跑新样本**：工具当前故障未修，故 16.2(a)(b) 是对**既有产物的重新聚合**，不是新回放；Phase 2/Phase 3 的证据仍缺。
- **未验证盘后入场收益**：盘后 cohort 尚不存在，16.4-1(b) 是待做实验。
- **未核**：§9 各 Phase 的测试覆盖（尚无代码落地）、§6.3 推送分级模板、§7 preflight 与现有 `scripts/cutover_preflight.py` / `daily_invariant_check.py` 的重叠度。
- **未提交本文档**，未触碰工作区在途身份标签批（mtime 07:50–07:55，codex 进程在跑）；本次只改本文件。
- 统计功效结论基于单窗口 60 笔 σ=1.175%；换窗口/换闸门口径 σ 会变，但方向稳健（n=30 不足以检出 <0.3% 的边缘）。

## 17. Codex 执行回执（2026-09-18）

> 本节记录用户令「按照建议执行」后的落地结果。**未打开 `EXT_ENTRY_ALLOW_SIM`，未触碰实盘通道。**

### 17.1 已完成事项

1. **仓库隔离**
   - 交易身份标签批已独立提交：`e03b30874` (`Unify trade identity for push and reporting`)。
   - 本批策略修复未混入该提交。

2. **回放工具根修**
   - `scripts/replay_ext_entry_sources.py` 不再尝试导入已退役的 shadow `decide()`。
   - 回放 rule 臂直接复用 `scripts.extended_hours_profit_exit_rules.decide_original_profit_exit`。
   - 删除 `except ImportError -> hold` 静默降级；导入失败会直接失败。
   - 报告新增 `rule_arm_source=shared_original_profit_exit`。

3. **退出优先级**
   - 新增 `prioritize_profit_exit()`：`TAKE_PROFIT` / `SL_BREAKEVEN` / `SL_TRAIL_LOCK` 优先。
   - 盘后 `hold_to_next_open` 只在无有效止盈时兜底。
   - 盘前 `pre_close_flatten` 只在无有效止盈时兜底。
   - 盘后 carry 仓在次日 `09:30-09:45` 的 `next_open` 兜底路径恢复执行，并使用 RTH 限价单。

4. **账本字段**
   - 延长时段新开仓写入 `trade_records.entry_session`。
   - 既有 4 笔主行和 3 笔同步行已回填 `premarket`。
   - 使用既有 `scripts/backfill_fee_net.py` 回填 `fees_est` / `pnl_net`，备份为 `data/ftseek_us.db.bak_feenet_20260918_091553`。

5. **`ALLOW_SIM_ENTRY` 代码默认值裁决登记（2026-09-18，复核建议 5 要求落在 §17.1）**
   - 裁决：代码默认由 `"true"` 翻为 **`"false"`**（fail-open → fail-closed）。
   - 理由：开关现在真的在下单（SIM 走真下单路线），而 `".env.us:135"` 是显式 `EXT_ENTRY_ALLOW_SIM=true` ⇒
     运行时行为**不变**；翻默认只影响"`.env.us` 缺失/未加载/键名写错"这一失败模式 —— 那时应当**不开仓**而不是静默开仓。
   - 回滚：改回 `"true"`（单行）即可；配套测试 `test_entry_switch_default_is_fail_closed` 钉住源码字面量，回滚须同时改测试。
   - 与 §5.1 `real_enabled: false` 形成双保险（实盘仍被 `ALLOW_REAL_TRADING=false` 全局阻断）。

### 17.2 修复后 60 交易日回放

> ⚠️ **口径已过时（2026-09-18）**：下列两表的"净均值"用扁平 `FEE_PCT=0.085%`。已按 §23.2
> 换成逐笔 per-share 费用模型重跑（盘前 n=56 / 盘后 n=18），**引用以 §23.2 为准**；
> 结论方向不变（差值仅 −0.013~−0.032pp），但低价档费率被低估最多 3.3×。

#### 盘前

产物：`data/reports/replay_ext_entry_sources_latest_premarket_fixed.json`

| 臂 | n | 净均值 | 胜率 | 最差 | 最好 |
|---|---:|---:|---:|---:|---:|
| 共享利润保护 rule | 56 | -0.213% | 69.6% | -7.281% | +2.346% |
| 盘前收口 | 56 | -0.402% | 42.9% | -7.281% | +6.764% |
| RTH 收盘 | 56 | -0.101% | 42.9% | -13.413% | +15.575% |
| 盘中 -5% 兜底 | 56 | +0.237% | 41.1% | -7.281% | +15.575% |

结论：共享利润保护优于旧收口，但整体仍为负期望；不能作为开启新开仓的证据。

#### 盘后

产物：`data/reports/replay_ext_entry_sources_latest_afterhours_fixed.json`

| 臂 | n | 净均值 | 胜率 | 最差 | 最好 |
|---|---:|---:|---:|---:|---:|
| 共享利润保护 rule | 18 | -0.572% | 55.6% | -16.941% | +12.160% |
| 盘后收口 | 18 | -0.241% | 50.0% | -16.941% | +15.484% |
| 次日开盘 | 16 | -0.084% | 50.0% | -19.391% | +26.504% |
| 次日 RTH 收盘 | 16 | -1.587% | 50.0% | -23.424% | +33.501% |

结论：盘后共享利润保护当前为负期望，且尾部亏损很大；不能开启盘后新开仓。

### 17.3 验证

- 延长时段相关测试：`75 passed`。
- 回放工具 5 日样本验证：`rule_arm_available=true`。
- 完整盘前/盘后 60 交易日回放均成功完成。
- `py_compile` 通过。
- `git diff --check` 通过。

### 17.4 仍然阻塞新开仓

1. `trade_session_policy` SSOT 未落地。
2. spread / 深度硬闸未落地。
3. 候选质量排序未落地。
4. 统一事件流 / gate reason / policy version 未落地。
5. Preflight 与对账停止机制未落地。
6. 日亏损权威未裁决。
7. 30 笔统计判据需按 Hermes §16.4-8 改为高信噪比口径。
8. 修复后回放仍显示盘前/盘后新开仓负期望。

### 17.5 当前裁决

**继续保持 `EXT_ENTRY_ALLOW_SIM=false`。**

理由：退出优先级与回放工具已修复，但入场质量与可观测性尚未完成，且新开仓 cohort 的最新回放仍不支持开启。

## 18. Hermes 第二轮复核（2026-09-18，针对 §17 执行回执）

> 方法同上轮：读码（file:line）+ DB 实查 + 独立重算产物 + 实跑测试/门控探针。只读；本轮只改本文件。
> 结论：§17 的**产物与代码声称逐条复现**，但新增 2 个 🔴 与 1 个 🟡 —— 其中「next_open 恢复执行」实际**没有触发器**（死路换层，不是修好）。

### 18.1 §17 各条复现判定

| §17 声称 | 复核结果 | 证据 |
|---|---|---|
| 身份批独立提交 `e03b30874`，策略修复未混入 | ✅ | `git show --stat`：11 文件（含未跟踪的 `app/core/trade_identity.py` 与 `tests/core/test_trade_identity.py`）；策略修复仍在工作区未提交（9 文件 M + 1 新测试 ??） |
| 回放工具根修：不再导入退役 `decide()`、复用共享纯函数、删静默降级 | ✅ | `replay_ext_entry_sources.py:32/:63` import+调用 `decide_original_profit_exit`；旧 `except ImportError → hold` 已删；summary 出 `rule_arm_source=shared_original_profit_exit` |
| 新增 `prioritize_profit_exit()` 并接线优先级 | ✅ | 纯函数 `extended_hours_profit_exit_rules.py:11-16`；两影子接线 `afterhours_exit_shadow.py:206`、`premarket_exit_shadow.py:299`；重现性测试 `test_extended_hours_original_profit_rules.py:27` |
| 盘后 carry 仓次日 09:30–09:45 `next_open` 兜底恢复、用 RTH 限价 | ⚠️ **代码已恢复但无法被触发**（🔴 见 18.3-①） | 执行器 `:42-43` 加窗 + `:207 order_session="RTH"` + `:155-158` 非 SIM 直接拒；但唯一调度器在 09:30–09:45 判窗外 |
| 延长时段新开仓写 `entry_session` + 回填 7 行 | ✅ | 4 主行 + 3 同步行现均 `entry_session='premarket'`；另有 4 行仍空（CST 格式的 playbook 行，非本议题） |
| `fees_est`/`pnl_net` 回填（含备份） | ⚠️ **部分错**（🔴 见 18.3-②） | 备份 `data/ftseek_us.db.bak_feenet_20260918_091553` 存在；4 主行净额 = 毛 − 费 ✅；但 3 笔重复标记行被写入幻影净额 −115.54 |
| §17.2 盘前表（56 笔四臂） | ✅ 逐格复现 | rule −0.213%/69.6%/−7.281%/+2.346%；收口 −0.402%/42.9%；RTH 收盘 −0.101%；盘中 −5% +0.237% |
| §17.2 盘后表（18/16 笔） | ✅ 逐格复现 | rule −0.572%/55.6%/−16.941%/+12.160%；盘后收口 −0.241%；次日开盘 n=16 −0.084%；次日 RTH −1.587% |
| §17.3 延长时段测试 75 passed | ⚠️ 计数不可核 + **有 1 个红**（🟡 见 18.3-③） | 我跑 22 个 ext 相关文件：**144 collected / 1 failed**；红 = 旧契约文本断言 |
| §17.4 阻塞项 8 条 / §17.5 保持关闭 | ✅ 与 §16.4 对应；裁决方向同意 | 见 18.4 |

**§16.4 阻塞项闭环进度**：1（工具）✅ 已修但样本不可复现（18.2a）；2（净口径）⚠️ 部分闭环 + 新增回归（18.3-②）；3（`entry_session`）✅；6（Phase 0）✅；7（`next_open`）⚠️ 代码 ✅ / 触发 ✗；4（事件流）、5（标签生产者）、8（§8 判据）仍待办（§17.4 已登记 4/5/8）。

### 18.2 本轮新增复算（4 项）

**(a) 回放样本不可复现 → 跨版本比较不配对**
同一条命令两次运行（07:13 旧产物 vs 09:22 新产物）样本不同：**12 dropped / 8 added**（如旧样本有 MU ×7、BABA、INTC/MRVL/NVDA/SNXX 8/27 一批，新样本多出 SMCI/SNDX/SKHY/INTC 等）。原因：候选宇宙由**当前** watchlist + **当前** 财报超跌榜反推历史，属"今天的名单投影到过去"。
⇒ 因此"共享利润保护优于旧收口"只能取**同文件内配对**检验：rule − 收口 = **+0.189%**，sd 2.261，**t = 0.62**，95% CI **[−0.404%, +0.781%]**，26/56 更优、10 平 ⇒ **方向成立、量级未证**。
建议：给回放工具加 `--universe-from <report.json>`（复用既有 `all_candidates`），把"版本对比"固定在同一样本上；否则每次回放都是新样本，拼不出双窗口/版本 A-B。

**(b) 负期望有机制归因：全部来自"低涨幅 + 无浮盈可锁"档**
盘前新产物按 `rule_action` 分层：

| rule_action | n | 净均值 | 说明 |
|---|---:|---:|---|
| SL_TRAIL_LOCK | 32 | **+0.709%** | 峰值武装后回落锁盈 |
| SL_BREAKEVEN | 14 | −0.035% | 保本线出场 |
| **hold（无有效止盈信号）** | **10** | **−3.414%** | 合计 **−34.14%**，独自超过全体 −11.94% |

`hold` 组 10 笔的 chg 全部落在 **2.1%–3.8%**（无一 ≥5%），MFE 全部 ≤0.53%（开盘即蒸发 ⇒ 永远不会武装 BE/TRAIL）⇒ 只能等 09:20 收口割在低位。
**非 hold 组 n=46 = +0.483% / WR 84.8%。**
盘后同构：≥3% 的 10 笔三臂全正（rule +0.654% / 盘后收口 +1.414% / 次日开盘 +2.026%），<3% 的 8 笔把整组拉负。
⇒ 两时段的负期望都集中在**同一档**，最高杠杆是 **Phase 3 的抬闸 + 排序**（机制解释：低涨幅档没有浮盈可锁，任何"利润保护"都无从触发），而不是继续调退出规则。同时 §17.2 盘后的措辞宜由「当前为负期望」改为「n=18 证据不足，且负值集中于 <3% 档」（裁决不变，理由更准）。

**(c) 费用口径：回放的扁平 8.5bps 对低价股低估 1.2–6.2 倍**（逐笔真实费重算）
真实 `fee_model.estimate_round_trip`（per-share，$5000 名义）折算费率随价格带：

| 入场价 | 往返费 | 占名义 | 对比扁平 0.085% |
|---|---:|---:|---:|
| $5 | $26.30 | **0.526%** | 6.2× |
| $10 | $13.20 | 0.264% | 3.1× |
| $20 | $6.65 | 0.133% | 1.6× |
| $30 | $5.11 | 0.102% | 1.2× |
| $50 | $4.70 | 0.094% | 1.1× |

逐笔真实费重算（盘前 n=56）：

| 口径 | 扁平 8.5bps | 真实 per-share |
|---|---:|---:|
| rule 全量 | −0.213% | **−0.245%** |
| rule ≥3% | +0.184% | **+0.146%** |
| rule ≥5% | +0.425% | **+0.390%** |
| 收口 ≥3% | +0.106% | +0.068% |
| px $10–20 档（n=12） | — | **−0.284%**（平均费 0.184%） |

⇒ 三件事：①回放应逐笔调 `estimate_round_trip`，不用扁平常数；②§13.1 的待决问题应从"spread 是否分档"扩为**"费用 + spread 双分档"**，因为费用在高价股≈0.09%、在 $5 股≈0.53%，跨价格带差 6 倍；③$5 价格下限只够"不被仙股拖死"，应给出**「价格带 × 所需最小 edge」表**（如 $5 档需 >0.53%、$20 档需 >0.13% 才过费用线），否则"≥3% 涨幅档 +0.146%"里的低价样本仍可能是负贡献。（注：4 笔实盘样本的 `fees_est` 与此模型一致 —— CTNT $5000/0.072 ≈ 69444 股 → 费 $451.26 = 9.0% 名义，模型忠实，问题在回放没用它。）

**(d) 如实登记：我在 §16.2b 的警告被本轮证据部分推翻**
旧阈值臂下 rule ≥3% 是 **−0.104%**；换成共享利润保护后同档为 **+0.184%**（真实费 +0.146%）。⇒ **Phase 2 的「有效止盈 > 兜底」方向在 ≥3% 档成立**，我上轮的"缺证据"结论现在有了证据。
但仍需保留两点边界：①≥5% 档收口仍略胜（真实费 +0.437% vs +0.390%）；②整体配对不显著（18.2a）。⇒ 正确定性是"方向成立、量级未证"。

### 18.3 新增/未闭环问题（按优先级）

**① 🔴 `next_open` 兜底"恢复了代码但没有触发器"（死路换层）**
- 事实：兑现窗 09:30–09:45 ET 需要 `premarket_exit_shadow.py` 在那个时点运行才能发出 `next_open_exit`；而唯一调度器 `extended_hours_exit_cron`（crontab:284，每分钟）用 `get_extended_hours_session()` 判定，`extended_hours_session_for()` 只在 **04:00–09:29** 返回 `premarket`、其余（含 09:30/09:35/09:45/09:46）返回 **None**。
- 实测（纯函数探针）：ET 0919→premarket，**0930/0935/0945/0946/0955 全为 None**，1600→afterhours；`extended_hours_exit_cron.py --dry` 窗外返回 `{"skip":"outside_extended_hours"}`。
- 后果：影子脚本在该窗**根本不运行** ⇒ `next_open_exit` 永不发出 ⇒「盘后 carry 仓次日 09:30–09:45 兑现」这条 §17.1 声称的恢复**在生产不可达**。DEB-20260917 §16 的"cron 已覆盖 09:30–09:45 ET，无需改 crontab"在 §19 换成 ET 日历门控后**已失效**（作者是按旧固定窗写的）。
- 二选一（删门 > 加守卫 的取舍）：**(a)** 在 session 判定里显式加 `next_open` 窗（09:30–09:45，并同步 §19.1 的窗定义与 cron 映射），或 **(b)** 承认"开盘兑现交给盘中引擎"（§20.7 的原意），把 `next_open` 分支连代码/影子动作/回放臂一起删掉。**不要保持现状**——它现在是一条"看起来通、实际到不了"的路，比显式禁用更难发现。

**② 🔴 幻影净额回归（复活了今日刚修的 F1）**
- 事实：`id=1523/1525/1527`（`data_quality='duplicate_external_close'`，即 F1 修复刻意置 NULL 的三行）现带 `pnl_net = −100.82 / −4.22 / −10.50`（合计 **−115.54**），而 `realized_pnl` 仍为 NULL（合计 0.0）。
- 根因：`scripts/backfill_fee_net.py:70-72` 只按 `exit_time IS NOT NULL AND exit_price > 0` 选行、**无 data_quality 过滤**；`:84` `gross = float(r["realized_pnl"] or 0)` ⇒ 毛 0 − 费 = 净额负数。当日 F1（d56612164）建立的口径是"重复标记行留 NULL，让净额求和自动排除（`daily_invariant_check.py:32/:998` 约定）"，该回填即其复活路径。
- 影响：任何不过滤 `data_quality` 的净额消费方（日报/复盘/OKR）会多算 −115.54，凭空制造两笔"低涨幅延长时段亏损"。
- 修法（一行 + 三步）：候选集加 `AND realized_pnl IS NOT NULL`（或显式排除 `duplicate_external_close`）→ 重跑把这三行置回 NULL → 补回归钉"重复标记行不得获得 `pnl_net`" → grep 全部 `pnl_net` 消费方确认过滤。**这是本轮最该立刻做的一件。**

**③ 🟡 本批引入的存量红：旧契约测试未同步**
`tests/test_premarket_close_flatten.py:60-65` 用源码文本断言钉住**旧语义**（`assert 'action = "pre_close_flatten"' in src`、以及"收口强平覆盖 TAKE_PROFIT/BE/TRAIL"的代码块）——重构后字面量变成 `fallback = "pre_close_flatten"` 且语义被反转 ⇒ 该文件未随批更新（`git status` 干净、最后提交 66d781dcc），全量 ext 相关 144 collected / **1 failed**。
处置：把该测试改写为**新契约**（`prioritize_profit_exit(action, fallback="pre_close_flatten")`，断言 TP/BE/TRAIL 胜出、flatten 仅兜底），或显式登记豁免。带红入库等于把归因负担转给下一批复核。
（同文件另两个测试语义仍成立，已通过：无利润信号时 flatten 触发、09:19 不强平。）

**④ 🟡 测试计数需附文件清单**：§17.3 的"75 passed"与我这两轮的 22 文件 144 collected 不可比。建议写成"文件集 + 数字"，否则下次复核无法判定是"少跑了文件"还是"红被略过"。

**⑤ ℹ️ 行为变更需登记解除来源**：恢复 `next_open` 是**行为变更**（DEB-20260917 §20.7 曾显式禁用）。建议在 §17.1 注明"由谁解除、为何"，并保留已验证的约束（`:207` RTH 限价、`:155-158` 非 SIM 直接拒）。

### 18.4 对 §17.5 裁决的意见

**同意保持 `EXT_ENTRY_ALLOW_SIM=false`**，且本轮证据加强了它：负期望虽已定位到具体档（<3% 涨幅 + 无浮盈可锁），但那一档正是 Phase 3（抬闸 + 排序）尚未落地的部分；同时盘后 carry 仓的次日兑现路径**当前不可达**（18.3-①），若此刻开仓会多出一批"无兜底、无触发器"的隔夜仓。
建议裁决文字补一句：**"负期望的贡献者是 <3% 涨幅档（盘前 hold 组 10/56、盘后 <3% 8/18），转正判据落地前必须先完成抬闸/排序 + 费用分档，不得以放宽闸门或加大配额换取样本速度。"**

### 18.5 复核边界（如实）

- 只读复核：未跑实弹回放（重算基于 §17 的两份产物），未连券商，未跑全量 `tests/`（只跑 ext 相关 22 文件）。
- 18.3-① 的定谳依据是 **session 纯函数在 930/935/945/946 全返回 None** + cron `--dry` 真实窗外返回；未在真实 09:30 时点实跑（该时点不在本会话内），但门控是纯函数、判定链闭合。
- 费用重算用 `app/execution/fee_model.estimate_round_trip`（用户提供的富途费率表，模型本身已由 4 笔实盘样本交叉验证一致）；未争论费率取值，只指出回放未使用它。
- 配对检验 n=56（盘前）/ n=18（盘后），均单窗口；σ 与显著性结论随窗口变化，方向稳健、量级不可外推。

## 19. Codex 第二轮修复回执（2026-09-18，针对 §18.3）

> 本节只处理 Hermes 标出的三个立即阻塞项，未打开新开仓，未触碰真实交易通道。

### 19.1 `next_open` 调度修复

- `scripts/extended_hours_exit_cron.py` 新增局部 `get_exit_cron_session()`：先沿用现有 premarket/afterhours 判定，再在 ET 09:30–09:45（09:30 含、09:45 不含）且为美股交易日时返回 `next_open`。
- `collector_command("next_open")` 复用 `premarket_exit_shadow.py`，不新增第二套 collector，也不修改全局 `extended_hours_session_for()` 的语义，避免影响其它消费者。
- `run_once(now=None)` 透传固定时间探针；新增测试钉住 09:30/09:35 触发、09:45/09:46 不触发，以及 `next_open` 必须派发 premarket shadow。
- 保持既有约束：执行器侧仍用 RTH 限价；非 SIM 通道仍拒绝，不因此打开实盘。

### 19.2 重复外部平仓行净额污染修复

- `scripts/backfill_fee_net.py` 候选集新增 `realized_pnl IS NOT NULL`，防止未实现盈亏行被 `NULL → 0` 转换后产生幻影亏损。
- 新增 `repair_duplicate_external_close_rows()`：`data_quality='duplicate_external_close' AND realized_pnl IS NULL` 的行，`fees_est / pnl_net / fees_source` 一律清回 NULL。
- 实际执行 `--apply`：新备份 `data/ftseek_us.db.bak_feenet_20260918_094156`；全表候选 864 行，清理污染行 3 行。
- DB 复核：`id=1523/1525/1527` 的 `realized_pnl / fees_est / pnl_net / fees_source` 均为 NULL，`data_quality` 保持 `duplicate_external_close`。
- 新增回归测试钉住重复标记行不得获得净额，且不影响已实现盈亏行。

### 19.3 旧契约测试同步

- `tests/test_premarket_close_flatten.py` 的源码文本断言改为新语义断言：
  - `TAKE_PROFIT / SL_BREAKEVEN / SL_TRAIL_LOCK` 优先；
  - `pre_close_flatten` 只在无有效止盈信号时兜底。
- 保留同文件其它有效测试：无利润信号时 flatten 触发、09:19 不强平、隔夜/legacy 仓不进 flatten cohort。

### 19.4 定向验证

先跑定向 8 文件，结果 `52 passed, 1 warning`。随后按关键词 `extended_hours|afterhours|premarket|EXT_|ext_entry|ext_take` 选择完整相关集（51 个测试文件，含本批新增测试），结果：

```bash
./venv/bin/python -m pytest -q $(rg -l \
  "extended_hours|afterhours|premarket|EXT_|ext_entry|ext_take" \
  tests --glob '*.py' | sort -u)
```

结果：**51 files / 1076 passed**（90.63s）。定向运行的 1 个 warning 为现有 futu/pytest deprecation，非本批行为问题。

静态检查：相关脚本 `py_compile` 通过；`git diff --check` 通过。

### 19.5 裁决不变

**继续保持 `EXT_ENTRY_ALLOW_SIM=false`。**

本轮只修复 Hermes 标出的执行断链与数据污染；入场质量、费用分档、spread/深度硬闸、候选排序与统一 gate reason 仍未闭环，且现有回放仍不支持开启新开仓。

## 20. 用户裁决：新开仓小流量试运行（2026-09-18）

> 用户明确令「启用新开仓，先看看流程和链路是否正常合理，定期优化调整」。本节登记执行边界：这是流程观察，不是转正证据。

### 20.1 启用与护栏

- 运行时配置：`EXT_ENTRY_ALLOW_SIM=true`，仅 SIM 开仓。
- 实盘仍全局阻断：`ALLOW_REAL_TRADING=false`；执行器继续拒绝 REAL 环境。
- 观察期日配额：`EXT_ENTRY_MAX_PER_DAY=3`，盘前+盘后合计共享，单笔仍为 `$5000`。
- 涨幅阈值暂保持 `2.0%`，不与本轮开关一起调整，避免一次改变多个变量。
- 回滚条件：链路异常、重复开仓、错误标的、日亏损失控或数据不可结算时，立即改回 `EXT_ENTRY_ALLOW_SIM=false`。

### 20.2 当前链路

1. 入口调度：crontab 每 5 分钟分别运行盘前/盘后 entry executor。
2. 候选过滤：异动榜候选依次过价格下限 `$5`、涨幅 `≥2%`、延长时段量 `≥20万股`、财报超预期 `>0`。
3. 防重复：已有持仓跳过；同标的当日一次 attempt；活动订单阻断；日配额合并盘前/盘后。
4. 执行记账：SIM 限价买入，未成交超时撤单；成交后写 `open_positions` / `trade_records`，记录 `entry_session` 与盘中止损元数据。
5. 退出调度：exit cron 每分钟运行，有效 `TAKE_PROFIT / SL_BREAKEVEN / SL_TRAIL_LOCK` 优先；盘后仓无有效止盈时持有到次日开盘兑现。
6. 结算观测：沿用 `settle_extended_hours.py` 与既有延长时段台账，不新增第五套事件文件。

### 20.3 验证

- 当前 ET 在交易窗外，`--session afterhours --dry` 返回 `{"ok": false, "skip": "outside_trading_window"}`，窗外 fail-closed 行为正确。
- 入口/退出定向测试：`45 passed`。
- 将“日配额默认 5”的本机环境敏感测试改为子进程隔离的 env 覆盖测试，避免 `.env.us` 运行态造成测试漂移。

### 20.4 定期优化机制

- 每个交易时段后：检查 `ext_entry.log`、entry state、价格闸 rejects、开仓结果、退出执行与结算报告。
- 每日复盘：按 session、价格带、涨幅档、费用、MFE/MAE、捕获率分层看净结果。
- 达到 30 笔已结算样本后：再评估是否调整涨幅阈值、候选源、日配额或加 spread/深度硬闸。
- 调整原则：一次只改一个变量，并用固定候选宇宙回放做前后对比；不得以放宽闸门换取样本速度。

### 20.5 风险边界

现有 60 交易日回放仍显示盘前/盘后新开仓整体负期望，且 spread/深度硬闸、候选排序、统一 gate reason 未完全落地。本次启用仅用于验证真实链路与积累可观测样本，不等价于策略转正。

## 21. Hermes 第三轮复核（2026-09-18，针对 §19 修复与 §20 启用）

> 只读复核 + 实跑探针（纯函数 / `run_once(now=...)` 固定时刻 / dry 扫描 / 子进程），**未下单、未改生产状态**。

### 21.1 §19 三项修复复现

| 项 | 结果 | 证据 |
|---|---|---|
| `next_open` 调度修复 | ✅ 生效 | 运行期探针：ET 09:29→`premarket`；**09:30 / 09:35 / 09:44→`next_open` 且派发 `premarket_exit_shadow.py`**；09:45 / 09:46 / 09:55→`None`；周六 9/19 与感恩节 2026-11-26 的 09:35→`None`。**§18.3-① 关闭** |
| 重复外部平仓行净额污染 | ✅ 根因级修复 | `1523/1525/1527` 的 `realized_pnl/fees_est/pnl_net/fees_source` 全 NULL；**广义扫描全表 `realized_pnl IS NULL AND pnl_net IS NOT NULL` = 0 行**；`abs(pnl_net −(realized_pnl − fees_est)) > 0.02` = 0 行；`duplicate_external_close` 组净额合计 `0.0`。**§18.3-② 关闭** |
| 旧契约测试同步 | ✅ | `test_premarket_close_flatten.py` 等 6 文件 = **27 passed**（含新契约断言） |
| §19.4 计数 `51 files / 1076 passed` | ✅ 独立复现 | 同关键词集实跑：**1076 passed / 0 failed**（91.5s） |
| §19.5「裁决不变」 | ✅ 与 §18.4 一致 | 本轮复核时开关仍为 false，§20 之后才启用 |

### 21.2 §20 启用核查（全部实测）

- **配置真实生效**：`.env.us` = `EXT_ENTRY_ALLOW_SIM=true` / `EXT_ENTRY_MAX_PER_DAY=3` / `ALLOW_REAL_TRADING=false`；`FUTU_TRD_ENV` 未设 → 代码默认 `SIMULATE`。`load_dotenv`（`:45`）早于常量读取（`:86`）⇒ 开关不会被加载顺序吃掉。
- **实盘硬门在位**：执行器 `:605` / `:794` 非 SIM 直接 `real_not_authorized`；下单前安全闸 `:804` 显式传 `today=_today_et()`（跨日旧单判据正确；`:458` 语义仍是"无 today 才全量处理"，无 `== None` 陷阱）。
- **调度**：两条 entry cron 均无 `--dry`，注释已改为「日配额3笔(2026-09-18观察期)」；exit cron 每分钟（现含 `next_open` 窗）。
- **部署无需重启**：本两批（`bb5a4c3b4` / `ebf5f517d`）**未触碰 `app/`**，服务进程（09:00:20 启动）不加载这些脚本；entry/exit 每轮新进程 ⇒ 新代码已直接生效。
- **窗口与配额**：ET `2026-09-18` 为交易日 ✅；`_opened_today_total` 用 `startswith(today)` ⇒ 新 ET 日从 0 起（state 现有 4 笔属 ET 9/17，不占 9/18 配额）；窗外 `--session afterhours --dry` → `outside_trading_window` ✅。
- **实弹链预检（dry，不下单）**：真实盘前榜 50 候选 → `$5` 价格闸拦 **18 只（36%）** → **3 只过四闸**（AEHL `$12.47/+35.3%`、GNRC `$230.8/+31.8%`、MGNI `$26.71/+12.3%`）⇒ 扫描 / 四闸 / 拦截打点 / 报价链全通。
  ⚠️ 该次运行在 ET 22:10（窗外），榜单仍是 9/17 盘前快照，这 3 只**不是**明天的候选；且 **AEHL 正是 9/17 亏 $160 的那只**（财报 `period=2012`，超预期>0 仍过事件闸）⇒ 「陈旧财报」类在观察期仍会进样本。

### 21.3 观察期需盯的三件（不阻塞启用）

1. **首窗时间与标的画像**：盘前 **今天 16:00 CST（ET 9/18 04:00）** 起每 5 分钟；盘后 **明天 04:00–07:55 CST**。涨幅阈值 2.0% 未随开关调整（"一次只改一个变量"）⇒ 首批样本大概率落在 **<3% 档**，而上一轮实证显示该档承载了全部负期望（盘前 `hold` 组 10/56、盘后 <3% 8/18）。这是**知情选择，不是缺陷**；但复盘必须按 **(涨幅档 / 价格带 / 费用% / earnings period 年龄)** 记录归因 —— 前两项 `extended_hours_entry_executions.jsonl` 已有，`entry_session` 已落库，费用% 可由 `estimate_round_trip` 事后算，无需新增字段。
2. **`ALLOW_SIM_ENTRY` 代码默认仍是 `"true"`（fail-open）**：`.env.us` 缺失/未加载 = 默认开仓。开关现在真的在用，建议翻成 `"false"`（§16.5 已提，未采纳）。
3. **§20.4 的"30 笔后评估"需换成高信噪比判据**：净均值 n=30 的 95% CI 为 ±0.42%，检出 +0.15% 需 n≈481（§16.2c）⇒ 主判据应为执行质量二值项（限价成交率、部分成交/超时撤单、对账零冲突）+ 分档归因，净均值降为观察项（同 §16.4-8）。

### 21.4 复核边界（如实）

- **未在真实窗内实跑**（复核时 ET 22:1x，三时段窗口全关）：`next_open` 与入口链结论来自固定时刻探针（`run_once(now=...)`）、纯函数探针与 dry 扫描，非窗内活体。
- 未连券商下单、未改 state/DB/配置；本轮只读 + 写本文件。
- 首个真实窗的"是否真按预期开仓/被拦"仍待 16:05 CST 起的实跑证据 —— 那才是本 DEB 链路的第一次活体考试。

## 22. 2026-09-18 用户确认后的补闸（不完全禁用）

### 22.1 落地项

- `.env.us` 保持 `EXT_ENTRY_ALLOW_SIM=true`、`EXT_ENTRY_MAX_PER_DAY=3`；代码默认 `MAX_PER_DAY` 同步为 3，环境缺失时不再回退到 5。
- 新增下单前硬闸（均可 env 调整，不整体关停）：
  - `EXT_ENTRY_MAX_SPREAD_PCT=1.0`
  - `EXT_ENTRY_MAX_ROUND_TRIP_FEE_PCT=1.0`
  - `EXT_ENTRY_MAX_PRICE_DRIFT_PCT=0.5`
  - `EXT_ENTRY_MIN_ASK_DEPTH_RATIO=1.0`
- 买单价改用有效买一价并按价格精度取整：<$1 保留 3 位，其余保留 2 位，规避 AEHL 4 位小数被券商拒绝的问题。
- 数量按买一价重算，确保单笔名义额仍不超过 `POSITION_USD=$5000`。
- 快照缺失、盘口无效、spread/深度/漂移/费用超限时，该候选被拒绝且不占用 attempt / 日配额。
- 对账回补成功的新仓会同步回填 `opened_today`，state 丢失时不再出现配额重置式超开。

### 22.2 退出链补丁

- 盘前/盘后 shadow 父进程超时~~改为 `max(90, EXT_EXIT_LIMIT_TIMEOUT_S + 40)`~~ →
  **2026-09-18 Hermes 复核 (§23 R2) 实锤该改法是空操作**：`EXT_EXIT_LIMIT_TIMEOUT_S` 全仓只有测试设过
  （默认 20）⇒ `max(90, 20+40) = 90`，与改动前硬编码的 `timeout=90` 逐字相同；而实测子进程正是
  **>90s 才被杀**（`logs/premarket_exit_shadow.log-20260918`，US.CRM 那轮）⇒ 失败模式原样保留。
  已改为**链条单调**的三档（同一处代码 + 测试钉住）：
  `cron 240s（extended_hours_exit_cron.run_once）> shadow 180s（_executor_timeout = max(180, t+90)）> 子进程限价等待 20s`。
  只抬 shadow 不抬 cron 会更糟（cron 先杀 shadow，孙进程仍在跑 ⇒ 孤儿单 + 零证据），故两者同批改。
  另：TimeoutExpired 时把子进程的**残留 stdout/stderr** 打进日志（原实现只打异常字符串，
  "下单了没/撤单确认了没"无从判断）。
- `ExitLifecycle` 保留 ownership fail-closed，但在拦截时输出阻塞订单 ID、状态和 remark，便于定位孤儿 ETH 单。
- 专项复盘见 `docs/debates/DEB-20260918-crm-overnight-gap-review.md`。

### 22.3 回归

- `tests/test_extended_hours_entry_executor.py` 覆盖盘口缺失、宽 spread、深度不足、买一漂移、费用过高与有效盘口通过。
- `tests/test_ext_take_profit_only.py` 覆盖父进程超时永远大于 durable exit 限价超时（**2026-09-18 更新为链条单调性测试**：`cron > shadow > 子进程限价等待`，并钉住旧值 90 不再是返回值）。
- `tests/execution/test_exit_lifecycle.py` 覆盖 active order 拦截诊断日志。
- `tests/test_extended_hours_entry_executor.py` 新增 `test_entry_switch_default_is_fail_closed` 钉住 `ALLOW_SIM_ENTRY` 的 fail-closed 代码默认。

## 23. Hermes 第四轮复核与落地回执（2026-09-18，针对 §22）

> 方法：逐条读码（file:line）+ DB 实查 + 独立重算回放产物 + 实跑日志取证。**用户令「按建议执行」后，本轮为落地回执**。

### 23.1 复核发现与处置（R1–R9）

| # | 复核发现 | 证据 | 处置 |
|---|---|---|---|
| R1 | §4.1 表用**毛口径**，把当日最大亏损低估 **7.9×**（−82.02 → −648.86），并把 CTNT 记成"盈利样本"（实为当日最大亏损，费用占名义 9.15%） | `trade_records` 1521/1522/1524/1526 的 `realized_pnl/fees_est/pnl_net` | ✅ 已改净口径 + 费用/名义列（§4.1） |
| R2 | §22.2「父进程超时已修复」= **空操作**：`max(90, 20+40) = 90` 与改动前硬编码逐字相同；实测子进程正是 >90s 才被杀 | `premarket_exit_shadow.log-20260918:195`（US.CRM 那轮 TimeoutExpired 90s） | ✅ 改链条 `cron 240 > shadow 180 > 子限价 20` + 残留输出打点 + 链条测试（§22.2 已更正） |
| R3 | CRM 复盘叙事不准：09:34:53 日志 `price=$242.39 触及 SL=$242.53` ⇒ 是**破 SL 后被执行阻塞**，不是"规则主动离场"；`exit_slippage_bps=-66.01` ⇒ 成交价**高于**触发价 66bps（这次延迟反而"赚"≈+$36.8，但不改变控制失效性质） | CRM 复盘 doc + `trade_records` id=1518 metadata | ✅ 见 CRM 复盘文末更正 |
| R4 | 14 分钟阻塞的**真实代价 = 205 条 ERROR 日志**（CRM 70 / GIPR 60 / US.TEST 75，同一 intent 反复重试），推送侧去重后 7 条；失败**不止 CRM** ⇒ 与 9/16 的孪生行幻影不同源（这次是真实存活孤儿单） | 影子日志逐行计数 + 钉钉推送记录 | ✅ 见 CRM 复盘文末更正 |
| R5 | `OVERNIGHT_GAP_STOP` 缺聚合：历史 4 次净 **−1,179.26**（GAP −386.77 / UBER −272.62 / SNDK −251.82 / CRM −268.05），3 次落在 09:30–09:38，只有 CRM 拖到 09:49（即被阻塞那笔） | `trade_records` 按 exit_reason 聚合 | ✅ 单笔复盘已挂档位聚合（CRM 复盘文末 R5）：该原因是**稳定的 ~$270/笔 档位**，不是单笔异常；是否调参留待该档位样本继续累积 |
| R6 | **无孤儿单回收器**：251.27 卖限价单存续约 4 小时（05:50→09:48:52 券商撤销），期间该多仓**没有有效下行保护**（挂单在价上方，守卫只能 fail-closed） | §22.2 新增的 `Exit blocked by active order` 日志 + 券商订单时间 | ✅ 方案 A 已实施（`scripts/orphan_ext_order_watch.py`，纯观察只告警，见 §23.3）；方案 B（撤单+attempt 清理）未做，需签字 |
| R7 | 订单级明细**不可独立复核**（旧判：Futu SIM `history_order_list_query` 返回 n=0） | 券商 API 实测 + DB | ⚠️ **2026-09-18 自证伪并更正**：`order_list_query` **能**查到这两个单 —— `8185508` = US.CRM SELL 23 @251.27 / `CANCELLED_ALL` / create **05:50:13 ET** / remark `ext_exit_tighten` / session **ETH**；`8186198` = SELL 23 / `FILLED_ALL` / create **09:48:59 ET** / remark `pbe_…` / session RTH。⇒ 订单级**身份/状态/创建时间可复核**（旧判错在查的是 history 端点）；**2026-09-18 末次闭环**：两个时刻一个已解释、一个已界定 —— ① `update_time` 为空 ⇒ 券商侧仍不能逐字证实"09:48:52 撤销"，但 app 侧 hold 循环把它界定到 **(09:48:46, 09:49:01] ET**（09:48:46 仍 `Exit held … exit_unowned_active_order`，09:49:01 该 tick 已放行并提交新市价单）；② 8.6s 差**已解释**：`trade_records.exit_time` 记的是**头寸同步(tombstone 确认)时刻**而非券商成交时刻 —— `logs/ftseek_us.log-20260918` `21:49:11,710 [US Sync] tombstone 确认 US.CRM` 与 DB `09:49:11.68` 相差 0.03s |
| R8 | §22 批次未提交（`exit_lifecycle.py` + 两 shadow + 本 DEB 均为工作区 M，最后一次 DEB 提交是 `ebf5f517d`）；`ALLOW_SIM_ENTRY` 代码默认仍是 `"true"`（fail-open） | `git status` / `git log` | ✅ 代码默认翻 `"false"`（行为不变：`.env.us:135` 显式 true；仅 env 缺失时 fail-closed）+ 测试钉住；提交见 §23.4 |
| R9 | 回放仍未用真实费用（`replay_ext_entry_sources.py` 只有扁平 `FEE_PCT=0.085`） | 读码 + 复算 | ✅ 已改逐笔 per-share 费用模型（与现役 `round_trip_fee_metrics` 同源）+ 费用分档，读数见 §23.2 |

### 23.2 费用保真复算（R9 落地读数，60 交易日 / 窗口 2026-06-23→09-17）

产物：`data/reports/replay_ext_entry_sources_latest_{premarket,afterhours}_feeaware.json`
（旧产物已备份至 `data/reports/backup_20260918_r9/`）。费用改为**逐笔**
`estimate_round_trip(qty=int(5000/入场价), …)`；交叉验证：对 9/17 四笔实弹复算 = DB `fees_est`
**451.26 / 100.86 / 4.22 / 10.50**（逐笔一致）。

**盘前（n=56）**

| 臂 | 净均值(真实费用) | 净均值(旧扁平 8.5bps) | 差值 | 胜率 | 最差 | 最好 |
|---|---:|---:|---:|---:|---:|---:|
| 共享利润保护 rule | **−0.245%** | −0.213% | −0.032pp | 66.1% | −7.475% | +2.262% |
| 盘前收口 | **−0.433%** | −0.402% | −0.031pp | 42.9% | −7.475% | +6.752% |
| RTH 收盘 | **−0.132%** | −0.101% | −0.031pp | 42.9% | −13.530% | +15.557% |
| 盘中 −5% 兜底 | **+0.206%** | +0.237% | −0.031pp | 41.1% | −7.475% | +15.557% |

**盘后（n=18 / 次日臂 n=16）**

| 臂 | 净均值(真实费用) | 净均值(旧扁平 8.5bps) | 差值 | 胜率 | 最差 | 最好 |
|---|---:|---:|---:|---:|---:|---:|
| 共享利润保护 rule | **−0.585%** | −0.572% | −0.013pp | 55.6% | −16.943% | +12.144% |
| 盘后收口 | **−0.254%** | −0.241% | −0.013pp | 50.0% | −16.943% | +15.468% |
| 次日开盘 | **−0.098%** | −0.084% | −0.014pp | 50.0% | −19.401% | +26.488% |
| 次日 RTH 收盘 | **−1.601%** | −1.587% | −0.014pp | 50.0% | −23.442% | +33.485% |
| 仅 −1.5% 止损（新增对照） | **+0.596%** | — | — | 38.9% | −7.688% | +15.468% |

配对检验（盘前 rule − 收口）：mean **+0.189%**，sd 2.281，**t=0.62**（CI 含 0）⇒ 与旧口径一致，无显著差异。

**费用分档**（按入场价）：

| 价格档 | 盘前 n | 盘前平均费率 | 盘后 n | 盘后平均费率 |
|---|---:|---:|---:|---:|
| $5–10 | 1 | **0.279%** | 0 | — |
| $10–20 | 12 | 0.184% | 1 | 0.142% |
| $20–50 | 13 | 0.102% | 6 | 0.105% |
| ≥$50 | 30 | 0.091% | 11 | 0.089% |
| <$5 | 0 | — | 0 | — |

**如实结论（方向与 §18.2c 一致，但量级要修正）**：
1. 本回放样本在 `$5` 价格闸下**最便宜的入场是 $5–10 档（盘前仅 1 笔，盘后 0 笔）**，故真实费用对**均值**的影响
   只有 **−0.013 ~ −0.032pp**（各臂同向），**不改变任何结论或臂排序** —— 上一轮"低估 1.2–6.2×"是**低价档**命题，
   在本样本里没有对应样本量；它在 **9/17 实弹样本**（CTNT $0.072 / GIPR $0.431）才真正致命（§4.1）。
2. 费率随价格单调下降（$5–10 档 0.279% = 扁平假设的 **3.3×**）⇒ 费用分档必须进报告口径；
   并且**这是支持 `$5` 价格闸（乃至更高下限）的独立证据**：低价档边际跳空样本本就承载全部负期望（§18.2b 同向）。
3. 配对检验仍为 t=0.62（不显著）⇒「rule 优于收口」仍**不能**当结论用，只作方向（同 §18.2a）。
4. 盘后新增对照"仅 −1.5% 止损"n=18 均值 **+0.596%**（胜率仅 38.9%）—— **但 n<40 且与 §17.2 的盘前结论冲突**，
   按功效门槛只能标"证据不足"，不得据此改盘后规则（真正的盘后问题是持仓期尾部的 −16.9%）。

> ⚠️ §17.2 的两张表（扁平 8.5bps 口径）**已被本节取代**，引用时以本节为准。

### 23.3 孤儿 ETH 单检测器（方案 A **已实施**，2026-09-18）

**问题**（R6）：延长时段的卖单在 `ExitLifecycle` 里被 fail-closed 守卫挡住时（`exit_unowned_active_order`），
孤儿单会一直存活到券商侧自行撤销/成交 —— CRM 那次 **4 小时**（05:50 → 09:48:52），期间该多仓
**没有任何有效下行保护**（挂单在价上方），而系统每分钟都在重复"被同一张单挡住"的失败（205 条 ERROR 日志）。

**已落地实现**（`scripts/orphan_ext_order_watch.py`，约 250 行 + 15 个测试）：

| 环节 | 实现 | 刻意的约束 |
|---|---|---|
| ① 触发 | `exit_intents` 里最近 15 分钟内 `state ∈ (PENDING,HELD)` 且 `last_error` 含 `exit_unowned_active_order` 的标的 | 用 durable 账本而非解析日志（结构化 + 有 intent 身份）；**无事件 ⇒ 零券商调用** |
| ② 确认 | 复用共享 `query_order_rows` **查一次**订单表，本地筛：`remark` 前缀 `ext_exit_` + 状态 ∈ `ACTIVE_ORDER_STATUSES`（正判据，避免与终态表漂移）+ `create_time` 年龄 ≥ 15 分钟 | 单次查询覆盖全标的（不按标的轮询） |
| ③ 落账 | `data/orphan_ext_orders.jsonl`，按 (symbol, order_id) 跨进程去重（cron 每轮新进程） | 每日告警上限 5 条，超限仍留痕只是不推 |
| ④ 告警 | 飞书（与延长时段执行器同通道），含订单号/状态/价格/年龄/intent | 每单只推一次 |
| ⑤ **不撤单** | 不动券商、不动 state | 撤当日陈旧孤儿单会消耗该标的当日 attempt ⇒ 静默掐掉当日机会（方案 B 才处理） |

- 挂载点：`extended_hours_exit_cron.run_once` 的**会话门控之前**（阻塞事件在 RTH 09:3x、孤儿单在 ETH 05:50，
  两者都落在本 cron 窗外 ⇒ 放门控之后就永远扫不到）。检测器异常**不阻塞退出链**，但错误会回填进 `result["orphan"]`（不静默）。
- env 开关（回滚用）：`EXT_ORPHAN_WATCH=false`（整关）/ `EXT_ORPHAN_MIN_AGE_MIN`（默认 15）/ `EXT_ORPHAN_LOOKBACK_S`（默认 900）/
  `EXT_ORPHAN_ALERT_MAX_PER_DAY`（默认 5）。另有 `--dry` 与 `--at` 便于回放。
- 单点回滚：删 `run_once` 里那次 `scan_orphan_orders()` 调用即回到"无检测器"。

**接线级验证（真实券商，2026-09-18 13:46 ET 01:46）**：探针查得订单表 38 行、其中 `ext_exit_*` 7 行
（CTNT/GIPR/NBIS/AEHL/CRM 的历史退出单），**全部为终态** ⇒ 检测器判定 **0 只孤儿、0 次写盘、0 次推送**
（正确的负例：那些单都已了结）。同时验证 `create_time` 年龄解析在真实数据上成立（CRM 单 1196 分钟）。
初测跑出 15 passed（含"无触发⇒零券商调用""同单不重复告警""新鲜单不算孤儿""终态/他人单不算""每日上限"
"券商失败可见降级""必须早于会话门控""检测器崩溃不拖垮退出链"）。

**人工那半已补（2026-09-18 末批，用户令"执行"）**：复核建议 3 的语义是"告警并（**人工确认后**）撤销"，
所以把"撤销动作"做成人工入口而不是自动化 —— `scripts/cancel_orphan_ext_order.py`（**不挂任何 cron/循环**）：

- 安全门（全过才动手）：① `remark` 前缀 `ext_exit_`（绝不碰别人的单）② 仍非终态 ③ 年龄 ≥15min（与检测器同阈值）
  ④ `--symbol` 一致 ⑤ 部分成交无均价 ⇒ 拒绝；默认 **dry-run**，必须 `--apply`。
- 撤单后**回查终态**才算成功（撤单 API 成功 ≠ 已撤，历史 #185）：回查仍活 ⇒ 退出码 2 + 审计 `cancel_unconfirmed`。
- `--clear-attempt` 才清 `{today}:{symbol}` attempt 键（默认不清 + state 备份）—— 因为撤当日孤儿单会消耗当日机会。
- 实弹 dry-run（真实券商）：`--order-id 8185508` → 正确识别 `CANCELLED_ALL` 并**拒绝执行**（退出码 1）；
  不存在的订单同样拒绝（退出码 1）。测试 16 例，含"回查仍活 ⇒ 报失败不宣称成功"。

**仍未做**：方案 B 的**自动化**那半（自动撤单 + attempt 清理 + `resume_close`）——需另行签字。

### 23.4 提交与台账

- 本批（§22 两 shadow + cron + entry executor 默认值 + 回放费用保真 + 两份 DEB 文档 + 测试）已提交：
  **`6f68d15e6 [RFC-083] 延长时段退出链超时链条修复 + 回放费用保真`**（12 文件，+839/−34），
  治理件 `docs/decisions/RFC-083-ext-exit-timeout-chain-and-replay-fee-fidelity.md`（含共享 skill 编辑台账）。
  **未把工作区里另一主题的未提交改动（`app/playbook/pipeline.py` 的 MFE/MAE 极值、`app/data/futu_lv3_provider.py`、
  `app/pipeline/order_executor.py`、`config/unified_config.yaml` 的 glm `reasoning_effort`、`scripts/config_validate.py --quick`、
  `scripts/daily_invariant_check.py`、`scripts/settle_winner_overnight_shadow.py`、`scripts/repair_trade_exit_invariants.py` 及
  对应测试，共 12 项）连带提交**，其归属留给对应作者。
- `ALLOW_SIM_ENTRY` 默认值翻 false 的裁决登记于本表 R8（行为不变，回滚 = 改回 `"true"`）。
- 部署形态：本批**只动 `scripts/`（cron 每轮新进程 ⇒ 立即生效，无需 restart）+ 测试 + 文档**；
  `app/execution/exit_lifecycle.py` 仅加一条 WARNING 日志，但服务进程（09:00:20 启动）**不会**加载它 ⇒
  下次 app 侧重启后才生效；exit cron 侧行为不受其影响（已在 `--dry` 探针确认窗控未死）。
- 未闭环（明确登记，不假装已做）：R5 档位聚合、方案 B（撤单+attempt 清理+`resume_close`）、
  R7 的两处时刻证据（"09:48:52 撤销"、"09:49:03 成交"）。
- 方案 A 检测器已在同一批落地（`scripts/orphan_ext_order_watch.py` + 挂载进 exit cron + 15 个测试），见 §23.3。

### 23.5 占位标的 (US.TEST) 源头治理（用户："另一个 agent 尝试修的问题是**连续推送不真实存在的标的股**"，2026-09-18）

**现象**：连续收到 `🚫 延长时段开仓拦截: US.TEST REAL未授权`；推送/日志里反复出现 US.TEST ——
一只**不存在于任何真实股票池**的占位标的。

**根因（不是推送去重不够，是占位标的能整条穿过流水线）**：

| 入口 | 实证 | 本批处置 |
| --- | --- | --- |
| 券商 SIM 账户报废测试单 | `order_list_query` 实测唯一占位行 `8185070 US.TEST CANCELLED_ALL`（9/17 00:08:03 ET，remark=`ext_entry`），同批还有 `8185071 US.MEDS` | 对账口丢弃 |
| 生产库 PDT 台账 | `pdt_trades` 被单测 artifact 写入 **28 行 US.TEST**（trade_date 2026-09-17），虚增 5 日窗口日内交易计数 → 可 fail-closed 拒真交易 | 写/读双侧挡 + 清历史 |
| 单测 fixture 惯例 | 全仓 60+ 处把 `US.TEST` 当 fixture 符号；一旦用例走到真路径，占位标的就被带进生产产物（本条 artifact 的成因） | ETH 链**路径测试**迁 `US.FAKE` |
| app 侧持仓/意图 | 至 9/18 00:18:22 CST 仍在 `[playbook平仓失败] US.TEST (sim) reason=EXIT_HELD` ×75 + 钉钉推送；库内该行已消失 | 既有守卫已覆盖（`app/push/hub.py` 推送过滤 + `trade_store`/`trade_recorder` 写库守卫），无需新增 |

**四个源头过滤器**（一律复用同一判据 `app.core.trade_identity.is_test_symbol`，不另写 SQL/正则以免语义分叉）：

1. `scripts/extended_hours_entry_executor.py::_scan_candidates._add` —— 候选源汇合口，
   `rank` / `beat` / `watchlist` / `snapshot` 四条源共用此口；此前占位标的能走完四闸、占 attempt 键、进审计与推送路径。
2. 同文件 `_reconcile_broker_entry_orders` —— 券商对账口：不再写 `{today}:{symbol}` attempt
   （否则静默封锁该标的）、不进 `active_orders`/`conflicts`、不触发成交回填补写。
3. `scripts/extended_hours_position_scope.py::load_non_manual_positions` —— 退出链持仓
   （盘前/盘后影子共用；假持仓会让退出链每分钟取行情/算信号空转）。
4. `app/risk/pdt_guard.py` —— `record_day_trade` 写入侧 + `_get_rolling_window_trades` 读数侧。

既存同向守卫（不重复实现）：订单口 `_execute`（`DEB-20260917 §23.12`，兄弟批次）、推送口 `app/push/hub.py`、写库口
`trade_store` / `trade_recorder` 的 `ensure_test_symbol_not_in_production_db`。

**artifact 清理**：`pdt_trades` 删 28 行占位，403 行真标的**全部保留**（`WHERE id IN (...)` 按 `is_test_symbol` 取 id，
非模式匹配）；备份 `data/ftseek_us.db.bak_20260918_pdt_placeholder`（24.3 MB）；清理脚本默认 dry-run + 删后自校验。

**接线级实弹验证**（真实券商 + 真实生产库，只读探针 `_write_open_position` 换记录桩，不撤单不下单不落库）：
券商 `remark=ext_entry` 共 6 行，唯一占位 `US.TEST:8185070` 被丢弃并留可见日志
（`[ext_entry] 对账忽略占位标的订单: US.TEST:8185070`），其余 5 行真标的照常按跨日旧单规则处理；
真实异动榜 50 只候选零占位；退出链持仓零占位。

**推送侧同类源头（用户看到的"连续推送"还有这一路）**：`FUTSEEK_PROBE=1` 探针静默 ——
`_push` 不推、且**不写去重键**（否则一次探针吃掉该 key 当日名额，反而静默掉生产真告警）；生产未设 ⇒ 行为不变。
动机：生产 cron 未设 `FUTU_TRD_ENV`（默认 SIMULATE）、审计无占位行 ⇒ 那串"REAL未授权"推送来自**人工/探针以 REAL 跑生产脚本**。
另补 `scripts/ah_exit_shadow.py::load_open_positions` 的占位过滤（此前 AH 影子为 US.TEST 连续输出观测行）。

**测试**：新增 8 个占位守卫测试（订单口优先级 / 候选源 / 对账口 / 持仓加载器 / PDT 写读 + 反面"真标的照记"）+ 探针静默 1 例 + AH 影子 1 例；
同时把 ETH 链**路径测试** fixture 由 `US.TEST` 迁到 `US.FAKE`（7 处），让"假标的=测试符号"的惯例不再制造生产 artifact。

**顺带修复的既有红灯（均非本批引入）**：
① `test_collect_dry_records_penny_candidates_blocked_by_price_gate` —— 内联快照桩只有 `close()`，
盘前 pretrade 闸取不到报价 ⇒ BIGCO 被判 `quote_missing`，用例恒红（与占位主题无关的真 bug：桩失真）；
已补 `get_market_snapshot`。
② 另一 agent 13:47 的 `1313d2ccb` 在订单口加了占位守卫，但**未同步**两个既有断言
（期望 `real_not_authorized`，实得 `test_symbol`）⇒ 分支 2 红；本批按"占位优先级最高"对齐断言。

**残留（登记，不假装已做）**：`scripts/ah_exit_shadow.py::load_open_positions` 未加占位过滤 ——
该影子是纯观测（只写自己的影子文件，不下单不推送），且其数据源 `open_positions` 已被写库守卫覆盖，
理论不会再出现占位行；若要彻底：一行 `if is_test_symbol(r["symbol"]): continue` + 1 个测试。
