# DEB-20261023 Finnhub 429 治理（A1 已落地）与「富途为主」的 surprise 源迁移（B 预注册前测量）

> 用户裁决：**「按建议执行」**（2026-10-23）＝ A1 立即执行（测量层零行为变更）＋ B 立 DEB（不许盲切）。
> 本文档同时完成两件事：①A1 的落地记录与活体读数；②B 的方向、证伪过程、预注册不变量与回滚。
> **重要更正**：本会话早前的口头判断「富途日历只有 estimate、无 actual，surprise 只能走 beat_rank 且必然漏小盘」
> 已被本文 §2 的实测**证伪**，B 的真实落点与本会话初版建议不同。以本文为准。

---

## 1. A1 落地（已完成，纯测量层）

### 1.1 改的是什么

| 项 | 事实 |
|---|---|
| 载体 | 系统 crontab 两行 EXT 入场执行器（`*/5 16-22 * * 1-5` premarket、`*/5 4-8 * * 2-6` afterhours） |
| 改动 | 命令前缀新增 `EXT_EARNINGS_CRITERION_SHADOW=false`（其余 env/脚本/参数逐字不动） |
| 备份 | `/root/crontab_backups/crontab_before_429fix.txt`（370 行）→ `crontab_after_429fix.txt`；`diff` 仅 2 行变化 |
| 生效方式 | cron 每 tick 起新进程 ⇒ 下一轮 tick 即生效，**不重启服务** |
| 回滚 | 删掉该 env 前缀，或 `crontab /root/crontab_backups/crontab_before_429fix.txt` |

### 1.2 为什么安全（代码级，不是推测）

`scripts/extended_hours_entry_executor.py:2464-2479`：

```python
if EARNINGS_CRITERION == "legacy_period":          # 回滚路径
    ...
else:                                              # 生产默认 calendar_actual
    new = _actual_with_day_union(ticker, session, event_day)   # ← 无条件执行，不受 SHADOW 门控
    legacy = _earnings_surprise_legacy_finnhub(ticker) if EARNINGS_CRITERION_SHADOW else (None, None)
    res, reason = new                                          # ← 判定只取 new
```

* 生产 `EARNINGS_CRITERION=calendar_actual`（cron 未设 ⇒ 代码默认值）⇒ `res/reason` 恒来自 `new` 臂。
* `EXT_EARNINGS_CRITERION_SHADOW` 只门控 ①legacy 臂的**逐票** `company_earnings` 调用 ②台账写行。
* 429 来源 = `_earnings_surprise_legacy_finnhub` 的 `c.company_earnings(ticker, limit=1)`（:2252），日志形态
  `[ext_entry] XXX finnhub earnings failed: FinnhubAPIException(status_code: 429)`。

### 1.3 四臂只读探针（零外部调用，全打桩）

脚本 `/root/.hermes/cache/scratch/probe_earnings_shadow_off.py`，生产 venv + `PYTHONPATH=.`，
`EXT_EARNINGS_CRITERION_SHADOW=false` 起新进程：

```
fresh-process env read: SHADOW=false -> module const SHADOW=False, CRITERION=calendar_actual
A1 production(env-driven)  criterion=calendar_actual shadow=False -> res=5.0 | new_calls=1 legacy_calls=0
A2 old shadowed            criterion=calendar_actual shadow=True  -> res=5.0 | new_calls=1 legacy_calls=1 shadow_rows=1
B  rollback path           criterion=legacy_period   shadow=False -> res=4.0 | new_calls=0 legacy_calls=1
```

读法：A1 与 A2 的判定结果**逐字相同**（`res=5.0`），差别只在逐票 legacy 调用 1→0；
回滚路径 `legacy_period` 语义未被本次改动破坏。

### 1.4 活体读数（19:05:10 CST tick，改后第一个 tick）

| 指标 | 改前 | 改后 |
|---|---|---|
| 当日 429 累计（`grep -c 429 logs/ext_entry.log`） | 581 | **581（+0）** |
| 台账行数 `data/ext_entry_earnings_criterion_shadow.jsonl` | 21450 | **21450（+0）** |
| 生产闸输出 | `闸计数[premarket] … earnings=2 … earnings_src_err=0` | 同形照常打印，`earnings_src_err=0` |
| 事件源 | `mode=calendar_finnhub_union futu=68 finnhub=6` | 逐字不变 |

即：判定链照常跑（候选 115 → 四闸），被掐掉的只有 429 风暴与台账写入。

### 1.5 配套登记（防两周后假红）

`app/playbook/shadow_registry.py` ShadowItem `ext_entry_earnings_criterion_shadow`：

* `stale_expected=True` + 注释说明停采原因/恢复路径 —— 否则 `check_mounting` 的产出超龄阈值（`_STALE_DAYS=14`）
  会在 ~11-07 起报「产出超龄 疑似断流」，是**假红**（写方被用户裁决停，不是断流）。
* `no_adjudication_reason` 追加「2026-10-23 起写方暂停(429治理), 台账冻结于 21450 行」。
* `_RECHECK_NOTES` 同条 read_at→10-23、recheck_after 仍 10-31（财报季末/OKR 结算窗）。
* 验证：`REGISTRY` 仍 94 条、该项 `stale_expected=True`、`check_mounting()` 总告警 **0**。

### 1.6 测试

```
tests/test_ext_entry_earnings_criterion.py
tests/test_ext_entry_deb20260923_contract.py
tests/test_ext_premarket_amc_pairing.py
tests/test_ext_entry_observation_decoupling.py                 48 passed
tests/test_shadow_mount_sentinel.py
tests/test_control_loop_ledger.py
tests/test_shadow_artifact_consumers.py
tests/scripts/test_ext_earnings_criterion_eval.py
tests/test_ext_entry_earnings_criterion.py                      66 passed
```

### 1.7 本次改动的代价（必须记账）

对照台账**停止增长**，判据② 的 10 月财报季人力裁决（RFC-097 §预注册判据②「修口径放回多少票」）
在冻结的 21450 行 / 冻结的 `legacy_blocked_new_pass` 读数上做，或先选 A2（legacy 臂加 (票,日) 缓存 +
≤50 次/分令牌桶）再恢复台账。**这是取舍，不是免费**。

---

## 2. 关键证伪：「富途只能提供 beat_rank 的 surprise」是错的

### 2.1 早前判断（现已撤回）

会话初版说：富途原生只有 `get_earnings_beat_rank` 能给 beat 证明，且该榜天然偏大中盘、小盘必漏 ⇒
「surprise 走富途为主」必须保留 Finnhub 逐票/全市场兜底。**这个前提错了。**

### 2.2 证伪源（两条只读探针，均在 OpenD 实弹）

**探针 1** `/root/.hermes/cache/scratch/probe_futu_calendar_actual.py`（`get_earnings_calendar`，2026-10-16~10-22）：

```
rows=298  cols=[security, name, earnings_date, earnings_timestamp, pub_type, period_text,
                eps_actual, eps_predict, revenue_actual, revenue_predict,
                ebit_actual, ebit_predict, option_volume, iv, iv_rank, iv_percentile,
                market_cap, price]
eps_actual 非空=252 / eps_predict 非空=244 / 两者皆有=242 / beat(actual>predict)=140
pub_type: {AFTER:160, BEFORE:124, REGULAR:14}
```

⇒ **富途日历自带 eps_actual / revenue_actual / ebit_actual 与 pub_type（相位）**，不是只有 estimate。
现有代码 `app/data/earnings_calendar.py:427-429` 只把 `eps_predict` 映射成 `estimate`，
`eps_actual` 被**读后丢弃** —— 数据一直在手，只是没接线。

**探针 2** `/root/.hermes/cache/scratch/probe_futu_calendar_mcap.py`（同窗口市值分档 × eps_actual 填充率）：

```
bucket      rows eps_actual非空  fill%
<$0.3B        36            8  22.2%
$0.3-2B       64           55  85.9%
$2-10B        88           84  95.5%
$10-50B       62           58  93.5%
>=$50B        48           47  97.9%
market_cap min=$0.00B p10=$0.32B median=$5.88B
```

对比 `beat_rank` 实测（`probe_futu_beat_rank.py`，count=200）：

```
rows=200  released_date range 2026-07-26 → 2026-10-22
market_cap min=$49.4B p10=$56.9B median=$97.8B | <$10B: 0 | <$2B: 0
beat_ratio min=0.062 median=10.136 (榜内全 >0)
detail_post_period: {BEFORE:107, AFTER:79, REGULAR:10, INTRADAY_TRADING:4}
```

⇒ 偏大中盘的是 **beat_rank**（≥$49B），不是日历。日历中位市值 $5.88B、$2-10B 档 actual 填充 95.5%。
**「小盘必漏」的结论只对 beat_rank 成立，对日历不成立。**

**探针 3（阿喀琉斯之踵，2026-10-23 07:15 ET 实弹）** `/root/.hermes/cache/scratch/probe_actual_timeliness.py`
—— 同一时刻比「当日 BMO 票的 actual 是否已填」：

```
futu  : 当日 rows=22, pub_type BEFORE 19 票 → eps_actual 非空 1 票 (5.3%), 缺 18 (AXP/SLB/INFY/BAH/...)
finnhub: 当日 rows=27 → epsActual 非空 7 票 (25.9%)
同票同刻都有 actual=0 | 仅 futu 有=1 | 仅 finnhub 有=7   ← 两源近乎不相交, 且 futu 更慢
```

这与 skill `ftseek-extended-hours-trading` 已实测的 2026-09-23 结论一致：**当日 BMO 的 actual 在盘前窗内结构性迟到
(1-5 小时)，「换源」不解这个问题**。本探针进一步证明 **futu 不是更快的那一个**（5.3% vs 25.9%）。

⚠️ 口径提醒：探针 2 的 95.5% 填充率是**历史日窗**（2026-10-16~10-22，绝大多数票已披露完成）的读数，
**不可外推为「当日盘中时效」**。这两件事必须分开报：**存量覆盖** futu 好，**实时到达** futu 更差。

### 2.3 因此 B 的正确落点变了（第二次修正）

| 方案（早前设想） | 现在的问题 |
|---|---|
| beat_rank 做 surprise 主源 | count 上限 300、按 beat_ratio 排序的 top-N，`released_date` 横跨 ~3 个月（需日期过滤），天然丢小盘 ⇒ 做**主源**不合适 |
| Finnhub 逐票 company_earnings 做 beat 证明 | 就是本次 429 的风暴源，且是 per-ticker 调用 |
| Finnhub 全市场日历（每进程 1 次） | 现状生产源，本身不 429；缺点仍是「Finnhub 为主」 |

**新落点（第二次修正）**：**不是「futu 换掉 Finnhub」，而是「并源补盲」**——因为探针 3 证明 futu 在当日 BMO 上
**更慢且不成规模**，替换主源会立刻减少放行（等于给判据② 加严，方向错了）。B 的真实价值只剩两条：

1. **退役逐票调用**：`company_earnings` 那 581 次/日 的 429 风暴源可以代码级删除（A1 已从 cron 层面停掉它）。
2. **补 AMC 配对人群**：昨日 AMC 的 actual 次日早晨两源都已在库 ⇒ futu actual 可用于 RFC-102 的「昨日 AMC」取数日兜底，
   把「futu 记当日 BMO / Finnhub 记昨日 AMC」这类源间日期打架的票补齐（现役只按**日期**并源，不按**源**并源）。

⇒ 若做，形态是 `actual = futu_actual ∪ finnhub_actual`（任一非空即用，两者都空才 `actual_missing`），
**判据② 只会变宽不会变窄**（需明确批准，因为它是放宽）；并且必须记录「这条放行是靠哪个源救回的」以便事后判优劣。
beat_rank 降级为可选 P2 **只读交叉校验**（它的价值在 `detail_post_period` 与 `earning_day_chg`，不在覆盖）。

---

## 3. B 的阶段化设计（未执行，待批准）

> **2026-10-23 用户裁「不用跑影子」** ⇒ 本节**不含**常驻影子臂（不新建 `futu_calendar_arm`、不长期并跑 A/B）。
> 由此 B 只剩两种可行形态：**(a) 直接切换 + 在线读数 + 一键回滚**，或 **(b) 停在提案**。
> 代价必须写明：没有并跑对照 ⇒ 判定分歧只能在切换后靠事后核对发现，**且 I3「分歧 100% 可解释」无法在切换前兑现**，
> 只能改成「切换后 n 笔内逐笔核对，出现不可解释分歧立即回滚」。时效性/口径差因此必须靠**一次性点测**先证伪（§3.0，风险详见 §4 第 1/2 条），
> 不能靠攒样。

### 3.0 一次性点测（不落盘、不起影子，随时可跑，约 2 次 API 调用）

| 点测 | 做法 | 现状 |
|---|---|---|
| 时效性 | 当日 BMO 票的 actual 填充率快照（futu vs finnhub，同一时刻） | **已测**（§2 探针 3，07:15 ET：futu 1/19=5.3%，finnhub 7/27=25.9%，交集 0）⇒ 当日 BMO 两源都晚，**并源增量不在这里** |
| 口径差 | 同一批「已发布」票，比较 futu `eps_actual vs eps_predict` 符号 与 Finnhub 日历 `epsActual vs epsEstimate` 符号 | **待测**（若做并源，这决定同一票在源间是否给出相反结论；实测探针 3 里两源当日 actual 交集为 0，说明并源主要是「补缺失」，冲突面估计小，但 AMC 人群必须实测） |
| 覆盖率 | 用**当日候选集**（非全市场）算 futu actual 缺失率，并看 Finnhub 空调度能否补齐 | **部分测**（全市场口径已测；候选集口径待测） |

### 3.1 并源补盲（形态 a，行为变更=放宽，需独立批准）

1. `app/data/earnings_calendar.py` futu 行映射**追加** `eps_actual/eps_predict/revenue_*/ebit_*/pub_type`（additive，不改既有键）。
2. 判据② 取 actual 改为**源并集**：`futu_actual ∪ finnhub_actual`（按现有 `_actual_with_day_union` 的日期兜底之后再叠一层源兜底），
   任一非空即用，两者都空才沿用 `actual_missing` fail-close；命中时落 `actual_source=futu|finnhub` 供事后判优劣。
3. 逐票 `company_earnings` **代码级删除**（A1 的 cron 停采升级为代码退场，`EXT_EARNINGS_CRITERION_SHADOW` 随之失效）。
4. 方向纪律：本次只允许**放宽**（更多票能证明 beat），不得顺带收紧任何一道闸；出现「并源后放行、事后亏」的重复模式见 I4。

### 3.2 预注册不变量（先定后测，禁止事后调阈值）

| # | 不变量 | 判法 |
|---|---|---|
| I1 | **缺失语义不变**：两源都缺 actual ⇒ 仍 `actual_missing` 拦（不得因"并源"而把缺失当默认放行） | 上线后 `actual_missing` 计数照常打印，不得出现 `actual_source=None` 的放行行 |
| I2 | **零风暴**：`grep -c "finnhub earnings failed.*429"` 恒 0 | 逐日累计 |
| I3 | **放宽可归因**：每条「靠 futu 救回」的放行行必须带 `actual_source=futu` + 当日涨幅，事后逐笔核对可解释 | 抽查即可（不需并跑对照） |
| I4 | **同向性**：不得出现「futu 救回而事后证明错放」的**重复模式** | 模式级判，不看单笔 |
| I5 | 前置：§3.0 口径差一项有读数（AMC 人群符号冲突率） | 缺该项不切 |

**禁止**：用切换后样本的 PnL 反推 I1-I5 阈值（阈值购物）；I1-I5 是**不变量**，不是可调参数。

### 回滚

| 层级 | 动作 |
|---|---|
| 一键 | cron env 改回 `EXT_EARNINGS_CRITERION=calendar_actual`（下一 tick 生效，不重启） |
| 更深 | `legacy_period`（旧语义，仍支持，探针 B 臂已验证可跑） |
| 阶段 0 回滚 | 删新增映射键 + 删影子臂（生产判定从未改动，回滚仅等于「少几个字段」） |

---

## 4. 风险与未验证项（切换前必须回答）

1. **时效性已测，结论不利**（§2 探针 3，2026-10-23 07:15 ET）：当日 BMO 19 票 futu 仅 1 票有 actual（5.3%），Finnhub 7/27（25.9%），
   两源交集 0。⇒ 与 2026-09-23 实测（actual 迟到 1-5 小时、换源不解）一致，**且 futu 更慢**。
   因此「futu 为主源」会收紧判据②（放行减少），方向错；并源只在 AMC 配对人群（actual 次日在库）有增量。
2. **口径差未验证**：富途 EPS 可能为 GAAP，Finnhub `epsActual` 多为 adjusted；`actual>predict` 的**符号**可能翻转（尤其一次性项目/减记的票）。
   I3 的可解释清单必须实测出这类票的占比，不能假设「口径差 = 少数」。
3. **微盘填充差**：`<$0.3B` 仅 22.2% 有 actual。EXT 有 `<$5` 价格闸、`market_cap` 闸（`market_cap_missing`/`market_cap_too_low` 计数键存在），
   实际影响面需按「通过前置闸后的候选市值分布」算，不能用全市场 298 行直接外推。
4. **共享 helper 的影响面**：`EarningsCalendar().get_earnings_calendar_with_status()` 同时喂盘前富集/playbook 等，
   阶段 0 的字段追加必须**只加不改**，并跑既有消费方测试（`tests/` 下 earnings 相关全套）。
5. **REGULAR / INTRADAY_TRADING 相位**：日历 `pub_type` 有 `REGULAR`（10 行）；beat_rank 还有 `INTRADAY_TRADING`。
   现役配对表 `_session_event_pairs` 是否把它们归入「同相位」需逐字核对（否则相位口径在迁移中悄然放宽）。
6. **A1 的代价仍在**：若阶段 0/1 拖过 10-31，判据② 的财报季人力裁决将没有新台账行；此时应先做 A2 恢复台账再谈 B。

---

## 5. 待办

> 用户裁「不用跑影子」(2026-10-23) ⇒ 下表**不含**影子/并跑类事项；B 默认停在此文档（形态 b），要推进须单独批准 §3.1 的切换。

| # | 事项 | 触发条件 |
|---|---|---|
| T1 | §3.0 一次性点测（时效性 / 口径差 / 候选集覆盖率），只读、不落盘 | 用户要求推进 B 时（或 10-31 财报季末复盘时顺手做） |
| T2 | `app/data/earnings_calendar.py` 追加 `eps_actual` 等字段（additive） | T1 三项读数无阻塞性发现后 |
| T3 | 并源补盲（`actual = futu ∪ finnhub`）+ 逐票 `company_earnings` 代码级删除 | T1/T2 完成 + 单独批准（这是**放宽**，行为变更） |
| T4 | 判据② 10 月财报季裁决（RFC-097 §预注册²） | 台账已冻结 ⇒ 无新数据，只能按 21450 行既有读数裁；若须新数据先做 A2 |
| T5 | beat_rank 定位重写：从「主源候选」改为「P2 只读交叉校验（相位/当日涨跌）」 | 下次触及事件源时随文档同步 |

---

## 6. 一句话结论

429 的根因是「为客户对照台账而逐票调 Finnhub」，掐掉它不碰任何交易判定（四臂探针 + 活体 tick 双证）。
「富途为主」经三次取证后**降级**：beat_rank 是 top-N/大中盘，不能当主源；futu 日历的 `eps_actual` 存量覆盖好
（298 行/7 日 252 行有 actual，中位市值 $5.88B）但**当日实时到达比 Finnhub 更慢**（07:15 ET：5.3% vs 25.9%，
交集 0）⇒ 替换主源会收紧判据②，方向错。B 的可行形态只剩 **并源补盲**（futu ∪ finnhub，补 AMC 配对人群 +
退役逐票调用），且它是**放宽**，须单独批准。
按用户裁「不跑影子」，B 现在**停在提案**，推进方式只能是 §3.0 一次性点测 → §3.1 并源 + 一键回滚，不得靠长期并跑攒样。