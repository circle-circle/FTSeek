# ADR-035: 指令-触发语义对齐 (entry_archetype 结构化字段) — 影子先行

**日期**: 2026-09-01
**状态**: 已采纳 (用户裁决 "批准选项1", 承接 DEB-20260831 SL_INITIAL/mfe=0 溯源 →
DEB-20260901 立案, 选项 1/2/3 见 `docs/debates/DEB-20260901-entry-archetype-alignment.md` §九)
**关联**: DEB-20260831 (溯源实锤) / ADR-033 (影子先行同款纪律) / C1 退出参数冻结至 9/30

## 背景

8/31 盘后全链复盘实锤 **指令-触发语义倒挂**: DA 侧 thesis 写"低吸限价单"
(确认位/分批/禁止抢筹), 但执行层只认宽区间一次市价 — re-price 重锚定 +
方向穿越, 低吸单被跑成市价追单。三案例: NOW 实盘成交 $145.09 比指令确认位
$140.50 高 **+3.26%**; AVGO/AFRM 同型。条件语义 100% 是装饰文本。

独立复核 390 笔 (6/27–8/31 回填): conf=0.7 LONG 胜率 60.2% 但净 **-286**
(赢 +49.2 / 亏 -81.4) → 胜率非瓶颈, 左尾是; 开盘窗口高开 0-4% 是唯一正期望
单元 (+594), 平开/低开多头 -1092、追 4%+ 缺口 -479。既有证据: 确认位重放
5 笔 -1412→-796 (减亏 44%); 锚限价影子 5 分钟 TTL Δ+416.8/108 对。

## 决策 (选项 1: 结构化字段 + 影子先行, 零行为变更)

**Phase A — 语义结构化 (DA 侧, 仓外 skills 目录, 备份 .bak-deb20260901)**:
execution_plan schema 增 `entry_archetype` / `confirmation` / `chase_cap_price`,
adapter 透传, pipeline `_derive_entry_semantics()` LLM 优先 + 文本确定性兜底。
8/31 全 7 候选解析实测正确 (NOW=140.5 / AVGO cap=370 / BABA cap=118 /
EIX=FADE_SHORT 63.27 / BWXT 试多句式 / AFRM=83.5)。FTSeek 侧 Instruction
dataclass +3 字段, bridge→vote→Instruction→raw_instruction_json 全链透传落库。

**Phase B — 影子 (record-only, 本 ADR 启用的唯一开关)**:
- `playbook.pullback_limit_shadow_enabled: true` — 带 entry_archetype 的指令
  触发时落 `data/shadow/pullback_limit_shadow.jsonl` (trigger/chase_cap_violation),
  re-price 越过 chase_cap 记违例遥测。**不改任何执行路径**。
- `scripts/cf_pullback_limit_replay.py` (cron 47 5 * * 2-6): 次日重放判
  would-fill / CF-PnL, 对齐确认位反事实尺子。
- `scripts/gap_alignment_shadow.py` (cron 52 5 * * 2-6): 开盘缺口对齐影子门,
  已回填 6/27–8/31 共 390 笔。
- `scripts/limit_entry_shadow_replay.py`: 哨兵两侧同按 (inst_id,日) 去重,
  修复重启重复根因 → reconciliation_ok 由 false 转 true。

**预注册判据 (四阶段影子, 全过才谈执行层接线)**: n≥20 / 期望改善≥30% /
成交率≥50% / mfe=0 占比降, 三条全过。首批影子判读 2026-09-02 05:47。

## 回滚

`pullback_limit_shadow_enabled: false` + 重启 (影子停采, 字段透传无害留存);
彻底回退 = revert 本提交 + 仓外 skills 从 .bak-deb20260901 恢复。

## 验证

全量 2206 passed / 2 failed (两失败均为存量: Hermes job 内嵌 SQL 哨兵 +
ops 脚本同步哨兵, 非本批引入)。重启冒烟 7 条断言全绿零 Traceback。
