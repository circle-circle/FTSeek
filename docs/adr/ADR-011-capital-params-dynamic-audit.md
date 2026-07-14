---
title: 资金/水位参数动态化审计 + 交易量档位%移入配置
status: active
module: risk
related_code:
  - app/risk/capital_safety.py
  - app/coordinator/dual_mode_params.py
  - config/unified_config.yaml
related_doc: docs/adr/ADR-010-capital-risk-limit-uplift.md
tags: [risk, capital, dynamic, config, no-hardcode]
updated: 2026-07-14
---

# ADR-011: 资金/水位参数动态化审计 + 交易量档位% 移入配置

**日期**: 2026-07-14
**状态**: 已实施
**决策者**: 用户 + Hermes

## 背景

用户要求：资金水位、购买力预算、水位等级均应**动态取自富途实际账户值**（equity/buying_power 动态、避免硬编码），实盘与模拟统一配置，**百分比与等级可一键调整**。据此对资金/风控数据来源做全链路审计。

## 审计结论（实证）

| 项 | 来源 | 状态 |
|----|------|------|
| equity / buying_power / cash / market_val | `position_monitor._update_capital_safety` → `broker_adapter.get_account_info()`（富途 accinfo_query, 30s刷新）→ `capital.update_account()` | ✅ 动态取富途；broker 不可用时用"上次现金+实际持仓市值"兜底（非硬编码 82K） |
| water_level（回撤水位） | 由动态 equity vs 峰值算回撤 → `risk.capital_safety.water_levels`（green/yellow/orange/red 阈值+系数, YAML） | ✅ 动态 + 配置可调 |
| 资金分层 tiers（max_positions 等） | `capital.tiers`（`*id001` 锚点同步 `risk.capital_safety.tiers`, YAML） | ✅ 配置可调（随 equity 自动定档） |
| sim / live 账户 | `AUTO_TRADE_ACCOUNT`(sim\|real) 决定 broker 连哪个富途账户；两者均返回实时 account_info + 共用同一 YAML 配置 | ✅ 统一，无分叉硬编码 |
| **auto 交易量档位 %（30/60/75）** | `capital_safety._AUTO_LEVEL_CAPS` **硬编码在 Python** | ❌ **不可一键调** → 本次修复 |

`capital.fixed_amount=82000` 仅作 sim 种子/兜底基线（已在 YAML，可调），不参与实时预算计算。

## 决策

唯一的硬编码点 `_AUTO_LEVEL_CAPS` 移入配置，复用系统既有的规范档位读取器 `dual_mode_params`（读 `dual_mode.<level>`, YAML 优先 + fallback）：

1. **`capital_safety.get_available_budget_for_auto`** 改为读 `config dual_mode.<level>.max_total_exposure`（当前档位 %）× buying_power = auto 预算；`_AUTO_LEVEL_CAPS` 降级为最终 fallback。
2. **YAML 补齐 `dual_mode.normal` 块**（此前仅存于 Python fallback，ADR-010 已启用 normal 却在 YAML 不可见）。三档 `max_total_exposure` = 0.30/0.60/0.75，即三档交易量 %，**一键可调、热重载、sim/live 统一**。

档位切换（等级）已可一键调：`feature_flags.auto_aggression`（YAML 持久化）+ Dashboard 运行时切换。

## 后果

**零行为变更**：配置值与原硬编码完全一致（0.30/0.60/0.75）；当前 normal 档 auto 预算仍 = $118,520。此后调 `dual_mode.<level>.max_total_exposure` 即改交易量档位 %，无需改代码。与 exposure 控制同源，语义一致。

**回滚**：`dual_mode_params` 读取失败自动回退 `_AUTO_LEVEL_CAPS` 硬编码兜底，永不因配置缺失而阻断。

## 验证

`yaml.safe_load`：`dual_mode` 三档 max_total_exposure = 0.3/0.6/0.75 齐备；`dual_mode_params.get_conservative_param("max_total_exposure")` @normal = 0.6；`get_available_budget_for_auto()` = $118,520.68 = $197,534 × 0.60（与配置驱动结果精确吻合）；`py_compile` 通过。
