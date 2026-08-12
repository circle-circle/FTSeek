# ADR-022: 再入场方向顺势裁决 — 不再硬继承前笔方向 (8/12 用户裁决直接落地)

- 状态: Accepted
- 日期: 2026-08-12
- 关联: skill ftseek-intraday-reentry 坑#2 (8/7 盲翻审计), ADR-020 (入场侧参数)

## 背景

盘中再入场生成器 (intraday_reentry.py) 方向硬继承前笔:
`direction = "BUY" if prior.direction in (LONG/BUY) else "SELL_SHORT"`。

近30天实证偏斜: DA指令 BUY:SELL_SHORT = 307:49 (6.3:1), 成交 LONG 179笔净 -$1277
为主要亏损源, SHORT 27笔净 -$206。用户裁决 (8/12): 做多做空都需要机会, 不应持续偏;
且确认门 (vwap_band / slope_5m) 本已双向对称 (evaluate_candidate SHORT 分支完备),
仅方向选择层缺电。

**历史证据披露**: 8/7 审计实测"盲翻/盲继承"对平仓后 1h 方向无区分力
(同向 55.4% vs 反向 44.6%, n=83, ≈随机)。本改动与之不同: 非盲翻, 方向裁决后
仍过 evaluate_candidate 双向对称确认门 — 方向错则门拒。slope 符号本身 alpha
未验证, 由 sim 成交直接检验 (sim 采集期数据量优先, 用户明确否决影子对照)。

## 决策

新增纯函数 `choose_direction(prior_direction, slope_5m, adaptive_enabled)`:

- slope_5m > 0 → BUY (动量向上顺势做多)
- slope_5m < 0 → SELL_SHORT (动量向下顺势做空)
- slope 缺失/==0 → 继承前笔方向 (与改动前行为完全一致, fail-safe)
- 配置键 `playbook.intraday_reentry.direction_adaptive_enabled` 默认 true

审计: shadow 行落 `direction_mode` (adaptive/inherited) + `prior_direction`,
T+1 eval 可按 adaptive 标记分组归因。thesis 文案按方向分流。

## 下游安全验证 (改动前 grep+读码)

- `_arm_instruction`: SL/TP 按方向镜像, 方向无关结构完好
- pipeline reprice: intraday_reentry- 前缀指令走 quick_sl_tp_pct (方向无关同式);
  SL 钳制分支 SHORT 含 short_widen
- DA SELL_SHORT 指令走同一 13 门链, 近30天 26 笔成交实证
- 隔夜豁免/db 归因按 decision_id 前缀判定, 不依赖方向
- thesis 文案无下游解析依赖 (grep 零匹配)

## 回滚

`playbook.intraday_reentry.direction_adaptive_enabled: false` + 重启 (旧行为),
或 git revert。

## 验证

choose_direction 单测 3 个 (翻向/同向/回退) + tests/test_intraday_reentry.py
36 passed + 全套 1129 passed (40.2s)。
