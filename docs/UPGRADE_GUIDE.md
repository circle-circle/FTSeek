# FTSeek v2.0 升级指南

**升级日期**: 2026-02-27  
**版本**: v1.x → v2.0  
**影响**: 重大升级 (新增模块 + 数据库)

---

## 📋 升级清单

### 新增模块

| 模块 | 路径 | 功能 |
|------|------|------|
| storage | app/storage/ | SQLite 数据存储 |
| data | app/data/ | 新闻抓取 |
| signal | app/signal/ | 信号融合引擎 |
| limiter | app/limiter/ | 频率限制器 |
| performance | app/performance/ | 绩效统计 |

### 升级模块

| 模块 | 变更 |
|------|------|
| notifier/dingtalk.py | 增加延期按钮 (三按钮) |
| server.py | 增加 /api/defer 接口 |
| config.py | 新增配置项 |

### 新增配置

| 文件 | 用途 |
|------|------|
| config/watchlist.json | 监控股票列表 |
| config/backtest_presets.json | 回测预设 |
| config/system_config.yaml | 系统配置 |

---

## 🚀 升级步骤

### 1. 备份现有配置

```bash
cd /opt/ftseek
cp -r FTSeek FTSeek.backup.$(date +%Y%m%d)
```

### 2. 安装新增依赖

```bash
cd /opt/ftseek/FTSeek
pip3 install aiohttp beautifulsoup4 lxml
```

### 3. 初始化数据库

```bash
# 方法 1: 启动时自动初始化 (推荐)
./run.sh

# 方法 2: 手动初始化
python3 -c "from app.storage.database import init_database; init_database(); print('数据库初始化完成')"
```

### 4. 配置监控股票

编辑 `config/watchlist.json`:

```json
{
  "hk_stocks": [
    {"code": "HK.00700", "name": "腾讯控股", "enabled": true}
  ],
  "us_stocks": [
    {"code": "US.AAPL", "name": "苹果公司", "enabled": true}
  ]
}
```

### 5. 配置环境变量

编辑 `.env` 文件或 systemd 服务:

```bash
# 新增配置项
export SIGNAL_EXPIRE_SECONDS=900          # 15 分钟
export PER_STOCK_DAILY_LIMIT=3            # 单股日限制
export GLOBAL_DAILY_LIMIT=20              # 全局日限制
export TECHNICAL_WEIGHT=0.6               # 技术权重
export SENTIMENT_WEIGHT=0.4               # 情绪权重
```

### 6. 重启服务

```bash
# systemd 方式
systemctl restart ftseek

# 手动方式
./run.sh
```

---

## ✅ 验证升级

### 1. 检查数据库

```bash
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db ".tables"
# 应显示：signals, trades, news, performance_snapshots, rate_limits
```

### 2. 测试新闻抓取

```bash
python3 -c "
from app.data.news_fetcher import get_sync_news_fetcher
fetcher = get_sync_news_fetcher()
news = fetcher.fetch('HK.00700')
print(f'获取 {len(news)} 条新闻')
for n in news[:3]:
    print(f'  - {n.title[:50]} (score={n.sentiment_score:.2f})')
"
```

### 3. 测试信号融合

```bash
python3 -c "
from app.signal.fusion_engine import get_fusion_engine
engine = get_fusion_engine()

indicators = {
    'current_price': 520.0,
    'rsi': 35,
    'macd': 2.5,
    'macd_signal': 2.0,
    'macd_histogram': 0.5,
    'ma_fast': 515,
    'ma_slow': 500,
    'volume': 1500000,
    'volume_avg': 1000000,
    'bollinger_upper': 540,
    'bollinger_middle': 520,
    'bollinger_lower': 500
}

result = engine.generate_signal(
    code='HK.00700',
    name='腾讯控股',
    price=520.0,
    indicators=indicators
)

if result:
    print(f'信号：{result.code} {result.action.value}')
    print(f'等级：{result.signal_level}')
    print(f'置信度：{result.confidence:.2f}')
    print(f'技术分：{result.technical_score:.2f}')
    print(f'情绪分：{result.sentiment_score:.2f}')
else:
    print('信号太弱，未生成')
"
```

### 4. 测试频率限制

```bash
python3 -c "
from app.limiter.rate_limiter import get_rate_limiter
limiter = get_rate_limiter()

for i in range(5):
    result = limiter.check('HK.00700', 'BUY')
    print(f'检查 {i+1}: allowed={result.allowed}, count={result.current_count}')
"
```

### 5. 测试绩效统计

```bash
python3 -c "
from app.performance.stats import get_performance_reporter
reporter = get_performance_reporter()

# 生成周报
report = reporter.generate_weekly_report()
print(report.summary())

# 目标追踪
tracking = reporter.get_target_tracking()
print(f'连续达标月数：{tracking[\"current_streak\"]}')
"
```

### 6. 测试钉钉三按钮

发送测试信号后，检查钉钉消息是否包含三个按钮:
- ✅ 确认执行
- ⏰ 延期 15 分钟
- ❌ 忽略

---

## 📊 新功能使用说明

### 1. 自定义股票列表

编辑 `config/watchlist.json`，添加新股票:

```json
{
  "hk_stocks": [
    {"code": "HK.00700", "name": "腾讯", "enabled": true},
    {"code": "HK.09988", "name": "阿里", "enabled": true},
    {"code": "HK.XXXXX", "name": "新股", "enabled": true}  // 新增
  ]
}
```

重启服务生效。

### 2. 回测周期选择

访问回测 API 时指定 preset:

```bash
curl "http://localhost:8899/api/backtest/run?strategy=fusion&preset=6_months&token=YOUR_TOKEN"
```

可用 preset: `3_months`, `6_months`, `1_year`, `2_years`, `max`

### 3. 绩效报告查看

```bash
# 周报
curl "http://localhost:8899/api/performance/weekly?token=YOUR_TOKEN"

# 月报
curl "http://localhost:8899/api/performance/monthly?token=YOUR_TOKEN"

# 目标追踪
curl "http://localhost:8899/api/performance/tracking?token=YOUR_TOKEN"
```

### 4. 信号延期

在钉钉消息中点击 "⏰ 延期 15 分钟" 按钮，信号有效期将延长 15 分钟。

可自定义延期时长:
```
/api/defer?signal_id=XXX&token=XXX&minutes=30
```

---

## 🔧 故障排查

### 问题 1: 数据库初始化失败

**症状**: 启动时报错 "no such table"

**解决**:
```bash
# 删除旧数据库
rm /opt/ftseek/FTSeek/data/ftseek.db

# 重启服务
systemctl restart ftseek
```

### 问题 2: 新闻抓取失败

**症状**: 情绪评分始终为 0

**解决**:
```bash
# 测试网络连接
curl -I https://feed.mix.sina.com.cn

# 检查代理配置
export HTTP_PROXY=http://your-proxy:port
export HTTPS_PROXY=http://your-proxy:port
```

### 问题 3: 频率限制过早触发

**症状**: 单股未到 3 次就限制

**解决**:
```bash
# 检查数据库计数
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db \
  "SELECT * FROM rate_limits WHERE date = date('now');"

# 手动重置
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db \
  "DELETE FROM rate_limits WHERE code = 'HK.00700';"
```

### 问题 4: 钉钉按钮不显示

**症状**: 消息只有文字没有按钮

**解决**:
1. 检查钉钉机器人配置是否为 "自定义机器人"
2. 确认 Webhook URL 和 Secret 正确
3. 检查消息格式是否为 actionCard

---

## 📈 性能优化建议

### 1. 数据库优化

```sql
-- 定期清理旧数据
DELETE FROM news WHERE fetched_at < datetime('now', '-90 days');
DELETE FROM rate_limits WHERE date < date('now', '-7 days');

-- 优化数据库
VACUUM;
```

### 2. 新闻抓取优化

- 调整抓取频率 (默认每小时)
- 减少回溯天数 (默认 3 天)
- 使用缓存避免重复抓取

### 3. 信号融合优化

- 调整权重比例 (默认 60/40)
- 设置最小置信度阈值
- 增加策略过滤条件

---

## 🎯 验收标准

| 功能 | 验收方法 | 预期结果 |
|------|----------|----------|
| 数据存储 | 查询数据库 | 信号/交易记录存在 |
| 新闻抓取 | 测试接口 | 返回新闻列表 |
| 信号融合 | 生成信号 | 包含技术 + 情绪分 |
| 频率限制 | 连续触发 | 达到限制后拦截 |
| 钉钉三按钮 | 发送消息 | 三个按钮正常显示 |
| 绩效统计 | 生成报告 | 包含胜率等指标 |
| 回测预设 | 选择周期 | 按预设天数回测 |

---

## 📞 技术支持

遇到问题请查看:
- 日志：`/opt/ftseek/FTSeek/logs/ftseek.log`
- 文档：`/opt/ftseek/FTSeek/docs/`
- 配置：`/opt/ftseek/FTSeek/config/`

---

**升级完成时间**: ___________  
**验收人**: ___________  
**验收结果**: ✅ 通过 / ❌ 失败
