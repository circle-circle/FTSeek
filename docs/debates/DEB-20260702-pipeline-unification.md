# DEB-20260702 执行管线统一：main vs playbook_sim 差异与方案

## 背景

FTSeek-US 有两条独立执行路径写入 trade_records：
- **MAIN_PIPELINE** (`owner_strategy=main/auto_main`)：signal_pipeline → order_executor._submit_order → Futu 下单
- **PLAYBOOK_SIM** (`owner_strategy=playbook_sim`)：playbook pipeline → playbook_service._execute_via_dual_broker → Futu 模拟下单

6/28 加了 `_skip_execution_in_trading=True` 让 scan_loop 在盘中不下单（playbook 独占），但盘前信号路径（PRE_MARKET）仍可绕过此检查下单。6/30 有3笔 MAIN_PIPELINE 新开仓（ARM/TSLA/IWM SHORT）。

## 目标

冻结观察期需要：**干净、统一、明确、有效的交易链路**。明确以谁为准，消除冲突。

## 数据与实证

### 两条路径的核心差异

| 维度 | MAIN_PIPELINE | PLAYBOOK_SIM |
|------|--------------|--------------|
| 代码入口 | `signal_pipeline.py:596 _execute_portfolio_plan` | `pipeline.py:1759 run_playbook_auto_execute` |
| 下单函数 | `order_executor._submit_order` | `playbook_service._execute_via_dual_broker` |
| source_type | `MAIN_PIPELINE` | `PLAYBOOK_SIM` |
| owner_strategy | `main` 或 `auto_main` | `playbook_sim` |
| 退出监控 | `position_exit_monitor.py` (ExitRuleDispatcher) | `pipeline.py:monitor_playbook_sim_exits` |
| 退出规则来源 | `exit_policy.yaml` (SSOT) | `_check_playbook_exit_rules` (硬编码 if/elif) |
| IC 闭环 | 无完整链路 | instruction→trigger→execute→IC |
| trade_mode | simulate | simulate |
| 当前状态 | 盘中已隔离(_skip_execution)，盘前仍可下单 | 活跃，盘中每30分钟检查triggered指令 |

### 6/24 冲突实证（已清理）
- US.NFLX LONG: playbook_sim TIME_EXIT(-1.15%) vs auto_main TIME_EXIT(-0.62%) — 同decision_id，两条路径各写一条trade_record
- US.PG SHORT: playbook_sim PRE_CLOSE_EXIT(-0.72%) vs main BREAKEVEN_STOP(-0.44%) — 同decision_id，不同退出原因

### 6/28后的状态
- 6/29: 4笔交易全部 playbook_sim ✅
- 6/30: 3笔 main 新开仓（ARM/TSLA/IWM，decision_id来自6/29盘前信号）+ 2笔 playbook_sim
- 6/28隔离开关只覆盖 scan_loop 盘中路径，不覆盖盘前信号执行路径

### 两条退出系统
1. **ExitMonitor** (`position_exit_monitor.py`): 读 `exit_policy.yaml`，控制 MAIN_PIPELINE 仓位
2. **_check_playbook_exit_rules** (`pipeline.py:1558`): 硬编码 if/elif，控制 PLAYBOOK_SIM 仓位
- 同一仓位可能被双重监控（如 BIDU 6/30 被 ExitMonitor 的 PRE_CLOSE_EXIT 和 playbook 的 EOD_FORCE_CLOSE 同时触发）

## 可选方案

### 方案A: Playbook 独占（当前方向，需补漏）
- 在 `_submit_order` 入口加硬开关：非 playbook 路径一律拒绝下单
- 退出统一到 `_check_playbook_exit_rules`
- ExitMonitor 仅保留给历史 main 仓位平仓后退出
- 优点：IC 闭环完整，决策链可追溯
- 风险：playbook 的过滤层可能导致产量不足

### 方案B: Main 独占（回退到主管线）
- 禁用 playbook auto_execute
- 所有信号走 scan_loop → _submit_order → MAIN_PIPELINE
- 退出统一到 ExitMonitor + exit_policy.yaml
- 优点：退出规则 SSOT（yaml配置），产量更稳定
- 风险：IC 闭环不完整，丢失 instruction 决策链

### 方案C: 分工协作（当前架构改进版）
- 盘前信号 → playbook_sim 执行（保留 IC 链路）
- 盘中信号 → 禁用（不再有 main 路径新开仓）
- 退出：各仓位按 owner_strategy 走对应退出路径
- 增加：_submit_order 入口硬开关阻止 main 路径新开仓
- 优点：兼顾 IC 链路和退出 SSOT
- 风险：两套退出系统并行维护

## 讨论记录

### 第 1 轮：hermes

**立场：支持方案A（Playbook独占），但需补硬开关防漏。**

理由：

1. **IC闭环是核心差异**：playbook 路径有完整决策链（screener→voting→instruction→trigger→execute→IC backfill），main 路径没有 instruction 层，IC 归因断裂。冻结观察期的目标是采集 IC 数据，应该用 IC 链路完整的路径。

2. **6/28的方向已正确**：`_skip_execution_in_trading=True` 让 scan_loop 盘中不下单，playbook 独占执行。问题是盘前路径仍可绕过——这是补漏问题，不是方向问题。

3. **退出冲突是真实风险**：BIDU 6/30 被 ExitMonitor(PRE_CLOSE_EXIT) 和 playbook(EOD_FORCE_CLOSE) 双重触发。虽然各管各的仓位，但同一 symbol 的 open_positions 表 PK 是 symbol+direction——两条退出路径竞争同一行记录，metadata_json 互相覆盖。

4. **做减法原则**：方案A只需在 `_submit_order` 加1行硬开关（`if source_type != 'PLAYBOOK_SIM': return`），比方案C维护两套退出系统简单。

5. **产量风险可控**：6/29 playbook_sim 产出4笔，6/30 产出2笔。日均2-4笔在20天采集期内可积累40-80笔，够IC统计。

**执行项**：
- `[P0]` `_submit_order` 入口加硬开关：source_type != PLAYBOOK_SIM → 拒绝下单（仅允许平仓）
- `[P0]` 确认 `_skip_execution_in_trading` 覆盖盘前路径，或在 _submit_order 层面统一拦截
- `[P1]` 历史 main 仓位（IWM/UNH pending close）由 ExitMonitor 完成平仓后，ExitMonitor 转为 observe-only
- `[P1]` Dashboard SQL 的 `source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING')` 已正确包含 PLAYBOOK_SIM，无需改

### 第 1 轮：claude-code

验证5项：1✅_submit_order无source_type守卫(:3062仅ACB/VIX,:984硬编码MAIN_PIPELINE)2✅_skip_execution仅TRADING置位(:186),PRE_MARKET可绕过3✅两套退出:exit_monitor:220读yaml vs pipeline:1940硬编码;ExitMonitor:769仅滤ORB/manual,不滤PLAYBOOK_SIM→双监真实4❌PK非symbol+direction!trade_store:228为`symbol TEXT PRIMARY KEY`单列,Hermes事实错误,冲突更严重5✅IC链完整:pipeline:427→1072→1193

立场:支持C反对A。Hermes"只加1行"低估复杂度——退出统一到硬编码=弃yaml SSOT逆架构。PK事实错误暴露其schema理解不准,硬开关基于错误假设会漏判。C保留yaml SSOT优势,靠owner_strategy路由隔离(ExitMonitor:769已有过滤框架),风险更可控。

### 第 2 轮：hermes

**接受CRITIC的PK修正：symbol单列PK，非symbol+direction。** 这确实比我的描述更严重——同symbol不能同时持有多空仓位，一条路径开仓会直接覆盖另一条。

**但维持方案A立场，理由如下：**

1. **硬开关已执行且生效**：_submit_order入口已加source_type检查，非PLAYBOOK_SIM/LIVE/ORB_ENGINE→return None。这不是"只加1行"——是链路统一的根基。CRITIC验证了5项中4项✅，唯一错误是PK描述，不影响硬开关逻辑（硬开关在下单入口，不依赖PK结构）。

2. **方案C的"两套退出系统并行"才是逆架构**：CRITIC说"保留yaml SSOT优势"——但_exit_check_playbook_rules是硬编码if/elif，不读yaml。让ExitMonitor读yaml+playbook硬编码同时跑=维护2×故障面2×。冻结期目标是"干净统一"，不是"两个系统并行"。

3. **退出统一方向**：playbook_sim退出（monitor_playbook_sim_exits+SL_ATR_STOP+PRE_CLOSE+EOD）是当前活跃路径，数据已证明它有效。ExitMonitor仅给历史main仓平仓后退出。这符合做减法：一条路径活跃，另一条自然消亡。

4. **yaml SSOT不是当前现实**：exit_policy.yaml控制ExitMonitor，但PLAYBOOK_SIM不读它。方案C要统一到yaml需要改_check_playbook_exit_rules读yaml=新代码=冻结期不宜。

**收敛**：P0硬开关已执行。退出维持各管各的（owner_strategy路由），main仓平完后ExitMonitor自然退役。
