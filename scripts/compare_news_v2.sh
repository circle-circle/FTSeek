#!/bin/bash
# FTSeek 新闻管线 V2 — 混合模式对比报告

echo "================================================================"
echo "  FTSeek 新闻管线 V2 — 混合模式对比报告"
echo "================================================================"
echo ""

DATA_DIR="$HOME/.hermes/ftseek/data/v2_collection"
TODAY=$(date +%Y-%m-%d)

echo "=== 服务状态 ==="
systemctl status ftseek-news-v2 --no-pager -l 2>&1 | head -10

echo ""
echo "=== 最近日志 ==="
journalctl -u ftseek-news-v2 --no-pager -n 10 2>/dev/null | grep -E 'V2\]|Pipeline'

echo ""
echo "=== 数据收集状态 ==="
if [ -f "$DATA_DIR/$TODAY.json" ]; then
    python3 -c "
import json, os
path = '$DATA_DIR/$TODAY.json'
with open(path) as f:
    d = json.load(f)
s = d['stats']
items = d['items']
total = s['total_received']
passed = s['total_passed']
rejected = s['total_rejected']
print(f'  日期: {d["date"]}')
print(f'  总接收: {total} 条')
print(f'  通过: {passed} 条 ({passed/max(1,total)*100:.0f}%)')
print(f'  拒绝: {rejected} 条 ({rejected/max(1,total)*100:.0f}%)')
print(f'  地缘事件: {s["geo_events"]} 条')
print(f'  紧急新闻: {s["immediate_news"]} 条')
print(f'  错误: {s["errors"]} 次')
print()
print(f'  拒绝原因:')
for r, c in sorted(s['rejected_by_reason'].items(), key=lambda x: -x[1]):
    print(f'    {r}: {c}')
print()
print(f'  最近通过新闻:')
for i, item in enumerate(items[-10:], 1):
    if item['passed']:
        print(f'    {i}. [{item["urgency"]}] {item["source"]}: {item["title"][:50]}')
"
else
    echo "  当日数据文件尚未生成 (等待首次采集)"
fi

echo ""
echo "=== 每日报告 ==="
if [ -f "$DATA_DIR/report_$TODAY.md" ]; then
    cat "$DATA_DIR/report_$TODAY.md"
else
    echo "  报告尚未生成 (等待 10 次采集周期后自动生成)"
    echo "  可手动触发: python3 -c 'from app.news_v2.collector import V2DataCollector; c=V2DataCollector(); print(c.get_daily_report())'"
fi

echo ""
echo "=== 与现有系统对比 ==="
echo "  V2 管线: 观察模式中 (收集数据，不推送)"
echo "  现有系统: ftseek-news-listener + rsshub_multi_source_cron (正常运行)"
echo ""
echo "  对比维度:"
echo "    - 有效新闻占比: V2 vs 现有"
echo "    - 延迟: V2 (记录中) vs 现有"
echo "    - 地缘事件检出: V2 (自动标记) vs 现有"
echo "    - 去重效果: V2 (SimHash) vs 现有 (精确哈希)"
echo ""
echo "================================================================"
