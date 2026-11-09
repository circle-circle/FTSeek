# RFC-194 EXT 入场「T-1 走势 / 波动分位」审计字段 (只记不判) + 反转臂预注册草案

- 日期: 2026-11-09
- 状态: 已落地 (纯观测层) / 第三臂仅**预注册**, 未实现、未授权交易
- 前序: RFC-193 (EXT 订单查询限频收敛)
- 变更面: `scripts/extended_hours_entry_executor.py` (审计字段 + 成交后 metadata),
  `tests/test_rfc194_prior_move_audit.py` (12 例)

## 1. 为什么做 (研究结论摘要)

4 年、200 只高流动性美股、同日横截面 ("该组 − 其余", 剔除 beta 污染), Newey-West t:

| 形态 | 5 日 CS 超额 | 20 日 CS 超额 | 备注 |
| --- | --- | --- | --- |
| 连涨 3 日 | −0.58% (t −3.0) | −2.12% (t −5.2) | 4/4 年同号 |
| 连涨 5 日 | −0.60% (t −3.2) | −2.21% (t −4.3) | 同向 |
| 连跌 3 日 (慢跌) | −0.35% (t −2.0) | −1.32% (t −3.1) | **继续跌**, 与"跌久必涨"相反 |
| 单日 ≤ −7% (急跌) | +1.46% (t 3.0) | +5.52% (t 3.2) | 4/4 年同号 |
| 5 日 ≤ −10% | +1.17% (t 3.7) | +7.17% (t 3.2) | 同向 |
| 20 日 ≥ +15% | — | +2.05% (t 3.0) | 动量, 不是衰竭 |

关键限定 (决定了本 RFC 只做"记录"而不做"信号"):

1. **钱在隔夜**: 急跌组隔夜跳空 +0.57% (t 3.4) vs 盘中 +0.19% (t 1.0); 从次日开盘算 5 日只剩
   +1.16% (t 2.3)。扣 20bps 双边后盘内部份基本归零。
2. **只在波动放大时成立**: SPY rv20 上三分位 +2.81% (t 2.5) / 中 +0.97% (t 1.3) / 低 −0.20% (t −0.2)。
3. **样本是牛市**: SPY 4 年 +110.8%, 仅 7.4% 交易日位于 200dma 下方, 最大回撤 −18.8%;
   200 只高流动性票存在幸存者偏差。

结论: 方向可验证, 但**执行口径与现有 EXT 出场链相反** (见 §4)。

## 2. 本批做了什么 (纯观测, 零行为变更)

- `_prior_move_context(symbol, today)` 产出并落库: `as_of` / `source` / `stale` /
  `t1_ret_pct` / `t5_ret_pct` / `t20_ret_pct` / `spy_rv20_pct` / `spy_rv20_rank` / `spy_as_of`。
- 落点: ①`data/extended_hours_entry_executions.jsonl` 审计行; ②`open_positions` 与
  `trade_records` 的 `metadata_json.prior_move` (只增键, 退出链零影响)。
- 调用点在下单**成交之后** (`_write_open_position` 之前一行): 兜底路径要调实时K线,
  永不占用下单前时延。
- 券商单回补路径 (`_reconcile_broker_entry_orders`) 同样补字段, 避免这类成交在样本里成空洞。

**不参与任何判定**: 不改阈值、不拦单、不进 gate、不进排序。

## 3. 数据源与口径 (含已知缺陷)

| 源 | 成本 | 覆盖 | 说明 |
| --- | --- | --- | --- |
| `data/cache/daily_kline_v1.json` (RFC-171) | 0 调用 | **实测 40%** 标的为最新 session (220/548), 其余滞后 1–11 日 | 逐标的惰性缓存, 只对最近被查询的标的新鲜 |
| `get_cur_kline` (订阅式实时K线) | 0 历史额度, 实测 0.04s | 全覆盖 | 仅在缓存 `as_of` 落后于 SPY 时触发 |

- `stale=True` 表示该标的 `as_of` 早于 SPY 的上一交易日 ⇒ **分析面必须剔除**, 不许当 T-1 用。
- `source` 标明本行是用缓存还是实时兜底算的, 便于口径复核。
- 局限 (诚实标注): 两个源都可能与复权口径不同源; 跨源混用会有小量不一致, 已用 `source` 隔离。

## 4. 为什么没有直接塞进现有 EXT 研究臂

现有研究臂 (`gate_mode=research_momentum`, 源级臂死线 2026-11-13) 与本假设四处不兼容:

| 维度 | 现有研究臂 | 本假设需要 |
| --- | --- | --- |
| 人群 | 当日**涨** 0.5–10% | 昨日急跌、今日**不涨** |
| 出场 | BE +0.5% / trail 0.4–1.2% (EXT 专属) | 持有 3–5 日, 中途不许被剪 |
| 期效 | 11-13 到期 | 需 ≥30 笔, 3–6 周 |
| 配额 | 严格臂吃满后的剩余 | 高波动日正是严格臂最忙时 |

自证: 我们自己的书上 "跌久 (≤ −15%) 入场" 组 MFE−MAE **+0.53%**, 但 `realized_pnl ≈ 0.00%`
—— 钱被 BE 出场吃掉了。所以**不改出场, 等于没测**。

## 5. 第三臂预注册草案 (research_reversal, 未实现)

> 状态: **待批**。本文只预注册判据, 未写代码、未开配额。预注册后不得再改判据 (阈值购物禁止)。

- **H1**: 在 SPY 波动放大期, "T-1 单日 ≤ −7% 或 5 日 ≤ −10%" 的个股, T+1 开盘入场、
  T+3 收盘出场, 相对同期同流动性对照组有正超额, 且扣 20bps 双边后净为正。
- **人群**: 价 ≥ $5; 20 日均成交额 ≥ $50M; 当日开盘涨跌幅 ∈ [−2%, +2%] (不追反弹、不接下跌刀);
  同日有财报事件者剔除。
- **市场门**: SPY rv20 分位 ≥ 50 (用本周新增的 `spy_rv20_rank`, 与 §1 分位检验同口径)。
- **方向**: 仅 LONG (空头在 SIM 的券源可靠性不足; 且 4 年样本牛市, 空头无说服力)。
- **入场**: RTH 09:30–10:00 限价; **不用**盘前薄量时段 (04:00–07:59 中位成交量仅 398–2640 股)。
- **出场**: T+3 收盘 或 −5% 硬止损, 先到先算。**禁用** BE / TRAIL / 时间退出以外的任何止盈。
- **仓位**: 单笔 ≤1% 权益, 并发 ≤2 笔, 不与严格臂共享在仓帽。
- **判据 (n ≥ 30 笔)**: 日均聚类 (Newey-West, lag=2) 的净收益均值 > 0 且 t ≥ 2.0。
- **停止条件**: S1 前 10 笔均值 < −1.5% 权益; S2 累计净额 ≤ −2% 权益; S3 MAE 中位 > 2× MFE 中位。
- **证伪条件**: n ≥ 30 后净超额 ≤ 0 且 t < 1.0 ⇒ 关闭, 不再重启 (除非有新预注册)。
- **样本预期**: 1–2 笔/日 (按 200 只流动性池的急跌频率 × 当日 [−2%,+2%] 闸), 3–6 周攒够 30 笔。
- **实现前置 (未做)**: ①`is_research_gate_mode()` 单一谓词收敛 (现散在 6 处分析面:
  `bundle_builder`×2 / `confidence_tier_eval` / `budget_p3_eval`×2 / `overnight_leg_decompose` /
  `meta_label_shadow` / `settle_entry_confirm_forward`), 否则第三臂会污染严格臂口径;
  ②独立出场档; ③独立配额; ④人群供给复用本地日K (零外部调用)。

## 6. 本周期的读法 (先看数据再谈建臂)

字段落地后, 任意时点可跑 (只读):

```sql
SELECT json_extract(metadata_json,'$.prior_move.t1_ret_pct') AS t1,
       json_extract(metadata_json,'$.prior_move.t5_ret_pct') AS t5,
       json_extract(metadata_json,'$.prior_move.spy_rv20_rank') AS rv_rank,
       realized_pnl, metadata_json
FROM trade_records
WHERE owner_strategy='extended_hours_entry' AND exit_time IS NOT NULL;
```

按 `rv_rank ≥ 50` / `t1 ≤ −7` / `t5 ≤ −10` 分桶看 `realized_pnl` 与 `metadata_json.mfe_pct`、`mae_pct`。
**先看 MFE−MAE, 再看 pnl** —— 两者的差就是出场链吃掉的部分。

## 7. 回滚

字段是纯增量键。回滚 = 删除 `_prior_move_context` 调用行; 已落库的 `metadata_json.prior_move`
不必清理 (分析面按 key 缺失/None 处理)。

## 8. 残留债 / 未验证

1. 日K缓存覆盖率 40% 是结构性的 (逐标的惰性), 实时兜底已补, 但**未在真实成交上取样验证**
   `source='live'` 的落库形态 (今日无新成交)。
2. `tests/test_extended_hours_entry_executor.py::test_live_path_prints_gate_counts_and_reserves_attempt`
   在 HEAD 上**本就失败** (已用干净 worktree 复现), 与本 RFC 无关, 记入遗留。
3. 4 年样本的牛市/幸存者偏差未解决; 本 RFC 只用它来"决定记录什么", 不用它下单。