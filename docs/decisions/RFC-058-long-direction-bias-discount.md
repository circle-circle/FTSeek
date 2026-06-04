# RFC-058: LONG 方向信号默认削弱 30%, 仅 BULL regime 豁免

> **状态**: 草稿
> **日期**: 2026-06-01
> **作者**: Claude (深度数据驱动 review)
> **审批人**: 项目 owner

---

## 摘要

90 天 SQL 显示 LONG 134 笔 20.9% 胜率盈利仅 $2,644, SHORT 174 笔 33.9% 胜率盈利 $13,016 (5×). 但当前系统 entry signal 不带方向偏向, LONG/SHORT 平等触发. 建议默认 LONG 信号 strength × 0.7 折扣, 仅在 SPY > MA50 + VIX < 20 (确认 BULL regime) 时不打折.

---

## 问题陈述

**SQL 证据**:
```
direction   n     wr_pct   total_pnl   avg_pnl
LONG        134   20.9%    +$2,644     +$19.7
SHORT       174   33.9%    +$13,016    +$75.7
SHORT_PROX  13    15.4%    -$30,237    -$2,326     ← 已禁
```

**根因假设**: 当前美股市场 (2026 Q2) 处于震荡/弱势状态, SHORT 反向交易胜率高且 R/R 好。LONG 在弱势市做多接飞刀。

**当前防护** (`market_regime_filter.py:99`):
- SPY < MA50 时 LONG 信号 ×0.7 — 但仅在大盘明确弱势时, 震荡市同样吃亏
- VWAP filter (line 894) 仅在 strength<80 + price<VWAP 时拦截 — 太宽松

数据看 LONG 在所有 regime 都偏弱:
```
regime × direction:
HIGH_VOL × LONG:    ~70 笔, ~14% wr, ~-$25K
RANGE × LONG:       ~30 笔, ~22% wr, ~+$3K
TREND_UP × LONG:    ~30 笔, ~33% wr, ~+$2K  ← 仅这里 LONG 还算
```

---

## 提议方案

### 方案 A (推荐): regime-aware LONG 折扣
```yaml
risk:
  long_direction_filter:
    enabled: true
    default_discount: 0.7              # LONG strength × 0.7
    bull_regime_exempt:                # 满足全部条件不打折
      spy_above_ma50: true
      spy_above_ma200: true
      vix_max: 20
      consecutive_up_days_min: 2
    log_every_discount: true           # 每次打折记日志便于审计
```

代码改动点: `app/risk/market_regime_filter.py::evaluate` 增加 `apply_long_discount` 子方法, 在现有规则前置执行。

### 影响范围
- 服务焦点: 入场信号方向加权
- 改动指标: LONG 入场频率
- 预期变化: LONG 笔数 134 → ~80 (-40%), 烂 LONG 信号过滤掉, SHORT 不变

---

## 风险评估

### 最坏情况
市场转 BULL 时仍打折, 错过好 LONG 机会。但豁免条件已设 (SPY>MA50+MA200+VIX<20+连涨 2 日), 真 BULL 不会被打。

### 缓解措施
默认豁免阈值偏严 → 真 BULL 时 LONG 信号 ×1.0 不变。如果半个月内豁免命中率 < 5% 但 LONG 信号被打折>50 次, 说明阈值过严, 可放宽 (e.g. SPY>MA200 即可豁免)。

---

## 验证计划

T+14 SQL (LONG 需要更多时间积累 n):
```sql
SELECT direction, COUNT(*), AVG(realized_pnl > 0) * 100 AS wr, SUM(realized_pnl)
FROM trade_records WHERE entry_time >= datetime('now', '-14 days') GROUP BY direction;
-- 期望: LONG wr 提高 5-10pp, LONG PnL 由负转正或减亏
-- SHORT 不应受影响
```

T+1 日志验证:
```bash
grep "long_discount" logs/ftseek_us.log | head -20
# 应看到打折次数, 检查豁免逻辑触发
```

---

## 回滚计划

```yaml
long_direction_filter:
  enabled: false
```

**回滚触发**: 真 BULL regime (VIX<15 + SPY 历史新高) 持续 5 天 LONG 笔数 < 1 (说明豁免阈值有 bug)。

---

## 替代方案

### 方案 B: 完全禁 LONG (除非 BULL)
过激, 美股长期 LONG bias 历史上有效, 仅当下震荡。

### 方案 C: 只在 sandbox 模式打折
不解决 normal mode 也偏弱的事实。

---

## 审批

- [ ] 5 问自检 (LONG 表现差是否仅近期? — 90 天数据已足够代表当前 regime)
- [ ] n 充分: ✅ 134 LONG 笔 > 30
- [ ] BULL 豁免 fallback 路径设计清晰
- [ ] 审批人签字
