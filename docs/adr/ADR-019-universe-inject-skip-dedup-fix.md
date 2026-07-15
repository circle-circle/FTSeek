# ADR-019: universe inject 排重排除 SKIP 行

- 状态: Accepted
- 日期: 2026-07-14
- 相关: ADR-016/017/018 (universe_expansion live 链路), pipeline SKIP 语义

## 背景

`cmd_inject` 写 `playbook_instructions` 前会排重「今日已在库的标的」，规则是
「DA 优先，universe_expansion 让路」。实现读 `PlaybookDB.get_instructions_by_date(today)`
把所有 symbol 纳入 `existing_pb`。

问题（证据）：
- `get_instructions_by_date` (db.py L201-205) 是 `SELECT * ... WHERE date=?`，**不过滤 SKIP**
  （同文件 L251/L641 其它查询都带 `direction != 'SKIP'`）。
- 全表 direction 分布：**SKIP 163 / BUY 112 / SELL_SHORT 79** — SKIP 是最大类。
  SKIP 行是「被投票/DA 拒绝」的候选（如 "投票中性无方向"、"confidence 60 < 70"、
  "价格偏离过大"），并非实际占位指令。
- 后果：universe 排重把这些**被拒**标的也当成「已占位」，连带封杀 universe 浮现同一标的。

量化（2026-07-08 SKIP 密集日）：旧逻辑封杀 **43** 只，其中仅 **20** 只是实际
BUY/SELL_SHORT 占位；**23** 只（AAPL/AMD/NVDA/GOOG/MSFT/WMT/XOM…）只是被 DA/投票
SKIP 却被误挡，universe 当日无法浮现它们。

## 决策

`cmd_inject` 构建 `existing_pb` 时跳过 `direction == 'SKIP'` 的行，只对**实际占位**
（BUY/SELL_SHORT）的指令让路。

```python
for _inst in PlaybookDB().get_instructions_by_date(today):
    if str(_inst.get("direction", "")).upper() == "SKIP":
        continue
    _s = str(_inst.get("symbol", "")).replace("US.", "").upper()
    if _s:
        existing_pb.add(_s)
```

## 影响范围

- 仅改 `scripts/llm_universe_scan.py` cmd_inject 排重集合构建，不碰 DA、投票、执行、退出链路。
- 效果：universe 只对 DA **真正下单占位**的标的让路；对 DA **拒绝(SKIP)** 的标的恢复浮现权。
- "DA 优先" 语义更准确：优先级只应对实际占位生效，不应把拒绝也算占位。

## 预期与边界 (诚实)

- 这是 correctness 修复，非行为放大：universe 仍受 min_confidence(0.70)、max_targets(5)、
  持仓排除、entry_zone 有效性等门约束。
- 不改变「落库≠成交」：解封的标的仍需过盘中突破触发 + fast_lane±3% + PDT/敞口门才成交。
- 若未来 DA 与 universe 对同一被拒标的产生分歧，由各自 confidence 门独立把关，互不掩盖。

## 验证

- py_compile ✅
- 2026-07-08 排重集合对照：before(含SKIP)=43 → after(排除SKIP)=20，解封 23 只 ✅
- SKIP 行本身仍保留在 DB（审计/复盘用），仅不参与 universe 排重。

## 回退

还原 cmd_inject 排重循环（删去 SKIP 跳过分支）即可，单函数级、秒级可逆。
