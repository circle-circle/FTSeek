# RFC-060 Phase 4: Alpha 提升

> **阶段目标**: 在 IC>0（前三阶段达成）后，系统性地提升信号预测力与执行质量。
> **核心命题**: 本阶段才动「加 Alpha」。前两阶段是让信号变好，本阶段是让信号更强。
> **预计周期**: 3-4 周
> **总纲**: 见 `RFC-060-TRADING-OPTIMIZATION-PROGRAM.md`
> **深度**: 📋 提纲（待进入实施时按 phase1 模板细化）
> **硬前提**: Gate 3→4 通过 + **IC 已连续 7 天 ≥0.05**（否则等于在 IC 为负时优化信号——PM 审计原话：在崖边加油门）

---

## 阶段总览

| ID | Item | Alpha 来源 | 数据基础 | 投入 | 预期 IC 提升 | 状态 |
|----|------|-----------|---------|------|------------|------|
| P4.1 | 因子权重 IC 学习（Ridge） | 权重自适应 | signal_snapshots.db | 中 | +20-30% | ⬜ |
| P4.2 | 截面排名因子 | 个股 vs universe | 全市场快照 | 中 | +0.05~0.10 | ⬜ |
| P4.3 | 自适应止损（regime/波动率分级） | 退出质量 | trade_records | 中 | 胜率 +3-5% | ⬜ |
| P4.4 | Thesis Challenge 接自动门 | 独立审计 | 代码已写 | 低 | 滤掉幻觉 | ⬜ |
| P4.5 | 微观结构因子（OBI/VPIN） | 盘口信息 | Futu LV3 | 中 | +0.10~0.20 | ⬜ |

> **建议顺序**: P4.4（低、代码已写）→ P4.1（与 P2.3 联动）→ P4.2 → P4.3 → P4.5（依赖 LV3 数据质量）

---

## P4.1 因子权重 IC 学习 🔴🔴

### 目标
当前因子权重硬编码（`app/core/intraday_scorer.py:247` `DEFAULT_WEIGHTS`）。改为从历史 IC 自动学习最优权重。

### 方案方向
```python
# 新增 app/factors/weight_learner.py
class FactorWeightLearner:
    def learn_from_history(self, days_back=60):
        # 从 signal_snapshots.db 读历史因子值 + 前瞻收益
        # Ridge Regression: max Σ(IC_i × w_i) - λΣ(w_i²)
        # 约束: Σw_i=1, w_i≥0
        pass
    def update_weights_weekly(self):
        # 每周日更新, 写 unified_config.yaml (复用 P2.5 ConfigWriter)
        pass
```

### 预期收益
- 因子权重随市场自适应（IC 提升 20-30%）
- 替代当前硬编码，是「系统能自己变好」在因子层的体现

### 依赖
- **P2.3**（IC 监控执行化）——学习的前提是 IC 可信、样本足够
- **P2.5**（ConfigWriter）——权重写回 YAML

### 待细化项
- [ ] 学习频率（周更 vs regime 切换触发）
- [ ] 过拟合防护（正则 λ、train/test 切分、OOS 验证）
- [ ] 权重漂移上限（防止单次学习剧烈改变）

---

## P4.2 截面排名因子 🔴🔴

### 目标
当前所有信号是时间序列因子（个股 vs 自己的历史）。引入截面排名（个股 vs universe），学术证明（Jegadeesh & Titman 1993）截面动量比时序动量更有效。

### 方案方向
```python
# 新增 app/factors/cross_sectional_rank.py
class CrossSectionalRanker:
    def compute_rank(self, symbol, universe, factor_name):
        # 动量排名: 个股 20 日收益率在 universe 的百分位
        # 波动率排名: ATR/Price 百分位
        # 资金流排名: volume_ratio 百分位
        values = [get_factor_value(s, factor_name) for s in universe]
        return sum(1 for v in values if v < current) / len(values)
```
- 集成点：`signal_stage.py` 用截面排名预筛 Top-N 候选

### 预期收益
- 截面动量 IC 通常比时序动量高 0.05-0.10
- 与现有因子正交，提升分散度（解决信号同质化）

### 依赖
- universe 定义（daily_active_pool 复用）
- 历史数据完整性

### 待细化项
- [ ] universe 范围（全市场 vs 池子 Top-200）
- [ ] 排名因子与现有 IntradayScorer 的融合方式
- [ ] 截面数据成本（全市场快照频率）

---

## P4.3 自适应止损（regime/波动率分级）🔴

### 目标
当前 `exit_policy.yaml` 的 `atr_stop.initial=4.0` 对所有标的一刀切。高波动标的（TSLA 日 ATR $15）止损 $60 可能超 max_loss 15% 硬底；低波动 ETF（XLU）止损 $2 在正常波动中频繁误触。

### 方案方向
- 低波动标的（ATR < price×0.5%）: 收紧到 3.0×ATR
- 高波动标的（ATR > price×3%）: 放宽到 5.0×ATR + 降仓位
- regime 联动：TRENDING 放宽 4.5×，RANGE 收紧 3.5×
- 复用 `market_regime_detector.py` 的 regime 信号

### 预期收益
- 减少低波动标的止损误触发（**提升胜率**）
- 高波动标的给更多空间（提升盈利持仓存活率）
- 在 RFC-055（SHORT+Trailing Stop 63.9% WR）的 edge 基础上进一步优化

### 依赖
- `market_regime_detector` 已有
- trade_records 足够（RFC-055 已验证数据基础）

### 待细化项
- [ ] 波动率分档阈值（需 trade_records 回测验证）
- [ ] 与 RFC-055 现有 scale_out / chandelier 的协同（避免冲突）

---

## P4.4 Thesis Challenge 接入自动进场门 🟡

### 目标
`Thesis Challenge`（进场理由审视，5 维度结构化检查）质量最高但**仅在 web dashboard API 调用**，不在自动交易管线。在进场前加一道独立审计。

### 关键接入点
- **逻辑已实现**：`app/llm/thesis_challenge.py:503` `challenge_thesis()`（temperature=0 / json_mode / 5 维度 / 清晰判定规则）
- **当前接入点**：仅 `app/web_dashboard/routes/dashboard.py:292-357`
- **目标接入点**：`app/risk/pre_entry_gate.py` 之前或之中
  ```python
  # 投票说"买"但 thesis 审计 INVALID 且 confidence>0.7 → 拒绝
  verdict = challenge_thesis(signal, ...)
  if verdict.is_invalid and signal.confidence > 0.7:
      return reject("thesis audit failed")
  ```

### 预期收益
- 滤掉 LLM 幻觉产生的低质量信号（胜率 +5-10%）
- 与 P2.4（辩论）互补的高置信度审计层

### 依赖
- 无强依赖（逻辑已完整）；建议 P1.1 模拟盘修复后用真实数据校准 verdict 阈值

### 待细化项
- [ ] verdict 阈值（INVALID 即拒 vs INVALID + 高 confidence 才拒）
- [ ] 缓存策略（当前 4h 缓存是否合适自动管线）
- [ ] 延迟成本（每笔进场多一次 LLM 调用，需评估 5s 超时）

---

## P4.5 微观结构因子（OBI/VPIN）🔴🔴

### 目标
当前订单流因子仅用 bid_vol vs ask_vol。引入更强的市场微观结构因子，数据基础（Futu LV3）已具备。

### 方案方向
```python
# 新增 app/factors/microstructure.py
class MicrostructureFactor:
    def compute(self, symbol, lv3_data):
        return {
            "obi": OBI(lv3_data),                 # 订单簿失衡: 加权买卖挂单差
            "trade_arrival_rate": TAR(lv3_data),   # 成交频率 (信息量代理)
            "large_order_ratio": LOR(lv3_data),    # 大单占比 (机构行为)
            "vpin": VPIN(lv3_data),                # 信息不对称度
        }
```
- 接入 `IntradayScorer._score_orderflow`（`intraday_scorer.py:580`）

### 预期收益
- 微观结构因子在日内交易 IC 强（0.10-0.20），尤其 VPIN
- 这是系统相对其他散户量化系统的差异化优势（LV3 数据已付费）

### 依赖
- Futu LV3 数据质量与新鲜度（需验证推送稳定性）
- `data_router` LV3 订阅（main.py:1502-1528 已有）

### 待细化项
- [ ] LV3 数据可用性验证（tick 级延迟、断流处理）
- [ ] 因子计算频率 vs 成本
- [ ] 与现有 orderflow 因子的融合/替换

---

## 阶段 4 实施状态表（迭代用）

| ID | Owner | PR/Commit | 部署日期 | 验收结果 | 状态 |
|----|-------|-----------|---------|---------|------|
| P4.1 | | | | | ⬜ |
| P4.2 | | | | | ⬜ |
| P4.3 | | | | | ⬜ |
| P4.4 | | | | | ⬜ |
| P4.5 | | | | | ⬜ |

---

## 阶段 4 完成定义（Definition of Done）
- [ ] P4.1~P4.5 全部 `✅`
- [ ] 因子权重已发生至少 1 次自动学习更新
- [ ] 至少 1 个新因子（截面/微观结构）IC>0.05 并接入主信号
- [ ] 自适应止损部署后，止损误触发率下降
- [ ] 整体 IC 稳定在 ≥0.05，胜率稳定在 45%+
