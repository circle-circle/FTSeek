# ADR-028: 用户三问落地 — cap 放宽 / 入场对齐实盘 / 水下仓首夜隔夜

**状态**: Accepted (用户 2026-08-15 三问裁决)
**日期**: 2026-08-15
**回滚**: 三项独立开关, 均可单行回滚 + 重启

## 变更 1 (Q1): max_daily_positions 5 → 8

**用户质疑**: sim 阶段核心是攒数据, DA/UE 每日产生不固定标的, cap 过小如何选择?

**数据裁决**:
- 全窗口(6-8月) cap 谱系反事实: cap=5 保留率 54% / cap=8 保留率 77%
  (+$4,177, 谱系最优) / cap=10 保留率 87% (+$4,028, 边际递减)。
- 选择机制本就是"按质排序" (UE 优先 → confidence 降序, pipeline.py:3160),
  cap 砍的是尾部弱信号, 非随机砍。
- 被砍候选的影子结局 (shadow_pnl) 由 shadow_tracker 照记, 攒数据不丢
  (8月验证: 未 executed 指令 shadow_pnl 覆盖率 100%)。
- 齐射防护不依赖日帽: 单轮 cap=3 (max_new_positions_per_cycle) + 水坝梯度
  (300s/轮) 独立承担; 敞口由 daily_notional_cap_pct(0.8) + 单名帽 0.08 兜底。

**结论**: 5 是止血值, 攒数据期 8 兼顾样本量与尾部弱信号过滤。

## 变更 2 (Q2): entry_open_blackout_hhmm 1015 → 0935

**用户裁**: sim 核心是打磨链路和基建, 交易时间对齐实盘标准 (开盘后 5 分钟),
不再延后。auto_execute 本已内置开盘 5min 延迟 (9:30+5), 本值收口触发门同刻。

**风险声明**: 9:30-10:15 窗历史期望为负 (7/1 起 n=181 净 -$1,111)。此为
**基建保真裁决** (链路行为与实盘一致才能打磨基建), 非 PnL 最优选择。
8/12 的 1015 水坝修复依据 (终点后移) 被本裁决覆盖, 但单轮 cap=3 梯度入场
仍在, 齐射不再瞬时倾泻。

## 变更 3 (Q3): loser_overnight_enabled — SIM 水下仓首夜隔夜豁免

**用户裁**: 盈利落袋为安 (PRE_CLOSE 盈利平仓行为不变), 未盈利继续隔夜;
SL/TP 按标的历史波动分桶设定, 保留空间。

**方向性冲突说明**: 现有 cohort B 隔夜实验是"盈利仓过夜", 与用户意图相反;
本 ADR 实现的是"水下仓过夜", 不复用 cohort B 路径。

**回放裁决** (不拍脑袋, scripts/overnight_loser_replay.py):
EOD 水下仓继续隔夜 vs 当日 15:52 平仓, n=15 (6/1 起):
- V1 次日开盘平 +$68 / V2 次日收盘平 +$593 / V3 smart +$668
- 修复率 53-67%, 三变体同向为正。样本小为已知局限, sim 期攒真实隔夜数据续验。

**SL/TP 不拍脑袋 = 现状已满足**: vol_buckets 四桶 × per-symbol ATR 驱动
(instruction_generator.py:191, sl_pct = clamp(ATR% × multiplier, floor, ceil)),
实证 8/11-8/14 指令 SL 距 2.44%~5.91% 随波动率分布。不改动。

**实现** (pipeline.py):
- `_loser_overnight_exempt()`: 配置开关 + 仅 PLAYBOOK_SIM + 水下 + 首夜四条件,
  异常→不豁免 (保守)。
- 接线两处: PRE_CLOSE 分支 (规则4) + EOD 兜底清扫 (双路豁免, 防绕过)。
- 隔夜风险兜底: initial SL 硬兜底 (SIM 循环灾难保护, 优先级在规则链之前)。
- 生命周期: 首夜检查自动失效 → 次日 PRE_CLOSE/EOD 正常收, 最长两日, 无扛单路径。

## 验证

- py_compile OK; YAML 解析 OK; PlaybookConfig 读值: cap=8 / blackout=0935 /
  loser_overnight=True。
- tests/test_playbook_pipeline.py + tests/test_patient_exit.py: 44 passed。
- 生效方式: PlaybookConfig 启动快照单例 → systemctl restart (周六休市零持仓窗口)。
