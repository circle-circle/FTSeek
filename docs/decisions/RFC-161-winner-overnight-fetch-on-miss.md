# RFC-161: 赢家隔夜影子结算器 fetch-on-miss (T+1 bar 结构性缺口根因修复)

> **状态**: 已落地 (2026-10-23)，用户裁「要」
> **类型**: L1 功能修复 (离线测量层; **零交易行为变更** — 不动 B16 判据定义/阈值/入场出场)
> **驱动**: 10/19 US.PEP 积压 3 天、9/4 US.META 卡 12 天 — 同一个结构性缺口, 会周期性复发
> **载体**: `scripts/settle_winner_overnight_shadow.py` (既有 cron `25 6 * * 2-6`, **不新增 cron 行**)
> **契约测试**: `tests/test_winner_overnight_settle.py` W9a-W9d (15 例存量 + 15 例新增 = 30)

---

## 1. 根因 (为什么不是"某天数据没到")

| 环节 | 行为 | 缺口 |
|------|------|------|
| `position_path_collector.py` | 只采平仓笔的 **entry_day / exit_day** 两天 | 从不采 T+1 |
| `settle_winner_overnight_shadow.py` | 需要 `next_trading_day(arm_date)` 的 1min bar | 只读缓存, 不补拉 |

组合结果: **凡 PRE_CLOSE 盈利仓, 其 arm 次日 bar 系统性缺失**，除非该标的次日恰好又成交
一笔（被当作 entry/exit 日采到）或人工补拉。故不是"偶发数据延迟", 而是测量链路的固定漏算:
样本静默卡在 pending ⇒ B16 (n≥20) 永远攒不满, 且 n 的增长速度被系统性低估。

## 2. 方案 (为什么改结算器而非采集器)

改结算器**已算出** `(symbol, next_trading_day)`, 缺什么补什么, 内聚且不影响其他 bar 消费方;
扩采集器要对"已 arm 的赢家标的"再扫一遍, 影响面覆盖整条 `position_path_collector` 采集语义。
两者都不动判据, 取内聚方案。

## 3. 四条护栏 (缺一条就会引入更坏的失败模式)

| 护栏 | 规则 | 不这么做会怎样 |
|------|------|----------------|
| **G1 收盘后才能拉** | 仅当 `bar_day < today_ET` 或 `now_ET ≥ 收盘+5min` 才 fetch; 早于收盘 → `too_early` 保持 pending | 盘中补拉会把**半日 bar 落盘**, 之后被当完整 EOD ⇒ 用 11:16 的价格结算 (静默错值, 比缺 bar 更坏: 缺了会告警, 错了不响) |
| **G2 完整性判据** | 末根 ≥ 该日收盘-5min (提前收盘日 13:00 → 12:55)、根数 ≥30; 残档视同缺失 | 旧判据 `len(bars) >= 30` 会接受任何半日档 |
| **G3 三因分离** | `too_early`/`not_trading_day` = 等待; `error` (额度/网络/OpenD) = 保持 pending **不累计**; 只有 `empty`/`truncated` (已收盘+交易日+拉取成功但无全 RTH) 才累计 miss, 连续 2 个周期 → `bar_no_bar` | 把额度耗尽当"该标的没数据" ⇒ 真样本被永久标成"停牌"踢出分母, 事后无从发现 |
| **G4 迟到留痕** | 结算行记 `settle_lag_days` + `bar_source` (cache/fetched); 汇总加 `late_settled_n` / `settle_lag_hist` | 批量回填让 n 从 8 跳到 20+, 读起来像"新证据", 实际全是旧 arm |

`bar_no_bar` 排除**可重开**: `--reset-settled` 一并清零 `bar_no_bar*` (否则被排除的行永远
回不到样本池)。排除行不进 B16 分母, 也不触发积压告警 (否则天天刷"永远好不了"的红)。

## 4. 数据面顺带修复: 17 个半日 bar 缓存

实测 `data/replay_bars/` 有 17 个 1min 档末根 < 15:55 (如 `US_SPY_2026-08-21.json` 106 根
止于 11:16、`US_CMG_2026-08-11.json` 80 根止于 10:50), 均为**盘中拉取落盘**后被
`fetch_bars` 的"缓存即完整"路径复用 ⇒ 任何回放/结算拿它们当收盘价都是错值。
`fetch_bars_with_status` 现在把残档视同 miss 重拉、拉到的残日**不写缓存**; 17 档已一次性
重拉为 390 根 (`repaired 17 / failed 0`, 标的均在本 30 天额度窗口内 ⇒ 零额度成本)。

## 5. 验证 (实弹)

| 项 | 结果 |
|----|------|
| 契约测试 | `30 passed` (W1-W8 存量 15 例零改动通过 + W9a-W9d 15 例) |
| 关联回归 | `test_replay_engine_import_smoke` / `test_exit_t_arm_shadow` / `test_weekly_optimizer_contracts` = `21 passed` |
| 活体探针 (真 OpenD) | 移走 `US_PEP_2026-10-20.json` → `ensure_bars` 返回 `status=fetched, 390 根 09:31–16:00`, 重建档与人工补拉档 **sha256 一致** (幂等) |
| 生产跑一次 | `bar 未就绪 US.UNH arm_date=2026-10-22 status=too_early → 保持 pending (不累计 no_bar)`; 读数不变 (settled_n=8, sum_delta=-85.12, win_rate=0.625, pending_n=1) |
| 数据面 | 残缺 1min 档 17 → **0** |

**当前时点判读 (TOO_EARLY, 不作"已生效"结论)**: US.UNH 的 bar 日 = 2026-10-23, 本次运行时
ET 仍是 10-22 19:2x (未收盘) ⇒ 按 G1 保持 pending 是**正确行为**, 不是接线死。该 arm 将在
2026-10-23 18:25 ET (cron `06:25 CST` 周六档) 跨过收盘线被补拉结算。

## 6. 边界 (本批不做)

- **不改** `position_path_collector` 采集语义 (仍不采 T+1; 由结算器兜住)。
- **不改** B16 判据定义/阈值/样本口径 (盈利臂 `arm_profit_pct>0`, min_n=20)。
- **不改** `load_1m` 以外的重放数学 (`_replay_eff_sl` 等 RFC-140 口径原样)。
- Futu 1min 深度实测 ≥5 年 (2021-10-15 仍返回 390 根), 故"迟到多久"不构成约束; 真正的
  约束是 30 天窗口额度 (实测 used 939 / remain 61, 按唯一标的计, 窗口内重复请求零成本)。
- 缓存**直接读文件**的消费方 (如 `reentry_gate_effect_tracker`) 不走 `fetch_bars`, 本批只
  保证文件本身已被修正, 未给它们加完整性判据 (后续按需)。

## 7. 回滚

`git revert` 该提交即可: 结算器退回"纯读缓存", 新增字段 (`settle_lag_days` / `bar_source` /
`bar_no_bar*`) 对旧代码是无害多余键 (旧代码不读)。