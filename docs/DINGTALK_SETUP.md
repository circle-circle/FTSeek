# FTSeek 钉钉推送配置指南

**5 分钟完成钉钉机器人配置**

---

## 📱 配置步骤

### 步骤 1: 创建钉钉群

1. 打开钉钉
2. 创建群聊 (或选择现有群)
3. 群名称建议：`FTSeek 交易信号`

### 步骤 2: 添加机器人

1. 进入群设置
2. 点击 **智能群助手**
3. 点击 **添加机器人**
4. 选择 **自定义** (通过 Webhook 接入)

### 步骤 3: 配置机器人

**机器人设置**:
- 机器人名字：`FTSeek 助手`
- 头像：(可选)
- ✅ 勾选 **自定义关键词**: 添加关键词 `FTSeek` 或 `信号`
- ✅ 勾选 **加签** (安全设置)

**完成后复制**:
- Webhook URL: `https://oapi.dingtalk.com/robot/send?access_token=xxx`
- Secret: `SECxxx`

### 步骤 4: 设置环境变量

```bash
# 编辑 ~/.bashrc 或 ~/.zshrc
export DINGTALK_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=xxx'
export DINGTALK_SECRET='SECxxx'

# 使配置生效
source ~/.bashrc
```

**或者** 在 systemd 服务中配置:

```ini
# /etc/systemd/system/ftseek.service
[Service]
Environment="DINGTALK_WEBHOOK_URL=https://..."
Environment="DINGTALK_SECRET=SEC..."
```

### 步骤 5: 测试推送

```bash
cd /opt/ftseek/FTSeek
python3 test_dingtalk.py
```

---

## 🔔 消息格式

### 1. 文本消息

```
[FTSeek v2.1 测试]

时间：2026-02-27 17:00:00
版本：v2.1 模块化版本

功能特性:
✅ 模块化设计 (4 个独立模块)
✅ 方向概率显示 (BUY%/SELL%)
✅ 信号等级分类
✅ 三按钮交互

部署状态：运行正常
```

### 2. 信号推送 (带概率条)

```
🔴 HK.00700 BUY 信号

标的：HK.00700 腾讯控股

方向：BUY

概率:
买入：█████████ 87.5%
卖出：█ 12.5%

置信度：83.0%
等级：recommended

技术分：+0.85
情绪分：+0.60

原因：方向:BUY | 置信度:83% | 技术分:+0.85 | 情绪分:+0.60 | RSI:25.0 | MACD:金叉

⏰ 有效期 15 分钟

[✅ 确认买入] [⏰ 延期 15 分钟] [❌ 忽略]
```

### 3. 执行结果

```
[交易结果] ✅ 执行成功
标的：HK.00700
方向：BUY
价格：HK$520.00
数量：100
详情：订单号：MO123456789
```

---

## 🧪 测试命令

### 测试文本消息
```bash
python3 -c "
from app.notifier.dingtalk import DingTalkNotifier
notifier = DingTalkNotifier()
notifier.send_text('FTSeek 测试消息')
"
```

### 测试信号推送
```bash
python3 -c "
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.base import Signal, SignalAction

notifier = DingTalkNotifier()
signal = Signal(
    code='HK.00700',
    name='腾讯控股',
    action=SignalAction.BUY,
    price=520.0,
    strategy='fusion_v2',
    extra={'direction': 'BUY', 'buy_probability': 0.875}
)
notifier.send_signal(signal)
"
```

### 完整测试
```bash
python3 test_dingtalk.py
```

---

## ⚠️ 常见问题

### Q1: 消息发送失败

**检查**:
1. Webhook URL 是否正确
2. Secret 是否匹配
3. 网络是否通畅

**测试**:
```bash
curl -X POST "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"msgtype":"text","text":{"content":"测试"}}'
```

### Q2: 消息被拦截

**原因**: 未设置关键词或关键词不匹配

**解决**:
1. 钉钉群 → 机器人设置
2. 检查自定义关键词
3. 添加 `FTSeek` 或 `信号` 关键词

### Q3: 按钮不显示

**原因**: 消息类型不是 actionCard

**解决**: 确保使用 `send_signal()` 方法，会自动发送 actionCard 格式

### Q4: 频率限制

**钉钉限制**: 每个机器人每分钟最多 20 条

**解决**: FTSeek 已内置限流，会自动合并摘要

---

## 📊 推送场景

| 场景 | 消息类型 | 内容 |
|------|----------|------|
| 系统启动 | 文本 | 系统状态、策略列表 |
| 交易信号 | ActionCard | 概率、等级、三按钮 |
| 执行结果 | 文本 | 订单号、状态 |
| 频率限制 | Markdown | 信号摘要 |
| 绩效报告 | Markdown | 周/月统计 |
| 系统异常 | 文本 | 错误信息 |

---

## 🔐 安全建议

1. **不要泄露 Webhook URL**
2. **启用加签验证** (FTSeek 已支持)
3. **设置关键词过滤**
4. **限制群成员查看权限**

---

## 📚 更多资源

- **模块化配置**: `docs/MODULAR_CONFIG.md`
- **快速启动**: `QUICKSTART_V2.md`
- **功能总结**: `docs/FEATURES_V2_1.md`

---

**配置完成后运行 `python3 test_dingtalk.py` 验证!** 🎉
