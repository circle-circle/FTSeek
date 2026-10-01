# RFC-124: 全链路盲区审计 (2026-10-01) — 调查结论 + 零行为变更批

- **状态**: 调查完成; 零行为变更部分已落地 (回填脚本 + LEDGER-CALIBER 契约文档);
  行为变更候选 (#2 conf 砍单依据 / #3 隔夜 gap) 仅登记观察, 未实施。
- **触发**: 用户令「整个全时间阶段的交易链路和策略, 包括 prompt/记录/信息源/输入,
  是否还有其他未发觉和发现的问题」→「按建议推进」。
- **方法**: 全部结论以 DB (data/ftseek_us.db) + 日志 + fee_model 复算为地面真值;
  每项建议先回放/复核再决定落地或撤回。本轮**撤回 2 项自己的前轮建议** (见 §3)。

---

## 1. 已落地 (零行为变更)

### 1.1 退出回写断链修复 (一次性回填)

- **病灶**: 9/17 盘前退出用临时阈值 (tighten +0.3% / lock_in +2%, 9/18 用户勘误已废)
  平仓时未带 instruction_id → `_book_playbook_exit_result` 的 pi 回写不触发 →
  pi.id 2550 (US.UBER) / 2771 (US.NOK) 滞留 `outcome='holding'` + 陈旧未实现快照。
  NOK 符号翻转: pi.pnl=−112.13 vs 权威净 **+108.43**。
- **现行链路已带 instruction_id, 全历史 gap 仅此 2 行, 零新增** → 定性为历史残留,
  回填修复而非改代码。
- **落地**: `scripts/fix_instruction_exit_writeback_gaps.py` (dry-run 默认 / --apply
  带 append-only jsonl 审计 / 幂等 / 终态与 GHOST 守卫 / find-apply 竞态防护)。
  已 --apply: 2 行修复, 复跑 0, 审计 `data/audits/instruction_exit_backfill_20261001T061257Z.jsonl`。
  契约测试 `tests/test_fix_instruction_exit_writeback_gaps.py` 9 passed。
- dry-run 首版曾多抓 1 行 (pi.id=369, outcome='closed' 旧 schema 终态 + BROKER_GHOST_CLOSE)
  → 收窄为仅 `holding/NULL` 且排除 GHOST/ORPHAN/PHANTOM 词表 (E2 契约 void 语义)。

### 1.2 账本口径契约文档 (常驻)

`docs/LEDGER-CALIBER.md` — 两本账权威关系 (trade_records=SSOT, pi.pnl=快照)、
毛/净口径、量纲陷阱 (pi.max_favorable_excursion=$ vs tr.mfe_pct=%)、strategy_id
必拆、两套退出词表、占位行/updated_at/僵尸 PENDING/时区四陷阱、conf 无 IC 结论。
下游任何复盘/IC/OKR 分析动手前先读。

---

## 2. 调查结论 (数据实锤)

### 2.1 9月全系统净 −535.86 的归因修正 (推翻我前轮"费吃毛利158%"的笼统归因)

| 通道 | n | 毛 | 费 | 净 |
|---|---|---|---|---|
| playbook_sim 主链 | 174 | +882.48 | −880.23 | **+2.25** (盈亏平衡) |
| extended_hours_entry EXT旁路 | 6 | +38.09 | −576.20 | **−538.11** |

- **黑洞在 EXT 旁路, 不在主链**。CTNT 单笔费 $451.26: NSCC 结算费 $0.003/股 ×
  68493 股, 与价格无关 → **低价仙股按 notional 定仓 = 结算费爆炸** (fee/notional 9.15%)。
  fee_model SSOT 复算差 $0.41, fees_est 无错。
- **EXT 旁路完全绕过 admission gate** (直接 place_order, 不写 playbook_instructions):
  admission 的 min_entry_price=5.0 / min_notional=2500 对其无效。与 RFC-122 偏斜门
  盲区**同构** (多路径写入/下单, 治理门只覆盖一条)。
- **现状 (2026-10-01 21:10 更正 — 原"已自我堵上"结论被实弹证伪)**:
  EXT 入场**一直在实弹运行**, 从未停用。crontab 的 `EXT_ENTRY_EXEC_PRICE_SHADOW=true`
  只控制 `_record_execution_price_shadow` (executor L808/L815) —— 一个 record-only 的
  "$50 执行池价格阈值对比" jsonl 记录器, **完全不碰 `_execute`/`place_order` 路径**
  (L2963/L3051)。9/28 提交 `b2183f737` 标题自证: 「零交易行为变更, **生产闸仍
  EXT_ENTRY_MIN_PRICE=5**」。
  我原结论把"9/19–9/30 无新成交"误读为"已停用", 实际是**那段时间没有候选通过闸**
  (价格闸 $5 拦 24 只、量闸、财报事件门), 不是通道关了。
  **实弹复现**: 2026-10-01 08:10 ET `US.ACN SHORT 23股 @212.529`, futu_order_id=8219132,
  `FILLED_ALL`, position_id=EXT-47604eaf9c8d, SL=223.16, gate_mode=strict,
  entry_grade=A, 财报日 (ACN 2026-Q4 BMO, surprise +2.36%), change_pct=+15.9 →
  `SHORT_ALLOW=true` (.env.us:191) + `change_pct >= SHORT_GAP_MIN=3.0` 判 SHORT。
  ⇒ **遗留风险不是"若重开", 而是"一直开着"**。
- 但 EXT 执行器代码内 MIN_PRICE=$5 地板 (L875 注释自证 CTNT $0.073/GIPR $0.435
  曾透过前三闸) — $5 地板挡不住 CTNT 型费用爆炸 ($0.072×68493 股 notional $4,931
  过 $5 闸? 否 — 实测 CTNT px=0.072 < $5 是 9/17 闸前入的; 现行 $5 闸会拦)。
  **遗留风险**: EXT 真单**从未停过** (§2.1 更正), 必须过 admission 费用门 (fee_bps 上限), 已登记 §4。

### 2.2 conf 无方向预测力 (分层 IC 复核, 修正 admission 的混合口径)

按通道分层 (trade_records 权威账, 8/1 后已结算):

| 通道 | n | IC(conf,净$) | t |
|---|---|---|---|
| deep_analysis | 249 | 0.006 | 0.10 |
| universe_expansion | 53 | 0.072 | 0.51 |
| bridge_scanner | 46 | −0.028 | −0.19 |
| intraday_reentry | 26 | 0.0 | 0.0 |
| 混合 | 379 | 0.047 | 0.92 |

- 所有通道 |t|<1.1, 分层后仍无 IC → admission "conf 无实证支持" 结论**在通道分层下依然成立**。
- conf≥70 vs conf<55: WR 81% vs 82%, 净%中位 0.194% vs 0.213% — **无差异**。
- 净$ 随 conf 上升 (70-79 桶 +680 vs 30-39 桶 −249) 是**尾部/仓位效应**, 非预测力:
  notional 中位各桶持平 (5447-5838), 净%中位各桶持平 → conf 桶间的净$差异来自
  少数大赢单落桶, 不是系统性方向优势。
- **含义**: 偏斜门按 conf 砍最低多数方 ≈ **随机砍单**; 且 E8 回放 (DEB-20260930)
  显示被砍者反事实净 +1,822 → 反向选择实锤。RFC-122 阶段1 裁决时应把
  "砍单排序依据从 conf 换成什么" 一并裁 (候选: notional 最小优先 / 随机 / 不砍只降仓)。

### 2.3 隔夜 vs 日内 (9月主链已结算)

| | n | 净 | WR |
|---|---|---|---|
| 日内平仓 | 147 | **+1,189.46** | 80% |
| 隔夜持仓 | 27 | **−1,187.21** | 63% |

- 隔夜负贡献几乎全部来自 OVERNIGHT_GAP_STOP 6 笔 (−1,789); 其余隔夜退出为正
  (SL_TRAIL_LOCK +574 / lock_in +108 / SL_BREAKEVEN +19) → **「留翻转空间」哲学有产出**,
  不能一刀切禁隔夜。
- 6 笔 gap 全是 BUY、SL 距离 4.5-5%、滑点 0.9-1.5x 设定风险。n=6 < 30 →
  **按纪律只登记观察, 不动** (样本不足不改行为)。攒够 n≥30 或单笔尾部再放大时重议。
- 候选假设 (未验证): gap 损与持仓 SL 宽度相关 (admission max_sl_pct=0 未启用);
  若启用 4% 上限, 9月会拦掉 6 笔中的 5 笔 — 但同口径也会拦掉部分隔夜赢家, 需回放裁决。

### 2.4 VIX 数据源清白 (撤回前轮怀疑)

近 2 日日志 140 次 `[Regime] VIX from Router` 全真值 (14.17-14.35), AlphaVantage
fallback 0 次, vix_uncertain 0 次 → 9月 VIX 窄带是**市场真实低波动**, 非死输入。
regime 两态 (cautious/risk_on) 在低波月无区分度是环境使然; regime×方向分层盈亏
遥测仍值得做 (纯遥测), 但不是数据源 bug。

### 2.5 SL_BREAKEVEN 88 笔不是僵尸单 (撤回前轮建议 #4)

权威 mfe_pct: 中位 0.57%, max 1.63% → 从未真正启动, 不是利润回吐。
持仓时长: **中位 0.3h**, 69/88 笔 <1h, >4h 仅 11 笔 → 是短线噪声单快速保本退出,
**不占用购买力** (资金护栏拦截 6 日与 breakeven 长持重叠仅 0-2 笔/日)。
日内 breakeven 78 笔净 +631 = 正贡献。**"入场后 N 分钟未启动提前释放"建议撤回** —
系统已经在这么做了 (中位 0.3h 退出), 无改进空间。

---

## 3. 本轮撤回清单 (横跳禁令留痕)

| 前轮建议 | 撤回理由 (证伪源) |
|---|---|
| #4 SL_BREAKEVEN 僵尸单早退 | 持仓中位 0.3h, 系统已快速退出; 日内 breakeven 净 +631 正贡献 (§2.5) |
| "VIX 死输入" 怀疑 | 140/140 真值 0 降级 (§2.4); 前轮已部分收回, 此处正式结案 |
| "费吃毛利158%"笼统归因 | 拆分后主链盈亏平衡, 黑洞在已停用的 EXT 旁路 (§2.1) |

---

## 4. 登记观察 / 待裁 (不实施)

1. **EXT 真单从未停用 (§2.1 2026-10-01 更正)**: 前置条件 = 必须过 admission 费用门 (或等价 fee_bps 上限,
   建议 ≤30bps 往返); settlement fee 对 qty 线性、对价格无关 → 低价股必须有
   max_shares 或 min_price×qty 联合闸。现状: **实弹运行中** (10/1 ACN SHORT 实证), 待用户裁处置。
2. **偏斜门砍单依据** (conf→?): 并入 RFC-122 阶段1 裁决包 (影子攒样 ≥10 交易日后)。
3. **OVERNIGHT_GAP_STOP**: n≥30 再议; 候选 max_sl_pct 启用需先回放 (含对赢家的误杀面)。
4. **regime×方向分层盈亏遥测**: 纯遥测, 待排期 (低优先, 当前 regime 无变异)。

## 5. 变更清单

| 文件 | 性质 |
|---|---|
| `scripts/fix_instruction_exit_writeback_gaps.py` | 新增, 一次性回填 (已 --apply, 幂等) |
| `tests/test_fix_instruction_exit_writeback_gaps.py` | 新增, 9 契约测试 |
| `docs/LEDGER-CALIBER.md` | 新增, 常驻账本口径契约 |
| `docs/decisions/RFC-124-full-chain-blindspot-audit.md` | 本文档 |
| DB: playbook_instructions id 2550/2771 | outcome/exit_*/pnl/pnl_pct 回填 (审计 jsonl 留痕) |

**零交易行为变更**: 无 app/ 代码改动, 无需重启服务。
