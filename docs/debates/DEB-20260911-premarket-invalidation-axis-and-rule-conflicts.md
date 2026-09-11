# 议题：盘前失效门（A/B/C 规则）的参照轴是否合理、如何降低误拦、与现役门的冲突如何裁决

## 状态

- [x] 进行中 → **已完成两轮辩论并收敛**（第二方/第三方辩论记录见文末；
  **本文正文多处数字已被核实为错误，行动前必读该章的勘误表 E1–E8**）
- [x] 待决策 → **已裁决**
- [x] 已决策（2026-09-11 用户裁「按建议执行」：执行范围 = **P0 全部 + P1 全部**；
  **P2-1/P2-2 明确未执行** — 触碰现役硬门/生产取数路径，须另立预注册 DEB）
- [x] 已验证（**契约钉 55 例全绿**：settler 17 + premarket 10 + 意图守卫 16 + W7 12；
  **全量回归亲跑**：3928 收集 → 3886 passed / 41 skipped / **1 failed** / 628s。
  唯一那红 = `test_ops_scripts_sync::test_repo_and_live_scripts_in_sync`
  （库内周检副本领先 `/root/.hermes/scripts` 线上副本，属部署步、等用户裁，非代码缺陷）；
  两条棘轮（`test_except_pass_ratchet` / `test_w456_log_hygiene_ratchet`）未被本轮新增
  except 块顶破。**旧基线 3485 passed 已作废**：工作树含未入库的 M2 批新测试
  （`tests/core/test_m2_a1_startup_mode.py` 等），本轮起以 3886/41 重锚）
- [ ] 已关闭（三项未了：①P2-1/P2-2 另立预注册；②周检线上副本部署（消掉上述红钉）；
  ③**服务重启** — A/C 退役是采集侧 Python 改动，只在 import 时加载，未重启则线上仍按
  三规则打标，重启窗口须用户裁）

- **提出方**：Hermes（qwen3.8-max），2026-09-11
- **数据基线**：`data/premarket_invalidation_shadow.jsonl` 310 行（**8/11–9/10，21 个交易日，
  其中 39 行 flagged**；原写 8/11–9/11 系勘误表 E2 所指窗口错标），
  结算产物 `data/premarket_invalidation_report_latest.json`（结算器 commit `ea614074`，
  2026-09-11 落地，本辩论数据全部由该结算器 + ad-hoc SQL 产出）
- **当前形态**：全部 record-only 影子，零交易行为变更。B 规则的**执行时刻硬门已独立在线上**
  （RFC-068，非本影子），A/C 无硬门。

---

## 背景

### 组件定位

`app/playbook/premarket_invalidation.py`（2026-08-09 用户批准方案1落地）：每交易日
**09:35–09:59 ET 评估一次**，对当日全部可执行指令取 Futu snapshot，算涨跌幅，
按三条候选规则打「若启用将失效」标记后落盘。零行为变更。

三条候选规则（源码 `_RULES`，阈值全部硬编码）：

| rule_id | 方向 | 条件 | 证据源（立项时） |
|---|---|---|---|
| `A_short_chase` | SELL_SHORT | chg > +3% | DEB-20260808 逆势追空亏损桶 −$172 |
| `B_long_knife` | BUY | chg < −5% | DEB §5.3 / DA prompt v2.4 已禁产 |
| `C_long_chase` | BUY | chg > +3% | 「Qoder ≥10:00 复算：顺势 LONG −12.22/笔」 |

立项时明确排除项：`LONG 盘前跌 −2%~0（回调日）= 赢家模式 +$103，不打标`。

### 触发本辩论的三件事

1. **C 规则误杀率 68% 超预注册门（≤40%）**：结算器首读 22 笔 executed 中 16 笔赢家
   （+$1,157）、6 笔输家（−$1,224），净值 ≈ **−$67 ≈ 零**。按预注册判据
   `净<0 ∧ 误杀≤40%` 不成立 → 不支持转正。
2. **基准/相位问题被用户追问暴露**：字段名 `premarket_price` 实际是**开盘后 09:35–09:59 的价格**
   （regular session），不是盘前 pre_price；分母 `prev_close` = 昨日正式收盘。
   命名与语义不符，且 C 的证据源是「≥10:00 复算」口径，应用时刻却在 09:35。
3. **同规则双影子并行攒样**：A 规则与 `short_chase_ban_shadow`
   （`pipeline.py:5530-5565`，DEB-20260808 §8.3）阈值同为 +3%，一个快照口径、一个触发时刻口径，
   且 DEB 已**否决过 A 的硬门版本**（残存效应 −$19.80，理由：8月逆势亏单全在 9 点档，被禁触窗架空）。

---

## 目标（请第二方逐一回答，不必同意提出方）

1. **参照是否合理**：`chg = (last_price − prev_close)/prev_close` 这个轴，对「该不该入场」
   是否携带信息？如果不携带，正确的轴是什么（anchor 延伸度 / ATR 归一 / 其他）？
2. **如何降低误拦**：在保留拦截价值的前提下，有哪些可验证的修正路径？
   各自的先验证据强度如何？
3. **规则冲突**：A vs `short_chase_ban_shadow`、C vs `chase_cap` + `extreme_gap`（现役硬门）、
   B 的三层冗余——哪些是真冲突、哪些是正确纵深？裁决日应如何归并防双转正？
4. **是否应该直接退役 C**：D2'（2min 延迟复核门）已覆盖 C 命中的全部 22 笔交易，
   C 是否已无独立存在价值？

---

## 数据与实证

> 所有数字均可复现。复现命令见每节末 `验证:` 行。
> **统计诚实声明（提出方主动标注）**：C 规则 executed 样本 **n=22**，分桶后每桶 n=3–8，
> **无一过显著性检验**。以下分桶结果是方向性诊断，不是统计裁决。
> 但「赢家/输家在 chg 轴上中心位置重合」这一条不依赖显著性——区分度为零的轴，样本再大也分不开。

### 实证 1：C 规则按 chg 分桶非单调（22 笔 executed）

| chg vs 昨收 | n | 净额 | 胜率 |
|---|---|---|---|
| [3%, 4%) | 4 | −$80 | 3/4 (75%) |
| [4%, 6%) | 4 | **+$125** | 4/4 (100%) |
| [6%, 10%) | 6 | **+$235** | 3/6 (50%) |
| [10%, +∞) | 8 | −$320 | 6/8 (75%) |

**关键**：涨幅最大的桶（[10,+)，−$320）和刚过线的桶（[3,4)，−$80）都亏，中间桶反而赚。
非单调 = 该轴不是「越大越危险」的风险轴。

### 实证 2：赢家/输家在候选轴上的中心位置

| 轴 | 赢家中位 | 输家中位 | 赢均 | 输均 | 分离度 |
|---|---|---|---|---|---|
| `chg` vs 昨收（现行 C 轴） | +7.01% | +7.17% | +9.88% | +9.75% | **零** |
| `extension vs anchor`（现价 vs 计划入场价） | +1.96% | +1.44% | +2.11% | +1.68% | 弱反向（见实证3） |
| `chg / ATR%`（波动率归一） | +125× | +179× | +218× | +238× | 弱 |

（`chg/ATR%` 数值 >100 是因为 `atr_pct` 字段存的是小数 0.05 而非百分数 5.0 —
**这是一个待第二方核实的数据口径疑点**，见「待验证疑点」§Q3。）

### 实证 3：anchor 延伸度分桶（22 笔）

| 现价 vs anchor 偏离 | n | 净额 | 胜率 |
|---|---|---|---|
| (−∞, 0) | 6 | −$206 | 4/6 (67%) |
| [0, 1%) | 3 | +$33 | 2/3 (67%) |
| **[1%, 3%)** | 7 | **+$514** | 6/7 (86%) |
| **[3%, +∞)** | 6 | **−$379** | 4/6 (67%) |

`[1,3)` 与 `[3,+)` 之间有一道可见的符号翻转。但 n=7 vs n=6，**不构成证据**，
且与实证1的 chg 分桶存在选样重叠（同一批 22 笔的不同切法）——多重比较风险，
提出方不主张这是「找到了新轴」，只主张「chg 轴无信息、anchor 轴至少值得测」。

### 实证 4：C 规则 22 笔逐笔明细（含 slip / vs_trigger 偏离）

```
sym      chg%   anchor     trig   entry$   slip%  vs_trig%      pnl entry_t
ORCL    +3.16   156.00   157.50   157.40   +0.89    -0.07    +16.3 09:48
SNDK    +3.17  1636.72  1634.82  1631.71   -0.31    -0.19   -122.0 10:16
ORCL    +3.24   143.00   146.84   146.98   +2.78    +0.10    +13.4 09:53
CRM     +3.60   265.00   265.80   266.22   +0.46    +0.16    +12.8 09:45
META    +4.03   617.73   618.27   618.18   +0.07    -0.01     +4.0 09:40
AU      +4.32   119.93   119.63   120.08   +0.12    +0.37    +13.2 09:36
AMD     +4.34   467.01   474.35   473.88   +1.47    -0.10    +13.3 09:41
ORCL    +4.37   144.25   149.61   149.41   +3.58    -0.13    +95.1 09:54
SMTC    +6.31   132.75   136.23   136.06   +2.49    -0.12   +448.7 09:37
DELL    +6.71   450.00   474.27   474.31   +5.40    +0.01    -45.1 09:42
USAR    +7.07    18.55    18.95    18.89   +1.81    -0.34   -149.8 09:37
QCOM    +7.28   184.86   181.27   180.94   -2.12    -0.18   -223.2 09:35
BE      +7.71   258.00   267.44   267.76   +3.78    +0.12   +179.2 09:40
ASTS    +9.28    57.85    60.43    60.40   +4.41    -0.05    +25.3 09:48
BULL   +11.34     9.70     9.60     9.57   -1.34    -0.31    +97.0 09:38
ROIV   +14.27    39.50    39.82    39.85   +0.89    +0.08   +155.4 09:35
AFRM   +15.44    83.50    86.31    86.37   +3.44    +0.07   -303.6 09:45
GTLB   +17.12    51.50    52.09    51.94   +0.85    -0.29    +16.4 09:37
GAP    +18.81    23.35    24.36    24.36   +4.33    +0.00   -380.3 09:40
OKTA   +20.48   161.05   164.62   164.62   +2.22    +0.00    +65.3 09:43
SNOW   +21.14   373.70   374.36   373.86   +0.04    -0.13    +25.1 10:01
ESTC   +23.39    96.50    99.81    99.92   +3.55    +0.12     +5.0 10:08
```

观察（提出方主张，请反驳）：
- `vs_trig%` 全部在 ±0.4% 内 → **触发门本身工作正常**，成交价紧贴触发线。
  C 规则想拦的不是执行滑点，是「今天整体高开」这个环境判断。
- 6 笔输家中 3 笔（SNDK/USAR/QCOM）MFE=0.00 且 `SL_INITIAL` 退出 → 入场即错。
  另 3 笔（DELL/AFRM/GAP）MFE>0 后回吐 → 退出层问题，非入场层。
  **C 规则若转正，只能解决前 3 笔的一半，后 3 笔属于退出层，不是它的管辖范围。**
- 最大赢家（SMTC +$449、BE +$179、ROIV +$155、OKTA +$65、BULL +$97）
  **全部 chg > +6%**，即全部会被 C 规则拦掉。这是误杀 68% 的结构性来源：
  高波动动量票 = 这个系统的主要利润来源（`SL_TRAIL_LOCK` 族 30天 +$3,910）。

### 实证 5：D2'（2min 延迟复核）对 C 命中交易的全覆盖交叉验证

C 命中的 22 笔 executed 交易，**D2' 影子全部覆盖（22/22，0 未覆盖）**，逐笔判定：

| 类别 | n | D2' 事前判定 | 说明 |
|---|---|---|---|
| C 的 6 笔输家中被 D2' 拦下 | 3 | `still_triggers=False` | SNDK(−$122)/GAP(−$380)/DELL(−$45)，合计省 $547 |
| C 的 6 笔输家中 D2' 放行 | 3 | `still_triggers=True` | AFRM(−$304)/USAR(−$150)/QCOM(−$223)，MFE=0 的「站得住的假突破」 |
| C 的 16 笔赢家中被 D2' 误杀 | 9 | `still_triggers=False` | 含 ROIV(+$155)/META/GTLB/SNOW/CRM/AU/ORCL×2 |
| C 的 16 笔赢家中 D2' 放行 | 7 | `still_triggers=True` | 含 SMTC(+$449)/BE(+$179)/OKTA(+$65)/BULL(+$97)/AMD/ASTS/ORCL |

**关键交叉结论（提出方主张）**：
- D2' 在**最大动量赢家上零误杀**（SMTC/BE/OKTA/BULL 全部放行）——
  这正是 C 规则用静态涨幅轴做不到的区分。
- D2' 拦下 C 的 6 笔输家中的 3 笔，与 C 的拦截集**部分重叠但不相同**。
- C 独有拦截（D2' 放行但 C 会拦）的 3 笔全是 MFE=0 的 `SL_INITIAL` 死亡单
  → 对这类单，静态涨幅门本来也拦不干净（它们 2 分钟后仍站在线上），
  **早期紧 SL（已在岗）是更对的层**：SL_INITIAL 均亏已从 −4.52% 收窄到 −1.96%。

### 实证 6：现役门与 C 的轴/相位对照

| 门 | 状态 | 轴（对比基准） | 相位 | 阈值 |
|---|---|---|---|---|
| `extreme_gap` 过滤 | **现役硬门** | `abs(current − entry_price)/entry_price` = **anchor 延伸** | 触发时刻 | 8%（逆方向额外永久作废） |
| `chase_cap` 禁追线 | **现役硬门** | 禁追线 vs 现价（reprice 越线即拦） | 触发 + reprice 时刻 | 按标的 |
| thesis 漂移作废门 | **现役硬门** | 相对 bridge entry_price 逆方向漂移 | 触发时刻 | 5% |
| `B_long_knife` | **现役硬门**（RFC-068，独立于本影子） | 实时价 vs 真昨收 | **执行时刻，每轮复评**（回升出飞刀区放行） | −5% |
| `A_short_chase`（影子） | record-only | 昨收 | 09:35 快照一次 | +3% |
| `short_chase_ban_shadow` | record-only | `ctx["change_percent"]` | **触发时刻** | +3% |
| `C_long_chase`（影子） | record-only | 昨收 | 09:35 快照一次 | +3% |

**观察**：现役硬门**全部用 anchor 轴 + 触发时刻相位**；A/C 影子是唯一用
「昨收轴 + 早盘快照」的组合。B 规则转正时改用了触发时刻相位（做对了），A/C 没有跟上。

### 实证 7：A 规则的历史裁决（DEB-20260808 §8.3）

`pipeline.py:5530-5534` 注释原文：

> 「SHORT 且当日涨>3% 禁入」硬门在 post-blackout 基线上**残存效应为负 (−$19.80)**，
> 被禁触窗架空：8月逆势亏单全在 9点档，故**不落硬门，仅 shadow 记录**

即：A 规则的硬门版本**已经被否决过一次**，否决理由是「基线叠加后残存为负」。
现在 A 又作为 `premarket_invalidation` 的 A_short_chase 在攒样（首读 n=1，+$6，误杀 100%）。

### 验证命令（第二方请自行复现，勿信提出方转述）

```bash
cd /opt/ftseek/FTSeek-US
# 结算器首读
venv/bin/python scripts/settle_premarket_invalidation.py
cat data/premarket_invalidation_report_latest.json

# C 规则 22 笔明细 + 分桶（实证 1-4）
venv/bin/python - <<'EOF'
import json, sqlite3
rows=[json.loads(l) for l in open('data/premarket_invalidation_shadow.jsonl')]
db=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True); db.row_factory=sqlite3.Row
tr={r["decision_id"]:dict(r) for r in db.execute(
  "SELECT decision_id,realized_pnl,entry_price,entry_time,mfe_pct,exit_reason "
  "FROM trade_records WHERE decision_id IS NOT NULL")}
cs=[r for r in rows if "C_long_chase" in (r.get("would_invalidate") or []) and r.get("decision_id") in tr]
print(len(cs))
for r in sorted(cs,key=lambda x:x["chg_pct"]): print(r["date"],r["symbol"],r["chg_pct"],tr[r["decision_id"]]["realized_pnl"])
EOF

# D2' 交叉覆盖（实证 5）— join key 是 trade_records.id = entry_delay_confirm.trade_id
grep -c entry_delay data/shadow/entry_delay_confirm.jsonl

# A 规则历史裁决原文（实证 7）
sed -n '5530,5565p' app/playbook/pipeline.py
```

---

## 待验证疑点（提出方未能解决，请第二方查证）

- **Q1**：`premarket_price` 字段名 vs 实际取值（开盘后 09:35+ 的 `last_price`）。
  这是命名遗留还是有意为之？若 C 转正，字段语义必须先正名，否则后续 agent 会按
  「盘前价」理解并做出错误推理。**注意 9/8 已有「pre_price 开盘一分钟内蒸发」的实锤**，
  真盘前价不可作为执行参照——这可能正是当初选 09:35 窗的原因（需查立项记录）。
- **Q2**：C 的证据源写的是「Qoder ≥10:00 复算：顺势 LONG −12.22/笔」，
  但影子在 09:35 打标、22 笔 executed 中 **19 笔在 10:00 前入场**。
  证据的时间条件与应用时刻错相（死门检测三路径之②：错相位输入 = 假阳）。
  **请查证**：原始「≥10:00 复算」分析是否真的存在、其口径是什么、
  能否用 `fwd_return_t1` 重跑一次 ≥10:00 入场子集验证 −12.22/笔这个数字。
- **Q3**：`playbook_instructions.atr_pct` 的实际量纲。实证 2 中 `chg/atr_pct`
  得出 125×–238× 的中位数，暗示 `atr_pct` 存的是小数（0.05）而非百分数（5.0）。
  若确认，实证 2 的第三行需重算（除以 100），结论方向可能不变但数值全错。
  **按「列存在≠有数据、量纲必查 DB 分布」纪律，这条必须实查后再用。**
- **Q4**：A 规则与 `short_chase_ban_shadow` 是否应在裁决日**合并为单一证据源**？
  两者阈值同为 +3%、语义同为「逆势追空」，但相位不同（快照 vs 触发时刻）。
  DEB-20260808 已按触发时刻口径否决过硬门版本。若两个影子独立攒样、独立裁决，
  存在「一个 FAIL 一个 PASS → 双转正打架」的风险。台账目前无互引。
- **Q5**：C 若按修正轴（anchor 延伸度）重做，它与现役 `extreme_gap`（8% anchor 轴）
  和 `chase_cap` 的**职责边界**是什么？是否存在「同一意图三道门、阈值各异」的
  SSOT 违反？还是应该直接把 `extreme_gap` 的 8% 收紧（做减法），而不是新增 C_v2？

---

## 可选方案

### 方案 A：不动 C，让预注册门自然否决（提出方当前推荐）

- **核心思路**：影子继续攒样，误杀 68% > 40% 门 → 裁决日自动 KEEP_SAMPLING/FAIL，
  不写任何新代码。同时把修正轴规格写进 C_v2 候选文档备查。
- **优点**：零改动零风险；尊重预注册纪律（不挪门柱）；D2' 已覆盖同批交易，
  等 D2' 裁决日一并处理更省事。
- **缺点**：继续消耗采集/结算资源在一个已被数据否定的轴上（31 天 310 行，
  其中 C 命中 32 行）；A/C 双影子与现役门的冲突关系无人裁决，
  将来某个 agent 可能误按快照口径转正。
- **适用条件**：D2' 在 1–2 周内到裁决点，届时一并处理。

### 方案 B：C 换轴 + 换相位 → C_v2（anchor 延伸度 × 触发时刻 × ATR 归一）

- **核心思路**：把 C 重做为「触发时刻，现价相对 anchor 的延伸度 > k×ATR%」，
  与 B 规则的相位一致（每轮复评，回落即放行）。本质上是**把 `extreme_gap`
  的 8% 固定阈值改成 ATR 归一的动态阈值**。
- **优点**：轴有方向性证据（实证 3 的 [1,3) vs [3,+) 符号翻转）；
  相位与证据源一致（修 Q2）；ATR 归一后不再系统性歧视高波动动量票（修实证 4 的结构性误杀）。
- **缺点**：n=22 且分桶后 n=6–7，**证据强度不足以支持重做**；
  与 `extreme_gap` 职责重叠（Q5 未解决），可能变成第四道追高门；
  需要重新攒样 15+ 交易日，而这段时间 D2' 可能已裁决。
- **适用条件**：Q5 先裁决（合并 vs 新增），且能证明 anchor 轴在**更大样本**上可分。

### 方案 C：退役 A/C，把「追高/追空」职责全交给现役门 + D2'

- **核心思路**：删除 A_short_chase / C_long_chase 两条影子规则（保留 B，因为 B 已有硬门
  且影子在测残存价值，语义清晰）。理由：① chg-vs-昨收轴零区分度（实证 2）；
  ② 现役 `extreme_gap`/`chase_cap`/thesis 漂移门已用 anchor 轴覆盖同意图（实证 6）；
  ③ A 的硬门版本已被 DEB 否决（实证 7），影子继续攒只是在重测已否决的假设；
  ④ D2' 用「入场后 2 分钟价格行为」区分真假突破，是比任何静态涨幅阈值更贴近死因的轴（实证 5）。
- **优点**：**做减法**（符合「删代码消除根因 > 加校验打补丁」纪律）；
  消除双影子双转正风险（Q4 自动解决）；释放采集/结算/watchdog 维护面。
- **缺点**：放弃「用昨收轴度量市场环境」这个维度——如果将来发现
  「整体高开日 vs 平开日」确实影响胜率（regime 层面而非个股层面），
  就没有传感器了。需要论证这个维度确实无价值，而不只是「当前样本看不出」。
- **适用条件**：第二方认同实证 2 的「零区分度」结论，且能回答
  「昨收轴是否应作为 regime 特征（全市场层面）而非个股门禁」。

### 方案 D（提出方未充分展开，留给第二方）：昨收轴上移到 regime 层

- **核心思路**：把「市场整体高开/低开」作为 **regime 特征**（与 VIX/SPY trend 同层）
  而非个股入场门。即：不拦单只票，而是在市场整体高开日调整仓位乘数或收紧全局阈值。
- **待第二方评估**：这是否与现有 `market_regime` / `regime_multiplier`（cautious score=4 → mult 0.5）
  重复？是否有独立信息？

---

## 关键分歧（预判，请第二方直接反驳）

1. **分歧点 1：chg vs 昨收这个轴是否携带信息？**
   - 提出方：不携带（实证 1 非单调 + 实证 2 中心重合）。
   - 可能的反方立场：n=22 太小，非单调可能是噪声；应该用 `fwd_return_t1`
     在**全部指令**（不只 executed）上算该轴的 IC，样本能到 310 行。
     → **这个反驳如果成立，提出方接受**，并请第二方直接跑这个 IC。
   - 可验证方式：对 310 行影子数据 join `playbook_instructions.fwd_return_t1_adj`
     算 chg 分位的 IC/Spearman，样本量足够。

2. **分歧点 2：误杀 68% 是「轴错」还是「阈值错」？**
   - 提出方：轴错（阈值从 3% 调到 6%/10% 都改变不了非单调结构）。
   - 可能的反方立场：阈值 + 方向组合可以修（例如只拦 chg>10% 的极端桶，
     实证 1 的 [10,+) 桶 net=−$320）。
     → 提出方部分认同，但 [10,+) 桶胜率 6/8=75% 仍高于均值，净负靠 2 笔大亏
     （AFRM −$304 / GAP −$380）驱动，**是尾部风险问题不是胜率问题**，
     应该用仓位控制而非入场门解决。
   - 可验证方式：把 [10,+) 桶的 8 笔逐笔列出，看净负是否由 ≤2 笔驱动（是 → 尾部问题）。

3. **分歧点 3：C 是否已被 D2' 完全取代？**
   - 提出方：功能上大部分重叠（实证 5：D2' 在最大赢家上零误杀，C 全杀），
     但 D2' 也漏掉 3 笔 MFE=0 死亡单 → **两者都不能单独解决墓地**，
     真正的层是「入场后紧 SL」（已在岗）+「源头供给质量」（UE LONG −$780）。
   - 可能的反方立场：D2' 自身全体净贡献仅 +$370/60天（很薄），
     不足以承担「取代 C」的论证重量；且 D2' 的 forward 窗才攒到 n=4。
     → 提出方接受这个反驳的力度：**不能用一个未裁决的影子去论证另一个影子该退役**。
   - 可验证方式：等 D2' forward 到 n≥20 再看。

4. **分歧点 4：A/C 双影子并行是否构成真实的裁决风险？**
   - 提出方：是（Q4，无台账互引，相位不同可能给出矛盾结论）。
   - 可能的反方立场：不是——两个影子测的是不同问题（快照 vs 触发时刻），
     矛盾结论本身就是有价值的信息（说明相位重要）。
   - 可验证方式：查 `docs/CONTROL-LOOP-LEDGER.md` 是否有互引；
     查两个影子的样本重叠率（同一 decision_id 是否都被记录）。

---

## 决策

- 选择方案：**用户已裁（2026-09-11「按建议执行」）= 方案 C 的两段式变体 + 方案 A′ 死线合并，P0 与 P1 全量执行；P2 未执行**
  - 第一段（P0，纯测量/治理层，零交易面）：✅ 分母审计（只读）→ ✅ 补登记
    `short_chase_ban_shadow` → ✅ 落「同意图互引 + 分母哨兵」守卫 → ✅ 本文勘误。
    **注**：原 P0 措辞含"修 ctx 侧陈旧昨收分母"，该**修复**动作经执行时定性属生产取数
    路径改动，已划归 P2-1 未执行；本轮只落"让分叉当日可见"的 W7 哨兵与污染期标注。
  - 第二段（P1）：✅ 退役 `C_long_chase` 采集；✅ `A_short_chase` 快照规则退役，
    其**假设**并入 ban-shadow（触发时刻相位）作为单一证据源（注册表 `verdict_history` +
    `cross_ref` 双向互指留痕），A 快照行不得独立转正；✅ 保留 `B_long_knife`
    （已验证、现役、分母走日K注入侧=干净）。
  - 否决项：方案 B（C_v2）冻结 —— 会成第四道追高门且 Q5 未裁；
    方案 D（昨收轴上移 regime）不落地 —— 先做 IC 验证，且其输入依赖同批污染数据。
  - 死线：A/C 的合并处置挂在 **D2' 裁决日**（forward 实测 13/20，约 1–2 周）；
    ban-shadow 预注册死线 10/31 不变，但**分母修复前的样本须标注污染期**，
    否则 10/31 到点裁的是废样（已写进注册表 `graduation_criteria` 前置条件）。
- 决策依据：见文末「共识清单」C1–C9（每条附复现命令或代码行号）。
  核心三条：① chg 轴有信息但 **C 把方向用反了**（BUY 侧 rho=+0.151、C 命中集指令口径
  fwd +4.90% vs 阈下 +1.60%）；② 「极端涨幅桶危险」由**1 笔过夜跳空**驱动
  （GAP −$380.3，同桶其余 7 笔日内 +$60.6）；③ A 的旧否决前提已失效
  （禁触窗 8/15 由 1015→0935，git `1d2b909d`），但其正确载体不是 09:35 快照。
- 保留空间：若用户倾向「先不动、继续攒样」，最低必须做 P0 的分母审计与 ban-shadow 登记
  （否则两个影子在裁决日都会给出不可解释的结论）。
- 负责人：本会话（team-lead）收口写入；并行会话分工 reviewer-kimi（#5 结算器/#8 回归）、
  verifier-glm（#6 W7 哨兵）、architect-qwen（#4 注册表起步）。#9 台账/本文回写由
  team-lead 独占写入权（避免同文件双写）。
- 截止时间：P0/P1 已于 2026-09-11 当日落地；后续节点 = D2' 裁决日（A/C 合并处置复核）
  与 ban-shadow 10/31 死线。
- **辩论阶段（两轮收敛前）零改动**：全部结论由只读复现（SQLite mode=ro、文件读取、
  只读 git）产出。**执行阶段（用户裁「按建议执行」后）改动清单**：
  `app/playbook/premarket_invalidation.py`（`_RULES` 仅存 B + 字段语义正名/评估窗不得前移的
  docstring 约束）、`app/playbook/shadow_registry.py`（`intent`/`cross_ref` 两字段 +
  ban-shadow 补登记 + `check_intent_uniqueness()`）、`ops/hermes/ftseek_weekly_checkpoint.py`
  （周检接 `intents` 键）、`scripts/settle_premarket_invalidation.py`（`RETIRED_RULES` 退役闸 +
  `_conflict_check` + conflict_check 进 latest.json）、`scripts/daily_invariant_check.py`（W7 哨兵）、
  `scripts/data_freshness_watchdog.py`（描述口径更正）、`docs/CONTROL-LOOP-LEDGER.md`
  （§2.5 两行 + §5 G3 更新/G8 新增 + L117 注记）、本文、`data/chg_denominator_baseline.json`（新，
  首跑锁 90 存量键）、契约钉 4 文件（settler/premarket 改写 + 意图守卫/W7 新增）。
- **生效边界（诚实登记，两条）**：①A/C 退役是**采集侧 record-only** 改动 —— 只停"打标"，
  零交易行为变更，未触碰任何现役硬门（`extreme_gap`/`chase_cap`/thesis 漂移/`B_long_knife`）
  与退出参数（DEB-20260909 冻结期内）；②**服务尚未重启** ⇒ 线上进程仍按旧三规则打标，
  直到重启加载新 `_RULES`（Python 代码只在 import 时加载，台账 §2.5 deploy_freshness 同课）。
  重启窗口、git 入库、周检线上副本部署三件事均**待用户裁**，本轮未做。

## 执行项

（辩论收敛后填写，每条须附 grep/SQL 验证结果；未过验证的降级 P2）

- [x] **P0-1 分母审计（只读，零风险）**：核 `ban-shadow` 的 `ctx["change_percent"]`
  陈旧范围。验证：BIDU 9/8 inst2353 两行 7 秒内 −7.65% vs +4.07%；
  `change_percent`/`volume` 在 09:35:25 与 10:47:00 完全冻结；
  同窗可比 38 对中 21 对（55%）|Δchg|>3pp；干净侧对照 SMTC 8/26 prev_close=127.52
  = 日线真值 127.52。复现：`grep '"US.BIDU"' data/entry_timing_shadow.jsonl`；
  `sed -n '509,534p' app/data/market_data_router.py`
  **审计结论（9/11 落地）**：`prev_close` 实为**三源并存** — ①快照
  `get_market_snapshot.prev_close_price`（干净）②`get_quote` 推送缓存（**污染源**，跨日冻结）
  ③RFC-068 日K自算注入 `ctx["prev_close"]`（`market_data_router.py:509-534`，干净）。
  **现役 `B_long_knife` 硬门走③，不在污染面内**（架构师第二轮主张"B 门输入也被污染"
  经三路径核实后**已撤回**）⇒ 本议题全程留在测量层，零现役硬门受影响。
  全量对账（W7 首跑，195 可比对）：**90 对（46%）差 >2pp**，窗参数不敏感
  （5/10/15min 窗发散数恒 90 — 分母分叉是全日冻结型）。已入账台账 §5 **G8**。
- [x] **P0-2 补登记 ban-shadow**：`app/playbook/shadow_registry.py`、watchdog、
  `docs/CONTROL-LOOP-LEDGER.md` 三处均零命中（「建了没登」第 4 例）。
  验证：`grep -rn short_chase_ban app/playbook/shadow_registry.py docs/CONTROL-LOOP-LEDGER.md` → 空
  **落地（9/11）**：注册表新增 `id="short_chase_ban_shadow"`（`config_key` 用嵌套真键
  `playbook.execution_parameters.short_chase_ban_shadow_enabled` — top-level 写法是死键；
  `eval_on_demand=True` 无独立结算器；`min_n=20`；预注册判据含**死线 2026-10-31** 与
  「分母修复前样本=污染期，是否计入 n 由裁决日显式裁」前置条件；`verdict_history` 两条留痕
  8/8 旧基线否决 → 9/11 归并为 SHORT 追空意图单一证据源）；`intent="short_chase_ban"` 与
  `premarket_invalidation`（`intent="long_knife_gate"`）`cross_ref` **双向互指**。
  台账：§5 G8 附带登记 + §2.5 意图守卫行。
  **一处与原执行项不同的裁定（诚实登记）**：watchdog 面**不补** —
  `data_freshness_watchdog.py` 的阈值是小时级，而本影子 18 天仅 5 行（8/24 NTES、9/1 MSTR、
  9/3 DDOG、9/8 BIDU×2），入表即永久误报；断流改由注册表 `check_mounting` 的
  **14 天静默哨兵**接管（登记后自动生效）。该影子同时被记为"采集濒死"：按此节奏
  10/31 死线大概率裁 KILLED 而非达标，且行内无 `date` 字段、`direction` 在写者处硬编码
  `'SHORT'`（`pipeline.py:5549`）、只能靠 `inst_id` join 而该键有脏对
  （NTES 8/24 inst1535 同日 DA BUY 09:35 / reentry SHORT 10:55）⇒ 三项须裁决前先修。
- [x] **P0-3 同意图守卫落地（三处落点）**：`shadow_registry.py:46` 增 `intent`+`cross_ref`
  并钉「同意图且均 OBSERVING ⇒ 必须互指」；`scripts/settle_premarket_invalidation.py:112-120`
  的 READY_TO_ADJUDICATE 分支强制输出 conflict_check（缺则降级 KEEP_SAMPLING）；
  `scripts/daily_invariant_check.py` 增分母哨兵（两 jsonl 按 (symbol,date) join，
  |Δchg|>2pp 即 violation）。
  **落点①已落地（9/11）**：`ShadowItem` 增 `intent: str = ""` / `cross_ref: tuple = ()`
  （`shadow_registry.py:75-76`，字段注释写明立起理由）；新增 `check_intent_uniqueness()`
  （同文件 `:1262`）**五判定** — ①同意图现役项必须互指 ②互引目标必须在册（悬空即报）
  ③目标现役必须回指（**单向即报**，补①盲区：本仓真实那对 intent 标签不同，①不触发）
  ④`REGISTRY` 内 id 必须唯一（**同日真实事故**：并发补登记一度产出同 id 两条，
  `summary()` 重复出行、互引只认一条）⑤守卫自身异常产 `intent_uniqueness_detector_error`
  （§6 规则5，"零违反"≠"守卫崩了"）。已接入周六周检
  （`ops/hermes/ftseek_weekly_checkpoint.py`：import + `'intents': check_intent_uniqueness()`
  + 渲染循环与 mounts 并列）— 防"函数建了没人读"的断头传感。现网零告警；
  契约钉 `tests/test_shadow_intent_uniqueness.py` 16 例。
  **落点②已落地（9/11）**：settle 脚本 `RETIRED_RULES` 恒判 A/C
  RETIRED_NO_PROMOTION（历史行仍结算呈报，实跑 A=1行/C=32行照常出数）；
  READY 前置 `_conflict_check`（未互引/注册表不可读→降级 KEEP_SAMPLING+stderr）。
  契约钉 TestRetiredGate/TestConflictCheck 共 5 例，17/17 通过。
  **追加（同日）**：`_verdict(rule, a, conflict=None)` 第三参可选（两参调用与 monkeypatch
  形态不变），`main()` 一次计算两处复用，并把结果写进
  `latest.json → rules[k].conflict_check`（`{ok, detail, refs}`，退役规则为 null）—
  原先降级只留 cron stderr，则"被冲突核对挡下"与"四关没过"在产物里长得一模一样，
  裁决日不可审计。实跑确认 B = `{ok: True, detail: "互引齐: ['RFC-068']"}`。
  **落点③已落地（9/11）**：`scripts/daily_invariant_check.py` 新增 `W7` 分母哨兵
  （`_check_chg_denominator`，随 I11~I14 同班 cron `5 5 * * 2-6`，接线进 `main()`、
  结果落 `result["chg_denominator"]`）。join 键 **inst_id 精确优先、(symbol,date) 回退**
  （比原执行项的单一 (symbol,date) 更严，防跨指令串染）；±10min 可比窗内取**最小** |Δchg|，
  >2pp 即 `W7_chg_denominator_divergence`。棘轮基线 `data/chg_denominator_baseline.json`
  首跑锁 90 存量键（BIDU 9/8 带本文出处入 `refs`）：存量 silenced_known、**新增才告警**、
  愈合只报不自动改写基线（守卫绝不自我加宽）；影子档缺失 `skipped_no_shadow`、
  单行坏跳过计数、整档不可读 `W7_detector_error`。
  契约钉 `tests/test_w7_chg_denominator_sentinel.py` 12 例（含 BIDU 脏对当晚告警、
  DDOG Δ0.17pp 健康对照零误报、窗外行/跨 inst_id 两个防假阳钉、棘轮只降不升）。
  台账 §2.5 已入账两行（W7 + 意图守卫）。
- [x] **P0-4 本文勘误**：已随本章节落档（见勘误表 E1–E8）。
  **追加（9/11）**：状态块的数据基线窗口按 E2 更正为 8/11–9/10（21 个交易日、39 flagged 行）。
- [x] **P1-1 退役 C 采集**（9/11 落地：`premarket_invalidation.py` `_RULES` 仅存 B；
  验证 `evaluate_rules("BUY", 5.0) == []` 且 B 硬门 SSOT 路径
  `evaluate_rules("BUY", -6.0)` 仍命中 B_long_knife；310 行 jsonl 未删）：删 `premarket_invalidation.py:29-38` 的 `C_long_chase` 规则行，
  归档 310 行 jsonl（**不删数据**）。验证：影子产物读者仅结算器/watchdog/ops 校验，
  无交易路径消费 → 前后行为等价；注意不可直接关 observe 总开关
  （`config/unified_config.yaml:1025` 会连 B 采集一起停）。
- [x] **P1-2 A 快照规则退役**（同批落地，规则行已删；ban-shadow 修复+补登记
  属 P0-2，由注册表任务承接）：同文件同轴同相位，随 C 一起处理；
  重测须在 0935 禁触窗新基线上跑（旧 −$19.80 残存效应不可直接引用，
  git `1d2b909d`）。
- [ ] **P2-1（本辩论权限外，需另立预注册）**：`prev_close` 单源化修复 B 门日K缺失回落分支、
  `extreme_gap` 8% 收紧、方案 D 落地、C_v2。
  **本轮（9/11 用户裁「按建议执行」）明确未执行 — 一行代码未动。** 理由：四项全部触碰
  **现役硬门或生产取数路径**（`extreme_gap`/`chase_cap`/thesis 漂移/`B_long_knife` 与
  `ctx["change_percent"]` 的写入侧），超出本辩论"测量层"权限，且未经预注册即改门 =
  挪门柱。当前替代防线：①`W7` 哨兵让分母分叉**当日可见**（基线外新增即告警）；
  ②注册表 `short_chase_ban_shadow` 把"分母修复前样本=污染期"写成转正**前置条件**，
  使 10/31 死线裁决不会拿废样当真样。解锁条件：另立预注册 DEB（判据写死于读数前）+ 用户裁。
- [ ] **P2-2（另开议题）**：chg>10% 桶净负由单笔 `OVERNIGHT_GAP_STOP` 驱动 →
  过夜跳空是否应由过夜规则/仓位处理，属退出与持仓层，不在本辩论权限内。
  **本轮明确未执行**：属退出/持仓层（且退出参数在 DEB-20260909 冻结期内，
  裁决窗口 9/12–9/16），须另开议题；本辩论只登记事实 —
  该桶全窗净 −$319.6 中 −$380.3 来自一笔过夜跳空，其余 7 笔日内合计 **+$60.6**
  ⇒ 以该桶为据收紧日内追高门属**归因错配**（E1/E3 同源教训：先分清毛净与昼夜，再谈门）。

---

## 附：本辩论不涉及的边界（防止范围蔓延）

- **不改任何现役硬门**（extreme_gap / chase_cap / thesis 漂移 / B_long_knife RFC-068）。
- **不动退出参数**（DEB-20260909 已冻结，`be0.3/dd0.3` 已裁不落地，
  `tiered_dd0.5_tier1.5` 攒样中，裁决窗口 9/12–9/16）。
- **不动 D2'/E3 的裁决时序**（RFC-081 已预置两案落地设计，等各自影子到点）。
- **不动 REENTRY 通道**（n=21、净均值 −$5.64，约 1 周后到 n=30 预注册裁决点，
  按判据自然裁决，本辩论不提前干预）。
- 本辩论的输出**只应是测量层/规格层的决定**（换轴、换相位、退役、合并裁决），
  任何「立刻上硬门」的结论都需要另开预注册攒样，不在本辩论权限内。

---

# 第二方/第三方辩论记录（2026-09-11，多模型两轮）

## 参与方与分工

| 方 | 模型 | 分工 | 产出性质 |
|---|---|---|---|
| 提出方 Hermes | qwen3.8-max | 开案（本文正文） | 主张 |
| 实证方 | GLM-5.3 | 复跑全部验证命令、逐条比对数字、查 Q1–Q4、跑 310 行 IC、追昨收分母调用链、只读 git 核配置史 | 事实裁决 |
| 审查方 | Kimi-K3 | 证据链/统计诚实性/越界主张审查、逐条对代码核实 | 论证裁决 |
| 架构方 | Qwen3.8-Max | 根因定位、门职责链与 SSOT、双影子治理风险、动作爆炸半径分级 | 结构裁决 |

轮次：第一轮三方互不可见独立成文；第二轮交叉转达彼此结论并要求「坚持/修正/撤回」三栏表态；
第三轮为单项追问（B 硬门分母归属）。**全程只读**（SQLite `mode=ro`、文件读取、只读 git），
未改动任何代码、配置或影子产物；本文正文原有内容未被改写，勘误以本章为准。

## 勘误表（正文需更正处，E1–E8）

| # | 位置 | 正文原文 | 实测/核实 | 判定 |
|---|---|---|---|---|
| E1 | L41 首读叙述 | 16 赢 +$1,157 / 6 输 −$1,224 / 净 ≈−$67 | 毛口径 16 赢 +$1,185.4 / 6 输 −$1,223.9 / 净 **−$38.5**；**净口径（结算器注册口径 `COALESCE(pnl_net,realized_pnl)`）15 赢 / 7 输 / 净 −$152.49 / 误杀 15/22=68.2%** | 三组互斥数字并存。「16 赢家」与「68%」在任何单一口径下不能同时成立；−$67 两口径都算不出 |
| E2 | L12 数据基线 | 310 行（8/11–9/11） | 310 行 ✓，但日期范围实为 **8/11–9/10** | 日期终值有偏 |
| E3 | L139-141 实证4 | 6 笔输家中 3 笔 MFE=0+SL_INITIAL、另 3 笔 MFE>0 回吐 | MFE=0 共 **5 笔**（SNDK/USAR/QCOM/AFRM/GAP；4 笔 SL_INITIAL、GAP 为 OVERNIGHT_GAP_STOP），MFE>0 仅 **DELL 1 笔**（1.31%）。AFRM 被误归入「回吐」组 | 分组错。注意：这**加强**而非削弱「C 管不了退出层」的论点（入场即错的单更多） |
| E4 | L153-155 实证5 | 赢家被 D2' 误杀 9 / 放行 7 | 实际 **7 / 9 互换**（毛口径误杀 7、净口径 6）；「ORCL×2」不实，实际仅 1 笔 ORCL 被拦。输家两格 3/3 ✓，22/22 覆盖 ✓，最大赢家零误杀 ✓ | 计数反了，点名标的列表与分组一致 |
| E5 | L90-93 实证2 第三行 | chg/ATR% = 125×–238× | **Q3 成立**：`atr_pct` 存小数（n=720，min 0.0064 / med 0.0359 / max 0.1538），÷100 后为 **1.26×–2.38×**；方向不变（输家延伸更高） | 数值错 100 倍，结论方向不变 |
| E6 | L177-178 实证6 | 「现役硬门**全部**用 anchor 轴 + 触发时刻相位」 | 假命题：`B_long_knife` 现役硬门就是**昨收轴**（实时价 vs prev_close <−5%，`pipeline.py:3914-3920`）。成立的只有「相位」结论（快照 vs 执行/触发时刻） | 全称判断错，连带方案 C 理由②失据（昨收轴未被整体取代） |
| E7 | L249 方案A | 「裁决日自动 KEEP_SAMPLING/FAIL → 不支持转正」 | 结算器 verdict 只有 `KEEP_SAMPLING` / `READY_TO_ADJUDICATE` 两态，且明示「提请转正另裁，不自动生效」（`scripts/settle_premarket_invalidation.py:112-120`）。**不存在自动否决机制** | 方案 A 的「自然否决」= 永不转正且永不退役的无限攒样，不是零风险 |
| E8 | L180-188 实证7 / L318 | 引文原文 + 「D2' forward 窗才攒到 n=4」 | 引文原文核对无误（−$19.80 / 禁触窗架空 / 8月逆势亏单全在9点档），DEB-20260808 L164 亦有。但**否决前提已失效**（见 C8）；D2' forward 实测 **n=13**（9/9:5 + 9/10:8，delay=2 status=ok），仍 <min_n 20 | 引文属实、时效未审；n=4 偏低 |

已核实**无误**的正文数字（供后续引用）：实证1 分桶四格（−80/+125/+235/−320，胜率 3/4、4/4、3/6、6/8）、
实证2 前两行、实证3 分桶四格（−206/+33/+514/−379）、实证4 逐笔 22 行 pnl 与 entry_t/slip%、
`vs_trig` 实测区间 [−0.34%, +0.16%]（以 `inst.entry_price` 为参照）、最大赢家全在 chg>+6%、
A 首读 n=1/+$6.2/误杀 100%、C 命中 32 行、`SL_TRAIL_LOCK` 30 天 n=58 净 **+$3,909.56**。
两处**未验证、勿作论据**：L144「UE LONG −$780」（`trade_records` 无 UE source 枚举，未找到出处）；
实证4 `trig` 列的统一来源（AU=119.63 不匹配任何 DB 字段，`inst.entry_price`=120.18，疑取自分钟 bar，未深挖 replay_bars）。

## 共识清单（C1–C9，三方一致或经第二轮收敛）

**C1 影子的事实骨架可靠。** `_RULES` 三规则阈值确为硬编码（`premarket_invalidation.py:29-38`），
评估窗 `_WINDOW=(935,959)` 每日一次，record-only 名副其实（产物读者仅结算器/watchdog/ops 校验脚本，
**无任何交易路径消费**），B 门是独立硬门且只取 `B_long_knife`（A/C 无硬门），
D2' 对 C 命中 22 笔 22/22 覆盖。

**C2 chg vs 昨收轴携带信息，但 C 把方向用反了 —— 不是「零区分度」。**
310 行影子按 `inst_id` join `playbook_instructions.fwd_return_t1_adj`（295/310 有值；
该列为方向化机制调整收益，SHORT 对称、正=赚，`ic_tracker.py:190`），Spearman：

| 分层 | n | chg 轴 rho | p | anchor 延伸轴 rho | p |
|---|---|---|---|---|---|
| 整体 | 295 | +0.043 | ≈0.46 | +0.103 | ≈0.08 |
| BUY | 242 | **+0.151** | ≈0.018 | **+0.284** | <0.001 |
| SELL_SHORT | 53 | **−0.378** | ≈0.005 | **−0.660** | <0.001 |
| executed | 184 | +0.054 | ≈0.47 | +0.125 | ≈0.09 |
| 未 executed | 111 | +0.036 | ≈0.70 | +0.036 | ≈0.71 |
| BUY & executed | 162 | +0.128 | ≈0.11 | +0.205 | ≈0.009 |
| BUY & 未 executed | 80 | +0.230 | ≈0.041 | +0.408 | <0.001 |

chg 五分位 fwd 均值：Q1[−22.1,−2.0%] +2.47% / Q2 +1.56% / Q3 +1.21% / Q4 +1.71% /
**Q5[+1.74,+109.6%] +3.80%**（最高分位最好）。阈值对照：**BUY chg>+3%（C 拦截集 n=32）fwd 均 +4.90%
vs 阈下（n=210）+1.60%**。executed/未 executed 分层无翻转 → 无选择偏差。
提出方 L70「样本再大也分不开」应撤回为：**在已成交实盘口径上无可分性，在指令口径上方向相反**。

**C3 信息量排序：anchor 延伸轴 > chg 轴**（所有分层 |rho| 约 1.7–2 倍）。
真变量是「入场价相对锚的延伸/滑点」，不是「距昨收涨幅」。这也解释了口径裂缝：
`fwd_return_t1_adj` 从**指令锚价**起算、不含追高滑点，而 C 命中单实际入场常高于锚 0.9–5.4%
（实证4 slip 列）→ 指令口径 +4.90% 与 executed 实盘净 −$152.49 并存，两者不矛盾。

**C4 「极端涨幅桶危险」不能作为入场门依据。** chg>10% 那 8 笔净 −$319.6 拆开：
**过夜 1 笔（GAP，`OVERNIGHT_GAP_STOP`，8/28 入场 → 8/31 出场）= −$380.3，其余 7 笔日内合计 +$60.6**。
即该桶的全部净负由一笔过夜跳空撑起（提出方原说「2 笔驱动」，收紧为 1 笔）→
政策变量是**过夜规则/仓位控制**，不是入场追高门。

**C5 误杀 68% 的正确读法与结论保留。** 注册口径（净）15/22=68.2% > 预注册门 ≤40% →
**C 不支持转正**，三方一致。但成立的理由应是 C2（方向用反）+ C4（极端桶由单笔过夜驱动）+
C3（真变量是延伸/滑点），而非「轴零信息」。

**C6 昨收分母存在两套来源，脏的是 ctx 侧 —— 定性为数据口径 bug，不是相位差。**
- 干净侧：A/C 影子的 `get_snapshot().prev_close_price`（`premarket_invalidation.py:97-112` →
  `futu_lv3_provider.py:1707-1735`）。真值对照：SMTC 8/26 影子 prev_close=**127.52** =
  replay 日线 8/25 真实收盘 **127.52**。
- 脏侧：`ban-shadow` 的 `ctx["change_percent"]`（`pipeline.py:5541` ← `get_full_market_context`
  → `futu.get_quote` 的 push 缓存优先路径，`futu_lv3_provider.py:915-932`）。
  实锤：BIDU 9/8 inst2353，09:35:17 影子 −7.65%（prev_close=99.47）vs 09:35:24 ban-shadow +4.07%，
  7 秒反号；两源**价格一致**（91.86 vs 91.85）→ 只能是分母不同（99.47 vs 隐含 ≈88.26）；
  且 BIDU 的 `change_percent`/`volume` 在 09:35:25 与 10:47:00 **完全冻结**（4.07 / 2578698）。
- 影响面：09:35–09:36 同窗可比 38 对中 **21 对（55%）|Δchg|>3pp 且符号频繁翻转**
  （HOOD −1.18 vs +13.7、AMD +4.34 vs −3.49）；ban-shadow 全量 5 行中 BIDU 2 行按错误 chg 攒样
  （记录 +4.07，真实 −7.65）。污染所有消费 `ctx["change_percent"]` 的影子
  （`short_chase_ban_shadow`、`entry_timing_shadow` 等）。

**C7 由 C6 解除/撤回两项第一轮的判断。**
① 架构方第一轮把「C 退役」降级为「等分母审计后定」的理由**解除** —— C 的分桶用的是干净分母，
实证1/2/3 的既有数字不作废，C 可以现在退役。
② 架构方「③ B 硬门输入同病」的影响面判断**撤回** —— B 门虽与 ban-shadow 共用同一个 ctx 字典，
但读的是 `ctx["price"]` + `ctx["prev_close"]`，**不读 `change_percent`**；而 `prev_close` 键由
`market_data_router.py:509-534`「RFC-068 审计修复（2026-08-29）」的**日K自算真昨收**独立注入
（注释明记「不用快照 prev_close_price — 盘前概率性返回两天前收盘，62% 实测」，仅日K全不可得才回落脏侧）。
即该注入恰是对 push 缓存陈旧问题的修复产物。+$302.97 的转正反事实用「成交价/真昨收」独立重算
（RFC-068 勘误2，且已自纠 SNDK 影子 −4.54% 的陈旧昨收假象）→ 不受污染，无需加可信度标注。
线上唯一拦截案例 TECK 9/10（chg −7.72→−6.08 随时间演化，证时价新鲜、逐轮复评）与干净参照
（pmi 09:35 chg=−7.51）差 <0.3pp，无 |Δ|>3pp 案例。
保留两个 caveat：日K缺失回落分支仍可能触脏（fail-open 放行，风险有限）；
**评估窗不可前移到盘前**（见 Q1）。

**C8 A 规则的旧否决前提已失效，但重测载体不是 A 快照行。**
只读 git 核实禁触窗变更链：`1817eec7`（8/8，RFC-063 上线开盘禁触窗 10:00）→
`ebdb7069`（8/12，10:00→10:15）→ **`1d2b909d`（8/15，ADR-028 用户裁，10:15→09:35 对齐实盘）**；
`3fca11a9` 显示回退到 1015 的旧 patch 已被物理废弃。当前值
`config/unified_config.yaml:1127 entry_open_blackout_hhmm: 0935`（enabled=true）。
DEB-20260808 裁决当日窗为 10:00，「8月逆势亏单全在9点档被禁触窗架空」的前提 8/15 后不成立 →
**−$19.80 残存效应不可直接引用**。同时 SHORT 侧 chg 轴 rho=−0.378（p≈0.005）说明
「逆势追空差」的**假设方向被数据支持**。结论：保留意图、换载体（修复并重注册后的 ban-shadow，
触发时刻相位 = B 转正先例），**A 快照行不得独立转正**。

**C9 双影子并行的治理风险真实且已兑现，级别高于「相位差」。**
同一意图的两把尺子**连分母都不一致**（C6），矛盾结论已在真实数据发生（BIDU），
不是「未来可能矛盾」。且 `ban-shadow` 作为传感器已接近坏死：全量仅 5 行、
`shadow_registry`/watchdog/`CONTROL-LOOP-LEDGER` **三处零登记**、`direction` 字段硬编码 `"SHORT"`
（`pipeline.py:5549`）、无 `decision_id` 字段、NTES 8/24 同一 inst_id 出现 DA BUY（09:35）与
reentry SHORT（10:55）两方向 → inst_id 作 join key 有脏数据。
属**系统性问题**（「建了没登」第 4 例，`CONTROL-LOOP-LEDGER.md:143-146` 已记前三例；
同形例：DA confidence 双算法（signal_gate:383-404 vs adapter:191-254）、KR2 冻结双尺子、
earnings_calendar 同名双载体）。共同根因：**守卫只强制「存在性登记」，无机制强制「意图唯一性」**。

## 未收敛分歧（含反转条件）

- **D1 C 的退役时点**：审查方主张立即退役采集；架构方主张审计后两段式。
  主持人合并意见：C6/C7 已使 C 侧分母清白 → **C 可现在退役，但先跑完 P0-1 审计**（成本近零），
  避免「退役」与「修 bug」两件事在时间上纠缠、日后无法归因。
  *反转条件*：若审计发现 A/C 影子在评估窗内也存在跨日 prev_close 污染，则 22 笔分桶需重算后才能退役。
- **D2 A 假设重测的投入优先级**：三方同意「重测 A」是新攒样而非延续旧影子，
  但对是否值得在 10/31 前投入 ban-shadow 修复工时未表态。
  *反转条件*：SHORT 侧 rho=−0.378 若在修复分母后仍稳定 → 值得投入；
  若 ban-shadow 修复需改 push 缓存订阅链路（成本过高）→ SHORT 侧改由快照口径继续攒
  （相位缺陷已知但分母干净），并在台账标注该权衡。
- **D3 方案 D（昨收轴上移 regime）的前置验证数据源**：三方同意「先验 IC、不落地」，
  但对用哪批数据验 IC 有分歧（实证方：ctx 侧污染使 D 的输入同样需先修）。
  架构方查实 `market_regime.py` 评分输入仅 SPY MA20/50/200 + VIX（:217-259），
  `spy_change_pct` 被采集（:152,:310）但**评分零消费** → 「全市场高开」确非现有 regime 输入，**D 不重复**；
  但 regime 在盘前生成（TTL 缓存 :77-79）而全市场高开 09:30 后才可知，落地需新增盘中复评环并
  触碰现役 `position_multiplier` 路径（`pipeline.py:1667`），且 LEDGER G3（:210）明记 regime 分类器自身 IC 无人测。

## 四个预判分歧点的裁决

1. **chg 轴是否携带信息** → 判给反方（见 C2/C3）。提出方在 L298-302 预留的
   「这个反驳如果成立，提出方接受」条件已满足：310 行 IC 实跑完成，BUY 侧 rho=+0.151 显著、
   C 命中集指令口径 fwd +4.90% vs 阈下 +1.60%。轴有信息，**C 用它做了反方向的事**。
2. **轴错还是阈值错** → 两者都不是（见 C4）。审查方「轴和阈值都答错了题」获支持；
   提出方「阈值从 3% 调到 6%/10% 改变不了非单调结构」被 IC 部分反驳
   （指令口径单调性存在，只是方向相反；实盘非单调由延伸/滑点与单笔过夜制造）。
3. **C 是否已被 D2' 取代** → **绕开而非裁决**。三方一致同意提出方的自我退让：
   不能用未裁决的影子论证另一影子该退役（D2' forward 实测 13，仍 <20，判据未到点）。
   但 C 退役的依据独立成立（C2/C4/C5），与 D2' 无关 → 该分歧点对决策不再关键。
4. **双影子并行是否构成真实裁决风险** → 判给提出方，且实测比正文描述更重（见 C6/C9）。
   反方「矛盾结论本身是有价值信息」部分成立：矛盾确实产出了分母 bug 这一发现，
   但代价是 ban-shadow 攒样与 A 的对照证据在修复前**不可用**。

## Q1–Q5 的第二方回答

- **Q1 命名与语义不符：成立。** `chg=(snapshot.last_price − snapshot.prev_close)/prev_close×100`，
  窗 09:35–09:59 属常规时段（`premarket_invalidation.py:97-124`），不是盘前价。
  立项记录仓库内**无**（8/12 复盘仅「首跑 9 只 0 作废」；「9/8 pre_price 蒸发实锤」未在仓库找到，
  可能在会话日志）→ 标【证据不足】。但机制性理由已由 `market_data_router.py:509-534` 注释给出：
  快照 `prev_close_price` 在**盘前窗 62% 返回两天前收盘** → **评估窗不可前移到盘前**，
  否则 A/C 分母直接命中该缺陷。若将来改窗（C_v2 或 A 重测），这是硬约束。
- **Q2 证据源存在但不可复现，错相位确认。** 「≥10:00 逆势 LONG +3.21 vs 顺势 −12.22」
  确在 `DEB-20260808-signal-source-strategy.md` L246/494/521（Qoder 8/8 复算），
  但仅一行引用、无口径存档 → 原数字【不可复现】。19/22 在 10:00 前入场 ✓。
  当前数据近似重算**方向相反**：8/11–9/10、LONG ≥10:00 入场，顺势（chg>0）n=20 均 **−$1.18/笔**
  vs 逆势 n=19 均 **−$12.89/笔**（chg>+3% 子集 n=3 均 −$34.88）；原始窗口 8/4–8/8 的 ≥10:00 LONG 现存仅 n=4。
  → C 的证据源**不能**用作支持 09:35 快照口径的依据。
- **Q3 确认小数**（E5）：`atr_pct` 量纲为小数，实证2 第三行须 ÷100，方向不变。
- **Q4 同意合并为单一证据源，但顺序必须是「先修分母 + 先登记，再谈合并裁决」。**
  台账互引缺失确认（LEDGER 仅 L117 结算器条目；ban-shadow 零条目、不在 shadow_registry）。
  样本重叠：A 命中仅 1 行（DDOG 9/3 inst2172，A 侧 +3.27 / ban 侧 +3.44，同判「拦」，覆盖率 1/1）；
  矛盾结论已在 BIDU 9/8 真实发生（C6）。
- **Q5 不建 C_v2，做减法。** 追高侧责任链已有四道 anchor 轴硬门：
  `extreme_gap` 8%（`pipeline.py:5576-5607`，逆方向额外永久作废）、thesis 漂移 5%（:5641-5649）、
  `chase_cap`（:4250-4382，9/5 转正、fail-close）、entry band（:5362-5452，widen 上限 8% = extreme_gap 线）。
  C_v2 会成第五道且踩 SSOT。`extreme_gap` 的 8% 是否收紧属**现役硬门变更**，
  本辩论权限外，需另立预注册。

## 辩论期间发现的新事实（超出原议题，需另立条目跟踪）

- **N1 push 缓存昨收跨日不更新**（C6）：影响所有 `ctx["change_percent"]` 消费者，
  含 ban-shadow 至 10/31 预注册死线的攒样有效性。**修复前该死线攒的是废样**，
  须暂停计入或标注污染期。
- **N2 ban-shadow 传感器坏死**（C9）：5 行、零登记、direction 硬编码、无 decision_id、
  inst_id join 脏。修复成本未评估。
- **N3 测量面冗余无「意图唯一性」守卫**（C9）：三处可执行落点已定位 ——
  `shadow_registry.py:46`（ShadowItem 增 `intent`+`cross_ref`，与 `check_mounting` :1129 同层）、
  `settle_premarket_invalidation.py:112-120`（READY_TO_ADJUDICATE 强制 conflict_check，缺则降级）、
  `daily_invariant_check.py`（分母哨兵：两 jsonl 按 (symbol,date) join，|Δchg|>2pp 即 violation，
  BIDU 案立刻可报）。
- **N4 过夜跳空单笔风险**（C4）：chg>10% 桶净负全部来自 1 笔 `OVERNIGHT_GAP_STOP`。
  属退出/持仓层，本辩论权限外（正文边界已声明不动退出参数）。

## 本轮实跑的复现入口

```bash
cd /opt/ftseek/FTSeek-US
# C2 310 行 IC（join key: shadow.inst_id → playbook_instructions.id；fwd 列 fwd_return_t1_adj）
#    分层：整体 / BUY / SELL_SHORT / executed / 未executed；chg 五分位 + 阈值对照
# C6 分母三路径对照
sed -n '509,534p' app/data/market_data_router.py          # RFC-068 prev_close 日K自算注入
grep '"US.BIDU"' data/entry_timing_shadow.jsonl            # change_percent/volume 冻结实锤
grep '"US.SMTC"' data/premarket_invalidation_shadow.jsonl | grep 2026-08-26   # 干净侧真值 127.52
# C8 禁触窗变更史（只读）
git log -S entry_open_blackout_hhmm --oneline              # 1817eec7 → ebdb7069 → 1d2b909d
sed -n '1126,1127p' config/unified_config.yaml             # 当前值 0935
# C4 过夜/日内切分（主持人补跑）
#   SELECT entry_time, exit_time, exit_reason, realized_pnl FROM trade_records
#   WHERE decision_id IN (C 命中 ∧ chg_pct>=10) → 8 笔：GAP 唯一 overnight=True
# C7 B 门线上判定样本
grep -h B_long_knife data/shadow/exec_gate_audit.jsonl | head -3   # TECK 9/10 chg 逐轮演化
```
