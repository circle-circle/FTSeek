# 议题：MFE 小的本质根因 — "DA 方向错误"还是"入场在最高价"？（都不是，且都沾边）

## 状态

- [x] 进行中（第 1 轮：数据调查完成，方案待辩论方核验）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> **本文用途**：回答用户 2026-07-24 提出的问题——"DA 产出方向错误或者入场最高价导致 MFE 小的问题如何解决"。方法：先用数据把两个候选假设**分离**（它们解法完全不同），再挖到机制层，最后给出多角度方案（含框架外替代方案）与诚实弱点。
> **作者**：Qoder　**日期**：2026-07-24（ET 07-23 晚）
> **数据口径**：全部 `source_type LIKE 'PLAYBOOK%'`，防跨策略污染（crash-day DEB 第 6 轮教训）。
> **前置关联**：[DEB-20260723-crash-day-long-bias-postmortem.md](./DEB-20260723-crash-day-long-bias-postmortem.md)（MFE<0.5% 分水岭 + P0 早走 shadow 已在轨）

---

## 一、问题的重新定义（现象 → 本质）

**用户直觉的两个候选根因**：
- H1：DA 产出方向错误 → 做多后跌 → MFE 小
- H2：入场在（当日）最高价 → 之后从未回到入场价上方 → MFE 小

**数据给出的答案：两个都不准确，但各沾一部分。** 真实图景是三层叠加：

| 层 | 本质 | 证据 |
|---|---|---|
| L1 信号层 | **方向不是"错"，是"无边缘"**（≈随机，非反指） | §二.1 |
| L2 机制层 | **穿越即市价全仓 + 回调路径零确认** → 把无边缘转化为 MFE≈0 | §二.2/2.3 |
| L3 持有层 | **输家拖 4.3h vs 赢家 2.1h 的持有倒挂**，把随机成本放大成稳定出血 | §二.4 |

一句话：**MFE<0.5% 不是某一笔单子的"错误"，是"计划位穿越延续"这个策略形态在无边缘信号下的稳态分布（每天 50~63%），再被持有结构放大成亏损。**

---

## 二、证据链（全部可复现）

### 二.1 方向对错分离（决定性实验）：不是反指，是无边缘

对本周 31 笔 MFE<0.5% 单子 join 指令表前瞻收益（`playbook_instructions.fwd_return_*`，小数制）：

```sql
SELECT t.symbol, t.trade_date, t.realized_pnl, t.mfe_pct,
       p.fwd_return_intraday, p.fwd_return_t1, p.fwd_return_t3
FROM trade_records t LEFT JOIN playbook_instructions p
  ON p.symbol=t.symbol AND p.date=t.trade_date
WHERE t.trade_date BETWEEN '2026-07-20' AND '2026-07-24'
  AND t.source_type LIKE 'PLAYBOOK%' AND t.exit_time!='' AND t.mfe_pct < 0.5
```

结果（有 fwd 数据的 18 笔，07-20~22；07-23 的 T+1 尚未回填）：

| 指标 | 结果 | 含义 |
|---|---|---|
| 当日 intraday 转正 | **1/18 (6%)** | 出场后当天也没修复 |
| T+1 转正 | **1/18 (6%)**（NVDA +2%） | 次日也不修复 |
| T+1 跌超 1% | **0/18** | 但也不是灾难性判反 |
| T+3 转正 | **0/n** | 三天内无均值回归 |

**三分法判定**：
- 若是 H1"方向错（反指）" → T+1 应大幅反向（可反手利用）→ **否**（无一大跌）
- 若是 H2"入场差但方向对" → T+1/T+3 应转正（价格回来）→ **否**（仅 1 笔 +2%）
- 实际：**无边缘（no edge）**——选出的票穿越后延续率≈随机，且不发生快速均值回归。**反着用也赚不到钱**（排除"反向信号"幻想）。

### 二.2 触发机制真相：不是"总在最高点追入"

读码 `app/playbook/pipeline.py:3256-3336`（触发判定）+ `:979`（下单）：

**实际是三路径限价穿越逻辑 + 市价全仓**（BUY 为例，SHORT 镜像）：

| 路径 | 条件 | 确认要求 | 形态 |
|---|---|---|---|
| 回调入场 | `current ≤ entry_price`（下限 knife_floor **-3%** 防接飞刀） | **零确认，直接触发** | 接回调 |
| 追涨入场 | `entry < current ≤ entry×1.05`（chase_zone **+5%**） | 量能门 intraday_vol_ratio ≥1.5 | 追突破 |
| 放弃 | `> entry×1.05` / 极端 gap >8% / thesis 漂移 >5% 作废 | — | — |

触发后 `OrderType.MARKET` 市价全仓（`playbook_service.py:979`）。

**遥测证据**（`data/trigger_miss_telemetry.jsonl`，本周 1500 条未触发记录）：
- `vol_gate_block` **892 次（59%）** —— 追涨路径大半被量能门拦下
- `after_cutoff` 405、`vwap_gate_block` 99、`extreme_gap` 54、`runaway_down` 48

**推论修正**：追涨路径有量能门重兵把守（且大多被拦），**实际入场的主力是"回调路径"——而回调路径零确认**（价格 ≤ 计划价即市价买入，只要不深于 -3%）。所以 MFE 小的机制成因不是"追高"，而是 **"接飞刀"：回调到计划价就接，接住后继续阴跌 → MFE≈0、MAE 累积**。

> 诚实标注：回调/追涨两条路径的实际入场占比**没有落库**（触发时走哪条路径不写 trade_records）——这是观测缺口 O1（§五）。

### 二.3 每日稳态：不是 regime 特有，是形态属性

```sql
SELECT trade_date, COUNT(*), SUM(mfe_pct<0.5), SUM(direction='LONG'), ROUND(SUM(realized_pnl),1)
FROM trade_records WHERE ... GROUP BY trade_date
```

| 日期 | n | MFE<0.5% | 占比 | 当日 PnL |
|---|---|---|---|---|
| 07-20 | 4 | 2 | 50% | **+40.3** |
| 07-21 | 17 | 9 | 53% | -158.2 |
| 07-22 | 14 | 8 | 57% | -163.8 |
| 07-23 | 19 | 12 | 63% | -379.2 |

**连赚钱的 07-20 也有 50% 单子 MFE<0.5%**——这是策略每天如此的稳态，崩盘日只是放大单均亏损（-20/笔 vs -9.3/笔），不是成因。**任何"等大盘好了就没事"的归因都被此表证伪。**

### 二.4 赢家/输家镜像：持有时间倒挂

| 组 | n | avg MFE | avg MAE | avg 持有 | WR |
|---|---|---|---|---|---|
| WIN（MFE≥0.5%） | 20 | 0.94% | **0.42%** | **2.1h** | 95% |
| LOSE（MFE<0.5%） | 30 | 0.18% | **1.66%** | **4.3h** | 7% |

好单进场即走对（MAE 浅、快速锁利 2.1h）；坏单进场即漂（MAE 1.66%、拖 4.3h TIME_EXIT）。**输家持有时间是赢家的 2 倍**——随机成本被时间放大成确定性出血。（P0 早走 shadow 正是治这一层，已在轨。）

### 二.5 DA conf 有信息，但触发层不消费它

7 月 PLAYBOOK 109 笔按 signal_confidence 分桶：

| conf 桶 | n | 中位数 PnL | 均值 | 总 PnL | top3 |
|---|---|---|---|---|---|
| [0.25,0.45) | 67 | **-2.3** | -6.5 | **-435.8** | +50/+59/+102 |
| [0.85,1.0) | 32 | **-5.1** | +3.5 | **+111.8** | +114/+169/+247 |

**反直觉发现（重要）**：高 conf 桶中位数也是负的——两桶的差别不在中位数（都亏），在**右尾厚度**（高 conf 的 top1 +247 vs 低 conf 仅 +102）。这完全符合突破/穿越形态的理论：利润来自少数大赢，conf 的作用是**加厚右尾**，不是提高中位数。

**含义**：① conf 加权有效（低桶期望明确为负：-6.5/笔×67 笔），但**不能一刀切砍低 conf**（桶内有 +102 的右尾）；② 触发层目前完全不读 conf（穿越即触发，conf 0.35 和 0.9 同等待遇）——**DA 的智力成果在入场层被稀释为零**。

---

## 三、本质总结（一张因果图）

```
DA/UE 盘前出"票+方向+计划价"（conf 含信息但不被下游消费）
        │
        ▼  盘中价格穿越计划价（回调≤entry 零确认 / 追涨≤+5% 需量能门）
市价全仓入场 ←—————— 主力路径：回调零确认（接飞刀形态）
        │
        ▼  穿越延续率 ≈ 随机（信号无边缘：T+1/T+3 不修复，44% WR）
~55% 单子 MFE<0.5%（每天如此，与 regime 无关）
        │
        ▼  持有倒挂：输家拖 4.3h × MAE 1.66%（赢家才 2.1h）
随机成本 → 确定性出血（TIME_EXIT 单桶 -$736/周 = 全周净亏 111%）
```

**三层可分别施策**（这是好消息）：L1 信号层加 conf 加权、L2 机制层给回调路径加确认、L3 持有层早走（已在轨）。三者不互斥，且每层都可 shadow 先行。

---

## 四、方案空间（多角度，按 杠杆÷风险 排序）

### P0（已在轨，不变）：MFE<0.5% 早走 shadow
治 L3 持有倒挂。预期 +$220~370/周。**先把无边缘的成本压到最小，是其他一切的前提。**

### P1-A（新增，本轮最大增量）：conf 进入触发层
- **规则**：conf≤0.35 指令的**回调路径也要求量能确认**（与追涨同标准），或直接不入场；conf≥0.85 维持现状（保护 +247 右尾）
- **依据**：§二.5，低 conf 桶 -435.8/7 月，期望明确为负
- **形式**：shadow 反事实（record-only 与 P0 同框架），测"被拦单的 MFE 分布"防误杀右尾
- **风险**：低 conf 桶内有 +102 赢家 → shadow 必须给出精确率/召回率，不一刀切

### P1-B（新增）：回调路径质量门（治"接飞刀"）
- **规则**：回调触发后延迟 N 分钟（或要求 5min K 企稳/不再创新低）再入场
- **依据**：§二.2，回调路径零确认是 MFE≈0 的主力机制成因
- **风险**：赢家 MAE 仅 0.42%（回调很浅）——延迟入场会吃掉赢家 0.3~0.5% 的 MFE 起点；**必须 shadow 测对赢家的磨损**（与 P1-A 同一个 shadow 框架可复用）

### P1-C（观测缺口，3 行改动）：entry_path 落库
- 触发时把"pullback / chase / reanchored"写入 trade_records（或 signal_details）
- **没有它，§二.2 的两路径效果永远无法分离评估**——当前所有机制结论都是推断
- 零风险，立即做

### P2（3 行改动）：entry_time 时区修复
- 当前全部 entry_time 显示 "20" 时（时区混杂，第一性文档原子事实 4）→ 时段分析不可做
- 修复后解锁：开盘 30min vs 午后入场的 MFE 分布 → 可能存在"时段门"（开盘噪声期不接飞刀）

### 框架外替代方案（用户要求不藏——以下按诚实评估给出）

| 方案 | 描述 | 评估 |
|---|---|---|
| **X1 降频路线** | 若 P0+P1 后 PF 仍 <1.3：砍到每周只做 conf≥0.85 的 2~3 笔 | **最值得认真对待的备选**。7 月高 conf 桶 32 笔 +111.8，低频但右尾厚。突破形态 PF≈1 时"少即是多"在数学上成立。决策点：P0 shadow 两周后 |
| **X2 双确认形态** | 个股穿越 + 板块同向（sector momentum 数据 playbook 已有）双闸门 | 把 conf 加权推广为"环境加权"。本周 52/54 全 LONG 在板块普跌日接飞刀，此门可拦一批。需 join sector 数据回测，工程量中 |
| **X3 盘中重判向** | DA 判向从盘前挪到 10:00 ET（用开盘 30min 数据） | 压缩信号→入场时滞。但 DA 单轮 28min，工程大；且盘前判向+盘中触发本是时滞对冲设计。缓议 |
| **X4 期权化** | 假突破成本锁死权利金，真突破杠杆放大 | 改变整个 P&L 形态与风控/保证金体系，工程巨大。仅记录，不建议现阶段 |
| **X5 反向利用输家** | MFE<0.5% 群体反手 | **已证伪**：§二.1，T+1 无一修复但也无大跌，无边缘≠反指，反着用也是随机 |

---

## 四-b、外部方向借鉴评估（2026-07-24 晚，应用户要求调研）

### 1. DojoAgents（github.com/Alpha-Dojo/DojoAgents）：**无直接借鉴价值**

组合管理层的 AI 代理框架（自主推理引擎 + 沙盒代码执行 + 记忆技能库），解决"投资组合诊断/板块轮动/用户批准后追踪"。在我们的问题维度（盘中动量入场质量）上更浅：**无多空辩论、无回测/shadow 机制、无入场信号质量验证**——这三项我们已有且更完善（DA 双模型辩论、P0/P1-C shadow 已上线）。层次不符，pass。

### 2. 微结构特征（OFI/订单流毒性，hummingbot 式）：**数据证伪，不投入** ✗

`entry_timing_shadow.jsonl` 已采集 order_flow_imbalance / ob_spread_bps / slope_5m 等特征，73 笔 live_triggered join 实际结果：

| 特征分组 | n | WR | MFE<0.5% 占比 | 总 PnL |
|---|---|---|---|---|
| OFI>0.1（买盘失衡） | 14 | 50% | 50% | -75.4 |
| OFI±0.1 | 50 | 44% | 52% | -511.4 |
| OFI<-0.1（卖盘失衡） | 9 | 44% | 56% | -73.8 |
| slope_5m>0 | 38 | 39% | 58% | -404.4 |
| slope_5m≤0 | 35 | 51% | 46% | -256.2 |

OFI 三组几乎无区分度 → **该特征对 MFE 无预测力**。slope_5m 有弱反向（追涨组更差）但样本小。结论：不在无效特征上建 ML 模型（negative result 同样值钱）。

### 3. 波动率过滤（"预测波动替代预测方向"，学术共识）：**验证有效，本次最大新发现** ✓✓

外部共识：方向预测≈随机，但**波动率/流动性可预测性高得多**。MFE 本质是波动捕捉——票的日常波动不足时，MFE 到 0.5% 是结构性不可能。用指令表 `atr_pct` 验证（7 月 55 笔 join）：

| ATR 桶 | n | avg MFE | MFE<0.5% 占比 | WR | 总 PnL |
|---|---|---|---|---|---|
| **<2.5%（KO/DIS/BAC/JPM/XLE/MNSB 型）** | 13 | 0.27 | **85%** | **15%** | **-244.0** |
| 2.5-3.5% | 26 | 0.64 | 42% | 62% | -73.5 |
| 3.5-5%（含 CRM 三连亏污染） | 11 | 0.39 | 64% | 36% | -315.2 |
| ≥5% | 5 | 0.94 | 40% | 60% | -28.4 |

- **ATR<2.5% 桶灾难性**：85% 单子 MFE<0.5%——低波防御股做动量突破天然失败，**波动能力决定 MFE 天花板**
- B 桶（2.5-3.5%）健康：WR 62%
- C 桶差是 CRM/META 个体污染（连败封禁 P1 覆盖），不归因 ATR
- 反事实：本周拦掉 A 桶 → 省 -244，误杀 2 赢家（KO+20/MA+16）→ **净省 ~$208/周**

### 4. Intraday momentum（Gao-Han-Li-Zhou 2018 开盘 30min 动量）：待验证

`opening_range_ready/opening_high/opening_low` 已在采集，但时段分析需 P2（entry_time 时区修复）先行。

### 5. 新增方案 P1-D：ATR 硬门（并入 shadow 框架）

- **规则**：指令触发时 `atr_pct<0.025` → record-only 标记"若拦"（2 周后按精确率裁决是否切 live）
- **落地侧**：触发侧（`_check_intraday_trigger` 读指令 atr_pct）优先于生成侧——不改 DA/UE 产出，只在入场前把关
- **与既有方案正交**：连败封禁管"同票反复亏"，ATR 门管"低波票不做"，conf 门管"低置信不做"——三门可叠加
- **风险**：atr_pct 回填率需确认（7 月 55/109 有值）；阈值 2.5% 系 55 笔事后切片（同 §五.7 自警，shadow 标定）

---

## 五、诚实弱点（必须随方案一并呈交）

1. **观测缺口 O1**：入场路径（回调/追涨）未落库 → §二.2"回调路径为主力"是遥测+代码推断，非逐笔直证。P1-C 补齐前，P1-B 的设计参数（延迟多久）缺乏标定数据。
2. **fwd 覆盖 18/31**：07-23 的 12 笔 T+1/T+3 尚未回填，"无边缘"结论将在回填后复核（若 07-23 崩盘日 fwd 大面积转正，结论需修正为"regime 主导的时点错"）。
3. **conf 桶小样本**：高 conf 桶 32 笔、右尾 top3 贡献全部利润——单票（+247）就能翻转桶结论。conf 加权的 shadow 期不得短于 P0（2 周）。
4. **滑点未量化**：触发→市价成交的滑点（估 0.1~0.3%）直接吃 MFE，现有数据无法分离"信号无边缘"与"执行滑点"各占多少。需 tick/逐笔数据。
5. **P1-B 对赢家的磨损未知**：赢家 avg MFE 0.94%，延迟入场若吃掉 0.3~0.5%，赢面缩 1/3——shadow 必须同步记录"赢家侧成本"，不能只看输家侧节省。
6. **时段维度全盲**：entry_time 时区债修复前，"开盘不接飞刀"类方案无从验证。
7. **方法学自警**：本文 §二.5 的 conf 分桶是事后切片——与 crash-day DEB 第 5 轮"MFE<0.3% 完美分离"的小样本过拟合教训同类，一切阈值以 shadow 前瞻数据为准，不以本文回测值为操作参数。

---

## 六、建议行动序列

| 序 | 动作 | 性质 | 依赖 |
|---|---|---|---|
| 1 | P0 MFE<0.5% 早走 shadow 脚本 | 已决策，待写 | 无 |
| 2 | P1-C entry_path 落库（3 行） | 零风险观测 | 无 |
| 3 | P1-A conf 加权 + P1-B 回调确认，并入同一 shadow | record-only | P1-C（参数标定） |
| 4 | P2 entry_time 时区修复（3 行） | 工程债 | 无 |
| 5 | 07-23 fwd 回填后复核 §二.1 | 复核 | T+3（07-28） |
| 6 | 2 周 shadow 后裁决：切 live / 启动 X1 降频评估 | 用户决策 | 1+3 |

**✅ 落地记录（2026-07-24，Qoder）**：

| 项 | 状态 | 实现 |
|---|---|---|
| P1-C entry_path 落库 | ✅ 已上线 | `app/playbook/pipeline.py:3324-3350`（三路径标注 pullback/chase/out_of_band + reanchored 覆盖）→ `_emit_entry_shadow` 新增 `entry_path` + `inst_confidence` 字段 → `data/entry_timing_shadow.jsonl`。6 场景冒烟全通过（BUY/SHORT × pullback/chase/out_of_band），测试记录已清理。 |
| P0 MFE 早走 shadow | ✅ 已上线 | `app/pipeline/position_monitor.py:1084-1142`：持仓满 90min 记录一次快照（mfe_pct/mae_pct@90min、四档阈值命中 0/0.1/0.3/0.5%、未实现盈亏）→ `data/shadow/mfe_early_exit.jsonl`。scheduler 级 set 幂等。record-only 零行为改动。**⚠ 2026-07-24 夜首夜实测修正：此钩子是死路径**——playbook 仓不进主线 position_monitor（被 owner 过滤/不入主线 store），jsonl 永远不会有 playbook 样本。**但标定数据未丢**：正确宿主 `monitor_playbook_sim_exits` 早已有 E shadow 检查点（`e_shadow_45/60/90m` 写 trade_records.metadata，首夜 7/7 实采，含 mfe/mae/price/held_min），覆盖同一需求且更密。处置：下方盘后分析 SQL 改读 `e_shadow_*`；position_monitor 死块周末减法移除（重复建设教训：落 shadow 前先查正确宿主是否已有同功能埋点）。 |

**盘后分析 SQL（2 周 shadow 后跑）**：

```bash
# P0: 四档阈值精确率/召回率标定 — shadow join 实际结果
./venv/bin/python -c "
import json, sqlite3
c = sqlite3.connect('data/ftseek_us.db'); c.row_factory = sqlite3.Row
trades = {}
for r in c.execute(\"SELECT symbol, trade_date, realized_pnl, mfe_pct, exit_reason FROM trade_records
  WHERE source_type LIKE 'PLAYBOOK%' AND exit_time!=''\"):
    trades[(r['symbol'], r['trade_date'])] = dict(r)
seen = set()
for line in open('data/shadow/mfe_early_exit.jsonl'):
    s = json.loads(line)
    key = (s['symbol'], s['direction'], s['entry_time'])
    if key in seen: continue          # 重启重记去重
    seen.add(key)
    day = s['ts'][:10]
    actual = trades.get((s['symbol'], day))
    if actual:
        print(s['symbol'], day, 'mfe@90min=', s['mfe_pct_at_90min'],
              'unreal@90min=', s['unrealized_pct_at_90min'],
              '→ actual_pnl=', actual['realized_pnl'], actual['exit_reason'])
"

# P1-C: 入场路径效果分离（live_triggered=True 的行 join trade_records）
./venv/bin/python -c "
import json
from collections import defaultdict
agg = defaultdict(lambda: [0,0])
for line in open('data/entry_timing_shadow.jsonl'):
    r = json.loads(line)
    if r.get('live_triggered') and r.get('entry_path'):
        agg[(r['entry_path'], r.get('inst_confidence'))][0] += 1
print(dict(agg))  # 按 (路径, conf) 统计触发次数, 再 join trade_records 算各组 PnL
"
```

**使用注意**：
1. **重启重记**：P0 幂等 set 是内存级，进程重启后同一持仓会再记一条（entry_time 相同）→ 分析侧按 `(symbol, direction, entry_time)` dedupe（上方 SQL 已含）。
2. **entry_time 时区债**：P0 的 age 计算依赖 position.entry_time 时区正确（同 H2 保护块口径）；时区异常时 age=-1 不记录（fail-safe 不误导）。P2 修复后数据质量自动改善。
3. **P1-A/P1-B 反事实数据源已就位**：P1-A（conf 加权）= `live_triggered=True 且 inst_confidence≤35 且 entry_path=pullback` 的记录 join 实际结果；P1-B（延迟入场）= 同一 inst_id 触发后 N 轮的 current_price 序列（entry_timing_shadow 每轮都记，天然是价格轨迹）。无需新机制，2 周后切片即可。
4. **同标的连败计数（缺陷 D）**：盘后由 mfe_early_exit 的 symbol+ts join 近 5 日 trade_records 计算，不入 tick（避免监控路径查 DB）。

---

## 复现命令

```bash
cd /opt/ftseek/FTSeek-US
# §二.1 方向对错分离
./venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/ftseek_us.db'); c.row_factory=sqlite3.Row
for r in c.execute('''SELECT t.symbol,t.trade_date,t.realized_pnl,t.mfe_pct,
  p.fwd_return_intraday fi,p.fwd_return_t1 t1,p.fwd_return_t3 t3
  FROM trade_records t LEFT JOIN playbook_instructions p ON p.symbol=t.symbol AND p.date=t.trade_date
  WHERE t.trade_date BETWEEN '2026-07-20' AND '2026-07-24'
  AND t.source_type LIKE 'PLAYBOOK%' AND t.exit_time!='' AND t.mfe_pct<0.5 ORDER BY t.trade_date'''):
  print(dict(r))"
# §二.5 conf 分桶
./venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/ftseek_us.db')
for r in c.execute('''SELECT ROUND(signal_confidence,1) b,COUNT(*),ROUND(AVG(mfe_pct),2),
  ROUND(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0 END)*100),ROUND(SUM(realized_pnl),1)
  FROM trade_records WHERE trade_date>='2026-07-01' AND source_type LIKE 'PLAYBOOK%'
  AND exit_time!='' GROUP BY b ORDER BY b'''): print(r)"
# §二.2 遥测
./venv/bin/python -c "
import json; from collections import Counter
rows=[json.loads(l) for l in open('data/trigger_miss_telemetry.jsonl') if l.strip()]
print(dict(Counter(r.get('reason') for r in rows if r.get('date','')>='2026-07-20')))"
```

**关键代码锚点**：`app/playbook/pipeline.py:3256-3336`（三路径触发）、`:979`（MARKET 市价单）、`:3284-3307`（量能门）、`app/playbook/entry_band.py`（CHASE_ZONE 5% / KNIFE_FLOOR 3%）、`app/pipeline/position_monitor.py:1062-1082`（MFE/MAE 逐 tick 语义）。

---

### 第 1 轮讨论记录

- 作者（Qoder，2026-07-24）：提交上述调查与方案。核心主张——MFE 小的本质是"无边缘信号 × 回调路径零确认 × 持有倒挂"三层叠加；H1（方向错）修正为"无边缘非反指"，H2（最高点入场）修正为"回调路径接飞刀为主力"。
- （待辩论方核验 / 第 2 轮）：
