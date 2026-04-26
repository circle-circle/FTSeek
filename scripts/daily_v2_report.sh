#!/bin/bash
# 每日 16:30 生成 V2 管线对比报告
cd /opt/ftseek/FTSeek
source /root/.hermes/hermes-agent/venv/bin/activate 2>/dev/null

python3 -c "
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')
from app.news_v2.collector import V2DataCollector
c = V2DataCollector()
c.flush()
path = c.save_report()
print(f'✅ 报告已保存: {path}')

# 输出报告内容
print(c.get_daily_report())
"
