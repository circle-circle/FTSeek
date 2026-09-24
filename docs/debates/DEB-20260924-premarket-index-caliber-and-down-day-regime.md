# DEB-20260924 · 盘前大盘环境口径缺陷 + 「普跌日」regime 裁决路径

> **性质**: 溯源取证 + 提案（**未改一行代码**）。含 ① 一处输入口径缺陷的修复提案（P0）
> ② 「普跌日」regime 是否值得成为信号的**裁决路径**提案（P1，默认先回放、不先攒样）。
> **状态**: 提案待裁决（F1–F7）。
> **立案人**: Hermes（应用户令「把这份溯源落成 DEB，并按规矩走独立复查」）。
> **目标读者**: ① **裁决者**（用户）——只需读 §一/§四/§八；② **执行者**——§六；
> ③ **复查者（第二 agent）**——§三 证据表逐格可跑、§七 点名挑战项。
> **触发**: 2026-09-24（ET 盘前）用户质疑「今天科技普跌而系统仍批量产出低吸 BUY，是正常获取和推理吗」。
> **红线**: 本提案不含任何阈值/门限/判据改动；P0 是**输入保真**，P1 是**只读回放**，
> 一律不动 admission / 毕业门 / 熔断 / 执行开关。

---

## 零、一句话结论

用户对事实的判断成立（今日科技普跌、系统仍以低吸 BUY 为主），但根因**不是「数据抓不到」**，
而是三条各自独立的东西叠在一起：**① DA 的「大盘环境」在盘前恒等于昨收且无任何口径标注
（同族第三处漏修）② 唯一方向硬门（飞刀门）阈值 -5%，温和普跌日根本不触发 ③ DA 的角色定位
本就是「优质多头选股器」（这是设计，不是缺陷）**。
上一轮口头溯源里我另有两条说法经本轮逐条回读源码后**站不住**，已在 §一就地更正。

**期望管理（先说清）**: P0 修复**不会多出单、不会直接赚钱**，它买到的是「让模型看见今天在跌」
这一条语义正确性；P1 的**先验期望是「无判别力」**——同族先例（E2 momentum 门 9/13 被裁 KILLED）
明确记录「现役过滤栈已把烂入场拦得差不多，第二道入场门边际价值快速下降」。凡 P1 读出没判别力，
正确动作是**关闭该轴**，不是继续攒样。

---

## 一、上一轮口头结论的更正（先行自曝，防止下游继续引用）

| # | 上一轮说法 | 回读实测 | 处置 |
|---|---|---|---|
| 1 | 「prompt 第 1081 行明文要求**回踩低吸优先**」 | `llm_synth.py:1081` 原文是 **archetype 分类回显规则**：`PULLBACK_LIMIT=等回踩低吸(价格回落到确认位才买… entry_strategy含"低吸/回踩/承接/企稳接"时必选此项)`。它**在 LLM 自己写完 entry_strategy 之后**把文本映射成枚举，对「先写低吸叙事」没有偏好权重；仓内亦无「等回踩」类正向指令。 | **部分证伪**。原文见 E8。该条**不再作为**「BUY 为主」的成因；成因改按 §二 的 ①②③ 归因。 |
| 2 | 「板块轮动补『当日板块涨跌』：现在 weakest 无科技但 SOXX 最弱，**核其实时性**」 | 板块上下文**已按 9/14 批修复**：prompt 明标「**上一交易日**板块涨跌 / 上一交易日(2026-09-23)盘中板块脉搏」，且带新鲜度守卫（非上一交易日即丢弃+报警，DEB-20260914 P1-1）。 | **撤回「时点 bug」定性**。它不含今日盘前强弱是**设计边界**（06:22 ET 无板块层盘前价）；若要今日板块信息，属**新增输入**（§八 F3），不是修 bug。 |
| 3 | 引用 `llm_synth.py:232` / `signal_gate.py:119-132,418-430` 未给目录 | 这两个文件**不在仓内 `app/`**，在 DA skill 目录 `~/.hermes/skills/deep-stock-analysis/scripts/`。且 `llm_synth.py:232` 是**飞刀几何一致性的提示词注记**，真正的 -5% 硬门在**仓内** `app/playbook/pipeline.py:4009-4045`（RFC-068）。 | **补正路径与指针**。行号本身复核无误（E8/E10/E9）。 |

> 纪律依据：`review-system-design` 技能「被证伪声称须同步清除」+「撤回与声称同权，必须过同一口径复核」。

---

## 二、问题陈述（含普查实测）

### 2.1 三条独立成因（按路程排序）

- **① 输入侧（真缺陷，P0）**：DA 的 `collect_market_context()` 对 SPY/QQQ 取**日K序列最后一根的收盘价**
  当「当前」，而 DA 跑在盘前（今天 20:00 CST = 08:00 ET）⇒ 该值**恒等于昨收**，渲染出来的行是
  `- SPY: $767.81 (MA20=763.77, 趋势=UP, 5日+2.1%)` —— **没有任何口径标注**，
  而**同一段 prompt 里个股的「当前价格」却标了**「⚠️盘前时段此值为昨收」（E3）。
  同族第三处：DEB-20260914 修掉了**同一个函数**里的 VIX 子项（P0-2）与板块脉搏新鲜度（P1-1），
  **SPY/QQQ 这一支漏了**。
- **② 门禁侧（现状事实，非缺陷）**：唯一对方向生效的硬门是飞刀门（LONG 且实时价 vs 昨收 < -5% 拒入场，
  RFC-068）；动量闸只在「**空头排列** + BUY」时 SKIP，多头排列 BUY 放行。今日跌幅 -0.5%~-4.5%，
  既不触飞刀也不破位 ⇒ **无闸触发**（E9/E10/E11）。
- **③ 角色侧（设计）**：DA 定位即「优质多头选股器」，SHORT 机会**明确交给 UE 不设限浮现**
  （`signal_gate.py:425-427`，2026-07-22 用户 B 裁决原文）。故「BUY 为主」有一部分是**设计使然**；
  §四 明确**不把这条当缺陷去修**。

### 2.2 覆盖审计为什么看不见 ①（掩盖机制）

`llm_synth._classify_data_sources` 对 `market_context` 的校验是**子项存在性**（spy/qqq/vix 无 `_error` 即 ok），
**不校验值的时点**（E6）⇒ 一个「新鲜但口径是昨天」的值，在 19 维覆盖审计里**记满分**。
与 DEB-20260914 P0-2 的掩盖机制**同型**（那次是「存在性 ok ⇒ 缺失不可见」，这次是「存在性 ok ⇒ 陈旧不可见」）。

### 2.3 普查：既有实现与其消费者（命令 + 原始输出见 §三 E-table）

| 普查项 | 命令 | 结果 |
|---|---|---|
| 仓内是否有 SPY/QQQ 盘前价可用的现成来源 | 快照 `pre_price`/`pre_change_rate`（`data_collector.collect_snapshot`） | **有**，实测 SPY `pre_price=763.96 / -0.50%`、QQQ `735.04 / -0.83%`（E4） |
| 同族缺口还有谁（大盘价被当实时用） | `app/playbook/market_regime.py:108` `SPY > MA20: +2` 的 `spy_price` 来自 `market_data_router.get_spy_context()` → `futu.get_quote("US.SPY")["price"]` | **同型残留**：盘前该 price 亦为昨收（对照 `llm_synth.py:375-380` 的既有断言）；今日实际影响=0（昨收与实时价同侧 MA20），列为**待复核兄弟项**（F5），本提案**不改** |
| 是否已有「市场级 regime」影子轴 | `app/playbook/shadow_registry.py` 47 个 id 全量打印 | **零命中**（无 market-level regime 轴；最近的 `premarket_invalidation` 是**逐股**盘前异动轴，`gap_*` 亦然）⇒ 本提案**不产生并列第二套台账** |
| 已有 regime/宏观门是否在接线 | `grep -rn macro_calendar app/ scripts/` | 采集脚本已因「零消费方」停采（`scripts/ue_macro_brief_collect.py:5`）；DEB-20260816 记录的「regime 层对入场决策影响力=0」**至今成立** |
| 回放可得性（决定「回放 vs 攒样」） | `get_kline.py --ktype 5m --session ALL` 拉历史 | **可得**：单日 288 根 5m bar **含盘前**（`2026-09-23 08:00:00` 有值）⇒ 「今日盘前指数涨跌」可**逐日重建**（E12） |

**P0.5 既有裁决查重**：`docs/debates` 同主题既有 4 份 + 1 份 RFC，关系表见 §九；编号 `DEB-20260924`
当日已占用一份（jev 议题），本文件以**主题后缀**区分，`git log` 复核无同名号冲突。

---

## 三、数据与实证表（每行作者实跑，命令整段可复制）

工作目录统一 `/opt/ftseek/FTSeek-US`，解释器 `./venv/bin/python3.11`。

| 编号 | 声称 | 复现命令 | 期望输出（作者实跑值） |
|---|---|---|---|
| E1 | 今日指令方向分布 + 环境字段 | `python3 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select direction,count(*) from playbook_instructions where date='2026-09-24' group by 1\").fetchall());print(c.execute(\"select spy_trend_at_generation,vix_at_generation,count(*) from playbook_instructions where date='2026-09-24' group by 1,2\").fetchall())"` | `[('BUY', 7), ('SELL_SHORT', 3), ('SKIP', 63)]` / `[('above_ma20', 14.27, 73)]` —— **73 条全带同一个结构性标签，无法表达「今天在跌」** |
| E2 | DA 提示词里「大盘环境」三行原文 | `python3 -c "import json;d=json.load(open('data/da_checkpoint/2026-09-24/US.AMD.raw.json'));L=d['user_prompt'].split(chr(10));[print(i,'|',L[i-1]) for i in (272,273,274)]"` | `272 ## 大盘环境` / `273 - SPY: $767.81 (MA20=763.77, 趋势=UP, 5日+2.1%, 20日+0.5%)` / `274 - QQQ: $741.21 (MA20=717.59, 趋势=UP, 5日+5.3%)` |
| E3 | 同 prompt 里**个股**价格有昨收标注、**大盘**没有 | 同上，取 `140/157/158/159/163` 行 | `140 - 当前价格: $614.61 (⚠️盘前时段此值为昨收, 有效现价见盘前段)` … `158 - 盘前现价(有效价格): $598.2` / `159 - 盘前涨跌幅(vs昨收): -2.67%` —— **对称性缺口：个股标了、大盘没标** |
| E4 | 767.81/741.21 就是 9-23 收盘（盘前口径） | `./venv/bin/python3.11 -c "import sys;sys.path.insert(0,'/root/.hermes/skills/deep-stock-analysis/scripts');import data_collector as d;print(d.collect_snapshot('US.SPY').get('prev_close'),d.collect_snapshot('US.SPY').get('pre_price'),d.collect_snapshot('US.SPY').get('pre_change_rate'));print(d.collect_snapshot('US.QQQ').get('prev_close'),d.collect_snapshot('US.QQQ').get('pre_price'),d.collect_snapshot('US.QQQ').get('pre_change_rate'))"` | `767.81 763.96 -0.501` / `741.21 735.04 -0.832` —— **prompt 里的 current == prev_close；今日真实盘前是 -0.50% / -0.83%** |
| E5 | 同一函数盘中返回的是今日 bar（证明缺陷只发生在盘前） | `./venv/bin/python3.11 -c "import sys,json;sys.path.insert(0,'/root/.hermes/skills/deep-stock-analysis/scripts');import data_collector as d;print(json.dumps(d.collect_market_context(),ensure_ascii=False))"` | ET 10:18 实跑：`spy.current=765.87` / `qqq.current=736.48`（**已随今日盘中 bar 变化，且每次都不同**）⇒ 盘前口径问题存在且**只**在盘前，而 DA 恰好只跑盘前。**注意：本行是时变读数，判据是「它 ≠ prev_close（767.81 / 741.21）」，不是某个具体数字。** |
| E6 | 覆盖审计只查存在性、不查时点 | `grep -n "market_context" -A 8 /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py \| sed -n '/_sub_miss/,+3p'` | `_sub_miss = [k for k in ("spy","qqq","vix") if not isinstance(val.get(k),dict) or "_error" in val.get(k,{})]` —— 只看有无 `_error` |
| E7 | 板块上下文已带「上一交易日」标签（非缺陷） | 同 E2 命令，取 `321/322/323` 行 | `322 上一交易日板块涨跌(弱→强): Communication(-0.85%), … Energy(+0.96%)` / `323 上一交易日(2026-09-23)盘中板块脉搏(15:30 ET, 弱→强): … Semis(-1.3%) …` |
| E8 | 1081 行是 archetype 分类，不是低吸偏好 | `grep -n "PULLBACK_LIMIT=" /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` | `1081:  PULLBACK_LIMIT=等回踩低吸(价格**回落**到确认位才买, 禁止追高; entry_strategy含"低吸/回踩/承接/企稳接"时必选此项);` |
| E9 | 飞刀门 = RFC-068，阈值 -5%，执行时刻口径 | `sed -n '4009,4020p' app/playbook/pipeline.py` | `── RFC-068 … B门硬门 — 接飞刀拒入场 ──` / `语义: 执行时刻实时价 vs 昨收 < -5% → 拒入场` |
| E10 | 动量闸只拦「空头排列+BUY」 | `sed -n '417,431p' /root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py` | `if scanner_direction == "bearish" and direction == "BUY": tier = "SKIP"` —— 多头排列 BUY **放行** |
| E11 | 今日执行漏斗（谁被执行、谁被追高闸拦） | `grep -h '"date": "2026-09-24"' data/shadow/exec_gate_audit.jsonl \| python3 -c "import sys,json,collections;C=collections.Counter();[C.update([(json.loads(l)['symbol'],json.loads(l)['action'])]) for l in sys.stdin];print(C)"` | `Counter({('US.META','skipped_chase_cap'):10, ('US.AMD','skipped_chase_cap'):8, ('US.FTNT','skipped_chase_cap'):3, ('US.ORCL','executed'):1, ('US.SNDK','executed'):1, ('US.FTNT','executed'):1, ('US.ARM','executed'):1])`（计数随盘中轮询继续增长） |
| E12 | 历史盘前指数 5m bar 可得（回放前提） | 见 §三.1 代码块 `E12` | `n= 288` / `首根 2026-09-23 00:00:00 747.14` / `08:00 段 [('08:00',745.79),('09:25',746.09),('09:30',747.1)]` ⇒ **盘前段存在** |
| E13 | 「普跌日」事件基率（21 个交易日重建） | 见 §三.1 代码块 `E13` | `交易日 21`；双腿 `QQQ<=-0.5% 且 SOXX<=-1.5%` = **2/21（9.5%: 09-01, 09-24）**；`QQQ<=-0.8% 且 SOXX<=-1.5%` = 2/21；`QQQ<=-0.5%` 单腿 = 3/21（+09-10）；`QQQ<=-0.2%` 单腿 = **10/21（48%）** |
| E14 | LONG 指令量级（回放样本盘） | `python3 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');r=c.execute(\"select count(*) from playbook_instructions where direction='BUY' and date>='2026-08-18'\").fetchone();print(r);print(c.execute(\"select date,count(*) from playbook_instructions where direction='BUY' and date>='2026-09-14' group by 1\").fetchall())"` | `(327,)`；近端 `[('2026-09-14',6),('2026-09-15',11),('2026-09-16',15),('2026-09-17',12),('2026-09-18',5),('2026-09-21',8),('2026-09-22',4),('2026-09-23',11),('2026-09-24',7)]` |
| E15 | 今日市场真值（ET 10:18 实跑） | `./venv/bin/python3.11 -c "import sys;sys.path.insert(0,'/root/.hermes/skills/deep-stock-analysis/scripts');import data_collector as d;[print(r['code'],r['prev_close_price'],r['pre_price'],r['pre_change_rate'],r['last_price']) for r in d.collect_snapshot_batch(['US.QQQ','US.SOXX','US.SMH','US.NVDA','US.ORCL','US.AMD','US.META','US.FTNT'])]"` | `US.QQQ 741.21 735.04 -0.832 736.65` / `US.SOXX 565.72 555.17 -1.864 557.04` / `US.SMH -1.714` / `US.NVDA -1.494` / `US.ORCL 144.56 136.81 -5.361 135.2` / `US.AMD -2.294` / `US.META +0.253` / `US.FTNT -0.156` —— **半导体最弱、科技普跌**，用户判断成立（`last_price` 列时变） |
| E16 | ORCL 执行时刻未触飞刀门（门按设计工作） | `grep -h '"US.ORCL"' data/shadow/exec_gate_audit.jsonl \| tail -1` | `{"ts":"2026-09-24T09:38:27-04:00", … "action":"executed", "entry":138.48, "size":42, "notional":5816.16, …}` ⇒ `138.48/144.56-1 = -4.21% > -5%` **放行**；其后跌至 -6.64%（E15/E16 last -5.36%→-6.6%）——**门是时点门，不是止损** |

### 三.1 复现代码块（整段可复制；E12/E13 专用）

```bash
# E12 — 历史盘前 5m bar 可得性
cd /opt/ftseek/FTSeek-US && ./venv/bin/python3.11 -c "
import json,subprocess
r=subprocess.run(['/opt/ftseek/FTSeek-US/venv/bin/python3.11','/root/.hermes/skills/futuapi/scripts/quote/get_kline.py','US.QQQ','--ktype','5m','--start','2026-09-23','--end','2026-09-23','--session','ALL','--num','1000','--json'],capture_output=True,text=True,timeout=180).stdout
f,l=r.find('{'),r.rfind('}');d=json.loads(r[f:l+1])['data']
print('n=',len(d));print('首根',d[0]['time'],d[0]['close'])
print('08:00 段',[(b['time'][11:16],b['close']) for b in d if b['time'][11:16] in ('08:00','09:25','09:30')])
"
```

```bash
# E13 — 逐日重建 08:00 ET 指数盘前涨跌 + 事件基率
cd /opt/ftseek/FTSeek-US && ./venv/bin/python3.11 -c "
import json,subprocess,collections
def kl(s):
 r=subprocess.run(['/opt/ftseek/FTSeek-US/venv/bin/python3.11','/root/.hermes/skills/futuapi/scripts/quote/get_kline.py',s,'--ktype','5m','--start','2026-08-18','--end','2026-09-24','--session','ALL','--num','1000','--json'],capture_output=True,text=True,timeout=180).stdout
 f,l=r.find('{'),r.rfind('}');return json.loads(r[f:l+1]).get('data') or []
def pre(s):
 b=collections.defaultdict(list)
 for x in kl(s):b[str(x['time'])[:10]].append((str(x['time'])[11:16],x['close']))
 d=sorted(b);o={}
 for i in range(1,len(d)):
  pc=[c for hm,c in b[d[i-1]] if hm=='15:55'];p8=[c for hm,c in b[d[i]] if hm=='08:00']
  if pc and p8:o[d[i]]=round((p8[0]-pc[0])/pc[0]*100,2)
 return o
q,sx=pre('US.QQQ'),pre('US.SOXX')
rows=[(d,q[d],sx[d]) for d in sorted(q) if d in sx]
print('交易日',len(rows))
print('双腿 QQQ<=-0.5 且 SOXX<=-1.5:',[d for d,a,b in rows if a<=-0.5 and b<=-1.5])
print('QQQ<=-0.8 且 SOXX<=-1.5:',[d for d,a,b in rows if a<=-0.8 and b<=-1.5])
print('QQQ<=-0.5 单腿:',[d for d,a,b in rows if a<=-0.5])
print('QQQ<=-0.2 单腿:',len([d for d,a,b in rows if a<=-0.2]),'/',len(rows))
"
```

> **时变读数纪律**: E5/E11/E15 的值随盘中变化；引用它们时**只引判据**（是否等于 `prev_close` / 是否触门），
> 不引具体数字当作稳定事实。E-table 全部实跑于 **2026-09-24 ET 08:20–10:25**。

---

## 四、方案设计

### 4.1 P0 — DA 大盘环境口径修复（建议先做；纯输入保真）

**目标（可判定）**: 盘前运行 DA 时，提示词里**不得出现无口径标注的昨收值冒充实时值**；
「大盘在跌/在涨」必须与当日盘前真值一致。

- **方案 B（推荐）**: 保留日线口径的 MA/趋势，同时①标注基准日 ②附当日盘前快照涨跌（与个股同源同方法）。
  渲染示例：
  ```
  - SPY: 昨收 $767.81 (2026-09-23) | 盘前 $763.96 (-0.50% vs 昨收) | MA20=763.77 趋势=UP [日线口径]
  - QQQ: 昨收 $741.21 (2026-09-23) | 盘前 $735.04 (-0.83% vs 昨收) | MA20=717.59 趋势=UP [日线口径]
  ```
- **方案 A（保守替代）**: 只加「截至 YYYY-MM-DD 收盘」标注，不引新数据源。零新增依赖，
  但模型仍拿不到今日大盘信息 ⇒ 只治「误读为实时」的一半。
- **明确不做（方案 C）**: 用盘前价替换 `current`。会让 `above_ma20/trend` 变成「今日价 vs 昨收基准 MA」的
  混合口径 ⇒ **等于改判据输入语义**，超出「输入保真」红线。
- **对称性要求**: 该修复必须与 E3 的个股标注**同一行文风格**（这是 9/14 批「缺失/陈旧从静默改显式」的续做）。

### 4.2 P1 — 「普跌日」regime 的裁决路径（默认先回放，不先攒样）

**立场**: 用户既有偏好在先——「历史数据/重放可行时直接回测裁决，不默认前向攒样；攒样只留给
输入不存在于历史的新组件」。E12 已证明**该输入存在于历史**（QQQ/SOXX 盘前 5m bar 可重建）
⇒ 本议题走**只读回放**，不新建影子、不挂 cron、不增台账。

- **Phase 0（只读脚本，零副作用）**: `scripts/premarket_index_regime_replay.py`（**未实现，待裁**）
  - 特征（每日一行，冻结时点 ET 08:00，与 DA 实跑时刻对齐）：
    `qqq_pre_chg_0800` = QQQ 08:00 5m close vs 上一交易日 15:55 close；`soxx_pre_chg_0800` 同法；`spy_pre_chg_0800`。
  - 对象：`playbook_instructions` 中 `direction='BUY'` 的近 30 个交易日（当前 n=327，E14）。
  - 结局：`fwd_return_intraday` / `fwd_return_t1_adj`（**毛**，价格口径）**＋**真实成交子集 `pnl_net`（**净**）。
    费用口径必须显式：净均需扣 ~10bps 费用线才能与「普跌日是否值得降档」挂钩。
  - 读法（预注册，见下）：**连续特征分桶**（五分位 + 三个先验档 `QQQ≤-0.5%` / `QQQ≤-0.8%` /
    `QQQ≤-0.5% 且 SOXX≤-1.5%`），每桶报 **n / 毛净均值 / 中位 / worst5% / 双窗是否同向**。
  - 双窗：`2026-08-18 ~ 09-08` 与 `2026-09-09 ~ 09-24`（同一批一次裁完）。
- **预注册判据（见数前写死，读后不得放宽）**:
  1. 只有当「最普跌桶」相对其余桶的**净均差 ≤ 0**、**双窗同向**、且**该桶指令 n ≥ 30** 时，
     才进入「是否给低吸 BUY 降档」的下一次裁决；
  2. 任一条件不满足 ⇒ 记录「**无判别力**」并**关闭该轴**（登记进 §十，防后人重提）；
  3. 禁止在看见读数后改桶界/改结局字段（criteria-shopping，DEB-20260805 §1 引先例）。
- **功效/MDE 声明（不许用「攒样」掩盖不可达）**: 最严档事件率 9.5%（2/21 日，E13），
  按出事件日 ~15 条 LONG 指令计，桶内 n 达 30 需 **3–4 个事件日 ≈ 30–40 个交易日**；
  近端 LONG 产出已从 8 月的 17-22 条/日降至 5-11 条/日（E14）⇒ **按当前节奏 30 日内不可达**。
  因此：**若走事件桶路线，判据冻结至「事件日累计 ≥3 天」**；推荐主判据用**连续特征分桶**（n=327 一次到位），
  事件桶只作机制诊断的辅助读数。
- **不裁决就转正的例外**: 无。本提案不申请任何执行侧改动。

---

## 五、决策依据最小充分集

| 项 | 本提案的状态 |
|---|---|
| ① 固定样本来源（可复跑） | ✅ 指令表 + 5m ETH 历史 K 线（E12），脚本可复跑 |
| ② 多窗口（≥2 段） | ✅ 8/18–9/08 与 9/09–9/24 双窗并列，报「是否同向」 |
| ③ 配对/对照臂 | ✅ 同一批指令按特征分桶互为对照；无「只留命中组」的选样陷阱 |
| ④ 尾部 | ✅ 每桶报 worst5% + 单笔集中度 |
| ⑤ 费用后口径 | ✅ 毛（fwd_return_*）+ 净（pnl_net）**双列**，费用线显式 |
| ⑥ 机制诊断 | ✅ 分桶形状 + 桶间单调性（Spearman），不只比均值 |
| ⑦ 功效/显著性声明 | ✅ 事件率 9.5% / 不可达即冻结（§四 P1）；回放按日聚类 + bootstrap CI |
| ⑧ 预注册判据 + 回滚 + 复现命令 | ✅ §四 P1（判据）、§六（回滚两层）、§三（命令） |

未凑齐项：**无**。故本提案具备进入裁决的形式要件；但**结论方向未知**，不得预判为「应加门」。

---

## 六、实施步骤（分阶段、每步独立可回滚）

### Phase 0 — 只读回放（本步零副作用，建议**先批这一步**）

1. 新增 `scripts/premarket_index_regime_replay.py`（只读 DB + 只读 K 线 API，写 `data/regime_replay_<date>.json`）。
2. 验收命令（期望：脚本可跑、产物含双窗读数与 n；**不产生任何指令/不下单/不写 shadow**）：
   ```bash
   cd /opt/ftseek/FTSeek-US && PYTHONPATH=. ./venv/bin/python3.11 scripts/premarket_index_regime_replay.py --since 2026-08-18 --dry-run
   ```
3. 独立复查（§七）通过前，不得据其读数提第二次改动。

### Phase 1 — P0 输入口径修复（行为可见，需裁决后执行）

1. 改 `~/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` 的 `## 大盘环境` 渲染段（+ 采数侧
   `data_collector.collect_market_context()` 增盘前腿，值走既有 `collect_snapshot`）。
2. **同步清单**（漏一项即静默失效）: skill 版本 `0.84 → 0.85` + `references/CHANGELOG.md`；
   `tests/test_da_output_contract.py` 的 `PROMPT_HASH` 棘轮（改提示词**必然**变 hash）；
   仓内 DA 副本若存在须同改。
3. 验收：重跑一次单标的 `data_collector` + 渲染，人工核对「大盘环境」段是否出现基准日与盘前涨跌；
   `grep -c "昨收" <prompt>` 计数应 ≥ 个股标注同量级。
4. 提交标签：DA skill 改动走 `[RFC-XXX]`（仓内新增脚本按目录触发 commit-msg hook；`enabled:true` 类
   YAML 无改动，无需 TTY 确认）。

### 回滚（两层，缺一不可）

| 层 | 内容 |
|---|---|
| **法子** | Phase 0：删脚本+删产物；Phase 1：`git revert` 渲染段 + skill 版本回滚 + `PROMPT_HASH` 同步回旧值 |
| **触发条件**（出现即回滚，不留给下次讨论） | ① Phase 1 落地后 **5 个交易日内 DA 的 SKIP 占比上升 > 20pp**，或 **LONG 产出低于前 10 均值的 50%**（= 模型被「大盘在跌」过度压制，杀掉已验证的「LONG 回调日赢家模式」）；② 提示词渲染出现 `N/A`/空值；③ Phase 0 产物缺 `n/window/universe` 完整性字段 |

---

## 七、复查清单（点名请第二 agent 挑战，只读：禁改文件、禁 git 写、禁重启）

| 编号 | 声称 | 打假方式（命令/动作） | **反例判据**（什么输出说明我错） |
|---|---|---|---|
| C1 | 盘前 `market_context.spy.current` == 昨收 | 复跑 E4 + 取 `data/da_checkpoint/2026-09-24/*.raw.json` 任一文件的 `market_context.spy.current`，与 `prev_close` 逐字比较 | 出现**任一**标的 `current != prev_close`（且差值 > 0.05%）⇒ 声称不成立 |
| C2 | 该值**在盘中**会随今日 bar 变化（故缺陷仅盘前） | 复跑 E5 | 盘中 `spy.current` 仍等于 `prev_close` ⇒ 说明是缓存/其他根因，我的归因错 |
| C3 | 覆盖审计对「陈旧时点」不可见 | 读 `_classify_data_sources` 的 `market_context` 分支（E6） | 存在任何按时间戳/基准日校验的代码路径 ⇒ 声称不成立 |
| C4 | 板块上下文已带日期标签、非缺陷 | 复跑 E7 | 出现无日期的板块段，或标签与实际日期不符 ⇒ 我撤回「非缺陷」定性 |
| C5 | 1081 行是分类规则、无低吸偏好权重 | 复跑 E8 + `grep -rn "等回踩\|优先低吸" <DA skill 目录>` | 找到任何「优先/倾向低吸」表述 ⇒ §一#1 的更正本身要再更正 |
| C6 | 飞刀门今日未触发（ORCL 执行价 -4.21%） | 复跑 E16；再核 `playbook.premarket_invalidation_hard_gate` 现值 | 该开关=false，或执行价 < -5% ⇒ 「门按设计工作」的表述要改 |
| C7 | 历史盘前 5m bar 可重建（回放可行性） | 复跑 E12，并抽查 3 个不同日期 | 任意一天 08:00 段缺失 ⇒ Phase 0 路线降级为「前向攒样」并须重写 §四 P1 |
| C8 | 近 30 日无「市场级 regime」影子轴（不产生第二套账本） | 打印 `shadow_registry.py` 全部 id 并逐个判作用域 | 存在覆盖「市场级 regime」且未退役的轴 ⇒ 应改为同轴增量臂而非新轴 |
| C9 | 「回踩低吸偏好」不成立后，BUY 为主的归因（输入中性+门阈值+角色定位）是否够 | **换一个切面**：统计 09-24 五条 LLM BUY 的 thesis 关键词分布（错杀/回调/低吸 vs 突破/催化） | 若 thesis 里「突破/催化」占多数 ⇒ 我的归因需要重写（方向不是「低吸」，而是别的） |

**请复查者特别挑战三点**: ① C1/C2 的因果链（我是否把「日K口径」误读成「昨天的值」）；
② C7 的可得性是否只对 **QQQ/SOXX** 成立、对**更早日期**是否失效（额度/翻页）；
③ **§四 P1 的判据是否被我写成了对自己有利的形状**（桶界、结局字段、双窗切点是否隐含偏向）。

---

## 八、风险 / 取舍 / 待裁问题（frontier）

| 编号 | 待裁问题 | 推荐 | 一行理由 |
|---|---|---|---|
| F1 | 是否批准 P0（方案 B：标注基准日 + 附盘前涨跌） | **批准 B** | 纯输入保真、零判据改动；A 只治一半，C 改判据语义不可取 |
| F2 | P0 与 9/14 批是否合批执行 | **合批**（同族收口） | 同一函数、同一病灶家族；分两批会让「第三种漏修」再发生一次 |
| F3 | 是否新增「今日板块盘前强弱」（用 SOXX/SMH 盘前价做代理） | **先不做** | 属**新增输入**不是修 bug；且板块代理与 F4 的高度相关，应与 F4 同批裁 |
| F4 | 是否批准 Phase 0 回放 | **批准（只读）** | 用户既有偏好「能回放就别攒样」；E12 证明输入可重建 |
| F5 | 兄弟项：`MarketRegimeGate` 的 `spy_price` 盘前同为昨收，是否同批修 | **先只记档，后裁** | 今日实际影响=0（同侧 MA20）；但它与 P0 同型，值得一次独立复核后再定 |
| F6 | 若回放显示「普跌桶净均更差」，下一步动作 | **只走影子，不直接加门** | 同族先例 E2 momentum 门被裁 KILLED（第二道入场门边际价值快速下降）；且须先做残存效应（post-blackout 基线）计算 |
| F7 | P1 主判据用连续分桶还是事件桶 | **连续分桶为主，事件桶为辅** | 事件桶 30 日内不可达（E13/E14）；连续分桶 n=327 一次到位 |

**反方三问（自答，必须直说）**:
1. **会不会多出单/多赚钱？** —— **不会**。P0 买到的是语义正确（防模型在「大盘健康」错觉下批量低吸），
   P1 买到的是「有没有判别力」这个可裁决的答案（期望值是**无**）。收益在可见性与防无用功。
2. **新失败模式？** —— P0 可能让 DA 产出结构变化（SKIP 增多、LONG 产能下降），已配回滚触发条件（§六）；
   P1 若被当成「预演的门」来读，会把「无判别力」误读成「应该加门」⇒ §七 C9 与 §四 判据① 反制。
3. **与已回滚/已证伪方案的关系？** —— 直接相关三例：`e2_momentum`（2026-09-13 KILLED）、
   `premarket_invalidation A/C`（2026-09-11 退役，仅存 B 且口径已更正为「盘中早段」）、
   `DEB-20260808` post-blackout 基线纪律（新规则必须在已落地规则基线上算残存效应）。
   本提案**不重开**任何一条：P1 第一轮只做只读回放。

---

## 九、与既有文件关系表

| 既有文件 | 它覆盖什么 | **本提案不重裁什么** | 本提案依赖它哪一段 |
|---|---|---|---|
| `DEB-20260914-premarket-data-validity-audit` | 盘前「时点不清的值被当实时值」三处病灶（VIX fail-close 纸面化 / DA VIX 恒缺 / 板块脉搏无日期） | 不重裁其任何判据与结论 | **本提案 = 同族第三处遗漏**（同函数 SPY/QQQ）；沿用其「标注基线日期」修法与红线（不改判据） |
| `DEB-20260816-regime-entry-gate` | 宏观披露日（CPI/PPI）入场降速门，**提案裁决中未实施** | 不重裁披露日门；本提案特征（盘前指数）与它（宏观日历）不同 | 其「regime 层零接线」现状事实（本提案不复用其修订路线） |
| `RFC-068`（接飞刀硬门） | 执行时刻 -5% B 门 | 不改阈值、不改口径 | E9/E16 —— 它已在线，是「温和普跌日无闸」的另一半事实 |
| `premarket_invalidation`（影子，仅存 B） | 逐股盘前/早段异动（口径 9/21 更正为 09:35–09:59 ET 盘中早段） | 不重裁 A/C 退役、不改其 B 规则 | 其 9/13 姊妹裁决（e2_momentum KILLED）作为**反方先验**引入 §八 F6 |
| `DEB-20260901`（entry archetype / PULLBACK_LIMIT） | archetype 枚举语义与路由 | 不改枚举、不改路由 | E8 —— 用于**更正**我上一轮对 1081 行的误读 |
| `DEB-20260924-jev-bounded-judgment-layer-feasibility` | 同日另一议题（外部判断部件可行性） | 无关 | 无（仅说明当日编号已被占用） |

---

## 十、自曝 / 更正记录

1. §一#1 更正（1081 行性质）: 上一轮把 archetype 分类规则读成「低吸优先指令」——
   **方向性误读**（把回显当权重）。据 E8 更正。
2. §一#2 撤回（板块实时性定性）: 9/14 批已修（E7），我上一轮的建议 #2 属**重复立案**，改列为 F3 新增输入议题。
3. §一#3 补正（文件路径）: `llm_synth.py` / `signal_gate.py` 位于 DA skill 目录而非仓内 `app/`；行号复核无误。
4. **计入口径纪律（本轮新踩）**: E13/E14 的事件率与产出率必须用 SQLite 到 **date 列类型**——
   本表 date 为 `YYYY-MM-DD` 文本，等值比较本次已用同格式字面量（`date>='2026-08-18'`），
   未混入 epoch 数值（后者会静默命中全表）。
5. 本提案**未引用**任何未复核的历史读数；E-table 全部为本轮实跑（2026-09-24 ET 08:20–10:10）。

## 十一、讨论记录

- **R1（2026-09-24, Hermes）**: 立案。三条成因定性 + 两处自我更正 + Phase 0/1 路线。
  待用户裁 F1–F7；Phase 0 脚本**未实现**，须先获裁再写码。