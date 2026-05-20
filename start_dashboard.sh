#!/bin/bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
exec python -m uvicorn app.web_dashboard.app:create_app --host 0.0.0.0 --port 8899
