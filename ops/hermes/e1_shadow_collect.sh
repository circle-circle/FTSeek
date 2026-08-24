#!/bin/bash
# DEB-20260815 E1 前瞻影子臂采集 wrapper (cron no_agent)
cd /opt/ftseek/FTSeek-US
PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python /opt/ftseek/FTSeek-US/venv/bin/python3.11 scripts/e1_shadow_collector.py
