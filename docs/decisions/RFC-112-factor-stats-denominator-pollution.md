# RFC-112: factor_stats 分母污染修复 (非结果行不得进胜率分母)

- 日期: 2026-09-26
- 状态: 已落地 (承接 ORCL 9/24 复盘 P0-4 影响面分析 → 挖出更上游的真病灶; 用户裁「继续」)
- 类型: L1 修尺子 (口径修复, 零行为变更 — 见下方「为何零行为变更」)
- 回滚: `git revert` 本提交 + `UPDATE ... SELECT` 从 `playbook_factor_stats_bak_rfc112` 恢复 (已建备份, 72 行) + `systemctl restart ftseek-us`

## 1. 病根

`app/playbook/factor_analyzer.py::compute_trade_stats` (修复前):

```python
pnl = t.get("pnl", 0) or 0          # NULL → 静默变 0
if outcome == "win" or pnl > 0:  wins += 1
elif outcome == "loss" or pnl < 0: losses += 1
# pnl == 0 → 既不进 wins 也不进 losses, 但 total 已 += 1

win_rate = wins / total                          # ← 分母含非结果行
bayesian_binomial(stats.wins, stats.total)       # ← 内部 losses = total - wins
                                                 #    ⇒ 未成交被当成【亏损】
```

非结果行 = `outcome in (missed, already_held, skipped, void且pnl=0)`，即**从未成交的指令**。它们进了 `total` 却进不了 `wins`，于是：
- `win_rate` 分母被灌水
- 更严重的是贝叶斯：`losses = total - wins` 把未成交**当作亏损**参与 Beta 后验，直接压低 `lower_95`/`upper_95`，而 `classify_factor` 判 negative 的条件正是 `bayesian.upper_95 < 0.45`

## 2. 实测影响面 (2026-09-26, 900 指令样本)

**污染规模**: `outcome` 分布 = missed 363 / win 297 / loss 222 / already_held 10 / void 8 → **非结果行占 40%**。

**逐因子 (single 级, n≥20)**:

| factor | n_all | n_decided | 旧WR | 新WR | Δ | 旧verdict | 新verdict |
|---|---|---|---|---|---|---|---|
| news_positive | 484 | 294 | 34.9% | **57.5%** | +22.6pp | negative | **observing** |
| earnings_beat_streak | 285 | 171 | 36.5% | **60.8%** | +24.3pp | negative | **observing** |
| pe_below_industry | 280 | 183 | 35.7% | **54.6%** | +18.9pp | negative | **observing** |
| insider_net_buy | 183 | 113 | 35.5% | **57.5%** | +22.0pp | negative | **observing** |
| above_ma20 | 172 | 106 | 37.2% | **60.4%** | +23.2pp | negative | **observing** |
| sentiment_bullish | 37 | 23 | 29.7% | 47.8% | +18.1pp | negative | negative |
| macd_golden_cross | 31 | 20 | 38.7% | 60.0% | +21.3pp | observing | observing |
| pe_above_industry | 30 | 17 | 23.3% | 41.2% | +17.8pp | negative | negative |
| insider_net_sell | 30 | 17 | 23.3% | 41.2% | +17.8pp | negative | negative |
| volume_expansion | 26 | 16 | 38.5% | 62.5% | +24.0pp | observing | observing |
| below_ma20 | 21 | 11 | 9.5% | **18.2%** | +8.7pp | negative | negative ✅保留 |
| news_negative | 20 | 11 | 25.0% | 45.5% | +20.5pp | negative | negative |

**5 个真实 WR≥50% 的因子被错判 negative**。反向验证: `below_ma20` (真实 WR 18.2%)、`pe_above_industry` (41.2%) 等真负因子**仍为 negative** ⇒ 修复不是「洗白所有 negative」，是精确剔除污染。

## 3. 连带效应 (修复后 reconcile 对账)

`scripts/factor_weight_reconcile.py` (三把尺子对账) 7 项不一致 → **4 项自动归零**:

| 检查项 | 修复前 | 修复后 |
|---|---|---|
| A 正权重负证据 | 4 | **0** ✅ |
| B IC通道反号 | 5 | **0** ✅ |
| F 尺子冲突 | 1 | **0** ✅ |
| G 台账分母口径 | 5 | **0** ✅ |
| C Σhist双计存量 | 11 | 11 (独立问题, 非本 RFC 范围) |
| D 小样本高权重 | 1 | 1 (独立问题) |
| E 罚错 | 2 | 2 (独立问题) |

注: 检查 **G 的定义本身** (`scripts/factor_weight_reconcile.py:25`「verdict=negative 但剔掉非结果行 missed/already_held/void 后胜率≥50%」) 说明**该口径 bug 早已被度量到**，但对账脚本是只读产物、无人消费处置。本 RFC 是把它修回本体。

## 4. 修法

单一改动点 = 口径统一到 `n_decided`，与仓内既有两处正确实现同源：
- `app/metrics/unified_trade_metrics.py:116-123`: `win_rate = wins/(wins+losses)` + `breakeven` 单列
- `app/playbook/horizon_return_model.py:63-64`: 显式 `只保留真实交易结果，排除 missed / void / skipped 等伪结果`

`factor_analyzer` 是唯一漏网者。

```python
_DECIDED_OUTCOMES = {"win", "loss"}   # 与 horizon_return_model 同源

class TradeStats:
    total: int          # 全部指令数 (含未成交) — 仅作"信号出现次数"与 min-sample 门槛
    nonoutcome: int     # 非结果行数
    @property
    def n_decided(self): return self.wins + self.losses
    @property
    def win_rate(self):  return self.wins / self.n_decided if self.n_decided else 0
    @property
    def avg_pnl(self):   return self.total_pnl / self.n_decided if self.n_decided else 0
```

`compute_trade_stats` 分流: `outcome in _DECIDED_OUTCOMES or pnl != 0` → 进 wins/losses；否则 → `nonoutcome += 1`。

三处 `bayesian_binomial(stats.wins, stats.total)` → `(stats.wins, stats.n_decided)`。
两处 `effective_sample_size: stats.total` → `stats.n_decided`。

**关键不变量**:
- `total_trades` **仍落全部指令数** ⇒ `factor_neg_edge_shadow.py:61` 的 `total_trades>=30` 门槛语义不变，reconcile 的 `nonoutcome_share` 仍可见污染规模 (不掩盖)
- `len(trades) < 3` / `< 5` 的 min-sample 门用**原始列表长度**，不受 `stats.total` 改动影响
- `pnl != 0` 兜底: 实测 `outcome=void` 19 笔中 **9 笔 pnl≠0** (4正5负)、10 笔 pnl=0 ⇒ void **不能整类划入非结果行**，靠 `pnl != 0` 保住真实盈亏 (测试 `test_pnl_nonzero_with_other_outcome_still_counted` 锁死)
- `total_pnl` 仍全额累加 (不丢真实盈亏)

## 5. 为何零行为变更

两处 `verdict` 消费方**均已停用**，由 `_FACTOR_BLACKLIST_ENABLED = False` (`pipeline.py:7711`, 2026-07-11 停用) 守卫:
- `pipeline.py:1681` 单因子负 edge 硬拦截
- `pipeline.py:1701` 配对级负 edge 闸门

⇒ 本次修复只改尺子读数，不改任何下单/跳过行为。若未来重开闸门，**必须用修复后的 verdict** (旧 verdict 会拦掉 5 个真实正期望因子)。

这也解释了 7/11 停用时的历史矛盾记录 (`pipeline.py:1678`:「v2 下 news_positive 反而净正 +$70/35笔」却 `verdict=negative`) — verdict 一直是污染口径算出来的。

## 6. 验证

- 新增 `tests/test_factor_stats_denominator.py` **11 passed**:
  - `test_missed_not_counted_as_loss` / `test_already_held_excluded` / `test_all_nonoutcome_yields_zero_not_nan` (无 ZeroDivision)
  - `test_bayesian_denominator_matches_decided` (锁死贝叶斯分母)
  - `test_pnl_nonzero_with_other_outcome_still_counted` (void+pnl≠0 不丢真实盈亏)
  - `test_polluted_factors_no_longer_negative` (reconcile G 项 5 因子, 真实数据回归)
  - `test_genuinely_negative_factors_stay_negative` (反向: 不是洗白)
  - `test_no_factor_becomes_positive` (保守性: 无因子跃升 positive)
- 全量回归 **120 passed** (含 test_playbook_db / test_da_output_contract / test_same_day_hard_stop_cooldown) + **test_phase2_modules 20 passed** (唯一其他 FactorAnalyzer 消费方)
- DB 读回验证 (`sqlite3`, 不信函数返回值): 5 因子 verdict=observing、`effective_sample_size` = decided 数、below_ma20/pe_above_industry 保留 negative
- 已重算并落库: `update_all_stats(90)` → `{'single': 17, 'pair': 34, 'combo': 19}`；reconcile 产物 `generated_at=2026-09-26T16:01:14`，G/A/B/F 全 0

## 7. 与 ORCL 9/24 复盘的关系

复盘 P0-4 主张「修 news_positive 标签映射 (`adapter.py` product_catalyst→news_positive) + 清下游污染」。**影响面分析证否了该顺序**:

1. 原报告定位的根因 `signal_gate.py:299 _CLAIM_NAME_MAP` 是**误判** — 那是 claim IC 比对用别名表，不产生 conditions。真产生点 = `adapter.py:42/47` (`product_catalyst`/`macro_tailwind` → `news_positive`) + `adapter.py:108/114` (关键词 fallback)
2. 原报告称的下游污染 `_HARD_BLOCK_NEG` 黑名单**早已死** (`_FACTOR_BLACKLIST_ENABLED=False`)，活污染点只剩 `f5_lifecycle +1` (`playbook_service.py:1885`) 一处
3. **真病灶是分母口径**，不是标签语义。修好口径后 `news_positive` 真实 WR=57.5% (9月 n=59 WR=78% +$759)、f5 标记的 165 笔成交 WR=69% **+$1170.54** ⇒ 该标签**不是**坏因子，改映射等于拿一个正在赚钱的因子开刀

⇒ 结论: **P0-4 的标签映射修正撤回**，改为修分母口径 (本 RFC)。`news_positive`/`news_negative` 极性不对称 (321 vs 21 = 15.3x) 仍存在，但那是标注覆盖度问题，非本 RFC 范围。

## 8. 遗留 (未做，待裁)

- **C Σhist双计存量 11 项**: `weight_adjustments` 与 base weight 是否重复计入同一 delta，无法唯一分解 (需查 `config/playbook_screening.yaml` 历史)
- **E 罚错 2 项**: `eff<0` 但 `verdict!=negative` 且现金口径 >0
- **f5_lifecycle 权重**: `playbook_service.py:1885` 把 `news_positive` 计入 f5 (165/321 = 51% 独立触发)，语义上 news_positive 不属「生命周期」维度 ⇒ 因子归类错误，但修它=改因子权重=行为变更，需影子
- **`result.anomalies` 零消费方**: DA skill v0.87 只修了尺子，异动数据尚未作为任何门禁输入
