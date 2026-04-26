#!/bin/bash
# FTSeek 新闻管线 V2 启动脚本 (观察模式)
# 用途: 与现有 RSSHubMultiSourceFetcher 并行运行，对比效果

echo "========================================"
echo "  FTSeek 新闻管线 V2 — 启动 (观察模式)"
echo "========================================"
echo "时间: $(date)"
echo "模式: observe (只记录不推送)"
echo "间隔: 180秒"
echo "========================================"

cd /opt/ftseek/FTSeek
source /root/.hermes/hermes-agent/venv/bin/activate 2>/dev/null

python3 -m app.news_v2.main --mode observe --interval 180 --log-level INFO
