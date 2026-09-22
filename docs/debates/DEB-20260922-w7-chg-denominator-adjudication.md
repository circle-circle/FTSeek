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

**未定的就是"哪一个"**: 三个面都会产出同样的数字对, 需在**盘前 09:33 ET (21:33 CST) 对 1-2 个标的抓一次原始快照**, 记录 `last_price / prev_close_price / pre_price / update_time` 与路由器最终给出的 `change_percent`, 即可一次判死 (成本: 1 次 snapshot 调用, 只读; 须避开交易环路的 snapshot 预算 ⇒ 只抓 1-2 只)。

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
| **A 先修分母** (维护批建议, **§4 已实证支持**) | 病灶已定量: ctx 的 `change_percent` 拿的是 (前一日收盘, 前前一日收盘) 对; 修点锁定 `futu_lv3_provider` 取数面 (push `:951-962` / 快照 `:986-998` / 路由器 `:432,:723`), 先 §6⑤ 抓包定死是哪一面, 再让当日读数必须来自当日会话。期间 38 键**不 append**, 保持可见 | ① 抓包定死单一面; ② 修后新样本 ctx 值 == 当日涨跌幅 (差 <0.1pp) **连续 3 个交易日**; ③ `_check_chg_denominator` 的 `healed` 列出这 38 键 或 `new=0` | 改动触及取数路径 ⇒ 按 `ADAPTATION-CONTROL-LAW` 走变更 (口径类改动先落档); 须验 fallback 路径 (snapshot 预算/限流) 不被新守卫打回 |
| B append 基线 | 把 38 键写入 `chg_denominator_baseline.json` 的 `divergent` + `refs` 写裁决出处 ⇒ 静默 | 无 (落地即静默) | **会静默一条在跑的日频分叉**; 且 §3 已证 ctx 侧日内不自洽 ⇒ 现在 append 会把可疑读数合法化 |
| C 收紧判据 | 只报 pp>5 或只报当日新增 | 需重新预注册阈值 + 双窗口验证 | 治噪声不治因; 分叉在 2-5pp 段同样可能是错分母 |
| D 维持无行动 | 不动 | — | 每天一条 P1 (+ 盘中签名归并) 噪声, 且消费者读数继续不可信 |

**建议: A**(附保守替代 B2): 若你判定"两路径口径差本身就是设计内", 则走 B, 但**只 append 到 09-14 之前的 2 键**, 09-15 之后 36 键留观 —— 因为 09-15 起的密度 (10/7/3/6/3) 与 09-11 前的稀疏 (90 键攒了 3 周) 不是同一形态。

## 8. 局限 (明说)

- 溯源已从"反推"升级为**等式实证** (3 样本 ≤0.003pp, 且 pmi 侧与交易所日K逐一对齐); 但**"三个取数面里哪一个"仍未定**, 需 §6⑤ 盘前抓包。
- 3 个样本都命中"前一日涨跌幅", 但 38 键里未逐键验证 ⇒ **不宜假设一个修法覆盖全部 38 键** (RUM 同日的 +16.46 读数就说明同日两种读法并存)。
- 诊断面可加固 (等裁决): 不变量 W7 在报分叉时**附带分类** ("等于前一日涨跌幅" / "其他") ⇒ 下次同类问题不必再人工溯源 3 小时。这会改探测器输出文案 (不改阈值/不改键集), 属诊断增强, 仍提请一并裁决。
- 本文只读: 未改任何判据/参数/基线; 未动 38 键在环里的状态。

## 9. 裁决记录

| 日期 | 裁决人 | 决定 | 依据/理由 |
|---|---|---|---|
| (待) | 用户 | | |