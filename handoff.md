# FTSeek-US · HANDOFF

> **这是什么**: DA-FTSeek 链路迭代的**活文档**。每次调整/计划都追加到这里, 避免跨会话/跨 Agent 从头开始。
> **怎么用**: 每次改动后更新"变更日志"(最新在最上) + "下一步"。其他 Agent 先读本文件再动手。
> **最后更新**: 2026-07-12 · 环境: 富途**模拟盘** (v2 sim, 无真实资金) · venv: `./venv/bin/python`

---

## 🎯 当前北极星 (换过 KPI)
- **只追"每笔期望值 ≥ $0"**, 不追胜率/周收益数字。收益是期望转正后的自然结果, 直接追会逼出"加仓凑收益"的自杀行为。
- 活跃 OKR: **`docs/OKR-2026-08.md`** (2026-07-14→08-31)。上一轮 `docs/OKR-2026-07.md` 已结算 **MISSED**。
- 重心在 **FTSeek 退出侧**, 不在 DA 预测侧 (见下"核心诊断")。

## 🔬 核心诊断 (数据实证, 别再推翻重来)
用 `scripts/exit_scorecard.py` 实测 (污染窗口 n=61/71):
1. **信号 ≈ 掷硬币偏正, 不是问题**: 持到收盘命中 50.8%, 均值 +0.37%/笔 → DA 有薄弱正 edge, 达到">50%"的诉求。
2. **漏血在退出**: 退出把 8 笔本该收盘为正的赢家做成亏损, 只救回 2 笔 → 胜率 50.8%→41%。
3. **R:R 结构性不足**: avg赢 $118 ≈ avg亏 $110, R:R=1.07 < 盈亏平衡 1.29 → 每笔期望 **−$10.57**。
4. **confidence 反向**: 低信心桶反而更好 (Playbook IC 为负) → 按 confidence 加仓 = 给更差信号加钱。**已拍平** (见变更日志)。

一句话: **不需要更准的预测; 需要退出别砍赢家 + 把 R:R 拉过 1.29。**

## 🧭 每周迭代循环 (每周五盘后)
```
1. 跑 scripts/exit_scorecard.py            → 期望值 / 杀赢家数 / R:R 缺口
2. 只问: 赢家还在被砍吗? R:R 离 1.29 还差多少?
3. 只改一个退出参数 (其余全冻结)
4. 下周同一张表对比: 变好→保留, 变差→回滚
铁律: 一次一个变量、一个指标、可回滚。禁止在期望转正前加仓/放大 R:R 追收益。
```

## 🛠️ 工具
| 工具 | 用途 |
|---|---|
| `python scripts/exit_scorecard.py [--since 2026-07-11] [--days N]` | 退出纪律周度判分 (只读)。落 `data/exit_scorecard_latest.json`+history |
| `python scripts/okr_status.py` | OKR 总览 (胜率/PnL/IC/因子)。落 `data/okr_latest.json` |

## 🗺️ 退出旋钮地图 (`config/playbook_horizon.yaml`, DA 恒走 `short_horizon`)
**A. 赢家被锁太小 (R:R 缺口主因) — `factor_exit_rules.default`**
- `drawdown_from_peak_pct` = **0.5** (L45) ← 浮盈回撤0.5%就平, 锁太紧, avg_win 上不去。07-09 从 0.8 收紧来的。
- `breakeven_at_pct` = 1.0 (L44)

**B. 赢家被噪声扫损 (杀赢家) — `short_horizon.vol_buckets`**
- low `sl_floor_pct`=2.0 (L370), medium=2.5 (L378) ← 多数标的落此, 实际止损≈2-2.5%
- ⚠️ 07-09 基于**日线回测**收紧过("2-3%最优"); 与 live scorecard 冲突时**信 scorecard**。

**C. 已核实**: `short_horizon.rr_ratio=999` (L353) → 固定止盈**关闭**, 靠 trailing+EOD。所以封顶赢家的是 A 的 drawdown 锁, **不是** rr_ratio。

## ➡️ 下一步 (下周五第一个动作)
**改 A: `drawdown_from_peak_pct` 0.5 → 0.8** (先只改 `default` L45)。
- 目的: 让赢家跑, 抬 avg_win, R:R 过 1.29。一个数字, 秒回滚, scorecard 判分。
- A 不够再动 B (`sl_floor` low/medium +0.5%)。**别 A/B 同时动。**

---

## 📝 变更日志 (最新在上)

### 2026-07-12
- **拍平 conf_factor** (数据支持, 可回滚): 新增 config `playbook.risk_parameters.confidence_sizing_enabled` (默认 **false** → conf_factor 恒 1.0)。改动仅 `pipeline.py` L2338-2345 reprice 块 (DA 路径); DEC 路径 `instruction_generator.py` L393 本就不 conf 加权。验证: flag 经 `get_playbook_config()` 实测=False。**开回来**: yaml 改 true。
- **新增 `scripts/exit_scorecard.py`**: 退出纪律周度判分器 (混淆矩阵 + expectancy + R:R + 杀赢家数)。只读。
- **OKR 换轨**: `OKR-2026-07.md` 结算为 MISSED (根因=退出漏血, 非预测); 新建 `OKR-2026-08.md` 以 expectancy 为核心 KR, 删除"加仓追收益"危险对策。
- **日内 IC 上线** (前序): `fwd_return_intraday` 列已迁移到线上库 `data/ftseek_us.db` (已备份 `.bak.20260712_intradayic`)。近30天覆盖 175/179。IC: intraday −0.14 / t1 −0.04 / t3 −0.09 / t5 −0.15 (污染窗口, 非定论)。
- **待办/未做**: 退出旋钮 A 的实际修改留到下周五循环; `US.US.` 双前缀脏 symbol 2 条 (id 245/323, 陈旧 DEC 行, 不影响交易) 暂不清洗, 自然 age out。
- **开盘前就绪检查 + 重启 (✅ 2026-07-12 11:24)**: 全链路体检通过 (模块导入/配置开关/DB schema+迁移幂等/sizing 模拟/服务/循环存活)。
  已**重启 `ftseek-us.service`** (新 PID 345299) 激活 conf 拍平 —— 冒烟检查全绿: 无 error、OpenD 连通、
  安全确认**模拟盘** (TRADE_ENV=1)、Futu 交易上下文连接、调度器配置加载。
  **⚠️ 周一盘前须确认**: DA 的 bridge 文件由 deep_analysis cron 在 **ET 09:00** 生成; 若缺失, 当天降级
  `TechnicalFallbackAdapter` (技术兜底), **不走 DA 信号** → 整条 DA-FTSeek 链路当天不生效。

### (更早) 2026-07-11 及之前 — 见 `docs/WEEKLY-SUMMARY-2026-07-11.md`
- 决策链路双脑清理、退出引擎批次1、校准闭环、dashboard 可视化、_TRACKING_FIELDS 审计断裂修复等。

---

## ⚠️ 长期 caveat (读之前先知道)
- **样本量**: ~49 笔/月, 干净窗口 (date≥07-11) 目前近 0 样本。多数结论 n<50, 要几周 go-forward 才显著。别把污染窗口的数字当定论。
- **模拟盘宽恕一切**: sim 不建模真实滑点/流动性; 即使 sim 正期望也未必活到实盘。
- **DA 是 repo 外的 Hermes skill**: 若信号在干净窗口被证伪 (期望仍<0), 修复点在 skill, 不在本 repo (见 OKR-2026-08 硬退出触发器 2026-08-10)。
- **纪律**: hard-to-reverse/线上写操作先备份+确认; 不为凑样本放松风控; 一次一个变量。
