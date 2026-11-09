# RFC-190 VIX 口径与硬拒门：代理腿的真实新鲜度、系数漂移与三选项

- 日期：2026-11-07
- 触发：DEB-20261106 §22.3 复核时发现同日两个 VIX（18.04 vs 28.18）横跨硬拒阈值 28
- 状态：**待用户裁决**（本 RFC 不含任何行为变更；已落地的只有审计脚本 + 一处注释更正）
- 关联：DEB-20261106 §22.3 / §二十三；DEB-20260914 §八（同型口径分裂，当时 14.71 vs 17.84）；
  `app/data/vix_source.py`、`app/analysis/market_state.py`、`app/risk/vix_guard.py`

## 1. 三个实测（一条命令可复跑：`venv/bin/python scripts/vix_caliber_audit.py`）

**A. 盘前那条"实时代理"没有当日新鲜度。** `fetch_vixy_via_opend` 读富途快照的 `last_price`，
而 `last_price` 在盘前停留在上一时段收盘。实测 `data/da_checkpoint/*/US.*.raw.json`（**按日聚合当日全部 raw 文件**）：
2026-10-07～11-06 的 **23/23 个交易日**，盘前 `vixy_price` 的取值集合全部 ⊆ {前一交易日 VIXY 收盘}。
⇒ 盘前 08:00 ET 的 VIX 与 FRED T-1 收盘**同时点**，只差一个换算系数；
"盘前能捕捉 VIX 跳升"这句注释与事实不符（已就地更正注释，未改代码）。
（口径提醒：只取当日第一个标的的 raw 会误报"某日缺字段"——实测 11-02 单文件采样得 None，按日聚合后 23/23 全等。）

**B. 0.85 这个常数系数不成立。** 同一收盘时刻 `VIX_spot / (0.85 × VIXY_close)`，近一年 n=251：

| 统计 | 值 |
|---|---|
| 中位 | 0.88 |
| p10 / p90 | 0.68 / 1.13 |
| 最小 / 最大 | 0.61 / **1.56**（2026-11-05，年内极值） |

逐月中位：`2025-12 0.65 → 2026-03 0.91 → 2026-07 0.95 → 2026-09 1.08 → 2026-10 1.18 → 2026-11 1.35`。
⇒ 一年前代理**高估** VIX 约 50%，现在**低估**约 26%，崩盘日低估 56%。常数系数不是"换算"，
是"用一条不会更新的经验式把两个不同标的绑在一起"。

**C. 换成"现货>28"会拦掉什么（A10 窗口 2026-07-01 起，90 个有成交的交易日 / 791 笔 / 净 −$1,867.33）：**

| 判据 | 命中交易日 | 当日笔数 | 当日净额 |
|---|---|---|---|
| 代理 > 28（现役） | **0 天** | — | — |
| 现货(T-1 收盘) > 28 | **1 天：2026-11-06** | 18 | **+$109.42** |

⇒ 把口径"修正"成现货，唯一新增拦截的是窗口内**最赚钱的一天**；而真正该被"预警"的 11-05
（当日收盘 VIX 28.18）在盘前根本不可能被任何 T-1 数字看见。这与 §五 轴 A「最普跌桶毛利最高」
是同一件事的另一个侧面：**VIX 阈值型拦截在本系统上是负 edge**。

## 2. 三个选项

| 选项 | 内容 | 性质 | 代价/收益 |
|---|---|---|---|
| **A（推荐先做）** | **纯标注**：DA brief / 看板 / `data_health_check` 把 VIX 值旁标注口径 ——「VIX 代理(VIXY×0.85, T-1 收盘)」vs「VIX 现货(T-1 收盘)」；不改任何数值 | 零行为变更（L5） | 让"看见 13.91 = 平静"这个误读消失；30 分钟内可完成 |
| **B** | 把硬拒门判据换成「现货 T-1 收盘 > 28」 | **行为变更**（L2） | 实测代价 = 拦掉 11-06（+$109.42/18 笔）；收益 = 0 笔已验证的救回。**不建议** |
| **C** | 盘前改读 `pre_price`（恢复"实时代理"的原意）+ 用滚动 60 日中位比值替代常数 0.85 | **行为变更**（L2，且引入"门槛随标定漂移"） | 才真正拿到盘前新鲜度；但需要先证明 premarket VIXY 报价质量（成交稀疏），并重标定阈值 |
| D | 只记录不动作（现状 + 审计脚本） | 零 | 保持现状，问题留档 |

**推荐顺序：A 立即做（零风险，修的是语义错误）→ C 走离线影子验证（record-only 记录
「若用 pre_price/滚动系数，读数会是多少」）→ B 明确不做。**
A2（把 T-1 现货也喂给模型，让模型看见两个数）属**输入变更**，须按 §14.4/§18.3 做 N≥10 噪声基线后再谈。

## 3. 预注册判据（若选 C）

- **观测面**：影子行 `data/shadow/vix_caliber_shadow.jsonl`，字段
  `{ts, phase, vixy_last, vixy_pre, factor_static, factor_roll60, vix_proxy_static, vix_proxy_roll, vix_spot_prev_close, would_block_static, would_block_roll}`；**零交易影响**。
- **首窗** 10 个交易日；判据（三项全过才提请 enforce）：
  1. `pre_price` 非空率 ≥ 80%（盘前成交稀疏则本方案不成立，直接结案）；
  2. 滚动系数与现货同时点比值的绝对误差中位 ≤ 0.10（现在常数系数是 0.26/0.56）；
  3. 影子 `would_block` 与现役判据的差异日 ≤ 2 个交易日（防止悄悄放量/缩量）。
- **停止条件**：任一项不过 ⇒ 维持现状 + 本 RFC 记 KILLED；**不得**为了过门去调判据。

## 4. 回滚

A 是文案层：改回原字符串即可。C 落地时开关 `risk.vix_proxy_live_enabled`（默认 false），
回滚 = 置 false；不需要 revert 代码。本轮已落地部分（审计脚本 + 注释更正）回滚 = revert 提交。

## 5. 已落地（本轮，零交易行为）

- `scripts/vix_caliber_audit.py`（只读审计，无 cron）+ `tests/test_vix_caliber_audit.py`（4 例）；
- `app/data/vix_source.py` 注释更正：把"盘前报价有效"改为实测结论（代码零改动）；
- DEB-20261106 §二十三 记录读数与结论。

## 6. 我不知道的（诚实边界）

- 富途 `pre_price` 在 VIXY 上的可用率与质量**未测**（本 RFC 的判据 1 就是去测它）；
- VIX 期货期限结构（contango/backwardation）本轮未取数——它是"为什么系数会漂"的因，
  但富途不供 VIX 期货，需另找数据源；
- 11-05 那种"当日从 16.61 冲到 28.18"的日内跳升，任何盘前口径都看不见；
  能看见它的只有**日内**价格行为（轴 B 已证无判别力），不要用 VIX 口径去补这个洞。

## 7. 已落地（第二轮）：市场环境账本（源头真值）

**问题**：口径修准了没人用 = 零价值；而"用"的前提是有一个**真值载体**，不是每次审计都现拉。

**交付**：`scripts/market_env_ledger.py`（只读）+ `data/market_env_daily.jsonl`（一行一交易日）。

| 字段 | 来源 | 为什么需要 |
|---|---|---|
| `vix_close` | CBOE 官方日线 | 真值本身（1990 起，免费） |
| `vix9d` / `vix3m` / `term_slope` | CBOE 官方日线 | 期限结构：`VIX9D/VIX3M-1`，>0 = 倒挂（近月恐慌） |
| `vix_pct252` | 派生 | 分位，避免用绝对点位跨周期比较 |
| `rv20_spy` | 自算（Yahoo SPY 日线） | 已实现波动，**完全自源、零延迟、可回放** |
| `spy_ret_pct` / `qqq_ret_pct` | Yahoo 日线 | 当日市况，做"环境 vs 执行"拆解 |
| `vix_proxy_premarket` / `vixy_price` | 本地 DA checkpoint | **近似值不删**，与真值并排留证 |
| `proxy_gap_pct` | 派生 | 代理相对"真值(前收)"偏离 %，>0 = 代理低估 |

复跑：`venv/bin/python scripts/market_env_ledger.py build` / `... report`。
消费方：①盘前健康检查新增一行「环境真值」（过期 >30h 自动重建，取数失败降级 🟡 不阻断）；
②复盘/研究取 `data/market_env_daily.jsonl`。**无新增 cron、不接交易路径。**

首次产出（2026-07-01～11-06，93 行）：
- 代理 vs 真值：23 天有对照，**中位 −16.7%**，`|差|≥20%` 的 **7 天**，最差 **−36.0%（11-06：18.04 vs 28.18）**；
- 11-05 期限结构 **倒挂 +27.8%**（VIX9D 32.66 > VIX3M 25.55）—— 这是代理口径**结构上**看不见的信息。

**修订 §6 的两条"我不知道"**：
- 期限结构**已有源**（CBOE VIX9D/VIX3M 官方日线，2 年 501 个交易日中倒挂 53 天 = 10.6%），不需要 VIX 期货；
- `pre_price` 质量仍**未测**（C 的判据 1），本条不变。

**已知未决（不在本 RFC 范围）**：`data/vix_cache/vix_data.json` 仍走 FRED `VIXCLS`，比 CBOE 官方日线**慢一天**
（实测 11-07 晨：cache 28.18 = 11-05 收盘，CBOE 已有 11-06 = 25.64）。它是看板/风险路径的读数，
**换源=改门**，须另开 RFC，本轮只登记、不动。

## 8. 已落地（第三轮，2026-11-09）：选项 A 接到消费端 + 选项外分级判据回放

**为什么还要做一轮**：§2 的 A（纯标注）在上一轮只落到审计脚本与健康检查一行，
真正读 VIX 的地方（看板药丸/风控行、日评分文案、持仓论据、`vix_data.json`）**仍是裸数字**，
而缓存里连"数据是哪天的"都没有（只有写入时刻）。用户令「让真实有效」= 把标注接进消费面，
并先验证分级用法是否值得（否则只是给无 edge 的开关刷漆）。

**改动（零交易行为）**：
- `app/data/market_env_truth.py`（新）：口径词表 + as-of + 真值/偏离/一行文本；
  只读 `data/market_env_daily.jsonl`，不设阈值、不联网、不兜底。
- `vix_monitor`：缓存新增 `as_of`（FRED CSV 首列），`.` 占位行跳过后继续找最近有效观测。
- 看板 `data_service.get_vix_detail()` + `/api/market/temperature`、`/api/risk/status` 新增
  `vix_detail`（既有 `vix` 浮点契约不动）；药丸/风控行加口径角标（悬停显示真值行）。
- `day_score._get_vix_meta()`：默认 20.0 明确标为「默认值(取数失败)」；
  `order/thesis.py` 论据栏带口径与 as-of。

**线上实测**（重启后）：`{"vix":28.18,"source":"fred","as_of":"2026-11-05"}`；
API 返回 `VIX 28.18 (FRED VIXCLS 现货收盘·as of 2026-11-05) | 真值 25.64 现货收盘 2026-11-06 | 分位 98.9% | 期限 +10.5% 倒挂 | rv20 19.71%`。

**选项外结论（分级降风险）：不做**。`scripts/vix_truth_tier_replay.py`（784 笔 / 90 日，
真值取 T-1 收盘防前视）：真值 ≥28 仅 1 日 18 笔、net **+109**；<20 档 734 笔 net **−1,928**。
⇒ 高 VIX 日在本窗口**不是**出血源，分级缩仓只会削掉唯一赚钱的高波动日。
诚实边界：≥28 只有 1 天，**无统计功效**；重开条件见 DEB-20261106 §57.3（≥28 累计 ≥10 日再跑，判据预注册）。

**测试**：`tests/test_market_env_truth.py` 8 例（缺失/过期/符号/词表/as-of 落缓存）；
变异验证 = 改符号 + 删 as_of ⇒ 3 红，复原 ⇒ 8 绿。
`test_day_score_ledger_label.py` 按新访问器冻结（守卫语义未变）。

**未闭环**：DA 生料 `market_context.vix.caliber` 仍写"盘前/盘中实时代理"（写入方不在本仓，
见 DEB §57.4）；`vix_data.json` 仍 FRED（比 CBOE 慢一天），换源=改门，另开 RFC。

## 9. 已落地（第四轮，2026-11-09）：DA 生料口径标签（skill 侧，只改语义不注入真值）

§57.4 记的"写入方不在本仓"已定位：DA 采集由 `~/.hermes/skills/deep-stock-analysis/` 驱动，
`scripts/data_collector.py::_get_vix` 就是写 `collected.market_context.vix` 的地方。

| 位置 | 修前 | 修后 |
|---|---|---|
| `data_collector._get_vix` 的 `caliber` | 「盘前/盘中实时代理 (VIX 现货无盘前交易)」 | 「VIXY×0.85 代理 · 盘前读到上一交易日收盘 (非实时)」+ `as_of_basis=prev_close` |
| `data_collector._get_vix` docstring | 「盘前 VIXY 报价有效」 | 实测口径：23/23 交易日盘前 `last_price` = 上一交易日收盘 |
| `llm_synth._format_data_for_prompt` | 「[代理值: US.VIXY×0.85 校准, VIXY=…; VIX 现货无盘前交易]」 | 追加「口径=上一交易日收盘, **非今日实时 VIX**」 |
| `llm_synth` VIX 行涨跌 | 「今日+0.0%」（盘前 last==prev_close ⇒ 恒 0.0，被读成"今日波动率没动"） | 「今日涨跌不可得(盘前快照=上一交易日收盘)」；非代理源仍照印真实 chg |

**实测渲染**（用 2026-11-06 生产生料 `US.AMD.raw.json` 回放）：

```
- VIX: 18.04 今日涨跌不可得(盘前快照=上一交易日收盘) [代理值: US.VIXY×0.85 校准,
  VIXY=$21.22; 口径=上一交易日收盘, 非今日实时 VIX; 现货无盘前交易]
```

**为什么不顺手把真值塞进 prompt**：那是 §2 的 A2（输入变更），本 RFC 自定门槛是 N≥10 噪声基线；
本轮只消除"标签与事实相反"这一条语义错误（零数值、零阈值、零判定）。

测试：skill `tests/test_vix_caliber_label.py` 4 例（prompt 文本、chg 抑制双向、无代理元数据时不臆造、
源码契约）；变异验证 = 标签回退旧文案 + 删「非今日实时」⇒ 2 红/3 绿；关掉 chg 抑制 ⇒ 1 红/4 绿；
复原 ⇒ 4 绿。skill 全量：**135 passed / 1 pre-existing time-dependent failure**
（`test_prompt_source_truth::test_prev_close_apply_changes_snapshot_and_prompt`：fixture 日K 为
2026-10-20/21，`reconcile_prev_close` 的 `gap_days>4` 跳过 ⇒ 与本批无关，未动）。

## 10. 已落地（第五轮，2026-11-09）：输入链准确性审计（两个真缺陷 + 两个死门 + 翻转量化）

**改（均为口径/可见性，不改成交集合）**
- `market_state._fetch_vix_data`：FRED 末行 `.` 不再吞掉整条腿（向前找最近有效观测），
  日频槽位升为 `(value, fetched_at, as_of)` 并兼容旧 2 元组；日志带 `as of`。
- `position_exit_monitor._get_vix`：`VIXY×1.05`（方向与 SSOT 相反）改为 `vixy_to_vix()`(0.85)；
  `provider.get_vix()` → `get_vix_or_none()`（不再把兜底 20.0 当真值缓存）；ATR 缺 VIX 时显式 WARNING。
- `pre_trade_check`：两处 `MarketStateChecker._fetch_vix()`（方法不存在，恒 AttributeError）不再静默，
  首次失败打 WARNING 并点名 RFC-190 §10；**门仍休眠**（复活属行为变更，未做）。

**不改（待裁）**
- 阈值表 (15/18/20/22/25/28/30/35) 与 regime 评分表仍两把尺子共用；翻转量化见 DEB §58.3
  （15 档 65% 天不一致；18/23 天 regime 分项偏乐观，3 天差 3 分；30/35 无样本）。
- `llm_pipeline` 的 `18.0`、`market_data_router.get_vix()` 的 `20.0` 哨兵保留登记（DEB §58.4）。

**测试**：`tests/test_vix_consumers_input_accuracy.py` 6 例 —— FRED `.` 回退、as_of 持久化与旧 2 元组兼容、
VIXY 腿走 SSOT、不缓存 provider 默认值、源码无 `*1.05`、死门留痕且未被复活。
变异验证：还原末行取值 + 还原 `*1.05` + 还原 `get_vix()` ⇒ **4 红/6 绿**，复原 ⇒ 6 绿。
VIX 相关 6 个测试文件合计 **74 passed**。

## 11. 独立复核补记（2026-11-09）：已修路径有效，但全链尚未闭环

第三视角只读复核确认：CBOE 11-06 的 VIX/VIX9D/VIX3M = 25.64/27.18/24.60，与本地环境账本一致；
FRED 最新有效值仍是 11-05 = 28.18。§1A/§1B 与 DEB §58.3 的脚本复跑结论成立。

但不得把 §10 解读为「VIX 全生产链已准确」：

1. `AlphaVantageProvider._fetch_fred_vix` 仍有同一个末行 `.` 吞腿缺陷；`premarket_enrichment` 的宏观序列与
   `vix_monitor.get_term_structure` 的 VXVCLS 也未统一成「最近有效观测 + as-of」。
2. `market_state` 在 live proxy/AV 成功分支没有重置 `_vix_last_as_of`，可能把上一次 FRED 日期贴到不同来源。
3. `position_exit_monitor` 的 1.05→0.85 会改变降级路径 ATR 宽度，§10 的「不改成交集合」只能表示未改入场规则，
   **不能宣称零退出行为变化**；实际命中频率仍未知。
4. DEB §59 另证实两项非 VIX 的 P0 输入错误：硬编码经济日历会输出假「本周无重要事件」；DA 个股/行业涨跌
   混用盘前与昨收口径，行业「5日」还只计算 4 个收益间隔。

因此本 RFC 状态仍为 **待裁决/未闭环**：A2 真值注入继续受 N≥10 噪声基线约束；先完成 DEB §59 的
P0/P1 数据正确性修复，不借本复核新增 VIX 硬门或缩仓规则。本轮复核只改文档，未改生产、未重启。

## 12. §59 修复已实施，A2 仍保持原裁决边界（2026-11-09 17:06 CST）

DEB §59 的 FRED 最近有效观测、全源 as-of、同日利差、动态经济日历、DA 时段有效价和行业 6 日 K 修复已落地。
完整实现、离线 A/B、实时探针和测试证据见该 DEB §60；机器回放报告为
`docs/analysis/2026-11-09-da-input-caliber-replay.json`。

- 36 份 raw 的候选数保持 36，177 个展示 peer 全保留；35/35 有效盘前快照改用真实有效涨跌。
- 旧 raw 无法回溯的行业价格/5 日收益显示 `N/A`，不伪装成已修复数值；新采集链输出真实 6 收盘间隔收益。
- LLM 缺数写 `N/A`；既有质量门 18/20 回退分与阈值不改，并显式标记非观测输入。
- 服务于 17:06 按军规重启，PID=2339967，启动冒烟全过，启动时 5 个未平仓标的重启前后不变；
  随后 PLTR/AVGO 分别按既有 `SL_BREAKEVEN`/`SL_TRAIL_LOC` 正常平仓，与本批输入修复及候选数量约束无关。

**A2 状态不变**：本批没有把 CBOE 现货真值新注入 DA，也没有新增 VIX 硬门、分级缩仓或仓位规则。
现货真值注入仍须完成既有 N≥10 噪声基线后另行裁决；不得把本次数据正确性闭环写成 A2 已批准。

## 11. 第六轮（2026-11-09）：regime VIX 分项反事实回放 = 不改

`scripts/regime_vix_caliber_replay.py`：54 个 regime 日，仅 2 天换档（3.7%，均朝更保守），
换档日净额 +73.81 / −201.60（合计 −127.79），未换档 52 天 +2,660.09。
11-06 因 18.04 与 28.18 同落 20–30 档而不换档 —— 粗档位吸收了口径差。

**裁决**：阈值表不动；细阈值（22/25/28）所在门仍休眠，禁止仅用代理复活。
重开条件沿用 §10（真值 ≥28 累积 ≥10 交易日重跑同一脚本）。

另修 `pre_entry_gate` 的 `vix=None → TypeError` / `vix=0.0` 渲染（默认关闭，潜伏缺陷）。

## 12. 第七轮（2026-11-09）：水平/变化分离（只读观测）

标定（361 日）：ΔVIX% ≈ **2.015 × ΔVIXY%**，R²=0.816；滚动 60 日 1.48–2.60。
同日预测 MAE：昨收真值 1.041 / VIXY×0.85 **7.676**（bias +7.06）/ 真值锚定×2.02 **0.431**。

落地：`app/data/vix_estimator.py`（估计+跳变判据）、`vix_source.fetch_vixy_quote_via_opend`、
`vix_monitor.check_vix_level` 附加 `estimate`/`jump`（级别语义逐字不变）、
`scheduler_core` 独立推送、`data_service.vix_detail.estimate`。

不变项：任何阈值/仓位/DA prompt/`vix_data.json` 的 vix·source·as_of 均未动。
测试：13 例，变异 4 红；重标脚本 `scripts/vixy_beta_calibration.py`（手动，无 cron）。
