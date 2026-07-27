# DEB-20260727-entry-fill: 入场成交价记账失真 — quote_fallback 系统性记高 + 延迟对账提案

> **状态**: 🔶 起案待复核 (用户令"先立案, 另一模型检查")
> **定位**: 修尺子 (测量层) + 连带行为影响 (保本止损锚) — 行为面修复前需用户裁决
> **发现者**: 用户 (2026-07-27 盘初, SOLS 推送盈亏与真实成交对不上, 人肉揪出)
> **姊妹篇**: DEB-20260727-info-channel-timeliness (同属"静默失真"族: 表面无恙, 数据在说谎)

---

## 一、案发经过与实锤

**用户疑问**: SOLS 买入 $60.882 / 卖出 $61.085 × 64 股 = 盈利 ≈+$13, 推送却报实现盈亏 **-$0.96**。

**落库实锤** (`data/ftseek_us.db :: trade_records id=1153`):

| 字段 | 值 | 真相源 |
|---|---|---|
| entry_price | **61.10** | `entry_price_source: "quote_fallback"` (盘口卖一快照) |
| exit_price | 61.085 | `exit_price_source: "broker_deal"` (券商真实成交 ✓ 与用户一致) |
| realized_pnl | -0.96 | = (61.085 − 61.10) × 64 — 口径自洽但入口是错的 |
| 用户真实成交 | 买 60.882 / 卖 61.085 | 真实盈亏 **+$12.99** (未扣佣金) |
| metadata.execution | arrival 60.81 / fill 61.1 / slippage 47.69bps | "滑点"是相对幻影盘口价算的, 真实成交 60.882 在 arrival 与 ask 之间, 属正常 |

**代码路径** (`app/playbook/playbook_service.py:1041-1066`): 券商返回成交均价=0 (sim 账户已知偶发,
07-21 曾修 dealt_avg_price 字段名笔误但本案证明仍会发生) → 兜底取实时盘口 ask 价记账 (BUY→ask,
07-16 P2 改进, 方向合理但仍是快照非成交)。订单确认逻辑 (30s 窗, 07-21 GHOST-RC1 修复) 只确认
"成交与否", **不校验成交价字段是否非零可信**。

## 二、影响三层 (从轻到重)

1. **盈亏记反**: 本笔 +$13 → -$1; 推送/台账/归因全链错 (attribution 还据此判了"❌ 浮盈变亏损")。
2. **行为杀伤 (本案最重)**: 保本止损锚在错误的 $61.10 上。价格冲 $61.65 (MFE 0.90%) 后回踩
   $61.045 → 打穿偏高的保本线 → 9.9 分钟提前出场。**若锚定真实成本 $60.882, 保本线 ≈$60.88-60.92,
   回踩不触发, 仓位大概率存活**。即: 记账失真直接改变交易行为。
3. **系统性偏差 (测量层污染)**: 全库 quote_fallback 共 **130 笔**, 且一律取 ask 侧 → 多头入场价
   系统性记高、盈亏记低。今天 09:35-09:41 全部 10 笔入场无一幸免 (INTC/MRK/BABA/LLY/GS/SLB/T/NIO/
   SOLS/TSM)。**交易层胜率/期望/回放判分全部在吃假样本** — 与信息渠道"常量退化"同族:
   系统表面健康, 数据在说谎。

## 三、修法提案 (修尺子口径, 两阶段)

### 阶段1: 延迟对账 (go-forward, 防新增)

入场记账后挂延迟对账任务 (30-60s, 对齐 07-21 的 30s 确认窗先例与平仓侧轮询惯例):
重查券商 `dealt_avg_price` (deal_list_query 或 order 详情), 与记录偏差 >0.1% 则:
- 回写 `entry_price` + metadata 标 `entry_corrected: true` (保留原值 `entry_price_orig`);
- **重锚保本止损/初始 SL 的相对偏移** (行为面 — 此条需用户裁决, 见 §四-Q1);
- 对账失败 (券商仍返 0) → WARNING 一次 (不静默), 标的进观察名单。

### 阶段2: 历史回填 (一次性, 修存量)

对 130 笔 quote_fallback 记录用 deal_list 回放真实成交均价, 回填 entry_price/realized_pnl
(保留原值审计字段), 重出受影响时段的胜率/期望统计 — **先出影响面报告再动手**, 若回填后
历史判分结论显著改变 (如某策略胜率翻转), 须重审此前引用这些统计的裁决。

## 四、预注册判据与待复核点

- **Q1 (行为面裁决)**: 延迟对账发现真价后, 是否允许**盘中重锚**保本止损?
  正方: 锚在假成本上=行为被假数据驱动 (本案已杀一单); 反方: 盘中改锚=行为变更,
  需走机制变更流程。备选: 首期只改记账+告警 (纯修尺子), 重锚待 n≥10 案例后裁决。
- **Q2 (回填判据)**: 回填后若 |Δ胜率| >3pp 或盈亏符号翻转笔数 >5%, 触发相关历史裁决重审。
- **待另一模型复核**:
  ① `dealt_avg_price` 在 sim 环境返 0 的触发条件 (是否仍在 07-21 修复的盲点, 如部分成交/盘中时序);
  ② 延迟对账挂点选型 (playbook_service 入场链路 vs position_sync 既有对账循环 — 后者是否天然
  更合适, 避免新挂点); ③ 130 笔回填前确认 deal_list 历史可查深度 (sim 账户成交回报保留期);
  ④ quote_fallback 兜底本身是否应保留 (保留=极端后备, 但触发即 WARNING, 与静默退化军规一致)。

## 五、执行记录

- 2026-07-27 起案: 用户 SOLS 案例实锤 + 130 笔影响面初查 + 代码路径确认, 待另一模型复核。

---

## 六、另一模型复核 (Qoder-主工程会话, 2026-07-27 盘中) — 四待复核点作答 + 翻案 + go-forward 已修

### 翻案: quote_fallback 不是"极端后备偶发", 事实上就是主路径

实测近一周入场价源 broker_deal 占比: 07-21 **0/17**, 07-22 1/14, 07-23 **0/19**, 07-24 0/8, 07-27 **0/10**。
**真根因是时序缺陷而非字段名** (07-21 修复未解决本问题):
1. `filled_price` 在下单瞬间取 (sim 异步成交, 必为 0)
2. 单次重查也在毫秒级后 (订单仍 SUBMITTING)
3. **30s 确认循环等到 FILLED_ALL 时, `od_data.iloc[0]` 行里就有 `dealt_avg_price` 真值, 旧版只返 bool 把价丢了**
4. 调用方被迫跳 quote 兜底记 ask 假价

### 四待复核点作答

| # | 问 | 答 |
|---|---|---|
| ① dealt_avg_price 返 0 触发条件 | **时序必然** (非偶发/非字段名残留): 下单瞬间查询早于 sim 成交回报, 几乎每笔都中 |
| ② 延迟对账挂点选型 | **都不需要** — 确认循环行内本就有真价, 就地带回即可 (零新挂点/零额外 API/记账瞬间即正确); 连 Q1 "盘中重锚"争议都被绕开 — 锚从第一天就是对的, 无需事后重锚 |
| ③ 130 笔回填可行性 | **起案的 deal_list 路径不可行** — 坑库已记录"富途 sim 不支持 history_deal_list_query"; 可行替代 = `history_order_list_query(start,end)` 带 dealt_avg_price (position_sync 既有消费惯例), sim 保留深度待实测 |
| ④ quote_fallback 去留 | 保留作极端后备 (已有 WARNING 非静默); 修复后触发应真正罕见 — **观察判据: 修复后 3 交易日 quote_fallback 新增 ≤3 笔, 否则重查** |

### go-forward 修复 (已落地, 盘中重启生效)

`playbook_service.py` 两处:
1. `_confirm_order_filled` 返三元组 `(confirmed, status, dealt_avg_price)` — 确认窗内行里真价就地带回;
2. 调用点优先用真价记账 (`entry_price_source=broker_deal_confirm`), quote 兜底加 `_fill<=0` 守卫 (旧版无条件执行会覆盖真价 — 复核中发现的二阶陷阱)。
验证: py_compile / 三元组签名+唯一调用点断言 / 980 passed。回滚: git revert 单文件。

### 阶段2 (历史回填 130 笔) 裁决保留

属历史数据改写 (entry_price/realized_pnl 重写 + 胜率统计重出), 按 Q2 判据**先出影响面报告再动手**, 且回填路径改用 history_order_list_query (见③)。待用户裁决后执行。

### 附带修复 (同批)

全量回归揭出 `test_intraday_scorer` 两个时间敏感假 mock (patch 打在不被调用的 5 档时代 API 上, 实测真墙钟, 盘中 prime 时段必挂 — 密封化漏网第三例) → 改 mock 真实调用链 (shared_kernel), 追平 2 档制语义, 980 全绿。

---

## 七、阶段2 影响面报告 (2026-07-27, `scripts/entry_fill_impact_report.py` 只读干跑, 用户令"先出报告")

**总结论: Q2 判据 🟢 未触发 — 历史裁决无需重审, 回填属低风险度量修正。**

| 指标 | 结果 | 判据 |
|---|---|---|
| 匹配率 | **124/130** (HIGH 123 / MEDIUM 1 / NONE 6, 其中 4 笔集中在 07-17 当日 sim 订单查询返空) | — |
| Δentry | median **-0.007%**, p10 -0.097% / p90 +0.024% (方向符合预期: ask 记高) | — |
| ΔPnL 合计 | **+$193.03** (均 +$1.61/笔 — 入场记高→盈亏被低估, 回填后整体上修) | — |
| 胜率 | 44.2% → 45.8% (Δ+1.7pp) | < 3pp ✅ |
| 盈亏符号翻转 | 2/120 (1.7%) | < 5% ✅ |

**对历史结论的影响 (关键安心项)**: 本周基线日序不变 (07-23 仍最差 -379→-336; 07-24 三门首夜 -31→-25 结论更好); ATR 分桶/三门验证/复盘结论方向全部稳固。
**报告产物**: `data/entry_fill_impact_report.json` 含逐笔明细 (id/entry_new/order_id/置信度), 回填执行时可直接消费, 无需重拉。
**建议执行方式 (待裁决)**: 仅回填 HIGH 123 笔 (MEDIUM 1 笔人工过目后并入, NONE 6 笔保持原样+标记); 逐笔保留 `entry_price_orig`+`entry_corrected:true` 审计字段, DB 先备份; realized_pnl/相关衍生字段同步重算。

---

## 八、阶段2 执行记录 (2026-07-27 深夜, 用户裁"执行")

`scripts/entry_fill_backfill.py` (dry-run 缺省 / --apply 真写 / sqlite backup API 在线备份 / 幂等):
- **回填 124 笔** (HIGH 123 + MEDIUM 1 笔 NVDA Δ仅 0.02% 并入); uncorrectable 标记 6 笔
- 衍生字段一致重算: entry_price / realized_pnl / pnl_pct / **mfe_pct / mae_pct** (基于 metadata peak/trough, 含 LONG/SHORT 方向语义)
- 审计字段: entry_price_orig / entry_price_source_orig / entry_corrected_at / entry_backfill_order_id
- **验证**: dry-run 与影响面报告完全对账 (124/+193.03/翻转2); SOLS 黄金样例 entry 61.10→60.882 / pnl -0.96→**+12.99** (与用户真实成交完全吻合); 全库计数 124/6 断言 ✓; 幂等复跑 0 笔 ✓
- 备份: `ftseek_us.db.bak_entryfill_20260727_233630`; 回滚 = 恢复备份或按 entry_price_orig 逐笔还原
- **另一处翻转**: COIN id=1080 -2.26→+0.21 (与 SOLS 同款)
- 后续: 消费这些统计的报表 (scorecard/回放) 下次重跑自动吃到真值; ④观察判据继续 (修复后 3 交易日 quote_fallback 新增 ≤3 笔)。本案闭环, 状态 → ✅ 已裁决已执行。

---

## 九、同款隐藏问题全面扫描 (2026-07-28 凌晨, 用户令"关联模块是否需调整 + 扫同款隐藏问题并修复")

同款模式定义: ①"下单瞬间取值必空 → 快照/下单量兜底记账" ②"确认/轮询循环行里有真值却不带回"。

### 🔴 实锤 2 处, 已修 (零新 API, 都是"就地带回")

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | **平仓侧** `dual_broker.close_position` | 完全对称同款: 轮询循环 `iloc[0]` 读了 status/dealt_qty 却不读同行 `dealt_avg_price`; 事后再查的主路径 history_deal sim 不支持 → 近两周 **41% 平仓价 (42/103) 是报价快照** (broker_closed_no_price 36 + NULL 6) | 轮询确认成交时就地带回 dealt_avg_price |
| 2 | **入场部分成交量** playbook_service | `_result.filled_qty` 也是下单瞬间取 (sim 异步必 0) → 部分成交被回退成全量记账 (幽灵仓残余风险) | `_confirm_order_filled` 升四元组额外带回 dealt_qty, 优先真值 |

### ✅ 扫过确认无同款 / 无需调整的关联面

- **entry_price_source 消费面**: 无代码按 source 过滤判分 (纯标注), 新值 broker_deal_confirm/broker_deal_backfill 不破坏任何消费方
- **SL/TP/保本锚**: 基于 `_fill` 计算 → go-forward 修复后自动正确, 无需改动
- **slippage_bps**: arrival vs fill → fill 真了滑点测量自动变真 (历史 130 笔的 execution.slippage_bps 未回填 — 观测字段不驱动行为, 影响面报告已覆盖主度量)
- **手动平仓** (web_dashboard/order/executor): 已是"带重试轮询 dealt_avg_price"正确模式
- **回放/报表** (L2/exit_replay/scorecard): 吃 DB 列, 回填后自动真
- **TWAP** (`fp=0 → slice_price` 同款兜底): 仅主线引擎 (已静默) 消费, playbook 不走 — 休眠路径挂账不动, 若未来启用 TWAP 须先修

### 待裁决 (新增)

**平仓侧存量 42 笔** (broker_closed_no_price 36 + NULL 6) 可用入场侧同款工具链 (impact_report → backfill) 回填。量级参考: 平仓假价是报价级而非 ask 单侧偏, 预期Δ 小于入场侧; 待用户裁决是否出 exit 侧影响面报告。

验证: py_compile / 四元组+初始化+平仓带价断言 / 980 passed / 重启冒烟 7/7。

---

## 十、平仓侧存量回填执行 (2026-07-28 凌晨, 用户裁"需要回填")

`scripts/exit_fill_backfill.py` (dry-run 报告→apply, 同入场侧规矩):
- 候选 66 笔 (broker_closed_no_price 全史 60 + NULL 近两周 6); 匹配口径: 平仓单方向取反 (LONG→SELL/SHORT→BUY) + qty 精确 + exit_time 邻近
- **Dry-run 影响面 (安心项)**: Δexit median **0.000%** (双侧对称 ±0.08%, 无系统性偏差 — 与入场侧 ask 单侧偏本质不同); ΔPnL 仅 +$7.48; 胜率零变化; 翻转 2 笔小额 (NVDA -1.32→+4.12 / META +0.82→-0.22) → Q2 类判据远未触发
- **执行**: 回填 **59 笔** (首轮 55 + 复跑补 4 — 07-20 首轮拉单限频瞬时返空误判 NONE, 幂等复跑自动补齐, 第三跑 0 新增收敛); 真不可修 7 笔标 exit_uncorrectable
- 重算: realized_pnl / pnl_pct (exit 价不影响 mfe/mae 不动); 审计字段全套 (exit_price_orig/原 source/order_id)
- 备份: `ftseek_us.db.bak_exitfill_20260728_005633`
- **方法论注记**: 幂等设计意外收获 — 限频瞬时失败不会永久误判, 复跑即自愈; 未来回填工具均应保持 "NONE 不落 uncorrectable 直到终跑"或简单复跑两次。

**本案最终状态**: 入场 124 + 平仓 59 = **183 笔假价全部修真**; go-forward 双侧就地带价已上线; 观察判据: 修复后 3 交易日 quote_fallback+broker_closed_no_price 新增合计 ≤3 笔。
