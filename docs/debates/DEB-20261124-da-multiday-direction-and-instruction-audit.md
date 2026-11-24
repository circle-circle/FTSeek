# 议题：连续多日 DA 产出指令、方向策略与执行语义核查

## 状态

- [ ] 进行中
- [x] 待决策（只读核查完成；是否改变 DA 时间/入场约束的执行语义待用户裁决）
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

## 1. 范围与结论

- 核查窗口：2026-11-17、11-18、11-19、11-20、11-23、11-24 六个连续交易日，即 qwen4-max cohort。
- 数据截点：2026-11-24 22:48 CST / 09:48 ET；**11-24 尚未收盘，不能当日终结算**。
- 数据源：`data/da_checkpoint/<date>/US.*.json`、`.raw.json`、只读 `data/ftseek_us.db`、生产日志、`scripts/okr_2026_11_scoring.py`。
- 总结论：**DA 原始 JSON 产出完整、方向与评分映射一致、风险几何有效，多数方向变化能由盘前价格/事件变化解释；但端到端不能判为完全正常。** 主要问题是：
  1. 方向在弱市/反弹日均高度集中于 SHORT，既有组合方向 guardrail 多次 `would-skip` 但按设计不拦截；
  2. DA 文本中的“唯一入场价 / 不到价不成交 / 当日了结”不是等价的机器约束，reprice、entry-confirm 与硬编码隔夜 cohort 会改变实际交易；
  3. 11-23 handoff 把上一日常规收盘值误作当日盘前值，所得“SPY -3.64%、VIX 36、周末利空防御日”与 DA 原始输入不符，须更正；
  4. `market_regime=no_trade` 是生成时标签，开盘执行前重新评估为 `risk_off` 后放行，不是绕过 NO_TRADE 硬门；但成交记录保留生成标签，存在混合时点口径。

**不建议仅凭这几日结果直接砍 LONG、放大 SHORT、开启方向偏斜硬闸或改 reprice/退出参数。** post n=31，且方向效果与执行语义混杂，先保持一次一变量和 C4 影子纪律。

## 2. 六日原始产出与成交

`B/S/Skip` 为 DA checkpoint 原始 `action`；成交和净额取 `trade_records`，净额=`pnl_net` 优先，否则 `realized_pnl-fees_est`。净额只含已平仓，未平不计；11-24 为进行中。

| 日期 | SPY/QQQ 盘前 | DA B/S/Skip | 新开成交 | 已平净额 | 备注 |
|---|---|---:|---:|---:|---|
| 11-17 | +0.005% / +0.389% | 6 / 9 / 20 | 6 | **-$231.75** | LONG 2 笔 -$320.17；SHORT 4 笔 +$88.42 |
| 11-18 | +0.192% / +0.625% | 11 / 8 / 12 | 8 | **-$197.93** | 另 LLY 未平；LONG 已平 5 笔 -$215.99；SHORT 2 笔 +$18.06 |
| 11-19 | +1.270% / +2.218% | 11 / 10 / 15 | 11 | **-$75.65** | LONG 5 笔 -$172.91；SHORT 6 笔 +$97.26 |
| 11-20 | -0.949% / -1.731% | 4 / 24 / 9 | 0 | $0 | DA 有产出；auto_execute 至 09:58 ET 才恢复，后续亦受入场门拦截 |
| 11-23 | +2.645% / +3.591% | 6 / 18 / 14 | 8 | **+$14.23** | 另 BA 未平；LONG 1 笔 +$21.04；SHORT 已平 6 笔 -$6.81 |
| 11-24 | -1.231% / -2.151% | 5 / 15 / 16 | 截点 0 | 不结算 | LLY BUY 因已有 LONG 仓被 `SKIPPED_EXISTING_POSITION`，未反向/重复开仓 |

11-17→11-23 已平合计 **n=31、净 -$491.10**：LONG n=13、净 **-$688.03**；SHORT n=18、净 **+$196.93**。这只是点估计，单侧 n<30，不证明 LONG 无 edge 或 SHORT 可放大；且下一节的 reprice/持夜语义使“方向归因”不能脱离执行归因。

格式体检：六日 checkpoint 数分别为 35/31/36/37/38/36，全部 `_finish_reason=stop`、`_model_reported=qwen4-max`；`action == _llm_output.direction` 不一致数 **0**。DB 非 SKIP 指令的 BUY `SL<entry<TP`、SHORT `TP<entry<SL` 全部成立，DB RR 与价格几何重算一致。confidence/decision_conf 多数集中于 35/0.5，不应当作已校准胜率或新建门禁。

## 3. 方向是否合理

### 3.1 合理/正常的部分

- 11-20、11-24 盘前 SPY/QQQ 同步下跌，DA 空头占比上升，方向与输入一致。
- 不是静态永远做空：COIN 六日持续 SHORT、MSFT 自 11-18 起持续 SHORT；AMZN、ARM、AMD 等随盘前与事件变化翻转。
- 相邻日共同且两日均非 SKIP 的 71 个符号观察中，20 次方向翻转（约 28%）；11-19→11-20、11-23→11-24 市场状态反转时翻转增加。该比例本身不构成异常证据。
- LLY 已持 LONG 时，11-23 的 SHORT 被重复持仓拦截；11-24 BUY 也为 `SKIPPED_EXISTING_POSITION`，未把 DA 方向翻转直接变成反向仓或加仓。

### 3.2 需要警惕的部分

- 最终 DB 非 SKIP 方向：11-20 **22S/1B**、11-23 **14S/1B**、11-24 截点 **14S/4B**。11-23 日志多次出现：`组合方向偏斜 guardrail 拒绝执行 (当日 1B/...S >80%)`，同时明确 `F1摘闸影子照算, 不拦截`。
- 11-23 盘前是 SPY +2.645%/QQQ +3.591%，DA 仍以反弹 fade/日线 DOWN 为主，属于**逆盘前动能、顺日线趋势**的可解释策略，不是数据看错；但其风险与 11-20/11-24 的顺势下跌做空不同，不能与后两者合并宣称“方向正确”。
- 11-23 AMD 是具体反例：盘前 +4.67% 时 DA 判为板块 beta 过度外推并计划 SHORT，实际净 -$101.45。单笔不证伪 fade 类，但说明方向集中时同类 beta 反弹可共同受损。

## 4. P1：DA 文本约束与机器执行不等价

### 4.1 当日了结没有下游执行语义

11-18 MU 原始输出：

- `valid_until=2026-11-18T16:00:00-05:00`；
- `execution_plan.entry_strategy` 明写“仓位当日了结（收盘前平仓），不承担……隔夜二元风险”；
- 计划 entry=715、SL=696、TP=742。

实际：09:56 ET 以 705 买入，SL/TP 经 reprice 改为 669.47/757.56，**持有到 11-20 09:30**，净 -$260.79。

源码证据：

- `/root/.hermes/skills/deep-stock-analysis/scripts/adapter.py:257-322` 的 `to_voting_result` 不透传 `valid_until`；
- 同文件 `:380-389` 的 `to_exit_meta` 硬编码 `cohort="A"  # A=日内可隔夜, B=日内平仓`；
- 对 DA skill、Playbook、risk/pipeline/core 检索 `valid_until`，未见该 DA 字段的 live 平仓消费方；`app/event_stream/instructions.py` 是另一离线事件研究契约，不能冒充 DA Playbook 消费方。

因此，“当日了结”目前是文字意图，不是机器保证。MU 这笔损失不能全记为 DA 方向错误。

### 4.2 唯一入场价会被 reprice 与确认门改写

11-23 AMD 原始输出明写：“只有反弹到 456.20 才做空；若今日未反弹到 456.20 则不成交”。实际：

- 09:45 首次 reprice：456.20→451.54；
- 确认时价格 446.8205，entry-confirm 以 reprice 后 451.54 为锚；
- 后续 4 轮 reprice，最终 DB entry=438.53、SL=463.96、TP=400.38；
- 实际 439.50 成交，净 -$101.45。

这符合现行执行代码，但不符合自然语言“不到 456.20 不成交”的通常理解：

- `app/playbook/pipeline_parts/auto_execute_flow.py:1205-1225` 以实时 entry/SL/TP 构造 reprice 参数并更新 DB；
- `app/playbook/entry_confirm_gate.py:556-582` 明确使用 reprice 后执行锚，原始 `planned_entry` 仅保留作 plan_gap 观察；
- `auto_execute_flow.py:1878-1893` 注释明确“锚取 reprice 后的执行锚”。

截至 11-23 的 75 条非 SKIP 指令中，44 条最终 DB entry 距 DA planned_entry >1%，13 条 >3%；64 条止损百分比宽于 DA 原计划。该统计说明**语义差异是系统性的**，不是证明 reprice 本身有负收益。11-24 截点尚未 reprice，不纳入这 75 条。

### 4.3 待裁的不是“是否再补一句 prompt”

需要先由用户确认目标语义：

1. **咨询模式**：DA 的价位/时间文字只作 thesis，实际交易完全由 reprice + RFC-141 + 统一退出管理；则应在输出/报表明确标注，避免把文字当保证；
2. **严格计划模式**：`confirmation.price_level`、`valid_until` 与“当日平仓”应是硬约束；则需独立 RFC、机器字段、回放/影子验证及一行回滚，并处理与既有 reprice、RFC-141、EOD 规则的冲突。

未裁决前不改 prompt、适配器、reprice、确认门或持夜规则；不借本核查新增同轴影子。

## 5. 11-23 市场读数更正与 regime 口径

### 5.1 handoff 旧条目的市场判断错误

handoff 2026-11-23 21:1x 条目写“SPY 盘前 689.81，较昨收 715.87 跌 -3.64%；VIX 36.07；周末重大利空”。DA 原始 `US.NVDA.raw.json.collected.market_context` 实为：

- SPY `current/prev_close=689.8144`（11-20 常规收盘），`pre_price=708.06`，`pre_chg_pct=+2.645`；
- QQQ `current/prev_close=608.4505`，`pre_price=630.30`，`pre_chg_pct=+3.591`；
- VIXY=25.95，代理 VIX=25.95×0.85=**22.06**。

689.81 相对 11-19 收盘 715.87 的 -3.64% 是**上一交易日（11-20）常规日跌幅**，不是 11-23 盘前。盘前摘要“51 涨、平均 +2.8%”与该原始输入一致，并非 DA 展示与输入相矛盾。旧条目不静默改写，在本 DEB 和新 handoff 条目中追加更正。

### 5.2 NO_TRADE 后成交不是硬门被绕过

- 11-23 生成标签为 `no_trade`，日志 22:00 CST 的评估为 score=-7、SPY=689.81、VIX=22.06；
- 09:45 ET 执行前重新评估为 `risk_off`：SPY=718.33、VIX=19.24、score=-4，随后 `auto_execute 开始: 19 条指令`；
- `risk_gates.py:233-244` 仅在实时 regime 仍为 NO_TRADE 时跳过，故当日放行有明确日志依据。

但 `playbook_service.py:2337-2344` 优先沿用 instruction 的 regime 文本，导致 11-23 成交记录出现 `market_regime=no_trade` 与 `entry_vix≈19.x` 并存。建议后续只做**新增执行时 regime 字段/报表双时点披露**的度量修复；不要直接改历史 `market_regime` 或 cohort 谓词。

## 6. 官方 OKR 读数

实际运行：

```bash
env -C /tmp PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/opt/ftseek/FTSeek-US \
  /opt/ftseek/FTSeek-US/venv/bin/python \
  /opt/ftseek/FTSeek-US/scripts/okr_2026_11_scoring.py --as-of 2026-11-24
```

返回 exit 0：

- whole：n=138，净 **-29.8836bps / -$1818.59**，KR1 NOT_MET；KR2 死单率 21.0145% MET；
- pre：n=107，净 -27.0715bps；
- post：n=31，净 **-41.5510bps / -$491.10**；
- post n<120，按已裁方案 B 为**功效不足**，本次 Kill/Pivot 不触发，不能用这 31 笔宣称 qwen4-max 已证伪，也不能因 SHORT 小计为正就宣称已有可放大 edge。
- 脚本提示：其查询是当前已平行、as_of 不设 trade_date 上界，不是历史快照；CI/MDE 为日等权对象，不是名义加权净 bps 的严格区间。

## 7. 复现口径与未验证

只读查询均用 `sqlite3.connect('file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro', uri=True)` + `PRAGMA query_only=ON`。checkpoint 统计遍历 `data/da_checkpoint/<date>/US.*.json` 并排除 `.raw.json`；原始盘前上下文读 `.raw.json.collected.market_context`。成交净额只取 `source_type='PLAYBOOK_SIM'` 且本窗实际均为 `decision_id LIKE 'DA-%'` 的行。

未验证：

- 未逐条外联网核实新闻真实性；本结论检查的是输入→输出→执行的一致性，不为外部新闻真伪背书。
- 未做逐笔反事实成交，不能声称“若保留 DA 原 SL/不到价不成交，一定少亏多少”。
- 未证明 LONG/SHORT 差异有统计显著性；未把盈亏结果倒推成单次方向是否合理。
- 11-24 尚未完成自然执行/收盘，未评价当日最终命中率或净收益。
- 本批只读数据与源码，零代码/配置/prompt/DB 改动、零下单、零付费、零重启；未跑代码测试（无代码改动）。

## 8. 建议与下一步

1. **事实口径更正立即生效**：以 §5 替代 11-23 旧交接的市场描述；后续盘前核查必须同时打印 `prev_close/pre_price/pre_chg_pct/snapshot_time`，禁止把 `current` 当盘前价。
2. **用户裁决 DA 是咨询还是严格计划**（§4.3）。在此之前保持当前执行语义，不把文字约束当已上线保护。
3. **方向偏斜只保留现有影子证据**：不在小样本下开启硬闸、不砍 LONG、不放大 SHORT；后续若裁，须把 11-23“上涨日 fade”与 11-20/24“下跌日顺势”分层。
4. **度量修复可另提小批**：执行时 regime 附加字段、DA planned→reprice→fill 双/三锚报表、valid_until 是否有消费方的基数/契约检查；不得静默改变既有判分人群。
