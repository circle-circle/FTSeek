# FTSeek v2.0 快速启动指南

**5 分钟完成部署**

---

## 🚀 一键启动

```bash
# 1. 设置环境变量
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export FTSEEK_DEV_MODE=1

# 2. 进入项目目录
cd /opt/ftseek/FTSeek

# 3. 初始化数据库
python3 -c "from app.storage.database import init_database; init_database()"

# 4. 运行测试 (可选)
python3 test_v2.py

# 5. 启动系统
./run.sh
```

---

## 📝 配置监控股票

编辑 `config/watchlist.json`:

```json
{
  "hk_stocks": [
    {"code": "HK.00700", "name": "腾讯控股", "enabled": true},
    {"code": "HK.09988", "name": "阿里巴巴", "enabled": true}
  ],
  "us_stocks": [
    {"code": "US.AAPL", "name": "苹果公司", "enabled": true},
    {"code": "US.TSLA", "name": "特斯拉", "enabled": true}
  ]
}
```

---

## 🔧 可选配置

### 调整信号融合权重

```bash
export TECHNICAL_WEIGHT=0.6      # 技术指标权重 (默认 60%)
export SENTIMENT_WEIGHT=0.4      # 情绪权重 (默认 40%)
```

### 调整频率限制

```bash
export PER_STOCK_DAILY_LIMIT=3   # 单股每日最多信号数 (默认 3)
export GLOBAL_DAILY_LIMIT=20     # 全局每日最多信号数 (默认 20)
```

### 调整信号有效期

```bash
export SIGNAL_EXPIRE_SECONDS=900  # 信号有效期 (秒，默认 15 分钟)
```

---

## 📊 验证功能

### 1. 检查数据库
```bash
sqlite3 data/ftseek.db ".tables"
# 应显示：signals trades news performance_snapshots rate_limits
```

### 2. 检查策略注册
```bash
export API_TOKEN=test123
python3 -c "from app.strategy import STRATEGY_REGISTRY; print(list(STRATEGY_REGISTRY.keys()))"
# 应包含：fusion_v2
```

### 3. 测试新闻抓取
```bash
python3 -c "
from app.data.news_fetcher import get_news_fetcher
fetcher = get_news_fetcher()
news = fetcher.fetch('HK.00700')
print(f'获取 {len(news)} 条新闻')
"
```

### 4. 测试信号融合
```bash
python3 -c "
from app.signal.fusion_engine import get_fusion_engine
engine = get_fusion_engine()
indicators = {
    'current_price': 520, 'rsi': 35, 'macd': 2.5,
    'ma_fast': 515, 'ma_slow': 500,
    'volume': 1500000, 'volume_avg': 1000000,
    'bollinger_upper': 540, 'bollinger_lower': 500
}
result = engine.generate_signal('HK.00700', '腾讯', 520, indicators)
print(f'信号等级：{result.signal_level if result else \"无\"}')
"
```

---

## 🔔 钉钉推送配置

### 1. 创建钉钉机器人
1. 打开钉钉群 → 群设置 → 智能群助手
2. 添加机器人 → 自定义
3. 复制 Webhook URL

### 2. 设置环境变量
```bash
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=XXX"
export DINGTALK_SECRET="SECXXX"
```

### 3. 测试推送
```bash
python3 -c "
from app.notifier.dingtalk import DingTalkNotifier
notifier = DingTalkNotifier()
notifier.send_text('FTSeek v2.0 测试消息')
"
```

---

## 📈 查看绩效

### 周度报告
```bash
python3 -c "
from app.performance.stats import get_performance_reporter
reporter = get_performance_reporter()
report = reporter.generate_weekly_report()
print(report.summary())
"
```

### 目标追踪
```bash
python3 -c "
from app.performance.stats import get_performance_reporter
reporter = get_performance_reporter()
tracking = reporter.get_target_tracking()
print(f'连续达标月数：{tracking[\"current_streak\"]}')
print(f'目标胜率：{tracking[\"target\"]:.0%}')
"
```

---

## 🐛 常见问题

### Q1: 数据库初始化失败
```bash
# 删除旧数据库重新初始化
rm data/ftseek.db
python3 -c "from app.storage.database import init_database; init_database()"
```

### Q2: 策略未注册
```bash
# 检查环境变量
export API_TOKEN=test123
export FTSEEK_DEV_MODE=1

# 手动注册
python3 -c "
from app.strategy import STRATEGY_REGISTRY
from app.strategy.fusion_v2 import FusionV2Strategy
STRATEGY_REGISTRY['fusion_v2'] = FusionV2Strategy
print('注册成功')
"
```

### Q3: 新闻抓取失败
- 检查网络连接
- 检查是否需要代理
- 可能是 API 临时限制，稍后重试

### Q4: 钉钉推送失败
- 检查 Webhook URL 是否正确
- 检查 Secret 是否匹配
- 确认机器人已启用

---

## 📚 更多文档

- **升级指南**: `docs/UPGRADE_GUIDE.md`
- **改造方案**: `docs/UPGRADE_PLAN_V2.md`
- **完成总结**: `docs/COMPLETION_SUMMARY.md`
- **系统配置**: `config/system_config.yaml`

---

## ✅ 启动检查清单

- [ ] 环境变量已设置 (API_TOKEN)
- [ ] 数据库已初始化
- [ ] 监控股票已配置
- [ ] 钉钉机器人已配置 (可选)
- [ ] 测试通过

**全部完成后，运行 `./run.sh` 启动系统!**

---

**祝使用愉快!** 🎉
