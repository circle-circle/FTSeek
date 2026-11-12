# DEB-20261110 — 11-09 美股收盘后 DA 全链与关联复盘

- 日期：2026-11-10 09:51 CST（11-09 20:37 ET 后复核）
- 状态：**MEASUREMENT_FIX_DEPLOYED；交易规则不变，下一开盘验收待做**
- 实施：2026-11-10 10:24–10:29 CST（§7）
- 范围：11-09 DA 候选→Gate→指令→成交→持夜，以及 VIX/行业/事件关联
- 机器摘要：`docs/analysis/2026-11-10-da-postclose-review.json`
- 前序：RFC-195、RFC-196、DEB-20261106 §64；本批只读生产 DB/checkpoint/日志，无重启、无交易写入

## 1. 先钉口径

本复盘分开四个集合：①11-09 新 DA cohort；②当日出场旧 DA；③EXT 研究臂；④未平仓 MTM。
官方 OKR 的 DA-only 净 bps 仍按 A9 已平仓口径，未平仓 MTM 只作风险补充，不能混入 KR。
价格用 ET 11-09 常规日 K；QCOM 盘后平仓另列。新闻关联不等于因果归因，单日 n<30 不做策略转正/证伪。

## 2. 真实链路和经济结果

36 份 DA 分析全部带 `risk_off`：BUY 27 / SHORT 6 / SKIP 3；Gate PASS 4 / WARN 18 / BLOCK 14。
落库 22 行 = 20 条方向指令 + 2 条 admission SKIP；按 `decision_id` 实际成交 **8/20=40%**。
其中 5 笔当日平仓、3 笔持夜，不是日报展示的 9 笔当日 DA 成交。

| 11-09 新 DA | 结果 |
|---|---:|
| 已平仓 5 笔毛额 / 费用 / 净额 | +$55.10 / -$20.99 / **+$34.11** |
| 费毛比 | **38.09%** |
| BA/GOOG/CAT 未平仓毛浮盈 | **-$208.48** |
| 已平仓净额 + 未平仓毛浮盈 | **-$174.37**（未扣未平仓费用，非最终 PnL） |

旧 DA GOOG 当日平仓另贡献 +$8.99，因此日报“核心已平仓 6 笔 +$43.10”本身算数正确，
但不能当作 11-09 新 DA cohort 已实现结果。日报常规收盘总账 18 笔净 +$287.82，其中 EXT 研究臂
12 笔 +$244.72；盘后 QCOM 于 19:20 ET 再平 +$24.73，故 20:37 ET 最终总账为 **19 笔 +$312.55**。
两个总数截止时点不同，不是互相矛盾。

三笔持夜按常规收盘毛 MTM：BA 175.79→172.38（21 股，-$71.61）、GOOG 292.22→286.01
（13 股，-$80.73）、CAT 空 590.86→598.88（7 股，-$56.14）。实际出场链有效止损分别为
166.99 / 277.52 / 622.80，均未被收盘穿越；GOOG `open_positions` 的 286.3756 不是实际出场止损，
不得误报漏平仓。EOD 持夜是既有规则结果。

## 3. 已证实的问题

### P0-A 日报计划配对错通道，执行率从 40% 虚增到 45%

`app/review/bundle_builder.py:139-213` 只按 `(symbol,direction)` 配对。APP 的 EXT 盘前 LONG
因此被误计为 DA APP 指令已执行，尽管 DA APP 的 `decision_id` 没有成交。TSLA 的 EXT LONG 与未执行
DA SHORT 也被混在同一“计划方向错配”视图。权威 `decision_id` 复算为 20 计划、8 成交。

`app/review/review_orchestrator.py:83-130` 已有正确锚：`metadata.playbook_instruction_id` 优先、
唯一非空 `decision_id` 兜底。修复应复用该锚，并检查 `daily_review._get_plan_vs_actual` 与
`PlanEffectivenessTracker` 的同型配对，新增“同标的同方向不同 decision_id”及“同标的跨通道反向”契约测试。
这是测量修复，不改任何交易行为。

### P0-B 已实现盈利掩盖未平仓风险

headline 只报已平仓，无法看到新 DA +$34.11 与三仓毛浮亏 -$208.48 并存。日报应增加
`cohort + realized_net + open_gross_mtm + mtm_as_of + effective_stop`，MTM 与 A9 分栏，禁止合并成 KR。
同时，新 DA 已平仓费毛比 38.09% 明显高于全通道 20.9%，应继续看费后净期望，不能只看 5/5 胜率。

### P0-C 开盘长步故障已修，下一开盘必须验收而不是再叠新机制

RFC-195/196 已确认：`trade_date` NameError 使长步反复崩溃，首轮 `auto_execute` 延至 09:40 ET；
供给完成跳过、09:20 启动截止和最多两次尝试已落地。下一交易日只需验证：
DA 供给完整跳过、零 NameError、首轮执行不再跨 09:30、候选/指令数量不减少。未验收前不再加门。

## 4. 关联性与反证

### 4.1 VIX/大盘：风险缓和成立，个股因果不成立

Futu 日 K：SPY 735.29→752.69，**+2.37%**。Yahoo `^VIX`：25.64→19.92，**-22.31%**；
CBOE VIX9D/VIX3M 分别收 19.88/20.85，较前日 -26.86%/-15.24%，期限结构恢复正常。
复盘时 CBOE `VIX_History.csv` 仍停在 11-06，故 19.92 明标 Yahoo 单源，等 CBOE 现货文件再确认。

36 份 DA prompt 都显示 VIXY 代理 17.88，而上一现货真值为 25.64，低 7.76 点；
但 09:40 regime 仍为 `risk_off`，日志明确主要由 SPY 低于 MA20/MA50 驱动。因此不能把成交方向或亏损
归因为“VIX 门放松”，也不能把 SPY/VIX 反弹回落直接解释 BA/GOOG/CAT 的个股路径。

只读估计器在复盘时给 22.84，相对 Yahoo 19.92 高 2.92 点（+14.66%）。这是一日前瞻残差，
应进入 truth-vs-estimate 台账，攒既有 N≥10 再评；**不因一天改 beta，不把估计接交易门**。

### 4.2 重定价：GOOG 是审计样本，不是立刻取消 chase cap 的证据

GOOG 原始计划 286、禁追线 292.20，最终却成交 292.22，收盘回 286.01；这是“原计划与执行偏移”
的明确复盘样本。反面样本也存在：LLY 计划 1354→成交 1379.38、XOM 162.5→164.29，均净盈利。
APP/LMT/GS 等原始低吸价全日未到，不能把后续上涨直接称为原策略错失收益。

下一步应用 1m/5m as-of bar 重放“原计划等待 vs 当前 reprice”，按成交时刻、可成交价、费用分账；
n≥30、日聚类与 MDE 后再裁，不能直接关闭 reprice/chase cap。

### 4.3 出场后上涨主要来自 EXT，不应全球放宽 DA 出场

DA 已平仓中，LLY/MRK 出场后至收盘再涨 2.06%/1.07%，XOM/UNH 近持平，
旧 GOOG/PDD 则分别回落 0.62%/1.00%。日报全通道较大的退出后涨幅主要受 MRVL/AVGO/MU/CRWV/APP
等 EXT 样本影响。应按通道和出场规则分账，不做全局延长持有。

### 4.4 证据覆盖不等于事件因果闭环

36/36 证据卡可用、500 条新闻引用齐全，但 176 条 bull/bear 论点精确链接仍为 **0/176**。
因此本复盘只把财报、评级、行业与价格路径作为观察关联，不声称某条新闻导致了推荐或收益。
SRC/STORY 离线 A/B 的生产 `NOT_APPROVED` 结论不变。

## 5. 其他测量缺口与建议顺序

1. 日报“0 个入场问题”旁边同时存在可行动 MFE **0/9、缺 bar 9**，这不是质量全绿；
   先补 as-of bar 覆盖，再判入场/延迟反事实。
2. 日报增加 `as_of`，并在盘后最后交易后二次结算，避免 18 笔与 19 笔被当冲突。
3. GOOG `open_positions.initial_sl/current_sl=286.3756` 与实际止损 277.52 不一致，属于 RFC-195 前
   建仓的存量/恢复路径待核项；先回填核对并验证 `sync_recovery`，不改实际出场参数。
4. 实施顺序：**先修日报 decision_id 配对与 MTM → 验收下一开盘 RFC-195/196 → 补 bar 做 reprice/exit shadow →
   攒 VIX 前瞻残差**。本轮未修改 app/config/prompt/阈值/仓位，未重启服务。

## 6. 可复现检查

```bash
# 当日方向指令与 decision_id 成交
venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
print(c.execute("select count(*) from playbook_instructions where date='2026-11-09' and row_kind='instruction' and direction in ('BUY','SELL_SHORT')").fetchone())
print(c.execute("select count(*) from trade_records where trade_date='2026-11-09' and decision_id like 'DA-%'").fetchone())
PY

# 现成日报的错误配对
venv/bin/python - <<'PY'
import sqlite3
from app.review.bundle_builder import build_plan_vs_actual
c=sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True); c.row_factory=sqlite3.Row
print({k:getattr(v,'value',v) for k,v in build_plan_vs_actual(c,'2026-11-09').items()})
PY
```

原始证据：`data/da_checkpoint/2026-11-09/`、`data/ftseek_us.db`、
`data/reviews/REVIEW_2026-11-09.md`、`logs/ftseek_us.log-20261110`、`data/cache/daily_kline_v1.json`。

## 7. 10:24 实施轮：测量修复已部署，成交后路径只支持继续影子验证

§5 第 4 项的“未修改 app/未重启”仅指 09:51 只读复盘轮。用户随后明确批准按建议执行，
本轮完成测量修复并部署；**没有修改 VIX、chase cap、候选数量、仓位或任何退出参数**。

### 7.1 已落地的根因修复

1. 新增 `app/playbook/instruction_attribution.py`：统一 `metadata.playbook_instruction_id` 优先、
   全表唯一非空 `decision_id` 兜底；禁止再用 symbol/direction 猜测归属。
2. `bundle_builder`、`daily_review._get_plan_vs_actual`、`PlanEffectivenessTracker` 三路同步修复。
   11-09 只读重算一致为 **20 计划 / 8 执行 / 40% / 10 笔 EXT 未归属 / 无方向错配**；
   APP 同向假执行与 TSLA 跨通道假反向均消失。同标的同方向不同 decision、跨通道反向、
   重复 decision 时 metadata 优先均有契约测试。
3. 日报新增新 DA cohort 常规收盘风险分栏：已平仓毛额、费用、费后净额、费毛比、
   未平仓毛 MTM、`mtm_as_of`、逐仓有效止损；缺精确日 K 时总额保持 `N/A`，不以 0 或部分和冒充全量。
   11-09 实盘只读渲染复现：5 笔净 +$34.11、费用 $20.99、费毛比 38.09%，
   3 笔毛 MTM -$208.48，GOOG 有效止损取实际链 277.52 而非 `open_positions` 回填值 286.3756。
4. 既有永久测量项 `post_exit_path` 按 `decision_id` 分 DA/EXT，未新增影子或交易门。

### 7.2 成交后路径给出的明确判断

| 出场后至常规收盘 | n | close_gap 均值 / 中位 | >1% |
|---|---:|---:|---:|
| 全通道混合 | 18 | +2.8584pp / +3.0337pp | 11 |
| DA（按退出日，含旧 cohort） | 6 | **+0.2749pp / +0.0715pp** | 2（LLY/MRK） |
| EXT | 12 | **+4.1501pp / +4.7683pp** | 9 |

混合总览的“早退错过上涨”主要由 EXT 驱动，不能拿来全球放宽 DA 出场。新 DA 自身也相互矛盾：
LLY/MRK 后续 +2.0564/+1.0701pp，但 PDD 为 -0.9962pp，XOM/UNH 近持平。因此当前**没有依据**
直接放宽保本/跟踪退出；继续用既有 `post_exit_path`/`exit_param_shadow` 做费后反事实，
按退出原因、通道、日聚类与 MDE 评估。

更值得跟踪的是**低 MFE 且水下持夜**：BA/GOOG/CAT 的 `_mfe_gate_pct` 分别为
0.2190/0.3525/0.3097，16:00:53 ET 净水下 2.0476/2.2342/1.4586%，三笔均落入既有
RFC-160 `eod_mfe_gate_shadow`（MFE<0.5% ∧ 水下）的观察形态。该现象只进入既有影子累计，
**不新造同轴门、不立即强平**；隔夜结果尚未发生，不能提前宣称优化收益。

入场侧同理：GOOG 的 reprice 后回落与 BA/GOOG/CAT 低 MFE 值得用 as-of 1m/5m bar 复核，
但 LLY/XOM 重定价后仍费后盈利，不能一天内关闭 chase cap。费用占新 DA 已平仓毛额 38.09%，
后续比较任何入场/退出变体都必须用费后净额，不用“持有更久”或“交易更多”追样本。

### 7.3 验证与部署回执

- 聚焦及 broader 回归：**157 passed, 1 skipped**；最终渲染相关复测 16 passed。
- 生产 DB 只读构建通过，输出 20/8/40%、新 DA 风险分栏和 DA/EXT 路径分账；
  `git diff --check`、JSON 解析、`py_compile` 通过。
- 重启前未平 BA/CAT/CVS/GOOG，`app/scripts` 仅本批改动；`systemctl restart ftseek-us` 后
  启动冒烟 **7/7**，服务 active，启动段零 ERROR/CRITICAL/Traceback，四个未平仓保持不变。
- 最终代码微修后按同一规程再次重启/冒烟，最新回执以 handoff 顶部为准。

下一步保持不变：下一开盘验收 RFC-195/196；补 as-of bar 做 reprice/exit 反事实；
既有 RFC-160 与 VIX 残差攒样。不得把本批测量修复描述成交易 edge 改善。

## 8. 11:05 补轮：RFC-160 终裁纠偏、GOOG 存量校正与分钟回放

### 8.1 RFC-160 已不是“继续观察等待转硬闸”

本节**取代 §7.2 和上一段下一步中“RFC-160 继续攒样/观察候选”的时效表述**，不静默改写历史。
11-10 06:30 最新结算已满足样本门：`mfe_track.n=12`、`ΣΔ=+$52.14`、`miskill_n=9`，
机械 verdict=`GATE_NOT_WORTH`。误杀 9>1 且已结算误杀不随新增样本下降，转硬闸路径正式关闭。
已追加 RFC-160 §8、OKR KR3 变更记录，注册表转 `KILLED`。共享 arm/结算 record-only 续采，
config/cron/实际 EOD 豁免不变；BA/GOOG/CAT 不强平，不新造同轴门。

### 8.2 GOOG 存量 open_positions 已做严格 CAS 校正

GOOG 是 RFC-195 未来恢复修复上线前建立的存量行。实际出场读 `trade_records.id=1826`，
指令真值 SL/TP=277.52/314.04；旧 `open_positions` 却为 286.3756/382.70。
在在线备份后，以 symbol、SIM 身份、全部旧值、无券商 SL/TP 单及唯一未平 trade 为前置条件，
单事务仅更新 GOOG 一行：initial/current SL→277.52、initial TP→314.04、空 TP JSON→同一目标。
`trade_records` 与实际出场参数未改，BA/CAT 前后不变。备份和前后审计：

- `data/audits/ftseek_us_before_goog_rfc195_cas_20261110T025855Z.db`
- `data/audits/goog_open_position_rfc195_cas_20261110T025855Z.json`

止损必须按时间口径读：日志中的 288.545 是早段 `early_tight` 线；常规收盘时该窗口已结束，
最新 `_effective_sl_observed=277.52`，所以日报的常规收盘有效止损 277.52 正确。
同时，GOOG 成交价 RR=1.484354<1.5，`_tp_rr_gate.swallowed=true`：314.04 是指令目标，
**不是仍有效的固定止盈触发线**，实际由 trailing/EOD 管理。不能把校正 initial TP 描述成新建止盈单。

### 8.3 GOOG 原低吸计划没有“早盘本可成交”的分钟证据

390 根 1m bar 显示：13:18 首次触到/收在 288 下方，顺序聚合的 5m 诊断在 13:20 首次收在
288 下方；但直到 **16:00 最后一根 1m bar** 才首次出现 low≤286（该根 low=285.94、
close=286.01），全天没有 1m close≤286。故原 286 低吸价不能被倒推为早盘可成交并规避浮亏。
“企稳”没有预注册的唯一 1m/5m 算法，OHLC 触价也不等于确认后必然成交。

实际链在 10:01:36 以 292.13 决策，10:01:48 以 292.22 成交；决策价在 292.20 cap 内，
成交价高 cap $0.02（约 0.68bp），说明当前 cap 约束决策价而非保证最高成交价。
计划/决策/成交 RR=2.5333/1.4997/1.484354。若只为费用敏感性而假设两边都在 16:00 平仓，
13 股往返费均估 $4.1446；实际仍持仓，该估费不是已实现费用或最终 PnL。

8 笔新 DA 的描述性复算为 6 笔计划→有效 RR 下调、2 笔上调；但已平仓 5 笔全部费后为正，
LLY/XOM 也是重定价后净 +$8.27/+$5.45。故只登记“RR 漂移与 cap 决策价/成交价差异”测量缺口，
不关闭 chase cap、不开启限价路由、不新增 RR 门、不减少标的。机器证据见
`docs/analysis/2026-11-10-da-reprice-minute-replay.json`。

### 8.4 验证与部署回执

- 两份机器摘要 JSON 解析、bar 数量/hash/首次触价、DB 校正值、8 笔 RR 方向与注册表状态断言通过；
  `git diff --check` 通过。
- 聚焦回归 **71 passed**；`test_shadow_criteria_are_visible_in_registry` 另有 **1 个既有 A11 可见性失败**。
  本批未改 `docs/ADJUDICATED-CRITERIA.md` 或 A11，RFC-160/结算/止损/复盘相关用例均通过，不顺带扩scope修A11。
- 重启前未平 BA/CAT/CVS/GOOG，`app/scripts` 只含本任务连续两批改动；`systemctl restart ftseek-us` 后
  启动冒烟 **7/7**，服务 active、PID=2454012，启动段无 ERROR/CRITICAL/Traceback。
- 重启后四个未平仓保持不变，GOOG open_positions 校正仍为 277.52/277.52/314.04；
  RFC-160 运行时状态为 KILLED，`collection_retired=False`，共享采集未停。

下一步：下一开盘验收 RFC-195/196；VIX 真值残差仍按既有 N≥10 基线，不接交易门。
RR 漂移与 cap 成交价差异只做跨日、费后、按日聚类测量，样本与 MDE 不足时不提新门。
