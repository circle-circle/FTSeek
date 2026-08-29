# RFC-068: B门硬门转正 — 接飞刀拒入场 (2026-08-29)

**状态**: 已实施 (用户批准 "按建议执行")
**类型**: L1 行为变更 (<50 行)

## 背景

盘前失效影子规则 B (`premarket_invalidation.py`, record-only, 2026-08-09):
BUY 且盘前跌 ≤ -5% = 接飞刀区。影子攒样 20 天，8/28 审计中转正裁决。

## 证据 (8/10–8/28 影子 × trade_records 对账)

| 日期 | 票 | 盘前 | 结局 | 损益 |
|---|---|---|---|---|
| 8/14 | UCTT | -6.0% | SL_INITIAL | -$145 |
| 8/18 | LRCX | -5.5% | DEAD_SIGNAL_EXIT | +$3 |
| 8/20 | MRNA | -15.9% | 未成交 | — |
| 8/21 | BABA | -5.6% | SL_INITIAL | -$138 |
| 8/24 | MU | -5.5% | 未成交 | — |
| 8/28 | RBRK | -5.6% | SL_INITIAL (26分钟) | -$159 |

成交 4 笔: 3 笔止损 + 1 笔 +$3, 净 **-$439**。作用域全量核对 (130 笔有盘前对照的
BUY 成交): 硬门仅拦 3 笔 (省 $283, 误杀 $3, 净 +$280)。

样本诚实边界: 成交样本 4 笔 < 30 组门槛。转正依据是方向性全亏 (零真盈利) +
用户明示裁决, 非统计显著性。保留影子版 record-only 继续采集对照。

## 机制

- 挂载点: `run_playbook_auto_execute` — 触发判定之后、`execute_instruction` 之前
- 语义: BUY/LONG 指令执行时刻 **实时价 / 昨收 < -5%** → 拒入场; 价格回升出飞刀区,
  下轮 (30分钟) 自然复评 — 非永久作废
- 判定复用 `premarket_invalidation.evaluate_rules` (SSOT, 阈值唯一定义源)
- 数据缺失 (价/昨收≤0、取数异常) → 放行 (质量门非安全门, 下游仍有 SL 兜底)
- 拦截记入 `exec_gate_audit.jsonl` (action=skipped_knife_gate)
- 昨收口径: `get_full_market_context.prev_close` — 盘中时段为已定盘的昨收,
  不受盘前快照陈旧问题影响 (盘前陈旧案见 de13b8d0/61a94fb)

## 配置

`config/unified_config.yaml`: `playbook.premarket_invalidation_hard_gate: true`
回滚: 置 `false` + `systemctl restart ftseek-us`

## 明确不做

- 不改影子版 (observe 继续采集, 供未来阈值校准)
- 不拦 SHORT、不拦盘前跌 -5%~-2% (回调日是赢家模式)
- 不做 re-price 封顶 (执行前实弹复演证伪, 见下)

## 附: re-price 封顶证伪记录 (建议②不落地)

执行前复演 (172 笔有锚定对照的 BUY 成交):
- 成交价超盘前锚定 >1% 的真追高仅 4 笔, 且 4 笔全部盈利 (+$3/+$12/+$1/+$2)
- 8/28 全部亏损笔 (AFRM -$308 / RBRK -$159) 均在锚定价 ±0.3% 内成交,
  亏损发生在持仓段 (入场后 -5% 砸到止损), 非入场段追高
- 结论: 封顶救不了任何一笔历史亏损, 机制前提不成立, 不落地
