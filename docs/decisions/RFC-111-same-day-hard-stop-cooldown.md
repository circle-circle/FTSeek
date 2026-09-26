# RFC-111: 同日硬止损冷却闸 (DA 主通道)

- 日期: 2026-09-26
- 状态: 已批准落地 (用户 9/26 裁「同意」, 对应 ORCL 9/24 复盘建议 #1)
- 类型: L1 功能修复 (补守卫, 复用现成 SSOT 常量, 不新建机制)
- 回滚: `playbook.same_day_hard_stop_cooldown: false` + `systemctl restart ftseek-us`

## 问题

9/24 ORCL 实证时间轴 (ET):

```
09:30:35  LONG 隔夜仓 OVERNIGHT_GAP_STOP 割肉 -$400.14
09:38:27  DA 主通道同向 BUY 重开 138.43×21   ← 零守卫
09:51:47  SL_INITIAL 再割 -$83.97
```

`intraday_reentry._HARD_STOP_REASONS` (0% WR 纯亏损/操作类出场原因集) 此前**只被
再入场通道消费** (`prior_exit_reenterable`, intraday_reentry.py:110)。DA 主通道
`run_playbook_auto_execute` 执行前不查 → 两个通道对"硬止损后能否再进"语义相反。
`same_day_reversal_block` 只禁反手 (方向相反), 不禁同向重开。

## 修法 (最小)

1. `pipeline._check_same_day_hard_stop_cooldown(symbol, direction, today)`:
   查当日该标的 playbook 自身成交 (source_type IN PLAYBOOK_SIM/PLAYBOOK_LIVE, S3 同口径)
   中同向 (BUY↔LONG / SELL_SHORT↔SHORT) 且 exit_reason ∈ `_HARD_STOP_REASONS` 的行。
   **从 intraday_reentry import 常量, 不复制清单** (SSOT, 防漂移)。
2. 接线: auto_execute 循环内, 触发确认后、B门硬门 (RFC-068) 前。命中 →
   skip_reason 回写 (`same_day_hard_stop_cooldown(<reason>,...)`) +
   exec_gate_audit 行 `action=skipped_hard_stop_cooldown` + 下轮复评
   (当日恒拦, 跨日自动解除 — 日期口径 substr(exit_time,1,10) vs ET today)。
3. fail-open: 查询异常 → 放行 + WARNING (质量门非安全门, 下游仍有 SL 兜底;
   与 RFC-068 数据缺失处置一致)。

不拦反向 (反手属 same_day_reversal_block 域); 不拦非 playbook 通道出场;
不拦良性出场 (SL_BREAKEVEN/SL_TRAIL_LOCK/TAKE_PROFIT/... = _BENIGN_REASONS 语义)。

## 历史回放 (全量 trade_records, 2026-09-26 实算)

同日同向硬止损后重开 = **6 笔**, 若本门全程生效则全部拦下:

| 日期 | 标的 | 前笔出场 | 重开 pnl_net |
|------|------|----------|-------------|
| 8/11 | TSM | TIME_EXIT | -4.24 |
| 8/11 | TSM | TIME_EXIT | -20.43 |
| 8/24 | BABA | SL_INITIAL | +15.20 |
| 9/01 | BWXT | DEAD_SIGNAL_EXIT | **+105.63** |
| 9/10 | SNDK | OVERNIGHT_GAP_STOP | +4.29 |
| 9/24 | ORCL | OVERNIGHT_GAP_STOP | -83.97 |

净效应 = **+$16.48 (n=6, 3盈3亏, 无统计显著性)**。

**诚实声明**: 本门的正当性不是历史净收益 (样本太小且混合), 而是**通道一致性** —
再入场通道自 8/07 起就禁止硬止损后再进 (0% WR 纯亏损证据 n=61 -$2320, 见
`_HARD_STOP_REASONS` 注释), DA 主通道缺同一守卫属接线遗漏而非设计差异。
主要已知代价: 会拦掉 BWXT 型"死信号出场后反转重开"的赢家 (n=1, 不作反证)。

## 验证

- 单元: `tests/test_same_day_hard_stop_cooldown.py` 10 passed
  (ORCL 复现 / 良性出场放行 / 反向放行 / 跨日放行 / 外部通道放行 /
  NULL exit_time 排除 / fail-open / SSOT 常量共享 / 接线契约 / 配置键)。
- 部署: playbook.* 键 HotReloader 穿不透 → `systemctl restart ftseek-us`
  (周六休市执行, 重启前核未平仓 3 笔 AVGO/NBIS/SNDK 不受影响 — 本门只拦新开仓)。

## 关联 (ORCL 复盘其余项, 另行裁决)

- #2 news_positive 标签映射修正 + 下游污染清理 — 影响面分析待出
- #3 亏损 pattern risk_note→扣分 — 影子先行
- #4 新闻严重度分级 (news_v2 summary) — 影子先行
- #5 黑天鹅检查复活+扩作用域 — 影子先行
- #6 anomalies 幽灵字段 — 零风险修正待排
