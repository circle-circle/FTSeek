# RFC-200 · 开盘禁入窗 0945 + auto_execute 早退闸 SSOT 化

- 日期: 2026-11-12 (CST)
- 状态: 已落地 (SIM), 观察期开始
- 关联: `playbook.execution_parameters.entry_open_blackout_{enabled,hhmm}`、
  `app/playbook/pipeline_parts/auto_execute_flow.py`、
  `app/playbook/pipeline_parts/trigger_flow.py::check_intraday_trigger`
- 回滚: `entry_open_blackout_hhmm` 改回 `0935` (热载 ~60s)。代码侧不硬编码, 无需回滚代码。

## 1. 改了什么

| # | 位置 | 改动 | 类别 |
|---|---|---|---|
| 1 | `config/unified_config.yaml` (execution_parameters) | `entry_open_blackout_hhmm: 0935 → 0945` | 交易行为 (L4/L2) |
| 2 | `auto_execute_flow.py` | 删硬编码 `_market_open_min + 5` (=09:35 floor), 改读同一配置键; 决策抽为纯函数 `open_floor_minutes()` | 缺陷修复 (两处真源 → 单一真源) |
| 3 | `tests/test_auto_execute_open_floor.py` | 新增 16 例 (配置→floor 语义 / 冻结时钟真行为 / 源码契约) | 守卫 |

为什么 2 必须跟着做: 改之前同一语义有两个真源 —— 外层 `auto_execute` 的硬编码
09:35 floor 与内层 `check_intraday_trigger` 的 `entry_open_blackout_hhmm`。
**谁 binding 取决于谁更晚**: 0945 生效后硬编码那行变成死码; 反过来只改硬编码又会被
config 覆盖。任一方只改一处都会得到"改了没反应"的假象。

关闸语义 (`enabled=false`) 与非法值语义保持旧行为: 回落 09:35 (旧的 +5 噪声底线),
**不会**放开成 09:30 裸执行 (2026-06-24 的开盘噪声证据仍成立)。

## 2. 证据 (只读实测, 全部可复跑)

### 2.1 被切掉的人群 = 09:35-09:45 (不是整个 09:30-09:45)

`trade_records`, `trade_date >= 2026-06-01`, 按 ET 入场分钟分桶:

| 桶 | n | 交易日 | 胜率 | 净额 | 均值 |
|---|---|---|---|---|---|
| 09:30-09:35 | 21 | 2 | 57.1% | −$6,972 | −$332.0 |
| **09:35-09:45** | **405** | **78** | **61.2%** | **−$2,648** | **−$6.5** |
| 09:45-10:00 | 168 | 58 | 78.0% | +$482 | +$2.9 |
| 10:00-11:00 | 142 | — | 53.5% | +$356 | +$2.5 |
| ≥11:00 | 86 | — | 60.5% | +$756 | +$8.8 |

- 09:30-09:35 只有 2 个交易日 (6/03、6/23), 是 6/24 加 +5 闸之前的残留 ⇒ **不是**现行人群。
- 09:35-09:45 在三个时段都稳定负: `<08-15` avg −$7.9 (n=139) / `08-15..10-31` −$5.9 (n=223) /
  `11-01+` −$5.4 (n=43)。期内日均笔数 4.2 (8/15-10/31) → 6.1 (11-01+) ⇒ 每天约 4-6 笔要被切掉。

### 2.2 诚实反方: 这一档同时装着一批真赢家

| 桶 | n | MFE≥1.5% 笔数 | 其中净赚 | 该组净额 |
|---|---|---|---|---|
| 09:35-09:45 | 405 | 66 (16%) | 62 | **+$4,847** |
| 09:45-10:00 | 168 | 23 (14%) | 23 | +$2,153 |

⇒ 切掉这一档 = 删掉 −$7,495 的尾部, 同时删掉 +$4,847 的赢家, 历史净改善 ≈ **+$2,648/78 日 ≈ $34/日**。
出场构成也印证"小额保护出场多、巨亏集中": `SL_BREAKEVEN` 158 + `SL_TRAIL_LOCK` 117 = 275/405 (68%),
最大 5 笔亏损合计 −$1,888 (占整档净亏 71%)。

### 2.3 这 15 分钟"能给"什么 (决定"延后"是否有正面理由)

- **开盘区间只有到 09:45 才完整**: `opening_high/low` 定义 = 前 15 根 1 分钟 K
  (`market_data_router.py`, `opening_range_ready = len(today_bars) >= 15`)。
  `entry_timing_shadow` 实测: 09:35-09:43 ready 比例 **0-4%**, 09:44 **94%**, 09:45 起 **100%**。
  0935 时代第一次评估拿到的是 5 分钟半成品区间 ⇒ 突破当日高点更容易 = 噪声突破。
- **盘口**: 09:37-09:44 中位价差 8.9-14.2bps; 09:47 之后 6.1-9.8bps (改善方向对, 量级 ~3bps, 非主要收益源)。
- **评估形态**: 09:35-09:45 段 `live_triggered` 67.4% (740 行中 499), 且 `entry_path` 以 **chase** 为主 (418 chase / 301 pullback);
  09:45-10:00 段 trigger 率降到 45.0%, pullback 反超 (391 vs 214)。
- **反向排除**: VWAP 不是这 15 分钟带来的 —— 09:35 时 `vwap_populated` 已 100%。

## 3. 已知未解决 (不给"已解决"的假象)

1. **观测黑洞 (P1)**: `auto_execute` 在闸内 `return None`, 逐条 `check_intraday_trigger` 一次都不跑 ⇒
   09:30-09:45 **零遥测**。实测 20 日 `trigger_miss_telemetry`: 09:30-09:34 = **0 行**, 09:35 起才有行;
   内层 `before_open_blackout` 20 日 **0 次** 触发 (被外层先挡)。⇒ 本次改动的收益/代价**无法用前向数据裁**,
   只能靠上面的历史回放。若要前向证据, 需要黑窗内 record-only 打点 (未做)。
2. **内层打点本身是废的**: `_emit_trigger_miss("before_open_blackout", None)` 传 `None` ⇒ 落盘
   `current_price=0 / gap_pct=0`。一旦有人下调外层 floor, 这行仍然读不出任何东西 (未改, 是 1 行增量)。
3. **追高没被解决, 只是推后**: 09:45 时当日 high 往往就是开盘区间 high, "突破当日高点"= 追在 15 分钟最高点。
   `entry_reanchor_enabled=false` ⇒ 出带的单只能整轮跳过。这是"延后"的真实代价, 也是下一个该单独裁决的旋钮。
4. **永久作废判据押在单点上**: `thesis_drift_invalidate` (5%) / `extreme_gap` (>8%) 用**一个时点价**
   永久写 `skip_reason`。窗口后移 10 分钟 ⇒ 这个永久判决落在开盘最动荡时段**末端的那一跳**上。
   若要稳, 应改为窗内 VWAP/中位或连续 N 轮确认 (未改, 属行为变更, 需独立预注册)。
5. `scripts/archive/gen_trading_ssot.py` 曾用正则抓 `_market_open_min + (\d+)` —— 该字面量已删,
   该脚本参数快照会少这一项。它是已归档脚本且 pre-commit 里本就是哑弹 (`|| true`), 不影响生产。

## 4. 落地与验收

- 重启取证: 文件 mtime `2026-11-12 10:53:21 CST` < 进程启动 `10:55:10 CST` (新 PID), 重启后 ERROR=0。
- 配置读值 (新进程): `open_floor_minutes() == (585, 945)` = 09:45 ET。
- 测试: `tests/test_auto_execute_open_floor.py` 16 passed; 修前基线 (worktree @HEAD) 同测试 **12/16 红** (变异验证)。
- 回归: `tests/test_playbook_pipeline.py` 74 passed; `test_exec_priority_first` /
  `test_same_day_hard_stop_cooldown` / `test_premarket_overrun_cancel` 25 passed。
- **未验证 (时段依赖, 今晚 09:30 ET 后才能观测)**: 首单时间戳 ≥ 09:45 ET、09:30-09:45 零入场、
  黑窗日志行 (`开盘禁入窗内不执行`) 首次出现。在此之前只能说"代码已加载 + 配置读值正确"。

## 5. 观察判据 (本次实验, 一晚样本只作观察)

1. 首单 ET 时间戳 ≥ 09:45; 09:30-09:45 零入场。
2. 与近 5 日同档对照: 首单笔数是否被"掐掉机会" (对照 09:45-10:00 桶历史 4-6 笔/日)。
3. 反方: 是否少了 MFE≥1.5% 的大赢家 (历史该档 16%、+$4.8k/78 日)。
4. 停止条件: 若连续 3 个交易日出现"9:45 后首单即被 chase cap/出带跳过"占比明显上升, 视为延后把机会转成了空转, 回滚 0935。

## 6. 追加批 A (2026-11-12, 用户裁「A」): 黑窗内只读观测

**动机**: §3.1 的观测黑洞使本次改动无法用前向数据裁决。用户裁 A = 只加观测, 不改判定。

**落地**:
- `auto_execute_flow.emit_blackout_shadow()` — 闸内 `return None` 前调用, 纯 append;
  每轮一行 `kind=cycle` (n_inst / n_priced / n_would_trigger, 证明观测自身活着) +
  每条指令**首次**落带时一行 `kind=hit`; 落 `data/entry_blackout_shadow.jsonl`。
- 配置 `playbook.execution_parameters.entry_blackout_shadow_enabled: true` (回滚 false, 热载)。
- **硬约束 (测试钉住)**: 不写 DB / 不改 skip_reason / 不做永久作废 / 不下单 / 不 reprice。
- **口径**: 只算 band 几何 + gap; 后续门 (VWAP/低ATR/流动性/借券/漂移/极端gap/admission)
  未观测 ⇒ `band_would_trigger` 是**上限**, 不是"会成交"。行内 `caveat` 字段写死这句。
- 窗口内量能门处开盘豁免 (`intraday_vol_gate_grace_min: 15`), 故 vol_ok 恒真且不必取量;
  行里标 `vol_assumed_ok` 供 grace 被改小时事后剔除。
- 附带修复: 内层 `_emit_trigger_miss("before_open_blackout", None)` → 传 `current_price`
  (原来落盘 `current_price=0/gap_pct=0`, 外层 floor 一旦低于本键该行仍无信息量)。

**顺带的真减法**: 触发判定原有三份同义分支 (生产 `check_intraday_trigger` / 双锚影子
`_anchor_trigger_geometry` / 本观测)。抽成 `entry_band.classify_trigger()` 唯一实现, 两处委托。
`test_geometry_stays_in_sync_with_production_branches` 从"token 扫描生产段"改为"钉
三处都委托同一函数 + 分支字面量只许出现在 entry_band.py" —— 重新内联一份同义分支即红。

**观测成本**: 黑窗 15min ≈ 5 轮 × 每指令 1 次 `get_full_market_context` (与正常轮次同量级);
相对全天 ~120 轮 ≈ +4% 快照负载。

**追加批 A 当时的未做项 (R1 已补)**: 该台账最初未注册进 `app/playbook/shadow_registry.py`，
缺 `check_mounting` 停摆守卫。R1 已新增 evaluator 并注册 `entry_blackout_shadow`
（永久测量、按需评估）；无 cycle / QUOTE_ALL_FAIL 均非零，详见 §8。

## 7. entry_confirm (RFC-141) 与 0945 的关系 — 结论: **本批不动它**

用户 2026-11-12 追问「开盘交易后 entry_confirm 的逻辑是否需要调整」。实测结论是不需要改参数,
但有三条口径必须写进判读:

1. **无证据支持调整**: `data/shadow/entry_confirm_forward.jsonl` 仅 **69 条 arm**
   (2026-11-05~11-11, 5 个交易日)。按 `session_min` 分桶: `0-15` n=48 放行 71% /
   `15-30` n=10 放行 70% / `30-60` n=8 放行 62% / `>=60` n=3 放行 67% ——
   差异在噪声内, 且桶内 n<40 = 无功效。**按这个样本调参 = 拿噪声当信号**。
2. **判据人群已换 (最重要的口径风险)**: 上述 69 条里 **48 条 (70%)** 落在 `session_min<15`,
   即 09:30-09:45 段 —— 这正是 0945 之后不再产生 arm 的窗口。⇒ RFC-141 预注册的裁决
   基线是"开门窗为主"的人群, 0945 之后的人群变了。**引用 11-05..11-11 的读数续判 = 跨口径**,
   裁决时必须按 `session_min` 分桶并声明"门后人群已变", 或重新预注册。
3. **时间叠加要算清 (否则会误判"没提前")**: 0945 只是**触发**下界; 实际成交 =
   09:45 arm → +`entry_confirm_delay_sec`(120s) 确认 → 下一次 auto_execute(≤180s) reprice 下单
   ⇒ **首单成交约 09:47-09:50 ET**。核"首单≥09:45"只能看 arm/trigger 时间戳, 看成交时间戳会
   把这道门的 2-5min 延迟误读成黑窗延长。
4. **一个同刻联动 (设计巧合, 非缺陷)**: 量能门的开盘豁免也是 15min
   (`intraday_vol_gate_grace_min: 15`, 判据 `0 <= 分钟 < 15`)。0945 之后, **当天第一笔入场
   落在豁免窗之外**, 必须真过量能门 (1.5)。历史上 09:35-09:44 的入场是"豁免窗内"的,
   所以这次改动不只是"晚 10 分钟", 还**多了一道量能确认**。方向上与用户意图一致 (更保守),
   但解释"今晚首单为什么这么少"时必须把这条并列, 别只归因黑窗。

## 8. R1 可靠性优化（2026-11-12，用户裁「按照建议执行优化」）

### 8.1 已落地

- 新增 `app/playbook/open_blackout.py`：外层 `auto_execute` 与内层
  `check_intraday_trigger` 共用配置解析、严格 `<` 边界和 09:35 非法值兜底。
  内层不再把 `"09:45"` 的 `int()` 失败静默解释成整天放开；开关关闭仍保持内层
  fail-open，外层 09:35 噪声底线不变。
- 黑窗影子每轮必写 `cycle`，新增 `status / n_eligible / n_quote_error /
  n_price_invalid / n_eval_error`；无路由、无候选、全量报价失败与部分失败可区分。
  主上下文无价时补用与生产一致的 `get_quote(last/price)` 兜底。
- `hit` 的进程内去重改为**写盘成功后提交**；append 失败下轮重试。重启或部分写入
  造成的重复由 evaluator 按 `(date, inst_id)` 取最早 `ts`，不再以牺牲首 hit 为代价去重。
- 新增 `scripts/entry_blackout_shadow_eval.py` 并注册 `entry_blackout_shadow`
  （`PERMANENT_MEASURE / eval_on_demand`）：无文件、无 cycle、全量报价失败均非零；
  无 hit 不是错误。输出 DA/MOM/OTHER 分桶和取价质量，明确不计算收益。
- MOM 报价缺失/异常改为暂时性 `MOM_QUOTE_UNAVAILABLE`，下轮复检，不再误写
  `MOM_FEE_SIZE_INFEASIBLE` 并把 direction 永久改成 SKIP。开关关闭和预算快照失败
  也有按指令/日去重的审计及 skipped 计数，仍不终结指令。
- 新影子路径同时登记 `tests/conftest.py` 生产写入缝与
  `tests/playbook/offline_chain_driver.py` 私有改道清单，防止测试污染正式样本。

### 8.2 可复跑历史口径

```bash
PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python \
  scripts/entry_blackout_window_stats.py --from-date 2026-06-01 --json
```

脚本只读 `data/ftseek_us.db`，净盈亏固定为 `pnl_net` 优先、否则
`realized_pnl-fees_est`；只统计已平仓且有净值的 PLAYBOOK 记录，未平仓单列。
2026-11-12 复核得到 09:35–09:45 桶 **403 笔 / 79 个有成交日 / 净 -$2,601.71**，
另有 3 笔未平仓排除。与 §2 原快照 405/78/-$2,648 的差异必须连同查询口径和快照日
一起读，不能混算。日簇描述性均值 -$32.93，正态近似 95% 区间
**[-$79.30, +$13.43]**，跨零；它不是政策收益的因果反事实。

### 8.3 裁决纪律收紧

- 任何非 force 的 09:30–09:45 实际新入场、配置实读不是 0945、黑窗影子整日无 cycle，
  均属**工程验收失败**，当日核查，不等收益样本。
- §5 原“连续 3 日占比明显上升”只保留为复查提示，不再作为模糊自动回滚线。
  chase/出带比例必须报告分子、分母和逐日值；收益/误杀裁决使用日簇区间和预注册样本量，
  不由单晚或三晚方向决定。
- 本政策读数命名为 **“0945 黑窗 + 首笔真实量能确认”组合政策**；不把全部效果归因于
  时间后移。RFC-141 从 0945 后新 arm 重新分桶，不与旧开窗人群混合结算。