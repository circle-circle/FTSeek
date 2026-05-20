# FTSeek-US 数据源配置指南

**更新日期**: 2026-03-24  
**状态**: ✅ 已配置

---

## 一、可用数据源对比

| 数据源 | 类型 | 延迟 | 限制 | 推荐度 | 状态 |
|--------|------|------|------|--------|------|
| **富途 OpenD** | 实时行情 | 实时 | 无 | ⭐⭐⭐⭐⭐ | ✅ 已配置 |
| Yahoo Finance | 延迟行情 | 15 分钟 | 无 | ⭐⭐⭐ | ⚠️ 网络问题 |
| Alpha Vantage | 实时行情 | 实时 | 500 次/天 | ⭐⭐⭐⭐ | ❌ 未配置 |
| Finnhub | 实时行情 | 实时 | 60 次/分钟 | ⭐⭐⭐⭐ | ❌ 未配置 |
| IEX Cloud | 实时行情 | 实时 | 付费 | ⭐⭐⭐ | ❌ 未配置 |

---

## 二、推荐方案：富途 OpenD

### 优势
- ✅ **实时行情**：无延迟
- ✅ **免费**：富途用户提供
- ✅ **稳定**：本地部署
- ✅ **已配置**：FTSeek 正在使用

### 配置
```bash
# .env.us 配置
DATA_SOURCE=futu_opend
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111
```

### 使用示例
```python
from futu import OpenQuoteContext

# 连接 OpenD
quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)

# 获取实时行情
ret_code, ret_data = quote_ctx.get_market_basic_quote('US.AAPL')

if ret_code == 0:
    price = ret_data['last_price'][0]
    print(f"AAPL: ${price}")
```

---

## 三、备用方案：Yahoo Finance

### 优势
- ✅ **免费**：无需 API Key
- ✅ **简单**：易于集成
- ⚠️ **延迟**：15 分钟延迟

### 配置
```bash
# .env.us 配置
DATA_SOURCE=yahoo_finance
YAHOO_FINANCE_CACHE_TTL=900  # 900 秒缓存
```

### 使用示例
```python
import yfinance as yf

# 获取数据
ticker = yf.Ticker("AAPL")
data = ticker.fast_info

print(f"Price: ${data.last_price}")
print(f"Volume: {data.last_volume}")
```

---

## 四、LLM API 配置

### 已配置 API (复制 FTSeek)

| 模型 | API Key | 状态 |
|------|---------|------|
| **Bailian (Qwen)** | sk-8f8a02822c1b46b4bd43b422b9196ec6 | ✅ 已配置 |
| **DeepSeek** | sk-8f8a02822c1b46b4bd43b422b9196ec6 | ✅ 已配置 |
| **GLM-5** | (使用 Bailian) | ✅ 已配置 |
| **MiniMax** | (使用 Bailian) | ✅ 已配置 |

### 配置详情
```bash
# Bailian API (主)
BAILIAN_API_KEY=sk-8f8a02822c1b46b4bd43b422b9196ec6
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/api/v1
BAILIAN_MODEL=qwen-plus

# DeepSeek API (投票系统)
DEEPSEEK_API_KEY=sk-8f8a02822c1b46b4bd43b422b9196ec6
DEEPSEEK_BASE_URL=https://dashscope.aliyuncs.com/api/v1
DEEPSEEK_MODEL=deepseek-chat
```

### 多 Agent 模型映射
```python
AGENT_MODEL_MAPPING = {
    "main": "bailian/qwen3.5-plus",      # 主协调器
    "decision": "deepseek/deepseek-chat", # 决策 Agent
    "risk": "bailian/glm-5",              # 风险 Agent
    "execution": "bailian/qwen3.5-plus",  # 执行 Agent
    "observability": "bailian/qwen3.5-plus", # 观测 Agent
}
```

---

## 五、数据源切换

### 切换到 Yahoo Finance
```bash
cd /opt/ftseek/FTSeek-US
vim .env.us

# 修改
DATA_SOURCE=yahoo_finance

# 重启服务
./start.sh
```

### 切换到富途 OpenD (推荐)
```bash
cd /opt/ftseek/FTSeek-US
vim .env.us

# 修改
DATA_SOURCE=futu_opend

# 重启服务
./start.sh
```

---

## 六、API Key 管理

### 查看当前配置
```bash
cat /opt/ftseek/FTSeek-US/.env.us | grep -E "API_KEY|SECRET|TOKEN"
```

### 更新 API Key
```bash
vim /opt/ftseek/FTSeek-US/.env.us

# 修改对应 Key
BAILIAN_API_KEY=sk-new_key_here
```

### 安全建议
- ✅ API Key 通过环境变量管理
- ✅ 不要硬编码在代码中
- ✅ 定期轮换 Key
- ✅ 限制 API 调用频率

---

## 七、测试数据源

### 测试富途 OpenD
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate

python3.8 << 'EOF'
from futu import OpenQuoteContext

quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)

# 测试 AAPL
ret_code, ret_data = quote_ctx.get_market_basic_quote('US.AAPL')

if ret_code == 0:
    print(f"✅ AAPL: ${ret_data['last_price'][0]}")
else:
    print(f"❌ 获取失败：{ret_data}")

quote_ctx.close()
EOF
```

### 测试 Yahoo Finance
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate

python3.8 << 'EOF'
import yfinance as yf

ticker = yf.Ticker("AAPL")
data = ticker.fast_info

if data:
    print(f"✅ AAPL: ${data.last_price}")
else:
    print(f"❌ 获取失败")
EOF
```

### 测试 LLM API
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate

python3.8 << 'EOF'
import requests
import os
from dotenv import load_dotenv

load_dotenv(".env.us")

api_key = os.getenv("BAILIAN_API_KEY")
url = "https://dashscope.aliyuncs.com/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

data = {
    "model": "qwen-plus",
    "messages": [{"role": "user", "content": "Hello"}]
}

response = requests.post(url, headers=headers, json=data)

if response.status_code == 200:
    print("✅ Bailian API 正常")
else:
    print(f"❌ Bailian API 失败：{response.status_code}")
EOF
```

---

## 八、故障排查

### 问题 1: 富途 OpenD 连接失败
```bash
# 检查 OpenD 是否运行
ps aux | grep FutuOpenD

# 检查端口
netstat -tlnp | grep 11111

# 重启 OpenD
systemctl restart futu_opend
```

### 问题 2: Yahoo Finance 无法访问
```bash
# 检查网络
ping finance.yahoo.com

# 使用代理
export HTTP_PROXY=http://proxy:port
export HTTPS_PROXY=http://proxy:port
```

### 问题 3: LLM API 认证失败
```bash
# 检查 API Key
cat .env.us | grep BAILIAN_API_KEY

# 测试 API
curl -X POST https://dashscope.aliyuncs.com/api/v1/chat/completions \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-plus","messages":[{"role":"user","content":"Hello"}]}'
```

---

## 九、最佳实践

### 1. 数据源选择
- **实时交易**: 使用富途 OpenD
- **历史分析**: 使用 Yahoo Finance
- **备份方案**: 配置多个数据源

### 2. API 调用优化
- **缓存**: 设置合理的 TTL
- **限流**: 控制调用频率
- **重试**: 实现重试机制

### 3. 错误处理
- **超时**: 设置合理的超时时间
- **降级**: 主数据源失败时切换备用
- **日志**: 记录所有 API 调用

---

## 十、总结

### 当前配置状态
| 配置项 | 状态 | 说明 |
|--------|------|------|
| 富途 OpenD | ✅ | 实时行情，已配置 |
| Yahoo Finance | ⚠️ | 网络问题，备用 |
| Bailian API | ✅ | LLM 调用，已配置 |
| DeepSeek API | ✅ | 投票系统，已配置 |
| 钉钉推送 | ✅ | 通知推送，已配置 |

### 推荐配置
```bash
# 数据源
DATA_SOURCE=futu_opend  # 实时行情

# LLM API
BAILIAN_API_KEY=sk-8f8a02822c1b46b4bd43b422b9196ec6  # 主 API
DEEPSEEK_API_KEY=sk-8f8a02822c1b46b4bd43b422b9196ec6  # 投票系统

# 推送
DINGTALK_WEBHOOK_URL_US=...  # 钉钉推送
```

---

**文档版本**: 1.0  
**最后更新**: 2026-03-24  
**维护人**: AI Assistant
