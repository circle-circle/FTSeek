# RFC-153 FADE_SHORT 开盘快跌禁追门（SIM 启用）

**状态**: 2026-10-20 用户批准「批准 SIM 启用」→ 实施
**类型**: 入场确认门参数扩展（仅 SIM；LIVE 仍在四锁 + 毕业门禁下，不放开）
**关联**: RFC-141 入场延迟确认门、RFC-152 执行 TCA、OKR-2026-10 C1
**C1 名额**: 本门属于**新入场假设**，按 OKR-2026-10 C1 占用 10 月唯一名额。期间不再立第二个入场假设。

---

## 1. 证据（2026-10-20 盘中实测 + 近 30 日样本）

今日 5 笔已平：2 胜 +$26.58 / 3 负 −$317.86，净 **−$291.28**。两笔大亏同型：

| symbol | 方向 | 入场时刻 | 确认窗顺向移动 (`opp_cost_pct`) | 执行锚 vs 计划锚 | MFE | 结局 |
|---|---|---|---:|---:|---:|---|
| US.LITE | SHORT | 09:44:42 | **+1.068%** | −340bps | 0.03% | `SL_INITIAL` −$149.47 |
| US.NBIS | SHORT | 09:41:21 | **+1.412%** | −265bps | 0.52% | `SL_INITIAL` −$147.97 |

两笔均为 `FADE_SHORT`，均在开盘后 15 分钟内，都是“确认窗内价格已经快速顺向下跌 → 门确认 → 实际变成追空”。

近 30 日（`PLAYBOOK_SIM`，已平，join `raw_instruction_json`）：

- `FADE_SHORT` 开盘 09:35–09:45：**n=23，净 −$446.69**；09:45–10:30 为 +$193.23（n=7）。
- `FADE_SHORT + 开盘 + 执行锚低于计划锚 >200bps`：**n=11，净 −$549.34**；4 笔 `SL_INITIAL` 全在该组合。
- 入场确认门已确认的 SHORT 行（10-12 起可 join 样本 n=12）按 `opp_cost_pct` 分桶：

| opp_cost_pct | n | 净 | 胜率 |
|---|---:|---:|---:|
| `<0.3%` | 3 | +$22.68 | 100% |
| `0.3–0.6%` | 4 | +$112.92 | 50% |
| `0.6–1.0%` | 3 | +$23.76 | 100% |
| `>=1.0%` | **2** | **−$297.44** | **0%** |

**机制**：`FADE_SHORT` 的本意是等反抽再空；确认门当前把“价格已顺向下跌”当成确认，等于在快跌末端追空。`opp_cost_pct >= 1%` 表示 2 分钟确认窗内价格已比 arm 锚顺向走掉 ≥1%，这是“追”而不是“等”。

**诚实边界**：阈值上方只有 n=2；证据是方向性的，不是统计显著。选择现在启用 SIM 的理由：左尾集中（4 笔 `SL_INITIAL` 全中）、机制明确、影响面窄、回滚一行；不是宣称已证明。

## 2. 规则（精确范围）

在 `entry_confirm_gate.sweep_entry_confirmations` 的 2 分钟判定点：

```text
IF archetype == FADE_SHORT
AND 0 <= session_min < entry_confirm_fade_short_window_min (默认 15)
AND still_triggers == True
AND opp_cost_pct >= entry_confirm_fade_short_max_opp_pct (默认 1.0%)
THEN ABANDONED (cause=fade_short_chase_cap)
```

- 只作用于 `FADE_SHORT`；`PULLBACK_LIMIT`/`BREAKOUT`/无 archetype 均不改变。
- `opp_cost_pct` 沿用 RFC-141 既有公式（执行锚×last，方向归一，正=延迟后入场更差），不换尺子。
- 不进入 grace：这是“已经追出去 ≥1%”的硬边界，不是单点噪声。
- 落库 `skip_reason` 仍为稳定契约 `entry_confirm_reversed`（下游日报/PlanEffectiveness 的分母口径不变）；具体原因写 `metadata_json.entry_confirm.abandon_cause` 与前瞻行 `chase_cap_hit/cause`。
- 配置（`playbook.execution_parameters`，拍平层）：
  - `entry_confirm_fade_short_chase_cap_enabled: true`
  - `entry_confirm_fade_short_max_opp_pct: 1.0`
  - `entry_confirm_fade_short_window_min: 15`
- 回滚：`entry_confirm_fade_short_chase_cap_enabled: false` + 重启（一行，不删数据）。

## 3. 预注册裁决（读数前写死）

- **样本门**：自 2026-10-21 起 ≥10 个 ET 交易日且 ≥30 笔 `FADE_SHORT` 开盘确认样本（treatment + abandoned 均计入机制样本；PnL 只对有成交的 treatment 计）。
- **主判据**：与门开启前同人群基线（2026-09-23–10-20，n=23，净 −$446.69 / `SL_INITIAL` 4 笔）比较：
  1. `FADE_SHORT` 开盘组 `SL_INITIAL` 笔数下降 ≥50%（基线 4 → ≤2，按 30 日窗口折算）；
  2. 扣费净 bps/笔 不低于基线，且日聚类 t 不显著恶化；
  3. 误杀可量化：被 abandon 的样本用 `_POST_WATCH` +5m/+10m 方向归一价差统计“若入场是否盈利”；误杀净额必须 < 避免亏损净额。
- **裁决动作**：三条件同时满足 ⇒ 维持/考虑延长观察；任一不满足 ⇒ 回滚并落档，不得静默续跑。
- 不足 30 笔前只报进度，不作“有效/无效”结论（宪章第 5 律）。

## 4. 非目标

- 不改 `entry_confirm` 的 2 分钟延迟、mid 判尺、grace 2-of-3；不改其他 archetype。
- 不改 `chase_cap` 生成侧、不改 DA prompt、不改仓位/止损/退出。
- 不放开 LIVE；不新增第二条入场假设。

## 5. 顺带修复（只读影子记录质量）

`execution_tca_shadow.finalize_and_record`：成功成交行若仍带前几轮拦截的旧 `skip_reason`（如实盘 10-20 的 RIVN/LITE），改为写 `prior_skip_reason` 并把 `skip_reason` 置 None；失败行保持原样。零交易行为变更。

## 6. 变更记录

- 2026-10-20 v1：用户批准 SIM 启用；同批落 RFC-152 TCA 记录质量修复。
