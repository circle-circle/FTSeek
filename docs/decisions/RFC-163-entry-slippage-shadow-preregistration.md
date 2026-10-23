---
title: RFC-163 入场逆向滑点影子预注册
status: rejected
module: playbook/execution
related_code: app/playbook/pipeline_parts/auto_execute_flow.py, scripts/execution_tca_shadow.py
related_okr: OKR-2026-11
tags: [execution, slippage, shadow, preregistration]
updated: 2026-10-23
---

# RFC-163: 入场逆向滑点影子预注册 (execution slippage shadow, record-only)

> **2026-10-23 用户裁：暂不立项**；本文仅留档，未加代码/cron/字段。

- 状态: **已决定：暂不立项（用户 2026-10-23 同意建议）；留档，不加代码/cron/字段**
- 日期: 2026-10-23
- 作者: Qoder (Bot)
- 审批人: 用户（pending）
- 关联: RFC-152（执行 TCA 台账）、判据字典 C9/C12/C13、OKR-2026-11 草案 KR3
- 回滚: 删除本批新增 cron/脚本/字段；零交易行为变更；若触及 `playbook.*` 键则重启

## 0. 一句话

历史数据提示“入场逆向滑点 ≥10bps 的成交期望为负”，但该证据**不足以批准 live 门**：剂量反应非单调、中位近零、亏损由 10 笔尾损主导、102 行缺字段、10bps 恰在日聚类 t 峰值。本 RFC 只申请一个**前瞻、record-only、阈值冻结**的影子，同时采集“可执行的前置指标”与“结局”，再由独立 DEB 裁决是否接 live。

## 1. 历史证据（仅作假设生成，不进前瞻判据）

口径：PLAYBOOK_SIM，2026-07-01→2026-10-22，`exit_price>0`，A9 剔除首跑测试批（4 行，id 1521/1522/1524/1526），净额=`coalesce(pnl_net, realized_pnl)`。n=685，毛 +8.74bps，费 -10.12bps，净 **-1.38bps**。

滑点 = `metadata_json.execution.slippage_bps`（fill vs arrival，方向归一）。已用 587 行 `arrival_price`/`fill_price` 独立复算，0 笔偏差 >0.02bps；正=逆向滑点，bps 量纲。

| 分带 | n | 净额 | 笔均 | 中位 |
|---|---:|---:|---:|---:|
| <0（有利） | 237 | +$15 | +$0.06 | — |
| 0–5 | 201 | -$523 | -$2.60 | +$5.66 |
| **5–10** | **92** | **+$1,423** | **+$15.47** | **+$6.02** |
| 10–15 | 27 | -$1,103 | -$40.86 | -$1.17 |
| 15–20 | 12 | -$872 | -$72.67 | -$2.54 |
| 20–25 | 10 | -$362 | -$36.25 | -$1.80 |
| 25–30 | 3 | +$94 | +$31.48 | — |
| ≥30 | 5 | -$71 | -$14.26 | — |

- ≥10bps 合计 n=57，净 **-$2,314.56**，笔均 -$40.61，**中位 -$1.29**；剔最大 3 笔（ORCL -$400.10 / RIVN -$319.60 / MU -$308.90）后 **-$1,285.99**；4/4 个月为负（-233.87 / -624.84 / -1,004.93 / -450.92）；全期日聚类 t≈-2.99（33 日）。
- 亏损集中于 10 笔尾损：6 笔 `SL_INITIAL` -$1,105.18 + 4 笔 `OVERNIGHT_GAP_STOP` -$1,280.77；同组 `SL_BREAKEVEN` 24 笔仅 -$80.22，`SL_TRAIL_LOCK` 12 笔 +$392.98。
- 52/57 发生在 09:xx，49 笔 LONG；可能只是“开盘快票”代理，不是因果。
- 102/689 原始行（A9 干净口径 98/685）没有 execution 元数据（净合计 +$294，含 26 笔死单 -$1,508.87）。

**判读**：方向有一致性，但 5–10bps 反而是最佳带；中位近零；尾损主导；阈值 10bps 恰在 t 峰值。按军规只能作假设，不能作 live 依据。

## 2. 同轴撞车检查

| 既有物件 | 关系 | 本影子增量 |
|---|---|---|
| `execution_tca_spread_shadow`（RFC-152） | 记录点差/报价龄/planned→exec | 本影子加 **逆向滑点→结局** 的反事实结算；不重复点差标签 |
| `chase_cap_enforce`（2026-09-04 P0-A） | 指令级禁追线 | 本影子必须记录“现有 chase_cap/entry_confirm/vwap 是否已会拦截”，只测增量 |
| `fee_gate_shadow` | 费用/毛边际比 | 不同轴（费用地板 vs 执行滑点） |
| `gap_chase_gate`（B1）/ `limit_entry`（B8 KILLED） | 缺口/限价 | 不同轴 |
| C9 | 禁止改 entry 定价/触发门使其成交 | 本提案方向相反（拒绝追价），不踩 |
| C12 | 禁止用分析质量字段降死单 | 滑点不在六字段内，不踩 |
| C13 | 盘前延续轴 | RTH 执行滑点，不同轴 |

## 3. 影子设计（record-only）

### 3.1 人群

- 主判：RTH PLAYBOOK_SIM entry，A9 剔除首跑批，走到下单调用的指令。
- 分层：`open`（09:35–09:45）/ `mid`（09:45–10:30）/ `late`（≥10:30）；`source`=DA/UE/intraday/other；EXT 单独报告，不进主判。
- 排除：净额为空、无 arrival、无 fill 的行；排除行数与原因必须落报告。

### 3.2 两个指标（关键）

- `pre_trade_adverse_bps`：**决策时刻**可成交价 vs arrival，方向归一（LONG: ask−arrival；SHORT: arrival−bid）。这是未来 live 门**唯一可用**的前置输入。
- `realized_slippage_bps`：fill vs arrival（历史口径，用于验证）。
- **有效性门**：若两指标 Spearman<0.5，或 `pre_trade` 覆盖率<95%，判 `INVALID_FOR_LIVE`——不得转 live，只留研究。

### 3.3 每行字段

`date, ts_et, inst_id, trade_id, symbol, direction, source, archetype, session_min, arrival_price, pre_trade_exec_price, fill_price, spread_bps, quote_age_ms, book_is_proxy, atr_pct, notional, would_block_10bps, existing_gates{chase_cap,entry_confirm,vwap}, pnl_net, exit_reason, mfe_pct, mae_pct, exclusion_reason`

### 3.4 结算

每日 cron 把影子行 join `trade_records`（优先 `trade_id`，其次 `inst_id+entry_time`），落 `data/execution_slippage_shadow.jsonl` 与 `_latest.json`。blocked 反事实 = $0（不交易），kept = 实际净额；费/滑点两边抵消，不做二次扣费。

### 3.5 零行为变更

不拒单、不改价、不改 size、不改退出；只记录。不得以影子标签触发任何门。

## 4. 预注册判据（读数前写死；改=重裁）

1. **样本门**：blocked n≥30 出描述性读数；n≥60 才可裁决（A1）。窗内至少 20 个交易日。
2. **主判据**（全部满足才提请 DEB）：
   - blocked 组笔均净额 <0；
   - 日聚类 t≤-2（80% CI 不含 0）；
   - **中位净额 <0**；
   - **剔最大 3 笔后笔均 <0**；
   - kept 组净额不劣于同期基线；
   - 总反事实净差 >0。
3. **单调性**：前瞻窗内 ≥10bps 组必须差于 [5,10) 组；若不成立，判 FAIL（即使 ≥10 组为负）。
4. **误杀**：记录 would-block 赢家数与 Σ；若 Σ赢家 > Σ输家，或误杀率>40%，判 FAIL。
5. **覆盖率**：RTH entry 的 `pre_trade` 与 `realized` 覆盖率均≥95%，否则 `INVALID_FOR_LIVE`。
6. **MDE**：必须报日聚类 80% CI 与 MDE80；观测效应 <MDE 只能写“不可分辨”，不得写“证伪”。
7. **阈值冻结**：10bps 为唯一主阈值；15bps 只作敏感性披露，不参与择优；5bps 不测（已知 5–10bps 为最佳带，避免多重比较）。
8. **安全停**：若 would-block 占流量>20%，暂停影子并呈报（防止门误伤面过大）。
9. **live 门**：影子不自动生效；任何 live 改动须另立 DEB + 用户签字 + 新 cohort。

## 5. 反证据与边界（必须并陈）

- 5–10bps 是最佳带（+$1,423，t=+2.32）；“滑点越大越差”不成立。
- ≥10bps 组中位仅 -$1.29；亏损集中于 10 笔尾损，可能是“开盘快票”代理而非因果。
- 102 行缺字段，门会 fail-open。
- SIM fill 含模拟成分（`engine.py:180-208`）；primary 分析剔 `book_is_proxy=true`，另报含代理口径。
- 现有 chase_cap 可能已覆盖部分人群；影子必须报“增量拦截”。
- 历史 57 笔为 in-sample 假设生成；前瞻样本自影子上线日起算，不复用历史行做判据。

## 6. 实现与验证

- 在 `app/playbook/pipeline_parts/auto_execute_flow.py` 下单调用的决策点加记录（不阻断）；复用 `execution_tca` 的 arrival/quote 读取，不新建行情层。
- 新增 `scripts/settle_execution_slippage_shadow.py`；cron `15 6 * * 2-6`（ET 6:15；CST 需按冬令时复核）。
- 新增 `tests/playbook/test_execution_slippage_shadow.py`：LONG/SHORT 符号、pre/realized 双指标、缺字段不猜、would_block、existing_gates 增量、结算 join 幂等；≥6 例。
- 只读核验：`PYTHONPATH=. ./venv/bin/python scripts/settle_execution_slippage_shadow.py --dry-run`。
- 若仅新增脚本/cron，无需重启；若改 pipeline 代码，按军规重启 + `startup_smoke_check.py --wait 90`。

## 7. 回滚

- 删 cron 行 + 删脚本/字段；数据保留；零交易行为变更。
- 若已接 live（本 RFC 不授权），回滚 = `playbook.execution_slippage_gate_enabled=false` + 重启；本 RFC 不创建该键。

## 8. 预期时间线（诚实披露）

- 历史 blocked 率 8.3%（57/689），约 0.7 笔/交易日。
- n=30：约 43 个交易日；n=60：约 85 个交易日。
- 因此本影子**不是** 11 月的盈利加速器；它是为 12 月/明年的执行门提供可裁决证据。11 月的实际路径 = OKR-2026-11 的 DA-only 转正 + RFC-160 MFE 轨 + 治理收缩（本影子未立项）。

## 9. 审批（2026-10-23 已决）

- [x] 用户审议：**暂不立项**（同意建议；不接 live、不加影子）
- [x] 本 RFC 仅留档；未加字段、未加 cron、未改代码
- [ ] 若未来重开：须重新预注册阈值/样本门/误杀口径，并先补字段覆盖率

## 10. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-23 | 初版 | 用户要求起草滑点影子预注册；基于 A9 干净口径与独立复核（glm-verifier / kimi-reviewer） |
| 2026-10-23 | 用户裁暂不立项 | 用户同意“证据不足、暂不立项”的建议；本 RFC 留档，不产生任何代码/cron/字段 |
