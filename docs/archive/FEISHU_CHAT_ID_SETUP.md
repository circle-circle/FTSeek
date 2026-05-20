# 飞书会话 ID 配置指南

**日期**: 2026-03-26  
**状态**: ⏳ 需要配置会话 ID

---

## 一、为什么需要会话 ID

FTSeek-US 使用飞书 API 推送消息，需要知道发送到哪个会话 (群聊或单聊)。

**配置要求**:
- ✅ FEISHU_APP_ID (已配置)
- ✅ FEISHU_APP_SECRET (已配置)
- ⏳ FEISHU_CHAT_ID (需要配置)

---

## 二、获取会话 ID 的方法

### 方法 1: 从飞书群聊界面获取 (推荐)

1. **打开飞书**，进入 FTSeek-US 绑定的群聊
2. **点击右上角设置** (群聊详情)
3. **下滑找到 "群聊 ID"** 或 "Chat ID"
4. **复制该 ID** (格式类似：`oc_xxxxxxxxxxxx`)

### 方法 2: 从消息回调获取

如果群聊中已有机器人消息，可以从回调中获取：

```json
{
  "header": {
    "chat_id": "oc_xxxxxxxxxxxxx"
  }
}
```

### 方法 3: 使用 API 获取

```bash
curl -X GET "https://open.feishu.cn/open-apis/im/v1/chats" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 三、配置步骤

### 3.1 获取会话 ID

按照上述方法获取群聊的 `chat_id`

### 3.2 配置环境变量

编辑 `/opt/ftseek/FTSeek-US/.env.us`:

```bash
# 在文件末尾添加
FEISHU_CHAT_ID=oc_xxxxxxxxxxxxx  # 替换为实际的群聊 ID
```

### 3.3 重启服务

```bash
# 如果使用 systemd
sudo systemctl restart ftseek_us

# 或手动重启
cd /opt/ftseek/FTSeek-US
python3 main.py --stop
python3 main.py --start
```

### 3.4 测试推送

```bash
cd /opt/ftseek/FTSeek-US
python3 -m app.reports.hourly_trade_push
python3 -m app.reports.daily_health_report
```

---

## 四、常见问题

### Q1: 找不到群聊 ID

**解决**: 
- 群聊 ID 可能在群设置的底部
- 或者联系群管理员获取
- 或使用飞书桌面客户端查看

### Q2: 推送失败

**检查**:
```bash
# 1. 确认 chat_id 配置
cat /opt/ftseek/FTSeek-US/.env.us | grep FEISHU_CHAT_ID

# 2. 查看日志
tail -f logs/hourly_push.log
tail -f logs/daily_report.log
```

### Q3: 权限错误

**解决**:
- 确保机器人已添加到目标群聊
- 确保机器人有发送消息权限
- 检查 App ID 和 App Secret 是否正确

---

## 五、当前配置

**已配置**:
```bash
FEISHU_APP_ID=cli_a94874620b38dbc8
FEISHU_APP_SECRET=22BzYqAYzOctHEsoNG53XfZJUDDYzIQX
```

**需要配置**:
```bash
FEISHU_CHAT_ID=oc_xxxxxxxxxxxxx  # 待添加
```

---

## 六、验证配置

配置完成后，运行测试：

```bash
cd /opt/ftseek/FTSeek-US
python3 -m app.reports.hourly_trade_push 2>&1 | grep -E "✅|❌"
```

**预期输出**:
```
✅ 飞书推送服务已初始化 (App ID: cli_a948..., Chat ID: oc_xxx...)
✅ 飞书 access_token 获取成功
✅ 飞书消息发送成功到会话：oc_xxx...
✅ 每小时推送完成
```

---

## 七、下一步

1. ⏳ 获取飞书群聊的 chat_id
2. ⏳ 配置到 `.env.us`
3. ⏳ 测试推送
4. ⏳ 配置定时任务

---

**文档版本**: 1.0  
**最后更新**: 2026-03-26  
**维护人**: AI Assistant
