# OKR 盈利/胜率补充完善：退出规则对齐 T+5 alpha — 现状与度量

> **日期**: 2026-06-24
> **目标**: OKR KR2.1 胜率≥55% / KR2.2 周PnL≥$1000 / KR2.3 IC(T+3)>0
> **结论**: 最大杠杆 = 退出规则对齐周级 alpha (T+5 IC +0.265)。该实验 (RFC-061 cohort A/B) **已在全链路实现并运行**, 此前缺的是**度量+决策门禁**, 本次补齐。

---

## 一、杠杆定位 (证据闭环)

依 `docs/ic_report_2026-06-24.md`:

- 信号 T+1 反预测 (IC -0.114), T+5 强正 (IC +0.265) — **alpha 是周级**。
- 持仓数据 (375 笔): avg 1.71h, 仅 1 笔>1 天; `EOD_FORCE_CLOSE` 7 笔 avg **+3.62%** (方向对、被收盘砍)。
- backtest: 4-24h 持仓 70.7% WR +$21K (最好), 1-4h 47.5% WR -$9.5K (死亡区间)。
- 自洽故事: **两头吃亏** — 吃 T+1 反向亏损 + 砍 T+5 正确盈利。

→ 任何继续优化 T+1/日内 scalp 的改动都在和数据作对。**把盈利仓持有到 ~T+5** 是 KR2.1/KR2.2 的最大杠杆, 比调因子权重更大。

## 二、实验现状 (RFC-061, 已全链路实现)

| 组件 | 位置 | 状态 |
|---|---|---|
| cohort 分配 (确定性 hash) | `trade_store.assign_cohort()` | ✅ |
| 入场注入 metadata | `order_executor.py:1119` + `trade_store.py:641` | ✅ |
| 主线 pre_close B 组跳过 EOD | `exit_rules/pre_close.py:79-93` (legacy) + `408-419` (adaptive) | ✅ |
| MAX_HOLD_T5 (5 交易日强平) | `pre_close.py:525-573` + `position_exit_monitor.py:1305,1440` | ✅ |
| 隔夜跳空止损 (-3%) | `pre_close` + `playbook/pipeline.py:1245` | ✅ |
| Playbook shadow 路径 | `pipeline.py:1245,1253-1273,1462` | ✅ |
| 退出 reason 枚举 | `core/models.py:136-138` (MAX_HOLD_T5/OVERNIGHT_GAP_STOP) | ✅ |
| source_type 准入 | `cohort_b_skip_allowed()`: SHADOW/SIM/MAIN_PIPELINE 默认允许, LIVE 需 env 显式开 | ✅ |

**规则**: cohort A = 现行日内 EOD 全平 (对照); cohort B = 盈利仓跳过 EOD 持夜到 T+5, 仍受 ATR 止损 + 隔夜 -3% + 5 交易日上限约束。

**实盘安全**: `PLAYBOOK_LIVE` 需 `RFC061_LIVE_OVERNIGHT_ENABLED=1` 才开, 不从 sim 自动继承隔夜。当前 `futu_simulate` (paper) 阶段, 无真实资金风险。

## 三、本次补齐: 度量 + 决策门禁 (此前缺的缺口)

实验自 2026-06-24 起跑, 但此前**无任何 A/B 度量工具** — 跑了却产不出 OKR 证据。本次新增:

### `scripts/cohort_ab_report.py`
- 从 `trade_records.metadata_json.cohort` 直接度量, 按 cohort (A/B/PRE_EXPERIMENT) 聚合:
  n / sum_pnl / avg_pnl_pct / avg_hold_h / win_rate / exit_reason 分布 / **持仓时长桶** (验证 B 是否向 1-5d 迁移)。
- **未平仓观测**: B 组持夜仓数 (验证实验真让仓过夜)。
- **决策门禁** (附录 B.4): PASS (B PnL>A 且 WR 不降 → 推广) / FAIL (B 隔夜跳空亏损 → 回日内) / INCONCLUSIVE (样本不足)。
- **OKR 映射**: B vs A 的 sum_pnl/WR 差值 = 退出对齐 T+5 的增量, 直接对应 KR2.1/KR2.2。

### 实测首跑 (2026-06-24)
- 未平仓: A=4, B=2 (B 有持夜仓 → 实验生效)
- 已平仓: A=1, B=1 → **INCONCLUSIVE** (实验今日启动, 需 ~2 周 / 每组≥30)
- PRE_EXPERIMENT 375 笔: avg 1.71h, WR 37.6%, 1 笔>1天 — 印证"两头吃亏"基线

### 用法
```bash
python scripts/cohort_ab_report.py                      # 全量
python scripts/cohort_ab_report.py --since 2026-06-19   # OKR 周期
python scripts/cohort_ab_report.py --json               # 机器可读
```
**已纳入 OKR 每周复盘** (2026-06-24): 核心逻辑在 `app/review/cohort_ab.py` (`compute_cohort_report` + `format_cohort_text`), 被 `app/review/weekly_review.py:generate()` 调用为 `cohort_ab` 段, 经 `app/core/scheduler_triggers.py:_format_weekly_review` 渲染进飞书推送文本。每周五 `check_weekly_review` 触发器自动推送, 无需单独 cron。

## 四、未做 (defer, 含理由)

| 项 | 理由 |
|---|---|
| ORB 路径 cohort 分支 (附录 B.2) | ORB 是 n=11 小路径; 入场不走 order_executor 故 pos 无 cohort, 需 3 点改动 (注入+evaluate 签名+调用点); 且 ORB 本质日内突破策略, 隔夜持仓语义可疑。ROI 低, defer。 |
| `momentum`/`spx_change_percent` 字段映射 | 改动1 的 minor 洞 (prompt 要但 from_raw 没映射), 不直接命中 OKR, 单独跟进。 |
| 放宽触发窗口 / 扩 watchlist | OKR Week 1 已列, 属交易 config 调参, 非代码补充, 不单方面改。 |

## 五、下一步 (按 OKR 节奏)

1. **让实验跑 ~2 周** (至 ~7/8), 每组累计 ≥30 笔。
2. 每周复盘跑 `cohort_ab_report.py`, 看决策门禁:
   - **PASS** → 主线 EOD 强平改为盈利仓可选过夜 (推广), 这是 KR2.2 达 $1000 的路径。
   - **FAIL** → 信号/持仓周期不匹配, 回日内另寻 alpha (IC report 附录 B.4)。
3. 配合 IC 度量 (`ic_tracker.compute_playbook_ic` T+3, KR2.3): 改动1 让 decision_id 干净分组, cohort B 持夜到 T+5 后 IC(T+5) 应进一步上升。

## 六、一句话

退出规则对齐 T+5 alpha 是 OKR 盈利/胜率的最大杠杆, 实验已全链路跑着; 此前缺度量, 现已补齐 `cohort_ab_report.py` + 决策门禁。剩下的是**让它跑 2 周拿样本, 再用门禁决定推广或回退** — 不是继续加代码。
