# Entry Gate v3 · 实盘每日复盘 Log

**用途**：VWAP 方向一致性硬门（Tier1）自 2026-07-15 起在 sim 模拟盘**实盘启用**（`entry_vwap_gate_enabled=true`）。本文件按日 append 结构一致的复盘，积累成**证据链**——将来任何一次门调整（改容差 / 加 SPY 维 / 关掉）都以此为据，不靠回忆。

**生成方式**（每日收盘后一条命令，自动 append 到本文件末尾）：
```bash
cd /opt/ftseek/FTSeek-US && ./venv/bin/python scripts/entry_gate_daily_report.py --date <YYYY-MM-DD> --append-log
# 不带 --date 默认取 jsonl 中最新交易日; 不带 --append-log 只打印不写盘
```
- 数据源：`data/entry_timing_shadow.jsonl`（门特征+决策）+ `data/ftseek_us.db` 的 `playbook_instructions`（成交/PnL，按 `inst_id` join）。

---

## ⚠️ 度量边界（务必先读，否则会误读趋势）

1. **实盘模式下"被拦单"看不到反事实 PnL**：门真拦 → 不成交 → 无 PnL。所以"被拦的是不是亏损单"这个干净反事实**测不到**（那是影子模式才有的）。本 log 不假装能测它。
2. **部分补救 = "延迟入场"**：VWAP 门每 5 分钟重查，多为"暂时拦、对齐后放行"。因此很多被拦单会晚点成交 → 报告用 **立即入场 vs 延迟入场（拦后成交）** 的表现对比，作为门择时价值的间接证据。
3. **数据源 (2026-07-16 修正)**：门已从 250 笔滚动 `ticker_vwap_deviation`（语义错、依赖 push tickers 常缺失）**改用当日累计 VWAP** `ctx["vwap"]`（K 线 `cum(tp×vol)/cum(vol)`，K_1M 全订阅可靠填充）。`vwap_populated` 现反映**K 线 VWAP** 可得性，盘初 K 线不足时才 fail-open；填充率应远高于旧版。容差 `tolerance=0.003`（0.3%，噪声底）。
4. **before/after 对比有混杂**：拿门启用后成交单 vs 启用前基线，受市场 regime 日间变化混杂，只作趋势参考，非因果。

---

## 每日 Section 结构（模板，脚本自动填充）

```
## YYYY-MM-DD

**样本**: rows=N | 指令数=M | 源: universe_expansion=a / voting_llm=b / ...

### A. 门是否在工作
- vwap_populated: 行级 X/N (p%) · 指令级 Y/M 至少一次有值
- gate_block 事件: Z 次 (占 populated 行 q%)
- ⚠️/✅ 自动判定: populated 率是否足以让门真正生效

### B. 拦截行为 (按指令)
- 曾被拦 blocked_ever: B  →  拦后成交(延迟) B1 / 从未成交(反事实丢失) B2
- 全程未拦直接成交 immediate: I

### C. 成交单表现 (join playbook_instructions.pnl)
- 全部成交: n 单 · 胜率 w% · 净PnL $X · 均值 $Y
- 立即入场 vs 延迟入场: WR / 均值 PnL 对比

### D. 备注 / 异常
- (自动告警 + 人工补充)
```

---

<!-- 每日复盘从下方开始 append。首份预计 2026-07-16 收盘后（北京）生成。 -->
