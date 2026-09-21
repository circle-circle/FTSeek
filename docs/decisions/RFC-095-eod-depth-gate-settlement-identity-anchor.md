# RFC-095: EOD 深水闸影子结算器身份锚定 v2 — 结算把"最新已平仓"当结局的失配修复

> **状态**: 已实施 (2026-09-22, 用户裁"同意"= 采纳建议 A/B)
> **类型**: L1 (测量侧修复, 零交易行为变更 / 零阈值变更 / 零新门禁)
> **前置**: RFC-079 (EOD 深水闸 record-only 影子, 2026-09-09) · 判据 B19 (docs/ADJUDICATED-CRITERIA.md)
> **触发**: 用户问「EOD 兜底豁免水下仓持夜这条规则样本表现如何 / 是不是在制造隔夜亏损」

## 1. 问题陈述

深水闸影子的结算口径是"arm 行 → 实际结局"的关联。旧实现 (`scripts/settle_eod_depth_gate_shadow.py`)
用 **`WHERE symbol=? AND exit_time>=arm_date ORDER BY exit_time DESC LIMIT 1`** 取"最新已平仓"当结局，
两个病灶:

**病灶① 同日同标的另一笔仓被当成结局，且永久冻结。**
实测 `US.MU` arm 行 (arm_date 2026-09-15, entry_price 934.515, qty 6): 真正的仓是 `trade_records.id=1503`
(9/16 09:31 ET 平, 亏 −23.61)。但结算在 **9/16 06:30 CST** 跑，此刻 1503 **还没平** → 查询配到同日
另一笔 **1501** (9/15 10:03 就平了, 盈 +9.63) → 该行写入 `settled=true` **此后不再重算**，
Δ 由 −36.51 变成 −73.23 (**错 $36.72**)。凡是"同标的同交易日两笔 + 结算时真仓未平"都会中招。

**病灶② 未持夜的行被当成隔夜结局。**
`US.NOK` 9/17 15:52:04 记"豁免成立"，同一分钟 15:52:51 实际按规则 4 `PRE_CLOSE_EXIT` 平仓
(净盈亏在 47 秒内由 −0.11% 翻到 +0.03%，跨过 `net<0` 的豁免判定线)。该行被算成隔夜样本，Δ=−5.55。
Δ 的口径本应是"持夜 vs 当日 EOD 平"，未过夜的行不属于该反事实。

**病灶③ 同一仓多夜会落多行，逐行求和会重复计数。**
`US.UBER` 1485 因多夜持仓落 4 行 (9/11/14/15/16 arm)。旧报告 `trigger_settled_n` 是**行数**，
而判据 (B19: n≥8) 的 n 轴是**仓数**。两轴不同轴 → 可能提前触发转正。

**为什么值得修 (影响面)**：B19 是预注册待裁项，其转录判据是
「深水 (>2.5%) 触发组 ΣΔ > 0 且误杀 ≤1 且 n≥8 → GRADUATION_LINE_HIT」。配错仓 = Δ 符号可能整体翻转
= 用错误证据做出**会改变交易行为**的裁决。当前实测结论未受影响 (触发组仅 1 行)，但失败模式已实活。

## 2. 修法 (测得到、可回滚)

| 面 | 改动 |
|---|---|
| 打点 (`app/playbook/overnight_decision.py::emit_eod_depth_gate_shadow`) | arm 行新增两个**身份锚**字段: `trade_ref` (= `trade_records.id`, 整数, 最强锚) 与 `entry_time` (= 该仓 entry_time 原样透传, ISO 串, 只做逐字比对不做时区解析)。锚转换失败 → 退化 `None`，**行照常落盘**(度量连续性优先) |
| 锚注入 | `pipeline.py` 建 meta 时补 `meta["_trade_id"] = r["id"]`; poll 侧 `exit_engine.py` 传 `trade_id=meta.get("_trade_id")`; sweeper 侧传 `trade_id=r["id"]` |
| 结算 (`scripts/settle_eod_depth_gate_shadow.py`) | 匹配改为 **trade_ref 精确 > entry_time 精确 > (entry_price±0.5% ∧ qty ∧ 出场晚于 arm 决策时刻)**; **都中不了 → 保持 unsettled，不再猜** |
| 未持夜 | 出场 ET 日 ≤ arm_date → `status=not_held`, `held_overnight=False`, `cf_delta=None`，**不进 Δ 聚合** |
| 存量行 | 已 settled 且无 `match_by` 的行**重算一次**，旧值写 `superseded` 留痕；重算不中 → `match_by=legacy_unverified`(保留旧值，不进聚合，仅呈报) |
| 判据轴 | 触发组按 `trade_id` **去重取最早夜**；报告同时给 `trigger_settled_n`(仓数=判据轴) 与 `trigger_settled_rows`(行数) |
| 阈值 | **逐字未动** (MIN_N=8 / MAX_MISKILL=1 / FEE_LINE=5.0 / 判据字符串) — 契约测试 `test_verdict_thresholds_unchanged` 钉锁 |

**不做**：不改 EOD 豁免行为、不改深水阈值、不新增门禁 (OKR-2026-09 C1 退出参数冻结期内)。

## 3. 验证 (故障复现式, 非"跑通即算")

新旧两版在**同一份生产库副本**上对撞 (`/tmp/rfc095_verify.py`, 只读生产、写 /tmp):

```
旧版: US.MU trade_id=1501 Δ=-73.23   ← 复现病灶①
新版: US.MU trade_id=1503 Δ=-36.51 (match_by=entry_price_qty) 留痕 superseded
      US.NOK 9/17 status=not_held Δ=None  ← 病灶②排除
单调性: 旧已结算 18 行 → 新已结算 18 行, 回落 0 行
未结算积压: 旧=2 新=2 (口径一致; = US.RIVN 仍在仓 + 1 行测试占位)
判据读数: 旧 n=1 ΣΔ=-109.12 KEEP_SAMPLING → 新 n=1 ΣΔ=-109.12 KEEP_SAMPLING (未变)
```

**验证中抓到并修掉的自身缺陷**：重构时非 legacy 分支漏 `if not ok: continue`，导致 2 个无锚行被误标
`settled` 且 `unsettled_backlog` 假报 0。已修，并把它固化成断言 ("无锚行未被误标 settled") +
契约测试 `TestNoAnchorStaysUnsettled`。**这条正是"验证必须能证伪"的价值**。

测试: `tests/test_settle_eod_depth_gate_shadow.py` (19 例, 内存 sqlite 桩) +
`tests/playbook/test_eod_depth_gate_shadow.py` (+3 身份锚字段契约) → 24 passed。

## 4. 生产落地记录

```
2026-09-22T06:56 CST 首跑: arm行 19 新结算 17 重算 2(US.MU↓·US.NOK) 未结算积压 2 未持夜 1
                     触发组 n=1/8 ΣΔ=-109.12 误杀 1/1 verdict=KEEP_SAMPLING
二次运行: 新结算 0 / 重算 0 (幂等)
```

## 5. 风险与缓解

- **最坏情况**: 新锚在某个边角配错仓 → Δ 错。缓解: 三重锚 + 唯一性(出场晚于决策时刻) + 不中即
  unsettled(**失败态可见**, 不再静默配对) + 重算留痕 `superseded`。
- **回滚**: `git revert <commit>` 恢复旧结算器；影子文件字段是**纯增量**(旧行仍可读)，无迁移。
- **回滚触发**: 结算出现 `legacy_unverified > 0` 或未结算积压持续上升 (>3 个交易日)。

## 6. 遗留 (不在本 RFC 范围)

1. 病灶①的**同类面**: `eod_depth_gate_shadow` 之外的影子结算器若也用"最新已平"配仓 (待扫)。
2. B19 转录判据的**回溯基线**仍用日K收盘代理价 (影子实测双价偏差中位 0.11%/最大 0.43%) —
   建议换 15:52 实价重跑，属**另一件** (涉及判据证据重算，须单独立项留痕)。
3. 深水闸本身维持 record-only 攒样 (触发组 n=1/8)，**无转硬闸证据**。

## 7. 审批

- [x] 5 问自检 (改哪里/影响谁/如何验/如何回滚/有没有更简单)
- [x] 测试覆盖 (24 例)
- [x] 回滚 = git revert
- [x] 用户裁决: "同意" (2026-09-22)