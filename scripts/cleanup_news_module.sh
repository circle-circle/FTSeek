#!/bin/bash
# FTSeek 新闻模块清理脚本

set -e

echo "=== 开始清理新闻推送模块 ==="

cd /opt/ftseek/FTSeek

# 1. 删除新闻相关脚本文件
echo "🗑️  删除新闻脚本..."
rm -f scripts/daily_news_report.py
rm -f scripts/news_push.service
rm -f scripts/news_digest.py
rm -f scripts/start_news_push.sh
rm -f scripts/test_news_sources.py
rm -f test_news_push.py
rm -f scripts/demo_push_formats.py

# 2. 删除新闻分析模块
echo "🗑️  删除新闻分析模块..."
rm -rf app/analyzers/
rm -rf app/data/news*
rm -rf app/data/multi_source_news.py
rm -rf app/reporters/news_digest.py

# 3. 删除测试文件
echo "🗑️  删除新闻测试..."
rm -f tests/test_news_classifier.py
rm -f tests/test_news_sources.py

# 4. 删除文档
echo "🗑️  删除新闻文档..."
rm -f docs/plans/2026-03-01-news-enhancement-plan.md

# 5. 清理日志
echo "🗑️  清理新闻日志..."
rm -f logs/news*.log
rm -f app/data/news_push_state.json

# 6. 清理 crontab
echo "🗑️  清理 crontab 新闻任务..."
crontab -l | grep -v "news" | grep -v "^$" > /tmp/crontab_clean.txt || true
crontab /tmp/crontab_clean.txt

echo "✅ 新闻推送模块已完全移除！"
echo ""
echo "📝 注意：以下文件仍引用新闻模块，需要手动处理："
echo "   - app/signal_engine/fusion_engine.py"
echo "   - app/strategies/fusion_v2.py"
echo "   - app/notifier/dingtalk.py (send_news_digest 方法)"
echo "   - app/data/__init__.py"
echo "   - scripts/strategy_monitor.py"
