# RFC-056: HIGH_VOL 市况过滤阈值收紧

> **状态**: 草稿
> **日期**: 2026-06-01
> **作者**: Claude (深度数据驱动 review)
> **审批人**: 项目 owner

---

## 摘要

90 天数据显示 HIGH_VOL 市况下 141 笔交易胜率 14.9% 亏损 -$39,537 (占总亏损 271%)。当前 `MarketRegimeFilter.VIX_FEAR_THRESHOLD=30.0` 阈值偏松，VIX 在 22-29 区间时入场未被拦截。建议收紧到 22-25 多档。

---

## 问题陈述

**SQL 证据** (n=141, 远超 30 笔策略改阈值):

```sql
SELECT market_regime, COUNT(*), wr_pct, SUM(realized_pnl)
FROM trade_records WHERE entry_time >= datetime('now', '-90 days')
-- HIGH_VOL: 141 笔, 14.9% wr, -$39,537
-- TREND_UP: 88 笔, 51.1% wr, +$6,262
-- RANGE:    82 笔, 25.6% wr, +$17,299
```

**当前 filter 行为** (`app/risk/market_regime_filter.py:77`):
```python
if vix > VIX_FEAR_THRESHOLD:  # 30.0
    return 0, "VIX恐惧 → 熔断"
```
VIX 22-29 区间不触发熔断, 仅在 VIX>30 才完全拒绝。

---

## 提议方案

### 方案 A (推荐): 分级阈值
```yaml
risk:
  market_regime_filter:
    vix_levels:
      low: 18         # < 18: 信号 ×1.0 (正常)
      mid: 22         # 18-22: 信号 ×0.85
      high: 25        # 22-25: 信号 ×0.5 (强力削弱)
      critical: 28    # ≥ 28: 完全拒绝 (从 30 降至 28)
```

### 方案 B (简单): 直接降阈值
`VIX_FEAR_THRESHOLD: 30.0 → 25.0`

### 影响范围
- 服务焦点: 入场过滤 (北极星: 月度净 PnL ≥ 0)
- 改动指标: HIGH_VOL 市况入场频率
- 预期变化: 减少 ~70-100 笔 HIGH_VOL 入场, 减亏 -$25K~-$30K (按当前比例外推)

---

## 风险评估

### 最坏情况
高波动期完全错失反转机会。但数据显示 HIGH_VOL 中胜率仅 14.9%, 错过比错入更安全。

### 缓解措施
保留 SHORT 方向豁免 (高波动反向更敏感, 数据 SHORT 33.9% > LONG 20.9%), 仅拦 LONG。

---

## 验证计划

T+7 SQL:
```sql
SELECT market_regime, COUNT(*), wr, SUM(realized_pnl)
FROM trade_records
WHERE entry_time >= datetime('now', '-7 days')
GROUP BY market_regime;
-- 期望: HIGH_VOL 笔数 < 5, 占比 < 10%
```

如果 HIGH_VOL 仍 ≥ 30 笔/周或亏损 < -$5,000 → 阈值还需要进一步收紧。

---

## 回滚计划

```bash
# 一行回滚 (只改一个 yaml 数字)
sed -i 's/critical: 28/critical: 30/' config/unified_config.yaml
systemctl restart ftseek-us
```

**回滚触发**: 连续 3 个交易日机会数为 0 (filter 太严)。

---

## 替代方案

### 方案 C: 保持现状但加 VIX 跳变检测
当前 `_detect_vix_jump` 已处理 VIX 跳变, 但只削弱不拦截。可单独提议放弃。

---

## 审批

- [ ] 5 问自检完成 (此问题为何之前没发现? 因为只看了均值, 没切 regime)
- [ ] 数据 n 充分: ✅ 141 > 30
- [ ] 回滚 playbook: ✅ 单行 sed
- [ ] 审批人签字
