# 周一开盘准备报告 (2026-06-02)

**生成时间**: 2026-06-01 20:15 CST (周日晚上)

---

## 系统状态总结

### ✅ 所有检查通过

| 检查项 | 状态 | 详情 |
|--------|------|------|
| 系统健康 | ✅ | 服务运行正常，健康检查通过 |
| 持仓风险 | ✅ | 3 个 SHORT 持仓，符合 RFC-055 策略 |
| 市场状态 | ✅ | VIX=15.74 < 22，允许交易 |
| RFC-055 配置 | ✅ | 所有 15 项配置已生效 |
| ORB 状态 | ✅ | observe 模式运行中 |
| 信号管道 | ✅ | 最近 7 天 35 个信号 |

---

## 当前持仓

| 标的 | 方向 | 数量 | 入场价 | 持仓价值 | Trace ID |
|------|------|------|--------|----------|----------|
| US.SNDK | SHORT | 30 | $1682.34 | $50,470.20 | US-20260529-FB9EED |
| US.QQQ | SHORT | 60 | $737.67 | $44,260.20 | US-20260529-AF927C |
| US.RIVN | SHORT | 30 | $16.25 | $487.65 | US-20260529-E51DC6 |

**总持仓价值**: $95,218.05  
**持仓方向**: 100% SHORT（符合 RFC-055 SHORT-first 策略）

---

## RFC-055 配置验证

### Phase 1: 止损止血 (5 项)
- ✅ 最小持仓时间: 20min
- ✅ 时间衰减: 90min
- ✅ 黑名单: QCOM/LCID/TME/NVDA/DIA/LMT/UNH/NFLX/ARM/AMD/BWXT
- ✅ VIX 门禁: 22
- ✅ LONG 信心阈值: 0.90

### Phase 2: 退出优化 (3 项)
- ✅ Pre-Close 窗口: 8min
- ✅ Chandelier 激活: 0.6 ATR
- ✅ Scale Out: 2 层

### Phase 3: SHORT 优先 (3 项)
- ✅ Net exposure 偏空
- ✅ SHORT 信心: 0.55
- ✅ 最大持仓: 5

### Phase 4: VIX 门禁 (4 项)
- ✅ VIX 阈值: 22
- ✅ LONG 阻断: 0.90
- ✅ 黑名单扩充
- ✅ 信号质量门禁

---

## 自动化监控已设置

### Cron 任务

| 任务 | 时间 | 说明 |
|------|------|------|
| RFC-055 周一验证 | 周二 04:05 (北京) | 周一收盘后首日验证 |
| RFC-055 每日监控 | 周三-六 04:05 (北京) | 每日收盘后验证 |
| RFC-055 周报 | 周六 09:00 | 7 天综合分析 |
| ORB 日志分析 | 每天 18:00 | ORB 观察模式日志 |
| 盘中监控 | 每 15 分钟 (交易时段) | 实时交易监控 |

### 监控日志位置

- `logs/rfc055_monday_verification.log` - 周一验证报告
- `logs/rfc055_daily_monitor.log` - 每日监控
- `logs/rfc055_weekly_report.log` - 周报
- `logs/orb_daily_summary.log` - ORB 日志摘要
- `logs/intraday_monitor.log` - 盘中监控

---

## 周一关注重点

### 1. 交易频率
- **目标**: ≥ 3 笔/天
- **当前**: 0.7 笔/天 (14 笔/月)
- **观察**: 是否有更多 SHORT 信号触发

### 2. 胜率
- **目标**: ≥ 50%
- **回测**: 54.8% (SHORT + 非 HIGH_VOL)
- **早期验证**: 60% (5 笔)
- **观察**: 首日胜率是否达标

### 3. 退出方式
- **目标**: TRAILING_STOP 为主
- **早期验证**: 100% TRAILING_STOP
- **观察**: 退出方式是否符合预期

### 4. ORB 辅线
- **模式**: observe (仅日志)
- **观察**: 是否识别到开盘区间和潜在信号
- **日志**: `logs/ftseek_us.log` 中搜索 `[ORB]`

### 5. 现有持仓
- **SNDK**: 关注是否触发 TRAILING_STOP
- **QQQ**: 关注是否触发 TRAILING_STOP
- **RIVN**: 关注是否触发 TRAILING_STOP

---

## 成功标准

### North Star 目标
- 胜率 ≥ 50%
- 日均收益 ≥ $500

### 周一首日目标
- 有交易发生（验证信号管道）
- 胜率 ≥ 40%（允许首日波动）
- 无重大亏损（单笔 > $500）

### 第一周目标 (6/2-6/6)
- 总交易数 ≥ 10 笔
- 胜率 ≥ 45%
- 周收益 > $0

---

## 应急预案

### 如果首日无交易
1. 检查信号管道日志
2. 检查 VIX 是否 >= 22
3. 检查是否所有标的都在黑名单
4. 考虑放宽部分过滤条件

### 如果胜率 < 40%
1. 运行 `scripts/verify_rfc055.py --days 1` 详细分析
2. 检查哪些标的亏损
3. 考虑添加更多标的到黑名单
4. 考虑收紧 SHORT 信心阈值到 0.60

### 如果日均收益 < $100
1. 分析交易频率是否足够
2. 检查单笔收益是否达标
3. 考虑放宽信号过滤条件
4. 考虑 ORB 切 shadow 模式增加交易频率

---

## 手动检查命令

### 周一收盘后手动验证
```bash
# 1. 运行首日验证
python3 scripts/verify_rfc055.py --days 1

# 2. 查看今日交易
sqlite3 data/ftseek_us.db "SELECT * FROM trade_records WHERE DATE(entry_time) = DATE('now');"

# 3. 查看 ORB 日志
grep "\[ORB\]" logs/ftseek_us.log | tail -50

# 4. 查看盘中监控
tail -100 logs/intraday_monitor.log

# 5. 检查当前持仓
sqlite3 data/ftseek_us.db "SELECT * FROM open_positions WHERE sync_status IS NULL;"
```

### 实时监控
```bash
# 盘中实时查看交易
tail -f logs/ftseek_us.log | grep -i "trade\|order\|signal"

# 查看 ORB 实时日志
tail -f logs/ftseek_us.log | grep "\[ORB\]"

# 查看退出监控
tail -f logs/ftseek_us.log | grep "exit\|trailing\|stop"
```

---

## 相关文档

- `RFC055_IMPLEMENTATION_REPORT.md` - 详细实施报告
- `docs/decisions/RFC-055-*.md` - 决策文档
- `docs/decisions/RFC-056-orb-auxiliary-pipeline.md` - ORB 设计文档
- `scripts/pre_market_checklist.py` - 开盘前检查脚本
- `scripts/verify_rfc055.py` - RFC-055 验证脚本
- `scripts/market_monitor_daemon.py` - 盘中监控脚本

---

## 下一步行动

### 周一 (6/2)
1. ✅ 自动监控已设置，等待收盘后验证
2. 手动检查 `logs/rfc055_monday_verification.log`
3. 分析首日表现，决定是否需要调整

### 周二 (6/3)
1. 查看 `logs/rfc055_daily_monitor.log`
2. 对比周一和周二的表现
3. 如果表现良好，考虑 ORB 切 shadow

### 周三-周五 (6/4-6/6)
1. 每日查看监控日志
2. 累积数据，分析趋势
3. 准备周末的周报分析

### 周六 (6/7)
1. 查看 `logs/rfc055_weekly_report.log`
2. 评估第一周整体表现
3. 决定下一步优化方向

---

**系统已完全准备好，等待周一开盘验证！** 🚀
