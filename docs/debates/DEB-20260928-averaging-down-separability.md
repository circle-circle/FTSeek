# DEB-20260928 · 浮亏补仓（摊薄均价）可分离性研究 → 条件影子

- **立案日**: 2026-09-28
- **发起人**: 用户（"对于亏损的标的股，是否值得在合适的价格进行补仓，降低均价？这个问题和金字塔影子类似，需要分析是否值得"）
- **状态**: **提案（待用户裁）**；Phase 0 只读分析，零实盘行为变更、零配置变更、零 cron 变更
- **类型**: 三段式（可分离性归因 → 条件前向影子 → 机制 DEB），同 DEB③ 止损几何的纪律
- **来源**: 父 DEB-20260928（底牌7 回翻分层）+ 本会话一阶反事实测算（§二）
- **关联**: 判据字典 C4（影子先行）/ C8（禁方向否决门——边界论证见 §五 R4）/ DEB-20260808（每两周 1 个 A/B 变量）/ DEB① 金字塔（镜像：赢家加仓）；同轴影子枚举见 §三（P0.5，template.md 新规首个执行案例）

---

## 一、问题界定

金字塔（DEB①）问"给赢家加码"，本案问镜像问题：**持仓浮亏到 −k% 时，等量/定量补仓摊薄均价，扣费后是否为正期望？**
经典风险是 martingale（赢小输大、尾部加倍）。本案**不预设立场**，先回答一个更便宜的前置问题：
**在浮亏 −k% 的当下，是否存在可在线获得的特征，把"会回来的"与"会砸穿的"分开？**
分不开 ⇒ 补仓永久结案；分得开 ⇒ 才谈 Phase 1 影子。

## 二、底牌清单（一阶反事实已先行披露，全部实测可复现）

口径声明：人群=8/25 起已平仓 207 笔（mae_pct 有效覆盖 136/207=65.7%，20s 轮询采样，瞬时触及漏记 ⇒ 触发数偏低估，方向保守）；虚拟腿=浮亏首触 −k% 时**等量补仓**，滑点 0.10% 方向感知 + 费 0.15% 往返；**退出时机不变假设**（母仓按原规则退出——现实中补仓会改均价/BE 线/退出时点，这是固有失真，§五 R1）。

| # | 事实 | 读数 | 证据锚 |
|---|---|---|---|
| 1 | 三地带形态 | k=0.5%: 90 触发 Σ腿 **−$1,657**（胜率 68% 仍亏，martingale 指纹）; k=1.0%: 75 笔 Σ−$36; k=1.5%: 50 笔 Σ+$236; **k=2.0%: 39 笔 Σ+$618（中位+$51/均值+$16）**; k=3.0%: 19 笔中位 **−$23**（转彩票） | §附脚本1 |
| 2 | **k=2% 腿盈亏按母仓退出原因完全分家** | 回翻类：TRAIL_LOCK 6 笔 +$1,175 / DEAD_SIGNAL 7 笔 +$704 / BREAKEVEN 7 笔 +$608 / lock_in 1 +$228（合计 **+$2,716**）；真跌类：SL_INITIAL 11 笔 **−$927** / OVERNIGHT_GAP 5 笔 **−$1,106** / tighten 2 −$64（合计 **−$2,097**） | §附脚本2 |
| 3 | 日聚类后期望很薄 | k=2% 39 笔分布 20 个交易日：盈利日 **10/20**，日Σ中位 +$26，最差日 **−$360（8/28）** | §附脚本2 |
| 4 | 🔴 **帽内可执行性（与 DEB① §15.6 同构但更严重）** | 触发仓名义均值 $5,396 vs 单名帽 $5,919（净值 $98,655×6%）：**等量补仓仅 4/39 可行**，帽内可补比例均值 **14.5%** ⇒ 可执行口径下期望 +$618×14.5% ≈ **+$90/5周 ≈ $18/周** | §附脚本3 |
| 5 | 同源机制的价值下锚 | `exit_disable_cf`（"不砍"反事实，同一批回归仓位）：saved 中位 0.22%/笔 ≈ $8，且 saved 均值在费用线下（父 DEB §十一 S7 已降级）——"不砍"都只值 $8/笔，加码版合理上限高不到哪去 | `data/shadow/exit_disable_cf_latest.json` |
| 6 | 背景分母 | k=2% 触发集 39 笔的母仓合计亏 **−$3,286**；腿 +$618 只回补 19% | §附脚本2 |
| 7 | 隔夜叠加禁忌 | 回翻类里的隔夜跳空子集 5 笔腿 −$1,106 ⇒ **补仓持有过夜=主动加仓进全系统唯一出血桶**（父 DEB 表A：隔夜 −$1,345=全部净亏来源） | 父 DEB §四 |

## 三、P0.5 同轴既有影子枚举（template.md 新规首个执行案例，注册表 63 项全扫）

| 既有载体 | 它管什么 | 与本轴的关系 |
|---|---|---|
| `penetration_shadow`（OBSERVING） | 保本线穿透判别器（快跌 vs V回），触发瞬间 9 维特征已采集 + B17 预注册判据 | **最近同轴但不重复**：它的人群=已到保本线的出场仓，本案=浮亏中持仓。**其特征清单（穿透深度/自峰回吐/是否刷新低/持仓时长/ATR 归一/路径）直接复用为本案 Phase 0 候选特征** |
| `exit_disable_cf`（OBSERVING） | DEAD_SIGNAL/MFE_STALL "不砍"反事实 | 同源假设反方向（不砍 vs 加码）；价值下锚见底牌5；**毕业判据须扣减两者重叠**（同一批回归仓位会被两机制重复计数） |
| `post_exit_path`（OBSERVING） | 全量平仓单出场后全天路径；计划产出"深回撤 V 反日 vs 平静日"分层 | Phase 0 的 V 反上下文特征可直接 join 其产物 |
| `early_dead_signal_shadow`（OBSERVING） | 15/30min MFE 快照识别死单 | 候选特征载体（早期 MFE 形态） |
| `overnight_known_event_flag`（OBSERVING） | 隔夜已知事件日历（9/24 起攒样，现仅 5 行） | **首选分离特征候选**（事件日浮亏不补 vs 非事件日可补），但历史窗覆盖不足 ⇒ 前向攒 |
| `eod_depth_gate` / `ticket_size_breakeven` / `trail_lock_pyramid` | B19 深水闸 / 费用地板 / 赢家加仓 | 相邻不同轴；金字塔与本案同属"加仓类"，若都毕业上 sim 须按 DEB-20260808 排队（金字塔在先） |

⇒ 无既有载体覆盖"浮亏中加仓反事实"，**增量成立**；且 Phase 0 特征应尽量 join 既有产物，不新建采集。

## 四、Phase 0 设计（只读归因，一次性读出）

**结局定义（预注册）**：虚拟补仓腿净额（§二口径）>0 = 回翻类；≤0 = 恶化类。
**特征候选清单（预注册，触发时刻全部在线可得，禁未来数据）**：

- F1 已知事件日历（当日/次日；join `overnight_known_event_flag` 产物，历史窗覆盖不足则如实标注留前向）
- F2 持仓龄桶（当日浮亏 vs 隔夜浮亏；entry_time 与交易日历来分）
- F3 大盘当日方向（QQQ 当日涨跌符号；data/replay_bars 日K）
- F4 入场 VIX 档（trade_records.entry_vix 列，分三档）
- F5 价格/费用档（<$20 / $20–100 / ≥$100；DEB② 已证无残余轴，作对照列）
- F6 入场时段（早盘/午盘/尾盘，entry_time ET 小时）
- F7 信号源与强度（source_type × signal_strength 档）

**评估口径**：逐特征分桶比桶间腿胜率差，报效应量 + Wilson 95%CI；**不设 Fisher 显著性死门**（DEB③ §15.4 教训：小样本下"显著"数学不可达，不是攒样能救的，是尺子选错了）。

## 五、反方观点

| # | 反方论据 | 回应 | 残余风险 |
|---|---|---|---|
| R1 | "退出时机不变"假设失真：真实补仓改变均价 ⇒ BE 线/trail/退出时点全变 | Phase 0 只是方向筛查；Phase 1 若立项用实际轨迹（复用金字塔 hook 模式），不用该假设 | 中 |
| R2 | 9 月上行期顺风：均值回翻策略在顺风 regime 天然虚高 | 底牌全部标注；Phase 1 前向窗跨 regime；最差日 −$360（8/28）已是逆风样本点 | 高 |
| R3 | martingale 尾部：p10 −$202/笔，是中位 +$51 的 4 倍；k=0.5% 已实证 68% 胜率仍亏 | 任何后续设计判据**必须含 p10 尾部硬指标**，不许只看中位/均值；底牌1 即反面教材存档 | 高（本假设核心风险） |
| R4 | C8 边界滑坡：分离特征若用于"拦入场"就违反 C8 | 本案是**持仓侧加仓许可**，不否决任何已有信号/仓位；若任何特征被提议用于入场否决 = 越界，届时另立 DEB + 用户裁 + 知情破例记录（同 DEB② §三论证结构） | 低（治理设计内） |
| R5 | 与 `exit_disable_cf` 重复计数同一批回归仓位 | 毕业判据扣减重叠；且其价值下锚 $8/笔 已在底牌5 压低预期 | 中 |
| R6 | 帽约束使期望只剩 $18/周（底牌4） | 正是"先 Phase 0 后影子"的理由：可执行上限这么薄，唯一值得研究的是"能否选出该补的子集"；选不出就没有任何落地形态 | 高 |
| R7 | mae_pct 20s 采样低估瞬时触及 | 触发数偏保守（低估），方向不夸大结论 | 低 |

## 六、预注册判据（读数前写死，死线 2026-12-31）

```
averaging_down Phase 0 毕业门 (只读, 一次读出):
  对 k=2.0% 触发集 (8/25窗 39 笔 + 读出前自然新增), 逐特征 (F1–F7) 分桶:
    至少一个特征存在桶间腿胜率差 ≥20pp
    ∧ Wilson 95%CI 方向一致 (不跨 0)
    ∧ 较优桶 n≥15
    ∧ 该特征触发时刻在线可得 (无未来数据)
  → 通过: 另立 Phase 1 影子 DEB (record-only 浮亏触发采集, 复用金字塔 hook 模式;
          三硬约束写死: ①当日 EOD 必平不隔夜 ②帽内 add_ratio 结算不突破 6% 单名帽
          ③判据含 p10 尾部硬指标; k=2.0% 单值冻结)
  → 不通过: 结题 ("两类不可分 ⇒ 不补"), 话题归档, 不再复活。
禁改: k=2.0% 单值 / 结局定义 (腿净额>0) / 特征清单 F1–F7 / 20pp+Wilson 口径 — 改=重裁。
```

**治理**：Phase 0 只读零行为，不占 A/B 预算；若进 Phase 1 影子（record-only）仍不占预算，但需回答"与金字塔影子并列的审查带宽"（现役 63 项注册影子）。

## 七、边界 / 验证 / 回滚

- 本文档零代码零配置零 cron；Phase 0 落地=一个只读分析脚本（届时随 DEB 裁决一并评审）。
- 回滚：整文件 revert，无运行时依赖。
- 自曝 1：**k 网格读数先于立案披露**（0.5/1.0/1.5/2.0/3.0 五档已跑）——k=2.0 的单值选定**受读数影响**（登记为知情选择：浅层负 Σ、深层中位转负，2.0 是唯一结构与费用平衡的档位）；Phase 1 若立项不得再调 k。
- 自曝 2：底牌 1–4 全部来自同一批 207 笔历史数据——按"同一批数据不双花"纪律，**只作立项动机，不进任何判据**；Phase 0 读出允许用同窗（假设生成），但任何入选特征的前向确认必须用读出后的新增样本。
- 自曝 3：我口头初判"中补（1.5–2%）有正期望迹象"——日聚类后盈利日仅 10/20、帽内可执行后仅 $18/周，收敛表述为"**有条件正期望、条件一阶不可在线判定、可执行上限很薄**"。

## 附：可复现命令

```bash
# 脚本1+2: 三地带/exit_reason分解/日聚类 (只读, 2026-09-28 实跑)
./venv/bin/python - <<'PY'
import json, sqlite3
from collections import defaultdict
con = sqlite3.connect("file:data/ftseek_us.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
SELECT id, symbol, direction, entry_price, exit_price, quantity, pnl_net, exit_reason,
       date(entry_time) d, json_extract(metadata_json,'$.mae_pct') mae
FROM trade_records WHERE date(entry_time)>='2026-08-25' AND exit_time IS NOT NULL
AND pnl_net IS NOT NULL AND exit_price > 0""").fetchall()
SLIP, FEE_RT = 0.001, 0.0015
def leg(r, k):
    if (r["mae"] or 0) < k: return None
    sgn = 1.0 if r["direction"]=="LONG" else -1.0
    add_px = r["entry_price"]*(1-sgn*k/100)*(1+sgn*SLIP)
    n = add_px*r["quantity"]
    return (sgn*(r["exit_price"]/add_px-1)-FEE_RT)*n
for k in (0.5,1.0,1.5,2.0,3.0):
    legs=[leg(r,k) for r in rows]; legs=[x for x in legs if x is not None]
    if legs:
        s=sorted(legs); n=len(s)
        print(f"k={k}%: n={n} Σ={sum(s):+.2f} 中位={s[n//2]:+.2f} 均值={sum(s)/n:+.2f} "
              f"胜率={sum(1 for x in s if x>0)/n*100:.0f}% p10={s[int(n*0.1)]:+.2f}")
# k=2% exit_reason 分解 + 日聚类
g=defaultdict(list); by_day=defaultdict(float)
for r in rows:
    v=leg(r,2.0)
    if v is not None: g[r["exit_reason"]].append(v); by_day[r["d"]]+=v
for reason,vals in sorted(g.items(),key=lambda kv:sum(kv[1])):
    print(f"  {reason}: n={len(vals)} Σ={sum(vals):+.2f}")
sums=list(by_day.values())
print(f"日聚类: {len(sums)}天 盈利日{sum(1 for x in sums if x>0)}/{len(sums)} 最差{min(sums):+.2f}")
PY

# 脚本3: 帽内可执行性 (等量补仓需要100%余量)
./venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("file:data/ftseek_us.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT entry_price*quantity n,
json_extract(metadata_json,'$.mae_pct') mae FROM trade_records
WHERE date(entry_time)>='2026-08-25' AND exit_time IS NOT NULL
AND pnl_net IS NOT NULL AND exit_price > 0""").fetchall()
CAP = 98654.638 * 0.06   # 2026-09-28 capital_state 快照 × 单名帽
for k in (1.5, 2.0):
    t=[r["n"] for r in rows if (r["mae"] or 0)>=k]
    full=sum(1 for x in t if x*2<=CAP)
    avg=sum(max(0.0,min(1.0,(CAP-x)/x)) for x in t)/len(t)
    print(f"k={k}%: {len(t)}笔 等量可行{full} 帽内可补比例均值{avg*100:.1f}%")
# 期望(2026-09-28): k=1.5% 50笔 可行5 14.7% / k=2.0% 39笔 可行4 14.5%
PY
```
