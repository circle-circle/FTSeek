# DEB-20260918 · UE 推送读数对齐真实链路 (用户令「动」)

> 触发: 用户贴 2026-09-18 21:11 CST 的 UE 扫描推送 (`llm_universe_scan.py push`) 并问
> 「请检查这次 ue 的调用和结果是否正常」。我方按 UE 首考清单逐项实测 (scan jsonl / 拒绝归因
> jsonl / llm_audit / playbook_instructions / 代码锚点) 后给出「调用正常、结果 0 条可执行、
> 推送文案两处失真」的结论 + P2/P3 分档建议, 用户回「动」= 按建议落地 P2-1/P2-2 + P3 登记。

## 1. 核实结论 (全部实测, 判定与建议依据)

**A. 调用侧正常 (无异常项)**

| 项 | 实测值 | 判定 |
|---|---|---|
| 两窗 | da_feed `2026-09-18T08:01:14-04:00` / premarket `09:11:00-04:00`, 各 surfaced 6 | ✅ |
| 归因戳 | `model_used=glm-5.3` `prompt_version=v2.13` `prompt_hash=70e4680597af` `brief_template_version=ue_brief_v4` | ✅ 与代码常量 `PROMPT_VERSION`(:164) 一致 |
| 契约 | `contract_stats={violations: [], repaired:0, still_bad:0, clean:true}` | ✅ 零违约 |
| LLM 实弹 | `llm_calls` id 11970 / 11971: `success=1 is_test=0 error_msg=''`, tokens 3192→5280 (52.4s) / 4443→3637 (36.1s) | ✅ 无 fallback、无截断 |
| 拒绝归因 | 6 只浮现 → filter 5 + build 1 + batch_decision 1 = **6/6 全覆盖可归因** | ✅ |
| R5 快照 | `signal_snapshots` 当日 UBER/ZIM 两行 `strategy_id=universe_expansion` | ✅ 与日志 2/2 一致 |

注: 日志 `[scan] prompt_version=v2 (config: universe_expansion.prompt_version)` 的 `v2` 是
**v1/v2 prompt 族选择器** (`:2702-2706`), 与归因版号 `v2.13` 不同轴 — 非矛盾, 不改。

**B. 结果侧: 两条 LONG 全部被下游闸拦, 当日真注入 = 0 条**

| 标的 | 死因 | 性质 |
|---|---|---|
| UBER | `streak_gate(3>2)` — 9/16→9/17→9/18 连续 3 日浮现 vs `max_emergence_streak=2` | 设计内 (OOS: streak≥3 桶前瞻 −0.28%); 反事实已登记 `ue_streak_counterfactual.jsonl` (当日 n_blocked=1) |
| ZIM | `low_atr(0.0262<0.03)` — yaml `playbook.execution_parameters.low_atr_gate_min_atr_pct=0.03`, ATR 日线自算 (`atr_reason=ok`, bars_asof=9/17) | 设计内 |

**C. 三处失真/缺口 (P2×2 + P3)**

| # | 病灶 | 实测证据 |
|---|---|---|
| P2-1 | 推送「已注入DB→执行轨道 (实际N条)」计数只按 `decision_id LIKE 'universe_expansion:%'`, **把 SKIP 行算成已注入** | 当日该前缀仅 **1 行 = US.ZIM 的 SKIP 行** (`id=2985`), 真落库可交易指令 = **0 行** (`direction IN ('BUY','SELL_SHORT')`) |
| P2-2 | 「可交易: N只」取 `surfaced` 的 LONG/SHORT = **扫描期口径**, 在 streak/ATR 闸**之前**, 无下游门提示 | 当日显示「可交易 2 只」而两只全部被拦, 执行面 0 |
| P3 | 池外/**传导型**候选不进输入侧资格预筛 | `_prescreen_input_symbols` 只覆盖 `named_in_materials` (当日 56 具名 → 16 tradeable / 31 context_only); ZIM `evidence_citation_state=transmission_assumed` (Reuters 航运稿不含 "ZIM"), 从未过 ATR/价格地板判定 → 直到 inject 才被 ATR 闸拦 |

另 (噪声, 未改): `[warn] cooldown 未生效: US.ZIM 方向 'SKIP' 无法映射到成交词表` — cooldown
映射只认成交词表 (BUY/SELL_SHORT), SKIP 行本就不该登记冷却, 属良性; 但每日 WARN 会被当异常扫。

## 2. 落地内容 (P2-1 + P2-2, 测量层)

文件: `scripts/llm_universe_scan.py` (仅 `cmd_push` 计数 SQL + 文案 + 新增纯读助手)

- **计数收窄**: `SELECT COUNT(*) ... AND direction IN ('BUY','SELL_SHORT')`
  — 与执行侧白名单同源 (`app/pipeline/llm_pipeline.py:833`)。
  ⚠ **只按方向收窄, 不过滤 `skip_reason`**: 9/14 实测 BUY/SELL_SHORT 行上的非空 skip_reason
  是**注入后**状态 (`no_trigger` / `chase_cap_enforced` / `thesis_drift_invalidate` /
  `TRIPWIRE_INVALIDATED`), 它们是真注入过的指令; 加 skip_reason 过滤会把 9/14 的
  3 条误报成 1 条 (本人初版即此错, 实施期 DB 对照后自纠)。
- **文案**: 计数 0 时改 `注入0条 (候选N只未过执行闸)` (原措辞「被现价缺失/排重/冷却等过滤」
  是凭空列举, 与当日真因 streak/ATR 不符)。
- **标签**: `可交易:` → `扫描期可交易:` (显式声明它在执行闸之前)。
- **新增行**: `执行闸: 拦N只 (按原因分桶)` — 新助手 `_gated_breakdown()` 只读当日
  `data/shadow/llm_universe_inject.jsonl` 的 `filter/build/save` 行, 原因前缀中文化
  (`_REJECT_LABELS`), `direction(SKIP)` 桶略去 (上一行 `SKIP{n}` 已表达), 空则不出行。
  纯读落盘产物, 不重算判据、不落盘、不改任何门。

## 3. 验证 (Gate 5)

- `py_compile` 通过; UE 相关测试 5 文件 **166 passed** (`test_ue_streak_gate` /
  `test_ue_low_atr_precheck` / `test_ue_contract_validator` / `test_ue_evidence_index` /
  `test_ue_evidence_citation`)。
- **实弹 `push --dry` 三日对照** (读真产物, 不推送) — 新旧读数差与算术闭合:
  | 日期 | 推送读数 (新) | 独立 SQL 复算 | 闭合 |
  |---|---|---|---|
  | 09-18 | 注入0条 / 扫描期可交易2 / 执行闸拦2 (streak闸1 / 低ATR闸1) | 0 / 2 | 0+2=2 ✅ |
  | 09-15 | 注入4条 / 扫描期可交易7 / 执行闸拦3 (已写库2 / 低置信1) | 4 / 7 | 4+3=7 ✅ |
  | 09-14 | 注入3条 / 扫描期可交易6 / 执行闸拦3 (已写库2 / 低ATR闸1) | 3 / 6 | 3+3=6 ✅ |
  | 09-17 | 注入0条 / 扫描期可交易3 / 执行闸拦3 (冷却1 / 已写库1 / 已持仓1) | 0 / 3 | 0+3=3 ✅ |
  (09-14 旧口径会显示「实际4条」= 含 1 条 SKIP 行; 09-17 旧口径显示「实际0条」但无分解)
- 无测试断言该文案 (`grep 'Universe选股扫描完成' tests/` = 空) ⇒ 无需改测试。

## 4. 回滚

- 代码: `git revert <本提交>` (单文件 + 本文档)。回滚后即回到原 SQL 计数与原标签,
  无数据/状态残留 (本函数只读 DB + 推送文案)。
- 生效时点: `llm_universe_scan.py` 由 crontab 直接 exec (无进程缓存, 且**无 `/root/.hermes/scripts/`
  镜像副本** — 实查仅有 `ue_glm53_first_run_verify.py` / `ue_truncation_readout.py` 两个无关脚本)
  ⇒ **下一交易日 21:10 即生效, 主服务无需重启**。

## 5. 未做 / 待裁

| # | 项 | 处置建议 |
|---|---|---|
| 1 | P3: 池外传导型 (`transmission_assumed`, 材料未具名) 候选不过输入侧资格预筛 → 可被报作「可交易」再到 inject 才拦 (今日 ZIM 即此型) | 两选一: ①对未具名但被模型浮现的符号补一次 `screen_candidates` 三态; ②推送/permit 层标注「池外未预筛」。属设计缺口, 需用户裁 |
| 2 | `cooldown 未生效 ... 'SKIP' 无法映射到成交词表` 每日 WARN | 建议降为 DEBUG 或改写措辞 (「SKIP 行不登记冷却」), 避免日志噪声被当异常扫; 待裁 |
| 3 | 观察项 (非缺陷): 09-17 两窗 glm-5.3 均 `finish_reason=length` (tokens_out 24000/16000) → `downshift_retry` 交 `deepseek-v4.1-flash` 作答, 故 scan 记录 `model_used=deepseek-v4.1-flash`; 09-18 未触发下移 (3637/5280 正常收尾) | 已有 cron `96c8e404f55a` (9/18 21:45 思考预算后验) 覆盖, 本批不动 |