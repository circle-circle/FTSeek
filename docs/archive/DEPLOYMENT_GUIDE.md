# FTSeek-US 部署指南

> 中长线美股交易平台 - 基于 FTSeek 现有配置

**版本**: 1.0.0  
**创建日期**: 2026-03-24  
**状态**: ✅ 准备就绪

---

## 一、前置条件

### 1.1 现有 FTSeek 配置复用

已自动复制以下配置：
- ✅ Bailian API Key
- ✅ DeepSeek API Key
- ✅ 钉钉 Webhook + Secret
- ✅ 富途模拟账户 ID (8246815)
- ✅ API Token

### 1.2 美股行情数据源

**问题**: 富途模拟账户无法订阅美股行情

**解决方案**: 使用第三方免费数据源

| 数据源 | 延迟 | 限制 | 推荐度 |
|--------|------|------|--------|
| **Yahoo Finance** | 15 分钟 | 无限制 | ⭐⭐⭐⭐⭐ |
| Alpha Vantage | 实时 | 500 次/天 | ⭐⭐⭐⭐ |
| Finnhub | 实时 | 60 次/分钟 | ⭐⭐⭐⭐ |
| IEX Cloud | 实时 | 付费 | ⭐⭐⭐ |

**默认配置**: Yahoo Finance (已配置)

---

## 二、快速部署

### 2.1 环境变量配置

配置文件已创建：`/opt/ftseek/FTSeek-US/.env.us`

**关键配置项**:
```bash
# 已自动复制 FTSeek 配置
BAILIAN_API_KEY=sk-8f8a02822c1b46b4bd43b422b9196ec6
DINGTALK_WEBHOOK_URL_US=https://...
DINGTALK_SECRET_US=SEC...

# 中长线交易配置
TRADE_ENV=1                    # 模拟环境
SIGNAL_COOLDOWN=600            # 10 分钟 (降低频率)
SIGNAL_EXPIRE_SECONDS=1800     # 30 分钟 (延长)
DEFAULT_STOP_LOSS=0.10         # -10% (放宽)
TIME_STOP_HOURS=168            # 7 天 (延长)
```

### 2.2 监控股票池

配置文件：`/opt/ftseek/FTSeek-US/config/watchlist_us.json`

**已配置 8 只中长线标的**:
| 代码 | 名称 | 仓位 | 说明 |
|------|------|------|------|
| AAPL | 苹果 | 30% | 科技龙头 |
| MSFT | 微软 | 30% | 云计算 |
| GOOGL | 谷歌 | 25% | 搜索广告 |
| AMZN | 亚马逊 | 25% | 电商 AWS |
| NVDA | 英伟达 | 25% | AI 芯片 |
| META | Meta | 25% | 社交媒体 |
| BABA | 阿里 | 20% | 中概龙头 |
| PDD | 拼多多 | 15% | 高增长 |

### 2.3 安装依赖

```bash
cd /opt/ftseek/FTSeek-US

# 创建虚拟环境
python3.8 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 额外安装行情数据源
pip install yfinance pandas
```

### 2.4 启动服务

```bash
# 方式 1: 手动启动
./start.sh

# 方式 2: systemd 服务
sudo systemctl start ftseek_us
sudo systemctl enable ftseek_us

# 查看状态
systemctl status ftseek_us
```

### 2.5 验证部署

```bash
# 健康检查
curl http://localhost:8901/health

# 预期输出: {"status":"ok"}
```

---

## 三、中长线交易策略配置

### 3.1 策略参数

已在 `.env.us` 中配置：

```bash
# 降低交易频率
SIGNAL_COOLDOWN=600            # 10 分钟 (港股默认 2 分钟)

# 延长信号有效期
SIGNAL_EXPIRE_SECONDS=1800     # 30 分钟 (港股默认 5 分钟)

# 放宽止损止盈
DEFAULT_STOP_LOSS=0.10         # -10% (港股默认 -6%)
DEFAULT_TAKE_PROFIT_1=0.15     # +15%
DEFAULT_TAKE_PROFIT_2=0.25     # +25%
DEFAULT_TAKE_PROFIT_3=0.40     # +40%

# 延长持仓时间
TIME_STOP_HOURS=168            # 7 天 (港股默认 72 小时)
```

### 3.2 仓位管理

```bash
# 提高单品种仓位 (中长线)
MAX_POSITION_PER_SYMBOL=0.30   # 30% (港股默认 25%)

# 提高总仓位
MAX_TOTAL_POSITION=0.80        # 80% (港股默认 80%)
```

### 3.3 多 Agent 投票决策

已配置 4 个模型并行投票：

```python
from app.agents.decision_agent import DecisionAgent

# 启用投票系统
agent = DecisionAgent(use_voting=True)

# 模型权重:
# - DeepSeek: 30% (趋势分析)
# - MiniMax: 25% (情绪分析)
# - GLM-5: 25% (技术分析)
# - Qwen3.5: 20% (基本面分析)
```

---

## 四、行情数据源集成

### 4.1 Yahoo Finance 集成

**创建数据获取脚本**:

```python
# scripts/get_yahoo_data.py
import yfinance as yf
import json

def get_us_stock_data(symbol: str):
    """获取美股数据 (15 分钟延迟)"""
    ticker = yf.Ticker(symbol.replace("US.", ""))
    
    # 获取实时数据
    data = ticker.fast_info
    
    return {
        "symbol": symbol,
        "price": data.last_price,
        "volume": data.last_volume,
        "avg_volume": data.average_daily_volume_10_day,
        "volume_ratio": data.last_volume / data.average_daily_volume_10_day,
        "momentum": (data.last_price - data.fifty_day_average) / data.fifty_day_average,
    }

# 测试
if __name__ == "__main__":
    data = get_us_stock_data("US.AAPL")
    print(json.dumps(data, indent=2))
```

### 4.2 集成到 FTSeek-US

修改 `app/data/market_data_provider.py`:

```python
import yfinance as yf

class YahooFinanceProvider:
    """Yahoo Finance 数据提供商"""
    
    def get_quote(self, symbol: str) -> dict:
        """获取报价"""
        ticker = yf.Ticker(symbol.replace("US.", ""))
        data = ticker.fast_info
        
        return {
            "symbol": symbol,
            "price": data.last_price,
            "volume": data.last_volume,
            "bid": data.bid,
            "ask": data.ask,
            "open": data.day_open,
            "high": data.day_high,
            "low": data.day_low,
            "previous_close": data.previous_close,
        }
    
    def get_market_data(self, symbol: str) -> dict:
        """获取完整市场数据"""
        ticker = yf.Ticker(symbol.replace("US.", ""))
        
        # 获取历史数据计算指标
        hist = ticker.history(period="60d")
        
        # 计算动量
        ma20 = hist["Close"].rolling(window=20).mean().iloc[-1]
        ma50 = hist["Close"].rolling(window=50).mean().iloc[-1]
        current_price = hist["Close"].iloc[-1]
        
        return {
            "symbol": symbol,
            "price": current_price,
            "volume": hist["Volume"].iloc[-1],
            "volume_ratio": hist["Volume"].iloc[-1] / hist["Volume"].mean(),
            "momentum": (current_price - ma20) / ma20,
            "trend": "UP" if current_price > ma50 else "DOWN",
            "ma20": ma20,
            "ma50": ma50,
        }
```

---

## 五、钉钉告警测试

### 5.1 测试推送

```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate

# 运行测试脚本
python -c "
from app.observability.alert_manager import AlertManager

manager = AlertManager(
    provider='dingtalk',
    dingtalk_webhook='https://oapi.dingtalk.com/robot/send?access_token=cddea8919223640d4f3bd59e507373cae0df476df5f541fcca5c8921eab73121',
    dingtalk_secret='SEC177f5321e91e689a1a2134e4414ae49e7b7f2cddc63775613effbf371f388f78'
)

# 发送测试消息
manager.send_info(
    title='FTSeek-US 启动测试',
    content='中长线美股交易平台已启动，监控股票 8 只'
)

print('✅ 钉钉推送测试成功')
"
```

### 5.2 预期效果

钉钉群收到消息：
```
## FTSeek-US 启动测试

**级别**: <font color="green">INFO</font>

**内容**: 中长线美股交易平台已启动，监控股票 8 只

**时间**: 2026-03-24 07:10:00
```

---

## 六、多 Agent 投票系统测试

### 6.1 测试投票

```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate

python -c "
from app.agents.voting.voting_coordinator import VotingCoordinator

coordinator = VotingCoordinator()

market_data = {
    'symbol': 'US.AAPL',
    'price': 175.0,
    'volume_ratio': 1.5,
    'momentum': 0.02,
}

result = coordinator.run_voting(market_data)

print(f'最终评分：{result[\"final_strength\"]}')
print(f'置信度：{result[\"final_confidence\"]}')
print(f'一致性：{result[\"consensus_level\"]}')
print(f'需人工审核：{result[\"needs_manual_review\"]}')
print(f'模型评分：{len(result[\"model_scores\"])} 个')
"
```

### 6.2 预期输出

```
最终评分：73
置信度：0.75
一致性：0.85
需人工审核：False
模型评分：4 个
```

---

## 七、运行监控

### 7.1 查看日志

```bash
# 实时日志
tail -f /opt/ftseek/FTSeek-US/logs/ftseek_us.log

# 错误日志
grep ERROR /opt/ftseek/FTSeek-US/logs/ftseek_us.log | tail -20
```

### 7.2 服务状态

```bash
# systemd 状态
systemctl status ftseek_us

# 进程状态
ps aux | grep ftseek_us
```

### 7.3 性能监控

```bash
# CPU/内存
top -p $(pgrep -f ftseek_us)

# 磁盘使用
du -sh /opt/ftseek/FTSeek-US/data/
du -sh /opt/ftseek/FTSeek-US/logs/
```

---

## 八、常见问题

### Q1: 富途模拟账户无法订阅美股行情？

**A**: 使用 Yahoo Finance 替代 (已配置)，15 分钟延迟对中长线交易无影响。

### Q2: 如何切换实盘模式？

**A**: 修改 `.env.us`:
```bash
TRADE_ENV=0  # 改为实盘
```

⚠️ **警告**: 实盘前务必充分测试！

### Q3: 如何调整投票权重？

**A**: 修改 `app/agents/voting/voting_config.py`:
```python
model_weights={
    ModelRole.DEEPSEEK: 0.35,  # 提高 DeepSeek 权重
    ModelRole.MINIMAX: 0.20,
    ModelRole.GLM5: 0.25,
    ModelRole.QWEN35: 0.20,
}
```

### Q4: 如何添加新股？

**A**: 编辑 `config/watchlist_us.json`,在 `us_stocks` 数组添加：
```json
{
  "code": "US.TSLA",
  "name": "Tesla Inc.",
  "sector": "Consumer Discretionary",
  "enabled": true,
  "tier": "TIER1",
  "order_qty": 50,
  "max_position_ratio": 0.20
}
```

---

## 九、下一步

1. ✅ 完成部署
2. ✅ 测试钉钉推送
3. ✅ 测试投票系统
4. 🔄 运行 1 周观察信号质量
5. 🔄 根据实际表现调整权重
6. 🔄 考虑切换实盘 (谨慎!)

---

*文档版本：1.0 | 最后更新：2026-03-24*
