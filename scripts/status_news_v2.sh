#!/bin/bash
echo "=== FTSeek 新闻管线 V2 状态 ==="
systemctl status ftseek-news-v2 --no-pager -l 2>/dev/null

echo ""
echo "=== 最近日志 (最后 20 行) ==="
journalctl -u ftseek-news-v2 --no-pager -n 20 2>/dev/null

echo ""
echo "=== 日志文件 ==="
ls -la /opt/ftseek/FTSeek/logs/news_pipeline_v2.log 2>/dev/null

echo ""
echo "=== 管线统计 (如果日志存在) ==="
if [ -f /opt/ftseek/FTSeek/logs/news_pipeline_v2.log ]; then
    echo "总采集: $(grep -c '总采集' /opt/ftseek/FTSeek/logs/news_pipeline_v2.log 2>/dev/null || echo 0) 次"
    echo "通过: $(grep -o '通过=[0-9]*' /opt/ftseek/FTSeek/logs/news_pipeline_v2.log | tail -1)"
    echo "拒绝: $(grep -o '拒绝=[0-9]*' /opt/ftseek/FTSeek/logs/news_pipeline_v2.log | tail -1)"
fi
