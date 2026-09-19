# RFC-087: 盘前收口强平的「条件收口 + 豁免期只限限价」预注册 (A + E)

> **状态**: 预注册已立 / **影子已落, 未切生产** (`EXT_PREMARKET_FLATTEN_MODE` 默认 `unconditional`)
> **日期**: 2026-09-19
> **作者**: Hermes
> **审批人**: 项目所有者 (用户令「按建议做」= 授权落地 A + E 的影子 + 双窗口回放读数 + 预注册)
> **关联**: `docs/debates/DEB-20260919-extended-hours-profit-lock-and-trail.md`、
> `~/.hermes/skills/quantitative-trading/ftseek-extended-hours-trading/references/premarket-loss-to-open-exit-policy.md`、
> `RFC-083` (超时链条 + 回放费用保真)、`RFC-082` (知情破例预注册先例)
> **变更类型**: L2 (政策级 + 执行链), 走完整 6 Gate; 本批**只到 Gate 5**, Gate 6 (切换生产) 待读数裁决

---

## 1. 问题陈述

盘前开的仓在 09:20 ET 之后被 `pre_close_flatten` **无条件强平** (覆盖一切止盈信号)。三个待答问题:

| # | 问题 | 结论 (见 §3) |
|---|------|-------------|
| Q1 | 盘前反转能否吃到止盈? | 现设计把出场放在反转**之前** ⇒ 系统性漏损, 但幅度远小于旧读数 |
| Q2 | 波动会不会让止损成交在不利极值? | 实证基本不成立; 唯一真尾部 = **跳空穿线** |
| Q3 | 亏损仓该不该给开盘前豁免? | 只该给**浅亏/中亏**; 深亏(破位)档反而要更早走 |

**政策冲突声明 (知情破例)**: 收口强平是 2026-09-17 已落地裁决 (`DEB-20260917 §16.5`)。本 RFC 要改它 ⇒
按 `RFC-082` 先例, **必须预注册后才切生产**; 本批只落影子 + 回放读数, 生产行为逐字不变。

---

## 2. 现状锚点 (代码级)

| 情形 | 现行行为 | 锚点 |
|---|---|---|
| 今日盘前开的仓, 09:20 仍亏 | **无条件强平**, 覆盖一切止盈信号 | `premarket_exit_shadow.flatten_now()` = 开关(默认 true) + `hhmm ≥ EXT_PREMARKET_FLATTEN_FROM(920)` + `_entry_et_date(entry_time)==today_et` |
| 隔夜/盘后 carry 仓 | 不进收口; 走 `hold_to_next_open` → 09:30–09:45 `next_open_exit` | `is_afterhours_carry()` |
| 收口的成交保证 | **尽力而为**: 限价 + `EXT_EXIT_LIMIT_TIMEOUT_S(20s)` 超时撤单, 不追价不市价兜底 | `extended_hours_exit_executor.py` |

---

## 3. ⚠️ 先更正上一批读数: 回放样本有**静默截断** (本次复核发现, 我自己的错)

上一版臂对照 (盘前 n=56: `pre_close −0.402%` / `rth_close −0.101%` / `intraday_sl +0.237%` /
`sl_only −1.613%`, 最差 `−7.281%`) 建在一份**尾部被截断**的样本上, **不得再引用旧数字**:

- **根因**: `replay_ext_entry_sources._kline_chunked` 原用 `CHUNK_DAYS*2` = 12 日历日 ≈ 8–9 个交易日
  ≈ 1,700 根 bar, 而富途 `max_count=1000` ⇒ **每个 chunk 的尾部被静默丢弃**。
- **实测形态**: 7 个交易日 (06-30 / 07-13 / 07-27 / 08-10 / 08-21 / 09-03 / 09-16) 全市场只剩
  `04:05–07:20` 的 bar; 且这些日子**没有 RTH bar** ⇒ `pre_close / rth_close / intraday_sl / rule`
  四条臂在这些笔上读到**同一个 07:20 价格** (56 笔里 9 笔命中, 占 16%)。
- **被污染的典型**: 旧表里"收口最差 −7.281%"= US.SNXX 2026-08-10 (入场 9.45), 它就是
  07:20 价在四条臂上的同一个读数 — 不是"跳空穿线"的证据。
- **连带影响**: 截断日之后一天因缺前一日 RTH 收盘而被整行丢弃 ⇒ 样本日从 60 天缩到 35 天
  (这是"样本为什么这么薄"的真实原因之一, 不是"行情没机会")。
- **修复** (本批同批落地): `_kline_chunked` 块跨度改 `CHUNK_DAYS` 且**触顶即二分重取** (自愈);
  `_kline_cached` 读缓存前过 `_cache_is_complete()` (末根须 ≥19:00 且距窗口 end ≤5 日历日),
  不完整 ⇒ 重取覆写。**修复前先做离线取证**, 见 §7 复现命令。

> 纪律: 任何引用"盘前/盘后臂读数"的文档 (含 skill reference) 必须标窗口 + n + 是否在修复后样本上;
> 旧文档里的 `−0.402% / +0.237% / −7.281%` 一律视为**待更正数字**。

---

## 4. 变更内容 (A + E, 只落影子)

### A 条件收口 (最小改动, 可真伪)
现役与候选模式**共用同一条代码路径**, 只换判据函数 `conditional_flatten_verdict(pnl, armed)`:

| mode | 语义 | 现役? |
|---|---|---|
| `unconditional` (**默认**) | 恒收口, 逐字等于改动前 | ✅ 生产在用 |
| `conditional` | 有浮盈 **或** 利润保护已武装 (BE/TRAIL 线越过成本) ⇒ 收口; 否则**交 RTH 引擎** | 影子 |
| `band` (B 分档豁免) | `conditional` + 深亏破位 (`pnl ≤ −6%`) 仍收口 | 影子 |

- 未识别 mode ⇒ 按 `unconditional` 处理 (拼错开关名不得意外放宽风控)。
- 缺报价 ⇒ 保守收口 (`no_quote`), 不因缺数据放宽。
- "武装"由调用方从 `decision.breakeven_triggered` / `effective_sl` 方向感知推出
  (LONG: `effective_sl > entry`; SHORT: `0 < effective_sl < entry`), **影子与回放用同一套语义**。

### E 豁免期 = 时间窗 × 价格类型 (不是"不出场")
09:20–09:45 只许**限价离场**、禁市价, 到期交 RTH 规则。本链 (盘前/盘后影子 + exit executor)
**从无市价分支** ⇒ E 是**不变量**; 本批把它与"薄盘价差"一起记进影子行, 让"薄盘割损"这个
原始痛点变成**可测量**的, 而不是靠断言。

### 影子字段 (新增 6 个, 写进 `data/premarket_exit_shadow.jsonl` 每行)
`flatten_mode` / `cond_flatten_verdict` / `cond_flatten_reason` / `cond_armed` / `limit_only` / `thin_book`

### 回滚
- 影子无行为变更 ⇒ 无需回滚; 若要切生产再切回: `EXT_PREMARKET_FLATTEN_MODE=unconditional` (env, 改 crontab 行内联或 `.env.us`)。
- 彻底停收口: `EXT_PREMARKET_FLATTEN=false` (既有开关, 未动)。

---

## 5. 预注册判据 (切生产前必须同时满足)

**样本**: 固定样本 `--universe-from` (同一 (日,标的) 集, 保证换臂可配对), 两个窗口:

| 窗口 | 样本 | 说明 |
|---|---|---|
| W1 2026-06-24 → 09-16 (60 交易日) | 盘前 56 笔 / 14 只 | 修复后样本, 与旧表同源可对照 |
| W2 2026-01-02 → 03-10 (45 交易日) | 盘后 15 笔 (Jan–Mar 独立窗口) | 换 regime 的样本外读数 (watchlist 名单为**当前**名单 ⇒ 生存偏差须标) |

**主判据 (逐条, 配对)**:

| # | 判据 | 门槛 |
|---|---|---|
| 1 | 费用后净均值 (配对 vs `pre_close`) | 优于现设计, **两个窗口同向** |
| 2 | 尾部最差 5% | 不劣化 > 1pp |
| 3 | 被割在负而当日收盘转正率 (`reversal_after_cut_pct`) | 下降 (即"少割反转") |
| 4 | MFE 捕获率 (`capture_mean_pct`) | 不劣化 |

**功效 (必须写在裁决里)**: 单笔净 σ ≈ 1.18% ⇒ 检出 +0.15% 级边缘需 n≈481; n=56 的 CI 半宽 ±0.31pp。
⇒ 本 RFC 的读数**只能判方向与尾部, 不能判 0.1–0.2pp 级优劣**; n<40 的臂一律写"证据不足", 不写"负期望"。

---

## 6. 复现命令

```bash
cd /opt/ftseek/FTSeek-US
# W1 盘前 (固定样本: 只含成样笔的标的集, 省接口预算; 完整宇宙见 all_candidates 版产物)
venv/bin/python scripts/replay_ext_entry_sources.py --session premarket \
  --universe-from data/reports/_rfc087_universe_premarket.json --split-half \
  --out data/reports/replay_ext_entry_sources_20260919_premarket_fixed.json
# W1 盘后
venv/bin/python scripts/replay_ext_entry_sources.py --session afterhours \
  --universe-from data/reports/_rfc087_universe_afterhours.json --split-half \
  --out data/reports/replay_ext_entry_sources_20260919_afterhours_fixed.json
# W2 (Jan–Mar 独立窗口, 盘后)
venv/bin/python scripts/replay_ext_entry_sources.py --session afterhours \
  --universe-from data/reports/_rfc087_universe_ah_janmar.json --split-half \
  --out data/reports/replay_ext_entry_sources_20260919_ah_janmar.json
```

判据读法: `summary.paired_vs_base.arms.<臂>` (配对增量/更好·更差笔数/t)、
`summary.arm_diagnostics.<臂>` (worst5_mean / reversal_after_cut_pct / capture_mean_pct)、
`summary.split_half` (前后半分片一致性)、`summary.gates_drift_rows` (固定样本与现役闸门是否已漂移)。

---

## 7. 修复前取证 (离线, 不连券商)

```bash
venv/bin/python - <<'PY'
import json, sys; sys.path.insert(0, ".")
import scripts.replay_ext_entry_sources as R
b = json.load(open("data/replay_cache/US_SNXX_2026-06-23_2026-09-17_K_5M.json"))
print("complete?", R._cache_is_complete(b, "2026-09-17"))     # 旧缓存 → False
today = [x for x in b if x["t"][:10] == "2026-08-10"]
print(len(today), today[-1]["t"])                             # 40 根 / 收尾 07:20 ⇒ 截断形态
PY
```

---

## 8. 局限 (必须与读数并列写明)

1. **样本内代理**: 固定样本来自同一段窗口, 臂间配对成立, 但**不是样本外**; W2 提供方向性交叉验证。
2. **样本集中**: W1 的 56 笔只有 **14 只标的** (同一批 watchlist 标的的多个交易日) ⇒ 相关性高,
   等效自由度远小于 56 (报显著性时必须标这一点)。
3. **09:30–09:35 无止损缝未建模**: A 的"转交 RTH"在回放里用 `intraday_sl` (−5% 或收盘) 代理,
   真实链是 RTH 引擎 09:35 起才动作 ⇒ 回放读数对 A 是**上限**, 不是保真读数。切生产前必须先补缝。
4. **执行层不可回放**: 5m bar 收盘当成交价、无滑点、无盘前逐笔; E 的"限价能否成交"只在影子行里观测。
5. **跳空尾部所有方案同值**: 隔夜跳空一步跨过所有线, 收口/豁免都不改变它 (只能靠仓位或券商侧隔夜单)。

---

## 9. 反方审查 (为什么"无条件豁免"不能做)

1. **最大单笔亏损类别恰好来自"持有到开盘"**: `OVERNIGHT_GAP_STOP` 历史 4 次合计 −$1,179.26, 3 次在开盘 5 分钟内成交。
2. **豁免会造出无止损时间缝** (SIM 无券商 STOP, 盘前亏损侧一律 `hold_no_stop`) ⇒ 必须先补 §8.3 的缝。
3. **旧读数的 +60.8pp"被割反转"里含截断污染** (§3) ⇒ 量级不可外推, 只能重新读数。