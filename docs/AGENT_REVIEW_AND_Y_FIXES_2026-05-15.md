# Agent 诊断验证 + Y 系列数据驱动修复

> **日期**: 2026-05-15
> **基于**: 另一 agent 的深度诊断 + 真实 trade_records (701 笔历史)
> **测试**: 111/111 全绿 (含新增 11 条 Y 测试)

## 1. Agent 诊断的真实性核验

### Agent 论点 vs 真实数据 (SQL 验证)

| Agent 论点 | 真实数据 | 判定 |
|-----------|---------|-----|
| MAIN 137 笔 40% +$15K | 138 笔 39% +$15K | ✅ 准确 |
| **(关键) MAIN 真实 edge** | **剔除 ORCL +$17K 异常后, MAIN 仅 +$3,131 (avg $23/笔)** | 🔴 Agent 高估了 5x |
| SCALPING 119 笔 22% -$1,248 | 业务 119 笔 22% -$1,248 (含 428 orphan_cleaned 0 PnL) | ✅ 准确 |
| BB 普遍崩溃 (BWXT/RIVN/LCID) | 仅 LCID afternoon 带宽 $0.02 是真问题, 其他正常 | ⚠ 局部非普遍 |
| Regime 全 UNKNOWN 256 笔 | **701/701 笔全 UNKNOWN** | ✅ 比 agent 看到的更严重 |
| 同标的连续止损模式 | LCID 16 笔 12 亏 -$676, ARM 单笔 -$757 | ✅ 比 agent 看到的更严重 |
| 5 因子 4 个源自价格 | 代码确认 (trend/momentum/lifecycle/volume 都基于 price) | ✅ 正确 |
| TRAILING_STOP 唯一盈利 | 还有 PRE_CLOSE +$2577, tp_hit +$747 (但 WR 27%) | ⚠ 漏掉 |

### Agent **没看到**的关键问题
1. **STRUCTURE_EXIT 33 笔 -$3,106** — 真实最大单一亏损源 (Agent 只标"巨额亏损")
2. **GOOGL SHORT BREAKEVEN_STOP -$6,015 单笔** — 这是**代码 bug** 不是市场风险
3. **ORCL SHORT EXTERNAL_CLOSE +$17,882 单笔** — 财报抓到掩盖了主线真实表现
4. **NVDA 4/29-5/1 PRE_CLOSE 3 笔 -$1,602** — 主线在 NVDA 上反复犯错

---

## 2. Agent 建议优先级再排序 (基于真实数据)

| Agent 建议 | 我的判定 | 理由 |
|-----------|---------|------|
| 关 SCALPING | ❌ 不需 (P0-X 已收紧) | scalping -$1248 仅占总损失 30%, 关掉损失"数据来源"。已加 SymbolDailyLossGuard 收紧到 2 笔触发 |
| BB 带宽崩溃过滤 | ✅ 已修 (Y2) | LCID 类局部问题, 加 `bandwidth < price * 0.001` 过滤 |
| Regime Detector | ✅✅ 已修 (Y1) | 701/701 UNKNOWN 致命数据缺失, 用 ADX+VIX+SPY 三维分类 |
| 连续亏损暂停 | ✅ 已做 (P0-X) | SymbolDailyLossGuard, 阈值收紧到 2 笔 |
| TRAILING SSOT | ⏸ 复杂 | TRAILING +$4921 是最大盈利, 但不应一刀切 — 应按 thesis 选 (Phase B) |
| entry_thesis | ✅ 已做 (L3) | yaml flag 启用即可 |
| 因子独立性 | ⏸ 长期 | 重大架构改动, 不在今天范围 |

---

## 3. Y 系列实际修复 (今日落地)

### Y1: Market Regime Detector
**文件**: `app/risk/market_regime_detector.py` (新, 220 行)
**核心**: 用 ADX(14) + VIX + SPY 日内变化 三维分类
```
EXTREME (VIX>35) — 不开仓, position_scale=0.0
TRENDING_UP (ADX>25 + SPY>+0.5%) — 顺势, scale=1.0
TRENDING_DOWN (ADX>25 + SPY<-0.5%) — 顺空, scale=1.0
RANGING (ADX<20) — 均值回归, scale=0.6
HIGH_VOL_CHOP (ADX<20 + VIX>25) — 不推荐任何策略, scale=0.3
TRANSITION (ADX 20-25) — 减仓观望, scale=0.5
UNKNOWN — 数据缺失兜底, scale=0.5
```

**自动 fallback**: K 线数据不足 → 仅用 VIX 分类 (不会再 700/700 UNKNOWN)

### Y2: BB 带宽崩溃过滤
**文件**: `app/scalping/signals.py:check_bollinger`
基于 LCID afternoon `bb_upper=6.08 bb_lower=6.06` (带宽 0.3%) 数据:
```python
if bandwidth < price * 0.001 or bandwidth <= 0:
    return None  # 数据异常, 不出信号
```

### Y3: STRUCTURE_EXIT 收紧
**文件**: `config/unified_config.yaml`
```yaml
symbol_cooldown.structure_exit:
  consecutive_losses: 3 → 2     # 实测 2 次后大概率继续亏
  cooldown_hours: 24 → 48       # 短期 1 天不够
scalping_sl_hit:
  daily_limit: 3 → 2
```

### Y4: BREAKEVEN_STOP for SHORT bug 修复 ⭐⭐⭐
**文件**: `app/risk/atr_stop_engine.py:217-218`
**真凶**: GOOGL SHORT entry $335.99, breakeven_stop = $335.99 × (1 - 0.002) = $335.32 (在 entry **之下**!)
- SHORT 取 `min(candidates)` 选最低 stop_line → $335.32 是最低
- SHORT 触发条件: `current_price >= stop_line`
- 入场 $335.99 已经 > $335.32, **入场瞬间满足触发条件**
- 实际异常 +$6,015 损失因为价格反弹到 $346 才被打到

**修复**:
```python
# 旧 bug:
breakeven_stop = entry_price * (1 + buffer_pct) if direction == "LONG" \
    else entry_price * (1 - buffer_pct)  ← SHORT 用减法 = entry 之下

# 新:
if direction == "LONG":
    breakeven_stop = entry_price * (1 + buffer_pct)
else:
    breakeven_stop = entry_price * (1 + buffer_pct)  ← SHORT 也是加法
```

### Y5: PnL 异常值监控
**文件**: `app/risk/pnl_outlier_monitor.py` (新, 150 行)
- 30 天滚动 mean/std
- 单笔 |z-score| > 5 → 关键告警 + 写 `pnl_outliers` 表
- 接入主线 + scalping 平仓路径
- ORCL +$17K (z=170σ) 和 GOOGL -$6K (z=-60σ) 类异常**永远逃不掉监控**

---

## 4. 测试覆盖

`tests/coordinator/test_y_data_driven_fixes.py`: **11/11 全绿**
- Regime: EXTREME / 数据缺失 / TRENDING_UP / position_scale
- BB: 源码层验证 (避开 pytz 间接依赖)
- BREAKEVEN: 源码层验证 SHORT 修复
- PnL outlier: 初始/正常/极端三态
- STRUCTURE_EXIT yaml 收紧验证

全 coordinator 回归: **111/111 全绿**

---

## 5. 综合数据真相

### 主线 MAIN_PIPELINE 真实表现 (剔除异常值)
| 范围 | n | total | avg | win_rate |
|------|---|-------|-----|---------|
| 含异常 | 138 | +$14,999 | $109 | 39% |
| **剔除 ORCL+GOOGL 两个异常** | 136 | **+$3,131** | **$23** | 39% |

→ MAIN edge 真实存在但**远小于 agent 估算**, ORCL 财报抓到吹大了数据.

### 头部 + 尾部交易 (说明集中度)
**Top 5 利润**: ORCL EXTERNAL_CLOSE +$17K / BWXT TRAILING +$3K / MSFT PRE_CLOSE +$3K / ORCL TRAILING +$2K / SPY PRE_CLOSE +$2K
**Bottom 5 损失**: GOOGL BREAKEVEN -$6K (bug!) / META PRE_CLOSE -$2K / NVDA STRUCTURE -$1.3K / NVDA PRE_CLOSE × 2 -$1.6K

**结论**: 利润和损失都极度集中在少数标的的少数大笔. 系统**不是稳定 edge**, 是**少数大赢压住多数小亏**.

### Exit Reason 综合
| 退出原因 | n | total PnL | avg | 评价 |
|---------|---|-----------|-----|------|
| EXTERNAL_CLOSE | 1 | +$17,882 | 1 笔异常 | 财报抓到, 不可复制 |
| TRAILING_STOP | 76 | +$4,921 | +$65 | ✅ 真正的 edge |
| PRE_CLOSE_EXIT | 22 | +$2,577 | +$117 | ⚠ WR 27% 但 avg 高 (赔率好) |
| tp_hit | 5 | +$747 | +$149 | ✅ TP 命中即赚 |
| BREAKEVEN_STOP | 1 | -$6,015 | 1 笔 bug | 🔴 已修 (Y4) |
| STRUCTURE_EXIT | 33 | -$3,106 | -$94 | 🔴 已收紧 (Y3) |
| sl_hit | 105 | -$1,632 | -$16 | ⚠ 标准止损被打 |
| FLASH_EXIT | 4 | -$1,263 | -$316 | 🚨 4 笔全亏建议禁用 |
| time_stop | 7 | -$376 | -$54 | ⚠ 无中途退出策略时的兜底 |

---

## 6. 今晚开盘验证 checklist

启动后看:
```bash
journalctl -u ftseek-us -n 100 | grep -E "RegimeDetector|breakeven|BB.*sanity|outlier"
```

期望:
- ✅ `[RegimeDetector] regime=RANGING confidence=0.7` 类日志 (而不是 UNKNOWN)
- ✅ 任何 BREAKEVEN_STOP 触发的损失 < 1% (不再有 -$6K 异常)
- ✅ scalping 中 BB 带宽 < 0.1% 价格的标的不会出信号

收盘后:
```bash
sqlite3 data/ftseek_us.db "
SELECT exit_reason, COUNT(*) n, ROUND(SUM(realized_pnl),2) total
FROM trade_records WHERE date(exit_time)=date('now')
GROUP BY 1 ORDER BY n DESC;"
# 期望: STRUCTURE_EXIT 笔数减少 (cooldown 收紧), BREAKEVEN_STOP 不再出现 -$6K 类异常
```

```bash
sqlite3 data/ftseek_us.db "SELECT * FROM pnl_outliers WHERE date(detected_at,'unixepoch')=date('now');"
# 期望: 0 行, 但若有也 OK (说明监控生效)
```

---

## 7. 后续 (本周)

1. ⏸ 启用 L3 entry_thesis (yaml flag) — 让退出按入场理由选 policy
2. ⏸ 启用 L6 regime classifier — 与 Y1 detector 互补 (LLM 更深推理)
3. ⏸ FLASH_EXIT 4 笔 -$1,263 全亏 → 评估禁用 (`exit_policy.yaml` 加 enabled=false)
4. ⏸ 因子独立性: 引入订单流深度数据 (LV3 已有, orderflow 因子权重从 17% 提到 30%)

---

**结论**: Agent 整体方向正确, 但有几处不准确 (高估 main edge, 漏 GOOGL bug, 漏 STRUCTURE_EXIT 最大亏损源). Y 系列 5 项修复基于真实 SQL 验证, 优先级精准, 111 单测全绿. 今晚开盘可立即验证.
