# 富途通道「活体可用性」验证 + 上一轮审计更正 (2026-09-20)

**性质**: 测量层批 — 新增一个只读探针工具 + 文档更正。**零交易行为变更 / 零配置变更 /
零新影子通道 / 未重启服务 / 未动费用路径 (该路径由并行的 T3 批次在跑)。**

**触发**: 用户令「确认是否真实可用，如无误且有价值可直接应用，如无必须可跳过影子阶段，
更新写入文件」——针对 2026-09-20 数据面审计报告里那张「未接入的富途能力」表。

---

## §1 方法: 为什么必须活体探针

「某通道未接入」有两种截然不同的原因，只看 `grep -c` 会把它们混为一谈：

| 原因 | 含义 | 正确处置 |
|---|---|---|
| (a) SDK 无此接口 / 本机权限拿不到 | **真缺能力** | 换源或放弃 |
| (b) 接口好用，只是没接线/没订阅 | **接线问题** | 想接就直接接，不必"评估可行性" |

上一轮报告用的是 `grep`，因此 6 项里至少 2 项定性错误（见 §3）。本批把"可用性"做成
**可重跑的工具**，而不是一次性会话结论：

```bash
cd /opt/ftseek/FTSeek-US && ./venv/bin/python scripts/futu_capability_live_probe.py
# 退出码 0 = 核心通道全 PASS; 1 = 有核心通道 FAIL
# 产物: data/futu_capability_probe_latest.json (矩阵 + 逐项证据 + 费用逐单对照)
```

只读约束：仅调查询类接口；订阅只在探针自己的 context 内（`close()` 释放）；不落库、不下单、
不改配置、不写 `data/*.jsonl`；REAL 侧只有 `history_deal_list_query` + `order_fee_query` 两个查询。

---

## §2 今日实测矩阵 (SDK 10.09.6908 / OpenD 本机 127.0.0.1:11111)

| 通道 | 实测结果 | 证据 |
|---|---|---|
| `get_capital_distribution` | ✅ **可用** | 主力/大单/中单/小单 in+out 八列，`update_time=2026-09-18 19:59:59` |
| `get_capital_flow` | ✅ 可用 | 391 行分钟级序列（`super/big/mid/sml/main_in_flow`） |
| `get_rt_ticker` | ✅ 可用（**需先订阅 TICKER**） | 订阅后取到真实逐笔：NVDA `222.27 @ 2026-09-18 16:00:00.161` |
| `get_rt_data` | ✅ 可用（**需先订阅 RT_DATA**） | 390 行分时 |
| `get_option_chain` | ✅ 可用（**窗口 ≤30 天**，超窗报错） | NVDA 9/20–10/10 共 998 合约 |
| `get_option_volatility` | ✅ 可用，但**只吃期权代码** | 传标的 → `只支持期权代码`；传 `US.NVDA260921C50000` → 4 行 |
| `get_option_underlying_his_volatility` | ✅ 可用 | 标的级前瞻 IV + HV 250 行（通道 D 生产在用） |
| `get_stock_filter` | ✅ 可用 | US 全市场 `all_count=7701`；返回 `FilterStockData` 对象 |
| `acctradinginfo_query` | ✅ 可用 | `max_cash_buy / max_sell_short / session=RTH`（SIM 账户现金 $993） |
| `get_margin_ratio` | ✅ 可用 | `short_fee_rate=3.0`、`short_pool_remain=28.5M`（借券费已接） |
| `order_fee_query(REAL)` | ✅ 可用 | 30 张美股权订单实收费用；**模型 29/30 落在 ±$0.02**，`max|Δ|=$0.0382` |
| `history_deal_list_query(SIM)` | ⛔ **结构性不可得** | `模拟交易不支持成交数据`（与 DEB-20260730 层1 裁决一致） |

`verdict=ALL_CORE_PASS core_failures=[]`。

**费用模型交叉验证（本批独立重跑，非引用 8/24 结论）**：REAL 侧 8/1–9/20 共 87 笔成交 / 36 笔美股 /
30 张订单，逐单比对 `app/execution/fee_model.estimate_fee` vs 券商 `fee_amount`：
29 张误差 ≤$0.02，唯一 1 张 $0.0382（$3.08 的 1.2%，SNDK 30 股卖单，多笔成交取整差）。
⇒ **模型不是"不可靠"，`fees_est` 不是需要被替换的东西。**

---

## §3 对上一轮审计报告的更正 (自证伪，共 5 处)

1. **❌「这些产物…没有任何读方 / 白占 39MB」——错。**
   `data/futu_capital_flow.jsonl` 有**生产消费方**：`app/playbook/intraday_reentry.py`
   的盘中资金背离门（`_capital_flow_today_index()` `:742/:787`，判定 `:441-446`，
   调用点 `:882/:965`），另有 `scripts/gate_liveness_probe.py:145`、
   `scripts/reentry_gate_effect_tracker.py:44`、`scripts/capital_flow_daily_aggregate.py`、
   `scripts/futu_channel_shadow_eval.py` 在读。
   上一轮只 grep 了"影子注册表 + 采集脚本"，**漏了生产决策路径**。
   ⇒ 影响：不能按"零消费 → 直接 KILL"处置；KILL 掉分钟级采集 = 该背离门失去料源，
   fail-open 后退化为**一道不存在的门**。
2. **⚠️「get_order_fee 0 调用 → 最该接」——方向对但前提错。**
   真名是 `order_fee_query`（不是 `get_order_fee`）；它在 SIM 下**结构性不可得**
   （今日实测 `模拟交易不支持成交数据`），所以 DEB-20260730 层1 早就裁过"est 是 SIM 期唯一口径"。
   REAL 侧金标准 8/24 已做（16 单），本批复核扩到 30 单仍逐位吻合 ⇒ **"接实测费用替换模型"不是缺口**；
   真正的费用缺口是 **$1.99/单佣金地板在小名义额上的占比**（CTNT 类），
   而这正是**并行 T3 批次**在跑的事（`fee_gate_shadow.py` 9/20 起攒样 → 10/11 终裁）。
   **本批不碰费用路径**，避免与 T3 双写。
3. **⚠️ 期权 IV 项表述不准。** 标的级前瞻 IV **已经接了**（通道 D，
   `get_option_underlying_his_volatility`，今日实测 250 行）。缺的是**合约级期权链 +
   财报前后 IV crush 事件研究**，不是"标的级 IV 缺失"。
4. **⚠️ 选股器数字与用法错。** 实测 `get_stock_filter` 的 `StockField` 共 **121 个**
   （原文写"244+ 因子"无据），且字段名与 App 侧不同（`VOLUME` 不支持 → `不支持该过滤字段`，
   可用 `CUR_PRICE / PB_RATE / MARKET_VAL` 等），返回 `FilterStockData` 对象。
5. **⚠️ `rt_data/rt_ticker` 不是"未接入"，是"未订阅"。** 订阅后即刻可用（今日实测）。
   ⇒ 接它的成本是订阅配额，不是可行性。
   另：注册表 `futu_channel_capital_flow` notes 的"当前 258 行"已过期，实测 `capital_flow_daily.jsonl`
   = **483 行**（min_n=540 将满）。

**口径陷阱（防下一次误裁）**：注册表 `futu_channel_capital_flow` 的 FAIL_INTERIM
（`ic=0.0511 / t=0.595 / OOS 双窗异号`）是**日级聚合增量 IC** 的判据；
而生产在用的**盘中背离门**有自己的一份影子读数（`data/reentry_gate_effect_report.json`：
`gate_would_block n=9 / avg −6.63` vs `gate_pass n=44 / avg −17.77` ⇒ 拦下的不是赢家组）。
**两者不是同一口径，禁互相冒充**：拿日级 FAIL 去证"盘中门无据"是错的口径混用，反之亦然。

---

## §4 本批落地 (全部为测量层，零行为变更)

1. `scripts/futu_capability_live_probe.py`（新，只读，含 `--no-real` / `--json`）
   + 产物 `data/futu_capability_probe_latest.json`。用途：**切实盘前 / 每次 SDK·OpenD 升级后
   一条命令确认"哪些通道此刻真能取到数"**，替代"grep 一下没调用"的定性法。
2. 注册表 `futu_channel_capital_flow` 条目补注：生产消费方清单 + 计数更正 + 口径警示
   （防下一次"零消费 → KILL"误裁）。**状态/判据/采样面一律未动。**
3. `handoff.md` 留痕（宪章 §34：handoff 为唯一进度队列）。

## §5 明确不做（及理由）

| 不做 | 理由 |
|---|---|
| 把 `get_capital_distribution` / `get_stock_filter` / `get_option_chain` 接进任何门 | 无预注册可证伪假设 = "给零做运算"。用户纪律：信息收集 ≠ 有用，需先证明预测力 |
| 新建影子通道 | 用户令"如无必须可跳过影子阶段" ⇒ 本批新增的只有**验证工具**，零新影子 |
| 动费用路径 | 并行 T3 批次 9/20 已在攒 `fee_gate_shadow`（10/11 终裁），双写 = 两套判据漂移 |
| KILL `futu_channel_capital_flow` | ①该决定用户保留（上一轮已列为待裁）②原始采集有生产消费方，KILL 会断门 |
| 用 `acctradinginfo_query` 替换现有购买力帽 | SIM 的 `max_cash_buy=$993` 是模拟账户现金，**不代表实盘**；切实盘时须用 REAL 账户校准后再接 |

## §6 待裁 (2 项，比上一轮 3 项少 1：费用项已被并行 T3 覆盖且模型已证准确)

1. **`futu_channel_capital_flow` 日级 IC 通道**: 判据 FAIL_INTERIM、样本 483/540。
   建议 **标 `RETIRED_NO_PROMOTION`**（保留采集与生产消费，但永不再提请转正），
   而不是 KILL；需要你裁（属治理变更，我不单方面动）。
2. **选股器 / 资金分布是否接**: 若要接，先给可证伪假设（我可以出候选，例如
   "主力净额 5 日分位 + 异动榜入选" 双条件），否则维持不动。

## §7 诚实边界

- 探针只代表**此刻本机 OpenD + 该账户权限**下的可用性；权限/配额变化需重跑。
- 费用模型对照基于 REAL 账户 8/1–9/20 的 30 张美股权订单；样本量与市场结构变化
  都不足以宣称"任何单都吻合"（唯一 1 张 1.2% 偏差即例）。
- `get_option_chain` 首次调用曾出现一次 `PacketErr.Timeout`，重试即成功；
  探针把"首次超时"计为 FAIL，属偏保守（宁可误报不可漏报）。

---

## §8 执行延迟：存在，但位置/代价与上一轮报告不同（2026-09-20 补测）

取证源：`data/playbook_exits.db`（exit_intents/exit_legs，9/11–9/18）、
`data/ftseek_us.db` trade_records（8/1–9/20，335 笔已平仓）、
`data/extended_hours_exit_executions.jsonl`（15 行）、`crontab -l`、`exit_lifecycle.py`。

| 观测 | 实测值 |
| --- | --- |
| 入场滑点（参考价→成交） | n=327，中位 0.0bps，中位绝对值 4.0bps，p90 绝对值 15.8bps，成交更差占 48% |
| 出场滑点 | n=329，中位 0.0bps，中位绝对值 2.9bps，p90 绝对值 16.4bps |
| RTH 限价腿超时 | 48 单中 5 单"限价撤单→MARKET 补腿补齐"（账本例：`LIMIT d=0 → MARKET d=24`） |
| 保本/回撤锁路径（BE telemetry） | 177 笔中 146 笔（82%）限价未成交→转市价；`drift_from_be_line` 中位 +18bps |
| **转市价的**经济方向** | 有利 133 笔 / 不利 13 笔；净 **+$3,937**（LONG +$3,076、SHORT +$861）= 兜底成交价优于限价 |
| ETH 出场追价 | **设计上不追价**：`exit_lifecycle.py:519-522` 对 `session=='ETH'` 直接返回 HELD（`exit_eth_partial_fill_held`）；日志中该事件 **0 次** |
| EXT 限价超时现值 | crontab 实值 `EXT_EXIT_LIMIT_TIMEOUT_S=60`（非报告所写 20），每分钟跑 `extended_hours_exit_cron.py` |
| EXT 执行记录 | 15 行：ok=true 7 / ok=false 8。其中 ACN（执行 21:38:26Z→成交 21:38:52Z，pnl_net +59.09）、WBD（23:29:18Z→23:29:49Z，+51.66）为**成交确认延迟造成的假负**，非未成交 |

**对上一轮"五、速度审计 🔴"的更正（分时段）**：

- "盘前收口限价 20s 到期撤单不追价、薄盘可能根本没卖掉" → RTH 侧**不成立**（有
  市价补腿）；ETH 侧**成立**（设计即不追价）但该分支 **0 次触发**，且 15 行执行
  记录中**无 1 例"未卖掉"**。超时值 20 也已不是现网值（现 60），且每分钟重试。
- **符号陷阱（自证伪）**：`drift_from_be_line` 是**未按方向调整**的原始差值
  （写入方 `pipeline.py:7038/8529+`），直接按正负读会得出相反结论——我第一遍读反了。
  按 `direction` 换算经济方向后，结论从"兜底代价 ~$2.3k"反转为"兜底有利 ~+$3.9k"。
  教训：**解释 telemetry 符号前必须先读写入方**。

**诚实边界（SIM）**：上列滑点与"兜底有利"均在 SIM 成交引擎下测得，SIM 不建模真实
点差与部分成交，实盘不可外推；真实盘口下"转市价"很可能变成成本项。

## §9 多源冗余冲突：部分存在，部分是本轮误报

| 项 | 实测 | 判定 |
| --- | --- | --- |
| 符号形式（BRK-B / BRK.B） | `app/utils/symbol_utils.py` 的 `normalize_symbol` / `to_dot_style`，注释含三源实测差异表（TwelveData 认 BRK-B→404、Futu 认 BRK.B、Yahoo 认 BRK-B） | **已处理**；上一轮当"未处理缺陷"报 = 误报 |
| 分析师共识双源 | 富途 `get_research_analyst_consensus` vs finnhub `stock/recommendation`。实测 NVDA：富途 buy 100%（n=30，9/19）vs finnhub 强买+买 94%（n=69，period 9/1）；MU 96.6%(n=29) vs 92%(n=58)；BABA 100%(n=12) vs 90%(n=40) | **结构性双源存在**：口径（百分比 vs 分档家数）、基数（12–30 vs 40–69）、时效（9/19 vs 9/1）三重不同；两源**方向一致**（皆多头） |
| 是否同字段覆盖 | 落库键不同：playbook/盘前 = `futu_analyst`（`premarket_enrichment.py:149`）；DA = `analyst_cluster`（`deep_research_pipeline.py:142`） | **无同字段覆盖**，不构成静默冲突；LLM 同时见到两种口径时的权衡**未度量** |
| 昨收（快照 vs 日K） | 今日 3/3 Δ=0bps（NVDA/MU/BABA：快照 `prev_close_price` == 日K 真昨收） | 无非冲突证据；**今日休市，不能代表盘前窗口**（该问题的取证窗口是盘前时段） |
| 成本重复 | 同一概念确有两次调用，但富途侧接入动机正是"替代 Finnhub 付费 API" | 存在，属优化方向 |