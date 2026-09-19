# RFC-083: 延长时段退出链超时链条修复 + 回放费用保真 (R2/R9 落地)

> **状态**: 已实施
> **日期**: 2026-09-18
> **作者**: Hermes (第四轮独立复核后的落地批)
> **审批人**: 项目所有者 (用户令「按建议执行」= 对建议 1/2/4/5 的整批授权; 建议 3 = 新增护栏, 仅出方案, 未实施)
> **关联文档**: `docs/debates/DEB-20260918-extended-hours-ssot-and-observability.md` §23 (复核与回执)、
> `docs/debates/DEB-20260918-crm-overnight-gap-review.md` (孤儿单事件复盘 + 更正)

---

## 摘要

对 §22「用户确认后的补闸」做独立复核后落地五件：(1) 修掉一处**空操作**修复（父子进程超时链条），
(2) 把 `trade_records` 净额口径回填进 §4.1 实盘样本表，(3) 把延长时段回放从扁平 8.5bps 换成**逐笔 per-share 费用模型**并出费用分档，
(4) `ALLOW_SIM_ENTRY` 代码默认由 fail-open 翻 fail-closed，(5) 新增**孤儿 ETH 单检测器（方案 A：纯观察、只告警、不撤单）**，
挂载进 exit cron 的会话门控之前。方案 B（撤单 + attempt 清理）仍未做，需另行签字。

---

## 问题陈述

1. **空操作修复（R2，实锤）**：§22.2 声称"父进程超时已修复"，实际 `max(90, EXT_EXIT_LIMIT_TIMEOUT_S + 40)`
   在默认 `20` 下恒等于 **90**，与改动前的硬编码 `timeout=90` 逐字相同；而实测子进程正是 **>90s 才被杀**
   （`logs/premarket_exit_shadow.log-20260918:195`，US.CRM 那轮）⇒ 失败模式原样保留（孤儿单 + 零证据）。
2. **口径错误（R1）**：§4.1 用毛口径，把当日最大亏损低估 **7.9×**（−82.02 → **−648.86**），
   并把实际最大亏损样本 CTNT（费用占名义 **9.15%**）记成"盈利样本"。
3. **回放费用失真（R9）**：`replay_ext_entry_sources.py` 只有扁平 `FEE_PCT=0.085`，低价档被低估最多 3.3×；
   而该费率同时决定规则臂的 **BE 净线**高度，口径错会让规则臂在低价股上系统性提前锁盈。
4. **默认值方向（R8）**：`ALLOW_SIM_ENTRY = os.getenv("EXT_ENTRY_ALLOW_SIM", "true")` = fail-open
   ⇒ `.env.us` 缺失/未加载/键名写错就**静默开仓**，而开关现在真的在下单。

---

## 提议方案（已实施）

### 1. 超时链条改单调（`scripts/{premarket,afterhours}_exit_shadow.py` + `scripts/extended_hours_exit_cron.py`）

```
cron 240s (run_once timeout_s)  >  shadow 180s (_executor_timeout = max(180, t+90))  >  子进程限价等待 20s (env)
```

- **两者必须同批改**：只抬 shadow 不抬 cron ⇒ cron 先杀 shadow，其孙进程（正在撤单/确认）仍在跑 ⇒ 孤儿单 + 零证据，**比不改更糟**。
- 240 的构成（如实）：180（容忍一个卡住的子进程走完撤单与终态确认）+ 60（同轮其余标的正常返回）。用 flock 防叠加，故长跑只会跳过后续 tick。
- 另：父进程超时时打印子进程**残留 stdout/stderr**（原实现只打异常字符串，"下单了没/撤单确认了没"无从判断）。

### 2. §4.1 改净额口径 + 费用/名义列（纯文档，数据源在 `trade_records`）

### 3. 回放费用保真（`scripts/replay_ext_entry_sources.py`）

- 全表改用逐笔 `estimate_round_trip(qty=int(POSITION_USD/入场价), entry, exit, "LONG", 日期)`，
  与现役链路**共用** `extended_hours_market_session.round_trip_fee_metrics`（不新造第二份费率逻辑）。
- 规则臂的 `fee_pct` 由逐 bar 价实时算（与 `premarket_exit_shadow` 同口径）。
- summary 新增 `fee_bands`（价格档费率）与 `arms_flat_fee_net`（旧口径对照）。

### 4. `ALLOW_SIM_ENTRY` 默认翻 `false`（`scripts/extended_hours_entry_executor.py`）

行为不变（`.env.us:135` 显式 `EXT_ENTRY_ALLOW_SIM=true` 且 `load_dotenv` 先于常量读取）；仅 env 缺失时 fail-closed。

### 5. 孤儿 ETH 单检测器（方案 A，`scripts/orphan_ext_order_watch.py` + exit cron 挂载）

- 触发源 = durable 账本（`exit_intents` 中最近 15 分钟 `state ∈ (PENDING,HELD)` 且 `last_error` 含
  `exit_unowned_active_order`）⇒ **无事件时零券商调用**；确认 = 复用共享 `query_order_rows` 查**一次**
  订单表，本地筛 `remark` 前缀 `ext_exit_` + 状态 ∈ `ACTIVE_ORDER_STATUSES` + `create_time` 年龄 ≥ 15 分钟；
  命中 ⇒ `data/orphan_ext_orders.jsonl`（按 (symbol, order_id) 跨进程去重）+ 飞书告警（每单一次，每日上限 5）。
- **不撤单、不动 state**：撤当日陈旧孤儿单会消耗该标的当日 attempt ⇒ 会静默掐掉当日机会（方案 B 才处理）。
- 挂载点在 `run_once` 的**会话门控之前**：阻塞事件在 RTH 09:3x、孤儿单在 ETH 05:50，两者都在 cron 窗外 ⇒
  放门控之后永远扫不到。检测器异常不阻塞退出链，但错误回填进 `result["orphan"]`（不静默）。

### 6. 人工确认撤销工具 + 探针静默 + AH 影子占位过滤（2026-09-18 末批，用户令"执行"）

复核建议 3 的语义是"告警并（**人工确认后**）撤销" —— 自动那半（§5 检测器）已落地，人工这半此前只有口头约定。
本批把它补成工具与开关：

- **`scripts/cancel_orphan_ext_order.py`**（新，人工入口，**不挂任何 cron/循环**）：
  安全门 = ① `remark` 前缀 `ext_exit_`（**绝不碰别人的单**）② 状态仍非终态 ③ 年龄 ≥ `EXT_ORPHAN_MIN_AGE_MIN`（默认 15min，与检测器同阈值）
  ④ `--symbol` 一致 ⑤ 部分成交但拿不到成交均价 ⇒ 拒绝；
  默认 **dry-run**，必须显式 `--apply`；撤单后**轮询确认**（默认 20s，`EXT_ORPHAN_CONFIRM_TIMEOUT_S`），
  且只有**撤销类终态**（`CANCELLED_ALL/CANCELLED_PART/FILL_CANCELLED/DELETED`）才判成功
  （撤单 API 成功 ≠ 已撤，历史 #185；且撤单异步，立即回查常见 `CANCELLING_ALL` 不可一次判死）——
  间隙成交 `FILLED_ALL` ⇒ 退出码 3 + 审计 `filled_during_cancel`；超时仍活/查询失败 ⇒ 退出码 2 + `cancel_unconfirmed`；
  其他非撤销终态 ⇒ 退出码 2 + `terminal_unexpected`。**绝不宣称成功**。
  `--clear-attempt` 才清 `{today}:{symbol}` attempt 键（默认不清，附 state 备份；清理时
  复用执行器 state 文件锁 + 原子写，持锁失败则不动文件）。
- **`FUTSEEK_PROBE=1` 探针静默**：`_push` 不推、且**不写去重键**（否则一次探针会吃掉该 key 的当日名额，反而静默掉生产真告警）；
  生产未设该 env ⇒ 行为完全不变。动机：用户看到的"连续推送虚拟标的"里有一类正是**人工/探针以 REAL 环境跑生产脚本**产生的
  （生产 cron 未设 `FUTU_TRD_ENV` ⇒ 默认 SIMULATE，审计里也无占位行）。
- **`scripts/ah_exit_shadow.py::load_open_positions` 占位过滤**：此前该影子会为 `US.TEST` 连续输出观测行（数据源里没有真持仓）。

### 影响范围

- 服务焦点: F1（延长时段退出链）+ F3（候选/费用口径）
- 改动指标: ①父进程被杀导致的孤儿单率 ②回放净均值口径（−0.013~−0.032pp 修正）③env 缺失时的开仓行为
- 预期变化: ①孤儿单窗口从"必然"降为"仅 broker 真卡死"；②口径修正后**结论与臂排序不变**（已实测）

---

## 风险评估

### 最坏情况
- cron 上限 240s ⇒ 一次卡住的运行会跳过最多 3 个 subsequent tick（同窗其他标的的退出最多延迟约 4 分钟）。
- 回放费用改动会让历史读数与旧报告不一致（已用 `data/reports/backup_20260918_r9/` 备份旧产物，并在 §23.2 标注取代关系）。

### 缓解措施
- 链条单调性用测试 `test_timeout_chain_is_monotonic` 钉住（防止将来只改一层）。
- 费用口径改动**不改任何交易行为**（只在回放/报告侧），且人工用 9/17 四笔实弹与 DB `fees_est` 逐笔对齐过。

---

## 验证计划

```bash
# 1. 链条与默认值
PYTHONPATH=. venv/bin/python -m pytest tests/test_ext_take_profit_only.py tests/test_extended_hours_entry_executor.py \
  tests/test_extended_hours_exit_cron.py tests/test_extended_hours_exit_cron_next_open.py tests/execution/test_exit_lifecycle.py -q
# → 134 passed
# 2. 窗外门控未死
PYTHONPATH=. venv/bin/python scripts/extended_hours_exit_cron.py --dry   # → outside_extended_hours
# 3. 费用模型对 DB 逐笔复算
python -c "from app.execution.fee_model import estimate_round_trip; print(estimate_round_trip(68493,0.072,0.074,'LONG','2026-09-17')['total'])"  # → 451.26 (= fees_est)
# 4. 回放重跑 (60 交易日, 逐笔费用)
PYTHONPATH=. venv/bin/python3.11 scripts/replay_ext_entry_sources.py --days 60 --session premarket --end 2026-09-17 --source both
# 5. 孤儿单检测器 (单元 + 真实券商接线探针)
PYTHONPATH=. venv/bin/python -m pytest tests/test_orphan_ext_order_watch.py -q      # → 15 passed
PYTHONPATH=. venv/bin/python scripts/orphan_ext_order_watch.py --dry                # 真实券商: 无触发 ⇒ 不连券商
PYTHONPATH=. venv/bin/python scripts/extended_hours_exit_cron.py --dry              # → outside_extended_hours + orphan 段
# 6. 人工撤销工具: 真实券商 dry-run —— 终态单必须被拒 (安全门)
PYTHONPATH=. venv/bin/python scripts/cancel_orphan_ext_order.py --order-id 8185508   # → already_terminal:CANCELLED_ALL, 退出码 1
# 7. 探针静默 + AH 影子占位过滤 + 工具安全门
PYTHONPATH=. venv/bin/python -m pytest tests/test_cancel_orphan_ext_order.py \
  tests/test_ext_entry_test_symbol_guard.py tests/test_ah_exit_shadow_echo.py -q      # → 26 passed
```

---

## 回滚计划

```bash
# 超时链条: 单行 env 优先 (不改码)
#   shadow: max(180, t+90) → 如需回退到 90s, 改 _executor_timeout 常量; cron 同步改回 120
# 回放费用: FEE_PCT 常量仍在, 把 _round_trip_fee_pct 调用换回 FEE_PCT 即回到旧口径
# 开关默认: 把 ALLOW_SIM_ENTRY 默认字面量改回 "true"
# 孤儿单检测器: env EXT_ORPHAN_WATCH=false 整关 (保留审计行); 或删 run_once 里那次 scan_orphan_orders() 调用
# 旧回放产物: cp data/reports/backup_20260918_r9/* data/reports/
# 人工撤销工具: 不挂循环 ⇒ 无需回滚 (不用即放着); 探针静默: unset FUTSEEK_PROBE 即恢复生产推送
# AH 影子占位过滤: git revert (单文件)
```

---

## 未实施（明确登记）

- **方案 B 的自动化那半**（**自动**撤单 + attempt 清理 + `resume_close` 恢复保护）：本批只做了"人工确认后撤销"
  那一半（§6 工具）—— 自动撤单会消耗该标的当日 attempt（= 静默掐掉当日机会），需用户另行签字才做。
- ~~R5 `OVERNIGHT_GAP_STOP` 档位聚合~~ → **已做**：CRM 复盘文末 R5（4 次净 −1,179.26，稳定 ~$270/笔 档位）。
- ~~R7 的两处时刻证据~~ → **2026-09-18 末次闭环**：① 8.6s 差**已解释**（`exit_time` = 头寸同步 tombstone 确认时刻，
  日志 `21:49:11,710 [US Sync] tombstone 确认 US.CRM` 与 DB `09:49:11.68` 差 0.03s）；
  ② "09:48:52 撤销" 由 app 侧 hold 循环界定到 **(09:48:46, 09:49:01] ET**（券商 `update_time` 仍空 ⇒ 不逐字证实）。

---

## 2026-09-19 补充批 — 推送出口收口 (单测 fixture 真发生产 IM)

**触发**: 用户收到一条"建仓成交富卡", 逐项等于 `tests/test_extended_hours_order_execution.py:136-141`
的 fixture (`US.FAKE` 100@10.0 / 盘前 +3.00% / EXT 量 300,000 / 财报超预期 +10.0% / SL $9.50)。
逐项实证生产链不可能产生它: `trade_records` / `open_positions` / `logs/ftseek_us.log` 三处 `US.FAKE`
**0 次** ⇒ 是**通知出口被测试标的污染**, 不是交易链故障、零交易影响。

**三个真缺口 → 三处修 (全部零交易行为变更)**:

| # | 缺口 | 修法 | 文件 |
|---|---|---|---|
| 1 | `_push_filled` 不走 `_push_once` 去重 ⇒ 同一笔成交被反复演练可反复外发 | 抽出 `_dedup_reserve(key)` 供两处复用; 富卡 key = `ext_entry_filled:<symbol>:<pos_id>` | `scripts/extended_hours_entry_executor.py` |
| 2 | 静默只看 env ⇒ **绕过 conftest 的 pytest 跑法** (`--noconftest` / 裸 import 测试模块) 无任何静默 | `_probe_mode()` 追加"进程内在跑 pytest"判定 (四条推送出口共用同一判据) | 同上 |
| 3 | 出口没封住时测试**照样全绿**, 泄漏只能靠用户看 IM | conftest 会话结束: 渠道 `send` 层有留痕 ⇒ 置 `exitstatus=FAILED` + 点名; 白名单 `@pytest.mark.allow_outbound_notification` | `tests/conftest.py` + `pytest.ini` |

**为何不用 pytest.ini 的一行 `env =`**（原建议如此）: 它需要 **pytest-env 插件, 本机 venv 未装**
⇒ 写进 ini 只会得到 `Unknown config option: env` 警告并**静默不生效** = 假修复。判据改放 `_probe_mode()`,
覆盖全部 pytest 跑法（含 `--noconftest`）且生产进程零变化（`app/`、`scripts/` 无 `import pytest`）。

**验证 (实测, 非推断)**:

```bash
# 1. 新回归 + 相邻守卫
venv/bin/python -m pytest tests/test_ext_entry_push_guards.py tests/test_ext_entry_test_symbol_guard.py -q
#   → 9 passed
# 2. "放行方向"实测 (只验拦截方向 = 可能把生产告警永久静默): 干净子进程 _probe_mode()=False; FUTSEEK_PROBE=1 → True
# 3. 报红机制自检: 临时用例打渠道 send 层 → 打印 "1 passed" **但会话退出码 1** + 点名用例 + 首条正文
# 4. 事故路径复现: probe 关 + 真 Hub(渠道被 conftest 封) ⇒ 渠道层收到 **2 条**(钉钉+飞书双发)富卡,
#    正文 = `✅ US.FAKE BUY 成交 … 成交价 $10.00 数量 100 股 …`  ← 与原事故同形
# 5. 相关回归 203 passed (ext_entry / 孤儿单 / 推送 共 16 个文件); 全量套件与改动前基线失败集逐条比对
```

**回滚**: `git revert <本提交>`（单批: tests/ + pytest.ini + 脚本推送分支 + 文档, 不需重启服务 ——
脚本由 cron 每轮新起进程）。只想撤第 2 条: 删 `_probe_mode()` 里 `return "pytest" in sys.modules ...`
一行, 保留 env 分支。

**盲区 / 未做（登记）**:

- `US.FAKE` **不进** `is_test_symbol()`（会误伤 8 个把 US.FAKE 当正常符号的既有用例, 9/18 已回退）
  ⇒ "演练标的"这一类仍无代码概念, 只能靠"去重 + pytest 静默"收敛, 不是白名单式根治。
- `trade_entry` 富卡是 `Level.NORMAL` ⇒ FlagFileChannel 不写 `data/critical_alerts.flag`、Hub 也不落盘
  每笔推送 ⇒ **没有新去重键就查不到痕迹**。该判据已登记进
  `DEB-20260919-review-decision-support-system.md §5.8`（`self_audit.fixture_symbol_pushes`）。

---

## 共享 skill 编辑台账（multi-agent-project-collaboration 要求；供他 agent 审计/勿重提被否决改法）

| skill | 编辑 | 依据 |
|---|---|---|
| `~/.hermes/skills/quantitative-trading/ftseek-extended-hours-trading` | ①新增「超时链条必须单调」节：三层链 `cron 240 > shadow 180 > 子进程 20`、`max(A, env+B)` 类修复**必须代入现役默认值算实数**再与旧值逐字比、“只抬一层更糟”（外层先杀 → 孙进程仍在跑 ⇒ 孤儿单 + 零证据）、超时必须打子进程残留 stdout/stderr；②回放费用节由“不能用扁平费率”改为**已落地**并写入实测读数（−0.013~−0.032pp、$5–10 档 0.279% = 3.3×）；③参考节加本 RFC 与孤儿单检测器（方案 A 已实施 / 方案 B 待批）；④「延长时段退出载体」节加孤儿单检测器（挂载点/触发源/阈值/env/只告警不撤单）；⑤「其它坑」加一条：订单级证据查 `order_list_query` 而非 history 端点（后者 SIM 恒 n=0），`update_time` 为空 ⇒ 终态时刻不可复核；⑥新增「占位标的 (US.TEST) 与探针纪律」节（四入口过滤只用 `is_test_symbol`；PDT 台账污染会间接拒真交易；fixture 别拿 US.TEST 当通用符号；`trade_records.exit_time` = 头寸同步时刻而非成交时刻；人工撤销工具的安全门）；⑦新增「探针/测试不得污染生产」节（`FUTSEEK_PROBE=1` 不推且**不写去重键** —— 否则探针吃掉生产当日告警名额） | 本 RFC §1/§3/§5/§6；DEB §23.1 R7 自证伪、§23.2、§23.3、§23.5 |
| `~/.hermes/skills/engineering-change-protocol` | Anti-Rationalizations 表新增一行「常量推导式空操作」反模式（含逐字对比规程与链条单调性） | 本 RFC §1（R2 实锤） |
| `~/.hermes/skills/devops/ftseek-verification-audit` | 新增「复核"改读数"类改动」节：①改读数必落档三件（改哪条读数/切线日期/证伪指标），缺一按 P1 治理缺口报；②A/B 对比搭台纪律（**别把脚本复制到 /tmp** —— REPO 派生常量指向 /tmp 会造出自制 detector_error，误算成被复核方缺陷；worktree 跑基线要 `ln -s 生产仓/data`，别用 fixture 库）；③双实现分叉的合并规程（逐输入类比动作差异 → 算现网命中数 → 0 命中才敢称行为中性 → 加防复活测试）；④新增/变更 LLM 参数必须 provider 级实弹核 HTTP 200 vs 400（本仓 4xx 会静默 fallback ⇒ 改动变空操作），并核 `payload.update(extra_body)` 在 `setdefault` 之前 | 本 RFC §6 工具修订复核；DEB `DEB-20260918-quality-caliber-and-llm-thinking-budget.md` §3/§4 |
| `~/.hermes/skills/quantitative-trading/ftseek-extended-hours-trading` | 「探针/测试不得污染生产」节由 2 条扩到 5 条：①静默判据唯一落点 = `_probe_mode()`（同时认 `FUTSEEK_PROBE=1` 与"进程内在跑 pytest"，覆盖 `--noconftest` / 裸 import）；②加开关式修法先验插件装了没（`pytest.ini` 的 `env =` 需 pytest-env，未装即**假修复**）；③每个新推送出口必须接 `_dedup_reserve`，key 带 `pos_id`（同标的再入场照发）；④"测试有没有真外发"要能自动报红（conftest 会话结束置败 + 白名单 marker）；⑤渠道桩必须 `staticmethod` 装类上（否则描述符把实例当第一参数） | 本 RFC 2026-09-19 补充批；DEB-20260919-review-decision-support-system §5.8 |

**未改动的共享 skill**：`ftseek-trigger-execution-audit` / `backtest-rule-replay` 等未触及；孤儿单检测器落地方案 A 时已按上表第 ④⑤ 项补进 extended-hours skill（方案 B 落地时再补）。
