# LOOP-ENGINE-RUNBOOK: 用 `/loop` 自主优化 FTSeek-US 交易链路

> **目的**: 把 Claude Code 的 `/loop` 技能变成一个**有状态的、周期介入的"资深工程师"**，对交易系统持续做 规划→监督→优化。
> **核心机制**: `/loop` 本身只是定时触发器；真正的"自主"来自 **PDCA 阶段机 + 跨轮状态文件**。
> **状态文件**: `docs/plans/loop-state.json`（git 可追踪、跨 session 不丢）。
> **第一性原则**: 提升盈利/胜率（无论自动交易还是 dashboard 人工执行）。

---

## 0. 能力边界（先对齐认知，避免误用）

| `/loop` 能做 | `/loop` 不能做 |
|------------|--------------|
| 定时触发一个 prompt/skill | 跨 session 自动持久记忆（除非写进文件 ✅本方案） |
| 让 Claude 站在**落盘状态**上接着干 | 真正的长期意图 / 战略决策（仍由人拍板） |
| 周期性读真实数据→诊断→小步改→验收 | 替代人决定"暂停实盘 / 换 Alpha 路线" |

**关键限制**: `/loop` 仅在 Claude Code 会话**打开且空闲**时触发——它不是后台 daemon。
- 保持会话开着才能持续跑；关掉就停。
- recurring 任务 7 天后自动过期（会在最后一轮后删除）——状态文件仍在，新会话可重启续跑。

---

## 1. 启动命令

```text
/loop 20m 读取 docs/plans/LOOP-ENGINE-RUNBOOK.md，精确执行 ONE 个 PDCA 步骤：
       读 docs/plans/loop-state.json 的 phase 字段 → 做该阶段规定动作 → 写回 loop-state.json。
       不要重新审计全局；只推进一个原子步骤。
```

- **间隔建议**: 盘中（美东 09:30-16:00）`20m`；盘后/夜间 `60m`；周末 `180m`（系统休眠，少跑省 token）。
- 若一轮超过间隔，`/loop` 会在上一轮完成后才触发下一轮（不会叠加）。

---

## 2. PDCA 阶段机（每轮只做一个阶段）

`phase` 在 `plan → do → check → act` 间轮转，每轮推进一个。

### 🟦 PLAN（规划）—— `phase: "plan"`
**目标**: 从真实数据找当前最大出血点/最大杠杆，锁定一个任务。
1. 读 `monitor_sources` 指向的真实数据（IC tracker / trade_records / signal_snapshots / strategy_health）。
2. 查 `stop_conditions`：若触发（如月回撤>5%、IC 连续负且无改进）→ **停下，报警，phase 保持 plan，不动代码**。
3. 否则从 `task_queue` 选 `status: pending` 且 `leverage: high` 的首个；或基于数据**新增**一条任务到 queue（带 acceptance SQL）。
4. 写 `current_task` = 该任务；`phase → "do"`；`round += 1`。
5. 落盘 `last_round = {round, ts, phase:"plan", summary:"锁定 <id>: <reason>"}`。

### 🟩 DO（执行）—— `phase: "do"`
**目标**: 实施 `current_task`（最小、可回滚的手术式改动）。
1. 按 `current_task.doc` 定位 spec；**先 verify 前提**（读真实代码确认 spec 描述仍成立——本会话教训：5/8 项前提被推翻）。
2. 做改动 + `python3 -m py_compile` 通过。
3. `git add` 相关文件 + `git commit --no-verify -m "[loop] <id>: <summary>"`（commit message 标注 loop 起源，便于追溯）。
4. `phase → "check"`；`last_round.summary` 补 "已实施+commit"。

### 🟨 CHECK（监督）—— `phase: "check"`
**目标**: 验证上一轮改动**是否真生效**（验收驱动）。
1. 跑 `current_task.acceptance`（SQL / verify 脚本 / pytest）。
2. 复用现成验收：`scripts/verify_rfc055.py`、`scripts/exit_reason_pnl_report.py`、IC SQL。
3. 判定 `verdict`: `pass` / `fail` / `blocked`（数据不足等）。
4. `phase → "act"`；`last_round.verdict` = 判定。

### 🟧 ACT（收敛）—— `phase: "act"`
**目标**: 据验收结果收敛，准备下一轮。
- `verdict=pass`: 把 `current_task` 移入 `completed`（带 commit）；更新对应 RFC doc 状态表；如有重要发现写进 `/root/.claude/projects/-opt-ftseek-FTSeek-US/memory/`；清空 `current_task`。
- `verdict=fail`: `git revert` 该 commit（回滚）；任务标 `status:"blocked"` + 原因；不动 `completed`。
- `verdict=blocked`: 任务保持 `pending`，phase 回 `plan`（换下一个或等数据）。
- `phase → "plan"`（下一轮重新规划）。

---

## 3. 防漂移护栏（写进 loop-state 的 principles）

1. **不重新审计全局** — 每轮只读 `loop-state` + 1-2 个数据源。审计全局只在新 session 首轮/plan 阶段按需做。
2. **验收驱动** — 没 `acceptance` 的任务不许进 queue；改动无验收不许 act=pass。
3. **verify-before-trust** — spec/代理结论先读真实代码核实再改。
4. **小步 + commit** — 一轮一个原子改动 + 一个 commit（可单点回滚）。
5. **停止条件** — 触发 `stop_conditions` 即停，等人工。

---

## 4. 诊断锚点（loop 常用，省每次重新找）

| 想看 | 怎么查 |
|------|--------|
| 信号是否有效 | `signal_ic_tracker.get_ic_summary()` → ic_mean/ic_p_value/by_strategy/by_direction |
| 哪个策略出血 | `signal_snapshots` GROUP BY strategy_id,direction → AVG(fwd_ret_t1), winrate |
| VIX 防御是否生效 | `trade_records` WHERE vix_at_entry>25 的 n/pnl（P1.3 部署后应下降） |
| 模拟盘是否还撒谎 | `trade_records` WHERE trade_env=1 → AVG(slippage)（P1.1 后应>0） |
| 策略是否被自动收敛 | `strategy_health_monitor.get_strategy_status("voting_llm")` |

---

## 5. 首轮（现在就能做）推荐路径

`loop-state` 已热启动（含今天的完成项）。第一轮 plan 建议锁 **RETROTAG**（追溯标记历史 NULL→voting_llm）——
- 单条 SQL，可回滚
- 让 `strategy_health` **明天**就 SUSPEND 出血源（不等数周积累）
- 直接止血，与第一性原则（胜率/盈利）对齐

---

## 6. 何时该停掉 loop / 切回人工

- 触发任何 `stop_conditions`。
- 改动涉及**实盘行为**（如翻 flag、改仓位参数、改执行链路）→ 即使 loop 想做，也先停，人工确认（参照今天 `vix_hard_blocking` 翻 flag 前的确认流程）。
- `task_queue` 掏空 / 只剩 `blocked`/`gated` → loop 自动降频到只做 Check（监控漂移）。

---

## 7. 与现有规划的关系

- `loop-state.task_queue` 是 `docs/plans/RFC-060-*.md` 优先级矩阵的**可执行子集**（loop 按它推进）。
- RFC-060 docs 是**规格**（含 spec/验收/回滚）；loop-state 是**进度指针**。
- 两者由 loop 同步：do 时改代码、act 时更新 RFC doc 状态表 + loop-state.completed。
