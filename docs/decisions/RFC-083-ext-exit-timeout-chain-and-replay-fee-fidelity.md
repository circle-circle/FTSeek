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
```

---

## 未实施（明确登记）

- **方案 B**（孤儿单撤单 + `{today}:{symbol}` attempt 清理 + `resume_close` 恢复保护）：需另行签字。
- R5 `OVERNIGHT_GAP_STOP` 档位聚合进单笔复盘模板。
- R7 的两处时刻证据（"09:48:52 撤销"、"09:49:03 成交"）：`order_list_query.update_time` 为空，不可复核
  （订单身份/状态/创建时刻已由 `order_list_query` 独立复核，见 DEB §23.1 R7 的自证伪更正）。

---

## 共享 skill 编辑台账（multi-agent-project-collaboration 要求；供他 agent 审计/勿重提被否决改法）

| skill | 编辑 | 依据 |
|---|---|---|
| `~/.hermes/skills/quantitative-trading/ftseek-extended-hours-trading` | ①新增「超时链条必须单调」节：三层链 `cron 240 > shadow 180 > 子进程 20`、`max(A, env+B)` 类修复**必须代入现役默认值算实数**再与旧值逐字比、“只抬一层更糟”（外层先杀 → 孙进程仍在跑 ⇒ 孤儿单 + 零证据）、超时必须打子进程残留 stdout/stderr；②回放费用节由“不能用扁平费率”改为**已落地**并写入实测读数（−0.013~−0.032pp、$5–10 档 0.279% = 3.3×）；③参考节加本 RFC 与孤儿单检测器（方案 A 已实施 / 方案 B 待批）；④「延长时段退出载体」节加孤儿单检测器（挂载点/触发源/阈值/env/只告警不撤单）；⑤「其它坑」加一条：订单级证据查 `order_list_query` 而非 history 端点（后者 SIM 恒 n=0），`update_time` 为空 ⇒ 终态时刻不可复核 | 本 RFC §1/§3/§5；DEB §23.1 R7 自证伪、§23.2、§23.3 |
| `~/.hermes/skills/engineering-change-protocol` | Anti-Rationalizations 表新增一行「常量推导式空操作」反模式（含逐字对比规程与链条单调性） | 本 RFC §1（R2 实锤） |

**未改动的共享 skill**：`ftseek-trigger-execution-audit` / `backtest-rule-replay` 等未触及；孤儿单检测器落地方案 A 时已按上表第 ④⑤ 项补进 extended-hours skill（方案 B 落地时再补）。