---
title: "RFC-159 DA 杠杆/反向 ETF 前置过滤（省额度）"
status: active
module: deep-stock-analysis.pipeline
related_code: /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py, config/unified_config.yaml
related_okr: OKR-2026-10
related_adr: ADR-035
tags: [da, candidate, etf, budget, efficiency]
updated: 2026-10-22
---

# RFC-159 DA 杠杆/反向 ETF 前置过滤（省额度）

**状态**: active（2026-10-22 用户「明确有收益/有助于效率提升的可以落地执行」后实施）

**类型**: DA 候选预筛变更（只影响分析预算，不改任何入场/出场/仓位门）。

**C1 名额**: 不占用 OKR-2026-10 C1 新入场假设名额。

---

## 1. 证据（2026-10-22 08:48 ET 批次）

DA 批次 28 条 gate 决策中，**8 条是杠杆/反向 ETF**，全部因缺 9 维核心数据
（新闻/研报/情绪等）判 `SKIP`：

| 标的 | name | gate |
|---|---|---|
| US.AAOZ | 2倍做空AAOI ETF-Tradr | SKIP |
| US.AXTQ | 2倍做空AXTI ETF-Tradr | SKIP |
| US.AXTX | 2倍做多AXTI ETF-Tradr | SKIP |
| US.BMNZ | 2倍做空BMNR ETF-Defiance | SKIP |
| US.SMST | 2倍做空MSTR ETF-Defiance | SKIP |
| US.LITG | 2倍做多LITE ETF-Leverage Shares | SKIP |
| US.LRCC | 2倍做多LRCX ETF-Corgi | SKIP |
| US.LRCU | 2倍做多LRCX ETF-Tradr | SKIP |

- 占当日 gate 的 **8/28 = 28.6%**；按每只 2 次 LLM 估算，约 **16 次 LLM 调用**
  花在结构性死票上。
- `extended_hours_entry_executor` 已用 `"ETF" in name.upper()` 过滤 ETF
  （实测 6/6 命中、同榜个股 0 误伤），但 DA 的主池/盘前异动通道没有复用这条规则。
- 这批 ETF 全部没有财报事件，`market_scanner` 的前缀 ETF 名单（TQQQ/SQQQ/SPY…）
  也不覆盖“2 倍做空/做多单股 ETF”。

## 2. 规则

在 DA `_screen_da_candidates` 的资格判定处，若：

```text
name 含 "ETF"
且 name 含任一杠杆/反向关键词:
  2X / 3X / 2倍 / 3倍 / BULL / BEAR / ULTRA / INVERSE / 做空 / 做多 / 杠杆
```

则直接 `REJECT`，reason=`leveraged_etf`，不进入数据采集/2 次 LLM/信号门。

- **只过滤杠杆/反向 ETF**；SPY/QQQ 等宽基 ETF、普通个股不误伤。
- 配置：`universe_expansion.da_skip_leveraged_etf: true`（读不到 ⇒ false，保持原行为）。
- 回滚：`da_skip_leveraged_etf: false` + 下次 DA 进程生效（无需改代码）。
- 代码备份：`pipeline.py.bak-da-leveraged-etf-20261022`。

## 3. 收益与边界

- **直接收益**: 今日 8/28 的分析额度释放；按 5 并发、每只 1–2min 采集 + 2 次 LLM
  估算，批次可提前约 3–6 分钟完成，给开盘前留更多余量。
- **间接收益**: 漏斗中 `leveraged_etf` 可归因，后续不会再把 ETF 的“缺 9 维数据”
  混进 DA 的 SKIP 统计。
- **边界**: 若未来某只杠杆 ETF 出现真实可交易信号，本过滤会按设计放弃它；
  当前系统的其他层（ext_entry）本来也不交易 ETF，故与现策略一致。
- **不改**: 入场门/仓位/止损/退出/UE 扫描/宽基 ETF。

## 4. 验证

- 外部契约测试 `tests/test_leveraged_etf_filter.py`：**18 passed**
  （9 个杠杆 ETF 名全命中、6 个非 ETF 名零误伤、screening REJECT、config 开关读写）。
- `py_compile` 通过；干跑今日名称：ETF **8/8** 命中，15 条落库指令 **0/15** 误伤。
- 下一 DA 批次观察 `[Pipeline]` / 候选漏斗的 `leveraged_etf` 计数。

## 5. 监控与回退观察

- 下一批次检查 `candidate_screening.reasons` 中 `leveraged_etf` 的计数：
  - 若 >0：记录实际省下的分析数，与今日 8/28 基线对比；
  - 若 =0（说明候选 `name` 未透传到 DA 筛选层）：在 `_analyze_one` 采集数据后、
    调 LLM 前补一次 `name` 检查（不调 LLM），作为一次新变量重新验证；
  - 不应仅因计数为 0 就回滚配置——先定位是 name 透传问题还是候选来源问题。
- 回滚：`universe_expansion.da_skip_leveraged_etf: false`（下次 DA 进程生效）。
