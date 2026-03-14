#!/bin/bash
# FTSeek v2.0 初始化脚本

set -e

echo "========================================"
echo "  FTSeek v2.0 初始化"
echo "========================================"
echo ""

cd /opt/ftseek/FTSeek

# 1. 检查 Python 版本
echo "📌 检查 Python 版本..."
python3 --version

# 2. 安装新增依赖
echo ""
echo "📦 安装新增依赖..."
pip3 install -q aiohttp beautifulsoup4 lxml 2>/dev/null || {
    echo "⚠️  依赖安装失败，请手动执行：pip3 install aiohttp beautifulsoup4 lxml"
}

# 3. 初始化数据库
echo ""
echo "🗄️  初始化数据库..."
python3 -c "
from app.storage.database import init_database
db = init_database()
print('✅ 数据库初始化完成')
print(f'   路径：{db.db_path}')
"

# 4. 测试新闻抓取
echo ""
echo "📰 测试新闻抓取..."
python3 -c "
from app.data.news_fetcher import get_sync_news_fetcher
fetcher = get_sync_news_fetcher()
news = fetcher.fetch('HK.00700')
print(f'✅ 获取 {len(news)} 条新闻')
if news:
    print(f'   示例：{news[0].title[:50]}')
" || echo "⚠️  新闻抓取测试失败 (可能是网络问题)"

# 5. 测试信号融合
echo ""
echo "🧠 测试信号融合..."
python3 -c "
from app.signal.fusion_engine import get_fusion_engine
engine = get_fusion_engine()

indicators = {
    'current_price': 520.0,
    'rsi': 35,
    'macd': 2.5,
    'macd_signal': 2.0,
    'ma_fast': 515,
    'ma_slow': 500,
    'volume': 1500000,
    'volume_avg': 1000000,
    'bollinger_upper': 540,
    'bollinger_lower': 500
}

result = engine.generate_signal('HK.00700', '腾讯控股', 520.0, indicators)
if result:
    print(f'✅ 生成信号：{result.code} {result.action.value}')
    print(f'   等级：{result.signal_level}')
    print(f'   置信度：{result.confidence:.2f}')
else:
    print('⚠️  信号太弱，未生成 (正常)')
"

# 6. 测试频率限制
echo ""
echo "🚦 测试频率限制..."
python3 -c "
from app.limiter.rate_limiter import get_rate_limiter
limiter = get_rate_limiter()
result = limiter.check('HK.TEST', 'BUY')
print(f'✅ 限频器正常：剩余{result.limit - result.current_count}/{result.limit}')
"

# 7. 测试绩效统计
echo ""
echo "📊 测试绩效统计..."
python3 -c "
from app.performance.stats import get_performance_reporter
reporter = get_performance_reporter()
tracking = reporter.get_target_tracking()
print(f'✅ 绩效追踪正常')
print(f'   连续达标月数：{tracking[\"current_streak\"]}')
"

# 8. 显示配置信息
echo ""
echo "⚙️  配置信息..."
echo "   数据库：/opt/ftseek/FTSeek/data/ftseek.db"
echo "   监控股票：/opt/ftseek/FTSeek/config/watchlist.json"
echo "   回测预设：/opt/ftseek/FTSeek/config/backtest_presets.json"

echo ""
echo "========================================"
echo "  ✅ 初始化完成!"
echo "========================================"
echo ""
echo "下一步:"
echo "  1. 编辑 config/watchlist.json 配置监控股票"
echo "  2. 设置环境变量 (参考 .env.example)"
echo "  3. 运行 ./run.sh 启动系统"
echo ""
