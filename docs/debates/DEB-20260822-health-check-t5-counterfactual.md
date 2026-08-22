# DEB-20260822 全系统体检执行轮 + T+5 摆动反事实尺子落地

> **状态**: ✅ 已执行 (零实弹行为变更, 一处休眠纯函数正确性修复)
> **日期**: 2026-08-22 (周六, 美股闭市)
> **触发**: 用户令"按照建议执行, 并将你的发现和分析更新到deb文档中" (承接本会话体检报告)
> **角色**: 独立体检+执行会话 (与当日并行会话"T+N 影子字段"工作互不知情、互不重叠, 见 §八)
> **纪律**: 验证期白名单内 = 修尺子×1 (t5_hold_counterfactual) + 攒样本×1 (cron) + 做减法×1 (生成物出库) + 卫生×3 (数据修复/测试/纯函数钉死); **零重启、零配置变更、零机制上线**

---

## §一 体检结论摘要 (独立复核, 非转抄)

北极星读数 (`data/exit_scorecard_latest.json`, 08-22 06:20 生成): 期望值 **-$6.24/笔** (滑点调整 -$18.48), n=227; 杀赢家率 **37.3%** (38/(64+38), 目标≤15%, 较7月基线26%**恶化**); R:R 0.90 vs 盈亏平衡 1.29; 回撤 2.31% ✓ 但窗口含连亏 22 笔 (发生在 8 月初段, 同日齐射相关性下非独立性事件)。OKR 五项 KR 全部未达。费用单位经济 (`data/fee_unit_economics_latest.json`, n=282): 毛 -3.6bps / 费 +11.7bps / 净 **-15.3bps**, **费用占净亏损 76.6%** (三期 -24.8→-19.1→-15.3 改善中); 近三日 (8/19-21) 净 **+$1,116** 转暖。

四核心问题 (详见本会话体检报告, 数字均复核):
1. **周期错配是主病灶且与费用同解** — BUY T+1 反指 vs T+5 正指; 客单 $3.7K 下 12bps 往返费门槛。
2. **迭代强度侵蚀验证期** — OOS 机制变更已第 5/6 次 + 整批零落档先例。
3. **杀赢家率是唯一走错方向的仪表** — TIME_EXIT/MFE_STALL 两桶为最大出血点 (§五实证)。
4. **工程卫生小地雷** — 本轮清掉 3 项 (§三), 新发现结构性缺口 1 项排队 (§四)。

服务健康: systemd active; 今晨 04:32 watchdog 对 llm_strategy_loop stale 1829s 自愈成功 (设计内动作 ✓); 近 36h 重启 5 次, 其中 23:03/23:08 CST 两次落在美股盘中 — **提案 P2-OPS: 盘中 (21:30-05:00 CST) 非必要不重启约定, 待批**。

---

## §二 执行项 F1: BABA `account_type='real'` 根因链闭合 + 数据修复

### 发现 (比体检初判更深的根因)

体检报告原判"标签 bug"。深挖后定性升级为**同标的跨环境仓位碰撞事故 (无资金损失)**:

| 时间 (ET) | 事件 | 证据 |
|---|---|---|
| 8/20 09:48 | 用户面板手动 SHORT BABA 100股 GTC → 券商拒单 ("不支持股票卖空") | manual_order_audit id=73 |
| 8/20 09:36 | 手动 SHORT 50股 @129 LIMIT DAY submitted | manual_order_audit id=74 |
| 8/20 17:39 CST | sync 日志 "Ghost position: US.BABA (DB only, closing)" | logs/ftseek_us.log-20260821.gz |
| 8/21 09:47 | playbook 指令 id=1440 开仓 BABA LONG 47股计划 → 实际成交 23股 @122.435, **PLAYBOOK_SIM/futu_simulate** | trade_records id=1336, futu_order_id=8125011 |
| 8/21 10:29 | **用户面板手动 BUY BABA 50股 MARKET, mode=live_conservative** submitted | manual_order_audit id=77 |
| 之后某刻 | open_positions 行被翻成 owner='manual' / account_type='real' | 修复前快照见下 |

### 关键排除与确认

- **无真实资金事故**: trade_records id=1336 三字段一致 (`source_type='PLAYBOOK_SIM'`, `mode='futu_simulate'`, `trade_mode='simulate'`); capital_state 权益 $97,722 与 sim 账户吻合。
- **playbook 写入路径无辜**: `playbook_service.py:1514 account_type=_trade_mode`, `_trade_mode` 在毕业门禁降级后正确重算 (:955/:959), 不存在 stale `_is_live`。
- **account_type='real' 唯一写入方** = `position_sync.py:897-904` 自管隔离分支 INSERT..ON CONFLICT (硬编码 'real'); 该行 entry_time 保持 21:47:57 说明走的是 ON CONFLICT UPDATE 路径而非删除重建。
- **yaml 自管名单无关**: `risk.self_managed_symbols` 仅 HK.00700。
- **自增强回路**: `get_all_manual_symbols()` = yaml ∪ **DB owner='manual'** (config_container.py:597) → 一旦翻转即永久自证; position_monitor 退出防护 check2 (`_db_owner=='manual'`, :810) 使其粘滞。

### 后果

该 sim 仓被引擎当作用户自管仓 → 退出防护拒绝自动平仓 (protection 语义), trailing/BE/W_WEEK 到期管理全部休眠; 若不修复, 8/28 到期无人平仓, 成为孤儿仓污染后续样本。

### 处置 (已执行)

```sql
-- BEFORE: US.BABA|manual|real|synced|updated_at=2026-08-21T21:47:57 (留痕)
UPDATE open_positions SET owner='auto', account_type='simulate',
       updated_at=datetime('now','localtime') WHERE symbol='US.BABA';
-- AFTER 验证: US.BABA|auto|simulate|synced ✓ (get_all_manual_symbols 60s TTL 过期后 sync 自动采信, 无需重启)
```

**语义说明**: 用户在**实盘账户**手动买的 50 股 BABA 从来不在此 DB 行的表征范围 (position_sync 单 env 查 sim 账户, 实盘持仓对系统不可见, 与 8/21 之前一致)。恢复 auto 只影响 sim 簿记, 双方意图互不侵犯。若用户意图是让系统完全放手 BABA, 请明示, 可再翻回 manual。

### 结构性缺口 → 排队 (终裁后, 建议编号 P1-8)

同一 symbol 的 real-manual 仓与 sim-playbook 仓共用 `open_positions(symbol)` 单主键行:
① Phase 3 源头拦截只查 ManualHoldRegistry 且只在**入场时**生效, 无法防开仓后到达的手动单;
② manual_order_audit 按"当日有审计行"触发翻转 (:797), 不区分审计行的账户环境;
③ 翻转粘滞无退出条件。修复方向 = PK 加 env 维度或同 symbol 互斥硬闸, 属机制类, 排队。

---

## §三 执行项 F2/F3: 卫生三件

### F2 生成物出库 (做减法)

`config/exit_calibration.json` 由 cron `30 6 * * 2-6` 经 `scripts/exit_calibration_refresh.py:200` 全量重写 (grep 全库无任何消费方读它, 纯人读产物), 入库导致树常年脏、淹没真变更。已 `.gitignore` + `git rm --cached` (文件保留磁盘)。顺带登记同类: `data/exit_rule_expectancy_latest.json` (同脚本第二产物, 本就未跟踪)。**回滚 = 删 .gitignore 尾部两行 + git add**。

### F3 order_executor.py 最小测试集 (P1-3 前置子项提前)

新增 `tests/execution/test_order_executor_pure_minimal.py` (13 例, 全过): 覆盖 `_is_option_contract` / `_close_retry_key` / `_is_backoff_error` / `_build_close_metadata_json` / `_derive_entry_reason_for_close`。回归: task34 close-backoff 7/7 过; tests/execution + tests/playbook 合计 **173 passed**。

**抓到真 bug 并处置**:

① **`_is_option_contract` root≥4字符全误判 (已修)** — 旧实现 `"P" in ticker_part[6:10]` 仅当 root≤3 字符时 P/C 才落入切片; `US.AAPL260515C180000` (root=4) 返回 False → `_get_contract_multiplier` 取 1 而非 100, PnL 错百倍。修法 = OCC 结构正则 `[A-Z]{1,6}\d{6}[PC]\d+` (order_executor.py:4203)。
   **爆炸半径分析**: 三消费方 (scheduler_triggers.py:950 / order_executor.py:4442 / dingtalk.py:298) 全是 PnL 计算路径; 全库 trade_records/open_positions/playbook_instructions 期权格式代码 **0 条** → **现网行为零变化**, 属钉死休眠路径的已知错误, 非 8 月新机制。
② **`_build_close_metadata_json` 非 dict meta 半死容错 (不改, 记录)** — 首行 isinstance 容错但 :3834 `meta.get("execution",...)` 直接炸 None; 生产唯一调用方 (:5014) 上游恒传 dict, 无现网风险。冻结期内不动平仓路径, 测试改为钉死现契约并注释, 修复归 P2 卫生。

---

## §四 执行项 A: T+5 摆动变体离线反事实尺子 (本轮核心产出)

### 设计

`scripts/t5_hold_counterfactual.py` (mode=ro): 干净窗口 PLAYBOOK 已平仓交易 × 日K缓存 (`data/replay_bars_daily/`, 缺失标的 Futu QFQ 补拉), 反事实"持有至锚日后第 5 个交易日收盘", 配对比较实际净额 vs CF净额。口径: 锚日=trade_date (权威列); CF 费用沿用实际两腿总费; SHORT 借券敏感性 3%/年×5/252 单列; α 变体扣同窗 SPY 收盘收益 (SPY 缓存独立 `data/t5_cf_spy_bars.json`, 不污染共享目录); 删失计数显式 (missing_bars / window_end), 不静默截断; 逐笔明细落 JSON (id 对齐 trade_records) 供独立复核。cron 06:53 CST Tue-Sat (备份 data/crontab.backup.20260822-t5cf)。

**预注册边界 (内嵌于输出)**: 描述性证据, 供 PIVOT"更长持有期"赛道判读参考; 08-29 终裁判据仍以 direction_counterfactual_ruler 预注册 T+3/T+5 门为准, 本读数**不构成也不替代**任何 GO/PIVOT/STOP 判据。

### 验证

单笔人工复算 LLY#1052 (7/14 入场 1158.31, T+5=7/21 收 1175.41, fee 4.05): Δ=+37.40 与工具逐位一致; 逐笔行 221 条落档可抽查。

### 首跑读数 (since 2026-07-11, 计算 n=221 / 右缘删失 61)

```
实际: 净合计 -2265.74 (-$10.25/笔, WR 38.9%)
T+5 : 净合计 -1001.82 (-$4.53/笔, WR 52.0%) [含SHORT借券调整]
Δ   : +$5.72/笔 (工具自定描述带 ±$10 → 中性偏正)   翻转: 亏→赢 58 / 赢→亏 29
T+5 α-vs-SPY: -$11.29/笔
```

分桶 (by_exit_reason, 按 n 排序):

| 出场原因 | n | 实际/笔 | T+5 CF/笔 | Δ/笔 | L2W/W2L 翻转 |
|---|---|---|---|---|---|
| SL_BREAKEVEN | 54 | -2.19 | -40.39 | **-38.20** | 13/9 |
| SL_TRAIL_LOCK | 50 | +24.70 | +43.79 | +19.09 | 1/14 |
| TIME_EXIT | 31 | -30.37 | +17.41 | **+47.77** | 16/0 |
| MFE_STALL_EXIT | 31 | -43.63 | +8.30 | **+51.93** | 16/0 |
| PRE_CLOSE_EXIT | 26 | +14.62 | +65.19 | +50.57 | 4/4 |
| DEAD_SIGNAL_EXIT | 9 | -24.73 | -192.64 | -167.91 | 2/0 |
| 其余 8 桶 | 20 | — | — | 合计负贡献 | — |

方向切分: LONG n=193 Δ **+$8.69** (CF 仍 -$2.18/笔, α -$6.89); SHORT n=28 ⚠️n<30 Δ **-$14.78** (CF -$20.74)。

### 解读 (描述性, 五条)

1. **周期错配假设在交易级成立但幅度有限**: 无条件持到 T+5 把期望从 -$10.25 抬到 -$4.53 (+$5.72), WR 38.9%→52.0%。方向对了, 但**单靠拿得久到不了正期望**。
2. **α 仍是负的**: 即使 T+5, 相对 SPY 每笔仍 -$11.29 — 改善主要来自"少自我伤害"+beta 漂移, 不是选股 alpha 出现了。与审计"E2 正收益剥 beta 后 100% 来自 SPY"在交易级互相印证。
3. **真正的出血点被精确点名**: TIME_EXIT 与 MFE_STALL 两桶 (合计 n=62) 各 Δ≈+$50/笔且 32 笔零反向翻转 — 这两个"放弃性出场"在系统性砍掉后来会赢的单。杀赢家率 37.3% 的元凶在此, 不在止损族。
4. **保护性出场是真的保护**: SL_BREAKEVEN (54笔) 若不设, T+5 时每笔多亏 $38; DEAD_SIGNAL/SL_INITIAL/PATIENT_STOP 全部负 Δ。"杀赢家"叙事要修正为"**杀耐心**": 止损族无罪, 放弃族有罪。
5. **SHORT 绝不能拉长持有**: n=28 (不下结论但方向明确负), 与 UE SHORT 池候选级 T+5 正读数形成张力 — 候选池≠成交样本, 供给端选择效应待 Track A/B 释疑。

### 对 9 月 PIVOT 设计的含义 (供终裁后参考, 现不动)

证据指向的不是"无条件 T+5", 而是**混合变体**: 保留 BE/SL/trailing 保护 (已验证优势), 对 LONG 侧松绑/移除 TIME_EXIT 与 MFE_STALL 两类放弃性出场, 让赢家跑到周级。这正是"M1 方向-周期对齐"的具体形态, 且与费用解 (降频提名义) 同向。SHORT 维持日级纪律。

---

## §五 局限 (诚实声明)

1. **右缘删失 61 笔** (27.7%): 近两周交易的 T+5 超出 8/21 最后 bar — 删失组恰是近期 regime 样本, 读数对"老交易"权重偏高; 随 cron 每日回填自然缓解。
2. **CF 价格用收盘 print**: 无滑点模型; 但 Δ 为同费配对差, 对称性使滑点假设一阶抵消 (绝对水平仍偏乐观)。
3. **借券 3%/年为上界假设** (SNDR 实测), SEC/TAF 尾差二阶略去。
4. **α 为 CF 单边** (CF−SPY), 未做 actual 窗口配对 α; 结论只用其符号不用其精度。
5. **锚日 trade_date**: 个别裸 datetime CST 行 (P2-7 已登记) 日期可能偏移一天, 占比极小。
6. **窗口 regime 有偏**: 8 月 risk_on 98.2%, LONG Δ+8.69 的外推性受限于单一 regime (与 [R3] 同 caveat)。

---

## §六 待用户裁决 / 知悉清单

| # | 事项 | 性质 |
|---|---|---|
| 1 | P2-OPS 盘中禁重启约定 (21:30-05:00 CST 非必要不重启) | 待批 (运维约定, 非代码) |
| 2 | 9月流程硬化三条: 变更日历(只周六动配置)/单飞原则(一个问题一个在飞实验)/落档当日清零 | 待批 (流程) |
| 3 | BABA 若需保持人工接管请明示 (当前已恢复 auto/simulate, 引擎按 W_WEEK 管理, ~8/28 到期) | 知悉 |
| 4 | P1-8 同标的跨环境仓位碰撞 (PK 加 env 或互斥硬闸) | 排队终裁后 |
| 5 | `_build_close_metadata_json` 半死容错分支修复 | 排队 P2 卫生 |
| 6 | 费用侧 P0-2 九月重提证据已足 (fee 占净亏 76.6%; Q1 22.6bps vs Q5 9.3bps 纯算术) | 知悉 |

---

## §七 回滚键汇总

| 动作 | 回滚 |
|---|---|
| t5_hold_counterfactual.py + cron 06:53 | 删脚本 + 删 cron 行 + 删 data/t5_hold_cf_*.{json,jsonl} + data/t5_cf_spy_bars.json |
| exit_calibration.json 出库 | 删 .gitignore 尾两行 + git add config/exit_calibration.json |
| BABA 行修复 | `UPDATE open_positions SET owner='manual', account_type='real' WHERE symbol='US.BABA'` (不建议, 除非用户明示接管) |
| _is_option_contract 修复 | revert order_executor.py:4203 hunk (现网零消费期权代码, 零行为影响) |
| 测试文件 | 删 tests/execution/test_order_executor_pure_minimal.py |

---

## §八 与并行会话的边界声明

工作树中发现另一会话当日进行中的未提交改动 (`app/playbook/db.py` +14 / `app/playbook/ic_tracker.py` +117, 注释标"2026-08-22 用户裁'落 T+N 影子字段'"): 指令级 fwd_return_t{N}_adj 影子列。与本轮交易级 T+5 反事实**互补不重叠** (指令级 vs trade级; 机制盯市 vs 无条件持有), 本轮未触碰、未提交上述文件; handoff.md 中随本批入库的 8/21 Qoder 追溯条目系该会话前已完成的自包含文档 (8863100c 之后续), 一并落地避免遗失。
