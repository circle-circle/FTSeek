---
title: "RFC-158 FADE_SHORT-aware VWAP 门预注册（只读，不改门）"
status: draft
module: playbook.execution_parameters
related_code: scripts/eval_vwap_gate.py, app/playbook/pipeline.py, app/playbook/pipeline_parts/trigger_flow.py
related_okr: OKR-2026-10
related_adr: ADR-035
tags: [vwap, fade-short, preregistration, shadow, ab]
updated: 2026-10-22
---

# RFC-158 FADE_SHORT-aware VWAP 门预注册（只读，不改门）

**状态**: draft / pre-registration。本 RFC **不启用**任何门变更，不占 OKR-2026-10 C1 名额。

**类型**: 只读 A/B 读数的预注册与扩样裁决规则；不新增交易行为。

**关联**: RFC-141（入场确认）、RFC-153（FADE_SHORT 开盘禁追）、
`docs/analysis/2026-10-20-left-tail-autopsy.md`、2026-10-21 门控/计划锚冲突审计。

---

## 1. 背景

2026-10-21 的冲突审计记录了一个小样本读数：`vwap_gate_ab.jsonl` 当日 gated 臂
n=9 均值 +0.315%（FADE_SHORT n=2 +1.336%），released 臂 n=11 均值 −0.313%
（FADE_SHORT n=5 −0.948%）；据此提出「FADE_SHORT 的 VWAP 方向门疑似反向」，
但明确 n<30、两组测量基不同，**不改门**，下一步做 archetype-aware A/B/影子预注册。

2026-10-22 用扩大的 5 日窗（2026-10-15..2026-10-21，`eval_vwap_gate.py --days 5
--by-archetype`）重读，结论与 10/21 小样本**不一致**：

| 组 | 口径 | n | 均值 | 中位 |
|---|---|---:|---:|---:|
| 处理组（被 wrong_side 拦掉，反事实） | 全体 | 23 | −0.346% | −0.149% |
| 处理组 | FADE_SHORT | 5 | −0.343% | +0.610% |
| 处理组 | PULLBACK_LIMIT | 17 | −0.441% | −0.268% |
| 对照组（放行，已实现） | 全体 | 26 | +0.164% | +0.265% |
| 对照组 | FADE_SHORT | 10 | −0.029% | +0.155% |
| 对照组 | PULLBACK_LIMIT | 15 | +0.273% | +0.270% |

- 总体处理组均值为负（−0.346%）→ VWAP 门在本窗内**倾向拦对（救命）**，不是扼杀。
- FADE_SHORT 处理组 n=5、均值 −0.343%；对照组 n=10、均值 −0.029%：两组都略负，
  **不支持**「门拦住赢家、放行输家」的反向假设。
- 10/21 的 n=2/5 读数属于小样本噪声，不能作为改门依据。
- 两组测量基仍不同（处理组=分钟级离散反事实；对照组=全持仓期已实现），
  **不可直接相减**；只能各自对 0 看方向。
- `plan_gap_bucket` 无单调关系：处理组 `<=-1%` −0.469% / `-1..1%` −0.695% /
  `>=1%` −0.105%；对照组 −0.029% / +0.287% / +0.246%（n 均 <10）。

## 2. 假设（读数前写死）

**H1（门反向）**: 对 `FADE_SHORT`，被 VWAP `wrong_side` 拦掉的指令，若放行，
其 forward 反事实收益**显著为正**；同时被放行的 `FADE_SHORT` 已实现收益**为负或显著低于 0**。
即门把赢家拦掉、把输家放进来。

**H0（门有效/无差异）**: 上述两侧不同时成立，或效应量低于门槛。

## 3. 测量与判据（预注册，不事后挪）

- 数据源：`data/vwap_gate_ab.jsonl`（分臂台账）+ `data/entry_timing_shadow.jsonl`
  （处理组反事实轨迹）+ `data/ftseek_us.db::trade_records`（对照组已实现）。
- 工具：`scripts/eval_vwap_gate.py --by-archetype`（只读；不改交易逻辑）。
- 样本单位：去重决策事件（symbol-day-direction-instruction），不是轮询行。
- 最小样本：**每个 archetype 每臂 n≥30**；未达则 INCONCLUSIVE，继续攒样。
- 主判据（H1 成立需同时满足）：
  1. 处理组 FADE_SHORT 均值 > 0，且日聚类 95% CI 不含 0（或 t≥2.0）；
  2. 对照组 FADE_SHORT 均值 < 0，且日聚类 95% CI 不含 0（或 t≤−2.0）；
  3. 处理组均值 − 对照组均值 ≥ +0.30pp（仅作效应量参考，不作因果相减）；
  4. 分方向复核：FADE_SHORT 结果不能由单日/单标的驱动（去掉最大单日贡献后符号不变）。
- 次级判据（不单独触发变更）：
  - `PULLBACK_LIMIT` 同向恶化（处理组均值明显 < 对照组）→ 说明问题可能是
    「均值回归 archetype vs VWAP 方向」而非 FADE_SHORT 独有；
  - `plan_gap_bucket` 分层是否单调；
  - 市场 beta 校正：FADE_SHORT 全是空头，若窗内大盘下跌，处理组正收益可能只是 beta。
    毕业前必须补一版市场/板块调整读数（可用 SPY/QQQ 同窗收益做一阶调整），
    当前脚本未做，此为已知限制。
- 裁决：
  - 满足主判据 → 另立生效 RFC（FADE_SHORT-aware bypass），附用户批准与 C1 名额评估；
  - n≥60 仍不满足 → 关闭假设，保留现有 50/50 A/B 作为常规测量；
  - 中间态 → INCONCLUSIVE，继续采样，不挪判据。

## 4. 非目标

- 本 RFC 不启用 `FADE_SHORT` bypass，不改变 `entry_vwap_gate_enabled` /
  `vwap_gate_ab_enabled`，不改变仓位/止损/入场确认。
- 不把 `PULLBACK_LIMIT` 一并豁免；不做全局 VWAP 门关闭。
- 不占 C1；不新增第二个入场假设。

## 5. 当前读数与结论

- 5 日扩样读数不支持 H1；总体门倾向拦对；FADE_SHORT 两臂都略负且 n<30。
- **结论：维持现状，不改门。** 继续 50/50 A/B；下一次复读在 FADE_SHORT 每臂
  n≥30 或 2026-11-15（先到者），按 §3 判据裁决。
- 2026-10-22 当日 CAT（FADE_SHORT）落 gated、TSLA/COIN/ARM（FADE_SHORT）落 bypass，
  是新的分臂样本；当日结果只能作为样本，不单独裁决。

## 6. 回滚/停采

- 本 RFC 为只读预注册，无代码行为变更，无需回滚。
- 停采 = 保留现有 A/B（`vwap_gate_ab_enabled: true`）即可；删本 RFC 不影响交易。
