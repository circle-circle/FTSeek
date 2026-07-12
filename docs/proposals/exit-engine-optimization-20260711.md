# 退出引擎优化方案 (2026-07-11)

> 触发: 用户直觉"到点就走·不看逻辑过于粗暴, 影响收益和胜率"。
> 调查后结论: **该直觉指向的表象(时钟退出)其实在赚钱, 真正的弱点在别处。**
> 本文档先纠正一个我方引入的可视化归类 bug, 再给出数据指向的四块优化。
> 落地方式: 用户已确认"先出方案, 审阅后再实施"。**本文档不含代码改动。**
> 所有操作在富途模拟盘, 风险可控; 仍遵循"修复打时间戳、验证卡 cutoff"纪律 (见
> decision-chain-single-brain-cleanup-20260711.md §8)。

---

## 0. 先纠错: "逻辑类仅 7%/止盈仅 1 次"是归类 bug 造的假象

上一轮退出画像 (`app/web_dashboard/routes/chain.py` L361-367) 的 `CAT` 映射太窄,
`logic`(止盈)类只认了 `TAKE_PROFIT`, 漏掉了实际主力止盈标签。近 60 天真实数据:

| 主动止盈标签 | 笔数 | 胜率 | 均收益% |
|---|---|---|---|
| DAY_TRADE_TP | 13 | 13/13 | +0.71 |
| SCALE_OUT (分批) | 4 | 4/4 | +2.03 |
| TAKE_PROFIT | 1 | 1/1 | +3.82 |
| ORB_TAKE_PROFIT | 1 | 1/1 | +0.45 |

**主动止盈实际约 19 笔、几乎全盈利**, 不是"仅 7%/1 次"。系统的止盈能力健康得多。
"止盈<5%"告警也是假的 (take_profit_n 只数了 TAKE_PROFIT)。这个错误直接影响了对退出
系统健康度的判断, 必须先修 (见 §3)。

---

## 1. 修正后的真实退出图景 (近 60 天, 排除 shadow/scalp)

退出引擎其实**相当成熟**, 6 级优先级链 (pipeline.py L3028-3110):
`SL(开盘15min放宽4%) → TAKE_PROFIT(RR≥1.5门禁) → 隔夜跳空 → PRE_CLOSE(盈利仓cohortB可持过夜)
→ TIME_EXIT(仅砍亏损) → MAX_HOLD`, 外加 breakeven + trailing 动态锁利。

关键事实 (被数据澄清):
- **系统是纯日内**: executed 单 horizon 全是 short (83/83), DA 建议持有均 4.3h。
  当天 PRE_CLOSE/EOD 平仓**符合 DA 意图**, 非执行脑粗暴。
- **到点类其实赚钱**: PRE_CLOSE +1.44% (12/22胜)、EOD +0.98% (5/7胜)、DAY_TRADE_TP +0.71%。
- **TIME_EXIT 全亏 (7/7)、SL 全亏**是对的 — 它们的职责就是砍亏损仓。

### 数据真正指向的弱点

| 弱点 | 证据 | 归属工作块 |
|---|---|---|
| **TRAILING_STOP 系统性亏损** | 22 笔、仅 8 胜、均 **-1.32%** — 唯一"主动决策却亏钱"的退出 | §2 |
| **MFE/MAE 数据坏** | 122 笔中 mfe_pct 87 笔=0、14 笔脏(>100%), 仅 21 笔合理 | §1-fix |
| **退出画像归类错** | logic 类漏标 (§0) | §3 |
| **invalidation 只存不用** | thesis 失效不驱动退出, 唯一缺失的"逻辑腿" | §4 |

---

## 2. 工作块 A — 修 MFE/MAE 数据管道 (地基, 最高优先)

**为什么最先做**: TRAILING_STOP 优化 (§2工作块B) 的唯一反馈依据就是"盘中峰值 vs 最终
落袋"(giveback)。MFE 坏 → trailing 任何调参都是盲调。edge_weekly_report.py L312 已挂
"⚠️ mfe_pct 大面积失效 → 阻塞 MFE 类退出调优"。

### 现状 (已调查)
- **go-forward 反算已存在** (pipeline.py L2688-2715, 2026-07-10): mfe_pct 缺失时用持久化
  的 `metadata.peak_price` 反算 `(peak-entry)/entry*100`, 且兜底 `mfe ≥ pnl_pct*100`。
- 但存量 71% 仍为 0 — 该修复只对**修复后新平仓**的单生效。
- **`mae_pct` 完全没有反算逻辑** — 只从 metadata 读 (L2698), 而 metadata 几乎不写,
  残留脏值 (NU 111.27、GOOG 98.51, 不可能是日内回撤%)。
- **无量纲夹取** — 脏值 >100% 直接落库污染 giveback 计算。

### 方案
1. **存量回填脚本** `scripts/backfill_mfe_mae.py`:
   - 遍历 trade_records 已平仓单, 读 `metadata_json.peak_price` + entry_price + direction,
     用与 pipeline L2705-2710 **完全相同**的公式反算 mfe_pct; 兜底 `mfe ≥ pnl_pct*100`。
   - metadata 无 peak_price 的单 → mfe_pct 置 NULL (诚实标记"无数据", 不塞 0 冒充)。
   - 幂等: 只在 `mfe_pct IS NULL OR mfe_pct=0 OR mfe_pct>阈值` 时覆盖。
2. **量纲夹取** (回填 + go-forward 两处): 日内 MFE/MAE 合理上限约 30%, 超过判脏 → 置 NULL,
   不夹到边界值 (避免假数据)。参照 `_norm_conf` 的思路但**坏值置 NULL 而非截断**。
3. **mae_pct 对称反算**: 确认 metadata 是否持久化 `trough_price`(最低价)。
   - 若有 → 对称反算 `(entry-trough)/entry*100` (LONG)。
   - 若无 → mae 只能 go-forward 采集: 在 monitor 循环持久化 trough_price (与 peak_price 对称),
     存量 mae 保持 NULL (诚实)。
4. **健康探针**: check_decision_chain.py 加 `--mfe-health` — 报 mfe_pct 的 NULL/0/脏/合理占比,
   go-forward 新单 mfe 覆盖率作为验证信号。

### 验证
- 回填后 mfe_pct 合理占比从 ~17% 提升; 盈利单 mfe ≥ pnl 恒成立 (定义自洽)。
- go-forward: cutoff 后新平仓单 mfe_pct>0 占比 ≥ 90% (排除真·秒杀 MFE=0)。

### 风险
- 低。纯归因字段, 不碰交易热路径。回填幂等且只覆盖坏值。坏值置 NULL 是保守选择。

---

## 3. 工作块 B — 优化 TRAILING_STOP 质量

**前置依赖 §2工作块A** (需可信 MFE 才能判断 trailing 是"锁太松"还是"太紧")。

### 现状
- 22 笔均 -1.32%、仅 8 胜。trailing 由 `_compute_dynamic_effective_sl` (pipeline L2873)
  实现: `breakeven_at_pct` 保本 + `drawdown_from_peak_pct` 从峰值回撤锁利。
- 参数在 `config/playbook_horizon.yaml factor_exit_rules` (default: breakeven 1.0 /
  drawdown 0.5, 已于 7/9 收紧过一轮)。

### 诊断方法 (拿到可信 MFE 后)
对这 22 笔逐笔算三元组 `(peak_profit_pct, drawdown_触发点, 最终pnl)`, 区分两种病理:
- **锁太松 (giveback 型)**: peak 高 (如 +2%) 但 drawdown 0.5% 才触发, 回吐大 → 收紧 drawdown。
- **太紧 (震荡扫出型)**: peak 低 (如 +0.6%), breakeven/trailing 在正常波动里被扫, 之后价格
  反而继续走 → 放宽 breakeven 激活点 或加 ATR 缓冲。
- 两种病理**方向相反**, 必须用数据分辨, 不能拍脑袋二选一。

### 方案 (数据分辨后择一/分因子)
- 按 factor 类型差异化 (trend/momentum 让利润跑, 用宽 drawdown; mean-reversion 快锁)。
- 候选参数: breakeven_at_pct、drawdown_from_peak_pct、trailing 激活的 ATR 缓冲。
- **模拟盘 A/B**: 改参数打时间戳, 只用 cutoff 后新单验证 (呼应 §8 纪律), 样本 <30 只看结构。

### 风险
- 中。改 trailing 参数直接影响模拟盘退出行为。用 factor_exit_rules 分因子、小步调、
  cutoff 后验证。保留改前参数快照以便回退。

---

## 4. 工作块 C — 修退出画像归类 bug (可视化纠错, 低风险)

### 方案 (chain.py exit-profile 端点)
1. `CAT` 映射补全:
   - `logic`(建议改名 `profit`) 补: `DAY_TRADE_TP`、`SCALE_OUT`、`ORB_TAKE_PROFIT`、
     `SCALE_OUT_ghost_cleanup`。
   - `ORB_BREAKDOWN` 从 logic **移到 stop** (破位是止损, 非止盈)。
   - `TRAILING_STOP` 建议**单列一类** `trailing`(意图锁利但当前系统性亏损, 值得独立盯),
     或保留 stop 但前端标注。
2. `take_profit_n` 改为统计所有止盈类标签之和 (不止 TAKE_PROFIT)。
3. "止盈<5%"告警阈值随之复核 (真实止盈占比约 15%)。
4. 前端 `loadExitProfile` categories 是动态渲染, 后端加类别可自动显示; 复核 epBar 配色。

### 验证
- 重跑 exit-profile 端点, profit 类应显示 ~19 笔且高胜率; TestClient + 重启 dashboard。

### 风险
- 极低。纯可视化/归类, 不碰交易与 PnL。

---

## 5. 工作块 D — 引入 invalidation 驱动退出 (Stage 2)

### 现状
- `playbook_instructions.invalidation` 已落库 (Stage 1), 单笔 trace 已可读展示, 但**不驱动
  退出** — thesis 失效时系统不会主动离场。

### 依赖与分期 (关键: 需 Hermes 侧配合)
- **前提**: 当前 invalidation 是**自然语言文本** (如"跌破关键支撑/基本面恶化"), 机器无法判定。
  需 Hermes 侧把 invalidation **结构化**成可判定条件 (price < X / RSI > Y / vol < Z / news_flag)。
  纯 repo 侧无法完成 (DA 脑是外部 skill, 见 §决策链文档)。
- **Stage 2a (repo, 可先做)**: 退出引擎加 `INVALIDATION_EXIT` 规则骨架 + **影子触发**
  (shadow, 不实平), 记录"若 invalidation 命中会何时离场", 与实际退出对比累积样本。
- **Stage 2b (需 Hermes)**: invalidation 结构化契约落地后, 影子→实盘灰度。
- 优先级排在 A/B/C 之后 (依赖外部, 且需先有 MFE 反馈闭环判断其增益)。

### 风险
- 中高 (未来实盘阶段)。影子阶段零风险。结构化契约未定前不启用实平。

---

## 6. 实施顺序与依赖

```
A(MFE/MAE 数据)  ──┐
                   ├──► B(TRAILING_STOP 调参, 依赖 A 的可信数据)
C(归类 bug, 独立) ──┘
D(invalidation, 依赖 Hermes + A 的反馈闭环) ── 最后
```

建议批次:
1. **批次 1 (地基+纠错, 低风险)**: A 的回填+夹取+健康探针 + C 的归类修复。一起验证、重启 dashboard。
2. **批次 2 (调参, 中风险)**: 拿批次1的可信 MFE, 诊断 22 笔 trailing, 分因子小步调 B。
3. **批次 3 (Stage 2a 影子)**: invalidation 影子触发骨架, 累积样本; Stage 2b 等 Hermes。

## 7. 验证纪律 (贯穿)
- 所有效果验证卡 `date >= cutoff` (修复打时间戳当日), 不用跨口径旧数据下结论。
- 样本 <30 (trailing) / <50 (校准) 只看结构不下结论。
- 参数改动前存快照, 保留回退路径。模拟盘不因凑样本放松风控。

## 8. 待用户确认项
- 批次 1 是否现在开工 (A+C, 低风险, 我方引入的 bug 建议尽快修)。
- TRAILING_STOP 单列成独立画像类别, 还是并入 stop 加标注。
- Stage 2a 影子骨架是否本轮先搭 (不依赖 Hermes, 但价值要等结构化契约才释放)。

---

## 9. 落地记录 (✅ 2026-07-11, 批次1 A+C + Stage 2a)

用户确认: ①现在开工 ②TRAILING 并入止损类 ③Stage 2a 先搭。

### 勘误 (诚实): "MFE 存量脏值 >100%" 是查询错误, 非数据脏
§1/§2 曾称"14 笔 mfe_pct 脏值 >100%"。核实: mfe_pct 列 **max=3.82, 无 >50 的脏值**。
根因是我做 giveback 分析时写了 `mfe_pct*100` —— 列内量纲本就是"百分比数值"(2.79=2.79%),
再 *100 才显示成 278%。真实缺口是**纯粹的 91% 为 0 (需回填)**, 不是脏值。量纲夹取代码
作为 go-forward 防御保留 (无害), 但其"清理存量脏值"的动机不成立 (存量清 0 笔)。

### C — 退出画像归类修复 (chain.py)
- logic 类补 `DAY_TRADE_TP/SCALE_OUT/ORB_TAKE_PROFIT/SIGNAL_EXIT`; `ORB_BREAKDOWN`→stop;
  clock/stop/broker 补齐衍生标签; TRAILING_STOP 按用户选**留 stop 并在 hint 标注**。
- take_profit_n 改统计全部止盈标签。
- **实测 (近60天, 119笔)**: 逻辑类 19/16.0%/胜率100%/均+1.14% (原误报"仅7%"), take_profit_n=19
  (原 1), 时钟类降 31.1%; "止盈<5%""时钟≥40%"两条假告警消失, 仅留券商幽灵 10 笔告警。
- 止损类 47/39.5%/均-1.1%/胜率23.4% (含 TRAILING) 现为最大且唯一系统性亏损类 → 坐实批次2 目标。

### A — MFE/MAE 数据管道
- **A1 调查**: peak_price 48/103 单可用; trough_price 存量 0 (谷值追踪 7/11 才上线, 已在
  建仓 metadata 初始化 + monitor 循环采集); mae 存量无源, 只能 go-forward。
- **A2 回填** `scripts/backfill_mfe_mae.py` (默认 dry-run, --commit 写库, 改前已备份
  `ftseek_us.db.bak.20260711_mfebackfill`): 394 单扫描, 133 单回填 (17 由 peak 反算,
  116 由 realized 兜底), 无源保持 226。**mfe 合理占比 9% → 43%**。填不了的保持 0 (诚实, 不塞假值)。
- **A3 go-forward** (pipeline.py): 采集处 + 反算处各加量纲夹取 (`_MFE_MAE_SANE_MAX=50`,
  >50 视坏点/归 0); trough 采集已存在。
- **A4 探针** `check_decision_chain.py --mfe-health`: 报覆盖率 + giveback 分类。
  **局限 (诚实)**: 当前 giveback 多为 0, 因回填用 realized 兜底 (mfe=pnl); 真实 giveback
  需 go-forward 每轮真实峰值累积 → 批次2 前置。

### Stage 2a — invalidation 影子骨架
- 新增 `app/playbook/invalidation_shadow.py` 纯函数模块 (自测 6/6): 结构化 rules 优先 +
  自然语言尽力解析 (价格/百分比阈值), 返回 `{would_trigger, reason, parseable}`;
  含 Stage 2b 建议契约 (Hermes 侧 `invalidation_rules[]`)。
- 接入: 建仓 metadata 写入 `invalidation` (playbook_service.py); monitor 循环 not-exit
  分支影子评估, 首次命中记 `meta.invalidation_shadow` (held_minutes/price), **不实平**。
  老单无 invalidation 键→自然跳过, go-forward 新单生效。

### 验证 & 部署
- 全部文件 py_compile + GetProblems 无错; `invalidation_shadow.py` 自测 6/6;
  TestClient 验 `/api/chain/{exit-profile,calibration}` 均 401 (auth-gated, 路由已注册非 404);
  `ftseek-dashboard.service`(:8901) 已重启 active。
- 复核 `--mfe-health` (近60天 119 笔): 合理 72/61% · 脏 0 · 缺失 47。占比较回填当时(43%)
  自然回升, 因新平仓单走 go-forward 采集; giveback 仍多为 0 (回填 realized 兜底所致), 待真实峰值累积。

### 下一步 (未开工, 待累积/确认)
- **批次2 (TRAILING 调参)**: 等 go-forward 真实 MFE 累积 (≥2-3 周日内样本), 用 `--mfe-health`
  的 giveback 分辨"锁太松/太紧"再分因子调 breakeven/drawdown。
- **Stage 2b**: 需 Hermes 侧产出结构化 `invalidation_rules`, 影子命中率验证后再灰度实平。

