# DEB-20260928 · SL_INITIAL 止损几何归因（只读，冻结期合规）

- **立案日**: 2026-09-28
- **发起人**: 用户（"从提升收益的目标出发给出优化建议"→ 同意立案）
- **状态**: **测量已修 + 点估计反向（noise ≤ breakdown），建议直接结题（待用户终裁）**；§十一 Codex 复核补齐事件优先删失后正典表为 noise 6/10 vs breakdown 3/5、p=1.0；零行为变更——冻结期内不提参数
- **类型**: 只读归因 → 若假设成立，9/30 冻结结束后另立"stop ≥ k×ATR"影子
- **来源**: DEB-20260928 §五/§十四 + DA 笔记挑战者行（止损几何问题）+ loss_exit_recovery
- **关联**: 判据字典 C1（退出参数冻结至 9/30）；DEB-20260928 底牌7（回翻分层）

---

## 一、问题界定

日内 `SL_INITIAL` 是第二大出血口（16 笔 −$2,081），且日期/价格层分散、无单点集中——
不是个别坏票，可能是**止损几何与日内噪音不匹配**的结构问题。DA 笔记挑战者已多次点名
"止损距离 < 单日振幅"（例：AMD 止损 3.24% vs 单日振幅 5.135%）。本 DEB 只做一件事：
**用逐仓 ATR 比（|entry−stop|/atr）量化"多少 SL_INITIAL 是被日内噪音打穿的"**，
并预注册"若过半是噪音触发"的后续影子入口。

## 二、底牌清单

| # | 事实 | 读数 | 证据锚 |
|---|---|---|---|
| 1 | 日内 SL_INITIAL 16 笔 **−$2,080.86**（−$130.05/笔）；另隔夜 1 笔（UBER 8/26）−$307.13 | 日期分散 8/28→9/24；层分布 <$20 3 笔 −$325 / $20–100 3 笔 −$598 / ≥$100 10 笔 −$1,158 | trade_records SQL（§附） |
| 2 | 回翻分层（旧方向+旧删失口径）：SL_INITIAL ≤5 日触碰 **59%**（13/22）→ ≤20 日衰减 **38%**（3/8）；§十一正典口径为 ≤5 日 **11/23=47.8%**、≤20 日 **14/19=73.7%** | 旧“过半快回”不再成立，且不存在衰减形态；**回≠该扛** | scripts/loss_exit_recovery_check.py 实跑 + §十一复核 |
| 3 | 逐仓 ATR 已落库 | metadata `atr`（**绝对价**）+ `atr_basis`（=proxy）+ `stop_loss`——量纲可声明、无需新拉行情 | trade_records.metadata_json |
| 4 | 全量 16 笔 ratio 已读出（本会话附录命令实测） | 14/16 聚在 **0.93–1.04**（止损系统性贴 **1×ATR** 放置）；1 离群 LLY **16.17**（ATR 3.13 vs 止损距 50.61）；**noise（ratio<1）=10/16=62.5%**；7/16 行 `atr_basis` 缺失（旧行）须分层 | §附 SQL（已实跑） |

## 三、影子/归因设计（裁决后落地，只读）

1. **人群**：8/25 起日内 `SL_INITIAL` 已平仓（含后续增量），逐笔计算
   `ratio = |entry − stop_loss| / atr`（**同基绝对价，无量纲**；atr_basis≠proxy 的行
   如实标注并单独分层，防量纲混装）。
2. **分类**：`noise`（ratio < 1.0，止损距离小于一个 ATR）vs `breakdown`（ratio ≥ 1.0）。
3. **结局 join**：接 `loss_exit_recovery` 的 ttr_touch/close 口径，比两桶的 ≤5 日回翻率。
4. **报告铁律**：ratio 单位（无量纲）、ATR 单位（绝对价）、分母（atr）三项每报必带
   （军规"单位声明+契约"纪律）。
5. **⚠️读数已部分先行披露**：ratio 半场（§二底牌 4）在附录命令复现检查中已跑出
   （noise 62.5%）——正式归因报告须原样复核该数并补齐**恢复率差**半场与 basis
   分层，判据不因先行读数移动任何阈值。

## 四、反方观点

| # | 反方论据 | 回应 | 残余风险 |
|---|---|---|---|
| R1 | 放宽止损=错时亏更深（deep 桶 20% 回翻支持砍深亏是对的） | 归因不自动等于放宽——只测"噪音触发占比"；k×ATR 影子届时独立预注册 | 高（方向反噬风险） |
| R2 | 回翻≠该扛（DEB-20260928 Q4 幸存偏差已登记） | 归因判据用"噪音占比+回翻差"双条件，不用单一回翻率 | 中 |
| R3 | C1 冻结：止损参数 9/30 前禁改 | 本 DEB 冻结期只读；任何参数影子排 10 月后 | 低（设计内） |
| R4 | ATR basis=proxy 的质量/量纲风险 | §三.1 按 basis 分层 + 单位必报；proxy 行单独统计 | 中 |
| R5 | n=16 小样本 | 归因只作方向参考；后续影子另设 n 门 | 中 |

## 五、预注册判据（归因读出后，冻结结束后才可触发下一步）

```
sl_initial_stop_geometry 归因门 (一次读数, 无攒样):
  noise 桶占比 ≥ 50% (日内 SL_INITIAL)
  ∧ noise 桶 ≤5日触碰率 ≥ 70% 且显著高于 breakdown 桶
  → 10 月冻结结束后另立 "stop ≥ k×ATR" 影子 DEB
    (k 预注册单值, 禁网格; 影子先行; 用户裁)
  否则: 止损几何假设不成立, 结题 (出血归 SL_INITIAL 正常功能)。
禁改: ratio 边界 1.0 / noise 定义 — 改=重裁。
```

## 六、边界 / 验证 / 回滚

- 零行为零配置零 cron；归因脚本只读 DB + 复用 loss_exit_recovery 产物。
- 回滚：整文件 revert。
- 自曝 1：建议稿口头版直接指向"k×ATR 参数影子"——自查 C1 冻结 + 一次一变量纪律后
  收敛为"归因→条件触发→另立影子"三段式。
- 自曝 2：附录命令复现检查把全量 16 笔 ratio 先跑了出来（本想只验 SQL 语法）——
  按"轮内自我修正光荣"就地披露：noise 62.5% 已知，判据**未因看到读数而移动**
  （ratio 边界 1.0 是预注册后才跑的全量）；剩余决定性条件=恢复率差。
- 自曝 3：全量读数显示止损已系统性贴 1×ATR（14/16 在 0.93–1.04）——原假设表述
  "止损几何不匹配"应精化为"**1×ATR 对日内噪音是否过紧**"，本节就地更正。

## 七、作者复查追加（2026-09-28，详见 DEB-20260928 §十五；判据原文未动）

- **数字 CONFIRMED**：日内 SL_INITIAL 16 笔 −$2,080.86/−$130.05、隔夜 1 笔 −$307.13、LLY ratio 16.169 离群、`atr_basis` 缺失 7/16 —— 全部逐位吻合。
- **1 处小差**：贴 1×ATR 带（0.93–1.04）实测 **15/16**（原文称 14/16）。差的 1 笔 = SAIC，其 `|entry−stop| = 6.30 == atr = 6.30` **精确相等** ⇒ ratio=1.000 落在带内边界。不影响任何结论，登记备查。
- **🔴 §15.4 判据第二条是死门（数学不可达）**：`noise ≤5日触碰率 ≥70% 且**显著高于** breakdown 桶`。实测桶容量 **noise n=12 / breakdown n=4**（含 LLY 离群）。功效检验：即便取极端（noise 12/12=100% vs breakdown 0/4=0%），Fisher 精确双侧 **p=1.0000** ⇒ "显著"在 breakdown n=4 下**不可达**。实测两桶触碰率 noise **11/11=100%** vs breakdown **2/3=66.7%**（方向差存在、n 不允许称显著）⇒ 建议改为**效应量 + Wilson 95%CI**或降为观察项不设门，否则本 DEB 永远无法裁决。同 `trivial_rule_control_arm` 影子已登记的教训："不是攒样能救的，是尺子选错了"。
- **🟡 §15.5 判据第一条边界未预注册精度（敏感性极高）**：15/16 笔 ratio 落在 **[0.93, 1.04]** 极窄带，门 `ratio<1.0` 正切在带**中央**。敏感性实测：边界 0.93→noise 0%、0.95→12.5%、0.98→37.5%（均 FAIL）；**1.00→75.0%（浮点）/ 68.8%（Decimal 严格）**；1.02→87.5%、1.04→93.8%（PASS）。三口径分叉根因 = SAIC `dist==atr` 精确相等（浮点 x/x 可得 0.9999…）。禁改条款只写"ratio 边界 1.0 / noise 定义"，**未写比较精度与舍入规则** ⇒ 结论由小数第二位决定。建议补一句"ratio 用 Decimal 严格比较，`<1.0` 不含相等"（结论 PASS 不变，但口径须先写死）。
- **归因发现本身很硬**：15/16 笔贴 1×ATR（0.93–1.04）说明止损是**系统性按 1×ATR 放置**（非逐票定制），这正是 DA 笔记挑战者多次点名的"止损距离 < 单日振幅"结构问题的量化实锤。零攒样、一次读数、成本最低 ⇒ 三份新案中**性价比最高**，建议修完上述两条判据缺陷后优先做。

## 八、Codex 复核修正（2026-09-28；原判据暂停，不就地挪门柱）

- **§15.4 “Fisher 数学死门”证伪**：`fisher_exact([[12,0],[0,4]])` 双侧 p=**0.000549**，不是 1.0；p=1.0 的表是 `[[12,0],[4,0]]`。按旧 `high≥entry` 但修正“事件先于删失则计入”的口径，严格 Decimal 分类触碰表为 `[[10,1],[3,2]]`，p=**0.214**。正确问题是当前功效不足，不是数学不可达。
- **严格 Decimal 分类修正**：`ratio<1.0` 且不含相等时为 **noise 11 笔 / breakdown 5 笔**；SAIC `dist==atr` 归 breakdown。§15.5 的浮点 12/16、Decimal 11/16 只适用于边界敏感性，不应再把 SAIC 计入 noise 后写成 12/4 分桶。
- **🔴 新发现：回翻方向语义反了**：`scripts/loss_exit_recovery_check.py` 对 LONG/SHORT 一律用 `high≥entry`。本 16 笔中 6 笔 SHORT 全部因此旧 `ttr_touch=1`；SHORT 的回翻应测 `low≤entry`。按严格 Decimal 分桶和“已观察事件先于删失则计入，未事件且窗口<5 才剔除”重算：noise ≤5 日触碰 **6/11=54.5%**，breakdown **3/5=60.0%**，Fisher p=**1.000**。原“noise 100% vs breakdown 66.7%”不可用。
- **全量底牌同步降级**：8/1 起 24 笔 SL_INITIAL 在正典删失口径下，方向感知 5 日触碰为 **11/23=47.8%**、20 日 **14/19=73.7%**。底牌2 的“59%/38%”只保留为错误口径历史，不得再引用。
- **处置**：本 DEB 的触碰率半场和 §十五建议排序作废；不把判据改成“效应量+CI”来迁就修正后读数（那是看到数据后挪门柱）。当前只保留 15/16 贴 1×ATR 的几何事实；若继续验证，需先修方向感知测量并另立/重裁前向判据。

## 九、第三方复查追加（2026-09-28，kimi-k3 独立重跑，不引用前文数字；判据原文未动）

- **既有结论 CONFIRMED**：16 笔 −$2,080.86、LLY 离群 16.17、7/16 basis 缺失、15/16 贴 1×ATR 带（§15.1 的 SAIC=1.000 修正对）、严格 Decimal 分桶 noise 11/breakdown 5、6 笔 SHORT——逐位吻合。Fisher 修正独立复算：`fisher_exact([[12,0],[0,4]])` 双侧 p=**0.000549**（§16.2 对，§15.4"死门"确为算术错误，返回 1.0 的退化表是 `[[12,0],[4,0]]`）。
- **🔴 新发现 1（可复现性缺陷）**：用本 DEB 附录脚本原样复跑，方向感知表 = **noise 6/10=60.0% vs breakdown 3/5=60.0%（p=1.0）**，与 §16.3 的 6/11=54.5% **差 1 行删失判定**——STX（9/24 离场）在缓存中仅 2 根 post bar（9/25、9/28），按"未事件且窗口<5 剔除"应删，§16.3 计入了。且 `data/replay_bars/postexit_*` 缓存**每晨刷新**（mtime 09-28 05:07）⇒ 同一脚本同一判据、不同日期跑出不同读数。**"一次读数"设计必须钉死缓存快照日期 + 删失边界写成精确不等式**，否则复查者永远对不上。
- **🔴 新发现 2（活地雷）**：两个复查提交（f59a8e6ca/fe3cdd926）均为纯文档，`scripts/loss_exit_recovery_check.py:103-110` 的方向语义 bug **原样未修**（LONG/SHORT 一律 `high≥entry`，无 direction 分支）。父 DEB 底牌7 引用的是修正后数字，任何人重跑该脚本会再产出旧错误数字——正是"防第三套数字"纪律要防的事，且该脚本已被多个底牌引用。**建议立即修脚本（只读分析工具、零生产行为）+ 文件头标注口径变更 + 重跑一次生成正典产物**。
- **🟡 比 §16.4 更强的结论**：修正方向语义后**点估计已反对原假设**——noise 桶触碰率 54.5–60.0%（删失口径 wobble）≤ breakdown 60.0%，两个口径下都不存在"noise 桶更常回翻"。15/16 贴 1×ATR 的几何事实为真，但它本身不蕴含"过紧"——没有恢复率差，就没有钱留在桌上。**建议路径收紧为：修脚本 → 钉缓存快照重算一次 → 若 noise ≤ breakdown 维持，直接结题**（"1×ATR 对日内噪音过紧"判为无证据），不另立前向攒样——16 笔历史样本点不出方向的假设，前向攒样先验不值得占影子位。
- **杂项**：LLY ratio 16.17（stop 距 50.61 vs atr 3.13、basis 缺失）单列**数据质量 ticket**，不进几何结论；桶名 `noise`/`breakdown` 预支结论（"是否噪音"正是测量未能证明的事），建议改 `tight`/`wide`。

## 十、第四轮作者复核追加（2026-09-28，详见父 DEB §十八；判据原文未动）

**测量缺陷已修复**（`scripts/loss_exit_recovery_check.py`，方向感知 + 删失规则统一 + 快照字段），修后重算：

| 口径 | noise | breakdown | Fisher p |
|---|---|---|---|
| ttr_touch ≤5日（统一删失规则） | **6/10 = 60.0%** | **3/5 = 60.0%** | **1.0000** |
| ttr_be_arm ≤5日 | **6/10 = 60.0%** | **3/5 = 60.0%** | **1.0000** |

三方读数（Codex §16.3 = 6/11 vs 3/5、kimi §17.4 = 6/10 vs 3/5、本轮 = 6/10 vs 3/5）差异已定位为 **STX（win=2、SHORT、未触发）的删失归属**，结论完全一致。

**⇒ 归因假设不成立，建议直接结题**：底牌 4 的"noise 62.5%"是 LONG 语义污染值；方向感知后 **noise 桶回翻率并不高于 breakdown 桶**（60.0% = 60.0%），判据第一条（noise≥50%）勉强达标但**第二条（noise 显著高于 breakdown）方向反了**。§六自曝 3 提出的"1×ATR 对日内噪音是否过紧"这一精化问法，**答案是：数据不支持**。

**同时撤回父 DEB §五 B2 行的衰减叙事**：SL_INITIAL ≤5日触碰 **11/23=47.8%**、≤20日 **14/19=73.7%** ⇒ 不存在衰减形态，"回翻的快、不回的死 ⇒ 彩票分布"不成立，B2 已降级为观察项。

## 十一、Codex 复核修正（2026-09-28，第五轮）

- **§十声称“删失规则统一”但脚本未落地**：`agg()` 仍用 `win_avail>=k`，会剔除已确凿触发但窗口不足的行。已改为“事件已发生即计入；未发生且窗口<k才剔除”，并在产物写入 `censor_rule=event_first_else_window`。
- **正典读数**：16 笔当前窗口内 noise **6/10=60.0%**、breakdown **3/5=60.0%**、Fisher p=**1.000**；全量 24 笔 SL_INITIAL 触碰 ≤5日 **11/23=47.8%**、≤20日 **14/19=73.7%**。
- **结论不变但理由更正**：原假设“1×ATR 对日内噪音过紧”仍不成立；B2 的旧 `3/8=37.5%` 无法从正典产物复现，衰减叙事作废。

**保留的硬发现**（不受测量缺陷影响）：15/16 笔 ratio 落在 [0.93, 1.04] ⇒ 止损**系统性按 1×ATR 放置**（非逐票定制）这一事实成立，只是它**没有造成可测的噪音误伤**。⇒ 后续若要动止损几何，需另找论据（如 MAE 分布 vs ATR 的错配），不能用本归因。

**数据质量 ticket**（独立于本 DEB 结论）：LLY id=1413 `atr=3.13` vs `|entry−stop|=50.61`（ratio 16.17）、entry_price=$1,177.50 ⇒ ATR 疑似未按同一标的/复权口径落库（该股价格量级与 ATR 量级差 375 倍）。建议查 ATR 写入侧是否混用了不同 symbol 或前复权/不复权口径。

**判据精度补写建议**（§15.5，仍成立）：ratio 边界须写死"Decimal 严格比较，`<1.0` 不含相等"——SAIC `dist == atr` 精确相等那笔在浮点下会被判 noise、Decimal 下判 breakdown，是 11/5 与 12/4 两种分桶的根源。

## 附：可复现命令

```bash
sqlite3 -header -column "file:data/ftseek_us.db?mode=ro" "
SELECT symbol, entry_price, json_extract(metadata_json,'$.atr') atr,
json_extract(metadata_json,'$.atr_basis') basis,
json_extract(metadata_json,'$.stop_loss') stop,
ROUND(ABS(entry_price-json_extract(metadata_json,'$.stop_loss'))
      / json_extract(metadata_json,'$.atr'),2) ratio
FROM trade_records WHERE date(entry_time)>='2026-08-25' AND exit_time IS NOT NULL
AND exit_reason='SL_INITIAL'
AND CAST(julianday(date(exit_time))-julianday(date(entry_time)) AS INT)=0
ORDER BY date(entry_time);"
```

方向感知复核（2026-09-28，只读 DB + 既有日K缓存）：

```bash
./venv/bin/python - <<'PY'
import glob, json, sqlite3
from decimal import Decimal
from scipy.stats import fisher_exact

ids = [1391,1394,1405,1407,1408,1413,1418,1434,1448,1449,
       1460,1476,1499,1538,1558,1564]
con = sqlite3.connect("file:data/ftseek_us.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(f"""
SELECT id,symbol,direction,entry_price,exit_time,
       json_extract(metadata_json,'$.atr') atr,
       json_extract(metadata_json,'$.stop_loss') stop
FROM trade_records WHERE id IN ({','.join('?' * len(ids))})
""", ids).fetchall()
cache = {p.split("postexit_")[1].split("_2026")[0]: json.load(open(p))
         for p in glob.glob("data/replay_bars/postexit_*")}

def table(direction_aware):
    buckets = {"noise": [0, 0], "breakdown": [0, 0]}
    for row in rows:
        ratio = (abs(Decimal(str(row["entry_price"])) - Decimal(str(row["stop"])))
                 / Decimal(str(row["atr"])))
        bucket = "noise" if ratio < 1 else "breakdown"
        post = [b for b in cache[row["symbol"].replace(".", "_")]
                if b[0] > row["exit_time"][:10]]
        entry = float(row["entry_price"])
        if direction_aware and row["direction"] == "SHORT":
            touch = next((i for i, b in enumerate(post, 1) if b[3] <= entry), None)
        else:
            touch = next((i for i, b in enumerate(post, 1) if b[2] >= entry), None)
        valid = touch is not None or len(post) >= 5
        hit = touch is not None and touch <= 5
        if valid:
            buckets[bucket][0 if hit else 1] += 1
    matrix = [buckets["noise"], buckets["breakdown"]]
    return matrix, fisher_exact(matrix).pvalue

print("old-direction:", table(False))
print("direction-aware:", table(True))
print("extreme:", fisher_exact([[12, 0], [0, 4]]).pvalue)
PY
```
