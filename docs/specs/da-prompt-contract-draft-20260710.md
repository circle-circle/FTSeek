# DA prompt 契约改造草案 — 轨道A 阶段2 (草案, 2026-07-10)

> ⚠️ **已被取代 (2026-07-10)**: 本草案主张"移除 LLM 数字", 已改为 spec 的
> "有界自治 propose–verify–calibrate 封套"(保留 LLM 数字 + 代码验证/校准/夹逼)。
> 以下仅 `entry_reference` 枚举、key_levels 已代码化、DA 无 ATR 等核实结论仍复用。
>
> 状态: **草案, 不改 skill**。本文件仅定义目标契约与 adapter 接线方式, 供评审。
> 实施需 sim + 改 DA skill(工作区外), 属难回退, 每步带备份。

## 背景 / 动机
职责分离: LLM 只出**判断**, 代码出**数值**。离线报告 (`scripts/track_a_offline_alignment.py`)
显示已成交单 LLM entry ≈ 实际成交 (median 0.00%), 故本改造**不是纠错**, 价值在:
消除数字幻觉面、可校准/回测、契约清晰。

## 现状(已核实, 证据)
- LLM 输出契约: `llm_synth.py` L509-517 `execution_plan` 要求 LLM 给**具体数字**
  `entry_price / stop_loss_price / take_profit_price`(L513-515)+ `entry_strategy`(文本)
  + `invalidation`(条件)。L529 明确"价位必须是具体数字"。
- adapter `to_voting_result` (adapter.py L239-241) 直接抄这三个数字。
- **关键位已代码化**: `deep_analyzer.py` L212-213 代码算 `support_20d/resistance_20d`,
  L221-223 写入 `key_levels.{current,support,resistance}`(覆盖 LLM 值)。
- **DA 已有确定性位**: price / ma5/10/20/50/60 / rsi14 / support_20d / resistance_20d
  (deep_analyzer L206-213)。→ 喂 `resolve_entry_reference` 的 levels 齐备。
- **DA 无 ATR**(既往核实): 故 SL/TP 的波动率分桶重算仍以执行端 reprice 为权威。

## 目标契约

### execution_plan — 移除数字, 改出类别
**移除**(不再向 LLM 索取):
- `entry_price`(数字)
- `stop_loss_price`(数字)
- `take_profit_price`(数字)
- `stop_loss` / `take_profit`(文本价位)

**新增**(判断, 枚举/类别):
- `entry_reference`: 枚举, 取自 `app/playbook/entry_reference.py::ALLOWED_ENTRY_REFERENCES`
  = `market | pullback_to_support | breakout_above_resistance | vwap | ma20 | ma50 | orb`
- `strategy_type`: 枚举 = `breakout | pullback | mean_reversion`
- (可选) `needs_wide_stop`: bool — 把"该给宽止损"表达成类别, 不给数字

**保留**:
- `entry_strategy`: 文本**理由**(人读, 不再解析取数)
- `invalidation`: 条件描述(轨道B 消费)

### 目标 JSON(execution_plan 段)
```json
"execution_plan": {
    "strategy_type": "breakout|pullback|mean_reversion",
    "entry_reference": "market|pullback_to_support|breakout_above_resistance|vwap|ma20|ma50|orb",
    "entry_strategy": "文本理由: 为何在该参考位进场(不含承诺的精确数字)",
    "invalidation": "使交易逻辑失效的条件",
    "needs_wide_stop": false
}
```
其余顶层字段(direction/confidence/rating/score/bull_case/bear_case/causal_chain/
risks/key_levels/data_quality_note)**不变**。`key_levels` 继续由代码算。

## adapter 接线(阶段2, 需 sim)
`to_voting_result` 改为**不抄 LLM 数字**, 改调代码引擎:
1. `entry_price, _src = resolve_entry_reference(direction, plan.entry_reference, tech, levels=L)`
   其中 `L` 来自代码化 `key_levels` + ma:
   `{"support": kl.support[0], "resistance": kl.resistance[0], "vwap": ..., "orb_high/low": ...}`
   (vwap/orb 无则 None → 自动回退 market)。
2. `entry_low, entry_high = compute_entry_band(entry_price, is_short)`(承接 band 单一真源;
   Q2 波动率化启用后传 `atr_pct`)。
3. SL/TP: DA 无 ATR → 写**占位/保留**, 以执行端 reprice(ATR 分桶)为权威;
   或后续给 DA 补 ATR 源再本地算(单列评估)。

## 兼容 / 回退(硬要求)
- `entry_reference` 缺失(旧 LLM 输出 / 解析失败)→ 默认 `"market"`(安全, 等价现状 ask/bid)。
- `strategy_type` 缺失 → 不影响数值(仅影响退出策略选择, 走既有 fallback)。
- 全程不返回 0 价: `resolve_entry_reference` 已保证有价即回退 market。

## 验证
1. 阶段1 离线复跑(次交易日 DA 新契约产出后): 对同批标的, 比较
   "代码引擎 entry/band" vs "当前 LLM entry" vs "实际成交", 确认代码 ≥ LLM。
2. sim: 成交集不变或更优、entry/SL/TP、pnl_pct 分布对比切换前后; 一次只切 entry。
3. 度量用 pnl_pct。

## 风险 / 边界
- 改 DA skill(`~/.hermes`, 工作区外只读, permissions=all)+ LLM prompt → 难回退, 带备份。
- `entry_reference` / `strategy_type` 枚举保持小而明确, 禁自由文本。
- SL/TP 不在本阶段迁移(DA 无 ATR, 权威在执行端 reprice); 单列评估。
- 分批建仓/多参考位: 先做单参考, 后扩展。

## 假设
- DA `key_levels`(代码算)+ ma20/ma50 覆盖主要 `entry_reference` 场景; vwap/orb 暂缺→回退 market。
- 富途模拟盘(TRADE_ENV=1)承载 sim。
