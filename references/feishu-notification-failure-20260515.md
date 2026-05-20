# Pattern 28: Feishu Notification Channel Failure (2026-05-15)

## Symptom
`❌ 飞书消息发送失败：field validation failed` in logs, followed by `[CriticalNotifier] 全 5 次重试失败`. All critical alerts (daily loss guard, Watchdog, etc.) silently fail — user receives NO notifications.

## Root Cause (Two Layers)

### Layer 1: Code Bug (FIXED 05-15 14:55 CST)
`FeishuChatService` in `app/reports/feishu_push_service.py` had three bugs:
1. **Missing `send_text()` method** — `critical_alerts.py` checks `hasattr(service, "send_text")` but only `send_post_message()` existed → always returns False
2. **`content` field passed as Python dict** instead of JSON string → Lark API field validation error
3. **`receive_id_type`/`receive_id` set on wrong builder objects** (body vs request)

**Fix applied**: Added `send_text()` wrapper, `json.dumps(content_obj)`, moved `receive_id_type` to request builder, `receive_id` to body builder.

### Layer 2: Config Issue (UNRESOLVED)
After code fix, actual API error is `230002: Bot/User can NOT be out of the chat.` The Feishu bot (`cli_a94874620b38dbc8`) has been removed from the configured chat (`oc_e3ec1a1147cf66f8c32ebe465778b340`).

**User must re-add bot to chat or update `FEISHU_CHAT_ID` in `.env.us`.**

## Diagnostic
```bash
cd /opt/ftseek/FTSeek-US
venv/bin/python3 -c "
import os, dotenv; dotenv.load_dotenv('.env.us')
from app.reports.feishu_push_service import get_feishu_push_service
svc = get_feishu_push_service()
print(svc.send_text('test') if svc else 'No service')
"
# If False → check logs for API error code
tail -20 logs/ftseek_us.log | grep "飞书"
```

## Severity
**P1** — critical alerts undelivered. System continues trading but user is blind to emergencies.

## Fix Code Changes Applied
```diff
# app/reports/feishu_push_service.py

# 1. Added import json
+import json

# 2. Added send_text method
+def send_text(self, text: str) -> bool:
+    return self.send_post_message("关键告警", [text])

# 3. Fixed content to be JSON string
-body_builder.content = content_obj
+body_builder.content = json.dumps(content_obj)

# 4. Fixed builder attribute placement
-body_builder.receive_id_type = "chat_id"
-body_builder.receive_id = self.chat_id
 body_builder.msg_type = "POST"
 body_builder.content = json.dumps(content_obj)

 request = CreateMessageRequest.builder() \
+    .receive_id_type("chat_id") \
     .request_body(body_builder.build()) \
     .build()
```
