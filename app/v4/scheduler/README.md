# TradingScheduler - 定时任务调度器

交易定时任务调度器，支持日报、周报和绩效检查的自动调度。

## 功能特性

- **日报任务**: 自动推送港股/美股日报
  - 港股：周一至周五 16:30 HKT (收盘后 30 分钟)
  - 美股：周一至周五 04:30 EST (收盘后 30 分钟)
- **周报任务**: 每周日 20:00 Asia/Shanghai 推送周报
- **绩效检查**: 每小时检查止盈止损条件，自动触发退出
- **时区支持**: 自动处理 HKT/EST/Asia/Shanghai 时区转换
- **错误处理**: 任务失败自动记录日志，支持重试
- **线程安全**: 使用 APScheduler 后台调度器

## 快速开始

```python
from app.v4.scheduler import TradingScheduler
from app.v4.report.report_generator import ReportGenerator
from app.v4.core.decision_engine import DecisionEngine

# 初始化依赖
report_generator = ReportGenerator(db_path="data/trading.db")
decision_engine = DecisionEngine(db_path="data/trading.db")

# 创建调度器
scheduler = TradingScheduler(
    report_generator=report_generator,
    decision_engine=decision_engine,
    db_path="data/trading.db"
)

# 配置任务
scheduler.schedule_daily_report("0700.HK", "HK")  # 港股日报
scheduler.schedule_daily_report("TSLA", "US")     # 美股日报
scheduler.schedule_weekly_report("0700.HK")       # 周报
scheduler.schedule_performance_check()            # 绩效检查

# 启动调度器
scheduler.start()
```

## API 文档

### TradingScheduler

#### 初始化

```python
scheduler = TradingScheduler(
    report_generator: ReportGenerator,
    decision_engine: Optional[DecisionEngine] = None,
    db_path: str = None
)
```

**参数**:
- `report_generator`: 报告生成器实例 (必需)
- `decision_engine`: 决策引擎实例 (可选，用于绩效检查)
- `db_path`: 数据库路径

#### 方法

##### `start()`
启动调度器。

```python
scheduler.start()
```

##### `stop()`
停止调度器，等待所有任务完成。

```python
scheduler.stop()
```

##### `schedule_daily_report(code: str, market: str)`
调度日报任务。

**参数**:
- `code`: 股票代码 (如 "0700.HK", "TSLA")
- `market`: 市场类型 ("HK" 或 "US")

**调度时间**:
- HK: 周一至周五 16:30 HKT
- US: 周一至周五 04:30 EST

```python
scheduler.schedule_daily_report("0700.HK", "HK")
```

##### `schedule_weekly_report(code: str)`
调度周报任务。

**参数**:
- `code`: 股票代码

**调度时间**: 周日 20:00 Asia/Shanghai

```python
scheduler.schedule_weekly_report("0700.HK")
```

##### `schedule_performance_check()`
调度绩效检查任务 (每小时一次)。

检查所有持仓的止盈止损条件，自动触发退出。

```python
scheduler.schedule_performance_check()
```

### 单例模式

```python
from app.v4.scheduler import init_trading_scheduler, get_trading_scheduler

# 初始化单例
scheduler = init_trading_scheduler(
    report_generator=report_generator,
    decision_engine=decision_engine
)

# 获取单例
scheduler = get_trading_scheduler()
```

## 定时任务配置

| 任务类型 | 股票代码 | 市场 | 调度时间 | 时区 |
|---------|---------|------|---------|------|
| 日报 | 0700.HK | HK | 周一~周五 16:30 | HKT (UTC+8) |
| 日报 | 9988.HK | HK | 周一~周五 16:30 | HKT (UTC+8) |
| 日报 | TSLA | US | 周一~周五 04:30 | EST (UTC-5) |
| 日报 | NVDA | US | 周一~周五 04:30 | EST (UTC-5) |
| 周报 | 所有 | - | 周日 20:00 | Asia/Shanghai |
| 绩效检查 | 所有 | - | 每小时整点 | Asia/Shanghai |

## 错误处理

调度器会自动捕获并记录所有任务异常:

```
ERROR ftseek.v4.trading_scheduler - 日报推送失败：0700.HK - Webhook timeout
ERROR ftseek.v4.trading_scheduler - 绩效检查失败：Database locked
```

任务配置了 `misfire_grace_time`:
- 日报：5 分钟容错
- 周报：10 分钟容错
- 绩效检查：1 分钟容错

## 日志配置

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 调整调度器日志级别
logging.getLogger("ftseek.v4.trading_scheduler").setLevel(logging.DEBUG)
```

## 测试

运行单元测试:

```bash
cd /opt/ftseek/FTSeek
python -m pytest tests/v4/test_trading_scheduler.py -v
```

查看测试覆盖率:

```bash
python -m pytest tests/v4/test_trading_scheduler.py --cov=app.v4.scheduler.trading_scheduler --cov-report=term-missing
```

## 示例

完整示例见 `examples/scheduler_example.py`。

## 依赖

- APScheduler >= 3.10.4
- pytz (时区处理)
- Python >= 3.8

## 注意事项

1. **时区**: 调度器使用各市场本地时区，自动处理夏令时
2. **任务幂等**: 重复调用 `schedule_*` 会替换现有任务
3. **优雅关闭**: 调用 `stop()` 会等待所有任务完成
4. **资源清理**: 调度器停止后会自动释放资源
5. **生产部署**: 建议使用 systemd 或 supervisor 管理调度器进程
