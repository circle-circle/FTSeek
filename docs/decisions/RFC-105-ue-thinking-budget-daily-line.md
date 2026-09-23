# RFC-105 UE 思考预算读数并入日报（一次性后验 job → 常驻一行）

- **日期**：2026-09-24 | **状态**：**已落地**（L1 监测面改动，零交易逻辑变更）
- **用户裁决**：2026-09-24「UE 截断是否正常 请核查 是否可以调整为 glm-5.3-prime」→ 核查给出
  「截断已在 9/18 修复、日报 23.8% 是滚动窗口尾巴」+「Prime 是同模型优速档，非能力升级」→
  追问「「UE 思考预算后验」一次性 job …要不要重挂载体？」→ **「并入每日 LLM 日报常驻一行
  （不再依赖一次性 job）」**
- **载体**：`scripts/daily_llm_token_report.py`（crontab `20 6 * * 2-6 --push`，standalone 读
  config/库 → **无需重启任何服务**）
- **回滚**：`git revert` 本提交即可（只加一段读数，不删既有段；`_build_message` 新增参数有默认
  `None`，旧调用行为逐字不变 — 回归守卫见 §4）

---

## 1. 起因（核查结论，先钉事实）

用户问 UE 截断（日报 7 日窗口 `universe_expansion_reasoning: 5/21 (23.8%) 🔴`）是否正常。

| 事实 | 证据 |
|---|---|
| 截断**已发生且已修复**，非当前状态 | 主调用触顶率 **改前 6/9=67%**（CST 9/15–9/18 15:51，llm_audit id 11686/11719/11721/11751/11779/11782）→ **改后 0/10**（`reasoning_effort: high` 落库后），max_out 9662/24000，兜底行 0，p50 延迟 163s→63s |
| 「抬 max_tokens」单独**无效** | 9/17 已抬 16000→24000，当晚两窗仍 24000 触顶（id 11779/11782）⇒ 真因是思考链烧预算，非输出真超限 |
| 日报 23.8% = **滚动 7 日窗口的尾巴** | 5 行 = 3 次真实事件（2 行是 `downshift_retry` 重试臂重复计数），全部发生在锚点前；窗口 = `[target-6, target]` CST ⇒ target 9/24 仍显示 2/15、**target 9/25 起自动消失**（同 SQL 复算实测） |
| 后验载体**已死且结论从未送达** | 一次性 job `96c8e404f55a`（载体 `ops/hermes/ue_truncation_readout.py`）9/18 21:45 跑完即消耗（`repeat.times=1`），`executions.db.delivery_outcome=failed`；且它在锚点后**尚无样本**时跑（当时唯一结论只会是「样本不足」）。9/24 手动复跑读数 = **改善 0/9** |
| `glm-5.3-prime` 存在但**不是能力升级** | DashScope `/models` 实测 created 2026-09-21；官方「优速模式（Prime）」文档：TPS 1.5–2×、**能力与使用限制同原版**、价格 2×（¥16/¥56 vs glm-5.3 ¥8/¥28）；本账号共享 endpoint 可直接调用，同样**强制思考**（`enable_thinking=false` → HTTP 400 `restricted to True`） |

**⇒ 本 RFC 只解决「读数载体死了」这一件事**（判据/锚点/阈值一律不动）。

## 2. 改动（最小面）

1. `scripts/daily_llm_token_report.py` 新增 `UE_BUDGET_ANCHOR` / `UE_FEATURE` /
   `UE_BUDGET_BASELINE` 常量与 `_query_ue_budget(conn)`，并在 `_build_message` 末段输出常驻一行：
   ```
   ── UE 思考预算 (glm-5.3 强制思考, 修复锚 9/18 起累计) ──
   主调用 10 次 | 触顶 0 (0%) | 最大 out=9662 | 中位延迟 63s | 失败 0 | 兜底 0 次 | 降档重试 0 次 → 判定: 改善 (改前基线 6/9=67%)
   ```
2. 档 `data/llm_token_daily_<day>.json` 增 `ue_budget` 键（供事后复算，不参与任何判定）。

**口径三条**（都写进 docstring，防下一份文档读错）：
- `downshift_retry` 行 = 同一轮的降档重试臂 ⇒ **不入分母**（否则一次触顶记两次；实测 5 行↔3 事件）；
- 兜底行（非 glm 模型）**单独计数** —— 主模型失败由 fallback 作答，对下游不可见，必须可见；
- 延迟取**主调用中位数**（含触顶行会拖高；触顶行本就最慢）。

## 3. 判据（**不改尺子**）

| 项 | 值 | 来源 |
|---|---|---|
| 锚 | `created_at >= '2026-09-18 16:00:00'` | 与 `ops/hermes/ue_truncation_readout.py` **逐字相同**（守卫见 §4） |
| 判定 | n<3 样本不足 / 触顶率 <1/3 改善 / ≥1/2 未改善 / 其余临界 | 同上（该预注册出自 9/18 一次性 job，改判据＝改尺子须用户裁） |
| 基线 | 6/9=67% | llm_audit 实测（行 id 见 §1） |

⚠ **锚点是保守的**：`created_at` 是 **UTC**（CST−8），该锚在 CST 口径 ≈ 9/19 00:00，
比修复时刻（CST 9/18 15:51）晚 8h ⇒ 漏计 9/18 20:01/21:10 两次修复后干净调用；
方向是**偏保守**（少计正例，不会把改善算过头），与既有读数脚本同源故不改。

## 4. 验证（Gate 5）

- 新增 `tests/test_daily_llm_token_report_ue_budget.py`（14 例，内存库打桩）：测试流量剔除 /
  重试臂不入分母 / 兜底单列 / 锚前剔除 / 中位数与 max_out / 四档判定（含**率恰 =1/3 判临界**
  边界）/ 渲染含 n / 未传参时旧输出逐字不变；
- **跨文件口径不漂移守卫**：断言独立脚本的锚点字面量与阈值表达式仍与日报一致（改一处不改另一处＝两把尺子）；
- 真库干跑（`--date 2026-09-23 --dry`）：新行输出 `主调用 10 次 | 触顶 0 (0%) | … 判定: 改善`；
- 等基对比：`test_daily_llm_token_report_da_direct::test_da_direct_aggregates_usage_and_cost`
  与 `test_control_loop_ledger::test_crontab_loops_are_ledgered_or_baselined` 在 HEAD 裸 worktree
  **同样红** ⇒ 非本批引入（前者成本尺口径已变，后者为他人在途环未入账）。

## 5. 未做（明确排除）

- **不换** UE 模型：Prime 是同模型加速档（能力/限制同原版），解除截断的作用面为零；用户裁
  「先跑一轮同材料 A/B（3 轮×2 模型）再定」——A/B 产物 `data/shadow/ue_prime_bakeoff_20260924.jsonl`
  + 结论另行呈报，**本 RFC 不含换档授权**；
- **不改** 7 日截断率段的窗口/口径（滚动尾巴会自行滚出，改窗口＝改既有验收尺）；
- **不动** `ops/hermes/ue_truncation_readout.py`（保留手动/交互用，锚点由守卫对齐）。