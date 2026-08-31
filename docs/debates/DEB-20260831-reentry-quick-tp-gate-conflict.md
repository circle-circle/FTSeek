# DEB-20260831 · 再入场快 TP 与 min_tp_rr 门禁互杀 — 发现定案 + 影子先行

> 状态: **影子采集中 (record-only, 零行为变更)**。符合 9 月冻结 (判据字典 C1/C7):
> 不改任何退出参数/门禁, 只加观测。
> 起源: 2026-08-31 退出策略合理性核查 (用户令"核查当前最新止损止盈退出策略是否合理")。
> 用户裁决 (同日, 三选一案选 2): **影子先行**, 攒反事实数据后再裁恢复/废除。

---

## 一、发现 (两个已裁决设计互相抵消, 此前无人察觉)

### 1.1 规则 A — 再入场快收结构 (2026-08-07, 用户裁决)
`196e317e` [hotfix] "再入场快收结构: ATR相对 SL放宽/TP降档":
- `quick_sl = clamp(0.6×ATR%, 2.0, 3.5)`, `quick_tp = clamp(0.35×ATR%, 1.0, 2.0)`
  (`app/playbook/intraday_reentry.py:124-137`)。
- 设计语义: **止盈近、止损宽, 见好就收, 胜率优先**。几何盈亏比天然 ≈ 0.35/0.6 ≈ **0.58**。
- fallback 路径 `tp_rr=1.6` 注释明写 "过 min_tp_rr=1.5 门" (`intraday_reentry.py:191`)
  → 作者知道门禁存在并为 fallback 规避, 但 quick 主路径未规避。

### 1.2 规则 B — 止盈几何门禁 (2026-07-10)
`pipeline.py` 规则1.5: `min_tp_rr=1.5` (`config/unified_config.yaml:1062`)。
- 原始动机: 杀分析师畸形 TP ("冒大风险博小利润", 实证案例 NVDA SL-4.5%/TP+1.7%, RR=0.38)。
- 行为: 指令 TP 相对初始 SL 的 RR < 1.5 → **忽略该 TP**, 交给 trailing/EOD。

### 1.3 冲突
快收 TP 的 RR≈0.58 在门禁眼中与畸形单同构 → **全部再入场臂的 TP 被静默吞掉**。

## 二、实证 (生产数据)

| # | 证据 | 数据 |
|---|---|---|
| E1 | 再入场臂 TP 几何 | 8/7 后 13 笔已执行再入场指令: SL 2.0–3.5% / TP 1.1–2.0%, RR=0.55–0.62 (除 8/7 AVGO 首笔 RR=1.50 系 quick 路径前旧口径) |
| E2 | TAKE_PROFIT 出场数 | 13 笔实弹再入场臂 **0 笔** TAKE_PROFIT; 出场分布 DEAD_SIGNAL 6 / SL_BREAKEVEN 4 / PRE_CLOSE 1 / SL_TRAIL_LOCK 1 / TIME_EXIT 1 (trade_records decision_id LIKE 'intraday_ree%') |
| E3 | 冻结窗对照 | 8/24–8/31 全部 58 笔 PLAYBOOK_SIM 闭环中 TAKE_PROFIT 仅 1 笔 (DA 臂), 再入场 0 |
| E4 | 稳健性 | 即使按 8/8 DEB §23.4 β 下限口径 (有效 SL 压到 1.5–1.75%), 最优情形 RR≈2.0/1.5=1.33 仍 <1.5, 门禁仍吞 |

后果定性: **不失控但设计落空** — 止损/保本锁照常; 赢家本应"到 TP 价全落袋",
实际走"等回撤 0.5–0.8% 确认"(trailing) 或死信号/尾盘, 对目标利润仅 1–2% 的快单
每次赢家系统性回吐约 0.5–0.8% (目标利润的 25–50%)。

## 三、危害面与边界

- 仅影响 `decision_id` 前缀 `intraday_reentry` 的臂; DA 臂无 RR<1.5 (8/24 后 108 笔
  实证 0), UE 常数 SL 指令 8/8 未执行 (全部 no_trigger)。
- 门禁本身对畸形分析师 TP 的防护价值不受质疑 (当年实证充分); 问题只在
  **快收设计与之同构而被误伤**。
- 在档检索 (docs/debates 全量 grep min_tp_rr/quick_tp): 无既往讨论, 非重复上报。

## 四、预注册裁决判据 (判据字典 B13, 窗口内禁改)

- **观察窗**: 2026-09-01 → 09-30 (冻结期全程)。
- **度量**: `data/shadow/reentry_quick_tp_shadow.jsonl` —
  ① TP 触达率 (触达行/再入场平仓行); ② `tp_vs_actual_pct`
  (正 = 按 TP 出场优于实际出场) 的均值与方向占比。
- **裁决规则** (n≥10 笔触达结算行, 或窗口结束强制结算):
  - **a. 恢复快TP**: 均值 > 费用线 (~0.15%) ∧ ≥70% 方向为正 → 提案门禁按
    `decision_id` 前缀豁免再入场臂 (另立 DEB + 用户裁决, 影子先行毕业流程)。
  - **b. 维持门禁**: 均值 ≤ 费用线 ∨ 正方向 <70% → 快 TP 触碰无价值或价值不抵
    回吐噪声 → 提案废除 quick_tp 参数 (承认门禁吞掉=正确结局), 快收结构退化为
    "只有宽 SL + trailing/EOD"。
  - **c. 样本不足**: 窗口结束触达行 <10 → 顺延至 10 月首周结算 (只顺延, 不重置)。
- **禁止动作**: 窗口内不得改 min_tp_rr / quick_sl / quick_tp 任何参数 (防污染)。

## 五、影子实现 (本批落地, 零行为变更)

- 模块: `app/playbook/reentry_tp_shadow.py` (note_gate_swallowed_touch + settle_on_close)。
- 挂点1: `pipeline.py` 规则1.5 门禁吞掉分支 → 价格首次触及被吞 TP 记入仓位 meta
  (键 `_reentry_tp_shadow_touch`, JSON-safe 跨轮持久, 键存在即去重)。
- 挂点2: `_close_trade_record` conn.commit() 之后 → 再入场臂 (或带触达记录的仓)
  落一行结算 (触达信息 + 实际出场 + 反事实差)。
- 开关: `playbook.execution_parameters.reentry_quick_tp_shadow_enabled: true`
  (必须在 execution_parameters 内 — get_playbook_config() 只合并该两段,
  放 playbook 顶层=死键; 2026-08-31 重启后自查发现并归位)。
- 登记: `shadow_registry.py` id=`reentry_quick_tp_gate` (min_n=10) + 判据字典 B13。
- 测试: `tests/test_reentry_quick_tp_shadow.py` (触达/去重/结算差/门禁行为不变)。
- 回滚: yaml 改 false + 重启 (PlaybookConfig 单例缓存; 或删两处 try 块则无需重启)。

## 六、诚实边界

- 触达判定用监控轮报价 (20s 粒度), 轮间穿越未触达 → 触达率轻微低估 (保守方向)。
- `tp_vs_actual_pct` 未扣费 (毛口径), 裁决线与费用线 (~0.15%) 显式对齐, 不重复扣。
- 影子不记录"未触达"再入场臂的逐轮价格 (只结算行留分母), 无法回放触达时点分布。
- 8/7 前再入场臂 (旧口径, RR≥1.5) 不在本影子语义内, 历史不回填。
