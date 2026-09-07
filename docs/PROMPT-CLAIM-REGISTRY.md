# PROMPT-CLAIM-REGISTRY — Prompt 声称的下游规则 ↔ 真实代码消费方 登记表

- **建立**: 2026-09-07 (DEB-20260907 P0-5, 用户裁「直接修复」批次配套)
- **目的**: Prompt 里每一句"下游会怎样"的断言，登记真实代码位置与核验状态。
  新增/修改 prompt 中任何下游行为描述前，必须先在本表登记或更新对应行。
- **核验口径**: "已核验" = 有代码位置 + 最近一次人工/Agent 读码确认日期；
  "假断言" = 代码中不存在该机制或阈值，prompt 侧已删除或改写。

---

## 1. DA 分析师 Prompt (hermes: deep-stock-analysis/scripts/llm_synth.py)

| # | Prompt 断言 | 真实机制 | 代码位置 | 状态 | 核验日 |
|---|---|---|---|---|---|
| 1 | 「止损距 entry 超 6% 被准入层硬拦」 | **不存在**。准入止损宽度上限 `max_sl_pct: 0.0`（不拦，2026-08-16 禁用）；真实收口是重定价桶边界钳制（钳，非拦） | `app/playbook/admission.py:56`、`config/unified_config.yaml:1418` | 假断言 → 已改写为真实接口事实（钳制约 1.5–5.8%，不拒单） | 2026-09-07 |
| 2 | 「不接飞刀：当日跌 >5% 无企稳 LONG 不推荐」 | 规则层已存在同源硬门 B_long_knife（−5%） | `app/playbook/pipeline.py:3863-3881`、`premarket_invalidation.py:36` | 真但双重实现 → prompt 侧已删硬门表述，保留「企稳/错杀」语义判断任务 | 2026-09-07 |
| 3 | 「confidence（高/中高/…）」输出字段 | 消费方全为 record/display-only：merge 档位微调（缺省"中"）、`_llm_self_confidence` 归档、signal_gate `quality_score`（conf_map，判定链无该字段，对 PASS/BLOCK 影响恒零） | `signal_gate.py:94-96`、`llm_synth.py:2164`、`pipeline.py:1311` | 零门禁消费 → 已删字段，替代出口 `evidence_gaps[]` | 2026-09-07 |
| 4 | score_band → 数字映射 HIGH=8/MEDIUM=6/LOW=3 | 真实存在，代码重算 | hermes `signal_gate.py` | 真断言（保留） | 2026-09-07 |
| 5 | 质量闸 WARN → 信心再降 0.15 | 真实存在 | hermes `pipeline.py:1127` | 真断言（保留） | 2026-09-07 |
| 6 | 辩论信心合成（基 0.50 / 一致 +0.20 / 反向 −0.15 / 分差衰减） | 真实存在，与模型自评 confidence 无关 | hermes `adapter.py::_compute_confidence` | 真断言（保留） | 2026-09-07 |

## 2. 证伪官 Prompt (llm_synth.py 内 challenger_system_v2)

| # | Prompt 断言 | 真实机制 | 代码位置 | 状态 | 核验日 |
|---|---|---|---|---|---|
| 7 | 「每发现一处来源不明数字 my_score −1」 | 扣分由模型自罚执行，无代码核验 | — | 诱使少报 → 已删，幻觉计数改由代码独立核验 | 2026-09-07 |
| 8 | 「my_direction 必须 BUY/SELL_SHORT 二选一」 | 下游对 SKIP 有承接分支（合法弃权不罚） | `llm_synth.py` _debate 合并逻辑 | 假断言 → 已改为允许 SKIP（弃权率作观测信号） | 2026-09-07 |
| 9 | 新增 `falsification_target` / `flip_condition` | 消费方 = `_debate` 字典透传落库（record-only） | `llm_synth.py:2094` 附近 | 已登记 | 2026-09-07 |

## 3. UE Prompt (FTSeek-US scripts/llm_universe_scan.py)

| # | Prompt 断言 | 真实机制 | 代码位置 | 状态 | 核验日 |
|---|---|---|---|---|---|
| 10 | UE `confidence` 0~1 | **生产消费中**：注入闸 `s["confidence"] >= 0.5` 硬过滤 | `scripts/llm_universe_scan.py:2038` | 真断言（本轮**未动** UE prompt） | 2026-09-07 |
| 11 | 「代码将核验引证真实性、空引证封顶 0.60」 | 待核验（登记占位） | — | 待核验 | — |

## 4. 新增字段消费方登记（2026-09-07 修复批引入）

| 字段 | 产出方 | 消费方 | 代码位置 | 门禁影响 |
|---|---|---|---|---|
| `evidence_gaps[]` | DA | SKIP 审计 + data_quality_note 对账（record-only） | hermes `pipeline.py` raw_instruction 白名单 | 无（护栏：SKIP 率相对基线升幅 ≤10pp） |
| `falsification_target` / `flip_condition` | 证伪官 | 辩论落库（record-only，AGREE 同样必填） | hermes `llm_synth.py` `_debate` dict | 无 |

## 5. 已知归因缺口

- 证伪官侧指纹 `_challenger_prompt_hash = _prompt_fingerprint(challenger_system, "v3-falsify")`
  只含 system 串，user 模板改动不进指纹（本轮 E13 加了 user 侧必填声明 → 指纹不变）。
  后续若按指纹切基线需注意此盲区。

---

**Prompt 版本指纹（本轮修复后）**：DA = `1d333e53715e`；Challenger = `feb264e32b1c`。
回滚：`cp scripts/llm_synth.py.bak-deb20260907 scripts/llm_synth.py`（pipeline.py 同理，hermes 仓）。
