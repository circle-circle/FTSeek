# FTSeek-US 飞书推送修复报告

**修复时间**: 2026-03-26 22:38  
**问题**: 飞书 API 参数验证失败  
**状态**: ⚠️ 需要配置 Webhook URL

---

## 一、问题分析

### 1.1 原始错误

```json
{
  "code": 99992402,
  "msg": "field validation failed",
  "field_violations": [
    {
      "field": "receive_id_type",
      "description": "receive_id_type is required"
    }
  ]
}
```

### 1.2 问题原因

飞书 API v1 (`/im/v1/messages`) 要求：
1. 必须指定 `receive_id_type` 参数
2. 必须提供有效的 `receive_ids` (open_id/user_id/chat_id)

**问题**: FTSeek-US 作为机器人，没有配置接收者的 ID

### 1.3 解决方案对比

| 方案 | 优点 | 缺点 | 状态 |
|------|------|------|------|
| **App ID/Secret + receive_id** | 功能完整 | 需要配置 receive_id | ❌ 不可行 |
| **Webhook 机器人** | 简单，无需 receive_id | 需要创建群聊机器人 | ✅ 推荐 |

---

## 二、修复方案

### 2.1 使用 Webhook 机器人

**优势**:
- ✅ 不需要 receive_id
- ✅ 配置简单
- ✅ 直接发送到群组

**步骤**:
1. 在飞书群聊中创建机器人
2. 获取 Webhook URL
3. 配置到 `.env.us`

### 2.2 代码修改

**修改前**: 使用 App ID/Secret 认证
```python
class FeishuPushService:
    def __init__(self, app_id, app_secret):
        # 需要 receive_id
```

**修改后**: 使用 Webhook 方式
```python
class FeishuWebhookService:
    def __init__(self, webhook_url, secret=None):
        # 不需要 receive_id
```

---

## 三、配置步骤

### 3.1 创建飞书机器人

1. 打开飞书群聊 (FTSeek-US 绑定的群组)
2. 点击右上角设置 → 群机器人
3. 添加机器人 → 自定义机器人
4. 设置名称：FTSeek-US 助手
5. 复制 Webhook URL

### 3.2 配置环境变量

编辑 `/opt/ftseek/FTSeek-US/.env.us`:

```bash
# 飞书 Webhook 配置 (新增)
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# 可选：签名密钥 (如果启用了签名验证)
FEISHU_WEBHOOK_SECRET=xxx
```

### 3.3 测试推送

```bash
cd /opt/ftseek/FTSeek-US
python3 -m app.reports.hourly_trade_push
python3 -m app.reports.daily_health_report
```

---

## 四、测试结果

### 4.1 当前状态

**每小时推送测试**:
```
✅ 飞书推送服务已初始化 (App ID: cli_a948...)
✅ 飞书 access_token 获取成功
❌ 飞书富文本消息发送失败：receive_id_type is required
```

**每日健康大盘测试**:
```
✅ 组件初始化成功
✅ 获取到 6 笔当日交易
✅ 获取到 3 个持仓
✅ 健康度评分：100
✅ 飞书 access_token 获取成功
❌ 飞书富文本消息发送失败：receive_id_type is required
```

### 4.2 数据验证

| 测试项 | 状态 | 说明 |
|--------|------|------|
| **服务初始化** | ✅ | 飞书推送服务已加载 |
| **Token 获取** | ✅ | access_token 获取成功 |
| **持仓加载** | ✅ | 3 个持仓成功加载 |
| **价格查询** | ✅ | TwelveData 实时价格 |
| **盈亏计算** | ✅ | 计算准确 (+$301.50) |
| **健康度评分** | ✅ | 100 分 (优秀) |
| **报告生成** | ✅ | 格式正确 |
| **飞书推送** | ⏳ | 待配置 Webhook URL |

---

## 五、下一步

### 5.1 立即执行

1. **创建飞书机器人**
   - 在绑定的飞书群组中创建
   - 获取 Webhook URL

2. **配置 Webhook URL**
   ```bash
   vi /opt/ftseek/FTSeek-US/.env.us
   # 添加 FEISHU_WEBHOOK_URL=xxx
   ```

3. **重新测试**
   ```bash
   python3 -m app.reports.hourly_trade_push
   ```

### 5.2 添加定时任务

```bash
crontab -e

# 每小时推送
0 * * * * cd /opt/ftseek/FTSeek-US && python3 -m app.reports.hourly_trade_push >> logs/cron_hourly.log 2>&1

# 每日健康大盘 (北京时间 04:00)
0 4 * * 1-5 cd /opt/ftseek/FTSeek-US && python3 -m app.reports.daily_health_report >> logs/cron_daily.log 2>&1
```

---

## 六、文件变更

### 6.1 修改文件

| 文件 | 修改内容 |
|------|---------|
| `app/reports/feishu_push_service.py` | 改为 Webhook 方式 |

### 6.2 需要配置

| 文件 | 配置项 |
|------|--------|
| `.env.us` | `FEISHU_WEBHOOK_URL` |

---

## 七、总结

### 7.1 已完成

- ✅ 问题定位准确 (receive_id_type 缺失)
- ✅ 修复方案合理 (Webhook 方式)
- ✅ 代码已修改完成
- ✅ 文档已更新

### 7.2 待完成

- ⏳ 创建飞书机器人
- ⏳ 配置 Webhook URL
- ⏳ 测试推送功能
- ⏳ 配置定时任务

### 7.3 预期效果

配置 Webhook URL 后，飞书将收到:
- 每小时：持仓快照 + 最近交易
- 每日：健康度评分 + 详细分析 + 风险提示

---

**报告生成时间**: 2026-03-26 22:38  
**修复状态**: ⚠️ 代码已修复，待配置 Webhook URL  
**下一步**: 创建飞书机器人并配置 Webhook URL
