# DEB-20260918 · 盘前预检报告内容对齐真实链路 (用户令"按建议动手执行")

> 触发: 用户贴 2026-09-18 盘前综合预检 cron 报告 (job `2d69370d5c91`) 并问
> "最新的链路逻辑和流程，策略，数据等，是否需要结合真实的情况做内容更新，展现最优价值的"。
> 我方逐行核报告内容 vs 真实现场 (脚本产物 json / 生产库 / 代码锚点) 后列出 A/B/C 三档建议,
> 用户回"按建议动手执行" = A+B 全权委托落地 (C 档改读数待签字, 本批未动)。

## 1. 核实结论 (改动依据, 全部实测)

| # | 报告原文 | 真实现场 | 定性 |
|---|---------|---------|------|
| 1 | `盘前 20:45 ET` | cron `45 20 * * 1-5` = **CST** 20:45 = 08:45 ET | 时区错标 12h (prompt 模板写死 20:00 ET 更旧) |
| 2 | `Futu OpenD ✅ / 延迟<1s` | `app/data/futu_lv3_provider.py:491` 的 **硬编码字面量** `f"[FutuLV3] ✅ 连接成功 \| LV3 行情可用 \| US.AAPL=${price:.2f} \| 延迟 < 1s"`, 语义只是"LV3 快照权限探测通过" | 来源真实但**非实测延迟**; 报告未注明语义 |
| 3 | `Capital State updated=20:39 ET` | `capital_state.last_update` 由 `app/risk/capital_safety.py:180` 裸 `datetime.now()` 写入 = **CST 墙钟**; 实值 20:39 CST = 08:39 ET | 时区错标 8h (读者会误判"资金状态停滞") |
| 4 | `策略级 IC (日聚类口径)` | `scripts/daily_ic_analysis.py` 内 **零处** 日聚类计算 (grep `日聚类\|day_cluster\|clustered` = 0), 数字出自 90 天**池化**分组的 `llm_feature_ic`; cron prompt 口径纪律本就禁止池化/单策略读数作头条 | 口径标错 + 违反自身纪律 |
| 5 | `✅ BATTLE_PLAN_FAST_PATH IC=0.3558` / `❌ strategy_unknown` / `✅ technical_fallback IC=0.3896` | `signal_snapshots` 实测最后信号: **06-25 / 06-29 / 08-21** = 已停产管线; 同组 `technical_fallback` `avg_ret_t1=-1.79%` (负) 却打 ✅; 当日活跃主链 `deep_analysis` (N=1950) 不在头条 | 停产标签当"当前信号质量" |
| 6 | `Signal Snapshots 5d_null=100%` (异常项 4) | T+5 结算需 5 交易日而窗宽 = 7 自然日 ⇒ 结构恒 100% (实测近 7 日 142/142); 检查脚本自身注释早写"5d NULL率高是正常的(还没到5天)"; 9~30 日前应结算窗实测 484 条 / null=3 (**1%**) | 结构性假告警 (每天都报) |
| 7 | 缺: 方向构成 / 亏损归因 / 资金帽余量 / 生成 regime | 数据现成: 12 BUY + 2 SELL_SHORT + 1 SKIP(PRICE_TOO_LOW); 昨日 `exit_reason` 档位 tighten −451.58(n=3) / breakeven_lock −296.65(n=2) / OVERNIGHT_GAP_STOP −268.05(n=1) vs SL_TRAIL_LOCK +186.21(n=3) / lock_in +108.43(n=1); capital equity $99,335.69 / peak $99,593.88 / buying_power $198,671 / 水位 green / dd −0.26%; 15/15 条指令生成于 `risk_on` 而同份报告 IC 段称 risk_on 历史 AvgPnL −0.02% | 高价值缺口 (报告内自相矛盾未点出) |

**勘误 (本批自查)**: 初版判定第 2 条为"编造读数", 实施阶段读码后**自我推翻** —
该行确有代码来源 (`futu_lv3_provider.py:491`), 是**硬编码字面量**而非实测延迟。
定性由"编造"降级为"来源真实但语义未注明"。承坑 37/45 (复核方不豁免)。

## 2. 落地内容 (A+B)

**A 档 — 纠错 + 去噪 (测量层, 零交易行为变更)**

- `scripts/data_health_check.py::check_signal_snapshots`: 1d 仍用近 7 日窗; **5d 改 9~30 日前
  "应结算窗"**; 新增 5d>50% 才是 🔴 的判据 (旧实现该分支恒不触发)。
  实测新读数: `近7日 142条 1d_null=37% | 9~30日前应结算窗 484条 5d_null=1%`。
- `scripts/daily_ic_analysis.py`: 新增 `ACTIVE_STRATEGY_DAYS=14` + `_is_retired_strategy()`,
  策略组按最后信号日分流 — 活跃进 `llm_feature_ic` (头条), 停产进新 key
  `retired_strategy_groups` (仅存档), 打印段单列"── 已停产策略组 (不计入当前信号质量) ──"。
  实跑: 移出头条的 4 组 = BATTLE_PLAN_FAST_PATH / unknown / voting_llm / technical_fallback;
  活跃在位 = deep_analysis / universe_expansion。
- cron 提示词 (job `2d69370d5c91`, 经 `hermes cron edit` 落库): 时间标注改
  `08:45 ET (北京 20:45 CST)`; 新增口径纪律 6 条 (只引脚本产物 / LV3 延迟语义 / 停产组不得
  引用 / 5d 新口径 / CST-ET 换算 / 退出策略字段不得裁剪); 输出格式新增方向构成、
  资金与帽余量、亏损归因段, 并要求空头=0 显式标 ⚠️。

**B 档 — 补 4 项高价值读数 (`scripts/pre_market_check.py`, 3 个新检查项)**

- `指令方向构成`: BUY/SELL_SHORT 构成 + 跳过成因 (skip_reason) + 今日生成 regime 分布
  + 同 regime 历史成交 (近 90 日窗, 源 `data/ic_analysis_latest.json`, 标注产物时刻);
  无空头产出时追加 warning (对齐"禁纯单向多头"核心原则)。
- `资金与帽余量`: capital_state 4 数 + 水位 + dd + `last_update` 经 `parse_ledger_ts`(naive→CST)
  →`to_et` 换算并双标 (`CST (= xx ET)`); 帽参数读 `playbook.execution_parameters`
  (日开仓数帽 16 / 单轮新开 3 / 日名义帽 0.8)。
- `昨日亏损归因`: 同 `exit_reason` 档位聚合, 最亏三档 + 最赚两档同屏 + 净额口径标注
  (含"X 行缺净额按毛额兜底"提示)。

## 3. 验证 (Gate 5)

- `py_compile` 三文件通过。
- 实跑 `run_premarket_check()` (**故意不调 `push_notification`**, 免真推告警): overall=PASS,
  13 项全 PASS, 新增 3 项输出与独立 SQL 复算一致 (见 §1 第 7 行数字)。
- `analyze_llm_features()` 直调断言: 3 停产组已移出头条 ✅ / 活跃组在位 ✅ (`ALL OK`)。
- `data_health_check` 三个跑得动的检查函数实跑 (不跑 `main()` 免推送): 新 5d 读数 1%。
- 守卫测试 5 文件: **93 passed / 4 failed**; 4 条红在本批之前的提交
  (worktree `/tmp/wt_base_ic` @ `0a8362040`) 上**逐条相同** ⇒ 既有红, 归属他 agent 在途欠账
  (新 job `96c8e404f55a`/`e31ad3c8a032` 未入台账、`da_prompt_distill_shadow*.py` 陈旧基线行、
  `crontab-cf-pullback-window` 行缺载体脚本名、`/root/.hermes/scripts/` 存在未入库脚本),
  **本批引入 0 条**。
- 副作用声明: 验证跑期间 `logs/ftseek_us.log` 出现 6 条 yfinance/TwelveData ERROR
  (21:01:36~21:01:58), 经时间戳比对**早于本次验证进程启动 (21:03:00)**, 非本次动作产生。

## 4. 回滚

- 代码: `git revert <本提交>` (三脚本 + 本文档 + handoff 行)。
- cron 提示词: `cp /root/.hermes/cron/jobs.json.bak_20260918_preflight_report_update
  /root/.hermes/cron/jobs.json` 或重放
  `hermes cron edit 2d69370d5c91 --prompt "$(cat /root/.hermes/workspace/preflight_prompt_20260918.txt)"`。
  落库后已与 workspace 文本逐字比对一致 (sha256 前 16 位 `5b8b16692a06027c`)。
- 生效时点: 三脚本均为 cron 直接 exec (无进程缓存), 下一交易日 20:45 即生效; 主服务**无需重启**。

## 5. 未做 (C 档, 待用户签字)

把 IC 头条从池化 `ic_t1` 换成**真日聚类**实现 (脚本内新增按日聚类再聚合) — 属"改读数"类改动,
落档须三件 (改的是哪条读数 / 切线日期 / 哪条后验指标能证伪), 且会造成历史 IC 对比断层。
本批**未动**。另: 昨日 3 行 `pnl_net IS NULL` (external_close) 使净额合计对这几笔回退毛额,
属既有口径残留 (P3 登记)。

## 6. 2026-11-11 复核：UE 直注文案与做空执行策略展示

- **触发**: 用户贴 2026-11-11 21:46 CST 盘前预检，指出 `UE=0 (UE 21:10注入)` 过时，并要求做空策略展示正确、不要废话。
- **核实**:
  - RFC-148 已于 2026-10-18 停用 UE 09:10 fresh-event 当日写库；现役 crontab 的 21:10/22:10 仅为 `llm_universe_scan.py scan`，无 `inject` / `da_fresh_event_batch.py --execute`。当日指令实测 `DA=27 / UE=0`。
  - 现役做空执行链为 Playbook `SELL_SHORT` → `Direction.SHORT` → `DualBrokerAdapter` → Futu `TrdSide.SELL`；最近空头成交 `source_type=PLAYBOOK_SIM`。
  - 旧预检读取 legacy `ShortStrategyRouter`。该路由器只含 `stock_short`，但 `run_llm_strategy_scan` 全仓零调用；`options.short_strategy.default_method=options_put` 为死配置。把 legacy 路由器和死配置放在盘前头条会误导现役策略。
- **落地**: `scripts/pre_market_check.py` 改为展示 `做空执行策略: stock_short（正股做空）| Playbook → DualBrokerAdapter | mode=<playbook.execute_mode>`；指令统计改为 `UE=0 (UE 当日直注已停用)`。新增 `tests/test_pre_market_check_report.py` 锁住两处文案与渲染。
- **验证**: `./venv/bin/python -m pytest -q tests/test_pre_market_check_report.py` = **3 passed**；`py_compile` 通过；`git diff --check` 通过；真实配置探针输出 `mode=futu_simulate`。
- **未做**: 未改交易路由、UE cron、`options.short_strategy` 配置或 Hermes prompt；未退役 legacy 路由器本体；未重启主服务；未重跑完整 `pre_market_check.py` 以避免额外推送。
