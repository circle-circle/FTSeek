---
title: 资金/风控上限提升 (交易量档位 + 并发持股)
status: active
module: risk
related_code:
  - config/unified_config.yaml
  - app/risk/position_sizer.py
  - app/web_dashboard/mode_switch_service.py
related_doc: docs/adr/ADR-009-universe-expansion-execution-rail.md
tags: [risk, capital, position_sizing, aggression]
updated: 2026-07-14
---

# ADR-010: 资金/风控上限提升 — 交易量档位 normal + 并发持股 medium 8→10

**日期**: 2026-07-14
**状态**: 已实施
**决策者**: 用户 + Hermes

## 背景

用户提出：每日最大持股数量（不含已卖出的并发在手）与交易量是否有限制，基于资金/风控提升。实证摸底（活体数据）：

- **equity = $98,767**（medium 档 $50K–200K）；**buying_power = $197,534**（≈2× 保证金）；回撤 **−2.5%**（green 健康区）；当前在手仅 2 笔（MANUAL）——利用率远低于上限。
- **交易量真正节流点**：`auto_aggression = conservative`（auto 预算 = BP × 30% = **$59,260**），剩余 $138K 购买力闲置。
- **并发持股上限**：medium 档 `max_positions = 8`（position_sizer 建仓/槽位；随 equity 增长自动升 large 档=10）。
- 已存在的单日闸门：`max_daily_positions = 10`（新开/日，DA+universe_expansion 共享）、`daily_notional_cap_pct = 0.60`——在 $59K 预算下很少真正触顶，非当前约束。

## 决策

在资金充裕（$197K BP）、回撤健康（−2.5%）前提下，一次调两个高杠杆且可逆的旋钮，**不动**已足够的单日闸门：

| 变更 | 从 → 到 | 载体 | 影响 |
|------|---------|------|------|
| 交易量档位 `auto_aggression` | conservative(30%) → **normal(60%)** | `feature_flags.auto_aggression`（YAML，ModeSwitchService 启动读取，同 auto_trading_enabled 机制） | auto 预算 $59K → **$118K** |
| medium 档 `max_positions` | 8 → **10** | `capital.tiers`（YAML，`*id001` 锚点同步至 `risk.capital_safety.tiers`）+ position_sizer `DEFAULT_TIERS` 兜底 | 并发在手上限 8 → 10，对齐 large 档与 max_daily_positions |
| `max_daily_positions` | 10（不变） | — | 单日新开笔数已够（近 14 日多为 1–10，仅 7/08 达 12） |
| `daily_notional_cap_pct` | 0.60（不变） | — | 名义闸门已够 |

## 后果

**风险口径**：per-position 风险% 不变；仅放开预算与槽位。10 仓 × ~$10–12K/仓（~10–12% equity），2× 保证金有余量；daily_notional_cap(60% of risk_base) 仍封顶单日名义；`risk_merger` 按风险态继续压降并发（MODERATE→5 / WARNING→2 / DANGER→0）；water_level 回撤节流（green/yellow/orange/red）不变。上限提升只在信号优质、闸门放行时才被用满。

**回滚**：改回 `auto_aggression: conservative` 与 `max_positions: 8` 即恢复（YAML 秒改）；或 Dashboard 运行时切 aggression。全程 Paper Trade，不触真实资金。

**生效时点**：持久化已落 YAML/代码；运行中进程按其配置加载/单例缓存，**下次重启或配置热加载后生效**（未擅自重启 live 进程）；aggression 亦可经 Dashboard 运行时即时切换。

## 验证

`yaml.safe_load` 确认锚点传播：`capital.tiers.medium` 与 `risk.capital_safety.tiers.medium` 同为 10；`feature_flags.auto_aggression=normal`；`ModeSwitchService._read_yaml_auto_aggression()` 返回 `normal`；`DEFAULT_TIERS` 兜底同步为 10；两 py 文件 `py_compile` 通过。新 auto 预算 = $197,534 × 60% = $118,520。
