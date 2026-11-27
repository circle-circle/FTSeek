# ANALYSIS 2026-11-27 · score_band×signal_gate 档位耦合（record-only，P3 留痕待裁）

> 状态：**纯记录，不改任何代码/阈值/映射**。来源：用户转发独立审计 P3 项＋本批只读复核。
> 关联：DEB-20260825 第14轮 P8（score_band 三档枚举修复）、RFC-218 §23。

## 1. 耦合结构（两个独立设计的交互）

- **DA 侧**（skill 仓 `llm_synth.py:1569`）：`_SCORE_BAND_MAP = {"HIGH": 8.0, "MEDIUM": 6.0, "LOW": 3.0}`。
  `DA_SCORE_BAND=1`（默认）时模型只给档位，数字分由代码映射；原始数字保留 `score_legacy`。
  设计意图（DEB-20260825 P8）：模型点估值塌缩到安全中值 7.0 → 档间差恢复（HIGH−LOW=5>3），
  使 merge 的 `|orig−chall|>3→SKIP` 辩论改判规则重新可触发。
- **门侧**（skill 仓 `signal_gate.py:~430`）：
  `优 = score≥7 ∧ decision_conf≥0.7`；`良 = score≥6 ∧ conf≥0.6`；`中 = score≥5 ∧ conf≥0.5`；否则 `差`。
  弃权抬升路径 `conf = max(conf, score/10)`（真弃权∧非 DISAGREE 矛盾输出时）。

## 2. 结构性后果（算术必然，非事故）

| band | 映射分 | 可达最高档 | 约束 |
|---|---|---|---|
| HIGH | 8.0 | 优 | 需 conf≥0.7（辩论一致度或弃权抬升 0.8） |
| MEDIUM | 6.0 | **良（结构性封顶）** | 6<7 ⇒ 永不达优；conf≥0.6 才良，否则中 |
| LOW | 3.0 | 差 | 3<5 ⇒ 差/BLOCK（方向 SKIP 另计） |

- MEDIUM 封顶良是两设计交互的**隐含结果**，未见任何 DEB 显式裁决过「MEDIUM 是否应可升优」。
- 弃权抬升下 MEDIUM conf=0.6 ⇒ 恰好良；HIGH conf=0.8 ⇒ 可优。DISAGREE 矛盾输出不抬升（2026-10-27 B2）。

## 3. 实测分布（data/da_gate_decisions.jsonl，2026-11-01 起，n=619，只读）

| da_score | SKIP | 差 | 中 | 良 | 优 |
|---|---|---|---|---|---|
| 3.0 (LOW, n=157) | 148 | 9 | 0 | 0 | 0 |
| 6.0 (MEDIUM, n=245) | 60 | 0 | 136 | 49 | **0** |
| 8.0 (HIGH, n=217) | 37 | 0 | 137 | 0 | 43 |

- MEDIUM→优=0 与算术一致；HIGH 的 63%（137/217）落「中」＝conf<0.7 被辩论一致度压档。
- legacy→mapped 样例：7.0→8.0（13 行，band 升档）、4.0→3.0（36 行，降档）、6.5→6.0（17 行）。
- 「优」全账占比 43/619≈7%：供给偏紧是 conf 门与 band 映射共同作用，非单一阈值。

## 4. 待裁问题（不在本批实施）

1. MEDIUM 结构性封顶良是否为期望行为？若期望「中档+强一致」可升优，需改映射或门阈值——
   两者都属**正式判定变量**，须单变量 A/B＋预注册，不得以「正确性」名义直改。
2. HIGH 大量落中（conf<0.7）：挑战者弃权率高时抬升路径已覆盖部分；是否进一步放宽属收益假设，
   同样须 A/B（§10.4 纪律）。
3. 本 note 仅留痕＋提供分布证据；任何调整另立 DEB/RFC 裁决。

## 5. 复跑凭据

```python
# 只读聚合（无 DB 写）:
import json; from collections import Counter
rows = [json.loads(l) for l in open('data/da_gate_decisions.jsonl') if l.strip()]
recent = [r for r in rows if r.get('trade_date','') >= '2026-11-01']
print(Counter((r.get('da_score'), r.get('tier')) for r in recent))
```

**最后更新：2026-11-27（RFC-218 §23 批，record-only）。**
