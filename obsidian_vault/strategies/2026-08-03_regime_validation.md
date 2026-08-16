---
type: "review"
title: "Regime 精准性验证 (2026-08-03)"
created: "2026-08-03"
tags: [regime, validation, market-regime]
---

# Regime 精准性验证 — 用真实 SPY 日K 重算比对

> 方法: 拉取 SPY 一年日K (Futu, 251行), 按 MarketRegimeGate 规则重算每日 regime (T-1 收盘视角防前视),
> 与系统记录 (playbook_instructions.market_regime) 逐日对比。VIX 无真实日线, 用 SPY 20日 realized vol 代理。

## 一、结论速览

| 维度 | 判定 |
|---|---|
| 趋势分 (MA 部分) | ✅ 精准 — 7月有记录日 11/13 与重算一致 |
| VIX 分量 | ⚠️ 主要短板 — 7/7、7/31 两处分歧全来自 VIX 数据源 |
| 标签缺失 | ⚠️ 25% None (7/9-7/23 指令级未落库) |
| 大跌响应 | ✅ 无滞后 — 7月无大盘级大跌 (最大 -1.5%); 7/27 是板块级 (半导体) |
| 评估时点 | ✅ 系统盘中实时评估, 比日收盘重算更敏感 (7/23 盘中已转 cautious) |

## 二、逐日比对 (7月)

| 日期 | 记录 | 重算 | score | SPY | 当日% | 备注 |
|---|---|---|---|---|---|---|
| 07-01/02/06 | cautious | cautious | 3 | 745-751 | -0.1~+0.9 | ✅ |
| 07-07 | risk_on | cautious | 3 | 747.71 | -0.5% | ⚠️ VIX: 记录15.81 vs RV20 18.4 |
| 07-08 | risk_on | risk_on | 6 | 745.40 | -0.3% | ✅ |
| 07-09~23 | None | risk_on/cautious | 2-8 | — | — | 标签缺失 (非错判) |
| 07-22 | risk_on | risk_on | 8 | 747.41 | -0.1% | ✅ 高分日 |
| 07-23 | cautious | risk_on* | 8 | 738.18 | -1.2% | *盘中实时评估已转 cautious (系统更敏感) |
| 07-24~30 | cautious | cautious | 4 | 729-742 | -1.5~+1.7 | ✅ |
| 07-31 | risk_off | cautious | 2 | 747.03 | +0.7% | ⚠️ VIX: 记录20.66 vs RV20 12.4 |

## 三、关键发现

1. **趋势分精准**: MA 部分与真实 SPY 数据完全一致, 规则引擎本身可靠。
2. **VIX 数据源是主要短板**: 两处分歧 (7/7, 7/31) 均由 VIX 与 SPY 实际波动脱节造成。
   risk_off 的 VIX 恒 20.66 (n=23 同值) — 单日缓存特征, 7/31 的 risk_off 判定可能被脏 VIX 拉低。
3. **"大跌滞后"结论修正**: 7月无大盘级大跌 (最大 -1.5% 在 7/29)。7/27 的 12/12 LONG 事故是**板块级** (半导体) 大跌,
   大盘 regime 天然捕捉不到 → 真正的结构缺陷是 **regime 粒度 (大盘) vs 指令粒度 (个股) 错配**。
4. **risk_on 的 -180 结论验证有效**: risk_on 交易集中在 7/7-7/8 + 7/20-7/22 (重算 score 6-8 高分日),
   7/22-23 追高被 7/23 -1.2% 套住。机制 = "高分日追高", 不是 regime 定义错。
5. **评估时点错配**: 指令盘前生成 (regime 未落库) vs 执行盘中实时评估 (7/23 盘中已转 cautious)。
   指令按盘前 regime 生成、按盘中 regime 执行 — 两者可能不同, 因指令级缺失无法验证。

## 四、改进建议 (按优先级)

1. **修 VIX 数据源**: FRED/AlphaVantage 与 SPY 波动校准, 或双源交叉验证; 排查 7/31 恒 20.66 缓存问题
2. **指令级 regime 落库**: 7/9-7/23 缺失是记录 bug, 修复后复盘才能区分"生成时 vs 执行时" regime
3. **板块级 regime**: 在大盘 regime 上叠加半导体/科技/金融等板块状态 — 7/27 事故的根因
4. **记录 score 分量**: 趋势分/波动分分开落库, 复盘可验证 VIX 是否污染判定
5. **生成/执行双时点记录**: 指令生成时与执行时的 regime 都记录, 暴露错配

## 五、对上一轮 regime×方向 结论的修正

- "risk_on 禁 LONG" (-180): **保留但机制修正** — 是"高分日 (score 6-8) 追高"陷阱, 不是 risk_on 定义错
- "RANGE 禁 SHORT": **作废** (RANGE 是主管线旧值混入)
- "cautious 双向都行" (+235): 保留 (主导 regime, 11 天验证一致)
- 56 笔输家单 -2039: 不受影响 (纯退出路径分解)

## 六、修复执行记录 (2026-08-03 16:20 CST)

### 修复 1: VIX 数据源 — FRED 滞后污染 (已修复+验证)

**根因 (修正后)**: 非交易时段 (盘前生成指令) 用 FRED VIXCLS 日频, FRED 更新滞后 2-3 天
(8/3 仅到 7/30) → 7/31 盘前生成 23 条指令全用 7/29 的 20.66, 与 SPY 实际波动 12.4% 脱节。
(注: 初诊误判 FINNHUB_API_KEY 缺失 — 独立进程未加载 .env.us; 服务进程 key 有效,
Finnhub VIXY 交易时段路径本就工作, 问题只在非交易时段的 FRED 滞后。)

**修复**: `app/data/market_data_router.py` get_vix_or_none() — 盘前/交易时段 (PRE_MARKET/TRADING)
优先 Futu VIXY 实时报价 × 0.85 contango 校准 (与 AlphaVantage 同系数), 失败再走 AlphaVantage 链。

**验证**: PRE_MARKET 实测 = 17.43 (VIXY 20.51×0.85, 实时) vs FRED 滞后 17.09 (7/30);
MarketRegimeGate 链路 regime=risk_on score=6 vix=17.43 vix_uncertain=False ✓

### 修复 2: 指令级 regime 落库 — UE 直写缺三字段 (已修复)

**根因**: 7/9-7/23 指令级 market_regime 全 NULL (129条: deep_analysis 108 + universe_expansion 21)。
- DA bridge (skill pipeline.py): Fix②b 2026-07-12 已接 regime 字段, 此处无缺失
- 主生成 run_playbook_generate: 已传 regime_ctx ✓
- **UE inject (scripts/llm_universe_scan.py cmd_inject): 直写未传 regime 字段 ← 缺失源**

**修复**: UE inject 指令 dict 补 market_regime/vix_at_generation/spy_trend_at_generation
(复用 MarketRegimeGate, 与 DA bridge 同口径; assess() 内建降级链, best-effort 不阻塞注入)。

### 第 3 项 (板块级 regime): 暂缓 — 设计型改动, 且 UE v2.3 级联 (7/28) + sector_context 注入已部分覆盖
### 第 4 项 (score 分量): 已存在 — save_regime_log 落 score 分量 (pipeline.py L1504-1505), 复盘可查

### 待办
- 服务已重启 (16:15/16:20, 新代码生效); 今晚 20:00 盘前生成 + 21:10 UE inject 用新代码
- 8 月第 2 周可复查: 新指令的 market_regime 填充率应为 100% (除 assess 降级)
- 下一轮 OOS (8/1-8/15 定级 → 8/16-8/31 验证) 时, regime 切片现在有干净数据