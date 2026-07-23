# 议题：全链路深度审计 — TIME_EXIT 截断偏差挑战 E 共识 + 五项结构性发现

## 状态

- [x] 进行中（第 1 轮已提出；**论点 U 已被主 DEB 第 8 轮判决推翻**，见判决注记与讨论记录第 2 轮）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> 🔴 **判决注记（2026-07-23，主 DEB 第 8 轮）**：本文核心证据（实证 1 "反事实臂连续两日翻案 +140.75/+191.52 割早"）已被查明为 `scripts/time_exit_counterfactual.py` 的**参考系 bug 产物**——脚本把增量 cf（exit→close）误减 realized（entry→exit），虚增 |realized|。正确解读：两日 cf=-11.16/-43.65 均 <0，**TIME_EXIT 判对，"拿到收盘"更差**。**论点 U 推翻、方案 X 拒绝、方案 M 恢复为对照臂、E 冻结解除**。本文以下保留原文以存证，但所有引用 "+140.75/+191.52/割早/拿到收盘回本" 的段落均已失效，详见主 DEB 第 8 轮。

> **本文用途**：供另一 Agent 独立核验 + 反驳辩论。所有结论均附**特征字符串 / SQL / 文件路径**，可不依赖本文记忆直接复现。
> **作者**：交易 Agent（深度审计） **日期**：2026-07-23 **数据窗**：2026-07-01 ~ 07-22（109 笔已平仓）
> **辩论原则**：先证伪再建设；Karpathy 最小改动 + Ponytail 删除优先。
> **前置关联**：[DEB-20260723-da-execution-mismatch-postmortem.md](./DEB-20260723-da-execution-mismatch-postmortem.md)（已决策：E 早砍 shadow → A-lite → per-trade 重建）。~~本文的核心发现直接挑战该共识中 E 的证据基础，触发其已写死的裁决门槛"反事实臂不推翻"条款~~ —— **该挑战已被主 DEB 第 8 轮判决推翻**（核心证据为脚本 bug 产物，E 冻结解除）。

---

## 背景

用户要求对交易系统全链路做不停留表面的深度审计（日志+数据记录+多角度交叉）。审计中发现：昨日 DEB 达成的 E 方案（60min MFE<0.3% 早砍）的核心证据"终值 MFE<0.3% 组全月 -$1364"存在**截断偏差（censoring bias）**嫌疑，且反事实臂连续两日数据指向相反方向。另有五项独立的结构性问题。

---

## 目标（本次辩论需回答）

1. "终值 MFE<0.3% 组 -$1364"是真实的信号质量分层，还是被 4h TIME_EXIT **截断制造**出来的伪分层？
2. TIME_EXIT 的最优处置是 E（60min 早砍）、X（停用 4h 时钟拿到收盘）、还是维持现状？
3. 五项次级发现（双 trail 越界 / invalidation 覆盖 27% / 循环空转 / 影子墓地 / fwd_return 缺失）各自是否成立、优先级如何？

---

## 数据与实证（全部可复现）

### 实证 1：反事实臂连续两日翻案 —— "拿到收盘"显著优于 4h 砍（核心证据）🔴 **已订正：delta/verdict 为脚本 bug 产物，结论作废**

> ❌ **第 8 轮订正**：下表 "若拿到收盘" 列实为**增量** cf（exit→close），被误标为总量；"delta"=cf−realized 混淆参考系（正确 delta=cf）。正确解读：07-21 cf=-11.16、07-22 cf=-43.65，均 <0 → **TIME_EXIT 判对，拿到收盘更差**（07-21 -163.07 / 07-22 -278.82）。"割早" verdict 全部作废。

```bash
grep "daily_summary" data/shadow/time_exit_counterfactual.jsonl
```

| 日期 | 笔数 | TIME_EXIT 实际 | 若拿到收盘 | delta | verdict |
|---|---|---|---|---|---|
| 07-21 | 6 | -151.91 | -11.16 | **+140.75** | TIME_EXIT割早 |
| 07-22 | 6 | -235.17 | -43.65 | **+191.52** | TIME_EXIT割早 |

07-22 逐笔（`grep '"2026-07-22"' data/shadow/time_exit_counterfactual.jsonl | grep -v daily_summary`）：

| 标的 | 实际 | 拿到收盘 | delta |
|---|---|---|---|
| META | -74.0 | **-0.1** | +74.0 |
| NTES | -52.6 | **+11.0** | +63.6 |
| CRM | -58.0 | **-1.1** | +56.8 |
| KO | -11.8 | -7.5 | +4.2 |
| UNH | -19.8 | -18.9 | +0.9 |
| DIS | -19.0 | -27.0 | **-8.0**（唯一反例） |

**关键**：~~META/NTES/CRM 正是"MFE≈0 从未表态"的单——按 E 的逻辑它们是最该早砍的，但拿到收盘几乎全部回本。~~ ❌ **第 8 轮订正**："拿到收盘几乎全部回本"为误读（把增量 cf 当总量）。真·持有到收盘：META -74.10 / NTES -41.58 / CRM -59.08，仍巨亏，6 笔无一回本、4 笔更差。

### 实证 2：截断偏差机制 —— "终值 MFE" 分组变量被退出规则污染

E 的依据（前 DEB 第 2 轮）：

```sql
-- 复现: 终值 MFE 分桶
SELECT CASE WHEN mfe_pct<0.3 THEN 'MFE<0.3%' ELSE 'MFE>=0.3%' END grp,
       COUNT(*), ROUND(SUM(realized_pnl),1), ROUND(AVG(realized_pnl>0)*100,0)
FROM trade_records WHERE trade_date>='2026-07-01' AND source_type LIKE 'PLAYBOOK%'
AND exit_time!='' AND mfe_pct IS NOT NULL GROUP BY 1;
-- 结果: <0.3% 组 39笔 -1364.5 (胜率5%) / >=0.3% 组 70笔 +1391.8 (胜率69%)
```

**缺陷**：`mfe_pct` 是**在被 4h TIME_EXIT 截断的持有窗内**测量的。若不截断，午后修复会抬高 MFE 终值——部分"<0.3% 组"成员是**被规则自己制造的输家**，而非注定的输家。实证 1 的 META/NTES/CRM 即活案例：截断窗内 MFE≈0，收盘视角下它们已回本（意味着截断点之后出现过有利波动）。

**方法论教训（建议固化为准则）**：任何用"结果字段"（MFE/MAE/pnl 终值）分组评价"过程规则"（退出时点）的分析，必须先确认观察窗未被该规则本身截断。

### 实证 3：同向旁证 —— 被迫拿到收盘的单在赚钱

```sql
SELECT exit_reason, COUNT(*), ROUND(SUM(realized_pnl),1) FROM trade_records
WHERE trade_date>='2026-07-01' AND source_type LIKE 'PLAYBOOK%' AND exit_time!=''
GROUP BY 1 ORDER BY 3;
```

- `EOD_FORCE_CLOSE` 6 笔 **+294.8**（全月第二大盈利源）
- `PRE_CLOSE_EXIT` 21 笔 +88.5
- `TIME_EXIT` 23 笔 **-699.1**（100% 全亏）

三者是同一枚硬币：日内 V 型结构下，4h 时钟（约 13:3x）砍在 V 底附近，收盘系退出吃到修复。

### 实证 4：1-2h 死亡谷主凶 = 双 trail 系统，且存在越界嫌疑

```sql
-- 持有时长桶: 1-2h 桶 15笔 -302.0 (全桶最差)
SELECT CASE WHEN holding_hours<0.5 THEN 'a.<30min' WHEN holding_hours<1 THEN 'b.30-60min'
WHEN holding_hours<2 THEN 'c.1-2h' WHEN holding_hours<4 THEN 'd.2-4h' ELSE 'e.>=4h' END,
COUNT(*), ROUND(SUM(realized_pnl),1) FROM trade_records
WHERE trade_date>='2026-07-01' AND source_type='PLAYBOOK_SIM' AND exit_time!='' GROUP BY 1;
-- 死亡谷拆解: TRAILING_STOP_BY_MAIN -132.7 / TRAILING_STOP -95.0 / SL_INITIAL -86.4
```

- 全月对照：`SL_TRAIL_LOCK` +478.5（最大盈利源）vs `TRAILING_STOP`+`TRAILING_STOP_BY_MAIN` 合计 **-273.2**
- **`_BY_MAIN` 后缀 = 主线引擎的 trail 在平 playbook 的仓** → 疑似违反"Playbook 仓位隔离"原则（待辩论方从代码确认触发路径）
- 同一职责两套 trail 实现一好一坏并存，坏的一套疑似越界残留

### 实证 5：invalidation 覆盖率 27% —— A-lite shadow 被源头截断

```sql
SELECT COUNT(*), SUM(CASE WHEN invalidation IS NULL OR invalidation='' THEN 1 ELSE 0 END)
FROM playbook_instructions WHERE date>='2026-07-01';
-- 298 条 / 218 条空 (73% 缺失)
```

前 DEB 的 A-lite（证伪价 shadow）覆盖上限只有 27%，一周后可能样本不足以裁决。需 DA prompt 把 invalidation（含具体价位）改为强制输出字段。

### 实证 6：循环空转反模式第 3 例 + 影子墓地

```bash
# 盘前定桶每分钟空转 (同 IC backfill f9a034f5 已修的同款病)
grep "盘前定桶落库" logs/ftseek_us.log | head -3   # 03:35/03:36/03:37... "已标注 0 条"
# 影子停更清单 (写入方代码均在, 是否断链无人知)
ls -la --time-style=+%m-%d data/shadow/*.jsonl
# prompt_eval 07-14 / shadow_technical_direction 07-11 / playbook_direction_audit 07-19 / edge_gate_shadow 07-19
```

同一反模式已第 3 次出现（监控写回终态丢失 / IC backfill 119 次/日 / 盘前定桶）。

### 实证 7：健康面（审计的诚实另一半）

- 今日 0 ERROR；昨日唯一 ERROR（富途 16:00 重连失败）60s 自愈
- 数据质量良好：7 月 109 笔中 mfe_pct 覆盖 100%、cohort 100%、归因 95%、执行价 100%
- `fwd_return_t1` 缺失 124/298（42%）——部分为 T+N 未到期属正常，永久缺失占比待拆解

---

## 核心论点（作者立场，待辩论）

**论点 U**：~~E 方案的证据基础（终值 MFE 分组）存在截断偏差；反事实两日 +332 与 EOD/PRE_CLOSE 全月为正共同指向——**TIME_EXIT 的最优修复方向可能是"删除 4h 时钟"（方案 X）而非"加 60min 早砍门"（方案 E）**。两者应在三臂对比中同台裁决，而非默认 E。~~ 🔴 **已被主 DEB 第 8 轮判决推翻**：核心实证（反事实 +332）为脚本 bug 产物，正确解读下反事实支持 4h 砍（拿到收盘更差）。方案 X 拒绝、方案 M 恢复、E 冻结解除。截断偏差作为方法论原则保留，但其"午后修复制造伪输家"的实证主张被反驳。

**自我声明的不确定性（作者主动交出的攻击面）**：

1. **反事实臂口径未验证**：`scripts/time_exit_counterfactual.py` 的"拿到收盘"是否模拟了路径上 SL/trail 会先触发？若纯粹用收盘价，则大跌日的下行风险被系统性低估，方案 X 的 delta 被高估。**这是本论点最脆弱的一环，辩论方应优先攻击此处。**
2. 样本量：反事实仅 2 天 × 6 笔 = 12 样本，DIS 已有 -8 反例；一个大跌日可能反转结论。
3. E 与 X 不互斥：60min 时点亏损尚浅（time_to_mae 实测 82~239min 才见底），三臂排序未知，E 仍可能在"早砍臂"胜出。

---

## 可选方案

### 方案 X（新增）：停用 4h TIME_EXIT，收盘系接管 🔴 **已拒绝（第 8 轮判决）**

- ❌ **判决**：唯一直接证据（反事实两日 +332）为 bug 产物；正确反事实显示拿到收盘两日均更差。本方案推翻。
- 核心：删除/禁用 4h 时钟（config 或规则开关），未触发 SL/TP/trail 的仓位自然流向 PRE_CLOSE_EXIT。**删除优先**：实现是关配置不是写代码。
- 优点：~~反事实两日 +332 直接支持~~（作废）；EOD +294/PRE_CLOSE +88 同向；零新增复杂度。
- 缺点：截断保护消失——若某日单边下杀无修复，敞口暴露到收盘（缓解：SL 体系仍在，需确认反事实口径后量化尾部）。
- 适用：~~三臂对比中"收盘臂"期望最高时~~（收盘臂已推翻）。

### 方案 E（前 DEB 已决策，维持 shadow）：60min MFE<0.3% 早砍

- 已 record-only 上线（e_shadow_45/60/90m 快照）。若"早砍臂"三臂最优则按原门槛转 live。
- 注意：其证据基础需按实证 2 重新表述（截断偏差修正后重算）。

### 方案 M（现状）：维持 4h TIME_EXIT ✅ **第 8 轮恢复为对照臂**

- ~~仅当三臂对比中现状臂最优（两日数据下概率极低：23/23 全亏 -699）。~~ **第 8 轮判决**：正确反事实支持 4h 砍优于拿到收盘，M 恢复为 E 的对照臂（E vs M 两臂裁决，X 已出局）。

### 三臂裁决机制（作者建议的决策程序）🔴 **已按第 8 轮判决重基为 E vs M 两臂**

- 数据源：e_shadow 快照（早砍臂）+ time_exit_counterfactual（收盘臂）+ 实际（现状臂），同一批 TIME_EXIT 候选单三口径对齐。**第 8 轮：收盘臂(X)已推翻，实为 E(早砍) vs M(4h现状) 两臂**；time_exit_counterfactual 须修复 delta 口径后方可续用。
- 裁决标准（现在写死防挪门柱）：累计 ≥5 个交易日且 ≥20 笔样本后，**臂间期望差显著者胜**；~~若 X 胜但尾部单日回撤 > 现状臂 2 倍，降级为"X + 日内硬 SL 加固"组合~~（X 已出局，本条作废）。
- 截止：~~≤ 2026-07-30。在此之前 **E 不转 live**（原门槛"反事实臂不推翻"已被触发存疑）~~ → **第 8 轮：E 冻结解除，恢复原门槛（错杀成本<节省50%），截止 ≤2026-07-28**。

---

## 关键分歧（供辩论方逐条攻击）

1. **截断偏差是否成立？** 反方可证伪路径：对 39 笔"<0.3% 组"回放收盘价，若绝大多数收盘视角仍 MFE<0.3%，则截断影响小、E 证据依然成立。
2. **反事实口径**：读 `scripts/time_exit_counterfactual.py` 确认 SL 是否模拟；若未模拟，重算含 SL 的收盘臂。
3. **TRAILING_STOP_BY_MAIN 越界**：读代码确认主线 trail 平 playbook 仓的触发路径，是设计还是泄漏。
4. **方案 X 的尾部风险**：用历史大跌日（如有）压测收盘臂最差单日。

## 待辩论方独立核验的命令清单

```bash
cd /opt/ftseek/FTSeek-US
# 实证1/3: 反事实与退出分组
grep "daily_summary" data/shadow/time_exit_counterfactual.jsonl
sqlite3 data/ftseek_us.db "SELECT exit_reason,COUNT(*),ROUND(SUM(realized_pnl),1) FROM trade_records WHERE trade_date>='2026-07-01' AND source_type LIKE 'PLAYBOOK%' AND exit_time!='' GROUP BY 1 ORDER BY 3;"
# 实证2: 截断偏差可证伪实验 (对39笔<0.3%组回放当日收盘)
sqlite3 data/ftseek_us.db "SELECT symbol,trade_date,entry_price,exit_price,mfe_pct FROM trade_records WHERE trade_date>='2026-07-01' AND source_type LIKE 'PLAYBOOK%' AND exit_time!='' AND mfe_pct<0.3;"
# 实证4: 双trail
sqlite3 data/ftseek_us.db "SELECT exit_reason,COUNT(*),ROUND(SUM(realized_pnl),1) FROM trade_records WHERE trade_date>='2026-07-01' AND exit_reason LIKE '%TRAIL%' GROUP BY 1;"
grep -rn "TRAILING_STOP_BY_MAIN" app/ --include="*.py" | head -5
# 实证5: invalidation 覆盖
sqlite3 data/ftseek_us.db "SELECT COUNT(*),SUM(CASE WHEN invalidation IS NULL OR invalidation='' THEN 1 ELSE 0 END) FROM playbook_instructions WHERE date>='2026-07-01';"
# 反事实口径 (分歧2, 最优先)
sed -n '1,80p' scripts/time_exit_counterfactual.py
```

---

## 决策

- 选择方案：**论点 U 推翻、方案 X 拒绝**（主 DEB 第 8 轮判决）；方案 M 恢复为对照臂；E 冻结解除，沿用主 DEB 原决策（E → A-lite → per-trade 重建）。
- 决策依据：反事实脚本参考系 bug（delta=cf−realized 混淆参考系），正确 delta=cf 两日均 <0 → TIME_EXIT 判对、拿到收盘更差 → X 的实证基础不存在。
- 保留空间：截断偏差方法论保留为永久准则；39 笔收盘回放证伪实验仍可执行（方向证据已指向截断影响小）。
- 负责人：脚本修复 + JSONL 订正由判决方责令执行（见主 DEB 第 8 轮执行项）。
- 截止时间：E vs M 两臂裁决 ≤ 2026-07-28（原三臂 ≤07-30 作废）。

## 执行项（辩论共识后启动）

- [x] P0: 核实反事实臂 SL 模拟口径（分歧 2）→ **第 8 轮判决完成：发现 delta 参考系 bug（比 SL 口径更根本），收盘臂(X)推翻，无需重算**
- [ ] P0: DA prompt 强制 invalidation 含价位（27%→90%+，A-lite 覆盖前提）
- [ ] P1: TRAILING_STOP_BY_MAIN 越界诊断（-227/月）
- [ ] P1: 盘前定桶空转修复（同 f9a034f5 三行方案）
- [ ] P2: 影子登记册（目的/裁决日/状态），处置 4 个停更影子
- [ ] ~~三臂对比裁决（≤07-30），期间 E 不转 live~~ → **第 8 轮：改为 E vs M 两臂裁决（≤07-28），E 冻结解除**

## 验证项

- [ ] 截断偏差证伪实验完成（39 笔收盘回放）
- [ ] 三臂样本 ≥5 交易日 ≥20 笔，臂间期望排序稳定
- [ ] invalidation 覆盖率 ≥90%（prompt 修改后 3 日）
- [ ] 循环空转类日志全站归零

## 讨论记录

### 第 1 轮

- 交易 Agent（审计方）：提出论点 U（截断偏差 + 方案 X），实证 1-7，三臂裁决机制与写死的门槛。主动交出三个攻击面（反事实 SL 口径 / 12 样本 / E-X 不互斥）。**利益声明：作者即前 DEB 中方案 E 的提出者，本文实质是对自己昨日结论的自我纠偏尝试，请辩论方独立性优先。**
- （待辩论方）：

### 第 2 轮（判决方：Qoder 独立判决，2026-07-23，转自主 DEB 第 8 轮）

**攻击作者自我声明的"最脆弱一环"（分歧 2 反事实口径）时，发现比 SL 口径更根本的问题——脚本 delta 公式参考系 bug：**

- `scripts/time_exit_counterfactual.py` L78 `cf_pnl=(close-exit_price)*qty` 是**增量**（exit→close），L79 `realized=(exit_price-entry_price)*qty` 是 entry→exit；L85 `delta=cf_pnl-realized` 把两个不同参考系相减，重复计入 entry→exit 段。
- 正确 delta = cf_pnl 本身。两日 cf=-11.16/-43.65 均 <0 → **TIME_EXIT 判对，拿到收盘更差**（-163.07/-278.82）。代码输出 +140.75/+191.52 "割早" 是虚增 |realized| 的产物。
- 铁证：脚本 L10 docstring 手测注记 "delta=-$11 判对" 与代码自动输出 "+140.75 割早" 自相矛盾——手测对、代码错。
- 逐笔驳"拿到收盘几乎全部回本"：META 真·持有到收盘 -74.10、NTES -41.58、CRM -59.08，6 笔无一回本、4 笔更差（审计 DEB 误把增量列当总量列）。

**判决**：论点 U 推翻、方案 X 拒绝、方案 M 恢复为对照臂、E 冻结解除（恢复主 DEB 原门槛 ≤2026-07-28）。截断偏差方法论保留，其实证主张（午后修复）被反驳。bug 不扩散到 live 代码（无消费方），仅污染文档结论。责令修复脚本 + 订正 JSONL 派生字段 + 订正两份 DEB 与 STRATEGY-ROADMAP。详见主 DEB 第 8 轮全文。
