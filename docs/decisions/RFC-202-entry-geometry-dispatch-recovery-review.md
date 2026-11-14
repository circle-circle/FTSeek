# RFC-202 · 入场几何、确认后派发与恢复口径只读评估

- 日期: 2026-11-12
- 状态: 只读分析器已实施；生产交易行为未变
- 关联: RFC-123（双锚）、RFC-141（entry_confirm）、RFC-155（尺寸）、RFC-200（开盘黑窗）

## 1. 为什么只做分析，不直接改参数

2026-11-12 早盘显示，主要亏损不是费用由 9bps 或 0945 单独造成，而是：

- reprice 把 DA 的计划低吸锚推高后继续按 pullback 处理；
- entry_confirm 通过后仍等待一个完整轮询周期，产生额外价格漂移；
- 5 分钟宽限期内只保留 4% 灾难线，紧止损在宽限期结束后才武装；
- 收养仓 ATR/peak/MFE 与正常 Playbook 路径口径不一致。

这些都涉及真实入场/退出行为。根据 C4，先固定测量与反事实边界，不用单日结果调参。

## 2. 已实施

新增 `scripts/entry_geometry_review.py` 与 `tests/test_entry_geometry_review.py`：

```bash
./venv/bin/python scripts/entry_geometry_review.py \
  --date 2026-11-12 --anchor-cap-pct 2.0 \
  --out /tmp/entry_geometry_20261112.json
```

分析器只读 `trade_records`、`playbook_instructions`、`entry_confirm_forward.jsonl`，输出：

1. **reprice 漂移**：优先从 `raw_instruction_json.planned_entry` 取原始计划锚；
   缺 reprice_audit 时，effective 回退到指令当前 reprice 值，避免把原始价误当当前价。
   按方向计算 `fill vs planned/effective`，候选锚带只标 `would_anchor_skip`，不拦截。
2. **确认后派发延迟**：treatment 将 arm→confirm 与 confirm→fill 相加；
   holdout 单独计算 arm→fill，避免重复扣减 120 秒。
3. **宽限期事件**：按真实时间戳计算持仓分钟与 MAE≥2% 的事件，不虚构反事实 PnL。
4. **恢复口径**：`entry_price_source`、ATR/SL 距离、peak/MFE，识别收养仓口径偏差。

没有新增生产影子、没有新 cron、没有 DB 写入、没有订单或参数变更。

## 3. 当前证据如何使用

- DPC 的计划 33.00 → 实际成交 34.16，顺向漂移 +3.5152%，在候选 2% 锚带下被标 `would_anchor_skip`。
- GS 的计划 902.00 → 实际成交 921.685，漂移 +2.1824%，同样被标记。
- UBER 的 +0.9712% 不触发候选 2% 锚带，说明分析器不会简单把所有 reprice 都判坏。
- treatment 需把确认段和确认后排队段相加；不能与 holdout 的直接执行延迟混算。
- 收养仓 ATR/SL 距离中位数约 0.5，证实其与正常路径全距离口径不同。

这些是候选规则的识别能力，不是规则已经有效的证明。

## 4. 后续影子方案（未启用）

| 顺序 | 候选 | 当前状态 | 转正前必须回答 |
|---|---|---|---|
| S1 | reprice 锚带（1%/2%/3% 分桶） | 只标 affected | 避免多少尾损、误杀多少 GS 类赢家；n≥30 且 ≥10 ET 日 |
| S2 | 确认后立即派发 | 只测延迟 | 是否减少确认后逆向漂移；不得绕过 reprice/费用/admission/重复仓守卫 |
| S3 | 黑窗到点立即评估 | 只测轮询量化误差 | 是否减少政策外延迟；不得改变 0945 触发语义 |
| S4 | 宽限期灾难线分桶 | 只标 MAE 事件 | 是否减少 5–6 分钟边界大亏，是否增加噪声误杀；建议 n≥60 事件 |
| S5 | 收养仓 ATR/peak/MFE 修复 | 只读确认 bug | 先固定正确口径，再评估退出差异；禁止直接改完再编证据 |

S1–S4 若进入生产，必须另起行为变更 RFC、影子验证和用户批准；本 RFC 不授权启用。

## 6. R1 恢复口径修复（2026-11-13）

`app/core/position_sync.py` 新增 `_proxy_atr_from_stop()`，收养仓 ATR 改为完整
`|entry-SL|`，与 `PlaybookService._extract_atr_with_basis()` 的 proxy 分支一致，
不再使用 `|entry-SL|/2`。`tests/test_position_sync_atr_proxy.py` 钉住：

- 100/95 时 ATR=5，与 Playbook proxy 一致；
- 不得再变成 2.5；
- 无效价格为 0。

这属于恢复口径修正，不是新增止损参数。由于 early-tight 使用 0.5×ATR，旧实现
实际形成 0.25×SL 距离的紧止损，可能系统性偏紧。peak/MFE 的恢复仍需按 S5 单独处理，
本批不虚构收养前的峰值历史。

## 5. 明确禁止的误读

- `would_anchor_skip` 只是候选名单，不能说被跳过的交易一定会赚或亏。
- 缺分钟 bar 时不做完整反事实 PnL；不能把同出场价假设当成真实路径。
- 不用 DPC 单笔推翻 0945、early-tight、chase cap 或 direction guardrail。
- 不把 quality_score 直接变成新硬门；先验证前瞻 IC。
