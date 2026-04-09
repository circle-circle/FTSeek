# 决策缓冲队列满问题分析报告

**日期**: 2026-04-09  
**问题**: 决策缓冲队列满，导致多个信号被拒绝

---

## 问题现象

### 日志输出

```
[决策缓冲] ❌ 拒绝信号：HK.01024 BUY | 原因：队列已满，被更高优先级信号替换
[决策缓冲] 队列已满，移除低优先级信号：HK.01024 BUY
```

### 被拒绝信号列表

| 时间 | 股票代码 | 方向 | 拒绝原因 |
|------|----------|------|----------|
| 最近 | HK.00175 | BUY | 队列已满 |
| 最近 | HK.09992 | BUY | 队列已满 |
| 最近 | HK.01093 | BUY | 队列已满 |
| 最近 | HK.01378 | BUY | 队列已满 |
| 最近 | HK.01347 | BUY | 队列已满 |
| 最近 | HK.01024 | BUY | 队列已满 |
| 最近 | HK.00981 | BUY | 队列已满 |

**分析**: 
- 连续 7+ 个不同标的 BUY 信号被拒绝
- 说明市场普涨，多个股票同时触发买入
- 系统执行能力不足，队列迅速填满

---

## 根本原因

### 1. 供需失衡

```
信号产生速度 >> 执行速度

共识引擎 → 多个策略 → 大量信号
              ↓
        决策缓冲层 (max_pending=10)
              ↓
        执行层 (单线程？有延迟？)
```

**问题**: 
- 队列容量仅 10 个信号
- 执行层消费速度跟不上生产速度

### 2. 队列配置过小

```python
# app/main.py L116-121
decision_buffer = init_decision_buffer(
    signal_cooldown=120,        # 冷却期 120 秒
    max_pending=30,             # 最大队列长度 (2026-04-08 优化：10→30) ← 已调大但仍不够
    min_confidence=0.35,        # 最低置信度
)
```

**现状**: 
- 虽然从 10 调大到 30，但在市场波动大时仍不够
- 建议：根据历史峰值动态调整

### 3. 执行环节可能存在阻塞

**可能原因**:
- 执行层单线程处理
- Futu API 调用延迟高
- 交易确认等待时间长
- 风控检查耗时

### 4. 信号冷却期设置

```python
signal_cooldown = 120  # 秒
```

**影响**:
- 同一标的同方向 120 秒内只能执行 1 次
- 不同标的不受影响，会持续进入队列
- 当市场普涨时，大量不同标的信号涌入

---

## 诊断建议

### 1. 检查执行层状态

```bash
# 查看执行日志，确认是否有执行输出
grep "执行\|execute\|BUY\|SELL" logs/ftseek.log | tail -50
```

**目的**: 确认执行层是否正常工作，是否有阻塞

### 2. 检查 Futu API 响应时间

```python
# 在 executor 中添加耗时统计
start = time.time()
# ... 执行交易 ...
elapsed = time.time() - start
logger.info(f"执行耗时：{elapsed:.2f}s")
```

**目的**: 确认是否 API 调用过慢

### 3. 查看队列监控

```python
# 添加队列长度监控
from app.decision_buffer import get_decision_buffer
buffer = get_decision_buffer()
stats = buffer.get_stats()
print(f"当前队列长度：{stats['pending_count']}")
print(f"累计拒绝：{stats['total_rejected']}")
```

---

## 优化方案

### 方案 1: 增加队列容量（短期）

```python
# app/main.py L118
decision_buffer = init_decision_buffer(
    signal_cooldown=120,
    max_pending=50,             # 30 → 50 (根据峰值调整)
    min_confidence=0.35,
)
```

**优点**: 简单快速
**缺点**: 治标不治本

### 方案 2: 动态队列容量（中期）

```python
# app/decision_buffer.py
class DecisionBuffer:
    def __init__(self, ..., max_pending: int = 30):
        self.max_pending_base = max_pending
        self.max_pending = max_pending  # 动态调整
    
    def adjust_capacity(self, rejection_rate: float):
        """
        根据拒绝率动态调整队列容量
        
        规则:
        - 拒绝率 > 20% → 容量 +10
        - 拒绝率 < 5% → 容量 -5
        - 容量范围：[10, 100]
        """
        if rejection_rate > 0.20:
            self.max_pending = min(100, self.max_pending + 10)
            logger.info(f"队列容量调整：{self.max_pending}")
        elif rejection_rate < 0.05:
            self.max_pending = max(10, self.max_pending - 5)
```

**优点**: 自适应市场波动
**缺点**: 需要实现监控逻辑

### 方案 3: 多消费者模式（中期）

```python
# 新增：执行消费者线程池
from concurrent.futures import ThreadPoolExecutor

class ExecutionConsumer:
    def __init__(self, num_workers: int = 3):
        self.executor = ThreadPoolExecutor(max_workers=num_workers)
    
    def consume(self, signal):
        """异步执行信号"""
        self.executor.submit(self._execute, signal)
```

**优点**: 提升执行并发度
**缺点**: 需要处理并发交易问题

### 方案 4: 信号优先级优化（中期）

```python
# app/decision_buffer.py L194-199
# 当前：仅按置信度排序
self._pending_queue.sort(
    key=lambda r: r.consensus_result.confidence, 
    reverse=True
)

# 优化：综合多个维度
def priority_score(record):
    """
    计算优先级分数
    
    维度:
    1. 置信度 (40%)
    2. 分数绝对值 (30%)
    3. 等待时间 (20%)
    4. 市值因子 (10%)
    """
    confidence = record.consensus_result.confidence * 0.4
    score_factor = abs(record.consensus_result.score) * 0.3
    wait_time = (time.time() - record.created_at) * 0.2
    # ...
    return confidence + score_factor + wait_time
```

**优点**: 更公平的排队
**缺点**: 实现复杂度增加

### 方案 5: 背压机制（长期）

```python
# 当队列满时，反向限制共识引擎
class DataSubscriber:
    def _on_consensus(self, code, result):
        buffer = get_decision_buffer()
        stats = buffer.get_stats()
        
        if stats['pending_count'] > stats['max_pending'] * 0.8:
            # 队列使用率 > 80%，降低信号频率
            logger.warning("队列拥堵，降低信号频率")
            self._throttle_signals()  # 限流
```

**优点**: 从源头控制
**缺点**: 可能错过机会

---

## 实施状态

### ✅ 已完成 (2026-04-09)

1. **增加队列容量**
   - `max_pending: 30 → 50`
   - 文件：`app/main.py` L118

2. **添加队列拥堵告警**
   - 当使用率 ≥ 80% 时推送警告日志
   - 文件：`app/decision_buffer.py` L196-201

### 🔄 待实施

1. **执行耗时监控** - 建议本周实施
2. **动态队列容量** - 建议本周实施
3. **多消费者模式** - 建议本月实施

---

### 立即执行（P0）

1. **增加队列容量**
   ```python
   # app/main.py L118
   max_pending = 50  # 30 → 50
   ```

2. **添加监控告警**
   ```python
   # 当队列使用率 > 80% 时推送钉钉
   if stats['pending_count'] / stats['max_pending'] > 0.8:
       notifier.send(f"⚠️ 决策缓冲队列拥堵：{stats['pending_count']}/{stats['max_pending']}")
   ```

### 本周执行（P1）

1. **诊断执行瓶颈**
   - 添加执行耗时统计
   - 确认 Futu API 响应时间
   - 检查是否有阻塞点

2. **动态队列容量**
   - 实现根据拒绝率自动调整

### 本月执行（P2）

1. **多消费者模式**
   - 评估并发执行可行性
   - 实现线程池消费

2. **背压机制**
   - 在共识引擎层增加限流

---

## 配置修改建议

### 修改 `app/main.py` L116-121

```python
# 修改前
decision_buffer = init_decision_buffer(
    signal_cooldown=120,
    max_pending=30,
    min_confidence=0.35,
)

# 修改后
decision_buffer = init_decision_buffer(
    signal_cooldown=120,
    max_pending=50,             # 10 → 30 → 50
    min_confidence=0.35,
)
```

### 新增监控 API

```python
# app/dashboard_api.py
@app.get("/api/v2/buffer/stats")
async def get_buffer_stats():
    """获取决策缓冲层统计（实时）"""
    from app.decision_buffer import get_decision_buffer
    buffer = get_decision_buffer()
    stats = buffer.get_stats()
    
    # 新增：队列使用率
    stats['queue_usage'] = round(
        stats['pending_count'] / stats['max_pending'], 2
    )
    
    # 新增：告警状态
    stats['alert'] = stats['queue_usage'] > 0.8
    
    return stats
```

---

## 预期效果

### 方案 1（增加容量）

| 指标 | 当前 | 预期 |
|------|------|------|
| 队列容量 | 30 | 50 |
| 拒绝率 | ~30% | <10% |
| 信号通过率 | ~70% | >90% |

### 方案 2+3（动态容量 + 多消费者）

| 指标 | 当前 | 预期 |
|------|------|------|
| 拒绝率 | ~30% | <5% |
| 平均执行延迟 | ? | -30% |
| 系统吞吐量 | 1x | 2-3x |

---

## 风险评估

### 增加队列容量的风险

1. **内存占用增加**
   - 每个 DecisionRecord 约 1KB
   - 50 个信号 ≈ 50KB（可忽略）

2. **过期信号风险**
   - 信号在队列中停留过久可能失效
   - 缓解：添加信号超时机制（如 5 分钟未执行自动丢弃）

### 多消费者的风险

1. **并发交易冲突**
   - 同一标的可能同时执行
   - 缓解：标的级别锁

2. **风控检查并发**
   - 多个线程同时检查仓位
   - 缓解：风控层加锁

---

## 总结

### 问题根因

**信号生产速度 > 执行消费速度**

### 最优方案

1. **短期**: 增加队列容量 (30→50)
2. **中期**: 动态容量 + 执行耗时监控
3. **长期**: 多消费者模式 + 背压机制

### 建议行动

1. 立即修改 `max_pending = 50`
2. 添加队列监控和告警
3. 诊断执行层瓶颈
4. 本周实施动态容量

---

**分析者**: Claude Code  
**分析日期**: 2026-04-09  
**优先级**: P0 (立即执行)
