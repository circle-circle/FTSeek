# RFC-119: MFE 近零死亡族的 2 分钟延迟确认观测分层

> **状态**: 已实施 (record-only 观测分层; 不接线、不改交易行为)
> **日期**: 2026-09-30
> **作者**: Codex
> **审批人**: 待用户追认 — 注: 2026-09-30 用户「按照建议执行」批复对象是 RFC-118 影子阶段; 本 RFC 为 Codex 顺带扩展的纯观测层 (零判据改动/零接线), 由 Hermes 独立复核数据后提交 (复核记录见下), 审批溯源如实标注不冒用
> **Hermes 复核 (2026-09-30 17:55)**: 可加性精确验证 (18+88=106=ALL n; +1172.42−1587.73=−415.31=ALL net 分毫不差); mfe_pct 覆盖 106/106; graduation 原判据块零改动; MFE 仅汇总期 join 禁在线; 112 passed

---

## 摘要

复用既有 D2' 触发延迟确认影子 (`scripts/entry_delay_confirm_shadow.py`) 的 2 分钟与 5 分钟反事实行，在结算汇总期 join `trade_records.mfe_pct`，新增 `MFE<0.3%` 死亡族 / 非死亡族 / `MFE≤0.001%` 三个只读分层。目标是回答：2 分钟“仍站触发方向才入场”的机制，是否专门捕获 MFE 近零死亡族，以及它是否同时误杀正常交易。本 RFC 不改变 D2' 原毕业判据，不新增在线判定，不接线。

---

## 问题陈述

RFC-118 已确认：入场价改善约 15bp 只能处理费用线附近的边际交易，无法拯救 MFE<0.3% 的死亡族。死亡族的问题更接近“信号触发后方向没有延续”，因此需要独立的时点/回避观测，而不是把死亡族混入可达性门。

既有 D2' 影子已经实现“触发后延迟 N 分钟，价格仍站在触发方向则入场，否则回避”的反事实：

```text
N=2: 触发后 2 分钟复核
N=5: 触发后 5 分钟复核
```

但它原汇总只按 ALL、SL_INITIAL、B/D 分支分层，未直接按 MFE 死亡族分层。因此本 RFC 只补观测面。

---

## 提议方案

### 方案概述

在 D2' 每日结算时：

1. 读取 `data/shadow/entry_delay_confirm.jsonl` 的既有 `(trade_id, delay_min)` 行；
2. 只读 join `trade_records.mfe_pct`；
3. 输出以下分层：
   - `death_mfe_lt_0_3`: MFE<0.3% 死亡族；
   - `alive_mfe_ge_0_3`: 非死亡族；
   - `mfe_zero_le_0_001`: MFE 近零子集；
4. 每层继续沿用 D2' 的回避率、回避净 PnL、误杀率、机会成本、净价值和日聚类读数；
5. 日报增加 forward 死亡族/非死亡族对照行。

### 关键纪律

- MFE 是事后标签，禁止作为在线入场条件。
- 本分层不改变 D2' 预注册毕业判据：`n≥20 ∧ 避免率≥30% ∧ 机会成本<0.3%`。
- 不新增采集器、不新增 cron；复用 D2' 的 58 5 结算链。
- 死亡族与非死亡族必须同屏呈报，防止只看“拦掉亏损单”的单边叙事。

---

## 当前读数 (2026-09-30 16:47)

### 2 分钟腿

| 分层 | n | 回避 | 回避率 | 误杀 | 净价值 |
|---|---:|---:|---:|---:|---:|
| forward 死亡族 MFE<0.3% | 18 | 11 | 61.1% | 3/11=27.3% | +$1,172.42 |
| forward 非死亡族 | 88 | 42 | 47.7% | 36/42=85.7% | -$1,588 |
| forward MFE≤0.001% | 7 | 5 | 71.4% | 0/5=0% | +$435.84 |

forward 全体 D2' 读数为净 -$415 左右：死亡族收益被非死亡族误杀/机会成本抵消。

### 结论

2 分钟机制确实显示死亡族捕获能力：

- forward 死亡族回避率 61.1%；
- 死亡族误杀率 27.3%，低于 40% 参考线；
- MFE 近零子集 5 笔全部为亏损单，0 误杀。

但它不能直接转正：

- 非死亡族 forward 净价值 -$1,588；
- 非死亡族误杀率 85.7%；
- 死亡族 forward n=18，仍低于 30 笔宪章地板；
- 总体 forward 仍为负，说明该门目前太粗，无法区分“死信号”与“正常信号”。

因此本 RFC 的判断是：**方向有效、机制未毕业，继续攒样，不接线**。

---

## 风险评估

- **前视风险**: MFE 只能作为离线标签，不得进入在线规则。
- **样本风险**: forward 死亡族 n=18，不足 30，不可裁决。
- **误杀风险**: 非死亡族误杀率极高，直接上线会显著减少有效交易。
- **口径风险**: `mfe_pct` 来自交易记录，若历史 MFE 回填口径变化，应重新冻结窗口。

---

## 验证计划

```bash
./venv/bin/python -m pytest tests/test_entry_delay_confirm_shadow.py -q
./venv/bin/python scripts/entry_delay_confirm_shadow.py --days 60 --no-fetch
./venv/bin/python ops/hermes/entry_delay_shadow_push.py
```

验收：

1. latest.json 出现 `delays[2].mfe_cohort` 与 `delays[5].mfe_cohort`；
2. 契约测试覆盖死亡族/非死亡族/MFE 近零分层；
3. 日报出现死亡族 forward 对照行；
4. 原 graduation 判据不变。

---

## 回滚计划

```bash
git revert <this-commit>
```

回滚触发条件：MFE join 口径失真、D2' 原判据被误改，或死亡族分层造成结算链异常。

---

## 实施记录

**实施日期**: 2026-09-30
**改动**:
1. `scripts/entry_delay_confirm_shadow.py`: 增加 MFE map、`mfe_cohort` 分层；
2. `ops/hermes/entry_delay_shadow_push.py`: 日报增加死亡族 forward 对照行；
3. `tests/test_entry_delay_confirm_shadow.py`: 增加契约钉。

**验证结果**: `tests/test_entry_delay_confirm_shadow.py` 21 passed；结算脚本 rc=0；日报输出正常。
