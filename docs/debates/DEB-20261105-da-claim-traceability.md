# DEB-20261105 · DA 双 LLM 理由分类与跨表溯源修复

**状态**: 已部署并完成 DA→Playbook 生产验收；后续分类规律攒样
**类型**: 既有测量链缺陷修复；不新增入场机制、不调整门槛、方向、仓位或出场
**关联**: DEB-20261105-da-main-pool-supply-expansion、OKR-2026-11 KR5

## 1. 审计结论

正常完成双 LLM 分析的 DA 标的，原始理由、机器分类和挑战过程均有记录，可按 `decision_id` 溯源：

| 记录面 | 内容 |
|---|---|
| `signal_snapshots.factors_json.claims` | bull/bear、thesis、source_type、claim_type、strength、LLM 标注来源；连接 T+1/T+5/T+20 收益 |
| `data/da_checkpoint/<ET日期>/<symbol>.json` | 完整 bull_case/bear_case（含 evidence）、evidence_gaps、分类复核、完整 `_debate`、模型和 prompt hash |
| `data/opportunity_scan_<date>.json` | 可发布候选的 `decision_id`、执行 conditions、完整 debate、invalidation/tripwires |
| `playbook_instructions.raw_instruction_json` | 指令摘要、debate_outcome、挑战者影子、风险及 supply cohort；完整论据仍回连 checkpoint |
| `data/da_gate_decisions.jsonl` | gate 原因、skip_reason、挑战者 verdict/方向/评分/验证失败轴、模型、usage、prompt hash |

分类不是事后自由文本猜测。主 LLM 必须给每条 bull/bear claim 标注 11 类枚举之一：
`product_catalyst`、`earnings_beat`、`valuation_cheap/expensive`、
`insider_bullish/bearish`、`technical_breakout/breakdown`、`sentiment_crowded`、
`macro_tailwind/headwind`。系统另以确定性关键词复核并把 reported/inferred mismatch
写入 `_score_band_audit.claim_type_recheck`；它是分类质量审计，不自动覆盖主 LLM 标签。

2026-10-20 至 2026-11-04 共 354 条 DA 快照：221 条保留结构化 claims，含 990 个 claim 对象，
claim_type 非空率 100%，上述 11 类均有实际样本。SKIP/BLOCK 也进入信号快照和 gate 台账，
不是只记录可交易候选。采集或 LLM 失败则记录 `rating=错误`、conclusion/risks 和空 claims，
不得伪造一套双 LLM 理由。

354/354 均能以 decision_id 回连 checkpoint，且都有完整 debate 和 challenger validation；
310 条有具体 challenged_claims，153 条有非空 evidence_gaps（空数不代表漏记，也可能是无缺口）。
分类复核共覆盖 1,656 个原始 claim，reported 与规则 inferred 不一致 755 个（45.6%）。
该值是**审计分歧率，不是 LLM 错误率**：复合论据只给一个主标签、关键词优先级和类别覆盖不足
都会造成分歧。后续应按 mismatch 子集人工/离线复核，不得把确定性关键词结果自动当成真值改写。

## 2. 发现的断链与根因

其余 133 条恰为 bridge 已发布候选，`factors_json` 从 DA 写入的富对象退化为简单数组。
KMT 实证：DA 先写 `claims/conditions/debate_outcome`，随后 Playbook 用同一
`decision_id` 调 `record_signal_snapshot()`，`ON CONFLICT DO UPDATE` 以
`["news_positive","volume_expansion"]` 覆盖整个富对象。

这造成选择偏差：未发布候选有理由分类，真正进入后续交易链的候选反而在快照里丢失分类。
133/133 均能以相同 `decision_id` 回连仍存在的 checkpoint 和完整 bull/bear_case，
所以是数据库富结构被覆盖，不是原始双 LLM 产物灭失。

## 3. 最小修复

`app/analysis/signal_ic_tracker.py` 仅对 `strategy_id=deep_analysis` 的重复写入增加合并：

1. 已有对象含 `claims`、后来为简单 factor list 时，保留原 claims 和 DA conditions；
2. 后来的 Playbook factors 原样放入 `playbook_factors`，同时去重并入既有 `conditions`，
   使 `factor_ic_backfill` 等既有消费者不会少算原来的执行因子；
3. `market_context_json` 的既有 upsert 语义不变；其他 strategy 仍按原逻辑覆盖；
4. 新写入若自身也是含 claims 的富对象，仍允许正常更新。

本批不回写历史 133 行，避免在同一 OKR 窗内静默重算历史 IC。历史复盘可从 checkpoint
按 decision_id 只读重建；新样本从 2026-11-05 起保留完整结构。修复没有改变交易代码，
但后续 claim IC 会正确纳入已发布候选，这是纠正原有样本选择偏差，不应与旧缺数口径混合作功效宣称。

## 4. 验证

- 新增 upsert 回归：DA claims、debate、原 conditions 均保留，Playbook factors 不丢失；
  非 DA strategy 行为不变。
- 聚焦与相邻回归：221 passed；重启前 COIN/ALB 两仓不变、非 BOOKED 出场为 0。
- `systemctl restart ftseek-us` 后启动冒烟 7/7。
- 生产复核：2026-11-05 首只 HCA 的 snapshot 已为 object，含 4 个结构化 claims、
  debate_outcome 和 gate_decision，decision_id=`DA-20261105211741-US.HCA-ece1`；
  完整 DA 批 36/36 均为富对象、0 数组退化，含 EPAM/WPM/TIC 三个扩容 cohort；
  09:00 EST Playbook 于 09:06:50 完成 80/80 指令生成后，36/36 仍均为富对象且 claims 非空；
  17 条实际发生 Playbook 合并，17/17 保留 claims，0 条退化。EPAM/TIC 均同时保留
  原始 claims、合并后 conditions 和独立 playbook_factors，生产断链已闭合。
  当日最终复核共 36 条 snapshot、168 个 claims，11 个 claim_type 全部有实际样本，
  `llm_labeled=true` 为 168/168；source_type 分布为 fundamental 68、news 37、technical 35、
  sentiment 24、macro 4，bull/bear=80/88。分类字段名是 `claim_type/source_type`，不是
  泛化的 `category`；后续 SQL 须按真实字段取数，避免把“字段名不同”误报为未分类。

## 5. 后续规律分析口径

按 `decision_id + claim_type` 去重后，可分 bull/bear、方向、challenger verdict、
base/expansion supply cohort、gate/成交状态比较样本数、T+1/T+5 收益和费用后交易净盈亏。
信号收益与真实成交净盈亏必须分开，SKIP/BLOCK 只用于反事实，不得混入实际交易胜率。
同日信号相关，结论须按日聚类并报告 MDE；类别 n<30 只作描述，不直接加门或调权。

```sql
WITH claim_rows AS (
  SELECT DISTINCT s.decision_id, s.symbol, s.trade_date, s.direction,
         json_extract(c.value, '$.side') AS side,
         json_extract(c.value, '$.claim_type') AS claim_type,
         s.fwd_ret_t1, s.alpha_t1, s.fwd_ret_t5
  FROM signal_snapshots AS s,
       json_each(json_extract(s.factors_json, '$.claims')) AS c
  WHERE s.strategy_id='deep_analysis' AND s.trade_date >= '2026-11-05'
)
SELECT claim_type, side, direction, COUNT(*) AS signals,
       AVG(fwd_ret_t1) AS avg_t1, AVG(alpha_t1) AS avg_spy_alpha_t1,
       AVG(fwd_ret_t5) AS avg_t5
FROM claim_rows
GROUP BY claim_type, side, direction;
```

当前限制：库内 `alpha_t5` 尚无覆盖，T+5 只能用 `fwd_ret_t5`，不得称为 SPY 超额；
旧汇总器也兼容历史 list[str]，做 claim 规律分析时须只取上述 claims 和 11 类枚举，
不要把 Playbook conditions 或旧结构键误当 LLM 理由类别。完整证据与挑战原文以 checkpoint
为准；signal snapshot 的 thesis 为 200 字截断版，适合分类统计，不替代原文审计。
单 claim 只有主类别， challenger 的 validation_failed 则按 data/logic/risk 分轴；
复合理由的多标签拆分或新增 analyst/liquidity 等类别须另立影子评估，本批不改 prompt taxonomy。

回滚：移除 `_merge_da_factor_payload` 及调用即恢复旧 upsert，但会重新丢失已发布候选的
claims，不推荐；更安全的工程回滚是保留数据、暂停新的类别消费，不回删任何原始记录。
