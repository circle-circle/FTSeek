# ADR-016: Universe-Scan Live Prompt 切换 V2 (config 驱动, 富途模拟盘)

- 状态: Accepted
- 日期: 2026-07-14
- 关联: ADR-012 (deprecate legacy battle plan), scan-eval / eval-report (`scripts/llm_universe_scan.py`)

## 背景

`universe_expansion` 选股宇宙扩张策略的 live 链路已开通并在富途**模拟盘**运行:
`cmd_scan → cmd_inject → playbook_instructions → fast_lane → futu_simulate 下单`
(`universe_expansion.entry_enabled=true`, `TRADE_ENV=1`, `execute_mode=futu_simulate`)。

此前 live `cmd_scan` 硬编码使用 `SYSTEM_PROMPT` (V1)。scan-eval 的 A/B 影子数据
(2026-07-14, 4 次) 显示 V1 质量明显弱于 V2:

| 指标 | V1 (原 live) | V2 |
|---|---|---|
| 浮现数量 | 8–10 只 | 2 只 (克制比 0.25) |
| 引证率 evidence_ref | 0% | 100% |
| tier 分布 | 无 | HARD 0 / MARKET 8 / THESIS 0 |
| 稳定性 Jaccard | 0.51 (每跑换股) | 1.00 (完全稳定) |

V2 稳定浮现的 `EOG LONG` / `ORCL SHORT` 均锚定真实市场佐证 (XLE Energy +3.01% 轮入 /
XLK Tech −2.42% 轮出), 与 `deep_sector_rotation.json` 一致。V1 每次换一批无据标的。

## 决策

将 live `cmd_scan` 使用的 prompt 改为 **config 驱动**, 不再硬编码:

- 新增 `universe_expansion.prompt_version` (取值 `v1` | `v2`)。
- 代码默认 `v1` (安全回退); yaml 显式设 `v2` 启用。
- `v2` 路径同时开启 `_normalize_surfaced(..., with_tier=True)` —— 解析 evidence_ref/
  self_tier 并执行 Track A 夹逼 (无证据 → THESIS_ONLY 且 confidence≤0.60)。
- scan 记录落 `prompt_version` 字段供归因。

首次上线值: `prompt_version: v2`。

## 影响范围

- **仅影响** `universe_expansion` 策略浮现的标的集合与置信度; 经既有 `min_confidence`/
  `max_targets_per_day` 闸门后进富途模拟盘。**不碰下单/退出链路本身**。
- 仅模拟盘 (`force_simulate=true`); 无真实资金风险。
- 完全可回退: `prompt_version: v1` 即恢复原行为, 无需改代码。
- scan-eval / eval-report 影子对比不受影响 (V1/V2 A/B 仍独立运行)。

## 验证

- `py_compile scripts/llm_universe_scan.py` 通过。
- 下一次调度 scan 日志将打印 `[scan] prompt_version=v2`, 落库记录含 `prompt_version`。
- 后续观察富途模拟盘 V2 成交/退出质量, 再决定是否给 clue brief 补硬事件源
  (当前 HARD_EVENT tier 为 0, 因 clue 无硬催化输入)。

## 回退

编辑 `config/unified_config.yaml` → `universe_expansion.prompt_version: v1`, 重启服务。
