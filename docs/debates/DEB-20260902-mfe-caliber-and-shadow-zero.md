# DEB-20260902 指令表 MFE/MAE 口径污染 + pullback_limit 影子"假零" (9/1 复盘新发现)

**日期**: 2026-09-02
**触发**: 9/1 全链复盘 (Qoder) 两项既有 DEB 未覆盖的新发现
**状态**: 已裁决执行 (2026-09-02 用户裁"按建议执行": 议题一方案A + 议题二建议1-3; 执行中发现同根议题三一并修复)

## 议题一: playbook_instructions MFE/MAE 持仓期口径污染

### 病灶实证 (ORCL 9/1)

- 台账 `max_favorable_excursion=118.4` (+2.06%), 实锤来源 = **09:31 ET 开盘 spike 146.77/146.79, 比建仓 10:02:39 ET 早 31 分钟** — 仓位从未见过这个价 (1min bar 入场后真实峰值 144.30 @10:12, +0.33% = $18.8, 台账虚增 **6.3×**)。
- 机制: `shadow_tracker._calc_mfe_mae` (app/playbook/shadow_tracker.py:339) 用**日 bar** high/low 算 MFE/MAE, 无 entry_time 概念 → 凡顺延/盘中延迟入场 (9/1 常态: 单轮3笔上限+冻结致 ORCL/EIX 顺延 3 轮), 入场前的当日极端值全部计入。
- 符号混装 (同列第二病): shadow_tracker 持仓行写**负** MAE (ORCL -155.2), 平仓路径 `_close_trade_record` (app/playbook/pipeline.py:6183-6190) 写**正** MAE (SAIC +71.76) — 同一列符号取决于仓位状态。单位混装第四案后军规已立 (AGENT.md), 此为存量旧字段同款病。

### 影响面 (已逐消费者核查)

- **干净**: 已平仓终态行 — 平仓路径以 tick 级 meta peak/trough 落终值, 且 shadow_tracker 终态守卫 (shadow_tracker.py:129, 6/24 起) 跳过 win/loss 行不再覆写。9/1 已平 13 笔逐笔验证 = tick 口径 (如 LLY 台账 0.00 = 真实 MFE 0)。
- **干净**: gap_alignment 影子 (读 trade_records.mfe_pct, 非指令表)、symbol_profile (过滤 outcome win/loss)。
- **污染**: 一切读**持仓中**行 (outcome=holding/NULL) 的消费者 — feishu_push 盘中推送显示、review_orchestrator、以及**任何临时复盘查询** (本次复盘初读即差点把 ORCL +2.06% 假 MFE 写进结论, 属实例自证)。持仓越久污染存续越久; 隔夜仓跨日时"日 bar"仍是入场日的, 失真随时间放大。
- 零交易行为影响 (盘中规则用 position meta tick 级 peak/trough, 不读此列)。

### 可选方案

**方案 A (推荐): 持仓行 MFE/MAE 改同源 tick 口径**
- 核心: shadow_tracker 对 executed 持仓行改读 trade_records.metadata_json 的 peak_price/trough_price (与平仓终值同源), MFE=(peak−exec)×size, MAE 统一正数约定; 日 bar 路径删除 (或仅在 meta 缺失时兜底并打标)。
- 优点: 盘中读数与平仓终值口径一致, 符号统一, 所有现存消费者零改动自动正确。
- 缺点: peak/trough 是轮询采样粒度 (会错过采样点间极值) — 但平仓终值同口径, "一致"优先于"更细"。
- 风险: 低, 纯观察层 SQL 读源切换; 需 2-3 契约测试 (LONG/SHORT/符号约定)。

**方案 B (最小): 加 mfe_source 标记**
- 日 bar 值照写, 加 `mfe_source='daily_bar_est'`, 推送显示加 `~` 前缀。
- 优点: 零风险。缺点: 假值继续流, 每个消费者要自己看懂标记 — 打标式修复正是单位混装军规否定 pattern。

**方案 C: 不动** — 平仓自纠正。缺点: 盘中分析/复盘持续被骗, 隔夜仓污染跨日存续。

### 裁决点

1. 选 A/B/C; 2. 若 A: 历史无回填 (持仓中行随平仓自愈), ORCL 行平仓时自动纠正, 无需数据修复。

---

## 议题二: pullback_limit 影子首日"假零"与判读起点口径

### 事实 (逐环核实)

- 9/1 全天触发影子行零落盘 (`json.loads(dict)` bug, 修复 7e4d51cb 落於 23:13 CST **盘中**; 此后 notional cap 已满 + 无新 archetype 触发 → `data/shadow/pullback_limit_shadow.jsonl` **至今从未创建**)。已在 DEB-20260901 §10.1 披露"EIX 已过触发行不可回补"。
- replay 主输入是 DB `raw_instruction_json` (影子文件可选), 故 9/2 05:47 首判读**正常产出**: n=11 带 archetype 指令, actual_net +55.79 vs CF_net +237.30 (近似上界, note 已载"价格条件下界/量价未重放/CF 出场仅 SL|EOD")。
- **残留风险**: 报告 `n_chase_cap_violated=0` 是"影子未采集"的**假零**, 报告内无口径注记 — 判读时会误读为"零违例", 与"采到零违例"语义相反 (chase_cap 恰是 DEB-20260901 立案的核心疑点之一)。

### 处置建议 (零行为变更, 仅报告口径)

1. replay 报告: 影子文件缺失/无行的日期, `n_chase_cap_violated` 输出 `null` + note "trigger shadow 未采集" (而非 0); 有行时正常计数。
2. 注册表 `pullback_limit_shadow` notes 更新判读窗口分行口径: 判据 n≥20 的"可配对样本"自 9/1 起算 (指令侧 DB 重构可信); chase_cap 违例率维度自 9/2 起算 (影子侧首采夜), 两侧起点不同如实标注。
3. 契约测试 1 例: 影子文件缺失 → n_chase_cap_violated 为 null 且 note 含"未采集"。

### 裁决点

是否按上述 1-3 执行 (预估改动 <30 行 + 1 测试)。

---

## 议题三 (执行中发现, 同根): TradeStore._row_to_dict 位置映射漂移 → P2b 联接守卫纸面修复

落实议题一时发现 `_find_executed_trade` 联接仍取不到 decision_id, 追到第三处同根病灶:

- **事实**: `_row_to_dict` (app/storage/trade_store.py) 按位置 (row[0..15]) 映射 SELECT 列。8/27 给 `query_by_date` SELECT 补 `decision_id` 列但映射未同步 → 实库 9/1 全部 14 行 `dict["created_at"]` 槽实装 decision_id 值、`dict["decision_id"]` 键缺失。**8/27 P2b 轮次级联接守卫 (防再武装指令行认领前轮交易) 名义已修, 实际取不到 decision_id 恒走兜底 — 修复纸面化**。
- **query_range 更直接**: 17 列 SELECT 共享 19 列位置映射, 实测调用即 `IndexError: tuple index out of range` (该路径至今无人调用过才未爆)。
- **修复**: `query_by_date`/`query_range` 设 `conn.row_factory = sqlite3.Row` + `_row_to_dict` 改按列名 `dict(row)` 映射 (SELECT 列序漂移时要么缺键要么显式 KeyError, 不再静默错位); `query_by_date` SELECT 补 `metadata_json` 列 (议题一方案A 需要)。这是单位/位置混装军规的第五案, 但军规的契约断言已覆盖 (新测试断言 created_at 是时间戳格式)。

---

## 执行记录 (2026-09-02, 用户裁"按建议执行")

**议题一·方案A** (app/playbook/shadow_tracker.py `_calc_mfe_mae`):
- 主路径: executed 持仓行读 `trade_records.metadata_json` peak_price/trough_price (与平仓终值同源, 轮询采样粒度) — LONG: MFE=(peak−exec)×size, MAE=(exec−trough)×size; SHORT 对称。MAE **统一正数**约定 (原持仓行写负数, 与平仓路径 `_close_trade_record` 的正数同列符号混装一并消除)。
- 兜底: meta 峰谷不可得 (联接失败/字段缺失) 时回退原日 bar 路径, 打 WARNING "[shadow_tracker] ... 回退日 bar 口径 (含入场前极值, DEB-20260902 议题一知悉)" — 不静默。
- ORCL 9/1 干跑 (修复前→后): MFE 118.4 (+2.06%) → **16.4 (+0.33%, =1min 真实峰值 $18.8 口径)**; MAE −155.2 (负号) → **+154.4**。虚增 6.3× 消除。
- 历史无回填 (裁决点2): 持仓中行随平仓自愈 (平仓路径 tick 终值覆写), 与方案A预案一致。

**议题二·建议1-3** (scripts/cf_pullback_limit_replay.py + shadow_registry.py):
- 建议1 + 补强(行级): 影子未采集日期 summary `n_chase_cap_violated=null` + note 追加"trigger shadow 未采集 → chase_cap 维度 null (非零)"; **行级** `chase_cap_violated=null` (非 False — False 是"采到零违例")。有行时正常计数。
- 建议2: 注册表 pullback_limit_shadow notes 双起点口径 (判据配对样本自 9/1 / chase_cap 违例率维度自 9/2, 9/1 该维度输出 null 非零已注记)。
- 建议3: 契约测试 3 例 (文件缺失→summary null+note / 行级 null / 有行正常计数) — 临时库合成环境钉住。
- 衍生 (消费者同病): deb20260901_shadow_first_report.py 第二节原"影子文件不存在 → 打印 触发行 0; 违例 0"同款假零, 改为"⚠️ 影子文件不存在 → 当日未采集 (计数无效)"。
- 真实 9/1 数据干跑 (临时输出, 不向生产 jsonl 重复追加 — `cumulative_pairs` 读全量日志, 重跑会双计 9/1 配对污染 n≥20 分母): 11/11 行 null, summary null, note 带注记, CF 净值与 05:47 旧跑逐笔一致 (237.30/55.79 — 只有 chase_cap 维度从假 0 变 null)。

**议题三**: 见上节, trade_store.py 按列名映射 + row_factory + metadata_json 列。

**测试**: tests/test_shadow_tracker.py 48 passed (旧 MFE/MAE 2 例重锚至日bar兜底路径正数口径 + 新 TestMfeMaeTickCaliber 6 例 + 新 TestTradeStoreNameMapping 3 例); tests/test_pullback_limit_shadow_telemetry.py 7 passed (新增 3)。

**部署注意**: shadow_tracker/trade_store 变更在主进程 (daily_tracking 于 EOD 跑, pipeline.py), **须在今晚开盘前重启生效**; 现存持仓行 (ORCL/EIX) 今夜 EOD 追踪即落 tick 口径, 平仓时终值覆写自愈。零交易行为变更 (盘中规则读 position meta, 不读指令表 MFE 列); 无 yaml 键变更, exit-f837b002896d 不变 (KR2)。

---

## 附: 复盘同时验证为"无问题"的项 (免再查)

- 3 笔被拦再入场反事实 (TSLA -0.75% / RIVN -0.32% / BABA -0.60% 收盘口径): notional cap 拦截方向正确。
- 规则 0.5 窗口语义: 30min 窗过期后紧 SL 让位原始 SL 是设计行为 (ORCL 75min 时 -1.11% 破 142.39 紧 SL 未触发, 非 bug)。
- 已平仓 13 笔 MFE 台账值 = tick 口径 (污染仅限持仓行, 见议题一)。
