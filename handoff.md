# FTSeek-US · HANDOFF

> **这是什么**: DA-FTSeek 链路迭代的**活文档**。每次调整/计划都追加到这里, 避免跨会话/跨 Agent 从头开始。
> **怎么用**: 每次改动后更新"变更日志"(最新在最上) + "下一步"。其他 Agent 先读本文件再动手。
> **最后更新**: 2026-07-20 · 环境: 富途**模拟盘** (v2 sim, 无真实资金) · venv: `./venv/bin/python`

---

## 🎯 当前北极星 (换过 KPI)
- **只追"每笔期望值 ≥ $0"**, 不追胜率/周收益数字。收益是期望转正后的自然结果, 直接追会逼出"加仓凑收益"的自杀行为。
- 活跃 OKR: **`docs/OKR-2026-08.md`** (2026-07-14→08-31)。上一轮 `docs/OKR-2026-07.md` 已结算 **MISSED**。
- 重心在 **FTSeek 退出侧**, 不在 DA 预测侧 (见下"核心诊断")。

## 🔬 核心诊断 (数据实证, 别再推翻重来)
用 `scripts/exit_scorecard.py` 实测 (污染窗口 n=61/71):
1. **信号 ≈ 掷硬币偏正, 不是问题**: 持到收盘命中 50.8%, 均值 +0.37%/笔 → DA 有薄弱正 edge, 达到">50%"的诉求。
2. **漏血在退出**: 退出把 8 笔本该收盘为正的赢家做成亏损, 只救回 2 笔 → 胜率 50.8%→41%。
3. **R:R 结构性不足**: avg赢 $118 ≈ avg亏 $110, R:R=1.07 < 盈亏平衡 1.29 → 每笔期望 **−$10.57**。
4. **confidence 反向**: 低信心桶反而更好 (Playbook IC 为负) → 按 confidence 加仓 = 给更差信号加钱。**已拍平** (见变更日志)。

一句话: **不需要更准的预测; 需要退出别砍赢家 + 把 R:R 拉过 1.29。**

## 🧭 每周迭代循环 (每周五盘后)
```
1. 跑 scripts/exit_scorecard.py            → 期望值 / 杀赢家数 / R:R 缺口
2. 只问: 赢家还在被砍吗? R:R 离 1.29 还差多少?
3. 只改一个退出参数 (其余全冻结)
4. 下周同一张表对比: 变好→保留, 变差→回滚
铁律: 一次一个变量、一个指标、可回滚。禁止在期望转正前加仓/放大 R:R 追收益。
```

## 🛠️ 工具
| 工具 | 用途 |
|---|---|
| `python scripts/exit_scorecard.py [--since 2026-07-11] [--days N]` | 退出纪律周度判分 (只读)。落 `data/exit_scorecard_latest.json`+history |
| `python scripts/okr_status.py` | OKR 总览 (胜率/PnL/IC/因子/void率趋势)。落 `data/okr_latest.json` |
| `python scripts/trigger_gate_report.py` | 触发门判分 (只读): executed vs missed 的 fwd_return 缺口/方向分层/机会成本。落 `data/trigger_gate_latest.json`+history |

## 🗺️ 退出旋钮地图 (`config/playbook_horizon.yaml`, DA 恒走 `short_horizon`)
**A. 赢家被锁太小 (R:R 缺口主因) — `factor_exit_rules.default`**
- `drawdown_from_peak_pct` = **0.8** (L49) ← 07-19 从 0.4 回归到 0.8 (handoff 处方)。给赢家 0.8% 呼吸空间, 不再 0.4% 就锁。
- `breakeven_at_pct` = 0.5 (L48) ← 不动, 仍早保本 (亏不了)。

**B. 赢家被噪声扫损 (杀赢家) — `short_horizon.vol_buckets`**
- low `sl_floor_pct`=2.0 (L370), medium=2.5 (L378) ← 多数标的落此, 实际止损≈2-2.5%
- ⚠️ 07-09 基于**日线回测**收紧过(“2-3%最优”); 与 live scorecard 冲突时**信 scorecard**。

**C. 已核实**: `short_horizon.rr_ratio=999` (L353) → 固定止盈**关闭**, 靠 trailing+EOD。所以封顶赢家的是 A 的 drawdown 锁, **不是** rr_ratio。

**D. 早期紧SL (方向全错快速脱身) — `unified_config.yaml::playbook.execution_parameters`**
- `early_tight_sl_enabled` = **false** ← 07-20 修复M 从 true 降级为 **shadow-only** (积累期实单退出参数冻结)。
- 逻辑保留供口径对齐: 入场后30min内 breakeven未激活 → SL收紧到1%. 但**不再实单化**, 改由 `exit_shadow.EARLY_TIGHT_VARIANTS` (`early_tight_30m_1pct`) record-only 采集反事实 `counterfactual_saved_pct` (正=紧SL更优)。
- **为什么降级**: 见变更日志 07-20 修复M — 积累期每改一次实单退出参数就断一次 config_version 窗口, 污染毕业(胜率/PnL)判定。新退出规则一律先走 shadow, 2 周数据选出赢家再一次性切换。
- 回滚/实单化: `early_tight_sl_enabled` 改 true (哈希变化会自然开新 config_version 窗口)。

## ➡️ 下一步 (下周五第一个动作)
**跑 `scripts/exit_scorecard.py --since 2026-07-21`** → 对比本次修复前后:
- R:R 是否从 1.63 提升 (drawdown 0.4→0.8 释放赢家)
- 早期紧SL 现为 shadow-only: 查 `data/shadow/exit_param_shadow.jsonl` 的 `early_tight` 反事实 (`counterfactual_saved_pct` 均值>0 且稳定才评估实单化)
- avg_win 是否从 $27.59 提升 (SL_TRAIL_LOCK 不再 0.4% 就锁)
- 若 R:R 仍<1.5: 动 B (`sl_floor` low/medium +0.5%)。**别 A/B 同时动。**

---

## 📝 变更日志 (最新在上)

### 2026-07-20 (四批) · 退出策略全链路审计修复 — 2P0 + 2P1

**背景**: 退出策略配置层→分桶匹配→动态SL→退出优先级链→影子评估 全链路交叉比对。

**P0-1 — 7/19 dd=0.8 处方被因子分桶架空** (`config/playbook_horizon.yaml`)
- 通俗: 7/19 把 default 的锁利回撤从 0.4 放宽到 0.8（释放赢家），但 15 个趋势类因子规则仍是 0.5 → 绝大多数带因子标签的单子实际还在用 0.5，处方没喂到病人嘴里。下周五 exit_scorecard 会得出"放宽没效果"的假阴性。
- 修复: 全部 15 个 `trailing_enabled: true` 因子的 `drawdown_from_peak_pct` 0.5→0.8 对齐 default。`trailing=false`（快速证伪类）不动（无 dd 字段，走 profit_override 语义）。
- 回滚: 改回 0.5。

**P0-2 — SHORT 配上多头退出规则（方向分桶漏洞）** (`app/playbook/pipeline.py` `_get_factor_exit_config`)
- 通俗: SHORT 指令的 conditions 若全是 bullish 因子（如逆向做空过热股），第一轮方向匹配空手后，第二轮原样取第一个有规则的 condition → SHORT 拿到 `trailing=true, max_hold=3天` 的多头规则。与 7/7 fix 目标直接矛盾。
- 修复: 第二轮回退增加方向过滤 — 跳过方向相反的 condition，全部跳过则落 default（max_hold=1天，对 SHORT 合理）。direction=None 兼容旧行为。
- 冒烟: 5 场景全过（SHORT+bullish→default / LONG+bullish→因子 / SHORT+bearish→空头规则 / LONG+bearish→default / None→兼容）。

**P1-1 — 影子 baseline 漂移 + fee_cushion 缺失** (`app/playbook/exit_shadow.py`)
- baseline_be1.0_dd0.5 注释称"对照=现行default"，但现行 default 已是 be0.5/dd0.8 → 对照组失真。
- 修复: ① 新增 `baseline_be0.5_dd0.8` 为正确对照，旧 baseline 标记 LEGACY 保留（不破坏 in-progress 数据键名）；② `_variant_effective_sl` 补 `fee_cushion_pct` 参数，保本线数学对齐实盘 pipeline L3961。
- 冒烟: LONG/SHORT fee_cushion 方向正确 + dd 主导时不干扰 + 新旧 baseline 共存。

**P1-2 — rr_ratio=999 与 vol_bucket TP 冲突（注释澄清）** (`config/playbook_horizon.yaml`)
- 999 哨兵（"去TP"决策）实际从未生效：instruction_generator L399 vol_bucket 路径覆写 rr_ratio=tp_multiplier(1.5~2.0)。配置声明与真实行为相反。
- 处理: 加注释澄清真实行为（每单有TP, RR 1.5-2.0），待数据裁决去留。不改代码。

**已生效: 2026-07-20 重启 `ftseek-us.service` (四批)。**

### 2026-07-20 (三批) · 实盘就绪 P0 预备 — B4 数据新鲜度门 + B3 交易环境横幅

**背景**: 富途平台架构深查后确定的实盘前 P0 清单中, 先落地模拟盘可验证的两项。

**B4 — 开仓数据新鲜度门 (降级/延迟行情禁开新仓)** (`app/risk/data_health_guard.py` + `playbook_service.py` + `playbook/config.py` + `futu_lv3_provider.py`)
- 通俗: 行情降级到 Finnhub 时报价延迟 15 分钟, 系统之前会照常按旧价开仓; 现在降级期间/报价过期 (>90s) 一律禁止开新仓, 平仓不受影响。
- 专业: 三处配合 — ① 根因: provider L850 曾把 fallback 报价 `timestamp` 覆写为抓取时间 (把 15min 延迟"洗白", 摧毁 07-19 finnhub C3 修复), 改为仅缺失时兜底; ② 新增 `check_quote_freshness()` (不依赖 bid/ask, 补 `check_quote_health` 盲区: 降级源恰好无盘口 → 整体绕过 health 门), 降级源集合 `DEGRADED_QUOTE_SOURCES` + 时间戳年龄双判定, 兼容 Unix float / ISO 字符串; ③ 接线 playbook 开仓汇合点 (health 门之后), 配置 `entry_freshness_gate_enabled: True` / `entry_max_quote_age_sec: 90.0`, 取价失败维持 fail-open 与既有语义一致。
- 冒烟: 7 场景全过 (finnhub 拦/fallback+兜底盘口拦[health门盲区实证]/futu过期拦/ISO过期拦/新鲜放行/无时间戳放行/配置键)。
- 回滚: `entry_freshness_gate_enabled: False` (一键) 或删 playbook_service 新鲜度门块。

**B3 — 启动交易环境横幅** (`app/core/scheduler_core.py` start 处)
- 启动时醒目打印 AUTO_TRADE_ACCOUNT / ALLOW_REAL_TRADING / execute_mode / acb·preflight_fail_mode; real 模式用 WARNING 级 + "实盘要求均为 closed" 提示。纯可见性, 硬拦截仍在 di_container LIVE 启动断言 (已存在, L158-179: real + fail-open → 拒绝启动)。
- 排查确认: 原计划的 "precheck_fail_mode 切换预案" 大部分已存在 — di_container 已有 LIVE 启动断言, 本批只补横幅可见性。

**已生效: 2026-07-20 重启 `ftseek-us.service` (三批)。**

### 2026-07-20 (二批) · 开盘前全链路隐藏 bug 排查 — 4 处实锤修复

**背景**: 由 `_track_bar_peak_trough` SHORT 方向语义 bug 触发的同类问题全链路深查 (方向语义/数据源语义/键名契约/截断行为), 目标: 消除开盘交易的不利因素。

**B1 — VWAP 硬门数据源语义错配 (P0 行为级)** (`app/data/market_data_router.py` + `pipeline.py` VWAP门)
- 通俗: 实盘启用的 VWAP 方向门, 一直在拿"60天平均价"当"今天的 VWAP"用 — 近期上涨的股票做空恒被拦, 下跌的股票做多恒被拦, 方向性系统误拦。
- 专业: `ctx["vwap"]` 主路径来自 `calculate_from_klines(daily_klines)` (60根日K 全量 cumsum), 07-16 修复的"当日累计 VWAP"假设只在日K不足的 1M fallback 路径成立。修复: router 用 1min K 过滤今日 bar 单独算 cum(tp×vol)/cum(vol), 新增 `ts_ctx["vwap_intraday"]` (门专用, 0=今日bar<5 视缺失) 并覆盖 `ts_ctx["vwap"]`; 门侧优先读 `vwap_intraday`, 缺失 fail-open。
- 回滚: 删 router 当日VWAP块 + 门侧改回 `ctx.get("vwap")`。

**B2 — get_kline HTTP 冷路径旧数据截断 (P0 数据完整性)** (router `count 200→800`, pipeline 两处 `240→800`)
- 通俗: 冷缓存时问券商要 K 线, 要少了会拿到"昨天最旧的一段"而不是"最新的", 导致今天的数据全为空。
- 专业: get_kline HTTP 路径 `start=T-1` 范围含 ~780 根 1min bar, OpenD 对 `max_count<范围总数` 返回**最旧N根** (provider 06-30 注释实证)。count=200/240 时冷路径拿纯 T-1 bar → 今日过滤恒空: 盘中量能门恒 fail-open、#3 bar 峰谷采集失效、`slope_5m` 算的是昨天中午的斜率。修复: 三处 count → 800 (覆盖 T-1+今日全时段, 仍单次请求不多耗配额)。已知残留: push 缓存仅存 300 根, 15:30 后 push 路径今日累计量略低估 (触发窗 14:00 截止, 影响可忽略)。

**B3 — ORB 开盘区间键名契约 (P1)** (router ORB 段)
- 专业: HTTP 路径 bar 键是 `time`, push 路径是 `time_key`; ORB 段只认 `time_key` → HTTP 冷路径下 `opening_high/low` 永不注入 → ORB 策略 `opening_range_ready` 恒 False。修复: `k.get("time_key") or k.get("time")`。

**B4 — 归因 peak/trough SHORT 塞错桶 (P1 归因失真)** (`app/analysis/trade_attribution.py`)
- 通俗: 与 bar 峰谷同款方向 bug — 做空单的"最有利价(最低)"被塞进高价桶、"最不利价(最高)"被塞进低价桶, 被 max/min 直接忽略。
- 专业: SHORT 时 `metadata.peak_price`=持仓期最低价/`trough_price`=最高价, 原实现不分方向 peak→all_highs/trough→all_lows → SHORT 的 D2入场/D3出场/D5锁利维度退化回 entry/exit 近似。修复: 按 direction 分桶。冒烟: SHORT 100→97 (peak95/trough102) 的 best_exit 修复前 97 (假"精准") → 修复后 95。
- 同批新增 (用户手写): `_calc_intraday_vol_ratio` 盘中实时量能比替代原日级 volume_ratio (审查通过, 契约/时区/fail-open 均正确; 已知偏差: elapsed_fraction 线性假设 vs 成交量 U 型分布 → 早盘门偏松/午后偏严, 14:00 截止后无影响)。
- 排查确认无问题: 触发链路方向语义 (漂移作废/SL穿越/VWAP侧向/miss分类)、MFE/MAE SHORT 反算、`_is_instruction_expired` timezone、entry_time 时区 (带-04:00 与 futu ET wall time 对齐)、volume/time 字段契约、#6 _tr/_meta 收敛。
- **已生效: 2026-07-20 14:16 CST 重启 `ftseek-us.service` 完成** (启动日志验证无导入错误, 对账/订阅/Watchdog 均正常; 代码已验证: py_compile 3 文件 + 量能比/归因双向冒烟全通过)。

### 2026-07-20 · 参数冻结下的结构性优化批次 (#1/#2/#3/#4/#6/#7, 全部不碰实单退出参数)

**背景**: 积累期参数冻结 (避免 config_version 断点), 本批只做归因补齐/测量精度/灾难兜底/可观测性, 零行为变更类改进一律 record-only。

**#1 — missed 指令 skip_reason 归因黑洞补齐** (`app/playbook/shadow_tracker.py`)
- 通俗: 94 条"没做成的交易"不知道为什么没做成, 现在每条都有说法。
- 专业: shadow_tracker 两个写 `outcome='missed'` 的分支不写 skip_reason → 代码修复 (仅原 reason 为空时补标, 不覆盖 auto_execute 具体拒因) + DB 回填 94 条 (81 no_trigger + 13 trigger_no_execution)。剩余空归因 = 0。

**#2 — 触发门反向选择周度判分器** (新建 `scripts/trigger_gate_report.py`, 只读)
- 通俗: "没追到的车是不是比追到的更好"固化成周度报表。
- 首跑结论: missed 组 fwd_return +0.915% vs executed +0.249% (缺口 0.666pp), **集中在 BUY 侧** (+1.152pp), SHORT 侧健康 (−0.219pp); 机会成本累计 $12,819。内置判读: 缺口>0 且双向同号稳健 且持续≥2周 → 下一个 shadow 变体应为"触发条件放宽" (目前双向不同号, 继续观察)。

**#3 — peak/trough 1min bar 并行采集 (record-only)** (`app/playbook/pipeline.py::_track_bar_peak_trough`)
- 通俗: 峰谷价以前靠"每轮瑞一眼报价", 两眼之间的最高/最低点全丢 (MFE 系统性低估约一半)。现在同时用 1 分钟 K 线高低点并行测一份。
- 专业: futu LV3 `get_kline(K_1M)` (300s 缓存, 基本不耗配额) 取入场后 bar 的 max(high)/min(low), 单调更新 `metadata.peak_price_bar/trough_price_bar`, 并进 `exit_telemetry`。**不参与任何退出决策**; 周度对比 bar 版 vs quote 版差距后再评估切换 (切换=行为变更, 需开新 config_version 窗口另行批准)。
- **同日人工审阅修复 (方向语义)**: 初版不分方向一律 peak=max(highs)/trough=min(lows), SHORT 时与 quote 版既有语义 (peak=最有利价, SHORT 取最低; trough=最不利价, SHORT 取最高) 完全反转 → SHORT 的 bar 版 MFE 会符号反转, 周度对比产生"bar 版更差"假象。修复: helper 加 `direction` 参数 (默认 LONG 向后兼容), SHORT 分支 peak=min(lows)/trough=max(highs)。双向冒烟通过; 周日修复, 盘中未跑过, 无脏数据需清洗。
- 回滚: 删 monitor 循环里 `_bar_track_new` 一行 + helper。

**#4 — grace 5min 不再完全豁免, 保留 4% 灾难兜底** (`pipeline.py::monitor_playbook_sim_exits`)
- 通俗: 入场后 5 分钟内以前是"完全不看盘", 闪崩 10% 也不管; 现在闪崩超 4% 照样斩。
- 专业: grace 期 `continue` 前先查是否击穿 `cooldown_sl_pct` (4%) 兜底线, 与规则 0 "冷却期放宽而非豁免"语义对齐。非参数变更 (4% 是既有冷却参数), 属灾难兜底补洞。

**#6 — `_close_trade_record` 读写收敛 (行为零改变)** (`pipeline.py`)
- 通俗: 平仓时对同一笔交易反复查数据库 10 次、写 3 次, 现在查 1 次、写 2 次, 内容一模一样。
- 专业: 函数开头读一次行快照 `_tr` + 解析一份 `_meta`, 各块 (MFE反算/exit_shadow/holding_hours/pnl_pct兜底/telemetry/ghost_cause/归因/推送) 顺序加工同一 dict; telemetry+ghost 合并为主 UPDATE 前一次写回, 归因写回保留 (commit 时点不变)。消除多次读改写的互相覆盖风险面。
- 验证: 临时 DB 全链路冒烟 (MFE 反算 5%/MAE 2%/telemetry 含 bar 峰谷/instruction 回写 win+MFE美元/open_positions 删除/归因落盘) 全部通过。

**#7 — void 率趋势接入 okr_status** (`scripts/okr_status.py` 新增 `void_trend` 段)
- 口径: void/(win+loss+void) 周度分组, ≥10% 打 ⚠️。首跑: W25 25.0% / W26 14.3% / W27 25.9% / **W28 2.9%** — 幽灵仓根因修复生效的趋势证据; 若回升 = 执行/对账链路又在产幽灵仓。

**未做 (有意为之)**: #5 broker 侧止损底线单 — 切 LIVE 前头号阻断项, 保持在毕业 checklist, SIM 阶段不做。

### 2026-07-20 · DEB-20260720 盲区审计修复批次: 保护毕业数据质量 + 零成本防真金 (修复 M/5/7/6a/3b)

**辩论锚点**: `docs/debates/DEB-20260720-blindspot-risk-audit.md` (三轮收敛)。当下 SIM 阶段做 5 项 (M+5+7+6a+3b), 全部保护毕业判定数据质量或零成本防真金; 修复 1/6b 为切 LIVE 头号阻断 (本批不做); 4/3a/2 延后。

**关键洞察 (元风险拆两半)**: 毕业判定分两个维度, 对退出参数污染的敏感度不同 —
- **IC 侧 (`IC(T+3)>0.05`) 天然免疫**: IC 基于 forward return = 入场价→N日后日线价, 是**入场锚定、退出无关**的量。改 time_exit/drawdown 不动 forward return。
- **PnL/胜率侧 (cap_reduction 45%胜率+正PnL) 受污染**: 已实现 PnL = 入场 edge × 退出执行。退出参数一变, 同一批入场信号的胜率就变。
→ 最优解不是"停止探索", 而是**打标 + 按版本取样 + 探索走 shadow** (修复M)。

**修复 M (P0) — 退出参数 config_version 打标 + 毕业统计按版本窗口 + 探索走 shadow**
- 通俗解释: 以前"边积累数据边调退出参数"就像"边煮饭边换锅", 最后不知道哪锅饭是哪个参数煮的, 毕业判定的胜率被多套参数搅浑。现在给每笔平仓记录盖一个"退出参数版本印章", 毕业统计只数**当前印章**的样本, 参数探索全部放到"影子"里跑 (不动真单)。
- 专业解释: (a) `PlaybookConfig.get_exit_config_version()` 对会改变已实现 PnL 的退出参数 (factor_exit_rules / vol_buckets / cooldown / early_tight / fee_cushion) 算稳定短哈希; (b) `_close_trade_record` 平仓时把版本写进 `metadata_json.config_version`; (c) `scripts/okr_status.py` 新增 `graduation_sample` 段, 用 `json_extract` 只取当前版本窗口的已平仓样本算胜率/PnL; (d) 参数探索继续走 `exit_shadow` (record-only)。
- 收益: 既保护毕业判定不被多套参数污染 (PnL 侧可比), 又不牺牲探索速度 (shadow 并行)。复用现有 metadata_json + shadow 框架, 零新基建 (未加 DB 列)。
- **联动: 修复 B (早期紧SL) 收编为 shadow-only** — `early_tight_sl_enabled: true→false`, 改由 `exit_shadow.EARLY_TIGHT_VARIANTS` (`early_tight_30m_1pct`) 采集反事实 `counterfactual_saved_pct`。避免又引入一个 config_version 断点。
- 回滚: 打标不可逆但无害 (只多一个 metadata 字段); 窗口化统计改回全样本即删 `graduation_sample` 段; early_tight 实单化改回 `enabled: true`。

**修复 5 (P1) — reconcile 幂等失效**
- 通俗解释: 对账用的"上次状态存档"文件有时会被清空成 2 字节 `{}`, 导致每次重启都以为"全是新持仓"重复告警/重复对账。根因是写文件时如果对账中途异常, 会用空存档覆盖好存档, 且异常被 debug 日志吞了看不见。
- 专业解释 (`app/positions/reconciler.py`): `_save_signatures` 增加防覆盖 — 本轮签名为空但历史非空时跳过写入 (保留好状态); 异常从 `logger.debug` 升级 `logger.warning`; `_load_signatures` 对"文件存在但空/非法"显式 WARN (幂等失效可见)。
- 收益: 消除每次重启的重复告警/重复对账, 对账幂等恢复。回滚: 移除防覆盖分支。

**修复 7 (P1) — NULL 口径污染绩效**
- 通俗解释: 统计"真实成交笔数"的 SQL 有个 NULL 陷阱 —— `exit_reason NOT IN(...)` 遇到 NULL 值会判成"未知"从而**漏掉**该行, 幽灵单反而被算进绩效。而毕业判定就是吃这个绩效。
- 专业解释: 全仓 6 个 web_dashboard 文件 (analytics/dashboard/risk/trade/data_service/thesis) 都存在同一模式 —— 正确的 `COALESCE(exit_reason,'') NOT IN(...)` 后面又跟一个冗余且 NULL-unsafe 的 `AND exit_reason NOT IN(...)`。系统性删除冗余子句, 保留 COALESCE 版作为唯一真相 (16 处 COALESCE 守卫全部保留)。
- 收益: 毕业判定输入干净 (PHANTOM/ORPHAN 不进胜率分母), 与修复 M 同一目标 (保护毕业数据) 的两面。回滚: git revert。

**修复 6a (P0 提前到现在) — LIVE 启动断言 (零数据扰动)**
- 通俗解释: 现在风控检查异常时是"放行开仓"(fail-open, SIM 期合理)。但万一某次误切真账户时忘了改成"拒绝开仓", 就会在真金上裸奔。加一道启动断言: 真账户模式下如果 fail 模式不是 closed, **直接拒绝启动**。SIM 模式完全不受影响。
- 专业解释 (`app/core/di_container.py`): 仅在 `_auto_account=='real'` 分支内, 断言 `risk.acb_fail_mode==closed && risk.preflight_fail_mode==closed`, 否则 `raise RuntimeError` 拒绝启动 (配置读取失败也拒绝)。
- 收益: 零成本防止积累期误配置/误切 real 时 fail-open 裸奔。回滚: 移除断言块。

**修复 3b (P0 起逐步) — 平仓失败可观测性**
- 通俗解释: 以前"为什么平仓失败/仓位卡住"这一半是黑暗的 (异常被吞进 debug 日志)。平仓失败=仓位卡住无止损保护, 是切实盘后最致命的场景。现在平仓失败升级为结构化 error + 飞书 HIGH 告警。
- 专业解释 (`app/playbook/pipeline.py`): 新增 `_alert_close_failure(symbol, env, reason, detail)` (WARN 日志 + push HIGH, 按 symbol+日+reason 去重防刷屏), 接入正常退出/SL兜底/EOD兜底三条平仓链路的所有卡住点 (BROKER_EXCEPTION / CLOSE_FAILED / ORDER_NOT_FILLED / SL_* / EOD_* 连续失败≥3)。record-only, 不改平仓行为。
- 收益: 补上"为什么平仓失败"这一半黑暗; 当下 SIM 也能先积累平仓失败画像。回滚: 移除 helper 调用。

**验证**: py_compile 全过 (12 文件); `get_exit_config_version()` 实测产出稳定哈希 `exit-4bc8f6797088` 且缓存一致; reconciler 防覆盖分支实测跳过写入并 WARN; `okr_status.py` graduation_sample 窗口化实测输出 (Closed:0 = 打标从今起, 符合预期); exit_shadow early_tight 变体实测触发 + 反事实计算正确; pytest 9 项失败经 `git stash` 对照证实为**基线遗留** (test fixture 缺 `mfe_pct` 列 + intraday trigger mock 设置), 与本批改动无关, **零新回归**。

**本批不做 (切 LIVE 前)**: 修复 1 (broker stop-limit 冗余止损底线) + 修复 6b (移除 SIM→REAL 账户 fallback)。**延后 (P1-P2)**: 修复 4 (半日/EOD 硬编码读交易日历) / 3a (删死 AuditLogger) / 2 (删 HSI 死逻辑 + 补 SPY 急跌维度)。

### 2026-07-19 · 退出侧结构性修复: 释放赢家 + 方向全错快速脱身

**核心诊断** (数据实证, 与 7/12 handoff 一致):
- 信号≈摄硬币偏正 (50.8% 持到收盘命中, +0.37%/笔) → DA 不是问题。
- **漏血在退出**: SL_TRAIL_LOCK 15笔 100%WR 但 avg_win=$27.59 (MFE 1.48% 仅捕获一半) vs EOD 赢家 avg=$96.74。
- **根因**: `drawdown_from_peak_pct=0.4` 在日内噪声底附近触发, 系统性截断赢家 → avg_win 上不去 → R:R 被压。
- **TIME_EXIT 11笔 0%WR**: MFE 全部<0.5% (4笔=0), 方向从未兑现, 但等 4-6h 才触发 TIME_EXIT。

**修复 A: `drawdown_from_peak_pct` 0.4 → 0.8** (`config/playbook_horizon.yaml` L49)
- 通俗解释: 以前浮盈从峰值回撤 0.4% 就锁利平仓, 但美股日内波动随便就 0.5-1%, 0.4% 等于“刚赚一点就被噪声晃出来”。现在给 0.8% 呼吸空间, 让赢家跑完趋势再锁。
- 专业解释: trailing stop 的 drawdown trigger 从 40bp 放宽到 80bp, 降低 trail_lock 触发频率, 提升 avg_win, 从而抬 R:R。
- breakeven_at_pct=0.5 不动 (仍早保本, 亏不了), 只放宽锁利回撤容忍。
- 回滚: 改回 0.4。判分: 下周五 exit_scorecard R:R + avg_win 对比。

**修复 B: 早期紧SL** (`unified_config.yaml` + `pipeline.py` 规则0.5)
- 通俗解释: 以前方向做错了, 要等到亏 2% 才止损 (或者等 4-6h TIME_EXIT), 像“温水煮青蛙”。现在入场后 30 分钟内, 如果价格反向走 1% 就立即止损 — 快速认错, 小亏出场。
- 专业解释: 入场后 30min 内 breakeven 未激活 (peak_profit < 0.5%) → SL 从正常 2-2.5% 收紧到 1%. 方向对→浮盈→breakeven激活→本规则自动失效; 方向错→1%SL快速触发SL_INITIAL。
- 与 cooldown_sl_pct(4%) 互补: cooldown 防开盘瞬间秒杀(放宽); 本规则防方向全错慢性失血(收紧). cooldown 优先。
- 退出原因仍为 SL_INITIAL, 无新标签, 复用现有基建。
- 回滚: `early_tight_sl_enabled` 改 false。判分: TIME_EXIT 笔数下降 + SL_INITIAL 早期笔数/损失。

**为什么不用 15min 硬时间门 (DIRECTION_FAIL)?**
- 15min 太短, 无法区分“方向错”和“慢热型” (EOD 赢家前 15min 也可能浮亏)。
- 硬时间门 = 另一种“砍赢家”, 与 handoff 诊断方向相反。
- 早期紧SL 更智能: 用价格行为 (1% 反向) 而非时间来判断, 且方向对时自动失效。

**验证**: py_compile ✓; PlaybookConfig 加载确认 drawdown=0.8 / early_tight=true/30min/1% ✓。

---

### 2026-07-19 · DEB-round3 pool→trade 链路审计修复批次 (commit `74defd97` + `5ccad370`)
- **辩论锚点**: `docs/debates/DEB-20260719-pool-to-trade-chain-audit.md` (三方收敛执行清单 v2)。P0 5 项 + P1 5 项全量落地。
- **#1 bridge 三态告警**: `opportunity_scan_{date}.json` 缺失/空/schema非法 → 飞书 HIGH (当日去重)。新增 schema fail-safe (非法视作缺失不消费半损坏文件)。覆盖"启动后文件仍缺"盲区 — `run_generate` + `auto_execute` (每5min) 双入口直读文件, 不依赖启动时一次性加载。
- **#2 失效门组** (`_check_intraday_trigger`): ①触发窗收紧 **14:00 ET** 截止 (`entry_time_cutoff_enabled`, 默认True); ②财报禁建日拒触发 (`earnings_blackout_gate_enabled`, 复用 `scheduler.earnings_risk_guard`); ③漂移>5% **指令作废** (`thesis_drift_invalidate` 写 skip_reason 白名单落库, auto_execute+trigger 双保险跳过; trigger_miss 遥测即 shadow 记录)。
- **#3-B**: SCAN 注入 `expires_at=24h`; **消费端首轮遗漏只写不读, 本轮核查发现已补** (commit `5ccad370`: auto_execute 跳过 + trigger 拒触发双保险)。
- **GHOST 根因修复**: RC1 — `playbook_service._confirm_order_filled` 入场单 order_status 短轮询≤5s, 未成交拒记账+释放抢占 (与 close 路径共用 2026-07-07 确立的真相源; 查询异常降级维持旧兜底); RC2 — `_classify_ghost_cause` 写 `metadata.ghost_cause` (double_tracked/entry_unfilled_or_external, record-only 供 2 周复审)。
- **熔断 shadow 告警版**: 当日 ≥2 笔分钟级(holding≤0.1h)亏损平仓 → 飞书 HIGH, **不阻断** (`sl_burst_alert_enabled` 默认True)。注: holding_hours 落库 round(.,1), 故用 ≤0.1 近似 <5min 口径。
- **30min-abort shadow**: `exit_shadow.ABORT_30M_VARIANTS` 全路径采集 would_abort (MFE<0.1%且浮亏) + close 反事实 `counterfactual_saved_pct` (正=abort更优)。TIME_EXIT 11笔0%WR 根因=方向未兑现, 但实单 abort 反事实 -$160 (杀 EOD 慢热赢家) → 仅 shadow。
- **#11 IC 覆盖**: `ic_tracker` 未触发指令缺 entry_price → 当日 open 代理回填, `fwd_proxy=1` 落库标记可剔除; db 迁移列已手动触发落地 (lazy 迁移周日未跑, 明日前已补齐)。
- **#6-A**: factor_exit conditions 零命中 → WARN (进程级去重, default 兜底行为不变)。
- **#12**: `scripts/gate_cost_report.py` (stdlib 只读) — 各门拦截成本 + VWAP 门 blocked/allowed `fwd_return_intraday` 终审 + 分钟级亏损统计。实测: §4 近14天 56笔 胜率39.3% 总PnL+$40.11。
- **δ.6 清洗口径入 `docs/OKR-SPEC.md`**: void 不计 + 事件日双向对称剔除 + SHADOW 本就不计; 清洗后基线 80笔/45.0%WR/+$1422.40。
- **验证**: py_compile 全过; pytest 33+28 项通过 (4 项 pre-existing 失败经 stash 对照证实与本次无关); 服务重启 PID 408034 零 error; 新符号导入全通。
- **⏳ 明日开盘待办**: ①bridge 文件外部依赖 (Hermes ET06:53 前后产出, 周五 7-17 已正常; 若缺失 #1 告警兜底+TechnicalFallback 降级); ②#2 漂移门/熔断/bridge 告警首日上线, 关注飞书 HIGH 是否误报; ③2 周后跑 `gate_cost_report.py` + abort_30m/ghost_cause 样本复审。
- **P2 本批不做** (数据驱动后再议): #8 入场带 / #7 UE调参+DA提量。**不做**: #5/#9/#10/#6-B、30min-abort 实单版、实单熔断。

### 2026-07-12 · LLM edge 判分 (n=211) + P1 影子判决埋点 (零交易逻辑)
- **缘起**: 另一 Agent 主张"接线 LLM 擅长的质疑/环境判断"(7 项 37 行)。核心方向对 (LLM 做减法/veto, 不做加法/predict, 与北极星一致), 但它引用的支撑数字**统计无效**: conf<40 胜率 0%(n=8)、6/16 幻觉(单日 anecdote)、47% 方向(n 未知)。
- **想直接回测"挑战者质疑→盈亏" — 查完数据: 不成立**。`data/debate_verdicts/` **空**; `debates/` 是 5 月因子分箱非逐笔判决; `model_votes.model_role` **88% UNKNOWN**(无 CHALLENGER 角色); `trade_outcomes` 干净 join **n=13**; `llm_calls` 只有 hash 无正文。**当年数据根本没留**, 跑出来只能是编的。
- **唯一真实可用样本 = fuzzy (symbol, date) join `decisions`↔`trade_records` = n=211** (相关非因果)。落 **`scripts/llm_edge_scorecard.py`** → `data/llm_edge_scorecard.json` (可复现)。结果: **conviction/共识 与盈亏负相关 (r≈−0.21)** —— LLM 越自信/越一致, 结果越差。独立佐证 conf 拍平 + "LLM 不会预测"。⚠️ 置信边界: 模糊 join、时间混淆(早期 regime=None 且更赚)、混合 source_type → **强线索非因果**。
- **P1 埋点 (先造能测的数据, 再让数据决定接不接 SKIP-gate)**: DA 挑战者/校验产物**已持久化在 `data/daily_battle_plan_us.json`** (`targets[].verdict` 总判决 / `data_cited[]` 论据含 `[Futu]` 溯源标 / `factor_validation` 逐因子 `FALSE_POSITIVE` 伪信号标 / `catalyst.source_count`)。新增 **`scripts/shadow_verdict_recorder.py`** 逐日抽取 → **`data/shadow_verdicts.db`** (按 (plan_date,symbol) UPSERT 幂等)。字段: evidence_sourced/unsourced、factor_confirmed/weak/false_positive、catalyst_* 等。
- **关键工程判断**: 原计划要 `decision_id` 穿 `signal_pipeline` 做 clean trace_id 链 —— 那是**高风险治理路径**, **不做**。改用**与 scorecard 同款 fuzzy (symbol,date) join** (已验证 n=211 可行) → **零 pipeline 改动、零交易逻辑、零服务重启**。埋点仅读 JSON 产物 + 写独立库。
- **调度**: crontab 加 `30 21 * * 1-5` (服务器 CST 21:30 = ET 09:30, DA 计划生成后) 逐个交易日捕获。**回滚**: `crontab -e` 删该行即可; 删 `scripts/shadow_verdict_recorder.py` + `data/shadow_verdicts.db` 彻底清除。
- **首跑**: 落 5 条 (plan_date=07-11), 5/5 含 FALSE_POSITIVE 因子 + 未溯源论据; join 盈亏 0 行 (当日未平)。
- **⏳ 待办 (3-4 周后)**: `python scripts/shadow_verdict_recorder.py --report` 攒够 ≥30 闭环后, 比"被标记(伪信号/未溯源) vs 未标记"的期望值差 → **差显著为负才接 SKIP-gate** (阈值用该回测校准); 无差异则连质疑都无接线价值。严防像退出砍赢家那样在入场端 veto 掉赢家。

### 2026-07-12 · 交易页 Phase B+C (sim/live 隔离 + 轻量 UX, 不改交易逻辑)
- **调查结论**: 交易页数据源已基本正确 —— 实时路径 `DataService._get_env_ctx()` 因 `_is_live()==True` 恒查**真实账户** (TrdEnv.REAL/REAL_ACCOUNT_ID); 交易记录/决策质量 `slow-data` 早已从本页移除 (`trade.js` L1467)。**唯一 sim 泄漏 = DB 兜底**。
- **Phase B (后端, sim/live 隔离)**:
  - `data_service.py` `_fetch_positions_from_db`: SQL 加 `AND LOWER(COALESCE(account_type,'')) != 'simulate'` (排除模拟仓, 保留 real+NULL); SELECT 补 `account_type,owner` 写入 dict。
  - `dashboard.py` `_parse_positions`: 防御性 `continue` 跳过 `account_type=='simulate'`。
  - 根因: `open_positions` 表混存 sim+live, 券商宕机走 DB 兜底时会把**模拟仓当实盘仓**渲染 (实测当时表内唯一仓即 `account_type=simulate/owner_strategy=playbook_sim`)。
- **Phase C (前端, 轻量)**: `trade.html` `.trade-summary` 资金头条加 `position:sticky;top:0;z-index:50` → 滚动持仓/记录时资产/购买力/盈亏/新鲜度圆点常驻可见。
- 验证: py_compile ✓ / SQL 谓词自检 (sim+大写SIM 排除, real+NULL 保留) ✓ / `_parse_positions` 防御跳过 ✓ / 重启 dashboard (PID 355297) 无 error / 兜底路径实测**返回 0 真实仓** (原会返回 1 条 sim)。
- **回滚**: 删 SQL 谓词 + `_parse_positions` continue + sticky CSS 即恢复, 零交易逻辑。

### 2026-07-12 · 交易页 Phase A (数据诚实化, 不改交易逻辑)
- **背景**: web dashboard 交易页 (`/trade`) 是**手动真实实盘**界面 (非模拟盘, `manual_order_service._is_live()` 恒 True → `TrdEnv.REAL`), 要逐步替代富途 app。目标: ①数据准确无虚构 ②信噪比更好。
- **Phase A = 纯诚实化, 零交易逻辑/下单/sizing 改动, 可回滚**。核心: 券商实时不可用时**绝不把 $0/成本价伪装成真实盈亏**。
- 后端 (3 文件):
  - `data_service.py` `_fetch_positions_from_db`: DB 降级持仓打 `data_source="db_fallback"`。
  - `dashboard.py` `_parse_positions`: 透传 `price_basis`(live/cost/delayed/none) + `stale`; `_parse_account` 打 `fx_estimated`(7.8 汇率兜底路径为 True); `_calc_pnl` 加 `positions_stale` 聚合标记。
  - `trade.py` snapshot: 加 `stale` + `as_of` 快照级新鲜度。
- 前端 (`trade.js`+`trade.html`):
  - 持仓 stale → 浮盈/今日盈亏显示 `--` (不再是伪造 $0); 现价加时段徽标 (成本/延迟)。
  - 顶部持仓盈亏/今日盈亏: 降级时显示 `--`(黄)。购买力估算加 `≈` 标记。
  - 新鲜度圆点三态: **绿=实时券商 / 黄=降级·成本价 / 灰=离线** + 文字提示。
- 验证: py_compile ✓ / `node --check` ✓ / 逻辑自检 ✓ (fx_estimated 三态正确; db_fallback→stale/cost, Futu行内价→delayed)。
- **待办**: ①**重启 `ftseek-dashboard.service`** 才能生效后端改动 (前端需浏览器硬刷新)。②Phase B(sim/live 记录隔离)/Phase C(信噪比 UX) 尚未做。

### 2026-07-12
- **拍平 conf_factor** (数据支持, 可回滚): 新增 config `playbook.risk_parameters.confidence_sizing_enabled` (默认 **false** → conf_factor 恒 1.0)。改动仅 `pipeline.py` L2338-2345 reprice 块 (DA 路径); DEC 路径 `instruction_generator.py` L393 本就不 conf 加权。验证: flag 经 `get_playbook_config()` 实测=False。**开回来**: yaml 改 true。
- **新增 `scripts/exit_scorecard.py`**: 退出纪律周度判分器 (混淆矩阵 + expectancy + R:R + 杀赢家数)。只读。
- **OKR 换轨**: `OKR-2026-07.md` 结算为 MISSED (根因=退出漏血, 非预测); 新建 `OKR-2026-08.md` 以 expectancy 为核心 KR, 删除"加仓追收益"危险对策。
- **日内 IC 上线** (前序): `fwd_return_intraday` 列已迁移到线上库 `data/ftseek_us.db` (已备份 `.bak.20260712_intradayic`)。近30天覆盖 175/179。IC: intraday −0.14 / t1 −0.04 / t3 −0.09 / t5 −0.15 (污染窗口, 非定论)。
- **待办/未做**: 退出旋钮 A 的实际修改留到下周五循环; `US.US.` 双前缀脏 symbol 2 条 (id 245/323, 陈旧 DEC 行, 不影响交易) 暂不清洗, 自然 age out。
- **开盘前就绪检查 + 重启 (✅ 2026-07-12 11:24)**: 全链路体检通过 (模块导入/配置开关/DB schema+迁移幂等/sizing 模拟/服务/循环存活)。
  已**重启 `ftseek-us.service`** (新 PID 345299) 激活 conf 拍平 —— 冒烟检查全绿: 无 error、OpenD 连通、
  安全确认**模拟盘** (TRADE_ENV=1)、Futu 交易上下文连接、调度器配置加载。
  **⚠️ 周一盘前须确认**: DA 的 bridge 文件由 deep_analysis cron 在 **ET 09:00** 生成; 若缺失, 当天降级
  `TechnicalFallbackAdapter` (技术兜底), **不走 DA 信号** → 整条 DA-FTSeek 链路当天不生效。
- **源码锚点提交 (✅ 2026-07-12)**: `9c5cb2cc`(133源码/文档) + `3fb5d6c1`(补齐 2 源码)。含本会话全部改动 + 此前累积。
  **未入库(有意)**: `data/*.db`·`app/data/*.db`(live 数据) / `logs/*` / 一堆未跟踪 junk。
  **⚠️ `config/unified_config.yaml` 卡治理未提交**: 含 `entry_reanchor_enabled:true`·`trailing_enabled:true` 等
  `enabled→true` 项, pre-commit hook (SYSTEM_OBJECTIVE §5) 要求 **ADR/RFC 人审批**才能入库。
  这些值**已在运行时生效** (磁盘配置已加载), 只是没进 git 历史。要入库须先补 ADR/RFC 或经你确认 `--no-verify`。
  (提交时两个 governance hook 均触发: pre-commit 拦 yaml enabled→true; commit-msg 要求高风险改动带 `[ADR]/[RFC]/[hotfix]` 标签。)

### (更早) 2026-07-11 及之前 — 见 `docs/WEEKLY-SUMMARY-2026-07-11.md`
- 决策链路双脑清理、退出引擎批次1、校准闭环、dashboard 可视化、_TRACKING_FIELDS 审计断裂修复等。

---

## ⚠️ 长期 caveat (读之前先知道)
- **样本量**: ~49 笔/月, 干净窗口 (date≥07-11) 目前近 0 样本。多数结论 n<50, 要几周 go-forward 才显著。别把污染窗口的数字当定论。
- **模拟盘宽恕一切**: sim 不建模真实滑点/流动性; 即使 sim 正期望也未必活到实盘。
- **DA 是 repo 外的 Hermes skill**: 若信号在干净窗口被证伪 (期望仍<0), 修复点在 skill, 不在本 repo (见 OKR-2026-08 硬退出触发器 2026-08-10)。
- **纪律**: hard-to-reverse/线上写操作先备份+确认; 不为凑样本放松风控; 一次一个变量。
