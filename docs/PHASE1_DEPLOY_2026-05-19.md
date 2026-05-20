# Phase 1 部署 + 今晚开盘验证清单 (2026-05-19)

> **5 项数据驱动优化已实施 + 测试**, 236/236 全绿.
> 美东 09:30 ET = 北京 21:30. 今晚部署 + 验证.

---

## 1. 修改清单

| # | 文件 | 变更 | 行数 |
|---|---|---|---|
| **A.1** | `app/pipeline/order_executor.py:792-803` | 删除 `short_min_strength<20` 绝对底限, 仅留 `effective<65` | -10 |
| **B.1** | `app/scalping/exits.py:105` | `breakeven_after_minutes: 2 → 6` | +5 注释 |
| **B.2** | `app/scalping/engine.py:1881-1888` | `_execute_trade` 加 `price < 20.0` 拒绝 | +9 |
| **C.1** | `app/scalping/engine.py:1890-1899` | `_execute_trade` 加 `LONG + regime=TREND_DOWN` 拒绝 | +10 |
| **C.2** | `app/scalping/circuit_breaker.py:57-78` | `record_loss(gross_pnl_pct)`, 噪声 ≥-0.1% 不计 | +20 |
| **C.2** | `app/scalping/engine.py:2789` | 调用方传 `gross_pnl_pct=pnl_pct` | 1 行 |

净增约 **+30 行**, 修改面小, 风险面窄.

---

## 2. 部署步骤

### 2.1 sanity check (本地, 已完成)
```bash
PYTHONPATH=. pytest tests/coordinator/test_phase1_optimizations.py -v  # 9/9
PYTHONPATH=. pytest tests/coordinator/ -q                              # 236/236
```

### 2.2 生产部署 (今晚 21:00 北京时间, 开盘前 30 min)
```bash
# 在生产服务器 39.97.58.4
cd /opt/ftseek/FTSeek-US
git pull origin main
git log -3 --oneline                                  # 确认 5 个 Phase 1 commit
sudo systemctl restart ftseek-us
sleep 10
journalctl -u ftseek-us --since "1 minute ago" | head -50
```

**期望日志**:
```
[ScalpingEngine] 信号冷却时间: 300s
[O1·SANDBOX] polling_controller 已严格过滤: 注册 12 只
[Coordinator·idempotent] (无, 因为还没 entry)
[CircuitBreaker] 初始化 | 最大连续亏损: 3 | 冷却时间: 60 分钟
✅ [PostTradeHook·wired] order_executor 已绑定 (2 处 on_position_opened 调用, 2 处 import)
✅ FTSeek-US 服务已启动
```

---

## 3. 今晚开盘验证 (21:30-22:30 北京 = 09:30-10:30 ET)

### 3.1 5 个修复点的可观测验证

```bash
# A.1: SHORT 信号通过率上升 (期望 SHORT_STRENGTH_WEAK = 0)
sqlite3 data/rejections.db "
SELECT COUNT(*) FROM rejections 
WHERE date(timestamp) = date('now') AND reason_category='SHORT_STRENGTH_WEAK';"
# 期望: 0 (修前每天 13-76)

# B.1: 提前止损 6min 起 (持仓 < 6min 不应有 "提前止损")
journalctl -u ftseek-us | grep "提前止损 (持仓 [0-5]min" | wc -l
# 期望: 0

# B.2: 低价股拒绝
journalctl -u ftseek-us | grep "Scalping·低价股门"
# 期望: 多次出现 LCID/TME/RIVN

sqlite3 data/ftseek_us.db "
SELECT COUNT(*) FROM trade_records 
WHERE trade_date=date('now') AND owner_strategy='scalping' AND entry_price<20;"
# 期望: 0 (修前每天 ~13)

# C.1: regime gate 拒绝
journalctl -u ftseek-us | grep "Scalping·regime门"
# 期望: TREND_DOWN 时段出现

# C.2: 噪声不计熔断
journalctl -u ftseek-us | grep "CircuitBreaker·噪声"
# 期望: 出现 (说明噪声过滤生效)
journalctl -u ftseek-us | grep "CircuitBreaker.*🚨 熔断触发"
# 期望: 减少 (5/18 是 3 次, 期望 ≤ 1)
```

### 3.2 业绩指标 (开盘 1h 后 22:30 检查)

```sql
-- 1. SHORT 通过笔数 vs 历史 (期望 SHORT 比例上升)
SELECT direction, COUNT(*) n
FROM trade_records WHERE trade_date = date('now', 'localtime')
GROUP BY direction;
-- 5/18 历史: LONG 11 vs SHORT 4 (3:1)
-- 期望今晚: LONG ≤ 5, SHORT ≥ 3 (1:1 趋势)

-- 2. 低价股交易数 (期望 0)
SELECT symbol, entry_price, COUNT(*) FROM trade_records 
WHERE trade_date=date('now','localtime') AND entry_price<20
GROUP BY symbol;
-- 期望: 0 行

-- 3. <6min 持仓占比 (期望 < 10%)
SELECT 
  CASE WHEN holding_hours < 0.1 THEN 'fast' ELSE 'normal' END b,
  COUNT(*) n
FROM trade_records WHERE trade_date=date('now','localtime')
GROUP BY b;
-- 5/18: fast 16/15=>百分制 ~100%; 期望今晚: fast ≤ 10%
```

### 3.3 收盘后业绩 (5/20 04:00 ET = 16:00 北京)

```sql
SELECT 
  COUNT(*) n,
  SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
  ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*), 1) winrate,
  ROUND(SUM(realized_pnl), 2) pnl
FROM trade_records 
WHERE trade_date='2026-05-19' AND realized_pnl IS NOT NULL;
-- 6 天历史平均: 7-14% 胜率, 周均 -$300
-- 期望: 胜率 ≥ 15%, PnL ≥ +$200 (Phase 1 预期 +$2,400/周 = +$480/日)
```

---

## 4. 风险与回滚

### 4.1 已知风险

| 风险 | 缓解 |
|---|---|
| **A.1**: 删 short_min_strength 后, 极弱反转信号 (s=5) 可能进 | effective_strength<65 兜底; 历史无 s<10 案例 |
| **B.1**: 6min hold 给反弹时间, 但震荡市可能小亏变中亏 | atr_stop 价格穿 SL 路径不变 (真损还会及时退) |
| **B.2**: 排除 < $20 等于放弃 LCID/TME 流动性最好的标的 | 数据驱动 — 这两个标的 21+15=36 笔历史 0% 胜率 |
| **C.1**: regime 误判 (TREND_DOWN 但实际反弹) → 漏 LONG 机会 | regime 判定错误率历史 ~10%, 远好于 LONG 全损 97% |
| **C.2**: 熔断阈值放松 → 真灾难时反应慢 | NOISE_LOSS_THRESHOLD=-0.1% 仅过滤摩擦级噪声 |

### 4.2 回滚 (单点 git revert)

```bash
# 找 Phase 1 commit (5 个)
git log --oneline | head -5
# 全部回滚
git revert <commit-hash-1>...<commit-hash-5> --no-edit
sudo systemctl restart ftseek-us
```

### 4.3 应急切断

如果开盘 30min 内出现下列任一信号 → 立即回滚:
- 任何单笔 PnL < -$500 (类似 5/12 QCOM -$743)
- 30min 内连续 5 笔亏损
- ERROR 级别日志 > 10 条
- TRADE_ENV=1 paper 模式异常 (看到 TRADE_ENV=0 立即停)

---

## 5. 监控仪表盘 (建议每 15min 跑一次)

```bash
#!/bin/bash
# scripts/phase1_dashboard.sh
echo "=== Phase 1 Dashboard $(date) ==="
echo ""
echo "## SHORT 信号通过/拒绝比"
sqlite3 data/rejections.db "SELECT reason_category, COUNT(*) FROM rejections WHERE date(timestamp)=date('now') GROUP BY reason_category;"
echo ""
echo "## 当日交易统计"
sqlite3 data/ftseek_us.db "
SELECT direction, COUNT(*) n, ROUND(SUM(realized_pnl),2) pnl
FROM trade_records WHERE trade_date=date('now','localtime')
GROUP BY direction;"
echo ""
echo "## 低价股拦截 (期望 0)"
sqlite3 data/ftseek_us.db "
SELECT symbol, entry_price FROM trade_records 
WHERE trade_date=date('now','localtime') AND entry_price<20;"
echo ""
echo "## 熔断触发次数 (期望 <= 1)"
journalctl -u ftseek-us --since today | grep -c "CircuitBreaker.*🚨 熔断触发"
```

---

## 6. 一句话总结

> **5 个改动小, 影响大**: A.1 释放 SHORT 信号 (5/12-5/18 错拒 124 笔), B.2 砍 LCID/TME (21+15=36 笔 0-5% 胜率), C.1 阻止熊市做多 (LONG 73 笔 2.7% 胜率), C.2 + B.1 减少摩擦/熔断浪费. **236/236 测试全绿, 净改 +30 行**.
>
> **预期**: 7 天累计从周均 -$300 → +$2,100 (+$2,400 改善). 关键监控: SHORT 通过率, <6min 持仓占比, 低价股 0 笔, 熔断 ≤ 1/日.
