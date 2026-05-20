# FTSeek-US 执行摘要

**日期**: 2026-03-26  
**状态**: 优先级 1 改进已完成

---

## 🎯 核心定位

**FTSeek-US** = 美股中长线持仓监控器

**FTSeek** = 港股日内高频交易系统

**分工**:
- FTSeek 专注港股 (09:30-16:00)
- FTSeek-US 专注美股 (21:30-次日 04:00)
- 互相借鉴策略，独立运行

---

## ✅ 已完成 (优先级 1)

| 项目 | 状态 | 说明 |
|------|------|------|
| **富途 API 集成** | ✅ | 可读取持仓、执行交易 |
| **备用数据源** | ✅ | TwelveData/Finnhub/AlphaVantage |
| **自动止损止盈** | ✅ | 触发条件自动平仓 |
| **股票池统一** | ✅ | 16 只股票 (10 美股 +4 港股 +2ETF) |
| **监控器升级** | ✅ | 集成 Fallback 机制 |

---

## 📊 当前状态

### 实际交易记录

```
港股 (FTSeek):
  HK.00981 (中芯国际): 169 笔
  HK.03690 (美团-W): 152 笔
  HK.09992 (泡泡玛特): 76 笔
  HK.09988 (阿里巴巴): 10 笔

美股 (FTSeek-US):
  US.TSLA (特斯拉): 2 笔 (测试单)
```

### 监控能力

- ✅ 实时股价查询 (富途 + 备用数据源)
- ✅ 持仓同步 (富途 API)
- ✅ 止损止盈检查 (自动)
- ✅ 钉钉通知 (触发时推送)
- ✅ 跨时段监控 (24 小时)

---

## 📋 下一步行动

### 本周

1. ⏳ **测试自动平仓** (模拟盘)
   ```bash
   # 创建模拟持仓
   # 触发止损条件
   # 验证订单执行
   ```

2. ⏳ **清理代码**
   - 移除未使用的多 Agent 代码
   - 简化 scheduler.py

3. ⏳ **监控运行**
   - 观察配额使用
   - 检查 Fallback 日志

### 下周

1. **移植 FTSeek 策略**
   ```bash
   cp /opt/ftseek/FTSeek/strategies/*.py \
      /opt/ftseek/FTSeek-US/strategies/
   # 适配美股参数
   ```

2. **添加基本面分析**
   - 财报日期监控
   - PE/EPS 数据

---

## 📁 关键文件

### 配置

- `config/us_stock_config.yaml` - 止损止盈参数
- `config/watchlist_us.json` - 监控股票池
- `.env.us` - API Keys

### 代码

- `app/us_stock_monitor.py` - 监控器 (已升级)
- `app/futu_integration.py` - 富途 API 集成
- `app/market_data_backup.py` - 备用数据源

### 文档

- `docs/POSITIONING_AND_LOGIC.md` - 定位文档 ⭐ 新增
- `docs/BACKUP_DATA_SOURCE_GUIDE.md` - 备用数据源指南
- `QUICKSTART_v2.md` - 快速启动指南

---

## 🚀 快速启动

```bash
# 测试连接
cd /opt/ftseek/FTSeek-US
python3 scripts/test_futu_integration.py
python3 scripts/test_backup_sources.py

# 启动监控
python3 main.py --start

# 查看状态
python3 main.py --status
tail -f logs/ftseek_us.log
```

---

## ⚠️ 注意事项

### 配额管理

- TwelveData: 800 次/天 (已用 <10%)
- Finnhub: 60 次/分钟
- Alpha Vantage: 500 次/天

**安全边际**: 28% ✅

### 自动交易

- 当前模式：**SIMULATE** (模拟盘)
- 切换到实盘：编辑 `.env.us` → `FUTU_TRD_ENV=REAL`
- 实盘需先在 OpenD GUI 解锁交易密码

---

## 📞 支持

- **项目路径**: `/opt/ftseek/FTSeek-US`
- **日志**: `logs/ftseek_us.log`
- **文档**: `docs/` 目录

---

**报告生成**: 2026-03-26 08:00  
**下次检查**: 2026-03-27 (监控配额使用)
