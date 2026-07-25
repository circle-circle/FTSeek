# Sim 验证方案 v3 · 内联影子日志（前置审计 + 定案）

**载体**：富途模拟盘（不损真实资金）
**性质**：只读审计记录 + 定案。v3 取代 v2.0（harness 回放）/ v2.1（forward-sim + 持久化）——因约束 2 否决历史回放、且 live sim 本身已是记录完整的运行中沙盒。
**数据截至**：universe 上线首日 2026-07-14（当日 5 条 universe 指令）。
**状态**：三项核实完成；方案定为 v3；**一期纯影子、零行为改动**。

---

## 1. 三项核实结果（按核实产出规范原样）

### 约束 1：`playbook_instructions` 作为 universe 回放源 —— 能反解，但样本≈0
- **位置**：`data/ftseek_us.db`（354 行，全源）；live 库 `data/ftseek_us_live.db`（16 行）。`playbook_instructions.db` / `playbook.db` 均为空壳（误导项）。DB 名→路径由 `app/storage/db_pool.py` 的 `_DB_PATHS` 映射：`ftseek_us` → `data/ftseek_us.db`（`TRADE_ENV=0` 时切 `ftseek_us_live.db`）。
- **universe 行的反解（4th-watch：未触发阻塞）**：表**无 `source` 列**，但两条路可靠标识：
  1. `decision_id` 前缀 `universe_expansion:DATE:SYMBOL`；
  2. `raw_instruction_json` = `{"source":"universe_expansion","prompt_version":"v2.0","origin":{causal_chain, winner_loser, corroborated_by_market}}`（**tier/论点信息全在此**）。
- **致命点**：**universe 仅 5 行，全部在 2026-07-14 一天**（当日上线）。作为"过去 30-60 天回放源" → **几乎无历史可回放**。
- **有利面**：schema 极全，每条指令已回填 `mfe/mae/pnl/pnl_pct/holding_period_hours/executed/triggered/execution_price/exit_*/entry_low/entry_high/confidence/horizon/invalidation` + `raw_instruction_json`（含 tier）。样例：EOG BUY 成交@139.845，TRAILING_STOP 1.8h，pnl −68.89（loss）；ORCL SHORT，0.3h，+28.5（win）。→ **"入场 + 结果 + tier"链已现成，可直接采集。**
- **小缺口**：`vix_at_generation` / `spy_trend_at_generation` 列存在但 universe 行为 NULL（生成时上下文未回填）。

### 约束 2：5m / 分钟 K 线持久化 —— 无（硬约束）
- `data/backtest_cache/` 仅**日线**（`*_1day.csv`，9 标的，停在 2026-03-27）；`data/yahoo_cache/` 空。
- 全库**无 kline / bar / candle / intraday 表**。
- Provider 只**按需实时取**：`market_data_router` 取 1-min 算 `slope_5m`；`finnhub_provider` 支持 5m（`resolution="5"`）但 API 限额 + 回溯有限；`futu_lv3_provider` 可订阅 `K_5M`，但 **ADR-013 明注"无有效交易链路消费者"、未持久化**。
- **结论：历史 5m 回放不可行** → "拉过去 30-60 天信号 + 回放 5m 数据"路线不成立。

### 约束 3：SPY / sector ETF 5m 触发瞬间可得性 —— SPY 可，sector ETF 不可
- **SPY：可得**。`get_spy_context()`（`spy_price/spy_ma20/spy_ma50/spy_ma200` 缓存）+ `slope_5m`（从 1-min 实时算）已在执行路径；`pipeline.py` 已带 `spy_price/spy_ma20/spy_ma50`。
- **sector ETF 5m 相对强度：不可得**。`app/risk/sector_leader.py` 是**硬编码"板块→成员股"映射（8 类）、新闻驱动、用成员股均值算 RS**，**无 ETF ticker、无 5m、未挂执行路径**。`s_sector` 需新增映射 + 新取数路径。
- 注：V2 生成时 `causal_chain` 已点名板块（"XLE 板块轮动" / "XLK −2.42%"）+ `corroborated_by_market`，但那是**盘前叙事（先验）**，非盘中 5m 战术信号。

### 4th-constraint watch 结论
用户预警的"`trade_records` 无法反解 universe"**未发生**：`trade_records.decision_id` 含 `universe_expansion:...` 前缀，且表已有 `mfe_pct/mae_pct/entry_session`。反解链通。

---

## 2. 关键发现：约束 2 + 约束 1 叠加 → 不造 harness

- 约束 2 单独 → 无历史分钟数据可回放。
- 约束 1 单独 → universe 仅 5 条 signal。
- **叠加**：即便补拉 5m bar，回放骨架"有 bar、无 signal batch"，仍是 empty run。**光解约束 2 救不了回放。**
- 但核实同时发现：**系统本身就是一个天天在跑、且把每条指令的入场/结果/tier 全落库（`playbook_instructions`）、且已在 live 跑 `_check_intraday_trigger` 的沙盒**。→ **不需要另造 harness**，让 live sim 当实验台即可。这就是 v3。

---

## 3. 方案 v3：内联影子日志 + 离线切片

### 3.1 核心洞察（副驾上的教练）
现有"突破触发"规则是**正在开车的司机**（live 决策 + 真实盈亏已落库）。新门（VWAP/量能/大盘）不抢方向盘，坐副驾**只记录**"这一刻我会说进还是等、依据是什么"。开够多趟后对账：**教练说『进』的那些趟，结局是否真的更好**。对得上账，才让他上手。

### 3.2 只做一件事（Step 0）
在现有触发点 `_check_intraday_trigger`（该点本就已取到 live `price/day_high/day_low/vwap/slope/spy`）——对**每一条走到触发阶段的信号（所有源）**，best-effort 追加一条 `data/entry_timing_shadow.jsonl`：
```
{ decision_id, inst_id, symbol, direction, source, ts, session_bucket,
  entry_price, current_price, live_triggered,          # 司机的真实决策
  vwap, vwap_deviation, slope_5m, spy_price, spy_ma20, spy_ma50,
  volume_ratio, rsi_14, atr, day_high, day_low }        # 门的原始特征(记全, 缺失置 null)
```
- **完全复用现有 `_emit_trigger_miss` 模式**（同一函数内已有的零行为改动 JSONL 埋点）：`try/except pass`、只 append、不影响触发判定与返回值。
- **不改任何 live 行为**：突破触发继续当 live 驱动（= 天然的 B2 baseline，内联在同一路径，无需重建）。

### 3.3 分析全离线做（算术，秒级，可反复）
- **过滤价值**：按 `live_triggered=True` 的真实入场单，用 `decision_id` join `playbook_instructions.pnl/mfe`，再按"新门 would_pass"切片比 PnL。→ "突破实际入的单里，同时过新门的那批是否更好"。
- **调参/换权重**：门的 tier1/tier2 是**已记录特征的加权求和**，换权重/阈值只是重算——**不需要原始 5m bar、不需要回放**。原 v2.0/v2.1 的组件消融(Step 2)+阈值搜索(Step 3) 全变离线切片，一下午跑完。
- **统计合法性**：突破已真金白银（模拟盘）入单、留真实结局，故这是**基于真实结局的过滤有效性检验**，无模拟撮合误差。

### 3.4 Q2 sector 退化仍适用
门为**两维**：`score = 0.6·z(s_vwap) + 0.4·z(s_vol)`；`sector_check='disabled'`（'disabled' ≠ 'degraded'）。sector ETF 5m RS 不补新基建；重启触发条件：一期分析显示"卡在板块脱离类病理"才回来补。

---

## 4. 相比 v2.0 / v2.1 删掉了什么

| v2.0/v2.1 里的东西 | v3 | 为什么能删 |
|---|---|---|
| Step 0 回放骨架 / harness | ❌删 | live sim 就是实验台 |
| Step 1 baseline 重建 | ❌删 | 突破触发是内联 live baseline |
| 5m bar 持久化 | ❌删 | 记**派生特征**、不回放 → 不需原始 bar |
| finnhub 补拉 | ❌删 | 同上 |
| futu K_5M 写盘消费者 | ❌删 | 整件事没了 |
| Step 2 串行 6 周 | ❌删 | 变离线切片，一下午 |
| A+B2 双轨记录 | ✅内联自然满足 | 突破=live 决策、新门=同点影子 |
| 全源影子（原 §5.4）| ✅**免费并入** | 特征对所有源都记 → 样本 ~10× |
| pre-reg/freeze/changelog 等纪律 | ⏸推迟到二期 | 一期纯观察零风险，不需要防 p-hacking |

---

## 5. 落地步骤

- **Step 0（唯一工程，~30 行、零行为改动）**：在 `_check_intraday_trigger` 加 `entry_timing_shadow.jsonl` 影子埋点（全源）。
- **Step 1（日历时间）**：live sim 边跑边攒样本；全源 → 每月 ~300+ 条（vs universe ~30），治功效不足。
- **Step 2（离线切片，可反复）**：过滤价值分析 + 权重/阈值网格；放行率目标 LONG 40-60%、SHORT 不为 0 且 >10%。
- **Step 3（条件性、二期）**：仅当一期证明过滤有效，才把某 config 切成 live 驱动，测"等待→晚点再入"的**择时增量**——**此时才启用 pre-registration / freeze-then-tune 全套纪律**。

---

## 6. 诚实边界
- 纯日志能测**过滤价值**（切片真实入场单）；**择时价值**（等待后晚入的反事实 PnL）观察测不了 → 分二期，二期才 live flip。
- 只记"走到触发阶段"的信号；只能用**记下来的特征**做离线消融 → **特征要记全**（log generously）。
- forward-only：影子仅从埋点上线**之后**积累（历史不可追）；样本量仍需日历时间。
- 出场逻辑全程用现有 1h 账本规则不变（只研究入场，出场held constant）。

## 7. 待办
1. Step 0 影子埋点（进行中）。
2. 攒样本（数周）。
3. 离线切片脚本（Step 2 时写，读 `entry_timing_shadow.jsonl` + join `playbook_instructions`）。
4. 二期 live flip（条件性，另行确认）。
