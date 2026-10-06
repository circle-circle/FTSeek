# DEB-20260922 · W7 昨收分母分叉: 38 键基线外新增的裁决包

> **状态**: **待裁决** (owner=用户) · 出具日 2026-09-22 · 出具人 维护批/Hermes 元环
> **关联**: 不变量 `W7_chg_denominator_divergence` (`scripts/daily_invariant_check.py`, 2026-09-11 落地) ·
> 先例 `DEB-20260911 C6` (同类立档实锤) · 生产者 `data/invariant_check_latest.json` ·
> 消费者 `docs/CONTROL-LOOP-LEDGER.md` W7 行 + 元环 `invariant:W7_chg_denominator_divergence` (P1, due 2026-10-04)
> **要你回的一句话**: 「先修分母」/「append 基线」/「收紧判据」/「维持无行动」

---

## 1. 事实 (读数为 2026-09-22 实跑, 非转述)

`_check_chg_denominator` 本体实跑 (`status=drift`):

| 项 | 值 |
|---|---|
| 可比配对数 | 278 |
| 不可配对 | 127 (影子档缺行/时间窗外) |
| 超阈分叉总数 | **128** |
| 基线内 (silenced_known) | 90 |
| **基线外新增** | **38** |
| healed | 0 |
| 判据阈值 | \|Δchg\| > 2.0pp, 可比窗 ±10min |

38 键按日: `09-11×2 · 09-14×7 · 09-15×10 · 09-16×7 · 09-17×3 · 09-18×6 · 09-21×3`
⇒ **每个交易日都有, 最新到 09-21 (昨天), healed=0** ⇒ 这不是"某次事故的残留", 是**在跑的日频分叉**。

|Δ| 区间 2.02 ~ 20.08pp。全表 (date·symbol#inst_id / pp):

```
09-11 INTC#2535 8.01 | 09-11 ITUB#2534 2.60 | 09-14 AAOI#2618 9.46 | 09-14 BE#2615 15.47
09-14 CRWD#2666 8.19 | 09-14 DELL#2614 17.96 | 09-14 NOK#2617 14.77 | 09-14 RUM#2682 20.08
09-14 SKHY#2616 8.08 | 09-15 ARM#2697 12.01 | 09-15 ASML#2756 8.15 | 09-15 COIN#2692 15.14
09-15 CRWD#2690 13.91 | 09-15 META#2699 2.58 | 09-15 MSTR#2723 8.95 | 09-15 MU#2698 6.67
09-15 NOK#2741 16.31 | 09-15 NVDA#2693 4.47 | 09-15 ORCL#2702 3.01 | 09-16 COIN#2832 9.79
09-16 MPC#2768 4.08 | 09-16 NOK#2771 3.02 | 09-16 QCOM#2769 3.99 | 09-16 UMC#2772 5.62
09-16 VLO#2770 4.40 | 09-16 YOU#2773 2.36 | 09-17 AMD#2851 2.66 | 09-17 SLB#2855 4.24
09-17 SPCX#2840 3.22 | 09-18 ARM#2919 8.90 | 09-18 COIN#2917 2.06 | 09-18 HPE#2913 7.39
09-18 ORCL#2924 7.40 | 09-18 TSLA#2916 2.02 | 09-18 UMC#2912 9.75 | 09-21 CRM#2997 2.66
09-21 RIVN#3051 7.06 | 09-21 SPCX#2987 2.28
```

## 2. 判据口径 (两条路径比的是什么, 代码原文)

| 路径 | 文件 | 被比对字段 | 定义 |
|---|---|---|---|
| pmi "干净昨收" | `data/premarket_invalidation_shadow.jsonl` | `chg_pct` | 行内自带 `premarket_price` 与 `prev_close`, chg_pct = (premarket_price − prev_close)/prev_close, **可逐字复核** |
| ctx "push 缓存昨收" | `data/entry_timing_shadow.jsonl` | `features.change_percent` | 由 `signal_pipeline.py:738/1166` 写 `ctx.change_pct × 100`; `pipeline_contracts.py:39` 注释 = "日内涨跌幅" |

配对: 按 `inst_id`(+date) 精确, 缺则 `(symbol,date)` 兜底; 只在 ±10min 窗内取 **最小 |Δ|** (分母一致 ⇒ 最接近时刻的两次读数必 ≈0; 分母分叉 ⇒ 全日冻结必全发散)。

## 3. 样例逐字读数 (两路径原始行, 非复算)

| 键 | pmi 行 | ctx 行 | \|Δ\| |
|---|---|---|---|
| `09-14 US.RUM#2682` | ts 09:35:40 premarket_price 8.42 **prev_close 7.17** chg_pct **+17.434** | 同窗 `features.change_percent` **−2.65** (同日另有 +16.46 读数多行) | 20.08pp |
| `09-15 US.NOK#2741` | ts 09:36:00 premarket_price 9.9401 **prev_close 9.65** chg_pct **+3.006** | 同窗 **−13.3** | 16.31pp |
| `09-21 US.RIVN#3051` | ts 09:35:49 premarket_price 15.65 **prev_close 14.99** chg_pct **+4.403** | 同窗 **−2.66** | 7.06pp |

两点当场可见:
1. **符号都翻了** (pmi 正 / ctx 负) ⇒ 不是舍入、不是窗宽、不是日内漂移能解释的;
2. `US.RUM 09-14` 同一标的同一天, ctx 侧**同时存在 −2.65 与 +16.46 两种读数** ⇒ ctx 侧读数本身在日内不自洽。

## 4. 溯源（**已实证: 错在 ctx 侧, 它报的是"前一交易日的涨跌幅"**）

先用交易所日K（Futu `request_history_kline` K_DAY/autype=NONE, 只读）做地面真值:

| 键 | pmi `prev_close` | 交易所前一日收盘 (日K) | 一致? |
|---|---|---|---|
| `09-21 RIVN` | 14.99 | 9/18 收盘 **14.99** | ✓ |
| `09-15 NOK` | 9.65 | 9/14 收盘 **9.65** | ✓ |
| `09-14 RUM` | 7.17 | 9/11 收盘 **7.17** | ✓ |

⇒ **pmi 侧分母与交易所一致, 错的是 ctx 侧。** 再把 ctx 读数与"前一交易日的涨跌幅"比:

| 键 | 前一交易日涨跌幅 (日K算) | ctx 实报 | 差 |
|---|---|---|---|
| `09-21 RIVN` | (14.99−15.40)/15.40 = **−2.66%** | −2.66 | **0.002pp** |
| `09-15 NOK` | (9.65−11.13)/11.13 = **−13.30%** | −13.30 | **0.003pp** |
| `09-14 RUM` | (7.17−7.365)/7.365 = **−2.65%** | −2.65 | **0.002pp** |

三个独立样本全部精确吻合（差 ≤0.003pp）⇒ **ctx 侧的 `change_percent` 是"滞后一日的读法": 它拿到的 (price, prev_close) 是 (前一日收盘, 前前一日收盘) 这一对**，不是当日的。这解释了 §3 的全部现象: 符号翻转 (当日 +4.4% vs 前一日 −2.66%)、同日 ctx 自相矛盾 (−2.66 与 +16.46 并存: 一个是陈旧对、一个是当日对)。

**取数嫌疑面（三选一, 需盘前一次抓包定死）** — `ctx.change_percent` 由 `market_data_router.py:432` / `:723` 直接抄 `futu_quote["change_percent"]`, 该值在 `app/data/futu_lv3_provider.py` 内产生:

| 面 | 位置 | 机制假设 |
|---|---|---|
| push 通道 | `:951-962` + 缓存写入 `:561` (`price=row["last_price"]`, `prev_close=row["prev_close_price"]`) | push 行里的这两个字段本身就滞后一个会话 (盘前 `last_price` 未更新 + `prev_close_price` 已是前收) |
| 快照通道 | `:986-998` (`prev_close=row.get("prev_close_price", last_price)`) | 盘前快照的 `last_price`/`prev_close_price` 成对滞后 |
| 路由器缓存 | `:432/:723` 抄值 | 抄到的是一份跨会话未失效的旧 quote |

### 4.1 ~~已钉死: 快照字段语义面~~ (⚠️ 本节结论已被 §4.4 更正 — 保留以留痕)

读码与先例把"哪一个"定死了, 且**仓库里已有同形修法**:

1. **脏字段**: `futu_lv3_provider` 用快照/推送的 `prev_close_price` 当分母算 `change_percent` —
   push 通道 `:957-961` (`prev_close = float(quote.get("prev_close") or 0)`), 快照通道 `:986-988`
   (`prev_close = row.get("prev_close_price", last_price)`)。盘前窗里 `last_price` 还停在**最近一个已收盘日**的收盘,
   而 `prev_close_price` **概率性地返回"两天前收盘"** ⇒ 比值恰好 = **前一交易日的涨跌幅** (§4 三样本 ≤0.003pp 全中)。
2. **该字段的毛病仓库早就量过**: `market_data_router.py:509-534` 的 RFC-068 审计修复 (2026-08-29) 注释原文 ——
   *"昨收口径: 日K自算真昨收 (承 de13b8d0 教训; **不用快照 prev_close_price — 盘前概率性返回两天前收盘, 62% 实测**)"*。
   即 `prev_close` 这个键早在 8/29 就被换成日K自算真昨收 (B 门由此才不再结构性短路)。
3. **但 RFC-068 只修了 `prev_close` 这一个键, `change_percent` 漏在门外**: router `:432` / `:723` /
   其余非期权主路径仍原样抄 `futu_quote["change_percent"]` (由 §1 的脏分母算出)。
   ⇒ 这不是新 bug, 是**同一次修复的漏项**; 08-29 之后 W7 每天报的分叉, 就是这条漏项在产。

### 4.2 修法 (与 RFC-068 同形, 待裁后落)

在 router 注入真昨收的同一位置, **当 `prev_close` 被日K真昨收替换时, 用真昨收重算 `change_percent`**:

```python
# 紧随 RFC-068 prev_close 注入 (market_data_router.py:533 之后)
_pc_clean = ts_ctx.get("prev_close")            # 日K自算真昨收 (RFC-068 产物)
_px_now = ts_ctx.get("price")
if _pc_clean and _px_now and float(_pc_clean) > 0:
    ts_ctx["change_percent"] = round((float(_px_now) - float(_pc_clean)) / float(_pc_clean) * 100, 3)
    ts_ctx["change_percent_source"] = "router_recomputed_daily_close"
```

- 只在真昨收可得时重算 (日K全不可得 ⇒ 保留原值, 与 RFC-068 的 fail-open 同语义);
- 加 `change_percent_source` 溯源键 ⇒ 日后再出分歧可直接分辨是谁算的 (W7 也可据此区分);
- 影响面即 §5 的消费方; 不改阈值、不改键集、不动交易链路结构。

### 4.4 更正 + 真病灶 (行级, 2026-09-22 用户裁「先修分母」后读码所得)

**§4.1 的钉点是错的, 在此更正。** 我先前把病灶钉在"快照 `prev_close_price` 语义"上, 依据是 RFC-068 注释里
"盘前概率性返回两天前收盘, 62% 实测" — 但那是 `prev_close` **键**的旧病史, 不是 `change_percent` 的生产者。
读码到行级后, 真链路是:

| 步 | 位置 | 行为 |
|---|---|---|
| ① | `app/data/lv3_technical_calculator.py:339-351` `_calc_change_percent` | `prev_close = df["close"].iloc[-2]`, `curr_price = result["price"]` (= `df["close"].iloc[-1]`), `change_percent = (curr_price − prev_close)/prev_close×100` |
| ② | 同一函数 `:68` | `result["price"] = float(df["close"].iloc[-1])` — 分子分母**都来自日K序列相邻两根 bar** |
| ③ | `app/data/market_data_router.py:544` "用快照价覆盖 K 线价" | 只把 `ts_ctx["price"]` 换成实时价, **没有重算 `change_percent`** |

⇒ 当日盘前/盘中, 日K序列的最后一根 bar 是**最近一个已收盘交易日** ⇒ ①算出的比值 **恒等于"前一交易日的涨跌幅"**;
③把价格换成实时价之后, ctx 输出的是 **"实时价 + 昨日涨跌幅"混装**。这就是 §4 三样本 (≤0.003pp) 的成因,
也解释了为什么它**整日不变**、以及 `US.RUM 09-14` 同日为何同时存在 −2.65 (主路径) 与 +16.46 (另一路径/时点) 两种读法。

**修法 (已落地, 用户 2026-09-22 裁「先修分母」)**: `market_data_router.py` Step 8 之后追加一处重算
(与 RFC-068 同一处收口点, 路径共同、fail-open 同语义):

```python
_pc_rf = float(ts_ctx.get("prev_close") or 0)     # RFC-068 日K自算真昨收
_px_rf = float(ts_ctx.get("price") or 0)          # 已被实时价覆盖
if _pc_rf > 0 and _px_rf > 0:
    ts_ctx["change_percent"] = round((_px_rf - _pc_rf) / _pc_rf * 100.0, 3)
    ts_ctx["change_percent_source"] = "router_recomputed_daily_close"
```

守卫 (3 条, 均落在 `tests/test_router_prev_close_injection.py`):
`test_calculator_change_percent_is_prior_session_move` (根因钉住: 计算器口径 = 相邻 bar 涨跌幅, 且**不得**等于当日涨幅)、
`test_change_percent_recomputed_from_live_price_and_true_prev_close` (主守卫: 脏值 −0.93 不得存活, 须出 −5.401 + 溯源键)、
`test_change_percent_untouched_when_prev_close_or_price_missing` (fail-open: 不写溯源键、不改数、不编造昨收)。
**残余 (明说)**: 真昨收不可得的分支 (日K全挂) 仍是旧口径原值 — 属已知 fail-open, 与 RFC-068 同; 该分支由 W7 继续可见。

### 4.3 抓包降级为"确认性测试" (预注册预测 — **其中 P1 已被 §4.4 证伪**)

> ⚠️ **2026-10-06 更正 (见 §10)**: 本节 P1 的 `c1`/`c2` 命名与探针代码的**索引约定错位一天**,
> 且探针从未读取 P1 所需的 `closes[-3]` ⇒ **本节的「回归取证」价值主张不成立** (探针从不读 ctx 侧,
> 测不到「修复上线后盘前是否还有陈旧对」)。产物里 3 次 `stale_pair_hit=true` 经逐行实测
> **全是退化行假命中**。本节原文保留以留痕, 事实更正与证据见 §10。

`8a989cf4501d` (09:33 ET) 仍是必要取证, 但性质已变——**它能被证伪**:
- **预测 P1**: 盘前窗内至少 1 只标的的快照 `prev_close_price` == **前前一日收盘 (c2)** 而非前一日收盘 (c1)
  ⇒ 该标的"陈旧对命中 (stale_pair_hit)"为真, 且 `cp_from_snapshot_fields` == 前一日涨跌幅;
- **预测 P2**: 同一时刻日K `c1` 与 pmi 侧 `prev_close` 一致 (pmi 干净);
- **若两只都不命中 P1**: 快照语义面被证伪 — **这已经发生**: §4.4 读码把病灶钉到"日K序列口径 + 覆盖 price 不重算",
  既不是快照字段面, 也不在 §4.3 列的两个备选里 (预注册的备选清单不完整, 据实记下)。抓包 job 因此**转为回归取证**:
  它的价值变成"修复上线后, 盘前窗内是否还有陈旧对" — 有则说明另有残余面。

**机制层面的既有先例**仍未变: `DEB-20260911 C6` 立档的 "push 缓存昨收跨日不更新" (当时实测 pmi −7.65% vs ctx +4.07%, 7 秒内差 11.72pp, `change_percent` 全日冻结) 与本文同族; 区别是本文给出了 **ctx 值 = 前一日涨跌幅** 的定量身份, 把"缓存脏了"从形容词变成了等式。

## 5. 影响面 (谁在读这个字段)

`grep -rn 'change_percent' app/`:

- `app/core/intraday_scorer.py:494` / `:610` 盘中评分读 `ctx.get("change_percent")`; `:546` 注释明写"slope_5m 不可用时**退回 change_percent (日累计)**" ⇒ 正好在 fallback 路径上吃这个值;
- `app/playbook/pipeline.py:5830` `_chg_now = ctx.get("change_percent")` (管道判据面);
- `app/core/pipeline_contracts.py:136` → `ctx.change_pct`;
- `app/pipeline/stages/market_data_stage.py:168` "单日暴涨 +{change_pct}%" 会进短线原因列表;
- `data/entry_timing_shadow.jsonl` 本身是闸门证据档 (D2' 入场确认门影子的输入)。

⇒ 不是纯记账口径问题: **该日的 ctx 涨跌幅读数及其消费者不可信** (W7 自带结论语)。

## 6. 复现命令 (逐字可跑, 全程只读)

```bash
cd /opt/ftseek/FTSeek-US
# ① 判据本体实跑 (只调 _check_chg_denominator, 不进 main(): 避免写产物/告警)
PYTHONPATH=. venv/bin/python - <<'EOF'
import importlib.util
spec = importlib.util.spec_from_file_location("dic", "scripts/daily_invariant_check.py")
dic = importlib.util.module_from_spec(spec); spec.loader.exec_module(dic)
calls = []
info = dic._check_chg_denominator(lambda *a: calls.append(a))
print({k: v for k, v in info.items() if k != "divergent"})
for a in calls: print(a[2], "|", a[3][:110])
EOF
# ② 地面真值: 交易所日K (只读; 直接判 pmi 的 prev_close 对不对)
venv/bin/python - <<'EOF'
from futu import OpenQuoteContext, KLType, AuType, RET_OK
q = OpenQuoteContext(host="127.0.0.1", port=11111)
try:
    for code in ("US.RIVN", "US.NOK", "US.RUM"):
        ret, d, _ = q.request_history_kline(code=code, ktype=KLType.K_DAY, autype=AuType.NONE,
                                           start="2026-09-04", end="2026-09-22", max_count=100)
        print(code, [(r["time_key"][:10], r["close"]) for r in d.to_dict("records")][-6:] if ret == RET_OK else d)
finally:
    q.close()
EOF
# ③ ctx 隐含"它用的昨收" = pm 价/(1+cp/100); 与日K比一眼见真章
# ④ 若要走 append 基线: 先落档再改 data/chg_denominator_baseline.json 的 divergent + refs (改基线=改判据)
# ⑤ 盘前抓包 (定死三选一) — **已落地为限时 job `8a989cf4501d`** (`33 21 * * 1-5` = 09:33 ET 盘前/开盘窗)
python3 /root/.hermes/scripts/w7_denominator_probe.py   # 壳体 → scripts/w7_denominator_probe.py (只读)
#   抓 RIVN/NOK 的 last_price/prev_close_price/pre_price/update_time + 日K对表; 诊断档 data/audit/w7_denominator_probe.jsonl
#   首跑 (2026-09-22 11:28 CST, 盘后): 2 只均"未命中" ⇒ 快照字段本身与交易所一致 (盘后窗口无法区分, 需盘前那次)
#   ⚠️ 冬令时 (11 月起) 同一 crontab 时刻右移 1h ⇒ 变成 08:33 EST, 仍在盘前窗, 但列入 11 月重评
```

## 7. 四个选项与预注册判据

| 选项 | 做什么 | 预注册判据 (怎么算达成) | 风险 |
|---|---|---|---|
| **A 先修分母 — ✅ 用户 2026-09-22 裁定并已落地** (见 §4.4) | 病灶已定量: ctx 的 `change_percent` 拿的是 (前一日收盘, 前前一日收盘) 对; 修点锁定 `futu_lv3_provider` 取数面 (push `:951-962` / 快照 `:986-998` / 路由器 `:432,:723`), 先 §6⑤ 抓包定死是哪一面, 再让当日读数必须来自当日会话。期间 38 键**不 append**, 保持可见 | ① 抓包定死单一面; ② 修后新样本 ctx 值 == 当日涨跌幅 (差 <0.1pp) **连续 3 个交易日**; ③ `_check_chg_denominator` 的 `healed` 列出这 38 键 或 `new=0` | 改动触及取数路径 ⇒ 按 `ADAPTATION-CONTROL-LAW` 走变更 (口径类改动先落档); 须验 fallback 路径 (snapshot 预算/限流) 不被新守卫打回 |
| B append 基线 | 把 38 键写入 `chg_denominator_baseline.json` 的 `divergent` + `refs` 写裁决出处 ⇒ 静默 | 无 (落地即静默) | **会静默一条在跑的日频分叉**; 且 §3 已证 ctx 侧日内不自洽 ⇒ 现在 append 会把可疑读数合法化 |
| C 收紧判据 | 只报 pp>5 或只报当日新增 | 需重新预注册阈值 + 双窗口验证 | 治噪声不治因; 分叉在 2-5pp 段同样可能是错分母 |
| D 维持无行动 | 不动 | — | 每天一条 P1 (+ 盘中签名归并) 噪声, 且消费者读数继续不可信 |

**建议: A**(附保守替代 B2): 若你判定"两路径口径差本身就是设计内", 则走 B, 但**只 append 到 09-14 之前的 2 键**, 09-15 之后 36 键留观 —— 因为 09-15 起的密度 (10/7/3/6/3) 与 09-11 前的稀疏 (90 键攒了 3 周) 不是同一形态。

## 8. 局限 (明说)

- 溯源已从"反推"升级为**等式实证** (3 样本 ≤0.003pp, 且 pmi 侧与交易所日K逐一对齐); 取数面已按
  §4.1 **钉死到快照字段语义** (代码路径 + RFC-068 注释的 62% 实测双重确认), 并由 §4.3 给出可证伪的盘前预测。
- 3 个样本都命中"前一日涨跌幅", 但 38 键里未逐键验证 ⇒ **不宜假设一个修法覆盖全部 38 键** (RUM 同日的 +16.46 读数就说明同日两种读法并存)。
- 诊断面可加固 (等裁决): 不变量 W7 在报分叉时**附带分类** ("等于前一日涨跌幅" / "其他") ⇒ 下次同类问题不必再人工溯源 3 小时。这会改探测器输出文案 (不改阈值/不改键集), 属诊断增强, 仍提请一并裁决。
- 本文只读: 未改任何判据/参数/基线; 未动 38 键在环里的状态。

## 9. 裁决记录

| 日期 | 裁决人 | 决定 | 依据/理由 |
|---|---|---|---|
| 2026-09-22 | 用户 | **「先修分母」** (选项 A) | 判决书 §1-§4.4 实证: 38 键在跑的日频分叉 + ctx 值 = 前一交易日涨跌幅 (≤0.003pp) + 影响面含生产 fallback 路径; append 基线会静默在跑的错读数 |

**执行回执 (2026-09-22)**: 修法已落 `app/data/market_data_router.py` (Step 8 后重算 + 溯源键);
3 条守卫落 `tests/test_router_prev_close_injection.py` (7 passed); 相关面回归 281 passed (另 5 个 `test_trade_recorder` 失败
为**既有夹具 schema 漂移**: 夹具库缺 `trade_records.config_version` 列, 真库有 — 与本改动无关, 未处置, 已记录);
服务 `ftseek-us.service` 重启后生效。**达成判据** (§7 A): 新样本 ctx 值 == 当日涨跌幅 (差 <0.1pp) 连续 3 个交易日, 且 W7 `new=0`
或 `healed` 列出这 38 键 — 观察窗自 2026-09-23 起算。

---

## 10. 抓包探针复核与更正 (2026-10-06, 推送治理批连带)

> 缘起: 本 agent 在「推送治理」批把 job `8a989cf4501d` 的 `deliver` 从 `origin`
> (一直报 `delivery_failed`, 10 次执行零送达) 改成显式飞书投递 —— 理由是「探针产物
> 30 行正常增长 ⇒ 采集活着, 断的只是投递」。**该理由只证到「进程在跑」, 没证「测得对」**。
> 本节是独立核验后的更正; 判据本身 (§2/§4/§7) 一字未改, 改的是**探针实现与其价值主张**。

### 10.1 §4.3 的「回归取证」价值主张不成立 (实测, 非读码推断)

探针 `scripts/w7_denominator_probe.py` 全文只调两个行情接口:
`get_market_snapshot` + `request_history_kline`。grep `ctx|change_percent|LV3|router|calculator|_calc_`
⇒ **0 处代码命中** (只命中 docstring 背景描述)。

而 §4.3 声称它的价值变成「修复上线后, 盘前窗内是否还有陈旧对」。ctx 侧的
`change_percent` 才是 §4.4 钉出的病灶面 —— 探针从不读它 ⇒ **测不到自己文档声称的东西**。

**真正在做这件事的是另一道, 而且它活着**:

| 面 | 实测 (2026-10-06) |
|---|---|
| `scripts/daily_invariant_check.py:784 _check_chg_denominator` | **真读 ctx 侧** `features.change_percent` (L805) 并与 pmi 路径对比, 阈值 2.0pp / 窗 10min |
| 载体 | crontab `5 5 * * 2-6`; `logs/invariant_check.log` age=11.7h ⇒ 活 |
| 近期真出数 | 10-03 报 `W7_chg_denominator_divergence×38` (存量已登记 `chg_denominator_baseline.json`) |
| 输入源 | `premarket_invalidation_shadow.jsonl` 511 行 age=19.2h ✓ / `entry_timing_shadow.jsonl` **8034 行** age=17.2h ✓ |

⇒ 探针退役**不会造成可见性丢失**; §4.3 说的「由 W7 继续可见」这件事, 一直是不变量 W7 在做, 不是这个抓包 job。

### 10.2 索引约定错位一天 (探针自 09-22 上线以来的实现缺陷)

§4.3 P1 原文用 `c1`/`c2` 表示「前一日 / 前前一日收盘」; 而探针代码
`c1 = closes[-1]`、`c2 = closes[-2]`, 且**盘前窗 `request_history_kline` 已含当日 bar**。
用产物 30 行的实测分布定语义 (不靠变量名猜):

| DEB 语义 | 代码变量 | 实测证据 (n=30) |
|---|---|---|
| 当日 bar close | `c1 = closes[-1]` | `\|c1 − last_price\| < 0.01` : **28/30** |
| 前一日收盘 | `c2 = closes[-2]` | `\|c2 − prev_close_price\| < 0.01` : **30/30** |
| **前前一日收盘** | `closes[-3]` | ★**从未被读取, 也从未落盘** |

⇒ P1 判据需要的 `closes[-3]` 不在探针里 (数组本身含 08-25 至今约 30 根 bar, 完全可得);
而旧判据 `prev==c1 ∧ last==c2` 实际比的是「prev==当日close ∧ last==前一日close」= **错位一天**。

⚠️ **同族自查 (本 agent 的假阳, 留痕防后人重犯)**: 据此一度算出「按 P1 定义命中 27 行、
旧判据漏检」并报成缺陷 —— 错在把 `prev == c2` 这个**常态**当异常 (实测 30/30 恒成立:
前一日收盘本来就该等于 `prev_close_price`)。下「仪器漏检」结论前必须先问
「这个形态是不是本来就该恒成立」。

### 10.3 产物里 3 次 `stale_pair_hit=true` 全是假命中 (逐行实测)

| 日期 | 标的 | last | prev | c1 | c2 | \|c1−c2\| | 性质 |
|---|---|---|---|---|---|---|---|
| 09-25 | US.NOK | 10.425 | 10.430 | 10.425 | 10.430 | **0.005** | 退化 |
| 09-29 | US.RIVN | 14.790 | 14.790 | 14.790 | 14.790 | **0.000** | 退化 |
| 09-30 | US.NOK | 10.365 | 10.360 | 10.360 | 10.360 | **0.000** | 退化 |

`c1 ≈ c2` (横盘日) 时, 旧判据的两个条件同时退化为 `prev == last` ⇒ **无意义比较**。
这就是它 30 行里只「命中」3 次、且全部落在横盘日的原因。

**Codex 2026-10-06 的补丁** (工作区 WIP) 在 `_stale_pair_hit` 里加了
`if abs(c1-c2) < 0.01: return False` ⇒ 30 行回放命中 3 → **0**, 3 次全翻转。
经红绿验证 (删掉该行 ⇒ 精准只有 `test_equal_adjacent_closes_do_not_report_stale_pair` 转红)
⇒ **抑制方向正确**。但它是抑制症状, 没修 §10.2 的错位本身: 补上退化排除后,
该判据变成恒 False (30/30 行都因 `|c2−prev|<0.01` 常态而无法满足 `|prev−c1|<0.01` 的非退化组合)
⇒ 仪器仍然测不到 P1。

### 10.4 已执行 / 未执行

**已执行 (本批)**:
- `hermes cron edit 8a989cf4501d --deliver local` —— **撤回本 agent 上一批的投递修复**。
  采集照跑、产物照长, 但不再外发。回读确认 `deliver='local'` 且 job 总数 35 未变。
  若不撤回: 今晚 21:33 CST 起 (`33 21 * * 1-5`), 一个测不到东西、判据错位、历史命中全假的
  仪器会**每天推 1 条**给用户 —— 正是「推送治理」要消的东西, 而上一批亲手接通了它。
- 产物读者面已按退役前置纪律查全零: crontab **0 命中** / 仓内 `app·scripts·ops·tests` 代码
  **0 命中** / 仅 docs (本文 L161 + RFC-091 L353 + CONTROL-LOOP-LEDGER L171) + 探针自身 + shim。

**未执行 (待裁, 见 §10.5)**: 删 job + 删两处 shim (真退役)。

### 10.5 待裁: 探针存续 (RFC-091 与本文给了相反指令)

- `docs/decisions/RFC-091-loop-engineering-meta-audit-loop.md:353` 逐字写
  「**存在期限 = W7 裁决落地即删**」, 而修复已 2026-09-22 落地 (`da10092f6`) ⇒ 按此条**该删**;
  该行还写「回滚: 删本 job + 删两处 shim」。
- 本文 §4.3 又把它「**转为回归取证**」⇒ 按此条**该留**。
- ⇒ 两条文档相反指令, job 至今 `enabled=True`。**建议退役**, 依据 = §10.1 (它测不到声称要测的,
  而真正在测的不变量 W7 活着且出数) + §10.3 (历史命中全假) + 产物零代码消费者。
- **为何本轮暂缓**: Codex 于 15:14 改了探针真身 + 新增 `tests/test_w7_denominator_probe.py`,
  至今**未提交** (工作区 `M` / `??`)。现在删 job 与 shim 会让它的改动失去载体,
  且其测试会因真身被删而变 ERROR ⇒ 须等那批落地或明确放弃后再退役。
  紧迫性已由 `deliver=local` 解除 (不外发), 不存在「今天不删就多推一条」的压力。
- 若裁**保留**: 必须先补 `closes[-3]` 落盘 + 按 §4.3 P1 字面重算判据, 并给产物加
  `judge_version` 字段 —— 当前 30 行历史是 v1 判据算的, 新行是 v2, 同一份 jsonl
  混存两套口径且无版本标记, 任何按 `stale_pair_hit` 聚合的读数都会把已判假的 3 次计入。
  (⚠️ 补索引 = 改判据实现, 属重裁面, 不得由执行方单边决定。)

**本节只读 + 只改投递目标**: 未改任何判据/阈值/基线, 未动 `chg_denominator_baseline.json`,
未改探针逻辑 (逻辑改动属 Codex WIP 与 §10.5 待裁项)。
